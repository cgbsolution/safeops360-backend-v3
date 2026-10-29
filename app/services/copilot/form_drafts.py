"""Observation-form and HIRA-entry drafts the copilot can read and suggest into.

The browser sends the open form's current state with each turn (`formDraft`),
the copilot proposes changes through `update_observation_draft` /
`update_hira_entry_draft`, and the form applies them live. Nothing is saved by
the copilot: the user still presses Submit / Save, and the normal endpoints
re-validate everything.

The HIRA what-if (`assess_controls`) reuses the HIRA router's own residual
model (`_suggest_residual_scores`), matrix cells and ALARP banding, so the
numbers the copilot quotes are the numbers the editor shows.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

FORM_KINDS = ("OBSERVATION", "HIRA_ENTRY")
OBS_TYPES = ("SAFE_ACT", "UNSAFE_ACT", "SAFE_CONDITION", "UNSAFE_CONDITION")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
HIERARCHY = ("ELIMINATION", "SUBSTITUTION", "ENGINEERING", "ADMINISTRATIVE", "PPE")
EFFECTIVENESS = ("EFFECTIVE", "PARTIALLY_EFFECTIVE", "NOT_VERIFIED", "INEFFECTIVE")
COST_BANDS = ("LOW", "MEDIUM", "HIGH", "VERY_HIGH")
LEVEL_ORDER = ("LOW", "MODERATE", "HIGH", "CRITICAL")
MIN_SEVERITY_REASON = 10


def _s(v: Any, n: int = 400) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    return t[:n] if t else None


def _int(v: Any, lo: int = 1, hi: int = 10) -> int | None:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if lo <= i <= hi else None


def _enum(v: Any, allowed: tuple[str, ...]) -> str | None:
    t = (str(v).strip().upper() if v is not None else "")
    return t if t in allowed else None


# ─── Client draft normalisation ─────────────────────────────────────────


def normalise_client_form(raw: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Whitelist and clip the browser's form snapshot. Returns (kind, draft)."""
    if not isinstance(raw, dict):
        return None, None
    kind = _enum(raw.get("kind"), FORM_KINDS)
    d = raw.get("draft")
    if kind is None or not isinstance(d, dict):
        return None, None
    if kind == "OBSERVATION":
        return kind, {
            "plantId": _s(d.get("plantId"), 64), "areaId": _s(d.get("areaId"), 64), "areaName": _s(d.get("areaName"), 120),
            "type": _enum(d.get("type"), OBS_TYPES), "categoryCode": _s(d.get("categoryCode"), 64),
            "subCategoryCode": _s(d.get("subCategoryCode"), 64), "severity": _enum(d.get("severity"), SEVERITIES),
            "severityReason": _s(d.get("severityReason"), 500), "suggestedSeverity": _enum(d.get("suggestedSeverity"), SEVERITIES),
            "description": _s(d.get("description"), 4000), "immediateAction": _s(d.get("immediateAction"), 2000),
            "contractorInvolved": bool(d.get("contractorInvolved")), "workersNamed": _int(d.get("workersNamed"), 0, 50) or 0,
            "areas": [{"areaId": _s(a.get("areaId"), 64), "name": _s(a.get("name"), 120)}
                      for a in (d.get("areas") or [])[:80] if isinstance(a, dict)],
        }

    def ctrl(c: Any) -> dict[str, Any] | None:
        if not isinstance(c, dict):
            return None
        return {"key": _s(c.get("key"), 64), "hierarchy": _enum(c.get("hierarchy"), HIERARCHY),
                "description": _s(c.get("description"), 500), "effectiveness": _enum(c.get("effectiveness"), EFFECTIVENESS),
                "status": _s(c.get("status"), 30)}

    def block(b: Any) -> dict[str, Any] | None:
        if not isinstance(b, dict):
            return None
        return {"likelihood": _int(b.get("likelihood")), "severity": _int(b.get("severity")), "score": _int(b.get("score"), 0, 100),
                "level": _enum(b.get("level"), LEVEL_ORDER), "region": _s(b.get("region"), 30),
                "auto": b.get("auto") if isinstance(b.get("auto"), bool) else None,
                "acceptable": b.get("acceptable") if isinstance(b.get("acceptable"), bool) else None}

    return kind, {
        "entryId": _s(d.get("entryId"), 64), "studyId": _s(d.get("studyId"), 64),
        "activity": _s(d.get("activity"), 1000), "routine": _enum(d.get("routine"), ("ROUTINE", "NON_ROUTINE", "EMERGENCY")),
        "area": _s(d.get("area"), 120), "subLocation": _s(d.get("subLocation"), 200),
        "hazards": [{"name": _s(h.get("name"), 200), "category": _s(h.get("category"), 60), "consequence": _s(h.get("consequence"), 500)}
                    for h in (d.get("hazards") or [])[:20] if isinstance(h, dict)],
        "initial": block(d.get("initial")), "residual": block(d.get("residual")), "target": block(d.get("target")),
        "existingControls": [c for c in (ctrl(x) for x in (d.get("existingControls") or [])[:40]) if c],
        "recommendedControls": [c for c in (ctrl(x) for x in (d.get("recommendedControls") or [])[:40]) if c],
        "acceptableThreshold": _enum(d.get("acceptableThreshold"), LEVEL_ORDER),
    }


# ─── Observation patch ──────────────────────────────────────────────────


def parse_observation_patch(args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    patch: dict[str, Any] = {}
    errs: list[str] = []
    if "type" in args:
        t = _enum(args["type"], OBS_TYPES)
        patch["type"] = t if t else errs.append(f"type must be one of {', '.join(OBS_TYPES)}")
    for k in ("areaId", "categoryCode", "subCategoryCode"):
        if k in args:
            patch[k] = _s(args[k], 64)
    if "severity" in args:
        sv = _enum(args["severity"], SEVERITIES)
        patch["severity"] = sv if sv else errs.append(f"severity must be one of {', '.join(SEVERITIES)}")
    if "severityReason" in args:
        patch["severityReason"] = _s(args["severityReason"], 500)
    if "description" in args:
        dsc = _s(args["description"], 4000)
        if dsc is not None and len(dsc) < 10:
            errs.append("description must be at least 10 characters")
        patch["description"] = dsc
    if "immediateAction" in args:
        patch["immediateAction"] = _s(args["immediateAction"], 2000)
    if ("categoryCode" in patch) != ("subCategoryCode" in patch):
        errs.append("send categoryCode and subCategoryCode together")
    if errs:
        return None, "; ".join(e for e in errs if e)
    if not patch:
        return None, "Nothing to change"
    return patch, None


# ─── HIRA patch ─────────────────────────────────────────────────────────


def parse_hira_patch(args: dict[str, Any], draft: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    patch: dict[str, Any] = {}
    errs: list[str] = []

    def controls(key: str, *, recommended: bool) -> None:
        items = args.get(key)
        if items is None:
            return
        if not isinstance(items, list) or len(items) > 8:
            errs.append(f"{key} must be a list of at most 8 controls")
            return
        out = []
        for i, c in enumerate(items):
            if not isinstance(c, dict):
                errs.append(f"{key}[{i}] must be an object")
                continue
            h = _enum(c.get("hierarchy"), HIERARCHY)
            d = _s(c.get("description"), 500)
            if not h or not d or len(d) < 5:
                errs.append(f"{key}[{i}] needs a hierarchy ({'/'.join(HIERARCHY)}) and a description")
                continue
            item: dict[str, Any] = {"hierarchy": h, "description": d}
            if recommended:
                item["rationale"] = _s(c.get("rationale"), 500)
                if c.get("estimatedCostBand") is not None:
                    item["estimatedCostBand"] = _enum(c.get("estimatedCostBand"), COST_BANDS)
            else:
                eff = c.get("effectiveness")
                item["effectiveness"] = _enum(eff, EFFECTIVENESS) if eff is not None else None
            out.append(item)
        patch[key] = out

    controls("addExistingControls", recommended=False)
    controls("addRecommendedControls", recommended=True)

    if (eff := args.get("setControlEffectiveness")) is not None:
        keys = {c.get("key") for c in draft.get("existingControls") or []}
        out = []
        for i, c in enumerate(eff if isinstance(eff, list) else []):
            k = _s((c or {}).get("key"), 64)
            e = _enum((c or {}).get("effectiveness"), EFFECTIVENESS)
            if not k or k not in keys or not e:
                errs.append(f"setControlEffectiveness[{i}] needs an existing control key from the draft and a valid effectiveness")
                continue
            out.append({"key": k, "effectiveness": e})
        patch["setControlEffectiveness"] = out

    if (t := args.get("target")) is not None:
        if not isinstance(t, dict):
            errs.append("target must be an object")
        else:
            tl, ts = _int(t.get("likelihoodScore")), _int(t.get("severityScore"))
            if tl is None or ts is None:
                errs.append("target needs likelihoodScore and severityScore on the matrix scale")
            else:
                patch["target"] = {"likelihoodScore": tl, "severityScore": ts, "rationale": _s(t.get("rationale"), 600)}
    if errs:
        return None, "; ".join(errs)
    if not patch:
        return None, "Nothing to change"
    return patch, None


def apply_hira_patch(draft: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """The draft as the editor will hold it after the patch (for the what-if)."""
    d = {**draft}
    ex = [dict(c) for c in d.get("existingControls") or []]
    for c in patch.get("setControlEffectiveness") or []:
        for e in ex:
            if e.get("key") == c["key"]:
                e["effectiveness"] = c["effectiveness"]
    ex += [{**c, "key": f"copilot-{i}"} for i, c in enumerate(patch.get("addExistingControls") or [])]
    d["existingControls"] = ex
    d["recommendedControls"] = [*(d.get("recommendedControls") or []),
                                *({**c, "status": "PROPOSED"} for c in patch.get("addRecommendedControls") or [])]
    return d


# ─── What-if: how controls move the residual risk ───────────────────────


def _cell(cells: list[Any], l: int, s: int) -> tuple[int, str, str | None]:
    from app.routers.hira import _compute_risk  # noqa: PLC0415 — router import is heavy

    return _compute_risk(cells, l, s)


def rate(cells: list[Any], l: int, s: int, *, threshold: str | None, bands: dict | None) -> dict[str, Any]:
    from app.routers.hira import _alarp_region  # noqa: PLC0415

    score, level, color = _cell(cells, l, s)
    region = _alarp_region(level, bands)
    within = threshold is None or (level in LEVEL_ORDER and threshold in LEVEL_ORDER
                                   and LEVEL_ORDER.index(level) <= LEVEL_ORDER.index(threshold))
    if region == "UNACCEPTABLE" or not within:
        verdict = "NOT_ACCEPTABLE"
    elif region == "TOLERABLE":
        verdict = "TOLERABLE_NEEDS_ALARP"
    else:
        verdict = "ACCEPTABLE"
    return {"likelihood": l, "severity": s, "score": score, "level": level, "color": color,
            "alarpRegion": region, "verdict": verdict}


def contributions(controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.routers.hira import CONTROL_REDUCTION, DEFAULT_EFFECTIVENESS_FACTOR, EFFECTIVENESS_FACTOR  # noqa: PLC0415

    out = []
    for c in controls:
        base = CONTROL_REDUCTION.get((c.get("hierarchy") or "").upper())
        if not base:
            continue
        eff = c.get("effectiveness")
        f = EFFECTIVENESS_FACTOR.get(eff, DEFAULT_EFFECTIVENESS_FACTOR) if eff else DEFAULT_EFFECTIVENESS_FACTOR
        out.append({"control": c.get("description"), "hierarchy": c.get("hierarchy"), "effectiveness": eff or "NOT_RATED",
                    "credit": f, "likelihoodSteps": round(base[0] * f, 2), "severitySteps": round(base[1] * f, 2)})
    return out


def residual(initial_l: int, initial_s: int, controls: list[dict[str, Any]]) -> tuple[int, int]:
    from app.routers.hira import _suggest_residual_scores  # noqa: PLC0415

    objs = [SimpleNamespace(hierarchy=c.get("hierarchy"), effectiveness=c.get("effectiveness")) for c in controls]
    return _suggest_residual_scores(initial_l, initial_s, objs)


def assess_controls(*, cells: list[Any], initial_l: int, initial_s: int, before_controls: list[dict[str, Any]],
                    after_controls: list[dict[str, Any]], threshold: str | None, bands: dict | None,
                    matrix_code: str | None = None) -> dict[str, Any]:
    """Residual risk before/after a control change, with the reasons, and the
    smallest single extra control that would make the residual acceptable."""
    initial = rate(cells, initial_l, initial_s, threshold=threshold, bands=bands)
    bl, bs = residual(initial_l, initial_s, before_controls)
    al, as_ = residual(initial_l, initial_s, after_controls)
    before = rate(cells, bl, bs, threshold=threshold, bands=bands)
    after = rate(cells, al, as_, threshold=threshold, bands=bands)
    lvl = LEVEL_ORDER.index
    direction = ("lower" if (after["score"], lvl(after["level"])) < (before["score"], lvl(before["level"]))
                 else "higher" if (after["score"], lvl(after["level"])) > (before["score"], lvl(before["level"]))
                 else "unchanged")
    lines = [
        f"Initial (no controls): L{initial_l} x S{initial_s} = {initial['level']} ({initial['score']}).",
        f"Residual before: L{bl} x S{bs} = {before['level']} ({before['score']}), {before['verdict'].replace('_', ' ').lower()}.",
        f"Residual after: L{al} x S{as_} = {after['level']} ({after['score']}), {after['verdict'].replace('_', ' ').lower()}.",
    ]
    if as_ == initial_s and initial_s >= 4:
        lines.append("Severity did not move: only elimination, substitution, engineering or PPE controls reduce severity, "
                     "and only elimination/substitution reduce it by more than one step.")
    if any((c.get("effectiveness") in (None, "NOT_VERIFIED")) for c in after_controls):
        lines.append("Unverified controls earn only part of their credit (not rated 60%, not verified 30%). "
                     "Verifying them on site can lower the residual further.")

    to_reach = None
    if after["verdict"] != "ACCEPTABLE":
        # Every single extra control (one per hierarchy level, verified effective)
        # that would lower the level, highest in the hierarchy first. The copilot
        # recommends from the top: PPE that "works" on paper is the last resort.
        options = []
        for h in HIERARCHY:
            tl, ts = residual(initial_l, initial_s, [*after_controls, {"hierarchy": h, "effectiveness": "EFFECTIVE"}])
            r = rate(cells, tl, ts, threshold=threshold, bands=bands)
            if lvl(r["level"]) < lvl(after["level"]) or r["verdict"] == "ACCEPTABLE":
                options.append({"addOneEffective": h, "wouldGive": r, "reachesAcceptable": r["verdict"] == "ACCEPTABLE"})
        if options:
            to_reach = {"options": options,
                        "note": "Prefer the highest level that is reasonably practicable; PPE and procedures alone are the last resort."}
        verified = [{**c, "effectiveness": "EFFECTIVE"} if c.get("effectiveness") in (None, "NOT_VERIFIED", "PARTIALLY_EFFECTIVE")
                    else c for c in after_controls]
        vl, vs = residual(initial_l, initial_s, verified)
        if (vl, vs) != (al, as_):
            to_reach = {**(to_reach or {}), "ifExistingVerifiedEffective": rate(cells, vl, vs, threshold=threshold, bands=bands)}

    return {
        "matrix": matrix_code, "acceptableThreshold": threshold, "initial": initial,
        "before": before, "after": after, "direction": direction,
        "contributionsAfter": contributions(after_controls),
        "explanation": lines, "toImprove": to_reach,
        "model": "Residual = initial minus control credits by hierarchy x effectiveness (strongest control full, others half). "
                 "Same model as the HIRA editor's auto-calculation.",
    }


async def load_matrix(db: Any, *, entry_id: str | None) -> tuple[Any, list[Any], str | None]:
    """The entry's study matrix, or the default matrix when no entry is open."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models.hira import HiraEntry, HiraStudy, RiskMatrix  # noqa: PLC0415
    from app.routers.hira import _load_matrix_scales  # noqa: PLC0415

    matrix = None
    routine = None
    if entry_id:
        entry = await db.get(HiraEntry, entry_id)
        if entry is not None:
            study = await db.get(HiraStudy, entry.studyId)
            matrix = await db.get(RiskMatrix, study.riskMatrixId) if study else None
            routine = entry.routine
    if matrix is None:
        matrix = (await db.execute(select(RiskMatrix).where(RiskMatrix.isDefault.is_(True)).limit(1))).scalar_one_or_none()
    if matrix is None:
        return None, [], routine
    cells, _, _ = await _load_matrix_scales(db, matrix.id)
    return matrix, cells, routine
