"""The Principle Mapping Engine — auto-populating BRSR from platform data.

Every resolver answers one question: *what is this indicator's value for this
cycle, and which records back it?* It returns both, always, because a figure
without its records is not an assurable disclosure — the drill-through is the
feature, not a nicety on top of it.

    BrsrDataSource(indicatorCode, resolverKey, args)
              │
              ▼
       RESOLVERS[resolverKey](db, cycle, **args)  ──►  Resolved
              │                                          │
              │                                          ├─ value
              │                                          ├─ sourceRecordRefs  ← the drill-through
              │                                          └─ derivationNote
              ▼
       BrsrIndicatorValue(provenance=AUTO, …)

Four rules the engine holds to:

1. **A resolver never overwrites a human.** A value whose provenance is MANUAL,
   AUTO_OVERRIDDEN or NOT_APPLICABLE is left exactly as it is. Re-running the
   engine mid-review must be safe, or nobody will run it.

2. **A resolver that finds nothing writes nothing.** It returns None and the
   indicator stays unanswered, which reads in the UI as "needs input" — not as
   a confident zero. A real measured zero is a person entering 0.

3. **A resolver that raises does not stop the run.** It is recorded as a failed
   source for that one indicator and the sweep continues. One module being
   unentitled, un-migrated or empty must not block the other eight principles.

4. **Record refs carry labels, not just ids.** `{"module", "entity", "id",
   "label"}` — the drill-through has to render a row for a record the reader may
   not be entitled to open, and the platform already has a standing rule against
   rendering a raw cuid.

P2, P4, P7 and P8 have no resolvers. That is a deliberate, visible state, not an
oversight: `MANUAL_ONLY_PRINCIPLES` drives a "not sourced from platform data"
banner so the operator knows those need direct input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brsr import (
    ANSWERED_PROVENANCES,
    MANUAL_ONLY_PRINCIPLES,
    PLATFORM_SOURCED_PRINCIPLES,
    PRINCIPLES,
    PROVENANCE_AUTO,
    PROVENANCE_AUTO_OVERRIDDEN,
    PROVENANCE_MANUAL,
    PROVENANCE_NOT_APPLICABLE,
    VALUE_TYPE_BOOLEAN,
    VALUE_TYPE_NUMBER,
    VALUE_TYPE_TABLE,
    BrsrDataSource,
    BrsrIndicator,
    BrsrIndicatorValue,
    BrsrPrincipleResponse,
    BrsrReportingCycle,
)
from app.models.epc import ContractorCompany, ContractorWorker
from app.models.erm_t3 import Control, ControlTest, VendorAssessment, VendorProfile
from app.models.factory import SocialComplianceProfile, WorkforceComposition
from app.models.incident import Incident, IncidentType
from app.models.near_miss import NearMiss
from app.models.training_engine import TrainingAssignment
from app.services import brsr_env, frequency_rates

log = logging.getLogger("safeops360.brsr")


@dataclass
class Resolved:
    """One resolver's answer."""

    value: Any
    unit: str | None = None
    recordRefs: list[dict] = field(default_factory=list)
    recordCount: int | None = None
    note: str | None = None


def _ref(module: str, entity: str, rec_id: str, label: str) -> dict:
    """A drill-through reference. Label is mandatory by construction."""
    return {"module": module, "entity": entity, "id": rec_id, "label": label}


def _fy_bounds(cycle: BrsrReportingCycle) -> tuple[datetime, datetime]:
    return cycle.periodStart, cycle.periodEnd


# ═══════════════════════════════════════════════════════════════════════════
#  P1 — Ethics, Transparency & Accountability  (source: ERM internal controls)
# ═══════════════════════════════════════════════════════════════════════════


async def resolve_control_count(db: AsyncSession, cycle: BrsrReportingCycle, **kw) -> Resolved | None:
    """Active internal controls in the register, optionally filtered to key ones."""
    key_only = bool(kw.get("keyControlsOnly"))
    stmt = select(Control).where(Control.isActive.is_(True), Control.isDeleted.is_(False))
    if key_only:
        stmt = stmt.where(Control.isKeyControl.is_(True))
    rows = list((await db.execute(stmt)).scalars())
    if not rows:
        return None
    return Resolved(
        value=float(len(rows)),
        unit="controls",
        recordRefs=[
            _ref("ERM", "Control", c.id, f"{c.controlCode} — {c.name}") for c in rows[:200]
        ],
        recordCount=len(rows),
        note=(
            f"Count of active {'key ' if key_only else ''}controls in the ERM internal-controls "
            f"register as at {cycle.periodEnd:%d %b %Y}."
        ),
    )


async def resolve_control_test_coverage(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """Percentage of active controls tested within the reporting period."""
    start, end = _fy_bounds(cycle)
    controls = list(
        (
            await db.execute(
                select(Control).where(Control.isActive.is_(True), Control.isDeleted.is_(False))
            )
        ).scalars()
    )
    if not controls:
        return None

    tests = list(
        (
            await db.execute(
                select(ControlTest).where(
                    ControlTest.controlId.in_([c.id for c in controls]),
                    ControlTest.createdAt >= start,
                    ControlTest.createdAt <= end,
                )
            )
        ).scalars()
    )
    tested_ids = {t.controlId for t in tests}
    pct = round(100.0 * len(tested_ids) / len(controls), 2)
    by_id = {c.id: c for c in controls}
    return Resolved(
        value=pct,
        unit="%",
        recordRefs=[
            _ref("ERM", "Control", cid, f"{by_id[cid].controlCode} — {by_id[cid].name}")
            for cid in list(tested_ids)[:200]
            if cid in by_id
        ],
        recordCount=len(tested_ids),
        note=(
            f"{len(tested_ids)} of {len(controls)} active controls were tested between "
            f"{start:%d %b %Y} and {end:%d %b %Y} (ERM control-testing cycle)."
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  P3 — Employee Wellbeing & Safety
#       (sources: Manhours, Incident, NearMiss, Training, WorkforceComposition)
# ═══════════════════════════════════════════════════════════════════════════


# ⚠ Raw SQL, deliberately. The SQLAlchemy `Manhours` model is out of sync with
# the actual DB: Prisma owns the schema and uses employeeHours / contractorHours
# / rwcCount / fatalityCount, which the SA model misnames as manhoursWorked /
# contractorManhours / fatalCount, and it also declares a `headcount` column
# that does not exist. Selecting the ORM entity therefore raises
# UndefinedColumnError. app/routers/dashboard.py hit the same wall and took the
# same route (see its comment at the Manhours query).
#
# These are the columns that ACTUALLY exist, verified against
# information_schema. Fixing the shared model is a separate change with its own
# blast radius (app/routers/manhours.py and app/schemas/manhours.py move with
# it), so BRSR does not depend on it.
_MANHOURS_SQL = text(
    'SELECT id, year, month, "employeeHours", "contractorHours", '
    '"ltiCount", "mtcCount", "rwcCount", "fatalityCount", "lostDays" '
    'FROM "Manhours" '
    'WHERE make_date(year, month, 1) >= :start AND make_date(year, month, 1) <= :end'
)


async def resolve_safety_metric(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """LTIFR / recordable injuries / fatalities / lost days, from Manhours.

    `metric` selects which. LTIFR is recomputed from the summed components
    rather than averaged across the monthly rows — averaging a rate weights a
    quiet month equally with a busy one, which is simply the wrong number.
    """
    metric = kw.get("metric", "ltifr")
    start, end = _fy_bounds(cycle)

    # Manhours is keyed (plantId, year, month) rather than by a date, so the
    # period filter is built from the composite. That way a financial year
    # straddling two calendar years is captured correctly.
    rows = list(
        (await db.execute(_MANHOURS_SQL, {"start": start.date(), "end": end.date()})).mappings()
    )
    if not rows:
        return None

    total_hours = sum((r["employeeHours"] or 0) + (r["contractorHours"] or 0) for r in rows)
    lti = sum(r["ltiCount"] or 0 for r in rows)
    mtc = sum(r["mtcCount"] or 0 for r in rows)
    rwc = sum(r["rwcCount"] or 0 for r in rows)
    fatal = sum(r["fatalityCount"] or 0 for r in rows)
    lost_days = sum(r["lostDays"] or 0 for r in rows)

    refs = [
        _ref(
            "MANHOURS", "Manhours", r["id"],
            f"{r['year']}-{r['month']:02d} · "
            f"{(r['employeeHours'] or 0) + (r['contractorHours'] or 0):,} person-hours",
        )
        for r in rows[:200]
    ]

    if metric == "ltifr":
        # Per million person-hours worked — the basis BRSR asks for, and the
        # basis services.frequency_rates publishes LTIFR on for every screen.
        # Fatalities are lost-time injuries and are counted here; the previous
        # local formula omitted them, understating the rate at exactly the sites
        # where it matters most. None (never 0) when no exposure was reported.
        value = frequency_rates.ltifr(lti=lti, fatalities=fatal, exposure_hours=total_hours)
        if value is None:
            return None
        note = (
            f"({lti + fatal} lost-time injuries incl. fatalities ÷ {total_hours:,} "
            f"person-hours) × 1,000,000, "
            f"summed across {len(rows)} monthly Manhours records in the reporting period. "
            "Includes contractor hours."
        )
        return Resolved(value=value, unit="per million person-hours", recordRefs=refs,
                        recordCount=len(rows), note=note)

    if metric == "recordable_injuries":
        # Lost-time + restricted-work + medical-treatment + fatal. Restricted
        # work cases ARE recordable; omitting them would understate the figure.
        return Resolved(
            value=float(lti + rwc + mtc + fatal), unit="count", recordRefs=refs,
            recordCount=len(rows),
            note=(
                f"Lost-time ({lti}) + restricted-work ({rwc}) + medical-treatment ({mtc}) "
                f"+ fatal ({fatal}) cases, summed across {len(rows)} monthly Manhours records."
            ),
        )

    if metric == "fatalities":
        return Resolved(value=float(fatal), unit="count", recordRefs=refs, recordCount=len(rows),
                        note=f"Fatal cases recorded across {len(rows)} monthly Manhours records.")

    if metric == "lost_days":
        return Resolved(value=float(lost_days), unit="days", recordRefs=refs, recordCount=len(rows),
                        note="Total days lost to injury across the reporting period.")

    if metric == "manhours_worked":
        return Resolved(value=float(total_hours), unit="person-hours", recordRefs=refs,
                        recordCount=len(rows),
                        note="Employee + contractor person-hours worked in the reporting period.")
    return None


async def resolve_incident_count(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """Incidents of a given type in the period. `incidentTypes` is a list."""
    start, end = _fy_bounds(cycle)
    types = kw.get("incidentTypes") or []
    stmt = select(Incident).where(
        Incident.isDeleted.is_(False),
        Incident.occurredAt >= start,
        Incident.occurredAt <= end,
    )
    if types:
        stmt = stmt.where(Incident.type.in_([IncidentType(t) for t in types]))
    rows = list((await db.execute(stmt)).scalars())
    if not rows:
        return None
    return Resolved(
        value=float(len(rows)),
        unit="count",
        recordRefs=[
            _ref("INCIDENT", "Incident", i.id,
                 f"{i.number} · {i.type.value if hasattr(i.type, 'value') else i.type}")
            for i in rows[:200]
        ],
        recordCount=len(rows),
        note=(
            f"Incidents of type {', '.join(types) if types else 'any'} with an occurrence date "
            f"between {start:%d %b %Y} and {end:%d %b %Y}."
        ),
    )


async def resolve_near_miss_count(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    start, end = _fy_bounds(cycle)
    rows = list(
        (
            await db.execute(
                select(NearMiss).where(NearMiss.date >= start, NearMiss.date <= end)
            )
        ).scalars()
    )
    if not rows:
        return None
    return Resolved(
        value=float(len(rows)),
        unit="count",
        recordRefs=[_ref("NEAR_MISS", "NearMiss", n.id, n.number) for n in rows[:200]],
        recordCount=len(rows),
        note=f"Near-miss reports raised between {start:%d %b %Y} and {end:%d %b %Y}.",
    )


async def resolve_training_coverage(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """Health-&-safety training coverage: completed vs assigned in the period."""
    start, end = _fy_bounds(cycle)
    rows = list(
        (
            await db.execute(
                select(TrainingAssignment).where(
                    TrainingAssignment.isDeleted.is_(False),
                    TrainingAssignment.assignedAt >= start,
                    TrainingAssignment.assignedAt <= end,
                )
            )
        ).scalars()
    )
    if not rows:
        return None
    completed = [r for r in rows if r.completedAt is not None]
    people = {r.personUserId for r in rows}
    pct = round(100.0 * len(completed) / len(rows), 2)
    return Resolved(
        value=pct,
        unit="%",
        recordRefs=[
            _ref("TRAINING", "TrainingAssignment", r.id,
                 f"{r.status} · due {r.dueDate:%d %b %Y}" if r.dueDate else r.status)
            for r in rows[:200]
        ],
        recordCount=len(rows),
        note=(
            f"{len(completed)} of {len(rows)} training assignments completed, covering "
            f"{len(people)} people, for assignments raised in the reporting period."
        ),
    )


async def resolve_workforce_composition(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """Employee / worker headcount grids, from the Facilities workforce records.

    BRSR asks for these as a male/female/total table, so the resolver returns a
    TABLE payload rather than a scalar. `segment` picks permanent vs contract —
    BRSR's "employees" and "workers" distinction maps onto them.
    """
    segment = kw.get("segment", "permanent")
    rows = list(
        (
            await db.execute(
                select(WorkforceComposition).where(
                    WorkforceComposition.isCurrent.is_(True),
                    WorkforceComposition.isDeleted.is_(False),
                )
            )
        ).scalars()
    )
    if not rows:
        return None

    if segment == "permanent":
        total = sum(r.permanentCount for r in rows)
    elif segment == "contract":
        total = sum(r.contractCount for r in rows)
    elif segment == "apprentice":
        total = sum(r.apprenticeTraineeCount for r in rows)
    else:
        total = sum(r.totalCount for r in rows)

    male = sum(r.maleCount for r in rows)
    female = sum(r.femaleCount for r in rows)
    headcount = sum(r.totalCount for r in rows) or 1
    # The gender split is recorded for the site as a whole, not per segment, so
    # it is apportioned. Stated in the note rather than presented as measured.
    est_female = round(total * female / headcount)
    est_male = total - est_female

    return Resolved(
        value={
            "total": total,
            "male": est_male,
            "female": est_female,
            "differentlyAbled": sum(r.differentlyAbledCount or 0 for r in rows),
        },
        unit="persons",
        recordRefs=[
            _ref("FACILITIES", "WorkforceComposition", r.id,
                 f"{r.siteId} · as at {r.asOfDate:%d %b %Y} · {r.totalCount} persons")
            for r in rows[:200]
        ],
        recordCount=len(rows),
        note=(
            f"Current workforce composition across {len(rows)} facility records. "
            f"Gender split is apportioned from the site-level ratio "
            f"({male} male / {female} female of {headcount}) — it is not captured per "
            "employment segment. Verify before filing."
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  P5 — Human Rights  (sources: EPC contractor management, SA8000 social profile)
# ═══════════════════════════════════════════════════════════════════════════


async def resolve_contractor_workforce(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """Contractor headcount and the companies engaging them.

    Neither EPC table carries `isDeleted` — the module retires a worker through
    `overallStatus` / `rosterStatus` instead. So the filter is on status, and
    the note says which statuses were counted rather than implying "everyone".
    """
    excluded = ("EXITED", "TERMINATED", "BLACKLISTED")
    rows = [
        w
        for w in (await db.execute(select(ContractorWorker))).scalars()
        if (w.overallStatus or "") not in excluded
    ]
    if not rows:
        return None
    companies = list(
        (
            await db.execute(
                select(ContractorCompany).where(ContractorCompany.status != "BLACKLISTED")
            )
        ).scalars()
    )
    return Resolved(
        value=float(len(rows)),
        unit="persons",
        recordRefs=[
            _ref("EPC", "ContractorCompany", c.id, f"{c.code} — {c.name}" if c.code else c.name)
            for c in companies[:200]
        ],
        recordCount=len(rows),
        note=(
            f"{len(rows)} contractor workers on the roster across {len(companies)} contractor "
            f"companies in Contractor Management. Excludes workers whose status is one of "
            f"{', '.join(excluded)}."
        ),
    )


async def resolve_social_compliance_flag(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """An SA8000 human-rights assessment field, aggregated across facilities.

    `field` names the column. Returns the count of facilities assessed COMPLIANT
    plus the total assessed, because BRSR asks "% of plants covered by
    assessment" rather than a single verdict.
    """
    column = kw.get("field", "overallSocialComplianceFlag")
    rows = list(
        (
            await db.execute(
                select(SocialComplianceProfile).where(
                    SocialComplianceProfile.isDeleted.is_(False)
                )
            )
        ).scalars()
    )
    if not rows:
        return None
    assessed = [r for r in rows if getattr(r, column, "NOT_ASSESSED") != "NOT_ASSESSED"]
    if not assessed:
        return None
    compliant = [r for r in assessed if getattr(r, column) == "COMPLIANT"]
    pct = round(100.0 * len(compliant) / len(assessed), 2)
    return Resolved(
        value=pct,
        unit="%",
        recordRefs=[
            _ref("FACILITIES", "SocialComplianceProfile", r.id,
                 f"{r.siteId} · {getattr(r, column)} · assessed {r.asOfDate:%d %b %Y}")
            for r in assessed[:200]
        ],
        recordCount=len(assessed),
        note=(
            f"{len(compliant)} of {len(assessed)} assessed facilities are COMPLIANT on "
            f"'{column}' (SA8000 social-compliance profile). "
            f"{len(rows) - len(assessed)} facilities are not yet assessed and are excluded "
            "from the denominator."
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  P6 — Environment  (primary: BRSR environmental capture; secondary: Incident)
# ═══════════════════════════════════════════════════════════════════════════


async def resolve_env_total(db: AsyncSession, cycle: BrsrReportingCycle, **kw) -> Resolved | None:
    """A P6 figure from this cycle's own environmental submissions.

    The primary P6 source. Reads only SUBMITTED / VERIFIED submissions, so a
    draft someone is mid-way through entering never moves a disclosure figure.
    """
    metric = kw.get("metric")
    totals = await brsr_env.load_totals(db, cycle.id)
    if totals.siteCount == 0:
        return None

    mapping: dict[str, tuple[float | None, str]] = {
        "energy_total": (totals.energyTotalGj, "GJ"),
        "energy_renewable": (totals.energyRenewableGj, "GJ"),
        "energy_non_renewable": (totals.energyNonRenewableGj, "GJ"),
        "water_withdrawn": (totals.waterWithdrawnKl, "kL"),
        "water_discharged": (totals.waterDischargedKl, "kL"),
        "water_consumed": (totals.waterConsumedKl, "kL"),
        "water_recycled": (totals.waterRecycledKl, "kL"),
        "scope1": (totals.scope1TCo2e, "tCO2e"),
        "scope2": (totals.scope2TCo2e, "tCO2e"),
        "scope3": (totals.scope3TCo2e, "tCO2e"),
        "waste_generated": (totals.wasteGeneratedT, "MT"),
        "waste_recovered": (totals.wasteRecoveredT, "MT"),
        "waste_disposed": (totals.wasteDisposedT, "MT"),
    }
    if metric not in mapping:
        return None
    value, unit = mapping[metric]
    if value is None:
        return None

    note = (
        f"Summed from {totals.siteCount} facility environmental "
        f"{'submission' if totals.siteCount == 1 else 'submissions'} "
        f"(SUBMITTED or VERIFIED only) for {cycle.financialYear}."
    )
    if metric in ("scope1", "scope2") and totals.unresolvedEmissionLines:
        # Never let an incomplete emissions total present itself as complete.
        note += (
            f" ⚠ {totals.unresolvedEmissionLines} emission line(s) could not be resolved "
            "(missing emission factor or unconvertible unit) and are EXCLUDED from this total."
        )
    if metric == "scope3":
        note += " Scope 3 is entered manually per facility; no calculation engine is applied."

    return Resolved(
        value=value,
        unit=unit,
        recordRefs=[
            _ref("BRSR_ENV", "BrsrEnvironmentalMetric", sid, f"Site submission · {sid}")
            for sid in totals.siteIds[:200]
        ],
        recordCount=totals.siteCount,
        note=note,
    )


async def resolve_env_intensity(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """Intensity of a P6 figure per rupee of turnover. Omitted, never guessed."""
    metric = kw.get("metric", "scope1")
    totals = await brsr_env.load_totals(db, cycle.id)
    if totals.siteCount == 0 or not totals.turnoverInr:
        return None
    base = {
        "scope1": totals.scope1TCo2e,
        "scope2": totals.scope2TCo2e,
        "scope1_2": totals.scope1TCo2e + totals.scope2TCo2e,
        "energy": totals.energyTotalGj,
        "water": totals.waterConsumedKl,
    }.get(metric)
    if base is None:
        return None
    return Resolved(
        value=base / totals.turnoverInr,
        unit="per INR of turnover",
        recordRefs=[
            _ref("BRSR_ENV", "BrsrEnvironmentalMetric", sid, f"Site submission · {sid}")
            for sid in totals.siteIds[:200]
        ],
        recordCount=totals.siteCount,
        note=(
            f"{metric} ÷ turnover of ₹{totals.turnoverInr:,.0f}, summed across "
            f"{totals.siteCount} facility submissions."
        ),
    )


async def resolve_environmental_incidents(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """Environmental incidents in the period — the SECONDARY P6 source.

    Marked secondary in the seed: it does not populate an emissions or resource
    figure, it evidences the "any material environmental non-compliance" line.
    """
    start, end = _fy_bounds(cycle)
    rows = list(
        (
            await db.execute(
                select(Incident).where(
                    Incident.isDeleted.is_(False),
                    Incident.type == IncidentType.ENVIRONMENTAL,
                    Incident.occurredAt >= start,
                    Incident.occurredAt <= end,
                )
            )
        ).scalars()
    )
    if not rows:
        return None
    return Resolved(
        value=float(len(rows)),
        unit="count",
        recordRefs=[_ref("INCIDENT", "Incident", i.id, i.number) for i in rows[:200]],
        recordCount=len(rows),
        note=(
            f"Incidents classified ENVIRONMENTAL with an occurrence date between "
            f"{start:%d %b %Y} and {end:%d %b %Y}."
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  P9 — governance-adjacent value-chain items  (source: ERM vendor / ESG risk)
# ═══════════════════════════════════════════════════════════════════════════


async def resolve_vendor_assessment_coverage(
    db: AsyncSession, cycle: BrsrReportingCycle, **kw
) -> Resolved | None:
    """Percentage of active vendors carrying a current assessment of a lens."""
    lens = kw.get("lens", "ESG")
    vendors = list(
        (
            await db.execute(
                select(VendorProfile).where(
                    VendorProfile.isActive.is_(True), VendorProfile.isDeleted.is_(False)
                )
            )
        ).scalars()
    )
    if not vendors:
        return None
    assessments = list(
        (
            await db.execute(
                select(VendorAssessment).where(
                    VendorAssessment.vendorId.in_([v.id for v in vendors]),
                    VendorAssessment.lens == lens,
                    VendorAssessment.isCurrent.is_(True),
                    VendorAssessment.isDeleted.is_(False),
                )
            )
        ).scalars()
    )
    assessed_ids = {a.vendorId for a in assessments}
    pct = round(100.0 * len(assessed_ids) / len(vendors), 2)
    by_id = {v.id: v for v in vendors}
    return Resolved(
        value=pct,
        unit="%",
        recordRefs=[
            _ref("ERM", "VendorProfile", vid, f"{by_id[vid].vendorCode} — {by_id[vid].legalName}")
            for vid in list(assessed_ids)[:200]
            if vid in by_id
        ],
        recordCount=len(assessed_ids),
        note=(
            f"{len(assessed_ids)} of {len(vendors)} active vendors hold a current {lens} "
            "assessment in the ERM vendor register."
        ),
    )


# ── the registry ────────────────────────────────────────────────────────────
# `BrsrDataSource.resolverKey` indexes into this. A seeded mapping whose key is
# absent is reported as a configuration error rather than silently skipped —
# see `sweep`.
RESOLVERS: dict[str, Callable[..., Awaitable[Resolved | None]]] = {
    # P1
    "erm.control_count": resolve_control_count,
    "erm.control_test_coverage": resolve_control_test_coverage,
    # P3
    "manhours.safety_metric": resolve_safety_metric,
    "incident.count": resolve_incident_count,
    "near_miss.count": resolve_near_miss_count,
    "training.coverage": resolve_training_coverage,
    "facilities.workforce_composition": resolve_workforce_composition,
    # P5
    "epc.contractor_workforce": resolve_contractor_workforce,
    "facilities.social_compliance": resolve_social_compliance_flag,
    # P6
    "brsr_env.total": resolve_env_total,
    "brsr_env.intensity": resolve_env_intensity,
    "incident.environmental": resolve_environmental_incidents,
    # P9
    "erm.vendor_assessment_coverage": resolve_vendor_assessment_coverage,
}


# ── the sweep ───────────────────────────────────────────────────────────────


@dataclass
class SweepResult:
    populated: int = 0
    skippedManual: int = 0
    noData: int = 0
    failed: list[dict] = field(default_factory=list)
    misconfigured: list[str] = field(default_factory=list)


def _apply_value(
    row: BrsrIndicatorValue, indicator: BrsrIndicator, resolved: Resolved
) -> None:
    """Write a resolver's answer onto the value row, typed by the indicator."""
    row.valueNumber = None
    row.valueText = None
    row.valueBoolean = None
    row.valueJson = None

    if indicator.valueType == VALUE_TYPE_NUMBER:
        row.valueNumber = float(resolved.value)
    elif indicator.valueType == VALUE_TYPE_BOOLEAN:
        row.valueBoolean = bool(resolved.value)
    elif indicator.valueType == VALUE_TYPE_TABLE:
        row.valueJson = resolved.value
    else:
        row.valueText = str(resolved.value)

    row.unit = resolved.unit or indicator.unit


async def sweep(
    db: AsyncSession,
    cycle: BrsrReportingCycle,
    *,
    only_principle: str | None = None,
    actor_id: str | None = None,
) -> SweepResult:
    """Run every active mapping for a cycle and write the AUTO values.

    Caller commits. Safe to re-run at any point before the cycle is FILED:
    human-entered figures are left untouched (rule 1 in the module docstring).
    """
    result = SweepResult()

    sources = list(
        (
            await db.execute(
                select(BrsrDataSource).where(
                    BrsrDataSource.isActive.is_(True), BrsrDataSource.isPrimary.is_(True)
                )
            )
        ).scalars()
    )
    if not sources:
        return result

    indicators = {
        i.code: i
        for i in (
            await db.execute(
                select(BrsrIndicator).where(BrsrIndicator.isActive.is_(True))
            )
        ).scalars()
    }
    responses = {
        r.principle: r
        for r in (
            await db.execute(
                select(BrsrPrincipleResponse).where(BrsrPrincipleResponse.cycleId == cycle.id)
            )
        ).scalars()
    }
    existing = {
        v.indicatorCode: v
        for v in (
            await db.execute(
                select(BrsrIndicatorValue).where(BrsrIndicatorValue.cycleId == cycle.id)
            )
        ).scalars()
    }

    now = datetime.now(timezone.utc)

    for src in sources:
        indicator = indicators.get(src.indicatorCode)
        if indicator is None:
            result.misconfigured.append(
                f"{src.indicatorCode}: no active BrsrIndicator with this code"
            )
            continue
        if only_principle and indicator.principle != only_principle:
            continue

        resolver = RESOLVERS.get(src.resolverKey)
        if resolver is None:
            # Loud, not silent: a mapping row pointing at a resolver that does
            # not exist means an indicator everyone believes is sourced is not.
            result.misconfigured.append(
                f"{src.indicatorCode}: resolverKey '{src.resolverKey}' has no resolver"
            )
            continue

        row = existing.get(src.indicatorCode)
        # Rule 1 — never overwrite a human.
        if row is not None and row.provenance in (
            PROVENANCE_MANUAL,
            PROVENANCE_AUTO_OVERRIDDEN,
            PROVENANCE_NOT_APPLICABLE,
        ):
            result.skippedManual += 1
            continue

        # Rule 3, and the reason it needs a SAVEPOINT rather than a bare
        # try/except: when a resolver fails on a *database* error — a column the
        # ORM declares but the table does not have, say — Postgres aborts the
        # whole transaction. Catching the Python exception does not un-abort it,
        # so every subsequent statement in the sweep dies with "current
        # transaction is aborted" and the failure of one mapping silently
        # becomes the failure of all of them. `begin_nested` scopes the damage
        # to the one resolver and leaves the outer transaction usable.
        try:
            async with db.begin_nested():
                resolved = await resolver(db, cycle, **(src.resolverArgsJson or {}))
        except Exception as e:  # noqa: BLE001
            log.warning(
                "BRSR resolver %s failed for %s: %s", src.resolverKey, src.indicatorCode, e
            )
            result.failed.append(
                {"indicatorCode": src.indicatorCode, "resolverKey": src.resolverKey,
                 "error": str(e)}
            )
            continue

        # Rule 2 — nothing found writes nothing. An unanswered indicator reads
        # as "needs input"; a zero would read as a measured fact.
        if resolved is None or resolved.value is None:
            result.noData += 1
            continue

        if row is None:
            response = responses.get(indicator.principle) if indicator.principle else None
            row = BrsrIndicatorValue(
                cycleId=cycle.id,
                principleResponseId=response.id if response else None,
                indicatorCode=src.indicatorCode,
                createdBy=actor_id,
            )
            db.add(row)
            existing[src.indicatorCode] = row

        _apply_value(row, indicator, resolved)
        row.provenance = PROVENANCE_AUTO
        row.sourceModule = src.sourceModule
        row.resolverKey = src.resolverKey
        row.sourceRecordRefs = resolved.recordRefs
        row.sourceRecordCount = resolved.recordCount
        # The seeded note explains the mapping in general; the resolver's note
        # describes THIS run over THIS data. Prefer the specific one.
        row.derivationNote = resolved.note or src.derivationNote
        row.computedAt = now
        # A re-sweep changes the number, so a prior verification no longer
        # applies to what is on screen.
        row.isVerified = False
        row.verifiedById = None
        row.verifiedAt = None
        row.updatedBy = actor_id
        result.populated += 1

    return result


def principle_is_platform_sourced(principle: str) -> bool:
    return principle in PLATFORM_SOURCED_PRINCIPLES


__all__ = [
    "RESOLVERS",
    "Resolved",
    "SweepResult",
    "MANUAL_ONLY_PRINCIPLES",
    "PLATFORM_SOURCED_PRINCIPLES",
    "PRINCIPLES",
    "ANSWERED_PROVENANCES",
    "principle_is_platform_sourced",
    "sweep",
]
