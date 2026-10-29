"""Agent runtime for the user-initiated agent platform.

Responsibilities:
  • Load the agent config and active prompt
  • Authorize the caller against the agent's invoke permission code
  • Enforce per-hour rate limit per plant
  • Create the AgentInvocation row (status=RUNNING)
  • Build the input context for the agent (Commit 1 ships a generic
    record-fetch; Commit 3 swaps this for the RCA-specific context
    builder)
  • Run the tool-use loop via complete_with_tools()
  • Parse <reasoning> / <suggestion> / <confidence> from the final text
  • Detect hallucinations (invented record IDs)
  • Persist the result (RUNNING → PENDING_REVIEW / ERRORED)
  • Record human accept/modify/reject decisions
  • Update agent rolling metrics on each decision

Concurrency note: agent invocations are kicked off via FastAPI's
BackgroundTasks. The route returns the invocation ID immediately; the
loop runs in the background and the frontend polls. The background
task gets its own AsyncSession (passed explicitly here, not via
Depends) so it survives the request lifecycle.

Pricing note (cost computation): Anthropic prices vary by model. We
keep a static price table here for cost tracking. Real production
pricing should be loaded from a config table — punt that to Commit 5
when the operations dashboard lands.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.models.agent import Agent, AgentInvocation, AgentPrompt, AgentToolCall
from app.models.capa import Capa
from app.models.flra import FLRA
from app.models.hira import HiraCapa, HiraEntry, HiraEntryHazard, HiraHazard, HiraStudy
from app.models.incident import Incident, IncidentCapa
from app.models.near_miss import NearMiss
from app.models.observation import Observation
from app.models.permit import Permit
from app.services.agents.context_builders import CONTEXT_BUILDERS
from app.services.agents.tools import get_tool_definitions, get_tool_handler
from app.services.agents.tools._scope import tool_scope
from app.services.ai.anthropic_client import (
    AnthropicApiError,
    AnthropicNotConfiguredError,
    ToolLoopResult,
    complete_with_tools,
)

# ─── Pricing table (USD per million tokens) ────────────────────────────
# Approximate Anthropic public pricing at time of writing. Update as
# Anthropic publishes new tiers. Used only for cost tracking; never
# affects routing. Real numbers come from billing.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_million, output_per_million)
    # Opus 4.7 is $5/$25 (the $15/$75 figure was Opus 4.1-era pricing and
    # tripled every escalated run's recorded cost).
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE = (3.0, 15.0)  # fallback when model not in table


# ─── Agent code → permission code mapping ──────────────────────────────
# Each user-initiated agent gets its own AGENT.<MODULE>.INVOKE permission
# so RBAC can restrict the pilot to specific plants/roles. Adding a new
# agent means adding a row here AND seeding the permission in seed-rbac.ts.
_INVOKE_PERMISSIONS: dict[str, str] = {
    "RCA_ASSISTANT": "AGENT.RCA_INVOKE",
    "PERMIT_RISK_REVIEWER": "AGENT.PERMIT_REVIEW_INVOKE",
    "TRIAGE_AGENT": "AGENT.TRIAGE_INVOKE",
    "HIRA_ASSISTANT": "AGENT.HIRA_INVOKE",
    "CAPA_ASSISTANT": "AGENT.CAPA_INVOKE",
    # Future agents register here.
}


# ─── Hallucination check ───────────────────────────────────────────────
# Record numbers come in many shapes (INC-2026-NW-0045, INC-PFI-002,
# INC-SW-HIST-02-01, PTW-AXM-2026-0841, FACROLL-PTW-AMD-01, SO-2026-NW-0231,
# CAPA-MAG-MAG-TIR1-01, ...), so fixed per-format regexes both missed real
# references and matched fragments of them ("PTW-AXM-2026" out of
# PTW-AXM-2026-0841). Instead, take every whole hyphenated token, classify it
# by its record prefix (first segment, or second for site-prefixed numbers),
# and look the exact token up in every column that holds that kind of number.
_RECORD_TOKEN = re.compile(r"(?<![A-Za-z0-9-])([A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,6})(?![A-Za-z0-9-])")

# prefix -> (label, [(model, column name), ...])
_RECORD_PREFIXES: dict[str, tuple[str, list[tuple[type[Any], str]]]] = {
    "INC": ("Incident", [(Incident, "number")]),
    "PTW": ("Permit", [(Permit, "number")]),
    "NM": ("NearMiss", [(NearMiss, "number")]),
    "OBS": ("Observation", [(Observation, "number")]),
    "SO": ("Observation", [(Observation, "number")]),
    "CAPA": (
        "CAPA",
        [(Capa, "capaNumber"), (Capa, "aliasNumber"), (IncidentCapa, "capaNumber"), (HiraCapa, "number")],
    ),
    "HIRA": ("HIRA study", [(HiraStudy, "number")]),
    "FLRA": ("FLRA", [(FLRA, "number")]),
}


def _classify_record_token(token: str) -> str | None:
    """Return the record prefix a token refers to, or None if it isn't a
    record number (e.g. ISO-45001, XCORR-019, AINV-2026-000001)."""
    if not any(ch.isdigit() for ch in token):
        return None
    parts = token.split("-")
    if parts[0] in _RECORD_PREFIXES:
        return parts[0]
    if len(parts) > 2 and parts[1] in _RECORD_PREFIXES:
        return parts[1]
    return None


@dataclass
class InvocationResult:
    """Returned by invoke_agent() so callers / tests can introspect the
    full outcome without re-querying the DB."""

    invocation_id: str
    invocation_number: str
    status: str  # "PENDING_REVIEW" or "ERRORED"
    suggestion: dict[str, Any] | None
    reasoning: str | None
    confidence: float | None
    tool_call_count: int
    cost_usd: float | None
    latency_ms: int | None
    hallucination_flagged: bool


# ─────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────


async def start_invocation(
    *,
    db: AsyncSession,
    agent_code: str,
    source_module: str,
    source_record_id: str,
    user_id: str,
    force_escalation_model: bool = False,
) -> AgentInvocation:
    """Create the AgentInvocation row (status=RUNNING) and return it.

    Caller is expected to:
      1. Have already verified the AGENT.*.INVOKE permission via the
         standard permissions service.
      2. Schedule run_invocation() in a BackgroundTask so the HTTP
         response can return immediately.

    Side effects:
      • Enforces rate limit (raises HTTPException 429 on breach).
      • Resolves plant scope from the source record (used by rate limit
         and for filtering in the dashboard).
      • Generates the invocation number.

    Does NOT call Anthropic. That happens in run_invocation().
    """
    agent = await _load_agent(db, agent_code)
    prompt = await _load_active_prompt(db, agent)

    plant_id = await _resolve_source_plant_id(db, source_module, source_record_id)
    await _enforce_rate_limit(db, agent, plant_id)

    invocation_number = await _generate_invocation_number(db)
    model_to_use = (
        agent.escalationModelId
        if force_escalation_model and agent.escalationModelId
        else agent.primaryModelId
    )

    invocation = AgentInvocation(
        invocationNumber=invocation_number,
        agentId=agent.id,
        invokedById=user_id,
        invocationTrigger="USER_INITIATED",
        sourceModule=source_module,
        sourceRecordId=source_record_id,
        sourceRecordType=source_module,
        sourcePlantId=plant_id,
        authorityLevelUsed=agent.currentAuthorityLevel,
        promptVersionId=prompt.id,
        modelUsed=model_to_use,
        inputContext={},  # filled in run_invocation when context is built
        status="RUNNING",
    )
    db.add(invocation)
    # Count every run here, not only the ones that reach PENDING_REVIEW, so
    # the metric matches the AgentInvocation rows (errored runs included).
    agent.totalInvocations = (agent.totalInvocations or 0) + 1
    prompt.invocationCount = (prompt.invocationCount or 0) + 1
    await db.flush()
    await db.refresh(invocation)
    await db.commit()
    return invocation


async def run_invocation(
    *,
    db: AsyncSession,
    invocation_id: str,
) -> InvocationResult:
    """Execute the tool-use loop for an invocation already in RUNNING
    state. This is the function the BackgroundTask runs.

    Catches all errors and lands the invocation in ERRORED state with a
    populated errorDetails — the background task wrapper should not let
    exceptions propagate (no one is awaiting them).
    """
    invocation = await _load_invocation(db, invocation_id)
    if invocation.status != "RUNNING":
        raise ValueError(
            f"Invocation {invocation.invocationNumber} is in status {invocation.status}, "
            "expected RUNNING"
        )

    agent = await db.get(Agent, invocation.agentId)
    prompt = await db.get(AgentPrompt, invocation.promptVersionId)
    if agent is None or prompt is None:
        await _mark_errored(db, invocation, "INTERNAL", "Agent or prompt vanished mid-flight")
        return _result_from(invocation)

    started_at = datetime.now(timezone.utc)
    try:
        context = await _build_context(
            db,
            source_module=invocation.sourceModule,
            source_record_id=invocation.sourceRecordId,
        )
        invocation.inputContext = context
        # Commit the context now instead of holding it open until the end.
        # It is durable evidence of what the agent was handed, and it means
        # the row no longer depends on the tool loop surviving.
        await db.commit()
    except Exception as e:  # noqa: BLE001
        await _mark_errored(db, invocation, "INTERNAL", f"Context build failed: {e}")
        return _result_from(invocation)

    tool_definitions = get_tool_definitions(list(agent.availableTools))

    # Tools run on their OWN session — never the one that persists the
    # result. A tool that trips a DB error (an invalid enum literal, a
    # constraint violation, …) aborts its Postgres transaction; when that
    # was the shared session, every later tool failed with "current
    # transaction is aborted" AND the final flush blew up, stranding the
    # invocation in RUNNING with no error recorded. Isolating the session
    # and rolling back on failure caps the blast radius at one tool call.
    tool_db = AsyncSessionLocal()

    async def dispatch_tool(tool_name: str, tool_input: dict[str, Any]) -> Any:
        handler = get_tool_handler(tool_name)
        try:
            return await handler(
                tool_input,
                db=tool_db,
                source_record_id=invocation.sourceRecordId,
                source_module=invocation.sourceModule,
            )
        except Exception:
            # Clear the aborted transaction so the *next* tool still works.
            # The exception continues to the loop, which reports it back to
            # the model as a tool error.
            await tool_db.rollback()
            raise

    settings = get_settings()
    try:
        # Tools read only what the invoking user may read (see tools/_scope.py).
        with tool_scope(invocation.invokedById):
            loop_result: ToolLoopResult = await complete_with_tools(
                system=prompt.systemPrompt,
                initial_user_message=json.dumps(context, default=str, indent=2),
                tools=tool_definitions,
                dispatch_tool=dispatch_tool,
                model=invocation.modelUsed,
                max_tokens=settings.agent_max_tokens_per_turn,
                max_iterations=settings.agent_max_tool_iterations,
            )
    except AnthropicNotConfiguredError as e:
        await _mark_errored(db, invocation, "INTERNAL", str(e))
        return _result_from(invocation)
    except AnthropicApiError as e:
        await _mark_errored(db, invocation, "API_ERROR", str(e))
        return _result_from(invocation)
    except Exception as e:  # noqa: BLE001
        await _mark_errored(db, invocation, "INTERNAL", f"Unexpected runtime error: {e}")
        return _result_from(invocation)
    finally:
        await tool_db.close()

    # Everything from here writes the result. It is wrapped because a
    # failure in this block used to escape run_invocation entirely and get
    # swallowed by the background-task catch — leaving the row RUNNING
    # forever. Any failure now lands the invocation in ERRORED instead.
    try:
        # Persist tool calls
        for record in loop_result.tool_calls:
            db.add(
                AgentToolCall(
                    invocationId=invocation.id,
                    toolName=record.name,
                    toolInput=record.input,
                    toolOutput=record.output,
                    executionMs=record.execution_ms,
                    hadError=record.had_error,
                    errorDetails=record.error_details,
                    sequence=record.sequence,
                )
            )

        # Parse the final response into reasoning / suggestion / confidence
        parsed = _parse_final_response(loop_result.final_text)
        invocation.agentReasoning = parsed["reasoning"]
        invocation.agentSuggestion = await _label_hira_hazards(db, agent.code, parsed["suggestion"])
        invocation.agentConfidence = parsed["confidence"]
        invocation.rawApiResponse = loop_result.raw_last_response

        # Token / cost accounting
        invocation.inputTokens = loop_result.input_tokens_total
        invocation.outputTokens = loop_result.output_tokens_total
        invocation.totalCostUsd = _compute_cost(
            invocation.modelUsed,
            loop_result.input_tokens_total,
            loop_result.output_tokens_total,
        )
        invocation.latencyMs = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )

        # Hallucination detection — scan the agent's text for plausible
        # record IDs and verify each. The full text includes reasoning +
        # suggestion JSON; cast a wide net.
        hallucinations = await _detect_hallucinations(db, loop_result.final_text)
        if hallucinations:
            invocation.hallucinationFlagged = True
            invocation.hallucinationDetails = hallucinations

        # Soft-fail if the loop hit its iteration cap. Still PENDING_REVIEW
        # so the human can decide whether to accept what we have.
        if loop_result.hit_iteration_cap:
            invocation.errorType = "ITERATION_CAP"
            invocation.errorDetails = "Agent loop hit max_iterations without end_turn"
        elif loop_result.stop_reason == "max_tokens":
            invocation.errorType = "TRUNCATED"
            invocation.errorDetails = (
                "The answer hit the per-turn output limit and was cut off; "
                "raise AGENT_MAX_TOKENS_PER_TURN or re-run."
            )
        elif loop_result.stop_reason == "refusal":
            invocation.errorType = "REFUSAL"
            invocation.errorDetails = "The model declined to answer this request."

        invocation.status = "PENDING_REVIEW"
        await db.commit()
    except Exception as e:  # noqa: BLE001
        await _mark_errored(
            db, invocation, "INTERNAL", f"Failed to persist agent result: {e}"
        )
        return _result_from(invocation)

    return _result_from(invocation)


# How long an invocation may sit in RUNNING before the reaper calls it
# dead. Healthy runs finish in ~60s; the Anthropic client caps a single
# call at 90s with one retry, so anything past this is not coming back.
STALE_RUNNING_AFTER = timedelta(minutes=15)


async def expire_stale_invocations(
    db: AsyncSession, *, older_than: timedelta = STALE_RUNNING_AFTER
) -> int:
    """Mark long-abandoned RUNNING invocations as ERRORED and return how
    many were swept.

    An invocation is only moved off RUNNING by its own background task. If
    that task dies — process restart, deploy, unhandled error — nothing
    else ever touches the row, and the UI polls it forever (the card shows
    a spinner counting up for days). This is the backstop: it guarantees
    every invocation reaches a terminal state.

    Safe to run against a genuinely in-flight invocation: the background
    task writes its own status at the end and simply overwrites this.
    Idempotent, and commits only when it actually changed something.
    """
    cutoff = datetime.now(timezone.utc) - older_than
    result = await db.execute(
        update(AgentInvocation)
        .where(AgentInvocation.status == "RUNNING")
        .where(AgentInvocation.invokedAt < cutoff)
        .values(
            status="ERRORED",
            hadError=True,
            errorType="TIMED_OUT",
            errorDetails=(
                "The agent run was abandoned before it finished (most often a "
                "backend restart mid-run). No result was produced — start a "
                "new analysis."
            ),
        )
    )
    swept = result.rowcount or 0
    if swept:
        await db.commit()
    return swept


async def record_human_decision(
    *,
    db: AsyncSession,
    invocation_id: str,
    decision: str,
    user_id: str,
    human_modifications: dict[str, Any] | None = None,
    rejection_reason: str | None = None,
    rating: int | None = None,
    feedback: str | None = None,
) -> AgentInvocation:
    """Record the investigator's accept/modify/reject decision for an
    invocation. Updates the rolling acceptance/modification/rejection
    counters on the Agent row.

    Raises HTTPException 404 if the invocation doesn't exist, 409 if
    it's not in PENDING_REVIEW, 400 if decision is invalid.
    """
    if decision not in ("ACCEPT_AS_IS", "ACCEPT_WITH_MODIFICATION", "REJECT"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid decision: {decision!r}. Must be one of "
            "ACCEPT_AS_IS, ACCEPT_WITH_MODIFICATION, REJECT.",
        )
    if decision == "ACCEPT_WITH_MODIFICATION" and human_modifications is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "ACCEPT_WITH_MODIFICATION requires humanModifications payload",
        )
    if decision == "REJECT" and not rejection_reason:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "REJECT requires rejectionReason",
        )

    invocation = await _load_invocation(db, invocation_id)
    if invocation.status != "PENDING_REVIEW":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Invocation is in status {invocation.status!r}, cannot record decision",
        )

    invocation.humanDecisionAt = datetime.now(timezone.utc)
    invocation.humanDecisionById = user_id
    invocation.humanDecision = decision
    invocation.humanModifications = human_modifications
    invocation.rejectionReason = rejection_reason
    invocation.ratingByHuman = rating
    invocation.detailedFeedback = feedback

    if decision == "ACCEPT_AS_IS":
        invocation.status = "ACCEPTED"
    elif decision == "ACCEPT_WITH_MODIFICATION":
        invocation.status = "MODIFIED"
    else:
        invocation.status = "REJECTED"

    # Rolling counters on the Agent row. The calibration job will
    # recompute rates from these later.
    agent = await db.get(Agent, invocation.agentId)
    if agent is not None:
        if invocation.status == "ACCEPTED":
            agent.totalAcceptances += 1
        elif invocation.status == "MODIFIED":
            agent.totalModifications += 1
        elif invocation.status == "REJECTED":
            agent.totalRejections += 1

    await db.commit()
    await db.refresh(invocation)
    return invocation


def get_invoke_permission_code(agent_code: str) -> str:
    """Lookup helper used by the router. Raises if the agent code has no
    registered invoke permission — agents must be registered in
    _INVOKE_PERMISSIONS before they can be invoked."""
    try:
        return _INVOKE_PERMISSIONS[agent_code]
    except KeyError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown agent code {agent_code!r} (no invoke permission registered)",
        ) from e


# ─────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────


async def _load_agent(db: AsyncSession, agent_code: str) -> Agent:
    stmt = select(Agent).where(Agent.code == agent_code)
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Agent {agent_code!r} is not configured",
        )
    if not agent.isActive:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Agent {agent_code!r} is currently disabled",
        )
    return agent


async def _load_active_prompt(db: AsyncSession, agent: Agent) -> AgentPrompt:
    if agent.activePromptId is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Agent {agent.code!r} has no active prompt — cannot invoke",
        )
    prompt = await db.get(AgentPrompt, agent.activePromptId)
    if prompt is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Agent {agent.code!r} active prompt pointer is stale",
        )
    return prompt


async def _load_invocation(db: AsyncSession, invocation_id: str) -> AgentInvocation:
    invocation = await db.get(AgentInvocation, invocation_id)
    if invocation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invocation not found")
    return invocation


async def _resolve_source_plant_id(
    db: AsyncSession, source_module: str, source_record_id: str
) -> str | None:
    """Look up the plantId on the source record so per-plant rate limits
    and dashboard filters work. Returns None when the module doesn't
    have a plant (shouldn't happen for any operational module today)."""
    # HIRA's source record is a HiraEntry, which carries no plantId of its
    # own — the plant lives on the parent study. Resolve via the study.
    if source_module == "HIRA":
        entry = await db.get(HiraEntry, source_record_id)
        if entry is None:
            return None
        study = await db.get(HiraStudy, entry.studyId)
        return study.plantId if study is not None else None

    table_map: dict[str, type[Any]] = {
        "INCIDENT": Incident,
        "OBSERVATION": Observation,
        "NEAR_MISS": NearMiss,
        "PTW": Permit,
        "CAPA": Capa,
    }
    model = table_map.get(source_module)
    if model is None:
        return None
    row = await db.get(model, source_record_id)
    if row is None:
        # Don't fail invocation start over a missing source — the tool
        # dispatcher will surface the real error.
        return None
    return getattr(row, "plantId", None)


async def _enforce_rate_limit(
    db: AsyncSession, agent: Agent, plant_id: str | None
) -> None:
    """Block invocation when the configured rate ceiling is reached.
    Rate is per-agent per-plant per hour. A null plant counts against a
    null bucket, so global / cross-plant agents have their own quota."""
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    stmt = (
        select(func.count())
        .select_from(AgentInvocation)
        .where(AgentInvocation.agentId == agent.id)
        .where(AgentInvocation.invokedAt > one_hour_ago)
    )
    if plant_id is None:
        stmt = stmt.where(AgentInvocation.sourcePlantId.is_(None))
    else:
        stmt = stmt.where(AgentInvocation.sourcePlantId == plant_id)
    count = (await db.execute(stmt)).scalar_one()
    if count >= agent.rateLimit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Rate limit reached for agent {agent.code} "
            f"({agent.rateLimit}/hour). Try again later.",
        )


async def _generate_invocation_number(db: AsyncSession) -> str:
    """Generate "AINV-{year}-{6-digit}" by finding the MAX numeric suffix
    across this year's rows. Tolerates concurrency via the unique
    constraint on invocationNumber catching the rare race."""
    year = datetime.now(timezone.utc).year
    prefix = f"AINV-{year}-"
    existing = (
        await db.execute(
            select(AgentInvocation.invocationNumber).where(
                AgentInvocation.invocationNumber.like(f"{prefix}%")
            )
        )
    ).scalars().all()
    max_suffix = 0
    for n in existing:
        try:
            suffix = int(n.rsplit("-", 1)[-1])
            if suffix > max_suffix:
                max_suffix = suffix
        except ValueError:
            continue
    return f"{prefix}{max_suffix + 1:06d}"


async def _build_context(
    db: AsyncSession, *, source_module: str, source_record_id: str
) -> dict[str, Any]:
    """Build the input context dict fed to the agent.

    Dispatch by module to a registered builder under
    app/services/agents/context_builders/. INCIDENT has the rich
    builder (Commit 3); other modules fall back to a minimal stub
    until their builders are written. Adding a new builder is a
    one-line entry in CONTEXT_BUILDERS, no edit needed here.
    """
    builder = CONTEXT_BUILDERS.get(source_module)
    if builder is not None:
        return await builder(db, source_record_id)

    # Fallback for unregistered modules: enough for the loop to run,
    # not enough for the agent to do anything useful. The system prompt
    # will surface this thinness in its `caveats`.
    return {
        "sourceModule": source_module,
        "sourceRecordId": source_record_id,
        "_note": (
            f"No rich context builder registered for module {source_module!r}; "
            "agent has only the record ID. Register a builder in "
            "app/services/agents/context_builders/."
        ),
    }


def _coerce_suggestion_json(raw: str) -> Any:
    """Best-effort parse of the JSON inside a <suggestion> block.

    Models very often wrap the JSON in a markdown code fence
    (```json ... ```) or add a stray sentence around it. A bare
    json.loads() then fails and the whole draft was being stored as an
    unrenderable "_unparsed" blob. We strip fences and, as a last resort,
    extract the outermost {...}/[...] span before giving up.
    """
    s = raw.strip()

    # 1) Strip a surrounding markdown fence: ```json\n...\n``` or ```...```
    fence = re.match(r"^```(?:json|JSON)?\s*([\s\S]*?)\s*```$", s)
    if fence:
        s = fence.group(1).strip()

    # 2) Direct parse.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 3) Last resort: the outermost object/array span, ignoring any prose
    #    the model wrote before or after it.
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    end = max(s.rfind("}"), s.rfind("]"))
    if starts and end > min(starts):
        try:
            return json.loads(s[min(starts) : end + 1])
        except json.JSONDecodeError:
            pass

    # Genuinely unparseable — keep the raw text so the UI can still show it.
    return {"_unparsed": raw}


def _parse_final_response(text: str) -> dict[str, Any]:
    """Extract <reasoning>, <suggestion>, and <confidence> blocks from
    the agent's final assistant turn. Missing blocks return None — the
    caller decides whether that's an error."""
    out: dict[str, Any] = {"reasoning": None, "suggestion": None, "confidence": None}

    reasoning_match = re.search(r"<reasoning>([\s\S]*?)</reasoning>", text)
    if reasoning_match:
        out["reasoning"] = reasoning_match.group(1).strip()

    suggestion_match = re.search(r"<suggestion>([\s\S]*?)</suggestion>", text)
    if suggestion_match:
        out["suggestion"] = _coerce_suggestion_json(suggestion_match.group(1))
    else:
        # Model skipped the <suggestion> tags but may have emitted a fenced
        # JSON block (or a bare object) anyway — recover it rather than
        # reporting a null draft.
        fenced = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)\s*```", text)
        if fenced:
            recovered = _coerce_suggestion_json(fenced.group(1))
            if "_unparsed" not in recovered:
                out["suggestion"] = recovered

    confidence_match = re.search(r"<confidence>([\d.]+)</confidence>", text)
    if confidence_match:
        try:
            value = float(confidence_match.group(1))
            if 0.0 <= value <= 1.0:
                out["confidence"] = value
        except ValueError:
            pass

    return out


async def _label_hira_hazards(
    db: AsyncSession, agent_code: str, suggestion: Any
) -> Any:
    """HIRA suggestions name hazards by id only, which the review card would
    show as raw cuids. Put the library name and category beside each id, and
    keep one entry per hazard — the model sometimes repeats one and marks the
    copy "disregard". An id found in neither table is labelled as such."""
    if agent_code != "HIRA_ASSISTANT" or not isinstance(suggestion, dict):
        return suggestion
    hazards = suggestion.get("suggested_hazards")
    controls = suggestion.get("control_suggestions")
    hazards = [h for h in hazards if isinstance(h, dict)] if isinstance(hazards, list) else None
    controls = [c for c in controls if isinstance(c, dict)] if isinstance(controls, list) else None
    if not hazards and not controls:
        return suggestion

    ids = {h.get("hazard_master_id") for h in hazards or []}
    ids |= {c.get("hazard_id") for c in controls or []}
    ids = {i for i in ids if isinstance(i, str) and i}
    labels: dict[str, tuple[str, str]] = {}
    if ids:
        rows = await db.execute(
            select(HiraHazard.id, HiraHazard.name, HiraHazard.category).where(HiraHazard.id.in_(ids))
        )
        labels = {r.id: (r.name, r.category) for r in rows}
        # Controls may point at a team-added entry hazard instead of the library row.
        rest = ids - labels.keys()
        if rest:
            rows = await db.execute(
                select(HiraEntryHazard.id, HiraHazard.name, HiraHazard.category)
                .join(HiraHazard, HiraHazard.id == HiraEntryHazard.hazardId)
                .where(HiraEntryHazard.id.in_(rest))
            )
            labels.update({r.id: (r.name, r.category) for r in rows})

    def labelled(item: dict[str, Any], id_key: str) -> dict[str, Any]:
        name, category = labels.get(item.get(id_key), ("Not found in the hazard library", None))
        return {"hazard": name, "hazard_category": category, **item}

    out = dict(suggestion)
    if hazards is not None:
        best: dict[Any, dict[str, Any]] = {}
        for h in hazards:
            key = h.get("hazard_master_id") or id(h)  # only real ids are merged
            kept = best.get(key)
            if kept is None or (h.get("relevance_confidence") or 0) > (kept.get("relevance_confidence") or 0):
                best[key] = h
        out["suggested_hazards"] = [labelled(h, "hazard_master_id") for h in best.values()]
    if controls is not None:
        out["control_suggestions"] = [labelled(c, "hazard_id") for c in controls]
    return out


async def _detect_hallucinations(
    db: AsyncSession, text: str
) -> list[dict[str, Any]] | None:
    """Scan the agent's final output for record-ID patterns and verify
    each against the DB. Returns a list of unverified references, or
    None if everything checks out.

    This is a coarse defence — the agent could still hallucinate facts
    that don't involve record IDs. But inventing INC-FAKE-0001-style
    references is the failure mode most damaging to trust, so we
    catch it explicitly.
    """
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _RECORD_TOKEN.finditer(text or ""):
        value = match.group(1)
        prefix = _classify_record_token(value)
        if prefix is None or value in seen:
            continue
        seen.add(value)
        type_label, columns = _RECORD_PREFIXES[prefix]
        exists = False
        for model, column in columns:
            stmt = select(model.id).where(getattr(model, column) == value).limit(1)
            if (await db.execute(stmt)).scalar_one_or_none() is not None:
                exists = True
                break
        if not exists:
            findings.append(
                {
                    "type": "INVENTED_RECORD_ID",
                    "recordType": type_label,
                    "value": value,
                    "context": "Looks like a record number but no row with this number exists",
                }
            )
    return findings or None


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Approximate USD cost from token counts using the static price
    table. Returns 0.0 when the model isn't priced (don't block on
    missing pricing)."""
    in_price, out_price = _MODEL_PRICING.get(model, _DEFAULT_PRICE)
    return round(
        (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price,
        6,
    )


async def _mark_errored(
    db: AsyncSession,
    invocation: AgentInvocation,
    error_type: str,
    detail: str,
) -> None:
    """Land an invocation in ERRORED status with the failure cause.

    This has to work even when the session that got us here is broken —
    which is the common case, since a DB error is one of the things that
    lands us in ERRORED. So: capture the id first, roll the session back,
    then write through a Core UPDATE rather than the ORM. (After a
    rollback the ORM instance is expired, and touching an expired
    attribute on an async session raises MissingGreenlet.)
    """
    invocation_id = invocation.id  # read BEFORE the rollback expires it

    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass  # already rolled back / connection gone — the UPDATE will tell us

    try:
        await db.execute(
            update(AgentInvocation)
            .where(AgentInvocation.id == invocation_id)
            .values(
                status="ERRORED",
                hadError=True,
                errorType=error_type,
                errorDetails=detail,
            )
        )
        await db.commit()
        # Repopulate the expired instance so _result_from() can read it.
        await db.refresh(invocation)
    except Exception as e:  # noqa: BLE001
        # Last resort: a completely unusable session. Use a fresh one so the
        # row still reaches a terminal state — leaving it RUNNING is the one
        # outcome we must never allow.
        print(
            f"[agents] _mark_errored fell back to a new session for "
            f"{invocation_id}: {e}",
            file=sys.stderr,
        )
        async with AsyncSessionLocal() as rescue:
            await rescue.execute(
                update(AgentInvocation)
                .where(AgentInvocation.id == invocation_id)
                .values(
                    status="ERRORED",
                    hadError=True,
                    errorType=error_type,
                    errorDetails=detail,
                )
            )
            await rescue.commit()


def _result_from(invocation: AgentInvocation) -> InvocationResult:
    """Build the InvocationResult dataclass from the persisted row.
    Used by both successful and errored returns.

    Note: tool_call_count is left at 0 here. Touching invocation.toolCalls
    on an async session triggers a sync greenlet load (MissingGreenlet)
    unless the relationship was explicitly eager-loaded. Callers who
    need the count should query AgentToolCall directly.
    """
    try:
        return InvocationResult(
            invocation_id=invocation.id,
            invocation_number=invocation.invocationNumber,
            status=invocation.status,
            suggestion=invocation.agentSuggestion,
            reasoning=invocation.agentReasoning,
            confidence=invocation.agentConfidence,
            tool_call_count=0,
            cost_usd=invocation.totalCostUsd,
            latency_ms=invocation.latencyMs,
            hallucination_flagged=invocation.hallucinationFlagged,
        )
    except Exception:  # noqa: BLE001
        # The instance is expired or detached because its session died, and
        # re-reading an attribute would need a DB round-trip we can't make
        # here. The row itself was already written; read what's still in
        # __dict__ (never triggers a load) and report the rest as unknown.
        d = invocation.__dict__
        return InvocationResult(
            invocation_id=d.get("id", ""),
            invocation_number=d.get("invocationNumber", ""),
            status=d.get("status", "ERRORED"),
            suggestion=None,
            reasoning=None,
            confidence=None,
            tool_call_count=0,
            cost_usd=None,
            latency_ms=None,
            hallucination_flagged=False,
        )
