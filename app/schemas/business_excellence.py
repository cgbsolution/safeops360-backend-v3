"""Pydantic contract for the Business Excellence registers.

camelCase throughout (no alias_generator) to match the DB and the frontend.
These models double as the API contract the Next.js layer mirrors as TS types.

The vocabulary Literals below are generated from the tuples in
models/business_excellence.py rather than retyped, so a value added to the model
cannot drift from what the API accepts. `GET /api/be/meta` serves the same
tuples to the frontend, which closes the loop: a dropdown can never offer a
value that 422s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import UserRefOut
from app.models.business_excellence import (
    ACK_STATUSES,
    BE_SOURCE_MODULES,
    KAIZEN_CATEGORIES,
    KAIZEN_LANES,
    KAIZEN_STATUSES,
    OPL_CATEGORIES,
    OPL_STATUSES,
    POKA_YOKE_APPROACHES,
    POKA_YOKE_DEVICE_TYPES,
    POKA_YOKE_REACTIONS,
    POKA_YOKE_STATUSES,
    SAVING_TYPES,
    VERIFICATION_FREQUENCIES,
    VERIFICATION_RESULTS,
)

KaizenCategory = Literal[KAIZEN_CATEGORIES]  # type: ignore[valid-type]
KaizenLane = Literal[KAIZEN_LANES]  # type: ignore[valid-type]
KaizenStatus = Literal[KAIZEN_STATUSES]  # type: ignore[valid-type]
SavingType = Literal[SAVING_TYPES]  # type: ignore[valid-type]
OplCategory = Literal[OPL_CATEGORIES]  # type: ignore[valid-type]
OplStatus = Literal[OPL_STATUSES]  # type: ignore[valid-type]
AckStatus = Literal[ACK_STATUSES]  # type: ignore[valid-type]
PokaYokeDeviceType = Literal[POKA_YOKE_DEVICE_TYPES]  # type: ignore[valid-type]
PokaYokeApproach = Literal[POKA_YOKE_APPROACHES]  # type: ignore[valid-type]
PokaYokeReaction = Literal[POKA_YOKE_REACTIONS]  # type: ignore[valid-type]
PokaYokeStatus = Literal[POKA_YOKE_STATUSES]  # type: ignore[valid-type]
VerificationFrequency = Literal[VERIFICATION_FREQUENCIES]  # type: ignore[valid-type]
VerificationResult = Literal[VERIFICATION_RESULTS]  # type: ignore[valid-type]
BeSourceModule = Literal[BE_SOURCE_MODULES]  # type: ignore[valid-type]


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ─────────────────────────────────────────────────────────────────────────────
# Shared
# ─────────────────────────────────────────────────────────────────────────────
# Never render a raw user id — the platform rule. `UserRefOut` is the shared
# type every module already resolves through services/user_directory.py, so BE
# payloads carry exactly the same shape the frontend's <UserRefLabel/> expects.
# Aliased rather than re-declared: a second, nearly-identical ref type is how
# the two halves drift.
UserRef = UserRefOut


class MetaOut(BaseModel):
    """Everything the BE forms need to build their dropdowns."""

    kaizenCategories: list[str]
    kaizenLanes: list[str]
    kaizenStatuses: list[str]
    savingTypes: list[str]
    oplCategories: list[str]
    oplStatuses: list[str]
    ackStatuses: list[str]
    pokaYokeDeviceTypes: list[str]
    pokaYokeApproaches: list[str]
    pokaYokeReactions: list[str]
    pokaYokeStatuses: list[str]
    verificationFrequencies: list[str]
    verificationResults: list[str]
    sourceModules: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen
# ─────────────────────────────────────────────────────────────────────────────
class KaizenCreate(BaseModel):
    plantId: str
    areaId: str | None = None
    title: str = Field(min_length=4, max_length=200)
    category: KaizenCategory
    lane: KaizenLane = "STANDARD"
    lineOrMachine: str | None = Field(default=None, max_length=200)
    processStep: str | None = Field(default=None, max_length=200)
    problemStatement: str = Field(min_length=10)
    currentState: str | None = None
    proposedImprovement: str = Field(min_length=10)
    expectedBenefit: str | None = None
    ownerId: str | None = None
    targetDate: datetime | None = None
    currency: str = "INR"
    investmentCost: float | None = Field(default=None, ge=0)
    estimatedAnnualSaving: float | None = Field(default=None, ge=0)
    savingType: SavingType | None = None
    sourceModule: BeSourceModule | None = None
    sourceRecordId: str | None = None
    sourceRecordRef: str | None = None

    @field_validator("savingType")
    @classmethod
    def _saving_type_needs_a_figure(cls, v: str | None, info: Any) -> str | None:
        # A saving TYPE with no saving AMOUNT is a claim with no number behind
        # it, and it reads on the register as if a value had been recorded.
        if v is not None and not info.data.get("estimatedAnnualSaving"):
            raise ValueError(
                "savingType is only meaningful with an estimatedAnnualSaving figure."
            )
        return v


class KaizenUpdate(BaseModel):
    """Every field optional — PATCH semantics. `model_fields_set` is what the
    router reads, so a caller can explicitly clear a nullable field by sending
    null rather than having the value silently preserved."""

    areaId: str | None = None
    title: str | None = Field(default=None, min_length=4, max_length=200)
    category: KaizenCategory | None = None
    lane: KaizenLane | None = None
    lineOrMachine: str | None = None
    processStep: str | None = None
    problemStatement: str | None = Field(default=None, min_length=10)
    currentState: str | None = None
    proposedImprovement: str | None = Field(default=None, min_length=10)
    expectedBenefit: str | None = None
    ownerId: str | None = None
    targetDate: datetime | None = None
    currency: str | None = None
    investmentCost: float | None = Field(default=None, ge=0)
    estimatedAnnualSaving: float | None = Field(default=None, ge=0)
    savingType: SavingType | None = None
    implementationNote: str | None = None
    yokotenScope: dict | None = None
    rewardPoints: int | None = Field(default=None, ge=0)


class KaizenReplicationOut(_Base):
    """One recorded horizontal deployment of an idea to another plant."""

    id: str
    replicatedAtPlantId: str
    #: Frozen at write time. A plant renamed next year must not rewrite last
    #: year's spread history, and the client must never see the cuid.
    replicatedAtPlantName: str | None = None
    replicaKaizenId: str | None = None
    replicaKaizenNo: str | None = None
    replicatedBy: UserRef | None = None
    replicatedAt: datetime
    notes: str | None = None


class KaizenReplicateIn(BaseModel):
    """Body for POST /kaizen/{id}/replicate."""

    plantId: str
    areaId: str | None = None
    notes: str | None = None
    #: Override the copied title. Defaults to the source title, which is usually
    #: right — the whole point of yokoten is that it is the same idea.
    title: str | None = Field(default=None, min_length=4, max_length=200)
    #: Copy the source's owner across. Off by default: the person who ran it at
    #: one plant is rarely the person who will run it at another, and silently
    #: assigning them work at a site they do not work at is worse than leaving it
    #: unassigned (where the approval gate will catch it).
    copyOwner: bool = False


class KaizenSearchHit(_Base):
    """A similar-idea hit, for the raise form's duplicate check.

    Deliberately thin. This renders in a dropdown under a half-typed title while
    someone is still writing, so it carries what identifies the idea and nothing
    that would tempt a caller to use it as a list payload.
    """

    id: str
    kaizenNo: str | None
    title: str
    status: str
    category: str
    plantId: str
    siteName: str | None
    createdAt: datetime
    #: 0..1 trigram similarity against the query, highest first. Exposed so the
    #: UI can distinguish "almost certainly the same idea" from "vaguely
    #: related" rather than presenting every hit with equal weight.
    similarity: float


class KaizenSearchResponse(BaseModel):
    items: list[KaizenSearchHit]
    #: Plants actually searched, after the caller's read scope was applied. The
    #: UI states this ("searched 12 plants") so a user knows a nil result means
    #: "not raised at the plants you can see", not "not raised anywhere".
    plantsSearched: int


class KaizenParticipationOut(BaseModel):
    """§3's engagement figures for one plant and one period.

    `participationRate` and `headcount` are null — never 0 — when the plant has
    no FactoryProfile. Only 16 of 28 plants have one, so this is the normal case
    and the screen has to say "headcount not recorded" rather than render a 0%
    that reads as a failing programme.
    """

    plantId: str | None = None
    siteName: str | None = None
    periodFrom: datetime | None = None
    periodTo: datetime | None = None
    submitters: int = 0
    ideas: int = 0
    headcount: int | None = None
    participationRate: float | None = None
    ideasPerSubmitter: float | None = None
    #: Names the source of the denominator so the number is auditable on the
    #: screen rather than only in this file.
    headcountSource: str = "FactoryProfile.totalEmployees"
    #: Top contributors this period. Not a leaderboard the platform awards
    #: anything for — see `recognitionNote`.
    topContributors: list[KaizenContributorOut] = []
    #: Recognition is NOT wired to Kaizen. RecognitionEntry's award categories
    #: are observation- and leadership-walk-based and no job scores Kaizen, so
    #: this endpoint reports contribution and stops there rather than growing a
    #: second recognition mechanism inside Business Excellence.
    recognitionNote: str | None = None


class KaizenContributorOut(BaseModel):
    user: UserRef | None = None
    ideas: int
    verifiedSaving: float | None = None


class KaizenListItem(_Base):
    id: str
    kaizenNo: str | None
    title: str
    category: str
    lane: str
    status: str
    plantId: str
    siteName: str | None
    areaName: str | None
    lineOrMachine: str | None
    owner: UserRef | None = None
    raisedBy: UserRef | None = None
    targetDate: datetime | None
    implementedAt: datetime | None
    currency: str
    estimatedAnnualSaving: float | None
    verifiedAnnualSaving: float | None
    isOverdue: bool = False
    createdAt: datetime
    #: Whether the FAST-TRACK BADGE is earned by this record's own investment and
    #: implementation-window figures. Deliberately separate from `lane`, which
    #: routes the approval workflow: the register used to render the badge
    #: straight off `lane`, so five live records claimed "cheap and quick" with a
    #: null investment AND a null saving. Server-computed on every read — there
    #: is no stored fastTrack column to drift out of step with the money fields.
    fastTrack: bool = False
    #: How many other plants this idea has actually been deployed at. Counts
    #: BeKaizenReplication rows, not `yokotenScope` intentions.
    replicationCount: int = 0


class KaizenCycleTimeOut(BaseModel):
    """§2's cycle-time aggregate, over the caller's whole scoped set.

    Both medians are null below `minimumSampleSize` measurable records. Null is
    the honest answer and the UI renders "Not enough data"; a median computed
    over three records looks exactly like a median computed over three hundred.
    """

    medianDaysRaisedToImplemented: float | None = None
    medianDaysRaisedToVerified: float | None = None
    implementedSampleSize: int = 0
    verifiedSampleSize: int = 0
    minimumSampleSize: int = 5


class KaizenListResponse(BaseModel):
    items: list[KaizenListItem]
    total: int
    statusCounts: dict[str, int] = {}
    #: Computed over every record in scope, not over the returned page — a
    #: median of whatever fitted on page one is not a median.
    cycleTime: KaizenCycleTimeOut = KaizenCycleTimeOut()


class KaizenOut(KaizenListItem):
    problemStatement: str
    currentState: str | None
    proposedImprovement: str
    expectedBenefit: str | None
    processStep: str | None
    investmentCost: float | None
    savingType: str | None
    implementationNote: str | None
    verifiedBy: UserRef | None = None
    verifiedAt: datetime | None
    verificationNote: str | None
    yokotenScope: dict | None
    rewardPoints: int | None
    rejectionReason: str | None
    closedAt: datetime | None
    sourceModule: str | None
    sourceRecordId: str | None
    sourceRecordRef: str | None
    workflowInstanceId: str | None
    updatedAt: datetime
    #: Which transitions the CALLER may perform right now, already resolved
    #: against status and permissions. The UI renders buttons from this rather
    #: than re-deriving the rules — a disabled button whose reason the client
    #: guessed is how the PTW closure bugs happened.
    availableActions: list[str] = []

    # ── Cycle-time instrumentation ──
    screenedAt: datetime | None = None
    approvedAt: datetime | None = None

    # ── §5 Why the fast-track badge is or is not shown ──
    #: Human-readable reasons the badge is withheld, e.g. "No investment figure
    #: recorded." Returned so the detail screen can explain the absence instead
    #: of leaving a submitter to wonder where their badge went.
    fastTrackReasons: list[str] = []
    fastTrackMaxInvestment: float | None = None
    fastTrackMaxImplementationDays: int | None = None

    # ── §6 Why Approve is disabled ──
    #: Empty means approval is possible. The router enforces the same list, so
    #: the button and the gate are the same rule read twice, not two rules.
    approvalBlockers: list[str] = []

    # ── §4 Horizontal deployment ──
    originKaizenId: str | None = None
    #: The source record's number, when this one was replicated from another
    #: plant. Resolved server-side because the client must never be handed a raw
    #: cuid to render.
    originKaizenNo: str | None = None
    replications: list[KaizenReplicationOut] = []

    # ── §7 Closed loop into standard work ──
    generatedOplId: str | None = None
    generatedOplNo: str | None = None
    generatedOplTitle: str | None = None

    # ── §3 Recognition seam. Always null today — see the model comment. ──
    recognitionEventId: str | None = None


class KaizenTransition(BaseModel):
    """Body for POST /kaizen/{id}/transition/{target}.

    `verifiedAnnualSaving` rides on the VERIFIED transition rather than on
    KaizenUpdate, and that placement is the point. The column is readable on
    KaizenOut but was writable NOWHERE, so the VERIFIED gate — which refuses to
    advance while it is null — could never be satisfied and no Kaizen could
    reach VERIFIED or CLOSED. Found by seeding a full lifecycle and watching it
    dead-end.

    Putting it on the transition keeps the narrow gate: that endpoint already
    requires KAIZEN.VERIFY and already refuses the person who raised the idea.
    Adding it to KaizenUpdate instead would have let anyone holding
    KAIZEN.UPDATE — supervisors, safety officers, most of the plant — write the
    savings figure finance is later asked to stand behind.
    """

    note: str | None = None
    #: Required by the VERIFIED transition; ignored by every other target.
    verifiedAnnualSaving: float | None = Field(default=None, ge=0)


class KaizenReject(BaseModel):
    reason: str = Field(min_length=4)


class KaizenImplement(BaseModel):
    implementedAt: datetime | None = None
    implementationNote: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# One Point Lesson
# ─────────────────────────────────────────────────────────────────────────────
class OplAudience(BaseModel):
    """Who has to read this. Resolved to concrete acknowledgement rows at
    publish time — see services/business_excellence.resolve_opl_audience."""

    roleCodes: list[str] = []
    areaIds: list[str] = []
    userIds: list[str] = []
    #: Everyone with a roster row at the OPL's plant. Deliberately explicit
    #: rather than "empty audience means everyone" — an accidental
    #: everybody-must-read is a plant-wide notification nobody asked for.
    allPlant: bool = False


class OplCreate(BaseModel):
    plantId: str
    areaId: str | None = None
    title: str = Field(min_length=4, max_length=200)
    category: OplCategory
    lineOrMachine: str | None = Field(default=None, max_length=200)
    contentHtml: str | None = None
    keyPoints: list[str] = []
    effectiveFrom: datetime | None = None
    reviewDueAt: datetime | None = None
    audience: OplAudience = Field(default_factory=OplAudience)
    acknowledgementDueDays: int = Field(default=14, ge=1, le=365)
    competencyId: str | None = None
    sourceModule: BeSourceModule | None = None
    sourceRecordId: str | None = None
    sourceRecordRef: str | None = None

    @field_validator("keyPoints")
    @classmethod
    def _cap_key_points(cls, v: list[str]) -> list[str]:
        # An OPL is one page. Five bullets is the point at which it stops being
        # one, and the constraint is the format's whole value.
        if len(v) > 5:
            raise ValueError("An OPL carries at most 5 key points — it is a ONE point lesson.")
        return v


class OplUpdate(BaseModel):
    areaId: str | None = None
    title: str | None = Field(default=None, min_length=4, max_length=200)
    category: OplCategory | None = None
    lineOrMachine: str | None = None
    contentHtml: str | None = None
    keyPoints: list[str] | None = None
    effectiveFrom: datetime | None = None
    reviewDueAt: datetime | None = None
    audience: OplAudience | None = None
    acknowledgementDueDays: int | None = Field(default=None, ge=1, le=365)
    competencyId: str | None = None


class AckSummary(BaseModel):
    assigned: int = 0
    read: int = 0
    acknowledged: int = 0
    waived: int = 0
    overdue: int = 0
    #: acknowledged / assigned, 0-100, or None when nothing is assigned yet.
    #: None rather than 0 — "nobody has been asked" and "nobody has read it"
    #: are different facts and a 0% badge conflates them.
    percent: float | None = None


class OplListItem(_Base):
    id: str
    oplNo: str | None
    title: str
    category: str
    status: str
    revision: int
    plantId: str
    siteName: str | None
    areaName: str | None
    lineOrMachine: str | None
    author: UserRef | None = None
    effectiveFrom: datetime | None
    reviewDueAt: datetime | None
    publishedAt: datetime | None
    isReviewOverdue: bool = False
    acknowledgement: AckSummary = Field(default_factory=AckSummary)
    createdAt: datetime


class OplListResponse(BaseModel):
    items: list[OplListItem]
    total: int
    statusCounts: dict[str, int] = {}


class OplOut(OplListItem):
    contentHtml: str | None
    keyPoints: list[str] = []
    audience: OplAudience = Field(default_factory=OplAudience)
    acknowledgementDueDays: int
    competencyId: str | None
    approver: UserRef | None = None
    approvedAt: datetime | None
    supersedesOplId: str | None
    supersededByOplId: str | None
    rejectionReason: str | None
    retiredAt: datetime | None
    sourceModule: str | None
    sourceRecordId: str | None
    sourceRecordRef: str | None
    workflowInstanceId: str | None
    updatedAt: datetime
    availableActions: list[str] = []
    #: Present only for the caller — their own obligation against this OPL, if
    #: they have one. Drives the "Acknowledge" button on the viewer.
    myAcknowledgement: "AckOut | None" = None


class AckOut(_Base):
    id: str
    oplId: str
    oplRevision: int
    person: UserRef | None = None
    status: str
    assignedAt: datetime
    dueAt: datetime | None
    readAt: datetime | None
    acknowledgedAt: datetime | None
    acknowledgementNote: str | None
    isOverdue: bool = False


class AckListResponse(BaseModel):
    items: list[AckOut]
    total: int
    summary: AckSummary = Field(default_factory=AckSummary)


class AckConfirm(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class OplReject(BaseModel):
    reason: str = Field(min_length=4)


# ─────────────────────────────────────────────────────────────────────────────
# Poka Yoke
# ─────────────────────────────────────────────────────────────────────────────
class PokaYokeCreate(BaseModel):
    plantId: str
    areaId: str | None = None
    title: str = Field(min_length=4, max_length=200)
    defectModePrevented: str = Field(min_length=5)
    description: str | None = None
    deviceType: PokaYokeDeviceType
    approach: PokaYokeApproach
    reactionMode: PokaYokeReaction
    lineOrMachine: str | None = Field(default=None, max_length=200)
    processStep: str | None = Field(default=None, max_length=200)
    beforeCondition: str | None = None
    afterCondition: str | None = None
    ownerId: str | None = None
    currency: str = "INR"
    cost: float | None = Field(default=None, ge=0)
    installedAt: datetime | None = None
    verificationFrequency: VerificationFrequency = "MONTHLY"
    sourceKaizenId: str | None = None
    sourceRcaId: str | None = None


class PokaYokeUpdate(BaseModel):
    areaId: str | None = None
    title: str | None = Field(default=None, min_length=4, max_length=200)
    defectModePrevented: str | None = Field(default=None, min_length=5)
    description: str | None = None
    deviceType: PokaYokeDeviceType | None = None
    approach: PokaYokeApproach | None = None
    reactionMode: PokaYokeReaction | None = None
    lineOrMachine: str | None = None
    processStep: str | None = None
    beforeCondition: str | None = None
    afterCondition: str | None = None
    ownerId: str | None = None
    currency: str | None = None
    cost: float | None = Field(default=None, ge=0)
    installedAt: datetime | None = None
    verificationFrequency: VerificationFrequency | None = None
    sourceKaizenId: str | None = None
    sourceRcaId: str | None = None


class PokaYokeListItem(_Base):
    id: str
    deviceNo: str | None
    title: str
    status: str
    deviceType: str
    approach: str
    reactionMode: str
    plantId: str
    siteName: str | None
    areaName: str | None
    lineOrMachine: str | None
    owner: UserRef | None = None
    defectModePrevented: str
    installedAt: datetime | None
    verificationFrequency: str
    lastVerifiedAt: datetime | None
    lastVerificationResult: str | None
    nextVerificationDueAt: datetime | None
    isVerificationOverdue: bool = False
    isBypassed: bool = False
    # What the badge should say, which is NOT always `status`. A device with an
    # open bypass is not protecting the line whatever its lifecycle column
    # holds, and both values travel so the screen can show the honest one
    # without the register losing what the device was before it was switched
    # off. Derived server-side — never computed in the browser, or the two
    # halves drift the first time the rule changes.
    displayStatus: str
    # Hours the current bypass has been open, measured to NOW while it is still
    # open. Null when the device is not bypassed.
    bypassOpenHours: float | None = None
    createdAt: datetime


class PokaYokeListResponse(BaseModel):
    items: list[PokaYokeListItem]
    total: int
    statusCounts: dict[str, int] = {}
    overdueCount: int = 0
    # Scope-wide, like overdueCount — NOT a count of the rows on this page. The
    # register's banner claims a plant-wide number, and the previous version
    # computed it client-side from the first 200 rows, so a plant with 250
    # devices under-reported its own bypasses and nobody could tell.
    activeBypasses: int = 0


class PokaYokeOut(PokaYokeListItem):
    description: str | None
    processStep: str | None
    beforeCondition: str | None
    afterCondition: str | None
    currency: str
    cost: float | None
    # ── The flat columns, kept for back-compat ──
    # These are a CACHE of the currently-open episode. `bypasses` below is the
    # record of truth; anything historical must be read from there.
    bypassReason: str | None
    bypassedAt: datetime | None
    bypassedBy: UserRef | None = None
    bypassApprovedBy: UserRef | None = None
    # The full append-only history, newest first, open episodes included.
    bypasses: list["BypassOut"] = []
    activeBypass: "BypassOut | None" = None
    rejectionReason: str | None
    retiredAt: datetime | None
    sourceKaizenId: str | None
    sourceRcaId: str | None
    workflowInstanceId: str | None
    updatedAt: datetime
    availableActions: list[str] = []
    verifications: list["VerificationOut"] = []


class VerificationOut(_Base):
    id: str
    deviceId: str
    verifiedBy: UserRef | None = None
    verifiedAt: datetime
    result: str
    note: str | None
    dueAt: datetime | None
    capaId: str | None


class VerificationCreate(BaseModel):
    result: VerificationResult
    note: str | None = None
    verifiedAt: datetime | None = None

    @field_validator("note")
    @classmethod
    def _fail_needs_a_note(cls, v: str | None, info: Any) -> str | None:
        # A failed check with no explanation is a red row nobody can act on.
        if info.data.get("result") == "FAIL" and not (v or "").strip():
            raise ValueError("A failed verification must record what was wrong.")
        return v


class BypassOut(_Base):
    id: str
    deviceId: str
    bypassedBy: UserRef | None = None
    bypassedAt: datetime
    reason: str
    approvedBy: UserRef | None = None
    statusAtBypass: str | None = None
    restoredAt: datetime | None = None
    restoredBy: UserRef | None = None
    restoreNote: str | None = None
    # Measured to now while the episode is still open — see
    # services/business_excellence.bypass_duration_hours. A blank cell until the
    # day somebody restores it is how a bypass becomes permanent without anybody
    # deciding it should be.
    durationHours: float | None = None
    isOpen: bool = False


class BypassRequest(BaseModel):
    # A bypass removes protection from a line. Ten characters is not a high bar,
    # but it does refuse "n/a" and "temp", which is the whole point: the reason
    # is the only thing the register can hand an auditor.
    reason: str = Field(min_length=10)
    approvedById: str | None = None


class BypassRestore(BaseModel):
    note: str | None = None


class PokaYokeReject(BaseModel):
    reason: str = Field(min_length=4)


OplOut.model_rebuild()
PokaYokeOut.model_rebuild()


__all__ = [
    "AckConfirm",
    "BypassOut",
    "BypassRestore",
    "AckListResponse",
    "AckOut",
    "AckSummary",
    "BypassRequest",
    "KaizenCreate",
    "KaizenImplement",
    "KaizenListItem",
    "KaizenListResponse",
    "KaizenOut",
    "KaizenReject",
    "KaizenTransition",
    "KaizenUpdate",
    "MetaOut",
    "OplAudience",
    "OplCreate",
    "OplListItem",
    "OplListResponse",
    "OplOut",
    "OplReject",
    "OplUpdate",
    "PokaYokeCreate",
    "PokaYokeListItem",
    "PokaYokeListResponse",
    "PokaYokeOut",
    "PokaYokeReject",
    "PokaYokeUpdate",
    "UserRef",
    "VerificationCreate",
    "VerificationOut",
]
