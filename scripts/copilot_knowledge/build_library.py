"""Build the copilot's observation reference library from a reviewed register.

Inputs (none of them ship with the app):
  --rows      de-identified observation rows (JSON list, from prepare_observations.py)
  --reviews   directory of reviewer JSONL files, one line per row (see REVIEW_BRIEF.md)
  --names     JSON list of every person name in the source register (used only to
              scan the output for leaks; it is never written out)
  --hira      curated HIRA library JSON (from curate_hira.py)
  --out       app/services/copilot/knowledge/data

What goes into observation_library.json:
  * every reviewed row except those marked EXCLUDE, with the reviewer's corrected
    type / category / risk / fatal-potential labels, a short reviewer-written hazard
    phrase and hazard tags;
  * rewritten observation + corrective-action text ONLY for rows the reviewer chose
    as worked examples, and only if the PII scan finds no person name, employee id
    or phone number in it;
  * per-category statistics and the plant's closure-target practice.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import statistics
import sys
from pathlib import Path

VALID_TAGS = set("""exposed_wiring electrical_panel earthing cable_damage electrical_shock fall_from_height open_edge_railing
floor_opening ladder_scaffold fragile_roof slip_oil_water trip_hazard floor_damage rotating_unguarded nip_point conveyor_roller
crane_hoist lifting_tackle suspended_load falling_object material_stacking coil_handling vehicle_pedestrian forklift road_condition
combustible_material fire_equipment hot_metal_slag hot_surface_burn gas_leak gas_cylinder chemical_acid dust_fume noise heat_stress
illumination ventilation confined_space ppe_noncompliance unsafe_behaviour permit_isolation housekeeping_scrap passage_blocked
signage emergency_egress defective_tool pressure_system hydraulic_oil_leak structural_damage ergonomics lone_work water_drainage
other""".split())
LEVELS = {"ELIMINATION", "SUBSTITUTION", "ENGINEERING", "ADMINISTRATIVE", "PPE", "NONE"}
CAT_LABELS = {
    "A1": "Tools, Equipment & Machine Guarding (unsafe use)", "A2": "Environmental & Lone Work Hazards",
    "A3": "Procedures, PTW & Isolation", "A4": "Competency & Fitness for Duty", "A5": "PPE & Clothing Compliance",
    "A6": "Indiscipline", "A7": "Material Handling", "A8": "Workplace Protocol & Unauthorized Access", "A9": "Road Safety",
    "A10": "Housekeeping (act)", "C1": "Defective Equipment", "C2": "Defective Workplace", "C3": "Electrical Hazard",
    "C4": "Ergonomics", "C5": "Fire Hazard", "C6": "Fugitive Emission", "C7": "Hazardous Substance", "C8": "Heat/Cold",
    "C9": "Housekeeping (condition)", "C10": "Illumination", "C11": "Machine Guarding", "C12": "Noise",
    "C13": "Passage Obstruction", "C14": "Radiation", "C15": "Signages", "C16": "Slip/Trip/Fall", "C17": "Tools & Tackles",
    "C18": "Transportation", "C19": "Ventilation", "C20": "Work at Height",
}
# Words that are also people's names in the register but are ordinary words here.
NAME_ALLOW = {"maint", "instrument", "face", "name", "incharge", "risky", "electical", "unknown", "rout", "team",
              "shift", "line", "area", "crane", "civil", "operator", "driver", "person", "persons", "test", "confirm",
              "jindal"}


def name_tokens_for(names: list[str], rows: dict[str, dict]) -> set[str]:
    """Words that occur in people's names, minus words that are ordinary in
    observation text (the register's "Individual Name" column sometimes holds
    team names such as "Civil Team")."""
    df: collections.Counter[str] = collections.Counter()
    for r in rows.values():
        df.update(set(re.findall(r"[a-z]{4,}", (r.get("obs") or "").lower())))
    toks = {w for n in names for w in re.findall(r"[a-z]{4,}", n.lower())}
    return {w for w in toks if df[w] < 8 and w not in NAME_ALLOW}


def pii_hits(text: str, full_names: list[str], tokens: set[str]) -> list[str]:
    t = text.lower()
    # Full names only when every long word in them is a name word ("Civil Team" is not a person).
    hits = [n for n in full_names if len(n) >= 6 and n.lower() in t
            and all(w in tokens for w in re.findall(r"[a-z]{4,}", n.lower()))]
    for w in re.findall(r"[a-z]{4,}", t):
        if w in tokens:
            hits.append(w)
    if re.search(r"\b9000\d{4}\b|\b\d{10}\b", text):
        hits.append("employee id / phone number")
    return sorted(set(hits))


def load_reviews(folder: str) -> tuple[dict[str, dict], int]:
    reviews: dict[str, dict] = {}
    bad = 0
    for f in sorted(Path(folder).glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            reviews[r.get("code")] = r
    return reviews, bad


def corrected_cases(rows: dict[str, dict], reviews: dict[str, dict], names: list[str], name_tokens: set[str]):
    """Apply each row's review: drop EXCLUDE rows, use the corrected labels, keep
    rewritten text only for worked examples that pass the PII scan."""
    stats = collections.Counter()
    issue_counts = collections.Counter()
    pii_dropped: list[dict] = []
    cases = []
    for code, row in rows.items():
        rv = reviews[code]
        verdict = rv.get("verdict", "OK")
        issues = rv.get("issues") or []
        issue_counts.update(issues)
        stats[f"verdict_{verdict}"] += 1
        if verdict == "EXCLUDE":
            continue
        cat = row["cat"]
        sug = (rv.get("suggested_cat") or "").strip().upper()
        if sug in CAT_LABELS and ({"CATEGORY_MISMATCH", "TYPE_MISMATCH"} & set(issues)):
            cat = sug
            stats["category_corrected"] += 1
        typ = "ACT" if cat.startswith("A") else "CONDITION"
        risk = row["risk"]
        fp = row["severity"] == "Fatal Potential"
        sr = (rv.get("suggested_risk") or "").strip().capitalize()
        if "FATAL_POTENTIAL_MISSED" in issues:
            fp, risk = True, "High"
            stats["fatal_potential_added"] += 1
        elif sr in ("Low", "Medium", "High") and ({"RISK_UNDERRATED", "RISK_OVERRATED"} & set(issues)):
            risk = sr
            stats["risk_corrected"] += 1
            if fp and sr != "High":
                fp = False
        tags = [t for t in (rv.get("tags") or []) if t in VALID_TAGS][:3]
        level = rv.get("control_level") if rv.get("control_level") in LEVELS else None
        case = {"id": code, "type": typ, "cat": cat, "risk": risk, "fp": fp, "fn": row.get("function"),
                "hazard": (rv.get("hazard") or "").strip().lower()[:80] or None, "tags": tags, "level": level,
                "area": row.get("area")}
        if cat != row["cat"]:
            case["pc"] = row["cat"]  # the category the plant originally filed it under
        ex = bool(rv.get("exemplar")) and rv.get("clean_obs") and not ({"CATEGORY_MISMATCH", "RISK_UNDERRATED",
                                                                          "FATAL_POTENTIAL_MISSED"} & set(issues))
        if ex:
            obs = " ".join(str(rv["clean_obs"]).split())[:260]
            act = " ".join(str(rv.get("clean_action") or "").split())[:260] or None
            hits = pii_hits(obs + " " + (act or ""), names, name_tokens)
            if hits:
                pii_dropped.append({"code": code, "hits": hits})
                ex = False
            else:
                case.update({"ex": True, "obs": obs, "action": act})
                stats["exemplars"] += 1
        cases.append(case)
    return cases, stats, issue_counts, pii_dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--reviews", required=True)
    ap.add_argument("--names", required=True)
    ap.add_argument("--hira", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--source-label", default="Plant safety observation register")
    ap.add_argument("--report", default=None, help="write a JSON build report here")
    a = ap.parse_args()

    rows = {r["code"]: r for r in json.load(open(a.rows, encoding="utf-8"))}
    reviews, bad_lines = load_reviews(a.reviews)
    missing = sorted(set(rows) - set(reviews))
    extra = sorted(set(reviews) - set(rows))
    if missing or bad_lines:
        print(f"ERROR: {len(missing)} rows have no review, {bad_lines} unparseable review lines", file=sys.stderr)
        print("first missing:", missing[:20], file=sys.stderr)
        return 1

    names = json.load(open(a.names, encoding="utf-8"))
    name_tokens = name_tokens_for(names, rows)

    cases, stats, issue_counts, pii_dropped = corrected_cases(rows, reviews, names, name_tokens)

    # Category statistics (after corrections).
    cats: dict[str, dict] = {}
    by_cat = collections.defaultdict(list)
    for c in cases:
        by_cat[c["cat"]].append(c)
    for code, items in by_cat.items():
        rm = collections.Counter("FatalPotential" if c["fp"] else c["risk"] for c in items)
        fm = collections.Counter(c["fn"] for c in items if c["fn"])
        hz = collections.Counter(c["hazard"] for c in items if c["hazard"])
        lv = collections.Counter(c["level"] for c in items if c["level"])
        tg = collections.Counter(t for c in items for t in c["tags"])
        cats[code] = {"code": code, "label": CAT_LABELS.get(code, code), "type": "ACT" if code.startswith("A") else "CONDITION",
                      "count": len(items), "riskMix": dict(rm), "functionMix": dict(fm.most_common(4)),
                      "topHazards": [h for h, _ in hz.most_common(10)], "topTags": [t for t, _ in tg.most_common(6)],
                      "controlLevelMix": dict(lv)}

    # Closure-target practice from the source dates.
    td = collections.defaultdict(list)
    for r in rows.values():
        if r.get("target_days") is not None:
            td["High" if r["severity"] == "Fatal Potential" else r["risk"]].append(r["target_days"])
    target_days = {k: int(round(statistics.median(v))) for k, v in td.items()}
    target_days["High"] = max(1, target_days.get("High", 1))

    dates = sorted(r["date"] for r in rows.values() if r.get("date"))
    lib = {
        "meta": {
            "source": a.source_label, "rows": len(rows), "reviewedRows": len(reviews), "included": len(cases),
            "exemplars": stats["exemplars"], "period": [dates[0], dates[-1]] if dates else None,
            "built": dt.date.today().isoformat(),
            "note": "Labels are the reviewer-corrected labels. Free text exists only for reviewed worked examples, "
                    "rewritten with names removed.",
        },
        "practice": {
            "targetDays": target_days,
            "rules": [
                "Fatal Potential is always rated High risk and closed the same day or next day.",
                "Medium risk: closure target about two weeks. Low risk: about 30 days.",
                "Unsafe conditions need a corrective action plan; unsafe acts are closed by correcting the behaviour on the spot.",
                "The responsible function follows the hazard: electrical faults to Electrical, guards and leaks to Mechanical, "
                "housekeeping and passage obstruction to Operations, structural repairs to Civil.",
            ],
        },
        "categories": dict(sorted(cats.items(), key=lambda kv: (kv[0][0], int(kv[0][1:])))),
        "cases": cases,
    }
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "observation_library.json").write_text(json.dumps(lib, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    hira = json.load(open(a.hira, encoding="utf-8"))
    hira.setdefault("meta", {}).update({"source": "Plant HIRA, steel melt shop (QSD/24/F) with control strategy (QSD/25/F)",
                                        "significantScore": 3.33, "built": dt.date.today().isoformat()})
    # PII scan of the HIRA library too (team names are in the workbook header, never in rows).
    hira_hits = [e["ref"] for e in hira["entries"]
                 if pii_hits(json.dumps(e, ensure_ascii=False), names, name_tokens)]
    (out / "hira_library.json").write_text(json.dumps(hira, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    report = {"stats": dict(stats), "issues": dict(issue_counts.most_common()), "extraReviews": extra[:20],
              "piiDroppedExemplars": pii_dropped, "hiraPiiHits": hira_hits, "targetDays": target_days,
              "categories": {k: v["count"] for k, v in lib["categories"].items()}}
    if a.report:
        Path(a.report).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
