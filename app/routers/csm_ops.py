"""Contractor Safety Management router, part 2. Mounts at /api/csm (same prefix
as routers/csm.py; the paths do not overlap).

  /work-orders            contract / work order, safety checklist, activation gate
  /work-orders/{id}/roster  plant mobilisation: existing ContractorWorker rows,
                          plant induction and the plant gate clearance
  /evaluations            periodic and contract-end performance evaluations
  /sanctions              suspend / blacklist / reinstate with approval

Plant mobilisation design: a work-order roster, not a synthetic
ConstructionSite per plant. The EPC tables stay untouched (no ALTER, no plant
"sites" appearing in EPC lists, no plant scope retrofitted into the unscoped EPC
routers). The roster reuses ContractorWorker (onboarding, medical fitness,
safety-roster hold) and runs the same gate checks against the work order.

RBAC as in routers/csm.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.csm import CsmEvaluation, CsmPrequalification, CsmSanction, CsmWorkOrder, CsmWorkOrderWorker
from app.models.epc import ContractorCompany, ContractorWorker, GatePass
from app.models.user import User
from app.routers import csm as base
from app.services import csm as svc
from app.services.numbering import next_number

router = APIRouter(prefix="/api/csm", tags=["csm"])

READ, MANAGE, EVALUATE, APPROVE = base.READ, base.MANAGE, base.EVALUATE, base.APPROVE
iso = base.iso


async def _company(db: AsyncSession, cid: str) -> ContractorCompany:
    c = await db.get(ContractorCompany, cid)
    if c is None:
        raise HTTPException(404, "Contractor not found")
    return c


def _qualification(c: ContractorCompany) -> str:
    return svc.effective_qualification(c.prequalificationStatus, c.prequalificationValidUntil)


# ── work orders ──────────────────────────────────────────────────────────────
class WorkOrderIn(BaseModel):
    contractorCompanyId: str
    plantId: str
    areaId: str | None = None
    title: str = Field(min_length=3, max_length=200)
    scope: str = Field(min_length=10, max_length=8000)
    workType: str = "MAINTENANCE"
    contractRef: str | None = Field(None, max_length=100)
    startDate: datetime
    endDate: datetime
    engineerInChargeId: str | None = None
    estimatedManpower: int = Field(0, ge=0, le=20000)
    supervisorCount: int | None = Field(None, ge=0, le=2000)
    highRiskActivities: list[str] = []
    safetyChecklist: dict[str, bool] = {}
    safetyNotes: str | None = Field(None, max_length=4000)


class WorkOrderPatch(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=200)
    scope: str | None = Field(None, min_length=10, max_length=8000)
    areaId: str | None = None
    workType: str | None = None
    contractRef: str | None = None
    startDate: datetime | None = None
    endDate: datetime | None = None
    engineerInChargeId: str | None = None
    estimatedManpower: int | None = Field(None, ge=0, le=20000)
    supervisorCount: int | None = Field(None, ge=0, le=2000)
    highRiskActivities: list[str] | None = None
    safetyChecklist: dict[str, bool] | None = None
    safetyNotes: str | None = None


def _wo(w: CsmWorkOrder, c: ContractorCompany | None, users: dict[str, str], plants: dict[str, str]) -> dict[str, Any]:
    return {
        "id": w.id, "workOrderNo": w.workOrderNo, "contractorCompanyId": w.contractorCompanyId,
        "contractorName": c.name if c else None, "contractorStatus": (c.status if c else None),
        "plantId": w.plantId, "plantName": plants.get(w.plantId), "areaId": w.areaId, "areaName": w.areaName,
        "title": w.title, "scope": w.scope, "workType": w.workType, "contractRef": w.contractRef,
        "startDate": iso(w.startDate), "endDate": iso(w.endDate), "engineerInChargeId": w.engineerInChargeId,
        "engineerInChargeName": users.get(w.engineerInChargeId or ""), "estimatedManpower": w.estimatedManpower,
        "supervisorCount": w.supervisorCount, "highRiskActivities": w.highRiskActivities or [],
        "safetyChecklist": w.safetyChecklist or {}, "safetyNotes": w.safetyNotes, "status": w.status,
        "activatedAt": iso(w.activatedAt), "suspendedReason": w.suspendedReason, "completedAt": iso(w.completedAt),
        "closureNotes": w.closureNotes, "createdAt": iso(w.createdAt),
    }


async def _area_name(db: AsyncSession, area_id: str | None, plant_id: str) -> str | None:
    if not area_id:
        return None
    from app.models.plant import Area

    a = await db.get(Area, area_id)
    if a is None or a.plantId != plant_id:
        raise HTTPException(422, "That area does not belong to the work order's plant")
    return a.name


def _check_wo_fields(highrisk: list[str] | None, checklist: dict | None, work_type: str | None) -> None:
    base.check_choice(work_type, svc.WORK_TYPES, "workType")
    bad = [h for h in (highrisk or []) if h not in svc.HIGH_RISK_ACTIVITIES]
    if bad:
        raise HTTPException(422, f"Unknown high-risk activity: {', '.join(bad)}")
    keys = {k for k, _l in svc.SAFETY_CHECKLIST}
    if checklist and set(checklist) - keys:
        raise HTTPException(422, f"Unknown checklist item: {', '.join(sorted(set(checklist) - keys))}")


async def _activation_blockers(db: AsyncSession, w: CsmWorkOrder) -> list[str]:
    c = await _company(db, w.contractorCompanyId)
    return svc.activation_blockers(
        company_status=c.status, qualification=_qualification(c), checklist=w.safetyChecklist or {},
        start=w.startDate, end=w.endDate, engineer_id=w.engineerInChargeId, manpower=w.estimatedManpower or 0,
        supervisors=w.supervisorCount,
    )


async def _wo_detail(db: AsyncSession, w: CsmWorkOrder) -> dict[str, Any]:
    c = await db.get(ContractorCompany, w.contractorCompanyId)
    roster = (await db.execute(
        select(CsmWorkOrderWorker, ContractorWorker).join(ContractorWorker, ContractorWorker.id == CsmWorkOrderWorker.contractorWorkerId)
        .where(CsmWorkOrderWorker.workOrderId == w.id).order_by(ContractorWorker.fullName)
    )).all()
    users, pl = await base.names(db, [w.engineerInChargeId, *[r.inductionById for r, _w in roster]], [w.plantId])
    out = _wo(w, c, users, pl)
    out["contractorQualification"] = _qualification(c) if c else None
    out["activationBlockers"] = await _activation_blockers(db, w) if w.status in ("DRAFT", "SUSPENDED") else []
    out["history"] = w.history or []
    out["roster"] = [_roster(r, wk, users) for r, wk in roster]
    out["rosterCounts"] = {s: sum(1 for r, _ in roster if r.status == s) for s in svc.ROSTER_STATUSES}
    return out


def _roster(r: CsmWorkOrderWorker, wk: ContractorWorker, users: dict[str, str]) -> dict[str, Any]:
    return {
        "id": r.id, "contractorWorkerId": r.contractorWorkerId, "workerCode": wk.workerCode, "fullName": wk.fullName,
        "primaryTrade": wk.primaryTrade, "tradeAtSite": r.tradeAtSite, "status": r.status,
        "medicalValidUntil": iso(wk.currentMedicalValidUntil), "medicalState": svc.expiry_state(wk.currentMedicalValidUntil) or "MISSING",
        "workerStatus": wk.overallStatus, "rosterStatus": getattr(wk, "rosterStatus", "active") or "active",
        "inductionAt": iso(r.inductionAt), "inductionValidUntil": iso(r.inductionValidUntil),
        "inductionByName": users.get(r.inductionById or ""), "lastCheck": r.lastCheck, "lastCheckedAt": iso(r.lastCheckedAt),
        "gatePassNo": r.gatePassNo, "gatePassValidUntil": iso(r.gatePassValidUntil), "releasedAt": iso(r.releasedAt),
    }


async def _wo_for(db: AsyncSession, user: User, wid: str, perm: str) -> CsmWorkOrder:
    w = await base.get_live(db, CsmWorkOrder, wid)
    await base.require(db, user, perm, w.plantId)
    if perm == READ:
        plants = await base.scoped_plants(db, user, None)
        if plants is not None and w.plantId not in plants:
            raise HTTPException(403, "Plant is outside your access scope")
    return w


@router.get("/work-orders")
async def list_work_orders(
    plantId: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    contractorCompanyId: str | None = Query(None), q: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await base.scoped_plants(db, user, plantId)
    stmt = select(CsmWorkOrder, ContractorCompany).join(ContractorCompany, ContractorCompany.id == CsmWorkOrder.contractorCompanyId) \
        .where(CsmWorkOrder.isDeleted.is_(False)).where(base.plant_where(CsmWorkOrder.plantId, plants))
    if status_ == "LIVE":
        stmt = stmt.where(CsmWorkOrder.status.in_(("ACTIVE", "SUSPENDED")))
    elif status_:
        stmt = stmt.where(CsmWorkOrder.status == status_)
    if contractorCompanyId:
        stmt = stmt.where(CsmWorkOrder.contractorCompanyId == contractorCompanyId)
    if q:
        stmt = stmt.where(or_(CsmWorkOrder.title.ilike(f"%{q}%"), CsmWorkOrder.workOrderNo.ilike(f"%{q}%"),
                              ContractorCompany.name.ilike(f"%{q}%")))
    rows = (await db.execute(stmt.order_by(CsmWorkOrder.startDate.desc()).limit(500))).all()
    users, pl = await base.names(db, [w.engineerInChargeId for w, _ in rows], [w.plantId for w, _ in rows])
    return {"items": [_wo(w, c, users, pl) for w, c in rows], "total": len(rows)}


@router.post("/work-orders", status_code=201)
async def create_work_order(body: WorkOrderIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await base.require(db, user, MANAGE, body.plantId)
    c = await _company(db, body.contractorCompanyId)
    if (c.status or "").upper() == "BLACKLISTED":
        raise HTTPException(409, "Contractor company is blacklisted: no work order can be raised")
    _check_wo_fields(body.highRiskActivities, body.safetyChecklist, body.workType)
    if body.endDate <= body.startDate:
        raise HTTPException(422, "End date must be after the start date")
    no = await next_number(db, column=CsmWorkOrder.workOrderNo, stem=f"CWO-{body.startDate.year}-")
    w = CsmWorkOrder(
        workOrderNo=no, contractorCompanyId=c.id, plantId=body.plantId, areaId=body.areaId,
        areaName=await _area_name(db, body.areaId, body.plantId), title=body.title.strip(), scope=body.scope.strip(),
        workType=body.workType, contractRef=body.contractRef, startDate=body.startDate, endDate=body.endDate,
        engineerInChargeId=body.engineerInChargeId, estimatedManpower=body.estimatedManpower,
        supervisorCount=body.supervisorCount, highRiskActivities=list(dict.fromkeys(body.highRiskActivities)),
        safetyChecklist={k: bool(v) for k, v in body.safetyChecklist.items()}, safetyNotes=body.safetyNotes,
        status="DRAFT", history=[], createdBy=user.id,
    )
    base.add_history(w, user, None, "DRAFT", "Work order raised")
    db.add(w)
    await db.commit()
    await db.refresh(w)
    return await _wo_detail(db, w)


@router.get("/work-orders/{wid}")
async def get_work_order(wid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await _wo_detail(db, await _wo_for(db, user, wid, READ))


@router.patch("/work-orders/{wid}")
async def update_work_order(wid: str, body: WorkOrderPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Draft: everything. Once activated the checklist is frozen (it was the
    activation evidence); dates, manpower and notes can still change."""
    w = await _wo_for(db, user, wid, MANAGE)
    if w.status in ("COMPLETED", "CANCELLED"):
        raise HTTPException(409, f"A {w.status.lower()} work order cannot be edited")
    data = body.model_dump(exclude_unset=True)
    if w.status != "DRAFT":
        locked = {"safetyChecklist", "contractorCompanyId", "startDate", "workType", "areaId"} & set(data)
        if locked:
            raise HTTPException(409, f"After activation these cannot change: {', '.join(sorted(locked))}")
    _check_wo_fields(data.get("highRiskActivities"), data.get("safetyChecklist"), data.get("workType"))
    if "areaId" in data:
        w.areaName = await _area_name(db, data["areaId"], w.plantId)
    for k, v in data.items():
        if k == "safetyChecklist":
            v = {**(w.safetyChecklist or {}), **{kk: bool(vv) for kk, vv in (v or {}).items()}}
        if k == "highRiskActivities":
            v = list(dict.fromkeys(v or []))
        setattr(w, k, v)
    if svc.aware(w.endDate) <= svc.aware(w.startDate):
        raise HTTPException(422, "End date must be after the start date")
    await db.commit()
    await db.refresh(w)
    return await _wo_detail(db, w)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class NotesIn(BaseModel):
    notes: str | None = Field(None, max_length=4000)


async def _wo_move(db: AsyncSession, user: User, w: CsmWorkOrder, target: str, note: str | None = None) -> None:
    if not svc.can_move_wo(w.status, target):
        raise HTTPException(409, f"Cannot move a work order from {w.status} to {target}")
    base.add_history(w, user, w.status, target, note)
    w.status = target


async def _block_roster(db: AsyncSession, wo_ids: list[str], note: str) -> int:
    n = 0
    for r in (await db.execute(
        select(CsmWorkOrderWorker).where(CsmWorkOrderWorker.workOrderId.in_(wo_ids or ["__none__"]))
        .where(CsmWorkOrderWorker.status == "CLEARED")
    )).scalars().all():
        r.status = "BLOCKED"
        r.gatePassValidUntil = svc.utcnow()
        r.lastCheck = {"overall": "not_cleared", "blocking": ["work_order"], "note": note}
        r.lastCheckedAt = svc.utcnow()
        n += 1
    return n


@router.post("/work-orders/{wid}/activate")
async def activate(wid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    w = await _wo_for(db, user, wid, MANAGE)
    if w.status not in ("DRAFT", "SUSPENDED"):
        raise HTTPException(409, f"A {w.status.lower()} work order cannot be activated")
    base.conflict(await _activation_blockers(db, w))
    await _wo_move(db, user, w, "ACTIVE", "Activated" if w.status == "DRAFT" else "Resumed")
    w.activatedAt = w.activatedAt or svc.utcnow()
    w.activatedById = w.activatedById or user.id
    w.suspendedReason = None
    await db.commit()
    await db.refresh(w)
    return await _wo_detail(db, w)


@router.post("/work-orders/{wid}/suspend")
async def suspend_wo(wid: str, body: ReasonIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    w = await _wo_for(db, user, wid, MANAGE)
    await _wo_move(db, user, w, "SUSPENDED", body.reason)
    w.suspendedReason = body.reason
    await _block_roster(db, [w.id], f"Work order suspended: {body.reason}")
    await db.commit()
    await db.refresh(w)
    return await _wo_detail(db, w)


@router.post("/work-orders/{wid}/complete")
async def complete_wo(wid: str, body: NotesIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    w = await _wo_for(db, user, wid, MANAGE)
    await _wo_move(db, user, w, "COMPLETED", body.notes)
    w.completedAt, w.closureNotes = svc.utcnow(), body.notes
    for r in (await db.execute(select(CsmWorkOrderWorker).where(CsmWorkOrderWorker.workOrderId == w.id)
                               .where(CsmWorkOrderWorker.status != "RELEASED"))).scalars().all():
        r.status, r.releasedAt = "RELEASED", svc.utcnow()
    await db.commit()
    await db.refresh(w)
    return await _wo_detail(db, w)


@router.post("/work-orders/{wid}/cancel")
async def cancel_wo(wid: str, body: ReasonIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    w = await _wo_for(db, user, wid, MANAGE)
    await _wo_move(db, user, w, "CANCELLED", body.reason)
    w.closureNotes = body.reason
    for r in (await db.execute(select(CsmWorkOrderWorker).where(CsmWorkOrderWorker.workOrderId == w.id)
                               .where(CsmWorkOrderWorker.status != "RELEASED"))).scalars().all():
        r.status, r.releasedAt = "RELEASED", svc.utcnow()
    await db.commit()
    await db.refresh(w)
    return await _wo_detail(db, w)


# ── roster (plant mobilisation) ──────────────────────────────────────────────
class RosterAddIn(BaseModel):
    contractorWorkerIds: list[str] = Field(min_length=1, max_length=500)
    tradeAtSite: str | None = None


class InductionIn(BaseModel):
    conductedAt: datetime | None = None
    validityDays: int = Field(svc.INDUCTION_VALIDITY_DAYS, ge=1, le=730)
    conductedById: str | None = None


@router.post("/work-orders/{wid}/roster", status_code=201)
async def add_to_roster(wid: str, body: RosterAddIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    w = await _wo_for(db, user, wid, MANAGE)
    if w.status in ("COMPLETED", "CANCELLED"):
        raise HTTPException(409, f"Workers cannot be added to a {w.status.lower()} work order")
    workers = (await db.execute(select(ContractorWorker).where(ContractorWorker.id.in_(body.contractorWorkerIds)))).scalars().all()
    if len(workers) != len(set(body.contractorWorkerIds)):
        raise HTTPException(404, "One or more workers were not found")
    foreign = [wk.fullName for wk in workers if wk.contractorCompanyId != w.contractorCompanyId]
    if foreign:
        raise HTTPException(422, f"Not employed by this work order's contractor: {', '.join(foreign[:5])}")
    existing = {r.contractorWorkerId: r for r in (await db.execute(
        select(CsmWorkOrderWorker).where(CsmWorkOrderWorker.workOrderId == w.id)
    )).scalars().all()}
    for wk in workers:
        r = existing.get(wk.id)
        if r is None:
            db.add(CsmWorkOrderWorker(
                workOrderId=w.id, contractorWorkerId=wk.id, contractorCompanyId=w.contractorCompanyId,
                plantId=w.plantId, tradeAtSite=body.tradeAtSite or wk.primaryTrade, status="PROPOSED", createdBy=user.id,
            ))
        elif r.status == "RELEASED":
            r.status, r.releasedAt = "PROPOSED", None
    await db.commit()
    return await _wo_detail(db, w)


async def _roster_row(db: AsyncSession, w: CsmWorkOrder, rid: str) -> CsmWorkOrderWorker:
    r = await db.get(CsmWorkOrderWorker, rid)
    if r is None or r.workOrderId != w.id:
        raise HTTPException(404, "Roster entry not found")
    return r


@router.post("/work-orders/{wid}/roster/{rid}/induction")
async def record_induction(wid: str, rid: str, body: InductionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    w = await _wo_for(db, user, wid, MANAGE)
    r = await _roster_row(db, w, rid)
    at = body.conductedAt or svc.utcnow()
    if svc.aware(at) > svc.utcnow() + timedelta(hours=1):
        raise HTTPException(422, "An induction cannot be recorded in the future")
    r.inductionAt, r.inductionValidUntil = at, at + timedelta(days=body.validityDays)
    r.inductionById = body.conductedById or user.id
    await db.commit()
    return await _wo_detail(db, w)


@router.post("/work-orders/{wid}/roster/{rid}/clear")
async def clear_worker(wid: str, rid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Run the plant gate clearance for one worker. Clear: a plant gate pass
    number valid until the earliest of work-order end, medical and induction."""
    from app.services import roster_gate

    w = await _wo_for(db, user, wid, MANAGE)
    r = await _roster_row(db, w, rid)
    if r.status == "RELEASED":
        raise HTTPException(409, "This worker has been released from the work order")
    wk = await db.get(ContractorWorker, r.contractorWorkerId)
    c = await _company(db, w.contractorCompanyId)
    result = svc.worker_clearance(
        wo_status=w.status, wo_end=w.endDate, company_status=c.status, qualification=_qualification(c),
        medical_valid_until=wk.currentMedicalValidUntil, worker_status=wk.overallStatus,
        roster_check=roster_gate.for_person(wk).as_check(), induction_valid_until=r.inductionValidUntil,
        same_company=wk.contractorCompanyId == w.contractorCompanyId,
    )
    now = svc.utcnow()
    r.lastCheck, r.lastCheckedAt = result, now
    if result["overall"] == "not_cleared":
        r.status = "BLOCKED"
        r.gatePassValidUntil = None if r.gatePassNo is None else now
    else:
        r.status = "CLEARED"
        if not r.gatePassNo:
            r.gatePassNo = await next_number(db, column=CsmWorkOrderWorker.gatePassNo, stem=f"PGP-{now.year}-", width=5)
        r.gatePassValidUntil = datetime.fromisoformat(result["passValidUntil"])
    await db.commit()
    out = await _wo_detail(db, w)
    out["clearance"] = {"rosterId": r.id, **result}
    return out


@router.post("/work-orders/{wid}/roster/{rid}/release")
async def release_worker(wid: str, rid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    w = await _wo_for(db, user, wid, MANAGE)
    r = await _roster_row(db, w, rid)
    r.status, r.releasedAt = "RELEASED", svc.utcnow()
    if r.gatePassValidUntil and svc.aware(r.gatePassValidUntil) > svc.utcnow():
        r.gatePassValidUntil = svc.utcnow()
    await db.commit()
    return await _wo_detail(db, w)


# ── evaluations ──────────────────────────────────────────────────────────────
class EvaluationIn(BaseModel):
    contractorCompanyId: str
    plantId: str
    evaluationType: str
    periodStart: datetime | None = None
    periodEnd: datetime | None = None
    workOrderId: str | None = None


class ManualIn(BaseModel):
    manual: dict[str, dict[str, Any]] = {}
    evaluatorRemarks: str | None = Field(None, max_length=4000)


def _eval(e: CsmEvaluation, c: ContractorCompany | None, users: dict[str, str], plants: dict[str, str]) -> dict[str, Any]:
    return {
        "id": e.id, "evaluationNo": e.evaluationNo, "contractorCompanyId": e.contractorCompanyId,
        "contractorName": c.name if c else None, "plantId": e.plantId, "plantName": plants.get(e.plantId),
        "workOrderId": e.workOrderId, "evaluationType": e.evaluationType, "periodStart": iso(e.periodStart),
        "periodEnd": iso(e.periodEnd), "kpis": e.kpis or [], "counts": e.counts or {}, "manual": e.manual or {},
        "autoScore": e.autoScore, "manualScore": e.manualScore, "finalScore": e.finalScore, "ratingBand": e.ratingBand,
        "evaluatorRemarks": e.evaluatorRemarks, "status": e.status, "evaluatorId": e.evaluatorId,
        "evaluatorName": users.get(e.evaluatorId or ""), "submittedAt": iso(e.submittedAt),
        "signedOffByName": users.get(e.signedOffById or ""), "signedOffAt": iso(e.signedOffAt),
        "signOffNotes": e.signOffNotes, "createdAt": iso(e.createdAt),
        "fatalityCapApplied": bool((e.counts or {}).get("fatalities")) and (e.finalScore or 0) <= svc.FATALITY_SCORE_CAP,
    }


async def _fill(db: AsyncSession, e: CsmEvaluation) -> None:
    counts = await svc.kpi_counts(db, company_id=e.contractorCompanyId, plant_id=e.plantId, start=e.periodStart, end=e.periodEnd)
    start, end = svc.aware(e.periodStart), svc.aware(e.periodEnd)
    if e.workOrderId:
        w = await db.get(CsmWorkOrder, e.workOrderId)
        if w is not None:
            start, end = max(start, svc.aware(w.startDate)), min(end, svc.aware(w.endDate))
    expected = svc.working_days(start, end)
    e.counts = {**counts, "expectedTbt": expected}
    e.kpis = svc.evaluation_kpis(counts, expected)
    _score(e)


def _score(e: CsmEvaluation) -> None:
    s = svc.evaluation_score(e.kpis or [], e.manual or {}, fatalities=int((e.counts or {}).get("fatalities", 0)))
    e.autoScore, e.manualScore, e.finalScore, e.ratingBand = s["autoScore"], s["manualScore"], s["finalScore"], s["ratingBand"]


async def _eval_for(db: AsyncSession, user: User, eid: str, perm: str) -> CsmEvaluation:
    e = await base.get_live(db, CsmEvaluation, eid)
    await base.require(db, user, perm, e.plantId)
    if perm == READ:
        plants = await base.scoped_plants(db, user, None)
        if plants is not None and e.plantId not in plants:
            raise HTTPException(403, "Plant is outside your access scope")
    return e


async def _eval_out(db: AsyncSession, e: CsmEvaluation) -> dict[str, Any]:
    c = await db.get(ContractorCompany, e.contractorCompanyId)
    users, pl = await base.names(db, [e.evaluatorId, e.signedOffById], [e.plantId])
    out = _eval(e, c, users, pl)
    if e.workOrderId:
        w = await db.get(CsmWorkOrder, e.workOrderId)
        out["workOrderNo"] = w.workOrderNo if w else None
    return out


@router.get("/evaluations")
async def list_evaluations(
    plantId: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    contractorCompanyId: str | None = Query(None), evaluationType: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await base.scoped_plants(db, user, plantId)
    stmt = select(CsmEvaluation, ContractorCompany).join(ContractorCompany, ContractorCompany.id == CsmEvaluation.contractorCompanyId) \
        .where(CsmEvaluation.isDeleted.is_(False)).where(base.plant_where(CsmEvaluation.plantId, plants))
    if status_:
        stmt = stmt.where(CsmEvaluation.status == status_)
    if contractorCompanyId:
        stmt = stmt.where(CsmEvaluation.contractorCompanyId == contractorCompanyId)
    if evaluationType:
        stmt = stmt.where(CsmEvaluation.evaluationType == evaluationType)
    rows = (await db.execute(stmt.order_by(CsmEvaluation.periodEnd.desc()).limit(500))).all()
    users, pl = await base.names(db, [e.evaluatorId for e, _ in rows] + [e.signedOffById for e, _ in rows], [e.plantId for e, _ in rows])
    return {"items": [_eval(e, c, users, pl) for e, c in rows], "total": len(rows)}


@router.post("/evaluations", status_code=201)
async def create_evaluation(body: EvaluationIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await base.require(db, user, EVALUATE, body.plantId)
    base.check_choice(body.evaluationType, svc.EVAL_TYPES, "evaluationType")
    await _company(db, body.contractorCompanyId)
    start, end = body.periodStart, body.periodEnd
    if body.evaluationType == "CONTRACT_END":
        if not body.workOrderId:
            raise HTTPException(422, "A contract-end evaluation needs the work order")
        w = await base.get_live(db, CsmWorkOrder, body.workOrderId)
        if w.contractorCompanyId != body.contractorCompanyId or w.plantId != body.plantId:
            raise HTTPException(422, "That work order belongs to another contractor or plant")
        start = start or w.startDate
        end = end or min(svc.aware(w.endDate), svc.utcnow())
    if start is None or end is None:
        d_start, d_end = svc.previous_period(body.evaluationType)
        start, end = start or d_start, end or d_end
    if svc.aware(end) <= svc.aware(start):
        raise HTTPException(422, "Period end must be after period start")
    if svc.aware(start) > svc.utcnow():
        raise HTTPException(422, "The evaluation period has not started yet")
    dup = (await db.execute(
        select(CsmEvaluation).where(CsmEvaluation.contractorCompanyId == body.contractorCompanyId)
        .where(CsmEvaluation.plantId == body.plantId).where(CsmEvaluation.evaluationType == body.evaluationType)
        .where(CsmEvaluation.periodStart == start).where(CsmEvaluation.periodEnd == end)
        .where(CsmEvaluation.isDeleted.is_(False))
    )).scalars().first()
    if dup is not None:
        raise HTTPException(409, f"{dup.evaluationNo} already covers this contractor and period")
    no = await next_number(db, column=CsmEvaluation.evaluationNo, stem=f"CEV-{svc.aware(end).year}-")
    e = CsmEvaluation(
        evaluationNo=no, contractorCompanyId=body.contractorCompanyId, plantId=body.plantId,
        workOrderId=body.workOrderId, evaluationType=body.evaluationType, periodStart=start, periodEnd=end,
        manual={}, status="DRAFT", evaluatorId=user.id, createdBy=user.id,
    )
    await _fill(db, e)
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return await _eval_out(db, e)


@router.get("/evaluations/{eid}")
async def get_evaluation(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await _eval_out(db, await _eval_for(db, user, eid, READ))


@router.put("/evaluations/{eid}/manual")
async def save_manual(eid: str, body: ManualIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _eval_for(db, user, eid, EVALUATE)
    if e.status != "DRAFT":
        raise HTTPException(409, "Only a draft evaluation can be edited")
    codes = {c["code"] for c in svc.MANUAL_CRITERIA}
    manual = dict(e.manual or {})
    for code, v in body.manual.items():
        if code not in codes:
            raise HTTPException(422, f"Unknown criterion {code}")
        s = (v or {}).get("score")
        if s is not None and (not isinstance(s, (int, float)) or int(s) != s or not 1 <= s <= 5):
            raise HTTPException(422, f"{code}: score must be a whole number from 1 to 5")
        manual[code] = {"score": int(s) if s is not None else None, "comment": ((v or {}).get("comment") or "")[:1000] or None}
    e.manual = manual
    if body.evaluatorRemarks is not None:
        e.evaluatorRemarks = body.evaluatorRemarks
    _score(e)
    await db.commit()
    await db.refresh(e)
    return await _eval_out(db, e)


@router.post("/evaluations/{eid}/refresh")
async def refresh_evaluation(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _eval_for(db, user, eid, EVALUATE)
    if e.status != "DRAFT":
        raise HTTPException(409, "KPIs are frozen once the evaluation is submitted")
    await _fill(db, e)
    await db.commit()
    await db.refresh(e)
    return await _eval_out(db, e)


@router.post("/evaluations/{eid}/submit")
async def submit_evaluation(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _eval_for(db, user, eid, EVALUATE)
    base.conflict(svc.eval_submit_blockers(status=e.status, manual=e.manual or {}))
    await _fill(db, e)  # freeze the latest numbers at submission
    e.status, e.submittedAt, e.evaluatorId = "SUBMITTED", svc.utcnow(), user.id
    await db.commit()
    await db.refresh(e)
    return await _eval_out(db, e)


@router.post("/evaluations/{eid}/sign-off")
async def sign_off(eid: str, body: NotesIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _eval_for(db, user, eid, APPROVE)
    base.conflict(svc.eval_signoff_blockers(status=e.status, evaluator_id=e.evaluatorId, signer_id=user.id))
    e.status, e.signedOffById, e.signedOffAt, e.signOffNotes = "SIGNED_OFF", user.id, svc.utcnow(), body.notes
    await db.commit()
    await db.refresh(e)
    return await _eval_out(db, e)


@router.post("/evaluations/{eid}/return")
async def return_evaluation(eid: str, body: ReasonIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _eval_for(db, user, eid, APPROVE)
    if e.status != "SUBMITTED":
        raise HTTPException(409, "Only a submitted evaluation can be returned")
    e.status, e.signOffNotes = "DRAFT", f"Returned: {body.reason}"
    await db.commit()
    await db.refresh(e)
    return await _eval_out(db, e)


# ── sanctions ────────────────────────────────────────────────────────────────
class SanctionIn(BaseModel):
    contractorCompanyId: str
    plantId: str
    action: str
    reasonCategory: str
    reason: str = Field(min_length=10, max_length=4000)
    evidence: list[dict[str, Any]] = []
    effectiveUntil: datetime | None = None


def _sanction(s: CsmSanction, c: ContractorCompany | None, users: dict[str, str], plants: dict[str, str]) -> dict[str, Any]:
    return {
        "id": s.id, "sanctionNo": s.sanctionNo, "contractorCompanyId": s.contractorCompanyId,
        "contractorName": c.name if c else None, "contractorStatus": c.status if c else None,
        "plantId": s.plantId, "plantName": plants.get(s.plantId), "action": s.action,
        "reasonCategory": s.reasonCategory, "reason": s.reason, "evidence": s.evidence or [],
        "effectiveUntil": iso(s.effectiveUntil), "status": s.status, "previousStatus": s.previousStatus,
        "requestedById": s.requestedById, "requestedByName": users.get(s.requestedById),
        "requestedAt": iso(s.requestedAt), "decidedByName": users.get(s.decidedById or ""),
        "decidedAt": iso(s.decidedAt), "decisionNotes": s.decisionNotes,
    }


async def _sanction_out(db: AsyncSession, s: CsmSanction) -> dict[str, Any]:
    c = await db.get(ContractorCompany, s.contractorCompanyId)
    users, pl = await base.names(db, [s.requestedById, s.decidedById], [s.plantId])
    return _sanction(s, c, users, pl)


@router.get("/sanctions")
async def list_sanctions(
    plantId: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    contractorCompanyId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await base.scoped_plants(db, user, plantId)
    stmt = select(CsmSanction, ContractorCompany).join(ContractorCompany, ContractorCompany.id == CsmSanction.contractorCompanyId) \
        .where(base.plant_where(CsmSanction.plantId, plants))
    if status_:
        stmt = stmt.where(CsmSanction.status == status_)
    if contractorCompanyId:
        stmt = stmt.where(CsmSanction.contractorCompanyId == contractorCompanyId)
    rows = (await db.execute(stmt.order_by(CsmSanction.requestedAt.desc()).limit(500))).all()
    users, pl = await base.names(db, [s.requestedById for s, _ in rows] + [s.decidedById for s, _ in rows], [s.plantId for s, _ in rows])
    return {"items": [_sanction(s, c, users, pl) for s, c in rows], "total": len(rows)}


async def _pending(db: AsyncSession, company_id: str) -> bool:
    return (await db.execute(
        select(CsmSanction.id).where(CsmSanction.contractorCompanyId == company_id)
        .where(CsmSanction.status == "PENDING_APPROVAL")
    )).first() is not None


@router.post("/sanctions", status_code=201)
async def request_sanction(body: SanctionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await base.require(db, user, MANAGE, body.plantId)
    base.check_choice(body.reasonCategory, svc.SANCTION_REASONS, "reasonCategory")
    c = await _company(db, body.contractorCompanyId)
    base.conflict(svc.sanction_request_blockers(action=body.action, current_status=c.status, reason=body.reason,
                                                pending=await _pending(db, c.id)))
    if body.effectiveUntil and body.action != "SUSPEND":
        raise HTTPException(422, "An end date applies only to a suspension")
    now = svc.utcnow()
    s = CsmSanction(
        sanctionNo=await next_number(db, column=CsmSanction.sanctionNo, stem=f"CSA-{now.year}-"),
        contractorCompanyId=c.id, plantId=body.plantId, action=body.action, reasonCategory=body.reasonCategory,
        reason=body.reason.strip(), evidence=body.evidence[:50], effectiveUntil=body.effectiveUntil,
        status="PENDING_APPROVAL", previousStatus=c.status or "ACTIVE", requestedById=user.id, requestedAt=now,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return await _sanction_out(db, s)


@router.post("/sanctions/{sid}/approve")
async def approve_sanction(sid: str, body: NotesIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Plant head / corporate HSE approval. Applies the status to the
    ContractorCompany and the consequences: live work orders suspended,
    cleared workers blocked, today's EPC gate passes revoked; a reinstatement
    from blacklist resets the prequalification and opens a renewal."""
    s = await db.get(CsmSanction, sid)
    if s is None:
        raise HTTPException(404, "Sanction not found")
    await base.require(db, user, APPROVE, s.plantId)
    base.conflict(svc.sanction_decision_blockers(status=s.status, requested_by=s.requestedById, decided_by=user.id))
    c = await _company(db, s.contractorCompanyId)
    base.conflict(svc.sanction_request_blockers(action=s.action, current_status=c.status, reason=s.reason, pending=False))
    now = svc.utcnow()
    target = svc.sanction_target_status(s.action)
    previous = (c.status or "ACTIVE").upper()
    s.status, s.decidedById, s.decidedAt, s.decisionNotes, s.previousStatus = "APPROVED", user.id, now, body.notes, previous
    c.status = target
    c.suspensionHistory = [*(c.suspensionHistory or []), {
        "action": s.action, "fromStatus": previous, "toStatus": target, "sanctionNo": s.sanctionNo,
        "reasonCategory": s.reasonCategory, "reason": s.reason, "effectiveUntil": iso(s.effectiveUntil),
        "requestedById": s.requestedById, "approvedById": user.id, "at": now.isoformat(), "source": "CSM",
    }]
    if s.action in ("SUSPEND", "BLACKLIST"):
        live = (await db.execute(
            select(CsmWorkOrder).where(CsmWorkOrder.contractorCompanyId == c.id).where(CsmWorkOrder.status == "ACTIVE")
            .where(CsmWorkOrder.isDeleted.is_(False))
        )).scalars().all()
        for w in live:
            base.add_history(w, user, w.status, "SUSPENDED", f"Contractor {target.lower()} ({s.sanctionNo})")
            w.status, w.suspendedReason = "SUSPENDED", f"Contractor {target.lower()} ({s.sanctionNo})"
        await _block_roster(db, [w.id for w in live], f"Contractor {target.lower()} ({s.sanctionNo})")
        worker_ids = (await db.execute(select(ContractorWorker.id).where(ContractorWorker.contractorCompanyId == c.id))).scalars().all()
        for gp in (await db.execute(
            select(GatePass).where(GatePass.contractorWorkerId.in_(worker_ids or ["__none__"])).where(GatePass.status == "active")
        )).scalars().all():
            gp.status, gp.revokedAt, gp.revokedById = "revoked", now, user.id
            gp.revocationReason = f"Contractor {target.lower()} ({s.sanctionNo})"
    if s.action == "REINSTATE" and previous == "BLACKLISTED":
        # Coming back from a blacklist is a fresh start: requalify first.
        c.prequalificationStatus, c.prequalificationValidUntil = "not_applied", None
        for p in (await db.execute(select(CsmPrequalification).where(CsmPrequalification.contractorCompanyId == c.id)
                                   .where(CsmPrequalification.isCurrent.is_(True)))).scalars().all():
            p.isCurrent = False
        try:
            async with db.begin_nested():
                t = await base.active_template(db)
                await base._new_application(db, user, company=c, plant_id=s.plantId, template=t, renewal_of=None)
        except HTTPException:
            pass  # no active template or an application already open: HSE starts it by hand
    await db.commit()
    await db.refresh(s)
    return await _sanction_out(db, s)


@router.post("/sanctions/{sid}/reject")
async def reject_sanction(sid: str, body: ReasonIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await db.get(CsmSanction, sid)
    if s is None:
        raise HTTPException(404, "Sanction not found")
    await base.require(db, user, APPROVE, s.plantId)
    if s.status != "PENDING_APPROVAL":
        raise HTTPException(409, f"This request is already {s.status.lower().replace('_', ' ')}")
    s.status, s.decidedById, s.decidedAt, s.decisionNotes = "REJECTED", user.id, svc.utcnow(), body.reason
    await db.commit()
    await db.refresh(s)
    return await _sanction_out(db, s)
