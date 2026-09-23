"""Pydantic schemas for the LOTO (Lockout/Tagout) module.

This layer is the validation boundary. Every controlled vocabulary in the module
is a `Literal` here rather than a Postgres enum on the column, so a value the
API does not recognise comes back as a clean 422 instead of aborting the
transaction at flush time.

Two rules are enforced here rather than only in the router, because they are
properties of the payload and a route that forgot to check would be a silent
hole:

  • A procedure body submitted for publication must carry at least one
    isolation point AND at least one verification step (spec §2.1). See
    `ProcedurePublishability`.
  • Ordered collections are re-sequenced densely from 1 on write, so a client
    that sends gaps, duplicates or nothing at all still produces a procedure
    whose isolation order is unambiguous.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ─────────────────────────────────────────────────────────────────────
# Vocabularies — mirrored from app.models.loto so the two cannot drift.
# ─────────────────────────────────────────────────────────────────────

EnergyType = Literal[
    "electrical", "mechanical", "hydraulic", "pneumatic",
    "thermal", "chemical", "gravity", "other",
]
IsolationMethod = Literal[
    "breaker", "valve", "blocking", "blanking",
    "disconnect", "plug", "chain", "other",
]
HardwareItemType = Literal["lock", "tag", "hasp", "chain", "blind", "lockbox", "other"]
ProcedureStatus = Literal["draft", "active", "under_review", "retired"]
ExecutionStatus = Literal[
    "locks_applied", "verified", "work_in_progress",
    "locks_removed", "closed", "aborted",
]
ParticipantRole = Literal["primary_authorized", "secondary", "affected_employee"]
ReviewOutcome = Literal["pass", "fail", "updated"]


# ─────────────────────────────────────────────────────────────────────
# Procedure body — the four child collections
# ─────────────────────────────────────────────────────────────────────


class EnergySourceIn(BaseModel):
    # Present when editing an existing row; absent when adding a new one. The
    # service matches on it so an edit that only reorders does not destroy and
    # recreate rows the isolation points point at.
    id: str | None = None
    energyType: EnergyType
    magnitude: str | None = None
    locationDescription: str | None = None


class EnergySourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence: int
    energyType: str
    magnitude: str | None = None
    locationDescription: str | None = None


class IsolationPointIn(BaseModel):
    id: str | None = None
    #: Reference into the energy-source list of the SAME payload, by id. A
    #: client adding a brand-new source and a point that references it in one
    #: request sends `energySourceRef` (the array index) instead.
    energySourceId: str | None = None
    energySourceRef: int | None = Field(
        default=None,
        description="0-based index into this payload's energySources, for a source created in the same request",
    )
    location: str = Field(min_length=1)
    isolationMethod: IsolationMethod
    lockType: str | None = None
    verificationMethod: str | None = None
    notes: str | None = None

    @field_validator("location")
    @classmethod
    def _location_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Isolation point location cannot be blank")
        return v.strip()


class IsolationPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence: int
    energySourceId: str | None = None
    location: str
    isolationMethod: str
    lockType: str | None = None
    verificationMethod: str | None = None
    notes: str | None = None


class HardwareRequirementIn(BaseModel):
    id: str | None = None
    itemType: HardwareItemType
    description: str | None = None
    quantityRequired: int = Field(default=1, ge=1, le=999)


class HardwareRequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    itemType: str
    description: str | None = None
    quantityRequired: int


class VerificationStepIn(BaseModel):
    id: str | None = None
    stepText: str = Field(min_length=1)
    requiresPhoto: bool = False
    requiresSignoff: bool = True

    @field_validator("stepText")
    @classmethod
    def _text_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Verification step text cannot be blank")
        return v.strip()


class VerificationStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence: int
    stepText: str
    requiresPhoto: bool
    requiresSignoff: bool


# ─────────────────────────────────────────────────────────────────────
# Procedure
# ─────────────────────────────────────────────────────────────────────


class ProcedureCreate(BaseModel):
    siteId: str = Field(min_length=1)
    areaId: str | None = None
    area: str | None = None

    #: Reference to the Equipment master. Optional because a procedure
    #: legitimately covers something the master carries no row for (a valve
    #: station, a common header) — in which case equipmentName/Tag carry it.
    equipmentId: str | None = None
    equipmentName: str | None = None
    equipmentTag: str | None = None

    #: Left blank, the service allocates the next LOTO-EQ-#### for the site.
    procedureCode: str | None = None

    title: str = Field(min_length=1)
    description: str | None = None
    reviewFrequencyMonths: int = Field(default=12, ge=1, le=60)

    energySources: list[EnergySourceIn] = Field(default_factory=list)
    isolationPoints: list[IsolationPointIn] = Field(default_factory=list)
    hardware: list[HardwareRequirementIn] = Field(default_factory=list)
    verificationSteps: list[VerificationStepIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _equipment_identified(self) -> "ProcedureCreate":
        if not (self.equipmentId or (self.equipmentName and self.equipmentName.strip())):
            raise ValueError(
                "Identify the equipment: pick it from the equipment master "
                "(equipmentId) or name it (equipmentName)."
            )
        return self


class ProcedureUpdate(BaseModel):
    """Every field optional — a PATCH-shaped PUT.

    Passing any of the four body collections REPLACES that collection wholesale
    (it is a set, not a delta) and is what makes an edit MATERIAL. Omitting a
    collection leaves it untouched, so a title-only edit stays MINOR and does
    not withdraw the live approval.
    """

    areaId: str | None = None
    area: str | None = None
    equipmentId: str | None = None
    equipmentName: str | None = None
    equipmentTag: str | None = None
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    reviewFrequencyMonths: int | None = Field(default=None, ge=1, le=60)

    energySources: list[EnergySourceIn] | None = None
    isolationPoints: list[IsolationPointIn] | None = None
    hardware: list[HardwareRequirementIn] | None = None
    verificationSteps: list[VerificationStepIn] | None = None

    changeSummary: str | None = Field(
        default=None,
        description="Why the procedure changed. Recorded on the version row.",
    )


class ProcedureVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    version: int
    isPublished: bool
    publishedAt: datetime | None = None
    publishedById: str | None = None
    publishedByName: str | None = None
    supersededAt: datetime | None = None
    changeType: str
    changeSummary: str | None = None
    createdById: str | None = None
    createdByName: str | None = None
    createdAt: datetime


class ReviewStatus(BaseModel):
    """The scheduled-evaluation state, computed for every list row.

    `isOverdue` is what the library screen renders as a flag. It is derived
    server-side from nextReviewDueAt so the list and the detail can never
    disagree about whether a procedure has lapsed.
    """

    nextReviewDueAt: datetime | None = None
    lastReviewedAt: datetime | None = None
    lastReviewedById: str | None = None
    lastReviewedByName: str | None = None
    isOverdue: bool = False
    isDueSoon: bool = False
    daysUntilDue: int | None = None
    pendingReviewId: str | None = None


class ProcedureListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    procedureCode: str
    title: str
    status: str
    version: int
    publishedVersion: int | None = None

    siteId: str
    siteName: str | None = None
    area: str | None = None

    equipmentId: str | None = None
    equipmentName: str | None = None
    equipmentTag: str | None = None

    qrCodeToken: str | None = None

    energySourceCount: int = 0
    isolationPointCount: int = 0
    verificationStepCount: int = 0
    openExecutionCount: int = 0

    review: ReviewStatus = Field(default_factory=ReviewStatus)

    createdById: str | None = None
    createdByName: str | None = None
    createdAt: datetime
    updatedAt: datetime


class ProcedureListResponse(BaseModel):
    items: list[ProcedureListItem]
    total: int


class ProcedureOut(ProcedureListItem):
    description: str | None = None
    reviewFrequencyMonths: int = 12

    energySources: list[EnergySourceOut] = Field(default_factory=list)
    isolationPoints: list[IsolationPointOut] = Field(default_factory=list)
    hardware: list[HardwareRequirementOut] = Field(default_factory=list)
    verificationSteps: list[VerificationStepOut] = Field(default_factory=list)

    versions: list[ProcedureVersionOut] = Field(default_factory=list)

    #: Server's verdict on §2.1's publish precondition, so the builder can
    #: disable the button for the same reason the API would refuse.
    canPublish: bool = False
    publishBlockers: list[str] = Field(default_factory=list)

    #: True when the live body has moved ahead of the published version, i.e.
    #: a material edit is awaiting re-approval.
    hasUnpublishedChanges: bool = False


class ProcedurePublishRequest(BaseModel):
    notes: str | None = None


class ProcedureReviewRequest(BaseModel):
    outcome: ReviewOutcome
    notes: str | None = None

    @model_validator(mode="after")
    def _fail_needs_a_reason(self) -> "ProcedureReviewRequest":
        # A review that concludes the procedure is wrong, with no note, tells a
        # future auditor nothing and gives the author nothing to act on.
        if self.outcome == "fail" and not (self.notes and self.notes.strip()):
            raise ValueError("A failed review must record why it failed.")
        return self


class ProcedureDeleteRequest(BaseModel):
    reason: str = Field(min_length=3)


# ─────────────────────────────────────────────────────────────────────
# QR — the public, unauthenticated field view
# ─────────────────────────────────────────────────────────────────────


class QrProcedureView(BaseModel):
    """Read-only field view. Deliberately narrow.

    Carries what someone standing at the equipment needs and nothing that
    identifies a person or the wider site: no author, no reviewer, no execution
    history, no internal ids beyond the ones the steps reference. This endpoint
    needs no authentication (spec §2.3), so its payload is its access-control
    boundary.
    """

    procedureCode: str
    title: str
    description: str | None = None
    equipmentName: str | None = None
    equipmentTag: str | None = None
    siteName: str | None = None
    area: str | None = None

    #: The PUBLISHED version — not necessarily the latest. If a material edit is
    #: awaiting re-approval, the field keeps seeing the last approved sequence.
    version: int
    publishedAt: datetime | None = None

    #: draft/under_review procedures with a prior publication still resolve, and
    #: this flag tells the page to say so honestly.
    procedureStatus: str
    isRetired: bool = False
    hasUnpublishedChanges: bool = False

    energySources: list[EnergySourceOut] = Field(default_factory=list)
    isolationPoints: list[IsolationPointOut] = Field(default_factory=list)
    hardware: list[HardwareRequirementOut] = Field(default_factory=list)
    verificationSteps: list[VerificationStepOut] = Field(default_factory=list)

    #: Opaque handle the "Start lockout" button posts back with. The QR view is
    #: anonymous; starting an execution is not, so the button routes through the
    #: authenticated app.
    procedureId: str


# ─────────────────────────────────────────────────────────────────────
# Execution
# ─────────────────────────────────────────────────────────────────────


class ParticipantIn(BaseModel):
    userId: str = Field(min_length=1)
    participantRole: ParticipantRole = "secondary"
    assignedIsolationPointIds: list[str] = Field(default_factory=list)
    lockTagNumber: str | None = None
    notes: str | None = None


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    userId: str
    userName: str | None = None
    userRole: str | None = None
    participantRole: str
    assignedIsolationPointIds: list[str] = Field(default_factory=list)
    lockTagNumber: str | None = None

    lockAppliedAt: datetime | None = None
    lockAppliedConfirmed: bool = False
    lockRemovedAt: datetime | None = None
    lockRemovedConfirmed: bool = False
    notes: str | None = None

    #: False for affected_employee rows — they are notified, not locked on, and
    #: the roster renders them differently so nobody waits on a confirmation
    #: that is never coming.
    isLockHolder: bool = True


class ExecutionCreate(BaseModel):
    procedureId: str = Field(min_length=1)
    ptwId: str | None = None
    isGroupLockout: bool = False
    #: Omit entirely for a solo lockout — the service enrols the caller as the
    #: primary authorised person.
    participants: list[ParticipantIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _group_needs_a_roster(self) -> "ExecutionCreate":
        if self.isGroupLockout and len(self.participants) < 2:
            raise ValueError(
                "A group lockout needs at least two participants. For a single "
                "person, start a solo lockout instead."
            )
        return self


class LockConfirmRequest(BaseModel):
    """Per-participant lock application.

    `participantId` is optional and, when given, must be the caller's OWN row —
    the router re-derives the target from the authenticated user and refuses a
    mismatch. It exists so the mobile client can be explicit, not so one person
    can confirm for another.
    """

    participantId: str | None = None
    lockTagNumber: str | None = None
    notes: str | None = None


class UnlockConfirmRequest(BaseModel):
    participantId: str | None = None
    notes: str | None = None


class VerificationRecordIn(BaseModel):
    stepId: str = Field(min_length=1)
    signoff: bool = False
    photoUrl: str | None = None
    notes: str | None = None


class VerifyRequest(BaseModel):
    """One or more completed verification steps.

    Accepts a batch because a crew works down the checklist and a per-step
    round-trip on a 2G handset at a kiln is not a real interaction. The service
    still validates every step individually.
    """

    records: list[VerificationRecordIn] = Field(min_length=1)


class VerificationRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    stepId: str
    sequence: int
    stepText: str | None = None
    completedById: str
    completedByName: str | None = None
    completedAt: datetime
    photoUrl: str | None = None
    signoff: bool
    notes: str | None = None


class ExecutionCloseRequest(BaseModel):
    closureNotes: str | None = None


class ExecutionAbortRequest(BaseModel):
    reason: str = Field(min_length=3)


class ExecutionStartWorkRequest(BaseModel):
    notes: str | None = None


class ExecutionGate(BaseModel):
    """Why the next transition is or is not available.

    All-at-once, never short-circuited, so the execution screen can render every
    outstanding reason in one panel instead of revealing them one refresh at a
    time. This is the same shape as the PTW activation gate.
    """

    canApplyLocks: bool = False
    canVerify: bool = False
    canStartWork: bool = False
    canRemoveLocks: bool = False
    canClose: bool = False
    blockers: list[str] = Field(default_factory=list)

    #: Named specifically, so the UI can point at the row rather than say
    #: "someone hasn't confirmed".
    awaitingLockConfirmation: list[str] = Field(default_factory=list)
    awaitingUnlockConfirmation: list[str] = Field(default_factory=list)
    outstandingVerificationSteps: list[str] = Field(default_factory=list)


class ExecutionListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: str
    status: str

    procedureId: str
    procedureCode: str | None = None
    procedureTitle: str | None = None
    snapshotVersion: int = 1

    equipmentName: str | None = None
    equipmentTag: str | None = None

    ptwId: str | None = None
    ptwNumber: str | None = None

    siteId: str
    siteName: str | None = None

    initiatedById: str
    initiatedByName: str | None = None
    initiatedAt: datetime

    isGroupLockout: bool
    participantCount: int = 0
    locksConfirmedCount: int = 0
    locksRemovedCount: int = 0
    lockHolderCount: int = 0

    closedAt: datetime | None = None
    createdAt: datetime


class ExecutionListResponse(BaseModel):
    items: list[ExecutionListItem]
    total: int


class ExecutionOut(ExecutionListItem):
    #: The FROZEN body this execution runs against. Callers render THIS, never
    #: a fresh read of the procedure — that is what makes a mid-job authoring
    #: edit invisible to a crew already at the equipment.
    procedureVersionSnapshot: dict[str, Any] = Field(default_factory=dict)

    participants: list[ParticipantOut] = Field(default_factory=list)
    verificationRecords: list[VerificationRecordOut] = Field(default_factory=list)

    workStartedAt: datetime | None = None
    locksRemovedAt: datetime | None = None
    closedById: str | None = None
    closedByName: str | None = None
    closureNotes: str | None = None
    abortedById: str | None = None
    abortedAt: datetime | None = None
    abortReason: str | None = None

    gate: ExecutionGate = Field(default_factory=ExecutionGate)

    #: True when the live procedure has moved past this execution's snapshot.
    #: Surfaced so a supervisor knows the library changed under a running job —
    #: it does NOT change what the crew is following.
    procedureHasChangedSinceStart: bool = False


# ─────────────────────────────────────────────────────────────────────
# PTW cross-reference (spec §6)
# ─────────────────────────────────────────────────────────────────────


class PermitLinkRequest(BaseModel):
    """Link an existing lockout to a permit, or unlink."""

    lotoExecutionId: str | None = Field(
        default=None,
        description="Execution to link. Null unlinks the permit from its current execution.",
    )


class PermitLotoStatus(BaseModel):
    """What the PTW detail screen renders, and what its closure gate reads."""

    permitId: str
    lotoExecutionId: str | None = None
    execution: ExecutionListItem | None = None
    #: True when a linked execution is still holding locks. The permit cannot
    #: close while this is true.
    blocksPermitClosure: bool = False
    blockReason: str | None = None


# ─────────────────────────────────────────────────────────────────────
# Meta — vocabularies for the form dropdowns
# ─────────────────────────────────────────────────────────────────────


class LotoMeta(BaseModel):
    energyTypes: list[str]
    isolationMethods: list[str]
    hardwareItemTypes: list[str]
    procedureStatuses: list[str]
    executionStatuses: list[str]
    participantRoles: list[str]


__all__ = [
    "EnergySourceIn", "EnergySourceOut",
    "IsolationPointIn", "IsolationPointOut",
    "HardwareRequirementIn", "HardwareRequirementOut",
    "VerificationStepIn", "VerificationStepOut",
    "ProcedureCreate", "ProcedureUpdate", "ProcedureOut",
    "ProcedureListItem", "ProcedureListResponse",
    "ProcedureVersionOut", "ProcedurePublishRequest",
    "ProcedureReviewRequest", "ProcedureDeleteRequest",
    "ReviewStatus", "QrProcedureView",
    "ParticipantIn", "ParticipantOut",
    "ExecutionCreate", "LockConfirmRequest", "UnlockConfirmRequest",
    "VerifyRequest", "VerificationRecordIn", "VerificationRecordOut",
    "ExecutionCloseRequest", "ExecutionAbortRequest", "ExecutionStartWorkRequest",
    "ExecutionGate", "ExecutionListItem", "ExecutionListResponse", "ExecutionOut",
    "PermitLinkRequest", "PermitLotoStatus", "LotoMeta",
]
