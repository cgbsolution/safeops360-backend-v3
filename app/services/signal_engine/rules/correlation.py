"""Signal Engine — cross-module correlation rules (XCORR-001…008).

These are the reason the engine exists. Each one reads at least two modules and
states something no single-module insight can: the per-screen insight engine can
tell you near misses are up at NW, and it can tell you CAPAs are overdue at NW,
but only this layer can say those two facts are the same problem.

**Every rule below was feasibility-checked against live prod before it was
written.** Three rules from the original plan were CUT rather than shipped,
because the columns they depend on are empty on prod and they could therefore
never fire:

  • incident-occurred-under-an-active-permit — `Incident.activePermitId` is
    populated on 0 of 128 rows (and 0 of 427 observations, 0 of 191 near misses).
  • incident-triggered-training-not-assigned — `triggeredTrainingKeywords` and
    `triggeredTrainingFor` are 0 of 128.
  • observation-contributed-to-incident — `contributedToIncidentId` is 0 of 427.

A rule that runs green and can never fire is worse than no rule: it reads as
"we checked, and there is nothing here". Those three column gaps are real
findings, and they surface where they belong — through the data-quality rules,
which say the field is empty rather than implying the risk is absent. XCORR-003
and XCORR-005 below are the spatial-correlation redesigns that recover the same
questions from columns that ARE populated.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.services.signal_engine.base import (
    EvidenceRef,
    RuleContext,
    SignalCandidate,
    SignalRuleImpl,
)
from app.services.signal_engine.domain import (
    load_labels,
    pct,
    plural,
    rows,
)


class _CorrelationRule(SignalRuleImpl):
    rule_class = "CORRELATION"

    def render_action(self, c: SignalCandidate) -> str:  # pragma: no cover - overridden
        return "Review the linked records."


# ── XCORR-001 ────────────────────────────────────────────────────────────────
class Xcorr001NearMissRisingCapaSlowing(_CorrelationRule):
    """Near-miss reporting climbing at a site whose CAPA backlog is going late.

    The pairing is the finding. Near misses rising on their own is often GOOD —
    it usually means reporting culture improving. It is only alarming when the
    corrective machinery is simultaneously falling behind, because then the
    precursors are being collected and not acted on.
    """

    code = "XCORR-001"
    name = "Near-miss rate rising while CAPA closure slips"
    description = (
        "A site's near-miss reporting is up materially on the prior period while "
        "its CAPA backlog is running late. Precursors are being collected faster "
        "than they are being closed out."
    )
    category = "LEADING_INDICATOR"
    default_severity = "HIGH"
    source_modules = ("NEAR_MISS", "CAPA")
    window_days = 90
    default_thresholds = {
        "minCurrent": 5,          # too few near misses to read a trend
        "riseRatio": 0.20,        # +20% vs the prior equal window
        "capaOverduePct": 25.0,   # and at least this share of open CAPAs late
        "minOpenCapa": 3,
    }

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        w = ctx.window_days
        lb = await load_labels(ctx.db)

        nm = await rows(
            ctx.db,
            '''SELECT "plantId" AS site,
                      count(*) FILTER (WHERE date >= :cur) AS cur,
                      count(*) FILTER (WHERE date >= :prev AND date < :cur) AS prev
                 FROM "NearMiss" WHERE "plantId" IS NOT NULL GROUP BY 1''',
            cur=ctx.now - timedelta(days=w),
            prev=ctx.now - timedelta(days=2 * w),
        )
        capa = {
            r["site"]: r
            for r in await rows(
                ctx.db,
                '''SELECT "plantId" AS site,
                          count(*) FILTER (WHERE state NOT IN ('CLOSED','CANCELLED','VERIFIED')) AS open,
                          count(*) FILTER (WHERE state NOT IN ('CLOSED','CANCELLED','VERIFIED')
                                            AND "closureTargetDate" < :now) AS overdue
                     FROM "Capa" WHERE "isDeleted" = false AND "plantId" IS NOT NULL GROUP BY 1''',
                now=ctx.now,
            )
        }

        out: list[SignalCandidate] = []
        for r in nm:
            cur, prev = int(r["cur"]), int(r["prev"])
            if cur < t["minCurrent"] or prev <= 0:
                continue
            rise = (cur - prev) / prev
            c = capa.get(r["site"])
            if not c or int(c["open"]) < t["minOpenCapa"]:
                continue
            overdue_pct = pct(int(c["overdue"]), int(c["open"]))
            if rise < t["riseRatio"] or overdue_pct < t["capaOverduePct"]:
                continue

            ev = await rows(
                ctx.db,
                '''SELECT id, "capaNumber" AS ref, title, "closureTargetDate" AS due
                     FROM "Capa"
                    WHERE "isDeleted" = false AND "plantId" = :s
                      AND state NOT IN ('CLOSED','CANCELLED','VERIFIED')
                      AND "closureTargetDate" < :now
                    ORDER BY "closureTargetDate" LIMIT 5''',
                s=r["site"], now=ctx.now,
            )
            out.append(SignalCandidate(
                signalKey=f"NM_UP_CAPA_LATE::{r['site']}",
                severity="HIGH" if overdue_pct >= 40 else "MEDIUM",
                confidence=0.8,
                siteId=r["site"],
                windowStart=ctx.now - timedelta(days=w),
                windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]), "current": cur, "prior": prev,
                    "risePct": round(rise * 100, 1), "openCapa": int(c["open"]),
                    "overdueCapa": int(c["overdue"]), "overduePct": overdue_pct,
                    "windowDays": w,
                },
                evidence=[
                    EvidenceRef("CAPA", e["id"], e["ref"], 1.0,
                                {"title": e["title"], "due": str(e["due"])[:10]})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['site']} — near-miss reports are up {f['risePct']}% "
            f"({f['prior']} → {f['current']}) over the last {f['windowDays']} days, while "
            f"{f['overdueCapa']} of {f['openCapa']} open CAPAs ({f['overduePct']}%) are past "
            f"their closure target. The site is finding precursors faster than it is closing them out."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Review the {c.facts['overdueCapa']} overdue CAPAs at this site before the next "
            f"near-miss review; rising reporting only reduces risk if the resulting actions land."
        )


# ── XCORR-002 ────────────────────────────────────────────────────────────────
class Xcorr002IncidentAreaNotInHira(_CorrelationRule):
    """Incidents clustering in an area that no HIRA study has assessed.

    This is the cleanest possible statement of a risk-assessment gap: harm has
    repeatedly occurred somewhere the hazard register has never looked.
    """

    code = "XCORR-002"
    name = "Incident cluster in an area with no HIRA coverage"
    description = (
        "An area has accumulated incidents but appears in no HIRA entry, so the "
        "hazards that produced them have never been formally assessed."
    )
    category = "COMPLIANCE_GAP"
    default_severity = "HIGH"
    source_modules = ("INCIDENT", "HIRA")
    window_days = 365
    default_thresholds = {"minIncidents": 2}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        lb = await load_labels(ctx.db)
        covered = {
            (r["site"], r["area"])
            for r in await rows(
                ctx.db,
                '''SELECT DISTINCT s."plantId" AS site, e."areaId" AS area
                     FROM "HiraEntry" e JOIN "HiraStudy" s ON s.id = e."studyId"
                    WHERE e."areaId" IS NOT NULL''',
            )
        }
        clusters = await rows(
            ctx.db,
            '''SELECT "plantId" AS site, "areaId" AS area, count(*) AS n,
                      max(date) AS latest, min(date) AS earliest
                 FROM "Incident"
                WHERE "isDeleted" = false AND "areaId" IS NOT NULL AND date >= :since
                GROUP BY 1, 2''',
            since=ctx.now - timedelta(days=ctx.window_days),
        )

        out: list[SignalCandidate] = []
        for r in clusters:
            if int(r["n"]) < ctx.thresholds["minIncidents"]:
                continue
            if (r["site"], r["area"]) in covered:
                continue
            ev = await rows(
                ctx.db,
                '''SELECT id, number AS ref, date, type::text AS type, severity::text AS sev
                     FROM "Incident"
                    WHERE "isDeleted" = false AND "areaId" = :a AND "plantId" = :s
                    ORDER BY date DESC LIMIT 6''',
                a=r["area"], s=r["site"],
            )
            n = int(r["n"])
            out.append(SignalCandidate(
                signalKey=f"HIRA_GAP::{r['site']}::{r['area']}",
                severity="HIGH" if n >= 3 else "MEDIUM",
                confidence=0.9,
                siteId=r["site"], areaId=r["area"],
                windowStart=r["earliest"], windowEnd=r["latest"],
                facts={
                    "site": lb.site(r["site"]), "area": lb.area(r["area"]),
                    "incidents": n, "latest": str(r["latest"])[:10],
                },
                evidence=[
                    EvidenceRef("INCIDENT", e["id"], e["ref"], 1.0,
                                {"date": str(e["date"])[:10], "type": e["type"], "severity": e["sev"]})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['area']} at {f['site']} has had {f['incidents']} "
            f"{plural(f['incidents'], 'incident')} (most recent {f['latest']}) but is not "
            f"covered by any HIRA entry — the hazards there have never been formally assessed."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Extend an existing HIRA study to {c.facts['area']}, or raise a new one, using "
            f"the {c.facts['incidents']} linked incidents as the hazard evidence."
        )


# ── XCORR-003 ────────────────────────────────────────────────────────────────
class Xcorr003RepeatObservationsWithHarm(_CorrelationRule):
    """Repeat observations piling up in an area that is also producing harm.

    The redesign of the cut observation→incident FK rule. `isRepeat` IS
    populated (36 of 427), so the same question — "did we see this coming?" —
    is answered spatially instead of by a link nobody writes.
    """

    code = "XCORR-003"
    name = "Repeat observations in an area that is also producing near misses or incidents"
    description = (
        "An area is generating repeat safety observations AND near misses or "
        "incidents. The same condition is being observed more than once and is "
        "also causing events."
    )
    category = "LEADING_INDICATOR"
    default_severity = "MEDIUM"
    source_modules = ("OBSERVATION", "NEAR_MISS", "INCIDENT")
    window_days = 365
    default_thresholds = {"minRepeats": 2, "minHarmEvents": 1}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        data = await rows(
            ctx.db,
            '''WITH obs AS (
                   SELECT "plantId" AS site, "areaId" AS area,
                          count(*) FILTER (WHERE "isRepeat" IS TRUE) AS repeats,
                          count(*) AS total
                     FROM "Observation" WHERE "areaId" IS NOT NULL AND date >= :since GROUP BY 1,2),
                 nm AS (
                   SELECT "plantId" AS site, "areaId" AS area, count(*) AS n
                     FROM "NearMiss" WHERE "areaId" IS NOT NULL AND date >= :since GROUP BY 1,2),
                 inc AS (
                   SELECT "plantId" AS site, "areaId" AS area, count(*) AS n
                     FROM "Incident" WHERE "isDeleted"=false AND "areaId" IS NOT NULL AND date >= :since GROUP BY 1,2)
               SELECT obs.site, obs.area, obs.repeats, obs.total,
                      coalesce(nm.n,0) AS near_misses, coalesce(inc.n,0) AS incidents
                 FROM obs
                 LEFT JOIN nm  ON nm.site = obs.site  AND nm.area  = obs.area
                 LEFT JOIN inc ON inc.site = obs.site AND inc.area = obs.area''',
            since=ctx.now - timedelta(days=ctx.window_days),
        )

        out: list[SignalCandidate] = []
        for r in data:
            repeats = int(r["repeats"])
            harm = int(r["near_misses"]) + int(r["incidents"])
            if repeats < t["minRepeats"] or harm < t["minHarmEvents"]:
                continue
            ev = await rows(
                ctx.db,
                '''SELECT id, number AS ref, date, category::text AS cat, description
                     FROM "Observation"
                    WHERE "areaId" = :a AND "plantId" = :s AND "isRepeat" IS TRUE
                    ORDER BY date DESC LIMIT 5''',
                a=r["area"], s=r["site"],
            )
            out.append(SignalCandidate(
                signalKey=f"REPEAT_OBS_HARM::{r['site']}::{r['area']}",
                severity="HIGH" if int(r["incidents"]) >= 5 else "MEDIUM",
                confidence=0.7,
                siteId=r["site"], areaId=r["area"],
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]), "area": lb.area(r["area"]),
                    "repeats": repeats, "observations": int(r["total"]),
                    "nearMisses": int(r["near_misses"]), "incidents": int(r["incidents"]),
                },
                evidence=[
                    EvidenceRef("OBSERVATION", e["id"], e["ref"], 1.0,
                                {"date": str(e["date"])[:10], "category": e["cat"],
                                 "description": (e["description"] or "")[:160]})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['area']} at {f['site']} — {f['repeats']} of {f['observations']} observations are "
            f"flagged as repeats, and the same area carries {f['nearMisses']} near "
            f"{plural(f['nearMisses'], 'miss', 'misses')} and {f['incidents']} "
            f"{plural(f['incidents'], 'incident')}. The condition is being seen again and again "
            f"and is also causing events."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Treat {c.facts['area']} as a standing agenda item: a repeat observation that keeps "
            f"recurring means the last corrective action did not hold."
        )


# ── XCORR-004 ────────────────────────────────────────────────────────────────
class Xcorr004AuditFindingNoCapa(_CorrelationRule):
    """Open audit findings with no CAPA raised against them.

    A finding with no CAPA has no owner, no due date and no closure path. At
    audit time it will read as "identified" and be indistinguishable from
    "acted on".
    """

    code = "XCORR-004"
    name = "Audit findings with no CAPA raised"
    description = (
        "Open non-conformities from a compliance audit have no CAPA linked, so "
        "nothing in the system will drive them to closure."
    )
    category = "COMPLIANCE_GAP"
    default_severity = "HIGH"
    source_modules = ("CAMS_AUDIT", "CAPA")
    window_days = 365
    default_thresholds = {"minFindings": 1, "criticalSeverities": ["CRITICAL_NC", "MAJOR_NC"]}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        data = await rows(
            ctx.db,
            '''SELECT f."siteId" AS site, f.severity::text AS sev, count(*) AS n
                 FROM "AuditFinding" f
                WHERE f."isDeleted" = false AND f."capaId" IS NULL
                  AND f.status::text NOT IN ('CLOSED','CANCELLED')
                GROUP BY 1, 2''',
        )
        crit = {s.upper() for s in t["criticalSeverities"]}

        out: list[SignalCandidate] = []
        for r in data:
            n = int(r["n"])
            if n < t["minFindings"]:
                continue
            sev_token = (r["sev"] or "UNSPECIFIED").upper()
            ev = await rows(
                ctx.db,
                '''SELECT f.id, f."findingCode" AS ref, f.title, f."dueDate" AS due, a."auditNumber" AS audit
                     FROM "AuditFinding" f LEFT JOIN "ComplianceAudit" a ON a.id = f."auditId"
                    WHERE f."isDeleted" = false AND f."capaId" IS NULL
                      AND f.status::text NOT IN ('CLOSED','CANCELLED')
                      AND f.severity::text = :sev
                      AND (f."siteId" = :s OR (:s IS NULL AND f."siteId" IS NULL))
                    ORDER BY f."dueDate" NULLS LAST LIMIT 6''',
                sev=r["sev"], s=r["site"],
            )
            out.append(SignalCandidate(
                signalKey=f"FINDING_NO_CAPA::{r['site'] or 'GLOBAL'}::{sev_token}",
                severity="CRITICAL" if (sev_token in crit and n >= 10)
                else ("HIGH" if sev_token in crit else "MEDIUM"),
                confidence=0.95,
                siteId=r["site"],
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]) if r["site"] else "the portfolio",
                    "severity": sev_token.replace("_", " ").title(),
                    "count": n,
                },
                evidence=[
                    EvidenceRef("CAMS_AUDIT", e["id"], e["ref"], 1.0,
                                {"title": (e["title"] or "")[:160], "audit": e["audit"],
                                 "due": str(e["due"])[:10] if e["due"] else None})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['count']} open {f['severity']} audit {plural(f['count'], 'finding')} at "
            f"{f['site']} {plural(f['count'], 'has', 'have')} no CAPA raised. Nothing in the "
            f"system is driving {plural(f['count'], 'it', 'them')} to closure."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Raise a CAPA against each of the {c.facts['count']} findings, or record why one is "
            f"not required — an unlinked finding closes only by someone remembering it."
        )


# ── XCORR-005 ────────────────────────────────────────────────────────────────
class Xcorr005PermitDensityWithHarm(_CorrelationRule):
    """High permit density in an area that is also producing events.

    The redesign of the cut "incident under an active permit" rule. That one
    needed `Incident.activePermitId`, which is null on every row; this reads
    permit VOLUME by area, which is fully populated (342 permits), and asks the
    same question at area granularity: where is high-risk work concentrated,
    and is that where people are getting hurt?
    """

    code = "XCORR-005"
    name = "High permit-to-work density in an area that is also producing events"
    description = (
        "An area carries a large share of the site's permitted high-risk work "
        "and is also where near misses and incidents are occurring."
    )
    category = "OPERATIONAL_RISK"
    default_severity = "MEDIUM"
    source_modules = ("PTW", "NEAR_MISS", "INCIDENT")
    window_days = 365
    default_thresholds = {"minPermits": 10, "minEvents": 3, "minSharePct": 40.0}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        data = await rows(
            ctx.db,
            '''WITH p AS (
                   SELECT "plantId" AS site, "areaId" AS area, count(*) AS permits
                     FROM "Permit" WHERE "areaId" IS NOT NULL GROUP BY 1,2),
                 tot AS (SELECT site, sum(permits) AS site_permits FROM p GROUP BY 1),
                 nm AS (SELECT "plantId" AS site, "areaId" AS area, count(*) AS n
                          FROM "NearMiss" WHERE "areaId" IS NOT NULL GROUP BY 1,2),
                 inc AS (SELECT "plantId" AS site, "areaId" AS area, count(*) AS n
                           FROM "Incident" WHERE "isDeleted"=false AND "areaId" IS NOT NULL GROUP BY 1,2)
               SELECT p.site, p.area, p.permits, tot.site_permits,
                      coalesce(nm.n,0) AS near_misses, coalesce(inc.n,0) AS incidents
                 FROM p JOIN tot ON tot.site = p.site
                 LEFT JOIN nm  ON nm.site = p.site  AND nm.area  = p.area
                 LEFT JOIN inc ON inc.site = p.site AND inc.area = p.area''',
        )

        out: list[SignalCandidate] = []
        for r in data:
            permits, events = int(r["permits"]), int(r["near_misses"]) + int(r["incidents"])
            share = pct(permits, int(r["site_permits"]))
            if permits < t["minPermits"] or events < t["minEvents"] or share < t["minSharePct"]:
                continue
            ev = await rows(
                ctx.db,
                '''SELECT id, number AS ref, type::text AS type, "validFrom" AS from_
                     FROM "Permit" WHERE "areaId" = :a AND "plantId" = :s
                    ORDER BY "validFrom" DESC NULLS LAST LIMIT 5''',
                a=r["area"], s=r["site"],
            )
            out.append(SignalCandidate(
                signalKey=f"PTW_DENSITY::{r['site']}::{r['area']}",
                severity="HIGH" if int(r["incidents"]) >= 10 else "MEDIUM",
                confidence=0.65,
                siteId=r["site"], areaId=r["area"],
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]), "area": lb.area(r["area"]),
                    "permits": permits, "sharePct": share,
                    "nearMisses": int(r["near_misses"]), "incidents": int(r["incidents"]),
                },
                evidence=[
                    EvidenceRef("PTW", e["id"], e["ref"], 1.0,
                                {"type": e["type"], "validFrom": str(e["from_"])[:10] if e["from_"] else None})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['area']} at {f['site']} carries {f['permits']} permits — {f['sharePct']}% of the "
            f"site's permitted work — and is also where {f['nearMisses']} near "
            f"{plural(f['nearMisses'], 'miss', 'misses')} and {f['incidents']} "
            f"{plural(f['incidents'], 'incident')} occurred. High-risk work and harm are "
            f"concentrated in the same place."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Sample recent permits in {c.facts['area']} against the events there: if the same "
            f"activity recurs in both, the permit controls are not holding in practice."
        )


# ── XCORR-006 ────────────────────────────────────────────────────────────────
class Xcorr006NearMissPromoted(_CorrelationRule):
    """A near miss that became an incident — the precursor was real.

    Small numbers by nature, and that is the point: each one is a case where the
    organisation had advance warning in its own system and the event happened
    anyway. It belongs in front of a person individually.
    """

    code = "XCORR-006"
    name = "Near miss escalated into an incident"
    description = (
        "A reported near miss was promoted to an incident. The precursor was "
        "captured before the harm occurred."
    )
    category = "LAGGING_PATTERN"
    default_severity = "HIGH"
    source_modules = ("NEAR_MISS", "INCIDENT")
    window_days = 365
    default_thresholds = {"minCount": 1}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        lb = await load_labels(ctx.db)
        data = await rows(
            ctx.db,
            '''SELECT nm."plantId" AS site, count(*) AS n
                 FROM "NearMiss" nm
                WHERE nm."promotedToIncident" IS TRUE AND nm.date >= :since
                GROUP BY 1''',
            since=ctx.now - timedelta(days=ctx.window_days),
        )
        out: list[SignalCandidate] = []
        for r in data:
            n = int(r["n"])
            if n < ctx.thresholds["minCount"]:
                continue
            ev = await rows(
                ctx.db,
                '''SELECT nm.id, nm.number AS ref, nm.date, nm."promotedAt" AS promoted,
                          i.number AS incident
                     FROM "NearMiss" nm
                     LEFT JOIN "Incident" i ON i.id = nm."promotedIncidentId"
                    WHERE nm."promotedToIncident" IS TRUE AND nm."plantId" = :s
                    ORDER BY nm.date DESC LIMIT 6''',
                s=r["site"],
            )
            out.append(SignalCandidate(
                signalKey=f"NM_PROMOTED::{r['site']}",
                severity="HIGH",
                confidence=1.0,
                siteId=r["site"],
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]), "count": n,
                    "incidents": [e["incident"] for e in ev if e["incident"]],
                },
                evidence=[
                    EvidenceRef("NEAR_MISS", e["id"], e["ref"], 1.0,
                                {"date": str(e["date"])[:10], "becameIncident": e["incident"]})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        tail = f" ({', '.join(f['incidents'])})" if f.get("incidents") else ""
        return (
            f"{f['site']} — {f['count']} near {plural(f['count'], 'miss', 'misses')} "
            f"{plural(f['count'], 'was', 'were')} promoted to an incident{tail}. The warning was "
            f"already in the system before the harm occurred."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            "Read the original near-miss report against the incident investigation: what was "
            "recommended at near-miss stage, and why was it not in place?"
        )


# ── XCORR-007 ────────────────────────────────────────────────────────────────
class Xcorr007MocImplementedWithoutAssurance(_CorrelationRule):
    """A change implemented without the assurance step its own risk demanded.

    MOC exists precisely so a change cannot reach the plant without the checks
    its risk implies. A change that has been implemented while its PSSR or
    training requirement sits incomplete is that control failing.
    """

    code = "XCORR-007"
    name = "Change implemented without its required PSSR or training"
    description = (
        "A management-of-change request has reached implementation while a "
        "pre-startup safety review or training requirement it declared remains "
        "incomplete."
    )
    category = "COMPLIANCE_GAP"
    default_severity = "HIGH"
    source_modules = ("MOC", "TRAINING")
    window_days = 365
    default_thresholds = {
        "implementedStates": [
            "implementation_in_progress",
            "implementation_complete_pending_verification",
            "closed_successful",
        ]
    }

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        lb = await load_labels(ctx.db)
        states = [s.lower() for s in ctx.thresholds["implementedStates"]]
        data = await rows(
            ctx.db,
            '''SELECT id, number AS ref, title, "plantId" AS site, status::text AS status,
                      classification::text AS classification,
                      "pssrRequired" AS pssr_required, "pssrConductedAt" AS pssr_at,
                      "trainingRequired" AS training_required,
                      "trainingCertificateId" AS training_cert
                 FROM "ChangeRequest"
                WHERE lower(status::text) = ANY(:states)''',
            states=states,
        )
        out: list[SignalCandidate] = []
        for r in data:
            gaps: list[str] = []
            if r["pssr_required"] and not r["pssr_at"]:
                gaps.append("PSSR not conducted")
            if r["training_required"] and not r["training_cert"]:
                gaps.append("training not evidenced")
            if not gaps:
                continue
            out.append(SignalCandidate(
                signalKey=f"MOC_NO_ASSURANCE::{r['id']}",
                severity="CRITICAL" if (r["classification"] or "").lower() == "major" else "HIGH",
                confidence=0.9,
                siteId=r["site"],
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(r["site"]), "ref": r["ref"],
                    "title": (r["title"] or "")[:120],
                    "classification": (r["classification"] or "unclassified"),
                    "status": (r["status"] or "").replace("_", " "),
                    "gaps": gaps,
                },
                evidence=[EvidenceRef("MOC", r["id"], r["ref"], 1.0,
                                      {"title": (r["title"] or "")[:160], "status": r["status"],
                                       "gaps": gaps})],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['site']} — change {f['ref']} ({f['classification']}) is at "
            f"\"{f['status']}\" with {' and '.join(f['gaps'])}. The change has reached the plant "
            f"without the assurance step it declared for itself."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Complete {' and '.join(c.facts['gaps'])} for {c.facts['ref']}, or formally record "
            f"the deviation with the accountable manager's approval."
        )


# ── XCORR-008 ────────────────────────────────────────────────────────────────
class Xcorr008LotoReviewOverdueAtActiveSite(_CorrelationRule):
    """A lockout procedure past its review date at a site doing live work.

    Overdue on its own is a paperwork finding. Overdue at a site that is
    actively issuing permits and recording events is an energy-isolation
    procedure being relied on after its own review date has passed.
    """

    code = "XCORR-008"
    name = "LOTO procedure past review at a site with active work"
    description = (
        "A lockout/tagout procedure is past its scheduled review date at a site "
        "that is currently issuing permits and recording safety events."
    )
    category = "COMPLIANCE_GAP"
    default_severity = "HIGH"
    source_modules = ("LOTO", "PTW", "INCIDENT")
    window_days = 180
    default_thresholds = {"minSitePermits": 1, "activeStatuses": ["active", "under_review"]}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        lb = await load_labels(ctx.db)
        active = [s.lower() for s in t["activeStatuses"]]
        procs = await rows(
            ctx.db,
            '''SELECT id, "procedureCode" AS ref, title, "siteId" AS site,
                      "equipmentName" AS equipment, "nextReviewDueAt" AS due, status::text AS status
                 FROM "LotoProcedure"
                WHERE "isDeleted" = false AND lower(status::text) = ANY(:active)
                  AND ("nextReviewDueAt" IS NULL OR "nextReviewDueAt" < :now)''',
            active=active, now=ctx.now,
        )
        if not procs:
            return []

        activity = {
            r["site"]: int(r["permits"])
            for r in await rows(
                ctx.db,
                '''SELECT "plantId" AS site, count(*) AS permits FROM "Permit"
                    WHERE "validFrom" >= :since GROUP BY 1''',
                since=ctx.now - timedelta(days=ctx.window_days),
            )
        }

        by_site: dict[str | None, list[dict[str, Any]]] = {}
        for p in procs:
            by_site.setdefault(p["site"], []).append(p)

        out: list[SignalCandidate] = []
        for site, group in by_site.items():
            permits = activity.get(site, 0)
            if permits < t["minSitePermits"]:
                continue
            never = sum(1 for p in group if p["due"] is None)
            out.append(SignalCandidate(
                signalKey=f"LOTO_REVIEW_OVERDUE::{site or 'GLOBAL'}",
                severity="HIGH" if len(group) >= 3 else "MEDIUM",
                confidence=0.85,
                siteId=site,
                windowStart=ctx.now - timedelta(days=ctx.window_days), windowEnd=ctx.now,
                facts={
                    "site": lb.site(site), "count": len(group), "permits": permits,
                    "neverScheduled": never,
                    "procedures": [p["ref"] for p in group[:5]],
                },
                evidence=[
                    EvidenceRef("LOTO", p["id"], p["ref"], 1.0,
                                {"equipment": p["equipment"], "status": p["status"],
                                 "reviewDue": str(p["due"])[:10] if p["due"] else "never scheduled"})
                    for p in group[:6]
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        never = (
            f" ({f['neverScheduled']} of them with no review date ever set)"
            if f["neverScheduled"] else ""
        )
        return (
            f"{f['site']} — {f['count']} active LOTO {plural(f['count'], 'procedure')} "
            f"{plural(f['count'], 'is', 'are')} past review{never}, at a site that has issued "
            f"{f['permits']} permits in the window. Energy isolation is being relied on against "
            f"an unreviewed procedure."
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Re-validate {', '.join(c.facts['procedures'])} against the equipment as it stands "
            f"today before the next isolation."
        )


CORRELATION_RULES = (
    Xcorr001NearMissRisingCapaSlowing(),
    Xcorr002IncidentAreaNotInHira(),
    Xcorr003RepeatObservationsWithHarm(),
    Xcorr004AuditFindingNoCapa(),
    Xcorr005PermitDensityWithHarm(),
    Xcorr006NearMissPromoted(),
    Xcorr007MocImplementedWithoutAssurance(),
    Xcorr008LotoReviewOverdueAtActiveSite(),
)

__all__ = ["CORRELATION_RULES"]
