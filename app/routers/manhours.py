from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.manhours import Manhours
from app.models.plant import Plant
from app.models.user import User
from app.schemas.manhours import ManhoursCreate, ManhoursOut
from app.services import frequency_rates
from app.services.permissions import (
    PermissionContext,
    can,
    get_accessible_plants,
)

router = APIRouter(prefix="/api/manhours", tags=["manhours"])


def _compute_kpis(
    *,
    employee_hours: int,
    contractor_hours: int,
    lti: int,
    mtc: int,
    rwc: int,
    fatal: int,
    lost_days: int,
) -> dict[str, float | None]:
    """The three published rates, from the one shared definition.

    This used to apply a single 1,000,000-hour factor to all three, which is right
    for LTIFR and severity rate and wrong for TRIR (OSHA: 200,000). It also
    excluded RWC from the recordable count. The bases and the numerators now live
    in services.frequency_rates, which every reader on the platform shares.

    Nothing reads the stored columns these values land in any more — see the
    model. They are written for backwards compatibility only.
    """
    hours = (employee_hours or 0) + (contractor_hours or 0)
    return {
        "ltifr": frequency_rates.ltifr(lti=lti, fatalities=fatal, exposure_hours=hours),
        "trir": frequency_rates.trir(
            lti=lti, mtc=mtc, rwc=rwc, fatalities=fatal, exposure_hours=hours
        ),
        "severityRate": frequency_rates.severity_rate(
            lost_days=lost_days, fatalities=fatal, exposure_hours=hours
        ),
    }


@router.get("")
async def list_manhours(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    read_check = await can(db, user.id, "MANHOURS.READ", PermissionContext())
    if not read_check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, read_check.reason or "Access denied")
    plants = await get_accessible_plants(db, user.id)
    stmt = select(Manhours)
    if plants is None:
        pass
    elif not plants:
        return {"items": [], "total": 0}
    else:
        stmt = stmt.where(Manhours.plantId.in_(plants))
    # Newest-created first — platform-wide register convention. A submission
    # entered now for an older period must still lead the list.
    rows = (
        await db.execute(
            stmt.order_by(Manhours.createdAt.desc(), Manhours.id.desc()).limit(60)
        )
    ).scalars().all()
    return {"items": [ManhoursOut.model_validate(r) for r in rows], "total": len(rows)}


@router.post("", response_model=ManhoursOut, status_code=status.HTTP_201_CREATED)
async def create_manhours(
    payload: ManhoursCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ManhoursOut:
    await require_permission_with_context("MANHOURS.CREATE", user, db, plant_id=payload.plantId)
    plant = await db.get(Plant, payload.plantId)
    if plant is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid plant")

    now = datetime.now(timezone.utc)
    if payload.year > now.year or (payload.year == now.year and payload.month > now.month):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot enter manhours for a future month.")

    # Idempotent upsert via unique key (plantId, year, month)
    existing = (
        await db.execute(
            select(Manhours).where(
                Manhours.plantId == payload.plantId,
                Manhours.year == payload.year,
                Manhours.month == payload.month,
            )
        )
    ).scalar_one_or_none()

    kpis = _compute_kpis(
        employee_hours=payload.manhoursWorked,
        contractor_hours=payload.contractorManhours,
        lti=payload.ltiCount,
        mtc=payload.mtcCount,
        rwc=payload.rwcCount,
        fatal=payload.fatalCount,
        lost_days=payload.lostDays,
    )

    target = existing or Manhours(plantId=payload.plantId, year=payload.year, month=payload.month)
    target.headcount = payload.headcount
    # Canonical columns — what the Scorecard, the KPI engine, BRSR and the ERM KRI
    # feed all read. Writing only the legacy pair below meant a submission through
    # this endpoint never reached any of them (and could not insert at all:
    # employeeHours is NOT NULL with no database default).
    target.employeeHours = payload.manhoursWorked
    target.contractorHours = payload.contractorManhours
    target.ltiCount = payload.ltiCount
    target.mtcCount = payload.mtcCount
    target.rwcCount = payload.rwcCount
    target.facCount = payload.facCount
    target.fatalityCount = payload.fatalCount
    target.lostDays = payload.lostDays
    # Legacy aliases for the same three facts, kept in step.
    target.manhoursWorked = payload.manhoursWorked
    target.contractorManhours = payload.contractorManhours
    target.fatalCount = payload.fatalCount
    target.notes = payload.notes
    target.ltifr = kpis["ltifr"]
    target.trir = kpis["trir"]
    target.severityRate = kpis["severityRate"]
    target.submittedById = user.id
    target.submittedAt = now
    if existing is None:
        db.add(target)
    await db.flush()
    await db.refresh(target)
    return ManhoursOut.model_validate(target)
