"""The single control-center payload — a pure function of the snapshot.

GET /api/ptw/control-center returns exactly this (docs/ptw-control-center/PLAN.md;
shape per the build brief §4). Every widget reads from it, so one refresh is one
request. Nothing here formats for display beyond short labels; numbers stay
numbers and the UI decides how to render them.

Zero-baseline rule: a delta whose baseline is 0 is `null` (UI shows "—"),
never a percentage against nothing.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.services.ptw_ops import impairment_check as ic
from app.services.ptw_ops import simops, steps
from app.services.ptw_ops.derived import (
    RUNNING_STATUSES,
    WORKING_STATUSES,
    expired_open,
    expiring_2h,
    is_open,
    starting_2h,
)
from app.services.ptw_ops.rules import approval_sla_breach as sla_rule
from app.services.ptw_ops.rules import evaluate_all, impairment_overdue
from app.services.ptw_ops.shifts import local_day
from app.services.ptw_ops.types import ExceptionDraft, ImpairmentView, PermitView, Snapshot

# Relative risk of one permit of each type on site (type-mix "risk-weighted load").
RISK_WEIGHT = {
    "HOT_WORK": 3.0,
    "CONFINED_SPACE": 3.0,
    "ELECTRICAL_LOTO": 2.5,
    "WORK_AT_HEIGHT": 2.0,
    "LIFTING": 2.0,
    "EXCAVATION": 2.0,
    "GENERAL_COLD": 1.0,
}
TYPE_LABEL = {
    "HOT_WORK": "Hot work",
    "CONFINED_SPACE": "Confined space",
    "ELECTRICAL_LOTO": "Electrical / LOTO",
    "WORK_AT_HEIGHT": "Work at height",
    "LIFTING": "Lifting",
    "EXCAVATION": "Excavation",
    "GENERAL_COLD": "Cold work",
}
DUE_SOON_MIN = 60
ON_TIME_GRACE = timedelta(minutes=60)
MAX_LANES = 3
MAP_STATUSES = RUNNING_STATUSES  # pins: issued / active / suspended (+ expired-open)


def iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _mins(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


@dataclass
class Extras:
    """Context the loader resolves alongside the snapshot."""
    windowStart: datetime
    windowEnd: datetime
    rangeKey: str = "today"
    shiftCode: str | None = None
    areaFilter: str | None = None
    plantName: str | None = None
    plantLocation: str | None = None
    areaNames: dict[str, str] = field(default_factory=dict)
    userNames: dict[str, str] = field(default_factory=dict)
    persisted: dict[str, dict[str, Any]] = field(default_factory=dict)  # dedupeKey -> row
    brief: dict[str, Any] | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    contractorCompanies: dict[str, dict[str, Any]] = field(default_factory=dict)
    activeBaseline: int = 0
    cardsExpiring7d: int = 0
    guidanceUpdated: dict[str, str] = field(default_factory=dict)
    tablesReady: bool = True


# ── helpers ──────────────────────────────────────────────────────────────────
def zero_baseline_delta(current: int, baseline: int) -> int | None:
    """Signed difference vs the baseline; None when the baseline is 0."""
    if not baseline:
        return None
    return current - baseline


def impairment_status(imp: ImpairmentView, now: datetime) -> str:
    if imp.plannedRestoreAt is None:
        return "on_track"
    if imp.plannedRestoreAt < now:
        return "overdue"
    if imp.plannedRestoreAt <= now + timedelta(minutes=DUE_SOON_MIN):
        return "due_soon"
    return "on_track"


def auto_grid(n: int) -> list[list[dict[str, float]]]:
    """Placeholder rectangles for zones nobody has drawn yet, so the map is
    usable from day one. Display only — SIMOPS distance never uses these."""
    if n <= 0:
        return []
    cols = min(4, max(1, math.ceil(math.sqrt(n * 2))))
    rows = math.ceil(n / cols)
    gap = 2.0
    w = (100 - gap * (cols + 1)) / cols
    h = (100 - gap * (rows + 1)) / rows
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        x, y = gap + c * (w + gap), gap + r * (h + gap)
        out.append([{"x": round(x, 2), "y": round(y, 2)}, {"x": round(x + w, 2), "y": round(y, 2)},
                    {"x": round(x + w, 2), "y": round(y + h, 2)}, {"x": round(x, 2), "y": round(y + h, 2)}])
    return out


def pin_positions(polygon: list[dict[str, float]], n: int) -> list[tuple[float, float]]:
    """n deterministic pin points inside a zone's bounding box (inset so pins
    clear the zone label at top and the impairment badge at bottom-left)."""
    if n <= 0 or not polygon:
        return []
    xs = [float(p["x"]) for p in polygon]
    ys = [float(p["y"]) for p in polygon]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = max(x1 - x0, 0.1), max(y1 - y0, 0.1)
    ix0, ix1 = x0 + w * 0.14, x1 - w * 0.10
    iy0, iy1 = y0 + h * 0.34, y1 - h * 0.26
    cols = max(1, math.ceil(math.sqrt(n * (w / h))))
    rows = math.ceil(n / cols)
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        x = ix0 + (ix1 - ix0) * ((c + 0.5) / cols)
        y = iy0 + (iy1 - iy0) * ((r + 0.5) / rows) if rows > 1 else (iy0 + iy1) / 2
        out.append((round(x, 2), round(y, 2)))
    return out


def pack_lanes(bars: list[dict[str, Any]], max_lanes: int = MAX_LANES) -> tuple[list[dict[str, Any]], int]:
    """Greedy interval packing into ≤ max_lanes lanes. Returns (placed, hidden)."""
    lane_end: list[datetime] = []
    placed, hidden = [], 0
    for b in sorted(bars, key=lambda x: x["_start"]):
        for i, end in enumerate(lane_end):
            if b["_start"] >= end:
                lane_end[i] = b["_end"]
                b["lane"] = i
                break
        else:
            if len(lane_end) < max_lanes:
                lane_end.append(b["_end"])
                b["lane"] = len(lane_end) - 1
            else:
                hidden += 1
                continue
        placed.append(b)
    return placed, hidden


# ── sections ─────────────────────────────────────────────────────────────────
def _in_area(p: PermitView, area: str | None) -> bool:
    return area is None or p.areaId == area


def build(snap: Snapshot, ex: Extras) -> dict[str, Any]:
    now = snap.now
    area = ex.areaFilter
    permits = [p for p in snap.permits if _in_area(p, area)]
    in_force = [i for i in snap.impairments if i.is_in_force(now) and (area is None or i.areaId == area)]
    pending_imps = [i for i in snap.impairments if i.status == "requested" and (area is None or i.areaId == area)]
    day_start, _ = local_day(now)

    conflicts = [
        c for c in simops.detect(snap.permits, snap.simopsRules, snap.zones, snap.layout, snap.plantId)
        if area is None or area in c.areaIds
    ]
    drafts = [d for d in evaluate_all(snap) if area is None or d.areaId == area]

    name = ex.areaNames.get
    users = ex.userNames.get

    # ── KPIs ──
    active = [p for p in permits if p.status == "ACTIVE"]
    approvals = [p for p in permits if p.step in steps.APPROVAL_STEPS and is_open(p)]
    past_sla = [p for p in approvals if sla_rule.breached(snap, p)]
    starting = sorted([p for p in permits if starting_2h(p, now)], key=lambda p: p.validFrom)
    expiring = [p for p in permits if expiring_2h(p, now)]
    exp_open = [p for p in permits if expired_open(p, now)]
    exp_today = [p for p in exp_open if p.validTo >= day_start]
    working = [p for p in permits if p.status in WORKING_STATUSES and is_open(p)]
    uses_checkin = any(p.crewCheckedIn for p in snap.permits)
    workers = sum((p.crewCheckedIn if uses_checkin else p.crewCount) for p in working)
    contractors_on_site = {p.contractorCompanyId or (p.contractorName or "").strip().lower()
                           for p in working if p.contractorCompanyId or (p.contractorName or "").strip()}
    blocking = [i for i in in_force if any(
        r.systemType == i.systemType and r.effect == "block_new" and r.plantId in (None, snap.plantId)
        for r in snap.impairmentRules)]
    overdue_imps = [i for i in in_force if impairment_overdue.is_overdue(i, now)]
    suspended = [p for p in permits if p.status == "SUSPENDED"]
    reasons = Counter((p.suspendedReason or "").strip() for p in suspended if (p.suspendedReason or "").strip())
    starting_hot = [p for p in starting if p.type == "HOT_WORK"]

    kpis = {
        "activeNow": len(active),
        "pendingApproval": len(approvals),
        "pendingPastSla": len(past_sla),
        "starting2h": len(starting),
        "expiring2h": len(expiring),
        "expiredOpen": len(exp_open),
        "expiredOpenToday": len(exp_today),
        "workersUnderPermit": workers,
        "contractorsOnSite": len(contractors_on_site),
        "simopsConflicts": len(conflicts),
        "activeImpairments": len(in_force),
        "impairmentsOverdue": len(overdue_imps),
        "impairmentsBlocking": len(blocking),
        "suspended": len(suspended),
        "deltas": {"activeNow": zero_baseline_delta(len(active), ex.activeBaseline)},
        "baselines": {"activeNow": ex.activeBaseline},
        "context": {
            "nextStart": ({"type": starting[0].type, "at": iso(starting[0].validFrom)} if starting else None),
            "startingHotWork": len(starting_hot),
            "simopsLabel": (" ↔ ".join(name(a, "Unassigned") for a in conflicts[0].areaIds) if conflicts else None),
            "topSuspendedReason": (reasons.most_common(1)[0][0] if reasons else None),
        },
    }

    # ── map ──
    flagged_ids = {d.permitId for d in drafts if d.ruleKey in (
        "loto_not_verified", "gas_test_overdue", "hot_work_blocked_by_impairment", "impairment_controls_missing",
        "competency_expiring") and d.permitId}
    by_zone: dict[str, list[PermitView]] = defaultdict(list)
    for p in snap.permits:
        if p.areaId and ((p.status in MAP_STATUSES and is_open(p)) or expired_open(p, now)):
            by_zone[p.areaId].append(p)
    zones_out, pins = [], []
    ordered = sorted(snap.zones, key=lambda z: (z.sortOrder if z.sortOrder is not None else 9999, z.name))
    undrawn = [z for z in ordered if not z.polygon]
    auto = dict(zip([z.id for z in undrawn], auto_grid(len(undrawn)), strict=True)) if undrawn and len(undrawn) == len(ordered) else {}
    for z in ordered:
        polygon = z.polygon or auto.get(z.id) or []
        zp = sorted(by_zone.get(z.id, []), key=lambda p: (p.type, p.number))
        imp_codes = sorted(i.code for i in snap.impairments if i.areaId == z.id and i.is_in_force(now))
        zones_out.append({
            "id": z.id, "name": z.name, "polygon": polygon, "impairmentCodes": imp_codes,
            "activeCount": sum(1 for p in zp if p.status == "ACTIVE"), "permitCount": len(zp),
            "dimmed": area is not None and z.id != area,
        })
        for p, (x, y) in zip(zp, pin_positions(polygon, len(zp)), strict=False):
            pins.append({
                "permitId": p.id, "number": p.number, "type": p.type, "x": x, "y": y,
                "areaId": z.id, "status": p.status,
                "overdue": expired_open(p, now), "flagged": p.id in flagged_ids,
            })
    map_out = {
        "layoutImageUrl": snap.layout.imageUrl,
        "hasGeometry": any(z.polygon for z in snap.zones),
        "autoLayout": bool(auto),
        "zones": zones_out,
        "pins": pins,
        "conflicts": [
            {"areaIds": c.areaIds, "label": c.label, "permitIds": list(c.permitIds), "severity": c.severity,
             "distanceM": c.distanceM}
            for c in conflicts
        ],
    }

    # ── exceptions ──
    exceptions = []
    for d in drafts:
        row = ex.persisted.get(d.dedupeKey) or {}
        owner_id = row.get("ownerUserId") or d.ownerUserId
        exceptions.append({
            "id": row.get("id") or d.dedupeKey,
            "ruleKey": d.ruleKey,
            "severity": d.severity,
            "title": d.title,
            "detail": d.detail,
            "ref": d.ref,
            "permitId": d.permitId,
            "impairmentId": d.impairmentId,
            "areaId": d.areaId,
            "areaName": name(d.areaId or "", "Unassigned"),
            "since": iso(row.get("openedAt") or d.since),
            "owner": users(owner_id or "") if owner_id else None,
            "acknowledged": bool(row.get("acknowledgedAt")),
        })

    # ── impairments ──
    imps_out = []
    for i in sorted(in_force + pending_imps, key=lambda i: (i.status == "requested", i.plannedRestoreAt or now)):
        effect, sev = ic.effect_summary(i, snap.impairmentRules, snap.plantId)
        st = impairment_status(i, now) if i.is_in_force(now) else "awaiting_approval"
        imps_out.append({
            "id": i.id, "code": i.code, "systemType": i.systemType, "systemName": i.systemName,
            "areaId": i.areaId, "areaName": name(i.areaId or "", "Unassigned"), "reason": i.reason,
            "startedAt": iso(i.startedAt), "plannedRestoreAt": iso(i.plannedRestoreAt),
            "minutesToRestore": _mins(now, i.plannedRestoreAt) if i.plannedRestoreAt else None,
            "status": st, "lifecycle": i.status, "precautions": i.precautions,
            "effect": effect, "effectSeverity": sev,
            "approvedBy": i.approvedByName or (users(i.approvedById or "") if i.approvedById else None),
        })

    # ── pipeline ──
    at_step: dict[str, list[PermitView]] = defaultdict(list)
    for p in permits:
        if is_open(p) and p.step in steps.PIPELINE_BY_KEY:
            at_step[p.step].append(p)
    pipeline = []
    for spec in steps.PIPELINE:
        ps = at_step.get(spec.key, [])
        if spec.key == steps.APPROVAL_OTHER and not ps:
            continue
        dwell = [sla_rule.dwell_min(p, now) for p in ps]
        dwell = [d for d in dwell if d is not None]
        own = [sla_rule.sla_for(snap, spec.key, p) for p in ps]
        own = [x for x in own if x is not None]
        sla = min(own) if own else sla_rule.sla_for(snap, spec.key)
        n_breached = sum(1 for p in ps if sla_rule.breached(snap, p))
        pipeline.append({
            "step": spec.key, "label": spec.label, "count": len(ps),
            "avgDwellMin": round(sum(dwell) / len(dwell)) if dwell else None,
            "slaMin": sla, "breached": n_breached > 0, "breachedCount": n_breached, "tint": spec.tint,
        })

    # ── timeline ──
    w0, w1 = ex.windowStart, ex.windowEnd
    rows: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for p in permits:
        if p.status in ("DRAFT", "CANCELLED", "REJECTED"):
            continue
        if p.status == "CLOSED" or p.closedAt:
            kind, end = "closed", p.closedAt or p.validTo
        elif expired_open(p, now):
            kind, end = "overrun", now
        elif p.status in RUNNING_STATUSES and p.validFrom <= now:
            kind, end = "active", p.validTo
        elif p.status in ("WORK_COMPLETED", "HANDBACK_INSPECTION"):
            kind, end = "active", p.workCompletedAt or p.validTo
        else:
            kind, end = "scheduled", p.validTo
        start = p.validFrom
        if end <= w0 or start >= w1:
            continue
        rows[p.areaId].append({
            "permitId": p.id, "number": p.number, "type": p.type, "kind": kind,
            "start": iso(start), "end": iso(end), "validTo": iso(p.validTo),
            "holder": p.receiverName, "_start": start, "_end": end,
        })
    zone_order = {z.id: (z.sortOrder if z.sortOrder is not None else 9999, z.name) for z in snap.zones}
    timeline = []
    for aid in sorted(rows, key=lambda a: zone_order.get(a, (99999, name(a or "", "~")))):
        placed, hidden = pack_lanes(rows[aid])
        for b in placed:
            b.pop("_start"), b.pop("_end")
        timeline.append({"areaId": aid, "areaName": name(aid or "", "Unassigned"), "bars": placed, "hidden": hidden})

    # ── type mix (on site now: working + expired-open) ──
    on_site = [p for p in permits if (p.status in WORKING_STATUSES and is_open(p)) or expired_open(p, now)]
    mix = Counter(p.type for p in on_site)
    type_mix = sorted(
        ({"type": t, "label": TYPE_LABEL.get(t, t), "count": n, "riskWeight": RISK_WEIGHT.get(t, 1.0),
          "load": round(n * RISK_WEIGHT.get(t, 1.0), 1)} for t, n in mix.items()),
        key=lambda m: (-m["load"], m["type"]),
    )

    # ── compliance ──
    def pct(num: int, den: int) -> float | None:
        return round(100.0 * num / den, 1) if den else None

    gas_active = [p for p in permits if p.status == "ACTIVE" and p.gasRequired]
    gas_ok = [p for p in gas_active if p.lastGasAt and not (p.gasRefreshDueBy and now > p.gasRefreshDueBy)]
    iso_permits = [p for p in permits if p.status in ("ISSUED", "ACTIVE") and p.isolationsTotal]
    iso_total = sum(p.isolationsTotal for p in iso_permits)
    iso_ok = iso_total - sum(p.isolationsUnverified for p in iso_permits)
    closed_w = [p for p in permits if p.closedAt and w0 <= p.closedAt < w1]
    closed_on_time = [p for p in closed_w if p.closedAt <= p.validTo + ON_TIME_GRACE]
    crew_running = [p for p in permits if p.status in RUNNING_STATUSES and is_open(p)]
    crew_total = sum(p.crewCount for p in crew_running)
    crew_exp = sum(p.crewCertExpiringCount for p in crew_running)
    flra_need = [p for p in permits if p.status == "ACTIVE" and p.flraRequired]
    flra_ok = [p for p in flra_need if p.hasActiveFlra]
    compliance = [
        {"key": "gas_on_time", "label": "Gas tests on time", "pct": pct(len(gas_ok), len(gas_active)),
         "num": len(gas_ok), "den": len(gas_active)},
        {"key": "isolations_verified", "label": "Isolations verified", "pct": pct(iso_ok, iso_total),
         "num": iso_ok, "den": iso_total},
        {"key": "approvals_in_sla", "label": "Approvals within SLA",
         "pct": pct(len(approvals) - len(past_sla), len(approvals)),
         "num": len(approvals) - len(past_sla), "den": len(approvals)},
        {"key": "closed_on_time", "label": "Closed on time", "pct": pct(len(closed_on_time), len(closed_w)),
         "num": len(closed_on_time), "den": len(closed_w)},
        {"key": "crew_competent", "label": "Crew competency current",
         "pct": pct(crew_total - crew_exp, crew_total), "num": crew_total - crew_exp, "den": crew_total},
        {"key": "flra_done", "label": "FLRA completed", "pct": pct(len(flra_ok), len(flra_need)),
         "num": len(flra_ok), "den": len(flra_need)},
    ]

    # ── contractors & people ──
    sev_by_permit: dict[str, list[str]] = defaultdict(list)
    for d in drafts:
        if d.permitId:
            sev_by_permit[d.permitId].append(d.severity)
    groups: dict[str, dict[str, Any]] = {}
    for p in crew_running:
        key = p.contractorCompanyId or ("name:" + (p.contractorName or "").strip().lower()
                                         if (p.contractorName or "").strip() else "in-house")
        g = groups.setdefault(key, {"companyId": p.contractorCompanyId, "name": None, "permits": 0,
                                    "workers": 0, "sev": [], "expiring": 0})
        co = ex.contractorCompanies.get(p.contractorCompanyId or "") if p.contractorCompanyId else None
        g["name"] = (co or {}).get("name") or (p.contractorName or "").strip() or "In-house"
        g["permits"] += 1
        g["workers"] += p.crewCheckedIn if uses_checkin else p.crewCount
        g["sev"].extend(sev_by_permit.get(p.id, []))
        g["expiring"] += p.crewCertExpiringCount
    contractors = []
    for g in groups.values():
        co = ex.contractorCompanies.get(g["companyId"] or "") or {}
        flag, fsev = None, None
        if (co.get("status") or "ACTIVE") != "ACTIVE":
            flag, fsev = f"Company {str(co.get('status')).lower()}", "critical"
        elif "critical" in g["sev"]:
            n = g["sev"].count("critical")
            flag, fsev = f"{n} critical exception{'s' if n != 1 else ''}", "critical"
        elif g["expiring"]:
            flag, fsev = f"{g['expiring']} card{'s' if g['expiring'] != 1 else ''} expiring", "warning"
        elif co.get("score") is not None and int(co["score"]) < 70:
            flag, fsev = f"Safety score {co['score']}", "warning"
        elif "high" in g["sev"]:
            flag, fsev = "Open high exception", "warning"
        contractors.append({"name": g["name"], "permits": g["permits"], "workers": g["workers"],
                            "flag": flag, "flagSeverity": fsev})
    contractors.sort(key=lambda c: (-c["workers"], c["name"]))

    signals = dict(ex.signals)
    signals.setdefault("weekly", [])
    signals.setdefault("expiryRate", {"expired": 0, "total": 0})

    return {
        "generatedAt": iso(now),
        "plant": {"id": snap.plantId, "name": ex.plantName, "location": ex.plantLocation},
        "filters": {"area": area, "shift": ex.shiftCode, "range": ex.rangeKey,
                    "windowStart": iso(w0), "windowEnd": iso(w1)},
        "shifts": [{"code": s["code"], "label": s.get("label"), "startHour": s["startHour"], "endHour": s["endHour"]}
                   for s in snap.shifts],
        "areas": [{"id": z.id, "name": z.name} for z in sorted(
            snap.zones, key=lambda z: (z.sortOrder if z.sortOrder is not None else 9999, z.name))],
        "brief": ex.brief,
        "kpis": kpis,
        "map": map_out,
        "exceptions": exceptions,
        "impairments": imps_out,
        "impairmentRules": ic.rule_cards([r for r in snap.impairmentRules if r.plantId in (None, snap.plantId)]),
        "pipeline": pipeline,
        "timeline": timeline,
        "typeMix": type_mix,
        "compliance": compliance,
        "contractors": contractors,
        "muster": {"workers": workers, "cardsExpiring7d": ex.cardsExpiring7d},
        "signals": signals,
        "setup": {"tablesReady": ex.tablesReady, "hasGeometry": map_out["hasGeometry"]},
    }


def drafts_for(snap: Snapshot) -> list[ExceptionDraft]:
    return evaluate_all(snap)


__all__ = ["Extras", "build", "drafts_for", "zero_baseline_delta", "pin_positions", "pack_lanes",
           "impairment_status"]
