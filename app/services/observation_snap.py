"""Snap & Report ("picture gives everything") + the live observation dashboard.

Snap & Report: one photo → a fully prefilled observation the reporter only has
to confirm. The model is shown the photo AND the platform's real vocabularies
(observation types, the active DuPont STOP taxonomy per act/condition axis, the
storable hazard categories, the severity ladder). Whatever it answers is then
CLAMPED back onto those vocabularies here, so a hallucinated code can never
reach the observation create endpoint, which validates the same lists.

Severity follows policy: when the severity matrix has a rule for the suggested
STOP pair (and area tier) the card defaults to the matrix value, exactly as the
observer form does, and the model's own reading is shown as a hint. That keeps
the create endpoint's "justify an override" gate meaningful.

Location never comes from the model. The client supplies GPS, a scanned area QR
or the last-used area; `nearest_area` turns GPS into an area using the plant's
own geotagged observation photos (Area has no coordinates of its own).

The AI call follows AnthropicVisionProvider (app/services/ai/capture_providers.py):
same client, same image sniff/shrink, fail-soft to None. Model name, latency and
token usage are logged server-side only, never returned to the UI.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("safeops360.observation_snap")

OBSERVATION_TYPES = ("UNSAFE_ACT", "UNSAFE_CONDITION", "SAFE_ACT", "SAFE_CONDITION")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
DEFAULT_TYPE = "UNSAFE_CONDITION"
DEFAULT_SEVERITY = "MEDIUM"
NEAREST_AREA_MAX_METRES = 400.0
MIN_AT_RISK_DESCRIPTION = 50  # mirrors services/bbs_quality.MIN_AT_RISK_DESCRIPTION
# Hierarchy of controls, highest first: corrective actions are shown in this order.
CONTROL_LEVELS = ("ELIMINATION", "SUBSTITUTION", "ENGINEERING", "ADMINISTRATIVE", "PPE")
FATAL_LEVELS = ("YES", "POSSIBLE", "NO")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
# Plant practice closure targets (same rule the Copilot states).
CLOSURE_DAYS = {"CRITICAL": 0, "HIGH": 7, "MEDIUM": 14, "LOW": 30}
MAX_NOTE = 500


# ── vocabularies ─────────────────────────────────────────────────────────────
async def load_stop_rows(db: AsyncSession) -> list[dict[str, Any]]:
    """Active STOP taxonomy rows, ordered as the forms order them."""
    from app.models.observation import ObservationTaxonomy

    rows = (
        await db.execute(
            select(ObservationTaxonomy)
            .where(ObservationTaxonomy.isActive.is_(True))
            .order_by(ObservationTaxonomy.observationType, ObservationTaxonomy.displayOrder,
                      ObservationTaxonomy.categoryCode, ObservationTaxonomy.subCategoryCode)
        )
    ).scalars().all()
    return [
        {
            "axis": r.observationType, "categoryCode": r.categoryCode, "categoryLabel": r.categoryLabel,
            "subCategoryCode": r.subCategoryCode, "subCategoryLabel": r.subCategoryLabel,
            "stopReferenceCode": r.stopReferenceCode,
        }
        for r in rows
    ]


def storable_categories() -> list[str]:
    from app.models.observation import DB_OBSERVATION_CATEGORIES

    # STOP codes are written into `category` by the dual-write for at-risk
    # records; a SAFE observation picks a plain hazard category.
    stop = {"REACTIONS_OF_PEOPLE", "POSITIONS_OF_PEOPLE", "TOOLS_EQUIPMENT", "PROCEDURES"}
    return sorted(c for c in DB_OBSERVATION_CATEGORIES if c not in stop)


def _code(v: Any) -> str:
    if v is None or isinstance(v, (dict, list)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("null", "none") else s


def clamp_stop_pair(
    rows: list[dict[str, Any]], axis: str | None, category: Any, sub_category: Any,
    legacy_category: Any = None,
) -> tuple[str | None, str | None, bool]:
    """(categoryCode, subCategoryCode, clamped) — always a pair that exists and is
    active on `axis`, or (None, None, True) when the axis has no rows at all.

    Preference: the exact pair → the category's first sub-category → the
    legacy-category mapping → the axis fallback used by /capture conversion →
    the first row on the axis."""
    from app.services import observation_taxonomy as tax

    on_axis = [r for r in rows if r["axis"] == axis]
    if not on_axis:
        return None, None, True
    cat, sub = _code(category).upper(), _code(sub_category).upper()
    pairs = {(r["categoryCode"], r["subCategoryCode"]) for r in on_axis}
    if (cat, sub) in pairs:
        return cat, sub, False
    by_cat = [r for r in on_axis if r["categoryCode"] == cat]
    if by_cat:
        return by_cat[0]["categoryCode"], by_cat[0]["subCategoryCode"], True
    if legacy_category:
        lc, ls = tax.stop_pair_for_legacy_category(_code(legacy_category), axis or "CONDITION")
        if (lc, ls) in pairs:
            return lc, ls, True
    fc, fs = tax.HAZARD_FALLBACK.get(axis or "", (None, None))
    if (fc, fs) in pairs:
        return fc, fs, True
    return on_axis[0]["categoryCode"], on_axis[0]["subCategoryCode"], True


def _text(v: Any, limit: int) -> str:
    if v is None or isinstance(v, (dict, list)):
        return ""
    s = " ".join(str(v).split())
    return s[:limit]


def clamp_ai_output(raw: dict[str, Any] | None, stop_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Model JSON → a suggestion whose every coded field is a real vocabulary
    value. `clamped` lists the fields the model got wrong (logged, not shown)."""
    from app.services import observation_taxonomy as tax

    raw = raw if isinstance(raw, dict) else {}
    clamped: list[str] = []
    otype = _code(raw.get("observationType")).upper().replace(" ", "_")
    if otype not in OBSERVATION_TYPES:
        clamped.append("observationType")
        otype = DEFAULT_TYPE
    severity = _code(raw.get("severity")).upper()
    if severity not in SEVERITIES:
        clamped.append("severity")
        severity = DEFAULT_SEVERITY
    out: dict[str, Any] = {
        "observationType": otype, "severity": severity,
        "categoryCode": None, "subCategoryCode": None, "category": None,
    }
    if tax.requires_taxonomy(otype):
        cat, sub, was_clamped = clamp_stop_pair(
            stop_rows, tax.axis_for_type(otype), raw.get("categoryCode"), raw.get("subCategoryCode"),
            legacy_category=raw.get("hazardCategory"),
        )
        if was_clamped:
            clamped.append("categoryCode")
        out["categoryCode"], out["subCategoryCode"] = cat, sub
        out["category"] = tax.legacy_category_for(cat)
    else:
        hc = _code(raw.get("hazardCategory")).upper().replace(" ", "_").replace("-", "_")
        allowed = storable_categories()
        if hc not in allowed:
            clamped.append("hazardCategory")
            hc = "OTHERS"
        out["category"] = hc
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    fatal = _code(raw.get("fatalPotential")).upper()
    if fatal not in FATAL_LEVELS:
        fatal = "NO"
    raw_actions = raw.get("correctiveActions")
    raw_questions = raw.get("questions")
    actions: list[dict[str, str]] = []
    for a in raw_actions if isinstance(raw_actions, list) else []:
        text = _text(a.get("action") if isinstance(a, dict) else a, 300)
        if not text:
            continue
        level = _code(a.get("controlLevel") if isinstance(a, dict) else "").upper()
        actions.append({"action": text, "controlLevel": level if level in CONTROL_LEVELS else "ADMINISTRATIVE"})
    # Highest in the hierarchy first (eliminate/engineer out before instructions and PPE).
    actions.sort(key=lambda a: CONTROL_LEVELS.index(a["controlLevel"]))
    questions = [q for q in (_text(x, 200) for x in (raw_questions if isinstance(raw_questions, list) else [])) if q]
    out.update(
        title=_text(raw.get("title"), 120),
        description=_text(raw.get("description"), 1500),
        likelyHazard=_text(raw.get("likelyHazard"), 200),
        immediateAction=_text(raw.get("immediateAction"), 500),
        photoFindings=_text(raw.get("photoFindings"), 1500),
        consequence=_text(raw.get("consequence"), 500),
        severityReason=_text(raw.get("severityReason"), 300),
        fatalPotential=fatal,
        fatalPotentialQuestion=_text(raw.get("fatalPotentialQuestion"), 300),
        correctiveActions=actions[:3],
        responsibleFunction=_text(raw.get("responsibleFunction"), 80),
        questions=questions[:3],
        reply=_text(raw.get("reply"), 500),
        confidence=confidence,
        clamped=clamped,
    )
    return out


def compose_description(title: str, description: str, likely_hazard: str) -> str:
    """The text that goes into Observation.description: headline, what is
    visible, and the hazard, without repeating the headline."""
    parts: list[str] = []
    t, d, h = title.strip().rstrip("."), description.strip(), likely_hazard.strip().rstrip(".")
    if t and t.lower() not in d.lower():
        parts.append(f"{t}.")
    if d:
        parts.append(d)
    if h and h.lower() not in d.lower():
        parts.append(f"Likely hazard: {h}.")
    return " ".join(parts).strip()


# ── AI call ──────────────────────────────────────────────────────────────────
# The same depth of reading the SafeOps Copilot gives a photo: what is visible
# (with "(to confirm)" where unsure), the likely classification, severity with a
# reason, a fatal-potential check, 2-3 corrective actions highest in the
# hierarchy first, the responsible function, and what the reporter still needs
# to tell us. Grounding from the reference register (similar cases, plant
# practice severity, closure target) is added afterwards by build_analysis.
_SNAP_SYSTEM = """You are the SafeOps safety-observation assistant at a stainless steel plant in India \
(melt shop, hot and cold rolling mills, cranes, furnaces, acid pickling, grinding, material yards).
A worker has taken a photo to report a safety observation. Read the photo the way an experienced \
safety officer would, classify it using ONLY the codes in the vocabulary you are given, and write the report.
Rules:
- Describe only what is visible: the equipment, the act or condition, people's PPE and position. Never guess \
names, roles, the plant area, what happened before or after, causes or injuries. Mark anything you are unsure \
of with "(to confirm)". Never take the location from the photo.
- The reporter's note (if any) is extra context from the person who was there: trust it over your reading of \
the photo, and use their words for where it was.
- observationType: UNSAFE_ACT (a person doing something unsafe), UNSAFE_CONDITION (an unsafe physical state), \
SAFE_ACT / SAFE_CONDITION (good practice worth recognising).
- For UNSAFE_* pick categoryCode + subCategoryCode from stop[ACT] or stop[CONDITION] (matching the type's axis). \
For SAFE_* pick hazardCategory from hazardCategories.
- severity: the realistic worst credible outcome if nothing changes. fatalPotential: YES when a fatality is a \
credible outcome from what is visible, POSSIBLE when one fact you cannot see decides it (then ask that one \
question in fatalPotentialQuestion), else NO.
- photoFindings: 3-6 plain sentences, "What the photo shows", specific and factual.
- description: the observation text for the register, 2-3 factual sentences, at least 60 characters.
- consequence: one sentence on what could happen.
- correctiveActions: 2-3 actions specific to THIS hazard, highest in the hierarchy first (eliminate or engineer \
out before procedures and PPE); controlLevel one of ELIMINATION, SUBSTITUTION, ENGINEERING, ADMINISTRATIVE, PPE.
- immediateAction: the one thing a supervisor should do right now, imperative.
- responsibleFunction: the function that should own the fix (e.g. Mechanical, Electrical, Operation, Safety, \
the contractor's supervisor).
- questions: up to 3 short questions for the reporter whose answers would change the classification or severity.
Respond ONLY with JSON:
{"photoFindings": "...", "observationType": "...", "categoryCode": "...", "subCategoryCode": "...",
 "hazardCategory": "...", "severity": "...", "severityReason": "<one sentence>",
 "fatalPotential": "YES|POSSIBLE|NO", "fatalPotentialQuestion": "...",
 "title": "<max 80 chars>", "description": "...", "likelyHazard": "<short phrase>", "consequence": "...",
 "correctiveActions": [{"action": "...", "controlLevel": "..."}], "immediateAction": "...",
 "responsibleFunction": "...", "questions": ["..."], "confidence": <0..1>}"""

_REFINE_RULES = """
REFINE MODE: the reporter has already reviewed your reading. `currentDraft` is the observation as it stands \
now (it may include their own edits) and `instruction` is what they want changed. Apply the instruction and \
change ONLY what it asks for or what it logically implies; keep every other field exactly as in currentDraft. \
If they give a fact (e.g. "the cable insulation is damaged", "this is at CRD AP2"), update the reading, the \
classification and severity if that fact changes them, and drop questions it answers. If they ask for a \
different language, write the text fields in that language. If they ask for a severity below the photo's \
worst credible outcome, apply it but give the reason in severityReason. Add "reply": 1-2 sentences telling the \
reporter what you changed and why. Return the FULL JSON object, not just the changed fields."""


def vocabulary_for_prompt(stop_rows: list[dict[str, Any]]) -> dict[str, Any]:
    stop: dict[str, list[dict[str, Any]]] = {}
    for axis in ("ACT", "CONDITION"):
        cats: dict[str, dict[str, Any]] = {}
        for r in stop_rows:
            if r["axis"] != axis:
                continue
            c = cats.setdefault(r["categoryCode"], {"code": r["categoryCode"], "label": r["categoryLabel"], "subs": []})
            c["subs"].append({"code": r["subCategoryCode"], "label": r["subCategoryLabel"]})
        stop[axis] = list(cats.values())
    return {
        "observationTypes": list(OBSERVATION_TYPES), "severities": list(SEVERITIES),
        "hazardCategories": storable_categories(), "stop": stop,
    }


def build_user_text(stop_rows: list[dict[str, Any]], *, lang: str = "en", note: str | None = None,
                    refine: dict[str, Any] | None = None) -> str:
    """The text block sent alongside the photo (pure; tested)."""
    parts = ["Vocabulary:\n" + json.dumps(vocabulary_for_prompt(stop_rows), ensure_ascii=False)]
    note = _text(note, MAX_NOTE)
    if note:
        parts.append("Reporter's note: " + note)
    if refine:
        history = [
            {"role": h.get("role"), "text": _text(h.get("text"), 500)}
            for h in (refine.get("history") or [])[-8:] if isinstance(h, dict) and h.get("text")
        ]
        parts.append(_REFINE_RULES)
        parts.append("currentDraft: " + json.dumps(refine.get("current") or {}, ensure_ascii=False)[:6000])
        if history:
            parts.append("Earlier changes in this report: " + json.dumps(history, ensure_ascii=False))
        parts.append("instruction: " + _text(refine.get("instruction"), MAX_NOTE))
    parts.append(f"Write all text fields in language code: {lang} unless the instruction asks otherwise.")
    return "\n".join(parts)


async def analyze_photo(
    image: bytes, stop_rows: list[dict[str, Any]], *, lang: str = "en", note: str | None = None,
    refine: dict[str, Any] | None = None, timeout: float = 20.0,
) -> dict[str, Any] | None:
    """Photo (+ optional reporter note, or a refine instruction over the current
    draft) → raw model JSON, or None on any failure / when AI isn't configured.

    Capped at `timeout` seconds: the Next.js proxy gives up at ~26 s."""
    from app.core.config import get_settings
    from app.services.ai.anthropic_client import _get_client
    from app.services.ai.capture_providers import _VISION_MAX_RAW_BYTES, _VISION_MEDIA_TYPES, _shrink_image, _sniff_image_type

    client = _get_client()
    if client is None:
        return None
    media_type = _sniff_image_type(image)
    if media_type is None or len(image) > _VISION_MAX_RAW_BYTES:
        shrunk = _shrink_image(image)
        if shrunk is None:
            return None
        image, media_type = shrunk, "image/jpeg"
    if media_type not in _VISION_MEDIA_TYPES:
        return None
    model = get_settings().anthropic_model
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image).decode()}},
        {"type": "text", "text": build_user_text(stop_rows, lang=lang, note=note, refine=refine)},
    ]
    t0 = time.monotonic()
    try:
        create = client.with_options(timeout=timeout, max_retries=0).messages.create
        msg = await asyncio.to_thread(
            create, model=model, max_tokens=1600, system=_SNAP_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("snap-analyze call failed (model=%s refine=%s): %s", model, bool(refine), e)
        return None
    usage = getattr(msg, "usage", None)
    log.info(
        "snap-analyze model=%s refine=%s latency_ms=%d input_tokens=%s output_tokens=%s",
        model, bool(refine), int((time.monotonic() - t0) * 1000),
        getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
    )
    raw = "".join(getattr(b, "text", "") or "" for b in (msg.content or [])).strip()
    try:
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001
        log.warning("snap-analyze returned unparseable output")
        return None
    return data if isinstance(data, dict) else None


# ── grounding + the analysis card ────────────────────────────────────────────
def reference_grounding(description: str, observation_type: str | None) -> dict[str, Any] | None:
    """Similar observations in the reviewed reference register (the same
    knowledge the Copilot's suggest_observation_fields reads). Offline JSON,
    no DB. None when unavailable; the card simply omits those lines."""
    text = (description or "").strip()
    if len(text) < 8:
        return None
    try:
        from app.services.copilot.knowledge import observations as obs_kb

        kb = obs_kb.suggest(text, observation_type=observation_type)
    except Exception as e:  # noqa: BLE001
        log.info("snap grounding unavailable: %s", e)
        return None
    return kb if isinstance(kb, dict) and kb.get("available") else None


def _max_sev(*values: str | None) -> str | None:
    vals = [v for v in values if v in SEVERITY_RANK]
    return max(vals, key=lambda v: SEVERITY_RANK[v]) if vals else None


def choose_severity(*, matrix: str | None, plant_practice: str | None, ai: str, fatal: str,
                    directed: bool = False) -> str:
    """Severity policy (same as the Copilot): the higher of the SafeOps matrix and
    the plant-practice severity for similar cases; never below the matrix on our
    own. A clear fatal potential is CRITICAL. In refine mode the reporter's
    instruction is honoured (the form then asks them for the override reason)."""
    if directed:
        return ai
    if fatal == "YES":
        return "CRITICAL"
    return _max_sev(matrix, plant_practice) or ai


def _closure_label(days: int) -> str:
    if days == 0:
        return "same day"
    if days == 14:
        return "about 2 weeks"
    return f"about {days} days"


def build_analysis(
    s: dict[str, Any], *, kb: dict[str, Any] | None, matrix: str | None, matrix_rationale: str | None,
    labels: dict[str, str], area_known: bool, directed: bool = False,
) -> dict[str, Any]:
    """Clamped model output + reference grounding → the Copilot-style card and
    the final severity. Pure (tested)."""
    risk = (kb or {}).get("risk") or {}
    plant_practice = risk.get("safeopsSeverity") if risk.get("safeopsSeverity") in SEVERITY_RANK else None
    final = choose_severity(matrix=matrix, plant_practice=plant_practice, ai=s["severity"],
                            fatal=s["fatalPotential"], directed=directed)

    reasons: list[str] = []
    if matrix:
        reasons.append(f"SafeOps matrix rating: {matrix.title()}.")
    if plant_practice:
        mix = risk.get("mix") or {}
        mix_txt = ", ".join(f"{k} {round(v * 100)}%" for k, v in mix.items() if isinstance(v, (int, float)))
        reasons.append(f"Plant practice for similar cases: {plant_practice.title()}" + (f" ({mix_txt})." if mix_txt else "."))
    if s["fatalPotential"] == "YES":
        reasons.append("Fatal potential is visible in the photo, so it is rated Critical and should close the same day.")
    if s.get("severityReason"):
        reasons.append(s["severityReason"])

    needs_reason = bool(matrix) and final != matrix
    override_reason = ""
    if needs_reason:
        if s.get("severityReason"):
            override_reason = s["severityReason"]
        elif s["fatalPotential"] == "YES":
            override_reason = "Fatal potential visible in the photo."
        elif plant_practice and final == plant_practice:
            override_reason = f"Plant practice rates similar cases {plant_practice.title()}."

    why = None
    if kb:
        cat = kb.get("category") or {}
        alts = kb.get("alternatives") or []
        bits = [f"{kb.get('matches')} similar observations in the reference register"]
        if cat.get("label"):
            bits.append(f"mostly {cat['label']} ({round((cat.get('share') or 0) * 100)}%)")
        if alts:
            bits.append("then " + ", ".join(f"{a.get('label')} ({round((a.get('share') or 0) * 100)}%)" for a in alts[:2]))
        why = "; ".join(bits) + "."

    actions = [{**a, "source": "PHOTO"} for a in s["correctiveActions"]]
    seen = {a["action"].lower() for a in actions}
    for a in (kb or {}).get("correctiveActions") or []:
        text = _text(a.get("action") if isinstance(a, dict) else None, 300)
        if text and text.lower() not in seen and len(actions) < 5:
            level = _code(a.get("controlLevel")).upper()
            actions.append({"action": text, "controlLevel": level if level in CONTROL_LEVELS else "ADMINISTRATIVE",
                            "source": "REFERENCE", "ref": a.get("from")})
            seen.add(text.lower())
    actions.sort(key=lambda a: (a["source"] != "PHOTO", CONTROL_LEVELS.index(a["controlLevel"])))

    fatal_q = s.get("fatalPotentialQuestion") or ""
    if not fatal_q and (kb or {}).get("fatalPotentialSignals") and s["fatalPotential"] != "YES":
        fatal_q = "Could this realistically kill someone (energy, height, molten metal or a confined space within reach)?"
    questions = list(s["questions"])
    if not area_known:
        questions.insert(0, "Where exactly was this (area, bay or equipment)? Pick the area below or tell me.")

    days = 0 if (s["fatalPotential"] == "YES" or final == "CRITICAL") else CLOSURE_DAYS.get(final, 14)
    return {
        "severity": final,
        "needsReason": needs_reason,
        "overrideReason": override_reason,
        "analysis": {
            "photoFindings": s.get("photoFindings") or s.get("description"),
            "classification": {
                "observationType": s["observationType"], "categoryLabel": labels.get("categoryLabel"),
                "subCategoryLabel": labels.get("subCategoryLabel"), "category": s.get("category"), "why": why,
            },
            "severity": {"final": final, "matrix": matrix, "matrixRationale": matrix_rationale,
                         "plantPractice": plant_practice, "photo": s["severity"], "reasons": reasons},
            "fatalPotential": {"level": s["fatalPotential"], "question": fatal_q or None},
            "consequence": s.get("consequence") or None,
            "correctiveActions": actions,
            "responsibleFunction": s.get("responsibleFunction") or (kb or {}).get("responsibleFunction"),
            "closureTargetDays": days,
            "closureTarget": _closure_label(days),
            "questions": questions[:4],
            "similarCases": (kb or {}).get("matches"),
            "reply": s.get("reply") or None,
        },
    }


# ── location ─────────────────────────────────────────────────────────────────
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def area_centroids(points: list[tuple[str, float, float]]) -> dict[str, tuple[float, float, int]]:
    """[(areaId, lat, lng)] → {areaId: (lat, lng, n)} — the mean of each area's
    geotagged photos. Plant areas are a few hundred metres across, so a plain
    mean is accurate enough."""
    acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    for area_id, lat, lng in points:
        a = acc[area_id]
        a[0] += lat
        a[1] += lng
        a[2] += 1
    return {k: (v[0] / v[2], v[1] / v[2], int(v[2])) for k, v in acc.items() if v[2]}


def nearest_area(centroids: dict[str, tuple[float, float, int]], lat: float, lng: float,
                 max_metres: float = NEAREST_AREA_MAX_METRES) -> tuple[str, float] | None:
    best: tuple[str, float] | None = None
    for area_id, (alat, alng, _n) in centroids.items():
        d = haversine_m(lat, lng, alat, alng)
        if d <= max_metres and (best is None or d < best[1]):
            best = (area_id, d)
    return best


def _gps_of(exif: Any) -> tuple[float, float] | None:
    if not isinstance(exif, dict):
        return None
    gps = exif.get("gps")
    if not isinstance(gps, dict):
        return None
    try:
        lat, lng = float(gps.get("lat")), float(gps.get("lng"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return lat, lng


async def geotag_points(db: AsyncSession, plant_id: str, limit: int = 2000) -> list[tuple[str, float, float]]:
    """(areaId, lat, lng) from the plant's geotagged observation photos."""
    from app.models.observation import Observation, ObservationAttachment

    rows = (
        await db.execute(
            select(Observation.areaId, ObservationAttachment.exifData)
            .join(ObservationAttachment, ObservationAttachment.observationId == Observation.id)
            .where(Observation.plantId == plant_id)
            .where(Observation.areaId.isnot(None))
            .where(ObservationAttachment.deletedAt.is_(None))
            .where(ObservationAttachment.exifData.isnot(None))
            .order_by(ObservationAttachment.uploadedAt.desc())
            .limit(limit)
        )
    ).all()
    out: list[tuple[str, float, float]] = []
    for area_id, exif in rows:
        g = _gps_of(exif)
        if g:
            out.append((area_id, g[0], g[1]))
    return out


# ── live dashboard ───────────────────────────────────────────────────────────
OPEN_STATUSES = ("OPEN", "ASSIGNED", "IN_PROGRESS")


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def day_bounds(now: datetime, tz_offset_minutes: int) -> dict[str, datetime]:
    """Start of today / this week (Monday) / this month in the plant's local
    time, returned as UTC instants."""
    tz = timezone(timedelta(minutes=tz_offset_minutes))
    local = now.astimezone(tz)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    return {k: v.astimezone(timezone.utc) for k, v in (("today", today), ("week", week), ("month", month))}


def pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 1) if den else None


async def live_dashboard(
    db: AsyncSession, *, plant_ids: list[str] | None, now: datetime | None = None,
    tz_offset_minutes: int = 330, window_days: int = 30,
) -> dict[str, Any]:
    """One plant-scoped aggregate for the Live tab. `plant_ids` None = every
    plant (caller has ALL_PLANTS); [] = nothing visible."""
    from app.models.observation import Observation, ObservationAttachment, ObservationTaxonomy
    from app.models.plant import Area, Plant
    from app.models.user import User

    now = now or datetime.now(timezone.utc)
    b = day_bounds(now, tz_offset_minutes)
    tz = timezone(timedelta(minutes=tz_offset_minutes))
    window_start = (now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
                    - timedelta(days=window_days - 1)).astimezone(timezone.utc)
    earliest = min(window_start, b["month"], b["week"])

    def scoped(stmt):
        if plant_ids is None:
            return stmt
        return stmt.where(Observation.plantId.in_(plant_ids or ["__none__"]))

    # Window rows: everything reported since the earliest bound we need. Pulled
    # as narrow tuples and folded in Python, so one query serves every panel.
    rows = (
        await db.execute(
            scoped(
                select(
                    Observation.id, Observation.createdAt, Observation.type, Observation.category,
                    Observation.categoryCode, Observation.severity, Observation.status, Observation.areaId,
                    Observation.observerId, Observation.closedAt, Observation.targetDate, Observation.plantId,
                ).where(Observation.createdAt >= earliest)
            )
        )
    ).all()

    # Backlog figures are all-time, not windowed: an observation opened in
    # March and still open today is today's problem.
    backlog = (
        await db.execute(
            scoped(
                select(Observation.status, func.count()).group_by(Observation.status)
            )
        )
    ).all()
    status_counts = {str(getattr(s, "value", s)): int(n) for s, n in backlog}
    open_total = sum(n for s, n in status_counts.items() if s in OPEN_STATUSES)
    overdue = int(
        await db.scalar(
            scoped(
                select(func.count()).select_from(Observation)
                .where(Observation.status.in_(OPEN_STATUSES))
                .where(Observation.targetDate.isnot(None))
                .where(Observation.targetDate < now)
            )
        ) or 0
    )
    # Closures in the window (by closedAt, not createdAt) for the trend line.
    closed_rows = (
        await db.execute(
            scoped(
                select(Observation.closedAt, Observation.targetDate)
                .where(Observation.closedAt.isnot(None))
                .where(Observation.closedAt >= window_start)
            )
        )
    ).all()

    def v(x):
        return str(getattr(x, "value", x)) if x is not None else None

    in_window = [r for r in rows if _aware(r.createdAt) >= window_start]
    today = sum(1 for r in rows if _aware(r.createdAt) >= b["today"])
    week = sum(1 for r in rows if _aware(r.createdAt) >= b["week"])
    mtd = sum(1 for r in rows if _aware(r.createdAt) >= b["month"])
    closed_in_window = sum(1 for r in in_window if v(r.status) == "CLOSED")
    on_time = sum(
        1 for c, t in closed_rows if t is not None and _aware(c) is not None and _aware(c) <= _aware(t) + timedelta(days=1)
    )
    with_target = sum(1 for _c, t in closed_rows if t is not None)

    by_type = Counter(v(r.type) for r in in_window)
    by_severity = Counter(v(r.severity) for r in in_window)
    by_cat = Counter((r.categoryCode or v(r.category) or "UNCLASSIFIED") for r in in_window)
    by_area = Counter(r.areaId for r in in_window)
    by_observer = Counter(r.observerId for r in in_window)

    # names
    area_ids = [a for a in by_area if a]
    area_names = dict((await db.execute(select(Area.id, Area.name).where(Area.id.in_(area_ids or ["__none__"])))).all())
    observer_ids = [o for o in by_observer if o]
    users = (
        await db.execute(select(User.id, User.name, User.department).where(User.id.in_(observer_ids or ["__none__"])))
    ).all()
    user_name = {u.id: u.name for u in users}
    user_dept = {u.id: (u.department or "").strip() or None for u in users}
    by_dept: Counter[str] = Counter()
    for oid, n in by_observer.items():
        by_dept[user_dept.get(oid) or "Not recorded"] += n
    cat_labels = dict(
        (await db.execute(select(ObservationTaxonomy.categoryCode, ObservationTaxonomy.categoryLabel).distinct())).all()
    )

    # trend (local days)
    trend_idx: dict[str, dict[str, Any]] = {}
    for i in range(window_days):
        d = (window_start.astimezone(tz) + timedelta(days=i)).date().isoformat()
        trend_idx[d] = {"date": d, "reported": 0, "closed": 0, "unsafe": 0}
    for r in in_window:
        d = _aware(r.createdAt).astimezone(tz).date().isoformat()
        if d in trend_idx:
            trend_idx[d]["reported"] += 1
            if v(r.type) in ("UNSAFE_ACT", "UNSAFE_CONDITION"):
                trend_idx[d]["unsafe"] += 1
    for c, _t in closed_rows:
        d = _aware(c).astimezone(tz).date().isoformat()
        if d in trend_idx:
            trend_idx[d]["closed"] += 1

    # latest 10 with a photo id for the thumbnail
    latest = (
        await db.execute(
            scoped(
                select(Observation).order_by(Observation.createdAt.desc(), Observation.id.desc()).limit(10)
            )
        )
    ).scalars().all()
    latest_ids = [o.id for o in latest]
    photos: dict[str, str] = {}
    if latest_ids:
        for oid, aid in (
            await db.execute(
                select(ObservationAttachment.observationId, ObservationAttachment.id)
                .where(ObservationAttachment.observationId.in_(latest_ids))
                .where(ObservationAttachment.deletedAt.is_(None))
                .where(ObservationAttachment.mimeType.like("image/%"))
                .order_by(ObservationAttachment.uploadedAt.asc())
            )
        ).all():
            photos.setdefault(oid, aid)
    extra_users = [o.observerId for o in latest if o.observerId not in user_name]
    if extra_users:
        for uid, name in (await db.execute(select(User.id, User.name).where(User.id.in_(extra_users)))).all():
            user_name[uid] = name
    extra_areas = [o.areaId for o in latest if o.areaId and o.areaId not in area_names]
    if extra_areas:
        area_names.update(dict((await db.execute(select(Area.id, Area.name).where(Area.id.in_(extra_areas)))).all()))
    plant_names = dict(
        (await db.execute(select(Plant.id, Plant.name).where(Plant.id.in_([o.plantId for o in latest] or ["__none__"])))).all()
    )

    def top(counter: Counter, label_of, limit: int = 10) -> list[dict[str, Any]]:
        return [{"key": k, "label": label_of(k), "count": n} for k, n in counter.most_common(limit)]

    unsafe_act = by_type.get("UNSAFE_ACT", 0)
    unsafe_cond = by_type.get("UNSAFE_CONDITION", 0)
    return {
        "generatedAt": now.isoformat(),
        "windowDays": window_days,
        "window": {"start": window_start.isoformat(), "end": now.isoformat()},
        "counts": {"today": today, "week": week, "mtd": mtd, "window": len(in_window)},
        "backlog": {
            "open": open_total, "closed": status_counts.get("CLOSED", 0), "overdue": overdue,
            "byStatus": status_counts,
        },
        "closure": {
            "closedInWindow": closed_in_window, "reportedInWindow": len(in_window),
            "closureRate": pct(closed_in_window, len(in_window)),
            "closedByDateInWindow": len(closed_rows), "onTimeRate": pct(on_time, with_target),
        },
        "actVsCondition": {
            "unsafeAct": unsafe_act, "unsafeCondition": unsafe_cond,
            "safeAct": by_type.get("SAFE_ACT", 0), "safeCondition": by_type.get("SAFE_CONDITION", 0),
        },
        "byType": top(by_type, lambda k: (k or "").replace("_", " ").title(), 4),
        "bySeverity": [{"key": s, "label": s.title(), "count": by_severity.get(s, 0)} for s in SEVERITIES],
        "byCategory": top(by_cat, lambda k: cat_labels.get(k) or (k or "").replace("_", " ").title()),
        "byArea": top(by_area, lambda k: area_names.get(k) or "Area not recorded"),
        "byDepartment": top(by_dept, lambda k: k),
        "topReporters": top(by_observer, lambda k: user_name.get(k) or "Unknown", 8),
        "trend": list(trend_idx.values()),
        "latest": [
            {
                "id": o.id, "number": o.number, "createdAt": _aware(o.createdAt).isoformat() if o.createdAt else None,
                "type": v(o.type), "severity": v(o.severity), "status": v(o.status),
                "categoryCode": o.categoryCode or v(o.category),
                "categoryLabel": cat_labels.get(o.categoryCode or "") or (v(o.category) or "").replace("_", " ").title(),
                "description": (o.description or "")[:160], "areaName": area_names.get(o.areaId or ""),
                "plantName": plant_names.get(o.plantId), "observerName": user_name.get(o.observerId),
                "photoAttachmentId": photos.get(o.id),
            }
            for o in latest
        ],
    }


__all__ = [
    "OBSERVATION_TYPES", "SEVERITIES", "analyze_photo", "area_centroids", "clamp_ai_output", "clamp_stop_pair",
    "compose_description", "day_bounds", "geotag_points", "haversine_m", "live_dashboard", "load_stop_rows",
    "nearest_area", "storable_categories", "vocabulary_for_prompt",
]
