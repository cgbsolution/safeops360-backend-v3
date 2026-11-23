"""External-system connector framework (JSL Hisar: "integrate with JSL SAS portal").

STATUS: awaiting the JSL SAS API specification. Nothing in this module assumes
what SAS accepts; every SAS-specific fact (URL, auth, field names, value codes)
is endpoint configuration that an admin edits on /configuration/integrations.
Until a base URL is configured, or while an endpoint is in dry-run, outbound
rows are marked SKIPPED with the exact payload that WOULD have been sent, so the
mapping can be demonstrated and signed off before SAS is reachable.

Three halves:

* Pure helpers (field transforms, payload building, inbound mapping, back-off,
  API-key comparison, config validation). No DB access; tests run them offline
  (tests/test_integrations.py).

* Change capture. No hook in the observation write paths: `capture_changes`
  polls Observation.updatedAt past a per-endpoint watermark and compares each
  record's status with the last status pushed to that endpoint. That catches
  every write path (web form, Snap & Report, /capture conversion, mobile,
  Copilot, Prisma-side updates) and can never poison an observation's own
  transaction. A unique (endpointId, dedupeKey) index makes re-scans and
  overlapping runs idempotent.

* Delivery. `deliver_due` pushes PENDING rows and retries FAILED ones with
  exponential back-off. `run_integration_sync` (capture, then deliver) is the
  scheduler job body.

Secrets: endpoints store the NAME of an environment variable, never the value,
and only INTEGRATION_* names are accepted, so a misconfigured endpoint cannot be
pointed at DATABASE_URL or the JWT secret and send it to a third party.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import IntegrationEndpoint, IntegrationSyncLog

log = logging.getLogger("safeops360.integrations")

# ── vocabularies ─────────────────────────────────────────────────────────────
DIRECTIONS = ("OUTBOUND", "INBOUND", "BOTH")
METHODS = ("POST", "PUT", "PATCH")
AUTH_TYPES = ("NONE", "API_KEY", "BEARER", "BASIC")
LOG_STATUSES = ("PENDING", "SENT", "FAILED", "SKIPPED", "RECEIVED")
LOG_DIRECTIONS = ("OUTBOUND", "INBOUND")
ENTITY_TYPES = ("OBSERVATION",)
EVENT_CREATED = "observation.created"
EVENT_STATUS_CHANGED = "observation.status_changed"
EVENT_INBOUND = "inbound.create"
TRIGGER_EVENTS = (EVENT_CREATED, EVENT_STATUS_CHANGED)
MAPPING_DIRECTIONS = ("BOTH", "OUT", "IN")

TRANSFORMS = (
    "none", "string", "upper", "lower", "trim", "date", "datetime", "epoch_ms", "int",
    "bool_yn", "bool_10", "map", "prefix", "suffix", "truncate", "default", "constant",
)
TRANSFORM_HELP = {
    "none": "Value as is",
    "string": "Convert to text",
    "upper": "UPPER CASE",
    "lower": "lower case",
    "trim": "Strip surrounding spaces",
    "date": "Date only, YYYY-MM-DD",
    "datetime": "ISO 8601 date-time",
    "epoch_ms": "Milliseconds since 1970",
    "int": "Whole number",
    "bool_yn": "true/false → Y/N",
    "bool_10": "true/false → 1/0",
    "map": "Value table, e.g. UNSAFE_ACT=UA;UNSAFE_CONDITION=UC;*=OT",
    "prefix": "Add the argument in front",
    "suffix": "Add the argument at the end",
    "truncate": "Cut to N characters (argument = N)",
    "default": "Use the argument when the value is empty",
    "constant": "Always send the argument (source ignored)",
}

# SafeOps observation fields an admin can map. `in` marks the fields an inbound
# record may supply; inbound-only fields carry no outbound value.
SAFEOPS_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"label": "SafeOps record id", "in": False},
    "number": {"label": "Observation number", "in": False},
    "externalId": {"label": "External (SAS) record id", "in": True},
    "eventType": {"label": "Event (created / status changed)", "in": False},
    "date": {"label": "Observed at", "in": True},
    "createdAt": {"label": "Reported at", "in": False},
    "updatedAt": {"label": "Last updated at", "in": False},
    "type": {"label": "Observation type code", "in": True},
    "typeLabel": {"label": "Observation type label", "in": False},
    "actOrCondition": {"label": "Act or condition (ACT / CONDITION)", "in": False},
    "isUnsafe": {"label": "Is at-risk (true / false)", "in": False},
    "category": {"label": "Hazard category code", "in": True},
    "categoryCode": {"label": "STOP category code", "in": True},
    "categoryLabel": {"label": "STOP category label", "in": False},
    "subCategoryCode": {"label": "STOP sub-category code", "in": True},
    "subCategoryLabel": {"label": "STOP sub-category label", "in": False},
    "severity": {"label": "Severity", "in": True},
    "status": {"label": "Status", "in": False},
    "plantId": {"label": "Plant id", "in": True},
    "plantCode": {"label": "Plant code", "in": True},
    "plantName": {"label": "Plant name", "in": False},
    "areaId": {"label": "Area id", "in": True},
    "areaName": {"label": "Area name", "in": True},
    "observerName": {"label": "Reporter name", "in": True},
    "observerEmail": {"label": "Reporter email", "in": True},
    "observerDepartment": {"label": "Reporter department", "in": False},
    "responsiblePersonName": {"label": "Responsible person", "in": False},
    "description": {"label": "Description", "in": True},
    "immediateAction": {"label": "Immediate action taken", "in": True},
    "targetDate": {"label": "Target closure date", "in": False},
    "closedAt": {"label": "Closed at", "in": False},
    "closingRemark": {"label": "Closing remark", "in": False},
    "photoCount": {"label": "Number of photos", "in": False},
    "photoUrl": {"label": "First photo (signed link, 7 days)", "in": False},
    "latitude": {"label": "Latitude (photo GPS)", "in": False},
    "longitude": {"label": "Longitude (photo GPS)", "in": False},
}

# Starting mapping for the JSL SAS endpoint. The TARGET names are placeholders
# until JSL publishes the SAS API specification; they are deliberately plain so
# the sign-off conversation is about which fields, not about our naming.
JSL_SAS_TEMPLATE_MAPPING: list[dict[str, Any]] = [
    {"source": "number", "target": "safeopsRef", "transform": "none"},
    {"source": "externalId", "target": "sasId", "transform": "none", "direction": "IN"},
    {"source": "eventType", "target": "event", "transform": "none", "direction": "OUT"},
    {"source": "date", "target": "observedAt", "transform": "datetime"},
    {"source": "type", "target": "observationType", "transform": "none"},
    {"source": "categoryCode", "target": "category", "transform": "none"},
    {"source": "subCategoryCode", "target": "subCategory", "transform": "none"},
    {"source": "severity", "target": "severity", "transform": "none"},
    {"source": "status", "target": "status", "transform": "none", "direction": "OUT"},
    {"source": "plantCode", "target": "location.plant", "transform": "none"},
    {"source": "areaName", "target": "location.area", "transform": "none"},
    {"source": "latitude", "target": "location.lat", "transform": "none", "direction": "OUT"},
    {"source": "longitude", "target": "location.lng", "transform": "none", "direction": "OUT"},
    {"source": "observerName", "target": "reportedBy.name", "transform": "none"},
    {"source": "observerEmail", "target": "reportedBy.email", "transform": "lower"},
    {"source": "description", "target": "description", "transform": "truncate", "arg": "4000"},
    {"source": "immediateAction", "target": "immediateAction", "transform": "none"},
    {"source": "targetDate", "target": "targetClosureDate", "transform": "date", "direction": "OUT"},
    {"source": "photoUrl", "target": "photoUrl", "transform": "none", "direction": "OUT"},
]

MAX_ATTEMPTS = 8
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 6 * 3600
CAPTURE_OVERLAP = timedelta(minutes=10)  # re-scan window for late-committing transactions
CAPTURE_BATCH = 500
DELIVER_BATCH = 100
EXCERPT_CHARS = 1000
_ENV_NAME_RE = re.compile(r"^INTEGRATION_[A-Z0-9_]{1,64}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SECRETISH_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    """DB drivers hand back naive UTC on some backends; compare as aware UTC."""
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── transforms ───────────────────────────────────────────────────────────────
def _parse_map_arg(arg: str | None) -> tuple[dict[str, str], str | None]:
    """"A=1;B=2;*=9" → ({"A": "1", "B": "2"}, "9"). Comma also separates pairs."""
    table: dict[str, str] = {}
    fallback: str | None = None
    for part in re.split(r"[;,]", arg or ""):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "*":
            fallback = v
        elif k:
            table[k] = v
    return table, fallback


def _to_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    s = str(value).strip()
    if re.fullmatch(r"\d{12,14}", s):
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
    try:
        return _aware(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _truthy(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "y", "yes", "true", "t"):
        return True
    if s in ("0", "n", "no", "false", "f"):
        return False
    return None


def apply_transform(value: Any, transform: str | None, arg: str | None = None) -> Any:
    """SafeOps value → external value. Unknown transforms pass the value through
    (config validation rejects them before they are saved)."""
    t = (transform or "none").lower()
    if t == "constant":
        return arg
    if t == "default":
        return arg if value is None or value == "" else value
    if value is None:
        return None
    if t == "none":
        return value
    if t == "string":
        return str(value)
    if t == "upper":
        return str(value).upper()
    if t == "lower":
        return str(value).lower()
    if t == "trim":
        return str(value).strip()
    if t in ("date", "datetime", "epoch_ms"):
        d = _to_datetime(value)
        if d is None:
            return None
        if t == "date":
            return d.date().isoformat()
        if t == "datetime":
            return d.isoformat()
        return int(d.timestamp() * 1000)
    if t == "int":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    if t in ("bool_yn", "bool_10"):
        b = _truthy(value)
        if b is None:
            return None
        return ("Y" if b else "N") if t == "bool_yn" else (1 if b else 0)
    if t == "map":
        table, fallback = _parse_map_arg(arg)
        key = str(value)
        return table.get(key, fallback if fallback is not None else value)
    if t == "prefix":
        return f"{arg or ''}{value}"
    if t == "suffix":
        return f"{value}{arg or ''}"
    if t == "truncate":
        try:
            n = int(arg or "0")
        except ValueError:
            return value
        s = str(value)
        return s[:n] if n > 0 else s
    return value


def reverse_transform(value: Any, transform: str | None, arg: str | None = None) -> Any:
    """External value → SafeOps value for an inbound record. Lossy transforms
    (truncate, constant) are not reversed; `constant` yields nothing."""
    t = (transform or "none").lower()
    if t == "constant":
        return None
    if value is None or value == "":
        return None
    if t == "map":
        table, _fallback = _parse_map_arg(arg)
        inverse = {v: k for k, v in table.items()}
        return inverse.get(str(value), value)
    if t in ("date", "datetime", "epoch_ms"):
        return _to_datetime(value)
    if t in ("bool_yn", "bool_10"):
        return _truthy(value)
    if t == "prefix" and arg and str(value).startswith(arg):
        return str(value)[len(arg):]
    if t == "suffix" and arg and str(value).endswith(arg):
        return str(value)[: -len(arg)]
    if t == "upper":
        return str(value).upper()
    if t == "lower":
        return str(value).lower()
    if t == "trim":
        return str(value).strip()
    if t == "int":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return value


def set_path(target: dict[str, Any], path: str, value: Any) -> None:
    """Write `value` at a dotted path ("location.area"), creating objects."""
    parts = [p for p in path.split(".") if p]
    cur = target
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    if parts:
        cur[parts[-1]] = value


def get_path(source: Any, path: str) -> Any:
    cur = source
    for p in [p for p in path.split(".") if p]:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and p.isdigit() and int(p) < len(cur):
            cur = cur[int(p)]
        else:
            return None
    return cur


def _row_dir(row: dict[str, Any]) -> str:
    return str(row.get("direction") or "BOTH").upper()


def build_payload(record: dict[str, Any], mapping: list[dict[str, Any]] | None) -> dict[str, Any]:
    """SafeOps record dict → external JSON body using the endpoint's mapping.
    With no mapping, the flat record is sent as is (useful before sign-off)."""
    if not mapping:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in record.items()}
    out: dict[str, Any] = {}
    for row in mapping:
        if _row_dir(row) == "IN":
            continue
        target = str(row.get("target") or "").strip()
        if not target:
            continue
        value = apply_transform(record.get(str(row.get("source") or "")), row.get("transform"), row.get("arg"))
        if isinstance(value, datetime):
            value = value.isoformat()
        set_path(out, target, value)
    return out


def map_inbound(body: dict[str, Any], mapping: list[dict[str, Any]] | None) -> dict[str, Any]:
    """External JSON → {SafeOps field: value}. With no mapping, the body's keys
    are taken as SafeOps field names (only the inbound-capable ones)."""
    out: dict[str, Any] = {}
    if not mapping:
        for k, meta in SAFEOPS_FIELDS.items():
            if meta["in"] and body.get(k) not in (None, ""):
                out[k] = body[k]
        return out
    for row in mapping:
        if _row_dir(row) == "OUT":
            continue
        source = str(row.get("source") or "")
        if not SAFEOPS_FIELDS.get(source, {}).get("in"):
            continue
        raw = get_path(body, str(row.get("target") or ""))
        value = reverse_transform(raw, row.get("transform"), row.get("arg"))
        if value not in (None, ""):
            out[source] = value
    return out


# ── back-off ─────────────────────────────────────────────────────────────────
def backoff_seconds(attempts: int) -> int:
    """Delay before retry number `attempts` + 1: 1, 2, 4, 8 … minutes, capped at 6 h."""
    n = max(1, int(attempts))
    return int(min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (n - 1))))


def next_attempt_at(attempts: int, now: datetime | None = None) -> datetime | None:
    """None once the attempts budget is spent (the row is then terminally FAILED)."""
    if attempts >= MAX_ATTEMPTS:
        return None
    return (now or utcnow()) + timedelta(seconds=backoff_seconds(attempts))


def is_retryable_status(http_status: int | None) -> bool:
    """Network errors and 5xx/408/429 are retried; any other 4xx means the
    payload or credentials were refused and retrying the same bytes won't help."""
    if http_status is None:
        return True
    if http_status in (408, 425, 429):
        return True
    return http_status >= 500


# ── secrets + auth ───────────────────────────────────────────────────────────
def valid_env_name(name: str | None) -> bool:
    return bool(name) and bool(_ENV_NAME_RE.match(name or ""))


def resolve_secret(env_name: str | None) -> str | None:
    """Value of an INTEGRATION_* environment variable (process env, then .env).
    Any other name resolves to None, by design."""
    if not valid_env_name(env_name):
        return None
    value = os.getenv(env_name or "")
    if value is None:
        try:
            from dotenv import dotenv_values

            from app.core.config import Settings

            value = dotenv_values(Settings.model_config.get("env_file") or ".env").get(env_name or "")
        except Exception:  # noqa: BLE001
            value = None
    value = (value or "").strip()
    return value or None


def api_key_matches(provided: str | None, expected: str | None) -> bool:
    """Constant-time comparison. An unset expected key never matches: an
    endpoint whose key env var is missing refuses everyone, it does not open up."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def auth_headers(auth_type: str, secret: str | None, header_name: str | None = None) -> dict[str, str]:
    t = (auth_type or "NONE").upper()
    if t == "NONE" or not secret:
        return {}
    if t == "API_KEY":
        return {(header_name or "X-API-Key"): secret}
    if t == "BEARER":
        return {"Authorization": f"Bearer {secret}"}
    if t == "BASIC":
        import base64

        # The env var holds "user:password".
        return {"Authorization": "Basic " + base64.b64encode(secret.encode("utf-8")).decode("ascii")}
    return {}


def target_url(base_url: str | None, path: str | None) -> str | None:
    if not base_url or not base_url.strip():
        return None
    base = base_url.strip().rstrip("/")
    p = (path or "").strip()
    if not p:
        return base
    return f"{base}/{p.lstrip('/')}"


def excerpt(text: str | None, limit: int = EXCERPT_CHARS) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def validate_config(cfg: dict[str, Any]) -> list[str]:
    """Problems with an endpoint configuration, in plain words. Empty = valid."""
    errs: list[str] = []
    code = str(cfg.get("code") or "")
    if not _CODE_RE.match(code):
        errs.append("Code must be 2-40 characters: capital letters, digits and underscore, starting with a letter.")
    if cfg.get("direction") not in DIRECTIONS:
        errs.append(f"Direction must be one of {', '.join(DIRECTIONS)}.")
    if (cfg.get("method") or "POST") not in METHODS:
        errs.append(f"Method must be one of {', '.join(METHODS)}.")
    if (cfg.get("authType") or "NONE") not in AUTH_TYPES:
        errs.append(f"Auth type must be one of {', '.join(AUTH_TYPES)}.")
    base = (cfg.get("baseUrl") or "").strip()
    if base and not re.match(r"^https?://[^\s/$.?#].[^\s]*$", base, re.I):
        errs.append("Base URL must start with http:// or https://.")
    for key in ("secretEnvVar", "inboundSecretEnvVar"):
        name = cfg.get(key)
        if name and not valid_env_name(name):
            errs.append(f"{key} must be the NAME of an environment variable starting with INTEGRATION_ (for example INTEGRATION_JSL_SAS_KEY). Never paste the secret itself.")
    if (cfg.get("authType") or "NONE") != "NONE" and not cfg.get("secretEnvVar"):
        errs.append("An auth type other than NONE needs the name of the environment variable holding the secret.")
    if cfg.get("direction") in ("INBOUND", "BOTH") and not cfg.get("inboundSecretEnvVar"):
        errs.append("Inbound endpoints need the name of the environment variable holding the inbound API key.")
    header_name = cfg.get("authHeaderName")
    if header_name and not _HEADER_NAME_RE.match(header_name):
        errs.append("Auth header name may contain only letters, digits and hyphens.")
    headers = cfg.get("headers") or {}
    if not isinstance(headers, dict):
        errs.append("Headers must be an object of name → value.")
    else:
        for k in headers:
            if not _HEADER_NAME_RE.match(str(k)):
                errs.append(f"Header name '{k}' is not valid.")
            elif str(k).lower() in _SECRETISH_HEADERS:
                errs.append(f"Header '{k}' carries a credential; use the auth type and an environment variable instead.")
    timeout = cfg.get("timeoutSeconds", 15)
    if not isinstance(timeout, int) or not 1 <= timeout <= 120:
        errs.append("Timeout must be between 1 and 120 seconds.")
    for ev in cfg.get("triggerEvents") or []:
        if ev not in TRIGGER_EVENTS:
            errs.append(f"Unknown trigger event '{ev}'.")
    mapping = cfg.get("fieldMapping") or []
    if not isinstance(mapping, list):
        errs.append("Field mapping must be a list of rows.")
        mapping = []
    seen_targets: set[tuple[str, str]] = set()
    for i, row in enumerate(mapping, start=1):
        if not isinstance(row, dict):
            errs.append(f"Mapping row {i} is not an object.")
            continue
        src, tgt = str(row.get("source") or ""), str(row.get("target") or "").strip()
        tr = str(row.get("transform") or "none")
        if tr != "constant" and src not in SAFEOPS_FIELDS:
            errs.append(f"Mapping row {i}: unknown SafeOps field '{src}'.")
        if not tgt or not re.match(r"^[A-Za-z_][A-Za-z0-9_\-]*(\.[A-Za-z_][A-Za-z0-9_\-]*)*$", tgt):
            errs.append(f"Mapping row {i}: external field '{tgt}' must be a name or dotted path (location.area).")
        if tr not in TRANSFORMS:
            errs.append(f"Mapping row {i}: unknown transform '{tr}'.")
        if tr in ("map", "prefix", "suffix", "truncate", "default", "constant") and not str(row.get("arg") or "").strip():
            errs.append(f"Mapping row {i}: transform '{tr}' needs an argument.")
        if tr == "truncate" and not str(row.get("arg") or "").strip().isdigit():
            errs.append(f"Mapping row {i}: truncate needs a whole number.")
        if _row_dir(row) not in MAPPING_DIRECTIONS:
            errs.append(f"Mapping row {i}: direction must be BOTH, OUT or IN.")
        key = (_row_dir(row), tgt)
        if tgt and key in seen_targets:
            errs.append(f"Mapping row {i}: external field '{tgt}' is mapped twice.")
        seen_targets.add(key)
    return errs


# ── record → flat SafeOps dict ───────────────────────────────────────────────
_TYPE_LABEL = {
    "SAFE_ACT": "Safe act", "UNSAFE_ACT": "Unsafe act",
    "SAFE_CONDITION": "Safe condition", "UNSAFE_CONDITION": "Unsafe condition",
}


def _val(v: Any) -> Any:
    return getattr(v, "value", v)


async def observation_record(
    db: AsyncSession, obs: Any, *, event_type: str | None = None, include_photo_url: bool = False,
) -> dict[str, Any]:
    """Every mappable SafeOps field for one observation, as plain values."""
    from app.models.observation import ObservationAttachment, ObservationTaxonomy
    from app.models.plant import Area, Plant
    from app.models.user import User

    plant = await db.get(Plant, obs.plantId) if obs.plantId else None
    area = await db.get(Area, obs.areaId) if obs.areaId else None
    observer = await db.get(User, obs.observerId) if obs.observerId else None
    responsible = await db.get(User, obs.responsiblePersonId) if obs.responsiblePersonId else None
    tax = None
    if obs.categoryCode and obs.subCategoryCode:
        tax = (
            await db.execute(
                select(ObservationTaxonomy)
                .where(ObservationTaxonomy.categoryCode == obs.categoryCode)
                .where(ObservationTaxonomy.subCategoryCode == obs.subCategoryCode)
                .limit(1)
            )
        ).scalar_one_or_none()
    photos = (
        await db.execute(
            select(ObservationAttachment)
            .where(ObservationAttachment.observationId == obs.id)
            .where(ObservationAttachment.deletedAt.is_(None))
            .where(ObservationAttachment.mimeType.like("image/%"))
            .order_by(ObservationAttachment.uploadedAt.asc())
        )
    ).scalars().all()
    lat = lng = None
    for p in photos:
        gps = (p.exifData or {}).get("gps") if isinstance(p.exifData, dict) else None
        if isinstance(gps, dict) and gps.get("lat") is not None and gps.get("lng") is not None:
            lat, lng = gps.get("lat"), gps.get("lng")
            break
    photo_url = None
    if include_photo_url and photos:
        try:
            import asyncio

            from app.services.storage import create_signed_download_url, is_storage_configured

            if is_storage_configured():
                photo_url = await asyncio.to_thread(
                    create_signed_download_url, photos[0].storagePath, 7 * 24 * 3600, None
                )
        except Exception as e:  # noqa: BLE001 — a missing link must not block the push
            log.warning("integration photo link failed for %s: %s", obs.id, e)
    otype = _val(obs.type)
    from app.services.observation_taxonomy import axis_for_type, requires_taxonomy

    return {
        "id": obs.id,
        "number": obs.number,
        "externalId": None,
        "eventType": event_type,
        "date": _aware(obs.date),
        "createdAt": _aware(obs.createdAt),
        "updatedAt": _aware(obs.updatedAt),
        "type": otype,
        "typeLabel": _TYPE_LABEL.get(otype, otype),
        "actOrCondition": axis_for_type(otype),
        "isUnsafe": requires_taxonomy(otype),
        "category": _val(obs.category),
        "categoryCode": obs.categoryCode,
        "categoryLabel": tax.categoryLabel if tax else None,
        "subCategoryCode": obs.subCategoryCode,
        "subCategoryLabel": tax.subCategoryLabel if tax else None,
        "severity": _val(obs.severity),
        "status": _val(obs.status),
        "plantId": obs.plantId,
        "plantCode": plant.code if plant else None,
        "plantName": plant.name if plant else None,
        "areaId": obs.areaId,
        "areaName": area.name if area else None,
        "observerName": observer.name if observer else None,
        "observerEmail": observer.email if observer else None,
        "observerDepartment": observer.department if observer else None,
        "responsiblePersonName": responsible.name if responsible else None,
        "description": obs.description,
        "immediateAction": obs.immediateAction,
        "targetDate": _aware(obs.targetDate),
        "closedAt": _aware(obs.closedAt),
        "closingRemark": obs.closingRemark,
        "photoCount": len(photos),
        "photoUrl": photo_url,
        "latitude": lat,
        "longitude": lng,
    }


def mapping_uses(mapping: list[dict[str, Any]] | None, field: str) -> bool:
    if not mapping:
        return False
    return any(str(r.get("source")) == field and _row_dir(r) != "IN" for r in mapping)


async def preview_payload(db: AsyncSession, endpoint: IntegrationEndpoint, obs: Any, event_type: str = EVENT_CREATED) -> dict[str, Any]:
    record = await observation_record(
        db, obs, event_type=event_type, include_photo_url=mapping_uses(endpoint.fieldMapping, "photoUrl"),
    )
    return build_payload(record, endpoint.fieldMapping)


# ── change capture ───────────────────────────────────────────────────────────
def is_outbound(endpoint: IntegrationEndpoint) -> bool:
    return endpoint.direction in ("OUTBOUND", "BOTH")


def is_inbound(endpoint: IntegrationEndpoint) -> bool:
    return endpoint.direction in ("INBOUND", "BOTH")


def decide_event(*, last_status: str | None, current_status: str, created_at: datetime | None,
                 baseline: datetime | None) -> str | None:
    """Which event (if any) a scanned observation represents for one endpoint.

    * never pushed before and created after the endpoint existed → created
    * never pushed before and older → nothing (no silent historical backfill)
    * pushed before with a different status → status_changed
    * same status as last pushed → nothing (a description edit is not an event)
    """
    if last_status is None:
        if baseline is not None and created_at is not None and _aware(created_at) < _aware(baseline):
            return None
        return EVENT_CREATED
    if last_status != current_status:
        return EVENT_STATUS_CHANGED
    return None


async def capture_changes(db: AsyncSession, endpoint: IntegrationEndpoint, now: datetime | None = None) -> dict[str, int]:
    """Enqueue an OUTBOUND row for every observation created / status-changed
    since this endpoint's watermark. Idempotent (dedupeKey unique index)."""
    from app.models.observation import Observation

    now = now or utcnow()
    if not (endpoint.enabled and is_outbound(endpoint) and endpoint.entityType == "OBSERVATION"):
        return {"scanned": 0, "enqueued": 0}
    triggers = set(endpoint.triggerEvents or TRIGGER_EVENTS)
    cursor = _aware(endpoint.cursorAt) or _aware(endpoint.createdAt) or now
    since = cursor - CAPTURE_OVERLAP
    stmt = select(Observation).where(Observation.updatedAt > since)
    if endpoint.plantIds:
        stmt = stmt.where(Observation.plantId.in_(list(endpoint.plantIds)))
    rows = (await db.execute(stmt.order_by(Observation.updatedAt.asc()).limit(CAPTURE_BATCH))).scalars().all()
    if not rows:
        endpoint.cursorAt = now
        await db.flush()
        return {"scanned": 0, "enqueued": 0}

    ids = [r.id for r in rows]
    prior = (
        await db.execute(
            select(IntegrationSyncLog.entityId, IntegrationSyncLog.entityStatus, IntegrationSyncLog.direction,
                   IntegrationSyncLog.status)
            .where(IntegrationSyncLog.endpointId == endpoint.id)
            .where(IntegrationSyncLog.entityType == "OBSERVATION")
            .where(IntegrationSyncLog.entityId.in_(ids))
            .order_by(IntegrationSyncLog.createdAt.asc())
        )
    ).all()
    last_status: dict[str, str | None] = {}
    came_from_here: set[str] = set()
    for eid, est, direction, st in prior:
        if direction == "INBOUND":
            if st == "RECEIVED":
                came_from_here.add(eid)
        else:
            last_status[eid] = est

    enqueued = 0
    max_seen = cursor
    include_photo = mapping_uses(endpoint.fieldMapping, "photoUrl")
    for obs in rows:
        max_seen = max(max_seen, _aware(obs.updatedAt) or max_seen)
        if obs.id in came_from_here:
            continue  # the record originated at this endpoint: never echo it back
        status_now = str(_val(obs.status))
        event = decide_event(
            last_status=last_status.get(obs.id), current_status=status_now,
            created_at=obs.createdAt, baseline=endpoint.createdAt,
        )
        if event is None or event not in triggers:
            if event is not None:
                last_status[obs.id] = status_now
            continue
        updated_iso = (_aware(obs.updatedAt) or now).isoformat()
        dedupe = f"{obs.id}:created" if event == EVENT_CREATED else f"{obs.id}:{status_now}:{updated_iso}"
        record = await observation_record(db, obs, event_type=event, include_photo_url=include_photo)
        row = IntegrationSyncLog(
            endpointId=endpoint.id, entityType="OBSERVATION", entityId=obs.id, entityRef=obs.number,
            eventType=event, entityStatus=status_now, direction="OUTBOUND", status="PENDING", attempts=0,
            payload=build_payload(record, endpoint.fieldMapping), dedupeKey=dedupe,
        )
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
            enqueued += 1
            last_status[obs.id] = status_now
        except IntegrityError:
            pass  # already enqueued by an overlapping scan
    # Advance the watermark; a full batch means more remain, so stop at the last
    # row seen rather than at "now".
    endpoint.cursorAt = max_seen if len(rows) >= CAPTURE_BATCH else max(max_seen, now)
    await db.flush()
    return {"scanned": len(rows), "enqueued": enqueued}


async def enqueue_manual(db: AsyncSession, endpoint: IntegrationEndpoint, obs: Any) -> IntegrationSyncLog:
    """Admin "push this record now" — bypasses change capture, never deduped."""
    record = await observation_record(
        db, obs, event_type=EVENT_CREATED, include_photo_url=mapping_uses(endpoint.fieldMapping, "photoUrl"),
    )
    row = IntegrationSyncLog(
        endpointId=endpoint.id, entityType="OBSERVATION", entityId=obs.id, entityRef=obs.number,
        eventType=EVENT_CREATED, entityStatus=str(_val(obs.status)), direction="OUTBOUND", status="PENDING",
        attempts=0, payload=build_payload(record, endpoint.fieldMapping),
    )
    db.add(row)
    await db.flush()
    return row


# ── delivery ─────────────────────────────────────────────────────────────────
HttpClientFactory = Callable[[float], Any]


def _default_client(timeout: float):
    import httpx

    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


_client_factory: HttpClientFactory = _default_client


def set_http_client_factory(factory: HttpClientFactory | None) -> None:
    """Tests swap the HTTP client here (no real outbound calls in tests)."""
    global _client_factory
    _client_factory = factory or _default_client


def outbound_headers(endpoint: IntegrationEndpoint) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "SafeOps360-Integration/1.0"}
    for k, v in (endpoint.headers or {}).items():
        if str(k).lower() not in _SECRETISH_HEADERS:
            headers[str(k)] = str(v)
    headers.update(auth_headers(endpoint.authType, resolve_secret(endpoint.secretEnvVar), endpoint.authHeaderName))
    return headers


def skip_reason(endpoint: IntegrationEndpoint | None) -> str | None:
    """Why a row would be SKIPPED rather than sent, or None to send."""
    if endpoint is None or endpoint.isDeleted:
        return "Endpoint no longer exists."
    if not endpoint.enabled:
        return "Endpoint is disabled."
    if endpoint.dryRun:
        return "Dry run: payload recorded, nothing sent."
    if not target_url(endpoint.baseUrl, endpoint.path):
        return "No base URL configured (awaiting JSL SAS API specification): payload recorded, nothing sent."
    if endpoint.authType != "NONE" and not resolve_secret(endpoint.secretEnvVar):
        return None  # sent anyway → the 401 is the honest signal; see test_connection
    return None


async def _send(endpoint: IntegrationEndpoint, payload: dict[str, Any]) -> tuple[int | None, str | None, Any, str | None]:
    """(http status, response text, parsed json or None, transport error)."""
    url = target_url(endpoint.baseUrl, endpoint.path)
    client = _client_factory(float(endpoint.timeoutSeconds or 15))
    try:
        async with client as c:
            resp = await c.request(endpoint.method or "POST", url, json=payload, headers=outbound_headers(endpoint))
        text_body = resp.text
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001
            parsed = None
        return resp.status_code, text_body, parsed, None
    except Exception as e:  # noqa: BLE001
        return None, None, None, f"{type(e).__name__}: {e}"[:500]


async def deliver_row(db: AsyncSession, row: IntegrationSyncLog, endpoint: IntegrationEndpoint | None,
                      now: datetime | None = None) -> str:
    now = now or utcnow()
    reason = skip_reason(endpoint)
    if reason:
        row.status = "SKIPPED"
        row.error = reason
        row.nextAttemptAt = None
        row.lastAttemptAt = now
        return row.status
    assert endpoint is not None
    row.attempts = int(row.attempts or 0) + 1
    row.lastAttemptAt = now
    http_status, body, parsed, transport_error = await _send(endpoint, row.payload or {})
    row.httpStatus = http_status
    row.responseExcerpt = excerpt(body)
    if http_status is not None and 200 <= http_status < 300:
        row.status = "SENT"
        row.sentAt = now
        row.error = None
        row.nextAttemptAt = None
        ext = get_path(parsed, endpoint.responseIdPath or "id") if isinstance(parsed, (dict, list)) else None
        row.externalId = str(ext) if ext not in (None, "") else row.externalId
        return row.status
    row.status = "FAILED"
    row.error = transport_error or f"HTTP {http_status}"
    row.nextAttemptAt = next_attempt_at(row.attempts, now) if is_retryable_status(http_status) else None
    return row.status


async def deliver_due(db: AsyncSession, now: datetime | None = None, endpoint_id: str | None = None,
                      limit: int = DELIVER_BATCH) -> dict[str, int]:
    now = now or utcnow()
    stmt = (
        select(IntegrationSyncLog)
        .where(IntegrationSyncLog.direction == "OUTBOUND")
        .where(or_(
            IntegrationSyncLog.status == "PENDING",
            and_(IntegrationSyncLog.status == "FAILED", IntegrationSyncLog.nextAttemptAt.isnot(None)),
        ))
        .where(or_(IntegrationSyncLog.nextAttemptAt.is_(None), IntegrationSyncLog.nextAttemptAt <= now))
    )
    if endpoint_id:
        stmt = stmt.where(IntegrationSyncLog.endpointId == endpoint_id)
    rows = (await db.execute(stmt.order_by(IntegrationSyncLog.createdAt.asc()).limit(limit))).scalars().all()
    counts = {"due": len(rows), "sent": 0, "failed": 0, "skipped": 0, "deferred": 0}
    endpoints: dict[str, IntegrationEndpoint | None] = {}
    blocked: set[tuple[str, str | None]] = set()
    for row in rows:
        key = (row.endpointId, row.entityId)
        if key in blocked:
            counts["deferred"] += 1
            continue
        # Keep per-record order: never push a status change ahead of an
        # unsent earlier event for the same record.
        older = await db.scalar(
            select(func.count()).select_from(IntegrationSyncLog)
            .where(IntegrationSyncLog.endpointId == row.endpointId)
            .where(IntegrationSyncLog.entityId == row.entityId)
            .where(IntegrationSyncLog.direction == "OUTBOUND")
            .where(IntegrationSyncLog.id != row.id)
            .where(IntegrationSyncLog.createdAt < row.createdAt)
            .where(or_(
                IntegrationSyncLog.status == "PENDING",
                and_(IntegrationSyncLog.status == "FAILED", IntegrationSyncLog.nextAttemptAt.isnot(None)),
            ))
        )
        if older:
            blocked.add(key)
            counts["deferred"] += 1
            continue
        if row.endpointId not in endpoints:
            endpoints[row.endpointId] = await db.get(IntegrationEndpoint, row.endpointId)
        result = await deliver_row(db, row, endpoints[row.endpointId], now)
        if result == "SENT":
            counts["sent"] += 1
        elif result == "SKIPPED":
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
            blocked.add(key)
    await db.flush()
    return counts


async def run_integration_sync(db: AsyncSession) -> dict[str, Any]:
    """Scheduler job body: change capture for every enabled outbound endpoint,
    then delivery of everything due. Idempotent."""
    now = utcnow()
    endpoints = (
        await db.execute(
            select(IntegrationEndpoint)
            .where(IntegrationEndpoint.isDeleted.is_(False))
            .where(IntegrationEndpoint.enabled.is_(True))
            .where(IntegrationEndpoint.direction.in_(("OUTBOUND", "BOTH")))
        )
    ).scalars().all()
    scanned = enqueued = 0
    for ep in endpoints:
        try:
            async with db.begin_nested():
                res = await capture_changes(db, ep, now)
            scanned += res["scanned"]
            enqueued += res["enqueued"]
        except Exception as e:  # noqa: BLE001 — one bad endpoint must not stop the rest
            log.warning("integration capture failed for %s: %s", ep.code, e)
    delivered = await deliver_due(db, now)
    return {"endpoints": len(endpoints), "scanned": scanned, "created": enqueued, **delivered}


# ── inbound ──────────────────────────────────────────────────────────────────
class InboundError(ValueError):
    """An inbound record that cannot become an observation (422 to the sender)."""


def inbound_dedupe_key(external_id: str) -> str:
    return f"in:{external_id}"


async def find_received(db: AsyncSession, endpoint_id: str, external_id: str) -> IntegrationSyncLog | None:
    return (
        await db.execute(
            select(IntegrationSyncLog)
            .where(IntegrationSyncLog.endpointId == endpoint_id)
            .where(IntegrationSyncLog.dedupeKey == inbound_dedupe_key(external_id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def build_inbound_create(db: AsyncSession, endpoint: IntegrationEndpoint, fields: dict[str, Any]) -> dict[str, Any]:
    """Mapped inbound fields → kwargs for ObservationCreate, resolving plant and
    area against the masters and clamping the classification to the real
    taxonomy. Raises InboundError with a sender-readable message."""
    from app.models.observation import DB_OBSERVATION_CATEGORIES, ObservationType, Severity
    from app.models.plant import Area, Plant
    from app.services import observation_taxonomy as tax
    from app.services.observation_snap import clamp_stop_pair, load_stop_rows

    plant = None
    if fields.get("plantId"):
        plant = await db.get(Plant, str(fields["plantId"]))
    if plant is None and fields.get("plantCode"):
        plant = (
            await db.execute(select(Plant).where(func.lower(Plant.code) == str(fields["plantCode"]).strip().lower()).limit(1))
        ).scalar_one_or_none()
    if plant is None and endpoint.defaultPlantId:
        plant = await db.get(Plant, endpoint.defaultPlantId)
    if plant is None:
        raise InboundError("Unknown plant: send a known plantCode, or configure a default plant on the endpoint.")

    area_id = None
    if fields.get("areaId"):
        area = await db.get(Area, str(fields["areaId"]))
        if area is not None and area.plantId == plant.id:
            area_id = area.id
    if area_id is None and fields.get("areaName"):
        area = (
            await db.execute(
                select(Area).where(Area.plantId == plant.id)
                .where(func.lower(Area.name) == str(fields["areaName"]).strip().lower()).limit(1)
            )
        ).scalar_one_or_none()
        area_id = area.id if area else None

    otype = str(fields.get("type") or "").strip().upper().replace(" ", "_")
    if otype not in {t.value for t in ObservationType}:
        raise InboundError(f"Observation type '{fields.get('type')}' is not one of {', '.join(t.value for t in ObservationType)}.")
    severity = str(fields.get("severity") or "MEDIUM").strip().upper()
    if severity not in {s.value for s in Severity}:
        raise InboundError(f"Severity '{fields.get('severity')}' is not one of {', '.join(s.value for s in Severity)}.")

    kwargs: dict[str, Any] = {"plantId": plant.id, "areaId": area_id, "type": otype, "severity": severity}
    notes: list[str] = []
    if tax.requires_taxonomy(otype):
        axis = tax.axis_for_type(otype)
        rows = await load_stop_rows(db)
        cat, sub, clamped = clamp_stop_pair(rows, axis, fields.get("categoryCode"), fields.get("subCategoryCode"),
                                            legacy_category=fields.get("category"))
        if not cat or not sub:
            raise InboundError("No STOP taxonomy is configured for this observation type.")
        if clamped:
            notes.append("classification defaulted, please review")
        kwargs.update(categoryCode=cat, subCategoryCode=sub)
    else:
        legacy = str(fields.get("category") or "OTHERS").strip().upper().replace(" ", "_")
        kwargs["category"] = legacy if legacy in DB_OBSERVATION_CATEGORIES else "OTHERS"

    when = fields.get("date")
    when = when if isinstance(when, datetime) else _to_datetime(when)
    kwargs["date"] = when or utcnow()
    ext = str(fields.get("externalId") or "")
    reporter = " / ".join(str(fields[k]) for k in ("observerName", "observerEmail") if fields.get(k))
    suffix = f"[Received from {endpoint.name} (ref {ext}){'; reported by ' + reporter if reporter else ''}{'; ' + '; '.join(notes) if notes else ''}.]"
    description = str(fields.get("description") or "").strip()
    # The quality gate judges the sender's own words; the provenance suffix
    # below must not pad a vague at-risk report past it.
    from app.services.bbs_quality import validate_quality

    quality_error = validate_quality(otype, description)
    if quality_error:
        raise InboundError(quality_error)
    kwargs["description"] = f"{description}\n\n{suffix}" if description else suffix
    if fields.get("immediateAction"):
        kwargs["immediateAction"] = str(fields["immediateAction"])
    return kwargs


async def test_connection(endpoint: IntegrationEndpoint) -> tuple[bool, str, int | None]:
    """Reachability + credentials check. Sends a GET to the base URL (no payload).
    Any HTTP answer proves the host is reachable; 401/403 says the credentials
    were refused."""
    url = target_url(endpoint.baseUrl, None)
    if not url:
        return False, "No base URL configured. Awaiting the JSL SAS API specification.", None
    if endpoint.authType != "NONE" and not resolve_secret(endpoint.secretEnvVar):
        return False, f"Environment variable {endpoint.secretEnvVar or '(not set)'} is empty on the server, so no credentials would be sent.", None
    client = _client_factory(float(endpoint.timeoutSeconds or 15))
    try:
        async with client as c:
            resp = await c.request("GET", url, headers=outbound_headers(endpoint))
    except Exception as e:  # noqa: BLE001
        return False, f"Could not reach {url}: {type(e).__name__}: {e}"[:500], None
    code = resp.status_code
    if code in (401, 403):
        return False, f"Reached {url} but the credentials were refused (HTTP {code}).", code
    if code >= 500:
        return False, f"Reached {url} but it answered with a server error (HTTP {code}).", code
    return True, f"Reached {url} (HTTP {code}).", code


__all__ = [
    "AUTH_TYPES", "DIRECTIONS", "EVENT_CREATED", "EVENT_STATUS_CHANGED", "JSL_SAS_TEMPLATE_MAPPING", "LOG_STATUSES",
    "MAX_ATTEMPTS", "METHODS", "SAFEOPS_FIELDS", "TRANSFORMS", "TRIGGER_EVENTS",
    "api_key_matches", "apply_transform", "auth_headers", "backoff_seconds", "build_payload", "capture_changes",
    "decide_event", "deliver_due", "deliver_row", "enqueue_manual", "map_inbound", "next_attempt_at",
    "observation_record", "preview_payload", "resolve_secret", "reverse_transform", "run_integration_sync",
    "set_http_client_factory", "test_connection", "validate_config",
    "InboundError", "build_inbound_create", "find_received", "inbound_dedupe_key",
]
