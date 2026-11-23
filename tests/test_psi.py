"""PSI / Chem advisor: offline unit tests. Compatibility rules (incl. symmetry),
hazard-group derivation, SDS status, PSI completeness, and the advisor's
grounding / citation shaping against a mocked AI reply."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app.services import psi as svc
from app.services import psi_compat as compat
from app.services.psi_compat import Chem

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)

H2SO4 = Chem("c-h2so4", "Sulphuric acid 98%", "7664-93-9", ["Skin Corr. 1A", "H314"], ["GHS05"])
NAOH = Chem("c-naoh", "Sodium hydroxide solution 48%", "1310-73-2", ["Skin Corr. 1A"], ["GHS05"])
HNO3 = Chem("c-hno3", "Nitric acid 68%", "7697-37-2", ["Ox. Liq. 3", "Skin Corr. 1A", "H272"], ["GHS03", "GHS05"])
HF = Chem("c-hf", "Hydrofluoric acid 70%", "7664-39-3", ["Acute Tox. 2", "Skin Corr. 1A"], ["GHS06", "GHS05"])
O2 = Chem("c-o2", "Oxygen", "7782-44-7", ["Ox. Gas 1", "Press. Gas"], ["GHS03", "GHS04"])
LPG = Chem("c-lpg", "LPG", "68476-85-7", ["Flam. Gas 1", "Press. Gas"], ["GHS02", "GHS04"])
OIL = Chem("c-oil", "Hydraulic oil ISO VG 68", None, [], [])
DIESEL = Chem("c-hsd", "Diesel (HSD)", "68334-30-5", ["Flammable liquid Cat 3", "H226"], ["GHS02"])
NACN = Chem("c-nacn", "Sodium cyanide", "143-33-9", ["Acute Tox. 2", "H300"], ["GHS06"])
NA2S = Chem("c-na2s", "Sodium sulphide flakes", "1313-82-2", ["Skin Corr. 1B"], ["GHS05"])
CAC2 = Chem("c-cac2", "Calcium carbide", "75-20-7", ["Water-react. 1", "H260"], ["GHS02"])
HYPO = Chem("c-hypo", "Sodium hypochlorite solution 12%", "7681-52-9", ["Skin Corr. 1B"], ["GHS05"])
NH3 = Chem("c-nh3", "Ammonia solution 25%", "1336-21-6", ["Skin Corr. 1B"], ["GHS05"])
H2O2 = Chem("c-h2o2", "Hydrogen peroxide 50%", "7722-84-1", ["Ox. Liq. 1", "H271"], ["GHS03", "GHS05"])
ACETONE = Chem("c-acetone", "Acetone", "67-64-1", ["Flam. Liq. 2", "H225"], ["GHS02"])
UNKNOWN = Chem("c-x", "Mystery additive XR-7", None, [], [])
ARGON = Chem("c-ar", "Argon", "7440-37-1", ["Press. Gas"], ["GHS04"])

ALL = [H2SO4, NAOH, HNO3, HF, O2, LPG, OIL, DIESEL, NACN, NA2S, CAC2, HYPO, NH3, H2O2, ACETONE, UNKNOWN, ARGON]


def verdict(a, b):
    return compat.check_pair(a, b)["verdict"]


def rule_ids(a, b):
    return {r["ruleId"] for r in compat.check_pair(a, b)["reasons"]}


# ── derivation ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("chem,expect", [
    (H2SO4, {"ACID"}), (NAOH, {"BASE"}), (HNO3, {"ACID", "OXIDISER"}), (O2, {"OXIDISING_GAS", "COMPRESSED_GAS"}),
    (LPG, {"FLAMMABLE_GAS", "COMPRESSED_GAS"}), (OIL, {"OILS_GREASE", "ORGANIC"}), (DIESEL, {"FLAMMABLE_LIQUID"}),
    (NACN, {"CYANIDE", "TOXIC"}), (NA2S, {"SULPHIDE"}), (CAC2, {"WATER_REACTIVE"}), (HYPO, {"HYPOCHLORITE"}),
    (NH3, {"AMMONIA", "BASE"}), (H2O2, {"PEROXIDE", "OXIDISER"}), (ARGON, {"COMPRESSED_GAS"}),
])
def test_derive_groups(chem, expect):
    assert expect <= compat.derive_groups(chem)


def test_oxygen_is_an_oxidising_gas_not_a_solid_oxidiser():
    g = compat.derive_groups(O2)
    assert "OXIDISER" not in g and "OXIDISING_GAS" in g


def test_specific_flammable_form_drops_generic_flag_and_acid_drops_corrosive():
    assert "FLAMMABLE" not in compat.derive_groups(DIESEL)
    assert "CORROSIVE" not in compat.derive_groups(H2SO4)


def test_unknown_chemical_has_no_groups():
    assert compat.derive_groups(UNKNOWN) == set()


# ── rules ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b,expected,rule", [
    (H2SO4, NAOH, "INCOMPATIBLE", "ACID_BASE"),
    (H2SO4, NACN, "INCOMPATIBLE", "ACID_CYANIDE"),
    (HF, NA2S, "INCOMPATIBLE", "ACID_SULPHIDE"),
    (H2SO4, HYPO, "INCOMPATIBLE", "ACID_HYPOCHLORITE"),
    (HYPO, NH3, "INCOMPATIBLE", "HYPOCHLORITE_AMMONIA"),
    (O2, OIL, "INCOMPATIBLE", "OXYGEN_OILS"),
    (O2, LPG, "INCOMPATIBLE", "OXYGEN_FUEL_GAS"),
    (HNO3, DIESEL, "INCOMPATIBLE", "OXIDISER_FUEL"),
    (H2O2, ACETONE, "INCOMPATIBLE", "PEROXIDE_FUEL"),
    (CAC2, H2SO4, "INCOMPATIBLE", "WATER_REACTIVE_AQUEOUS"),
    (CAC2, NAOH, "INCOMPATIBLE", "WATER_REACTIVE_AQUEOUS"),  # NaOH solution is aqueous
    (HNO3, HF, "CAUTION", "OXIDISER_ACID"),
    (LPG, DIESEL, "CAUTION", "FLAMMABLE_GAS_LIQUID"),
    (ARGON, ACETONE, "CAUTION", "GAS_CYLINDER_FIRE"),
])
def test_rule_pairs(a, b, expected, rule):
    assert verdict(a, b) == expected
    assert rule in rule_ids(a, b)


def test_compatible_pair_gets_explicit_reason():
    res = compat.check_pair(H2SO4, HF)  # two acids: no rule
    assert res["verdict"] == "COMPATIBLE"
    assert res["reasons"][0]["ruleId"] == "NO_RULE"


def test_unknown_hazard_is_caution_not_compatible():
    res = compat.check_pair(UNKNOWN, ARGON)
    assert res["verdict"] == "CAUTION" and res["reasons"][0]["ruleId"] == "UNKNOWN_HAZARD"


def test_register_listed_incompatibility_by_name_cas_and_group():
    a = Chem("a", "Ferric chloride solution", None, ["Skin Corr. 1C"], [], incompatibleSubstances=["Sodium cyanide", "strong bases"])
    assert "REGISTER_LISTED" in rule_ids(a, NACN)  # by name
    assert verdict(a, NAOH) == "INCOMPATIBLE"  # 'strong bases' matches the BASE group
    b = Chem("b", "Product Z", None, [], [], incompatibleSubstances=["7664-93-9"])
    assert verdict(b, H2SO4) == "INCOMPATIBLE"  # by CAS, even though Z has no GHS data


def test_every_pair_is_symmetric():
    for a, b in itertools.combinations(ALL, 2):
        assert compat.check_pair(a, b) == compat.check_pair(b, a), (a.name, b.name)


def test_matrix_shape_and_worst():
    m = compat.matrix([H2SO4, NAOH, HF])
    assert m["cells"][0][0] is None and m["cells"][0][1] == m["cells"][1][0] == "INCOMPATIBLE"
    assert len(m["pairs"]) == 3 and m["worst"] == "INCOMPATIBLE"
    assert m["chemicals"][0]["hazardGroupLabels"]


def test_rules_as_data_valid():
    for r in compat.rules_as_data():
        assert r["verdict"] in compat.VERDICTS
        assert set(r["left"]) <= set(compat.GROUP_LABEL) and set(r["right"]) <= set(compat.GROUP_LABEL)
        assert r["reason"] and r["advice"]


# ── register flags ───────────────────────────────────────────────────────────
def test_sds_status():
    assert svc.sds_status(None, NOW) == "MISSING"
    assert svc.sds_status({"hasFile": False, "reviewDue": NOW + timedelta(days=400)}, NOW) == "MISSING"
    assert svc.sds_status({"hasFile": True, "reviewDue": NOW - timedelta(days=1)}, NOW) == "EXPIRED"
    assert svc.sds_status({"hasFile": True, "reviewDue": NOW + timedelta(days=10)}, NOW) == "DUE_SOON"
    assert svc.sds_status({"hasFile": True, "reviewDue": NOW + timedelta(days=100)}, NOW) == "CURRENT"
    # no review date: issued + 3 years
    assert svc.sds_status({"hasFile": True, "issuedDate": NOW - timedelta(days=4 * 365)}, NOW) == "EXPIRED"
    assert svc.sds_status({"hasFile": True, "issuedDate": NOW - timedelta(days=365)}, NOW) == "CURRENT"


def test_over_max_and_utilisation():
    assert svc.over_max(120, 100) and not svc.over_max(100, 100) and not svc.over_max(500, None)
    assert svc.utilisation(50, 200) == 25.0 and svc.utilisation(5, 0) is None


def test_completeness_per_unit():
    docs = [
        {"id": "1", "unitArea": "CRM-2 Pickling", "docType": "PID", "status": "CURRENT", "hasFile": True, "docNo": "P-1", "revision": "B"},
        {"id": "2", "unitArea": "CRM-2 Pickling", "docType": "PID", "status": "SUPERSEDED", "hasFile": True, "docNo": "P-1", "revision": "A"},
        {"id": "3", "unitArea": "CRM-2 Pickling", "docType": "SAFE_OPERATING_LIMITS", "status": "CURRENT", "hasFile": False,
         "reviewDue": NOW - timedelta(days=1), "docNo": "SOL-1", "revision": "0"},
        {"id": "4", "unitArea": None, "docType": "PFD", "status": "CURRENT", "hasFile": True, "docNo": "F-1", "revision": "0"},
        {"id": "5", "unitArea": "CRM-2 Pickling", "docType": "SDS", "status": "CURRENT", "hasFile": True, "docNo": "S-1", "revision": "1"},
    ]
    out = svc.completeness(docs, units=["SMS-1 EAF"], now=NOW)
    units = {u["unitArea"]: u for u in out}
    crm = units["CRM-2 Pickling"]
    assert crm["present"] == 2 and crm["percent"] == round(200 / len(svc.REQUIRED_UNIT_DOC_TYPES))
    assert "PFD" in crm["missing"] and crm["reviewOverdue"] == 1
    assert units["SMS-1 EAF"]["present"] == 0
    assert units[svc.UNASSIGNED_UNIT]["present"] == 1
    assert out[-1]["unitArea"] == svc.UNASSIGNED_UNIT  # unassigned sorts last


def test_revision_blockers():
    assert svc.revision_blockers(["A", "B"], "b")
    assert svc.revision_blockers([], " ")
    assert svc.revision_blockers(["A"], "C") == []


# ── advisor ──────────────────────────────────────────────────────────────────
REGISTER = [
    {"id": "h1", "chemicalName": "Nitric acid 68%", "casNumber": "7697-37-2"},
    {"id": "h2", "chemicalName": "Hydrofluoric acid 70%", "casNumber": "7664-39-3"},
    {"id": "h3", "chemicalName": "Sodium hydroxide solution 48%", "casNumber": "1310-73-2"},
    {"id": "h4", "chemicalName": "Diesel (HSD)", "casNumber": "68334-30-5"},
]


def test_match_chemicals_by_name_token_and_cas():
    assert svc.match_chemicals("Can nitric be stored next to HF 7664-39-3?", REGISTER)[:2] == ["h2", "h1"]
    assert svc.match_chemicals("Where is the diesel kept?", REGISTER) == ["h4"]
    # generic words ('acid', 'sodium') alone do not match everything
    assert svc.match_chemicals("Which acid is the most dangerous?", REGISTER) == []


def _fact(rid: str, name: str, **kw):
    row = {"id": rid, "chemicalName": name, "casNumber": kw.pop("cas", None), "storageBuilding": "Acid yard",
           "storageRoom": "Bay 2", "ppeRequired": ["Face shield", "PVC gloves"], "spillKitLocation": "Bay 2 gate",
           "secondaryContainmentPresent": True, **kw}
    return svc.chem_fact(row, ["ACID"], {"docNo": "SDS-1", "revision": "3", "sdsStatus": "CURRENT", "reviewDueIso": None})


FACTS = [_fact("h1", "Nitric acid 68%", cas="7697-37-2"), _fact("h2", "Hydrofluoric acid 70%")]


def test_shape_keeps_only_grounded_citations():
    ai = {"answer": "Store apart unless mixed by design.", "usedRecordIds": ["h1", "h-invented", "h1", 7], "generalGuidance": True,
          "followUps": ["What PPE for HF?", "", 3]}
    out = svc.shape_advisor_response(question="nitric with HF?", facts=FACTS, compat=None, ai=ai, ai_configured=True)
    assert out["aiUsed"] and out["mode"] == "AI"
    assert [c["id"] for c in out["citations"]] == ["h1"]
    assert out["droppedCitations"] == 2
    assert out["generalGuidance"] is True
    assert out["followUps"] == ["What PPE for HF?"]
    assert "Verify against the current SDS" in out["disclaimer"]
    assert out["citations"][0]["sds"]["docNo"] == "SDS-1"


def test_shape_cites_all_facts_when_model_names_none():
    out = svc.shape_advisor_response(question="q?", facts=FACTS, compat=None, ai={"answer": "x"}, ai_configured=True)
    assert [c["id"] for c in out["citations"]] == ["h1", "h2"]


def test_shape_never_leaks_model_details():
    out = svc.shape_advisor_response(question="q?", facts=FACTS, compat=None,
                                     ai={"answer": "x", "model": "claude-x", "cost": 1.2}, ai_configured=True)
    flat = repr(out).lower()
    assert "claude" not in flat and "cost" not in flat and "latency" not in flat


@pytest.mark.parametrize("ai,configured,mode", [
    (None, False, "RULES_ONLY_AI_UNAVAILABLE"),
    (None, True, "RULES_ONLY_AI_FAILED"),
    ({"answer": "   "}, True, "RULES_ONLY_AI_FAILED"),
    ({"text": "wrong shape"}, True, "RULES_ONLY_AI_FAILED"),
])
def test_soft_fail_still_answers_from_register_and_rules(ai, configured, mode):
    m = compat.matrix([HNO3, HF])
    out = svc.shape_advisor_response(question="Can nitric acid be stored with HF? What PPE and fire media?",
                                     facts=FACTS, compat=m, ai=ai, ai_configured=configured)
    assert out["mode"] == mode and not out["aiUsed"]
    assert "CAUTION" in out["answer"]  # the rule verdict is in the text
    assert "Face shield" in out["answer"]  # PPE from the register
    assert "SDS section 5" in out["answer"]  # fire media not in register -> points to SDS
    assert out["compatibility"]["worst"] == "CAUTION"
    assert {c["id"] for c in out["citations"]} == {"h1", "h2"}


def test_fallback_with_no_facts_says_not_in_register():
    out = svc.shape_advisor_response(question="What about unobtainium?", facts=[], compat=None, ai=None, ai_configured=False)
    assert "none of the chemicals" in out["answer"].lower() and out["citations"] == []


def test_prompt_carries_facts_and_compat_not_secrets():
    p = svc.build_advisor_prompt("q", FACTS, compat.matrix([HNO3, HF]), "JSL Hisar")
    assert "Nitric acid 68%" in p and "OXIDISER_ACID" not in p and "CAUTION" in p and "JSL Hisar" in p


def test_detect_topics():
    assert set(svc.detect_topics("What PPE and spill response for HF?")) >= {"ppe", "spill"}
    assert svc.detect_topics("tell me about it") == ["general"]


def test_match_chemicals_understands_plant_shorthand():
    assert set(svc.match_chemicals("Can HNO3 sit next to HF?", REGISTER)) == {"h1", "h2"}
    assert svc.match_chemicals("caustic spill response", REGISTER) == ["h3"]
