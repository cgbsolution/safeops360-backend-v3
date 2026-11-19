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
    assert turn.log.finished
    return [(event, data) for _, event, data in turn.log.events]


def _frames(body: str) -> list[tuple[int | None, str, dict[str, Any]]]:
    """Parse an SSE body into (id, event, data); comment lines are skipped."""
    out = []
    for chunk in body.split("\n\n"):
        seq, event, data = None, None, None
        for line in chunk.split("\n"):
            if line.startswith("id: "):
                seq = int(line[4:])
            elif line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event:
            out.append((seq, event, data))
    return out


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

    app, rows = _copilot_app(monkeypatch, copilot_router, fake_ensure, no_limit)

    body = {"conversationId": "conv-12345678", "message": "hi", "messages": [],
            "page": {"path": "/dashboard"}, "nowLocal": "2026-10-26T15:00", "timezoneOffsetMinutes": -330,
            "turnId": "a" * 32}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        bad = await client.post("/api/copilot/chat", json={**body, "messages": [{"role": "assistant", "content": "x"}]})
        assert bad.status_code == 400
        res = await client.post("/api/copilot/chat", json=body)
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
        events = _frames(res.text)
        assert events[0] == (1, "text", {"delta": "Hello from copilot", "at": 0})
        assert [e for _, e, _ in events][-2:] == ["done", "end"]
        assert len(events[-2][2]["appendMessages"]) == 2

        # Resuming after event 1 gets only what came later.
        rows["a" * 32] = SimpleNamespace(id="a" * 32, invokedById="me", sourceModule="COPILOT")
        again = await client.get(f"/api/copilot/chat/{'a' * 32}/events?after=1")
        assert [e for _, e, _ in _frames(again.text)] == ["done", "end"]
        # Someone else's turn, or an unknown one, is not found.
        rows["b" * 32] = SimpleNamespace(id="b" * 32, invokedById="other", sourceModule="COPILOT")
        assert (await client.get(f"/api/copilot/chat/{'b' * 32}/events")).status_code == 404
        assert (await client.get(f"/api/copilot/chat/{'c' * 32}/events")).status_code == 404


def _copilot_app(monkeypatch, copilot_router, fake_ensure, no_limit):
    """FastAPI app with the copilot router; DB lookups served from `rows`."""
    from fastapi import FastAPI

    from app.core.db import get_db
    from app.core.deps import get_current_user
    from app.services.copilot import turn_stream

    rows: dict[str, Any] = {}

    async def fake_find(db, turn_id):
        return rows.get(turn_id)

    async def no_saver(turn):
        return None

    class _Db:
        async def commit(self):
            pass

    monkeypatch.setattr(copilot_router, "ensure_copilot_agent", fake_ensure)
    monkeypatch.setattr(copilot_router, "enforce_rate_limit", no_limit)
    monkeypatch.setattr(copilot_router, "is_configured", lambda: True)
    monkeypatch.setattr(copilot_router, "get_settings", lambda: SimpleNamespace(copilot_enabled=True))
    monkeypatch.setattr(copilot_router, "find_turn", fake_find)
    monkeypatch.setattr(turn_stream, "_saver", no_saver)

    app = FastAPI()
    app.include_router(copilot_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="me", plantId="p1")
    app.dependency_overrides[get_db] = lambda: _Db()
    return app, rows


async def test_retried_request_joins_the_running_turn(monkeypatch):
    """The browser resends POST with the same turnId when it never got an
    answer; that must not start a second turn (or a second model call)."""
    import httpx

    from app.routers import copilot as copilot_router
    from app.services.copilot import turn_stream

    finals = [_Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text="Only once")])]
    _, calls, _ = _make_turn(monkeypatch, finals, draft=None)

    async def fake_ensure(db, user_id):
        return (SimpleNamespace(id="agent-1", isActive=True, primaryModelId="claude-opus-5-5", currentAuthorityLevel="L1", rateLimit=60),
                SimpleNamespace(id="prompt-1", systemPrompt="SYSTEM"))

    async def no_limit(db, agent, user_id):
        return None

    app, rows = _copilot_app(monkeypatch, copilot_router, fake_ensure, no_limit)
    turn_id = "d" * 32
    started: list[str] = []
    real_start = turn_stream.start_turn

    async def start_once(turn):
        started.append(turn.turn_id)
        await real_start(turn)
        rows[turn.turn_id] = SimpleNamespace(id=turn.turn_id, invokedById="me", sourceModule="COPILOT")

    monkeypatch.setattr(copilot_router, "start_turn", start_once)
    body = {"conversationId": "conv-12345678", "message": "hi", "messages": [], "page": {"path": "/"}, "turnId": turn_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post("/api/copilot/chat", json=body)
        second = await client.post("/api/copilot/chat", json=body)
    assert started == [turn_id] and len(calls) == 1
    for res in (first, second):
        text = "".join(d["delta"] for _, e, d in _frames(res.text) if e == "text")
        assert text == "Only once"
    rows[turn_id] = SimpleNamespace(id=turn_id, invokedById="someone-else", sourceModule="COPILOT")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.post("/api/copilot/chat", json=body)).status_code == 409


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
    monkeypatch.setattr(runtime, "_ROUND_RETRY_DELAYS", (0.0, 0.0, 0.0))
    events = await _drain(turn)
    assert events[0][0] == "error" and "reach the AI service" in events[0][1]["message"]
    assert events[-1][1]["appendMessages"] == []
    assert audit["error"]


# ─── 2026-11-04: turns that survive dropped connections ─────────────────


def _apply_text(events) -> str:
    """What the browser shows: text by offset, with rewinds applied."""
    text = ""
    for e, d in events:
        if e == "text":
            at = d["at"]
            text = text[:at] + d["delta"] if at <= len(text) else text + d["delta"]
        elif e == "rewind":
            text = text[: d["to"]]
    return text


async def test_round_that_drops_mid_stream_is_retried_without_duplicate_text(monkeypatch):
    import anthropic
    import httpx

    finals = [_Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text="Full answer.")])]
    turn, calls, audit = _make_turn(monkeypatch, finals, draft=None)
    monkeypatch.setattr(runtime, "_ROUND_RETRY_DELAYS", (0.0, 0.0, 0.0))

    class _Dropping(_Stream):
        def __aiter__(self):
            async def gen():
                yield SimpleNamespace(type="text", text="Half an ans")
                yield SimpleNamespace(type="content_block_start",
                                      content_block=SimpleNamespace(type="tool_use", id="tu_x", name="check_ptw_draft"))
                raise anthropic.APIConnectionError(request=httpx.Request("POST", "http://api/v1/messages"))
            return gen()

    real = _FakeClient.stream
    state = {"n": 0}

    def flaky(self, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            self.calls.append(kwargs)
            return _Dropping(_Final(stop_reason="end_turn", usage=_usage(), content=[]))
        return real(self, **kwargs)

    monkeypatch.setattr(_FakeClient, "stream", flaky)
    events = await _drain(turn)
    rewind = next(d for e, d in events if e == "rewind")
    assert rewind == {"to": 0, "dropTools": ["tu_x"]}
    assert _apply_text(events) == "Full answer."
    assert len(calls) == 2 and audit["error"] is None
    assert events[-1][0] == "done" and len(events[-1][1]["appendMessages"]) == 2


async def test_overload_reported_inside_a_stream_counts_as_transient():
    import anthropic
    import httpx

    response = httpx.Response(200, request=httpx.Request("POST", "http://api/v1/messages"))
    err = anthropic.APIStatusError("overloaded", response=response,
                                   body={"type": "error", "error": {"type": "overloaded_error"}})
    assert runtime._is_transient(err)
    bad = anthropic.APIStatusError("bad", response=httpx.Response(400, request=response.request),
                                   body={"type": "error", "error": {"type": "invalid_request_error"}})
    assert not runtime._is_transient(bad)


def test_turn_log_offsets_are_utf16_and_snapshot_merges_text():
    from app.services.copilot.turn_stream import TurnLog

    tl = TurnLog()
    tl.append("text", {"delta": "Fire 🔥 "})          # the emoji is 2 UTF-16 units
    tl.append("text", {"delta": "watch"})
    tl.append("tool", {"id": "t1", "status": "pending"})
    tl.append("text", {"delta": "!"})
    assert [d.get("at") for _, e, d in tl.events if e == "text"] == [0, 8, 13]
    snap = tl.snapshot()
    assert snap["events"] == [
        [2, "text", {"delta": "Fire 🔥 watch", "at": 0}],
        [3, "tool", {"id": "t1", "status": "pending"}],
        [4, "text", {"delta": "!", "at": 13}],
    ]
    assert snap["seq"] == 4 and snap["done"] is False and snap["beat"]


async def test_local_stream_ends_its_segment_with_reconnect():
    from app.services.copilot import turn_stream

    tl = turn_stream.TurnLog()
    tl.append("text", {"delta": "Working"})
    chunks = [c async for c in turn_stream._local_events(tl, 0, 0.05)]
    events = _frames(b"".join(chunks).decode())
    assert events == [(1, "text", {"delta": "Working", "at": 0}), (None, "reconnect", {"after": 1})]

    tl.append("text", {"delta": " on it"})
    tl.finish()
    chunks = [c async for c in turn_stream._local_events(tl, 1, 5)]
    assert _frames(b"".join(chunks).decode()) == [(2, "text", {"delta": " on it", "at": 7}), (None, "end", {})]


async def test_other_worker_streams_from_the_saved_copy(monkeypatch):
    """A resume that lands on a worker not running the turn reads the row's copy,
    sends only text it has not sent yet, and stops at `done`."""
    from datetime import datetime, timezone

    from app.services.copilot import turn_stream

    tl = turn_stream.TurnLog()
    tl.append("text", {"delta": "Hello"})
    first = tl.snapshot()
    tl.append("text", {"delta": " world"})
    tl.append("done", {"appendMessages": []})
    second = tl.snapshot(done=True)
    now = datetime.now(timezone.utc)
    rows = iter([
        SimpleNamespace(status="RUNNING", agentSuggestion={"stream": first}, invokedAt=now),
        SimpleNamespace(status="COMPLETED", agentSuggestion={"stream": second}, invokedAt=now),
    ])

    async def load(turn_id):
        return next(rows)

    monkeypatch.setattr(turn_stream, "_load_saved", load)
    monkeypatch.setattr(turn_stream, "POLL_SECONDS", 0.0)
    chunks = [c async for c in turn_stream._saved_events("e" * 32, 0, 30)]
    assert _frames(b"".join(chunks).decode()) == [
        (1, "text", {"delta": "Hello", "at": 0}),
        (2, "text", {"delta": " world", "at": 5}),
        (3, "done", {"appendMessages": []}),
        (None, "end", {}),
    ]


async def test_saved_copy_without_heartbeat_reports_a_lost_turn(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.services.copilot import turn_stream

    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    row = SimpleNamespace(status="RUNNING", invokedAt=None,
                          agentSuggestion={"stream": {"events": [], "done": False, "beat": old}})

    async def load(turn_id):
        return row

    monkeypatch.setattr(turn_stream, "_load_saved", load)
    chunks = [c async for c in turn_stream._saved_events("f" * 32, 0, 30)]
    events = _frames(b"".join(chunks).decode())
    assert events == [(None, "error", {"message": turn_stream.LOST_MESSAGE}), (None, "end", {})]


async def test_long_answer_survives_many_stream_segments(monkeypatch):
    """A client that follows `reconnect` gets the whole answer, once, even when
    the answer takes several stream segments (here 0.1 s each)."""
    import asyncio

    import httpx

    from app.routers import copilot as copilot_router

    pieces = [f"part{i} " for i in range(12)]
    finals = [_Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text="".join(pieces))])]
    _make_turn(monkeypatch, finals, draft=None)

    class _Slow(_Stream):
        def __aiter__(self):
            async def gen():
                for p in pieces:
                    await asyncio.sleep(0.03)
                    yield SimpleNamespace(type="text", text=p)
            return gen()

    monkeypatch.setattr(_FakeClient, "stream", lambda self, **kw: (self.calls.append(kw), _Slow(self._finals.pop(0)))[1])

    async def fake_ensure(db, user_id):
        return (SimpleNamespace(id="agent-1", isActive=True, primaryModelId="claude-opus-5-5", currentAuthorityLevel="L1", rateLimit=60),
                SimpleNamespace(id="prompt-1", systemPrompt="SYSTEM"))

    async def no_limit(db, agent, user_id):
        return None

    app, rows = _copilot_app(monkeypatch, copilot_router, fake_ensure, no_limit)
    monkeypatch.setattr(copilot_router, "get_settings",
                        lambda: SimpleNamespace(copilot_enabled=True, copilot_stream_segment_seconds=0.1))
    turn_id = "9" * 32
    body = {"conversationId": "conv-12345678", "message": "hi", "messages": [], "page": {"path": "/"}, "turnId": turn_id}
    seen: list[tuple[str, dict[str, Any]]] = []
    after, segments = 0, 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        res = await client.post("/api/copilot/chat", json=body)
        rows[turn_id] = SimpleNamespace(id=turn_id, invokedById="me", sourceModule="COPILOT")
        while True:
            segments += 1
            frames = _frames(res.text)
            for seq, event, data in frames:
                if seq is not None:
                    assert seq > after
                    after = seq
                seen.append((event, data))
            if frames[-1][1] == "end":
                break
            assert frames[-1][1] == "reconnect" and segments < 40
            res = await client.get(f"/api/copilot/chat/{turn_id}/events?after={after}")
    assert segments > 1
    assert _apply_text(seen) == "".join(pieces)
    assert [e for e, _ in seen][-2:] == ["done", "end"]


async def test_stop_cancels_the_turn_and_keeps_history_unchanged(monkeypatch):
    import asyncio

    from app.services.copilot import turn_stream

    turn, _, audit = _make_turn(monkeypatch, [], draft=None)

    class _Slow:
        def __init__(self, **kw):
            self.messages = self

        def stream(self, **kw):
            return _Hang()

    class _Hang(_Stream):
        def __init__(self):
            pass

        def __aiter__(self):
            async def gen():
                yield SimpleNamespace(type="text", text="Starting")
                await asyncio.sleep(30)
            return gen()

    import anthropic

    async def no_saver(t):
        return None

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Slow)
    monkeypatch.setattr(turn_stream, "_saver", no_saver)
    await turn_stream.start_turn(turn)
    for _ in range(50):
        if turn.log.text_len:
            break
        await asyncio.sleep(0.01)
    assert await turn_stream.request_stop(turn.turn_id, "me") is True
    await asyncio.wait_for(turn_stream._LOCAL[turn.turn_id].task, 2)
    done = [d for _, e, d in turn.log.events if e == "done"][-1]
    assert done["stopped"] is True and done["appendMessages"] == []
    assert audit["error"] == turn_stream.STOPPED_TEXT and turn.log.finished


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
