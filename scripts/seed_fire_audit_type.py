"""Configure the CAMS Fire Safety AUDIT type and its approved checklist template.

Idempotent. Creates (or leaves as-is):
  * CamsTemplate  FSA-TEMPLATE  — "Fire Safety Audit", APPROVED, 5 sections
  * CamsAuditType FIRE_SAFETY_AUDIT — COMPLIANCE_AUDIT, default template above,
    fire standards (NBC 2016 Part 4, IS 2190, IS 2189, IS 15105, IS 3844)

This is the CAMS side of Fire & Life Safety: an independence-checked audit by a
lead auditor against fire codes, distinct from the routine technician checklists
(PIL-FAS-*, PIL-FHS-*, PIL-FE-*) which are seeded by seed_fire_checklists.py.

    python scripts/seed_fire_audit_type.py            # dry run
    python scripts/seed_fire_audit_type.py --commit
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.cams import (  # noqa: E402
    CamsAuditType,
    CamsTemplate,
    CamsTemplateQuestion,
    CamsTemplateSection,
)
from app.models.fire_audit import FIRE_AUDIT_TYPE_CODE  # noqa: E402
from app.models.user import User  # noqa: E402

TEMPLATE_CODE = "FSA-TEMPLATE"
STANDARDS = ["NBC_2016_PART_4", "IS_2190", "IS_2189", "IS_15105", "IS_3844"]

SECTIONS: list[tuple[str, float, list[tuple[str, str]]]] = [
    ("Portable fire extinguishers", 25, [
        ("Extinguishers of the correct class are provided for each hazard area", "IS 2190:2010 cl.5"),
        ("Every extinguisher is mounted, visible, unobstructed and signed", "IS 2190:2010 cl.7"),
        ("Monthly inspection record is complete for the last 3 months", "IS 2190:2010 cl.8.2"),
        ("Hydrostatic test and refilling are within due dates", "IS 2190:2010 cl.8.4"),
    ]),
    ("Fire detection & alarm", 25, [
        ("Fire alarm panel shows normal status with no unacknowledged faults", "IS 2189:2008 cl.12"),
        ("Detectors are provided per coverage plan and are unobstructed", "IS 2189:2008 cl.6"),
        ("Manual call points are accessible and signed", "IS 2189:2008 cl.7.6"),
        ("Daily panel checks and quarterly testing are recorded", "IS 2189:2008 cl.13"),
    ]),
    ("Hydrant & sprinkler systems", 20, [
        ("Hydrant/hose reel points are accessible with hoses and branches in place", "IS 3844:1989 cl.4"),
        ("Pump room: jockey/main/diesel pumps on auto; pressure within range", "NBC 2016 Part 4 cl.5.1.4"),
        ("Sprinkler heads unobstructed; 450 mm storage clearance maintained", "IS 15105:2002 cl.6"),
    ]),
    ("Means of escape & housekeeping", 20, [
        ("Exit routes and emergency exits are unobstructed and unlocked", "NBC 2016 Part 4 cl.4.4"),
        ("Exit signage and emergency lighting are functional", "NBC 2016 Part 4 cl.4.4.2.5"),
        ("Combustible storage and stock stacking do not block escape or fire equipment", "NBC 2016 Part 4 cl.4.2"),
    ]),
    ("Records, training & drills", 10, [
        ("Fire drill conducted within the last 6 months with findings closed", "NBC 2016 Part 4 cl.6.4"),
        ("Fire wardens nominated and trained for each shift", "NBC 2016 Part 4 cl.6.3"),
    ]),
]


async def main(commit: bool) -> None:
    async with AsyncSessionLocal() as db:
        owner = (
            await db.execute(select(User.id).where(User.email.in_(["admin@safeops360.in", "sysadmin@safeops360.in"])))
        ).scalars().first() or (await db.execute(select(User.id).limit(1))).scalars().first()

        tpl = (await db.execute(select(CamsTemplate).where(CamsTemplate.templateCode == TEMPLATE_CODE))).scalars().first()
        if tpl is None:
            now = datetime.now(timezone.utc)
            tpl = CamsTemplate(
                templateCode=TEMPLATE_CODE, name="Fire Safety Audit",
                description="Independent Fire & Life Safety audit against NBC 2016 Part 4 and the IS fire codes.",
                applicableEngagementTypes=["COMPLIANCE_AUDIT"], standardRefs=STANDARDS, version=1,
                status="APPROVED", approvedBy=owner, approvedAt=now, ownerId=owner, isGlobal=True,
                scoringConfig={"method": "WEIGHTED_SECTIONS"},
            )
            db.add(tpl)
            await db.flush()
            for si, (title, weight, questions) in enumerate(SECTIONS):
                sec = CamsTemplateSection(templateId=tpl.id, orderIndex=si, title=title, weightPct=weight)
                db.add(sec)
                await db.flush()
                for qi, (text, clause) in enumerate(questions):
                    db.add(CamsTemplateQuestion(
                        sectionId=sec.id, orderIndex=qi, text=text, questionType="CONFORM_NC_NA",
                        isMandatory=True, standardClauseRef=clause, ncTriggersFinding=True,
                        evidenceRequiredOnNc=True,
                    ))
            print(f"template {TEMPLATE_CODE}: created ({sum(len(q) for _, _, q in SECTIONS)} questions)")
        else:
            print(f"template {TEMPLATE_CODE}: exists")

        at = (await db.execute(select(CamsAuditType).where(CamsAuditType.typeCode == FIRE_AUDIT_TYPE_CODE))).scalars().first()
        if at is None:
            db.add(CamsAuditType(
                typeCode=FIRE_AUDIT_TYPE_CODE, name="Fire Safety Audit", engagementType="COMPLIANCE_AUDIT",
                defaultTemplateId=tpl.id, defaultRecurrence="ANNUAL", requiresAssetRef=False,
                requiresAuditorCompetency=[], regimeCode="FIRE", standardRefs=STANDARDS, isActive=True,
                createdBy=owner,
            ))
            print(f"audit type {FIRE_AUDIT_TYPE_CODE}: created")
        else:
            print(f"audit type {FIRE_AUDIT_TYPE_CODE}: exists")

        if commit:
            await db.commit()
            print("committed")
        else:
            await db.rollback()
            print("dry run — rolled back (pass --commit)")


if __name__ == "__main__":
    asyncio.run(main("--commit" in sys.argv))
