"""Shift windows in plant-local time, from the SHIFT masters (A/B/C fallback).

The same master list feeds the permit form's Shift dropdown, the permit's
stored shiftId, and the Control Center's shift filter, so they always agree.

Real master lists are not a tidy 3 × 8 h rotation: a shift can be inactive
(leaving a gap, e.g. 06:00–09:00 when only B 14–22, C 22–06 and G 09–17 are
active) and shifts can overlap (G 09–17 overlaps B 14–22). Rules:
  • overlap → the shift that started most recently wins (at 15:00, B over G)
  • gap     → no shift contains the time; `current_shift` returns the most
              recent shift that has started (the one just ended) and
              `shift_at` returns None, so nothing is mis-assigned.
"""

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


def _candidates(at: datetime, shifts: list[dict[str, Any]]):
    """Every (shift, start, end) window that has started at or before `at`
    (today's and yesterday's occurrences), newest start first."""
    local = at.astimezone(LOCAL_TZ)
    out = []
    for day in (local, local - timedelta(days=1)):
        for s in shifts:
            start, end = _window_for(day, s)
            if start <= local:
                out.append((s, start, end))
    out.sort(key=lambda x: x[1], reverse=True)
    return out, local


def shift_at(at: datetime, shifts: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The shift containing `at` (most recently started wins on overlap), or
    None when `at` falls in a gap between shifts."""
    shifts = shifts or DEFAULT_SHIFTS
    cands, local = _candidates(at, shifts)
    for s, start, end in cands:
        if start <= local < end:
            return s
    return None


def current_shift(now: datetime, shifts: list[dict[str, Any]] | None = None) -> tuple[str, datetime, datetime]:
    """(code, start_utc, end_utc) of the shift containing `now`; in a gap, the
    most recent shift that has started (never a future window)."""
    shifts = shifts or DEFAULT_SHIFTS
    cands, local = _candidates(now, shifts)
    containing = [c for c in cands if c[1] <= local < c[2]]
    s, start, end = (containing or cands)[0]
    return s["code"], start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def shift_window(now: datetime, code: str, shifts: list[dict[str, Any]] | None = None) -> tuple[datetime, datetime]:
    """Most recent window of shift `code` that has started (today's, or
    yesterday's when today's has not begun yet)."""
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
