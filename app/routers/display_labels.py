"""Display-label overrides for the active plant.

`GET /api/display-labels` → `{plantId, profile, labels}` where `labels` holds
ONLY the override rows. The client merges them over the hardcoded default it
passes at every call site, so an empty map means "render exactly as before".

Plant resolution: ?plantId= → X-Active-Plant header (the plant switcher cookie,
forwarded by the Next proxy) → the caller's home plant. Labels are vocabulary,
not data, so no per-module permission applies beyond being logged in.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.display_label import PlantDisplayProfile
from app.models.user import User
from app.services.display_labels import labels_for_plant

router = APIRouter(prefix="/api/display-labels", tags=["display-labels"])


@router.get("")
async def get_display_labels(
    plantId: str | None = Query(default=None),
    x_active_plant: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    plant_id = plantId or x_active_plant or user.plantId
    labels = await labels_for_plant(db, plant_id)
    profile = None
    if plant_id and labels:
        profile = (
            await db.execute(select(PlantDisplayProfile.profileCode).where(PlantDisplayProfile.plantId == plant_id))
        ).scalar_one_or_none()
    return {"plantId": plant_id, "profile": profile, "labels": labels}
