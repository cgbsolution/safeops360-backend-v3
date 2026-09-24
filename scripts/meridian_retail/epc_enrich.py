"""Meridian Retail — Contractor Safety Management: fill every EPC screen.

epc.py seeds the backbone (6 contractors, 9 fit-out projects, 48 workers,
mobilisations, inductions, gate passes). This fills in what each sub-screen
reads, so none of them is thin or empty:

  * Contractors  — GST/PAN/registration, representative and safety officer,
                   compliance documents (licences, insurance, ESIC/PF), and a
                   suspension history on the suspended firm.
  * Projects     — statutory approvals, client contacts, contract value, and a
                   SiteComplianceConfig (PTW, induction, gate, PPE, KPI targets).
  * Workers      — medical fitness, training certificates, trade competencies,
                   PPE issuances, personal/emergency details; two workers carry
                   a lapsed medical so the gate has something to block.
  * Mobilisation — pre-mobilisation checklists on every record, plus new
                   deployments waiting in the approval queue (pending checks,
                   checks complete / pending approval) and one suspension.
  * Gate         — 14 days of history plus today, cleared / with warnings /
                   NOT cleared (lapsed medical, expired induction, suspended firm).
  * Inductions   — one failed assessment with re-induction due.

All rows carry tenantId RETAIL. Idempotent: skips if already applied.

    python -m scripts.meridian_retail.epc_enrich            # dry run
    python -m scripts.meridian_retail.epc_enrich --commit
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta, timezone

from scripts.meridian_retail.common import EMAIL_DOMAIN, RNG, conn, new_id, now, user_map

TENANT = "RETAIL"


def _n(d):
    return d.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(d):
    return d.astimezone(timezone.utc).isoformat()


COMPANY_DETAIL = {
    "MR-CC-01": ("Shelfline Interiors", "27AAKCS4471M1Z2", "AAKCS4471M", "U36100MH2011PTC219873", "Nitin Kulkarni"),
    "MR-CC-02": ("CoolAir HVAC", "29AAFCC8812K1Z9", "AAFCC8812K", "U29220KA2014PTC074411", "Sunil Reddy"),
    "MR-CC-03": ("Voltline Electricals", "27AAGFV2210P1Z4", "AAGFV2210P", "Partnership — PR/2016/0412", "Asif Shaikh"),
    "MR-CC-04": ("Rackfix Engineering", "06AAJCR6630L1Z1", "AAJCR6630L", "U28990HR2018PTC072230", "Harinder Pal"),
    "MR-CC-05": ("Brightsign Signage", "07AAMFB1190Q1Z6", "AAMFB1190Q", "Proprietorship — DL/2019/8812", "Rakesh Arora"),
    "MR-CC-06": ("FloorCraft Flooring", "32AANFF5512R1Z3", "AANFF5512R", "Proprietorship — KL/2020/3310", "Joseph Mathew"),
}
TRADE_COMPETENCY = {
    "electrical": ("ELEC-WIREMAN", "Licensed Wireman (State Electrical Licensing Board)"),
    "hvac": ("REFRIG-HANDLING", "Refrigerant Handling & Brazing Certificate"),
    "racking": ("RACK-INSPECT", "SEMA-approved Racking Installer"),
    "signage": ("WAH-ADV", "Working at Height — Advanced (MEWP operator)"),
    "carpentry": ("POWER-TOOLS", "Power Tools Safe Use"),
    "flooring": ("CHEM-HANDLING", "Adhesives & Solvents Safe Handling"),
}


def enrich(cur) -> None:
    users = user_map(cur)
    coord = users[f"projects@{EMAIL_DOMAIN}"]
    admin = users[f"store-ops.admin@{EMAIL_DOMAIN}"]
    cur.execute('select count(*) from "SiteComplianceConfig" where "tenantId"=%s', (TENANT,))
    if cur.fetchone()[0]:
        print("epc_enrich: already applied — skipping")
        return
    RNG.seed(4711)
    t0 = now()

    # ── Contractors ──
    cur.execute('select id, code, name, "prequalificationStatus" from "ContractorCompany" where "tenantId"=%s', (TENANT,))
    companies = cur.fetchall()
    for cid, code, name, pq in companies:
        trade_name, gst, pan, reg, rep = COMPANY_DETAIL[code]
        docs = [
            {"documentType": "GST Registration", "documentNumber": gst, "validUpto": None, "status": "valid"},
            {"documentType": "Labour Licence (CLRA)", "documentNumber": f"CLRA/{code}/2026", "validUpto": _iso(t0 + timedelta(days=210)), "status": "valid"},
            {"documentType": "Workmen's Compensation Insurance", "documentNumber": f"WC-{RNG.randint(100000, 999999)}",
             "validUpto": _iso(t0 + timedelta(days=RNG.choice([25, 140, 300]))), "status": "valid"},
            {"documentType": "ESIC / PF Registration", "documentNumber": f"ESIC-{RNG.randint(10000000, 99999999)}", "validUpto": None, "status": "valid"},
        ]
        if code == "MR-CC-03":
            docs.append({"documentType": "Electrical Contractor Licence", "documentNumber": "ECL/MH/2021/4410",
                         "validUpto": _iso(t0 + timedelta(days=400)), "status": "valid"})
        suspension = []
        if pq == "suspended":
            suspension = [{"suspendedAt": _iso(t0 - timedelta(days=12)), "suspendedBy": admin,
                           "reason": "Worker found operating floor grinder without guard and eye protection at Store 022; "
                                     "second PPE violation in 30 days.",
                           "liftedAt": None}]
        cur.execute('''update "ContractorCompany" set "tradeName"=%s, "gstNumber"=%s, "panNumber"=%s, "registrationNumber"=%s,
            "representativeName"=%s, "representativeEmail"=%s, "representativePhone"=%s, "safetyOfficerPhone"=%s,
            "complianceDocuments"=%s, "suspensionHistory"=%s, "updatedAt"=%s where id=%s''',
                    (trade_name, gst, pan, reg, rep, f"{rep.split()[0].lower()}@{code.lower()}.example.in",
                     f"98{RNG.randint(10000000, 99999999)}", f"97{RNG.randint(10000000, 99999999)}",
                     json.dumps(docs), json.dumps(suspension), _n(t0), cid))
    print(f"contractors: {len(companies)} enriched")

    # ── Projects + compliance config ──
    cur.execute('select id, "siteCode", "siteName", status, "plannedStartDate" from "ConstructionSite" where "tenantId"=%s', (TENANT,))
    sites = cur.fetchall()
    for sid, code, sname, status, start in sites:
        store = code[3:7]
        approvals = [
            {"type": "Shop & Establishment Licence (renovation intimation)", "authority": "Municipal Corporation",
             "documentNumber": f"SE/{store}/2026/{RNG.randint(100, 999)}", "date": _iso(start - timedelta(days=20))[:10], "status": "approved"},
            {"type": "Fire NOC — interior modification", "authority": "State Fire Services",
             "documentNumber": f"FS/NOC/{store}/{RNG.randint(1000, 9999)}", "date": _iso(start - timedelta(days=12))[:10],
             "status": "approved" if status != "planning" else "applied"},
            {"type": "Mall / landlord fit-out permission", "authority": "Property management",
             "documentNumber": f"LL/FO/{store}/26", "date": _iso(start - timedelta(days=30))[:10], "status": "approved"},
        ]
        cur.execute('''update "ConstructionSite" set "statutoryApprovals"=%s, "clientContactName"=%s, "clientContactEmail"=%s,
            "contractValue"=%s, "contractCurrency"='INR', "awardDate"=%s, "corporateHseOwnerUserId"=%s, "updatedAt"=%s where id=%s''',
                    (json.dumps(approvals), "Imran Qureshi", f"projects@{EMAIL_DOMAIN}", float(RNG.randint(18, 95)) * 100000,
                     _n(start - timedelta(days=35)), admin, _n(t0), sid))
        cur.execute('''insert into "SiteComplianceConfig"(id, "tenantId", "siteId", "clientName", "ptwConfig", "inductionConfig",
            "gateConfig", "mandatoryTraining", "minimumPpeRequirements", "kpiTargets", "effectiveFrom", "approvedById", "approvedAt",
            "updatedAt") values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())''',
                    (new_id(), TENANT, sid, "Meridian Retail",
                     json.dumps({"required": True, "types": ["hot_work", "electrical", "work_at_height"], "afterHoursOnly": True}),
                     json.dumps({"required": True, "durationMinutes": 45, "passMark": 70, "languages": ["Hindi", "English"]}),
                     json.dumps({"biometricRequired": False, "gatePassRequired": True, "photoRequired": True,
                                 "customerAreaSegregation": True}),
                     json.dumps(["store_safety_induction", "fire_extinguisher_use", "working_at_height"]),
                     json.dumps(["HELMET-INDUSTRIAL", "BOOTS-SAFETY", "VEST-HV", "GLOVES-GENERAL"]),
                     json.dumps({"ltifr": 0, "firstAidFrequency": 1.0, "nearMissReportingRate": 0.9, "customerIncidents": 0}),
                     _n(start), admin, _n(start - timedelta(days=2))))
    print(f"projects: {len(sites)} with statutory approvals + compliance config")

    # ── Workers ──
    cur.execute('select id, "workerCode", "primaryTrade", "createdAt" from "ContractorWorker" where "tenantId"=%s order by "workerCode"', (TENANT,))
    workers = cur.fetchall()
    lapsed = {workers[4][0], workers[17][0]}
    districts = [("Latur", "Maharashtra"), ("Gaya", "Bihar"), ("Sitapur", "Uttar Pradesh"), ("Mysuru", "Karnataka"),
                 ("Vizianagaram", "Andhra Pradesh"), ("Jhansi", "Uttar Pradesh"), ("Satara", "Maharashtra")]
    for wid, wcode, trade, created in workers:
        issued = t0 - timedelta(days=RNG.randint(40, 200))
        med_until = (t0 - timedelta(days=RNG.randint(3, 20))) if wid in lapsed else (issued + timedelta(days=365))
        comp = TRADE_COMPETENCY.get(trade)
        dist, state = RNG.choice(districts)
        cur.execute('''update "ContractorWorker" set "medicalFitnessRecords"=%s, "currentMedicalValidUntil"=%s,
            "trainingCertificates"=%s, "competencyRecords"=%s, "ppeIssuances"=%s, "emergencyContactName"=%s,
            "emergencyContactPhone"=%s, "emergencyContactRelation"='SPOUSE', "homeDistrict"=%s, "homeState"=%s,
            "educationLevel"=%s, gender='MALE', "bloodGroup"=%s, "overallStatus"=%s, "updatedAt"=%s where id=%s''',
                    (json.dumps([{"certificate_type": "Annual Medical Fitness", "issued_by": "Apollo Clinic — Occupational Health",
                                  "issued_at": _iso(issued), "valid_until": _iso(med_until), "conditions_noted": None,
                                  "certificate_url": None}]),
                     _n(med_until),
                     json.dumps([{"programCode": "STORE_SAFETY_INDUCTION", "programName": "Store Contractor Safety Induction",
                                  "issuedAt": _iso(issued + timedelta(days=2)), "validUntil": _iso(issued + timedelta(days=367)),
                                  "status": "valid", "certificateUrl": None},
                                 {"programCode": "FIRE_EXTINGUISHER_USE", "programName": "Fire Extinguisher Use & Evacuation",
                                  "issuedAt": _iso(issued + timedelta(days=2)), "validUntil": _iso(issued + timedelta(days=732)),
                                  "status": "valid", "certificateUrl": None}]),
                     json.dumps([{"competencyCode": comp[0], "competencyName": comp[1], "validFrom": _iso(issued - timedelta(days=300)),
                                  "validUntil": _iso(issued + timedelta(days=430)), "status": "valid",
                                  "assessorName": "Trade assessor — contractor QA"}] if comp else []),
                     json.dumps([{"ppeTypeCode": code, "ppeTypeName": pname, "issuedAt": _iso(issued + timedelta(days=3)),
                                  "expiresAt": _iso(issued + timedelta(days=368)), "itemSerial": f"{code[:4]}-{wcode[-4:]}",
                                  "status": "active"}
                                 for code, pname in (("HELMET-INDUSTRIAL", "Safety Helmet"), ("BOOTS-SAFETY", "Safety Shoes"),
                                                     ("VEST-HV", "Hi-vis Vest"))]),
                     f"{RNG.choice(['Sunita', 'Rekha', 'Pooja', 'Anita', 'Farah'])} ({wcode[-4:]})",
                     f"9{RNG.randint(100000000, 999999999)}", dist, state,
                     RNG.choice(["ITI Electrician", "10th pass", "12th pass", "ITI Fitter", "Diploma"]),
                     RNG.choice(["B+", "O+", "A+", "AB+", "O-"]),
                     "blocked" if wid in lapsed else "active", _n(t0), wid))
    print(f"workers: {len(workers)} enriched ({len(lapsed)} with a lapsed medical)")

    # ── Mobilisation checklists on existing records ──
    cur.execute('select id, "mobilisationDate", "contractorWorkerId" from "MobilizationRecord" where "tenantId"=%s', (TENANT,))
    for mid, mdate, wid in cur.fetchall():
        d = mdate.replace(tzinfo=timezone.utc)
        checks = {k: {"status": "pass", "checked_at": _iso(d), "checked_by": coord}
                  for k in ("id_verification", "medical_fitness", "training_induction", "ppe_issued", "site_rules_briefed")}
        checks["competency_verified"] = {"status": "pass", "checked_at": _iso(d)}
        cur.execute('update "MobilizationRecord" set "preMobilisationChecks"=%s where id=%s', (json.dumps(checks), mid))

    # ── New deployments in the approval queue ──
    cur.execute('select id, "siteCode" from "ConstructionSite" where "tenantId"=%s and status=%s order by "siteCode"', (TENANT, "active"))
    active_sites = cur.fetchall()
    cur.execute('select id, name, "tradeCategories" from "ContractorCompany" where "tenantId"=%s and "prequalificationStatus"<>%s',
                (TENANT, "suspended"))
    firms = cur.fetchall()
    cur.execute('select count(*) from "ContractorWorker" where "tenantId"=%s', (TENANT,))
    wseq = cur.fetchone()[0]
    cur.execute('select count(*) from "MobilizationRecord" where "tenantId"=%s', (TENANT,))
    mseq = cur.fetchone()[0]
    queue = [("pending_checks", 4), ("checks_complete_pending_approval", 3), ("suspended", 1)]
    first = ["Rajan", "Kishore", "Balaji", "Naveen", "Ajay", "Shyam", "Irfan", "Dinesh"]
    last = ["Gowda", "Pawar", "Rathod", "Yadav", "Mondal", "Shaikh", "Nair", "Chauhan"]
    k = 0
    for status, count in queue:
        for _ in range(count):
            k += 1
            wseq += 1
            mseq += 1
            sid, scode = active_sites[k % len(active_sites)]
            fid, fname, trades = firms[k % len(firms)]
            trade = (trades or ["carpentry"])[0]
            wid = new_id()
            cur.execute('''insert into "ContractorWorker"(id, "tenantId", "contractorCompanyId", "workerCode", "fullName",
                "mobileNumber", "primaryTrade", "yearsExperience", "overallStatus", "rosterStatus", "aadhaarLast4",
                "aadhaarVerified", "createdById", "updatedAt") values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s)''',
                        (wid, TENANT, fid, f"MR-CW-26-{wseq:04d}", f"{first[k % 8]} {last[(k * 3) % 8]}",
                         f"9{RNG.randint(100000000, 999999999)}", trade, RNG.randint(2, 12),
                         "pending" if status == "pending_checks" else "active", f"{RNG.randint(1000, 9999)}",
                         status != "pending_checks", coord, _n(t0)))
            mdate = t0 + timedelta(days=RNG.randint(1, 6)) if status != "suspended" else t0 - timedelta(days=9)
            pend = {"status": "pending"}
            ok = lambda: {"status": "pass", "checked_at": _iso(t0 - timedelta(days=1)), "checked_by": coord}  # noqa: E731
            checks = ({"id_verification": ok(), "medical_fitness": pend, "training_induction": pend, "ppe_issued": pend,
                       "site_rules_briefed": pend} if status == "pending_checks"
                      else {k2: ok() for k2 in ("id_verification", "medical_fitness", "training_induction", "ppe_issued",
                                                "site_rules_briefed")})
            cur.execute('''insert into "MobilizationRecord"(id, "tenantId", "mobilizationNumber", "contractorWorkerId",
                "contractorCompanyId", "siteId", "mobilizationType", "tradeAtSite", "workArea", "contractorCoordinatorUserId",
                "mobilisationDate", "plannedDemobilisationDate", "preMobilisationChecks", status, "approvedById", "approvedAt",
                "approvalConditions", "demobilisationReason", "createdById", "updatedAt")
                values (%s,%s,%s,%s,%s,%s,'new_deployment',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (new_id(), TENANT, f"MOB-{scode}-2026-{mseq:04d}", wid, fid, sid, trade, "Hoarded work zone", coord,
                         _n(mdate), _n(mdate + timedelta(days=30)), json.dumps(checks), status,
                         admin if status == "suspended" else None, _n(t0 - timedelta(days=9)) if status == "suspended" else None,
                         "Night shift only; no work above 2 m without store manager sign-off" if status == "suspended" else None,
                         "Suspended pending investigation — worked in customer area without barricading" if status == "suspended" else None,
                         coord, _n(t0)))
    print(f"mobilisation: approval queue {', '.join(f'{c} {s}' for s, c in queue)}")

    # ── Induction: a failed assessment with re-induction due ──
    cur.execute('select id from "SiteInduction" where "tenantId"=%s order by "conductedAt" limit 1', (TENANT,))
    ind = cur.fetchone()
    cur.execute('''update "SiteInduction" set "assessmentScore"=55, "assessmentPassed"=false, "failedTopics"=%s,
        "reInductionRequired"=true, "reInductionDate"=%s where id=%s''',
                (json.dumps(["Fire extinguisher locations", "PTW for hot work / height"]), _n(t0 + timedelta(days=2)), ind[0]))

    # ── Gate history: 14 days + today, incl. NOT cleared ──
    cur.execute('''select w.id, w."workerCode", w."fullName", w."primaryTrade", c.name, c."prequalificationStatus", m."siteId",
        s."siteCode", w."currentMedicalValidUntil" from "ContractorWorker" w join "ContractorCompany" c on c.id=w."contractorCompanyId"
        join "MobilizationRecord" m on m."contractorWorkerId"=w.id join "ConstructionSite" s on s.id=m."siteId"
        where w."tenantId"=%s and s.status in ('active','demobilising') and m.status in ('active','suspended')''', (TENANT,))
    pool = cur.fetchall()
    cur.execute('''select w.id, w."workerCode", w."fullName", w."primaryTrade", c.name, s.id, s."siteCode"
        from "ContractorWorker" w join "ContractorCompany" c on c.id=w."contractorCompanyId"
        join "MobilizationRecord" m on m."contractorWorkerId"=w.id join "ConstructionSite" s on s.id=m."siteId"
        where w."tenantId"=%s and c."prequalificationStatus"='suspended' limit 2''', (TENANT,))
    suspended_workers = cur.fetchall()
    gseq, n_checks, n_block = 0, 0, 0
    cur.execute('select count(*) from "GatePass" where "tenantId"=%s', (TENANT,))
    gseq = cur.fetchone()[0] + 100
    for day in range(14, -1, -1):
        date0 = (t0 - timedelta(days=day)).replace(hour=3, minute=0, second=0, microsecond=0)  # ~08:30 IST
        todays = RNG.sample(pool, k=min(len(pool), 6 if day else 8))
        extra = [(s[0], s[1], s[2], s[3], s[4], "suspended", s[5], s[6], None) for s in suspended_workers] if day in (3, 0) else []
        for w in todays + extra:
            wid, wcode, wname, trade, cname, pq, sid, scode, med = w
            at = date0 + timedelta(minutes=RNG.randint(0, 150))
            if day == 0 and at > t0:
                at = t0 - timedelta(minutes=RNG.randint(5, 90))
            blocking, warning = [], []
            if pq == "suspended":
                blocking.append(f"Contractor company {cname} is suspended")
            if med and med.replace(tzinfo=timezone.utc) < at:
                blocking.append("Medical fitness certificate expired")
            if not blocking and RNG.random() < 0.12:
                warning.append("Site induction refresher due within 7 days")
            result = "not_cleared" if blocking else ("cleared_with_warnings" if warning else "cleared")
            chk = {"identity": {"result": "pass", "detail": "Aadhaar verified"},
                   "induction": {"result": "pass" if "induction" not in " ".join(blocking) else "fail", "detail": "Store induction on file"},
                   "medical": {"result": "fail" if "Medical" in " ".join(blocking) else "pass", "detail": "Annual fitness certificate"},
                   "prequalification": {"result": "fail" if pq == "suspended" else "pass", "detail": f"{cname} — {pq}"},
                   "ppe": {"result": "pass", "detail": "Helmet, safety shoes, hi-vis vest"}}
            cid_, pid_ = new_id(), (new_id() if result != "not_cleared" else None)
            cur.execute('''insert into "GateClearanceCheck"(id, "tenantId", "siteId", "contractorWorkerId", "workerName", "workerCode",
                "contractorCompanyName", "checkRequestedAt", "checkMethod", checks, "overallResult", "blockingIssues", "warningIssues",
                "gatePassIssued", "gatePassId", "checkCompletedAt", "processingDurationMs", "createdAt")
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (cid_, TENANT, sid, wid, wname, wcode, cname, _n(at), RNG.choice(["qr_scan", "qr_scan", "manual_search"]),
                         json.dumps(chk), result, json.dumps(blocking), json.dumps(warning), pid_ is not None, pid_,
                         _n(at + timedelta(seconds=4)), RNG.randint(900, 4200), _n(at)))
            if pid_:
                gseq += 1
                cur.execute('''insert into "GatePass"(id, "tenantId", "siteId", "clearanceCheckId", "contractorWorkerId", "workerName",
                    "workerCode", "primaryTrade", "contractorCompanyName", "passNumber", "passType", "validFrom", "validUntil",
                    "authorizedAreas", "authorizedTrades", status, "qrCodeData", "generatedAt", "createdAt")
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'daily',%s,%s,%s,%s,%s,%s,%s,%s)''',
                            (pid_, TENANT, sid, cid_, wid, wname, wcode, trade, cname, f"GP-{scode}-{at:%Y%m%d}-{gseq:04d}",
                             _n(at), _n(at + timedelta(hours=12)), json.dumps(["Hoarded work zone", "Stockroom"]), json.dumps([trade]),
                             "active" if day == 0 else "expired", f"safeops:gatepass:{pid_}", _n(at), _n(at)))
            n_checks += 1
            n_block += result == "not_cleared"
    print(f"gate: {n_checks} checks over 15 days ({n_block} NOT cleared, with blocking reasons)")


def main(commit: bool) -> None:
    c = conn()
    cur = c.cursor()
    enrich(cur)
    if commit:
        c.commit()
        print("committed")
    else:
        c.rollback()
        print("dry run — rolled back (pass --commit)")


if __name__ == "__main__":
    main("--commit" in sys.argv)
