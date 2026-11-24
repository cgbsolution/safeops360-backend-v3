"""PTW Control Center v2 API (docs/ptw-control-center/PLAN.md).

  GET    /api/ptw/control-center?plant&area&shift&range   the ONE payload (PTW.READ)
  GET    /api/ptw/kiosk/control-center                    same payload for a wall display (kiosk token)
  GET    /api/ptw/kiosk/zones/{areaId}/guidance           zone guidance for the wall display (kiosk token)
  POST   /api/ptw/control-center/evaluate?plant           run the exception engine now (PTW_ZONE.MANAGE)
  POST   /api/ptw/control-center/brief?plant              regenerate the shift brief (PTW_ZONE.MANAGE)
  POST   /api/ptw/exceptions/{id}/ack                     acknowledge an exception (PTW.READ + plant)
  GET    /api/ptw/zones/{areaId}/guidance                 guidance + active impairments (PTW.READ)
  PUT    /api/ptw/zones/{areaId}/guidance                 area owner or PTW_ZONE.MANAGE
  PUT    /api/ptw/zones/{areaId}/geometry                 polygon + sort order (PTW_ZONE.MANAGE)
  GET    /api/ptw/layout?plant                            layout + zones for the editor (PTW.READ)
  PUT    /api/ptw/layout?plant                            scale in metres (PTW_ZONE.MANAGE)
  POST   /api/ptw/layout/image?plant                      upload the layout image (PTW_ZONE.MANAGE)
  GET    /api/ptw/{permitId}/controls                     impairment controls required/satisfied
  POST   /api/ptw/{permitId}/controls                     tick a control (permit party / PTW.UPDATE;
                                                          fire_officer_cosign needs PTW_IMPAIRMENT.APPROVE)
  DELETE /api/ptw/{permitId}/controls/{key}               untick (same rule)
  GET/POST/DELETE /api/ptw/kiosk-tokens                   wall-display tokens (PTW_KIOSK.MANAGE)

⚠ Mounted BEFORE "ptw" in app/main.py — `GET /api/ptw/{permit_id}` would
otherwise swallow /api/ptw/layout and /api/ptw/control-center.

Kiosk tokens are opaque random strings stored as SHA-256 hashes. They are not
JWTs: get_current_user rejects them, so a kiosk token cannot call any endpoint
except the one read-only route that looks them up explicitly.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.permit import Permit
from app.models.plant import Area, Plant
from app.models.ptw_ops import (
    TENANT_DEFAULT,
    Impairment,
    PermitControl,
    PlantLayout,
    PtwException,
    PtwKioskToken,
    ZoneGuidance,
)
from app.models.user import User
from app.services.access_scope import build_query_scope
from app.services.permissions import PermissionContext, can
from app.services.ptw_ops import brief as brief_svc
from app.services.ptw_ops import engine, loaders
from app.services.ptw_ops import impairment_check as ic
from app.services.ptw_ops.derived import RUNNING_STATUSES
from app.services.ptw_ops.payload import build

router = APIRouter(prefix="/api/ptw", tags=["ptw-control-center-v2"])

READ = "PTW.READ"
ZONE_MANAGE = "PTW_ZONE.MANAGE"
IMP_APPROVE = "PTW_IMPAIRMENT.APPROVE"
KIOSK_MANAGE = "PTW_KIOSK.MANAGE"
RANGES = ("shift", "today", "7d")
PERMIT_TYPES = ("HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "EXCAVATION", "ELECTRICAL_LOTO", "LIFTING",
                "GENERAL_COLD")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _allowed(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> bool:
    return (await can(db, user.id, perm, PermissionContext(plant_id=plant_id))).allowed


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _require_tables(db: AsyncSession, *tables: str) -> None:
    s = await loaders.schema(db)
    missing = [t for t in tables if not s.get(t)]
    if missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "PTW Control Center tables are not installed yet — run "
            "prisma/apply-ptw-control-center-ddl.ts (missing: " + ", ".join(missing) + ")",
        )


async def resolve_plant(db: AsyncSession, user: User, plant: str | None) -> str:
    """The plant to show: the one asked for (if in scope), else the user's own
    plant, else the first plant in scope. Never a plant outside the scope."""
    await _require(db, user, READ)
    scope = await build_query_scope(db, user.id, READ)
    if plant:
        if not scope.allows_plant(plant):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant is outside your access scope")
        return plant
    if user.plantId and scope.allows_plant(user.plantId):
        return user.plantId
    if scope.plant_ids:
        return sorted(scope.plant_ids)[0]
    if scope.all_plants:
        first = (await db.execute(select(Plant.id).order_by(Plant.name).limit(1))).scalar_one_or_none()
        if first:
            return first
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No plant available")


async def _payload(db: AsyncSession, plant_id: str, area: str | None, shift: str | None, rng: str) -> dict[str, Any]:
    if rng not in RANGES:
        rng = "today"
    if area:
        ok = await db.scalar(select(Area.id).where(Area.id == area).where(Area.plantId == plant_id))
        if not ok:
            area = None  # an area from another plant is ignored, never leaked
    snap, extras = await loaders.load(db, plant_id=plant_id, area=area, shift=shift, rng=rng)
    return build(snap, extras)


@router.get("/control-center")
async def control_center(
    plant: str | None = Query(None),
    plantId: str | None = Query(None),
    area: str | None = Query(None),
    shift: str | None = Query(None),
    range: str = Query("today"),  # noqa: A002 — query name fixed by the brief
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    pid = await resolve_plant(db, user, plant or plantId)
    out = await _payload(db, pid, area, shift, range)
    out["permissions"] = {
        "manageZones": await _allowed(db, user, ZONE_MANAGE, pid),
        "raiseImpairment": await _allowed(db, user, "PTW_IMPAIRMENT.CREATE", pid),
        "approveImpairment": await _allowed(db, user, IMP_APPROVE, pid),
        "manageKiosk": await _allowed(db, user, KIOSK_MANAGE, pid),
    }
    return out


# ── kiosk ────────────────────────────────────────────────────────────────────
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _kiosk_row(db: AsyncSession, raw: str | None) -> PtwKioskToken:
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Kiosk token required")
    await _require_tables(db, "PtwKioskToken")
    row = (await db.execute(
        select(PtwKioskToken).where(PtwKioskToken.tokenHash == hash_token(raw))
        .where(PtwKioskToken.revokedAt.is_(None)).where(PtwKioskToken.tenantId == TENANT_DEFAULT)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Kiosk token invalid or revoked")
    return row


@router.get("/kiosk/control-center")
async def kiosk_control_center(
    token: str | None = Query(None),
    x_kiosk_token: str | None = Header(None),
    area: str | None = Query(None),
    shift: str | None = Query(None),
    range: str = Query("today"),  # noqa: A002
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await _kiosk_row(db, x_kiosk_token or token)
    row.lastSeenAt = _now()
    await db.commit()
    out = await _payload(db, row.plantId, area, shift, range)
    out["kiosk"] = {"label": row.label}
    out["permissions"] = {"manageZones": False, "raiseImpairment": False, "approveImpairment": False,
                          "manageKiosk": False}
    return out


@router.get("/kiosk/zones/{area_id}/guidance")
async def kiosk_guidance(area_id: str, token: str | None = Query(None), x_kiosk_token: str | None = Header(None),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await _kiosk_row(db, x_kiosk_token or token)
    area = await _area_or_404(db, area_id)
    if area.plantId != row.plantId:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found")
    out = await _guidance_payload(db, area)
    out["canEdit"] = False
    return out


class KioskTokenIn(BaseModel):
    plantId: str
    label: str = Field(min_length=1, max_length=80)


@router.get("/kiosk-tokens")
async def list_kiosk_tokens(plant: str | None = Query(None), user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    pid = await resolve_plant(db, user, plant)
    await _require(db, user, KIOSK_MANAGE, pid)
    await _require_tables(db, "PtwKioskToken")
    rows = (await db.execute(select(PtwKioskToken).where(PtwKioskToken.plantId == pid)
                             .where(PtwKioskToken.tenantId == TENANT_DEFAULT)
                             .order_by(PtwKioskToken.createdAt.desc()))).scalars().all()
    return [{"id": r.id, "label": r.label, "createdAt": r.createdAt.isoformat(),
             "revokedAt": r.revokedAt.isoformat() if r.revokedAt else None,
             "lastSeenAt": r.lastSeenAt.isoformat() if r.lastSeenAt else None} for r in rows]


@router.post("/kiosk-tokens", status_code=201)
async def create_kiosk_token(body: KioskTokenIn, user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pid = await resolve_plant(db, user, body.plantId)
    await _require(db, user, KIOSK_MANAGE, pid)
    await _require_tables(db, "PtwKioskToken")
    raw = secrets.token_urlsafe(32)
    row = PtwKioskToken(plantId=pid, label=body.label.strip(), tokenHash=hash_token(raw), createdById=user.id)
    db.add(row)
    await db.commit()
    # The raw token is returned exactly once; only its hash is stored.
    return {"id": row.id, "label": row.label, "token": raw}


@router.delete("/kiosk-tokens/{token_id}", status_code=204)
async def revoke_kiosk_token(token_id: str, user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)) -> None:
    await _require_tables(db, "PtwKioskToken")
    row = await db.get(PtwKioskToken, token_id)
    if row is None:
        raise HTTPException(404, "Token not found")
    await _require(db, user, KIOSK_MANAGE, row.plantId)
    row.revokedAt = row.revokedAt or _now()
    await db.commit()


# ── engine / brief on demand ─────────────────────────────────────────────────
@router.post("/control-center/evaluate")
async def evaluate_now(plant: str | None = Query(None), user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pid = await resolve_plant(db, user, plant)
    await _require(db, user, ZONE_MANAGE, pid)
    out = await engine.evaluate_plant(db, pid)
    await db.commit()
    return out


@router.post("/control-center/brief")
async def regenerate_brief(plant: str | None = Query(None), user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pid = await resolve_plant(db, user, plant)
    await _require(db, user, ZONE_MANAGE, pid)
    out = await brief_svc.generate(db, pid, force=True)
    await db.commit()
    return {k: v for k, v in out.items() if k in ("written", "cached", "skipped")}


@router.post("/exceptions/{exception_id}/ack")
async def acknowledge(exception_id: str, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_tables(db, "PtwException")
    row = await db.get(PtwException, exception_id)
    if row is None or row.tenantId != TENANT_DEFAULT:
        raise HTTPException(404, "Exception not found (it may not be persisted yet — the engine runs every 5 min)")
    await resolve_plant(db, user, row.plantId)
    row.acknowledgedAt = row.acknowledgedAt or _now()
    row.acknowledgedById = row.acknowledgedById or user.id
    await db.commit()
    return {"id": row.id, "acknowledgedAt": row.acknowledgedAt.isoformat()}


# ── zones: guidance, geometry, layout ────────────────────────────────────────
class TypeRule(BaseModel):
    permitType: str
    state: str = Field(pattern="^(allowed|conditional|blocked)$")
    conditionText: str | None = Field(None, max_length=300)


class GuidanceIn(BaseModel):
    restrictedHours: str | None = Field(None, max_length=500)
    instructions: list[str] = Field(default_factory=list, max_length=20)
    mandatoryPpe: list[str] = Field(default_factory=list, max_length=20)
    assemblyPoint: str | None = Field(None, max_length=200)
    extinguisherLocations: str | None = Field(None, max_length=300)
    permitTypeRules: list[TypeRule] = Field(default_factory=list, max_length=10)


async def _area_or_404(db: AsyncSession, area_id: str) -> Area:
    area = await db.get(Area, area_id)
    if area is None:
        raise HTTPException(404, "Area not found")
    return area


def _guidance_out(area: Area, g: ZoneGuidance | None, owner: str | None, imps: list[Impairment],
                  rules: list, now: datetime) -> dict[str, Any]:
    from app.services.ptw_ops.loaders import _utc
    from app.services.ptw_ops.types import ImpairmentView

    active = []
    for i in imps:
        v = ImpairmentView(id=i.id, code=i.code, plantId=i.plantId, areaId=i.areaId, systemType=i.systemType,
                           systemName=i.systemName, reason=i.reason, status=i.status, startedAt=_utc(i.startedAt),
                           plannedRestoreAt=_utc(i.plannedRestoreAt), restoredAt=_utc(i.restoredAt))
        if not v.is_in_force(now):
            continue
        effect, sev = ic.effect_summary(v, rules, area.plantId)
        active.append({"id": i.id, "code": i.code, "systemName": i.systemName, "systemType": i.systemType,
                       "reason": i.reason, "plannedRestoreAt": i.plannedRestoreAt.isoformat() if i.plannedRestoreAt else None,
                       "precautions": list(i.precautions or []), "effect": effect, "effectSeverity": sev})
    # Permit-type states: the zone's own rules, with live impairments layered on
    # top (a type blocked by an impairment shows blocked whatever the standing rule says).
    type_rules = [dict(r) for r in ((g.permitTypeRules if g else None) or [])]
    for a in active:
        v_rules = [r for r in rules if r.systemType == a["systemType"] and r.plantId in (None, area.plantId)]
        for r in v_rules:
            if r.effect == "allow":
                continue
            targets = PERMIT_TYPES if r.permitType == "*" else (r.permitType,)
            for t in targets:
                existing = next((x for x in type_rules if x.get("permitType") == t), None)
                state = "blocked" if r.effect == "block_new" else "conditional"
                cond = (f"New work blocked — {a['code']} impaired" if state == "blocked" else
                        "Needs " + ", ".join(ic.control_label(c).lower() for c in r.requiredControls))
                if existing is None:
                    if r.permitType != "*":
                        type_rules.append({"permitType": t, "state": state, "conditionText": cond, "live": True})
                elif existing.get("state") != "blocked":
                    existing.update({"state": state, "conditionText": cond, "live": True})
    return {
        "areaId": area.id, "areaName": area.name, "areaOwner": owner,
        "restrictedHours": g.restrictedHours if g else None,
        "instructions": list(g.instructions or []) if g else [],
        "mandatoryPpe": list(g.mandatoryPpe or []) if g else [],
        "assemblyPoint": g.assemblyPoint if g else None,
        "extinguisherLocations": g.extinguisherLocations if g else None,
        "permitTypeRules": type_rules,
        "updatedAt": g.updatedAt.isoformat() if g and g.updatedAt else None,
        "impairments": active,
    }


async def _load_rules(db: AsyncSession, plant_id: str) -> list:
    from app.services.ptw_ops import defaults
    from app.services.ptw_ops.types import ImpairmentRuleView

    s = await loaders.schema(db)
    if not s["ImpairmentRule"]:
        return defaults.impairment_rule_views()
    rows = (await db.execute(text(
        """SELECT id, "plantId", "systemType", "permitType", effect, "requiredControls", message
           FROM "ImpairmentRule" WHERE "isActive" AND "tenantId" = :t AND ("plantId" IS NULL OR "plantId" = :p)"""
    ), {"t": TENANT_DEFAULT, "p": plant_id})).mappings().all()
    return [ImpairmentRuleView(id=r["id"], systemType=r["systemType"], permitType=r["permitType"],
                               effect=r["effect"], requiredControls=list(r["requiredControls"] or []),
                               message=r["message"], plantId=r["plantId"]) for r in rows] \
        or defaults.impairment_rule_views()


@router.get("/zones/{area_id}/guidance")
async def get_guidance(area_id: str, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    area = await _area_or_404(db, area_id)
    await resolve_plant(db, user, area.plantId)
    out = await _guidance_payload(db, area)
    out["canEdit"] = (area.ownerUserId == user.id) or await _allowed(db, user, ZONE_MANAGE, area.plantId)
    return out


async def _guidance_payload(db: AsyncSession, area: Area) -> dict[str, Any]:
    area_id = area.id
    s = await loaders.schema(db)
    g = None
    imps: list[Impairment] = []
    if s["ZoneGuidance"]:
        g = (await db.execute(select(ZoneGuidance).where(ZoneGuidance.areaId == area_id)
                              .where(ZoneGuidance.tenantId == TENANT_DEFAULT))).scalar_one_or_none()
    if s["Impairment"]:
        imps = list((await db.execute(select(Impairment).where(Impairment.areaId == area_id)
                                      .where(Impairment.tenantId == TENANT_DEFAULT)
                                      .where(Impairment.status.in_(("approved", "active"))))).scalars().all())
    owner = await db.scalar(select(User.name).where(User.id == area.ownerUserId)) if area.ownerUserId else None
    return _guidance_out(area, g, owner, imps, await _load_rules(db, area.plantId), _now())


@router.put("/zones/{area_id}/guidance")
async def put_guidance(area_id: str, body: GuidanceIn, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_tables(db, "ZoneGuidance")
    area = await _area_or_404(db, area_id)
    await resolve_plant(db, user, area.plantId)
    if area.ownerUserId != user.id and not await _allowed(db, user, ZONE_MANAGE, area.plantId):
        raise HTTPException(403, "Only the area owner or an EHS admin can edit zone guidance")
    for r in body.permitTypeRules:
        if r.permitType not in PERMIT_TYPES:
            raise HTTPException(400, f"Unknown permit type {r.permitType}")
    g = (await db.execute(select(ZoneGuidance).where(ZoneGuidance.areaId == area_id)
                          .where(ZoneGuidance.tenantId == TENANT_DEFAULT))).scalar_one_or_none()
    if g is None:
        g = ZoneGuidance(areaId=area_id, plantId=area.plantId)
        db.add(g)
    g.restrictedHours = (body.restrictedHours or "").strip() or None
    g.instructions = [x.strip() for x in body.instructions if x.strip()]
    g.mandatoryPpe = [x.strip() for x in body.mandatoryPpe if x.strip()]
    g.assemblyPoint = (body.assemblyPoint or "").strip() or None
    g.extinguisherLocations = (body.extinguisherLocations or "").strip() or None
    g.permitTypeRules = [r.model_dump() for r in body.permitTypeRules]
    g.updatedById = user.id
    g.updatedAt = _now()
    await db.commit()
    return await get_guidance(area_id, user, db)


class GeometryIn(BaseModel):
    polygon: list[dict[str, float]] = Field(default_factory=list, max_length=64)
    sortOrder: int | None = None


@router.put("/zones/{area_id}/geometry")
async def put_geometry(area_id: str, body: GeometryIn, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await loaders.schema(db)
    if not s["Area.geometry"]:
        await _require_tables(db, "Area.geometry")
    area = await _area_or_404(db, area_id)
    await _require(db, user, ZONE_MANAGE, area.plantId)
    pts = []
    for p in body.polygon:
        x, y = float(p.get("x", -1)), float(p.get("y", -1))
        if not (0 <= x <= 100 and 0 <= y <= 100):
            raise HTTPException(400, "Polygon points must be within 0–100 (% of the layout image)")
        pts.append({"x": round(x, 2), "y": round(y, 2)})
    if pts and len(pts) < 3:
        raise HTTPException(400, "A zone polygon needs at least 3 points")
    import json

    await db.execute(text('UPDATE "Area" SET geometry = CAST(:g AS jsonb), "sortOrder" = :o WHERE id = :id'),
                     {"g": json.dumps(pts) if pts else None, "o": body.sortOrder, "id": area_id})
    await db.commit()
    return {"areaId": area_id, "polygon": pts, "sortOrder": body.sortOrder}


@router.get("/layout")
async def get_layout(plant: str | None = Query(None), user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pid = await resolve_plant(db, user, plant)
    snap, _ = await loaders.load(db, plant_id=pid)
    s = await loaders.schema(db)
    return {
        "plantId": pid, "imageUrl": snap.layout.imageUrl, "widthM": snap.layout.widthM,
        "heightM": snap.layout.heightM, "ready": bool(s.get("ready")),
        "zones": [{"id": z.id, "name": z.name, "polygon": z.polygon, "sortOrder": z.sortOrder} for z in snap.zones],
        "canEdit": await _allowed(db, user, ZONE_MANAGE, pid),
    }


class LayoutIn(BaseModel):
    widthM: float | None = Field(None, gt=0, le=100000)
    heightM: float | None = Field(None, gt=0, le=100000)


async def _layout_row(db: AsyncSession, pid: str) -> PlantLayout:
    row = (await db.execute(select(PlantLayout).where(PlantLayout.plantId == pid)
                            .where(PlantLayout.tenantId == TENANT_DEFAULT))).scalar_one_or_none()
    if row is None:
        row = PlantLayout(plantId=pid)
        db.add(row)
    return row


@router.put("/layout")
async def put_layout(body: LayoutIn, plant: str | None = Query(None), user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pid = await resolve_plant(db, user, plant)
    await _require(db, user, ZONE_MANAGE, pid)
    await _require_tables(db, "PlantLayout")
    row = await _layout_row(db, pid)
    row.widthM, row.heightM, row.updatedById, row.updatedAt = body.widthM, body.heightM, user.id, _now()
    await db.commit()
    return {"plantId": pid, "widthM": row.widthM, "heightM": row.heightM}


@router.post("/layout/image")
async def upload_layout_image(plant: str | None = Query(None), file: UploadFile = File(...),
                              user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from app.services.storage import is_storage_configured, upload_object

    pid = await resolve_plant(db, user, plant)
    await _require(db, user, ZONE_MANAGE, pid)
    await _require_tables(db, "PlantLayout")
    if (file.content_type or "") not in ("image/png", "image/jpeg", "image/webp", "image/svg+xml"):
        raise HTTPException(400, "Layout must be a PNG, JPEG, WebP or SVG image")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "Layout image is larger than 8 MB")
    if not is_storage_configured():
        raise HTTPException(503, "File storage is not configured on this instance")
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/svg+xml": "svg"}[file.content_type]
    path = f"ptw-layouts/{pid}/{secrets.token_hex(6)}.{ext}"
    upload_object(path, data, file.content_type)
    row = await _layout_row(db, pid)
    row.storagePath, row.updatedById, row.updatedAt = path, user.id, _now()
    await db.commit()
    return {"plantId": pid, "storagePath": path}


# ── permit controls (require_controls gate) ─────────────────────────────────
class ControlIn(BaseModel):
    controlKey: str = Field(min_length=2, max_length=60)
    note: str | None = Field(None, max_length=500)
    attachmentId: str | None = None


async def permit_decision(db: AsyncSession, permit: Permit, *, running: bool | None = None,
                          now: datetime | None = None) -> ic.Decision:
    """The impairment decision for one permit, from the live tables (used by the
    controls panel, permit create and the workflow approval gate)."""
    from app.services.ptw_ops.loaders import _utc
    from app.services.ptw_ops.types import ImpairmentView

    now = now or _now()
    s = await loaders.schema(db)
    if not s["Impairment"] or not permit.areaId:
        return ic.Decision()
    imps = (await db.execute(select(Impairment).where(Impairment.areaId == permit.areaId)
                             .where(Impairment.tenantId == TENANT_DEFAULT)
                             .where(Impairment.status.in_(("approved", "active"))))).scalars().all()
    views = [ImpairmentView(id=i.id, code=i.code, plantId=i.plantId, areaId=i.areaId, systemType=i.systemType,
                            systemName=i.systemName, reason=i.reason, status=i.status, startedAt=_utc(i.startedAt),
                            plannedRestoreAt=_utc(i.plannedRestoreAt), restoredAt=_utc(i.restoredAt))
             for i in imps]
    satisfied: set[str] = set()
    if s["PermitControl"] and permit.id:
        satisfied = set((await db.execute(select(PermitControl.controlKey)
                                          .where(PermitControl.permitId == permit.id))).scalars().all())
    if getattr(permit, "fireWatchPersonId", None):
        satisfied.add("fire_watch")
    status_v = getattr(permit.status, "value", permit.status)
    if running is None:
        running = str(status_v) in RUNNING_STATUSES
    ptype = str(getattr(permit.type, "value", permit.type))
    return ic.check(permit_type=ptype, area_id=permit.areaId, plant_id=permit.plantId, impairments=views,
                    rules=await _load_rules(db, permit.plantId), now=now, satisfied=satisfied, running=running)


async def _permit_for_controls(db: AsyncSession, user: User, permit_id: str) -> Permit:
    from app.services.ptw_access import load_permit_or_403

    return await load_permit_or_403(db, permit_id, user, "PTW.READ")


@router.get("/{permit_id}/controls")
async def get_controls(permit_id: str, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    permit = await _permit_for_controls(db, user, permit_id)
    d = await permit_decision(db, permit)
    s = await loaders.schema(db)
    rows = []
    if s["PermitControl"]:
        rows = (await db.execute(select(PermitControl).where(PermitControl.permitId == permit_id))).scalars().all()
    names = await loaders.load_names(db, {r.satisfiedById for r in rows})
    by_key = {r.controlKey: r for r in rows}
    return {
        "effect": d.effect,
        "blocking": d.blockingMessages,
        "impairments": [{"code": h.impairment.code, "systemName": h.impairment.systemName,
                         "effect": h.rule.effect} for h in d.hits],
        "controls": [
            {"key": c, "label": ic.control_label(c), "satisfied": c not in d.missingControls,
             "satisfiedAt": by_key[c].satisfiedAt.isoformat() if c in by_key else None,
             "satisfiedBy": names.get(by_key[c].satisfiedById) if c in by_key else
             ("Fire watch assigned on permit" if c == "fire_watch" and permit.fireWatchPersonId else None),
             "note": by_key[c].note if c in by_key else None,
             "needsFireOfficer": c == "fire_officer_cosign"}
            for c in d.requiredControls
        ],
        "missing": d.missingControls,
        "ready": bool(s.get("PermitControl")),
    }


async def _can_tick(db: AsyncSession, user: User, permit: Permit, key: str) -> None:
    from app.services.ptw_access import is_permit_party

    if key == "fire_officer_cosign":
        if not await _allowed(db, user, IMP_APPROVE, permit.plantId):
            raise HTTPException(403, "Fire Officer co-sign needs the impairment-approval permission")
        return
    if not (await is_permit_party(db, permit, user.id) or await _allowed(db, user, "PTW.UPDATE", permit.plantId)):
        raise HTTPException(403, "Only a party to this permit can record its controls")


@router.post("/{permit_id}/controls")
async def tick_control(permit_id: str, body: ControlIn, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_tables(db, "PermitControl")
    permit = await _permit_for_controls(db, user, permit_id)
    await _can_tick(db, user, permit, body.controlKey)
    existing = (await db.execute(select(PermitControl).where(PermitControl.permitId == permit_id)
                                 .where(PermitControl.controlKey == body.controlKey))).scalar_one_or_none()
    if existing is None:
        db.add(PermitControl(permitId=permit_id, controlKey=body.controlKey, satisfiedById=user.id,
                             note=body.note, attachmentId=body.attachmentId))
    else:
        existing.note, existing.attachmentId = body.note or existing.note, body.attachmentId or existing.attachmentId
    await db.commit()
    engine.trigger(permit.plantId)
    return await get_controls(permit_id, user, db)


@router.delete("/{permit_id}/controls/{control_key}")
async def untick_control(permit_id: str, control_key: str, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_tables(db, "PermitControl")
    permit = await _permit_for_controls(db, user, permit_id)
    await _can_tick(db, user, permit, control_key)
    row = (await db.execute(select(PermitControl).where(PermitControl.permitId == permit_id)
                            .where(PermitControl.controlKey == control_key))).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    engine.trigger(permit.plantId)
    return await get_controls(permit_id, user, db)
