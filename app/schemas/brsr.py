"""Pydantic contracts for the BRSR Reporting module."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── Cycles ──────────────────────────────────────────────────────────────────


class CycleCreate(BaseModel):
    financialYear: str = Field(..., description='e.g. "FY2025-26"')
    periodStart: datetime
    periodEnd: datetime
    entityName: str | None = None
    cin: str | None = None
    stockExchangeCodes: list[str] = []
    registeredOfficeAddress: str | None = None
    contactName: str | None = None
    contactEmail: str | None = None
    contactPhone: str | None = None


class CycleUpdate(BaseModel):
    entityName: str | None = None
    cin: str | None = None
    stockExchangeCodes: list[str] | None = None
    registeredOfficeAddress: str | None = None
    contactName: str | None = None
    contactEmail: str | None = None
    contactPhone: str | None = None
    assuranceProvider: str | None = None
    assuranceType: str | None = None
    notes: str | None = None


class CycleTransition(BaseModel):
    status: str
    # Recorded when moving to FILED — what the entity filed, through its own
    # SEBI process. SafeOps360 does not submit.
    filingReference: str | None = None
    notes: str | None = None


class CycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    financialYear: str
    periodStart: datetime
    periodEnd: datetime
    status: str
    entityName: str | None = None
    cin: str | None = None
    stockExchangeCodes: list[str] = []
    registeredOfficeAddress: str | None = None
    contactName: str | None = None
    contactEmail: str | None = None
    contactPhone: str | None = None
    assuranceProvider: str | None = None
    assuranceType: str | None = None
    completionPct: float = 0
    autoPopulatedPct: float = 0
    lastComputedAt: datetime | None = None
    approvedById: str | None = None
    approvedAt: datetime | None = None
    filedById: str | None = None
    filedAt: datetime | None = None
    filingReference: str | None = None
    snapshotHash: str | None = None
    notes: str | None = None
    createdAt: datetime
    updatedAt: datetime


# ── Principles ──────────────────────────────────────────────────────────────


class PrincipleResponseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cycleId: str
    principle: str
    title: str | None = None
    status: str
    # False for P2/P4/P7/P8 — drives the "not sourced from platform data"
    # banner. Sent explicitly rather than inferred client-side from an empty
    # figure list, which would look identical to "no data entered yet".
    isPlatformSourced: bool = False
    ownerUserId: str | None = None
    dueDate: datetime | None = None
    totalIndicators: int = 0
    answeredIndicators: int = 0
    autoPopulatedIndicators: int = 0
    manualPendingIndicators: int = 0
    completionPct: float = 0
    autoPopulatedPct: float = 0
    narrative: str | None = None
    reviewedById: str | None = None
    reviewedAt: datetime | None = None
    reviewNotes: str | None = None
    lastComputedAt: datetime | None = None


class PrincipleResponseUpdate(BaseModel):
    narrative: str | None = None
    ownerUserId: str | None = None
    dueDate: datetime | None = None
    status: str | None = None
    reviewNotes: str | None = None


# ── Indicators + values ─────────────────────────────────────────────────────


class IndicatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    section: str
    principle: str | None = None
    indicatorClass: str | None = None
    label: str
    groupLabel: str | None = None
    guidance: str | None = None
    valueType: str
    unit: str | None = None
    tableSchemaJson: list | None = None
    isMandatory: bool = True
    displayOrder: int = 0
    sebiFormatVersion: str | None = None


class SourceRecordRef(BaseModel):
    """One drill-through target.

    `label` is required, not optional — the platform's standing rule is that a
    raw record id is never rendered to a user.
    """

    module: str
    entity: str
    id: str
    label: str


class IndicatorValueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    indicatorCode: str
    valueNumber: float | None = None
    valueText: str | None = None
    valueBoolean: bool | None = None
    valueJson: Any | None = None
    unit: str | None = None
    provenance: str = "MANUAL"
    notApplicableReason: str | None = None
    sourceModule: str | None = None
    resolverKey: str | None = None
    sourceRecordRefs: list[SourceRecordRef] | None = None
    sourceRecordCount: int | None = None
    derivationNote: str | None = None
    computedAt: datetime | None = None
    autoValueNumber: float | None = None
    autoValueText: str | None = None
    overrideReason: str | None = None
    overriddenById: str | None = None
    overriddenAt: datetime | None = None
    isVerified: bool = False
    verifiedById: str | None = None
    verifiedAt: datetime | None = None
    evidenceNote: str | None = None


class IndicatorWithValue(BaseModel):
    """What the principle-response screen renders one row from."""

    indicator: IndicatorOut
    value: IndicatorValueOut | None = None


class PrincipleDetailOut(BaseModel):
    response: PrincipleResponseOut
    essentialIndicators: list[IndicatorWithValue] = []
    leadershipIndicators: list[IndicatorWithValue] = []


class IndicatorValueWrite(BaseModel):
    """A manual entry, or an override of an auto-populated figure.

    The server decides the resulting provenance — a client cannot declare its
    own figure AUTO. Overriding an existing AUTO value requires `overrideReason`
    and is rejected without one.
    """

    valueNumber: float | None = None
    valueText: str | None = None
    valueBoolean: bool | None = None
    valueJson: Any | None = None
    unit: str | None = None
    notApplicable: bool = False
    notApplicableReason: str | None = None
    overrideReason: str | None = None
    evidenceNote: str | None = None


class IndicatorVerify(BaseModel):
    isVerified: bool = True
    evidenceNote: str | None = None


# ── Environmental capture ───────────────────────────────────────────────────


class EnvLineWrite(BaseModel):
    stream: str
    categoryCode: str
    flowType: str = "NA"
    destination: str = "NA"
    treatmentLevel: str | None = None
    quantity: float | None = None
    unit: str | None = None
    dataQuality: str | None = None
    evidenceNote: str | None = None
    notes: str | None = None


class EnvLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    stream: str
    categoryCode: str
    categoryLabel: str | None = None
    flowType: str = "NA"
    destination: str = "NA"
    treatmentLevel: str | None = None
    quantity: float | None = None
    unit: str | None = None
    scope: str | None = None
    emissionFactorId: str | None = None
    factorValue: float | None = None
    factorPerUnit: str | None = None
    factorSource: str | None = None
    computedTCo2e: float | None = None
    dataQuality: str | None = None
    evidenceNote: str | None = None
    notes: str | None = None


class EnvMetricCreate(BaseModel):
    siteId: str
    periodLabel: str
    periodStart: datetime | None = None
    periodEnd: datetime | None = None
    turnoverInr: float | None = None
    productionVolume: float | None = None
    productionUnit: str | None = None
    scope3TCo2e: float | None = None
    scope3Methodology: str | None = None
    isWaterPositive: bool | None = None
    hasZeroLiquidDischarge: bool | None = None
    consentStatus: str | None = None
    notes: str | None = None


class EnvMetricUpdate(BaseModel):
    periodStart: datetime | None = None
    periodEnd: datetime | None = None
    turnoverInr: float | None = None
    productionVolume: float | None = None
    productionUnit: str | None = None
    scope3TCo2e: float | None = None
    scope3Methodology: str | None = None
    isWaterPositive: bool | None = None
    hasZeroLiquidDischarge: bool | None = None
    consentStatus: str | None = None
    notes: str | None = None
    lines: list[EnvLineWrite] | None = None


class EnvMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cycleId: str
    siteId: str
    siteName: str | None = None
    periodLabel: str
    periodStart: datetime | None = None
    periodEnd: datetime | None = None
    status: str
    turnoverInr: float | None = None
    productionVolume: float | None = None
    productionUnit: str | None = None
    scope3TCo2e: float | None = None
    scope3Methodology: str | None = None
    isWaterPositive: bool | None = None
    hasZeroLiquidDischarge: bool | None = None
    consentStatus: str | None = None
    submittedById: str | None = None
    submittedAt: datetime | None = None
    verifiedById: str | None = None
    verifiedAt: datetime | None = None
    notes: str | None = None
    lines: list[EnvLineOut] = []


class EnvSlotOut(BaseModel):
    """A capture slot the form renders, whether or not it holds a value yet."""

    stream: str
    categoryCode: str
    categoryLabel: str
    flowType: str
    destination: str
    destinationLabel: str | None = None
    unit: str | None = None
    scope: str | None = None
    guidance: str | None = None


class EnvTotalsOut(BaseModel):
    sitesReporting: int = 0
    energyTotalGj: float = 0
    energyRenewableGj: float = 0
    energyNonRenewableGj: float = 0
    waterWithdrawnKl: float = 0
    waterDischargedKl: float = 0
    waterConsumedKl: float = 0
    waterRecycledKl: float = 0
    scope1TCo2e: float = 0
    scope2TCo2e: float = 0
    scope3TCo2e: float | None = None
    wasteGeneratedT: float = 0
    wasteRecoveredT: float = 0
    wasteDisposedT: float = 0
    wasteDivertedPct: float | None = None
    turnoverInr: float | None = None
    # Never hidden: an unresolved emission line is a hole in the disclosure.
    unresolvedEmissionLines: int = 0


class EmissionFactorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    factorType: str
    scope: str
    factorValue: float
    perUnit: str
    source: str
    sourceYear: str | None = None
    region: str | None = None
    isActive: bool = True


# ── Engine + dashboard ──────────────────────────────────────────────────────


class SweepResultOut(BaseModel):
    """What a mapping-engine run did.

    `misconfigured` and `failed` are surfaced rather than logged away — a
    mapping that silently stopped populating is exactly how a disclosure ends
    up looking sourced when it is not.
    """

    populated: int = 0
    skippedManual: int = 0
    noData: int = 0
    failed: list[dict] = []
    misconfigured: list[str] = []
    completion: dict[str, Any] = {}


class PrincipleProgressOut(BaseModel):
    principle: str
    title: str
    status: str
    isPlatformSourced: bool
    totalIndicators: int
    answeredIndicators: int
    autoPopulatedIndicators: int
    manualPendingIndicators: int
    completionPct: float
    autoPopulatedPct: float


class CycleDashboardOut(BaseModel):
    cycle: CycleOut
    principles: list[PrincipleProgressOut] = []
    environmental: EnvTotalsOut
    unverifiedAutoCount: int = 0
    sitesExpected: int = 0
    sitesReported: int = 0


class TrendPointOut(BaseModel):
    financialYear: str
    status: str
    energyTotalGj: float | None = None
    scope1TCo2e: float | None = None
    scope2TCo2e: float | None = None
    waterConsumedKl: float | None = None
    wasteGeneratedT: float | None = None
    turnoverInr: float | None = None
    # Intensities are None where the denominator is missing — never zero, and
    # never silently dropped from the series.
    energyIntensity: float | None = None
    emissionsIntensity: float | None = None
    waterIntensity: float | None = None
    ltifr: float | None = None


__all__ = [
    "CycleCreate",
    "CycleUpdate",
    "CycleTransition",
    "CycleOut",
    "CycleDashboardOut",
    "PrincipleResponseOut",
    "PrincipleResponseUpdate",
    "PrincipleDetailOut",
    "PrincipleProgressOut",
    "IndicatorOut",
    "IndicatorValueOut",
    "IndicatorValueWrite",
    "IndicatorWithValue",
    "IndicatorVerify",
    "SourceRecordRef",
    "EnvLineWrite",
    "EnvLineOut",
    "EnvMetricCreate",
    "EnvMetricUpdate",
    "EnvMetricOut",
    "EnvSlotOut",
    "EnvTotalsOut",
    "EmissionFactorOut",
    "SweepResultOut",
    "TrendPointOut",
]
