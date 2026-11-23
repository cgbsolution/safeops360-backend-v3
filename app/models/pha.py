"""Process Hazard Analysis: HAZOP / What-If / HAZID studies (JSL Hisar requirement).

A PHA study is a facilitated, team-based review of a process unit, broken into
nodes. Each node gets a worksheet of deviations (guideword x parameter), and
each deviation can raise recommendations. This is the classic IEC 61882 HAZOP
layout; What-If and HAZID reuse the same worksheet with free-text "deviation"
questions.

  PhaStudy           the study header: unit, method, scope, P&IDs, matrix,
                     facilitator / scribe / approver, status, revalidation.
  PhaTeamMember      the study team (platform user or an external name).
  PhaSession         a worksheet session: date, duration, attendees, nodes.
  PhaNode            a node: design intent, boundaries, drawing, parameters.
  PhaDeviation       a worksheet row: causes, consequences, safeguards, risk
                     before and after the recommendations.
  PhaRecommendation  an action from a deviation. May spawn a universal CAPA
                     (source PHA_RECOMMENDATION); the CAPA id is kept here.

Risk is rated against the platform RiskMatrix tables (app/models/hira.py); the
study pins its matrix so a later matrix edit cannot re-rate an approved study
silently (the rating is stored on each row).

Tables are created by safeops_360/prisma/apply-pha-ddl.ts (hand DDL, additive).
Statuses are TEXT, not native enums (house rule).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, SoftDeleteMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


class PhaStudy(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "PhaStudy"
    studyNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    unit: Mapped[str | None] = mapped_column(String)
    area: Mapped[str | None] = mapped_column(String)
    method: Mapped[str] = mapped_column(String, nullable=False, default="HAZOP")  # HAZOP | WHAT_IF | HAZID
    title: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text)
    designIntentSummary: Mapped[str | None] = mapped_column(Text)
    drawingRefs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # P&ID / PFD numbers
    riskMatrixId: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    facilitatorId: Mapped[str | None] = mapped_column(String)
    scribeId: Mapped[str | None] = mapped_column(String)
    approverId: Mapped[str | None] = mapped_column(String)
    mocId: Mapped[str | None] = mapped_column(String)
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submittedById: Mapped[str | None] = mapped_column(String)
    reviewComments: Mapped[str | None] = mapped_column(Text)
    approvedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approvedById: Mapped[str | None] = mapped_column(String)
    revalidationDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_PhaStudy_plant_status", "plantId", "status"),)


class PhaTeamMember(Base, IdMixin):
    __tablename__ = "PhaTeamMember"
    studyId: Mapped[str] = mapped_column(String, nullable=False)
    userId: Mapped[str | None] = mapped_column(String)
    externalName: Mapped[str | None] = mapped_column(String)
    organisation: Mapped[str | None] = mapped_column(String)
    discipline: Mapped[str | None] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, nullable=False, default="MEMBER")
    createdAt: Mapped[datetime] = _c()
    __table_args__ = (Index("ix_PhaTeamMember_study", "studyId"),)


class PhaSession(Base, IdMixin):
    __tablename__ = "PhaSession"
    studyId: Mapped[str] = mapped_column(String, nullable=False)
    sessionNo: Mapped[int] = mapped_column(Integer, nullable=False)
    sessionDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    durationMinutes: Mapped[int | None] = mapped_column(Integer)
    attendeeUserIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    attendeeNames: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # external attendees
    nodeIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # nodes covered
    notes: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_PhaSession_study", "studyId"),)


class PhaNode(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "PhaNode"
    studyId: Mapped[str] = mapped_column(String, nullable=False)
    nodeNo: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    designIntent: Mapped[str | None] = mapped_column(Text)
    boundaries: Mapped[str | None] = mapped_column(Text)
    drawingRef: Mapped[str | None] = mapped_column(String)
    # [{"name": "Fuel gas pressure", "value": "0.3", "unit": "barg"}, ...]
    operatingParameters: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_PhaNode_study", "studyId"),)


class PhaDeviation(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "PhaDeviation"
    studyId: Mapped[str] = mapped_column(String, nullable=False)  # denormalised for study-wide reads
    nodeId: Mapped[str] = mapped_column(String, nullable=False)
    rowNo: Mapped[int] = mapped_column(Integer, nullable=False)  # display order within the node
    guideword: Mapped[str | None] = mapped_column(String)
    parameter: Mapped[str | None] = mapped_column(String)
    deviation: Mapped[str] = mapped_column(Text, nullable=False)
    causes: Mapped[str | None] = mapped_column(Text)
    consequences: Mapped[str | None] = mapped_column(Text)
    safeguards: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[int | None] = mapped_column(Integer)
    likelihood: Mapped[int | None] = mapped_column(Integer)
    riskScore: Mapped[int | None] = mapped_column(Integer)
    riskLevel: Mapped[str | None] = mapped_column(String)
    riskColor: Mapped[str | None] = mapped_column(String)
    residualSeverity: Mapped[int | None] = mapped_column(Integer)
    residualLikelihood: Mapped[int | None] = mapped_column(Integer)
    residualScore: Mapped[int | None] = mapped_column(Integer)
    residualLevel: Mapped[str | None] = mapped_column(String)
    residualColor: Mapped[str | None] = mapped_column(String)
    remarks: Mapped[str | None] = mapped_column(Text)
    aiSuggested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_PhaDeviation_node_row", "nodeId", "rowNo"),
        Index("ix_PhaDeviation_study", "studyId"),
    )


class PhaRecommendation(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "PhaRecommendation"
    studyId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)  # denormalised for plant scoping
    nodeId: Mapped[str] = mapped_column(String, nullable=False)
    deviationId: Mapped[str] = mapped_column(String, nullable=False)
    recNo: Mapped[int] = mapped_column(Integer, nullable=False)  # per study: R-001, R-002 ...
    text: Mapped[str] = mapped_column(Text, nullable=False)
    ownerId: Mapped[str | None] = mapped_column(String)
    dueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(String, nullable=False, default="MEDIUM")  # HIGH | MEDIUM | LOW
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")  # OPEN | IN_PROGRESS | CLOSED | REJECTED
    justification: Mapped[str | None] = mapped_column(Text)  # why rejected
    closureNote: Mapped[str | None] = mapped_column(Text)
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedById: Mapped[str | None] = mapped_column(String)
    capaId: Mapped[str | None] = mapped_column(String)
    capaNumber: Mapped[str | None] = mapped_column(String)
    capaState: Mapped[str | None] = mapped_column(String)  # last state seen on read
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_PhaRecommendation_study", "studyId"),
        Index("ix_PhaRecommendation_plant_status", "plantId", "status"),
        Index("ix_PhaRecommendation_deviation", "deviationId"),
    )


__all__ = ["PhaStudy", "PhaTeamMember", "PhaSession", "PhaNode", "PhaDeviation", "PhaRecommendation"]
