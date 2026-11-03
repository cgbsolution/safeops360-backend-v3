"""HIRA calibration v2 endpoints (feature flag hira.calibration.v2).

  GET  /api/hira/calibration/config?plantId=     effective settings + flag mode (HIRA.READ)
  PUT  /api/hira/calibration/config              new settings version for the tenant or a plant (HIRA.LIBRARY_MANAGE)
  GET  /api/hira/calibration/shadow-runs         v1 / v2 side-by-side log summary (HIRA.LIBRARY_MANAGE)
  POST /api/hira/entries/{id}/calibration/apply  one-click: existing controls / recommendations / defaults (HIRA.UPDATE)

The review itself is GET /api/copilot/hira-review/{id} (v1, shadow or v2 by the flag).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services.hira_calibration import config as cal_config
from app.services.hira_calibration import loader, service
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/hira", tags=["hira-calibration"])

_TOP_KEYS = set(cal_config.DEFAULT_CONFIG)


class CalibrationConfigIn(BaseModel):
    plantId: str | None = None
    mode: Literal["OFF", "SHADOW", "ON"] = "OFF"
    config: dict[str, Any] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=1000)


class CalibrationApplyIn(BaseModel):
    parts: list[Literal["EXISTING_CONTROLS", "CONFIRM_ROUTINE_CONTROLS", "RECOMMENDATIONS", "DEFAULTS"]] = (
        Field(min_length=1)
    )
    changeReason: str | None = Field(default=None, max_length=500)
    recommendationIds: list[str] | None = None


async def _require(db: AsyncSession, user: User, code: str, plant_id: str | None) -> None:
    check = await can(db, user.id, code, PermissionContext(plant_id=plant_id) if plant_id else None)
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")


def _validate(cfg: dict[str, Any]) -> None:
    unknown = set(cfg) - _TOP_KEYS
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown settings: {', '.join(sorted(unknown))}"
        )
    th = cal_config.deep_merge(cal_config.DEFAULT_CONFIG, cfg)["thresholds"]
    try:
        ok = 1 <= int(th["lowMax"]) < int(th["moderateMax"]) < int(th["highMax"]) < 25
    except (TypeError, ValueError, KeyError):
        ok = False
    if not ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "thresholds must be whole numbers with lowMax < moderateMax < highMax < 25",
        )


@router.get("/calibration/config")
async def get_calibration_config(
    plant_id: str | None = Query(None, alias="plantId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, "HIRA.READ", plant_id)
    cfg, mode, versions = await cal_config.load(db, plant_id)
    return {
        "flag": cal_config.FLAG,
        "plantId": plant_id,
        "mode": mode,
        "config": cfg,
        "versions": versions,
        "engineVersion": cal_config.ENGINE_VERSION,
        "schemaReady": await loader.calibration_schema_ready(db),
    }


@router.put("/calibration/config")
async def put_calibration_config(
    payload: CalibrationConfigIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.models.hira_calibration import HiraCalibrationConfig  # noqa: PLC0415

    await _require(db, user, "HIRA.LIBRARY_MANAGE", payload.plantId)
    if not await cal_config.table_exists(db, "HiraCalibrationConfig"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The calibration tables are not created yet: run "
            "`npx tsx prisma/apply-hira-calibration-ddl.ts` in safeops_360 first.",
        )
    _validate(payload.config)
    q = select(func.max(HiraCalibrationConfig.version))
    q = (
        q.where(HiraCalibrationConfig.plantId.is_(None))
        if payload.plantId is None
        else q.where(HiraCalibrationConfig.plantId == payload.plantId)
    )
    current = (await db.execute(q)).scalar() or 0
    row = HiraCalibrationConfig(
        plantId=payload.plantId,
        version=current + 1,
        mode=payload.mode,
        config=payload.config,
        isActive=True,
        note=payload.note,
        createdById=user.id,
    )
    db.add(row)
    await db.commit()
    cfg, mode, versions = await cal_config.load(db, payload.plantId)
    return {
        "flag": cal_config.FLAG,
        "plantId": payload.plantId,
        "mode": mode,
        "config": cfg,
        "versions": versions,
        "engineVersion": cal_config.ENGINE_VERSION,
    }


@router.get("/calibration/scope")
async def calibration_scope(
    plant_id: str | None = Query(None, alias="plantId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What the calibrated review is validated for (the Copilot panel shows it),
    and the plant's pack."""
    from app.services.hira_calibration import rubric_packs  # noqa: PLC0415

    await _require(db, user, "HIRA.READ", plant_id)
    cfg, mode, _ = await cal_config.load(db, plant_id)
    _, pack = rubric_packs.apply_pack(cfg, cfg.get("rubricPack"))
    scope = rubric_packs.scope_for(pack, "TASK_BASED")
    return {
        "mode": mode,
        "validatedFor": rubric_packs.validated_for(),
        "pack": scope["pack"],
        "validated": scope["validated"],
        "banner": scope["banner"],
        "packs": rubric_packs.list_packs(),
    }


@router.get("/calibration/shadow-runs")
async def calibration_shadow_runs(
    plant_id: str | None = Query(None, alias="plantId"),
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.models.hira_calibration import HiraCopilotShadowRun  # noqa: PLC0415

    await _require(db, user, "HIRA.LIBRARY_MANAGE", plant_id)
    if not await cal_config.table_exists(db, "hira_copilot_shadow_runs"):
        return {"runs": [], "stats": {}, "note": "Shadow table not created yet."}
    q = select(HiraCopilotShadowRun).order_by(HiraCopilotShadowRun.created_at.desc()).limit(limit)
    if plant_id:
        q = q.where(HiraCopilotShadowRun.plant_id == plant_id)
    rows = (await db.execute(q)).scalars().all()
    v2_status = Counter(((r.summary or {}).get("v2") or {}).get("status") for r in rows)
    recs = [((r.summary or {}).get("v2") or {}).get("recommendations") or 0 for r in rows]
    return {
        "runs": [
            {
                "id": r.id,
                "at": r.created_at.isoformat() if r.created_at else None,
                "surface": r.surface,
                "entryId": r.entry_id,
                "plantId": r.plant_id,
                "summary": r.summary,
            }
            for r in rows
        ],
        "stats": {
            "count": len(rows),
            "v2Status": dict(v2_status),
            "avgV2Recommendations": round(sum(recs) / len(recs), 2) if recs else None,
        },
    }


@router.post("/entries/{entry_id}/calibration/apply")
async def apply_calibration(
    entry_id: str,
    payload: CalibrationApplyIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.models.hira import HiraEntry, HiraStudy  # noqa: PLC0415
    from app.routers.hira import _study_ctx  # noqa: PLC0415

    entry = await db.get(HiraEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "HIRA entry not found")
    study = await db.get(HiraStudy, entry.studyId)
    check = await can(db, user.id, "HIRA.UPDATE", _study_ctx(study, entry.id))
    if not check.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, check.reason or "Access denied")
    _, mode, _ = await cal_config.load(db, study.plantId)
    if mode != "ON":
        await _require(
            db, user, "HIRA.LIBRARY_MANAGE", study.plantId
        )  # preview only, for calibration managers
    return await service.apply(
        db,
        entry_id,
        parts=list(payload.parts),
        change_reason=payload.changeReason,
        user=user,
        selected_recommendations=payload.recommendationIds,
    )
