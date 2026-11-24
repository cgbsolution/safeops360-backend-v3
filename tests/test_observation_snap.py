"""Snap & Report + live dashboard helpers: offline unit tests (no DB, no AI)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.observation import DB_OBSERVATION_CATEGORIES
from app.services import observation_snap as snap

ROWS = [
    {"axis": "ACT", "categoryCode": "PPE", "categoryLabel": "PPE", "subCategoryCode": "PPE_NOT_WORN", "subCategoryLabel": "PPE not worn", "stopReferenceCode": "STOP-3"},
    {"axis": "ACT", "categoryCode": "PPE", "categoryLabel": "PPE", "subCategoryCode": "PPE_WRONG_TYPE", "subCategoryLabel": "Wrong PPE", "stopReferenceCode": "STOP-3"},
    {"axis": "ACT", "categoryCode": "PROCEDURES", "categoryLabel": "Procedures", "subCategoryCode": "PR_NOT_FOLLOWED", "subCategoryLabel": "Not followed", "stopReferenceCode": "STOP-5"},
    {"axis": "CONDITION", "categoryCode": "HOUSEKEEPING", "categoryLabel": "Housekeeping", "subCategoryCode": "HK_CLUTTER_DEBRIS", "subCategoryLabel": "Clutter", "stopReferenceCode": "STOP-6"},
    {"axis": "CONDITION", "categoryCode": "TOOLS_EQUIPMENT", "categoryLabel": "Tools", "subCategoryCode": "TE_UNSAFE_CONDITION", "subCategoryLabel": "Unsafe", "stopReferenceCode": "STOP-4"},
]


def test_valid_ai_output_passes_through():
    s = snap.clamp_ai_output({
        "observationType": "UNSAFE_ACT", "categoryCode": "PPE", "subCategoryCode": "PPE_NOT_WORN",
        "severity": "HIGH", "title": "No helmet", "description": "Worker near the ladle without a helmet.",
        "likelyHazard": "Head injury", "immediateAction": "Stop work and issue a helmet.", "confidence": 0.9,
    }, ROWS)
    assert (s["observationType"], s["categoryCode"], s["subCategoryCode"], s["severity"]) == ("UNSAFE_ACT", "PPE", "PPE_NOT_WORN", "HIGH")
    assert s["category"] == "PPE"  # legacy dual-write value
    assert s["clamped"] == []


def test_invented_codes_are_clamped_onto_the_real_taxonomy():
    s = snap.clamp_ai_output({
        "observationType": "DANGER", "categoryCode": "LASERS", "subCategoryCode": "LASER_EYE",
        "severity": "CATASTROPHIC", "confidence": "high",
    }, ROWS)
    assert s["observationType"] in snap.OBSERVATION_TYPES
    assert s["severity"] in snap.SEVERITIES
    pairs = {(r["categoryCode"], r["subCategoryCode"]) for r in ROWS if r["axis"] == "CONDITION"}
    assert (s["categoryCode"], s["subCategoryCode"]) in pairs
    assert {"observationType", "severity", "categoryCode"} <= set(s["clamped"])
    assert s["confidence"] == 0.0


def test_sub_category_from_wrong_axis_is_not_accepted():
    # a CONDITION sub-category offered on an ACT observation
    s = snap.clamp_ai_output({"observationType": "UNSAFE_ACT", "categoryCode": "HOUSEKEEPING",
                              "subCategoryCode": "HK_CLUTTER_DEBRIS", "severity": "LOW"}, ROWS)
    act_pairs = {(r["categoryCode"], r["subCategoryCode"]) for r in ROWS if r["axis"] == "ACT"}
    assert (s["categoryCode"], s["subCategoryCode"]) in act_pairs
    assert "categoryCode" in s["clamped"]


def test_known_category_with_bad_sub_keeps_the_category():
    cat, sub, clamped = snap.clamp_stop_pair(ROWS, "ACT", "ppe", "PPE_ON_BACKWARDS")
    assert (cat, sub, clamped) == ("PPE", "PPE_NOT_WORN", True)


def test_legacy_category_hint_used_when_codes_missing():
    cat, sub, _ = snap.clamp_stop_pair(ROWS, "CONDITION", None, None, legacy_category="HOUSEKEEPING")
    assert (cat, sub) == ("HOUSEKEEPING", "HK_CLUTTER_DEBRIS")


def test_axis_without_rows_returns_none():
    assert snap.clamp_stop_pair([], "ACT", "PPE", "PPE_NOT_WORN") == (None, None, True)


def test_safe_observation_gets_storable_hazard_category():
    s = snap.clamp_ai_output({"observationType": "SAFE_ACT", "hazardCategory": "work at height", "severity": "LOW"}, ROWS)
    assert s["category"] == "WORK_AT_HEIGHT"
    assert s["categoryCode"] is None and s["subCategoryCode"] is None
    s = snap.clamp_ai_output({"observationType": "SAFE_CONDITION", "hazardCategory": "ERGONOMICS"}, ROWS)
    assert s["category"] == "OTHERS"  # Python-only label, not storable
    assert set(snap.storable_categories()) <= DB_OBSERVATION_CATEGORIES


def test_garbage_input_is_safe():
    s = snap.clamp_ai_output(None, ROWS)
    assert s["observationType"] == snap.DEFAULT_TYPE and s["severity"] == snap.DEFAULT_SEVERITY
    s = snap.clamp_ai_output({"title": {"x": 1}, "description": ["a"], "observationType": None}, ROWS)
    assert s["title"] == "" and s["description"] == ""


def test_text_fields_are_trimmed_and_capped():
    s = snap.clamp_ai_output({"title": "  a   b  " + "x" * 300}, ROWS)
    assert s["title"].startswith("a b") and len(s["title"]) == 120


def test_compose_description():
    d = snap.compose_description("No helmet", "Worker at the ladle without a helmet.", "Head injury")
    assert d == "No helmet. Worker at the ladle without a helmet. Likely hazard: Head injury."
    # headline already in the body is not repeated
    assert snap.compose_description("helmet", "Worker without a helmet.", "") == "Worker without a helmet."


def test_vocabulary_groups_by_axis():
    v = snap.vocabulary_for_prompt(ROWS)
    assert [c["code"] for c in v["stop"]["ACT"]] == ["PPE", "PROCEDURES"]
    assert len(v["stop"]["ACT"][0]["subs"]) == 2
    assert v["severities"] == list(snap.SEVERITIES)


# ── location ─────────────────────────────────────────────────────────────────
def test_nearest_area_from_geotagged_history():
    pts = [("a-sms", 29.1500, 75.7200), ("a-sms", 29.1502, 75.7202), ("a-crm", 29.1600, 75.7300)]
    c = snap.area_centroids(pts)
    assert c["a-sms"][2] == 2
    hit = snap.nearest_area(c, 29.1503, 75.7201)
    assert hit and hit[0] == "a-sms" and hit[1] < 50
    assert snap.nearest_area(c, 28.0, 75.0) is None  # far away → no guess


def test_haversine_known_distance():
    # ~111 km per degree of latitude
    assert 110_000 < snap.haversine_m(29.0, 75.0, 30.0, 75.0) < 112_000


def test_gps_parsing_rejects_junk():
    assert snap._gps_of({"gps": {"lat": "29.1", "lng": "75.7"}}) == (29.1, 75.7)
    assert snap._gps_of({"gps": {"lat": 0, "lng": 0}}) is None
    assert snap._gps_of({"gps": {"lat": 120, "lng": 1}}) is None
    assert snap._gps_of("x") is None


# ── dashboard periods ────────────────────────────────────────────────────────
def test_day_bounds_use_plant_local_time():
    # 2026-11-23 20:00 UTC is already 24 Nov 01:30 in IST
    b = snap.day_bounds(datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc), 330)
    assert b["today"] == datetime(2026, 11, 23, 18, 30, tzinfo=timezone.utc)
    assert b["week"] == datetime(2026, 11, 22, 18, 30, tzinfo=timezone.utc)  # Monday 23 Nov IST
    assert b["month"] == datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)


def test_pct():
    assert snap.pct(1, 3) == 33.3
    assert snap.pct(0, 0) is None


# ── Copilot-style reading: clamp, severity policy, analysis card, refine ─────
_RICH = {
    "photoFindings": "A worker crouches at floor level cutting steel with an angle grinder. Sparks fly towards loose cables.",
    "observationType": "UNSAFE_CONDITION", "categoryCode": "TOOLS_EQUIPMENT", "subCategoryCode": "TE_UNSAFE_CONDITION",
    "severity": "MEDIUM", "severityReason": "Cables lie in the spark path next to an HV box.",
    "fatalPotential": "possible", "fatalPotentialQuestion": "Are any of the trailing cables damaged?",
    "title": "Grinding sparks on loose cables", "description": "Angle grinding at floor level with sparks landing on loose power cables.",
    "consequence": "Cable insulation could burn through and cause a shock or fire.",
    "correctiveActions": [
        {"action": "Wear a face shield", "controlLevel": "PPE"},
        {"action": "Fit a spark screen", "controlLevel": "engineering"},
        {"action": "", "controlLevel": "PPE"},
        "Route cables through protectors",
    ],
    "responsibleFunction": "Mechanical", "questions": ["Is the disc guard fitted?", {"x": 1}], "confidence": 0.8,
}


def test_rich_fields_are_clamped_and_actions_sorted_by_hierarchy():
    s = snap.clamp_ai_output(_RICH, ROWS)
    assert s["fatalPotential"] == "POSSIBLE"
    assert [a["controlLevel"] for a in s["correctiveActions"]] == ["ENGINEERING", "ADMINISTRATIVE", "PPE"]
    assert s["questions"] == ["Is the disc guard fitted?"]
    assert s["consequence"].startswith("Cable insulation")
    assert snap.clamp_ai_output({"fatalPotential": "maybe"}, ROWS)["fatalPotential"] == "NO"


def test_severity_policy_matches_copilot():
    # higher of matrix and plant practice; never below the matrix on our own
    assert snap.choose_severity(matrix="MEDIUM", plant_practice="LOW", ai="LOW", fatal="NO") == "MEDIUM"
    assert snap.choose_severity(matrix="LOW", plant_practice="HIGH", ai="LOW", fatal="NO") == "HIGH"
    assert snap.choose_severity(matrix=None, plant_practice=None, ai="HIGH", fatal="NO") == "HIGH"
    assert snap.choose_severity(matrix="LOW", plant_practice="LOW", ai="LOW", fatal="YES") == "CRITICAL"
    # refine: the reporter's instruction wins (the form then asks for a reason)
    assert snap.choose_severity(matrix="HIGH", plant_practice=None, ai="LOW", fatal="NO", directed=True) == "LOW"


_KB = {
    "available": True, "matches": 25,
    "category": {"code": "C3", "label": "Electrical Hazard", "share": 0.45},
    "alternatives": [{"label": "Defective Equipment", "share": 0.22}],
    "risk": {"safeopsSeverity": "HIGH", "mix": {"Low": 0.5, "Medium": 0.45, "FatalPotential": 0.05}},
    "fatalPotentialSignals": ["hv"], "responsibleFunction": "Operation",
    "correctiveActions": [{"action": "Fit a spark screen", "controlLevel": "ENGINEERING", "from": "UC-1"},
                          {"action": "Dress the cables", "controlLevel": "ADMINISTRATIVE", "from": "UC-2"}],
}


def test_analysis_card_grounded_in_reference_register():
    s = snap.clamp_ai_output(_RICH, ROWS)
    out = snap.build_analysis(s, kb=_KB, matrix="MEDIUM", matrix_rationale="rule", labels={"categoryLabel": "Tools"},
                              area_known=False)
    a = out["analysis"]
    assert out["severity"] == "HIGH" and out["needsReason"] and out["overrideReason"]
    assert "25 similar observations" in a["classification"]["why"] and "Electrical Hazard (45%)" in a["classification"]["why"]
    assert any("Plant practice" in r for r in a["severity"]["reasons"])
    # photo actions first; reference actions de-duplicated and labelled
    assert [x["source"] for x in a["correctiveActions"]] == ["PHOTO", "PHOTO", "PHOTO", "REFERENCE"]
    assert a["correctiveActions"][-1]["ref"] == "UC-2"
    assert a["questions"][0].startswith("Where exactly")
    assert a["fatalPotential"] == {"level": "POSSIBLE", "question": "Are any of the trailing cables damaged?"}
    assert a["responsibleFunction"] == "Mechanical" and a["closureTarget"] == "about 7 days"


def test_analysis_without_grounding_or_matrix():
    s = snap.clamp_ai_output({**_RICH, "fatalPotential": "YES", "severity": "HIGH"}, ROWS)
    out = snap.build_analysis(s, kb=None, matrix=None, matrix_rationale=None, labels={}, area_known=True)
    assert out["severity"] == "CRITICAL" and not out["needsReason"]
    assert out["analysis"]["closureTarget"] == "same day"
    assert out["analysis"]["classification"]["why"] is None
    assert not any(q.startswith("Where exactly") for q in out["analysis"]["questions"])


def test_user_text_carries_note_and_refine_context():
    t = snap.build_user_text(ROWS, note="  CRD AP2,   near HV panel ")
    assert "Reporter's note: CRD AP2, near HV panel" in t and "REFINE MODE" not in t
    r = snap.build_user_text(ROWS, lang="hi", refine={
        "instruction": "cable insulation is damaged", "current": {"severity": "MEDIUM"},
        "history": [{"role": "user", "text": "make it shorter"}, "junk"],
    })
    assert "REFINE MODE" in r and "instruction: cable insulation is damaged" in r
    assert '"severity": "MEDIUM"' in r and "make it shorter" in r and "language code: hi" in r
