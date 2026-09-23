"""Seed the PTW precaution catalog from PIL/EHSD/CL/038-R2 (General Work Permit).

Source: "PIL EHSD CL 038 - General Work Permit.xlsx", back side, plus the
front-side Part-A electrical clearance block. Every line below is transcribed
verbatim from that document — including its punctuation quirks — so an auditor
comparing the screen against the controlled form sees the same words.

⚠ THIS SCRIPT WRITES TO WHATEVER `DATABASE_URL` POINTS AT, WHICH IS PRODUCTION.
   It is additive and idempotent by design:
     • upserts on (hazardType, sequence) — re-running updates text in place
     • NEVER deletes, and never touches PermitPrecautionResponse
   Retiring a line is `isActive = False`, not a delete, so responses already
   recorded against it stay renderable.

   Run:  python -m app.seed.seed_ptw_precautions

Coverage note — read before demoing:
   LIFTING and CIVIL get NO items. The source document ticks "Civil Work" as a
   work type but carries no civil checklist (its precautions sit in the General
   Work list), and has no lifting checklist at all. Content for those two has
   to come from the client; nothing is invented here. An annexure with an empty
   catalog is trivially "complete", so attaching one of those hazards today
   adds the workflow routing and the validity cap but no checklist gate.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.permit import PermitHazardType, PermitPrecautionItem

_DOC = "PIL/EHSD/CL/038-R2"

# (isMandatory, allowsNA, text)
_Item = tuple[bool, bool, str]

CONFINED_SPACE: list[_Item] = [
    (True, False, "Supervisor must be continuously available at the site during the work."),
    (True, False, "Ensure the area is cordoned off & precautionary tags/boards provided."),
    (True, False, "Ensure proper illumination & ventilation arrangements are in place before the start of work."),
    (True, False, "Check the presence of toxic, flammable or oxygen displacing vapours/gases in the confined space."),
    (True, False, "Ensure that the confined space is positively isolated from any sources of energy and is properly steamed/purged/water flushed/drained/depressurized."),
    (True, True, "Ensure at least two teams comprising of 2 persons each are available to work on rotation basis inside the confined space (No person should work for more than 30 minutes continuously)."),
    (True, False, "Always use suitable respiratory protection for entry in confined spaces."),
    (True, True, "Entry into manhole shall be manned by an attendant at all the times, till the last man comes out from inside."),
    (True, True, "Attendants at manhole shall be trained in rescue, use of respirator, firefighting equipment and emergency procedures."),
    (True, False, "Maintain attendance sheet for all entries and provide adequate communication facility to attendant to remain in-touch with the persons working inside."),
    (True, False, "Ensure at least 2 proper means of exits/escape provided to the confined space."),
    (True, False, "Lifelines with safety harness should be used for entry in confined space."),
    (True, True, "Only 24V hand lamps should be allowed inside confined space."),
    (True, True, "All electrical connection shall be provided with ELCB 30mA rating type."),
    (True, True, "Ensure flame resistant apron is provided to the welder to prevent sparks entering the clothing & boots."),
    (True, False, "Ensure first aid box is available at the work place."),
    (False, True, "Any other site-specific safety precautions."),
    (True, False, "Opening of confined space has been guaranteed against closure."),
]

WORK_AT_HEIGHT: list[_Item] = [
    (True, True, "Ensure scaffolds are of good construction, adequate strength with toe boards and wide screens."),
    (True, True, "Ensure 50 mm clear walkway is maintained on the scaffold."),
    (True, True, "Ensure scaffold is well secured with stairways and handrails are wide enough to pass two persons at a time."),
    (True, False, 'Ensure good housekeeping at work location/site and "men at work" sign posted.'),
    (True, True, "Ensure safety nets are fixed at the bottom of scaffold to prevent bodily injury in case of fall."),
    (True, False, "All elevated working platforms, portable & fixed ladders and scaffolding are checked before use."),
    (True, True, "Elevated working platforms are fitted with handrails."),
    (True, False, "Proper access is available for working platforms."),
    (True, True, "Ensure well maintained ladders of strong material are put into use."),
    (True, True, "Ensure ladder is not placed against loose boxes, materials, round objects and near electrical installations."),
    (True, True, "Ensure ladder of sufficient height is used, on top tied down and man positioned at foot of ladder."),
    (True, True, "Ensure ladder placed at an angle of 70 to 75 degrees."),
    (True, False, "Area of work barricaded so that no person can walk below the ladder."),
    (True, True, "Ensure that the ground surface is stable where ladder is positioned."),
    (True, False, "Check for overhead electrical lines. If they are present, ensure that power is isolated to the overhead lines."),
    (True, False, "Ensure safety shoes, safety belts and safety helmets are used by the person working at height."),
    (True, False, "Ensure full body safety harness with double lanyard is used."),
    (True, False, "Ensure rigid support is available to anchor full body harness."),
    (True, False, "Ensure proper lifeline is available (Vertical or Horizontal)."),
    (True, False, "Ensure that the person(s) carrying out height work are medically fit from vertigo, general giddiness & height related diseases."),
]

FRAGILE_ROOF: list[_Item] = [
    (True, False, "Ensure that roof plans and drawings have been pre-checked for fragile areas (e.g. skylights/repaired sections etc.,)"),
    (True, False, "Ensure the roof surface has no slipping/tripping hazard such as debris/oil etc.,"),
    (True, False, "Ensure all the equipment that are to be used are safe/free from defects and do not cause harm to the personnel."),
    (True, True, "Ensure safety nets provided under the roof."),
    (True, False, "Ensure that crawl board/roof ladders are used."),
    (True, False, "Ensure edge protection is provided to prevent fall."),
    (True, False, "Ensure that the person(s) carrying out work are medically fit from vertigo, general giddiness & height related diseases."),
    # The form prints this as one line with five circle-Yes/No sub-prompts and
    # the standing instruction beneath it. Kept as a single mandatory item so
    # the wording — and the instruction — survive intact.
    (True, False,
     "Ensure that risks are assessed for the following potential hazards: "
     "Stormy weather with lightening; Excess heat which can cause harm to body; "
     "Extreme flashy light; Presence of Overhead high voltage transmission lines "
     "near the place of work; Emissions of poisonous gases & steam vents near the "
     "place of work. No work to be carried out unless proper safe guards are put "
     "in place to mitigate the above-mentioned risk."),
]

GENERAL: list[_Item] = [
    (True, False, "Tool box is provided."),
    (True, False, "Good housekeeping is maintained & area is barricaded."),
    (True, False, "Applicable caution boards are displayed."),
    (True, False, "Required PPE's are available."),
    (True, True, "Plant or system is isolated from all source of energy."),
    (True, False, "Tools/equipment's are inspected and safe for use."),
    (True, False, "Work area is free from slip & trips."),
    (True, True, "Portable electrical tools are inspected & safe for use."),
]

# The source numbers these 1–10 then 12–14, skipping 11. Sequence below is
# contiguous (1–13); the printed numbering is not reproduced because it would
# make `sequence` non-contiguous for no gain.
HOT_WORK: list[_Item] = [
    (True, True, "All lines leading to and from the equipment are disconnected and isolated."),
    (True, True, "Equipment washed thoroughly with steam/water."),
    (True, True, "Pipeline/equipment purged with air/nitrogen."),
    (True, False, "Area under/around the hot work place is cordoned off/cleared off."),
    (True, False, "Person engaged is experienced and suitable for the job and has been explained on safe work method."),
    (True, False, "Work area is safe for hot work operation. No combustible or flammable materials are within 10 meters of the hot work. Explosives atmosphere due to flammable gases from paints, thinner, compound or gas concentration have been eliminated."),
    (True, True, "Combustible and flammable materials were relocated or adequately protected from flames, sparks, arcs or slag."),
    (True, False, "Assigned fire watch guard has the knowledge of identifying fire hazards and operation of firefighting equipment. Hot work area must be monitored at least one hour after the job is completed to ensure that no fire hazards exists. Return permit to the person who issued it after one-hour monitoring requirement."),
    (True, False, "Firefighting equipment is available for immediate use in the area of hot work operation."),
    (True, True, "Welding Machine, Cables and electrode holder are safe for operation."),
    (True, True, "Oxygen and Acetylene cylinders are not inside a confined space."),
    (True, True, "Welding torch, hoses, fittings and regulators have been inspected and found to be safer for operation."),
    (True, True, "Cylinder is connected with flash back arrestor."),
]

EXCAVATION: list[_Item] = [
    (True, True, "Tracing equipment required (i.e. CAT)."),
    (True, True, "Shoring required as excavation > 1.25 meters in depth or Soil stability."),
    (True, True, "The isolator is locked out and tagged (LOTO)."),
    (True, False, "The above described work is recorded in the log sheet/log book, including the instructions for reliever."),
    (True, False, "Good housekeeping is maintained & area is barricaded."),
    (True, False, "Applicable signage boards are displayed."),
    (True, True, "Airline, cable, waterline etc., are checked."),
    (True, False, "Safe means of ingress and egress."),
    (True, False, "Loose tools & other materials are kept 2 meters away from the edge of excavation."),
    (True, True, "Support to trench is provided to prevent collapse."),
    (True, True, "Ladders are extended 3 feet above the trench."),
]

# Front side, "PART-A (Electrical Work Permit)". On paper this is a conditional
# block gated on "Will electrical power be used?"; here it is an annexure, so a
# General Work permit can carry electrical clearance without a second permit.
ELECTRICAL_LOTO: list[_Item] = [
    (True, False, "The equipment identified for the work has been isolated from electrical supply by using necessary instruments (record the identification numbers of switch fuse link / isolator / MCCB / HT breaker in the remark)."),
    (True, True, "The bus bar is properly earthed and secured."),
    (True, False, "Caution/Danger Signs are placed at all points of isolation."),
    (True, False, "The above described work is recorded in the log sheet/log book, including the instructions for reliever."),
    (True, False, "I have physically identified the equipment. The concerned supervisor has been informed with necessary single line diagram about the safety arrangements taken, points of isolation done, earthing of live parts done and disconnection of the power supply feeding the equipment. It is now safe to work on the equipment mentioned above."),
]

CATALOG: dict[PermitHazardType, list[_Item]] = {
    PermitHazardType.CONFINED_SPACE: CONFINED_SPACE,
    PermitHazardType.WORK_AT_HEIGHT: WORK_AT_HEIGHT,
    PermitHazardType.FRAGILE_ROOF: FRAGILE_ROOF,
    PermitHazardType.GENERAL: GENERAL,
    PermitHazardType.HOT_WORK: HOT_WORK,
    PermitHazardType.EXCAVATION: EXCAVATION,
    PermitHazardType.ELECTRICAL_LOTO: ELECTRICAL_LOTO,
}


async def seed_ptw_precautions() -> dict[str, int]:
    created = updated = unchanged = 0

    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(PermitPrecautionItem))).scalars().all()
        by_key = {(r.hazardType, r.sequence): r for r in existing}

        for hazard, items in CATALOG.items():
            for idx, (mandatory, allows_na, text) in enumerate(items, start=1):
                row = by_key.get((hazard, idx))
                if row is None:
                    db.add(
                        PermitPrecautionItem(
                            hazardType=hazard,
                            sequence=idx,
                            text=text,
                            isMandatory=mandatory,
                            allowsNA=allows_na,
                            sourceRef=_DOC,
                            isActive=True,
                        )
                    )
                    created += 1
                elif (
                    row.text != text
                    or row.isMandatory != mandatory
                    or row.allowsNA != allows_na
                    or not row.isActive
                ):
                    row.text = text
                    row.isMandatory = mandatory
                    row.allowsNA = allows_na
                    row.sourceRef = _DOC
                    row.isActive = True
                    updated += 1
                else:
                    unchanged += 1

        await db.commit()

    return {"created": created, "updated": updated, "unchanged": unchanged}


async def _main() -> None:
    stats = await seed_ptw_precautions()
    total = sum(len(v) for v in CATALOG.values())
    print("PTW precaution catalog seeded from " + _DOC)
    for hazard, items in CATALOG.items():
        print(f"  {hazard.value:<16} {len(items):>3} items")
    for hazard in (PermitHazardType.LIFTING, PermitHazardType.CIVIL):
        print(f"  {hazard.value:<16}   0 items  ⚠ no checklist in the source document")
    print(
        f"\n  total {total} items — "
        f"created {stats['created']}, updated {stats['updated']}, unchanged {stats['unchanged']}"
    )


if __name__ == "__main__":
    asyncio.run(_main())
