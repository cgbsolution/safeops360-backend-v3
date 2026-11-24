"""Exception rules registry — one module per rule, each exposing
RULE_KEY, SEVERITY and evaluate(snapshot) -> list[ExceptionDraft]."""

from __future__ import annotations

import logging

from app.services.ptw_ops.rules import (
    approval_sla_breach,
    competency_expiring,
    gas_test_overdue,
    handover_unacknowledged,
    hot_work_blocked_by_impairment,
    impairment_controls_missing,
    impairment_overdue,
    loto_not_verified,
    permit_expired_open,
    simops_conflict,
)
from app.services.ptw_ops.types import ExceptionDraft, Snapshot

RULES = (
    permit_expired_open,
    simops_conflict,
    impairment_overdue,
    hot_work_blocked_by_impairment,
    impairment_controls_missing,
    gas_test_overdue,
    loto_not_verified,
    approval_sla_breach,
    competency_expiring,
    handover_unacknowledged,
)
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
log = logging.getLogger("safeops360.ptw_ops")


def evaluate_all(snap: Snapshot) -> list[ExceptionDraft]:
    """Every rule over the snapshot, de-duplicated, sorted critical → low then
    oldest first. A rule that raises is skipped (and logged) so one bad row
    cannot blank the whole list."""
    out: list[ExceptionDraft] = []
    for rule in RULES:
        try:
            out.extend(rule.evaluate(snap))
        except Exception:  # noqa: BLE001
            log.exception("PTW exception rule %s failed", rule.RULE_KEY)
    seen: set[str] = set()
    uniq: list[ExceptionDraft] = []
    for d in out:
        if d.dedupeKey not in seen:
            seen.add(d.dedupeKey)
            uniq.append(d)
    uniq.sort(key=lambda d: (SEVERITY_ORDER.get(d.severity, 9), d.since or snap.now))
    return uniq
