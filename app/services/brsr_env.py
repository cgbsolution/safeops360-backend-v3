"""Environmental capture: emission calculation and per-cycle totals.

Two jobs.

**Emission calculation.** A line carrying an activity quantity (litres of
diesel, kWh of grid electricity) is multiplied by the emission factor that
applies to it, and the factor is FROZEN onto the line — value, unit and
citation. Recomputing later re-resolves the factor only when explicitly asked;
a factor revision published next year therefore cannot silently restate a
number a reviewer has already signed off. Scope 3 is captured as a single
manual field on the header and is never computed here: the build spec puts a
Scope 3 engine explicitly out of scope.

**Totals.** The aggregates the report generator, the FactoryEnvPeriod rollup and
the year-over-year dashboard all need, computed once, from the lines.

Deterministic and offline — no LLM, no external service, the same constraint the
Insight Engine is built under. A BRSR figure has to be reproducible from the
same inputs two years later when an assurance provider asks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brsr import (
    SCOPE_1,
    SCOPE_2,
    SLOT_NA,
    STREAM_EMISSIONS,
    STREAM_ENERGY,
    STREAM_WASTE,
    STREAM_WATER,
    BrsrEmissionFactor,
    BrsrEnvironmentalMetric,
    BrsrEnvMetricLine,
)
from app.services import brsr_taxonomy as tax

# Unit conversions into the factor's own `perUnit`. A line may be captured in a
# unit the factor is not published in (kWh vs MWh, kg vs tonne); anything not
# listed here is treated as already matching and is NOT silently scaled.
_UNIT_ALIASES: dict[tuple[str, str], float] = {
    ("MWH", "KWH"): 1_000.0,
    ("KWH", "MWH"): 0.001,
    ("GJ", "KWH"): 277.778,
    ("KWH", "GJ"): 0.0036,
    ("TONNE", "KG"): 1_000.0,
    ("KG", "TONNE"): 0.001,
    ("MT", "KG"): 1_000.0,
    ("KG", "MT"): 0.001,
    ("KL", "LITRE"): 1_000.0,
    ("LITRE", "KL"): 0.001,
}


def _convert(quantity: float, from_unit: str | None, to_unit: str | None) -> float | None:
    """Scale `quantity` from one unit to another, or None if we cannot.

    Returning None rather than guessing is deliberate: an unconvertible pair
    means the caller records no computed emission and the UI shows the line as
    needing a unit fix. Multiplying by 1.0 and hoping would produce a wrong
    number that looks exactly like a right one.
    """
    if from_unit is None or to_unit is None:
        return None
    a, b = from_unit.strip().upper(), to_unit.strip().upper()
    if a == b:
        return quantity
    factor = _UNIT_ALIASES.get((a, b))
    if factor is None:
        return None
    return quantity * factor


async def resolve_factor(
    db: AsyncSession,
    *,
    factor_type: str,
    scope: str,
    category_code: str,
    as_of: datetime | None = None,
    region: str | None = None,
) -> BrsrEmissionFactor | None:
    """The active factor for a category at a point in time.

    Preference order: an exact `code` match on the category, then a regional
    factor, then the all-India one. `as_of` defaults to now — the report
    generator passes the cycle's period end so a re-run against a closed year
    picks the factor that was current THEN, not the one current today.
    """
    at = as_of or datetime.now(timezone.utc)

    stmt = select(BrsrEmissionFactor).where(
        BrsrEmissionFactor.factorType == factor_type,
        BrsrEmissionFactor.scope == scope,
        BrsrEmissionFactor.isActive.is_(True),
    )
    rows = list((await db.execute(stmt)).scalars())

    def _valid(f: BrsrEmissionFactor) -> bool:
        if f.validFrom is not None and f.validFrom > at:
            return False
        if f.validUntil is not None and f.validUntil < at:
            return False
        return True

    rows = [f for f in rows if _valid(f)]
    if not rows:
        return None

    exact = [f for f in rows if f.code == category_code]
    if exact:
        rows = exact
    if region:
        regional = [f for f in rows if f.region == region]
        if regional:
            return regional[0]
    national = [f for f in rows if f.region is None]
    return (national or rows)[0]


async def compute_line_emission(
    db: AsyncSession,
    line: BrsrEnvMetricLine,
    *,
    as_of: datetime | None = None,
    region: str | None = None,
) -> BrsrEnvMetricLine:
    """Fill `computedTCo2e` and freeze the factor used onto the line.

    Mutates and returns the line; the caller commits. Never raises on a missing
    factor or an unconvertible unit — the line simply keeps `computedTCo2e =
    None` and the capture form surfaces it as unresolved. A half-built
    environmental submission must stay saveable.
    """
    if line.stream != STREAM_EMISSIONS or line.quantity is None:
        return line

    cat = tax.get(line.categoryCode)
    if cat is None:
        return line

    # A directly-entered total IS the answer — multiplying it by a factor would
    # be an order-of-magnitude error, so these bypass the factor path entirely.
    if line.categoryCode in tax.DIRECT_TOTAL_CATEGORIES:
        line.computedTCo2e = line.quantity
        line.scope = cat.scope
        line.emissionFactorId = None
        line.factorValue = None
        line.factorPerUnit = None
        line.factorSource = "Entered directly — no emission factor applied"
        return line

    if cat.factorType is None or cat.scope is None:
        return line

    factor = await resolve_factor(
        db,
        factor_type=cat.factorType,
        scope=cat.scope,
        category_code=line.categoryCode,
        as_of=as_of,
        region=region,
    )
    if factor is None:
        line.computedTCo2e = None
        line.scope = cat.scope
        return line

    qty = _convert(line.quantity, line.unit or cat.unit, factor.perUnit)
    if qty is None:
        line.computedTCo2e = None
        line.scope = cat.scope
        return line

    line.scope = factor.scope
    line.emissionFactorId = factor.id
    line.factorValue = factor.factorValue
    line.factorPerUnit = factor.perUnit
    # Frozen citation — the string the report prints under the figure.
    line.factorSource = (
        f"{factor.source} ({factor.sourceYear})" if factor.sourceYear else factor.source
    )
    line.computedTCo2e = qty * factor.factorValue
    return line


# ── Totals ──────────────────────────────────────────────────────────────────


@dataclass
class EnvTotals:
    """Everything downstream needs, computed once from the lines."""

    energyRenewableGj: float = 0.0
    energyNonRenewableGj: float = 0.0
    energyTotalGj: float = 0.0

    waterWithdrawnKl: float = 0.0
    waterDischargedKl: float = 0.0
    waterConsumedKl: float = 0.0
    waterRecycledKl: float = 0.0

    scope1TCo2e: float = 0.0
    scope2TCo2e: float = 0.0
    scope3TCo2e: float | None = None

    wasteGeneratedT: float = 0.0
    wasteRecoveredT: float = 0.0
    wasteDisposedT: float = 0.0
    wasteDivertedPct: float | None = None

    # Denominators, summed across the sites included in the total.
    turnoverInr: float | None = None
    productionVolume: float | None = None
    productionUnit: str | None = None

    # Per-site coverage, so a dashboard can say "3 of 5 sites reported" rather
    # than presenting a partial total as if it were the whole entity.
    siteCount: int = 0
    siteIds: list[str] = field(default_factory=list)
    # Emission lines that could not be resolved (no factor / bad unit). Surfaced
    # rather than swallowed — an unresolved line is a hole in the disclosure.
    unresolvedEmissionLines: int = 0

    def intensity(self, numerator: float) -> dict[str, float | None]:
        """Per-rupee and per-unit-output intensity for a figure.

        Returns None for a denominator that is absent or zero rather than
        raising or substituting 1 — BRSR allows a turnover intensity and a
        physical intensity, and an entity may legitimately have only one.
        """
        return {
            "perTurnoverInr": (
                numerator / self.turnoverInr
                if self.turnoverInr not in (None, 0)
                else None
            ),
            "perProductionUnit": (
                numerator / self.productionVolume
                if self.productionVolume not in (None, 0)
                else None
            ),
        }


def totals_from_lines(
    metrics: list[BrsrEnvironmentalMetric],
    lines_by_metric: dict[str, list[BrsrEnvMetricLine]],
) -> EnvTotals:
    """Aggregate a set of environmental submissions into one EnvTotals.

    Pure — takes already-loaded rows so it can serve the report generator, the
    Facilities rollup and the dashboard without three different queries.
    """
    t = EnvTotals()

    for metric in metrics:
        t.siteCount += 1
        t.siteIds.append(metric.siteId)

        if metric.turnoverInr is not None:
            t.turnoverInr = (t.turnoverInr or 0.0) + metric.turnoverInr
        if metric.productionVolume is not None:
            t.productionVolume = (t.productionVolume or 0.0) + metric.productionVolume
            # Only meaningful when every site reports the same unit; the
            # generator checks this and omits physical intensity if they differ.
            t.productionUnit = t.productionUnit or metric.productionUnit
        if metric.scope3TCo2e is not None:
            t.scope3TCo2e = (t.scope3TCo2e or 0.0) + metric.scope3TCo2e

        for line in lines_by_metric.get(metric.id, []):
            qty = line.quantity or 0.0
            cat = tax.get(line.categoryCode)

            if line.stream == STREAM_ENERGY:
                if cat is not None and cat.isRenewable:
                    t.energyRenewableGj += qty
                else:
                    t.energyNonRenewableGj += qty

            elif line.stream == STREAM_WATER:
                if line.flowType == tax.FLOW_WITHDRAWAL:
                    t.waterWithdrawnKl += qty
                elif line.flowType == tax.FLOW_DISCHARGE:
                    t.waterDischargedKl += qty
                elif line.flowType == tax.FLOW_CONSUMPTION:
                    t.waterConsumedKl += qty
                elif line.flowType == tax.FLOW_RECYCLED:
                    t.waterRecycledKl += qty

            elif line.stream == STREAM_WASTE:
                if line.flowType == tax.WASTE_GENERATED:
                    t.wasteGeneratedT += qty
                elif line.flowType in tax.WASTE_RECOVERY_FLOWS:
                    t.wasteRecoveredT += qty
                elif line.flowType in tax.WASTE_DISPOSAL_FLOWS:
                    t.wasteDisposedT += qty

            elif line.stream == STREAM_EMISSIONS:
                if line.computedTCo2e is None:
                    if qty:
                        t.unresolvedEmissionLines += 1
                    continue
                if line.scope == SCOPE_1:
                    t.scope1TCo2e += line.computedTCo2e
                elif line.scope == SCOPE_2:
                    t.scope2TCo2e += line.computedTCo2e

    t.energyTotalGj = t.energyRenewableGj + t.energyNonRenewableGj
    if t.wasteGeneratedT:
        t.wasteDivertedPct = round(100.0 * t.wasteRecoveredT / t.wasteGeneratedT, 2)

    return t


async def load_totals(
    db: AsyncSession,
    cycle_id: str,
    *,
    site_ids: list[str] | None = None,
) -> EnvTotals:
    """EnvTotals for a cycle, optionally narrowed to specific sites.

    Only VERIFIED and SUBMITTED submissions are counted. A DRAFT is somebody
    mid-entry; rolling it into a disclosure total would make the report move
    under the reader.
    """
    stmt = select(BrsrEnvironmentalMetric).where(
        BrsrEnvironmentalMetric.cycleId == cycle_id,
        BrsrEnvironmentalMetric.isDeleted.is_(False),
        BrsrEnvironmentalMetric.status.in_(["SUBMITTED", "VERIFIED"]),
    )
    if site_ids:
        stmt = stmt.where(BrsrEnvironmentalMetric.siteId.in_(site_ids))
    metrics = list((await db.execute(stmt)).scalars())
    if not metrics:
        return EnvTotals()

    line_rows = list(
        (
            await db.execute(
                select(BrsrEnvMetricLine).where(
                    BrsrEnvMetricLine.metricId.in_([m.id for m in metrics])
                )
            )
        ).scalars()
    )
    by_metric: dict[str, list[BrsrEnvMetricLine]] = {}
    for line in line_rows:
        by_metric.setdefault(line.metricId, []).append(line)

    return totals_from_lines(metrics, by_metric)


def blank_slots() -> list[dict]:
    """Every capture slot the taxonomy defines, as empty line payloads.

    The form renders from this so an operator sees the full BRSR grid — including
    the categories that are zero for them — rather than an empty page they have
    to know how to populate. A category left blank stays blank; it is not
    persisted as a zero, because "not applicable to this site" and "measured
    zero" are different disclosures.
    """
    out: list[dict] = []
    for cat in tax.ALL_CATEGORIES:
        for flow in cat.flows:
            if cat.stream == STREAM_WATER and flow == tax.FLOW_DISCHARGE:
                for dest_code, dest_label in tax.WATER_DISCHARGE_DESTINATIONS:
                    out.append(
                        {
                            "stream": cat.stream,
                            "categoryCode": cat.code,
                            "categoryLabel": cat.label,
                            "flowType": flow,
                            "destination": dest_code,
                            "destinationLabel": dest_label,
                            "unit": cat.unit,
                            "scope": cat.scope,
                            "guidance": cat.guidance,
                        }
                    )
                continue
            out.append(
                {
                    "stream": cat.stream,
                    "categoryCode": cat.code,
                    "categoryLabel": cat.label,
                    "flowType": flow,
                    "destination": SLOT_NA,
                    "destinationLabel": None,
                    "unit": cat.unit,
                    "scope": cat.scope,
                    "guidance": cat.guidance,
                }
            )
    return out


__all__ = [
    "EnvTotals",
    "blank_slots",
    "compute_line_emission",
    "load_totals",
    "resolve_factor",
    "totals_from_lines",
]
