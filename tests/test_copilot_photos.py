"""Copilot photos: validation, the model sees them, the transcript does not keep them."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.copilot import form_drafts, photos, runtime
from tests.test_safeops_copilot import _Block, _drain, _Final, _make_turn, _usage

JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def test_valid_photos_become_image_blocks():
    blocks = photos.validate_photos([
        {"mediaType": "image/jpeg", "data": _b64(JPEG)},
        {"mediaType": "image/png", "data": "data:image/png;base64," + _b64(PNG)},
    ])
    assert [b["source"]["media_type"] for b in blocks] == ["image/jpeg", "image/png"]
    assert blocks[1]["source"]["data"] == _b64(PNG)  # data: prefix removed
    assert photos.validate_photos([]) == [] and photos.validate_photos(None) == []


@pytest.mark.parametrize("raw, code", [
    ([{"mediaType": "image/gif", "data": _b64(JPEG)}], 400),          # type not allowed
    ([{"mediaType": "image/jpeg", "data": "not base64!!"}], 400),     # unreadable
    ([{"mediaType": "image/jpeg", "data": _b64(PNG)}], 400),          # bytes don't match the type
    ([{"mediaType": "image/jpeg", "data": _b64(JPEG)}] * 4, 413),     # too many
    ([{"mediaType": "image/jpeg", "data": "A" * (photos.MAX_PHOTO_B64 + 4)}], 413),
])
def test_bad_photos_are_refused_before_the_turn(raw, code):
    with pytest.raises(HTTPException) as e:
        photos.validate_photos(raw)
    assert e.value.status_code == code


def test_total_size_cap():
    big = "/9j/" + "A" * (photos.MAX_PHOTO_B64 - 8)
    with pytest.raises(HTTPException) as e:
        photos.validate_photos([{"mediaType": "image/jpeg", "data": big}] * 3)
    assert e.value.status_code == 413


def test_user_message_puts_photos_before_text_and_flags_them():
    blocks = photos.validate_photos([{"mediaType": "image/jpeg", "data": _b64(JPEG)}])
    msg = runtime.build_user_message(text="Oil leak near the mill stand", page={"path": "/observations/new"},
                                     draft=None, now_local=None, tz_offset=0, user_plant_id="p1", photos=blocks)
    assert [b["type"] for b in msg["content"]] == ["text", "image", "text"]
    ctx = json.loads(msg["content"][0]["text"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert ctx["photosAttached"] == 1


def test_strip_photos_replaces_images_with_a_note():
    msg = {"role": "user", "content": [{"type": "text", "text": "ctx"},
                                       {"type": "image", "source": {"data": "x"}},
                                       {"type": "image", "source": {"data": "y"}},
                                       {"type": "text", "text": "hi"}]}
    out = photos.strip_photos(msg)
    assert all(b["type"] == "text" for b in out["content"])
    assert "2 photos" in out["content"][0]["text"] and out["content"][-1]["text"] == "hi"
    assert msg["content"][1]["type"] == "image"  # original untouched
    plain = {"role": "user", "content": "text only"}
    assert photos.strip_photos(plain) is plain


def test_observation_draft_keeps_photo_count():
    _, d = form_drafts.normalise_client_form({"kind": "OBSERVATION", "draft": {"photoCount": 2}})
    assert d["photoCount"] == 2


async def test_turn_sends_photos_to_the_model_but_not_back_to_the_browser(monkeypatch):
    finals = [_Final(stop_reason="end_turn", usage=_usage(), content=[_Block(type="text", text="I can see an oil leak.")])]
    turn, calls, audit = _make_turn(monkeypatch, finals, draft=None)
    blocks = photos.validate_photos([{"mediaType": "image/jpeg", "data": _b64(JPEG)}])
    # Same turn, now with a photo attached (as the router builds it).
    turn.photos = blocks
    turn.user_message = runtime.build_user_message(
        text="What is unsafe here?", page={"path": "/observations/new"}, draft=None, now_local=None,
        tz_offset=0, user_plant_id="p1", photos=blocks)
    events = await _drain(turn)

    sent = calls[0]["messages"][-1]["content"]
    assert any(b["type"] == "image" for b in sent)
    done = events[-1][1]
    first = done["appendMessages"][0]
    assert not any(b.get("type") == "image" for b in first["content"])
    assert "1 photo" in first["content"][0]["text"]
    assert done["appendMessages"][1]["role"] == "assistant"
    assert photos.photo_summary(blocks)[0]["mediaType"] == "image/jpeg"
    assert audit["error"] is None


def test_request_model_accepts_images():
    from app.routers.copilot import CopilotTurnRequest

    req = CopilotTurnRequest(conversationId="conv-12345678", message="look",
                             images=[{"mediaType": "image/jpeg", "data": _b64(JPEG)}])
    assert len(req.images) == 1
    assert SimpleNamespace  # keep import used
