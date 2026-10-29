"""The copilot's tool surface.

One fixed tool list for every page (the list never changes inside a
conversation, so the prompt cache and the model's earlier reasoning stay
valid). Tools that only make sense on one page say so in their result.

Every handler:
  * opens its own DB session (the request session is gone once streaming
    starts) and runs inside `tool_scope(user)`, so the shared scope helpers
    narrow reads to the plants the user may read;
  * re-checks permissions itself — the model's choice of ids is never trusted;
  * returns plain JSON. `runtime` PII-redacts every result before the model
    sees it, and keeps user names out entirely (people come back as ids plus
    designation / department; the browser shows the names).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.user import User
from app.services.agents.tools._scope import readable_plants, restrict_to_plants, tool_scope
from app.services.copilot import form_drafts, ptw_draft
from app.services.copilot.knowledge import hira_library as hira_kb
from app.services.copilot.knowledge import observations as obs_kb

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

# Background specialist runs. Strong refs so the event loop doesn't GC them.
_BACKGROUND: set[asyncio.Task] = set()


@dataclass
class TurnContext:
    user_id: str
    user_plant_id: str | None
    conversation_id: str
    page: dict[str, Any]
    emit: Emit
    # Working copy of the New Permit draft; None when the form isn't open.
    draft: dict[str, Any] | None = None
    now_local: str | None = None
    tz_offset_minutes: int = 0
    draft_patches: int = 0
    specialists_started: list[str] = field(default_factory=list)
    # The open observation form or HIRA entry editor, if any ("OBSERVATION" /
    # "HIRA_ENTRY"), with the browser's snapshot of it.
    form_kind: str | None = None
    form_draft: dict[str, Any] | None = None
    form_patches: int = 0


# ─── Specialist registry ────────────────────────────────────────────────

SPECIALISTS: dict[str, dict[str, Any]] = {
    "RCA_ASSISTANT": {
        "label": "Incident RCA assistant",
        "modules": ["INCIDENT"],
        "does": "Root-cause analysis draft for an incident (causes, contributing factors, evidence).",
    },
    "PERMIT_RISK_REVIEWER": {
        "label": "Permit risk reviewer",
        "modules": ["PTW"],
        "does": "Reviews a SUBMITTED permit for SIMOPS conflicts, control gaps, crew competency and past-incident patterns.",
    },
    "TRIAGE_AGENT": {
        "label": "Triage agent",
        "modules": ["OBSERVATION", "NEAR_MISS"],
        "does": "Classifies severity / category and suggests routing for an observation or near miss.",
    },
    "HIRA_ASSISTANT": {
        "label": "HIRA assistant",
        "modules": ["HIRA"],
        "does": "Hazard identification and risk assessment suggestions for one HIRA entry (activity).",
    },
    "CAPA_ASSISTANT": {
        "label": "CAPA assistant",
        "modules": ["CAPA"],
        "does": "Corrective / preventive action quality review and suggestions for a CAPA.",
    },
}

_MODULE_HREF = {
    "PTW": "/ptw",
    "INCIDENT": "/incidents",
    "OBSERVATION": "/observations",
    "NEAR_MISS": "/near-miss",
    "CAPA": "/capa",
}


async def _record_href(db: AsyncSession, module: str, record_id: str) -> str | None:
    if module in _MODULE_HREF:
        return f"{_MODULE_HREF[module]}/{record_id}"
    if module == "HIRA":
        from app.models.hira import HiraEntry

        entry = await db.get(HiraEntry, record_id)
        return f"/hira/{entry.studyId}/entries/{record_id}" if entry else None
    return None


# ─── Tool definitions ───────────────────────────────────────────────────


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


_ID = {"type": "string", "maxLength": 64}
_NULLABLE_STR = {"type": ["string", "null"]}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "find_records",
        "Look up the user's records by number or text. Returns only records the user can open. "
        "Use it to resolve a record the user names (e.g. 'INC-2026-NW-0045', 'the hot work permit at the kiln') "
        "before running a specialist or linking to it.",
        {
            "module": {"type": "string", "enum": ["PTW", "INCIDENT", "OBSERVATION", "NEAR_MISS", "CAPA"]},
            "query": {"type": "string", "minLength": 2, "maxLength": 120, "description": "Record number (or part) or words from the description."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        ["module", "query"],
    ),
    _tool(
        "run_specialist_agent",
        "Start one of SafeOps360's specialist agents on a record. It runs in the background (about 30-90 s) and "
        "its result appears on the record page and as a card in this chat. Only start one when the user asks for "
        "that analysis. Specialists: "
        + "; ".join(f"{code} — {s['does']} (modules: {', '.join(s['modules'])})" for code, s in SPECIALISTS.items()),
        {
            "agentCode": {"type": "string", "enum": list(SPECIALISTS)},
            "module": {"type": "string", "enum": ["INCIDENT", "PTW", "OBSERVATION", "NEAR_MISS", "HIRA", "CAPA"]},
            "recordId": {**_ID, "description": "Record id from pageContext or find_records (not the record number)."},
        },
        ["agentCode", "module", "recordId"],
    ),
    _tool(
        "get_specialist_result",
        "Fetch the status and result of a specialist run started earlier (by invocationId). Use when the user "
        "asks what the analysis found. Do not call it repeatedly to wait.",
        {"invocationId": _ID},
        ["invocationId"],
    ),
    _tool(
        "suggest_navigation",
        "Show the user a button that opens a SafeOps360 page (it does not navigate by itself). "
        "Use '/ptw/new' for the New Permit form, '/observations/new' for the New Observation form, "
        "or a record href from find_records.",
        {
            "path": {"type": "string", "pattern": "^/[A-Za-z0-9/_\\-?=&.%]*$", "maxLength": 200},
            "label": {"type": "string", "maxLength": 60},
        },
        ["path", "label"],
    ),
    _tool(
        "get_ptw_reference",
        "Permit-to-work rules and vocabularies: permit types, hazard types, PPE codes, crew roles, isolation types, "
        "and — for a given type plus extra hazards — the validity cap, mandatory controls (gas test / fire watch / "
        "standby / rescue plan), required competencies, default PPE and default gas-test parameters. "
        "Set includeChecklist to see the precaution checklist items (to explain them; you never answer them).",
        {
            "permitType": {"type": "string", "enum": list(ptw_draft.PERMIT_TYPE_LABELS)},
            "extraHazards": {"type": "array", "items": {"type": "string"}, "maxItems": 9},
            "includeChecklist": {"type": "boolean"},
        },
    ),
    _tool(
        "get_plant_options",
        "Areas, departments, equipment (with codes) and contractor companies for a plant — the ids the permit "
        "form needs. Defaults to the draft's plant. Also lists the plants the user can raise permits at.",
        {"plantId": _ID, "equipmentSearch": {"type": "string", "maxLength": 60, "description": "Optional filter on equipment name/code/location."}},
    ),
    _tool(
        "find_people",
        "Find people at a plant for permit roles. Returns user ids with designation and department only — names "
        "are never returned; the form shows them to the user. Pass the name the user typed as query. "
        "role=PERMIT_ISSUER lists designated issuers.",
        {
            "plantId": _ID,
            "query": {"type": "string", "maxLength": 60},
            "role": {"type": "string", "enum": ["PERMIT_ISSUER", "ANY"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 15},
        },
    ),
    _tool(
        "update_ptw_draft",
        "Fill or change fields on the New Permit form the user has open. The form updates live. Send only the "
        "fields to change; null clears a field; lists (workCrew, isolations, requiredPpe, toolsEquipment, "
        "subjectEquipment, extraHazards) replace the whole list. Changing type resets PPE and gas test to that "
        "type's defaults unless you also send them. Ids must come from get_plant_options / find_people. "
        "Isolations you propose but the user has not tagged get isolationPointTag 'TBC'. "
        "checklistSuggestions pre-fills precaution answers (YES, or NA with a reason) using item ids from "
        "get_ptw_reference(includeChecklist=true); the user must verify them on site and confirm before submitting. "
        "Returns the updated rule check. You cannot submit.",
        {
            "type": {"type": "string", "enum": list(ptw_draft.PERMIT_TYPE_LABELS)},
            "extraHazards": {"type": "array", "items": {"type": "string"}, "maxItems": 9,
                             "description": "Hazard annexures beyond the base type (e.g. HOT_WORK on a WORK_AT_HEIGHT permit)."},
            "validFrom": {**_NULLABLE_STR, "description": "Local 'YYYY-MM-DDTHH:MM'"},
            "validTo": {**_NULLABLE_STR, "description": "Local 'YYYY-MM-DDTHH:MM'"},
            "plantId": _NULLABLE_STR,
            "departmentId": _NULLABLE_STR,
            "areaId": _NULLABLE_STR,
            "specificLocation": _NULLABLE_STR,
            "scopeOfWork": {**_NULLABLE_STR, "description": "Clear job description: what, where, how, which equipment."},
            "workOrderNumber": _NULLABLE_STR,
            "contractorCompanyId": _NULLABLE_STR,
            "issuerId": _NULLABLE_STR,
            "receiverId": _NULLABLE_STR,
            "workCrew": {"type": "array", "items": {"type": "object", "properties": {
                "userId": {"type": "string"}, "role": {"type": "string"}}, "required": ["userId"]}},
            "fireWatchPersonId": _NULLABLE_STR,
            "standbyPersonId": _NULLABLE_STR,
            "isolations": {"type": "array", "items": {"type": "object", "properties": {
                "isolationType": {"type": "string"}, "description": {"type": "string"},
                "isolationPointTag": {"type": "string"}, "lotoTagNumber": _NULLABLE_STR},
                "required": ["isolationType", "description", "isolationPointTag"]}},
            "requiredPpe": {"type": "array", "items": {"type": "string"}},
            "toolsEquipment": {"type": "array", "items": {"type": "object", "properties": {
                "equipmentId": _NULLABLE_STR, "freeTextDescription": _NULLABLE_STR}}},
            "subjectEquipment": {"type": "array", "items": {"type": "object", "properties": {
                "equipmentId": {"type": "string"}, "workNature": {"type": "string"}}, "required": ["equipmentId"]}},
            "gasTestPlan": {"type": ["object", "null"], "properties": {
                "refreshFrequencyMinutes": {"type": "integer"},
                "parametersToTest": {"type": "array", "items": {"type": "object", "properties": {
                    "parameter": {"type": "string"}, "lowLimit": {"type": ["number", "null"]},
                    "highLimit": {"type": ["number", "null"]}, "unit": {"type": "string"}},
                    "required": ["parameter", "unit"]}},
                "instrumentSerial": _NULLABLE_STR,
                "instrumentLastCalibrated": {**_NULLABLE_STR, "description": "YYYY-MM-DD"}}},
            "rescuePlan": _NULLABLE_STR,
            "weatherConditionsAtIssue": _NULLABLE_STR,
            "windSpeedKmh": {"type": ["number", "null"]},
            "flraRequired": {"type": ["boolean", "null"], "description": "null = follow site policy"},
            "checklistSuggestions": {
                "type": "object",
                "description": "Hazard type -> suggested answers. Replaces earlier suggestions for that hazard.",
                "additionalProperties": {"type": "array", "items": {"type": "object", "properties": {
                    "itemId": {"type": "string"},
                    "response": {"type": "string", "enum": ["YES", "NA"]},
                    "remark": _NULLABLE_STR}, "required": ["itemId", "response"]}},
            },
        },
    ),
    _tool(
        "find_past_permits",
        "Past permits at a plant for lessons learned: closed, expired, cancelled or suspended permits of a type "
        "(and optionally an area or words in the scope), with how each ended — outcome, closing and hand-back "
        "remarks, suspensions with reasons, extensions, and incidents or near misses raised while it was live. "
        "Defaults to the open draft's plant and type. Use it for 'what went well / what went wrong last time'.",
        {
            "permitType": {"type": "string", "enum": list(ptw_draft.PERMIT_TYPE_LABELS)},
            "plantId": _ID,
            "areaId": _ID,
            "query": {"type": "string", "maxLength": 80, "description": "Optional words to match in scope or location."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 15},
        },
    ),
    _tool(
        "check_ptw_draft",
        "Check the open permit draft against the submission rules: what still blocks each step, required "
        "controls, validity cap, crew competency gaps and advisory warnings.",
        {},
    ),
    _tool(
        "ptw_risk_context",
        "Site risk context for the open permit draft: overlapping live permits at the plant (SIMOPS), recent "
        "high-severity observations in the area, past closed incidents on the same permit type, and HIRA "
        "high-risk entries for the area. Use before advising on controls.",
        {},
    ),
    _tool(
        "suggest_observation_fields",
        "Suggest how to record a safety observation from its description, using the reviewed reference register of "
        "a stainless steel plant (thousands of real, reviewed observations): plant category, risk level, fatal-"
        "potential signals, responsible function, closure target, corrective actions from similar cases, and the "
        "matching SafeOps STOP category/sub-category with the severity the SafeOps matrix will suggest. Defaults to "
        "the open observation form's description and type.",
        {
            "description": {"type": "string", "maxLength": 2000, "description": "What was observed. Defaults to the form's description."},
            "observationType": {"type": "string", "enum": list(form_drafts.OBS_TYPES)},
        },
    ),
    _tool(
        "update_observation_draft",
        "Fill or change fields on the New Observation form the user has open; the form updates live and the user "
        "submits. Send only fields to change. categoryCode and subCategoryCode go together and must be a STOP pair "
        "from suggest_observation_fields (stopCandidates) for the observation type. If severity differs from the "
        "SafeOps matrix suggestion, give severityReason (at least 10 characters, the user can edit it). Only rewrite "
        "description or immediateAction when the user asks you to.",
        {
            "type": {"type": "string", "enum": list(form_drafts.OBS_TYPES)},
            "areaId": {"type": "string", "maxLength": 64, "description": "An areaId from the draft's areas list."},
            "categoryCode": {"type": "string", "maxLength": 64},
            "subCategoryCode": {"type": "string", "maxLength": 64},
            "severity": {"type": "string", "enum": list(form_drafts.SEVERITIES)},
            "severityReason": {"type": "string", "maxLength": 500},
            "description": {"type": "string", "maxLength": 4000},
            "immediateAction": {"type": "string", "maxLength": 2000},
        },
    ),
    _tool(
        "search_hira_library",
        "Search the reviewed reference HIRA of a steel melt shop (charging, EAF, AOD, LRF, VD, ladle and alloy feeding "
        "areas; ~330 activity-hazard rows with consequences, existing controls by hierarchy, PPE, the plant's "
        "SP x LH x CN x DE scores, legal/domino/chronic/emergency concerns and legal references). Also returns "
        "good-practice higher-hierarchy control ideas for the hazard types found, and matching SafeOps hazard-"
        "master entries. Defaults to the open HIRA entry's activity.",
        {
            "query": {"type": "string", "maxLength": 300, "description": "Activity / hazard words. Defaults to the open entry's activity."},
            "hazardType": {"type": "string", "enum": sorted(hira_kb.MASTER_CATEGORIES)},
            "section": {"type": "string", "maxLength": 80, "description": "Plant area filter, e.g. 'AOD', 'Ladle'."},
            "significantOnly": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        },
    ),
    _tool(
        "assess_hira_controls",
        "What-if for the open HIRA entry: how the residual risk changes if controls are added, removed or their "
        "effectiveness changes, using the same model and matrix as the HIRA editor. Returns initial, residual "
        "before and after (likelihood x severity, level, ALARP region, verdict), each control's credit, plain "
        "explanations, and which single extra control (by hierarchy level) would make it acceptable. Set "
        "includeRecommended to forecast the risk once the recommended controls are in place. Optionally rate "
        "with the plant's own method (plantMethod: SP, LH, CN, DE and concerns).",
        {
            "addControls": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
                "hierarchy": {"type": "string", "enum": list(form_drafts.HIERARCHY)},
                "description": {"type": "string", "maxLength": 300},
                "effectiveness": {"type": ["string", "null"], "enum": [*form_drafts.EFFECTIVENESS, None]}},
                "required": ["hierarchy"]}},
            "removeControlKeys": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 64}},
            "setEffectiveness": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {
                "key": {"type": "string", "maxLength": 64},
                "effectiveness": {"type": "string", "enum": list(form_drafts.EFFECTIVENESS)}},
                "required": ["key", "effectiveness"]}},
            "includeRecommended": {"type": "boolean"},
            "initialLikelihood": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Only when no entry is open."},
            "initialSeverity": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Only when no entry is open."},
            "routine": {"type": "string", "enum": ["ROUTINE", "NON_ROUTINE", "EMERGENCY"]},
            "plantMethod": {"type": "object", "properties": {
                "SP": {"type": "integer", "minimum": 1, "maximum": 5}, "LH": {"type": "integer", "minimum": 1, "maximum": 5},
                "CN": {"type": "integer", "minimum": 1, "maximum": 5}, "DE": {"type": "integer", "minimum": 1, "maximum": 5},
                "concerns": {"type": "object", "properties": {
                    "legal": {"type": "boolean"}, "domino": {"type": "boolean"},
                    "chronic": {"type": "boolean"}, "emergency": {"type": "boolean"}}},
                "after": {"type": "object", "properties": {
                    "SP": {"type": "integer", "minimum": 1, "maximum": 5}, "LH": {"type": "integer", "minimum": 1, "maximum": 5},
                    "CN": {"type": "integer", "minimum": 1, "maximum": 5}, "DE": {"type": "integer", "minimum": 1, "maximum": 5}}}},
                "required": ["SP", "LH", "CN", "DE"]},
        },
    ),
    _tool(
        "update_hira_entry_draft",
        "Add suggestions to the HIRA entry editor the user has open; the editor updates live and the user reviews "
        "and saves. addRecommendedControls: proposed further controls (with rationale). addExistingControls: only "
        "controls the user has told you are already in place. setControlEffectiveness: only when the user tells you "
        "how effective a control is (keys from the draft). target: the forecast residual once the recommended "
        "controls are in place (use assess_hira_controls with includeRecommended first). Returns the new what-if.",
        {
            "addExistingControls": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
                "hierarchy": {"type": "string", "enum": list(form_drafts.HIERARCHY)},
                "description": {"type": "string", "maxLength": 500},
                "effectiveness": {"type": ["string", "null"], "enum": [*form_drafts.EFFECTIVENESS, None]}},
                "required": ["hierarchy", "description"]}},
            "addRecommendedControls": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
                "hierarchy": {"type": "string", "enum": list(form_drafts.HIERARCHY)},
                "description": {"type": "string", "maxLength": 500},
                "rationale": {"type": "string", "maxLength": 500},
                "estimatedCostBand": {"type": "string", "enum": list(form_drafts.COST_BANDS)}},
                "required": ["hierarchy", "description"]}},
            "setControlEffectiveness": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {
                "key": {"type": "string", "maxLength": 64},
                "effectiveness": {"type": "string", "enum": list(form_drafts.EFFECTIVENESS)}},
                "required": ["key", "effectiveness"]}},
            "target": {"type": "object", "properties": {
                "likelihoodScore": {"type": "integer", "minimum": 1, "maximum": 10},
                "severityScore": {"type": "integer", "minimum": 1, "maximum": 10},
                "rationale": {"type": "string", "maxLength": 600}},
                "required": ["likelihoodScore", "severityScore"]},
        },
    ),
]

TOOL_NAMES = [t["name"] for t in TOOL_DEFINITIONS]

# Human labels for the activity chips in the chat panel.
TOOL_LABELS = {
    "find_records": "Searching records",
    "run_specialist_agent": "Starting specialist agent",
    "get_specialist_result": "Reading specialist result",
    "suggest_navigation": "Preparing link",
    "get_ptw_reference": "Reading permit rules",
    "get_plant_options": "Loading plant masters",
    "find_people": "Finding people",
    "update_ptw_draft": "Updating permit form",
    "check_ptw_draft": "Checking permit draft",
    "ptw_risk_context": "Checking site risk context",
    "find_past_permits": "Reviewing past permits",
    "suggest_observation_fields": "Checking similar observations",
    "update_observation_draft": "Updating observation form",
    "search_hira_library": "Searching HIRA library",
    "assess_hira_controls": "Calculating risk change",
    "update_hira_entry_draft": "Updating HIRA entry",
}


class ToolError(Exception):
    """A tool failure the model should see and recover from."""


# ─── Dispatcher ─────────────────────────────────────────────────────────


async def dispatch(name: str, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool {name!r}")
    if not isinstance(args, dict):
        raise ToolError("Tool input must be a JSON object")
    async with AsyncSessionLocal() as db:
        with tool_scope(ctx.user_id):
            try:
                return await handler(db, args, ctx)
            except HTTPException as e:
                await db.rollback()
                raise ToolError(str(e.detail)) from e
            except ToolError:
                await db.rollback()
                raise


async def _load_user(db: AsyncSession, ctx: TurnContext) -> User:
    user = await db.get(User, ctx.user_id)
    if user is None:
        raise ToolError("User not found")
    return user


def _clip(v: Any, n: int = 240) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _enum(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


# ─── find_records ───────────────────────────────────────────────────────


async def _find_records(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.models.capa import Capa
    from app.models.incident import Incident
    from app.models.near_miss import NearMiss
    from app.models.observation import Observation
    from app.models.permit import Permit
    from app.services.agents.source_access import check_source_access

    module = str(args.get("module") or "")
    query = str(args.get("query") or "").strip()
    limit = max(1, min(int(args.get("limit") or 6), 10))
    if len(query) < 2:
        raise ToolError("query must be at least 2 characters")
    like = f"%{query}%"

    specs: dict[str, tuple[Any, str, list[Any], Any, Any]] = {
        # model, read perm, searchable columns, plant column, order column
        "PTW": (Permit, "PTW.READ", [Permit.number, Permit.scopeOfWork, Permit.location], Permit.plantId, Permit.createdAt),
        "INCIDENT": (Incident, "INCIDENT.READ", [Incident.number, Incident.description], Incident.plantId, Incident.date),
        "OBSERVATION": (Observation, "OBSERVATION.READ", [Observation.number, Observation.description], Observation.plantId, Observation.date),
        "NEAR_MISS": (NearMiss, "NEAR_MISS.READ", [NearMiss.number, NearMiss.description], NearMiss.plantId, NearMiss.date),
        "CAPA": (Capa, "CAPA.READ", [Capa.capaNumber, Capa.aliasNumber, Capa.title], Capa.plantId, Capa.detectedAt),
    }
    if module not in specs:
        raise ToolError(f"Unsupported module {module!r}")
    model, perm, cols, plant_col, order_col = specs[module]
    stmt = select(model).where(or_(*[c.ilike(like) for c in cols]))
    if hasattr(model, "isDeleted"):
        stmt = stmt.where(model.isDeleted.is_(False))
    stmt = restrict_to_plants(stmt, plant_col, await readable_plants(db, perm))
    rows = (await db.execute(stmt.order_by(order_col.desc()).limit(limit * 3))).scalars().all()

    user = await _load_user(db, ctx)
    out: list[dict[str, Any]] = []
    for r in rows:
        allowed, _ = await check_source_access(db, user, module, r.id)
        if not allowed:
            continue
        item: dict[str, Any] = {
            "recordId": r.id,
            "number": getattr(r, "number", None) or getattr(r, "capaNumber", None),
            "status": _enum(getattr(r, "status", None)),
            "href": await _record_href(db, module, r.id),
        }
        if module == "PTW":
            item |= {"type": _enum(r.type), "location": r.location, "scope": _clip(r.scopeOfWork),
                     "validFrom": _iso(r.validFrom), "validTo": _iso(r.validTo)}
        elif module == "CAPA":
            item |= {"title": _clip(r.title, 160), "severity": r.severity}
        else:
            item |= {"date": _iso(getattr(r, "date", None)), "description": _clip(r.description)}
            if module == "OBSERVATION":
                item["severity"] = _enum(r.severity)
        out.append(item)
        if len(out) >= limit:
            break
    return {"module": module, "query": query, "count": len(out), "records": out}


# ─── Specialists ────────────────────────────────────────────────────────


async def _run_specialist_background(invocation_id: str) -> None:
    from app.services.agents import agent_service

    async with AsyncSessionLocal() as session:
        try:
            await agent_service.run_invocation(db=session, invocation_id=invocation_id)
        except Exception:  # noqa: BLE001 — run_invocation lands the row in ERRORED itself
            pass


async def _run_specialist(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.services.agents import agent_service
    from app.services.agents.source_access import assert_source_access
    from app.services.permissions import PermissionContext, can

    code = str(args.get("agentCode") or "")
    module = str(args.get("module") or "")
    record_id = str(args.get("recordId") or "")
    spec = SPECIALISTS.get(code)
    if spec is None:
        raise ToolError(f"Unknown specialist {code!r}")
    if module not in spec["modules"]:
        raise ToolError(f"{code} works on {', '.join(spec['modules'])} records, not {module}")
    if code in ctx.specialists_started:
        raise ToolError(f"{code} was already started in this turn")

    user = await _load_user(db, ctx)
    permission_code = agent_service.get_invoke_permission_code(code)
    await assert_source_access(db, user, module, record_id)
    plant_id = await agent_service._resolve_source_plant_id(db, module, record_id)
    check = await can(db, user.id, permission_code, PermissionContext(plant_id=plant_id))
    if not check.allowed:
        raise ToolError(check.reason or f"You do not hold {permission_code} for this record")

    inv = await agent_service.start_invocation(
        db=db, agent_code=code, source_module=module, source_record_id=record_id, user_id=user.id,
    )
    task = asyncio.create_task(_run_specialist_background(inv.id))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    ctx.specialists_started.append(code)

    href = await _record_href(db, module, record_id)
    await ctx.emit("action", {
        "type": "specialist",
        "invocationId": inv.id,
        "invocationNumber": inv.invocationNumber,
        "agentCode": code,
        "label": spec["label"],
        "module": module,
        "recordId": record_id,
        "href": href,
    })
    return {
        "started": True,
        "invocationId": inv.id,
        "invocationNumber": inv.invocationNumber,
        "agent": spec["label"],
        "href": href,
        "note": "Running in the background; the chat shows a live card and the result also appears on the record page.",
    }


async def _get_specialist_result(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.models.agent import Agent, AgentInvocation
    from app.routers.agents import _authorise_view

    inv = await db.get(AgentInvocation, str(args.get("invocationId") or ""))
    if inv is None or inv.sourceModule == "COPILOT":
        raise ToolError("No specialist run with that id")
    await _authorise_view(db, await _load_user(db, ctx), inv)
    agent = await db.get(Agent, inv.agentId)
    suggestion = inv.agentSuggestion
    text = json.dumps(suggestion, default=str) if suggestion is not None else None
    if text and len(text) > 8000:
        text = text[:8000] + "…(truncated)"
    return {
        "invocationNumber": inv.invocationNumber,
        "agent": agent.code if agent else None,
        "status": inv.status,
        "summaryReasoning": _clip(inv.agentReasoning, 1500),
        "suggestion": json.loads(text) if text and not text.endswith("(truncated)") else text,
        "confidence": inv.agentConfidence,
        "error": _clip(inv.errorDetails, 400) if inv.hadError else None,
        "href": await _record_href(db, inv.sourceModule, inv.sourceRecordId),
    }


# ─── Navigation ─────────────────────────────────────────────────────────

_SAFE_PATH = re.compile(r"^/(?!/)[A-Za-z0-9/_\-?=&.%]*$")


async def _suggest_navigation(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    path = str(args.get("path") or "")
    label = _clip(args.get("label") or "Open", 60) or "Open"
    if not _SAFE_PATH.match(path) or ".." in path:
        raise ToolError("path must be an in-app path like /ptw/new")
    await ctx.emit("action", {"type": "navigate", "path": path, "label": label})
    return {"shown": True, "path": path}


# ─── PTW reference / masters / people ───────────────────────────────────


async def _get_ptw_reference(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    ptype = args.get("permitType") or (ctx.draft or {}).get("type")
    extra = args.get("extraHazards")
    if extra is None:
        extra = (ctx.draft or {}).get("extraHazards") or []
    ref = ptw_draft.reference_for(ptype, [str(h) for h in extra])
    if args.get("includeChecklist") and ref.get("forThisPermit"):
        from app.models.permit import PermitHazardType
        from app.services.ptw_annexures import load_catalog

        hazards = [PermitHazardType(h) for h in ref["forThisPermit"]["effectiveHazards"]]
        catalog = await load_catalog(db, hazards)
        ref["checklist"] = {
            h.value: [
                {"itemId": i.id, "text": _clip(i.text, 220), "mandatory": i.isMandatory, "naAllowed": i.allowsNA}
                for i in items[:40]
            ]
            for h, items in catalog.items()
        }
    return ref


async def _accessible_permit_plants(db: AsyncSession, ctx: TurnContext) -> list[str] | None:
    from app.services.permissions import get_accessible_plants_for

    return await get_accessible_plants_for(db, ctx.user_id, "PTW.CREATE")


async def _get_plant_options(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.models.epc import ContractorCompany
    from app.models.equipment import Equipment
    from app.models.masters import Department
    from app.models.plant import Area, Plant

    accessible = await _accessible_permit_plants(db, ctx)
    plant_stmt = select(Plant).order_by(Plant.name)
    if accessible is not None:
        plant_stmt = plant_stmt.where(Plant.id.in_(accessible))
    plants = (await db.execute(plant_stmt.limit(40))).scalars().all()
    plant_list = [{"plantId": p.id, "code": p.code, "name": p.name} for p in plants]

    plant_id = args.get("plantId") or (ctx.draft or {}).get("plantId") or ctx.user_plant_id
    if not plant_id:
        return {"plants": plant_list, "note": "No plant chosen — pick one of these."}
    if accessible is not None and plant_id not in accessible:
        raise ToolError("You cannot raise permits at that plant")
    plant = await db.get(Plant, plant_id)
    if plant is None:
        raise ToolError("Plant not found")

    areas = (await db.execute(select(Area).where(Area.plantId == plant_id).order_by(Area.name))).scalars().all()
    deps = (
        await db.execute(
            select(Department).where(Department.plantId == plant_id, Department.active.is_(True)).order_by(Department.name)
        )
    ).scalars().all()
    eq_stmt = select(Equipment).where(Equipment.plantId == plant_id, Equipment.active.is_(True))
    if q := str(args.get("equipmentSearch") or "").strip():
        like = f"%{q}%"
        eq_stmt = eq_stmt.where(or_(Equipment.name.ilike(like), Equipment.code.ilike(like), Equipment.location.ilike(like)))
    equipment = (await db.execute(eq_stmt.order_by(Equipment.name).limit(150))).scalars().all()
    contractors = (
        await db.execute(
            select(ContractorCompany).where(ContractorCompany.status == "ACTIVE").order_by(ContractorCompany.name).limit(100)
        )
    ).scalars().all()
    return {
        "plant": {"plantId": plant.id, "code": plant.code, "name": plant.name},
        "areas": [{"areaId": a.id, "name": a.name} for a in areas],
        "departments": [{"departmentId": d.id, "name": d.name} for d in deps],
        "equipment": [
            {"equipmentId": e.id, "code": e.code, "name": e.name, "category": e.category, "location": e.location}
            for e in equipment
        ],
        "equipmentTruncated": len(equipment) >= 150,
        "contractors": [{"contractorCompanyId": c.id, "name": c.name} for c in contractors],
        "otherPlants": [p for p in plant_list if p["plantId"] != plant_id][:20],
    }


async def _find_people(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.models.user import Role, UserRole

    plant_id = args.get("plantId") or (ctx.draft or {}).get("plantId") or ctx.user_plant_id
    if not plant_id:
        raise ToolError("plantId required (no plant on the draft yet)")
    accessible = await _accessible_permit_plants(db, ctx)
    if accessible is not None and plant_id not in accessible:
        raise ToolError("You cannot raise permits at that plant")
    limit = max(1, min(int(args.get("limit") or 8), 15))

    issuer_ids = select(UserRole.userId).join(Role, Role.id == UserRole.roleId).where(Role.code == "PERMIT_ISSUER")
    stmt = select(User).where(User.plantId == plant_id)
    if hasattr(User, "rosterStatus"):
        stmt = stmt.where(User.rosterStatus == "active")
    if q := str(args.get("query") or "").strip():
        like = f"%{q}%"
        stmt = stmt.where(or_(User.name.ilike(like), User.designation.ilike(like), User.department.ilike(like)))
    role_filtered = args.get("role") == "PERMIT_ISSUER"
    if role_filtered:
        stmt_role = stmt.where(or_(User.id.in_(issuer_ids), User.role == "PERMIT_ISSUER"))
        rows = (await db.execute(stmt_role.order_by(User.name).limit(limit))).scalars().all()
        fallback = not rows
        if fallback:
            rows = (await db.execute(stmt.order_by(User.name).limit(limit))).scalars().all()
    else:
        fallback = False
        rows = (await db.execute(stmt.order_by(User.name).limit(limit))).scalars().all()

    issuers = set(
        (await db.execute(issuer_ids.where(UserRole.userId.in_([u.id for u in rows])))).scalars().all()
    ) if rows else set()
    people = [
        {
            "userId": u.id,
            "designation": u.designation,
            "department": u.department,
            "isPermitIssuer": u.id in issuers or u.role == "PERMIT_ISSUER",
            "isYou": u.id == ctx.user_id,
        }
        for u in rows
    ]
    await ctx.emit("people", {"userIds": [p["userId"] for p in people]})
    return {
        "plantId": plant_id,
        "count": len(people),
        "people": people,
        "note": (
            "No designated permit issuers at this plant; showing all plant users." if fallback else None
        ),
    }


# ─── Draft tools ────────────────────────────────────────────────────────


def _require_draft(ctx: TurnContext) -> dict[str, Any]:
    if ctx.draft is None:
        raise ToolError(
            "The New Permit form is not open. Offer the user a link to /ptw/new with suggest_navigation; "
            "drafting continues there."
        )
    return ctx.draft


async def _update_ptw_draft(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    draft = _require_draft(ctx)
    if ctx.draft_patches >= 12:
        raise ToolError("Too many form updates in one turn — summarise and let the user review.")
    patch, err = ptw_draft.parse_patch(args)
    if err:
        raise ToolError(err)
    assert patch is not None
    merged = ptw_draft.apply_patch(draft, patch)
    problems = await ptw_draft.validate_references(db, merged, patch, user_id=ctx.user_id)
    if patch.get("checklistSuggestions"):
        errs, summary = await ptw_draft.validate_checklist_suggestions(db, merged, patch["checklistSuggestions"])
        problems += errs
        merged["checklist"] = {**(merged.get("checklist") or {}), **summary}
    if problems:
        raise ToolError("Nothing was changed: " + "; ".join(problems))
    ctx.draft = merged
    ctx.draft_patches += 1
    report = await ptw_draft.check_draft(db, merged, user_id=ctx.user_id, now_local=ctx.now_local)
    await ctx.emit("draft_patch", {"patch": patch, "check": report})
    return {"applied": sorted(patch), "check": report}


async def _check_ptw_draft(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    draft = _require_draft(ctx)
    report = await ptw_draft.check_draft(db, draft, user_id=ctx.user_id, now_local=ctx.now_local)
    await ctx.emit("draft_check", {"check": report})
    return report


def _local_to_utc(v: Any, offset_minutes: int) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        naive = datetime.strptime(v[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    # JS getTimezoneOffset(): minutes to ADD to local time to get UTC.
    return (naive + timedelta(minutes=offset_minutes)).replace(tzinfo=timezone.utc)


async def _ptw_risk_context(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.models.incident import Incident, IncidentStatus
    from app.models.observation import Observation, Severity
    from app.models.permit import Permit, PermitStatus

    draft = _require_draft(ctx)
    plant_id = draft.get("plantId")
    if not plant_id:
        raise ToolError("Set the plant on the draft first")
    area_id = draft.get("areaId")
    ptype = draft.get("type")
    now = datetime.now(timezone.utc)
    start = _local_to_utc(draft.get("validFrom"), ctx.tz_offset_minutes) or now
    end = _local_to_utc(draft.get("validTo"), ctx.tz_offset_minutes) or start + timedelta(hours=8)
    out: dict[str, Any] = {"plantId": plant_id, "areaId": area_id, "window": [start.isoformat(), end.isoformat()]}

    live = [PermitStatus.SUBMITTED, PermitStatus.APPROVED, PermitStatus.ISSUED, PermitStatus.ACTIVE, PermitStatus.SUSPENDED]
    p_stmt = (
        select(Permit)
        .where(Permit.plantId == plant_id, Permit.status.in_(live), Permit.validFrom < end, Permit.validTo > start)
        .where(Permit.isDeleted.is_(False))
    )
    p_stmt = restrict_to_plants(p_stmt, Permit.plantId, await readable_plants(db, "PTW.READ"))
    permits = (await db.execute(p_stmt.order_by(Permit.validFrom).limit(15))).scalars().all()
    out["overlappingPermits"] = [
        {"number": p.number, "type": _enum(p.type), "status": _enum(p.status), "sameArea": bool(area_id and p.areaId == area_id),
         "location": p.location, "scope": _clip(p.scopeOfWork, 160), "validFrom": _iso(p.validFrom), "validTo": _iso(p.validTo)}
        for p in permits
    ]

    o_stmt = select(Observation).where(
        Observation.plantId == plant_id,
        Observation.date >= now - timedelta(days=60),
        Observation.severity.in_([Severity.HIGH, Severity.CRITICAL]),
    )
    if area_id:
        o_stmt = o_stmt.where(Observation.areaId == area_id)
    o_stmt = restrict_to_plants(o_stmt, Observation.plantId, await readable_plants(db, "OBSERVATION.READ"))
    obs = (await db.execute(o_stmt.order_by(Observation.date.desc()).limit(8))).scalars().all()
    out["recentHighSeverityObservations"] = [
        {"number": o.number, "date": _iso(o.date), "severity": _enum(o.severity), "category": _enum(o.category),
         "status": _enum(o.status), "description": _clip(o.description, 200)}
        for o in obs
    ]

    if ptype:
        i_stmt = (
            select(Incident)
            .join(Permit, Permit.id == Incident.activePermitId)
            .where(Incident.plantId == plant_id, Incident.status == IncidentStatus.CLOSED,
                   Incident.date >= now - timedelta(days=730), Permit.type == ptype)
        )
        i_stmt = restrict_to_plants(i_stmt, Incident.plantId, await readable_plants(db, "INCIDENT.READ"))
        incs = (await db.execute(i_stmt.order_by(Incident.date.desc()).limit(6))).scalars().all()
        out["pastIncidentsSamePermitType"] = [
            {"number": i.number, "date": _iso(i.date), "severity": i.severity,
             "description": _clip(i.initialDescription or i.description, 200),
             "rootCause": _clip(i.rootCauseSummary, 240)}
            for i in incs
        ]

    try:
        from app.routers.hira import for_ptw

        hira = await for_ptw(plant_id=plant_id, area_id=area_id, permit_type=ptype, user=await _load_user(db, ctx), db=db)
        out["hira"] = {
            "advisory": hira.advisory,
            "criticalCount": hira.gatingBlockers,
            "highCount": hira.highCount,
            "entries": [
                {"activity": _clip(getattr(e, "activityDescription", None), 160), "residualRisk": getattr(e, "residualRiskLevel", None)}
                for e in (hira.entries or [])[:6]
            ],
        }
    except HTTPException:
        out["hira"] = {"note": "No HIRA access at this plant"}
    except Exception:  # noqa: BLE001 — context is advisory
        out["hira"] = {"note": "HIRA lookup unavailable"}
    return out


async def _find_past_permits(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from sqlalchemy import func as sa_func

    from app.models.incident import Incident
    from app.models.near_miss import NearMiss
    from app.models.permit import Permit, PermitExtension, PermitStatus, PermitSuspension

    draft = ctx.draft or {}
    plant_id = args.get("plantId") or draft.get("plantId") or ctx.user_plant_id
    ptype = args.get("permitType") or draft.get("type")
    area_id = args.get("areaId")
    limit = max(1, min(int(args.get("limit") or 8), 15))
    finished = [PermitStatus.CLOSED, PermitStatus.EXPIRED, PermitStatus.CANCELLED, PermitStatus.WORK_COMPLETED,
                PermitStatus.HANDBACK_INSPECTION, PermitStatus.REJECTED]
    stmt = select(Permit).where(Permit.isDeleted.is_(False), Permit.status.in_(finished))
    if plant_id:
        stmt = stmt.where(Permit.plantId == plant_id)
    if ptype:
        stmt = stmt.where(Permit.type == ptype)
    if area_id:
        stmt = stmt.where(Permit.areaId == area_id)
    if q := str(args.get("query") or "").strip():
        like = f"%{q}%"
        stmt = stmt.where(or_(Permit.scopeOfWork.ilike(like), Permit.location.ilike(like)))
    stmt = restrict_to_plants(stmt, Permit.plantId, await readable_plants(db, "PTW.READ"))
    permits = (await db.execute(stmt.order_by(Permit.validFrom.desc()).limit(limit))).scalars().all()
    ids = [p.id for p in permits]
    susp: dict[str, list[Any]] = {}
    ext: dict[str, int] = {}
    incs: dict[str, list[str]] = {}
    if ids:
        for s in (await db.execute(select(PermitSuspension).where(PermitSuspension.permitId.in_(ids)))).scalars():
            susp.setdefault(s.permitId, []).append(s)
        for pid, n in (await db.execute(
            select(PermitExtension.permitId, sa_func.count()).where(PermitExtension.permitId.in_(ids)).group_by(PermitExtension.permitId)
        )).all():
            ext[pid] = n
        inc_plants = await readable_plants(db, "INCIDENT.READ")
        i_stmt = restrict_to_plants(select(Incident).where(Incident.activePermitId.in_(ids)), Incident.plantId, inc_plants)
        for i in (await db.execute(i_stmt)).scalars():
            incs.setdefault(i.activePermitId, []).append(f"Incident {i.number}: {_clip(i.initialDescription or i.description, 160)}")
        nm_plants = await readable_plants(db, "NEAR_MISS.READ")
        n_stmt = restrict_to_plants(select(NearMiss).where(NearMiss.activePermitId.in_(ids)), NearMiss.plantId, nm_plants)
        for n in (await db.execute(n_stmt)).scalars():
            incs.setdefault(n.activePermitId, []).append(f"Near miss {n.number}: {_clip(n.description, 160)}")

    out = []
    for p in permits:
        out.append({
            "number": p.number,
            "href": f"/ptw/{p.id}",
            "type": _enum(p.type),
            "status": _enum(p.status),
            "location": p.location,
            "scope": _clip(p.scopeOfWork, 200),
            "validFrom": _iso(p.validFrom),
            "outcome": _enum(getattr(p, "outcome", None)),
            "closureType": _enum(getattr(p, "closureType", None)),
            "closingRemark": _clip(getattr(p, "closingRemark", None), 240),
            "handbackNotes": _clip(getattr(p, "returnNotes", None), 240),
            "cancellationReason": _clip(getattr(p, "cancellationReason", None), 160),
            "rejectionReason": _clip(getattr(p, "rejectionReason", None), 160),
            "suspensions": [
                {"reason": s.reason, "detail": _clip(s.reasonDetail, 160), "resumed": s.resumedAt is not None,
                 "resumptionConditions": _clip(s.resumptionConditions, 160)}
                for s in susp.get(p.id, [])[:4]
            ],
            "extensions": ext.get(p.id, 0),
            "eventsDuringPermit": incs.get(p.id, [])[:4],
        })
    return {
        "plantId": plant_id,
        "permitType": ptype,
        "count": len(out),
        "permits": out,
        "note": None if out else "No finished permits of this type at this plant match.",
    }


# ─── Observation form ───────────────────────────────────────────────────


def _obs_draft(ctx: TurnContext, *, required: bool) -> dict[str, Any] | None:
    if ctx.form_kind == "OBSERVATION" and ctx.form_draft is not None:
        return ctx.form_draft
    if required:
        raise ToolError(
            "The New Observation form is not open. Offer the user a link to /observations/new with "
            "suggest_navigation; you can still suggest values in the chat."
        )
    return None


async def _live_stop_pairs(db: AsyncSession, axis: str) -> dict[tuple[str, str], dict[str, str]]:
    from app.models.observation import ObservationTaxonomy

    rows = (await db.execute(
        select(ObservationTaxonomy).where(ObservationTaxonomy.observationType == axis, ObservationTaxonomy.isActive.is_(True))
    )).scalars().all()
    return {(r.categoryCode, r.subCategoryCode): {"categoryLabel": r.categoryLabel, "subCategoryLabel": r.subCategoryLabel}
            for r in rows}


async def _suggest_observation_fields(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.services import observation_severity as sev

    draft = _obs_draft(ctx, required=False) or {}
    text = str(args.get("description") or draft.get("description") or "").strip()
    if len(text) < 8:
        raise ToolError("Give the observation description (what was seen, where).")
    otype = args.get("observationType") or draft.get("type")
    kb = obs_kb.suggest(text, observation_type=otype)
    if not kb.get("available"):
        return kb
    # The plant category decides act vs condition when the form type is unset.
    cat = (kb.get("category") or {}).get("code") or ""
    axis = "ACT" if "ACT" in str(otype or "").upper() else "CONDITION" if "CONDITION" in str(otype or "").upper() \
        else ("ACT" if cat.startswith("A") else "CONDITION")
    live = await _live_stop_pairs(db, axis)
    stop = []
    for c in kb.get("stopCandidates") or []:
        labels = live.get((c["categoryCode"], c["subCategoryCode"]))
        if labels:
            stop.append({**c, **labels})
    if not stop and live:
        stop = [{"categoryCode": k[0], "subCategoryCode": k[1], **v, "why": "no mapped pair is active; choose from the live list"}
                for k in list(live)[:12] for v in [live[k]]]
    kb["observationType"] = "UNSAFE_ACT" if axis == "ACT" else "UNSAFE_CONDITION"
    kb["stopCandidates"] = stop
    if stop:
        m = await sev.resolve(db, observation_type=axis, category=stop[0]["categoryCode"],
                              sub_category=stop[0]["subCategoryCode"], plant_id=draft.get("plantId"), area_id=draft.get("areaId"))
        kb["safeopsMatrix"] = {"forPair": f"{stop[0]['categoryCode']}/{stop[0]['subCategoryCode']}",
                               "suggestedSeverity": m.get("suggested"), "areaTier": m.get("tierApplied"),
                               "rationale": _clip(m.get("rationale"), 200)}
        kb["severityNote"] = (
            "The form pre-fills the SafeOps matrix severity. If the plant-practice severity "
            f"({kb['risk']['safeopsSeverity']}) differs, set it with a severityReason the user can edit."
            if m.get("suggested") and m.get("suggested") != kb["risk"]["safeopsSeverity"] else None)
    kb["reference"] = obs_kb.library()["meta"].get("source")
    return kb


async def _update_observation_draft(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.models.plant import Area
    from app.services import observation_severity as sev
    from app.services.observation_taxonomy import axis_for_type, validate_selection
    from app.services.permissions import get_accessible_plants_for

    draft = _obs_draft(ctx, required=True)
    assert draft is not None
    if ctx.form_patches >= 8:
        raise ToolError("Too many form updates in one turn — summarise and let the user review.")
    patch, err = form_drafts.parse_observation_patch(args)
    if err:
        raise ToolError(err)
    assert patch is not None
    merged = {**draft, **patch}
    plant_id = merged.get("plantId")
    accessible = await get_accessible_plants_for(db, ctx.user_id, "OBSERVATION.CREATE")
    if plant_id and accessible is not None and plant_id not in accessible:
        raise ToolError("You cannot raise observations at the form's plant")
    if patch.get("areaId"):
        area = await db.get(Area, patch["areaId"])
        if area is None or area.plantId != plant_id:
            raise ToolError("areaId is not an area of the form's plant; use one from the draft's areas list")
    otype = merged.get("type")
    if "categoryCode" in patch or "type" in patch:
        if otype and axis_for_type(otype) and (merged.get("categoryCode") or merged.get("subCategoryCode")):
            await validate_selection(db, otype, merged.get("categoryCode"), merged.get("subCategoryCode"))
    matrix = None
    if otype and merged.get("categoryCode") and merged.get("subCategoryCode"):
        matrix = await sev.resolve(db, observation_type=otype, category=merged["categoryCode"],
                                   sub_category=merged["subCategoryCode"], plant_id=plant_id, area_id=merged.get("areaId"))
    suggested = (matrix or {}).get("suggested")
    final_sev = merged.get("severity")
    if "severity" in patch:
        # A new severity carries its own reason (the form clears the old one).
        merged["severityReason"] = patch.get("severityReason") or None
    if "severity" in patch and suggested and final_sev != suggested:
        reason = (merged.get("severityReason") or "").strip()
        if len(reason) < form_drafts.MIN_SEVERITY_REASON:
            raise ToolError(f"Nothing was changed: the SafeOps matrix suggests {suggested} for this classification. "
                            f"Send severityReason (≥{form_drafts.MIN_SEVERITY_REASON} chars) explaining why {final_sev} fits.")
    if "severity" not in patch and suggested:
        # The form pre-fills the matrix severity when the classification changes.
        merged["severity"] = suggested
    ctx.form_draft = merged
    ctx.form_patches += 1
    await ctx.emit("form_patch", {"kind": "OBSERVATION", "patch": patch})
    return {
        "applied": sorted(patch),
        "severityMatrix": {"suggested": suggested, "rationale": _clip((matrix or {}).get("rationale"), 200)} if matrix else None,
        "formNow": {k: merged.get(k) for k in ("type", "areaId", "categoryCode", "subCategoryCode", "severity")},
        "note": "The user reviews the form and presses Submit; nothing has been saved.",
    }


# ─── HIRA ───────────────────────────────────────────────────────────────


def _hira_draft(ctx: TurnContext, *, required: bool) -> dict[str, Any] | None:
    if ctx.form_kind == "HIRA_ENTRY" and ctx.form_draft is not None:
        return ctx.form_draft
    if required:
        raise ToolError("No HIRA entry is open in the editor. Ask the user to open the entry (HIRA → study → entry).")
    return None


async def _search_hira_library(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    from app.models.hira import HiraHazard

    lib = hira_kb.library()
    if not lib["meta"].get("available"):
        return {"available": False, "note": "The HIRA reference library is not installed on this server."}
    draft = _hira_draft(ctx, required=False) or {}
    query = str(args.get("query") or draft.get("activity") or "").strip()
    htype = args.get("hazardType")
    if not query and not htype:
        raise ToolError("Give an activity or hazard to search for.")
    limit = max(1, min(int(args.get("limit") or 6), 12))
    hits = hira_kb.search(query, hazard_type=htype, section=args.get("section"),
                          significant_only=bool(args.get("significantOnly")), limit=limit)
    types = []
    for h in hits:
        if h["hazardType"] not in types:
            types.append(h["hazardType"])
    if htype and htype not in types:
        types.insert(0, htype)
    cats = sorted({c for t in types[:3] for c in hira_kb.MASTER_CATEGORIES.get(t, [])})
    masters = []
    if cats:
        rows = (await db.execute(select(HiraHazard).where(HiraHazard.category.in_(cats)).limit(60))).scalars().all()
        words = set(query.lower().split())
        rows = sorted(rows, key=lambda r: -len(words & set((r.name or "").lower().split())))
        masters = [{"hazardId": r.id, "code": r.code, "name": r.name, "category": r.category} for r in rows[:10]]
    return {
        "reference": lib["meta"].get("source"),
        "plantMethod": "GT = SP (spread) x LH (likelihood) x CN (consequence) x DE (detectability); not acceptable when "
                       f"GT > {lib['meta'].get('significantScore', 3.33)} or any legal/domino/chronic/emergency concern is Y.",
        "matches": [{
            "ref": h["ref"], "section": h["section"], "activity": h["activity"], "routine": h["routine"],
            "hazard": h["hazardText"], "hazardType": h["hazardType"], "consequence": h["consequence"],
            "plantScores": h["scores"], "concerns": [k for k, v in h["concerns"].items() if v],
            "acceptable": h["acceptable"], "controls": h["controls"], "ppe": h["ppe"], "legalRef": h["legalRef"],
            "reviewFlags": [f.split(":")[0] for f in h["reviewFlags"]], "stale": h.get("stale", False),
        } for h in hits],
        "controlIdeas": {t: hira_kb.control_ideas(t) for t in types[:3] if hira_kb.control_ideas(t)},
        "hazardMasters": masters,
        "note": None if hits else "No similar activity in the reference HIRA; use the hazard masters and good practice.",
    }


def _ctrl_list(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{"key": c.get("key"), "hierarchy": c.get("hierarchy"), "description": c.get("description"),
             "effectiveness": c.get("effectiveness")} for c in items or [] if c.get("hierarchy")]


async def _hira_whatif(db: AsyncSession, ctx: TurnContext, draft: dict[str, Any], *, before: list[dict[str, Any]],
                       after: list[dict[str, Any]], initial_l: int | None = None, initial_s: int | None = None,
                       routine: str | None = None) -> dict[str, Any]:
    from app.routers.hira import DEFAULT_ALARP_BANDS
    from app.services.agents.source_access import assert_source_access

    entry_id = draft.get("entryId")
    if entry_id:
        await assert_source_access(db, await _load_user(db, ctx), "HIRA", entry_id)
    matrix, cells, entry_routine = await form_drafts.load_matrix(db, entry_id=entry_id)
    if matrix is None or not cells:
        raise ToolError("No risk matrix is configured.")
    init = draft.get("initial") or {}
    l = initial_l or init.get("likelihood")
    s = initial_s or init.get("severity")
    if not l or not s:
        raise ToolError("The initial likelihood and severity are needed (from the open entry, or initialLikelihood/initialSeverity).")
    rt = (routine or draft.get("routine") or entry_routine or "ROUTINE").lower()
    threshold = (matrix.acceptableResidual or {}).get(rt)
    return form_drafts.assess_controls(cells=cells, initial_l=int(l), initial_s=int(s), before_controls=before,
                                       after_controls=after, threshold=threshold,
                                       bands=matrix.alarpBands or DEFAULT_ALARP_BANDS, matrix_code=matrix.code)


async def _assess_hira_controls(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    draft = _hira_draft(ctx, required=False) or {}
    before = _ctrl_list(draft.get("existingControls"))
    remove = set(args.get("removeControlKeys") or [])
    eff = {c["key"]: c["effectiveness"] for c in args.get("setEffectiveness") or [] if c.get("key")}
    unknown = (remove | set(eff)) - {c["key"] for c in before}
    if unknown:
        raise ToolError(f"Unknown control keys: {', '.join(sorted(unknown))}. Use keys from the draft's existingControls.")
    after = [{**c, "effectiveness": eff.get(c["key"], c["effectiveness"])} for c in before if c["key"] not in remove]
    for c in args.get("addControls") or []:
        after.append({"key": None, "hierarchy": c.get("hierarchy"), "description": c.get("description"),
                      "effectiveness": c.get("effectiveness")})
    if args.get("includeRecommended"):
        # Forecast: recommended controls implemented and verified effective.
        after += [{"key": None, "hierarchy": c.get("hierarchy"), "description": c.get("description"), "effectiveness": "EFFECTIVE"}
                  for c in draft.get("recommendedControls") or [] if c.get("hierarchy")
                  and (c.get("status") or "PROPOSED").upper() not in ("REJECTED", "DEFERRED")]
    out = await _hira_whatif(db, ctx, draft, before=before, after=after, initial_l=args.get("initialLikelihood"),
                             initial_s=args.get("initialSeverity"), routine=args.get("routine"))
    pm = args.get("plantMethod")
    if isinstance(pm, dict):
        concerns = pm.get("concerns") or {}
        now = hira_kb.plant_method(pm["SP"], pm["LH"], pm["CN"], pm["DE"], concerns)
        res: dict[str, Any] = {"now": now}
        if isinstance(pm.get("after"), dict):
            a = {k: pm["after"].get(k) or pm[k] for k in ("SP", "LH", "CN", "DE")}
            res["after"] = hira_kb.plant_method(a["SP"], a["LH"], a["CN"], a["DE"], concerns)
        res["howControlsMoveIt"] = ("Engineering and administrative controls lower LH; detection aids (alarms, gas "
                                    "detectors, inspections) lower DE; segregation and barriers lower SP; only elimination "
                                    "or substitution lowers CN. A Y concern keeps the risk significant whatever GT is.")
        out["plantMethod"] = res
    return out


async def _update_hira_entry_draft(db: AsyncSession, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    draft = _hira_draft(ctx, required=True)
    assert draft is not None
    if ctx.form_patches >= 8:
        raise ToolError("Too many editor updates in one turn — summarise and let the user review.")
    patch, err = form_drafts.parse_hira_patch(args, draft)
    if err:
        raise ToolError(err)
    assert patch is not None
    if t := patch.get("target"):
        matrix, cells, _ = await form_drafts.load_matrix(db, entry_id=draft.get("entryId"))
        if not any(c.likelihoodScore == t["likelihoodScore"] and c.severityScore == t["severityScore"] for c in cells):
            raise ToolError("Nothing was changed: target likelihood/severity is not a cell of this entry's matrix.")
    merged = form_drafts.apply_hira_patch(draft, patch)
    whatif = await _hira_whatif(db, ctx, merged, before=_ctrl_list(draft.get("existingControls")),
                                after=_ctrl_list(merged.get("existingControls")))
    ctx.form_draft = merged
    ctx.form_patches += 1
    await ctx.emit("form_patch", {"kind": "HIRA_ENTRY", "patch": patch})
    return {"applied": sorted(patch), "residualChange": {k: whatif[k] for k in ("before", "after", "direction", "explanation")},
            "note": "Shown in the editor as unsaved changes; the user reviews and presses Save."}


_HANDLERS: dict[str, Callable[[AsyncSession, dict[str, Any], TurnContext], Awaitable[dict[str, Any]]]] = {
    "find_records": _find_records,
    "run_specialist_agent": _run_specialist,
    "get_specialist_result": _get_specialist_result,
    "suggest_navigation": _suggest_navigation,
    "get_ptw_reference": _get_ptw_reference,
    "get_plant_options": _get_plant_options,
    "find_people": _find_people,
    "update_ptw_draft": _update_ptw_draft,
    "check_ptw_draft": _check_ptw_draft,
    "ptw_risk_context": _ptw_risk_context,
    "find_past_permits": _find_past_permits,
    "suggest_observation_fields": _suggest_observation_fields,
    "update_observation_draft": _update_observation_draft,
    "search_hira_library": _search_hira_library,
    "assess_hira_controls": _assess_hira_controls,
    "update_hira_entry_draft": _update_hira_entry_draft,
}

assert set(_HANDLERS) == set(TOOL_NAMES), "every tool needs a handler"

__all__ = ["SPECIALISTS", "TOOL_DEFINITIONS", "TOOL_LABELS", "TOOL_NAMES", "ToolError", "TurnContext", "dispatch"]
