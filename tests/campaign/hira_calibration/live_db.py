"""Read-only connection to the database in safeops_360_bakend/.env, for the live shadow run.

Two guards, either of which is enough:
  1. Postgres: every connection runs `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`, and the
     runner checks `SHOW transaction_read_only` = on before reading anything. Any INSERT / UPDATE / DELETE /
     DDL then fails inside the database.
  2. Client: a cursor hook refuses any statement that is not a read (SELECT / WITH / SHOW / SET for the
     read-only setting / transaction control) before it is sent. Refusals are counted.
The app's own engine (app.core.db) is never used here.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

ALLOWED = re.compile(r"^\s*(select|with|show|set session characteristics as transaction read only|begin|rollback|commit)\b", re.I)
STATS: dict[str, Any] = {"statements": 0, "refused": [], "connections": 0}


class WriteRefused(RuntimeError):
    pass


def readonly_engine() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    url = get_settings().async_database_url
    pooled = ":6543/" in url or url.endswith(":6543")
    eng = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=4,
        max_overflow=0,
        connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0} if pooled else {},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _on_connect(dbapi_conn: Any, _rec: Any) -> None:  # noqa: ANN401
        STATS["connections"] += 1
        # on the raw asyncpg connection (autocommit), so no rollback can undo it
        dbapi_conn.run_async(lambda c: c.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))

    @event.listens_for(eng.sync_engine, "before_cursor_execute", retval=True)
    def _guard(conn: Any, cursor: Any, statement: str, params: Any, context: Any, executemany: bool) -> tuple[str, Any]:  # noqa: ANN401, FBT001
        STATS["statements"] += 1
        if not ALLOWED.match(statement or ""):
            STATS["refused"].append(statement[:120])
            raise WriteRefused(f"refused a non-read statement: {statement[:80]!r}")
        return statement, params

    return eng, async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def assert_read_only(db: AsyncSession) -> None:
    from sqlalchemy import text  # noqa: PLC0415

    v = (await db.execute(text("SHOW transaction_read_only"))).scalar()
    if v != "on":
        raise RuntimeError(f"connection is not read-only (transaction_read_only={v}); stopping")
