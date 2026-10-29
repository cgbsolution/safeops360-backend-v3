"""Incident AI assist: summary draft + root-cause suggestion (no DB, LLM stubbed)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.services.incident_ai as ai


def _incident(**kw):
    base = dict(
        id="inc-1", type=SimpleNamespace(value="LTI"), location="Press shop", specificLocation="Line 2",
        initialDescription="", description="Operator's hand caught in the press while clearing a jam.",
        immediateAction="Machine stopped", activityBeingPerformed="Clearing a jam", immediateCause="Guard open",
        immediateCauses=None, underlyingCauses=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def llm(monkeypatch):
    state = {"reply": None, "prompts": [], "sim_kwargs": None}

    async def fake_complete_json(*, system, user, max_tokens=1024, model=None):
        state["prompts"].append(user)
        return state["reply"]

    async def fake_similar(db, incident, **kwargs):
        state["sim_kwargs"] = kwargs
        return [
            {"incidentId": "a", "number": "INC-1", "type": "LTI", "rootCauses": ["Guard bypass"]},
            {"incidentId": "b", "number": "INC-2", "type": "MTC", "rootCauses": []},
        ]

    monkeypatch.setattr(ai, "complete_json", fake_complete_json)
    monkeypatch.setattr(ai, "is_configured", lambda: True)
    monkeypatch.setattr(ai.incident_similarity, "similar_incidents", fake_similar)
    return state


@pytest.mark.parametrize("reply", [None, ["x"], {"summary": ["x"]}, {"summary": 3}, {"summary": "   "}])
def test_summary_garbage_is_fail_soft(llm, reply) -> None:
    llm["reply"] = reply
    assert asyncio.run(ai.draft_summary(None, _incident())) is None


def test_summary_trims(llm) -> None:
    llm["reply"] = {"summary": "  A cut at the press.  "}
    assert asyncio.run(ai.draft_summary(None, _incident())) == {"summary": "A cut at the press."}


def test_root_cause_prompt_has_incident_facts_and_scope_passes_through(llm) -> None:
    llm["reply"] = {"rootCause": "Guard interlock bypassed."}
    out = asyncio.run(ai.suggest_root_cause(None, _incident(), plant_ids=["p1"], reporter_id="u1"))
    assert out == {"text": "Guard interlock bypassed.", "confidence": 50, "basedOnIncidentIds": ["a", "b"]}
    assert "hand caught in the press" in llm["prompts"][-1]
    assert "(INC-1) Guard bypass" in llm["prompts"][-1]
    assert llm["sim_kwargs"]["plant_ids"] == ["p1"] and llm["sim_kwargs"]["reporter_id"] == "u1"


@pytest.mark.parametrize("reply", [None, {"rootCause": 7}, {"rootCause": ""}])
def test_root_cause_garbage_is_fail_soft(llm, reply) -> None:
    llm["reply"] = reply
    assert asyncio.run(ai.suggest_root_cause(None, _incident())) is None
