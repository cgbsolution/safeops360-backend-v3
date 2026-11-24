"""permit_expired_open [critical] — past valid_to and not closed."""

from __future__ import annotations

from app.services.ptw_ops.derived import expired_open
from app.services.ptw_ops.rules._util import fmt_dur, mins, tl
from app.services.ptw_ops.types import ExceptionDraft, Snapshot

RULE_KEY = "permit_expired_open"
SEVERITY = "critical"


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    out = []
    for p in snap.permits:
        if not expired_open(p, snap.now):
            continue
        out.append(ExceptionDraft(
            ruleKey=RULE_KEY, severity=SEVERITY, dedupeKey=f"{RULE_KEY}:{p.id}", plantId=p.plantId,
            areaId=p.areaId, permitId=p.id, ref=p.number, since=p.validTo,
            ownerUserId=p.issuerId or p.receiverId,
            title=f"{tl(p.type)} permit expired {fmt_dur(mins(p.validTo, snap.now))} ago and is not closed",
            detail="Close it out (work completed or withdrawn) or raise a new permit before work continues.",
        ))
    return out
