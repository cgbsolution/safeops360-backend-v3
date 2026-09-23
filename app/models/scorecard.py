"""EHS Scorecard — the monthly rollup store.

Backend-only tables (like Signal / InsightSnapshot / Attachment): reached solely
through FastAPI, never Prisma, so they are NOT in schema.prisma — `prisma db push`
would drop them. Created by `scripts/create_scorecard_tables.py` (idempotent).
camelCase columns match the house convention.

**Why a stored rollup at all.** Every other analytics surface on this platform
computes from raw records on each load, which is right for a screen that answers
"what is true now". A scorecard answers "what was true in March", and that is a
different obligation: the number presented to a client in a quarterly review must
still read the same when someone opens the deck in September. Recomputing from
live records cannot promise that — a soft-delete, a back-dated correction or a
re-classification silently rewrites history. So each plant-month is computed once
and frozen, and the row records the inputs it was computed from.

**One row per (tenant, site, year, month).** Quarterly is derived by aggregating
three monthly rows, never stored separately — a second stored grain is a second
thing to keep in sync, and the classic failure of quarterly reporting is a
quarter that does not equal the sum of its months.

**Rates are stored with their numerator AND denominator.** A frequency rate with
no exposure figure beside it cannot be re-aggregated (you cannot average three
monthly LTIFRs to get a quarterly one) and cannot be audited. Both are kept.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin

DEFAULT_TENANT = "default"

# The indicator set, in the order a scorecard reads: leading first (what we are
# doing about safety), then lagging (what happened anyway). Declared here so the
# rollup, the API, the dashboard and both exporters cannot drift on naming.
LEADING_KEYS = (
    "observationsLogged",
    "nearMissReported",
    "ptwCompliancePct",
    "trainingCompletionPct",
    "inductionsConducted",
    "leadershipWalksCompletedPct",
    "cultureStageScore",
)
LAGGING_KEYS = (
    "incidentsTotal",
    "ltiCount",
    "recordableCount",
    "firstAidCount",
    "fatalityCount",
    "highSeverityCount",
    "ltifr",
    "trir",
    "severityRate",
)


class ScorecardPeriod(Base, IdMixin):
    """One plant-month of computed EHS indicators."""

    __tablename__ = "ScorecardPeriod"

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default=DEFAULT_TENANT, index=True)
    siteId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    # Denormalised "2026-03" — every query and every export groups or sorts on
    # it, and reconstructing it from year/month in SQL on every read is both
    # slower and one more place to get the zero-padding wrong.
    period: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # ── Exposure (the denominator every rate depends on) ────────────────────
    employeeHours: Mapped[float | None] = mapped_column(Float)
    contractorHours: Mapped[float | None] = mapped_column(Float)
    totalHours: Mapped[float | None] = mapped_column(Float)
    headcount: Mapped[int | None] = mapped_column(Integer)

    # ── Leading indicators ──────────────────────────────────────────────────
    observationsLogged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nearMissReported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ptwIssued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ptwClosedProperly: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ptwCompliancePct: Mapped[float | None] = mapped_column(Float)
    trainingAssigned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trainingCompleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trainingCompletionPct: Mapped[float | None] = mapped_column(Float)
    inductionsConducted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leadershipWalksPlanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leadershipWalksCompleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leadershipWalksCompletedPct: Mapped[float | None] = mapped_column(Float)
    cultureStageScore: Mapped[float | None] = mapped_column(Float)
    perceptionScore: Mapped[float | None] = mapped_column(Float)

    # ── Lagging indicators ──────────────────────────────────────────────────
    incidentsTotal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ltiCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recordableCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    firstAidCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    highSeverityCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Severity rate charges 6,000 days per fatality (IS 3786:1983), so a quarter
    # cannot be re-derived from its months without knowing how many fatalities
    # each month carried. Without this column the monthly severity rate would
    # include the charge and the quarterly one would silently drop it.
    fatalityCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lostDays: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # NULL where no exposure was reported for the month. Never 0 — a frequency
    # rate of zero and an unmeasurable one are opposite facts, and this platform
    # has already shipped that bug once on Incident's Overdue tile.
    ltifr: Mapped[float | None] = mapped_column(Float)
    trir: Mapped[float | None] = mapped_column(Float)
    severityRate: Mapped[float | None] = mapped_column(Float)

    # ── Provenance ──────────────────────────────────────────────────────────
    # Which indicators could NOT be computed for this month, and why. Carried on
    # the row rather than derived at render time so the export and the dashboard
    # cannot disagree about what was measurable.
    gaps: Mapped[list | None] = mapped_column(JSON)
    # Where each rate came from — the Manhours module's own figure, or derived.
    # LTIFR exists in two places on this platform; recording which one was used
    # is the difference between a defensible number and a coincidence.
    sources: Mapped[dict | None] = mapped_column(JSON)
    computedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenantId", "siteId", "year", "month", name="uq_scorecard_site_period"),
        Index("ix_scorecard_period_site", "tenantId", "period", "siteId"),
    )


__all__ = ["DEFAULT_TENANT", "LAGGING_KEYS", "LEADING_KEYS", "ScorecardPeriod"]
