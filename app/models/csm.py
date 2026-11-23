"""Contractor Safety Management (CSM) for PLANT contractors (JSL Hisar requirement).

Client wording: "Contractor Safety Management end to end process, from
contractor qualification to contractor evaluation". The contractor master is
the existing ``ContractorCompany`` (app/models/epc.py): CSM never copies it, it
writes the prequalification outcome and sanctions back onto it.

  CsmQuestionnaireTemplate  prequalification questionnaire (sections/questions
                            with weights, stored as JSON so a version is one row)
  CsmPrequalification       one application: answers, auto score, evaluator
                            overrides, SUBMITTED -> HSE_REVIEW -> APPROVAL ->
                            QUALIFIED / CONDITIONAL / REJECTED, validity
  CsmPortalToken            tokenised external link a contractor uses to fill
  CsmPortalAccessLog        the questionnaire (supplier_portal pattern)
  CsmDocument               statutory / HSE documents with expiry dates
  CsmWorkOrder              contract / work order at a plant, with the safety
                            requirements checklist and the activation gate
  CsmWorkOrderWorker        work-order roster: existing ContractorWorker rows,
                            plant induction and the plant gate clearance
  CsmEvaluation             periodic / contract-end performance evaluation
  CsmSanction               suspend / blacklist / reinstate with approval

Tables are created by safeops_360/prisma/apply-csm-ddl.ts (hand DDL, additive,
no ALTER on any existing table). Statuses are TEXT, not native enums.
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


def _dt():
    return mapped_column(DateTime(timezone=True))


class CsmQuestionnaireTemplate(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "CsmQuestionnaireTemplate"
    code: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")  # DRAFT | ACTIVE | RETIRED
    # [{code, title, weight, questions: [{code, text, answerType, weight, critical,
    #   required, help, docType, preferredAnswer, bands: [{max, score}]}]}]
    sections: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    passScore: Mapped[float] = mapped_column(Float, nullable=False, default=70)
    conditionalScore: Mapped[float] = mapped_column(Float, nullable=False, default=55)
    validityMonths: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    conditionalValidityMonths: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    activatedAt: Mapped[datetime | None] = _dt()
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (UniqueConstraint("code", "version", name="uq_CsmQuestionnaireTemplate_code_version"),)


class CsmPrequalification(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "CsmPrequalification"
    applicationNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    contractorCompanyId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    templateId: Mapped[str] = mapped_column(String, nullable=False)
    templateSnapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # frozen copy of the template
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    answers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # {questionCode: {value, comment}}
    overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # {questionCode: {score, reason, byId, at}}
    autoScore: Mapped[float | None] = mapped_column(Float)
    finalScore: Mapped[float | None] = mapped_column(Float)
    criticalFailures: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recommendation: Mapped[str | None] = mapped_column(String)  # QUALIFIED | CONDITIONAL | REJECTED
    reviewNotes: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(String)
    decisionNotes: Mapped[str | None] = mapped_column(Text)
    validFrom: Mapped[datetime | None] = _dt()
    validUntil: Mapped[datetime | None] = _dt()
    isCurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    isRenewal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    previousPrequalificationId: Mapped[str | None] = mapped_column(String)
    submittedVia: Mapped[str | None] = mapped_column(String)  # PORTAL | INTERNAL
    submittedAt: Mapped[datetime | None] = _dt()
    submittedByName: Mapped[str | None] = mapped_column(String)
    reviewStartedAt: Mapped[datetime | None] = _dt()
    reviewerId: Mapped[str | None] = mapped_column(String)
    recommendedAt: Mapped[datetime | None] = _dt()
    recommendedById: Mapped[str | None] = mapped_column(String)
    decidedAt: Mapped[datetime | None] = _dt()
    decidedById: Mapped[str | None] = mapped_column(String)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{at, byId, byName, from, to, note}]
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_CsmPrequalification_company", "contractorCompanyId"),
        Index("ix_CsmPrequalification_plant_status", "plantId", "status"),
    )


class CsmPortalToken(Base, IdMixin):
    __tablename__ = "CsmPortalToken"
    prequalificationId: Mapped[str] = mapped_column(String, nullable=False)
    contractorCompanyId: Mapped[str] = mapped_column(String, nullable=False)
    tokenHash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    tokenPrefix: Mapped[str] = mapped_column(String, nullable=False)
    contactEmail: Mapped[str | None] = mapped_column(String)
    contactName: Mapped[str | None] = mapped_column(String)
    expiresAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revokedAt: Mapped[datetime | None] = _dt()
    revokedById: Mapped[str | None] = mapped_column(String)
    revokedReason: Mapped[str | None] = mapped_column(String)
    lastAccessedAt: Mapped[datetime | None] = _dt()
    accessCount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = _c()
    createdById: Mapped[str | None] = mapped_column(String)
    __table_args__ = (Index("ix_CsmPortalToken_prequal", "prequalificationId"),)


class CsmPortalAccessLog(Base, IdMixin):
    __tablename__ = "CsmPortalAccessLog"
    tokenId: Mapped[str | None] = mapped_column(String)
    tokenPrefix: Mapped[str | None] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String, nullable=False)  # OK | NOT_FOUND | REVOKED | EXPIRED | RATE_LIMITED
    action: Mapped[str] = mapped_column(String, nullable=False)
    isWrite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ipAddress: Mapped[str | None] = mapped_column(String)
    userAgent: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()


class CsmDocument(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "CsmDocument"
    contractorCompanyId: Mapped[str] = mapped_column(String, nullable=False)
    prequalificationId: Mapped[str | None] = mapped_column(String)
    questionCode: Mapped[str | None] = mapped_column(String)
    docType: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String)
    documentNo: Mapped[str | None] = mapped_column(String)
    fileName: Mapped[str] = mapped_column(String, nullable=False)
    storagePath: Mapped[str] = mapped_column(String, nullable=False)
    mimeType: Mapped[str | None] = mapped_column(String)
    fileSize: Mapped[int | None] = mapped_column(Integer)
    issuedOn: Mapped[datetime | None] = _dt()
    expiresOn: Mapped[datetime | None] = _dt()
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")  # ACTIVE | SUPERSEDED | REJECTED
    uploadedVia: Mapped[str] = mapped_column(String, nullable=False, default="INTERNAL")  # INTERNAL | PORTAL
    uploadedById: Mapped[str | None] = mapped_column(String)
    uploadedByName: Mapped[str | None] = mapped_column(String)
    verifiedAt: Mapped[datetime | None] = _dt()
    verifiedById: Mapped[str | None] = mapped_column(String)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_CsmDocument_company_type", "contractorCompanyId", "docType"),
        Index("ix_CsmDocument_expires", "expiresOn"),
    )


class CsmWorkOrder(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "CsmWorkOrder"
    workOrderNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    contractorCompanyId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    areaId: Mapped[str | None] = mapped_column(String)
    areaName: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    workType: Mapped[str] = mapped_column(String, nullable=False, default="MAINTENANCE")
    contractRef: Mapped[str | None] = mapped_column(String)
    startDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endDate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    engineerInChargeId: Mapped[str | None] = mapped_column(String)
    estimatedManpower: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    supervisorCount: Mapped[int | None] = mapped_column(Integer)
    highRiskActivities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    safetyChecklist: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    safetyNotes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")  # DRAFT | ACTIVE | SUSPENDED | COMPLETED | CANCELLED
    activatedAt: Mapped[datetime | None] = _dt()
    activatedById: Mapped[str | None] = mapped_column(String)
    suspendedReason: Mapped[str | None] = mapped_column(Text)
    completedAt: Mapped[datetime | None] = _dt()
    closureNotes: Mapped[str | None] = mapped_column(Text)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_CsmWorkOrder_plant_status", "plantId", "status"),
        Index("ix_CsmWorkOrder_company", "contractorCompanyId"),
    )


class CsmWorkOrderWorker(Base, IdMixin):
    __tablename__ = "CsmWorkOrderWorker"
    workOrderId: Mapped[str] = mapped_column(String, nullable=False)
    contractorWorkerId: Mapped[str] = mapped_column(String, nullable=False)
    contractorCompanyId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    tradeAtSite: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PROPOSED")  # PROPOSED | CLEARED | BLOCKED | RELEASED
    inductionAt: Mapped[datetime | None] = _dt()
    inductionValidUntil: Mapped[datetime | None] = _dt()
    inductionById: Mapped[str | None] = mapped_column(String)
    lastCheck: Mapped[dict | None] = mapped_column(JSON)
    lastCheckedAt: Mapped[datetime | None] = _dt()
    gatePassNo: Mapped[str | None] = mapped_column(String)
    gatePassValidUntil: Mapped[datetime | None] = _dt()
    releasedAt: Mapped[datetime | None] = _dt()
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        UniqueConstraint("workOrderId", "contractorWorkerId", name="uq_CsmWorkOrderWorker_wo_worker"),
        Index("ix_CsmWorkOrderWorker_company_status", "contractorCompanyId", "status"),
    )


class CsmEvaluation(Base, IdMixin, SoftDeleteMixin):
    __tablename__ = "CsmEvaluation"
    evaluationNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    contractorCompanyId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)
    workOrderId: Mapped[str | None] = mapped_column(String)
    evaluationType: Mapped[str] = mapped_column(String, nullable=False)  # MONTHLY | QUARTERLY | CONTRACT_END
    periodStart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    periodEnd: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kpis: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # auto-filled KPI rows
    counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # raw source counts
    manual: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # {criterionCode: {score 1-5, comment}}
    autoScore: Mapped[float | None] = mapped_column(Float)
    manualScore: Mapped[float | None] = mapped_column(Float)
    finalScore: Mapped[float | None] = mapped_column(Float)
    ratingBand: Mapped[str | None] = mapped_column(String)
    evaluatorRemarks: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")  # DRAFT | SUBMITTED | SIGNED_OFF
    evaluatorId: Mapped[str | None] = mapped_column(String)
    submittedAt: Mapped[datetime | None] = _dt()
    signedOffById: Mapped[str | None] = mapped_column(String)
    signedOffAt: Mapped[datetime | None] = _dt()
    signOffNotes: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_CsmEvaluation_company_period", "contractorCompanyId", "periodEnd"),
        Index("ix_CsmEvaluation_plant_status", "plantId", "status"),
    )


class CsmSanction(Base, IdMixin):
    __tablename__ = "CsmSanction"
    sanctionNo: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    contractorCompanyId: Mapped[str] = mapped_column(String, nullable=False)
    plantId: Mapped[str] = mapped_column(String, nullable=False)  # requesting plant (approval scope)
    action: Mapped[str] = mapped_column(String, nullable=False)  # SUSPEND | BLACKLIST | REINSTATE
    reasonCategory: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [{type, id, label}]
    effectiveUntil: Mapped[datetime | None] = _dt()
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING_APPROVAL")  # PENDING_APPROVAL | APPROVED | REJECTED
    previousStatus: Mapped[str | None] = mapped_column(String)
    requestedById: Mapped[str] = mapped_column(String, nullable=False)
    requestedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decidedById: Mapped[str | None] = mapped_column(String)
    decidedAt: Mapped[datetime | None] = _dt()
    decisionNotes: Mapped[str | None] = mapped_column(Text)
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()
    __table_args__ = (
        Index("ix_CsmSanction_company", "contractorCompanyId"),
        Index("ix_CsmSanction_plant_status", "plantId", "status"),
    )


__all__ = [
    "CsmQuestionnaireTemplate", "CsmPrequalification", "CsmPortalToken", "CsmPortalAccessLog", "CsmDocument",
    "CsmWorkOrder", "CsmWorkOrderWorker", "CsmEvaluation", "CsmSanction",
]
