"""AI Insights router — deterministic, airgap-safe insight layer for list screens.

`GET /api/insights/{module}` returns bar-level insights + row-level signals for
a module, scoped to a plant + date range. This is the house GET-analytics
convention (cams/dashboard/risk-dashboard) standing in for the spec's
`POST /api/ai-insights/:module`: the engine pulls from the existing models
server-side rather than the browser shipping the record set up (spec §1.1). The
response is fully computed from real records; no network egress occurs.

Scope: cards are plant-level aggregates that name records, so the caller needs
a plant-level grant (ALL_PLANTS / OWN_PLANT) on the module's READ permission
(services/insights/access.py). A `plant` outside the caller's plants is refused;
with no `plant`, the engine runs over exactly the caller's plants — never over
every plant unless the caller is unrestricted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.insights import InsightResponse
from app.services.insights import SUPPORTED_MODULES, compute, compute_for_plants
from app.services.insights.access import INSIGHT_MODULE_READ, AggregateAccess, aggregate_access

router = APIRouter(prefix="/api/insights", tags=["insights"])
log = logging.getLogger("safeops360.insights")

# Weekly Insight Engine (hero + secondary row). Only observation is wired in the
# pilot; the internal module name differs from the list-screen key.
_WEEKLY_MODULE_MAP = {"observation": "safety_observation"}
# Above this many plants the per-plant fan-out stops being cheap; the bar is
# suppressed with a reason rather than silently widened to every plant.
_FANOUT_CAP = 12


def _check_plant(access: AggregateAccess, plant: str | None) -> None:
    if plant and access.plants is not None and plant not in access.plants:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot read insights for this plant.")


def _empty_weekly(module: str, scope: str) -> dict:
    return {"module": module, "weekOf": None, "scope": scope, "hero": None, "row": [], "moreCount": 0, "empty": None}


@router.get("/modules")
async def list_modules(
    user: User = Depends(get_current_user),  # noqa: ARG001 — auth gate only
) -> dict[str, list[str]]:
    return {"modules": list(SUPPORTED_MODULES)}


@router.get("/{module}/weekly")
async def module_weekly(
    module: str,
    plant: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Weekly Insight Engine view: the 'This week's focus' hero + secondary row.
    Unrestricted readers get the persisted platform-wide week; a plant-scoped
    reader (or an explicit `plant`) gets an in-memory view over just those
    plants. Degrades to an empty view on any engine error (e.g. the
    InsightSnapshot table not yet applied) so the list screen never breaks."""
    internal = _WEEKLY_MODULE_MAP.get(module)
    if internal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No weekly insight engine for module '{module}'")
    access = await aggregate_access(db, user.id, INSIGHT_MODULE_READ[module])
    if not access.allowed:
        return _empty_weekly(internal, "restricted")
    _check_plant(access, plant)
    from app.services.insights.weekly import compute_weekly_view, get_current_week_view

    try:
        if plant:
            return await compute_weekly_view(db, module=internal, plants=[plant])
        if access.plants is None:
            return await get_current_week_view(db, module=internal)
        return await compute_weekly_view(db, module=internal, plants=access.plants)
    except Exception as e:  # noqa: BLE001 — never break the screen on an engine/DDL issue
        log.warning("weekly insight view failed for %s: %s", internal, e)
        await db.rollback()
        return _empty_weekly(internal, "error")


@router.get("/{module}", response_model=InsightResponse)
async def module_insights(
    module: str,
    plant: str | None = Query(default=None, description="Active plant id to scope insights to"),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InsightResponse:
    if module not in SUPPORTED_MODULES:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No insight rules for module '{module}'. Supported: {', '.join(SUPPORTED_MODULES)}",
        )
    access = await aggregate_access(db, user.id, INSIGHT_MODULE_READ[module])
    if not access.allowed:
        # Record-level readers (OWN_RECORDS / OWN_DEPARTMENT) see no plant-wide
        # rollups; an empty, suppressed bar keeps the list screen unchanged.
        return InsightResponse(
            module=module, plant=plant, generatedAt=datetime.now(timezone.utc),
            bar=[], signals=[], recordCount=0, suppressed=True, reason="restricted", cached=False,
        )
    _check_plant(access, plant)
    if plant or access.plants is None:
        return await compute(db, module, plant=plant, date_from=date_from, date_to=date_to)
    if len(access.plants) > _FANOUT_CAP:
        return InsightResponse(
            module=module, plant=None, generatedAt=datetime.now(timezone.utc),
            bar=[], signals=[], recordCount=0, suppressed=True, reason="too_many_sites", cached=False,
        )
    return await compute_for_plants(
        db, module, access.plants, date_from=date_from, date_to=date_to
    )
