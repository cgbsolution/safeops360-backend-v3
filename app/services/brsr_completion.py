"""Completion rollup for a BRSR cycle.

Answers the only two questions the cycle dashboard asks: how much of this
disclosure is done, and how much of it the platform produced rather than a
person typing.

The denominator is the seeded indicator catalogue, filtered to what actually has
to be answered:

* **Mandatory indicators only.** Leadership indicators are voluntary under the
  SEBI format. Counting them would show an entity at 60% when its required
  disclosure is complete, and the number nobody trusts is the number nobody uses.
* **NOT_APPLICABLE counts as answered.** An explicit N/A with a reason IS a
  complete disclosure. An empty cell is not.
* **Auto % is measured against answered, not against total.** "78% of what we
  have came from platform data" is a statement about provenance; dividing by
  total would silently conflate it with progress.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brsr import (
    PLATFORM_SOURCED_PRINCIPLES,
    PRINCIPLES,
    PROVENANCE_AUTO,
    PROVENANCE_AUTO_OVERRIDDEN,
    BrsrIndicator,
    BrsrIndicatorValue,
    BrsrPrincipleResponse,
    BrsrReportingCycle,
)

# Provenances that count as the platform having produced the figure. An
# overridden value still began as an auto-population, and the review screen
# needs to be able to say so.
_AUTO_PROVENANCES = (PROVENANCE_AUTO, PROVENANCE_AUTO_OVERRIDDEN)


def _is_answered(v: BrsrIndicatorValue) -> bool:
    """Whether a value row constitutes an answer.

    A row can exist and still be empty — the sweep creates one the moment a
    resolver runs, and a person can open a form and save nothing. Presence of
    the row is therefore not the test; presence of a value is.
    """
    if v.provenance == "NOT_APPLICABLE":
        return bool(v.notApplicableReason)
    return (
        v.valueNumber is not None
        or v.valueBoolean is not None
        or (v.valueText or "").strip() != ""
        or v.valueJson not in (None, {}, [])
    )


async def recompute(
    db: AsyncSession, cycle: BrsrReportingCycle
) -> dict[str, dict[str, float | int]]:
    """Recompute every principle response's rollup and the cycle's own.

    Mutates the rows in the session; the caller commits. Returns a per-principle
    summary so the API can hand the dashboard fresh numbers in the same response
    that triggered the recompute.
    """
    indicators = list(
        (
            await db.execute(
                select(BrsrIndicator).where(
                    BrsrIndicator.isActive.is_(True), BrsrIndicator.isMandatory.is_(True)
                )
            )
        ).scalars()
    )
    values = {
        v.indicatorCode: v
        for v in (
            await db.execute(
                select(BrsrIndicatorValue).where(BrsrIndicatorValue.cycleId == cycle.id)
            )
        ).scalars()
    }
    responses = {
        r.principle: r
        for r in (
            await db.execute(
                select(BrsrPrincipleResponse).where(BrsrPrincipleResponse.cycleId == cycle.id)
            )
        ).scalars()
    }

    now = datetime.now(timezone.utc)
    summary: dict[str, dict[str, float | int]] = {}

    cycle_total = 0
    cycle_answered = 0
    cycle_auto = 0

    for principle in PRINCIPLES:
        applicable = [i for i in indicators if i.principle == principle]
        answered = 0
        auto = 0
        for ind in applicable:
            v = values.get(ind.code)
            if v is None or not _is_answered(v):
                continue
            answered += 1
            if v.provenance in _AUTO_PROVENANCES:
                auto += 1

        total = len(applicable)
        cycle_total += total
        cycle_answered += answered
        cycle_auto += auto

        completion = round(100.0 * answered / total, 2) if total else 0.0
        # Denominator is `answered`, not `total` — see the module docstring.
        auto_pct = round(100.0 * auto / answered, 2) if answered else 0.0

        summary[principle] = {
            "totalIndicators": total,
            "answeredIndicators": answered,
            "autoPopulatedIndicators": auto,
            "manualPendingIndicators": total - answered,
            "completionPct": completion,
            "autoPopulatedPct": auto_pct,
            "isPlatformSourced": principle in PLATFORM_SOURCED_PRINCIPLES,
        }

        row = responses.get(principle)
        if row is not None:
            row.totalIndicators = total
            row.answeredIndicators = answered
            row.autoPopulatedIndicators = auto
            row.completionPct = completion
            row.autoPopulatedPct = auto_pct
            row.lastComputedAt = now
            # Advance the per-principle status, but never regress a REVIEWED
            # principle: a re-sweep that adds one figure must not quietly undo
            # a reviewer's sign-off without them seeing it.
            if row.status != "REVIEWED":
                if answered == 0:
                    row.status = "NOT_STARTED"
                elif answered < total:
                    row.status = "IN_PROGRESS"
                else:
                    row.status = "READY_FOR_REVIEW"

    # Sections A and B carry no principle; they still count toward the cycle.
    section_ab = [i for i in indicators if i.principle is None]
    for ind in section_ab:
        cycle_total += 1
        v = values.get(ind.code)
        if v is not None and _is_answered(v):
            cycle_answered += 1
            if v.provenance in _AUTO_PROVENANCES:
                cycle_auto += 1

    cycle.completionPct = round(100.0 * cycle_answered / cycle_total, 2) if cycle_total else 0.0
    cycle.autoPopulatedPct = (
        round(100.0 * cycle_auto / cycle_answered, 2) if cycle_answered else 0.0
    )
    cycle.lastComputedAt = now

    summary["_cycle"] = {
        "totalIndicators": cycle_total,
        "answeredIndicators": cycle_answered,
        "autoPopulatedIndicators": cycle_auto,
        "manualPendingIndicators": cycle_total - cycle_answered,
        "completionPct": cycle.completionPct,
        "autoPopulatedPct": cycle.autoPopulatedPct,
    }
    return summary


async def unverified_auto_values(
    db: AsyncSession, cycle_id: str
) -> list[BrsrIndicatorValue]:
    """Auto-populated figures nobody has looked at yet.

    The review screen's work queue, and the check that gates APPROVED: filing a
    disclosure whose numbers no human has confirmed is the failure mode this
    whole provenance model exists to prevent.
    """
    return list(
        (
            await db.execute(
                select(BrsrIndicatorValue).where(
                    BrsrIndicatorValue.cycleId == cycle_id,
                    BrsrIndicatorValue.provenance == PROVENANCE_AUTO,
                    BrsrIndicatorValue.isVerified.is_(False),
                )
            )
        ).scalars()
    )


__all__ = ["recompute", "unverified_auto_values"]
