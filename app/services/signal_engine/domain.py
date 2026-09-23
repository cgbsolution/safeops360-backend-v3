"""Signal Engine — shared helpers for the domain rules.

`data_access.py` serves the schema-level data-quality rules (it walks tables
generically). This module serves the correlation and statistical rules, which
read named business tables and need three things the generic layer cannot give
them: human labels, a shared vocabulary of "open", and outlier maths.

Two house rules are enforced here rather than left to each rule:

  • **Never render a cuid.** Every site/area id a rule touches is resolved to a
    label through `LabelBook` before it reaches a narrative.
  • **One definition of open.** "Open" is spelled out once per module below. If
    Incident and CAPA disagreed about it, two signals would contradict each
    other and the engine's whole claim — that it reads every module as one
    dataset — would be false.

Every column name below was read off the live database, not assumed.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ── Module → (table, open-state predicate, business-date column) ─────────────
# Read off prod 2026-08-18. The state vocabularies differ per module because the
# workflows genuinely differ; what must NOT differ is that each one is written
# down once, here.
OPEN_PREDICATE: dict[str, str] = {
    "INCIDENT": "status::text IN ('REPORTED','INVESTIGATION','CAPA_ASSIGNED')",
    "OBSERVATION": "status::text IN ('OPEN','ASSIGNED','IN_PROGRESS')",
    "NEAR_MISS": "status::text IN ('REPORTED','UNDER_REVIEW','ACTION_ASSIGNED')",
    "CAPA": "state NOT IN ('CLOSED','CANCELLED','VERIFIED')",
    "PTW": "status::text NOT IN ('CLOSED','CANCELLED','REJECTED','EXPIRED')",
    "CAMS_AUDIT": "status::text NOT IN ('CLOSED','CANCELLED')",
}

MODULE_TABLE: dict[str, str] = {
    "INCIDENT": "Incident",
    "OBSERVATION": "Observation",
    "NEAR_MISS": "NearMiss",
    "CAPA": "Capa",
    "PTW": "Permit",
    "CAMS_AUDIT": "ComplianceAudit",
    "MOC": "ChangeRequest",
    "HIRA": "HiraStudy",
    "LOTO": "LotoProcedure",
    "TRAINING": "TrainingAssignment",
}

# Modules whose table carries `isDeleted`. Reading a soft-deleted row into a
# correlation would let a deleted incident keep driving a live signal.
SOFT_DELETE_MODULES = frozenset({"INCIDENT", "CAPA", "CAMS_AUDIT", "LOTO", "TRAINING"})

# The human-facing record reference per module (never the id).
MODULE_REF_COLUMN: dict[str, str] = {
    "INCIDENT": "number",
    "OBSERVATION": "number",
    "NEAR_MISS": "number",
    "CAPA": "capaNumber",
    "PTW": "number",
    "CAMS_AUDIT": "auditNumber",
    "MOC": "number",
    "HIRA": "number",
    "LOTO": "procedureCode",
}

# The module's own "when did this happen" column.
MODULE_DATE_COLUMN: dict[str, str] = {
    "INCIDENT": "date",
    "OBSERVATION": "date",
    "NEAR_MISS": "date",
    "CAPA": "createdAt",
    "PTW": "validFrom",
    "CAMS_AUDIT": "scheduledDate",
    "MOC": "createdAt",
}


def soft_delete_clause(module: str) -> str:
    return '"isDeleted" = false' if module in SOFT_DELETE_MODULES else "TRUE"


# ── Labels ───────────────────────────────────────────────────────────────────
@dataclass
class LabelBook:
    """id → label for the entities a narrative can name.

    Loaded once per rule run. Area names repeat across plants ("Process Area A"
    exists at both Meridian sites), so a colliding area name is disambiguated
    with its plant CODE — two identically-labelled findings in one list is
    indistinguishable from a bug.
    """

    sites: dict[str, str] = field(default_factory=dict)
    site_codes: dict[str, str] = field(default_factory=dict)
    areas: dict[str, str] = field(default_factory=dict)

    def site(self, sid: str | None) -> str:
        if not sid:
            return "an unassigned site"
        return self.sites.get(sid) or "an unknown site"

    def code(self, sid: str | None) -> str:
        return self.site_codes.get(sid or "", "??")

    def area(self, aid: str | None) -> str:
        if not aid:
            return "an unassigned area"
        return self.areas.get(aid) or "an unknown area"


async def load_labels(db: AsyncSession) -> LabelBook:
    lb = LabelBook()
    for pid, code, name in (
        await db.execute(text('SELECT id, code, name FROM "Plant"'))
    ).all():
        lb.sites[pid] = f"{code} — {name}" if code else name
        lb.site_codes[pid] = code or "??"

    rows = (await db.execute(text('SELECT id, name, "plantId" FROM "Area"'))).all()
    counts: dict[str, int] = {}
    for _, name, _p in rows:
        counts[name] = counts.get(name, 0) + 1
    for aid, name, pid in rows:
        if counts.get(name, 0) > 1 and pid:
            lb.areas[aid] = f"{name} · {lb.site_codes.get(pid, '??')}"
        else:
            lb.areas[aid] = name
    return lb


# ── Outlier maths ────────────────────────────────────────────────────────────
def median_abs_deviation(values: list[float]) -> tuple[float, float]:
    """(median, MAD). MAD rather than standard deviation on purpose.

    These populations are small and skewed — two Meridian sites carry most of
    the volume and twenty Page Industries sites carry a handful each. A mean and
    a σ computed over that are dragged by the very points we are testing, so a
    genuine outlier raises the threshold that is supposed to catch it. The
    median and MAD are not.
    """
    if not values:
        return (0.0, 0.0)
    med = statistics.median(values)
    return (med, statistics.median([abs(v - med) for v in values]))


def robust_z(value: float, med: float, mad: float) -> float:
    """Modified z-score (Iglewicz–Hoaglin). 0.6745 makes MAD comparable to σ.

    A MAD of zero means over half the population sits on the same value; there
    is then no spread to be an outlier against, so anything different is
    reported as a large but capped score rather than an infinity.
    """
    if mad <= 0:
        return 0.0 if value == med else 6.0
    return 0.6745 * (value - med) / mad


def pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


async def rows(db: AsyncSession, sql: str, **params: Any) -> list[dict[str, Any]]:
    """Run a read-only query and return plain dicts.

    Plain dicts, not ORM instances, so nothing a rule returns can be expired by
    a later `rollback()` in the runner — the exact fault that made the engine
    emit zero signals for its entire life. Rules must never hold ORM state.
    """
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


__all__ = [
    "LabelBook",
    "MODULE_DATE_COLUMN",
    "MODULE_REF_COLUMN",
    "MODULE_TABLE",
    "OPEN_PREDICATE",
    "SOFT_DELETE_MODULES",
    "load_labels",
    "median_abs_deviation",
    "pct",
    "plural",
    "robust_z",
    "rows",
    "soft_delete_clause",
]
