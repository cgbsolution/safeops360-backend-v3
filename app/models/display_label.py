"""Tenant display-label overrides — the UI-rendering-layer vocabulary.

A *profile* (e.g. RETAIL) is a set of `key → label` rows; a plant opts into a
profile through PlantDisplayProfile. Rendering code asks for a key WITH its
hardcoded default, so a plant with no profile (every existing site) renders
exactly the string it always did. Nothing here renames a column, field or API
contract — these rows only change what a human reads.

Tables are created by prisma/apply-display-labels-ddl.ts (additive, idempotent).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class DisplayLabelProfile(Base, IdMixin):
    __tablename__ = "DisplayLabelProfile"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DisplayLabel(Base, IdMixin):
    __tablename__ = "DisplayLabel"
    __table_args__ = (UniqueConstraint("profileCode", "key", name="DisplayLabel_profile_key_key"),)

    profileCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Dotted key: "term.plant", "nav./ptw", "nav.section.facilities", …
    key: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlantDisplayProfile(Base):
    __tablename__ = "PlantDisplayProfile"

    plantId: Mapped[str] = mapped_column(String, primary_key=True)
    profileCode: Mapped[str] = mapped_column(String, nullable=False, index=True)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
