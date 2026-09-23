from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class Manhours(Base, IdMixin):
    __tablename__ = "Manhours"
    __table_args__ = (UniqueConstraint("plantId", "year", "month", name="uq_manhours_period"),)

    plantId: Mapped[str] = mapped_column(ForeignKey("Plant.id"), nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)

    headcount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── The canonical exposure + injury columns ─────────────────────────────
    # These are what the Prisma schema declares and what the whole platform
    # reads: the EHS Scorecard rollup, the Manhours KPI engine, the BRSR safety
    # mapping and the ERM KRI feed. They are NOT NULL with NO database default,
    # which is why this model has to declare them — an insert through the ORM
    # that omitted them failed outright, and an update that omitted them left the
    # canonical figures behind while the legacy pair below moved.
    employeeHours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contractorHours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ltiCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mtcCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rwcCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    facCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fatalityCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lostDays: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Legacy aliases, dual-written ───────────────────────────────────────
    # The same three facts under this model's original names. Nothing reads them
    # any more, but they are NOT NULL and 204 production rows carry them in step
    # with the canonical columns, so the write path keeps them in step rather than
    # leaving a table where two columns for one fact disagree.
    manhoursWorked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contractorManhours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fatalCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Published rates. Kept in sync by the write path for backwards compatibility;
    # NOTHING reads them — every screen derives from the counts and hours above
    # through services.frequency_rates. See that module's docstring.
    ltifr: Mapped[float | None] = mapped_column(Numeric(10, 4))
    trir: Mapped[float | None] = mapped_column(Numeric(10, 4))
    severityRate: Mapped[float | None] = mapped_column(Numeric(10, 4))

    submittedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(String)

    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
