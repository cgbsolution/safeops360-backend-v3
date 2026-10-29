"""Compare the client's HIRA scores with the SafeOps benchmark ratings.

Usage: python compare.py <bench_dir>
Reads in/chunk*.json and out/chunk*.jsonl, writes divergence.json and prints a summary.
"""
import collections as C
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
THRESHOLD = 3.33
FACTORS = ("SP", "LH", "CN", "DE")
CONCERNS = ("LC", "DC", "CE", "EC")
RANGES = {"SP": (1, 4), "LH": (1, 2), "CN": (1, 5), "DE": (1, 2)}


def gt(s):
    return s["SP"] * s["LH"] * s["CN"] * s["DE"]


def acceptable(s):
    return not any(s[c] for c in CONCERNS) and gt(s) <= THRESHOLD


import re

INSPECTED = re.compile(r"inspect|third|certif|test", re.I)
HARMONISED = []


def harmonise(chunk, r, o):
    """One rule per judgement call the four reviewers made differently."""
    b, notes = o["bench"], []
    ctl = " ".join(r["controls"])
    if r["hazardType"] == "EQUIPMENT_FAILURE" and "lance" not in r["activity"].lower():
        # Crane or lifting-gear failure: statutory examination finds wear (DE 1, 3 of 4 reviewers);
        # routine lifts with that examination in place stay LH 1 (3 of 4 reviewers).
        if b["DE"] == 2:
            b["DE"] = 1
            notes.append("DE 2->1 (lifting gear is examined; failure is not hidden)")
        if r["routine"] == "ROUTINE" and INSPECTED.search(ctl) and b["LH"] == 2 and r["client"]["LH"] == 1:
            b["LH"] = 1
            notes.append("LH 2->1 (routine lift with statutory inspection)")
    if r["hazardType"] == "DUST_FUME" and b["CN"] >= 3 and b["DE"] == 1:
        # Rubric: slow chronic exposure is hard to detect before harm.
        b["DE"] = 2
        notes.append("DE 1->2 (chronic exposure, rubric)")
    if notes:
        HARMONISED.append((chunk, r["ref"], notes))
        o["harmonised"] = notes


rows, bench = [], {}
for i in range(1, 5):
    inp = json.loads((ROOT / "in" / f"chunk{i}.json").read_text(encoding="utf-8"))
    out = [json.loads(l) for l in (ROOT / "out" / f"chunk{i}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [r["ref"] for r in inp] == [o["ref"] for o in out], f"chunk{i}: ref order/count mismatch"
    for r, o in zip(inp, out):
        harmonise(i, r, o)
        b = o["bench"]
        for f, (lo, hi) in RANGES.items():
            assert isinstance(b[f], int) and lo <= b[f] <= hi, (o["ref"], f, b[f])
        for c in CONCERNS:
            assert isinstance(b[c], bool), (o["ref"], c)
        rows.append(r)
        bench[r["ref"]] = o

result, stats = [], C.Counter()
fac_moves = {f: C.Counter() for f in FACTORS}
conc_moves = {c: C.Counter() for c in CONCERNS}
by_hazard = C.defaultdict(lambda: C.Counter())
for r in rows:
    c = r["client"]
    o = bench[r["ref"]]
    b = o["bench"]
    c_gt, b_gt = c["GT"], gt(b)
    c_ok, b_ok = c["acceptable"], acceptable(b)
    factor_diff = {f: [c[f], b[f]] for f in FACTORS if c[f] != b[f]}
    concern_diff = {k: [c[k], b[k]] for k in CONCERNS if bool(c[k]) != b[k]}
    for f, (x, y) in factor_diff.items():
        fac_moves[f]["up" if y > x else "down"] += 1
    for k, (x, y) in concern_diff.items():
        conc_moves[k]["added" if y else "removed"] += 1

    if c_ok and not b_ok:
        accept_change = "BECOMES_NOT_ACCEPTABLE"
    elif b_ok and not c_ok:
        accept_change = "BECOMES_ACCEPTABLE"
    else:
        accept_change = "NONE"
    direction = "HIGHER" if b_gt > c_gt else "LOWER" if b_gt < c_gt else "SAME"
    tags = []
    if accept_change != "NONE":
        tags.append(accept_change)
    if b["CN"] >= 4 and c["CN"] <= 3:
        tags.append("SERIOUS_CONSEQUENCE_UNDERRATED")
    if c["CN"] >= 4 and b["CN"] <= 3:
        tags.append("CONSEQUENCE_OVERRATED")
    if c["LH"] == 1 and b["LH"] == 2:
        tags.append("LIKELIHOOD_RELIES_ON_PROCEDURE_PPE")
    if b["LC"] and not c["LC"]:
        tags.append("LEGAL_CONCERN_MISSING")
    if (b["EC"] and not c["EC"]) or (b["DC"] and not c["DC"]):
        tags.append("ESCALATION_NOT_FLAGGED")
    if (b["CE"] and not c["CE"]):
        tags.append("CHRONIC_EFFECT_NOT_FLAGGED")
    # MAJOR = changes what the plant must do, or misses a death/disability outcome.
    if accept_change != "NONE" or "SERIOUS_CONSEQUENCE_UNDERRATED" in tags or "CONSEQUENCE_OVERRATED" in tags:
        level = "MAJOR"
    elif factor_diff or concern_diff:
        level = "MINOR"
    else:
        level = "NONE"
    for t in tags:
        stats["tag_" + t] += 1
    stats[level] += 1
    stats[f"dir_{direction}"] += 1
    stats[f"acc_{accept_change}"] += 1
    by_hazard[r["hazardType"]][direction] += 1
    result.append({
        "ref": r["ref"], "area": r["area"], "activity": r["activity"], "hazard": r["hazard"],
        "hazardType": r["hazardType"],
        "client": {**{k: c[k] for k in FACTORS + CONCERNS}, "GT": c_gt, "acceptable": c_ok},
        "benchmark": {**{k: b[k] for k in FACTORS + CONCERNS}, "GT": b_gt, "acceptable": b_ok},
        "factorDiff": factor_diff, "concernDiff": concern_diff,
        "direction": direction, "acceptabilityChange": accept_change, "level": level, "tags": tags,
        "harmonised": o.get("harmonised"),
        "reason": o["reason"], "suggestedConsequence": o.get("suggestedConsequence"),
        "confidence": o.get("confidence"),
    })

(ROOT / "divergence.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

n = len(result)
print("rows", n)
print("levels", {k: stats[k] for k in ("NONE", "MINOR", "MAJOR")})
print("direction", {k: stats[f"dir_{k}"] for k in ("HIGHER", "SAME", "LOWER")})
print("acceptability", {k: stats[f"acc_{k}"] for k in ("NONE", "BECOMES_NOT_ACCEPTABLE", "BECOMES_ACCEPTABLE")})
print("client not acceptable", sum(1 for x in result if not x["client"]["acceptable"]),
      "benchmark not acceptable", sum(1 for x in result if not x["benchmark"]["acceptable"]))
print("factor moves", {f: dict(v) for f, v in fac_moves.items()})
print("concern moves", {k: dict(v) for k, v in conc_moves.items()})
print("tags", {k[4:]: v for k, v in stats.items() if k.startswith("tag_")})
print("harmonised rows", len(HARMONISED))
print("confidence", dict(C.Counter(x["confidence"] for x in result if x["level"] != "NONE")))
cg = sorted(x["client"]["GT"] for x in result)
bg = sorted(x["benchmark"]["GT"] for x in result)
print("median GT client", cg[n // 2], "benchmark", bg[n // 2], "| mean", round(sum(cg) / n, 2), round(sum(bg) / n, 2))
print("by hazard type (HIGHER/SAME/LOWER):")
for h, v in sorted(by_hazard.items(), key=lambda kv: -sum(kv[1].values())):
    print(f"  {h:24s} {v['HIGHER']:3d} {v['SAME']:3d} {v['LOWER']:3d}")
print("CN by hazard type, client -> benchmark (mean):")
agg = C.defaultdict(list)
for x in result:
    agg[x["hazardType"]].append((x["client"]["CN"], x["benchmark"]["CN"]))
for h, v in sorted(agg.items(), key=lambda kv: -len(kv[1])):
    print(f"  {h:24s} n={len(v):3d} {sum(a for a, _ in v) / len(v):.1f} -> {sum(b for _, b in v) / len(v):.1f}")
print("per-area major:")
area = C.defaultdict(C.Counter)
for x in result:
    area[x["area"]][x["level"]] += 1
for a, v in area.items():
    print(f"  {a:40s} {dict(v)}")
