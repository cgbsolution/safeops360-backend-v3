"""impairment_overdue [critical] — planned_restore_at passed, system not restored."""

from __future__ import annotations

from datetime import datetime

from app.services.ptw_ops.rules._util import fmt_dur, mins
from app.services.ptw_ops.types import ExceptionDraft, ImpairmentView, Snapshot

RULE_KEY = "impairment_overdue"
SEVERITY = "critical"


def is_overdue(imp: ImpairmentView, now: datetime) -> bool:
    return imp.is_in_force(now) and imp.plannedRestoreAt is not None and imp.plannedRestoreAt < now


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    out = []
    for imp in snap.impairments:
        if not is_overdue(imp, snap.now):
            continue
        out.append(ExceptionDraft(
            ruleKey=RULE_KEY, severity=SEVERITY, dedupeKey=f"{RULE_KEY}:{imp.id}", plantId=imp.plantId,
            areaId=imp.areaId, impairmentId=imp.id, ref=imp.code, since=imp.plannedRestoreAt,
            ownerUserId=snap.areaOwners.get(imp.areaId or "") or imp.approvedById or imp.createdById,
            title=f"{imp.systemName} still impaired {fmt_dur(mins(imp.plannedRestoreAt, snap.now))} past restore time",
            detail="Restore the system, or re-approve the impairment with a new restore time and precautions.",
        ))
    return out
