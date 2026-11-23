"""Pre-Startup Safety Review (PSSR), standalone register (JSL Hisar "MOC & PSSR").

Before this module a PSSR existed only as a JSON checklist on a change request
(`ChangeRequest.pssrChecklist`). That cannot serve a commissioning or a
turnaround restart, which has no MOC, and it has no punch list, no sign-off
matrix and no register. These tables are the standalone PSSR:

  PssrTemplate / PssrTemplateItem  - the checklist master. A default steel-plant
                                     template is seeded by the DDL script.
  PssrReview                        - one review of one start-up (new facility,
                                     modification, turnaround, restart). May link
                                     a MOC (`mocId`) and a PHA study (`phaStudyId`,
                                     id only: no FK, the PHA tables are built
                                     separately).
  PssrItem                          - the checklist, COPIED from the template at
                                     creation so a later template edit never
                                     rewrites a review already in progress. A NO
                                     answer is a punch item (category A = close
                                     before start-up, B = may close after).
  PssrSignoff                       - the sign-off matrix: one current decision
                                     per discipline.

Tables are created by safeops_360/prisma/apply-pssr-psi-ddl.ts (hand DDL,
additive). Statuses are TEXT (house rule). The go / no-go rules live in
app/services/pssr.py as pure functions.
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


class PssrTemplate(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "PssrTemplate"
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    plantId: Mapped[str | None] = mapped_column(String)  # None = available to every plant
    isDefault: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()


class PssrTemplateItem(Base, IdMixin):
    __tablename__ = "PssrTemplateItem"
    templateId: Mapped[str] = mapped_column(String, nullable=False)
    section: Mapped[str] = mapped_column(String, nullable=False)
    sectionOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    itemOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    guidance: Mapped[str | None] = mapped_column(Text)
    discipline: Mapped[str] = mapped_column(String, nullable=False)
    defaultPunchCategory: Mapped[str] = mapped_column(String, nullable=False, default="A")  # A | B
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_PssrTemplateItem_template", "templateId", "sectionOrder", "itemOrder"),)


class PssrReview(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "PssrReview"
    pssrNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    area: Mapped[str | None] = mapped_column(String)
    unit: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String, nullable=False)  # NEW_FACILITY | MODIFICATION | TURNAROUND | RESTART_AFTER_SHUTDOWN | OTHER
    mocId: Mapped[str | None] = mapped_column(String)
    mocNumber: Mapped[str | None] = mapped_column(String)  # denormalised for the register
    phaStudyId: Mapped[str | None] = mapped_column(String)  # id only, no FK
    templateId: Mapped[str | None] = mapped_column(String)
    plannedStartupDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leaderId: Mapped[str] = mapped_column(String, nullable=False)
    teamUserIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    requiredDisciplines: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decidedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    noGoReason: Mapped[str | None] = mapped_column(Text)
    reopenCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lastReopenReason: Mapped[str | None] = mapped_column(Text)
    lastReopenedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actualStartupDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_PssrReview_plant_status", "plantId", "status"),
        Index("ix_PssrReview_moc", "mocId"),
    )


class PssrItem(Base, IdMixin):
    __tablename__ = "PssrItem"
    reviewId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)  # denormalised: plant scope for the evidence layer
    templateItemId: Mapped[str | None] = mapped_column(String)
    section: Mapped[str] = mapped_column(String, nullable=False)
    sectionOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    itemOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    guidance: Mapped[str | None] = mapped_column(Text)
    discipline: Mapped[str] = mapped_column(String, nullable=False)
    defaultPunchCategory: Mapped[str] = mapped_column(String, nullable=False, default="A")
    response: Mapped[str | None] = mapped_column(String)  # YES | NO | NA
    remarks: Mapped[str | None] = mapped_column(Text)
    verifiedById: Mapped[str | None] = mapped_column(String)
    verifiedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # punch (only meaningful when response == NO)
    punchCategory: Mapped[str | None] = mapped_column(String)  # A | B
    punchDescription: Mapped[str | None] = mapped_column(Text)
    punchStatus: Mapped[str | None] = mapped_column(String)  # OPEN | CLOSED
    punchOwnerId: Mapped[str | None] = mapped_column(String)
    punchDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    punchClosedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    punchClosedById: Mapped[str | None] = mapped_column(String)
    punchClosureNote: Mapped[str | None] = mapped_column(Text)
    capaId: Mapped[str | None] = mapped_column(String)
    capaNumber: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_PssrItem_review", "reviewId", "sectionOrder", "itemOrder"),)


class PssrSignoff(Base, IdMixin):
    __tablename__ = "PssrSignoff"
    reviewId: Mapped[str] = mapped_column(String, nullable=False)
    discipline: Mapped[str] = mapped_column(String, nullable=False)
    userId: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)  # APPROVE | REJECT
    comment: Mapped[str | None] = mapped_column(Text)
    signedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # A re-sign or a reopen retires the earlier decision; it stays for the audit trail.
    isCurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _c()
    __table_args__ = (Index("ix_PssrSignoff_review", "reviewId", "isCurrent"),)


__all__ = ["PssrTemplate", "PssrTemplateItem", "PssrReview", "PssrItem", "PssrSignoff"]
