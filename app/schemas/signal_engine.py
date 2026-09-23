"""Signal Engine — API contracts.

Named `signal_engine` rather than `signals` because `app.schemas.insights`
already exports a `Signal` (the row-level insight chip on a list screen). The
two are unrelated: that one is a chip, this one is a cross-module finding with
evidence and a lifecycle. The engine's type is `SignalOut` here and
`EngineSignal` on the frontend so neither ever silently stands in for the other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SignalEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sourceModule: str
    sourceRecordId: str
    sourceRecordRef: str | None = None
    weight: float
    snapshotJson: dict[str, Any] | None = None


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ruleCode: str
    ruleName: str | None = None
    # CORRELATION | STATISTICAL | DATA_QUALITY — the rule's computation class.
    ruleClass: str | None = None
    signalKey: str
    severity: str
    status: str
    category: str
    confidence: float
    siteId: str | None = None
    siteName: str | None = None
    areaId: str | None = None
    # Always populated, always the default view. `narrativeLLM` is an optional
    # rewrite and never authoritative (spec §4.5).
    narrativeTemplate: str
    narrativeLLM: str | None = None
    recommendedAction: str
    windowStart: datetime
    windowEnd: datetime
    computedAt: datetime
    firstSeenAt: datetime
    lastSeenAt: datetime
    occurrenceCount: int
    expiresAt: datetime | None = None
    thresholdSnapshot: dict[str, Any] | None = None
    acknowledgedBy: str | None = None
    acknowledgedAt: datetime | None = None
    dismissedReason: str | None = None
    linkedCapaId: str | None = None
    linkedTrainingId: str | None = None
    evidenceCount: int = 0
    evidence: list[SignalEvidenceOut] = Field(default_factory=list)


class SignalListResponse(BaseModel):
    signals: list[SignalOut]
    total: int
    limit: int
    offset: int


class SignalSummary(BaseModel):
    """Counts for the panel headers. Deliberately split by category — the
    DATA_QUALITY audience is admin/engineering and the rest is safety."""

    byStatus: dict[str, int]
    bySeverity: dict[str, int]
    byCategory: dict[str, int]
    byModule: dict[str, int]
    byRuleClass: dict[str, int]
    openTotal: int


class SignalRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    description: str
    category: str
    ruleClass: str = "CORRELATION"
    defaultSeverity: str
    sourceModules: list[str] | None = None
    windowDays: int
    defaultThresholds: dict[str, Any] | None = None
    enabled: bool
    # Effective values after the tenant override is applied.
    effectiveEnabled: bool = True
    effectiveThresholds: dict[str, Any] | None = None
    severityOverride: str | None = None
    openSignals: int = 0
    implemented: bool = True


class ResolvePayload(BaseModel):
    """Optional note recorded against a resolution."""

    note: str | None = None


class RuleOverridePatch(BaseModel):
    enabled: bool | None = None
    thresholdJson: dict[str, Any] | None = None
    severityOverride: str | None = None


class DismissPayload(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class SignalRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    runType: str
    triggerEvent: str | None = None
    startedAt: datetime
    completedAt: datetime | None = None
    durationMs: int | None = None
    rulesRun: int
    signalsEmitted: int
    signalsUpdated: int
    signalsExpired: int
    errorCount: int
    errorDetail: dict[str, Any] | None = None
    ruleDetail: dict[str, Any] | None = None


__all__ = [
    "DismissPayload",
    "RuleOverridePatch",
    "SignalEvidenceOut",
    "SignalListResponse",
    "SignalOut",
    "SignalRuleOut",
    "SignalRunOut",
    "SignalSummary",
]
