"""Signal Engine — cross-module correlation store (Signal Engine spec §2.1).

Five backend-only tables (like InsightSnapshot / Attachment): reached solely
through FastAPI, never Prisma, so they are NOT in schema.prisma. Created by the
hand-DDL applier `scripts/create_signal_engine_tables.py` (idempotent). camelCase
columns match the house Prisma convention.

Three deliberate deviations from the spec's Prisma block, each recorded in
SIGNAL_ENGINE_DECISIONS.md:

1. **TEXT columns, not Postgres enums.** The spec declares `SignalSeverity` /
   `SignalStatus` / `SignalCategory` as PG enums. This codebase has already been
   bitten by that: a value that is not a member aborts the whole transaction at
   flush (see the agent-platform enum poisoning fix), and every rule here writes
   a value that originates in Python code. TEXT + the module-level tuples below
   gives the same vocabulary with a survivable failure mode; the API layer
   validates membership on the way in.

2. **`ruleCode` is denormalised onto Signal** alongside `ruleId`. The runner
   dedupes and expires by rule without joining, and a signal stays readable in a
   raw SQL dump — which is how these get debugged in prod.

3. **Identity excludes `windowStart`.** The spec keys signals on
   `ruleId + siteId + areaId + windowStart`. With a rolling window that key
   changes every night, so an unacknowledged signal would re-appear as new on
   every run — precisely the alert fatigue NFR §7 forbids. Identity here is
   `(tenantId, ruleCode, signalKey)`: ONE live signal per real-world finding,
   whose window/evidence/confidence are refreshed in place while `status` and
   `acknowledgedAt` survive. `windowStart`/`windowEnd` remain as attributes.

Tenancy: this platform is single-tenant per deployment (see the Weekly Insight
Engine's DEFAULT_TENANT). `tenantId` is carried on every row anyway so the
isolation guarantee is structural rather than retrofitted, and defaults to
"default". Real scoping is `siteId` (a Plant id).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin

# ── Controlled vocabularies (TEXT columns, validated in Python) ──────────────
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
STATUSES = ("OPEN", "ACKNOWLEDGED", "ACTIONED", "DISMISSED", "EXPIRED")
CATEGORIES = (
    "LEADING_INDICATOR",
    "LAGGING_PATTERN",
    "COMPLIANCE_GAP",
    "OPERATIONAL_RISK",
    "DATA_QUALITY",
)
# Statuses a live finding can hold. A rule that stops emitting an identity
# expires only these; DISMISSED is a human decision and is never overwritten.
LIVE_STATUSES = ("OPEN", "ACKNOWLEDGED", "ACTIONED")

DEFAULT_TENANT = "default"


class Signal(Base, IdMixin):
    """One cross-module finding. Not owned by any module — its evidence spans
    several, which is the whole reason this is a first-class entity rather than
    a column on Incident or PTW (spec §2.2)."""

    __tablename__ = "Signal"
    __table_args__ = (
        UniqueConstraint("tenantId", "ruleCode", "signalKey", name="ux_Signal_identity"),
        Index("ix_Signal_feed", "tenantId", "siteId", "status", "severity"),
        Index("ix_Signal_rule_window", "tenantId", "ruleCode", "windowStart"),
        Index("ix_Signal_category_status", "tenantId", "category", "status"),
    )

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default=DEFAULT_TENANT, index=True)
    siteId: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String)

    ruleId: Mapped[str] = mapped_column(ForeignKey("SignalRule.id"), nullable=False, index=True)
    # Denormalised for dedupe/expiry without a join (deviation 2 above).
    ruleCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Rule-supplied identity discriminator within a rule, e.g.
    # "HiraEntry.requiresPermit" or "<siteId>::<areaId>::UNSAFE_CONDITION".
    signalKey: Mapped[str] = mapped_column(String, nullable=False)

    severity: Mapped[str] = mapped_column(String, nullable=False, default="INFO")
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    category: Mapped[str] = mapped_column(String, nullable=False)

    # "Signal strength", NOT a machine-learned probability (spec §4.4). A
    # deterministic weighted sum of rule-specific factors, normalised 0-1.
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Always populated. This is the shipped narrative for an airgapped tenant,
    # so it must stand alone as a complete, audit-ready sentence (spec §4.3).
    narrativeTemplate: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional LLM rewrite. Never authoritative, never the default view (§4.5).
    narrativeLLM: Mapped[str | None] = mapped_column(Text)
    recommendedAction: Mapped[str] = mapped_column(Text, nullable=False)

    windowStart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    windowEnd: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The threshold values IN EFFECT at compute time — snapshotted, not read
    # from current config at display time. Hard explainability requirement
    # (NFR §7): a signal must be defensible against the config it ran under.
    thresholdSnapshot: Mapped[dict | None] = mapped_column(JSON)

    computedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    firstSeenAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lastSeenAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # How many runs have re-confirmed this identity. Drives "persistent" framing
    # and stops a re-confirmation being mistaken for a new finding.
    occurrenceCount: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expiresAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    acknowledgedBy: Mapped[str | None] = mapped_column(String)
    acknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissedBy: Mapped[str | None] = mapped_column(String)
    dismissedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissedReason: Mapped[str | None] = mapped_column(Text)

    linkedCapaId: Mapped[str | None] = mapped_column(String)
    linkedTrainingId: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    evidence: Mapped[list["SignalEvidence"]] = relationship(
        back_populates="signal", cascade="all, delete-orphan", lazy="selectin"
    )


class SignalEvidence(Base, IdMixin):
    """The specific source records that produced a signal.

    `snapshotJson` freezes the key fields at compute time so the evidence a
    reviewer sees survives later edits to — or soft-deletion of — the source
    record. Without that freeze, "why did this fire?" becomes unanswerable the
    moment someone edits the incident (NFR §7)."""

    __tablename__ = "SignalEvidence"
    __table_args__ = (
        Index("ix_SignalEvidence_signal", "signalId"),
        Index("ix_SignalEvidence_source", "sourceModule", "sourceRecordId"),
    )

    signalId: Mapped[str] = mapped_column(
        ForeignKey("Signal.id", ondelete="CASCADE"), nullable=False
    )
    sourceModule: Mapped[str] = mapped_column(String, nullable=False)
    sourceRecordId: Mapped[str] = mapped_column(String, nullable=False)
    # Human-readable ref (INC-2026-0089) when the source has one, so evidence
    # reads as a record rather than a cuid (house rule: never render a raw id).
    sourceRecordRef: Mapped[str | None] = mapped_column(String)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    snapshotJson: Mapped[dict | None] = mapped_column(JSON)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    signal: Mapped[Signal] = relationship(back_populates="evidence")


class SignalRule(Base, IdMixin):
    """Catalog row for a rule implemented in code.

    The code class is the source of truth; `sync_rule_catalog()` in the runner
    reconciles this table to the registry on every run, so a fresh deployment
    needs no seed script for the engine to work."""

    __tablename__ = "SignalRule"

    code: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String, nullable=False)
    # The KIND of computation (CORRELATION | STATISTICAL | DATA_QUALITY), as
    # distinct from `category`, which is what the finding MEANS. Two axes on
    # purpose: the rule catalog is argued by class ("two rules per category"),
    # while the dashboard filters by meaning. A statistical outlier in audit
    # compliance is genuinely both, and collapsing the axes would lose one.
    # Nullable with a default so the additive DDL needs no backfill window.
    ruleClass: Mapped[str] = mapped_column(String, nullable=False, default="CORRELATION")
    defaultSeverity: Mapped[str] = mapped_column(String, nullable=False, default="INFO")
    # JSON array rather than TEXT[]: the house ORM avoids PG array columns, and
    # nothing queries into this — it is display + provenance metadata.
    sourceModules: Mapped[list | None] = mapped_column(JSON)
    windowDays: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    defaultThresholds: Mapped[dict | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    overrides: Mapped[list["SignalRuleOverride"]] = relationship(
        back_populates="rule", cascade="all, delete-orphan"
    )


class SignalRuleOverride(Base, IdMixin):
    """Per-tenant tuning: enable/disable, threshold values, severity.

    Exists so an implementation team can tune a client's thresholds without an
    engineering ticket (spec §5.1)."""

    __tablename__ = "SignalRuleOverride"
    __table_args__ = (
        UniqueConstraint("tenantId", "ruleId", name="ux_SignalRuleOverride_tenant_rule"),
    )

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default=DEFAULT_TENANT, index=True)
    ruleId: Mapped[str] = mapped_column(ForeignKey("SignalRule.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool | None] = mapped_column(Boolean)
    thresholdJson: Mapped[dict | None] = mapped_column(JSON)
    severityOverride: Mapped[str | None] = mapped_column(String)

    updatedBy: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    rule: Mapped[SignalRule] = relationship(back_populates="overrides")


class SignalRunLog(Base, IdMixin):
    """One row per engine run. The ops/support debugging surface, and the
    evidence for the NFR §7 claim that nightly runs complete clean."""

    __tablename__ = "SignalRunLog"
    __table_args__ = (Index("ix_SignalRunLog_tenant_started", "tenantId", "startedAt"),)

    tenantId: Mapped[str] = mapped_column(String, nullable=False, default=DEFAULT_TENANT, index=True)
    runType: Mapped[str] = mapped_column(String, nullable=False, default="SCHEDULED")  # SCHEDULED | EVENT_TRIGGERED | MANUAL
    triggerEvent: Mapped[str | None] = mapped_column(String)

    startedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    durationMs: Mapped[int | None] = mapped_column(Integer)

    rulesRun: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signalsEmitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # new identities
    signalsUpdated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # re-confirmed
    signalsExpired: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errorCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errorDetail: Mapped[dict | None] = mapped_column(JSON)
    # Per-rule counts + notes (e.g. XCORR-020's unmapped reference codes), so a
    # run is explicable without re-running it.
    ruleDetail: Mapped[dict | None] = mapped_column(JSON)


__all__ = [
    "Signal",
    "SignalEvidence",
    "SignalRule",
    "SignalRuleOverride",
    "SignalRunLog",
    "SEVERITIES",
    "STATUSES",
    "CATEGORIES",
    "LIVE_STATUSES",
    "DEFAULT_TENANT",
]
