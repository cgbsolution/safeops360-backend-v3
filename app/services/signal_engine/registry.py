"""Signal Engine — rule registry.

Code is the source of truth for what rules exist; the `SignalRule` table is a
reconciled projection of this tuple (see `sync_rule_catalog` in the runner). A
fresh deployment therefore needs no seed script for the engine to run — the seed
script exists only so an operator can inspect and tune the catalog before the
first nightly run.
"""

from __future__ import annotations

from app.services.signal_engine.base import SignalRuleImpl
from app.services.signal_engine.rules import (
    CORRELATION_RULES,
    DATA_QUALITY_RULES,
    STATISTICAL_RULES,
    Xcorr019SilentZeroRow,
    Xcorr020ReferenceIntegrity,
)

RULES: tuple[SignalRuleImpl, ...] = (
    *CORRELATION_RULES,      # XCORR-001..008
    *STATISTICAL_RULES,      # XSTAT-001..006
    Xcorr019SilentZeroRow(),  # XCORR-019 — data quality, legacy code
    Xcorr020ReferenceIntegrity(),  # XCORR-020 — data quality, legacy code
    *DATA_QUALITY_RULES,     # XDQ-021..024
)

RULES_BY_CLASS: dict[str, tuple[SignalRuleImpl, ...]] = {
    "CORRELATION": tuple(r for r in RULES if r.rule_class == "CORRELATION"),
    "STATISTICAL": tuple(r for r in RULES if r.rule_class == "STATISTICAL"),
    "DATA_QUALITY": tuple(r for r in RULES if r.rule_class == "DATA_QUALITY"),
}

RULES_BY_CODE: dict[str, SignalRuleImpl] = {r.code: r for r in RULES}


def get_rule(code: str) -> SignalRuleImpl | None:
    return RULES_BY_CODE.get(code)


def rules_for_modules(modules: set[str]) -> list[SignalRuleImpl]:
    """Rules that read any of the given modules — the selector an
    event-triggered run (Stream 2) uses to re-evaluate only what a write can
    have changed."""
    return [r for r in RULES if set(r.source_modules) & modules]


__all__ = ["RULES", "RULES_BY_CLASS", "RULES_BY_CODE", "get_rule", "rules_for_modules"]
