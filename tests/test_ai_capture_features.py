"""Capture / voice / WhatsApp AI features — the parts that don't need a DB or the API.

The model is simulated: complete_json and the Anthropic client are monkeypatched with
realistic and malformed answers, so these tests pin the parsing and fallback paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import types
from typing import Any

import pytest

from app.services import capture_voice, whatsapp
from app.services.ai import anthropic_client, capture_providers as cp


def run(coro):
    return asyncio.run(coro)


def _patch_complete_json(monkeypatch, answer: Any, calls: list | None = None) -> None:
    async def fake(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return answer

    monkeypatch.setattr(anthropic_client, "complete_json", fake)
    monkeypatch.setattr(capture_voice, "complete_json", fake)


# ─── vision: image handling ───────────────────────────────────────────────


def _png_bytes(size=(64, 48)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"GIF89arest", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPrest", "image/webp"),
        (b"\x00\x00\x00 ftypheic", None),
    ],
)
def test_sniff_image_type(data: bytes, expected: str | None) -> None:
    assert cp._sniff_image_type(data) == expected


def test_shrink_image_fits_api_limits() -> None:
    big = _png_bytes((4000, 3000))
    out = cp._shrink_image(big)
    assert out is not None and out[:3] == b"\xff\xd8\xff"
    from PIL import Image

    with Image.open(io.BytesIO(out)) as img:
        assert max(img.size) <= cp._VISION_MAX_EDGE


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.kwargs: dict[str, Any] = {}
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.kwargs = kw
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])

        self.messages = _Messages()


def _vision(monkeypatch, model_text: str, image: bytes, mime: str = "image/jpeg"):
    fake = _FakeClient(model_text)
    monkeypatch.setattr(cp, "_get_client", lambda: fake)
    result = run(cp.AnthropicVisionProvider().suggest(image, mime, [{"code": "H1", "labels": {"en": "x"}}], "hi"))
    return result, fake.kwargs


def test_vision_sends_the_real_media_type_not_the_label(monkeypatch) -> None:
    # The wizard labels un-recompressed files image/jpeg; a PNG sent with that
    # label is rejected by the API.
    res, kw = _vision(monkeypatch, '{"l1Code":"H1","l2Code":null,"description":"d","descriptionEn":"d","confidence":0.8}',
                      _png_bytes(), mime="image/jpeg")
    block = kw["messages"][0]["content"][0]
    assert block["source"]["media_type"] == "image/png"
    assert "temperature" not in kw
    assert res and res["l1Code"] == "H1" and res["l2Code"] is None and res["confidence"] == 0.8


def test_vision_normalises_null_like_codes(monkeypatch) -> None:
    res, _ = _vision(monkeypatch, 'Sure: {"l1Code":"null","l2Code":7,"description":"d","descriptionEn":"","confidence":"0.6"}',
                     _png_bytes())
    assert res["l1Code"] is None and res["l2Code"] == "7" and res["confidence"] == 0.6


def test_vision_rejects_non_object_answer(monkeypatch) -> None:
    res, _ = _vision(monkeypatch, '[1, 2, 3]', _png_bytes())
    assert res is None


def test_vision_unknown_format_is_reencoded_or_dropped(monkeypatch) -> None:
    res, kw = _vision(monkeypatch, '{"l1Code":"H1","confidence":0.9}', b"\x00\x00\x00 ftypheic-not-decodable")
    assert res is None and kw == {}  # undecodable → no API call at all


# ─── text assist: malformed model output must not 500 ─────────────────────


@pytest.mark.parametrize("answer", [None, ["cleaned"], "plain string"])
def test_cleanup_tolerates_non_object_answers(monkeypatch, answer) -> None:
    _patch_complete_json(monkeypatch, answer)
    assert run(cp.cleanup_text("machine guard missing", "en")) is None


def test_cleanup_and_draft_happy_path(monkeypatch) -> None:
    _patch_complete_json(monkeypatch, {"cleaned": "The machine guard is missing."})
    assert run(cp.cleanup_text("machin gard missing", "en")) == "The machine guard is missing."
    _patch_complete_json(monkeypatch, {"description": "Guard missing on press 3.", "descriptionEn": "Guard missing on press 3."})
    out = run(cp.draft_description(report_type="observation", category_label=None, location="Press shop",
                                   severity=None, answers=[{"q": "What", "a": "guard missing"}], lang="en"))
    assert out == {"description": "Guard missing on press 3.", "descriptionEn": "Guard missing on press 3."}


def test_draft_tolerates_list_answer(monkeypatch) -> None:
    _patch_complete_json(monkeypatch, [{"description": "x"}])
    out = run(cp.draft_description(report_type="observation", category_label=None, location=None,
                                   severity=None, answers=[{"q": "What", "a": "x"}], lang="hi"))
    assert out is None


def test_category_suggestion_normalises_codes(monkeypatch) -> None:
    _patch_complete_json(monkeypatch, {"l1Code": " H1 ", "l2Code": "None", "confidence": 2})
    out = run(cp.suggest_category_from_text("loose wire", [{"code": "H1"}], "en"))
    assert out == {"l1Code": "H1", "l2Code": None, "confidence": 1.0}


# ─── voice translation ────────────────────────────────────────────────────


def test_english_transcript_is_copied_without_a_model_call(monkeypatch) -> None:
    calls: list = []
    _patch_complete_json(monkeypatch, {"english": "should not be used"}, calls)
    assert run(capture_voice._english_for("  oil on floor  ", "en-IN")) == "oil on floor"
    assert calls == []


def test_other_languages_are_translated(monkeypatch) -> None:
    calls: list = []
    _patch_complete_json(monkeypatch, {"english": "Oil on the floor"}, calls)
    assert run(capture_voice._english_for("फर्श पर तेल", "hi")) == "Oil on the floor"
    assert len(calls) == 1 and "temperature" not in calls[0]


@pytest.mark.parametrize("answer", [None, ["x"], {"english": "  "}])
def test_translation_failures_return_none_for_retry(monkeypatch, answer) -> None:
    _patch_complete_json(monkeypatch, answer)
    assert run(capture_voice._english_for("फर्श पर तेल", "hi")) is None


def test_blank_transcript_is_skipped(monkeypatch) -> None:
    calls: list = []
    _patch_complete_json(monkeypatch, {"english": "x"}, calls)
    assert run(capture_voice._english_for("   ", "hi")) is None and calls == []


# ─── WhatsApp ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "answer,expected",
    [
        ({"type": "Lost Time Injury", "severity": "high"}, {"type": "LTI", "severity": "HIGH"}),
        ({"type": "near-miss", "severity": "?"}, {"type": "HIPO_NEAR_MISS", "severity": "MEDIUM"}),
        ({"type": "FIRE", "severity": "CRITICAL"}, {"type": "FIRE", "severity": "CRITICAL"}),
        (["LTI"], {"type": "HIPO_NEAR_MISS", "severity": "MEDIUM"}),
        (None, {"type": "HIPO_NEAR_MISS", "severity": "MEDIUM"}),
    ],
)
def test_whatsapp_classification(monkeypatch, answer, expected) -> None:
    _patch_complete_json(monkeypatch, answer)
    monkeypatch.setattr(anthropic_client, "is_configured", lambda: True)
    assert run(whatsapp.classify_transcript("worker hand caught in roller")) == expected


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.added: list = []

    async def execute(self, stmt):
        return _FakeResult(self.rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def test_whatsapp_numbering_is_max_plus_one_of_its_own_shape() -> None:
    db = _FakeDb(["INC-2026-NW-0045", "INC-2026-NW-0007", "INC-2026-NW-HIST-01"])
    assert run(whatsapp._next_incident_number(db, "NW", 2026)) == "INC-2026-NW-0046"
    assert run(whatsapp._next_incident_number(_FakeDb(), "SW", 2026)) == "INC-2026-SW-0001"


@pytest.mark.parametrize("mtype,text,media", [("interactive", "APPROVE", None), ("text", "   ", None), ("voice", None, None)])
def test_whatsapp_non_reports_never_create_incidents(monkeypatch, mtype, text, media) -> None:
    sender = types.SimpleNamespace(id="s1", employeeId="u1", plantId="p1", role="WORKER")

    async def resolve(db, phone):
        return sender

    monkeypatch.setattr(whatsapp, "resolve_sender", resolve)
    monkeypatch.setattr(whatsapp, "is_verified", lambda s: True)

    async def boom(*a, **k):
        raise AssertionError("must not create an incident")

    monkeypatch.setattr(whatsapp, "create_incident_from_whatsapp", boom)
    out = run(whatsapp.handle_inbound(_FakeDb(), phone="+91999", message_type=mtype, text=text, media_id=media))
    assert out["action"] == "ignored"


def test_webhook_signature(monkeypatch) -> None:
    from app.routers import whatsapp as wa_router

    body = json.dumps({"from": "+91999", "type": "text", "text": "x"}).encode()
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    assert wa_router._signature_ok(body, None)  # no secret configured → accepted
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert wa_router._signature_ok(body, good)
    assert not wa_router._signature_ok(body, None)
    assert not wa_router._signature_ok(body, "sha256=" + "0" * 64)


# ─── feature flags ────────────────────────────────────────────────────────


def test_feature_flags_read_dotenv_and_licence_override(monkeypatch) -> None:
    import dotenv

    from app.licensing import state as lic_state
    from app.routers import capture as capture_router

    monkeypatch.delenv("AI_CAPTURE_ASSIST", raising=False)
    monkeypatch.delenv("VOICE_TRANSCRIPTION", raising=False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {"AI_CAPTURE_ASSIST": "true"})
    no_licence_flags = types.SimpleNamespace(payload=types.SimpleNamespace(feature_flags={}))
    monkeypatch.setattr(lic_state, "get_state", lambda: no_licence_flags)
    assert capture_router._features()["aiCaptureAssist"] is True  # set only in .env

    licence_off = types.SimpleNamespace(payload=types.SimpleNamespace(feature_flags={"aiCaptureAssist": False}))
    monkeypatch.setattr(lic_state, "get_state", lambda: licence_off)
    assert capture_router._features()["aiCaptureAssist"] is False  # licence wins

    monkeypatch.setenv("VOICE_TRANSCRIPTION", "yes")
    monkeypatch.setattr(lic_state, "get_state", lambda: no_licence_flags)
    assert capture_router._features()["voiceTranscription"] is True  # process env still honoured
