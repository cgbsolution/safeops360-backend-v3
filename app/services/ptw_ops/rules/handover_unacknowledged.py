"""handover_unacknowledged [low] — an ACTIVE permit carried over a shift change
with nothing recorded on it since the new shift began (no gas reading, no
lifecycle evidence, no step change) 30 min into that shift.

The platform has no handover entity; this is the proxy agreed in
docs/ptw-control-center/PLAN.md §7."""

from __future__ import annotations

from datetime import timedelta

from app.services.ptw_ops.shifts import current_shift
from app.services.ptw_ops.types import ExceptionDraft, Snapshot

RULE_KEY = "handover_unacknowledged"
SEVERITY = "low"
GRACE = timedelta(minutes=30)


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    code, start, _ = current_shift(snap.now, snap.shifts)
    if snap.now - start < GRACE:
        return []
    out = []
    for p in snap.permits:
        if p.status != "ACTIVE":
            continue
        began = p.activatedAt or p.validFrom
        if began >= start:
            continue
        if p.lastActivityAt is not None and p.lastActivityAt >= start:
            continue
        out.append(ExceptionDraft(
            ruleKey=RULE_KEY, severity=SEVERITY, dedupeKey=f"{RULE_KEY}:{p.id}:{start.isoformat()}",
            plantId=p.plantId, areaId=p.areaId, permitId=p.id, ref=p.number, since=start + GRACE,
            ownerUserId=p.receiverId or p.issuerId,
            title=f"Carried into Shift {code} — handover not acknowledged",
            detail="The incoming holder confirms the job on site (gas reading, evidence or comment).",
        ))
    return out
