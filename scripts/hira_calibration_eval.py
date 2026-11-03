"""Evaluation harness for HIRA calibration v2 (offline: no DB, no network, no LLM).

For every activity in tests/eval/hira_calibration/activities.json:

  a. pass 1   v2 review: assistant_v2.calibrate_draft() for Copilot drafts, or
              review_v2.evaluate() for a stored team rating (e.g. BENCH-6).
  b. pass 2   apply_suggestions(entry, pass1) (planned recommendations + accepted
              existing controls + suggested defaults), re-evaluate: blockers must
              not grow (target: none at all). A third review after applying pass 2
              checks that nothing new appears either.
  c. single   apply_suggestions(entry, pass1, implement=True), re-evaluate: each
     pass     hazard's tolerability state. The entry "reaches" when every hazard
              is ACCEPTABLE / TOLERABLE_ALARP or on the OVERRIDE route.
  d. determinism: everything is run twice (and in reverse order) in-process, and
              once more in a subprocess with a different PYTHONHASHSEED.
  e. v1 numbers: one entry rating (the max proposed L x S), the v1 residual model
              (app.routers.hira._suggest_residual_scores; residual = initial when no
              controls), the v1 band (L2 x S2 = LOW) and the v1 recommendation count
              (every control the LLM proposed).

Pass 2 / 3 model the review of the SAVED entry (service.evaluate_entry), which
calls evaluate() without the LLM's candidate controls.

Writes tests/eval/hira_calibration/REPORT.md and results.json.

    cd safeops_360_bakend && python scripts/hira_calibration_eval.py
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.hira_calibration import assistant_v2 as av  # noqa: E402
from app.services.hira_calibration import recommend as rec  # noqa: E402
from app.services.hira_calibration import review_v2 as rv  # noqa: E402
from app.services.hira_calibration import risk_engine as eng  # noqa: E402
from app.services.hira_calibration.config import DEFAULT_CONFIG, ENGINE_VERSION, MatrixRule  # noqa: E402

EVAL_DIR = ROOT / "tests" / "eval" / "hira_calibration"
ACTIVITIES = EVAL_DIR / "activities.json"
REPORT = EVAL_DIR / "REPORT.md"
RESULTS = EVAL_DIR / "results.json"

CFG = DEFAULT_CONFIG
REACHED = (eng.ACCEPTABLE, eng.TOLERABLE_ALARP)

# Targets (from the calibration brief).
T_CRIT_INHERENT = 0.15
T_CRIT_RESIDUAL = 0.05
T_AVG_RECS = 3.0
T_MAX_RECS = 5
T_REACH = 0.80


# ─── fixtures -> engine inputs ──────────────────────────────────────────


def load_fixture(path: Path = ACTIVITIES) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rule_for(fixture: dict[str, Any], act: dict[str, Any]) -> MatrixRule:
    m = fixture["matrices"][act["matrix"]]
    return MatrixRule(act["matrix"], m["likelihoodScale"], bool(m["moderateNotAcceptable"]))


def evidence_for(act: dict[str, Any]) -> eng.Evidence:
    by = {
        fam: {
            "window": int(v.get("window", 0)),
            "12m": int(v.get("12m", 0)),
            "failures": list(v.get("failures") or []),
            "items": list(v.get("items") or []),
        }
        for fam, v in sorted(((act.get("evidence") or {}).get("byFamily") or {}).items())
    }
    return eng.Evidence(
        events_window=sum(v["window"] for v in by.values()),
        events_12m=sum(v["12m"] for v in by.values()),
        control_failures=[f for v in by.values() for f in v["failures"]],
        items=[i for v in by.values() for i in v["items"]],
        by_family=by,
    )


def plant_for(act: dict[str, Any]) -> rv.PlantData:
    p = act.get("plantData") or {}
    return rv.PlantData(
        existing_control_proposals=copy.deepcopy(p.get("existingControlProposals") or []),
        open_capas=copy.deepcopy(p.get("openCapas") or []),
        sibling_recommended=copy.deepcopy(p.get("siblingRecommended") or []),
        statutory_overdue=copy.deepcopy(p.get("statutoryOverdue") or {}),
    )


def draft_for(act: dict[str, Any]) -> dict[str, Any]:
    return {
        "entryId": act["id"],
        "label": act["id"],
        "activity": act["activity"],
        "routine": act.get("routine"),
        "frequency": act.get("frequency"),
        "hazards": copy.deepcopy(act["hazards"]),
        "existingControls": copy.deepcopy(act.get("existingControls") or []),
        "recommendedControls": copy.deepcopy(act.get("recommendedControls") or []),
        "reviewDateSet": bool(act.get("reviewDateSet")),
        "eliminationConsidered": bool(act.get("eliminationConsidered")),
        "statutoryCurrent": act.get("statutoryCurrent"),
    }


def entry_from_draft(
    draft: dict[str, Any], calibrated: dict[str, Any]
) -> tuple[rv.EntryInput, list[rec.Candidate]]:
    """The EntryInput (and LLM candidates) calibrate_draft() evaluated, rebuilt
    from its output. Mirrors assistant_v2.calibrate_draft line for line; run()
    checks that evaluating it reproduces calibrate_draft's review exactly."""
    routine = (draft.get("routine") or "ROUTINE").upper()
    src = {
        str(h.get("key") or f"h{i}"): h
        for i, h in enumerate(h for h in draft.get("hazards") or [] if h.get("key") or h.get("name"))
    }
    hazards = []
    for h in calibrated["hazards"]:  # the kept hazards, in calibrate_draft's ranked order
        d = src[h["key"]]
        hazards.append(
            eng.Hazard(
                key=h["key"],
                name=d.get("name") or "Hazard",
                code=d.get("code"),
                category=d.get("category"),
                consequence=d.get("consequence"),
                likelihood=h["inherent"]["likelihood"],
                severity=h["inherent"]["severity"],
            )
        )
    controls = [
        eng.Control(
            key=f"x{i}",
            hierarchy=(c.get("hierarchy") or "").upper(),
            description=c.get("description") or "",
            effectiveness=c.get("effectiveness"),
            verification_method=c.get("verificationMethod"),
            hazard_keys=tuple(c["hazardKeys"]) if c.get("hazardKeys") else None,
        )
        for i, c in enumerate(draft.get("existingControls") or [])
        if c.get("description")
    ]
    proposed = [
        {
            "hierarchy": (c.get("hierarchy") or "ADMINISTRATIVE").upper(),
            "description": (c.get("description") or "").strip(),
            "costBand": (c.get("costBand") or c.get("estimatedCostBand") or "MEDIUM").upper(),
        }
        for c in draft.get("recommendedControls") or []
        if (c.get("description") or "").strip()
    ]
    entry = rv.EntryInput(
        entry_id=draft.get("entryId") or "draft",
        label=draft.get("label") or "draft",
        activity=draft.get("activity") or "",
        routine=routine,
        frequency=draft.get("frequency"),
        hazards=hazards,
        entry_l=None,
        entry_s=None,
        saved_residual=None,
        controls=controls,
        candidate_controls=proposed,
        statutory_current=draft.get("statutoryCurrent"),
        hazard_ratings_stored=True,
        review_date_set=bool(draft.get("reviewDateSet")),
        elimination_considered=bool(draft.get("eliminationConsidered")),
    )
    return entry, None


def stored_entry(act: dict[str, Any]) -> rv.EntryInput:
    """A team's saved entry: one entry rating, hazards rated or not."""
    er = act["entryRating"]
    hazards = [
        eng.Hazard(
            key=h["key"],
            name=h["name"],
            code=h.get("code"),
            category=h.get("category"),
            consequence=h.get("consequence"),
            likelihood=h.get("likelihood"),
            severity=h.get("severity"),
        )
        for h in act["hazards"]
    ]
    controls = [
        eng.Control(
            key=f"c{i + 1}",
            hierarchy=(c.get("hierarchy") or "").upper(),
            description=c["description"],
            effectiveness=c.get("effectiveness"),
            verification_method=c.get("verificationMethod"),
            hazard_keys=tuple(c["hazardKeys"]) if c.get("hazardKeys") else None,
        )
        for i, c in enumerate(act.get("existingControls") or [])
    ]
    saved = tuple(act["savedResidual"]) if act.get("savedResidual") else None
    # The entry's own proposed (not yet planned) controls are candidates, exactly
    # as a draft's are (round 2 E: one engine path for stored entries and drafts).
    proposed = [
        {
            "hierarchy": (c.get("hierarchy") or "ADMINISTRATIVE").upper(),
            "description": c["description"],
            "costBand": (c.get("costBand") or c.get("estimatedCostBand") or "MEDIUM").upper(),
        }
        for c in act.get("recommendedControls") or []
        if c.get("description")
    ]
    return rv.EntryInput(
        act["id"],
        act["id"],
        act["activity"],
        act.get("routine"),
        act.get("frequency"),
        hazards,
        er["likelihood"],
        er["severity"],
        saved,
        rating_basis=act.get("ratingBasis") or "INHERENT",
        controls=controls,
        hazard_ratings_stored=all(h.likelihood and h.severity for h in hazards),
        review_date_set=bool(act.get("reviewDateSet")),
        elimination_considered=bool(act.get("eliminationConsidered")),
        candidate_controls=proposed,
        statutory_current=act.get("statutoryCurrent"),
    )


def saved_draft(act: dict[str, Any], calibrated: dict[str, Any]) -> dict[str, Any]:
    """The calibrated draft as it is saved: the kept hazards with their own
    ratings, the same controls and proposed recommendations (for the parity
    check: the stored-entry path must give the same result as the draft)."""
    out = copy.deepcopy(act)
    out["id"], out["mode"] = act["id"], "STORED"
    kept = {h["key"]: h for h in calibrated["hazards"]}
    src = {h["key"]: h for h in act["hazards"]}
    out["hazards"] = [
        {
            **src[k],
            "likelihood": kept[k]["inherent"]["likelihood"],
            "severity": kept[k]["inherent"]["severity"],
        }
        for k in kept
    ]
    drv = calibrated["entry"]["inherent"] or {"likelihood": None, "severity": None}
    out["entryRating"] = {"likelihood": drv["likelihood"], "severity": drv["severity"]}
    out["savedResidual"] = None
    return out


def stored_variant(act: dict[str, Any]) -> dict[str, Any]:
    """A drafted activity saved the v1 way: one entry rating (the max proposal)
    spread over unrated hazards, saved residual = that rating. For BENCH-1 this
    is exactly tests/test_hira_calibration_engine.entry1(ENTRY1_EXISTING)."""
    l, s = v1_entry_rating(act)
    out = copy.deepcopy(act)
    out["id"] = f"{act['id']}/stored"
    out["mode"] = "STORED"
    out["entryRating"] = {"likelihood": l, "severity": s}
    out["savedResidual"] = [l, s]
    for h in out["hazards"]:
        h.pop("likelihood", None)
        h.pop("severity", None)
    return out


# ─── v1 (old model) ─────────────────────────────────────────────────────


def v1_band(l: int, s: int) -> str:
    """v1 STD matrix: same thresholds as v2 except the L2 x S2 cell was LOW."""
    sc = l * s
    if sc <= 3 or (l, s) == (2, 2):
        return "LOW"
    if sc <= 6:
        return "MODERATE"
    if sc <= 12:
        return "HIGH"
    return "CRITICAL"


def v1_rate(l: int, s: int) -> dict[str, Any]:
    return {"likelihood": l, "severity": s, "score": l * s, "level": v1_band(l, s)}


def v1_entry_rating(act: dict[str, Any]) -> tuple[int, int]:
    if act.get("entryRating"):
        return act["entryRating"]["likelihood"], act["entryRating"]["severity"]
    best = max(
        act["hazards"], key=lambda h: (h["likelihood"] * h["severity"], h["severity"], h["likelihood"])
    )
    return best["likelihood"], best["severity"]


def v1_numbers(act: dict[str, Any]) -> dict[str, Any]:
    from app.routers.hira import _suggest_residual_scores  # noqa: PLC0415 - heavy import, only when needed

    l, s = v1_entry_rating(act)
    ctl = act.get("existingControls") or []
    if ctl:
        rl, rs = _suggest_residual_scores(
            l,
            s,
            [
                SimpleNamespace(hierarchy=c.get("hierarchy"), effectiveness=c.get("effectiveness"))
                for c in ctl
            ],
        )
        how = "v1 control model"
    else:
        rl, rs, how = l, s, "residual = initial (no controls)"
    return {
        "inherent": v1_rate(l, s),
        "residual": v1_rate(rl, rs),
        "residualSource": how,
        "recommendations": len(act.get("recommendedControls") or []),
    }


# ─── one activity ───────────────────────────────────────────────────────


def controls_profile(act: dict[str, Any]) -> str:
    ctl = act.get("existingControls") or []
    if not ctl:
        return "none"
    if all(eng.is_baseline((c.get("hierarchy") or "").upper(), c.get("description"), CFG) for c in ctl):
        return "baseline only"
    return "real"


def hazard_states(review: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for h in review["hazards"]:
        t = h.get("tolerability")
        if t is None:
            low = bool(h.get("inherent")) and h["inherent"]["level"] == "LOW"
            out.append(
                {
                    "key": h["key"],
                    "name": h["name"],
                    "state": eng.ACCEPTABLE if low else "INHERENT_ONLY",
                    "route": None,
                    "missing": [],
                    "residual": None,
                    "note": "inherent LOW, no controls needed"
                    if low
                    else "no controls: residual not assessed",
                }
            )
        else:
            out.append(
                {
                    "key": h["key"],
                    "name": h["name"],
                    "state": t["state"],
                    "route": t["route"],
                    "missing": t["missing"],
                    "residual": h["residual"],
                }
            )
    return out


def final_state(review: dict[str, Any]) -> dict[str, Any]:
    states = hazard_states(review)
    override = any(s["route"] == "OVERRIDE" for s in states)
    reached = bool(states) and all(s["state"] in REACHED or s["route"] == "OVERRIDE" for s in states)
    if not reached:
        open_ = [s for s in states if not (s["state"] in REACHED or s["route"] == "OVERRIDE")]
        label = "NOT_REACHED (" + ", ".join(sorted({s["route"] or s["state"] for s in open_})) + ")"
    elif override:
        label = "OVERRIDE"
    elif all(s["state"] == eng.ACCEPTABLE for s in states):
        label = "ACCEPTABLE"
    else:
        label = "TOLERABLE_ALARP"
    return {
        "label": label,
        "reached": reached,
        "override": override,
        "status": review["entry"]["status"],
        "entryResidual": review["entry"]["residual"],
        "hazards": states,
    }


def _rec_summary(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "hierarchy": r["hierarchy"],
        "description": r["description"],
        "targets": [t["key"] for t in r["targetHazards"]],
        "costBand": r["costBand"],
        "kind": r["kind"],
        "source": r["source"],
        "studyLevel": r["studyLevel"],
        "residualAfter": r["residualAfter"],
    }


def _expected_match(expected: str | None, actual: str) -> bool | None:
    if not expected or expected.startswith("TODO"):
        return None
    return expected == actual


def run_activity(act: dict[str, Any], fixture: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(summary, raw engine outputs) for one activity."""
    rule, ev, plant = rule_for(fixture, act), evidence_for(act), plant_for(act)
    kw = {"cfg": CFG, "rule": rule, "evidence": ev}
    calibrated = None
    reconstruction_ok = None
    if act["mode"] == "DRAFT":
        draft = draft_for(act)
        calibrated = av.calibrate_draft(draft, plant=copy.deepcopy(plant), **kw)
        entry, extra = entry_from_draft(draft, calibrated)
        check = rv.evaluate(copy.deepcopy(entry), plant=copy.deepcopy(plant), extra_candidates=extra, **kw)
        reconstruction_ok = check == calibrated["review"]
        r1 = calibrated["review"]
        # round 2 E: the same content through the stored-entry path
        st = stored_entry(saved_draft(act, calibrated))
        ps = rv.evaluate(copy.deepcopy(st), plant=copy.deepcopy(plant), **kw)
        p3 = rv.evaluate(rv.apply_suggestions(st, ps, implement=True), plant=copy.deepcopy(plant), **kw)
        parity = {
            "recommendations": [r["id"] for r in ps["recommendations"]]
            == [r["id"] for r in r1["recommendations"]],
            "finalState": None,  # filled below once the draft's single pass is known
            "_storedFinal": final_state(p3)["label"],
        }
    else:
        entry = stored_entry(act)
        r1 = rv.evaluate(copy.deepcopy(entry), plant=copy.deepcopy(plant), **kw)
        parity = None

    # b. convergence: accept everything proposed, review the saved entry again
    e2 = rv.apply_suggestions(entry, r1)
    r2 = rv.evaluate(copy.deepcopy(e2), plant=copy.deepcopy(plant), **kw)
    r2b = rv.evaluate(rv.apply_suggestions(e2, r2), plant=copy.deepcopy(plant), **kw)
    # c. single pass with the recommendations implemented and verified
    e3 = rv.apply_suggestions(entry, r1, implement=True)
    r3 = rv.evaluate(copy.deepcopy(e3), plant=copy.deepcopy(plant), **kw)

    b1, b2, b2b = rv.blocker_codes(r1), rv.blocker_codes(r2), rv.blocker_codes(r2b)
    inh, res = r1["entry"]["inherent"], r1["entry"]["residual"]
    level_for_cap = (res or inh or {}).get("level")
    fin = final_state(r3)
    if parity is not None:
        parity["finalState"] = parity.pop("_storedFinal") == fin["label"]
    crit_res = [h for h in r1["hazards"] if h.get("residual") and h["residual"]["level"] == "CRITICAL"]
    exp = act.get("expected") or {}
    residual_label = res["level"] if res else "NOT_ASSESSED"
    summary = {
        "id": act["id"],
        "title": act["title"],
        "mode": act["mode"],
        "matrix": act["matrix"],
        "routine": act.get("routine"),
        "frequency": act.get("frequency"),
        "controlsProfile": controls_profile(act),
        "evidenceFamilies": sorted(ev.by_family) if ev.by_family else [],
        "reconstructionMatchesCalibrateDraft": reconstruction_ok,
        "parity": parity,
        "v1": v1_numbers(act),
        "v2": {
            "inherent": inh,
            "residual": res,
            "residualLabel": r1["entry"]["residualLabel"],
            "inherentOnly": r1["entry"]["inherentOnly"],
            "status": r1["entry"]["status"],
            "entryLevelForCap": level_for_cap,
            "recommendationCap": r1["recommendationCap"],
            "recommendations": [_rec_summary(r) for r in r1["recommendations"]],
            "linkedCapas": [c.get("number") or c.get("id") for c in r1["linkedCapas"]],
            "existingControlProposals": len(r1["existingControlProposals"]),
            "droppedHazards": [d["name"] for d in calibrated["droppedHazards"]] if calibrated else [],
            "blockers": sorted(b1),
            "findings": [f["code"] for f in r1["findings"]],
            "controlsJustify": r1["controlsJustify"],
            "criticalResidualHazards": [
                {
                    "key": h["key"],
                    "name": h["name"],
                    "family": h["family"],
                    "rating": h["residual"],
                    "evidence": ev.for_family(h["family"]).has_any,
                }
                for h in crit_res
            ],
            "hazards": [
                {
                    "key": h["key"],
                    "name": h["name"],
                    "code": h.get("code"),
                    "family": h["family"],
                    "proposed": next(
                        (
                            {"likelihood": x.get("likelihood"), "severity": x.get("severity")}
                            for x in act["hazards"]
                            if x["key"] == h["key"]
                        ),
                        None,
                    ),
                    "likelihoodReason": h["likelihood"]["reason"] if h.get("likelihood") else None,
                    "severityFinding": ((h.get("severity") or {}).get("finding") or {}).get("kind"),
                    "inherent": h["inherent"],
                    "residual": h["residual"],
                    "tolerability": h["tolerability"]
                    and {k: h["tolerability"][k] for k in ("state", "route", "missing")},
                }
                for h in r1["hazards"]
            ],
        },
        "pass2": {
            "blockers": sorted(b2),
            "newBlockers": sorted(b2 - b1),
            "recommendations": len(r2["recommendations"]),
            "blockerDetails": [
                f"{f['code']}: {f['detail']}" for f in r2["findings"] if f["class"] == rv.BLOCKER
            ],
            "status": r2["entry"]["status"],
            "pass3Blockers": sorted(b2b),
            "pass3NewBlockers": sorted(b2b - b2),
        },
        "final": fin,
        "expected": exp,
        "expectedMatch": {
            "inherent": _expected_match(exp.get("inherentBand"), inh["level"] if inh else "NONE"),
            "residual": _expected_match(exp.get("residualBand"), residual_label),
        },
    }
    raw = {"calibrated": calibrated, "pass1": r1, "pass2": r2, "pass2b": r2b, "implemented": r3}
    return summary, raw


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def run_all(fixture: dict[str, Any], *, reverse: bool = False) -> tuple[list[dict[str, Any]], dict[str, str]]:
    acts = list(reversed(fixture["activities"])) if reverse else fixture["activities"]
    summaries, digests = {}, {}
    for act in acts:
        s, raw = run_activity(act, fixture)
        summaries[act["id"]] = s
        digests[act["id"]] = _digest({"summary": s, "raw": raw})
    order = [a["id"] for a in fixture["activities"]]
    return [summaries[i] for i in order], {i: digests[i] for i in order}


# ─── targets and benchmarks ─────────────────────────────────────────────


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100.0 * n / d:.0f}%)" if d else "0/0"


def targets(rows: list[dict[str, Any]], deterministic: bool, det_detail: str) -> list[dict[str, Any]]:
    n = len(rows)
    crit_inh = [r["id"] for r in rows if (r["v2"]["inherent"] or {}).get("level") == "CRITICAL"]
    crit_res = [r["id"] for r in rows if (r["v2"]["residual"] or {}).get("level") == "CRITICAL"]
    crit_res_no_ev = [
        r["id"] for r in rows if any(not h["evidence"] for h in r["v2"]["criticalResidualHazards"])
    ]
    recs = [len(r["v2"]["recommendations"]) for r in rows]
    over5 = [r["id"] for r in rows if len(r["v2"]["recommendations"]) > T_MAX_RECS]
    reached = [r["id"] for r in rows if r["final"]["reached"]]
    override = [r["id"] for r in rows if r["final"]["reached"] and r["final"]["override"]]
    loops = [r["id"] for r in rows if r["pass2"]["blockers"]]
    new_b = [r["id"] for r in rows if r["pass2"]["newBlockers"] or r["pass2"]["pass3NewBlockers"]]
    more_recs = [r["id"] for r in rows if r["pass2"]["recommendations"]]
    avg = sum(recs) / n if n else 0.0
    out = [
        {
            "target": "Critical inherent <= 15% of entries",
            "value": _pct(len(crit_inh), n),
            "pass": len(crit_inh) / n <= T_CRIT_INHERENT,
            "offenders": crit_inh,
        },
        {
            "target": "Critical residual <= 5% of entries, and only with evidence",
            "value": f"{_pct(len(crit_res), n)}; without evidence: {len(crit_res_no_ev)}",
            "pass": len(crit_res) / n <= T_CRIT_RESIDUAL and not crit_res_no_ev,
            "offenders": crit_res,
        },
        {
            "target": "Average recommendations per entry <= 3.0",
            "value": f"{avg:.2f} (total {sum(recs)})",
            "pass": avg <= T_AVG_RECS,
            "offenders": [],
        },
        {
            "target": "Never more than 5 recommendations on an entry",
            "value": f"max {max(recs) if recs else 0}",
            "pass": not over5,
            "offenders": over5,
        },
        {
            "target": ">= 80% reach ACCEPTABLE / TOLERABLE_ALARP (or the OVERRIDE route) in one pass",
            "value": f"{_pct(len(reached), n)}; of which OVERRIDE route: {len(override)}",
            "pass": len(reached) / n >= T_REACH,
            "offenders": [r["id"] for r in rows if not r["final"]["reached"]],
        },
        {
            "target": "Zero loops: pass 2 has 0 blockers",
            "value": f"{len(loops)} entr{'y' if len(loops) == 1 else 'ies'} with pass-2 blockers",
            "pass": not loops,
            "offenders": loops,
        },
        {
            "target": "No new blockers on pass 2 or pass 3",
            "value": f"{len(new_b)} entries",
            "pass": not new_b,
            "offenders": new_b,
        },
        {"target": "100% deterministic", "value": det_detail, "pass": deterministic, "offenders": []},
        {
            "target": "(info) pass 2 still recommends more controls",
            "value": f"{len(more_recs)} entries",
            "pass": None,
            "offenders": more_recs,
        },
    ]
    return out


def _lik_note(h: dict[str, Any]) -> str:
    """How the likelihood got here. For drafts the review sees the value
    calibrate_draft already calibrated, so compare with the LLM's proposal."""
    reason = (h.get("likelihoodReason") or "").lower()
    proposed = (h.get("proposed") or {}).get("likelihood")
    final = (h.get("inherent") or {}).get("likelihood")
    if proposed and final and final < proposed:
        return f"capped from L{proposed}, no evidence"
    if "plant index" in reason:
        return "plant-index pass-through"
    if "supported" in reason:
        return "evidence"
    if "capped" in reason:
        return "capped"
    return "as proposed"


def _sev_note(h: dict[str, Any], rubric: dict[str, Any]) -> str:
    rule = rubric.get(h["family"]) or {}
    proposed = (h.get("proposed") or {}).get("severity")
    final = (h.get("inherent") or {}).get("severity")
    if h.get("severityFinding") == "OVERSTATED" or (proposed and final and final < proposed):
        return f"clamped to rubric max from S{proposed}" if proposed else "overstated"
    if h.get("severityFinding") == "UNDERSTATED" or (proposed and final and final > proposed):
        return f"raised to rubric min from S{proposed}" if proposed else "understated (team value kept)"
    if not rule:
        return f"no rubric for {h['family']}"
    if "max" not in rule:
        return f"rubric {h['family']} has no max"
    return "within rubric"


def why(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Computed explanations for each target's offenders (what drives the number)."""
    rubric = CFG["severity"]["rubric"]
    out: dict[str, list[str]] = {"criticalInherent": [], "criticalResidual": [], "reach": [], "loops": []}
    for r in rows:
        hz = r["v2"]["hazards"]
        crit = [h for h in hz if h["inherent"] and h["inherent"]["level"] == "CRITICAL"]
        if crit:
            out["criticalInherent"].append(
                f"{r['id']}: "
                + "; ".join(
                    f"{h['name']} [{h['family']}] proposed L{(h['proposed'] or {}).get('likelihood')}xS"
                    f"{(h['proposed'] or {}).get('severity')} -> L{h['inherent']['likelihood']} ({_lik_note(h)}) x "
                    f"S{h['inherent']['severity']} ({_sev_note(h, rubric)})"
                    for h in crit[:3]
                )
            )
        for h in r["v2"]["criticalResidualHazards"]:
            src = next(x for x in hz if x["key"] == h["key"])
            out["criticalResidual"].append(
                f"{r['id']}: {h['name']} [{h['family']}] residual L{h['rating']['likelihood']}xS{h['rating']['severity']}"
                f" ({'with' if h['evidence'] else 'NO'} evidence; L {_lik_note(src)}, S {_sev_note(src, rubric)})"
            )
        if not r["final"]["reached"]:
            open_ = [
                h for h in r["final"]["hazards"] if not (h["state"] in REACHED or h["route"] == "OVERRIDE")
            ]
            out["reach"].append(
                f"{r['id']}: "
                + "; ".join(
                    f"{h['name']} {_r(h['residual']) if h['residual'] else ''} {h['route'] or h['state']}"
                    + (f" missing {', '.join(h['missing'])}" if h["missing"] else "")
                    for h in open_[:4]
                )
            )
        if r["pass2"]["blockers"]:
            out["loops"].append(
                f"{r['id']}: pass-1 {r['v2']['blockers'] or '[]'} / "
                f"{len(r['v2']['recommendations'])} recs -> pass-2 "
                + " | ".join(d[:160] for d in r["pass2"]["blockerDetails"])
                + f" (pass-2 recommends {r['pass2']['recommendations']} more)"
            )
    return out


def bench1_checks(row: dict[str, Any]) -> list[dict[str, Any]]:
    res = row["v2"]["residual"]
    recs = row["v2"]["recommendations"]
    soft = {"h5", "h7"}  # sharp edge + dust
    soft_n = sum(1 for r in recs if set(r["targets"]) & soft)
    return [
        {
            "check": "entry residual <= 10 and not CRITICAL",
            "value": eng.rating_text(res) if res else "not assessed",
            "pass": bool(res) and res["score"] <= 10 and res["level"] != "CRITICAL",
        },
        {"check": "recommendations <= 5", "value": str(len(recs)), "pass": len(recs) <= 5},
        {
            "check": "dust + sharp-edge hazards together get <= 2 recommendations",
            "value": str(soft_n),
            "pass": soft_n <= 2,
        },
    ]


def bench6_checks(row: dict[str, Any]) -> list[dict[str, Any]]:
    h = row["v2"]["hazards"][0]
    recs = row["v2"]["recommendations"]
    res = h["residual"]
    return [
        {
            "check": "no 'L1 x S2' claim: residual stays L2 x S3",
            "value": eng.rating_text(res) if res else "not assessed",
            "pass": bool(res)
            and (res["likelihood"], res["severity"]) == (2, 3)
            and not any("L1 x S2" in j for j in row["v2"]["controlsJustify"]),
        },
        {
            "check": "SEVERITY_UNDERSTATED ADVISORY present",
            "value": "present" if "SEVERITY_UNDERSTATED" in row["v2"]["findings"] else "missing",
            "pass": "SEVERITY_UNDERSTATED" in row["v2"]["findings"]
            and "SEVERITY_UNDERSTATED" not in row["v2"]["blockers"],
        },
        {
            "check": "at most 1 recommendation and it is ENGINEERING",
            "value": ", ".join(f"{r['hierarchy']}: {r['description'][:50]}" for r in recs) or "none",
            "pass": len(recs) <= 1 and all(r["hierarchy"] == "ENGINEERING" for r in recs),
        },
    ]


# ─── report ─────────────────────────────────────────────────────────────


def _r(r: dict[str, Any] | None) -> str:
    if not r:
        return "Inherent only"
    return f"{r['level']} {r['score']} (L{r['likelihood']}xS{r['severity']})"


def _pf(p: bool | None) -> str:
    return "info" if p is None else ("PASS" if p else "FAIL")


def render_report(out: dict[str, Any]) -> str:
    rows = out["activities"]
    L: list[str] = []
    a = L.append
    a("# HIRA calibration v2: evaluation report")
    a("")
    a(
        f"Generated by `scripts/hira_calibration_eval.py` from `tests/eval/hira_calibration/activities.json` "
        f"({len(rows)} activities). Engine {out['engineVersion']}, DEFAULT_CONFIG, offline (no DB, network or LLM)."
    )
    a("")
    a(
        "How each entry is run: pass 1 = `calibrate_draft` (Copilot drafts) or `review_v2.evaluate` (stored team "
        "rating); pass 2 = `apply_suggestions(entry, pass1)` then review of the saved entry; single pass = "
        "`apply_suggestions(entry, pass1, implement=True)` then review. v1 = one entry rating (max proposed L x S), "
        "`_suggest_residual_scores` with the existing controls (residual = initial without controls), v1 band "
        "(L2xS2 = LOW), v1 recommendations = every control the LLM proposed."
    )
    a("")
    a("## Targets")
    a("")
    a("| Target | Result | Status |")
    a("|---|---|---|")
    for t in out["targets"]:
        off = f" — {', '.join(t['offenders'])}" if t["offenders"] else ""
        a(f"| {t['target']} | {t['value']}{off} | **{_pf(t['pass'])}** |")
    a("")
    a("### What drives the failing targets (computed from this run)")
    a("")
    titles = {
        "criticalInherent": "CRITICAL inherent hazards (L: capped / evidence / plant-index pass-through / as "
        "proposed; S: rubric effect)",
        "criticalResidual": "CRITICAL residual hazards",
        "reach": "Hazards still open after implementing the pass-1 recommendations once",
        "loops": "Pass-2 blockers (after accepting everything pass 1 proposed)",
    }
    for key, title in titles.items():
        items = out["why"][key]
        if not items:
            continue
        a(f"**{title}**")
        a("")
        for x in items:
            a(f"- {x}")
        a("")
    a("## Benchmark regressions")
    a("")
    a("| Benchmark | Check | Value | Status |")
    a("|---|---|---|---|")
    for b in out["benchmarks"]:
        for c in b["checks"]:
            a(f"| {b['id']} | {c['check']} | {c['value']} | **{_pf(c['pass'])}** |")
    a("")
    a("### Benchmarks before (v1) and after (v2)")
    a("")
    a(
        "| Benchmark | Inherent v1 | Inherent v2 | Residual v1 | Residual v2 | Recs v1 | Recs v2 | Pass-2 blockers | "
        "After implementing |"
    )
    a("|---|---|---|---|---|---|---|---|---|")
    for b in out["benchmarks"]:
        r = b["row"]
        a(
            f"| {r['id']} | {_r(r['v1']['inherent'])} | {_r(r['v2']['inherent'])} | {_r(r['v1']['residual'])} | "
            f"{_r(r['v2']['residual'])} | {r['v1']['recommendations']} | {len(r['v2']['recommendations'])} | "
            f"{', '.join(r['pass2']['blockers']) or 'none'} | {r['final']['label']} |"
        )
    a("")
    a("## Per activity")
    a("")
    a(
        "Final state = after implementing the pass-1 recommendations once (single pass). Expected = HSE judgement "
        "recorded in the fixture before the run (TODO = not agreed yet)."
    )
    a("")
    a(
        "| ID | Activity | Matrix | Controls | Evidence | Inherent v1 | Inherent v2 | Residual v1 | Residual v2 | "
        "Recs v1 | Recs v2 | Pass-1 blockers | Pass-2 blockers | Final state | Expected inh / res |"
    )
    a("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        exp = r["expected"]
        em = r["expectedMatch"]

        def mark(v: bool | None) -> str:
            return "" if v is None else (" ✓" if v else " ✗")

        a(
            f"| {r['id']} | {r['title']} | {r['matrix']} | {r['controlsProfile']} | "
            f"{', '.join(r['evidenceFamilies']) or '-'} | {_r(r['v1']['inherent'])} | {_r(r['v2']['inherent'])} | "
            f"{_r(r['v1']['residual'])} | {_r(r['v2']['residual'])} | {r['v1']['recommendations']} | "
            f"{len(r['v2']['recommendations'])} | {', '.join(r['v2']['blockers']) or '-'} | "
            f"{', '.join(r['pass2']['blockers']) or '-'} | {r['final']['label']} | "
            f"{exp.get('inherentBand', '-')}{mark(em['inherent'])} / {exp.get('residualBand', '-')}{mark(em['residual'])} |"
        )
    a("")
    s = out["summary"]
    a(
        f"Totals: v1 recommendations {s['v1Recs']} -> v2 {s['v2Recs']}; v1 CRITICAL inherent {s['v1CritInh']} -> "
        f"v2 {s['v2CritInh']}; v1 CRITICAL residual {s['v1CritRes']} -> v2 {s['v2CritRes']}; "
        f"expected bands agreed {s['expectedAgree']} of {s['expectedCompared']} compared."
    )
    a("")
    a("## Hazard detail (pass 1)")
    a("")
    for r in rows:
        a(f"<details><summary>{r['id']} — {r['title']}</summary>")
        a("")
        a(
            "| Hazard | Family | LLM / team | Inherent v2 | Residual v2 | Tolerability (pass 1) | After implementing |"
        )
        a("|---|---|---|---|---|---|---|")
        fin = {h["key"]: h for h in r["final"]["hazards"]}
        for h in r["v2"]["hazards"]:
            p = h["proposed"] or {}
            prop = f"L{p.get('likelihood')}xS{p.get('severity')}" if p.get("likelihood") else "entry rating"
            tol = h["tolerability"]
            tol_s = f"{tol['state']} ({tol['route'] or '-'})" if tol else "not assessed"
            f = fin.get(h["key"]) or {}
            fin_s = f"{f.get('state')} ({f.get('route') or '-'})" + (
                f" missing {', '.join(f['missing'])}" if f.get("missing") else ""
            )
            a(
                f"| {h['name']} | {h['family']} | {prop} | {_r(h['inherent'])} | "
                f"{_r(h['residual']) if h['residual'] else 'not assessed'} | {tol_s} | {fin_s} |"
            )
        if r["v2"]["droppedHazards"]:
            a("")
            a(f"Dropped by the hazard cap: {', '.join(r['v2']['droppedHazards'])}.")
        if r["v2"]["recommendations"]:
            a("")
            a(
                "Recommendations: "
                + "; ".join(
                    f"{x['hierarchy']} {x['id']} -> {', '.join(x['targets'])} "
                    f"({x['costBand']}{', study-level' if x['studyLevel'] else ''})"
                    for x in r["v2"]["recommendations"]
                )
                + "."
            )
        if r["v2"]["linkedCapas"]:
            a(f"Linked open CAPAs (not re-raised): {', '.join(r['v2']['linkedCapas'])}.")
        a("")
        a("</details>")
        a("")
    return "\n".join(L) + "\n"


# ─── driver ─────────────────────────────────────────────────────────────


def _subprocess_digests(seed: str) -> dict[str, str] | None:
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT)}
    p = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--digest-only"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if p.returncode != 0:
        return None
    return json.loads(p.stdout.strip().splitlines()[-1])


def evaluate_all(path: Path = ACTIVITIES, *, cross_process: bool = True) -> dict[str, Any]:
    fixture = load_fixture(path)
    rows, d1 = run_all(fixture)
    _, d2 = run_all(fixture)
    _, d3 = run_all(fixture, reverse=True)
    same = d1 == d2 == d3
    detail = "2 in-process runs + 1 reverse-order run identical" if same else "in-process runs differ"
    if cross_process:
        sub = [(seed, _subprocess_digests(seed)) for seed in ("0", "4242")]
        ok = all(d == d1 for _, d in sub)
        same = same and ok
        detail += (
            "; subprocesses with PYTHONHASHSEED=0 and 4242 identical"
            if ok
            else "; subprocess runs differ: "
            + ", ".join(
                f"seed {s}: " + ("failed" if d is None else ", ".join(k for k in d1 if d.get(k) != d1[k]))
                for s, d in sub
                if d != d1
            )
        )
    by_id = {r["id"]: r for r in rows}
    benches = []
    if "BENCH-1" in by_id:
        benches.append({"id": "BENCH-1", "row": by_id["BENCH-1"], "checks": bench1_checks(by_id["BENCH-1"])})
        b1 = next(a for a in fixture["activities"] if a["id"] == "BENCH-1")
        sv, _ = run_activity(stored_variant(b1), fixture)
        benches.append(
            {"id": "BENCH-1/stored (tests entry1, supplementary)", "row": sv, "checks": bench1_checks(sv)}
        )
    if "BENCH-6" in by_id:
        benches.append({"id": "BENCH-6", "row": by_id["BENCH-6"], "checks": bench6_checks(by_id["BENCH-6"])})
    compared = [v for r in rows for v in r["expectedMatch"].values() if v is not None]
    summary = {
        "v1Recs": sum(r["v1"]["recommendations"] for r in rows),
        "v2Recs": sum(len(r["v2"]["recommendations"]) for r in rows),
        "v1CritInh": sum(1 for r in rows if r["v1"]["inherent"]["level"] == "CRITICAL"),
        "v2CritInh": sum(1 for r in rows if (r["v2"]["inherent"] or {}).get("level") == "CRITICAL"),
        "v1CritRes": sum(1 for r in rows if r["v1"]["residual"]["level"] == "CRITICAL"),
        "v2CritRes": sum(1 for r in rows if (r["v2"]["residual"] or {}).get("level") == "CRITICAL"),
        "expectedCompared": len(compared),
        "expectedAgree": sum(1 for v in compared if v),
    }
    return {
        "engineVersion": ENGINE_VERSION,
        "activityCount": len(rows),
        "deterministic": same,
        "determinismDetail": detail,
        "digests": d1,
        "targets": targets(rows, same, detail),
        "why": why(rows),
        "benchmarks": benches,
        "summary": summary,
        "activities": rows,
    }


def _strip_dates(obj: Any) -> Any:
    """results.json stays stable from day to day: drop date fields."""
    if isinstance(obj, dict):
        return {k: _strip_dates(v) for k, v in obj.items() if k not in ("dueDate",)}
    if isinstance(obj, list):
        return [_strip_dates(v) for v in obj]
    return obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--activities", default=str(ACTIVITIES))
    ap.add_argument(
        "--digest-only", action="store_true", help="print per-activity digests as JSON (determinism check)"
    )
    ap.add_argument("--no-subprocess", action="store_true", help="skip the cross-process determinism check")
    args = ap.parse_args(argv)
    if args.digest_only:
        _, d = run_all(load_fixture(Path(args.activities)))
        print(json.dumps(d, sort_keys=True))
        return 0
    out = evaluate_all(Path(args.activities), cross_process=not args.no_subprocess)
    REPORT.write_text(render_report(out), encoding="utf-8")
    results = {k: v for k, v in out.items() if k != "digests"}
    results["benchmarks"] = [
        {
            "id": b["id"],
            "checks": b["checks"],
            "row": {k: b["row"][k] for k in ("id", "v1", "v2", "pass2", "final")},
        }
        for b in out["benchmarks"]
    ]
    RESULTS.write_text(
        json.dumps(_strip_dates(results), indent=1, sort_keys=False, default=str) + "\n", encoding="utf-8"
    )
    print(f"{out['activityCount']} activities, engine {out['engineVersion']}")
    for t in out["targets"]:
        print(f"  [{_pf(t['pass']):4}] {t['target']}: {t['value']}")
    for b in out["benchmarks"]:
        for c in b["checks"]:
            print(f"  [{_pf(c['pass']):4}] {b['id']}: {c['check']} ({c['value']})")
    print(f"wrote {REPORT.relative_to(ROOT)} and {RESULTS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
