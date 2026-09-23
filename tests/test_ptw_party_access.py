"""PTW named-party read rule — bug PTW-3.

The defect: a Supervisor named as *Receiver* on a permit raised by another
department saw "Permission 'PTW.READ' present but scope does not include this
record" on the very permit they had been handed a task for. SUPERVISOR holds
PTW.READ at OWN_DEPARTMENT, and being a named party is not a department.

`app.services.ptw_access` adds the rule: whoever the permit itself names —
originator, issuer, receiver, or a crew member — can always READ it. These
tests pin both halves of it: that the exemption applies to reads, and that it
does NOT leak into write/approve operations.

Run:  .venv/Scripts/python.exe -m pytest tests/test_ptw_party_access.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import ptw_access
from app.services.permissions import CanResult

PERMIT = SimpleNamespace(
    id="permit1",
    plantId="plant1",
    originatorId="user-originator",
    issuerId="user-issuer",
    receiverId="user-receiver",
)

DENIED = CanResult(
    allowed=False,
    reason="Permission 'PTW.READ' present but scope does not include this record",
)
ALLOWED = CanResult(allowed=True, matched_scope="OWN_PLANT")


class _FakeDb:
    """Stands in for the AsyncSession — only the crew lookup touches it."""

    def __init__(self, crew: list[str] | None = None):
        self.crew = crew or []

    async def execute(self, _stmt):
        rows = self.crew

        class _Result:
            def scalars(self_inner):
                return SimpleNamespace(all=lambda: rows)

        return _Result()


def _user(uid: str):
    return SimpleNamespace(id=uid)


@pytest.fixture
def deny_everything(monkeypatch):
    """RBAC says no to everything, so only the party rule can allow access."""

    async def _can(_db, _uid, _code, _ctx=None):
        return DENIED

    monkeypatch.setattr(ptw_access, "can", _can)


def test_party_ids_are_the_owner_fields_can_matches():
    assert ptw_access.permit_party_ids(PERMIT) == {
        "originatorId": "user-originator",
        "issuerId": "user-issuer",
        "receiverId": "user-receiver",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", ["user-originator", "user-issuer", "user-receiver"])
async def test_named_parties_can_always_read_their_permit(deny_everything, uid):
    allowed, reason = await ptw_access.check_permit_access(
        _FakeDb(), PERMIT, _user(uid), "PTW.READ"
    )
    assert allowed is True, reason


@pytest.mark.asyncio
async def test_a_crew_member_can_read_the_permit_they_are_on(deny_everything):
    db = _FakeDb(crew=["user-crew"])
    allowed, _ = await ptw_access.check_permit_access(db, PERMIT, _user("user-crew"), "PTW.READ")
    assert allowed is True


@pytest.mark.asyncio
async def test_a_stranger_is_still_refused(deny_everything):
    allowed, reason = await ptw_access.check_permit_access(
        _FakeDb(), PERMIT, _user("user-nobody"), "PTW.READ"
    )
    assert allowed is False
    assert "scope does not include this record" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["PTW.UPDATE", "PTW.APPROVE", "PTW.EXECUTE", "PTW.DELETE"])
async def test_being_named_does_not_confer_write_or_approval(deny_everything, op):
    # The exemption is read-only on purpose: a receiver must not gain
    # PTW.APPROVE just by being written onto the permit.
    allowed, _ = await ptw_access.check_permit_access(
        _FakeDb(), PERMIT, _user("user-receiver"), op
    )
    assert allowed is False


@pytest.mark.asyncio
async def test_rbac_still_decides_when_it_allows(monkeypatch):
    async def _can(_db, _uid, _code, _ctx=None):
        return ALLOWED

    monkeypatch.setattr(ptw_access, "can", _can)
    allowed, reason = await ptw_access.check_permit_access(
        _FakeDb(), PERMIT, _user("user-nobody"), "PTW.UPDATE"
    )
    assert allowed is True
    assert reason is None
