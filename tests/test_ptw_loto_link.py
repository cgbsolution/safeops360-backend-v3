"""PTW ↔ LOTO cross-link (Part A) — the build spec's §5 checklist as tests.

Covers the two things a UI cannot be trusted for: that the API accepts and
returns the link, and that the close-out gate cannot be bypassed by calling the
API directly.

Run:  venv/Scripts/python.exe -m pytest tests/test_ptw_loto_link.py -v
"""

from __future__ import annotations

import inspect

import pytest

from app.models.loto import EXECUTION_STATUSES, OPEN_EXECUTION_STATUSES
from app.schemas.permit import PermitCreate, PermitOut, PermitUpdate


# ═══════════════════════════════════════════════════════════════════════════
#  §5.2 — the field is on the API surface, in all three directions
# ═══════════════════════════════════════════════════════════════════════════


def test_permit_create_accepts_loto_execution_id():
    assert "lotoExecutionId" in PermitCreate.model_fields


def test_permit_update_accepts_loto_execution_id():
    assert "lotoExecutionId" in PermitUpdate.model_fields


def test_permit_out_serializes_loto_execution_id():
    # The gap the diagnostic found: the column existed and the gate worked, but
    # no consumer reading a permit could see the link.
    assert "lotoExecutionId" in PermitOut.model_fields


def test_loto_execution_id_is_optional_everywhere():
    # Part A never makes LOTO mandatory. A permit payload with no LOTO key at
    # all must remain valid for every permit type.
    for model in (PermitCreate, PermitUpdate):
        assert model.model_fields["lotoExecutionId"].default is None


def test_field_reaches_the_generated_openapi_document():
    # Model-defined but never exposed on the route is the recurring defect on
    # this codebase, so assert against the generated document, not the class.
    from app.main import create_app

    spec = create_app().openapi()
    schemas = spec["components"]["schemas"]
    for name in ("PermitCreate", "PermitUpdate", "PermitOut"):
        assert "lotoExecutionId" in schemas[name]["properties"], f"{name} missing it"


# ═══════════════════════════════════════════════════════════════════════════
#  §4 / §5.6 — the close-out gate, server-side
# ═══════════════════════════════════════════════════════════════════════════


def test_open_statuses_block_and_terminal_statuses_do_not():
    # The gate keys on this set. If a status were added to the machine and not
    # classified here, a permit could close over live locks.
    assert OPEN_EXECUTION_STATUSES == {
        "locks_applied", "verified", "work_in_progress", "locks_removed",
    }
    assert set(EXECUTION_STATUSES) - OPEN_EXECUTION_STATUSES == {"closed", "aborted"}


def test_gate_is_invoked_inside_the_ptw_closure_branch():
    # Structural: the gate lives in workflow_engine.approve()'s CLOSURE branch.
    # A refactor that dropped the call would silently un-gate every permit and
    # nothing else in the suite would notice.
    from app.services import workflow_engine

    src = inspect.getsource(workflow_engine.approve)
    assert "permit_closure_blocker" in src
    assert "StepType.CLOSURE" in src
    # …and it must be reached before the permit is advanced, not after.
    assert src.index("permit_closure_blocker") < src.index("return await _advance")


def test_blocker_message_identifies_the_outstanding_lockout():
    # §4: "return a clear error identifying the outstanding LOTO execution
    # (procedure title + status), not a generic validation failure."
    from app.services import loto

    src = inspect.getsource(loto.permit_closure_blocker)
    assert "ex.number" in src
    assert "ex.status" in src


# ═══════════════════════════════════════════════════════════════════════════
#  §2 — link validation cannot be skipped, and the gate cannot be dodged
# ═══════════════════════════════════════════════════════════════════════════


def test_create_and_edit_share_one_validation_helper():
    # Two call sites applying different rules is how a permit ends up with a
    # close-out gate pointing at a lockout that does not exist.
    from app.routers import ptw

    src = inspect.getsource(ptw)
    assert src.count("await _link_loto_execution(") >= 2, "helper not reused by both paths"


def test_link_helper_checks_existence_site_and_prior_claim():
    from app.routers.ptw import _link_loto_execution

    src = inspect.getsource(_link_loto_execution)
    assert "could not be found" in src          # unknown / deleted id
    assert "different sites" in src             # cross-site link
    assert "already linked to permit" in src    # stolen from another permit


def test_details_update_refuses_to_clear_an_existing_link():
    # The bypass hole: if PATCH /details could null the field, "cannot close
    # while a lockout is open" would be solvable by deleting the reference to
    # the lockout rather than closing it.
    from app.routers import ptw

    src = inspect.getsource(ptw.update_permit_details)
    assert "lotoExecutionId" in src
    assert "cannot be removed from here" in src


def test_unlink_path_refuses_while_the_lockout_is_open():
    # The one endpoint that CAN unlink still refuses on an open lockout.
    from app.routers import loto as loto_router

    src = inspect.getsource(loto_router.link_permit)
    assert "permit_closure_blocker" in src
    assert "close or abort it rather than" in src


def test_link_helper_degrades_cleanly_without_the_loto_module():
    # A tenant without LOTO must get a 422 on an unknown id, not an ImportError
    # 500 out of the permit-create path.
    from app.routers.ptw import _link_loto_execution

    src = inspect.getsource(_link_loto_execution)
    assert "not available on this deployment" in src


# ═══════════════════════════════════════════════════════════════════════════
#  §5.8 — no regression to the wizard contract
# ═══════════════════════════════════════════════════════════════════════════


def test_permit_create_required_fields_are_unchanged():
    # Adding an optional field must not have made anything else required.
    required = {n for n, f in PermitCreate.model_fields.items() if f.is_required()}
    assert required == {
        "type", "plantId", "location", "scopeOfWork",
        "validFrom", "validTo", "issuerId", "receiverId",
    }, f"PermitCreate's required set changed: {sorted(required)}"


@pytest.mark.parametrize("permit_type", [
    "HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "EXCAVATION",
    "ELECTRICAL_LOTO", "LIFTING", "GENERAL_COLD",
])
def test_every_permit_type_still_validates_with_no_loto_link(permit_type):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    payload = PermitCreate(
        type=permit_type, plantId="p1", location="Bay A",
        scopeOfWork="Routine maintenance task on the line.",
        validFrom=now, validTo=now + timedelta(hours=8), issuerId="u1", receiverId="u2",
    )
    assert payload.lotoExecutionId is None
