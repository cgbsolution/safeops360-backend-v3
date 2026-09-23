"""PTW closure gate — which close-out door is a permit allowed to leave by?

A permit can reach a terminal state two operationally distinct ways:

  A. Work actually happened.  Receiver acknowledged the permit (and completed
     the FLRA where one was required), the crew worked, and the job ended —
     possibly past the validity window.
     → "Declare Work Completed", POST /api/ptw/{id}/complete.

  B. Work never happened.  The permit was issued, the receiver never
     acknowledged it, and the validity window simply ran out.
     → "Withdraw / Close Unexecuted Permit", POST /api/ptw/{id}/withdraw.

Before this module, only door A existed and it was gated on STATUS alone, so
a permit that expired unacknowledged could be pushed through it — producing a
"Work Completed" record, an outcome, and a completion-shaped audit trail for
work nobody ever authorised to start. `status` cannot tell the two apart:
both sit at EXPIRED.

The discriminator is acknowledgement. `activatedAt` is stamped only by
POST /accept (and by the engine when the instance advances into CLOSURE),
and acceptance is what runs the activation gate — FLRA, crew validity, PPE,
isolations. So `activatedAt IS NOT NULL` means "the receiver stood at the
worksite and signed for this permit, past every activation blocker".

This module is deliberately shaped like `loto.permit_closure_blocker`: one
pure function returning the reason a door is shut, called BOTH by the API that
enforces it and by the endpoint the UI reads. The panel can therefore never
say "clear" about something the API is going to refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.flra import FLRA, FLRAStatus
from app.models.permit import Permit, PermitStatus

# Statuses at which a receiver may declare work completed, ASSUMING the permit
# was acknowledged. EXPIRED stays in the list on purpose: work that ran past
# the window still has to be closed out honestly (see the note in
# ptw_lifecycle.declare_work_completed).
DECLARABLE_STATUSES = {
    PermitStatus.ACTIVE,
    PermitStatus.SUSPENDED,
    PermitStatus.EXPIRED,
}

# Statuses from which an unacknowledged permit may be withdrawn as unexecuted.
# ISSUED/APPROVED are included only when the validity window has already
# passed — a live permit still inside its window is not "unexecuted", it is
# simply pending, and the existing /cancel path owns pulling it.
WITHDRAWABLE_STATUSES = {
    PermitStatus.EXPIRED,
    PermitStatus.ISSUED,
    PermitStatus.APPROVED,
}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def was_acknowledged(permit: Permit) -> bool:
    """True when the receiver accepted this permit at the worksite.

    `activatedAt` is the primary fact. The status fallback covers legacy rows
    written before the closed-loop rebuild stamped activatedAt — a permit
    sitting at ACTIVE/SUSPENDED/WORK_COMPLETED/HANDBACK_INSPECTION got there
    through acceptance by definition, so treating it as unacknowledged would
    deadlock a real in-flight job."""
    if permit.activatedAt is not None:
        return True
    return permit.status in (
        PermitStatus.ACTIVE,
        PermitStatus.SUSPENDED,
        PermitStatus.WORK_COMPLETED,
        PermitStatus.HANDBACK_INSPECTION,
    )


def is_past_validity(permit: Permit) -> bool:
    """EXPIRED by status, or validTo already behind us. The second half
    matters because the expiry flip is a scheduled scan — a permit can be
    functionally expired for hours before its status catches up."""
    if permit.status == PermitStatus.EXPIRED:
        return True
    valid_to = _aware(permit.validTo)
    return valid_to is not None and valid_to <= datetime.now(timezone.utc)


async def _has_completed_flra(db: AsyncSession, permit_id: str) -> bool:
    row = (
        await db.execute(
            select(FLRA.id)
            .where(FLRA.permitId == permit_id)
            .where(FLRA.status == FLRAStatus.COMPLETED)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def work_completion_blocker(db: AsyncSession, permit: Permit) -> str | None:
    """Reason the Work Completed declaration is not available, or None.

    Ordered most-informative-first so the message a user sees names the real
    obstacle rather than the first technicality."""
    if permit.workCompletedAt is not None or permit.returnedAt is not None:
        return "Work has already been declared completed on this permit."

    if not was_acknowledged(permit):
        # The defect this module exists for.
        if is_past_validity(permit):
            return (
                "This permit expired before the receiver acknowledged it — work "
                "was never authorised to start, so it cannot be closed as "
                "\"work completed\". Close it as an unexecuted permit instead."
            )
        return (
            "The receiver has not acknowledged this permit yet. Work cannot be "
            "declared complete before it has been accepted at the worksite."
        )

    # Belt-and-braces on the FLRA. The activation gate already enforces this at
    # acceptance, so an acknowledged permit has passed it — this only catches a
    # row acknowledged through some path that bypassed the gate. Scoped to
    # never-activated rows would be dead code; scoped to ALL rows would risk
    # deadlocking a legacy in-flight permit, so it is checked only where the
    # acknowledgement fact itself came from `activatedAt`.
    if permit.flraRequired and permit.activatedAt is not None:
        if permit.currentActiveFlraId is None and not await _has_completed_flra(
            db, permit.id
        ):
            return (
                "This permit requires an FLRA and no completed FLRA is on "
                "record. Complete and sign the FLRA before declaring work "
                "completed."
            )

    if permit.status not in DECLARABLE_STATUSES:
        return f"Cannot declare work completed on a {permit.status.value} permit."

    return None


async def unexecuted_withdrawal_blocker(
    db: AsyncSession, permit: Permit
) -> str | None:
    """Reason the unexecuted-withdrawal path is not available, or None.

    Mirrors `work_completion_blocker` — the two are mutually exclusive by
    construction: exactly one of them is open on any given permit, or neither
    (a permit still inside its validity window that nobody has accepted yet is
    simply pending; `/cancel` owns pulling it)."""
    if permit.workCompletedAt is not None or permit.returnedAt is not None:
        return (
            "Work has already been declared completed on this permit — it "
            "cannot be closed as unexecuted."
        )
    if was_acknowledged(permit):
        return (
            "The receiver acknowledged this permit, so work was authorised to "
            "start. Close it through the Work Completed declaration and record "
            "the honest outcome."
        )
    if permit.status in (
        PermitStatus.CLOSED,
        PermitStatus.CANCELLED,
        PermitStatus.REJECTED,
    ):
        return f"This permit is already {permit.status.value.lower()}."
    if permit.status not in WITHDRAWABLE_STATUSES:
        return (
            f"A {permit.status.value} permit cannot be withdrawn as unexecuted."
        )
    if not is_past_validity(permit):
        return (
            "This permit is still inside its validity window — the receiver can "
            "still accept it. Use Cancel Permit to pull it early."
        )

    # An unacknowledged permit should never have live isolations against it, but
    # "shouldn't" is not "can't": if a lockout was applied anyway, the equipment
    # cannot be handed back while locks are on it, and that gate applies to this
    # door exactly as it applies to the completion door.
    from app.services.loto import permit_closure_blocker

    loto_blocker = await permit_closure_blocker(db, permit.id)
    if loto_blocker:
        return loto_blocker

    return None


@dataclass
class ClosureGate:
    """What the permit detail screen needs to render the close-out area."""

    acknowledged: bool
    pastValidity: bool
    executionState: str | None
    closureType: str | None
    canDeclareWorkCompleted: bool
    workCompletedBlocker: str | None
    canWithdrawUnexecuted: bool
    withdrawBlocker: str | None
    lotoBlocker: str | None

    def as_dict(self) -> dict:
        return {
            "acknowledged": self.acknowledged,
            "pastValidity": self.pastValidity,
            "executionState": self.executionState,
            "closureType": self.closureType,
            "canDeclareWorkCompleted": self.canDeclareWorkCompleted,
            "workCompletedBlocker": self.workCompletedBlocker,
            "canWithdrawUnexecuted": self.canWithdrawUnexecuted,
            "withdrawBlocker": self.withdrawBlocker,
            "lotoBlocker": self.lotoBlocker,
        }


async def closure_gate(db: AsyncSession, permit: Permit) -> ClosureGate:
    """Both doors evaluated together, for the UI. Same functions the API
    enforces with — so the panel cannot offer what the API will refuse."""
    from app.services.loto import permit_closure_blocker

    wc = await work_completion_blocker(db, permit)
    wd = await unexecuted_withdrawal_blocker(db, permit)
    loto = await permit_closure_blocker(db, permit.id)

    def _val(v) -> str | None:
        return v.value if v is not None and hasattr(v, "value") else v

    return ClosureGate(
        acknowledged=was_acknowledged(permit),
        pastValidity=is_past_validity(permit),
        executionState=_val(permit.executionState),
        closureType=_val(permit.closureType),
        canDeclareWorkCompleted=wc is None,
        workCompletedBlocker=wc,
        canWithdrawUnexecuted=wd is None,
        withdrawBlocker=wd,
        lotoBlocker=loto,
    )


__all__ = [
    "DECLARABLE_STATUSES",
    "WITHDRAWABLE_STATUSES",
    "ClosureGate",
    "closure_gate",
    "is_past_validity",
    "unexecuted_withdrawal_blocker",
    "was_acknowledged",
    "work_completion_blocker",
]
