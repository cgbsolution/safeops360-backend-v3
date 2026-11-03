"""Regression guard for the HIRA calibration v2 evaluation harness.

Runs scripts/hira_calibration_eval.py over tests/eval/hira_calibration/activities.json
(25 activities, offline: no DB, network or LLM) and asserts what holds today:
determinism, recommendation caps, no residual without real controls, the two
benchmark regressions, and convergence (no blockers after accepting pass 1).

Convergence (zero loops, no new blockers) is asserted for every activity; a
newly found engine defect can be parked as a strict xfail in KNOWN_* with its
cause, and must be removed once fixed. The percentage targets (Critical
inherent / residual share, average recommendations, single-pass reach) are
reported in tests/eval/hira_calibration/REPORT.md, not asserted here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hira_calibration_eval", ROOT / "scripts" / "hira_calibration_eval.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

FIXTURE = harness.load_fixture()
ACTS = {a["id"]: a for a in FIXTURE["activities"]}
IDS = list(ACTS)

# Engine defects found by this harness (inherent-only dead end, accepting
# proposals raising a new blocker, the 3-step budget spent in a fixed order)
# are fixed; add an activity here (id -> reason) only for a new, understood
# defect, and remove it when fixed (strict xfails fail the run on XPASS).
KNOWN_PASS2_BLOCKERS: dict[str, str] = {}
KNOWN_NEW_BLOCKERS: dict[str, str] = {}


def _params(known: dict[str, str]) -> list:
    return [pytest.param(i, marks=pytest.mark.xfail(strict=True, reason=known[i])) if i in known else i for i in IDS]


@pytest.fixture(scope="module")
def run():
    rows, digests = harness.run_all(FIXTURE)
    return {r["id"]: r for r in rows}, digests


@pytest.fixture(scope="module")
def rows(run):
    return run[0]


def test_fixture_shape():
    assert len(IDS) == 25 and len(set(IDS)) == 25
    assert IDS[:2] == ["BENCH-1", "BENCH-6"]
    for a in FIXTURE["activities"]:
        assert a["matrix"] in FIXTURE["matrices"]
        assert all(h.get("consequence") for h in a["hazards"]), a["id"]
        assert set(a["expected"]) == {"inherentBand", "residualBand"}, a["id"]


def test_rebuilt_entry_reproduces_calibrate_draft(rows):
    """The harness rebuilds the EntryInput calibrate_draft evaluated; evaluating it
    must give calibrate_draft's own review, or pass 2 / 3 would test something else."""
    for i, r in rows.items():
        if ACTS[i]["mode"] == "DRAFT":
            assert r["reconstructionMatchesCalibrateDraft"] is True, i


def test_deterministic_in_process(run):
    _, d1 = run
    _, d2 = harness.run_all(FIXTURE)
    _, d3 = harness.run_all(FIXTURE, reverse=True)
    assert d1 == d2 == d3


def test_deterministic_across_hash_seeds(run):
    _, d1 = run
    env = {**os.environ, "PYTHONHASHSEED": "4242", "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "hira_calibration_eval.py"), "--digest-only"],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-2000:]
    assert json.loads(p.stdout.strip().splitlines()[-1]) == d1


@pytest.mark.parametrize("aid", _params(KNOWN_PASS2_BLOCKERS))
def test_zero_loops_pass2_has_no_blockers(rows, aid):
    r = rows[aid]
    assert r["pass2"]["blockers"] == [], r["pass2"]["blockerDetails"]


@pytest.mark.parametrize("aid", _params(KNOWN_NEW_BLOCKERS))
def test_applying_suggestions_raises_no_new_blockers(rows, aid):
    r = rows[aid]
    assert r["pass2"]["newBlockers"] == [] and r["pass2"]["pass3NewBlockers"] == [], r["pass2"]["blockerDetails"]


@pytest.mark.parametrize("aid", IDS)
def test_recommendation_caps(rows, aid):
    r = rows[aid]
    n = len(r["v2"]["recommendations"])
    assert n <= 5
    if r["v2"]["entryLevelForCap"] in ("LOW", "MODERATE"):
        assert n <= 3
    targets = [t for x in r["v2"]["recommendations"] for t in x["targets"]]
    assert len(targets) == len(set(targets)), "at most one recommendation per hazard"


def test_no_residual_without_real_controls(rows):
    """Empty (or baseline-PPE-only) controls: 'Inherent only', never residual = inherent."""
    checked = 0
    for i, r in rows.items():
        if r["controlsProfile"] in ("none", "baseline only"):
            checked += 1
            assert r["v2"]["residual"] is None, i
            assert r["v2"]["inherentOnly"] is True, i
            assert r["v2"]["residualLabel"] == "Inherent only. Existing controls not captured", i
            assert all(h["residual"] is None for h in r["v2"]["hazards"]), i
    assert checked >= 2
    assert any(not ACTS[i]["existingControls"] for i in rows)  # at least one with no controls at all


def test_bench1_regression(rows):
    for c in harness.bench1_checks(rows["BENCH-1"]):
        assert c["pass"], c
    # the same entry saved the v1 way (one L4 x S5 spread over 7 unrated hazards)
    stored, _ = harness.run_activity(harness.stored_variant(ACTS["BENCH-1"]), FIXTURE)
    for c in harness.bench1_checks(stored):
        assert c["pass"], c


def test_bench6_regression(rows):
    for c in harness.bench6_checks(rows["BENCH-6"]):
        assert c["pass"], c
    assert rows["BENCH-6"]["final"]["label"] == "OVERRIDE"  # terminates at the override, not more controls


def test_v1_baseline_is_what_the_brief_describes(rows):
    b1, b6 = rows["BENCH-1"]["v1"], rows["BENCH-6"]["v1"]
    assert (b1["inherent"]["likelihood"], b1["inherent"]["severity"]) == (4, 5) and b1["recommendations"] == 8
    assert (b6["residual"]["likelihood"], b6["residual"]["severity"]) == (1, 2)  # v1's "L1 x S2" claim
    assert harness.v1_band(2, 2) == "LOW" and harness.v1_band(3, 5) == "CRITICAL"
    for i, a in ACTS.items():
        if not a["existingControls"]:
            assert rows[i]["v1"]["residual"] == rows[i]["v1"]["inherent"]  # v1: residual = initial
