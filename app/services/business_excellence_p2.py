"""Business Excellence Phase 2 — Suggestion, QCC, SIP and benefit realisation.

Rules live here rather than in the router for the same reason Phase 1 put them
in a service: three registers that each have a state machine, a due date and a
sign-off will drift into three subtly different implementations of the same idea
the moment they are written inline.

WHAT IS DELIBERATELY NOT HERE
  * No RCA. §6 requires the circle's analysis stage to use the shared engine, so
    `ensure_project_rca()` calls rca_core.create_problem_rca() and stores an id.
    There is no cause model, no 5-why and no fishbone anywhere in Phase 2.
  * No stored RAG. `project_rag()` and `sip_rag()` derive it at read time from
    dates that are already on the row — see the models module docstring.
  * No notification code. §8's framework is services/notifications.py and the
    workflow engine's own SLA escalation; this module raises no email of its own.

THE WORKFLOW BRIDGE
Phase 1's `sync_be_record_status()` is still the single entry point the workflow
engine calls. It now delegates here for the three Phase 2 modules. Both of its
call sites are already wrapped in a SAVEPOINT, which is what stops a missing
Phase 2 table from turning into a 500 on every workflow transition
platform-wide — the LOTO failure mode. Nothing in this module may be called from
the engine outside that savepoint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business_excellence_p2 import (
    QCC_RCA_STAGE,
    QCC_STAGES_BY_METHODOLOGY,
    VALIDATION_WINDOW_DAYS,
    BeBenefit,
    BeBenefitReading,
    BeQccProject,
    BeQccProjectStage,
    BeQccTeam,
    BeQccTeamMember,
    BeSip,
    BeSipMilestone,
    BeSuggestion,
)

log = logging.getLogger(__name__)

#: Workflow-engine module tokens. One definition per token, seeded by
#: prisma/seed-be-p2-workflows.ts. Phase 1's BE_WORKFLOW_MODULES is widened to
#: include these so the engine's existing `module in BE_WORKFLOW_MODULES` gate
#: routes them without a second branch.
WF_MODULE_SUGGESTION = "BE_SUGGESTION"
WF_MODULE_QCC = "BE_QCC"
WF_MODULE_SIP = "BE_SIP"

P2_WORKFLOW_MODULES: frozenset[str] = frozenset(
    {WF_MODULE_SUGGESTION, WF_MODULE_QCC, WF_MODULE_SIP}
)

#: How long past a date something has to be before it turns the rollup red
#: rather than amber. One working fortnight — short enough that a slipping
#: project is visible while it can still be recovered, long enough that a board
#: is not permanently red because somebody signed a gate off a day late.
_AMBER_DAYS = 14


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive DB value to UTC-aware.

    Rows written before a column was timezone-aware come back naive from
    asyncpg, and a naive ↔ aware comparison raises TypeError mid-request. Every
    date comparison in this module goes through here — same rule as Phase 1.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ─────────────────────────────────────────────────────────────────────────────
# Suggestion Scheme
# ─────────────────────────────────────────────────────────────────────────────
#: status → the statuses it may move to, and the permission each move needs.
#: SCREEN and DECIDE are separate permissions because §3 describes two separate
#: acts by (usually) two different groups: a coordinator triages the inbox, a
#: committee decides. Collapsing them would let whoever clears the queue also
#: approve the incentive.
SUGGESTION_TRANSITIONS: dict[str, dict[str, str]] = {
    "DRAFT": {"SUBMITTED": "SUGGESTION.CREATE"},
    "SUBMITTED": {"SCREENING": "SUGGESTION.SCREEN"},
    "SCREENING": {
        "ACCEPTED": "SUGGESTION.DECIDE",
        "REJECTED": "SUGGESTION.DECIDE",
        "DEFERRED": "SUGGESTION.DECIDE",
        # DUPLICATE is a triage outcome, not a committee decision — it needs no
        # deliberation and closing it under SCREEN keeps the acceptance-rate
        # denominator honest.
        "DUPLICATE": "SUGGESTION.SCREEN",
    },
    "ACCEPTED": {"IN_IMPLEMENTATION": "SUGGESTION.UPDATE"},
    # A deferred suggestion is re-submitted, not resurrected into ACCEPTED: it
    # goes back through screening so the committee that deferred it sees it again.
    "DEFERRED": {"SUBMITTED": "SUGGESTION.UPDATE", "REJECTED": "SUGGESTION.DECIDE"},
    "IN_IMPLEMENTATION": {"IMPLEMENTED": "SUGGESTION.UPDATE"},
    "IMPLEMENTED": {"CLOSED": "SUGGESTION.DECIDE"},
    "CLOSED": {},
    "REJECTED": {},
    "DUPLICATE": {},
}

SUGGESTION_TERMINAL: frozenset[str] = frozenset({"CLOSED", "REJECTED", "DUPLICATE"})


def allowed_suggestion_actions(s: BeSuggestion, granted: set[str]) -> list[str]:
    """Which transitions THIS caller may perform on THIS record right now.

    Returned on every detail payload so the UI renders buttons from the server's
    answer instead of re-deriving the rules — a client that guesses produces
    either a button that 403s or a hidden action the user was entitled to.
    """
    return [
        target
        for target, perm in SUGGESTION_TRANSITIONS.get(s.status, {}).items()
        if perm in granted
    ]


def suggestion_transition_permitted(
    s: BeSuggestion, target: str, granted: set[str]
) -> bool:
    return target in allowed_suggestion_actions(s, granted)


def submitter_visible_to(s: BeSuggestion, viewer_id: str, granted: set[str]) -> bool:
    """Whether this viewer may see who raised an anonymous suggestion.

    The ONLY place that decision is made. A non-anonymous suggestion is visible
    to anyone who can read the record; an anonymous one is visible to nobody but
    the submitter themselves — deliberately including administrators, because a
    scheme where a manager can unmask a critic is not a scheme people use twice.

    `granted` is accepted and ignored on purpose: it makes the "no permission
    unmasks this" rule explicit at the call site rather than something a future
    reader has to infer from an absent parameter.
    """
    if not s.isAnonymous:
        return True
    return viewer_id == s.createdById


def is_suggestion_overdue(s: BeSuggestion, *, at: datetime | None = None) -> bool:
    """Past its implementation target and not yet implemented.

    A terminal record is never overdue however old its target is — an overdue
    flag on a finished record is noise that trains people to ignore the column.
    """
    if s.status in SUGGESTION_TERMINAL or s.status in {"IMPLEMENTED", "DEFERRED"}:
        return False
    target = _aware(s.targetDate)
    return target is not None and target < (at or _now())


def is_deferral_due(s: BeSuggestion, *, at: datetime | None = None) -> bool:
    """A deferred suggestion whose parking period has expired.

    Without this a DEFER is indistinguishable from a quiet rejection: the record
    sits in DEFERRED forever and nobody is ever prompted to look again.
    """
    if s.status != "DEFERRED":
        return False
    until = _aware(s.deferredUntil)
    return until is not None and until <= (at or _now())


# ─────────────────────────────────────────────────────────────────────────────
# QCC — stage gates
# ─────────────────────────────────────────────────────────────────────────────
QCC_PROJECT_TRANSITIONS: dict[str, dict[str, str]] = {
    "DRAFT": {"CHARTERED": "QCC.CREATE"},
    "CHARTERED": {
        "IN_PROGRESS": "QCC.UPDATE",
        "ABANDONED": "QCC.UPDATE",
        "REJECTED": "QCC.SIGNOFF",
    },
    # COMPLETED additionally requires every stage signed off — a status gate
    # alone would let a circle declare victory at MEASURE.
    "IN_PROGRESS": {"COMPLETED": "QCC.SIGNOFF", "ABANDONED": "QCC.UPDATE"},
    "COMPLETED": {"BENEFIT_VALIDATION": "QCC.UPDATE"},
    # CLOSED additionally requires a VALIDATED benefit. See close_gate_blockers.
    "BENEFIT_VALIDATION": {"CLOSED": "QCC.SIGNOFF"},
    "CLOSED": {},
    "ABANDONED": {},
    "REJECTED": {},
}

QCC_TERMINAL: frozenset[str] = frozenset({"CLOSED", "ABANDONED", "REJECTED"})


def stages_for(methodology: str) -> tuple[str, ...]:
    """The gate sequence for a methodology, defaulting to DMAIC.

    Degrades rather than raises: a project with a typo'd methodology should
    still render a board, not 500 the detail screen.
    """
    return QCC_STAGES_BY_METHODOLOGY.get(methodology, QCC_STAGES_BY_METHODOLOGY["DMAIC"])


def build_project_stages(project: BeQccProject) -> list[BeQccProjectStage]:
    """Materialise the full gate sequence at charter time.

    All of them, up front, including the ones months away — the board has to be
    able to show a circle the whole path with the un-started gates greyed out. A
    board that only renders the stages already reached tells them nothing about
    what is coming.
    """
    return [
        BeQccProjectStage(
            projectId=project.id,
            plantId=project.plantId,
            stage=stage,
            sequence=i,
            status="IN_PROGRESS" if i == 0 else "NOT_STARTED",
            startedAt=_now() if i == 0 else None,
        )
        for i, stage in enumerate(stages_for(project.methodology))
    ]


def rca_stage_for(project: BeQccProject) -> str:
    """The gate that cannot be signed off without a root-cause analysis."""
    return QCC_RCA_STAGE.get(project.methodology, "ANALYZE")


def stage_signoff_blockers(
    project: BeQccProject,
    stage: BeQccProjectStage,
    stages: Sequence[BeQccProjectStage],
) -> list[str]:
    """Why this gate cannot be signed off yet. Empty list means it can.

    Returned as a list of sentences rather than a bool so the UI can disable the
    button AND say why — a disabled control with no reason is the single most
    reported defect on this platform's permit screens.
    """
    blockers: list[str] = []

    if stage.status == "SIGNED_OFF":
        blockers.append(f"{stage.stage.title()} has already been signed off.")
        return blockers

    if project.status not in {"CHARTERED", "IN_PROGRESS"}:
        blockers.append(
            f"A project at {project.status.replace('_', ' ').lower()} is not "
            "running, so its gates cannot be signed off."
        )

    # Gates are sequential. Skipping one would let a circle sign off Control on a
    # project whose measurement was never done.
    earlier_open = [
        s.stage
        for s in stages
        if s.sequence < stage.sequence and s.status not in {"SIGNED_OFF", "SKIPPED"}
    ]
    if earlier_open:
        blockers.append(
            "Sign off "
            + ", ".join(x.title() for x in earlier_open)
            + " before this stage."
        )

    if stage.stage == rca_stage_for(project) and not project.rcaId:
        blockers.append(
            f"{stage.stage.title()} needs a root-cause analysis. Open one from "
            "this project — it is recorded in the platform's shared RCA "
            "register, not here."
        )

    if not (stage.summary or "").strip():
        blockers.append("Record what this stage concluded before signing it off.")

    return blockers


def close_gate_blockers(
    project: BeQccProject,
    stages: Sequence[BeQccProjectStage],
    benefits: Sequence[BeBenefit],
    target: str,
) -> list[str]:
    """Preconditions on the two QCC transitions that need more than a status.

    §6 makes both of these explicit: a project is not complete until its gates
    are, and a benefit is not realised until somebody outside the circle has
    said so.
    """
    blockers: list[str] = []

    if target == "COMPLETED":
        open_stages = [s.stage for s in stages if s.status not in {"SIGNED_OFF", "SKIPPED"}]
        if open_stages:
            blockers.append(
                "These stages are still open: "
                + ", ".join(x.title() for x in open_stages)
                + "."
            )

    if target == "CLOSED":
        if not benefits:
            blockers.append(
                "Record the project's benefit before closing it."
            )
        else:
            unvalidated = [b for b in benefits if b.status != "VALIDATED"]
            if unvalidated:
                blockers.append(
                    f"{len(unvalidated)} of {len(benefits)} benefit line(s) have not "
                    "been validated. A benefit counts only once someone outside "
                    "the circle has confirmed it held."
                )

    return blockers


def allowed_qcc_project_actions(
    project: BeQccProject, granted: set[str]
) -> list[str]:
    return [
        target
        for target, perm in QCC_PROJECT_TRANSITIONS.get(project.status, {}).items()
        if perm in granted
    ]


def project_rag(
    project: BeQccProject,
    stages: Sequence[BeQccProjectStage],
    *,
    at: datetime | None = None,
) -> str:
    """Red/Amber/Green for the circle's project board. Derived, never stored.

    A finished project is GREEN regardless of how it got there — the board shows
    what needs attention, and a closed project needs none. History belongs in the
    dates, not in a permanent red badge.
    """
    now = at or _now()
    if project.status in QCC_TERMINAL or project.status == "BENEFIT_VALIDATION":
        return "GREEN"

    target = _aware(project.targetDate)
    if target is not None and target < now:
        return "RED"

    overdue_stages = [
        s
        for s in stages
        if s.status not in {"SIGNED_OFF", "SKIPPED"}
        and (d := _aware(s.targetDate)) is not None
        and d < now
    ]
    if any(
        (d := _aware(s.targetDate)) is not None and d < now - timedelta(days=_AMBER_DAYS)
        for s in overdue_stages
    ):
        return "RED"
    if overdue_stages:
        return "AMBER"

    if target is not None and target < now + timedelta(days=_AMBER_DAYS):
        # Close to the wire with gates still open.
        if any(s.status not in {"SIGNED_OFF", "SKIPPED"} for s in stages):
            return "AMBER"

    return "GREEN"


def project_progress(stages: Sequence[BeQccProjectStage]) -> dict[str, Any]:
    """Gate counts for the board header."""
    total = len(stages)
    done = sum(1 for s in stages if s.status in {"SIGNED_OFF", "SKIPPED"})
    current = next(
        (s.stage for s in sorted(stages, key=lambda x: x.sequence) if s.status == "IN_PROGRESS"),
        None,
    )
    return {
        "totalStages": total,
        "signedOffStages": done,
        "currentStage": current,
        # None rather than 0 when there are no stages: "not chartered yet" and
        # "chartered and nothing done" are different facts.
        "percent": round(done * 100.0 / total, 1) if total else None,
    }


async def ensure_project_rca(
    db: AsyncSession, project: BeQccProject, *, actor_id: str, methodology: str = "FIVE_WHY"
) -> str:
    """Open (or return) the shared RCA for this project and pin its id.

    §6: "draws on the platform's shared RCA engine — no separate RCA tool or
    duplicate record". `create_problem_rca` is idempotent, so a circle that hits
    the button twice gets one analysis.
    """
    from app.services.rca_core import create_problem_rca

    if project.rcaId:
        return project.rcaId

    rca = await create_problem_rca(
        db,
        source_problem_id=project.id,
        title=f"Root cause — {project.title}",
        methodology=methodology,
        narrative=project.problemStatement,
        actor_id=actor_id,
    )
    await db.flush()
    project.rcaId = rca.id
    return rca.id


async def team_member_ids(
    db: AsyncSession, team_id: str, *, include_past: bool = True
) -> set[str]:
    """Everyone who is, or ever was, in this circle.

    `include_past` defaults to True because the caller that matters most is the
    separation-of-duties check on benefit validation: somebody who left the
    circle last month is still too close to it to sign off its savings.
    """
    stmt = select(BeQccTeamMember.userId).where(BeQccTeamMember.teamId == team_id)
    if not include_past:
        stmt = stmt.where(BeQccTeamMember.leftAt.is_(None))
    return set((await db.execute(stmt)).scalars().all())


# ─────────────────────────────────────────────────────────────────────────────
# SIP
# ─────────────────────────────────────────────────────────────────────────────
SIP_TRANSITIONS: dict[str, dict[str, str]] = {
    "DRAFT": {"SUBMITTED": "SIP.CREATE"},
    "SUBMITTED": {"UNDER_REVIEW": "SIP.APPROVE", "REJECTED": "SIP.APPROVE"},
    "UNDER_REVIEW": {"APPROVED": "SIP.APPROVE", "REJECTED": "SIP.APPROVE"},
    "APPROVED": {"IN_PROGRESS": "SIP.UPDATE", "CANCELLED": "SIP.APPROVE"},
    "IN_PROGRESS": {
        "ON_HOLD": "SIP.UPDATE",
        "COMPLETED": "SIP.UPDATE",
        "CANCELLED": "SIP.APPROVE",
    },
    "ON_HOLD": {"IN_PROGRESS": "SIP.UPDATE", "CANCELLED": "SIP.APPROVE"},
    "COMPLETED": {"BENEFIT_VALIDATION": "SIP.UPDATE"},
    "BENEFIT_VALIDATION": {"CLOSED": "SIP.APPROVE"},
    "CLOSED": {},
    "REJECTED": {},
    "CANCELLED": {},
}

SIP_TERMINAL: frozenset[str] = frozenset({"CLOSED", "REJECTED", "CANCELLED"})


def allowed_sip_actions(sip: BeSip, granted: set[str]) -> list[str]:
    return [
        target
        for target, perm in SIP_TRANSITIONS.get(sip.status, {}).items()
        if perm in granted
    ]


def sip_close_blockers(
    sip: BeSip, milestones: Sequence[BeSipMilestone], benefits: Sequence[BeBenefit], target: str
) -> list[str]:
    """Preconditions on the SIP transitions that need more than a status."""
    blockers: list[str] = []

    if target == "COMPLETED":
        open_ms = [
            m.name
            for m in milestones
            if m.status not in {"COMPLETED", "CANCELLED"}
        ]
        if open_ms:
            blockers.append(
                f"{len(open_ms)} milestone(s) are still open: "
                + ", ".join(open_ms[:4])
                + ("…" if len(open_ms) > 4 else "")
                + "."
            )

    if target == "CLOSED":
        if not benefits:
            blockers.append("Record the project's benefit before closing it.")
        else:
            unvalidated = [b for b in benefits if b.status != "VALIDATED"]
            if unvalidated:
                blockers.append(
                    f"{len(unvalidated)} of {len(benefits)} benefit line(s) have not "
                    "been validated by the designated authority."
                )
        if not (sip.lessonsLearned or "").strip():
            # §7 closure: "with a lessons-learned note captured back into the
            # shared repository". Required, because the note is the only part of
            # a finished project that helps the next one.
            blockers.append("Capture the lessons-learned note before closing.")

    return blockers


def milestone_due(m: BeSipMilestone) -> datetime | None:
    """The date this milestone is actually being held to.

    A revised date supersedes the plan for the purpose of "is it late today",
    while `plannedDate` stays untouched so slippage against the original
    commitment is still recoverable. Both questions are real; they are just
    different questions.
    """
    return _aware(m.revisedDate) or _aware(m.plannedDate)


def is_milestone_late(m: BeSipMilestone, *, at: datetime | None = None) -> bool:
    if m.status in {"COMPLETED", "CANCELLED"}:
        return False
    due = milestone_due(m)
    return due is not None and due < (at or _now())


def sip_rag(
    sip: BeSip, milestones: Sequence[BeSipMilestone], *, at: datetime | None = None
) -> str:
    """Red/Amber/Green rollup for the SIP portfolio. Derived, never stored."""
    now = at or _now()
    if sip.status in SIP_TERMINAL or sip.status in {"COMPLETED", "BENEFIT_VALIDATION"}:
        return "GREEN"
    if sip.status == "ON_HOLD":
        # On hold is not green. A project nobody is working on is exactly the
        # thing a portfolio review exists to surface.
        return "AMBER"

    target = _aware(sip.targetDate)
    if target is not None and target < now:
        return "RED"

    late = [m for m in milestones if is_milestone_late(m, at=now)]
    if any(
        (d := milestone_due(m)) is not None and d < now - timedelta(days=_AMBER_DAYS)
        for m in late
    ):
        return "RED"
    if late or any(m.status == "DELAYED" for m in milestones):
        return "AMBER"

    if target is not None and target < now + timedelta(days=_AMBER_DAYS):
        if any(m.status not in {"COMPLETED", "CANCELLED"} for m in milestones):
            return "AMBER"

    return "GREEN"


def sip_metric_progress(sip: BeSip) -> dict[str, Any]:
    """How far the tracked metric has moved from baseline toward target.

    Direction-agnostic: a SIP that drives scrap DOWN and one that drives OTIF UP
    both read as a percentage of the distance covered, because the sign of
    (target − baseline) tells us which way "better" is. Returns None rather than
    0 when baseline and target are equal or missing — a percentage of a zero
    span is not 0%, it is undefined, and rendering it as 0 reads as failure.
    """
    baseline, target, actual = sip.baselineValue, sip.targetValue, sip.latestActualValue
    if baseline is None or target is None or actual is None:
        return {"percent": None, "direction": None, "onTrack": None}
    span = target - baseline
    if span == 0:
        return {"percent": None, "direction": None, "onTrack": None}
    covered = (actual - baseline) / span
    return {
        "percent": round(max(-100.0, min(200.0, covered * 100.0)), 1),
        "direction": "INCREASE" if span > 0 else "DECREASE",
        "onTrack": covered >= 0.0,
    }


def milestone_summary(milestones: Sequence[BeSipMilestone]) -> dict[str, Any]:
    rows = list(milestones)
    total = len(rows)
    done = sum(1 for m in rows if m.status == "COMPLETED")
    return {
        "total": total,
        "completed": done,
        "late": sum(1 for m in rows if is_milestone_late(m)),
        "percent": round(done * 100.0 / total, 1) if total else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Benefit realisation
# ─────────────────────────────────────────────────────────────────────────────
def validation_due_from(window_months: int | None, *, start: datetime | None = None) -> datetime | None:
    """When the follow-up check falls due. None when no window was set."""
    if not window_months:
        return None
    days = VALIDATION_WINDOW_DAYS.get(window_months, window_months * 30)
    return (start or _now()) + timedelta(days=days)


def is_validation_due(b: BeBenefit, *, at: datetime | None = None) -> bool:
    """The window has elapsed and nobody has validated it yet."""
    if b.status in {"VALIDATED", "REJECTED", "LAPSED"}:
        return False
    due = _aware(b.validationDueAt)
    return due is not None and due <= (at or _now())


async def validation_blockers(
    db: AsyncSession, b: BeBenefit, *, actor_id: str
) -> list[str]:
    """Why this person may not validate this benefit. Empty means they may.

    THIS IS THE SEPARATION-OF-DUTIES GATE. §6: benefit sign-off must be
    "separate from the circle's own reporting"; §7: "validated by a designated
    authority". The rule is enforced here, once, for all six workflows rather
    than re-implemented per register — which is how one of them ends up without
    it.

    The exclusion widens with how collective the work was:
      * every source — the person who recorded the claim may not confirm it
      * QCC — nobody who was ever in the circle, including people who have left
      * SIP — not the owner and not the sponsor; a sponsor signing off the
        benefit of the project they championed is the conflict this rule exists
        to prevent
    """
    blockers: list[str] = []

    if b.status == "VALIDATED":
        blockers.append("This benefit has already been validated.")
        return blockers
    if b.status in {"REJECTED", "LAPSED"}:
        blockers.append(f"A {b.status.lower()} benefit line cannot be validated.")
        return blockers
    if b.realizedValue is None:
        blockers.append("Record the realised value before validating it.")

    if actor_id == b.createdById:
        blockers.append(
            "The benefit must be validated by someone other than the person who "
            "recorded it."
        )

    if b.validatingAuthorityId and actor_id != b.validatingAuthorityId:
        blockers.append(
            "This benefit is assigned to a nominated validating authority."
        )

    if b.sourceType == "QCC":
        project = await db.get(BeQccProject, b.sourceId)
        if project is not None:
            members = await team_member_ids(db, project.teamId, include_past=True)
            if actor_id in members or actor_id == project.createdById:
                blockers.append(
                    "A circle cannot validate its own benefit. Sign-off must come "
                    "from outside the team."
                )
    elif b.sourceType == "SIP":
        sip = await db.get(BeSip, b.sourceId)
        if sip is not None and actor_id in {sip.ownerId, sip.sponsorId, sip.createdById}:
            blockers.append(
                "A project's owner or sponsor cannot validate its own benefit."
            )
    elif b.sourceType == "KAIZEN":
        from app.models.business_excellence import BeKaizen

        kaizen = await db.get(BeKaizen, b.sourceId)
        if kaizen is not None and actor_id == kaizen.createdById:
            blockers.append(
                "Savings must be validated by someone other than the person who "
                "raised the idea."
            )
    elif b.sourceType == "SUGGESTION":
        suggestion = await db.get(BeSuggestion, b.sourceId)
        if suggestion is not None and actor_id == suggestion.createdById:
            blockers.append(
                "Savings must be validated by someone other than the submitter."
            )

    return blockers


async def benefits_for(
    db: AsyncSession, *, source_type: str, source_id: str
) -> list[BeBenefit]:
    return list(
        (
            await db.execute(
                select(BeBenefit).where(
                    BeBenefit.sourceType == source_type,
                    BeBenefit.sourceId == source_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def record_reading(
    db: AsyncSession,
    benefit: BeBenefit,
    *,
    actual_value: float,
    target_value: float | None,
    period_label: str | None,
    note: str | None,
    actor_id: str,
) -> BeBenefitReading:
    """Append one measurement and refresh the SIP's denormalised latest value.

    Both writes happen here so they cannot diverge. `BeSip.latestActualValue` is
    a cache for the portfolio list; this is the only function allowed to move it.
    """
    reading = BeBenefitReading(
        benefitId=benefit.id,
        plantId=benefit.plantId,
        actualValue=actual_value,
        targetValue=target_value,
        periodLabel=period_label,
        note=note,
        recordedById=actor_id,
    )
    db.add(reading)
    await db.flush()

    if benefit.sourceType == "SIP":
        sip = await db.get(BeSip, benefit.sourceId)
        if sip is not None:
            latest = _aware(sip.latestReadingAt)
            taken = _aware(reading.readingAt) or _now()
            # Only move the cache forward. Back-filling last quarter's figure
            # must not overwrite this month's.
            if latest is None or taken >= latest:
                sip.latestActualValue = actual_value
                sip.latestReadingAt = taken

    return reading


def summarise_benefits(benefits: Iterable[BeBenefit]) -> dict[str, Any]:
    """Rollup for a register row or a dashboard tile.

    Financial and non-financial totals are kept apart on purpose: adding rupees
    to parts-per-million produces a number that looks like a benefit and means
    nothing. §7 asks for both to be captured, not for both to be summed.
    """
    rows = list(benefits)
    financial = [b for b in rows if b.valueKind == "FINANCIAL"]
    validated = [b for b in rows if b.status == "VALIDATED"]
    return {
        "lines": len(rows),
        "validatedLines": len(validated),
        "pendingValidation": sum(1 for b in rows if b.status == "PENDING_VALIDATION"),
        "overdueValidation": sum(1 for b in rows if is_validation_due(b)),
        "projectedFinancial": sum(b.projectedValue or 0.0 for b in financial) or None,
        # Only VALIDATED lines count toward realised. This is the whole point of
        # the validation window — a realised total that includes unvalidated
        # claims is the projected total wearing a different label.
        "realisedFinancial": sum(
            b.realizedValue or 0.0 for b in financial if b.status == "VALIDATED"
        )
        or None,
        "nonFinancialLines": len(rows) - len(financial),
    }


# ─────────────────────────────────────────────────────────────────────────────
# The workflow bridge (delegated to from Phase 1's sync_be_record_status)
# ─────────────────────────────────────────────────────────────────────────────
_ON_APPROVED: dict[str, str] = {
    WF_MODULE_SUGGESTION: "SCREENING",
    WF_MODULE_QCC: "CHARTERED",
    WF_MODULE_SIP: "APPROVED",
}
_ON_REJECTED: dict[str, str] = {
    WF_MODULE_SUGGESTION: "REJECTED",
    WF_MODULE_QCC: "REJECTED",
    WF_MODULE_SIP: "REJECTED",
}
_IN_CHAIN: dict[str, tuple[str, str]] = {
    # module → (status it may be dragged out of, status it lands in) while the
    # approval chain is still running. Only DRAFT is ever moved: a record already
    # further along must not be pulled backwards by a mid-chain advance.
    WF_MODULE_SUGGESTION: ("DRAFT", "SUBMITTED"),
    WF_MODULE_QCC: ("DRAFT", "DRAFT"),
    WF_MODULE_SIP: ("DRAFT", "SUBMITTED"),
}
_MODEL_FOR: dict[str, type] = {
    WF_MODULE_SUGGESTION: BeSuggestion,
    WF_MODULE_QCC: BeQccProject,
    WF_MODULE_SIP: BeSip,
}

#: Never resurrect a record in one of these. A repair job that recreates a
#: closure task must not reopen something already finished.
_TERMINAL = SUGGESTION_TERMINAL | QCC_TERMINAL | SIP_TERMINAL


async def sync_p2_record_status(
    db: AsyncSession,
    *,
    module: str,
    record_id: str,
    instance_completed: bool,
    rejected: bool = False,
    reason: str | None = None,
) -> bool:
    """Land a workflow decision on a Phase 2 BE record. True if it applied.

    Called only from Phase 1's `sync_be_record_status`, which is itself only ever
    called inside a SAVEPOINT. Returns False rather than raising for anything it
    does not own.
    """
    if module not in P2_WORKFLOW_MODULES:
        return False

    record = await db.get(_MODEL_FOR[module], record_id)
    if record is None:
        return False

    if rejected:
        record.status = _ON_REJECTED[module]
        if reason:
            record.rejectionReason = reason
        await db.flush()
        return True

    if not instance_completed:
        from_status, to_status = _IN_CHAIN[module]
        if record.status == from_status and to_status != from_status:
            record.status = to_status
            await db.flush()
            return True
        return False

    if record.status in _TERMINAL:
        return False

    record.status = _ON_APPROVED[module]
    if module == WF_MODULE_QCC and record.charteredAt is None:
        record.charteredAt = _now()
    await db.flush()
    return True


__all__ = [
    "P2_WORKFLOW_MODULES",
    "QCC_PROJECT_TRANSITIONS",
    "QCC_TERMINAL",
    "SIP_TERMINAL",
    "SIP_TRANSITIONS",
    "SUGGESTION_TERMINAL",
    "SUGGESTION_TRANSITIONS",
    "WF_MODULE_QCC",
    "WF_MODULE_SIP",
    "WF_MODULE_SUGGESTION",
    "allowed_qcc_project_actions",
    "allowed_sip_actions",
    "allowed_suggestion_actions",
    "benefits_for",
    "build_project_stages",
    "close_gate_blockers",
    "ensure_project_rca",
    "is_deferral_due",
    "is_milestone_late",
    "is_suggestion_overdue",
    "is_validation_due",
    "milestone_due",
    "milestone_summary",
    "project_progress",
    "project_rag",
    "rca_stage_for",
    "record_reading",
    "sip_close_blockers",
    "sip_metric_progress",
    "sip_rag",
    "stage_signoff_blockers",
    "stages_for",
    "submitter_visible_to",
    "suggestion_transition_permitted",
    "summarise_benefits",
    "sync_p2_record_status",
    "team_member_ids",
    "validation_blockers",
    "validation_due_from",
]
