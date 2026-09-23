"""Scale + endpoint diagnostic for the Kaizen gap-closure build.

Checklist item 8: run the search / filter / export against a plant with 50+
records, not the 4 currently visible. Seeds 60 throwaway records inside ONE
transaction, exercises the real router functions against them, and ROLLS BACK.
Nothing is committed.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/verify/kaizen_scale.py
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app.models  # noqa: F401
from app.core.db import AsyncSessionLocal
from app.core.soft_delete import register_default_governed
from app.models.business_excellence import BeKaizen
from app.models.plant import Plant
from app.models.user import User
from app.routers import business_excellence as router
from app.services import business_excellence as be
from app.services.access_scope import system_scope

register_default_governed()

SEED_N = 60
CATEGORIES = ["SAFETY", "QUALITY", "COST", "DELIVERY", "PRODUCTIVITY"]
STATUSES = [
    "SUBMITTED", "SCREENED", "APPROVED", "IN_IMPLEMENTATION",
    "IMPLEMENTED", "VERIFIED", "CLOSED",
]
TOPICS = [
    "conveyor guard interlock", "needle-change downtime", "thread trolley position",
    "collar attach jig", "packing bench reach", "bobbin changeover time",
    "cutting table lighting", "steam iron leak", "fabric roll handling",
    "line 4 air leak",
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[PASS]' if ok else '[FAIL]'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        plant = (
            await db.execute(
                select(Plant).where(
                    Plant.id == select(BeKaizen.plantId).limit(1).scalar_subquery()
                )
            )
        ).scalars().first()
        if plant is None:
            plant = (await db.execute(select(Plant).limit(1))).scalars().first()
        actor = (await db.execute(select(User).limit(1))).scalars().first()
        print(f"Seeding {SEED_N} throwaway records at {plant.name}\n")

        rng = random.Random(42)
        now = datetime.now(timezone.utc)
        for i in range(SEED_N):
            status = STATUSES[i % len(STATUSES)]
            created = now - timedelta(days=rng.randint(10, 300))
            implemented = (
                created + timedelta(days=rng.randint(3, 60))
                if status in {"IMPLEMENTED", "VERIFIED", "CLOSED"}
                else None
            )
            verified = (
                implemented + timedelta(days=rng.randint(1, 30))
                if implemented and status in {"VERIFIED", "CLOSED"}
                else None
            )
            db.add(
                BeKaizen(
                    plantId=plant.id,
                    kaizenNo=f"SCALE-{i:04d}",
                    title=f"Reduce {TOPICS[i % len(TOPICS)]} on line {i % 6 + 1}",
                    category=CATEGORIES[i % len(CATEGORIES)],
                    # i%11==0 is cheap and quick but stays on the STANDARD
                    # lane, so the "badge is not the lane" claim is tested in
                    # BOTH directions rather than only the negative one.
                    lane="FAST_TRACK" if i % 7 == 0 else "STANDARD",
                    problemStatement=f"Operators lose time on the {TOPICS[i % len(TOPICS)]}.",
                    proposedImprovement=f"Re-fit the {TOPICS[i % len(TOPICS)]} so it self-locates.",
                    status=status,
                    ownerId=actor.id if i % 3 else None,
                    createdById=actor.id,
                    createdAt=created,
                    targetDate=created
                    + timedelta(days=1 if (i % 7 == 0 or i % 11 == 0) else rng.randint(14, 90)),
                    # Three deliberate shapes, because a purely random spread
                    # never produced a cheap-and-quick record and the positive
                    # badge path was passing vacuously:
                    #   i%5==0  → no investment recorded at all
                    #   i%7==0  → genuinely cheap (fast lane, 1-day window)
                    #   else    → priced well above the threshold
                    investmentCost=(
                        None
                        if i % 5 == 0
                        else float(rng.randint(200, 4_000))
                        if (i % 7 == 0 or i % 11 == 0)
                        else float(rng.randint(20_000, 90_000))
                    ),
                    estimatedAnnualSaving=float(rng.randint(10_000, 900_000)),
                    verifiedAnnualSaving=float(rng.randint(10_000, 800_000)) if verified else None,
                    implementedAt=implemented,
                    verifiedAt=verified,
                    closedAt=verified + timedelta(days=2) if verified and status == "CLOSED" else None,
                    screenedAt=created + timedelta(days=2) if status != "SUBMITTED" else None,
                    approvedAt=created + timedelta(days=5)
                    if status not in {"SUBMITTED", "SCREENED"}
                    else None,
                )
            )
        await db.flush()

        scope = system_scope([plant.id], job_name="verify")
        total_here = (
            await db.execute(
                scope.apply(select(func.count(BeKaizen.id)), BeKaizen, plant_attr="plantId")
            )
        ).scalar_one()
        print(f"  {total_here} records now visible at this plant\n")
        check("seeded past the 50-record bar", total_here >= 50, f"{total_here} rows")

        def scoped(stmt, **kw):
            base = dict(
                plantId=plant.id, statuses=None, category=None, lane=None,
                mine_user_id=None, q=None, raisedFrom=None, raisedTo=None,
                savingMin=None, savingMax=None,
            )
            base.update(kw)
            return router._kaizen_filters(
                scope.apply(stmt, BeKaizen, plant_attr="plantId"), **base
            )

        # ── Multi-select status ─────────────────────────────────────────────
        print("\nFilters")
        multi = router._split_statuses("SUBMITTED,IN_IMPLEMENTATION")
        rows = (await db.execute(scoped(select(BeKaizen), statuses=multi))).scalars().all()
        check(
            "multi-select status returns both states",
            {r.status for r in rows} == {"SUBMITTED", "IN_IMPLEMENTATION"},
            f"{len(rows)} rows across {sorted({r.status for r in rows})}",
        )
        check(
            "an unknown status token is dropped, not fatal",
            router._split_statuses("SUBMITTED,NONSENSE") == ["SUBMITTED"],
        )
        check("an all-unknown list falls back to no filter", router._split_statuses("XX,YY") is None)

        # ── Saving range ────────────────────────────────────────────────────
        rows = (
            await db.execute(scoped(select(BeKaizen), savingMin=500_000))
        ).scalars().all()
        bad = [
            r.kaizenNo
            for r in rows
            if (r.verifiedAnnualSaving if r.verifiedAnnualSaving is not None else r.estimatedAnnualSaving or 0)
            < 500_000
        ]
        check("saving range honours verified-over-estimated", not bad, f"{len(rows)} rows, {len(bad)} wrong")

        # ── Date range ──────────────────────────────────────────────────────
        cutoff = now - timedelta(days=100)
        rows = (await db.execute(scoped(select(BeKaizen), raisedFrom=cutoff))).scalars().all()
        check(
            "date range excludes older records",
            all(be._aware(r.createdAt) >= cutoff for r in rows),
            f"{len(rows)} rows since {cutoff.date()}",
        )

        # ── Free text ───────────────────────────────────────────────────────
        rows = (await db.execute(scoped(select(BeKaizen), q="conveyor"))).scalars().all()
        check("free-text search matches the problem text", len(rows) > 0, f"{len(rows)} hits")

        # ── Sort whitelist ──────────────────────────────────────────────────
        print("\nSorting")
        for name in router.KAIZEN_SORTS:
            col = router.KAIZEN_SORTS[name]
            r = (
                await db.execute(
                    scoped(select(BeKaizen)).order_by(col.asc().nullslast(), BeKaizen.id.desc()).limit(3)
                )
            ).scalars().all()
            check(f"sort by {name}", len(r) > 0)
        check(
            "an unknown sort key falls back rather than raising",
            router.KAIZEN_SORTS.get("../../etc/passwd", BeKaizen.createdAt) is BeKaizen.createdAt,
        )

        rows = (
            await db.execute(
                scoped(select(BeKaizen))
                .order_by(BeKaizen.targetDate.asc().nullslast(), BeKaizen.id.desc())
                .limit(10)
            )
        ).scalars().all()
        check(
            "ascending sort does not open with a page of nulls",
            rows[0].targetDate is not None,
        )

        # ── Cycle time now that there IS a sample ───────────────────────────
        print("\nCycle time at scale")
        cyc_rows = (
            await db.execute(
                scoped(select(BeKaizen.createdAt, BeKaizen.implementedAt, BeKaizen.verifiedAt))
            )
        ).all()
        cycle = be.kaizen_cycle_times(
            [router._CycleRow(createdAt=c, implementedAt=i, verifiedAt=v) for c, i, v in cyc_rows]
        )
        print(f"       {cycle}")
        check(
            "median now computes",
            cycle["medianDaysRaisedToImplemented"] is not None,
            f"{cycle['medianDaysRaisedToImplemented']}d over {cycle['implementedSampleSize']} records",
        )

        # ── Trigram search at scale, and whether the index is used ──────────
        print("\nSearch at scale")
        probe = "conveyor gu"
        similarity = func.greatest(
            func.similarity(BeKaizen.title, probe),
            func.similarity(BeKaizen.problemStatement, probe),
            func.similarity(BeKaizen.proposedImprovement, probe),
        ).label("similarity")
        t0 = time.perf_counter()
        hits = (
            await db.execute(
                scope.apply(select(BeKaizen.kaizenNo, similarity), BeKaizen, plant_attr="plantId")
                .where(similarity >= router.SIMILARITY_FLOOR, BeKaizen.status != "DRAFT")
                .order_by(similarity.desc())
                .limit(5)
            )
        ).all()
        ms = (time.perf_counter() - t0) * 1000
        print(f'       "{probe}" -> {[(n, round(float(s), 3)) for n, s in hits]} in {ms:.0f} ms')
        check("a half-typed phrase finds the right ideas", len(hits) > 0)
        check("search returns inside a typeahead budget", ms < 1500, f"{ms:.0f} ms")

        # ── Participation with a real denominator ───────────────────────────
        print("\nParticipation")
        heads = await be.resolve_plant_headcount(db, [plant.id])
        rows = (await db.execute(scoped(select(BeKaizen)))).scalars().all()
        summary = be.summarise_participation(rows, headcount=heads.get(plant.id))
        print(f"       {summary}")
        check(
            "rate is a number when headcount exists, null when it does not",
            (summary["participationRate"] is None) == (plant.id not in heads),
        )

        # ── Export shares the filter helper ─────────────────────────────────
        print("\nExport")
        from app.services.report_pdf import render_kaizen_register_pdf

        export_rows = (
            await db.execute(scoped(select(BeKaizen), statuses=["CLOSED", "VERIFIED"]).limit(800))
        ).scalars().all()
        list_rows = (
            await db.execute(scoped(select(BeKaizen), statuses=["CLOSED", "VERIFIED"]))
        ).scalars().all()
        check(
            "export and list resolve the SAME record set",
            {r.id for r in export_rows} == {r.id for r in list_rows},
            f"{len(export_rows)} records",
        )

        t0 = time.perf_counter()
        pdf = render_kaizen_register_pdf(
            [
                {
                    "kaizenNo": r.kaizenNo, "title": r.title, "siteName": r.siteName,
                    "category": r.category, "status": r.status, "ownerName": "Someone",
                    "targetDate": r.targetDate.isoformat() if r.targetDate else None,
                    "saving": r.verifiedAnnualSaving or r.estimatedAnnualSaving,
                    "currency": r.currency,
                    "fastTrack": be.fast_track_eligibility(r)["eligible"],
                }
                for r in export_rows
            ],
            filter_summary="status CLOSED, VERIFIED",
            generated_by_name="verify",
            plant_label=plant.name,
            truncated=0,
        )
        check(
            "PDF renders the whole filtered set",
            pdf.startswith(b"%PDF-"),
            f"{len(pdf) / 1024:.0f} kB in {(time.perf_counter() - t0) * 1000:.0f} ms",
        )

        # ── Fast-track at scale: no badge without an investment figure ──────
        print("\nFast track at scale")
        all_rows = (await db.execute(scoped(select(BeKaizen)))).scalars().all()
        lane_ft = [r for r in all_rows if r.lane == "FAST_TRACK"]
        earned = [r for r in all_rows if be.fast_track_eligibility(r)["eligible"]]
        no_money = [r for r in earned if r.investmentCost is None]
        print(f"       {len(lane_ft)} on the fast lane, {len(earned)} earn the badge")
        check("nothing earns the badge without an investment figure", not no_money)
        check(
            "some records DO earn the badge",
            len(earned) > 0,
            "otherwise the positive path passes vacuously",
        )
        # Both directions, which is the whole claim: fast-lane records that do
        # NOT earn the badge, and standard-lane records that DO. A count
        # comparison is not enough — the two sets could be the same size and
        # still be the same set.
        earned_ids = {r.id for r in earned}
        lane_ids = {r.id for r in lane_ft}
        check(
            "some fast-lane records do NOT earn the badge",
            bool(lane_ids - earned_ids),
            f"{len(lane_ids - earned_ids)} on the lane without it",
        )
        check(
            "some records earn the badge WITHOUT being on the fast lane",
            bool(earned_ids - lane_ids),
            f"{len(earned_ids - lane_ids)} off the lane with it",
        )
        # A record on the STANDARD lane with the right figures must still earn
        # it — the badge describes the idea, not the route.
        cheap_standard = [
            r for r in all_rows
            if r.lane == "STANDARD"
            and r.investmentCost is not None
            and r.investmentCost <= 5000
            and be.kaizen_implementation_window_days(r) is not None
            and be.kaizen_implementation_window_days(r) <= 1
        ]
        check(
            "a cheap STANDARD-lane record would earn it too",
            all(be.fast_track_eligibility(r)["eligible"] for r in cheap_standard),
            f"{len(cheap_standard)} such records",
        )
        check(
            "the STANDARD-lane positive case is actually present",
            len(cheap_standard) > 0,
            "otherwise the check above passes over an empty list",
        )

        await db.rollback()
        print("\n   (transaction rolled back — the 60 seeded records were not written)")

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILED — {len(failures)}: " + "; ".join(failures))
        raise SystemExit(1)
    print("All scale checks passed.")


asyncio.run(main())
