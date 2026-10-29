"""Safe coercion of model-supplied strings into DB enum values.

Why this exists
---------------
Agent tools take free-text filter values straight from the model. When
such a value is compared against a *Postgres enum* column, an invalid
string does not come back as a tidy Python error — asyncpg raises
InvalidTextRepresentationError, and Postgres **aborts the whole
transaction**. Every later statement on that session then fails with
"current transaction is aborted", including the agent runtime's own
attempt to persist the invocation result. The invocation is left stuck
in RUNNING forever with no error recorded.

That is exactly what a model guessing `permitType="LOTO"` (a value that
looks obvious but is spelled ELECTRICAL_LOTO in the enum) used to do to
every RCA run.

So: never let a model-supplied string reach an enum column. Validate it
here first and raise a plain ValueError listing the accepted values. The
agent runtime catches that and feeds it back as a tool error, which the
model can act on — it will usually retry with a correct value — and the
DB session stays clean.
"""

from __future__ import annotations

import enum
from collections.abc import Collection
from typing import TypeVar

E = TypeVar("E", bound=enum.Enum)


# Plain-English names models reach for, keyed by the normalised spelling.
# Without these, "Lost Time Injury" or "LOTO" costs a wasted tool round-trip.
INCIDENT_TYPE_ALIASES: dict[str, str] = {
    "LOST_TIME_INJURY": "LTI",
    "LOST_TIME": "LTI",
    "MEDICAL_TREATMENT_CASE": "MTC",
    "MEDICAL_TREATMENT": "MTC",
    "RESTRICTED_WORK_CASE": "RWC",
    "RESTRICTED_WORK": "RWC",
    "FIRST_AID_CASE": "FIRST_AID",
    "NEAR_MISS": "HIPO_NEAR_MISS",
    "HIGH_POTENTIAL_NEAR_MISS": "HIPO_NEAR_MISS",
    "ENVIRONMENT": "ENVIRONMENTAL",
    "PROPERTY": "PROPERTY_DAMAGE",
}
PERMIT_TYPE_ALIASES: dict[str, str] = {
    "LOTO": "ELECTRICAL_LOTO",
    "ELECTRICAL": "ELECTRICAL_LOTO",
    "ELECTRICAL_ISOLATION": "ELECTRICAL_LOTO",
    "COLD_WORK": "GENERAL_COLD",
    "GENERAL": "GENERAL_COLD",
    "HEIGHT": "WORK_AT_HEIGHT",
    "WORKING_AT_HEIGHT": "WORK_AT_HEIGHT",
    "WORK_AT_HEIGHTS": "WORK_AT_HEIGHT",
    "CRANE": "LIFTING",
    "LIFTING_OPERATIONS": "LIFTING",
}


def coerce_enum(
    value: str,
    enum_cls: type[E],
    field_name: str,
    aliases: dict[str, str] | None = None,
    allowed: Collection[str] | None = None,
) -> E:
    """Convert a model-supplied string into `enum_cls`, or raise ValueError.

    Matching is case-insensitive and tolerates spaces/hyphens in place of
    underscores, so "hot work" and "Hot-Work" both resolve to HOT_WORK.
    `aliases` maps further normalised spellings onto enum values.
    `allowed` restricts the result to these values — for a Python enum that
    carries labels the Postgres type lacks (see ObservationCategory).
    The error message lists every valid value so the model can self-
    correct on its next turn instead of guessing again.
    """
    raw = str(getattr(value, "value", value)).strip()
    normalised = raw.upper().replace(" ", "_").replace("-", "_")
    if aliases and normalised in aliases:
        normalised = aliases[normalised]

    for member in enum_cls:
        if allowed is not None and member.value not in allowed:
            continue
        if member.value.upper() == normalised or member.name.upper() == normalised:
            return member

    valid = ", ".join(m.value for m in enum_cls if allowed is None or m.value in allowed)
    raise ValueError(
        f"Invalid {field_name}: {raw!r}. Valid values are: {valid}. "
        "Retry with one of these, or omit the filter to search all values."
    )


__all__ = ["coerce_enum", "INCIDENT_TYPE_ALIASES", "PERMIT_TYPE_ALIASES"]
