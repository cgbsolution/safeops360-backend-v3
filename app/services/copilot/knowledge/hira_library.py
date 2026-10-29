"""HIRA reference library: reviewed plant HIRA rows, the plant's own scoring
method, and good-practice control ideas by hazard type.

data/hira_library.json is built by scripts/copilot_knowledge/build_library.py
from a plant HIRA workbook (activity → hazard → consequence → controls → score)
after a row-by-row review; corrected rows carry `reviewFlags` that say what was
changed and why.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.copilot.knowledge.text_index import Bm25Index, tokenize

DATA = Path(__file__).with_name("data") / "hira_library.json"

# Hazard type → SafeOps hazard-master categories (HiraHazard.category), so the
# copilot can point at master hazards the user can pick in the editor.
MASTER_CATEGORIES: dict[str, list[str]] = {
    "STEAM_EXPLOSION": ["fire_explosion", "thermal"], "MOLTEN_METAL_BREAKOUT": ["thermal", "fire_explosion"],
    "FIRE_EXPLOSION": ["fire_explosion"], "FIRE": ["fire_explosion"], "BURN_SCALD": ["thermal"],
    "VEHICULAR": ["transportation"], "EQUIPMENT_FAILURE": ["lifting", "mechanical"], "MACHINERY_TOOL": ["mechanical"],
    "CRUSHING": ["mechanical"], "GRAVITY_FALLING_OBJECT": ["mechanical", "lifting"], "ERGONOMIC": ["ergonomic"],
    "DUST_FUME": ["chemical", "health", "environmental"], "HEAT_STRESS": ["physical", "thermal"], "RADIATION": ["radiation"],
    "PRESSURE": ["pressure"], "VIBRATION": ["mechanical", "physical"], "SLIP_TRIP": ["physical"], "ELECTRICAL": ["electrical"],
    "BIOLOGICAL": ["biological"], "PSYCHOSOCIAL": ["psychosocial"], "DISTRACTION": ["behavioral"], "COMMUNICATION": ["behavioral"],
}

# Good-practice controls above "follow the SOP + wear PPE", by hazard type.
# These are SafeOps suggestions, not the plant's record: the copilot must say so.
CONTROL_IDEAS: dict[str, list[tuple[str, str]]] = {
    "EQUIPMENT_FAILURE": [
        ("ENGINEERING", "Overload limit and over-hoist/over-travel limit switches on the crane, tested each shift"),
        ("ENGINEERING", "Hook safety latch and anti-collision devices on cranes serving hot metal bays"),
        ("ENGINEERING", "Audible hooter with flashing beacon during every hot-metal or heavy lift travel"),
        ("ADMINISTRATIVE", "Exclusion zone under the load path; no one under a suspended load (enforced by the signaller)"),
        ("ADMINISTRATIVE", "Pre-use check of wire ropes, slings and ladle hooks; NDT of ladle trunnions and hooks at set intervals"),
        ("ADMINISTRATIVE", "Lifting tackle register with SWL tags and third-party examination (Factories Act Sec. 29)"),
    ],
    "MACHINERY_TOOL": [
        ("ENGINEERING", "Fixed or interlocked guards on rotating parts and nip points; guard removal only under LOTO"),
        ("ENGINEERING", "Emergency stop / pull-cord within reach along trolleys and conveyors"),
        ("ADMINISTRATIVE", "Isolation (LOTO) before clearing jams or reaching into moving equipment"),
    ],
    "CRUSHING": [
        ("ENGINEERING", "Travel alarm with beacon and end-stops on transfer trolleys and doors; emergency brake tested"),
        ("ENGINEERING", "Physical barrier or marked no-standing zone along the trolley track"),
        ("ADMINISTRATIVE", "Operator confirms the track is clear before moving (positive communication)"),
    ],
    "GRAVITY_FALLING_OBJECT": [
        ("ENGINEERING", "Use purpose-made lifting boxes/bins with lids instead of loose wire-rope slinging of material"),
        ("ENGINEERING", "Toe boards and kick plates on platforms where material is handled"),
        ("ADMINISTRATIVE", "Barricade the drop zone and keep people out while lifting"),
    ],
    "BURN_SCALD": [
        ("ELIMINATION", "Remove people from the splash zone: automatic or remote temperature/sampling probes (sub-lance)"),
        ("ENGINEERING", "Splash shields, closed dog-house doors and operator cabins during tapping, lancing and additions"),
        ("ENGINEERING", "Dry, pre-heated additions and tools only; moisture check before charging (prevents eruptions)"),
        ("ADMINISTRATIVE", "Keep a defined distance during tapping/teeming; only trained, authorised operators at the spout"),
        ("PPE", "Aluminised jacket and trousers, face shield with melting goggles, leg guards, FR clothing"),
    ],
    "FIRE": [
        ("ENGINEERING", "Fixed fire detection and hydrant/monitor coverage of ladle and slag paths; fire-resistant hydraulic fluid"),
        ("ENGINEERING", "Shield hydraulic and cable runs exposed to splash; slag pot areas kept dry with drained pits"),
        ("ADMINISTRATIVE", "No combustibles in the hot-metal path; daily housekeeping check of the bay"),
        ("ADMINISTRATIVE", "Extinguishers suited to metal fires (Class D) and trained responders on each shift"),
    ],
    "FIRE_EXPLOSION": [
        ("ENGINEERING", "Flame-failure device and interlocked purge on ladle-heating burners; gas leak detection"),
        ("ADMINISTRATIVE", "Burner light-up procedure with gas test; keep the burner area free of combustibles"),
    ],
    "STEAM_EXPLOSION": [
        ("ENGINEERING", "Water-flow and temperature monitoring with alarms on water-cooled panels; leak detection with auto-isolation"),
        ("ENGINEERING", "Dry pits and covered drains below ladle and furnace areas"),
        ("ADMINISTRATIVE", "Stop and isolate water before panel maintenance; no water near molten metal or slag"),
    ],
    "MOLTEN_METAL_BREAKOUT": [
        ("ENGINEERING", "Ladle shell thermal scanning (thermography) and refractory wear tracking with removal limits"),
        ("ADMINISTRATIVE", "Ladle campaign life limits and pre-use inspection; emergency pour-back plan"),
    ],
    "VEHICULAR": [
        ("ENGINEERING", "Segregated pedestrian walkways and physical barriers; reversing camera, alarm and horn on trucks and loaders"),
        ("ENGINEERING", "Wheel chocks and parking-brake interlock at loading points"),
        ("ADMINISTRATIVE", "Plant traffic plan with speed limits and a banksman for reversing in the charging bay"),
    ],
    "ERGONOMIC": [
        ("ENGINEERING", "Mechanical aids (hoist, tilting stand, trolley) for repetitive lifting of electrodes, plates and funnels"),
        ("ADMINISTRATIVE", "Manual handling training and job rotation; limit single-person lifts (Factories Act Sec. 34)"),
    ],
    "DUST_FUME": [
        ("ENGINEERING", "Local exhaust or fume extraction at the source (dog house, hood, bin top); keep dampers maintained"),
        ("SUBSTITUTION", "Vacuum or wet sweeping instead of dry brooming"),
        ("ADMINISTRATIVE", "Periodic dust/fume monitoring and medical surveillance of exposed workers"),
        ("PPE", "FFP2/FFP3 respirator fit-tested for the task"),
    ],
    "HEAT_STRESS": [
        ("ENGINEERING", "Cooling fans or spot coolers and shaded rest points near the furnace"),
        ("ADMINISTRATIVE", "Work-rest cycle by heat index, drinking water and ORS, acclimatisation of new workers"),
    ],
    "RADIATION": [
        ("ENGINEERING", "Heat-reflective screens at inspection points"),
        ("PPE", "IR-filtering face shield or melting goggles of the correct shade"),
    ],
    "PRESSURE": [
        ("ENGINEERING", "Pressure relief and rated hoses with whip restraints; gauges on the skid"),
        ("ADMINISTRATIVE", "De-pressurise and isolate before work; inspect hoses and fittings on a schedule (Factories Act Sec. 31)"),
    ],
    "SLIP_TRIP": [
        ("ENGINEERING", "Handrails on both sides of stairs, anti-skid nosing and adequate lighting"),
        ("ADMINISTRATIVE", "Clean spills at once; keep walkways clear and marked"),
    ],
    "ELECTRICAL": [
        ("ENGINEERING", "Close and lock panels, earth-leakage protection, insulated and earthed equipment"),
        ("ADMINISTRATIVE", "Electrical work only by licensed persons under permit and LOTO"),
    ],
    "VIBRATION": [
        ("SUBSTITUTION", "Low-vibration tools or remote demolition machine for brick and slag removal"),
        ("ADMINISTRATIVE", "Limit exposure time per shift and rotate operators"),
    ],
}


@lru_cache(maxsize=1)
def library() -> dict[str, Any]:
    if not DATA.exists():
        return {"meta": {"available": False}, "entries": [], "hazardTypeLabels": {}}
    lib = json.loads(DATA.read_text(encoding="utf-8"))
    lib.setdefault("meta", {})["available"] = True
    return lib


@lru_cache(maxsize=1)
def _index() -> tuple[Bm25Index, list[dict[str, Any]]]:
    entries = library()["entries"]
    docs = [tokenize(" ".join(filter(None, [e.get("activity"), e.get("activity"), e.get("hazardText"),
                                            e.get("hazardType", "").replace("_", " "), e.get("consequence"),
                                            e.get("section")])))
            for e in entries]
    return Bm25Index(docs), entries


def plant_method(sp: int, lh: int, cn: int, de: int, concerns: dict[str, bool] | None = None,
                 threshold: float | None = None) -> dict[str, Any]:
    """The plant's QSD/24/F scoring: GT = SP x (LH x CN) x DE.

    A risk is significant (not acceptable) when GT is above the sheet's
    significant score, or when any qualitative concern (legal, domino, chronic,
    emergency) is Y — whatever the score.
    """
    thr = threshold if threshold is not None else float(library().get("meta", {}).get("significantScore") or 3.33)
    sv = lh * cn
    gt = sp * sv * de
    flags = [k for k, v in (concerns or {}).items() if v]
    reasons = []
    if gt > thr:
        reasons.append(f"GT {gt} is above the significant score {thr:g}")
    if flags:
        reasons.append("qualitative concern: " + ", ".join(flags) + " (stays significant whatever the score)")
    return {"SP": sp, "LH": lh, "CN": cn, "SV": sv, "DE": de, "GT": gt, "significantScore": thr,
            "significant": bool(reasons), "acceptable": not reasons, "reasons": reasons}


def search(query: str, *, hazard_type: str | None = None, section: str | None = None,
           significant_only: bool = False, limit: int = 8) -> list[dict[str, Any]]:
    index, entries = _index()
    allow = None
    if hazard_type or section or significant_only:
        sec = (section or "").lower()
        allow = {i for i, e in enumerate(entries)
                 if (not hazard_type or e["hazardType"] == hazard_type)
                 and (not sec or sec in (e.get("section") or "").lower())
                 and (not significant_only or not e["acceptable"])}
    q = tokenize(query)
    hits = index.search(q, limit=limit * 3, allow=allow) if q else [(i, 0.0) for i in sorted(allow or [])[: limit * 3]]
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for i, score in hits:
        e = entries[i]
        key = ((e.get("activity") or "").lower(), e["hazardType"], (e.get("consequence") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({**e, "matchScore": round(score, 2)})
        if len(out) >= limit:
            break
    return out


def control_ideas(hazard_type: str) -> list[dict[str, str]]:
    return [{"hierarchy": h, "description": d, "source": "SafeOps good practice (not from the plant HIRA)"}
            for h, d in CONTROL_IDEAS.get(hazard_type, [])]


def hazard_types() -> dict[str, str]:
    return dict(library().get("hazardTypeLabels") or {})
