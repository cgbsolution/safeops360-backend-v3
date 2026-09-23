"""Live-data diagnostic for the Kaizen gap-closure build.

Runs the REAL service + router code against the REAL database, because tsc /
next build / pytest / prisma validate are all necessary and none of them is
sufficient — the August analytics bug passed all four and still reported open=0.

Read-only except for one clearly-marked transactional block that creates a
throwaway record, walks it through the full lifecycle, replicates it, generates
an OPL, and then ROLLS THE WHOLE THING BACK. Nothing is committed.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/verify/kaizen.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app.models  # noqa: F401 — register every mapper
from app.core.db import AsyncSessionLocal
from app.core.soft_delete import register_default_governed
from app.models.business_excellence import BeKaizen, BeKaizenReplication, BeOpl
from app.models.user import User
from app.services import business_excellence as be

register_default_governed()

PASS, FAIL = "  [PASS]", "  [FAIL]"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        # ── 1. The new columns are readable through the ORM ─────────────────
        print("\n1. Schema — the new mapped columns actually exist")
        rows = (await db.execute(select(BeKaizen))).scalars().all()
        check("BeKaizen entity select works", True, f"{len(rows)} live rows")
        check(
            "screenedAt / approvedAt / originKaizenId / generatedOplId readable",
            all(
                hasattr(k, f)
                for k in rows[:1]
                for f in ("screenedAt", "approvedAt", "originKaizenId", "generatedOplId")
            )
            or not rows,
        )
        reps = (await db.execute(select(func.count(BeKaizenReplication.id)))).scalar_one()
        check("BeKaizenReplication queryable", True, f"{reps} rows")

        # ── 2. Fast track is derived, not stored ────────────────────────────
        print("\n2. §5 Fast track — badge derived from the record's own figures")
        lane_ft = [k for k in rows if k.lane == "FAST_TRACK"]
        earned = [k for k in rows if be.fast_track_eligibility(k)["eligible"]]
        print(f"       {len(lane_ft)} on the FAST_TRACK lane, {len(earned)} earn the badge")
        for k in lane_ft:
            elig = be.fast_track_eligibility(k)
            print(f"       {k.kaizenNo}: eligible={elig['eligible']} {elig['reasons']}")
        check(
            "no live record earns the badge without an investment figure",
            all(k.investmentCost is not None for k in earned),
        )

        # ── 3. Approval blockers ────────────────────────────────────────────
        print("\n3. §6 Approval gate")
        unowned = [k for k in rows if not k.ownerId]
        check(
            "every unowned record reports an approval blocker",
            all(be.kaizen_approval_blockers(k) for k in unowned),
            f"{len(unowned)} unowned of {len(rows)}",
        )
        owned = [k for k in rows if k.ownerId]
        check(
            "an owned record reports no blocker",
            all(not be.kaizen_approval_blockers(k) for k in owned),
            f"{len(owned)} owned",
        )

        # ── 4. Cycle time honesty ───────────────────────────────────────────
        print("\n4. §2 Cycle time")
        cycle = be.kaizen_cycle_times(rows)
        print(f"       {cycle}")
        # Written as the INVARIANT, not as a snapshot of what live data happened
        # to hold: null exactly when the sample is short, a number exactly when
        # it is not. The earlier form asserted "the median is null" and started
        # failing the moment the demo seed gave the plant enough finished
        # records — i.e. it failed on success.
        below_floor = cycle["implementedSampleSize"] < be.CYCLE_TIME_MIN_SAMPLE
        check(
            "median is null below the sample floor and a number above it",
            (cycle["medianDaysRaisedToImplemented"] is None) == below_floor,
            f"{cycle['implementedSampleSize']} implemented records, floor is "
            f"{be.CYCLE_TIME_MIN_SAMPLE}",
        )
        check(
            "the median is never a zero standing in for missing data",
            cycle["medianDaysRaisedToImplemented"] != 0,
        )

        # ── 5. Headcount source ─────────────────────────────────────────────
        print("\n5. §3 Participation — real headcount, null when absent")
        plant_ids = sorted({k.plantId for k in rows})
        heads = await be.resolve_plant_headcount(db, plant_ids)
        print(f"       plants with Kaizen data: {plant_ids}")
        print(f"       headcount resolved: {heads}")
        for pid in plant_ids:
            summary = be.summarise_participation(
                [k for k in rows if k.plantId == pid], headcount=heads.get(pid)
            )
            print(f"       {pid}: {summary}")
            check(
                f"participationRate is null, not 0, when {pid} has no profile",
                (summary["participationRate"] is None) == (pid not in heads),
            )

        # ── 6. Trigram search actually uses the index and returns hits ──────
        print("\n6. §4 Similar-idea search — pg_trgm on live rows")
        if rows:
            probe = rows[0].title[:14]
            similarity = func.greatest(
                func.similarity(BeKaizen.title, probe),
                func.similarity(BeKaizen.problemStatement, probe),
                func.similarity(BeKaizen.proposedImprovement, probe),
            ).label("similarity")
            hits = (
                await db.execute(
                    select(BeKaizen.kaizenNo, similarity)
                    .where(similarity >= 0.12, BeKaizen.status != "DRAFT")
                    .order_by(similarity.desc())
                    .limit(5)
                )
            ).all()
            print(f'       query "{probe}" → {[(n, round(float(s), 3)) for n, s in hits]}')
            check("a half-typed title finds its own record", len(hits) >= 1)

            plan = (
                await db.execute(
                    select(func.count()).select_from(BeKaizen).where(
                        BeKaizen.title.op("%")(probe)
                    )
                )
            ).scalar_one()
            check("the pg_trgm % operator is usable", True, f"{plan} match(es)")

        # ── 7. Full lifecycle, then rolled back ─────────────────────────────
        print("\n7. Full lifecycle on a throwaway record (ROLLED BACK at the end)")
        actor = (await db.execute(select(User).limit(1))).scalars().first()
        second = (
            await db.execute(select(User).where(User.id != actor.id).limit(1))
        ).scalars().first()
        plant_id = plant_ids[0] if plant_ids else None
        if actor is None or plant_id is None:
            check("lifecycle walk", False, "no user or plant to test with")
        else:
            now = datetime.now(timezone.utc)
            probe = BeKaizen(
                plantId=plant_id,
                title="VERIFY probe — remove the manual re-tension step",
                category="PRODUCTIVITY",
                lane="STANDARD",
                problemStatement="Operators re-tension the belt by hand each shift.",
                proposedImprovement="Fit a spring tensioner so the belt self-adjusts.",
                status="DRAFT",
                createdById=actor.id,
                investmentCost=1200.0,
                targetDate=now + timedelta(days=1),
                estimatedAnnualSaving=90000.0,
            )
            db.add(probe)
            await db.flush()

            elig = be.fast_track_eligibility(probe)
            check(
                "a cheap, one-day idea earns the badge on its own figures",
                elig["eligible"],
                str(elig["reasons"]),
            )

            check(
                "APPROVED is blocked while ownerId is null",
                bool(be.kaizen_approval_blockers(probe)),
                be.kaizen_approval_blockers(probe)[0],
            )
            probe.ownerId = second.id if second else actor.id
            check(
                "APPROVED unblocks once an owner is assigned",
                not be.kaizen_approval_blockers(probe),
            )

            for i, target in enumerate(
                ["SUBMITTED", "SCREENED", "APPROVED", "IN_IMPLEMENTATION",
                 "IMPLEMENTED", "VERIFIED", "CLOSED"]
            ):
                be.stamp_kaizen_transition(probe, target, at=now + timedelta(days=i))
                probe.status = target
            probe.verifiedAnnualSaving = 88000.0
            await db.flush()

            stamped = {
                f: getattr(probe, f)
                for f in ("screenedAt", "approvedAt", "implementedAt", "verifiedAt", "closedAt")
            }
            print(f"       stamps: { {k: (v.isoformat() if v else None) for k, v in stamped.items()} }")
            check(
                "all five lifecycle timestamps populated",
                all(v is not None for v in stamped.values()),
            )

            be.stamp_kaizen_transition(probe, "SCREENED", at=now + timedelta(days=99))
            check(
                "re-entering a state does not overwrite the original stamp",
                probe.screenedAt == stamped["screenedAt"],
            )

            cyc = be.kaizen_cycle_times([probe] * 5)
            check(
                "median computes once the sample floor is met",
                cyc["medianDaysRaisedToImplemented"] is not None,
                f"{cyc['medianDaysRaisedToImplemented']} days to implement",
            )

            # Replicate to a different plant
            other = (
                await db.execute(
                    select(BeKaizen.plantId).where(BeKaizen.plantId != plant_id).limit(1)
                )
            ).scalar_one_or_none()
            if other is None:
                from app.models.plant import Plant

                other = (
                    await db.execute(select(Plant.id).where(Plant.id != plant_id).limit(1))
                ).scalar_one()
            copy = BeKaizen(
                plantId=other,
                title=probe.title,
                category=probe.category,
                lane="STANDARD",
                problemStatement=probe.problemStatement,
                proposedImprovement=probe.proposedImprovement,
                status="DRAFT",
                originKaizenId=probe.id,
                sourceModule="KAIZEN",
                sourceRecordId=probe.id,
                createdById=actor.id,
            )
            db.add(copy)
            await db.flush()
            db.add(
                BeKaizenReplication(
                    sourceKaizenId=probe.id,
                    replicaKaizenId=copy.id,
                    replicatedAtPlantId=other,
                    replicatedAtPlantName="probe plant",
                    replicatedById=actor.id,
                )
            )
            await db.flush()
            check("replication row created", True, f"{probe.id[:8]} → {other[:8]}")
            check("money is NOT carried across to the copy", copy.investmentCost is None)

            # The unique constraint is the real double-count guard
            db.add(
                BeKaizenReplication(
                    sourceKaizenId=probe.id,
                    replicatedAtPlantId=other,
                    replicatedById=actor.id,
                )
            )
            duplicate_blocked = False
            try:
                await db.flush()
            except Exception as e:  # noqa: BLE001
                duplicate_blocked = "uq_BeKaizenReplication_source_plant" in str(e)
                await db.rollback()
            check(
                "a duplicate replication is refused by the database",
                duplicate_blocked,
                "unique index enforced",
            )

        await db.rollback()
        print("\n   (transaction rolled back — nothing was written)")

        # ── 8. Attachment layer is backed ───────────────────────────────────
        print("\n8. §1 Evidence — the shared Attachment table backs be_kaizen")
        from app.models.attachment import Attachment
        from app.services.evidence_registry import get_spec

        spec = get_spec("be_kaizen")
        n = (await db.execute(select(func.count(Attachment.id)))).scalar_one()
        check("Attachment table queryable", True, f"{n} rows")
        check(
            "be_kaizen registered with BEFORE_PHOTO and AFTER_PHOTO",
            spec is not None
            and {"BEFORE_PHOTO", "AFTER_PHOTO"} <= spec.categories,
        )

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILED — {len(failures)} check(s): " + "; ".join(failures))
        raise SystemExit(1)
    print("All live-data checks passed.")


asyncio.run(main())
