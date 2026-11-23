"""QR-code fire equipment inspections: the pure rules.

Everything here takes plain values and touches no database, so
tests/test_fire_inspection.py runs it offline. The router
(app/routers/fire_safety.py) does the I/O and calls these in order:

  scan → GET /api/fire/equipment/by-code/{code}  (asset + its current checklist)
  submit → validate_responses → summarise → status_after_inspection
         → next_due → (critical fail) CAPA via services.capa_spawn

Status model (FireEquipment.status, TEXT):
  ACTIVE / DUE_INSPECTION / OVERDUE  derived from nextInspectionDueDate
  DEFECTIVE        set by an inspection with a failed critical item; cleared by
                   the next inspection with no critical failure (repair verified)
                   or by an explicit return-to-service
  OUT_OF_SERVICE   set by a person (or by the inspector on a critical failure);
                   cleared only by an explicit return-to-service, which needs a
                   passing inspection recorded after the asset went out of service
  DECOMMISSIONED   terminal; cannot be inspected
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

EQUIPMENT_TYPES = (
    "FIRE_EXTINGUISHER", "HYDRANT", "HOSE_REEL", "SMOKE_DETECTOR", "HEAT_DETECTOR",
    "MANUAL_CALL_POINT", "FIRE_ALARM_PANEL", "SPRINKLER", "FIRE_PUMP", "FIRE_DOOR",
    "EMERGENCY_LIGHT", "GAS_SUPPRESSION",
)
EQUIPMENT_STATUSES = ("ACTIVE", "DUE_INSPECTION", "OVERDUE", "DEFECTIVE", "OUT_OF_SERVICE", "DECOMMISSIONED")
RESPONSES = ("PASS", "FAIL", "NA")
RESULTS = ("PASS", "FAIL")

# Suggested inspection interval per type when an asset is registered. The
# asset's own inspectionFrequencyDays is what drives the due date.
DEFAULT_FREQUENCY_DAYS = {
    "FIRE_EXTINGUISHER": 30, "HYDRANT": 30, "HOSE_REEL": 30, "SMOKE_DETECTOR": 90,
    "HEAT_DETECTOR": 90, "MANUAL_CALL_POINT": 90, "FIRE_ALARM_PANEL": 30, "SPRINKLER": 90,
    "FIRE_PUMP": 7, "FIRE_DOOR": 30, "EMERGENCY_LIGHT": 30, "GAS_SUPPRESSION": 90,
}

# Item photos are uploaded to storage before submit; the submit only accepts
# paths minted for that asset by POST /api/fire/inspections/upload-url.
PHOTO_ENTITY = "fire_inspection"
PHOTO_PREFIX = f"evidence/{PHOTO_ENTITY}/"

MAX_ITEMS = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── templates ────────────────────────────────────────────────────────────────
def slug_key(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:40] or "item"


def normalise_items(raw: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Clean an admin-edited item list into the stored shape
    [{key, text, critical, guidance}]. Raises ValueError with a readable reason.

    Keys are kept when given (so an item keeps its identity across versions) and
    derived from the text otherwise; duplicates get a numeric suffix."""
    items = list(raw or [])
    if not items:
        raise ValueError("A checklist needs at least one item.")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"A checklist can have at most {MAX_ITEMS} items.")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, it in enumerate(items, start=1):
        text = str((it or {}).get("text") or "").strip()
        if len(text) < 3:
            raise ValueError(f"Item {i} needs a description of at least 3 characters.")
        if len(text) > 300:
            raise ValueError(f"Item {i} is longer than 300 characters.")
        base = slug_key(str(it.get("key") or "") or text)
        key, n = base, 2
        while key in seen:
            key = f"{base[:36]}_{n}"
            n += 1
        seen.add(key)
        guidance = str(it.get("guidance") or "").strip() or None
        out.append({"key": key, "text": text, "critical": bool(it.get("critical")), "guidance": guidance})
    # A checklist with no critical item is allowed (housekeeping-only rounds);
    # the template editor warns about it.
    return out


# ── inspection submit ────────────────────────────────────────────────────────
def inspection_blockers(*, status: str | None, is_active: bool, is_deleted: bool) -> list[str]:
    out: list[str] = []
    if is_deleted:
        out.append("This equipment has been deleted.")
    if status == "DECOMMISSIONED" or not is_active:
        out.append("This equipment is decommissioned and is no longer inspected.")
    return out


def validate_responses(items: list[dict[str, Any]], responses: dict[str, dict[str, Any]]) -> list[str]:
    """Every item answered once with PASS / FAIL / NA. A FAIL needs remarks (what
    is wrong), and so does NA on a critical item (why a critical check does not
    apply). Unknown keys are refused so a stale form cannot slip answers in."""
    errors: list[str] = []
    keys = {i["key"] for i in items}
    for k in responses:
        if k not in keys:
            errors.append(f"Unknown checklist item '{k}'.")
    for idx, it in enumerate(items, start=1):
        r = responses.get(it["key"])
        if not r or not r.get("response"):
            errors.append(f"Item {idx} ('{it['text'][:50]}') is not answered.")
            continue
        resp = str(r["response"]).upper()
        if resp not in RESPONSES:
            errors.append(f"Item {idx}: response must be PASS, FAIL or NA.")
            continue
        remarks = str(r.get("remarks") or "").strip()
        if resp == "FAIL" and len(remarks) < 3:
            errors.append(f"Item {idx} failed: describe the defect in remarks.")
        if resp == "NA" and it.get("critical") and len(remarks) < 3:
            errors.append(f"Item {idx} is a critical check: say why it does not apply.")
    return errors


def summarise(items: list[dict[str, Any]], responses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    counts = {"PASS": 0, "FAIL": 0, "NA": 0}
    critical_fails: list[dict[str, Any]] = []
    for it in items:
        resp = str((responses.get(it["key"]) or {}).get("response") or "").upper()
        if resp in counts:
            counts[resp] += 1
        if resp == "FAIL" and it.get("critical"):
            critical_fails.append({
                "key": it["key"], "text": it["text"],
                "remarks": str((responses.get(it["key"]) or {}).get("remarks") or "").strip(),
            })
    return {
        "result": "FAIL" if counts["FAIL"] else "PASS",
        "passCount": counts["PASS"], "failCount": counts["FAIL"], "naCount": counts["NA"],
        "criticalFailCount": len(critical_fails), "criticalFails": critical_fails,
    }


def status_after_inspection(current: str | None, *, critical_fail_count: int, mark_out_of_service: bool) -> str | None:
    """The sticky status an inspection leaves the asset in, or None meaning
    "derive it from the new due date" (ACTIVE / DUE_INSPECTION / OVERDUE).

    * OUT_OF_SERVICE stays put whatever the result: bringing an asset back is a
      deliberate return-to-service, not a side effect of an inspection.
    * A failed critical item makes the asset DEFECTIVE, or OUT_OF_SERVICE if
      the inspector says so (e.g. an extinguisher removed from its post).
    * No critical failure clears DEFECTIVE: the repair has been verified.
    """
    if current == "OUT_OF_SERVICE":
        return "OUT_OF_SERVICE"
    if critical_fail_count > 0:
        return "OUT_OF_SERVICE" if mark_out_of_service else "DEFECTIVE"
    return None


def status_event(before: str | None, after: str | None) -> str | None:
    """Event type to log for a status change caused by an inspection."""
    if before == after:
        return None
    if after == "OUT_OF_SERVICE":
        return "OUT_OF_SERVICE"
    if after == "DEFECTIVE":
        return "MARKED_DEFECTIVE"
    if before == "DEFECTIVE":
        return "DEFECT_CLEARED"
    return None


def next_due(completed_at: datetime, frequency_days: int | None) -> datetime:
    return _aware(completed_at) + timedelta(days=max(1, int(frequency_days or 30)))


def return_to_service_blockers(
    *, status: str | None, since: datetime | None,
    last_inspection: tuple[datetime, int] | None,
) -> list[str]:
    """Reasons an asset cannot be returned to service yet.

    ``since`` is when it went OUT_OF_SERVICE / DEFECTIVE (from the event log; None
    for assets taken out before the log existed). ``last_inspection`` is the
    latest inspection as (completedAt, criticalFailCount)."""
    if status not in ("OUT_OF_SERVICE", "DEFECTIVE"):
        return [f"Only an out-of-service or defective asset can be returned to service (this one is {status})."]
    if last_inspection is None:
        return ["Record a passing inspection first: no inspection has been recorded for this asset."]
    done_at, critical_fails = last_inspection
    if since is not None and _aware(done_at) < _aware(since):
        return ["Record a passing inspection first: none has been recorded since the asset was taken out of service."]
    if critical_fails > 0:
        return ["The latest inspection failed a critical item. Repair the asset and record a passing inspection first."]
    return []


def photo_path_ok(path: str | None, equipment_id: str) -> bool:
    if not path:
        return True
    return path.startswith(f"{PHOTO_PREFIX}{equipment_id}/") and ".." not in path


def due_bucket(status: str | None, due: datetime | None, now: datetime | None = None, soon_days: int = 30) -> str:
    """OVERDUE / DUE_SOON / OK / NEVER for the due list. Out-of-service and
    decommissioned assets are not 'due': nobody is expected to inspect them."""
    if status in ("OUT_OF_SERVICE", "DECOMMISSIONED"):
        return "NOT_IN_SERVICE"
    if due is None:
        return "NEVER"
    now = now or utcnow()
    d = _aware(due)
    if d < now:
        return "OVERDUE"
    if d <= now + timedelta(days=soon_days):
        return "DUE_SOON"
    return "OK"


def capa_problem_text(*, equipment_code: str, equipment_type: str, location: str | None,
                      inspection_no: str, critical_fails: list[dict[str, Any]]) -> str:
    lines = [
        f"Fire equipment {equipment_code} ({equipment_type.replace('_', ' ').lower()}) at "
        f"{location or 'unrecorded location'} failed {len(critical_fails)} critical check(s) in inspection {inspection_no}:",
    ]
    for f in critical_fails:
        lines.append(f"- {f['text']}" + (f": {f['remarks']}" if f.get("remarks") else ""))
    return "\n".join(lines)[:2000]


__all__ = [
    "EQUIPMENT_TYPES", "EQUIPMENT_STATUSES", "RESPONSES", "RESULTS", "DEFAULT_FREQUENCY_DAYS",
    "PHOTO_ENTITY", "PHOTO_PREFIX", "slug_key", "normalise_items", "inspection_blockers",
    "validate_responses", "summarise", "status_after_inspection", "status_event", "next_due",
    "return_to_service_blockers", "photo_path_ok", "due_bucket", "capa_problem_text",
]
