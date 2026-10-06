"""Meridian Retail — plant equipment master for the stores and DCs.

The PTW wizard's Step 5 (PPE & Equipment) picks "Tools / Equipment Used by Crew"
and "Subject Equipment (being worked on)" from the Equipment master of the
permit's site. No Retail site had a single Equipment row, so both "+ Add Tool"
and "+ Add Subject" stayed disabled on every Retail permit.

Each DC gets its MHE fleet, dock, conveyor, cold-chain and utility assets (the
LOTO procedures in loto.py name the same physical tags — DL-04, MCC-01, CNV-S1,
CH-02); each store gets the assets a facilities permit is raised against, sized
by store format. Inspection dates are current so nothing reads as overdue.

Idempotent: skips any site that already has Equipment rows.
Codes: MR-<site>-<tag>, unique per site.

    python -m scripts.meridian_retail.equipment            # dry run
    python -m scripts.meridian_retail.equipment --commit
"""

from __future__ import annotations

import sys
from datetime import timedelta, timezone

from scripts.meridian_retail.common import DCS, STORES, conn, new_id, now, plant_map

# (tag, name, category, subCategory, area, criticality, frequency)
DC_EQUIPMENT = [
    ("FLT-01", "Counterbalance forklift FLT-01 (2.5 t, diesel)", "MOBILE_EQUIPMENT", "Forklift", "Racking Aisles", "A", "DAILY"),
    ("FLT-02", "Counterbalance forklift FLT-02 (2.5 t, electric)", "MOBILE_EQUIPMENT", "Forklift", "Racking Aisles", "A", "DAILY"),
    ("RT-01", "Reach truck RT-01 (1.6 t, 9 m lift)", "MOBILE_EQUIPMENT", "Reach Truck", "Racking Aisles", "A", "DAILY"),
    ("SP-01", "Order picker / stock picker SP-01", "MOBILE_EQUIPMENT", "Order Picker", "Racking Aisles", "B", "DAILY"),
    ("MEWP-01", "Scissor lift MEWP-01 (10 m platform)", "MOBILE_EQUIPMENT", "MEWP", "Racking Aisles", "A", "WEEKLY"),
    ("DL-04", "Dock leveller 4 — hydraulic power pack", "UTILITIES", "Dock Leveller", "Loading Dock", "B", "MONTHLY"),
    ("DS-01", "Dock shelter and sectional door bank D1–D6", "UTILITIES", "Dock Door", "Loading Dock", "C", "MONTHLY"),
    ("CNV-S1", "Sortation conveyor S1 drive", "PROCESS_EQUIPMENT", "Conveyor", "Racking Aisles", "B", "WEEKLY"),
    ("MCC-01", "MCC panel 1 — racking area feeders", "ELECTRICAL", "Motor Control Centre", "Electrical Room", "A", "MONTHLY"),
    ("DG-01", "DG set 500 kVA — standby power", "ELECTRICAL", "Diesel Generator", "Electrical Room", "A", "WEEKLY"),
    ("CH-02", "Chiller 2 — cold-chain zone compressor", "UTILITIES", "Chiller", "HVAC Plant Room", "A", "MONTHLY"),
    ("AHU-01", "Air handling unit AHU-01 — ambient zone", "UTILITIES", "AHU", "HVAC Plant Room", "B", "QUARTERLY"),
    ("BAL-01", "Cardboard baler BAL-01 (hydraulic)", "PROCESS_EQUIPMENT", "Baler", "Loading Dock", "B", "WEEKLY"),
    ("FP-01", "Fire hydrant pump FP-01 (main electric)", "FIRE_SYSTEM", "Fire Pump", "Pump Room", "A", "WEEKLY"),
    ("BC-01", "Battery charging station BC-01 (MHE)", "ELECTRICAL", "Battery Charger", "Racking Aisles", "B", "MONTHLY"),
]

STORE_EQUIPMENT = [
    ("HVAC-01", "Rooftop packaged HVAC unit 1", "UTILITIES", "Packaged HVAC", "Sales Floor", "B", "QUARTERLY"),
    ("RF-01", "Refrigeration rack — chilled display cases", "UTILITIES", "Refrigeration Rack", "Cold Room", "A", "MONTHLY"),
    ("CR-01", "Walk-in cold room CR-01 (frozen, −18 °C)", "UTILITIES", "Cold Room", "Cold Room", "A", "MONTHLY"),
    ("LT-DB", "Main LT distribution board", "ELECTRICAL", "Distribution Board", "Back Office", "A", "MONTHLY"),
    ("DG-01", "DG set — store standby power", "ELECTRICAL", "Diesel Generator", "Back Office", "A", "WEEKLY"),
    ("PT-01", "Hand pallet truck PT-01", "MOBILE_EQUIPMENT", "Pallet Truck", "Stockroom", "C", "WEEKLY"),
    ("LAD-01", "Aluminium platform ladder LAD-01 (3 m)", "MOBILE_EQUIPMENT", "Ladder", "Stockroom", "C", "MONTHLY"),
    ("SIGN-01", "Façade signage and lighting", "ELECTRICAL", "Signage", "Checkout & Entrance", "C", "QUARTERLY"),
]
# Bigger formats carry the extra assets a hypermarket has and an express does not.
FORMAT_EXTRAS = {
    "Hypermarket": [
        ("ESC-01", "Escalator ESC-01 — ground to first floor", "CRANE", "Escalator", "Sales Floor", "A", "MONTHLY"),
        ("TRV-01", "Travelator TRV-01 — trolley ramp", "CRANE", "Travelator", "Sales Floor", "A", "MONTHLY"),
        ("GL-01", "Goods lift GL-01 (2 t)", "CRANE", "Goods Lift", "Stockroom", "A", "MONTHLY"),
        ("SL-01", "Scissor lift SL-01 (8 m platform)", "MOBILE_EQUIPMENT", "MEWP", "Sales Floor", "B", "WEEKLY"),
    ],
    "Supermarket": [
        ("GL-01", "Goods lift GL-01 (1 t)", "CRANE", "Goods Lift", "Stockroom", "A", "MONTHLY"),
    ],
    "Express": [],
}


def _naive(d):
    return d.astimezone(timezone.utc).replace(tzinfo=None)


_INTERVAL_DAYS = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 30, "QUARTERLY": 90, "HALF_YEARLY": 182, "ANNUAL": 365}


def _rows_for(site_code: str, assets) -> list[tuple]:
    t = now()
    rows = []
    for tag, name, cat, sub, area, crit, freq in assets:
        interval = _INTERVAL_DAYS[freq]
        # Last inspected part-way through the interval → next due is in the future.
        last = t - timedelta(days=max(interval // 3, 0), hours=2)
        rows.append((
            new_id(), f"{site_code}-{tag}", name, cat, sub, area, crit, freq,
            _naive(last), _naive(last + timedelta(days=interval)),
            _naive(t - timedelta(days=700)),
        ))
    return rows


def seed(cur) -> None:
    plants = plant_map(cur)
    sites: list[tuple[str, list]] = []
    for i, _ in enumerate(DCS, 1):
        sites.append((f"MR-DC{i:02d}", DC_EQUIPMENT))
    for i, (_, _, _, fmt) in enumerate(STORES, 1):
        sites.append((f"MR-S{i:03d}", STORE_EQUIPMENT + FORMAT_EXTRAS.get(fmt, [])))

    created = skipped = 0
    for code, assets in sites:
        pid = plants.get(code)
        if pid is None:
            print(f"equipment: {code} not found — skipping")
            continue
        cur.execute('select count(*) from "Equipment" where "plantId" = %s', (pid,))
        if cur.fetchone()[0]:
            skipped += 1
            continue
        for (eid, ecode, name, cat, sub, area, crit, freq, last, nxt, commissioned) in _rows_for(code, assets):
            cur.execute(
                '''insert into "Equipment"(id, code, name, category, "subCategory", "plantId", location,
                       criticality, frequency, "lastInspectionDate", "nextInspectionDue", "commissioningDate",
                       active, "createdAt", "updatedAt")
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s::"InspectionFrequency", %s, %s, %s, true, now(), now())''',
                (eid, ecode, name, cat, sub, pid, area, crit, freq, last, nxt, commissioned),
            )
            created += 1
    print(f"equipment: {created} rows created; {skipped} site(s) already had equipment")


def main(commit: bool) -> None:
    c = conn()
    cur = c.cursor()
    seed(cur)
    if commit:
        c.commit()
        print("committed")
    else:
        c.rollback()
        print("dry run — rolled back (pass --commit)")


if __name__ == "__main__":
    main("--commit" in sys.argv)
