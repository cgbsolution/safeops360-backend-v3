"""Recommendations for calibration v2 (R8): proportionate, capped, de-duplicated.

* 0 recommendations when every hazard is resolved (acceptable / ALARP).
* At most 1 per hazard; at most 3 per entry when the entry is LOW/MODERATE,
  5 when HIGH/CRITICAL (after de-duplication).
* Each states the target hazard(s), the expected effect, cost band, owner
  role and default due date, ranked by risk reduction per unit of effort.
* No HIGH-cost item unless the hazard's residual is HIGH or above; at most one
  capex item per entry, always with an interim measure.
* De-duplicated against existing controls, open plant CAPAs (linked, not
  re-raised) and sibling entries (flagged as a study-level common control).
* Generic filler (awareness training, update SOP...) only where the gap is shown.
* Only items that move a hazard out of NOT_ACCEPTABLE raise a CAPA.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.services.hira_calibration import risk_engine as eng

# id, family, hierarchy, cost band, description
CATALOG: list[tuple[str, str, str, str, str]] = [
    (
        "LIFT-ENG-1",
        "LIFTING",
        "ENGINEERING",
        "MEDIUM",
        "Hard-barricaded exclusion zone under the crane travel path, with an interlocked or chained access point "
        "so no one can stand under a suspended load",
    ),
    (
        "LIFT-ENG-2",
        "LIFTING",
        "ENGINEERING",
        "MEDIUM",
        "Overload limiter and over-hoist / over-travel limit switches on the crane, function-tested each shift",
    ),
    (
        "LIFT-ENG-3",
        "LIFTING",
        "ENGINEERING",
        "MEDIUM",
        "Purpose-made lifting frame / spreader with positive-locking hooks instead of loose slings",
    ),
    (
        "LIFT-ENG-4",
        "LIFTING",
        "ENGINEERING",
        "MEDIUM",
        "Radio remote control so the operator works from outside the drop zone",
    ),
    (
        "LIFT-ADM-1",
        "LIFTING",
        "ADMINISTRATIVE",
        "LOW",
        "Written lift plan for this routine lift (SWL, slinging method, travel path, no one under the load) with a "
        "named competent rigger and signaller",
    ),
    (
        "HT-ENG-1",
        "HEIGHT",
        "ENGINEERING",
        "MEDIUM",
        "Fixed guard rails with toe boards around the platform and a lockable grid cover over the opening",
    ),
    (
        "HT-ENG-2",
        "HEIGHT",
        "ENGINEERING",
        "MEDIUM",
        "Permanent lifeline / anchor points with full body harness fall arrest",
    ),
    (
        "HT-ADM-1",
        "HEIGHT",
        "ADMINISTRATIVE",
        "LOW",
        "Work-at-height permit for any work outside the guarded platform",
    ),
    (
        "HT-ENG-3",
        "HEIGHT",
        "ENGINEERING",
        "LOW",
        "Crawling boards / roof ladders, and covers over fragile sheets and skylights",
    ),
    (
        "HT-ENG-4",
        "HEIGHT",
        "ENGINEERING",
        "MEDIUM",
        "Safety net or birdcage scaffold under the fragile-roof work area",
    ),
    (
        "DU-ENG-1",
        "DUST",
        "ENGINEERING",
        "HIGH",
        "Local exhaust ventilation (LEV) hood at the discharge point",
    ),
    ("DU-ENG-2", "DUST", "ENGINEERING", "MEDIUM", "Enclosed discharge chute or water-mist dust suppression"),
    ("DU-PPE-1", "DUST", "PPE", "LOW", "Fit-tested FFP2 / N95 respirator during the dusty task"),
    (
        "SH-SUB-1",
        "SHARP",
        "SUBSTITUTION",
        "LOW",
        "Replace the open knife with a safety cutter (shrouded or retractable blade)",
    ),
    ("SH-PPE-1", "SHARP", "PPE", "LOW", "Cut-resistant gloves (EN 388 level C or above) for cutting tasks"),
    (
        "MH-ENG-1",
        "MANUAL_HANDLING",
        "ENGINEERING",
        "MEDIUM",
        "Mechanical aid (trolley, hoist) for loads over 25 kg",
    ),
    (
        "MH-ADM-1",
        "MANUAL_HANDLING",
        "ADMINISTRATIVE",
        "LOW",
        "Weight limit and team-lift rule for manual loads, written into the work instruction",
    ),
    (
        "FO-ENG-1",
        "FALLING_OBJECT",
        "ENGINEERING",
        "LOW",
        "Toe boards / mesh at the platform edge and a barricaded drop zone below",
    ),
    (
        "FO-ADM-1",
        "FALLING_OBJECT",
        "ADMINISTRATIVE",
        "LOW",
        "No one below during the task: drop zone cordoned and signed",
    ),
    ("MO-ENG-1", "MOLTEN", "ENGINEERING", "MEDIUM", "Splash shields at the tapping / pouring point"),
    (
        "MO-ENG-2",
        "MOLTEN",
        "ENGINEERING",
        "HIGH",
        "Mechanised sampling and temperature probe (no manual approach)",
    ),
    (
        "MO-ADM-1",
        "MOLTEN",
        "ADMINISTRATIVE",
        "LOW",
        "Moisture-free check of scrap, additions and tools before contact with molten metal",
    ),
    (
        "MO-PPE-1",
        "MOLTEN",
        "PPE",
        "MEDIUM",
        "Aluminised jacket and trousers, face shield with melting goggles and leg guards",
    ),
    (
        "EL-ENG-1",
        "ELECTRICAL",
        "ENGINEERING",
        "LOW",
        "Lockable isolation points with test points at each panel for zero-energy verification",
    ),
    ("EL-ENG-2", "ELECTRICAL", "ENGINEERING", "LOW", "30 mA RCD / ELCB on portable equipment circuits"),
    (
        "EL-ENG-3",
        "ELECTRICAL",
        "ENGINEERING",
        "MEDIUM",
        "Insulating shrouds / barriers over live parts in the panel",
    ),
    (
        "EL-ADM-1",
        "ELECTRICAL",
        "ADMINISTRATIVE",
        "LOW",
        "Electrical permit with LOTO; work only by licensed electricians",
    ),
    (
        "VE-ENG-1",
        "VEHICLE",
        "ENGINEERING",
        "MEDIUM",
        "Physically segregated pedestrian walkway with barriers",
    ),
    (
        "VE-ENG-2",
        "VEHICLE",
        "ENGINEERING",
        "MEDIUM",
        "Reversing camera / proximity sensor and blue-spot lights",
    ),
    (
        "VE-ADM-1",
        "VEHICLE",
        "ADMINISTRATIVE",
        "LOW",
        "Speed limit and banksman for reversing in congested bays",
    ),
    (
        "MA-ENG-1",
        "MACHINERY",
        "ENGINEERING",
        "MEDIUM",
        "Fixed or interlocked guards on rotating parts and nip points",
    ),
    ("MA-ENG-2", "MACHINERY", "ENGINEERING", "HIGH", "Light curtain or two-hand control at the press"),
    (
        "MA-ADM-1",
        "MACHINERY",
        "ADMINISTRATIVE",
        "LOW",
        "LOTO before any guard is removed for clearing or maintenance",
    ),
    (
        "CS-ENG-1",
        "CONFINED_SPACE",
        "ENGINEERING",
        "MEDIUM",
        "Forced ventilation with continuous multi-gas monitoring during entry",
    ),
    ("CS-ENG-2", "CONFINED_SPACE", "ENGINEERING", "MEDIUM", "Tripod and winch rescue set at the entry point"),
    (
        "CS-ADM-1",
        "CONFINED_SPACE",
        "ADMINISTRATIVE",
        "LOW",
        "Confined-space entry permit with gas test, trained attendant and rescue plan",
    ),
    (
        "CH-SUB-1",
        "CHEMICAL",
        "SUBSTITUTION",
        "MEDIUM",
        "Substitute with a less hazardous chemical where the process allows",
    ),
    (
        "CH-ENG-1",
        "CHEMICAL",
        "ENGINEERING",
        "MEDIUM",
        "Closed transfer (pump / dispensing system) instead of manual pouring",
    ),
    ("CH-ENG-2", "CHEMICAL", "ENGINEERING", "LOW", "Eyewash and safety shower within 10 seconds' reach"),
    (
        "CH-PPE-1",
        "CHEMICAL",
        "PPE",
        "LOW",
        "Chemical-resistant gloves, goggles / face shield and apron per the SDS",
    ),
    (
        "ST-ENG-1",
        "SLIP_TRIP",
        "ENGINEERING",
        "MEDIUM",
        "Anti-slip flooring / grating and adequate lighting on the route",
    ),
    (
        "ST-ADM-1",
        "SLIP_TRIP",
        "ADMINISTRATIVE",
        "LOW",
        "Housekeeping schedule with spillage clean-up and floor marking",
    ),
    (
        "FE-ENG-1",
        "FIRE_EXPLOSION",
        "ENGINEERING",
        "HIGH",
        "Gas leak detection with automatic shut-off on the fuel line",
    ),
    ("FE-ENG-2", "FIRE_EXPLOSION", "ENGINEERING", "LOW", "Flashback arrestors on gas cutting sets"),
    (
        "FE-ADM-1",
        "FIRE_EXPLOSION",
        "ADMINISTRATIVE",
        "LOW",
        "Hot-work permit with fire watch and extinguisher at the job",
    ),
    (
        "PR-ENG-1",
        "PRESSURE",
        "ENGINEERING",
        "LOW",
        "Pressure relief valve and whip-check restraints on hoses",
    ),
    (
        "PR-ADM-1",
        "PRESSURE",
        "ADMINISTRATIVE",
        "LOW",
        "Depressurise and bleed before breaking any line (written procedure)",
    ),
    ("NO-ENG-1", "NOISE", "ENGINEERING", "HIGH", "Acoustic enclosure / silencer at the noise source"),
    (
        "NO-PPE-1",
        "NOISE",
        "PPE",
        "LOW",
        "Ear muffs or plugs rated for the measured noise level in marked zones",
    ),
    ("HE-ENG-1", "HEAT", "ENGINEERING", "MEDIUM", "Spot cooling / man-coolers at hot workstations"),
    ("HE-ADM-1", "HEAT", "ADMINISTRATIVE", "LOW", "Work-rest cycle, drinking water and shaded rest area"),
    ("TH-ENG-1", "THERMAL", "ENGINEERING", "MEDIUM", "Insulation or guarding on hot surfaces"),
]

CATALOG += [
    (
        "FO-ENG-2",
        "FALLING_OBJECT",
        "ENGINEERING",
        "MEDIUM",
        "Load backrest extension on the forklift and loads secured / stretch-wrapped before travel",
    ),
    (
        "FO-ADM-2",
        "FALLING_OBJECT",
        "ADMINISTRATIVE",
        "LOW",
        "No one in the load path; stack height and tilt-back rules in the operating procedure",
    ),
    (
        "PR-ENG-2",
        "PRESSURE",
        "ENGINEERING",
        "LOW",
        "Burst sleeves / shields on high-pressure hydraulic hoses and fittings",
    ),
    (
        "PR-ADM-2",
        "PRESSURE",
        "ADMINISTRATIVE",
        "LOW",
        "Never feel for hydraulic leaks by hand; isolate and bleed the circuit before work",
    ),
]

# A catalogue item is offered only when the hazard text names its setting.
REQUIRES: dict[str, tuple[str, ...]] = {
    "FO-ENG-1": (
        "platform",
        "height",
        "edge",
        "scaffold",
        "bin",
        "overhead",
        "roof",
        "opening",
        "above",
        "stack",
        "rack",
        "mezzanine",
    ),
    "FO-ENG-2": ("fork", "forklift", "pallet", "stack", "reach truck"),
    "FO-ADM-2": ("fork", "forklift", "pallet", "stack", "reach truck"),
    "PR-ENG-1": (
        "gas",
        "air",
        "compressed",
        "steam",
        "vessel",
        "cylinder",
        "pneumatic",
        "receiver",
        "boiler",
    ),
    "PR-ENG-2": ("hydraulic", "hose", "oil", "injection"),
    "PR-ADM-2": ("hydraulic", "injection"),
    "LIFT-ENG-4": ("crane", "eot", "hoist"),
    "HT-ENG-1": ("platform", "edge", "opening", "bin", "pit", "floor", "mezzanine", "walkway"),
    "HT-ENG-3": ("fragile", "roof", "skylight", "sheet"),
    "HT-ENG-4": ("fragile", "roof", "skylight"),
}
# Never put water near molten metal (steam explosion).
_WATER = ("water", "mist", "wet ", "wetting", "spray")
_MOLTEN = ("molten", "hot metal", "liquid metal", "slag", "ladle", "furnace", "tapping", "steam explosion")
_GROUP = {
    "ELIMINATION": "E",
    "SUBSTITUTION": "E",
    "ENGINEERING": "ENG",
    "ADMINISTRATIVE": "ADM",
    "PPE": "PPE",
}

INTERIM = {
    "LIFTING": "Interim: supervisor present for every lift and no one under the load until it is installed",
    "HEIGHT": "Interim: harness tied off to a temporary anchor and a second person present",
    "DUST": "Interim: fit-tested respirators and limited time at the discharge point",
    "MOLTEN": "Interim: increased stand-off distance and full molten-metal PPE",
    "ELECTRICAL": "Interim: work only under an electrical permit with LOTO",
    "MACHINERY": "Interim: LOTO for any intervention and a temporary fixed guard",
    "VEHICLE": "Interim: banksman and cones to separate people from vehicles",
    "FIRE_EXPLOSION": "Interim: portable gas monitor and hot-work permit",
    "NOISE": "Interim: hearing protection zone and time limits",
}

_STOP = {
    "the",
    "and",
    "for",
    "with",
    "each",
    "every",
    "from",
    "this",
    "that",
    "all",
    "any",
    "only",
    "per",
    "use",
    "used",
    "into",
    "onto",
    "over",
    "under",
    "before",
    "after",
    "during",
    "where",
    "when",
    "not",
    "are",
    "its",
    "out",
    "off",
    "one",
    "two",
    "via",
    "done",
    "kept",
    "place",
    "provided",
    "available",
    "ensure",
    "proper",
}
_SYN = {
    "inspection": "exam",
    "inspected": "exam",
    "examination": "exam",
    "examined": "exam",
    "check": "exam",
    "checks": "exam",
    "checked": "exam",
    "test": "exam",
    "tested": "exam",
    "testing": "exam",
    "cranes": "crane",
    "eot": "crane",
    "hoist": "crane",
    "barricade": "barrier",
    "barricaded": "barrier",
    "barricading": "barrier",
    "cordon": "barrier",
    "cordoned": "barrier",
    "exclusion": "barrier",
    "fence": "barrier",
    "railing": "rail",
    "railings": "rail",
    "guardrail": "rail",
    "handrail": "rail",
    "rails": "rail",
    "slings": "sling",
    "gloves": "glove",
    "respirator": "mask",
    "respirators": "mask",
    "sop": "procedure",
    "instruction": "procedure",
    "instructions": "procedure",
    "procedures": "procedure",
    "permits": "permit",
    "limiter": "limit",
    "switches": "switch",
    "harnesses": "harness",
    "thirdparty": "third",
    "lev": "exhaust",
    "ventilation": "exhaust",
    "extraction": "exhaust",
}


def tokens(text: str | None) -> set[str]:
    t = (text or "").lower().replace("third-party", "third party").replace("pre-use", "pre use")
    out = set()
    for w in re.findall(r"[a-z]{3,}", t):
        if w in _STOP:
            continue
        w = _SYN.get(w, w)
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def similarity(a: str | None, b: str | None) -> float:
    """Jaccard similarity of the normalised word sets (synonyms folded)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def linked(a: str | None, b: str | None, threshold: float) -> bool:
    """A CAPA or a sibling's control is the same action when most of the shorter
    text's words (at least 3) appear in the other."""
    ta, tb = tokens(a), tokens(b)
    common = ta & tb
    return len(common) >= 3 and len(common) / max(1, min(len(ta), len(tb))) >= threshold


def same_control(
    cand_hierarchy: str, cand_text: str, c_hierarchy: str | None, c_text: str | None, threshold: float
) -> bool:
    """An existing control covers a candidate when both are in the same hierarchy
    group (a camera is not a banksman) and their wording overlaps enough."""
    return (
        _GROUP.get((cand_hierarchy or "").upper()) == _GROUP.get((c_hierarchy or "").upper())
        and similarity(cand_text, c_text) >= threshold
    )


@dataclass
class Candidate:
    id: str
    family: str
    hierarchy: str
    cost: str
    description: str
    source: str = "catalogue"


def catalogue_for(family: str) -> list[Candidate]:
    return [Candidate(i, f, h, c, d) for i, f, h, c, d in CATALOG if f == family]


def is_filler(text: str, cfg: dict[str, Any]) -> bool:
    t = (text or "").lower()
    return any(k in t for k in cfg["recommendations"]["genericFiller"])


def _effect_text(dl: int, ds: int) -> str:
    parts = [f"L -{dl}" if dl else "", f"S -{ds}" if ds else ""]
    return ", ".join(p for p in parts if p) or "completes the ALARP checklist"


def _cand_key(cand: Candidate) -> str:
    return cand.id if cand.source == "catalogue" else f"llm:{cand.description.lower()[:60]}"


def recommend(
    hazard_results: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    strict: bool,
    entry_level: str | None,
    open_capas: list[dict[str, Any]] | None = None,
    sibling_recommended: list[dict[str, Any]] | None = None,
    extra_candidates: list[Candidate] | None = None,
    entry_max_severity: int | None = None,
) -> dict[str, Any]:
    """Pick the recommendations for one entry.

    hazard_results: from review_v2 (hazard, controls, residual, tolerability ...).
    A hazard gets a recommendation only when its residual route is CONTROLS, or
    the ALARP checklist lacks an engineering control. Never from the inherent
    rating alone (round 2 G), never for S<=3 hazards whose controls are not
    captured (round 2 C: hr["noRecommendations"]).
    extra_candidates: proposed controls (LLM draft or the entry's own proposed
    recommendations), filtered the same way as the catalogue (round 2 E).
    """
    rc = cfg["recommendations"]
    weight = rc["costWeight"]
    open_capas = open_capas or []
    sibling_recommended = sibling_recommended or []
    picks: dict[str, dict[str, Any]] = {}
    linked_capas: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    needing: dict[str, dict[str, Any]] = {}
    per_hazard: list[
        tuple[dict[str, Any], eng.Hazard, str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]
    ] = []
    no_credit_items: list[dict[str, Any]] = []
    molten_entry = any(
        hr["hazard"].family == "MOLTEN" or any(w in hr["hazard"].text.lower() for w in _MOLTEN)
        for hr in hazard_results
        if hr.get("hazard") is not None
    )
    hidx = {x: i for i, x in enumerate(eng.HIERARCHY)}

    def pool_for(h: eng.Hazard, fam: str) -> list[Candidate]:
        htext = h.text.lower()
        pool = [
            c for c in catalogue_for(fam) if not REQUIRES.get(c.id) or any(w in htext for w in REQUIRES[c.id])
        ]
        for x in extra_candidates or []:
            fams = eng.control_families(x.description)
            if x.family in (fam, "GENERAL") or fam in fams or fams & ({fam} | eng.RELATED.get(fam, set())):
                pool.append(x)
        return pool

    def admissible(cand: Candidate, h: eng.Hazard, controls: list[eng.Control], res: dict[str, Any]) -> bool:
        if molten_entry and any(w in f" {cand.description.lower()} " for w in _WATER):
            skipped.append({"candidate": cand.description, "reason": "water near molten metal"})
            return False
        dup = next(
            (
                c
                for c in controls
                if same_control(
                    cand.hierarchy, cand.description, c.hierarchy, c.description, rc["dedupeSimilarity"]
                )
            ),
            None,
        )
        if dup is not None:
            skipped.append({"candidate": cand.description, "reason": f"already in place: {dup.description}"})
            return False
        capa = next(
            (k for k in open_capas if linked(cand.description, k.get("text"), rc.get("linkSimilarity", 0.6))),
            None,
        )
        if capa is not None:
            linked_capas[capa["id"]] = {
                **capa,
                "hazards": sorted({*linked_capas.get(capa["id"], {}).get("hazards", []), h.name}),
            }
            return False
        if is_filler(cand.description, cfg):
            has_admin = any(
                (c.hierarchy or "").upper() == "ADMINISTRATIVE" and eng.applies_to(c, h)[0]
                for c in controls
                if not c.planned
            )
            if has_admin:
                skipped.append({"candidate": cand.description, "reason": "generic filler; no gap shown"})
                return False
        if cand.cost in ("HIGH", "VERY_HIGH") and eng.level_index(res["level"]) < eng.level_index(
            rc["highCostMinResidual"]
        ):
            skipped.append(
                {"candidate": cand.description, "reason": "not reasonably practicable at this risk"}
            )
            return False
        return True

    def trial(hr: dict[str, Any], h: eng.Hazard, cands: list[Candidate]) -> dict[str, Any]:
        kept_existing = (
            [] if hr.get("basisIncludesControls") else [c for c in hr["controls"] if not c.planned]
        )
        new_controls = [
            eng.Control(
                key=None,
                hierarchy=c.hierarchy,
                description=c.description,
                effectiveness="EFFECTIVE",
                evidence_attached=True,
                hazard_keys=(h.key,),
                verification_method="planned verification",
            )
            for c in cands
        ]
        base_l, base_s = hr["basis"]
        r = eng.residual_from_controls(base_l, base_s, [*kept_existing, *new_controls], cfg, hazard=h)
        return eng.rate(r["likelihood"], r["severity"], cfg)

    def tol_after(hr: dict[str, Any], new_rate: dict[str, Any], cands: list[Candidate]) -> dict[str, Any]:
        # ALARP records (elimination considered, review date) come with the
        # suggested defaults; the controls themselves are what this adds.
        alarp_after = {
            **hr["alarp"],
            "eliminationConsidered": True,
            "reviewDateSet": True,
            "engineeringInPlaceOrPlanned": hr["alarp"].get("engineeringInPlaceOrPlanned")
            or any(c.hierarchy == "ENGINEERING" for c in cands),
        }
        return eng.tolerability(
            new_rate,
            credible_s=hr.get("credibleS"),
            alarp=alarp_after,
            strict=strict,
            cfg=cfg,
            verified_control=True,
            rating_credit=hr.get("ratingCredit", True),
        )

    for hr in hazard_results:
        tol = hr.get("tolerability")
        if hr.get("collapsed") or not tol or hr.get("noRecommendations") or tol.get("overridden"):
            continue
        h: eng.Hazard = hr["hazard"]
        fam = h.family
        res = hr["residual"]
        if res is None:
            continue  # never from the inherent rating alone
        sev = max(res["severity"], hr.get("credibleS") or 0)
        # Plant-index sheet rating: no control can move the rating. Offer one
        # improvement for a serious open hazard, tagged as such (round 2 H).
        if not hr.get("ratingCredit", True):
            if sev >= 4 and not eng.resolved(tol) and hr.get("plannedCovers") is not True:
                pool = [c for c in pool_for(h, fam) if admissible(c, h, hr["controls"], res)]
                pool.sort(
                    key=lambda c: (
                        c.hierarchy != "ENGINEERING",
                        hidx.get(c.hierarchy, 9),
                        rc["costWeight"].get(c.cost, 2),
                        c.id,
                    )
                )
                if pool:
                    no_credit_items.append({"hr": hr, "h": h, "fam": fam, "res": res, "cand": pool[0]})
            continue
        if eng.resolved(tol) or tol["route"] not in ("CONTROLS", "ALARP_CHECKLIST"):
            continue
        need_eng_only = tol["route"] == "ALARP_CHECKLIST"
        if need_eng_only and "engineeringInPlaceOrPlanned" not in tol["missing"]:
            continue  # the checklist gap is a record, not a control
        if hr.get("plannedCovers"):
            continue  # a planned control with a date already covers this hazard
        needing[h.key] = {"key": h.key, "name": h.name, "residual": res}
        scored = []
        for cand in pool_for(h, fam):
            if need_eng_only and cand.hierarchy != "ENGINEERING":
                continue
            if not admissible(cand, h, hr["controls"], res):
                continue
            new_rate = trial(hr, h, [cand])
            gain = res["score"] - new_rate["score"]
            if gain <= 0 and not need_eng_only:
                continue
            scored.append(
                {
                    "cand": cand,
                    "gain": gain,
                    "per_effort": gain / weight.get(cand.cost, 2),
                    "after": new_rate,
                    "tolAfter": tol_after(hr, new_rate, [cand]),
                }
            )
        if not scored:
            continue
        # A serious hazard (S>=4) needs an engineering control for ALARP anyway:
        # prefer one over a cheaper procedure that would leave that gap open.
        serious = sev >= 4 and not hr["alarp"].get("engineeringInPlaceOrPlanned")
        scored.sort(
            key=lambda x: (
                not eng.resolved(x["tolAfter"]) and x["tolAfter"]["route"] != "OVERRIDE",
                serious and x["cand"].hierarchy not in ("ELIMINATION", "SUBSTITUTION", "ENGINEERING"),
                -x["per_effort"],
                hidx.get(x["cand"].hierarchy, 9),
                x["cand"].id,
            )
        )
        per_hazard.append((hr, h, fam, res, tol, scored))

    def effect_text(res: dict[str, Any], after: dict[str, Any]) -> str:
        return _effect_text(res["likelihood"] - after["likelihood"], res["severity"] - after["severity"])

    def add_pick(
        key: str,
        cand: Candidate,
        h: eng.Hazard,
        fam: str,
        res: dict[str, Any],
        after: dict[str, Any],
        effect: str,
        moves_out: bool,
        rank: tuple[int, float],
        kind: str | None = None,
    ) -> None:
        if key in picks:
            pk = picks[key]
            pk["targetHazards"].append({"key": h.key, "name": h.name})
            pk["raisesCapa"] = pk["raisesCapa"] or moves_out
            if pk["kind"] != "IMPROVEMENT_NO_RATING_CREDIT":
                pk["kind"] = "CAPA" if pk["raisesCapa"] else "IMPROVEMENT_NOTE"
            pk["_rank"] = max(pk["_rank"], rank)
            return
        capex = cand.cost in ("HIGH", "VERY_HIGH")
        picks[key] = {
            "id": key,
            "hierarchy": cand.hierarchy,
            "description": cand.description,
            "targetHazards": [{"key": h.key, "name": h.name}],
            "expectedEffect": effect,
            "residualAfter": after,
            "costBand": cand.cost,
            "ownerRole": rc["ownerRoles"].get(cand.hierarchy, "Section Head"),
            "dueDays": rc["dueDays"].get(res["level"], 90),
            "raisesCapa": moves_out,
            "kind": kind or ("CAPA" if moves_out else "IMPROVEMENT_NOTE"),
            "source": cand.source,
            "family": fam,
            "interimMeasure": INTERIM.get(fam, "Interim: supervisor check before each task until in place")
            if capex
            else None,
            "_rank": rank,
        }

    # Pick per hazard, most serious first. The one-capex limit falls back to the
    # next affordable item, so no hazard is left over for a later pass. A second,
    # independent control only for an S>=4 hazard the first leaves open (D).
    capex_seen = 0
    max_per = rc.get("maxRecsPerHazard", rc["maxPerHazard"])
    per_hazard.sort(key=lambda x: (-x[3]["score"], -x[3]["severity"], x[1].name))
    for hr, h, fam, res, tol, scored in per_hazard:
        chosen: list[dict[str, Any]] = []
        for best in scored:
            if len(chosen) >= max_per:
                break
            cand = best["cand"]
            key = _cand_key(cand)
            if (
                cand.cost in ("HIGH", "VERY_HIGH")
                and key not in picks
                and capex_seen >= rc["maxCapexPerEntry"]
            ):
                skipped.append({"candidate": cand.description, "reason": "only one capex proposal per entry"})
                continue
            chosen.append(best)
            capex_seen += int(cand.cost in ("HIGH", "VERY_HIGH") and key not in picks)
        if not chosen:
            continue
        first = chosen[0]
        sev = max(res["severity"], hr.get("credibleS") or 0)
        if (
            rc.get("secondRecWhenSevere", True)
            and len(chosen) < 2
            and sev >= 4
            and not eng.resolved(first["tolAfter"])
            and first["tolAfter"]["route"] != "OVERRIDE"
        ):
            fc = first["cand"]
            seconds = []
            for x in scored[1:]:
                c2 = x["cand"]
                if same_control(c2.hierarchy, c2.description, fc.hierarchy, fc.description, 0.3):
                    continue  # not independent
                if (
                    c2.cost in ("HIGH", "VERY_HIGH")
                    and _cand_key(c2) not in picks
                    and capex_seen >= rc["maxCapexPerEntry"]
                ):
                    continue
                both = trial(hr, h, [fc, c2])
                if both["score"] >= first["after"]["score"]:
                    continue  # adds nothing beyond the first
                gain = res["score"] - both["score"]
                seconds.append(
                    {
                        "cand": c2,
                        "gain": gain,
                        "per_effort": gain / weight.get(c2.cost, 2),
                        "after": both,
                        "tolAfter": tol_after(hr, both, [fc, c2]),
                    }
                )
            seconds.sort(
                key=lambda x: (
                    not eng.resolved(x["tolAfter"]) and x["tolAfter"]["route"] != "OVERRIDE",
                    x["cand"].hierarchy != "ENGINEERING",
                    -x["per_effort"],
                    hidx.get(x["cand"].hierarchy, 9),
                    x["cand"].id,
                )
            )
            if seconds:
                chosen.append({**seconds[0], "second": True})
                capex_seen += int(seconds[0]["cand"].cost in ("HIGH", "VERY_HIGH"))
        final_tol = chosen[-1]["tolAfter"]
        moves_out = not eng.resolved(tol) and (eng.resolved(final_tol) or final_tol["route"] == "OVERRIDE")
        prev_after = res
        for best in chosen:
            cand = best["cand"]
            add_pick(
                _cand_key(cand),
                cand,
                h,
                fam,
                res,
                best["after"],
                effect_text(prev_after, best["after"]),
                moves_out,
                (res["score"], best["per_effort"]),
            )
            prev_after = best["after"]

    kept = sorted(picks.values(), key=lambda r: (-r["_rank"][0], -r["_rank"][1], r["id"]))
    if entry_max_severity is not None and entry_max_severity <= cfg["tolerability"].get("lowSeverityMax", 3):
        cap = rc.get("maxPerEntryLowSeverity", rc["maxPerEntryLowModerate"])
    else:
        cap = (
            rc["maxPerEntryHighCritical"]
            if entry_level in ("HIGH", "CRITICAL")
            else rc["maxPerEntryLowModerate"]
        )
    # Controls already planned (with a date) count toward the cap, so acting on
    # the recommendations never produces a fresh batch on the next pass.
    planned = {
        c.description.strip().lower()
        for hr in hazard_results
        for c in hr.get("controls") or []
        if c.planned and c.planned_date
    }
    room = max(0, cap - len(planned))
    for r in kept[room:]:
        skipped.append({"candidate": r["description"], "reason": f"entry cap of {cap} reached"})
    kept = kept[:room]
    # Improvements with no rating credit (plant-index sheet): within the cap,
    # never a CAPA, never counted toward blockers or the override.
    for item in no_credit_items:
        if len(kept) >= room:
            break
        cand = item["cand"]
        key = _cand_key(cand)
        if any(k["id"] == key for k in kept):
            continue
        kept.append(
            {
                "id": key,
                "hierarchy": cand.hierarchy,
                "description": cand.description,
                "targetHazards": [{"key": item["h"].key, "name": item["h"].name}],
                "expectedEffect": "No change to the plant rating until the team re-scores it on the sheet",
                "residualAfter": item["res"],
                "costBand": cand.cost,
                "ownerRole": rc["ownerRoles"].get(cand.hierarchy, "Section Head"),
                "dueDays": rc["dueDays"].get(item["res"]["level"], 90),
                "raisesCapa": False,
                "kind": "IMPROVEMENT_NO_RATING_CREDIT",
                "source": cand.source,
                "family": item["fam"],
                "interimMeasure": None,
                "_rank": (item["res"]["score"], 0.0),
            }
        )
    targeted = {
        t["key"] for r in kept if r["kind"] != "IMPROVEMENT_NO_RATING_CREDIT" for t in r["targetHazards"]
    }
    uncovered = [
        {
            **n,
            "reason": "entry cap reached"
            if (len(planned) + len(kept)) >= cap
            else "no practicable control found",
        }
        for k, n in needing.items()
        if k not in targeted
    ]
    for r in kept:
        r.pop("_rank", None)
        shared = [
            s
            for s in sibling_recommended
            if linked(r["description"], s.get("text"), rc.get("linkSimilarity", 0.6))
        ]
        r["studyLevel"] = bool(shared)
        r["sharedWith"] = sorted({s.get("label") or "" for s in shared})[:5]
    return {
        "recommendations": kept,
        "linkedCapas": list(linked_capas.values()),
        "skipped": skipped,
        "cap": cap,
        "uncovered": uncovered,
    }


__all__ = [
    "CATALOG",
    "Candidate",
    "catalogue_for",
    "is_filler",
    "linked",
    "recommend",
    "same_control",
    "similarity",
    "tokens",
]
