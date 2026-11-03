"""Industry rubric packs (round 3, M) and the scope of validity (K, N).

A pack holds an industry's family severity anchors and rubric rules, likelihood
anchors, baseline site rules (never credited, never recommended), routine
controls, default hazard-library families and regulation-mapping keys. Packs
are JSON files in ./packs; their `status` (DRAFT | VALIDATED) is set by a
person editing the file, never by code.

A plant chooses its pack with the calibration setting "rubricPack". When the
plant has no pack, the pack is DRAFT, or the study is not task-based, the review
shows a banner and residual-band claims are suppressed (inherent rating plus
suggested controls only).
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.hira_calibration.config import CALIBRATED_TYPES, deep_merge

PACKS_DIR = Path(__file__).with_name("packs")
STATUSES = ("DRAFT", "VALIDATED")
NOT_VALIDATED_BANNER = "Not validated for this industry: ratings are suggestions"
NOT_CALIBRATED_BANNER = "Calibrated rules are not applied to this study type."
# Keys a pack may override in the calibration settings.
PACK_KEYS = ("severity", "likelihood", "controlCredit", "routineControls", "recommendations")


@lru_cache(maxsize=16)
def _read(code: str) -> dict[str, Any]:
    p = PACKS_DIR / f"{code}.json"
    if not p.is_file():
        raise KeyError(f"unknown rubric pack {code!r}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("status") not in STATUSES:
        raise ValueError(f"pack {code}: status must be one of {STATUSES}")
    return data


def load_pack(code: str) -> dict[str, Any]:
    return copy.deepcopy(_read(code))


def list_packs() -> list[dict[str, Any]]:
    out = []
    for p in sorted(PACKS_DIR.glob("*.json")):
        d = _read(p.stem)
        out.append(
            {
                "code": d["code"],
                "name": d["name"],
                "status": d["status"],
                "industries": d.get("industries", []),
            }
        )
    return out


def validated_packs() -> list[dict[str, Any]]:
    return [p for p in list_packs() if p["status"] == "VALIDATED"]


def apply_pack(cfg: dict[str, Any], code: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The settings with the pack's overrides merged in, and the pack's meta
    (None when the plant has no pack or the code is unknown)."""
    if not code:
        return cfg, None
    try:
        pack = _read(code)
    except (KeyError, ValueError):
        return cfg, None
    over = {k: v for k, v in (pack.get("overrides") or {}).items() if k in PACK_KEYS}
    meta = {
        "code": pack["code"],
        "name": pack["name"],
        "status": pack["status"],
        "defaultHazardFamilies": pack.get("defaultHazardFamilies", []),
        "regulationKeys": pack.get("regulationKeys", []),
    }
    return deep_merge(cfg, over), meta


def scope_for(pack: dict[str, Any] | None, hira_type: str) -> dict[str, Any]:
    calibrated = hira_type in CALIBRATED_TYPES
    validated = calibrated and pack is not None and pack.get("status") == "VALIDATED"
    banner = None
    if not calibrated:
        banner = NOT_CALIBRATED_BANNER
    elif not validated:
        banner = NOT_VALIDATED_BANNER
    return {
        "hiraType": hira_type,
        "calibrated": calibrated,
        "validated": validated,
        "pack": pack and {"code": pack["code"], "name": pack["name"], "status": pack["status"]},
        "banner": banner,
        "validatedFor": validated_for(),
    }


def validated_for() -> str:
    names = [p["name"] for p in validated_packs()]
    return "task-based HIRA" + (", " + ", ".join(names) if names else "")


_TOLERABILITY_CODES = {
    "HAZARD_NOT_ACCEPTABLE",
    "ALARP_ENGINEERING",
    "ALARP_RECORD",
    "OVERRIDE_ROUTE",
    "NO_FURTHER_CONTROL",
    "CONFIRM_ROUTINE_CONTROLS",
    "SAVED_RESIDUAL_LOWER",
    "NO_RATING_CREDIT",
}


def apply_scope(result: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    """Attach the scope; when not validated, drop every residual-band claim:
    residuals, tolerability, entry state and the findings that rest on them.
    Recommendations stay, as suggested controls; nothing blocks submission."""
    out = {**result, "scope": scope}
    if scope["validated"] or not result.get("calibrated", True):
        return out
    entry = {
        **result["entry"],
        "residual": None,
        "state": None,
        "status": "NOT_VALIDATED",
        "residualLabel": scope["banner"],
    }
    hazards = [
        {**h, "residual": None, "residualAssessed": False, "tolerability": None, "credits": []}
        for h in result["hazards"]
    ]
    findings = [
        {**f, "class": "INFO"} if f["class"] == "BLOCKER" else f
        for f in result["findings"]
        if f["code"] not in _TOLERABILITY_CODES
    ]
    recs = [
        {
            **r,
            "kind": "SUGGESTION",
            "raisesCapa": False,
            "residualAfter": None,
            "expectedEffect": "Suggested control (not rated for this industry)",
        }
        for r in result["recommendations"]
    ]
    return {
        **out,
        "entry": entry,
        "hazards": hazards,
        "findings": findings,
        "shownFindings": findings[:3],
        "counts": {k: sum(1 for f in findings if f["class"] == k) for k in ("BLOCKER", "ADVISORY", "INFO")},
        "canSubmit": True,
        "recommendations": recs,
        "controlsJustify": [],
        "alarpDraft": None,
        "uncoveredHazards": [],
    }


__all__ = [
    "NOT_CALIBRATED_BANNER",
    "NOT_VALIDATED_BANNER",
    "apply_pack",
    "apply_scope",
    "list_packs",
    "load_pack",
    "scope_for",
    "validated_for",
    "validated_packs",
]
