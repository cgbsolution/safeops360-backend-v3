"""PTW closure state-gating — the build spec's §6 checklist as tests.

The defect: "Declare Work Completed" was gated on STATUS alone, so a permit
that expired BEFORE the receiver ever acknowledged it could be pushed through
the same closure path as one where the crew genuinely worked past its window.
Both produced a "Work Completed" record; only one of them was true.

These tests pin the discriminator (acknowledgement, i.e. `activatedAt`) and
the mutual exclusivity of the two closure doors. No DB and no HTTP — the gate
is pure over permit state on purpose, which is what makes it testable and what
lets the API and the UI share one verdict.

Run:  .venv/Scripts/python.exe -m pytest tests/test_ptw_closure_gate.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.permit import (
    Permit,
    PermitClosureType,
    PermitEvidenceAction,
    PermitExecutionState,
    PermitStatus,
)
from app.schemas.permit import WithdrawUnexecutedRequest
from app.services.ptw_closure_gate import (
    is_past_validity,
    unexecuted_withdrawal_blocker,
    was_acknowledged,
    work_completion_blocker,
)
from app.services.ptw_evidence import EVIDENCE_POLICY

NOW = datetime.now(timezone.utc)
PAST = NOW - timedelta(hours=6)
FUTURE = NOW + timedelta(hours=6)


def _permit(**kw) -> Permit:
    """A bare Permit instance — never added to a session. Only the columns the
    gate reads are set."""
    p = Permit()
    p.id = kw.pop("id", "permit-1")
    p.status = kw.pop("status", PermitStatus.ISSUED)
    p.validTo = kw.pop("validTo", FUTURE)
    p.activatedAt = kw.pop("activatedAt", None)
    p.workCompletedAt = kw.pop("workCompletedAt", None)
    p.returnedAt = kw.pop("returnedAt", None)
    p.flraRequired = kw.pop("flraRequired", False)
    p.currentActiveFlraId = kw.pop("currentActiveFlraId", None)
    p.executionState = kw.pop("executionState", None)
    p.closureType = kw.pop("closureType", None)
    assert not kw, f"unexpected kwargs: {kw}"
    return p


class _FakeDb:
    """The blocker functions only touch the DB for the FLRA lookup and the
    LOTO gate; both are exercised via monkeypatch, so nothing here is called
    in the paths under test."""

    async def execute(self, *a, **k):  # pragma: no cover - guard
        raise AssertionError("gate hit the database on a path that should not")


@pytest.fixture(autouse=True)
def _no_loto_block(monkeypatch):
    """Default: no lockout outstanding. The LOTO-lock gate has its own tests
    and is explicitly out of scope for this fix — it is only re-asserted here
    as a regression check (see test_loto_lock_still_blocks_withdrawal)."""

    async def _clear(db, permit_id):
        return None

    monkeypatch.setattr("app.services.loto.permit_closure_blocker", _clear)


# ═══════════════════════════════════════════════════════════════════════════
#  The discriminator — acknowledgement, not status
# ═══════════════════════════════════════════════════════════════════════════


def test_activated_permit_is_acknowledged():
    assert was_acknowledged(_permit(status=PermitStatus.ACTIVE, activatedAt=PAST))


def test_expired_permit_with_activation_is_still_acknowledged():
    # Scenario A: work ran past the validity window. Expiry does not erase the
    # fact that the receiver accepted the permit.
    assert was_acknowledged(_permit(status=PermitStatus.EXPIRED, activatedAt=PAST))


def test_expired_permit_never_activated_is_not_acknowledged():
    # Scenario B: the reported defect (PTW-NW-02236).
    assert not was_acknowledged(_permit(status=PermitStatus.EXPIRED, validTo=PAST))


def test_legacy_active_row_without_activated_at_counts_as_acknowledged():
    # Rows written before the closed-loop rebuild stamped activatedAt. Treating
    # these as unacknowledged would deadlock a real in-flight job.
    assert was_acknowledged(_permit(status=PermitStatus.ACTIVE))
    assert was_acknowledged(_permit(status=PermitStatus.SUSPENDED))
    assert was_acknowledged(_permit(status=PermitStatus.WORK_COMPLETED))


def test_past_validity_detected_before_the_expiry_scan_catches_up():
    # The status flip is a scheduled scan; validTo is the truth in between.
    assert is_past_validity(_permit(status=PermitStatus.ISSUED, validTo=PAST))
    assert not is_past_validity(_permit(status=PermitStatus.ISSUED, validTo=FUTURE))


# ═══════════════════════════════════════════════════════════════════════════
#  Door A — Declare Work Completed
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_completion_blocked_when_never_acknowledged():
    """The core fix. An expired-unacknowledged permit cannot be declared
    complete, and the refusal names the alternative."""
    p = _permit(status=PermitStatus.EXPIRED, validTo=PAST)
    blocker = await work_completion_blocker(_FakeDb(), p)
    assert blocker is not None
    assert "unexecuted" in blocker.lower()


@pytest.mark.asyncio
async def test_completion_allowed_on_expired_permit_that_was_worked():
    """Regression check on the legitimate case — an acknowledged permit that
    ran past its window must still close, or the closure task deadlocks in the
    issuer's inbox (the bug the previous fix removed)."""
    p = _permit(status=PermitStatus.EXPIRED, validTo=PAST, activatedAt=PAST)
    assert await work_completion_blocker(_FakeDb(), p) is None


@pytest.mark.asyncio
async def test_completion_allowed_on_active_permit():
    p = _permit(status=PermitStatus.ACTIVE, activatedAt=PAST)
    assert await work_completion_blocker(_FakeDb(), p) is None


@pytest.mark.asyncio
async def test_completion_blocked_before_acceptance_while_still_valid():
    """Not expired, not acknowledged — the permit is simply pending. Neither
    closure door is open."""
    p = _permit(status=PermitStatus.ISSUED, validTo=FUTURE)
    blocker = await work_completion_blocker(_FakeDb(), p)
    assert blocker is not None
    assert "not acknowledged" in blocker.lower()


@pytest.mark.asyncio
async def test_completion_blocked_when_already_declared():
    p = _permit(status=PermitStatus.WORK_COMPLETED, activatedAt=PAST, workCompletedAt=NOW)
    blocker = await work_completion_blocker(_FakeDb(), p)
    assert blocker is not None
    assert "already" in blocker.lower()


@pytest.mark.asyncio
async def test_completion_blocked_when_required_flra_is_missing(monkeypatch):
    async def _no_flra(db, permit_id):
        return False

    monkeypatch.setattr(
        "app.services.ptw_closure_gate._has_completed_flra", _no_flra
    )
    p = _permit(status=PermitStatus.ACTIVE, activatedAt=PAST, flraRequired=True)
    blocker = await work_completion_blocker(_FakeDb(), p)
    assert blocker is not None
    assert "flra" in blocker.lower()


@pytest.mark.asyncio
async def test_completion_allowed_when_required_flra_is_pinned():
    # currentActiveFlraId short-circuits the lookup — the FLRA is already
    # pinned on the permit at acceptance.
    p = _permit(
        status=PermitStatus.ACTIVE,
        activatedAt=PAST,
        flraRequired=True,
        currentActiveFlraId="flra-1",
    )
    assert await work_completion_blocker(_FakeDb(), p) is None


# ═══════════════════════════════════════════════════════════════════════════
#  Door B — Withdraw / Close Unexecuted Permit
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_withdrawal_allowed_on_expired_unacknowledged_permit():
    p = _permit(status=PermitStatus.EXPIRED, validTo=PAST)
    assert await unexecuted_withdrawal_blocker(_FakeDb(), p) is None


@pytest.mark.asyncio
async def test_withdrawal_allowed_before_the_expiry_scan_flips_the_status():
    p = _permit(status=PermitStatus.ISSUED, validTo=PAST)
    assert await unexecuted_withdrawal_blocker(_FakeDb(), p) is None


@pytest.mark.asyncio
async def test_withdrawal_blocked_when_the_permit_was_acknowledged():
    """The mirror of the core fix: a permit that WAS worked cannot be quietly
    recorded as never executed."""
    p = _permit(status=PermitStatus.EXPIRED, validTo=PAST, activatedAt=PAST)
    blocker = await unexecuted_withdrawal_blocker(_FakeDb(), p)
    assert blocker is not None
    assert "acknowledged" in blocker.lower()


@pytest.mark.asyncio
async def test_withdrawal_blocked_while_still_inside_validity_window():
    p = _permit(status=PermitStatus.ISSUED, validTo=FUTURE)
    blocker = await unexecuted_withdrawal_blocker(_FakeDb(), p)
    assert blocker is not None
    assert "validity window" in blocker.lower()


@pytest.mark.asyncio
async def test_withdrawal_blocked_when_work_was_already_declared():
    p = _permit(status=PermitStatus.EXPIRED, validTo=PAST, workCompletedAt=NOW)
    blocker = await unexecuted_withdrawal_blocker(_FakeDb(), p)
    assert blocker is not None


@pytest.mark.asyncio
async def test_loto_lock_still_blocks_withdrawal(monkeypatch):
    """Regression check on the gate that already worked: locks outstanding
    block BOTH closure doors, not just the completion one. Equipment cannot be
    handed back while locks are on it, however the permit is being closed."""

    async def _blocked(db, permit_id):
        return "Lockout LOTO-EX-2026-0005 linked to this permit is still open."

    monkeypatch.setattr("app.services.loto.permit_closure_blocker", _blocked)
    p = _permit(status=PermitStatus.EXPIRED, validTo=PAST)
    blocker = await unexecuted_withdrawal_blocker(_FakeDb(), p)
    assert blocker is not None
    assert "lockout" in blocker.lower()


# ═══════════════════════════════════════════════════════════════════════════
#  The two doors are mutually exclusive — never both, never neither-by-accident
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permit",
    [
        _permit(status=PermitStatus.EXPIRED, validTo=PAST),                      # never acknowledged
        _permit(status=PermitStatus.EXPIRED, validTo=PAST, activatedAt=PAST),    # worked past window
        _permit(status=PermitStatus.ACTIVE, activatedAt=PAST),                   # in progress
        _permit(status=PermitStatus.ISSUED, validTo=FUTURE),                     # pending
        _permit(status=PermitStatus.ISSUED, validTo=PAST),                       # expired, scan behind
    ],
)
async def test_at_most_one_door_is_ever_open(permit):
    wc = await work_completion_blocker(_FakeDb(), permit)
    wd = await unexecuted_withdrawal_blocker(_FakeDb(), permit)
    assert not (wc is None and wd is None), "both closure doors open on one permit"


# ═══════════════════════════════════════════════════════════════════════════
#  Data model + evidence policy
# ═══════════════════════════════════════════════════════════════════════════


def test_execution_state_covers_the_spec_values():
    values = {s.value for s in PermitExecutionState}
    assert values == {
        "NOT_STARTED",
        "IN_PROGRESS",
        "COMPLETED",
        "EXPIRED_UNEXECUTED",
        "WITHDRAWN",
    }


def test_closure_type_distinguishes_the_two_paths():
    values = {c.value for c in PermitClosureType}
    assert {"WORK_COMPLETED", "UNEXECUTED_CLOSURE"} <= values


def test_withdraw_request_requires_a_reason_code():
    with pytest.raises(Exception):
        WithdrawUnexecutedRequest()


def test_withdrawal_evidence_needs_a_signature_but_not_a_photo():
    # No work happened, so there is no worksite condition to photograph —
    # demanding one would only invite a meaningless picture. Accountability
    # (who closed this, from where) still applies.
    rule = EVIDENCE_POLICY[PermitEvidenceAction.WITHDRAW_UNEXECUTED]
    assert rule.signature and rule.gps and not rule.photo


def test_withdrawal_evidence_action_is_not_a_completion():
    assert (
        PermitEvidenceAction.WITHDRAW_UNEXECUTED
        != PermitEvidenceAction.WORK_COMPLETED_DECLARE
    )
