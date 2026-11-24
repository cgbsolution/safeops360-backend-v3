"""impairment_controls_missing [high] — a running permit sits under a
require_controls impairment (e.g. gas detector bypassed) without every control.

Not in the brief's rule table: it is the require_controls twin of
hot_work_blocked_by_impairment, so "an impairment raised on an area with running
permits creates a high exception for each affected permit" holds for every
effect, not only block_new."""

from __future__ import annotations

from app.services.ptw_ops.rules.hot_work_blocked_by_impairment import running_permit_drafts
from app.services.ptw_ops.types import ExceptionDraft, Snapshot

RULE_KEY = "impairment_controls_missing"
SEVERITY = "high"


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    return running_permit_drafts(snap, effect="require_controls", rule_key=RULE_KEY, severity=SEVERITY)
