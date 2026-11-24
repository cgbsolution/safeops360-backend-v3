"""loto_not_verified [high] — issued/active permit with isolation points not yet
verified, or a linked LOTO execution still at locks_applied (try-out pending)."""

from __future__ import annotations

from app.services.ptw_ops.rules._util import plural
from app.services.ptw_ops.types import ExceptionDraft, PermitView, Snapshot

RULE_KEY = "loto_not_verified"
SEVERITY = "high"


def flagged(p: PermitView) -> bool:
    return p.status in ("ISSUED", "ACTIVE") and (
        p.isolationsUnverified > 0 or p.lotoExecutionStatus == "locks_applied"
    )


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    out = []
    for p in snap.permits:
        if not flagged(p):
            continue
        if p.isolationsUnverified:
            title = f"{plural(p.isolationsUnverified, 'isolation point')} not verified"
        else:
            title = "LOTO applied but not verified (try-out pending)"
        out.append(ExceptionDraft(
            ruleKey=RULE_KEY, severity=SEVERITY, dedupeKey=f"{RULE_KEY}:{p.id}", plantId=p.plantId,
            areaId=p.areaId, permitId=p.id, ref=p.number, since=p.activatedAt or p.validFrom,
            ownerUserId=p.issuerId, title=title,
            detail="The issuer verifies every isolation (zero-energy try-out) before work starts.",
        ))
    return out
