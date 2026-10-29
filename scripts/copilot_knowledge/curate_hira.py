"""Curate the SMS-1 HIRA (QSD/24/F) + control strategy (QSD/25/F) into a clean library.

Every correction below comes from a manual row-by-row review; each corrected row
carries a review flag so the change is traceable to the source workbook row.
"""
import json, re, sys, difflib, collections

SRC = sys.argv[1]
OUT = sys.argv[2]
raw = json.load(open(SRC, encoding="utf-8"))

SPELL = {
    r"\bVehical\b": "Vehicle", r"\bChagring\b": "Charging", r"\btrasnfer\b": "transfer", r"\bHazrad\b": "Hazard",
    r"\bfailiour\b": "failure", r"\bLiftting\b": "Lifting", r"\bnthe\b": "the", r"\bManully\b": "Manual",
    r"\bauxilliary\b": "auxiliary", r"\bAuxilary\b": "Auxiliary", r"\bTemprature\b": "Temperature",
    r"\bspalshing\b": "splashing", r"\bRadition\b": "Radiation", r"\bnozzel\b": "nozzle", r"\brammimg\b": "ramming",
    r"\bWithdrawl\b": "Withdrawal", r"\bannd\b": "and", r"\bboilng\b": "boiling", r"\bavalilable\b": "available",
    r"\bmachinary\b": "machinery", r"\bEquipmental\b": "Equipment", r"\bgurad\b": "guard", r"\bscaning\b": "scanning",
    r"\bscanining\b": "scanning", r"\bprocided\b": "provided", r"\bHospitilazation\b": "hospitalisation",
    r"\bharassement\b": "harassment", r"\bcasusing\b": "causing", r"\bstucked\b": "stuck", r"\bregading\b": "regarding",
    r"\btrains and sprains\b": "Strains and sprains", r"\bmay injured\b": "may get injured", r"\bmay burnt\b": "may get burnt",
    r"\bL\.M\.": "liquid metal", r"\bLM\b": "liquid metal", r"\bFe Si\b": "FeSi",
}

def clean(t):
    if t is None:
        return None
    t = str(t).replace("�", "-").strip()
    t = " ".join(t.split())
    for pat, rep in SPELL.items():
        t = re.sub(pat, rep, t, flags=re.I if pat.startswith(r"\bmay") else 0)
    t = t.strip(" .")
    return t[:1].upper() + t[1:] if t else None

# Canonical hazard types (the HIRA's free-text hazard column, normalised).
HAZARD_TYPES = [
    ("STEAM_EXPLOSION", ["steam explosion"]),
    ("MOLTEN_METAL_BREAKOUT", ["puncturing of ladle"]),
    ("FIRE_EXPLOSION", ["fire & explosion", "explosion"]),
    ("FIRE", ["fire"]),
    ("BURN_SCALD", ["burn", "scald", "splash"]),
    ("VEHICULAR", ["vehic", "horn not", "parking brake"]),
    ("EQUIPMENT_FAILURE", ["equipment failure", "equipment hazard", "equipment failure"]),
    ("MACHINERY_TOOL", ["machinery", "machine/tool", "machine", "tool"]),
    ("CRUSHING", ["crushing"]),
    ("GRAVITY_FALLING_OBJECT", ["gravity"]),
    ("ERGONOMIC", ["ergonomic"]),
    ("DUST_FUME", ["environment", "respiratory"]),
    ("HEAT_STRESS", ["heat stress"]),
    ("RADIATION", ["radiation"]),
    ("PRESSURE", ["pressure"]),
    ("VIBRATION", ["vibration"]),
    ("SLIP_TRIP", ["slip trip"]),
    ("ELECTRICAL", ["electric shock"]),
    ("BIOLOGICAL", ["animal bite", "covid", "contaminated", "infected", "waste of mask"]),
    ("PSYCHOSOCIAL", ["behaviour", "workload", "working hours", "harassment"]),
    ("DISTRACTION", ["mobile phone"]),
    ("COMMUNICATION", ["communication"]),
]
HAZARD_LABEL = {
    "STEAM_EXPLOSION": "Steam explosion (water contact with molten metal)", "MOLTEN_METAL_BREAKOUT": "Ladle puncture / molten metal breakout",
    "FIRE_EXPLOSION": "Fire and explosion", "FIRE": "Fire", "BURN_SCALD": "Burn and scald (molten metal, slag, hot surfaces)",
    "VEHICULAR": "Vehicle movement", "EQUIPMENT_FAILURE": "Equipment / lifting gear failure", "MACHINERY_TOOL": "Machinery or tool",
    "CRUSHING": "Crushing (moving trolley / door)", "GRAVITY_FALLING_OBJECT": "Falling object / dropped load", "ERGONOMIC": "Ergonomic (manual handling, posture)",
    "DUST_FUME": "Dust and fume inhalation", "HEAT_STRESS": "Heat stress", "RADIATION": "Heat / light radiation", "PRESSURE": "Pressure (gas, hydraulic, pneumatic)",
    "VIBRATION": "Hand-arm vibration", "SLIP_TRIP": "Slip, trip and fall", "ELECTRICAL": "Electric shock", "BIOLOGICAL": "Biological (infection, animal bite)",
    "PSYCHOSOCIAL": "Psychosocial (stress, workload, harassment)", "DISTRACTION": "Distraction (mobile phone use)", "COMMUNICATION": "Communication failure",
}

def hazard_type(text):
    t = (text or "").lower()
    for code, keys in HAZARD_TYPES:
        if any(k in t for k in keys):
            return code
    return "OTHER"

LEGAL = [
    (r"sec\.?\s*-?\s*29|sec-29", "Factories Act 1948, Sec. 29 (lifting machines, chains, ropes and lifting tackle)"),
    (r"sec\.?\s*-?\s*38", "Factories Act 1948, Sec. 38 (precautions in case of fire)"),
    (r"sec\.?\s*-?\s*14", "Factories Act 1948, Sec. 14 (dust and fume)"),
    (r"sec\.?\s*-?\s*13", "Factories Act 1948, Sec. 13 (ventilation and temperature)"),
    (r"sec\.?\s*-?\s*31", "Factories Act 1948, Sec. 31 (pressure plant)"),
    (r"sec\.?\s*-?\s*34", "Factories Act 1948, Sec. 34 (excessive weights)"),
    (r"sec\.?\s*-?\s*36", "Factories Act 1948, Sec. 36 (dangerous fumes and gases)"),
    (r"sec\.?\s*-?\s*56", "Factories Act 1948, Sec. 56 (spread-over of working hours)"),
    (r"87\s*&\s*88", "Factories Act 1948, Sec. 87-88 (dangerous operations; notice of accidents)"),
    (r"cmv", "Central Motor Vehicles Rules 1989"),
    (r"air act", "Air (Prevention and Control of Pollution) Act 1981, Sec. 22"),
    (r"noise", "Noise Pollution (Regulation and Control) Rules 2000 (EP Act Sec. 3)"),
    (r"epidemic", "Epidemic Diseases Act 1897, Sec. 2"),
    (r"sexual", "Sexual Harassment of Women at Workplace (POSH) Act 2013"),
]

def legal(t):
    if not t:
        return None
    for pat, lab in LEGAL:
        if re.search(pat, t, flags=re.I):
            return lab
    return clean(t)

PPE_CANON = {
    "helmet": "Helmet", "safety shoes": "Safety shoes", "hand gloves": "Hand gloves", "gloves": "Hand gloves",
    "dust mask": "Dust mask", "mask": "Mask", "safety goggles": "Safety goggles", "safetygoggles": "Safety goggles",
    "melting goggles": "Melting (dark) goggles", "safety jacket": "Reflective safety jacket", "safety jackets": "Reflective safety jacket",
    "fire resistant jacket": "Fire-resistant jacket", "face shield": "Face shield", "leg guard": "Leg guard", "leg gurad": "Leg guard",
    "apron": "Aluminised apron", "ear plug": "Ear plugs", "none": None,
}

def ppe_list(t):
    if not t:
        return []
    out = []
    for p in re.split(r",", t):
        k = " ".join(p.lower().split())
        v = PPE_CANON.get(k, clean(p))
        if v and v not in out:
            out.append(v)
    return out

def doc_ref(t):
    if not t:
        return None
    m = re.search(r"SM\s*0?1I?/\d+/W", t, flags=re.I)
    if m:
        return m.group(0).upper().replace("SM01I", "SM01").replace("SMI01", "SM01").replace(" ", "")
    return None

# ── Section fixes (rows identified in review) ────────────────────────────
SECTION_FIX = {r: "General (all activities)" for r in range(344, 353)}
SECTION_FIX.update({r: "COVID-19 activities" for r in range(354, 357)})
SKIP_ROWS = {353, 361}

# Consequence/hazard corrections (copy-paste errors found in review).
CONSEQ_FIX = {
    82: "Hand or finger injury and strain while attaching the chain to the slag box",
    89: "Crane or tackle failure may drop the ladle and splash molten metal on people nearby",
    91: "Material or spillage falling from the suspended ladle may injure people below",
    102: "Person may get injured by slipping while adding radex",
}
HAZARD_FIX = {112: "Ergonomic Hazard"}  # lifting the gunning gun: consequence is back injury
ARITH_FIX = {132: {"SV": 4, "GT": 4}}     # LH 2 x CN 2 entered as SV 2
RNR_FIX = {317: "R"}

entries = []
for x in raw["hira"]:
    r = x["xl_row"]
    if r in SKIP_ROWS or x["hazard"] is None:
        continue
    flags = []
    section = SECTION_FIX.get(r, x["section"])
    if r in SECTION_FIX:
        flags.append("SECTION_CORRECTED: was listed under the AFS area; it is the plant-wide general/COVID block")
    hz = x["hazard"]
    if r in HAZARD_FIX:
        flags.append(f"HAZARD_CORRECTED: '{hz}' did not match the consequence")
        hz = HAZARD_FIX[r]
    cons = x["risk"]
    if r in CONSEQ_FIX:
        flags.append(f"CONSEQUENCE_CORRECTED: source said '{cons}', which does not follow from the hazard")
        cons = CONSEQ_FIX[r]
    sc = {k: x[k] for k in ("SP", "LH", "CN", "SV", "DE", "GT")}
    if r in ARITH_FIX:
        flags.append(f"SCORE_CORRECTED: LH {x['LH']} x CN {x['CN']} was entered as SV {x['SV']}")
        sc.update(ARITH_FIX[r])
    rnr = (x["rnr"] or "").strip() or None
    if r in RNR_FIX:
        flags.append("ROUTINE_FILLED: R/NR was blank; siblings are routine")
        rnr = RNR_FIX[r]
    concerns = {"legal": x["LC"] == "Y", "domino": x["DC"] == "Y", "chronic": x["CE"] == "Y", "emergency": x["EC"] == "Y"}
    significant_rule = any(concerns.values()) or (sc["GT"] or 0) >= 4
    acc = x["acceptable"]
    if acc is None:
        flags.append("ACCEPTABILITY_FILLED: blank in source; derived from the plant rule")
        acc = "No" if significant_rule else "Yes"
    elif acc == "Yes" and significant_rule:
        flags.append("RULE_INCONSISTENT: marked acceptable although a qualitative concern is Y; treated as not acceptable")
        acc = "No"
    elif acc == "No" and not significant_rule:
        flags.append("CONSERVATIVE_VERDICT: score and concerns allow 'acceptable' but the team marked it not acceptable")
    if re.fullmatch(r"(person|body part)s? may (get )?(injured|injury|get injured)|body part injury|person may get injured|accidents (may|can) happen|physical injury", (cons or "").lower().strip(" .")):
        flags.append("VAGUE_CONSEQUENCE: says only that someone may be injured")
    entries.append({
        "ref": f"SMS1-HIRA-R{r}", "section": clean(section), "sno": x["sno"], "activity": clean(x["activity"]),
        "routine": {"R": "ROUTINE", "NR": "NON_ROUTINE"}.get((rnr or "").upper()),
        "hazardText": clean(hz), "hazardType": hazard_type(clean(hz)), "consequence": clean(cons),
        "concerns": concerns, "scores": sc, "acceptable": acc == "Yes",
        "legalRef": legal(x["legal"]), "procedureRef": doc_ref(x["control_ref"]),
        "existingControlsNote": None if doc_ref(x["control_ref"]) else clean(x["control_ref"]),
        "controls": [], "ppe": [], "reviewFlags": flags,
        "stale": section == "COVID-19 activities" or "covid" in (hz or "").lower(),
    })

# ── Join the control strategy sheet (QSD/25/F) ──────────────────────────
SEC_ALIAS = {"Alloy Feeding System(AFS) Area": "Alloy Feeding System (AFS) Area", "Vacuum Degassing Area": "Vacuum Degassing (VD) Area"}
CTRL_COLS = [("elimination", "ELIMINATION"), ("tech_modification", "SUBSTITUTION"), ("engineering", "ENGINEERING"),
             ("procedure", "ADMINISTRATIVE"), ("signage", "ADMINISTRATIVE"), ("administrative", "ADMINISTRATIVE"),
             ("competence", "ADMINISTRATIVE"), ("pip", "ADMINISTRATIVE")]
ENGINEERING_WORDS = ("brake", "installed", "provided", "hooter", "shield", "railing", "cabin", "switch", "system")

def ctrl_text(col, v):
    if col == "signage":
        return "Warning signage displayed at the activity" if str(v).lower() == "yes" else clean(v)
    if col == "procedure":
        ref = doc_ref(v)
        return f"Follow work instruction {ref}" if ref else clean(v)
    if col == "competence":
        return clean(v)
    return clean(v)

unmatched = []
for c in raw["controls"]:
    r = c["xl_row"]
    sec = SEC_ALIAS.get(c["section"], c["section"])
    if 207 <= r <= 210:
        sec = "General (all activities)"
    if 211 <= r <= 213:
        sec = "COVID-19 activities"
    act = clean(c["activity"]) or ""
    htype = hazard_type(clean(c["hazard"]))
    cands = [e for e in entries if e["section"] == clean(sec)]
    def score(e):
        s = difflib.SequenceMatcher(None, act.lower(), (e["activity"] or "").lower()).ratio()
        s += 0.6 if e["hazardType"] == htype else 0
        s += 0.25 if str(e["sno"]) == str(c["sno"]) else 0
        s += 0.1 if not e["acceptable"] else 0
        return s
    best = max(cands, key=score) if cands else None
    if best is None or score(best) < 1.2:
        unmatched.append((r, sec, act, c["hazard"], round(score(best), 2) if best else None))
        continue
    for col, level in CTRL_COLS:
        v = c.get(col)
        if not v:
            continue
        txt = ctrl_text(col, v)
        lvl = level
        if col == "administrative" and any(w in txt.lower() for w in ENGINEERING_WORDS) and "inspection" not in txt.lower():
            lvl = "ENGINEERING"  # e.g. "emergency brake switch installed" was filed as administrative
        if col == "tech_modification" and "brake" in txt.lower():
            lvl = "ENGINEERING"
        item = {"hierarchy": lvl, "text": txt, "sourceColumn": col}
        if item["text"] and all(i["text"] != item["text"] for i in best["controls"]):
            best["controls"].append(item)
    for p in ppe_list(c.get("ppe")):
        if p not in best["ppe"]:
            best["ppe"].append(p)
    best.setdefault("_ctrlRows", []).append(r)

# Rows that are significant but got no strategy row
for e in entries:
    if e["procedureRef"] and not any(i["text"].endswith(e["procedureRef"]) for i in e["controls"]):
        e["controls"].insert(0, {"hierarchy": "ADMINISTRATIVE", "text": f"Follow work instruction {e['procedureRef']}", "sourceColumn": "hira"})
    if e["existingControlsNote"]:
        lvl = "ENGINEERING" if any(w in e["existingControlsNote"].lower() for w in ENGINEERING_WORDS) else "ADMINISTRATIVE"
        if all(i["text"] != e["existingControlsNote"] for i in e["controls"]):
            e["controls"].append({"hierarchy": lvl, "text": e["existingControlsNote"], "sourceColumn": "hira"})
    if not e["acceptable"] and not e.get("_ctrlRows"):
        e["reviewFlags"].append("NO_STRATEGY_ROW: not acceptable but has no row in the control strategy sheet")
    e.pop("_ctrlRows", None)
    e.pop("existingControlsNote", None)
    if not e["acceptable"] and not any(i["hierarchy"] in ("ELIMINATION", "SUBSTITUTION", "ENGINEERING") for i in e["controls"]):
        e["reviewFlags"].append("ADMIN_PPE_ONLY: not acceptable and controlled only by procedure/PPE")

summary = {
    "entries": len(entries),
    "notAcceptable": sum(1 for e in entries if not e["acceptable"]),
    "flags": collections.Counter(f.split(":")[0] for e in entries for f in e["reviewFlags"]),
    "hazardTypes": collections.Counter(e["hazardType"] for e in entries),
    "controlLevels": collections.Counter(i["hierarchy"] for e in entries for i in e["controls"]),
    "unmatchedControlRows": unmatched,
}
json.dump({"entries": entries, "hazardTypeLabels": HAZARD_LABEL}, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(json.dumps(summary, indent=1, default=str))
