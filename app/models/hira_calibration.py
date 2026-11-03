"""HIRA calibration v2 tables: versioned settings, the regulation map and the
v1 / v2 shadow log. Created by safeops_360/prisma/apply-hira-calibration-ddl.ts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


class HiraCalibrationConfig(Base, IdMixin):
    """Calibration settings. plantId NULL = tenant default; a plant row
    overrides it. Every change is a new row (version + 1); the newest active
    row wins. `mode` is the feature flag hira.calibration.v2: OFF | SHADOW | ON."""

    __tablename__ = "HiraCalibrationConfig"

    plantId: Mapped[str | None] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="OFF")
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    createdById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class HiraRegulationMap(Base, IdMixin):
    """Hazard -> suggested regulation reference (R10). Matched by hazard code,
    category or calibration family; plantId NULL applies to every plant.
    Editable without a deploy; suggestions are always marked 'verify'."""

    __tablename__ = "HiraRegulationMap"

    plantId: Mapped[str | None] = mapped_column(String, index=True)
    hazardCode: Mapped[str | None] = mapped_column(String)
    hazardCategory: Mapped[str | None] = mapped_column(String)
    family: Mapped[str | None] = mapped_column(String)
    regulation: Mapped[str] = mapped_column(String, nullable=False)
    section: Mapped[str | None] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                onupdate=func.now(), nullable=False)


class HiraCopilotShadowRun(Base, IdMixin):
    """One review / assistant / draft run evaluated by v1 and v2 side by side."""

    __tablename__ = "hira_copilot_shadow_runs"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    plant_id: Mapped[str | None] = mapped_column(String, index=True)
    entry_id: Mapped[str | None] = mapped_column(String, index=True)
    surface: Mapped[str] = mapped_column(String, nullable=False)  # REVIEW | ASSISTANT | COPILOT_DRAFT
    mode: Mapped[str] = mapped_column(String, nullable=False)
    engine_version: Mapped[str] = mapped_column(String, nullable=False)
    config_version: Mapped[dict | None] = mapped_column(JSON)
    v1: Mapped[dict | None] = mapped_column(JSON)
    v2: Mapped[dict | None] = mapped_column(JSON)
    summary: Mapped[dict | None] = mapped_column(JSON)
    user_id: Mapped[str | None] = mapped_column(String)


__all__ = ["HiraCalibrationConfig", "HiraCopilotShadowRun", "HiraRegulationMap"]
