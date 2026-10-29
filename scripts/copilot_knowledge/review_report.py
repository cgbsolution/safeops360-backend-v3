"""Summarise the row-by-row review into a markdown report for the safety team.

    python review_report.py --rows rows.json --reviews reviews/ --hira hira_library.json --out REPORT.md [--eval eval.json]

No names, employee ids or raw free text are written: only counts, the plant's
category codes, and the record numbers the team can look up themselves.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_library as bl  # noqa: E402

ISSUE_TEXT = {
    "CATEGORY_MISMATCH": "Filed under the wrong plant category",
    "CLOSURE_WEAK": "Closed with a remark that does not show the hazard was fixed",
    "RISK_UNDERRATED": "Risk level rated too low",
    "VAGUE_OBSERVATION": "Description too vague to act on",
    "TYPE_MISMATCH": "Unsafe act recorded as a condition, or the reverse",
    "FATAL_POTENTIAL_MISSED": "Fatal potential not flagged",
    "ACTION_WEAK": "Action plan missing, generic or wrong",
    "MULTIPLE_HAZARDS": "Several hazards in one record",
    "RISK_OVERRATED": "Risk level rated too high",
    "NOT_A_SAFETY_ISSUE": "Not a safety issue",
    "PII_IN_TEXT": "Person's name / phone number in the text",
    "ACTION_NOT_ADDRESSING_HAZARD": "Action does not address the hazard",
    "TARGET_INCONSISTENT": "Target date inconsistent with the risk level",
    "NON_ENGLISH_OR_GARBLED": "Garbled text",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--reviews", required=True)
    ap.add_argument("--hira", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--eval", default=None)
    a = ap.parse_args()
    rows = {r["code"]: r for r in json.load(open(a.rows, encoding="utf-8"))}
    reviews, _ = bl.load_reviews(a.reviews)
    n = len(rows)
    verdicts = collections.Counter(reviews[c].get("verdict", "OK") for c in rows)
    issues = collections.Counter(i for c in rows for i in reviews[c].get("issues") or [])
    moves = collections.Counter((rows[c]["cat"], reviews[c].get("suggested_cat")) for c in rows
                                if "CATEGORY_MISMATCH" in (reviews[c].get("issues") or []) and reviews[c].get("suggested_cat"))
    fp_missed = sorted(c for c in rows if "FATAL_POTENTIAL_MISSED" in (reviews[c].get("issues") or []))
    under_by_dept = collections.Counter(rows[c]["dept"] for c in rows if "RISK_UNDERRATED" in (reviews[c].get("issues") or []))
    weak_by_dept = collections.Counter(rows[c]["dept"] for c in rows if "CLOSURE_WEAK" in (reviews[c].get("issues") or []))
    dept_n = collections.Counter(rows[c]["dept"] for c in rows)
    tags = collections.Counter(t for c in rows for t in reviews[c].get("tags") or [])
    exemplars = sum(1 for c in rows if reviews[c].get("exemplar"))
    pending_fp = sorted(c for c in rows if rows[c]["status"] == "Pending" and (
        rows[c]["severity"] == "Fatal Potential" or "FATAL_POTENTIAL_MISSED" in (reviews[c].get("issues") or [])))
    label = bl.CAT_LABELS
    pct = lambda x, d=n: f"{100 * x / max(d, 1):.0f}%"  # noqa: E731

    L = [f"# Safety observation and HIRA data review — {Path(a.out).stem.split('_')[-1]}", "",
         f"Every one of the {n:,} observations was reviewed individually against the plant's own category list and a "
         "fatal / lost-time / first-aid risk rubric; every HIRA and control-strategy row was checked by hand. "
         "This report lists what the review found. Record numbers are given so the team can open them; "
         "no names are reproduced.", "",
         "## Observation register", "",
         "| | Rows | Share |", "|---|---:|---:|",
         f"| Usable as recorded | {verdicts['OK']:,} | {pct(verdicts['OK'])} |",
         f"| Usable, with a flagged problem | {verdicts['ISSUE']:,} | {pct(verdicts['ISSUE'])} |",
         f"| Not usable (not a safety issue, test entry, garbled) | {verdicts['EXCLUDE']:,} | {pct(verdicts['EXCLUDE'])} |",
         f"| Chosen as worked examples for the copilot | {exemplars:,} | {pct(exemplars)} |", "",
         "### Problems found", "", "| Problem | Rows | Share |", "|---|---:|---:|"]
    L += [f"| {ISSUE_TEXT.get(k, k)} | {v:,} | {pct(v)} |" for k, v in issues.most_common()]
    L += ["", "### Most common category corrections", "", "| Filed as | Should be | Rows |", "|---|---|---:|"]
    L += [f"| {o} {label.get(o, '')} | {s} {label.get(s, '')} | {v} |" for (o, s), v in moves.most_common(12)]
    L += ["", "### Where risk is under-rated and closures are weak (by department)", "",
          "| Department | Observations | Risk under-rated | Weak closures |", "|---|---:|---:|---:|"]
    for d, cnt in dept_n.most_common(14):
        L.append(f"| {d} | {cnt} | {under_by_dept[d]} ({pct(under_by_dept[d], cnt)}) | {weak_by_dept[d]} ({pct(weak_by_dept[d], cnt)}) |")
    L += ["", f"### Fatal potential not flagged ({len(fp_missed)} rows)", "",
          "Rated Low or Medium by the plant although the description shows fatal potential (unprotected height, "
          "live parts within reach, damaged lifting gear in use, people under loads, unguarded rotating parts). "
          "Worth re-checking:", "", ", ".join(fp_missed) or "None.", ""]
    if pending_fp:
        L += [f"**Still pending and fatal potential ({len(pending_fp)}):** " + ", ".join(pending_fp), ""]
    L += ["### Most frequent hazards", "", "| Hazard | Rows |", "|---|---:|"]
    L += [f"| {t.replace('_', ' ')} | {v} |" for t, v in tags.most_common(15)]

    hira = json.load(open(a.hira, encoding="utf-8"))["entries"]
    flags = collections.Counter(f.split(":")[0] for e in hira for f in e["reviewFlags"])
    levels = collections.Counter(c["hierarchy"] for e in hira for c in e["controls"])
    not_ok = [e for e in hira if not e["acceptable"]]
    L += ["", "## HIRA (SMS-1)", "",
          f"{len(hira)} activity-hazard rows; {len(not_ok)} not acceptable under the plant's rule "
          "(not acceptable when any legal, domino, chronic or emergency concern is Y, or GT = SP × LH × CN × DE is above 3.33). "
          "The rule reproduces the team's own verdict on all but a handful of rows, listed below.", "",
          "| Finding | Rows |", "|---|---:|"]
    text = {"ADMIN_PPE_ONLY": "Not acceptable, yet controlled only by an SOP reference and PPE",
            "VAGUE_CONSEQUENCE": "Consequence says only that someone may be injured",
            "SECTION_CORRECTED": "Listed under the wrong area (general / COVID-19 block under AFS)",
            "CONSEQUENCE_CORRECTED": "Consequence does not follow from the hazard (copy-paste)",
            "NO_STRATEGY_ROW": "Not acceptable but missing from the control strategy sheet",
            "CONSERVATIVE_VERDICT": "Marked not acceptable although score and concerns allow acceptable",
            "SCORE_CORRECTED": "Score arithmetic wrong", "RULE_INCONSISTENT": "Marked acceptable although a concern is Y",
            "ACCEPTABILITY_FILLED": "Acceptability left blank", "HAZARD_CORRECTED": "Hazard does not match the consequence",
            "ROUTINE_FILLED": "Routine / non-routine left blank"}
    L += [f"| {text.get(k, k)} | {v} |" for k, v in flags.most_common()]
    L += ["", "Rows with a corrected value:", ""]
    for e in hira:
        fixes = [f for f in e["reviewFlags"] if f.split(":")[0] in ("SCORE_CORRECTED", "RULE_INCONSISTENT", "ACCEPTABILITY_FILLED",
                                                                   "CONSEQUENCE_CORRECTED", "HAZARD_CORRECTED")]
        if fixes:
            L.append(f"- Workbook row {e['ref'].rsplit('R', 1)[-1]} ({e['section']}, {e['activity']}): " + "; ".join(fixes))
    L += ["", f"Control hierarchy across all rows: " + ", ".join(f"{k.lower()} {v}" for k, v in levels.most_common())
          + f". {'No' if not levels['ELIMINATION'] else levels['ELIMINATION']} elimination and {levels['SUBSTITUTION'] or 'no'} substitution "
          f"control{'s' if levels['SUBSTITUTION'] != 1 else ''}; engineering controls on only {sum(1 for e in hira if any(c['hierarchy'] == 'ENGINEERING' for c in e['controls']))} rows. "
          "The copilot proposes higher-order controls and shows how each would change the residual risk.", ""]

    if a.eval:
        ev = json.load(open(a.eval, encoding="utf-8"))
        t, f = ev["withObservationType"], ev["fatalPotential"]
        L += ["## How well the copilot's suggestions match the reviewed labels", "",
              f"Tested on all {ev['rowsEvaluated']:,} reviewed rows, each predicted from the others ({ev['folds']}-fold), "
              "using the original description as typed:", "",
              "| Measure | Result |", "|---|---:|",
              f"| Category right first time | {t['categoryTop1']}% |",
              f"| Category among the top 3 suggestions | {t['categoryTop3']}% |",
              f"| Severity exact | {t['severityExact']}% |",
              f"| Severity within one step | {t['severityWithinOneRung']}% |",
              f"| Fatal-potential rows rated High/Critical or flagged for the question | {f['ratedHighOrSignalled']}% |",
              f"| Plant's original category agrees with the review (for comparison) | {ev['baselines']['plantOriginalCategoryAgreesWithReview']}% |",
              "", "These are the copilot's starting suggestions from the library alone; the copilot then reads the "
              "description itself and asks about fatal potential when the signals are there.", ""]
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print("wrote", a.out, len(L), "lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
