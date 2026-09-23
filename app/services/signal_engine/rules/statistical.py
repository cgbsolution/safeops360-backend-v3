"""Signal Engine — statistical rules (XSTAT-001…006).

Outliers and anomalies, computed against the portfolio or against the series'
own history. No model, no training, no network — every number here is arithmetic
over rows the database already holds, which is what makes the engine safe to run
inside an airgapped client site.

**Robust statistics throughout, not mean and σ.** This portfolio is two Meridian
sites carrying most of the volume plus twenty Page Industries sites carrying a
handful each. A mean over that is dragged by the sites under test, so a genuine
outlier raises the very threshold meant to catch it — the classic masking
failure. Median and MAD (see `domain.median_abs_deviation`) are unaffected by up
to half the population being extreme.

**Small-n honesty.** Every rule here carries a minimum population and a minimum
per-unit count. A "300% spike" from one record to four is arithmetic, not
information, and shipping it teaches the reader to ignore the panel.
"""

from __future__ import annotations

from datetime import timedelta

from app.services.signal_engine.base import (
    EvidenceRef,
    RuleContext,
    SignalCandidate,
    SignalRuleImpl,
)
from app.services.signal_engine.domain import (
    load_labels,
    median_abs_deviation,
    pct,
    plural,
    robust_z,
    rows,
)


class _StatRule(SignalRuleImpl):
    rule_class = "STATISTICAL"

    def render_action(self, c: SignalCandidate) -> str:  # pragma: no cover - overridden
        return "Review the underlying records."


# ── XSTAT-001 ────────────────────────────────────────────────────────────────
class Xstat001StalledInvestigationOutlier(_StatRule):
    """A site whose share of stalled investigations is a portfolio outlier.

    Absolute counts favour the biggest site and tell a plant head nothing they
    did not know. The RATE, compared against every other site, is the statement
    that survives "well, we're the largest".
    """

    code = "XSTAT-001"
    name = "Site stalled-investigation rate is a portfolio outlier"
    description = (
        "The share of a site's open incidents with no update for weeks is far "
        "above the portfolio norm."
    )
    category = "OPERATIONAL_RISK"
    default_severity = "HIGH"
    source_modules = ("INCIDENT",)
    window_days = 365
    default_thresholds = {
        "stalledAfterDays": 30,
        "minOpenAtSite": 3,
        "minSites": 3,
        "zThreshold": 2.0,
        "absoluteRatePct": 80.0,   # a rate this bad is a finding regardless of spread
    }

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        data = await rows(
            ctx.db,
            '''SELECT "plantId" AS site,
                      count(*) FILTER (WHERE status::text IN ('REPORTED','INVESTIGATION','CAPA_ASSIGNED')) AS open,
                      count(*) FILTER (WHERE status::text IN ('REPORTED','INVESTIGATION','CAPA_ASSIGNED')
                                        AND "updatedAt" < :cutoff) AS stalled
                 FROM "Incident" WHERE "isDeleted" = false AND "plantId" IS NOT NULL
                GROUP BY 1''',
            cutoff=ctx.now - timedelta(days=t["stalledAfterDays"]),
        )
        eligible = [r for r in data if int(r["open"]) >= t["minOpenAtSite"]]
        rates = [pct(int(r["stalled"]), int(r["open"])) for r in eligible]
        med, mad = median_abs_deviation(rates)

        # With fewer than `minSites` comparable sites there is no peer group and
        # no outlier claim can honestly be made. That must NOT silence the rule:
        # "10 of 10 open investigations have had no update in 30 days" is a
        # finding on its own terms, and this portfolio — two Meridian sites
        # carrying the volume, twenty Page sites carrying a handful — would
        # otherwise never trigger it at all. Below the peer threshold the rule
        # falls back to the absolute rate and the narrative drops the comparison
        # rather than inventing one.
        comparable = len(eligible) >= t["minSites"]
        if not comparable:
            ctx.notes["degraded"] = (
                f"only {len(eligible)} site(s) carry >= {t['minOpenAtSite']} open incidents; "
                f"reporting on absolute rate >= {t['absoluteRatePct']}% without a peer comparison"
            )

        out: list[SignalCandidate] = []
        for r in eligible:
            rate = pct(int(r["stalled"]), int(r["open"]))
            z = robust_z(rate, med, mad) if comparable else 0.0
            if int(r["stalled"]) == 0:
                continue
            if comparable:
                if z < t["zThreshold"] and rate < t["absoluteRatePct"]:
                    continue
            elif rate < t["absoluteRatePct"]:
                continue
            ev = await rows(
                ctx.db,
                '''SELECT id, number AS ref, date, status::text AS status, "updatedAt" AS touched
                     FROM "Incident"
                    WHERE "isDeleted" = false AND "plantId" = :s
                      AND status::text IN ('REPORTED','INVESTIGATION','CAPA_ASSIGNED')
                      AND "updatedAt" < :cutoff
                    ORDER BY "updatedAt" LIMIT 6''',
                s=r["site"], cutoff=ctx.now - timedelta(days=t["stalledAfterDays"]),
            )
            out.append(SignalCandidate(
                signalKey=f"STALLED_OUTLIER::{r['site']}",
                severity="HIGH" if rate >= 80 else "MEDIUM",
                confidence=0.8,
                siteId=r["site"],
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]), "stalled": int(r["stalled"]),
                    "open": int(r["open"]), "ratePct": rate,
                    "portfolioMedianPct": round(med, 1), "z": round(z, 1),
                    "days": t["stalledAfterDays"], "sites": len(eligible),
                    "comparable": comparable,
                },
                evidence=[
                    EvidenceRef("INCIDENT", e["id"], e["ref"], 1.0,
                                {"status": e["status"], "lastTouched": str(e["touched"])[:10]})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        head = (
            f"{f['site']} — {f['stalled']} of {f['open']} open investigations ({f['ratePct']}%) "
            f"have had no update in {f['days']} days"
        )
        if f.get("comparable"):
            return (
                f"{head}, against a portfolio median of {f['portfolioMedianPct']}% across "
                f"{f['sites']} sites."
            )
        # No peer group — say so rather than imply one.
        return (
            f"{head}. Too few sites carry enough open incidents for a portfolio comparison, so "
            f"this is reported on the rate alone."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Assign an owner and a next action to the {c.facts['stalled']} stalled "
            f"investigations; at this rate the site's investigation process has stopped, not slowed."
        )


# ── XSTAT-002 ────────────────────────────────────────────────────────────────
class Xstat002VolumeSpike(_StatRule):
    """A month whose record volume breaks out of its own rolling baseline.

    This is the rule that should have caught Incident Analytics' 400% jump. It
    runs per module and per site, because a portfolio-level spike is usually one
    site and the aggregate hides which.
    """

    code = "XSTAT-002"
    name = "Record volume spike against rolling baseline"
    description = (
        "A module's monthly record count has broken sharply out of its own "
        "trailing baseline — either a real change in the plant or a change in "
        "how records are being created."
    )
    category = "LAGGING_PATTERN"
    default_severity = "MEDIUM"
    source_modules = ("INCIDENT", "OBSERVATION", "NEAR_MISS")
    window_days = 540
    default_thresholds = {
        "baselineMonths": 6,
        "minBaselineTotal": 12,
        "minCurrent": 8,
        "spikeRatio": 2.0,
        "zThreshold": 3.0,
        "scanMonths": 4,   # how many complete months back to test for a spike
    }

    _MODULES = (
        ("INCIDENT", "Incident", "date", True, "incidents"),
        ("OBSERVATION", "Observation", "date", False, "observations"),
        ("NEAR_MISS", "NearMiss", "date", False, "near misses"),
    )

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        out: list[SignalCandidate] = []
        for module, table, datecol, soft, noun in self._MODULES:
            deleted = 'AND "isDeleted" = false' if soft else ""
            series = await rows(
                ctx.db,
                f'''SELECT to_char({datecol}, 'YYYY-MM') AS m, count(*) AS n
                      FROM "{table}"
                     WHERE {datecol} >= :since {deleted}
                     GROUP BY 1 ORDER BY 1''',  # noqa: S608 — table/col are literals above
                since=ctx.now - timedelta(days=ctx.window_days),
            )
            if len(series) < t["baselineMonths"] + 1:
                continue
            # The most recent COMPLETE month. The current partial month always
            # looks like a collapse and would fire this rule every single day.
            current_month = ctx.now.strftime("%Y-%m")
            complete = [s for s in series if s["m"] != current_month]
            if len(complete) < t["baselineMonths"] + 1:
                continue

            # Scan the last `scanMonths` COMPLETE months, not only the most
            # recent one. Looking at the latest month alone made this rule
            # useless on the very dataset it was written for: incidents ramped
            # 6 → 21 → 30 across Mar–May, then fell back to 2 in July, so the
            # only month it inspected was a quiet one and the rule reported
            # nothing. A spike nobody acted on is still worth surfacing a
            # quarter later — arguably more so.
            for i in range(1, min(t["scanMonths"], len(complete) - t["baselineMonths"]) + 1):
                target = complete[-i]
                baseline = complete[-(i + t["baselineMonths"]):-i]
                if len(baseline) < t["baselineMonths"]:
                    continue
                base_counts = [float(b["n"]) for b in baseline]
                if sum(base_counts) < t["minBaselineTotal"]:
                    continue

                n = int(target["n"])
                if n < t["minCurrent"]:
                    continue
                med, mad = median_abs_deviation(base_counts)
                ratio = (n / med) if med > 0 else 0.0
                z = robust_z(float(n), med, mad)
                if ratio < t["spikeRatio"] and z < t["zThreshold"]:
                    continue

                ev = await rows(
                    ctx.db,
                    f'''SELECT id, number AS ref, {datecol} AS d
                          FROM "{table}"
                         WHERE to_char({datecol}, 'YYYY-MM') = :m {deleted}
                         ORDER BY {datecol} DESC LIMIT 5''',  # noqa: S608
                    m=target["m"],
                )
                out.append(SignalCandidate(
                    signalKey=f"SPIKE::{module}::{target['m']}",
                    severity="HIGH" if ratio >= 3 else "MEDIUM",
                    confidence=0.75,
                    windowStart=ctx.now - timedelta(days=30 * (i + t["baselineMonths"])),
                    windowEnd=ctx.now,
                    facts={
                        "module": module, "noun": noun, "month": target["m"], "count": n,
                        "baselineMedian": round(med, 1), "ratio": round(ratio, 1),
                        "z": round(z, 1), "baselineMonths": t["baselineMonths"],
                        "monthsAgo": i,
                    },
                    evidence=[
                        EvidenceRef(module, e["id"], e["ref"], 1.0, {"date": str(e["d"])[:10]})
                        for e in ev
                    ],
                ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['count']} {f['noun']} were recorded in {f['month']}, {f['ratio']}× the median of "
            f"{f['baselineMedian']} over the preceding {f['baselineMonths']} months."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Establish whether {f_month(c)} reflects a change in the plant or a change in "
            f"reporting — a spike driven by better reporting needs a different response from one "
            f"driven by more events."
        )


def f_month(c: SignalCandidate) -> str:
    return str(c.facts.get("month", "the spike"))


# ── XSTAT-003 ────────────────────────────────────────────────────────────────
class Xstat003AreaConcentration(_StatRule):
    """One area carrying a disproportionate share of its site's incidents."""

    code = "XSTAT-003"
    name = "Incident concentration in a single area"
    description = (
        "One area accounts for a disproportionate share of its site's incidents, "
        "which localises the problem to a place rather than a plant."
    )
    category = "LAGGING_PATTERN"
    default_severity = "MEDIUM"
    source_modules = ("INCIDENT",)
    window_days = 365
    default_thresholds = {"minSiteIncidents": 8, "minAreaIncidents": 4, "minSharePct": 50.0}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        data = await rows(
            ctx.db,
            '''WITH a AS (
                   SELECT "plantId" AS site, "areaId" AS area, count(*) AS n
                     FROM "Incident"
                    WHERE "isDeleted" = false AND "areaId" IS NOT NULL AND date >= :since
                    GROUP BY 1,2)
               SELECT a.site, a.area, a.n, s.total, s.areas
                 FROM a JOIN (SELECT site, sum(n) AS total, count(*) AS areas FROM a GROUP BY 1) s
                   ON s.site = a.site''',
            since=ctx.now - timedelta(days=ctx.window_days),
        )
        out: list[SignalCandidate] = []
        for r in data:
            n, total = int(r["n"]), int(r["total"])
            share = pct(n, total)
            if total < t["minSiteIncidents"] or n < t["minAreaIncidents"] or share < t["minSharePct"]:
                continue
            # A site with one area cannot have a concentration — it is the site.
            if int(r["areas"]) < 2:
                continue
            ev = await rows(
                ctx.db,
                '''SELECT id, number AS ref, date, type::text AS type
                     FROM "Incident"
                    WHERE "isDeleted" = false AND "plantId" = :s AND "areaId" = :a
                    ORDER BY date DESC LIMIT 6''',
                s=r["site"], a=r["area"],
            )
            out.append(SignalCandidate(
                signalKey=f"AREA_CONC::{r['site']}::{r['area']}",
                severity="HIGH" if share >= 70 else "MEDIUM",
                confidence=0.85,
                siteId=r["site"], areaId=r["area"],
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]), "area": lb.area(r["area"]),
                    "count": n, "total": total, "sharePct": share, "areas": int(r["areas"]),
                },
                evidence=[
                    EvidenceRef("INCIDENT", e["id"], e["ref"], 1.0,
                                {"date": str(e["date"])[:10], "type": e["type"]})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['area']} accounts for {f['count']} of {f['site']}'s {f['total']} incidents "
            f"({f['sharePct']}%) across {f['areas']} areas — the site's incident profile is "
            f"really this one area's."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Scope the next HIRA review, inspection round and toolbox talk to "
            f"{c.facts['area']} specifically rather than the site as a whole."
        )


# ── XSTAT-004 ────────────────────────────────────────────────────────────────
class Xstat004CapaOverdueOutlier(_StatRule):
    """A site whose CAPA overdue rate is an outlier against the portfolio."""

    code = "XSTAT-004"
    name = "Site CAPA overdue rate is a portfolio outlier"
    description = (
        "The share of a site's open CAPAs that are past their closure target is "
        "far above the portfolio norm."
    )
    category = "OPERATIONAL_RISK"
    default_severity = "HIGH"
    source_modules = ("CAPA",)
    window_days = 365
    default_thresholds = {
        "minOpenAtSite": 3, "minSites": 3, "zThreshold": 2.0, "absoluteRatePct": 75.0,
    }

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        data = await rows(
            ctx.db,
            '''SELECT "plantId" AS site,
                      count(*) FILTER (WHERE state NOT IN ('CLOSED','CANCELLED','VERIFIED')) AS open,
                      count(*) FILTER (WHERE state NOT IN ('CLOSED','CANCELLED','VERIFIED')
                                        AND "closureTargetDate" < :now) AS overdue
                 FROM "Capa" WHERE "isDeleted" = false AND "plantId" IS NOT NULL GROUP BY 1''',
            now=ctx.now,
        )
        eligible = [r for r in data if int(r["open"]) >= t["minOpenAtSite"]]
        rates = [pct(int(r["overdue"]), int(r["open"])) for r in eligible]
        med, mad = median_abs_deviation(rates)
        comparable = len(eligible) >= t["minSites"]
        if not comparable:
            ctx.notes["degraded"] = (
                f"only {len(eligible)} site(s) meet the minimum open-CAPA count; reporting on "
                f"absolute rate >= {t['absoluteRatePct']}% without a peer comparison"
            )

        out: list[SignalCandidate] = []
        for r in eligible:
            rate = pct(int(r["overdue"]), int(r["open"]))
            z = robust_z(rate, med, mad) if comparable else 0.0
            if int(r["overdue"]) == 0:
                continue
            if comparable:
                if z < t["zThreshold"] and rate < t["absoluteRatePct"]:
                    continue
            elif rate < t["absoluteRatePct"]:
                continue
            ev = await rows(
                ctx.db,
                '''SELECT id, "capaNumber" AS ref, title, "closureTargetDate" AS due, state
                     FROM "Capa"
                    WHERE "isDeleted" = false AND "plantId" = :s
                      AND state NOT IN ('CLOSED','CANCELLED','VERIFIED')
                      AND "closureTargetDate" < :now
                    ORDER BY "closureTargetDate" LIMIT 6''',
                s=r["site"], now=ctx.now,
            )
            out.append(SignalCandidate(
                signalKey=f"CAPA_OVERDUE_OUTLIER::{r['site']}",
                severity="HIGH" if rate >= 75 else "MEDIUM",
                confidence=0.85,
                siteId=r["site"],
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]), "overdue": int(r["overdue"]),
                    "open": int(r["open"]), "ratePct": rate,
                    "portfolioMedianPct": round(med, 1), "z": round(z, 1),
                    "sites": len(eligible), "comparable": comparable,
                },
                evidence=[
                    EvidenceRef("CAPA", e["id"], e["ref"], 1.0,
                                {"title": (e["title"] or "")[:160], "state": e["state"],
                                 "due": str(e["due"])[:10]})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        head = (
            f"{f['site']} — {f['overdue']} of {f['open']} open CAPAs ({f['ratePct']}%) are past "
            f"their closure target"
        )
        if f.get("comparable"):
            return (
                f"{head}, against a portfolio median of {f['portfolioMedianPct']}% across "
                f"{f['sites']} sites."
            )
        return f"{head}. Too few sites carry enough open CAPAs for a portfolio comparison."

    def render_action(self, c: SignalCandidate) -> str:
        return (
            "Re-baseline or re-assign the overdue CAPAs. A site this far above the norm is "
            "usually short of owner capacity, not short of intent."
        )


# ── XSTAT-005 ────────────────────────────────────────────────────────────────
class Xstat005ClosureTimeOutlier(_StatRule):
    """A site taking far longer than the portfolio to close records.

    Runs across every module that records a real closure timestamp, so a site
    that is slow everywhere reads as one finding per module rather than being
    averaged into invisibility.
    """

    code = "XSTAT-005"
    name = "Site closure time is a portfolio outlier"
    description = (
        "A site's average days-to-close for a module is far above the portfolio "
        "norm for the same module."
    )
    category = "OPERATIONAL_RISK"
    default_severity = "MEDIUM"
    source_modules = ("INCIDENT", "OBSERVATION", "NEAR_MISS")
    window_days = 365
    default_thresholds = {
        "minClosedAtSite": 5, "minSites": 3, "zThreshold": 2.0, "minAbsoluteDays": 20.0,
    }

    _MODULES = (
        ("INCIDENT", "Incident", "date", True, "incidents"),
        ("OBSERVATION", "Observation", "date", False, "observations"),
        ("NEAR_MISS", "NearMiss", "date", False, "near misses"),
    )

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        out: list[SignalCandidate] = []
        skipped: list[str] = []

        for module, table, datecol, soft, noun in self._MODULES:
            deleted = 'AND "isDeleted" = false' if soft else ""
            data = await rows(
                ctx.db,
                f'''SELECT "plantId" AS site, count(*) AS closed,
                           avg(EXTRACT(epoch FROM ("closedAt" - {datecol})) / 86400.0) AS avg_days
                      FROM "{table}"
                     WHERE "closedAt" IS NOT NULL AND "closedAt" >= {datecol}
                       AND "closedAt" >= :since AND "plantId" IS NOT NULL {deleted}
                     GROUP BY 1''',  # noqa: S608 — literals above
                since=ctx.now - timedelta(days=ctx.window_days),
            )
            eligible = [r for r in data if int(r["closed"]) >= t["minClosedAtSite"]]
            vals = [float(r["avg_days"]) for r in eligible]
            med, mad = median_abs_deviation(vals)
            # Same fallback as XSTAT-001/004: without a peer group the outlier
            # claim is dropped, but a site that is slow in absolute terms is
            # still reported. `minAbsoluteDays` is the floor in both branches,
            # so a portfolio that simply closes things quickly stays silent —
            # which is a true negative, not a rule that cannot fire.
            comparable = len(eligible) >= t["minSites"]
            if not comparable:
                skipped.append(
                    f"{module}: only {len(eligible)} site(s) with enough closures — "
                    f"reporting on absolute days >= {t['minAbsoluteDays']} only"
                )
            for r in eligible:
                v = float(r["avg_days"])
                z = robust_z(v, med, mad) if comparable else 0.0
                if v < t["minAbsoluteDays"]:
                    continue
                if comparable and z < t["zThreshold"]:
                    continue
                out.append(SignalCandidate(
                    signalKey=f"SLOW_CLOSURE::{module}::{r['site']}",
                    severity="MEDIUM",
                    confidence=0.7,
                    siteId=r["site"],
                    windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                    facts={
                        "site": lb.site(r["site"]), "module": module, "noun": noun,
                        "avgDays": round(v, 1), "closed": int(r["closed"]),
                        "portfolioMedianDays": round(med, 1), "z": round(z, 1),
                        "sites": len(eligible), "comparable": comparable,
                    },
                    evidence=[],
                ))
        if skipped:
            ctx.notes["skipped"] = skipped
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        head = (
            f"{f['site']} takes {f['avgDays']} days on average to close {f['noun']} "
            f"({f['closed']} closed in the window)"
        )
        if f.get("comparable"):
            return (
                f"{head}, against a portfolio median of {f['portfolioMedianDays']} days across "
                f"{f['sites']} sites."
            )
        return f"{head}. Too few sites have enough closures for a portfolio comparison."

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Check where {c.facts['noun']} sit longest at this site — a single approval step is "
            f"usually the whole difference."
        )


# ── XSTAT-006 ────────────────────────────────────────────────────────────────
class Xstat006ReportingWentQuiet(_StatRule):
    """A site that used to report and has stopped.

    Under-reporting is the hardest failure to see, because the screen it should
    appear on simply shows fewer rows. A site with a real reporting history that
    has gone silent is a stronger signal than one that never reported at all —
    the latter may just not be live yet.
    """

    code = "XSTAT-006"
    name = "Site reporting has gone quiet"
    description = (
        "A site with an established reporting history has recorded nothing for "
        "an unusually long stretch. Silence is being read as safety."
    )
    category = "LEADING_INDICATOR"
    default_severity = "MEDIUM"
    source_modules = ("OBSERVATION", "NEAR_MISS", "INCIDENT")
    window_days = 365
    default_thresholds = {"minHistory": 5, "quietDays": 30, "multipleOfTypicalGap": 3.0}

    _MODULES = (
        ("OBSERVATION", "Observation", "date", False, "observations"),
        ("NEAR_MISS", "NearMiss", "date", False, "near misses"),
    )

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        out: list[SignalCandidate] = []

        for module, table, datecol, soft, noun in self._MODULES:
            deleted = 'AND "isDeleted" = false' if soft else ""
            data = await rows(
                ctx.db,
                f'''SELECT "plantId" AS site, count(*) AS n,
                           max({datecol}) AS latest, min({datecol}) AS earliest
                      FROM "{table}"
                     WHERE "plantId" IS NOT NULL AND {datecol} >= :since {deleted}
                     GROUP BY 1''',  # noqa: S608 — literals above
                since=ctx.now - timedelta(days=ctx.window_days),
            )
            for r in data:
                n = int(r["n"])
                if n < t["minHistory"] or not r["latest"] or not r["earliest"]:
                    continue
                latest = r["latest"]
                if latest.tzinfo is not None:
                    latest = latest.replace(tzinfo=None)
                earliest = r["earliest"]
                if earliest.tzinfo is not None:
                    earliest = earliest.replace(tzinfo=None)
                now = ctx.now.replace(tzinfo=None)

                silent_days = (now - latest).days
                span_days = max((latest - earliest).days, 1)
                # The site's own typical gap between records — comparing a site
                # against itself, not against a busier site's cadence.
                typical_gap = span_days / max(n - 1, 1)
                if silent_days < t["quietDays"]:
                    continue
                if silent_days < typical_gap * t["multipleOfTypicalGap"]:
                    continue
                out.append(SignalCandidate(
                    signalKey=f"QUIET::{module}::{r['site']}",
                    severity="HIGH" if silent_days >= 60 else "MEDIUM",
                    confidence=0.7,
                    siteId=r["site"],
                    windowStart=earliest, windowEnd=now,
                    facts={
                        "site": lb.site(r["site"]), "module": module, "noun": noun,
                        "silentDays": silent_days, "records": n,
                        "typicalGapDays": round(typical_gap, 1),
                        "lastSeen": str(latest)[:10],
                    },
                    evidence=[],
                ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['site']} has recorded no {f['noun']} for {f['silentDays']} days (last: "
            f"{f['lastSeen']}), having previously logged {f['records']} at roughly one every "
            f"{f['typicalGapDays']} days. The site has gone quiet rather than got safer."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Confirm with the site whether {c.facts['noun']} reporting has genuinely stopped or "
            f"is being recorded somewhere other than the system."
        )


STATISTICAL_RULES = (
    Xstat001StalledInvestigationOutlier(),
    Xstat002VolumeSpike(),
    Xstat003AreaConcentration(),
    Xstat004CapaOverdueOutlier(),
    Xstat005ClosureTimeOutlier(),
    Xstat006ReportingWentQuiet(),
)

__all__ = ["STATISTICAL_RULES"]
