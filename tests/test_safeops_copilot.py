"""SafeOps Copilot — offline tests (no DB, no network, no LLM spend).

Covers the PTW draft rules, patch validation, the streaming tool loop (with a
fake Anthropic stream) and the request guards.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.services.copilot import ptw_draft, runtime, tools


# ─── Draft patch validation ─────────────────────────────────────────────


def test_patch_rejects_unknown_fields_and_checklist_answers():
    patch, err = ptw_draft.parse_patch({"type": "HOT_WORK", "hazards": [{"answers": []}]})
    assert patch is None and "hazards" in err


def test_patch_rejects_bad_ppe_and_time_format():
    _, err = ptw_draft.parse_patch({"requiredPpe": ["helmet", "jetpack"]})
    assert "jetpack" in err
    _, err = ptw_draft.parse_patch({"validFrom": "tomorrow 9am"})
    assert "validFrom" in err


def test_patch_null_clears_and_lists_validated():
    patch, err = ptw_draft.parse_patch({"receiverId": None, "workCrew": [{"userId": "u1"}]})
    assert err is None
    assert patch == {"receiverId": None, "workCrew": [{"userId": "u1", "role": "WORKER"}]}


def test_empty_patch_is_an_error():
    patch, err = ptw_draft.parse_patch({})
    assert patch is None and "Empty" in err


def test_type_change_applies_form_defaults_unless_overridden():
    merged = ptw_draft.apply_patch({"type": "GENERAL_COLD"}, {"type": "CONFINED_SPACE"})
    assert merged["requiredPpe"] == ptw_draft.PPE_DEFAULTS["CONFINED_SPACE"]
    assert merged["gasTestPlan"]["refreshFrequencyMinutes"] == 120
    merged = ptw_draft.apply_patch({}, {"type": "HOT_WORK", "requiredPpe": ["helmet"]})
    assert merged["requiredPpe"] == ["helmet"]


def test_plant_change_resets_area_and_department():
    merged = ptw_draft.apply_patch({"plantId": "p1", "areaId": "a1", "departmentId": "d1"}, {"plantId": "p2"})
    assert merged["areaId"] is None and merged["departmentId"] is None


# ─── Rule check (mirrors POST /api/ptw) ─────────────────────────────────


async def test_hot_work_needs_fire_watch_gas_test_and_respects_cap():
    draft = {
        "type": "HOT_WORK",
        "validFrom": "2026-10-27T08:00",
        "validTo": "2026-10-28T12:00",  # 28h > 24h cap
        "plantId": "p1",
        "areaId": "a1",
        "scopeOfWork": "Weld repair on conveyor chute liner",
        "requiredPpe": ["helmet"],
    }
    report = await ptw_draft.check_draft(None, draft, user_id="me", now_local="2026-10-26T15:00")
    assert "FIRE_WATCH" in report["requiredControls"] and "GAS_TEST" in report["requiredControls"]
    assert any("cap is 24h" in s for s in report["sections"]["job"])
    assert any("Fire watch" in s for s in report["sections"]["people"])
    assert any("Gas test plan" in s for s in report["sections"]["controls"])
    assert report["readyToSubmit"] is False


async def test_extra_hazard_tightens_cap_and_adds_controls():
    draft = {"type": "WORK_AT_HEIGHT", "extraHazards": ["HOT_WORK"],
             "validFrom": "2026-10-27T08:00", "validTo": "2026-10-28T20:00"}
    report = await ptw_draft.check_draft(None, draft, user_id="me")
    assert report["validityCapHours"] == 24
    assert {"RESCUE_PLAN", "FIRE_WATCH", "GAS_TEST"} <= set(report["requiredControls"])


async def test_standby_on_crew_and_self_issuer_flagged():
    draft = {"type": "CONFINED_SPACE", "issuerId": "me", "standbyPersonId": "u2",
             "workCrew": [{"userId": "u2", "role": "WORKER"}]}
    # receiver unset + crew set would call the competency service; stub it out.
    report = await ptw_draft.check_draft(_NoCompetencyDB(), draft, user_id="me")
    people = " ".join(report["sections"]["people"])
    assert "cannot also be the issuer" in people
    assert "cannot also be on the work crew" in people


async def test_checklist_is_reported_never_answered():
    draft = {"type": "GENERAL_COLD", "checklist": {"GENERAL": {"blocker": "General Work: 2 items unanswered"}}}
    report = await ptw_draft.check_draft(None, draft, user_id="me")
    assert report["sections"]["checklist"] == ["General Work: 2 items unanswered"]
    assert "never answers" in report["note"]


class _NoCompetencyDB:
    """check_draft only touches the DB through the competency service."""


@pytest.fixture(autouse=True)
def _stub_competency(monkeypatch):
    import app.services.competency as comp

    async def ok(db, user_id, code):
        return SimpleNamespace(ok=True, blockers=[])

    monkeypatch.setattr(comp, "check_competency_for_permit_type", ok)


def test_reference_lists_caps_controls_and_defaults():
    ref = ptw_draft.reference_for("CONFINED_SPACE", [])
    this = ref["forThisPermit"]
    assert this["validityCapHours"] == 24
    assert this["requiredControls"] == ["GAS_TEST", "RESCUE_PLAN", "STANDBY"]
    assert this["defaultGasTest"]["parametersToTest"][0]["parameter"] == "O2"
    assert "helmet" in ref["ppeCatalog"]


# ─── Request guards ─────────────────────────────────────────────────────


def test_history_guards():
    assert runtime.validate_history([]) == []
    with pytest.raises(HTTPException):
        runtime.validate_history([{"role": "assistant", "content": "hi"}])
    with pytest.raises(HTTPException):
        runtime.validate_history([{"role": "system", "content": "x"}])
    with pytest.raises(HTTPException) as e:
        runtime.validate_history([{"role": "user", "content": "x" * 1_000_000}])
    assert e.value.status_code == 413


def test_strip_thinking_keeps_text_and_tool_use():
    hist = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "text", "text": "a"},
            {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
        ]},
    ]
    out = runtime._strip_thinking(hist)
    assert [b["type"] for b in out[1]["content"]] == ["text", "tool_use"]


def test_request_options_per_model(monkeypatch):
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(copilot_effort="medium", copilot_refusal_fallback=True))
    body, headers = runtime._request_options("claude-opus-5-5")
    assert body["output_config"] == {"effort": "medium"} and body["fallbacks"] == "default"
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body, headers = runtime._request_options("claude-haiku-4-5")
    assert body == {} and headers == {}


def test_every_tool_is_strict_object_schema():
    for t in tools.TOOL_DEFINITIONS:
        assert t["input_schema"]["type"] == "object"
        assert t["input_schema"]["additionalProperties"] is False
    assert all(t["eager_input_streaming"] for t in runtime._TOOLS_FOR_API)


def test_user_message_carries_page_context_and_scrubs_contact_details():
    msg = runtime.build_user_message(
        text="call me on 9876543210 or a.b@x.com", page={"path": "/ptw/new", "module": "PTW"},
        draft={"type": "HOT_WORK"}, now_local="2026-10-26T15:00", tz_offset=-330, user_plant_id="p1",
    )
    ctx = json.loads(msg["content"][0]["text"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert ctx["ptwFormOpen"] is True and ctx["ptwDraft"]["type"] == "HOT_WORK"
    assert "9876543210" not in msg["content"][1]["text"] and "a.b@x.com" not in msg["content"][1]["text"]


# ─── Streaming loop with a fake Anthropic client ────────────────────────


class _Block(SimpleNamespace):
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class _Final(SimpleNamespace):
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"stop_reason": self.stop_reason, "content": [b.model_dump() for b in self.content]}


class _Stream:
    def __init__(self, final: _Final):
        self.final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for b in self.final.content:
                if b.type == "text":
                    yield SimpleNamespace(type="text", text=b.text)
                elif b.type == "tool_use":
                    yield SimpleNamespace(type="content_block_start", content_block=b)
        return gen()

    async def get_final_message(self):
        return self.final


class _FakeClient:
    def __init__(self, finals: list[_Final], calls: list[dict[str, Any]]):
        self._finals = finals
        self.calls = calls
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self._finals.pop(0))


def _usage():
    return SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)


class _DummySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def rollback(self):
        pass


def _make_turn(monkeypatch, finals, *, draft):
    import anthropic

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: _FakeClient(finals, calls))
    settings = SimpleNamespace(
        anthropic_api_key="test-key", copilot_max_iterations=6, copilot_max_tokens=4000,
        copilot_effort="medium", copilot_refusal_fallback=True,
    )
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(tools, "AsyncSessionLocal", lambda: _DummySession())

    async def no_refs(db, merged, patch, *, user_id):
        return []

    monkeypatch.setattr(ptw_draft, "validate_references", no_refs)
    audit: dict[str, Any] = {}

    async def open_row(self):
        return "row-1", "ACOP-2026-000001"

    async def close_row(self, row_id, *, started, error, last_response):
        audit.update(row=row_id, error=error, tools=list(self.tool_records), patches=list(self.patches))

    monkeypatch.setattr(runtime.CopilotTurn, "_open_audit_row", open_row)
    monkeypatch.setattr(runtime.CopilotTurn, "_close_audit_row", close_row)
    turn = runtime.CopilotTurn(
        user_id="me", user_plant_id="p1",
        agent=SimpleNamespace(id="agent-1", primaryModelId="claude-opus-5-5", currentAuthorityLevel="L1"),
        prompt=SimpleNamespace(id="prompt-1", systemPrompt="SYSTEM"),
        conversation_id="conv-12345678", history=[], user_text="Weld the chute liner at kiln 2 tomorrow",
        page={"path": "/ptw/new", "module": "PTW"}, draft=draft, now_local="2026-10-26T15:00", tz_offset=-330,
    )
    return turn, calls, audit


async def _drain(turn) -> list[tuple[str, dict[str, Any]]]:
    await turn.run()
    events = []
    while not turn.queue.empty():
        item = turn.queue.get_nowait()
        if item is not None:
            events.append(item)
    return events


async def test_turn_fills_draft_and_returns_append_only_history(monkeypatch):
    finals = [
        _Final(stop_reason="tool_use", usage=_usage(), content=[
            _Block(type="thinking", thinking="", signature="sig-1"),
            _Block(type="text", text="Setting this up as hot work."),
            _Block(type="tool_use", id="tu_1", name="update_ptw_draft", input={
                "type": "HOT_WORK", "scopeOfWork": "Weld repair of chute liner plates at kiln 2 inlet",
                "validFrom": "2026-10-27T09:00", "validTo": "2026-10-27T17:00"}),
        ]),
        _Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text=" Done.")]),
    ]
    turn, calls, audit = _make_turn(monkeypatch, finals, draft={})
    events = await _drain(turn)
    kinds = [e for e, _ in events]

    assert "".join(d["delta"] for e, d in events if e == "text") == "Setting this up as hot work. Done."
    patch_ev = next(d for e, d in events if e == "draft_patch")
    assert patch_ev["patch"]["type"] == "HOT_WORK"
    assert "FIRE_WATCH" in patch_ev["check"]["requiredControls"]
    assert [d["status"] for e, d in events if e == "tool"] == ["pending", "running", "done"]
    assert kinds[-1] == "done"

    done = events[-1][1]
    roles = [m["role"] for m in done["appendMessages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    # thinking blocks are kept verbatim for the next turn
    assert done["appendMessages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}
    result = done["appendMessages"][2]["content"][0]
    assert result["tool_use_id"] == "tu_1" and "is_error" not in result
    assert json.loads(result["content"])["applied"] == ["scopeOfWork", "type", "validFrom", "validTo"]
    # second request = first request's messages + appended turns (append-only)
    assert calls[1]["messages"][: len(calls[0]["messages"])] == calls[0]["messages"]
    assert calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert calls[0]["extra_body"]["fallbacks"] == "default"
    assert done["usage"]["input"] == 2000 and done["usage"]["costUsd"] > 0
    assert audit["error"] is None and audit["tools"][0]["toolName"] == "update_ptw_draft"


async def test_draft_tool_without_open_form_is_a_recoverable_error(monkeypatch):
    finals = [
        _Final(stop_reason="tool_use", usage=_usage(), content=[
            _Block(type="tool_use", id="tu_1", name="update_ptw_draft", input={"type": "HOT_WORK"}),
        ]),
        _Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text="Open the form first.")]),
    ]
    turn, _, audit = _make_turn(monkeypatch, finals, draft=None)
    events = await _drain(turn)
    result = events[-1][1]["appendMessages"][2]["content"][0]
    assert result["is_error"] is True and "/ptw/new" in json.loads(result["content"])["error"]
    assert audit["tools"][0]["hadError"] is True


async def test_truncated_tool_input_is_never_executed(monkeypatch):
    finals = [
        _Final(stop_reason="max_tokens", usage=_usage(), content=[
            _Block(type="tool_use", id="tu_1", name="update_ptw_draft", input={"type": "HOT"}),
        ]),
        _Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text="ok")]),
    ]
    turn, _, audit = _make_turn(monkeypatch, finals, draft={})
    events = await _drain(turn)
    assert not any(e == "draft_patch" for e, _ in events)
    result = events[-1][1]["appendMessages"][2]["content"][0]
    assert result["is_error"] is True and "limit" in result["content"]
    assert audit["tools"] == []


async def test_http_endpoint_streams_sse(monkeypatch):
    """POST /api/copilot/chat end to end through FastAPI: validation, then an
    SSE stream that ends in `done`. DB and model are faked."""
    import httpx
    from fastapi import FastAPI

    from app.core.db import get_db
    from app.core.deps import get_current_user
    from app.routers import copilot as copilot_router

    finals = [_Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text="Hello from copilot")])]
    _make_turn(monkeypatch, finals, draft=None)  # installs fake client, settings, audit stubs

    async def fake_ensure(db, user_id):
        return (SimpleNamespace(id="agent-1", isActive=True, primaryModelId="claude-opus-5-5", currentAuthorityLevel="L1", rateLimit=60),
                SimpleNamespace(id="prompt-1", systemPrompt="SYSTEM"))

    async def no_limit(db, agent, user_id):
        return None

    monkeypatch.setattr(copilot_router, "ensure_copilot_agent", fake_ensure)
    monkeypatch.setattr(copilot_router, "enforce_rate_limit", no_limit)
    monkeypatch.setattr(copilot_router, "is_configured", lambda: True)
    monkeypatch.setattr(copilot_router, "get_settings", lambda: SimpleNamespace(copilot_enabled=True))

    app = FastAPI()
    app.include_router(copilot_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="me", plantId="p1")
    app.dependency_overrides[get_db] = lambda: None

    body = {"conversationId": "conv-12345678", "message": "hi", "messages": [],
            "page": {"path": "/dashboard"}, "nowLocal": "2026-10-26T15:00", "timezoneOffsetMinutes": -330}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        bad = await client.post("/api/copilot/chat", json={**body, "messages": [{"role": "assistant", "content": "x"}]})
        assert bad.status_code == 400
        res = await client.post("/api/copilot/chat", json=body)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
    frames = [f for f in res.text.split("\n\n") if f.startswith("event:")]
    events = [(f.split("\n")[0][7:], json.loads(f.split("\n", 1)[1][6:])) for f in frames]
    assert events[0] == ("text", {"delta": "Hello from copilot"})
    assert events[-1][0] == "done" and len(events[-1][1]["appendMessages"]) == 2


async def test_api_failure_before_answer_leaves_history_untouched(monkeypatch):
    import anthropic

    turn, _, audit = _make_turn(monkeypatch, [], draft=None)

    class Boom:
        messages = None

        def __init__(self, **kw):
            self.messages = self

        def stream(self, **kw):
            import httpx

            raise anthropic.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:9/v1/messages"))

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Boom)
    events = await _drain(turn)
    assert events[0][0] == "error" and "reach the AI service" in events[0][1]["message"]
    assert events[-1][1]["appendMessages"] == []
    assert audit["error"]


# ─── 2026-10-27: draft-first fixes ──────────────────────────────────────


def test_checklist_suggestions_allow_yes_and_reasoned_na_only():
    ok, err = ptw_draft.parse_patch({"checklistSuggestions": {"HOT_WORK": [
        {"itemId": "i1", "response": "YES"}, {"itemId": "i2", "response": "NA", "remark": "No cylinders on site"}]}})
    assert err is None and len(ok["checklistSuggestions"]["HOT_WORK"]) == 2
    _, err = ptw_draft.parse_patch({"checklistSuggestions": {"HOT_WORK": [{"itemId": "i1", "response": "NO"}]}})
    assert err and "response" in err
    _, err = ptw_draft.parse_patch({"checklistSuggestions": {"HOT_WORK": [{"itemId": "i1", "response": "NA"}]}})
    assert err and "remark" in err


def test_suggestions_are_not_stored_as_draft_fields():
    merged = ptw_draft.apply_patch({"type": "HOT_WORK"}, {"scopeOfWork": "x" * 12, "checklistSuggestions": {"HOT_WORK": []}})
    assert "checklistSuggestions" not in merged and merged["scopeOfWork"]
    assert "checklistSuggestions" not in ptw_draft.normalise_client_draft({"checklistSuggestions": {}})


async def test_tbc_isolation_tags_block_submission():
    draft = {"type": "ELECTRICAL_LOTO", "isolations": [
        {"isolationType": "ELECTRICAL", "description": "Feeder motor breaker", "isolationPointTag": "TBC"}]}
    report = await ptw_draft.check_draft(None, draft, user_id="me")
    assert any("still TBC" in c for c in report["sections"]["controls"])


async def test_checklist_suggestions_validated_against_catalog(monkeypatch):
    import app.services.ptw_annexures as ann
    from app.models.permit import PermitHazardType

    items = [SimpleNamespace(id="i1", text="Area cleared of combustibles", isMandatory=True, allowsNA=False),
             SimpleNamespace(id="i2", text="Gas cylinders secured", isMandatory=True, allowsNA=True)]

    async def fake_catalog(db, hazards):
        return {PermitHazardType.HOT_WORK: items}

    monkeypatch.setattr(ann, "load_catalog", fake_catalog)
    merged = {"type": "HOT_WORK"}
    errs, summary = await ptw_draft.validate_checklist_suggestions(None, merged, {"HOT_WORK": [
        {"itemId": "i1", "response": "YES", "remark": None}, {"itemId": "i2", "response": "NA", "remark": "none"}]})
    assert errs == [] and "waiting for the user's on-site confirmation" in summary["HOT_WORK"]["blocker"]
    errs, _ = await ptw_draft.validate_checklist_suggestions(None, merged, {"HOT_WORK": [
        {"itemId": "i1", "response": "NA", "remark": "x"}, {"itemId": "zz", "response": "YES", "remark": None}]})
    assert any("cannot be answered NA" in e for e in errs) and any("unknown checklist item" in e for e in errs)
    errs, _ = await ptw_draft.validate_checklist_suggestions(None, merged, {"CONFINED_SPACE": []})
    assert any("not a hazard on this permit" in e for e in errs)


async def test_empty_final_reply_gets_one_nudge(monkeypatch):
    finals = [
        _Final(stop_reason="tool_use", usage=_usage(), content=[
            _Block(type="tool_use", id="tu_1", name="check_ptw_draft", input={})]),
        _Final(stop_reason="end_turn", usage=_usage(), content=[]),  # the silent reply seen on prod (ACOP-…004)
        _Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text="Here is the answer.")]),
    ]
    turn, calls, _ = _make_turn(monkeypatch, finals, draft={})
    events = await _drain(turn)
    assert len(calls) == 3
    done = events[-1][1]
    assert [m["role"] for m in done["appendMessages"]] == ["user", "assistant", "user", "assistant"]
    tool_turn = done["appendMessages"][2]["content"]
    assert tool_turn[0]["type"] == "tool_result" and tool_turn[-1]["text"].startswith("[app]")
    assert "".join(d["delta"] for e, d in events if e == "text") == "Here is the answer."
    # The third request is the second one plus the nudge — nothing earlier rewritten.
    assert calls[2]["messages"][:-1] == calls[1]["messages"][:-1]


def test_page_context_carries_user_id():
    msg = runtime.build_user_message(text="hi", page={"path": "/ptw/new"}, draft={}, now_local=None,
                                     tz_offset=0, user_plant_id="p1", user_id="u-123")
    assert '"userId": "u-123"' in msg["content"][0]["text"]


def test_new_tools_registered():
    assert "find_past_permits" in tools.TOOL_NAMES
    upd = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "update_ptw_draft")
    assert "checklistSuggestions" in upd["input_schema"]["properties"]


# ─── Plan-sanity alerts (mirror of src/lib/ptw/advisories.ts) ───────────


def _ids(alerts):
    return sorted(a["id"] for a in alerts)


def test_ten_minute_hot_work_is_flagged_with_a_realistic_window():
    alerts = ptw_draft.plan_advisories("HOT_WORK", [], "2026-10-28T09:00", "2026-10-28T09:10", "2026-10-27T15:00")
    assert _ids(alerts) == ["window-too-short"]
    msg = alerts[0]["message"]
    assert "10 minutes" in msg and "fire watch" in msg and "09:00–10:00" in msg


def test_extra_hazard_sets_the_minimum_window():
    # Cold work alone is fine in 20 min; add a confined-space annexure and it isn't.
    assert ptw_draft.plan_advisories("GENERAL_COLD", [], "2026-10-28T09:00", "2026-10-28T09:20", None) == []
    alerts = ptw_draft.plan_advisories("GENERAL_COLD", ["CONFINED_SPACE"], "2026-10-28T09:00", "2026-10-28T09:20", None)
    assert alerts[0]["id"] == "window-too-short" and "Confined Space" in alerts[0]["title"]


def test_past_start_and_no_time_for_approval():
    past = ptw_draft.plan_advisories("HOT_WORK", [], "2026-10-27T13:00", "2026-10-27T17:00", "2026-10-27T15:00")
    assert "start-in-past" in _ids(past)
    soon = ptw_draft.plan_advisories("HOT_WORK", [], "2026-10-27T15:10", "2026-10-27T18:00", "2026-10-27T15:00")
    assert _ids(soon) == ["no-time-for-approval"]


def test_night_and_multi_shift_are_info_only():
    alerts = ptw_draft.plan_advisories("HOT_WORK", [], "2026-10-28T08:00", "2026-10-29T06:00", "2026-10-27T15:00")
    assert _ids(alerts) == ["multi-shift", "night-work"]
    assert all(a["level"] == "info" for a in alerts)


def test_sensible_day_plan_has_no_alerts():
    assert ptw_draft.plan_advisories("HOT_WORK", [], "2026-10-28T09:00", "2026-10-28T13:00", "2026-10-27T15:00") == []


async def test_check_draft_reports_alerts():
    draft = {"type": "HOT_WORK", "validFrom": "2026-10-28T09:00", "validTo": "2026-10-28T09:05"}
    report = await ptw_draft.check_draft(None, draft, user_id="me", now_local="2026-10-27T15:00")
    assert report["alerts"][0]["id"] == "window-too-short"
    assert report["warnings"][0].startswith("Only 5 minutes for Hot Work")
