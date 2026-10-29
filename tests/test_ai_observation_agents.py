"""Observation / near-miss AI: triage, lessons, and their persistence paths.

The model is simulated (complete_json monkeypatched) and the DB is an
in-memory fake, so nothing here calls Anthropic or touches Postgres.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from app.services.ai.agents import lessons as lessons_mod
from app.services.ai.agents import triage as triage_mod

OBS_PAYLOAD = {
    "type": "UNSAFE_CONDITION",
    "category": "ELECTRICAL",
    "severity": "HIGH",
    "description": "Exposed live wires near pooled water",
    "immediateAction": "Area barricaded",
}


def _fake_model(monkeypatch: pytest.MonkeyPatch, module: Any, reply: Any, delay: float = 0.0) -> None:
    async def fake_complete_json(**_: Any) -> Any:
        if delay:
            await asyncio.sleep(delay)
        return reply

    monkeypatch.setattr(module, "complete_json", fake_complete_json)
    monkeypatch.setattr(module, "is_configured", lambda: True)


# ─── triage ────────────────────────────────────────────────────────────


def test_triage_keeps_only_valid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_model(monkeypatch, triage_mod, {
        "suggestedSeverity": "catastrophic",
        "suggestedCategory": "Electrical",
        "priority": "asap",
        "rationale": "Live conductors near water.",
        "firstResponse": "Isolate the supply",
    })
    out = asyncio.run(triage_mod.run_triage(observation=OBS_PAYLOAD))
    assert out["suggestedSeverity"] is None          # off-list dropped, not echoed
    assert out["suggestedCategory"] == "ELECTRICAL"  # normalised
    assert out["priority"] == "NORMAL"
    assert out["firstResponse"] == ["Isolate the supply"]  # a bare string is one item


def test_triage_valid_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_model(monkeypatch, triage_mod, {
        "suggestedSeverity": "CRITICAL", "suggestedCategory": "ELECTRICAL", "priority": "URGENT",
        "rationale": "r", "firstResponse": ["a", "b"],
    })
    out = asyncio.run(triage_mod.run_triage(observation=OBS_PAYLOAD))
    assert (out["suggestedSeverity"], out["priority"], out["firstResponse"]) == ("CRITICAL", "URGENT", ["a", "b"])
    assert not out.get("skipped")


@pytest.mark.parametrize("reply", [None, ["not", "an", "object"], "text"])
def test_triage_bad_reply_is_skipped_not_raised(monkeypatch: pytest.MonkeyPatch, reply: Any) -> None:
    _fake_model(monkeypatch, triage_mod, reply)
    out = asyncio.run(triage_mod.run_triage(observation=OBS_PAYLOAD))
    assert out["skipped"] is True


def test_triage_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_model(monkeypatch, triage_mod, {"suggestedSeverity": "LOW"}, delay=1.0)
    monkeypatch.setattr(triage_mod, "TRIAGE_TIMEOUT_S", 0.05)
    out = asyncio.run(triage_mod.run_triage(observation=OBS_PAYLOAD))
    assert out == {"agentId": "TriageAgent", "skipped": True, "reason": "model call timed out"}


class _FakeSession:
    """Just enough of AsyncSession for the background persistence paths."""

    store: dict[str, Any] = {}

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, _model: Any, key: str) -> Any:
        return self.store.get(key)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def flush(self) -> None:
        return None


def test_background_triage_prepends_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    obs = types.SimpleNamespace(
        type="UNSAFE_CONDITION", category="ELECTRICAL", severity="HIGH",
        description="d", immediateAction=None,
        closureTriggers=[{"ruleId": "older"}],
    )
    _FakeSession.store = {"obs-1": obs}
    import app.core.db as appdb

    monkeypatch.setattr(appdb, "AsyncSessionLocal", _FakeSession)
    _fake_model(monkeypatch, triage_mod, {
        "suggestedSeverity": "HIGH", "suggestedCategory": "ELECTRICAL", "priority": "HIGH",
        "rationale": "why", "firstResponse": ["x", "y"],
    })
    asyncio.run(triage_mod.triage_observation_in_background("obs-1"))
    first = obs.closureTriggers[0]
    assert first["ruleId"] == "rule_triage_on_submit" and first["fired"] is True
    assert first["data"]["suggestedSeverity"] == "HIGH"
    assert obs.closureTriggers[1] == {"ruleId": "older"}


def test_background_triage_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.db as appdb

    class Boom(_FakeSession):
        async def get(self, *_: Any) -> Any:
            raise RuntimeError("db down")

    monkeypatch.setattr(appdb, "AsyncSessionLocal", Boom)
    asyncio.run(triage_mod.triage_observation_in_background("obs-x"))  # must not raise


def test_create_observation_hands_triage_to_background() -> None:
    import inspect

    from app.routers import observations

    src = inspect.getsource(observations.create_observation)
    assert "run_triage(" not in src  # no model call inside the request
    params = list(inspect.signature(observations.create_observation).parameters)
    # capture.py calls create_observation(payload, user, db) positionally.
    assert params[:3] == ["payload", "user", "db"]
    # FastAPI runs BackgroundTasks before get_db's commit, so a triage queued
    # there can never see the new row (live failure 2026-10-25, SO-2026-NW-0254).
    assert "background_tasks" not in src and "schedule_triage(obs.id)" in src


def test_background_triage_waits_for_the_create_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """schedule_triage starts while the create request is still in flight; the
    row only becomes visible once that request commits."""
    obs = types.SimpleNamespace(
        type="UNSAFE_ACT", category="PPE", severity="LOW",
        description="d", immediateAction=None, closureTriggers=None,
    )
    reads = {"n": 0}

    class NotYetCommitted(_FakeSession):
        async def get(self, _model: Any, key: str) -> Any:
            reads["n"] += 1
            return obs if reads["n"] > 3 else None  # visible from the 4th read

    import app.core.db as appdb

    monkeypatch.setattr(appdb, "AsyncSessionLocal", NotYetCommitted)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(triage_mod.asyncio, "sleep", lambda _s: real_sleep(0))
    _fake_model(monkeypatch, triage_mod, {"suggestedSeverity": "LOW", "suggestedCategory": "PPE"})
    asyncio.run(triage_mod.triage_observation_in_background("obs-late"))
    assert obs.closureTriggers and obs.closureTriggers[0]["ruleId"] == "rule_triage_on_submit"


# ─── lessons ───────────────────────────────────────────────────────────


class _LessonsDb:
    def __init__(self, obs: Any) -> None:
        self.obs = obs

    async def get(self, _model: Any, _key: str) -> Any:
        return self.obs

    async def execute(self, _stmt: Any) -> Any:
        return types.SimpleNamespace(scalar_one_or_none=lambda: None)


def _closed_obs() -> Any:
    return types.SimpleNamespace(
        number="OBS-1", type="UNSAFE_ACT", category="PPE", severity="LOW",
        description="No gloves", immediateAction=None, closingRemark="Gloves issued",
    )


def test_lessons_normalises_lists_and_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_model(monkeypatch, lessons_mod, {
        "lesson": "Wear gloves.", "audience": "ALL_PLANT_OPERATIONS",
        "actions": None, "tags": ["PPE"], "confidence": "very high",
    })
    out = asyncio.run(lessons_mod.run_lessons_distribution(_LessonsDb(_closed_obs()), observation_id="o"))
    assert out["audience"] == ["ALL_PLANT_OPERATIONS"]  # not split into characters
    assert out["actions"] == []
    assert out["confidence"] == "MEDIUM"


@pytest.mark.parametrize("reply", [None, [1, 2], {"lesson": ""}])
def test_lessons_bad_reply_is_skipped(monkeypatch: pytest.MonkeyPatch, reply: Any) -> None:
    _fake_model(monkeypatch, lessons_mod, reply)
    out = asyncio.run(lessons_mod.run_lessons_distribution(_LessonsDb(_closed_obs()), observation_id="o"))
    assert out["skipped"] is True


def test_post_closure_inline_appends_event(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import post_closure_rules as pcr

    obs = _closed_obs()
    obs.closureTriggers = [{"ruleId": "rule_triage_on_submit"}]

    async def fake_lessons(_db: Any, *, observation_id: str) -> dict[str, Any]:
        return {"agentId": "LessonsDistributionAgent", "lesson": "L", "audience": ["A"], "actions": ["x"]}

    monkeypatch.setattr(pcr, "run_lessons_distribution", fake_lessons)
    events = asyncio.run(pcr.run_post_closure_rules(_LessonsDb(obs), observation_id="o", inline=True))
    assert events and events[0]["fired"] is True
    assert [e["ruleId"] for e in obs.closureTriggers] == ["rule_triage_on_submit", "rule_lessons_distribution"]


def test_post_closure_default_does_not_call_model_in_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import post_closure_rules as pcr

    called: list[str] = []

    async def fake_lessons(*_: Any, **__: Any) -> None:
        called.append("inline")

    async def fake_background(observation_id: str) -> None:
        called.append(f"background:{observation_id}")

    monkeypatch.setattr(pcr, "run_lessons_distribution", fake_lessons)
    monkeypatch.setattr(pcr, "_lessons_in_background", fake_background)

    async def go() -> list[dict[str, Any]]:
        events = await pcr.run_post_closure_rules(_LessonsDb(_closed_obs()), observation_id="o-9")
        await asyncio.sleep(0)  # let the scheduled task run
        return events

    assert asyncio.run(go()) == []
    assert called == ["background:o-9"]
