"""Plain dataclasses the pure layer works on. Loaders fill them; nothing here
touches the DB, so every rule and the payload builder are unit-testable."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.ptw_ops import steps


@dataclass
class PermitView:
    id: str
    number: str
    type: str
    status: str
    plantId: str
    areaId: str | None
    validFrom: datetime
    validTo: datetime
    createdAt: datetime | None = None
    workflowStepName: str | None = None
    stepEnteredAt: datetime | None = None        # last PermitEvent into the current step
    pendingAssigneeId: str | None = None         # open WorkflowTask owner (approval steps)
    stepSlaMin: int | None = None                # SLA of the permit's own current workflow step
    issuerId: str | None = None
    receiverId: str | None = None
    receiverName: str | None = None
    contractorCompanyId: str | None = None
    contractorName: str | None = None
    closureType: str | None = None
    closedAt: datetime | None = None
    activatedAt: datetime | None = None
    workCompletedAt: datetime | None = None
    cancelledAt: datetime | None = None
    suspendedReason: str | None = None
    crewCount: int = 0
    crewCheckedIn: int = 0
    gasRequired: bool = False
    lastGasAt: datetime | None = None
    gasRefreshDueBy: datetime | None = None
    lastGasExceedance: bool = False
    isolationsTotal: int = 0
    isolationsUnverified: int = 0
    lotoExecutionStatus: str | None = None
    flraRequired: bool = False
    hasActiveFlra: bool = False
    satisfiedControls: set[str] = field(default_factory=set)
    crewCertMinExpiry: datetime | None = None     # earliest cert expiry across active crew
    crewCertExpiringCount: int = 0
    crewCertExpiring7d: int = 0
    lastActivityAt: datetime | None = None        # newest evidence / gas reading / event

    @property
    def step(self) -> str:
        return steps.step_key(self.status, self.workflowStepName)


@dataclass
class ImpairmentView:
    id: str
    code: str
    plantId: str
    areaId: str | None
    systemType: str
    systemName: str
    reason: str
    status: str
    startedAt: datetime
    plannedRestoreAt: datetime | None
    restoredAt: datetime | None = None
    precautions: list[str] = field(default_factory=list)
    approvedById: str | None = None
    approvedByName: str | None = None
    createdById: str | None = None
    escalationCount: int = 0
    lastEscalatedAt: datetime | None = None

    def is_in_force(self, now: datetime) -> bool:
        """Active, or approved with its start time reached. Restored/cancelled never."""
        if self.restoredAt is not None or self.status in ("restored", "cancelled", "requested"):
            return False
        return self.status == "active" or (self.status == "approved" and self.startedAt <= now)


@dataclass
class ImpairmentRuleView:
    id: str
    systemType: str
    permitType: str          # a PermitType value or "*"
    effect: str              # block_new | require_controls | allow
    requiredControls: list[str] = field(default_factory=list)
    message: str | None = None
    plantId: str | None = None


@dataclass
class SimopsRuleView:
    id: str
    permitTypeA: str          # PermitType value or "*"
    permitTypeB: str
    minDistanceM: float | None
    sameArea: bool
    severity: str = "critical"
    plantId: str | None = None


@dataclass
class ZoneView:
    id: str
    name: str
    polygon: list[dict[str, float]] = field(default_factory=list)   # [{x,y}] 0-100
    sortOrder: int | None = None
    ownerUserId: str | None = None
    ownerName: str | None = None


@dataclass
class LayoutView:
    widthM: float | None = None      # real-world metres across the image width
    heightM: float | None = None     # ... and down its height
    imageUrl: str | None = None


@dataclass
class ExceptionDraft:
    ruleKey: str
    severity: str
    dedupeKey: str
    title: str
    plantId: str
    areaId: str | None = None
    permitId: str | None = None
    impairmentId: str | None = None
    detail: str | None = None
    ownerUserId: str | None = None
    since: datetime | None = None     # when the condition started (for "since")
    ref: str | None = None            # permit number / impairment code


@dataclass
class Snapshot:
    """Everything the rules and the payload need for one plant at one instant."""
    now: datetime
    plantId: str
    permits: list[PermitView]
    impairments: list[ImpairmentView] = field(default_factory=list)
    impairmentRules: list[ImpairmentRuleView] = field(default_factory=list)
    simopsRules: list[SimopsRuleView] = field(default_factory=list)
    zones: list[ZoneView] = field(default_factory=list)
    layout: LayoutView = field(default_factory=LayoutView)
    stepSlaMin: dict[str, int | None] = field(default_factory=dict)
    shifts: list[dict[str, Any]] = field(default_factory=list)
    areaOwners: dict[str, str | None] = field(default_factory=dict)

    def zone(self, area_id: str | None) -> ZoneView | None:
        return next((z for z in self.zones if z.id == area_id), None)
