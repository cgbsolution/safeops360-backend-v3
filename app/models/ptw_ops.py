"""PTW Control Center v2 tables (docs/ptw-control-center/PLAN.md §3).

Created by safeops_360/prisma/apply-ptw-control-center-ddl.ts (additive,
idempotent). Statuses/effects are TEXT with CHECK constraints — house style,
no new native enums.

Columns added to EXISTING tables by that DDL (Area.geometry / sortOrder /
layoutId, PermitCrewMember.checkedInAt / checkedOutAt) are deliberately NOT
mapped on the Area / PermitCrewMember models: mapping them would make every
existing query against those tables fail on a database where the DDL has not
been run yet. The control-center loaders read them with guarded raw SQL.

Every table carries tenantId (default 'default'), which QueryScope.apply()
filters unconditionally — the single-tenant deployment's multi-client seam.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin

TENANT_DEFAULT = "default"

IMPAIRMENT_SYSTEM_TYPES = (
    "sprinkler", "fire_detection", "fire_alarm", "gas_detection", "interlock", "emergency_lighting", "other",
)
IMPAIRMENT_STATUSES = ("requested", "approved", "active", "restored", "cancelled")
RULE_EFFECTS = ("block_new", "require_controls", "allow")
SEVERITIES = ("critical", "high", "medium", "low")

_J = JSON().with_variant(JSONB(), "postgresql")


def _now():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _tenant():
    return mapped_column(String, nullable=False, default=TENANT_DEFAULT, server_default=TENANT_DEFAULT)


class PlantLayout(Base, IdMixin):
    __tablename__ = "PlantLayout"
    tenantId: Mapped[str] = _tenant()
    plantId: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    storagePath: Mapped[str | None] = mapped_column(String)
    imageUrl: Mapped[str | None] = mapped_column(String)  # static asset (seed) when no storage object
    widthPx: Mapped[int | None] = mapped_column(Integer)
    heightPx: Mapped[int | None] = mapped_column(Integer)
    widthM: Mapped[float | None] = mapped_column(Float)    # real-world metres across the image
    heightM: Mapped[float | None] = mapped_column(Float)
    updatedById: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _now()


class ZoneGuidance(Base, IdMixin):
    __tablename__ = "ZoneGuidance"
    tenantId: Mapped[str] = _tenant()
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    areaId: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    restrictedHours: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    mandatoryPpe: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    assemblyPoint: Mapped[str | None] = mapped_column(String)
    extinguisherLocations: Mapped[str | None] = mapped_column(String)
    # [{permitType, state: allowed|conditional|blocked, conditionText}]
    permitTypeRules: Mapped[list] = mapped_column(_J, nullable=False, default=list)
    updatedById: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _now()


class Impairment(Base, IdMixin):
    __tablename__ = "Impairment"
    tenantId: Mapped[str] = _tenant()
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String, nullable=False)
    systemType: Mapped[str] = mapped_column(String, nullable=False)
    systemName: Mapped[str] = mapped_column(String, nullable=False)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    startedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plannedRestoreAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restoredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restoredById: Mapped[str | None] = mapped_column(String)
    precautions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    approvedById: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False, default="requested")
    cancelledReason: Mapped[str | None] = mapped_column(Text)
    escalationCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastEscalatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdById: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = _now()
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ImpairmentRule(Base, IdMixin):
    __tablename__ = "ImpairmentRule"
    tenantId: Mapped[str] = _tenant()
    plantId: Mapped[str | None] = mapped_column(String)   # NULL = global default
    systemType: Mapped[str] = mapped_column(String, nullable=False)
    permitType: Mapped[str] = mapped_column(String, nullable=False)  # PermitType value or '*'
    effect: Mapped[str] = mapped_column(String, nullable=False)
    requiredControls: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    message: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _now()


class SimopsRule(Base, IdMixin):
    __tablename__ = "SimopsRule"
    tenantId: Mapped[str] = _tenant()
    plantId: Mapped[str | None] = mapped_column(String)
    permitTypeA: Mapped[str] = mapped_column(String, nullable=False)
    permitTypeB: Mapped[str] = mapped_column(String, nullable=False)
    minDistanceM: Mapped[float | None] = mapped_column(Float)
    sameArea: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    severity: Mapped[str] = mapped_column(String, nullable=False, default="critical")
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _now()


class PermitControl(Base, IdMixin):
    """A required control ticked (or evidenced) on a permit — the require_controls
    gate reads these before a permit may leave Issuer Review."""
    __tablename__ = "PermitControl"
    tenantId: Mapped[str] = _tenant()
    permitId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    controlKey: Mapped[str] = mapped_column(String, nullable=False)
    satisfiedAt: Mapped[datetime] = _now()
    satisfiedById: Mapped[str] = mapped_column(String, nullable=False)
    attachmentId: Mapped[str | None] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text)


class PermitEvent(Base, IdMixin):
    """One row per pipeline step change. Written by the DB triggers on
    "Permit".status and "WorkflowInstance".currentStepName (so no code path can
    skip it), plus the synthetic backfill."""
    __tablename__ = "PermitEvent"
    tenantId: Mapped[str] = _tenant()
    permitId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    fromStep: Mapped[str | None] = mapped_column(String)
    toStep: Mapped[str] = mapped_column(String, nullable=False)
    actorId: Mapped[str | None] = mapped_column(String)
    actorRole: Mapped[str | None] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _now()


class PtwException(Base, IdMixin):
    __tablename__ = "PtwException"
    tenantId: Mapped[str] = _tenant()
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ruleKey: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    dedupeKey: Mapped[str] = mapped_column(String, nullable=False)
    permitId: Mapped[str | None] = mapped_column(String)
    impairmentId: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    ref: Mapped[str | None] = mapped_column(String)
    openedAt: Mapped[datetime] = _now()
    resolvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ownerUserId: Mapped[str | None] = mapped_column(String)
    notified: Mapped[list] = mapped_column(_J, nullable=False, default=list)  # [{channel,to,at}]
    acknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledgedById: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PtwShiftBrief(Base, IdMixin):
    __tablename__ = "PtwShiftBrief"
    tenantId: Mapped[str] = _tenant()
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    shift: Mapped[str] = mapped_column(String, nullable=False)
    generatedAt: Mapped[datetime] = _now()
    text: Mapped[str] = mapped_column(Text, nullable=False)
    inputsHash: Mapped[str] = mapped_column(String, nullable=False)


class PtwKioskToken(Base, IdMixin):
    """Opaque wall-display token, stored as a SHA-256 hash, scoped to one plant
    and accepted by exactly one read-only endpoint."""
    __tablename__ = "PtwKioskToken"
    tenantId: Mapped[str] = _tenant()
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    tokenHash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    createdById: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = _now()
    revokedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastSeenAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
