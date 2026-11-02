"""Import the JSL Hisar client workbooks into the SafeOps database.

    python scripts/import_jsl_hisar.py --hira HIRA_SMS-1.xlsx --observations "Safety Observation ....xlsx"
    python scripts/import_jsl_hisar.py ... --apply                 # commit (default: dry run that rolls back)
    python scripts/import_jsl_hisar.py --hira ... --replace-hira --apply          # rebuild the HIRA studies
    python scripts/import_jsl_hisar.py --observations ... --replace-observations --apply
    python scripts/import_jsl_hisar.py --rollback --apply          # remove everything this script created

Everything is written under a dedicated plant (code JSL-HSR).

HIRA workbook (tab "HIRA " = form QSD/24/F, tab "Controls" = form QSD/25/F)
  * Every area heading in the HIRA tab is its own HIRA study. Two blocks have no
    heading: rows where S.No restarts after AFS ("All Activities") form a
    "General (All Activities)" study; "COVID-19 Activities" is headed in column B.
  * One HiraEntry per S.No (S.No restarts at 1 in every area). A row with no S.No
    of its own is a merged cell of the S.No above (an extra hazard of the same
    activity); a repeated S.No is the same activity. Such rows become extra
    hazards of one entry. Missing numbers create no entry.
  * Rating: the plant's own method, GT = SP x (LH x CN) x DE, not acceptable when
    GT > 3.33 (the sheet's "significant score") or any LC/DC/CE/EC concern is Y.
    The studies use a plant-specific 5x5 matrix ("JSL_QSD24_GT"): likelihood =
    SP x LH x DE (1-4 in this workbook), severity = CN, so the stored risk score
    equals the sheet's GT for every row, and levels above GT 3 are not acceptable.
    An entry is rated by its worst row (highest GT, then CN). The workbook gives
    one rating with the existing controls in place and no separate residual, so
    the residual is saved as the same rating (manual) with the sheet's Yes/No.
  * Hazards: one line per sheet row (a combined text such as "Gravity hazard /
    Burn & scald Hazard" gets one line per hazard), text kept verbatim, with the
    closest SafeOps hazard master chosen from hazard + activity + consequence.
  * Controls: the HIRA tab's "Control for OH&S Risk" (SOP numbers as
    administrative; physical measures as engineering; gloves/masks as PPE) and
    every non-empty cell of the matching QSD/25/F row, by its column. QSD/25/F
    refers to rows by S.No "as in QSD/24/F", but several of its numbers are off,
    so rows are matched on S.No + activity + hazard (preferring the
    not-acceptable hazard, since the form is the strategy for unacceptable risks).
  * Kept as well: previous GT (column R, "*" = first assessed in this revision),
    QSD/25/F wording that differs from QSD/24/F, legal references (verbatim per
    hazard + normalised references), the HIRA team (leader and members as people),
    form numbers/revisions, abbreviations, footnote, unsigned approval block and
    workbook provenance.
  * The sheet values per entry are also written to
    app/services/copilot/knowledge/data/jsl_hira_source.json for the copilot review.

Observation register ("Safety Reports", 37 columns)
  * One Observation per row, numbered JSL-<Inspection Code>; text kept exactly
    (line breaks included); the observer and other people go into the description
    (one import user owns the records).
  * Status: Closed -> CLOSED (closedAt = Closed Date); Pending with a submitted
    closure and no final review yet -> IN_PROGRESS; other Pending -> OPEN.
  * Severity CRITICAL for "Fatal Potential", otherwise the Risk Level; riskLevel
    is always the sheet's Risk Level.
  * Closing remarks, final review date/remarks, approval, closure-rejection,
    exceptional and rejection remarks all go into closingRemark (dates in IST).
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
TEAM_EMAIL = "jsl.hsr.{}@safeops360.in"
STUDY_PREFIX = "HIRA-JSL-SMS1-"
OBS_PREFIX = "JSL-"
MATRIX_CODE = "JSL_QSD24_GT"
KNOWLEDGE = BACKEND / "app/services/copilot/knowledge/data"
LIBRARY = KNOWLEDGE / "observation_library.json"
HIRA_LIBRARY = KNOWLEDGE / "hira_library.json"
HIRA_SOURCE = KNOWLEDGE / "jsl_hira_source.json"
SIGNIFICANT = 3.33  # sheet cell Q18 = 3.3313 (average GT); GT values are integers, so GT >= 4 is significant

# Area heading (first two words) → study code.
STUDY_CODES = {"charging area": "CHG", "electric arc": "EAF", "aod area": "AOD", "ladle refining": "LRF",
               "vacuum degassing": "VD", "ladle area": "LADLE", "alloy feeding": "AFS", "general all": "GEN",
               "covid activities": "COVID"}
# HIRA area → the name the observation register uses for the same physical area
# in department SMS-1 (so HIRA and observations share one Area record).
AREA_ALIAS = {"electric arc": "EAF-1", "aod area": "AOD", "ladle refining": "LRF", "vacuum degassing": "VD Area",
              "alloy feeding": "FAFS"}

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)  # DB columns are naive UTC
IST = dt.timedelta(hours=5, minutes=30)


def new_id() -> str:
    return uuid.uuid4().hex


def clean(v):
    """Whitespace-collapsed text: for codes, keys and matching only."""
    if isinstance(v, str):
        v = " ".join(v.split())
        return v or None
    return v


def exact(v):
    """Text as the client wrote it: only line endings normalised and the ends trimmed."""
    if isinstance(v, str):
        v = v.replace("\r\n", "\n").replace("\r", "\n").strip()
        return v or None
    return v


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def ist(d: dt.datetime) -> str:
    return (d + IST).strftime("%d.%m.%Y %H:%M IST")


# ─────────────────────────────────────────────────────────────────────
# HIRA workbook (QSD/24/F + QSD/25/F)
# ─────────────────────────────────────────────────────────────────────

def grid(ws):
    """Cell values with merged ranges filled in (what Excel shows), plus the raw values."""
    raw = {}
    for row in ws.iter_rows():
        for c in row:
            raw[(c.row, c.column)] = c.value
    g = dict(raw)
    for mr in ws.merged_cells.ranges:
        v = raw.get((mr.min_row, mr.min_col))
        for r in range(mr.min_row, mr.max_row + 1):
            for cc in range(mr.min_col, mr.max_col + 1):
                g[(r, cc)] = v
    return g, raw, ws.max_row


def section_key(name: str) -> str:
    words = re.sub(r"[^a-z ]", " ", (name or "").lower()).split()
    return " ".join(words[:2])


def header_value(ws, prefix: str) -> str | None:
    for row in ws.iter_rows(min_row=1, max_row=16, values_only=True):
        for v in row:
            if isinstance(v, str) and v.strip().lower().startswith(prefix.lower()):
                return exact(v.split(":", 1)[1]) if ":" in v else exact(v)
    return None


def find_text(ws, pattern: str, max_row: int | None = None) -> str | None:
    for row in ws.iter_rows(min_row=1, max_row=max_row or ws.max_row, values_only=True):
        for v in row:
            if isinstance(v, str) and re.search(pattern, v, re.I):
                return clean(v)
    return None


def parse_hira(path: str):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = next(w for w in wb.worksheets if w.title.strip().upper() == "HIRA")
    cws = wb["Controls"]
    props = wb.properties
    meta = {
        "dept": header_value(ws, "Location/Dept"),
        "team": header_value(ws, "HIRA Team Members"),
        "non_managerial": header_value(ws, "Non Managerial Workers"),
        "leader": header_value(ws, "Team Leader"),
        "form24": find_text(ws, r"^Format No", 3),
        "form25": find_text(cws, r"^\s*Format No", 3),
        "abbreviations": find_text(ws, r"^Abbreviations", 16),
        "footnote": find_text(ws, r"^\* Not to be filled"),
        "approval24": "; ".join(t for t in (find_text(ws, r"Sectional HOD"), find_text(ws, r"HOD Safety")) if t),
        "file": Path(path).name,
        "author": props.creator, "lastModifiedBy": props.lastModifiedBy,
        "created": props.created.isoformat() if props.created else None,
        "modified": props.modified.isoformat() if props.modified else None,
    }
    g, raw, mx = grid(ws)
    for r in range(1, 16):
        for c in range(1, 24):
            if g.get((r, c)) == "Revision No.":
                meta["revision"] = g.get((r, c + 1))
            if g.get((r, c)) == "Date:":
                meta["date"] = g.get((r, c + 1))
    meta["significantScoreCell"] = g.get((18, 17))

    rows, section, prev = [], None, None
    last_sno = 0
    for r in range(17, mx + 1):
        if not any(raw.get((r, c)) not in (None, "") for c in range(1, 21)):
            continue  # empty, or only the lower half of cells merged with the row above
        v = [exact(g.get((r, c))) for c in range(1, 21)]
        a, b, hazard = v[0], v[1], v[3]
        if isinstance(a, str) and (a.startswith("*") or a.lower().startswith("approved")):
            break
        if isinstance(a, str) and num(a) is None and all(x in (None, a) for x in v[1:6]):
            section, last_sno = a, 0
            continue
        if a is None and isinstance(b, str) and hazard is None and v[10] is None:
            section, last_sno = b, 0  # e.g. "COVID-19 Activities", headed in column B
            continue
        if hazard is None and b is None and v[10] is None:
            continue
        sno = num(a)
        if sno == 1 and last_sno > 1 and section and not section.startswith("General"):
            section = "General (All Activities)"  # unlabelled block where S.No restarts
        if sno is None and prev:
            sno = prev["sno"]
        last_sno = sno or last_sno
        activity = b or (prev["activity"] if prev else None)
        if hazard is None and prev:  # continuation row: same hazard, re-scored
            hazard, v[4], v[5] = prev["hazard"], v[4] or prev["risk"], v[5] or prev["control_ref"]
        row = dict(xl_row=r, section=section, sno=sno, activity=activity,
                   rnr=(v[2] or (prev["rnr"] if prev else "R")).strip().upper(), rnr_blank=v[2] is None,
                   hazard=hazard, risk=v[4], control_ref=v[5],
                   LC=clean(v[6]), DC=clean(v[7]), CE=clean(v[8]), EC=clean(v[9]),
                   SP=num(v[10]), LH=num(v[11]), CN=num(v[12]), SV=num(v[13]), DE=num(v[14]), GT=num(v[15]),
                   prev_gt=v[17], acceptable=clean(v[18]), legal=v[19])
        if row["LH"] is None or row["CN"] is None:
            continue
        rows.append(row)
        prev = row

    g, raw, mx = grid(cws)
    cols = ["elimination", "tech_modification", "procedure", "engineering", "signage",
            "administrative", "competence", "pip", "ppe"]
    # The Controls tab has headings only for the plant areas; the General and
    # COVID blocks follow AFS with S.No restarting. A drop in S.No therefore
    # moves to the next HIRA area, in the HIRA tab's order.
    order = list(dict.fromkeys(r["section"] for r in rows))
    controls, section, prev, last_sno = [], None, None, 0
    meta["approval25"] = "; ".join(t for t in (find_text(cws, r"^1\)\s*HOD"), find_text(cws, r"^Approved By: HOD")) if t)
    for r in range(10, mx + 1):  # rows 1-9 are the form header
        if not any(raw.get((r, k)) not in (None, "") for k in range(1, 14)):
            continue  # empty, or a row that is entirely the lower half of merged cells (e.g. R178)
        v = [exact(g.get((r, k))) for k in range(1, 14)]
        a = v[0]
        if isinstance(a, str) and (a.lower().startswith("approved") or a.startswith("1)")):
            break
        if isinstance(a, str) and num(a) is None and all(x in (None, a) for x in v[1:5]):
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
        d = dict(xl_row=r, section=section, sno=sno, sno_in_sheet=num(a),
                 activity=v[1] or (prev["activity"] if prev else None), activity_own=v[1],
                 hazard=v[2] or (prev["hazard"] if prev else None), hazard_own=v[2], risk=v[3])
        d.update(zip(cols, v[4:13]))
        controls.append(d)
        prev = d
    return meta, rows, controls


# ── hazard classification ─────────────────────────────────────────────

MOLTEN = (r"liquid metal|molten|hot metal|slag|ladle|tapping|tap hole|teeming|sample|sampling|pour|furnace|\beaf\b|"
          r"\baod\b|\blrf\b|lancing|lance|temperature|\btip\b|\bheats?\b|tilting|radex|ferro|addition|arcing|"
          r"electrode|spout|porous|nozzle|slide gate|cpc|al-?mix|dolomite|refining|degass|\bvd\b|purging|burning of gases")
VEHICLE = r"truck|payloader|bobcat|hippo|vehicle|vehical|loader|dumper"
CRANE = r"crane|hook|wire rope|\brope\b|sling|hoist|lifting|\blift\b|lowering|electrode column"


def classify_one(hazard: str, activity: str | None, risk: str | None) -> str:
    h, a, rk = (hazard or "").lower(), (activity or "").lower(), (risk or "").lower()
    both = f"{a} {rk}"
    if re.search(r"covid|contaminat|infect|medical waste|virus", f"{h} {rk}") or "animal" in h:
        return "BIO_INFECTIOUS_AGENT"  # the library has no animal-bite master; nearest is biological
    if "sexual" in h:
        return "PSYCH_HARASSMENT"
    if "working hours" in h:
        return "PSYCH_FATIGUE"
    if "workload" in h:
        return "PSYCH_WORKLOAD"
    if re.search(r"behaviour|behavior|mobile phone|communication", h):
        return "BEHAV_SHORTCUTS"
    if "electric" in h:
        return "ELEC-001"
    if re.search(r"steam explosion|puncturing|slag may splash|splash", h):
        return "FIRE_MOLTEN_METAL"
    if "explosion" in h:
        if re.search(r"burner|\boil\b|\bgas\b", both):
            return "FIRE_LPG_LEAK"
        return "FIRE_MOLTEN_METAL" if re.search(MOLTEN, both) else "FIRE-001"
    if re.search(r"fire|hazrad", h):
        return "FIRE-001"
    if re.search(r"burn|scald", h):
        return "FIRE_MOLTEN_METAL" if re.search(MOLTEN, both) else "THERMAL_HOT_SURFACE"
    if re.search(r"radiation|radition", h):
        return "RADIATION_UV"  # eye injury from radiation; the library has no infrared master
    if "heat stress" in h:
        return "ENV_HEAT_STRESS"
    if "vibration" in h:
        return "MECH_VIBRATION"
    if "pressure" in h:
        if re.search(r"vd operation|vacuum", both):
            return "MECH-015"
        if re.search(r"\bgun|gunning|hammer", both):
            return "MECH-012"
        return "MECH-004"
    if re.search(r"slip|trip", h):
        return "PHYS_SLIP_TRIP"
    if re.search(r"vehic|truck|horn|parking", h):
        return "MECH_MOBILE_EQUIPMENT" if re.search(r"payloader|bobcat|hippo|loader", both) else "VEH-003"
    if "crush" in h:
        return "MECH_PINCH_POINT"
    if re.search(r"equipment|equipmental", h):
        if re.search(CRANE, both):
            return "MECH-008"
        if re.search(VEHICLE, both):
            return "VEH-003"
        if re.search(r"trolley|transfer car", both):
            return "MECH_MOBILE_EQUIPMENT"
        return "MECH-007"
    if "gravity" in h:
        return "HEIGHT_FALL_OVER_2M" if re.search(r"fall from|from height|platform", rk) else "MECH_FALLING_OBJECT"
    if "ergonomic" in h:
        return "MECH_HEAVY_LIFTING" if re.search(r"heavy|load|lift", f"{rk} {a}") else "ERGO_AWKWARD_POSTURE"
    if re.search(r"environment|respiratory|dust|inhal", h):
        return "NOISE_HIGH_EXPOSURE" if "noise" in rk else "ENV_DUST_AIRBORNE"
    if re.search(r"machin|tool", h):
        if re.search(r"\bbelt|conveyor", both):
            return "MECH-001"
        if re.search(r"knife|cutting|opening of the bucket", both):
            return "MECH_SHARP_EDGE"
        if re.search(VEHICLE, both):
            return "MECH_MOBILE_EQUIPMENT"
        if re.search(CRANE, both):
            return "MECH-008"
        if "trolley" in both:
            return "MECH_MOBILE_EQUIPMENT"
        if re.search(r"door|roof|hood|dog house", both):
            return "MECH_PINCH_POINT"
        if re.search(r"hammer|brick", both):
            return "MECH_PROJECTILE"
        return "MECH_UNGUARDED_ROTATING"
    return "MECH_UNGUARDED_ROTATING"


def classify(hazard: str, activity: str | None, risk: str | None) -> list[str]:
    """Master code(s) for one sheet hazard cell. "Gravity hazard / Burn & scald
    Hazard" names two hazards and gets two; "Machinery/tool hazard" is one."""
    parts = [p for p in re.split(r"\s*/\s*", hazard or "") if p.strip()]
    if len(parts) > 1 and all(re.search(r"hazard|fire|burn|scald|gravity", p, re.I) for p in parts):
        codes = list(dict.fromkeys(classify_one(p, activity, risk) for p in parts))
        if len(codes) > 1:
            return codes
    return [classify_one(hazard, activity, risk)]


MASTER_TYPE = {  # master → hazard type used by the copilot's control ideas
    "THERMAL_HOT_SURFACE": "BURN_SCALD", "FIRE-001": "FIRE", "FIRE_LPG_LEAK": "FIRE_EXPLOSION",
    "MECH-008": "EQUIPMENT_FAILURE", "MECH-007": "EQUIPMENT_FAILURE", "VEH-003": "VEHICULAR",
    "MECH_UNGUARDED_ROTATING": "MACHINERY_TOOL", "MECH-001": "MACHINERY_TOOL", "MECH_SHARP_EDGE": "MACHINERY_TOOL",
    "MECH_PROJECTILE": "MACHINERY_TOOL", "MECH_FALLING_OBJECT": "GRAVITY_FALLING_OBJECT",
    "HEIGHT_FALL_OVER_2M": "GRAVITY_FALLING_OBJECT", "MECH_HEAVY_LIFTING": "ERGONOMIC", "ERGO_AWKWARD_POSTURE": "ERGONOMIC",
    "ENV_DUST_AIRBORNE": "DUST_FUME", "NOISE_HIGH_EXPOSURE": "DUST_FUME", "ENV_HEAT_STRESS": "HEAT_STRESS",
    "RADIATION_UV": "RADIATION", "MECH-004": "PRESSURE", "MECH-012": "PRESSURE", "MECH-015": "PRESSURE",
    "MECH_VIBRATION": "VIBRATION", "PHYS_SLIP_TRIP": "SLIP_TRIP", "ELEC-001": "ELECTRICAL",
    "BIO_INFECTIOUS_AGENT": "BIOLOGICAL", "PSYCH_HARASSMENT": "PSYCHOSOCIAL", "PSYCH_FATIGUE": "PSYCHOSOCIAL",
    "PSYCH_WORKLOAD": "PSYCHOSOCIAL",
}


def hazard_type(code: str, hazard: str) -> str:
    h = (hazard or "").lower()
    if code == "FIRE_MOLTEN_METAL":
        return "STEAM_EXPLOSION" if "steam" in h else "MOLTEN_METAL_BREAKOUT" if "punctur" in h else "BURN_SCALD"
    if code == "MECH_MOBILE_EQUIPMENT":
        return "EQUIPMENT_FAILURE" if "equipment" in h else "VEHICULAR"
    if code == "MECH_PINCH_POINT":
        return "CRUSHING" if "crush" in h else "MACHINERY_TOOL"
    if code == "BEHAV_SHORTCUTS":
        return "COMMUNICATION" if "communication" in h else "DISTRACTION" if "mobile" in h else "PSYCHOSOCIAL"
    return MASTER_TYPE.get(code, "OTHER")


# ── controls ──────────────────────────────────────────────────────────

CONTROL_COLUMNS = [  # (QSD/25/F column, hierarchy, label) — the form's own columns
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


def f_hierarchy(text: str) -> str:
    """Hierarchy of a free-text "Control for OH&S Risk" cell (QSD/24/F column F)."""
    t = text.lower()
    if DOC_REF.search(text) or re.search(r"\bsop\b|document|guideline", t):
        return "ADMINISTRATIVE"
    if re.search(r"glove|mask|helmet|goggle|shoe|\bppe\b", t) and not re.search(
            r"distanc|sanitiz|scan|training|policy|bins?\b", t):
        return "PPE"
    if re.search(r"install|brake|hooter|flasher|railing|guard|interlock|\bbins?\b|cabin|shield|illumination|stairs", t):
        return "ENGINEERING"
    return "ADMINISTRATIVE"


# ── legal references ──────────────────────────────────────────────────

def legal_refs(text: str | None) -> list[tuple[str, str | None]]:
    """Normalised (regulation, section) pairs for one column-T cell."""
    if not text:
        return []
    low = text.lower()
    out: list[tuple[str, str | None]] = []
    if re.search(r"\bf\.?\s*a\b|factories act", low):
        m = re.search(r"section\s*(\d+)\s*&\s*(\d+)", low)
        secs = [m.group(1), m.group(2)] if m else re.findall(r"sec(?:tion)?\.?\s*-?\s*(\d+)", low)
        out += [("Factories Act, 1948", f"Sec. {s}") for s in secs] or [("Factories Act, 1948", None)]
    if "cmv" in low:
        out.append(("Central Motor Vehicles Rules, 1989", None))
    if "air act" in low:
        m = re.search(r"sec\.?\s*-?\s*(\d+)", low)
        out.append(("Air (Prevention and Control of Pollution) Act, 1981", f"Sec. {m.group(1)}" if m else None))
    if "noise pollution" in low:
        out.append(("Noise Pollution (Regulation and Control) Rules, 2000", "under Sec. 3 of the Environment (Protection) Act, 1986"))
    if "epidemic" in low:
        m = re.search(r"section\s*(\d+)", low)
        out.append(("Epidemic Diseases Act, 1897", f"Sec. {m.group(1)}" if m else None))
    if "sexual" in low:
        out.append(("Sexual Harassment of Women at Workplace (Prevention, Prohibition and Redressal) Act, 2013", None))
    return out or [(clean(text), None)]


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
    name = exact(name)
    if not name:
        return None
    bits = [b for b in (extra, f"Emp {emp}" if emp not in (None, "") else None) if b]
    return f"{name} ({', '.join(bits)})" if bits else name


def text_value(v):
    """A remark worth keeping: not blank and not just a placeholder like '-'."""
    v = exact(v if not isinstance(v, (int, float)) else str(v))
    return v if v and not re.fullmatch(r"[\W_]*", v) else None


# ─────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────

def connect():
    env = dotenv_values(BACKEND / ".env")
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", env["DATABASE_URL_SYNC"])
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


def drop_unused_areas(cur, plant_id) -> None:
    """Remove areas of this plant that nothing references any more (e.g. the
    HIRA-only names replaced by the register's names)."""
    cur.execute('SELECT id, name FROM "Area" a WHERE a."plantId"=%s '
                'AND NOT EXISTS (SELECT 1 FROM "Observation" o WHERE o."areaId"=a.id) '
                'AND NOT EXISTS (SELECT 1 FROM "HiraStudy" s WHERE s."areaId"=a.id) '
                'AND NOT EXISTS (SELECT 1 FROM "HiraEntry" e WHERE e."areaId"=a.id)', (plant_id,))
    dropped = []
    for aid, name in cur.fetchall():
        cur.execute("SAVEPOINT drop_area")
        try:
            cur.execute('DELETE FROM "Area" WHERE id=%s', (aid,))
            cur.execute("RELEASE SAVEPOINT drop_area")
            dropped.append(name)
        except psycopg2.Error:  # referenced by some other module; keep it
            cur.execute("ROLLBACK TO SAVEPOINT drop_area")
    if dropped:
        print(f"  - {len(dropped)} unused areas: {', '.join(dropped)}")


def _password() -> str:
    # Random unknown password: these accounts hold records, they are not for logging in.
    return bcrypt.hashpw(secrets.token_urlsafe(32).encode(), bcrypt.gensalt(10)).decode().replace("$2b$", "$2a$", 1)


def ensure_import_user(cur, plant_id) -> str:
    uid = one(cur, 'SELECT id FROM "User" WHERE email=%s', IMPORT_USER_EMAIL)
    if uid:
        return uid
    uid = new_id()
    cur.execute('INSERT INTO "User" (id, email, name, "passwordHash", role, "plantId", designation, department) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (uid, IMPORT_USER_EMAIL, "JSL Hisar (Excel import)", _password(), "HSE_MANAGER", plant_id,
                 "Data import", "HSE"))
    print(f"  + import user {IMPORT_USER_EMAIL}")
    return uid


def ensure_person(cur, plant_id, name: str, designation: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")
    email = TEAM_EMAIL.format(slug)
    uid = one(cur, 'SELECT id FROM "User" WHERE email=%s', email)
    if uid:
        return uid
    uid = new_id()
    cur.execute('INSERT INTO "User" (id, email, name, "passwordHash", role, "plantId", designation, department) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                (uid, email, name, _password(), "WORKER", plant_id, designation, "SMS-1"))
    return uid


def ensure_matrix(cur) -> str:
    """The plant's QSD/24/F scoring as a SafeOps matrix: likelihood = SP x LH x DE,
    severity = CN, score = GT. GT <= 3 is acceptable (below the significant score
    3.33); anything higher is significant. The level names above LOW are SafeOps
    bands by GT (the plant only distinguishes acceptable / significant)."""
    mid = one(cur, 'SELECT id FROM "RiskMatrix" WHERE code=%s', MATRIX_CODE)
    if mid:
        return mid
    mid = new_id()
    insert_many(cur, "RiskMatrix", [dict(
        id=mid, code=MATRIX_CODE, name="JSL QSD/24/F (GT = SP x LH x CN x DE)",
        description="Plant HIRA method of JSL Hisar (form QSD/24/F): likelihood axis = SP (spread) x LH (likelihood) "
                    "x DE (detectability); severity axis = CN (consequence); score = GT. GT above the significant "
                    "score 3.33, or any legal/domino/chronic/emergency concern = Y, is not acceptable and needs a "
                    "QSD/25/F control strategy.",
        likelihoodLevels=5, severityLevels=5,
        acceptableResidual={"routine": "LOW", "non_routine": "LOW", "emergency": "LOW"},
        controlHierarchyEnforced=True, isActive=True, isDefault=False, isGlobal=False,
        createdAt=NOW, updatedAt=NOW,
        alarpBands={"LOW": "BROADLY_ACCEPTABLE", "MODERATE": "TOLERABLE", "HIGH": "UNACCEPTABLE", "CRITICAL": "UNACCEPTABLE"},
    )])
    insert_many(cur, "RiskMatrixLikelihood", [dict(
        id=new_id(), matrixId=mid, score=n, label=f"SP x LH x DE = {n}",
        description="Plant index: spread x likelihood x detectability (QSD/24/F)", frequencyGuidance=None, sortOrder=n - 1)
        for n in range(1, 6)])
    insert_many(cur, "RiskMatrixSeverity", [dict(
        id=new_id(), matrixId=mid, score=n, label=f"CN {n}", description="Consequence as rated in QSD/24/F",
        sortOrder=n - 1) for n in range(1, 6)])
    bands = [(3, "LOW", "#10B981", "Acceptable: GT within the significant score 3.33 (unless a concern is Y)", 365),
             (6, "MODERATE", "#F59E0B", "Significant (GT > 3.33): control strategy required in QSD/25/F", 90),
             (12, "HIGH", "#F97316", "Significant (GT > 3.33): control strategy required in QSD/25/F", 30),
             (25, "CRITICAL", "#EF4444", "Significant (GT > 3.33): control strategy required in QSD/25/F", 7)]
    cells = []
    for l in range(1, 6):
        for s in range(1, 6):
            score = l * s
            _, level, color, action, days = next(b for b in bands if score <= b[0])
            cells.append(dict(id=new_id(), matrixId=mid, likelihoodScore=l, severityScore=s, riskScore=score,
                              riskLevel=level, colorHex=color, actionRequired=action, responseTimeDays=days))
    insert_many(cur, "RiskMatrixCell", cells)
    print(f"  + risk matrix {MATRIX_CODE}")
    return mid


# ─────────────────────────────────────────────────────────────────────
# Import steps
# ─────────────────────────────────────────────────────────────────────

STOP = {"of", "the", "and", "with", "to", "in", "at", "by", "on", "a", "for", "from", "into", "hazard", "may", "be",
        "get", "person", "help", "&", "/", "or", "is", "due"}


def words(s: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 1}


def sim(a: str | None, b: str | None) -> float:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def plant_rule(row: dict) -> bool:
    """The sheet's own rule: not acceptable when GT > 3.33 or any LC/DC/CE/EC is Y."""
    concerns = any((row[k] or "").upper() == "Y" for k in ("LC", "DC", "CE", "EC"))
    return not (concerns or (row["GT"] or 0) > SIGNIFICANT)


def not_acceptable(row: dict) -> bool:
    said = (row["acceptable"] or "").lower()
    return said.startswith("n") or not row["acceptableByRule"]


def match_control(c: dict, rows: list[dict]) -> tuple[dict | None, float]:
    """Best HIRA row in the same area for a Controls-tab row: S.No first, but
    activity and hazard must agree, because some S.No in that tab are off. On a
    tie the not-acceptable hazard wins: the tab is the strategy for unacceptable risks."""
    best, best_score = None, -1.0
    for r in rows:
        s = 3 * sim(c["activity"], r["activity"]) + 3 * sim(c["hazard"], r["hazard"]) + sim(c["risk"], r["risk"])
        if c["sno"] is not None and r["sno"] is not None:
            d = abs(c["sno"] - r["sno"])
            s += 2.5 if d == 0 else 1.0 if d <= 2 else 0
        if not_acceptable(r):
            s += 0.5
        if s > best_score:
            best, best_score = r, s
    return best, best_score


def split_names(text: str | None) -> list[str]:
    return [exact(n) for n in re.split(r",", text or "") if exact(n)]


def import_hira(cur, path, plant_id, user_id, replace=False):
    cur.execute('SELECT number FROM "HiraStudy" WHERE "plantId"=%s', (plant_id,))
    existing = [n for (n,) in cur.fetchall()]
    if existing:
        if not replace:
            print(f"  = {len(existing)} HIRA studies already exist, skipped (use --replace-hira to rebuild)")
            return None
        cur.execute('DELETE FROM "HiraStudy" WHERE "plantId"=%s', (plant_id,))
        print(f"  - removed {cur.rowcount} existing HIRA studies")

    meta, rows, controls = parse_hira(path)
    dept_code = "SMS-1"
    dept_id = ensure_departments(cur, plant_id, [dept_code])[dept_code]
    sections = list(dict.fromkeys(r["section"] for r in rows))
    area_name = {s: f"{dept_code} / {AREA_ALIAS.get(section_key(s), s)}" for s in sections}
    areas = ensure_areas(cur, plant_id, area_name.values())

    matrix_id = ensure_matrix(cur)
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
        lib = {e["ref"]: e for e in json.load(open(HIRA_LIBRARY, encoding="utf-8"))["entries"]}
    except Exception:
        lib = {}
    try:
        eff = dt.datetime.strptime(str(meta.get("date")), "%d.%m.%Y")
    except ValueError:
        eff = NOW

    # ── the HIRA team as people ─────────────────────────────────────────
    leader_name = meta.get("leader")
    leader_id = ensure_person(cur, plant_id, leader_name, "HIRA team leader (SMS-1)") if leader_name else user_id
    managerial = [(n, ensure_person(cur, plant_id, n, "HIRA team member (managerial), SMS-1"))
                  for n in split_names(meta.get("team"))]
    workers = [(n, ensure_person(cur, plant_id, n, "HIRA team member (non-managerial), SMS-1"))
               for n in split_names(meta.get("non_managerial"))]

    # ── group sheet rows: area → S.No → rows ────────────────────────────
    by_sec: dict[str, dict[int, list[dict]]] = {s: defaultdict(list) for s in sections}
    for r in rows:
        r["ref"] = f"SMS1-HIRA-R{r['xl_row']}"
        r["libraryFlags"] = (lib.get(r["ref"]) or {}).get("reviewFlags", [])
        r["acceptableByRule"] = plant_rule(r)
        r["masters"] = classify(r["hazard"], r["activity"], r["risk"])
        r["hazardTypes"] = [hazard_type(m, r["hazard"]) for m in r["masters"]]
        r["hazardType"] = r["hazardTypes"][0]
        r["L"] = (r["SP"] or 1) * (r["LH"] or 1) * (r["DE"] or 1)
        by_sec[r["section"]][r["sno"]].append(r)

    # ── Controls tab rows → HIRA rows ────────────────────────────────────
    ctrl_for_row: dict[int, list[dict]] = defaultdict(list)
    renumbered, unmatched = [], []
    for c in controls:
        sec = next((s for s in sections if section_key(s) == section_key(c["section"] or "")), None)
        if sec is None:
            unmatched.append(c["xl_row"])
            continue
        target, score = match_control(c, [r for rs in by_sec[sec].values() for r in rs])
        if target is None or score < 2.0:
            unmatched.append(c["xl_row"])
            continue
        c["matchedSno"], c["matchedRow"] = target["sno"], target["xl_row"]
        if c["sno_in_sheet"] is not None and c["sno_in_sheet"] != target["sno"]:
            renumbered.append((sec, c["xl_row"], c["sno_in_sheet"], target["sno"]))
        ctrl_for_row[target["xl_row"]].append(c)

    source = {"meta": {**{k: meta.get(k) for k in ("file", "form24", "form25", "dept", "revision", "date", "leader",
                                                    "team", "non_managerial", "abbreviations", "footnote", "author",
                                                    "lastModifiedBy", "created", "modified")},
                       "significantScore": SIGNIFICANT, "significantScoreCell": meta.get("significantScoreCell"),
                       "matrix": MATRIX_CODE, "imported": NOW.isoformat(timespec="seconds")},
              "studies": {}}
    n_entries = n_haz = n_ctl = n_reg = 0
    desc_common = (
        f"Imported from the client workbook {meta.get('file')}: tab 'HIRA ' = {meta.get('form24')}; "
        f"tab 'Controls' = {meta.get('form25')} (strategy for meeting unacceptable risks). "
        f"Location/Dept: {meta.get('dept')}. HIRA Revision No. {meta.get('revision')}, date {meta.get('date')}.\n"
        f"Team leader: {leader_name}\nHIRA team (managerial): {meta.get('team')}\n"
        f"Non-managerial workers: {meta.get('non_managerial')}\n"
        f"{meta.get('abbreviations')}\n"
        f"Plant method: SV = LH x CN, GT = SP x SV x DE; significant score (sheet) = "
        f"{meta.get('significantScoreCell')}; not acceptable when GT > 3.33 or any legal/domino/chronic/emergency "
        f"concern is Y. SafeOps rates it on the matrix {MATRIX_CODE}: likelihood = SP x LH x DE, severity = CN, "
        f"so the risk score equals GT.\n"
        f"Previous Score (GT) column: '*' = {(meta.get('footnote') or '').lstrip('* ')} (first assessed in this revision).\n"
        f"Approval block in the workbook: {meta.get('approval24')} - names and signatures left blank. "
        f"Workbook author {meta.get('author')}, created {(meta.get('created') or '')[:10]}, last modified "
        f"{(meta.get('modified') or '')[:10]} by {meta.get('lastModifiedBy')}.\n"
        f"Not in the workbook (set by the import): frequency (DAILY for R, OCCASIONAL for NR), review cycle "
        f"(annual), persons exposed (0 = not recorded)."
    )

    for sec in sections:
        code = STUDY_CODES.get(section_key(sec), re.sub(r"[^A-Z]", "", sec.upper())[:6])
        number = STUDY_PREFIX + code
        study_id = new_id()
        snos = sorted(by_sec[sec])
        missing = [n for n in range(1, max(snos) + 1) if n not in by_sec[sec]]
        entries, ent_haz, ent_ctl, ent_reg = [], [], [], []
        levels, acceptable_count = Counter(), 0
        src_entries = {}
        regs_all: dict[str, None] = {}
        for sno in snos:
            group = by_sec[sec][sno]
            worst = max(group, key=lambda r: ((r["GT"] or 0), (r["CN"] or 0)))
            L, S = max(1, min(5, worst["L"])), max(1, min(5, worst["CN"]))
            score, level, color = cell[(L, S)]
            levels[level] += 1
            verdicts = [(r["acceptable"] or "").strip().lower() for r in group]
            acceptable = None if all(not v for v in verdicts) else all(v.startswith("y") for v in verdicts if v)
            acceptable_count += bool(acceptable)
            hazard_names = list(dict.fromkeys(r["hazard"] for r in group))
            eid = new_id()
            rnrs = list(dict.fromkeys(r["rnr"] for r in group))

            def row_line(r):
                concerns = [k for k in ("LC", "DC", "CE", "EC") if (r[k] or "").upper() == "Y"]
                prev = r["prev_gt"]
                prev_txt = ("first assessed in this revision ('*')" if str(prev).strip() == "*"
                            else f"previous GT {prev}" if prev not in (None, "") else "previous GT blank")
                return (f"{r['hazard']} [{r['rnr']}]: SP {r['SP']} x (LH {r['LH']} x CN {r['CN']} = SV {r['SV']}) "
                        f"x DE {r['DE']} = GT {r['GT']}" + (f", concerns {'/'.join(concerns)}" if concerns else "")
                        + f", acceptable: {r['acceptable'] or 'blank'}, {prev_txt} (sheet row {r['xl_row']})")

            variants = []
            for r in group:
                for c in ctrl_for_row.get(r["xl_row"], []):
                    diff = []
                    if c["activity_own"] and norm(c["activity_own"]) != norm(r["activity"]):
                        diff.append(f"activity '{c['activity_own']}'")
                    if c["hazard_own"] and norm(c["hazard_own"]) != norm(r["hazard"]):
                        diff.append(f"hazard '{c['hazard_own']}'")
                    if c["risk"] and norm(c["risk"]) != norm(r["risk"]):
                        diff.append(f"risk '{c['risk']}'")
                    if diff:
                        variants.append(f"QSD/25/F row {c['xl_row']} words it differently: " + "; ".join(diff))
                        c["variant"] = diff

            entries.append(dict(
                id=eid, studyId=study_id, sequenceNumber=sno, groupLabel=None,
                activityDescription=worst["activity"] or group[0]["activity"] or worst["hazard"],
                areaId=areas[area_name[sec]],
                subLocation=f"QSD/24/F S.No {sno} (sheet row{'s' if len(group) > 1 else ''} "
                            f"{', '.join(str(r['xl_row']) for r in group)})",
                routine="NON_ROUTINE" if worst["rnr"].startswith("N") else "ROUTINE",
                frequency="OCCASIONAL" if worst["rnr"].startswith("N") else "DAILY",
                initialLikelihoodId=lik[L], initialLikelihoodScore=L,
                initialLikelihoodRationale=f"Plant index SP {worst['SP']} x LH {worst['LH']} x DE {worst['DE']} = {L} "
                                           f"(QSD/24/F sheet row {worst['xl_row']}"
                                           + (f", worst of {len(group)} hazard rows" if len(group) > 1 else "") + ")"
                                           + (f". R/NR differs between the rows: {', '.join(rnrs)}" if len(rnrs) > 1 else ""),
                initialSeverityId=sev[S], initialSeverityScore=S,
                initialSeverityRationale=f"Consequence CN {S}. QSD/24/F evaluation - "
                                         + "; ".join(row_line(r) for r in group)
                                         + ("\n" + "\n".join(variants) if variants else ""),
                initialRiskScore=score, initialRiskLevel=level, initialRiskColor=color,
                residualLikelihoodId=lik[L], residualLikelihoodScore=L,
                residualLikelihoodRationale="The workbook gives one rating, made with the existing controls in place; "
                                            "the residual is saved as that same rating",
                residualSeverityId=sev[S], residualSeverityScore=S,
                residualRiskScore=score, residualRiskLevel=level, residualRiskColor=color,
                residualAcceptable=acceptable, residualAutoCalculated=False,
                residualAcceptanceRationale="Acceptable OH&S risk per QSD/24/F: "
                                            + ("Yes" if acceptable else "No" if acceptable is False else "not stated")
                                            + (" (" + "; ".join(f"row {r['xl_row']}: {r['acceptable'] or 'blank'}"
                                                                for r in group) + ")" if len(group) > 1 else ""),
                status="ACTIVE", versionNumber=1, isCurrentVersion=True,
                createdAt=eff, createdById=user_id, updatedAt=NOW, updatedById=user_id,
                lastReviewedAt=eff, lastReviewedById=leader_id, nextReviewDue=eff + dt.timedelta(days=365),
            ))

            by_master: dict[str, dict] = {}
            for r in group:
                harms = [r["risk"]] if r["risk"] else []
                for c in ctrl_for_row.get(r["xl_row"], []):
                    if c.get("risk") and norm(c["risk"]) != norm(r["risk"]) and c["risk"] not in harms:
                        harms.append(c["risk"])  # QSD/25/F words the risk differently: keep both
                for m in r["masters"]:
                    hid = hazards[m]
                    if hid in by_master:  # same master twice in one activity: one line, both texts
                        h = by_master[hid]
                        if r["hazard"] and r["hazard"].lower() not in h["contextualDescription"].lower():
                            h["contextualDescription"] += f"; {r['hazard']}"
                        h["potentialHarm"] = list(dict.fromkeys([*(h["potentialHarm"] or []), *harms])) or None
                        continue
                    by_master[hid] = dict(id=new_id(), entryId=eid, hazardId=hid,
                                          contextualDescription=r["hazard"], consequence=r["risk"],
                                          potentialHarm=harms or None,
                                          regulationRef=(r["legal"] or None) and r["legal"][:200],
                                          sortOrder=len(by_master))
            ent_haz.extend(by_master.values())

            seen_reg = set()
            for r in group:
                for reg, sec_ in legal_refs(r["legal"]):
                    regs_all[f"{reg}, {sec_}" if sec_ else reg] = None
                    if (reg, sec_) in seen_reg:
                        continue
                    seen_reg.add((reg, sec_))
                    ent_reg.append(dict(id=new_id(), entryId=eid, regulation=reg, section=sec_,
                                        requirementSummary=f"As cited in QSD/24/F row {r['xl_row']}: {r['legal']}"))

            seen, order = set(), 0

            def add_ctl(hier, desc, ref=None):
                nonlocal order
                key = (hier, desc.lower())
                if key in seen:
                    return
                seen.add(key)
                ent_ctl.append(dict(id=new_id(), entryId=eid, hierarchy=hier, description=desc,
                                    documentReference=ref, sortOrder=order))
                order += 1

            for r in group:
                ref = r["control_ref"]
                if ref:
                    is_doc = bool(DOC_REF.search(ref)) and len(ref) < 40
                    add_ctl(f_hierarchy(ref), f"Work instruction / SOP {ref}" if is_doc else ref, ref if is_doc else None)
            src_ctrl = []
            for r in group:
                tag = f"[{r['hazard']}] " if len(hazard_names) > 1 else ""
                for c in ctrl_for_row.get(r["xl_row"], []):
                    cols = {}
                    for col, hier, label in CONTROL_COLUMNS:
                        val = c.get(col)
                        if val is None or str(val).strip().lower() in ("", "-", "na", "n/a", "no"):
                            continue
                        val = str(val)
                        cols[col] = val
                        if col == "procedure" and norm_ref(val) and any(
                                norm_ref(val) in norm_ref(x["control_ref"]) for x in group):
                            continue  # same SOP as the HIRA tab's control column
                        text = f"{label}: in place" if val.strip().lower() == "yes" else f"{label}: {val}"
                        add_ctl(hier, tag + text if hier != "PPE" else text, val if col == "procedure" else None)
                    src_ctrl.append({"sheetRow": c["xl_row"], "snoInSheet": c["sno_in_sheet"], "forHiraRow": r["xl_row"],
                                     "hazard": c["hazard"], "risk": c["risk"], "activity": c["activity"],
                                     "wordingDiffers": c.get("variant"), "columns": cols})
            src_entries[str(sno)] = {
                "rows": [{k: r[k] for k in ("xl_row", "ref", "activity", "rnr", "hazard", "hazardType", "hazardTypes",
                                           "masters", "risk", "control_ref", "LC", "DC", "CE", "EC", "SP", "LH", "CN",
                                           "SV", "DE", "GT", "L", "prev_gt", "acceptable", "acceptableByRule", "legal",
                                           "libraryFlags")} for r in group],
                "controlRows": src_ctrl,
            }

        form_no = (re.search(r"QSD/\d+/F", meta.get("form24") or "") or re.search(r".+", "QSD/24/F")).group(0)
        title = f"{dept_code} {sec} — HIRA ({form_no} Rev {meta.get('revision')})"
        study = dict(
            id=study_id, number=number, plantId=plant_id, departmentId=dept_id, areaId=areas[area_name[sec]],
            scopeType="AREA", title=title,
            description=f"{sec} of the {meta.get('dept')} steel melt shop: {len(snos)} activities, S.No 1-{max(snos)}"
                        + (f" (no S.No {', '.join(map(str, missing))} in the workbook)" if missing else "") + ".\n"
                        + desc_common,
            riskMatrixId=matrix_id, teamLeaderId=leader_id, status="ACTIVE",
            initiatedAt=eff, completedAt=eff, approvedAt=None, approvedById=None, effectiveFrom=eff,
            nextScheduledReviewDate=eff + dt.timedelta(days=365), reviewFrequency="ANNUAL",
            applicableRegulations=list(regs_all), regulatoryReviewRequired=bool(regs_all),
            aggregateMetrics={"totalEntries": len(entries),
                              "byInitialLevel": {k: levels.get(k, 0) for k in ("LOW", "MODERATE", "HIGH", "CRITICAL")},
                              "byResidualLevel": {k: levels.get(k, 0) for k in ("LOW", "MODERATE", "HIGH", "CRITICAL")},
                              "acceptableResidualCount": acceptable_count},
            createdAt=NOW, createdById=user_id, updatedAt=NOW, updatedById=user_id,
        )
        insert_many(cur, "HiraStudy", [study])
        insert_many(cur, "HiraStudyTeamMember",
                    [dict(id=new_id(), studyId=study_id, userId=uid, teamRole="SUBJECT_MATTER_EXPERT", department=dept_code)
                     for _, uid in managerial]
                    + [dict(id=new_id(), studyId=study_id, userId=uid, teamRole="OPERATOR_REP", department=dept_code)
                       for _, uid in workers])
        insert_many(cur, "HiraEntry", entries)
        insert_many(cur, "HiraEntryHazard", ent_haz)
        insert_many(cur, "HiraEntryControl", ent_ctl)
        insert_many(cur, "HiraEntryRegulationRef", ent_reg)
        n_entries, n_haz, n_ctl, n_reg = n_entries + len(entries), n_haz + len(ent_haz), n_ctl + len(ent_ctl), n_reg + len(ent_reg)
        source["studies"][number] = {
            "section": sec, "title": title, "area": area_name[sec], "snoRange": [1, max(snos)], "missingSno": missing,
            "renumberedControlRows": [{"sheetRow": x[1], "snoInSheet": x[2], "matchedSno": x[3]}
                                      for x in renumbered if x[0] == sec],
            "entries": src_entries,
        }
        print(f"  + {number}: {sec} — {len(entries)} activities (S.No 1-{max(snos)}"
              f"{', missing ' + ','.join(map(str, missing)) if missing else ''}), "
              f"{len(ent_haz)} hazards, {len(ent_ctl)} controls, {len(ent_reg)} legal refs; levels {dict(levels)}")

    print(f"  = {len(sections)} studies, {n_entries} entries, {n_haz} hazard lines, {n_ctl} controls, {n_reg} legal refs; "
          f"{len(controls) - len(unmatched)}/{len(controls)} Controls-tab rows matched; team: leader + "
          f"{len(managerial)} managerial + {len(workers)} non-managerial")
    if renumbered:
        print(f"    Controls-tab rows whose S.No did not match the HIRA tab ({len(renumbered)}):")
        for sec, xl, was, now in renumbered:
            print(f"      {sec}: Controls row {xl} says S.No {was} -> matched S.No {now}")
    if unmatched:
        print(f"    ! unmatched Controls-tab rows: {unmatched}")
    return source


def import_observations(cur, path, plant_id, user_id, replace=False):
    rows = parse_observations(path)
    numbers = [OBS_PREFIX + str(r["Inspection Code"]).strip() for r in rows]
    if replace:
        cur.execute('DELETE FROM "Observation" WHERE "plantId"=%s AND number LIKE %s', (plant_id, OBS_PREFIX + "%"))
        print(f"  - removed {cur.rowcount} existing JSL observations")
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

    def when(v):
        return v if isinstance(v, dt.datetime) else None

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
        created = when(r["Created Date"]) or NOW
        closed_date, review_date = when(r["Closed Date"]), when(r.get("Final Review Date"))
        sheet_closed = clean(r["Status"]) == "Closed"
        if sheet_closed:
            status = "CLOSED"
        elif closed_date and not review_date:
            status = "IN_PROGRESS"  # closure submitted by the task executor, final review pending
        else:
            status = "OPEN"  # not acted on yet, or the submitted closure was rejected at final review

        details = [
            f"Inspection code: {code}",
            f"Category: {cat_code} {exact(r['Category']) or ''}".strip(),
            f"Observer: {person(r['Observer Name'], r['Employee ID'], ', '.join(x for x in (exact(r['Observer Designation']), exact(r['Observer Department'])) if x))}",
            f"Location: {' / '.join(x for x in (exact(r['Area']), exact(r['Section']), exact(r['Location'])) if x)}",
            f"Area I/C: {person(r['Area I/C Name'], r['Area I/C Emp ID'])}" if exact(r["Area I/C Name"]) else None,
            f"Task executor: {person(r['Task Executor Name'], r['Task Executor Emp ID'], exact(r['Task Executor Function']))}" if exact(r["Task Executor Name"]) else None,
            f"Function: {exact(r['Function'])}" if exact(r["Function"]) else None,
            f"Severity (sheet): {clean(r['Severity'])}" if fatal else None,
            f"Special category: {exact(r['Special Category'])}" if clean(r["Special Category"]) not in (None, "None") else None,
            f"Impacted: {person(r['Individual Name'], extra=exact(r['Impacted Individual']))}" if exact(r["Impacted Individual"]) else None,
            f"Impacted function: {exact(r['Impacted Function'])}" if exact(r.get("Impacted Function")) else None,
        ]
        remarks = [text_value(r.get("Closing Remarks"))]
        if not sheet_closed and closed_date:
            remarks.append(f"Closure submitted by the task executor: {ist(closed_date)}"
                           + ("" if review_date else " (awaiting final review)"))
        final = text_value(r.get("Final Remarks"))
        if review_date:
            remarks.append(f"Final review: {ist(review_date)}" + (f" - {final}" if final else ""))
        elif final:
            remarks.append(f"Final review: {final}")
        for label, col in (("Approval remarks", "Approve Remarks"), ("Closure rejected", "Closing Rejection Remarks"),
                           ("Exceptional remarks", "Exceptional Remarks"), ("Rejection remarks", "Rejection Remarks")):
            if text_value(r.get(col)):
                remarks.append(f"{label}: {text_value(r.get(col))}")
        remarks = [x for x in remarks if x]
        stamps = [d for d in (created, closed_date, review_date) if d]
        out.append(dict(
            id=new_id(), number=number, observerId=user_id, date=created, plantId=plant_id,
            areaId=areas[area_of(r)], type=typ, category=cat or "OTHERS",
            categoryCode=cat, subCategoryCode=sub, taxonomyAxis=axis if cat else None,
            description=(exact(r["Observation"]) or "(no text)") + "\n\n" + "\n".join(d for d in details if d),
            severity="CRITICAL" if fatal else level, riskLevel=level,
            immediateAction=exact(r["Action Plan"]),
            targetDate=when(r["Target Date"]),
            status=status,
            closedAt=closed_date if sheet_closed else None,
            closingRemark="\n".join(remarks) or None,
            createdAt=created, updatedAt=max(stamps),
        ))
    insert_many(cur, "Observation", out)
    print(f"  + {len(out)} observations ({skipped} already present)")
    if out:
        print(f"    status: {dict(Counter(o['status'] for o in out))}")
        print(f"    severity: {dict(Counter(o['severity'] for o in out))}  riskLevel: {dict(Counter(o['riskLevel'] for o in out))}")
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
    print(f"  - {cur.rowcount} HIRA studies (entries, hazards, controls, team cascade)")
    cur.execute('DELETE FROM "Observation" WHERE "plantId"=%s', (pid,))
    print(f"  - {cur.rowcount} observations")
    cur.execute('DELETE FROM "User" WHERE email=%s OR email LIKE %s', (IMPORT_USER_EMAIL, TEAM_EMAIL.format("%")))
    print(f"  - {cur.rowcount} users (import + HIRA team)")
    mid = one(cur, 'SELECT id FROM "RiskMatrix" WHERE code=%s', MATRIX_CODE)
    if mid and not one(cur, 'SELECT count(*) FROM "HiraStudy" WHERE "riskMatrixId"=%s', mid):
        for t in ("RiskMatrixCell", "RiskMatrixLikelihood", "RiskMatrixSeverity"):
            cur.execute(f'DELETE FROM "{t}" WHERE "matrixId"=%s', (mid,))
        cur.execute('DELETE FROM "RiskMatrix" WHERE id=%s', (mid,))
        print(f"  - risk matrix {MATRIX_CODE}")
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
    ap.add_argument("--replace-hira", action="store_true", help="delete and rebuild the imported HIRA studies")
    ap.add_argument("--replace-observations", action="store_true", help="delete and re-import the JSL observations")
    ap.add_argument("--grant", nargs="+", metavar="EMAIL", default=[],
                    help="give these plant-scoped users access to the JSL plant")
    a = ap.parse_args()
    if not (a.rollback or a.hira or a.observations or a.grant):
        ap.error("give --hira, --observations, --grant, or --rollback")

    conn = connect()
    hira_source = None
    try:
        with conn.cursor() as cur:
            if a.rollback:
                rollback(cur)
            else:
                plant_id = ensure_plant(cur)
                user_id = ensure_import_user(cur, plant_id)
                if a.hira:
                    hira_source = import_hira(cur, a.hira, plant_id, user_id, replace=a.replace_hira)
                if a.observations:
                    import_observations(cur, a.observations, plant_id, user_id, replace=a.replace_observations)
                if a.grant:
                    grant_access(cur, plant_id, a.grant)
                drop_unused_areas(cur, plant_id)
        if a.apply:
            conn.commit()
            print("Committed.")
            if hira_source:
                HIRA_SOURCE.write_text(json.dumps(hira_source, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
                print(f"Wrote {HIRA_SOURCE.relative_to(BACKEND)} (sheet values for the copilot review).")
        else:
            conn.rollback()
            print("Dry run: rolled back. Re-run with --apply to save.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
