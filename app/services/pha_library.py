"""HAZOP guideword x parameter library (config, not data).

IEC 61882 guidewords crossed with the process parameters a steel plant HAZOP
actually walks through. Not every pair is meaningful ("Reverse temperature"
is not a deviation anyone writes down), so the library lists the meaningful
combinations only, each with the deviation wording the worksheet starts from.
The team edits the wording on the row; the codes stay for filtering and export.

`generate_rows()` turns a selection of (guideword, parameter) pairs into new
worksheet rows for a node, skipping pairs the node already has and pairs the
library does not consider meaningful. Pure, no DB: tests/test_hazop.py.
"""

from __future__ import annotations

from typing import Any, Iterable

GUIDEWORDS: list[dict[str, str]] = [
    {"code": "NO", "label": "No / None", "meaning": "Complete negation of the design intent"},
    {"code": "MORE", "label": "More", "meaning": "Quantitative increase"},
    {"code": "LESS", "label": "Less", "meaning": "Quantitative decrease"},
    {"code": "AS_WELL_AS", "label": "As well as", "meaning": "Qualitative increase: something in addition"},
    {"code": "PART_OF", "label": "Part of", "meaning": "Qualitative decrease: only part of the intent achieved"},
    {"code": "REVERSE", "label": "Reverse", "meaning": "Logical opposite of the design intent"},
    {"code": "OTHER_THAN", "label": "Other than", "meaning": "Complete substitution"},
    {"code": "EARLY", "label": "Early", "meaning": "Relative to clock time"},
    {"code": "LATE", "label": "Late", "meaning": "Relative to clock time"},
    {"code": "BEFORE", "label": "Before", "meaning": "Relative to order or sequence"},
    {"code": "AFTER", "label": "After", "meaning": "Relative to order or sequence"},
]

PARAMETERS: list[dict[str, str]] = [
    {"code": "FLOW", "label": "Flow"},
    {"code": "PRESSURE", "label": "Pressure"},
    {"code": "TEMPERATURE", "label": "Temperature"},
    {"code": "LEVEL", "label": "Level"},
    {"code": "COMPOSITION", "label": "Composition"},
    {"code": "REACTION", "label": "Reaction"},
    {"code": "PHASE", "label": "Phase"},
    {"code": "TIME", "label": "Time"},
    {"code": "SEQUENCE", "label": "Sequence"},
    {"code": "MIXING", "label": "Mixing"},
    {"code": "UTILITIES", "label": "Utilities"},
    {"code": "MAINTENANCE", "label": "Maintenance"},
    {"code": "STARTUP_SHUTDOWN", "label": "Start-up / Shutdown"},
]

# parameter -> {guideword: deviation wording}. Only meaningful pairs.
COMBINATIONS: dict[str, dict[str, str]] = {
    "FLOW": {
        "NO": "No flow",
        "MORE": "High flow",
        "LESS": "Low flow",
        "REVERSE": "Reverse flow / backflow",
        "AS_WELL_AS": "Contaminant or extra material in the flow",
        "PART_OF": "Loss of a component in the flow",
        "OTHER_THAN": "Flow to the wrong destination / misdirected flow",
    },
    "PRESSURE": {
        "NO": "Loss of pressure / loss of containment",
        "MORE": "High pressure",
        "LESS": "Low pressure / vacuum",
        "REVERSE": "Pressure reversal across a barrier (backflow into a lower-pressure system)",
    },
    "TEMPERATURE": {
        "MORE": "High temperature",
        "LESS": "Low temperature",
        "OTHER_THAN": "Thermal shock / rapid temperature change",
    },
    "LEVEL": {
        "NO": "Empty / no level",
        "MORE": "High level / overfill",
        "LESS": "Low level",
    },
    "COMPOSITION": {
        "MORE": "Higher concentration than intended",
        "LESS": "Lower concentration than intended",
        "AS_WELL_AS": "Impurities or moisture present",
        "PART_OF": "Component missing / wrong ratio",
        "OTHER_THAN": "Wrong material / off-spec composition",
    },
    "REACTION": {
        "NO": "No reaction",
        "MORE": "Runaway / excessive reaction",
        "LESS": "Incomplete reaction",
        "REVERSE": "Reverse reaction / decomposition",
        "AS_WELL_AS": "Side reaction",
        "OTHER_THAN": "Unintended reaction (e.g. molten metal with water)",
    },
    "PHASE": {
        "AS_WELL_AS": "Additional phase present (two-phase flow, entrained liquid or solids)",
        "OTHER_THAN": "Phase change (flashing, freezing, condensation, solidification)",
    },
    "TIME": {
        "MORE": "Step takes too long",
        "LESS": "Step too short",
        "EARLY": "Action too early",
        "LATE": "Action too late",
    },
    "SEQUENCE": {
        "NO": "Step omitted",
        "BEFORE": "Step performed before its time",
        "AFTER": "Step performed after its time",
        "REVERSE": "Steps performed in reverse order",
        "PART_OF": "Step only partly completed",
        "OTHER_THAN": "Wrong action taken",
    },
    "MIXING": {
        "NO": "No mixing",
        "MORE": "Excessive mixing / agitation",
        "LESS": "Poor mixing / stratification",
    },
    "UTILITIES": {
        "NO": "Loss of utility (power, instrument air, cooling water, nitrogen, steam)",
        "LESS": "Reduced utility supply",
        "PART_OF": "Partial utility failure (one feeder, one pump)",
        "OTHER_THAN": "Wrong or contaminated utility",
    },
    "MAINTENANCE": {
        "NO": "Maintenance not done / isolation not provided",
        "PART_OF": "Incomplete isolation or partial maintenance",
        "OTHER_THAN": "Wrong maintenance procedure or spare part",
        "LATE": "Overdue inspection or preventive maintenance",
    },
    "STARTUP_SHUTDOWN": {
        "NO": "Emergency shutdown fails to act",
        "BEFORE": "Start-up before pre-start checks / purge complete",
        "EARLY": "Premature restart after trip",
        "LATE": "Delayed shutdown on demand",
        "OTHER_THAN": "Abnormal start-up or shutdown sequence",
    },
}

GUIDEWORD_CODES = tuple(g["code"] for g in GUIDEWORDS)
PARAMETER_CODES = tuple(p["code"] for p in PARAMETERS)
_GW_LABEL = {g["code"]: g["label"] for g in GUIDEWORDS}
_PARAM_LABEL = {p["code"]: p["label"] for p in PARAMETERS}


def guideword_label(code: str | None) -> str:
    return _GW_LABEL.get(code or "", code or "")


def parameter_label(code: str | None) -> str:
    return _PARAM_LABEL.get(code or "", code or "")


def is_meaningful(guideword: str, parameter: str) -> bool:
    return guideword in COMBINATIONS.get(parameter, {})


def deviation_text(guideword: str, parameter: str) -> str | None:
    return COMBINATIONS.get(parameter, {}).get(guideword)


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


_GW_ALIASES = {
    **{_norm(g["code"]): g["code"] for g in GUIDEWORDS},
    **{_norm(g["label"]): g["code"] for g in GUIDEWORDS},
    "NO": "NO", "NONE": "NO", "HIGH": "MORE", "HIGHER": "MORE", "LOW": "LESS", "LOWER": "LESS",
    "ASWELLAS": "AS_WELL_AS", "PARTOF": "PART_OF", "OTHERTHAN": "OTHER_THAN", "INSTEADOF": "OTHER_THAN",
}
_PARAM_ALIASES = {
    **{_norm(p["code"]): p["code"] for p in PARAMETERS},
    **{_norm(p["label"]): p["code"] for p in PARAMETERS},
    "STARTUP": "STARTUP_SHUTDOWN", "SHUTDOWN": "STARTUP_SHUTDOWN", "UTILITY": "UTILITIES", "TEMP": "TEMPERATURE",
}


def normalise_guideword(value: str | None) -> str | None:
    """Map a free-text guideword ("No / None", "more", "As well as") to its
    library code, or None when it is not a library guideword."""
    if not value:
        return None
    return _GW_ALIASES.get(_norm(value))


def normalise_parameter(value: str | None) -> str | None:
    if not value:
        return None
    return _PARAM_ALIASES.get(_norm(value))


def library() -> dict[str, Any]:
    """The library as the FE renders it: a matrix of meaningful cells."""
    return {
        "guidewords": GUIDEWORDS,
        "parameters": PARAMETERS,
        "combinations": [
            {"guideword": gw, "parameter": param, "deviation": text}
            for param in PARAMETER_CODES
            for gw, text in COMBINATIONS.get(param, {}).items()
        ],
    }


def generate_rows(
    selected: Iterable[tuple[str, str]] | Iterable[dict[str, str]],
    existing: Iterable[tuple[str | None, str | None]] = (),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Rows to create for a node from a selection of (guideword, parameter).

    Returns (rows, skipped). Rows come out in library order (parameter, then
    guideword) regardless of selection order, so a generated worksheet reads
    the way a HAZOP is walked. A pair is skipped when it is not meaningful,
    already on the node, or selected twice.
    """
    have = {(g, p) for g, p in existing if g and p}
    wanted: set[tuple[str, str]] = set()
    skipped: list[dict[str, str]] = []
    for item in selected:
        gw, param = (item.get("guideword"), item.get("parameter")) if isinstance(item, dict) else item
        gw = normalise_guideword(gw) or (gw or "")
        param = normalise_parameter(param) or (param or "")
        if not is_meaningful(gw, param):
            skipped.append({"guideword": gw, "parameter": param, "reason": "not a meaningful combination"})
        elif (gw, param) in have:
            skipped.append({"guideword": gw, "parameter": param, "reason": "already on this node"})
        else:
            wanted.add((gw, param))
    rows = [
        {"guideword": gw, "parameter": param, "deviation": text}
        for param in PARAMETER_CODES
        for gw, text in COMBINATIONS.get(param, {}).items()
        if (gw, param) in wanted
    ]
    return rows, skipped


__all__ = [
    "GUIDEWORDS", "PARAMETERS", "COMBINATIONS", "GUIDEWORD_CODES", "PARAMETER_CODES",
    "guideword_label", "parameter_label", "is_meaningful", "deviation_text",
    "normalise_guideword", "normalise_parameter", "library", "generate_rows",
]
