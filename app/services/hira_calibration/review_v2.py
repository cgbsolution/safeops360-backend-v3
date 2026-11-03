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
from app.services.hira_calibration.config import CALIBRATED_TYPES, ENGINE_VERSION, MatrixRule, with_matrix

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
    hira_type: str = "TASK_BASED"
    # Proposed (not yet planned) controls on the entry, e.g. a draft's or the
    # team's recommended controls: candidates on both paths (round 2 E).
    candidate_controls: list[dict[str, Any]] = field(default_factory=list)
    # Statutory examination status when it is known as data (round 2 J);
    # None = derived from the plant's statutory register.
    statutory_current: bool | None = None


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


def _verified(c: eng.Control) -> bool:
    return bool((c.verification_method or "").strip()) or c.effectiveness in (
        "EFFECTIVE",
        "PARTIALLY_EFFECTIVE",
    )


ROUTINE_VERIFICATION = "Confirmed in place by the assessor (Copilot review)"


def _routine_suggestions(entry: EntryInput, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Routine controls offered for confirmation when none are captured (round 2 C)."""
    rc = cfg.get("routineControls") or {}
    out, seen = [], set()
    fams = [h.family for h in entry.hazards]
    for fam in [*fams, "_ALL"]:
        for hierarchy, desc in rc.get(fam) or []:
            if desc in seen:
                continue
            seen.add(desc)
            out.append(
                {
                    "hierarchy": hierarchy,
                    "description": desc,
                    "source": "Routine control (confirm it is in place)",
                    "verificationMethod": ROUTINE_VERIFICATION,
                    "routine": True,
                }
            )
    return out


def _entry_state(tols: list[dict[str, Any]]) -> str | None:
    if not tols:
        return None
    if any(
        t["state"] == eng.NOT_ACCEPTABLE and t["route"] != "OVERRIDE" and not t.get("overridden")
        for t in tols
    ):
        return "NOT_ACCEPTABLE"
    if any(t["route"] == "OVERRIDE" for t in tols):
        return "OVERRIDE"
    if any(t["state"] == eng.TOLERABLE_ALARP for t in tols):
        return "TOLERABLE_ALARP"
    return "ACCEPTABLE"


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
    if entry.hira_type not in CALIBRATED_TYPES:
        return structural_review(entry)
    cfg = with_matrix(cfg, rule)
    findings: list[dict[str, Any]] = []
    basis_with_controls = entry.rating_basis == "WITH_CONTROLS"
    rating_credit = not basis_with_controls  # a sheet rating already includes its controls (round 2 H)
    real_controls = eng.has_real_controls(entry.controls, cfg)
    hazard_out: list[dict[str, Any]] = []
    rec_input: list[dict[str, Any]] = []
    unsupported_l, sev_under, sev_over, unrated_same = [], [], [], False
    provisional_any = False
    estimated = not entry.hazard_ratings_stored
    inherent_only = not basis_with_controls and not real_controls
    low_max = cfg["tolerability"].get("lowSeverityMax", 3)

    # ── existing controls first (R6), plus routine controls to confirm (round 2 C) ──
    offered = list(plant.existing_control_proposals)

    def _low_sev_uncontrolled(h: eng.Hazard) -> bool:
        s_ = h.severity or entry.entry_s or 0
        lo = eng.severity_rule(h.family, h.text, cfg)["min"] or 0
        has = any(
            not c.planned and not eng.is_baseline(c.hierarchy, c.description, cfg) and eng.applies_to(c, h)[0]
            for c in entry.controls
        )
        return max(s_, lo) <= low_max and not has

    routine_for = [h for h in entry.hazards if inherent_only or _low_sev_uncontrolled(h)]
    if routine_for:
        offered += _routine_suggestions(
            EntryInput(
                entry.entry_id,
                entry.label,
                entry.activity,
                entry.routine,
                entry.frequency,
                routine_for,
                entry.entry_l,
                entry.entry_s,
                None,
            ),
            cfg,
        )
    routine_keys = {h.key for h in routine_for}
    proposals: list[dict[str, Any]] = []
    for p in offered:
        if any(
            rec.same_control(
                p["hierarchy"],
                p["description"],
                c.hierarchy,
                c.description,
                cfg["recommendations"]["dedupeSimilarity"],
            )
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
            and (not p.get("routine") or h.key in routine_keys)
        ]
        if not relevant:
            continue
        proposals.append({**p, "effectiveness": "NOT_VERIFIED", "hazardKeys": relevant})

    # With no controls recorded, recommendations are worked out as if the
    # proposed / routine controls were accepted, so accepting them raises no new
    # demands, and a recommendation never rests on the inherent rating alone.
    provisional_controls = [
        *entry.controls,
        *(
            eng.Control(
                key=f"proposal-{i}",
                hierarchy=pr["hierarchy"],
                description=pr["description"],
                effectiveness="NOT_VERIFIED",
                verification_method=pr.get("verificationMethod"),
                hazard_keys=tuple(pr["hazardKeys"]),
            )
            for i, pr in enumerate(proposals)
        ),
    ]
    candidates = [
        *(extra_candidates or []),
        *(
            rec.Candidate(
                id=f"proposed-{i}",
                family=next(iter(sorted(eng.control_families(c["description"]))), "GENERAL"),
                hierarchy=(c.get("hierarchy") or "ADMINISTRATIVE").upper(),
                cost=(c.get("costBand") or "MEDIUM").upper(),
                description=c["description"],
                source="proposed",
            )
            for i, c in enumerate(entry.candidate_controls)
            if c.get("description")
        ),
    ]

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
        inherent = eng.rate(L, S, cfg)  # no control credit of any kind (round 2 F)
        credible_s = scal["credibleMin"]
        tier_s = max(S, credible_s or 0)
        collapsed = inherent["level"] == "LOW"

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

        applicable = [c for c in entry.controls if not c.planned and eng.applies_to(c, h)[0]]
        verified_control = any(
            _verified(c)
            and not eng.is_baseline(c.hierarchy, c.description, cfg)
            and c.effectiveness != "INEFFECTIVE"
            for c in applicable
        )
        unverified_keys = [
            c.key
            for c in applicable
            if c.key and not _verified(c) and not eng.is_baseline(c.hierarchy, c.description, cfg)
        ]
        planned_eng = any(
            c.planned
            and (c.hierarchy or "").upper() == "ENGINEERING"
            and c.planned_date
            and eng.applies_to(c, h)[0]
            for c in entry.controls
        )
        eng_in_place = any(
            (c.hierarchy or "").upper() == "ENGINEERING" and c.effectiveness != "INEFFECTIVE"
            for c in applicable
        )
        planned_covers = any(c.planned and c.planned_date and eng.applies_to(c, h)[0] for c in entry.controls)
        statutory = (
            entry.statutory_current
            if entry.statutory_current is not None
            else not plant.statutory_overdue.get(fam)
        )
        alarp = {
            "eliminationConsidered": entry.elimination_considered
            or any((c.hierarchy or "").upper() in ("ELIMINATION", "SUBSTITUTION") for c in entry.controls),
            "engineeringInPlaceOrPlanned": eng_in_place or planned_eng,
            "statutoryCurrent": statutory,
            "reviewDateSet": entry.review_date_set,
        }

        def tol_for(res: dict[str, Any] | None, exhausted: bool = False, verified: bool = verified_control):
            if not res:
                return None
            return eng.tolerability(
                res,
                credible_s=credible_s,
                alarp=alarp,
                strict=rule.moderate_not_acceptable,
                cfg=cfg,
                overridden=entry.override_active,
                verified_control=verified,
                exhausted=exhausted,
                rating_credit=rating_credit,
            )

        tol = tol_for(residual)
        # round 2 C: an S<=3 hazard with no applicable control captured is not
        # given recommendations; the assessor confirms the routine controls.
        uncontrolled_low = tier_s <= low_max and not any(
            not eng.is_baseline(c.hierarchy, c.description, cfg) for c in applicable
        )
        if tol and uncontrolled_low and not eng.resolved(tol) and rating_credit:
            # the residual is not assessed (nothing captured), so it does not drive the entry residual
            residual, credits = None, []
            tol = {
                "state": eng.NOT_ACCEPTABLE,
                "route": "CONFIRM_CONTROLS",
                "missing": ["routineControls"],
                "overridden": False,
                "note": "No control is captured for this low-severity hazard: confirm the routine controls.",
            }
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
            "severityTier": "S<=3" if tier_s <= low_max else "S>=4",
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
            "verifiedControl": verified_control,
            "unverifiedControlKeys": unverified_keys,
        }
        hazard_out.append(item)
        rec_item = {
            **item,
            "hazard": h,
            "controls": entry.controls,
            "basis": (L, S),
            "basisIncludesControls": basis_with_controls,
            "credibleS": credible_s,
            "ratingCredit": rating_credit,
            "_tol_for": tol_for,
            "noRecommendations": uncontrolled_low,
        }
        if inherent_only:
            pr = eng.residual_from_controls(L, S, provisional_controls, cfg, hazard=h)
            prov = eng.rate(pr["likelihood"], pr["severity"], cfg)
            rec_item.update(
                residual=prov,
                controls=provisional_controls,
                tolerability=tol_for(prov, verified=True),
                # S<=3 hazards with nothing captured: confirm routine controls, no recommendations
                noRecommendations=tier_s <= low_max,
            )
        rec_input.append(rec_item)
        if (
            residual
            and real_controls
            and not basis_with_controls
            and (residual["likelihood"], residual["severity"]) == (L, S)
            and any(c.effectiveness is None for c in entry.controls if not c.planned)
        ):
            unrated_same = True

    rating = eng.entry_rating([h for h in hazard_out if h.get("inherent")])
    entry_level = (rating["residual"] or rating["inherent"] or {}).get("level")
    max_s = max((h["inherent"]["severity"] for h in hazard_out if h.get("inherent")), default=None)

    # ── recommendations (R8, round 2 D/E/H) ──
    recs = rec.recommend(
        rec_input,
        cfg=cfg,
        strict=rule.moderate_not_acceptable,
        entry_level=entry_level,
        open_capas=plant.open_capas,
        sibling_recommended=plant.sibling_recommended,
        extra_candidates=candidates,
        entry_max_severity=max_s,
    )
    due_base = entry.today
    for r in recs["recommendations"]:
        r["dueDate"] = (due_base + timedelta(days=r["dueDays"])).isoformat()

    # ── phase 2: no practicable control left -> ALARP checklist / override (round 2 A) ──
    exhausted = {
        u["key"] for u in recs.get("uncovered") or [] if u["reason"] == "no practicable control found"
    }
    by_key = {h["key"]: h for h in hazard_out}
    for ri in rec_input:
        if ri["key"] in exhausted:
            ri["tolerability"] = ri["_tol_for"](ri["residual"], exhausted=True)
            if by_key[ri["key"]].get("residual"):
                by_key[ri["key"]]["tolerability"] = ri["_tol_for"](
                    by_key[ri["key"]]["residual"], exhausted=True
                )
            by_key[ri["key"]]["controlsExhausted"] = True
    for ri in rec_input:
        ri.pop("_tol_for", None)

    # ── findings (R9) ──
    targeted = {
        t["key"]
        for r in recs["recommendations"]
        if r["kind"] != "IMPROVEMENT_NO_RATING_CREDIT"
        for t in r["targetHazards"]
    }
    uncovered = {u["key"]: u for u in recs.get("uncovered") or []}
    if inherent_only and any(h.get("inherent") for h in hazard_out):
        serious = [
            ri["key"]
            for ri in rec_input
            if not ri["collapsed"] and max(ri["inherent"]["severity"], ri.get("credibleS") or 0) > low_max
        ]
        routine_only = not serious
        findings.append(
            _finding(
                "NO_CONTROLS" if serious else "CONFIRM_ROUTINE_CONTROLS",
                BLOCKER if serious else ADVISORY,
                "Inherent only. Existing controls not captured" if serious else "Confirm routine controls",
                "No controls are recorded, so the residual cannot be assessed (it is not the same as the inherent risk)."
                if serious
                else "No controls are recorded. These are low-severity hazards: confirm the routine controls "
                "in place instead of adding new ones.",
                (
                    f"Use these {len(proposals)} existing / routine controls (one click), then rate their effectiveness."
                    if proposals and not routine_only
                    else f"Confirm these {len(proposals)} routine controls (one click)."
                    if proposals
                    else "Record the controls already in place (SOP / work instruction, inspections, guards)."
                ),
                [h["key"] for h in hazard_out if h.get("inherent")],
                action=("USE_EXISTING_CONTROLS" if serious else "CONFIRM_ROUTINE_CONTROLS")
                if proposals
                else None,
            )
        )
    confirm = [
        h for h in hazard_out if h.get("tolerability") and h["tolerability"]["route"] == "CONFIRM_CONTROLS"
    ]
    if confirm:
        findings.append(
            _finding(
                "CONFIRM_ROUTINE_CONTROLS",
                ADVISORY,
                "Confirm routine controls",
                "; ".join(f"{h['name']}: {eng.rating_text(h['residual'])}" for h in confirm[:4])
                + ". Low-severity hazards are acceptable once the routine controls are confirmed and checked.",
                "Confirm the controls in place and how they are checked (one click).",
                [h["key"] for h in confirm],
                action="CONFIRM_ROUTINE_CONTROLS",
            )
        )
    open_all = [
        h
        for h in hazard_out
        if h.get("tolerability")
        and h["tolerability"]["route"] == "CONTROLS"
        and not h["plannedCovers"]
        and not h["tolerability"]["overridden"]
    ]
    open_controls = [h for h in open_all if h["key"] in targeted]
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
    eng_missing_all = [
        h
        for h in hazard_out
        if h.get("tolerability")
        and h["tolerability"]["route"] == "ALARP_CHECKLIST"
        and "engineeringInPlaceOrPlanned" in h["tolerability"]["missing"]
        and not h["plannedCovers"]
    ]
    eng_missing = [h for h in eng_missing_all if h["key"] in targeted]
    no_action = [h for h in [*open_all, *eng_missing_all] if h["key"] not in targeted]
    if no_action:
        reasons = sorted(
            {(uncovered.get(h["key"]) or {}).get("reason", "no practicable control found") for h in no_action}
        )
        findings.append(
            _finding(
                "NO_FURTHER_CONTROL",
                ADVISORY,
                f"{len(no_action)} hazard(s) left for the override or a split",
                "; ".join(f"{h['name']}: {eng.rating_text(h['residual'])}" for h in no_action[:4])
                + f" ({', '.join(reasons)}).",
                "Use the elevated override at approval with a dated review, or split the activity so each part can be "
                "controlled.",
                [h["key"] for h in no_action],
                action="OVERRIDE",
            )
        )
    if eng_missing:
        findings.append(
            _finding(
                "ALARP_ENGINEERING",
                BLOCKER,
                "No engineering control for a serious hazard",
                "; ".join(
                    f"{h['name']} ({eng.rating_text(h['residual'])}, credible "
                    f"S{h['severity']['credibleMin'] or h['residual']['severity']})"
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
                "Apply suggested defaults: it sets the review date and drafts the ALARP record for you to edit."
                if record_gaps != ["statutoryCurrent"]
                else "Bring the statutory examination up to date and record it; the checklist then completes.",
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
                "No practicable control is left or the plant rule does not accept this residual: "
                + ", ".join(h["name"] for h in override[:3])
                + ".",
                "At approval, the Plant Head / Corporate HSE records the override; a justification is drafted below.",
                [h["key"] for h in override],
                action="OVERRIDE",
            )
        )
    no_credit = [r for r in recs["recommendations"] if r["kind"] == "IMPROVEMENT_NO_RATING_CREDIT"]
    if no_credit:
        findings.append(
            _finding(
                "NO_RATING_CREDIT",
                INFO,
                "Improvements that do not change the plant rating",
                "The plant sheet rating already includes the existing controls; a new control changes it only when the "
                "team re-scores the sheet (SP / LH / DE). "
                + "; ".join(r["description"] for r in no_credit[:2]),
                None,
                [t["key"] for r in no_credit for t in r["targetHazards"]],
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
            justify.append(f"{h['name']}: residual not assessed (no controls recorded).")
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
    # A serious hazard (S>=4, as rated or credible) that is not yet acceptable
    # will need the ALARP record once its controls are in place: draft it now.
    needs_alarp_record = [
        ri["name"]
        for ri in rec_input
        if ri.get("tolerability")
        and ri.get("residual")
        and not eng.resolved(ri["tolerability"])
        and max(ri["residual"]["severity"], ri["severity"].get("credibleMin") or 0) > low_max
    ]
    confirm_keys = sorted({k for h in confirm for k in h.get("unverifiedControlKeys") or []})
    defaults = {
        "controlEffectiveness": [
            {"key": c.key, "effectiveness": "NOT_VERIFIED"}
            for c in entry.controls
            if not c.planned and c.effectiveness is None and c.key
        ],
        "confirmControls": [{"key": k, "verificationMethod": ROUTINE_VERIFICATION} for k in confirm_keys],
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
        "hiraType": entry.hira_type,
        "calibrated": True,
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
                        if k in ("window", "12m", "failures", "items")
                    },
                }
                for h in entry.hazards
            }
            if evidence.by_family
            else {},
        },
        "entry": {
            **rating,
            # informational only: no blocker, recommendation or CAPA rests on it (round 2 G)
            "inherentLabel": "Inherent (before controls)",
            "inherentOnly": inherent_only,
            "status": status,
            "state": _entry_state(tolerabilities),
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
        "uncoveredHazards": recs.get("uncovered") or [],
        "skippedCandidates": recs["skipped"],
        "existingControlProposals": proposals,
        "suggestedDefaults": defaults,
        "alarpDraft": alarp_draft(
            entry, [h["name"] for h in override] or needs_alarp_record, rule.moderate_not_acceptable
        )
        if (override or needs_alarp_record)
        else None,
    }


NOT_CALIBRATED_BANNER = "Calibrated rules are not applied to this study type."


def structural_review(entry: EntryInput) -> dict[str, Any]:
    """A non task-based study (HAZOP / FMEA / MOC ...): completeness checks only.
    No rating is changed, no recommendation is capped or generated."""
    findings = []
    if not entry.hazards:
        findings.append(
            _finding(
                "STRUCT_NO_HAZARDS",
                ADVISORY,
                "No hazards recorded",
                "The study has no hazards / deviations listed.",
                "List them before review.",
            )
        )
    unrated = [h.name for h in entry.hazards if not (h.likelihood and h.severity)] if entry.hazards else []
    if entry.hazards and unrated and not (entry.entry_l and entry.entry_s):
        findings.append(
            _finding(
                "STRUCT_NOT_RATED",
                ADVISORY,
                "Ratings missing",
                ", ".join(unrated[:4]),
                "Rate them with the study's own method.",
            )
        )
    missing_consequence = [h.name for h in entry.hazards if not (h.consequence or "").strip()]
    if missing_consequence:
        findings.append(
            _finding(
                "STRUCT_NO_CONSEQUENCE",
                ADVISORY,
                "Consequence missing",
                ", ".join(missing_consequence[:4]),
                "Describe who is harmed, how and how badly.",
            )
        )
    if not [c for c in entry.controls if not c.planned]:
        findings.append(_finding("STRUCT_NO_CONTROLS", INFO, "No safeguards / controls recorded", "", None))
    return {
        "engineVersion": ENGINE_VERSION,
        "entryId": entry.entry_id,
        "label": entry.label,
        "activity": entry.activity,
        "hiraType": entry.hira_type,
        "calibrated": False,
        "scope": {"calibrated": False, "hiraType": entry.hira_type, "banner": NOT_CALIBRATED_BANNER},
        "ratingBasis": entry.rating_basis,
        "entry": {
            "inherent": None,
            "residual": None,
            "drivers": [],
            "inherentOnly": False,
            "status": "NOT_CALIBRATED",
            "residualLabel": None,
        },
        "hazards": [],
        "controlsJustify": [],
        "findings": findings,
        "shownFindings": findings[:3],
        "counts": {k: sum(1 for f in findings if f["class"] == k) for k in (BLOCKER, ADVISORY, INFO)},
        "canSubmit": True,
        "recommendations": [],
        "recommendationCap": 0,
        "linkedCapas": [],
        "uncoveredHazards": [],
        "skippedCandidates": [],
        "existingControlProposals": [],
        "suggestedDefaults": {
            "controlEffectiveness": [],
            "recommendedDefaults": [],
            "nextReviewDue": None,
            "alarpRecord": None,
            "hazardRatings": [],
            "regulations": [],
        },
        "alarpDraft": None,
    }


def blocker_codes(result: dict[str, Any]) -> set[str]:
    return {f["code"] for f in result["findings"] if f["class"] == BLOCKER}


def apply_suggestions(
    entry: EntryInput, result: dict[str, Any], *, implement: bool | str = False
) -> EntryInput:
    """The entry after the assessor accepts everything the review proposes: the
    existing / routine controls, the routine-control confirmations, the
    recommendations and the suggested defaults. implement=False: recommendations
    planned with owner and date; True: implemented and verified; "UNVERIFIED":
    implemented but not yet verified (provisional credit). Used by the
    convergence tests and the eval harness."""
    import copy  # noqa: PLC0415

    e = copy.deepcopy(entry)
    d = result["suggestedDefaults"]
    eff = {x["key"]: x["effectiveness"] for x in d.get("controlEffectiveness") or []}
    confirm = {x["key"]: x["verificationMethod"] for x in d.get("confirmControls") or []}
    for c in e.controls:
        if c.key in eff:
            c.effectiveness = eff[c.key]
        if c.key in confirm and not (c.verification_method or "").strip():
            c.verification_method = confirm[c.key]
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
    used = set()
    for r in result["recommendations"]:
        keys = tuple(t["key"] for t in r["targetHazards"])
        used.add(r["description"].strip().lower())
        if implement is True:
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
        elif implement == "UNVERIFIED":
            e.controls.append(
                eng.Control(
                    key=f"rec-{r['id']}",
                    hierarchy=r["hierarchy"],
                    description=r["description"],
                    effectiveness="NOT_VERIFIED",
                    hazard_keys=keys,
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
    e.candidate_controls = [
        c for c in e.candidate_controls if (c.get("description") or "").strip().lower() not in used
    ]
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
    return e


__all__ = [
    "NOT_CALIBRATED_BANNER",
    "structural_review",
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
