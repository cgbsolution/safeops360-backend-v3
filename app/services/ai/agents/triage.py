"""TriageAgent — runs once per observation submission.

Reads the just-created observation and asks Claude to suggest:
  • severity (LOW | MEDIUM | HIGH | CRITICAL) — sanity check on the
    observer's pick
  • category — sanity check
  • priority (NORMAL | HIGH | URGENT)
  • two short "first-response" prompts the responsible person should do
    immediately

Fail-open: never blocks creation. The create endpoint calls
schedule_triage(), which runs triage_observation_in_background() as its own
task once the create has committed: running the
model inside the request held the DB transaction open for up to the client
timeout (90s + one retry) and outlived the Next.js proxy's 26s deadline, so a
slow model showed the user a failed submit for an observation that had in
fact been saved — and a retry created a duplicate.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.services.ai.anthropic_client import complete_json, is_configured

AGENT_ID = "TriageAgent"
RULE_ID = "rule_triage_on_submit"

# Upper bound on one triage call. It runs in the background, so this only
# caps how long a stuck call can keep a worker coroutine busy.
TRIAGE_TIMEOUT_S = 60.0

_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
_PRIORITIES = {"NORMAL", "HIGH", "URGENT"}

_SYSTEM_PROMPT = """You are SafeOps360's TriageAgent. You receive a freshly submitted safety observation and produce a quick triage in JSON.

Output JSON ONLY with EXACTLY these keys:

{
  "suggestedSeverity": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "suggestedCategory": "<one of: PPE, HOUSEKEEPING, WORK_AT_HEIGHT, HOT_WORK, MOBILE_EQUIPMENT, ELECTRICAL, MATERIAL_HANDLING, CONFINED_SPACE, CHEMICAL_HANDLING, EMERGENCY_PREP, TOOLS_EQUIPMENT, PROCEDURES, REACTIONS_OF_PEOPLE, POSITIONS_OF_PEOPLE, OTHERS>",
  "priority": "NORMAL" | "HIGH" | "URGENT",
  "rationale": "<single sentence explaining why>",
  "firstResponse": ["<short imperative>", "<short imperative>"]   // exactly 2 items
}

Rules:
  • Be conservative on severity — only call CRITICAL when fatality / serious-injury potential is clear from the description.
  • Output JSON only. No prose around it."""


def _valid_categories() -> frozenset[str]:
    # Only labels the Postgres enum has: an accepted suggestion is written to
    # Observation.category, and OTHER / EMERGENCY / … would fail that insert.
    from app.models.observation import DB_OBSERVATION_CATEGORIES

    return DB_OBSERVATION_CATEGORIES


def _clean(result: dict[str, Any], model: str | None) -> dict[str, Any]:
    """Keep only values the UI and the DB enums can use. The model's JSON is
    untrusted: an off-list severity or category is dropped, not echoed."""
    severity = str(result.get("suggestedSeverity") or "").strip().upper()
    category = str(result.get("suggestedCategory") or "").strip().upper().replace(" ", "_")
    priority = str(result.get("priority") or "NORMAL").strip().upper()
    first = result.get("firstResponse")
    if isinstance(first, str):
        first = [first]
    if not isinstance(first, list):
        first = []
    return {
        "agentId": AGENT_ID,
        "model": model,
        "suggestedSeverity": severity if severity in _SEVERITIES else None,
        "suggestedCategory": category if category in _valid_categories() else None,
        "priority": priority if priority in _PRIORITIES else "NORMAL",
        "rationale": str(result.get("rationale") or "").strip()[:400],
        "firstResponse": [str(a).strip() for a in first if str(a).strip()][:3],
    }


async def run_triage(*, observation: dict[str, Any]) -> dict[str, Any] | None:
    """Caller passes the freshly-created Observation as a dict (the
    fields needed are: type, category, severity, description,
    immediateAction). Returns a triage dict, or a {"skipped": True} dict
    on any failure."""
    if not is_configured():
        return {"agentId": AGENT_ID, "skipped": True, "reason": "ANTHROPIC_API_KEY not set"}

    settings_model = None
    try:
        from app.core.config import get_settings

        settings_model = get_settings().anthropic_model
    except Exception:
        pass

    try:
        result = await asyncio.wait_for(
            complete_json(
                system=_SYSTEM_PROMPT,
                user=json.dumps(observation, default=str, indent=2),
                max_tokens=400,
            ),
            timeout=TRIAGE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return {"agentId": AGENT_ID, "skipped": True, "reason": "model call timed out"}
    if result is None:
        return {"agentId": AGENT_ID, "skipped": True, "reason": "model call failed"}
    if not isinstance(result, dict):
        return {"agentId": AGENT_ID, "skipped": True, "reason": "model returned an unexpected shape"}

    return _clean(result, settings_model)


def triage_entry(triage: dict[str, Any]) -> dict[str, Any]:
    """The closureTriggers audit entry for one triage result."""
    return {
        "ruleId": RULE_ID,
        "ruleName": "Triage (AI)",
        "fired": not triage.get("skipped", False),
        "reason": triage.get("rationale") or triage.get("reason") or "",
        "spawnedRecordType": "AI_TRIAGE",
        "data": triage,
    }


async def triage_observation_in_background(observation_id: str) -> None:
    """Run triage for a saved observation and prepend the result to its
    closureTriggers. Owns its session; never raises (BackgroundTasks has no
    observer)."""
    try:
        from app.core.db import AsyncSessionLocal
        from app.models.observation import Observation

        obs = None
        # Started while the create request is still in flight: wait for its
        # commit to become visible rather than silently dropping the triage.
        for attempt in range(6):
            async with AsyncSessionLocal() as db:
                obs = await db.get(Observation, observation_id)
                if obs is not None:
                    payload = {
                        "type": getattr(obs.type, "value", obs.type),
                        "category": getattr(obs.category, "value", obs.category),
                        "severity": getattr(obs.severity, "value", obs.severity),
                        "description": obs.description,
                        "immediateAction": obs.immediateAction,
                    }
                    break
            await asyncio.sleep(1.0 * (attempt + 1))
        if obs is None:
            print(f"[triage] observation {observation_id} not found", file=sys.stderr)
            return

        triage = await run_triage(observation=payload)
        if triage is None:
            return

        async with AsyncSessionLocal() as db:
            obs = await db.get(Observation, observation_id)
            if obs is None:
                return
            existing = obs.closureTriggers if isinstance(obs.closureTriggers, list) else []
            # Reassign (never mutate in place) so the JSON column is marked dirty.
            obs.closureTriggers = [triage_entry(triage), *existing]
            await db.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[triage] background triage failed for {observation_id}: {e}", file=sys.stderr)


# Strong references to fire-and-forget tasks so they are not garbage-collected
# mid-run (asyncio keeps only weak references to tasks).
_PENDING: set[asyncio.Task[None]] = set()


def schedule_triage(observation_id: str) -> None:
    """Fire-and-forget triage for callers without FastAPI BackgroundTasks
    (capture conversion calls create_observation directly). The loader's
    retry covers the caller committing a moment later."""
    task = asyncio.get_running_loop().create_task(triage_observation_in_background(observation_id))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
