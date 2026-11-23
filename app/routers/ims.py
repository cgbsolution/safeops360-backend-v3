"""IMS / MIS router (JSL Hisar requirement #8). Mounts at /api/ims.

Registers: toolbox talks, show cause notices, safety meetings with the MOM
action tracker. Plus GET /dashboard, the single plant-scoped aggregate for the
IMS/MIS screen. Mock drills are scheduled through /api/fire/drills.

RBAC (granted by safeops_360/prisma/apply-ims-permissions.ts):
  IMS.READ    view registers and the dashboard (plant-scoped via build_query_scope)
  IMS.CREATE  record a TBT, issue a notice, schedule a meeting, add an action
  IMS.UPDATE  record a reply, mark a meeting held / edit minutes, move an action
  IMS.CLOSE   close a notice with a decision, publish minutes
An action item's owner may always move their own item; that is the point of a
MOM tracker.

Mounted ungated (same reasoning as fire_safety / capture): a new licence module
code would 403 under the signed dev licence that predates it. Per-endpoint RBAC
is the gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.ims import MeetingActionItem, SafetyMeeting, ShowCauseNotice, ToolboxTalk
from app.models.user import User
from app.services import ims as svc
from app.services.access_scope import build_query_scope
from app.services.numbering import next_number
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names
from app.services.user_directory import resolve_user_directory

router = APIRouter(prefix="/api/ims", tags=["ims"])

READ, CREATE, UPDATE, CLOSE = "IMS.READ", "IMS.CREATE", "IMS.UPDATE", "IMS.CLOSE"


# ── helpers ──────────────────────────────────────────────────────────────────
async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _scoped_plants(db: AsyncSession, user: User, plant_id: str | None) -> list[str] | None:
    """Plant filter for reads: None = all plants. A requested plant outside the
    caller's scope is refused, not silently emptied."""
    await _require(db, user, READ)
    scope = await build_query_scope(db, user.id, READ)
    if plant_id:
        if not scope.allows_plant(plant_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant is outside your access scope")
        return [plant_id]
    return None if scope.all_plants else scope.plant_ids


def _plant_where(col, plants: list[str] | None):
    return True if plants is None else col.in_(plants or ["__none__"])


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


async def _names(db: AsyncSession, user_ids, plant_ids) -> tuple[dict[str, str], dict[str, str]]:
    users = await resolve_user_directory(db, user_ids)
    plants = await resolve_plant_names(db, plant_ids)
    return {k: v.name for k, v in users.items()}, plants


async def _get_live(db: AsyncSession, model, rid: str):
    row = await db.get(model, rid)
    if row is None or getattr(row, "isDeleted", False):
        raise HTTPException(404, f"{model.__name__} not found")
    return row


def _check_choice(value: str | None, allowed: tuple[str, ...], field: str) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(422, f"{field} must be one of {', '.join(allowed)}")


# ── meta + dashboard ─────────────────────────────────────────────────────────
@router.get("/meta")
async def meta(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "recipientTypes": svc.RECIPIENT_TYPES, "violationCategories": svc.VIOLATION_CATEGORIES,
        "noticeDecisions": svc.NOTICE_DECISIONS, "noticeStatuses": svc.NOTICE_STATUSES,
        "sourceTypes": svc.SOURCE_TYPES, "meetingTypes": svc.MEETING_TYPES,
        "meetingStatuses": svc.MEETING_STATUSES, "actionStatuses": svc.ACTION_STATUSES,
        "drillTypes": svc.DRILL_TYPES, "periods": svc.PERIODS,
    }


@router.get("/dashboard")
async def dashboard(
    plantId: str | None = Query(None), period: str = Query("FYTD"),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    try:
        start, end = svc.period_bounds(period)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    data = await svc.dashboard(db, plant_ids=plants, start=start, end=end)
    return {**data, "period": period.upper(), "plantId": plantId}


# ── Toolbox talks ────────────────────────────────────────────────────────────
class TbtIn(BaseModel):
    plantId: str
    topic: str = Field(min_length=3, max_length=200)
    conductedAt: datetime
    department: str | None = None
    areaId: str | None = None
    keyPoints: str | None = None
    language: str | None = None
    durationMinutes: int | None = Field(None, ge=1, le=240)
    employeeCount: int = Field(0, ge=0, le=5000)
    contractorCount: int = Field(0, ge=0, le=5000)
    contractorCompanyId: str | None = None
    permitId: str | None = None
    conductedById: str | None = None


def _tbt(t: ToolboxTalk, users: dict[str, str], plants: dict[str, str]) -> dict[str, Any]:
    return {
        "id": t.id, "talkNo": t.talkNo, "plantId": t.plantId, "plantName": plants.get(t.plantId),
        "topic": t.topic, "keyPoints": t.keyPoints, "department": t.department, "language": t.language,
        "conductedAt": _iso(t.conductedAt), "conductedById": t.conductedById,
        "conductedByName": users.get(t.conductedById), "durationMinutes": t.durationMinutes,
        "employeeCount": t.employeeCount, "contractorCount": t.contractorCount,
        "participants": (t.employeeCount or 0) + (t.contractorCount or 0), "permitId": t.permitId,
    }


@router.get("/toolbox-talks")
async def list_tbts(
    plantId: str | None = Query(None), q: str | None = Query(None), days: int = Query(90, ge=1, le=730),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    stmt = (
        select(ToolboxTalk).where(ToolboxTalk.isDeleted.is_(False)).where(_plant_where(ToolboxTalk.plantId, plants))
        .where(ToolboxTalk.conductedAt >= svc.utcnow() - timedelta(days=days))
    )
    if q:
        stmt = stmt.where(or_(ToolboxTalk.topic.ilike(f"%{q}%"), ToolboxTalk.talkNo.ilike(f"%{q}%")))
    rows = (await db.execute(stmt.order_by(ToolboxTalk.conductedAt.desc()).limit(500))).scalars().all()
    users, pl = await _names(db, [r.conductedById for r in rows], [r.plantId for r in rows])
    return {"items": [_tbt(r, users, pl) for r in rows], "total": len(rows)}


@router.post("/toolbox-talks", status_code=201)
async def create_tbt(body: TbtIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, CREATE, body.plantId)
    if body.employeeCount + body.contractorCount == 0:
        raise HTTPException(422, "Record at least one participant")
    if body.conductedAt > svc.utcnow() + timedelta(hours=1):
        raise HTTPException(422, "A toolbox talk cannot be recorded in the future")
    no = await next_number(db, column=ToolboxTalk.talkNo, stem=f"TBT-{body.conductedAt.year}-", width=5)
    t = ToolboxTalk(
        talkNo=no, plantId=body.plantId, areaId=body.areaId, department=body.department, topic=body.topic.strip(),
        keyPoints=body.keyPoints, language=body.language, conductedAt=body.conductedAt,
        conductedById=body.conductedById or user.id, durationMinutes=body.durationMinutes,
        employeeCount=body.employeeCount, contractorCount=body.contractorCount,
        contractorCompanyId=body.contractorCompanyId, permitId=body.permitId, createdBy=user.id,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    users, pl = await _names(db, [t.conductedById], [t.plantId])
    return _tbt(t, users, pl)


# ── Show cause notices ───────────────────────────────────────────────────────
class NoticeIn(BaseModel):
    plantId: str
    recipientType: str
    recipientName: str = Field(min_length=2, max_length=200)
    recipientUserId: str | None = None
    contractorCompanyId: str | None = None
    violationCategory: str
    violationDetails: str = Field(min_length=10, max_length=4000)
    violationAt: datetime | None = None
    sourceType: str | None = None
    sourceId: str | None = None
    replyWithinDays: int = Field(3, ge=1, le=30)


class ReplyIn(BaseModel):
    replyText: str = Field(min_length=3, max_length=4000)
    receivedAt: datetime | None = None


class CloseNoticeIn(BaseModel):
    decision: str
    decisionNotes: str | None = None
    penaltyAmount: float | None = Field(None, ge=0)


def _notice(n: ShowCauseNotice, users: dict[str, str], plants: dict[str, str]) -> dict[str, Any]:
    return {
        "id": n.id, "noticeNo": n.noticeNo, "plantId": n.plantId, "plantName": plants.get(n.plantId),
        "recipientType": n.recipientType, "recipientName": n.recipientName, "recipientUserId": n.recipientUserId,
        "contractorCompanyId": n.contractorCompanyId, "violationCategory": n.violationCategory,
        "violationDetails": n.violationDetails, "violationAt": _iso(n.violationAt),
        "sourceType": n.sourceType, "sourceId": n.sourceId,
        "issuedAt": _iso(n.issuedAt), "issuedById": n.issuedById, "issuedByName": users.get(n.issuedById),
        "replyDueDate": _iso(n.replyDueDate), "replyReceivedAt": _iso(n.replyReceivedAt), "replyText": n.replyText,
        "decision": n.decision, "decisionNotes": n.decisionNotes, "penaltyAmount": n.penaltyAmount,
        "status": n.status, "closedAt": _iso(n.closedAt), "closedByName": users.get(n.closedById or ""),
        "replyOverdue": svc.notice_reply_overdue(n.status, n.replyDueDate),
    }


@router.get("/show-cause")
async def list_notices(
    plantId: str | None = Query(None), status_: str | None = Query(None, alias="status"), q: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    stmt = select(ShowCauseNotice).where(ShowCauseNotice.isDeleted.is_(False)).where(_plant_where(ShowCauseNotice.plantId, plants))
    if status_ == "OPEN":
        stmt = stmt.where(ShowCauseNotice.status != "CLOSED")
    elif status_:
        stmt = stmt.where(ShowCauseNotice.status == status_)
    if q:
        stmt = stmt.where(or_(ShowCauseNotice.recipientName.ilike(f"%{q}%"), ShowCauseNotice.noticeNo.ilike(f"%{q}%")))
    rows = (await db.execute(stmt.order_by(ShowCauseNotice.issuedAt.desc()).limit(500))).scalars().all()
    users, pl = await _names(db, [r.issuedById for r in rows] + [r.closedById for r in rows], [r.plantId for r in rows])
    return {"items": [_notice(r, users, pl) for r in rows], "total": len(rows)}


@router.post("/show-cause", status_code=201)
async def issue_notice(body: NoticeIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, CREATE, body.plantId)
    _check_choice(body.recipientType, svc.RECIPIENT_TYPES, "recipientType")
    _check_choice(body.violationCategory, svc.VIOLATION_CATEGORIES, "violationCategory")
    _check_choice(body.sourceType, svc.SOURCE_TYPES, "sourceType")
    now = svc.utcnow()
    no = await next_number(db, column=ShowCauseNotice.noticeNo, stem=f"SCN-{now.year}-")
    n = ShowCauseNotice(
        noticeNo=no, plantId=body.plantId, recipientType=body.recipientType, recipientName=body.recipientName.strip(),
        recipientUserId=body.recipientUserId, contractorCompanyId=body.contractorCompanyId,
        violationCategory=body.violationCategory, violationDetails=body.violationDetails, violationAt=body.violationAt,
        sourceType=body.sourceType, sourceId=body.sourceId, issuedAt=now, issuedById=user.id,
        replyDueDate=now + timedelta(days=body.replyWithinDays), status="ISSUED",
    )
    db.add(n)
    await db.commit()
    await db.refresh(n)
    users, pl = await _names(db, [n.issuedById], [n.plantId])
    return _notice(n, users, pl)


@router.post("/show-cause/{nid}/reply")
async def record_reply(nid: str, body: ReplyIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n = await _get_live(db, ShowCauseNotice, nid)
    await _require(db, user, UPDATE, n.plantId)
    if n.status != "ISSUED":
        raise HTTPException(409, f"A reply can only be recorded on an ISSUED notice (this one is {n.status})")
    n.replyText = body.replyText
    n.replyReceivedAt = body.receivedAt or svc.utcnow()
    n.status = "REPLY_RECEIVED"
    await db.commit()
    await db.refresh(n)
    users, pl = await _names(db, [n.issuedById, n.closedById], [n.plantId])
    return _notice(n, users, pl)


@router.post("/show-cause/{nid}/close")
async def close_notice(nid: str, body: CloseNoticeIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n = await _get_live(db, ShowCauseNotice, nid)
    await _require(db, user, CLOSE, n.plantId)
    blockers = svc.notice_close_blockers(status=n.status, decision=body.decision, penalty_amount=body.penaltyAmount)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    n.decision = body.decision
    n.decisionNotes = body.decisionNotes
    n.penaltyAmount = body.penaltyAmount if body.decision == "PENALTY" else None
    n.status = "CLOSED"
    n.closedAt = svc.utcnow()
    n.closedById = user.id
    await db.commit()
    await db.refresh(n)
    users, pl = await _names(db, [n.issuedById, n.closedById], [n.plantId])
    return _notice(n, users, pl)


# ── Meetings + MOM tracker ───────────────────────────────────────────────────
class MeetingIn(BaseModel):
    plantId: str
    meetingType: str
    title: str = Field(min_length=3, max_length=200)
    scheduledAt: datetime
    venue: str | None = None
    chairpersonId: str | None = None
    secretaryId: str | None = None
    agenda: str | None = None


class HeldIn(BaseModel):
    heldAt: datetime | None = None
    attendeeUserIds: list[str] = []
    workerRepCount: int | None = Field(None, ge=0)
    managementRepCount: int | None = Field(None, ge=0)
    minutes: str | None = None


class MinutesIn(BaseModel):
    minutes: str


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class ActionIn(BaseModel):
    description: str = Field(min_length=3, max_length=2000)
    ownerUserId: str
    dueDate: datetime


class ActionMoveIn(BaseModel):
    status: str
    closureNote: str | None = None


def _action(a: MeetingActionItem, users: dict[str, str], meeting: SafetyMeeting | None = None) -> dict[str, Any]:
    return {
        "id": a.id, "meetingId": a.meetingId, "plantId": a.plantId, "itemNo": a.itemNo,
        "description": a.description, "ownerUserId": a.ownerUserId, "ownerName": users.get(a.ownerUserId),
        "dueDate": _iso(a.dueDate), "status": a.status, "closureNote": a.closureNote,
        "closedAt": _iso(a.closedAt), "overdue": svc.action_overdue(a.status, a.dueDate),
        **({"meetingNo": meeting.meetingNo, "meetingTitle": meeting.title, "meetingType": meeting.meetingType} if meeting else {}),
    }


def _meeting(m: SafetyMeeting, users: dict[str, str], plants: dict[str, str], counts: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": m.id, "meetingNo": m.meetingNo, "plantId": m.plantId, "plantName": plants.get(m.plantId),
        "meetingType": m.meetingType, "title": m.title, "scheduledAt": _iso(m.scheduledAt), "heldAt": _iso(m.heldAt),
        "venue": m.venue, "chairpersonId": m.chairpersonId, "chairpersonName": users.get(m.chairpersonId or ""),
        "secretaryId": m.secretaryId, "secretaryName": users.get(m.secretaryId or ""),
        "attendeeUserIds": m.attendeeUserIds or [],
        "attendees": [{"id": u, "name": users.get(u, u)} for u in (m.attendeeUserIds or [])],
        "workerRepCount": m.workerRepCount, "managementRepCount": m.managementRepCount,
        "agenda": m.agenda, "minutes": m.minutes, "status": m.status,
        "minutesPublishedAt": _iso(m.minutesPublishedAt), "cancelReason": m.cancelReason,
        "actionsOpen": (counts or {}).get("open", 0), "actionsTotal": (counts or {}).get("total", 0),
    }


async def _action_counts(db: AsyncSession, meeting_ids: list[str]) -> dict[str, dict[str, int]]:
    if not meeting_ids:
        return {}
    rows = (
        await db.execute(
            select(MeetingActionItem.meetingId, MeetingActionItem.status, func.count())
            .where(MeetingActionItem.isDeleted.is_(False)).where(MeetingActionItem.meetingId.in_(meeting_ids))
            .group_by(MeetingActionItem.meetingId, MeetingActionItem.status)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for mid, st, n in rows:
        c = out.setdefault(mid, {"open": 0, "total": 0})
        c["total"] += int(n)
        if st in svc.ACTION_OPEN:
            c["open"] += int(n)
    return out


@router.get("/meetings")
async def list_meetings(
    plantId: str | None = Query(None), meetingType: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    stmt = select(SafetyMeeting).where(SafetyMeeting.isDeleted.is_(False)).where(_plant_where(SafetyMeeting.plantId, plants))
    if meetingType:
        stmt = stmt.where(SafetyMeeting.meetingType == meetingType)
    if status_:
        stmt = stmt.where(SafetyMeeting.status == status_)
    rows = (await db.execute(stmt.order_by(SafetyMeeting.scheduledAt.desc()).limit(500))).scalars().all()
    counts = await _action_counts(db, [r.id for r in rows])
    users, pl = await _names(db, [r.chairpersonId for r in rows], [r.plantId for r in rows])
    return {"items": [_meeting(r, users, pl, counts.get(r.id)) for r in rows], "total": len(rows)}


@router.post("/meetings", status_code=201)
async def create_meeting(body: MeetingIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, CREATE, body.plantId)
    _check_choice(body.meetingType, svc.MEETING_TYPES, "meetingType")
    no = await next_number(db, column=SafetyMeeting.meetingNo, stem=f"MTG-{body.scheduledAt.year}-")
    m = SafetyMeeting(
        meetingNo=no, plantId=body.plantId, meetingType=body.meetingType, title=body.title.strip(),
        scheduledAt=body.scheduledAt, venue=body.venue, chairpersonId=body.chairpersonId,
        secretaryId=body.secretaryId, agenda=body.agenda, attendeeUserIds=[], status="SCHEDULED", createdBy=user.id,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    users, pl = await _names(db, [m.chairpersonId, m.secretaryId], [m.plantId])
    return _meeting(m, users, pl)


async def _meeting_detail(db: AsyncSession, m: SafetyMeeting) -> dict[str, Any]:
    actions = (
        await db.execute(
            select(MeetingActionItem).where(MeetingActionItem.meetingId == m.id)
            .where(MeetingActionItem.isDeleted.is_(False)).order_by(MeetingActionItem.itemNo)
        )
    ).scalars().all()
    ids = [m.chairpersonId, m.secretaryId, *(m.attendeeUserIds or []), *[a.ownerUserId for a in actions]]
    users, pl = await _names(db, ids, [m.plantId])
    counts = {"open": sum(1 for a in actions if a.status in svc.ACTION_OPEN), "total": len(actions)}
    return {**_meeting(m, users, pl, counts), "actions": [_action(a, users) for a in actions]}


@router.get("/meetings/{mid}")
async def get_meeting(mid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = await _get_live(db, SafetyMeeting, mid)
    await _require(db, user, READ, m.plantId)
    return await _meeting_detail(db, m)


@router.post("/meetings/{mid}/held")
async def mark_held(mid: str, body: HeldIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = await _get_live(db, SafetyMeeting, mid)
    await _require(db, user, UPDATE, m.plantId)
    if not svc.can_move_meeting(m.status, "HELD"):
        raise HTTPException(409, f"Cannot mark a {m.status} meeting as held")
    m.status = "HELD"
    m.heldAt = body.heldAt or svc.utcnow()
    m.attendeeUserIds = list(dict.fromkeys(body.attendeeUserIds))
    m.workerRepCount = body.workerRepCount
    m.managementRepCount = body.managementRepCount
    if body.minutes is not None:
        m.minutes = body.minutes
    await db.commit()
    await db.refresh(m)
    return await _meeting_detail(db, m)


@router.put("/meetings/{mid}/minutes")
async def save_minutes(mid: str, body: MinutesIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = await _get_live(db, SafetyMeeting, mid)
    await _require(db, user, UPDATE, m.plantId)
    if m.status != "HELD":
        raise HTTPException(409, "Minutes can be edited only while the meeting is HELD (before publishing)")
    m.minutes = body.minutes
    await db.commit()
    await db.refresh(m)
    return await _meeting_detail(db, m)


@router.post("/meetings/{mid}/publish")
async def publish_minutes(mid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = await _get_live(db, SafetyMeeting, mid)
    await _require(db, user, CLOSE, m.plantId)
    n_actions = await db.scalar(
        select(func.count()).select_from(MeetingActionItem).where(MeetingActionItem.meetingId == m.id)
        .where(MeetingActionItem.isDeleted.is_(False))
    )
    blockers = svc.minutes_publish_blockers(status=m.status, minutes=m.minutes, action_count=int(n_actions or 0))
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    m.status = "MINUTES_PUBLISHED"
    m.minutesPublishedAt = svc.utcnow()
    await db.commit()
    await db.refresh(m)
    return await _meeting_detail(db, m)


@router.post("/meetings/{mid}/cancel")
async def cancel_meeting(mid: str, body: CancelIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = await _get_live(db, SafetyMeeting, mid)
    await _require(db, user, UPDATE, m.plantId)
    if not svc.can_move_meeting(m.status, "CANCELLED"):
        raise HTTPException(409, f"Cannot cancel a {m.status} meeting")
    m.status = "CANCELLED"
    m.cancelReason = body.reason
    await db.commit()
    await db.refresh(m)
    return await _meeting_detail(db, m)


@router.post("/meetings/{mid}/actions", status_code=201)
async def add_action(mid: str, body: ActionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = await _get_live(db, SafetyMeeting, mid)
    await _require(db, user, CREATE, m.plantId)
    if m.status not in ("HELD", "MINUTES_PUBLISHED"):
        raise HTTPException(409, "Action points are recorded once the meeting has been held")
    highest = await db.scalar(select(func.max(MeetingActionItem.itemNo)).where(MeetingActionItem.meetingId == m.id))
    a = MeetingActionItem(
        meetingId=m.id, plantId=m.plantId, itemNo=int(highest or 0) + 1, description=body.description.strip(),
        ownerUserId=body.ownerUserId, dueDate=body.dueDate, status="OPEN", createdBy=user.id,
    )
    db.add(a)
    await db.commit()
    return await _meeting_detail(db, m)


@router.get("/actions")
async def mom_tracker(
    plantId: str | None = Query(None), status_: str | None = Query(None, alias="status"), mine: bool = Query(False),
    overdueOnly: bool = Query(False), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The MOM tracker: every action point across meetings."""
    plants = await _scoped_plants(db, user, plantId)
    stmt = (
        select(MeetingActionItem, SafetyMeeting).join(SafetyMeeting, SafetyMeeting.id == MeetingActionItem.meetingId)
        .where(MeetingActionItem.isDeleted.is_(False)).where(_plant_where(MeetingActionItem.plantId, plants))
    )
    if status_ == "OPEN":
        stmt = stmt.where(MeetingActionItem.status.in_(svc.ACTION_OPEN))
    elif status_:
        stmt = stmt.where(MeetingActionItem.status == status_)
    if mine:
        stmt = stmt.where(MeetingActionItem.ownerUserId == user.id)
    if overdueOnly:
        stmt = stmt.where(MeetingActionItem.status.in_(svc.ACTION_OPEN)).where(MeetingActionItem.dueDate < svc.utcnow())
    rows = (await db.execute(stmt.order_by(MeetingActionItem.dueDate.asc()).limit(1000))).all()
    users, _ = await _names(db, [a.ownerUserId for a, _m in rows], [])
    return {"items": [_action(a, users, m) for a, m in rows], "total": len(rows)}


@router.post("/actions/{aid}/status")
async def move_action(aid: str, body: ActionMoveIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    a = await _get_live(db, MeetingActionItem, aid)
    if a.ownerUserId != user.id:
        await _require(db, user, UPDATE, a.plantId)
    _check_choice(body.status, svc.ACTION_STATUSES, "status")
    if not svc.can_move_action(a.status, body.status):
        raise HTTPException(409, f"Cannot move an action from {a.status} to {body.status}")
    if body.status in ("CLOSED", "DROPPED") and not (body.closureNote and body.closureNote.strip()):
        raise HTTPException(422, "A closure note is required to close or drop an action")
    a.status = body.status
    if body.status in ("CLOSED", "DROPPED"):
        a.closureNote = body.closureNote
        a.closedAt = svc.utcnow()
        a.closedById = user.id
    else:
        a.closedAt = None
        a.closedById = None
    await db.commit()
    await db.refresh(a)
    m = await db.get(SafetyMeeting, a.meetingId)
    users, _ = await _names(db, [a.ownerUserId], [])
    return _action(a, users, m)
