"""Exception engine: snapshot → rules → PtwException rows.

Idempotent. For each plant: drafts with a dedupeKey that has no open row are
opened (and notified when critical/high), open rows whose condition still holds
are refreshed in place (title/severity can change as minutes pass), and open
rows whose condition cleared are resolved. An exception is never deleted.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ptw_ops import TENANT_DEFAULT, PtwException
from app.services.ptw_ops import loaders, notify
from app.services.ptw_ops.rules import evaluate_all
from app.services.ptw_ops.types import ExceptionDraft

log = logging.getLogger("safeops360.ptw_ops")
NOTIFY_SEVERITIES = frozenset({"critical", "high"})


@dataclass
class Plan:
    to_open: list[ExceptionDraft]
    to_refresh: list[ExceptionDraft]
    to_resolve: list[str]


def reconcile(open_keys: set[str], drafts: list[ExceptionDraft]) -> Plan:
    """Pure upsert/resolve decision: drafts without an open row are opened,
    drafts with one are refreshed, open rows with no draft are resolved."""
    seen = set()
    to_open, to_refresh = [], []
    for d in drafts:
        if d.dedupeKey in seen:
            continue
        seen.add(d.dedupeKey)
        (to_refresh if d.dedupeKey in open_keys else to_open).append(d)
    return Plan(to_open, to_refresh, sorted(open_keys - seen))


async def evaluate_plant(db: AsyncSession, plant_id: str, *, now: datetime | None = None,
                         send: bool = True) -> dict[str, Any]:
    s = await loaders.schema(db)
    if not s["PtwException"]:
        return {"plantId": plant_id, "skipped": "PtwException table not created yet"}
    now = now or datetime.now(timezone.utc)
    snap, _ = await loaders.load(db, plant_id=plant_id, now=now)
    drafts = evaluate_all(snap)

    open_rows = (await db.execute(
        select(PtwException).where(PtwException.plantId == plant_id)
        .where(PtwException.tenantId == TENANT_DEFAULT).where(PtwException.resolvedAt.is_(None))
    )).scalars().all()
    plan = reconcile({r.dedupeKey for r in open_rows}, drafts)
    by_key = {r.dedupeKey: r for r in open_rows}
    opened: list[PtwException] = []
    for d in plan.to_open:
        row = PtwException(
            plantId=plant_id, ruleKey=d.ruleKey, severity=d.severity, dedupeKey=d.dedupeKey,
            permitId=d.permitId, impairmentId=d.impairmentId, areaId=d.areaId, title=d.title,
            detail=d.detail, ref=d.ref, openedAt=now, ownerUserId=d.ownerUserId, notified=[],
        )
        db.add(row)
        opened.append(row)
    for d in plan.to_refresh:
        row = by_key[d.dedupeKey]
        row.title, row.detail, row.severity, row.ref = d.title, d.detail, d.severity, d.ref
        if not row.ownerUserId:
            row.ownerUserId = d.ownerUserId
    for key in plan.to_resolve:
        by_key[key].resolvedAt = now
    await db.flush()

    sent = 0
    if send:
        for row in opened:
            if row.severity in NOTIFY_SEVERITIES:
                try:
                    sent += len(await notify.notify_exception(db, row))
                except Exception:  # noqa: BLE001
                    log.exception("PTW exception notify failed for %s", row.dedupeKey)
        await db.flush()
    return {"plantId": plant_id, "open": len(drafts), "opened": len(opened), "resolved": len(plan.to_resolve),
            "notifications": sent}


async def plants_to_evaluate(db: AsyncSession) -> list[str]:
    """Plants with anything the rules could fire on: open permits, impairments,
    or exceptions still open (so they can resolve)."""
    s = await loaders.schema(db)
    parts = ["""SELECT DISTINCT "plantId" FROM "Permit" WHERE "isDeleted" = false
                AND status::text NOT IN ('DRAFT','CLOSED','CANCELLED','REJECTED') AND "closedAt" IS NULL"""]
    if s["Impairment"]:
        parts.append("""SELECT DISTINCT "plantId" FROM "Impairment" WHERE status IN ('approved','active')""")
    if s["PtwException"]:
        parts.append("""SELECT DISTINCT "plantId" FROM "PtwException" WHERE "resolvedAt" IS NULL""")
    rows = (await db.execute(text(" UNION ".join(parts)))).all()
    return [r[0] for r in rows if r[0]]


async def evaluate_all_plants(db: AsyncSession) -> dict[str, Any]:
    out = {"plants": 0, "opened": 0, "resolved": 0, "notifications": 0, "evaluated": 0}
    for pid in await plants_to_evaluate(db):
        r = await evaluate_plant(db, pid)
        if r.get("skipped"):
            return {**out, "skipped": r["skipped"]}
        out["plants"] += 1
        out["evaluated"] += r["open"]
        out["opened"] += r["opened"]
        out["resolved"] += r["resolved"]
        out["notifications"] += r["notifications"]
    return out


# ── event-triggered evaluation (debounced, fire-and-forget) ─────────────────
_pending: dict[str, asyncio.Task] = {}
DEBOUNCE_S = 3.0


def trigger(plant_id: str | None) -> None:
    """Re-evaluate one plant shortly after a permit/impairment write. Debounced
    per plant, runs in its own session after the request's transaction commits.
    Never raises into the caller."""
    if not plant_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    old = _pending.get(plant_id)
    if old and not old.done():
        old.cancel()
    _pending[plant_id] = loop.create_task(_delayed(plant_id))


async def _delayed(plant_id: str) -> None:
    from app.core.audit_context import set_system_actor
    from app.core.db import AsyncSessionLocal

    try:
        await asyncio.sleep(DEBOUNCE_S)
        set_system_actor("ptw_exceptions_evaluate")
        async with AsyncSessionLocal() as db:
            await evaluate_plant(db, plant_id)
            await db.commit()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("PTW event-triggered evaluation failed for plant %s", plant_id)
