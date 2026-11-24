"""Seed the PTW Control Center demo plant — "Meridian North Works · West DC Bhiwandi".

    ALLOW_DEMO_SEED=1 python -m scripts.seed_ptw_control_center
    (frontend shortcut: npm run db:seed-ptw-control-center)

Writes exactly the scenario in app/services/ptw_ops/demo.py (the same data
tests/test_ptw_ops.py asserts against), relative to the time it is run:
7 zones with polygons + guidance, ~40 permits today (12 active, 4 expired-open
with 2 today, 6 in Issuer Review past SLA, 4 closure pending, 11 closed today,
1 SIMOPS pair), 3 impairments, 4 contractors, 87 workers checked in, and 12
weeks of history (49 of 352 expired). Then runs the exception engine (no
notifications) and writes a shift brief.

Re-runnable: it wipes and rebuilds ONLY the demo plant's permits, workflow
rows, impairments, exceptions and briefs. It refuses to run unless
ALLOW_DEMO_SEED=1 (main is the production deploy) and unless the control-center
DDL has been applied.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.db import AsyncSessionLocal
from app.models._base import gen_id
from app.models.permit import (
    Permit,
    PermitClosureType,
    PermitCrewMember,
    PermitGasTestPlan,
    PermitGasTestReading,
    PermitIsolation,
    PermitStatus,
    PermitType,
)
from app.models.plant import Area, Plant
from app.models.ptw_ops import Impairment, PermitControl, PlantLayout, ZoneGuidance
from app.models.user import User
from app.models.workflow import TaskType, WorkflowDefinition, WorkflowInstance, WorkflowStep, WorkflowTask
from app.services.ptw_ops import brief, demo, engine, loaders, steps

EMAIL_DOMAIN = "meridian-demo.invalid"   # .invalid: can never receive mail
UNUSABLE_HASH = "!demo-account-no-login"


def _email(slug: str) -> str:
    return f"{slug}@{EMAIL_DOMAIN}"


async def _user(db, slug: str, name: str, role: str, plant_id: str, designation: str | None = None) -> str:
    row = (await db.execute(select(User).where(User.email == _email(slug)))).scalar_one_or_none()
    if row is None:
        row = User(email=_email(slug), name=name, passwordHash=UNUSABLE_HASH, role=role, plantId=plant_id,
                   designation=designation)
        db.add(row)
        await db.flush()
    else:
        row.name, row.role, row.plantId = name, role, plant_id
    return row.id


async def _workflow_issuer_step(db, actor_id: str) -> tuple[str, str, str]:
    """(definitionId, stepId, stepName) of an active PTW Issuer Review step, creating
    a minimal demo definition only when the instance has no PTW workflow at all."""
    row = (await db.execute(
        select(WorkflowStep.id, WorkflowStep.name, WorkflowStep.definitionId)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowStep.definitionId)
        .where(WorkflowDefinition.module == "PTW").where(WorkflowDefinition.isActive.is_(True))
        .where(WorkflowStep.name.ilike("%issuer review%"))
        .order_by(WorkflowStep.slaHours.asc().nulls_last()).limit(1)
    )).first()
    if row:
        return row.definitionId, row.id, row.name
    d = WorkflowDefinition(module="PTW", recordType=None, name="PTW — Control Center demo", isActive=True)
    db.add(d)
    await db.flush()
    s = WorkflowStep(definitionId=d.id, sequence=2, stepType="CHECKER", name="Issuer Review",
                     approverRole="PERMIT_ISSUER", slaHours=2)
    db.add(s)
    await db.flush()
    return d.id, s.id, s.name


async def wipe(db, plant_id: str) -> None:
    ids = [r[0] for r in (await db.execute(text('SELECT id FROM "Permit" WHERE "plantId" = :p'), {"p": plant_id})).all()]
    if ids:
        await db.execute(text("""DELETE FROM "WorkflowTask" WHERE module = 'PTW' AND "recordId" = ANY(:ids)"""), {"ids": ids})
        await db.execute(text("""DELETE FROM "WorkflowHistory" WHERE "instanceId" IN
                                 (SELECT id FROM "WorkflowInstance" WHERE module = 'PTW' AND "recordId" = ANY(:ids))"""),
                         {"ids": ids})
        await db.execute(text("""DELETE FROM "WorkflowInstance" WHERE module = 'PTW' AND "recordId" = ANY(:ids)"""), {"ids": ids})
        # Permit is a governed (soft-delete) entity; the ORM guard blocks hard
        # deletes, so the demo plant's rows go by SQL. Children cascade.
        await db.execute(text('DELETE FROM "Permit" WHERE id = ANY(:ids)'), {"ids": ids})
    for t in ("PtwException", "PtwShiftBrief", "Impairment"):
        await db.execute(text(f'DELETE FROM "{t}" WHERE "plantId" = :p'), {"p": plant_id})  # noqa: S608
    await db.flush()


async def seed() -> dict:
    now = datetime.now(timezone.utc)
    sc = demo.build(now)
    async with AsyncSessionLocal() as db:
        s = await loaders.schema(db, fresh=True)
        if not s.get("ready"):
            missing = [k for k, v in s.items() if v is False]
            sys.exit(f"Control-center DDL not applied (missing: {', '.join(missing)}). "
                     "Run: npx tsx prisma/apply-ptw-control-center-ddl.ts")

        plant = (await db.execute(select(Plant).where(Plant.code == demo.PLANT_CODE))).scalar_one_or_none()
        if plant is None:
            plant = Plant(code=demo.PLANT_CODE, name=demo.PLANT_NAME, location=demo.PLANT_LOCATION,
                          state="Maharashtra", unitType="Distribution Center")
            db.add(plant)
            await db.flush()
        pid = plant.id
        await wipe(db, pid)

        # ── people ──
        issuer = await _user(db, "issuer", "Ravi Kulkarni", "PERMIT_ISSUER", pid, "Permit Issuer")
        safety = await _user(db, "safety", "Sneha Patil", "SAFETY_OFFICER", pid, "Safety Officer")
        fire = await _user(db, "fire-officer", "Imran Shaikh", "SAFETY_OFFICER", pid, "Fire Officer")
        await _user(db, "plant-head", "Anil Deshmukh", "PLANT_HEAD", pid, "Plant Head")
        receivers = [await _user(db, f"receiver-{i}", n, "SUPERVISOR", pid, "Permit Receiver")
                     for i, n in enumerate(["Vikas Jadhav", "Pooja Naik", "Suresh More", "Farhan Qureshi"], 1)]
        owners = {}
        for z in demo.ZONES:
            owners[z.key] = await _user(db, f"owner-{z.key}", z.owner, "DEPARTMENT_HEAD", pid, z.owner)
        workers = [await _user(db, f"worker-{i:03d}", f"Meridian Worker {i:03d}", "WORKER", pid, "Technician")
                   for i in range(1, 101)]

        # ── contractors ──
        companies = {}
        for c in demo.CONTRACTORS:
            cid = (await db.execute(text(
                """INSERT INTO "ContractorCompany" (id, name, status, score, "updatedAt")
                   VALUES (:id, :n, 'ACTIVE', :s, now())
                   ON CONFLICT (name) DO UPDATE SET score = EXCLUDED.score, status = 'ACTIVE', "updatedAt" = now()
                   RETURNING id"""), {"id": gen_id(), "n": c["name"], "s": c["score"]})).scalar_one()
            companies[c["key"]] = (cid, c["name"])

        # ── zones, layout, guidance ──
        areas = {}
        existing = {a.name: a for a in (await db.execute(select(Area).where(Area.plantId == pid))).scalars().all()}
        for i, z in enumerate(demo.ZONES):
            a = existing.get(z.name) or Area(name=z.name, plantId=pid)
            a.ownerUserId = owners[z.key]
            db.add(a)
            await db.flush()
            await db.execute(text('UPDATE "Area" SET geometry = CAST(:g AS jsonb), "sortOrder" = :o WHERE id = :id'),
                             {"g": json.dumps(z.polygon), "o": i, "id": a.id})
            areas[z.key] = a.id
            g = (await db.execute(select(ZoneGuidance).where(ZoneGuidance.areaId == a.id))).scalar_one_or_none()
            g = g or ZoneGuidance(areaId=a.id, plantId=pid)
            g.restrictedHours, g.instructions, g.mandatoryPpe = z.restricted, z.instructions, z.ppe
            g.assemblyPoint, g.extinguisherLocations = z.assembly, z.extinguishers
            g.permitTypeRules, g.updatedById, g.updatedAt = z.type_rules, owners[z.key], now - timedelta(days=3)
            db.add(g)
        lay = (await db.execute(select(PlantLayout).where(PlantLayout.plantId == pid))).scalar_one_or_none()
        lay = lay or PlantLayout(plantId=pid)
        lay.widthM, lay.heightM, lay.updatedAt = demo.LAYOUT_W_M, demo.LAYOUT_H_M, now
        db.add(lay)
        await db.flush()

        # ── permits ──
        def_id, step_id, step_name = await _workflow_issuer_step(db, issuer)
        worker_iter = iter(workers)
        permits: dict[str, Permit] = {}
        crew_ids: list[str] = []
        for n, d in enumerate(sc.permits, 1):
            p = Permit(
                number=f"PTW-{demo.PLANT_CODE}-{n:05d}", type=PermitType(d.type), plantId=pid, areaId=areas[d.zone],
                location=next(z.name for z in demo.ZONES if z.key == d.zone), scopeOfWork=d.scope or "Demo job",
                validFrom=d.validFrom, validTo=d.validTo, originatorId=receivers[n % 4], issuerId=issuer,
                receiverId=receivers[n % 4], status=PermitStatus(d.status),
                contractorCompanyId=companies[d.contractor][0] if d.contractor else None,
                contractorName=companies[d.contractor][1] if d.contractor else None,
                gasTestRequired=d.gas is not None or d.type == "CONFINED_SPACE",
                fireWatchRequired=d.type == "HOT_WORK", fireWatchPersonId=safety if d.fireWatch else None,
                activatedAt=d.activatedAt, activatedById=receivers[n % 4] if d.activatedAt else None,
                issuedAt=(d.activatedAt - timedelta(minutes=20)) if d.activatedAt else (
                    now - timedelta(minutes=30) if d.status == "ISSUED" else None),
                workCompletedAt=d.workCompletedAt, siteVerifiedAt=d.siteVerifiedAt, closedAt=d.closedAt,
                suspendedAt=d.suspendedAt, suspendedReason=d.suspendedReason,
                isCurrentlySuspended=d.status == "SUSPENDED", expiredAt=d.expiredAt,
                autoExpiredAt=d.expiredAt if d.status == "EXPIRED" else None,
                cancelledAt=(d.validTo + timedelta(hours=2)) if d.status == "CANCELLED" else None,
                closureType=PermitClosureType(d.closureType) if d.closureType else None,
                workflowDefinitionCode="PTW_HIGH_RISK",
            )
            p.createdAt = d.createdAt or d.validFrom - timedelta(hours=1)
            db.add(p)
            permits[d.key] = p
        await db.flush()

        for d in sc.permits:
            p = permits[d.key]
            for _ in range(d.crew):
                w = next(worker_iter, None)
                if w is None:
                    worker_iter = iter(workers)
                    w = next(worker_iter)
                c = PermitCrewMember(permitId=p.id, userId=w, role="CONTRACTOR" if d.contractor else "WORKER",
                                     trainingValidAtIssuance=True, medicalValidAtIssuance=True,
                                     contractorActiveAtIssuance=True)
                db.add(c)
                await db.flush()
                crew_ids.append(c.id)
            if d.gas:
                db.add(PermitGasTestPlan(permitId=p.id, refreshFrequencyMinutes=60,
                                         parametersToTest=[{"parameter": "O2", "lowLimit": 19.5, "highLimit": 23.5,
                                                            "unit": "%"},
                                                           {"parameter": "LEL", "lowLimit": 0, "highLimit": 10,
                                                            "unit": "%"}]))
                at, due, exc = d.gas
                db.add(PermitGasTestReading(permitId=p.id, recordedAt=at, recordedById=safety, refreshDueBy=due,
                                            isExceedance=exc, isPreEntry=False, readings=[
                                                {"parameter": "O2", "value": 20.8, "unit": "%", "isWithinLimit": True},
                                                {"parameter": "LEL", "value": 12 if exc else 2, "unit": "%",
                                                 "isWithinLimit": not exc}]))
            total, unverified = d.isolations
            for i in range(total):
                db.add(PermitIsolation(permitId=p.id, isolationType="Electrical", description=f"Isolation point {i + 1}",
                                       isolationPointTag=f"ISO-{p.number[-3:]}-{i + 1}",
                                       isolationVerifiedAt=None if i < unverified else (d.activatedAt or now),
                                       isolationVerifiedById=None if i < unverified else issuer))
            for key in d.controls:
                db.add(PermitControl(permitId=p.id, controlKey=key, satisfiedById=fire if key == "fire_officer_cosign" else safety,
                                     satisfiedAt=d.activatedAt or now, note="Seeded demo control"))
            if d.status == "SUBMITTED":
                inst = WorkflowInstance(definitionId=def_id, module="PTW", recordId=p.id, recordNumber=p.number,
                                        initiatedById=p.originatorId, status="IN_PROGRESS", currentStepId=step_id,
                                        currentStepName=step_name, initiatedAt=d.createdAt)
                db.add(inst)
                await db.flush()
                db.add(WorkflowTask(instanceId=inst.id, stepId=step_id, stepName=step_name, taskType=TaskType.APPROVAL,
                                    module="PTW", recordId=p.id, recordNumber=p.number,
                                    recordTitle=p.scopeOfWork[:120], assignedToId=issuer, status="PENDING",
                                    assignedAt=d.createdAt, dueAt=d.createdAt + timedelta(hours=2)))
        await db.flush()

        # crew check-in (column added by the DDL; not mapped on the model)
        working = [permits[d.key].id for d in sc.permits if d.status in ("ACTIVE", "SUSPENDED")]
        await db.execute(text("""UPDATE "PermitCrewMember" SET "checkedInAt" = :t
                                 WHERE "permitId" = ANY(:ids)"""), {"t": now - timedelta(hours=2), "ids": working})

        # ── impairments ──
        for x in sc.impairments:
            db.add(Impairment(plantId=pid, code=x.code, systemType=x.systemType, systemName=x.systemName,
                              areaId=areas[x.zone], reason=x.reason, startedAt=x.startedAt,
                              plannedRestoreAt=x.plannedRestoreAt, precautions=x.precautions, status="active",
                              approvedById=fire, approvedAt=x.startedAt, createdById=safety,
                              createdAt=x.startedAt - timedelta(minutes=30)))
        await db.flush()

        # ── PermitEvent: the triggers stamped everything "now"; replace with the
        #    real step-entry times so dwell is meaningful. ──
        ids = [p.id for p in permits.values()]
        await db.execute(text('DELETE FROM "PermitEvent" WHERE "permitId" = ANY(:ids)'), {"ids": ids})
        rows = []
        for d in sc.permits:
            p = permits[d.key]
            key = steps.step_key(d.status, d.workflowStep)
            created = d.createdAt or d.validFrom
            entered = {
                steps.ACTIVE: d.activatedAt, steps.SUSPENDED: d.suspendedAt,
                steps.CLOSURE_PENDING: d.siteVerifiedAt or d.workCompletedAt, steps.CLOSED: d.closedAt,
                steps.EXPIRED: d.expiredAt, steps.AWAITING_ACCEPT: p.issuedAt or created,
            }.get(key) or created
            first = steps.ISSUER_REVIEW
            rows.append({"id": gen_id(), "p": p.id, "f": None, "t": first, "a": p.originatorId, "at": created})
            if key != first:
                rows.append({"id": gen_id(), "p": p.id, "f": first, "t": key, "a": issuer, "at": max(entered, created)})
        await db.execute(text("""INSERT INTO "PermitEvent" (id, "permitId", "fromStep", "toStep", "actorId", "createdAt")
                                 VALUES (:id, :p, :f, :t, :a, :at)"""), rows)
        await db.commit()

    # Engine + brief in their own transactions (they read what was just committed).
    async with AsyncSessionLocal() as db:
        loaders.reset_schema_cache()
        ev = await engine.evaluate_plant(db, pid, send=False)
        br = await brief.generate(db, pid, force=True)
        await db.commit()
    return {"plantId": pid, "permits": len(sc.permits), "impairments": len(sc.impairments),
            "crew": len(crew_ids), "exceptions": ev, "brief": br}


if __name__ == "__main__":
    if os.getenv("ALLOW_DEMO_SEED") != "1":
        sys.exit("Refusing to seed: set ALLOW_DEMO_SEED=1 (this writes a demo plant into the configured database).")
    print(json.dumps(asyncio.run(seed()), indent=2, default=str))
