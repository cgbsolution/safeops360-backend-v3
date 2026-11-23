"""Snap & Report + the live observation dashboard (JSL Hisar: "observation /
concern reporting with real-time dashboard & analytics; picture gives
everything").

  POST /api/observations/snap-analyze   photo → prefilled, vocabulary-clamped card
  GET  /api/observations/snap-context   plants, areas, nearest area for a GPS fix
  GET  /api/observations/live           plant-scoped real-time aggregate

The observation itself is still created by POST /api/observations and the photo
attached through /api/observations/{id}/attachments. Nothing here writes an
observation, so every create-time gate (taxonomy, quality, severity override,
SLA, workflow) applies to Snap & Report unchanged.

MOUNT ORDER: like observation_sla / observation_severity this router MUST be
mounted BEFORE `observations` in app/main.py _ROUTERS, because observations.py
owns `GET /api/observations/{observation_id}`, which would otherwise swallow
`/live` and `/snap-context` as observation ids.

RBAC: snap-* need OBSERVATION.CREATE (the caller is about to create one);
/live needs OBSERVATION.READ and is plant-scoped through build_query_scope.
Mounted ungated like ims / capture; per-endpoint RBAC is the gate.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.plant import Area, Plant
from app.models.user import User
from app.services import observation_snap as snap
from app.services.access_scope import build_query_scope
from app.services.permissions import PermissionContext, can

router = APIRouter(tags=["observations"])

READ, CREATE = "OBSERVATION.READ", "OBSERVATION.CREATE"
MAX_IMAGE_BYTES = 5 * 1024 * 1024


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


def ai_enabled() -> bool:
    """AI on when the platform's Anthropic key is configured, unless the
    AI_SNAP_REPORT kill switch is explicitly off."""
    from app.services.ai.anthropic_client import is_configured

    flag = (os.getenv("AI_SNAP_REPORT") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return is_configured()


async def _plants_for(db: AsyncSession, user: User, perm: str) -> list[Plant]:
    scope = await build_query_scope(db, user.id, perm)
    stmt = select(Plant).order_by(Plant.name.asc())
    if not scope.all_plants:
        stmt = stmt.where(Plant.id.in_(scope.plant_ids or ["__none__"]))
    return list((await db.execute(stmt.limit(200))).scalars().all())


async def _nearest(db: AsyncSession, plant_id: str | None, lat: float | None, lng: float | None) -> dict[str, Any] | None:
    if not plant_id or lat is None or lng is None:
        return None
    centroids = snap.area_centroids(await snap.geotag_points(db, plant_id))
    hit = snap.nearest_area(centroids, lat, lng)
    if not hit:
        return None
    area = await db.get(Area, hit[0])
    if area is None:
        return None
    return {"areaId": area.id, "areaName": area.name, "distanceMetres": round(hit[1]), "basis": "GEOTAGGED_HISTORY"}


# ── snap context ─────────────────────────────────────────────────────────────
@router.get("/api/observations/snap-context")
async def snap_context(
    plantId: str | None = Query(None), lat: float | None = Query(None, ge=-90, le=90),
    lng: float | None = Query(None, ge=-180, le=180),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Everything the Snap & Report page needs besides the photo: the plants the
    caller may report in, the chosen plant's areas, and the nearest area to a
    GPS fix. The client keeps the last-used area itself (per user, on-device)."""
    await _require(db, user, CREATE)
    plants = await _plants_for(db, user, CREATE)
    plant_ids = {p.id for p in plants}
    chosen = plantId if plantId in plant_ids else (user.plantId if user.plantId in plant_ids else (plants[0].id if plants else None))
    if plantId and plantId not in plant_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant is outside your access scope")
    areas = []
    if chosen:
        areas = list((await db.execute(select(Area).where(Area.plantId == chosen).order_by(Area.name.asc()))).scalars().all())
    return {
        "plants": [{"id": p.id, "code": p.code, "name": p.name} for p in plants],
        "plantId": chosen,
        "areas": [{"id": a.id, "name": a.name} for a in areas],
        "nearest": await _nearest(db, chosen, lat, lng),
        "aiEnabled": ai_enabled(),
        "minAtRiskDescription": snap.MIN_AT_RISK_DESCRIPTION,
        # Hazard categories a SAFE observation may carry (storable labels only).
        "hazardCategories": snap.storable_categories(),
    }


# ── snap analyze ─────────────────────────────────────────────────────────────
class SnapIn(BaseModel):
    imageB64: str = Field(min_length=16)
    mimeType: str | None = None
    plantId: str | None = None
    areaId: str | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    lang: str = Field("en", max_length=8)


@router.post("/api/observations/snap-analyze")
async def snap_analyze(body: SnapIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Photo → a prefilled observation card. Never 5xx on AI trouble: returns
    {ok: false, reason} and the page falls back to the manual quick form."""
    await _require(db, user, CREATE, body.plantId)
    nearest = await _nearest(db, body.plantId, body.lat, body.lng)
    if not ai_enabled():
        return {"ok": False, "reason": "ai_disabled", "nearest": nearest}
    try:
        image = base64.b64decode(body.imageB64.split(",", 1)[-1], validate=True)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "bad_image", "nearest": nearest}
    if not image or len(image) > MAX_IMAGE_BYTES:
        return {"ok": False, "reason": "bad_image", "nearest": nearest}

    stop_rows = await snap.load_stop_rows(db)
    raw = await snap.analyze_photo(image, stop_rows, lang=body.lang)
    if raw is None:
        return {"ok": False, "reason": "no_suggestion", "nearest": nearest}
    s = snap.clamp_ai_output(raw, stop_rows)
    if s["clamped"]:
        snap.log.info("snap-analyze clamped fields=%s user=%s", s["clamped"], user.id)

    # Severity policy: the matrix value wins the default, the model's reading
    # is a hint. Same resolver the create endpoint re-runs.
    from app.services import observation_severity as sev_svc

    area_for_sev = body.areaId or (nearest or {}).get("areaId")
    suggestion = await sev_svc.resolve(
        db, observation_type=s["observationType"], category=s["categoryCode"], sub_category=s["subCategoryCode"],
        plant_id=body.plantId, area_id=area_for_sev,
    ) if s["categoryCode"] else {"suggested": None, "rationale": sev_svc.NO_RULE_MESSAGE}
    matrix = suggestion.get("suggested")

    labels = {}
    for r in stop_rows:
        if r["categoryCode"] == s["categoryCode"] and r["subCategoryCode"] == s["subCategoryCode"]:
            labels = {"categoryLabel": r["categoryLabel"], "subCategoryLabel": r["subCategoryLabel"]}
            break
    return {
        "ok": True,
        "suggestion": {
            "observationType": s["observationType"],
            "categoryCode": s["categoryCode"], "subCategoryCode": s["subCategoryCode"],
            "category": s["category"], **labels,
            "severity": matrix or s["severity"],
            "aiSeverity": s["severity"],
            "matrixSeverity": matrix,
            "matrixRationale": suggestion.get("rationale") if matrix else None,
            "title": s["title"], "description": s["description"], "likelyHazard": s["likelyHazard"],
            "immediateAction": s["immediateAction"],
            "composedDescription": snap.compose_description(s["title"], s["description"], s["likelyHazard"]),
            "lowConfidence": s["confidence"] < 0.5,
        },
        "nearest": nearest,
    }


# ── live dashboard ───────────────────────────────────────────────────────────
@router.get("/api/observations/live")
async def live(
    plantId: str | None = Query(None), windowDays: int = Query(30, ge=7, le=90),
    tzOffsetMinutes: int = Query(330, ge=-720, le=840),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, READ)
    scope = await build_query_scope(db, user.id, READ)
    if plantId:
        if not scope.allows_plant(plantId):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant is outside your access scope")
        plants: list[str] | None = [plantId]
    else:
        plants = None if scope.all_plants else scope.plant_ids
    data = await snap.live_dashboard(db, plant_ids=plants, tz_offset_minutes=tzOffsetMinutes, window_days=windowDays)
    return {**data, "plantId": plantId}
