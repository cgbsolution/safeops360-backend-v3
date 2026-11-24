"""Lifecycle pipeline step keys.

Permit.status alone is too coarse for the pipeline: every approval step sits
under status SUBMITTED, and the workflow's current step name says which one.
The step key folds (status, workflow step name) into one value. The same
mapping exists in SQL as ptw_step_key() (prisma/apply-ptw-control-center-ddl.ts),
which the PermitEvent triggers use, so the two MUST stay in lockstep — the
unit test pins both tables of values.
"""

from __future__ import annotations

from dataclasses import dataclass

DRAFT = "DRAFT"
ISSUER_REVIEW = "ISSUER_REVIEW"
SAFETY_REVIEW = "SAFETY_REVIEW"
PLANT_HEAD = "PLANT_HEAD_APPROVAL"
APPROVAL_OTHER = "APPROVAL"
AWAITING_ACCEPT = "AWAITING_ACCEPT"
ACTIVE = "ACTIVE"
SUSPENDED = "SUSPENDED"
CLOSURE_PENDING = "CLOSURE_PENDING"
CLOSED = "CLOSED"
EXPIRED = "EXPIRED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"

TERMINAL_STEPS = frozenset({CLOSED, CANCELLED, REJECTED})
APPROVAL_STEPS = (ISSUER_REVIEW, SAFETY_REVIEW, PLANT_HEAD, APPROVAL_OTHER)


@dataclass(frozen=True)
class PipelineStep:
    key: str
    label: str
    default_sla_min: int | None
    tint: str  # neutral | active | closure — the UI's base tint before breach


# Order = left-to-right on the pipeline row. APPROVAL_OTHER only shows when a
# custom workflow has an approval step none of the three names match.
PIPELINE: tuple[PipelineStep, ...] = (
    PipelineStep(ISSUER_REVIEW, "Issuer review", 120, "neutral"),
    PipelineStep(SAFETY_REVIEW, "Safety review", 120, "neutral"),
    PipelineStep(PLANT_HEAD, "Plant head", 240, "neutral"),
    PipelineStep(APPROVAL_OTHER, "Other approval", 240, "neutral"),
    PipelineStep(AWAITING_ACCEPT, "Issued · awaiting accept", 60, "neutral"),
    PipelineStep(ACTIVE, "Active", None, "active"),
    PipelineStep(SUSPENDED, "Suspended", 120, "neutral"),
    PipelineStep(CLOSURE_PENDING, "Closure pending", 240, "closure"),
)
PIPELINE_BY_KEY = {p.key: p for p in PIPELINE}

# Register-tab filter for each step (clicking a pipeline tile).
STEP_STATUSES: dict[str, tuple[str, ...]] = {
    ISSUER_REVIEW: ("SUBMITTED",),
    SAFETY_REVIEW: ("SUBMITTED",),
    PLANT_HEAD: ("SUBMITTED",),
    APPROVAL_OTHER: ("SUBMITTED",),
    AWAITING_ACCEPT: ("APPROVED", "ISSUED"),
    ACTIVE: ("ACTIVE",),
    SUSPENDED: ("SUSPENDED",),
    CLOSURE_PENDING: ("WORK_COMPLETED", "HANDBACK_INSPECTION"),
}


def approval_substep(step_name: str | None) -> str:
    n = (step_name or "").lower()
    if "issuer" in n:
        return ISSUER_REVIEW
    if "safety" in n:
        return SAFETY_REVIEW
    if "plant head" in n or "plant_head" in n:
        return PLANT_HEAD
    return APPROVAL_OTHER


def step_key(status: str | None, workflow_step_name: str | None = None) -> str:
    """Pipeline step for a permit. Mirrors SQL ptw_step_key(status, step_name)."""
    s = (status or "").upper()
    if s in ("SUBMITTED", "ISSUER_APPROVED", "SAFETY_APPROVED", "PLANT_HEAD_APPROVED"):
        return approval_substep(workflow_step_name)
    if s in ("APPROVED", "ISSUED"):
        return AWAITING_ACCEPT
    if s in ("WORK_COMPLETED", "HANDBACK_INSPECTION"):
        return CLOSURE_PENDING
    if s in (ACTIVE, SUSPENDED, CLOSED, EXPIRED, CANCELLED, REJECTED, DRAFT):
        return s
    return s or DRAFT
