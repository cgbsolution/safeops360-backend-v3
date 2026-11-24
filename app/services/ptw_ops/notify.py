"""ptw/notify — email (existing mailer) + WhatsApp (when a provider is
configured). Every attempt that actually reaches a channel is appended to
PtwException.notified as {channel, to, at, ok}, so the board and the audit can
show who was told and when. Personal data stays server-side."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ptw_ops import PtwException
from app.models.user import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _contacts(db: AsyncSession, user_ids: list[str]) -> list[dict[str, Any]]:
    ids = [u for u in dict.fromkeys(user_ids) if u]
    if not ids:
        return []
    users = (await db.execute(select(User.id, User.name, User.email).where(User.id.in_(ids)))).all()
    phones: dict[str, str] = {}
    try:
        from app.models.incident_intel import WhatsappSender

        rows = (await db.execute(
            select(WhatsappSender.employeeId, WhatsappSender.phoneNumber)
            .where(WhatsappSender.employeeId.in_(ids)).where(WhatsappSender.verifiedAt.is_not(None))
        )).all()
        phones = {r[0]: r[1] for r in rows}
    except Exception:  # noqa: BLE001 — table may be absent on older instances
        phones = {}
    return [{"id": u.id, "name": u.name, "email": u.email, "phone": phones.get(u.id)} for u in users]


async def send(
    db: AsyncSession,
    *,
    recipients: list[str],
    subject: str,
    body: str,
    deep_link: str | None = None,
) -> list[dict[str, Any]]:
    """Deliver to each recipient on every channel they have. Returns the log
    entries (ok or not) for the caller to persist."""
    from app.services.notifications import send_email
    from app.services.whatsapp import send_message

    log: list[dict[str, Any]] = []
    text = body + (f"\n\nOpen: {deep_link}" if deep_link else "")
    for c in await _contacts(db, recipients):
        at = _now().isoformat()
        if c["email"]:
            try:
                ok = await send_email([c["email"]], subject, text)
            except Exception:  # noqa: BLE001
                ok = False
            log.append({"channel": "email", "to": c["email"], "userId": c["id"], "at": at, "ok": bool(ok)})
        if c["phone"]:
            res = await send_message(c["phone"], text=f"{subject}\n{text}")
            if res.get("sent") or res.get("reason") != "WhatsApp provider not configured":
                log.append({"channel": "whatsapp", "to": c["phone"], "userId": c["id"], "at": at,
                            "ok": bool(res.get("sent"))})
    return log


async def notify_exception(db: AsyncSession, row: PtwException, *, extra_recipients: list[str] | None = None,
                           escalation: bool = False) -> list[dict[str, Any]]:
    recipients = [row.ownerUserId or "", *(extra_recipients or [])]
    prefix = "ESCALATION · " if escalation else ""
    subject = f"[PTW {row.severity.upper()}] {prefix}{row.title}"
    body = "\n".join(x for x in (row.title, row.ref and f"Ref: {row.ref}", row.detail) if x)
    link = f"/ptw/{row.permitId}" if row.permitId else "/ptw?tab=impairments"
    entries = await send(db, recipients=recipients, subject=subject, body=body, deep_link=link)
    if entries:
        row.notified = [*(row.notified or []), *entries]
    return entries
