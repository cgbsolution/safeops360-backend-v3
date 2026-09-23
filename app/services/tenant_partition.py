"""Row-level tenant partition for modules that carry a `tenantId` column (EPC).

This database hosts more than one demo tenant, told apart by the display-label
profile of a plant (PlantDisplayProfile — see services/fire_tenancy.py). A
caller's partition is the profile of their HOME plant:

    partition(user) = PlantDisplayProfile.profileCode for user.plantId
                      else "default"

Every pre-existing row carries tenantId = 'default' and every pre-existing plant
has no profile, so existing users resolve to "default" and see exactly what
they always did. A Meridian Retail user resolves to "RETAIL" and sees only
RETAIL rows; rows of another partition behave as if they do not exist (404).

Human-readable codes (SITE-0001, CC-0001, CW-2026-0001, …) stay globally
counted — the columns are globally unique — so code generators must NOT be
filtered by partition.
"""

from __future__ import annotations

from typing import Any, TypeVar

from app.services.fire_tenancy import plant_profile

DEFAULT_PARTITION = "default"

_CACHE_KEY = "tenant_partition"

T = TypeVar("T")


async def partition_for(db, user) -> str:
    """The caller's partition. Cached on the (per-request) session."""
    cache: dict = db.info.setdefault(_CACHE_KEY, {})
    key = (getattr(user, "id", None), getattr(user, "plantId", None))
    if key not in cache:
        cache[key] = (
            await plant_profile(db, getattr(user, "plantId", None))
        ) or DEFAULT_PARTITION
    return cache[key]


def in_partition(row: Any, partition: str) -> bool:
    return row is not None and (getattr(row, "tenantId", None) or DEFAULT_PARTITION) == partition


async def get_scoped(db, model: type[T], row_id: str | None, partition: str) -> T | None:
    """`db.get` that treats a row of another partition as missing."""
    if not row_id:
        return None
    row = await db.get(model, row_id)
    return row if in_partition(row, partition) else None


async def is_foreign(db, model: type, row_id: str | None, partition: str) -> bool:
    """True only when the row EXISTS in another partition. For endpoints that
    never checked existence before: a missing id keeps its old behaviour, a
    foreign id is refused."""
    if not row_id:
        return False
    row = await db.get(model, row_id)
    return row is not None and not in_partition(row, partition)
