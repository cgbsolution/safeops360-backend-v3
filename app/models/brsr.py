"""BRSR — Business Responsibility & Sustainability Reporting (SEBI).

A standalone disclosure module. It aggregates FROM other modules (ERM controls
and vendor risk, Manhours, Incident/NearMiss, Training & Competency, Safety
Culture, EPC contractors, Facilities workforce/social-compliance, CAMS) but owns
its own data model, workflow and report generator — the Daily Brief pattern, not
a sub-module of ERM.

House conventions followed: camelCase columns to match the Prisma-owned schema,
cross-module references as plain FK-by-value `String` columns (never a hard FK
into another module's table), and vocabularies as module constants on `String`
columns rather than Postgres enums so a new value is a seed change instead of a
type migration.

Four deliberate departures from the build spec's shape:

1. **Indicator values live in `BrsrIndicatorValue`, not inside
   `BrsrPrincipleResponse`.** The spec puts "essential + leadership indicator
   values" on the principle record. That works right up until the audit
   question, which is always about ONE number: where did this figure come from,
   who put it there, and which source records back it. A JSON blob per principle
   cannot carry per-figure provenance, cannot be indexed for "show me every
   unverified auto-populated figure", and cannot record that a human overrode
   the engine on indicator 11 while leaving 12 alone. `BrsrPrincipleResponse`
   survives as the spec intends — the workflow and rollup unit (status,
   completion %, narrative, sign-off) — with the figures hanging off it one row
   each.

2. **`BrsrIndicator` exists as a seeded catalogue.** Implied but not named by
   the spec. Completion % is meaningless without a denominator, and "mirror the
   actual BRSR indicator taxonomy, do not invent field names" is only
   enforceable if the taxonomy is data. Adding an indicator SEBI introduces next
   cycle is then a seed row, not a migration.

3. **`BrsrEnvironmentalMetric` is a header + `BrsrEnvMetricLine` children.**
   The spec describes one capture entity. But BRSR P6 asks for energy split by
   fuel type, water by source AND discharge by destination and treatment level,
   and waste across a category × disposal-method grid — a shape that is a line
   table, not columns. Flat columns would need a migration every time SEBI adds
   a fuel category.

4. **`FactoryEnvPeriod` is NOT extended — it becomes derived from this data.**
   Facilities already owns `FactoryEnvPeriod` (one flat row per site/period,
   read by the Facilities ESG rollup tab). Capturing environmental data twice
   would put two different Scope 1 numbers on two screens. See
   `services/brsr_env_rollup.py`: the BRSR lines are the source of truth and the
   Facilities row is recomputed from them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin

# ── vocabularies ────────────────────────────────────────────────────────────

# Cycle lifecycle. Ordered — the API only allows a forward step (or an explicit
# reopen back to DATA_COLLECTION before APPROVED).
CYCLE_DRAFT = "DRAFT"
CYCLE_DATA_COLLECTION = "DATA_COLLECTION"
CYCLE_REVIEW = "REVIEW"
CYCLE_APPROVED = "APPROVED"
CYCLE_FILED = "FILED"
CYCLE_STATUSES = (
    CYCLE_DRAFT,
    CYCLE_DATA_COLLECTION,
    CYCLE_REVIEW,
    CYCLE_APPROVED,
    CYCLE_FILED,
)
# Past this point the cycle is immutable — the CAMS audit-report pattern. Every
# write path checks this rather than each status individually, so adding a
# post-filing status later cannot accidentally reopen a filed disclosure.
CYCLE_LOCKED_STATUSES = (CYCLE_FILED,)

# The three BRSR sections. Section C is the only one partitioned by principle.
SECTION_A = "A"  # Entity details
SECTION_B = "B"  # Management & process disclosures
SECTION_C = "C"  # Principle-wise performance
SECTIONS = (SECTION_A, SECTION_B, SECTION_C)

# Section C indicator classes.
INDICATOR_ESSENTIAL = "ESSENTIAL"
INDICATOR_LEADERSHIP = "LEADERSHIP"
INDICATOR_CLASSES = (INDICATOR_ESSENTIAL, INDICATOR_LEADERSHIP)

# The nine principles, as SEBI numbers them.
PRINCIPLES = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")

# Which principles the platform can source from existing module data, and which
# are manual-entry only. Drives the "not sourced from platform data" banner —
# derived from this constant rather than from "did the mapping engine find
# anything", so an empty result reads as "no data yet" instead of silently
# looking like an unsourceable principle.
PLATFORM_SOURCED_PRINCIPLES = frozenset({"P1", "P3", "P5", "P6", "P9"})
MANUAL_ONLY_PRINCIPLES = frozenset({"P2", "P4", "P7", "P8"})

# How a figure got its value. The distinction that matters for assurance is
# AUTO vs AUTO_OVERRIDDEN: both have a platform-computed number behind them, but
# the second one a human disagreed with, and that disagreement is the thing an
# assurance provider samples.
PROVENANCE_AUTO = "AUTO"
PROVENANCE_MANUAL = "MANUAL"
PROVENANCE_AUTO_OVERRIDDEN = "AUTO_OVERRIDDEN"
PROVENANCE_NOT_APPLICABLE = "NOT_APPLICABLE"
PROVENANCES = (
    PROVENANCE_AUTO,
    PROVENANCE_MANUAL,
    PROVENANCE_AUTO_OVERRIDDEN,
    PROVENANCE_NOT_APPLICABLE,
)
# Counted as "answered" when computing completion %. NOT_APPLICABLE counts —
# an explicit N/A with a reason is a complete disclosure, an empty cell is not.
ANSWERED_PROVENANCES = frozenset(PROVENANCES)

# Indicator value shapes. `TABLE` covers every current-year/previous-year grid
# BRSR is largely made of; the payload lives in `valueJson`.
VALUE_TYPE_NUMBER = "NUMBER"
VALUE_TYPE_TEXT = "TEXT"
VALUE_TYPE_BOOLEAN = "BOOLEAN"
VALUE_TYPE_TABLE = "TABLE"
VALUE_TYPES = (VALUE_TYPE_NUMBER, VALUE_TYPE_TEXT, VALUE_TYPE_BOOLEAN, VALUE_TYPE_TABLE)

# Sentinel for the parts of a BrsrEnvMetricLine's unique slot that do not apply
# to its stream (energy and emissions lines have no flow type or destination).
# An explicit token rather than NULL, because the slot columns participate in a
# unique constraint and Postgres does not consider two NULLs equal.
SLOT_NA = "NA"

# Environmental capture streams.
STREAM_ENERGY = "ENERGY"
STREAM_WATER = "WATER"
STREAM_EMISSIONS = "EMISSIONS"
STREAM_WASTE = "WASTE"
STREAMS = (STREAM_ENERGY, STREAM_WATER, STREAM_EMISSIONS, STREAM_WASTE)

# GHG scope. Scope 3 is a captured field only — no calculation engine, per the
# build spec's explicit out-of-scope list.
SCOPE_1 = "SCOPE_1"
SCOPE_2 = "SCOPE_2"
SCOPE_3 = "SCOPE_3"
SCOPES = (SCOPE_1, SCOPE_2, SCOPE_3)

# Environmental-metric submission lifecycle, per site per period.
ENV_DRAFT = "DRAFT"
ENV_SUBMITTED = "SUBMITTED"
ENV_VERIFIED = "VERIFIED"
ENV_STATUSES = (ENV_DRAFT, ENV_SUBMITTED, ENV_VERIFIED)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )


# ── BrsrReportingCycle — one financial year's disclosure ─────────────────────
class BrsrReportingCycle(Base, IdMixin):
    """One BRSR filing cycle: one financial year, one listed entity.

    No `plantId`. BRSR is filed by the listed entity, not the site — a per-plant
    cycle would model a filing that does not exist. Sites enter the model one
    level down, at `BrsrEnvironmentalMetric`, which is genuinely per-facility.

    `snapshotJson` is the immutability mechanism. On transition to FILED the
    generator freezes the fully assembled report into this column, so the filed
    disclosure keeps reading identically even after a source record is corrected
    or an emission factor is revised. Same pattern as the CAMS audit report.
    """

    __tablename__ = "BrsrReportingCycle"

    # "FY2025-26" — the label SEBI uses, stored as typed by the user so the
    # report header never has to reconstruct it from dates.
    financialYear: Mapped[str] = mapped_column(String, nullable=False, index=True)
    periodStart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    periodEnd: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(
        String, nullable=False, default=CYCLE_DRAFT, index=True
    )

    # ── Section A entity identity (asked once per cycle, not per principle) ──
    entityName: Mapped[str | None] = mapped_column(String)
    cin: Mapped[str | None] = mapped_column(String)
    # BSE/NSE scrip codes etc. — a list, because dual-listed entities exist.
    stockExchangeCodes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    registeredOfficeAddress: Mapped[str | None] = mapped_column(Text)
    # The person SEBI requires the entity to name as the BRSR contact.
    contactName: Mapped[str | None] = mapped_column(String)
    contactEmail: Mapped[str | None] = mapped_column(String)
    contactPhone: Mapped[str | None] = mapped_column(String)
    # "Yes"/"No" + provider — captured for the disclosure. The assurance
    # WORKFLOW itself is explicitly out of scope; this is just the declaration.
    assuranceProvider: Mapped[str | None] = mapped_column(String)
    assuranceType: Mapped[str | None] = mapped_column(String)

    # ── rollup, recomputed by services.brsr_completion ──
    completionPct: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    autoPopulatedPct: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    lastComputedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── sign-off + immutable snapshot ──
    approvedById: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    filedById: Mapped[str | None] = mapped_column(String)
    filedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Free text — the entity files through its own SEBI process; SafeOps360 does
    # not submit. This records what they filed and when, for the audit trail.
    filingReference: Mapped[str | None] = mapped_column(String)
    snapshotJson: Mapped[dict | None] = mapped_column(JSON)
    # SHA-256 over the canonicalised snapshot. Lets the report screen prove the
    # PDF being downloaded is the one that was approved.
    snapshotHash: Mapped[str | None] = mapped_column(String)

    notes: Mapped[str | None] = mapped_column(Text)

    principleResponses: Mapped[list["BrsrPrincipleResponse"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    __table_args__ = (
        # One cycle per financial year. Partial so a soft-deleted cycle does not
        # block re-creating the year — the same shape SeverityMatrixRule uses.
        Index(
            "ux_BrsrReportingCycle_fy",
            "financialYear",
            unique=True,
            postgresql_where=text('"isDeleted" = false'),
        ),
    )


# ── BrsrIndicator — the seeded SEBI indicator catalogue ──────────────────────
class BrsrIndicator(Base, IdMixin):
    """One disclosure line item in the SEBI BRSR format.

    Seeded, not user-created. This is the taxonomy the whole module is measured
    against: completion % is `answered indicators / applicable indicators`, and
    the manual-entry forms for P2/P4/P7/P8 are rendered from these rows rather
    than hand-built, which is what keeps the field names SEBI's and not ours.

    `code` mirrors SEBI's own numbering as closely as the format allows —
    "C.P6.EI.1" is Section C, Principle 6, Essential Indicator 1. The report
    generator orders by `displayOrder`, never by code, because SEBI's numbering
    is not lexicographically sortable past 9.
    """

    __tablename__ = "BrsrIndicator"

    code: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    section: Mapped[str] = mapped_column(String, nullable=False, index=True)  # A | B | C
    # NULL for Sections A and B — only Section C is principle-partitioned.
    principle: Mapped[str | None] = mapped_column(String, index=True)
    # NULL outside Section C.
    indicatorClass: Mapped[str | None] = mapped_column(String, index=True)

    # SEBI's wording, verbatim. Long — this is the question as it appears on the
    # filing, and paraphrasing it is how a disclosure stops matching the format.
    label: Mapped[str] = mapped_column(Text, nullable=False)
    # Sub-heading this indicator sits under, e.g. "Water withdrawal by source".
    groupLabel: Mapped[str | None] = mapped_column(String)
    guidance: Mapped[str | None] = mapped_column(Text)

    valueType: Mapped[str] = mapped_column(String, nullable=False, default=VALUE_TYPE_TEXT)
    unit: Mapped[str | None] = mapped_column(String)
    # For valueType=TABLE: the column definitions the form and the report render.
    # [{"key": "currentFy", "label": "FY 2025-26", "type": "NUMBER"}, ...]
    tableSchemaJson: Mapped[list | None] = mapped_column(JSON)

    # Leadership indicators are voluntary; a blank one must not drag completion %
    # down. Essential indicators are required and do count.
    isMandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    displayOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    # Which SEBI circular this wording came from. When the format is revised, a
    # reader can tell which indicators were restated.
    sebiFormatVersion: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_BrsrIndicator_section_principle", "section", "principle", "displayOrder"),
    )


# ── BrsrDataSource — indicator → platform field mapping ──────────────────────
class BrsrDataSource(Base, IdMixin):
    """Which SafeOps360 module and field feeds which BRSR indicator.

    The traceability record the spec asks for, and also the mapping engine's
    configuration: `resolverKey` names a function in
    `services/brsr_mapping.py`, so adding a source for an indicator is a seed
    row plus a resolver, never a change to the engine's control flow.

    Seeded and admin-visible but not admin-editable — a mapping whose
    `resolverKey` has no matching resolver would silently stop populating, and a
    disclosure that quietly stops being sourced is worse than one that never was.
    """

    __tablename__ = "BrsrDataSource"

    indicatorCode: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # "ERM" | "MANHOURS" | "INCIDENT" | "TRAINING" | "EPC" | "FACILITIES" |
    # "CAMS" | "SAFETY_CULTURE" | "BRSR_ENV" — matches the licensing module code
    # where one exists, so the UI can grey out a source whose module is not
    # entitled instead of reporting it as missing data.
    sourceModule: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # The table the figure is computed over, for the "sourced from" caption.
    sourceEntity: Mapped[str] = mapped_column(String, nullable=False)
    sourceField: Mapped[str | None] = mapped_column(String)

    # Function name in services/brsr_mapping.RESOLVERS.
    resolverKey: Mapped[str] = mapped_column(String, nullable=False)
    # Static kwargs handed to the resolver — lets one resolver serve several
    # indicators (e.g. a waste-category total parameterised by category code).
    resolverArgsJson: Mapped[dict | None] = mapped_column(JSON)

    # Operator-facing sentence describing how the number is derived. Rendered
    # verbatim under the figure, so an auditor reads the derivation without
    # opening the code.
    derivationNote: Mapped[str | None] = mapped_column(Text)
    # PRIMARY sources populate the figure; SECONDARY ones only contribute
    # drill-through records (e.g. environmental incident counts alongside the
    # captured P6 environmental figures).
    isPrimary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("indicatorCode", "resolverKey", name="uq_BrsrDataSource_ind_resolver"),
        Index("ix_BrsrDataSource_module", "sourceModule", "isActive"),
    )


# ── BrsrPrincipleResponse — the workflow + rollup unit ───────────────────────
class BrsrPrincipleResponse(Base, IdMixin):
    """One principle (P1–P9) within one cycle.

    Owns the narrative, the ownership and the sign-off; the figures live in
    `BrsrIndicatorValue` (see the module docstring). Completion is stored rather
    than computed on read because the cycle dashboard renders all nine at once
    and the mapping engine already has to walk every indicator to refresh them.
    """

    __tablename__ = "BrsrPrincipleResponse"

    cycleId: Mapped[str] = mapped_column(
        ForeignKey("BrsrReportingCycle.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cycle: Mapped[BrsrReportingCycle] = relationship(back_populates="principleResponses")

    principle: Mapped[str] = mapped_column(String, nullable=False, index=True)  # P1..P9

    # NOT_STARTED | IN_PROGRESS | READY_FOR_REVIEW | REVIEWED — the per-principle
    # workflow inside the cycle's own DATA_COLLECTION / REVIEW stages.
    status: Mapped[str] = mapped_column(String, nullable=False, default="NOT_STARTED", index=True)

    # Denormalised from PLATFORM_SOURCED_PRINCIPLES at row creation so the UI
    # banner survives a future change to the constant without restating history.
    isPlatformSourced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    ownerUserId: Mapped[str | None] = mapped_column(String, index=True)
    dueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── rollup ──
    totalIndicators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    answeredIndicators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    autoPopulatedIndicators: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completionPct: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    autoPopulatedPct: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    lastComputedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    narrative: Mapped[str | None] = mapped_column(Text)

    reviewedById: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewNotes: Mapped[str | None] = mapped_column(Text)

    values: Mapped[list["BrsrIndicatorValue"]] = relationship(
        back_populates="principleResponse", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("cycleId", "principle", name="uq_BrsrPrincipleResponse_cycle_principle"),
    )


# ── BrsrIndicatorValue — one answered figure, with its provenance ────────────
class BrsrIndicatorValue(Base, IdMixin):
    """One indicator's answer for one cycle, plus where it came from.

    This is the audit-grade unit. Everything an assurance provider asks about a
    single figure is on this row: the value, whether a person or the platform
    produced it, which resolver ran, which source records it walked, what the
    platform said before a human changed it, and why they changed it.

    `sourceRecordRefs` stores `[{"module": "...", "entity": "...", "id": "...",
    "label": "..."}]` rather than bare ids, because the drill-through has to
    render a row for a record the user may not be entitled to open — and a
    dangling cuid with no label is exactly the "never render a raw id" failure
    the platform already has a rule about.

    Deliberately NOT foreign-keyed to those records: they span nine modules and
    several are soft-deleted rather than removed. A filed disclosure must keep
    rendering its provenance even after a source record is retired.
    """

    __tablename__ = "BrsrIndicatorValue"

    cycleId: Mapped[str] = mapped_column(
        ForeignKey("BrsrReportingCycle.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL for Section A / B indicators, which belong to no principle.
    principleResponseId: Mapped[str | None] = mapped_column(
        ForeignKey("BrsrPrincipleResponse.id", ondelete="CASCADE"), index=True
    )
    principleResponse: Mapped["BrsrPrincipleResponse | None"] = relationship(
        back_populates="values"
    )

    indicatorCode: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # One of these is populated according to the indicator's valueType. Kept as
    # separate typed columns rather than one text blob so a numeric indicator can
    # be aggregated for the year-over-year trend without a cast.
    valueNumber: Mapped[float | None] = mapped_column(Float)
    valueText: Mapped[str | None] = mapped_column(Text)
    valueBoolean: Mapped[bool | None] = mapped_column(Boolean)
    valueJson: Mapped[dict | list | None] = mapped_column(JSON)
    unit: Mapped[str | None] = mapped_column(String)

    provenance: Mapped[str] = mapped_column(
        String, nullable=False, default=PROVENANCE_MANUAL, index=True
    )
    # Required by the API when provenance is NOT_APPLICABLE — an unexplained N/A
    # on a mandatory SEBI indicator is a finding waiting to happen.
    notApplicableReason: Mapped[str | None] = mapped_column(Text)

    # ── auto-population trace (null on a purely manual figure) ──
    sourceModule: Mapped[str | None] = mapped_column(String, index=True)
    resolverKey: Mapped[str | None] = mapped_column(String)
    sourceRecordRefs: Mapped[list | None] = mapped_column(JSON)
    sourceRecordCount: Mapped[int | None] = mapped_column(Integer)
    derivationNote: Mapped[str | None] = mapped_column(Text)
    computedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── override trace (populated only when provenance = AUTO_OVERRIDDEN) ──
    # The platform's number, frozen at the moment it was overridden. Stored
    # rather than recomputed, because re-running the resolver later answers a
    # different question than "what did the reviewer actually disagree with".
    autoValueNumber: Mapped[float | None] = mapped_column(Float)
    autoValueText: Mapped[str | None] = mapped_column(Text)
    overrideReason: Mapped[str | None] = mapped_column(Text)
    overriddenById: Mapped[str | None] = mapped_column(String)
    overriddenAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # An auto-populated figure a human has explicitly looked at. The review
    # screen's whole job is driving this to true for every mandatory indicator.
    isVerified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    verifiedById: Mapped[str | None] = mapped_column(String)
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    evidenceNote: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("cycleId", "indicatorCode", name="uq_BrsrIndicatorValue_cycle_indicator"),
        Index("ix_BrsrIndicatorValue_cycle_prov", "cycleId", "provenance"),
        Index("ix_BrsrIndicatorValue_verify", "cycleId", "isVerified"),
    )


# ── BrsrEmissionFactor — the seeded factor lookup ────────────────────────────
class BrsrEmissionFactor(Base, IdMixin):
    """Emission factor for one fuel or grid, with its published source.

    Versioned by validity window rather than overwritten. A factor revision must
    never restate a prior year's filed Scope 1 — and because
    `BrsrEnvMetricLine` also freezes the factor it used onto the line, a revision
    cannot restate an unfiled draft either without someone re-running the
    calculation deliberately.

    `source` and `sourceYear` are not decoration: a Scope 2 figure whose factor
    has no citation is not an assurable disclosure.
    """

    __tablename__ = "BrsrEmissionFactor"

    code: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # ELECTRICITY_GRID | LIQUID_FUEL | GASEOUS_FUEL | SOLID_FUEL | REFRIGERANT
    factorType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String, nullable=False, index=True)  # SCOPE_1 | SCOPE_2

    # tCO2e per `perUnit` of fuel/electricity.
    factorValue: Mapped[float] = mapped_column(Float, nullable=False)
    perUnit: Mapped[str] = mapped_column(String, nullable=False)  # kWh | litre | kg | scm | tonne

    # "CEA CO2 Baseline Database v20", "IPCC AR6", "MoEFCC" — the citation.
    source: Mapped[str] = mapped_column(String, nullable=False)
    sourceYear: Mapped[str | None] = mapped_column(String)
    # NULL = the all-India grid factor. Set for a regional grid factor.
    region: Mapped[str | None] = mapped_column(String)

    validFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = _created()
    updatedAt: Mapped[datetime] = _updated()

    __table_args__ = (
        Index("ix_BrsrEmissionFactor_lookup", "factorType", "scope", "isActive"),
    )


# ── BrsrEnvironmentalMetric — per-facility, per-period capture header ─────────
class BrsrEnvironmentalMetric(Base, IdMixin):
    """One facility's environmental submission for one period of one cycle.

    The header carries the workflow (who submitted, who verified) and the
    denominators the intensity ratios need; the quantities are child lines.

    `turnoverInr` and `productionVolume` live here rather than on the cycle
    because BRSR asks for intensity per rupee of turnover AND allows a
    physical-output intensity, and both denominators are per-facility when the
    entity is multi-site. A site with no denominator still reports absolute
    figures — intensity is simply omitted rather than divided by a guess.
    """

    __tablename__ = "BrsrEnvironmentalMetric"

    cycleId: Mapped[str] = mapped_column(
        ForeignKey("BrsrReportingCycle.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # FK-by-value to Plant, per house convention for cross-module references.
    siteId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Denormalised so a register row never has to render a raw Plant cuid, and
    # a filed snapshot keeps its site names after a plant is renamed.
    siteName: Mapped[str | None] = mapped_column(String)

    # "FY2025-26" for an annual submission, "2025-Q3" for quarterly. Quarterly
    # rows roll up into the cycle; the report generator sums whatever exists.
    periodLabel: Mapped[str] = mapped_column(String, nullable=False, index=True)
    periodStart: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    periodEnd: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, nullable=False, default=ENV_DRAFT, index=True)

    # ── intensity denominators ──
    turnoverInr: Mapped[float | None] = mapped_column(Float)
    productionVolume: Mapped[float | None] = mapped_column(Float)
    productionUnit: Mapped[str | None] = mapped_column(String)

    # ── Scope 3, captured not computed (explicitly out of scope to calculate) ──
    scope3TCo2e: Mapped[float | None] = mapped_column(Float)
    scope3Methodology: Mapped[str | None] = mapped_column(Text)

    # ── flags BRSR P6 asks for directly ──
    isWaterPositive: Mapped[bool | None] = mapped_column(Boolean)
    hasZeroLiquidDischarge: Mapped[bool | None] = mapped_column(Boolean)
    # Free text mirror of the SPCB consent standing, as Facilities already models it.
    consentStatus: Mapped[str | None] = mapped_column(String)

    submittedById: Mapped[str | None] = mapped_column(String)
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verifiedById: Mapped[str | None] = mapped_column(String)
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["BrsrEnvMetricLine"]] = relationship(
        back_populates="metric", cascade="all, delete-orphan"
    )

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "cycleId", "siteId", "periodLabel", name="uq_BrsrEnvironmentalMetric_cycle_site_period"
        ),
        Index("ix_BrsrEnvironmentalMetric_cycle_status", "cycleId", "status"),
    )


# ── BrsrEnvMetricLine — one quantity ─────────────────────────────────────────
class BrsrEnvMetricLine(Base, IdMixin):
    """One captured environmental quantity: a stream, a category, a number.

    A line table rather than columns because BRSR's environmental section is a
    set of grids whose categories SEBI revises — energy by fuel type, water by
    source and by discharge destination, waste by category and by disposal
    method. Adding a category SEBI introduces is a seed row here; as columns it
    would be a migration on a table that is already filed against.

    `computedTCo2e` is written by the service, never by the client. The factor
    that produced it is frozen onto the line (`factorValue` / `factorSource`),
    so revising `BrsrEmissionFactor` next year cannot silently restate a number
    that has already been reviewed.
    """

    __tablename__ = "BrsrEnvMetricLine"

    metricId: Mapped[str] = mapped_column(
        ForeignKey("BrsrEnvironmentalMetric.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric: Mapped[BrsrEnvironmentalMetric] = relationship(back_populates="lines")

    stream: Mapped[str] = mapped_column(String, nullable=False, index=True)  # see STREAMS
    # Seeded token, e.g. ELECTRICITY_NON_RENEWABLE / GROUNDWATER / PLASTIC_WASTE.
    categoryCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Frozen label so a filed snapshot survives a taxonomy rename.
    categoryLabel: Mapped[str | None] = mapped_column(String)

    # WATER only: WITHDRAWAL | DISCHARGE | CONSUMPTION | RECYCLED.
    # WASTE only:  GENERATED | RECYCLED | REUSED | RECOVERED | INCINERATED |
    #              LANDFILLED | OTHER_DISPOSAL.
    #
    # NOT NULL with the explicit sentinel SLOT_NA for ENERGY and EMISSIONS,
    # where the category alone is the full key. These two columns are part of
    # the row's unique slot, and Postgres treats NULLs as distinct — nullable
    # here would let the same energy category be inserted unlimited times while
    # the constraint silently passed. Same reasoning for `destination`.
    flowType: Mapped[str] = mapped_column(String, nullable=False, default=SLOT_NA, index=True)
    # WATER DISCHARGE only — "to surface water", "to groundwater", "to sea".
    destination: Mapped[str] = mapped_column(String, nullable=False, default=SLOT_NA)
    treatmentLevel: Mapped[str | None] = mapped_column(String)

    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String)

    # ── emissions derivation (frozen; see the class docstring) ──
    scope: Mapped[str | None] = mapped_column(String, index=True)
    emissionFactorId: Mapped[str | None] = mapped_column(String)
    factorValue: Mapped[float | None] = mapped_column(Float)
    factorPerUnit: Mapped[str | None] = mapped_column(String)
    factorSource: Mapped[str | None] = mapped_column(String)
    computedTCo2e: Mapped[float | None] = mapped_column(Float)

    # Where the number came from: METER | INVOICE | ESTIMATE | THIRD_PARTY.
    # BRSR assurance asks this; an unlabelled figure defaults to nothing rather
    # than to METER, so an estimate is never mistaken for a reading.
    dataQuality: Mapped[str | None] = mapped_column(String)
    evidenceNote: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = _created()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _updated()
    updatedBy: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint(
            "metricId", "stream", "categoryCode", "flowType", "destination",
            name="uq_BrsrEnvMetricLine_slot",
        ),
        Index("ix_BrsrEnvMetricLine_metric_stream", "metricId", "stream"),
    )


__all__ = [
    "BrsrReportingCycle",
    "BrsrIndicator",
    "BrsrDataSource",
    "BrsrPrincipleResponse",
    "BrsrIndicatorValue",
    "BrsrEmissionFactor",
    "BrsrEnvironmentalMetric",
    "BrsrEnvMetricLine",
    "CYCLE_DRAFT",
    "CYCLE_DATA_COLLECTION",
    "CYCLE_REVIEW",
    "CYCLE_APPROVED",
    "CYCLE_FILED",
    "CYCLE_STATUSES",
    "CYCLE_LOCKED_STATUSES",
    "SECTION_A",
    "SECTION_B",
    "SECTION_C",
    "SECTIONS",
    "INDICATOR_ESSENTIAL",
    "INDICATOR_LEADERSHIP",
    "INDICATOR_CLASSES",
    "PRINCIPLES",
    "PLATFORM_SOURCED_PRINCIPLES",
    "MANUAL_ONLY_PRINCIPLES",
    "PROVENANCE_AUTO",
    "PROVENANCE_MANUAL",
    "PROVENANCE_AUTO_OVERRIDDEN",
    "PROVENANCE_NOT_APPLICABLE",
    "PROVENANCES",
    "ANSWERED_PROVENANCES",
    "VALUE_TYPE_NUMBER",
    "VALUE_TYPE_TEXT",
    "VALUE_TYPE_BOOLEAN",
    "VALUE_TYPE_TABLE",
    "VALUE_TYPES",
    "SLOT_NA",
    "STREAM_ENERGY",
    "STREAM_WATER",
    "STREAM_EMISSIONS",
    "STREAM_WASTE",
    "STREAMS",
    "SCOPE_1",
    "SCOPE_2",
    "SCOPE_3",
    "SCOPES",
    "ENV_DRAFT",
    "ENV_SUBMITTED",
    "ENV_VERIFIED",
    "ENV_STATUSES",
]
