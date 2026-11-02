"""Copilot review of one HIRA entry (activity): what is wrong, what to correct in
the residual risk, and what to improve.

Two kinds of evidence:

  * The entry as saved now: initial and residual rating, hazards, controls,
    recommended controls, review date. Rated with the HIRA router's own matrix,
    ALARP bands and residual model (via form_drafts), so the numbers match the
    editor.
  * The client's source sheet, when the entry was imported from one
    (knowledge/data/jsl_hira_source.json, written by scripts/import_jsl_hisar.py):
    the QSD/24/F scores (SP, LH, CN, SV, DE, GT), the qualitative concerns, the
    Yes/No verdict and the QSD/25/F control-strategy rows. These let the review
    point at the exact sheet row that needs fixing.

Everything here is deterministic. The chat copilot calls the same function
(tool `review_hira_entry`) and explains or applies the fixes in the editor; the
assessor still reviews and saves.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.services.copilot import form_drafts
from app.services.copilot.knowledge import hira_library as hira_kb

SOURCE = Path(__file__).with_name("knowledge") / "data" / "jsl_hira_source.json"
STRONG = ("ELIMINATION", "SUBSTITUTION", "ENGINEERING")
LEVELS = form_drafts.LEVEL_ORDER
ORDER = {"error": 0, "warning": 1, "suggestion": 2, "info": 3}


@lru_cache(maxsize=1)
def _source_file() -> dict[str, Any]:
    try:
        return json.loads(SOURCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def source_for(study_number: str | None, sequence: int | None) -> dict[str, Any] | None:
    study = (_source_file().get("studies") or {}).get(study_number or "")
    if not study:
        return None
    entry = (study.get("entries") or {}).get(str(sequence))
    return {"study": study, "entry": entry} if entry else None


def _yn(v: Any) -> bool:
    return str(v or "").strip().upper() == "Y"


def _concerns(r: dict[str, Any]) -> list[str]:
    names = {"LC": "legal", "DC": "domino", "CE": "chronic effect", "EC": "emergency"}
    return [n for k, n in names.items() if _yn(r.get(k))]


def _verdict_text(v: str) -> str:
    return {"ACCEPTABLE": "acceptable", "TOLERABLE_NEEDS_ALARP": "tolerable only if ALARP is shown",
            "NOT_ACCEPTABLE": "not acceptable"}.get(v, v.lower())


def _rating_text(r: dict[str, Any]) -> str:
    return f"L{r['likelihood']} x S{r['severity']} = {r['level']} ({r['score']})"


class _Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, code: str, level: str, area: str, title: str, detail: str, fix: str | None = None,
            sheet_row: int | None = None) -> None:
        self.items.append({"code": code, "level": level, "area": area, "title": title, "detail": detail,
                           "fix": fix, "sheetRow": sheet_row})

    def sorted(self) -> list[dict[str, Any]]:
        return sorted(self.items, key=lambda f: ORDER.get(f["level"], 9))


# ── sheet checks ──────────────────────────────────────────────────────────

SEVERE_TYPES = {"STEAM_EXPLOSION": 4, "MOLTEN_METAL_BREAKOUT": 4, "FIRE_EXPLOSION": 4}
MOLTEN = re.compile(r"liquid metal|molten|hot metal|slag|ladle|tapping|splash", re.I)
CRANE = re.compile(r"crane|ladle|hook|wire rope|lift", re.I)
MISMATCH = [  # consequence words that do not follow from the hazard type
    (re.compile(r"ignite|combustible|fire", re.I), {"FIRE", "FIRE_EXPLOSION", "BURN_SCALD", "STEAM_EXPLOSION"}, "fire"),
    (re.compile(r"back injur|posture|strain|sprain", re.I), {"ERGONOMIC"}, "ergonomic (manual handling)"),
    (re.compile(r"inhal|respirat|dust|breathing", re.I), {"DUST_FUME"}, "dust / fume"),
]


def _sheet_checks(f: _Findings, src: dict[str, Any], entry_seq: int) -> None:
    rows = src["entry"]["rows"]
    ctrl_rows = src["entry"].get("controlRows") or []
    for r in rows:
        row = r["xl_row"]
        lh, cn, sv, sp, de, gt = (r.get(k) for k in ("LH", "CN", "SV", "SP", "DE", "GT"))
        if None not in (lh, cn, sv) and lh * cn != sv:
            f.add("SV_ARITHMETIC", "error", "Sheet scoring", f"SV is wrong for '{r['hazard']}'",
                  f"LH {lh} x CN {cn} = {lh * cn}, but the sheet shows SV {sv}.",
                  f"Correct SV to {lh * cn} and GT to {(sp or 1) * lh * cn * (de or 1)} in QSD/24/F row {row}.", row)
        elif None not in (sp, sv, de, gt) and sp * sv * de != gt:
            f.add("GT_ARITHMETIC", "error", "Sheet scoring", f"GT is wrong for '{r['hazard']}'",
                  f"SP {sp} x SV {sv} x DE {de} = {sp * sv * de}, but the sheet shows GT {gt}.",
                  f"Correct GT to {sp * sv * de} in QSD/24/F row {row}.", row)

        said = str(r.get("acceptable") or "").strip().lower()
        rule = bool(r.get("acceptableByRule"))
        why = ", ".join(_concerns(r)) or (f"GT {gt} > 3.33" if gt and gt > 3.33 else "")
        if not said:
            f.add("VERDICT_BLANK", "warning", "Sheet scoring", "'Acceptable OH&S Risk' is blank",
                  f"Row {row} has no Yes/No. By the plant's own rule it is {'acceptable' if rule else 'NOT acceptable'}"
                  + (f" ({why})." if why else "."),
                  f"Fill '{'Yes' if rule else 'No'}' in row {row}" + ("" if rule else " and add a QSD/25/F strategy row."), row)
        elif said.startswith("y") and not rule:
            f.add("VERDICT_RULE", "error", "Sheet scoring", "Marked acceptable, but the plant rule says it is not",
                  f"Row {row} says Yes, but {why} makes it significant (any Y concern or GT > 3.33).",
                  f"Change row {row} to 'No' and add a control strategy row in QSD/25/F.", row)
        elif said.startswith("n") and rule:
            f.add("VERDICT_CONSERVATIVE", "suggestion", "Sheet scoring", "Marked not acceptable although the score allows it",
                  f"Row {row}: GT {gt} with no Y concern would be acceptable by the plant rule; the team chose 'No'.",
                  "Fine if deliberate: write the reason in the HIRA, or correct the verdict to 'Yes'.", row)

        not_ok = said.startswith("n") or (not said and not rule) or (said.startswith("y") and not rule)
        if not_ok and not any(c["forHiraRow"] == row for c in ctrl_rows):
            f.add("NO_STRATEGY_ROW", "error", "Controls", f"No QSD/25/F control strategy for '{r['hazard']}'",
                  "Every unacceptable risk needs a row in the 'Controls' tab (strategy for unacceptable risks); this one has none.",
                  "Add the strategy row with the controls by hierarchy, or add recommended controls here.", row)

        if _yn(r.get("LC")) and not r.get("legal"):
            f.add("LEGAL_MISSING", "warning", "Legal", "Legal concern is Y but no legal reference is given",
                  f"Row {row} flags a legal concern without naming the requirement.",
                  "Add the applicable Act/Rule and section (e.g. Factories Act 1948, Sec. 29 for lifting tackle).", row)
        elif r.get("legal") and not _yn(r.get("LC")):
            f.add("LEGAL_FLAG", "suggestion", "Legal", "Legal reference given but LC is N",
                  f"Row {row} cites '{r['legal']}' while the legal concern column is N.",
                  "Set LC = Y (which makes the risk significant) or drop the reference if it does not apply.", row)

        ht = r.get("hazardType")
        hts = set(r.get("hazardTypes") or ([ht] if ht else []))
        risk = r.get("risk") or ""
        for rx, ok_types, label in MISMATCH:
            if rx.search(risk) and hts and not (hts & ok_types):
                f.add("HAZARD_CONSEQUENCE", "warning", "Hazard", "Consequence does not match the hazard",
                      f"Row {row}: hazard '{r['hazard']}' but the consequence reads '{risk}' ({label}).",
                      "Correct the hazard or the consequence; it looks like a copy-paste from another row.", row)
                break
        for flag in r.get("libraryFlags") or []:
            code = flag.split(":")[0]
            if code in ("HAZARD_CORRECTED", "CONSEQUENCE_CORRECTED") and not any(
                    x["code"] == "HAZARD_CONSEQUENCE" and x["sheetRow"] == row for x in f.items):
                f.add("HAZARD_CONSEQUENCE", "warning", "Hazard", "Consequence does not match the hazard",
                      flag.split(":", 1)[1].strip(), "Correct the hazard or the consequence in the sheet.", row)
            if code == "VAGUE_CONSEQUENCE":
                f.add("VAGUE_CONSEQUENCE", "suggestion", "Hazard", "Consequence is too vague",
                      f"Row {row} says only '{risk}'.",
                      "State the credible worst outcome, e.g. 'serious burns from molten metal splash' or "
                      "'fatal injury if the load falls'. The severity rating should follow from it.", row)

        needs = [SEVERE_TYPES[t] for t in hts if t in SEVERE_TYPES]
        if "BURN_SCALD" in hts and MOLTEN.search(f"{r.get('activity')} {risk}"):
            needs.append(3)
        if "EQUIPMENT_FAILURE" in hts and CRANE.search(r.get("activity") or "") and MOLTEN.search(r.get("activity") or ""):
            needs.append(4)
        need = max(needs) if needs else None
        if need and cn and cn < need:
            f.add("SEVERITY_LOW", "warning", "Initial risk", f"Consequence CN {cn} looks under-rated",
                  f"'{r['hazard']}' during '{r.get('activity')}' can credibly cause serious or fatal injury "
                  f"(molten metal / heavy hot loads), yet CN is {cn}.",
                  f"Re-assess CN at {need} or higher; if the team keeps {cn}, record why.", row)

    for c in ctrl_rows:
        if c.get("snoInSheet") is not None and c["snoInSheet"] != entry_seq:
            f.add("STRATEGY_SNO", "warning", "Controls", "Controls tab uses the wrong S.No",
                  f"QSD/25/F row {c['sheetRow']} refers to S.No {c['snoInSheet']}, but its activity and hazard are "
                  f"S.No {entry_seq} in QSD/24/F.",
                  f"Correct the S.No in Controls row {c['sheetRow']} to {entry_seq}.", c["sheetRow"])

    if len(rows) > 1:
        lines = "; ".join(f"row {r['xl_row']} {r['hazard']} (GT {r['GT']})" for r in rows)
        f.add("MERGED_SNO", "info", "Sheet structure", f"S.No {entry_seq} holds {len(rows)} hazard ratings",
              f"{lines}. SafeOps rates the activity by the worst one.",
              "In the sheet, give each hazard row its own S.No so the Controls tab can refer to it unambiguously.")


# ── live entry checks ─────────────────────────────────────────────────────

async def _load(db: AsyncSession, entry_id: str) -> Any:
    from app.models.hira import HiraEntry, HiraEntryHazard

    stmt = (select(HiraEntry).where(HiraEntry.id == entry_id)
            .options(selectinload(HiraEntry.study), selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
                     selectinload(HiraEntry.existingControls), selectinload(HiraEntry.recommendedControls)))
    return (await db.execute(stmt)).scalar_one_or_none()


def _ideas(types: list[str], existing: list[Any]) -> list[dict[str, str]]:
    have = " ".join((c.description or "").lower() for c in existing)
    out, seen = [], set()
    for t in types:
        for idea in hira_kb.control_ideas(t):
            key = idea["description"]
            words = {w for w in re.findall(r"[a-z]{5,}", key.lower())}
            if key in seen or (words and sum(w in have for w in words) / len(words) > 0.5):
                continue
            seen.add(key)
            out.append({**idea, "hazardType": t, "hazardLabel": hira_kb.hazard_types().get(t, t)})
    order = {h: i for i, h in enumerate(form_drafts.HIERARCHY)}
    return sorted(out, key=lambda i: order.get(i["hierarchy"], 9))


async def review_entry(db: AsyncSession, entry_id: str) -> dict[str, Any]:
    from app.routers.hira import DEFAULT_ALARP_BANDS

    e = await _load(db, entry_id)
    if e is None:
        raise LookupError("HIRA entry not found")
    study = e.study
    f = _Findings()
    src = source_for(study.number if study else None, e.sequenceNumber)
    if src:
        _sheet_checks(f, src, e.sequenceNumber)

    matrix, cells, routine = await form_drafts.load_matrix(db, entry_id=e.id)
    threshold = ((matrix.acceptableResidual or {}) if matrix else {}).get((routine or "ROUTINE").lower())
    bands = (matrix.alarpBands if matrix else None) or DEFAULT_ALARP_BANDS
    controls = sorted(e.existingControls, key=lambda c: c.sortOrder or 0)
    ctl = [{"hierarchy": c.hierarchy, "description": c.description, "effectiveness": c.effectiveness} for c in controls]

    residual_block: dict[str, Any] = {"threshold": threshold}
    il, is_ = e.initialLikelihoodScore, e.initialSeverityScore
    if il and is_ and cells:
        initial = form_drafts.rate(cells, il, is_, threshold=threshold, bands=bands)
        model = form_drafts.assess_controls(cells=cells, initial_l=il, initial_s=is_, before_controls=ctl,
                                            after_controls=ctl, threshold=threshold, bands=bands,
                                            matrix_code=matrix.code if matrix else None)
        modelled = model["after"]
        stored = None
        if e.residualLikelihoodScore and e.residualSeverityScore:
            stored = form_drafts.rate(cells, e.residualLikelihoodScore, e.residualSeverityScore,
                                      threshold=threshold, bands=bands)
        residual_block |= {"initial": initial, "saved": stored, "fromControls": modelled,
                           "toImprove": model.get("toImprove"), "explanation": model.get("explanation")}

        if stored is None:
            f.add("RESIDUAL_MISSING", "error", "Residual risk", "No residual risk is recorded",
                  "The entry has an initial rating only.",
                  f"Set the residual to what the controls achieve: {_rating_text(modelled)} by the SafeOps model.")
        else:
            same = (stored["likelihood"], stored["severity"]) == (il, is_)
            no_gain = (modelled["likelihood"], modelled["severity"]) == (stored["likelihood"], stored["severity"])
            if same and controls and no_gain and stored["verdict"] == "ACCEPTABLE":
                f.add("RESIDUAL_EQUALS_INITIAL", "info", "Residual risk", "Initial and residual are the same rating",
                      f"{_rating_text(stored)} is acceptable either way; the listed controls would not move it on the matrix.",
                      "No change needed. To show the effect of the controls, rate the initial risk as it would be "
                      "with no controls in place.")
            elif same and controls:
                f.add("RESIDUAL_EQUALS_INITIAL", "warning", "Residual risk", "Residual is the same as the initial risk",
                      f"Residual {_rating_text(stored)} equals the initial rating although {len(controls)} control(s) "
                      "are listed. The source sheet has one rating only, so it does not show what the controls achieve.",
                      "Either raise the initial rating to the uncontrolled risk (what could happen with no controls) "
                      f"and keep this as residual, or lower the residual: with the listed controls the SafeOps model "
                      f"gives {_rating_text(modelled)} ({_verdict_text(modelled['verdict'])}).")
            elif (stored["score"], LEVELS.index(stored["level"])) < (modelled["score"], LEVELS.index(modelled["level"])):
                f.add("RESIDUAL_OPTIMISTIC", "warning", "Residual risk", "Residual looks lower than the controls justify",
                      f"Saved residual {_rating_text(stored)}; the listed controls support {_rating_text(modelled)}.",
                      "Add or verify controls that justify the lower rating, or raise the residual.")
            if stored["severity"] < is_ and not any(c["hierarchy"] in ("ELIMINATION", "SUBSTITUTION", "ENGINEERING", "PPE")
                                                    for c in ctl):
                f.add("SEVERITY_REDUCED_BY_ADMIN", "warning", "Residual risk",
                      "Severity reduced by procedures alone",
                      "Procedures and training lower likelihood, not severity.",
                      "Keep the residual severity at the initial value unless an elimination, substitution, "
                      "engineering or PPE control limits the harm.")
            if stored["verdict"] == "NOT_ACCEPTABLE":
                opts = (model.get("toImprove") or {}).get("options") or []
                best = next((o for o in opts if o.get("reachesAcceptable")), opts[0] if opts else None)
                f.add("RESIDUAL_NOT_ACCEPTABLE", "error", "Residual risk", "Residual risk is not acceptable",
                      f"{_rating_text(stored)} is above the matrix limit for {(routine or 'routine').lower().replace('_', '-')} "
                      f"work ({threshold or 'n/a'}).",
                      (f"Add a verified {best['addOneEffective'].lower()} control: it would give "
                       f"{_rating_text(best['wouldGive'])}." if best else "Add stronger controls (elimination / engineering).")
                      + " Record it as a recommended control with an owner and date.")
            elif stored["verdict"] == "TOLERABLE_NEEDS_ALARP" and e.alarpStatus not in ("DEMONSTRATED", "ACCEPTED"):
                f.add("ALARP_NOT_SHOWN", "warning", "Residual risk", "Residual is tolerable only if ALARP is shown",
                      f"{_rating_text(stored)} sits in the ALARP band and no ALARP justification is recorded.",
                      "Record which further controls were considered and why they are or are not reasonably practicable.")

            if src:
                rows = src["entry"]["rows"]
                sheet_ok = all(str(r.get("acceptable") or "").lower().startswith("y") for r in rows) and \
                    all(r.get("acceptableByRule") for r in rows)
                if not sheet_ok and stored["verdict"] == "ACCEPTABLE":
                    worst = max(rows, key=lambda r: r.get("GT") or 0)
                    pm = hira_kb.plant_method(worst["SP"] or 1, worst["LH"] or 1, worst["CN"] or 1, worst["DE"] or 1,
                                              {k: _yn(worst.get(k)) for k in ("LC", "DC", "CE", "EC")})
                    f.add("SHEET_VS_MATRIX", "warning", "Residual risk",
                          "The plant sheet says NOT acceptable, the risk matrix says acceptable",
                          f"The matrix rates the score only ({_rating_text(stored)}). The plant method also makes a risk "
                          f"significant when a qualitative concern is Y: {'; '.join(pm['reasons'])}.",
                          "Treat it as significant: keep the QSD/25/F strategy and add recommended controls until the "
                          "plant method is satisfied (no Y concern left unmanaged, GT <= 3.33).")

    strong = [c for c in ctl if c["hierarchy"] in STRONG]
    not_ok = (e.residualAcceptable is False) or any(x["code"] in ("RESIDUAL_NOT_ACCEPTABLE", "SHEET_VS_MATRIX")
                                                    for x in f.items)
    types = []
    for r in (src["entry"]["rows"] if src else []):
        for t in r.get("hazardTypes") or ([r["hazardType"]] if r.get("hazardType") else []):
            if t not in types:
                types.append(t)
    ideas = _ideas(types, controls) if types else []
    if not_ok and not strong:
        top = next((i for i in ideas if i["hierarchy"] in STRONG), None)
        f.add("ADMIN_PPE_ONLY", "warning", "Controls", "Only procedures and PPE control a significant risk",
              "PPE and SOPs are the last line of defence; nothing removes or isolates the hazard.",
              f"Add an engineering control, e.g. '{top['description']}'." if top else
              "Add an elimination, substitution or engineering control.")
    if not_ok and not e.recommendedControls:
        f.add("NO_RECOMMENDED", "warning", "Controls", "No recommended (further) controls",
              "The risk is not acceptable but no further control is planned.",
              "Add recommended controls with an owner and target date; they can raise a CAPA.")
    unverified = [c for c in ctl if c["effectiveness"] in (None, "NOT_VERIFIED")]
    if unverified:
        f.add("CONTROLS_UNVERIFIED", "suggestion", "Controls", f"{len(unverified)} control(s) not verified",
              "Unverified controls earn only part of their credit in the residual (not rated 60%, not verified 30%).",
              "Check each control on site and set its effectiveness.")
    vague = [c for c in ctl if re.search(r":\s*in place$", c["description"] or "", re.I)]
    if vague:
        f.add("VAGUE_CONTROL", "suggestion", "Controls", "Control described only as 'in place'",
              "; ".join(c["description"] for c in vague[:3]),
              "Say what is in place, e.g. the sign's wording and location, so it can be checked.")
    if "BURN_SCALD" in types or "FIRE" in types:
        ppe = " ".join((c["description"] or "").lower() for c in ctl if c["hierarchy"] == "PPE")
        if ppe and not re.search(r"alumin|face shield|melting|leg guard|fr ", ppe):
            f.add("PPE_NOT_SPECIFIC", "suggestion", "Controls", "PPE is not specific to molten-metal work",
                  f"Listed PPE: {ppe[:160]}.",
                  "Specify aluminised jacket/trousers, face shield with melting goggles and leg guards for splash exposure.")
    if e.nextReviewDue and e.nextReviewDue < datetime.utcnow():
        f.add("REVIEW_OVERDUE", "warning", "Review", "Review is overdue",
              f"Next review was due {e.nextReviewDue:%d %b %Y}.", "Schedule the periodic review.")

    items = f.sorted()
    counts = {k: sum(1 for x in items if x["level"] == k) for k in ORDER}
    headline = ("Looks consistent" if not counts["error"] and not counts["warning"] else
                f"{counts['error']} issue(s) to correct, {counts['warning']} to check")
    label = f"S.No {e.sequenceNumber}" if src else f"#{e.sequenceNumber}"
    return {
        "entryId": e.id,
        "studyId": e.studyId,
        "studyNumber": study.number if study else None,
        "label": label,
        "activity": e.activityDescription,
        "headline": headline,
        "counts": counts,
        "findings": items,
        "residual": residual_block,
        "controlIdeas": ideas[:6],
        "sheet": ({"rows": [{k: r.get(k) for k in ("xl_row", "hazard", "risk", "SP", "LH", "CN", "SV", "DE", "GT",
                                                   "acceptable", "legal")} | {"concerns": _concerns(r)}
                            for r in src["entry"]["rows"]],
                   "strategyRows": [c["sheetRow"] for c in src["entry"].get("controlRows") or []],
                   "form": "QSD/24/F + QSD/25/F"} if src else None),
        "note": "SafeOps suggestions from the plant's own rules and good practice; the assessor decides and saves.",
    }
