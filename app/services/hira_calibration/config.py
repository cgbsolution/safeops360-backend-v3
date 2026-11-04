"""Calibration settings for HIRA v2.

Defaults live here. A tenant-wide row (plantId NULL) and a plant row in
"HiraCalibrationConfig" can override any key; the newest active version wins.
Every value is plain JSON so a row can carry a partial override.

The plant's own matrix still decides what is acceptable: when a matrix marks
MODERATE as unacceptable (e.g. a plant rule such as QSD/24/F "GT > 3.33 is
significant"), tolerability follows that stricter rule automatically.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

ENGINE_VERSION = "2.0.0"
FLAG = "hira.calibration.v2"
MODES = ("OFF", "SHADOW", "ON")

# Frequency words that mark a likelihood scale as "how often" (vs a plant index).
_FREQUENCY_WORDS = (
    "rare",
    "unlikely",
    "possible",
    "likely",
    "almost certain",
    "remote",
    "improbable",
    "occasional",
    "frequent",
    "probable",
    "certain",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "schema": 1,
    # Industry rubric pack for the plant (round 3 M): STEEL_HEAVY | APPAREL_TEXTILE |
    # CHEMICAL_PHARMA | WAREHOUSE_LOGISTICS. None = no pack: the review shows
    # "Not validated for this industry" and no residual-band claims.
    "rubricPack": None,
    # One set of bands for every 5x5 cell: score = L x S.
    "thresholds": {"lowMax": 3, "moderateMax": 6, "highMax": 12},
    "likelihood": {
        "scale": "AUTO",  # AUTO (from the matrix labels) | FREQUENCY | PLANT_INDEX
        "anchors": {
            "1": "Not expected in 10+ years",
            "2": "Once in 5-10 years",
            "3": "Once in 1-5 years",
            "4": "Once a year or more - needs a recorded event in the evidence window or a documented control failure",
            "5": "Several times a year - needs 2 or more recorded events in 12 months",
        },
        "evidenceWindowMonths": 24,
        "l5EventsIn12Months": 2,
        "capWithoutEvidence": 3,
        "defaultRoutine": 2,
        "defaultHighExposure": 3,
        "highExposureFrequencies": ["DAILY", "CONTINUOUS"],
        # An AI draft's per-hazard likelihood may sit at most this many steps below the exposure
        # default without evidence (the Assistant's single entry-level number: not below it at all).
        "draftMaxStepsBelowDefault": 1,
    },
    "severity": {
        # Credible worst case per hazard family (round 2 B). A floor ("min") only
        # applies when the hazard text names the high-consequence scenario
        # ("floorWhen"; no list = the family itself is that scenario). "rules"
        # are scenario rules, first match wins; each has an id for the report.
        "workAtHeightThresholdM": 2.0,  # plant-configurable; placeholder, HSE lead to confirm
        "rubric": {
            "LIFTING": {
                "min": 4,
                "floorWhen": [
                    "crane",
                    "hoist",
                    "suspended",
                    "dropped load",
                    "falling load",
                    "load fall",
                    "over people",
                    "tonne",
                    "1 t ",
                    "sling",
                    "rigging",
                    "hook",
                    "eot",
                    "lifting equipment",
                    "lifting gear",
                    "swinging load",
                    "bag",
                ],
                "s5Keywords": ["1 t ", "1t ", "tonne", "ton ", "fatal", "over people", "crushed by"],
            },
            "HEIGHT": {
                "min": 4,
                "heightRule": True,
                "s5Keywords": ["fatal", "into the bin", "bin opening", "engulf"],
            },
            "MOLTEN": {"min": 4},
            "ELECTRICAL": {
                "min": 4,
                "floorWhen": [
                    "panel",
                    "mcc",
                    "switchgear",
                    "busbar",
                    "live",
                    "arc",
                    "kv",
                    "high voltage",
                    "hv ",
                    "transformer",
                    "electrocut",
                    "fatal",
                    "415",
                    "440",
                    "substation",
                    "overhead line",
                    "wet",
                    "damaged cable",
                    "grinder cable",
                    "hand lamp",
                ],
            },
            "CONFINED_SPACE": {"min": 4, "s5Keywords": ["oxygen", "toxic", "engulf", "entrap", "fatal"]},
            "VEHICLE": {
                "min": 4,
                "floorWhen": [
                    "truck",
                    "forklift",
                    "tipper",
                    "dumper",
                    "loader",
                    "trailer",
                    "carrier",
                    "vehicle",
                    "reversing",
                    "run over",
                    "struck",
                    "collision",
                    "crush",
                    "pedestrian",
                    "fatal",
                    "overturn",
                    "transfer car",
                ],
            },
            "PRESSURE": {
                "min": 4,
                "floorWhen": [
                    "receiver",
                    "vessel",
                    "boiler",
                    "rupture",
                    "burst",
                    "explosion",
                    "compressed",
                    "high pressure",
                    "bar",
                    "injection",
                    "fatal",
                    "blast",
                    "hydraulic",
                ],
            },
            "FIRE_EXPLOSION": {
                "min": 4,
                # flammable inventory: stored fuels, gases, solvents (not combustible waste)
                "floorWhen": [
                    "lpg",
                    "png",
                    "gas",
                    "fuel",
                    "oil drum",
                    "oil tank",
                    "oil store",
                    "oil storage",
                    "furnace oil",
                    "hydraulic oil",
                    "solvent",
                    "paint",
                    "thinner",
                    "flammable",
                    "cylinder",
                    "acetylene",
                    "oxygen",
                    "diesel",
                    "petrol",
                    "hydrogen",
                    "explosion",
                    "explosive",
                    "molten",
                    "flashback",
                ],
                "rules": [
                    {
                        "id": "FIRE_ROUTINE_SPARKS_S3",
                        "when": ["spark", "grinding", "cutting", "welding", "hot work"],
                        "unless": [
                            "lpg",
                            "png",
                            "gas",
                            "fuel",
                            "oil drum",
                            "oil tank",
                            "oil store",
                            "oil storage",
                            "furnace oil",
                            "solvent",
                            "paint",
                            "thinner",
                            "flammable",
                            "cylinder",
                            "acetylene",
                            "diesel",
                            "petrol",
                            "hydrogen",
                            "explosion",
                            "explosive",
                            "flashback",
                        ],
                        "max": 3,
                        "noFloor": True,
                    }
                ],
            },
            "MACHINERY": {
                "min": 3,
                "max": 4,
                "floorWhen": [
                    "entangle",
                    "caught",
                    "drawn",
                    "amputat",
                    "crush",
                    "press",
                    "rotating",
                    "nip",
                    "conveyor",
                    "shear",
                    "pulley",
                ],
                "uncapKeywords": ["fatal", "drawn into", "crushed to death"],
            },
            "CHEMICAL": {
                "min": 3,
                "max": 4,
                "floorWhen": [
                    "corrosive",
                    "acid",
                    "alkali",
                    "caustic",
                    "burn",
                    "eye",
                    "splash",
                    "toxic",
                    "vapour",
                ],
                "uncapKeywords": [
                    "toxic gas",
                    "h2s",
                    "hydrogen sulphide",
                    "hydrogen sulfide",
                    "chlorine",
                    "ammonia",
                    "fatal",
                    "asphyx",
                    "carbon monoxide",
                    "cyanide",
                ],
            },
            "FALLING_OBJECT": {"max": 4},
            "HEAT": {
                "max": 3,
                "rules": [
                    {
                        "id": "HEAT_AMBIENT_S3",
                        "when": [
                            "ambient",
                            "outdoor",
                            "outdoors",
                            "summer",
                            "sun",
                            "cabin",
                            "cab ",
                            "gate",
                            "heat exhaustion",
                            "heat stress",
                            "weather",
                        ],
                        "unless": ["close range", "close-range", "taphole", "within 1 m", "within 2 m"],
                        "max": 3,
                        "noFloor": True,
                    },
                    {
                        "id": "HEAT_RADIANT_CLOSE_S4",
                        "when": [
                            "close range",
                            "close-range",
                            "near the furnace",
                            "furnace",
                            "ladle",
                            "tapping",
                            "taphole",
                            "molten",
                            "within 1 m",
                            "within 2 m",
                            "hot surface",
                            "radiant",
                        ],
                        "min": 4,
                        "max": 4,
                    },
                ],
                "uncapKeywords": ["fatal"],
            },
            "THERMAL": {"max": 3, "uncapKeywords": ["steam", "molten", "fatal"]},
            "SHARP": {"max": 3},
            "DUST": {
                "max": 3,
                "uncapKeywords": [
                    "asbestos",
                    "silica",
                    "mesothelioma",
                    "carcinogen",
                    "silicosis",
                    "benzene",
                    "cancer",
                ],
            },
            "MANUAL_HANDLING": {
                "max": 2,
                "rules": [
                    {
                        "id": "MANUAL_HEAVY_LOAD_S3",
                        "when": [
                            "heavy",
                            "bale",
                            "sack",
                            "drum",
                            "carboy",
                            "full bin",
                            "tube",
                            "roll",
                            "cylinder",
                            "bag",
                            "box",
                            "carton",
                            "awkward load",
                        ],
                        "heavyKg": 20,
                        "max": 3,
                    }
                ],
            },
            "SLIP_TRIP": {"max": 3},
            "NOISE": {"max": 3},
            "PSYCHOSOCIAL": {"max": 3},
        },
    },
    "controlCredit": {
        "maxTotalSteps": 3,
        "ppeMaxSeverity": 3,  # PPE earns L-1 only for hazards rated S <= this
        # Baseline site rules: recorded, never credited, never recommended.
        "baselineKeywords": [
            "helmet",
            "hard hat",
            "safety shoe",
            "safety shoes",
            "safety boot",
            "gumboot",
            "hi-vis",
            "hi vis",
            "high visibility",
            "reflective jacket",
            "reflective vest",
            "hand gloves",
            "glove",
            "cotton gloves",
            "leather gloves",
            "gloves",
            "induction",
            "general work permit",
            "work permit system",
            "general permit",
            "ppe as per norms",
            "basic ppe",
            "standard ppe",
            "uniform",
            "ppe",
        ],
        # A PPE item naming one of these is task-specific, not baseline.
        "specificQualifiers": [
            "cut",
            "heat",
            "alumin",
            "chemical",
            "insulat",
            "nitrile",
            "rubber",
            "respirat",
            "ffp",
            "n95",
            "p2",
            "p3",
            "face shield",
            "harness",
            "lanyard",
            "arc",
            "flame",
            "fr ",
            "welding",
            "leg guard",
            "apron",
            "earmuff",
            "ear muff",
            "ear plug",
            "goggle",
            "visor",
            "kevlar",
            "anti-vibration",
            "full body",
            "breathing apparatus",
            "scba",
        ],
        # Words of a remark that follows baseline PPE ("as per site rule", "to be worn at all
        # times"): such a remark does not make baseline PPE task-specific.
        "baselineFillerWords": [
            "a", "all", "always", "an", "and", "are", "area", "areas", "as", "at", "basic", "be", "by", "company",
            "compulsory", "during", "employees", "every", "everyone", "for", "general", "in", "inside", "is",
            "job", "mandatory", "must", "norm", "norms", "of", "on", "per", "person", "persons", "plant",
            "policy", "premises", "required", "rule", "rules", "shall", "shop", "site", "standard", "standards",
            "the", "time", "times", "to", "use", "used", "wear", "wearing", "while", "with", "work", "workers",
            "working", "worn",
        ],
        # PPE items that are not baseline: a PPE line naming one of these is task-specific.
        "ppeItemNouns": [
            "mask", "glasses", "spectacle", "shield", "suit", "coverall", "sleeve", "gauntlet", "ear defender",
            "knee pad", "arm guard", "shin guard", "belt", "jacket", "boot", "cap", "protection", "protector",
            "defender", "ear", "eye", "hearing", "face", "plug", "muff", "screen", "hood",
        ],
    },
    "tolerability": {
        "alarpBandEnabled": True,
        # Round 2 A: hazards rated S <= lowSeverityMax are never put through the
        # ALARP checklist; LOW / MODERATE is ACCEPTABLE once a non-baseline
        # control with a recorded verification method is in place.
        "lowSeverityMax": 3,
        "requireVerifiedControlForModerate": True,
        # None = follow the matrix (MODERATE -> UNACCEPTABLE in alarpBands means strict).
        "moderateNotAcceptable": None,
        "reviewMonths": 12,
    },
    "recommendations": {
        "maxPerHazard": 1,  # = maxRecsPerHazard
        "secondRecWhenSevere": True,  # a 2nd, independent control for an S>=4 hazard the 1st leaves open
        "maxPerEntryLowSeverity": 2,  # entries whose highest inherent severity is <= 3
        "maxPerEntryLowModerate": 3,
        "maxPerEntryHighCritical": 5,
        "maxCapexPerEntry": 1,
        "highCostMinResidual": "HIGH",
        "dueDays": {"CRITICAL": 30, "HIGH": 60, "MODERATE": 90, "LOW": 180},
        "ownerRoles": {
            "ELIMINATION": "Section Head",
            "SUBSTITUTION": "Section Head",
            "ENGINEERING": "Maintenance Head",
            "ADMINISTRATIVE": "Section Head",
            "PPE": "Safety Officer",
        },
        "costWeight": {"LOW": 1, "MEDIUM": 2, "HIGH": 4, "VERY_HIGH": 8},
        "genericFiller": [
            "awareness training",
            "awareness programme",
            "awareness program",
            "update sop",
            "revise sop",
            "update the sop",
            "toolbox talk",
            "sensitisation",
            "sensitization",
            "safety awareness",
            "be careful",
            "follow sop",
            "proper supervision",
        ],
        "dedupeSimilarity": 0.4,  # Jaccard, same hierarchy group
        "linkSimilarity": 0.6,  # share of the shorter text's words (min 3), for open CAPAs / siblings
    },
    # Round 2 C: when controls are not captured, the assessor confirms the
    # routine controls in place instead of getting recommendations.
    "routineControls": {
        "_ALL": [
            ["ADMINISTRATIVE", "Work instruction / SOP for this task, briefed to the crew"],
            ["ADMINISTRATIVE", "Supervisor check before the task starts"],
        ],
        "SLIP_TRIP": [
            ["ADMINISTRATIVE", "Housekeeping schedule with spillage clean-up, checked by the supervisor"]
        ],
        "SHARP": [["ADMINISTRATIVE", "Safe handling of sharp scrap and cut-ends in the work instruction"]],
        "MANUAL_HANDLING": [
            ["ADMINISTRATIVE", "Manual handling briefing and team-lift rule for heavy items"]
        ],
        "DUST": [
            [
                "ADMINISTRATIVE",
                "Dust control routine (damp sweeping / extraction check) in the work instruction",
            ]
        ],
        "NOISE": [["ADMINISTRATIVE", "Hearing protection zone signage and issue register"]],
        "HEAT": [["ADMINISTRATIVE", "Heat stress routine: drinking water, shade and rest breaks"]],
        "THERMAL": [["ADMINISTRATIVE", "Hot surface marking and do-not-touch briefing"]],
        "ELECTRICAL": [
            ["ADMINISTRATIVE", "Inspection register for portable electrical equipment and extension boards"]
        ],
        "PSYCHOSOCIAL": [["ADMINISTRATIVE", "Shift roster and workload review by the supervisor"]],
    },
    "evidence": {
        # Which incidents / near misses count for an entry: the same area
        # (default), an area group from areaGroups, or the whole plant.
        "areaScope": "AREA",
        "areaGroups": {},
    },
    "findings": {"maxBlockersShown": 3, "defaultShown": 3},
    "assistant": {"maxHazardsRoutine": 5, "maxHazardsNonRoutine": 7},
}


def deep_merge(base: dict[str, Any], over: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# HIRA types (round 3, K). Calibration rules run for TASK_BASED only; the
# others get structural completeness checks and a neutral banner.
HIRA_TYPES = (
    "TASK_BASED",
    "HAZOP_PHA",
    "FMEA",
    "FIRE_EXPLOSION_STUDY",
    "MOC_RISK",
    "EMERGENCY_SCENARIO",
    "OCC_HEALTH",
    "OTHER",
)
CALIBRATED_TYPES = ("TASK_BASED",)


@dataclass(frozen=True)
class MatrixRule:
    """What the study's matrix says about the plant's own acceptance rule.
    `matrix` is the study matrix through the adapter (matrix.MatrixConfig);
    None means the built-in config for `code`."""

    code: str | None
    likelihood_scale: str  # FREQUENCY | PLANT_INDEX
    moderate_not_acceptable: bool
    matrix: Any = None


def matrix_rule(matrix: Any, cfg: dict[str, Any], likelihood_labels: list[str] | None = None) -> MatrixRule:
    """Derive the plant rule from the matrix (alarpBands / acceptableResidual)
    unless the config pins it."""
    code = getattr(matrix, "code", None)
    scale = cfg["likelihood"].get("scale", "AUTO")
    if scale == "AUTO":
        labels = [(l or "").lower() for l in (likelihood_labels or [])]
        scale = (
            "FREQUENCY"
            if (not labels or any(any(w in l for w in _FREQUENCY_WORDS) for l in labels))
            else "PLANT_INDEX"
        )
    strict = cfg["tolerability"].get("moderateNotAcceptable")
    if strict is None:
        bands = getattr(matrix, "alarpBands", None) or {}
        acc = getattr(matrix, "acceptableResidual", None) or {}
        strict = bands.get("MODERATE") == "UNACCEPTABLE" or (
            acc.get("routine") == "LOW" and bands.get("MODERATE") not in ("TOLERABLE", "BROADLY_ACCEPTABLE")
        )
    return MatrixRule(code=code, likelihood_scale=scale, moderate_not_acceptable=bool(strict))


# ─── DB loading ─────────────────────────────────────────────────────────


_TABLES: dict[str, tuple[bool, float]] = {}


async def table_exists(db: Any, name: str) -> bool:
    """Cached (5 min) check, so a missing table never aborts the caller's transaction."""
    import time  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    hit = _TABLES.get(name)
    if hit and time.monotonic() - hit[1] < 300:
        return hit[0]
    n = (
        await db.execute(
            text("SELECT count(*) FROM information_schema.tables WHERE table_name = :t"), {"t": name}
        )
    ).scalar()
    _TABLES[name] = (bool(n), time.monotonic())
    return bool(n)


def with_matrix(cfg: dict[str, Any], rule: MatrixRule) -> dict[str, Any]:
    """The config with the study matrix attached (under "_matrix"), so every
    rating in one evaluation reads its band from the same matrix."""
    if cfg.get("_matrix") is not None:
        return cfg
    from app.services.hira_calibration import matrix as mx  # noqa: PLC0415

    return {**cfg, "_matrix": rule.matrix if rule.matrix is not None else mx.builtin(rule.code, cfg)}


async def load(db: Any, plant_id: str | None) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """(effective config, mode, version info) for a plant. Mode defaults to OFF."""
    from sqlalchemy import or_, select  # noqa: PLC0415

    from app.models.hira_calibration import HiraCalibrationConfig  # noqa: PLC0415

    if not await table_exists(db, "HiraCalibrationConfig"):  # DDL not applied yet: behave as OFF
        return copy.deepcopy(DEFAULT_CONFIG), "OFF", {"tenant": None, "plant": None, "engine": ENGINE_VERSION}
    rows = (
        (
            await db.execute(
                select(HiraCalibrationConfig)
                .where(HiraCalibrationConfig.isActive.is_(True))
                .where(
                    or_(HiraCalibrationConfig.plantId.is_(None), HiraCalibrationConfig.plantId == plant_id)
                )
                .order_by(HiraCalibrationConfig.version.desc())
            )
        )
        .scalars()
        .all()
    )
    tenant = next((r for r in rows if r.plantId is None), None)
    plant = next((r for r in rows if r.plantId == plant_id), None) if plant_id else None
    cfg = deep_merge(DEFAULT_CONFIG, tenant.config if tenant else None)
    cfg = deep_merge(cfg, plant.config if plant else None)
    mode = (
        (plant.mode if plant and plant.mode else None)
        or (tenant.mode if tenant and tenant.mode else None)
        or "OFF"
    )
    if mode not in MODES:
        mode = "OFF"
    return (
        cfg,
        mode,
        {
            "tenant": tenant.version if tenant else None,
            "plant": plant.version if plant else None,
            "engine": ENGINE_VERSION,
        },
    )


__all__ = [
    "CALIBRATED_TYPES",
    "HIRA_TYPES",
    "with_matrix",
    "DEFAULT_CONFIG",
    "ENGINE_VERSION",
    "FLAG",
    "MODES",
    "MatrixRule",
    "deep_merge",
    "load",
    "matrix_rule",
    "table_exists",
]
