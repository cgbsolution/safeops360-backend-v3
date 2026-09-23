"""Tests for the Kaizen gap-closure build.

Offline, in the house style of test_business_excellence.py: the gates, the
derivations and the aggregates need no database.

The cases below are chosen for the specific mistakes THIS module has already
made in production, not for coverage:

  * a "Fast track" badge displayed on a record with a null investment AND a null
    saving, because the badge was read off the approval lane;
  * an APPROVED record with no owner — all four live records were in that state;
  * an aggregate reporting a confident number computed from one or two rows;
  * a participation rate of 0% that really meant "we have no headcount";
  * a timestamp re-stamped when a parked record walks the chain a second time,
    which would make the cycle-time median measure the second pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.business_excellence import (
    FAST_TRACK_MAX_IMPLEMENTATION_DAYS,
    FAST_TRACK_MAX_INVESTMENT,
)
from app.services import business_excellence as be


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _kaizen(**kw) -> SimpleNamespace:
    """A Kaizen that would earn the badge, so each test can break exactly one
    thing and the failure names the rule it broke."""
    created = kw.pop("createdAt", _now())
    base = dict(
        id="k1",
        status="SUBMITTED",
        lane="STANDARD",
        ownerId="u-owner",
        createdAt=created,
        createdById="u-author",
        targetDate=created + timedelta(days=FAST_TRACK_MAX_IMPLEMENTATION_DAYS),
        investmentCost=FAST_TRACK_MAX_INVESTMENT - 1,
        estimatedAnnualSaving=50_000.0,
        verifiedAnnualSaving=None,
        implementedAt=None,
        verifiedAt=None,
        screenedAt=None,
        approvedAt=None,
        closedAt=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ─────────────────────────────────────────────────────────────────────────────
# §6 — approval requires an owner
# ─────────────────────────────────────────────────────────────────────────────
def test_approval_is_blocked_without_an_owner():
    """The defect: four of four live records sat at or past APPROVED with a null
    owner, because nothing anywhere checked."""
    assert be.kaizen_approval_blockers(_kaizen(ownerId=None))


def test_approval_is_open_once_an_owner_is_assigned():
    assert be.kaizen_approval_blockers(_kaizen(ownerId="u-owner")) == []


def test_an_empty_string_owner_is_not_an_owner():
    """`ownerId=""` passes a `is not None` check and fails a truthiness one. The
    gate must use the second — an empty string assigns work to nobody."""
    assert be.kaizen_approval_blockers(_kaizen(ownerId=""))


def test_blockers_are_readable_sentences_not_codes():
    """The list is rendered verbatim in a tooltip on the disabled button, so a
    code like OWNER_REQUIRED would reach a user."""
    (message,) = be.kaizen_approval_blockers(_kaizen(ownerId=None))
    assert message.endswith(".") and " " in message


# ─────────────────────────────────────────────────────────────────────────────
# §5 — the fast-track badge is earned, not chosen
# ─────────────────────────────────────────────────────────────────────────────
def test_a_cheap_quick_idea_earns_the_badge():
    assert be.fast_track_eligibility(_kaizen())["eligible"] is True


def test_a_missing_investment_is_not_a_zero_investment():
    """THE production defect. KZN-2026-0022 carried the badge with a null
    investment and a null saving. Treating absent as zero makes the cheapest
    possible claim out of no data at all."""
    result = be.fast_track_eligibility(_kaizen(investmentCost=None))
    assert result["eligible"] is False
    assert any("investment" in r.lower() for r in result["reasons"])


def test_a_missing_target_date_is_not_a_zero_day_window():
    result = be.fast_track_eligibility(_kaizen(targetDate=None))
    assert result["eligible"] is False
    assert any("target date" in r.lower() for r in result["reasons"])


def test_investment_above_the_threshold_loses_the_badge():
    assert (
        be.fast_track_eligibility(
            _kaizen(investmentCost=FAST_TRACK_MAX_INVESTMENT + 1)
        )["eligible"]
        is False
    )


def test_investment_exactly_on_the_threshold_keeps_the_badge():
    """The rule is "at or under". An off-by-one here silently disqualifies every
    record priced exactly at the policy limit, which is where people price."""
    assert (
        be.fast_track_eligibility(
            _kaizen(investmentCost=FAST_TRACK_MAX_INVESTMENT)
        )["eligible"]
        is True
    )


def test_a_long_implementation_window_loses_the_badge():
    now = _now()
    k = _kaizen(createdAt=now, targetDate=now + timedelta(days=30))
    assert be.fast_track_eligibility(k)["eligible"] is False


def test_the_badge_does_not_depend_on_the_approval_lane():
    """`lane` routes the workflow; the badge describes the idea. Coupling them
    is the whole defect — a record on the fast lane with no figures must NOT
    show the badge, and one on the standard lane with good figures must."""
    assert be.fast_track_eligibility(_kaizen(lane="FAST_TRACK", investmentCost=None))[
        "eligible"
    ] is False
    assert be.fast_track_eligibility(_kaizen(lane="STANDARD"))["eligible"] is True


def test_eligibility_explains_itself():
    """A badge that quietly does not appear is indistinguishable from a bug, so
    the reasons are part of the contract, not a debugging aid."""
    result = be.fast_track_eligibility(_kaizen(investmentCost=None, targetDate=None))
    assert len(result["reasons"]) == 2
    assert result["maxInvestment"] == FAST_TRACK_MAX_INVESTMENT
    assert result["maxImplementationDays"] == FAST_TRACK_MAX_IMPLEMENTATION_DAYS


def test_eligibility_tolerates_a_naive_datetime_from_the_db():
    """Rows written before the column was timezone-aware come back naive, and a
    naive/aware comparison raises TypeError mid-request."""
    naive = datetime(2026, 1, 1)
    k = _kaizen(createdAt=naive, targetDate=naive + timedelta(days=1))
    assert be.fast_track_eligibility(k)["eligible"] is True


# ─────────────────────────────────────────────────────────────────────────────
# §2 — lifecycle timestamps
# ─────────────────────────────────────────────────────────────────────────────
def test_each_transition_stamps_its_own_clock():
    now = _now()
    k = _kaizen()
    for target, field in be.KAIZEN_TIMESTAMP_FIELDS.items():
        be.stamp_kaizen_transition(k, target, at=now)
        assert getattr(k, field) == now, f"{target} did not stamp {field}"


def test_re_entering_a_state_keeps_the_original_stamp():
    """A PARKED record re-enters SUBMITTED and walks the chain again. Re-stamping
    would make the median measure the second pass and report the programme as
    faster than it is."""
    first, later = _now(), _now() + timedelta(days=40)
    k = _kaizen()
    be.stamp_kaizen_transition(k, "SCREENED", at=first)
    be.stamp_kaizen_transition(k, "SCREENED", at=later)
    assert k.screenedAt == first


def test_a_transition_with_no_clock_is_a_no_op():
    """REJECTED and PARKED have no timestamp field; the helper must not raise
    when the state machine passes one through."""
    k = _kaizen()
    be.stamp_kaizen_transition(k, "REJECTED", at=_now())
    be.stamp_kaizen_transition(k, "PARKED", at=_now())


def test_every_timestamp_field_exists_on_the_model():
    """A typo in the table would silently setattr a new attribute on the
    instance and never reach the database."""
    from app.models.business_excellence import BeKaizen

    for field in be.KAIZEN_TIMESTAMP_FIELDS.values():
        assert hasattr(BeKaizen, field), f"BeKaizen has no column {field}"


def test_timestamp_targets_are_all_real_statuses():
    from app.models.business_excellence import KAIZEN_STATUSES

    unknown = [t for t in be.KAIZEN_TIMESTAMP_FIELDS if t not in KAIZEN_STATUSES]
    assert unknown == []


# ─────────────────────────────────────────────────────────────────────────────
# §2 — cycle time
# ─────────────────────────────────────────────────────────────────────────────
def _finished(days_to_implement: int, days_to_verify: int | None = None):
    created = _now() - timedelta(days=200)
    return _kaizen(
        createdAt=created,
        targetDate=created + timedelta(days=1),
        implementedAt=created + timedelta(days=days_to_implement),
        verifiedAt=(
            created + timedelta(days=days_to_verify) if days_to_verify is not None else None
        ),
    )


def test_median_is_none_below_the_sample_floor():
    """Four records is not a median. A number computed from three rows looks
    exactly like one computed from three hundred, and the screen has no way to
    tell the reader which it is holding."""
    rows = [_finished(10) for _ in range(be.CYCLE_TIME_MIN_SAMPLE - 1)]
    assert be.kaizen_cycle_times(rows)["medianDaysRaisedToImplemented"] is None


def test_median_computes_at_the_sample_floor():
    rows = [_finished(10) for _ in range(be.CYCLE_TIME_MIN_SAMPLE)]
    assert be.kaizen_cycle_times(rows)["medianDaysRaisedToImplemented"] == 10.0


def test_unfinished_records_do_not_count_as_zero_days():
    """Counting an in-flight record as 0 days is how a programme that finishes
    nothing reports the best cycle time in the company."""
    rows = [_finished(10) for _ in range(be.CYCLE_TIME_MIN_SAMPLE)]
    rows += [_kaizen(implementedAt=None) for _ in range(50)]
    result = be.kaizen_cycle_times(rows)
    assert result["implementedSampleSize"] == be.CYCLE_TIME_MIN_SAMPLE
    assert result["medianDaysRaisedToImplemented"] == 10.0


def test_median_is_not_the_mean():
    """One idea that sat for a year must not drag the headline number. 1,2,3,4,
    500 has a mean of 102 and a median of 3."""
    rows = [_finished(d) for d in (1, 2, 3, 4, 500)]
    assert be.kaizen_cycle_times(rows)["medianDaysRaisedToImplemented"] == 3.0


def test_even_sample_averages_the_middle_pair():
    rows = [_finished(d) for d in (1, 2, 3, 4, 10, 20)]
    assert be.kaizen_cycle_times(rows)["medianDaysRaisedToImplemented"] == 3.5


def test_implemented_and_verified_are_measured_independently():
    """A record can be implemented without anyone verifying the saving. The two
    medians must not require the same sample."""
    rows = [_finished(10, days_to_verify=None) for _ in range(be.CYCLE_TIME_MIN_SAMPLE)]
    result = be.kaizen_cycle_times(rows)
    assert result["medianDaysRaisedToImplemented"] == 10.0
    assert result["medianDaysRaisedToVerified"] is None
    assert result["verifiedSampleSize"] == 0


def test_cycle_times_over_an_empty_set_is_null_not_zero():
    result = be.kaizen_cycle_times([])
    assert result["medianDaysRaisedToImplemented"] is None
    assert result["medianDaysRaisedToVerified"] is None


# ─────────────────────────────────────────────────────────────────────────────
# §3 — participation
# ─────────────────────────────────────────────────────────────────────────────
def test_participation_counts_people_not_ideas():
    """The failure mode a Kaizen programme actually has is "the same three
    people every month", and an idea count hides it completely."""
    rows = [_kaizen(createdById="u1") for _ in range(9)] + [_kaizen(createdById="u2")]
    result = be.summarise_participation(rows, headcount=100)
    assert result["submitters"] == 2
    assert result["ideas"] == 10
    assert result["participationRate"] == 2.0


def test_participation_rate_is_null_without_a_headcount():
    """0% and "we do not know" look identical on a tile and mean opposite
    things. 16 of 28 plants have no factory profile, so this is the normal
    case."""
    rows = [_kaizen(createdById="u1")]
    assert be.summarise_participation(rows, headcount=None)["participationRate"] is None


def test_a_zero_headcount_is_treated_as_no_headcount():
    """A 0 denominator either divides by zero or reports an infinite rate.
    Neither is a number to put on a screen."""
    rows = [_kaizen(createdById="u1")]
    assert be.summarise_participation(rows, headcount=0)["participationRate"] is None


def test_participation_is_capped_at_one_hundred_percent():
    """Contractors raise ideas and are not in totalEmployees, so an active plant
    can genuinely exceed its own denominator. 118% just reads as broken."""
    rows = [_kaizen(createdById=f"u{i}") for i in range(150)]
    assert be.summarise_participation(rows, headcount=100)["participationRate"] == 100.0


def test_ideas_per_submitter_divides_by_submitters_not_headcount():
    rows = [_kaizen(createdById="u1") for _ in range(6)]
    assert be.summarise_participation(rows, headcount=1000)["ideasPerSubmitter"] == 6.0


def test_ideas_per_submitter_is_null_with_no_submitters():
    assert be.summarise_participation([], headcount=100)["ideasPerSubmitter"] is None


# ─────────────────────────────────────────────────────────────────────────────
# The implementation window
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("days", [0, 1, 7, 90])
def test_implementation_window_is_the_gap_from_raise_to_target(days: int):
    created = _now()
    k = _kaizen(createdAt=created, targetDate=created + timedelta(days=days))
    assert be.kaizen_implementation_window_days(k) == days


def test_implementation_window_is_none_without_a_target():
    assert be.kaizen_implementation_window_days(_kaizen(targetDate=None)) is None


def test_a_target_in_the_past_does_not_produce_a_negative_window():
    """A back-dated record is a data-entry accident, not a negative-duration
    project. Clamped to 0 so nothing downstream has to reason about a negative
    duration — note this DOES leave such a record fast-track eligible on the
    time axis, which is deliberate: a target already in the past is due now."""
    created = _now()
    k = _kaizen(createdAt=created, targetDate=created - timedelta(days=5))
    assert be.kaizen_implementation_window_days(k) == 0
