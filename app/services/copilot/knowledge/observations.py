"""Safety-observation reference library and the field suggestions built on it.

The library (data/observation_library.json) is built from a plant's reviewed
observation register by scripts/copilot_knowledge/build_library.py. Every row
was reviewed before it went in: wrong categories and under-rated risks carry
the reviewer's corrected label, unusable rows are left out, and only rows the
reviewer marked as worked examples keep any free text (rewritten, with names
removed). Everything else contributes only its labels and a short hazard
phrase, so the copilot can learn the plant's practice without seeing raw text.

Nothing here touches the database or the network; the copilot tool that calls
`suggest()` maps the result onto the live SafeOps taxonomy.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.copilot.knowledge.text_index import Bm25Index, tokenize

DATA = Path(__file__).with_name("data") / "observation_library.json"

# Plant risk level → SafeOps observation severity. "Fatal Potential" is the
# plant's top flag (always High risk, same-day target), so it maps to CRITICAL.
SEVERITY_FOR = {"Low": "LOW", "Medium": "MEDIUM", "High": "HIGH", "FatalPotential": "CRITICAL"}

# Plant category → SafeOps STOP (category, sub-category). The first pair is the
# default; a tag listed against a later pair selects it instead.
STOP_MAP: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
    "A1": [("TOOLS_EQUIPMENT", "TE_USED_INCORRECTLY", ()),
           ("POSITIONS_OF_PEOPLE", "PP_CAUGHT_IN_ON_BETWEEN", ("rotating_unguarded", "nip_point", "conveyor_roller")),
           ("TOOLS_EQUIPMENT", "TE_IMPROVISED_TOOL", ("defective_tool",))],
    "A2": [("PROCEDURES", "PR_NOT_FOLLOWED", ())],
    "A3": [("PROCEDURES", "PR_NOT_FOLLOWED", ()), ("PROCEDURES", "PR_PERMIT_LOTO_BYPASSED", ("permit_isolation",))],
    "A4": [("PROCEDURES", "PR_NOT_FOLLOWED", ())],
    "A5": [("PPE", "PPE_NOT_WORN", ())],
    "A6": [("PROCEDURES", "PR_DEVIATED_FROM_SOP", ())],
    "A7": [("HOUSEKEEPING", "HK_OBSTRUCTING_PATH", ()),
           ("POSITIONS_OF_PEOPLE", "PP_STRUCK_BY", ("suspended_load", "crane_hoist", "falling_object")),
           ("POSITIONS_OF_PEOPLE", "PP_OVEREXERTION", ("ergonomics",))],
    "A8": [("PROCEDURES", "PR_NOT_FOLLOWED", ()), ("HOUSEKEEPING", "HK_OBSTRUCTING_PATH", ("emergency_egress", "passage_blocked"))],
    "A9": [("PROCEDURES", "PR_NOT_FOLLOWED", ())],
    "A10": [("HOUSEKEEPING", "HK_NOT_CLEAN_AS_YOU_GO", ()),
            ("HOUSEKEEPING", "HK_OBSTRUCTING_PATH", ("passage_blocked", "emergency_egress")),
            ("HOUSEKEEPING", "HK_IMPROPER_WASTE_DISPOSAL", ("housekeeping_scrap",))],
    "C1": [("TOOLS_EQUIPMENT", "TE_DAMAGED", ()), ("TOOLS_EQUIPMENT", "TE_GUARD_MISSING", ("rotating_unguarded", "nip_point")),
           ("TOOLS_EQUIPMENT", "TE_OVERDUE_MAINTENANCE", ("fire_equipment",))],
    "C2": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ()),
           ("HOUSEKEEPING", "HK_DAMAGED_FLOORING", ("floor_damage", "trip_hazard", "floor_opening")),
           ("HOUSEKEEPING", "HK_SPILL_NOT_CLEANED", ("slip_oil_water", "hydraulic_oil_leak"))],
    "C3": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ()), ("TOOLS_EQUIPMENT", "TE_DAMAGED", ("cable_damage",))],
    "C4": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ())],
    "C5": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ()), ("TOOLS_EQUIPMENT", "TE_OVERDUE_MAINTENANCE", ("fire_equipment",)),
           ("HOUSEKEEPING", "HK_CLUTTER_DEBRIS", ("combustible_material",))],
    "C6": [("TOOLS_EQUIPMENT", "TE_DAMAGED", ())],
    "C7": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ()), ("HOUSEKEEPING", "HK_SPILL_NOT_CLEANED", ("slip_oil_water", "chemical_acid"))],
    "C8": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ())],
    "C9": [("HOUSEKEEPING", "HK_CLUTTER_DEBRIS", ())],
    "C10": [("HOUSEKEEPING", "HK_POOR_LIGHTING", ())],
    "C11": [("TOOLS_EQUIPMENT", "TE_GUARD_MISSING", ())],
    "C12": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ())],
    "C13": [("HOUSEKEEPING", "HK_BLOCKED_EGRESS", ()), ("HOUSEKEEPING", "HK_CLUTTER_DEBRIS", ("housekeeping_scrap", "material_stacking"))],
    "C14": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ())],
    "C15": [("PROCEDURES", "PR_NOT_AVAILABLE", ())],
    "C16": [("HOUSEKEEPING", "HK_DAMAGED_FLOORING", ()), ("HOUSEKEEPING", "HK_SPILL_NOT_CLEANED", ("slip_oil_water",)),
            ("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ("open_edge_railing", "floor_opening", "ladder_scaffold"))],
    "C17": [("TOOLS_EQUIPMENT", "TE_DAMAGED", ())],
    "C18": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ())],
    "C19": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ())],
    "C20": [("TOOLS_EQUIPMENT", "TE_UNSAFE_CONDITION", ())],
}

# Plain-language signals that an observation may have fatal potential. They
# never set the rating on their own; they make the copilot ask the question.
FATAL_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    ("fall from height (open edge, opening, missing railing or lifeline)",
     re.compile(r"(height|roof|edge|platform|scaffold|ladder|opening|pit|cut ?out|shaft|deck|stair|tank top|walkway|gallery)"
                r"\W+(?:\w+\W+){0,6}?(no|without|missing|broken|removed|damaged|open|not provided|unprotected|gap)"
                r"|(railing|handrail|guard ?rail|toe ?board|lifeline|life line|harness|barricad\w*|cover)\W+(?:\w+\W+){0,4}?"
                r"(missing|broken|damaged|removed|not (provided|available|used|anchored)|absent)"
                r"|(no|without|missing)\W+(?:\w+\W+){0,3}?(railing|handrail|guard ?rail|lifeline|life line|harness|barricad\w*)"
                r"|fall from height|fragile|excavation|manhole|open (drain|chamber|sump|trench)|cover (is )?missing", re.I)),
    ("live electrical parts within reach",
     re.compile(r"\blive\b|bus ?bar|exposed (terminal|conductor|joint|wire|cable)|naked (wire|cable)|bare wire"
                r"|(panel|db|mcc|pcc|junction ?box|jb|terminal box)\W+(?:\w+\W+){0,4}?(open|without cover|cover missing|door open)"
                r"|without (electric )?plug|dsl|without earthing|earthing (missing|not|disconnected)", re.I)),
    ("molten metal, slag or water near hot metal",
     re.compile(r"molten|liquid metal|\bslag\b|tapping|ladle|hot metal|red[- ]hot|splash|water\W+(?:\w+\W+){0,5}?(furnace|ladle|melting)", re.I)),
    ("people near suspended loads or failing lifting gear",
     re.compile(r"(under|below|beneath)\W+(?:\w+\W+){0,3}?(suspended|hanging|load|lift)|suspended (load|drum|coil)"
                r"|(sling|wire rope|rope|hook|shackle|chain|lifting tackle|tong)\W+(?:\w+\W+){0,4}?"
                r"(damaged|damage|broken|cut|kinked|knot\w*|latch|frayed|bird ?cag\w*|jump\w*|worn)"
                r"|(damaged|knotted|defective)\W+(?:\w+\W+){0,2}?(sling|wire rope|shackle|hook)|in place of (a )?d[- ]?shackle|overload\w*", re.I)),
    ("unguarded rotating parts or nip points",
     re.compile(r"(coupling|shaft|pulley|gear|belt|roller|roll|chain drive|chain|nip|spindle|chuck|fan|bridle|pinch)"
                r"\W+(?:\w+\W+){0,6}?(guard (is )?(missing|removed|not|open|broken|damaged)|unguarded|without guard|exposed|open)"
                r"|unguarded|guard\w* (missing|removed|bypass\w*)|(limit switch|interlock)\W+(?:\w+\W+){0,3}?bypass\w*|bypass\w* (the )?(limit switch|interlock)", re.I)),
    ("gas, fume or confined-space atmosphere",
     re.compile(r"gas leak|\blpg\b|carbon monoxide|\bco\b gas|confined space|inside the (tank|vessel|furnace|duct)|oxygen (line|leak|cylinder)"
                r"|h2s|ammonia|\bhf\b|flash ?back arrest\w*|acetylene", re.I)),
    ("vehicle and pedestrian conflict",
     re.compile(r"(forklift|truck|trailer|dumper|hydra|loader|vehicle|tractor|crane)\W+(?:\w+\W+){0,8}?"
                r"(pedestrian|walkway|reverse|horn|speed|overload\w*|no (driver|operator)|without (lashing|route|driver))"
                r"|without lashing|lashing (missing|not)", re.I)),
    ("stored pressure release", re.compile(r"hose (burst|damaged|leak)|high pressure|cylinder\w* (not chained|unsecured|without cap)", re.I)),
]

_LOCATION_HINT = re.compile(r"\b(at|near|on|in|below|above|behind|inside|outside)\b|line|mill|bay|shed|furnace|panel|floor|area|section|crane|road", re.I)


@lru_cache(maxsize=1)
def library() -> dict[str, Any]:
    if not DATA.exists():
        return {"meta": {"available": False}, "categories": {}, "practice": {}, "cases": []}
    lib = json.loads(DATA.read_text(encoding="utf-8"))
    lib.setdefault("meta", {})["available"] = True
    return lib


@lru_cache(maxsize=1)
def _index() -> tuple[Bm25Index, list[dict[str, Any]]]:
    cases = library()["cases"]
    docs = []
    for c in cases:
        text = " ".join(filter(None, [c.get("hazard"), " ".join(c.get("tags") or []).replace("_", " "),
                                      c.get("obs"), c.get("area")]))
        docs.append(tokenize(text))
    return Bm25Index(docs), cases


def _axis(observation_type: str | None) -> str | None:
    t = (observation_type or "").upper()
    if "ACT" in t:
        return "ACT"
    if "CONDITION" in t:
        return "CONDITION"
    return None


def similar_cases(text: str, *, observation_type: str | None = None, limit: int = 25,
                  exemplars_only: bool = False) -> list[tuple[dict[str, Any], float]]:
    index, cases = _index()
    axis = _axis(observation_type)
    allow = None
    if axis or exemplars_only:
        allow = {i for i, c in enumerate(cases)
                 if (axis is None or c["type"] == axis) and (not exemplars_only or c.get("ex"))}
    return [(cases[i], s) for i, s in index.search(tokenize(text), limit=limit, allow=allow)]


def _shares(votes: dict[str, float]) -> list[tuple[str, float]]:
    total = sum(votes.values()) or 1.0
    return sorted(((k, v / total) for k, v in votes.items()), key=lambda kv: -kv[1])


def fatal_signals(text: str) -> list[str]:
    return [label for label, rx in FATAL_SIGNALS if rx.search(text or "")]


def stop_candidates(cat: str, tags: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    rules = STOP_MAP.get(cat, [])
    for category, sub, when in rules[1:]:
        if any(t in tags for t in when):
            out.append({"categoryCode": category, "subCategoryCode": sub, "why": f"tag {', '.join(t for t in when if t in tags)}"})
    if rules:
        category, sub, _ = rules[0]
        out.append({"categoryCode": category, "subCategoryCode": sub, "why": "default for this plant category"})
    seen: set[tuple[str, str]] = set()
    return [o for o in out if not ((o["categoryCode"], o["subCategoryCode"]) in seen or seen.add((o["categoryCode"], o["subCategoryCode"])))]


def text_quality(text: str) -> list[str]:
    notes: list[str] = []
    words = re.findall(r"[A-Za-z]+", text or "")
    if len(words) < 6:
        notes.append("The description is very short. Say what is unsafe, exactly where, and what could happen.")
    if not _LOCATION_HINT.search(text or ""):
        notes.append("No exact location in the description (line, equipment, floor or bay). The responsible team needs it to find the spot.")
    if not re.search(r"could|may|might|risk|lead|result|cause|injur|fall|hit|burn|shock|fire", text or "", re.I):
        notes.append("Add what could happen (e.g. 'a person could fall 3 m', 'risk of electric shock'), so the risk is clear.")
    return notes


def suggest(text: str, *, observation_type: str | None = None, limit_examples: int = 5) -> dict[str, Any]:
    """Suggest classification, risk and corrective action for an observation description."""
    lib = library()
    if not lib["meta"].get("available"):
        return {"available": False, "note": "The observation reference library is not installed on this server."}
    hits = similar_cases(text, observation_type=observation_type, limit=25)
    signals = fatal_signals(text)
    if not hits:
        return {"available": True, "matches": 0, "fatalPotentialSignals": signals, "textQuality": text_quality(text),
                "note": "No similar observations in the reference library; classify from the description."}

    top = hits[0][1] or 1.0
    cat_v: dict[str, float] = defaultdict(float)
    plant_v: dict[str, float] = defaultdict(float)
    risk_v: dict[str, float] = defaultdict(float)
    fn_v: dict[str, float] = defaultdict(float)
    tag_v: dict[str, float] = defaultdict(float)
    for c, s in hits:
        w = s / top
        cat_v[c["cat"]] += w
        plant_v[c.get("pc") or c["cat"]] += w
        risk_v["FatalPotential" if c.get("fp") else c["risk"]] += w
        if c.get("fn"):
            fn_v[c["fn"]] += w
        for t in c.get("tags") or []:
            tag_v[t] += w

    cats = _shares(cat_v)
    risks = dict(_shares(risk_v))
    p_high = risks.get("High", 0) + risks.get("FatalPotential", 0)
    p_med = risks.get("Medium", 0)
    # Safety-weighted: a fatal-potential signal in the text, backed by even a
    # small share of fatal-potential look-alikes, is enough to suggest it.
    if risks.get("FatalPotential", 0) >= 0.3 or (signals and p_high >= 0.1):
        level = "FatalPotential"
    elif p_high >= 0.35 or (signals and p_high + p_med >= 0.5):
        level = "High"
    elif p_high + p_med >= 0.40:
        level = "Medium"
    else:
        level = "Low"

    best_cat, best_share = cats[0]
    confidence = "high" if best_share >= 0.6 and len(hits) >= 8 else "medium" if best_share >= 0.4 else "low"
    top_tags = [t for t, _ in sorted(tag_v.items(), key=lambda kv: -kv[1])[:4]]
    catinfo = lib["categories"]

    examples: list[dict[str, Any]] = []
    actions: list[dict[str, str]] = []
    seen_actions: set[str] = set()
    ex_hits = [h for h in hits if h[0].get("ex")]
    if len(ex_hits) < limit_examples:
        ex_hits += [h for h in similar_cases(text, observation_type=observation_type, limit=limit_examples * 2,
                                             exemplars_only=True) if h not in ex_hits]
    for c, _s in ex_hits[: limit_examples]:
        examples.append({
            "ref": c["id"], "category": f"{c['cat']} {catinfo.get(c['cat'], {}).get('label', '')}".strip(),
            "risk": "Fatal Potential" if c.get("fp") else c["risk"], "observation": c.get("obs"),
            "correctiveAction": c.get("action"), "controlLevel": c.get("level"), "area": c.get("area"),
        })
        a = (c.get("action") or "").strip()
        key = a.lower()[:60]
        if a and key not in seen_actions:
            seen_actions.add(key)
            actions.append({"action": a, "controlLevel": c.get("level") or "", "from": c["id"]})

    practice = lib.get("practice", {})
    target_days = (practice.get("targetDays") or {}).get("High" if level == "FatalPotential" else level)
    return {
        "available": True,
        "matches": len(hits),
        "confidence": confidence,
        "category": {"code": best_cat, "label": catinfo.get(best_cat, {}).get("label"), "share": round(best_share, 2)},
        "alternatives": [{"code": k, "label": catinfo.get(k, {}).get("label"), "share": round(v, 2)} for k, v in cats[1:3]],
        # Reviewed labels can differ from how the plant files similar rows (e.g. C2 used as a catch-all).
        "plantUsuallyFiles": (lambda pc: None if pc == best_cat else {"code": pc, "label": catinfo.get(pc, {}).get("label")})(
            _shares(plant_v)[0][0]),
        "risk": {
            "plantLevel": "Fatal Potential" if level == "FatalPotential" else level,
            "safeopsSeverity": SEVERITY_FOR[level],
            "mix": {k: round(v, 2) for k, v in risks.items()},
            "rule": "Fatal potential ⇒ High risk, CRITICAL severity, close the same day",
        },
        "fatalPotentialSignals": signals,
        "responsibleFunction": _shares(fn_v)[0][0] if fn_v else None,
        "targetDays": target_days,
        "hazardTags": top_tags,
        "stopCandidates": stop_candidates(best_cat, top_tags),
        "correctiveActions": actions[:4],
        "examples": examples,
        "textQuality": text_quality(text),
    }


def category_overview(code: str | None = None) -> dict[str, Any]:
    lib = library()
    cats = lib.get("categories", {})
    if code:
        return cats.get(code) or {}
    return {k: {"label": v.get("label"), "type": v.get("type"), "count": v.get("count")} for k, v in cats.items()}
