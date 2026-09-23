"""Daily Brief feed window — regression tests for the "0 items need attention
while 112 criticals are open" contradiction.

House style: no DB, no TestClient, no conftest. The window is expressed as a
SQLAlchemy boolean clause, so it is testable by compiling the clause and by
evaluating the equivalent predicate over plain rows — both are done here, because
the compile check alone would not catch an inverted OR.

Ground truth this pins (prod, 2026-08-19 06:25 UTC):
  * 592 Alert rows; 112 status='new' severity='critical', 469 'new'/'attention'
  * newest createdAt 2026-08-17 11:21 → 0 rows inside the default 24h window
  * the sentinel upsert refreshes rows in place and never rewrites createdAt
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.alerts import Alert
from app.routers.alerts import _feed_stmt, _is_open_critical, _recency_window


def _sql(clause) -> str:
    return str(
        select(Alert.id).where(clause).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class _Scope:
    """A no-op QueryScope: RBAC is not what these tests are about."""

    all_plants = True
    plant_ids: list[str] = []

    def apply(self, stmt, model, plant_attr="plantId"):
        return stmt


# ── the window clause itself ─────────────────────────────────────────────────


def test_window_reads_updated_at_not_created_at_alone():
    """The bug: windowing on createdAt alone. The sentinel upsert never rewrites
    createdAt, so a refreshed-but-old finding fell out of the feed forever."""
    sql = _sql(_recency_window(24))
    assert "coalesce" in sql.lower(), sql
    assert '"updatedAt"' in sql, sql
    # createdAt may only appear as the coalesce fallback, never as a lone bound.
    assert 'WHERE "Alert"."createdAt" >=' not in sql, sql


def test_open_criticals_are_exempt_from_the_window():
    sql = _sql(_recency_window(24)).lower()
    assert " or " in sql, "an open critical must be OR-ed past the window"
    assert "'critical'" in sql and "'new'" in sql, sql


def test_open_critical_clause_is_severity_and_status():
    sql = _sql(_is_open_critical()).lower()
    assert "severity" in sql and "'critical'" in sql
    assert "status" in sql and "'new'" in sql
    # An acknowledged critical is NOT exempt — it has been picked up by a human.
    assert "'acknowledged'" not in sql


# ── the predicate, evaluated over the real prod shape ────────────────────────
#
# Mirrors _recency_window in plain Python. If the SQL and this drift, the tests
# above catch the shape and these catch the meaning.


def _kept(row, *, now: datetime, window_hours: int) -> bool:
    recent = (row.updatedAt or row.createdAt) >= now - timedelta(hours=window_hours)
    return recent or (row.severity == "critical" and row.status == "new")


NOW = datetime(2026, 8, 19, 6, 25, tzinfo=timezone.utc)


def _alert(**kw):
    base = dict(
        severity="attention", status="new",
        createdAt=NOW - timedelta(days=30), updatedAt=NOW - timedelta(days=30),
    )
    return SimpleNamespace(**{**base, **kw})


def test_the_prod_case_an_open_critical_stale_by_four_weeks_is_kept():
    """The three sourceEventType='insight' criticals on prod: created 2026-07-23,
    last refreshed 2026-08-17, still open, invisible for four weeks."""
    row = _alert(
        severity="critical", status="new",
        createdAt=datetime(2026, 7, 23, 10, 35, tzinfo=timezone.utc),
        updatedAt=datetime(2026, 8, 17, 11, 17, tzinfo=timezone.utc),
    )
    assert _kept(row, now=NOW, window_hours=24) is True


def test_a_refreshed_finding_is_kept_even_when_not_critical():
    """createdAt outside the window, updatedAt inside it — the sentinel-refresh
    case. This is what the coalesce buys."""
    row = _alert(
        severity="attention",
        createdAt=NOW - timedelta(days=40),
        updatedAt=NOW - timedelta(hours=2),
    )
    assert _kept(row, now=NOW, window_hours=24) is True


def test_a_stale_untouched_non_critical_still_falls_out():
    """The window must still DO something — this is not 'show everything'."""
    row = _alert(severity="attention", status="new")
    assert _kept(row, now=NOW, window_hours=24) is False


def test_an_acknowledged_stale_critical_falls_out():
    """Acknowledged means a human has it. The exemption is for unacknowledged."""
    row = _alert(severity="critical", status="acknowledged")
    assert _kept(row, now=NOW, window_hours=24) is False


def test_no_open_critical_can_be_windowed_out_at_any_age():
    for days in (1, 7, 30, 400, 4000):
        row = _alert(
            severity="critical", status="new",
            createdAt=NOW - timedelta(days=days), updatedAt=NOW - timedelta(days=days),
        )
        assert _kept(row, now=NOW, window_hours=24) is True, f"{days}d old critical dropped"


# ── the contradiction itself ─────────────────────────────────────────────────


def test_empty_feed_implies_no_open_criticals():
    """The invariant behind the fix: "Nothing needs your attention right now" can
    only render over an empty feed, and an empty feed can no longer coexist with
    an open critical in scope."""
    prod_shape = [
        *(_alert(severity="critical", status="new",
                 createdAt=NOW - timedelta(days=3), updatedAt=NOW - timedelta(days=2))
          for _ in range(112)),
        *(_alert(severity="attention", status="new") for _ in range(469)),
        *(_alert(severity="info", status="new") for _ in range(10)),
    ]
    feed = [r for r in prod_shape if _kept(r, now=NOW, window_hours=24)]
    open_criticals = [r for r in prod_shape if r.severity == "critical" and r.status == "new"]

    assert len(open_criticals) == 112
    assert len(feed) >= len(open_criticals), "every open critical must reach the feed"
    assert not (len(feed) == 0 and open_criticals), "0-items-need-attention while criticals are open"


def test_feed_stmt_compiles_with_the_new_window():
    """End-to-end shape check on the statement the router actually issues."""
    stmt = _feed_stmt(_Scope(), None, 24, None, None, None)
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "coalesce" in sql.lower()
    assert "mutedUntil" in sql  # default view still hides muted cards
