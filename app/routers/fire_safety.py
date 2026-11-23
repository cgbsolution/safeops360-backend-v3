"""Fire Safety & Emergency Response API (P1-4).

Equipment lifecycle, assembly points, emergency plans, drills (with the MAJOR_GAP
completion gate), QR-code inspections against versioned checklist templates,
crisis escalation and the FSER panel. Plant-scoped via QueryScope.

NB: gated by the FIRE licence module in the model (registry/editions); the router is
mounted always-on in dev because the unsigned dev licence predates the FIRE code —
add "fire_safety": "FIRE" to ROUTER_MODULE once a FIRE-inclusive licence is issued.

RBAC (FIRE.* granted by safeops_360/prisma/apply-fire-inspection-permissions.ts):
  reads           FIRE.READ or the legacy INCIDENT.READ (either grants the read;
                  the plant scope is the union of the two)
  FIRE.INSPECT    perform an inspection, upload item photos (field inspectors)
  FIRE.MANAGE     register / edit equipment, out-of-service, return-to-service,
                  edit checklist templates. Equipment endpoints also still accept
                  INCIDENT.UPDATE, which is what they required before FIRE.* existed;
                  templates need FIRE.MANAGE.
  drills, plans, crisis escalation: unchanged, INCIDENT.UPDATE.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.numbering import next_number
from app.core.deps import get_current_user
from app.core.db import get_db
from app.models.cams import CamsEngagement
from app.models.fire_safety import (
    AssemblyPoint, FireChecklistTemplate, FireDrill, FireDrillFinding, FireEmergencyPlan, FireEquipment,
    FireEquipmentEvent, FireIncidentLink, FireInspection, FireInspectionItem,
)
from app.models.plant import Plant
from app.models.user import User
from app.services import fire_inspection as fi
from app.services import fire_safety as svc
from app.services.access_scope import QueryScope, build_query_scope
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names
from app.services.storage import (
    build_evidence_storage_path, create_signed_download_url, create_signed_upload_url, is_storage_configured,
)
from app.services.user_directory import resolve_user_directory

router = APIRouter(prefix="/api/fire", tags=["fire-safety"])

_READ = "INCIDENT.READ"
_WRITE = "INCIDENT.UPDATE"
FIRE_READ, FIRE_INSPECT, FIRE_MANAGE = "FIRE.READ", "FIRE.INSPECT", "FIRE.MANAGE"
READ_ANY = (FIRE_READ, _READ)
MANAGE_ANY = (FIRE_MANAGE, _WRITE)
INSPECT_ANY = (FIRE_INSPECT, FIRE_MANAGE)

PHOTO_MAX_BYTES = 10 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _require_any(db: AsyncSession, user: User, perms: tuple[str, ...], plant_id: str | None = None) -> str:
    """Pass if the caller holds any of ``perms`` (for this plant). Returns the
    code that matched."""
    reason = None
    for perm in perms:
        res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
        if res.allowed:
            return perm
        reason = reason or res.reason
    raise HTTPException(status.HTTP_403_FORBIDDEN, reason or f"Requires one of: {', '.join(perms)}")


async def _read_scope(db: AsyncSession, user: User) -> QueryScope:
    """Union of the caller's FIRE.READ and INCIDENT.READ plant scopes. Refuses
    (403) when neither is held."""
    scopes: list[QueryScope] = []
    for perm in READ_ANY:
        res = await can(db, user.id, perm, PermissionContext())
        if res.allowed:
            scopes.append(await build_query_scope(db, user.id, perm))
    if not scopes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requires FIRE.READ or INCIDENT.READ")
    if any(s.all_plants for s in scopes):
        return QueryScope(actor_id=user.id, all_plants=True, plant_ids=[])
    plants: list[str] = []
    for s in scopes:
        plants.extend(p for p in s.plant_ids if p not in plants)
    return QueryScope(actor_id=user.id, all_plants=False, plant_ids=plants)


async def _scoped_plant(db: AsyncSession, user: User, plant_id: str | None) -> QueryScope:
    scope = await _read_scope(db, user)
    if plant_id and not scope.allows_plant(plant_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant is outside your access scope")
    return scope


async def _names(db: AsyncSession, user_ids, plant_ids) -> tuple[dict[str, str], dict[str, str]]:
    users = await resolve_user_directory(db, user_ids)
    plants = await resolve_plant_names(db, plant_ids)
    return {k: v.name for k, v in users.items()}, plants


async def _get_equipment(db: AsyncSession, eid: str) -> FireEquipment:
    e = await db.get(FireEquipment, eid)
    if not e or e.isDeleted:
        raise HTTPException(404, "Equipment not found")
    return e


def _eq(e: FireEquipment, plants: dict[str, str] | None = None) -> dict[str, Any]:
    live = svc.compute_status(e)  # due-date statuses move daily; don't wait for the nightly job
    return {
        "id": e.id, "equipmentCode": e.equipmentCode, "type": e.type, "make": e.make, "model": e.model,
        "serialNo": e.serialNo, "location": e.location, "buildingId": e.buildingId, "plantId": e.plantId,
        "plantName": (plants or {}).get(e.plantId),
        "latitude": e.latitude, "longitude": e.longitude, "floorLevel": e.floorLevel,
        "installationDate": _iso(e.installationDate),
        "lastInspectionDate": _iso(e.lastInspectionDate),
        "nextInspectionDueDate": _iso(e.nextInspectionDueDate),
        "inspectionFrequencyDays": e.inspectionFrequencyDays, "status": live, "capacitySpec": e.capacitySpec,
        "maintenanceContractor": e.maintenanceContractor, "qrCode": e.qrCode, "isActive": e.isActive,
        "outOfServiceReason": e.outOfServiceReason,
        "dueBucket": fi.due_bucket(live, e.nextInspectionDueDate, soon_days=svc.due_soon_days(e.inspectionFrequencyDays)),
    }


def _log(db: AsyncSession, e: FireEquipment, event_type: str, *, before: str | None, after: str | None,
         note: str | None, actor_id: str | None, inspection_id: str | None = None) -> None:
    db.add(FireEquipmentEvent(
        equipmentId=e.id, plantId=e.plantId, eventType=event_type, fromStatus=before, toStatus=after,
        note=note, inspectionId=inspection_id, actorId=actor_id,
    ))


# ── Meta ─────────────────────────────────────────────────────────────────────
@router.get("/meta")
async def meta(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "equipmentTypes": fi.EQUIPMENT_TYPES, "equipmentStatuses": fi.EQUIPMENT_STATUSES,
        "responses": fi.RESPONSES, "results": fi.RESULTS, "defaultFrequencyDays": fi.DEFAULT_FREQUENCY_DAYS,
    }


# ── Dashboard (FS-01) ────────────────────────────────────────────────────────
@router.get("/dashboard")
async def dashboard(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    scope = await _read_scope(db, user)
    eq = (await db.execute(scope.apply(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)).where(FireEquipment.isActive.is_(True)), FireEquipment))).scalars().all()
    by_status: dict[str, int] = {}
    live = {e.id: svc.compute_status(e) for e in eq}
    for e in eq:
        by_status[live[e.id]] = by_status.get(live[e.id], 0) + 1
    now = _now()
    soon = now + timedelta(days=svc.DUE_SOON_DAYS)
    due_month = sum(1 for e in eq if e.nextInspectionDueDate and svc._aware(e.nextInspectionDueDate) <= soon and svc._aware(e.nextInspectionDueDate) >= now)
    overdue = by_status.get("OVERDUE", 0)
    drills = (await db.execute(scope.apply(select(FireDrill).where(FireDrill.isDeleted.is_(False)), FireDrill))).scalars().all()
    year_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    drills_done = sum(1 for d in drills if d.status == "COMPLETED" and d.conductedDate and svc._aware(d.conductedDate) >= year_start)
    drills_due = sum(1 for d in drills if d.status == "PLANNED")
    plans = (await db.execute(scope.apply(select(FireEmergencyPlan).where(FireEmergencyPlan.isDeleted.is_(False)), FireEmergencyPlan))).scalars().all()
    plans_review_due = sum(1 for p in plans if p.nextReviewDate and svc._aware(p.nextReviewDate) < now)
    since = now - timedelta(days=30)
    insp_rows = (
        await db.execute(scope.apply(select(FireInspection.result, FireInspection.criticalFailCount, FireInspection.scanned)
                                     .where(FireInspection.completedAt >= since), FireInspection))
    ).all()
    return {
        "totalEquipment": len(eq), "byStatus": by_status, "dueThisMonth": due_month, "overdue": overdue,
        "drillsCompletedThisYear": drills_done, "drillsDue": drills_due, "plansReviewDue": plans_review_due,
        "defective": by_status.get("DEFECTIVE", 0), "outOfService": by_status.get("OUT_OF_SERVICE", 0),
        "inspectionsLast30": len(insp_rows),
        "failedLast30": sum(1 for r in insp_rows if r[0] == "FAIL"),
        "criticalFailsLast30": sum(1 for r in insp_rows if (r[1] or 0) > 0),
        "scannedLast30": sum(1 for r in insp_rows if r[2]),
        "overdueItems": [_eq(e) for e in eq if live[e.id] == "OVERDUE"][:25],
    }


# ── Equipment register (FS-02/03) ────────────────────────────────────────────
@router.get("/equipment")
async def list_equipment(
    estatus: str | None = Query(None, alias="status"), etype: str | None = Query(None, alias="type"),
    buildingId: str | None = Query(None), dueOnly: bool = Query(False), plantId: str | None = Query(None),
    q: str | None = Query(None), includeInactive: bool = Query(False),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    scope = await _scoped_plant(db, user, plantId)
    stmt = scope.apply(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)), FireEquipment)
    if not includeInactive:
        stmt = stmt.where(FireEquipment.isActive.is_(True))
    if plantId:
        stmt = stmt.where(FireEquipment.plantId == plantId)
    if etype:
        stmt = stmt.where(FireEquipment.type == etype)
    if buildingId:
        stmt = stmt.where(FireEquipment.buildingId == buildingId)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(FireEquipment.equipmentCode.ilike(like), FireEquipment.location.ilike(like),
                              FireEquipment.serialNo.ilike(like)))
    rows = (await db.execute(stmt.order_by(FireEquipment.nextInspectionDueDate.asc().nulls_first(), FireEquipment.equipmentCode))).scalars().all()
    # Status filters run on the live status, not the nightly-recomputed column.
    _, plants = await _names(db, [], [r.plantId for r in rows])
    items = [_eq(e, plants) for e in rows]
    if estatus:
        items = [i for i in items if i["status"] == estatus]
    if dueOnly:
        items = [i for i in items if i["status"] in ("DUE_INSPECTION", "OVERDUE")]
    return {"items": items, "total": len(items)}


class EquipmentCreate(BaseModel):
    type: str
    location: str = Field(min_length=2, max_length=200)
    plantId: str
    buildingId: str | None = None
    make: str | None = None
    model: str | None = None
    serialNo: str | None = None
    capacitySpec: str | None = None
    inspectionFrequencyDays: int | None = Field(None, ge=1, le=730)
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    floorLevel: int | None = None
    maintenanceContractor: str | None = None
    installationDate: datetime | None = None
    lastInspectionDate: datetime | None = None


class EquipmentPatch(BaseModel):
    type: str | None = None
    location: str | None = Field(None, min_length=2, max_length=200)
    buildingId: str | None = None
    make: str | None = None
    model: str | None = None
    serialNo: str | None = None
    capacitySpec: str | None = None
    inspectionFrequencyDays: int | None = Field(None, ge=1, le=730)
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    floorLevel: int | None = None
    maintenanceContractor: str | None = None
    installationDate: datetime | None = None


async def _next_code(db: AsyncSession, plant_id: str) -> str:
    plant = await db.get(Plant, plant_id)
    tag = (plant.code if plant and plant.code else plant_id[:4]).upper()
    return await next_number(db, column=FireEquipment.equipmentCode, stem=f"FE-{tag}-")


def _check_type(t: str | None) -> None:
    if t is not None and t not in fi.EQUIPMENT_TYPES:
        raise HTTPException(422, f"type must be one of {', '.join(fi.EQUIPMENT_TYPES)}")


@router.post("/equipment", status_code=201)
async def create_equipment(body: EquipmentCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_any(db, user, MANAGE_ANY, plant_id=body.plantId)
    _check_type(body.type)
    if body.lastInspectionDate and svc._aware(body.lastInspectionDate) > _now() + timedelta(hours=1):
        raise HTTPException(422, "Last inspection date cannot be in the future")
    freq = body.inspectionFrequencyDays or fi.DEFAULT_FREQUENCY_DAYS.get(body.type, 30)
    code = await _next_code(db, body.plantId)
    e = FireEquipment(
        equipmentCode=code, type=body.type, location=body.location.strip(), plantId=body.plantId, buildingId=body.buildingId,
        make=body.make, model=body.model, serialNo=body.serialNo, capacitySpec=body.capacitySpec,
        inspectionFrequencyDays=freq, latitude=body.latitude, longitude=body.longitude,
        floorLevel=body.floorLevel, maintenanceContractor=body.maintenanceContractor, installationDate=body.installationDate,
        lastInspectionDate=body.lastInspectionDate,
        nextInspectionDueDate=fi.next_due(body.lastInspectionDate, freq) if body.lastInspectionDate else None,
        qrCode=f"SAFEOPS-FIRE-{code}", createdBy=user.id, status="ACTIVE", isActive=True,
    )
    e.status = svc.compute_status(e)
    db.add(e)
    await db.flush()
    _log(db, e, "CREATED", before=None, after=e.status, note=None, actor_id=user.id)
    await db.commit()
    await db.refresh(e)
    return _eq(e)


@router.patch("/equipment/{eid}")
async def update_equipment(eid: str, body: EquipmentPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _get_equipment(db, eid)
    await _require_any(db, user, MANAGE_ANY, plant_id=e.plantId)
    data = body.model_dump(exclude_unset=True)
    _check_type(data.get("type"))
    if "location" in data and not (data["location"] or "").strip():
        raise HTTPException(422, "location cannot be empty")
    changed: list[str] = []
    for k, v in data.items():
        if k in ("type", "location", "inspectionFrequencyDays") and v is None:
            continue  # required columns: null means "leave as is"
        if getattr(e, k) != v:
            setattr(e, k, v.strip() if isinstance(v, str) and k == "location" else v)
            changed.append(k)
    if "inspectionFrequencyDays" in changed and e.lastInspectionDate:
        e.nextInspectionDueDate = fi.next_due(e.lastInspectionDate, e.inspectionFrequencyDays)
    before = e.status
    e.status = svc.compute_status(e)
    e.updatedBy = user.id
    if changed:
        _log(db, e, "UPDATED", before=before, after=e.status, note="Changed: " + ", ".join(changed), actor_id=user.id)
    await db.commit()
    await db.refresh(e)
    return _eq(e)


async def _last_inspection(db: AsyncSession, eid: str) -> FireInspection | None:
    return (
        await db.execute(select(FireInspection).where(FireInspection.equipmentId == eid)
                         .order_by(FireInspection.completedAt.desc()).limit(1))
    ).scalar_one_or_none()


async def _active_template(db: AsyncSession, equipment_type: str) -> FireChecklistTemplate | None:
    return (
        await db.execute(
            select(FireChecklistTemplate).where(FireChecklistTemplate.equipmentType == equipment_type)
            .where(FireChecklistTemplate.isActive.is_(True)).where(FireChecklistTemplate.isDeleted.is_(False))
            .order_by(FireChecklistTemplate.version.desc()).limit(1)
        )
    ).scalar_one_or_none()


@router.get("/equipment/by-code/{code}")
async def equipment_by_code(code: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """What a QR scan opens: the asset, its current checklist and its last
    inspection. Accepts the bare code or the legacy SAFEOPS-FIRE-<code> payload."""
    raw = code.strip()
    if raw.upper().startswith("SAFEOPS-FIRE-"):
        raw = raw[len("SAFEOPS-FIRE-"):]
    e = (
        await db.execute(select(FireEquipment).where(func.upper(FireEquipment.equipmentCode) == raw.upper())
                         .where(FireEquipment.isDeleted.is_(False)))
    ).scalar_one_or_none()
    if e is None:
        raise HTTPException(404, f"No fire equipment with code {raw}")
    await _require_any(db, user, READ_ANY, plant_id=e.plantId)
    tpl = await _active_template(db, e.type)
    last = await _last_inspection(db, e.id)
    users, plants = await _names(db, [last.inspectorId if last else None], [e.plantId])
    can_inspect = False
    for perm in INSPECT_ANY:
        if (await can(db, user.id, perm, PermissionContext(plant_id=e.plantId))).allowed:
            can_inspect = True
            break
    return {
        "equipment": _eq(e, plants),
        "template": _tpl(tpl) if tpl else None,
        "lastInspection": _insp(last, users) if last else None,
        "canInspect": can_inspect,
        "blockers": fi.inspection_blockers(status=e.status, is_active=e.isActive, is_deleted=e.isDeleted),
    }


@router.get("/equipment/{eid}")
async def get_equipment(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _get_equipment(db, eid)
    await _require_any(db, user, READ_ANY, plant_id=e.plantId)
    inspections = (
        await db.execute(select(FireInspection).where(FireInspection.equipmentId == eid)
                         .order_by(FireInspection.completedAt.desc()).limit(200))
    ).scalars().all()
    events = (
        await db.execute(select(FireEquipmentEvent).where(FireEquipmentEvent.equipmentId == eid)
                         .order_by(FireEquipmentEvent.createdAt.desc()).limit(200))
    ).scalars().all()
    # Older inspections ran as template-less CAMS engagements (sourceModule=FIRE).
    legacy = (
        await db.execute(
            select(CamsEngagement).where(CamsEngagement.sourceModule == "FIRE").where(CamsEngagement.sourceEntityId == eid)
            .order_by(CamsEngagement.plannedDate.desc())
        )
    ).scalars().all()
    tpl = await _active_template(db, e.type)
    users, plants = await _names(
        db, [i.inspectorId for i in inspections] + [ev.actorId for ev in events] + [e.createdBy], [e.plantId],
    )
    return {
        **_eq(e, plants),
        "createdAt": _iso(e.createdAt), "createdByName": users.get(e.createdBy or ""),
        "template": _tpl(tpl, with_items=False) if tpl else None,
        "inspections": [_insp(i, users) for i in inspections],
        "events": [
            {"id": ev.id, "eventType": ev.eventType, "fromStatus": ev.fromStatus, "toStatus": ev.toStatus,
             "note": ev.note, "inspectionId": ev.inspectionId, "actorName": users.get(ev.actorId or ""),
             "createdAt": _iso(ev.createdAt)}
            for ev in events
        ],
        "legacyInspections": [
            {"id": i.id, "engagementCode": i.engagementCode, "title": i.title, "status": i.status,
             "plannedDate": _iso(i.plannedDate), "scorePercent": i.scorePercent}
            for i in legacy
        ],
    }


class OutOfServiceBody(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


class ReturnToServiceBody(BaseModel):
    note: str = Field(min_length=3, max_length=1000)


async def _status_since(db: AsyncSession, eid: str) -> datetime | None:
    """When the asset last went out of service / defective (event log)."""
    return (
        await db.execute(
            select(func.max(FireEquipmentEvent.createdAt)).where(FireEquipmentEvent.equipmentId == eid)
            .where(FireEquipmentEvent.eventType.in_(("OUT_OF_SERVICE", "MARKED_DEFECTIVE")))
        )
    ).scalar()


@router.post("/equipment/{eid}/out-of-service")
async def out_of_service(eid: str, body: OutOfServiceBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _get_equipment(db, eid)
    await _require_any(db, user, MANAGE_ANY, plant_id=e.plantId)
    if e.status == "DECOMMISSIONED":
        raise HTTPException(409, "A decommissioned asset cannot be taken out of service")
    if e.status == "OUT_OF_SERVICE":
        raise HTTPException(409, "This asset is already out of service")
    before = svc.compute_status(e)
    e.status = "OUT_OF_SERVICE"
    e.outOfServiceReason = body.reason.strip()
    e.updatedBy = user.id
    _log(db, e, "OUT_OF_SERVICE", before=before, after=e.status, note=e.outOfServiceReason, actor_id=user.id)
    await db.commit()
    await db.refresh(e)
    return _eq(e)


@router.post("/equipment/{eid}/return-to-service")
async def return_to_service(eid: str, body: ReturnToServiceBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Bring an OUT_OF_SERVICE / DEFECTIVE asset back. Needs a passing inspection
    (no critical failure) recorded after it went out of service."""
    e = await _get_equipment(db, eid)
    await _require_any(db, user, MANAGE_ANY, plant_id=e.plantId)
    last = await _last_inspection(db, e.id)
    blockers = fi.return_to_service_blockers(
        status=e.status, since=await _status_since(db, e.id),
        last_inspection=(last.completedAt, last.criticalFailCount) if last else None,
    )
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    before = e.status
    e.status = "ACTIVE"
    e.status = svc.compute_status(e)
    e.outOfServiceReason = None
    e.updatedBy = user.id
    _log(db, e, "RETURNED_TO_SERVICE", before=before, after=e.status, note=body.note.strip(), actor_id=user.id,
         inspection_id=last.id if last else None)
    await db.commit()
    await db.refresh(e)
    return _eq(e)


@router.post("/equipment/{eid}/trigger-inspection", status_code=201)
async def trigger_inspection(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Legacy: create a template-less CAMS inspection engagement for this asset
    (sourceModule='FIRE'). The QR checklist (POST /inspections) replaces it."""
    e = await _get_equipment(db, eid)
    await _require(db, user, _WRITE, plant_id=e.plantId)
    eng_code = await next_number(db, column=CamsEngagement.engagementCode, stem=f"FIRE-INSP-{_now().year}-")
    eng = CamsEngagement(
        engagementCode=eng_code,
        title=f"Fire equipment inspection — {e.equipmentCode} ({e.type})",
        engagementType="inspection", siteId=e.plantId, leadAuditorId=user.id,
        plannedDate=_now(), status="PLANNED", sourceModule="FIRE", sourceEntityId=e.id,
    )
    db.add(eng)
    await db.commit()
    await db.refresh(eng)
    return {"ok": True, "engagementId": eng.id, "engagementCode": eng.engagementCode}


@router.post("/recompute-status")
async def recompute(plantId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_any(db, user, MANAGE_ANY)
    res = await svc.recompute_all_statuses(db, plant_id=plantId)
    await db.commit()
    return res


@router.get("/equipment-due")
async def equipment_due(days: int = Query(30, ge=1, le=365), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """FS-09 — 'all equipment due within N days' (regulator-ready)."""
    scope = await _read_scope(db, user)
    horizon = _now() + timedelta(days=days)
    stmt = scope.apply(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)).where(FireEquipment.isActive.is_(True)).where(FireEquipment.nextInspectionDueDate <= horizon), FireEquipment)
    rows = (await db.execute(stmt.order_by(FireEquipment.nextInspectionDueDate.asc()))).scalars().all()
    return {"items": [_eq(e) for e in rows], "total": len(rows), "windowDays": days}


# ── Checklist templates ──────────────────────────────────────────────────────
def _tpl(t: FireChecklistTemplate, with_items: bool = True) -> dict[str, Any]:
    items = list(t.items or [])
    out = {
        "id": t.id, "code": t.code, "version": t.version, "equipmentType": t.equipmentType, "name": t.name,
        "standardRef": t.standardRef, "description": t.description, "frequencyDays": t.frequencyDays,
        "isActive": t.isActive, "itemCount": len(items), "criticalCount": sum(1 for i in items if i.get("critical")),
        "updatedAt": _iso(t.updatedAt), "createdAt": _iso(t.createdAt),
    }
    if with_items:
        out["items"] = items
    return out


class TemplateItemIn(BaseModel):
    key: str | None = None
    text: str
    critical: bool = False
    guidance: str | None = None


class TemplateIn(BaseModel):
    equipmentType: str
    name: str = Field(min_length=3, max_length=200)
    code: str | None = Field(None, max_length=60)
    standardRef: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=2000)
    frequencyDays: int | None = Field(None, ge=1, le=730)
    items: list[TemplateItemIn]


class TemplateEdit(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    standardRef: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=2000)
    frequencyDays: int | None = Field(None, ge=1, le=730)
    items: list[TemplateItemIn]


def _clean_items(items: list[TemplateItemIn]) -> list[dict[str, Any]]:
    try:
        return fi.normalise_items([i.model_dump() for i in items])
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from ex


@router.get("/templates")
async def list_templates(
    equipmentType: str | None = Query(None), includeHistory: bool = Query(False),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _read_scope(db, user)
    stmt = select(FireChecklistTemplate).where(FireChecklistTemplate.isDeleted.is_(False))
    if not includeHistory:
        stmt = stmt.where(FireChecklistTemplate.isActive.is_(True))
    if equipmentType:
        stmt = stmt.where(FireChecklistTemplate.equipmentType == equipmentType)
    rows = (await db.execute(stmt.order_by(FireChecklistTemplate.equipmentType, FireChecklistTemplate.version.desc()))).scalars().all()
    covered = {r.equipmentType for r in rows if r.isActive}
    return {
        "items": [_tpl(r) for r in rows], "total": len(rows),
        "typesWithoutChecklist": [t for t in fi.EQUIPMENT_TYPES if t not in covered] if not equipmentType else [],
    }


@router.get("/templates/{tid}")
async def get_template(tid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _read_scope(db, user)
    t = await db.get(FireChecklistTemplate, tid)
    if not t or t.isDeleted:
        raise HTTPException(404, "Checklist template not found")
    history = (
        await db.execute(select(FireChecklistTemplate).where(FireChecklistTemplate.code == t.code)
                         .where(FireChecklistTemplate.isDeleted.is_(False)).order_by(FireChecklistTemplate.version.desc()))
    ).scalars().all()
    return {**_tpl(t), "versions": [_tpl(h, with_items=False) for h in history]}


@router.post("/templates", status_code=201)
async def create_template(body: TemplateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_any(db, user, (FIRE_MANAGE,))
    _check_type(body.equipmentType)
    existing = await _active_template(db, body.equipmentType)
    if existing:
        raise HTTPException(409, f"{body.equipmentType} already has a checklist ({existing.name} v{existing.version}). Edit it instead.")
    items = _clean_items(body.items)
    code = fi.slug_key(body.code or body.equipmentType).upper()
    highest = await db.scalar(select(func.max(FireChecklistTemplate.version)).where(FireChecklistTemplate.code == code)
                              .execution_options(include_deleted=True))
    t = FireChecklistTemplate(
        code=code, version=int(highest or 0) + 1, equipmentType=body.equipmentType, name=body.name.strip(),
        standardRef=body.standardRef, description=body.description, frequencyDays=body.frequencyDays,
        items=items, isActive=True, createdBy=user.id, updatedBy=user.id,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _tpl(t)


@router.put("/templates/{tid}")
async def edit_template(tid: str, body: TemplateEdit, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Edit = publish a new version. The old version is retired, never changed,
    so past inspections keep pointing at the wording they answered."""
    await _require_any(db, user, (FIRE_MANAGE,))
    old = await db.get(FireChecklistTemplate, tid)
    if not old or old.isDeleted:
        raise HTTPException(404, "Checklist template not found")
    if not old.isActive:
        raise HTTPException(409, "Only the current version can be edited. Open the latest version.")
    items = _clean_items(body.items)
    old.isActive = False
    old.updatedBy = user.id
    await db.flush()  # retire first: one active version per type (partial unique index)
    new = FireChecklistTemplate(
        code=old.code, version=old.version + 1, equipmentType=old.equipmentType, name=body.name.strip(),
        standardRef=body.standardRef, description=body.description, frequencyDays=body.frequencyDays,
        items=items, isActive=True, createdBy=user.id, updatedBy=user.id,
    )
    db.add(new)
    await db.commit()
    await db.refresh(new)
    return _tpl(new)


# ── Inspections ──────────────────────────────────────────────────────────────
def _insp(i: FireInspection, users: dict[str, str], plants: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "id": i.id, "inspectionNo": i.inspectionNo, "equipmentId": i.equipmentId, "equipmentCode": i.equipmentCode,
        "equipmentType": i.equipmentType, "location": i.location, "plantId": i.plantId,
        "plantName": (plants or {}).get(i.plantId),
        "templateId": i.templateId, "templateCode": i.templateCode, "templateVersion": i.templateVersion,
        "inspectorId": i.inspectorId, "inspectorName": users.get(i.inspectorId),
        "startedAt": _iso(i.startedAt), "completedAt": _iso(i.completedAt), "result": i.result,
        "passCount": i.passCount, "failCount": i.failCount, "naCount": i.naCount,
        "criticalFailCount": i.criticalFailCount, "remarks": i.remarks,
        "latitude": i.latitude, "longitude": i.longitude, "gpsAccuracyM": i.gpsAccuracyM, "scanned": i.scanned,
        "equipmentStatusBefore": i.equipmentStatusBefore, "equipmentStatusAfter": i.equipmentStatusAfter,
        "nextDueDate": _iso(i.nextDueDate), "capaId": i.capaId, "capaNumber": i.capaNumber,
        "followUpNote": i.followUpNote,
    }


class AnswerIn(BaseModel):
    key: str
    response: str
    remarks: str | None = Field(None, max_length=2000)
    photoPath: str | None = Field(None, max_length=500)


class InspectionIn(BaseModel):
    equipmentId: str
    templateId: str
    answers: list[AnswerIn]
    remarks: str | None = Field(None, max_length=4000)
    startedAt: datetime | None = None
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    gpsAccuracyM: float | None = Field(None, ge=0)
    scanned: bool = False
    markOutOfService: bool = False
    capaOwnerId: str | None = None


@router.get("/inspections")
async def list_inspections(
    plantId: str | None = Query(None), equipmentType: str | None = Query(None), result: str | None = Query(None),
    criticalOnly: bool = Query(False), equipmentId: str | None = Query(None), inspectorId: str | None = Query(None),
    dateFrom: datetime | None = Query(None), dateTo: datetime | None = Query(None), q: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Inspection register (newest first, 500 max)."""
    scope = await _scoped_plant(db, user, plantId)
    stmt = scope.apply(select(FireInspection), FireInspection)
    if plantId:
        stmt = stmt.where(FireInspection.plantId == plantId)
    if equipmentType:
        stmt = stmt.where(FireInspection.equipmentType == equipmentType)
    if result:
        if result not in fi.RESULTS:
            raise HTTPException(422, "result must be PASS or FAIL")
        stmt = stmt.where(FireInspection.result == result)
    if criticalOnly:
        stmt = stmt.where(FireInspection.criticalFailCount > 0)
    if equipmentId:
        stmt = stmt.where(FireInspection.equipmentId == equipmentId)
    if inspectorId:
        stmt = stmt.where(FireInspection.inspectorId == inspectorId)
    if dateFrom:
        stmt = stmt.where(FireInspection.completedAt >= dateFrom)
    if dateTo:
        stmt = stmt.where(FireInspection.completedAt < dateTo)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(FireInspection.inspectionNo.ilike(like), FireInspection.equipmentCode.ilike(like),
                              FireInspection.location.ilike(like)))
    rows = (await db.execute(stmt.order_by(FireInspection.completedAt.desc()).limit(500))).scalars().all()
    users, plants = await _names(db, [r.inspectorId for r in rows], [r.plantId for r in rows])
    return {"items": [_insp(r, users, plants) for r in rows], "total": len(rows)}


@router.get("/inspections/due")
async def due_list(
    plantId: str | None = Query(None), equipmentType: str | None = Query(None), days: int | None = Query(None, ge=0, le=365),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Assets to inspect: overdue, never inspected, defective, or coming due
    (within ``days`` if given, else each asset's own due-soon window), from the
    live due dates, most urgent first."""
    scope = await _scoped_plant(db, user, plantId)
    stmt = scope.apply(select(FireEquipment).where(FireEquipment.isDeleted.is_(False)).where(FireEquipment.isActive.is_(True)), FireEquipment)
    if plantId:
        stmt = stmt.where(FireEquipment.plantId == plantId)
    if equipmentType:
        stmt = stmt.where(FireEquipment.type == equipmentType)
    rows = (await db.execute(stmt)).scalars().all()
    now = _now()
    _, plants = await _names(db, [], [r.plantId for r in rows])
    items = []
    for e in rows:
        d = _eq(e, plants)
        window = days if days is not None else svc.due_soon_days(e.inspectionFrequencyDays)
        bucket = fi.due_bucket(d["status"], e.nextInspectionDueDate, now, soon_days=window)
        if bucket in ("OVERDUE", "NEVER", "DUE_SOON") or d["status"] == "DEFECTIVE":
            d["dueBucket"] = bucket
            items.append(d)
    order = {"OVERDUE": 0, "NEVER": 1, "DUE_SOON": 2, "OK": 3}
    items.sort(key=lambda d: (order.get(d["dueBucket"], 4), d["nextInspectionDueDate"] or ""))
    counts = {k: sum(1 for d in items if d["dueBucket"] == k) for k in ("OVERDUE", "NEVER", "DUE_SOON")}
    return {"items": items, "total": len(items), "counts": counts, "windowDays": days}


@router.get("/inspections/{iid}")
async def get_inspection(iid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    i = await db.get(FireInspection, iid)
    if not i:
        raise HTTPException(404, "Inspection not found")
    await _require_any(db, user, READ_ANY, plant_id=i.plantId)
    items = (
        await db.execute(select(FireInspectionItem).where(FireInspectionItem.inspectionId == iid)
                         .order_by(FireInspectionItem.sortOrder))
    ).scalars().all()
    users, plants = await _names(db, [i.inspectorId], [i.plantId])
    return {
        **_insp(i, users, plants),
        "items": [
            {"key": it.itemKey, "text": it.itemText, "critical": it.critical, "response": it.response,
             "remarks": it.remarks, "photoPath": it.photoPath, "sortOrder": it.sortOrder}
            for it in items
        ],
    }


@router.post("/inspections", status_code=201)
async def submit_inspection(body: InspectionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    e = await _get_equipment(db, body.equipmentId)
    await _require_any(db, user, INSPECT_ANY, plant_id=e.plantId)
    blockers = fi.inspection_blockers(status=e.status, is_active=e.isActive, is_deleted=e.isDeleted)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    t = await db.get(FireChecklistTemplate, body.templateId)
    if not t or t.isDeleted:
        raise HTTPException(422, "Checklist template not found")
    if t.equipmentType != e.type:
        raise HTTPException(422, f"This checklist is for {t.equipmentType}; the asset is {e.type}.")
    keys = [a.key for a in body.answers]
    if len(keys) != len(set(keys)):
        raise HTTPException(422, "An item was answered more than once")
    answers = {a.key: {"response": (a.response or "").upper(), "remarks": a.remarks, "photoPath": a.photoPath} for a in body.answers}
    items = list(t.items or [])
    errors = fi.validate_responses(items, answers)
    for a in body.answers:
        if not fi.photo_path_ok(a.photoPath, e.id):
            errors.append(f"Photo for '{a.key}' was not uploaded for this asset.")
    if errors:
        raise HTTPException(422, "\n".join(errors))
    now = _now()
    if body.startedAt and svc._aware(body.startedAt) > now + timedelta(minutes=5):
        raise HTTPException(422, "startedAt is in the future")
    # Double-submit guard (a second tap on a slow mobile connection).
    recent = await db.scalar(
        select(func.count()).select_from(FireInspection).where(FireInspection.equipmentId == e.id)
        .where(FireInspection.inspectorId == user.id).where(FireInspection.completedAt >= now - timedelta(minutes=2))
    )
    if recent:
        raise HTTPException(409, "You recorded an inspection of this asset in the last 2 minutes. Refresh to see it.")

    summary = fi.summarise(items, answers)
    no = await next_number(db, column=FireInspection.inspectionNo, stem=f"FINSP-{now.year}-", width=5)
    before = svc.compute_status(e)
    sticky = fi.status_after_inspection(before, critical_fail_count=summary["criticalFailCount"],
                                        mark_out_of_service=body.markOutOfService)
    e.lastInspectionDate = now
    e.nextInspectionDueDate = fi.next_due(now, e.inspectionFrequencyDays)
    if sticky:
        e.status = sticky
    else:
        e.status = "ACTIVE"
        e.status = svc.compute_status(e)
    if sticky == "OUT_OF_SERVICE" and before != "OUT_OF_SERVICE":
        e.outOfServiceReason = f"Failed critical check(s) in {no}: " + "; ".join(f["text"] for f in summary["criticalFails"])[:900]
    e.updatedBy = user.id

    insp = FireInspection(
        inspectionNo=no, equipmentId=e.id, equipmentCode=e.equipmentCode, equipmentType=e.type, location=e.location,
        plantId=e.plantId, templateId=t.id, templateCode=t.code, templateVersion=t.version, inspectorId=user.id,
        startedAt=body.startedAt, completedAt=now, result=summary["result"], passCount=summary["passCount"],
        failCount=summary["failCount"], naCount=summary["naCount"], criticalFailCount=summary["criticalFailCount"],
        remarks=(body.remarks or "").strip() or None, latitude=body.latitude, longitude=body.longitude,
        gpsAccuracyM=body.gpsAccuracyM, scanned=body.scanned, equipmentStatusBefore=before,
        equipmentStatusAfter=e.status, nextDueDate=e.nextInspectionDueDate,
    )
    db.add(insp)
    await db.flush()
    for n, it in enumerate(items, start=1):
        a = answers[it["key"]]
        db.add(FireInspectionItem(
            inspectionId=insp.id, sortOrder=n, itemKey=it["key"], itemText=it["text"], critical=bool(it.get("critical")),
            response=a["response"], remarks=(a["remarks"] or "").strip() or None, photoPath=a["photoPath"] or None,
        ))
    ev = fi.status_event(before, e.status)
    if ev:
        note = None
        if summary["criticalFails"]:
            note = "Failed: " + "; ".join(f["text"] for f in summary["criticalFails"])
        _log(db, e, ev, before=before, after=e.status, note=note, actor_id=user.id, inspection_id=insp.id)

    if summary["criticalFailCount"]:
        await _raise_capa(db, insp, e, summary, owner_id=body.capaOwnerId or user.id, actor_id=user.id)

    await db.commit()
    await db.refresh(insp)
    users, plants = await _names(db, [insp.inspectorId], [insp.plantId])
    return {**_insp(insp, users, plants), "equipment": _eq(e, plants)}


async def _raise_capa(db: AsyncSession, insp: FireInspection, e: FireEquipment, summary: dict[str, Any],
                      *, owner_id: str, actor_id: str) -> None:
    """A failed critical item opens a CAPA on the universal engine (source
    INSPECTION_FINDING, which every tenant seeds). If the CAPA masters are not
    seeded the inspection still saves, and the note says why no CAPA exists."""
    from app.services import capa_spawn

    try:
        capa = await capa_spawn.spawn_capa(
            db, source_code="INSPECTION_FINDING", plant_id=e.plantId,
            title=f"Fire equipment {e.equipmentCode} failed critical inspection check",
            problem=fi.capa_problem_text(
                equipment_code=e.equipmentCode, equipment_type=e.type, location=e.location,
                inspection_no=insp.inspectionNo, critical_fails=summary["criticalFails"],
            ),
            ref_id=insp.id, ref_url=f"/fire-safety/inspections/{insp.id}",
            ref_summary=f"{insp.inspectionNo} — {e.equipmentCode} ({e.type.replace('_', ' ').lower()})",
            metadata={"module": "FIRE", "inspectionNo": insp.inspectionNo, "equipmentId": e.id,
                      "equipmentCode": e.equipmentCode, "equipmentType": e.type,
                      "failedItems": [f["key"] for f in summary["criticalFails"]]},
            severity="HIGH", priority="HIGH", detected_method="FIRE_INSPECTION",
            owner_id=owner_id, actor_id=actor_id, due_days=7,
        )
    except ValueError as ex:
        insp.followUpNote = f"CAPA not raised: {ex}"
        return
    await db.flush()
    insp.capaId = capa.id
    insp.capaNumber = capa.capaNumber
    insp.followUpNote = f"CAPA {capa.capaNumber} raised for the failed critical item(s)."


class PhotoInitIn(BaseModel):
    equipmentId: str
    fileName: str = Field(min_length=1, max_length=200)
    contentType: str = Field(min_length=3, max_length=100)
    fileSize: int | None = Field(None, ge=1)


class PhotoViewIn(BaseModel):
    storagePath: str = Field(min_length=5, max_length=500)


@router.post("/inspections/upload-url")
async def photo_upload_url(body: PhotoInitIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Signed upload URL for one checklist-item photo. The browser PUTs the bytes
    straight to storage and sends the returned storagePath with the submit."""
    e = await _get_equipment(db, body.equipmentId)
    await _require_any(db, user, INSPECT_ANY, plant_id=e.plantId)
    if not body.contentType.lower().startswith("image/"):
        raise HTTPException(422, "Only image files can be attached to a checklist item")
    if body.fileSize and body.fileSize > PHOTO_MAX_BYTES:
        raise HTTPException(422, "Photo is larger than 10 MB")
    if not is_storage_configured():
        raise HTTPException(503, "File storage is not configured on this server")
    path = build_evidence_storage_path(entity_type=fi.PHOTO_ENTITY, entity_id=e.id, category="ITEM_PHOTO", file_name=body.fileName)
    try:
        signed = create_signed_upload_url(path)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(503, f"Storage init failed: {ex}") from ex
    return {"storagePath": path, "uploadUrl": signed["uploadUrl"], "token": signed.get("token", "")}


@router.post("/inspections/view-url")
async def photo_view_url(body: PhotoViewIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    path = body.storagePath
    if not path.startswith(fi.PHOTO_PREFIX) or ".." in path:
        raise HTTPException(422, "Not a fire inspection photo")
    parts = path.split("/")
    if len(parts) < 4:
        raise HTTPException(422, "Not a fire inspection photo")
    e = await _get_equipment(db, parts[2])
    await _require_any(db, user, READ_ANY, plant_id=e.plantId)
    if not is_storage_configured():
        raise HTTPException(503, "File storage is not configured on this server")
    try:
        url = create_signed_download_url(path, expires_in_sec=300)
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(503, f"Could not sign the photo URL: {ex}") from ex
    return {"url": url}


# ── Assembly points ──────────────────────────────────────────────────────────
@router.get("/assembly-points")
async def list_aps(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    scope = await _read_scope(db, user)
    rows = (await db.execute(scope.apply(select(AssemblyPoint).where(AssemblyPoint.isDeleted.is_(False)), AssemblyPoint))).scalars().all()
    users, plants = await _names(db, [a.wardenUserId for a in rows] + [a.alternateWardenUserId for a in rows], [a.plantId for a in rows])
    return {"items": [{"id": a.id, "code": a.code, "name": a.name, "plantId": a.plantId, "plantName": plants.get(a.plantId),
                       "capacity": a.capacity, "isActive": a.isActive,
                       "wardenUserId": a.wardenUserId, "wardenName": users.get(a.wardenUserId or ""),
                       "alternateWardenUserId": a.alternateWardenUserId,
                       "alternateWardenName": users.get(a.alternateWardenUserId or ""),
                       "buildingIds": a.buildingIds, "latitude": a.latitude, "longitude": a.longitude} for a in rows]}


# ── Plans ────────────────────────────────────────────────────────────────────
@router.get("/plans")
async def list_plans(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    scope = await _read_scope(db, user)
    rows = (await db.execute(scope.apply(select(FireEmergencyPlan).where(FireEmergencyPlan.isDeleted.is_(False)), FireEmergencyPlan))).scalars().all()
    _, plants = await _names(db, [], [p.plantId for p in rows])
    return {"items": [{"id": p.id, "planCode": p.planCode, "title": p.title, "plantId": p.plantId,
                       "plantName": plants.get(p.plantId), "status": p.status,
                       "lastReviewDate": p.lastReviewDate.isoformat() if p.lastReviewDate else None,
                       "fireTypes": p.fireTypes, "assemblyPointIds": p.assemblyPointIds, "externalContacts": p.externalContacts,
                       "commandStructure": p.commandStructure, "nextReviewDate": p.nextReviewDate.isoformat() if p.nextReviewDate else None} for p in rows]}


# ── Drills (FS-07) ───────────────────────────────────────────────────────────
@router.get("/drills")
async def list_drills(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    scope = await _read_scope(db, user)
    # Newest-created first — platform-wide register convention.
    rows = (
        await db.execute(
            scope.apply(select(FireDrill).where(FireDrill.isDeleted.is_(False)), FireDrill).order_by(
                FireDrill.createdAt.desc(), FireDrill.id.desc()
            ),
        )
    ).scalars().all()
    out = []
    for d in rows:
        out.append({"id": d.id, "drillCode": d.drillCode, "plantId": d.plantId, "drillType": d.drillType,
                    "status": d.status, "outcome": d.outcome,
                    "scheduledDate": d.scheduledDate.isoformat() if d.scheduledDate else None,
                    "conductedDate": d.conductedDate.isoformat() if d.conductedDate else None,
                    "evacuationTimeMinutes": d.evacuationTimeMinutes, "evacuationTargetMinutes": d.evacuationTargetMinutes,
                    "unaccountedPersons": d.unaccountedPersons, "isAnnualMandatory": d.isAnnualMandatory})
    return {"items": out, "total": len(out)}


class DrillScheduleBody(BaseModel):
    plantId: str
    drillType: str = "FIRE_EVACUATION"
    scheduledDate: datetime
    planId: str | None = None
    facilitatorId: str | None = None
    evacuationTargetMinutes: float | None = Field(None, gt=0)
    isAnnualMandatory: bool = False


class DrillCancelBody(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/drills", status_code=201)
async def schedule_drill(body: DrillScheduleBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Schedule a mock drill (IMS/MIS: 'mock drill schedule & closure'). Closure
    is the existing /drills/{id}/complete, with its MAJOR_GAP gate."""
    from app.services.ims import DRILL_TYPES

    await _require(db, user, _WRITE, plant_id=body.plantId)
    if body.drillType not in DRILL_TYPES:
        raise HTTPException(422, f"drillType must be one of {', '.join(DRILL_TYPES)}")
    code = await next_number(db, column=FireDrill.drillCode, stem=f"DRILL-{body.scheduledDate.year}-")
    d = FireDrill(
        drillCode=code, plantId=body.plantId, drillType=body.drillType, planId=body.planId,
        scheduledDate=body.scheduledDate, status="PLANNED", facilitatorId=body.facilitatorId,
        evacuationTargetMinutes=body.evacuationTargetMinutes, isAnnualMandatory=body.isAnnualMandatory,
        createdBy=user.id,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return {"id": d.id, "drillCode": d.drillCode, "status": d.status, "scheduledDate": d.scheduledDate.isoformat()}


@router.post("/drills/{did}/cancel")
async def cancel_drill(did: str, body: DrillCancelBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await db.get(FireDrill, did)
    if not d or d.isDeleted:
        raise HTTPException(404, "Drill not found")
    await _require(db, user, _WRITE, plant_id=d.plantId)
    if d.status != "PLANNED":
        raise HTTPException(409, f"Only a PLANNED drill can be cancelled (this one is {d.status})")
    d.status = "CANCELLED"
    d.reportRichText = f"Cancelled: {body.reason}"
    await db.commit()
    return {"ok": True, "drillId": d.id, "status": d.status}


class DrillCompleteBody(BaseModel):
    conductedDate: datetime | None = None
    outcome: str = "SATISFACTORY"
    participantCount: int | None = None
    evacuationTimeMinutes: float | None = None
    evacuationTargetMinutes: float | None = None
    assemblyPointVerified: bool = True
    unaccountedPersons: int = 0
    reportRichText: str | None = None


@router.post("/drills/{did}/complete")
async def complete_drill(did: str, body: DrillCompleteBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await db.get(FireDrill, did)
    if not d or d.isDeleted:
        raise HTTPException(404, "Drill not found")
    await _require(db, user, _WRITE, plant_id=d.plantId)
    # apply the conduct data first so the gate sees the final values
    d.unaccountedPersons = body.unaccountedPersons
    d.assemblyPointVerified = body.assemblyPointVerified
    d.participantCount = body.participantCount
    d.evacuationTimeMinutes = body.evacuationTimeMinutes
    d.evacuationTargetMinutes = body.evacuationTargetMinutes
    d.reportRichText = body.reportRichText
    blockers = await svc.drill_completion_blockers(db, d)
    if blockers:
        raise HTTPException(400, "Cannot complete drill: " + " ".join(blockers))
    d.status = "COMPLETED"
    d.outcome = body.outcome
    d.conductedDate = body.conductedDate or _now()
    d.updatedBy = user.id
    await db.commit()
    return {"ok": True, "drillId": d.id, "status": d.status, "outcome": d.outcome}


# ── Crisis escalation + FSER ─────────────────────────────────────────────────
class EscalateBody(BaseModel):
    plantId: str | None = None
    affectedEquipmentIds: list[str] = []
    evacuationOrdered: bool = True
    fireServiceCalled: bool = True


@router.post("/incidents/{incident_id}/escalate-crisis", status_code=201)
async def escalate_crisis(incident_id: str, body: EscalateBody, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, _WRITE, plant_id=body.plantId)
    res = await svc.escalate_incident_to_crisis(
        db, incident_id, body.plantId, user.id, body.affectedEquipmentIds, body.evacuationOrdered, body.fireServiceCalled,
    )
    await db.commit()
    return res


@router.get("/fser/{plant_id}")
async def fser(plant_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_any(db, user, READ_ANY, plant_id=plant_id)
    return await svc.fser_panel(db, plant_id)
