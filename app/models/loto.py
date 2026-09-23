"""SQLAlchemy mirror of the LOTO (Lockout/Tagout) Prisma models.

Hand-mirrored with camelCase columns to match Prisma's naming (same convention
as moc.py / competency_matrix.py). Prisma owns the schema declaration and
prisma/apply-loto-ddl.ts owns the physical DDL; this mirror lets the FastAPI
layer read/write the tables.

Additive-only: relationship()s are declared ONLY among these new tables.
References to pre-existing tables (User, Plant, Area, Equipment, Permit) are
plain String columns (FK-by-value), so nothing here can change how an existing
model loads.

`status`, `energyType`, `participantRole` and friends are Strings, not Enums.
That is deliberate and load-bearing: a value that is not a member of a NATIVE
Postgres enum aborts the entire transaction at flush time, and the resulting
swallowed failure is exactly what stranded every agent RCA run in RUNNING
forever. Validation lives in app/schemas/loto.py, so a bad value is a clean 422
instead of a poisoned session. Transition rules live in app/services/loto.py.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin

# ─── Vocabularies ────────────────────────────────────────────────────────────
# Single source of truth for the string columns above. The Pydantic layer
# validates against these, the service layer branches on them, and the frontend
# receives them from GET /api/loto/meta so the dropdowns can never drift from
# what the API will accept.

ENERGY_TYPES: tuple[str, ...] = (
    "electrical",
    "mechanical",
    "hydraulic",
    "pneumatic",
    "thermal",
    "chemical",
    "gravity",
    "other",
)

ISOLATION_METHODS: tuple[str, ...] = (
    "breaker",
    "valve",
    "blocking",
    "blanking",
    "disconnect",
    "plug",
    "chain",
    "other",
)

HARDWARE_ITEM_TYPES: tuple[str, ...] = (
    "lock",
    "tag",
    "hasp",
    "chain",
    "blind",
    "lockbox",
    "other",
)

PROCEDURE_STATUSES: tuple[str, ...] = ("draft", "active", "under_review", "retired")

EXECUTION_STATUSES: tuple[str, ...] = (
    "locks_applied",
    "verified",
    "work_in_progress",
    "locks_removed",
    "closed",
    "aborted",
)

#: An execution in any of these states still holds locks on equipment. A PTW
#: linked to one of these cannot close (spec §6).
OPEN_EXECUTION_STATUSES: frozenset[str] = frozenset(
    {"locks_applied", "verified", "work_in_progress", "locks_removed"}
)

#: Terminal states — the lockout is finished, one way or another.
TERMINAL_EXECUTION_STATUSES: frozenset[str] = frozenset({"closed", "aborted"})

PARTICIPANT_ROLES: tuple[str, ...] = (
    "primary_authorized",
    "secondary",
    "affected_employee",
)

#: Roles that actually put a lock on the equipment. An `affected_employee` is
#: someone whose work is affected by the lockout — OSHA 1910.147(b) — and is
#: NOTIFIED, not locked on. Gating the status transitions on affected employees
#: would block every group lockout on a person who has no lock to confirm.
LOCK_HOLDER_ROLES: frozenset[str] = frozenset({"primary_authorized", "secondary"})

REVIEW_OUTCOMES: tuple[str, ...] = ("pass", "fail", "updated")


# ─── LotoProcedure — the reusable library entry ──────────────────────────────


class LotoProcedure(Base, IdMixin):
    """A reusable, versioned energy-isolation procedure.

    The editable body lives in the four child collections. `version` counts
    material edits to that body; `publishedVersionId` points at the immutable
    LotoProcedureVersion snapshot the FIELD is allowed to see. Those two can
    legitimately disagree — that is the whole point. While a material edit sits
    unapproved, `version` has moved on but a QR scan still resolves the last
    published snapshot, so nobody at the equipment is handed an isolation
    sequence a human has not signed off.
    """

    __tablename__ = "LotoProcedure"

    procedureCode: Mapped[str] = mapped_column(String, nullable=False)

    siteId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    area: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String)

    equipmentId: Mapped[str | None] = mapped_column(String, index=True)
    equipmentName: Mapped[str | None] = mapped_column(String)
    equipmentTag: Mapped[str | None] = mapped_column(String)

    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    publishedVersionId: Mapped[str | None] = mapped_column(String)

    qrCodeToken: Mapped[str | None] = mapped_column(String, unique=True)

    reviewFrequencyMonths: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    lastReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastReviewedById: Mapped[str | None] = mapped_column(String)
    nextReviewDueAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdById: Mapped[str | None] = mapped_column(String)
    updatedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletedBy: Mapped[str | None] = mapped_column(String)
    deletionReason: Mapped[str | None] = mapped_column(String)

    energySources: Mapped[list["LotoEnergySource"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan",
        order_by="LotoEnergySource.sequence",
    )
    isolationPoints: Mapped[list["LotoIsolationPoint"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan",
        order_by="LotoIsolationPoint.sequence",
    )
    hardware: Mapped[list["LotoHardwareRequirement"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan",
    )
    verificationSteps: Mapped[list["LotoVerificationStep"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan",
        order_by="LotoVerificationStep.sequence",
    )
    versions: Mapped[list["LotoProcedureVersion"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan",
        order_by="LotoProcedureVersion.version",
    )
    reviewLogs: Mapped[list["LotoReviewLog"]] = relationship(
        back_populates="procedure", cascade="all, delete-orphan",
    )


class LotoEnergySource(Base, IdMixin):
    __tablename__ = "LotoEnergySource"

    procedureId: Mapped[str] = mapped_column(
        ForeignKey("LotoProcedure.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    energyType: Mapped[str] = mapped_column(String, nullable=False)
    magnitude: Mapped[str | None] = mapped_column(String)
    locationDescription: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    procedure: Mapped[LotoProcedure] = relationship(back_populates="energySources")
    isolationPoints: Mapped[list["LotoIsolationPoint"]] = relationship(
        back_populates="energySource"
    )


class LotoIsolationPoint(Base, IdMixin):
    """One physical isolation point, in the order it must be actioned.

    `sequence` is safety-critical, not cosmetic — isolating a downstream valve
    before the upstream breaker is how people get hurt. The service layer
    renumbers densely from 1 on every replace so the order can never contain a
    gap or a duplicate.
    """

    __tablename__ = "LotoIsolationPoint"

    procedureId: Mapped[str] = mapped_column(
        ForeignKey("LotoProcedure.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    energySourceId: Mapped[str | None] = mapped_column(
        ForeignKey("LotoEnergySource.id", ondelete="SET NULL")
    )
    location: Mapped[str] = mapped_column(String, nullable=False)
    isolationMethod: Mapped[str] = mapped_column(String, nullable=False)
    lockType: Mapped[str | None] = mapped_column(String)
    verificationMethod: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    procedure: Mapped[LotoProcedure] = relationship(back_populates="isolationPoints")
    energySource: Mapped[LotoEnergySource | None] = relationship(
        back_populates="isolationPoints"
    )


class LotoHardwareRequirement(Base, IdMixin):
    __tablename__ = "LotoHardwareRequirement"

    procedureId: Mapped[str] = mapped_column(
        ForeignKey("LotoProcedure.id", ondelete="CASCADE"), nullable=False, index=True
    )
    itemType: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    quantityRequired: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    procedure: Mapped[LotoProcedure] = relationship(back_populates="hardware")


class LotoVerificationStep(Base, IdMixin):
    __tablename__ = "LotoVerificationStep"

    procedureId: Mapped[str] = mapped_column(
        ForeignKey("LotoProcedure.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stepText: Mapped[str] = mapped_column(Text, nullable=False)
    requiresPhoto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requiresSignoff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    procedure: Mapped[LotoProcedure] = relationship(back_populates="verificationSteps")


class LotoProcedureVersion(Base, IdMixin):
    """Immutable snapshot of a procedure body at one version number.

    Never mutated after write. The one exception is `supersededAt` / `isPublished`,
    which record that a LATER version took over — the body itself is frozen.
    """

    __tablename__ = "LotoProcedureVersion"

    procedureId: Mapped[str] = mapped_column(
        ForeignKey("LotoProcedure.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # JSONB, and always REASSIGNED rather than mutated in place — an in-place
    # mutation of a JSON column is invisible to SQLAlchemy's dirty tracking and
    # the commit silently no-ops (the CAMS citation-provenance lesson).
    snapshotJson: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )

    isPublished: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    publishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publishedById: Mapped[str | None] = mapped_column(String)
    supersededAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    changeSummary: Mapped[str | None] = mapped_column(Text)
    changeType: Mapped[str] = mapped_column(String, nullable=False, default="MATERIAL")

    createdById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    procedure: Mapped[LotoProcedure] = relationship(back_populates="versions")


# ─── LotoExecution — one actual lockout event ────────────────────────────────


class LotoExecution(Base, IdMixin):
    __tablename__ = "LotoExecution"

    number: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    procedureId: Mapped[str] = mapped_column(
        ForeignKey("LotoProcedure.id"), nullable=False, index=True
    )
    procedureVersionId: Mapped[str | None] = mapped_column(
        ForeignKey("LotoProcedureVersion.id")
    )

    #: The frozen body. Read this — NEVER the live procedure — for anything the
    #: crew acts on. Reassign wholesale to change it (see snapshotJson note).
    procedureVersionSnapshot: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )
    snapshotVersion: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    ptwId: Mapped[str | None] = mapped_column(String, index=True)
    ptwNumber: Mapped[str | None] = mapped_column(String)

    siteId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)

    initiatedById: Mapped[str] = mapped_column(String, nullable=False)
    initiatedByName: Mapped[str | None] = mapped_column(String)
    initiatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    isGroupLockout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="locks_applied")

    workStartedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locksRemovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    closedById: Mapped[str | None] = mapped_column(String)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closureNotes: Mapped[str | None] = mapped_column(Text)

    abortedById: Mapped[str | None] = mapped_column(String)
    abortedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abortReason: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    deletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletedBy: Mapped[str | None] = mapped_column(String)
    deletionReason: Mapped[str | None] = mapped_column(String)

    participants: Mapped[list["LotoExecutionParticipant"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan",
    )
    verificationRecords: Mapped[list["LotoVerificationRecord"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan",
        order_by="LotoVerificationRecord.sequence",
    )


class LotoExecutionParticipant(Base, IdMixin):
    """One person on a lockout, and their own two confirmations.

    Both `lockAppliedConfirmed` and `lockRemovedConfirmed` are per-person by
    construction, and the API only ever lets a caller move their OWN row. There
    is deliberately no bulk "remove all" write path: a single action that clears
    every participant's lock is indistinguishable, in the record, from a
    supervisor removing someone else's lock — which is the precise thing lockout
    procedure exists to prevent.
    """

    __tablename__ = "LotoExecutionParticipant"

    executionId: Mapped[str] = mapped_column(
        ForeignKey("LotoExecution.id", ondelete="CASCADE"), nullable=False, index=True
    )
    userId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    userName: Mapped[str | None] = mapped_column(String)
    userRole: Mapped[str | None] = mapped_column(String)
    participantRole: Mapped[str] = mapped_column(String, nullable=False, default="secondary")

    assignedIsolationPointIds: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    lockTagNumber: Mapped[str | None] = mapped_column(String)

    lockAppliedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lockAppliedConfirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lockRemovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lockRemovedConfirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    execution: Mapped[LotoExecution] = relationship(back_populates="participants")

    @property
    def is_lock_holder(self) -> bool:
        return self.participantRole in LOCK_HOLDER_ROLES


class LotoVerificationRecord(Base, IdMixin):
    __tablename__ = "LotoVerificationRecord"

    executionId: Mapped[str] = mapped_column(
        ForeignKey("LotoExecution.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Id of the step inside procedureVersionSnapshot — NOT a live FK to
    #: LotoVerificationStep, which may have been edited away since.
    stepId: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stepText: Mapped[str | None] = mapped_column(Text)

    completedById: Mapped[str] = mapped_column(String, nullable=False)
    completedByName: Mapped[str | None] = mapped_column(String)
    completedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    photoUrl: Mapped[str | None] = mapped_column(String)
    signoff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    execution: Mapped[LotoExecution] = relationship(back_populates="verificationRecords")


class LotoReviewLog(Base, IdMixin):
    """One scheduled evaluation cycle.

    Created in `pending` by the daily scan when a procedure falls due, completed
    by a human via POST /api/loto/procedures/{id}/review. A pending row whose
    dueAt has passed is what makes the procedure render as OVERDUE on the
    library screen — the cycle is visible, not silent.
    """

    __tablename__ = "LotoReviewLog"

    procedureId: Mapped[str] = mapped_column(
        ForeignKey("LotoProcedure.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    dueAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    reviewedById: Mapped[str | None] = mapped_column(String)
    reviewedByName: Mapped[str | None] = mapped_column(String)
    reviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    outcome: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)
    newVersionId: Mapped[str | None] = mapped_column(String)

    notifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    procedure: Mapped[LotoProcedure] = relationship(back_populates="reviewLogs")


__all__ = [
    "LotoProcedure",
    "LotoEnergySource",
    "LotoIsolationPoint",
    "LotoHardwareRequirement",
    "LotoVerificationStep",
    "LotoProcedureVersion",
    "LotoExecution",
    "LotoExecutionParticipant",
    "LotoVerificationRecord",
    "LotoReviewLog",
    "ENERGY_TYPES",
    "ISOLATION_METHODS",
    "HARDWARE_ITEM_TYPES",
    "PROCEDURE_STATUSES",
    "EXECUTION_STATUSES",
    "OPEN_EXECUTION_STATUSES",
    "TERMINAL_EXECUTION_STATUSES",
    "PARTICIPANT_ROLES",
    "LOCK_HOLDER_ROLES",
    "REVIEW_OUTCOMES",
]
