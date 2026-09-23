"""Meridian Retail — DC Lockout/Tagout procedures and executions.

Four published procedures across the three DCs and four lockout records, one of them
linked to the closed ELECTRICAL_LOTO MCC permit seeded by ops.py. The
procedure version snapshot mirrors `body_snapshot()` in app/services/loto.py
exactly — the execution console reads the frozen copy, never the live rows.
Codes: LOTO-EQ-NNNN per site; executions continue the global LOTO-EX sequence.

    python -m scripts.meridian_retail.loto            # dry run
    python -m scripts.meridian_retail.loto --commit
"""

from __future__ import annotations

import json
import secrets
import sys
from datetime import timedelta, timezone

from scripts.meridian_retail.common import EMAIL_DOMAIN, conn, new_id, now, plant_map, user_map


def _naive(d):
    return d.astimezone(timezone.utc).replace(tzinfo=None)


# (dc, area, equipment tag, equipment name, title, energy[(type, magnitude, where)],
#  points[(source idx, location, method, lock, verify)], hardware[(item, desc, qty)], steps[(text, photo, signoff)])
PROCEDURES = [
    (1, "Loading Dock", "DL-04", "Dock leveller 4 — hydraulic power pack", "Dock leveller hydraulic power pack isolation",
     [("electrical", "415 V 3-phase", "Leveller control panel feeder, DB-DOCK-2"), ("hydraulic", "180 bar", "Power pack accumulator"),
      ("gravity", "Deck mass ~1.2 t", "Raised leveller deck")],
     [(0, "DB-DOCK-2 feeder MCB 7", "breaker", "lock", "Try start at leveller push-button"),
      (1, "Accumulator bleed valve", "valve", "lock", "Pressure gauge reads 0 bar"),
      (2, "Leveller maintenance strut", "blocking", "tag", "Strut engaged and pinned")],
     [("lock", "Personal safety padlock", 2), ("tag", "Danger — Do Not Operate tag", 2), ("hasp", "Group hasp", 1)],
     [("Confirm feeder isolated — try start fails", False, True), ("Bleed accumulator to 0 bar", True, True),
      ("Engage and pin maintenance strut before entering pit", True, True)]),
    (1, "Electrical Room", "MCC-01", "MCC panel 1 — racking area feeders", "MCC panel feeder isolation for busbar work",
     [("electrical", "415 V / 800 A", "MCC-01 incomer ACB")],
     [(0, "MCC-01 incomer ACB racked out", "disconnect", "lock", "Test for dead at busbar with approved tester")],
     [("lock", "Personal safety padlock", 2), ("tag", "Danger tag", 1)],
     [("Rack out ACB and apply lock", True, True), ("Prove tester on known live source", False, True),
      ("Test for dead on all three phases and neutral", True, True)]),
    (2, "Racking Aisles", "CNV-S1", "Sortation conveyor S1 drive", "Sortation conveyor drive isolation",
     [("electrical", "415 V", "Conveyor VFD panel CP-S1"), ("mechanical", "Belt tension / stored motion", "Drive pulley")],
     [(0, "CP-S1 isolator", "disconnect", "lock", "E-stop and try start at HMI"),
      (1, "Drive pulley chain block", "chain", "tag", "Belt cannot be moved by hand")],
     [("lock", "Personal safety padlock", 2), ("chain", "Chain and block", 1)],
     [("Isolate CP-S1 and lock", False, True), ("Chain-block drive pulley", True, True)]),
    (3, "HVAC Plant Room", "CH-02", "Chiller 2 — cold-chain zone compressor", "Chiller compressor isolation for condenser service",
     [("electrical", "415 V", "Chiller starter panel"), ("thermal", "Hot discharge line ~85 °C", "Compressor discharge"),
      ("chemical", "R-134a refrigerant", "Service valves")],
     [(0, "Chiller starter isolator", "disconnect", "lock", "Try start from BMS"),
      (2, "Suction and discharge service valves", "valve", "tag", "Gauge manifold shows isolation holding")],
     [("lock", "Personal safety padlock", 2), ("tag", "Danger tag", 2)],
     [("Stop chiller from BMS and isolate starter", False, True), ("Allow 30 min cool-down, check discharge temp", True, True),
      ("Close service valves and verify with manifold", True, True)]),
]


def seed(cur) -> None:
    plants = plant_map(cur)
    users = user_map(cur)
    dc_ids = [plants[f"MR-DC{d:02d}"] for d in (1, 2, 3)]
    cur.execute('select count(*) from "LotoProcedure" where "siteId" = any(%s)', (dc_ids,))
    if cur.fetchone()[0]:
        print("loto: Retail procedures already present — skipping")
        return
    cur.execute('select id, name, code from "Plant" where id = any(%s)', (dc_ids,))
    site = {pid: (name, code) for pid, name, code in cur.fetchall()}
    cur.execute('select id, name, role from "User" where email like %s', (f"%@{EMAIL_DOMAIN}",))
    uinfo = {uid: (name, role) for uid, name, role in cur.fetchall()}
    admin = users[f"store-ops.admin@{EMAIL_DOMAIN}"]
    created = []
    per_site: dict[str, int] = {}
    for dc, area, tag, eq_name, title, energy, points, hardware, steps in PROCEDURES:
        pid = plants[f"MR-DC{dc:02d}"]
        maint = users[f"dc.maint.dc{dc:02d}@{EMAIL_DOMAIN}"]
        per_site[pid] = per_site.get(pid, 0) + 1
        code = f"LOTO-EQ-{per_site[pid]:04d}"
        proc_id = new_id()
        cur.execute('select id from "Area" where "plantId"=%s and name=%s', (pid, area))
        area_row = cur.fetchone()
        created_at = now() - timedelta(days=120)
        cur.execute('''insert into "LotoProcedure"(id, "procedureCode", "siteId", "siteName", area, "areaId", "equipmentName",
            "equipmentTag", title, description, status, version, "qrCodeToken", "reviewFrequencyMonths", "lastReviewedAt",
            "lastReviewedById", "nextReviewDueAt", "createdById", "updatedById", "createdAt", "updatedAt")
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',1,%s,12,%s,%s,%s,%s,%s,%s,%s)''',
                    (proc_id, code, pid, site[pid][0], area, area_row[0] if area_row else None, eq_name, tag, title,
                     f"Isolation of all energy sources on {eq_name} before maintenance.", secrets.token_urlsafe(16),
                     _naive(created_at), admin, _naive(created_at + timedelta(days=365)), maint, maint,
                     _naive(created_at), _naive(created_at)))
        es_rows, ip_rows, hw_rows, vs_rows = [], [], [], []
        for i, (etype, mag, where) in enumerate(energy, 1):
            es_rows.append({"id": new_id(), "sequence": i, "energyType": etype, "magnitude": mag, "locationDescription": where})
        for i, (src, loc, method, lock, verify) in enumerate(points, 1):
            ip_rows.append({"id": new_id(), "sequence": i, "energySourceId": es_rows[src]["id"], "location": loc,
                            "isolationMethod": method, "lockType": lock, "verificationMethod": verify, "notes": None})
        for item, desc, qty in hardware:
            hw_rows.append({"id": new_id(), "itemType": item, "description": desc, "quantityRequired": qty})
        for i, (text, photo, signoff) in enumerate(steps, 1):
            vs_rows.append({"id": new_id(), "sequence": i, "stepText": text, "requiresPhoto": photo, "requiresSignoff": signoff})
        for r in es_rows:
            cur.execute('insert into "LotoEnergySource"(id, "procedureId", sequence, "energyType", magnitude, "locationDescription") '
                        'values (%s,%s,%s,%s,%s,%s)', (r["id"], proc_id, r["sequence"], r["energyType"], r["magnitude"], r["locationDescription"]))
        for r in ip_rows:
            cur.execute('insert into "LotoIsolationPoint"(id, "procedureId", sequence, "energySourceId", location, "isolationMethod", '
                        '"lockType", "verificationMethod", notes) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (r["id"], proc_id, r["sequence"], r["energySourceId"], r["location"], r["isolationMethod"],
                         r["lockType"], r["verificationMethod"], r["notes"]))
        for r in hw_rows:
            cur.execute('insert into "LotoHardwareRequirement"(id, "procedureId", "itemType", description, "quantityRequired") '
                        'values (%s,%s,%s,%s,%s)', (r["id"], proc_id, r["itemType"], r["description"], r["quantityRequired"]))
        for r in vs_rows:
            cur.execute('insert into "LotoVerificationStep"(id, "procedureId", sequence, "stepText", "requiresPhoto", "requiresSignoff") '
                        'values (%s,%s,%s,%s,%s,%s)', (r["id"], proc_id, r["sequence"], r["stepText"], r["requiresPhoto"], r["requiresSignoff"]))
        snapshot = {
            "header": {"procedureCode": code, "title": title,
                       "description": f"Isolation of all energy sources on {eq_name} before maintenance.",
                       "equipmentId": None, "equipmentName": eq_name, "equipmentTag": tag, "siteId": pid,
                       "siteName": site[pid][0], "area": area, "version": 1},
            "energySources": es_rows, "isolationPoints": ip_rows, "hardware": hw_rows, "verificationSteps": vs_rows,
        }
        ver_id = new_id()
        cur.execute('''insert into "LotoProcedureVersion"(id, "procedureId", version, "snapshotJson", "isPublished", "publishedAt",
            "publishedById", "changeSummary", "changeType", "createdById", "createdAt") values (%s,%s,1,%s,true,%s,%s,%s,%s,%s,%s)''',
                    (ver_id, proc_id, json.dumps(snapshot), _naive(created_at), admin, "Initial publication.", "MATERIAL",
                     maint, _naive(created_at)))
        cur.execute('update "LotoProcedure" set "publishedVersionId"=%s where id=%s', (ver_id, proc_id))
        created.append(dict(dc=dc, pid=pid, proc=proc_id, ver=ver_id, snapshot=snapshot, points=ip_rows, steps=vs_rows,
                            maint=maint, mgr=users[f"dc.manager.dc{dc:02d}@{EMAIL_DOMAIN}"]))

    # Executions continue the global sequence.
    cur.execute('select coalesce(max(substring(number from %s)::int), 0) from "LotoExecution" where number like %s',
                (r"LOTO-EX-\d{4}-(\d+)$", f"LOTO-EX-{now().year}-%"))
    seq = cur.fetchone()[0]
    cur.execute('select id, number, "plantId" from "Permit" where "plantId" = any(%s) and type=%s and status=%s order by "validFrom"',
                (dc_ids, "ELECTRICAL_LOTO", "CLOSED"))
    closed_permits = {pid: (pmid, num) for pmid, num, pid in cur.fetchall()}
    plan = [(created[1], "closed", 21, True), (created[0], "closed", 17, False), (created[3], "work_in_progress", 0.1, False),
            (created[2], "aborted", 6, False)]
    for c, status, ago, link in plan:
        seq += 1
        ex_id = new_id()
        permit = closed_permits.get(c["pid"]) if link else None
        start = now() - timedelta(days=ago)
        done = status == "closed"
        cur.execute('''insert into "LotoExecution"(id, number, "procedureId", "procedureVersionId", "procedureVersionSnapshot",
            "snapshotVersion", "ptwId", "ptwNumber", "siteId", "siteName", "initiatedById", "initiatedByName", "initiatedAt",
            "isGroupLockout", status, "workStartedAt", "locksRemovedAt", "closedById", "closedAt", "closureNotes",
            "abortedById", "abortedAt", "abortReason", "createdAt", "updatedAt")
            values (%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (ex_id, f"LOTO-EX-{now().year}-{seq:04d}", c["proc"], c["ver"], json.dumps(c["snapshot"]),
                     permit[0] if permit else None, permit[1] if permit else None, c["pid"], site[c["pid"]][0],
                     c["maint"], uinfo[c["maint"]][0], _naive(start), status,
                     _naive(start) if status in ("work_in_progress", "closed") else None,
                     _naive(start + timedelta(hours=5)) if done else None, c["mgr"] if done else None,
                     _naive(start + timedelta(hours=6)) if done else None,
                     "All locks accounted for; equipment returned to service." if done else None,
                     c["mgr"] if status == "aborted" else None, _naive(start + timedelta(hours=1)) if status == "aborted" else None,
                     "Spare VFD not delivered — work rescheduled, locks removed." if status == "aborted" else None,
                     _naive(start), _naive(start)))
        for uid, role, tagno in ((c["maint"], "primary_authorized", f"LK-MR{seq:03d}1"), (c["mgr"], "secondary", f"LK-MR{seq:03d}2")):
            cur.execute('''insert into "LotoExecutionParticipant"(id, "executionId", "userId", "userName", "userRole",
                "participantRole", "assignedIsolationPointIds", "lockTagNumber", "lockAppliedAt", "lockAppliedConfirmed",
                "lockRemovedAt", "lockRemovedConfirmed") values (%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s)''',
                        (new_id(), ex_id, uid, uinfo[uid][0], uinfo[uid][1], role, [p["id"] for p in c["points"]], tagno,
                         _naive(start), _naive(start + timedelta(hours=5)) if done or status == "aborted" else None,
                         done or status == "aborted"))
        if status in ("closed", "work_in_progress"):
            for s in c["steps"]:
                cur.execute('''insert into "LotoVerificationRecord"(id, "executionId", "stepId", sequence, "stepText",
                    "completedById", "completedByName", "completedAt", signoff) values (%s,%s,%s,%s,%s,%s,%s,%s,true)''',
                            (new_id(), ex_id, s["id"], s["sequence"], s["stepText"], c["maint"], uinfo[c["maint"]][0], _naive(start)))
        if permit:
            cur.execute('update "Permit" set "lotoExecutionId"=%s where id=%s', (ex_id, permit[0]))
    print(f"loto: {len(created)} procedures, {len(plan)} lockout records (LOTO-EX up to {seq:04d})")


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
