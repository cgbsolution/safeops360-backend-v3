"""Platform-wide query scoping (P1-2).

A single, fail-closed source of truth for *which rows an actor may see*. Every
list/get query routes a SELECT through `QueryScope.apply()` before it touches the
DB — no query returns a row outside the actor's plant (and, when present, tenant).

Two axes:
  • plant scope — which plants this actor may read (today's isolation boundary)
  • tenant seam — `tenantId` is filtered unconditionally when the model carries it;
    today single-tenant (DEPLOYMENT_TENANT_ID), so the filter is always-true but
    present — multi-client isolation then costs zero additional work.

Built from the permission-SPECIFIC scope (`get_accessible_plants_for`), so a list
query never shows rows the per-record `can(<read_permission>)` check would deny.
`all_plants` is True only when the actor holds ALL_PLANTS on the read permission.
No plants → empty result (fail closed), never "all rows".

System/job actors use `system_scope(plant_ids)` and MUST declare an explicit
scope — a job never defaults to "all plants".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import false
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.permissions import get_accessible_plants_for

DEPLOYMENT_TENANT_ID = "default"  # single-tenant deployment; seam for multi-client


@dataclass
class QueryScope:
    actor_id: str
    all_plants: bool                       # True == unrestricted (ALL_PLANTS on read perm)
    plant_ids: list[str] = field(default_factory=list)  # [] == fail-closed (nothing visible)
    tenant_id: str = DEPLOYMENT_TENANT_ID
    is_system: bool = False

    def apply(self, stmt, model, plant_attr: str = "plantId"):
        """Scope a SELECT to this actor: tenant (if the model has tenantId) + plant.
        Models without a plant column are returned unscoped on the plant axis."""
        if hasattr(model, "tenantId"):
            stmt = stmt.where(getattr(model, "tenantId") == self.tenant_id)
        if self.all_plants:
            return stmt
        col = getattr(model, plant_attr, None)
        if col is None:
            # try the common alternate column name (CAMS / Facilities use siteId)
            col = getattr(model, "siteId", None)
        if col is None:
            return stmt  # not plant-scoped → tenant filter only
        if not self.plant_ids:
            return stmt.where(false())  # fail closed — no plants, no rows
        return stmt.where(col.in_(self.plant_ids))

    def plant_filter(self, column):
        """A boolean clause for ad-hoc queries that can't pass a model
        (e.g. raw column on a join). all_plants → no clause (True)."""
        if self.all_plants:
            return True
        return column.in_(self.plant_ids) if self.plant_ids else false()

    def allows_plant(self, plant_id: str | None) -> bool:
        return self.all_plants or (plant_id is not None and plant_id in self.plant_ids)


async def build_query_scope(db: AsyncSession, actor_id: str, read_permission: str) -> QueryScope:
    """Build the scope for a human actor from their grant on `read_permission`.
    ALL_PLANTS → unrestricted; OWN_PLANT/etc → their plant set; permission absent
    → empty (fail closed)."""
    plants = await get_accessible_plants_for(db, actor_id, read_permission)
    if plants is None:
        return QueryScope(actor_id=actor_id, all_plants=True, plant_ids=[])
    return QueryScope(actor_id=actor_id, all_plants=False, plant_ids=list(plants))


def system_scope(plant_ids: list[str], *, all_plants: bool = False, job_name: str = "system") -> QueryScope:
    """Scope for a background job/automated process. MUST declare its plant scope;
    a job that wants all plants states it explicitly — it never defaults to it."""
    return QueryScope(
        actor_id=f"SYSTEM:{job_name}",
        all_plants=all_plants,
        plant_ids=list(plant_ids),
        is_system=True,
    )


# ─── Record-register scope (plant + department + own records) ──────────────
#
# QueryScope above narrows on the plant axis only, which is right for modules
# whose read grants are plant-wide. The operational registers (PTW, incident,
# near miss, observation, FLRA, HIRA) also hand out OWN_DEPARTMENT and
# OWN_RECORDS grants, and their detail endpoints enforce those through can().
# A register built from the plant set alone showed a department head every
# department and, before that, `get_accessible_plants()` showed every plant to
# anyone holding ALL_PLANTS on any module. RecordScope mirrors can()'s rules so
# a list never shows a row its own detail endpoint would refuse.


@dataclass
class RecordScope:
    user_id: str
    scopes: set[str]                 # scopes held on the read permission (empty = none)
    plant_ids: list[str]             # the user's plants (unused when unrestricted)
    department: str | None           # the user's department name

    @property
    def unrestricted(self) -> bool:
        return "ALL_PLANTS" in self.scopes

    def filter(self, plant_col, *, owner=None, department_col=None, department_clause=None, owner_always=False):
        """WHERE clause for a register query; None == no restriction.

        plant_col          the row's plant column
        owner              clause matching rows the user is party to (OWN_RECORDS)
        department_col     the row's Department.id column, if it has one
        department_clause  alternative: callable(dept_name, plant_ids) -> clause
        owner_always       party rows are visible whatever the scope (PTW rule)
        """
        from sqlalchemy import and_, func, or_, select

        from app.models.masters import Department

        if self.unrestricted:
            return None
        clauses = []
        if "OWN_PLANT" in self.scopes and self.plant_ids:
            clauses.append(plant_col.in_(self.plant_ids))
        if "OWN_DEPARTMENT" in self.scopes and self.department and self.plant_ids:
            if department_col is not None:
                dept_ids = select(Department.id).where(
                    func.lower(Department.name) == self.department.strip().lower(),
                    Department.plantId.in_(self.plant_ids),
                )
                clauses.append(and_(plant_col.in_(self.plant_ids), department_col.in_(dept_ids)))
            elif department_clause is not None:
                clauses.append(and_(plant_col.in_(self.plant_ids), department_clause(self.department, self.plant_ids)))
        if owner is not None and (owner_always or self.scopes & {"OWN_DEPARTMENT", "OWN_RECORDS"}):
            clauses.append(owner)
        return or_(*clauses) if clauses else false()

    def apply(self, stmt, plant_col, **kw):
        clause = self.filter(plant_col, **kw)
        return stmt if clause is None else stmt.where(clause)

    def allows_plant(self, plant_id: str | None) -> bool:
        """Plant-level visibility (aggregates, plant-keyed lookups): ALL_PLANTS,
        or a plant-wide/department grant at one of the user's plants."""
        if self.unrestricted:
            return True
        return bool(plant_id) and plant_id in self.plant_ids and bool(self.scopes & {"OWN_PLANT", "OWN_DEPARTMENT", "OWN_RECORDS"})


async def build_record_scope(db: AsyncSession, actor_id: str, read_permission: str) -> RecordScope:
    from app.services.permissions import _load_user_profile, permission_scopes

    scopes = set(await permission_scopes(db, actor_id, read_permission))
    profile = await _load_user_profile(db, actor_id)
    return RecordScope(
        user_id=actor_id,
        scopes=scopes,
        plant_ids=sorted(profile.plantIds) if profile and profile.plantIds else [],
        department=profile.department if profile else None,
    )
