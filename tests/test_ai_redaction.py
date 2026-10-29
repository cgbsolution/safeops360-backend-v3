"""PII choke point in anthropic_client — offline, no network.

Every LLM payload and every agent tool result is scrubbed by
services/ai/redaction.py before it leaves the platform.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic

from app.services.ai import anthropic_client as ac
from app.services.ai.redaction import redact_data, redact_payload, scrub_text


def test_structured_person_names_become_stable_placeholders():
    data = {
        "injuredPersonName": "Ravi Kumar",
        "witnesses": [{"witnessName": "Sunita Rao", "statement": "Ravi Kumar slipped near the press."}],
        "summary": "Sunita Rao called the supervisor.",
    }
    out = redact_data(data)
    assert out["injuredPersonName"] == "Person 1"
    assert out["witnesses"][0]["witnessName"] == "Person 2"
    assert out["witnesses"][0]["statement"] == "Person 1 slipped near the press."
    assert out["summary"] == "Person 2 called the supervisor."


def test_plain_name_only_redacted_on_person_objects():
    data = {
        "crew": [{"userId": "u1", "name": "Amit Shah", "role": "Welder"}],
        "equipment": {"code": "EQ-1", "name": "EOT Crane 2"},
    }
    out = redact_data(data)
    assert out["crew"][0]["name"] == "Person 1" and out["crew"][0]["role"] == "Welder"
    assert out["equipment"]["name"] == "EOT Crane 2"  # not a person


def test_contact_and_identity_fields_dropped():
    out = redact_data({"email": "a@b.com", "phone": "9876543210", "aadhaarLast4": "1234",
                       "gpsLatitude": 21.1, "designation": "Fitter", "contractorName": "CoolTech Mechanical"})
    assert out["email"] == out["phone"] == out["aadhaarLast4"] == out["gpsLatitude"] == "[redacted]"
    assert out["designation"] == "Fitter" and out["contractorName"] == "CoolTech Mechanical"


def test_free_text_patterns():
    t = "Call ravi.k@plant.in or +91 98765 43210, Aadhaar 1234 5678 9012, PAN ABCDE1234F."
    out = scrub_text(t)
    assert "ravi.k@plant.in" not in out and "98765" not in out and "5678" not in out and "ABCDE1234F" not in out
    # dates, record numbers and small numbers survive
    keep = "INC-2026-NW-0045 on 2026-10-24, 3 workers, 120 kg"
    assert scrub_text(keep) == keep


def test_existing_placeholders_pass_through():
    out = redact_data({"injuredPersonName": "Person 2", "statement": "Person 2 fell."})
    assert out == {"injuredPersonName": "Person 2", "statement": "Person 2 fell."}


def test_redact_payload_json_and_text():
    js = json.dumps({"ownerName": "Meera Iyer", "note": "Meera Iyer to verify"}, indent=2)
    out = json.loads(redact_payload(js))
    assert out == {"ownerName": "Person 1", "note": "Person 1 to verify"}
    assert redact_payload("mail me at x@y.org") == "mail me at [email]"
    assert redact_payload("") == ""


def _msg(content, stop="end_turn"):
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1}})


class _Client:
    def __init__(self, replies):
        self.sent = []
        self.replies = list(replies)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.sent.append(json.loads(json.dumps(kw["messages"], default=str)))
        return self.replies.pop(0)

    def with_options(self, **_kw):
        return self


async def test_complete_json_payload_is_redacted(monkeypatch):
    client = _Client([_msg([{"type": "text", "text": "{\"ok\": true}"}])])
    monkeypatch.setattr(ac, "_get_client", lambda: client)
    out = await ac.complete_json(system="s", user=json.dumps({"reporterName": "Ravi Kumar", "email": "r@x.in"}))
    assert out == {"ok": True}
    sent = json.loads(client.sent[0][0]["content"])
    assert sent == {"reporterName": "Person 1", "email": "[redacted]"}


async def test_tool_loop_redacts_context_and_tool_results(monkeypatch):
    client = _Client([
        _msg([{"type": "tool_use", "id": "t1", "name": "get_training_records", "input": {}}], stop="tool_use"),
        _msg([{"type": "text", "text": "done"}]),
    ])
    monkeypatch.setattr(ac, "_get_client", lambda: client)

    async def dispatch(name, inp):
        return {"userId": "u1", "name": "Amit Shah", "phone": "9876543210"}

    res = await ac.complete_with_tools(
        system="s", initial_user_message=json.dumps({"witnessName": "Sunita Rao"}),
        tools=[{"name": "get_training_records", "description": "d", "input_schema": {"type": "object"}}],
        dispatch_tool=dispatch, model="claude-haiku-4-5",
    )
    assert res.final_text == "done"
    first = json.loads(client.sent[0][0]["content"])
    assert first == {"witnessName": "Person 1"}
    tool_msg = client.sent[1][-1]["content"][0]
    assert json.loads(tool_msg["content"]) == {"userId": "u1", "name": "Person 1", "phone": "[redacted]"}
    # the platform's own record of the tool call keeps the real output
    assert res.tool_calls[0].output["name"] == "Amit Shah"
