"""Fire Safety audit scope — which fire assets a CAMS audit engagement covers.

Routine checklist runs are one CamsEngagement per (asset, template, period) and
carry their asset in `sourceEntityId`. A Fire Safety AUDIT covers many assets,
so "Include in audit" on an asset writes one row here. Table created by
prisma/apply-fire-audit-scope-ddl.ts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin

# The CamsAuditType.typeCode that marks an engagement as a Fire Safety audit.
FIRE_AUDIT_TYPE_CODE = "FIRE_SAFETY_AUDIT"


class CamsEngagementAsset(Base, IdMixin):
    __tablename__ = "CamsEngagementAsset"
    __table_args__ = (
        UniqueConstraint("engagementId", "sourceModule", "entityId", name="uq_CamsEngagementAsset"),
    )

    engagementId: Mapped[str] = mapped_column(
        ForeignKey("CamsEngagement.id", ondelete="CASCADE"), nullable=False
    )
    sourceModule: Mapped[str] = mapped_column(String, nullable=False, default="FIRE")
    entityId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    addedBy: Mapped[str | None] = mapped_column(String)
    addedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
