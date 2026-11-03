"""HIRA risk engine (calibration v2). Pure functions, no DB.

One matrix for every study: score = L x S on a 5 x 5 grid and ONE set of band
thresholds (default <=3 LOW, 4-6 MODERATE, 8-12 HIGH, >=15 CRITICAL). A plant
that is stricter (its matrix marks MODERATE unacceptable) keeps the same bands;
only the tolerability decision changes.

Rules implemented here (numbering from the calibration brief):
  R1  score / band / rate                      - code decides every number
  R2  per-hazard rating, entry = max            - entry_rating()
  R3  likelihood anchored to evidence           - calibrate_likelihood()
  R4  severity = credible worst case            - calibrate_severity()
  R5  control credit model                      - residual_from_controls()
  R7  tolerability that terminates (ALARP)      - tolerability()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

LEVELS = ("LOW", "MODERATE", "HIGH", "CRITICAL")
HIERARCHY = ("ELIMINATION", "SUBSTITUTION", "ENGINEERING", "ADMINISTRATIVE", "PPE")

ACCEPTABLE = "ACCEPTABLE"
TOLERABLE_ALARP = "TOLERABLE_ALARP"
NOT_ACCEPTABLE = "NOT_ACCEPTABLE"


# ─── R1: score and band ─────────────────────────────────────────────────


def score(l: int, s: int) -> int:
    if not (1 <= int(l) <= 5 and 1 <= int(s) <= 5):
        raise ValueError(f"likelihood and severity must be 1-5 (got L{l} S{s})")
    return int(l) * int(s)


def band(score_: int, cfg: dict[str, Any]) -> str:
    th = cfg["thresholds"]
    if score_ <= th["lowMax"]:
        return "LOW"
    if score_ <= th["moderateMax"]:
        return "MODERATE"
    if score_ <= th["highMax"]:
        return "HIGH"
    return "CRITICAL"


def rate(l: int, s: int, cfg: dict[str, Any]) -> dict[str, Any]:
    sc = score(l, s)
    return {"likelihood": int(l), "severity": int(s), "score": sc, "level": band(sc, cfg)}


def level_index(level: str | None) -> int:
    return LEVELS.index(level) if level in LEVELS else -1


def rating_text(r: dict[str, Any] | None) -> str:
    if not r:
        return "not rated"
    return f"L{r['likelihood']} x S{r['severity']} = {r['level']} ({r['score']})"


# ─── Hazard families ────────────────────────────────────────────────────

FAMILY_BY_CODE = {
    "MECH-008": "LIFTING",
    "MECH-009": "LIFTING",
    "VEH-010": "LIFTING",
    "MECH_HEAVY_LIFTING": "MANUAL_HANDLING",
    "MECH-013": "MANUAL_HANDLING",
    "MECH_SHARP_EDGE": "SHARP",
    "ENV_DUST_AIRBORNE": "DUST",
    "MECH_FALLING_OBJECT": "FALLING_OBJECT",
    "PHYS_STRUCK_BY_FALLING": "FALLING_OBJECT",
    "PHYS_SLIP_TRIP": "SLIP_TRIP",
    "MECH_MOBILE_EQUIPMENT": "VEHICLE",
    "MECH_UNGUARDED_ROTATING": "MACHINERY",
    "MECH_PINCH_POINT": "MACHINERY",
    "MECH_PROJECTILE": "MACHINERY",
    "FIRE_MOLTEN_METAL": "MOLTEN",
    "ENV_HEAT_STRESS": "HEAT",
    "ENV_COLD_STRESS": "HEAT",
    "MECH_VIBRATION": "VIBRATION",
}
FAMILY_BY_CATEGORY = {
    "lifting": "LIFTING",
    "height": "HEIGHT",
    "electrical": "ELECTRICAL",
    "confined_space": "CONFINED_SPACE",
    "transportation": "VEHICLE",
    "pressure": "PRESSURE",
    "fire_explosion": "FIRE_EXPLOSION",
    "emergency_response": "FIRE_EXPLOSION",
    "chemical": "CHEMICAL",
    "health": "CHEMICAL",
    "ergonomic": "MANUAL_HANDLING",
    "noise": "NOISE",
    "psychosocial": "PSYCHOSOCIAL",
    "behavioral": "PSYCHOSOCIAL",
    "thermal": "THERMAL",
    "radiation": "RADIATION",
    "environmental": "ENVIRONMENT",
    "mechanical": "MACHINERY",
    "physical": "PHYSICAL",
    "biological": "BIOLOGICAL",
    "physiological": "HEAT",
    "utility_failure": "GENERAL",
}
# Words in the hazard text, for hazards without a mapped code or category.
FAMILY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("MOLTEN", ("molten", "hot metal", "liquid metal", "slag splash", "ladle", "tapping")),
    ("CONFINED_SPACE", ("confined space", "vessel entry", "tank entry", "manhole")),
    ("ELECTRICAL", ("electric", "live ", "shock", "arc flash", "panel")),
    (
        "LIFTING",
        ("crane", "hoist", "sling", "rigging", "suspended load", "eot", "chain block", "dropped load"),
    ),
    ("HEIGHT", ("fall from", "work at height", "ladder", "scaffold", "roof", "platform edge", "bin opening")),
    ("VEHICLE", ("forklift", "vehicle", "truck", "dumper", "reversing", "loader")),
    ("MACHINERY", ("rotating", "press", "nip point", "entangle", "caught in", "amputat", "conveyor")),
    ("PRESSURE", ("pressure", "hydraulic", "pneumatic", "compressed")),
    ("FIRE_EXPLOSION", ("fire", "explosion", "flammable", "lpg")),
    ("CHEMICAL", ("chemical", "acid", "alkali", "corrosive", "toxic", "solvent")),
    ("DUST", ("dust",)),
    ("SHARP", ("knife", "cut ", "laceration", "sharp")),
    ("MANUAL_HANDLING", ("manual handling", "strain", "sprain", "posture", "back injur")),
    ("SLIP_TRIP", ("slip", "trip")),
    ("NOISE", ("noise",)),
    ("FALLING_OBJECT", ("falling object", "struck by falling", "dropped object")),
]
# Families whose controls also help each other (a control for one applies to the other).
RELATED = {
    "FALLING_OBJECT": {"LIFTING", "HEIGHT"},
    "LIFTING": {"FALLING_OBJECT"},
    "THERMAL": {"MOLTEN"},
    "MOLTEN": {"THERMAL", "FIRE_EXPLOSION"},
    "MACHINERY": {"ELECTRICAL"},
    "ELECTRICAL": {"MACHINERY"},
    "PHYSICAL": {"SLIP_TRIP", "FALLING_OBJECT"},
}
CONTROL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "LIFTING": (
        "crane",
        "hoist",
        "sling",
        "rigging",
        "rigger",
        "lifting plan",
        "lift plan",
        "lifting tackle",
        "lifting gear",
        "lifting machine",
        "suspended load",
        "hook",
        "tackle",
        "signal",
        "banksman",
        "tag line",
        "swl",
        "limit switch",
        "wire rope",
        "eot",
        "chain block",
        "exclusion zone",
        "load test",
        "anti-collision",
    ),
    "HEIGHT": (
        "harness",
        "railing",
        "guard rail",
        "guardrail",
        "handrail",
        "grating",
        "grid",
        "edge protection",
        "ladder",
        "scaffold",
        "fall arrest",
        "anchor",
        "lifeline",
        "toe board",
        "height",
        "bin top cover",
        "opening cover",
        "platform",
    ),
    "DUST": (
        "dust",
        "respirat",
        "extraction",
        "lev ",
        "local exhaust",
        "ventilation",
        "suppression",
        "enclos",
        "ffp",
        "n95",
        "mask",
    ),
    "SHARP": (
        "knife",
        "cutter",
        "cut-resistant",
        "cut resistant",
        "blade",
        "sharp",
        "bag opener",
        "bag cutter",
    ),
    "MANUAL_HANDLING": (
        "trolley",
        "mechanical aid",
        "manual handling",
        "posture",
        "team lift",
        "weight limit",
        "lifting technique",
        "ergonom",
    ),
    "MOLTEN": (
        "alumin",
        "splash",
        "ladle",
        "molten",
        "slag",
        "face shield",
        "moisture",
        "preheat",
        "tapping",
        "leg guard",
    ),
    "ELECTRICAL": (
        "loto",
        "lock out",
        "lockout",
        "lock-out",
        "isolation",
        "isolat",
        "earthing",
        "electrical",
        "insulat",
        "rcd",
        "elcb",
        "rccb",
        "panel",
        "voltage",
        "arc flash",
    ),
    "VEHICLE": (
        "horn",
        "reverse",
        "reversing",
        "segregat",
        "speed limit",
        "vehicle",
        "forklift",
        "truck",
        "pedestrian",
        "walkway",
        "mirror",
        "beacon",
        "seat belt",
    ),
    "FALLING_OBJECT": ("toe board", "netting", "barricad", "falling", "overhead", "cordon", "drop zone"),
    "MACHINERY": (
        "guard",
        "interlock",
        "emergency stop",
        "e-stop",
        "light curtain",
        "two-hand",
        "loto",
        "lockout",
    ),
    "CONFINED_SPACE": (
        "confined",
        "gas test",
        "gas monitor",
        "oxygen",
        "attendant",
        "rescue",
        "entry permit",
        "tripod",
    ),
    "CHEMICAL": (
        "msds",
        "sds",
        "chemical",
        "spill",
        "bund",
        "eyewash",
        "safety shower",
        "neutralis",
        "fume hood",
    ),
    "SLIP_TRIP": (
        "housekeeping",
        "anti-slip",
        "non-slip",
        "spillage",
        "drain",
        "floor marking",
        "illumination",
        "lighting",
    ),
    "FIRE_EXPLOSION": (
        "fire",
        "extinguisher",
        "hydrant",
        "hot work",
        "gas detector",
        "flame",
        "spark",
        "flashback",
    ),
    "PRESSURE": ("relief valve", "pressure", "gauge", "hose", "whip", "depressur", "bleed"),
    "NOISE": ("noise", "acoustic", "silencer", "earmuff", "ear muff", "ear plug"),
    "HEAT": ("heat", "cool", "drinking water", "rest", "shade", "hydration", "acclimat"),
}
GENERIC_KEYWORDS = (
    "sop",
    "work instruction",
    "procedure",
    "jsa",
    "job safety analysis",
    "method statement",
    "supervis",
    "training",
    "trained",
    "competen",
    "authoris",
    "authoriz",
    "checklist",
    "pre-use",
    "pre use",
    "briefing",
    "toolbox",
    "doc no",
    "w/i",
    "permit",
)
MITIGATING_KEYWORDS = (
    "arrest",
    "net",
    "catch",
    "airbag",
    "suppression",
    "sprinkler",
    "deluge",
    "shower",
    "eyewash",
    "blast",
    "relief valve",
    "rupture",
    "cushion",
    "absorber",
    "rcd",
    "elcb",
    "rccb",
    "bund",
    "spill tray",
)


def _norm(t: str | None) -> str:
    return " " + re.sub(r"\s+", " ", (t or "").lower()) + " "


def hazard_family(code: str | None, category: str | None, text: str | None) -> str:
    """Family of a hazard: master code first, molten-metal wording next, then
    the master category, then words in the hazard / consequence text."""
    if code and code in FAMILY_BY_CODE:
        return FAMILY_BY_CODE[code]
    t = _norm(text)
    if any(k in t for k in FAMILY_KEYWORDS[0][1]) and (category or "") in (
        "",
        "fire_explosion",
        "thermal",
        "mechanical",
        "physical",
    ):
        return "MOLTEN"
    if category and category in FAMILY_BY_CATEGORY:
        return FAMILY_BY_CATEGORY[category]
    for fam, kws in FAMILY_KEYWORDS:
        if any(k in t for k in kws):
            return fam
    return "GENERAL"


def control_families(description: str | None) -> set[str]:
    t = _norm(description)
    return {fam for fam, kws in CONTROL_KEYWORDS.items() if any(k in t for k in kws)}


def is_generic_control(description: str | None) -> bool:
    t = _norm(description)
    return any(k in t for k in GENERIC_KEYWORDS)


def engineering_kind(description: str | None) -> str:
    t = _norm(description)
    return "MITIGATING" if any(k in t for k in MITIGATING_KEYWORDS) else "PREVENTIVE"


def is_baseline(hierarchy: str | None, description: str | None, cfg: dict[str, Any]) -> bool:
    """Baseline site rules (helmet, safety shoes, hi-vis, basic gloves, induction,
    general permit system): recorded, never credited, never recommended."""
    h = (hierarchy or "").upper()
    if h not in ("PPE", "ADMINISTRATIVE"):
        return False
    cc = cfg["controlCredit"]
    t = _norm(description)
    t = re.sub(r"^\s*(ppe|use of ppe|wearing of ppe)\s*[:\-]\s*", " ", t)
    items = [i.strip() for i in re.split(r",|;|/|\+| and |\n", t) if i.strip()]
    if not items:
        return False
    for item in items:
        it = f" {item} "
        if any(q in it for q in cc["specificQualifiers"]):
            return False
        if not any(k in it for k in cc["baselineKeywords"]):
            return False
    return True


# ─── R3: likelihood anchored to evidence ────────────────────────────────


@dataclass
class Evidence:
    """What the plant's records show for this activity / area."""

    events_window: int = 0  # incidents + near misses in the evidence window
    events_12m: int = 0
    control_failures: list[str] = field(default_factory=list)  # overdue statutory exam, failed inspection
    window_months: int = 24
    items: list[str] = field(default_factory=list)  # human-readable lines, for the rationale
    # Per hazard family: {"window": n, "12m": n, "failures": [...], "items": [...]}. An
    # event only supports the likelihood of the hazards it is about (a forklift
    # near miss is not evidence that a crane load will drop).
    by_family: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def has_any(self) -> bool:
        return bool(self.events_window or self.control_failures)

    def for_family(self, family: str) -> Evidence:
        if not self.by_family:
            return self
        f = self.by_family.get(family) or {}
        return Evidence(
            events_window=f.get("window", 0),
            events_12m=f.get("12m", 0),
            control_failures=list(f.get("failures") or []),
            window_months=self.window_months,
            items=list(f.get("items") or []),
        )


def default_likelihood(routine: str | None, frequency: str | None, cfg: dict[str, Any]) -> int:
    lk = cfg["likelihood"]
    if (routine or "ROUTINE").upper() == "ROUTINE" and (frequency or "").upper() in lk[
        "highExposureFrequencies"
    ]:
        return lk["defaultHighExposure"]
    return lk["defaultRoutine"]


def calibrate_likelihood(
    proposed: int | None,
    *,
    routine: str | None,
    frequency: str | None,
    evidence: Evidence,
    scale: str,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Cap a proposed likelihood by the evidence. L4 needs a recorded event in
    the window or a documented control failure; L5 needs 2+ events in 12 months.
    Exposure frequency can justify L3 over L2 but never L4 by itself.
    Plant-index scales (not frequency) are passed through unchanged."""
    lk = cfg["likelihood"]
    if scale != "FREQUENCY":
        v = proposed if proposed else default_likelihood(routine, frequency, cfg)
        return {
            "value": v,
            "proposed": proposed,
            "capped": False,
            "reason": "Plant index scale (not a frequency scale): the evidence cap does not apply.",
        }
    p = proposed if proposed else default_likelihood(routine, frequency, cfg)
    ev_txt = (
        "; ".join(evidence.items[:3])
        if evidence.items
        else f"no recorded events in the last {evidence.window_months} months"
    )
    if p >= 5:
        if evidence.events_12m >= lk["l5EventsIn12Months"]:
            return {"value": 5, "proposed": proposed, "capped": False, "reason": f"L5 supported: {ev_txt}."}
        if evidence.has_any:
            return {
                "value": 4,
                "proposed": proposed,
                "capped": True,
                "reason": f"L5 needs {lk['l5EventsIn12Months']}+ recorded events in 12 months; "
                f"L4 is supported by: {ev_txt}.",
            }
        p = 4  # fall through to the L4 test
    if p == 4:
        if evidence.has_any:
            return {"value": 4, "proposed": proposed, "capped": False, "reason": f"L4 supported: {ev_txt}."}
        cap = lk["capWithoutEvidence"]
        return {
            "value": cap,
            "proposed": proposed,
            "capped": True,
            "reason": f"Capped at L{cap}: {ev_txt}. Daily exposure is not the same as a likely event; "
            f"L4 needs a recorded incident/near miss on this activity or area, or a documented "
            f"control failure (overdue statutory exam, failed inspection).",
        }
    return {
        "value": p,
        "proposed": proposed,
        "capped": False,
        "reason": f"L{p}: {lk['anchors'].get(str(p), '')} ({ev_txt}).",
    }


# ─── R4: severity = credible worst case ─────────────────────────────────


def credible_severity(family: str, text: str | None, cfg: dict[str, Any]) -> tuple[int | None, int | None]:
    rule = (cfg["severity"]["rubric"] or {}).get(family) or {}
    lo, hi = rule.get("min"), rule.get("max")
    t = _norm(text)
    if lo and any(k in t for k in rule.get("s5Keywords") or []):
        lo = 5
    return lo, hi


def calibrate_severity(
    proposed: int | None, *, family: str, text: str | None, cfg: dict[str, Any], adjust: bool
) -> dict[str, Any]:
    """Check a severity against the credible-worst-case rubric. With adjust=True
    (Assistant drafts) the value is moved into the credible range; otherwise
    (a team's own rating) it is left alone and an ADVISORY is returned."""
    lo, hi = credible_severity(family, text, cfg)
    out: dict[str, Any] = {
        "value": proposed,
        "proposed": proposed,
        "credibleMin": lo,
        "credibleMax": hi,
        "finding": None,
    }
    if proposed is None:
        return out
    if lo and proposed < lo:
        out["finding"] = {
            "kind": "UNDERSTATED",
            "reason": f"{family.replace('_', ' ').title()} can credibly cause "
            f"{'a fatality' if lo >= 5 else 'serious or permanent injury'}; S{proposed} is below "
            f"the credible worst case (S{lo}+).",
        }
        if adjust:
            out["value"] = lo
    elif hi and proposed > hi:
        out["finding"] = {
            "kind": "OVERSTATED",
            "reason": f"{family.replace('_', ' ').title()} outcomes are recoverable injuries (S1-S{hi}); "
            f"S{proposed} assumes a theoretical maximum, not the credible worst case.",
        }
        if adjust:
            out["value"] = hi
    return out


# ─── R5: control credit ─────────────────────────────────────────────────


@dataclass
class Control:
    key: str | None
    hierarchy: str
    description: str
    effectiveness: str | None = None
    verification_method: str | None = None
    evidence_attached: bool = False
    hazard_keys: tuple[str, ...] | None = None  # linked hazards; None = the whole activity
    planned: bool = False  # a recommended control not yet in place
    planned_date: str | None = None


@dataclass
class Hazard:
    key: str
    name: str
    code: str | None = None
    category: str | None = None
    consequence: str | None = None
    likelihood: int | None = None
    severity: int | None = None

    @property
    def text(self) -> str:
        return f"{self.name}. {self.consequence or ''}"

    @property
    def family(self) -> str:
        return hazard_family(self.code, self.category, self.text)


def applies_to(c: Control, h: Hazard) -> tuple[bool, str]:
    if c.hazard_keys is not None:
        return (
            h.key in c.hazard_keys
        ), "linked to this hazard" if h.key in c.hazard_keys else "linked elsewhere"
    fams = control_families(c.description)
    fam = h.family
    if fams:
        if fam in fams or (RELATED.get(fam, set()) & fams):
            return True, f"{'/'.join(sorted(fams)).lower()} control"
        if is_generic_control(c.description) and not (fams - {"FALLING_OBJECT"}):
            return True, "procedure for the whole activity"
        return False, f"addresses {'/'.join(sorted(fams)).lower()}, not {fam.lower()}"
    if is_generic_control(c.description):
        return True, "procedure for the whole activity"
    return True, "not linked to a hazard: counted for the whole activity"


def _credit(c: Control, axis: str | None, steps: int, status: str, reason: str) -> dict[str, Any]:
    return {
        "key": c.key,
        "control": c.description,
        "hierarchy": c.hierarchy,
        "axis": axis,
        "steps": steps,
        "status": status,
        "reason": reason,
    }


def _verified(c: Control) -> bool:
    return c.effectiveness in ("EFFECTIVE", "PARTIALLY_EFFECTIVE")


def residual_from_controls(
    l: int, s: int, controls: list[Control], cfg: dict[str, Any], *, hazard: Hazard
) -> dict[str, Any]:
    """Residual (L, S) for one hazard from its existing controls.

    Elimination / substitution: can lower severity and likelihood.
    Engineering preventive: L-1, or L-2 with 2+ independent ones backed by evidence.
    Engineering mitigating: S-1 when justified (verified or evidence attached).
    Administrative (all combined): L-1 max; provisional without a verification method.
    PPE: L-1 max and only for hazards rated S <= 3. Never severity.
    Baseline site rules: no credit. Total reduction capped at 3 levels; L >= 1.
    """
    cc = cfg["controlCredit"]
    credits: list[dict[str, Any]] = []
    usable: list[Control] = []
    for c in controls:
        ok, why = applies_to(c, hazard)
        if not ok:
            credits.append(_credit(c, None, 0, "NOT_RELEVANT", why))
        elif c.planned:
            credits.append(_credit(c, None, 0, "PLANNED", "planned, no credit until in place"))
        elif c.effectiveness == "INEFFECTIVE":
            credits.append(_credit(c, None, 0, "NO_CREDIT", "rated ineffective"))
        elif is_baseline(c.hierarchy, c.description, cfg):
            credits.append(_credit(c, None, 0, "BASELINE", "baseline site rule: recorded, no credit"))
        else:
            usable.append(c)

    by = {h: [c for c in usable if (c.hierarchy or "").upper() == h] for h in HIERARCHY}
    eng_mit = [c for c in by["ENGINEERING"] if engineering_kind(c.description) == "MITIGATING"]
    eng_prev = [c for c in by["ENGINEERING"] if c not in eng_mit]

    # (controls, axis, steps, provisional, reason) in order of strength.
    grants: list[tuple[list[Control], str, int, bool, str]] = []
    if by["ELIMINATION"]:
        c = by["ELIMINATION"]
        grants.append((c, "S", 1, not any(_verified(x) for x in c), "elimination lowers the severity"))
        grants.append((c, "L", 2, not any(_verified(x) for x in c), "elimination removes most exposure"))
    elif by["SUBSTITUTION"]:
        c = by["SUBSTITUTION"]
        grants.append((c, "S", 1, not any(_verified(x) for x in c), "substitution lowers the severity"))
        grants.append((c, "L", 1, not any(_verified(x) for x in c), "substitution lowers the likelihood"))
    justified_mit = [c for c in eng_mit if _verified(c) or c.evidence_attached or c.verification_method]
    if justified_mit:
        grants.append((justified_mit, "S", 1, False, "mitigating engineering control limits the harm"))
    unjustified_mit = [c for c in eng_mit if c not in justified_mit]
    for c in unjustified_mit:
        credits.append(
            _credit(
                c,
                None,
                0,
                "NO_CREDIT",
                "mitigating control: record its verification to claim the severity step",
            )
        )
    if eng_prev:
        evidenced = [c for c in eng_prev if c.evidence_attached or c.effectiveness == "EFFECTIVE"]
        steps = 2 if len(evidenced) >= 2 else 1
        grants.append(
            (
                eng_prev,
                "L",
                steps,
                not any(_verified(x) for x in eng_prev),
                "two independent engineering controls with evidence"
                if steps == 2
                else "engineering control prevents the event",
            )
        )
    if by["ADMINISTRATIVE"]:
        a = by["ADMINISTRATIVE"]
        grants.append(
            (
                a,
                "L",
                1,
                not any(x.verification_method for x in a),
                "procedures / checks combined (max one step)",
            )
        )
    if by["PPE"]:
        if s <= cc["ppeMaxSeverity"]:
            grants.append(
                (
                    by["PPE"],
                    "L",
                    1,
                    not any(_verified(x) for x in by["PPE"]),
                    "task-specific PPE (hazard S<=3)",
                )
            )
        else:
            for c in by["PPE"]:
                credits.append(_credit(c, None, 0, "NO_CREDIT", f"PPE gets no credit for S{s} hazards"))

    budget = cc["maxTotalSteps"]
    dl = ds = 0
    for ctrls, axis, steps, provisional, reason in grants:
        room = (l - 1 - dl) if axis == "L" else (s - 1 - ds)
        take = max(0, min(steps, budget, room))
        status = "PROVISIONAL" if provisional else "CREDITED"
        if take == 0:
            status, why = (
                "NO_CREDIT",
                (
                    "cap of 3 levels reached"
                    if budget == 0
                    else "likelihood already at L1"
                    if axis == "L"
                    else "severity already at S1"
                ),
            )
        else:
            why = reason + (
                " (unverified: rate its effectiveness or record how it is checked)" if provisional else ""
            )
        for i, c in enumerate(ctrls):
            credits.append(
                _credit(
                    c,
                    axis if take and i == 0 else None,
                    take if i == 0 else 0,
                    status if i == 0 or take == 0 else "COUNTED_WITH_ABOVE",
                    why,
                )
            )
        budget -= take
        if axis == "L":
            dl += take
        else:
            ds += take
    rl, rs = max(1, l - dl), max(1, s - ds)
    provisional = any(cr["status"] == "PROVISIONAL" for cr in credits)
    return {
        "likelihood": rl,
        "severity": rs,
        "stepsL": dl,
        "stepsS": ds,
        "credits": credits,
        "provisional": provisional,
        "creditedCount": sum(1 for cr in credits if cr["steps"]),
    }


def has_real_controls(controls: list[Control], cfg: dict[str, Any]) -> bool:
    """At least one existing, non-baseline control (anything that can earn credit)."""
    return any(not c.planned and not is_baseline(c.hierarchy, c.description, cfg) for c in controls)


# ─── R7: tolerability that terminates ───────────────────────────────────

ALARP_ITEMS = {
    "eliminationConsidered": "Elimination / substitution considered and recorded as not reasonably practicable",
    "engineeringInPlaceOrPlanned": "At least one engineering control in place, or planned with a date",
    "statutoryCurrent": "Statutory examinations for the equipment are current",
    "reviewDateSet": "Review date set",
}


def tolerability(
    residual: dict[str, Any],
    *,
    credible_s: int | None,
    alarp: dict[str, bool],
    strict: bool,
    cfg: dict[str, Any],
    overridden: bool = False,
) -> dict[str, Any]:
    """ACCEPTABLE / TOLERABLE_ALARP / NOT_ACCEPTABLE, with the route that ends it.

    MODERATE with a hazard of S>=4: the deterministic ALARP checklist (all four
    items). MODERATE with S<=3: review date only. Checklist complete ->
    TOLERABLE_ALARP (monitor, review in 12 months) and no more control requests.
    A stricter plant rule (MODERATE not acceptable) -> elevated override with a
    pre-drafted justification instead of more controls.
    """
    level = residual["level"]
    months = cfg["tolerability"]["reviewMonths"]
    if level == "LOW":
        return {
            "state": ACCEPTABLE,
            "route": None,
            "missing": [],
            "overridden": False,
            "note": "Acceptable: keep the controls in place.",
        }
    if level == "MODERATE" and cfg["tolerability"]["alarpBandEnabled"]:
        sev = max(residual["severity"], credible_s or 0)
        items = list(ALARP_ITEMS) if sev >= 4 else ["reviewDateSet"]
        missing = [i for i in items if not alarp.get(i)]
        if missing:
            return {
                "state": NOT_ACCEPTABLE,
                "route": "ALARP_CHECKLIST",
                "missing": missing,
                "overridden": False,
                "note": "Complete the ALARP checklist: " + "; ".join(ALARP_ITEMS[m] for m in missing) + ".",
            }
        if strict:
            return {
                "state": NOT_ACCEPTABLE,
                "route": "OVERRIDE",
                "missing": [],
                "overridden": overridden,
                "note": (
                    "Accepted under the elevated override."
                    if overridden
                    else "The plant rule does not accept MODERATE. The ALARP checklist is complete, so route it "
                    "to the elevated override (Plant Head / Corporate HSE) with the drafted justification "
                    "instead of adding controls."
                ),
            }
        return {
            "state": TOLERABLE_ALARP,
            "route": None,
            "missing": [],
            "overridden": False,
            "note": f"Tolerable (ALARP): monitor and review in {months} months. No further controls needed.",
        }
    return {
        "state": NOT_ACCEPTABLE,
        "route": "OVERRIDE" if overridden else "CONTROLS",
        "missing": [],
        "overridden": overridden,
        "note": (
            "Accepted under the elevated override."
            if overridden
            else f"{level} residual: reduce it with controls, or use the elevated override."
        ),
    }


def resolved(t: dict[str, Any]) -> bool:
    """True when the hazard needs nothing more from the assessor."""
    return t["state"] in (ACCEPTABLE, TOLERABLE_ALARP) or t.get("overridden", False)


# ─── R2: entry rating from hazards ──────────────────────────────────────


def entry_rating(hazards: list[dict[str, Any]]) -> dict[str, Any]:
    """Entry rating = max of hazard residuals (or inherent when residual is not
    assessed), with the top 2 driving hazards."""

    def key(h: dict[str, Any], which: str) -> tuple[int, int]:
        r = h.get(which) or {}
        return (r.get("score") or 0, r.get("severity") or 0)

    rated = [h for h in hazards if h.get("inherent")]
    if not rated:
        return {"inherent": None, "residual": None, "drivers": []}
    inh = max(rated, key=lambda h: key(h, "inherent"))["inherent"]
    with_res = [h for h in rated if h.get("residual")]
    res = max(with_res, key=lambda h: key(h, "residual"))["residual"] if len(with_res) == len(rated) else None
    which = "residual" if res else "inherent"
    drivers = sorted(rated, key=lambda h: (-key(h, which)[0], -key(h, which)[1], h.get("name") or ""))[:2]
    return {
        "inherent": inh,
        "residual": res,
        "drivers": [{"key": d["key"], "name": d["name"], "rating": d.get(which)} for d in drivers],
    }


__all__ = [
    "ACCEPTABLE",
    "ALARP_ITEMS",
    "Control",
    "Evidence",
    "HIERARCHY",
    "Hazard",
    "LEVELS",
    "NOT_ACCEPTABLE",
    "TOLERABLE_ALARP",
    "applies_to",
    "band",
    "calibrate_likelihood",
    "calibrate_severity",
    "control_families",
    "credible_severity",
    "default_likelihood",
    "engineering_kind",
    "entry_rating",
    "has_real_controls",
    "hazard_family",
    "is_baseline",
    "is_generic_control",
    "level_index",
    "rate",
    "rating_text",
    "resolved",
    "residual_from_controls",
    "score",
    "tolerability",
]
