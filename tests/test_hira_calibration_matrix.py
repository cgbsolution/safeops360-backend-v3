"""Matrix adapter (round 3 L) and HIRA type gate (round 3 K)."""

from __future__ import annotations

import pytest

from app.services.hira_calibration import matrix as mx
from app.services.hira_calibration import review_v2 as rv
from app.services.hira_calibration import risk_engine as eng
from app.services.hira_calibration.config import DEFAULT_CONFIG, MatrixRule, with_matrix

CFG = DEFAULT_CONFIG


def test_builtin_5x5_equals_the_threshold_engine():
    for code in ("STD_5X5", "JSL_QSD24_GT"):
        m = mx.builtin(code, CFG)
        assert m.identity
        for l in range(1, 6):
            for s in range(1, 6):
                assert m.rate(l, s) == eng.rate(l, s, CFG)  # exactly the pre-adapter dict


def test_jsl_is_plant_index():
    assert mx.builtin("JSL_QSD24_GT", CFG).scoring_mode == "plantIndex"


def _sq(n: int, bands: dict[tuple[int, int], str] | None = None) -> mx.MatrixConfig:
    cells = {
        (l, s): mx.band_by_thresholds(
            l * s, {"lowMax": max(1, n - 1), "moderateMax": n * 2 - 1, "highMax": n * n - n}
        )
        for l in range(1, n + 1)
        for s in range(1, n + 1)
    }
    cells.update(bands or {})
    return mx.MatrixConfig(code=f"T{n}", likelihood_levels=n, severity_levels=n, cells=cells)


@pytest.mark.parametrize("n", [3, 4, 6])
def test_non_5x5_maps_to_the_normalised_scale_and_back(n):
    m = _sq(n)
    assert not m.identity
    assert m.native_l(1) == 1 and m.native_l(5) == n
    for native in range(1, n + 1):
        back = m.native_l(m.norm_l(native))
        if n <= 5:
            assert back == native  # exact round trip
        else:
            assert back >= native  # 6 levels fold into 5: never rounds a risk down
    r = m.rate(5, 5)
    assert r["native"] == {"likelihood": n, "severity": n, "matrix": f"T{n}", "dimensions": f"{n}x{n}"}
    assert r["level"] == m.cells[(n, n)]


def test_3x3_normalised_steps_round_risk_up():
    m = _sq(3)  # native 1, 2, 3 -> normalised 1, 3, 5
    assert m.lmap == {1: 1, 2: 3, 3: 5}
    assert m.native_l(2) == 2 and m.native_l(4) == 3  # never rounds a risk down


def test_cell_level_override_decides_the_band():
    m = _sq(4, {(2, 2): "LOW"})
    assert m.rate(m.norm_l(2), m.norm_s(2))["level"] == "LOW"
    m2 = _sq(4, {(2, 2): "MODERATE"})
    assert m2.rate(m2.norm_l(2), m2.norm_s(2))["level"] == "MODERATE"


def test_6x6_custom_scoring():
    cells = {(l, s): "LOW" if l * s <= 6 else "HIGH" for l in range(1, 7) for s in range(1, 7)}
    m = mx.MatrixConfig(
        code="C6",
        likelihood_levels=6,
        severity_levels=6,
        cells=cells,
        scoring_mode="custom",
        cell_scores={(6, 6): 99},
    )
    assert m.rate(5, 5)["score"] == 99


def test_from_dict_with_thresholds_and_override():
    m = mx.from_dict(
        {
            "code": "D4",
            "likelihoodLevels": 4,
            "severityLevels": 4,
            "thresholds": {"lowMax": 3, "moderateMax": 6, "highMax": 12},
            "cellOverrides": {"2,2": "LOW"},
        }
    )
    assert m.cells[(2, 2)] == "LOW" and m.cells[(2, 3)] == "MODERATE"


def test_invalid_matrices_rejected():
    with pytest.raises(ValueError):
        mx.MatrixConfig(code="X", likelihood_levels=7, severity_levels=7, cells={})
    with pytest.raises(ValueError):
        mx.MatrixConfig(code="X", likelihood_levels=3, severity_levels=3, cells={(1, 1): "LOW"})


def test_review_reads_bands_through_the_study_matrix():
    m = _sq(3)
    rule = MatrixRule("T3", "FREQUENCY", False, m)
    e = rv.EntryInput(
        "e",
        "#1",
        "Bench work",
        "ROUTINE",
        "DAILY",
        [
            eng.Hazard(
                "h",
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
        controls=[eng.Control("c", "ADMINISTRATIVE", "Knife work instruction")],
        hazard_ratings_stored=True,
    )
    r = rv.evaluate(e, cfg=CFG, rule=rule, evidence=eng.Evidence())
    h = r["hazards"][0]
    assert h["inherent"]["native"]["dimensions"] == "3x3"
    assert h["inherent"]["level"] == m.cells[(m.native_l(3), m.native_s(3))]


def test_with_matrix_is_idempotent():
    c1 = with_matrix(CFG, MatrixRule("STD_5X5", "FREQUENCY", False))
    assert with_matrix(c1, MatrixRule("JSL_QSD24_GT", "PLANT_INDEX", True)) is c1


# ── K: HIRA type gate ──


@pytest.mark.parametrize("kind", ["HAZOP_PHA", "FMEA", "MOC_RISK", "OTHER"])
def test_non_task_types_get_structural_checks_only(kind):
    e = rv.EntryInput(
        "e",
        "#1",
        "Node 3 deviation",
        "ROUTINE",
        "DAILY",
        [eng.Hazard("h", "High pressure", likelihood=5, severity=5, consequence="")],
        5,
        5,
        (5, 5),
        hira_type=kind,
    )
    r = rv.evaluate(e, cfg=CFG, rule=MatrixRule("STD_5X5", "FREQUENCY", False), evidence=eng.Evidence())
    assert r["calibrated"] is False and r["scope"]["banner"] == rv.NOT_CALIBRATED_BANNER
    assert r["entry"]["residual"] is None and r["recommendations"] == [] and r["canSubmit"]
    assert all(f["class"] != rv.BLOCKER for f in r["findings"])
    assert {f["code"] for f in r["findings"]} >= {"STRUCT_NO_CONSEQUENCE"}
