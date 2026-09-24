"""Per-plant PTW permit-type curation.

`GET /api/ptw-type-config` → `{allTypes, configs: {plantId: {enabledTypes,
defaultType, blockedHazards}}}` for the plants the caller can act in. Only
plants WITH a curation row appear in `configs`; a plant missing from the map
uses the full platform set and the Hot Work default.

Mounted outside /api/ptw so it can never collide with `/api/ptw/{permit_id}`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.ptw_type_config import PlantPermitTypeConfig
from app.models.user import User
from app.services.permissions import get_accessible_plants
from app.services.ptw_type_config import ALL_TYPES, configs_for_plants

router = APIRouter(prefix="/api/ptw-type-config", tags=["ptw"])


@router.get("")
async def get_ptw_type_config(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    plants = await get_accessible_plants(db, user.id)
    if plants is None:
        plants = (await db.execute(select(PlantPermitTypeConfig.plantId))).scalars().all()
    return {"allTypes": ALL_TYPES, "configs": await configs_for_plants(db, plants)}
