"""Chemical compatibility (segregation) rules for the PSI / Chem advisor.

Rules live here as DATA (`RULES`), not in the model: an advisor that decided
storage compatibility with free-text generation could be confidently wrong
about two drums standing next to each other. The AI advisor quotes these
results; it does not override them.

How a pair is judged:
  1. Each chemical gets a set of HAZARD GROUPS derived from its GHS hazard
     classes / H-codes, GHS pictograms and (because the register's GHS fields
     are free text and often incomplete) its name, using `_NAME_HINTS`.
  2. Every rule whose two group-sets match the pair (in either order) fires.
  3. Each chemical's `incompatibleSubstances` list (the register / SDS section
     10 entries) is matched against the OTHER chemical's name, CAS number and
     hazard groups; a hit is INCOMPATIBLE.
  4. The verdict is the worst that fired: INCOMPATIBLE > CAUTION > COMPATIBLE.
     A chemical with no derivable hazard group is CAUTION ("cannot assess"),
     never COMPATIBLE by default.

Judging is symmetric by construction: check_pair(a, b) == check_pair(b, a)
(tested in tests/test_psi.py).

This is a screening aid for storage and handling segregation. It is not a
substitute for SDS section 7 / 10 and a competent person's review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

VERDICTS = ("COMPATIBLE", "CAUTION", "INCOMPATIBLE")
_RANK = {v: i for i, v in enumerate(VERDICTS)}

# ── hazard groups ────────────────────────────────────────────────────────────
GROUP_LABEL: dict[str, str] = {
    "EXPLOSIVE": "Explosive",
    "FLAMMABLE_GAS": "Flammable gas",
    "FLAMMABLE_LIQUID": "Flammable liquid",
    "FLAMMABLE_SOLID": "Flammable solid",
    "FLAMMABLE": "Flammable (form not recorded)",
    "OXIDISER": "Oxidiser",
    "OXIDISING_GAS": "Oxidising gas (e.g. oxygen)",
    "PEROXIDE": "Peroxide / self-reactive",
    "PYROPHORIC": "Pyrophoric",
    "WATER_REACTIVE": "Water-reactive",
    "COMPRESSED_GAS": "Gas under pressure",
    "ACID": "Acid",
    "BASE": "Base / alkali",
    "CORROSIVE": "Corrosive (acid/base not recorded)",
    "CYANIDE": "Cyanide",
    "SULPHIDE": "Sulphide",
    "HYPOCHLORITE": "Hypochlorite / chlorine release agent",
    "AMMONIA": "Ammonia / ammonium compound",
    "TOXIC": "Acutely toxic",
    "ORGANIC": "Organic material / solvent",
    "OILS_GREASE": "Oil / grease / lubricant",
    "AQUEOUS": "Aqueous solution",
}

FLAMMABLES = frozenset({"FLAMMABLE_GAS", "FLAMMABLE_LIQUID", "FLAMMABLE_SOLID", "FLAMMABLE"})
FUELS = FLAMMABLES | {"ORGANIC", "OILS_GREASE"}
OXIDISERS = frozenset({"OXIDISER", "OXIDISING_GAS"})


@dataclass(frozen=True)
class Rule:
    id: str
    left: frozenset[str]
    right: frozenset[str]
    verdict: str
    reason: str
    advice: str


def _r(rid: str, left: Iterable[str], right: Iterable[str], verdict: str, reason: str, advice: str) -> Rule:
    return Rule(rid, frozenset(left), frozenset(right), verdict, reason, advice)


RULES: tuple[Rule, ...] = (
    _r("OXIDISER_FUEL", {"OXIDISER"}, FUELS, "INCOMPATIBLE",
       "Oxidisers supply oxygen to a fire and can ignite or violently intensify the burning of flammables and organic material.",
       "Store in separate areas or cabinets; never in the same bund; keep organic absorbents away from oxidiser spills."),
    _r("OXYGEN_FUEL_GAS", {"OXIDISING_GAS"}, {"FLAMMABLE_GAS", "FLAMMABLE"}, "INCOMPATIBLE",
       "Oxygen and fuel gas cylinders together turn a small leak or fire into an intense one.",
       "Separate oxygen from fuel gas cylinders by at least 6 m or a fire-resistant barrier; only coupled on the cutting set in use."),
    _r("OXYGEN_OILS", {"OXIDISING_GAS"}, {"OILS_GREASE", "ORGANIC", "FLAMMABLE_LIQUID", "FLAMMABLE_SOLID"}, "INCOMPATIBLE",
       "Oil or grease in contact with compressed oxygen can ignite spontaneously.",
       "Keep oxygen cylinders, regulators and fittings oil- and grease-free; never store near lubricants or solvents."),
    _r("ACID_BASE", {"ACID"}, {"BASE"}, "INCOMPATIBLE",
       "Acids and bases react violently and exothermically; mixing can boil and spatter corrosive liquid.",
       "Separate bunds and drains; never share secondary containment."),
    _r("ACID_CYANIDE", {"ACID"}, {"CYANIDE"}, "INCOMPATIBLE",
       "Acids release highly toxic hydrogen cyanide gas from cyanides.",
       "Store cyanides in a locked, dry, acid-free area with separate drainage."),
    _r("ACID_SULPHIDE", {"ACID"}, {"SULPHIDE"}, "INCOMPATIBLE",
       "Acids release toxic, flammable hydrogen sulphide gas from sulphides.",
       "Separate storage and drainage; no shared spill kit neutraliser."),
    _r("ACID_HYPOCHLORITE", {"ACID"}, {"HYPOCHLORITE"}, "INCOMPATIBLE",
       "Acids release toxic chlorine gas from hypochlorite.",
       "Separate storage and drainage; label drains."),
    _r("HYPOCHLORITE_AMMONIA", {"HYPOCHLORITE"}, {"AMMONIA"}, "INCOMPATIBLE",
       "Hypochlorite with ammonia forms toxic chloramine gases.",
       "Separate storage; never use the same cleaning containers."),
    _r("WATER_REACTIVE_AQUEOUS", {"WATER_REACTIVE"}, {"AQUEOUS", "ACID"}, "INCOMPATIBLE",
       "Water-reactive substances react with water or aqueous solutions, releasing heat and flammable or toxic gas.",
       "Keep dry, raised off the floor, away from water lines and aqueous chemicals; no water-based extinguishing on them."),
    _r("WATER_REACTIVE_BASE", {"WATER_REACTIVE"}, {"BASE"}, "CAUTION",
       "Aqueous alkali solutions contain water, which reacts with water-reactive substances.",
       "Store apart; check SDS section 10 for the specific pair."),
    _r("PEROXIDE_FUEL", {"PEROXIDE"}, FUELS, "INCOMPATIBLE",
       "Peroxides are strong oxidisers and can decompose explosively in contact with organics and flammables.",
       "Separate, cool, ventilated storage; respect the peroxide's maximum storage temperature."),
    _r("PEROXIDE_ACID_BASE", {"PEROXIDE"}, {"ACID", "BASE"}, "CAUTION",
       "Acids, bases and metal contamination can catalyse peroxide decomposition.",
       "Store apart; never return unused peroxide to the original container."),
    _r("PYROPHORIC_ANY", {"PYROPHORIC"}, FUELS | OXIDISERS | {"AQUEOUS", "ACID", "BASE", "PEROXIDE"}, "INCOMPATIBLE",
       "Pyrophoric material ignites on contact with air and will ignite or react with nearby material.",
       "Store under inert atmosphere in a dedicated area."),
    _r("EXPLOSIVE_ANY", {"EXPLOSIVE"}, FUELS | OXIDISERS | {"ACID", "BASE", "PEROXIDE", "PYROPHORIC", "WATER_REACTIVE", "COMPRESSED_GAS"}, "INCOMPATIBLE",
       "Explosives must be stored in a licensed magazine, away from other hazardous goods.",
       "Licensed magazine only (PESO); no co-storage."),
    _r("OXIDISER_REDUCER", {"OXIDISER", "OXIDISING_GAS"}, {"CYANIDE", "SULPHIDE"}, "INCOMPATIBLE",
       "Cyanides and sulphides are reducing agents; with oxidisers they can react violently or release toxic gas.",
       "Separate storage areas."),
    _r("OXIDISER_ACID", {"OXIDISER"}, {"ACID"}, "CAUTION",
       "Oxidisers (including oxidising acids such as nitric acid) with other acids can release toxic gases "
       "(e.g. nitric + hydrochloric acid gives chlorine and nitrosyl chloride).",
       "Store apart unless mixed by design in a controlled process (e.g. HF / HNO3 pickling acid make-up)."),
    _r("OXIDISER_AMMONIA", {"OXIDISER", "HYPOCHLORITE"}, {"AMMONIA"}, "CAUTION",
       "Ammonia and ammonium compounds can form unstable or toxic products with oxidisers.",
       "Store apart; check SDS section 10."),
    _r("FLAMMABLE_GAS_LIQUID", {"FLAMMABLE_GAS"}, {"FLAMMABLE_LIQUID"}, "CAUTION",
       "Both feed the same fire; a pool fire can heat gas cylinders to failure.",
       "Keep cylinders in a separate, ventilated cylinder store away from flammable liquid storage."),
    _r("GAS_CYLINDER_FIRE", {"COMPRESSED_GAS"}, FLAMMABLES - {"FLAMMABLE_GAS"}, "CAUTION",
       "Cylinders exposed to a fire from nearby flammables can rupture.",
       "Store cylinders upright, chained, in a ventilated area away from flammable liquids and solids."),
    _r("TOXIC_ACID_GAS_RELEASE", {"ACID"}, {"AMMONIA"}, "INCOMPATIBLE",
       "Acids react violently with ammonia and ammonium hydroxide.",
       "Separate bunds and drains."),
)


# ── derivation ───────────────────────────────────────────────────────────────
# (pattern on GHS class / H-code text, groups)
_CLASS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"\bexplosive|\bh20[0-5]\b|\bghs01\b", ("EXPLOSIVE",)),
    (r"flammable gas|flammable aerosol|\bh22[01]\b|\bh222\b", ("FLAMMABLE_GAS",)),
    (r"flammable liquid|\bh22[4-6]\b", ("FLAMMABLE_LIQUID",)),
    (r"flammable solid|\bh228\b", ("FLAMMABLE_SOLID",)),
    (r"oxidi[sz]ing gas|\bh270\b", ("OXIDISING_GAS",)),
    (r"oxidi[sz]ing (liquid|solid)|oxidi[sz]er|\bh27[12]\b|\bghs03\b", ("OXIDISER",)),
    (r"organic peroxide|self[- ]reactive|\bh24[0-2]\b", ("PEROXIDE",)),
    (r"pyrophoric|\bh25[01]\b", ("PYROPHORIC",)),
    (r"water[- ]react|in contact with water|reacts violently with water|\bh26[01]\b|euh014|euh029", ("WATER_REACTIVE",)),
    (r"gas(es)? under pressure|compressed gas|liquefied gas|refrigerated liquefied|\bh28[01]\b|\bghs04\b", ("COMPRESSED_GAS",)),
    (r"acute tox|fatal if|toxic if|\bh3[0-3]0\b|\bh3[0-3]1\b|\bghs06\b", ("TOXIC",)),
    (r"skin corr|corrosive|\bh314\b|\bh290\b|\bghs05\b", ("CORROSIVE",)),
    (r"flame over circle", ("OXIDISER",)),
    (r"exploding bomb", ("EXPLOSIVE",)),
    (r"gas cylinder", ("COMPRESSED_GAS",)),
    (r"skull", ("TOXIC",)),
    (r"\bcorrosion\b", ("CORROSIVE",)),
    (r"\bghs02\b|\bflame\b(?! over)|flammable", ("FLAMMABLE",)),
)

# (pattern on chemical name, groups). Order does not matter; all hits union.
_NAME_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"\bacid\b|\bpickl", ("ACID", "AQUEOUS")),
    (r"\bnitric|\bperchloric|\bchromic", ("ACID", "OXIDISER")),
    (r"\bacetic|\bformic|\bcitric|\boxalic", ("ACID", "ORGANIC")),
    (r"\boleum\b|fuming sulph|fuming sulf", ("ACID", "WATER_REACTIVE")),
    (r"hydroxide|caustic|\balkali|soda lye|\blye\b", ("BASE", "AQUEOUS")),
    (r"\bammonia|ammonium hydroxide", ("BASE", "AMMONIA")),
    (r"\bammonium\b", ("AMMONIA",)),
    (r"\bamine\b|ethanolamine|\bmea\b", ("BASE", "ORGANIC")),
    (r"quick ?lime|calcium oxide|burnt lime", ("BASE", "WATER_REACTIVE")),
    (r"hydrated lime|slaked lime|calcium hydroxide", ("BASE",)),
    (r"cyanide", ("CYANIDE", "TOXIC")),
    (r"sulphide|sulfide|hydrosulphide|hydrosulfide", ("SULPHIDE",)),
    (r"hypochlorite|bleach", ("HYPOCHLORITE", "OXIDISER", "BASE", "AQUEOUS")),
    (r"peroxide|peracetic", ("PEROXIDE", "OXIDISER")),
    (r"permanganate|dichromate|chromate|chlorate|perchlorate|\bnitrate", ("OXIDISER",)),
    (r"^\s*(liquid |gaseous |compressed )?oxygen\b|\boxygen (gas|cylinder)", ("OXIDISING_GAS", "COMPRESSED_GAS")),
    (r"\blpg\b|propane|butane|acetylene|\bdissolved acetylene|natural gas|\bpng\b|\bcng\b|methane|coke oven gas|blast furnace gas|\bbf gas|\bco gas", ("FLAMMABLE_GAS", "COMPRESSED_GAS")),
    (r"^\s*hydrogen\s*(gas)?\s*$|\bhydrogen (gas|cylinder)", ("FLAMMABLE_GAS", "COMPRESSED_GAS")),
    (r"carbon monoxide", ("FLAMMABLE_GAS", "TOXIC", "COMPRESSED_GAS")),
    (r"\bchlorine\b(?! dioxide)", ("OXIDISING_GAS", "TOXIC", "COMPRESSED_GAS")),
    (r"\bnitrogen\b(?! ?(di)?oxide)|\bargon\b|carbon dioxide|\bco2\b|\bhelium\b", ("COMPRESSED_GAS",)),
    (r"diesel|\bhsd\b|\bldo\b|kerosene|furnace oil|fuel oil|petrol|gasoline|\bnaphtha", ("FLAMMABLE_LIQUID", "ORGANIC")),
    (r"acetone|toluene|xylene|methanol|ethanol|isopropanol|\bipa\b|thinner|solvent|\bmek\b|hexane|benzene|alcohol", ("FLAMMABLE_LIQUID", "ORGANIC")),
    (r"\bpaint\b|varnish|lacquer|adhesive", ("FLAMMABLE_LIQUID", "ORGANIC")),
    (r"\boil\b|grease|lubricant|hydraulic|quench(ing)? oil|rolling oil|\bwax\b", ("OILS_GREASE", "ORGANIC")),
    (r"calcium carbide|sodium metal|potassium metal|lithium metal|magnesium (powder|chips|turnings)|aluminium (powder|dust)|aluminum (powder|dust)|ferro[- ]?silicon|calcium silicide|phosphorus pentoxide|thionyl chloride|silicon tetrachloride|titanium tetrachloride", ("WATER_REACTIVE",)),
    (r"aluminium (powder|dust)|aluminum (powder|dust)|magnesium (powder|chips|turnings)", ("FLAMMABLE_SOLID",)),
    (r"\bsolution\b|aqueous|\bdilute\b|\d+\s*%|\bw/w\b|\bw/v\b", ("AQUEOUS",)),
    (r"hydrofluoric|hydrogen fluoride|\bhf\b", ("ACID", "TOXIC", "AQUEOUS")),
    (r"hydrochloric|muriatic|\bhcl\b|sulphuric|sulfuric|phosphoric", ("ACID", "AQUEOUS")),
)

# incompatibleSubstances text -> groups it names (beyond a literal name match).
_INCOMPAT_TEXT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"\bacid", ("ACID",)),
    (r"\bbase|\balkali|caustic|hydroxide", ("BASE",)),
    (r"oxidi[sz]", ("OXIDISER", "OXIDISING_GAS", "PEROXIDE", "HYPOCHLORITE")),
    (r"flammable|combustible", tuple(FLAMMABLES) + ("ORGANIC", "OILS_GREASE")),
    (r"organic", ("ORGANIC", "OILS_GREASE", "FLAMMABLE_LIQUID")),
    (r"\bwater|moisture|aqueous|humid", ("AQUEOUS",)),
    (r"cyanide", ("CYANIDE",)),
    (r"sulphide|sulfide", ("SULPHIDE",)),
    (r"reducing", ("CYANIDE", "SULPHIDE")),
    (r"\boil|grease|lubric", ("OILS_GREASE",)),
    (r"peroxide", ("PEROXIDE",)),
    (r"ammonia|ammonium", ("AMMONIA",)),
    (r"hypochlorite|bleach|chlorine", ("HYPOCHLORITE",)),
)


@dataclass
class Chem:
    """The subset of a HazardousMaterial row the rules read."""

    id: str
    name: str
    casNumber: str | None = None
    ghsHazardClasses: list[str] = field(default_factory=list)
    ghsPictograms: list[str] = field(default_factory=list)
    incompatibleSubstances: list[str] = field(default_factory=list)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def derive_groups(chem: Chem) -> set[str]:
    groups: set[str] = set()
    class_text = " | ".join(_norm(c) for c in [*(chem.ghsHazardClasses or []), *(chem.ghsPictograms or [])])
    for pat, gs in _CLASS_HINTS:
        if class_text and re.search(pat, class_text):
            groups.update(gs)
    name = _norm(chem.name)
    for pat, gs in _NAME_HINTS:
        if re.search(pat, name):
            groups.update(gs)
    # A specific flammable form makes the generic one redundant.
    if groups & (FLAMMABLES - {"FLAMMABLE"}):
        groups.discard("FLAMMABLE")
    # GHS03 on a gas is an oxidising GAS: the solid/liquid oxidiser rules
    # (e.g. oxidiser + acid) do not apply to an oxygen cylinder.
    if "OXIDISER" in groups and "COMPRESSED_GAS" in groups and "AQUEOUS" not in groups:
        groups.add("OXIDISING_GAS")
    if "OXIDISING_GAS" in groups and "COMPRESSED_GAS" in groups:
        groups.discard("OXIDISER")
    # Corrosive resolved to acid / base needs no generic flag.
    if groups & {"ACID", "BASE"}:
        groups.discard("CORROSIVE")
    return groups


def _listed_against(src: Chem, other: Chem, other_groups: set[str]) -> list[str]:
    """Entries in src.incompatibleSubstances that name `other` (by name, CAS, or
    a hazard group the other belongs to)."""
    hits: list[str] = []
    oname, ocas = _norm(other.name), _norm(other.casNumber)
    for raw in src.incompatibleSubstances or []:
        t = _norm(raw)
        if not t:
            continue
        if (ocas and ocas in t) or (oname and (oname in t or t in oname)):
            hits.append(raw)
            continue
        for pat, gs in _INCOMPAT_TEXT_HINTS:
            if re.search(pat, t) and other_groups & set(gs):
                hits.append(raw)
                break
    return hits


def check_pair(a: Chem, b: Chem, ga: set[str] | None = None, gb: set[str] | None = None) -> dict[str, Any]:
    """Verdict for one pair. Symmetric: check_pair(a, b) == check_pair(b, a)
    (fired rules and reasons are sorted, not order-dependent)."""
    ga = derive_groups(a) if ga is None else ga
    gb = derive_groups(b) if gb is None else gb
    fired: list[dict[str, str]] = []
    for rule in RULES:
        if (ga & rule.left and gb & rule.right) or (gb & rule.left and ga & rule.right):
            fired.append({"ruleId": rule.id, "verdict": rule.verdict, "reason": rule.reason, "advice": rule.advice})
    for src, dst, gdst in ((a, b, gb), (b, a, ga)):
        for entry in _listed_against(src, dst, gdst):
            fired.append({
                "ruleId": "REGISTER_LISTED",
                "verdict": "INCOMPATIBLE",
                "reason": f"{src.name}'s register / SDS entry lists '{entry}' as incompatible.",
                "advice": "Follow the SDS section 10 segregation for this pair.",
            })
    if (not ga or not gb) and not fired:
        missing = [c.name for c, g in ((a, ga), (b, gb)) if not g]
        fired.append({
            "ruleId": "UNKNOWN_HAZARD",
            "verdict": "CAUTION",
            "reason": f"No GHS hazard class recorded for {', '.join(sorted(missing))}; compatibility cannot be assessed.",
            "advice": "Record the GHS classes from the SDS, then re-check.",
        })
    if not fired and "CORROSIVE" in ga and "CORROSIVE" in gb:
        fired.append({
            "ruleId": "CORROSIVE_UNRESOLVED",
            "verdict": "CAUTION",
            "reason": "Both are corrosive but the register does not say which is acid and which is base.",
            "advice": "Confirm acid / base character from SDS section 9 before co-storage.",
        })
    # de-duplicate, deterministic order
    seen: set[tuple[str, str]] = set()
    uniq = []
    for f in sorted(fired, key=lambda x: (-_RANK[x["verdict"]], x["ruleId"], x["reason"])):
        k = (f["ruleId"], f["reason"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    verdict = max((f["verdict"] for f in uniq), key=lambda v: _RANK[v], default="COMPATIBLE")
    return {
        "verdict": verdict,
        "reasons": uniq or [{
            "ruleId": "NO_RULE", "verdict": "COMPATIBLE",
            "reason": "No incompatibility found between the recorded hazard groups.",
            "advice": "Store within quantity limits; confirm against SDS sections 7 and 10.",
        }],
        "pairIds": sorted([a.id, b.id]),
    }


def matrix(chems: list[Chem]) -> dict[str, Any]:
    """Pairwise matrix for 2+ chemicals. `cells[i][j]` is the verdict string
    (None on the diagonal); `pairs` carries the reasons per unordered pair."""
    groups = {c.id: derive_groups(c) for c in chems}
    n = len(chems)
    cells: list[list[str | None]] = [[None] * n for _ in range(n)]
    pairs: list[dict[str, Any]] = []
    worst = "COMPATIBLE"
    for i in range(n):
        for j in range(i + 1, n):
            res = check_pair(chems[i], chems[j], groups[chems[i].id], groups[chems[j].id])
            cells[i][j] = cells[j][i] = res["verdict"]
            pairs.append({"a": chems[i].id, "b": chems[j].id, "aName": chems[i].name, "bName": chems[j].name, **res})
            if _RANK[res["verdict"]] > _RANK[worst]:
                worst = res["verdict"]
    return {
        "chemicals": [
            {"id": c.id, "name": c.name, "casNumber": c.casNumber, "hazardGroups": sorted(groups[c.id]),
             "hazardGroupLabels": [GROUP_LABEL[g] for g in sorted(groups[c.id])]}
            for c in chems
        ],
        "cells": cells,
        "pairs": pairs,
        "worst": worst if n > 1 else None,
        "disclaimer": "Screening result from rule tables; confirm against SDS sections 7 and 10.",
    }


def rules_as_data() -> list[dict[str, Any]]:
    return [{"id": r.id, "left": sorted(r.left), "right": sorted(r.right), "verdict": r.verdict,
             "reason": r.reason, "advice": r.advice} for r in RULES]
