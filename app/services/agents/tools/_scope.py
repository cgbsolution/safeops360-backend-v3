"""Keep agent-tool reads inside what the invoking user may read.

Why this exists
---------------
Tools run with the system DB session and receive no user, so before this
every tool could read any plant: `find_similar_incidents` searched all
plants, `get_active_permits_at_time` took whatever `plantId` the model
chose, and the results landed in the invocation for the user to see. An
agent acting for a plant-scoped user must not show them records their own
screens would refuse.

The agent runtime sets the invoking user for the duration of a run with
`tool_scope(user_id)`. Tools then narrow their queries to the plants that
user holds for the permission guarding the data they read — the same
per-permission plant set the list endpoints use. A user with ALL_PLANTS on
that permission keeps cross-plant ("sister plant") results; a run without a
user (tests, scripts) is unrestricted.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.permissions import get_accessible_plants_for, permission_scopes

# READ permission guarding each agent source module's records.
MODULE_READ_PERMISSION: dict[str, str] = {
    "INCIDENT": "INCIDENT.READ",
    "PTW": "PTW.READ",
    "HIRA": "HIRA.READ",
    "CAPA": "CAPA.READ",
    "OBSERVATION": "OBSERVATION.READ",
    "NEAR_MISS": "NEAR_MISS.READ",
}


@dataclass
class _ToolScope:
    user_id: str
    plants_by_permission: dict[str, list[str] | None] = field(default_factory=dict)


_current: ContextVar[_ToolScope | None] = ContextVar("agent_tool_scope", default=None)


@contextmanager
def tool_scope(user_id: str | None) -> Iterator[None]:
    """Run the enclosed tool calls on behalf of `user_id` (None = unrestricted)."""
    token = _current.set(_ToolScope(user_id) if user_id else None)
    try:
        yield
    finally:
        _current.reset(token)


async def readable_plants(db: AsyncSession, permission_code: str) -> list[str] | None:
    """Plant ids the invoking user holds `permission_code` in; None = unrestricted."""
    scope = _current.get()
    if scope is None:
        return None
    if permission_code not in scope.plants_by_permission:
        scopes = await permission_scopes(db, scope.user_id, permission_code)
        if scopes and scopes <= {"OWN_RECORDS"}:
            # Own-records users may read only their own rows; these tools
            # return other people's records, so they get none.
            plants: list[str] | None = []
        else:
            plants = await get_accessible_plants_for(db, scope.user_id, permission_code)
        scope.plants_by_permission[permission_code] = plants
    return scope.plants_by_permission[permission_code]


def restrict_to_plants(stmt: Any, column: Any, plants: list[str] | None) -> Any:
    """Add `column IN plants` unless unrestricted. An empty list matches nothing."""
    if plants is None:
        return stmt
    return stmt.where(column.in_(plants))


async def plant_is_readable(db: AsyncSession, permission_code: str, plant_id: str | None) -> bool:
    plants = await readable_plants(db, permission_code)
    return plants is None or (plant_id is not None and plant_id in plants)


__all__ = [
    "MODULE_READ_PERMISSION",
    "plant_is_readable",
    "readable_plants",
    "restrict_to_plants",
    "tool_scope",
]
