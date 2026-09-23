"""Frequency rates — one definition for the whole platform.

Every safety frequency rate on this platform is `numerator x base / exposure
hours`. That is trivial arithmetic, which is exactly why it had been written
eight times, and why the eight copies disagreed. This module is the only place
allowed to know the formula, the base, or what to do when there is no exposure.

**Three rules.**

1. *A missing denominator is None, never 0.* "0 injuries over 0 hours" is not a
   zero-injury month, it is an unmeasured one, and rendering it as 0.00 reads as
   the best possible result. Two screens got this wrong in opposite ways:
   ``routers/dashboard.py`` returned ``0.0``, and the frontend KPI engine
   returned ``0`` and then banded it "WORLD CLASS". ``scorecard/rollup.py`` got
   it right and said so in a docstring; this module makes the right answer the
   only reachable one.

2. *The base belongs to the rate, not to the caller.* LTIFR, TRIFR, IFR and
   severity rate are per **1,000,000** hours (IS 3786:1983 / ILO). TRIR and DART
   are per **200,000** hours (OSHA 29 CFR 1904 — 100 FTE-years). Passing a base
   in is how ``scorecard/payload.py`` came to publish LTIFR on the 200,000-hour
   base — a factor-of-five understatement against the same metric on the Manhours
   screens. Ask for a named rate; you cannot choose its base.

3. *Never average rates.* Combining periods or sites means summing numerators and
   summing exposure, then dividing once. Averaging three monthly LTIFRs weights a
   40-hour month equally with a 200,000-hour one. Every function here takes
   totals for exactly that reason.

**Canonical source.** The ``Manhours`` monthly return is the platform's canonical
exposure and injury-count source (``employeeHours + contractorHours``,
``ltiCount``, ``mtcCount``, ``rwcCount``, ``facCount``, ``fatalityCount``,
``lostDays``). Resolving those rows is the caller's job — this module stays pure
so it is testable without a database.

Note the stored ``Manhours.ltifr`` / ``.trir`` / ``.severityRate`` columns are NOT
read by anything any more. They are NOT NULL DEFAULT 0 (so they cannot express
"unmeasured"), and the values currently in production were written on the
200,000-hour base for all three, which is wrong for LTIFR and severity rate.
Derive from the components instead — for a single plant-month that reproduces the
intended published figure exactly, and for any wider window it is the only
correct answer.
"""

from __future__ import annotations

# ── bases ────────────────────────────────────────────────────────────────────
# Per million person-hours: IS 3786:1983 (India) and the ILO convention.
BASE_PER_MILLION = 1_000_000
# Per 200,000 person-hours: OSHA 29 CFR 1904 — 100 full-time equivalents working
# 40 hours for 50 weeks.
BASE_OSHA = 200_000

#: The base each named rate is published on. See rule 2 — callers read, never set.
RATE_BASES: dict[str, int] = {
    "ltifr": BASE_PER_MILLION,
    "trifr": BASE_PER_MILLION,
    "ifr": BASE_PER_MILLION,
    "severityRate": BASE_PER_MILLION,
    "trir": BASE_OSHA,
    "dartRate": BASE_OSHA,
}

#: Days charged for a fatality when computing severity rate (IS 3786:1983).
FATALITY_DAYS_CHARGED = 6_000

_PRECISION = 4


def rate(numerator: float | None, exposure_hours: float | None, base: int) -> float | None:
    """``numerator * base / exposure_hours``, or ``None`` when there is no exposure.

    Prefer the named helpers below — they carry the correct base. This is exposed
    for rates the platform does not yet name (the near-miss and observation
    reporting rates on the KPI screens, which share the denominator but not the
    numerator).
    """
    if not exposure_hours or exposure_hours <= 0:
        return None
    return round((numerator or 0) * base / exposure_hours, _PRECISION)


def pct(numerator: float | None, denominator: float | None) -> float | None:
    """A percentage, or ``None`` when nothing was counted. "0 of 0" is not "0%".

    The same rule as :func:`rate`, for the compliance-style indicators. A
    scorecard that prints 0% training completion when no training was assigned is
    worse than one that says it does not know.
    """
    if not denominator or denominator <= 0:
        return None
    return round(100.0 * (numerator or 0) / denominator, 1)


# ── the named rates ──────────────────────────────────────────────────────────


def ltifr(*, lti: int, fatalities: int = 0, exposure_hours: float | None) -> float | None:
    """Lost Time Injury Frequency Rate — per million hours (IS 3786:1983).

    Fatalities are lost-time injuries. Omitting them (as the legacy stored column
    did) understates the rate at exactly the sites where it matters most.
    """
    return rate((lti or 0) + (fatalities or 0), exposure_hours, RATE_BASES["ltifr"])


def trifr(
    *, lti: int, mtc: int = 0, rwc: int = 0, fatalities: int = 0, exposure_hours: float | None
) -> float | None:
    """Total Recordable Injury Frequency Rate — per million hours.

    Recordable = LTI + MTC + RWC + fatality. First-aid cases are excluded per OSHA
    recordability; :func:`ifr` is the first-aid-inclusive variant.
    """
    return rate(
        (lti or 0) + (mtc or 0) + (rwc or 0) + (fatalities or 0),
        exposure_hours,
        RATE_BASES["trifr"],
    )


def trir(
    *, lti: int, mtc: int = 0, rwc: int = 0, fatalities: int = 0, exposure_hours: float | None
) -> float | None:
    """Total Recordable Incident Rate — the same numerator as :func:`trifr` on the
    OSHA 200,000-hour base.

    Two names for one count on two bases. Keeping both metrics is deliberate;
    keeping both bases reachable from one argument was the bug.
    """
    return rate(
        (lti or 0) + (mtc or 0) + (rwc or 0) + (fatalities or 0),
        exposure_hours,
        RATE_BASES["trir"],
    )


def ifr(
    *,
    lti: int,
    mtc: int = 0,
    rwc: int = 0,
    first_aid: int = 0,
    fatalities: int = 0,
    exposure_hours: float | None,
) -> float | None:
    """Injury Frequency Rate — all personal injuries, first-aid inclusive, per
    million hours. What Indian industry reports as "IFR"."""
    return rate(
        (lti or 0) + (mtc or 0) + (rwc or 0) + (first_aid or 0) + (fatalities or 0),
        exposure_hours,
        RATE_BASES["ifr"],
    )


def dart_rate(
    *, lti: int, rwc: int = 0, fatalities: int = 0, exposure_hours: float | None
) -> float | None:
    """Days Away, Restricted or Transferred rate — per 200,000 hours (OSHA)."""
    return rate((lti or 0) + (rwc or 0) + (fatalities or 0), exposure_hours, RATE_BASES["dartRate"])


def severity_rate(
    *, lost_days: int, fatalities: int = 0, exposure_hours: float | None
) -> float | None:
    """Severity Rate — days lost per million hours, each fatality charged at
    :data:`FATALITY_DAYS_CHARGED` days (IS 3786:1983)."""
    charged = (lost_days or 0) + (fatalities or 0) * FATALITY_DAYS_CHARGED
    return rate(charged, exposure_hours, RATE_BASES["severityRate"])


def fsi(ltifr_value: float | None, severity_rate_value: float | None) -> float | None:
    """Frequency-Severity Index — sqrt((LTIFR x Severity Rate) / 1000), IS 3786.

    ``None`` in, ``None`` out. A derived index over an unmeasured input is not
    zero; the frontend's version returned 0 and then banded it as excellent.
    """
    if ltifr_value is None or severity_rate_value is None:
        return None
    product = ltifr_value * severity_rate_value
    if product <= 0:
        return 0.0
    return round((product / 1000.0) ** 0.5, _PRECISION)


__all__ = [
    "BASE_OSHA",
    "BASE_PER_MILLION",
    "FATALITY_DAYS_CHARGED",
    "RATE_BASES",
    "dart_rate",
    "fsi",
    "ifr",
    "ltifr",
    "pct",
    "rate",
    "severity_rate",
    "trifr",
    "trir",
]
