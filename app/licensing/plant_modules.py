"""Per-plant on/off for modules that are mounted UNGATED.

Several routers are mounted without a licence gate because the licence in use
predates their module codes (see the comments on each in app/main.py). That
left no way to switch one of them off for a single plant — the licence ceiling
can't express it and the per-factory matrix only restricts licensed codes.

This reuses the same FactoryModuleEntitlement rows with the same opt-out
semantics: NO ROW → ON. An explicit `enabled = false` row switches the module
off for that plant, in the API (403) and in the nav / route guard (via
`disabledModules` on /api/licensing/modules). Every existing plant has no row
for these codes, so it behaves exactly as before.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Header, HTTPException, Request, status

from app.licensing import factory_entitlements

# code → routers (by _ROUTERS key in app/main.py) that belong to it.
UNGATED_MODULES: dict[str, tuple[str, ...]] = {
    "ALERTS": ("alerts",),                       # Daily Brief
    "LOTO": ("loto",),
    "FIRE": ("fire_audits", "fire_safety", "fire_checklists"),
    "CAPTURE": ("capture",),                     # Field Reports / Guided Capture
    "BRSR": ("brsr",),
    "BUSINESS_EXCELLENCE": ("business_excellence", "business_excellence_p2"),
    "TRAINING_ENGINE": ("training_engine",),     # training rule engine
    "SCORECARD": ("scorecard",),
    "SIGNALS": ("signals",),
    # General-purpose CAMS (every audit domain other than Fire Safety). Switched
    # off → the plant keeps CAMS for Fire Safety engagements only: the
    # ComplianceAudit engine, programme, completion packs and assurance are
    # refused, and cams_fire_only_guard narrows the shared /api/cams router.
    "CAMS_GENERAL": ("audit_compliance", "programme", "cams_completion", "assurance"),
}

ROUTER_PLANT_MODULE: dict[str, str] = {r: code for code, routers in UNGATED_MODULES.items() for r in routers}


def disabled_for_plant(plant_id: str | None) -> list[str]:
    """Ungated module codes switched off for this plant (empty with no rows)."""
    if not plant_id:
        return []
    return sorted(c for c in UNGATED_MODULES if not factory_entitlements.is_enabled_for_plant(c, plant_id))


def require_plant_module(code: str) -> Callable:
    """403 when the active plant has this ungated module switched off."""

    async def _checker(x_active_plant: str | None = Header(default=None)) -> None:
        if x_active_plant and not factory_entitlements.is_enabled_for_plant(code, x_active_plant):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "MODULE_DISABLED_FOR_PLANT",
                    "module": code,
                    "plantId": x_active_plant,
                    "message": "This module is not enabled for this site.",
                },
            )

    return _checker


def cams_fire_only(plant_id: str | None) -> bool:
    """True when this plant runs CAMS for Fire Safety engagements only."""
    return bool(plant_id) and not factory_entitlements.is_enabled_for_plant("CAMS_GENERAL", plant_id)


async def cams_fire_only_guard(
    request: Request,
    x_active_plant: str | None = Header(default=None),
) -> None:
    """Router dependency on /api/cams. A no-op unless the active plant is
    CAMS fire-only; then:
      * config writes (audit types, templates, recurrences, compliance links)
        and generic engagement creation are refused — Fire Safety audits are
        created through /api/fire/audits;
      * any /engagements/{id}… or /findings/{id}… call must address a FIRE
        engagement (or a finding on one).
    Reads of the plant's own engagements are already plant-scoped, and a
    fire-only plant has no non-fire engagements to read."""
    if not cams_fire_only(x_active_plant):
        return
    from app.core.db import AsyncSessionLocal
    from app.models.cams import CamsEngagement, CamsFinding

    parts = [p for p in request.url.path.split("/") if p][2:]  # after api/cams
    head = parts[0] if parts else ""
    method = request.method.upper()
    deny = HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={"code": "CAMS_FIRE_ONLY", "message": "This site uses CAMS for Fire Safety audits only."},
    )
    if head in ("audit-types", "templates", "recurrences", "compliance") and method != "GET":
        raise deny
    if head == "engagements" and len(parts) == 1 and method == "POST":
        raise deny
    if head in ("engagements", "findings") and len(parts) >= 2:
        async with AsyncSessionLocal() as db:
            if head == "engagements":
                eng = await db.get(CamsEngagement, parts[1])
            else:
                f = await db.get(CamsFinding, parts[1])
                eng = await db.get(CamsEngagement, f.engagementId) if f else None
            if eng is not None and eng.sourceModule != "FIRE":
                raise deny
