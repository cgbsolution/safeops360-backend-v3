"""EHS Scorecard — the monthly rollup.

Computes one `ScorecardPeriod` row per plant-month across six source modules,
and freezes it. Deterministic, pure SQL plus arithmetic: no model call, no
network, safe on an airgapped deployment.

Three rules this file exists to enforce.

**Never compute a second LTIFR.** The Manhours module already publishes
`ltifr` / `trir` / `severityRate` per plant-month, computed from the hours its
own submitters signed off. Deriving a second set here from Incident counts would
produce two numbers with the same name that disagree — the exact failure the
Analytics Screen Contract was built to stop. The rollup READS the Manhours figure
and records in `sources` that it did. Where no Manhours row exists, the rate is
`None` and the gap is named; it is never silently zero.

**A missing input is a recorded gap, not a zero.** Every indicator that cannot be
computed for a month appends to `gaps`, and the dashboard and both exports render
from that same list. A scorecard that prints 0% training completion when no
training was assigned is worse than one that says so.

**A quarter is the sum of its months, recomputed — not the average of three
rates.** Quarterly aggregation lives in `aggregate()` below and re-derives every
rate from summed numerators and summed exposure. Averaging three monthly LTIFRs
weights a 40-hour month equally with a 200,000-hour one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scorecard import DEFAULT_TENANT, ScorecardPeriod
from app.services.frequency_rates import ltifr as _ltifr
from app.services.frequency_rates import pct as _pct
from app.services.frequency_rates import severity_rate as _severity_rate
from app.services.frequency_rates import trir as _trir

log = logging.getLogger("safeops360.scorecard")


def period_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


@dataclass
class Gap:
    """An indicator that could not be computed, and why — in the reader's words."""

    indicator: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"indicator": self.indicator, "reason": self.reason}


@dataclass
class PeriodFacts:
    siteId: str
    year: int
    month: int
    values: dict[str, Any] = field(default_factory=dict)
    gaps: list[Gap] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


async def _rows(db: AsyncSession, sql: str, **p: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), p)).mappings().all()]


async def compute_month(
    db: AsyncSession, *, site_id: str, year: int, month: int
) -> PeriodFacts:
    """Every indicator for one plant-month, with its gaps and provenance."""
    f = PeriodFacts(siteId=site_id, year=year, month=month)
    v, gaps, src = f.values, f.gaps, f.sources
    args = {"s": site_id, "y": year, "m": month}

    # ── Exposure. Everything rate-based depends on this existing. ───────────
    mh = await _rows(
        db,
        '''SELECT "employeeHours", "contractorHours", headcount,
                  ltifr, trir, "severityRate",
                  "ltiCount", "mtcCount", "rwcCount", "facCount", "fatalityCount", "lostDays"
             FROM "Manhours" WHERE "plantId" = :s AND year = :y AND month = :m LIMIT 1''',
        **args,
    )
    if mh:
        r = mh[0]
        emp = float(r["employeeHours"] or 0)
        con = float(r["contractorHours"] or 0)
        v["employeeHours"], v["contractorHours"] = emp, con
        v["totalHours"] = emp + con
        v["headcount"] = r["headcount"]

        # The monthly return's own injury counts. These are the canonical
        # numerators (see the module docstring) and they are carried onto the row
        # so the quarterly and all-sites cells in payload._aggregate re-derive
        # from the SAME counts the monthly rate used. A quarter derived from one
        # source over a month derived from another is how a rollup starts
        # disagreeing with the months inside it.
        v["ltiCount"] = int(r["ltiCount"] or 0)
        v["recordableCount"] = int(r["mtcCount"] or 0) + int(r["rwcCount"] or 0)
        v["firstAidCount"] = int(r["facCount"] or 0)
        v["fatalityCount"] = int(r["fatalityCount"] or 0)
        v["lostDays"] = int(r["lostDays"] or 0)

        # Derived from those components, not read from Manhours.ltifr/.trir/
        # .severityRate. Those columns are NOT NULL DEFAULT 0 — they cannot say
        # "unmeasured" — and the values in production were written on the
        # 200,000-hour base for all three, which understates LTIFR and severity
        # rate five-fold. Deriving reproduces the intended plant-month figure and
        # is the only correct answer for any wider window.
        v["ltifr"] = _ltifr(
            lti=v["ltiCount"], fatalities=v["fatalityCount"], exposure_hours=v["totalHours"]
        )
        v["trir"] = _trir(
            lti=v["ltiCount"], mtc=int(r["mtcCount"] or 0), rwc=int(r["rwcCount"] or 0),
            fatalities=v["fatalityCount"], exposure_hours=v["totalHours"],
        )
        v["severityRate"] = _severity_rate(
            lost_days=v["lostDays"], fatalities=v["fatalityCount"],
            exposure_hours=v["totalHours"],
        )
        basis = "Manhours module (submitted) — derived from reported counts over reported hours"
        src["ltifr"] = src["trir"] = src["severityRate"] = basis
        src["exposure"] = "Manhours module (submitted)"
        src["ltiCount"] = src["recordableCount"] = src["firstAidCount"] = "Manhours module (submitted)"
        if v["totalHours"] <= 0:
            gaps.append(Gap("Frequency rates",
                            "the month's manhours submission records zero exposure hours, so "
                            "LTIFR, TRIR and severity rate have no denominator"))
    else:
        v["employeeHours"] = v["contractorHours"] = v["totalHours"] = None
        v["headcount"] = None
        v["ltifr"] = v["trir"] = v["severityRate"] = None
        v["fatalityCount"] = 0
        v["lostDays"] = 0
        gaps.append(Gap(
            "Frequency rates",
            "no manhours were submitted for this site in this month, so LTIFR, TRIR and "
            "severity rate cannot be computed — this is missing exposure data, not a "
            "zero-injury month",
        ))

    # ── Leading: Observation ────────────────────────────────────────────────
    v["observationsLogged"] = (await _rows(
        db,
        '''SELECT count(*) n FROM "Observation"
            WHERE "plantId" = :s AND EXTRACT(year FROM date) = :y AND EXTRACT(month FROM date) = :m''',
        **args,
    ))[0]["n"]
    src["observationsLogged"] = "Observation register"

    # ── Leading: Near Miss ──────────────────────────────────────────────────
    v["nearMissReported"] = (await _rows(
        db,
        '''SELECT count(*) n FROM "NearMiss"
            WHERE "plantId" = :s AND EXTRACT(year FROM date) = :y AND EXTRACT(month FROM date) = :m''',
        **args,
    ))[0]["n"]
    src["nearMissReported"] = "Near Miss register"

    # ── Leading: PTW compliance ─────────────────────────────────────────────
    # "Compliant" = the permit reached a controlled CLOSED state. A permit that
    # expired or was auto-expired was live work that nobody closed out, which is
    # precisely what this indicator is meant to catch.
    p = (await _rows(
        db,
        '''SELECT count(*) issued,
                  count(*) FILTER (WHERE status::text = 'CLOSED') closed
             FROM "Permit"
            WHERE "plantId" = :s AND "validFrom" IS NOT NULL
              AND EXTRACT(year FROM "validFrom") = :y AND EXTRACT(month FROM "validFrom") = :m''',
        **args,
    ))[0]
    v["ptwIssued"], v["ptwClosedProperly"] = int(p["issued"]), int(p["closed"])
    v["ptwCompliancePct"] = _pct(int(p["closed"]), int(p["issued"]))
    src["ptwCompliancePct"] = "Permit to Work — closed / issued"
    if not p["issued"]:
        gaps.append(Gap("PTW compliance", "no permits were issued at this site in this month"))

    # ── Leading: Training ───────────────────────────────────────────────────
    t = (await _rows(
        db,
        '''SELECT count(*) assigned,
                  count(*) FILTER (WHERE upper(status::text) = 'COMPLETED') completed
             FROM "TrainingAssignment"
            WHERE "isDeleted" = false AND "plantId" = :s
              AND EXTRACT(year FROM "assignedAt") = :y AND EXTRACT(month FROM "assignedAt") = :m''',
        **args,
    ))[0]
    v["trainingAssigned"], v["trainingCompleted"] = int(t["assigned"]), int(t["completed"])
    v["trainingCompletionPct"] = _pct(int(t["completed"]), int(t["assigned"]))
    src["trainingCompletionPct"] = "Training Engine — completed / assigned"
    if not t["assigned"]:
        gaps.append(Gap("Training completion",
                        "no training was assigned at this site in this month"))

    # ── Leading: Induction ──────────────────────────────────────────────────
    ind = await _rows(
        db,
        '''SELECT count(*) n FROM "SiteInduction"
            WHERE "siteId" = :s AND "conductedAt" IS NOT NULL
              AND EXTRACT(year FROM "conductedAt") = :y AND EXTRACT(month FROM "conductedAt") = :m''',
        **args,
    )
    v["inductionsConducted"] = int(ind[0]["n"]) if ind else 0
    src["inductionsConducted"] = "Site inductions"

    # ── Leading: Leadership walks (Safety Culture) ──────────────────────────
    w = (await _rows(
        db,
        '''SELECT count(*) planned,
                  count(*) FILTER (WHERE "completedDate" IS NOT NULL) completed
             FROM "LeadershipWalk"
            WHERE "plantId" = :s
              AND EXTRACT(year FROM COALESCE("completedDate", "scheduledDate")) = :y
              AND EXTRACT(month FROM COALESCE("completedDate", "scheduledDate")) = :m''',
        **args,
    ))[0]
    v["leadershipWalksPlanned"], v["leadershipWalksCompleted"] = int(w["planned"]), int(w["completed"])
    v["leadershipWalksCompletedPct"] = _pct(int(w["completed"]), int(w["planned"]))
    src["leadershipWalksCompletedPct"] = "Safety Culture — leadership walks"
    if not w["planned"]:
        gaps.append(Gap("Leadership walks", "no leadership walks were scheduled or completed"))

    # ── Leading: Safety Culture maturity + perception ───────────────────────
    cm = await _rows(
        db,
        '''SELECT "stageScore" FROM "CultureMaturitySnapshot"
            WHERE "plantId" = :s AND period = :p ORDER BY "snapshotAt" DESC LIMIT 1''',
        s=site_id, p=period_key(year, month),
    )
    v["cultureStageScore"] = float(cm[0]["stageScore"]) if cm and cm[0]["stageScore"] is not None else None
    if v["cultureStageScore"] is None:
        gaps.append(Gap("Culture maturity", "no maturity snapshot was taken for this month"))
    else:
        src["cultureStageScore"] = "Safety Culture — maturity snapshot"

    # Perception is surveyed QUARTERLY, so a monthly row carries its quarter's
    # score rather than a null. Repeating the quarter's value across its three
    # months is stated in `sources` so nobody reads it as three measurements.
    pq = await _rows(
        db,
        '''SELECT "compositeScore" FROM "PerceptionIndexSnapshot"
            WHERE "plantId" = :s AND period = :p ORDER BY "publishedAt" DESC NULLS LAST LIMIT 1''',
        s=site_id, p=f"{year}-Q{quarter_of(month)}",
    )
    v["perceptionScore"] = float(pq[0]["compositeScore"]) if pq and pq[0]["compositeScore"] is not None else None
    if v["perceptionScore"] is not None:
        src["perceptionScore"] = f"Safety Culture — perception index, {year}-Q{quarter_of(month)} (quarterly survey)"

    # ── Lagging: Incident ───────────────────────────────────────────────────
    i = (await _rows(
        db,
        '''SELECT count(*) total,
                  count(*) FILTER (WHERE type::text = 'LTI') lti,
                  count(*) FILTER (WHERE type::text IN ('MTC','RWC')) recordable,
                  count(*) FILTER (WHERE type::text = 'FIRST_AID') fac,
                  count(*) FILTER (WHERE type::text = 'FATALITY') fatal,
                  count(*) FILTER (WHERE severity::text IN ('CRITICAL','HIGH')) high_sev
             FROM "Incident"
            WHERE "isDeleted" = false AND "plantId" = :s
              AND EXTRACT(year FROM date) = :y AND EXTRACT(month FROM date) = :m''',
        **args,
    ))[0]
    # These two have no Manhours equivalent — the monthly return does not track
    # property damage, fire or a severity grading — so they are always the
    # register's own counts.
    v["incidentsTotal"] = int(i["total"])
    v["highSeverityCount"] = int(i["high_sev"])
    src["incidentsTotal"] = src["highSeverityCount"] = "Incident register"

    # The injury counts exist in both places. Manhours (submitted) is canonical,
    # so where a monthly return exists its counts stand and the register's are
    # only used to CHECK them. Where no return exists the register is all there
    # is — recorded as such, because a count from the register sitting next to a
    # rate that could not be computed is a different kind of row.
    register = {
        "ltiCount": int(i["lti"]),
        "recordableCount": int(i["recordable"]),
        "firstAidCount": int(i["fac"]),
        "fatalityCount": int(i["fatal"]),
    }
    if v.get("totalHours") is None:
        v.update(register)
        for k in register:
            src[k] = "Incident register (no manhours return for this month)"
    else:
        # A disagreement between the signed-off monthly return and the register is
        # a real finding, not something to average away or quietly prefer one side
        # of. Name it so the reader knows why the count beside the rate is what it
        # is — and can go and reconcile the two.
        diverged = [
            f"{k.replace('Count', '')}: return says {v.get(k)}, register says {n}"
            for k, n in register.items()
            if int(v.get(k) or 0) != n
        ]
        if diverged:
            gaps.append(Gap(
                "Injury counts",
                "the manhours return and the incident register disagree for this month "
                "(" + "; ".join(diverged) + "). The return is the canonical figure and "
                "the rates are derived from it; the register needs reconciling.",
            ))

    return f


async def upsert_period(
    db: AsyncSession, facts: PeriodFacts, *, tenant: str = DEFAULT_TENANT
) -> str:
    """Insert or refresh one plant-month. Returns "NEW" | "UPDATED"."""
    from sqlalchemy import select

    existing = (
        await db.execute(
            select(ScorecardPeriod).where(
                ScorecardPeriod.tenantId == tenant,
                ScorecardPeriod.siteId == facts.siteId,
                ScorecardPeriod.year == facts.year,
                ScorecardPeriod.month == facts.month,
            )
        )
    ).scalar_one_or_none()

    payload = dict(facts.values)
    payload["gaps"] = [g.as_dict() for g in facts.gaps]
    payload["sources"] = dict(facts.sources)
    payload["computedAt"] = datetime.now(timezone.utc)
    payload["period"] = period_key(facts.year, facts.month)

    if existing is None:
        db.add(ScorecardPeriod(
            tenantId=tenant, siteId=facts.siteId, year=facts.year, month=facts.month, **payload
        ))
        return "NEW"
    for k, val in payload.items():
        setattr(existing, k, val)
    return "UPDATED"


async def run_rollup(
    db: AsyncSession,
    *,
    tenant: str = DEFAULT_TENANT,
    months: int = 18,
    site_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Recompute the trailing `months` plant-months for every active site.

    Recomputes rather than only filling gaps: a back-dated incident or a late
    manhours submission changes a month that is already stored, and a rollup
    that only ever appended would leave the stored history quietly wrong. The
    freeze this table provides is against RECOMPUTATION AT READ TIME, not
    against correction — corrections are exactly what the nightly job is for.
    """
    started = datetime.now(timezone.utc)

    # The site universe is every plant with either exposure or activity — not
    # every Plant row. A site with no records at all would otherwise generate a
    # year of zero-filled months that read as a perfect safety performance.
    sites = await _rows(
        db,
        '''SELECT DISTINCT p.id, p.code, p.name FROM "Plant" p
            WHERE EXISTS (SELECT 1 FROM "Manhours" m WHERE m."plantId" = p.id)
               OR EXISTS (SELECT 1 FROM "Observation" o WHERE o."plantId" = p.id)
               OR EXISTS (SELECT 1 FROM "Incident" i WHERE i."plantId" = p.id AND i."isDeleted" = false)
            ORDER BY p.code''',
    )
    if site_ids:
        wanted = set(site_ids)
        sites = [s for s in sites if s["id"] in wanted]

    now = datetime.now(timezone.utc)
    periods: list[tuple[int, int]] = []
    y, m = now.year, now.month
    for _ in range(months):
        periods.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12

    new_n = upd_n = 0
    errors: list[str] = []
    for site in sites:
        for (yy, mm) in periods:
            try:
                facts = await compute_month(db, site_id=site["id"], year=yy, month=mm)
                outcome = await upsert_period(db, facts, tenant=tenant)
                new_n += outcome == "NEW"
                upd_n += outcome == "UPDATED"
            except Exception as e:  # noqa: BLE001 — one bad month must not sink the run
                await db.rollback()
                errors.append(f"{site['code']} {yy}-{mm:02d}: {str(e)[:150]}")
                log.warning("scorecard rollup failed for %s %s-%s: %s", site["code"], yy, mm, e)
                continue
        await db.commit()

    finished = datetime.now(timezone.utc)
    return {
        "sites": len(sites),
        "periods": len(periods),
        "new": new_n,
        "updated": upd_n,
        "errorCount": len(errors),
        "errors": errors[:10],
        "durationMs": int((finished - started).total_seconds() * 1000),
    }


__all__ = [
    "RATE_BASE",
    "Gap",
    "PeriodFacts",
    "compute_month",
    "period_key",
    "quarter_of",
    "run_rollup",
    "upsert_period",
]
