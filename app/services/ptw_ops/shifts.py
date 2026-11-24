"""Shift windows in plant-local time (SHIFT masters, A/B/C fallback)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.ptw_cc import DEFAULT_SHIFTS, LOCAL_TZ


def _window_for(day_local: datetime, shift: dict[str, Any]) -> tuple[datetime, datetime]:
    sh, eh = int(shift["startHour"]), int(shift["endHour"]) % 24
    start = day_local.replace(hour=sh, minute=0, second=0, microsecond=0)
    end = day_local.replace(hour=eh, minute=0, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def current_shift(now: datetime, shifts: list[dict[str, Any]] | None = None) -> tuple[str, datetime, datetime]:
    """(code, start_utc, end_utc) of the shift containing `now`."""
    shifts = shifts or DEFAULT_SHIFTS
    local = now.astimezone(LOCAL_TZ)
    for day in (local, local - timedelta(days=1)):
        for s in shifts:
            start, end = _window_for(day, s)
            if start <= local < end:
                return s["code"], start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    s = shifts[0]
    start, end = _window_for(local, s)
    return s["code"], start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def shift_window(now: datetime, code: str, shifts: list[dict[str, Any]] | None = None) -> tuple[datetime, datetime]:
    """Most recent window of shift `code` that has started (today's, or yesterday's
    when today's has not begun yet)."""
    shifts = shifts or DEFAULT_SHIFTS
    s = next((x for x in shifts if x["code"] == code), None)
    if s is None:
        _, a, b = current_shift(now, shifts)
        return a, b
    local = now.astimezone(LOCAL_TZ)
    start, end = _window_for(local, s)
    if start > local:
        start, end = _window_for(local - timedelta(days=1), s)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def local_day(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(LOCAL_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def range_window(now: datetime, rng: str, shift_code: str | None, shifts: list[dict[str, Any]]) -> tuple[datetime, datetime]:
    """The filter window: 'shift' (selected or current shift), 'today', '7d'."""
    if rng == "shift":
        if shift_code:
            return shift_window(now, shift_code, shifts)
        _, a, b = current_shift(now, shifts)
        return a, b
    if rng == "7d":
        day_start, day_end = local_day(now)
        return day_start - timedelta(days=6), day_end
    return local_day(now)
