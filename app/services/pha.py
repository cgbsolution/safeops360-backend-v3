"""PHA / HAZOP service: workflow rules, risk ranking, CAPA bridge, hub aggregate.

Pure helpers first (vocabularies, state moves, gates, matrix rating, dates).
They take plain values, have no DB access and are tested offline in
tests/test_hazop.py. The DB helpers below them load a RiskMatrix, keep
recommendations in step with their CAPAs, and build the hub KPIs.

Risk ranking uses the platform RiskMatrix tables (app/models/hira.py): the cell
for (likelihood, severity) carries the score, level and colour, so a HAZOP row
is coloured exactly like a HIRA entry rated on the same matrix. A study with no
matrix (none seeded in the tenant) falls back to DEFAULT_MATRIX, a plain 5x5.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# ── vocabularies ─────────────────────────────────────────────────────────────
METHODS = ("HAZOP", "WHAT_IF", "HAZID")
STUDY_STATUSES = ("DRAFT", "IN_PROGRESS", "UNDER_REVIEW", "APPROVED", "CLOSED")
EDITABLE_STATUSES = ("DRAFT", "IN_PROGRESS")
REC_STATUSES = ("OPEN", "IN_PROGRESS", "CLOSED", "REJECTED")
REC_OPEN = ("OPEN", "IN_PROGRESS")
PRIORITIES = ("HIGH", "MEDIUM", "LOW")
TEAM_ROLES = ("FACILITATOR", "SCRIBE", "PROCESS_ENGINEER", "OPERATIONS", "MAINTENANCE", "INSTRUMENTATION",
              "ELECTRICAL", "SAFETY", "DESIGN_ENGINEER", "VENDOR", "MEMBER")
DISCIPLINES = ("PROCESS", "OPERATIONS", "MECHANICAL", "ELECTRICAL", "INSTRUMENTATION", "SAFETY", "ENVIRONMENT",
               "PROJECTS", "QUALITY", "OTHER")
REVALIDATION_YEARS = 5
REVALIDATION_WARNING_DAYS = 180
LEVEL_RANK = {"LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4, "EXTREME": 4}

_STUDY_MOVES = {
    "DRAFT": {"IN_PROGRESS"},
    "IN_PROGRESS": {"UNDER_REVIEW"},
    "UNDER_REVIEW": {"APPROVED", "IN_PROGRESS"},  # approve, or return for rework
    "APPROVED": {"CLOSED"},
    "CLOSED": set(),
}
_REC_MOVES = {
    "OPEN": {"IN_PROGRESS", "CLOSED", "REJECTED"},
    "IN_PROGRESS": {"OPEN", "CLOSED", "REJECTED"},
    "CLOSED": {"OPEN"},  # reopen if verification fails
    "REJECTED": {"OPEN"},
}

# CAPA states (universal engine) and what they mean for the recommendation.
CAPA_DONE = {"VERIFIED", "CLOSED", "CLOSED_RECURRED"}
CAPA_DEAD = {"CANCELLED", "REJECTED"}
CAPA_WORKING = {"ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION"}
CAPA_SOURCE_CODE = "PHA_RECOMMENDATION"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


as_aware = _aware


# ── workflow ─────────────────────────────────────────────────────────────────
def can_move_study(current: str, target: str) -> bool:
    return target in _STUDY_MOVES.get(current, set())


def can_move_rec(current: str, target: str) -> bool:
    return target in _REC_MOVES.get(current, set())


def is_editable(status: str) -> bool:
    return status in EDITABLE_STATUSES


def submit_blockers(
    *, status: str, facilitator_id: str | None, approver_id: str | None, team_count: int, session_count: int,
    node_row_counts: dict[str, int], incomplete_rows: Iterable[str],
) -> list[str]:
    """Why a study cannot go to review. Empty list = ready.

    node_row_counts: {"Node 1 - Fuel gas skid": 6, ...}
    incomplete_rows: labels of rows with no consequence or no S/L rating.
    """
    out: list[str] = []
    if status != "IN_PROGRESS":
        out.append(f"Only a study in progress can be submitted (this one is {status}).")
    if not facilitator_id:
        out.append("Name the facilitator.")
    if not approver_id:
        out.append("Name the approver.")
    if team_count < 2:
        out.append("A PHA is a team exercise: record at least two team members.")
    if session_count < 1:
        out.append("Record at least one study session.")
    if not node_row_counts:
        out.append("Add at least one node.")
    empty = [n for n, c in node_row_counts.items() if c == 0]
    if empty:
        out.append(f"These nodes have no worksheet rows: {', '.join(empty)}.")
    rows = list(incomplete_rows)
    if rows:
        more = f" and {len(rows) - 8} more" if len(rows) > 8 else ""
        out.append(f"Every row needs consequences and a severity / likelihood rating. Incomplete: {', '.join(rows[:8])}{more}.")
    return out


def approve_blockers(*, status: str, actor_id: str, facilitator_id: str | None) -> list[str]:
    out: list[str] = []
    if status != "UNDER_REVIEW":
        out.append(f"Only a study under review can be approved (this one is {status}).")
    if facilitator_id and actor_id == facilitator_id:
        out.append("The facilitator cannot approve their own study.")
    return out


def close_blockers(*, status: str, recs: Iterable[tuple[int, str, str | None]]) -> list[str]:
    """recs: (recNo, status, justification). A study closes only when every
    recommendation is CLOSED, or REJECTED with a written justification."""
    out: list[str] = []
    if status != "APPROVED":
        out.append(f"Only an approved study can be closed (this one is {status}).")
    open_recs, unjustified = [], []
    for no, st, just in recs:
        if st in REC_OPEN:
            open_recs.append(rec_label(no))
        elif st == "REJECTED" and not (just and just.strip()):
            unjustified.append(rec_label(no))
    if open_recs:
        out.append(f"Recommendations still open: {', '.join(open_recs)}.")
    if unjustified:
        out.append(f"Rejected without a justification: {', '.join(unjustified)}.")
    return out


def rec_move_blockers(
    *, current: str, target: str, justification: str | None, closure_note: str | None, capa_state: str | None,
) -> list[str]:
    out: list[str] = []
    if target not in REC_STATUSES:
        return [f"status must be one of {', '.join(REC_STATUSES)}"]
    if not can_move_rec(current, target):
        return [f"Cannot move a recommendation from {current} to {target}."]
    if target == "REJECTED" and len((justification or "").strip()) < 10:
        out.append("Rejecting a recommendation needs a written justification (at least 10 characters).")
    if target == "CLOSED":
        if not (closure_note and closure_note.strip()):
            out.append("A closure note is required.")
        if capa_state and capa_state not in CAPA_DONE and capa_state not in CAPA_DEAD:
            out.append(f"This recommendation is tracked by a CAPA (state {capa_state}); close the CAPA and it closes here.")
    return out


def rec_label(no: int | None) -> str:
    return f"R-{int(no or 0):03d}"


def rec_overdue(status: str, due: datetime | None, now: datetime | None = None) -> bool:
    due = _aware(due)
    return bool(due and status in REC_OPEN and due < (now or utcnow()))


def add_years(d: datetime, years: int) -> datetime:
    """Same calendar day `years` later; 29 Feb falls back to 28 Feb."""
    y = d.year + years
    day = min(d.day, calendar.monthrange(y, d.month)[1])
    return d.replace(year=y, day=day)


def default_revalidation(approved_at: datetime) -> datetime:
    return add_years(approved_at, REVALIDATION_YEARS)


def revalidation_state(due: datetime | None, status: str, now: datetime | None = None) -> str | None:
    """OVERDUE / DUE_SOON / CURRENT for approved or closed studies, else None."""
    due = _aware(due)
    if due is None or status not in ("APPROVED", "CLOSED"):
        return None
    now = now or utcnow()
    if due < now:
        return "OVERDUE"
    if due < now + timedelta(days=REVALIDATION_WARNING_DAYS):
        return "DUE_SOON"
    return "CURRENT"


# ── risk ranking ─────────────────────────────────────────────────────────────
LEVEL_COLORS = {"LOW": "#22c55e", "MODERATE": "#eab308", "HIGH": "#f97316", "CRITICAL": "#dc2626"}


@dataclass
class Matrix:
    """A risk matrix in memory: scales and an explicit cell map."""
    id: str | None
    code: str
    name: str
    likelihood_levels: int
    severity_levels: int
    likelihoods: list[dict[str, Any]] = field(default_factory=list)
    severities: list[dict[str, Any]] = field(default_factory=list)
    cells: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)  # (likelihood, severity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "code": self.code, "name": self.name,
            "likelihoodLevels": self.likelihood_levels, "severityLevels": self.severity_levels,
            "likelihoods": self.likelihoods, "severities": self.severities,
            "cells": [
                {"likelihood": l, "severity": s, **c}
                for (l, s), c in sorted(self.cells.items())
            ],
        }


def _default_level(score: int) -> str:
    if score >= 20:
        return "CRITICAL"
    if score >= 10:
        return "HIGH"
    if score >= 5:
        return "MODERATE"
    return "LOW"


def _build_default() -> Matrix:
    sev = ["Negligible", "Minor", "Moderate", "Major", "Catastrophic"]
    lik = ["Rare", "Unlikely", "Possible", "Likely", "Almost certain"]
    cells = {}
    for l in range(1, 6):
        for s in range(1, 6):
            lvl = _default_level(l * s)
            cells[(l, s)] = {"score": l * s, "level": lvl, "color": LEVEL_COLORS[lvl]}
    return Matrix(
        id=None, code="PHA-DEFAULT-5X5", name="Default 5 x 5 (no matrix configured)",
        likelihood_levels=5, severity_levels=5,
        likelihoods=[{"score": i + 1, "label": t, "description": t} for i, t in enumerate(lik)],
        severities=[{"score": i + 1, "label": t, "description": t} for i, t in enumerate(sev)],
        cells=cells,
    )


DEFAULT_MATRIX = _build_default()


def rate(matrix: Matrix, severity: int | None, likelihood: int | None) -> dict[str, Any] | None:
    """Rank one (severity, likelihood) pair on the matrix.

    None when either input is missing (row not yet rated). ValueError when an
    input is outside the matrix scale: a 6 on a 5-level scale is a data error,
    not something to clamp silently. A missing cell (sparse seed) falls back to
    the product with the default banding, so a row is never left uncoloured.
    """
    if severity is None or likelihood is None:
        return None
    if not (1 <= int(severity) <= matrix.severity_levels):
        raise ValueError(f"Severity must be between 1 and {matrix.severity_levels}")
    if not (1 <= int(likelihood) <= matrix.likelihood_levels):
        raise ValueError(f"Likelihood must be between 1 and {matrix.likelihood_levels}")
    cell = matrix.cells.get((int(likelihood), int(severity)))
    if cell:
        return {"score": cell["score"], "level": cell["level"], "color": cell["color"]}
    score = int(severity) * int(likelihood)
    lvl = _default_level(round(score * 25 / (matrix.severity_levels * matrix.likelihood_levels)))
    return {"score": score, "level": lvl, "color": LEVEL_COLORS[lvl]}


def apply_rating(row: Any, matrix: Matrix) -> None:
    """Write both ratings (before and after recommendations) onto a deviation."""
    r = rate(matrix, row.severity, row.likelihood)
    row.riskScore, row.riskLevel, row.riskColor = (r["score"], r["level"], r["color"]) if r else (None, None, None)
    rr = rate(matrix, row.residualSeverity, row.residualLikelihood)
    row.residualScore, row.residualLevel, row.residualColor = (rr["score"], rr["level"], rr["color"]) if rr else (None, None, None)


async def load_matrix(db: AsyncSession, matrix_id: str | None) -> Matrix:
    """The study's matrix, or the tenant default, or DEFAULT_MATRIX."""
    from app.models.hira import RiskMatrix, RiskMatrixCell, RiskMatrixLikelihood, RiskMatrixSeverity

    m = await db.get(RiskMatrix, matrix_id) if matrix_id else None
    if m is None:
        m = (
            await db.execute(
                select(RiskMatrix).where(RiskMatrix.isActive.is_(True))
                .order_by(RiskMatrix.isDefault.desc(), RiskMatrix.code).limit(1)
            )
        ).scalar_one_or_none()
    if m is None:
        return DEFAULT_MATRIX
    liks = (await db.execute(select(RiskMatrixLikelihood).where(RiskMatrixLikelihood.matrixId == m.id).order_by(RiskMatrixLikelihood.score))).scalars().all()
    sevs = (await db.execute(select(RiskMatrixSeverity).where(RiskMatrixSeverity.matrixId == m.id).order_by(RiskMatrixSeverity.score))).scalars().all()
    cells = (await db.execute(select(RiskMatrixCell).where(RiskMatrixCell.matrixId == m.id))).scalars().all()
    return Matrix(
        id=m.id, code=m.code, name=m.name, likelihood_levels=m.likelihoodLevels, severity_levels=m.severityLevels,
        likelihoods=[{"score": x.score, "label": x.label, "description": x.description} for x in liks],
        severities=[{"score": x.score, "label": x.label, "description": x.description} for x in sevs],
        cells={(c.likelihoodScore, c.severityScore): {"score": c.riskScore, "level": c.riskLevel, "color": c.colorHex} for c in cells},
    )


async def matrix_exists(db: AsyncSession, matrix_id: str) -> bool:
    from app.models.hira import RiskMatrix

    m = await db.get(RiskMatrix, matrix_id)
    return bool(m and m.isActive)


async def list_matrices(db: AsyncSession) -> list[dict[str, Any]]:
    from app.models.hira import RiskMatrix

    rows = (await db.execute(select(RiskMatrix).where(RiskMatrix.isActive.is_(True)).order_by(RiskMatrix.isDefault.desc(), RiskMatrix.code))).scalars().all()
    return [
        {"id": r.id, "code": r.code, "name": r.name, "likelihoodLevels": r.likelihoodLevels,
         "severityLevels": r.severityLevels, "isDefault": bool(r.isDefault)}
        for r in rows
    ]


# ── CAPA bridge ──────────────────────────────────────────────────────────────
_CAPA_SEVERITY = {"CRITICAL": "CRITICAL", "EXTREME": "CRITICAL", "HIGH": "HIGH", "MODERATE": "MODERATE", "MEDIUM": "MODERATE", "LOW": "LOW"}
_CAPA_PRIORITY = {"HIGH": "HIGH", "MEDIUM": "MODERATE", "LOW": "LOW"}


def capa_severity(risk_level: str | None) -> str:
    return _CAPA_SEVERITY.get((risk_level or "").upper(), "MODERATE")


def capa_priority(priority: str | None) -> str:
    return _CAPA_PRIORITY.get((priority or "").upper(), "MODERATE")


def rec_status_from_capa(rec_status: str, capa_state: str | None) -> str | None:
    """New recommendation status implied by its CAPA, or None for no change.

    The CAPA engine owns verification, so a verified / closed CAPA closes the
    recommendation. A CAPA being worked moves an OPEN recommendation to
    IN_PROGRESS. A cancelled CAPA changes nothing here: the team still has to
    decide (close with a note, or reject with a justification)."""
    if not capa_state:
        return None
    if capa_state in CAPA_DONE and rec_status in REC_OPEN:
        return "CLOSED"
    if capa_state in CAPA_WORKING and rec_status == "OPEN":
        return "IN_PROGRESS"
    return None


async def sync_recs_with_capas(db: AsyncSession, recs: list[Any]) -> int:
    """Pull CAPA state onto linked recommendations (status sync on read).
    Returns how many rows changed; the caller commits."""
    from app.models.capa import Capa

    ids = [r.capaId for r in recs if r.capaId]
    if not ids:
        return 0
    try:
        # Savepoint: on Postgres a failed statement would otherwise abort the
        # whole request transaction, not just this optional read.
        async with db.begin_nested():
            capas = (
                await db.execute(select(Capa.id, Capa.state, Capa.capaNumber).where(Capa.id.in_(ids)).execution_options(include_deleted=True))
            ).all()
    except Exception:  # noqa: BLE001 - CAPA tables absent (e.g. a test DB): read stays usable
        return 0
    by_id = {cid: (state, no) for cid, state, no in capas}
    changed = 0
    for r in recs:
        if not r.capaId or r.capaId not in by_id:
            continue
        state, no = by_id[r.capaId]
        dirty = False
        if r.capaState != state:
            r.capaState = state
            dirty = True
        if no and r.capaNumber != no:
            r.capaNumber = no
            dirty = True
        nxt = rec_status_from_capa(r.status, state)
        if nxt:
            r.status = nxt
            if nxt == "CLOSED":
                r.closedAt = utcnow()
                r.closureNote = r.closureNote or f"Closed through {no or 'the linked CAPA'} ({state})."
            dirty = True
        changed += int(dirty)
    return changed


async def spawn_rec_capa(db: AsyncSession, *, rec: Any, study: Any, deviation: Any, node: Any, actor_id: str) -> tuple[Any | None, str | None]:
    """Raise a universal CAPA for a recommendation. Returns (capa, warning).

    Runs in a savepoint: a missing source type (DDL not applied) or any CAPA
    failure leaves the recommendation saved and reports a warning instead."""
    from app.services.capa_spawn import spawn_capa

    if not rec.ownerId:
        return None, "A CAPA needs an owner: assign the recommendation first."
    due_days = 90
    if rec.dueDate is not None:
        due_days = max(1, (_aware(rec.dueDate) - utcnow()).days)
    try:
        async with db.begin_nested():
            capa = await spawn_capa(
                db, source_code=CAPA_SOURCE_CODE, plant_id=study.plantId,
                title=f"{study.method.replace('_', '-')} {rec_label(rec.recNo)}: {rec.text[:150]}",
                problem=(
                    f"{study.studyNo} node {node.nodeNo} ({node.title}). Deviation: {deviation.deviation}. "
                    f"Consequences: {(deviation.consequences or 'not recorded')[:400]}. "
                    f"Risk {deviation.riskLevel or 'unrated'}. Recommendation: {rec.text[:600]}"
                ),
                ref_id=rec.id, ref_url=f"/process-safety/hazop/{study.id}",
                ref_summary=f"{study.studyNo} {rec_label(rec.recNo)}",
                metadata={"phaStudyId": study.id, "studyNo": study.studyNo, "nodeId": node.id, "deviationId": deviation.id,
                          "recommendationId": rec.id, "recNo": rec.recNo, "riskLevel": deviation.riskLevel, "method": study.method},
                severity=capa_severity(deviation.riskLevel), priority=capa_priority(rec.priority),
                detected_method="PHA_STUDY", owner_id=rec.ownerId, actor_id=actor_id, due_days=due_days,
            )
            await db.flush()
    except ValueError as e:
        return None, f"CAPA not raised: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"CAPA not raised: {type(e).__name__}"
    rec.capaId, rec.capaNumber, rec.capaState = capa.id, capa.capaNumber, capa.state
    return capa, None


async def cancel_rec_capa(db: AsyncSession, rec: Any) -> None:
    """A rejected recommendation stops its CAPA from chasing anyone."""
    from app.models.capa import Capa

    if not rec.capaId:
        return
    try:
        capa = await db.get(Capa, rec.capaId)
    except Exception:  # noqa: BLE001
        return
    if capa is not None and capa.state not in CAPA_DONE and capa.state not in CAPA_DEAD:
        capa.state = "CANCELLED"
        capa.stateChangedAt = utcnow()
        rec.capaState = "CANCELLED"


# ── hub aggregate ────────────────────────────────────────────────────────────
async def hub_kpis(db: AsyncSession, plant_ids: list[str] | None, now: datetime | None = None) -> dict[str, Any]:
    from app.models.pha import PhaRecommendation, PhaStudy

    now = now or utcnow()

    def plant_where(col):
        return True if plant_ids is None else col.in_(plant_ids or ["__none__"])

    by_status = {s: 0 for s in STUDY_STATUSES}
    for st, n in (
        await db.execute(
            select(PhaStudy.status, func.count()).where(PhaStudy.isDeleted.is_(False)).where(plant_where(PhaStudy.plantId))
            .group_by(PhaStudy.status)
        )
    ).all():
        by_status[st] = int(n)
    by_method = {m: 0 for m in METHODS}
    for m, n in (
        await db.execute(
            select(PhaStudy.method, func.count()).where(PhaStudy.isDeleted.is_(False)).where(plant_where(PhaStudy.plantId))
            .group_by(PhaStudy.method)
        )
    ).all():
        by_method[m] = int(n)
    rec_base = select(func.count()).select_from(PhaRecommendation).where(PhaRecommendation.isDeleted.is_(False)).where(plant_where(PhaRecommendation.plantId))
    open_recs = int(await db.scalar(rec_base.where(PhaRecommendation.status.in_(REC_OPEN))) or 0)
    overdue = int(await db.scalar(rec_base.where(PhaRecommendation.status.in_(REC_OPEN)).where(PhaRecommendation.dueDate < now)) or 0)
    reval_base = (
        select(func.count()).select_from(PhaStudy).where(PhaStudy.isDeleted.is_(False)).where(plant_where(PhaStudy.plantId))
        .where(PhaStudy.status.in_(("APPROVED", "CLOSED"))).where(PhaStudy.revalidationDueDate.is_not(None))
    )
    reval_overdue = int(await db.scalar(reval_base.where(PhaStudy.revalidationDueDate < now)) or 0)
    reval_soon = int(await db.scalar(
        reval_base.where(PhaStudy.revalidationDueDate >= now).where(PhaStudy.revalidationDueDate < now + timedelta(days=REVALIDATION_WARNING_DAYS))
    ) or 0)
    return {
        "studiesByStatus": by_status, "studiesByMethod": by_method,
        "studiesTotal": sum(by_status.values()),
        "studiesActive": by_status["DRAFT"] + by_status["IN_PROGRESS"] + by_status["UNDER_REVIEW"],
        "openRecommendations": open_recs, "overdueRecommendations": overdue,
        "revalidationOverdue": reval_overdue, "revalidationDueSoon": reval_soon,
        "revalidationWindowDays": REVALIDATION_WARNING_DAYS,
    }
