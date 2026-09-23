"""Meridian Retail — Fire & Life Safety at Page Industries depth.

Same schema and engines as the Page build; only the data is Meridian Retail's:

  1. Controlled documents: Meridian Retail copies of the routine checklists
     (Daily / Monthly / Quarterly / Yearly per asset type), numbered MR/FLS/CL/…,
     tagged documentMeta.tenant = "RETAIL" so they apply only at Retail sites
     (services/fire_tenancy.py). Branded registers: three FireRegisterViewConfig
     rows with tenantId "RETAIL", a Store column and Meridian Retail branding.
  2. Register: ~150 assets over 40 stores + 3 DCs with mixed coverage, extinguisher
     register data and HP-test / refill certificates (some expired, some due).
  3. History: routine checklist runs over the compliance window, one CamsEngagement
     per (asset, template, period) exactly as the checklist engine writes them.
     Each store has a discipline profile, so completion is a mix of on-schedule,
     overdue and missed — not all-green.
  4. CAMS: completed Fire Safety audits (lead auditor, fire standards, asset scope)
     assigned through the real independence engine, plus one open audit for the
     "Include in audit" demo.

    python -m scripts.meridian_retail.fire            # dry run
    python -m scripts.meridian_retail.fire --commit
    python -m scripts.meridian_retail.fire --commit --reset   # rebuild Retail fire data
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from datetime import date, datetime, timedelta, timezone

from psycopg2.extras import execute_values

from scripts.meridian_retail.common import (
    DCS,
    EMAIL_DOMAIN,
    RNG,
    STORES,
    conn,
    dc_code,
    new_id,
    now,
    plant_map,
    store_code,
    user_map,
)

TENANT = "RETAIL"

# ── 1. Controlled documents ─────────────────────────────────────────────────
DOC_NO = {
    "PIL-FAS-DAILY": ("MR-FAS-DAILY", "MR/FLS/CL/01-R1 (A)", "Daily Fire Alarm Panel Round"),
    "PIL-FAS-MONTHLY-UNIT_21_A": ("MR-FAS-MONTHLY-ZONE", "MR/FLS/CL/01-R1 (B)", "Monthly Fire Alarm Panel Test — Zone Panels"),
    "PIL-FAS-MONTHLY-UNIT_21_B": ("MR-FAS-MONTHLY-LOOP", "MR/FLS/CL/01-R1 (C)", "Monthly Fire Alarm Panel Test — Loop Panels"),
    "PIL-FAS-QUARTERLY": ("MR-FAS-QUARTERLY", "MR/FLS/CL/01-R1 (D)", "Quarterly Fire Detection & Alarm Test"),
    "PIL-FAS-ANNUAL": ("MR-FAS-ANNUAL", "MR/FLS/CL/01-R1 (E)", "Annual Fire Alarm System Service"),
    "PIL-FHS-DAILY": ("MR-FHS-DAILY", "MR/FLS/CL/02-R1 (A)", "Daily Hydrant & Sprinkler Pump Round"),
    "PIL-FHS-MONTHLY": ("MR-FHS-MONTHLY", "MR/FLS/CL/02-R1 (B)", "Monthly Hydrant & Sprinkler Inspection"),
    "PIL-FHS-QUARTERLY": ("MR-FHS-QUARTERLY", "MR/FLS/CL/02-R1 (C)", "Quarterly Hydrant & Sprinkler Test"),
    "PIL-FHS-YEARLY": ("MR-FHS-YEARLY", "MR/FLS/CL/02-R1 (D)", "Annual Hydrant & Sprinkler System Service"),
    "PIL-FE-INSPECTION": ("MR-FE-INSPECTION", "MR/FLS/CL/03-R1", "Monthly Fire Extinguisher Inspection"),
}
# Page's sheets name Page's own shop floor (hooters "Near Dyeing Emergency exit",
# dB readings at "Warping" / "ETP"). The Retail copies keep every check and
# replace those location rows with store zones.
RETAIL_HOOTERS = ["Near security cabin", "Sales floor — centre aisle", "Stockroom", "Checkout & entrance",
                  "Cold room / food court"]
QUARTERLY_ZONES = {"fas_q_db_warping": ("fas_q_db_sales", "Sales floor"),
                   "fas_q_db_weaving": ("fas_q_db_stock", "Stockroom"),
                   "fas_q_db_dyeing": ("fas_q_db_checkout", "Checkout & entrance"),
                   "fas_q_db_hrd": ("fas_q_db_office", "Back office"),
                   "fas_q_db_etp": ("fas_q_db_cold", "Cold room / food court")}


def retail_definition(t):
    """The Retail copy of one Page sheet, with Page's shop-floor rows replaced."""
    from app.services import fire_checklist_templates as ft

    if t.code == "PIL-FAS-MONTHLY-UNIT_21_A":
        t = ft._fas_monthly("ZONE_PANEL", "Zone Number", RETAIL_HOOTERS, ["B1", "B2"])
    elif t.code == "PIL-FAS-MONTHLY-UNIT_21_B":
        t = ft._fas_monthly("LOOP_PANEL", "Loop Number", RETAIL_HOOTERS, ["B1", "B2"])
    elif t.code == "PIL-FAS-QUARTERLY":
        sections = []
        for sec in t.sections:
            items = [dataclasses.replace(i, key=QUARTERLY_ZONES[i.key][0], text=QUARTERLY_ZONES[i.key][1])
                     if i.key in QUARTERLY_ZONES else i for i in sec.items]
            sections.append(dataclasses.replace(sec, items=items))
        t = dataclasses.replace(t, sections=sections)
    return t


SIGN_OFF = ("Prepared by: Fire Safety Technician", "Reviewed by: Store Manager", "Approved by: Head — Store Safety")

REGISTERS = [
    {
        "assetType": "FIRE_EXTINGUISHER", "routeSlug": "extinguisher-register",
        "brandName": "Meridian Retail — Register of Fire Extinguishers", "documentNo": "MR/FLS/REG/FE-01",
        "columns": [
            ("slNo", "Sl. No"), ("siteName", "Store"), ("allottedSerialNo", "Tag No."), ("type", "Type"),
            ("capacity", "Capacity"), ("make", "Make"), ("location", "Location in store"),
            ("expiryDate", "Cylinder life ends"), ("hpTestDueDate", "HP test due"),
            ("dueForRefilling", "Refill due"), ("remarks", "Remarks"),
        ],
    },
    {
        "assetType": "FIRE_ALARM_PANEL", "routeSlug": "alarm-panel-register",
        "brandName": "Meridian Retail — Register of Fire Alarm Panels", "documentNo": "MR/FLS/REG/FAS-01",
        "columns": [
            ("slNo", "Sl. No"), ("siteName", "Store"), ("equipmentCode", "Panel Code"),
            ("assetSubtype", "Addressing"), ("make", "Make"), ("model", "Model"), ("location", "Location"),
            ("lastInspectionDate", "Last inspected"), ("nextInspectionDueDate", "Next due"),
            ("status", "Status"), ("remarks", "Remarks"),
        ],
    },
    {
        "assetType": "FIRE_HYDRANT_SYSTEM", "routeSlug": "hydrant-system-register",
        "brandName": "Meridian Retail — Register of Hydrant & Sprinkler Systems", "documentNo": "MR/FLS/REG/FHS-01",
        "columns": [
            ("slNo", "Sl. No"), ("siteName", "Store"), ("equipmentCode", "System Code"), ("make", "Make"),
            ("capacity", "Rated capacity"), ("location", "Pump room / location"),
            ("lastInspectionDate", "Last inspected"), ("nextInspectionDueDate", "Next due"),
            ("status", "Status"), ("remarks", "Remarks"),
        ],
    },
]


async def seed_documents(commit: bool) -> None:
    from sqlalchemy import select

    from app.core.db import AsyncSessionLocal
    from app.models.fire_safety import FireRegisterViewConfig
    from app.models.user import User
    from app.services.fire_checklist_templates import ALL_TEMPLATES
    from seed_fire_checklists import ensure_audit_type, upsert_template

    async with AsyncSessionLocal() as db:
        owner = (await db.execute(select(User).where(User.email == f"store-ops.admin@{EMAIL_DOMAIN}"))).scalars().first()
        assert owner, "run scripts.meridian_retail.base --commit first"
        audit_type = await ensure_audit_type(db)
        n = 0
        for t in ALL_TEMPLATES:
            if t.code not in DOC_NO:
                continue  # no beam detectors in a retail estate
            code, doc_no, name = DOC_NO[t.code]
            rt = dataclasses.replace(
                retail_definition(t), code=code, name=name, documentNo=doc_no, supersedesNo="", revision="R1",
                effectiveDate="2026-04-01", reviewDate="2029-03-31", department="Store Operations & Safety",
                signOffRoles=SIGN_OFF, sourceSheet="Meridian Retail FLS manual",
                footnotes=[f.replace("Electrician", "Technician") for f in t.footnotes],
            )
            tpl, _ = await upsert_template(db, rt, owner.id, audit_type.id)
            tpl.documentMeta = {**(tpl.documentMeta or {}), "tenant": TENANT}
            tpl.isGlobal = False
            n += 1
        for r in REGISTERS:
            cfg = (
                await db.execute(
                    select(FireRegisterViewConfig).where(
                        FireRegisterViewConfig.tenantId == TENANT, FireRegisterViewConfig.assetType == r["assetType"]
                    )
                )
            ).scalars().first()
            if cfg is None:
                cfg = FireRegisterViewConfig(tenantId=TENANT, assetType=r["assetType"], createdBy=owner.id)
                db.add(cfg)
            cfg.brandName, cfg.routeSlug, cfg.documentNo = r["brandName"], r["routeSlug"], r["documentNo"]
            cfg.supersedesNo, cfg.revision = None, "R1"
            cfg.effectiveDate = datetime(2026, 4, 1, tzinfo=timezone.utc)
            cfg.reviewDate = datetime(2029, 3, 31, tzinfo=timezone.utc)
            cfg.department = "Store Operations & Safety"
            cfg.columns = [{"key": k, "label": lbl} for k, lbl in r["columns"]]
            cfg.pdfTemplateKey = "GENERIC_REGISTER"
            cfg.isActive = True
        print(f"documents: {n} Meridian Retail checklists (tenant {TENANT}), {len(REGISTERS)} branded registers")
        if commit:
            await db.commit()
        else:
            await db.rollback()


# ── 2. Register ─────────────────────────────────────────────────────────────
MAKES = ["Ceasefire", "Safex", "Minimax", "Kanex", "Firetech"]
PANEL_MAKES = [("Honeywell", "Notifier NFS-320"), ("Siemens", "Cerberus FC722"), ("Bosch", "FPA-1200"), ("Ravel", "RE-116")]
FE_SPOTS = ["Checkout zone", "Stockroom door", "Back office", "Cold room entrance", "Food court", "Electrical DB room"]
DC_FE_SPOTS = ["Loading dock 2", "Racking aisle 14", "Battery charging bay"]
# Store discipline → (P(complete), P(in progress)) per owed occurrence.
DISCIPLINE = {"ON_TRACK": (0.96, 0.02), "SLIPPING": (0.85, 0.05), "BEHIND": (0.66, 0.08), "NEGLECTED": (0.38, 0.07)}


def _discipline(code: str) -> str:
    # A stable, deliberately uneven spread: most stores fine, a tail that is not.
    i = int(code[-2:])
    return ["ON_TRACK", "ON_TRACK", "SLIPPING", "ON_TRACK", "BEHIND", "ON_TRACK", "SLIPPING", "NEGLECTED"][i % 8]


def plan_assets(plants: dict[str, str]) -> list[dict]:
    assets: list[dict] = []

    def fe(pcode, n, spots):
        for k in range(1, n + 1):
            sub = RNG.choice(["ABC", "ABC", "ABC", "CO2", "CO2", "FOAM"])
            cap = {"ABC": "6 kg", "CO2": "4.5 kg", "FOAM": "9 L"}[sub]
            yom = RNG.choice([2018, 2019, 2020, 2021, 2022, 2023, 2024])
            assets.append(dict(
                plant=pcode, code=f"{pcode}-FE-{k:02d}", type="FIRE_EXTINGUISHER", subtype=sub,
                make=RNG.choice(MAKES), model=None, serial=f"{RNG.choice('KSMN')}{RNG.randint(100000, 999999)}",
                cap=cap, location=spots[(k - 1) % len(spots)], yom=yom, weight={"ABC": 10.5, "CO2": 11.8, "FOAM": 13.2}[sub],
                allotted=f"FE/{pcode[3:]}/{k:02d}",
            ))

    def panel(pcode, sub):
        mk, md = RNG.choice(PANEL_MAKES)
        assets.append(dict(plant=pcode, code=f"{pcode}-FAP-01", type="FIRE_ALARM_PANEL", subtype=sub, make=mk, model=md,
                           serial=f"FAP{RNG.randint(10000, 99999)}", cap=f"{RNG.choice([4, 8, 16])} {'loops' if sub == 'LOOP' else 'zones'}",
                           location="Security cabin" if pcode.startswith("MR-DC") else "Back office — fire control point"))

    def hydrant(pcode):
        assets.append(dict(plant=pcode, code=f"{pcode}-FHS-01", type="FIRE_HYDRANT_SYSTEM", subtype="HYDRANT_SPRINKLER",
                           make=RNG.choice(["Kirloskar", "Grundfos", "Mather+Platt"]), model=None,
                           serial=f"PMP{RNG.randint(10000, 99999)}", cap=f"{RNG.choice([1620, 2280, 2850])} LPM @ 7 bar",
                           location="Basement pump room" if not pcode.startswith("MR-DC") else "Pump house"))

    for i, (_, _, _, fmt) in enumerate(STORES, 1):
        pc = store_code(i)
        if fmt == "Hypermarket":
            fe(pc, 3, FE_SPOTS); panel(pc, "LOOP"); hydrant(pc)
        elif fmt == "Supermarket":
            fe(pc, 2, FE_SPOTS); panel(pc, "ZONE")
            if i % 3 == 0:
                hydrant(pc)
        else:  # Express
            fe(pc, 1 if i % 2 else 2, FE_SPOTS)
            if i % 3 == 0:
                panel(pc, "ZONE")
    for d in range(1, len(DCS) + 1):
        pc = dc_code(d)
        fe(pc, 3, DC_FE_SPOTS); panel(pc, "LOOP"); hydrant(pc)
    return assets


def _period_label(freq: str, d: date) -> str:
    return {"DAILY": d.isoformat(), "MONTHLY": f"{d.year:04d}-{d.month:02d}",
            "QUARTERLY": f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}", "ANNUAL": f"{d.year:04d}"}[freq]


def _periods(freq: str, start: date, end: date) -> list[tuple[str, date]]:
    out, seen, d = [], set(), start
    while d <= end:
        lbl = _period_label(freq, d)
        if lbl not in seen:
            seen.add(lbl)
            out.append((lbl, d))
        d += timedelta(days=1)
    return out


def _variant_ok(variant: str | None, subtype: str | None) -> bool:
    if not variant or not subtype:
        return True
    return variant.upper() in subtype.upper() or subtype.upper() in variant.upper()


def seed_register_and_history(cur, reset: bool) -> dict[str, str]:
    plants = plant_map(cur)
    users = user_map(cur)
    pids = list(plants.values())
    cur.execute('select count(*) from "FireEquipment" where "plantId" = any(%s)', (pids,))
    if cur.fetchone()[0] and not reset:
        print("register: Retail fire assets already present (use --reset to rebuild)")
        cur.execute('select "equipmentCode", id from "FireEquipment" where "plantId" = any(%s)', (pids,))
        return dict(cur.fetchall())
    if reset:
        cur.execute('delete from "CamsEngagement" where "sourceModule"=%s and "siteId" = any(%s)', ("FIRE", pids))
        cur.execute('delete from "FireAssetCertificate" where "plantId" = any(%s)', (pids,))
        cur.execute('delete from "FireChecklistReminder" where "plantId" = any(%s)', (pids,))
        cur.execute('delete from "FireEquipment" where "plantId" = any(%s)', (pids,))
        print("reset: Retail fire data removed")

    RNG.seed(4242)
    today = now().date()
    tech_for = {}
    cur.execute(
        'select u.email, ur."scopeValue" from "UserRole" ur join "User" u on u.id=ur."userId" '
        'where u.email like %s and ur."scopeType"=%s', (f"fire.tech%@{EMAIL_DOMAIN}", "PLANT"))
    for email, pid in cur.fetchall():
        tech_for.setdefault(pid, users[email])
    manager_for = {}
    cur.execute('select id, "plantId", email from "User" where email like %s', (f"%@{EMAIL_DOMAIN}",))
    for uid, pid, email in cur.fetchall():
        if email.startswith(("sm.s", "dc.manager")):
            manager_for[pid] = uid
    approver = users[f"store-ops.admin@{EMAIL_DOMAIN}"]

    assets = plan_assets(plants)
    rows, certs, ids = [], [], {}
    for a in assets:
        aid = new_id()
        ids[a["code"]] = aid
        pid = plants[a["plant"]]
        installed = datetime(a.get("yom") or RNG.choice([2019, 2020, 2021, 2022]), RNG.randint(1, 12), RNG.randint(1, 28), tzinfo=timezone.utc)
        is_fe = a["type"] == "FIRE_EXTINGUISHER"
        rows.append((
            aid, a["code"], a["type"], a["make"], a.get("model"), a["serial"], a["location"], pid, installed,
            30, "ACTIVE", a["cap"], "Blaze Guard Fire Services (AMC)", a["subtype"],
            a.get("allotted"), a.get("yom"),
            datetime(a["yom"] + 10, 3, 31, tzinfo=timezone.utc) if is_fe else None,
            a.get("weight"), None, tech_for.get(pid), "seed:meridian-retail",
        ))
        if is_fe:
            # HP test every 5 years, refill yearly — a spread of expired / due / fine.
            hp_issue = today - timedelta(days=RNG.choice([400, 900, 1500, 1790, 1820]))
            rf_issue = today - timedelta(days=RNG.choice([40, 120, 200, 300, 340, 372, 395]))
            for ctype, issue, life in (("HYDROSTATIC_TEST", hp_issue, 5 * 365), ("REFILL", rf_issue, 365)):
                exp = issue + timedelta(days=life)
                certs.append((new_id(), aid, pid, ctype, f"{ctype[:2]}-{RNG.randint(10000, 99999)}",
                              "Blaze Guard Fire Services", datetime.combine(issue, datetime.min.time(), timezone.utc),
                              datetime.combine(exp, datetime.min.time(), timezone.utc),
                              "EXPIRED" if exp < today else "VALID"))
    execute_values(cur, '''insert into "FireEquipment"(id, "equipmentCode", type, make, model, "serialNo", location,
        "plantId", "installationDate", "inspectionFrequencyDays", status, "capacitySpec", "maintenanceContractor",
        "assetSubtype", "allottedSerialNo", "yearOfManufacture", "expiryDate", "weightKg", "registerRemarks",
        "assignedTechnicianId", "createdBy") values %s''', rows)
    execute_values(cur, '''insert into "FireAssetCertificate"(id, "assetId", "plantId", "certificateType",
        "certificateNo", "issuingAuthority", "issueDate", "expiryDate", status) values %s''', certs)
    print(f"register: {len(rows)} assets, {len(certs)} certificates")

    # ── History ──
    cur.execute('select id, "templateCode", version, "documentMeta" from "CamsTemplate" where "documentMeta"->>%s = %s '
                'and status=%s', ("tenant", TENANT, "APPROVED"))
    templates = cur.fetchall()
    cur.execute('select t."templateCode", q.id, q."questionType", q."ncTriggersFinding" from "CamsTemplateQuestion" q '
                'join "CamsTemplateSection" s on s.id=q."sectionId" join "CamsTemplate" t on t.id=s."templateId" '
                'where t."documentMeta"->>%s = %s order by s."orderIndex", q."orderIndex"', ("tenant", TENANT))
    questions: dict[str, list[tuple[str, str]]] = {}
    for tcode, qid, qtype, _ in cur.fetchall():
        questions.setdefault(tcode, []).append((qid, qtype))

    start = date(today.year, today.month, 1) - timedelta(days=95)
    start = date(start.year, start.month, 1)
    eng_rows, resp_rows = [], []
    last_done: dict[str, datetime] = {}
    seq = 0
    for a in assets:
        pid = plants[a["plant"]]
        p_done, p_prog = DISCIPLINE[_discipline(a["plant"])]
        tech = tech_for.get(pid) or approver
        for tid, tcode, version, meta in templates:
            if meta.get("assetType") != a["type"] or not _variant_ok(meta.get("siteVariant"), a["subtype"]):
                continue
            freq = meta.get("frequency", "MONTHLY")
            for label, pstart in _periods(freq, start, today):
                current = _period_label(freq, today) == label
                r = RNG.random()
                if current:
                    # The period that is still open: most not yet done, some started.
                    status = "REPORT_ISSUED" if r < p_done * 0.45 else ("IN_PROGRESS" if r < p_done * 0.45 + 0.25 else None)
                else:
                    status = "REPORT_ISSUED" if r < p_done else ("FIELDWORK_COMPLETE" if r < p_done + p_prog else None)
                if status is None:
                    continue  # never opened — owed, missed
                seq += 1
                eid = new_id()
                done_at = datetime.combine(min(pstart + timedelta(days=RNG.randint(0, 3)), today), datetime.min.time(),
                                           timezone.utc) + timedelta(hours=RNG.randint(9, 17))
                approved = status == "REPORT_ISSUED"
                answers = []
                for qid, qtype in questions.get(tcode, []):
                    if qtype == "NUMERIC":
                        answers.append({"questionId": qid, "value": round(RNG.uniform(6.5, 7.4), 1), "conformance": None})
                    elif qtype == "TEXT":
                        answers.append({"questionId": qid, "value": "OK", "conformance": None})
                    else:
                        nc = RNG.random() < (0.03 if p_done > 0.8 else 0.09)
                        answers.append({"questionId": qid, "value": "NO" if nc else "YES",
                                        "conformance": "NC" if nc else "CONFORM",
                                        "ncSeverity": "MINOR_NC" if nc else None,
                                        "note": "Reported to store manager" if nc else ""})
                signoffs = None
                if approved:
                    signoffs = [
                        {"role": "PREPARED", "userId": tech, "name": None, "designation": "Fire Safety Technician",
                         "signatureKind": "TYPED", "typedName": "Technician", "statement": "Checked as recorded",
                         "signedAt": done_at.isoformat()},
                        {"role": "REVIEWED", "userId": manager_for.get(pid), "name": None, "designation": "Store Manager",
                         "signatureKind": "TYPED", "typedName": "Store Manager", "statement": "Reviewed",
                         "signedAt": (done_at + timedelta(hours=5)).isoformat()},
                        {"role": "APPROVED", "userId": approver, "name": "Kavya Menon",
                         "designation": "Head — Store Operations & Safety", "signatureKind": "TYPED",
                         "typedName": "Kavya Menon", "statement": "Approved", "signedAt": (done_at + timedelta(days=1)).isoformat()},
                    ]
                eng_rows.append((
                    eid, f"MRF-{done_at.year}-{seq:05d}", f"{DOC_NO_NAME[tcode]} — {a['code']} — {label}", "INSPECTION",
                    meta.get("auditTypeId"), json.dumps([meta.get("documentNo")]), pid, a["code"],
                    f"{meta.get('documentNo', '')} R1 — {a['location']}", tech,
                    datetime.combine(pstart, datetime.min.time(), timezone.utc), done_at, tid, version, status,
                    "FIRE", ids[a["code"]], label,
                    manager_for.get(pid) if approved else None, done_at + timedelta(hours=5) if approved else None,
                    approver if approved else None, done_at + timedelta(days=1) if approved else None,
                    json.dumps(signoffs) if signoffs else None, tech,
                ))
                resp_rows.append((new_id(), eid, version, json.dumps(answers), tech if status != "IN_PROGRESS" else None,
                                  done_at if status != "IN_PROGRESS" else None))
                if approved and freq in ("MONTHLY",):
                    prev = last_done.get(a["code"])
                    if prev is None or done_at > prev:
                        last_done[a["code"]] = done_at
    for i in range(0, len(eng_rows), 800):
        execute_values(cur, '''insert into "CamsEngagement"(id, "engagementCode", title, "engagementType", "auditTypeId",
            "standardRefs", "siteId", "areaOrAssetRef", "scopeStatement", "leadAuditorId", "plannedDate", "conductedDate",
            "templateId", "templateVersionUsed", status, "sourceModule", "sourceEntityId", "periodLabel",
            "reviewedBy", "reviewedAt", "approvedBy", "approvedAt", "signOffs", "createdBy", "updatedAt")
            values %s''', [r + (now(),) for r in eng_rows[i:i + 800]])
    for i in range(0, len(resp_rows), 800):
        execute_values(cur, '''insert into "CamsResponse"(id, "engagementId", "templateVersionUsed", answers,
            "completedBy", "completedAt", "sectionScores", "updatedAt") values %s''',
                       [r + (json.dumps([]), now()) for r in resp_rows[i:i + 800]])
    print(f"history: {len(eng_rows)} checklist runs "
          f"({sum(1 for r in eng_rows if r[14] == 'REPORT_ISSUED')} approved, "
          f"{sum(1 for r in eng_rows if r[14] != 'REPORT_ISSUED')} submitted/in progress; the rest of the owed periods missed)")

    # Register dates + status from the last approved monthly run (same rule as
    # services/fire_safety.compute_status: OVERDUE past due, DUE_INSPECTION ≤7d).
    for a in assets:
        last = last_done.get(a["code"])
        if last is None:
            last = datetime.combine(start - timedelta(days=RNG.randint(5, 40)), datetime.min.time(), timezone.utc)
        nxt = last + timedelta(days=30)
        days = (nxt.date() - today).days
        status = "OVERDUE" if days < 0 else ("DUE_INSPECTION" if days <= 7 else "ACTIVE")
        cur.execute('update "FireEquipment" set "lastInspectionDate"=%s, "nextInspectionDueDate"=%s, status=%s where id=%s',
                    (last, nxt, status, ids[a["code"]]))
    # Two assets out of service with a recorded reason (sticky override).
    for code, why in (("MR-S008-FAP-01", "Panel mainboard failed — replacement ordered"),
                      ("MR-S032-FHS-01", "Jockey pump seal leak — pump isolated, AMC visit booked")):
        if code in ids:
            cur.execute('update "FireEquipment" set status=%s, "statusOverride"=%s, "statusOverrideReason"=%s, '
                        '"outOfServiceReason"=%s where id=%s', ("OUT_OF_SERVICE", "OUT_OF_SERVICE", why, why, ids[code]))
    cur.execute('select status, count(*) from "FireEquipment" where "plantId" = any(%s) group by status', (pids,))
    print("register status:", dict(cur.fetchall()))
    return ids


DOC_NO_NAME = {v[0]: v[2] for v in DOC_NO.values()}


# ── 4. CAMS Fire Safety audits ──────────────────────────────────────────────
AUDITS = [
    # (store, lead, team, status, days ago conducted, result, score, findings[(severity, status, question idx)])
    ("MR-S001", "fire.auditor1", ["fire.auditor2"], "CLOSED", 58, "MINOR_NC", 91.0, [("MINOR_NC", "CLOSED", 1)]),
    ("MR-S020", "fire.auditor2", ["fire.auditor3"], "REPORT_ISSUED", 24, "MAJOR_NC", 74.5,
     [("MAJOR_NC", "OPEN", 9), ("MINOR_NC", "IN_PROGRESS", 12), ("OBSERVATION", "OPEN", 14)]),
    ("MR-S034", "fire.auditor3", ["fire.auditor1"], "REPORT_ISSUED", 9, "MINOR_NC", 86.0,
     [("MINOR_NC", "OPEN", 2), ("MINOR_NC", "OPEN", 5)]),
]
OPEN_AUDIT = ("MR-S009", "fire.auditor1", ["fire.auditor2"], 12)  # planned, 12 days ahead


async def seed_audits(commit: bool) -> None:
    from sqlalchemy import func, select

    from app.core.db import AsyncSessionLocal
    from app.models.cams import CamsAuditType, CamsEngagement, CamsFinding, CamsResponse, CamsTemplateQuestion, CamsTemplateSection
    from app.models.fire_audit import FIRE_AUDIT_TYPE_CODE, CamsEngagementAsset
    from app.models.fire_safety import FireEquipment
    from app.models.plant import Plant
    from app.models.user import User
    from app.services import independence as inde
    from app.services.independence_events import record_verdicts

    async with AsyncSessionLocal() as db:
        at = (await db.execute(select(CamsAuditType).where(CamsAuditType.typeCode == FIRE_AUDIT_TYPE_CODE))).scalars().first()
        assert at, "run scripts/seed_fire_audit_type.py --commit first"
        existing = (await db.execute(select(func.count()).select_from(CamsEngagement).where(
            CamsEngagement.auditTypeId == at.id, CamsEngagement.engagementCode.like("FSA-MR-%")))).scalar()
        if existing:
            print(f"audits: {existing} Meridian Retail Fire Safety audits already present")
            return
        qs = (await db.execute(
            select(CamsTemplateQuestion).join(CamsTemplateSection).where(CamsTemplateSection.templateId == at.defaultTemplateId)
            .order_by(CamsTemplateSection.orderIndex, CamsTemplateQuestion.orderIndex))).scalars().all()
        users = {u.email.split("@")[0]: u for u in (await db.execute(
            select(User).where(User.email.like(f"%@{EMAIL_DOMAIN}")))).scalars().all()}
        plants = {p.code: p for p in (await db.execute(select(Plant).where(Plant.code.like("MR-%")))).scalars().all()}
        fcount = (await db.execute(select(func.count()).select_from(CamsFinding))).scalar() or 0

        async def make(n: int, store: str, lead: str, team: list[str], status: str, when: datetime):
            p = plants[store]
            manager = users[f"sm.s{int(store[-3:]):03d}"]
            eng = CamsEngagement(
                engagementCode=f"FSA-MR-{when.year}-{n:03d}", title=f"Fire Safety Audit — {p.name.split(' — ')[0]}",
                engagementType="COMPLIANCE_AUDIT", auditTypeId=at.id, standardRefs=list(at.standardRefs or []),
                siteId=p.id, leadAuditorId=users[lead].id, auditTeamIds=[users[t].id for t in team],
                auditeeOwnerId=manager.id, plannedDate=when, templateId=at.defaultTemplateId,
                scopeStatement="Annual independent Fire & Life Safety audit: extinguishers, detection & alarm, "
                               "hydrant & sprinkler, means of escape, records.",
                sourceModule="FIRE", status=status, createdBy=users["store-ops.admin"].id,
            )
            scope = inde.scope_for_engagement(eng)
            scope.kind = "AUDIT"
            verdicts = await inde.check_many(db, user_ids=[users[lead].id] + [users[t].id for t in team],
                                             scope=scope, assigning_as="AUDITOR")
            blocked = [uid for uid, v in verdicts.items() if not v.allowed]
            assert not blocked, f"independence blocked {store}: {blocked}"
            db.add(eng)
            await db.flush()
            await record_verdicts(verdicts=verdicts, engagement_kind="AUDIT", origin="SEED",
                                  attempted_by_user_id=users["store-ops.admin"].id, engagement_id=eng.id,
                                  engagement_code=eng.engagementCode, site_id=p.id, include_cleared=True, session=db)
            assets = (await db.execute(select(FireEquipment.id).where(FireEquipment.plantId == p.id))).scalars().all()
            for aid in assets:
                db.add(CamsEngagementAsset(engagementId=eng.id, sourceModule="FIRE", entityId=aid,
                                           addedBy=users[lead].id))
            return eng, manager, verdicts

        for n, (store, lead, team, status, ago, result, score, findings) in enumerate(AUDITS, 1):
            conducted = now() - timedelta(days=ago)
            eng, manager, verdicts = await make(n, store, lead, team, status, conducted - timedelta(days=2))
            eng.conductedDate, eng.templateVersionUsed = conducted, 1
            eng.overallResult, eng.scorePercent = result, score
            eng.reviewedBy, eng.reviewedAt = users[team[0]].id, conducted + timedelta(days=2)
            eng.approvedBy, eng.approvedAt = users["store-ops.admin"].id, conducted + timedelta(days=4)
            eng.signOffs = [
                {"role": "LEAD_AUDITOR", "userId": users[lead].id, "name": users[lead].name,
                 "designation": "Fire Safety Lead Auditor", "signatureKind": "TYPED", "typedName": users[lead].name,
                 "statement": "Audit conducted as recorded", "signedAt": (conducted + timedelta(days=1)).isoformat()},
                {"role": "AUDITEE", "userId": manager.id, "name": manager.name, "designation": "Store Manager",
                 "signatureKind": "TYPED", "typedName": manager.name, "statement": "Findings acknowledged",
                 "signedAt": (conducted + timedelta(days=2)).isoformat()},
            ]
            nc_idx = {q for _, _, q in findings}
            answers = []
            for qi, q in enumerate(qs):
                is_nc = qi in nc_idx
                answers.append({"questionId": q.id, "value": "NC" if is_nc else "CONFORM",
                                "conformance": "NC" if is_nc else "CONFORM", "note": "", "evidenceAttachmentIds": []})
            db.add(CamsResponse(engagementId=eng.id, templateVersionUsed=1, answers=answers, sectionScores=[],
                                completedBy=users[lead].id, completedAt=conducted))
            for sev, fstatus, qi in findings:
                fcount += 1
                q = qs[qi]
                db.add(CamsFinding(
                    findingCode=f"FND-{conducted.year}-MR{fcount:04d}", engagementId=eng.id, siteId=eng.siteId,
                    title=q.text[:120], description=f"Non-conformance against {q.standardClauseRef}: {q.text}",
                    severity=sev, status=fstatus, standardClauseRef=q.standardClauseRef, ownerId=manager.id,
                    dueDate=conducted + timedelta(days=30 if sev == "MINOR_NC" else 14),
                    closedBy=manager.id if fstatus == "CLOSED" else None,
                ))
            outcome = {uid: ("WARN" if v.conflicts else "CLEAR") for uid, v in verdicts.items()}
            print(f"audit {eng.engagementCode} {store} {status} score {score} — independence {outcome}")

        store, lead, team, ahead = OPEN_AUDIT
        eng, _, _ = await make(len(AUDITS) + 1, store, lead, team, "SCHEDULED", now() + timedelta(days=ahead))
        eng.scheduledStart = now() + timedelta(days=ahead)
        print(f"audit {eng.engagementCode} {store} SCHEDULED (open — 'Include in audit' target)")
        if commit:
            await db.commit()
        else:
            await db.rollback()


def main(commit: bool, reset: bool) -> None:
    asyncio.run(seed_documents(commit))
    c = conn()
    cur = c.cursor()
    seed_register_and_history(cur, reset)
    if commit:
        c.commit()
    else:
        c.rollback()
    if commit:
        asyncio.run(seed_audits(commit))
    print("committed" if commit else "dry run — rolled back (pass --commit)")


if __name__ == "__main__":
    main("--commit" in sys.argv, "--reset" in sys.argv)
