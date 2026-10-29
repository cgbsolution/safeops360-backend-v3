"""Feature 6 — WhatsApp capture router.

Endpoints:
  • GET  /api/whatsapp/webhook/inbound   — Meta webhook verification (hub.challenge)
  • POST /api/whatsapp/webhook/inbound   — inbound message (voice/text/interactive)
  • POST /api/whatsapp/senders/otp/request  — issue an OTP for a phone number
  • POST /api/whatsapp/senders/otp/verify   — bind phone→employee after OTP
  • POST /api/whatsapp/senders/register     — HR-admin registration (no OTP)
  • GET/POST /api/whatsapp/templates        — pre-approved message registry

The webhook is intentionally unauthenticated (Meta/BSP calls it); it is guarded
by an optional verify-token + the hard rule that an unverified number can NEVER
create an incident. Admin endpoints require auth + an admin role.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.incident_intel import WhatsappTemplate
from app.models.user import User
from app.services import whatsapp

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

_ADMIN_ROLES = {"HSE_MANAGER", "PLANT_HEAD", "CORPORATE_HSE", "ADMIN", "SYSTEM_ADMIN"}


# ─── Webhook ────────────────────────────────────────────────────────────────

@router.get("/webhook/inbound")
async def verify_webhook(request: Request) -> Any:
    """Meta webhook verification handshake — echoes hub.challenge when the
    verify token matches (if one is configured)."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    expected = getattr(get_settings(), "whatsapp_verify_token", None)
    if mode == "subscribe" and (expected is None or token == expected):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(challenge or "")
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification failed")


def _parse_inbound(body: dict[str, Any]) -> dict[str, Any] | None:
    """Accept either a Meta Cloud API envelope or a flat test payload."""
    # Flat form: {phone|from, type, text, mediaId}
    if body.get("phone") or body.get("from"):
        return {
            "phone": body.get("phone") or body.get("from"),
            "type": body.get("type", "text"),
            "text": body.get("text"),
            "mediaId": body.get("mediaId") or body.get("media_id"),
        }
    # Meta envelope: entry[].changes[].value.messages[]
    try:
        msg = body["entry"][0]["changes"][0]["value"]["messages"][0]
    except (KeyError, IndexError, TypeError):
        return None
    mtype = msg.get("type", "text")
    return {
        "phone": msg.get("from"),
        "type": "voice" if mtype in ("audio", "voice") else ("interactive" if mtype == "interactive" else "text"),
        "text": (msg.get("text") or {}).get("body") if mtype == "text" else None,
        "mediaId": (msg.get("audio") or msg.get("voice") or {}).get("id"),
    }


def _signature_ok(raw: bytes, header: str | None) -> bool:
    """Meta signs every webhook POST with X-Hub-Signature-256 (HMAC-SHA256 of
    the raw body with the app secret). When a secret is configured, anything
    unsigned is rejected — otherwise anyone who knows a registered phone
    number could post an incident in that employee's name."""
    import hashlib
    import hmac
    import os

    secret = getattr(get_settings(), "whatsapp_app_secret", None) or os.getenv("WHATSAPP_APP_SECRET")
    if not secret:
        return True  # no provider configured yet (dev / stubbed transport)
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@router.post("/webhook/inbound")
async def inbound(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Inbound WhatsApp message. Unverified numbers get OTP registration; never
    a silent incident. Verified voice/text becomes a classified incident."""
    raw = await request.body()
    if not _signature_ok(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")
    try:
        import json

        body = json.loads(raw or b"{}")
    except ValueError:
        return {"action": "ignored", "reason": "body is not JSON"}
    parsed = _parse_inbound(body if isinstance(body, dict) else {})
    if not parsed or not parsed.get("phone"):
        return {"action": "ignored", "reason": "no message payload"}
    return await whatsapp.handle_inbound(
        db, phone=parsed["phone"], message_type=parsed["type"],
        text=parsed.get("text"), media_id=parsed.get("mediaId"),
    )


# ─── Sender registration / OTP ──────────────────────────────────────────────

class OtpRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phone: str


class OtpVerify(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phone: str
    code: str
    # Optional and only honoured when it is the caller's own id: the OTP proves
    # the caller owns the phone, not who they are. Binding someone else's
    # number is the HR-admin route (/senders/register).
    employeeId: str | None = None


class AdminRegister(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phone: str
    employeeId: str
    plantId: str | None = None
    role: str | None = None


@router.post("/senders/otp/request")
async def otp_request(payload: OtpRequest, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await whatsapp.start_registration(db, payload.phone)


@router.post("/senders/otp/verify")
async def otp_verify(
    payload: OtpVerify,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Self-service binding is for the signed-in employee only. Previously this
    # was unauthenticated and bound the phone to any employeeId supplied, so
    # anyone could file WhatsApp incident reports in another person's name.
    if payload.employeeId and payload.employeeId != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can only verify a number for yourself; ask an admin to register it for someone else.",
        )
    res = await whatsapp.verify_otp(db, payload.phone, payload.code, employee_id=user.id)
    if not res.get("ok"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, res.get("reason", "Verification failed"))
    return res


@router.post("/senders/register")
async def admin_register(
    payload: AdminRegister,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if user.role not in _ADMIN_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    sender = await whatsapp.admin_register(
        db, phone=payload.phone, employee_id=payload.employeeId, plant_id=payload.plantId, role=payload.role
    )
    return {"ok": True, "senderId": sender.id, "verifiedAt": sender.verifiedAt.isoformat() if sender.verifiedAt else None}


# ─── Template registry ──────────────────────────────────────────────────────

class TemplateInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    body: str
    category: str = "UTILITY"
    language: str = "en"
    status: str = "DRAFT"


@router.get("/templates")
async def list_templates(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = (await db.execute(select(WhatsappTemplate).order_by(WhatsappTemplate.name))).scalars().all()
    return [
        {"id": t.id, "name": t.name, "category": t.category, "language": t.language,
         "body": t.body, "status": t.status}
        for t in rows
    ]


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TemplateInput,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if user.role not in _ADMIN_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    existing = (
        await db.execute(select(WhatsappTemplate).where(WhatsappTemplate.name == payload.name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Template name already exists")
    t = WhatsappTemplate(
        name=payload.name, body=payload.body, category=payload.category,
        language=payload.language, status=payload.status,
    )
    db.add(t)
    await db.flush()
    await db.refresh(t)
    return {"id": t.id, "name": t.name, "status": t.status}
