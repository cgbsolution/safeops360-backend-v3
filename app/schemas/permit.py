from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.permit import (
    PermitHazardType,
    PermitOutcome,
    PermitStatus,
    PermitType,
    PermitUnexecutedReason,
    PrecautionResponse,
)


# ─── Closed-loop field evidence (GPS + photo + signature) ─────────────
#
# Carried by every lifecycle action payload. Per-action requirements are
# policy in app/services/ptw_evidence.py (EVIDENCE_POLICY) — the schema
# keeps everything optional so validation errors are aggregated there.


class PtwEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    gpsAccuracyMeters: float | None = None
    # Drawn signature as a data-URL PNG (canvas.toDataURL). ~5-20 KB.
    signatureImageBase64: str | None = None
    # PermitAttachment ids uploaded beforehand via POST /api/ptw/{id}/attachments.
    photoAttachmentIds: list[str] | None = None
    declarationText: str | None = None


# ─── Sub-payloads for the wizard's multi-row sub-forms ────────────────


class IsolationInput(BaseModel):
    """One row from PTW Step 4 — Isolations."""
    model_config = ConfigDict(extra="ignore")

    isolationType: str   # FK by id to MasterItem(ISOLATION_TYPE)
    description: str = Field(min_length=1)
    isolationPointTag: str = Field(min_length=1)
    lotoTagNumber: str | None = None


class ToolEquipmentInput(BaseModel):
    """One row from PTW Step 5 — Tools/Equipment used by the crew."""
    model_config = ConfigDict(extra="ignore")

    equipmentId: str | None = None
    freeTextDescription: str | None = None


class SubjectEquipmentInput(BaseModel):
    """One row from PTW Step 5 — Equipment being worked on."""
    model_config = ConfigDict(extra="ignore")

    equipmentId: str
    workNature: Literal["INSPECTION", "REPAIR", "REPLACEMENT", "MODIFICATION"]


class GasTestParameterSpec(BaseModel):
    """One parameter in the gas test plan (PTW Step 6)."""
    model_config = ConfigDict(extra="allow")

    parameter: str  # O2 | LEL | CO | H2S | OTHER
    lowLimit: float | None = None
    highLimit: float | None = None
    unit: str   # %, ppm, etc.


class GasTestPlanInput(BaseModel):
    """1-to-1 gas test plan attached to the permit (PTW Step 6)."""
    model_config = ConfigDict(extra="ignore")

    refreshFrequencyMinutes: int = Field(ge=15, le=480, default=120)
    parametersToTest: list[GasTestParameterSpec]
    instrumentSerial: str | None = None
    instrumentLastCalibrated: datetime | None = None


class CrewMemberInput(BaseModel):
    """One row from PTW Step 3 — Work crew."""
    model_config = ConfigDict(extra="ignore")

    userId: str
    role: Literal["OPERATOR", "HELPER", "SUPERVISOR", "TECHNICIAN", "WORKER", "CONTRACTOR"] = "WORKER"


# ─── Hazard annexures + precaution checklists ──────────────────────────
#
# A permit carries ONE base `type` plus N hazard annexures, each with its own
# precaution checklist. Rules (workflow routing, validity caps, mandatory
# controls, competency) live in app/services/ptw_hazards.py; completeness
# lives in app/services/ptw_annexures.py.


class PrecautionAnswerInput(BaseModel):
    """One answer to one catalog item."""

    model_config = ConfigDict(extra="ignore")

    itemId: str
    response: PrecautionResponse
    # Required when response is NA — enforced in the completeness gate rather
    # than here, so the user gets one aggregated blocker instead of a field
    # error per line.
    remark: str | None = None


class HazardAnnexureInput(BaseModel):
    """One hazard attached to a permit, with its checklist answers.

    The base type's own annexure is created server-side whether or not it
    appears here, so a client that sends nothing still gets the base
    checklist.
    """

    model_config = ConfigDict(extra="ignore")

    hazardType: PermitHazardType
    answers: list[PrecautionAnswerInput] = []
    notes: str | None = None


class PrecautionItemOut(BaseModel):
    """A catalog row, as the wizard renders it."""

    id: str
    hazardType: PermitHazardType
    sequence: int
    text: str
    isMandatory: bool
    allowsNA: bool
    sourceRef: str | None = None

    model_config = {"from_attributes": True}


class PrecautionAnswerOut(BaseModel):
    itemId: str
    response: PrecautionResponse
    remark: str | None = None
    respondedById: str | None = None
    respondedAt: datetime | None = None

    model_config = {"from_attributes": True}


class HazardAnnexureOut(BaseModel):
    id: str
    hazardType: PermitHazardType
    label: str
    isPrimary: bool
    completedAt: datetime | None = None
    completedById: str | None = None
    notes: str | None = None
    items: list[PrecautionItemOut] = []
    answers: list[PrecautionAnswerOut] = []
    # Mirrors ptw_annexures.AnnexureVerdict so the panel and the API agree.
    complete: bool = False
    unanswered: list[str] = []
    refused: list[str] = []
    naWithoutRemark: list[str] = []


class AnnexureCompletenessOut(BaseModel):
    """GET /api/ptw/{id}/annexures — the shared verdict the UI reads and the
    API enforces."""

    complete: bool
    blocker: str | None = None
    effectiveRiskType: PermitType | None = None
    validityCapHours: int | None = None
    requiredControls: list[str] = []
    precautionsCertifiedAt: datetime | None = None
    precautionsCertifiedById: str | None = None
    annexures: list[HazardAnnexureOut] = []


class AnnexureAnswersRequest(BaseModel):
    """PUT /api/ptw/{id}/annexures/{hazardType}/answers — replace the answers
    on one annexure. Partial sets are accepted; the completeness gate is what
    decides whether the permit may proceed."""

    model_config = ConfigDict(extra="ignore")

    answers: list[PrecautionAnswerInput]
    notes: str | None = None


class AnnexureAttachRequest(BaseModel):
    """POST /api/ptw/{id}/annexures — attach a hazard to an existing permit."""

    model_config = ConfigDict(extra="ignore")

    hazardType: PermitHazardType


class PermitCreate(BaseModel):
    """Create-permit payload from the 8-step wizard. All Step 1 / Step 2 /
    Step 3 / Step 8 fields are required; Steps 4-7 are conditional on the
    permit type's metadata (e.g. gas test plan only for Hot Work / Confined
    Space). Legacy fields are kept for back-compat with older clients."""
    model_config = ConfigDict(extra="ignore")

    # ─── Required core ───
    type: PermitType
    # Additional hazard annexures beyond the one `type` implies. The permit
    # stays ONE record with one number, one validity window and one closure;
    # each extra hazard adds its checklist, tightens the validity cap and can
    # escalate the approval chain. See app/services/ptw_hazards.py.
    hazards: list[HazardAnnexureInput] = []
    plantId: str
    location: str = Field(min_length=1)
    scopeOfWork: str = Field(min_length=10)
    validFrom: datetime
    validTo: datetime
    issuerId: str
    receiverId: str

    # ─── Step 1/2 additions ───
    departmentId: str | None = None
    areaId: str | None = None
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    workOrderNumber: str | None = None
    attachedDrawingIds: list[str] = []

    # ─── HIRA provenance (set by the Create-PTW prompt on a hazard row) ───
    hiraEntryId: str | None = None
    hiraEntryHazardId: str | None = None

    # ─── LOTO cross-reference (LOTO spec §6 / Part A) ───
    # Optional at every permit type in Part A — LOTO is never mandatory here.
    # Set from the wizard's Isolations step (step 4) when the originator links
    # or starts a lockout. A REFERENCE only: LOTO owns the isolation record.
    # Validated on create (must exist, same plant, not already claimed by
    # another permit); enforced again at close-out by the workflow engine.
    lotoExecutionId: str | None = None

    # ─── Step 3 additions ───
    workCrew: list[CrewMemberInput] = []
    fireWatchPersonId: str | None = None
    standbyPersonId: str | None = None

    # ─── Step 4: Isolations ───
    isolations: list[IsolationInput] = []

    # ─── Step 5: PPE & Equipment ───
    requiredPpe: list[str] | None = None  # PPE codes — accepted; legacy ppeChecklist also kept
    toolsEquipment: list[ToolEquipmentInput] = []
    subjectEquipment: list[SubjectEquipmentInput] = []

    # ─── Step 6: Gas Test Plan ───
    gasTestPlan: GasTestPlanInput | None = None

    # ─── Step 7: Additional Controls ───
    weatherConditionsAtIssue: str | None = None
    windSpeedKmh: float | None = None
    adjacentAreaNotifications: dict[str, Any] | None = None

    # ─── FLRA policy override (closed-loop rebuild). None → resolved from
    #     instance config (PTW_FLRA_REQUIRED_DEFAULT / _TYPES). ───
    flraRequired: bool | None = None

    # ─── Legacy fields (kept for back-compat with single-page form) ───
    contractorName: str | None = None
    contractorCompanyId: str | None = None  # structured contractor link
    isolationsRequired: str | None = None  # CSV
    ppeChecklist: str | None = None        # JSON string
    gasTestRequired: bool = False
    gasTestResult: str | None = None
    o2Level: str | None = None
    lelLevel: str | None = None
    h2sLevel: str | None = None
    fireWatchRequired: bool = False
    rescuePlan: str | None = None


class PermitUpdate(BaseModel):
    """Edit the core details of a permit while it is still open (DRAFT /
    SUBMITTED — before any approval). Child collections (crew, isolations,
    gas plan, tools) are NOT edited here; they belong to the create wizard /
    active-phase panels. All fields optional — only keys sent are applied.
    Router enforces PTW.UPDATE + the open-status guard."""
    model_config = ConfigDict(extra="ignore")

    type: PermitType | None = None
    location: str | None = Field(default=None, min_length=1)
    scopeOfWork: str | None = Field(default=None, min_length=10)
    validFrom: datetime | None = None
    validTo: datetime | None = None
    departmentId: str | None = None
    areaId: str | None = None
    specificLocation: str | None = None
    workOrderNumber: str | None = None
    weatherConditionsAtIssue: str | None = None
    windSpeedKmh: float | None = None
    contractorName: str | None = None
    contractorCompanyId: str | None = None

    # LOTO cross-reference. Settable here only while the permit is still open
    # (this endpoint's own DRAFT/SUBMITTED guard), and `None` in an
    # exclude_unset payload means "not supplied" — it never clears the link by
    # omission. The general-purpose path that works at ANY permit status, and
    # that refuses to unlink while the lockout is still open, is
    # POST /api/loto/permits/{permitId}/link.
    lotoExecutionId: str | None = None


class PermitOut(BaseModel):
    id: str
    number: str
    type: PermitType
    plantId: str
    areaId: str | None
    location: str
    scopeOfWork: str
    validFrom: datetime
    validTo: datetime
    originatorId: str
    issuerId: str | None
    receiverId: str | None
    contractorName: str | None
    contractorCompanyId: str | None = None
    status: PermitStatus
    issuerApprovedAt: datetime | None
    safetyApprovedAt: datetime | None
    plantHeadApprovedAt: datetime | None
    closedAt: datetime | None
    suspendedAt: datetime | None
    suspendedReason: str | None
    expiredAt: datetime | None
    createdAt: datetime
    updatedAt: datetime

    # Phase-1+ additions (read-back)
    validityHours: int | None = None
    departmentId: str | None = None
    specificLocation: str | None = None
    gpsLatitude: float | None = None
    gpsLongitude: float | None = None
    workOrderNumber: str | None = None
    # HIRA provenance — non-null when this permit was raised from a hazard row.
    hiraEntryId: str | None = None
    hiraEntryHazardId: str | None = None
    # LOTO cross-reference — non-null when a lockout is linked to this permit.
    # Read-back so any consumer (web, mobile, export, integration) can see the
    # link from the permit itself rather than having to call the LOTO module.
    lotoExecutionId: str | None = None
    fireWatchPersonId: str | None = None
    standbyPersonId: str | None = None
    weatherConditionsAtIssue: str | None = None
    windSpeedKmh: float | None = None
    activatedAt: datetime | None = None
    activatedById: str | None = None
    currentActiveFlraId: str | None = None
    isCurrentlySuspended: bool = False
    returnedAt: datetime | None = None
    returnedById: str | None = None
    siteVerifiedAt: datetime | None = None
    siteVerifiedById: str | None = None
    closingRemark: str | None = None

    # ─── Closed-loop rebuild additions ───
    flraRequired: bool = False
    issuedAt: datetime | None = None
    issuedById: str | None = None
    workCompletedAt: datetime | None = None
    workCompletedById: str | None = None
    outcome: PermitOutcome | None = None
    returnNotes: str | None = None
    closedById: str | None = None
    rejectionReason: str | None = None
    cancelledAt: datetime | None = None
    cancelledById: str | None = None
    cancellationReason: str | None = None
    isArchived: bool = False
    archivedAt: datetime | None = None

    # ─── Multi-hazard annexures ───
    # effectiveRiskType is the chain that actually ran (the UNION across all
    # hazards); it equals `type` on a single-hazard permit.
    effectiveRiskType: PermitType | None = None
    precautionsCertifiedAt: datetime | None = None
    precautionsCertifiedById: str | None = None

    model_config = {"from_attributes": True}


class SuspendRequest(BaseModel):
    reason: str = Field(min_length=1)
    evidence: PtwEvidenceInput | None = None


class ResumeRequest(BaseModel):
    comments: str | None = None
    evidence: PtwEvidenceInput | None = None


class AdminResetRequest(BaseModel):
    status: str  # DRAFT or SUBMITTED only


# ─── Closed-loop lifecycle actions ─────────────────────────────────────


class AcceptRequest(BaseModel):
    """Receiver accepts the ISSUED permit at the worksite. First-class
    signed act: declaration + signature + GPS + onsite photo."""

    model_config = ConfigDict(extra="ignore")

    comments: str | None = None
    evidence: PtwEvidenceInput


class CompleteRequest(BaseModel):
    """Receiver declares Work Completed — structured outcome + close-out
    narrative + evidence. Replaces the legacy Return step (the two
    restoration booleans carry over here)."""

    model_config = ConfigDict(extra="ignore")

    outcome: PermitOutcome
    isolationsRestored: bool
    workAreaClean: bool
    notes: str | None = None
    evidence: PtwEvidenceInput


class HandbackRequest(BaseModel):
    """Handback inspection (legacy name: site verification) — the issuer /
    safety officer walks the site after Work Completed."""

    model_config = ConfigDict(extra="ignore")

    checklist: dict[str, bool]
    notes: str | None = None
    evidence: PtwEvidenceInput


class CancelRequest(BaseModel):
    """Operational cancellation (distinct from an approver rejection)."""

    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5)
    evidence: PtwEvidenceInput | None = None


class WithdrawUnexecutedRequest(BaseModel):
    """Close out a permit that expired before the receiver ever acknowledged
    it. NOT a work-completed declaration — no work happened, so there is no
    outcome to record, only a reason the permit went unused.

    Evidence is signer identity + GPS (no onsite photo — see EVIDENCE_POLICY)."""

    model_config = ConfigDict(extra="ignore")

    reasonCode: PermitUnexecutedReason
    notes: str | None = None
    evidence: PtwEvidenceInput | None = None


class IsolationVerifyRequest(BaseModel):
    """Confirm one isolation point is physically locked-out and tagged.
    Safety-critical — carries evidence."""

    model_config = ConfigDict(extra="ignore")

    notes: str | None = None
    evidence: PtwEvidenceInput


class ExtensionRequestIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    newValidTo: datetime
    reason: str = Field(min_length=5)
    evidence: PtwEvidenceInput | None = None


class ExtensionDecisionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: Literal["APPROVED", "REJECTED"]
    approverComments: str | None = None
    evidence: PtwEvidenceInput | None = None


# ─── Attachments (two-phase signed-URL upload, mirrors incidents) ──────

PERMIT_ATTACHMENT_CATEGORIES = {
    "SCOPE_DRAWING",
    "SITE_PHOTO",
    "MSDS",
    "TPI_CERT",
    "RESCUE_PLAN",
    "RETURN_PHOTO",
    "VERIFICATION_PHOTO",
    "CLOSURE_DOC",
    "ACTION_EVIDENCE_PHOTO",
}


class PermitAttachmentInit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    phase: Literal["init"]
    category: str
    fileName: str = Field(min_length=1)
    fileSize: int = Field(gt=0)
    mimeType: str = Field(min_length=3)


class PermitAttachmentOut(BaseModel):
    id: str
    permitId: str
    actionEvidenceId: str | None = None
    category: str
    fileName: str
    fileSize: int
    mimeType: str
    caption: str | None = None
    uploadedById: str
    uploadedAt: datetime

    model_config = {"from_attributes": True}
