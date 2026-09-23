"""XCORR-019 — silent zero-row pattern detector (spec §3, rule 19).

This rule automates the diagnostic that has, until now, been run by hand against
prod for every module: "the feature shipped, the column exists, and nothing ever
wrote to it." That pattern has been found repeatedly and always the same way —
someone thought to look. HIRA's `requiresPermit` sat at 0 of 167 rows;
`consequence` at 0 of 96; `PermitActionEvidence` was empty while the UI claimed
evidence was captured. Each was invisible on every screen, because a field that
is never written renders as a blank cell, and a blank cell reads as "nothing to
report" rather than "nothing was ever recorded."

Two design choices keep it honest rather than merely loud:

* **It only accuses a table that has had time to speak.** A column is flagged
  only once its table holds `minRows` records AND the oldest of them is older
  than `minAgeDays`. A new module with three rows proves nothing.
* **A wholly-unwired table reports once, not forty times.** If more than
  `tableRollupThreshold` columns on one table are dead, that is one finding
  about the table, not one per column — the remedy is a single conversation.

Audience is admin/engineering, not the safety officer (spec §5.1), which is why
the category is DATA_QUALITY: these are excluded from the executive Daily Brief.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.services.signal_engine import data_access as da
from app.services.signal_engine.base import (
    EvidenceRef,
    RuleContext,
    SignalCandidate,
    SignalRuleImpl,
)


def _pct(n: int, d: int) -> float:
    return round((n / d) * 100, 1) if d else 0.0


def _fmt_cols(cols: list[str], limit: int = 3) -> str:
    shown = ", ".join(cols[:limit])
    extra = len(cols) - min(len(cols), limit)
    return f"{shown}, +{extra} more" if extra > 0 else shown


class Xcorr019SilentZeroRow(SignalRuleImpl):
    code = "XCORR-019"
    name = "Silent zero-row field detector"
    description = (
        "Flags fields that are effectively never populated despite their table "
        "having been live and accumulating records — the 'built, deployed, zero "
        "rows' pattern. Admin/engineering audience, not safety."
    )
    rule_class = "DATA_QUALITY"
    category = "DATA_QUALITY"
    default_severity = "INFO"
    source_modules = ("PLATFORM",)
    window_days = 0  # whole-table state, not a rolling window
    default_thresholds: dict[str, Any] = {
        # Below this many rows the table cannot support a conclusion.
        "minRows": 30,
        # A field on a table younger than this may simply be awaiting traffic.
        "minAgeDays": 30,
        # Populated ratio at or below which a field counts as effectively dead.
        "nearZeroRatio": 0.05,
        # More dead columns than this on one table → a single table-level signal.
        "tableRollupThreshold": 5,
        # Sample evidence rows per finding.
        "sampleSize": 5,
    }

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        min_rows = int(t["minRows"])
        min_age = int(t["minAgeDays"])
        near_zero = float(t["nearZeroRatio"])
        rollup_at = int(t["tableRollupThreshold"])
        sample = int(t["sampleSize"])

        out: list[SignalCandidate] = []
        scanned = skipped_small = skipped_young = 0

        for spec in da.scannable_tables():
            pop = await da.scan_population(ctx.db, spec)
            if pop is None:
                continue  # table not present in this database (module unapplied)
            scanned += 1
            if pop.total < min_rows:
                skipped_small += 1
                continue

            age_days: int | None = None
            if pop.oldest_row is not None:
                # `ctx.now` is naive UTC (see RuleContext.now), but createdAt is
                # timestamptz on the newer SQLAlchemy-era tables and naive on the
                # Prisma-era ones. Normalise the ROW to the clock, not the other
                # way round — the previous form adapted the clock to the row and
                # broke the moment the clock itself became naive.
                oldest = pop.oldest_row
                if oldest.tzinfo is not None:
                    oldest = oldest.replace(tzinfo=None)
                age_days = (ctx.now - oldest).days
                if age_days < min_age:
                    skipped_young += 1
                    continue

            dead = [
                col for col, n in pop.populated.items()
                if (n / pop.total) <= near_zero
            ]
            if not dead:
                continue

            window_start = ctx.now - timedelta(days=age_days or 0)
            common = {
                "module": spec.module,
                "table": spec.table,
                "totalRows": pop.total,
                "ageDays": age_days,
            }

            if len(dead) > rollup_at:
                # One conversation, one signal. Reporting 20 columns separately
                # would bury every OTHER table's single real finding.
                out.append(SignalCandidate(
                    signalKey=f"{spec.table}::TABLE",
                    severity=self.default_severity,
                    confidence=self._confidence(0, pop.total, age_days),
                    facts={
                        **common,
                        "scope": "TABLE",
                        "deadColumns": sorted(dead),
                        "deadCount": len(dead),
                        "scannedColumns": len(spec.columns),
                    },
                    evidence=[],  # the finding is the table, not any one row
                    windowStart=window_start,
                    windowEnd=ctx.now,
                ))
                continue

            for col in sorted(dead):
                n = pop.populated[col]
                rows = await da.sample_ids_missing(ctx.db, spec, col, limit=sample)
                out.append(SignalCandidate(
                    signalKey=f"{spec.table}.{col}",
                    severity=self.default_severity,
                    confidence=self._confidence(n, pop.total, age_days),
                    facts={
                        **common,
                        "scope": "COLUMN",
                        "column": col,
                        "isFlag": col in spec.bool_columns,
                        "populated": n,
                        "populatedPct": _pct(n, pop.total),
                    },
                    evidence=[
                        EvidenceRef(
                            sourceModule=spec.module,
                            sourceRecordId=rid,
                            sourceRecordRef=ref,
                            weight=round(1.0 / max(len(rows), 1), 3),
                            snapshot={"table": spec.table, "column": col, "value": None},
                        )
                        for rid, ref in rows
                    ],
                    windowStart=window_start,
                    windowEnd=ctx.now,
                ))

        ctx.notes["tablesScanned"] = scanned
        ctx.notes["skippedBelowMinRows"] = skipped_small
        ctx.notes["skippedBelowMinAge"] = skipped_young
        return out

    # ── Confidence: "signal strength", not a learned probability (spec §4.4) ──
    # Three deterministic factors, each reproducible from the signal's own facts:
    #   emptiness (0.5) — a hard zero is stronger evidence than 4%
    #   volume    (0.25) — 500 rows saying nothing beats 30 rows saying nothing
    #   age       (0.25) — 6 months of silence beats 5 weeks of it
    @staticmethod
    def _confidence(populated: int, total: int, age_days: int | None) -> float:
        emptiness = 1.0 if populated == 0 else max(0.0, 1.0 - (populated / total) / 0.05)
        volume = min(total / 200.0, 1.0)
        age = min((age_days or 0) / 90.0, 1.0)
        return round(0.5 * emptiness + 0.25 * volume + 0.25 * age, 3)

    @staticmethod
    def _age_clause(age: int | None, total: int) -> tuple[str, str]:
        """(observation, connector) for the age evidence.

        When a table carries no `createdAt` there is no age to state — and
        asserting one anyway ("accumulating for an unknown period, so this is
        not new") claims something the data does not support. The connector
        differs accordingly: settled fact vs something to go and check."""
        if age is None:
            return (
                f"The table holds {total} rows but carries no creation timestamp "
                f"to age-check against",
                "so confirm when the module went live before treating this as settled —",
            )
        return (
            f"The table has been accumulating records for {age} days",
            "so this is not new and awaiting traffic:",
        )

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        age, connector = self._age_clause(f.get("ageDays"), f["totalRows"])
        never = f["populated"] == 0 if f.get("scope") == "COLUMN" else True

        if f.get("scope") == "TABLE":
            return (
                f"{f['module']} — {f['deadCount']} of {f['scannedColumns']} fields on "
                f"{f['table']} are effectively unpopulated across all {f['totalRows']} "
                f"rows ({_fmt_cols(f['deadColumns'])}). {age}, {connector} "
                f"no code path writes them."
            )

        if f.get("isFlag"):
            # A NOT NULL boolean is never "empty" — it is false. Saying
            # "unpopulated" about a flag would read as a schema complaint; what
            # the reader needs to know is how often anyone has ticked it. And
            # "never" is only claimed when the count is actually zero.
            tail = (
                "no user or code path has ever turned it on."
                if never else
                f"only {f['populated']} row(s) have it on, too few to be carrying the "
                f"meaning the field implies."
            )
            return (
                f"{f['module']} — {f['table']}.{f['column']} is set on {f['populated']} "
                f"of {f['totalRows']} rows ({f['populatedPct']}%). {age}, {connector} {tail}"
            )

        tail = (
            "no code path writes it."
            if never else
            f"only {f['populated']} row(s) carry a value, too few for anything "
            f"downstream to rely on."
        )
        return (
            f"{f['module']} — {f['table']}.{f['column']} is populated on "
            f"{f['populated']} of {f['totalRows']} rows ({f['populatedPct']}%). "
            f"{age}, {connector} {tail}"
        )

    def render_action(self, c: SignalCandidate) -> str:
        f = c.facts
        target = f["table"] if f.get("scope") == "TABLE" else f"{f['table']}.{f['column']}"
        return (
            f"Trace the write path for {target}. Either the screen or API meant to "
            f"capture it was never wired, or the field was superseded and should be "
            f"removed. Check any report, export or dashboard that reads it first — "
            f"those are currently showing a client an empty column and calling it data."
        )


__all__ = ["Xcorr019SilentZeroRow"]
