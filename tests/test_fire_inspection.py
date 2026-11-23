"""QR-code fire inspections: offline unit tests of the pure rules (no DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import fire_inspection as fi
from app.services.fire_safety import compute_status

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)

ITEMS = [
    {"key": "pin_seal", "text": "Safety pin and tamper seal intact", "critical": True, "guidance": None},
    {"key": "gauge", "text": "Pressure gauge needle in the green zone", "critical": True, "guidance": None},
    {"key": "signage", "text": "Signage visible", "critical": False, "guidance": None},
]


def ans(**kw):
    return {k: {"response": v[0], "remarks": v[1] if len(v) > 1 else None} for k, v in kw.items()}


# ── templates ────────────────────────────────────────────────────────────────
def test_normalise_items_derives_keys_and_dedupes():
    out = fi.normalise_items([
        {"text": "Pressure gauge in green", "critical": True},
        {"text": "Pressure gauge in green"},
        {"key": "pin", "text": "Pin intact"},
    ])
    assert [i["key"] for i in out] == ["pressure_gauge_in_green", "pressure_gauge_in_green_2", "pin"]
    assert out[0]["critical"] is True and out[1]["critical"] is False
    assert out[2]["guidance"] is None


def test_normalise_items_rejects_empty_and_short():
    with pytest.raises(ValueError):
        fi.normalise_items([])
    with pytest.raises(ValueError):
        fi.normalise_items([{"text": "ok"}])
    with pytest.raises(ValueError):
        fi.normalise_items([{"text": "x" * 301}])
    with pytest.raises(ValueError):
        fi.normalise_items([{"text": f"item {n}"} for n in range(fi.MAX_ITEMS + 1)])


def test_slug_key_never_empty():
    assert fi.slug_key("!!!") == "item"
    assert fi.slug_key("Hose & nozzle OK") == "hose_nozzle_ok"


# ── responses ────────────────────────────────────────────────────────────────
def test_all_pass_is_valid():
    r = ans(pin_seal=("PASS",), gauge=("PASS",), signage=("PASS",))
    assert fi.validate_responses(ITEMS, r) == []
    s = fi.summarise(ITEMS, r)
    assert s["result"] == "PASS" and s["passCount"] == 3 and s["criticalFailCount"] == 0


def test_missing_and_unknown_items_refused():
    errs = fi.validate_responses(ITEMS, ans(pin_seal=("PASS",), bogus=("PASS",)))
    assert any("Unknown" in e for e in errs)
    assert sum("not answered" in e for e in errs) == 2


def test_fail_needs_remarks_and_critical_na_needs_reason():
    errs = fi.validate_responses(ITEMS, ans(pin_seal=("FAIL",), gauge=("NA",), signage=("NA",)))
    assert len(errs) == 2  # non-critical NA without remarks is fine
    assert fi.validate_responses(ITEMS, ans(pin_seal=("FAIL", "Seal broken"), gauge=("NA", "CO2 type, no gauge"), signage=("NA",))) == []


def test_bad_response_value():
    assert fi.validate_responses(ITEMS, ans(pin_seal=("MAYBE",), gauge=("PASS",), signage=("PASS",)))


def test_summarise_counts_critical_fails_only():
    s = fi.summarise(ITEMS, ans(pin_seal=("FAIL", "Seal missing"), gauge=("PASS",), signage=("FAIL", "Faded")))
    assert s["result"] == "FAIL" and s["failCount"] == 2 and s["criticalFailCount"] == 1
    assert s["criticalFails"][0]["key"] == "pin_seal" and s["criticalFails"][0]["remarks"] == "Seal missing"


def test_non_critical_fail_still_fails_the_inspection_but_not_the_asset():
    s = fi.summarise(ITEMS, ans(pin_seal=("PASS",), gauge=("PASS",), signage=("FAIL", "Faded")))
    assert s["result"] == "FAIL" and s["criticalFailCount"] == 0
    assert fi.status_after_inspection("ACTIVE", critical_fail_count=0, mark_out_of_service=False) is None


# ── status ───────────────────────────────────────────────────────────────────
def test_critical_fail_marks_defective_or_out_of_service():
    assert fi.status_after_inspection("ACTIVE", critical_fail_count=1, mark_out_of_service=False) == "DEFECTIVE"
    assert fi.status_after_inspection("OVERDUE", critical_fail_count=2, mark_out_of_service=True) == "OUT_OF_SERVICE"


def test_passing_inspection_clears_defective_but_not_out_of_service():
    assert fi.status_after_inspection("DEFECTIVE", critical_fail_count=0, mark_out_of_service=False) is None
    assert fi.status_after_inspection("OUT_OF_SERVICE", critical_fail_count=0, mark_out_of_service=False) == "OUT_OF_SERVICE"
    assert fi.status_after_inspection("OUT_OF_SERVICE", critical_fail_count=1, mark_out_of_service=False) == "OUT_OF_SERVICE"


def test_status_events():
    assert fi.status_event("ACTIVE", "DEFECTIVE") == "MARKED_DEFECTIVE"
    assert fi.status_event("DEFECTIVE", "ACTIVE") == "DEFECT_CLEARED"
    assert fi.status_event("OVERDUE", "OUT_OF_SERVICE") == "OUT_OF_SERVICE"
    assert fi.status_event("OVERDUE", "ACTIVE") is None
    assert fi.status_event("ACTIVE", "ACTIVE") is None


def test_defective_is_sticky_in_compute_status():
    eq = SimpleNamespace(status="DEFECTIVE", nextInspectionDueDate=NOW + timedelta(days=90))
    assert compute_status(eq, NOW) == "DEFECTIVE"
    eq.status = "ACTIVE"
    assert compute_status(eq, NOW) == "ACTIVE"
    eq.nextInspectionDueDate = NOW - timedelta(days=1)
    assert compute_status(eq, NOW) == "OVERDUE"


def test_monthly_asset_is_not_due_right_after_inspection():
    from app.services.fire_safety import due_soon_days

    assert due_soon_days(30) == 7 and due_soon_days(7) == 1 and due_soon_days(365) == 30 and due_soon_days(None) == 30
    eq = SimpleNamespace(status="ACTIVE", inspectionFrequencyDays=30, nextInspectionDueDate=fi.next_due(NOW, 30))
    assert compute_status(eq, NOW) == "ACTIVE"
    assert compute_status(eq, NOW + timedelta(days=24)) == "DUE_INSPECTION"


def test_next_due_uses_frequency_and_handles_naive():
    assert fi.next_due(NOW, 30) == NOW + timedelta(days=30)
    assert fi.next_due(NOW.replace(tzinfo=None), None) == NOW + timedelta(days=30)
    assert fi.next_due(NOW, 0) == NOW + timedelta(days=30)  # 0 = unset, not "due immediately"
    assert fi.next_due(NOW, 7) == NOW + timedelta(days=7)


def test_inspection_blockers():
    assert fi.inspection_blockers(status="ACTIVE", is_active=True, is_deleted=False) == []
    assert fi.inspection_blockers(status="OUT_OF_SERVICE", is_active=True, is_deleted=False) == []
    assert fi.inspection_blockers(status="DECOMMISSIONED", is_active=True, is_deleted=False)
    assert fi.inspection_blockers(status="ACTIVE", is_active=False, is_deleted=False)


# ── return to service ────────────────────────────────────────────────────────
def test_return_to_service_needs_passing_inspection_after_going_out():
    since = NOW - timedelta(days=2)
    assert fi.return_to_service_blockers(status="ACTIVE", since=None, last_inspection=None)
    assert fi.return_to_service_blockers(status="OUT_OF_SERVICE", since=since, last_inspection=None)
    # inspected before it went out of service: not good enough
    assert fi.return_to_service_blockers(status="OUT_OF_SERVICE", since=since, last_inspection=(since - timedelta(hours=1), 0))
    # inspected after, but a critical item still fails
    assert fi.return_to_service_blockers(status="DEFECTIVE", since=since, last_inspection=(NOW, 1))
    assert fi.return_to_service_blockers(status="DEFECTIVE", since=since, last_inspection=(NOW, 0)) == []
    # legacy out-of-service with no event: the latest inspection must pass
    assert fi.return_to_service_blockers(status="OUT_OF_SERVICE", since=None, last_inspection=(NOW, 0)) == []


# ── photos, due list, CAPA text ──────────────────────────────────────────────
def test_photo_path_must_belong_to_the_asset():
    assert fi.photo_path_ok(None, "eq1")
    assert fi.photo_path_ok("evidence/fire_inspection/eq1/item_photo/ab12-p.jpg", "eq1")
    assert not fi.photo_path_ok("evidence/fire_inspection/eq2/item_photo/ab12-p.jpg", "eq1")
    assert not fi.photo_path_ok("incidents/x/photo.jpg", "eq1")
    assert not fi.photo_path_ok("evidence/fire_inspection/eq1/../eq2/p.jpg", "eq1")


def test_due_bucket():
    assert fi.due_bucket("ACTIVE", None, NOW) == "NEVER"
    assert fi.due_bucket("OVERDUE", NOW - timedelta(days=1), NOW) == "OVERDUE"
    assert fi.due_bucket("DUE_INSPECTION", NOW + timedelta(days=5), NOW) == "DUE_SOON"
    assert fi.due_bucket("ACTIVE", NOW + timedelta(days=60), NOW) == "OK"
    assert fi.due_bucket("OUT_OF_SERVICE", NOW - timedelta(days=9), NOW) == "NOT_IN_SERVICE"


def test_capa_problem_text_lists_failed_items():
    txt = fi.capa_problem_text(equipment_code="FE-JSLH-0007", equipment_type="FIRE_EXTINGUISHER", location="CRM-2 bay 4",
                               inspection_no="FINSP-2026-00001",
                               critical_fails=[{"key": "gauge", "text": "Gauge in green", "remarks": "Needle in red"}])
    assert "FE-JSLH-0007" in txt and "Gauge in green: Needle in red" in txt and "fire extinguisher" in txt


def test_vocabularies():
    for v in (fi.EQUIPMENT_TYPES, fi.EQUIPMENT_STATUSES, fi.RESPONSES):
        assert len(v) == len(set(v))
    assert set(fi.DEFAULT_FREQUENCY_DAYS) == set(fi.EQUIPMENT_TYPES)
