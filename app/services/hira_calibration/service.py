"""Calibration v2 wiring: flag modes, shadow logging, router hooks, one-click apply.

Flag hira.calibration.v2 per plant (HiraCalibrationConfig.mode):
  OFF     v1 everywhere (default; nothing below runs)
  SHADOW  users see v1; v2 runs alongside and both go to hira_copilot_shadow_runs
  ON      users see v2 (review card, residual from the v2 engine, calibrated drafts)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.hira_calibration import config as cal_config
from app.services.hira_calibration import loader
from app.services.hira_calibration import review_v2 as rv

log = logging.getLogger("safeops360.hira.calibration")


async def plant_of_entry(db: AsyncSession, entry_id: str) -> str | None:
    from app.models.hira import HiraEntry, HiraStudy  # noqa: PLC0415

    row = (await db.execute(select(HiraStudy.plantId).join(HiraEntry, HiraEntry.studyId == HiraStudy.id)
                            .where(HiraEntry.id == entry_id))).first()
    return row[0] if row else None


async def mode_for_plant(db: AsyncSession, plant_id: str | None) -> tuple[dict[str, Any], str, dict[str, Any]]:
    return await cal_config.load(db, plant_id)


async def evaluate_entry(db: AsyncSession, entry_id: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    if cfg is None:
        cfg, _, _ = await cal_config.load(db, await plant_of_entry(db, entry_id))
    built = await loader.build(db, entry_id, cfg)
    out = rv.evaluate(built["input"], cfg=cfg, rule=built["rule"], evidence=built["evidence"], plant=built["plant"])
    out["schemaReady"] = built["schemaReady"]
    return out


def _v1_summary(v1: dict[str, Any]) -> dict[str, Any]:
    res = v1.get("residual") or {}
    saved = res.get("saved") or {}
    model = res.get("fromControls") or {}
    return {"headline": v1.get("headline"), "counts": v1.get("counts"),
            "codes": [f["code"] for f in v1.get("findings") or []],
            "savedResidual": saved.get("level") and f"{saved.get('level')} ({saved.get('score')})",
            "controlsJustify": model.get("level") and
            f"L{model.get('likelihood')} x S{model.get('severity')} = {model.get('level')} ({model.get('score')})",
            "controlIdeas": len(v1.get("controlIdeas") or [])}


def _v2_summary(v2: dict[str, Any]) -> dict[str, Any]:
    e = v2["entry"]
    return {"status": e["status"], "inherent": e["inherent"] and f"{e['inherent']['level']} ({e['inherent']['score']})",
            "residual": e["residual"] and f"{e['residual']['level']} ({e['residual']['score']})",
            "inherentOnly": e["inherentOnly"], "counts": v2["counts"],
            "blockers": sorted(rv.blocker_codes(v2)), "recommendations": len(v2["recommendations"]),
            "existingControlProposals": len(v2["existingControlProposals"])}


async def log_shadow(*, surface: str, plant_id: str | None, entry_id: str | None, mode: str,
                     versions: dict[str, Any], v1: Any, v2: Any, summary: dict[str, Any] | None,
                     user_id: str | None) -> None:
    """Write one shadow row in its own session; never fails the caller."""
    from app.core.db import AsyncSessionLocal  # noqa: PLC0415
    from app.models.hira_calibration import HiraCopilotShadowRun  # noqa: PLC0415

    try:
        async with AsyncSessionLocal() as s:
            s.add(HiraCopilotShadowRun(plant_id=plant_id, entry_id=entry_id, surface=surface, mode=mode,
                                       engine_version=cal_config.ENGINE_VERSION, config_version=versions,
                                       v1=v1, v2=v2, summary=summary, user_id=user_id))
            await s.commit()
    except Exception as exc:  # noqa: BLE001 — shadow logging is best effort
        log.warning("hira calibration shadow log failed (%s): %s", surface, exc)


async def review(db: AsyncSession, entry_id: str, *, user_id: str | None, preview: bool = False) -> dict[str, Any]:
    """The review the entry page shows, by the plant's flag mode.
    preview=True (calibration managers) returns v2 even when the flag is OFF."""
    from app.services.copilot.hira_review import review_entry  # noqa: PLC0415

    plant_id = await plant_of_entry(db, entry_id)
    cfg, mode, versions = await cal_config.load(db, plant_id)
    if mode == "ON" or preview:
        v2 = await evaluate_entry(db, entry_id, cfg)
        return {"version": "v2", "mode": mode, **v2}
    v1 = await review_entry(db, entry_id)
    if mode == "SHADOW":
        try:
            v2 = await evaluate_entry(db, entry_id, cfg)
            await log_shadow(surface="REVIEW", plant_id=plant_id, entry_id=entry_id, mode=mode, versions=versions,
                             v1=v1, v2=v2, summary={"v1": _v1_summary(v1), "v2": _v2_summary(v2)}, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — v2 must never break the v1 page
            log.warning("hira calibration shadow review failed for %s: %s", entry_id, exc)
    return {"version": "v1", "mode": mode, **v1}


# ─── router hook: the residual from the v2 engine when the flag is ON ────


async def apply_v2_residual(db: AsyncSession, entry: Any, liks_by_score: dict[int, Any],
                            sevs_by_score: dict[int, Any], cells: list[Any]) -> bool:
    """When the plant's flag is ON, set the entry's residual from the v2 engine
    (max of the hazard residuals) instead of the v1 model, or clear it when no
    controls are recorded (never residual = initial from an empty list).
    Returns False (caller falls back to v1) when the flag is not ON."""
    plant_id = entry.study.plantId if getattr(entry, "study", None) else await plant_of_entry(db, entry.id)
    cfg, mode, _ = await cal_config.load(db, plant_id)
    if mode != "ON":
        return False
    await db.flush()
    try:
        result = await evaluate_entry(db, entry.id, cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("v2 residual failed for %s, falling back to v1: %s", entry.id, exc)
        return False
    r = result["entry"]["residual"]
    if r is None:
        for f in ("residualLikelihoodId", "residualLikelihoodScore", "residualSeverityId", "residualSeverityScore",
                  "residualRiskScore", "residualRiskLevel", "residualRiskColor"):
            setattr(entry, f, None)
        return True
    lk, sv = liks_by_score.get(r["likelihood"]), sevs_by_score.get(r["severity"])
    if lk is None or sv is None:
        return False
    cell = next((c for c in cells if c.likelihoodScore == r["likelihood"] and c.severityScore == r["severity"]), None)
    entry.residualLikelihoodId, entry.residualLikelihoodScore = lk.id, r["likelihood"]
    entry.residualSeverityId, entry.residualSeverityScore = sv.id, r["severity"]
    entry.residualRiskScore, entry.residualRiskLevel = r["score"], r["level"]
    entry.residualRiskColor = cell.colorHex if cell else None
    return True


# ─── one-click apply ─────────────────────────────────────────────────────


async def apply(db: AsyncSession, entry_id: str, *, parts: list[str], change_reason: str | None, user: Any,
                selected_recommendations: list[str] | None = None) -> dict[str, Any]:
    """Apply what the v2 review proposes, through the HIRA router's own
    functions (permissions, versioning, change reasons stay in one place).

    parts: EXISTING_CONTROLS | RECOMMENDATIONS | DEFAULTS
    """
    from fastapi import HTTPException, status  # noqa: PLC0415

    from app.models.hira import HiraEntry, HiraEntryHazard  # noqa: PLC0415
    from app.routers.hira import replace_existing_controls, replace_recommended_controls, update_entry  # noqa: PLC0415
    from app.schemas.hira import (  # noqa: PLC0415
        HiraEntryControlReplaceItem,
        HiraEntryControlReplaceRequest,
        HiraEntryRecommendedControlReplaceItem,
        HiraEntryRecommendedControlReplaceRequest,
        HiraEntryUpdate,
    )
    from app.services.copilot import hira_builder  # noqa: PLC0415
    from sqlalchemy.orm import selectinload  # noqa: PLC0415

    plant_id = await plant_of_entry(db, entry_id)
    cfg, mode, _ = await cal_config.load(db, plant_id)
    result = await evaluate_entry(db, entry_id, cfg)
    entry = (await db.execute(select(HiraEntry).where(HiraEntry.id == entry_id).options(
        selectinload(HiraEntry.study), selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
        selectinload(HiraEntry.existingControls), selectinload(HiraEntry.recommendedControls)))).scalar_one()
    if not entry.isCurrentVersion:
        raise HTTPException(status.HTTP_409_CONFLICT, "That is an old version of the entry")
    study = entry.study
    versioned_entry = study.status in hira_builder.LOCKED_STUDY_STATUSES or entry.versionNumber > 1
    if versioned_entry and not (change_reason or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "This entry is on an approved/active study: a change reason is required")
    reason = (change_reason or "").strip() or None
    done: list[str] = []
    defaults = result["suggestedDefaults"]
    ready = result.get("schemaReady")

    def forget() -> None:
        db.expire(entry, ["hazards", "existingControls", "recommendedControls"])

    versioned = False
    patch: dict[str, Any] = {}
    if "DEFAULTS" in parts and defaults.get("alarpRecord"):
        patch.update(defaults["alarpRecord"])
    if patch:
        forget()
        await update_entry(entry_id=entry.id, payload=HiraEntryUpdate(**patch), change_reason=reason,
                           change_trigger=None, user=user, db=db)
        versioned = versioned_entry
        done.append("defaults")

    new_controls: list[dict[str, Any]] = []
    eff = {x["key"]: x["effectiveness"] for x in defaults.get("controlEffectiveness") or []} \
        if "DEFAULTS" in parts else {}
    if "EXISTING_CONTROLS" in parts or eff:
        current = [{k: getattr(c, k, None) for k in ("id", "controlId", "hierarchy", "description", "effectiveness",
                                                      "verificationMethod", "verificationFreq", "responsibleRole",
                                                      "evidenceAttached", "documentReference")} | {"_id": c.id}
                   for c in sorted(entry.existingControls, key=lambda c: c.sortOrder or 0)]
        for c in current:
            if c["_id"] in eff and not c["effectiveness"]:
                c["effectiveness"] = eff[c["_id"]]
        if "EXISTING_CONTROLS" in parts:
            for p in result["existingControlProposals"]:
                current.append({"controlId": None, "hierarchy": p["hierarchy"], "description": p["description"],
                                "effectiveness": "NOT_VERIFIED", "verificationMethod": p.get("verificationMethod"),
                                "verificationFreq": None, "responsibleRole": None, "evidenceAttached": False,
                                "documentReference": p.get("source"), "_hazards": p.get("hazardKeys")})
                new_controls.append(p)
        forget()
        await replace_existing_controls(
            entry_id=entry.id,
            payload=HiraEntryControlReplaceRequest(controls=[
                HiraEntryControlReplaceItem(**{k: v for k, v in c.items() if not k.startswith("_")}, sortOrder=i)
                for i, c in enumerate(current)]),
            change_reason=reason, skip_version=versioned, user=user, db=db)
        versioned = versioned or study.status in hira_builder.LOCKED_STUDY_STATUSES
        done.append("existing controls")

    recs = result["recommendations"]
    if selected_recommendations is not None:
        recs = [r for r in recs if r["id"] in selected_recommendations]
    rec_defaults = {x["key"]: x for x in defaults.get("recommendedDefaults") or []} if "DEFAULTS" in parts else {}
    if ("RECOMMENDATIONS" in parts and recs) or rec_defaults:
        keep = []
        for c in entry.recommendedControls:
            d = rec_defaults.get(c.id) or {}
            keep.append({"id": c.id, "hierarchy": c.hierarchy, "description": c.description, "rationale": c.rationale,
                         "targetLikelihoodReduction": c.targetLikelihoodReduction,
                         "targetSeverityReduction": c.targetSeverityReduction,
                         "estimatedCostBand": c.estimatedCostBand,
                         "proposedImplementationDate": c.proposedImplementationDate or (
                             datetime.fromisoformat(d["proposedImplementationDate"]).replace(tzinfo=timezone.utc)
                             if d.get("proposedImplementationDate") else None),
                         "responsibleId": c.responsibleId, "status": c.status, "capaId": c.capaId,
                         "evidenceAttached": bool(c.evidenceAttached), "documentReference": c.documentReference})
        if "RECOMMENDATIONS" in parts:
            for r in recs:
                m_l = re.search(r"L -(\d)", r["expectedEffect"])
                m_s = re.search(r"S -(\d)", r["expectedEffect"])
                dl, ds = (int(m_l.group(1)) if m_l else None), (int(m_s.group(1)) if m_s else None)
                keep.append({"hierarchy": r["hierarchy"], "description": r["description"],
                             "rationale": f"{r['expectedEffect']} on {', '.join(t['name'] for t in r['targetHazards'])}"
                                          f" (residual after: {r['residualAfter']['level']} {r['residualAfter']['score']})"
                                          + (f". {r['interimMeasure']}" if r.get("interimMeasure") else "")
                                          + (" Study-level common control." if r.get("studyLevel") else ""),
                             "targetLikelihoodReduction": dl, "targetSeverityReduction": ds,
                             "estimatedCostBand": r["costBand"],
                             "proposedImplementationDate": datetime.fromisoformat(r["dueDate"]).replace(
                                 tzinfo=timezone.utc),
                             "responsibleId": None, "status": "PROPOSED", "capaId": None,
                             "evidenceAttached": False, "documentReference": None})
        forget()
        await replace_recommended_controls(
            entry_id=entry.id,
            payload=HiraEntryRecommendedControlReplaceRequest(controls=[
                HiraEntryRecommendedControlReplaceItem(**c) for c in keep]),
            change_reason=reason, skip_version=versioned, user=user, db=db)
        done.append("recommended controls")

    # Columns added by the calibration DDL: links, owner roles, per-hazard ratings.
    if ready:
        if "RECOMMENDATIONS" in parts:
            for r in recs:
                await db.execute(text(
                    'UPDATE "HiraEntryRecommendedControl" SET "ownerRole" = :o, "appliesToHazardIds" = '
                    'CAST(:h AS JSONB), "calibrationRef" = :ref WHERE "entryId" = :e AND description = :d'),
                    {"o": r["ownerRole"], "h": _json([t["key"] for t in r["targetHazards"]]), "ref": r["id"],
                     "e": entry.id, "d": r["description"]})
        for d in (defaults.get("recommendedDefaults") or []) if "DEFAULTS" in parts else []:
            await db.execute(text('UPDATE "HiraEntryRecommendedControl" SET "ownerRole" = COALESCE("ownerRole", :o) '
                                  'WHERE id = :id'), {"o": d["ownerRole"], "id": d["key"]})
        for p in new_controls:
            await db.execute(text('UPDATE "HiraEntryControl" SET "appliesToHazardIds" = CAST(:h AS JSONB) '
                                  'WHERE "entryId" = :e AND description = :d'),
                             {"h": _json(p.get("hazardKeys") or []), "e": entry.id, "d": p["description"]})
        if "DEFAULTS" in parts:
            for h in defaults.get("hazardRatings") or []:
                await db.execute(text(
                    'UPDATE "HiraEntryHazard" SET "likelihoodScore" = :l, "severityScore" = :s, '
                    '"likelihoodRationale" = :lr, "severityRationale" = :sr, "ratingSource" = \'ESTIMATED\' '
                    'WHERE "entryId" = :e AND "hazardId" = :h AND "likelihoodScore" IS NULL'),
                    {"l": h["likelihood"], "s": h["severity"], "lr": h["likelihoodRationale"],
                     "sr": h["severityRationale"], "e": entry.id, "h": h["key"]})
            for g in defaults.get("regulations") or []:
                await db.execute(text(
                    'UPDATE "HiraEntryHazard" SET "regulationRef" = :r, "regulationSection" = :s, '
                    '"regulationSuggested" = true WHERE "entryId" = :e AND "hazardId" = :h AND '
                    '("regulationRef" IS NULL OR "regulationRef" = \'\')'),
                    {"r": g["regulation"][:200], "s": (g.get("section") or "")[:120], "e": entry.id, "h": g["key"]})
    elif "DEFAULTS" in parts and (defaults.get("hazardRatings") or defaults.get("regulations")):
        done.append("per-hazard ratings and regulation suggestions skipped (calibration schema not applied yet)")

    if "DEFAULTS" in parts and defaults.get("nextReviewDue") and entry.nextReviewDue is None:
        entry.nextReviewDue = datetime.fromisoformat(defaults["nextReviewDue"]).replace(tzinfo=timezone.utc)
        done.append("review date")
    entry.updatedAt = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()
    after = await evaluate_entry(db, entry_id, cfg)
    return {"applied": done, "mode": mode, "review": {"version": "v2", "mode": mode, **after}}


# ─── Copilot chat drafts (create_hira_entry) ─────────────────────────────


async def calibrate_copilot_draft(db: AsyncSession, *, study: Any, area_id: str | None, parsed: dict[str, Any],
                                  expand: bool, user_id: str | None) -> dict[str, Any] | None:
    """Calibrate a Copilot-drafted entry before it is saved. Returns None when
    the flag is OFF; {"mode": "SHADOW"} after logging; for ON the parsed tool
    arguments rewritten by the engine plus a summary for the model."""
    from app.models.hira import HiraHazard  # noqa: PLC0415
    from app.services.hira_calibration import assistant_v2  # noqa: PLC0415
    from app.services.hira_calibration import risk_engine as eng  # noqa: PLC0415

    cfg, mode, versions = await cal_config.load(db, study.plantId)
    if mode == "OFF":
        return None
    ids = [h["hazardId"] for h in parsed["hazards"]]
    masters = {r[0]: r for r in (await db.execute(
        select(HiraHazard.id, HiraHazard.name, HiraHazard.code, HiraHazard.category).where(HiraHazard.id.in_(ids)))).all()}
    hazards = []
    for h in parsed["hazards"]:
        m = masters.get(h["hazardId"])
        hazards.append({"key": h["hazardId"], "name": m[1] if m else h["hazardId"], "code": m[2] if m else None,
                        "category": m[3] if m else None,
                        "consequence": h.get("consequence") or h.get("contextualDescription"),
                        "likelihood": h.get("likelihood") or parsed["initialLikelihood"],
                        "severity": h.get("severity") or parsed["initialSeverity"]})
    families = {eng.hazard_family(x["code"], x["category"], f"{x['name']} {x['consequence'] or ''}") for x in hazards}
    built = await loader.build_for_draft(db, study=study, area_id=area_id, families=families, cfg=cfg)
    draft = {"activity": parsed["activityDescription"], "routine": parsed["routine"], "frequency": parsed["frequency"],
             "hazards": hazards, "existingControls": parsed["existingControls"],
             "recommendedControls": [{"hierarchy": c["hierarchy"], "description": c["description"],
                                      "costBand": c.get("estimatedCostBand")} for c in parsed["recommendedControls"]]}
    cal = assistant_v2.calibrate_draft(draft, cfg=cfg, rule=built["rule"], evidence=built["evidence"],
                                       plant=built["plant"], expand=expand)
    summary = {"notes": cal["calibrationNotes"],
               "entryInherent": cal["entry"]["inherent"], "entryResidual": cal["entry"]["residual"],
               "residualLabel": cal["entry"]["residualLabel"],
               "hazards": [{"name": h["name"], "inherent": h["inherent"], "residual": h["residual"]}
                           for h in cal["hazards"]],
               "droppedHazards": [d["name"] for d in cal["droppedHazards"]],
               "recommendations": [{"description": r["description"], "effect": r["expectedEffect"],
                                    "owner": r["ownerRole"], "dueDays": r["dueDays"]} for r in cal["recommendations"]],
               "linkedCapas": [c.get("number") for c in cal["linkedCapas"]],
               "existingControlsToConfirm": [p["description"] for p in cal["existingControlProposals"]][:5]}
    if mode == "SHADOW":
        await log_shadow(surface="COPILOT_DRAFT", plant_id=study.plantId, entry_id=None, mode=mode, versions=versions,
                         v1={"initial": [parsed["initialLikelihood"], parsed["initialSeverity"]],
                             "hazards": len(parsed["hazards"]), "existingControls": len(parsed["existingControls"]),
                             "recommended": len(parsed["recommendedControls"])},
                         v2=summary, summary={"v2Recommendations": len(cal["recommendations"]),
                                              "v1Recommendations": len(parsed["recommendedControls"])},
                         user_id=user_id)
        return {"mode": "SHADOW"}

    kept = {h["key"] for h in cal["hazards"]}
    out = dict(parsed)
    out["hazards"] = [h for h in parsed["hazards"] if h["hazardId"] in kept]
    inh = cal["entry"]["inherent"]
    if inh:
        out["initialLikelihood"], out["initialSeverity"] = inh["likelihood"], inh["severity"]
        if (inh["likelihood"], inh["severity"]) != (parsed["initialLikelihood"], parsed["initialSeverity"]):
            out["initialLikelihoodRationale"] = (parsed["initialLikelihoodRationale"] + " [Calibrated: "
                                                 + "; ".join(n for n in cal["calibrationNotes"] if "likelihood" in n)[:400]
                                                 + "]")[:1000]
            out["initialSeverityRationale"] = (parsed["initialSeverityRationale"] + " [Entry rating = the driving "
                                               "hazard's credible worst case]")[:1000]
    out["recommendedControls"] = [{"hierarchy": r["hierarchy"], "description": r["description"][:500],
                                   "rationale": f"{r['expectedEffect']} on "
                                                f"{', '.join(t['name'] for t in r['targetHazards'])}"[:500],
                                   "estimatedCostBand": r["costBand"]} for r in cal["recommendations"]]
    ratings = [{"key": h["key"], "likelihood": h["inherent"]["likelihood"], "severity": h["inherent"]["severity"],
                "likelihoodRationale": h.get("likelihoodRationale") or h["likelihood"]["reason"],
                "severityRationale": h.get("severityRationale")} for h in cal["hazards"]]
    return {"mode": "ON", "parsed": out, "summary": summary, "hazardRatings": ratings}


async def save_hazard_ratings(db: AsyncSession, entry_id: str, ratings: list[dict[str, Any]],
                              source: str = "COPILOT") -> bool:
    """Write per-hazard ratings (columns from the calibration DDL). No-op before the DDL."""
    if not ratings or not await loader.calibration_schema_ready(db):
        return False
    for r in ratings:
        await db.execute(text(
            'UPDATE "HiraEntryHazard" SET "likelihoodScore" = :l, "severityScore" = :s, "likelihoodRationale" = :lr, '
            '"severityRationale" = :sr, "ratingSource" = :src WHERE "entryId" = :e AND "hazardId" = :h'),
            {"l": r["likelihood"], "s": r["severity"], "lr": (r.get("likelihoodRationale") or "")[:2000],
             "sr": (r.get("severityRationale") or "")[:2000], "src": source, "e": entry_id, "h": r["key"]})
    return True


# ─── HIRA Assistant (LLM agent) output ───────────────────────────────────

_REC_KEY = {"ELIMINATION": "elimination_controls", "SUBSTITUTION": "substitution_controls",
            "ENGINEERING": "engineering_controls", "ADMINISTRATIVE": "administrative_controls",
            "PPE": "ppe_controls"}


def _compact(cal: dict[str, Any]) -> dict[str, Any]:
    return {
        "hazards": [{"key": h["key"], "name": h["name"], "family": h["family"], "inherent": h["inherent"],
                     "residual": h["residual"], "likelihood": h["likelihood"], "severity": h["severity"],
                     "tolerability": h["tolerability"], "manifestationNeeded": h.get("manifestationNeeded")}
                    for h in cal["hazards"]],
        "droppedHazards": cal["droppedHazards"], "entry": cal["entry"], "recommendations": cal["recommendations"],
        "linkedCapas": cal["linkedCapas"], "existingControlProposals": cal["existingControlProposals"],
        "notes": cal["calibrationNotes"],
        "findings": [{"code": f["code"], "class": f["class"], "title": f["title"]} for f in cal["findings"]],
    }


async def calibrate_assistant_output(db: AsyncSession, *, entry_id: str, suggestion: Any, context: dict[str, Any],
                                     user_id: str | None) -> Any:
    """Flag ON: the Assistant's hazards and controls pass the v2 calibration
    (<=5 hazards, evidence-capped likelihood, credible severity, capped and
    de-duplicated recommendations) and its residual guidance is replaced by the
    engine's residual. SHADOW: unchanged output, v1 and v2 logged. OFF: no-op."""
    from app.services.hira_calibration import assistant_v2  # noqa: PLC0415

    if not isinstance(suggestion, dict):
        return suggestion
    plant_id = await plant_of_entry(db, entry_id)
    cfg, mode, versions = await cal_config.load(db, plant_id)
    if mode == "OFF":
        return suggestion
    try:
        built = await loader.build(db, entry_id, cfg)
        library = {h.get("hazard_master_id"): h for h in
                   ((context.get("context") or {}).get("applicable_hazard_library") or [])}
        draft = assistant_v2.from_assistant_suggestion(suggestion, context=context, library=library)
        draft["existingControls"] = [{"hierarchy": c.hierarchy, "description": c.description,
                                      "effectiveness": c.effectiveness, "verificationMethod": c.verification_method,
                                      "hazardKeys": list(c.hazard_keys) if c.hazard_keys else None}
                                     for c in built["input"].controls if not c.planned]
        draft["reviewDateSet"] = built["input"].review_date_set
        draft["eliminationConsidered"] = built["input"].elimination_considered
        cal = assistant_v2.calibrate_draft(draft, cfg=cfg, rule=built["rule"], evidence=built["evidence"],
                                           plant=built["plant"])
    except Exception as exc:  # noqa: BLE001 — never lose the Assistant's answer over calibration
        log.warning("assistant calibration failed for %s: %s", entry_id, exc)
        return suggestion
    compact = _compact(cal)
    if mode == "SHADOW":
        await log_shadow(surface="ASSISTANT", plant_id=plant_id, entry_id=entry_id, mode=mode, versions=versions,
                         v1=suggestion, v2=compact,
                         summary={"v1": {"hazards": len(suggestion.get("suggested_hazards") or []),
                                         "controls": sum(len(b.get(k) or []) for b in suggestion.get(
                                             "control_suggestions") or [] for k in _REC_KEY.values())},
                                  "v2": {"hazards": len(cal["hazards"]), "recommendations": len(cal["recommendations"]),
                                         "status": cal["entry"]["status"]}},
                         user_id=user_id)
        return suggestion

    out = dict(suggestion)
    kept = {h["key"] for h in cal["hazards"]}
    out["suggested_hazards"] = [h for h in suggestion.get("suggested_hazards") or []
                                if isinstance(h, dict) and h.get("hazard_master_id") in kept]
    blocks: dict[str, dict[str, Any]] = {}
    for r in cal["recommendations"]:
        hid = r["targetHazards"][0]["key"]
        b = blocks.setdefault(hid, {"hazard_id": hid, **{k: [] for k in _REC_KEY.values()}})
        b[_REC_KEY.get(r["hierarchy"], "administrative_controls")].append({
            "description": r["description"],
            "rationale": f"{r['expectedEffect']} on {', '.join(t['name'] for t in r['targetHazards'])}; owner "
                         f"{r['ownerRole']}, due in {r['dueDays']} days" + (f". {r['interimMeasure']}"
                                                                            if r.get("interimMeasure") else ""),
            "effectiveness_evidence": "Calibrated by the plant's risk rules",
            "estimated_cost_band": (r["costBand"] or "MEDIUM").lower()})
    out["control_suggestions"] = list(blocks.values())
    rc = dict(out.get("risk_calibration") or {})
    e = cal["entry"]
    residual_txt = (f"Residual is calculated from the recorded controls: L{e['residual']['likelihood']} x "
                    f"S{e['residual']['severity']} = {e['residual']['level']}." if e.get("residual")
                    else "Inherent only. Existing controls not captured, so no residual is shown.")
    rc["residual_likelihood_guidance"] = residual_txt
    rc["residual_severity_guidance"] = ("Procedures and PPE never lower severity; only elimination, substitution "
                                        "or a justified mitigating engineering control can.")
    if e.get("inherent"):
        for key, axis in (("initial_likelihood_suggestion", "likelihood"), ("initial_severity_suggestion", "severity")):
            sug = dict(rc.get(key) or {})
            if sug.get("score") and sug["score"] != e["inherent"][axis]:
                sug["calibrated_score"] = e["inherent"][axis]
                sug["reasoning"] = (sug.get("reasoning") or "") + (
                    f" [Calibrated to {axis[0].upper()}{e['inherent'][axis]} for the driving hazard: "
                    + "; ".join(cal["calibrationNotes"][:2]) + "]")
            rc[key] = sug
    out["risk_calibration"] = rc
    out["calibration"] = compact
    return out


def _json(v: Any) -> str:
    import json  # noqa: PLC0415

    return json.dumps(v)


__all__ = ["apply", "apply_v2_residual", "evaluate_entry", "log_shadow", "mode_for_plant", "plant_of_entry", "review"]
