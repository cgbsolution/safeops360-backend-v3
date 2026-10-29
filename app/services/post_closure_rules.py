"""Post-closure rules engine for Safety Observation (Dimension 4 of the
brief). Ports the Node `src/lib/observation/post-closure-rules.ts` shape
to Python. Each rule is independent — failures are caught and logged, so
one bad rule can't block another from firing. Audit entries are appended
to Observation.closureTriggers (JSONB column).

Currently wired:
  • LessonsDistributionAgent (Anthropic) — generates a sharable lesson
    + audience + follow-up actions on every closure.

Stubs for the remaining rules from the brief (focused inspection on
repeats, contractor score, PPE trend, permit flag, behavioural coaching,
systemic CAPA, analytics refresh, anomaly feed) live in the Node file
for now and can be ported here as the modules they touch land in Python.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.services.ai.agents.lessons import run_lessons_distribution


def _lesson_event(lesson: dict[str, Any] | None, error: str | None = None) -> dict[str, Any] | None:
    if error is not None:
        return {
            "ruleId": "rule_lessons_distribution",
            "ruleName": "Lessons Distribution (AI)",
            "fired": False,
            "error": error,
        }
    if lesson is None:
        return None
    return {
        "ruleId": "rule_lessons_distribution",
        "ruleName": "Lessons Distribution (AI)",
        "fired": not lesson.get("skipped", False),
        "reason": (
            f"Lesson generated, {len(lesson.get('audience') or [])} audience, "
            f"{len(lesson.get('actions') or [])} actions"
            if not lesson.get("skipped")
            else lesson.get("reason") or "skipped"
        ),
        "spawnedRecordType": "AI_LESSON",
        "data": lesson,
    }


async def _append_events(db: AsyncSession, observation_id: str, events: list[dict[str, Any]]) -> None:
    obs = await db.get(Observation, observation_id)
    if obs is None or not events:
        return
    existing = obs.closureTriggers if isinstance(obs.closureTriggers, list) else []
    # Reassign (never mutate in place) so the JSON column is marked dirty.
    obs.closureTriggers = [*existing, *events]
    await db.flush()


async def _lessons_event(db: AsyncSession, observation_id: str) -> dict[str, Any] | None:
    try:
        lesson = await run_lessons_distribution(db, observation_id=observation_id)
        return _lesson_event(lesson)
    except Exception as e:  # noqa: BLE001
        print(f"[post-closure] lessons agent crashed: {e}", file=sys.stderr)
        return _lesson_event(None, error=str(e))


async def _lessons_in_background(observation_id: str) -> None:
    """Run the lessons agent after the closing transaction has committed.

    The agent used to run inline, inside the workflow approval's open
    transaction — a model call of up to 90s (+ one retry) held the DB
    transaction and outlived the Next.js proxy's 26s deadline, so the approver
    saw a failure for a closure that had in fact gone through. Owns its
    sessions; never raises."""
    from app.core.db import AsyncSessionLocal
    from app.models.observation import ObservationStatus

    try:
        # Wait (briefly) for the closing transaction to become visible, so the
        # lesson is built from the final record (closing remark, history).
        for attempt in range(6):
            async with AsyncSessionLocal() as db:
                obs = await db.get(Observation, observation_id)
                if obs is not None and obs.status == ObservationStatus.CLOSED:
                    break
            await asyncio.sleep(1.0 + attempt)
        else:
            print(f"[post-closure] {observation_id} never showed CLOSED; lesson skipped", file=sys.stderr)
            return

        async with AsyncSessionLocal() as db:
            event = await _lessons_event(db, observation_id)
            await db.rollback()  # the agent only reads
        if event is None:
            return
        async with AsyncSessionLocal() as db:
            await _append_events(db, observation_id, [event])
            await db.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[post-closure] background lessons failed for {observation_id}: {e}", file=sys.stderr)


# Strong references so fire-and-forget tasks are not garbage-collected.
_PENDING: set[asyncio.Task[None]] = set()


async def run_post_closure_rules(
    db: AsyncSession, *, observation_id: str, inline: bool = False
) -> list[dict[str, Any]]:
    """Run all post-closure rules and persist the audit.

    By default the AI rule is scheduled to run after the caller's
    transaction commits (see _lessons_in_background) and this returns [].
    `inline=True` runs it now on `db` and returns the events — for tests and
    manual repair tools."""
    obs = await db.get(Observation, observation_id)
    if obs is None:
        return []

    if not inline:
        task = asyncio.get_running_loop().create_task(_lessons_in_background(observation_id))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
        return []

    events: list[dict[str, Any]] = []
    event = await _lessons_event(db, observation_id)
    if event is not None:
        events.append(event)
    # Persist all events onto the observation. Append to existing
    # closureTriggers if any (so re-runs are visible side-by-side).
    if events:
        try:
            await _append_events(db, observation_id, events)
        except Exception as e:  # noqa: BLE001
            print(f"[post-closure] persist failed: {e}", file=sys.stderr)
    return events
