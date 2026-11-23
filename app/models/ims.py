"""IMS / MIS governance registers (JSL Hisar requirement #8).

Four registers the plant safety team keeps today in Excel, and that the IMS/MIS
dashboard needs as data:

  ToolboxTalk          — a toolbox talk (TBT) delivered to a crew. Distinct from
                         the TBT fields on FLRA: those record "a TBT happened
                         before this job"; this is the plant-wide TBT register
                         (topic, crew size, contractor share) that the monthly
                         MIS counts.
  ShowCauseNotice      — a disciplinary show-cause notice for a safety violation,
                         issued to an employee, a contractor worker or a
                         contractor company, with reply and decision.
  SafetyMeeting        — a safety committee (or apex / departmental / contractor)
                         meeting: scheduled → held → minutes published.
  MeetingActionItem    — the MOM tracker: each action point from the minutes,
                         with owner, due date and closure.

Mock drills are NOT here: they live in FireDrill (app/models/fire_safety.py),
which already carries the completion gate; this build adds the schedule
endpoint there. Safety audits are CAMS engagements — also not duplicated.

Tables are created by safeops_360/prisma/apply-ims-ddl.ts (hand DDL, additive).
Statuses are TEXT, not native enums (house rule).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, SoftDeleteMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


class ToolboxTalk(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "ToolboxTalk"
    talkNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    areaId: Mapped[str | None] = mapped_column(String)
    department: Mapped[str | None] = mapped_column(String)
    topic: Mapped[str] = mapped_column(String, nullable=False)
    keyPoints: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String)
    conductedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conductedById: Mapped[str] = mapped_column(String, nullable=False)
    durationMinutes: Mapped[int | None] = mapped_column(Integer)
    employeeCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contractorCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contractorCompanyId: Mapped[str | None] = mapped_column(String)
    permitId: Mapped[str | None] = mapped_column(String)
    photoUrl: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_ToolboxTalk_plant_conducted", "plantId", "conductedAt"),)


class ShowCauseNotice(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "ShowCauseNotice"
    noticeNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    recipientType: Mapped[str] = mapped_column(String, nullable=False)  # EMPLOYEE | CONTRACTOR_WORKER | CONTRACTOR_COMPANY
    recipientName: Mapped[str] = mapped_column(String, nullable=False)
    recipientUserId: Mapped[str | None] = mapped_column(String)
    contractorCompanyId: Mapped[str | None] = mapped_column(String)
    violationCategory: Mapped[str] = mapped_column(String, nullable=False)
    violationDetails: Mapped[str] = mapped_column(Text, nullable=False)
    violationAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sourceType: Mapped[str | None] = mapped_column(String)  # OBSERVATION | PERMIT | INCIDENT | NEAR_MISS | INSPECTION
    sourceId: Mapped[str | None] = mapped_column(String)
    issuedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issuedById: Mapped[str] = mapped_column(String, nullable=False)
    replyDueDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    replyReceivedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replyText: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(String)  # NO_ACTION | WARNING | PENALTY | SUSPENSION | TERMINATION
    decisionNotes: Mapped[str | None] = mapped_column(Text)
    penaltyAmount: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ISSUED")  # ISSUED | REPLY_RECEIVED | CLOSED
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_ShowCauseNotice_plant_status", "plantId", "status"),)


class SafetyMeeting(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "SafetyMeeting"
    meetingNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    meetingType: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    scheduledAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heldAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    venue: Mapped[str | None] = mapped_column(String)
    chairpersonId: Mapped[str | None] = mapped_column(String)
    secretaryId: Mapped[str | None] = mapped_column(String)
    attendeeUserIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    workerRepCount: Mapped[int | None] = mapped_column(Integer)
    managementRepCount: Mapped[int | None] = mapped_column(Integer)
    agenda: Mapped[str | None] = mapped_column(Text)
    minutes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, default="SCHEDULED")  # SCHEDULED | HELD | MINUTES_PUBLISHED | CANCELLED
    minutesPublishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelReason: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_SafetyMeeting_plant_scheduled", "plantId", "scheduledAt"),)


class MeetingActionItem(Base, IdMixin):
    __tablename__ = "MeetingActionItem"
    meetingId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)  # denormalised for plant scoping
    itemNo: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    ownerUserId: Mapped[str] = mapped_column(String, nullable=False)
    dueDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")  # OPEN | IN_PROGRESS | CLOSED | DROPPED
    closureNote: Mapped[str | None] = mapped_column(Text)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedById: Mapped[str | None] = mapped_column(String)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_MeetingActionItem_meeting", "meetingId"),
        Index("ix_MeetingActionItem_plant_status", "plantId", "status"),
    )


__all__ = ["ToolboxTalk", "ShowCauseNotice", "SafetyMeeting", "MeetingActionItem"]
