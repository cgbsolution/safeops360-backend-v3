"""HIRA calibration round 2 (A-I): tolerability tiers, rubric rules, routine
controls, conditional second recommendation, inherent integrity and gating,
plant-index improvements, evidence scope. Pure (no DB)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from app.services.hira_calibration import review_v2 as rv
from app.services.hira_calibration import risk_engine as eng
from app.services.hira_calibration.config import DEFAULT_CONFIG, MatrixRule, deep_merge
from app.services.hira_calibration.loader import Event, area_in_scope, evidence_from_events

CFG = DEFAULT_CONFIG
C, H = eng.Control, eng.Hazard
STD = MatrixRule("STD_5X5", "FREQUENCY", False)
STRICT = MatrixRule("JSL_QSD24_GT", "PLANT_INDEX", True)
ALL = {
    "eliminationConsidered": True,
    "engineeringInPlaceOrPlanned": True,
    "statutoryCurrent": True,
    "reviewDateSet": True,
}


def tol(l, s, **kw):
    kw.setdefault("alarp", ALL)
    kw.setdefault("strict", False)
    return eng.tolerability(eng.rate(l, s, CFG), credible_s=kw.pop("credible_s", None), cfg=CFG, **kw)


def review(e, rule=STD, plant=None, ev=None):
    return rv.evaluate(e, cfg=CFG, rule=rule, evidence=ev or eng.Evidence(), plant=plant)


# ── A: tolerability tiers ──


@pytest.mark.parametrize("l,s", [(1, 1), (1, 3), (2, 2), (2, 3), (3, 2)])
def test_a_s3_or_less_never_runs_alarp_or_shows_tolerable(l, s):
    for verified in (False, True):
        t = tol(l, s, verified_control=verified)
        assert t["state"] != eng.TOLERABLE_ALARP and t["route"] != "ALARP_CHECKLIST"


def test_a_s3_moderate_needs_a_verified_non_baseline_control():
    assert tol(2, 3)["route"] == "CONFIRM_CONTROLS"
    assert tol(2, 3, verified_control=True)["state"] == eng.ACCEPTABLE
    assert tol(1, 3)["state"] == eng.ACCEPTABLE  # LOW: baseline-only is enough


def test_a_s4_moderate_runs_the_checklist_high_needs_controls_then_alarp():
    assert tol(1, 4)["state"] == eng.TOLERABLE_ALARP
    assert tol(1, 4, alarp={**ALL, "reviewDateSet": False})["route"] == "ALARP_CHECKLIST"
    assert tol(2, 5)["route"] == "CONTROLS"
    assert tol(2, 5, exhausted=True)["state"] == eng.TOLERABLE_ALARP  # HIGH: controls or the ALARP route
    assert tol(3, 5)["route"] == "CONTROLS"
    assert tol(3, 5, exhausted=True)["route"] == "OVERRIDE"  # CRITICAL: controls or override


def test_a_strict_plant_keeps_its_rule_and_ends_on_override():
    assert tol(1, 5, strict=True)["route"] == "OVERRIDE"
    assert tol(2, 3, strict=True, rating_credit=False)["route"] == "OVERRIDE"
    assert tol(2, 3, strict=True)["route"] == "CONTROLS"
    assert tol(2, 5, strict=True, exhausted=True)["route"] == "OVERRIDE"


def test_a_entry_state_is_the_worst_hazard_and_alarp_needs_an_s4_hazard():
    e = rv.EntryInput(
        "e",
        "#1",
        "Bench work",
        "ROUTINE",
        "DAILY",
        [
            H(
                "a",
                "Cut",
                code="MECH_SHARP_EDGE",
                category="mechanical",
                likelihood=2,
                severity=3,
                consequence="cut",
            ),
            H(
                "b",
                "Slip",
                code="PHYS_SLIP_TRIP",
                category="physical",
                likelihood=2,
                severity=2,
                consequence="bruise",
            ),
        ],
        2,
        3,
        None,
        controls=[
            C("c", "ADMINISTRATIVE", "Work instruction for the bench work", verification_method="audit")
        ],
        hazard_ratings_stored=True,
        review_date_set=True,
    )
    r = review(e)
    assert r["entry"]["state"] == "ACCEPTABLE"
    assert all(h["tolerability"]["state"] != eng.TOLERABLE_ALARP for h in r["hazards"])


# ── B: rubric rules (a)-(d) and conditional floors ──


@pytest.mark.parametrize(
    "family,text,lo,hi,rule",
    [
        # (a) heat: ambient / outdoor / cabin S3 (heat illness in these settings is recoverable); radiant
        # close-range or hot-surface heat S4 (severe burns credible)
        ("HEAT", "Heat stress during outdoor gate duty in summer", None, 3, "HEAT_AMBIENT_S3"),
        ("HEAT", "Radiant heat in the crane cabin above the ladle", None, 3, "HEAT_AMBIENT_S3"),
        ("HEAT", "Radiant heat at close range to the taphole", 4, 4, "HEAT_RADIANT_CLOSE_S4"),
        # (b) manual handling: repetitive strain / routine lifts S2 (recoverable); heavy loads S3 (LTI-level)
        ("MANUAL_HANDLING", "Repetitive feeding of blanks", None, 2, None),
        ("MANUAL_HANDLING", "Lifting a 30 kg roll", None, 3, "MANUAL_HEAVY_LOAD_S3"),
        # (c) height: ladder / step-stool below the threshold S3 (LTI-level); at / above it, edges,
        # openings, fragile surfaces S4-S5 (fatal falls credible)
        ("HEIGHT", "Step stool to reach the top shelf", None, 3, "HEIGHT_LADDER_BELOW_THRESHOLD_S3"),
        ("HEIGHT", "Ladder access to a 3 m rack", 4, None, "HEIGHT_AT_OR_ABOVE_THRESHOLD_OR_EDGE_S4"),
        (
            "HEIGHT",
            "Fall through a fragile roof sheet: fatal",
            5,
            None,
            "HEIGHT_AT_OR_ABOVE_THRESHOLD_OR_EDGE_S4",
        ),
        # (d) fire: routine grinding / cutting sparks S3 (local burns) unless flammable inventory is named
        ("FIRE_EXPLOSION", "Grinding sparks igniting combustibles", None, 3, "FIRE_ROUTINE_SPARKS_S3"),
        ("FIRE_EXPLOSION", "Cutting sparks near a diesel drum", 4, None, None),
        # conditional floor: a low-voltage office board does not get the live-panel floor
        (
            "ELECTRICAL",
            "Shock from a cracked extension board under the desk",
            None,
            None,
            "ELECTRICAL_FLOOR_NOT_TRIGGERED",
        ),
        ("ELECTRICAL", "Contact with a live 415 V busbar", 4, None, None),
    ],
)
def test_b_rubric_rules(family, text, lo, hi, rule):
    r = eng.severity_rule(family, text, CFG)
    assert (r["min"], r["max"], r["rule"]) == (lo, hi, rule)


def test_b_height_threshold_is_configurable_per_plant():
    cfg = deep_merge(CFG, {"severity": {"workAtHeightThresholdM": 4.0}})
    assert eng.severity_rule("HEIGHT", "Ladder access to a 3 m rack", cfg)["max"] == 3
    assert eng.severity_rule("HEIGHT", "Ladder access to a 3 m rack", CFG)["min"] == 4


def test_b_floors_never_raise_a_team_rating_in_review():
    h = H(
        "e",
        "Damaged extension board",
        code="ELEC-001",
        category="electrical",
        consequence="Electric shock from a cracked multi-plug extension board under the desk",
    )
    r = eng.calibrate_severity(3, family=h.family, text=h.text, cfg=CFG, adjust=True)
    assert r["value"] == 3 and r["finding"] is None  # LOW-01 is no longer raised to S4


# ── C: S<=3 with controls not captured ──


def test_c_low_severity_inherent_only_gets_routine_confirmation_not_recommendations():
    e = rv.EntryInput(
        "e",
        "#h",
        "Shop-floor housekeeping",
        "ROUTINE",
        "DAILY",
        [
            H(k, n, code=c, category=cat, likelihood=4, severity=3, consequence=q)
            for k, n, c, cat, q in [
                ("a", "Oily floor", "PHYS_SLIP_TRIP", "physical", "slip, wrist fracture"),
                ("b", "Sharp scrap ends", "MECH_SHARP_EDGE", "mechanical", "hand laceration"),
                ("c", "Scale dust", "ENV_DUST_AIRBORNE", "environmental", "eye irritation"),
                (
                    "d",
                    "Lifting full bins",
                    "MECH_HEAVY_LIFTING",
                    "mechanical",
                    "back strain lifting a 30 kg bin",
                ),
            ]
        ],
        4,
        3,
        None,
        controls=[C("p", "PPE", "Helmet, safety shoes, cotton hand gloves")],
        hazard_ratings_stored=True,
    )
    r = review(e)
    assert r["recommendations"] == []
    assert not rv.blocker_codes(r)
    f = next(f for f in r["findings"] if f["code"] == "CONFIRM_ROUTINE_CONTROLS")
    assert f["action"] == "CONFIRM_ROUTINE_CONTROLS" and r["existingControlProposals"]
    r2 = review(rv.apply_suggestions(e, r))
    assert r2["entry"]["state"] == "ACCEPTABLE" and len(r2["recommendations"]) <= 2


def test_c_low_severity_entries_get_at_most_two_recommendations():
    e = rv.EntryInput(
        "e",
        "#l",
        "Stores",
        "ROUTINE",
        "DAILY",
        [
            H(f"h{i}", n, code=c, category=cat, likelihood=4, severity=3, consequence=q)
            for i, (n, c, cat, q) in enumerate(
                [
                    ("Slip", "PHYS_SLIP_TRIP", "physical", "slip"),
                    ("Cut", "MECH_SHARP_EDGE", "mechanical", "cut"),
                    ("Dust", "ENV_DUST_AIRBORNE", "environmental", "dust"),
                    ("Noise", "NOISE_HIGH_EXPOSURE", "noise", "noise"),
                ]
            )
        ],
        4,
        3,
        None,
        controls=[C("c", "ADMINISTRATIVE", "Stores SOP")],
        hazard_ratings_stored=True,
    )
    r = review(e)
    assert len(r["recommendations"]) <= 2


# ── D: conditional second recommendation ──


def _fall_entry(**kw):
    return rv.EntryInput(
        "e",
        "#f",
        "Roof sheet replacement",
        "NON_ROUTINE",
        "OCCASIONAL",
        [
            H(
                "f",
                "Fall through a fragile sheet",
                code="HEIGHT_FRAGILE_ROOF",
                category="height",
                likelihood=4,
                severity=5,
                consequence="fatal fall through a fragile roof sheet",
            )
        ],
        4,
        5,
        None,
        controls=[C("c", "ADMINISTRATIVE", "Roof work permit")],
        hazard_ratings_stored=True,
        **kw,
    )


def test_d_second_recommendation_only_for_s4_hazards_left_open():
    ev = eng.Evidence(events_window=1, items=["near miss NM-1 (01 Jan 2026)"])
    r = review(_fall_entry(), ev=ev)
    assert len(r["recommendations"]) == 2
    assert r["recommendations"][0]["description"] != r["recommendations"][1]["description"]
    off = deep_merge(CFG, {"recommendations": {"secondRecWhenSevere": False}})
    r1 = rv.evaluate(_fall_entry(), cfg=off, rule=STD, evidence=ev)
    assert len(r1["recommendations"]) == 1


def test_d_no_second_recommendation_for_s3_or_when_the_first_resolves():
    e = rv.EntryInput(
        "e",
        "#c",
        "Knife work",
        "ROUTINE",
        "DAILY",
        [
            H(
                "k",
                "Cut",
                code="MECH_SHARP_EDGE",
                category="mechanical",
                likelihood=3,
                severity=3,
                consequence="cut",
            )
        ],
        3,
        3,
        None,
        controls=[C("c", "ADMINISTRATIVE", "Knife SOP")],
        hazard_ratings_stored=True,
    )
    assert len(review(e)["recommendations"]) <= 1


# ── E: parity is asserted over all fixtures in tests/test_hira_calibration_eval.py ──


def test_e_proposed_controls_on_a_stored_entry_are_candidates():
    e = rv.EntryInput(
        "e",
        "#1",
        "Bag feeding by crane",
        "ROUTINE",
        "DAILY",
        [
            H(
                "o",
                "Falling object",
                code="MECH_FALLING_OBJECT",
                category="mechanical",
                likelihood=3,
                severity=4,
                consequence="struck by falling alloy lumps",
            )
        ],
        3,
        4,
        None,
        controls=[C("a", "ADMINISTRATIVE", "Work instruction for bag feeding")],
        hazard_ratings_stored=True,
        candidate_controls=[
            {
                "hierarchy": "ENGINEERING",
                "description": "Overload limit switch on EOT crane",
                "costBand": "MEDIUM",
            }
        ],
    )
    r = review(e)
    assert any(x["description"] == "Overload limit switch on EOT crane" for x in r["recommendations"])


# ── F: inherent integrity ──


def test_f_inherent_has_no_control_credit():
    base = _fall_entry()
    more = copy.deepcopy(base)
    more.controls += [
        C("l", "ADMINISTRATIVE", "LOTO and permit to work", verification_method="audit"),
        C("g", "ENGINEERING", "Crawling boards and safety net", effectiveness="EFFECTIVE"),
    ]
    assert review(base)["hazards"][0]["inherent"] == review(more)["hazards"][0]["inherent"]
    assert review(base)["hazards"][0]["inherent"]["likelihood"] == 3  # L4 capped at L3 without evidence


# ── G: inherent display and gating ──


def test_g_no_action_rests_on_the_inherent_band():
    e = rv.EntryInput(
        "e",
        "#g",
        "Guarded press",
        "ROUTINE",
        "DAILY",
        [
            H(
                "p",
                "Press stroke",
                code="MECH-001",
                category="mechanical",
                likelihood=3,
                severity=4,
                consequence="hand crushed in the die area",
            )
        ],
        3,
        4,
        None,
        controls=[
            C("x", "ELIMINATION", "Automatic feeding: no hand in the die area", effectiveness="EFFECTIVE"),
            C("y", "ENGINEERING", "Light curtain", effectiveness="EFFECTIVE"),
        ],
        hazard_ratings_stored=True,
        review_date_set=True,
        elimination_considered=True,
    )
    r = review(e)
    assert r["entry"]["inherent"]["level"] in ("HIGH", "CRITICAL")
    assert r["entry"]["residual"]["level"] == "LOW"
    assert not rv.blocker_codes(r) and r["recommendations"] == []
    assert not any(x.get("raisesCapa") for x in r["recommendations"])
    assert r["entry"]["inherentLabel"] == "Inherent (before controls)"


# ── H: plant-index sheet ratings ──


def test_h_plant_index_improvements_are_tagged_and_never_gate():
    e = rv.EntryInput(
        "e6",
        "S.No 6",
        "Placement of bags at the bin top",
        "ROUTINE",
        "DAILY",
        [
            H(
                "h1",
                "Crane / lifting equipment failure - dropped load",
                code="MECH-008",
                category="lifting",
                consequence="Person may get injured",
            )
        ],
        2,
        3,
        (2, 3),
        rating_basis="WITH_CONTROLS",
        controls=[C("c1", "ADMINISTRATIVE", "Work instruction / SOP DOC No SM01/03/W")],
        review_date_set=True,
    )
    r = review(e, rule=STRICT)
    assert all(
        x["kind"] == "IMPROVEMENT_NO_RATING_CREDIT" and not x["raisesCapa"] for x in r["recommendations"]
    )
    assert all("L -" not in x["expectedEffect"] for x in r["recommendations"])  # no promised reduction
    assert not rv.blocker_codes(r)
    r2 = review(rv.apply_suggestions(e, r), rule=STRICT)
    assert r2["entry"]["state"] == "OVERRIDE" and not rv.blocker_codes(r2)


# ── I: evidence scope ──

NOW = datetime(2026, 11, 3, tzinfo=UTC)


def _ev(*events, scope="AREA", groups=None):
    return evidence_from_events(list(events), entry_area="A1", months=24, now=NOW, scope=scope, groups=groups)


def test_i_events_lift_only_their_own_family():
    ev = _ev(Event(NOW - timedelta(days=30), "near miss NM-1", "A1", "crane dropped a load near the bay"))
    assert ev.for_family("LIFTING").has_any
    assert not ev.for_family("HEIGHT").has_any and not ev.for_family("DUST").has_any
    l = eng.calibrate_likelihood(
        4, routine="ROUTINE", frequency="DAILY", evidence=ev.for_family("HEIGHT"), scale="FREQUENCY", cfg=CFG
    )
    assert l["value"] == 3


def test_i_events_in_another_area_or_outside_the_window_do_not_count():
    other = Event(NOW - timedelta(days=30), "near miss NM-2", "B7", "crane dropped a load")
    old = Event(NOW - timedelta(days=30 * 30), "incident INC-3", "A1", "crane dropped a load")
    assert not _ev(other).for_family("LIFTING").has_any
    assert not _ev(old).for_family("LIFTING").has_any
    assert _ev(other, scope="AREA_GROUP", groups={"melt shop": ["A1", "B7"]}).for_family("LIFTING").has_any
    assert _ev(other, scope="PLANT").for_family("LIFTING").has_any
    assert area_in_scope("B7", "A1", "AREA", {}) is False


def test_i_rationale_lists_event_ids_and_dates():
    ev = _ev(
        Event(NOW - timedelta(days=10), "near miss NM-2026-0310", "A1", "tipper reversed near the hopper")
    )
    l = eng.calibrate_likelihood(
        4, routine="ROUTINE", frequency="DAILY", evidence=ev.for_family("VEHICLE"), scale="FREQUENCY", cfg=CFG
    )
    assert l["value"] == 4 and "NM-2026-0310" in l["reason"] and "Oct 2026" in l["reason"]
