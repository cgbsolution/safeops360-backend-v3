"""Meridian Retail — Contractor Safety Management (the EPC module, relabelled).

Nine store fit-out / renovation projects, each an EPC ConstructionSite, with the
contractor companies, workers, mobilisations, site inductions, gate checks and
gate passes the site page renders. Every row carries tenantId = "RETAIL", the
EPC tenant partition (app/services/tenant_partition.py): Retail users see only
these, and nobody else sees them.

Codes use an MR- prefix so they never collide with the app's count-based
generators (SITE-/CC-/CW-/MOB-/GP-).

    python -m scripts.meridian_retail.epc            # dry run
    python -m scripts.meridian_retail.epc --commit
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta, timezone

from scripts.meridian_retail.common import EMAIL_DOMAIN, RNG, STORES, conn, new_id, now, plant_map, user_map

TENANT = "RETAIL"


def _naive(d):
    return d.astimezone(timezone.utc).replace(tzinfo=None)


# (name, code, trades, prequal status, size, score)
COMPANIES = [
    ("Shelfline Interiors Pvt Ltd", "MR-CC-01", ["carpentry", "shopfitting"], "approved", "medium", 92),
    ("CoolAir HVAC Services", "MR-CC-02", ["hvac", "refrigeration"], "approved", "small", 88),
    ("Voltline Electricals", "MR-CC-03", ["electrical"], "approved", "small", 85),
    ("Rackfix Engineering", "MR-CC-04", ["racking", "fabrication"], "conditionally_approved", "small", 74),
    ("Brightsign Signage Works", "MR-CC-05", ["signage", "work_at_height"], "approved", "micro", 81),
    ("FloorCraft Flooring", "MR-CC-06", ["flooring", "civil"], "suspended", "micro", 58),
]
# (store no., project, type, status, companies idx, days since start, weeks planned, expired induction?)
PROJECTS = [
    (5, "Full store refit — new fixtures and lighting", "Store Fit-out", "active", [0, 2, 4], 18, 6, False),
    (9, "Cold room expansion and refrigeration upgrade", "Cold-chain Upgrade", "active", [1, 2], 9, 4, False),
    (15, "Checkout zone renovation and signage", "Renovation", "active", [0, 4], 25, 5, True),
    (19, "New mezzanine stockroom racking", "Store Fit-out", "active", [3, 2], 12, 5, False),
    (22, "Flooring replacement, sales floor", "Renovation", "demobilising", [5, 0], 40, 6, False),
    (27, "Rooftop HVAC replacement", "Cold-chain Upgrade", "completed", [1, 2], 75, 5, False),
    (31, "Façade and illuminated signage", "Renovation", "active", [4, 2], 6, 3, False),
    (34, "Pharmacy corner fit-out", "Store Fit-out", "planning", [0], -10, 4, False),
    (38, "Fire-rated stockroom partition", "Renovation", "completed", [0, 3], 60, 3, False),
]
FIRST = ["Ramesh", "Suresh", "Mahesh", "Imran", "Rajesh", "Santosh", "Vinod", "Abdul", "Ravi", "Prakash", "Manoj", "Salim"]
LAST = ["Yadav", "Gupta", "Kumar", "Shaikh", "Patil", "Nayak", "Das", "Ansari", "Singh", "Rao", "Pandey", "Khan"]


def seed(cur) -> None:
    plants = plant_map(cur)
    users = user_map(cur)
    cur.execute('select count(*) from "ConstructionSite" where "tenantId"=%s', (TENANT,))
    if cur.fetchone()[0]:
        print("epc: Retail contractor projects already present — skipping")
        return
    RNG.seed(9001)
    coord = users[f"projects@{EMAIL_DOMAIN}"]
    t0 = now()
    comp_ids = []
    for name, code, trades, pq, size, score in COMPANIES:
        cid = new_id()
        comp_ids.append((cid, name, trades))
        cur.execute('''insert into "ContractorCompany"(id, name, code, "contactPerson", "contactEmail", "contactPhone", status,
            score, "prequalificationStatus", "prequalificationScore", "prequalificationReviewedAt",
            "prequalificationReviewedById", "prequalificationValidUntil", "sizeCategory", "tradeCategories",
            "safetyOfficerName", "tenantId", "updatedAt") values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (cid, name, code, f"{RNG.choice(FIRST)} {RNG.choice(LAST)}", f"ops@{code.lower()}.example.in",
                     f"98{RNG.randint(10000000, 99999999)}", "SUSPENDED" if pq == "suspended" else "ACTIVE", score, pq,
                     float(score), _naive(t0 - timedelta(days=90)), coord, _naive(t0 + timedelta(days=275)), size,
                     json.dumps(trades), f"{RNG.choice(FIRST)} {RNG.choice(LAST)}", TENANT, _naive(t0)))
    wseq = mseq = gseq = 0
    counts = {"sites": 0, "workers": 0, "mob": 0, "ind": 0, "gate": 0}
    for sno, project, ptype, status, cidx, ago, weeks, expired in PROJECTS:
        loc, city, state, _ = STORES[sno - 1]
        pid = plants[f"MR-S{sno:03d}"]
        site_id = new_id()
        code = f"MR-S{sno:03d}-FIT01"
        start = t0 - timedelta(days=ago)
        end = start + timedelta(weeks=weeks)
        cur.execute('''insert into "ConstructionSite"(id, "tenantId", "siteCode", "siteName", "projectNumber", "clientName",
            "clientProjectManager", address, district, state, "projectType", "scopeDescription", status, "plannedStartDate",
            "plannedCompletionDate", "actualStartDate", "actualCompletionDate", "peakWorkforcePlanned", "siteManagerUserId",
            "siteHseManagerUserId", "createdById", "updatedAt") values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (site_id, TENANT, code, f"Store {sno:03d} {loc} — {project}", f"MR-PRJ-26-{sno:03d}", "Meridian Retail",
                     "Imran Qureshi", f"Meridian Retail Store {sno:03d}, {loc}", city, state, ptype,
                     f"{project}. Store trades normally around a hoarded work zone; hot work and work at height under PTW.",
                     status, _naive(start), _naive(end), _naive(start) if status != "planning" else None,
                     _naive(end) if status == "completed" else None, 6 + 3 * len(cidx),
                     users.get(f"sm.s{sno:03d}@{EMAIL_DOMAIN}"), coord, coord, _naive(t0)))
        counts["sites"] += 1
        if status == "planning":
            continue
        for ci in cidx:
            cid, cname, trades = comp_ids[ci]
            for _ in range(2 if len(cidx) > 2 else 3):
                wseq += 1
                wid = new_id()
                wcode = f"MR-CW-26-{wseq:04d}"
                wname = f"{RNG.choice(FIRST)} {RNG.choice(LAST)}"
                trade = trades[0]
                cur.execute('''insert into "ContractorWorker"(id, "tenantId", "contractorCompanyId", "workerCode", "fullName",
                    "mobileNumber", "primaryTrade", "yearsExperience", "overallStatus", "rosterStatus",
                    "currentMedicalValidUntil", "aadhaarLast4", "aadhaarVerified", "createdById", "updatedAt")
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,true,%s,%s)''',
                            (wid, TENANT, cid, wcode, wname, f"9{RNG.randint(100000000, 999999999)}", trade, RNG.randint(2, 18),
                             "active", _naive(t0 + timedelta(days=RNG.randint(60, 300))), f"{RNG.randint(1000, 9999)}",
                             coord, _naive(t0)))
                counts["workers"] += 1
                mseq += 1
                mob_id = new_id()
                mstatus = "demobilised" if status == "completed" else "active"
                cur.execute('''insert into "MobilizationRecord"(id, "tenantId", "mobilizationNumber", "contractorWorkerId",
                    "contractorCompanyId", "siteId", "mobilizationType", "tradeAtSite", "workArea", "contractorCoordinatorUserId",
                    "mobilisationDate", "plannedDemobilisationDate", "actualDemobilisationDate", status, "approvedById",
                    "approvedAt", "createdById", "updatedAt") values (%s,%s,%s,%s,%s,%s,'new_deployment',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                            (mob_id, TENANT, f"MOB-{code}-2026-{mseq:04d}", wid, cid, site_id, trade, "Hoarded work zone",
                             coord, _naive(start), _naive(end), _naive(end) if mstatus == "demobilised" else None, mstatus,
                             coord, _naive(start - timedelta(days=1)), coord, _naive(t0)))
                counts["mob"] += 1
                valid_until = (t0 - timedelta(days=3)) if expired and counts["mob"] % 2 == 0 else (start + timedelta(days=180))
                cur.execute('''insert into "SiteInduction"(id, "tenantId", "contractorWorkerId", "siteId", "mobilizationRecordId",
                    "inductionType", "topicsCovered", "clientRequirementsCovered", "siteEmergencyProceduresCovered",
                    "siteLayoutFamiliarization", "musterPointIdentified", "ppeCoveredBool", "ptwSystemExplained",
                    "incidentReportingExplained", "conductedById", "conductedAt", "durationMinutes", "inductionLanguage",
                    "assessmentConducted", "assessmentScore", "assessmentPassScore", "assessmentPassed", "workerAcknowledged",
                    "workerAcknowledgementMethod", "workerAcknowledgedAt", "validFrom", "validUntil", "isExpired", "updatedAt")
                    values (%s,%s,%s,%s,%s,'full_site',%s,true,true,true,true,true,true,true,%s,%s,45,%s,true,%s,70,true,true,
                    'digital_signature',%s,%s,%s,false,%s)''',
                            (new_id(), TENANT, wid, site_id, mob_id,
                             json.dumps(["Store emergency exits & assembly point", "Customer-area segregation", "PTW for hot work / height",
                                         "Fire extinguisher locations", "Incident reporting"]),
                             users.get(f"sm.s{sno:03d}@{EMAIL_DOMAIN}") or coord, _naive(start), RNG.choice(["hindi", "english"]),
                             float(RNG.randint(72, 96)), _naive(start), _naive(start), _naive(valid_until), _naive(t0)))
                counts["ind"] += 1
                if status in ("active", "demobilising"):
                    gseq += 1
                    chk_id, pass_id = new_id(), new_id()
                    at = t0 - timedelta(hours=RNG.randint(2, 30))
                    result = "cleared_with_warnings" if (expired and counts["mob"] % 2 == 0) else "cleared"
                    cur.execute('''insert into "GateClearanceCheck"(id, "tenantId", "siteId", "contractorWorkerId", "workerName",
                        "workerCode", "contractorCompanyName", "checkRequestedAt", "checkMethod", checks, "overallResult",
                        "blockingIssues", "warningIssues", "gatePassIssued", "gatePassId", "checkCompletedAt", "processingDurationMs")
                        values (%s,%s,%s,%s,%s,%s,%s,%s,'qr_scan',%s,%s,'[]',%s,true,%s,%s,%s)''',
                                (chk_id, TENANT, site_id, wid, wname, wcode, cname, _naive(at),
                                 json.dumps({"induction": "pass", "medical": "pass", "prequalification": "pass"}), result,
                                 json.dumps(["Site induction expired — refresher due"] if result != "cleared" else []),
                                 pass_id, _naive(at + timedelta(seconds=4)), RNG.randint(900, 3800)))
                    cur.execute('''insert into "GatePass"(id, "tenantId", "siteId", "clearanceCheckId", "contractorWorkerId",
                        "workerName", "workerCode", "primaryTrade", "contractorCompanyName", "passNumber", "passType", "validFrom",
                        "validUntil", "authorizedAreas", "authorizedTrades", status, "qrCodeData", "generatedAt")
                        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'daily',%s,%s,%s,%s,'active',%s,%s)''',
                                (pass_id, TENANT, site_id, chk_id, wid, wname, wcode, trade, cname,
                                 f"GP-{code}-{at:%Y%m%d}-{gseq:04d}", _naive(at), _naive(at + timedelta(hours=12)),
                                 json.dumps(["Hoarded work zone", "Stockroom"]), json.dumps([trade]),
                                 f"safeops:gatepass:{pass_id}", _naive(at)))
                    counts["gate"] += 1
    print(f"epc: {len(COMPANIES)} contractors, {counts['sites']} fit-out projects, {counts['workers']} workers, "
          f"{counts['mob']} mobilisations, {counts['ind']} inductions, {counts['gate']} gate passes (tenant {TENANT})")


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
