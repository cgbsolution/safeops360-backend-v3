"""Import the JSL Hisar client workbooks into the SafeOps database.

    python scripts/import_jsl_hisar.py --hira HIRA_SMS-1.xlsx --observations "Safety Observation ....xlsx"
    python scripts/import_jsl_hisar.py ... --apply      # commit (default is a dry run that rolls back)
    python scripts/import_jsl_hisar.py --rollback --apply   # remove everything this script created

Until now this data only lived in the copilot's JSON knowledge files, which the
HIRA and Observation screens never read. This script writes it to the real
tables under a dedicated plant (code JSL-HSR):

  * Plant, Departments (one per department code in the sheets) and Areas
  * One "import" user that owns the records (it cannot log in)
  * HIRA:  one HiraStudy for SMS-1 (QSD/24/F) with one HiraEntry per sheet row,
           its hazard (HiraEntryHazard) and controls (HiraEntryControl, from the
           sheet's control reference plus the QSD/25/F "Controls" sheet)
  * Observations: one Observation per register row, numbered JSL-<Inspection Code>

Scoring: the sheet rates SV = LH x CN, so LH and CN go onto the 5x5 matrix as
likelihood and severity. SP, DE, GT and the legal/domino/chronic/emergency
concerns are kept in the rationale text. The sheet scores risk with existing
controls in place, so the residual rating equals the initial one and
"acceptable" comes from the sheet's Yes/No column.

Re-running is safe: rows that already exist (same study / observation number)
are skipped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import secrets
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import bcrypt
import openpyxl
import psycopg2
import psycopg2.extras
from dotenv import dotenv_values

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.services.copilot.knowledge.observations import stop_candidates  # noqa: E402

PLANT = dict(code="JSL-HSR", name="JSL Hisar — Stainless Steel Works", location="Hisar",
             state="Haryana", unitType="Steel")
IMPORT_USER_EMAIL = "jsl.import@safeops360.in"
STUDY_NUMBER = "HIRA-JSL-SMS1-001"
OBS_PREFIX = "JSL-"
LIBRARY = BACKEND / "app/services/copilot/knowledge/data/observation_library.json"

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)  # DB columns are naive UTC


def new_id() -> str:
    return uuid.uuid4().hex


def clean(v):
    if isinstance(v, str):
        v = " ".join(v.split())
        return v or None
    return v


def num(v):
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


# ─────────────────────────────────────────────────────────────────────
# HIRA workbook (QSD/24/F + QSD/25/F)
# ─────────────────────────────────────────────────────────────────────

def grid(ws):
    g = {}
    for row in ws.iter_rows():
        for c in row:
            g[(c.row, c.column)] = c.value
    for mr in ws.merged_cells.ranges:
        v = g.get((mr.min_row, mr.min_col))
        for r in range(mr.min_row, mr.max_row + 1):
            for cc in range(mr.min_col, mr.max_col + 1):
                g[(r, cc)] = v
    return g, ws.max_row


def section_key(name: str) -> str:
    words = re.sub(r"[^a-z ]", " ", name.lower()).split()
    return " ".join(words[:2])


def header_value(ws, prefix: str) -> str | None:
    for row in ws.iter_rows(min_row=1, max_row=16, values_only=True):
        for v in row:
            if isinstance(v, str) and v.strip().lower().startswith(prefix.lower()):
                return clean(v.split(":", 1)[1]) if ":" in v else clean(v)
    return None


def parse_hira(path: str):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = next(w for w in wb.worksheets if w.title.strip().upper() == "HIRA")
    meta = {
        "dept": header_value(ws, "Location/Dept"),
        "team": header_value(ws, "HIRA Team Members"),
        "non_managerial": header_value(ws, "Non Managerial Workers"),
        "leader": header_value(ws, "Team Leader"),
    }
    g, mx = grid(ws)
    for r in range(1, 16):
        for c in range(1, 24):
            if g.get((r, c)) == "Revision No.":
                meta["revision"] = g.get((r, c + 1))
            if g.get((r, c)) == "Date:":
                meta["date"] = g.get((r, c + 1))

    rows, section, prev = [], None, None
    last_sno = 0
    for r in range(17, mx + 1):
        v = [clean(g.get((r, c))) for c in range(1, 21)]
        a, b, hazard = v[0], v[1], v[3]
        if isinstance(a, str) and (a.startswith("*") or a.lower().startswith("approved")):
            break
        if isinstance(a, str) and all(x in (None, a) for x in v[1:6]):
            section, last_sno = a, 0
            continue
        if a is None and isinstance(b, str) and hazard is None and v[10] is None:
            section, last_sno = b, 0            # e.g. "COVID-19 Activities" heading in column B
            continue
        if hazard is None and b is None and v[10] is None:
            continue
        sno = num(a)
        if sno == 1 and last_sno > 1 and section and not section.startswith("General"):
            section = "General (All Activities)"   # unlabelled block where S.No restarts
        if sno is None and prev:
            sno = prev["sno"]
        last_sno = sno or last_sno
        activity = b or (prev["activity"] if prev else None)
        if hazard is None and prev:              # continuation row: same hazard, re-scored
            hazard, v[4], v[5] = prev["hazard"], v[4] or prev["risk"], v[5] or prev["control_ref"]
        row = dict(xl_row=r, section=section, sno=sno, activity=activity,
                   rnr=(v[2] or (prev["rnr"] if prev else "R")).strip().upper(),
                   hazard=hazard, risk=v[4], control_ref=v[5],
                   LC=v[6], DC=v[7], CE=v[8], EC=v[9], SP=num(v[10]), LH=num(v[11]), CN=num(v[12]),
                   SV=num(v[13]), DE=num(v[14]), GT=num(v[15]), sig=v[16], prev_gt=v[17],
                   acceptable=v[18], legal=v[19])
        if row["LH"] is None or row["CN"] is None:
            continue
        rows.append(row)
        prev = row

    cws = wb["Controls"]
    g, mx = grid(cws)
    cols = ["elimination", "tech_modification", "procedure", "engineering", "signage",
            "administrative", "competence", "pip", "ppe"]
    # The Controls sheet has headings only for the plant areas; the General and
    # COVID blocks follow AFS with S.No restarting. A drop in S.No therefore
    # moves to the next HIRA section, in the HIRA sheet's order.
    order = list(dict.fromkeys(r["section"] for r in rows))
    controls, section, prev, last_sno = [], None, None, 0
    for r in range(10, mx + 1):  # rows 1-9 are the form header
        v = [clean(g.get((r, k))) for k in range(1, 14)]
        a = v[0]
        if isinstance(a, str) and (a.lower().startswith("approved") or a.startswith("1)")):
            break
        if isinstance(a, str) and all(x in (None, a) for x in v[1:5]):
            section, last_sno = a, 0
            continue
        if v[1] is None and v[2] is None and not any(v[4:13]):
            continue
        sno = num(a) if num(a) is not None else (prev["sno"] if prev else None)
        if sno is not None and sno < last_sno and section:
            pos = next((i for i, s in enumerate(order) if section_key(s) == section_key(section)), None)
            if pos is not None and pos + 1 < len(order):
                section = order[pos + 1]
        last_sno = sno if sno is not None else last_sno
        d = dict(xl_row=r, section=section, sno=sno, hazard=v[2] or (prev["hazard"] if prev else None))
        d.update(zip(cols, v[4:13]))
        controls.append(d)
        prev = d
    return meta, rows, controls


HAZARD_RULES: list[tuple[str, str]] = [
    (r"covid|contaminat|infect|medical waste|animal bite", "BIO_INFECTIOUS_AGENT"),
    (r"sexual", "PSYCH_HARASSMENT"),
    (r"working hours", "PSYCH_FATIGUE"),
    (r"workload", "PSYCH_WORKLOAD"),
    (r"behaviour|mobile phone|communication", "BEHAV_SHORTCUTS"),
    (r"electric", "ELEC-001"),
    (r"steam explosion|puncturing|slag may splash|splashing", "FIRE_MOLTEN_METAL"),
    (r"explosion|fire", "FIRE-001"),
    (r"burn|scald", "@burn"),
    (r"heat stress|radiation|radition", "ENV_HEAT_STRESS"),
    (r"vibration", "MECH_VIBRATION"),
    (r"pressure", "MECH-004"),
    (r"slip|trip", "PHYS_SLIP_TRIP"),
    (r"vehic|truck|horn|parking", "VEH-004"),
    (r"crush", "MECH_PINCH_POINT"),
    (r"equipment|equipmental", "@equipment"),
    (r"gravity", "@gravity"),
    (r"ergonomic", "@ergo"),
    (r"environment|respiratory", "@env"),
    (r"machin|tool", "MECH_UNGUARDED_ROTATING"),
]


def hazard_code(hazard: str, risk: str | None) -> str:
    h, rk = (hazard or "").lower(), (risk or "").lower()
    for rx, code in HAZARD_RULES:
        if re.search(rx, h):
            break
    else:
        return "MECH_UNGUARDED_ROTATING"
    if code == "@burn":
        return "FIRE_MOLTEN_METAL" if re.search(r"liquid|metal|slag|splash", rk) else "THERMAL_HOT_SURFACE"
    if code == "@equipment":
        return "MECH-008" if re.search(r"crane|rope|hook|sling|lift", h + " " + rk) else "MECH-007"
    if code == "@gravity":
        return "HEIGHT_FALL_OVER_2M" if re.search(r"fall from|height|platform", rk) else "MECH_FALLING_OBJECT"
    if code == "@ergo":
        return "MECH_HEAVY_LIFTING" if re.search(r"heavy|load|lift", rk) else "ERGO_AWKWARD_POSTURE"
    if code == "@env":
        return "NOISE_HIGH_EXPOSURE" if "noise" in rk else "ENV_DUST_AIRBORNE"
    return code


CONTROL_COLUMNS = [  # (sheet column, hierarchy, label)
    ("elimination", "ELIMINATION", "Elimination"),
    ("tech_modification", "ENGINEERING", "Technical modification"),
    ("engineering", "ENGINEERING", "Engineering control"),
    ("procedure", "ADMINISTRATIVE", "Procedure / practice"),
    ("signage", "ADMINISTRATIVE", "Signage / warning"),
    ("administrative", "ADMINISTRATIVE", "Administrative control"),
    ("competence", "ADMINISTRATIVE", "Competence / behaviour"),
    ("pip", "ADMINISTRATIVE", "PIP"),
    ("ppe", "PPE", "PPE"),
]

DOC_REF = re.compile(r"\b(DOC\s*NO\.?\s*)?SM\w*/\d+/\w+", re.I)


def norm_ref(s: str | None) -> str:
    return re.sub(r"[^a-z0-9/]", "", (s or "").lower().replace("docno", ""))


# ─────────────────────────────────────────────────────────────────────
# Observation register
# ─────────────────────────────────────────────────────────────────────

def parse_observations(path: str):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    it = wb.worksheets[0].iter_rows(values_only=True)
    hdr = [clean(h) for h in next(it)]
    return [dict(zip(hdr, r)) for r in it if r and r[0]]


TYPE_FOR = {"Unsafe-Act": ("UNSAFE_ACT", "ACT"), "Unsafe-Condition": ("UNSAFE_CONDITION", "CONDITION"),
            "Safe-Act": ("SAFE_ACT", "ACT"), "Safe-Condition": ("SAFE_CONDITION", "CONDITION")}
LEVEL_FOR = {"Low": "LOW", "Medium": "MEDIUM", "High": "HIGH"}


def person(name, emp=None, extra=None):
    name = clean(name)
    if not name:
        return None
    bits = [b for b in (extra, f"Emp {emp}" if emp else None) if b]
    return f"{name} ({', '.join(bits)})" if bits else name


# ─────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────

def connect():
    env = dotenv_values(BACKEND / ".env")
    url = env["DATABASE_URL_SYNC"]
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    return psycopg2.connect(url)


def one(cur, sql, *args):
    cur.execute(sql, args)
    r = cur.fetchone()
    return r[0] if r else None


def insert_many(cur, table: str, rows: list[dict]):
    if not rows:
        return
    cols = list(rows[0].keys())
    col_sql = ", ".join('"' + c + '"' for c in cols)
    sql = f'INSERT INTO "{table}" ({col_sql}) VALUES %s'
    vals = [tuple(psycopg2.extras.Json(r[c]) if isinstance(r[c], (dict, list)) else r[c] for c in cols) for r in rows]
    psycopg2.extras.execute_values(cur, sql, vals, page_size=500)


def ensure_plant(cur) -> str:
    pid = one(cur, 'SELECT id FROM "Plant" WHERE code=%s', PLANT["code"])
    if pid:
        return pid
    pid = new_id()
    cur.execute('INSERT INTO "Plant" (id, code, name, location, state, "unitType") VALUES (%s,%s,%s,%s,%s,%s)',
                (pid, PLANT["code"], PLANT["name"], PLANT["location"], PLANT["state"], PLANT["unitType"]))
    print(f"  + plant {PLANT['code']}")
    return pid


def ensure_departments(cur, plant_id, codes) -> dict[str, str]:
    cur.execute('SELECT name, id FROM "Department" WHERE "plantId"=%s', (plant_id,))
    have = dict(cur.fetchall())
    new = [dict(id=new_id(), plantId=plant_id, name=c, code=c) for c in sorted(codes) if c not in have]
    insert_many(cur, "Department", new)
    have.update({d["name"]: d["id"] for d in new})
    if new:
        print(f"  + {len(new)} departments")
    return have


def ensure_areas(cur, plant_id, names) -> dict[str, str]:
    cur.execute('SELECT name, id FROM "Area" WHERE "plantId"=%s', (plant_id,))
    have = dict(cur.fetchall())
    new = [dict(id=new_id(), plantId=plant_id, name=n) for n in sorted(names) if n not in have]
    insert_many(cur, "Area", new)
    have.update({a["name"]: a["id"] for a in new})
    if new:
        print(f"  + {len(new)} areas")
    return have


def ensure_import_user(cur, plant_id) -> str:
    uid = one(cur, 'SELECT id FROM "User" WHERE email=%s', IMPORT_USER_EMAIL)
    if uid:
        return uid
    uid = new_id()
    # Random unknown password: the account exists to own imported records, not to log in.
    pw = bcrypt.hashpw(secrets.token_urlsafe(32).encode(), bcrypt.gensalt(10)).decode().replace("$2b$", "$2a$", 1)
    cur.execute('INSERT INTO "User" (id, email, name, "passwordHash", role, "plantId", designation, department) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (uid, IMPORT_USER_EMAIL, "JSL Hisar (Excel import)", pw, "HSE_MANAGER", plant_id,
                 "Data import", "HSE"))
    print(f"  + import user {IMPORT_USER_EMAIL}")
    return uid


# ─────────────────────────────────────────────────────────────────────
# Import steps
# ─────────────────────────────────────────────────────────────────────

def import_hira(cur, path, plant_id, user_id, replace=False):
    if one(cur, 'SELECT id FROM "HiraStudy" WHERE number=%s', STUDY_NUMBER):
        if not replace:
            print(f"  = HIRA study {STUDY_NUMBER} already exists, skipped (use --replace-hira to rebuild)")
            return
        cur.execute('DELETE FROM "HiraStudy" WHERE number=%s', (STUDY_NUMBER,))
        print(f"  - removed existing {STUDY_NUMBER} (entries, hazards, controls cascade)")
    meta, rows, controls = parse_hira(path)
    dept_code = "SMS-1"
    dept_id = ensure_departments(cur, plant_id, [dept_code])[dept_code]
    area_name = {s: f"{dept_code} / {s}" for s in {r["section"] for r in rows}}
    areas = ensure_areas(cur, plant_id, area_name.values())

    matrix_id = one(cur, 'SELECT id FROM "RiskMatrix" WHERE code=%s', "STD_5X5")
    cur.execute('SELECT score, id FROM "RiskMatrixLikelihood" WHERE "matrixId"=%s', (matrix_id,))
    lik = dict(cur.fetchall())
    cur.execute('SELECT score, id FROM "RiskMatrixSeverity" WHERE "matrixId"=%s', (matrix_id,))
    sev = dict(cur.fetchall())
    cur.execute('SELECT "likelihoodScore", "severityScore", "riskScore", "riskLevel", "colorHex" '
                'FROM "RiskMatrixCell" WHERE "matrixId"=%s', (matrix_id,))
    cell = {(l, s): (score, lvl, col) for l, s, score, lvl, col in cur.fetchall()}
    cur.execute('SELECT code, id FROM "HiraHazard"')
    hazards = dict(cur.fetchall())

    try:
        eff = dt.datetime.strptime(str(meta.get("date")), "%d.%m.%Y")
    except ValueError:
        eff = NOW
    study_id = new_id()
    regs = sorted({r["legal"] for r in rows if r["legal"]})
    desc = (f"Imported from client workbook QSD/24/F (HIRA) and QSD/25/F (Controls), "
            f"Location/Dept: {meta.get('dept')}, Revision {meta.get('revision')} dated {meta.get('date')}.\n"
            f"Team leader: {meta.get('leader')}\n"
            f"HIRA team (managerial): {meta.get('team')}\n"
            f"Non-managerial workers: {meta.get('non_managerial')}\n"
            f"Scoring in the sheet: SV = LH x CN, GT = SP x SV x DE. LH and CN are used as likelihood "
            f"and severity on the 5x5 matrix.")

    entries, ent_haz, ent_ctl = [], [], []
    by_sec_sno = defaultdict(list)
    levels_init = Counter()
    acceptable_count = 0
    for i, r in enumerate(rows, 1):
        L, S = max(1, min(5, r["LH"])), max(1, min(5, r["CN"]))
        score, level, color = cell[(L, S)]
        levels_init[level] += 1
        acceptable = (r["acceptable"] or "").strip().lower().startswith("y") if r["acceptable"] else None
        acceptable_count += bool(acceptable)
        concerns = [n for k, n in (("LC", "legal"), ("DC", "domino"), ("CE", "chronic effect"), ("EC", "emergency"))
                    if (r[k] or "").upper() == "Y"]
        eid = new_id()
        entries.append(dict(
            id=eid, studyId=study_id, sequenceNumber=i, groupLabel=r["section"],
            activityDescription=r["activity"] or r["hazard"], areaId=areas[area_name[r["section"]]],
            subLocation=f"Sheet S.No {r['sno']}" if r["sno"] is not None else None,
            routine="NON_ROUTINE" if r["rnr"].startswith("N") else "ROUTINE",
            frequency="OCCASIONAL" if r["rnr"].startswith("N") else "DAILY",
            initialLikelihoodId=lik[L], initialLikelihoodScore=L,
            initialLikelihoodRationale=f"QSD/24/F likelihood LH = {r['LH']}",
            initialSeverityId=sev[S], initialSeverityScore=S,
            initialSeverityRationale=(f"QSD/24/F consequence CN = {r['CN']} (SV = LH x CN = {r['SV']}); "
                                      f"SP = {r['SP']}, DE = {r['DE']}, GT = {r['GT']}"
                                      + (f"; concerns: {', '.join(concerns)}" if concerns else "")),
            initialRiskScore=score, initialRiskLevel=level, initialRiskColor=color,
            residualLikelihoodId=lik[L], residualLikelihoodScore=L,
            residualSeverityId=sev[S], residualSeverityScore=S,
            residualRiskScore=score, residualRiskLevel=level, residualRiskColor=color,
            residualAcceptable=acceptable,
            residualAcceptanceRationale=(f"Acceptable OH&S risk per QSD/24/F: {r['acceptable'] or 'not stated'} "
                                         f"(scored with existing controls in place; sheet row {r['xl_row']})"),
            status="ACTIVE", versionNumber=1, isCurrentVersion=True,
            createdAt=eff, createdById=user_id, updatedAt=NOW, updatedById=user_id,
            lastReviewedAt=eff, lastReviewedById=user_id, nextReviewDue=eff + dt.timedelta(days=365),
        ))
        code = hazard_code(r["hazard"], r["risk"])
        ent_haz.append(dict(id=new_id(), entryId=eid, hazardId=hazards[code],
                            contextualDescription=r["hazard"], consequence=r["risk"],
                            potentialHarm=[r["risk"]] if r["risk"] else None,
                            regulationRef=(r["legal"] or None) and r["legal"][:200], sortOrder=0))
        order = 0
        if r["control_ref"]:
            ref = r["control_ref"]
            is_doc = bool(DOC_REF.search(ref)) and len(ref) < 40
            ent_ctl.append(dict(id=new_id(), entryId=eid, hierarchy="ADMINISTRATIVE",
                                description=f"Work instruction / SOP {ref}" if is_doc else ref,
                                documentReference=ref if is_doc else None, sortOrder=order))
            order += 1
        by_sec_sno[(section_key(r["section"]), r["sno"])].append((eid, r, order))

    matched = 0
    unmatched = []
    for c in controls:
        cands = by_sec_sno.get((section_key(c["section"] or ""), c["sno"]))
        if not cands:
            unmatched.append((c["section"], c["sno"], c["xl_row"]))
            continue
        hz = (c["hazard"] or "").lower()[:6]
        idx = next((k for k, (_, r, _) in enumerate(cands) if (r["hazard"] or "").lower()[:6] == hz), 0)
        eid, r, order = cands[idx]
        for col, hier, label in CONTROL_COLUMNS:
            val = c.get(col)
            if not val or str(val).strip().lower() in ("-", "na", "n/a", "no"):
                continue
            val = str(val)
            if col == "procedure" and norm_ref(val) and norm_ref(val) in norm_ref(r["control_ref"]):
                continue  # same SOP already recorded from the HIRA sheet
            text = f"{label}: in place" if val.strip().lower() == "yes" else f"{label}: {val}"
            ent_ctl.append(dict(id=new_id(), entryId=eid, hierarchy=hier, description=text,
                                documentReference=val if col == "procedure" else None, sortOrder=order))
            order += 1
        cands[idx] = (eid, r, order)
        matched += 1

    study = dict(
        id=study_id, number=STUDY_NUMBER, plantId=plant_id, departmentId=dept_id, areaId=None,
        scopeType="DEPARTMENT", title=f"{dept_code} Steel Melt Shop — HIRA (QSD/24/F Rev {meta.get('revision')})",
        description=desc, riskMatrixId=matrix_id, teamLeaderId=user_id, status="ACTIVE",
        initiatedAt=eff, completedAt=eff, approvedAt=eff, effectiveFrom=eff,
        nextScheduledReviewDate=eff + dt.timedelta(days=365), reviewFrequency="ANNUAL",
        applicableRegulations=regs, regulatoryReviewRequired=bool(regs),
        aggregateMetrics={"totalEntries": len(entries),
                          "byInitialLevel": {k: levels_init.get(k, 0) for k in ("LOW", "MODERATE", "HIGH", "CRITICAL")},
                          "byResidualLevel": {k: levels_init.get(k, 0) for k in ("LOW", "MODERATE", "HIGH", "CRITICAL")},
                          "acceptableResidualCount": acceptable_count},
        createdAt=NOW, createdById=user_id, updatedAt=NOW, updatedById=user_id,
    )
    insert_many(cur, "HiraStudy", [study])
    insert_many(cur, "HiraEntry", entries)
    insert_many(cur, "HiraEntryHazard", ent_haz)
    insert_many(cur, "HiraEntryControl", ent_ctl)
    print(f"  + HIRA study {STUDY_NUMBER}: {len(entries)} entries, {len(ent_haz)} hazards, "
          f"{len(ent_ctl)} controls ({matched}/{len(controls)} QSD/25/F rows matched)")
    print(f"    sections: {dict(Counter(r['section'] for r in rows))}")
    print(f"    risk levels: {dict(levels_init)}")
    if unmatched:
        print(f"    ! unmatched control rows (section, sno, sheet row): {unmatched[:10]}")


def import_observations(cur, path, plant_id, user_id):
    rows = parse_observations(path)
    numbers = [OBS_PREFIX + str(r["Inspection Code"]).strip() for r in rows]
    cur.execute('SELECT number FROM "Observation" WHERE number = ANY(%s)', (numbers,))
    existing = {n for (n,) in cur.fetchall()}

    depts = {clean(r["Department Code"]) or "Unassigned" for r in rows}
    ensure_departments(cur, plant_id, depts)
    area_of = lambda r: f"{clean(r['Department Code']) or 'Unassigned'} / {clean(r['Area']) or 'General'}"
    areas = ensure_areas(cur, plant_id, {area_of(r) for r in rows})

    cur.execute('SELECT "categoryCode", "subCategoryCode", "observationType" FROM "ObservationTaxonomy"')
    taxonomy = set(cur.fetchall())
    try:
        tags = {c["id"]: c.get("tags", []) for c in json.load(open(LIBRARY, encoding="utf-8"))["cases"]}
    except Exception:  # library is optional; defaults are used without it
        tags = {}

    out, skipped = [], 0
    for r, number in zip(rows, numbers):
        if number in existing:
            skipped += 1
            continue
        code = str(r["Inspection Code"]).strip()
        typ, axis = TYPE_FOR.get(clean(r["Observation Type"]), ("UNSAFE_CONDITION", "CONDITION"))
        cat_code = clean(r["Category Code"]) or ""
        cat, sub = None, None
        for cand in stop_candidates(cat_code, tags.get(code, [])):
            if (cand["categoryCode"], cand["subCategoryCode"], axis) in taxonomy:
                cat, sub = cand["categoryCode"], cand["subCategoryCode"]
                break
        fatal = clean(r["Severity"]) == "Fatal Potential"
        level = LEVEL_FOR.get(clean(r["Risk Level"]), "LOW")
        severity = "CRITICAL" if fatal else level
        created = r["Created Date"] if isinstance(r["Created Date"], dt.datetime) else NOW
        closed = clean(r["Status"]) == "Closed"
        closed_at = r["Closed Date"] if isinstance(r["Closed Date"], dt.datetime) else None
        details = [
            f"Inspection code: {code}",
            f"Category: {cat_code} {clean(r['Category']) or ''}".strip(),
            f"Observer: {person(r['Observer Name'], r['Employee ID'], ', '.join(x for x in (clean(r['Observer Designation']), clean(r['Observer Department'])) if x))}",
            f"Location: {' / '.join(x for x in (clean(r['Area']), clean(r['Section']), clean(r['Location'])) if x)}",
            f"Area I/C: {person(r['Area I/C Name'], r['Area I/C Emp ID'])}" if clean(r["Area I/C Name"]) else None,
            f"Task executor: {person(r['Task Executor Name'], r['Task Executor Emp ID'], clean(r['Task Executor Function']))}" if clean(r["Task Executor Name"]) else None,
            f"Function: {clean(r['Function'])}" if clean(r["Function"]) else None,
            f"Severity (sheet): {clean(r['Severity'])}" if fatal else None,
            f"Special category: {clean(r['Special Category'])}" if clean(r["Special Category"]) not in (None, "None") else None,
            f"Impacted: {person(r['Individual Name'], extra=clean(r['Impacted Individual']))}" if clean(r["Impacted Individual"]) else None,
        ]
        remarks = [x for x in (clean(r.get("Closing Remarks")),
                               f"Final review: {clean(r.get('Final Remarks'))}" if clean(r.get("Final Remarks")) else None) if x]
        out.append(dict(
            id=new_id(), number=number, observerId=user_id, date=created, plantId=plant_id,
            areaId=areas[area_of(r)], type=typ, category=cat or "OTHERS",
            categoryCode=cat, subCategoryCode=sub, taxonomyAxis=axis if cat else None,
            description=(clean(r["Observation"]) or "(no text)") + "\n\n" + "\n".join(d for d in details if d),
            severity=severity, riskLevel=severity,
            immediateAction=clean(r["Action Plan"]),
            targetDate=r["Target Date"] if isinstance(r["Target Date"], dt.datetime) else None,
            status="CLOSED" if closed else "OPEN",
            closedAt=closed_at if closed else None,
            closingRemark="\n".join(remarks) or None,
            createdAt=created, updatedAt=closed_at or created,
        ))
    insert_many(cur, "Observation", out)
    print(f"  + {len(out)} observations ({skipped} already present)")
    if out:
        print(f"    status: {dict(Counter(o['status'] for o in out))}")
        print(f"    severity: {dict(Counter(o['severity'] for o in out))}")
        print(f"    category: {dict(Counter(o['category'] for o in out))}")


def grant_access(cur, plant_id, emails):
    """Give plant-scoped users (e.g. an NW HSE manager) read access to the JSL plant,
    the same way cross-plant access is granted elsewhere: an extra PLANT-scoped UserRole."""
    for email in emails:
        cur.execute('SELECT ur."userId", ur."roleId" FROM "UserRole" ur JOIN "User" u ON u.id=ur."userId" '
                    'WHERE u.email=%s AND ur."scopeType"=%s', (email, "PLANT"))
        roles = {rid: uid for uid, rid in cur.fetchall()}
        if not roles:
            print(f"  ! {email}: no plant-scoped role found, skipped")
            continue
        for role_id, user_id in roles.items():
            if one(cur, 'SELECT id FROM "UserRole" WHERE "userId"=%s AND "roleId"=%s AND "scopeType"=%s '
                        'AND "scopeValue"=%s', user_id, role_id, "PLANT", plant_id):
                continue
            cur.execute('INSERT INTO "UserRole" (id, "userId", "roleId", "scopeType", "scopeValue") '
                        'VALUES (%s,%s,%s,%s,%s)', (new_id(), user_id, role_id, "PLANT", plant_id))
        print(f"  + {email}: access to {PLANT['code']}")


def rollback(cur):
    pid = one(cur, 'SELECT id FROM "Plant" WHERE code=%s', PLANT["code"])
    if not pid:
        print("  nothing to remove")
        return
    cur.execute('DELETE FROM "UserRole" WHERE "scopeType"=%s AND "scopeValue"=%s', ("PLANT", pid))
    print(f"  - {cur.rowcount} plant access grants")
    cur.execute('DELETE FROM "HiraStudy" WHERE "plantId"=%s', (pid,))
    print(f"  - {cur.rowcount} HIRA studies (entries, hazards, controls cascade)")
    cur.execute('DELETE FROM "Observation" WHERE "plantId"=%s', (pid,))
    print(f"  - {cur.rowcount} observations")
    cur.execute('DELETE FROM "User" WHERE email=%s', (IMPORT_USER_EMAIL,))
    cur.execute('DELETE FROM "Area" WHERE "plantId"=%s', (pid,))
    print(f"  - {cur.rowcount} areas")
    cur.execute('DELETE FROM "Department" WHERE "plantId"=%s', (pid,))
    print(f"  - {cur.rowcount} departments")
    cur.execute('DELETE FROM "Plant" WHERE id=%s', (pid,))
    print("  - plant")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hira", help="HIRA workbook (QSD/24/F + Controls sheet)")
    ap.add_argument("--observations", help="Safety observation register workbook")
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    ap.add_argument("--rollback", action="store_true", help="remove everything this script imported")
    ap.add_argument("--replace-hira", action="store_true", help="delete and rebuild the imported HIRA study")
    ap.add_argument("--grant", nargs="+", metavar="EMAIL", default=[],
                    help="give these plant-scoped users access to the JSL plant")
    a = ap.parse_args()
    if not (a.rollback or a.hira or a.observations or a.grant):
        ap.error("give --hira, --observations, --grant, or --rollback")

    conn = connect()
    try:
        with conn.cursor() as cur:
            if a.rollback:
                rollback(cur)
            else:
                plant_id = ensure_plant(cur)
                user_id = ensure_import_user(cur, plant_id)
                if a.hira:
                    import_hira(cur, a.hira, plant_id, user_id, replace=a.replace_hira)
                if a.observations:
                    import_observations(cur, a.observations, plant_id, user_id)
                if a.grant:
                    grant_access(cur, plant_id, a.grant)
        if a.apply:
            conn.commit()
            print("Committed.")
        else:
            conn.rollback()
            print("Dry run: rolled back. Re-run with --apply to save.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
