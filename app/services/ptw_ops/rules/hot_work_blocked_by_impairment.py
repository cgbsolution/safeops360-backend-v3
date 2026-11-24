"""hot_work_blocked_by_impairment [high] — a permit is running in a zone where an
impairment now blocks its type (e.g. hot work under an impaired sprinkler), and
the controls the block_new rule requires of running permits are not all in place."""

from __future__ import annotations

from app.services.ptw_ops import impairment_check as ic
from app.services.ptw_ops.derived import RUNNING_STATUSES, is_open
from app.services.ptw_ops.rules._util import tl
from app.services.ptw_ops.types import ExceptionDraft, Snapshot

RULE_KEY = "hot_work_blocked_by_impairment"
SEVERITY = "high"
EFFECT = "block_new"


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    return running_permit_drafts(snap, effect=EFFECT, rule_key=RULE_KEY, severity=SEVERITY)


def running_permit_drafts(snap: Snapshot, *, effect: str, rule_key: str, severity: str) -> list[ExceptionDraft]:
    out = []
    for p in snap.permits:
        if p.status not in RUNNING_STATUSES or not is_open(p):
            continue
        decision = ic.check(
            permit_type=p.type, area_id=p.areaId, plant_id=p.plantId, impairments=snap.impairments,
            rules=snap.impairmentRules, now=snap.now, satisfied=p.satisfiedControls, running=True,
        )
        for hit in decision.hits:
            if hit.rule.effect != effect:
                continue
            missing = [c for c in hit.rule.requiredControls if c not in p.satisfiedControls]
            if not missing:
                continue
            imp = hit.impairment
            needs = " + ".join(ic.control_label(c).lower() for c in missing)
            if effect == "block_new":
                title = f"{tl(p.type)} running in impaired zone ({imp.code}) — needs {needs}"
                detail = "New work of this type is blocked here. Running permits continue only with these controls."
            else:
                title = f"{tl(p.type)} under impairment {imp.code} without {needs}"
                detail = "Record the controls on the permit (Impairment controls panel) or suspend the work."
            out.append(ExceptionDraft(
                ruleKey=rule_key, severity=severity, dedupeKey=f"{rule_key}:{p.id}:{imp.id}",
                plantId=p.plantId, areaId=p.areaId, permitId=p.id, impairmentId=imp.id,
                ref=f"{p.number} · {imp.code}", since=max(imp.startedAt, p.activatedAt or p.validFrom),
                ownerUserId=p.issuerId, title=title, detail=detail,
            ))
    return out
