"""Input rules for the copilot's HIRA write tools.

The copilot can create a HIRA study, add activity entries with hazards and
controls, edit a saved entry and submit it for review. The handlers (in
`tools.py`) call the HIRA router's own endpoint functions, so permissions,
numbering, risk scoring, versioning and the ISO 45001 rationale rules all stay
in one place. This module only cleans the model's arguments and merges
additions into an entry's current hazard / control lists, because the router's
child endpoints replace a whole list at once.

Everything here is pure (no DB), so it is tested offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.copilot.form_drafts import COST_BANDS, EFFECTIVENESS, HIERARCHY

SCOPE_TYPES = ("PLANT", "DEPARTMENT", "AREA", "ACTIVITY", "EQUIPMENT", "PROCESS")
REVIEW_FREQUENCIES = ("ANNUAL", "BIENNIAL", "QUARTERLY", "TRIGGERED_ONLY", "CUSTOM")
TEAM_ROLES = ("FACILITATOR", "SUBJECT_MATTER_EXPERT", "OPERATOR_REP", "SAFETY_OFFICER", "DEPARTMENT_HEAD",
              "EXTERNAL_CONSULTANT")
ROUTINES = ("ROUTINE", "NON_ROUTINE", "EMERGENCY")
FREQUENCIES = ("CONTINUOUS", "DAILY", "WEEKLY", "MONTHLY", "OCCASIONAL", "RARE")
# Studies that still accept new entries (the router's own rule).
OPEN_STUDY_STATUSES = ("DRAFT", "IN_PROGRESS")
# Studies whose entries can only change with a change reason (a new version).
LOCKED_STUDY_STATUSES = ("APPROVED", "ACTIVE")

MIN_RATIONALE = 10
MIN_CONSEQUENCE = 10
MAX_HAZARDS = 15
MAX_CONTROLS = 15
MAX_RECOMMENDED = 10


class InputError(ValueError):
    """Bad tool arguments. The message is shown to the model so it can fix them."""


# ─── Scalar helpers ─────────────────────────────────────────────────────


def _s(v: Any, n: int) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    return t[:n] if t else None


def _req(args: dict[str, Any], key: str, n: int, *, min_len: int = 1, label: str | None = None) -> str:
    v = _s(args.get(key), n)
    if v is None or len(v) < min_len:
        raise InputError(f"{label or key} is required" + (f" (at least {min_len} characters)" if min_len > 1 else ""))
    return v


def _enum(v: Any, allowed: tuple[str, ...], key: str) -> str | None:
    if v is None or v == "":
        return None
    t = str(v).strip().upper()
    if t not in allowed:
        raise InputError(f"{key} must be one of {', '.join(allowed)}")
    return t


def _int(v: Any, key: str, lo: int, hi: int) -> int | None:
    if v is None:
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise InputError(f"{key} must be a whole number") from None
    if not lo <= i <= hi:
        raise InputError(f"{key} must be between {lo} and {hi}")
    return i


def _str_list(v: Any, key: str, *, max_items: int = 20, n: int = 120) -> list[str] | None:
    if v is None:
        return None
    if not isinstance(v, list):
        raise InputError(f"{key} must be a list")
    out = [s for s in (_s(x, n) for x in v[:max_items]) if s]
    return out


def _date(v: Any, key: str) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        d = datetime.strptime(str(v)[:10], "%Y-%m-%d")
    except ValueError:
        raise InputError(f"{key} must be a date like 2026-12-31") from None
    return d.replace(tzinfo=timezone.utc)


def _objs(v: Any, key: str, max_items: int) -> list[dict[str, Any]]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise InputError(f"{key} must be a list of objects")
    if len(v) > max_items:
        raise InputError(f"{key}: at most {max_items} at a time")
    return v


# ─── Lists the model sends ──────────────────────────────────────────────


def parse_hazards(v: Any, key: str = "hazards") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, h in enumerate(_objs(v, key, MAX_HAZARDS), start=1):
        hid = _s(h.get("hazardId"), 64)
        if not hid:
            raise InputError(f"{key}[{i}]: hazardId is required (from find_hazard_masters)")
        if hid in seen:
            raise InputError(f"{key}[{i}]: hazard {hid} is listed twice")
        seen.add(hid)
        consequence = _s(h.get("consequence"), 1000)
        if not consequence or len(consequence) < MIN_CONSEQUENCE:
            raise InputError(f"{key}[{i}]: consequence is required — who is harmed, how, and how badly "
                             f"(at least {MIN_CONSEQUENCE} characters)")
        out.append({
            "hazardId": hid,
            "consequence": consequence,
            "contextualDescription": _s(h.get("contextualDescription"), 1000),
            "regulationRef": _s(h.get("regulationRef"), 120),
            "regulationSection": _s(h.get("regulationSection"), 120),
        })
    return out


def parse_controls(v: Any, key: str = "existingControls") -> list[dict[str, Any]]:
    out = []
    for i, c in enumerate(_objs(v, key, MAX_CONTROLS), start=1):
        hierarchy = _enum(c.get("hierarchy"), HIERARCHY, f"{key}[{i}].hierarchy")
        if not hierarchy:
            raise InputError(f"{key}[{i}]: hierarchy is required")
        out.append({
            "hierarchy": hierarchy,
            "description": _req(c, "description", 500, min_len=4, label=f"{key}[{i}].description"),
            "effectiveness": _enum(c.get("effectiveness"), EFFECTIVENESS, f"{key}[{i}].effectiveness"),
        })
    return out


def parse_recommended(v: Any, key: str = "recommendedControls") -> list[dict[str, Any]]:
    out = []
    for i, c in enumerate(_objs(v, key, MAX_RECOMMENDED), start=1):
        hierarchy = _enum(c.get("hierarchy"), HIERARCHY, f"{key}[{i}].hierarchy")
        if not hierarchy:
            raise InputError(f"{key}[{i}]: hierarchy is required")
        out.append({
            "hierarchy": hierarchy,
            "description": _req(c, "description", 500, min_len=4, label=f"{key}[{i}].description"),
            "rationale": _s(c.get("rationale"), 500),
            "estimatedCostBand": _enum(c.get("estimatedCostBand"), COST_BANDS, f"{key}[{i}].estimatedCostBand"),
        })
    return out


def _rationale(args: dict[str, Any], key: str, *, required: bool) -> str | None:
    v = _s(args.get(key), 1000)
    if v is None:
        if required:
            raise InputError(f"{key} is required: say in one or two sentences why this score fits "
                             "(ISO 45001 needs a documented reason for every risk score)")
        return None
    if len(v) < MIN_RATIONALE:
        raise InputError(f"{key} is too short (at least {MIN_RATIONALE} characters)")
    return v


# ─── Study ──────────────────────────────────────────────────────────────


def parse_study(args: dict[str, Any]) -> dict[str, Any]:
    plant_id = _req(args, "plantId", 64)
    title = _req(args, "title", 200, min_len=4)
    area_id = _s(args.get("areaId"), 64)
    dept_id = _s(args.get("departmentId"), 64)
    scope = _enum(args.get("scopeType"), SCOPE_TYPES, "scopeType") or (
        "AREA" if area_id else "DEPARTMENT" if dept_id else "PLANT")
    review = _enum(args.get("reviewFrequency"), REVIEW_FREQUENCIES, "reviewFrequency") or "ANNUAL"
    months = _int(args.get("customReviewMonths"), "customReviewMonths", 1, 60)
    if review == "CUSTOM" and months is None:
        raise InputError("customReviewMonths is required when reviewFrequency is CUSTOM")
    team = []
    seen: set[str] = set()
    for i, m in enumerate(_objs(args.get("team"), "team", 12), start=1):
        uid = _s(m.get("userId"), 64)
        if not uid:
            raise InputError(f"team[{i}]: userId is required (from find_people)")
        role = _enum(m.get("teamRole"), TEAM_ROLES, f"team[{i}].teamRole") or "SUBJECT_MATTER_EXPERT"
        if uid not in seen:
            seen.add(uid)
            team.append({"userId": uid, "teamRole": role})
    return {
        "plantId": plant_id,
        "title": title,
        "scopeType": scope,
        "areaId": area_id,
        "departmentId": dept_id,
        "processCode": _s(args.get("processCode"), 60),
        "description": _s(args.get("description"), 2000),
        "riskMatrixId": _s(args.get("riskMatrixId"), 64),
        "reviewFrequency": review,
        "customReviewMonths": months if review == "CUSTOM" else None,
        "targetCompletionDate": _date(args.get("targetCompletionDate"), "targetCompletionDate"),
        "applicableRegulations": _str_list(args.get("applicableRegulations"), "applicableRegulations", max_items=10),
        "team": team,
    }


# ─── New entry ──────────────────────────────────────────────────────────


def parse_new_entry(args: dict[str, Any]) -> dict[str, Any]:
    hazards = parse_hazards(args.get("hazards"))
    if not hazards:
        raise InputError("hazards: add at least one hazard (hazardId from find_hazard_masters, with its consequence)")
    likelihood = _int(args.get("initialLikelihood"), "initialLikelihood", 1, 10)
    severity = _int(args.get("initialSeverity"), "initialSeverity", 1, 10)
    if likelihood is None or severity is None:
        raise InputError("initialLikelihood and initialSeverity (scores on the study's matrix) are required")
    return {
        "activityDescription": _req(args, "activityDescription", 2000, min_len=4),
        "routine": _enum(args.get("routine"), ROUTINES, "routine") or "ROUTINE",
        "frequency": _enum(args.get("frequency"), FREQUENCIES, "frequency") or "DAILY",
        "areaId": _s(args.get("areaId"), 64),
        "subLocation": _s(args.get("subLocation"), 200),
        "groupLabel": _s(args.get("groupLabel"), 120),
        "typicalDurationMin": _int(args.get("typicalDurationMin"), "typicalDurationMin", 1, 10080),
        "personsEmployees": _int(args.get("personsEmployees"), "personsEmployees", 0, 10000) or 0,
        "personsContractors": _int(args.get("personsContractors"), "personsContractors", 0, 10000) or 0,
        "personsVisitors": _int(args.get("personsVisitors"), "personsVisitors", 0, 10000) or 0,
        "personsPublic": _int(args.get("personsPublic"), "personsPublic", 0, 10000) or 0,
        "affectedPersonGroups": _s(args.get("affectedPersonGroups"), 300),
        "equipmentUsed": _str_list(args.get("equipmentUsed"), "equipmentUsed"),
        "materialsUsed": _str_list(args.get("materialsUsed"), "materialsUsed"),
        "energySourcesPresent": _str_list(args.get("energySourcesPresent"), "energySourcesPresent"),
        "initialLikelihood": likelihood,
        "initialSeverity": severity,
        "initialLikelihoodRationale": _rationale(args, "initialLikelihoodRationale", required=True),
        "initialSeverityRationale": _rationale(args, "initialSeverityRationale", required=True),
        "residualLikelihoodRationale": _rationale(args, "residualLikelihoodRationale", required=False),
        "residualSeverityRationale": _rationale(args, "residualSeverityRationale", required=False),
        "hazards": hazards,
        "existingControls": parse_controls(args.get("existingControls")),
        "recommendedControls": parse_recommended(args.get("recommendedControls")),
    }


# ─── Entry update ───────────────────────────────────────────────────────

_RATIONALES = ("initialLikelihoodRationale", "initialSeverityRationale",
               "residualLikelihoodRationale", "residualSeverityRationale")


def parse_entry_update(args: dict[str, Any]) -> dict[str, Any]:
    """Returns {"fields": {...PATCH fields...}, "initial": (L, S) | None, "target": {...} | None,
    "addHazards", "updateHazards", "removeHazardIds", "addExistingControls", "setControlEffectiveness",
    "removeExistingControlKeys", "addRecommendedControls", "removeRecommendedControlKeys", "changeReason"}."""
    # A null never clears a saved value: required fields (routine, frequency,
    # rationales) can't be blanked from the chat, and the editor is the place to
    # clear an optional one.
    fields: dict[str, Any] = {}
    if args.get("activityDescription") is not None:
        fields["activityDescription"] = _req(args, "activityDescription", 2000, min_len=4)
    for k, allowed in (("routine", ROUTINES), ("frequency", FREQUENCIES)):
        v = _enum(args.get(k), allowed, k)
        if v:
            fields[k] = v
    for k in ("subLocation", "affectedPersonGroups"):
        v = _s(args.get(k), 300)
        if v:
            fields[k] = v
    for k in ("personsEmployees", "personsContractors", "personsVisitors", "personsPublic"):
        v = _int(args.get(k), k, 0, 10000)
        if v is not None:
            fields[k] = v
    for k in ("equipmentUsed", "materialsUsed", "energySourcesPresent"):
        if args.get(k) is not None:
            fields[k] = _str_list(args[k], k) or []
    for k in _RATIONALES:
        v = _rationale(args, k, required=False)
        if v:
            fields[k] = v

    initial = None
    if "initialLikelihood" in args or "initialSeverity" in args:
        initial = (_int(args.get("initialLikelihood"), "initialLikelihood", 1, 10),
                   _int(args.get("initialSeverity"), "initialSeverity", 1, 10))

    target = None
    t = args.get("target")
    if t is not None:
        if not isinstance(t, dict):
            raise InputError("target must be an object")
        tl = _int(t.get("likelihood"), "target.likelihood", 1, 10)
        ts = _int(t.get("severity"), "target.severity", 1, 10)
        if tl is None or ts is None:
            raise InputError("target needs likelihood and severity")
        target = {"likelihood": tl, "severity": ts, "rationale": _s(t.get("rationale"), 600)}

    update_hazards = []
    for i, h in enumerate(_objs(args.get("updateHazards"), "updateHazards", MAX_HAZARDS), start=1):
        hid = _s(h.get("hazardId"), 64)
        if not hid:
            raise InputError(f"updateHazards[{i}]: hazardId is required")
        c = _s(h.get("consequence"), 1000)
        if c is not None and len(c) < MIN_CONSEQUENCE:
            raise InputError(f"updateHazards[{i}]: consequence is too short")
        update_hazards.append({"hazardId": hid, "consequence": c,
                               "contextualDescription": _s(h.get("contextualDescription"), 1000),
                               "contextualDescriptionSet": "contextualDescription" in h})

    eff = []
    for i, e in enumerate(_objs(args.get("setControlEffectiveness"), "setControlEffectiveness", 20), start=1):
        key = _s(e.get("key"), 64)
        value = _enum(e.get("effectiveness"), EFFECTIVENESS, f"setControlEffectiveness[{i}].effectiveness")
        if not key or not value:
            raise InputError(f"setControlEffectiveness[{i}] needs key and effectiveness")
        eff.append({"key": key, "effectiveness": value})

    out = {
        "fields": fields,
        "initial": initial,
        "target": target,
        "addHazards": parse_hazards(args.get("addHazards"), "addHazards"),
        "updateHazards": update_hazards,
        "removeHazardIds": _str_list(args.get("removeHazardIds"), "removeHazardIds", n=64) or [],
        "addExistingControls": parse_controls(args.get("addExistingControls"), "addExistingControls"),
        "setControlEffectiveness": eff,
        "removeExistingControlKeys": _str_list(args.get("removeExistingControlKeys"), "removeExistingControlKeys",
                                               n=64) or [],
        "addRecommendedControls": parse_recommended(args.get("addRecommendedControls"), "addRecommendedControls"),
        "removeRecommendedControlKeys": _str_list(args.get("removeRecommendedControlKeys"),
                                                  "removeRecommendedControlKeys", n=64) or [],
        "changeReason": _s(args.get("changeReason"), 500),
    }
    if initial is not None and (initial[0] is None or initial[1] is None):
        raise InputError("send initialLikelihood and initialSeverity together")
    if not any(out[k] for k in out if k != "changeReason"):
        raise InputError("Nothing to change")
    return out


def touches_hazards(u: dict[str, Any]) -> bool:
    return bool(u["addHazards"] or u["updateHazards"] or u["removeHazardIds"])


def touches_controls(u: dict[str, Any]) -> bool:
    return bool(u["addExistingControls"] or u["setControlEffectiveness"] or u["removeExistingControlKeys"])


def touches_recommended(u: dict[str, Any]) -> bool:
    return bool(u["addRecommendedControls"] or u["removeRecommendedControlKeys"])


# ─── Merging into the saved lists ───────────────────────────────────────


def merge_hazards(existing: list[dict[str, Any]], add: list[dict[str, Any]], update: list[dict[str, Any]],
                  remove: list[str]) -> list[dict[str, Any]]:
    """The full hazard list to PUT. `existing` rows carry hazardId, consequence,
    contextualDescription, regulationRef, regulationSection."""
    by_id = {h["hazardId"]: dict(h) for h in existing}
    unknown = [h for h in remove if h not in by_id]
    if unknown:
        raise InputError(f"removeHazardIds not on this entry: {', '.join(unknown)}")
    for u in update:
        row = by_id.get(u["hazardId"])
        if row is None:
            raise InputError(f"updateHazards: hazard {u['hazardId']} is not on this entry (use addHazards)")
        if u["consequence"]:
            row["consequence"] = u["consequence"]
        if u["contextualDescriptionSet"]:
            row["contextualDescription"] = u["contextualDescription"]
    out = [by_id[h["hazardId"]] for h in existing if h["hazardId"] not in remove]
    for a in add:
        if a["hazardId"] in by_id and a["hazardId"] not in remove:
            raise InputError(f"addHazards: hazard {a['hazardId']} is already on this entry (use updateHazards)")
        out.append(dict(a))
    if not out:
        raise InputError("An entry needs at least one hazard; add one before removing the last")
    if len(out) > 25:
        raise InputError("An entry can hold at most 25 hazards")
    for i, h in enumerate(out):
        h["sortOrder"] = i
    return out


_CONTROL_KEEP = ("id", "controlId", "hierarchy", "description", "effectiveness", "verificationMethod",
                 "verificationFreq", "responsibleRole", "evidenceAttached", "documentReference")


def merge_existing_controls(existing: list[dict[str, Any]], add: list[dict[str, Any]], set_eff: list[dict[str, Any]],
                            remove: list[str]) -> list[dict[str, Any]]:
    by_key = {c["id"]: c for c in existing}
    unknown = [k for k in [*remove, *(e["key"] for e in set_eff)] if k not in by_key]
    if unknown:
        raise InputError(f"Unknown existing-control keys: {', '.join(unknown)} (use the keys from get_hira_study)")
    eff = {e["key"]: e["effectiveness"] for e in set_eff}
    out = []
    for c in existing:
        if c["id"] in remove:
            continue
        row = {k: c.get(k) for k in _CONTROL_KEEP}
        row["evidenceAttached"] = bool(row.get("evidenceAttached"))
        if c["id"] in eff:
            row["effectiveness"] = eff[c["id"]]
        out.append(row)
    seen = {(c["hierarchy"], (c["description"] or "").strip().lower()) for c in out}
    for a in add:
        sig = (a["hierarchy"], a["description"].strip().lower())
        if sig in seen:
            continue  # already recorded; adding it twice would double-count its credit
        seen.add(sig)
        out.append({"hierarchy": a["hierarchy"], "description": a["description"],
                    "effectiveness": a["effectiveness"], "evidenceAttached": False})
    for i, c in enumerate(out):
        c["sortOrder"] = i
    return out


_RECOMMENDED_KEEP = ("id", "hierarchy", "description", "rationale", "targetLikelihoodReduction",
                     "targetSeverityReduction", "estimatedCostBand", "proposedImplementationDate", "responsibleId",
                     "status", "capaId", "evidenceAttached", "documentReference")


def merge_recommended(existing: list[dict[str, Any]], add: list[dict[str, Any]], remove: list[str]) -> list[dict[str, Any]]:
    by_key = {c["id"]: c for c in existing}
    unknown = [k for k in remove if k not in by_key]
    if unknown:
        raise InputError(f"Unknown recommended-control keys: {', '.join(unknown)}")
    locked = [k for k in remove if (by_key[k].get("status") or "PROPOSED") != "PROPOSED" or by_key[k].get("capaId")]
    if locked:
        raise InputError("Only proposed controls with no CAPA can be removed; these have moved on: " + ", ".join(locked))
    out = []
    for c in existing:
        if c["id"] in remove:
            continue
        row = {k: c.get(k) for k in _RECOMMENDED_KEEP}
        row["evidenceAttached"] = bool(row.get("evidenceAttached"))
        out.append(row)
    seen = {(c["hierarchy"], (c["description"] or "").strip().lower()) for c in out}
    for a in add:
        sig = (a["hierarchy"], a["description"].strip().lower())
        if sig in seen:
            continue
        seen.add(sig)
        out.append({"hierarchy": a["hierarchy"], "description": a["description"], "rationale": a["rationale"],
                    "estimatedCostBand": a["estimatedCostBand"], "status": "PROPOSED", "evidenceAttached": False})
    return out


__all__ = [
    "FREQUENCIES", "InputError", "LOCKED_STUDY_STATUSES", "OPEN_STUDY_STATUSES", "REVIEW_FREQUENCIES", "ROUTINES",
    "SCOPE_TYPES", "TEAM_ROLES", "merge_existing_controls", "merge_hazards", "merge_recommended", "parse_controls",
    "parse_entry_update", "parse_hazards", "parse_new_entry", "parse_recommended", "parse_study",
    "touches_controls", "touches_hazards", "touches_recommended",
]
