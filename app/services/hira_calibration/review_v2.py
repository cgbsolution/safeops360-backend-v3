"""Copilot review of one HIRA entry, calibration v2 (R2-R9). Pure: evaluate().

Findings come in three classes and only BLOCKERs stop submission:

  BLOCKER   no existing controls captured; a hazard NOT_ACCEPTABLE with no
            planned control; an S>=4 MODERATE hazard with no engineering
            control in place or planned (ALARP item b).
  ADVISORY  likelihood above the evidence, severity under/overstated, controls
            unrated or unverified, saved residual differs from the engine,
            override needed (with a drafted justification), review overdue.
  INFO      collapsed low hazards, estimated per-hazard ratings, suggested
            regulation references, baseline PPE recorded.

The result is deterministic for the same input. Applying what it proposes
(existing controls, recommendations as planned controls, suggested defaults)
never raises a new BLOCKER on the next pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.services.hira_calibration import recommend as rec
from app.services.hira_calibration import risk_engine as eng
from app.services.hira_calibration.config import ENGINE_VERSION, MatrixRule

BLOCKER, ADVISORY, INFO = "BLOCKER", "ADVISORY", "INFO"

# Suggested regulation references (R10). The live map is the HiraRegulationMap
# table; this is the fallback with the same rows. Marked "suggested, verify".
DEFAULT_REGULATION_MAP: list[dict[str, str]] = [
    {
        "family": "LIFTING",
        "regulation": "Factories Act, 1948",
        "section": "Sec. 29 (lifting machines, chains, ropes and lifting tackles)",
    },
    {
        "family": "HEIGHT",
        "regulation": "Factories Act, 1948",
        "section": "Sec. 32 (floors, stairs and means of access); Sec. 33 (pits, sumps, openings in floors)",
    },
    {"family": "MACHINERY", "regulation": "Factories Act, 1948", "section": "Sec. 21 (fencing of machinery)"},
    {"family": "PRESSURE", "regulation": "Factories Act, 1948", "section": "Sec. 31 (pressure plant)"},
    {
        "family": "FIRE_EXPLOSION",
        "regulation": "Factories Act, 1948",
        "section": "Sec. 38 (precautions in case of fire)",
    },
    {"family": "DUST", "regulation": "Factories Act, 1948", "section": "Sec. 14 (dust and fume)"},
    {
        "family": "MANUAL_HANDLING",
        "regulation": "Factories Act, 1948",
        "section": "Sec. 34 (excessive weights)",
    },
    {
        "family": "CONFINED_SPACE",
        "regulation": "Factories Act, 1948",
        "section": "Sec. 36 (precautions against dangerous fumes, gases, etc.)",
    },
]


@dataclass
class EntryInput:
    entry_id: str
    label: str
    activity: str
    routine: str | None
    frequency: str | None
    hazards: list[eng.Hazard]
    entry_l: int | None
    entry_s: int | None
    saved_residual: tuple[int, int] | None
    rating_basis: str = "INHERENT"  # INHERENT | WITH_CONTROLS (single-rating sheet)
    controls: list[eng.Control] = field(default_factory=list)  # existing + planned recommended
    recommended: list[dict[str, Any]] = field(default_factory=list)
    hazard_ratings_stored: bool = False
    elimination_considered: bool = False
    review_date_set: bool = False
    review_overdue: bool = False
    override_active: bool = False
    hazard_regulations: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    today: date = field(default_factory=date.today)


@dataclass
class PlantData:
    existing_control_proposals: list[dict[str, Any]] = field(default_factory=list)
    open_capas: list[dict[str, Any]] = field(default_factory=list)
    sibling_recommended: list[dict[str, Any]] = field(default_factory=list)
    statutory_overdue: dict[str, list[str]] = field(default_factory=dict)  # family -> lines
    regulation_map: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_REGULATION_MAP))
    extra_findings: list[dict[str, Any]] = field(default_factory=list)  # e.g. source-sheet checks


def _finding(
    code: str,
    cls: str,
    title: str,
    detail: str,
    fix: str | None = None,
    hazards: list[str] | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "class": cls,
        "title": title,
        "detail": detail,
        "fix": fix,
        "hazardKeys": hazards or [],
        "action": action,
    }


def _reg_for(h: eng.Hazard, rmap: list[dict[str, Any]]) -> dict[str, Any] | None:
    fam = h.family
    for r in rmap:
        if (
            (r.get("hazardCode") and r["hazardCode"] == h.code)
            or (r.get("family") and r["family"] == fam)
            or (r.get("hazardCategory") and r["hazardCategory"] == h.category)
        ):
            return {"regulation": r["regulation"], "section": r.get("section"), "note": "suggested, verify"}
    return None


def alarp_draft(entry: EntryInput, hazard_names: list[str], strict: bool) -> str:
    names = ", ".join(hazard_names[:3])
    base = (
        f"Activity: {entry.activity[:160]}. Hazard(s): {names}. Elimination and substitution were considered "
        "and are not reasonably practicable because the task itself requires the hazard to be present "
        "(edit to state why). Engineering control(s) are in place or planned with a date; statutory "
        "examinations are current; the residual is MODERATE and will be reviewed within 12 months."
    )
    if strict:
        base += (
            " The plant rule treats MODERATE as not acceptable, so this residual is put to the Plant Head / "
            "Corporate HSE for an elevated override with the review date above."
        )
    return base


def evaluate(
    entry: EntryInput,
    *,
    cfg: dict[str, Any],
    rule: MatrixRule,
    evidence: eng.Evidence,
    plant: PlantData | None = None,
    extra_candidates: list[rec.Candidate] | None = None,
) -> dict[str, Any]:
    plant = plant or PlantData()
    findings: list[dict[str, Any]] = []
    basis_with_controls = entry.rating_basis == "WITH_CONTROLS"
    real_controls = eng.has_real_controls(entry.controls, cfg)
    hazard_out: list[dict[str, Any]] = []
    rec_input: list[dict[str, Any]] = []
    unsupported_l, sev_under, sev_over, unrated_same = [], [], [], False
    provisional_any = False
    estimated = not entry.hazard_ratings_stored

    for h in entry.hazards:
        fam = h.family
        # per-hazard rating: stored, or estimated from the single entry rating
        l_team = h.likelihood if h.likelihood else entry.entry_l
        s_team = h.severity if h.severity else entry.entry_s
        if not l_team or not s_team:
            hazard_out.append(
                {
                    "key": h.key,
                    "name": h.name,
                    "family": fam,
                    "inherent": None,
                    "residual": None,
                    "tolerability": None,
                    "collapsed": False,
                    "note": "not rated",
                }
            )
            continue
        lcal = eng.calibrate_likelihood(
            l_team,
            routine=entry.routine,
            frequency=entry.frequency,
            evidence=evidence.for_family(fam),
            scale=rule.likelihood_scale,
            cfg=cfg,
        )
        # The team's severity is never raised by the review (an ADVISORY says so).
        # Only when one entry rating is spread over several hazards is it lowered
        # to each hazard's credible maximum (a cut is not S5 because a crane drop is).
        scal = eng.calibrate_severity(s_team, family=fam, text=h.text, cfg=cfg, adjust=False)
        spread = estimated and len(entry.hazards) > 1 and not h.severity
        if spread and scal["finding"] and scal["finding"]["kind"] == "OVERSTATED":
            scal = {**scal, "value": scal["credibleMax"], "spreadClamped": True}
        if lcal["capped"]:
            unsupported_l.append((h, l_team, lcal))
        if scal["finding"]:
            (sev_under if scal["finding"]["kind"] == "UNDERSTATED" else sev_over).append((h, s_team, scal))
        L, S = lcal["value"], scal["value"]
        inherent = eng.rate(L, S, cfg)
        credible_s = scal["credibleMin"]
        collapsed = inherent["score"] <= cfg["thresholds"]["lowMax"]

        residual = None
        credits: list[dict[str, Any]] = []
        provisional = False
        if basis_with_controls:
            residual = inherent
            credits = [
                {
                    "control": c.description,
                    "hierarchy": c.hierarchy,
                    "steps": 0,
                    "status": "IN_RATING",
                    "reason": "already included in the sheet rating",
                }
                for c in entry.controls
                if not c.planned
            ]
        elif real_controls:
            r = eng.residual_from_controls(L, S, entry.controls, cfg, hazard=h)
            residual = eng.rate(r["likelihood"], r["severity"], cfg)
            credits, provisional = r["credits"], r["provisional"]
            provisional_any = provisional_any or provisional

        planned_eng = any(
            c.planned
            and (c.hierarchy or "").upper() == "ENGINEERING"
            and c.planned_date
            and eng.applies_to(c, h)[0]
            for c in entry.controls
        )
        eng_in_place = any(
            not c.planned
            and (c.hierarchy or "").upper() == "ENGINEERING"
            and c.effectiveness != "INEFFECTIVE"
            and eng.applies_to(c, h)[0]
            for c in entry.controls
        )
        planned_covers = any(c.planned and c.planned_date and eng.applies_to(c, h)[0] for c in entry.controls)
        alarp = {
            "eliminationConsidered": entry.elimination_considered
            or any((c.hierarchy or "").upper() in ("ELIMINATION", "SUBSTITUTION") for c in entry.controls),
            "engineeringInPlaceOrPlanned": eng_in_place or planned_eng,
            "statutoryCurrent": not plant.statutory_overdue.get(fam),
            "reviewDateSet": entry.review_date_set,
        }
        tol = None
        if residual:
            tol = eng.tolerability(
                residual,
                credible_s=credible_s,
                alarp=alarp,
                strict=rule.moderate_not_acceptable,
                cfg=cfg,
                overridden=entry.override_active,
            )
        reg = None
        have_reg = entry.hazard_regulations.get(h.key) or (None, None)
        if not have_reg[0]:
            reg = _reg_for(h, plant.regulation_map)
        item = {
            "key": h.key,
            "name": h.name,
            "code": h.code,
            "family": fam,
            "consequence": h.consequence,
            "ratingSource": "STORED" if h.likelihood and h.severity else "ESTIMATED_FROM_ENTRY",
            "likelihood": lcal,
            "severity": scal,
            "inherent": inherent,
            "residual": residual,
            "residualAssessed": residual is not None,
            "credits": credits,
            "provisional": provisional,
            "tolerability": tol,
            "collapsed": collapsed,
            "alarp": alarp,
            "plannedCovers": planned_covers,
            "regulationSuggestion": reg,
        }
        hazard_out.append(item)
        rec_input.append(
            {
                **item,
                "hazard": h,
                "controls": entry.controls,
                "basis": (L, S),
                "basisIncludesControls": basis_with_controls,
                "credibleS": credible_s,
            }
        )
        if (
            residual
            and real_controls
            and not basis_with_controls
            and (residual["likelihood"], residual["severity"]) == (L, S)
            and any(c.effectiveness is None for c in entry.controls if not c.planned)
        ):
            unrated_same = True

    rating = eng.entry_rating([h for h in hazard_out if h.get("inherent")])
    inherent_only = not basis_with_controls and not real_controls
    entry_level = (rating["residual"] or rating["inherent"] or {}).get("level")

    # ── recommendations (R8) ──
    recs = rec.recommend(
        rec_input,
        cfg=cfg,
        strict=rule.moderate_not_acceptable,
        entry_level=entry_level,
        open_capas=plant.open_capas,
        sibling_recommended=plant.sibling_recommended,
        extra_candidates=extra_candidates,
    )
    due_base = entry.today
    for r in recs["recommendations"]:
        r["dueDate"] = (due_base + timedelta(days=r["dueDays"])).isoformat()

    # ── existing controls first (R6) ──
    proposals = []
    for p in plant.existing_control_proposals:
        if any(
            rec.similarity(p["description"], c.description) >= cfg["recommendations"]["dedupeSimilarity"]
            for c in entry.controls
            if not c.planned
        ):
            continue
        if any(rec.similarity(p["description"], q["description"]) >= 0.8 for q in proposals):
            continue
        relevant = [
            h.key
            for h in entry.hazards
            if eng.applies_to(eng.Control(None, p["hierarchy"], p["description"]), h)[0]
        ]
        if not relevant:
            continue
        proposals.append({**p, "effectiveness": "NOT_VERIFIED", "hazardKeys": relevant})

    # ── findings (R9) ──
    if inherent_only and rating["inherent"] and rating["inherent"]["level"] != "LOW":
        findings.append(
            _finding(
                "NO_CONTROLS",
                BLOCKER,
                "Inherent only. Existing controls not captured",
                "No controls are recorded, so the residual cannot be assessed (it is not the same as the inherent risk).",
                (
                    f"Use these {len(proposals)} existing controls from plant records, then rate their effectiveness."
                    if proposals
                    else "Record the controls already in place (SOP / work instruction, inspections, guards)."
                ),
                action="USE_EXISTING_CONTROLS" if proposals else None,
            )
        )
    open_controls = [
        h
        for h in hazard_out
        if h.get("tolerability")
        and h["tolerability"]["route"] == "CONTROLS"
        and not h["plannedCovers"]
        and not h["tolerability"]["overridden"]
    ]
    if open_controls:
        findings.append(
            _finding(
                "HAZARD_NOT_ACCEPTABLE",
                BLOCKER,
                f"{len(open_controls)} hazard(s) not acceptable with no planned control",
                "; ".join(f"{h['name']}: {eng.rating_text(h['residual'])}" for h in open_controls[:4]),
                "Add the recommended control for each (owner and date prefilled), or use the elevated override.",
                [h["key"] for h in open_controls],
                action="ADD_RECOMMENDATIONS",
            )
        )
    eng_missing = [
        h
        for h in hazard_out
        if h.get("tolerability")
        and h["tolerability"]["route"] == "ALARP_CHECKLIST"
        and "engineeringInPlaceOrPlanned" in h["tolerability"]["missing"]
        and not h["plannedCovers"]
    ]
    if eng_missing:
        findings.append(
            _finding(
                "ALARP_ENGINEERING",
                BLOCKER,
                "No engineering control for a serious hazard",
                "; ".join(
                    f"{h['name']} ({eng.rating_text(h['residual'])}, credible S{h['severity']['credibleMin'] or h['residual']['severity']})"
                    for h in eng_missing[:3]
                )
                + ". ALARP needs at least one engineering control in place or planned with a date.",
                "Plan the suggested engineering control with a date; no other control is needed for ALARP.",
                [h["key"] for h in eng_missing],
                action="ADD_RECOMMENDATIONS",
            )
        )
    record_gaps = sorted(
        {
            m
            for h in hazard_out
            if h.get("tolerability") and h["tolerability"]["route"] == "ALARP_CHECKLIST"
            for m in h["tolerability"]["missing"]
            if m != "engineeringInPlaceOrPlanned"
        }
    )
    if record_gaps:
        findings.append(
            _finding(
                "ALARP_RECORD",
                ADVISORY,
                "ALARP checklist needs a record",
                "; ".join(eng.ALARP_ITEMS[m] for m in record_gaps) + ".",
                "Apply suggested defaults: it sets the review date and drafts the ALARP record for you to edit.",
                action="APPLY_DEFAULTS",
            )
        )
    override = [
        h
        for h in hazard_out
        if h.get("tolerability")
        and h["tolerability"]["route"] == "OVERRIDE"
        and not h["tolerability"]["overridden"]
    ]
    if override:
        findings.append(
            _finding(
                "OVERRIDE_ROUTE",
                ADVISORY,
                "Route to the elevated override, not more controls",
                "The ALARP checklist is complete, but the plant rule does not accept MODERATE: "
                + ", ".join(h["name"] for h in override[:3])
                + ".",
                "At approval, the Plant Head / Corporate HSE records the override; a justification is drafted below.",
                [h["key"] for h in override],
                action="OVERRIDE",
            )
        )
    if unsupported_l:
        findings.append(
            _finding(
                "LIKELIHOOD_UNSUPPORTED",
                ADVISORY,
                "Likelihood is above what the records support",
                "; ".join(f"{h.name}: L{lt} -> L{lc['value']}" for h, lt, lc in unsupported_l[:3])
                + f". {unsupported_l[0][2]['reason']}",
                "Use the calibrated likelihood, or attach the incident / near miss / control failure that supports it.",
                [h.key for h, _, _ in unsupported_l],
            )
        )
    if sev_under:
        findings.append(
            _finding(
                "SEVERITY_UNDERSTATED",
                ADVISORY,
                "Severity looks understated",
                "; ".join(f"{h.name}: S{st} - {sc['finding']['reason']}" for h, st, sc in sev_under[:3]),
                "Re-assess the severity at the credible worst case, or record why the team keeps it.",
                [h.key for h, _, _ in sev_under],
            )
        )
    if sev_over and entry.hazard_ratings_stored:
        findings.append(
            _finding(
                "SEVERITY_OVERSTATED",
                ADVISORY,
                "Severity looks overstated",
                "; ".join(f"{h.name}: S{st} - {sc['finding']['reason']}" for h, st, sc in sev_over[:3]),
                "Rate the credible worst case, not the theoretical maximum.",
                [h.key for h, _, _ in sev_over],
            )
        )
    if unrated_same:
        findings.append(
            _finding(
                "RATE_EFFECTIVENESS",
                ADVISORY,
                "Rate the effectiveness of the listed controls",
                "The residual equals the inherent rating while controls are listed without an effectiveness.",
                "Rate each control (verified on site or not). Do not raise the inherent rating to make room.",
                action="APPLY_DEFAULTS",
            )
        )
    elif provisional_any:
        findings.append(
            _finding(
                "CONTROLS_PROVISIONAL",
                ADVISORY,
                "Some control credit is provisional",
                "Unverified controls, or procedures without a recorded verification method, are credited provisionally.",
                "Verify on site and record how each is checked (e.g. 'pre-use checklist signed each shift').",
            )
        )
    if entry.saved_residual and not basis_with_controls and real_controls and rating["residual"]:
        sl, ss = entry.saved_residual
        saved = eng.rate(sl, ss, cfg)
        if (saved["score"], saved["severity"]) < (
            rating["residual"]["score"],
            rating["residual"]["severity"],
        ):
            findings.append(
                _finding(
                    "SAVED_RESIDUAL_LOWER",
                    ADVISORY,
                    "Saved residual is lower than the controls support",
                    f"Saved {eng.rating_text(saved)}; the controls support {eng.rating_text(rating['residual'])} "
                    "(procedures and PPE never lower severity).",
                    "Use the calculated residual, or add the control that justifies the lower rating.",
                )
            )
    if entry.review_overdue:
        findings.append(
            _finding(
                "REVIEW_OVERDUE",
                ADVISORY,
                "Review is overdue",
                "The next review date has passed.",
                "Schedule the periodic review.",
            )
        )
    findings.extend(plant.extra_findings)
    collapsed = [h for h in hazard_out if h.get("collapsed")]
    if collapsed:
        findings.append(
            _finding(
                "LOW_HAZARDS",
                INFO,
                f"{len(collapsed)} low hazard(s) listed only",
                ", ".join(h["name"] for h in collapsed),
                None,
                [h["key"] for h in collapsed],
            )
        )
    if estimated and len(entry.hazards) > 1:
        findings.append(
            _finding(
                "ESTIMATED_PER_HAZARD",
                INFO,
                "Per-hazard ratings estimated from the single entry rating",
                "The entry has one rating for all hazards; each hazard is shown within its credible range.",
                "Rate each hazard on its own (suggested defaults fill the estimates).",
            )
        )
    regs = [h for h in hazard_out if h.get("regulationSuggestion")]
    if regs:
        findings.append(
            _finding(
                "REGULATION_SUGGESTED",
                INFO,
                "Suggested legal references (verify)",
                "; ".join(
                    f"{h['name']}: {h['regulationSuggestion']['regulation']} "
                    f"{h['regulationSuggestion']['section'] or ''}".strip()
                    for h in regs[:4]
                ),
                "Check that each applies before keeping it.",
                [h["key"] for h in regs],
            )
        )
    baseline = [
        c for c in entry.controls if not c.planned and eng.is_baseline(c.hierarchy, c.description, cfg)
    ]
    if baseline:
        findings.append(
            _finding(
                "BASELINE_RECORDED",
                INFO,
                "Baseline site PPE / rules recorded (no credit)",
                "; ".join(c.description for c in baseline[:3]),
                None,
            )
        )

    order = {BLOCKER: 0, ADVISORY: 1, INFO: 2}
    findings.sort(key=lambda f: (order[f["class"]], f["code"]))
    blockers = [f for f in findings if f["class"] == BLOCKER]
    shown = blockers[: cfg["findings"]["maxBlockersShown"]]
    shown += [f for f in findings if f["class"] != BLOCKER][
        : max(0, cfg["findings"]["defaultShown"] - len(shown))
    ]

    # ── "what the listed controls justify" (same engine) ──
    justify = []
    for h in hazard_out:
        if not h.get("inherent"):
            continue
        if basis_with_controls:
            justify.append(
                f"{h['name']}: {eng.rating_text(h['residual'])} as rated (the sheet rating already "
                "includes the listed controls; no further credit)."
            )
        elif not h["residualAssessed"]:
            justify.append(
                f"{h['name']}: inherent {eng.rating_text(h['inherent'])}; residual not assessed "
                "(no controls recorded)."
            )
        else:
            steps = [c for c in h["credits"] if c["steps"]]
            why = (
                "; ".join(
                    f"{c['hierarchy'].title()} {c['axis']}-{c['steps']}"
                    + (" (provisional)" if c["status"] == "PROVISIONAL" else "")
                    for c in steps
                )
                or "no credit"
            )
            justify.append(
                f"{h['name']}: {eng.rating_text(h['inherent'])} -> {eng.rating_text(h['residual'])} ({why})."
            )

    # ── suggested defaults (one click) ──
    review_due = (entry.today + timedelta(days=30 * cfg["tolerability"]["reviewMonths"])).isoformat()
    # Any serious hazard (S>=4, as rated or credible) that is not yet acceptable
    # will need the ALARP record once its controls are in place: draft it now.
    needs_alarp_record = [
        h["name"]
        for h in hazard_out
        if h.get("tolerability")
        and not eng.resolved(h["tolerability"])
        and max(h["residual"]["severity"], h["severity"].get("credibleMin") or 0) >= 4
    ]
    defaults = {
        "controlEffectiveness": [
            {"key": c.key, "effectiveness": "NOT_VERIFIED"}
            for c in entry.controls
            if not c.planned and c.effectiveness is None and c.key
        ],
        "recommendedDefaults": [
            {
                "key": r.get("key"),
                "ownerRole": r.get("ownerRole")
                or cfg["recommendations"]["ownerRoles"].get(
                    (r.get("hierarchy") or "").upper(), "Section Head"
                ),
                "proposedImplementationDate": r.get("proposedImplementationDate")
                or (
                    entry.today
                    + timedelta(days=cfg["recommendations"]["dueDays"].get(entry_level or "MODERATE", 90))
                ).isoformat(),
            }
            for r in entry.recommended
            if r.get("key") and (not r.get("ownerRole") or not r.get("proposedImplementationDate"))
        ],
        "nextReviewDue": None if entry.review_date_set else review_due,
        "alarpRecord": (
            {
                "alarpFurtherControlsConsidered": True,
                "alarpFurtherControlsDescription": alarp_draft(
                    entry, needs_alarp_record, rule.moderate_not_acceptable
                ),
            }
            if needs_alarp_record and not entry.elimination_considered
            else None
        ),
        "hazardRatings": (
            [
                {
                    "key": h["key"],
                    "likelihood": h["inherent"]["likelihood"],
                    "severity": h["inherent"]["severity"],
                    "likelihoodRationale": h["likelihood"]["reason"],
                    "severityRationale": (h["severity"]["finding"] or {}).get("reason")
                    or f"Credible worst case for {h['family'].replace('_', ' ').lower()}.",
                }
                for h in hazard_out
                if h.get("inherent")
            ]
            if estimated
            else []
        ),
        "regulations": [{"key": h["key"], **h["regulationSuggestion"]} for h in regs],
    }

    tolerabilities = [h["tolerability"] for h in hazard_out if h.get("tolerability")]
    if inherent_only:
        status = "INHERENT_ONLY"
    elif tolerabilities and all(eng.resolved(t) for t in tolerabilities):
        status = "RESOLVED"
    elif tolerabilities and all(eng.resolved(t) or t["route"] == "OVERRIDE" for t in tolerabilities):
        status = "OVERRIDE_PENDING"
    else:
        status = "OPEN"
    return {
        "engineVersion": ENGINE_VERSION,
        "entryId": entry.entry_id,
        "label": entry.label,
        "activity": entry.activity,
        "ratingBasis": entry.rating_basis,
        "matrixRule": {
            "code": rule.code,
            "likelihoodScale": rule.likelihood_scale,
            "moderateNotAcceptable": rule.moderate_not_acceptable,
        },
        "evidence": {
            "eventsInWindow": evidence.events_window,
            "eventsIn12Months": evidence.events_12m,
            "controlFailures": evidence.control_failures,
            "windowMonths": evidence.window_months,
            "lines": evidence.items,
            "byHazard": {
                h.key: {
                    "family": h.family,
                    **{
                        k: v
                        for k, v in (evidence.by_family.get(h.family) or {}).items()
                        if k in ("window", "12m", "failures")
                    },
                }
                for h in entry.hazards
            }
            if evidence.by_family
            else {},
        },
        "entry": {
            **rating,
            "inherentOnly": inherent_only,
            "status": status,
            "residualLabel": ("Inherent only. Existing controls not captured" if inherent_only else None),
        },
        "hazards": hazard_out,
        "controlsJustify": justify,
        "findings": findings,
        "shownFindings": shown,
        "counts": {k: sum(1 for f in findings if f["class"] == k) for k in (BLOCKER, ADVISORY, INFO)},
        "canSubmit": not blockers,
        "recommendations": recs["recommendations"],
        "recommendationCap": recs["cap"],
        "linkedCapas": recs["linkedCapas"],
        "skippedCandidates": recs["skipped"],
        "existingControlProposals": proposals,
        "suggestedDefaults": defaults,
        "alarpDraft": alarp_draft(
            entry, [h["name"] for h in override] or needs_alarp_record, rule.moderate_not_acceptable
        )
        if (override or needs_alarp_record)
        else None,
    }


def blocker_codes(result: dict[str, Any]) -> set[str]:
    return {f["code"] for f in result["findings"] if f["class"] == BLOCKER}


def apply_suggestions(entry: EntryInput, result: dict[str, Any], *, implement: bool = False) -> EntryInput:
    """The entry after the assessor accepts everything the review proposes:
    the existing controls (unverified), the recommendations (planned with owner
    and date, or implemented and verified when implement=True) and the
    suggested defaults. Used by the convergence test and the eval harness."""
    import copy  # noqa: PLC0415

    e = copy.deepcopy(entry)
    d = result["suggestedDefaults"]
    eff = {x["key"]: x["effectiveness"] for x in d["controlEffectiveness"]}
    for c in e.controls:
        if c.key in eff:
            c.effectiveness = eff[c.key]
    for i, p in enumerate(result["existingControlProposals"]):
        e.controls.append(
            eng.Control(
                key=f"accepted-{i}",
                hierarchy=p["hierarchy"],
                description=p["description"],
                effectiveness="NOT_VERIFIED",
                verification_method=p.get("verificationMethod"),
                hazard_keys=tuple(p.get("hazardKeys") or ()) or None,
            )
        )
    for r in result["recommendations"]:
        keys = tuple(t["key"] for t in r["targetHazards"])
        if implement:
            e.controls.append(
                eng.Control(
                    key=f"rec-{r['id']}",
                    hierarchy=r["hierarchy"],
                    description=r["description"],
                    effectiveness="EFFECTIVE",
                    evidence_attached=True,
                    hazard_keys=keys,
                    verification_method="verified at implementation",
                )
            )
        else:
            e.controls.append(
                eng.Control(
                    key=f"rec-{r['id']}",
                    hierarchy=r["hierarchy"],
                    description=r["description"],
                    hazard_keys=keys,
                    planned=True,
                    planned_date=r["dueDate"],
                )
            )
        e.recommended.append(
            {
                "key": f"rec-{r['id']}",
                "hierarchy": r["hierarchy"],
                "description": r["description"],
                "ownerRole": r["ownerRole"],
                "proposedImplementationDate": r["dueDate"],
            }
        )
    if d["nextReviewDue"]:
        e.review_date_set = True
    if d["alarpRecord"]:
        e.elimination_considered = True
    if d["hazardRatings"]:
        by = {x["key"]: x for x in d["hazardRatings"]}
        for h in e.hazards:
            if h.key in by:
                h.likelihood, h.severity = by[h.key]["likelihood"], by[h.key]["severity"]
        e.hazard_ratings_stored = True
    for r in d["regulations"]:
        e.hazard_regulations[r["key"]] = (r["regulation"], r.get("section"))
    if result["entry"]["status"] == "OVERRIDE_PENDING" or any(
        f["code"] == "OVERRIDE_ROUTE" for f in result["findings"]
    ):
        pass  # the override is an approval step by another role; it never blocks submission
    return e


__all__ = [
    "ADVISORY",
    "BLOCKER",
    "DEFAULT_REGULATION_MAP",
    "EntryInput",
    "INFO",
    "PlantData",
    "alarp_draft",
    "apply_suggestions",
    "blocker_codes",
    "evaluate",
]
