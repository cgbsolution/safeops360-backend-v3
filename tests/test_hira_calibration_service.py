"""HIRA calibration v2 flag wiring: OFF / SHADOW / ON for the review, the HIRA
Assistant output and Copilot drafts. DB access is stubbed (no database)."""

from __future__ import annotations

import asyncio
import copy

import pytest

from app.services.hira_calibration import config as cal_config
from app.services.hira_calibration import review_v2 as rv
from app.services.hira_calibration import risk_engine as eng
from app.services.hira_calibration import service
from tests.test_hira_calibration_engine import ENTRY1_EXISTING, ENTRY1_HAZARDS, STD, entry1


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def flag(monkeypatch):
    state = {"mode": "OFF", "shadow": []}

    async def load(db, plant_id):
        cfg = copy.deepcopy(cal_config.DEFAULT_CONFIG)
        cfg["rubricPack"] = state.get("pack", "STEEL_HEAVY")  # a configured steel plant
        return cfg, state["mode"], {"tenant": 1, "plant": None}

    async def plant_of_entry(db, entry_id):
        return "plant-1"

    async def log_shadow(**kw):
        state["shadow"].append(kw)

    monkeypatch.setattr(cal_config, "load", load)
    monkeypatch.setattr(service, "plant_of_entry", plant_of_entry)
    monkeypatch.setattr(service, "log_shadow", log_shadow)
    return state


@pytest.fixture
def built(monkeypatch):
    async def build(db, entry_id, cfg):
        return {
            "input": entry1(ENTRY1_EXISTING),
            "rule": STD,
            "evidence": eng.Evidence(),
            "plant": rv.PlantData(),
            "entry": None,
            "study": None,
            "schemaReady": False,
        }

    async def build_for_draft(db, *, study, area_id, families, cfg):
        return {"rule": STD, "evidence": eng.Evidence(), "plant": rv.PlantData()}

    monkeypatch.setattr(service.loader, "build", build)
    monkeypatch.setattr(service.loader, "build_for_draft", build_for_draft)


@pytest.fixture
def v1(monkeypatch):
    from app.services.copilot import hira_review

    async def review_entry(db, entry_id):
        return {"headline": "v1", "findings": [], "counts": {}, "residual": {}, "controlIdeas": []}

    monkeypatch.setattr(hira_review, "review_entry", review_entry)


# ── review ──


def test_off_shows_v1_and_logs_nothing(flag, built, v1):
    r = run(service.review(None, "e1", user_id="u"))
    assert r["version"] == "v1" and r["headline"] == "v1" and flag["shadow"] == []


def test_shadow_shows_v1_and_logs_both(flag, built, v1):
    flag["mode"] = "SHADOW"
    r = run(service.review(None, "e1", user_id="u"))
    assert r["version"] == "v1"
    assert len(flag["shadow"]) == 1
    log = flag["shadow"][0]
    assert log["surface"] == "REVIEW" and log["v1"]["headline"] == "v1" and log["v2"]["engineVersion"]
    assert log["summary"]["v2"]["residual"].startswith("HIGH")


def test_on_shows_v2(flag, built, v1):
    flag["mode"] = "ON"
    r = run(service.review(None, "e1", user_id="u"))
    assert r["version"] == "v2" and r["entry"]["residual"]["level"] == "HIGH"


def test_preview_returns_v2_while_off(flag, built, v1):
    r = run(service.review(None, "e1", user_id="u", preview=True))
    assert r["version"] == "v2" and r["mode"] == "OFF"


# ── router hook ──


def test_residual_hook_is_a_noop_unless_on(flag, built):
    class E:
        id = "e1"
        study = type("S", (), {"plantId": "plant-1"})()

    class DB:
        async def flush(self):
            return None

    assert run(service.apply_v2_residual(DB(), E(), {}, {}, [])) is False


def test_residual_hook_clears_residual_for_inherent_only(flag, monkeypatch):
    flag["mode"] = "ON"

    async def build(db, entry_id, cfg):
        return {
            "input": entry1(),
            "rule": STD,
            "evidence": eng.Evidence(),
            "plant": rv.PlantData(),
            "entry": None,
            "study": None,
            "schemaReady": False,
        }

    monkeypatch.setattr(service.loader, "build", build)

    class E:
        id = "e1"
        study = type("S", (), {"plantId": "plant-1"})()
        residualLikelihoodScore = 4
        residualSeverityScore = 5
        residualRiskLevel = "CRITICAL"

    class DB:
        async def flush(self):
            return None

    e = E()
    assert run(service.apply_v2_residual(DB(), e, {}, {}, [])) is True
    assert e.residualRiskLevel is None and e.residualLikelihoodScore is None  # never residual = initial


# ── HIRA Assistant output ──


def _assistant_suggestion():
    ids = [h.key for h in ENTRY1_HAZARDS]
    return {
        "suggested_hazards": [
            {
                "hazard_master_id": i,
                "relevance_confidence": 0.9,
                "reasoning": "x",
                "suggested_for_addition": True,
            }
            for i in ids
        ],
        "control_suggestions": [
            {
                "hazard_id": ids[0],
                "engineering_controls": [{"description": "Overload limit switch on EOT crane"}] * 3,
                "administrative_controls": [{"description": "Awareness training"}] * 4,
                "ppe_controls": [{"description": "Dust mask"}],
            }
        ],
        "risk_calibration": {
            "initial_likelihood_suggestion": {"score": 4, "reasoning": "daily"},
            "initial_severity_suggestion": {"score": 5, "reasoning": "fatal"},
            "residual_likelihood_guidance": "should reduce to 1",
            "residual_severity_guidance": "PPE reduces severity to 2",
        },
    }


def _context():
    lib = [
        {"hazard_master_id": h.key, "name": h.name, "code": h.code, "category": h.category}
        for h in ENTRY1_HAZARDS
    ]
    return {
        "activity": {
            "description": "Bag-to-bin feeding by crane",
            "routine": "ROUTINE",
            "frequency": "DAILY",
        },
        "team_already_added": {"existingEntryHazards": [], "existingControls": []},
        "context": {"applicable_hazard_library": lib},
    }


def test_assistant_off_and_shadow_leave_the_answer_unchanged(flag, built):
    s = _assistant_suggestion()
    assert (
        run(
            service.calibrate_assistant_output(
                None, entry_id="e1", suggestion=s, context=_context(), user_id="u"
            )
        )
        is s
    )
    flag["mode"] = "SHADOW"
    out = run(
        service.calibrate_assistant_output(None, entry_id="e1", suggestion=s, context=_context(), user_id="u")
    )
    assert out is s and flag["shadow"][0]["surface"] == "ASSISTANT"


def test_assistant_on_caps_hazards_controls_and_owns_the_residual(flag, built):
    flag["mode"] = "ON"
    out = run(
        service.calibrate_assistant_output(
            None, entry_id="e1", suggestion=_assistant_suggestion(), context=_context(), user_id="u"
        )
    )
    assert len(out["suggested_hazards"]) <= 5
    n_controls = sum(len(b[k]) for b in out["control_suggestions"] for k in b if k.endswith("_controls"))
    assert n_controls <= 5
    rc = out["risk_calibration"]
    assert "reduce to 1" not in rc["residual_likelihood_guidance"]  # the LLM's residual number is gone
    assert "never lower severity" in rc["residual_severity_guidance"]
    assert rc["initial_likelihood_suggestion"]["calibrated_score"] == 3  # L4 with no events -> L3
    assert out["calibration"]["entry"]["residual"]["score"] <= 10


# ── Copilot drafts ──


def _parsed(existing=True):
    return {
        "activityDescription": "Bag-to-bin feeding by crane",
        "routine": "ROUTINE",
        "frequency": "DAILY",
        "initialLikelihood": 4,
        "initialSeverity": 5,
        "initialLikelihoodRationale": "Daily heavy lifts",
        "initialSeverityRationale": "Fatal if dropped",
        "hazards": [{"hazardId": h.key, "consequence": h.consequence} for h in ENTRY1_HAZARDS],
        "existingControls": (
            [
                {"hierarchy": c.hierarchy, "description": c.description, "effectiveness": None}
                for c in ENTRY1_EXISTING
            ]
            if existing
            else []
        ),
        "recommendedControls": [
            {
                "hierarchy": "ADMINISTRATIVE",
                "description": f"Awareness training {i}",
                "estimatedCostBand": "LOW",
            }
            for i in range(8)
        ],
    }


@pytest.fixture
def masters(monkeypatch):
    class R:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

        def scalar(self):
            return 0  # information_schema: no HiraStudy.hiraType column (before the round-3 DDL)

    class DB:
        async def execute(self, stmt):
            return R([(h.key, h.name, h.code, h.category) for h in ENTRY1_HAZARDS])

    return DB()


def test_copilot_draft_off_is_untouched(flag, built, masters):
    study = type("S", (), {"plantId": "plant-1", "id": "s1"})()
    assert (
        run(
            service.calibrate_copilot_draft(
                masters, study=study, area_id=None, parsed=_parsed(), expand=False, user_id="u"
            )
        )
        is None
    )


def test_copilot_draft_on_is_calibrated(flag, built, masters):
    flag["mode"] = "ON"
    study = type("S", (), {"plantId": "plant-1", "id": "s1"})()
    cal = run(
        service.calibrate_copilot_draft(
            masters, study=study, area_id=None, parsed=_parsed(), expand=False, user_id="u"
        )
    )
    p = cal["parsed"]
    assert len(p["hazards"]) <= 5
    assert (p["initialLikelihood"], p["initialSeverity"]) == (3, 5)  # L4 capped: no recorded events
    assert len(p["recommendedControls"]) <= 5
    assert not any(
        "awareness" in r["description"].lower() for r in p["recommendedControls"]
    )  # filler dropped
    assert cal["summary"]["entryResidual"]["level"] != "CRITICAL"
    assert len(cal["hazardRatings"]) == len(p["hazards"])


def test_copilot_draft_on_without_controls_is_inherent_only(flag, built, masters):
    flag["mode"] = "ON"
    study = type("S", (), {"plantId": "plant-1", "id": "s1"})()
    cal = run(
        service.calibrate_copilot_draft(
            masters, study=study, area_id=None, parsed=_parsed(existing=False), expand=False, user_id="u"
        )
    )
    assert cal["summary"]["entryResidual"] is None
    assert cal["summary"]["residualLabel"] == "Inherent only. Existing controls not captured"


# ── round 3 M / N: scope of validity ──


@pytest.mark.parametrize("pack", [None, "APPAREL_TEXTILE", "UNKNOWN_PACK"])
def test_no_pack_or_draft_pack_never_shows_a_residual_band(flag, built, v1, pack):
    flag["mode"], flag["pack"] = "ON", pack
    r = run(service.review(None, "e1", user_id="u"))
    assert r["scope"]["validated"] is False
    assert r["scope"]["banner"] == "Not validated for this industry: ratings are suggestions"
    assert r["entry"]["residual"] is None and r["entry"]["state"] is None
    assert all(h["residual"] is None and h["tolerability"] is None for h in r["hazards"])
    assert all(x["kind"] == "SUGGESTION" and x["residualAfter"] is None for x in r["recommendations"])
    assert r["canSubmit"] and all(f["class"] != "BLOCKER" for f in r["findings"])
    assert r["entry"]["inherent"] is not None  # inherent plus suggested controls only


def test_validated_pack_shows_residual_and_scope(flag, built, v1):
    flag["mode"] = "ON"
    r = run(service.review(None, "e1", user_id="u"))
    assert r["scope"]["validated"] and r["scope"]["banner"] is None
    assert r["scope"]["validatedFor"].startswith("task-based HIRA")
    assert r["entry"]["residual"] is not None


def test_unsupported_hira_type_never_shows_a_residual_band(flag, monkeypatch, v1):
    flag["mode"] = "ON"

    async def build(db, entry_id, cfg):
        e = entry1(ENTRY1_EXISTING)
        e.hira_type = "HAZOP_PHA"
        return {
            "input": e,
            "rule": STD,
            "evidence": eng.Evidence(),
            "plant": rv.PlantData(),
            "entry": None,
            "study": None,
            "schemaReady": False,
        }

    monkeypatch.setattr(service.loader, "build", build)
    r = run(service.review(None, "e1", user_id="u"))
    assert r["scope"]["calibrated"] is False
    assert r["scope"]["banner"] == "Calibrated rules are not applied to this study type."
    assert r["entry"]["residual"] is None and r["recommendations"] == []
