"""simops_conflict [critical] — two live permits conflict by type pair and proximity."""

from __future__ import annotations

from app.services.ptw_ops import simops
from app.services.ptw_ops.rules._util import tl
from app.services.ptw_ops.types import ExceptionDraft, Snapshot

RULE_KEY = "simops_conflict"
SEVERITY = "critical"


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    by_id = {p.id: p for p in snap.permits}
    out = []
    for c in simops.detect(snap.permits, snap.simopsRules, snap.zones, snap.layout, snap.plantId):
        a, b = by_id[c.permitIds[0]], by_id[c.permitIds[1]]
        if c.sameArea:
            where = "in the same area"
        elif c.distanceM is not None:
            where = f"{round(c.distanceM)} m apart"
        else:
            where = "nearby"
        out.append(ExceptionDraft(
            ruleKey=RULE_KEY, severity=c.severity or SEVERITY, dedupeKey=f"{RULE_KEY}:{c.key}",
            plantId=snap.plantId, areaId=a.areaId, permitId=a.id, ref=f"{a.number} ↔ {b.number}",
            since=max(a.validFrom, b.validFrom), ownerUserId=a.issuerId,
            title=f"SIMOPS: {tl(a.type)} and {tl(b.type).lower()} {where}",
            detail="Stagger the work, or put a joint SIMOPS control in place and record it on both permits.",
        ))
    return out
