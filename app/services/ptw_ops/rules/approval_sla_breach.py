"""approval_sla_breach [medium] — dwell at the current step exceeds that step's SLA
(WorkflowStep.slaHours where the definition sets one, else the step default).
Covers every pipeline step with an SLA except Active (bounded by validity)."""

from __future__ import annotations

from datetime import datetime

from app.services.ptw_ops import steps
from app.services.ptw_ops.derived import is_open
from app.services.ptw_ops.rules._util import fmt_dur, mins
from app.services.ptw_ops.types import ExceptionDraft, PermitView, Snapshot

RULE_KEY = "approval_sla_breach"
SEVERITY = "medium"


def sla_for(snap: Snapshot, step: str, p: PermitView | None = None) -> int | None:
    """The permit's own workflow-step SLA when its current workflow step is the
    approval step it sits in (cold work and hot work carry different SLAs);
    else the plant's configured SLA for the step; else the step default."""
    if p is not None and p.stepSlaMin is not None and step in steps.APPROVAL_STEPS:
        return p.stepSlaMin
    if step in snap.stepSlaMin:
        return snap.stepSlaMin[step]
    spec = steps.PIPELINE_BY_KEY.get(step)
    return spec.default_sla_min if spec else None


def dwell_min(p: PermitView, now: datetime) -> int | None:
    entered = p.stepEnteredAt or p.createdAt
    return mins(entered, now) if entered else None


def breached(snap: Snapshot, p: PermitView) -> bool:
    sla = sla_for(snap, p.step, p)
    d = dwell_min(p, snap.now)
    return sla is not None and d is not None and d > sla


def evaluate(snap: Snapshot) -> list[ExceptionDraft]:
    out = []
    for p in snap.permits:
        if not is_open(p) or p.step not in steps.PIPELINE_BY_KEY or p.step == steps.ACTIVE:
            continue
        if not breached(snap, p):
            continue
        spec = steps.PIPELINE_BY_KEY[p.step]
        out.append(ExceptionDraft(
            ruleKey=RULE_KEY, severity=SEVERITY, dedupeKey=f"{RULE_KEY}:{p.id}:{p.step}", plantId=p.plantId,
            areaId=p.areaId, permitId=p.id, ref=p.number, since=p.stepEnteredAt or p.createdAt,
            ownerUserId=p.pendingAssigneeId or p.issuerId,
            title=(f"Waiting {fmt_dur(dwell_min(p, snap.now))} at {spec.label.lower()} "
                   f"(SLA {fmt_dur(sla_for(snap, p.step, p))})"),
            detail="Act on the pending task or reassign it.",
        ))
    return out
