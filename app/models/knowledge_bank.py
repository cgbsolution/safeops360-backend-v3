"""Knowledge bank (JSL Hisar requirement: "All safety standards, checklists,
policies will store in the digital bank & read accessible to everyone inside
company").

  KbCategory           a browsable category tree (Safety Standards, Policies,
                       SOPs, ...). Seeded by the DDL script.
  KbDocument           one controlled document: number, metadata, lifecycle
                       status, the version readers see (currentVersionId).
                       plantId NULL means company-wide; a plant id means the
                       document applies to that plant. It is NOT an access
                       boundary: every KB.READ holder can read every published
                       document, whatever plant it applies to.
  KbDocumentVersion    one uploaded file. Versions carry their own review state
                       so a published document stays readable while its next
                       revision is drafted and reviewed.
  KbAcknowledgement    "I have read this" against one specific version. A new
                       published version therefore needs a fresh acknowledgement.
  KbView               read log (who opened what, when). Drives viewCount and
                       the "open before you acknowledge" rule.

Distinct from One Point Lessons (BeOpl): an OPL is a one-page lesson pushed to
named people; a knowledge-bank document is a controlled reference file anyone
in the company can look up.

Tables are created by safeops_360/prisma/apply-knowledge-bank-ddl.ts (hand
DDL, additive). Statuses are TEXT, not native enums (house rule).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, SoftDeleteMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


class KbCategory(Base, IdMixin):
    __tablename__ = "KbCategory"
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    parentId: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()


class KbDocument(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "KbDocument"
    docNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    categoryId: Mapped[str] = mapped_column(String, nullable=False)
    docType: Mapped[str] = mapped_column(String, nullable=False)
    standardRef: Mapped[str | None] = mapped_column(String)  # IS 3786 / OISD-STD-105 / ISO 45001 / Factories Act / company std
    department: Mapped[str | None] = mapped_column(String)  # Department.name (names are what User.department carries)
    plantId: Mapped[str | None] = mapped_column(String)  # NULL = company-wide
    ownerUserId: Mapped[str] = mapped_column(String, nullable=False)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")  # DRAFT | UNDER_REVIEW | PUBLISHED | OBSOLETE
    currentVersionId: Mapped[str | None] = mapped_column(String)
    effectiveDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewIntervalMonths: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    lastReviewedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastReviewedBy: Mapped[str | None] = mapped_column(String)
    ackRequired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    audienceRoles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # role codes; [] = everyone
    viewCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Denormalised search corpus: docNo, title, summary, tags, standard,
    # department and the current version's extracted text. Maintained by the
    # service; the GIN full-text index is built over this one column.
    searchText: Mapped[str | None] = mapped_column(Text)
    publishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    obsoletedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    obsoletedById: Mapped[str | None] = mapped_column(String)
    obsoleteReason: Mapped[str | None] = mapped_column(Text)
    supersededByDocId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_KbDocument_status", "status"),
        Index("ix_KbDocument_category", "categoryId"),
        Index("ix_KbDocument_plant", "plantId"),
        Index("ix_KbDocument_reviewDue", "reviewDueDate"),
    )


class KbDocumentVersion(Base, IdMixin):
    __tablename__ = "KbDocumentVersion"
    documentId: Mapped[str] = mapped_column(String, nullable=False)
    versionNo: Mapped[int] = mapped_column(Integer, nullable=False)
    # DRAFT | UNDER_REVIEW | PUBLISHED | SUPERSEDED | DISCARDED
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    mimeType: Mapped[str] = mapped_column(String, nullable=False)
    sizeBytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String)
    extractedText: Mapped[str | None] = mapped_column(Text)
    extractionStatus: Mapped[str] = mapped_column(String, nullable=False, default="SKIPPED")  # OK | EMPTY | SKIPPED | FAILED
    changeNote: Mapped[str | None] = mapped_column(Text)
    reviewNote: Mapped[str | None] = mapped_column(Text)
    uploadedBy: Mapped[str] = mapped_column(String, nullable=False)
    uploadedAt: Mapped[datetime] = _c()
    submittedBy: Mapped[str | None] = mapped_column(String)
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publishedBy: Mapped[str | None] = mapped_column(String)
    publishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("documentId", "versionNo", name="uq_KbDocumentVersion_doc_version"),
        Index("ix_KbDocumentVersion_document", "documentId"),
    )


class KbAcknowledgement(Base, IdMixin):
    __tablename__ = "KbAcknowledgement"
    documentId: Mapped[str] = mapped_column(String, nullable=False)
    versionId: Mapped[str] = mapped_column(String, nullable=False)
    userId: Mapped[str] = mapped_column(String, nullable=False)
    acknowledgedAt: Mapped[datetime] = _c()
    __table_args__ = (
        UniqueConstraint("documentId", "versionId", "userId", name="uq_KbAcknowledgement_doc_version_user"),
        Index("ix_KbAcknowledgement_user", "userId"),
        Index("ix_KbAcknowledgement_version", "versionId"),
    )


class KbView(Base, IdMixin):
    __tablename__ = "KbView"
    documentId: Mapped[str] = mapped_column(String, nullable=False)
    versionId: Mapped[str | None] = mapped_column(String)
    userId: Mapped[str] = mapped_column(String, nullable=False)
    viewedAt: Mapped[datetime] = _c()
    __table_args__ = (Index("ix_KbView_doc_user", "documentId", "userId", "viewedAt"),)


__all__ = ["KbCategory", "KbDocument", "KbDocumentVersion", "KbAcknowledgement", "KbView"]
