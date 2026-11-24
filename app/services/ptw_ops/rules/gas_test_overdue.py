"""gas_test_overdue [high] — active permit needing gas tests, now > next_due_at
(or no reading at all 15 min after activation)."""

from __future__ import annotations

from datetime import timedelta

from app.services.ptw_ops.rules._util import fmt_dur, mins
from app.services.ptw_ops.types import ExceptionDraft, Snapshot

RULE_KEY = "gas_test_overdue"
SEVERITY = "high"
MISSING_GRACE = timedelta(minutes=15)


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    out = []
    for p in snap.permits:
        if p.status != "ACTIVE" or not p.gasRequired:
            continue
        if p.lastGasAt is None:
            started = p.activatedAt or p.validFrom
            if snap.now - started < MISSING_GRACE:
                continue
            due, title = started + MISSING_GRACE, "No gas reading recorded since activation"
        elif p.gasRefreshDueBy is not None and snap.now > p.gasRefreshDueBy:
            due = p.gasRefreshDueBy
            title = f"Gas test overdue by {fmt_dur(mins(due, snap.now))}"
        else:
            continue
        out.append(ExceptionDraft(
            ruleKey=RULE_KEY, severity=SEVERITY, dedupeKey=f"{RULE_KEY}:{p.id}", plantId=p.plantId,
            areaId=p.areaId, permitId=p.id, ref=p.number, since=due, ownerUserId=p.receiverId or p.issuerId,
            title=title, detail="Take and record a reading before work continues.",
        ))
    return out
