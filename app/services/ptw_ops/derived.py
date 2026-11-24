"""Derived permit statuses — computed, never stored.

"Open" means the permit still authorises (or claims to authorise) work: it
has left DRAFT and has not reached a terminal status (CLOSED / CANCELLED /
REJECTED) or a closedAt stamp. An EXPIRED permit stays open until it is closed
out or withdrawn (withdraw moves it to CANCELLED) — expiry is a fact about the
clock, not a close-out.

closureType is NOT a closed signal: /complete stamps WORK_COMPLETED on the
Work Completed declaration, while the permit still waits for handback and
closure approval.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.ptw_ops.types import PermitView

TERMINAL_STATUSES = frozenset({"CLOSED", "CANCELLED", "REJECTED"})
PRE_ACTIVE_STATUSES = frozenset({"SUBMITTED", "APPROVED", "ISSUED"})
WORKING_STATUSES = frozenset({"ACTIVE", "SUSPENDED"})
RUNNING_STATUSES = frozenset({"ISSUED", "ACTIVE", "SUSPENDED"})
WINDOW = timedelta(hours=2)


def is_open(p: PermitView) -> bool:
    if p.status == "DRAFT" or p.status in TERMINAL_STATUSES:
        return False
    return p.closedAt is None


def expired_open(p: PermitView, now: datetime) -> bool:
    """Past valid_to and not closed."""
    return is_open(p) and p.validTo < now


def active_outside_validity(p: PermitView, now: datetime) -> bool:
    """Working (ACTIVE/SUSPENDED) while the clock says the window is over or not begun."""
    return p.status in WORKING_STATUSES and (now > p.validTo or now < p.validFrom)


def expiring_2h(p: PermitView, now: datetime) -> bool:
    return is_open(p) and p.status in RUNNING_STATUSES and now <= p.validTo <= now + WINDOW


def starting_2h(p: PermitView, now: datetime) -> bool:
    return p.status in PRE_ACTIVE_STATUSES and now < p.validFrom <= now + WINDOW


def derived_flags(p: PermitView, now: datetime) -> list[str]:
    out = []
    if expired_open(p, now):
        out.append("expired_open")
    if active_outside_validity(p, now):
        out.append("active_outside_validity")
    if expiring_2h(p, now):
        out.append("expiring_2h")
    if starting_2h(p, now):
        out.append("starting_2h")
    return out
