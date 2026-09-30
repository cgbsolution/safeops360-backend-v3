"""PTW — route issuer steps to the Issuer named on the permit, and add the
missing Lifting Operations workflow.

Why
---
* The live definitions (prisma/seed-workflows.ts) assign "Issuer Review" and
  Cold Work's "Issuer Closes Permit" by ROLE (approverRole=PERMIT_ISSUER). The
  engine then picks the first PERMIT_ISSUER at the site — ignoring the Issuer
  the originator chose in the wizard — and at a site with no PERMIT_ISSUER
  (every Meridian Retail store) it fell back to a DC maintenance lead with no
  scope at that store, so store permits could never leave Issuer Review.
  app/seed/seed_workflows.py (the intended design) already uses
  approverField="ISSUER"; this brings the live rows in line. create_permit now
  also checks the named Issuer holds PTW.APPROVE at the site.
* LIFTING is an enabled permit type (and a hazard that can be attached) but
  had no definition: initiate() found nothing and the permit stayed DRAFT.
  It gets the same 6-step high-risk chain as Work at Height.

Safety
------
Steps are updated IN PLACE (ids preserved), so in-flight instances and their
tasks are untouched — tasks already created keep their assignee; only tasks
created from now on use the named Issuer. Each changed definition is
snapshotted to WorkflowDefinitionVersion first (Configuration → Workflows →
History can restore it). The LIFTING definition is a pure insert.

    python -m scripts.ptw_named_issuer_and_lifting           # dry run
    python -m scripts.ptw_named_issuer_and_lifting --apply
"""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import AsyncSessionLocal
from app.models.user import User
from app.models.workflow import StepType, WorkflowDefinition, WorkflowDefinitionVersion, WorkflowStep

LIFTING_TEMPLATE = "WORK_AT_HEIGHT"


def _snapshot(d: WorkflowDefinition) -> dict:
    return {
        "module": d.module, "recordType": d.recordType, "name": d.name, "description": d.description, "isActive": d.isActive,
        "steps": [
            {
                "sequence": s.sequence, "stepType": s.stepType.value if hasattr(s.stepType, "value") else s.stepType,
                "name": s.name, "approverRole": s.approverRole, "approverField": s.approverField,
                "approverUserId": s.approverUserId, "approverGroupRoles": s.approverGroupRoles, "slaHours": s.slaHours,
                "slaUnit": s.slaUnit, "escalationRole": s.escalationRole, "isOptional": s.isOptional,
                "conditionExpr": s.conditionExpr, "notes": s.notes,
            }
            for s in sorted(d.steps, key=lambda x: x.sequence)
        ],
    }


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        editor = (await db.execute(select(User).where(User.email == "admin@safeops360.in"))).scalar_one_or_none()
        if editor is None:
            editor = (await db.execute(select(User).order_by(User.createdAt).limit(1))).scalar_one()
        defs = (
            await db.execute(
                select(WorkflowDefinition)
                .where(WorkflowDefinition.module == "PTW", WorkflowDefinition.isActive.is_(True))
                .options(selectinload(WorkflowDefinition.steps))
            )
        ).scalars().all()
        by_type = {d.recordType: d for d in defs}

        # 1. issuer steps → named Issuer
        for d in sorted(defs, key=lambda x: x.recordType or ""):
            targets = [s for s in d.steps if s.approverRole == "PERMIT_ISSUER"]
            if not targets:
                print(f"  {d.recordType:<16} no role-assigned issuer steps — skip")
                continue
            for s in targets:
                print(f"  {d.recordType:<16} step {s.sequence} {s.name!r}: approverRole PERMIT_ISSUER → approverField ISSUER")
            if apply:
                last = (
                    await db.execute(
                        select(WorkflowDefinitionVersion.version)
                        .where(WorkflowDefinitionVersion.definitionId == d.id)
                        .order_by(WorkflowDefinitionVersion.version.desc()).limit(1)
                    )
                ).scalar_one_or_none() or 0
                db.add(WorkflowDefinitionVersion(
                    definitionId=d.id, version=last + 1, snapshot=json.dumps(_snapshot(d)), editedById=editor.id,
                    changeNote="Issuer steps assigned to the Issuer named on the permit (was: first PERMIT_ISSUER at the site).",
                ))
                for s in targets:
                    s.approverRole = None
                    s.approverField = "ISSUER"

        # 2. LIFTING definition
        if "LIFTING" in by_type:
            print("  LIFTING          definition already exists — skip")
        else:
            tpl = by_type[LIFTING_TEMPLATE]
            print(f"  LIFTING          create 'PTW — Lifting Operations' from {LIFTING_TEMPLATE} ({len(tpl.steps)} steps)")
            if apply:
                new = WorkflowDefinition(
                    module="PTW", recordType="LIFTING", name="PTW — Lifting Operations", isActive=True,
                    description="Issuer → Safety Officer → Plant Head → Receiver acknowledges → Safety Officer closes",
                )
                db.add(new)
                await db.flush()
                for s in sorted(tpl.steps, key=lambda x: x.sequence):
                    db.add(WorkflowStep(
                        definitionId=new.id, sequence=s.sequence, stepType=s.stepType, name=s.name,
                        approverRole=None if s.approverRole == "PERMIT_ISSUER" else s.approverRole,
                        approverField="ISSUER" if s.approverRole == "PERMIT_ISSUER" else s.approverField,
                        approverUserId=s.approverUserId, approverGroupRoles=s.approverGroupRoles, slaHours=s.slaHours,
                        slaUnit=s.slaUnit, escalationRole=s.escalationRole, isOptional=s.isOptional,
                        conditionExpr=s.conditionExpr, notes=s.notes, parallelStrategy=s.parallelStrategy,
                        slaBySeverity=s.slaBySeverity,
                    ))

        if apply:
            await db.commit()
            print("applied")
        else:
            await db.rollback()
            print("dry run — nothing written (pass --apply)")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
