"""IMS / MIS service: register state rules and the dashboard aggregate.

Two halves:

* Pure helpers (period bounds, state transitions, overdue rules, the rate
  block). They take plain values and have no DB access, so tests run them
  offline (tests/test_ims.py).
* ``dashboard()``: one plant-scoped aggregate over every indicator in the JSL
  Hisar MIS list. It counts other modules' tables; it never copies their data.

Rates come only from ``services.frequency_rates`` over ``Manhours`` totals
(that module's rule 3: sum numerators and exposure, then divide once). Never
average monthly rates here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import frequency_rates as fr

# ── vocabularies ─────────────────────────────────────────────────────────────
RECIPIENT_TYPES = ("EMPLOYEE", "CONTRACTOR_WORKER", "CONTRACTOR_COMPANY")
VIOLATION_CATEGORIES = (
    "PPE_NON_COMPLIANCE", "PTW_VIOLATION", "LOTO_VIOLATION", "WORK_AT_HEIGHT", "UNSAFE_ACT",
    "HOUSEKEEPING", "TRAFFIC_RULES", "SAFETY_DEVICE_BYPASS", "GAS_SAFETY", "OTHER",
)
NOTICE_DECISIONS = ("NO_ACTION", "WARNING", "PENALTY", "SUSPENSION", "TERMINATION")
NOTICE_STATUSES = ("ISSUED", "REPLY_RECEIVED", "CLOSED")
SOURCE_TYPES = ("OBSERVATION", "PERMIT", "INCIDENT", "NEAR_MISS", "INSPECTION")

MEETING_TYPES = (
    "SAFETY_COMMITTEE", "APEX_SAFETY_COMMITTEE", "DEPARTMENT_SAFETY", "CONTRACTOR_SAFETY",
    "MANAGEMENT_REVIEW", "OTHER",
)
MEETING_STATUSES = ("SCHEDULED", "HELD", "MINUTES_PUBLISHED", "CANCELLED")
ACTION_STATUSES = ("OPEN", "IN_PROGRESS", "CLOSED", "DROPPED")
ACTION_OPEN = ("OPEN", "IN_PROGRESS")

DRILL_TYPES = ("FIRE_EVACUATION", "GAS_LEAK", "CHEMICAL_SPILL", "MOLTEN_METAL", "MEDICAL_EMERGENCY", "BOMB_THREAT", "TABLETOP", "OTHER")

PERIODS = ("MTD", "FYTD", "L30", "L90", "L365")

# Allowed state moves. Anything not listed is refused with a 409.
_MEETING_MOVES = {
    "SCHEDULED": {"HELD", "CANCELLED"},
    "HELD": {"MINUTES_PUBLISHED"},
    "MINUTES_PUBLISHED": set(),
    "CANCELLED": set(),
}
_ACTION_MOVES = {
    "OPEN": {"IN_PROGRESS", "CLOSED", "DROPPED"},
    "IN_PROGRESS": {"OPEN", "CLOSED", "DROPPED"},
    "CLOSED": {"OPEN"},  # reopen if verification fails
    "DROPPED": {"OPEN"},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def period_bounds(period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) for a dashboard period key.

    FYTD is the Indian financial year (1 April to 31 March), which is how the
    plant MIS is reported. MTD starts on the 1st of the current month.
    """
    now = now or utcnow()
    key = (period or "FYTD").upper()
    end = now
    if key == "MTD":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif key == "FYTD":
        fy_year = now.year if now.month >= 4 else now.year - 1
        start = now.replace(year=fy_year, month=4, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif key in ("L30", "L90", "L365"):
        start = now - timedelta(days=int(key[1:]))
    else:
        raise ValueError(f"Unknown period '{period}'. Use one of {', '.join(PERIODS)}")
    return start, end


def months_in_window(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Every (year, month) the window touches, inclusive of partial months."""
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def can_move_meeting(current: str, target: str) -> bool:
    return target in _MEETING_MOVES.get(current, set())


def can_move_action(current: str, target: str) -> bool:
    return target in _ACTION_MOVES.get(current, set())


def notice_reply_overdue(status: str, reply_due: datetime | None, now: datetime | None = None) -> bool:
    """A notice is overdue only while it waits for the recipient's reply."""
    if status != "ISSUED" or reply_due is None:
        return False
    return _aware(reply_due) < (now or utcnow())


def action_overdue(status: str, due: datetime | None, now: datetime | None = None) -> bool:
    if status not in ACTION_OPEN or due is None:
        return False
    return _aware(due) < (now or utcnow())


def notice_close_blockers(*, status: str, decision: str | None, penalty_amount: float | None) -> list[str]:
    """Reasons a show-cause notice cannot be closed yet."""
    out: list[str] = []
    if status == "CLOSED":
        out.append("Notice is already closed.")
    if not decision:
        out.append("Record a decision before closing.")
    elif decision not in NOTICE_DECISIONS:
        out.append(f"Unknown decision '{decision}'.")
    elif decision == "PENALTY" and not (penalty_amount and penalty_amount > 0):
        out.append("A PENALTY decision needs a penalty amount.")
    return out


def minutes_publish_blockers(*, status: str, minutes: str | None, action_count: int) -> list[str]:
    """Reasons a meeting's minutes cannot be published yet. A committee meeting
    with no recorded minutes is not a held meeting in any audit's eyes."""
    out: list[str] = []
    if status != "HELD":
        out.append("Only a held meeting can publish minutes.")
    if not (minutes and minutes.strip()):
        out.append("Minutes text is empty.")
    _ = action_count  # zero actions is allowed: some meetings decide nothing new
    return out


def rate_block(*, hours: float, lti: int, fatalities: int, lost_days: int, mtc: int, rwc: int, fac: int) -> dict[str, Any]:
    """Frequency/severity block from summed Manhours totals. None = unmeasured."""
    exposure = hours if hours > 0 else None
    ltifr_v = fr.ltifr(lti=lti, fatalities=fatalities, exposure_hours=exposure)
    sr_v = fr.severity_rate(lost_days=lost_days, fatalities=fatalities, exposure_hours=exposure)
    return {
        "manHours": hours,
        "ltiCount": lti,
        "fatalityCount": fatalities,
        "lostDays": lost_days,
        "recordableCount": lti + mtc + rwc + fatalities,
        "firstAidCount": fac,
        "frequencyRate": _r(ltifr_v),
        "severityRate": _r(sr_v),
        "fsi": _r(fr.fsi(ltifr_v, sr_v)),
        "trifr": _r(fr.trifr(lti=lti, mtc=mtc, rwc=rwc, fatalities=fatalities, exposure_hours=exposure)),
    }


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── dashboard aggregate ──────────────────────────────────────────────────────
async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


async def dashboard(db: AsyncSession, *, plant_ids: list[str] | None, start: datetime, end: datetime) -> dict[str, Any]:
    """Every MIS indicator for the plants in ``plant_ids`` (None = all plants)
    over [start, end). Point-in-time figures (open, overdue) are as of now."""
    from app.models.cams import CamsEngagement
    from app.models.equipment import Inspection
    from app.models.fire_safety import FireDrill, FireInspection
    from app.models.flra import FLRA
    from app.models.hira import HiraEntry, HiraReviewCycle, HiraStudy
    from app.models.incident import Incident
    from app.models.ims import MeetingActionItem, SafetyMeeting, ShowCauseNotice, ToolboxTalk
    from app.models.manhours import Manhours
    from app.models.near_miss import NearMiss
    from app.models.observation import Observation
    from app.models.permit import Permit
    from app.models.training import TrainingSchedule

    now = utcnow()

    def plant(col):
        return True if plant_ids is None else col.in_(plant_ids or ["__none__"])

    def window(col):
        return and_(col >= start, col < end)

    # Manpower + rates (Manhours is the canonical exposure source)
    months = months_in_window(start, end)
    mh_rows = (
        await db.execute(
            select(Manhours).where(plant(Manhours.plantId)).where(
                or_(*[and_(Manhours.year == y, Manhours.month == m) for (y, m) in months])
            )
        )
    ).scalars().all() if months else []
    hours = sum((r.employeeHours or 0) + (r.contractorHours or 0) for r in mh_rows)
    latest_by_plant: dict[str, Any] = {}
    for r in mh_rows:
        cur = latest_by_plant.get(r.plantId)
        if cur is None or (r.year, r.month) > (cur.year, cur.month):
            latest_by_plant[r.plantId] = r
    rates = rate_block(
        hours=hours,
        lti=sum(r.ltiCount or 0 for r in mh_rows),
        fatalities=sum(r.fatalityCount or 0 for r in mh_rows),
        lost_days=sum(r.lostDays or 0 for r in mh_rows),
        mtc=sum(r.mtcCount or 0 for r in mh_rows),
        rwc=sum(r.rwcCount or 0 for r in mh_rows),
        fac=sum(r.facCount or 0 for r in mh_rows),
    )
    manpower = {
        "headcount": sum(r.headcount or 0 for r in latest_by_plant.values()),
        "manHours": hours,
        "employeeHours": sum(r.employeeHours or 0 for r in mh_rows),
        "contractorHours": sum(r.contractorHours or 0 for r in mh_rows),
        "monthsReported": len(mh_rows),
    }

    # Trainings (internal vs external)
    tr_base = (
        select(func.count()).select_from(TrainingSchedule)
        .where(plant(TrainingSchedule.plantId)).where(window(TrainingSchedule.startDate))
        .where(TrainingSchedule.status.notin_(("DRAFT", "CANCELLED")))
    )
    trainings_total = await _count(db, tr_base)
    trainings_external = await _count(db, tr_base.where(TrainingSchedule.isExternalTrainer.is_(True)))
    trainings_completed = await _count(db, tr_base.where(TrainingSchedule.status == "COMPLETED"))

    # Toolbox talks (register + FLRA pre-job TBTs)
    tbt_q = select(
        func.count(), func.coalesce(func.sum(ToolboxTalk.employeeCount + ToolboxTalk.contractorCount), 0)
    ).where(ToolboxTalk.isDeleted.is_(False)).where(plant(ToolboxTalk.plantId)).where(window(ToolboxTalk.conductedAt))
    tbt_count, tbt_people = (await db.execute(tbt_q)).one()
    flra_tbt = await _count(
        db,
        select(func.count()).select_from(FLRA).where(plant(FLRA.plantId))
        .where(FLRA.toolboxTalkConducted.is_(True)).where(window(FLRA.toolboxTalkConductedAt)),
    )

    # Inspections (equipment inspections; fire inspections run as CAMS engagements below)
    insp_done = await _count(
        db, select(func.count()).select_from(Inspection).where(plant(Inspection.plantId))
        .where(Inspection.status == "COMPLETED").where(window(Inspection.completedDate)),
    )
    insp_sched = await _count(
        db, select(func.count()).select_from(Inspection).where(plant(Inspection.plantId)).where(window(Inspection.scheduledDate)),
    )
    insp_overdue = await _count(
        db, select(func.count()).select_from(Inspection).where(plant(Inspection.plantId))
        .where(Inspection.status.notin_(("COMPLETED", "SKIPPED"))).where(Inspection.scheduledDate < now),
    )

    # Audits = CAMS engagements (schedule & closure); 'inspection' engagements counted separately
    cams_live = and_(CamsEngagement.isDeleted.is_(False), CamsEngagement.status != "CANCELLED")
    audit_kind = CamsEngagement.engagementType != "inspection"
    audits_planned = await _count(
        db, select(func.count()).select_from(CamsEngagement).where(cams_live).where(audit_kind)
        .where(plant(CamsEngagement.siteId)).where(window(CamsEngagement.plannedDate)),
    )
    audits_closed = await _count(
        db, select(func.count()).select_from(CamsEngagement).where(cams_live).where(audit_kind)
        .where(plant(CamsEngagement.siteId)).where(window(CamsEngagement.plannedDate))
        .where(CamsEngagement.status.in_(("REPORT_ISSUED", "CLOSED"))),
    )
    audits_overdue = await _count(
        db, select(func.count()).select_from(CamsEngagement).where(cams_live).where(audit_kind)
        .where(plant(CamsEngagement.siteId)).where(CamsEngagement.status.in_(("PLANNED", "SCHEDULED")))
        .where(CamsEngagement.plannedDate < now),
    )
    fire_insp = await _count(
        db, select(func.count()).select_from(CamsEngagement).where(cams_live)
        .where(CamsEngagement.sourceModule == "FIRE").where(plant(CamsEngagement.siteId))
        .where(window(CamsEngagement.plannedDate)),
    )
    # QR checklist inspections (FireInspection rows are written only on completion)
    fire_insp += await _count(
        db, select(func.count()).select_from(FireInspection).where(plant(FireInspection.plantId))
        .where(window(FireInspection.completedAt)),
    )

    # Show cause notices
    scn = ShowCauseNotice
    scn_live = and_(scn.isDeleted.is_(False), plant(scn.plantId))
    scn_issued = await _count(db, select(func.count()).select_from(scn).where(scn_live).where(window(scn.issuedAt)))
    scn_open = await _count(db, select(func.count()).select_from(scn).where(scn_live).where(scn.status != "CLOSED"))
    scn_overdue = await _count(
        db, select(func.count()).select_from(scn).where(scn_live).where(scn.status == "ISSUED").where(scn.replyDueDate < now),
    )
    scn_closed = await _count(db, select(func.count()).select_from(scn).where(scn_live).where(window(scn.closedAt)))

    # HIRA reviews (cycle → entry → study.plantId)
    hira_join = (
        select(func.count()).select_from(HiraReviewCycle)
        .join(HiraEntry, HiraEntry.id == HiraReviewCycle.entryId)
        .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
        .where(plant(HiraStudy.plantId))
    )
    hira_reviewed = await _count(db, hira_join.where(HiraReviewCycle.status == "COMPLETED").where(window(HiraReviewCycle.completedAt)))
    hira_overdue = await _count(
        db, hira_join.where(HiraReviewCycle.status.in_(("SCHEDULED", "IN_PROGRESS"))).where(HiraReviewCycle.scheduledFor < now),
    )
    hira_due30 = await _count(
        db, hira_join.where(HiraReviewCycle.status.in_(("SCHEDULED", "IN_PROGRESS")))
        .where(HiraReviewCycle.scheduledFor >= now).where(HiraReviewCycle.scheduledFor < now + timedelta(days=30)),
    )

    # Meetings + MOM tracker
    sm = SafetyMeeting
    sm_live = and_(sm.isDeleted.is_(False), plant(sm.plantId), window(sm.scheduledAt))
    mtg_scheduled = await _count(db, select(func.count()).select_from(sm).where(sm_live).where(sm.status != "CANCELLED"))
    mtg_held = await _count(db, select(func.count()).select_from(sm).where(sm_live).where(sm.status.in_(("HELD", "MINUTES_PUBLISHED"))))
    committee_held = await _count(
        db, select(func.count()).select_from(sm).where(sm_live).where(sm.status.in_(("HELD", "MINUTES_PUBLISHED")))
        .where(sm.meetingType.in_(("SAFETY_COMMITTEE", "APEX_SAFETY_COMMITTEE"))),
    )
    mai = MeetingActionItem
    mai_live = and_(mai.isDeleted.is_(False), plant(mai.plantId))
    mom_open = await _count(db, select(func.count()).select_from(mai).where(mai_live).where(mai.status.in_(ACTION_OPEN)))
    mom_overdue = await _count(
        db, select(func.count()).select_from(mai).where(mai_live).where(mai.status.in_(ACTION_OPEN)).where(mai.dueDate < now),
    )
    mom_closed = await _count(db, select(func.count()).select_from(mai).where(mai_live).where(window(mai.closedAt)))

    # Mock drills
    fd_live = and_(FireDrill.isDeleted.is_(False), plant(FireDrill.plantId))
    drills_sched = await _count(
        db, select(func.count()).select_from(FireDrill).where(fd_live).where(window(FireDrill.scheduledDate))
        .where(FireDrill.status != "CANCELLED"),
    )
    drills_done = await _count(
        db, select(func.count()).select_from(FireDrill).where(fd_live).where(FireDrill.status == "COMPLETED")
        .where(window(FireDrill.conductedDate)),
    )
    drills_overdue = await _count(
        db, select(func.count()).select_from(FireDrill).where(fd_live).where(FireDrill.status == "PLANNED")
        .where(FireDrill.scheduledDate < now),
    )
    drills_upcoming = await _count(
        db, select(func.count()).select_from(FireDrill).where(fd_live).where(FireDrill.status == "PLANNED")
        .where(FireDrill.scheduledDate >= now).where(FireDrill.scheduledDate < now + timedelta(days=30)),
    )

    # Leading / lagging operational counts
    obs_reported = await _count(
        db, select(func.count()).select_from(Observation).where(plant(Observation.plantId)).where(window(Observation.createdAt)),
    )
    obs_open = await _count(
        db, select(func.count()).select_from(Observation).where(plant(Observation.plantId)).where(Observation.status != "CLOSED"),
    )
    nm_reported = await _count(
        db, select(func.count()).select_from(NearMiss).where(plant(NearMiss.plantId)).where(window(NearMiss.createdAt)),
    )
    inc_q = (
        select(Incident.type, func.count()).where(Incident.isDeleted.is_(False))
        .where(plant(Incident.plantId)).where(window(Incident.date)).group_by(Incident.type)
    )
    incidents_by_type = {
        (t.value if hasattr(t, "value") else str(t)): int(n) for t, n in (await db.execute(inc_q)).all()
    }
    permits_live = await _count(
        db, select(func.count()).select_from(Permit).where(Permit.isDeleted.is_(False)).where(plant(Permit.plantId))
        .where(Permit.status.in_(("ISSUED", "ACTIVE", "SUSPENDED"))),
    )
    permits_issued = await _count(
        db, select(func.count()).select_from(Permit).where(Permit.isDeleted.is_(False)).where(plant(Permit.plantId))
        .where(window(Permit.validFrom)).where(Permit.status.notin_(("DRAFT", "REJECTED", "CANCELLED"))),
    )

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "generatedAt": now.isoformat(),
        "lagging": {**rates, "incidentsByType": incidents_by_type, "incidentsTotal": sum(incidents_by_type.values())},
        "manpower": manpower,
        "trainings": {"total": trainings_total, "completed": trainings_completed,
                      "external": trainings_external, "internal": trainings_total - trainings_external},
        "toolboxTalks": {"register": int(tbt_count), "participants": int(tbt_people), "preJobFlra": flra_tbt,
                         "total": int(tbt_count) + flra_tbt},
        "inspections": {"completed": insp_done, "scheduled": insp_sched, "overdue": insp_overdue, "fireInspections": fire_insp},
        "audits": {"planned": audits_planned, "closed": audits_closed, "overdue": audits_overdue},
        "showCause": {"issued": scn_issued, "open": scn_open, "replyOverdue": scn_overdue, "closed": scn_closed},
        "hira": {"reviewed": hira_reviewed, "overdue": hira_overdue, "dueNext30": hira_due30},
        "meetings": {"scheduled": mtg_scheduled, "held": mtg_held, "committeeHeld": committee_held},
        "mom": {"open": mom_open, "overdue": mom_overdue, "closed": mom_closed},
        "mockDrills": {"scheduled": drills_sched, "completed": drills_done, "overdue": drills_overdue, "upcoming30": drills_upcoming},
        "observations": {"reported": obs_reported, "open": obs_open},
        "nearMisses": {"reported": nm_reported},
        "permits": {"live": permits_live, "issued": permits_issued},
    }
