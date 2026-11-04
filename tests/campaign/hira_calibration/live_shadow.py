"""HIRA calibration v2 campaign, section 5: live-data shadow run, READ-ONLY, against the database in .env.

For a stratified sample of real entries per plant, runs v1 (hira_review.review_entry) and v2
(service.evaluate_entry) in-process through a read-only connection (live_db.py), then writes the section 5
report, the blind-scoring sheet and key, and a before/after row-count file.

Nothing is written to the database:
  - the connection is read-only inside Postgres (any write fails there) and a client hook refuses non-read SQL;
  - the app's own engine (app.core.db) is replaced by a stub that raises if anything tries to use it;
  - step 5.2 "store both in hira_copilot_shadow_runs" is NOT done (that is a write); results are saved locally.
Outputs go to out/live/<label>/ (git-ignored: it holds real plant data).

Rubric pack: the plant's own HiraCalibrationConfig is used; a plant with no pack is run with --pack (default
STEEL_HEAVY), the pack agreed for JSL Hisar and NW, and the report says so.

    cd safeops_360_bakend
    .venv/Scripts/python.exe tests/campaign/hira_calibration/live_shadow.py --label before [--n 100] [--limit 3]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

import live_db  # noqa: E402
import app.core.db as appdb  # noqa: E402


class _NoAppDb:
    def __call__(self, *a: Any, **k: Any) -> Any:  # noqa: ANN401
        raise RuntimeError("the app's write-capable DB session was requested during the read-only live run")


appdb.AsyncSessionLocal = _NoAppDb()  # log_shadow and anything else that opens its own session fails loudly

from app.services.copilot.hira_review import review_entry  # noqa: E402
from app.services.hira_calibration import config as cal_config  # noqa: E402
from app.services.hira_calibration import review_v2 as rv  # noqa: E402
from app.services.hira_calibration import service as svc  # noqa: E402

BANDS = ("LOW", "MODERATE", "HIGH", "CRITICAL")
BAND_ALIASES = {"LOW": 0, "ACCEPTABLE": 0, "MODERATE": 1, "MEDIUM": 1, "HIGH": 2, "SUBSTANTIAL": 2, "CRITICAL": 3,
                "VERY_HIGH": 3, "INTOLERABLE": 3, "EXTREME": 3}
COUNT_TABLES = ["HiraStudy", "HiraEntry", "HiraEntryHazard", "HiraEntryControl", "HiraEntryRecommendedControl", "HiraCapa",
                "Capa", "HiraCalibrationConfig", "hira_copilot_shadow_runs"]


def band_idx(level: str | None) -> int | None:
    return BAND_ALIASES.get((level or "").upper().replace(" ", "_")) if level else None


def num(x: Any) -> int | None:  # noqa: ANN401
    """A hazard's likelihood / severity: a number, or the engine's calibration dict ({"value": ...})."""
    return x.get("value") if isinstance(x, dict) else x


def h8(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


async def row_counts(db: Any) -> dict[str, int | None]:  # noqa: ANN401
    out = {}
    for t in COUNT_TABLES:
        try:
            out[t] = (await db.execute(text(f'SELECT count(*) FROM "{t}"'))).scalar()
        except Exception:  # noqa: BLE001
            await db.rollback()
            out[t] = None
    await db.rollback()
    return out


async def candidates(db: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    q = '''
      SELECT e.id, p.code AS plant, p.id AS plant_id, s.number AS study, s.title AS study_title, s.status AS study_status,
             e.status, e.routine, e."sequenceNumber" AS seq, a.name AS area, e."areaId" AS area_id, coalesce(m.code, '-') AS matrix,
             (SELECT count(*) FROM "Incident" i WHERE i."areaId" = e."areaId" AND i.date >= now() - interval '720 days')
           + (SELECT count(*) FROM "NearMiss" x WHERE x."areaId" = e."areaId" AND x.date >= now() - interval '720 days') AS area_events
      FROM "HiraEntry" e JOIN "HiraStudy" s ON s.id = e."studyId" JOIN "Plant" p ON p.id = s."plantId"
      LEFT JOIN "Area" a ON a.id = e."areaId" LEFT JOIN "RiskMatrix" m ON m.id = s."riskMatrixId"
      WHERE e."isCurrentVersion"'''
    rows = [dict(r) for r in (await db.execute(text(q))).mappings().all()]
    await db.rollback()
    return rows


def stratified(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Per plant: all entries if <= n; else half routine / half non-routine, round-robin over areas, deterministic order."""
    out = []
    by_plant: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_plant.setdefault(r["plant"], []).append(r)
    for plant, rs in sorted(by_plant.items()):
        rs = sorted(rs, key=lambda r: h8("live-sample:" + r["id"]))
        if len(rs) <= n:
            out += rs
            continue
        strata = {"ROUTINE": [r for r in rs if r["routine"] == "ROUTINE"], "OTHER": [r for r in rs if r["routine"] != "ROUTINE"]}
        want = {"ROUTINE": n // 2, "OTHER": n - n // 2}
        short = {k: max(0, want[k] - len(v)) for k, v in strata.items()}
        want = {"ROUTINE": want["ROUTINE"] + short["OTHER"], "OTHER": want["OTHER"] + short["ROUTINE"]}
        for k, pool in strata.items():
            by_area: dict[str, list[dict[str, Any]]] = {}
            for r in pool:
                by_area.setdefault(r["area"] or "-", []).append(r)
            picked: list[dict[str, Any]] = []
            while len(picked) < want[k] and any(by_area.values()):
                for a in sorted(by_area):
                    if by_area[a] and len(picked) < want[k]:
                        picked.append(by_area[a].pop(0))
            out += picked
    return out


def v2_summary(v2: dict[str, Any]) -> dict[str, Any]:
    e = v2["entry"]
    hz = v2.get("hazards") or []
    return {
        "status": e.get("status"), "state": e.get("state"),
        "inherent": e.get("inherent"), "residual": e.get("residual"), "inherentOnly": e.get("inherentOnly"),
        "tolerability": sorted({f"{h['tolerability']['state']}/{h['tolerability'].get('route') or '-'}" for h in hz if h.get("tolerability")}),
        "blockers": sorted(rv.blocker_codes(v2)), "canSubmit": v2.get("canSubmit"),
        "recommendations": len(v2.get("recommendations") or []), "hazards": len(hz),
        "hiraType": v2.get("hiraType"), "calibrated": v2.get("calibrated"), "ratingBasis": v2.get("ratingBasis"),
        "banner": (v2.get("scope") or {}).get("banner") if isinstance(v2.get("scope"), dict) else v2.get("scopeBanner"),
        "criticalNoEvidence": [h["name"] for h in hz if (h.get("residual") or {}).get("level") == "CRITICAL"
                               and not ((v2.get("evidence") or {}).get("items") or [])],
    }


def worst_reason(v2: dict[str, Any]) -> str:
    """Why the residual is what it is, from the hazard that drives it."""
    hz = [h for h in v2.get("hazards") or [] if h.get("residual") or h.get("inherent")]
    if not hz:
        return "no hazards rated"
    d = max(hz, key=lambda h: ((h.get("residual") or h.get("inherent") or {}).get("score") or 0))
    cr = d.get("credits") or []
    parts = [f"driver '{d['name'][:50]}' ({d.get('family')}) L{num(d.get('likelihood'))} S{num(d.get('severity'))}"]
    nr = [c for c in cr if c["status"] == "NOT_RELEVANT"]
    nc = [c for c in cr if c["status"] == "NO_CREDIT"]
    ok = [c for c in cr if c["status"] not in ("NOT_RELEVANT", "NO_CREDIT")]
    if not cr:
        parts.append("no existing control recorded")
    if ok:
        parts.append("credited: " + "; ".join(f"'{c['control'][:40]}' {c['hierarchy']} -{c.get('steps', 0)}" for c in ok[:3]))
    if nr:
        parts.append("ruled not relevant: " + "; ".join(f"'{c['control'][:40]}' ({c['reason'][:40]})" for c in nr[:3]))
    if nc:
        parts.append("no credit: " + "; ".join(f"'{c['control'][:40]}' ({c.get('reason', '')[:50]})" for c in nc[:3]))
    return ". ".join(parts)


async def run_entry(db: Any, row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    out: dict[str, Any] = {k: row[k] for k in ("id", "plant", "study", "study_title", "study_status", "status", "routine", "seq", "area",
                                                "matrix", "area_events")}
    t0 = time.perf_counter()
    try:
        v1 = await review_entry(db, row["id"])
        out["v1"] = svc._v1_summary(v1)
        res = v1.get("residual") or {}
        out["v1Model"] = res.get("fromControls")
        out["v1Saved"] = res.get("saved")
        out["v1Ideas"] = len(v1.get("controlIdeas") or [])
    except Exception as exc:  # noqa: BLE001
        out["v1Error"] = f"{type(exc).__name__}: {exc}"[:300]
        await db.rollback()
    out["v1Ms"] = round((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    try:
        v2 = await svc.evaluate_entry(db, row["id"], cfg)
        out["v2Ms"] = round((time.perf_counter() - t0) * 1000)
        v2b = await svc.evaluate_entry(db, row["id"], cfg)  # determinism on live data
        strip = lambda x: json.dumps({k: v for k, v in x.items() if k not in ("schemaReady",)}, sort_keys=True, default=str)  # noqa: E731
        out["deterministic"] = strip(v2) == strip(v2b)
        out["v2"] = v2_summary(v2)
        out["reason"] = worst_reason(v2)
        out["v2Hazards"] = [{"name": h["name"], "family": h.get("family"), "L": num(h.get("likelihood")), "S": num(h.get("severity")),
                             "inherent": (h.get("inherent") or {}).get("level"), "residual": (h.get("residual") or {}).get("level"),
                             "tol": (h.get("tolerability") or {}).get("state"),
                             "credits": [(c["control"][:60], c["hierarchy"], c["status"], c.get("reason", "")[:60]) for c in h.get("credits") or []]}
                            for h in v2.get("hazards") or []]
        out["evidence"] = (v2.get("evidence") or {}).get("items") if isinstance(v2.get("evidence"), dict) else None
        out["_v2raw"] = v2
    except Exception as exc:  # noqa: BLE001
        out["v2Error"] = f"{type(exc).__name__}: {exc}"[:300]
        out["v2Ms"] = round((time.perf_counter() - t0) * 1000)
        await db.rollback()
    await db.rollback()
    db.expunge_all()
    return out


def v1_level(r: dict[str, Any]) -> tuple[str | None, str]:
    m, s = r.get("v1Model") or {}, r.get("v1Saved") or {}
    if m.get("level"):
        return m["level"], "v1 model (controls justify)"
    if s.get("level"):
        return s["level"], "team's saved residual"
    return None, "-"


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "0/0"


def write_csv(path: Path, rows: list[dict[str, Any]], cols: list[str] | None = None) -> None:
    cols = cols or (list(rows[0]) if rows else [])
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v) for k, v in r.items()})


def rerender(args: argparse.Namespace) -> int:
    """Rebuild the report from the saved results: no database access."""
    import re  # noqa: PLC0415

    out_dir = HERE / "out" / "live" / args.label
    saved = json.loads((out_dir / "live_shadow_runs.json").read_text(encoding="utf-8"))
    meta = saved.get("meta")
    if meta is None:  # a run saved before the metadata was added: take it from that run's own files
        txt = (out_dir / "db_row_count_diff.txt").read_text(encoding="utf-8")
        cnt = {m[0]: (None if m[1] == "None" else int(m[1]), None if m[2] == "None" else int(m[2]))
               for m in re.findall(r"^(\w+): (\w+) -> (\w+)$", txt, re.M)}
        md = (out_dir / "live_shadow_report.md").read_text(encoding="utf-8")
        meta = {"poolSize": int(re.search(r"sampled from (\d+) current", md).group(1)),
                "before": {k: v[0] for k, v in cnt.items()}, "after": {k: v[1] for k, v in cnt.items()},
                "totalSeconds": float(re.search(r"Whole run: (\d+) s", md).group(1)), "studyMs": [],
                "statements": int(re.search(r"(\d+) statements were sent", md).group(1)), "refused": []}
    live_db.STATS["statements"], live_db.STATS["refused"] = meta["statements"], meta["refused"]
    render(out_dir, args, [None] * meta["poolSize"], saved["results"], meta["before"], meta["after"], meta["totalSeconds"], meta["studyMs"])
    print(f"re-rendered {out_dir.relative_to(ROOT)} from saved results (no database access)")
    return 0


async def main_async(args: argparse.Namespace) -> int:
    out_dir = HERE / "out" / "live" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    gi = HERE / "out" / "live" / ".gitignore"
    if not gi.exists():
        gi.write_text("# real plant data from the live shadow run: keep out of git\n*\n!.gitignore\n", encoding="utf-8")
    eng, S = live_db.readonly_engine()
    t_start = time.perf_counter()
    async with S() as db:
        await live_db.assert_read_only(db)
        before = await row_counts(db)
        rows = await candidates(db)
        sample = stratified(rows, args.n)
        if args.limit:
            sample = sample[: args.limit]
        cfgs: dict[str, tuple[dict[str, Any], str, str | None]] = {}
        results = []
        for i, row in enumerate(sample, 1):
            if row["plant_id"] not in cfgs:
                cfg, mode, _ = await cal_config.load(db, row["plant_id"])
                configured = cfg.get("rubricPack")
                cfg = dict(cfg)
                cfg["rubricPack"] = configured or args.pack
                cfgs[row["plant_id"]] = (cfg, mode, configured)
                await db.rollback()
            cfg, mode, configured = cfgs[row["plant_id"]]
            r = await run_entry(db, row, cfg)
            r["flagMode"], r["packConfigured"], r["packUsed"] = mode, configured, cfg["rubricPack"]
            results.append(r)
            if i % 20 == 0 or i == len(sample):
                print(f"  {i}/{len(sample)} entries", flush=True)
        await live_db.assert_read_only(db)
        after = await row_counts(db)
    await eng.dispose()
    total_s = time.perf_counter() - t_start

    # ── study-run timing: up to 10 entries of one study, sequential (engine + DB) ──
    by_study: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_study.setdefault(r["study"], []).append(r)
    study_ms = sorted(sum(x.get("v2Ms", 0) for x in rs[:10]) for rs in by_study.values() if len(rs) >= 5)

    for r in results:
        r.pop("_v2raw", None)
    meta = {"poolSize": len(rows), "before": before, "after": after, "totalSeconds": total_s, "studyMs": study_ms,
            "statements": live_db.STATS["statements"], "refused": live_db.STATS["refused"]}
    (out_dir / "live_shadow_runs.json").write_text(json.dumps({"generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
                                                              "label": args.label, "meta": meta, "results": results}, indent=1, default=str,
                                                             ensure_ascii=False), encoding="utf-8")
    render(out_dir, args, rows, results, before, after, total_s, study_ms)
    print(f"{len(results)} entries; statements {live_db.STATS['statements']}, refused {len(live_db.STATS['refused'])}; "
          f"row counts unchanged: {before == after}; wrote {out_dir.relative_to(ROOT)}")
    return 0


def render(out_dir: Path, args: argparse.Namespace, pool: list[dict[str, Any]], results: list[dict[str, Any]], before: dict[str, Any],
           after: dict[str, Any], total_s: float, study_ms: list[int]) -> None:
    ok = [r for r in results if r.get("v2")]
    cal = [r for r in ok if r["v2"]["residual"] or r["v2"]["inherent"]]
    plants = sorted({r["plant"] for r in results})

    # worsening / lowering vs v1
    rows_d = []
    for r in ok:
        v1l, src = v1_level(r)
        v2r = (r["v2"]["residual"] or {}).get("level")
        v2i = (r["v2"]["inherent"] or {}).get("level")
        a, b = band_idx(v1l), band_idx(v2r)
        change = "" if a is None or b is None else ("same" if a == b else (f"worse +{b - a}" if b > a else f"better -{a - b}"))
        saved = (r.get("v1Saved") or {}).get("level")
        rows_d.append({"plant": r["plant"], "study": r["study"], "seq": r["seq"], "area": r["area"], "routine": r["routine"],
                       "status": r["status"], "matrix": r["matrix"], "v1_residual": v1l or "", "v1_source": src, "team_saved_residual": saved or "",
                       "v2_inherent": v2i or "", "v2_residual": v2r or ("Inherent only" if r["v2"]["inherentOnly"] else ""),
                       "residual_change_vs_v1": change,
                       "v2_vs_team_saved": "" if band_idx(saved) is None or b is None else (
                           "same" if b == band_idx(saved) else (f"worse +{b - band_idx(saved)}" if b > band_idx(saved) else f"better -{band_idx(saved) - b}")),
                       "v2_tolerability": " ".join(r["v2"]["tolerability"]),
                       "v1_recs": r.get("v1Ideas", ""), "v2_recs": r["v2"]["recommendations"], "v2_blockers": " ".join(r["v2"]["blockers"]),
                       "reason": r.get("reason", ""), "likely_cause": "", "entry_id": r["id"]})
    for d, r in zip(rows_d, ok):
        if d["residual_change_vs_v1"].startswith("worse"):
            hz = r.get("v2Hazards") or []
            causes = []
            if d["v2_vs_team_saved"] == "same":
                causes.append("v2 shows the team's own saved rating; v1's 'controls justify' figure had lowered it")
            if any(any(c[2] == "NOT_RELEVANT" for c in h["credits"]) for h in hz):
                causes.append("v1 credited controls written for other hazards")
            if any(any(c[2] == "NO_CREDIT" and c[1] == "PPE" for c in h["credits"]) for h in hz):
                causes.append("PPE gets no credit (S>=4 or baseline PPE)")
            if any(not h["credits"] for h in hz if h["residual"] in ("HIGH", "CRITICAL")):
                causes.append("no control recorded for a high hazard")
            if r["v2"].get("ratingBasis") == "WITH_CONTROLS":
                causes.append("imported sheet rating already includes the controls")
            drv = max(hz, key=lambda h: BAND_ALIASES.get(h["residual"] or h["inherent"] or "", -1), default=None)
            m = r.get("v1Model") or {}
            if drv and (drv["L"] or 0) >= 4 and r.get("area_events"):
                causes.append(f"the area's records support L{drv['L']} ({r['area_events']} incidents / near misses in 24 months)")
            if drv and m.get("severity") and drv["S"] and drv["S"] > m["severity"]:
                causes.append(f"v1 lowered severity to S{m['severity']} with controls; v2 keeps S{drv['S']} (controls never cut severity unless "
                              "they eliminate or mitigate)")
            d["likely_cause"] = "; ".join(causes) or "v2 rates per hazard (v1 used one entry-level rating)"
    write_csv(out_dir / "live_shadow_entries.csv", rows_d)

    def dist(fn: Any, src: list[dict[str, Any]]) -> str:  # noqa: ANN401
        c = Counter(k for k in (fn(r) for r in src) if k)
        return ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: BAND_ALIASES.get(kv[0], 9))) or "-"

    worse = [d for d in rows_d if d["residual_change_vs_v1"].startswith("worse")]
    worse2 = [d for d in worse if int(d["residual_change_vs_v1"].split("+")[1]) >= 2]
    worse_team = [d for d in rows_d if d["v2_vs_team_saved"].startswith("worse")]
    explained = [d for d in worse if d["v2_vs_team_saved"] in ("same", "") or d["v2_vs_team_saved"].startswith("better")]
    better2 = [d for d in rows_d if d["residual_change_vs_v1"].startswith("better") and int(d["residual_change_vs_v1"].split("-")[1]) >= 2]
    inherent_only = [r for r in ok if r["v2"]["inherentOnly"]]
    blockers = [r for r in ok if r["v2"]["blockers"]]
    recs = [r["v2"]["recommendations"] for r in ok]
    crit_no_ev = [r for r in ok if r["v2"]["criticalNoEvidence"]]
    crit_empty = [r for r in ok if (r["v2"]["residual"] or {}).get("level") == "CRITICAL" and r["v2"]["inherentOnly"]]
    nondet = [r for r in ok if not r.get("deterministic")]
    errors = [r for r in results if r.get("v1Error") or r.get("v2Error")]
    with_ev = [r for r in ok if r.get("area_events")]
    v2ms = sorted(r["v2Ms"] for r in ok)

    def gate(name: str, good: bool, value: str) -> str:
        return f"| {name} | {'PASS' if good else 'FAIL'} | {value} |"

    L = [f"# Live shadow report ({args.label})", "",
         f"Generated {datetime.now(UTC).isoformat(timespec='seconds')} against the database in safeops_360_bakend/.env "
         "(Supabase, the production project), READ-ONLY. Engine "
         f"{cal_config.ENGINE_VERSION}. {len(results)} entries sampled from {len(pool)} current entries "
         f"({', '.join(f'{p}: {sum(1 for r in results if r['plant'] == p)}' for p in plants)}).", "",
         "Read-only proof: the connection ran with `transaction_read_only = on` (checked before and after), "
         f"{live_db.STATS['statements']} statements were sent, {len(live_db.STATS['refused'])} refused by the client guard, and the row counts "
         f"below are {'identical' if before == after else '**DIFFERENT**'} before and after. Step 5.2 (store v1/v2 in "
         "hira_copilot_shadow_runs) was not done because it is a write; the per-entry results are in live_shadow_runs.json.", "",
         "Settings: " + "; ".join(sorted({f"{r['plant']}: flag {r['flagMode']}, pack configured {r['packConfigured'] or 'none'}, run with "
                                          f"{r['packUsed']}" for r in results})) + ".", "",
         "## Gates on live data (section 10)", "", "| Gate | Result | Value |", "|---|---|---|",
         gate("No Critical residual without evidence", not crit_no_ev, f"{len(crit_no_ev)} entries"),
         gate("Average recommendations <= 3.0, none above 5", (statistics.mean(recs) if recs else 0) <= 3.0 and max(recs or [0]) <= 5,
              f"avg {statistics.mean(recs) if recs else 0:.2f}, max {max(recs or [0])}"),
         gate("No 'Residual Critical' with empty controls", not crit_empty, f"{len(crit_empty)} entries"),
         gate("At most 10% of entries worsen their residual band vs v1", len(worse) <= 0.1 * max(1, len(rows_d)), pct(len(worse), len(rows_d))),
         gate("Every worsening has a listed reason", all(d["reason"] and d["likely_cause"] for d in worse), f"{len(worse)} listed; "
              f"{len(explained)} of them equal (or are below) the team's own saved rating"),
         gate("No 2+ band worsening (blocker until explained)", not worse2, f"{len(worse2)} entries (each listed below with its cause)"),
         gate("Determinism on live data (v2 run twice per entry)", not nondet, f"{len(ok) - len(nondet)}/{len(ok)} identical"),
         gate("No crashes", not errors, f"{len(errors)} entries"), "",
         "## Distributions (v1 vs v2)", ""]

    L.append(f"- v2 inherent: {dist(lambda r: (r["v2"]["inherent"] or {}).get("level"), ok)}")
    L.append(f"- v2 residual: {dist(lambda r: (r["v2"]["residual"] or {}).get("level") or ("Inherent only" if r["v2"]["inherentOnly"] else None), ok)}")
    L.append(f"- v1 residual (model where shown, else the team's saved residual): {dist(lambda r: v1_level(r)[0], ok)}")
    L.append(f"- team's saved residual: {dist(lambda r: (r.get("v1Saved") or {}).get("level"), ok)}")
    tol = Counter(t for r in ok for t in r["v2"]["tolerability"])
    L.append("- v2 hazard tolerability/route: " + ", ".join(f"{k} {v}" for k, v in tol.most_common()))
    L += [f"- Recommendations per entry: v1 {statistics.mean([r.get('v1Ideas', 0) for r in ok]) if ok else 0:.2f} control ideas, "
          f"v2 {statistics.mean(recs) if recs else 0:.2f} recommendations (max {max(recs or [0])})",
          f"- Entries with blockers (v2): {pct(len(blockers), len(ok))}: " + ", ".join(f"{k} {v}" for k, v in Counter(b for r in blockers for b in r['v2']['blockers']).most_common(6)),
          f"- 'Inherent only, controls not captured': {pct(len(inherent_only), len(ok))}",
          f"- Residual worse than the team's own saved rating: {pct(len(worse_team), len(rows_d))}"
          + (" (" + ", ".join(f"{k} {v}" for k, v in Counter(d['plant'] for d in worse_team).most_common()) + ")" if worse_team else ""),
          f"- Entries in areas with recorded incidents / near misses (24 months): {len(with_ev)}",
          f"- Study types seen: {dict(Counter(r['v2']['hiraType'] for r in ok))}", ""]
    for p in plants:
        po = [r for r in ok if r["plant"] == p]
        a = dist(lambda r: (r["v2"]["residual"] or {}).get("level") or ("Inherent only" if r["v2"]["inherentOnly"] else None), po)
        L.append(f"- {p}: v2 residual {a} | v1 residual {dist(lambda r: v1_level(r)[0], po)}")
    L += ["", f"## Entries whose residual band worsens under v2 ({len(worse)})", "",
          "| Plant | Study / S.No | Activity area | v1 | v2 | Change | Team's saved | Likely cause | Reason (driving hazard) |", "|---|---|---|---|---|---|---|---|---|"]
    for d in sorted(worse, key=lambda d: (-int(d["residual_change_vs_v1"].split("+")[1]), d["plant"], str(d["study"]), d["seq"] or 0))[:80]:
        L.append(f"| {d['plant']} | {d['study']} / {d['seq']} | {d['area']} | {d['v1_residual']} | {d['v2_residual']} | {d['residual_change_vs_v1']} | "
                 f"{d['team_saved_residual'] or '-'} | {d['likely_cause']} | {str(d['reason'])[:260].replace('|', '/')} |")
    if len(worse) > 80:
        L.append(f"| ... | {len(worse) - 80} more in live_shadow_entries.csv | | | | | | | |")
    L += ["", f"## Entries where v2 lowers the band by 2 or more ({len(better2)})", ""]
    L += [f"- {d['plant']} {d['study']} / {d['seq']}: v1 {d['v1_residual']} -> v2 {d['v2_residual']}. {str(d['reason'])[:200]}" for d in better2] or ["None."]
    if crit_no_ev:
        L += ["", f"## Critical residual without evidence ({len(crit_no_ev)})", ""]
        L += [f"- {r['plant']} {r['study']} / {r['seq']}: {', '.join(r['v2']['criticalNoEvidence'][:3])}. {r.get('reason', '')[:220]}" for r in crit_no_ev[:40]]
    if errors:
        L += ["", "## Errors", ""] + [f"- {r['plant']} {r['study']} / {r['seq']}: {r.get('v1Error') or ''} {r.get('v2Error') or ''}" for r in errors]
    L += ["", "## Performance (engine + database, read-only, from this machine)", "",
          f"- v2 per entry: p50 {v2ms[len(v2ms) // 2] if v2ms else 0} ms, p95 {v2ms[int(0.95 * (len(v2ms) - 1))] if v2ms else 0} ms, "
          f"max {v2ms[-1] if v2ms else 0} ms; over 30 s: {sum(1 for x in v2ms if x > 30000)}",
          f"- A 10-entry study (v2, sequential): " + (f"p50 {study_ms[len(study_ms) // 2] / 1000:.1f} s, max {study_ms[-1] / 1000:.1f} s" if study_ms else "n/a"),
          f"- Whole run: {total_s:.0f} s for {len(results)} entries (v1 + v2 twice each).", "",
          "## Row counts before / after (read-only proof)", "", "| Table | Before | After |", "|---|---|---|"]
    L += [f"| {t} | {before.get(t)} | {after.get(t)} |" for t in COUNT_TABLES]
    (out_dir / "live_shadow_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out_dir / "db_row_count_diff.txt").write_text(
        "Row counts before / after the read-only live shadow run\n\n" + "\n".join(f"{t}: {before.get(t)} -> {after.get(t)}" for t in COUNT_TABLES)
        + f"\n\nIdentical: {before == after}\n", encoding="utf-8")

    # ── blind scoring (section 5.4): 5 low / 5 medium / 5 high by worst hazard severity, no engine output in the sheet ──
    strata: dict[str, list[dict[str, Any]]] = {"low": [], "medium": [], "high": []}
    for r in sorted(ok, key=lambda r: h8("blind:" + r["id"])):
        s = max([h["S"] or 0 for h in r.get("v2Hazards") or []] or [0])
        k = "low" if s <= 3 else ("medium" if s == 4 else "high")
        if len(strata[k]) < 5 and r["v2"]["hazards"]:
            strata[k].append(r)
    sheet, key = [], []
    n = 0
    for k, rs in strata.items():
        for r in rs:
            n += 1
            hz = r.get("v2Hazards") or []
            sheet.append({"blind_id": f"B{n:02d}", "plant": r["plant"], "area": r["area"], "routine": r["routine"],
                          "activity": f"{r['study_title'] or ''} / S.No {r['seq']}",
                          "hazards": " | ".join(h["name"] for h in hz),
                          "existing_controls": " | ".join(sorted({c[0] for h in hz for c in h["credits"]})),
                          "area_incident_summary": f"{r['area_events']} incidents / near misses in this area in 24 months",
                          "hse_inherent": "", "hse_residual": "", "hse_acceptable_yes_no": "", "hse_notes": ""})
            key.append({"blind_id": f"B{n:02d}", "stratum": k, "entry_id": r["id"], "v2_inherent": (r["v2"]["inherent"] or {}).get("level"),
                        "v2_residual": (r["v2"]["residual"] or {}).get("level") or "Inherent only", "v2_tolerability": " ".join(r["v2"]["tolerability"]),
                        "v1_residual": v1_level(r)[0], "team_saved_residual": (r.get("v1Saved") or {}).get("level")})
    write_csv(out_dir / "blind_scoring_sheet.csv", sheet)
    write_csv(out_dir / "blind_scoring_key.csv", key)



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", default="run")
    ap.add_argument("--n", type=int, default=100, help="entries per plant (all entries if a plant has fewer)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N sampled entries (smoke test)")
    ap.add_argument("--pack", default="STEEL_HEAVY", help="rubric pack for plants with none configured")
    ap.add_argument("--rerender", action="store_true", help="rebuild the report from the saved results; no database access")
    args = ap.parse_args(argv)
    return rerender(args) if args.rerender else asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
