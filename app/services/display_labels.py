"""Resolve per-plant display-label overrides.

Contract (the whole point of the layer):
    label(labels, key, default)  → the override if one exists and is non-blank,
                                   otherwise `default` — never "" and never None.

Callers always pass the literal they rendered before this layer existed, so a
plant with no profile row renders byte-identically to the pre-refactor output.

Cache: plantId → {key → label}, 60s TTL. Label rows change rarely (seed/admin)
and a stale minute is harmless; `invalidate()` clears it after a write.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.display_label import DisplayLabel, DisplayLabelProfile, PlantDisplayProfile

log = logging.getLogger("safeops360.display_labels")

_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, dict[str, str]]] = {}

Labels = Mapping[str, str]
EMPTY: dict[str, str] = {}


def label(labels: Labels | None, key: str, default: str) -> str:
    """Override for `key`, falling through to `default` on a missing/blank row."""
    if labels:
        v = labels.get(key)
        if v is not None and v.strip():
            return v
    return default


def invalidate() -> None:
    _cache.clear()


async def labels_for_plant(db: AsyncSession, plant_id: str | None) -> dict[str, str]:
    """Override rows for the plant's active profile ({} when it has none).

    Fails SAFE: any lookup error returns {} so rendering falls back to the
    hardcoded defaults rather than breaking a page or an export.
    """
    if not plant_id:
        return EMPTY
    hit = _cache.get(plant_id)
    now = time.monotonic()
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    try:
        rows = (
            await db.execute(
                select(DisplayLabel.key, DisplayLabel.label)
                .join(PlantDisplayProfile, PlantDisplayProfile.profileCode == DisplayLabel.profileCode)
                .join(DisplayLabelProfile, DisplayLabelProfile.code == DisplayLabel.profileCode)
                .where(PlantDisplayProfile.plantId == plant_id, DisplayLabelProfile.isActive.is_(True))
            )
        ).all()
        out = {k: v for k, v in rows if v and v.strip()}
    except Exception as e:  # noqa: BLE001
        log.warning("display-label lookup failed for plant %s: %s", plant_id, e)
        return EMPTY
    _cache[plant_id] = (now, out)
    return out


async def labels_for_plants(db: AsyncSession, plant_ids: Iterable[str] | None) -> dict[str, str]:
    """Labels for a multi-plant document (e.g. a consolidated export).

    Applies overrides only when EVERY plant in scope shares the same profile;
    a mixed scope keeps the defaults so no plant is described in another
    tenant's vocabulary.
    """
    ids = [p for p in (plant_ids or []) if p]
    if not ids:
        return EMPTY
    first = await labels_for_plant(db, ids[0])
    for pid in ids[1:]:
        if await labels_for_plant(db, pid) != first:
            return EMPTY
    return first
