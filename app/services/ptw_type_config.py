"""Resolve and enforce per-plant permit-type curation.

Contract: a plant with no PlantPermitTypeConfig row gets `None` from
`config_for_plant`, which every caller treats as "full platform set, Hot Work
default" — so existing sites are untouched. The wizard hides what is disabled;
this module is the authority that refuses it server-side.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import PermitHazardType, PermitType
from app.models.ptw_type_config import PlantPermitTypeConfig
from app.services.ptw_hazards import hazard_for_base_type

log = logging.getLogger("safeops360.ptw_type_config")

ALL_TYPES: list[str] = [t.value for t in PermitType]


async def config_for_plant(db: AsyncSession, plant_id: str | None) -> PlantPermitTypeConfig | None:
    if not plant_id:
        return None
    return await db.get(PlantPermitTypeConfig, plant_id)


async def configs_for_plants(db: AsyncSession, plant_ids: Iterable[str]) -> dict[str, dict]:
    ids = [p for p in plant_ids if p]
    if not ids:
        return {}
    rows = (
        await db.execute(select(PlantPermitTypeConfig).where(PlantPermitTypeConfig.plantId.in_(ids)))
    ).scalars().all()
    return {r.plantId: to_dict(r) for r in rows}


def to_dict(cfg: PlantPermitTypeConfig) -> dict:
    return {
        "enabledTypes": [t for t in ALL_TYPES if t in set(cfg.enabledTypes)],
        "defaultType": cfg.defaultType,
        "blockedHazards": sorted(h.value for h in blocked_hazards(cfg)),
    }


def blocked_hazards(cfg: PlantPermitTypeConfig | None) -> set[PermitHazardType]:
    """Annexures that belong to a removed permit type (Confined Space, Excavation…).

    A hazard stays attachable if any still-enabled type contributes it.
    """
    if cfg is None:
        return set()
    enabled = set(cfg.enabledTypes)
    kept = {hazard_for_base_type(t) for t in enabled}
    return {hazard_for_base_type(t) for t in ALL_TYPES if t not in enabled} - kept


async def assert_type_allowed(
    db: AsyncSession,
    plant_id: str,
    permit_type: PermitType | str,
    hazards: Iterable[PermitHazardType] = (),
) -> None:
    cfg = await config_for_plant(db, plant_id)
    if cfg is None:
        return
    value = permit_type.value if isinstance(permit_type, PermitType) else str(permit_type)
    if value not in cfg.enabledTypes:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{value.replace('_', ' ').title()} permits are not used at this site.",
        )
    bad = sorted(h.value for h in set(hazards) & blocked_hazards(cfg))
    if bad:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Hazard annexure not used at this site: {', '.join(bad)}.",
        )
