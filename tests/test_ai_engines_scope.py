"""Scope + correctness of the deterministic AI engines — offline (no DB).

Covers the fixes for: insights aggregating every plant for any logged-in user,
multi-plant merge collisions, signals visible across plants, the weekly
snapshot week lookup on non-UTC hosts, and plant validation. The DB-bound rule
functions are exercised against the live flow, not here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import insights as insights_router
from app.routers import signals as signals_router
from app.schemas.insights import Insight, InsightResponse, Signal
from app.services.insights import access, cache, engine
from app.services.insights.weekly import repo


def _card(cid: str, sev: str = "high") -> Insight:
    return Insight(id=cid, kind="overdue_escalation", severity=sev, headline=f"h {cid}",
                   evidence="e", confidence="medium")


def _resp(bar, signals=(), n=10, suppressed=False) -> InsightResponse:
    return InsightResponse(module="incident", plant=None, generatedAt=datetime.now(timezone.utc),
                           bar=list(bar), signals=list(signals), recordCount=n,
                           suppressed=suppressed, reason=None, cached=False)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    """Answers the Plant(id, code) lookup compute_for_plants makes."""

    def __init__(self, codes):
        self.codes = codes

    async def execute(self, _stmt):
        return _Rows(list(self.codes.items()))


# ─── multi-plant merge ──────────────────────────────────────────────────────

async def test_merge_keeps_each_plants_card_and_disambiguates_ids(monkeypatch):
    per_plant = {
        "p1": _resp([_card("incident:overdue:stalled", "critical"), _card("only:p1")],
                    [Signal(recordId="r1", recordRef="INC-1", kind="duplicate", severity="watch",
                            label="dup", evidence="e", filterHref="?insight=incident:overdue:stalled")], n=7),
        "p2": _resp([_card("incident:overdue:stalled", "high")], n=5),
    }

    async def fake_compute(db, module, *, plant=None, date_from=None, date_to=None):
        return per_plant[plant]

    monkeypatch.setattr(engine, "compute", fake_compute)
    out = await engine.compute_for_plants(_FakeDb({"p1": "NW", "p2": "SW"}), "incident", ["p1", "p2"])

    ids = [c.id for c in out.bar]
    assert "incident:overdue:stalled@NW" in ids and "incident:overdue:stalled@SW" in ids
    assert "only:p1" in ids  # unique ids are left alone
    assert len(set(ids)) == len(ids)
    assert out.bar[0].id == "incident:overdue:stalled@NW"  # critical ranks first
    assert out.recordCount == 12 and out.suppressed is False
    # the chip that linked to the renamed card follows it
    assert out.signals[0].filterHref == "?insight=incident:overdue:stalled@NW"


async def test_merge_with_no_plants_is_suppressed():
    out = await engine.compute_for_plants(_FakeDb({}), "incident", [])
    assert out.suppressed and out.reason == "no_accessible_plants" and out.bar == []


async def test_merge_suppressed_only_when_every_plant_is(monkeypatch):
    async def fake_compute(db, module, *, plant=None, date_from=None, date_to=None):
        return _resp([], n=2, suppressed=True)

    monkeypatch.setattr(engine, "compute", fake_compute)
    out = await engine.compute_for_plants(_FakeDb({"a": "A", "b": "B"}), "incident", ["a", "b"])
    assert out.suppressed and out.recordCount == 4


# ─── aggregate access rule ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "scopes,plants,allowed,expected_plants",
    [
        ({"ALL_PLANTS"}, None, True, None),
        ({"OWN_PLANT"}, ["p1", "p2"], True, ["p1", "p2"]),
        ({"OWN_RECORDS"}, ["p1"], False, []),
        ({"OWN_DEPARTMENT"}, ["p1"], False, []),
        (set(), [], False, []),
    ],
)
async def test_aggregate_access_needs_plant_level_grant(monkeypatch, scopes, plants, allowed, expected_plants):
    async def fake_scopes(db, uid, code):
        return scopes

    async def fake_plants(db, uid, code):
        return plants

    monkeypatch.setattr(access, "permission_scopes", fake_scopes)
    monkeypatch.setattr(access, "get_accessible_plants_for", fake_plants)
    a = await access.aggregate_access(None, "u", "INCIDENT.READ")
    assert a.allowed is allowed and a.plants == expected_plants


def test_every_insight_module_has_a_read_permission():
    from app.services.insights import SUPPORTED_MODULES

    assert set(SUPPORTED_MODULES) <= set(access.INSIGHT_MODULE_READ)


# ─── insights router paths ──────────────────────────────────────────────────

def _patch_access(monkeypatch, a: access.AggregateAccess):
    async def fake(db, uid, code):
        return a

    monkeypatch.setattr(insights_router, "aggregate_access", fake)


async def test_router_restricted_reader_gets_empty_bar(monkeypatch):
    _patch_access(monkeypatch, access.AggregateAccess(allowed=False, plants=[]))
    r = await insights_router.module_insights("incident", plant=None, date_from=None, date_to=None,
                                              user=SimpleNamespace(id="u"), db=None)
    assert r.suppressed and r.reason == "restricted" and r.recordCount == 0


async def test_router_refuses_plant_outside_scope(monkeypatch):
    _patch_access(monkeypatch, access.AggregateAccess(allowed=True, plants=["p1"]))
    with pytest.raises(HTTPException) as e:
        await insights_router.module_insights("incident", plant="p9", date_from=None, date_to=None,
                                              user=SimpleNamespace(id="u"), db=None)
    assert e.value.status_code == 403


async def test_router_fans_out_over_readers_plants(monkeypatch):
    _patch_access(monkeypatch, access.AggregateAccess(allowed=True, plants=["p1", "p2"]))
    seen = {}

    async def fake_merge(db, module, plants, **kw):
        seen["plants"] = plants
        return _resp([])

    async def no_global(*a, **k):  # an unscoped compute must not happen
        raise AssertionError("compute(plant=None) called for a plant-scoped reader")

    monkeypatch.setattr(insights_router, "compute_for_plants", fake_merge)
    monkeypatch.setattr(insights_router, "compute", no_global)
    await insights_router.module_insights("incident", plant=None, date_from=None, date_to=None,
                                          user=SimpleNamespace(id="u"), db=None)
    assert seen["plants"] == ["p1", "p2"]


async def test_weekly_restricted_reader_gets_empty_view(monkeypatch):
    _patch_access(monkeypatch, access.AggregateAccess(allowed=False, plants=[]))
    v = await insights_router.module_weekly("observation", plant=None, user=SimpleNamespace(id="u"), db=None)
    assert v["scope"] == "restricted" and v["hero"] is None and v["row"] == []


# ─── weekly snapshot week lookup ────────────────────────────────────────────

def test_weekly_repo_marks_naive_weeks_as_utc():
    naive = datetime(2026, 10, 19)
    assert repo._utc(naive) == datetime(2026, 10, 19, tzinfo=timezone.utc)
    aware = datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert repo._utc(aware) is aware
    assert repo._utc(None) is None


# ─── signal visibility ──────────────────────────────────────────────────────

async def test_signal_sites_follow_source_module_grants(monkeypatch):
    grants = {"INCIDENT.READ": ["p1"], "NEAR_MISS.READ": ["p2"], "CAPA.READ": None}

    async def fake(db, uid, code):
        if code in grants:
            return access.AggregateAccess(allowed=True, plants=grants[code])
        return access.AggregateAccess(allowed=False, plants=[])

    monkeypatch.setattr(signals_router, "aggregate_access", fake)
    sites = await signals_router._visible_sites_by_rule(None, SimpleNamespace(id="u"))
    assert sites["XSTAT-001"] == ["p1"]                 # INCIDENT only
    assert sites["XCORR-006"] == ["p1", "p2"]           # NEAR_MISS + INCIDENT, union
    assert sites["XSTAT-004"] is None                   # CAPA at all plants
    assert sites["XCORR-007"] == []                     # MOC + TRAINING: no grant
    assert sites["XDQ-021"] == []                       # PLATFORM-only: admin only


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()
