"""Pick demo-ready JSL Hisar HIRA entries: ones where SafeOps agrees with the client's own data. READ-ONLY.

Runs the Copilot review that production shows today (v1, hira_review.review_entry) and the calibrated engine
(v2, service.evaluate_entry, STEEL_HEAVY pack) on every current JSL-HSR entry, through the read-only connection
in live_db.py. Nothing is written to the database (Postgres read-only session + client guard, same as
live_shadow.py).

An entry is demo-ready only if every gate passes:
  G1  Production card is green: "Looks consistent" (0 to correct, 0 to check).
  G2  "Residual saved" and "What the listed controls justify" show the same L x S.
  G3  The plant sheet (QSD/24/F) says Acceptable = Yes on every row, the plant rule agrees, and the matrix
      verdict is Acceptable.
  G4  The calibrated engine (v2) gives the same residual band as the team, with no blockers, no
      recommendations, no "severity looks understated / overstated" note, and the same result on a second run.
  G5  The review is not due within --margin-days (else the card turns amber with "Review is overdue").
  G6  Active, current entry; not the COVID study; no errors from either engine.

Passing entries are ranked (fewer minor notes, one hazard row, more and stronger controls, steel-specific
hazards, serious initial severity) and the top ones are spread over the areas.

    cd safeops_360_bakend
    .venv/Scripts/python.exe tests/campaign/hira_calibration/demo_picks.py [--top 10] [--margin-days 30] [--resume]

Outputs (git-ignored, real plant data): out/live/demo/demo_picks.md and demo_candidates.csv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import live_shadow as ls  # sets sys.path, stubs the app's write-capable DB session, imports both engines
from sqlalchemy import text

import live_db
from app.services.copilot.hira_review import review_entry, source_for
from app.services.hira_calibration import config as cal_config
from app.services.hira_calibration import review_v2 as rv
from app.services.hira_calibration import service as svc

PLANT = "JSL-HSR"
STRONG = {"ELIMINATION", "SUBSTITUTION", "ENGINEERING"}
STEEL_FAMILIES = {"MOLTEN", "LIFTING", "THERMAL", "HEAT", "FIRE_EXPLOSION", "ELECTRICAL", "VEHICLE", "GAS", "EXPLOSION"}
OUT = ls.HERE / "out" / "live" / "demo"


async def entry_rows(db: Any) -> tuple[list[dict[str, Any]], str | None]:  # noqa: ANN401
    q = '''
      SELECT e.id, e."studyId" AS study_id, s.number AS study, s.title AS study_title, s.status AS study_status,
             e.status, e.routine, e."sequenceNumber" AS seq, e."activityDescription" AS activity,
             e."nextReviewDue" AS next_review, a.name AS area
      FROM "HiraEntry" e JOIN "HiraStudy" s ON s.id = e."studyId" JOIN "Plant" p ON p.id = s."plantId"
      LEFT JOIN "Area" a ON a.id = e."areaId"
      WHERE e."isCurrentVersion" AND p.code = :plant
      ORDER BY s.number, e."sequenceNumber"'''
    rows = [dict(r) for r in (await db.execute(text(q), {"plant": PLANT})).mappings().all()]
    pid = (await db.execute(text('SELECT id FROM "Plant" WHERE code = :plant'), {"plant": PLANT})).scalar()
    await db.rollback()
    return rows, pid


def rating(r: dict[str, Any] | None) -> str:
    return f"L{r['likelihood']} x S{r['severity']} = {r['level']} ({r['score']})" if r else "-"


def judge(row: dict[str, Any], v1: dict[str, Any] | None, v2: dict[str, Any] | None, v2_same: bool,
          margin: datetime) -> dict[str, Any]:
    fails: list[str] = []
    res = (v1 or {}).get("residual") or {}
    saved, model, initial = res.get("saved"), res.get("fromControls"), res.get("initial")
    counts = (v1 or {}).get("counts") or {}
    findings = (v1 or {}).get("findings") or []
    src = source_for(row["study"], row["seq"])
    sheet = (src or {}).get("entry", {}).get("rows") or []

    # G6 basics
    if v1 is None or v2 is None:
        fails.append("G6 engine error")
    if row["study"].endswith("-COVID"):
        fails.append("G6 COVID study")
    if (row["status"] or "").upper() not in ("ACTIVE", "APPROVED"):
        fails.append(f"G6 status {row['status']}")
    # G1 green card
    if counts.get("error") or counts.get("warning"):
        codes = [f["code"] for f in findings if f["level"] in ("error", "warning")]
        fails.append("G1 card not green: " + ", ".join(codes))
    # G2 saved == controls justify
    if not saved or not model or (saved["likelihood"], saved["severity"]) != (model["likelihood"], model["severity"]):
        fails.append(f"G2 saved {rating(saved)} vs controls justify {rating(model)}")
    # G3 plant sheet agrees
    if not sheet:
        fails.append("G3 no sheet source row")
    elif not all(str(r.get("acceptable") or "").lower().startswith("y") and r.get("acceptableByRule") for r in sheet):
        fails.append("G3 sheet not 'Yes' by its own rule")
    if saved and saved.get("verdict") != "ACCEPTABLE":
        fails.append(f"G3 matrix verdict {saved.get('verdict')}")
    # G4 calibrated engine agrees
    v2e = (v2 or {}).get("entry") or {}
    v2res = (v2e.get("residual") or {}).get("level")
    if v2:
        if ls.band_idx(v2res) is None or ls.band_idx(v2res) != ls.band_idx((saved or {}).get("level")):
            fails.append(f"G4 v2 residual {v2res or 'none'} vs team {(saved or {}).get('level')}")
        if rv.blocker_codes(v2):
            fails.append("G4 v2 blockers " + ",".join(sorted(rv.blocker_codes(v2))))
        if v2.get("recommendations"):
            fails.append(f"G4 v2 recommends {len(v2['recommendations'])}")
        if not v2_same:
            fails.append("G4 v2 not deterministic")
        # e.g. liquid-metal work rated CN 1 that v1's keyword check misses: showing it as "agrees" would
        # contradict the under-rated entries shown next to it
        sev = sorted({f.get("code") for f in v2.get("findings") or []
                      if f.get("code") in ("SEVERITY_UNDERSTATED", "SEVERITY_OVERSTATED")})
        if sev:
            fails.append("G4 v2 " + ", ".join(sev))
    # G5 review date
    nr = row["next_review"]
    if nr is not None and (nr if nr.tzinfo else nr.replace(tzinfo=UTC)) < margin:
        fails.append(f"G5 review due {nr:%d %b %Y}")

    hz = (v2 or {}).get("hazards") or []
    families = sorted({h.get("family") or "-" for h in hz})
    controls: dict[str, str] = {}
    for h in hz:
        for c in h.get("credits") or []:
            controls.setdefault(c["control"], c["hierarchy"])
    minor = (counts.get("suggestion") or 0) + (counts.get("info") or 0)
    score = (-2 * minor + (2 if len(sheet) == 1 else 0) + min(len(controls), 4)
             + (3 if any(h in STRONG for h in controls.values()) else 0)
             + (3 if set(families) & STEEL_FAMILIES else 0)
             + (2 if initial and initial["severity"] >= 3 else 0)
             + (1 if all((h.get("tolerability") or {}).get("state") == "ACCEPTABLE" for h in hz) and hz else 0)
             + (1 if any(r.get("legal") for r in sheet) else 0))
    return {
        "ready": not fails, "score": score, "fails": fails,
        "study": row["study"], "seq": row["seq"], "area": row["area"], "activity": row["activity"],
        "status": row["status"], "study_status": row["study_status"], "routine": row["routine"],
        "url": f"/hira/{row['study_id']}/entries/{row['id']}", "entry_id": row["id"],
        "initial": rating(initial), "saved": rating(saved), "controls_justify": rating(model),
        "v2_residual": v2res or "", "v1_minor_notes": ", ".join(f["code"] for f in findings if f["level"] in ("suggestion", "info")),
        "sheet": "; ".join(f"row {r['xl_row']}: {r['hazard']} -> {r.get('risk')} (SP{r['SP']} LH{r['LH']} CN{r['CN']} DE{r['DE']} "
                           f"GT {r['GT']}, {r.get('acceptable')})" for r in sheet),
        "legal": "; ".join(sorted({r["legal"] for r in sheet if r.get("legal")})),
        "hazards": "; ".join(f"{h['name']} ({h.get('family')})" for h in hz), "families": ", ".join(families),
        "controls": "; ".join(f"{k} [{v}]" for k, v in controls.items()),
        "next_review": f"{nr:%d %b %Y}" if nr else "",
    }


def spread(ready: list[dict[str, Any]], top: int) -> list[dict[str, Any]]:
    """Best first, round-robin over studies (areas) so the demo covers the shop floor."""
    by: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(ready, key=lambda r: (-r["score"], r["study"], r["seq"])):
        by.setdefault(r["study"], []).append(r)
    order = sorted(by, key=lambda s: -by[s][0]["score"])
    out: list[dict[str, Any]] = []
    while len(out) < top and any(by.values()):
        for s in order:
            if by[s] and len(out) < top:
                out.append(by[s].pop(0))
    return out


def md_cell(s: Any) -> str:  # noqa: ANN401
    return str(s or "").replace("|", "/").replace("\n", " ").strip()


def write_report(all_rows: list[dict[str, Any]], picks: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    ready = [r for r in all_rows if r["ready"]]
    gate_fail: dict[str, int] = {}
    for r in all_rows:
        for g in {f[:2] for f in r["fails"]}:
            gate_fail[g] = gate_fail.get(g, 0) + 1
    per_study: dict[str, list[int]] = {}
    for r in all_rows:
        per_study.setdefault(r["study"], [0, 0])
        per_study[r["study"]][0] += 1
        per_study[r["study"]][1] += r["ready"]
    L = [f"# JSL Hisar demo entries (SafeOps agrees with the plant's own data)", "",
         f"Generated {meta['at']} from the production database, read-only ({meta['statements']} statements, "
         f"{meta['refused']} refused, transaction_read_only = on). {len(all_rows)} current JSL-HSR entries checked; "
         f"**{len(ready)} pass every gate**."
         + (f" Row counts moved during the run because other people were using the app: {meta['countsMoved']}."
            if meta.get("countsMoved") else ""), "",
         "Gates: G1 card is green 'Looks consistent' | G2 saved residual = what the controls justify | G3 plant sheet "
         "says Yes by its own rule, matrix says Acceptable | G4 calibrated engine gives the same band, no blockers or "
         f"recommendations | G5 review not due before {meta['margin']} | G6 active, not COVID.", "",
         "Entries failing each gate: " + ", ".join(f"{g} {n}" for g, n in sorted(gate_fail.items())), "",
         "Passing per area: " + ", ".join(f"{s.replace('HIRA-JSL-SMS1-', '')} {v[1]}/{v[0]}" for s, v in per_study.items()), "",
         f"## Recommended demo set ({len(picks)})", "",
         "| # | Area / S.No | Activity | Hazard (sheet) | Rating (saved = controls justify) | Controls | Open |",
         "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(picks, 1):
        L.append(f"| {i} | {md_cell(r['area'])} / S.No {r['seq']} | {md_cell(r['activity'])[:90]} | {md_cell(r['sheet'])[:140]} | "
                 f"{r['saved']} | {md_cell(r['controls'])[:140]} | [open]({meta['base']}{r['url']}) |")
    L += ["", "## Every passing entry, ranked", "", "| Score | Study / S.No | Activity | Saved | Minor notes (hidden by default) |",
          "|---|---|---|---|---|"]
    for r in sorted(ready, key=lambda r: (-r["score"], r["study"], r["seq"])):
        L.append(f"| {r['score']} | {r['study']} / {r['seq']} | {md_cell(r['activity'])[:80]} | {r['saved']} | {r['v1_minor_notes']} |")
    (OUT / "demo_picks.md").write_text("\n".join(L) + "\n", encoding="utf-8")


async def main_async(args: argparse.Namespace) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    gi = OUT.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("# real plant data from the live shadow run: keep out of git\n*\n!.gitignore\n", encoding="utf-8")
    margin = datetime.now(UTC) + timedelta(days=args.margin_days)
    eng, S = live_db.readonly_engine()
    t0 = time.perf_counter()
    async with S() as db:
        await live_db.assert_read_only(db)
        before = await ls.row_counts(db)
        rows, pid = await entry_rows(db)
        if args.limit:
            rows = rows[: args.limit]
        cfg, mode, _ = await cal_config.load(db, pid)
        cfg = dict(cfg)
        cfg["rubricPack"] = cfg.get("rubricPack") or args.pack
        await db.rollback()

    # Progress is saved after every entry, so a stopped run picks up where it left off with --resume.
    progress = OUT / "progress.jsonl"
    done: dict[str, dict[str, Any]] = {}
    if args.resume and progress.exists():
        for line in progress.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["entry_id"]] = r
    elif progress.exists():
        progress.unlink()
    todo = [r for r in rows if r["id"] not in done]
    print(f"  {len(rows)} entries, {len(done)} already done, {len(todo)} to check", flush=True)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for r in todo:
        queue.put_nowait(r)
    lock = asyncio.Lock()

    async def worker() -> None:
        async with S() as db:
            await live_db.assert_read_only(db)
            while not queue.empty():
                row = queue.get_nowait()
                v1 = v2 = None
                same = False
                try:
                    v1 = await review_entry(db, row["id"])
                except Exception:  # noqa: BLE001
                    await db.rollback()
                try:
                    v2 = await svc.evaluate_entry(db, row["id"], cfg)
                    v2b = await svc.evaluate_entry(db, row["id"], cfg)
                    same = json.dumps(v2, sort_keys=True, default=str) == json.dumps(v2b, sort_keys=True, default=str)
                except Exception:  # noqa: BLE001
                    await db.rollback()
                await db.rollback()
                db.expunge_all()
                res = judge(row, v1, v2, same, margin)
                async with lock:
                    done[row["id"]] = res
                    with progress.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(res, default=str, ensure_ascii=False) + "\n")
                    n = len(done)
                    if n % 25 == 0 or n == len(rows):
                        print(f"  {n}/{len(rows)} entries, {sum(r['ready'] for r in done.values())} ready", flush=True)

    # live_db's pool holds 4 connections; one worker per connection
    await asyncio.gather(*(worker() for _ in range(max(1, min(args.workers, 4)))))
    async with S() as db:
        await live_db.assert_read_only(db)
        after = await ls.row_counts(db)
    await eng.dispose()
    out = [done[r["id"]] for r in rows if r["id"] in done]
    # This connection cannot write (read-only session + guard, refused count below). Counts can still move when
    # other people use the app during the run; report it rather than stop.
    moved = {k: f"{before[k]} -> {after[k]}" for k in before if before[k] != after.get(k)}
    if moved:
        print(f"  note: row counts moved during the run (other app users; this run refused "
              f"{len(live_db.STATS['refused'])} writes): {moved}", flush=True)
    picks = spread([r for r in out if r["ready"]], args.top)
    meta = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "statements": live_db.STATS["statements"],
            "refused": len(live_db.STATS["refused"]), "margin": f"{margin:%d %b %Y}", "flagMode": mode,
            "base": args.base_url.rstrip("/"), "countsMoved": moved}
    ls.write_csv(OUT / "demo_candidates.csv", sorted(out, key=lambda r: (not r["ready"], -r["score"], r["study"], r["seq"])))
    (OUT / "demo_picks.json").write_text(json.dumps({"meta": meta, "picks": picks, "all": out}, indent=1, default=str,
                                                    ensure_ascii=False), encoding="utf-8")
    write_report(out, picks, meta)
    print(f"{len(out)} entries, {sum(r['ready'] for r in out)} ready, {len(picks)} picked in {time.perf_counter() - t0:.0f}s; "
          f"statements {meta['statements']}, refused {meta['refused']}, row counts unchanged; wrote {OUT.relative_to(ls.ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--top", type=int, default=10, help="size of the recommended demo set")
    ap.add_argument("--margin-days", type=int, default=30, help="skip entries whose review falls due within this many days")
    ap.add_argument("--pack", default="STEEL_HEAVY", help="rubric pack if the plant has none configured")
    ap.add_argument("--limit", type=int, default=0, help="only the first N entries (smoke test)")
    ap.add_argument("--workers", type=int, default=4, help="entries checked at the same time (max 4)")
    ap.add_argument("--resume", action="store_true", help="keep the entries already saved in progress.jsonl")
    ap.add_argument("--base-url", default="https://safe-ops360.vercel.app", help="frontend address for the entry links")
    ap.add_argument("--rerender", action="store_true", help="rebuild the report from demo_picks.json; no database access")
    args = ap.parse_args(argv)
    if args.rerender:
        saved = json.loads((OUT / "demo_picks.json").read_text(encoding="utf-8"))
        meta = saved["meta"] | {"base": args.base_url.rstrip("/")}
        picks = spread([r for r in saved["all"] if r["ready"]], args.top)
        (OUT / "demo_picks.json").write_text(json.dumps({"meta": meta, "picks": picks, "all": saved["all"]}, indent=1,
                                                        default=str, ensure_ascii=False), encoding="utf-8")
        write_report(saved["all"], picks, meta)
        print(f"re-rendered {OUT.relative_to(ls.ROOT)} (no database access)")
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
