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
    },
    "severity": {
        # Credible worst case per hazard family: min = understated below this,
        # max = overstated above this. s5Keywords lift the floor to 5.
        "rubric": {
            "LIFTING": {
                "min": 4,
                "s5Keywords": ["1 t ", "1t ", "tonne", "ton ", "fatal", "over people", "crushed by"],
            },
            "HEIGHT": {
                "min": 4,
                "s5Keywords": ["fatal", "into the bin", "bin opening", "engulf", "over 2", "2 m", "2m"],
            },
            "MOLTEN": {"min": 4},
            "ELECTRICAL": {"min": 4},
            "CONFINED_SPACE": {"min": 4, "s5Keywords": ["oxygen", "toxic", "engulf", "entrap", "fatal"]},
            "VEHICLE": {"min": 4},
            "PRESSURE": {"min": 4},
            "FIRE_EXPLOSION": {"min": 4},
            "MACHINERY": {"min": 3, "max": 4, "uncapKeywords": ["fatal", "drawn into", "crushed to death"]},
            "FALLING_OBJECT": {"max": 4},
            "HEAT": {"max": 4, "uncapKeywords": ["fatal"]},
            "THERMAL": {"max": 3, "uncapKeywords": ["steam", "molten", "fatal"]},
            "CHEMICAL": {"min": 3, "max": 4, "uncapKeywords": ["toxic gas", "h2s", "hydrogen sulphide", "hydrogen sulfide", "chlorine", "ammonia", "fatal", "asphyx", "carbon monoxide", "cyanide"]},
            "SHARP": {"max": 3},
            "DUST": {"max": 3, "uncapKeywords": ["asbestos", "silica", "mesothelioma", "carcinogen", "silicosis", "benzene", "cancer"]},
            "MANUAL_HANDLING": {"max": 3},
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
    },
    "tolerability": {
        "alarpBandEnabled": True,
        # None = follow the matrix (MODERATE -> UNACCEPTABLE in alarpBands means strict).
        "moderateNotAcceptable": None,
        "reviewMonths": 12,
    },
    "recommendations": {
        "maxPerHazard": 1,
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
HIRA_TYPES = ("TASK_BASED", "HAZOP_PHA", "FMEA", "FIRE_EXPLOSION_STUDY", "MOC_RISK", "EMERGENCY_SCENARIO",
              "OCC_HEALTH", "OTHER")
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
