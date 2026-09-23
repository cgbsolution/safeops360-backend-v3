"""Kaizen demo seed for the gap-closure build — 14 records plus 2 replications.

Fills the register with enough finished lifecycle for the NEW capabilities to
have something to show:

  * 7 records with an implementation date and 5 with a verification date, so the
    median-days tiles clear their 5-record sample floor instead of correctly but
    unhelpfully reading "Not enough data"
  * 8 distinct submitters, so the Participation view is about people rather than
    about one enthusiast
  * 3 records that EARN the fast-track badge and 2 that sit on the fast LANE
    without earning it — the split this build introduced is invisible unless
    both sides of it exist
  * 1 approved-stage record with no owner, so the approval gate can be
    demonstrated failing rather than described
  * 2 horizontal deployments to Meridian South Works, with the replication rows
    behind them

WHY DIRECT DB AND NOT THE API
scripts/seed_be_demo.py argues — correctly, for its purpose — that a seed should
drive the API so it exercises the rules rather than writing rows that merely look
right. This one cannot, and the reason is the point of the dataset: every record
here needs a BACKDATED createdAt and a coherent chain of historical lifecycle
timestamps. The API stamps now() on all of them, so an API-driven seed would
produce fourteen records raised and closed the same afternoon, and every cycle
time would be zero days — which is precisely the number this seed exists to make
non-zero. Numbering still goes through be.next_record_number() rather than being
reimplemented, so the max+1 and include-deleted rules still hold.

WHERE IT SEEDS, AND WHY THERE
Meridian North Works. It is the only plant that can actually show this: it has
59 users and 22 areas. The 16 Meridian Apparel plants have headcount recorded
but ZERO users and ZERO areas, so records seeded there would be invisible to
every login and unassignable to any area.

  PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/seed_kaizen_gap_demo.py
  ... --dry-run     print what would be written, write nothing
  ... --force       add a second dataset even if one is already present

IDEMPOTENT: every seeded record carries sourceRecordId = "SEED-KAIZEN-GAP" and a
second run stops rather than doubling the register.

⚠ This writes to whatever DATABASE_URL points at, which for this project is
production. It only INSERTS; it modifies and deletes nothing existing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app.models  # noqa: F401 — register every mapper
from app.core.db import AsyncSessionLocal
from app.core.soft_delete import register_default_governed
from app.models.business_excellence import BeKaizen, BeKaizenReplication
from app.models.plant import Area, Plant
from app.models.user import User
from app.services import business_excellence as be

register_default_governed()

MARKER = "SEED-KAIZEN-GAP"
PLANT_NAME_LIKE = "Meridian North Works%"
REPLICATE_TO_LIKE = "Meridian South Works%"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# The dataset
# ─────────────────────────────────────────────────────────────────────────────
# `raisedDaysAgo` anchors each record; every later timestamp is an offset from
# it, so the whole set stays coherent however long after writing it is read.
#
# `cast` keys are ROLE names resolved against real users at this plant at run
# time rather than hard-coded ids — a re-seeded tenant changes every cuid.
#
# The money columns are the load-bearing part. `investment` at or under ₹5,000
# together with a 1-day window is what EARNS the fast-track badge; `lane` is a
# separate choice about which approval workflow the record runs through. Records
# 12 and 13 exist specifically to show a fast LANE with no badge.
RECORDS: list[dict] = [
    {
        "title": "Nest the collar and cuff markers to cut fabric wastage",
        "area": "Cutting Hall",
        "line": "Cutting table 2",
        "category": "COST",
        "problem": (
            "Collar and cuff markers are laid out separately, leaving a 60 mm "
            "strip down the length of every lay that goes to waste. On a 40-ply "
            "lay that is roughly 11 m of fabric a shift."
        ),
        "improvement": (
            "Re-nest the two markers so the cuff pieces sit inside the collar "
            "gaps. The CAD file is a one-off change; the cutter follows the same "
            "process afterwards."
        ),
        "benefit": "About 11 m of shell fabric a shift on the shirt programme.",
        "status": "CLOSED",
        "raisedDaysAgo": 214,
        "screenedAfter": 6, "approvedAfter": 13, "implementedAfter": 41,
        "verifiedAfter": 74, "closedAfter": 79,
        "investment": 42_000.0, "estimated": 380_000.0, "verified": 356_000.0,
        "savingType": "HARD",
        "targetAfter": 45,
        "raiser": "SUPERVISOR", "owner": "DEPARTMENT_HEAD", "verifier": "PLANT_HEAD",
        "verificationNote": "Measured against 6 weeks of cut-plan fabric issue records.",
        "replicate": True,
    },
    {
        "title": "Trolley the needle-change kit to the machine",
        "area": "Sewing Line 1",
        "line": "Sewing line 1, stations 4-18",
        "category": "PRODUCTIVITY",
        "problem": (
            "An operator with a broken needle walks to the supervisor's desk for "
            "the change kit and back. It happens 20 to 30 times a shift across "
            "the line and each trip is about 90 seconds."
        ),
        "improvement": (
            "Fit a small parts trolley with needles, screwdriver and a scrap bin "
            "that the line's floater wheels between stations."
        ),
        "benefit": "Roughly 35 operator-minutes a shift returned to the line.",
        "status": "CLOSED",
        "raisedDaysAgo": 168,
        "screenedAfter": 1, "approvedAfter": 1, "implementedAfter": 2,
        "verifiedAfter": 34, "closedAfter": 38,
        # Cheap and same-week: this one EARNS the badge.
        "investment": 3_500.0, "estimated": 120_000.0, "verified": 138_000.0,
        "savingType": "SOFT",
        "targetAfter": 1,
        "lane": "FAST_TRACK",
        "raiser": "WORKER", "owner": "SUPERVISOR", "verifier": "DEPARTMENT_HEAD",
        "verificationNote": "Time study over 3 shifts, both sewing lines.",
    },
    {
        "title": "Pre-fold the export cartons at the packing bench",
        "area": "Finishing & Packing",
        "line": "Packing bench 3",
        "category": "PRODUCTIVITY",
        "problem": (
            "Packers assemble each export carton from flat at the bench, which "
            "takes two hands and about 40 seconds while finished garments queue "
            "behind them."
        ),
        "improvement": (
            "Build a simple folding jig so a batch of 25 cartons is pre-formed "
            "during the line changeover gap and stacked ready at the bench."
        ),
        "benefit": "Around 16 minutes a shift per packing bench.",
        "status": "VERIFIED",
        "raisedDaysAgo": 132,
        "screenedAfter": 8, "approvedAfter": 15, "implementedAfter": 44,
        "verifiedAfter": 81,
        "investment": 18_000.0, "estimated": 240_000.0, "verified": 210_000.0,
        "savingType": "SOFT",
        "targetAfter": 40,
        "raiser": "FIELD_TECHNICIAN", "owner": "SUPERVISOR", "verifier": "DEPARTMENT_HEAD",
        "verificationNote": "Verified at 210k, below the 240k estimate — bench 4 was not converted.",
    },
    {
        "title": "Recover boiler condensate back to the feed tank",
        "area": "Boiler House",
        "line": "Boiler 1 steam header",
        "category": "ENVIRONMENT",
        "problem": (
            "Condensate from the finishing-section steam presses runs to drain "
            "at around 70 degrees. The feed tank is then made up with cold water "
            "and heated again from ambient."
        ),
        "improvement": (
            "Run a condensate return line from the press manifold back to the "
            "feed tank with a trap set and a small pump."
        ),
        "benefit": "Lower furnace-oil consumption and less hot effluent to the ETP.",
        "status": "VERIFIED",
        "raisedDaysAgo": 155,
        "screenedAfter": 11, "approvedAfter": 26, "implementedAfter": 68,
        "verifiedAfter": 112,
        "investment": 145_000.0, "estimated": 700_000.0, "verified": 640_000.0,
        "savingType": "HARD",
        "targetAfter": 70,
        "raiser": "MAINTENANCE_HEAD", "owner": "MAINTENANCE_HEAD", "verifier": "PLANT_HEAD",
        "verificationNote": "Verified against 3 months of furnace-oil issue slips.",
        "replicate": True,
    },
    {
        "title": "Fortnightly compressed-air leak survey with tagging",
        "area": "Compressor House",
        "line": "Ring main, sewing floor spurs",
        "category": "COST",
        "problem": (
            "Nobody owns air leaks. The compressor runs loaded far longer at "
            "night than the machine load explains, and leaks are only found when "
            "somebody happens to hear one."
        ),
        "improvement": (
            "A fortnightly ultrasonic walk with numbered tags on every leak "
            "found, and the tags cleared through the maintenance work order list."
        ),
        "benefit": "Lower compressor loaded hours; a standing routine rather than a one-off fix.",
        "status": "VERIFIED",
        "raisedDaysAgo": 121,
        "screenedAfter": 5, "approvedAfter": 12, "implementedAfter": 33,
        "verifiedAfter": 76,
        "investment": 9_000.0, "estimated": 300_000.0, "verified": 285_000.0,
        "savingType": "HARD",
        "targetAfter": 35,
        "raiser": "FIELD_TECHNICIAN", "owner": "MAINTENANCE_HEAD", "verifier": "DEPARTMENT_HEAD",
        "verificationNote": "Loaded-hour comparison, 8 weeks before and after.",
    },
    {
        "title": "Colour-band the fabric roll racks by shade lot",
        "area": "Fabric Warehouse",
        "line": "Rack rows C and D",
        "category": "QUALITY",
        "problem": (
            "Shade lots are written on the roll end in marker. Issuing the wrong "
            "lot to a cut is caught at inspection, by which point the garments "
            "are already sewn."
        ),
        "improvement": (
            "Colour-band the rack locations by shade lot and match the band to "
            "the cut plan, so an issue against the wrong lot is visible from the "
            "aisle before the roll is moved."
        ),
        "benefit": "Fewer shade-variation rejects reaching the sewing floor.",
        "status": "IMPLEMENTED",
        "raisedDaysAgo": 96,
        "screenedAfter": 7, "approvedAfter": 18, "implementedAfter": 52,
        "investment": 12_000.0, "estimated": 260_000.0,
        "savingType": "SOFT",
        "targetAfter": 50,
        "raiser": "WORKER", "owner": "SUPERVISOR",
    },
    {
        "title": "Magnetic thread-cone holder at each station",
        "area": "Sewing Line 2",
        "line": "Sewing line 2",
        "category": "MORALE",
        "problem": (
            "Spare thread cones sit loose on the machine table and roll onto the "
            "floor. The operator stops, retrieves it and re-threads."
        ),
        "improvement": "A magnetic cone cradle fixed to the side of each machine head.",
        "benefit": "Fewer stoppages and a tidier station at handover.",
        "status": "IMPLEMENTED",
        "raisedDaysAgo": 63,
        "screenedAfter": 1, "approvedAfter": 1, "implementedAfter": 3,
        # Cheap and next-day: EARNS the badge, on the fast lane too.
        "investment": 2_200.0, "estimated": 60_000.0,
        "savingType": "SOFT",
        "targetAfter": 1,
        "lane": "FAST_TRACK",
        "raiser": "WORKER", "owner": "SUPERVISOR",
    },
    {
        "title": "Move ETP sludge dewatering to the night shift",
        "area": "Effluent Treatment Plant",
        "line": "Filter press",
        "category": "ENVIRONMENT",
        "problem": (
            "The filter press runs during the day against peak tariff, and the "
            "sludge trolley crosses the dispatch route while lorries are loading."
        ),
        "improvement": (
            "Shift the dewatering cycle to the night shift. Off-peak tariff, and "
            "the dispatch yard is clear."
        ),
        "benefit": "Lower tariff on the press and no trolley/vehicle interaction.",
        "status": "IN_IMPLEMENTATION",
        "raisedDaysAgo": 48,
        "screenedAfter": 9, "approvedAfter": 21,
        "investment": 65_000.0, "estimated": 310_000.0,
        "savingType": "HARD",
        "targetAfter": 60,
        "raiser": "ENVIRONMENT_MANAGER", "owner": "MAINTENANCE_HEAD",
    },
    {
        "title": "Shadow-board the sewing machine service tools",
        "area": "Maintenance Workshop",
        "line": "Mechanics' bench",
        "category": "PRODUCTIVITY",
        "problem": (
            "Service tools are kept in a drawer. A mechanic called to a stopped "
            "machine spends the first two minutes finding the right driver, with "
            "the line waiting."
        ),
        "improvement": "Shadow-board the eight tools actually used, at the bench and on the trolley.",
        "benefit": "A missing tool is visible before the mechanic leaves the bench.",
        "status": "IN_IMPLEMENTATION",
        "raisedDaysAgo": 27,
        "screenedAfter": 1, "approvedAfter": 1,
        # Cheap and next-day: EARNS the badge.
        "investment": 4_500.0, "estimated": 90_000.0,
        "savingType": "SOFT",
        "targetAfter": 1,
        "raiser": "FIELD_TECHNICIAN", "owner": "MAINTENANCE_HEAD",
    },
    {
        "title": "Stage the AQL sample at the line end, not the lab",
        "area": "Quality Control Laboratory",
        "line": "Line-end inspection",
        "category": "QUALITY",
        "problem": (
            "AQL samples are carried to the lab in ones and twos. The inspector "
            "walks the floor repeatedly and results come back after the next "
            "bundle is already sewn."
        ),
        "improvement": (
            "Stage a sample rack at the line end and collect on a fixed hourly "
            "round, so feedback reaches the line within the same bundle."
        ),
        "benefit": "Defect feedback inside the bundle rather than after it.",
        "status": "APPROVED",
        "raisedDaysAgo": 22,
        "screenedAfter": 5, "approvedAfter": 11,
        "investment": 8_000.0, "estimated": 150_000.0,
        "savingType": "SOFT",
        "targetAfter": 40,
        "raiser": "SUPERVISOR", "owner": "DEPARTMENT_HEAD",
    },
    {
        "title": "Adjustable dock plate for the mixed-height lorries",
        "area": "Loading / Dispatch Area",
        "line": "Dock 2",
        "category": "SAFETY",
        "problem": (
            "Container beds and local lorries differ by up to 200 mm. The loaders "
            "bridge the gap with a plank and a trolley wheel has already dropped "
            "through it once."
        ),
        "improvement": "Fit a hinged adjustable dock plate with side kerbs at dock 2.",
        "benefit": "Removes the plank; removes the trolley-drop and fall risk at the edge.",
        "status": "SCREENED",
        "raisedDaysAgo": 16,
        "screenedAfter": 6,
        "investment": 55_000.0, "estimated": 0.0,
        "targetAfter": 55,
        "raiser": "SAFETY_OFFICER", "owner": "MAINTENANCE_HEAD",
    },
    {
        "title": "Interlock guard on the laser marker at the cutting table",
        "area": "Cutting Hall",
        "line": "Cutting table 1 laser marker",
        "category": "SAFETY",
        "problem": (
            "The laser marker head can be reached while it is live. The cutter "
            "leans in to reposition the lay and the beam is at eye height."
        ),
        "improvement": "Fit a fixed guard with a position switch that kills the beam when opened.",
        "benefit": "Removes an eye-exposure route that currently depends on the cutter remembering.",
        "status": "SUBMITTED",
        "raisedDaysAgo": 9,
        # ON the fast lane, but NO investment figure and NO target date, so it
        # earns no badge. This is the KZN-2026-0022 shape, deliberately kept.
        "lane": "FAST_TRACK",
        "investment": None, "estimated": None,
        "targetAfter": None,
        "raiser": "WORKER", "owner": "SUPERVISOR",
    },
    {
        "title": "Standardise operator chair height across both sewing lines",
        "area": "Sewing Line 1",
        "line": "Sewing lines 1 and 2",
        "category": "MORALE",
        "problem": (
            "Chairs have been replaced piecemeal and sit at four different "
            "heights. Operators shim them with cardboard, and the physiotherapist "
            "has flagged three shoulder complaints on line 1."
        ),
        "improvement": "Replace with one gas-lift model set to a marked height range, plus a footrest.",
        "benefit": "Consistent posture across both lines and an end to the cardboard shims.",
        "status": "SUBMITTED",
        "raisedDaysAgo": 6,
        # ON the fast lane, but ₹90,000 and a 45-day window — well past both
        # thresholds, so no badge. A supervisor chose the route; the figures
        # decide the badge.
        "lane": "FAST_TRACK",
        "investment": 90_000.0, "estimated": 0.0,
        "targetAfter": 45,
        "raiser": "OCCUPATIONAL_HEALTH_OFFICER", "owner": "DEPARTMENT_HEAD",
    },
    {
        "title": "Mark a maximum stack height on the pallet racking",
        "area": "Main Warehouse",
        "line": "Bulk trim racking",
        "category": "SAFETY",
        "problem": (
            "Trim cartons are stacked to whatever height the forklift can reach. "
            "Two have come down in the last quarter, both into an aisle people "
            "walk through."
        ),
        "improvement": (
            "Paint a maximum-height line on each rack upright and brief the "
            "forklift operators against it."
        ),
        "benefit": "A limit anybody can see and check from the aisle.",
        "status": "SUBMITTED",
        "raisedDaysAgo": 3,
        # DELIBERATELY UNOWNED. This is the record that demonstrates the §6
        # approval gate refusing rather than the gate being described.
        "investment": 6_000.0, "estimated": 0.0,
        "targetAfter": 30,
        "raiser": "FIELD_TECHNICIAN", "owner": None,
    },
]

#: Roles the seed needs a real user for. Ordered by how specific they are — the
#: fallback chain below walks it so a tenant missing one role still seeds.
ROLE_FALLBACKS: dict[str, list[str]] = {
    "WORKER": ["WORKER", "FIELD_TECHNICIAN", "SUPERVISOR"],
    "SUPERVISOR": ["SUPERVISOR", "DEPARTMENT_HEAD", "MAINTENANCE_HEAD"],
    "FIELD_TECHNICIAN": ["FIELD_TECHNICIAN", "WORKER", "SUPERVISOR"],
    "MAINTENANCE_HEAD": ["MAINTENANCE_HEAD", "DEPARTMENT_HEAD", "SUPERVISOR"],
    "DEPARTMENT_HEAD": ["DEPARTMENT_HEAD", "PLANT_HEAD", "SUPERVISOR"],
    "PLANT_HEAD": ["PLANT_HEAD", "DEPARTMENT_HEAD", "CORPORATE_HSE"],
    "ENVIRONMENT_MANAGER": ["ENVIRONMENT_MANAGER", "HSE_MANAGER", "SUPERVISOR"],
    "SAFETY_OFFICER": ["SAFETY_OFFICER", "HSE_MANAGER", "SUPERVISOR"],
    "OCCUPATIONAL_HEALTH_OFFICER": ["OCCUPATIONAL_HEALTH_OFFICER", "HSE_MANAGER", "SUPERVISOR"],
}


async def _resolve_cast(db, plant_id: str) -> dict[str, User]:
    """One real user per role this dataset names, at this plant.

    Resolved at run time rather than hard-coded: this tenant has been reseeded
    before and every cuid changed. Roles are taken in order so the same person
    is always chosen for the same role between runs — a stable cast keeps the
    Participation figures stable across a re-seed.
    """
    users = (
        await db.execute(
            select(User).where(User.plantId == plant_id).order_by(User.name)
        )
    ).scalars().all()
    by_role: dict[str, list[User]] = {}
    for u in users:
        by_role.setdefault(u.role, []).append(u)

    cast: dict[str, User] = {}
    used: set[str] = set()
    for role, chain in ROLE_FALLBACKS.items():
        for candidate_role in chain:
            pool = by_role.get(candidate_role) or []
            # Prefer somebody not already cast, so the participation figures
            # reflect several people rather than one person wearing every hat.
            fresh = [u for u in pool if u.id not in used] or pool
            if fresh:
                cast[role] = fresh[0]
                used.add(fresh[0].id)
                break
    return cast


async def main(dry_run: bool, force: bool) -> int:
    async with AsyncSessionLocal() as db:
        plant = (
            await db.execute(select(Plant).where(Plant.name.like(PLANT_NAME_LIKE)))
        ).scalars().first()
        if plant is None:
            print(f"No plant matching {PLANT_NAME_LIKE!r}. Nothing to seed.")
            return 1

        existing = (
            await db.execute(
                select(func.count(BeKaizen.id)).where(BeKaizen.sourceRecordId == MARKER)
            )
        ).scalar_one()
        if existing and not force:
            print(f"Already seeded — {existing} record(s) carry the {MARKER} marker.")
            print("Re-run with --force to add a second dataset.")
            return 0

        cast = await _resolve_cast(db, plant.id)
        needed = {r["raiser"] for r in RECORDS} | {
            r["owner"] for r in RECORDS if r.get("owner")
        } | {r["verifier"] for r in RECORDS if r.get("verifier")}
        missing = sorted(needed - set(cast))
        if missing:
            print(f"Cannot seed: no user at {plant.name} resolves for {', '.join(missing)}")
            return 1

        areas = {
            a.name: a
            for a in (
                await db.execute(select(Area).where(Area.plantId == plant.id))
            ).scalars().all()
        }

        target = (
            await db.execute(select(Plant).where(Plant.name.like(REPLICATE_TO_LIKE)))
        ).scalars().first()

        print(f"Seeding the Kaizen gap-closure demo dataset")
        print(f"  plant     : {plant.name}")
        print(f"  cast      : {len(cast)} roles -> {len({u.id for u in cast.values()})} people")
        print(f"  areas     : {len(areas)} available")
        print(f"  replicate : {target.name if target else '(no second plant found)'}")
        print(f"  mode      : {'DRY RUN — nothing will be written' if dry_run else 'WRITING'}\n")

        now = _now()
        created: list[BeKaizen] = []
        # A dry run writes nothing, so next_record_number() cannot see the
        # records ahead of this one and would hand every row the same number.
        # That would make the preview look like a numbering collision when the
        # real run is fine, so the offset is tracked locally for the preview.
        dry_offset = 0

        for spec in RECORDS:
            raised = now - timedelta(days=spec["raisedDaysAgo"])

            def at(key: str) -> datetime | None:
                days = spec.get(key)
                return raised + timedelta(days=days) if days is not None else None

            area = areas.get(spec["area"])
            number = await be.next_record_number(
                db, model=BeKaizen, column=BeKaizen.kaizenNo, prefix="KZN",
                plant_id=plant.id, year=raised.year,
            )
            if dry_run:
                stem, _, tail = number.rpartition("-")
                number = f"{stem}-{int(tail) + dry_offset:04d}"
                dry_offset += 1
            raiser = cast[spec["raiser"]]
            owner = cast[spec["owner"]] if spec.get("owner") else None
            verifier = cast[spec["verifier"]] if spec.get("verifier") else None

            record = BeKaizen(
                kaizenNo=number,
                plantId=plant.id,
                siteName=plant.name,
                areaId=area.id if area else None,
                areaName=area.name if area else None,
                title=spec["title"],
                category=spec["category"],
                lane=spec.get("lane", "STANDARD"),
                lineOrMachine=spec.get("line"),
                problemStatement=spec["problem"],
                proposedImprovement=spec["improvement"],
                expectedBenefit=spec.get("benefit"),
                ownerId=owner.id if owner else None,
                targetDate=at("targetAfter"),
                currency="INR",
                investmentCost=spec.get("investment"),
                # 0.0 means "no figure yet" in these specs, and it must NOT be
                # written as a zero saving — a 0 reads on the register as a
                # measured claim that the idea saves nothing.
                estimatedAnnualSaving=spec.get("estimated") or None,
                savingType=spec.get("savingType"),
                verifiedAnnualSaving=spec.get("verified"),
                verifiedById=verifier.id if verifier and spec.get("verified") else None,
                verifiedAt=at("verifiedAfter"),
                verificationNote=spec.get("verificationNote"),
                status=spec["status"],
                screenedAt=at("screenedAfter"),
                approvedAt=at("approvedAfter"),
                implementedAt=at("implementedAfter"),
                closedAt=at("closedAfter"),
                sourceModule="MANUAL",
                sourceRecordId=MARKER,
                createdById=raiser.id,
                createdAt=raised,
            )

            eligible = be.fast_track_eligibility(record)["eligible"]
            flags = []
            if eligible:
                flags.append("BADGE")
            if record.lane == "FAST_TRACK":
                flags.append("fast lane")
            if not record.ownerId:
                flags.append("NO OWNER")
            print(
                f"  {number}  {spec['status']:<18} {spec['title'][:46]:<46} "
                f"{'  '.join(flags)}"
            )

            if not dry_run:
                db.add(record)
                # Flushed per record so the next next_record_number() call sees
                # this one — otherwise all fourteen claim the same number and
                # the partial unique index rejects the batch.
                await db.flush()
            created.append(record)

        # ── Horizontal deployment ───────────────────────────────────────────
        replications = 0
        if target is not None:
            for spec, source in zip(RECORDS, created):
                if not spec.get("replicate"):
                    continue
                actor = cast["DEPARTMENT_HEAD"]
                copy = BeKaizen(
                    plantId=target.id,
                    siteName=target.name,
                    title=source.title,
                    category=source.category,
                    lane="STANDARD",
                    lineOrMachine=source.lineOrMachine,
                    problemStatement=source.problemStatement,
                    proposedImprovement=source.proposedImprovement,
                    expectedBenefit=source.expectedBenefit,
                    currency="INR",
                    # Money is deliberately NOT carried across — one saving must
                    # not be reported at two plants.
                    status="DRAFT",
                    originKaizenId=source.id if not dry_run else None,
                    sourceModule="KAIZEN",
                    sourceRecordId=source.id if not dry_run else MARKER,
                    sourceRecordRef=source.kaizenNo,
                    createdById=actor.id,
                    createdAt=now - timedelta(days=spec["raisedDaysAgo"] - 20),
                )
                print(f"  replicate {source.kaizenNo} -> {target.name}")
                if not dry_run:
                    db.add(copy)
                    await db.flush()
                    db.add(
                        BeKaizenReplication(
                            sourceKaizenId=source.id,
                            replicaKaizenId=copy.id,
                            replicatedAtPlantId=target.id,
                            replicatedAtPlantName=target.name,
                            replicatedById=actor.id,
                            replicatedAt=copy.createdAt,
                            notes="Same line layout and the same fabric programme.",
                        )
                    )
                    await db.flush()
                replications += 1

        if dry_run:
            await db.rollback()
            print(f"\nDRY RUN — rolled back. Would have written "
                  f"{len(created)} records and {replications} replication(s).")
            return 0

        await db.commit()

        # Verify rather than trust: read the aggregates back through the real
        # service so the run reports what the SCREEN will show, not what the
        # seed intended.
        rows = (
            await db.execute(select(BeKaizen).where(BeKaizen.plantId == plant.id))
        ).scalars().all()
        cycle = be.kaizen_cycle_times(rows)
        heads = await be.resolve_plant_headcount(db, [plant.id])
        part = be.summarise_participation(rows, headcount=heads.get(plant.id))
        badged = [r for r in rows if be.fast_track_eligibility(r)["eligible"]]
        unowned = [r for r in rows if not r.ownerId]

        print(f"\nWrote {len(created)} Kaizen records and {replications} replication(s).\n")
        print("What the register will now show at this plant:")
        print(f"  records                     {len(rows)}")
        print(f"  median days to implement    {cycle['medianDaysRaisedToImplemented']} "
              f"(from {cycle['implementedSampleSize']}, floor {cycle['minimumSampleSize']})")
        print(f"  median days to verified     {cycle['medianDaysRaisedToVerified']} "
              f"(from {cycle['verifiedSampleSize']})")
        print(f"  distinct submitters         {part['submitters']}")
        print(f"  participation rate          {part['participationRate']}"
              f"{'  (no FactoryProfile at this plant — expected)' if part['headcount'] is None else ''}")
        print(f"  fast-track badges earned    {len(badged)}")
        print(f"  records with no owner       {len(unowned)}")

        if cycle["medianDaysRaisedToImplemented"] is None:
            print("\n⚠ The median tile will still read 'Not enough data'.")
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--force", action="store_true", help="seed again even if a dataset exists")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.dry_run, args.force)))
