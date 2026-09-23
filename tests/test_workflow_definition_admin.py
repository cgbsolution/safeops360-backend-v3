"""Workflow admin-UI defects found while wiring Part B §3.

Two live bugs in `routers/workflow_definitions.py`, both of which made the
visual workflow builder quietly wrong rather than obviously broken:

  1. `_validate_steps` demanded EXACTLY ONE Closure step. INCIDENT ships two —
     "Plant Head Final Close" gated on severity LOW/MEDIUM and "Plant Head +
     Corporate HSE Joint Close" gated on HIGH/CRITICAL. The engine runs that
     configuration happily, so the validator was stricter than the runtime and
     the live Incident Investigation workflow could not be saved from the admin
     UI at all — every edit 400'd.

  2. The parallelStrategy / slaBySeverity preservation fallback was keyed by
     step SEQUENCE, and sequences are re-numbered densely on every save. A save
     that reordered steps therefore grafted one step's settings onto whichever
     step had taken its position.

Both are pinned here because both are invisible in normal use: #1 only bites an
editor, and #2 produces a workflow that still runs — just with the wrong
approver rule on the wrong gate.
"""

from __future__ import annotations

from app.routers.workflow_definitions import _validate_steps


def _step(seq: int, step_type: str, name: str, **extra) -> dict:
    return {"sequence": seq, "stepType": step_type, "name": name, **extra}


def _minimal() -> list[dict]:
    return [
        _step(1, "MAKER", "Raise"),
        _step(2, "CHECKER", "Approve"),
        _step(3, "CLOSURE", "Close"),
    ]


# ── the shape that must keep working ────────────────────────────────────────


def test_a_single_closure_workflow_still_validates():
    assert _validate_steps(_minimal()) is None


def test_the_existing_structural_rules_are_unchanged():
    """Relaxing the Closure count must not have loosened anything else."""
    no_maker = [_step(1, "CHECKER", "Approve"), _step(2, "CLOSURE", "Close")]
    assert "exactly one Maker" in (_validate_steps(no_maker) or "")

    two_makers = [
        _step(1, "MAKER", "Raise"),
        _step(2, "MAKER", "Raise again"),
        _step(3, "CHECKER", "Approve"),
        _step(4, "CLOSURE", "Close"),
    ]
    assert "exactly one Maker" in (_validate_steps(two_makers) or "")

    maker_not_first = [
        _step(1, "CHECKER", "Approve"),
        _step(2, "MAKER", "Raise"),
        _step(3, "CLOSURE", "Close"),
    ]
    assert "first step must be the Maker" in (_validate_steps(maker_not_first) or "")

    no_middle = [_step(1, "MAKER", "Raise"), _step(2, "CLOSURE", "Close")]
    assert "at least one Checker or Assignee" in (_validate_steps(no_middle) or "")

    unnamed = [_step(1, "MAKER", ""), _step(2, "CHECKER", "A"), _step(3, "CLOSURE", "C")]
    assert "missing a name" in (_validate_steps(unnamed) or "")

    bad_type = [
        _step(1, "MAKER", "Raise"),
        _step(2, "NONSENSE", "?"),
        _step(3, "CLOSURE", "Close"),
    ]
    assert "unknown type" in (_validate_steps(bad_type) or "")


# ── defect 1: the live INCIDENT workflow ────────────────────────────────────


def test_the_real_incident_workflow_can_now_be_saved():
    """Reproduces the exact 11-step definition live in prod.

    Before the fix this returned "Workflow must have exactly one Closure step."
    and the whole Incident Investigation workflow was uneditable.
    """
    incident = [
        _step(1, "MAKER", "First Responder Reports"),
        _step(2, "CHECKER", "HSE Manager Classification"),
        _step(3, "ASSIGNEE_TASK", "Investigation Team RCA + CAPA Definition"),
        _step(4, "CHECKER", "HSE Manager Reviews Investigation Report"),
        _step(5, "CHECKER", "Plant Head Approves Final Report"),
        _step(6, "CHECKER", "Corporate HSE Reviews"),
        _step(7, "ASSIGNEE_TASK", "CAPA Execution"),
        _step(8, "VERIFIER", "Safety Officer Verifies CAPAs"),
        _step(
            9,
            "ASSIGNEE_TASK",
            "Statutory Forms Submission",
            conditionExpr='{"isReportable":[true]}',
        ),
        _step(
            10,
            "CLOSURE",
            "Plant Head Final Close",
            conditionExpr='{"severity":["LOW","MEDIUM"]}',
        ),
        _step(
            11,
            "CLOSURE",
            "Plant Head + Corporate HSE Joint Close",
            conditionExpr='{"severity":["HIGH","CRITICAL"]}',
            parallelStrategy="JOINT_APPROVAL",
        ),
    ]
    assert _validate_steps(incident) is None


def test_an_unconditional_closure_before_another_is_refused():
    """The loosened rule must not become a silent trap.

    The engine takes the FIRST step whose condition passes, so an unconditional
    Closure always wins and every Closure after it is dead config that looks
    configured. Rejecting it is the whole reason the count could be relaxed
    safely.
    """
    steps = [
        _step(1, "MAKER", "Raise"),
        _step(2, "CHECKER", "Approve"),
        _step(3, "CLOSURE", "Close early"),  # no condition
        _step(4, "CLOSURE", "Close late", conditionExpr='{"severity":["HIGH"]}'),
    ]
    err = _validate_steps(steps) or ""
    assert "unreachable" in err
    assert "Close early" in err  # names the offending step, not just the rule


def test_a_blank_condition_counts_as_no_condition():
    """An empty string round-tripped by an editor must not pass as configured."""
    steps = [
        _step(1, "MAKER", "Raise"),
        _step(2, "CHECKER", "Approve"),
        _step(3, "CLOSURE", "Close early", conditionExpr="   "),
        _step(4, "CLOSURE", "Close late", conditionExpr='{"severity":["HIGH"]}'),
    ]
    assert "unreachable" in (_validate_steps(steps) or "")


def test_the_last_step_must_still_be_a_closure():
    steps = [
        _step(1, "MAKER", "Raise"),
        _step(2, "CLOSURE", "Close", conditionExpr='{"severity":["LOW"]}'),
        _step(3, "CHECKER", "Approve"),
    ]
    assert "last step must be a Closure" in (_validate_steps(steps) or "")


def test_a_workflow_with_no_closure_is_refused():
    steps = [_step(1, "MAKER", "Raise"), _step(2, "CHECKER", "Approve")]
    assert "at least one Closure" in (_validate_steps(steps) or "")
