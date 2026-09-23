"""Meridian Retail — Field Reports, DC permits, scorecard inputs, Daily Brief.

  * Field Reports: 28 mobile captures (/capture PWA shape: tap count, duration,
    offline sync, voice note, map pin) by store floor staff; 13 converted into
    Near Misses / Incidents exactly as the triage "convert" action leaves them
    (capture status converted + convertedEntity*, record reporter = floor staff).
  * PTW: 11 DC maintenance permits (racking, HVAC, electrical) in mixed states.
    ACTIVE ones get a future validTo so the hourly expiry scan leaves them alone.
  * Manhours for the two previous months only. The current month has none, so
    the EHS Scorecard shows LTIFR/TRIR as "cannot be computed", not zero.
  * Daily Brief cards as materialised Alert rows scoped to Retail sites.
  * Scorecard rollup for the Retail sites.

No workflow instances are created: the stock workflow steps name stock roles,
and the record pages render without an instance.

    python -m scripts.meridian_retail.ops            # dry run
    python -m scripts.meridian_retail.ops --commit
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone

from psycopg2.extras import execute_values

from scripts.meridian_retail.common import EMAIL_DOMAIN, RNG, conn, dc_code, new_id, now, plant_map, store_code, user_map


def _naive(d: datetime) -> datetime:
    """These tables are timestamp WITHOUT time zone — store naive UTC."""
    return d.astimezone(timezone.utc).replace(tzinfo=None)


# (store no., staff tag, capture type, L1 code, area, severity, description, voice?, conversion)
# conversion: None | ("NearMiss", potentialSeverity, status) | ("Incident", type, severity, status, lostDays)
REPORTS = [
    (1, "a", "near_miss", "slip_trip_fall", "Sales Floor", "high", "Water leaking from chiller cabinet at produce section, customer nearly slipped near aisle 3.", True, ("NearMiss", "HIGH", "ACTION_ASSIGNED")),
    (1, "b", "unsafe_condition", "housekeeping", "Stockroom", "medium", "Empty cartons stacked against the stockroom sprinkler riser.", False, None),
    (2, "a", "unsafe_condition", "fire", "Checkout & Entrance", "high", "Emergency exit next to checkout 6 blocked by promo display stand.", True, ("NearMiss", "HIGH", "CLOSED")),
    (2, "b", "incident", "material_handling", "Stockroom", "medium", "Box cutter slipped while opening a carton — cut on left thumb, first aid given.", False, ("Incident", "FIRST_AID", "LOW", "CLOSED", 0)),
    (3, "a", "near_miss", "working_at_height", "Stockroom", "high", "Step ladder top step cracked; associate stood on it to reach top shelf.", True, ("NearMiss", "HIGH", "UNDER_REVIEW")),
    (3, "b", "incident", "working_at_height", "Stockroom", "high", "Associate fell from 3rd rung of ladder while shelving stock, ankle sprain — 4 days off.", False, ("Incident", "LTI", "HIGH", "INVESTIGATION", 4)),
    (4, "a", "unsafe_condition", "electrical", "Sales Floor", "high", "Exposed wiring at back of illuminated signage above billing counter.", False, ("NearMiss", "HIGH", "ACTION_ASSIGNED")),
    (4, "b", "observation", "ppe", "Cold Room", "low", "Cold room gloves missing from the entry rack.", False, None),
    (5, "a", "near_miss", "material_handling", "Stockroom", "medium", "Stock fell from top shelf during restock, missed associate by a metre.", True, ("NearMiss", "MEDIUM", "REPORTED")),
    (5, "b", "unsafe_condition", "slip_trip_fall", "Checkout & Entrance", "medium", "Entrance mat curled at edge, trip hazard in rain.", False, None),
    (6, "a", "incident", "ergonomics", "Stockroom", "medium", "Back strain lifting 25 kg rice bags without trolley — sent for physiotherapy.", False, ("Incident", "MTC", "MEDIUM", "CAPA_ASSIGNED", 0)),
    (6, "b", "near_miss", "vehicle_forklift", "Stockroom", "high", "Pallet jack rolled down receiving ramp with no one on the handle.", True, ("NearMiss", "HIGH", "CLOSED")),
    (7, "a", "unsafe_condition", "fire", "Back Office", "high", "Fire extinguisher at back office missing its safety pin and tag.", False, None),
    (7, "b", "observation", "housekeeping", "Sales Floor", "low", "Broken glass jar cleaned up but no caution sign placed.", False, None),
    (8, "a", "unsafe_condition", "fire", "Back Office", "high", "Fire alarm panel shows fault light; nobody has logged it.", True, ("NearMiss", "CRITICAL", "ACTION_ASSIGNED")),
    (8, "b", "near_miss", "slip_trip_fall", "Cold Room", "medium", "Ice build-up on cold room floor, associate slipped but held the rack.", False, ("NearMiss", "MEDIUM", "UNDER_REVIEW")),
    (9, "a", "incident", "electrical", "Sales Floor", "high", "Smoke from overloaded extension board at seasonal display; extinguished with CO2 extinguisher.", False, ("Incident", "FIRE", "HIGH", "INVESTIGATION", 0)),
    (9, "b", "unsafe_condition", "housekeeping", "Stockroom", "medium", "Aisle in stockroom narrowed to under 60 cm by overflow stock.", False, None),
    (10, "a", "near_miss", "material_handling", "Stockroom", "medium", "Roll cage tipped while being pushed over the dock plate.", True, ("NearMiss", "MEDIUM", "CLOSED")),
    (10, "b", "observation", "ppe", "Stockroom", "low", "Receiving team not wearing safety shoes during unloading.", False, None),
    (11, "a", "unsafe_condition", "slip_trip_fall", "Sales Floor", "medium", "Spilled oil at aisle 7 left for 20 minutes before cleaning.", False, None),
    (11, "b", "incident", "slip_trip_fall", "Sales Floor", "medium", "Customer slipped on spilled oil at aisle 7 — minor bruise, first aid at store.", False, ("Incident", "FIRST_AID", "MEDIUM", "REPORTED", 0)),
    (12, "a", "near_miss", "fire", "Back Office", "high", "Space heater left on under the desk overnight in back office.", True, ("NearMiss", "HIGH", "REPORTED")),
    (12, "b", "unsafe_condition", "electrical", "Checkout & Entrance", "medium", "POS cable run across walkway at checkout 2.", False, None),
    (13, "a", "near_miss", "slip_trip_fall", "Sales Floor", "high", "Wet floor after mopping without caution board near entrance.", False, ("NearMiss", "MEDIUM", "CLOSED")),
    (13, "b", "unsafe_condition", "fire", "Stockroom", "high", "Sprinkler head in stockroom covered by stacked cartons (under 45 cm clearance).", True, None),
    (14, "a", "near_miss", "vehicle_forklift", "Checkout & Entrance", "high", "Delivery van reversed into customer parking bay without banksman.", False, ("NearMiss", "HIGH", "UNDER_REVIEW")),
    (14, "b", "observation", "housekeeping", "Cold Room", "low", "Cold room door closer weak, door stays ajar.", False, None),
]

# DC incidents so the DCs are in the scorecard universe (it needs an incident,
# observation or manhours row), reported by the DC maintenance lead.
DC_INCIDENTS = [
    (1, "PROPERTY_DAMAGE", "MEDIUM", "CLOSED", "Racking Aisles", "Reach truck struck upright of bay 14 — upright deformed, bay unloaded and cordoned.", 0),
    (2, "FIRST_AID", "LOW", "CLOSED", "Loading Dock", "Finger pinched between dock leveller lip and pallet.", 0),
    (3, "MTC", "MEDIUM", "INVESTIGATION", "Electrical Room", "Minor electric shock while resetting MCC feeder; medical check, no lost time.", 0),
]

# (dc, type, scope, area, status, days ago start, hours valid)
PERMITS = [
    (1, "WORK_AT_HEIGHT", "Replace damaged upright and beam locks, racking bay 14 (post reach-truck strike)", "Racking Aisles", "CLOSED", 40, 8),
    (1, "HOT_WORK", "Braze refrigerant line on rooftop HVAC unit 2", "HVAC Plant Room", "CLOSED", 33, 6),
    (1, "ELECTRICAL_LOTO", "Thermography follow-up: retighten busbar joints, MCC panel 1", "Electrical Room", "CLOSED", 21, 6),
    (1, "WORK_AT_HEIGHT", "Racking inspection repairs — beam connector replacement, aisles 9–12", "Racking Aisles", "ACTIVE", 0, 10),
    (2, "GENERAL_COLD", "HVAC AHU-3 filter and belt replacement", "HVAC Plant Room", "CLOSED", 28, 6),
    (2, "ELECTRICAL_LOTO", "Dock leveller 4 hydraulic power pack motor replacement", "Loading Dock", "CLOSED", 17, 8),
    (2, "WORK_AT_HEIGHT", "Install anti-collapse mesh on high-bay racking, aisle 3", "Racking Aisles", "SUBMITTED", -2, 10),
    (2, "HOT_WORK", "Weld repair on dock bumper bracket, door 6", "Loading Dock", "CANCELLED", 12, 4),
    (3, "ELECTRICAL_LOTO", "Replace faulty ACB in main LT panel", "Electrical Room", "CLOSED", 36, 8),
    (3, "GENERAL_COLD", "Chiller condenser coil cleaning, cold-chain zone", "HVAC Plant Room", "ACTIVE", 0, 8),
    (3, "WORK_AT_HEIGHT", "Relamping high-bay LED fixtures above racking aisles 1–6", "Racking Aisles", "CLOSED", 9, 8),
]


def seed(cur) -> None:
    plants = plant_map(cur)
    users = user_map(cur)
    cur.execute('select count(*) from "CaptureSubmission" where "plantId" = any(%s)', (list(plants.values()),))
    if cur.fetchone()[0]:
        print("ops: Retail field reports already present — skipping")
        return
    RNG.seed(7331)
    cur.execute('select id, name, "plantId" from "Area" where "plantId" = any(%s)', (list(plants.values()),))
    area = {(pid, name): aid for aid, name, pid in cur.fetchall()}
    cur.execute('select id, code, labels, "iconKey" from "CaptureTaxonomy" where kind=%s and level=1', ("HAZARD",))
    tax = {code: (tid, labels, icon) for tid, code, labels, icon in cur.fetchall()}
    manager = {int(e[4:7]): uid for e, uid in users.items() if e.startswith("sm.s")}
    admin = users[f"store-ops.admin@{EMAIL_DOMAIN}"]

    caps, incs, nms = [], [], []
    per_plant: dict[str, dict[str, int]] = {}
    risk_of = {"low": (2, 2), "medium": (3, 3), "high": (4, 4)}
    for idx, (sno, tag, ctype, l1, area_name, sev, desc, voice, conv) in enumerate(REPORTS):
        code = store_code(sno)
        pid = plants[code]
        reporter = users[f"floor.s{sno:03d}.{tag}@{EMAIL_DOMAIN}"]
        # Spread over ~11 weeks; the last few inside the Daily Brief's 24h / 7d windows.
        ago = max(0.1, 76 - idx * 2.75 + RNG.uniform(-1, 1)) if idx < 24 else [0.2, 0.6, 2.5, 4.0][idx - 24]
        at = now() - timedelta(days=ago)
        n = per_plant.setdefault(code, {"FLD": 0, "INC": 0, "NM": 0})
        n["FLD"] += 1
        tid, labels, icon = tax[l1]
        lik, cons = risk_of[sev]
        score = lik * cons
        level = "CRITICAL" if score >= 17 else "HIGH" if score >= 10 else "MODERATE" if score >= 5 else "LOW"
        cap_id = new_id()
        converted = None
        if conv:
            eid = new_id()
            if conv[0] == "NearMiss":
                n["NM"] += 1
                number = f"NM-{at.year}-{code}-{n['NM']:04d}"
                _, psev, status = conv
                nms.append((eid, number, reporter, _naive(at), pid, area.get((pid, area_name)), f"{area_name}",
                            desc, psev, status, "EMPLOYEE", level, lik, cons, score,
                            _naive(at + timedelta(days=12)) if status == "CLOSED" else None,
                            manager.get(sno) if status == "CLOSED" else None,
                            "Hazard removed and briefed at morning huddle." if status == "CLOSED" else None,
                            manager.get(sno), _naive(at + timedelta(days=14)), _naive(at)))
                converted = ("NearMiss", eid)
            else:
                n["INC"] += 1
                number = f"INC-{at.year}-{code}-{n['INC']:04d}"
                _, itype, isev, status, lost = conv
                incs.append((eid, number, _naive(at), itype, pid, area.get((pid, area_name)), area_name, reporter, desc,
                             isev, status, lost or None, _naive(at), _naive(at + timedelta(minutes=35)), "Store associate",
                             "First aid given; area cordoned and store manager informed.",
                             _naive(at + timedelta(days=20)) if status == "CLOSED" else None, _naive(at)))
                converted = ("Incident", eid)
        caps.append((
            cap_id, f"FLD-{at.year}-{code}-{n['FLD']:04d}", new_id(), ctype, reporter, pid, area.get((pid, area_name)),
            round(RNG.uniform(15, 85), 1), round(RNG.uniform(15, 85), 1), tid,
            json.dumps({"l1": {"code": l1, "labels": labels, "iconKey": icon}, "l2": None}),
            RNG.random() < 0.6, round(RNG.uniform(0.62, 0.93), 2) if RNG.random() < 0.6 else None, sev, desc,
            "hi" if voice else None, "(voice note, Hindi)" if voice else None, desc if voice else None,
            "done" if voice else None,
            "converted" if converted else ("triaged" if idx % 3 == 0 else "submitted"),
            (manager.get(sno) or admin) if (converted or idx % 3 == 0) else None,
            _naive(at + timedelta(hours=3)) if (converted or idx % 3 == 0) else None,
            lik, cons, score, level,
            converted[0] if converted else None, converted[1] if converted else None,
            (manager.get(sno) or admin) if converted else None, _naive(at + timedelta(hours=4)) if converted else None,
            RNG.randint(5, 11), RNG.randint(38000, 115000), RNG.random() < 0.25, "2.4.1",
            "hi" if voice else "en", _naive(at - timedelta(seconds=RNG.randint(20, 90))), _naive(at), _naive(at),
        ))
    execute_values(cur, '''insert into "CaptureSubmission"(id, number, "clientSubmissionId", type, "reporterId", "plantId",
        "areaId", "mapPinX", "mapPinY", "categoryL1Id", "categorySnapshot", "aiSuggested", "aiConfidence",
        "severitySelfReported", description, "voiceLangCode", "transcriptOriginal", "transcriptEnglish",
        "transcriptionStatus", status, "triagedById", "triagedAt", "hiraLikelihood", "hiraSeverity", "riskScore",
        "riskLevel", "convertedEntityType", "convertedEntityId", "convertedById", "convertedAt", "tapCount",
        "durationMs", "wasOffline", "appVersion", "deviceLang", "createdAtClient", "createdAt", "updatedAt") values %s''', caps)
    execute_values(cur, '''insert into "NearMiss"(id, number, "reporterId", date, "plantId", "areaId", location, description,
        "potentialSeverity", status, "reporterType", "riskLevel", "riskLikelihood", "riskConsequence", "riskScore",
        "closedAt", "closedById", "closingRemark", "actionOwnerId", "targetDate", "createdAt", "updatedAt")
        values %s''', [r + (now().replace(tzinfo=None),) for r in nms],
                   template="(%s,%s,%s,%s,%s,%s,%s,%s,%s::\"Severity\",%s::\"NearMissStatus\",%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")

    for dno, itype, isev, status, area_name, desc, lost in DC_INCIDENTS:
        code = dc_code(dno)
        pid = plants[code]
        at = now() - timedelta(days=RNG.randint(10, 70))
        n = per_plant.setdefault(code, {"FLD": 0, "INC": 0, "NM": 0})
        n["INC"] += 1
        incs.append((new_id(), f"INC-{at.year}-{code}-{n['INC']:04d}", _naive(at), itype, pid, area.get((pid, area_name)),
                     area_name, users[f"dc.maint.dc{dno:02d}@{EMAIL_DOMAIN}"], desc, isev, status, lost or None,
                     _naive(at), _naive(at + timedelta(minutes=20)), "DC maintenance lead",
                     "Area isolated; DC manager informed.", _naive(at + timedelta(days=15)) if status == "CLOSED" else None,
                     _naive(at)))
    execute_values(cur, '''insert into "Incident"(id, number, date, type, "plantId", "areaId", location, "reporterId",
        description, severity, status, "lostDays", "occurredAt", "reportedAt", "reporterRole", "immediateAction",
        "closedAt", "createdAt", "updatedAt") values %s''', [r + (now().replace(tzinfo=None),) for r in incs],
                   template="(%s,%s,%s,%s::\"IncidentType\",%s,%s,%s,%s,%s,%s,%s::\"IncidentStatus\",%s,%s,%s,%s,%s,%s,%s,%s)")
    print(f"field reports: {len(caps)} captures, {len(nms)} near misses, {len(incs)} incidents "
          f"({len(incs) - len(DC_INCIDENTS)} from captures, {len(DC_INCIDENTS)} at DCs)")

    # ── PTW ──
    rows = []
    seqs: dict[str, int] = {}
    for dno, ptype, scope, area_name, status, ago, hours in PERMITS:
        code = dc_code(dno)
        pid = plants[code]
        seqs[code] = seqs.get(code, 0) + 1
        start = (now() - timedelta(days=ago)).replace(hour=9, minute=0, second=0, microsecond=0)
        if status == "ACTIVE":
            start = now() - timedelta(hours=2)
        end = start + timedelta(hours=hours)
        maint = users[f"dc.maint.dc{dno:02d}@{EMAIL_DOMAIN}"]
        mgr = users[f"dc.manager.dc{dno:02d}@{EMAIL_DOMAIN}"]
        issued = status in ("ACTIVE", "CLOSED")
        closed = status == "CLOSED"
        rows.append((
            new_id(), f"PTW-{code}-{seqs[code]:05d}", ptype, pid, area.get((pid, area_name)), area_name, scope,
            _naive(start), _naive(end), maint, mgr if status != "SUBMITTED" else None, maint,
            RNG.choice(["Rackfix Engineering", "CoolAir HVAC Services", "Voltline Electricals"]),
            json.dumps(["Helmet", "Safety shoes", "Hi-vis vest"] + (["Full body harness"] if ptype == "WORK_AT_HEIGHT" else [])
                       + (["Welding shield", "Fire-retardant apron"] if ptype == "HOT_WORK" else [])
                       + (["Insulated gloves", "Arc-flash face shield"] if ptype == "ELECTRICAL_LOTO" else [])),
            ptype == "HOT_WORK", status,
            _naive(start - timedelta(hours=1)) if issued else None, _naive(start - timedelta(minutes=40)) if issued else None,
            _naive(start) if issued else None, _naive(start) if issued else None,
            _naive(end - timedelta(minutes=30)) if closed else None, _naive(end) if closed else None,
            "Work completed, area handed back clean." if closed else None,
            "COMPLETED" if closed else ("IN_PROGRESS" if status == "ACTIVE" else ("WITHDRAWN" if status == "CANCELLED" else "NOT_STARTED")),
            "WORK_COMPLETED" if closed else None, "COMPLETED" if closed else None,
            _naive(start - timedelta(days=1)) if status == "CANCELLED" else None,
            "Contractor unavailable — rescheduled." if status == "CANCELLED" else None,
            ptype == "HOT_WORK" or ptype == "WORK_AT_HEIGHT", _naive(start - timedelta(hours=3)),
        ))
    execute_values(cur, '''insert into "Permit"(id, number, type, "plantId", "areaId", location, "scopeOfWork", "validFrom",
        "validTo", "originatorId", "issuerId", "receiverId", "contractorName", "ppeChecklist", "fireWatchRequired", status,
        "issuerApprovedAt", "safetyApprovedAt", "issuedAt", "activatedAt", "workCompletedAt", "closedAt", "closingRemark",
        "executionState", "closureType", outcome, "cancelledAt", "cancellationReason", "flraRequired", "createdAt",
        "updatedAt") values %s''', [r + (now().replace(tzinfo=None),) for r in rows],
                   template="(%s,%s,%s::\"PermitType\",%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::\"PermitStatus\",%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
    print(f"PTW: {len(rows)} DC maintenance permits "
          f"({', '.join(f'{s}={sum(1 for p in PERMITS if p[4] == s)}' for s in sorted({p[4] for p in PERMITS}))})")

    # ── Manhours: two previous months only ──
    today = now().date()
    first = date(today.year, today.month, 1)
    months = []
    for back in (1, 2):
        d = first
        for _ in range(back):
            d = (d - timedelta(days=1)).replace(day=1)
        months.append((d.year, d.month))
    mh = []
    for code, pid in plants.items():
        dc = code.startswith("MR-DC")
        for y, m in months:
            emp = RNG.randint(38000, 52000) if dc else RNG.randint(5200, 14800)
            con = RNG.randint(6000, 11000) if dc else RNG.randint(400, 1800)
            mh.append((new_id(), pid, y, m, emp, con, emp + con, con, RNG.randint(90, 140) if dc else RNG.randint(28, 85),
                       users.get(f"sm.s{int(code[-3:]):03d}@{EMAIL_DOMAIN}") if not dc else users[f"dc.manager.dc{int(code[-2:]):02d}@{EMAIL_DOMAIN}"],
                       _naive(datetime(y, m, 28, 12, tzinfo=timezone.utc) + timedelta(days=5)), now().replace(tzinfo=None)))
    execute_values(cur, '''insert into "Manhours"(id, "plantId", year, month, "employeeHours", "contractorHours",
        "manhoursWorked", "contractorManhours", headcount, "submittedById", "submittedAt", "updatedAt") values %s
        on conflict ("plantId", year, month) do nothing''', mh)
    print(f"manhours: {len(mh)} rows for {months} — current month deliberately absent")

    # ── Daily Brief cards ──
    def alert(code, sev, title, body, link, key, hours_ago, source):
        pid = plants[code]
        at = _naive(now() - timedelta(hours=hours_ago))
        return (new_id(), pid, sev, title, body, source, json.dumps([]), link, f"mr:{key}", json.dumps([pid]), at, at)
    alerts = [
        alert("MR-S008", "critical", "Fire alarm panel out of service — Store 008",
              "MR-S008-FAP-01 mainboard failed; the store has no automatic detection until replacement. Fire watch rota required.",
              "/fire-safety", "fap-oos-s008", 3, "fire.asset_out_of_service"),
        alert("MR-S002", "critical", "Emergency exit blocked at checkout — Store 002",
              "Promo display stand reported blocking the exit next to checkout 6 (field report).",
              "/field-reports", "exit-blocked-s002", 5, "observation.triaged_high"),
        alert("MR-S003", "attention", "Working-at-height cluster in Stockroom — Store 003",
              "Two height-related reports in 7 days (cracked ladder step, ladder fall LTI).",
              "/near-miss", "cluster-s003-wah", 20, "observation.triaged_high"),
        alert("MR-DC01", "attention", "Racking permit expires in 8h — West DC Bhiwandi",
              "PTW-MR-DC01-00004: beam connector replacement, aisles 9–12, closes at end of shift.",
              "/ptw", "ptw-expiring-dc01", 2, "ptw.expiring"),
        alert("MR-S020", "attention", "Fire Safety Audit major finding open — Store 020",
              "FSA-MR audit raised a MAJOR non-conformance on hydrant readiness; CAPA due in 14 days.",
              "/cams/findings", "fsa-major-s020", 30, "cams.finding_open"),
        alert("MR-S013", "info", "Sprinkler clearance issue reported — Store 013",
              "Cartons stacked within 45 cm of a stockroom sprinkler head (field report with voice note).",
              "/field-reports", "sprinkler-s013", 14, "observation.submitted"),
    ]
    execute_values(cur, '''insert into "Alert"(id, "siteId", severity, title, "bodyText", "sourceEventType",
        "impactedEntities", "deepLink", "dedupeKey", "audienceSiteIds", "createdAt", "updatedAt") values %s
        on conflict do nothing''', alerts)
    print(f"daily brief: {len(alerts)} alert cards")


async def rollup() -> None:
    from sqlalchemy import select

    from app.core.db import AsyncSessionLocal
    from app.models.plant import Plant
    from app.services.scorecard.rollup import run_rollup

    async with AsyncSessionLocal() as db:
        ids = list((await db.execute(select(Plant.id).where(Plant.code.like("MR-%")))).scalars().all())
        res = await run_rollup(db, months=4, site_ids=ids)
        await db.commit()
        print(f"scorecard rollup: {res}")


def main(commit: bool) -> None:
    c = conn()
    cur = c.cursor()
    seed(cur)
    if commit:
        c.commit()
        asyncio.run(rollup())
        print("committed")
    else:
        c.rollback()
        print("dry run — rolled back (pass --commit)")


if __name__ == "__main__":
    main("--commit" in sys.argv)
