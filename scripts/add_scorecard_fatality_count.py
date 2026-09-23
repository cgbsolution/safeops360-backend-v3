"""EHS Scorecard - fatalityCount column (additive DDL).

Severity rate charges 6,000 days per fatality (IS 3786:1983). The rollup
re-derives quarterly and all-sites rates by summing each month's numerators over
each month's exposure, so without a stored fatality count the monthly severity
rate would include the 6,000-day charge and the quarterly one would silently drop
it. A safety scorecard that cannot count fatalities is also a gap in its own
right.

NOT NULL DEFAULT 0 rather than nullable: a month that was rolled up has a known
fatality count, and 0 is the true value for every existing row (0 FATALITY rows
in Incident, 0 in every Manhours return at the time of writing).

Additive + re-runnable. Never `prisma db push` - it would drop the hand-DDL
tables on this database.

    venv/Scripts/python.exe scripts/add_scorecard_fatality_count.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

STMTS = [
    'ALTER TABLE "ScorecardPeriod" '
    'ADD COLUMN IF NOT EXISTS "fatalityCount" INTEGER NOT NULL DEFAULT 0',
]


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        for stmt in STMTS:
            s.execute(text(stmt))
        s.commit()
        ok = bool(s.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name='ScorecardPeriod' AND column_name='fatalityCount'"
        )).first())
        print(f"  column ScorecardPeriod.fatalityCount  {'present' if ok else 'MISSING'}")
        if ok:
            rows = s.execute(text('SELECT count(*) FROM "ScorecardPeriod"')).scalar_one()
            print(f"  {rows} existing rows defaulted to 0")
    print("\nDONE" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
