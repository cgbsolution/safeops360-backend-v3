"""Per-plant permit-type curation for the PTW wizard.

A plant with NO row keeps the full platform set and the Hot Work default —
i.e. every existing site behaves exactly as before. A row narrows the types a
permit can be raised under at that plant (and the hazard annexures that belong
to the removed types) and picks the card the wizard opens on.

This is config, not vocabulary: it lives beside the display-label layer rather
than in it. Table is created by prisma/apply-ptw-type-config-ddl.ts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base


class PlantPermitTypeConfig(Base):
    __tablename__ = "PlantPermitTypeConfig"

    plantId: Mapped[str] = mapped_column(String, primary_key=True)
    # PermitType values, e.g. ["WORK_AT_HEIGHT", "ELECTRICAL_LOTO", ...]
    enabledTypes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    defaultType: Mapped[str] = mapped_column(String, nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
