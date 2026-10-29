"""Hold-out evaluation of the observation suggestions.

    python evaluate.py --rows rows.json --reviews reviews/ --names names.json [--folds 5]

Builds the library from (k-1)/k of the reviewed rows and asks `suggest()` about
the held-out rows, using the ORIGINAL observation text (what a user would type)
and comparing against the REVIEWED labels. Runs every fold, so every row is
tested once. Offline; nothing is written except the printed report.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import build_library as bl  # noqa: E402

from app.services.copilot.knowledge import observations as obs_kb  # noqa: E402

RUNG = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def fold_of(code: str, k: int) -> int:
    return int(hashlib.sha1(code.encode()).hexdigest(), 16) % k


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--reviews", required=True)
    ap.add_argument("--names", required=True)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    rows = {r["code"]: r for r in json.load(open(a.rows, encoding="utf-8"))}
    reviews, _ = bl.load_reviews(a.reviews)
    names = json.load(open(a.names, encoding="utf-8"))
    tokens = bl.name_tokens_for(names, rows)
    cases, *_ = bl.corrected_cases(rows, reviews, names, tokens)
    by_id = {c["id"]: c for c in cases}
    practice = {"targetDays": {"Low": 30, "Medium": 14, "High": 1}}

    m = collections.Counter()
    conf = collections.defaultdict(collections.Counter)
    sev_matrix = collections.Counter()
    for k in range(a.folds):
        train = [c for c in cases if fold_of(c["id"], a.folds) != k]
        test = [c for c in cases if fold_of(c["id"], a.folds) == k]
        lib = {"meta": {"available": True}, "practice": practice, "cases": train,
               "categories": {code: {"label": bl.CAT_LABELS.get(code)} for code in bl.CAT_LABELS}}
        obs_kb.library = lambda lib=lib: lib  # type: ignore[assignment]
        obs_kb._index.cache_clear()
        for c in test:
            text = rows[c["id"]].get("obs") or ""
            truth_sev = "CRITICAL" if c["fp"] else {"Low": "LOW", "Medium": "MEDIUM", "High": "HIGH"}[c["risk"]]
            orig = rows[c["id"]]
            orig_cat = orig["cat"]
            for mode, otype in (("typed", "UNSAFE_ACT" if c["type"] == "ACT" else "UNSAFE_CONDITION"), ("untyped", None)):
                s = obs_kb.suggest(text, observation_type=otype)
                if not s.get("matches"):
                    m[f"{mode}_nomatch"] += 1
                    continue
                m[f"{mode}_n"] += 1
                pred = s["category"]["code"]
                top3 = [pred] + [x["code"] for x in s.get("alternatives") or []]
                m[f"{mode}_cat_top1"] += pred == c["cat"]
                m[f"{mode}_cat_top3"] += c["cat"] in top3
                m[f"{mode}_axis"] += pred[0] == c["cat"][0]
                ps = s["risk"]["safeopsSeverity"]
                m[f"{mode}_sev_exact"] += ps == truth_sev
                m[f"{mode}_sev_within1"] += abs(RUNG[ps] - RUNG[truth_sev]) <= 1
                m[f"{mode}_sev_under"] += RUNG[ps] < RUNG[truth_sev]
                if mode == "typed":
                    conf[c["cat"]][pred] += 1
                    sev_matrix[(truth_sev, ps)] += 1
                    if c["fp"]:
                        m["fp_n"] += 1
                        m["fp_caught"] += ps in ("HIGH", "CRITICAL")
                        m["fp_flagged"] += ps in ("HIGH", "CRITICAL") or bool(s["fatalPotentialSignals"])
                    if ps == "CRITICAL":
                        m["pred_crit"] += 1
                        m["pred_crit_true_high"] += truth_sev in ("HIGH", "CRITICAL")
                    m["fn_n"] += bool(c.get("fn"))
                    m["fn_ok"] += bool(c.get("fn")) and s.get("responsibleFunction") == c["fn"]
                    m["plant_label_agrees"] += orig_cat == c["cat"]
            m["majority_baseline"] += c["cat"] == "C2"

    n_t, n_u = m["typed_n"] or 1, m["untyped_n"] or 1
    pct = lambda x, n: round(100 * x / n, 1)  # noqa: E731
    report = {
        "rowsEvaluated": len(cases), "folds": a.folds,
        "withObservationType": {
            "categoryTop1": pct(m["typed_cat_top1"], n_t), "categoryTop3": pct(m["typed_cat_top3"], n_t),
            "severityExact": pct(m["typed_sev_exact"], n_t), "severityWithinOneRung": pct(m["typed_sev_within1"], n_t),
            "severityUnderRated": pct(m["typed_sev_under"], n_t), "noMatch": m["typed_nomatch"],
        },
        "withoutObservationType": {
            "categoryTop1": pct(m["untyped_cat_top1"], n_u), "categoryTop3": pct(m["untyped_cat_top3"], n_u),
            "actVsConditionRight": pct(m["untyped_axis"], n_u), "severityExact": pct(m["untyped_sev_exact"], n_u),
        },
        "fatalPotential": {"rows": m["fp_n"], "ratedHighOrCritical": pct(m["fp_caught"], m["fp_n"] or 1),
                           "ratedHighOrSignalled": pct(m["fp_flagged"], m["fp_n"] or 1),
                           "criticalSuggestionsThatWereHighOrFatal": pct(m["pred_crit_true_high"], m["pred_crit"] or 1)},
        "responsibleFunction": pct(m["fn_ok"], m["fn_n"] or 1),
        "baselines": {"alwaysMostCommonCategory": pct(m["majority_baseline"], len(cases)),
                      "plantOriginalCategoryAgreesWithReview": pct(m["plant_label_agrees"], n_t)},
        "severityConfusion(truth->pred)": {f"{t}->{p}": n for (t, p), n in sorted(sev_matrix.items())},
        "worstCategories": sorted(
            ({"cat": k, "n": sum(v.values()), "top1": pct(v[k], sum(v.values())), "mostConfusedWith": v.most_common(2)}
             for k, v in conf.items() if sum(v.values()) >= 15), key=lambda d: d["top1"])[:8],
    }
    print(json.dumps(report, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
