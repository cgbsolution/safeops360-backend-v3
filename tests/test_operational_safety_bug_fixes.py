"""Regression tests for the Operational Safety bug tracker fixes.

Offline unit tests (no DB), house style of test_rca.py / test_capture.py. Each
test names the tracker row it locks down so a future refactor that reintroduces
the defect fails here rather than in QA.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import capture as capture_svc
from app.services.rca import generate_rca_summary, is_empty_rca_data
from app.services.workflow_engine import _resolve_due_at


# ── II-3: "Save Cause Analysis" returned 500 ─────────────────────────────
# The RCA helpers are a port of `src/lib/rca/types.ts`, which reads every field
# through optional chaining. The port dropped that, so a canvas payload with a
# null field — or a list entry that was not an object — raised inside the
# request handler and surfaced as "Internal server error."

MALFORMED_RCA_PAYLOADS = [
    ("FIVE_WHY", {"problemStatement": None, "rootCause": None, "whys": [{"question": None, "answer": None}]}),
    ("FIVE_WHY", {"whys": [{"question": None, "answer": ""}]}),
    ("FIVE_WHY", {"whys": ["not a dict", None, 5]}),
    ("FIVE_WHY", "not a dict at all"),
    ("FIVE_WHY", None),
    ("FISHBONE", {"problemStatement": "p", "categories": None, "rootCauses": [{"text": "a"}, None]}),
    ("FISHBONE", {"categories": "oops", "rootCauses": "nope"}),
    ("FTA", {"rootNode": "oops"}),
    ("BOWTIE", {"threats": "x", "consequences": None}),
    ("TAPROOT", {"causalFactors": [None, {"description": None}]}),
    ("CAUSE_MAP", {"impacts": [None, 1, "Downtime"], "causeNodes": "x"}),
    ("NARRATIVE", {"summary": None, "factors": [None, {"description": "f1"}]}),
]


@pytest.mark.parametrize("method,data", MALFORMED_RCA_PAYLOADS)
def test_rca_helpers_never_raise_on_malformed_data(method, data):
    # A 500 here is the bug. Any return value is acceptable; an exception is not.
    is_empty_rca_data(method, data)
    generate_rca_summary(method, data)


def test_five_why_summary_survives_a_null_question():
    data = {
        "problemStatement": "Belt snapped",
        "whys": [{"question": "Why?", "answer": "Worn"}, {"question": None, "answer": ""}],
        "rootCause": "No preventive maintenance",
    }
    assert is_empty_rca_data("FIVE_WHY", data) is False
    assert generate_rca_summary("FIVE_WHY", data) == (
        "Belt snapped. Root cause: No preventive maintenance."
    )


def test_five_why_falls_back_to_the_last_answer_when_no_root_cause():
    data = {"problemStatement": "Leak", "whys": [{"answer": "Seal perished"}, {"answer": None}]}
    assert generate_rca_summary("FIVE_WHY", data) == "Leak. Root cause: Seal perished."


def test_root_cause_nodes_render_their_label_not_their_json():
    # Canvas nodes are objects; stringifying one put "{'text': ...}" on screen.
    summary = generate_rca_summary(
        "FISHBONE",
        {"problemStatement": "p", "categories": {"machine": [1, 2]}, "rootCauses": [{"text": "Worn belt"}, "No guard"]},
    )
    assert "Worn belt; No guard" in summary
    assert "{" not in summary


# ── SO-1 / NM-4: the Closure step showed "No SLA" ────────────────────────


def test_closure_task_inherits_the_records_target_closure_date():
    target = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    assert _resolve_due_at("CLOSURE", None, {"targetDate": target}) == target


def test_closure_target_date_wins_over_a_step_sla():
    # The record-level SLA matrix is the commitment the metadata panel shows;
    # a step-relative countdown must not silently replace it.
    target = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    assert _resolve_due_at("CLOSURE", 48, {"targetDate": target}) == target


def test_naive_target_dates_are_read_as_utc():
    naive = datetime(2026, 9, 19, 12, 0)
    resolved = _resolve_due_at("CLOSURE", None, {"targetDate": naive})
    assert resolved == naive.replace(tzinfo=timezone.utc)
    assert resolved.tzinfo is not None


def test_closure_without_a_target_date_falls_back_to_the_step_sla():
    before = datetime.now(timezone.utc) + timedelta(hours=48)
    resolved = _resolve_due_at("CLOSURE", 48, {"targetDate": None})
    assert resolved is not None
    assert abs((resolved - before).total_seconds()) < 5


def test_non_closure_steps_ignore_the_records_target_date():
    target = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    resolved = _resolve_due_at("CHECKER", 24, {"targetDate": target})
    assert resolved != target
    assert resolved is not None


def test_a_step_with_no_sla_at_all_still_yields_no_due_date():
    assert _resolve_due_at("CHECKER", None, {}) is None
    assert _resolve_due_at("CLOSURE", None, {}) is None


# ── FR-1: the field-report detail screen printed a raw area cuid ─────────


def _submission(**over):
    base = dict(
        id="sub1", number="FLD-2026-NW-0022", clientSubmissionId=None, type="observation",
        status="triaged", isAnonymous=False, reporter=None, reporterId=None,
        plantId="plant1", areaId="cmraiymfq002jv88c9itdapup", mapPinX=None, mapPinY=None,
        equipmentId=None, qrScanned=False, categoryL1Id=None, categoryL2Id=None,
        categorySnapshot=None, aiSuggested=None, aiConfidence=None,
        severitySelfReported="medium", description="d", voiceLangCode=None,
        transcriptOriginal=None, transcriptEnglish=None, transcriptionStatus=None,
        triagedById=None, triagedAt=None, hiraLikelihood=None, hiraSeverity=None,
        riskScore=None, riskLevel=None, triageNote=None, convertedEntityType=None,
        convertedEntityId=None, convertedAt=None, linkedRcaIds=None, linkedCapaIds=None,
        linkedPtwIds=None, tapCount=None, durationMs=None, wasOffline=False,
        appVersion=None, deviceLang=None, createdAtClient=None, createdAt=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_submission_payload_carries_the_resolved_area_name():
    out = capture_svc.submission_out(
        _submission(), refs={"cmraiymfq002jv88c9itdapup": "Boiler House"}
    )
    assert out["areaId"] == "cmraiymfq002jv88c9itdapup"
    assert out["areaName"] == "Boiler House"


def test_unresolved_ids_yield_a_null_name_rather_than_the_id():
    # The client falls back to its own placeholder; it must never be handed the
    # cuid to print.
    out = capture_svc.submission_out(_submission(), refs={})
    assert out["areaName"] is None
    assert out["equipmentName"] is None


def test_refs_is_optional_so_existing_callers_keep_working():
    out = capture_svc.submission_out(_submission())
    assert out["areaName"] is None
