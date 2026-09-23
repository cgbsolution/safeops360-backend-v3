"""Which tenant's controlled documents apply at which site.

Fire checklist templates are a client's controlled documents (the Page
Industries sheets carry PIL/EHS/CL numbers). This database hosts more than one
tenant, so a template may name its owner in `documentMeta.tenant`, matched
against the display-label profile of the asset's plant (PlantDisplayProfile):

    template tenant == plant profile   → applies
    (untagged template, unprofiled plant: None == None → applies)

Every pre-existing template is untagged and every pre-existing plant has no
profile, so nothing changes for them; a RETAIL-tagged template applies only to
Meridian Retail sites, and Meridian Retail sites are never owed a Page sheet.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from app.models.display_label import PlantDisplayProfile


def template_tenant(tpl: Any) -> str | None:
    return (getattr(tpl, "documentMeta", None) or {}).get("tenant") or None


def applies(tpl: Any, plant_profile: str | None) -> bool:
    return template_tenant(tpl) == (plant_profile or None)


async def plant_profiles(db, plant_ids: Iterable[str | None]) -> dict[str, str | None]:
    ids = sorted({p for p in plant_ids if p})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(PlantDisplayProfile.plantId, PlantDisplayProfile.profileCode).where(
                PlantDisplayProfile.plantId.in_(ids)
            )
        )
    ).all()
    found = dict(rows)
    return {p: found.get(p) for p in ids}


async def plant_profile(db, plant_id: str | None) -> str | None:
    return (await plant_profiles(db, [plant_id])).get(plant_id) if plant_id else None
