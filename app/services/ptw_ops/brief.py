"""ptw/shift-brief — three sentences for the top of the control center.

Input to the model is COUNTS ONLY, built from the control-center payload:
no names, no user ids, no permit numbers (redact_payload also runs on the way
out of anthropic_client). The model is told to use numbers from the input only
and to wrap the single most serious risk in [[double brackets]] so the UI can
highlight it. Cached by a hash of the inputs: an unchanged site does not call
the model again. Model name/latency are logged server-side, never returned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ptw_ops import TENANT_DEFAULT, PtwShiftBrief
from app.services.ptw_ops import loaders
from app.services.ptw_ops.payload import build
from app.services.ptw_ops.shifts import current_shift

log = logging.getLogger("safeops360.ptw_ops.brief")
MAX_AGE_S = 3600

SYSTEM = (
    "You write the shift brief for an industrial Permit-to-Work control room. "
    "Write AT MOST 3 short sentences: (1) the headline counts, (2) the single most serious risk, "
    "(3) the bottleneck. Use ONLY numbers and facts present in the JSON input — never estimate, "
    "never speculate, never add advice. Wrap the single most serious risk phrase in [[ and ]]. "
    'Reply as JSON: {"text": "..."}'
)


def brief_inputs(payload: dict[str, Any]) -> dict[str, Any]:
    """The redacted, counts-only view of the payload the model sees."""
    k = payload["kpis"]
    worst_step = max(
        (s for s in payload["pipeline"] if s["count"]),
        key=lambda s: (s["breachedCount"], s["avgDwellMin"] or 0), default=None,
    )
    return {
        "activePermits": k["activeNow"], "workersUnderPermit": k["workersUnderPermit"],
        "contractorsOnSite": k["contractorsOnSite"], "pendingApproval": k["pendingApproval"],
        "pendingPastSla": k["pendingPastSla"], "expiredNotClosed": k["expiredOpen"],
        "expiredNotClosedToday": k["expiredOpenToday"], "expiringWithin2h": k["expiring2h"],
        "suspended": k["suspended"], "simopsConflicts": [
            {"areas": [next((a["name"] for a in payload["areas"] if a["id"] == aid), "Unassigned")
                       for aid in c["areaIds"]], "label": c["label"]}
            for c in payload["map"]["conflicts"][:3]
        ],
        "impairmentsInForce": [
            {"code": i["code"], "system": i["systemName"], "zone": i["areaName"], "status": i["status"],
             "effect": i["effect"]}
            for i in payload["impairments"] if i["status"] != "awaiting_approval"
        ][:5],
        "openExceptionsBySeverity": {
            s: sum(1 for e in payload["exceptions"] if e["severity"] == s) for s in ("critical", "high", "medium", "low")
        },
        "topExceptions": [{"severity": e["severity"], "title": e["title"], "zone": e["areaName"]}
                          for e in payload["exceptions"][:5]],
        "bottleneck": ({"step": worst_step["label"], "count": worst_step["count"],
                        "avgWaitMin": worst_step["avgDwellMin"], "slaMin": worst_step["slaMin"],
                        "pastSla": worst_step["breachedCount"]} if worst_step else None),
    }


def _fmt_min(m: int | None) -> str:
    if not m:
        return "0 m"
    h, mm = divmod(int(m), 60)
    return f"{h}h {mm:02d}m" if h else f"{mm} m"


def template_text(inputs: dict[str, Any]) -> str:
    """Deterministic brief from the same inputs — used when AI is not configured
    or the model call fails, so the banner never depends on the model."""
    a, w = inputs["activePermits"], inputs["workersUnderPermit"]
    parts = [f"{a} permit{'s' if a != 1 else ''} active with {w} worker{'s' if w != 1 else ''} under permit."]
    sx = inputs["simopsConflicts"]
    imps = [i for i in inputs["impairmentsInForce"] if i["status"] == "overdue"] or inputs["impairmentsInForce"]
    if sx:
        parts.append(f"[[SIMOPS conflict: {' and '.join(sx[0]['areas'])} ({sx[0]['label'].replace('SIMOPS · ', '')})]].")
    elif inputs["expiredNotClosed"]:
        n = inputs["expiredNotClosed"]
        parts.append(f"[[{n} permit{'s' if n != 1 else ''} expired without closure]].")
    elif imps:
        parts.append(f"[[{imps[0]['system']} impaired at {imps[0]['zone']}]] — {imps[0]['effect'].lower()}.")
    b = inputs["bottleneck"]
    if b and b["pastSla"]:
        parts.append(f"{b['step']} is the bottleneck: {b['count']} waiting an average of "
                     f"{_fmt_min(b['avgWaitMin'])} against a {_fmt_min(b['slaMin'])} SLA.")
    elif inputs["expiredNotClosed"] and sx:
        n = inputs["expiredNotClosed"]
        parts.append(f"{n} permit{'s' if n != 1 else ''} expired without closure.")
    return " ".join(parts[:3])


def inputs_hash(inputs: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


async def generate(db: AsyncSession, plant_id: str, *, now: datetime | None = None,
                   force: bool = False) -> dict[str, Any]:
    from app.services.ai.anthropic_client import complete_json, is_configured

    s = await loaders.schema(db)
    if not s["PtwShiftBrief"]:
        return {"plantId": plant_id, "skipped": "PtwShiftBrief table not created yet"}
    now = now or datetime.now(timezone.utc)
    code, shift_start, _ = current_shift(now)
    snap, ex = await loaders.load(db, plant_id=plant_id, now=now)
    inputs = brief_inputs(build(snap, ex))
    h = inputs_hash(inputs)

    last = (await db.execute(
        select(PtwShiftBrief).where(PtwShiftBrief.plantId == plant_id)
        .where(PtwShiftBrief.tenantId == TENANT_DEFAULT).order_by(PtwShiftBrief.generatedAt.desc()).limit(1)
    )).scalar_one_or_none()
    if last and not force:
        fresh = (now - last.generatedAt).total_seconds() < MAX_AGE_S and last.generatedAt >= shift_start
        if last.inputsHash == h or fresh:
            return {"plantId": plant_id, "cached": True}

    text_out = None
    if is_configured():
        t0 = time.monotonic()
        out = await complete_json(system=SYSTEM, user=json.dumps(inputs, default=str), max_tokens=300)
        text_out = (out or {}).get("text") if isinstance(out, dict) else None
        log.info("ptw shift brief plant=%s model_ok=%s latency_ms=%d", plant_id, bool(text_out),
                 int((time.monotonic() - t0) * 1000))
    if not text_out:
        text_out = template_text(inputs)
    db.add(PtwShiftBrief(plantId=plant_id, shift=code, generatedAt=now, text=str(text_out).strip()[:1200],
                         inputsHash=h))
    await db.flush()
    return {"plantId": plant_id, "written": 1}
