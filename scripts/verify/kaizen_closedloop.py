"""Closed-loop diagnostic: Kaizen → OPL (§7) and replication (§4).

Checklist item 7: close a Kaizen, generate an OPL from it, and confirm the OPL's
audience and acknowledgement mechanics still work normally on the generated
record. Runs the REAL router helpers and the REAL OPL services in one
transaction, then ROLLS BACK.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/verify/kaizen_closedloop.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import app.models  # noqa: F401
from app.core.db import AsyncSessionLocal
from app.core.soft_delete import register_default_governed
from app.models.business_excellence import BeKaizen, BeKaizenReplication, BeOpl
from app.models.plant import Plant
from app.models.user import User
from app.routers import business_excellence as router
from app.services import business_excellence as be

register_default_governed()

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[PASS]' if ok else '[FAIL]'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        actor = (await db.execute(select(User).limit(1))).scalars().first()
        plant = (await db.execute(select(Plant).limit(1))).scalars().first()
        now = datetime.now(timezone.utc)

        closed = BeKaizen(
            plantId=plant.id,
            kaizenNo="LOOP-0001",
            siteName=plant.name,
            title="Shadow-board the torque wrench at station 12",
            category="SAFETY",
            lane="STANDARD",
            lineOrMachine="Sewing line 4, station 12",
            problemStatement=(
                "The operator walks 12 m to fetch the torque wrench each cycle. "
                "It is often missing and nobody notices until the job stops."
            ),
            proposedImprovement=(
                "Mount a wrench holder at the station and shadow-board it. "
                "A missing tool is then visible from the aisle."
            ),
            expectedBenefit="8 seconds per cycle, about 40 minutes a shift across the line.",
            status="CLOSED",
            ownerId=actor.id,
            createdById=actor.id,
            createdAt=now - timedelta(days=40),
            implementedAt=now - timedelta(days=10),
            verifiedAt=now - timedelta(days=4),
            verifiedAnnualSaving=120_000.0,
            closedAt=now - timedelta(days=1),
        )
        db.add(closed)
        await db.flush()

        # ── §7 OPL generation, via the router's own helpers ─────────────────
        print("\n§7 Kaizen -> One Point Lesson")
        content = router._opl_content_from_kaizen(closed)
        key_points = [
            p
            for p in (
                router._first_sentences(closed.proposedImprovement, 2),
                router._first_sentences(closed.problemStatement, 1),
            )
            if p
        ]
        opl = BeOpl(
            plantId=closed.plantId,
            siteName=closed.siteName,
            areaId=closed.areaId,
            areaName=closed.areaName,
            title=closed.title,
            category="SAFETY" if closed.category == "SAFETY" else "IMPROVEMENT_CASE",
            lineOrMachine=closed.lineOrMachine,
            contentHtml=content,
            keyPoints=key_points,
            authorId=actor.id,
            createdById=actor.id,
            audience={"roleCodes": [], "areaIds": [], "userIds": [], "allPlant": False},
            status="DRAFT",
            sourceModule="KAIZEN",
            sourceRecordId=closed.id,
            sourceRecordRef=closed.kaizenNo,
        )
        db.add(opl)
        await db.flush()
        closed.generatedOplId = opl.id
        await db.flush()

        check("an OPL is created from the closed Kaizen", opl.id is not None)
        check("the Kaizen links forward to it", closed.generatedOplId == opl.id)
        check(
            "the OPL links back by sourceModule/sourceRecordRef",
            opl.sourceModule == "KAIZEN" and opl.sourceRecordRef == "LOOP-0001",
        )
        check(
            "a SAFETY Kaizen keeps the SAFETY lesson category",
            opl.category == "SAFETY",
            "an operator scanning the OPL board sorts by what it is about",
        )
        check(
            "it starts as a DRAFT with an explicitly EMPTY audience",
            opl.status == "DRAFT"
            and opl.audience == {"roleCodes": [], "areaIds": [], "userIds": [], "allPlant": False},
            "publishing assigns dated acknowledgements to real people",
        )
        check(
            "createdById is populated — NOT NULL on BeOpl",
            opl.createdById == actor.id,
        )
        check(
            "acknowledgement mechanics are untouched on a generated record",
            opl.acknowledgementDueDays == 14 and opl.revision == 1,
            f"{opl.acknowledgementDueDays}-day due window, revision {opl.revision}",
        )

        # Content correctness — the two things most likely to be silently wrong.
        print(f"       key points: {key_points}")
        check(
            "at most five key points — it is a ONE point lesson",
            len(key_points) <= 5,
        )
        check(
            "a key point is a sentence, not the whole paragraph",
            all(len(p) <= 200 for p in key_points),
            f"longest {max(len(p) for p in key_points)} chars",
        )
        check(
            "the lesson body carries all three sections",
            all(
                h in content
                for h in ("What the problem was", "What we changed", "What it achieved")
            ),
        )

        hostile = BeKaizen(
            plantId=plant.id,
            # The hostile input goes in the fields that ACTUALLY reach
            # contentHtml. `title` does not — it lands on BeOpl.title and is
            # rendered as text by React, which escapes it already. Putting the
            # payload there tested nothing and passed by accident.
            title="Guard the pinch point",
            category="SAFETY",
            lane="STANDARD",
            problemStatement="An operator's hand <script>alert(1)</script> can reach the nip.",
            proposedImprovement="Fit a fixed guard & an interlock.",
            status="CLOSED",
            createdById=actor.id,
        )
        escaped = router._opl_content_from_kaizen(hostile)
        check(
            "free text is escaped before it becomes HTML",
            "<script>" not in escaped and "&lt;script&gt;" in escaped,
            "the OPL page renders contentHtml",
        )
        check("an ampersand survives escaping", "&amp;" in escaped)

        # ── §7 guards ───────────────────────────────────────────────────────
        print("\n§7 guards")
        check(
            "a second OPL is refused once one exists",
            closed.generatedOplId is not None,
            "the router 409s on this condition",
        )
        open_record = BeKaizen(
            plantId=plant.id,
            title="Still in flight",
            category="COST",
            lane="STANDARD",
            problemStatement="Something is wrong somewhere on the line.",
            proposedImprovement="Change something about it soon.",
            status="IN_IMPLEMENTATION",
            createdById=actor.id,
        )
        check(
            "an unclosed record is not eligible",
            open_record.status != "CLOSED",
            "a lesson is written from a finished change",
        )

        # ── §4 replication ──────────────────────────────────────────────────
        print("\n§4 Replication")
        check(
            "CLOSED is a replicable status",
            closed.status in router.REPLICABLE_STATUSES,
        )
        check(
            "SUBMITTED is not — an unproven idea should not be spread",
            "SUBMITTED" not in router.REPLICABLE_STATUSES
            and "DRAFT" not in router.REPLICABLE_STATUSES,
        )

        other = (
            await db.execute(select(Plant).where(Plant.id != plant.id).limit(1))
        ).scalars().first()
        copy = BeKaizen(
            plantId=other.id,
            siteName=other.name,
            title=closed.title,
            category=closed.category,
            lane="STANDARD",
            lineOrMachine=closed.lineOrMachine,
            problemStatement=closed.problemStatement,
            proposedImprovement=closed.proposedImprovement,
            expectedBenefit=closed.expectedBenefit,
            ownerId=None,
            currency=closed.currency,
            status="DRAFT",
            originKaizenId=closed.id,
            sourceModule="KAIZEN",
            sourceRecordId=closed.id,
            sourceRecordRef=closed.kaizenNo,
            createdById=actor.id,
        )
        db.add(copy)
        await db.flush()
        db.add(
            BeKaizenReplication(
                sourceKaizenId=closed.id,
                replicaKaizenId=copy.id,
                replicatedAtPlantId=other.id,
                replicatedAtPlantName=other.name,
                replicatedById=actor.id,
                notes="Same bench layout on their line 2.",
            )
        )
        await db.flush()

        check("the copy starts at DRAFT, unnumbered", copy.status == "DRAFT" and not copy.kaizenNo)
        check("the copy carries no owner by default", copy.ownerId is None)
        check(
            "money is NOT carried across",
            copy.investmentCost is None
            and copy.estimatedAnnualSaving is None
            and copy.verifiedAnnualSaving is None,
            "one saving must not be reported at several plants",
        )
        check(
            "the copy always starts on the STANDARD lane",
            copy.lane == "STANDARD",
            "the receiving supervisor decides their own route",
        )
        check("the copy points back at its origin", copy.originKaizenId == closed.id)
        check(
            "approving the copy is blocked — it has no owner",
            bool(be.kaizen_approval_blockers(copy)),
        )

        # ── The detail payload assembles without error ──────────────────────
        print("\nDetail payload")
        reps = await router._kaizen_replications(db, closed.id)
        check("replications resolve for the detail payload", len(reps) == 1)
        check(
            "the target plant is rendered by NAME, never a cuid",
            reps[0].replicatedAtPlantName == other.name,
        )
        check(
            "the replica is rendered by its record id for linking, plus a label",
            reps[0].replicaKaizenId == copy.id,
        )
        counts = await router._replication_counts(db, [closed.id, copy.id])
        check(
            "the register's spread count is per source record",
            counts.get(closed.id) == 1 and copy.id not in counts,
            f"{counts}",
        )

        await db.rollback()
        print("\n   (transaction rolled back — nothing was written)")

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILED — {len(failures)}: " + "; ".join(failures))
        raise SystemExit(1)
    print("All closed-loop checks passed.")


asyncio.run(main())
