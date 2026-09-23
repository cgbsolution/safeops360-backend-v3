"""Frequency rates — the shared definition, and the two bugs it exists to stop.

House style: pure functions, no DB, no TestClient, no conftest.

The bugs, both found live on production 2026-08-19:

  * Manhours Performance showed `LTIFR 0.00` labelled "World Class" beside its own
    Heinrich pyramid showing 3 LTIs, because a zero denominator produced 0 and 0
    banded as best-in-class. `routers/dashboard.py` did the same with `else 0.0`.
  * `scorecard/payload.py` published LTIFR on the OSHA 200,000-hour base, a
    five-fold understatement against the same metric elsewhere, because the base
    was a caller-supplied constant rather than a property of the named rate.

Ground truth used below (production, rolling 12 months to 2026-09-01):
  NW (Meridian North Works) — 3 LTIs, 0 fatalities, 1,763,320 exposure hours
  NW 2026-05 — 1 LTI, 200,200 exposure hours; stored Manhours.ltifr = 0.9990
  Meridian Apparel (MAG-*) plants — incidents on record, zero Manhours rows
"""

import math

import pytest

from app.services import frequency_rates as fr

NW_HOURS_12M = 1_763_320
NW_LTI_12M = 3


# ── rule 1: a missing denominator is None, never 0 ──────────────────────────


@pytest.mark.parametrize("hours", [0, 0.0, None])
def test_every_rate_is_none_without_exposure(hours):
    """The headline bug. Not 0.0, not 0 — None, so no caller can band it."""
    assert fr.ltifr(lti=3, exposure_hours=hours) is None
    assert fr.trifr(lti=3, mtc=2, exposure_hours=hours) is None
    assert fr.trir(lti=3, mtc=2, exposure_hours=hours) is None
    assert fr.ifr(lti=3, first_aid=9, exposure_hours=hours) is None
    assert fr.dart_rate(lti=3, rwc=1, exposure_hours=hours) is None
    assert fr.severity_rate(lost_days=16, exposure_hours=hours) is None
    assert fr.rate(3, hours, fr.BASE_PER_MILLION) is None


def test_injuries_without_exposure_never_read_as_zero_injuries():
    """The Meridian Apparel case: incidents on record, no manhours return. The
    honest answer is "unmeasured", and the dangerous answer is 0.00."""
    assert fr.ltifr(lti=5, fatalities=1, exposure_hours=None) is None


def test_a_real_zero_is_still_zero():
    """The rule must not swallow genuine zero-injury months — a plant that worked
    193,640 hours with no LTI really does have LTIFR 0.00."""
    assert fr.ltifr(lti=0, exposure_hours=193_640) == 0.0


def test_negative_exposure_is_treated_as_missing():
    assert fr.ltifr(lti=1, exposure_hours=-100) is None


def test_pct_zero_of_zero_is_none_not_zero_percent():
    assert fr.pct(0, 0) is None
    assert fr.pct(None, None) is None
    assert fr.pct(0, 10) == 0.0
    assert fr.pct(7, 10) == 70.0


# ── rule 2: the base belongs to the rate ────────────────────────────────────


def test_per_million_rates_use_the_is3786_base():
    for name in ("ltifr", "trifr", "ifr", "severityRate"):
        assert fr.RATE_BASES[name] == 1_000_000, name


def test_osha_rates_use_the_200k_base():
    for name in ("trir", "dartRate"):
        assert fr.RATE_BASES[name] == 200_000, name


def test_ltifr_and_trir_differ_by_exactly_their_bases():
    """One count, two published rates. The five-fold gap between them is the
    BASE, and it has to come out of the rate rather than out of a caller."""
    kw = dict(lti=4, mtc=0, rwc=0, fatalities=0, exposure_hours=1_000_000)
    assert fr.ltifr(lti=4, exposure_hours=1_000_000) == 4.0
    assert fr.trir(**kw) == 0.8
    assert fr.ltifr(lti=4, exposure_hours=1_000_000) == pytest.approx(fr.trir(**kw) * 5)


def test_the_five_fold_understatement_is_gone():
    """payload.py's old `_rate(ltiCount, hours)` on the 200,000-hour base against
    the correct per-million LTIFR, on the real NW 2026-05 row."""
    wrong = round(1 * 200_000 / 200_200, 4)
    assert wrong == pytest.approx(0.999, abs=5e-4)  # == the stored column today
    assert fr.ltifr(lti=1, exposure_hours=200_200) == pytest.approx(4.995, abs=5e-4)


# ── rule 3: never average rates ─────────────────────────────────────────────


def test_summed_numerators_over_summed_exposure_beats_averaging_rates():
    """A quiet 40-hour month must not weigh the same as a 200,000-hour one."""
    months = [(1, 40_000), (0, 200_000), (0, 200_000)]
    correct = fr.ltifr(lti=sum(m[0] for m in months), exposure_hours=sum(m[1] for m in months))
    averaged = sum(fr.ltifr(lti=n, exposure_hours=h) for n, h in months) / 3
    assert correct == pytest.approx(2.2727, abs=1e-4)
    assert averaged == pytest.approx(8.3333, abs=1e-4)
    assert correct != pytest.approx(averaged)


# ── the numerators ──────────────────────────────────────────────────────────


def test_fatalities_count_as_lost_time_injuries():
    """The legacy stored column omitted them, understating the rate at exactly the
    sites where it matters most."""
    assert fr.ltifr(lti=2, fatalities=1, exposure_hours=1_000_000) == 3.0


def test_recordable_excludes_first_aid_and_ifr_includes_it():
    kw = dict(lti=1, mtc=1, rwc=1, fatalities=0, exposure_hours=1_000_000)
    assert fr.trifr(**kw) == 3.0
    assert fr.ifr(**{**kw, "first_aid": 5}) == 8.0


def test_dart_is_lti_rwc_and_fatality_only():
    """Days away / restricted / transferred — an MTC involves neither."""
    assert fr.dart_rate(lti=1, rwc=1, fatalities=1, exposure_hours=200_000) == 3.0


def test_severity_charges_six_thousand_days_per_fatality():
    assert fr.FATALITY_DAYS_CHARGED == 6_000
    assert fr.severity_rate(lost_days=0, fatalities=1, exposure_hours=1_000_000) == 6000.0
    assert fr.severity_rate(lost_days=16, fatalities=0, exposure_hours=1_000_000) == 16.0


# ── the derived index ───────────────────────────────────────────────────────


def test_fsi_propagates_unavailability_instead_of_returning_zero():
    """The frontend's version returned 0 for a missing input and then banded it as
    excellent — a false zero laundered through a square root."""
    assert fr.fsi(None, 12.0) is None
    assert fr.fsi(1.7, None) is None
    assert fr.fsi(None, None) is None


def test_fsi_matches_the_is3786_formula():
    assert fr.fsi(2.0, 500.0) == pytest.approx(math.sqrt(2.0 * 500.0 / 1000.0), abs=1e-4)


# ── the reported defect, end to end ─────────────────────────────────────────


def test_the_reported_screen_now_reads_a_real_rate():
    """Manhours Performance showed 0.00 "World Class" for NW while its own pyramid
    showed 3 LTIs. NW had 1,763,320 hours of submitted exposure the whole time —
    the denominator was discarded, not missing."""
    value = fr.ltifr(lti=NW_LTI_12M, exposure_hours=NW_HOURS_12M)
    assert value == pytest.approx(1.7013, abs=1e-4)
    # Registry bands for LTIFR: worldClass 1.0, excellent 2.0, average 5.0.
    assert value > 1.0, "must no longer band as WORLD_CLASS"
    assert value <= 2.0, "and lands in EXCELLENT on the real numbers"


def test_a_nonzero_lti_count_can_never_yield_a_zero_rate():
    """The invariant the whole module exists to hold: if injuries were reported,
    the published rate is either positive or explicitly unavailable. It is never
    0.00, which is the one value that reads as excellent."""
    for hours in (0, None, 1, 1_000, 1_763_320, 30_215_000):
        for fn in (fr.ltifr, fr.trifr, fr.trir):
            out = fn(lti=3, exposure_hours=hours)
            assert out is None or out > 0, f"{fn.__name__} returned {out} for {hours}h"
