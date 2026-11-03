"""Industry rubric packs (round 3 M) and scope of validity (K, N)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.hira_calibration import review_v2 as rv
from app.services.hira_calibration import risk_engine as eng
from app.services.hira_calibration import rubric_packs as rp
from app.services.hira_calibration.config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1]


def test_every_pack_loads_with_a_valid_status():
    packs = rp.list_packs()
    assert {p["code"] for p in packs} >= {
        "STEEL_HEAVY",
        "APPAREL_TEXTILE",
        "CHEMICAL_PHARMA",
        "WAREHOUSE_LOGISTICS",
    }
    assert all(p["status"] in ("DRAFT", "VALIDATED") for p in packs)
    assert {p["code"] for p in packs if p["status"] == "DRAFT"} >= {
        "APPAREL_TEXTILE",
        "CHEMICAL_PHARMA",
        "WAREHOUSE_LOGISTICS",
    }


def test_steel_pack_is_current_behaviour():
    cfg, meta = rp.apply_pack(DEFAULT_CONFIG, "STEEL_HEAVY")
    assert cfg == DEFAULT_CONFIG and meta["status"] == "VALIDATED"


def test_no_code_path_sets_a_pack_status():
    """Status is set by a person editing the pack file, never by code."""
    files = [
        *(ROOT / "app" / "services" / "hira_calibration").rglob("*.py"),
        ROOT / "app" / "routers" / "hira_calibration.py",
    ]
    for p in files:
        tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "status"
                        and "pack" in ast.unparse(t.value).lower()
                    ):
                        raise AssertionError(f"{p}: assigns a pack status")


def test_draft_pack_rules_apply_through_the_settings():
    cfg, meta = rp.apply_pack(DEFAULT_CONFIG, "WAREHOUSE_LOGISTICS")
    assert meta["status"] == "DRAFT"
    r = eng.severity_rule("FALLING_OBJECT", "Racking collapse from the high bay", cfg)
    assert r["rule"] == "WH_RACK_COLLAPSE_S5" and r["max"] == 5
    cfg2, _ = rp.apply_pack(DEFAULT_CONFIG, "APPAREL_TEXTILE")
    assert eng.severity_rule("SHARP", "Needle puncture at the sewing machine", cfg2)["max"] == 2
    assert eng.is_baseline("PPE", "Apron, hair net", cfg2)
    assert not eng.is_baseline("PPE", "Apron, hair net", DEFAULT_CONFIG)


def test_unknown_pack_is_no_pack():
    cfg, meta = rp.apply_pack(DEFAULT_CONFIG, "NOT_A_PACK")
    assert meta is None and rp.scope_for(meta, "TASK_BASED")["validated"] is False


@pytest.mark.parametrize("code", ["APPAREL_TEXTILE", "CHEMICAL_PHARMA", "WAREHOUSE_LOGISTICS", None])
def test_draft_or_missing_pack_suppresses_residual_claims(code):
    cfg, meta = rp.apply_pack(DEFAULT_CONFIG, code)
    e = rv.EntryInput(
        "e",
        "#1",
        "Dock loading",
        "ROUTINE",
        "DAILY",
        [
            eng.Hazard(
                "h",
                "Forklift strikes a pedestrian",
                code="MECH_MOBILE_EQUIPMENT",
                category="mechanical",
                likelihood=3,
                severity=5,
                consequence="pedestrian run over by a reversing forklift: fatal",
            )
        ],
        3,
        5,
        None,
        controls=[eng.Control("c", "ADMINISTRATIVE", "Dock SOP")],
        hazard_ratings_stored=True,
    )
    from app.services.hira_calibration.config import MatrixRule

    raw = rv.evaluate(e, cfg=cfg, rule=MatrixRule("STD_5X5", "FREQUENCY", False), evidence=eng.Evidence())
    out = rp.apply_scope(raw, rp.scope_for(meta, "TASK_BASED"))
    assert out["scope"]["banner"] == rp.NOT_VALIDATED_BANNER
    assert out["entry"]["residual"] is None and out["controlsJustify"] == []
    assert all(h["residual"] is None and h["tolerability"] is None for h in out["hazards"])
    assert all(r["kind"] == "SUGGESTION" for r in out["recommendations"])
    assert not rv.blocker_codes(out)
    assert out["suggestedDefaults"]["alarpRecord"] is None and out["alarpDraft"] is None
    assert not {f["code"] for f in out["findings"]} & rp._TOLERABILITY_CODES


def test_validated_for_lists_only_validated_packs():
    assert rp.validated_for() == "task-based HIRA, Steel and heavy industry"
