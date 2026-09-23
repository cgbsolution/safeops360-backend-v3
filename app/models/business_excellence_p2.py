"""Business Excellence — Phase 2: Suggestion Scheme, QCC, SIP, and the shared
benefit-realisation layer.

Phase 1 (business_excellence.py) shipped three of the six workflows named in
BE_Module_Functional_Scope.docx: Kaizen, OPL and Poka Yoke. This module adds the
remaining three plus the one thing §6 and §7 both require and Phase 1 had no
home for — a benefit that is *projected* by the people doing the work and
*validated*, months later, by somebody else.

Same conventions as Phase 1, deliberately: hand-mirrored camelCase columns so
Prisma's naming matches, String vocabularies rather than Postgres enums (a value
outside a NATIVE enum aborts the whole transaction at flush time), FK-by-value
to pre-existing tables (User, Plant, Area) and real FKs only where both ends are
new. Physical DDL lives in prisma/apply-be-p2-ddl.ts.

WHY SUGGESTION IS ITS OWN REGISTER AND NOT THE KAIZEN FAST_TRACK LANE
Phase 1 called `KAIZEN_LANES = ("STANDARD", "FAST_TRACK")` "the Suggestion Scheme
lane — the record is identical; only the approval path differs". Against §3 of
the functional scope that is no longer true, and one field is what breaks it:
**anonymous submission**. `BeKaizen.createdById` is NOT NULL and the register
renders `raisedBy` on every row; there is no way to serve an anonymous record out
of it without either lying about the column or special-casing every payload that
reads it. Three more differences follow §3 rather than §2 — a two-stage decision
(triage relevant/not-relevant/duplicate, *then* accept/reject/defer), a DEFERRED
state Kaizen has no analogue for, and incentive tracking that is explicitly
"tied to accepted and implemented suggestions".

The FAST_TRACK lane stays exactly as it is. It is live in prod with its own
workflow definition (BE_KAIZEN/FAST_TRACK) and it is still useful — it is a
Kaizen a supervisor can approve without convening the committee. It is simply
not the Suggestion Scheme. Phase 1's docstring on that constant is now wrong and
has been corrected in place.

WHY QCC'S ANALYZE STAGE HOLDS AN rcaId AND NOT AN RCA
§6: "Root-cause analysis stage draws on the platform's shared RCA engine — no
separate RCA tool or duplicate record." `BeQccProject.rcaId` points at a
`RootCauseAnalysis` row created through services/business_excellence_p2.py's
`ensure_project_rca()`, which goes through the same `create_problem_rca` path
Phase 1 added for BE. There is no cause, no 5-why and no fishbone column
anywhere in this file, and the ANALYZE stage refuses to sign off without that id.

WHY RAG IS COMPUTED AND NEVER STORED
§6 and §7 both want a Red/Amber/Green rollup. A stored RAG needs a job to
maintain it and is wrong for however long that job is broken — the Signal Engine
emitted nothing for five days while its job "ran" nightly. `project_rag()` and
`sip_rag()` in the service derive it from milestone slippage and the target date
at read time, the same way `is_ack_overdue()` does.
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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin, SoftDeleteMixin

# ─── Suggestion Scheme (§3) ──────────────────────────────────────────────────

#: Deliberately the SAME tuple Kaizen validates against, imported rather than
#: re-declared. §8 requires "a common benefit-type taxonomy … maintained once and
#: reused across all six workflows"; two category lists that drift is precisely
#: the failure that requirement exists to prevent.
from app.models.business_excellence import KAIZEN_CATEGORIES as BE_CATEGORIES

SUGGESTION_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "SUBMITTED",
    "SCREENING",
    "ACCEPTED",
    "DEFERRED",
    "IN_IMPLEMENTATION",
    "IMPLEMENTED",
    "CLOSED",
    "REJECTED",
    "DUPLICATE",
)

SUGGESTION_OPEN_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "SUBMITTED",
    "SCREENING",
    "ACCEPTED",
    "DEFERRED",
    "IN_IMPLEMENTATION",
    "IMPLEMENTED",
)

#: Stage one of §3's two-stage decision — the triage a screening committee does
#: before it debates anything. Kept separate from the decision below because a
#: suggestion found NOT_RELEVANT was never rejected on its merits, and reporting
#: that conflates the two overstates how much the committee turned down.
SCREENING_OUTCOMES: tuple[str, ...] = ("RELEVANT", "NOT_RELEVANT", "DUPLICATE")

#: Stage two. DEFER is a real outcome, not a synonym for reject: §3 requires it,
#: and a scheme with no way to say "good idea, not this year" either rejects
#: those ideas or leaves them open forever pretending they are being worked on.
SUGGESTION_DECISIONS: tuple[str, ...] = ("ACCEPT", "REJECT", "DEFER")

#: §3 "configurable incentive/reward tracking tied to accepted and implemented
#: suggestions". Tracked, never paid — payroll/rewards integration is explicitly
#: out of scope, so this register records the entitlement and stops there.
INCENTIVE_STATUSES: tuple[str, ...] = (
    "NOT_APPLICABLE",
    "PROPOSED",
    "APPROVED",
    "PAID",
)


# ─── Quality Circle (§6) ─────────────────────────────────────────────────────

QCC_TEAM_STATUSES: tuple[str, ...] = ("FORMING", "ACTIVE", "DORMANT", "DISBANDED")

#: §6 "Define → Measure → Analyze → Improve → Control, or an equivalent PDCA
#: structure configurable per RRL's preferred methodology". The methodology is
#: chosen per project and the stage rows are materialised from it at charter
#: time, so a circle running PDCA never sees a DMAIC gate.
QCC_METHODOLOGIES: tuple[str, ...] = ("DMAIC", "PDCA")

QCC_DMAIC_STAGES: tuple[str, ...] = ("DEFINE", "MEASURE", "ANALYZE", "IMPROVE", "CONTROL")
QCC_PDCA_STAGES: tuple[str, ...] = ("PLAN", "DO", "CHECK", "ACT")

QCC_STAGES_BY_METHODOLOGY: dict[str, tuple[str, ...]] = {
    "DMAIC": QCC_DMAIC_STAGES,
    "PDCA": QCC_PDCA_STAGES,
}

#: The stage that must carry an RCA before it can be signed off, per methodology.
#: PDCA's PLAN is where a circle does its cause analysis; DMAIC's is ANALYZE.
QCC_RCA_STAGE: dict[str, str] = {"DMAIC": "ANALYZE", "PDCA": "PLAN"}

QCC_STAGE_STATUSES: tuple[str, ...] = ("NOT_STARTED", "IN_PROGRESS", "SIGNED_OFF", "SKIPPED")

QCC_PROJECT_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "CHARTERED",
    "IN_PROGRESS",
    "COMPLETED",
    "BENEFIT_VALIDATION",
    "CLOSED",
    "ABANDONED",
    "REJECTED",
)

QCC_PROJECT_OPEN_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "CHARTERED",
    "IN_PROGRESS",
    "COMPLETED",
    "BENEFIT_VALIDATION",
)

QCC_MEMBER_ROLES: tuple[str, ...] = ("LEADER", "FACILITATOR", "MEMBER", "DEPUTY_LEADER")


# ─── Structured Improvement Project (§7) ─────────────────────────────────────

SIP_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "SUBMITTED",
    "UNDER_REVIEW",
    "APPROVED",
    "IN_PROGRESS",
    "ON_HOLD",
    "COMPLETED",
    "BENEFIT_VALIDATION",
    "CLOSED",
    "REJECTED",
    "CANCELLED",
)

SIP_OPEN_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "SUBMITTED",
    "UNDER_REVIEW",
    "APPROVED",
    "IN_PROGRESS",
    "ON_HOLD",
    "COMPLETED",
    "BENEFIT_VALIDATION",
)

MILESTONE_STATUSES: tuple[str, ...] = (
    "NOT_STARTED",
    "IN_PROGRESS",
    "COMPLETED",
    "DELAYED",
    "CANCELLED",
)

#: Computed, never stored. See the module docstring.
RAG_STATUSES: tuple[str, ...] = ("GREEN", "AMBER", "RED")


# ─── Shared benefit realisation (§2, §6, §7, §8) ─────────────────────────────

#: §8's "common benefit-type taxonomy (safety / quality / cost / productivity /
#: environment)", widened by the two axes §6 names explicitly.
BENEFIT_TYPES: tuple[str, ...] = (
    "COST_SAVING",
    "QUALITY_IMPROVEMENT",
    "PRODUCTIVITY_GAIN",
    "SAFETY_IMPROVEMENT",
    "DELIVERY_IMPROVEMENT",
    "ENVIRONMENT",
    "OTHER",
)

#: §7 "Financial and non-financial benefit capture". A non-financial benefit has
#: a unit (ppm, minutes, kWh) rather than a currency, and summing the two into
#: one "cumulative benefit realised" figure is how a BE dashboard ends up
#: reporting a number with no meaning.
BENEFIT_VALUE_KINDS: tuple[str, ...] = ("FINANCIAL", "NON_FINANCIAL")

BENEFIT_STATUSES: tuple[str, ...] = (
    "PROJECTED",
    "PENDING_VALIDATION",
    "VALIDATED",
    "REJECTED",
    "LAPSED",
)

#: Which register a benefit belongs to. POKA_YOKE and KAIZEN are included so the
#: cross-workflow dashboard in §8 has one table to sum rather than five.
BENEFIT_SOURCE_TYPES: tuple[str, ...] = (
    "KAIZEN",
    "SUGGESTION",
    "OPL",
    "POKA_YOKE",
    "QCC",
    "SIP",
)

#: §6 "a 3/6/12-month follow-up check, configurable". Offered as a vocabulary
#: rather than hard-coded so a plant can run a 1-month window on a fast line.
VALIDATION_WINDOW_MONTHS: tuple[int, ...] = (1, 3, 6, 12)

#: Days per validation window. Calendar-month arithmetic would need dateutil,
#: which this backend does not carry; a 30-day month is close enough for a
#: follow-up reminder and is what every other due-date in this module uses.
VALIDATION_WINDOW_DAYS: dict[int, int] = {1: 30, 3: 91, 6: 182, 12: 365}


# ─────────────────────────────────────────────────────────────────────────────
# Suggestion Scheme
# ─────────────────────────────────────────────────────────────────────────────
class BeSuggestion(Base, IdMixin, SoftDeleteMixin):
    """One employee suggestion, from raised to implemented or declined.

    ANONYMITY IS A RENDERING RULE, NOT A MISSING COLUMN
    `createdById` is NOT NULL even when `isAnonymous` is set, and every payload
    in the router suppresses it when that flag is on. This is a deliberate
    trade-off between two requirements that genuinely conflict: §3 wants an
    anonymous submission option, while §8 wants an immutable audit trail of
    "every submission, review, approval, rejection and status change", and §3
    itself wants the decision rationale "visible to the submitter" — which is
    impossible if nobody knows who the submitter is.

    So the identity is stored and withheld. `submitter_ref()` in the service is
    the ONLY place allowed to decide whether it may be shown, and it answers no
    for everyone except the submitter themselves. That is weaker than true
    anonymity and the UI says so on the form rather than implying otherwise —
    a scheme that promises anonymity it cannot deliver is worse than one that is
    honest about being confidential-but-attributable.
    """

    __tablename__ = "BeSuggestion"

    suggestionNo: Mapped[str | None] = mapped_column(String, index=True)

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    areaName: Mapped[str | None] = mapped_column(String)

    # §3 is explicit that a suggestion is "broader in scope than Kaizen, and not
    # limited to process-floor issues", so there is no lineOrMachine / processStep
    # here. A suggestion about the canteen rota is a valid suggestion.
    title: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    expectedBenefit: Mapped[str | None] = mapped_column(Text)

    isAnonymous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    # ── Stage one: triage ──
    screeningOutcome: Mapped[str | None] = mapped_column(String, index=True)
    screeningNote: Mapped[str | None] = mapped_column(Text)
    screenedById: Mapped[str | None] = mapped_column(String)
    screenedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when triage found this to be a restatement of an existing suggestion.
    duplicateOfSuggestionId: Mapped[str | None] = mapped_column(String, index=True)

    # ── Stage two: decision ──
    decision: Mapped[str | None] = mapped_column(String, index=True)
    # §3: "Decision rationale captured against every outcome, visible to the
    # submitter." Required by the API on every decision, including ACCEPT — a
    # scheme that explains its rejections but not its acceptances teaches people
    # nothing about what a good suggestion looks like.
    decisionRationale: Mapped[str | None] = mapped_column(Text)
    decidedById: Mapped[str | None] = mapped_column(String)
    decidedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Where a DEFER decision parks it until.
    deferredUntil: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # ── Implementation ──
    ownerId: Mapped[str | None] = mapped_column(String, index=True)
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    implementedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    implementationNote: Mapped[str | None] = mapped_column(Text)

    # ── Incentive (tracked only — no payout integration, per scope) ──
    incentiveStatus: Mapped[str] = mapped_column(
        String, nullable=False, default="NOT_APPLICABLE", index=True
    )
    incentivePoints: Mapped[int | None] = mapped_column(Integer)
    incentiveAmount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="INR")
    incentiveNote: Mapped[str | None] = mapped_column(Text)
    incentiveApprovedById: Mapped[str | None] = mapped_column(String)
    incentiveApprovedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)
    workflowInstanceId: Mapped[str | None] = mapped_column(String, index=True)
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set when an accepted suggestion was escalated into a full Kaizen because it
    # turned out to need one. The link is what stops the same idea being counted
    # twice on the cross-workflow dashboard.
    convertedToKaizenId: Mapped[str | None] = mapped_column(String, index=True)

    sourceModule: Mapped[str | None] = mapped_column(String, index=True)
    sourceRecordId: Mapped[str | None] = mapped_column(String, index=True)
    sourceRecordRef: Mapped[str | None] = mapped_column(String)

    createdById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    updatedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # MUST be default=, not server_default= — Prisma's @updatedAt is
    # client-managed and the column carries no DB default. See _base.py.
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_BeSuggestion_plant_status", "plantId", "status"),
        Index("ix_BeSuggestion_plant_created", "plantId", "createdAt"),
        Index("ix_BeSuggestion_source", "sourceModule", "sourceRecordId"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Quality Circle — team, membership, project, stage gates
# ─────────────────────────────────────────────────────────────────────────────
class BeQccTeam(Base, IdMixin, SoftDeleteMixin):
    """A standing quality circle.

    §6 wants "team-level history retained across multiple projects, supporting a
    running team leaderboard", which is the whole reason the team is a row rather
    than a field on the project. A circle that completed four projects over three
    years is the unit the leaderboard ranks; re-keying its name on every project
    would make that history unrecoverable.
    """

    __tablename__ = "BeQccTeam"

    teamNo: Mapped[str | None] = mapped_column(String, index=True)

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    areaName: Mapped[str | None] = mapped_column(String)

    name: Mapped[str] = mapped_column(String, nullable=False)
    department: Mapped[str | None] = mapped_column(String, index=True)
    motto: Mapped[str | None] = mapped_column(Text)

    # The two named offices §6 requires. Both are also carried as membership rows
    # so "who was in this circle" has one answer, but they are pinned here too
    # because a circle without a leader is not a circle and the register has to
    # be able to say who to call.
    leaderId: Mapped[str | None] = mapped_column(String, index=True)
    facilitatorId: Mapped[str | None] = mapped_column(String, index=True)

    formedOn: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disbandedOn: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, nullable=False, default="FORMING", index=True)

    createdById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    updatedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    members: Mapped[list["BeQccTeamMember"]] = relationship(
        back_populates="team", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        Index("ix_BeQccTeam_plant_status", "plantId", "status"),
        Index("ix_BeQccTeam_plant_created", "plantId", "createdAt"),
    )


class BeQccTeamMember(Base, IdMixin):
    """One person's membership of one circle, over a period.

    `leftAt` rather than a delete, because §6's leaderboard and the separation-of-
    duties check on benefit validation both need to know who was in the circle
    *at the time* — a member who left last month must still block themselves from
    validating the benefit of a project they worked on.
    """

    __tablename__ = "BeQccTeamMember"

    teamId: Mapped[str] = mapped_column(
        ForeignKey("BeQccTeam.id", ondelete="CASCADE"), nullable=False, index=True
    )
    userId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    memberRole: Mapped[str] = mapped_column(String, nullable=False, default="MEMBER")

    joinedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    leftAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    team: Mapped[BeQccTeam] = relationship(back_populates="members", lazy="raise")

    __table_args__ = (
        # One ACTIVE membership per person per circle. Partial-unique on leftAt
        # IS NULL is expressed in the DDL script, which Prisma cannot declare —
        # a person may rejoin a circle they previously left.
        Index("ix_BeQccMember_team_user", "teamId", "userId"),
        Index("ix_BeQccMember_user", "userId"),
    )


class BeQccProject(Base, IdMixin, SoftDeleteMixin):
    """One stage-gated circle project, from charter to validated benefit."""

    __tablename__ = "BeQccProject"

    projectNo: Mapped[str | None] = mapped_column(String, index=True)

    teamId: Mapped[str] = mapped_column(
        ForeignKey("BeQccTeam.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    areaName: Mapped[str | None] = mapped_column(String)

    title: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    problemStatement: Mapped[str] = mapped_column(Text, nullable=False)

    # ── §6 "Project Selection & Charter" ──
    selectionRationale: Mapped[str | None] = mapped_column(Text)
    # Priority score against other candidate problems. A plain float rather than
    # a rubric table: §6 asks for "priority scoring", and the weights a circle
    # uses to arrive at the number are theirs, not the platform's.
    priorityScore: Mapped[float | None] = mapped_column(Float)
    scope: Mapped[str | None] = mapped_column(Text)
    baselineMetric: Mapped[str | None] = mapped_column(String)
    baselineValue: Mapped[float | None] = mapped_column(Float)
    targetValue: Mapped[float | None] = mapped_column(Float)
    metricUnit: Mapped[str | None] = mapped_column(String)
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    charteredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    methodology: Mapped[str] = mapped_column(String, nullable=False, default="DMAIC")

    # ── The shared RCA, not a local one. See the module docstring. ──
    rcaId: Mapped[str | None] = mapped_column(String, index=True)

    # ── §6 "Evaluation" — a configurable rubric field, per scope. Convention
    #    and competition management is explicitly out of scope, so this is a
    #    score and a blob of whatever the rubric was, not an event module. ──
    evaluationRubric: Mapped[dict | None] = mapped_column(JSONB)
    evaluationScore: Mapped[float | None] = mapped_column(Float)
    evaluatedById: Mapped[str | None] = mapped_column(String)
    evaluatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    presentedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    presentationRef: Mapped[str | None] = mapped_column(String)

    actualValue: Mapped[float | None] = mapped_column(Float)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)
    workflowInstanceId: Mapped[str | None] = mapped_column(String, index=True)
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sourceModule: Mapped[str | None] = mapped_column(String, index=True)
    sourceRecordId: Mapped[str | None] = mapped_column(String, index=True)
    sourceRecordRef: Mapped[str | None] = mapped_column(String)

    createdById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    updatedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    stages: Mapped[list["BeQccProjectStage"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        Index("ix_BeQccProject_plant_status", "plantId", "status"),
        Index("ix_BeQccProject_plant_created", "plantId", "createdAt"),
        Index("ix_BeQccProject_team", "teamId", "status"),
    )


class BeQccProjectStage(Base, IdMixin):
    """One gate in a project's lifecycle, and the sign-off that opened the next.

    Materialised in full at charter time rather than created lazily, so the
    project board can render the whole path with the un-started gates greyed
    out — a board that only shows the stages already reached tells a circle
    nothing about what is coming.

    Append-only in spirit: `signedOffById`/`signedOffAt` are written once. Undoing
    a gate is a deliberate REOPEN action in the service that clears them and logs
    to the audit trail, not a silent overwrite.
    """

    __tablename__ = "BeQccProjectStage"

    projectId: Mapped[str] = mapped_column(
        ForeignKey("BeQccProject.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    stage: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Position in the methodology's sequence. Held rather than derived so a
    # future methodology with a different order does not need a code change to
    # sort the board.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String, nullable=False, default="NOT_STARTED", index=True
    )
    summary: Mapped[str | None] = mapped_column(Text)

    startedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signedOffById: Mapped[str | None] = mapped_column(String, index=True)
    signedOffAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signOffNote: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    project: Mapped[BeQccProject] = relationship(back_populates="stages", lazy="raise")

    __table_args__ = (
        UniqueConstraint("projectId", "stage", name="uq_BeQccStage_project_stage"),
        Index("ix_BeQccStage_project_seq", "projectId", "sequence"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Structured Improvement Project
# ─────────────────────────────────────────────────────────────────────────────
class BeSip(Base, IdMixin, SoftDeleteMixin):
    """A sponsor-backed improvement project with a tracked target metric.

    The difference from a Kaizen is the sponsor and the metric, and both are
    modelled as such: `sponsorId` is what makes a SIP fundable, and
    baseline/target/actual is what makes it measurable. §7 wants actual-vs-target
    "over the life of the project, not just at closure", which is why the readings
    live in BeBenefitReading and this row carries only the latest.
    """

    __tablename__ = "BeSip"

    sipNo: Mapped[str | None] = mapped_column(String, index=True)

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    areaName: Mapped[str | None] = mapped_column(String)
    department: Mapped[str | None] = mapped_column(String, index=True)

    title: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    problemStatement: Mapped[str | None] = mapped_column(Text)

    # §7 "sponsor, project owner". Two different people by design: the sponsor
    # authorises and unblocks, the owner runs it. A SIP where they are the same
    # person is a Kaizen with a longer form.
    sponsorId: Mapped[str | None] = mapped_column(String, index=True)
    ownerId: Mapped[str | None] = mapped_column(String, index=True)

    metricName: Mapped[str | None] = mapped_column(String)
    metricUnit: Mapped[str | None] = mapped_column(String)
    baselineValue: Mapped[float | None] = mapped_column(Float)
    targetValue: Mapped[float | None] = mapped_column(Float)
    # The most recent reading, denormalised off BeBenefitReading so the portfolio
    # list does not need a correlated subquery per row. The readings table stays
    # the source of truth; this is a cache and the service writes both together.
    latestActualValue: Mapped[float | None] = mapped_column(Float)
    latestReadingAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    startDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── §7 "Feasibility and impact scoring used to prioritize competing SIPs" ──
    feasibilityScore: Mapped[float | None] = mapped_column(Float)
    impactScore: Mapped[float | None] = mapped_column(Float)
    priorityScore: Mapped[float | None] = mapped_column(Float, index=True)

    # §7 closure: "a lessons-learned note captured back into the shared
    # repository". The note is held here; publishing it as an OPL is an explicit
    # action that sets `lessonsLearnedOplId`, because not every SIP produces a
    # lesson worth putting on the floor.
    lessonsLearned: Mapped[str | None] = mapped_column(Text)
    lessonsLearnedOplId: Mapped[str | None] = mapped_column(String, index=True)

    currency: Mapped[str] = mapped_column(String, nullable=False, default="INR")
    investmentCost: Mapped[float | None] = mapped_column(Float)

    rcaId: Mapped[str | None] = mapped_column(String, index=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)
    workflowInstanceId: Mapped[str | None] = mapped_column(String, index=True)
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    holdReason: Mapped[str | None] = mapped_column(Text)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sourceModule: Mapped[str | None] = mapped_column(String, index=True)
    sourceRecordId: Mapped[str | None] = mapped_column(String, index=True)
    sourceRecordRef: Mapped[str | None] = mapped_column(String)

    createdById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    updatedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    milestones: Mapped[list["BeSipMilestone"]] = relationship(
        back_populates="sip", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        Index("ix_BeSip_plant_status", "plantId", "status"),
        Index("ix_BeSip_plant_created", "plantId", "createdAt"),
        Index("ix_BeSip_owner", "ownerId", "status"),
    )


class BeSipMilestone(Base, IdMixin):
    """One tracked deliverable on a SIP, with its own owner and dates.

    `plannedDate` is never overwritten once the project is approved — `actualDate`
    and `revisedDate` carry the movement instead. A milestone tracker that lets
    the plan follow the actuals always reports green, which is the single most
    common way a project-tracking tool becomes useless.
    """

    __tablename__ = "BeSipMilestone"

    sipId: Mapped[str] = mapped_column(
        ForeignKey("BeSip.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ownerId: Mapped[str | None] = mapped_column(String, index=True)
    plannedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revisedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(
        String, nullable=False, default="NOT_STARTED", index=True
    )
    progressPercent: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)

    createdById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    sip: Mapped[BeSip] = relationship(back_populates="milestones", lazy="raise")

    __table_args__ = (
        Index("ix_BeSipMilestone_sip_seq", "sipId", "sequence"),
        Index("ix_BeSipMilestone_due", "plantId", "plannedDate"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared benefit realisation
# ─────────────────────────────────────────────────────────────────────────────
class BeBenefit(Base, IdMixin, SoftDeleteMixin):
    """A claimed improvement benefit, and the evidence somebody independent
    agreed it was real.

    ONE TABLE FOR ALL SIX WORKFLOWS
    §8 requires "cumulative benefit realised across all six workflows" on one
    dashboard. Five per-register savings columns cannot be summed without five
    UNIONs that drift the moment one register adds a currency or a unit, so the
    benefit is its own row keyed by (sourceType, sourceId). Kaizen's existing
    `estimatedAnnualSaving` / `verifiedAnnualSaving` columns stay exactly where
    they are — they are live in prod — and the service mirrors them into a
    BeBenefit row so the rollup has one thing to read.

    PROJECTED AND REALIZED ARE WRITTEN BY DIFFERENT PEOPLE AT DIFFERENT TIMES
    The same reasoning Phase 1 applied to Kaizen's two savings columns, made
    structural: `projectedValue` is the claim, `realizedValue` is what the
    validation window actually found, and `validatedById` is enforced by the
    service to be somebody other than the submitter or — for a QCC project —
    anybody who was ever in the circle. §6: "separate from the circle's own
    reporting".
    """

    __tablename__ = "BeBenefit"

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)

    sourceType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sourceId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Human-readable reference frozen at write time, so a benefit line on the
    # executive dashboard names the record it came from without a join.
    sourceRef: Mapped[str | None] = mapped_column(String)

    benefitType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    valueKind: Mapped[str] = mapped_column(
        String, nullable=False, default="FINANCIAL", index=True
    )
    # FINANCIAL benefits carry a currency; NON_FINANCIAL ones carry a unit.
    # Exactly one is meaningful and the schema validates which.
    currency: Mapped[str | None] = mapped_column(String)
    unit: Mapped[str | None] = mapped_column(String)

    projectedValue: Mapped[float | None] = mapped_column(Float)
    realizedValue: Mapped[float | None] = mapped_column(Float)
    # Annualised equivalent, so a one-off saving and a per-month saving can share
    # a column on the rollup. Written by the caller, never inferred — guessing a
    # periodicity is how a benefit total inflates twelvefold.
    annualisedValue: Mapped[float | None] = mapped_column(Float)

    # ── §6's configurable follow-up window ──
    validationWindowMonths: Mapped[int | None] = mapped_column(Integer)
    validationDueAt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    validatedById: Mapped[str | None] = mapped_column(String, index=True)
    validatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validationNote: Mapped[str | None] = mapped_column(Text)
    # Who is expected to validate — §6 "a designated validating authority (e.g.
    # finance or plant operations)". Nullable: a plant that has not nominated one
    # still gets the window and the separation-of-duties rule.
    validatingAuthorityId: Mapped[str | None] = mapped_column(String, index=True)

    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PROJECTED", index=True
    )
    note: Mapped[str | None] = mapped_column(Text)

    createdById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    updatedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    readings: Mapped[list["BeBenefitReading"]] = relationship(
        back_populates="benefit", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        # One benefit line per (record, benefit type). A project that saved money
        # AND cut defects gets two rows, which is right; two COST_SAVING rows on
        # one project is double-counting and the index refuses it. Partial on
        # isDeleted is applied in the DDL script.
        Index("ix_BeBenefit_source", "sourceType", "sourceId"),
        Index("ix_BeBenefit_plant_status", "plantId", "status"),
        Index("ix_BeBenefit_due", "plantId", "validationDueAt"),
    )


class BeBenefitReading(Base, IdMixin):
    """One measurement of a benefit's metric at a point in time.

    §7: actual-vs-target "over the life of the project, not just at closure".
    Append-only — a reading is a measurement, and a measurement you can edit
    afterwards is an opinion. Corrections are a new reading with a note.
    """

    __tablename__ = "BeBenefitReading"

    benefitId: Mapped[str] = mapped_column(
        ForeignKey("BeBenefit.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    readingAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # The period this reading describes, when it is a monthly/quarterly figure
    # rather than a spot measurement. Free text ("2026-07", "FY26-Q2") because
    # plants report on calendars this platform does not model.
    periodLabel: Mapped[str | None] = mapped_column(String)

    actualValue: Mapped[float] = mapped_column(Float, nullable=False)
    targetValue: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)

    recordedById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    benefit: Mapped[BeBenefit] = relationship(back_populates="readings", lazy="raise")

    __table_args__ = (Index("ix_BeBenefitReading_benefit_at", "benefitId", "readingAt"),)


__all__ = [
    # Models
    "BeBenefit",
    "BeBenefitReading",
    "BeQccProject",
    "BeQccProjectStage",
    "BeQccTeam",
    "BeQccTeamMember",
    "BeSip",
    "BeSipMilestone",
    "BeSuggestion",
    # Vocabularies
    "BE_CATEGORIES",
    "BENEFIT_SOURCE_TYPES",
    "BENEFIT_STATUSES",
    "BENEFIT_TYPES",
    "BENEFIT_VALUE_KINDS",
    "INCENTIVE_STATUSES",
    "MILESTONE_STATUSES",
    "QCC_DMAIC_STAGES",
    "QCC_MEMBER_ROLES",
    "QCC_METHODOLOGIES",
    "QCC_PDCA_STAGES",
    "QCC_PROJECT_OPEN_STATUSES",
    "QCC_PROJECT_STATUSES",
    "QCC_RCA_STAGE",
    "QCC_STAGES_BY_METHODOLOGY",
    "QCC_STAGE_STATUSES",
    "QCC_TEAM_STATUSES",
    "RAG_STATUSES",
    "SCREENING_OUTCOMES",
    "SIP_OPEN_STATUSES",
    "SIP_STATUSES",
    "SUGGESTION_DECISIONS",
    "SUGGESTION_OPEN_STATUSES",
    "SUGGESTION_STATUSES",
    "VALIDATION_WINDOW_DAYS",
    "VALIDATION_WINDOW_MONTHS",
]
