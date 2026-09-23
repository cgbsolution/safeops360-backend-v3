"""Tenant-specific roles that stand in for stock workflow roles.

Workflow definitions name stock roles on their steps (HSE_MANAGER, PLANT_HEAD,
PERMIT_ISSUER …). Those stock roles carry ALL_PLANTS grants on several modules,
so a tenant sharing this database (Meridian Retail) gets its own own-plant-only
clones instead (scripts/meridian_retail/base.py). This table tells the workflow
engine which clone may take which step — ONLY for assignee picking and the
step-role gate; it grants no permission and widens no scope anywhere else.

A user who holds none of these roles (every pre-existing user) is unaffected.
"""

from __future__ import annotations

from collections.abc import Iterable

STANDS_IN_FOR: dict[str, frozenset[str]] = {
    "RETAIL_OPS_ADMIN": frozenset({"HSE_MANAGER", "SAFETY_OFFICER", "PLANT_HSE_HEAD", "EMERGENCY_RESPONSE_COORDINATOR"}),
    "RETAIL_STORE_MANAGER": frozenset({"PLANT_HEAD", "DEPARTMENT_HEAD", "SUPERVISOR", "FACTORY_MANAGER"}),
    "RETAIL_DC_MAINTENANCE": frozenset({"PERMIT_ISSUER", "MAINTENANCE_HEAD"}),
    "RETAIL_FIRE_TECHNICIAN": frozenset({"FIELD_TECHNICIAN"}),
    "RETAIL_FLOOR_STAFF": frozenset({"WORKER"}),
    "RETAIL_FIRE_AUDITOR": frozenset({"LEAD_AUDITOR", "AUDITOR"}),
    "RETAIL_PROJECTS": frozenset({"CONTRACTOR_COORDINATOR"}),
}


def expand_step_roles(role_codes: Iterable[str]) -> set[str]:
    """The step's roles plus every tenant role that stands in for one of them."""
    wanted = set(role_codes)
    return wanted | {r for r, stock in STANDS_IN_FOR.items() if stock & wanted}


def satisfies(user_role_codes: Iterable[str], required: str) -> bool:
    """True if the user holds `required` or a tenant role standing in for it."""
    held = set(user_role_codes)
    return required in held or any(required in STANDS_IN_FOR.get(r, ()) for r in held)
