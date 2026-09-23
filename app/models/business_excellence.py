"""Business Excellence — the shop-floor improvement registers (Phase 1).

Four registers on one module: Kaizen, One Point Lesson, Poka Yoke, and a BE view
of the existing RootCauseAnalysis. Hand-mirrored camelCase columns to match
Prisma's naming (same convention as loto.py / moc.py); Prisma owns the schema
declaration and prisma/apply-be-ddl.ts owns the physical DDL.

WHY THIS IS NOT THE FORM ENGINE
The Form Engine's own docstring names these four registers as its motivating
case, and for a pure "form + approval" module it would have been right. It is
not right here for three reasons that only show up once the module has to be
useful on a shop floor:

  * `FormRecord` has no record-to-record link. Kaizen → OPL → Poka Yoke → RCA is
    the whole value chain of a BE module; the form primitive cannot express it.
  * The analytics engine (services/analytics/specs.py) is generic over MODEL
    COLUMNS — date, status, owner. Records whose fields live in `dataJson` get
    no analytics, no signals and no register workspace.
  * OPL needs a per-person acknowledgement with a due date and an escalation.
    Nothing in the form engine models "this person has read this".

The Form Engine keeps its place for plant-specific variable content authored by
a customer; it is not the substrate for a register the platform ships.

WHY THIS KAIZEN IS NOT THE SCI KAIZEN WALL
`app/models/kaizen.py` (KaizenPost / KaizenCommitteeRotation) is the Safety
Culture Index's anonymous posting board — a recognition surface with a rotating
committee and a points ledger. This is a different thing that happens to share a
Japanese word: a shop-floor improvement with a cost, a savings figure and an
implementation owner. They are deliberately separate tables, separate routers
and separate licence modules. Do not merge them.

STRINGS, NOT POSTGRES ENUMS
Every vocabulary column below is a String. This is load-bearing, not laziness: a
value outside a NATIVE enum aborts the whole transaction at flush time, and the
swallowed failure is exactly what stranded every agent RCA run in RUNNING
forever. Validation lives in app/schemas/business_excellence.py, so a bad value
is a clean 422 instead of a poisoned session.
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

# ─── Shared vocabularies ─────────────────────────────────────────────────────
# Single source of truth. Pydantic validates against these, the service layer
# branches on them, and the frontend receives them from GET /api/be/meta so a
# dropdown can never offer a value the API rejects.

#: SQCDM — the standard manufacturing improvement axes. Safety is first because
#: this platform's users arrive from the EHS side, not because it outranks the
#: others.
KAIZEN_CATEGORIES: tuple[str, ...] = (
    "SAFETY",
    "QUALITY",
    "COST",
    "DELIVERY",
    "MORALE",
    "PRODUCTIVITY",
    "ENVIRONMENT",
)

#: STANDARD runs the full screening committee. FAST_TRACK is a small idea a
#: supervisor can accept without convening anyone.
#:
#: ⚠ Phase 1 described FAST_TRACK as "the Suggestion Scheme lane". Phase 2
#: retired that framing: §3 of BE_Module_Functional_Scope.docx requires an
#: anonymous-submission option, and `createdById` here is NOT NULL and rendered
#: as `raisedBy` on every register row, so a suggestion cannot be served out of
#: this table without special-casing every payload. The Suggestion Scheme is now
#: `BeSuggestion` in business_excellence_p2.py. This lane keeps its meaning and
#: its BE_KAIZEN/FAST_TRACK workflow definition — it is a fast Kaizen, not a
#: second register.
KAIZEN_LANES: tuple[str, ...] = ("STANDARD", "FAST_TRACK")

#: The two thresholds that make an idea genuinely fast-trackable, and the reason
#: they live here rather than being hard-coded at the point of use.
#:
#: `lane` routes the approval workflow — it picks between the BE_KAIZEN
#: "screening and approval" and "Suggestion Scheme fast lane" definitions, both
#: live. The FAST-TRACK *BADGE*, which is what a reader interprets as "this was
#: cheap and quick", is a different claim and is now derived from the record's
#: own money and time fields rather than from the router. Before this split, five
#: production records carried the badge with a null investment AND a null saving
#: — the badge asserted something no data supported.
#:
#: Both thresholds must be BEATEN by values that are actually PRESENT. A missing
#: investment is not a zero investment; see `fast_track_eligibility`.
FAST_TRACK_MAX_INVESTMENT: float = 5000.0
FAST_TRACK_MAX_IMPLEMENTATION_DAYS: int = 1

KAIZEN_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "SUBMITTED",
    "SCREENED",
    "APPROVED",
    "IN_IMPLEMENTATION",
    "IMPLEMENTED",
    "VERIFIED",
    "CLOSED",
    "REJECTED",
    "PARKED",
)

#: Statuses in which the record is still someone's outstanding work. Everything
#: else is terminal or parked. Used by the register's Open tile and, later, by
#: the analytics FlowSpec.
KAIZEN_OPEN_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "SUBMITTED",
    "SCREENED",
    "APPROVED",
    "IN_IMPLEMENTATION",
    "IMPLEMENTED",
)

#: HARD savings hit a cost centre and can be audited against a ledger. SOFT
#: savings are real but unbookable (time released, rework avoided). Keeping them
#: separable is the difference between a savings figure finance will accept and
#: one they will not.
SAVING_TYPES: tuple[str, ...] = ("HARD", "SOFT")

#: TPM's OPL taxonomy. SAFETY is added because this platform's OPLs will most
#: often be written off the back of an incident or an observation.
OPL_CATEGORIES: tuple[str, ...] = (
    "BASIC_KNOWLEDGE",
    "IMPROVEMENT_CASE",
    "TROUBLE_CASE",
    "SAFETY",
    "QUALITY",
)

OPL_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "IN_REVIEW",
    "APPROVED",
    "PUBLISHED",
    "SUPERSEDED",
    "RETIRED",
    "REJECTED",
)

OPL_OPEN_STATUSES: tuple[str, ...] = ("DRAFT", "IN_REVIEW", "APPROVED")

#: An OPL in one of these is being served to the floor. Superseding one is the
#: only way to change published content — see BeOpl.revision.
OPL_LIVE_STATUSES: tuple[str, ...] = ("PUBLISHED",)

ACK_STATUSES: tuple[str, ...] = (
    "ASSIGNED",
    "READ",
    "ACKNOWLEDGED",
    "WAIVED",
)

#: Shingo's device classification. Kept because it is the vocabulary a
#: manufacturing engineer already uses on the shop floor.
POKA_YOKE_DEVICE_TYPES: tuple[str, ...] = ("CONTACT", "FIXED_VALUE", "MOTION_STEP")

#: The distinction that decides whether a device is worth anything. PREVENTION
#: makes the defect impossible; DETECTION only catches it after it happens. A
#: register that does not separate them cannot tell you how mistake-proofed a
#: line actually is.
POKA_YOKE_APPROACHES: tuple[str, ...] = ("PREVENTION", "DETECTION")

#: CONTROL stops the process. WARNING signals a human and relies on them acting.
POKA_YOKE_REACTIONS: tuple[str, ...] = ("CONTROL", "WARNING")

POKA_YOKE_STATUSES: tuple[str, ...] = (
    "PROPOSED",
    "APPROVED",
    "INSTALLED",
    "VERIFIED",
    "ACTIVE",
    "DEGRADED",
    "RETIRED",
    "REJECTED",
)

POKA_YOKE_OPEN_STATUSES: tuple[str, ...] = (
    "PROPOSED",
    "APPROVED",
    "INSTALLED",
    "DEGRADED",
)

#: A device in one of these is relied upon on a live line. Bypassing one of
#: these is the event the bypass log exists to capture.
POKA_YOKE_LIVE_STATUSES: tuple[str, ...] = ("VERIFIED", "ACTIVE")

VERIFICATION_FREQUENCIES: tuple[str, ...] = (
    "SHIFT",
    "DAILY",
    "WEEKLY",
    "MONTHLY",
    "QUARTERLY",
    "ANNUAL",
)

#: Days between verifications, per frequency. SHIFT is modelled as daily here —
#: Phase 2 hands scheduling to the inspection engine, which understands shift
#: patterns; this table only needs a due date good enough to sort an overdue
#: list on.
VERIFICATION_INTERVAL_DAYS: dict[str, int] = {
    "SHIFT": 1,
    "DAILY": 1,
    "WEEKLY": 7,
    "MONTHLY": 30,
    "QUARTERLY": 91,
    "ANNUAL": 365,
}

VERIFICATION_RESULTS: tuple[str, ...] = ("PASS", "FAIL")

#: Where a BE record came from, when it was not typed in cold. Mirrors the
#: `sourceModule` convention already used by CAPA and the training engine.
BE_SOURCE_MODULES: tuple[str, ...] = (
    "OBSERVATION",
    "NEAR_MISS",
    "INCIDENT",
    "AUDIT",
    "INSPECTION",
    "HIRA",
    "RCA",
    "KAIZEN",
    "MANUAL",
)


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen — the shop-floor improvement register
# ─────────────────────────────────────────────────────────────────────────────
class BeKaizen(Base, IdMixin, SoftDeleteMixin):
    """One improvement idea, from raised to savings-verified.

    `verifiedAnnualSaving` is deliberately separate from `estimatedAnnualSaving`
    and is written by a different person at a different time. Collapsing them
    into one column is how a Kaizen programme ends up reporting the number the
    submitter hoped for rather than the one the plant banked.
    """

    __tablename__ = "BeKaizen"

    kaizenNo: Mapped[str | None] = mapped_column(String, index=True)

    # House convention: never render a raw Plant cuid. `siteName` is frozen at
    # write time so a closed record keeps the site name it was filed under.
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    areaName: Mapped[str | None] = mapped_column(String)

    title: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    lane: Mapped[str] = mapped_column(String, nullable=False, default="STANDARD", index=True)

    # Free text rather than an equipment FK: a Kaizen is very often raised
    # against a station, a jig or a process step that has no Equipment row, and
    # forcing a master-data entry first is how a suggestion scheme dies.
    lineOrMachine: Mapped[str | None] = mapped_column(String)
    processStep: Mapped[str | None] = mapped_column(String)

    problemStatement: Mapped[str] = mapped_column(Text, nullable=False)
    currentState: Mapped[str | None] = mapped_column(Text)
    proposedImprovement: Mapped[str] = mapped_column(Text, nullable=False)
    expectedBenefit: Mapped[str | None] = mapped_column(Text)

    ownerId: Mapped[str | None] = mapped_column(String, index=True)
    targetDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    implementedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    implementationNote: Mapped[str | None] = mapped_column(Text)

    # ── Cycle-time instrumentation ──
    # `implementedAt`, `verifiedAt` and `closedAt` already existed and are
    # already stamped on their transitions; these two close the gap so the whole
    # raised → screened → approved → implemented → verified → closed path is
    # measurable. Deliberately NOT backfilled from `updatedAt`: `updatedAt` moves
    # on every edit, so a backfill would invent a screening date that is really
    # "the last time anyone touched the row" and the median would be a fiction.
    # Records that predate these columns simply do not contribute to the median.
    screenedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    currency: Mapped[str] = mapped_column(String, nullable=False, default="INR")
    investmentCost: Mapped[float | None] = mapped_column(Float)
    estimatedAnnualSaving: Mapped[float | None] = mapped_column(Float)
    savingType: Mapped[str | None] = mapped_column(String)

    # ── Savings verification (Phase 2 drives these from the UI; the columns
    #    ship now so the second phase needs no migration) ──
    verifiedAnnualSaving: Mapped[float | None] = mapped_column(Float)
    verifiedById: Mapped[str | None] = mapped_column(String)
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verificationNote: Mapped[str | None] = mapped_column(Text)

    # ── Yokoten / horizontal deployment (Phase 2) ──
    # {"plantIds": [...], "areaIds": [...], "note": "..."} — deliberately a blob
    # rather than a child table until the second phase decides what a deployment
    # instance actually needs to track.
    yokotenScope: Mapped[dict | None] = mapped_column(JSONB)

    # `yokotenScope` recorded an INTENTION to deploy elsewhere and nothing ever
    # wrote to it (0 rows in production). `BeKaizenReplication` records what was
    # actually replicated, where, by whom. The blob stays for the intention; the
    # child table is the fact. `originKaizenId` is the reverse pointer carried by
    # the copy so a replicated record knows what it came from.
    originKaizenId: Mapped[str | None] = mapped_column(String, index=True)

    # Closed loop into standard work. A Kaizen that closes without changing the
    # standard is an improvement that leaves with the person who made it.
    generatedOplId: Mapped[str | None] = mapped_column(String, index=True)

    rewardPoints: Mapped[int | None] = mapped_column(Integer)

    # Reference into the Safety-Culture recognition ledger, if and when that
    # engine is extended to award Kaizen contributions. NOTHING WRITES THIS
    # TODAY — `RecognitionEntry`'s award categories are observation- and
    # walk-based and no job scores Kaizen. The column exists so the link has one
    # home when Recognition is wired, rather than a second recognition mechanism
    # growing inside Business Excellence. See the participation endpoint.
    recognitionEventId: Mapped[str | None] = mapped_column(String, index=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)
    workflowInstanceId: Mapped[str | None] = mapped_column(String, index=True)
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Provenance when the idea came out of another module.
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
        Index("ix_BeKaizen_plant_status", "plantId", "status"),
        Index("ix_BeKaizen_plant_created", "plantId", "createdAt"),
        Index("ix_BeKaizen_source", "sourceModule", "sourceRecordId"),
    )


class BeKaizenReplication(Base, IdMixin):
    """One horizontal-deployment event: this idea was taken up at that plant.

    Append-only by construction — it has no soft-delete columns and no update
    path. A replication either happened or it did not, and the register's whole
    claim ("this idea spread to four other plants") is only worth anything if the
    rows behind it cannot be quietly removed when the number looks bad.

    `replicaKaizenId` is nullable on purpose. Two things are being recorded and
    they are not the same fact: (1) the source idea was deployed at another
    plant, and (2) a new Kaizen record was raised there to run it. The API only
    ever creates both together, but a later import of historical yokoten data
    would have (1) with no (2), and modelling that as NOT NULL would force the
    import to fabricate records that never existed.
    """

    __tablename__ = "BeKaizenReplication"

    sourceKaizenId: Mapped[str] = mapped_column(
        ForeignKey("BeKaizen.id"), nullable=False, index=True
    )
    # The record raised at the target plant, when one was.
    replicaKaizenId: Mapped[str | None] = mapped_column(String, index=True)

    replicatedAtPlantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Frozen label, same rule as everywhere else: never render a raw Plant cuid,
    # and a plant renamed next year must not rewrite last year's history.
    replicatedAtPlantName: Mapped[str | None] = mapped_column(String)

    replicatedById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    replicatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # One idea is deployed at one plant once. A second attempt is the same
        # deployment being recorded twice, and it inflates the spread figure —
        # which is the single number this table exists to make trustworthy.
        UniqueConstraint(
            "sourceKaizenId", "replicatedAtPlantId", name="uq_BeKaizenReplication_source_plant"
        ),
        Index("ix_BeKaizenReplication_plant", "replicatedAtPlantId"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# One Point Lesson
# ─────────────────────────────────────────────────────────────────────────────
class BeOpl(Base, IdMixin, SoftDeleteMixin):
    """A single-page visual lesson, and the audience that has to read it.

    Published content is immutable. Changing an OPL supersedes it with a new
    revision, because an acknowledgement is a claim about a *specific* piece of
    content — silently editing the page under people who already acknowledged it
    turns a training record into a lie.
    """

    __tablename__ = "BeOpl"

    oplNo: Mapped[str | None] = mapped_column(String, index=True)

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    areaName: Mapped[str | None] = mapped_column(String)

    title: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    lineOrMachine: Mapped[str | None] = mapped_column(String)

    # The lesson itself. `contentHtml` is the one page; `keyPoints` is the
    # 3-to-5 bullet summary an operator reads when they have twenty seconds.
    # Images live in the shared Attachment table via the evidence registry, not
    # inline as base64 — a 4 MB data URI in a TEXT column is a page nobody can
    # load on a plant wifi connection.
    contentHtml: Mapped[str | None] = mapped_column(Text)
    keyPoints: Mapped[list | None] = mapped_column(JSONB)

    authorId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    approverId: Mapped[str | None] = mapped_column(String)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    effectiveFrom: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewDueAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # ── Revision chain ──
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # FK-by-value, both directions, so a chain can be walked without a
    # self-referential relationship that complicates every eager load.
    supersedesOplId: Mapped[str | None] = mapped_column(String, index=True)
    supersededByOplId: Mapped[str | None] = mapped_column(String, index=True)

    # ── Audience ──
    # {"roleCodes": [...], "areaIds": [...], "userIds": [...], "allPlant": bool}
    # Resolved to concrete BeOplAcknowledgement rows at publish time, so the
    # audience is frozen against the roster as it stood when the lesson went
    # live rather than silently re-computed later.
    audience: Mapped[dict | None] = mapped_column(JSONB)
    acknowledgementDueDays: Mapped[int] = mapped_column(Integer, nullable=False, default=14)

    # Optional Skill Matrix link. Nullable by design: forcing a Competency row
    # per OPL would make authoring a lesson a taxonomy exercise, which is how
    # OPL programmes stall. When set, an acknowledgement can post a competency
    # record so the learning shows up in the skill matrix.
    competencyId: Mapped[str | None] = mapped_column(String, index=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT", index=True)
    workflowInstanceId: Mapped[str | None] = mapped_column(String, index=True)
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    publishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retiredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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

    acknowledgements: Mapped[list["BeOplAcknowledgement"]] = relationship(
        back_populates="opl", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        Index("ix_BeOpl_plant_status", "plantId", "status"),
        Index("ix_BeOpl_plant_created", "plantId", "createdAt"),
    )


class BeOplAcknowledgement(Base, IdMixin):
    """One person's obligation to read one revision of one OPL.

    Not `TrainingAssignment`. That table is structurally close — person, source
    record, due date, escalation, completion evidence — but its `competencyId`
    is NOT NULL, so reusing it would require inventing a Competency for every
    OPL ever written. We take its escalation-job shape and leave its table
    alone.

    No soft-delete mixin: an acknowledgement is an immutable claim about what
    someone was asked to read and whether they did. Rows are removed only with
    their parent OPL, via cascade.
    """

    __tablename__ = "BeOplAcknowledgement"

    # A real FK, unlike this module's references to pre-existing tables (User,
    # Plant, Area) which stay FK-by-value so nothing additive can change how an
    # existing model loads. Both ends of this one are new, so the constraint
    # costs nothing and buys referential integrity plus the cascade.
    oplId: Mapped[str] = mapped_column(
        ForeignKey("BeOpl.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Pinned so the record still says which content was acknowledged after the
    # OPL is superseded.
    oplRevision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    personUserId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    assignedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dueAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    readAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledgedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String, nullable=False, default="ASSIGNED", index=True)
    acknowledgementNote: Mapped[str | None] = mapped_column(Text)

    # Phase 2: a short comprehension check. The columns ship now so enabling it
    # is a UI change, not a migration.
    quizScore: Mapped[float | None] = mapped_column(Float)
    quizPassedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    escalatedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalatedToUserId: Mapped[str | None] = mapped_column(String)

    # Set when the OPL carries a competencyId and the acknowledgement posted a
    # record into the skill matrix.
    competencyRecordId: Mapped[str | None] = mapped_column(String)

    waivedById: Mapped[str | None] = mapped_column(String)
    waivedReason: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    opl: Mapped[BeOpl] = relationship(back_populates="acknowledgements", lazy="raise")

    __table_args__ = (
        # One obligation per person per revision. Re-publishing a superseded OPL
        # as a new revision creates a NEW row rather than resetting this one, so
        # the history of who read what stays intact.
        UniqueConstraint(
            "oplId", "personUserId", "oplRevision", name="uq_BeOplAck_opl_person_rev"
        ),
        Index("ix_BeOplAck_person_status", "personUserId", "status"),
        Index("ix_BeOplAck_plant_status", "plantId", "status"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Poka Yoke
# ─────────────────────────────────────────────────────────────────────────────
class BePokaYoke(Base, IdMixin, SoftDeleteMixin):
    """A mistake-proofing device, and proof it still works.

    The verification loop is not optional decoration. A device register without
    one records that a poka yoke was fitted in 2023 and says nothing about
    whether the sensor has been taped over since — which is the only question
    an auditor or a line manager actually has.
    """

    __tablename__ = "BePokaYoke"

    deviceNo: Mapped[str | None] = mapped_column(String, index=True)

    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String, index=True)
    areaName: Mapped[str | None] = mapped_column(String)
    lineOrMachine: Mapped[str | None] = mapped_column(String)
    processStep: Mapped[str | None] = mapped_column(String)

    title: Mapped[str] = mapped_column(String, nullable=False)
    defectModePrevented: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    deviceType: Mapped[str] = mapped_column(String, nullable=False, index=True)
    approach: Mapped[str] = mapped_column(String, nullable=False, index=True)
    reactionMode: Mapped[str] = mapped_column(String, nullable=False)

    beforeCondition: Mapped[str | None] = mapped_column(Text)
    afterCondition: Mapped[str | None] = mapped_column(Text)

    ownerId: Mapped[str | None] = mapped_column(String, index=True)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="INR")
    cost: Mapped[float | None] = mapped_column(Float)
    installedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Verification cycle ──
    verificationFrequency: Mapped[str] = mapped_column(
        String, nullable=False, default="MONTHLY"
    )
    lastVerifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastVerificationResult: Mapped[str | None] = mapped_column(String)
    nextVerificationDueAt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    # ── Bypass log ──
    # A bypassed device is still installed, which is exactly why this is a flag
    # on the row and not a status: the register must keep showing it as fitted
    # while making plain that it is not currently protecting anything.
    isBypassed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    bypassReason: Mapped[str | None] = mapped_column(Text)
    bypassedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bypassedById: Mapped[str | None] = mapped_column(String)
    bypassApprovedById: Mapped[str | None] = mapped_column(String)

    status: Mapped[str] = mapped_column(String, nullable=False, default="PROPOSED", index=True)
    workflowInstanceId: Mapped[str | None] = mapped_column(String, index=True)
    rejectionReason: Mapped[str | None] = mapped_column(Text)
    retiredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Where the device came from — a Kaizen idea, or the permanent countermeasure
    # of an RCA (8D's D5/D6).
    sourceKaizenId: Mapped[str | None] = mapped_column(String, index=True)
    sourceRcaId: Mapped[str | None] = mapped_column(String, index=True)

    createdById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    updatedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    verifications: Mapped[list["BePokaYokeVerification"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", lazy="raise"
    )
    bypasses: Mapped[list["BePokaYokeBypass"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        Index("ix_BePokaYoke_plant_status", "plantId", "status"),
        Index("ix_BePokaYoke_plant_created", "plantId", "createdAt"),
        Index("ix_BePokaYoke_due", "plantId", "nextVerificationDueAt"),
    )


class BePokaYokeVerification(Base, IdMixin):
    """One periodic check that a device still functions.

    Append-only: a verification is evidence, and evidence that can be edited
    after the fact is not evidence. Corrections are recorded as a new check.
    """

    __tablename__ = "BePokaYokeVerification"

    deviceId: Mapped[str] = mapped_column(
        ForeignKey("BePokaYoke.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    verifiedById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    verifiedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    result: Mapped[str] = mapped_column(String, nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text)

    # The due date this check was answering, kept so a late verification is
    # still attributable to the cycle it belonged to.
    dueAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set when a FAIL auto-raised a CAPA through services/capa_spawn.py.
    capaId: Mapped[str | None] = mapped_column(String, index=True)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    device: Mapped[BePokaYoke] = relationship(back_populates="verifications", lazy="raise")

    __table_args__ = (Index("ix_BePyVerification_device_at", "deviceId", "verifiedAt"),)


class BePokaYokeBypass(Base, IdMixin):
    """One period during which a device was deliberately overridden.

    WHY THIS IS A TABLE AND NOT THE FIVE COLUMNS IT REPLACES

    The device row already carried `isBypassed` / `bypassReason` / `bypassedAt`
    / `bypassedById` / `bypassApprovedById`, and they worked — right up to the
    moment somebody ended a bypass. `/restore` NULLed the timestamp and both
    user references, so a device bypassed and restored ten times kept exactly
    one reason string, no dates, and no names. The one question an auditor asks
    about a mistake-proofing register — how often is this device switched off,
    and for how long — was unanswerable by construction, and nothing on the
    screen revealed that the history had been discarded.

    So: append-only, one row per bypass episode, closed by stamping `restoredAt`
    rather than by deletion. The device's flat columns survive as a CACHE of the
    currently-open episode (the list query, the register chip and the mobile app
    all read `isBypassed`, and a join per row to answer a boolean is not worth
    it) — but this table is the record of truth, and `restoredAt IS NULL` is the
    only definition of "open" the service layer trusts.

    A device with an open bypass is not protecting the line, whatever its
    `status` column says. That is surfaced as a DERIVED display status rather
    than by writing "BYPASSED" into `status`: the lifecycle column drives the
    workflow engine and the verification obligation, and a device that is
    bypassed is still, structurally, an installed and active one. Overloading
    the column would make "was it active when it was bypassed?" unrecoverable —
    which is the exact mistake this table exists to undo.
    """

    __tablename__ = "BePokaYokeBypass"

    deviceId: Mapped[str] = mapped_column(
        ForeignKey("BePokaYoke.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plantId: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # ── Opened ──
    bypassedById: Mapped[str] = mapped_column(String, nullable=False, index=True)
    bypassedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # NOT NULL: a bypass without a stated reason is an undocumented one, and the
    # register would be recording that protection was removed for no reason
    # anybody has to own. The API refuses it; so does the column.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approvedById: Mapped[str | None] = mapped_column(String)

    # The device's status at the moment it was bypassed, so the restore can put
    # it back honestly instead of guessing, and so the history says what was
    # actually lost.
    statusAtBypass: Mapped[str | None] = mapped_column(String)

    # ── Closed ──
    restoredAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    restoredById: Mapped[str | None] = mapped_column(String)
    restoreNote: Mapped[str | None] = mapped_column(Text)

    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    device: Mapped[BePokaYoke] = relationship(back_populates="bypasses", lazy="raise")

    __table_args__ = (
        Index("ix_BePyBypass_device_at", "deviceId", "bypassedAt"),
        Index("ix_BePyBypass_plant_open", "plantId", "restoredAt"),
    )
    # ⚠ The "one open bypass per device" guard is a PARTIAL unique index
    # (WHERE "restoredAt" IS NULL) which neither Prisma nor a plain
    # UniqueConstraint can express — it lives in
    # prisma/apply-poka-yoke-bypass-ddl.ts. The router's 409 is the friendly
    # message; that index is what actually stops a double-click opening two
    # episodes and double-counting the dashboard tile.


__all__ = [
    "BeKaizen",
    "BeKaizenReplication",
    "BeOpl",
    "BeOplAcknowledgement",
    "BePokaYoke",
    "BePokaYokeBypass",
    "BePokaYokeVerification",
    "ACK_STATUSES",
    "BE_SOURCE_MODULES",
    "FAST_TRACK_MAX_IMPLEMENTATION_DAYS",
    "FAST_TRACK_MAX_INVESTMENT",
    "KAIZEN_CATEGORIES",
    "KAIZEN_LANES",
    "KAIZEN_OPEN_STATUSES",
    "KAIZEN_STATUSES",
    "OPL_CATEGORIES",
    "OPL_LIVE_STATUSES",
    "OPL_OPEN_STATUSES",
    "OPL_STATUSES",
    "POKA_YOKE_APPROACHES",
    "POKA_YOKE_DEVICE_TYPES",
    "POKA_YOKE_LIVE_STATUSES",
    "POKA_YOKE_OPEN_STATUSES",
    "POKA_YOKE_REACTIONS",
    "POKA_YOKE_STATUSES",
    "SAVING_TYPES",
    "VERIFICATION_FREQUENCIES",
    "VERIFICATION_INTERVAL_DAYS",
    "VERIFICATION_RESULTS",
]
