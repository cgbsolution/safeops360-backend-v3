"""Safety-system impairments (fire/gas protection out of service).

  GET    /api/ptw/impairments?plant&status          list (PTW.READ)
  GET    /api/ptw/impairments/rules?plant           conflict rules in force (PTW.READ)
  POST   /api/ptw/impairments                       raise → status requested (PTW_IMPAIRMENT.CREATE)
  PATCH  /api/ptw/impairments/{id}                  edit restore time / precautions / reason
  POST   /api/ptw/impairments/{id}/approve          → approved, or active when its start is reached
                                                    (PTW_IMPAIRMENT.APPROVE; never the person who raised it)
  POST   /api/ptw/impairments/{id}/activate         approved → active now (APPROVE)
  POST   /api/ptw/impairments/{id}/restore          system back in service (CREATE or APPROVE)
  POST   /api/ptw/impairments/{id}/cancel           withdraw before it took effect (CREATE or APPROVE)

Every state change re-runs the exception engine for the plant (debounced), so
running permits in the zone get their exceptions within seconds.

⚠ Mounted before "ptw" (see ptw_ops.py) so /api/ptw/impairments is not read as a permit id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.plant import Area
from app.models.ptw_ops import IMPAIRMENT_SYSTEM_TYPES, TENANT_DEFAULT, Impairment
from app.models.user import User
from app.routers.ptw_ops import _allowed, _load_rules, _require, _require_tables, resolve_plant
from app.services.ptw_ops import engine, loaders
from app.services.ptw_ops import impairment_check as ic
from app.services.ptw_ops.payload import impairment_status

router = APIRouter(prefix="/api/ptw/impairments", tags=["ptw-impairments"])

CREATE, APPROVE = "PTW_IMPAIRMENT.CREATE", "PTW_IMPAIRMENT.APPROVE"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _view(i: Impairment):  # noqa: ANN202
    from app.services.ptw_ops.loaders import _utc
    from app.services.ptw_ops.types import ImpairmentView

    return ImpairmentView(id=i.id, code=i.code, plantId=i.plantId, areaId=i.areaId, systemType=i.systemType,
                          systemName=i.systemName, reason=i.reason, status=i.status, startedAt=_utc(i.startedAt),
                          plannedRestoreAt=_utc(i.plannedRestoreAt), restoredAt=_utc(i.restoredAt),
                          precautions=list(i.precautions or []), approvedById=i.approvedById,
                          createdById=i.createdById)


async def _out(db: AsyncSession, rows: list[Impairment]) -> list[dict[str, Any]]:
    if not rows:
        return []
    now = _now()
    areas = dict((await db.execute(select(Area.id, Area.name).where(Area.id.in_({r.areaId for r in rows if r.areaId})))).all())
    names = await loaders.load_names(db, {r.approvedById for r in rows} | {r.createdById for r in rows}
                                     | {r.restoredById for r in rows})
    rules_by_plant: dict[str, list] = {}
    out = []
    for r in rows:
        if r.plantId not in rules_by_plant:
            rules_by_plant[r.plantId] = await _load_rules(db, r.plantId)
        v = _view(r)
        effect, sev = ic.effect_summary(v, rules_by_plant[r.plantId], r.plantId)
        out.append({
            "id": r.id, "code": r.code, "plantId": r.plantId, "systemType": r.systemType, "systemName": r.systemName,
            "areaId": r.areaId, "areaName": areas.get(r.areaId), "reason": r.reason, "status": r.status,
            "inForce": v.is_in_force(now),
            "restoreStatus": impairment_status(v, now) if v.is_in_force(now) else None,
            "startedAt": r.startedAt.isoformat(), "plannedRestoreAt": r.plannedRestoreAt.isoformat() if r.plannedRestoreAt else None,
            "restoredAt": r.restoredAt.isoformat() if r.restoredAt else None,
            "precautions": list(r.precautions or []), "effect": effect, "effectSeverity": sev,
            "createdBy": names.get(r.createdById), "createdById": r.createdById,
            "approvedBy": names.get(r.approvedById or ""), "approvedAt": r.approvedAt.isoformat() if r.approvedAt else None,
            "restoredBy": names.get(r.restoredById or ""), "cancelledReason": r.cancelledReason,
            "escalationCount": r.escalationCount, "createdAt": r.createdAt.isoformat(),
        })
    return out


@router.get("")
async def list_impairments(plant: str | None = Query(None), status: str | None = Query(None),
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pid = await resolve_plant(db, user, plant)
    s = await loaders.schema(db)
    perms = {"create": await _allowed(db, user, CREATE, pid), "approve": await _allowed(db, user, APPROVE, pid)}
    if not s["Impairment"]:
        return {"plantId": pid, "items": [], "ready": False, "permissions": perms}
    q = select(Impairment).where(Impairment.plantId == pid).where(Impairment.tenantId == TENANT_DEFAULT)
    if status == "open":
        q = q.where(Impairment.status.in_(("requested", "approved", "active")))
    elif status:
        q = q.where(Impairment.status == status)
    rows = (await db.execute(q.order_by(Impairment.createdAt.desc()).limit(500))).scalars().all()
    return {"plantId": pid, "items": await _out(db, list(rows)), "ready": True, "permissions": perms,
            "systemTypes": [{"key": k, "label": ic.SYSTEM_LABELS[k]} for k in IMPAIRMENT_SYSTEM_TYPES]}


@router.get("/rules")
async def list_rules(plant: str | None = Query(None), user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pid = await resolve_plant(db, user, plant)
    rules = await _load_rules(db, pid)
    return {"cards": ic.rule_cards(rules), "controls": [{"key": k, "label": v} for k, v in ic.CONTROL_LABELS.items()]}


class ImpairmentIn(BaseModel):
    plantId: str
    areaId: str
    systemType: str
    systemName: str = Field(min_length=2, max_length=120)
    code: str | None = Field(None, max_length=30)
    reason: str = Field(min_length=3, max_length=2000)
    startedAt: datetime | None = None
    plannedRestoreAt: datetime
    precautions: list[str] = Field(default_factory=list, max_length=20)


class ImpairmentPatch(BaseModel):
    plannedRestoreAt: datetime | None = None
    precautions: list[str] | None = Field(None, max_length=20)
    reason: str | None = Field(None, min_length=3, max_length=2000)
    systemName: str | None = Field(None, min_length=2, max_length=120)


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


async def _get(db: AsyncSession, imp_id: str) -> Impairment:
    await _require_tables(db, "Impairment")
    row = await db.get(Impairment, imp_id)
    if row is None or row.tenantId != TENANT_DEFAULT:
        raise HTTPException(404, "Impairment not found")
    return row


async def _next_code(db: AsyncSession, plant_id: str) -> str:
    codes = (await db.execute(select(Impairment.code).where(Impairment.plantId == plant_id)
                              .where(Impairment.code.like("IMP-%")))).scalars().all()
    n = 0
    for c in codes:
        try:
            n = max(n, int(c.split("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    return f"IMP-{n + 1:04d}"


@router.post("", status_code=201)
async def raise_impairment(body: ImpairmentIn, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_tables(db, "Impairment")
    pid = await resolve_plant(db, user, body.plantId)
    await _require(db, user, CREATE, pid)
    if body.systemType not in IMPAIRMENT_SYSTEM_TYPES:
        raise HTTPException(400, f"systemType must be one of {', '.join(IMPAIRMENT_SYSTEM_TYPES)}")
    area = await db.get(Area, body.areaId)
    if area is None or area.plantId != pid:
        raise HTTPException(400, "Zone does not belong to this plant")
    started = _aware(body.startedAt) or _now()
    restore = _aware(body.plannedRestoreAt)
    if restore <= started:
        raise HTTPException(400, "Restore-by must be after the start time")
    code = (body.code or "").strip().upper() or await _next_code(db, pid)
    dup = await db.scalar(select(Impairment.id).where(Impairment.plantId == pid).where(Impairment.code == code)
                          .where(Impairment.status.in_(("requested", "approved", "active"))))
    if dup:
        raise HTTPException(409, f"{code} is already open at this plant")
    row = Impairment(plantId=pid, code=code, systemType=body.systemType, systemName=body.systemName.strip(),
                     areaId=body.areaId, reason=body.reason.strip(), startedAt=started, plannedRestoreAt=restore,
                     precautions=[p.strip() for p in body.precautions if p.strip()], status="requested",
                     createdById=user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return (await _out(db, [row]))[0]


@router.patch("/{imp_id}")
async def edit_impairment(imp_id: str, body: ImpairmentPatch, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _get(db, imp_id)
    await resolve_plant(db, user, row.plantId)
    if row.status in ("restored", "cancelled"):
        raise HTTPException(409, "A closed impairment cannot be edited")
    # Extending an approved/active impairment is an approval act; the raiser may
    # edit freely only while it is still a request.
    if row.status == "requested":
        if row.createdById != user.id and not await _allowed(db, user, APPROVE, row.plantId):
            await _require(db, user, CREATE, row.plantId)
    else:
        await _require(db, user, APPROVE, row.plantId)
    if body.plannedRestoreAt is not None:
        restore = _aware(body.plannedRestoreAt)
        if restore <= row.startedAt:
            raise HTTPException(400, "Restore-by must be after the start time")
        if restore != row.plannedRestoreAt:
            row.plannedRestoreAt = restore
            row.escalationCount = 0
            row.lastEscalatedAt = None
    if body.precautions is not None:
        row.precautions = [p.strip() for p in body.precautions if p.strip()]
    if body.reason is not None:
        row.reason = body.reason.strip()
    if body.systemName is not None:
        row.systemName = body.systemName.strip()
    await db.commit()
    engine.trigger(row.plantId)
    await db.refresh(row)
    return (await _out(db, [row]))[0]


@router.post("/{imp_id}/approve")
async def approve_impairment(imp_id: str, user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _get(db, imp_id)
    await _require(db, user, APPROVE, row.plantId)
    if row.status != "requested":
        raise HTTPException(409, f"Only a requested impairment can be approved (this one is {row.status})")
    if row.createdById == user.id:
        raise HTTPException(403, "The person who raised an impairment cannot approve it")
    now = _now()
    row.approvedById, row.approvedAt = user.id, now
    row.status = "active" if row.startedAt <= now else "approved"
    await db.commit()
    engine.trigger(row.plantId)
    await db.refresh(row)
    return (await _out(db, [row]))[0]


@router.post("/{imp_id}/activate")
async def activate_impairment(imp_id: str, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _get(db, imp_id)
    await _require(db, user, APPROVE, row.plantId)
    if row.status != "approved":
        raise HTTPException(409, "Only an approved impairment can be activated")
    now = _now()
    row.status, row.startedAt = "active", min(row.startedAt, now)
    await db.commit()
    engine.trigger(row.plantId)
    await db.refresh(row)
    return (await _out(db, [row]))[0]


@router.post("/{imp_id}/restore")
async def restore_impairment(imp_id: str, user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _get(db, imp_id)
    if not (await _allowed(db, user, CREATE, row.plantId) or await _allowed(db, user, APPROVE, row.plantId)):
        raise HTTPException(403, "Restoring an impairment needs the impairment permission")
    if row.status not in ("approved", "active"):
        raise HTTPException(409, f"Only an approved or active impairment can be restored (this one is {row.status})")
    row.status, row.restoredAt, row.restoredById = "restored", _now(), user.id
    await db.commit()
    engine.trigger(row.plantId)
    await db.refresh(row)
    return (await _out(db, [row]))[0]


@router.post("/{imp_id}/cancel")
async def cancel_impairment(imp_id: str, body: CancelIn, user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _get(db, imp_id)
    if not (await _allowed(db, user, CREATE, row.plantId) or await _allowed(db, user, APPROVE, row.plantId)):
        raise HTTPException(403, "Cancelling an impairment needs the impairment permission")
    if row.status not in ("requested", "approved"):
        raise HTTPException(409, "Only an impairment that has not taken effect can be cancelled — restore it instead")
    row.status, row.cancelledReason = "cancelled", body.reason.strip()
    await db.commit()
    engine.trigger(row.plantId)
    await db.refresh(row)
    return (await _out(db, [row]))[0]
