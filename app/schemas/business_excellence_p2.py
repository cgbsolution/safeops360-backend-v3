"""Pydantic contract for Business Excellence Phase 2 — Suggestion, QCC, SIP and
the shared benefit layer.

Same conventions as Phase 1: camelCase throughout (no alias_generator), Literals
generated from the model tuples rather than retyped so a vocabulary cannot drift
from what the API accepts, and `GET /api/be/meta` serving those same tuples so a
dropdown can never offer a value that 422s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.business_excellence_p2 import (
    BE_CATEGORIES,
    BENEFIT_SOURCE_TYPES,
    BENEFIT_STATUSES,
    BENEFIT_TYPES,
    BENEFIT_VALUE_KINDS,
    INCENTIVE_STATUSES,
    MILESTONE_STATUSES,
    QCC_MEMBER_ROLES,
    QCC_METHODOLOGIES,
    QCC_PROJECT_STATUSES,
    QCC_STAGE_STATUSES,
    QCC_TEAM_STATUSES,
    RAG_STATUSES,
    SCREENING_OUTCOMES,
    SIP_STATUSES,
    SUGGESTION_DECISIONS,
    SUGGESTION_STATUSES,
    VALIDATION_WINDOW_MONTHS,
)
from app.schemas.common import UserRefOut

BeCategory = Literal[BE_CATEGORIES]  # type: ignore[valid-type]
SuggestionStatus = Literal[SUGGESTION_STATUSES]  # type: ignore[valid-type]
ScreeningOutcome = Literal[SCREENING_OUTCOMES]  # type: ignore[valid-type]
SuggestionDecision = Literal[SUGGESTION_DECISIONS]  # type: ignore[valid-type]
IncentiveStatus = Literal[INCENTIVE_STATUSES]  # type: ignore[valid-type]
QccTeamStatus = Literal[QCC_TEAM_STATUSES]  # type: ignore[valid-type]
QccMemberRole = Literal[QCC_MEMBER_ROLES]  # type: ignore[valid-type]
QccMethodology = Literal[QCC_METHODOLOGIES]  # type: ignore[valid-type]
QccProjectStatus = Literal[QCC_PROJECT_STATUSES]  # type: ignore[valid-type]
QccStageStatus = Literal[QCC_STAGE_STATUSES]  # type: ignore[valid-type]
SipStatus = Literal[SIP_STATUSES]  # type: ignore[valid-type]
MilestoneStatus = Literal[MILESTONE_STATUSES]  # type: ignore[valid-type]
RagStatus = Literal[RAG_STATUSES]  # type: ignore[valid-type]
BenefitType = Literal[BENEFIT_TYPES]  # type: ignore[valid-type]
BenefitValueKind = Literal[BENEFIT_VALUE_KINDS]  # type: ignore[valid-type]
BenefitStatus = Literal[BENEFIT_STATUSES]  # type: ignore[valid-type]
BenefitSourceType = Literal[BENEFIT_SOURCE_TYPES]  # type: ignore[valid-type]
ValidationWindow = Literal[VALIDATION_WINDOW_MONTHS]  # type: ignore[valid-type]

UserRef = UserRefOut


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class P2MetaOut(BaseModel):
    """Everything the Phase 2 forms need to build their dropdowns."""

    categories: list[str]
    suggestionStatuses: list[str]
    screeningOutcomes: list[str]
    suggestionDecisions: list[str]
    incentiveStatuses: list[str]
    qccTeamStatuses: list[str]
    qccMemberRoles: list[str]
    qccMethodologies: list[str]
    qccStagesByMethodology: dict[str, list[str]]
    qccProjectStatuses: list[str]
    qccStageStatuses: list[str]
    sipStatuses: list[str]
    milestoneStatuses: list[str]
    ragStatuses: list[str]
    benefitTypes: list[str]
    benefitValueKinds: list[str]
    benefitStatuses: list[str]
    benefitSourceTypes: list[str]
    validationWindowMonths: list[int]


# ─────────────────────────────────────────────────────────────────────────────
# Suggestion Scheme
# ─────────────────────────────────────────────────────────────────────────────
class SuggestionCreate(BaseModel):
    plantId: str
    areaId: str | None = None
    title: str = Field(min_length=4, max_length=200)
    category: BeCategory
    description: str = Field(min_length=10)
    expectedBenefit: str | None = None
    isAnonymous: bool = False
    ownerId: str | None = None
    targetDate: datetime | None = None
    sourceModule: str | None = None
    sourceRecordId: str | None = None
    sourceRecordRef: str | None = None


class SuggestionUpdate(BaseModel):
    """Every field optional — PATCH semantics. The router reads
    `model_fields_set`, so a caller clears a nullable field by sending null."""

    areaId: str | None = None
    title: str | None = Field(default=None, min_length=4, max_length=200)
    category: BeCategory | None = None
    description: str | None = Field(default=None, min_length=10)
    expectedBenefit: str | None = None
    ownerId: str | None = None
    targetDate: datetime | None = None
    implementationNote: str | None = None
    # `isAnonymous` is deliberately absent. Un-anonymising a submission after the
    # fact would retroactively expose someone who chose not to be named, and
    # anonymising one after people have seen the name achieves nothing. It is set
    # once, at creation.


class SuggestionScreen(BaseModel):
    """Stage one of §3's two-stage decision — triage."""

    outcome: ScreeningOutcome
    note: str | None = None
    duplicateOfSuggestionId: str | None = None

    @model_validator(mode="after")
    def _duplicate_needs_a_target(self) -> "SuggestionScreen":
        if self.outcome == "DUPLICATE" and not self.duplicateOfSuggestionId:
            raise ValueError(
                "Point a duplicate at the suggestion it duplicates — an unlinked "
                "duplicate is indistinguishable from a rejection."
            )
        return self


class SuggestionDecide(BaseModel):
    """Stage two — the committee's formal outcome."""

    decision: SuggestionDecision
    # Required on EVERY outcome, including ACCEPT. §3: "Decision rationale
    # captured against every outcome, visible to the submitter." A scheme that
    # explains its rejections but not its acceptances teaches people nothing
    # about what a good suggestion looks like.
    rationale: str = Field(min_length=10)
    deferredUntil: datetime | None = None

    @model_validator(mode="after")
    def _defer_needs_a_date(self) -> "SuggestionDecide":
        if self.decision == "DEFER" and self.deferredUntil is None:
            raise ValueError(
                "A deferred suggestion needs a date to be looked at again, or it "
                "is a rejection nobody admitted to."
            )
        if self.decision != "DEFER" and self.deferredUntil is not None:
            raise ValueError("deferredUntil applies only to a DEFER decision.")
        return self


class SuggestionIncentive(BaseModel):
    status: IncentiveStatus
    points: int | None = Field(default=None, ge=0)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = None
    note: str | None = None


class SuggestionTransition(BaseModel):
    note: str | None = None


class SuggestionReject(BaseModel):
    reason: str = Field(min_length=4)


class SuggestionListItem(_Base):
    id: str
    suggestionNo: str | None
    title: str
    category: str
    status: str
    plantId: str
    siteName: str | None
    areaName: str | None
    isAnonymous: bool
    #: None when the record is anonymous and the caller is not the submitter.
    #: The suppression happens in the router via `submitter_visible_to()`; this
    #: field being nullable is what makes that expressible without a second
    #: payload shape.
    submittedBy: UserRef | None = None
    owner: UserRef | None = None
    screeningOutcome: str | None
    decision: str | None
    incentiveStatus: str
    targetDate: datetime | None
    implementedAt: datetime | None
    isOverdue: bool = False
    deferralDue: bool = False
    createdAt: datetime


class SuggestionListResponse(BaseModel):
    items: list[SuggestionListItem]
    total: int
    statusCounts: dict[str, int] = {}


class SuggestionOut(SuggestionListItem):
    description: str
    expectedBenefit: str | None
    screeningNote: str | None
    screenedBy: UserRef | None = None
    screenedAt: datetime | None
    duplicateOfSuggestionId: str | None
    decisionRationale: str | None
    decidedBy: UserRef | None = None
    decidedAt: datetime | None
    deferredUntil: datetime | None
    implementationNote: str | None
    incentivePoints: int | None
    incentiveAmount: float | None
    currency: str
    incentiveNote: str | None
    convertedToKaizenId: str | None
    rejectionReason: str | None
    closedAt: datetime | None
    workflowInstanceId: str | None
    updatedAt: datetime
    availableActions: list[str] = []


# ─────────────────────────────────────────────────────────────────────────────
# QCC
# ─────────────────────────────────────────────────────────────────────────────
class QccMemberIn(BaseModel):
    userId: str
    memberRole: QccMemberRole = "MEMBER"


class QccTeamCreate(BaseModel):
    plantId: str
    areaId: str | None = None
    name: str = Field(min_length=3, max_length=120)
    department: str | None = None
    motto: str | None = None
    leaderId: str | None = None
    facilitatorId: str | None = None
    formedOn: datetime | None = None
    members: list[QccMemberIn] = []


class QccTeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=120)
    department: str | None = None
    motto: str | None = None
    leaderId: str | None = None
    facilitatorId: str | None = None
    areaId: str | None = None
    status: QccTeamStatus | None = None
    disbandedOn: datetime | None = None


class QccMemberOut(_Base):
    id: str
    user: UserRef | None = None
    userId: str
    memberRole: str
    joinedAt: datetime
    leftAt: datetime | None


class QccTeamListItem(_Base):
    id: str
    teamNo: str | None
    name: str
    department: str | None
    status: str
    plantId: str
    siteName: str | None
    areaName: str | None
    leader: UserRef | None = None
    facilitator: UserRef | None = None
    memberCount: int = 0
    activeProjects: int = 0
    closedProjects: int = 0
    formedOn: datetime | None
    createdAt: datetime


class QccTeamListResponse(BaseModel):
    items: list[QccTeamListItem]
    total: int
    statusCounts: dict[str, int] = {}


class QccTeamOut(QccTeamListItem):
    motto: str | None
    disbandedOn: datetime | None
    members: list[QccMemberOut] = []
    updatedAt: datetime


class QccProjectCreate(BaseModel):
    teamId: str
    plantId: str
    areaId: str | None = None
    title: str = Field(min_length=4, max_length=200)
    category: BeCategory
    problemStatement: str = Field(min_length=10)
    selectionRationale: str | None = None
    priorityScore: float | None = Field(default=None, ge=0)
    methodology: QccMethodology = "DMAIC"
    scope: str | None = None
    baselineMetric: str | None = None
    baselineValue: float | None = None
    targetValue: float | None = None
    metricUnit: str | None = None
    targetDate: datetime | None = None

    @model_validator(mode="after")
    def _target_needs_a_baseline(self) -> "QccProjectCreate":
        # §6's charter is "baseline metric, target metric". A target with no
        # baseline cannot be measured against anything, and the benefit that
        # comes out at the end would be unfalsifiable.
        if self.targetValue is not None and self.baselineValue is None:
            raise ValueError(
                "A target value needs a baseline value to be measured against."
            )
        return self


class QccProjectUpdate(BaseModel):
    areaId: str | None = None
    title: str | None = Field(default=None, min_length=4, max_length=200)
    category: BeCategory | None = None
    problemStatement: str | None = Field(default=None, min_length=10)
    selectionRationale: str | None = None
    priorityScore: float | None = Field(default=None, ge=0)
    scope: str | None = None
    baselineMetric: str | None = None
    baselineValue: float | None = None
    targetValue: float | None = None
    metricUnit: str | None = None
    targetDate: datetime | None = None
    actualValue: float | None = None
    # `methodology` is absent on purpose: changing it after the charter would
    # orphan the stage rows already materialised from it, and a project halfway
    # through DMAIC cannot be retconned into PDCA.


class QccStageUpdate(BaseModel):
    summary: str | None = None
    targetDate: datetime | None = None
    status: QccStageStatus | None = None


class QccStageSignOff(BaseModel):
    note: str | None = None


class QccStageOut(_Base):
    id: str
    stage: str
    sequence: int
    status: str
    summary: str | None
    startedAt: datetime | None
    targetDate: datetime | None
    signedOffBy: UserRef | None = None
    signedOffAt: datetime | None
    signOffNote: str | None
    #: Why this gate cannot be signed off yet — empty means it can. Served so the
    #: UI can disable the button AND say why, rather than showing a dead control.
    signOffBlockers: list[str] = []


class QccRcaRequest(BaseModel):
    methodology: str = "FIVE_WHY"


class QccEvaluation(BaseModel):
    """§6 evaluation — a configurable rubric field, per the agreed scope.
    Convention/competition management is explicitly out of scope."""

    score: float = Field(ge=0)
    rubric: dict[str, Any] | None = None
    presentedAt: datetime | None = None
    presentationRef: str | None = None


class QccProjectTransition(BaseModel):
    note: str | None = None


class QccProjectListItem(_Base):
    id: str
    projectNo: str | None
    title: str
    category: str
    status: str
    plantId: str
    siteName: str | None
    areaName: str | None
    teamId: str
    teamName: str | None = None
    methodology: str
    rag: str = "GREEN"
    currentStage: str | None = None
    signedOffStages: int = 0
    totalStages: int = 0
    stagePercent: float | None = None
    baselineValue: float | None
    targetValue: float | None
    actualValue: float | None
    metricUnit: str | None
    targetDate: datetime | None
    createdAt: datetime


class QccProjectListResponse(BaseModel):
    items: list[QccProjectListItem]
    total: int
    statusCounts: dict[str, int] = {}


class QccProjectOut(QccProjectListItem):
    problemStatement: str
    selectionRationale: str | None
    priorityScore: float | None
    scope: str | None
    baselineMetric: str | None
    charteredAt: datetime | None
    #: The SHARED RootCauseAnalysis id. There is no RCA content in this payload
    #: and no RCA table under QCC — §6 requires the platform engine.
    rcaId: str | None
    rcaStage: str | None = None
    evaluationScore: float | None
    evaluationRubric: dict | None
    evaluatedBy: UserRef | None = None
    evaluatedAt: datetime | None
    presentedAt: datetime | None
    presentationRef: str | None
    completedAt: datetime | None
    closedAt: datetime | None
    rejectionReason: str | None
    workflowInstanceId: str | None
    createdBy: UserRef | None = None
    updatedAt: datetime
    stages: list[QccStageOut] = []
    benefits: list["BenefitOut"] = []
    benefitSummary: dict[str, Any] = {}
    availableActions: list[str] = []
    #: Why a transition the caller holds the permission for would still fail.
    #: Keyed by target status.
    transitionBlockers: dict[str, list[str]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# SIP
# ─────────────────────────────────────────────────────────────────────────────
class SipMilestoneIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = None
    sequence: int = 0
    ownerId: str | None = None
    plannedDate: datetime | None = None


class SipCreate(BaseModel):
    plantId: str
    areaId: str | None = None
    department: str | None = None
    title: str = Field(min_length=4, max_length=200)
    category: BeCategory
    scope: str = Field(min_length=10)
    problemStatement: str | None = None
    sponsorId: str | None = None
    ownerId: str | None = None
    metricName: str | None = None
    metricUnit: str | None = None
    baselineValue: float | None = None
    targetValue: float | None = None
    startDate: datetime | None = None
    targetDate: datetime | None = None
    feasibilityScore: float | None = Field(default=None, ge=0, le=10)
    impactScore: float | None = Field(default=None, ge=0, le=10)
    currency: str = "INR"
    investmentCost: float | None = Field(default=None, ge=0)
    milestones: list[SipMilestoneIn] = []

    @model_validator(mode="after")
    def _sponsor_is_not_the_owner(self) -> "SipCreate":
        # §7 names both roles separately and they do different jobs: the sponsor
        # authorises and unblocks, the owner runs it. A SIP where they are the
        # same person is a Kaizen with a longer form — and it removes the
        # independent voice the benefit sign-off later depends on.
        if self.sponsorId and self.ownerId and self.sponsorId == self.ownerId:
            raise ValueError(
                "The sponsor and the project owner must be different people."
            )
        return self

    @model_validator(mode="after")
    def _target_needs_a_baseline(self) -> "SipCreate":
        if self.targetValue is not None and self.baselineValue is None:
            raise ValueError(
                "A target value needs a baseline value to be measured against."
            )
        return self


class SipUpdate(BaseModel):
    areaId: str | None = None
    department: str | None = None
    title: str | None = Field(default=None, min_length=4, max_length=200)
    category: BeCategory | None = None
    scope: str | None = Field(default=None, min_length=10)
    problemStatement: str | None = None
    sponsorId: str | None = None
    ownerId: str | None = None
    metricName: str | None = None
    metricUnit: str | None = None
    baselineValue: float | None = None
    targetValue: float | None = None
    startDate: datetime | None = None
    targetDate: datetime | None = None
    feasibilityScore: float | None = Field(default=None, ge=0, le=10)
    impactScore: float | None = Field(default=None, ge=0, le=10)
    currency: str | None = None
    investmentCost: float | None = Field(default=None, ge=0)
    lessonsLearned: str | None = None
    holdReason: str | None = None


class SipMilestoneUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = None
    sequence: int | None = None
    ownerId: str | None = None
    revisedDate: datetime | None = None
    actualDate: datetime | None = None
    status: MilestoneStatus | None = None
    progressPercent: int | None = Field(default=None, ge=0, le=100)
    note: str | None = None
    # `plannedDate` is absent by design. A tracker that lets the plan follow the
    # actuals always reports green — slippage is recorded on `revisedDate`, and
    # the original commitment stays visible.


class SipTransition(BaseModel):
    note: str | None = None


class SipMilestoneOut(_Base):
    id: str
    name: str
    description: str | None
    sequence: int
    owner: UserRef | None = None
    plannedDate: datetime | None
    revisedDate: datetime | None
    actualDate: datetime | None
    status: str
    progressPercent: int | None
    note: str | None
    isLate: bool = False
    #: Days between the original plan and where it stands now. Positive means
    #: late. Null when there was never a planned date to slip from.
    slipDays: int | None = None


class SipListItem(_Base):
    id: str
    sipNo: str | None
    title: str
    category: str
    status: str
    plantId: str
    siteName: str | None
    areaName: str | None
    department: str | None
    sponsor: UserRef | None = None
    owner: UserRef | None = None
    rag: str = "GREEN"
    metricName: str | None
    metricUnit: str | None
    baselineValue: float | None
    targetValue: float | None
    latestActualValue: float | None
    metricPercent: float | None = None
    milestoneTotal: int = 0
    milestoneCompleted: int = 0
    milestoneLate: int = 0
    startDate: datetime | None
    targetDate: datetime | None
    priorityScore: float | None
    createdAt: datetime


class SipListResponse(BaseModel):
    items: list[SipListItem]
    total: int
    statusCounts: dict[str, int] = {}
    ragCounts: dict[str, int] = {}


class SipOut(SipListItem):
    scope: str
    problemStatement: str | None
    latestReadingAt: datetime | None
    feasibilityScore: float | None
    impactScore: float | None
    currency: str
    investmentCost: float | None
    lessonsLearned: str | None
    lessonsLearnedOplId: str | None
    rcaId: str | None
    completedAt: datetime | None
    closedAt: datetime | None
    holdReason: str | None
    rejectionReason: str | None
    workflowInstanceId: str | None
    createdBy: UserRef | None = None
    updatedAt: datetime
    milestones: list[SipMilestoneOut] = []
    benefits: list["BenefitOut"] = []
    benefitSummary: dict[str, Any] = {}
    metricProgress: dict[str, Any] = {}
    availableActions: list[str] = []
    transitionBlockers: dict[str, list[str]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Benefit realisation
# ─────────────────────────────────────────────────────────────────────────────
class BenefitCreate(BaseModel):
    sourceType: BenefitSourceType
    sourceId: str
    benefitType: BenefitType
    valueKind: BenefitValueKind = "FINANCIAL"
    currency: str | None = None
    unit: str | None = None
    projectedValue: float | None = None
    annualisedValue: float | None = None
    validationWindowMonths: ValidationWindow | None = None
    validatingAuthorityId: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _kind_matches_its_measure(self) -> "BenefitCreate":
        # A financial benefit in "ppm" and a non-financial one in "INR" both
        # corrupt the rollup, which sums FINANCIAL lines and counts the rest.
        if self.valueKind == "FINANCIAL" and self.unit:
            raise ValueError("A financial benefit carries a currency, not a unit.")
        if self.valueKind == "NON_FINANCIAL":
            if self.currency:
                raise ValueError(
                    "A non-financial benefit carries a unit, not a currency."
                )
            if not self.unit:
                raise ValueError(
                    "A non-financial benefit needs a unit — a bare number cannot "
                    "be reported."
                )
        return self


class BenefitUpdate(BaseModel):
    benefitType: BenefitType | None = None
    currency: str | None = None
    unit: str | None = None
    projectedValue: float | None = None
    annualisedValue: float | None = None
    validationWindowMonths: ValidationWindow | None = None
    validatingAuthorityId: str | None = None
    note: str | None = None
    # `valueKind`, `sourceType` and `sourceId` are absent: changing any of them
    # turns this row into a different benefit on a different record, which is a
    # new line, not an edit.


class BenefitClaim(BaseModel):
    """Submit the realised figure for validation."""

    realizedValue: float
    note: str | None = None


class BenefitValidate(BaseModel):
    accept: bool = True
    realizedValue: float | None = None
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _rejection_needs_a_reason(cls, v: str | None, info: Any) -> str | None:
        if info.data.get("accept") is False and not (v or "").strip():
            raise ValueError("Say why the benefit was not accepted.")
        return v


class BenefitReadingCreate(BaseModel):
    actualValue: float
    targetValue: float | None = None
    periodLabel: str | None = Field(default=None, max_length=40)
    note: str | None = None


class BenefitReadingOut(_Base):
    id: str
    readingAt: datetime
    periodLabel: str | None
    actualValue: float
    targetValue: float | None
    note: str | None
    recordedBy: UserRef | None = None


class BenefitOut(_Base):
    id: str
    plantId: str
    siteName: str | None
    sourceType: str
    sourceId: str
    sourceRef: str | None
    benefitType: str
    valueKind: str
    currency: str | None
    unit: str | None
    projectedValue: float | None
    realizedValue: float | None
    annualisedValue: float | None
    validationWindowMonths: int | None
    validationDueAt: datetime | None
    validatingAuthority: UserRef | None = None
    validatedBy: UserRef | None = None
    validatedAt: datetime | None
    validationNote: str | None
    status: str
    note: str | None
    isValidationDue: bool = False
    createdBy: UserRef | None = None
    createdAt: datetime
    updatedAt: datetime
    readings: list[BenefitReadingOut] = []
    #: Why the CALLER may not validate this line right now. Empty means they may.
    #: Computed per-caller, so two people see different answers on the same row —
    #: which is exactly what a separation-of-duties rule means.
    validationBlockers: list[str] = []


class BenefitListResponse(BaseModel):
    items: list[BenefitOut]
    total: int
    summary: dict[str, Any] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Cross-workflow dashboard
# ─────────────────────────────────────────────────────────────────────────────
class WorkflowSummary(BaseModel):
    """One row of the §8 consolidated dashboard, per workflow type."""

    workflowType: str
    total: int = 0
    open: int = 0
    closed: int = 0
    rejected: int = 0
    #: Accepted / approved as a share of everything that reached a decision.
    #: Null rather than 0 when nothing has been decided yet — a 0% conversion
    #: badge on a scheme that launched last week reads as a failure that has
    #: not happened.
    conversionRate: float | None = None
    #: Mean days from submission to closure, over CLOSED records only.
    avgCycleTimeDays: float | None = None
    overdue: int = 0


class BeDashboardOut(BaseModel):
    workflows: list[WorkflowSummary] = []
    benefit: dict[str, Any] = {}
    #: Team leaderboard (§6). Empty when no circle has closed a project.
    qccLeaderboard: list[dict[str, Any]] = []
    #: SIP portfolio counts by RAG (§7).
    sipPortfolio: dict[str, int] = {}
    generatedAt: datetime


QccProjectOut.model_rebuild()
SipOut.model_rebuild()


__all__ = [
    "BeDashboardOut",
    "BenefitClaim",
    "BenefitCreate",
    "BenefitListResponse",
    "BenefitOut",
    "BenefitReadingCreate",
    "BenefitReadingOut",
    "BenefitUpdate",
    "BenefitValidate",
    "P2MetaOut",
    "QccEvaluation",
    "QccMemberIn",
    "QccMemberOut",
    "QccProjectCreate",
    "QccProjectListItem",
    "QccProjectListResponse",
    "QccProjectOut",
    "QccProjectTransition",
    "QccProjectUpdate",
    "QccRcaRequest",
    "QccStageOut",
    "QccStageSignOff",
    "QccStageUpdate",
    "QccTeamCreate",
    "QccTeamListItem",
    "QccTeamListResponse",
    "QccTeamOut",
    "QccTeamUpdate",
    "SipCreate",
    "SipListItem",
    "SipListResponse",
    "SipMilestoneIn",
    "SipMilestoneOut",
    "SipMilestoneUpdate",
    "SipOut",
    "SipTransition",
    "SipUpdate",
    "SuggestionCreate",
    "SuggestionDecide",
    "SuggestionIncentive",
    "SuggestionListItem",
    "SuggestionListResponse",
    "SuggestionOut",
    "SuggestionReject",
    "SuggestionScreen",
    "SuggestionTransition",
    "SuggestionUpdate",
    "WorkflowSummary",
]
