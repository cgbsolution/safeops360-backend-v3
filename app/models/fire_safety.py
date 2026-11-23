"""Fire Safety & Emergency Response (P1-4).

Equipment lifecycle, assembly points, emergency plans, drills, and the
incident→crisis link. Emergency plans optionally link to a BCM
ContinuityPlan; a CRITICAL fire incident escalates to an ERM-P3 CrisisEvent.

QR-code inspections (JSL Hisar: "digital inspection mechanism for fire
extinguishers, hydrant points, detection system, etc."):

  FireChecklistTemplate  one checklist per equipment type, stored as data and
                         versioned: an edit writes version n+1 and retires n,
                         so an inspection always points at the exact wording
                         the inspector answered.
  FireInspection         one completed inspection of one asset (scan, answer,
                         submit), with GPS, result counts and the CAPA it
                         raised when a critical item failed.
  FireInspectionItem     the per-item answers (PASS / FAIL / NA, remarks,
                         photo storage path), snapshotting the item text.
  FireEquipmentEvent     the asset's status log: out of service, defective,
                         returned to service, edited.

Older inspections ran as template-less CAMS engagements (sourceModule='FIRE');
those still show as legacy history on the asset. The four inspection tables are
created by safeops_360/prisma/apply-fire-inspection-ddl.ts. Statuses are TEXT.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin, SoftDeleteMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


class FireEquipment(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireEquipment"
    equipmentCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    make: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    serialNo: Mapped[str | None] = mapped_column(String)
    location: Mapped[str] = mapped_column(String, nullable=False)
    buildingId: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(String, nullable=False)  # == siteId
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    floorLevel: Mapped[int | None] = mapped_column(Integer)
    installationDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastInspectionDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextInspectionDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inspectionFrequencyDays: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")  # computed
    capacitySpec: Mapped[str | None] = mapped_column(String)
    maintenanceContractor: Mapped[str | None] = mapped_column(String)
    qrCode: Mapped[str | None] = mapped_column(String)
    outOfServiceReason: Mapped[str | None] = mapped_column(Text)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        Index("ix_FireEquipment_plant_status", "plantId", "status"),
        Index("ix_FireEquipment_type", "type"),
        Index("ix_FireEquipment_due", "nextInspectionDueDate"),
    )


class AssemblyPoint(Base, IdMixin):
    __tablename__ = "AssemblyPoint"
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    buildingIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    capacity: Mapped[int | None] = mapped_column(Integer)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    wardenUserId: Mapped[str | None] = mapped_column(String)
    alternateWardenUserId: Mapped[str | None] = mapped_column(String)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_AssemblyPoint_plant", "plantId"),)


class FireEmergencyPlan(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireEmergencyPlan"
    planCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    continuityPlanId: Mapped[str | None] = mapped_column(String)  # BCM ContinuityPlan link
    fireTypes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    commandStructure: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    callTree: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    assemblyPointIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    criticalEquipmentShutdownSequence: Mapped[str | None] = mapped_column(Text)
    hazmatLocations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    externalContacts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    lastReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nextReviewDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_FireEmergencyPlan_plant", "plantId"),)


class FireDrill(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireDrill"
    drillCode: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    drillType: Mapped[str] = mapped_column(String, nullable=False)
    planId: Mapped[str | None] = mapped_column(String)
    scheduledDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conductedDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False, default="PLANNED")
    outcome: Mapped[str | None] = mapped_column(String)
    facilitatorId: Mapped[str | None] = mapped_column(String)
    participantCount: Mapped[int | None] = mapped_column(Integer)
    evacuationTimeMinutes: Mapped[float | None] = mapped_column(Float)
    evacuationTargetMinutes: Mapped[float | None] = mapped_column(Float)
    assemblyPointVerified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unaccountedPersons: Mapped[int | None] = mapped_column(Integer)
    reportRichText: Mapped[str | None] = mapped_column(Text)
    isAnnualMandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (Index("ix_FireDrill_plant_status", "plantId", "status"),)


class FireDrillFinding(Base, IdMixin):
    __tablename__ = "FireDrillFinding"
    drillId: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)  # OBSERVATION|MINOR_GAP|MAJOR_GAP
    description: Mapped[str] = mapped_column(Text, nullable=False)
    capaId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    __table_args__ = (Index("ix_FireDrillFinding_drill", "drillId"),)


class FireIncidentLink(Base, IdMixin):
    __tablename__ = "FireIncidentLink"
    incidentId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str | None] = mapped_column(String)
    affectedEquipmentIds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    crisisEventId: Mapped[str | None] = mapped_column(String)
    evacuationOrdered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fireServiceCalled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    estimatedPropertyDamageInr: Mapped[float | None] = mapped_column(Float)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_FireIncidentLink_incident", "incidentId"),)


class FireChecklistTemplate(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "FireChecklistTemplate"
    code: Mapped[str] = mapped_column(String, nullable=False)  # stable family code across versions
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    equipmentType: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    standardRef: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    frequencyDays: Mapped[int | None] = mapped_column(Integer)
    # [{key, text, critical, guidance}] in display order
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    isActive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # the current version
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_FireChecklistTemplate_code_version"),
        Index("ix_FireChecklistTemplate_type_active", "equipmentType", "isActive"),
    )


class FireInspection(Base, IdMixin):
    __tablename__ = "FireInspection"
    inspectionNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    equipmentId: Mapped[str] = mapped_column(String, nullable=False)
    equipmentCode: Mapped[str] = mapped_column(String, nullable=False)
    equipmentType: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    templateId: Mapped[str] = mapped_column(String, nullable=False)
    templateCode: Mapped[str] = mapped_column(String, nullable=False)
    templateVersion: Mapped[int] = mapped_column(Integer, nullable=False)
    inspectorId: Mapped[str] = mapped_column(String, nullable=False)
    startedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[str] = mapped_column(String, nullable=False)  # PASS | FAIL
    passCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    naCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    criticalFailCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remarks: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    gpsAccuracyM: Mapped[float | None] = mapped_column(Float)
    scanned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # opened from the asset's QR
    equipmentStatusBefore: Mapped[str | None] = mapped_column(String)
    equipmentStatusAfter: Mapped[str | None] = mapped_column(String)
    nextDueDate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capaId: Mapped[str | None] = mapped_column(String)
    capaNumber: Mapped[str | None] = mapped_column(String)
    followUpNote: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = _c()
    __table_args__ = (
        Index("ix_FireInspection_equipment_completed", "equipmentId", "completedAt"),
        Index("ix_FireInspection_plant_completed", "plantId", "completedAt"),
    )


class FireInspectionItem(Base, IdMixin):
    __tablename__ = "FireInspectionItem"
    inspectionId: Mapped[str] = mapped_column(String, nullable=False)
    sortOrder: Mapped[int] = mapped_column(Integer, nullable=False)
    itemKey: Mapped[str] = mapped_column(String, nullable=False)
    itemText: Mapped[str] = mapped_column(Text, nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    response: Mapped[str] = mapped_column(String, nullable=False)  # PASS | FAIL | NA
    remarks: Mapped[str | None] = mapped_column(Text)
    photoPath: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    __table_args__ = (
        UniqueConstraint("inspectionId", "itemKey", name="uq_FireInspectionItem_inspection_key"),
        Index("ix_FireInspectionItem_inspection", "inspectionId"),
    )


class FireEquipmentEvent(Base, IdMixin):
    __tablename__ = "FireEquipmentEvent"
    equipmentId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    # CREATED | UPDATED | OUT_OF_SERVICE | MARKED_DEFECTIVE | RETURNED_TO_SERVICE | DEFECT_CLEARED
    eventType: Mapped[str] = mapped_column(String, nullable=False)
    fromStatus: Mapped[str | None] = mapped_column(String)
    toStatus: Mapped[str | None] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text)
    inspectionId: Mapped[str | None] = mapped_column(String)
    actorId: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    __table_args__ = (Index("ix_FireEquipmentEvent_equipment", "equipmentId", "createdAt"),)


__all__ = [
    "FireEquipment", "AssemblyPoint", "FireEmergencyPlan",
    "FireDrill", "FireDrillFinding", "FireIncidentLink",
    "FireChecklistTemplate", "FireInspection", "FireInspectionItem", "FireEquipmentEvent",
]
