"""Business Excellence — numbering, state machine, audience resolution.

Rules live here rather than in the router so that the three registers share one
implementation of the things that are easy to get subtly different: what the
next record number is, which transition a given actor may perform right now, and
how a workflow decision lands back on the record.

THE WORKFLOW BRIDGE
`workflow_engine` drives status for exactly one module inline (PTW) and hands
everything else to the form engine. BE records are neither, so this module
exposes `sync_be_record_status()` and the engine calls it from BOTH
`_sync_record_status` (approval / completion) and `reject()` (rejection). Both
call sites wrap it in a SAVEPOINT: every module in the platform reaches those
lines, so a missing BE table on a not-yet-migrated deployment must never be able
to break workflow transitions for PTW, HIRA or CAPA. That is not a hypothetical
— a LOTO deploy in exactly this shape took PTW down for 336 permits.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business_excellence import (
    FAST_TRACK_MAX_IMPLEMENTATION_DAYS,
    FAST_TRACK_MAX_INVESTMENT,
    VERIFICATION_INTERVAL_DAYS,
    BeKaizen,
    BeOpl,
    BeOplAcknowledgement,
    BePokaYoke,
    BePokaYokeBypass,
)
from app.models.factory import FactoryProfile
from app.models.plant import Area, Plant
from app.models.user import User

log = logging.getLogger(__name__)

#: The workflow-engine module tokens these registers submit under. One workflow
#: definition per token; the Form Engine's "several forms share one workflow"
#: shortcut does not apply because each register has a genuinely different
#: approval chain.
WF_MODULE_KAIZEN = "BE_KAIZEN"
WF_MODULE_OPL = "BE_OPL"
WF_MODULE_POKA_YOKE = "BE_POKA_YOKE"

#: Phase 2's tokens, written as literals rather than imported from
#: services/business_excellence_p2.py. That module imports vocabularies from
#: this one, so importing it back at module scope is a cycle. The literals are
#: asserted against the real constants by tests/test_business_excellence_p2.py,
#: so they cannot drift silently.
WF_MODULE_SUGGESTION = "BE_SUGGESTION"
WF_MODULE_QCC = "BE_QCC"
WF_MODULE_SIP = "BE_SIP"

#: The gate workflow_engine reads to decide whether a module is a BE register.
#: Widening it here is what routes the Phase 2 modules through the SAVEPOINT the
#: engine already wraps this bridge in — a second branch in the engine would be a
#: second place for a missing table to poison the transaction.
BE_WORKFLOW_MODULES: frozenset[str] = frozenset(
    {
        WF_MODULE_KAIZEN,
        WF_MODULE_OPL,
        WF_MODULE_POKA_YOKE,
        WF_MODULE_SUGGESTION,
        WF_MODULE_QCC,
        WF_MODULE_SIP,
    }
)

#: CAPA source-type code a failed Poka Yoke verification raises under. Seeded by
#: prisma/seed-be-capa-sources.ts; spawn_capa 404s cleanly if it is missing,
#: which is why the caller treats a null return as "no CAPA raised" rather than
#: an error.
CAPA_SOURCE_POKA_YOKE_FAILURE = "POKA_YOKE_FAILURE"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive DB value to UTC-aware.

    Rows written before a column was timezone-aware come back naive, and a
    naive ↔ aware comparison raises TypeError mid-request. Every date
    comparison in this module goes through here.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ─────────────────────────────────────────────────────────────────────────────
# Numbering
# ─────────────────────────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"(\d+)$")


async def next_record_number(
    db: AsyncSession,
    *,
    model: type,
    column: Any,
    prefix: str,
    plant_id: str,
    year: int | None = None,
) -> str:
    """Next record number for a (plant, prefix, year) sequence — highest + 1.

    Deliberately max-of-suffix, NOT `count(rows) + 1`. The count form breaks the
    moment any row is soft-deleted: the live count no longer matches the highest
    number issued, so the next insert re-issues an existing one and 500s on the
    unique index. That exact bug shipped in ~15 numbering functions across this
    platform before it was found.

    Soft-deleted rows are INCLUDED in the scan for the same reason — a deleted
    Kaizen must not free its number for reuse, or an export produced last month
    stops matching the register.

    ⚠ `include_deleted=True` is what makes that true. These models are
    registered governed, so every ORM SELECT is silently rewritten to
    `isDeleted = false`; without this option the scan sees only live rows and
    re-issues the number of anything deleted, which 500s on the unique index.
    The same option is on loto's and the form engine's generators for exactly
    this reason.
    """
    yr = year or _now().year
    stem = f"{prefix}-{yr}-"

    rows = (
        await db.execute(
            select(column)
            .where(
                model.plantId == plant_id,
                column.isnot(None),
                column.like(f"{stem}%"),
            )
            .execution_options(include_deleted=True)
        )
    ).scalars().all()

    highest = 0
    for value in rows:
        m = _NUM_RE.search(value or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{stem}{highest + 1:04d}"


# ─────────────────────────────────────────────────────────────────────────────
# Site / area denormalisation
# ─────────────────────────────────────────────────────────────────────────────
async def resolve_site_labels(
    db: AsyncSession, *, plant_id: str, area_id: str | None
) -> tuple[str | None, str | None]:
    """Freeze the plant and area NAMES onto the record at write time.

    House rule: never render a raw Plant cuid, and never make a closed record
    depend on a lookup that may have been renamed since. Returns
    `(siteName, areaName)`; either may be None if the id does not resolve.
    """
    plant = await db.get(Plant, plant_id)
    area_name: str | None = None
    if area_id:
        area = await db.get(Area, area_id)
        # An area belonging to a different plant is a client bug, not something
        # to silently accept — the caller validates, this just refuses to label.
        area_name = area.name if area is not None and area.plantId == plant_id else None
    return (plant.name if plant else None, area_name)


async def validate_area(db: AsyncSession, *, plant_id: str, area_id: str | None) -> bool:
    """True when `area_id` is absent or genuinely belongs to `plant_id`."""
    if not area_id:
        return True
    area = await db.get(Area, area_id)
    return area is not None and area.plantId == plant_id


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen state machine
# ─────────────────────────────────────────────────────────────────────────────
#: status → the statuses it may move to, and the permission each move needs.
#: Held as data so the register, the detail screen and the workflow bridge all
#: read one table instead of three sets of if-statements.
KAIZEN_TRANSITIONS: dict[str, dict[str, str]] = {
    "DRAFT": {"SUBMITTED": "KAIZEN.CREATE"},
    # SCREENED is where the committee lands a STANDARD idea; the FAST_TRACK lane
    # skips straight to APPROVED (see allowed_kaizen_actions).
    "SUBMITTED": {"SCREENED": "KAIZEN.APPROVE", "REJECTED": "KAIZEN.APPROVE", "PARKED": "KAIZEN.APPROVE"},
    "SCREENED": {"APPROVED": "KAIZEN.APPROVE", "REJECTED": "KAIZEN.APPROVE", "PARKED": "KAIZEN.APPROVE"},
    "APPROVED": {"IN_IMPLEMENTATION": "KAIZEN.UPDATE"},
    "IN_IMPLEMENTATION": {"IMPLEMENTED": "KAIZEN.UPDATE"},
    # VERIFIED requires a savings figure someone other than the submitter signed
    # off — the gate lives in the router, this only says who may attempt it.
    "IMPLEMENTED": {"VERIFIED": "KAIZEN.VERIFY"},
    "VERIFIED": {"CLOSED": "KAIZEN.VERIFY"},
    "PARKED": {"SUBMITTED": "KAIZEN.UPDATE", "REJECTED": "KAIZEN.APPROVE"},
    "REJECTED": {},
    "CLOSED": {},
}


def allowed_kaizen_actions(kaizen: BeKaizen, granted: set[str]) -> list[str]:
    """Which transitions THIS caller may perform on THIS record right now.

    Returned on every detail payload so the UI renders buttons from the server's
    answer instead of re-deriving the rules. A client that guesses produces
    either a button that 403s or a hidden action the user is entitled to — both
    shipped on PTW before the gate moved server-side.
    """
    out: list[str] = []
    for target, perm in KAIZEN_TRANSITIONS.get(kaizen.status, {}).items():
        if perm in granted:
            out.append(target)
    # The Suggestion Scheme lane: a small idea does not need a committee, so a
    # FAST_TRACK record may be approved straight out of SUBMITTED.
    if (
        kaizen.lane == "FAST_TRACK"
        and kaizen.status == "SUBMITTED"
        and "KAIZEN.APPROVE" in granted
        and "APPROVED" not in out
    ):
        out.append("APPROVED")
    return out


def kaizen_transition_permitted(kaizen: BeKaizen, target: str, granted: set[str]) -> bool:
    return target in allowed_kaizen_actions(kaizen, granted)


def is_kaizen_overdue(kaizen: BeKaizen, *, at: datetime | None = None) -> bool:
    """Past its implementation target and not yet implemented.

    A CLOSED or REJECTED record is never overdue however old its target is —
    an overdue flag on a finished record is noise that trains people to ignore
    the column.
    """
    if kaizen.status in {"IMPLEMENTED", "VERIFIED", "CLOSED", "REJECTED", "PARKED"}:
        return False
    target = _aware(kaizen.targetDate)
    if target is None:
        return False
    return target < (at or _now())


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen — completeness gates
# ─────────────────────────────────────────────────────────────────────────────
#: The status transitions that stamp a lifecycle timestamp, and the attribute
#: each one stamps. Held as data next to KAIZEN_TRANSITIONS so a new state
#: cannot be added to the machine without someone seeing that it needs a clock.
KAIZEN_TIMESTAMP_FIELDS: dict[str, str] = {
    "SCREENED": "screenedAt",
    "APPROVED": "approvedAt",
    "IMPLEMENTED": "implementedAt",
    "VERIFIED": "verifiedAt",
    "CLOSED": "closedAt",
}


def stamp_kaizen_transition(kaizen: BeKaizen, target: str, *, at: datetime) -> None:
    """Record WHEN a Kaizen entered `target`, first time only.

    First-time-only matters for the states that are reachable twice. A PARKED
    record re-enters SUBMITTED and walks the chain again; if that re-stamped
    `screenedAt`, the cycle-time median would silently measure the second pass
    and report a programme as faster than it is. The original timestamp is the
    one that answers "how long did this idea wait".
    """
    field = KAIZEN_TIMESTAMP_FIELDS.get(target)
    if field is not None and getattr(kaizen, field, None) is None:
        setattr(kaizen, field, at)


def kaizen_approval_blockers(kaizen: BeKaizen) -> list[str]:
    """Why this Kaizen cannot be approved right now — empty means it can.

    An explicit, testable function rather than an inline `if` in the router,
    matching how the other completeness gates in BE are written (QCC's
    `validation_blockers()`). Two callers depend on that being one function: the
    router enforces it, and the detail payload returns it so the UI can disable
    the button and say WHY instead of hiding an action the user is entitled to.

    The rule: approving an idea assigns work to somebody. An approval with no
    owner is a decision that produces no action, and it is how a register fills
    up with approved ideas nobody is doing. All four live records at the time of
    writing were at or past APPROVED with a null owner.
    """
    blockers: list[str] = []
    if not kaizen.ownerId:
        blockers.append("Assign an owner before approving.")
    return blockers


def kaizen_implementation_window_days(kaizen: BeKaizen) -> int | None:
    """Planned days from raise to target. None when there is no target date."""
    target = _aware(kaizen.targetDate)
    created = _aware(kaizen.createdAt)
    if target is None or created is None:
        return None
    return max(0, round((target - created).total_seconds() / 86400))


def fast_track_eligibility(kaizen: BeKaizen) -> dict[str, Any]:
    """Is the FAST-TRACK BADGE earned by this record's own data?

    Deliberately NOT the same question as `lane`. `lane` routes the approval
    workflow and is the submitter's choice of process; the badge is a claim
    about the idea — cheap and quick — that a reader acts on. Those were the same
    field, which is how five production records came to display "Fast track"
    with a null investment AND a null saving.

    Both figures must be PRESENT and under their threshold. A missing investment
    is not a zero investment: treating absent as zero is exactly the defect,
    because it makes the cheapest possible claim out of no data at all. The
    implementation window is derived from `targetDate` against `createdAt` —
    there is no separate estimated-duration field, and inventing one would leave
    every existing record permanently unable to qualify.

    Returns the reasons as well as the verdict so the UI can explain a missing
    badge rather than leaving someone to guess.
    """
    reasons: list[str] = []

    investment = kaizen.investmentCost
    if investment is None:
        reasons.append("No investment figure recorded.")
    elif investment > FAST_TRACK_MAX_INVESTMENT:
        reasons.append(
            f"Investment of {investment:,.0f} is above the "
            f"{FAST_TRACK_MAX_INVESTMENT:,.0f} fast-track limit."
        )

    days = kaizen_implementation_window_days(kaizen)
    if days is None:
        reasons.append("No target date, so the implementation window is unknown.")
    elif days > FAST_TRACK_MAX_IMPLEMENTATION_DAYS:
        reasons.append(
            f"Implementation window of {days} day(s) is above the "
            f"{FAST_TRACK_MAX_IMPLEMENTATION_DAYS}-day fast-track limit."
        )

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "maxInvestment": FAST_TRACK_MAX_INVESTMENT,
        "maxImplementationDays": FAST_TRACK_MAX_IMPLEMENTATION_DAYS,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen — participation
# ─────────────────────────────────────────────────────────────────────────────
async def resolve_plant_headcount(db: AsyncSession, plant_ids: Sequence[str]) -> dict[str, int]:
    """Active workforce per plant, from the platform's one populated source.

    That source is `FactoryProfile.totalEmployees`, and picking it was not
    obvious — four columns on this platform are named for headcount and three of
    them are empty:

      Manhours.headcount                       204 rows, 0 non-zero
      ScorecardPeriod.headcount                396 rows, 0 non-zero
      ManhoursSubmission.totalEmployeeStrength   2 rows, 0 non-zero
      ManhoursEmployeeCategory.averageHeadcount  0 rows
      FactoryProfile.totalEmployees             16 rows, 16 populated  ← this one

    Counting `User` rows per plant was the other candidate and is wrong by an
    order of magnitude: a plant with 880 workers has 59 platform logins, so
    participation would read 3% as 45%. A flattering denominator is worse than
    no denominator.

    Plants absent from the returned map have no recorded headcount. The caller
    must render that as "not recorded", never as zero — a zero denominator makes
    participation either infinite or 0%, and both are lies. Only 16 of 28 plants
    currently have a FactoryProfile, so this is the common case, not the edge.
    """
    if not plant_ids:
        return {}
    rows = (
        await db.execute(
            select(FactoryProfile.siteId, FactoryProfile.totalEmployees).where(
                FactoryProfile.siteId.in_(list(plant_ids)),
                FactoryProfile.isDeleted.is_(False),
                FactoryProfile.totalEmployees > 0,
            )
        )
    ).all()
    return {site_id: total for site_id, total in rows}


def summarise_participation(
    rows: Sequence[BeKaizen], *, headcount: int | None
) -> dict[str, Any]:
    """Unique submitters, their share of the workforce, and ideas per submitter.

    `participationRate` is None — not 0 — when the plant has no recorded
    headcount. The tile renders "Headcount not recorded" and links to the factory
    profile, which is a fixable problem; a 0% that is really "we do not know"
    reads as a failing programme and is not fixable because nobody knows it is
    wrong.

    `ideasPerSubmitter` divides by submitters, not by headcount: it answers "do
    the people who engage keep engaging", which is a different question from "how
    many people engage" and is the one that distinguishes a programme carried by
    two enthusiasts from a broad one.
    """
    submitters = {k.createdById for k in rows if k.createdById}
    submitter_count = len(submitters)
    idea_count = len(rows)

    rate: float | None = None
    if headcount:
        # Capped at 100. Contractors and agency workers raise ideas and are not
        # in totalEmployees, so an active plant can genuinely exceed its own
        # denominator; reporting 118% would just look broken.
        rate = round(min(submitter_count / headcount * 100, 100.0), 1)

    return {
        "submitters": submitter_count,
        "ideas": idea_count,
        "headcount": headcount,
        "participationRate": rate,
        "ideasPerSubmitter": round(idea_count / submitter_count, 2) if submitter_count else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen — cycle time
# ─────────────────────────────────────────────────────────────────────────────
#: Below this many measurable records a median is a number that looks like a
#: measurement and is not one; the screen shows "Not enough data" instead. At
#: four records, one slow idea moves the median by weeks.
CYCLE_TIME_MIN_SAMPLE = 5


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def kaizen_cycle_times(rows: Iterable[BeKaizen]) -> dict[str, Any]:
    """Median days raised → implemented, and raised → verified.

    Computed ONLY over records where the endpoint timestamp is actually set. A
    record still in progress has no implementation date, and counting it as zero
    days — or as today-minus-raised — would report a programme as fast because it
    has not finished anything. Missing denominator returns null, never 0: the
    same discipline the frequency-rate layer applies to manhours.
    """
    to_implemented: list[float] = []
    to_verified: list[float] = []

    for k in rows:
        created = _aware(k.createdAt)
        if created is None:
            continue
        implemented = _aware(k.implementedAt)
        if implemented is not None:
            to_implemented.append((implemented - created).total_seconds() / 86400)
        verified = _aware(k.verifiedAt)
        if verified is not None:
            to_verified.append((verified - created).total_seconds() / 86400)

    def summarise(samples: list[float]) -> float | None:
        # Below the sample floor the answer is "we do not know", and the caller
        # renders exactly that. Returning the median anyway and letting the UI
        # decide would put one decision in two places.
        if len(samples) < CYCLE_TIME_MIN_SAMPLE:
            return None
        return round(_median(samples), 1)

    return {
        "medianDaysRaisedToImplemented": summarise(to_implemented),
        "medianDaysRaisedToVerified": summarise(to_verified),
        "implementedSampleSize": len(to_implemented),
        "verifiedSampleSize": len(to_verified),
        "minimumSampleSize": CYCLE_TIME_MIN_SAMPLE,
    }


# ─────────────────────────────────────────────────────────────────────────────
# OPL — audience resolution and acknowledgement
# ─────────────────────────────────────────────────────────────────────────────
async def resolve_opl_audience(db: AsyncSession, opl: BeOpl) -> list[str]:
    """Expand an OPL's audience spec into concrete user ids.

    Resolved ONCE, at publish time, and frozen as acknowledgement rows. A
    lazily-evaluated audience would silently re-scope the lesson every time
    somebody joined or left a department, which makes "97% acknowledged" a
    number about the current roster rather than about the people who were
    actually asked.

    Union, not intersection: a lesson aimed at "all line leaders AND everyone in
    Cutting" means both groups, which is what an author expects when they tick
    two boxes.
    """
    spec = opl.audience or {}
    role_codes = [str(r) for r in (spec.get("roleCodes") or [])]
    area_ids = [str(a) for a in (spec.get("areaIds") or [])]
    explicit = [str(u) for u in (spec.get("userIds") or [])]
    all_plant = bool(spec.get("allPlant"))

    wanted: set[str] = set(explicit)

    if all_plant or role_codes:
        stmt = select(User.id).where(User.plantId == opl.plantId)
        if role_codes and not all_plant:
            stmt = stmt.where(User.role.in_(role_codes))
        # `isActive` is not universal across this schema's User rows; filtering
        # on it here would silently drop people on deployments that never set
        # it. Roster hygiene is a separate problem from lesson distribution.
        wanted |= set((await db.execute(stmt)).scalars().all())

    if area_ids:
        # Area membership is not modelled on User in this schema — there is no
        # user↔area join table. The nearest true signal is area ownership, so an
        # area audience resolves to the area owners rather than pretending to a
        # membership list that does not exist. Callers see exactly who was
        # assigned on the acknowledgement matrix, so this degrades visibly
        # rather than silently under-assigning.
        owners = (
            await db.execute(
                select(Area.ownerUserId).where(
                    Area.id.in_(area_ids),
                    Area.plantId == opl.plantId,
                    Area.ownerUserId.isnot(None),
                )
            )
        ).scalars().all()
        wanted |= {o for o in owners if o}

    # Never assign the author their own lesson — an acknowledgement from the
    # person who wrote it is not evidence of anything.
    wanted.discard(opl.authorId)
    return sorted(wanted)


async def create_acknowledgements(
    db: AsyncSession, opl: BeOpl, user_ids: Sequence[str]
) -> int:
    """Materialise one acknowledgement row per person for this revision.

    Idempotent against the (oplId, personUserId, oplRevision) unique key: a
    re-publish of the same revision tops up anyone missing rather than raising.
    Returns the number of NEW rows created.
    """
    if not user_ids:
        return 0

    existing = set(
        (
            await db.execute(
                select(BeOplAcknowledgement.personUserId).where(
                    BeOplAcknowledgement.oplId == opl.id,
                    BeOplAcknowledgement.oplRevision == opl.revision,
                )
            )
        ).scalars().all()
    )

    due = _now() + timedelta(days=opl.acknowledgementDueDays or 14)
    created = 0
    for uid in user_ids:
        if uid in existing:
            continue
        db.add(
            BeOplAcknowledgement(
                oplId=opl.id,
                oplRevision=opl.revision,
                personUserId=uid,
                plantId=opl.plantId,
                dueAt=due,
                status="ASSIGNED",
            )
        )
        created += 1
    await db.flush()
    return created


def is_ack_overdue(ack: BeOplAcknowledgement, *, at: datetime | None = None) -> bool:
    """Past due and still unacknowledged.

    OVERDUE is computed, never stored: a stored flag needs a job to maintain it
    and is wrong for however long that job is broken. The Signal Engine spent
    five days emitting nothing while its job "ran" nightly — the lesson taken
    from that is to derive anything derivable at read time.
    """
    if ack.status in {"ACKNOWLEDGED", "WAIVED"}:
        return False
    due = _aware(ack.dueAt)
    return due is not None and due < (at or _now())


def summarise_acknowledgements(acks: Iterable[BeOplAcknowledgement]) -> dict[str, Any]:
    """Counts for the register row and the detail header."""
    rows = list(acks)
    assigned = len(rows)
    read = sum(1 for a in rows if a.readAt is not None)
    acknowledged = sum(1 for a in rows if a.status == "ACKNOWLEDGED")
    waived = sum(1 for a in rows if a.status == "WAIVED")
    overdue = sum(1 for a in rows if is_ack_overdue(a))
    return {
        "assigned": assigned,
        "read": read,
        "acknowledged": acknowledged,
        "waived": waived,
        "overdue": overdue,
        # None, not 0, when nothing has been assigned: "nobody has been asked"
        # and "nobody has read it" are different facts, and a 0% badge on an
        # unpublished lesson reads as a failure that has not happened yet.
        "percent": round(acknowledged * 100.0 / assigned, 1) if assigned else None,
    }


def is_opl_review_overdue(opl: BeOpl, *, at: datetime | None = None) -> bool:
    if opl.status not in {"PUBLISHED"}:
        return False
    due = _aware(opl.reviewDueAt)
    return due is not None and due < (at or _now())


def allowed_opl_actions(opl: BeOpl, granted: set[str]) -> list[str]:
    out: list[str] = []
    if opl.status == "DRAFT" and "OPL.CREATE" in granted:
        out.append("SUBMIT")
    if opl.status == "APPROVED" and "OPL.APPROVE" in granted:
        out.append("PUBLISH")
    if opl.status == "PUBLISHED":
        if "OPL.CREATE" in granted:
            # Published content is immutable — the only way to change it is a
            # new revision that supersedes this one.
            out.append("REVISE")
        if "OPL.APPROVE" in granted:
            out.append("RETIRE")
    if opl.status in {"DRAFT", "IN_REVIEW"} and "OPL.UPDATE" in granted:
        out.append("EDIT")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Poka Yoke verification cycle
# ─────────────────────────────────────────────────────────────────────────────
def next_verification_due(frequency: str, *, from_dt: datetime | None = None) -> datetime:
    """When the next check falls due. Unknown frequency degrades to monthly
    rather than raising — a device with a typo'd frequency should still appear
    on somebody's list."""
    days = VERIFICATION_INTERVAL_DAYS.get(frequency, 30)
    return (from_dt or _now()) + timedelta(days=days)


def is_verification_overdue(device: BePokaYoke, *, at: datetime | None = None) -> bool:
    """Overdue only matters for a device that is supposed to be protecting
    something. A PROPOSED or RETIRED device has no verification obligation."""
    if device.status not in {"VERIFIED", "ACTIVE", "DEGRADED"}:
        return False
    due = _aware(device.nextVerificationDueAt)
    return due is not None and due < (at or _now())


#: The display status a device with an open bypass shows INSTEAD of its
#: lifecycle status. Deliberately not a value `BePokaYoke.status` can hold: that
#: column drives the workflow engine and the verification obligation, and a
#: bypassed device is still, structurally, an installed and active one. Writing
#: "BYPASSED" into it would make "what was it before somebody switched it off?"
#: unrecoverable — the exact class of loss BePokaYokeBypass exists to undo.
BYPASSED_DISPLAY_STATUS = "BYPASSED"


def poka_yoke_display_status(device: BePokaYoke, *, has_open_bypass: bool | None = None) -> str:
    """What the badge should say — which is not always what `status` holds.

    A device with an open bypass is not protecting the line, whatever its
    lifecycle column says, and a register that renders it as Active is stating
    something untrue on the one screen an auditor reads. The lifecycle value
    stays intact underneath and travels in the payload as `status`.

    `has_open_bypass` lets a caller that has already resolved the open episodes
    in bulk pass the answer in rather than forcing a query per row; it falls
    back to the device's own cached flag, which the bypass endpoints dual-write.
    """
    open_now = device.isBypassed if has_open_bypass is None else has_open_bypass
    if open_now and device.status not in {"RETIRED", "REJECTED"}:
        return BYPASSED_DISPLAY_STATUS
    return device.status


def bypass_duration_hours(bypass: "BePokaYokeBypass", *, at: datetime | None = None) -> float | None:
    """How long this episode has run, in hours. None if the start is unreadable.

    An OPEN bypass measures to now rather than returning null — "this device has
    been switched off for 340 hours" is the number that makes somebody act, and
    a blank cell until the day it is restored is how a bypass becomes permanent
    without anybody deciding it should be.
    """
    started = _aware(bypass.bypassedAt)
    if started is None:
        return None
    ended = _aware(bypass.restoredAt) or (at or _now())
    return max(0.0, round((ended - started).total_seconds() / 3600, 1))


def allowed_poka_yoke_actions(device: BePokaYoke, granted: set[str]) -> list[str]:
    out: list[str] = []
    if device.status == "PROPOSED" and "POKAYOKE.CREATE" in granted:
        out.append("SUBMIT")
    if device.status == "APPROVED" and "POKAYOKE.UPDATE" in granted:
        out.append("INSTALL")
    if device.status in {"INSTALLED", "VERIFIED", "ACTIVE", "DEGRADED"} and "POKAYOKE.VERIFY" in granted:
        out.append("VERIFY")
    if device.status in {"VERIFIED", "ACTIVE"} and not device.isBypassed and "POKAYOKE.UPDATE" in granted:
        out.append("BYPASS")
    if device.isBypassed and "POKAYOKE.UPDATE" in granted:
        out.append("RESTORE")
    if device.status not in {"RETIRED", "REJECTED"} and "POKAYOKE.APPROVE" in granted:
        out.append("RETIRE")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# The workflow bridge
# ─────────────────────────────────────────────────────────────────────────────
#: What each register's record status becomes when its workflow instance
#: finishes, and when a reviewer rejects it.
_ON_APPROVED: dict[str, str] = {
    WF_MODULE_KAIZEN: "APPROVED",
    WF_MODULE_OPL: "APPROVED",
    WF_MODULE_POKA_YOKE: "APPROVED",
}
_ON_REJECTED: dict[str, str] = {
    WF_MODULE_KAIZEN: "REJECTED",
    WF_MODULE_OPL: "REJECTED",
    WF_MODULE_POKA_YOKE: "REJECTED",
}
_MODEL_FOR: dict[str, type] = {
    WF_MODULE_KAIZEN: BeKaizen,
    WF_MODULE_OPL: BeOpl,
    WF_MODULE_POKA_YOKE: BePokaYoke,
}


async def sync_be_record_status(
    db: AsyncSession,
    *,
    module: str,
    record_id: str,
    instance_completed: bool,
    rejected: bool = False,
    reason: str | None = None,
) -> bool:
    """Land a workflow decision on the BE record. Returns True if it applied.

    A no-op for any module that is not one of ours, so the engine can call this
    unconditionally. Callers wrap it in a SAVEPOINT — see the module docstring.
    """
    if module not in BE_WORKFLOW_MODULES:
        return False

    if module not in _MODEL_FOR:
        # Phase 2 register (Suggestion / QCC / SIP). Imported here rather than at
        # module scope because business_excellence_p2 imports vocabularies from
        # this module — a top-level import would be a cycle. The caller's
        # SAVEPOINT still covers everything below.
        from app.services.business_excellence_p2 import sync_p2_record_status

        return await sync_p2_record_status(
            db,
            module=module,
            record_id=record_id,
            instance_completed=instance_completed,
            rejected=rejected,
            reason=reason,
        )

    model = _MODEL_FOR[module]
    record = await db.get(model, record_id)
    if record is None:
        return False

    if rejected:
        record.status = _ON_REJECTED[module]
        if reason:
            record.rejectionReason = reason
        await db.flush()
        return True

    if not instance_completed:
        # Still inside the approval chain. Only DRAFT needs moving — a record
        # already at SUBMITTED/IN_REVIEW must not be dragged backwards by a
        # mid-chain advance.
        if module == WF_MODULE_KAIZEN and record.status == "DRAFT":
            record.status = "SUBMITTED"
            await db.flush()
            return True
        if module == WF_MODULE_OPL and record.status == "DRAFT":
            record.status = "IN_REVIEW"
            await db.flush()
            return True
        return False

    approved = _ON_APPROVED[module]
    # Never resurrect a terminal record. A repair job that recreates a closure
    # task must not reopen something already closed or rejected.
    if record.status in {"CLOSED", "REJECTED", "RETIRED", "SUPERSEDED"}:
        return False
    record.status = approved
    if module == WF_MODULE_OPL:
        record.approvedAt = _now()
    await db.flush()
    return True


__all__ = [
    "BE_WORKFLOW_MODULES",
    "CAPA_SOURCE_POKA_YOKE_FAILURE",
    "KAIZEN_TRANSITIONS",
    "WF_MODULE_KAIZEN",
    "WF_MODULE_OPL",
    "WF_MODULE_POKA_YOKE",
    "WF_MODULE_QCC",
    "WF_MODULE_SIP",
    "WF_MODULE_SUGGESTION",
    "allowed_kaizen_actions",
    "allowed_opl_actions",
    "allowed_poka_yoke_actions",
    "create_acknowledgements",
    "is_ack_overdue",
    "is_kaizen_overdue",
    "is_opl_review_overdue",
    "is_verification_overdue",
    "kaizen_transition_permitted",
    "next_record_number",
    "next_verification_due",
    "resolve_opl_audience",
    "resolve_site_labels",
    "summarise_acknowledgements",
    "sync_be_record_status",
    "validate_area",
]
