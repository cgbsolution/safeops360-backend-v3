"""Process Safety Information (PSI) document library (JSL Hisar "PSI advisor").

PsiDocument is one REVISION of one controlled PSI document (P&ID, PFD, design
basis, relief-system design, electrical area classification, SDS, ...). A new
revision of the same (plantId, docNo) is a new row: the earlier row is marked
SUPERSEDED and the new one points back at it (`supersedesId`), so the revision
history is the rows themselves.

The file lives in platform storage (Supabase) behind short-lived signed URLs,
like every other upload; the row keeps the storage path and file metadata.

An SDS is a PSI document with docType SDS and `hazardousMaterialId` set: that
is how an SDS file is linked to a chemical in the HazardousMaterial register
without adding a column to the Facilities table.

Table created by safeops_360/prisma/apply-pssr-psi-ddl.ts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, SoftDeleteMixin


class PsiDocument(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "PsiDocument"
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    unitArea: Mapped[str | None] = mapped_column(String)
    docType: Mapped[str] = mapped_column(String, nullable=False)
    docNo: Mapped[str] = mapped_column(String, nullable=False)
    revision: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    hazardousMaterialId: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="CURRENT")  # CURRENT | SUPERSEDED
    supersedesId: Mapped[str | None] = mapped_column(String)
    issuedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewDue: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fileName: Mapped[str | None] = mapped_column(String)
    storagePath: Mapped[str | None] = mapped_column(String)
    fileSize: Mapped[int | None] = mapped_column(Integer)
    mimeType: Mapped[str | None] = mapped_column(String)
    fileUploadedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploadedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        Index("ix_PsiDocument_plant_type_status", "plantId", "docType", "status"),
        Index("ix_PsiDocument_plant_docno", "plantId", "docNo"),
        Index("ix_PsiDocument_material", "hazardousMaterialId"),
    )


__all__ = ["PsiDocument"]
