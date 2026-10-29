"""Record-number allocation: highest issued suffix + 1, never `count + 1`.

`count(rows) + 1` re-issues an existing number as soon as any row is deleted
(soft deletes hide rows from the count but not from the unique index), and
whenever the count spans a different set of rows than the number stem (e.g.
all years vs. the current year's stem). The create then 500s on the unique
constraint. This scans the numbers already issued under the stem — soft-
deleted rows included — and continues from the highest.

Same rule as business_excellence.next_record_number, for models whose number
stem is not (prefix, year) shaped.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def next_number(db: AsyncSession, *, column: Any, stem: str, width: int = 4) -> str:
    """Next `{stem}{n:0width}` given the numbers already issued under `stem`.

    Only numbers whose remainder after the stem is all digits count, so
    differently-shaped legacy numbers sharing the stem (e.g. seeded
    `INC-2026-NW-HIST-02`) neither break nor inflate the sequence."""
    rows = (
        await db.execute(
            select(column).where(column.like(f"{stem}%")).execution_options(include_deleted=True)
        )
    ).scalars().all()
    highest = 0
    for value in rows:
        rest = (value or "")[len(stem):]
        if rest.isdigit():
            highest = max(highest, int(rest))
    return f"{stem}{highest + 1:0{width}d}"


__all__ = ["next_number"]
