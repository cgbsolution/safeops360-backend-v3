"""One place that decides whether a user may act on a permit.

Four routers (`ptw`, `ptw_active`, `ptw_annexures`, `ptw_lifecycle`) each carried
a byte-identical `_load_permit_or_403`, so a rule fixed in one drifted from the
other three. They all delegate here now.

The rule this module adds on top of `can()` is the **named-party read**: whoever
the permit itself names — originator, issuer, receiver, or a crew member — can
always READ it, whatever their RBAC scope says.

Why that has to be a rule and not a grant: a permit's parties are chosen
per-permit, at issue time, and routinely cross department lines (maintenance
work in the production hall, a contractor crew under a plant supervisor). A
Supervisor holds `PTW.READ` at OWN_DEPARTMENT, so being named Receiver on a
permit raised by another department left them staring at
"Permission 'PTW.READ' present but scope does not include this record" on the
very permit they were supposed to acknowledge. The workflow had already handed
them the task; being denied sight of the record is never the right answer.

This mirrors the `_is_workflow_actor` fallback the Near Miss router already uses
for exactly the same reason.

Scope of the exemption, deliberately narrow:
  * READ-ish operations only. Write/approve operations keep running through
    `can()` alone — being named on a permit must not confer PTW.APPROVE.
  * The parties are read off this permit row, so it grants sight of exactly one
    record, never a list.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import Permit, PermitCrewMember
from app.models.user import User
from app.services.permissions import PermissionContext, can

# Operations a named party is entitled to on their own permit. Read-only by
# design — see the module docstring.
_PARTY_READ_OPS = frozenset({"PTW.READ", "PTW.EXPORT"})


def permit_party_ids(permit: Permit) -> dict[str, str | None]:
    """The owner-style fields `can()` matches for OWN_RECORDS scope."""
    return {
        "originatorId": permit.originatorId,
        "issuerId": permit.issuerId,
        "receiverId": permit.receiverId,
    }


async def is_permit_party(db: AsyncSession, permit: Permit, user_id: str) -> bool:
    """True when the permit itself names this user, crew membership included."""
    if user_id in {permit.originatorId, permit.issuerId, permit.receiverId}:
        return True
    crew = (
        await db.execute(
            select(PermitCrewMember.userId).where(PermitCrewMember.permitId == permit.id)
        )
    ).scalars().all()
    return user_id in set(crew)


async def check_permit_access(
    db: AsyncSession, permit: Permit, user: User, op: str
) -> tuple[bool, str | None]:
    """`(allowed, reason)` for one permit and one operation."""
    result = await can(
        db,
        user.id,
        op,
        PermissionContext(
            record_id=permit.id,
            plant_id=permit.plantId,
            record=permit_party_ids(permit),
        ),
    )
    if result.allowed:
        return True, None
    if op in _PARTY_READ_OPS and await is_permit_party(db, permit, user.id):
        return True, None
    return False, result.reason


async def load_permit_or_403(
    db: AsyncSession, permit_id: str, user: User, op: str
) -> Permit:
    """Fetch a permit and assert the user may perform `op` on it."""
    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    allowed, reason = await check_permit_access(db, permit, user, op)
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, reason or "Access denied")
    return permit


__all__ = [
    "check_permit_access",
    "is_permit_party",
    "load_permit_or_403",
    "permit_party_ids",
]
