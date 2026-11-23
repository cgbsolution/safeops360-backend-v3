"""CSM contractor portal tokens: issue, revoke, resolve.

The supplier_portal pattern (services/supplier_portal.py), applied to one
prequalification application per token:

  * The raw token is 32 random bytes (URL-safe) shown once to the issuer; only
    its SHA-256 is stored, so a database read does not yield a usable link.
  * It expires (default 30 days, max 90) and can be revoked; re-issuing revokes
    the previous live token for the same application.
  * Scope comes from the token row, never from the request: the application id
    is read off the row.
  * Every attempt is logged to CsmPortalAccessLog; every failure answers the
    same 404 so a caller probing tokens learns nothing.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.csm import CsmPortalAccessLog, CsmPortalToken

DEFAULT_TTL_DAYS = 30
MAX_TTL_DAYS = 90
_READ_LIMIT = 120
_WRITE_LIMIT = 60
_WINDOW_SECONDS = 300
# In-process sliding window, as in supplier_portal: the access log is the
# forensic record; this only stops a runaway client.
_hits: dict[tuple[str, str, bool], list[float]] = {}


class PortalError(Exception):
    def __init__(self, status_code: int, outcome: str):
        self.status_code = status_code
        self.outcome = outcome
        super().__init__(outcome)


@dataclass
class IssuedToken:
    token: CsmPortalToken
    rawToken: str
    portalPath: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def portal_path(raw: str) -> str:
    return f"/contractor-prequal/{raw}"


def _rate_limited(prefix: str, ip: str | None, *, is_write: bool) -> bool:
    key = (prefix, ip or "-", is_write)
    now = time.monotonic()
    window = _hits.setdefault(key, [])
    cutoff = now - _WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.pop(0)
    if len(window) >= (_WRITE_LIMIT if is_write else _READ_LIMIT):
        return True
    window.append(now)
    return False


async def _log(db: AsyncSession, *, outcome: str, action: str, is_write: bool, token: CsmPortalToken | None = None,
               prefix: str | None = None, ip: str | None = None, user_agent: str | None = None) -> None:
    db.add(CsmPortalAccessLog(
        tokenId=token.id if token else None, tokenPrefix=token.tokenPrefix if token else prefix,
        outcome=outcome, action=action, isWrite=is_write,
        ipAddress=(ip or "")[:64] or None, userAgent=(user_agent or "")[:400] or None,
    ))
    await db.flush()


async def issue_token(
    db: AsyncSession, *, prequalification_id: str, company_id: str, contact_email: str | None,
    contact_name: str | None, actor_id: str | None, ttl_days: int = DEFAULT_TTL_DAYS,
) -> IssuedToken:
    ttl = max(1, min(int(ttl_days or DEFAULT_TTL_DAYS), MAX_TTL_DAYS))
    now = _utcnow()
    for prior in (
        await db.execute(
            select(CsmPortalToken).where(CsmPortalToken.prequalificationId == prequalification_id)
            .where(CsmPortalToken.revokedAt.is_(None))
        )
    ).scalars().all():
        prior.revokedAt = now
        prior.revokedById = actor_id
        prior.revokedReason = "Superseded by a newly issued link"
    raw = secrets.token_urlsafe(32)
    row = CsmPortalToken(
        prequalificationId=prequalification_id, contractorCompanyId=company_id, tokenHash=hash_token(raw),
        tokenPrefix=raw[:8], contactEmail=contact_email, contactName=contact_name,
        expiresAt=now + timedelta(days=ttl), createdById=actor_id, accessCount=0,
    )
    db.add(row)
    await db.flush()
    return IssuedToken(token=row, rawToken=raw, portalPath=portal_path(raw))


async def revoke_for_application(db: AsyncSession, *, prequalification_id: str, actor_id: str | None, reason: str) -> int:
    n = 0
    for row in (
        await db.execute(
            select(CsmPortalToken).where(CsmPortalToken.prequalificationId == prequalification_id)
            .where(CsmPortalToken.revokedAt.is_(None))
        )
    ).scalars().all():
        row.revokedAt = _utcnow()
        row.revokedById = actor_id
        row.revokedReason = reason
        n += 1
    await db.flush()
    return n


async def live_token(db: AsyncSession, prequalification_id: str) -> CsmPortalToken | None:
    row = (
        await db.execute(
            select(CsmPortalToken).where(CsmPortalToken.prequalificationId == prequalification_id)
            .where(CsmPortalToken.revokedAt.is_(None)).order_by(CsmPortalToken.createdAt.desc())
        )
    ).scalars().first()
    if row is None or (_aware(row.expiresAt) and _aware(row.expiresAt) < _utcnow()):
        return None
    return row


async def resolve_token(
    db: AsyncSession, raw: str, *, action: str, is_write: bool = False, ip: str | None = None,
    user_agent: str | None = None,
) -> CsmPortalToken:
    prefix = (raw or "")[:8]
    if _rate_limited(prefix, ip, is_write=is_write):
        await _log(db, outcome="RATE_LIMITED", action=action, is_write=is_write, prefix=prefix, ip=ip, user_agent=user_agent)
        raise PortalError(429, "RATE_LIMITED")
    row = (
        await db.execute(select(CsmPortalToken).where(CsmPortalToken.tokenHash == hash_token(raw or "")))
    ).scalars().first()
    if row is None:
        await _log(db, outcome="NOT_FOUND", action=action, is_write=is_write, prefix=prefix, ip=ip, user_agent=user_agent)
        raise PortalError(404, "NOT_FOUND")
    if row.revokedAt is not None:
        await _log(db, outcome="REVOKED", action=action, is_write=is_write, token=row, ip=ip, user_agent=user_agent)
        raise PortalError(404, "REVOKED")
    if _aware(row.expiresAt) < _utcnow():
        await _log(db, outcome="EXPIRED", action=action, is_write=is_write, token=row, ip=ip, user_agent=user_agent)
        raise PortalError(404, "EXPIRED")
    row.lastAccessedAt = _utcnow()
    row.accessCount = (row.accessCount or 0) + 1
    await _log(db, outcome="OK", action=action, is_write=is_write, token=row, ip=ip, user_agent=user_agent)
    return row


__all__ = ["DEFAULT_TTL_DAYS", "IssuedToken", "PortalError", "hash_token", "issue_token", "live_token",
           "portal_path", "resolve_token", "revoke_for_application"]
