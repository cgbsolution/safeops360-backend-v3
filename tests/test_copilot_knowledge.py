"""Copilot reference libraries, observation / HIRA form drafts and the risk
what-if — offline (no DB, no network, no LLM spend)."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from app.services.copilot import form_drafts, runtime, tools
from app.services.copilot.knowledge import hira_library as hira_kb
from app.services.copilot.knowledge import observations as obs_kb
from app.services.copilot.knowledge.text_index import Bm25Index, tokenize

# The STOP taxonomy as seeded (safeops_360/prisma/seed-observation-taxonomy.ts).
STOP_PAIRS = {
    "ACT": {
        ("REACTIONS_OF_PEOPLE", s) for s in ("RP_ADJUSTED_PPE", "RP_CHANGED_POSITION", "RP_REARRANGED_JOB", "RP_STOPPED_JOB", "RP_ATTACHED_GROUNDS")
    } | {
        ("POSITIONS_OF_PEOPLE", s) for s in ("PP_STRIKING_AGAINST", "PP_STRUCK_BY", "PP_CAUGHT_IN_ON_BETWEEN", "PP_FALLING_SAME_LEVEL",
                                              "PP_FALLING_DIFFERENT_LEVEL", "PP_CONTACT_TEMPERATURE", "PP_CONTACT_ELECTRICAL", "PP_OVEREXERTION")
    } | {("PPE", s) for s in ("PPE_NOT_WORN", "PPE_WORN_INCORRECTLY", "PPE_REMOVED_DURING_TASK")}
      | {("TOOLS_EQUIPMENT", s) for s in ("TE_WRONG_TOOL", "TE_USED_INCORRECTLY", "TE_IMPROVISED_TOOL")}
      | {("PROCEDURES", s) for s in ("PR_NOT_FOLLOWED", "PR_DEVIATED_FROM_SOP", "PR_PERMIT_LOTO_BYPASSED")}
      | {("HOUSEKEEPING", s) for s in ("HK_NOT_CLEAN_AS_YOU_GO", "HK_OBSTRUCTING_PATH", "HK_IMPROPER_WASTE_DISPOSAL")},
    "CONDITION": {("PPE", s) for s in ("PPE_NOT_AVAILABLE", "PPE_DAMAGED_OR_EXPIRED", "PPE_WRONG_SPEC")}
      | {("TOOLS_EQUIPMENT", s) for s in ("TE_DAMAGED", "TE_UNSAFE_CONDITION", "TE_GUARD_MISSING", "TE_OVERDUE_MAINTENANCE")}
      | {("PROCEDURES", s) for s in ("PR_INADEQUATE_OR_OUTDATED", "PR_NOT_AVAILABLE", "PR_NOT_UNDERSTOOD")}
      | {("HOUSEKEEPING", s) for s in ("HK_SPILL_NOT_CLEANED", "HK_BLOCKED_EGRESS", "HK_POOR_LIGHTING", "HK_CLUTTER_DEBRIS", "HK_DAMAGED_FLOORING")},
}

# STD_5X5 as seeded (seed-hira-masters.ts cellLevel), score = L x S.
_GRID = [
    ["LOW", "LOW", "LOW", "MODERATE", "MODERATE"],
    ["LOW", "LOW", "MODERATE", "HIGH", "HIGH"],
    ["LOW", "MODERATE", "HIGH", "HIGH", "CRITICAL"],
    ["MODERATE", "HIGH", "HIGH", "CRITICAL", "CRITICAL"],
    ["MODERATE", "HIGH", "CRITICAL", "CRITICAL", "CRITICAL"],
]
CELLS = [SimpleNamespace(likelihoodScore=l, severityScore=s, riskScore=l * s, riskLevel=_GRID[l - 1][s - 1], colorHex="#000")
         for l in range(1, 6) for s in range(1, 6)]
BANDS = {"LOW": "BROADLY_ACCEPTABLE", "MODERATE": "TOLERABLE", "HIGH": "TOLERABLE", "CRITICAL": "UNACCEPTABLE"}


# ─── Text index ─────────────────────────────────────────────────────────


def test_tokenizer_folds_plant_vocabulary():
    assert "railing" in tokenize("Hand rail missing") or "railing" in tokenize("handrail missing")
    assert tokenize("Oil leakage on the walkway") == tokenize("oil leak walkway")
    assert "cable" in tokenize("exposed wiring")


def test_bm25_ranks_the_relevant_document_first():
    idx = Bm25Index([tokenize("broken handrail on staircase"), tokenize("oil spill near pump"), tokenize("no lighting in cable cellar")])
    assert idx.search(tokenize("railing damaged on stairs"), limit=1)[0][0] == 0


# ─── Observation library ────────────────────────────────────────────────


def test_observation_library_is_installed_and_has_no_personal_data():
    lib = obs_kb.library()
    assert lib["meta"]["available"], "observation_library.json missing — run scripts/copilot_knowledge/build_library.py"
    assert lib["meta"]["included"] > 5000 and lib["meta"]["exemplars"] > 1000
    raw = obs_kb.DATA.read_text(encoding="utf-8")
    assert not re.search(r"\b9000\d{4}\b", raw), "employee ids in the library"
    assert not re.search(r"\b[6-9]\d{9}\b", raw), "phone numbers in the library"
    for c in lib["cases"]:
        if not c.get("ex"):
            assert "obs" not in c and "action" not in c, "free text kept on a non-exemplar row"


def test_every_stop_mapping_exists_in_the_taxonomy_for_its_axis():
    for cat, rules in obs_kb.STOP_MAP.items():
        axis = "ACT" if cat.startswith("A") else "CONDITION"
        for category, sub, _ in rules:
            assert (category, sub) in STOP_PAIRS[axis], f"{cat} → {category}/{sub} is not a {axis} pair"


@pytest.mark.parametrize("text,expect_cat_prefix", [
    ("Electrical panel door open and live terminals exposed near the walkway", "C3"),
    ("Hand rail of the staircase broken near the mill stand, person may fall", "C"),
    ("Worker not wearing safety helmet while working near the crane", "A5"),
])
def test_suggest_classifies_typical_observations(text, expect_cat_prefix):
    s = obs_kb.suggest(text)
    assert s["available"] and s["matches"] > 0
    assert s["category"]["code"].startswith(expect_cat_prefix), s["category"]
    assert s["risk"]["safeopsSeverity"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert s["examples"] and all(e["observation"] for e in s["examples"])
    assert s["stopCandidates"]


def test_fatal_signals_and_quality_notes():
    assert obs_kb.fatal_signals("Open edge on the platform, railing missing, 6 m height")
    assert obs_kb.fatal_signals("Molten metal splash near the tapping area")
    assert not obs_kb.fatal_signals("Dust bin overflowing in the canteen")
    assert obs_kb.text_quality("oil")  # far too short
    assert obs_kb.text_quality("Oil spill at the AP4 entry section walkway, people may slip and fall") == []


def test_fatal_potential_is_suggested_for_unprotected_height():
    s = obs_kb.suggest("Railing missing at the open edge of the furnace top platform, 8 m height, workers go there daily",
                       observation_type="UNSAFE_CONDITION")
    assert s["fatalPotentialSignals"]
    assert s["risk"]["safeopsSeverity"] in ("HIGH", "CRITICAL")


# ─── HIRA library ───────────────────────────────────────────────────────


def test_hira_library_entries_are_consistent_with_the_plant_rule():
    lib = hira_kb.library()
    assert lib["meta"]["available"] and len(lib["entries"]) > 300
    for e in lib["entries"]:
        sc = e["scores"]
        pm = hira_kb.plant_method(sc["SP"], sc["LH"], sc["CN"], sc["DE"], e["concerns"])
        assert pm["GT"] == sc["GT"], e["ref"]
        flagged = any(f.startswith("CONSERVATIVE_VERDICT") for f in e["reviewFlags"])
        assert pm["acceptable"] == e["acceptable"] or (flagged and not e["acceptable"]), e["ref"]


def test_hira_search_finds_crane_ladle_rows_and_control_ideas():
    hits = hira_kb.search("lifting the teeming ladle with crane", limit=5)
    assert hits and any(h["hazardType"] == "EQUIPMENT_FAILURE" for h in hits)
    ideas = hira_kb.control_ideas("EQUIPMENT_FAILURE")
    assert any(i["hierarchy"] == "ENGINEERING" for i in ideas)
    assert all("not from the plant HIRA" in i["source"] for i in ideas)


def test_plant_method_concern_keeps_risk_significant():
    low = hira_kb.plant_method(1, 1, 2, 1, {"legal": True})
    assert low["GT"] == 2 and low["significant"] and "legal" in low["reasons"][0]
    assert hira_kb.plant_method(1, 1, 3, 1, {})["acceptable"]
    assert not hira_kb.plant_method(2, 1, 4, 1, {})["acceptable"]


# ─── Form drafts ────────────────────────────────────────────────────────


def test_client_form_is_whitelisted():
    kind, d = form_drafts.normalise_client_form({"kind": "OBSERVATION", "draft": {
        "type": "UNSAFE_CONDITION", "severity": "EXTREME", "description": "x" * 5000, "evil": "drop table"}})
    assert kind == "OBSERVATION" and d["severity"] is None and len(d["description"]) == 4000 and "evil" not in d
    assert form_drafts.normalise_client_form({"kind": "NOPE", "draft": {}}) == (None, None)
    kind, d = form_drafts.normalise_client_form({"kind": "HIRA_ENTRY", "draft": {
        "existingControls": [{"key": "c1", "hierarchy": "engineering", "effectiveness": "EFFECTIVE"}, "junk"],
        "initial": {"likelihood": 4, "severity": 99}}})
    assert d["existingControls"][0]["hierarchy"] == "ENGINEERING" and len(d["existingControls"]) == 1
    assert d["initial"]["likelihood"] == 4 and d["initial"]["severity"] is None


def test_observation_patch_rules():
    p, err = form_drafts.parse_observation_patch({"categoryCode": "HOUSEKEEPING"})
    assert p is None and "together" in err
    p, err = form_drafts.parse_observation_patch({"severity": "HIGH", "description": "short"})
    assert p is None and "description" in err
    p, err = form_drafts.parse_observation_patch({"type": "UNSAFE_CONDITION", "categoryCode": "HOUSEKEEPING",
                                                  "subCategoryCode": "HK_SPILL_NOT_CLEANED", "severity": "medium"})
    assert err is None and p["severity"] == "MEDIUM"


def test_hira_patch_rules_and_apply():
    draft = {"existingControls": [{"key": "c1", "hierarchy": "ADMINISTRATIVE", "effectiveness": None}], "recommendedControls": []}
    p, err = form_drafts.parse_hira_patch({"setControlEffectiveness": [{"key": "zz", "effectiveness": "EFFECTIVE"}]}, draft)
    assert p is None and "existing control key" in err
    p, err = form_drafts.parse_hira_patch({
        "addRecommendedControls": [{"hierarchy": "ENGINEERING", "description": "Interlocked guard on the coupling", "rationale": "nip point"}],
        "setControlEffectiveness": [{"key": "c1", "effectiveness": "EFFECTIVE"}],
        "target": {"likelihoodScore": 2, "severityScore": 3}}, draft)
    assert err is None
    merged = form_drafts.apply_hira_patch(draft, p)
    assert merged["existingControls"][0]["effectiveness"] == "EFFECTIVE"
    assert merged["recommendedControls"][0]["status"] == "PROPOSED"
    assert draft["existingControls"][0]["effectiveness"] is None  # original untouched


# ─── Risk what-if ───────────────────────────────────────────────────────


def test_whatif_engineering_control_makes_it_acceptable():
    before = [{"hierarchy": "ADMINISTRATIVE", "effectiveness": "EFFECTIVE", "description": "SOP"}]
    after = before + [{"hierarchy": "ENGINEERING", "effectiveness": "EFFECTIVE", "description": "Guard"}]
    r = form_drafts.assess_controls(cells=CELLS, initial_l=4, initial_s=4, before_controls=before, after_controls=after,
                                    threshold="MODERATE", bands=BANDS)
    assert r["initial"]["level"] == "CRITICAL"
    assert (r["before"]["likelihood"], r["before"]["severity"], r["before"]["level"]) == (3, 4, "HIGH")
    assert (r["after"]["likelihood"], r["after"]["severity"], r["after"]["level"]) == (1, 3, "LOW")
    assert r["after"]["verdict"] == "ACCEPTABLE" and r["direction"] == "lower"
    assert r["toImprove"] is None


def test_whatif_names_the_cheapest_control_that_gets_there():
    before = [{"hierarchy": "ADMINISTRATIVE", "effectiveness": "EFFECTIVE"}]
    r = form_drafts.assess_controls(cells=CELLS, initial_l=4, initial_s=4, before_controls=before, after_controls=before,
                                    threshold="MODERATE", bands=BANDS)
    assert r["direction"] == "unchanged" and r["after"]["verdict"] == "NOT_ACCEPTABLE"
    opts = r["toImprove"]["options"]
    assert [o["addOneEffective"] for o in opts][0] == "ELIMINATION"  # highest in the hierarchy first
    eng = next(o for o in opts if o["addOneEffective"] == "ENGINEERING")
    assert eng["reachesAcceptable"] and eng["wouldGive"]["level"] == "LOW"
    assert all(o["addOneEffective"] != "ADMINISTRATIVE" for o in opts)  # a second SOP does not move L3 x S4


def test_whatif_unverified_controls_get_partial_credit():
    ctl = [{"hierarchy": "ENGINEERING", "effectiveness": "NOT_VERIFIED"}]
    r = form_drafts.assess_controls(cells=CELLS, initial_l=4, initial_s=4, before_controls=[], after_controls=ctl,
                                    threshold="MODERATE", bands=BANDS)
    assert (r["after"]["likelihood"], r["after"]["severity"]) == (3, 4)  # 2 x 0.3 = 0.6 → 1 step; 1 x 0.3 → 0
    assert any("Unverified" in line for line in r["explanation"])
    assert r["toImprove"]["ifExistingVerifiedEffective"]["likelihood"] == 2


def test_whatif_matches_the_hira_router_model():
    from app.routers.hira import _suggest_residual_scores

    ctl = [{"hierarchy": "SUBSTITUTION", "effectiveness": "PARTIALLY_EFFECTIVE"}, {"hierarchy": "PPE", "effectiveness": None}]
    objs = [SimpleNamespace(**c) for c in ctl]
    assert form_drafts.residual(5, 5, ctl) == _suggest_residual_scores(5, 5, objs)


# ─── Wiring ─────────────────────────────────────────────────────────────


def test_new_tools_are_registered_with_labels():
    for name in ("suggest_observation_fields", "update_observation_draft", "search_hira_library",
                 "assess_hira_controls", "update_hira_entry_draft"):
        assert name in tools.TOOL_NAMES and name in tools.TOOL_LABELS
    json.dumps(tools.TOOL_DEFINITIONS)  # serialisable for the API


def test_page_context_carries_the_open_form():
    msg = runtime.build_user_message(text="hi", page={"path": "/observations/new"}, draft=None, now_local=None, tz_offset=0,
                                     user_plant_id="p1", user_id="u1", form_kind="OBSERVATION",
                                     form_draft={"type": "UNSAFE_ACT", "description": "x"})
    ctx = json.loads(msg["content"][0]["text"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert ctx["observationFormOpen"] is True and ctx["observationDraft"]["type"] == "UNSAFE_ACT"
    assert "hiraEntryDraft" not in ctx and ctx["ptwFormOpen"] is False


async def test_form_tools_refuse_when_the_form_is_not_open():
    ctx = tools.TurnContext(user_id="u1", user_plant_id=None, conversation_id="c" * 10, page={}, emit=None)  # type: ignore[arg-type]
    with pytest.raises(tools.ToolError, match="Observation form is not open"):
        await tools._update_observation_draft(None, {"severity": "HIGH"}, ctx)  # type: ignore[arg-type]
    with pytest.raises(tools.ToolError, match="No HIRA entry is open"):
        await tools._update_hira_entry_draft(None, {"target": {"likelihoodScore": 1, "severityScore": 1}}, ctx)  # type: ignore[arg-type]
