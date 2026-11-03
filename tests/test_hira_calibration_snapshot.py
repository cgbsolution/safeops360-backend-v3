"""Regression guard for refactors: the 25-fixture eval output must equal the
committed snapshot. A behaviour change updates the snapshot in the same commit
(run `python tests/test_hira_calibration_snapshot.py --update`) and says why."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "eval" / "hira_calibration" / "snapshots" / "pre_refactor.json"
_spec = importlib.util.spec_from_file_location(
    "hira_calibration_eval_snap", ROOT / "scripts" / "hira_calibration_eval.py"
)
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)


def current() -> dict:
    fixture = harness.load_fixture()
    rows, _ = harness.run_all(fixture)
    b1 = next(a for a in fixture["activities"] if a["id"] == "BENCH-1")
    sv, _ = harness.run_activity(harness.stored_variant(b1), fixture)
    snap = {"rows": harness._strip_dates(rows), "bench1Stored": harness._strip_dates(sv)}
    return json.loads(json.dumps(snap, sort_keys=True, default=str))


def test_eval_output_matches_snapshot():
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    got = current()
    if got != want:
        diff = [r["id"] for r, w in zip(got["rows"], want["rows"], strict=False) if r != w]
        raise AssertionError(f"eval output changed for: {diff or ['bench1Stored']}")


if __name__ == "__main__" and "--update" in sys.argv:
    SNAPSHOT.write_text(json.dumps(current(), indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"updated {SNAPSHOT}")
