"""Scan a paper permit-to-work into a draft for the New Permit wizard.

Flow: 1-4 photos (JPEG/PNG/WebP) or one PDF arrive as base64 JSON (the
Vercel proxy text-decodes request bodies, so binary multipart cannot transit
it; see routers/capture.py). Photos go to Claude vision as image blocks; a PDF
goes as a `document` block, and if that call fails the PDF's text layer is
extracted with pypdf and sent as plain text instead.

The model only TRANSCRIBES: a strict prompt asks for each field as printed,
with a confidence and an explicit "unreadable" list. Everything after that is
deterministic and unit-tested (tests/test_ptw_cc.py):

  normalise_extraction()   model JSON → clean values (enum codes, dates, PPE codes)
  match_name()             free text → master row (area, department, contractor, user)
  build_draft()            values + matches → PtwDraft patch (the wizard's own shape,
                           validated field by field against the copilot's PtwDraftPatch)

Never auto-applied: the browser shows the scan beside the fields for review
and the user presses "Continue to permit form". The precaution checklist is
deliberately not extracted (same rule as the copilot: only someone on site can
attest a control is in place).

Model name, token counts and latency are logged server-side only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.capture_providers import _shrink_image, _sniff_image_type
from app.services.copilot.ptw_draft import PPE_CATALOG, PtwDraftPatch  # read-only import

log = logging.getLogger("safeops360.ptw_scan")

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 12 * 1024 * 1024     # raw upload cap; >3.75 MB is re-encoded smaller
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_TOTAL_BYTES = 24 * 1024 * 1024
VISION_MAX_RAW = 3_750_000             # API cap is 5 MB base64
CALL_TIMEOUT_S = 20.0                  # the browser proxy gives up at 26 s
TEXT_FALLBACK_MIN_LEFT_S = 6.0
MAX_TOKENS = 3000
LOW_CONFIDENCE = 0.7

PERMIT_TYPES = ("HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "EXCAVATION",
                "ELECTRICAL_LOTO", "LIFTING", "GENERAL_COLD")
HAZARDS = ("HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "FRAGILE_ROOF", "EXCAVATION",
           "ELECTRICAL_LOTO", "LIFTING", "CIVIL", "GENERAL")
CREW_ROLES = ("OPERATOR", "HELPER", "SUPERVISOR", "TECHNICIAN", "WORKER", "CONTRACTOR")
ISOLATION_TYPES = ("ELECTRICAL", "MECHANICAL", "FLUID", "PNEUMATIC", "HYDRAULIC", "STEAM")

SCALAR_FIELDS: dict[str, str] = {
    "permitType": "Permit type",
    "paperPermitNumber": "Paper permit number",
    "plant": "Plant",
    "department": "Department",
    "area": "Area",
    "specificLocation": "Exact location",
    "scopeOfWork": "Scope of work",
    "validFrom": "Valid from",
    "validTo": "Valid to",
    "workOrderNumber": "Work order number",
    "contractor": "Contractor",
    "issuerName": "Issuer",
    "receiverName": "Receiver",
    "fireWatchName": "Fire watch",
    "standbyName": "Standby person",
    "rescuePlan": "Rescue plan",
    "weatherConditions": "Weather",
    "windSpeedKmh": "Wind speed (km/h)",
}

EXTRACTION_SYSTEM = f"""You transcribe paper Permit-to-Work (PTW) forms from an Indian steel plant into JSON.
You are shown photos or a PDF of ONE permit (front, back, annexures). Read printed text, ticks and handwriting.

Respond with ONLY one JSON object, no prose, shaped exactly like this:
{{
  "isPermit": true,
  "fields": {{
    "<key>": {{"value": <value or null>, "text": "<exactly what is written, or null>", "confidence": <0..1>}}
  }},
  "extraHazards": {{"value": [<codes>], "confidence": <0..1>}},
  "crew": [{{"name": "...", "role": "<{'|'.join(CREW_ROLES)}>", "confidence": <0..1>}}],
  "isolations": [{{"isolationType": "<{'|'.join(ISOLATION_TYPES)} or null>", "description": "...", "isolationPointTag": "<tag or null>", "lotoTagNumber": "<or null>", "confidence": <0..1>}}],
  "ppe": {{"value": ["<item as written>", ...], "confidence": <0..1>}},
  "gasReadings": [{{"parameter": "O2|LEL|H2S|CO|<other>", "value": <number>, "unit": "%|ppm", "confidence": <0..1>}}],
  "unreadable": ["<field key>", ...],
  "notes": "<one short sentence, or null>"
}}

Field keys (use these exactly): {", ".join(SCALAR_FIELDS)}.
- permitType value: one of {", ".join(PERMIT_TYPES)} (the main ticked/printed permit type). Cold work / general work = GENERAL_COLD.
- extraHazards: other hazard boxes ticked besides the main type, codes from {", ".join(HAZARDS)}.
- validFrom / validTo value: "YYYY-MM-DDTHH:MM" in the local time written on the form. Dates on these
  forms are day-first (DD/MM/YYYY). If the year is missing or illegible use null.
- windSpeedKmh value: a number. Everything else: a string.
- issuerName / receiverName / fireWatchName / standbyName / crew names: the person's name only (no designation).

Rules:
- Transcribe only what is on the paper. Never invent, infer or complete a value.
- A blank box is value null with confidence 0 and is NOT unreadable.
- "unreadable" lists keys where something IS written but you cannot read it reliably.
- confidence: 0.9+ printed or clearly written; 0.6-0.9 handwriting you can read with some doubt; below 0.6 a guess.
- Do not answer the precaution checklist (Yes/No/NA); ignore it.
- If the image is not a permit-to-work form at all, respond {{"isPermit": false}}."""


class ScanError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ── input handling ───────────────────────────────────────────────────────────
@dataclass
class ScanFile:
    name: str
    media_type: str
    data: bytes


def decode_files(files: list[dict[str, Any]]) -> list[ScanFile]:
    """base64 JSON → sniffed files. Accepts 1-4 images, or exactly one PDF."""
    if not files:
        raise ScanError("Attach a photo or PDF of the paper permit.")
    out: list[ScanFile] = []
    total = 0
    for f in files:
        try:
            raw = base64.b64decode(str(f.get("dataB64") or ""), validate=True)
        except (binascii.Error, ValueError) as e:
            raise ScanError(f"{f.get('name') or 'A file'} could not be decoded.") from e
        if not raw:
            raise ScanError(f"{f.get('name') or 'A file'} is empty.")
        total += len(raw)
        name = str(f.get("name") or "scan")[:120]
        if raw[:5] == b"%PDF-":
            if len(raw) > MAX_PDF_BYTES:
                raise ScanError(f"{name} is larger than {MAX_PDF_BYTES // 1024 // 1024} MB.")
            out.append(ScanFile(name, "application/pdf", raw))
            continue
        mt = _sniff_image_type(raw)
        if mt not in ("image/jpeg", "image/png", "image/webp"):
            raise ScanError(f"{name} is not a JPEG, PNG, WebP or PDF file.")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ScanError(f"{name} is larger than {MAX_IMAGE_BYTES // 1024 // 1024} MB.")
        out.append(ScanFile(name, mt, raw))
    pdfs = [f for f in out if f.media_type == "application/pdf"]
    if pdfs and len(out) > 1:
        raise ScanError("Send either one PDF or up to four photos, not both.")
    if len(out) > MAX_IMAGES:
        raise ScanError(f"Send at most {MAX_IMAGES} photos of one permit.")
    if total > MAX_TOTAL_BYTES:
        raise ScanError("The files are too large together. Retake the photos at a lower resolution.")
    return out


def pdf_page_count(data: bytes) -> int | None:
    try:
        from pypdf import PdfReader  # noqa: PLC0415

        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001
        return None


def pdf_text(data: bytes, max_pages: int = MAX_PDF_PAGES) -> str:
    """Text layer of a PDF (empty for a pure image scan)."""
    try:
        from pypdf import PdfReader  # noqa: PLC0415

        reader = PdfReader(io.BytesIO(data))
        parts = [(p.extract_text() or "") for p in reader.pages[:max_pages]]
    except Exception:  # noqa: BLE001
        return ""
    return "\n\n".join(t.strip() for t in parts if t and t.strip())


def image_block(f: ScanFile) -> dict[str, Any]:
    data, mt = f.data, f.media_type
    if len(data) > VISION_MAX_RAW:
        shrunk = _shrink_image(data)
        if shrunk is None:
            raise ScanError(f"{f.name} is too large to send for reading. Retake it at a lower resolution.")
        data, mt = shrunk, "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(data).decode()}}


def context_text(today: str, plant_name: str | None) -> str:
    return (f"Today is {today} (India). The permit was raised at plant: {plant_name or 'unknown'}.\n"
            "Return the JSON object now.")


# ── model call ───────────────────────────────────────────────────────────────
def _response_text(msg: Any) -> str:
    return "".join(getattr(b, "text", "") or "" for b in (getattr(msg, "content", None) or [])).strip()


def parse_model_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def _call(client: Any, model: str, content: list[dict[str, Any]], timeout: float) -> tuple[str, Any]:
    msg = await asyncio.to_thread(
        client.with_options(timeout=timeout, max_retries=0).messages.create,
        model=model, max_tokens=MAX_TOKENS, system=EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    return _response_text(msg), getattr(msg, "usage", None)


async def extract(files: list[ScanFile], *, today: str, plant_name: str | None) -> tuple[dict[str, Any], str]:
    """Run the model. Returns (parsed JSON, method). Raises ScanError on failure."""
    from app.core.config import get_settings  # noqa: PLC0415
    from app.services.ai.anthropic_client import _get_client  # noqa: PLC0415

    client = _get_client()
    if client is None:
        raise ScanError("AI_NOT_CONFIGURED", 503)
    model = get_settings().anthropic_model
    ctx = {"type": "text", "text": context_text(today, plant_name)}
    started = time.monotonic()
    method = "vision"
    raw, usage, err = "", None, None
    if files[0].media_type == "application/pdf":
        method = "document"
        pages = pdf_page_count(files[0].data)
        if pages is not None and pages > MAX_PDF_PAGES:
            raise ScanError(f"The PDF has {pages} pages; a permit scan should be at most {MAX_PDF_PAGES}.")
        doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                  "data": base64.b64encode(files[0].data).decode()}}
        try:
            raw, usage = await _call(client, model, [doc, ctx], CALL_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            err = e
        if err is not None or parse_model_json(raw) is None:
            left = 24.0 - (time.monotonic() - started)
            text = pdf_text(files[0].data)
            log.warning("ptw scan: document read failed (%s); text fallback %s, %.1fs left",
                        err or "unparseable reply", "available" if text else "empty", left)
            if not text:
                raise ScanError("The PDF could not be read. Photograph the paper permit instead.", 502)
            if left < TEXT_FALLBACK_MIN_LEFT_S:
                raise ScanError("Reading the PDF took too long. Try again, or photograph the permit.", 504)
            method, err = "pdf_text", None
            try:
                raw, usage = await _call(client, model, [{"type": "text", "text": "PERMIT TEXT (extracted from PDF):\n"
                                                           + text[:60000]}, ctx], max(5.0, left - 1.0))
            except Exception as e:  # noqa: BLE001
                err = e
    else:
        blocks = [image_block(f) for f in files]
        try:
            raw, usage = await _call(client, model, [*blocks, ctx], CALL_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            err = e
    latency_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "ptw scan: model=%s method=%s files=%d latency_ms=%d tokens_in=%s tokens_out=%s ok=%s",
        model, method, len(files), latency_ms, getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None), err is None,
    )
    if err is not None:
        log.warning("ptw scan: model call failed: %s", err)
        raise ScanError("The scan could not be read right now. Try again, or fill the form manually.", 502)
    data = parse_model_json(raw)
    if data is None:
        raise ScanError("The scan could not be read. Retake the photo in better light, or fill the form manually.", 502)
    return data, method


# ── normalisation (pure) ─────────────────────────────────────────────────────
def _conf(v: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def _clean_str(v: Any, max_len: int = 2000) -> str | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    if s.lower() in ("", "null", "none", "n/a", "na", "-", "nil"):
        return None
    return s[:max_len]


_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("confined", "CONFINED_SPACE"), ("vessel entry", "CONFINED_SPACE"),
    ("hot", "HOT_WORK"), ("welding", "HOT_WORK"), ("gas cutting", "HOT_WORK"),
    ("height", "WORK_AT_HEIGHT"), ("excavat", "EXCAVATION"), ("digging", "EXCAVATION"),
    ("electric", "ELECTRICAL_LOTO"), ("loto", "ELECTRICAL_LOTO"), ("lockout", "ELECTRICAL_LOTO"),
    ("lift", "LIFTING"), ("crane", "LIFTING"), ("rigging", "LIFTING"),
    ("cold", "GENERAL_COLD"), ("general", "GENERAL_COLD"),
]


def to_permit_type(value: Any, text: Any = None) -> str | None:
    v = (_clean_str(value) or "").upper().replace(" ", "_").replace("/", "_")
    if v in PERMIT_TYPES:
        return v
    hay = f"{_clean_str(value) or ''} {_clean_str(text) or ''}".lower()
    for kw, code in _TYPE_KEYWORDS:
        if kw in hay:
            return code
    return None


_DT_FORMATS = (
    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M",
    "%d.%m.%Y %H:%M", "%d/%m/%y %H:%M", "%d-%m-%y %H:%M", "%d/%m/%Y %I:%M %p", "%d-%m-%Y %I:%M %p",
    "%d/%m/%Y %H.%M", "%d %b %Y %H:%M", "%d %B %Y %H:%M",
)


def to_local_dt(value: Any) -> str | None:
    """Any common written form → 'YYYY-MM-DDTHH:MM' (the wizard's datetime-local value)."""
    s = _clean_str(value, 40)
    if not s:
        return None
    s = s.replace(" hrs", "").replace("hrs", "").strip()
    for fmt in _DT_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if not (2000 <= d.year <= 2100):
            return None
        return d.strftime("%Y-%m-%dT%H:%M")
    return None


_PPE_SYNONYMS: list[tuple[str, str]] = [
    ("welding glove", "welding_gloves"), ("leather glove", "welding_gloves"),
    ("insulat", "insulated_gloves"), ("electrical glove", "insulated_gloves"), ("rubber glove", "insulated_gloves"),
    ("arc flash", "arc_flash_suit"), ("arc suit", "arc_flash_suit"),
    ("face shield", "face_shield"), ("welding shield", "face_shield"),
    ("fr cover", "fr_coverall"), ("fire retard", "fr_coverall"), ("aluminised", "fr_coverall"), ("aluminized", "fr_coverall"),
    ("helmet", "helmet"), ("hard hat", "helmet"),
    ("shoe", "shoes"), ("boot", "shoes"),
    ("goggle", "goggles"), ("spectacle", "goggles"), ("glasses", "goggles"),
    ("harness", "harness"), ("lanyard", "lanyard"),
    ("scba", "scba"), ("breathing apparatus", "scba"), ("air line", "scba"),
    ("gas monitor", "gas_monitor"), ("gas detector", "gas_monitor"),
    ("vest", "high_vis"), ("hi-vis", "high_vis"), ("high vis", "high_vis"), ("reflective", "high_vis"),
    ("ear", "ear_plugs"), ("respirator", "respirator"), ("mask", "respirator"),
    ("glove", "gloves"),
]


def to_ppe_codes(items: Any) -> list[str]:
    out: list[str] = []
    for it in items if isinstance(items, list) else []:
        s = (_clean_str(it, 80) or "").lower()
        if not s:
            continue
        if s.replace(" ", "_") in PPE_CATALOG:
            code = s.replace(" ", "_")
        else:
            code = next((c for kw, c in _PPE_SYNONYMS if kw in s), None)
        if code and code not in out:
            out.append(code)
    return out


def guess_isolation_type(desc: str | None) -> str:
    d = (desc or "").lower()
    for kw, t in (("electric", "ELECTRICAL"), ("breaker", "ELECTRICAL"), ("mcc", "ELECTRICAL"),
                  ("steam", "STEAM"), ("hydraul", "HYDRAULIC"), ("pneumat", "PNEUMATIC"), ("air", "PNEUMATIC"),
                  ("valve", "FLUID"), ("water", "FLUID"), ("gas", "FLUID"), ("oil", "FLUID")):
        if kw in d:
            return t
    return "MECHANICAL"


@dataclass
class FieldVal:
    value: Any
    text: str | None
    confidence: float


@dataclass
class Extraction:
    is_permit: bool
    fields: dict[str, FieldVal]
    extra_hazards: list[str]
    extra_hazards_conf: float
    crew: list[dict[str, Any]]
    isolations: list[dict[str, Any]]
    ppe: list[str]
    ppe_conf: float
    gas_readings: list[dict[str, Any]]
    unreadable: list[str]
    notes: str | None


def normalise_extraction(data: dict[str, Any]) -> Extraction:
    """Model JSON → typed, cleaned values. Tolerant of every shape the model
    might get slightly wrong (bare values instead of {value,...} objects,
    labels instead of codes); anything that cannot be cleaned becomes null."""
    if data.get("isPermit") is False:
        return Extraction(False, {}, [], 0.0, [], [], [], 0.0, [], [], _clean_str(data.get("notes"), 300))
    raw_fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    unreadable = [k for k in (data.get("unreadable") or []) if isinstance(k, str) and k in SCALAR_FIELDS]
    fields: dict[str, FieldVal] = {}
    for key in SCALAR_FIELDS:
        f = raw_fields.get(key)
        if not isinstance(f, dict):
            f = {"value": f, "text": f, "confidence": 0.5 if f not in (None, "") else 0.0}
        text = _clean_str(f.get("text"), 2000) or _clean_str(f.get("value"), 2000)
        conf = _conf(f.get("confidence"))
        if key == "permitType":
            value: Any = to_permit_type(f.get("value"), f.get("text"))
        elif key in ("validFrom", "validTo"):
            value = to_local_dt(f.get("value")) or to_local_dt(f.get("text"))
            if value is None and text:
                if key not in unreadable:
                    unreadable.append(key)
        elif key == "windSpeedKmh":
            try:
                value = float(str(f.get("value")).replace("km/h", "").strip()) if f.get("value") is not None else None
                value = value if value is not None and 0 <= value <= 250 else None
            except ValueError:
                value = None
        else:
            value = _clean_str(f.get("value"), 2000)
        if value is None:
            conf = 0.0
        fields[key] = FieldVal(value, text, conf)

    eh = data.get("extraHazards") if isinstance(data.get("extraHazards"), dict) else {"value": data.get("extraHazards")}
    base = fields["permitType"].value
    base_hazard = {"GENERAL_COLD": "GENERAL"}.get(base or "", base)
    extra = []
    for h in eh.get("value") or []:
        code = (_clean_str(h, 40) or "").upper().replace(" ", "_")
        if code in HAZARDS and code != base_hazard and code not in extra:
            extra.append(code)

    crew = []
    for c in data.get("crew") or []:
        if not isinstance(c, dict):
            continue
        name = _clean_str(c.get("name"), 120)
        if not name:
            continue
        role = (_clean_str(c.get("role"), 30) or "WORKER").upper()
        crew.append({"name": name, "role": role if role in CREW_ROLES else "WORKER", "confidence": _conf(c.get("confidence"), 0.5)})

    isolations = []
    for i in data.get("isolations") or []:
        if not isinstance(i, dict):
            continue
        desc = _clean_str(i.get("description"), 300)
        tag = _clean_str(i.get("isolationPointTag"), 80)
        if not desc and not tag:
            continue
        itype = (_clean_str(i.get("isolationType"), 30) or "").upper()
        isolations.append({
            "isolationType": itype if itype in ISOLATION_TYPES else guess_isolation_type(desc),
            "description": desc or tag,
            "isolationPointTag": tag or "TBC",  # the wizard blocks submit until a real tag replaces TBC
            "lotoTagNumber": _clean_str(i.get("lotoTagNumber"), 80),
            "confidence": _conf(i.get("confidence"), 0.5) if tag else min(_conf(i.get("confidence"), 0.5), 0.4),
        })

    ppe_raw = data.get("ppe") if isinstance(data.get("ppe"), dict) else {"value": data.get("ppe"), "confidence": 0.5}
    gas = []
    for g in data.get("gasReadings") or []:
        if not isinstance(g, dict):
            continue
        param = _clean_str(g.get("parameter"), 24)
        try:
            val = float(g.get("value"))
        except (TypeError, ValueError):
            continue
        if param:
            gas.append({"parameter": param.upper(), "value": val, "unit": _clean_str(g.get("unit"), 12) or "",
                        "confidence": _conf(g.get("confidence"), 0.5)})
    return Extraction(
        True, fields, extra, _conf(eh.get("confidence"), 0.5) if extra else 0.0, crew[:30], isolations[:20],
        to_ppe_codes(ppe_raw.get("value")), _conf(ppe_raw.get("confidence"), 0.5), gas[:12],
        unreadable, _clean_str(data.get("notes"), 300),
    )


# ── name matching (pure) ─────────────────────────────────────────────────────
_HONORIFICS = {"mr", "mrs", "ms", "miss", "shri", "sh", "smt", "er", "dr", "sri", "kum"}


def norm_name(s: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    toks = [t for t in s.split() if t not in _HONORIFICS]
    return " ".join(toks)


def _initials_match(a: list[str], b: list[str]) -> bool:
    """'r kumar' ~ 'rajesh kumar': same token count, every token equal or an initial of the other."""
    if len(a) != len(b) or len(a) < 2:
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        if (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y)):
            continue
        return False
    return True


def name_score(query: str, candidate: str) -> float:
    q, c = norm_name(query), norm_name(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    qt, ct = q.split(), c.split()
    if sorted(qt) == sorted(ct):
        return 0.97
    if _initials_match(qt, ct):
        return 0.88
    ratio = difflib.SequenceMatcher(None, q, c).ratio()
    # "furnace 2" vs "steel melt shop furnace 2": every query token present in the candidate
    if len(qt) >= 1 and all(t in ct for t in qt) and len(q) >= 4:
        ratio = max(ratio, 0.86 if len(qt) > 1 else 0.8)
    return ratio


@dataclass
class Match:
    id: str | None
    name: str | None
    score: float
    candidates: list[dict[str, str]] = field(default_factory=list)
    reason: str | None = None


def match_name(query: str | None, options: list[tuple[str, str]], *, threshold: float = 0.85,
               margin: float = 0.04) -> Match:
    """Best option for `query`; unresolved (id None) when nothing clears the
    threshold or two options are too close to call."""
    if not query:
        return Match(None, None, 0.0, reason="blank")
    scored = sorted(((name_score(query, n), i, n) for i, n in options), key=lambda x: -x[0])
    top = [{"id": i, "name": n} for s, i, n in scored[:3] if s >= 0.5]
    if not scored or scored[0][0] < threshold:
        return Match(None, None, scored[0][0] if scored else 0.0, top, "no close match")
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - margin and scored[1][1] != scored[0][1]:
        return Match(None, None, scored[0][0], top, "more than one close match")
    s, i, n = scored[0]
    return Match(i, n, s)


# ── draft assembly (pure) ────────────────────────────────────────────────────
@dataclass
class Masters:
    plant_id: str
    plant_name: str | None
    areas: list[tuple[str, str]]
    departments: list[tuple[str, str]]
    contractors: list[tuple[str, str]]
    users: list[tuple[str, str]]
    current_user_id: str


def _validate_keys(draft: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """Keep only keys the copilot's PtwDraftPatch accepts, one by one, so a bad
    value drops that field rather than the whole draft."""
    ok: dict[str, Any] = {}
    for k, v in draft.items():
        try:
            PtwDraftPatch.model_validate({k: v})
        except Exception:  # noqa: BLE001
            warnings.append(f"{k} was dropped: the value read from the scan is not valid for the form.")
            continue
        ok[k] = v
    return ok


# Same caps as the copilot's PtwDraftPatch, so long handwriting is trimmed rather than dropped.
_MAX_LEN = {"specificLocation": 200, "scopeOfWork": 2000, "workOrderNumber": 60, "rescuePlan": 2000,
            "weatherConditionsAtIssue": 120}


def build_draft(ex: Extraction, m: Masters) -> dict[str, Any]:
    """Extraction + masters → {draft, fields, unresolved, warnings, ...} for the review screen."""
    f = ex.fields
    draft: dict[str, Any] = {"plantId": m.plant_id}
    review: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    warnings: list[str] = []

    def row(key: str, value: Any, display: Any, conf: float, status: str | None = None, note: str | None = None) -> None:
        fv = f.get(key)
        if status is None:
            if key in ex.unreadable:
                status = "unreadable"
            elif fv is None or (fv.value is None and not fv.text):
                status = "blank"
            elif conf < LOW_CONFIDENCE:
                status = "low"
            else:
                status = "ok"
        review.append({"key": key, "label": SCALAR_FIELDS.get(key, key), "text": fv.text if fv else None,
                       "value": value, "display": display, "confidence": round(conf, 2), "status": status, "note": note})

    def plain(key: str, draft_key: str | None) -> None:
        fv = f[key]
        if draft_key and fv.value is not None:
            v = fv.value
            draft[draft_key] = v[:_MAX_LEN[draft_key]] if isinstance(v, str) and draft_key in _MAX_LEN else v
        row(key, fv.value, fv.value, fv.confidence)

    def resolved(key: str, draft_key: str, options: list[tuple[str, str]], what: str) -> None:
        fv = f[key]
        query = fv.value if isinstance(fv.value, str) else fv.text
        if not query:
            row(key, None, None, 0.0)
            return
        mt = match_name(query, options)
        if mt.id:
            draft[draft_key] = mt.id
            row(key, mt.id, mt.name, fv.confidence * mt.score,
                note=None if mt.score >= 0.97 else f"Matched '{query}' to {mt.name}")
        else:
            unresolved.append({"field": key, "label": SCALAR_FIELDS[key], "text": query, "reason": mt.reason,
                               "candidates": mt.candidates})
            row(key, None, None, fv.confidence, status="unresolved",
                note=f"Could not match '{query}' to a {what} ({mt.reason}). Pick it on the form.")

    t = f["permitType"]
    if t.value:
        draft["type"] = t.value
    row("permitType", t.value, t.value, t.confidence)
    plain("paperPermitNumber", None)
    plant_text = f["plant"].text
    if plant_text and m.plant_name and name_score(plant_text, m.plant_name) < 0.6:
        warnings.append(f"The form names plant '{plant_text}', but the permit is being raised at {m.plant_name}.")
    row("plant", m.plant_id, m.plant_name, f["plant"].confidence if plant_text else 1.0,
        status="ok", note="Permits are raised at the plant selected on the form.")
    resolved("department", "departmentId", m.departments, "department of this plant")
    resolved("area", "areaId", m.areas, "area of this plant")
    plain("specificLocation", "specificLocation")
    if not draft.get("specificLocation") and not draft.get("areaId") and f["area"].value:
        draft["specificLocation"] = f["area"].value  # keep what was written so it isn't lost
    plain("scopeOfWork", "scopeOfWork")
    if draft.get("scopeOfWork") and len(draft["scopeOfWork"]) < 10:
        warnings.append("The scope of work read from the scan is shorter than the form's 10-character minimum.")
    plain("validFrom", "validFrom")
    plain("validTo", "validTo")
    if draft.get("validFrom") and draft.get("validTo") and draft["validTo"] <= draft["validFrom"]:
        warnings.append("Valid To is not after Valid From on the scan. Check the dates.")
    plain("workOrderNumber", "workOrderNumber")
    resolved("contractor", "contractorCompanyId", m.contractors, "registered contractor")
    resolved("issuerName", "issuerId", m.users, "user")
    resolved("receiverName", "receiverId", m.users, "user")
    resolved("fireWatchName", "fireWatchPersonId", m.users, "user")
    resolved("standbyName", "standbyPersonId", m.users, "user")
    plain("rescuePlan", "rescuePlan")
    plain("weatherConditions", "weatherConditionsAtIssue")
    plain("windSpeedKmh", "windSpeedKmh")

    if draft.get("issuerId") == m.current_user_id:
        warnings.append("The issuer on the paper is you; the originator cannot be their own issuer, so issuer was left blank.")
        draft.pop("issuerId")
    if draft.get("issuerId") and draft.get("issuerId") == draft.get("receiverId"):
        warnings.append("Issuer and receiver matched the same person; receiver was left blank.")
        draft.pop("receiverId")

    if ex.extra_hazards:
        draft["extraHazards"] = ex.extra_hazards

    crew_rows, crew_ids, crew_review = [], set(), []
    for c in ex.crew:
        mt = match_name(c["name"], m.users)
        crew_review.append({"name": c["name"], "role": c["role"], "confidence": round(c["confidence"] * (mt.score if mt.id else 1), 2),
                            "matchedName": mt.name})
        if mt.id and mt.id not in crew_ids and mt.id != draft.get("standbyPersonId"):
            crew_ids.add(mt.id)
            crew_rows.append({"userId": mt.id, "role": c["role"]})
        elif not mt.id:
            unresolved.append({"field": "crew", "label": "Crew member", "text": c["name"], "reason": mt.reason,
                               "candidates": mt.candidates})
    if crew_rows:
        draft["workCrew"] = crew_rows
    if ex.isolations:
        draft["isolations"] = [{k: i[k] for k in ("isolationType", "description", "isolationPointTag", "lotoTagNumber")}
                               for i in ex.isolations]
        if any(i["isolationPointTag"] == "TBC" for i in ex.isolations):
            warnings.append("Some isolation tags could not be read and are marked TBC; enter the real tags before submitting.")
    if ex.ppe:
        draft["requiredPpe"] = ex.ppe

    draft = _validate_keys(draft, warnings)
    return {
        "draft": draft,
        "fields": review,
        "lists": {
            "extraHazards": {"value": ex.extra_hazards, "confidence": round(ex.extra_hazards_conf, 2)},
            "crew": crew_review,
            "isolations": [{**i, "confidence": round(i["confidence"], 2)} for i in ex.isolations],
            "ppe": {"value": ex.ppe, "confidence": round(ex.ppe_conf, 2)},
            "gasReadings": ex.gas_readings,
        },
        "paperPermitNumber": f["paperPermitNumber"].value,
        "unreadable": [{"key": k, "label": SCALAR_FIELDS[k]} for k in ex.unreadable],
        "unresolved": unresolved,
        "warnings": warnings,
        "notes": ex.notes,
    }


# ── masters (async) ──────────────────────────────────────────────────────────
async def load_masters(db: AsyncSession, plant_id: str, user_id: str) -> Masters:
    from app.models.epc import ContractorCompany  # noqa: PLC0415
    from app.models.masters import Department  # noqa: PLC0415
    from app.models.plant import Area, Plant  # noqa: PLC0415
    from app.models.user import User  # noqa: PLC0415

    plant = await db.get(Plant, plant_id)
    areas = (await db.execute(select(Area.id, Area.name).where(Area.plantId == plant_id))).all()
    deps = (await db.execute(
        select(Department.id, Department.name).where(Department.plantId == plant_id).where(Department.active.is_(True))
    )).all()
    cstmt = select(ContractorCompany.id, ContractorCompany.name)
    if hasattr(ContractorCompany, "status"):
        cstmt = cstmt.where(ContractorCompany.status == "ACTIVE")
    contractors = (await db.execute(cstmt.limit(5000))).all()
    users = (await db.execute(select(User.id, User.name).where(User.plantId == plant_id).limit(20000))).all()
    return Masters(
        plant_id=plant_id, plant_name=plant.name if plant else None,
        areas=[(a, n) for a, n in areas], departments=[(a, n) for a, n in deps],
        contractors=[(a, n) for a, n in contractors], users=[(a, n) for a, n in users],
        current_user_id=user_id,
    )
