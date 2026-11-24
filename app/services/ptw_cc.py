"""PTW realtime control center + insights (JSL Hisar PTW requirement).

Two read models over the existing permit tables. Nothing here writes, and
nothing needs a new table: every figure is derived on read from columns the
PTW engine already maintains.

* Control center: the permits that are live on site right now (ISSUED /
  ACTIVE / SUSPENDED, plus APPROVED awaiting issue, plus EXPIRED permits that
  were never closed out), grouped by area, with an exception list.
* Insights: counts, cycle times and compliance rates over a window of permits
  raised between two dates.

Layout: the rules are pure functions over plain dataclasses (unit-tested in
tests/test_ptw_cc.py); the async loaders at the bottom only fetch rows.

Exception rules (severity in brackets)
  EXPIRED_NOT_CLOSED      [critical] validity over, permit still open
  GAS_TEST_OVERDUE        [critical] active permit, last reading's refreshDueBy passed
  GAS_EXCEEDANCE          [critical] latest reading exceeded a limit and the permit is still active
  GAS_TEST_MISSING        [high]     active permit that needs gas testing has no reading at all
  EXPIRING_SOON           [high/medium] validity ends within 60 min (high when ACTIVE)
  SIMOPS_CONFLICT         [high/medium] two live permits overlap in the same area, or the
                                     post-closure engine recorded a conflict
  SUSPENDED               [medium]   permit is suspended
  ISSUED_NOT_ACCEPTED     [medium]   issued 30+ min after planned start, receiver has not accepted
  GAS_TEST_DUE            [medium]   refresh due within 15 min
  LAPSED_BEFORE_ISSUE     [medium]   APPROVED permit whose window ended before issue
  APPROVED_AWAITING_ISSUE [low]      APPROVED permit whose window has started
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import (
    Permit,
    PermitCrewMember,
    PermitExtension,
    PermitGasTestPlan,
    PermitGasTestReading,
    PermitStatus,
    PermitSuspension,
)

# ── vocabulary + thresholds ──────────────────────────────────────────────────
WORKING_STATUSES = ("ISSUED", "ACTIVE", "SUSPENDED")
LIVE_STATUSES = ("APPROVED", *WORKING_STATUSES)
HIGH_RISK_TYPES = frozenset({"HOT_WORK", "CONFINED_SPACE", "ELECTRICAL_LOTO"})

EXPIRING_WINDOW_MIN = 60
GAS_DUE_SOON_MIN = 15
ISSUE_ACCEPT_GRACE_MIN = 30
ON_TIME_CLOSURE_GRACE_MIN = 60
LIVE_LIMIT = 1500
INSIGHT_LIMIT = 20000

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Plant-local clock for hour-of-day and shift buckets. India has no DST, so a
# fixed offset is exact (and avoids needing tzdata on Windows hosts).
LOCAL_TZ = timezone(timedelta(minutes=int(os.getenv("PTW_CC_TZ_OFFSET_MIN", "330"))))

# Used when the plant has no SHIFT master rows. A/B/C is the usual 8-hour
# rotation at an Indian integrated steel plant.
DEFAULT_SHIFTS: list[dict[str, Any]] = [
    {"code": "A", "label": "Shift A (06-14)", "startHour": 6, "endHour": 14},
    {"code": "B", "label": "Shift B (14-22)", "startHour": 14, "endHour": 22},
    {"code": "C", "label": "Shift C (22-06)", "startHour": 22, "endHour": 6},
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(d: datetime | None) -> datetime | None:
    """DB datetimes come back aware from Postgres but naive from SQLite."""
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def enum_value(v: Any) -> str | None:
    if v is None:
        return None
    return str(getattr(v, "value", v))


def minutes_between(a: datetime, b: datetime) -> int:
    """Whole minutes from a to b (negative when b is before a)."""
    return int((b - a).total_seconds() // 60)


def iso(d: datetime | None) -> str | None:
    d = as_utc(d)
    return d.isoformat() if d else None


# ── control center: snapshot + rules ────────────────────────────────────────
@dataclass
class PermitSnap:
    id: str
    number: str
    type: str
    status: str
    plantId: str
    areaId: str | None
    validFrom: datetime
    validTo: datetime
    issuerId: str | None = None
    receiverId: str | None = None
    location: str | None = None
    contractorName: str | None = None
    conflictingPermitIds: list[str] = field(default_factory=list)
    gasTestRequired: bool = False
    hasGasPlan: bool = False
    lastGasAt: datetime | None = None
    gasRefreshDueBy: datetime | None = None
    lastGasExceedance: bool = False
    crewCount: int = 0
    suspendedAt: datetime | None = None
    suspendedReason: str | None = None
    closureType: str | None = None
    issuedAt: datetime | None = None
    activatedAt: datetime | None = None

    @property
    def gas_required(self) -> bool:
        return bool(self.gasTestRequired or self.hasGasPlan)

    @property
    def expired_open(self) -> bool:
        return self.status == "EXPIRED" and not self.closureType


@dataclass
class CCException:
    code: str
    severity: str
    permitId: str
    permitNumber: str
    areaId: str | None
    title: str
    detail: str | None = None
    at: datetime | None = None          # the deadline / event the rule is about
    minutes: int | None = None          # minutes over (positive) or remaining
    relatedPermitIds: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "severity": self.severity, "permitId": self.permitId,
            "permitNumber": self.permitNumber, "areaId": self.areaId, "title": self.title,
            "detail": self.detail, "at": iso(self.at), "minutes": self.minutes,
            "relatedPermitIds": self.relatedPermitIds,
        }


def windows_overlap(a_from: datetime, a_to: datetime, b_from: datetime, b_to: datetime) -> bool:
    """Half-open overlap: a permit ending at 10:00 does not clash with one starting at 10:00."""
    return a_from < b_to and b_from < a_to


def simops_pairs(snaps: Iterable[PermitSnap]) -> dict[str, set[str]]:
    """Permit id → ids of live permits it conflicts with.

    Two sources, unioned: (1) computed on read — two live permits in the same
    area whose validity windows overlap; (2) `conflictingPermitIds` the
    post-closure engine stored, kept only while the other permit is live too.
    Permits with no area cannot be placed, so they never match on (1)."""
    live = [s for s in snaps if s.status in LIVE_STATUSES]
    ids = {s.id for s in live}
    out: dict[str, set[str]] = defaultdict(set)
    by_area: dict[str, list[PermitSnap]] = defaultdict(list)
    for s in live:
        if s.areaId:
            by_area[s.areaId].append(s)
    for group in by_area.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if windows_overlap(a.validFrom, a.validTo, b.validFrom, b.validTo):
                    out[a.id].add(b.id)
                    out[b.id].add(a.id)
    for s in live:
        for other in s.conflictingPermitIds or []:
            if other in ids and other != s.id:
                out[s.id].add(other)
                out[other].add(s.id)
    return dict(out)


def simops_severity(a: PermitSnap, b: PermitSnap) -> str:
    """High when crews are (or could be) working side by side on high-risk
    work: both permits past issue and at least one high-risk type."""
    both_on_site = a.status in WORKING_STATUSES and b.status in WORKING_STATUSES
    risky = a.type in HIGH_RISK_TYPES or b.type in HIGH_RISK_TYPES
    return "high" if both_on_site and risky else "medium"


def gas_state(s: PermitSnap, now: datetime) -> str:
    """N/A | MISSING | OVERDUE | DUE | OK — only meaningful on ACTIVE permits."""
    if not s.gas_required or s.status != "ACTIVE":
        return "N/A"
    if s.lastGasAt is None:
        return "MISSING"
    if s.gasRefreshDueBy is None:
        return "OK"
    left = minutes_between(now, s.gasRefreshDueBy)
    if left <= 0:
        return "OVERDUE"
    if left <= GAS_DUE_SOON_MIN:
        return "DUE"
    return "OK"


def evaluate_permit(s: PermitSnap, now: datetime) -> list[CCException]:
    out: list[CCException] = []

    def add(code: str, severity: str, title: str, *, detail: str | None = None,
            at: datetime | None = None, minutes: int | None = None) -> None:
        out.append(CCException(code, severity, s.id, s.number, s.areaId, title, detail, at, minutes))

    remaining = minutes_between(now, s.validTo)

    if s.expired_open or (s.status in WORKING_STATUSES and remaining <= 0):
        over = -remaining
        add("EXPIRED_NOT_CLOSED", "critical",
            f"Validity ended {fmt_minutes(over)} ago and the permit is not closed",
            detail=("Auto-expired. Close it out or withdraw it." if s.expired_open
                    else f"Still {s.status.lower()} past its validity."),
            at=s.validTo, minutes=over)
    elif s.status in WORKING_STATUSES and remaining <= EXPIRING_WINDOW_MIN:
        add("EXPIRING_SOON", "high" if s.status == "ACTIVE" else "medium",
            f"Validity ends in {fmt_minutes(remaining)}", at=s.validTo, minutes=remaining)

    if s.status == "APPROVED":
        if remaining <= 0:
            add("LAPSED_BEFORE_ISSUE", "medium", "Approved but the validity window ended before issue",
                at=s.validTo, minutes=-remaining)
        elif s.validFrom <= now:
            add("APPROVED_AWAITING_ISSUE", "low", "Approved and due to start, not yet issued",
                at=s.validFrom, minutes=minutes_between(s.validFrom, now))

    if s.status == "ISSUED" and remaining > 0:
        late = minutes_between(s.validFrom, now)
        if late >= ISSUE_ACCEPT_GRACE_MIN:
            add("ISSUED_NOT_ACCEPTED", "medium",
                f"Issued, receiver has not accepted ({fmt_minutes(late)} after planned start)",
                at=s.validFrom, minutes=late)

    if s.status == "SUSPENDED":
        since = minutes_between(s.suspendedAt, now) if s.suspendedAt else None
        add("SUSPENDED", "medium",
            "Suspended" + (f" for {fmt_minutes(since)}" if since is not None and since >= 0 else ""),
            detail=s.suspendedReason, at=s.suspendedAt, minutes=since)

    gs = gas_state(s, now)
    if gs == "MISSING":
        add("GAS_TEST_MISSING", "high", "No gas test recorded on an active permit that needs one")
    elif gs == "OVERDUE":
        over = minutes_between(s.gasRefreshDueBy, now)  # type: ignore[arg-type]
        add("GAS_TEST_OVERDUE", "critical", f"Gas re-test overdue by {fmt_minutes(over)}",
            at=s.gasRefreshDueBy, minutes=over)
    elif gs == "DUE":
        left = minutes_between(now, s.gasRefreshDueBy)  # type: ignore[arg-type]
        add("GAS_TEST_DUE", "medium", f"Gas re-test due in {fmt_minutes(left)}",
            at=s.gasRefreshDueBy, minutes=left)
    if s.status == "ACTIVE" and s.lastGasExceedance:
        add("GAS_EXCEEDANCE", "critical", "Latest gas reading exceeded a limit and the permit is still active",
            at=s.lastGasAt)
    return out


def evaluate(snaps: list[PermitSnap], now: datetime) -> tuple[list[CCException], dict[str, set[str]]]:
    """All exceptions, worst first, plus the SIMOPS map used by the cards."""
    out: list[CCException] = []
    for s in snaps:
        out.extend(evaluate_permit(s, now))
    pairs = simops_pairs(snaps)
    by_id = {s.id: s for s in snaps}
    seen: set[tuple[str, str]] = set()
    for pid, others in pairs.items():
        for oid in others:
            key = (pid, oid) if pid < oid else (oid, pid)
            if key in seen:
                continue
            seen.add(key)
            a, b = by_id[key[0]], by_id[key[1]]
            same_area = a.areaId and a.areaId == b.areaId
            out.append(CCException(
                "SIMOPS_CONFLICT", simops_severity(a, b), a.id, a.number, a.areaId,
                f"Simultaneous operations: {a.number} and {b.number}",
                detail=(f"{label_type(a.type)} and {label_type(b.type)} overlap in the same area"
                        if same_area else "Conflict recorded by the permit engine"),
                relatedPermitIds=[b.id],
            ))
    out.sort(key=lambda e: (SEVERITY_ORDER.get(e.severity, 9), -(e.minutes or 0) if e.severity == "critical" else (e.minutes or 0)))
    return out, pairs


def fmt_minutes(m: int | None) -> str:
    if m is None:
        return "-"
    m = abs(int(m))
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    if h < 48:
        return f"{h} h {mm} min" if mm else f"{h} h"
    d, hh = divmod(h, 24)
    return f"{d} d {hh} h" if hh else f"{d} d"


_TYPE_LABELS = {
    "HOT_WORK": "Hot work", "CONFINED_SPACE": "Confined space", "WORK_AT_HEIGHT": "Work at height",
    "EXCAVATION": "Excavation", "ELECTRICAL_LOTO": "Electrical / LOTO", "LIFTING": "Lifting",
    "GENERAL_COLD": "General cold work",
}


def label_type(t: str | None) -> str:
    return _TYPE_LABELS.get(t or "", (t or "-").replace("_", " ").title())


def build_payload(
    snaps: list[PermitSnap], *, now: datetime, area_names: dict[str, tuple[str, str | None]],
    user_names: dict[str, str], plant_names: dict[str, str], closed_last_24h: int,
    truncated: bool = False,
) -> dict[str, Any]:
    """The single control-center payload. `area_names` maps areaId → (name, plantId)."""
    exceptions, pairs = evaluate(snaps, now)
    worst_by_permit: dict[str, str] = {}
    codes_by_permit: dict[str, list[str]] = defaultdict(list)
    for e in exceptions:
        for pid in (e.permitId, *e.relatedPermitIds):
            codes_by_permit[pid].append(e.code)
            cur = worst_by_permit.get(pid)
            if cur is None or SEVERITY_ORDER[e.severity] < SEVERITY_ORDER[cur]:
                worst_by_permit[pid] = e.severity
    number_by_id = {s.id: s.number for s in snaps}

    def card(s: PermitSnap) -> dict[str, Any]:
        return {
            "id": s.id, "number": s.number, "type": s.type, "typeLabel": label_type(s.type),
            "status": s.status, "areaId": s.areaId, "location": s.location,
            "issuerName": user_names.get(s.issuerId or ""), "receiverName": user_names.get(s.receiverId or ""),
            "contractorName": s.contractorName,
            "validFrom": iso(s.validFrom), "validTo": iso(s.validTo),
            "minutesRemaining": minutes_between(now, s.validTo),
            "crewCount": s.crewCount if s.status in WORKING_STATUSES else 0,
            "gas": {"required": s.gas_required, "state": gas_state(s, now),
                    "lastAt": iso(s.lastGasAt), "dueBy": iso(s.gasRefreshDueBy)},
            "suspendedReason": s.suspendedReason if s.status == "SUSPENDED" else None,
            "simopsWith": sorted(number_by_id[o] for o in pairs.get(s.id, set()) if o in number_by_id),
            "flags": sorted(set(codes_by_permit.get(s.id, []))),
            "worstSeverity": worst_by_permit.get(s.id),
        }

    groups: dict[str | None, list[PermitSnap]] = defaultdict(list)
    for s in snaps:
        groups[s.areaId].append(s)
    areas = []
    for area_id, items in groups.items():
        name, plant_id = area_names.get(area_id or "", ("No area recorded", None))
        plant_id = plant_id or (items[0].plantId if items else None)
        items.sort(key=lambda s: (SEVERITY_ORDER.get(worst_by_permit.get(s.id, ""), 9), s.validTo))
        sev = [worst_by_permit[s.id] for s in items if s.id in worst_by_permit]
        areas.append({
            "areaId": area_id, "areaName": name, "plantId": plant_id, "plantName": plant_names.get(plant_id or ""),
            "counts": dict(Counter(s.status for s in items)),
            "workers": sum(s.crewCount for s in items if s.status == "ACTIVE"),
            "worstSeverity": min(sev, key=lambda x: SEVERITY_ORDER[x]) if sev else None,
            "permits": [card(s) for s in items],
        })
    areas.sort(key=lambda a: (SEVERITY_ORDER.get(a["worstSeverity"] or "", 9), a["areaId"] is None, a["areaName"].lower()))

    sev_counts = Counter(e.severity for e in exceptions)
    status_counts = Counter(s.status for s in snaps)
    ex_dicts = []
    for e in exceptions:
        d = e.as_dict()
        d["areaName"] = area_names.get(e.areaId or "", ("No area recorded", None))[0]
        d["relatedPermitNumbers"] = [number_by_id.get(r, r) for r in e.relatedPermitIds]
        ex_dicts.append(d)
    return {
        "generatedAt": now.isoformat(),
        "truncated": truncated,
        "kpis": {
            "livePermits": sum(status_counts[s] for s in WORKING_STATUSES),
            "active": status_counts["ACTIVE"],
            "issued": status_counts["ISSUED"],
            "suspended": status_counts["SUSPENDED"],
            "awaitingIssue": status_counts["APPROVED"],
            "workersOnSite": sum(s.crewCount for s in snaps if s.status == "ACTIVE"),
            "expiringSoon": sum(1 for e in exceptions if e.code == "EXPIRING_SOON"),
            "expiredNotClosed": sum(1 for e in exceptions if e.code == "EXPIRED_NOT_CLOSED"),
            "gasOverdue": sum(1 for e in exceptions if e.code in ("GAS_TEST_OVERDUE", "GAS_TEST_MISSING")),
            "simopsConflicts": sum(1 for e in exceptions if e.code == "SIMOPS_CONFLICT"),
            "critical": sev_counts["critical"],
            "high": sev_counts["high"],
            "closedLast24h": closed_last_24h,
        },
        "areas": areas,
        "exceptions": ex_dicts,
        "thresholds": {
            "expiringWithinMinutes": EXPIRING_WINDOW_MIN, "gasDueSoonMinutes": GAS_DUE_SOON_MIN,
            "issueAcceptGraceMinutes": ISSUE_ACCEPT_GRACE_MIN,
        },
    }


# ── insights: pure maths ─────────────────────────────────────────────────────
@dataclass
class InsightRow:
    id: str
    type: str
    status: str
    createdAt: datetime
    validFrom: datetime
    validTo: datetime
    areaId: str | None = None
    contractorCompanyId: str | None = None
    contractorName: str | None = None
    issuerId: str | None = None
    receiverId: str | None = None
    approvedAt: datetime | None = None
    issuedAt: datetime | None = None
    activatedAt: datetime | None = None
    workCompletedAt: datetime | None = None
    closedAt: datetime | None = None
    closureType: str | None = None
    gasRequired: bool = False


def final_approval_at(issuer: datetime | None, safety: datetime | None, plant_head: datetime | None,
                      issued: datetime | None) -> datetime | None:
    """Last approval stamp. Permits whose chain wrote none of the legacy
    columns fall back to issuedAt: issue fires automatically on final approval."""
    stamps = [as_utc(d) for d in (issuer, safety, plant_head) if d is not None]
    return max(stamps) if stamps else as_utc(issued)


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..100); None for no values."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def cycle_stats(hours: list[float]) -> dict[str, Any]:
    good = [h for h in hours if h is not None and h >= 0]
    r = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {
        "n": len(good),
        "medianHours": r(percentile(good, 50)),
        "p90Hours": r(percentile(good, 90)),
        "meanHours": r(sum(good) / len(good)) if good else None,
    }


CYCLE_STAGES: list[tuple[str, str, str, str]] = [
    ("submitToApproved", "Submitted to approved", "createdAt", "approvedAt"),
    ("approvedToIssued", "Approved to issued", "approvedAt", "issuedAt"),
    ("issuedToActivated", "Issued to activated (receiver accepts)", "issuedAt", "activatedAt"),
    ("activatedToWorkCompleted", "Activated to work completed", "activatedAt", "workCompletedAt"),
    ("workCompletedToClosed", "Work completed to closed", "workCompletedAt", "closedAt"),
    ("endToEnd", "Submitted to closed", "createdAt", "closedAt"),
]


def cycle_times(rows: list[InsightRow]) -> list[dict[str, Any]]:
    out = []
    for key, label, a, b in CYCLE_STAGES:
        hours = []
        for r in rows:
            start, end = as_utc(getattr(r, a)), as_utc(getattr(r, b))
            if start and end:
                hours.append((end - start).total_seconds() / 3600.0)
        out.append({"key": key, "label": label, **cycle_stats(hours)})
    return out


def shift_for(hour: int, shifts: list[dict[str, Any]]) -> str | None:
    for s in shifts:
        start, end = s.get("startHour"), s.get("endHour")
        if start is None or end is None:
            continue
        if start < end and start <= hour < end:
            return s["label"]
        if start >= end and (hour >= start or hour < end):  # wraps midnight
            return s["label"]
    return None


def gas_compliance(rows: list[InsightRow], readings: dict[str, list[tuple]], now: datetime) -> dict[str, Any]:
    """readings: permitId → [(recordedAt, refreshDueBy, isExceedance, isPreEntry)] sorted by time.

    * initial test: a pre-entry reading, or any reading within 15 min of activation
    * refresh on time: each reading taken no later than the previous reading's
      refreshDueBy; a final refreshDueBy that fell due before the work ended
      with no later reading counts as one missed refresh."""
    required = [r for r in rows if r.gasRequired and r.activatedAt]
    with_initial = evaluated = on_time = exceedances = readings_n = 0
    for r in required:
        rs = readings.get(r.id, [])
        readings_n += len(rs)
        exceedances += sum(1 for x in rs if x[2])
        act = as_utc(r.activatedAt)
        if any(x[3] or (act and as_utc(x[0]) <= act + timedelta(minutes=15)) for x in rs):
            with_initial += 1
        for prev, cur in zip(rs, rs[1:]):
            due = as_utc(prev[1])
            if due is None:
                continue
            evaluated += 1
            if as_utc(cur[0]) <= due:
                on_time += 1
        if rs:
            last_due = as_utc(rs[-1][1])
            work_end = as_utc(r.workCompletedAt) or as_utc(r.closedAt) or min(now, as_utc(r.validTo))
            if last_due is not None and last_due < work_end:
                evaluated += 1  # a refresh fell due while work continued and none was recorded
    pct = lambda a, b: round(100.0 * a / b, 1) if b else None  # noqa: E731
    return {
        "permitsRequiringGasTest": len(required),
        "permitsWithInitialTest": with_initial,
        "initialTestPct": pct(with_initial, len(required)),
        "readings": readings_n,
        "refreshesEvaluated": evaluated,
        "refreshesOnTime": on_time,
        "refreshCompliancePct": pct(on_time, evaluated),
        "exceedances": exceedances,
    }


def on_time_closure(rows: list[InsightRow], grace_min: int = ON_TIME_CLOSURE_GRACE_MIN) -> dict[str, Any]:
    """Of permits closed after real work (closureType WORK_COMPLETED, or a
    legacy CLOSED row with no closureType), the share closed within `grace_min`
    of the (final, post-extension) validTo."""
    closed = [r for r in rows if r.closedAt and (r.closureType in (None, "WORK_COMPLETED"))
              and r.status == "CLOSED"]
    on_time = sum(1 for r in closed if as_utc(r.closedAt) <= as_utc(r.validTo) + timedelta(minutes=grace_min))
    return {
        "closed": len(closed), "onTime": on_time, "late": len(closed) - on_time,
        "onTimePct": round(100.0 * on_time / len(closed), 1) if closed else None,
        "graceMinutes": grace_min,
        "expiredNotClosed": sum(1 for r in rows if r.status == "EXPIRED" and not r.closureType),
    }


def _period_key(d: datetime, by_week: bool) -> str:
    local = as_utc(d).astimezone(LOCAL_TZ).date()
    if by_week:
        local = local - timedelta(days=local.weekday())
    return local.isoformat()


def compute_insights(
    rows: list[InsightRow], *, now: datetime, start: datetime, end: datetime,
    extensions: list[tuple[str, str]], suspensions: list[tuple[str, str]],
    gas_readings: dict[str, list[tuple]], shifts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Everything the analytics panel shows, keyed by ids (the router adds names)."""
    shifts = shifts or DEFAULT_SHIFTS
    by_hour = [0] * 24
    by_shift: Counter[str] = Counter()
    for r in rows:
        h = as_utc(r.validFrom).astimezone(LOCAL_TZ).hour
        by_hour[h] += 1
        by_shift[shift_for(h, shifts) or "Outside defined shifts"] += 1

    contractor: Counter[str] = Counter()
    for r in rows:
        if r.contractorCompanyId:
            contractor[f"id:{r.contractorCompanyId}"] += 1
        elif (r.contractorName or "").strip():
            contractor[f"text:{r.contractorName.strip()}"] += 1
        else:
            contractor["none"] += 1

    by_week = (end - start) > timedelta(days=62)
    trend: dict[str, dict[str, int]] = {}
    for r in rows:
        k = _period_key(r.createdAt, by_week)
        trend.setdefault(k, {"raised": 0, "closed": 0})["raised"] += 1
    for r in rows:
        if r.closedAt and start <= as_utc(r.closedAt) < end:
            k = _period_key(r.closedAt, by_week)
            trend.setdefault(k, {"raised": 0, "closed": 0})["closed"] += 1

    ext_status = Counter((s or "PENDING").upper() for _, s in extensions)
    susp_reasons = Counter((reason or "Not stated").strip() for _, reason in suspensions)
    status_counts = Counter(r.status for r in rows)

    return {
        "totals": {
            "raised": len(rows),
            "closed": status_counts["CLOSED"],
            "cancelled": status_counts["CANCELLED"],
            "rejected": status_counts["REJECTED"],
            "expired": status_counts["EXPIRED"],
            "open": sum(v for k, v in status_counts.items() if k not in ("CLOSED", "CANCELLED", "REJECTED")),
        },
        "byType": _ranked(Counter(r.type for r in rows)),
        "byStatus": _ranked(status_counts),
        "byArea": _ranked(Counter(r.areaId or "none" for r in rows)),
        "byContractor": _ranked(contractor),
        "byHour": [{"hour": h, "count": c} for h, c in enumerate(by_hour)],
        "byShift": [{"key": s["label"], "count": by_shift.get(s["label"], 0)} for s in shifts]
                   + ([{"key": "Outside defined shifts", "count": by_shift["Outside defined shifts"]}]
                      if by_shift.get("Outside defined shifts") else []),
        "trend": {"granularity": "week" if by_week else "day",
                  "points": [{"period": k, **v} for k, v in sorted(trend.items())]},
        "cycleTimes": cycle_times(rows),
        "extensions": {
            "total": len(extensions), "approved": ext_status["APPROVED"], "rejected": ext_status["REJECTED"],
            "pending": ext_status["PENDING"], "permitsExtended": len({p for p, _ in extensions}),
        },
        "suspensions": {
            "total": len(suspensions), "permitsSuspended": len({p for p, _ in suspensions}),
            "topReasons": [{"key": k, "count": c} for k, c in susp_reasons.most_common(5)],
        },
        "gasTests": gas_compliance(rows, gas_readings, now),
        "closure": on_time_closure(rows),
        "topIssuers": _ranked(Counter(r.issuerId for r in rows if r.issuerId), 10),
        "topReceivers": _ranked(Counter(r.receiverId for r in rows if r.receiverId), 10),
    }


def _ranked(c: Counter, limit: int | None = None) -> list[dict[str, Any]]:
    items = sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))
    if limit:
        items = items[:limit]
    return [{"key": k, "count": v} for k, v in items]


def window_bounds(frm: date | None, to: date | None, today: date | None = None) -> tuple[datetime, datetime, date, date]:
    """[from 00:00 local, to+1 00:00 local) in UTC. Defaults to the last 90 days.
    Raises ValueError on a reversed or over-long window."""
    today = today or utcnow().astimezone(LOCAL_TZ).date()
    to = to or today
    frm = frm or (to - timedelta(days=89))
    if frm > to:
        raise ValueError("'from' must be on or before 'to'")
    if (to - frm).days > 731:
        raise ValueError("The window can be at most two years")
    start = datetime(frm.year, frm.month, frm.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end_day = to + timedelta(days=1)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    return start, end, frm, to


# ── loaders (async, read-only) ───────────────────────────────────────────────
def _plant_filter(col, plants: list[str] | None):
    return True if plants is None else col.in_(plants or ["__none__"])


async def load_live_snaps(db: AsyncSession, plants: list[str] | None, now: datetime) -> tuple[list[PermitSnap], bool]:
    P = Permit
    live = [PermitStatus(s) for s in LIVE_STATUSES]
    stmt = (
        select(
            P.id, P.number, P.type, P.status, P.plantId, P.areaId, P.location, P.specificLocation,
            P.validFrom, P.validTo, P.issuerId, P.receiverId, P.contractorName, P.conflictingPermitIds,
            P.gasTestRequired, P.suspendedAt, P.suspendedReason, P.closureType, P.issuedAt, P.activatedAt,
        )
        .where(P.isDeleted.is_(False))
        .where(_plant_filter(P.plantId, plants))
        .where(or_(
            P.status.in_(live),
            and_(P.status == PermitStatus.EXPIRED, P.closureType.is_(None), P.closedAt.is_(None)),
        ))
        .order_by(P.validTo.asc())
        .limit(LIVE_LIMIT + 1)
    )
    rows = (await db.execute(stmt)).all()
    truncated = len(rows) > LIVE_LIMIT
    rows = rows[:LIVE_LIMIT]
    snaps = [
        PermitSnap(
            id=r.id, number=r.number, type=enum_value(r.type) or "", status=enum_value(r.status) or "",
            plantId=r.plantId, areaId=r.areaId, location=r.specificLocation or r.location,
            validFrom=as_utc(r.validFrom), validTo=as_utc(r.validTo), issuerId=r.issuerId,
            receiverId=r.receiverId, contractorName=r.contractorName,
            conflictingPermitIds=list(r.conflictingPermitIds or []), gasTestRequired=bool(r.gasTestRequired),
            suspendedAt=as_utc(r.suspendedAt), suspendedReason=r.suspendedReason,
            closureType=enum_value(r.closureType), issuedAt=as_utc(r.issuedAt), activatedAt=as_utc(r.activatedAt),
        )
        for r in rows
    ]
    ids = [s.id for s in snaps]
    if not ids:
        return snaps, truncated
    by_id = {s.id: s for s in snaps}

    crew = await db.execute(
        select(PermitCrewMember.permitId, func.count(PermitCrewMember.id))
        .where(PermitCrewMember.permitId.in_(ids))
        .where(PermitCrewMember.removedAt.is_(None))
        .group_by(PermitCrewMember.permitId)
    )
    for pid, n in crew.all():
        by_id[pid].crewCount = int(n or 0)

    plans = await db.execute(select(PermitGasTestPlan.permitId).where(PermitGasTestPlan.permitId.in_(ids)))
    for (pid,) in plans.all():
        by_id[pid].hasGasPlan = True

    R = PermitGasTestReading
    latest = (
        select(R.permitId.label("pid"), func.max(R.recordedAt).label("mx"))
        .where(R.permitId.in_(ids))
        .group_by(R.permitId)
        .subquery()
    )
    gas = await db.execute(
        select(R.permitId, R.recordedAt, R.refreshDueBy, R.isExceedance)
        .join(latest, and_(R.permitId == latest.c.pid, R.recordedAt == latest.c.mx))
    )
    for pid, at, due, exc in gas.all():
        s = by_id[pid]
        s.lastGasAt, s.gasRefreshDueBy, s.lastGasExceedance = as_utc(at), as_utc(due), bool(exc)
    return snaps, truncated


async def count_closed_since(db: AsyncSession, plants: list[str] | None, since: datetime) -> int:
    n = await db.scalar(
        select(func.count(Permit.id))
        .where(Permit.isDeleted.is_(False))
        .where(_plant_filter(Permit.plantId, plants))
        .where(Permit.closedAt >= since)
    )
    return int(n or 0)


def _cohort_conditions(plants: list[str] | None, start: datetime, end: datetime):
    return (
        Permit.isDeleted.is_(False),
        _plant_filter(Permit.plantId, plants),
        Permit.createdAt >= start,
        Permit.createdAt < end,
    )


async def load_insight_inputs(
    db: AsyncSession, plants: list[str] | None, start: datetime, end: datetime,
) -> dict[str, Any]:
    P = Permit
    conds = _cohort_conditions(plants, start, end)
    res = await db.execute(
        select(
            P.id, P.type, P.status, P.areaId, P.contractorCompanyId, P.contractorName, P.issuerId, P.receiverId,
            P.createdAt, P.validFrom, P.validTo, P.issuerApprovedAt, P.safetyApprovedAt, P.plantHeadApprovedAt,
            P.issuedAt, P.activatedAt, P.workCompletedAt, P.closedAt, P.closureType, P.gasTestRequired,
        ).where(*conds).order_by(P.createdAt.asc()).limit(INSIGHT_LIMIT + 1)
    )
    raw = res.all()
    truncated = len(raw) > INSIGHT_LIMIT
    raw = raw[:INSIGHT_LIMIT]
    cohort = select(P.id).where(*conds)

    plans = {pid for (pid,) in (await db.execute(
        select(PermitGasTestPlan.permitId).where(PermitGasTestPlan.permitId.in_(cohort))
    )).all()}
    rows = [
        InsightRow(
            id=r.id, type=enum_value(r.type) or "", status=enum_value(r.status) or "",
            createdAt=as_utc(r.createdAt), validFrom=as_utc(r.validFrom), validTo=as_utc(r.validTo),
            areaId=r.areaId, contractorCompanyId=r.contractorCompanyId, contractorName=r.contractorName,
            issuerId=r.issuerId, receiverId=r.receiverId,
            approvedAt=final_approval_at(r.issuerApprovedAt, r.safetyApprovedAt, r.plantHeadApprovedAt, r.issuedAt),
            issuedAt=as_utc(r.issuedAt), activatedAt=as_utc(r.activatedAt),
            workCompletedAt=as_utc(r.workCompletedAt), closedAt=as_utc(r.closedAt),
            closureType=enum_value(r.closureType), gasRequired=bool(r.gasTestRequired) or r.id in plans,
        )
        for r in raw
    ]
    extensions = [(p, s) for p, s in (await db.execute(
        select(PermitExtension.permitId, PermitExtension.status).where(PermitExtension.permitId.in_(cohort))
    )).all()]
    suspensions = [(p, s) for p, s in (await db.execute(
        select(PermitSuspension.permitId, PermitSuspension.reason).where(PermitSuspension.permitId.in_(cohort))
    )).all()]
    R = PermitGasTestReading
    readings: dict[str, list[tuple]] = defaultdict(list)
    for pid, at, due, exc, pre in (await db.execute(
        select(R.permitId, R.recordedAt, R.refreshDueBy, R.isExceedance, R.isPreEntry)
        .where(R.permitId.in_(cohort))
        .order_by(R.permitId, R.recordedAt)
        .limit(200000)
    )).all():
        readings[pid].append((as_utc(at), as_utc(due), bool(exc), bool(pre)))
    return {"rows": rows, "truncated": truncated, "extensions": extensions,
            "suspensions": suspensions, "readings": dict(readings)}


async def load_shifts(db: AsyncSession) -> list[dict[str, Any]]:
    """SHIFT master rows with startHour/endHour in metadata; DEFAULT_SHIFTS when none usable."""
    from app.models.masters import MasterItem  # noqa: PLC0415

    res = await db.execute(
        select(MasterItem.id, MasterItem.code, MasterItem.label, MasterItem.metadata_)
        .where(MasterItem.type == "SHIFT").where(MasterItem.active.is_(True))
        .order_by(MasterItem.sortOrder)
    )
    out = []
    for mid, code, label, meta in res.all():
        meta = meta or {}
        try:
            sh, eh = int(meta.get("startHour")), int(meta.get("endHour"))
        except (TypeError, ValueError):
            continue
        if 0 <= sh <= 23 and 0 <= eh <= 24:
            out.append({"id": mid, "code": code, "label": label or code, "startHour": sh, "endHour": eh % 24})
    return out or DEFAULT_SHIFTS
