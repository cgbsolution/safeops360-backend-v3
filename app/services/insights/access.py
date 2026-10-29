"""Who may see a module's aggregate insights, and for which plants.

Insight cards are plant-level aggregates that name records ("8 investigations
stalled: INC-…"). Before this module the insight endpoints checked login only,
and with no `plant` param the engine aggregated every plant — an OWN_RECORDS
worker was handed recordCount=131, every incident on the platform.

The rule here: an aggregate needs a *plant-level* read grant (ALL_PLANTS or
OWN_PLANT) on the module's READ permission. OWN_DEPARTMENT / OWN_RECORDS grants
can open individual records but not plant-wide rollups, so they get nothing.
The plant set is the per-permission one (`get_accessible_plants_for`), never
the loose any-module helper.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.permissions import get_accessible_plants_for, permission_scopes

# List-screen module key → the READ permission guarding its records.
INSIGHT_MODULE_READ: dict[str, str] = {
    "incident": "INCIDENT.READ",
    "nearmiss": "NEAR_MISS.READ",
    "observation": "OBSERVATION.READ",
    "hira": "HIRA.READ",
    "eai": "EAI.READ",
    "combined-risk": "RISK.COMBINED_VIEW",
    "capa": "CAPA.READ",
    "moc": "MOC.READ",
}

_PLANT_LEVEL = {"ALL_PLANTS", "OWN_PLANT"}


@dataclass(frozen=True)
class AggregateAccess:
    allowed: bool              # holds a plant-level grant on the permission
    plants: list[str] | None   # None == every plant; [] == none


async def aggregate_access(db: AsyncSession, user_id: str, permission_code: str) -> AggregateAccess:
    scopes = await permission_scopes(db, user_id, permission_code)
    if not (set(scopes) & _PLANT_LEVEL):
        return AggregateAccess(allowed=False, plants=[])
    plants = await get_accessible_plants_for(db, user_id, permission_code)
    return AggregateAccess(allowed=True, plants=plants)


__all__ = ["INSIGHT_MODULE_READ", "AggregateAccess", "aggregate_access"]
