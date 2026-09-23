"""EHS Scorecard — the ONE payload every surface renders from.

The dashboard, the PDF and the PPTX all call `build_payload()` and render what
it returns. There is no export-only query path, because an export that fetches
its own data is an export that will eventually disagree with the screen it claims
to reproduce — and the disagreement surfaces in front of a client, in a deck,
months later, with nobody able to say which number was right.

Everything below reads from `ScorecardPeriod` (the frozen monthly rollup) and
never from raw source records. Quarterly is derived here, from the same monthly
rows, by re-aggregating numerators and exposure — never by averaging rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scorecard import DEFAULT_TENANT, ScorecardPeriod
from app.services.frequency_rates import ltifr as _ltifr
from app.services.frequency_rates import pct as _pct
from app.services.frequency_rates import severity_rate as _severity_rate
from app.services.frequency_rates import trir as _trir
from app.services.scorecard.rollup import quarter_of

# The indicator table, in reading order. One declaration, consumed by the API,
# the dashboard, the PDF and the PPTX — so a label, a unit or a direction can
# only ever be changed in one place.
#
# `goodDirection` is what an improving trend looks like. It is NOT uniform and
# must not be guessed: more observations logged is good (people are looking),
# more incidents is bad, and more near misses reported is *deliberately
# unjudged* — a rise usually means reporting culture improving, and colouring it
# red would train sites to report less.
@dataclass(frozen=True)
class Indicator:
    key: str
    label: str
    unit: str          # "count" | "pct" | "rate" | "score"
    band: str          # "leading" | "lagging"
    goodDirection: str  # "up" | "down" | "neutral"
    numerator: str | None = None
    denominator: str | None = None
    note: str = ""


INDICATORS: tuple[Indicator, ...] = (
    Indicator("observationsLogged", "Observations logged", "count", "leading", "up"),
    Indicator("nearMissReported", "Near misses reported", "count", "leading", "neutral",
              note="A rise here is usually better reporting, not more hazard — deliberately "
                   "shown without a verdict."),
    Indicator("ptwCompliancePct", "PTW closed-out compliance", "pct", "leading", "up",
              numerator="ptwClosedProperly", denominator="ptwIssued"),
    Indicator("trainingCompletionPct", "Training completion", "pct", "leading", "up",
              numerator="trainingCompleted", denominator="trainingAssigned"),
    Indicator("inductionsConducted", "Site inductions conducted", "count", "leading", "up"),
    Indicator("leadershipWalksCompletedPct", "Leadership walks completed", "pct", "leading", "up",
              numerator="leadershipWalksCompleted", denominator="leadershipWalksPlanned"),
    Indicator("cultureStageScore", "Safety culture maturity", "score", "leading", "up"),
    Indicator("incidentsTotal", "Incidents", "count", "lagging", "down"),
    # Ahead of LTIs, because a fatality outranks every other outcome on this page.
    # The column is new (see scripts/add_scorecard_fatality_count.py) and was added
    # so quarterly severity rate could be re-derived from its months; declaring it
    # here is what puts it in front of a reader, which a safety scorecard that could
    # not count fatalities at all plainly needed.
    Indicator("fatalityCount", "Fatalities", "count", "lagging", "down"),
    Indicator("ltiCount", "Lost-time injuries", "count", "lagging", "down"),
    Indicator("recordableCount", "Recordable injuries (MTC/RWC)", "count", "lagging", "down"),
    Indicator("firstAidCount", "First-aid cases", "count", "lagging", "down"),
    Indicator("highSeverityCount", "High/critical severity", "count", "lagging", "down"),
    Indicator("ltifr", "LTIFR", "rate", "lagging", "down",
              numerator="ltiCount", denominator="totalHours",
              note="Per million person-hours (IS 3786:1983). Derived by services."
                   "frequency_rates from the Manhours module's own submitted counts and "
                   "hours — the one definition every screen on the platform uses."),
    Indicator("trir", "TRIR", "rate", "lagging", "down",
              numerator="recordableCount", denominator="totalHours",
              note="Per 200,000 person-hours (OSHA 29 CFR 1904). Derived by services."
                   "frequency_rates from the Manhours module's own submitted counts and "
                   "hours — the one definition every screen on the platform uses."),
)
BY_KEY = {i.key: i for i in INDICATORS}

# Counts that re-aggregate by summing. Rates and scores never do.
_SUMMABLE = (
    "observationsLogged", "nearMissReported", "ptwIssued", "ptwClosedProperly",
    "trainingAssigned", "trainingCompleted", "inductionsConducted",
    "leadershipWalksPlanned", "leadershipWalksCompleted",
    "incidentsTotal", "ltiCount", "recordableCount", "firstAidCount",
    "fatalityCount", "highSeverityCount", "lostDays",
)
_EXPOSURE = ("employeeHours", "contractorHours", "totalHours")


def _bucket_rows(rows: list[ScorecardPeriod], grain: str) -> dict[str, list[ScorecardPeriod]]:
    out: dict[str, list[ScorecardPeriod]] = {}
    for r in rows:
        key = r.period if grain == "month" else f"{r.year}-Q{quarter_of(r.month)}"
        out.setdefault(key, []).append(r)
    return out


def _aggregate(rows: list[ScorecardPeriod]) -> dict[str, Any]:
    """Combine plant-months into one cell.

    Used for BOTH the quarterly grain and the all-sites view, because they are
    the same operation: several plant-months collapsing into one figure.

    Rates are re-derived from summed numerators over summed exposure. Averaging
    three monthly LTIFRs would weight a 40-hour month the same as a 200,000-hour
    one, which is the standard way a quarterly safety figure ends up wrong.
    """
    agg: dict[str, Any] = {k: 0 for k in _SUMMABLE}
    for k in _EXPOSURE:
        agg[k] = None

    exposure_seen = False
    for r in rows:
        for k in _SUMMABLE:
            agg[k] += int(getattr(r, k) or 0)
        for k in _EXPOSURE:
            val = getattr(r, k)
            if val is not None:
                agg[k] = (agg[k] or 0) + float(val)
                exposure_seen = True

    agg["ptwCompliancePct"] = _pct(agg["ptwClosedProperly"], agg["ptwIssued"])
    agg["trainingCompletionPct"] = _pct(agg["trainingCompleted"], agg["trainingAssigned"])
    agg["leadershipWalksCompletedPct"] = _pct(
        agg["leadershipWalksCompleted"], agg["leadershipWalksPlanned"]
    )

    if exposure_seen and agg["totalHours"]:
        # Re-derived from this cell's own summed numerators over its own summed
        # exposure. For a single month this reproduces that month's stored rate
        # exactly — same shared functions, same canonical counts — and across
        # several months or sites it is the only correct way to combine them.
        #
        # These used to go through a local `_rate` pinned to a 200,000-hour base,
        # which is right for TRIR and wrong by a factor of five for LTIFR and
        # severity rate. The base now travels with the named rate.
        agg["ltifr"] = _ltifr(
            lti=agg["ltiCount"], fatalities=agg["fatalityCount"],
            exposure_hours=agg["totalHours"],
        )
        agg["trir"] = _trir(
            lti=agg["ltiCount"], mtc=agg["recordableCount"], rwc=0,
            fatalities=agg["fatalityCount"], exposure_hours=agg["totalHours"],
        )
        agg["severityRate"] = _severity_rate(
            lost_days=agg["lostDays"], fatalities=agg["fatalityCount"],
            exposure_hours=agg["totalHours"],
        )
    else:
        agg["ltifr"] = agg["trir"] = agg["severityRate"] = None

    # Scores are a state, not a flow: the mean across the cell's sites/months,
    # over the rows that actually carry one.
    for k in ("cultureStageScore", "perceptionScore"):
        vals = [float(getattr(r, k)) for r in rows if getattr(r, k) is not None]
        agg[k] = round(sum(vals) / len(vals), 1) if vals else None

    gaps: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        for g in (r.gaps or []):
            key = (g.get("indicator", ""), g.get("reason", ""))
            if key not in seen:
                seen.add(key)
                gaps.append(g)
    agg["gaps"] = gaps
    agg["rowCount"] = len(rows)
    return agg


async def build_payload(
    db: AsyncSession,
    *,
    tenant: str = DEFAULT_TENANT,
    site_id: str | None = None,
    grain: str = "month",
    periods: int = 12,
    plants_allowed: list[str] | None = None,
) -> dict[str, Any]:
    """The scorecard, exactly as every surface renders it.

    `plants_allowed` is the caller's resolved plant scope and is applied in SQL.
    A scorecard is precisely the artefact someone forwards outside their own
    site, so the scope is fail-closed: an empty list yields nothing rather than
    everything.
    """
    stmt = select(ScorecardPeriod).where(ScorecardPeriod.tenantId == tenant)
    if plants_allowed is not None:
        if not plants_allowed:
            return _empty(tenant, site_id, grain, periods, "no accessible sites")
        stmt = stmt.where(ScorecardPeriod.siteId.in_(plants_allowed))
    if site_id:
        stmt = stmt.where(ScorecardPeriod.siteId == site_id)

    rows = list((await db.execute(stmt.order_by(ScorecardPeriod.period))).scalars().all())
    if not rows:
        return _empty(tenant, site_id, grain, periods, "no rollup rows — has the rollup job run?")

    buckets = _bucket_rows(rows, grain)
    keys = sorted(buckets)[-periods:]

    series = [{"period": k, **_aggregate(buckets[k])} for k in keys]
    current = series[-1] if series else None
    prior = series[-2] if len(series) > 1 else None

    # Site labels — house rule: never render a Plant cuid.
    site_ids = sorted({r.siteId for r in rows})
    labels: dict[str, str] = {}
    if site_ids:
        for pid, code, name in (
            await db.execute(
                text('SELECT id, code, name FROM "Plant" WHERE id = ANY(:ids)'), {"ids": site_ids}
            )
        ).all():
            labels[pid] = f"{code} — {name}" if code else name

    # Per-site breakdown for the current cell, so a portfolio view can still say
    # which site is carrying the number.
    by_site = []
    if current:
        for sid in site_ids:
            cell = [r for r in buckets[keys[-1]] if r.siteId == sid]
            if cell:
                by_site.append({"siteId": sid, "siteName": labels.get(sid, "Unknown site"),
                                **_aggregate(cell)})
        by_site.sort(key=lambda s: (-(s.get("incidentsTotal") or 0), s["siteName"]))

    return {
        "tenant": tenant,
        "site": site_id,
        "siteName": labels.get(site_id or "", None) if site_id else None,
        "grain": grain,
        "periods": keys,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "indicators": [
            {
                "key": i.key, "label": i.label, "unit": i.unit, "band": i.band,
                "goodDirection": i.goodDirection, "note": i.note,
                "numerator": i.numerator, "denominator": i.denominator,
            }
            for i in INDICATORS
        ],
        "series": series,
        "current": current,
        "prior": prior,
        "bySite": by_site,
        "sites": [{"value": s, "label": labels.get(s, "Unknown site")} for s in site_ids],
        # Which indicators could not be computed for the CURRENT cell. Rendered
        # by the dashboard as DataQualityFlags and printed in both exports.
        "gaps": (current or {}).get("gaps", []),
        "sourceCoverage": _coverage(rows),
        "empty": False,
        "message": None,
    }


def _coverage(rows: list[ScorecardPeriod]) -> dict[str, Any]:
    """How much of the stored history actually carries exposure data.

    The single most important caveat on any scorecard: a frequency rate is only
    as real as the manhours behind it, and this platform's Manhours submissions
    stop before the incident data does.
    """
    total = len(rows)
    with_hours = sum(1 for r in rows if (r.totalHours or 0) > 0)
    months = sorted({r.period for r in rows})
    hours_months = sorted({r.period for r in rows if (r.totalHours or 0) > 0})
    return {
        "plantMonths": total,
        "plantMonthsWithExposure": with_hours,
        "exposurePct": round(100.0 * with_hours / total, 1) if total else None,
        "firstPeriod": months[0] if months else None,
        "lastPeriod": months[-1] if months else None,
        "lastPeriodWithExposure": hours_months[-1] if hours_months else None,
    }


def _empty(tenant: str, site: str | None, grain: str, periods: int, why: str) -> dict[str, Any]:
    return {
        "tenant": tenant, "site": site, "siteName": None, "grain": grain,
        "periods": [], "generatedAt": datetime.now(timezone.utc).isoformat(),
        "indicators": [], "series": [], "current": None, "prior": None,
        "bySite": [], "sites": [], "gaps": [],
        "sourceCoverage": None, "empty": True, "message": why,
    }


__all__ = ["BY_KEY", "INDICATORS", "Indicator", "build_payload"]
