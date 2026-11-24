"""Scheduler job bodies — the brief's Inngest functions on the existing
dependency-free scheduler (app/services/scheduler.py; JobRun-audited,
run-now capable, misfire recovery).

  ptw_exceptions_evaluate   every 5 min   (+ engine.trigger() after writes)
  ptw_impairment_escalate   every 30 min  activate due impairments, escalate overdue
  ptw_shift_brief           every 15 min  writes at shift start and hourly, cached by inputs hash

Every job skips cleanly until the control-center DDL has been applied.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ptw_ops import TENANT_DEFAULT, Impairment, PtwException
from app.services.ptw_ops import brief, engine, loaders, notify

ESCALATE_EVERY = timedelta(minutes=30)
PLANT_HEAD_AFTER = 2   # misses before the plant head is added


async def exceptions_evaluate(db: AsyncSession) -> dict[str, Any]:
    return await engine.evaluate_all_plants(db)


async def _plant_heads(db: AsyncSession, plant_id: str) -> list[str]:
    rows = (await db.execute(text(
        """SELECT DISTINCT u.id FROM "User" u
           LEFT JOIN "UserRole" ur ON ur."userId" = u.id
           LEFT JOIN "Role" r ON r.id = ur."roleId"
           WHERE u."plantId" = :plant AND (u.role = 'PLANT_HEAD' OR r.code = 'PLANT_HEAD')"""
    ), {"plant": plant_id})).all()
    return [r[0] for r in rows]


async def impairment_escalate(db: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    s = await loaders.schema(db)
    if not (s["Impairment"] and s["PtwException"]):
        return {"skipped": "control-center tables not created yet"}
    now = now or datetime.now(timezone.utc)

    # approved + start time reached → active
    due = (await db.execute(
        select(Impairment).where(Impairment.tenantId == TENANT_DEFAULT).where(Impairment.status == "approved")
        .where(Impairment.startedAt <= now)
    )).scalars().all()
    for imp in due:
        imp.status = "active"

    overdue = (await db.execute(
        select(Impairment).where(Impairment.tenantId == TENANT_DEFAULT)
        .where(Impairment.status.in_(("approved", "active"))).where(Impairment.restoredAt.is_(None))
        .where(Impairment.plannedRestoreAt.is_not(None)).where(Impairment.plannedRestoreAt < now)
    )).scalars().all()
    escalated = 0
    owners = {}
    if overdue:
        area_ids = [i.areaId for i in overdue if i.areaId]
        if area_ids:
            rows = (await db.execute(text('SELECT id, "ownerUserId" FROM "Area" WHERE id = ANY(:ids)'),
                                     {"ids": area_ids})).all()
            owners = {r[0]: r[1] for r in rows}
    for imp in overdue:
        if imp.lastEscalatedAt and now - imp.lastEscalatedAt < ESCALATE_EVERY - timedelta(minutes=1):
            continue
        # make sure the critical exception exists before we notify against it
        await engine.evaluate_plant(db, imp.plantId, now=now, send=False)
        row = (await db.execute(
            select(PtwException).where(PtwException.dedupeKey == f"impairment_overdue:{imp.id}")
            .where(PtwException.resolvedAt.is_(None))
        )).scalar_one_or_none()
        recipients = [owners.get(imp.areaId or "") or "", imp.approvedById or "", imp.createdById]
        imp.escalationCount = (imp.escalationCount or 0) + 1
        if imp.escalationCount > PLANT_HEAD_AFTER:
            recipients += await _plant_heads(db, imp.plantId)
        if row is not None:
            await notify.notify_exception(db, row, extra_recipients=recipients,
                                          escalation=imp.escalationCount > PLANT_HEAD_AFTER)
        imp.lastEscalatedAt = now
        escalated += 1
    await db.flush()
    return {"activated": len(due), "overdue": len(overdue), "escalated": escalated, "updated": escalated + len(due)}


async def shift_brief(db: AsyncSession) -> dict[str, Any]:
    s = await loaders.schema(db)
    if not s["PtwShiftBrief"]:
        return {"skipped": "PtwShiftBrief table not created yet"}
    written = 0
    plants = await engine.plants_to_evaluate(db)
    for pid in plants:
        r = await brief.generate(db, pid)
        written += int(r.get("written") or 0)
    return {"plants": len(plants), "written": written}
