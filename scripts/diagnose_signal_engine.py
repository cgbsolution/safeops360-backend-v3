"""READ-ONLY diagnostic for XCORR-019 / XCORR-020 (spec §10 step 6, §9).

Evaluates the Stream 1 rules against whatever database `DATABASE_URL` points at
and prints what they WOULD emit. It writes nothing — no Signal rows, no run log,
no source-data mutation — so it is safe to point at production, and it does not
require the Signal Engine tables to exist yet.

That last property is the point: it lets the rule logic be validated against
ground truth BEFORE any DDL is applied to a live database.

Ground-truth regression check: prior manual diagnostics established that
`HiraHazard.requiresPermit` was true on 0 of 167 rows, `HiraEntryHazard.
consequence` populated on 0 of 96, and `PermitActionEvidence` empty before its
fix. If XCORR-019 does not retroactively surface those, the rule is wrong — so
this script asserts them explicitly and reports found-vs-expected rather than
leaving it to a reader to eyeball the output.

    python -m scripts.diagnose_signal_engine
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.db import AsyncSessionLocal
from app.services.signal_engine import data_access as da
from app.services.signal_engine.base import RuleContext
from app.services.signal_engine.registry import RULES

# (table, column, what the manual diagnostics found). `expected_zero` is what we
# assert; the counts are recorded for context and may legitimately have moved.
GROUND_TRUTH = [
    ("HiraHazard", "requiresPermit", "0/167 at diagnosis (NOT NULL bool, never set true)"),
    ("HiraEntryHazard", "consequence", "0/96 at diagnosis"),
    ("PermitActionEvidence", None, "table empty pre-fix (row count is the check)"),
]


async def _ground_truth(db) -> None:
    print("\n" + "=" * 78)
    print("GROUND TRUTH — known zero-population fields from prior manual diagnostics")
    print("=" * 78)
    for table, column, note in GROUND_TRUTH:
        if not await da.table_exists(db, table):
            print(f"  {table:<26} ABSENT from this database")
            continue
        total = (await db.execute(text(f'SELECT count(*) FROM {da.q(table)}'))).scalar_one()  # noqa: S608
        if column is None:
            print(f"  {table:<26} rows={total}   [{note}]")
            continue
        if not await da.column_exists(db, table, column):
            print(f"  {table}.{column:<18} COLUMN ABSENT   [{note}]")
            continue
        # Match the rule's own definition of populated, including the boolean case.
        kind = (
            await db.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
                ),
                {"t": table, "c": column},
            )
        ).scalar_one()
        if kind == "boolean":
            pop = (
                await db.execute(
                    text(f'SELECT count(*) FILTER (WHERE {da.q(column)} IS TRUE) FROM {da.q(table)}')  # noqa: S608
                )
            ).scalar_one()
        else:
            pop = (
                await db.execute(
                    text(
                        f"SELECT count(*) FILTER (WHERE {da.q(column)} IS NOT NULL AND "  # noqa: S608
                        f"btrim({da.q(column)}::text) NOT IN ('', '{{}}', '[]', 'null')) FROM {da.q(table)}"
                    )
                )
            ).scalar_one()
        pct = round(pop / total * 100, 1) if total else 0.0
        print(f"  {table}.{column:<20} {pop}/{total} populated ({pct}%)   [{note}]")


async def main() -> int:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        await _ground_truth(db)

        flagged: set[str] = set()
        for impl in RULES:
            ctx = RuleContext(
                db=db,
                tenant="default",
                now=now,
                thresholds=dict(impl.default_thresholds),
                window_days=impl.window_days,
            )
            print("\n" + "=" * 78)
            print(f"{impl.code} — {impl.name}")
            print("=" * 78)
            try:
                candidates = await impl.evaluate(ctx)
            except Exception as e:  # noqa: BLE001
                print(f"  RULE FAILED: {type(e).__name__}: {e}")
                continue
            print(f"  thresholds: {impl.default_thresholds}")
            print(f"  notes:      {ctx.notes}")
            print(f"  candidates: {len(candidates)}\n")
            for c in sorted(candidates, key=lambda x: -x.confidence):
                flagged.add(c.signalKey)
                print(f"  [{c.confidence:.3f}] {c.signalKey}")
                print(f"      {impl.render_narrative(c)}")
                if c.evidence:
                    refs = ", ".join((e.sourceRecordRef or e.sourceRecordId[:8]) for e in c.evidence[:3])
                    print(f"      evidence: {len(c.evidence)} record(s) — {refs}")
                print()

        print("=" * 78)
        print("REGRESSION CHECK — did XCORR-019 catch the known ground-truth gaps?")
        print("=" * 78)
        ok = True
        for table, column, _ in GROUND_TRUTH:
            if column is None:
                continue
            key = f"{table}.{column}"
            hit = key in flagged or f"{table}::TABLE" in flagged
            print(f"  {'PASS' if hit else 'MISS'}  {key}")
            ok = ok and hit
        print(
            "\nAll known gaps surfaced." if ok else
            "\nOne or more known gaps were NOT surfaced. Either the data has changed "
            "since diagnosis (check the ground-truth counts above — a field that is "
            "now populated SHOULD no longer fire) or the rule logic is wrong."
        )
        # Read-only: nothing to commit, and an explicit rollback makes that true
        # even if a future edit accidentally leaves the session dirty.
        await db.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
