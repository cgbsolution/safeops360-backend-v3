"""IMS / MIS registers: offline unit tests (no DB), house style of test_capture.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import ims as svc

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


# ── periods ──────────────────────────────────────────────────────────────────
def test_fytd_starts_on_first_april_of_current_fy():
    start, end = svc.period_bounds("FYTD", NOW)
    assert (start.year, start.month, start.day) == (2026, 4, 1)
    assert end == NOW


def test_fytd_in_q4_uses_previous_calendar_year():
    start, _ = svc.period_bounds("FYTD", datetime(2027, 2, 10, tzinfo=timezone.utc))
    assert (start.year, start.month, start.day) == (2026, 4, 1)


def test_mtd_and_rolling_windows():
    start, _ = svc.period_bounds("mtd", NOW)
    assert (start.month, start.day, start.hour) == (11, 1, 0)
    start, _ = svc.period_bounds("L30", NOW)
    assert NOW - start == timedelta(days=30)


def test_unknown_period_rejected():
    with pytest.raises(ValueError):
        svc.period_bounds("LAST_WEEK", NOW)


def test_months_in_window_crosses_year():
    months = svc.months_in_window(datetime(2026, 11, 15, tzinfo=timezone.utc), datetime(2027, 2, 1, tzinfo=timezone.utc))
    assert months == [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]


# ── state rules ──────────────────────────────────────────────────────────────
def test_meeting_moves():
    assert svc.can_move_meeting("SCHEDULED", "HELD")
    assert svc.can_move_meeting("SCHEDULED", "CANCELLED")
    assert svc.can_move_meeting("HELD", "MINUTES_PUBLISHED")
    assert not svc.can_move_meeting("SCHEDULED", "MINUTES_PUBLISHED")  # cannot skip holding it
    assert not svc.can_move_meeting("HELD", "CANCELLED")
    assert not svc.can_move_meeting("MINUTES_PUBLISHED", "HELD")


def test_action_moves_allow_reopen_but_not_noop():
    assert svc.can_move_action("OPEN", "CLOSED")
    assert svc.can_move_action("CLOSED", "OPEN")
    assert not svc.can_move_action("CLOSED", "DROPPED")
    assert not svc.can_move_action("OPEN", "OPEN")


def test_notice_overdue_only_while_awaiting_reply():
    past = NOW - timedelta(days=1)
    assert svc.notice_reply_overdue("ISSUED", past, NOW)
    assert not svc.notice_reply_overdue("REPLY_RECEIVED", past, NOW)
    assert not svc.notice_reply_overdue("ISSUED", NOW + timedelta(days=1), NOW)
    # naive datetimes from the DB are treated as UTC, not a crash
    assert svc.notice_reply_overdue("ISSUED", past.replace(tzinfo=None), NOW)


def test_action_overdue():
    past = NOW - timedelta(hours=1)
    assert svc.action_overdue("OPEN", past, NOW)
    assert svc.action_overdue("IN_PROGRESS", past, NOW)
    assert not svc.action_overdue("CLOSED", past, NOW)


def test_notice_close_needs_decision_and_penalty_amount():
    assert svc.notice_close_blockers(status="REPLY_RECEIVED", decision=None, penalty_amount=None)
    assert svc.notice_close_blockers(status="REPLY_RECEIVED", decision="PENALTY", penalty_amount=0)
    assert svc.notice_close_blockers(status="CLOSED", decision="WARNING", penalty_amount=None)
    assert svc.notice_close_blockers(status="REPLY_RECEIVED", decision="BOGUS", penalty_amount=None)
    assert svc.notice_close_blockers(status="ISSUED", decision="WARNING", penalty_amount=None) == []
    assert svc.notice_close_blockers(status="REPLY_RECEIVED", decision="PENALTY", penalty_amount=5000) == []


def test_minutes_publish_needs_held_meeting_and_text():
    assert svc.minutes_publish_blockers(status="SCHEDULED", minutes="x", action_count=0)
    assert svc.minutes_publish_blockers(status="HELD", minutes="  ", action_count=2)
    assert svc.minutes_publish_blockers(status="HELD", minutes="Discussed hot work.", action_count=0) == []


# ── rates ────────────────────────────────────────────────────────────────────
def test_rate_block_per_million_hours():
    b = svc.rate_block(hours=2_000_000, lti=2, fatalities=0, lost_days=60, mtc=1, rwc=1, fac=5)
    assert b["frequencyRate"] == 1.0
    assert b["severityRate"] == 30.0
    assert b["recordableCount"] == 4


def test_rate_block_unmeasured_is_none_not_zero():
    b = svc.rate_block(hours=0, lti=0, fatalities=0, lost_days=0, mtc=0, rwc=0, fac=0)
    assert b["frequencyRate"] is None
    assert b["severityRate"] is None
    assert b["fsi"] is None


def test_fatality_charged_in_severity_rate():
    b = svc.rate_block(hours=1_000_000, lti=0, fatalities=1, lost_days=0, mtc=0, rwc=0, fac=0)
    assert b["frequencyRate"] == 1.0  # a fatality is a lost-time injury
    assert b["severityRate"] == 6000.0  # IS 3786 charge


def test_vocabularies_have_no_duplicates():
    for vocab in (svc.RECIPIENT_TYPES, svc.VIOLATION_CATEGORIES, svc.NOTICE_DECISIONS, svc.MEETING_TYPES,
                  svc.ACTION_STATUSES, svc.DRILL_TYPES):
        assert len(vocab) == len(set(vocab))
