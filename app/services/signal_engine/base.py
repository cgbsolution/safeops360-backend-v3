"""Signal Engine — rule contract (spec §4.2).

A rule is a PURE function over the database: it queries, it computes, it returns
candidates. It never writes, never mutates a source record, and never reaches
the network. The runner owns all persistence, so a rule cannot half-commit and
a failing rule cannot corrupt another rule's output.

Narrative rendering is a separate method from evaluation on purpose. The
deterministic template IS the shipped narrative for an airgapped tenant (§4.3),
so it is written and reviewed as product copy, not buried in query code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class EvidenceRef:
    """One source record that contributed to a signal.

    `snapshot` is frozen into the Signal at compute time — the displayed
    evidence must survive an edit or soft-delete of the source row (NFR §7).
    """

    sourceModule: str
    sourceRecordId: str
    sourceRecordRef: str | None = None
    weight: float = 1.0
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class SignalCandidate:
    """A finding, before it is persisted.

    `signalKey` is the stable identity of the real-world finding within its
    rule. Two runs that observe the same finding MUST produce the same key —
    that is what makes re-runs update rather than duplicate, and what stops an
    unacknowledged signal re-surfacing as new every night.
    """

    signalKey: str
    severity: str
    confidence: float
    facts: dict[str, Any]
    evidence: list[EvidenceRef] = field(default_factory=list)
    siteId: str | None = None
    areaId: str | None = None
    windowStart: datetime | None = None
    windowEnd: datetime | None = None
    expiresAt: datetime | None = None
    # Overrides the rule's category when a rule dual-flags (e.g. XCORR-008).
    category: str | None = None


@dataclass
class RuleContext:
    """Everything a rule is allowed to see. Tenant-scoped by construction —
    a rule receives the tenant and its resolved thresholds, never a global
    config object it could read another tenant's tuning out of."""

    db: AsyncSession
    tenant: str
    # NAIVE UTC, deliberately. Every business table on this platform was created
    # by Prisma as `timestamp without time zone`, so comparing an aware datetime
    # against `Incident.date` raises "can't subtract offset-naive and
    # offset-aware datetimes" — which is not a type warning, it is a hard
    # asyncpg DataError that kills the rule. Twelve of the twenty rules failed
    # this way on their first run.
    #
    # The Signal Engine's OWN tables are `timestamptz`, so the runner converts
    # back to aware at the persistence boundary (see `_aware` in runner.py).
    # One clock in the rules, one conversion at the edge — rather than every
    # rule remembering which kind of datetime it is holding.
    now: datetime
    # Merged default + per-tenant override, and the exact dict snapshotted onto
    # every signal this rule emits.
    thresholds: dict[str, Any]
    window_days: int
    # Notes a rule wants surfaced in the run log (unmapped codes, tables
    # skipped, etc.). Written by the rule, read by the runner — never a
    # side channel back into another rule.
    notes: dict[str, Any] = field(default_factory=dict)


class SignalRuleImpl(ABC):
    """Base class for every correlation rule.

    Named `...Impl` because `SignalRule` is the catalog TABLE (app.models.
    signal_engine). The catalog row is reconciled from these class attributes on
    every run, so adding a rule is: write the class, register it, done.
    """

    code: str = ""
    name: str = ""
    description: str = ""
    # Two axes, deliberately. `rule_class` is what KIND of computation this is
    # (the build spec's correlation / statistical / data-quality), which is how
    # the rule catalog is grouped and how coverage is argued. `category` is what
    # the finding MEANS to a safety manager, which is how the dashboard filters.
    # Collapsing them would force "a statistical outlier in audit compliance" to
    # be filed under either statistics or compliance and lose the other.
    rule_class: str = "CORRELATION"   # CORRELATION | STATISTICAL | DATA_QUALITY
    category: str = "OPERATIONAL_RISK"
    default_severity: str = "INFO"
    source_modules: tuple[str, ...] = ()
    window_days: int = 30
    default_thresholds: dict[str, Any] = {}

    @abstractmethod
    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        """Query via the shared data-access helpers and return candidates.

        No side effects, no writes, no external calls."""
        ...

    @abstractmethod
    def render_narrative(self, c: SignalCandidate) -> str:
        """Deterministic template render — a complete, client-presentable
        sentence with zero LLM involvement. For an airgapped tenant this is the
        final output, not a draft."""
        ...

    @abstractmethod
    def render_action(self, c: SignalCandidate) -> str:
        """The recommended next step, in the same deterministic register."""
        ...


__all__ = ["EvidenceRef", "RuleContext", "SignalCandidate", "SignalRuleImpl"]
