"""Signal Engine — data-quality rules (XDQ-021…024).

These join XCORR-019 (silent zero-row fields) and XCORR-020 (reference
integrity), which keep their original codes: a rule code is the identity of
every signal and operator override already persisted under it, and renaming one
would orphan both.

The four here exist because of a pattern this platform keeps reproducing — a
feature that is present, wired and running, and silently doing nothing. The
build that added this file found three fresh instances in an afternoon:

  • `KaizenPost` maps five columns the database does not have, so every query
    against it 500s (XDQ-021).
  • `Incident.statutoryDeadline` is null on all 128 rows, so the Overdue KPI
    could only ever report zero (XDQ-022).
  • `signal_engine_scan` — this engine's own nightly job — had failed on all
    nine of its runs and never emitted a signal (XDQ-024).

None of these was visible on any screen. The point of these rules is that the
next one will be.
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
from app.services.signal_engine.data_access import schema_drift
from app.services.signal_engine.domain import pct, plural, rows


class _DqRule(SignalRuleImpl):
    rule_class = "DATA_QUALITY"
    category = "DATA_QUALITY"

    def render_action(self, c: SignalCandidate) -> str:  # pragma: no cover - overridden
        return "Review the schema or the affected records."


# ── XDQ-021 ──────────────────────────────────────────────────────────────────
class Xdq021SchemaDrift(_DqRule):
    """Mapped tables and columns the database does not have.

    The highest-consequence data-quality finding this engine can make, because
    the failure is total rather than partial: SQLAlchemy names every mapped
    column in its projection, so ONE missing column breaks every read of that
    entity — including reads that never mention it. The screen returns a 500 and
    nothing in the UI says why.
    """

    code = "XDQ-021"
    name = "ORM model and database schema have drifted"
    description = (
        "A mapped table or column is missing from the database. Every query "
        "against that entity fails, not only the ones that read the column."
    )
    default_severity = "CRITICAL"
    source_modules = ("PLATFORM",)
    window_days = 0
    default_thresholds: dict[str, Any] = {}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        drifts = await schema_drift(ctx.db)
        out: list[SignalCandidate] = []
        for d in drifts:
            if d.table_missing:
                out.append(SignalCandidate(
                    signalKey=f"DRIFT_TABLE::{d.table}",
                    # A table that is entirely absent is usually a module not yet
                    # applied, which is a deployment state rather than a live
                    # breakage — real, but not the same emergency as a broken read.
                    severity="HIGH",
                    confidence=1.0,
                    facts={"table": d.table, "module": d.module, "kind": "table"},
                    evidence=[EvidenceRef("PLATFORM", d.table, d.table, 1.0,
                                          {"module": d.module, "missing": "entire table"})],
                ))
            else:
                out.append(SignalCandidate(
                    signalKey=f"DRIFT_COLUMNS::{d.table}",
                    severity="CRITICAL",
                    confidence=1.0,
                    facts={
                        "table": d.table, "module": d.module, "kind": "columns",
                        "columns": list(d.missing_columns), "rows": d.row_count,
                    },
                    evidence=[EvidenceRef("PLATFORM", d.table, d.table, 1.0,
                                          {"module": d.module,
                                           "missingColumns": list(d.missing_columns),
                                           "rows": d.row_count})],
                ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        if f["kind"] == "table":
            return (
                f"{f['module']} — the table `{f['table']}` is mapped in code but does not exist "
                f"in the database. Any feature that reads it will fail."
            )
        cols = ", ".join(f["columns"])
        n = len(f["columns"])
        return (
            f"{f['module']} — `{f['table']}` maps {n} {plural(n, 'column')} the database does not "
            f"have ({cols}). Every query against this table fails, including the {f['rows']} "
            f"existing rows, because all mapped columns are named in the projection."
        )

    def render_action(self, c: SignalCandidate) -> str:
        f = c.facts
        if f["kind"] == "table":
            return f"Apply the DDL that creates `{f['table']}`, or remove the model if the module was dropped."
        return (
            f"Apply the DDL adding {', '.join(f['columns'])} to `{f['table']}` — or revert the "
            f"model. Until one or the other happens the entity is unreadable."
        )


# ── XDQ-022 ──────────────────────────────────────────────────────────────────
class Xdq022MetricBlockingEmptiness(_DqRule):
    """A field a published KPI depends on, empty across the board.

    Distinct from XCORR-019, which flags any field that is unpopulated. This one
    only looks at fields a KPI is actually COMPUTED FROM, and says so in the
    KPI's language — because the consequence is not "a field is empty", it is
    "a number on a dashboard is not what it appears to be".

    The registry below is deliberately hand-written and short. It is a claim
    about what the product publishes, and that cannot be inferred from a schema.
    """

    code = "XDQ-022"
    name = "A published KPI cannot be computed from the data"
    description = (
        "A field that a dashboard metric is derived from is empty on most or all "
        "records, so the metric reports a number it has no basis for."
    )
    default_severity = "HIGH"
    source_modules = ("INCIDENT", "OBSERVATION", "NEAR_MISS", "CAPA", "CAMS_AUDIT")
    window_days = 0
    default_thresholds = {"warnAtMissingPct": 50.0, "minRows": 20}

    # (module, table, column, soft-delete, the KPI it feeds, what it reads as when empty)
    _KPIS: tuple[tuple[str, str, str, bool, str, str], ...] = (
        ("INCIDENT", "Incident", "statutoryDeadline", True,
         "Overdue incidents", "zero incidents are overdue"),
        ("OBSERVATION", "Observation", "targetDate", False,
         "Overdue observations", "zero observations are overdue"),
        ("NEAR_MISS", "NearMiss", "targetDate", False,
         "Overdue near misses", "zero near misses are overdue"),
        ("CAPA", "Capa", "closureTargetDate", True,
         "Overdue CAPAs", "zero CAPAs are overdue"),
        ("CAMS_AUDIT", "AuditFinding", "dueDate", True,
         "Overdue audit findings", "zero findings are overdue"),
        ("INCIDENT", "Incident", "closedAt", True,
         "Average days to close", "closed incidents are excluded from the average"),
        ("OBSERVATION", "Observation", "closedAt", False,
         "Average days to close", "closed observations are excluded from the average"),
    )

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        out: list[SignalCandidate] = []
        for module, table, column, soft, kpi, consequence in self._KPIS:
            deleted = 'WHERE "isDeleted" = false' if soft else ""
            # `closedAt` is only expected on a closed record, so the denominator
            # for a closure timestamp is closed rows, not all rows. Measuring it
            # against everything would report a healthy backlog as a defect.
            if column == "closedAt":
                where = (deleted + (" AND " if deleted else "WHERE ")
                         + "status::text = 'CLOSED'")
            else:
                where = deleted
            try:
                r = (await rows(
                    ctx.db,
                    f'''SELECT count(*) AS total, count("{column}") AS filled
                          FROM "{table}" {where}''',  # noqa: S608 — literals above
                ))[0]
            except Exception as e:  # noqa: BLE001 — a module not deployed is not a finding
                ctx.notes.setdefault("unavailable", []).append(f"{table}.{column}: {str(e)[:80]}")
                continue

            total, filled = int(r["total"]), int(r["filled"])
            if total < t["minRows"]:
                continue
            missing = total - filled
            missing_pct = pct(missing, total)
            if missing_pct < t["warnAtMissingPct"]:
                continue
            out.append(SignalCandidate(
                signalKey=f"KPI_BLOCKED::{table}.{column}",
                severity="CRITICAL" if missing == total else "HIGH",
                confidence=1.0,
                facts={
                    "module": module, "table": table, "column": column, "kpi": kpi,
                    "total": total, "missing": missing, "missingPct": missing_pct,
                    "consequence": consequence, "complete": missing == total,
                },
                evidence=[EvidenceRef(module, f"{table}.{column}", f"{table}.{column}", 1.0,
                                      {"missing": missing, "total": total, "kpi": kpi})],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        scope = "every one of" if f["complete"] else f"{f['missing']} of"
        return (
            f"\"{f['kpi']}\" cannot be computed: `{f['table']}.{f['column']}` is empty on "
            f"{scope} {f['total']} records ({f['missingPct']}%). The metric currently reports that "
            f"{f['consequence']}, which is an absence of data, not a result."
        )

    def render_action(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"Either populate `{f['column']}` at capture time, or stop publishing "
            f"\"{f['kpi']}\" until it is populated — a reassuring number with no basis is worse "
            f"than no number."
        )


# ── XDQ-023 ──────────────────────────────────────────────────────────────────
class Xdq023TerminalWithoutTimestamp(_DqRule):
    """Records in a terminal state whose closure timestamp was never written.

    A silent, compounding integrity fault. The record LOOKS closed on every list
    and counter, but it is invisible to anything that measures closure — the
    trend chart's closed bar, days-to-close, on-time percentage. Nothing errors;
    the denominators just quietly shrink.
    """

    code = "XDQ-023"
    name = "Closed records with no closure timestamp"
    description = (
        "Records sit in a terminal state with no closure date recorded, so they "
        "are counted as closed but excluded from every closure metric."
    )
    default_severity = "MEDIUM"
    source_modules = ("INCIDENT", "OBSERVATION", "NEAR_MISS", "CAPA", "PTW")
    window_days = 0
    default_thresholds = {"minAffected": 1, "warnAtPct": 5.0}

    # (module, table, terminal predicate, timestamp column, soft-delete, ref column)
    _TARGETS: tuple[tuple[str, str, str, str, bool, str], ...] = (
        ("INCIDENT", "Incident", "status::text = 'CLOSED'", "closedAt", True, "number"),
        ("OBSERVATION", "Observation", "status::text = 'CLOSED'", "closedAt", False, "number"),
        ("NEAR_MISS", "NearMiss", "status::text = 'CLOSED'", "closedAt", False, "number"),
        ("CAPA", "Capa", "state = 'CLOSED'", "closedAt", True, "capaNumber"),
        ("PTW", "Permit", "status::text = 'CLOSED'", "closedAt", False, "number"),
    )

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        out: list[SignalCandidate] = []
        for module, table, terminal, tscol, soft, refcol in self._TARGETS:
            deleted = ' AND "isDeleted" = false' if soft else ""
            try:
                r = (await rows(
                    ctx.db,
                    f'''SELECT count(*) AS closed,
                               count(*) FILTER (WHERE "{tscol}" IS NULL) AS missing
                          FROM "{table}" WHERE {terminal}{deleted}''',  # noqa: S608
                ))[0]
            except Exception as e:  # noqa: BLE001
                ctx.notes.setdefault("unavailable", []).append(f"{table}: {str(e)[:80]}")
                continue

            closed, missing = int(r["closed"]), int(r["missing"])
            if missing < t["minAffected"] or closed == 0:
                continue
            share = pct(missing, closed)
            if share < t["warnAtPct"]:
                continue
            ev = await rows(
                ctx.db,
                f'''SELECT id, "{refcol}" AS ref FROM "{table}"
                     WHERE {terminal}{deleted} AND "{tscol}" IS NULL LIMIT 6''',  # noqa: S608
            )
            out.append(SignalCandidate(
                signalKey=f"NO_CLOSURE_TS::{table}",
                severity="HIGH" if share >= 25 else "MEDIUM",
                confidence=1.0,
                facts={
                    "module": module, "table": table, "column": tscol,
                    "missing": missing, "closed": closed, "sharePct": share,
                },
                evidence=[
                    EvidenceRef(module, e["id"], e["ref"], 1.0, {"issue": f"{tscol} is null"})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"{f['missing']} of {f['closed']} closed {f['table']} records ({f['sharePct']}%) have "
            f"no `{f['column']}`. They count as closed everywhere and are silently excluded from "
            f"days-to-close, the monthly closed series and on-time percentage."
        )

    def render_action(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"Backfill `{f['column']}` for the {f['missing']} affected records and make the "
            f"closure transition write it, so the gap cannot reopen."
        )


# ── XDQ-024 ──────────────────────────────────────────────────────────────────
class Xdq024JobFailingSilently(_DqRule):
    """A scheduled job that keeps failing, or has never once succeeded.

    The rule that would have caught this engine. `signal_engine_scan` was
    registered, scheduled, and ran on time every night — and failed on all nine
    of its runs, writing zero signals, with nothing anywhere in the product
    saying so. A job's failures are recorded in `JobRun` and read by nobody.
    """

    code = "XDQ-024"
    name = "Scheduled job failing silently"
    description = (
        "A background job's recent runs are predominantly failures, or it has "
        "never succeeded. The feature it powers appears present but produces "
        "nothing."
    )
    default_severity = "HIGH"
    source_modules = ("PLATFORM",)
    window_days = 30
    default_thresholds = {"minRuns": 3, "failPct": 50.0, "lookbackDays": 30}

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        data = await rows(
            ctx.db,
            '''SELECT "jobId" AS job, count(*) AS runs,
                      count(*) FILTER (WHERE status = 'SUCCESS') AS ok,
                      count(*) FILTER (WHERE status = 'FAILED') AS failed,
                      max("startedAt") AS last_run,
                      max("startedAt") FILTER (WHERE status = 'SUCCESS') AS last_ok
                 FROM "JobRun" WHERE "startedAt" >= :since GROUP BY 1''',
            since=ctx.now - timedelta(days=t["lookbackDays"]),
        )
        out: list[SignalCandidate] = []
        for r in data:
            runs, ok, failed = int(r["runs"]), int(r["ok"]), int(r["failed"])
            if runs < t["minRuns"]:
                continue
            fail_pct = pct(failed, runs)
            if fail_pct < t["failPct"]:
                continue
            ev = await rows(
                ctx.db,
                '''SELECT id, "startedAt" AS at, error FROM "JobRun"
                    WHERE "jobId" = :j AND status = 'FAILED'
                    ORDER BY "startedAt" DESC LIMIT 3''',
                j=r["job"],
            )
            never = ok == 0
            out.append(SignalCandidate(
                signalKey=f"JOB_FAILING::{r['job']}",
                severity="CRITICAL" if never else "HIGH",
                confidence=1.0,
                facts={
                    "job": r["job"], "runs": runs, "failed": failed, "ok": ok,
                    "failPct": fail_pct, "never": never,
                    "lastOk": str(r["last_ok"])[:10] if r["last_ok"] else None,
                    "lookbackDays": t["lookbackDays"],
                    "lastError": (ev[0]["error"] or "")[:200] if ev else None,
                },
                evidence=[
                    EvidenceRef("PLATFORM", e["id"], r["job"], 1.0,
                                {"startedAt": str(e["at"])[:19], "error": (e["error"] or "")[:300]})
                    for e in ev
                ],
            ))
        return out

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        head = (
            f"`{f['job']}` has never succeeded"
            if f["never"]
            else f"`{f['job']}` last succeeded on {f['lastOk']}"
        )
        err = f" Last error: {f['lastError']}" if f["lastError"] else ""
        return (
            f"{head} — {f['failed']} of its {f['runs']} runs in the last {f['lookbackDays']} days "
            f"failed ({f['failPct']}%). Whatever this job produces has not been produced.{err}"
        )

    def render_action(self, c: SignalCandidate) -> str:
        return (
            f"Read the recorded error for `{c.facts['job']}` and fix or disable it. A job that "
            f"fails on schedule is worse than one that is switched off, because it looks live."
        )


DATA_QUALITY_RULES = (
    Xdq021SchemaDrift(),
    Xdq022MetricBlockingEmptiness(),
    Xdq023TerminalWithoutTimestamp(),
    Xdq024JobFailingSilently(),
)

__all__ = ["DATA_QUALITY_RULES"]
