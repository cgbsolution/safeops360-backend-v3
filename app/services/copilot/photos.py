"""Photos the user attaches to a copilot turn (upload or camera).

The browser downscales each photo to JPEG before sending, so the model can
look at what the user saw (e.g. a hazard on the shop floor) and help record a
safety observation. Photos are shown to the model in the turn they were sent
with only. The transcript handed back to the browser carries a short text note
in their place, so later turns never re-upload them and the history stays
small. The same photos are attached to the New Observation form in the
browser and saved with the observation when the user submits it.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import HTTPException, status

MAX_PHOTOS = 3
# base64 characters per photo / per turn. The browser sends ~1600 px JPEGs
# (typically 150-600 KB), and the whole request must stay under the 4.5 MB
# body limit of the Vercel function that proxies the chat.
MAX_PHOTO_B64 = 1_400_000
MAX_TOTAL_B64 = 3_200_000
MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")

_MAGIC = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}


def validate_photos(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Check and normalise the request's photos. Returns Anthropic image blocks.

    Raises 400 for a malformed photo and 413 when there are too many or they
    are too large, before any turn starts."""
    if not raw:
        return []
    if len(raw) > MAX_PHOTOS:
        raise HTTPException(413, f"Attach at most {MAX_PHOTOS} photos to one message.")
    blocks: list[dict[str, Any]] = []
    total = 0
    for i, p in enumerate(raw, start=1):
        media = str(p.get("mediaType") or "").lower()
        data = p.get("data")
        if media not in MEDIA_TYPES or not isinstance(data, str) or not data:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Photo {i} is not a JPEG, PNG or WebP image.")
        if data.startswith("data:"):
            data = data.split(",", 1)[-1]
        if len(data) > MAX_PHOTO_B64:
            raise HTTPException(413, f"Photo {i} is too large. Attach a smaller photo.")
        total += len(data)
        if total > MAX_TOTAL_B64:
            raise HTTPException(413, "These photos are too large together. Send fewer photos.")
        try:
            head = base64.b64decode(data[:64] + "=" * (-len(data[:64]) % 4), validate=True)
            base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Photo {i} could not be read.") from None
        if not any(head.startswith(m) for m in _MAGIC[media]) or (media == "image/webp" and head[8:12] != b"WEBP"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Photo {i} is not a valid {media.split('/')[1].upper()} image.")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    return blocks


def photo_note(count: int) -> str:
    plural = "photo" if count == 1 else "photos"
    return (f"[The user attached {count} {plural} to this message. You saw {'it' if count == 1 else 'them'} in "
            f"this turn only; {'it is' if count == 1 else 'they are'} not repeated later. Rely on what you "
            f"described from {'it' if count == 1 else 'them'}.]")


def strip_photos(message: dict[str, Any]) -> dict[str, Any]:
    """The message as stored in the transcript: image blocks become one note."""
    content = message.get("content")
    if not isinstance(content, list):
        return message
    n = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
    if not n:
        return message
    kept = [b for b in content if not (isinstance(b, dict) and b.get("type") == "image")]
    return {**message, "content": [{"type": "text", "text": photo_note(n)}, *kept]}


def photo_summary(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the audit row records about the photos (never the image data)."""
    return [{"mediaType": b["source"]["media_type"], "approxBytes": len(b["source"]["data"]) * 3 // 4} for b in blocks]
