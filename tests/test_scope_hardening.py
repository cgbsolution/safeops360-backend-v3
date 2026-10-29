"""Read-scope hardening — offline (no DB).

Pins the fixes for the copilot-readiness scope findings:
  * can(): OWN_DEPARTMENT compared a record's Department.id with the user's
    department NAME, so it never matched a real record (a department head was
    refused their own department's permits); and OWN_DEPARTMENT / OWN_RECORDS
    grants ignored the plant on plant-only checks (EAI register fail-open,
    creates at any plant).
  * RecordScope: registers mirror can() — plant, department and own records —
    instead of the any-module plant set that showed every plant.
  * services/numbering: highest issued + 1, never count + 1.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.dialects import postgresql

from app.services import permissions as perms
from app.services.access_scope import RecordScope
from app.services.numbering import next_number
from app.services.permissions import PermissionContext, can


def _snapshot(scopes: dict[str, list[str]], *, plants=("p1", "p2"), department="IT"):
    rows = [
        perms._CachedRow(permission_code=code, scope=s, conditions=None, role_id="r")
        for code, ss in scopes.items()
        for s in ss
    ]
    return perms._UserSnapshot(
        rows=rows, role_codes=["R"], plant_id=plants[0] if plants else None,
        plant_ids=set(plants), department=department, expires_at=time.time() + 300,
    )


@pytest.fixture
def user(monkeypatch):
    """Install a cached permission snapshot + department master for user 'u'."""
    def _install(scopes, **kw):
        perms._CACHE["u"] = _snapshot(scopes, **kw)
        return "u"

    monkeypatch.setattr(perms, "_DEPT_CACHE", {
        "dept-it-p1": ("IT", "p1"),
        "dept-maint-p1": ("Maintenance", "p1"),
        "dept-it-p9": ("IT", "p9"),
    })
    yield _install
    perms._CACHE.pop("u", None)


# ─── can(): OWN_DEPARTMENT ──────────────────────────────────────────────────

async def test_own_department_reads_own_department_record(user):
    uid = user({"PTW.READ": ["OWN_DEPARTMENT"]})
    ctx = PermissionContext(record_id="x", plant_id="p1", department_id="dept-it-p1", record={})
    r = await can(None, uid, "PTW.READ", ctx)
    assert r.allowed and r.matched_scope == "OWN_DEPARTMENT"


async def test_own_department_refuses_other_department(user):
    uid = user({"PTW.READ": ["OWN_DEPARTMENT"]})
    ctx = PermissionContext(record_id="x", plant_id="p1", department_id="dept-maint-p1", record={})
    assert not (await can(None, uid, "PTW.READ", ctx)).allowed


async def test_own_department_refuses_same_name_at_another_plant(user):
    uid = user({"PTW.READ": ["OWN_DEPARTMENT"]})
    ctx = PermissionContext(record_id="x", plant_id="p9", department_id="dept-it-p9", record={})
    assert not (await can(None, uid, "PTW.READ", ctx)).allowed


async def test_own_department_reads_department_from_record_dict(user):
    uid = user({"INCIDENT.READ": ["OWN_DEPARTMENT"]})
    ctx = PermissionContext(record_id="x", plant_id="p1", record={"reporterId": "other", "departmentId": "dept-it-p1"})
    assert (await can(None, uid, "INCIDENT.READ", ctx)).allowed


async def test_own_department_matches_department_name(user):
    uid = user({"OBSERVATION.READ": ["OWN_DEPARTMENT"]})
    ctx = PermissionContext(record_id="x", plant_id="p1", record={"observerId": "o", "departmentName": "it"})
    assert (await can(None, uid, "OBSERVATION.READ", ctx)).allowed


async def test_own_department_includes_own_records(user):
    uid = user({"INCIDENT.READ": ["OWN_DEPARTMENT"]})
    ctx = PermissionContext(record_id="x", plant_id="p9", record={"reporterId": "u", "departmentId": "dept-maint-p1"})
    assert (await can(None, uid, "INCIDENT.READ", ctx)).allowed


async def test_own_department_without_department_info_is_denied(user):
    uid = user({"HIRA.READ": ["OWN_DEPARTMENT"]})
    ctx = PermissionContext(record_id="x", plant_id="p1", record={"createdById": "other"})
    assert not (await can(None, uid, "HIRA.READ", ctx)).allowed


async def test_user_without_department_never_matches_department(user):
    uid = user({"PTW.READ": ["OWN_DEPARTMENT"]}, department=None)
    ctx = PermissionContext(record_id="x", plant_id="p1", department_id="dept-it-p1", record={})
    assert not (await can(None, uid, "PTW.READ", ctx)).allowed


# ─── can(): plant on plant-only checks ─────────────────────────────────────

@pytest.mark.parametrize("scope", ["OWN_DEPARTMENT", "OWN_RECORDS"])
async def test_narrow_scopes_respect_plant_on_plant_only_checks(user, scope):
    uid = user({"EAI.READ": [scope]})
    assert (await can(None, uid, "EAI.READ", PermissionContext(plant_id="p1"))).allowed
    assert not (await can(None, uid, "EAI.READ", PermissionContext(plant_id="p9"))).allowed
    # no plant named → unchanged (module-level gate)
    assert (await can(None, uid, "EAI.READ", PermissionContext())).allowed


async def test_own_records_still_reads_own_record_at_any_plant(user):
    uid = user({"PTW.READ": ["OWN_RECORDS"]})
    ctx = PermissionContext(record_id="x", plant_id="p9", record={"receiverId": "u"})
    assert (await can(None, uid, "PTW.READ", ctx)).allowed


async def test_own_plant_unchanged(user):
    uid = user({"PTW.READ": ["OWN_PLANT"]})
    assert (await can(None, uid, "PTW.READ", PermissionContext(record_id="x", plant_id="p2"))).allowed
    assert not (await can(None, uid, "PTW.READ", PermissionContext(record_id="x", plant_id="p9"))).allowed


# ─── RecordScope filter ─────────────────────────────────────────────────────

_t = Table("Rec", MetaData(), Column("plantId", String), Column("departmentId", String), Column("ownerId", String))


def _sql(clause) -> str:
    return str(select(_t.c.plantId).where(clause).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _scope(scopes, plants=("p1", "p2"), department="IT"):
    return RecordScope(user_id="u", scopes=set(scopes), plant_ids=list(plants), department=department)


def test_all_plants_is_unrestricted():
    assert _scope({"ALL_PLANTS", "OWN_RECORDS"}).filter(_t.c.plantId, owner=_t.c.ownerId == "u") is None


def test_own_plant_filters_plants_only():
    sql = _sql(_scope({"OWN_PLANT"}).filter(_t.c.plantId, owner=_t.c.ownerId == "u", department_col=_t.c.departmentId))
    assert "IN ('p1', 'p2')" in sql and "ownerId" not in sql and "Department" not in sql


def test_own_plant_with_party_rule_includes_owner():
    sql = _sql(_scope({"OWN_PLANT"}).filter(_t.c.plantId, owner=_t.c.ownerId == "u", owner_always=True))
    assert "IN ('p1', 'p2')" in sql and "\"ownerId\" = 'u'" in sql


def test_own_department_filters_department_and_owner():
    sql = _sql(_scope({"OWN_DEPARTMENT"}).filter(_t.c.plantId, owner=_t.c.ownerId == "u", department_col=_t.c.departmentId))
    assert "\"Department\"" in sql and "lower(\"Department\".name) = 'it'" in sql
    assert "\"ownerId\" = 'u'" in sql and "IN ('p1', 'p2')" in sql


def test_own_department_without_department_falls_back_to_owner():
    sql = _sql(_scope({"OWN_DEPARTMENT"}, department=None).filter(
        _t.c.plantId, owner=_t.c.ownerId == "u", department_col=_t.c.departmentId))
    assert "Department" not in sql and "\"ownerId\" = 'u'" in sql


def test_own_records_is_owner_only():
    sql = _sql(_scope({"OWN_RECORDS"}).filter(_t.c.plantId, owner=_t.c.ownerId == "u", department_col=_t.c.departmentId))
    assert "\"ownerId\" = 'u'" in sql and "plantId\" IN" not in sql


def test_no_grant_matches_nothing():
    sql = _sql(_scope(set()).filter(_t.c.plantId, owner=_t.c.ownerId == "u"))
    assert "false" in sql.lower()


def test_allows_plant():
    assert _scope({"ALL_PLANTS"}).allows_plant("p9")
    assert _scope({"OWN_DEPARTMENT"}).allows_plant("p1")
    assert not _scope({"OWN_PLANT"}).allows_plant("p9")
    assert not _scope(set()).allows_plant("p1")


# ─── numbering ──────────────────────────────────────────────────────────────

class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _NumDb:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return _Rows(self.rows)


async def test_next_number_continues_from_highest_not_count():
    # 2 live rows but #0007 was issued (others deleted) → count+1 would re-issue 0003
    db = _NumDb(["INC-2026-NW-0002", "INC-2026-NW-0007"])
    assert await next_number(db, column=_t.c.ownerId, stem="INC-2026-NW-") == "INC-2026-NW-0008"


async def test_next_number_ignores_differently_shaped_numbers():
    db = _NumDb(["INC-2026-NW-HIST-02", "INC-2026-NW-0045", None])
    assert await next_number(db, column=_t.c.ownerId, stem="INC-2026-NW-") == "INC-2026-NW-0046"


async def test_next_number_first_and_width():
    assert await next_number(_NumDb([]), column=_t.c.ownerId, stem="OBS-NW-", width=5) == "OBS-NW-00001"
