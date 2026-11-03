"""Read-only evidence audit for one HIRA entry (round 2 I).

For a live entry, lists every incident / near miss of the plant around the
evidence window, and for each one: whether it is in the window, whether it is in
the entry's area scope, which hazard families it matches, and so whether it
counts. Then, per hazard of the entry: the family, the events that counted for
that family, and the calibrated inherent likelihood with its rationale.

This answers: "the earlier run found 6 crane-related events in the area of
Entry #1. Are they in-window, in the right area, and do they lift only the
lifting hazards?"

The script only reads. It opens a READ ONLY transaction and rolls it back; it
never writes to the database and does not need the calibration DDL.

    cd safeops_360_bakend
    venv/Scripts/python.exe scripts/hira_evidence_breakdown.py                # Entry #1 of HIRA-2026-NW-007
    venv/Scripts/python.exe scripts/hira_evidence_breakdown.py --study HIRA-2026-NW-007 --seq 1
    venv/Scripts/python.exe scripts/hira_evidence_breakdown.py --entry-id <HiraEntry.id> --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.services.hira_calibration import config as cal_config  # noqa: E402
from app.services.hira_calibration import loader, rubric_packs  # noqa: E402
from app.services.hira_calibration import review_v2 as rv  # noqa: E402

DEFAULT_STUDY = "HIRA-2026-NW-007"
DEFAULT_SEQ = 1


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


async def _entry_id(db: Any, args: argparse.Namespace) -> str | None:
    from app.models.hira import HiraEntry, HiraStudy  # noqa: PLC0415

    if args.entry_id:
        return args.entry_id
    return (
        await db.execute(
            select(HiraEntry.id)
            .join(HiraStudy, HiraStudy.id == HiraEntry.studyId)
            .where(HiraStudy.number == args.study, HiraEntry.sequenceNumber == args.seq)
            .where(HiraEntry.isCurrentVersion.is_(True))
        )
    ).scalar_one_or_none()


async def breakdown(db: Any, entry_id: str, pack: str | None) -> dict[str, Any]:
    from app.models.incident import Incident  # noqa: PLC0415
    from app.models.near_miss import NearMiss  # noqa: PLC0415
    from app.models.plant import Area  # noqa: PLC0415

    plant_id = await loader_plant(db, entry_id)
    cfg, mode, _ = await cal_config.load(db, plant_id)
    cfg, pack_meta = rubric_packs.apply_pack(cfg, pack or cfg.get("rubricPack"))
    built = await loader.build(db, entry_id, cfg)
    entry, ev = built["input"], built["evidence"]
    area_id = getattr(built.get("entry"), "areaId", None)
    months = cfg["likelihood"]["evidenceWindowMonths"]
    scope = (cfg.get("evidence") or {}).get("areaScope", "AREA")
    groups = (cfg.get("evidence") or {}).get("areaGroups") or {}
    now = datetime.now(UTC)
    since = now - timedelta(days=30 * months)

    # every event of the plant in a wider window (so out-of-window and other-area
    # events are visible too, with the reason they do not count)
    wide = since - timedelta(days=365)
    inc = (
        await db.execute(
            select(
                Incident.date,
                Incident.number,
                Incident.description,
                Incident.activityBeingPerformed,
                Incident.areaId,
            ).where(Incident.plantId == plant_id, Incident.date >= wide)
        )
    ).all()
    nm = (
        await db.execute(
            select(
                NearMiss.date,
                NearMiss.number,
                NearMiss.description,
                NearMiss.activity,
                NearMiss.activityBeingPerformed,
                NearMiss.hazardCategory,
                NearMiss.areaId,
            ).where(NearMiss.plantId == plant_id, NearMiss.date >= wide)
        )
    ).all()
    events = [loader.Event(d, f"incident {n}", a, f"{desc} {act or ''}") for d, n, desc, act, a in inc]
    events += [
        loader.Event(d, f"near miss {n}", a, f"{desc} {act or ''} {act2 or ''}", cat)
        for d, n, desc, act, act2, cat, a in nm
    ]
    area_ids = sorted({x.area_id for x in events if x.area_id} | ({area_id} if area_id else set()))
    names = (
        dict((await db.execute(select(Area.id, Area.name).where(Area.id.in_(area_ids)))).all())
        if area_ids
        else {}
    )

    rows = []
    for x in sorted(events, key=lambda x: (_aware(x.when) if x.when else wide, x.label), reverse=True):
        in_window = x.when is not None and _aware(x.when) >= since
        in_area = loader.area_in_scope(x.area_id, area_id, scope, groups)
        fams = sorted(loader._event_families(x.text, x.category))
        rows.append(
            {
                "event": x.label,
                "date": f"{x.when:%d %b %Y}" if x.when else None,
                "area": names.get(x.area_id, x.area_id),
                "inWindow": in_window,
                "inAreaScope": in_area,
                "families": fams,
                "counts": in_window and in_area and bool(fams),
                "why": "counts"
                if in_window and in_area and fams
                else "; ".join(
                    w
                    for w, bad in (
                        (f"outside the {months}-month window", not in_window),
                        (f"other area ({scope.lower()} scope)", not in_area),
                        ("no hazard family matched", not fams),
                    )
                    if bad
                ),
            }
        )

    review = rv.evaluate(entry, cfg=cfg, rule=built["rule"], evidence=ev, plant=built["plant"])
    hazards = []
    for h in review["hazards"]:
        fam = h["family"]
        f = ev.by_family.get(fam) or {}
        hazards.append(
            {
                "hazard": h["name"],
                "family": fam,
                "eventsInWindow": f.get("window", 0),
                "events12m": f.get("12m", 0),
                "controlFailures": f.get("failures", []),
                "eventsUsed": f.get("items", []),
                "inherentLikelihood": (h.get("likelihood") or {}).get("value"),
                "likelihoodReason": (h.get("likelihood") or {}).get("reason"),
                "inherent": h.get("inherent"),
            }
        )
    return {
        "entryId": entry_id,
        "label": entry.label,
        "activity": entry.activity,
        "plantId": plant_id,
        "area": names.get(area_id, area_id),
        "mode": mode,
        "pack": (pack_meta or {}).get("code"),
        "windowMonths": months,
        "areaScope": scope,
        "asOf": f"{now:%d %b %Y}",
        "events": rows,
        "byFamily": {
            k: {"window": v["window"], "12m": v["12m"], "items": v["items"]} for k, v in ev.by_family.items()
        },
        "hazards": hazards,
    }


async def loader_plant(db: Any, entry_id: str) -> str | None:
    from app.services.hira_calibration.service import plant_of_entry  # noqa: PLC0415

    return await plant_of_entry(db, entry_id)


def render(out: dict[str, Any]) -> str:
    lines = [
        f"{out['label']} — {out['activity']}",
        f"plant {out['plantId']}, area {out['area']}; window {out['windowMonths']} months, scope "
        f"{out['areaScope']}, pack {out['pack']}, as of {out['asOf']} (calibration mode {out['mode']})",
        "",
        "Events (plant, window + 12 months):",
    ]
    for e in out["events"]:
        lines.append(
            f"  {'COUNTS ' if e['counts'] else 'ignored'} {e['event']} ({e['date']}) area {e['area']} "
            f"families {', '.join(e['families']) or '-'}{'' if e['counts'] else ' — ' + e['why']}"
        )
    lines += ["", "Hazards:"]
    for h in out["hazards"]:
        lines.append(
            f"  {h['hazard']} [{h['family']}]: {h['eventsInWindow']} event(s) in window, "
            f"{h['events12m']} in 12 months -> L{h['inherentLikelihood']}. {h['likelihoodReason']}"
        )
        for x in h["eventsUsed"]:
            lines.append(f"      {x}")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            entry_id = await _entry_id(db, args)
            if not entry_id:
                print(f"no current entry {args.study} #{args.seq}")
                return 1
            out = await breakdown(db, entry_id, args.pack)
        finally:
            await db.rollback()
    print(render(out))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, default=str) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only HIRA evidence audit for one entry")
    ap.add_argument("--entry-id")
    ap.add_argument("--study", default=DEFAULT_STUDY)
    ap.add_argument("--seq", type=int, default=DEFAULT_SEQ)
    ap.add_argument(
        "--pack", default="STEEL_HEAVY", help="rubric pack to evaluate with (default STEEL_HEAVY)"
    )
    ap.add_argument("--json", help="also write the breakdown as JSON to this path")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
