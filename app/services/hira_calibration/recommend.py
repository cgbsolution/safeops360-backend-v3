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
    ("LIFT-ENG-1", "LIFTING", "ENGINEERING", "MEDIUM",
     "Hard-barricaded exclusion zone under the crane travel path, with an interlocked or chained access point "
     "so no one can stand under a suspended load"),
    ("LIFT-ENG-2", "LIFTING", "ENGINEERING", "MEDIUM",
     "Overload limiter and over-hoist / over-travel limit switches on the crane, function-tested each shift"),
    ("LIFT-ENG-3", "LIFTING", "ENGINEERING", "MEDIUM",
     "Purpose-made lifting frame / spreader with positive-locking hooks instead of loose slings"),
    ("LIFT-ENG-4", "LIFTING", "ENGINEERING", "MEDIUM",
     "Radio remote control so the operator works from outside the drop zone"),
    ("LIFT-ADM-1", "LIFTING", "ADMINISTRATIVE", "LOW",
     "Written lift plan for this routine lift (SWL, slinging method, travel path, no one under the load) with a "
     "named competent rigger and signaller"),
    ("HT-ENG-1", "HEIGHT", "ENGINEERING", "MEDIUM",
     "Fixed guard rails with toe boards around the platform and a lockable grid cover over the opening"),
    ("HT-ENG-2", "HEIGHT", "ENGINEERING", "MEDIUM",
     "Permanent lifeline / anchor points with full body harness fall arrest"),
    ("HT-ADM-1", "HEIGHT", "ADMINISTRATIVE", "LOW",
     "Work-at-height permit for any work outside the guarded platform"),
    ("DU-ENG-1", "DUST", "ENGINEERING", "HIGH", "Local exhaust ventilation (LEV) hood at the discharge point"),
    ("DU-ENG-2", "DUST", "ENGINEERING", "MEDIUM", "Enclosed discharge chute or water-mist dust suppression"),
    ("DU-PPE-1", "DUST", "PPE", "LOW", "Fit-tested FFP2 / N95 respirator during the dusty task"),
    ("SH-SUB-1", "SHARP", "SUBSTITUTION", "LOW",
     "Replace the open knife with a safety cutter (shrouded or retractable blade)"),
    ("SH-PPE-1", "SHARP", "PPE", "LOW", "Cut-resistant gloves (EN 388 level C or above) for cutting tasks"),
    ("MH-ENG-1", "MANUAL_HANDLING", "ENGINEERING", "MEDIUM", "Mechanical aid (trolley, hoist) for loads over 25 kg"),
    ("MH-ADM-1", "MANUAL_HANDLING", "ADMINISTRATIVE", "LOW",
     "Weight limit and team-lift rule for manual loads, written into the work instruction"),
    ("FO-ENG-1", "FALLING_OBJECT", "ENGINEERING", "LOW",
     "Toe boards / mesh at the platform edge and a barricaded drop zone below"),
    ("FO-ADM-1", "FALLING_OBJECT", "ADMINISTRATIVE", "LOW",
     "No one below during the task: drop zone cordoned and signed"),
    ("MO-ENG-1", "MOLTEN", "ENGINEERING", "MEDIUM", "Splash shields at the tapping / pouring point"),
    ("MO-ENG-2", "MOLTEN", "ENGINEERING", "HIGH", "Mechanised sampling and temperature probe (no manual approach)"),
    ("MO-ADM-1", "MOLTEN", "ADMINISTRATIVE", "LOW",
     "Moisture-free check of scrap, additions and tools before contact with molten metal"),
    ("MO-PPE-1", "MOLTEN", "PPE", "MEDIUM",
     "Aluminised jacket and trousers, face shield with melting goggles and leg guards"),
    ("EL-ENG-1", "ELECTRICAL", "ENGINEERING", "LOW",
     "Lockable isolation points with test points at each panel for zero-energy verification"),
    ("EL-ENG-2", "ELECTRICAL", "ENGINEERING", "LOW", "30 mA RCD / ELCB on portable equipment circuits"),
    ("EL-ENG-3", "ELECTRICAL", "ENGINEERING", "MEDIUM", "Insulating shrouds / barriers over live parts in the panel"),
    ("EL-ADM-1", "ELECTRICAL", "ADMINISTRATIVE", "LOW",
     "Electrical permit with LOTO; work only by licensed electricians"),
    ("VE-ENG-1", "VEHICLE", "ENGINEERING", "MEDIUM", "Physically segregated pedestrian walkway with barriers"),
    ("VE-ENG-2", "VEHICLE", "ENGINEERING", "MEDIUM", "Reversing camera / proximity sensor and blue-spot lights"),
    ("VE-ADM-1", "VEHICLE", "ADMINISTRATIVE", "LOW", "Speed limit and banksman for reversing in congested bays"),
    ("MA-ENG-1", "MACHINERY", "ENGINEERING", "MEDIUM", "Fixed or interlocked guards on rotating parts and nip points"),
    ("MA-ENG-2", "MACHINERY", "ENGINEERING", "HIGH", "Light curtain or two-hand control at the press"),
    ("MA-ADM-1", "MACHINERY", "ADMINISTRATIVE", "LOW", "LOTO before any guard is removed for clearing or maintenance"),
    ("CS-ENG-1", "CONFINED_SPACE", "ENGINEERING", "MEDIUM",
     "Forced ventilation with continuous multi-gas monitoring during entry"),
    ("CS-ENG-2", "CONFINED_SPACE", "ENGINEERING", "MEDIUM", "Tripod and winch rescue set at the entry point"),
    ("CS-ADM-1", "CONFINED_SPACE", "ADMINISTRATIVE", "LOW",
     "Confined-space entry permit with gas test, trained attendant and rescue plan"),
    ("CH-SUB-1", "CHEMICAL", "SUBSTITUTION", "MEDIUM", "Substitute with a less hazardous chemical where the process allows"),
    ("CH-ENG-1", "CHEMICAL", "ENGINEERING", "MEDIUM", "Closed transfer (pump / dispensing system) instead of manual pouring"),
    ("CH-ENG-2", "CHEMICAL", "ENGINEERING", "LOW", "Eyewash and safety shower within 10 seconds' reach"),
    ("CH-PPE-1", "CHEMICAL", "PPE", "LOW", "Chemical-resistant gloves, goggles / face shield and apron per the SDS"),
    ("ST-ENG-1", "SLIP_TRIP", "ENGINEERING", "MEDIUM", "Anti-slip flooring / grating and adequate lighting on the route"),
    ("ST-ADM-1", "SLIP_TRIP", "ADMINISTRATIVE", "LOW", "Housekeeping schedule with spillage clean-up and floor marking"),
    ("FE-ENG-1", "FIRE_EXPLOSION", "ENGINEERING", "HIGH", "Gas leak detection with automatic shut-off on the fuel line"),
    ("FE-ENG-2", "FIRE_EXPLOSION", "ENGINEERING", "LOW", "Flashback arrestors on gas cutting sets"),
    ("FE-ADM-1", "FIRE_EXPLOSION", "ADMINISTRATIVE", "LOW", "Hot-work permit with fire watch and extinguisher at the job"),
    ("PR-ENG-1", "PRESSURE", "ENGINEERING", "LOW", "Pressure relief valve and whip-check restraints on hoses"),
    ("PR-ADM-1", "PRESSURE", "ADMINISTRATIVE", "LOW", "Depressurise and bleed before breaking any line (written procedure)"),
    ("NO-ENG-1", "NOISE", "ENGINEERING", "HIGH", "Acoustic enclosure / silencer at the noise source"),
    ("NO-PPE-1", "NOISE", "PPE", "LOW", "Ear muffs or plugs rated for the measured noise level in marked zones"),
    ("HE-ENG-1", "HEAT", "ENGINEERING", "MEDIUM", "Spot cooling / man-coolers at hot workstations"),
    ("HE-ADM-1", "HEAT", "ADMINISTRATIVE", "LOW", "Work-rest cycle, drinking water and shaded rest area"),
    ("TH-ENG-1", "THERMAL", "ENGINEERING", "MEDIUM", "Insulation or guarding on hot surfaces"),
]

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

_STOP = {"the", "and", "for", "with", "each", "every", "from", "this", "that", "all", "any", "only", "per", "use",
         "used", "into", "onto", "over", "under", "before", "after", "during", "where", "when", "not", "are", "its",
         "out", "off", "one", "two", "via", "done", "kept", "place", "provided", "available", "ensure", "proper"}
_SYN = {"inspection": "exam", "inspected": "exam", "examination": "exam", "examined": "exam", "check": "exam",
        "checks": "exam", "checked": "exam", "test": "exam", "tested": "exam", "testing": "exam",
        "cranes": "crane", "eot": "crane", "hoist": "crane", "barricade": "barrier", "barricaded": "barrier",
        "barricading": "barrier", "cordon": "barrier", "cordoned": "barrier", "exclusion": "barrier",
        "fence": "barrier", "railing": "rail", "railings": "rail", "guardrail": "rail", "handrail": "rail",
        "rails": "rail", "slings": "sling", "gloves": "glove", "respirator": "mask", "respirators": "mask",
        "sop": "procedure", "instruction": "procedure", "instructions": "procedure", "procedures": "procedure",
        "permits": "permit", "limiter": "limit", "switches": "switch", "harnesses": "harness",
        "thirdparty": "third", "lev": "exhaust", "ventilation": "exhaust", "extraction": "exhaust"}


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
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


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


def recommend(hazard_results: list[dict[str, Any]], *, cfg: dict[str, Any], strict: bool,
              entry_level: str | None, open_capas: list[dict[str, Any]] | None = None,
              sibling_recommended: list[dict[str, Any]] | None = None,
              extra_candidates: list[Candidate] | None = None) -> dict[str, Any]:
    """Pick the recommendations for one entry.

    hazard_results: from review_v2 (each with hazard, controls, residual, tolerability...).
    extra_candidates: LLM-proposed controls (Assistant / Copilot draft), filtered the same way.
    """
    rc = cfg["recommendations"]
    weight = rc["costWeight"]
    open_capas = open_capas or []
    sibling_recommended = sibling_recommended or []
    picks: dict[str, dict[str, Any]] = {}
    linked_capas: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []

    for hr in hazard_results:
        tol = hr.get("tolerability")
        if hr.get("collapsed") or not tol or eng.resolved(tol) or tol["route"] == "OVERRIDE":
            continue
        need_eng_only = tol["route"] == "ALARP_CHECKLIST"
        if need_eng_only and "engineeringInPlaceOrPlanned" not in tol["missing"]:
            continue  # the checklist gap is a record, not a control
        if hr.get("plannedCovers"):
            continue  # a planned control with a date already covers this hazard
        h: eng.Hazard = hr["hazard"]
        fam = h.family
        controls: list[eng.Control] = hr["controls"]
        res = hr["residual"] or hr["inherent"]
        pool = [c for c in catalogue_for(fam)]
        for x in extra_candidates or []:
            if x.family in (fam, "GENERAL") or fam in eng.control_families(x.description) or \
                    eng.control_families(x.description) & ({fam} | eng.RELATED.get(fam, set())):
                pool.append(x)
        scored = []
        for cand in pool:
            if need_eng_only and cand.hierarchy != "ENGINEERING":
                continue
            # already in place (same family wording)?
            dup = next((c for c in controls if similarity(cand.description, c.description) >= rc["dedupeSimilarity"]),
                       None)
            if dup is not None:
                skipped.append({"candidate": cand.description, "reason": f"already in place: {dup.description}"})
                continue
            capa = next((k for k in open_capas if similarity(cand.description, k.get("text")) >= rc["dedupeSimilarity"]),
                        None)
            if capa is not None:
                linked_capas[capa["id"]] = {**capa, "hazards": sorted({*linked_capas.get(capa["id"], {}).get(
                    "hazards", []), h.name})}
                continue
            if is_filler(cand.description, cfg):
                has_admin = any((c.hierarchy or "").upper() == "ADMINISTRATIVE" and eng.applies_to(c, h)[0]
                                for c in controls if not c.planned)
                if has_admin:
                    skipped.append({"candidate": cand.description, "reason": "generic filler; no gap shown"})
                    continue
            if cand.cost in ("HIGH", "VERY_HIGH") and eng.level_index(res["level"]) < eng.level_index(
                    rc["highCostMinResidual"]):
                skipped.append({"candidate": cand.description, "reason": "not reasonably practicable at this risk"})
                continue
            # A rating that already includes the existing controls (single-rating
            # sheet) gets credit for the new control only.
            kept_existing = [] if hr.get("basisIncludesControls") else [c for c in controls if not c.planned]
            trial = [*kept_existing,
                     eng.Control(key=None, hierarchy=cand.hierarchy, description=cand.description,
                                 effectiveness="EFFECTIVE", evidence_attached=True, hazard_keys=(h.key,),
                                 verification_method="planned verification")]
            base_l, base_s = hr["basis"]
            new = eng.residual_from_controls(base_l, base_s, trial, cfg, hazard=h)
            new_rate = eng.rate(new["likelihood"], new["severity"], cfg)
            gain = res["score"] - new_rate["score"]
            if gain <= 0 and not need_eng_only:
                continue
            # The ALARP records (elimination considered, review date) are filled by
            # the suggested defaults; only the control itself is what this adds.
            alarp_after = {**hr["alarp"], "eliminationConsidered": True, "reviewDateSet": True,
                           "engineeringInPlaceOrPlanned":
                           hr["alarp"].get("engineeringInPlaceOrPlanned") or cand.hierarchy == "ENGINEERING"}
            tol_after = eng.tolerability(new_rate, credible_s=hr.get("credibleS"), alarp=alarp_after, strict=strict,
                                         cfg=cfg)
            scored.append({
                "cand": cand, "gain": gain, "per_effort": gain / weight.get(cand.cost, 2),
                "after": new_rate, "tolAfter": tol_after,
                "effect": _effect_text(res["likelihood"] - new_rate["likelihood"],
                                       res["severity"] - new_rate["severity"]),
            })
        if not scored:
            continue
        hidx = {x: i for i, x in enumerate(eng.HIERARCHY)}
        scored.sort(key=lambda x: (not eng.resolved(x["tolAfter"]) and x["tolAfter"]["route"] != "OVERRIDE",
                                   -x["per_effort"], hidx.get(x["cand"].hierarchy, 9), x["cand"].id))
        for best in scored[: rc["maxPerHazard"]]:
            cand = best["cand"]
            key = cand.id if cand.source == "catalogue" else f"llm:{cand.description.lower()[:60]}"
            moves_out = not eng.resolved(tol) and (eng.resolved(best["tolAfter"])
                                                   or best["tolAfter"]["route"] == "OVERRIDE")
            if key in picks:
                p = picks[key]
                p["targetHazards"].append({"key": h.key, "name": h.name})
                p["raisesCapa"] = p["raisesCapa"] or moves_out
                p["_rank"] = max(p["_rank"], (res["score"], best["per_effort"]))
                continue
            level = res["level"]
            picks[key] = {
                "id": key, "hierarchy": cand.hierarchy, "description": cand.description,
                "targetHazards": [{"key": h.key, "name": h.name}],
                "expectedEffect": best["effect"], "residualAfter": best["after"],
                "costBand": cand.cost, "ownerRole": rc["ownerRoles"].get(cand.hierarchy, "Section Head"),
                "dueDays": rc["dueDays"].get(level, 90), "raisesCapa": moves_out,
                "kind": "CAPA" if moves_out else "IMPROVEMENT_NOTE",
                "source": cand.source, "family": fam,
                "interimMeasure": INTERIM.get(fam, "Interim: supervisor check before each task until in place")
                if cand.cost in ("HIGH", "VERY_HIGH") else None,
                "_rank": (res["score"], best["per_effort"]),
            }

    recs = sorted(picks.values(), key=lambda r: (-r["_rank"][0], -r["_rank"][1], r["id"]))
    # at most one capex item per entry
    capex_seen = 0
    kept = []
    for r in recs:
        if r["costBand"] in ("HIGH", "VERY_HIGH"):
            capex_seen += 1
            if capex_seen > rc["maxCapexPerEntry"]:
                skipped.append({"candidate": r["description"], "reason": "only one capex proposal per entry"})
                continue
        kept.append(r)
    cap = rc["maxPerEntryHighCritical"] if entry_level in ("HIGH", "CRITICAL") else rc["maxPerEntryLowModerate"]
    for r in kept[cap:]:
        skipped.append({"candidate": r["description"], "reason": f"entry cap of {cap} reached"})
    kept = kept[:cap]
    for r in kept:
        r.pop("_rank", None)
        shared = [s for s in sibling_recommended if similarity(r["description"], s.get("text")) >= rc["dedupeSimilarity"]]
        r["studyLevel"] = bool(shared)
        r["sharedWith"] = sorted({s.get("label") or "" for s in shared})[:5]
    return {"recommendations": kept, "linkedCapas": list(linked_capas.values()), "skipped": skipped, "cap": cap}


__all__ = ["CATALOG", "Candidate", "catalogue_for", "is_filler", "recommend", "similarity", "tokens"]
