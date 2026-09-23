"""Derives `FactoryEnvPeriod` from BRSR environmental capture.

Facilities already owned an environmental table before BRSR existed:
`FactoryEnvPeriod`, one flat row per (site, period), read live by the Facilities
→ Compliance & Audit tab's Environmental Operational rollup. BRSR needs the same
underlying facts at a far finer grain — energy by carrier, water by source and
destination, waste by category × disposal operation.

Capturing both independently would have produced two Scope 1 numbers on two
screens with no way to tell which was right. So BRSR is the source of truth and
this module recomputes the Facilities row FROM it.

Three properties this is built to hold:

* **Additive, not destructive.** A site with no BRSR submission keeps whatever
  `FactoryEnvPeriod` row it already has. The Facilities tab predates BRSR and
  its existing seeded data must not vanish because a new module shipped.

* **Targets are never touched.** `energyTargetKwh` and `wasteDivertedTargetPct`
  are Facilities' own planning fields with no BRSR equivalent. They are
  explicitly excluded from the write, not merely absent from it — a future
  refactor that starts writing the whole row would otherwise silently null them.

* **Only SUBMITTED / VERIFIED data propagates.** Someone typing a draft must not
  move a number on a dashboard another department is reading.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brsr import (
    BrsrEnvironmentalMetric,
    BrsrEnvMetricLine,
)
from app.models.factory import FactoryEnvPeriod, FactoryProfile
from app.services import brsr_env

log = logging.getLogger("safeops360.brsr")

# kWh per GJ. FactoryEnvPeriod stores energy in kWh; BRSR captures GJ.
_KWH_PER_GJ = 277.778


async def derive_for_metric(
    db: AsyncSession, metric: BrsrEnvironmentalMetric
) -> FactoryEnvPeriod | None:
    """Recompute the Facilities row for one BRSR environmental submission.

    Returns the updated/created `FactoryEnvPeriod`, or None when the site has no
    `FactoryProfile` to hang it from — which is a normal state, not an error: a
    plant can report environmental data for BRSR long before anyone fills in its
    factory profile. Caller commits.
    """
    if metric.status not in ("SUBMITTED", "VERIFIED"):
        return None

    profile = (
        await db.execute(
            select(FactoryProfile).where(
                FactoryProfile.siteId == metric.siteId,
                FactoryProfile.isDeleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        log.info(
            "BRSR env rollup: no FactoryProfile for site %s — skipping FactoryEnvPeriod derivation",
            metric.siteId,
        )
        return None

    lines = list(
        (
            await db.execute(
                select(BrsrEnvMetricLine).where(BrsrEnvMetricLine.metricId == metric.id)
            )
        ).scalars()
    )
    totals = brsr_env.totals_from_lines([metric], {metric.id: lines})

    row = (
        await db.execute(
            select(FactoryEnvPeriod).where(
                FactoryEnvPeriod.factoryProfileId == profile.id,
                FactoryEnvPeriod.periodLabel == metric.periodLabel,
            )
        )
    ).scalar_one_or_none()

    created = row is None
    if row is None:
        row = FactoryEnvPeriod(
            factoryProfileId=profile.id,
            siteId=metric.siteId,
            periodLabel=metric.periodLabel,
        )
        db.add(row)

    energy_kwh = totals.energyTotalGj * _KWH_PER_GJ if totals.energyTotalGj else None

    row.energyKwh = energy_kwh
    if energy_kwh is not None and metric.productionVolume:
        row.energyIntensity = energy_kwh / metric.productionVolume
    row.waterWithdrawnKl = totals.waterWithdrawnKl or None
    row.effluentDischargedKl = totals.waterDischargedKl or None
    row.wasteGeneratedT = totals.wasteGeneratedT or None
    row.wasteDivertedPct = totals.wasteDivertedPct
    row.scope1TCo2e = totals.scope1TCo2e or None
    row.scope2TCo2e = totals.scope2TCo2e or None
    if metric.consentStatus:
        row.consentStatus = metric.consentStatus

    # NOT written, deliberately — see the module docstring:
    #   energyTargetKwh, wasteDivertedTargetPct  (Facilities' own planning fields)
    #   etpStatus                                (owned by the Facilities ETP flow)

    row.updatedBy = "brsr_env_rollup"
    row.updatedAt = datetime.now(timezone.utc)
    row.notes = (
        f"Derived from BRSR environmental capture ({metric.periodLabel}, "
        f"status {metric.status}). Edit the BRSR submission, not this row."
    )

    log.info(
        "BRSR env rollup: %s FactoryEnvPeriod for site=%s period=%s "
        "(scope1=%.2f scope2=%.2f energyGJ=%.2f)",
        "created" if created else "updated",
        metric.siteId,
        metric.periodLabel,
        totals.scope1TCo2e,
        totals.scope2TCo2e,
        totals.energyTotalGj,
    )
    return row


async def derive_for_cycle(db: AsyncSession, cycle_id: str) -> int:
    """Recompute every Facilities row a cycle's submissions feed.

    Returns the number of `FactoryEnvPeriod` rows written. Caller commits.
    """
    metrics = list(
        (
            await db.execute(
                select(BrsrEnvironmentalMetric).where(
                    BrsrEnvironmentalMetric.cycleId == cycle_id,
                    BrsrEnvironmentalMetric.isDeleted.is_(False),
                    BrsrEnvironmentalMetric.status.in_(["SUBMITTED", "VERIFIED"]),
                )
            )
        ).scalars()
    )
    written = 0
    for metric in metrics:
        if await derive_for_metric(db, metric) is not None:
            written += 1
    return written


__all__ = ["derive_for_cycle", "derive_for_metric"]
