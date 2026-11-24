"""competency_expiring [medium] — a crew member on a running permit holds a
competency card that expires within 24 h (or already has)."""

from __future__ import annotations

from datetime import timedelta

from app.services.ptw_ops.rules._util import plural
from app.services.ptw_ops.types import ExceptionDraft, Snapshot

RULE_KEY = "competency_expiring"
SEVERITY = "medium"
HORIZON = timedelta(days=1)


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    out = []
    for p in snap.permits:
        if p.status not in ("ISSUED", "ACTIVE", "SUSPENDED") or p.crewCertMinExpiry is None:
            continue
        if p.crewCertMinExpiry > snap.now + HORIZON:
            continue
        n = max(1, p.crewCertExpiringCount)
        lapsed = p.crewCertMinExpiry <= snap.now
        out.append(ExceptionDraft(
            ruleKey=RULE_KEY, severity=SEVERITY, dedupeKey=f"{RULE_KEY}:{p.id}", plantId=p.plantId,
            areaId=p.areaId, permitId=p.id, ref=p.number, since=p.crewCertMinExpiry - HORIZON,
            ownerUserId=p.receiverId or p.issuerId,
            title=f"{plural(n, 'crew competency card')} " + ("expired" if lapsed else "expiring within 24 h"),
            detail="Swap the crew member out or renew the card before the next shift.",
        ))
    return out
