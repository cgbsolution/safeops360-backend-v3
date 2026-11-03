"""HIRA calibration v2: risk engine, recommendations and review (pure, no DB)."""

from __future__ import annotations

import copy

import pytest

from app.services.hira_calibration import recommend as rec
from app.services.hira_calibration import review_v2 as rv
from app.services.hira_calibration import risk_engine as eng
from app.services.hira_calibration.config import DEFAULT_CONFIG, MatrixRule, deep_merge, matrix_rule

CFG = DEFAULT_CONFIG
C = eng.Control
H = eng.Hazard
STD = MatrixRule("STD_5X5", "FREQUENCY", False)
STRICT = MatrixRule("JSL_QSD24_GT", "PLANT_INDEX", True)
NO_EVIDENCE = eng.Evidence()

# ── R1: one matrix, one set of thresholds ─────────────────────────────────

EXPECTED = {  # (L, S) -> level, every cell of the 5x5
    (1, 1): "LOW",
    (1, 2): "LOW",
    (1, 3): "LOW",
    (1, 4): "MODERATE",
    (1, 5): "MODERATE",
    (2, 1): "LOW",
    (2, 2): "MODERATE",
    (2, 3): "MODERATE",
    (2, 4): "HIGH",
    (2, 5): "HIGH",
    (3, 1): "LOW",
    (3, 2): "MODERATE",
    (3, 3): "HIGH",
    (3, 4): "HIGH",
    (3, 5): "CRITICAL",
    (4, 1): "MODERATE",
    (4, 2): "HIGH",
    (4, 3): "HIGH",
    (4, 4): "CRITICAL",
    (4, 5): "CRITICAL",
    (5, 1): "MODERATE",
    (5, 2): "HIGH",
    (5, 3): "CRITICAL",
    (5, 4): "CRITICAL",
    (5, 5): "CRITICAL",
}


@pytest.mark.parametrize("ls,level", sorted(EXPECTED.items()))
def test_every_cell(ls, level):
    l, s = ls
    r = eng.rate(l, s, CFG)
    assert r["score"] == l * s
    assert r["level"] == level


def test_l2_s2_is_moderate_everywhere():
    # The client study (plant rule) and the standard matrix now agree.
    assert eng.rate(2, 2, CFG)["level"] == "MODERATE"


def test_band_is_monotonic_in_score():
    levels = [eng.band(s, CFG) for s in range(1, 26)]
    assert [eng.level_index(x) for x in levels] == sorted(eng.level_index(x) for x in levels)


def test_out_of_range_rejected():
    with pytest.raises(ValueError):
        eng.rate(0, 3, CFG)
    with pytest.raises(ValueError):
        eng.rate(3, 6, CFG)


def test_plant_thresholds_from_config():
    cfg = deep_merge(CFG, {"thresholds": {"lowMax": 2}})
    assert eng.band(3, cfg) == "MODERATE"


def test_matrix_rule_detects_plant_index_and_strict_rule():
    class M:  # noqa: D401 - a matrix-like stub
        code = "JSL_QSD24_GT"
        alarpBands = {
            "LOW": "BROADLY_ACCEPTABLE",
            "MODERATE": "UNACCEPTABLE",
            "HIGH": "UNACCEPTABLE",
            "CRITICAL": "UNACCEPTABLE",
        }
        acceptableResidual = {"routine": "LOW"}

    r = matrix_rule(M, CFG, [f"Plant index {n}" for n in range(1, 6)])
    assert r.likelihood_scale == "PLANT_INDEX" and r.moderate_not_acceptable

    class S:
        code = "STD_5X5"
        alarpBands = {
            "LOW": "BROADLY_ACCEPTABLE",
            "MODERATE": "TOLERABLE",
            "HIGH": "TOLERABLE",
            "CRITICAL": "UNACCEPTABLE",
        }
        acceptableResidual = {"routine": "MODERATE"}

    r = matrix_rule(S, CFG, ["Rare", "Unlikely", "Possible", "Likely", "Almost Certain"])
    assert r.likelihood_scale == "FREQUENCY" and not r.moderate_not_acceptable


# ── R3: likelihood needs evidence ─────────────────────────────────────────


def lcal(p, ev=NO_EVIDENCE, scale="FREQUENCY", freq="DAILY"):
    return eng.calibrate_likelihood(p, routine="ROUTINE", frequency=freq, evidence=ev, scale=scale, cfg=CFG)


def test_l4_without_evidence_is_capped_at_l3():
    r = lcal(4)
    assert r["value"] == 3 and r["capped"] and "no recorded events" in r["reason"]


def test_l4_with_an_event_or_control_failure():
    assert lcal(4, eng.Evidence(events_window=1, items=["NM-1"]))["value"] == 4
    assert lcal(4, eng.Evidence(control_failures=["crane exam overdue"]))["value"] == 4


def test_l5_needs_two_events_in_12_months():
    assert lcal(5, eng.Evidence(events_window=1, events_12m=1))["value"] == 4
    assert lcal(5, eng.Evidence(events_window=2, events_12m=2))["value"] == 5
    assert lcal(5)["value"] == 3


def test_exposure_justifies_l3_not_l4():
    assert lcal(None, freq="DAILY")["value"] == 3
    assert lcal(None, freq="MONTHLY")["value"] == 2


def test_plant_index_scale_is_not_capped():
    r = lcal(4, scale="PLANT_INDEX")
    assert r["value"] == 4 and not r["capped"]


# ── R4: severity = credible worst case ────────────────────────────────────


def test_dropped_crane_load_rated_s3_is_understated():
    h = H("h", "Dropped load", code="MECH-008", category="lifting", consequence="Person may get injured")
    r = eng.calibrate_severity(3, family=h.family, text=h.text, cfg=CFG, adjust=False)
    assert r["finding"]["kind"] == "UNDERSTATED" and r["value"] == 3
    assert eng.calibrate_severity(3, family=h.family, text=h.text, cfg=CFG, adjust=True)["value"] == 4


def test_suspended_one_tonne_load_is_s5():
    h = H(
        "h",
        "Dropped load",
        code="MECH-008",
        category="lifting",
        consequence="crushed by a falling 1 t bag: fatal",
    )
    assert eng.credible_severity(h.family, h.text, CFG)[0] == 5


def test_cuts_dust_manual_handling_are_s1_to_s3():
    for code, cat in (
        ("MECH_SHARP_EDGE", "mechanical"),
        ("ENV_DUST_AIRBORNE", "environmental"),
        ("MECH_HEAVY_LIFTING", "mechanical"),
    ):
        h = H("h", "x", code=code, category=cat)
        r = eng.calibrate_severity(5, family=h.family, text=h.text, cfg=CFG, adjust=True)
        assert r["finding"]["kind"] == "OVERSTATED" and r["value"] == 3


def test_manual_lifting_is_not_crane_lifting():
    assert (
        H("h", "Heavy lifting", code="MECH_HEAVY_LIFTING", category="mechanical").family == "MANUAL_HANDLING"
    )


# ── R5: control credit ────────────────────────────────────────────────────

LIFT = H("lift", "Dropped load", code="MECH-008", category="lifting", consequence="fatal")
CUT = H("cut", "Sharp edge", code="MECH_SHARP_EDGE", category="mechanical", consequence="laceration")


def resid(l, s, controls, hazard=LIFT):
    return eng.residual_from_controls(l, s, controls, CFG, hazard=hazard)


def test_admin_and_ppe_never_lower_severity():
    r = resid(
        2,
        3,
        [
            C("a", "ADMINISTRATIVE", "Work instruction SOP-12"),
            C("b", "ADMINISTRATIVE", "Third party inspection of cranes"),
            C("p", "PPE", "Face shield, fire resistant jacket"),
        ],
    )
    assert r["severity"] == 3  # never the v1 "L1 x S2"


def test_admin_combined_is_one_step_and_provisional_without_verification():
    r = resid(
        3, 5, [C("a", "ADMINISTRATIVE", "Lift plan for crane lifts"), C("b", "ADMINISTRATIVE", "Crane SOP")]
    )
    assert (r["likelihood"], r["stepsL"]) == (2, 1) and r["provisional"]
    r = resid(
        3, 5, [C("a", "ADMINISTRATIVE", "Lift plan for crane lifts", verification_method="signed per lift")]
    )
    assert not r["provisional"]


def test_ppe_no_credit_for_s4_plus():
    r = resid(3, 4, [C("p", "PPE", "Aluminised jacket for the crane rigger")])
    assert r["likelihood"] == 3
    assert any(c["status"] == "NO_CREDIT" and "S4" in c["reason"] for c in r["credits"])


def test_ppe_one_step_for_s3_or_less():
    r = resid(3, 3, [C("p", "PPE", "Cut-resistant gloves", effectiveness="EFFECTIVE")], hazard=CUT)
    assert (r["likelihood"], r["severity"]) == (2, 3)


def test_baseline_ppe_gets_no_credit():
    r = resid(3, 3, [C("p", "PPE", "Helmet, safety shoes, hand gloves")], hazard=CUT)
    assert r["likelihood"] == 3
    assert r["credits"][0]["status"] == "BASELINE"
    assert eng.is_baseline("PPE", "PPE: helmet, safety shoes, hi-vis jacket", CFG)
    assert not eng.is_baseline("PPE", "helmet, safety shoes, face shield", CFG)


def test_two_evidenced_engineering_controls_give_two_steps():
    one = resid(3, 5, [C("e1", "ENGINEERING", "Crane overload limiter", effectiveness="EFFECTIVE")])
    two = resid(
        3,
        5,
        [
            C("e1", "ENGINEERING", "Crane overload limiter", effectiveness="EFFECTIVE"),
            C("e2", "ENGINEERING", "Hard barricaded exclusion zone under the crane", evidence_attached=True),
        ],
    )
    assert one["stepsL"] == 1 and two["stepsL"] == 2


def test_mitigating_engineering_needs_justification_for_severity():
    fall = H("fall", "Fall from height", code="WAH-003", category="height", consequence="fall: fatal")
    unjust = eng.residual_from_controls(
        3, 5, [C("m", "ENGINEERING", "Fall arrest harness lifeline")], CFG, hazard=fall
    )
    just = eng.residual_from_controls(
        3,
        5,
        [C("m", "ENGINEERING", "Fall arrest harness lifeline", effectiveness="EFFECTIVE")],
        CFG,
        hazard=fall,
    )
    assert unjust["severity"] == 5 and just["severity"] == 4


def test_total_reduction_capped_at_three_and_l_floor():
    r = resid(
        5,
        5,
        [
            C(
                "x",
                "ELIMINATION",
                "Bags fed by a closed pneumatic system: no crane lift",
                effectiveness="EFFECTIVE",
            ),
            C("e1", "ENGINEERING", "Crane overload limiter", effectiveness="EFFECTIVE"),
            C("a", "ADMINISTRATIVE", "Crane lift plan", verification_method="audit"),
        ],
    )
    assert r["stepsL"] + r["stepsS"] == 3
    r = resid(1, 5, [C("e1", "ENGINEERING", "Crane overload limiter", effectiveness="EFFECTIVE")])
    assert r["likelihood"] == 1


def test_controls_only_credit_the_hazards_they_address():
    dust = H("dust", "Airborne dust", code="ENV_DUST_AIRBORNE", category="environmental")
    r = resid(3, 3, [C("c", "ADMINISTRATIVE", "Third party inspection of cranes")], hazard=dust)
    assert r["likelihood"] == 3 and r["credits"][0]["status"] == "NOT_RELEVANT"
    r = resid(3, 3, [C("c", "ADMINISTRATIVE", "Work instruction for bag feeding")], hazard=dust)
    assert r["likelihood"] == 2  # a work instruction covers the whole activity


def test_planned_controls_earn_no_credit():
    r = resid(3, 5, [C("p", "ENGINEERING", "Crane exclusion zone", planned=True, planned_date="2026-12-01")])
    assert r["likelihood"] == 3


# ── R7: tolerability terminates ───────────────────────────────────────────

ALL = {
    "eliminationConsidered": True,
    "engineeringInPlaceOrPlanned": True,
    "statutoryCurrent": True,
    "reviewDateSet": True,
}


def tol(l, s, alarp=ALL, strict=False, credible=None, overridden=False):
    return eng.tolerability(
        eng.rate(l, s, CFG), credible_s=credible, alarp=alarp, strict=strict, cfg=CFG, overridden=overridden
    )


def test_s5_at_l1_with_checklist_is_tolerable_and_stops():
    t = tol(1, 5)
    assert t["state"] == eng.TOLERABLE_ALARP and eng.resolved(t)


def test_checklist_gap_names_the_missing_item_not_more_controls():
    t = tol(1, 5, {**ALL, "eliminationConsidered": False})
    assert t["route"] == "ALARP_CHECKLIST" and t["missing"] == ["eliminationConsidered"]


def test_s3_moderate_needs_only_a_review_date():
    assert tol(2, 3, {"reviewDateSet": True})["state"] == eng.TOLERABLE_ALARP


def test_strict_plant_rule_routes_to_override():
    t = tol(1, 5, strict=True)
    assert t["state"] == eng.NOT_ACCEPTABLE and t["route"] == "OVERRIDE"
    assert eng.resolved(tol(1, 5, strict=True, overridden=True))


def test_high_needs_controls_or_override():
    assert tol(2, 5)["route"] == "CONTROLS"
    assert tol(1, 4, strict=False)["state"] == eng.TOLERABLE_ALARP


def test_alarp_band_can_be_switched_off():
    cfg = deep_merge(CFG, {"tolerability": {"alarpBandEnabled": False}})
    t = eng.tolerability(eng.rate(1, 5, cfg), credible_s=None, alarp=ALL, strict=False, cfg=cfg)
    assert t["route"] == "CONTROLS"


# ── R8: recommendations ───────────────────────────────────────────────────


def test_semantic_dedupe_example():
    assert (
        rec.similarity("third party inspection of cranes", "pre-use check + third-party examination") >= 0.5
    )
    assert (
        rec.similarity("Overload limiter and limit switches on the crane", "Third party inspection of cranes")
        < 0.5
    )


# ── Review: benchmarks, caps, convergence, determinism ────────────────────

ENTRY1_HAZARDS = [
    H(
        "h1",
        "Crane / lifting operations",
        code="MECH-008",
        category="lifting",
        consequence="Rigger crushed by a falling 1 t bag: fatal",
    ),
    H(
        "h2",
        "Swinging load",
        code="VEH-010",
        category="lifting",
        consequence="struck or pinned by a swinging bag: crush injury, possible fatality",
    ),
    H(
        "h3",
        "Fall from bin top",
        code="WAH-003",
        category="height",
        consequence="falls into the bin opening: fatality, engulfment",
    ),
    H(
        "h4",
        "Falling object",
        code="MECH_FALLING_OBJECT",
        category="mechanical",
        consequence="struck by falling alloy lumps: head injury",
    ),
    H("h5", "Sharp edge", code="MECH_SHARP_EDGE", category="mechanical", consequence="hand cut by the knife"),
    H("h6", "Manual handling", code="MECH_HEAVY_LIFTING", category="mechanical", consequence="back strain"),
    H(
        "h7",
        "Airborne dust",
        code="ENV_DUST_AIRBORNE",
        category="environmental",
        consequence="chronic respiratory disease",
    ),
]
ENTRY1_EXISTING = [
    C("c1", "ADMINISTRATIVE", "Third-party examination of EOT crane and lifting tackles (statutory)"),
    C("c2", "ADMINISTRATIVE", "Work instruction for bag-to-bin feeding"),
    C("c3", "PPE", "Helmet, safety shoes, hand gloves"),
]


def entry1(controls=None):
    return rv.EntryInput(
        "e1",
        "#1",
        "Bag-to-bin feeding by crane",
        "ROUTINE",
        "DAILY",
        copy.deepcopy(ENTRY1_HAZARDS),
        4,
        5,
        (4, 5),
        controls=copy.deepcopy(controls or []),
    )


def entry6():
    return rv.EntryInput(
        "e6",
        "S.No 6",
        "Placement of the bucket/bags at the top of the bin",
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
        controls=[
            C("c1", "ADMINISTRATIVE", "Work instruction / SOP DOC No SM01/03/W"),
            C("c2", "ADMINISTRATIVE", "Administrative control: Third party inspection of cranes"),
            C(
                "c3",
                "PPE",
                "PPE: Helmet, Safety shoes, Dust mask, hand gloves, Fire resistant jacket, "
                "face shield, safety goggles",
            ),
        ],
        review_date_set=True,
    )


def review(e, rule=STD, plant=None, ev=NO_EVIDENCE):
    return rv.evaluate(e, cfg=CFG, rule=rule, evidence=ev, plant=plant)


def test_no_critical_residual_from_an_empty_control_list():
    r = review(entry1())
    assert r["entry"]["residual"] is None and r["entry"]["inherentOnly"]
    assert r["entry"]["residualLabel"] == "Inherent only. Existing controls not captured"
    assert "NO_CONTROLS" in rv.blocker_codes(r)
    assert all(h["residual"] is None for h in r["hazards"])


def test_entry1_with_existing_controls_drops_below_critical():
    r = review(entry1(ENTRY1_EXISTING))
    assert r["entry"]["residual"]["score"] <= 10 and r["entry"]["residual"]["level"] != "CRITICAL"
    assert len(r["recommendations"]) <= 5
    soft = {"h5", "h7"}  # sharp edge + dust
    assert sum(1 for x in r["recommendations"] if {t["key"] for t in x["targetHazards"]} & soft) <= 2
    assert all(eng.level_index(h["inherent"]["level"]) <= eng.level_index("CRITICAL") for h in r["hazards"])


def test_entry1_single_rating_is_not_spread_to_cuts_and_dust():
    r = review(entry1(ENTRY1_EXISTING))
    by = {h["key"]: h for h in r["hazards"]}
    assert by["h5"]["inherent"]["severity"] <= 3 and by["h7"]["inherent"]["severity"] <= 3
    assert any(f["code"] == "LIKELIHOOD_UNSUPPORTED" for f in r["findings"])  # L4 with no events


def test_entry6_no_l1_s2_claim_flags_severity_one_engineering_suggestion():
    r = review(entry6(), rule=STRICT)
    h = r["hazards"][0]
    assert (h["residual"]["likelihood"], h["residual"]["severity"]) == (2, 3)
    assert "L1 x S2" not in " ".join(r["controlsJustify"])
    assert any(f["code"] == "SEVERITY_UNDERSTATED" and f["class"] == rv.ADVISORY for f in r["findings"])
    assert len(r["recommendations"]) <= 1
    assert all(x["hierarchy"] == "ENGINEERING" for x in r["recommendations"])


def test_entry6_terminates_at_the_override_not_more_controls():
    e = entry6()
    r1 = review(e, rule=STRICT)
    r2 = review(rv.apply_suggestions(e, r1), rule=STRICT)
    assert not rv.blocker_codes(r2)
    assert r2["recommendations"] == []
    assert r2["entry"]["status"] == "OVERRIDE_PENDING"
    assert r2["alarpDraft"]


def test_zero_recommendations_when_everything_is_acceptable():
    e = rv.EntryInput(
        "e",
        "#9",
        "Office filing",
        "ROUTINE",
        "DAILY",
        [H("h", "Slip on floor", code="PHYS_SLIP_TRIP", category="physical", likelihood=1, severity=2)],
        1,
        2,
        None,
        controls=[C("c", "ADMINISTRATIVE", "Housekeeping schedule")],
        hazard_ratings_stored=True,
    )
    r = review(e)
    assert r["recommendations"] == [] and r["canSubmit"]


def test_recommendation_caps_by_entry_level():
    many = [
        H(f"h{i}", n, code=c, category=cat, likelihood=3, severity=5)
        for i, (n, c, cat) in enumerate(
            [
                ("Crane", "MECH-008", "lifting"),
                ("Fall", "WAH-003", "height"),
                ("Electric", "ELEC-001", "electrical"),
                ("Vehicle", "VEH-001", "transportation"),
                ("Confined", "CS-001", "confined_space"),
                ("Press", "MECH-001", "mechanical"),
                ("Pressure", "MECH-004", "pressure"),
            ]
        )
    ]
    e = rv.EntryInput(
        "e",
        "#x",
        "Shutdown maintenance",
        "NON_ROUTINE",
        "OCCASIONAL",
        many,
        3,
        5,
        None,
        controls=[C("c", "ADMINISTRATIVE", "Shutdown work instruction")],
        hazard_ratings_stored=True,
    )
    r = review(e)
    assert len(r["recommendations"]) <= 5
    low = rv.EntryInput(
        "e",
        "#y",
        "Bin top housekeeping",
        "ROUTINE",
        "WEEKLY",
        [
            H(f"m{i}", n, code=c, category=cat, likelihood=3, severity=2)
            for i, (n, c, cat) in enumerate(
                [
                    ("Slip", "PHYS_SLIP_TRIP", "physical"),
                    ("Cut", "MECH_SHARP_EDGE", "mechanical"),
                    ("Dust", "ENV_DUST_AIRBORNE", "environmental"),
                    ("Back", "MECH_HEAVY_LIFTING", "mechanical"),
                ]
            )
        ],
        3,
        2,
        None,
        controls=[C("c", "PPE", "Helmet, safety shoes")],
        hazard_ratings_stored=True,
    )
    r = review(low)
    assert len(r["recommendations"]) <= 3


def test_recommendations_dedupe_against_existing_capas_and_siblings():
    plant = rv.PlantData(
        open_capas=[
            {
                "id": "capa1",
                "number": "CAPA-7",
                "text": "Hard barricaded exclusion zone under the crane travel path",
            }
        ],
        sibling_recommended=[
            {
                "label": "#2",
                "text": "Overload limiter and limit switches on the crane, function-tested each shift",
            }
        ],
    )
    r = review(entry1(ENTRY1_EXISTING), plant=plant)
    descs = " ".join(x["description"].lower() for x in r["recommendations"])
    assert "exclusion zone" not in descs
    assert r["linkedCapas"] and r["linkedCapas"][0]["number"] == "CAPA-7"
    shared = [x for x in r["recommendations"] if x["studyLevel"]]
    assert all(x["sharedWith"] == ["#2"] for x in shared)


def test_high_cost_only_when_residual_high_and_one_capex_with_interim():
    r = review(entry1(ENTRY1_EXISTING))
    capex = [x for x in r["recommendations"] if x["costBand"] in ("HIGH", "VERY_HIGH")]
    assert len(capex) <= 1 and all(x["interimMeasure"] for x in capex)
    for x in r["recommendations"]:
        assert x["ownerRole"] and x["dueDays"] in (30, 60, 90, 180) and x["expectedEffect"]


def test_existing_controls_offered_once_and_unverified():
    plant = rv.PlantData(
        existing_control_proposals=[
            {
                "hierarchy": "ADMINISTRATIVE",
                "description": "Third-party examination of EOT crane (Form 10, register)",
                "source": "Statutory inspection register",
            },
            {
                "hierarchy": "ADMINISTRATIVE",
                "description": "Work instruction WI-AFS-03 bag feeding",
                "source": "Sibling entry #2",
            },
        ]
    )
    r = review(entry1(), plant=plant)
    assert len(r["existingControlProposals"]) == 2
    assert all(p["effectiveness"] == "NOT_VERIFIED" for p in r["existingControlProposals"])
    nc = next(f for f in r["findings"] if f["code"] == "NO_CONTROLS")
    assert nc["action"] == "USE_EXISTING_CONTROLS" and "2 existing controls" in nc["fix"]


@pytest.mark.parametrize(
    "make,rule", [(lambda: entry1(), STD), (lambda: entry1(ENTRY1_EXISTING), STD), (entry6, STRICT)]
)
def test_rerun_after_applying_raises_no_new_blockers(make, rule):
    e = make()
    r1 = review(e, rule=rule)
    e2 = rv.apply_suggestions(e, r1)
    r2 = review(e2, rule=rule)
    new = rv.blocker_codes(r2) - rv.blocker_codes(r1)
    assert not new
    if r1["existingControlProposals"] or not rv.blocker_codes(r1) - {"NO_CONTROLS"}:
        pass
    # a third pass changes nothing more: converged
    r3 = review(rv.apply_suggestions(e2, r2), rule=rule)
    assert rv.blocker_codes(r3) <= rv.blocker_codes(r2)


def test_entry1_converges_to_zero_blockers_in_one_pass():
    e = entry1(ENTRY1_EXISTING)
    r2 = review(rv.apply_suggestions(e, review(e)))
    assert not rv.blocker_codes(r2) and r2["recommendations"] == []


def test_deterministic():
    a = review(entry1(ENTRY1_EXISTING))
    b = review(entry1(ENTRY1_EXISTING))
    assert a == b
