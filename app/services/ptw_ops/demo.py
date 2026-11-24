"""The demo plant for the client walkthrough — "Meridian North Works · West DC
Bhiwandi" — as pure data, relative to `now`.

One source of truth for two consumers:
  • scripts/seed_ptw_control_center.py writes it to the database
  • tests/test_ptw_ops.py builds a Snapshot from it and asserts the payload
    numbers the brief specifies (12 active, 4 expired-open with 2 today, …)

Times are offsets from `now`; "today" items are placed between local midnight
and `now` proportionally, so the seed is valid whatever hour it is run.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.services.ptw_ops.shifts import local_day

PLANT_CODE = "MNW-BHW"
PLANT_NAME = "Meridian North Works · West DC Bhiwandi"
PLANT_LOCATION = "Meridian Retail West Distribution Center, Bhiwandi"
LAYOUT_W_M, LAYOUT_H_M = 80.0, 14.0   # dock ↔ pump house centroids ≈ 8 m; tank farm ≈ 16 m


def rect(x: float, y: float, w: float, h: float) -> list[dict[str, float]]:
    return [{"x": x, "y": y}, {"x": x + w, "y": y}, {"x": x + w, "y": y + h}, {"x": x, "y": y + h}]


@dataclass
class DemoZone:
    key: str
    name: str
    polygon: list[dict[str, float]]
    owner: str
    restricted: str
    instructions: list[str]
    ppe: list[str]
    assembly: str
    extinguishers: str
    type_rules: list[dict[str, str]]


def _r(t: str, state: str, cond: str | None = None) -> dict[str, str]:
    return {"permitType": t, "state": state, "conditionText": cond or ""}


ZONES: list[DemoZone] = [
    DemoZone("dock", "Loading Dock", rect(3, 5, 31, 36), "Logistics Lead",
             "Truck movement 06:00–10:00 and 18:00–20:00; no lifting over dock lanes",
             ["Barricade an 11 m radius around hot work", "Lock out dock levellers before working underneath",
              "Forklift marshal required for the whole permit window"],
             ["Safety shoes", "Hi-vis vest", "Helmet", "FR coverall (hot work)"], "AP-2 Main Gate", "Bay 3, Bay 7",
             [_r("HOT_WORK", "conditional", "Fire watch for every job"), _r("LIFTING", "conditional", "Outside truck hours"),
              _r("WORK_AT_HEIGHT", "allowed")]),
    DemoZone("rack", "Racking Aisles", rect(37, 5, 36, 36), "Warehouse Manager",
             "No work above 3rd beam level during picking hours 08:00–20:00",
             ["Close the aisle with chain barriers and signage", "Inspect rack uprights before and after lifting",
              "MEWP only; no ladders above 2 m"],
             ["Safety shoes", "Hi-vis vest", "Helmet", "Harness (height)"], "AP-1 North Yard", "Aisle ends A1–A12",
             [_r("LIFTING", "conditional", "Spotter required"), _r("WORK_AT_HEIGHT", "allowed"),
              _r("HOT_WORK", "blocked", "Not permitted — stored combustibles")]),
    DemoZone("cold", "Cold Storage", rect(76, 5, 21, 36), "Cold Chain Supervisor",
             "Max 45 min continuous exposure below −18 °C",
             ["Buddy system mandatory inside chambers", "Check the door release from inside before entry",
              "Roof access via east ladder cage only"],
             ["Thermal suit", "Insulated gloves", "Safety shoes", "Helmet"], "AP-1 North Yard", "Ante-room",
             [_r("WORK_AT_HEIGHT", "allowed"), _r("ELECTRICAL_LOTO", "conditional", "LOTO on chamber compressors"),
              _r("HOT_WORK", "blocked", "Not permitted — PIR panels")]),
    DemoZone("pump", "Pump House", rect(3, 48, 19, 46), "Utilities Engineer",
             "No entry during diesel transfer 21:00–21:30",
             ["Ventilate 15 min and take a gas reading before entry", "Rescue tripod rigged at the sump manhole",
              "Isolate pumps P-1/P-2 via LOTO before work"],
             ["SCBA on standby", "Harness", "Safety shoes", "Personal gas monitor"], "AP-3 South Gate",
             "Entrance, foam unit",
             [_r("CONFINED_SPACE", "conditional", "Attendant + rescue plan"),
              _r("ELECTRICAL_LOTO", "conditional", "LOTO on P-1/P-2"),
              _r("HOT_WORK", "conditional", "Gas-free certificate first")]),
    DemoZone("tank", "Tank Farm", rect(25, 48, 23, 46), "Utilities Engineer",
             "No hot work during tanker unloading",
             ["Earthing check before any tanker activity", "Spark-proof tools only inside the bund",
              "Excavations over 1.2 m need shoring"],
             ["FR coverall", "Safety shoes", "Helmet", "Personal gas monitor"], "AP-3 South Gate",
             "Bund corners (DCP 9 kg)",
             [_r("EXCAVATION", "conditional", "Underground services check"),
              _r("HOT_WORK", "conditional", "Gas test every hour"), _r("CONFINED_SPACE", "conditional", "Attendant")]),
    DemoZone("elec", "Electrical Substation", rect(51, 48, 22, 46), "Electrical Engineer",
             "Switching only by authorised persons; no work during HT changeover",
             ["Verify zero energy with an approved tester before touching", "Arc-flash boundary marked at every panel",
              "Keep the substation door closed — clean agent system zone"],
             ["Arc-flash suit (cat 2)", "Insulated gloves", "Safety shoes", "Face shield"], "AP-2 Main Gate",
             "CO₂ at door, clean agent system",
             [_r("ELECTRICAL_LOTO", "conditional", "Authorised person + LOTO"), _r("WORK_AT_HEIGHT", "allowed"),
              _r("HOT_WORK", "blocked", "Not permitted near live gear")]),
    DemoZone("shop", "Maintenance Workshop", rect(76, 48, 21, 46), "Maintenance Planner",
             "Grinding and welding only in the screened hot-work bay",
             ["Clear combustibles 11 m around the hot-work bay", "Machine guards in place before start-up",
              "Hoist inspection tag must be current"],
             ["Safety shoes", "Safety glasses", "Welding shield (hot work)", "Hearing protection"],
             "AP-1 North Yard", "Hot-work bay, workshop exit",
             [_r("HOT_WORK", "allowed"), _r("LIFTING", "conditional", "Tagged hoist only"),
              _r("ELECTRICAL_LOTO", "allowed")]),
]

CONTRACTORS = [
    {"key": "C1", "name": "Shree Ganesh Fabricators", "score": 88},
    {"key": "C2", "name": "Konkan Industrial Services", "score": 74},
    {"key": "C3", "name": "Apex Electricals", "score": 66},
    {"key": "C4", "name": "Vertex Access Solutions", "score": 92},
]


@dataclass
class DemoPermit:
    key: str
    type: str
    zone: str
    status: str
    validFrom: datetime
    validTo: datetime
    crew: int = 0
    contractor: str | None = None
    scope: str = ""
    createdAt: datetime | None = None
    activatedAt: datetime | None = None
    closedAt: datetime | None = None
    workCompletedAt: datetime | None = None
    siteVerifiedAt: datetime | None = None
    suspendedAt: datetime | None = None
    suspendedReason: str | None = None
    expiredAt: datetime | None = None
    closureType: str | None = None
    workflowStep: str | None = None      # current workflow step name (SUBMITTED permits)
    gas: tuple[datetime, datetime, bool] | None = None   # (recordedAt, refreshDueBy, exceedance)
    isolations: tuple[int, int] = (0, 0)  # (total, unverified)
    controls: list[str] = field(default_factory=list)
    fireWatch: bool = False
    history: bool = False


@dataclass
class DemoImpairment:
    code: str
    systemType: str
    systemName: str
    zone: str
    reason: str
    startedAt: datetime
    plannedRestoreAt: datetime
    precautions: list[str]


@dataclass
class Scenario:
    now: datetime
    permits: list[DemoPermit]
    impairments: list[DemoImpairment]


def build(now: datetime, *, history_seed: int = 7) -> Scenario:
    m = lambda n: timedelta(minutes=n)  # noqa: E731
    day0, _ = local_day(now)
    span = now - day0

    def today(frac: float) -> datetime:
        """A moment `frac` of the way from local midnight to now."""
        return day0 + span * frac

    P: list[DemoPermit] = []

    def active(key, t, zone, start, end, crew, contractor, **kw):  # noqa: ANN001, ANN202
        vf = now - m(start)
        P.append(DemoPermit(key, t, zone, "ACTIVE", vf, now + m(end), crew=crew, contractor=contractor,
                            createdAt=vf - m(90), activatedAt=vf + m(10), **kw))

    # ── 12 active ──
    active("A1", "HOT_WORK", "dock", 150, 210, 6, "C1", fireWatch=True, scope="Weld repair on dock leveller 4 hinge")
    active("A2", "WORK_AT_HEIGHT", "dock", 120, 240, 5, "C1", scope="Replace canopy lights over doors 1–6")
    active("A3", "CONFINED_SPACE", "pump", 90, 150, 4, "C2", gas=(now - m(20), now + m(40), False),
           controls=["continuous_gas_monitor", "attendant"], scope="Desilt fire-water sump")
    active("A4", "ELECTRICAL_LOTO", "elec", 200, 120, 6, None, isolations=(3, 0), controls=["hourly_patrol"],
           scope="PCC-2 breaker retrofit")
    active("A5", "ELECTRICAL_LOTO", "elec", 60, 300, 5, "C3", isolations=(2, 1),
           controls=["hourly_patrol", "no_unattended_work"], scope="MCC-4 cable termination")
    active("A6", "ELECTRICAL_LOTO", "elec", 30, 90, 4, None, controls=["hourly_patrol", "no_unattended_work"],
           scope="Transformer T2 oil sampling")
    active("A7", "WORK_AT_HEIGHT", "rack", 240, 60, 10, "C4", scope="Sprinkler head inspection, aisles 7–9")
    active("A8", "WORK_AT_HEIGHT", "cold", 180, 200, 6, "C4", scope="Evaporator fan replacement, chamber 2")
    active("A9", "HOT_WORK", "shop", 100, 80, 8, "C3", scope="Fabricate pallet-racking guard rails")
    active("A10", "HOT_WORK", "shop", 45, 330, 8, "C3", scope="Grind and weld conveyor frame")
    active("A11", "LIFTING", "shop", 80, 160, 12, "C2", scope="Lift gearbox onto test bench (2.5 t)")
    active("A12", "CONFINED_SPACE", "tank", 160, 100, 6, "C2", gas=(now - m(150), now - m(30), False),
           scope="Internal inspection of diesel day tank DT-2")

    # ── 1 suspended (gas out of limit) ──
    vf = now - m(130)
    P.append(DemoPermit("S1", "CONFINED_SPACE", "tank", "SUSPENDED", vf, now + m(110), crew=7, contractor="C1",
                        createdAt=vf - m(60), activatedAt=vf + m(10), suspendedAt=now - m(25),
                        suspendedReason="Gas reading out of limit (LEL 12%)",
                        gas=(now - m(26), now + m(34), True), scope="Clean foam tank FT-1"))

    # ── 4 expired, not closed (2 today, 2 older) ──
    for key, t, zone, vfrom, vto in [
        ("E1", "LIFTING", "rack", today(0.2), max(day0 + m(1), now - min(m(90), span * 0.5))),
        ("E2", "EXCAVATION", "tank", today(0.3), max(day0 + m(2), now - min(m(40), span * 0.3))),
        ("E3", "GENERAL_COLD", "shop", now - timedelta(days=2, hours=8), now - timedelta(days=2)),
        ("E4", "WORK_AT_HEIGHT", "cold", now - timedelta(days=3, hours=6), now - timedelta(days=3)),
    ]:
        P.append(DemoPermit(key, t, zone, "EXPIRED", vfrom, vto, createdAt=vfrom - m(60), activatedAt=vfrom + m(15),
                            expiredAt=vto, scope=f"{t.replace('_', ' ').title()} job"))

    # ── 6 waiting at Issuer Review, all past the 2 h SLA ──
    for i, (t, zone) in enumerate([("GENERAL_COLD", "rack"), ("EXCAVATION", "tank"), ("WORK_AT_HEIGHT", "cold"),
                                   ("LIFTING", "shop"), ("ELECTRICAL_LOTO", "elec"), ("CONFINED_SPACE", "pump")]):
        created = now - m(180 + i * 35)
        P.append(DemoPermit(f"R{i + 1}", t, zone, "SUBMITTED", now + m(30 + i * 30), now + m(30 + i * 30 + 480),
                            createdAt=created, workflowStep="Issuer Review", scope=f"Planned {t.replace('_', ' ').lower()}"))

    # ── 4 closure pending ──
    for key, t, zone, status in [("C1", "WORK_AT_HEIGHT", "rack", "WORK_COMPLETED"),
                                 ("C2", "WORK_AT_HEIGHT", "cold", "WORK_COMPLETED"),
                                 ("C3", "HOT_WORK", "shop", "HANDBACK_INSPECTION"),
                                 ("C4", "ELECTRICAL_LOTO", "elec", "HANDBACK_INSPECTION")]:
        vfrom = today(0.25)
        P.append(DemoPermit(f"P{key}", t, zone, status, vfrom, now + m(60), createdAt=vfrom - m(60),
                            activatedAt=vfrom + m(10), workCompletedAt=now - m(70), closureType="WORK_COMPLETED",
                            siteVerifiedAt=(now - m(20)) if status == "HANDBACK_INSPECTION" else None,
                            scope="Job finished, awaiting close-out"))

    # ── 11 closed today ──
    zones_cycle = ["dock", "rack", "cold", "pump", "tank", "elec", "shop"]
    types_cycle = ["WORK_AT_HEIGHT", "GENERAL_COLD", "LIFTING", "ELECTRICAL_LOTO", "HOT_WORK", "EXCAVATION"]
    for i in range(11):
        vfrom = today(0.02 + i * 0.05)
        vto = today(0.1 + i * 0.07)
        closed = min(now - m(1), vto + (m(20) if i % 4 else m(95)))   # 3 of 11 closed late
        t = types_cycle[i % len(types_cycle)]
        if t == "HOT_WORK" and zones_cycle[i % 7] in ("rack", "cold", "elec", "dock"):
            t = "GENERAL_COLD"
        P.append(DemoPermit(f"D{i + 1}", t, zones_cycle[i % 7], "CLOSED", vfrom, vto, createdAt=vfrom - m(45),
                            activatedAt=vfrom + m(5), workCompletedAt=closed - m(15), siteVerifiedAt=closed - m(5),
                            closedAt=closed, closureType="WORK_COMPLETED", scope="Routine job, closed today"))

    # ── 2 starting within 2 h ──
    P.append(DemoPermit("N1", "HOT_WORK", "shop", "ISSUED", now + m(75), now + m(75 + 480), createdAt=now - m(200),
                        scope="Weld base plates for new conveyor"))
    P.append(DemoPermit("N2", "GENERAL_COLD", "dock", "APPROVED", now + m(50), now + m(50 + 240), createdAt=now - m(240),
                        scope="Re-mark dock lane floor lines"))

    # ── 12 weeks of history: 312 closed (45 of them expired unexecuted) ──
    rnd = random.Random(history_seed)
    for i in range(312):
        created = now - timedelta(days=2 + (i * 81) / 312, hours=rnd.random() * 6)
        vfrom = created + m(60)
        vto = vfrom + m(rnd.choice((240, 360, 480)))
        t = rnd.choice(types_cycle + ["CONFINED_SPACE", "WORK_AT_HEIGHT"])
        zone = rnd.choice(zones_cycle)
        expired = i % 7 == 3 and i < 7 * 45
        # An expired permit nobody worked under is withdrawn after the fact:
        # the live flow moves it to CANCELLED / UNEXECUTED_CLOSURE.
        P.append(DemoPermit(
            f"H{i + 1}", t, zone, "CANCELLED" if expired else "CLOSED", vfrom, vto, createdAt=created,
            activatedAt=None if expired else vfrom + m(10),
            closedAt=None if expired else vto + m(rnd.choice((10, 30, 70))),
            workCompletedAt=None if expired else vto - m(20),
            expiredAt=vto if expired else None,
            closureType="UNEXECUTED_CLOSURE" if expired else "WORK_COMPLETED",
            scope="Historical permit", history=True,
        ))

    imps = [
        DemoImpairment("SZ-03", "sprinkler", "Sprinkler zone SZ-03", "dock",
                       "Riser valve replacement on zone SZ-03", now - m(180), now + m(240),
                       ["Fire watch on every hot work job", "Portable extinguishers at bays 3 and 7",
                        "Hourly patrol by the fire team"]),
        DemoImpairment("GD-07", "gas_detection", "Fixed gas detector GD-07", "pump",
                       "Sensor head replacement — detector bypassed", now - m(120), now + m(35),
                       ["Portable 4-gas monitor at the manhole", "Attendant at entry"]),
        DemoImpairment("FA-L2", "fire_alarm", "Fire alarm loop L2", "elec",
                       "Loop L2 earth fault — panel isolated", now - m(360), now - m(50),
                       ["Hourly fire patrol", "No unattended work"]),
    ]
    return Scenario(now=now, permits=P, impairments=imps)


def snapshot(sc: Scenario):  # noqa: ANN201 — used by tests
    """The Snapshot the loaders would build from the seeded rows."""
    from app.services.ptw_cc import DEFAULT_SHIFTS
    from app.services.ptw_ops import defaults
    from app.services.ptw_ops.types import ImpairmentView, LayoutView, PermitView, Snapshot, ZoneView

    plant = "demo-plant"
    zones = [ZoneView(id=f"z-{z.key}", name=z.name, polygon=z.polygon, sortOrder=i, ownerUserId=f"u-{z.key}")
             for i, z in enumerate(ZONES)]
    permits = []
    for i, d in enumerate(sc.permits):
        controls = set(d.controls) | ({"fire_watch"} if d.fireWatch else set())
        p = PermitView(
            id=f"p-{d.key}", number=f"PTW-{PLANT_CODE}-{i + 1:05d}", type=d.type, status=d.status, plantId=plant,
            areaId=f"z-{d.zone}", validFrom=d.validFrom, validTo=d.validTo, createdAt=d.createdAt,
            workflowStepName=d.workflowStep, issuerId="u-issuer", receiverId="u-receiver",
            contractorCompanyId=f"co-{d.contractor}" if d.contractor else None, closureType=d.closureType,
            closedAt=d.closedAt, activatedAt=d.activatedAt, workCompletedAt=d.workCompletedAt,
            suspendedReason=d.suspendedReason, crewCount=d.crew, crewCheckedIn=d.crew,
            gasRequired=d.gas is not None or d.type == "CONFINED_SPACE",
            lastGasAt=d.gas[0] if d.gas else None, gasRefreshDueBy=d.gas[1] if d.gas else None,
            lastGasExceedance=bool(d.gas and d.gas[2]), isolationsTotal=d.isolations[0],
            isolationsUnverified=d.isolations[1], satisfiedControls=controls,
            stepSlaMin=120 if d.workflowStep == "Issuer Review" else None,
        )
        p.stepEnteredAt = d.createdAt if d.status == "SUBMITTED" else (
            d.activatedAt if d.status == "ACTIVE" else d.suspendedAt or d.workCompletedAt or d.createdAt)
        p.lastActivityAt = p.lastGasAt or p.activatedAt
        permits.append(p)
    imps = [ImpairmentView(id=f"i-{x.code}", code=x.code, plantId=plant, areaId=f"z-{x.zone}", systemType=x.systemType,
                           systemName=x.systemName, reason=x.reason, status="active", startedAt=x.startedAt,
                           plannedRestoreAt=x.plannedRestoreAt, precautions=x.precautions,
                           approvedById="u-fire", createdById="u-safety")
            for x in sc.impairments]
    return Snapshot(now=sc.now, plantId=plant, permits=permits, impairments=imps,
                    impairmentRules=defaults.impairment_rule_views(), simopsRules=defaults.simops_rule_views(),
                    zones=zones, layout=LayoutView(widthM=LAYOUT_W_M, heightM=LAYOUT_H_M), shifts=DEFAULT_SHIFTS,
                    areaOwners={z.id: z.ownerUserId for z in zones})
