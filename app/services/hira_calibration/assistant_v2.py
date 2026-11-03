"""Calibration of LLM drafts (HIRA Assistant suggestions, Copilot-drafted entries).

R1  The LLM proposes hazards, likelihood / severity with a rationale, and
    controls. Any residual number or band it outputs is ignored: code computes
    residuals, bands, tolerability and the recommendation list.
R2  Rated per hazard; at most 5 credible hazards for a routine activity (7 for
    non-routine) unless the user expands; generic hazards need "how it
    manifests" text. The entry rating is derived (max).
R3  Likelihood capped by recorded evidence (L4/L5 need events or a control failure).
R4  Severity moved into the credible worst-case range (cuts, dust, manual
    handling S1-S3; dropped crane loads, falls into bins S4-S5).
R6  Existing controls first: proposals from plant data are returned for one click.
R8  LLM-proposed controls pass the same caps, de-duplication and practicability filter.
R10 Blank regulation fields get a suggestion from the regulation map ("verify").
"""

from __future__ import annotations

import re
from typing import Any

from app.services.hira_calibration import recommend as rec
from app.services.hira_calibration import review_v2 as rv
from app.services.hira_calibration import risk_engine as eng
from app.services.hira_calibration.config import MatrixRule, with_matrix

GENERIC_HAZARD = re.compile(
    r"^\s*(machine|machinery|tool|equipment|mechanical|general|other|misc\w*)(\s*(/|or|and)\s*\w+)?\s*(hazard|failure"
    r"\s*hazard)?\s*$|person may (get )?injur|may cause injury|^\s*injury\s*$",
    re.I,
)


def needs_manifestation(name: str | None, consequence: str | None) -> bool:
    c = (consequence or "").strip()
    return (
        len(c) < 15
        or bool(GENERIC_HAZARD.search(c))
        or bool(GENERIC_HAZARD.search(name or "") and len(c) < 25)
    )


def calibrate_draft(
    draft: dict[str, Any],
    *,
    cfg: dict[str, Any],
    rule: MatrixRule,
    evidence: eng.Evidence,
    plant: rv.PlantData | None = None,
    expand: bool = False,
) -> dict[str, Any]:
    """draft: {"activity", "routine", "frequency", "hazards": [{key, name, code, category, consequence,
    likelihood, severity, likelihoodRationale?, severityRationale?, relevance?}], "existingControls":
    [{hierarchy, description, effectiveness?, hazardKeys?}], "recommendedControls": [{hierarchy, description,
    costBand?, hazardKeys?}]}. Residual fields in the draft are ignored."""
    cfg = with_matrix(cfg, rule)
    routine = (draft.get("routine") or "ROUTINE").upper()
    notes: list[str] = []
    hazards_in = [h for h in draft.get("hazards") or [] if h.get("key") or h.get("name")]
    calibrated = []
    for i, h in enumerate(hazards_in):
        hz = eng.Hazard(
            key=str(h.get("key") or f"h{i}"),
            name=h.get("name") or "Hazard",
            code=h.get("code"),
            category=h.get("category"),
            consequence=h.get("consequence"),
        )
        fam = hz.family
        lcal = eng.calibrate_likelihood(
            h.get("likelihood"),
            routine=routine,
            frequency=draft.get("frequency"),
            evidence=evidence.for_family(fam),
            scale=rule.likelihood_scale,
            cfg=cfg,
            draft=True,  # an LLM draft is capped by evidence even on a plant-index scale
        )
        scal = eng.calibrate_severity(h.get("severity"), family=fam, text=hz.text, cfg=cfg, adjust=True)
        if scal["value"] is None:
            lo, hi = scal["credibleMin"], scal["credibleMax"]
            scal["value"] = lo or min(hi or 3, 3)
        hz.likelihood, hz.severity = lcal["value"], scal["value"]
        if lcal["capped"]:
            notes.append(
                f"{hz.name}: likelihood L{h.get('likelihood')} -> L{lcal['value']} ({lcal['reason']})"
            )
        if scal["finding"]:
            notes.append(
                f"{hz.name}: severity S{h.get('severity')} -> S{scal['value']} ({scal['finding']['reason']})"
            )
        calibrated.append(
            {
                "hazard": hz,
                "family": fam,
                "relevance": float(h.get("relevance") or 0.0),
                "likelihood": lcal,
                "severity": scal,
                "manifestationNeeded": needs_manifestation(hz.name, hz.consequence),
                "inherent": eng.rate(hz.likelihood, hz.severity, cfg),
                "likelihoodRationale": (h.get("likelihoodRationale") or "").strip()
                + (f" [{lcal['reason']}]" if lcal["capped"] else ""),
                "severityRationale": (h.get("severityRationale") or "").strip()
                + (f" [{scal['finding']['reason']}]" if scal["finding"] else ""),
            }
        )

    # R2: at most N credible hazards, the most serious first (ties: relevance, name).
    cap = (
        cfg["assistant"]["maxHazardsRoutine"]
        if routine == "ROUTINE"
        else cfg["assistant"]["maxHazardsNonRoutine"]
    )
    ranked = sorted(
        calibrated,
        key=lambda c: (
            -c["inherent"]["score"],
            -c["inherent"]["severity"],
            -c["relevance"],
            c["hazard"].name,
        ),
    )
    kept, dropped = (ranked, []) if expand else (ranked[:cap], ranked[cap:])
    if dropped:
        notes.append(
            f"{len(dropped)} lower-ranked hazard(s) left out (cap {cap} for a {routine.lower()} activity; "
            "expand to include): " + ", ".join(d["hazard"].name for d in dropped)
        )

    controls = [
        eng.Control(
            key=f"x{i}",
            hierarchy=(c.get("hierarchy") or "").upper(),
            description=c.get("description") or "",
            effectiveness=c.get("effectiveness"),
            verification_method=c.get("verificationMethod"),
            hazard_keys=tuple(c["hazardKeys"]) if c.get("hazardKeys") else None,
        )
        for i, c in enumerate(draft.get("existingControls") or [])
        if c.get("description")
    ]
    # The draft's proposed controls ride on the entry as candidates, exactly as a
    # stored entry's proposed recommendations do (one path, round 2 E).
    proposed = [
        {
            "hierarchy": (c.get("hierarchy") or "ADMINISTRATIVE").upper(),
            "description": (c.get("description") or "").strip(),
            "costBand": (c.get("costBand") or c.get("estimatedCostBand") or "MEDIUM").upper(),
        }
        for c in draft.get("recommendedControls") or []
        if (c.get("description") or "").strip()
    ]
    entry = rv.EntryInput(
        entry_id=draft.get("entryId") or "draft",
        label=draft.get("label") or "draft",
        activity=draft.get("activity") or "",
        routine=routine,
        frequency=draft.get("frequency"),
        hazards=[c["hazard"] for c in kept],
        entry_l=None,
        entry_s=None,
        saved_residual=None,
        controls=controls,
        hazard_ratings_stored=True,
        review_date_set=bool(draft.get("reviewDateSet")),
        candidate_controls=proposed,
        statutory_current=draft.get("statutoryCurrent"),
        elimination_considered=bool(draft.get("eliminationConsidered")),
    )
    result = rv.evaluate(entry, cfg=cfg, rule=rule, evidence=evidence, plant=plant, extra_candidates=None)
    by_key = {c["hazard"].key: c for c in kept}
    hazards_out = []
    for h in result["hazards"]:
        c = by_key.get(h["key"])
        hazards_out.append(
            {
                **h,
                "manifestationNeeded": c["manifestationNeeded"] if c else False,
                "likelihoodRationale": c["likelihoodRationale"] if c else None,
                "severityRationale": c["severityRationale"] if c else None,
            }
        )
    return {
        "hazards": hazards_out,
        "droppedHazards": [
            {"key": d["hazard"].key, "name": d["hazard"].name, "inherent": d["inherent"]} for d in dropped
        ],
        "entry": result["entry"],
        "recommendations": result["recommendations"],
        "linkedCapas": result["linkedCapas"],
        "existingControlProposals": result["existingControlProposals"],
        "findings": result["findings"],
        "calibrationNotes": notes,
        "review": result,
    }


def from_assistant_suggestion(
    suggestion: dict[str, Any], *, context: dict[str, Any], library: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Turn a HIRA Assistant suggestion (prompt v2 JSON) into a calibration draft.
    The Assistant gives one initial L/S for the activity: each suggested or
    team-added hazard starts from it and is then calibrated per hazard."""
    act = context.get("activity") or {}
    team = context.get("team_already_added") or {}
    calib = suggestion.get("risk_calibration") or {}
    l = (calib.get("initial_likelihood_suggestion") or {}).get("score") or None
    s = (calib.get("initial_severity_suggestion") or {}).get("score") or None
    hazards: list[dict[str, Any]] = []
    seen = set()
    for h in team.get("existingEntryHazards") or []:
        hid = h.get("hazardMasterId")
        if hid and hid not in seen:
            seen.add(hid)
            hazards.append(
                {
                    "key": hid,
                    "name": h.get("name"),
                    "code": h.get("code"),
                    "category": h.get("category"),
                    "consequence": h.get("contextualDescription"),
                    "likelihood": l,
                    "severity": s,
                    "relevance": 1.0,
                }
            )
    for h in suggestion.get("suggested_hazards") or []:
        hid = h.get("hazard_master_id")
        if not hid or hid in seen or h.get("suggested_for_addition") is False:
            continue
        seen.add(hid)
        lib = library.get(hid) or {}
        hazards.append(
            {
                "key": hid,
                "name": lib.get("name") or hid,
                "code": lib.get("code"),
                "category": lib.get("category"),
                "consequence": h.get("reasoning"),
                "likelihood": l,
                "severity": s,
                "relevance": float(h.get("relevance_confidence") or 0.0),
            }
        )
    recs = []
    for block in suggestion.get("control_suggestions") or []:
        for key, hier in (
            ("elimination_controls", "ELIMINATION"),
            ("substitution_controls", "SUBSTITUTION"),
            ("engineering_controls", "ENGINEERING"),
            ("administrative_controls", "ADMINISTRATIVE"),
            ("ppe_controls", "PPE"),
        ):
            for c in block.get(key) or []:
                if isinstance(c, dict) and c.get("description"):
                    recs.append(
                        {
                            "hierarchy": hier,
                            "description": c["description"],
                            "costBand": c.get("estimated_cost_band"),
                            "hazardKeys": [block["hazard_id"]] if block.get("hazard_id") else None,
                        }
                    )
    return {
        "activity": act.get("description"),
        "routine": act.get("routine"),
        "frequency": act.get("frequency"),
        "hazards": hazards,
        "existingControls": [
            {
                "hierarchy": c.get("hierarchy"),
                "description": c.get("description"),
                "effectiveness": c.get("effectiveness"),
            }
            for c in team.get("existingControls") or []
        ],
        "recommendedControls": recs,
    }


__all__ = ["calibrate_draft", "from_assistant_suggestion", "needs_manifestation"]
