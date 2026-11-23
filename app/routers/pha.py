"""PHA / HAZOP router (JSL Hisar process safety). Mounts at /api/pha.

Studies (HAZOP / What-If / HAZID) with team, sessions, nodes, the deviation
worksheet, recommendations (optionally raised as universal CAPAs), the
guideword library, AI suggestions per node, the XLSX worksheet and the data
for the printable report. GET /dashboard feeds the /process-safety hub.

Workflow: DRAFT -> IN_PROGRESS -> UNDER_REVIEW -> APPROVED -> CLOSED, with
UNDER_REVIEW -> IN_PROGRESS to return a study for rework. The worksheet is
editable only in DRAFT / IN_PROGRESS. A study closes when every
recommendation is CLOSED, or REJECTED with a justification.

RBAC (granted by safeops_360/prisma/apply-pha-permissions.ts):
  PHA.READ     view studies, worksheets, register, hub KPIs (plant-scoped)
  PHA.CREATE   create a study
  PHA.UPDATE   edit header, team, sessions, nodes, worksheet; raise and move
               recommendations; submit for review
  PHA.APPROVE  approve or return a study under review; close an approved study
A recommendation's owner may always move their own recommendation.

Mounted ungated (same reasoning as ims / fire_safety): a new licence module
code would 403 under the signed licence that predates it. Per-endpoint RBAC is
the gate.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.soft_delete import soft_delete
from app.models.pha import PhaDeviation, PhaNode, PhaRecommendation, PhaSession, PhaStudy, PhaTeamMember
from app.models.user import User
from app.services import pha as svc
from app.services import pha_ai
from app.services import pha_library as lib
from app.services.access_scope import build_query_scope
from app.services.numbering import next_number
from app.services.pha_export import worksheet_xlsx
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names
from app.services.user_directory import resolve_user_directory

router = APIRouter(prefix="/api/pha", tags=["pha"])

READ, CREATE, UPDATE, APPROVE = "PHA.READ", "PHA.CREATE", "PHA.UPDATE", "PHA.APPROVE"
METHOD_LABELS = {"HAZOP": "HAZOP", "WHAT_IF": "What-If", "HAZID": "HAZID"}


# ── helpers ──────────────────────────────────────────────────────────────────
async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _allowed(db: AsyncSession, user: User, perm: str, plant_id: str | None) -> bool:
    return (await can(db, user.id, perm, PermissionContext(plant_id=plant_id))).allowed


async def _scoped_plants(db: AsyncSession, user: User, plant_id: str | None) -> list[str] | None:
    """Plant filter for reads: None = all plants. A requested plant outside the
    caller's scope is refused, not silently emptied."""
    await _require(db, user, READ)
    scope = await build_query_scope(db, user.id, READ)
    if plant_id:
        if not scope.allows_plant(plant_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant is outside your access scope")
        return [plant_id]
    return None if scope.all_plants else scope.plant_ids


def _plant_where(col, plants: list[str] | None):
    return True if plants is None else col.in_(plants or ["__none__"])


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


async def _names(db: AsyncSession, user_ids, plant_ids) -> tuple[dict[str, str], dict[str, str]]:
    users = await resolve_user_directory(db, [u for u in user_ids if u])
    plants = await resolve_plant_names(db, [p for p in plant_ids if p])
    return {k: v.name for k, v in users.items()}, plants


async def _get_live(db: AsyncSession, model, rid: str):
    row = await db.get(model, rid)
    if row is None or getattr(row, "isDeleted", False):
        raise HTTPException(404, f"{model.__name__} not found")
    return row


def _check_choice(value: str | None, allowed: tuple[str, ...], field: str) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(422, f"{field} must be one of {', '.join(allowed)}")


def _ensure_editable(study: PhaStudy) -> None:
    if not svc.is_editable(study.status):
        raise HTTPException(409, f"The worksheet is locked while the study is {study.status}. Return it for rework to edit.")


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s or None


async def _study_of(db: AsyncSession, study_id: str) -> PhaStudy:
    return await _get_live(db, PhaStudy, study_id)


# ── serialisers ──────────────────────────────────────────────────────────────
def _study(s: PhaStudy, users: dict[str, str], plants: dict[str, str]) -> dict[str, Any]:
    return {
        "id": s.id, "studyNo": s.studyNo, "plantId": s.plantId, "plantName": plants.get(s.plantId),
        "unit": s.unit, "area": s.area, "method": s.method, "methodLabel": METHOD_LABELS.get(s.method, s.method),
        "title": s.title, "scope": s.scope, "designIntentSummary": s.designIntentSummary,
        "drawingRefs": list(s.drawingRefs or []), "riskMatrixId": s.riskMatrixId, "status": s.status,
        "facilitatorId": s.facilitatorId, "facilitatorName": users.get(s.facilitatorId or ""),
        "scribeId": s.scribeId, "scribeName": users.get(s.scribeId or ""),
        "approverId": s.approverId, "approverName": users.get(s.approverId or ""),
        "mocId": s.mocId, "submittedAt": _iso(s.submittedAt), "reviewComments": s.reviewComments,
        "approvedAt": _iso(s.approvedAt), "approvedByName": users.get(s.approvedById or ""),
        "revalidationDueDate": _iso(s.revalidationDueDate),
        "revalidationState": svc.revalidation_state(s.revalidationDueDate, s.status),
        "closedAt": _iso(s.closedAt),
    }


def _team(m: PhaTeamMember, users: dict[str, str]) -> dict[str, Any]:
    return {
        "id": m.id, "studyId": m.studyId, "userId": m.userId, "externalName": m.externalName,
        "name": users.get(m.userId or "") or m.externalName or m.userId, "isExternal": not m.userId,
        "organisation": m.organisation, "discipline": m.discipline, "role": m.role,
    }


def _session(s: PhaSession, users: dict[str, str], node_labels: dict[str, str]) -> dict[str, Any]:
    return {
        "id": s.id, "studyId": s.studyId, "sessionNo": s.sessionNo, "sessionDate": _iso(s.sessionDate),
        "durationMinutes": s.durationMinutes, "attendeeUserIds": list(s.attendeeUserIds or []),
        "attendees": [{"id": u, "name": users.get(u, u)} for u in (s.attendeeUserIds or [])],
        "attendeeNames": list(s.attendeeNames or []), "nodeIds": list(s.nodeIds or []),
        "nodeLabels": [node_labels.get(n, "?") for n in (s.nodeIds or [])], "notes": s.notes,
    }


def _node(n: PhaNode, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    st = stats or {}
    return {
        "id": n.id, "studyId": n.studyId, "nodeNo": n.nodeNo, "title": n.title, "description": n.description,
        "designIntent": n.designIntent, "boundaries": n.boundaries, "drawingRef": n.drawingRef,
        "operatingParameters": list(n.operatingParameters or []),
        "rowCount": st.get("rows", 0), "ratedCount": st.get("rated", 0), "openRecs": st.get("openRecs", 0),
        "recCount": st.get("recs", 0), "highestRisk": st.get("highest"),
    }


def _rec(r: PhaRecommendation, users: dict[str, str], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": r.id, "studyId": r.studyId, "plantId": r.plantId, "nodeId": r.nodeId, "deviationId": r.deviationId,
        "recNo": r.recNo, "recLabel": svc.rec_label(r.recNo), "text": r.text, "ownerId": r.ownerId,
        "ownerName": users.get(r.ownerId or ""), "dueDate": _iso(r.dueDate), "priority": r.priority,
        "status": r.status, "justification": r.justification, "closureNote": r.closureNote,
        "closedAt": _iso(r.closedAt), "capaId": r.capaId, "capaNumber": r.capaNumber, "capaState": r.capaState,
        "overdue": svc.rec_overdue(r.status, r.dueDate), **(ctx or {}),
    }


def _dev(d: PhaDeviation, recs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": d.id, "studyId": d.studyId, "nodeId": d.nodeId, "rowNo": d.rowNo,
        "guideword": d.guideword, "guidewordLabel": lib.guideword_label(d.guideword),
        "parameter": d.parameter, "parameterLabel": lib.parameter_label(d.parameter),
        "deviation": d.deviation, "causes": d.causes, "consequences": d.consequences, "safeguards": d.safeguards,
        "severity": d.severity, "likelihood": d.likelihood, "riskScore": d.riskScore, "riskLevel": d.riskLevel,
        "riskColor": d.riskColor, "residualSeverity": d.residualSeverity, "residualLikelihood": d.residualLikelihood,
        "residualScore": d.residualScore, "residualLevel": d.residualLevel, "residualColor": d.residualColor,
        "remarks": d.remarks, "aiSuggested": bool(d.aiSuggested), "recommendations": recs,
    }


# ── loaders ──────────────────────────────────────────────────────────────────
async def _nodes(db: AsyncSession, study_id: str) -> list[PhaNode]:
    return list((await db.execute(
        select(PhaNode).where(PhaNode.studyId == study_id).where(PhaNode.isDeleted.is_(False)).order_by(PhaNode.nodeNo)
    )).scalars().all())


async def _devs(db: AsyncSession, *, study_id: str | None = None, node_id: str | None = None) -> list[PhaDeviation]:
    stmt = select(PhaDeviation).where(PhaDeviation.isDeleted.is_(False))
    stmt = stmt.where(PhaDeviation.nodeId == node_id) if node_id else stmt.where(PhaDeviation.studyId == study_id)
    return list((await db.execute(stmt.order_by(PhaDeviation.rowNo))).scalars().all())


async def _recs(db: AsyncSession, *, study_id: str | None = None, node_id: str | None = None) -> list[PhaRecommendation]:
    stmt = select(PhaRecommendation).where(PhaRecommendation.isDeleted.is_(False))
    stmt = stmt.where(PhaRecommendation.nodeId == node_id) if node_id else stmt.where(PhaRecommendation.studyId == study_id)
    return list((await db.execute(stmt.order_by(PhaRecommendation.recNo))).scalars().all())


async def _sync(db: AsyncSession, recs: list[PhaRecommendation]) -> None:
    if await svc.sync_recs_with_capas(db, recs):
        await db.commit()


def _node_label(n: PhaNode) -> str:
    return f"Node {n.nodeNo} - {n.title}"


async def _study_detail(db: AsyncSession, s: PhaStudy, user: User) -> dict[str, Any]:
    await db.refresh(s)
    team = (await db.execute(select(PhaTeamMember).where(PhaTeamMember.studyId == s.id).order_by(PhaTeamMember.createdAt))).scalars().all()
    sessions = (await db.execute(select(PhaSession).where(PhaSession.studyId == s.id).order_by(PhaSession.sessionNo))).scalars().all()
    nodes = await _nodes(db, s.id)
    devs = await _devs(db, study_id=s.id)
    recs = await _recs(db, study_id=s.id)
    await _sync(db, recs)
    matrix = await svc.load_matrix(db, s.riskMatrixId)

    stats: dict[str, dict[str, Any]] = {n.id: {"rows": 0, "rated": 0, "recs": 0, "openRecs": 0, "highest": None} for n in nodes}
    for d in devs:
        st = stats.get(d.nodeId)
        if st is None:
            continue
        st["rows"] += 1
        if d.riskLevel:
            st["rated"] += 1
            if svc.LEVEL_RANK.get(d.riskLevel, 0) > svc.LEVEL_RANK.get((st["highest"] or {}).get("level", ""), 0):
                st["highest"] = {"level": d.riskLevel, "color": d.riskColor, "score": d.riskScore}
    for r in recs:
        st = stats.get(r.nodeId)
        if st is not None:
            st["recs"] += 1
            st["openRecs"] += int(r.status in svc.REC_OPEN)

    ids = [s.facilitatorId, s.scribeId, s.approverId, s.approvedById, *[m.userId for m in team],
           *[u for x in sessions for u in (x.attendeeUserIds or [])], *[r.ownerId for r in recs]]
    users, plants = await _names(db, ids, [s.plantId])
    node_labels = {n.id: f"Node {n.nodeNo}" for n in nodes}
    incomplete = [f"{node_labels.get(d.nodeId, '?')}.{d.rowNo}" for d in devs if not (d.consequences and d.riskLevel)]
    rec_counts = {st_: sum(1 for r in recs if r.status == st_) for st_ in svc.REC_STATUSES}
    gate = dict(
        facilitator_id=s.facilitatorId, approver_id=s.approverId, team_count=len(team), session_count=len(sessions),
        node_row_counts={_node_label(n): stats[n.id]["rows"] for n in nodes}, incomplete_rows=incomplete,
    )
    return {
        **_study(s, users, plants),
        "team": [_team(m, users) for m in team],
        "sessions": [_session(x, users, node_labels) for x in sessions],
        "nodes": [_node(n, stats.get(n.id)) for n in nodes],
        "matrix": matrix.to_dict(),
        "recommendationCounts": rec_counts,
        "overdueRecommendations": sum(1 for r in recs if svc.rec_overdue(r.status, r.dueDate)),
        "incompleteRows": incomplete,
        # Preview of the submit gate while the worksheet is open (status checked on submit).
        "submitBlockers": svc.submit_blockers(status="IN_PROGRESS", **gate) if s.status in svc.EDITABLE_STATUSES else [],
        "closeBlockers": svc.close_blockers(status=s.status, recs=[(r.recNo, r.status, r.justification) for r in recs])
        if s.status == "APPROVED" else [],
        "perms": {
            "update": await _allowed(db, user, UPDATE, s.plantId),
            "approve": await _allowed(db, user, APPROVE, s.plantId),
        },
    }


async def _worksheet(db: AsyncSession, node: PhaNode, study: PhaStudy) -> dict[str, Any]:
    devs = await _devs(db, node_id=node.id)
    recs = await _recs(db, node_id=node.id)
    await _sync(db, recs)
    users, _ = await _names(db, [r.ownerId for r in recs], [])
    by_dev: dict[str, list[dict[str, Any]]] = {}
    for r in recs:
        by_dev.setdefault(r.deviationId, []).append(_rec(r, users))
    matrix = await svc.load_matrix(db, study.riskMatrixId)
    return {
        "node": _node(node, {"rows": len(devs), "rated": sum(1 for d in devs if d.riskLevel), "recs": len(recs),
                             "openRecs": sum(1 for r in recs if r.status in svc.REC_OPEN)}),
        "studyId": study.id, "studyStatus": study.status, "editable": svc.is_editable(study.status),
        "deviations": [_dev(d, by_dev.get(d.id, [])) for d in devs],
        "matrix": matrix.to_dict(),
    }


# ── meta, matrices, hub ──────────────────────────────────────────────────────
@router.get("/meta")
async def meta(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "methods": [{"code": m, "label": METHOD_LABELS[m]} for m in svc.METHODS],
        "studyStatuses": svc.STUDY_STATUSES, "recStatuses": svc.REC_STATUSES, "priorities": svc.PRIORITIES,
        "teamRoles": svc.TEAM_ROLES, "disciplines": svc.DISCIPLINES, "revalidationYears": svc.REVALIDATION_YEARS,
        "library": lib.library(),
    }


@router.get("/risk-matrices")
async def risk_matrices(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    items = await svc.list_matrices(db)
    return {"items": items, "fallback": None if items else svc.DEFAULT_MATRIX.to_dict()}


@router.get("/risk-matrices/{mid}")
async def risk_matrix(mid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    return (await svc.load_matrix(db, mid)).to_dict()


@router.get("/dashboard")
async def dashboard(plantId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    kpis = await svc.hub_kpis(db, plants)
    recent = (await db.execute(
        select(PhaStudy).where(PhaStudy.isDeleted.is_(False)).where(_plant_where(PhaStudy.plantId, plants))
        .order_by(PhaStudy.updatedAt.desc()).limit(6)
    )).scalars().all()
    reval = (await db.execute(
        select(PhaStudy).where(PhaStudy.isDeleted.is_(False)).where(_plant_where(PhaStudy.plantId, plants))
        .where(PhaStudy.status.in_(("APPROVED", "CLOSED"))).where(PhaStudy.revalidationDueDate.is_not(None))
        .order_by(PhaStudy.revalidationDueDate.asc()).limit(5)
    )).scalars().all()
    users, pl = await _names(db, [r.facilitatorId for r in recent], [r.plantId for r in [*recent, *reval]])
    return {**kpis, "plantId": plantId,
            "recentStudies": [_study(r, users, pl) for r in recent],
            "revalidationQueue": [_study(r, users, pl) for r in reval]}


# ── studies ──────────────────────────────────────────────────────────────────
class StudyIn(BaseModel):
    plantId: str
    title: str = Field(min_length=3, max_length=200)
    method: str = "HAZOP"
    unit: str | None = Field(None, max_length=120)
    area: str | None = Field(None, max_length=120)
    scope: str | None = Field(None, max_length=8000)
    designIntentSummary: str | None = Field(None, max_length=8000)
    drawingRefs: list[str] = Field(default_factory=list, max_length=100)
    riskMatrixId: str | None = None
    facilitatorId: str | None = None
    scribeId: str | None = None
    approverId: str | None = None
    mocId: str | None = None


class StudyPatch(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=200)
    method: str | None = None
    unit: str | None = Field(None, max_length=120)
    area: str | None = Field(None, max_length=120)
    scope: str | None = Field(None, max_length=8000)
    designIntentSummary: str | None = Field(None, max_length=8000)
    drawingRefs: list[str] | None = Field(None, max_length=100)
    riskMatrixId: str | None = None
    facilitatorId: str | None = None
    scribeId: str | None = None
    approverId: str | None = None
    mocId: str | None = None


class CommentIn(BaseModel):
    comments: str | None = Field(None, max_length=4000)


class ApproveIn(BaseModel):
    comments: str | None = Field(None, max_length=4000)
    revalidationDueDate: datetime | None = None


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


async def _check_matrix(db: AsyncSession, matrix_id: str | None) -> None:
    if matrix_id and not await svc.matrix_exists(db, matrix_id):
        raise HTTPException(422, "Unknown or inactive risk matrix.")


def _drawings(refs: list[str] | None) -> list[str]:
    return list(dict.fromkeys(r.strip() for r in (refs or []) if r and r.strip()))


@router.get("/studies")
async def list_studies(
    plantId: str | None = Query(None), status_: str | None = Query(None, alias="status"), method: str | None = Query(None),
    q: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    stmt = select(PhaStudy).where(PhaStudy.isDeleted.is_(False)).where(_plant_where(PhaStudy.plantId, plants))
    if status_ == "ACTIVE":
        stmt = stmt.where(PhaStudy.status.in_(("DRAFT", "IN_PROGRESS", "UNDER_REVIEW")))
    elif status_:
        stmt = stmt.where(PhaStudy.status == status_)
    if method:
        stmt = stmt.where(PhaStudy.method == method)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(PhaStudy.title.ilike(like), PhaStudy.studyNo.ilike(like), PhaStudy.unit.ilike(like)))
    rows = (await db.execute(stmt.order_by(PhaStudy.createdAt.desc()).limit(500))).scalars().all()
    ids = [r.id for r in rows]
    counts: dict[str, dict[str, int]] = {}
    if ids:
        for sid, n in (await db.execute(
            select(PhaNode.studyId, func.count()).where(PhaNode.isDeleted.is_(False)).where(PhaNode.studyId.in_(ids)).group_by(PhaNode.studyId)
        )).all():
            counts.setdefault(sid, {})["nodes"] = int(n)
        for sid, st, n in (await db.execute(
            select(PhaRecommendation.studyId, PhaRecommendation.status, func.count()).where(PhaRecommendation.isDeleted.is_(False))
            .where(PhaRecommendation.studyId.in_(ids)).group_by(PhaRecommendation.studyId, PhaRecommendation.status)
        )).all():
            c = counts.setdefault(sid, {})
            c["recs"] = c.get("recs", 0) + int(n)
            if st in svc.REC_OPEN:
                c["openRecs"] = c.get("openRecs", 0) + int(n)
    users, pl = await _names(db, [r.facilitatorId for r in rows], [r.plantId for r in rows])
    items = [{**_study(r, users, pl), "nodeCount": counts.get(r.id, {}).get("nodes", 0),
              "recCount": counts.get(r.id, {}).get("recs", 0), "openRecs": counts.get(r.id, {}).get("openRecs", 0)} for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/studies", status_code=201)
async def create_study(body: StudyIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, CREATE, body.plantId)
    _check_choice(body.method, svc.METHODS, "method")
    await _check_matrix(db, body.riskMatrixId)
    year = svc.utcnow().year
    no = await next_number(db, column=PhaStudy.studyNo, stem=f"PHA-{year}-", width=4)
    s = PhaStudy(
        studyNo=no, plantId=body.plantId, title=body.title.strip(), method=body.method, unit=_clean(body.unit),
        area=_clean(body.area), scope=_clean(body.scope), designIntentSummary=_clean(body.designIntentSummary),
        drawingRefs=_drawings(body.drawingRefs), riskMatrixId=body.riskMatrixId, status="DRAFT",
        facilitatorId=body.facilitatorId, scribeId=body.scribeId, approverId=body.approverId, mocId=_clean(body.mocId),
        createdBy=user.id,
    )
    db.add(s)
    await db.flush()
    # The facilitator and scribe are team members by definition.
    for uid, role in ((body.facilitatorId, "FACILITATOR"), (body.scribeId, "SCRIBE")):
        if uid:
            db.add(PhaTeamMember(studyId=s.id, userId=uid, role=role))
    await db.commit()
    return await _study_detail(db, s, user)


@router.get("/studies/{sid}")
async def get_study(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, READ, s.plantId)
    return await _study_detail(db, s, user)


@router.patch("/studies/{sid}")
async def update_study(sid: str, body: StudyPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    data = body.model_dump(exclude_unset=True)
    if "method" in data:
        _check_choice(data["method"], svc.METHODS, "method")
    if "title" in data and data["title"] is None:
        raise HTTPException(422, "title cannot be empty")
    rerate = "riskMatrixId" in data and data["riskMatrixId"] != s.riskMatrixId
    if rerate:
        await _check_matrix(db, data["riskMatrixId"])
    for k, v in data.items():
        if k == "drawingRefs":
            v = _drawings(v)
        elif isinstance(v, str):
            v = v.strip() if k == "title" else _clean(v)
        setattr(s, k, v)
    if rerate:
        matrix = await svc.load_matrix(db, s.riskMatrixId)
        for d in await _devs(db, study_id=s.id):
            try:
                svc.apply_rating(d, matrix)
            except ValueError as e:
                await db.rollback()
                raise HTTPException(409, f"Row {d.rowNo} is rated outside the new matrix scale: {e}") from e
    await db.commit()
    return await _study_detail(db, s, user)


@router.delete("/studies/{sid}")
async def delete_study(sid: str, body: ReasonIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, UPDATE, s.plantId)
    if s.status != "DRAFT":
        raise HTTPException(409, "Only a DRAFT study can be deleted.")
    soft_delete(s, user.id, body.reason)
    await db.commit()
    return {"ok": True}


async def _move(db: AsyncSession, s: PhaStudy, target: str) -> None:
    if not svc.can_move_study(s.status, target):
        raise HTTPException(409, f"Cannot move a {s.status} study to {target}.")
    s.status = target


@router.post("/studies/{sid}/start")
async def start_study(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, UPDATE, s.plantId)
    await _move(db, s, "IN_PROGRESS")
    await db.commit()
    return await _study_detail(db, s, user)


@router.post("/studies/{sid}/submit")
async def submit_study(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, UPDATE, s.plantId)
    detail = await _study_detail(db, s, user)
    blockers = svc.submit_blockers(
        status=s.status, facilitator_id=s.facilitatorId, approver_id=s.approverId, team_count=len(detail["team"]),
        session_count=len(detail["sessions"]),
        node_row_counts={f"Node {n['nodeNo']} - {n['title']}": n["rowCount"] for n in detail["nodes"]},
        incomplete_rows=detail["incompleteRows"],
    )
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    await _move(db, s, "UNDER_REVIEW")
    s.submittedAt = svc.utcnow()
    s.submittedById = user.id
    await db.commit()
    return await _study_detail(db, s, user)


@router.post("/studies/{sid}/return")
async def return_study(sid: str, body: CommentIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, APPROVE, s.plantId)
    if not (body.comments and body.comments.strip()):
        raise HTTPException(422, "Say what needs rework.")
    await _move(db, s, "IN_PROGRESS")
    s.reviewComments = body.comments.strip()
    await db.commit()
    return await _study_detail(db, s, user)


@router.post("/studies/{sid}/approve")
async def approve_study(sid: str, body: ApproveIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, APPROVE, s.plantId)
    blockers = svc.approve_blockers(status=s.status, actor_id=user.id, facilitator_id=s.facilitatorId)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    now = svc.utcnow()
    due = body.revalidationDueDate or svc.default_revalidation(now)
    if svc.as_aware(due) <= now:
        raise HTTPException(422, "The revalidation date must be in the future.")
    s.status = "APPROVED"
    s.approvedAt = now
    s.approvedById = user.id
    s.revalidationDueDate = due
    if body.comments and body.comments.strip():
        s.reviewComments = body.comments.strip()
    await db.commit()
    return await _study_detail(db, s, user)


@router.post("/studies/{sid}/close")
async def close_study(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, APPROVE, s.plantId)
    recs = await _recs(db, study_id=s.id)
    await _sync(db, recs)
    blockers = svc.close_blockers(status=s.status, recs=[(r.recNo, r.status, r.justification) for r in recs])
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    s.status = "CLOSED"
    s.closedAt = svc.utcnow()
    s.closedById = user.id
    await db.commit()
    return await _study_detail(db, s, user)


# ── team ─────────────────────────────────────────────────────────────────────
class TeamIn(BaseModel):
    userId: str | None = None
    externalName: str | None = Field(None, max_length=160)
    organisation: str | None = Field(None, max_length=160)
    discipline: str | None = None
    role: str = "MEMBER"


class TeamPatch(BaseModel):
    organisation: str | None = Field(None, max_length=160)
    discipline: str | None = None
    role: str | None = None


@router.post("/studies/{sid}/team", status_code=201)
async def add_team(sid: str, body: TeamIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    _check_choice(body.role, svc.TEAM_ROLES, "role")
    _check_choice(body.discipline, svc.DISCIPLINES, "discipline")
    if not body.userId and not _clean(body.externalName):
        raise HTTPException(422, "Pick a user or type the external member's name.")
    if body.userId:
        dup = await db.scalar(select(func.count()).select_from(PhaTeamMember).where(PhaTeamMember.studyId == s.id).where(PhaTeamMember.userId == body.userId))
        if dup:
            raise HTTPException(409, "That person is already on the team.")
    db.add(PhaTeamMember(studyId=s.id, userId=body.userId or None, externalName=None if body.userId else _clean(body.externalName),
                         organisation=_clean(body.organisation), discipline=body.discipline, role=body.role))
    await db.commit()
    return await _study_detail(db, s, user)


@router.patch("/team/{mid}")
async def update_team(mid: str, body: TeamPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = await _get_live(db, PhaTeamMember, mid)
    s = await _study_of(db, m.studyId)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    data = body.model_dump(exclude_unset=True)
    _check_choice(data.get("role"), svc.TEAM_ROLES, "role")
    _check_choice(data.get("discipline"), svc.DISCIPLINES, "discipline")
    for k, v in data.items():
        if k == "role" and v is None:
            continue
        setattr(m, k, _clean(v) if k == "organisation" else v)
    await db.commit()
    return await _study_detail(db, s, user)


@router.delete("/team/{mid}")
async def remove_team(mid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    m = await _get_live(db, PhaTeamMember, mid)
    s = await _study_of(db, m.studyId)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    await db.delete(m)
    await db.commit()
    return await _study_detail(db, s, user)


# ── sessions ─────────────────────────────────────────────────────────────────
class SessionIn(BaseModel):
    sessionDate: datetime
    durationMinutes: int | None = Field(None, ge=10, le=24 * 60)
    attendeeUserIds: list[str] = Field(default_factory=list, max_length=100)
    attendeeNames: list[str] = Field(default_factory=list, max_length=100)
    nodeIds: list[str] = Field(default_factory=list, max_length=200)
    notes: str | None = Field(None, max_length=8000)


async def _valid_nodes(db: AsyncSession, study_id: str, node_ids: list[str]) -> list[str]:
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return []
    ok = set((await db.execute(select(PhaNode.id).where(PhaNode.studyId == study_id).where(PhaNode.id.in_(ids)).where(PhaNode.isDeleted.is_(False)))).scalars().all())
    bad = [i for i in ids if i not in ok]
    if bad:
        raise HTTPException(422, "Some nodes covered are not nodes of this study.")
    return ids


def _apply_session(x: PhaSession, body: SessionIn, nodes: list[str]) -> None:
    x.sessionDate = body.sessionDate
    x.durationMinutes = body.durationMinutes
    x.attendeeUserIds = list(dict.fromkeys(body.attendeeUserIds))
    x.attendeeNames = list(dict.fromkeys(n.strip() for n in body.attendeeNames if n and n.strip()))
    x.nodeIds = nodes
    x.notes = _clean(body.notes)


@router.post("/studies/{sid}/sessions", status_code=201)
async def add_session(sid: str, body: SessionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    nodes = await _valid_nodes(db, s.id, body.nodeIds)
    highest = await db.scalar(select(func.max(PhaSession.sessionNo)).where(PhaSession.studyId == s.id))
    x = PhaSession(studyId=s.id, sessionNo=int(highest or 0) + 1, createdBy=user.id, sessionDate=body.sessionDate)
    _apply_session(x, body, nodes)
    db.add(x)
    await db.commit()
    return await _study_detail(db, s, user)


@router.put("/sessions/{xid}")
async def update_session(xid: str, body: SessionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    x = await _get_live(db, PhaSession, xid)
    s = await _study_of(db, x.studyId)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    _apply_session(x, body, await _valid_nodes(db, s.id, body.nodeIds))
    await db.commit()
    return await _study_detail(db, s, user)


@router.delete("/sessions/{xid}")
async def delete_session(xid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    x = await _get_live(db, PhaSession, xid)
    s = await _study_of(db, x.studyId)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    await db.delete(x)
    await db.commit()
    return await _study_detail(db, s, user)


# ── nodes ────────────────────────────────────────────────────────────────────
class ParamIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    value: str | None = Field(None, max_length=120)
    unit: str | None = Field(None, max_length=40)


class NodeIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str | None = Field(None, max_length=8000)
    designIntent: str | None = Field(None, max_length=8000)
    boundaries: str | None = Field(None, max_length=4000)
    drawingRef: str | None = Field(None, max_length=200)
    operatingParameters: list[ParamIn] = Field(default_factory=list, max_length=60)


class NodePatch(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=200)
    description: str | None = Field(None, max_length=8000)
    designIntent: str | None = Field(None, max_length=8000)
    boundaries: str | None = Field(None, max_length=4000)
    drawingRef: str | None = Field(None, max_length=200)
    operatingParameters: list[ParamIn] | None = Field(None, max_length=60)


def _params(ps: list[ParamIn] | None) -> list[dict[str, Any]]:
    return [{"name": p.name.strip(), "value": _clean(p.value), "unit": _clean(p.unit)} for p in (ps or []) if p.name.strip()]


async def _node_and_study(db: AsyncSession, nid: str) -> tuple[PhaNode, PhaStudy]:
    n = await _get_live(db, PhaNode, nid)
    return n, await _study_of(db, n.studyId)


@router.post("/studies/{sid}/nodes", status_code=201)
async def add_node(sid: str, body: NodeIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    highest = await db.scalar(
        select(func.max(PhaNode.nodeNo)).where(PhaNode.studyId == s.id).execution_options(include_deleted=True)
    )
    n = PhaNode(
        studyId=s.id, nodeNo=int(highest or 0) + 1, title=body.title.strip(), description=_clean(body.description),
        designIntent=_clean(body.designIntent), boundaries=_clean(body.boundaries), drawingRef=_clean(body.drawingRef),
        operatingParameters=_params(body.operatingParameters), createdBy=user.id,
    )
    db.add(n)
    await db.commit()
    return await _study_detail(db, s, user)


@router.patch("/nodes/{nid}")
async def update_node(nid: str, body: NodePatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n, s = await _node_and_study(db, nid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "operatingParameters":
            n.operatingParameters = _params(body.operatingParameters)
        elif k == "title":
            if not v:
                raise HTTPException(422, "title cannot be empty")
            n.title = v.strip()
        else:
            setattr(n, k, _clean(v))
    await db.commit()
    return await _worksheet(db, n, s)


@router.delete("/nodes/{nid}")
async def delete_node(nid: str, body: ReasonIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n, s = await _node_and_study(db, nid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    if await _recs(db, node_id=n.id):
        raise HTTPException(409, "This node has recommendations. Delete or move them before deleting the node.")
    for d in await _devs(db, node_id=n.id):
        soft_delete(d, user.id, body.reason)
    soft_delete(n, user.id, body.reason)
    await db.commit()
    return await _study_detail(db, s, user)


@router.get("/nodes/{nid}/worksheet")
async def get_worksheet(nid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n, s = await _node_and_study(db, nid)
    await _require(db, user, READ, s.plantId)
    return await _worksheet(db, n, s)


# ── worksheet rows ───────────────────────────────────────────────────────────
class GenerateIn(BaseModel):
    combinations: list[dict[str, str]] = Field(min_length=1, max_length=200)


class DeviationIn(BaseModel):
    guideword: str | None = None
    parameter: str | None = None
    deviation: str | None = Field(None, max_length=1000)
    causes: str | None = Field(None, max_length=6000)
    consequences: str | None = Field(None, max_length=6000)
    safeguards: str | None = Field(None, max_length=6000)
    severity: int | None = None
    likelihood: int | None = None
    residualSeverity: int | None = None
    residualLikelihood: int | None = None
    remarks: str | None = Field(None, max_length=4000)
    aiSuggested: bool = False
    recommendationText: str | None = Field(None, max_length=4000)  # accept an AI suggestion's recommendation too


class DeviationPatch(BaseModel):
    guideword: str | None = None
    parameter: str | None = None
    deviation: str | None = Field(None, max_length=1000)
    causes: str | None = Field(None, max_length=6000)
    consequences: str | None = Field(None, max_length=6000)
    safeguards: str | None = Field(None, max_length=6000)
    severity: int | None = None
    likelihood: int | None = None
    residualSeverity: int | None = None
    residualLikelihood: int | None = None
    remarks: str | None = Field(None, max_length=4000)


class OrderIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


def _codes(guideword: str | None, parameter: str | None) -> tuple[str | None, str | None]:
    gw = lib.normalise_guideword(guideword) if guideword else None
    pa = lib.normalise_parameter(parameter) if parameter else None
    if guideword and not gw:
        raise HTTPException(422, f"Unknown guideword '{guideword}'")
    if parameter and not pa:
        raise HTTPException(422, f"Unknown parameter '{parameter}'")
    return gw, pa


async def _next_row(db: AsyncSession, node_id: str) -> int:
    return int(await db.scalar(select(func.max(PhaDeviation.rowNo)).where(PhaDeviation.nodeId == node_id).where(PhaDeviation.isDeleted.is_(False))) or 0) + 1


async def _next_rec_no(db: AsyncSession, study_id: str) -> int:
    return int(await db.scalar(
        select(func.max(PhaRecommendation.recNo)).where(PhaRecommendation.studyId == study_id).execution_options(include_deleted=True)
    ) or 0) + 1


@router.post("/nodes/{nid}/generate")
async def generate_rows(nid: str, body: GenerateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n, s = await _node_and_study(db, nid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    existing = [(d.guideword, d.parameter) for d in await _devs(db, node_id=n.id)]
    rows, skipped = lib.generate_rows(body.combinations, existing)
    nxt = await _next_row(db, n.id)
    for i, r in enumerate(rows):
        db.add(PhaDeviation(studyId=s.id, nodeId=n.id, rowNo=nxt + i, guideword=r["guideword"], parameter=r["parameter"],
                            deviation=r["deviation"], createdBy=user.id))
    await db.commit()
    return {**await _worksheet(db, n, s), "created": len(rows), "skipped": skipped}


@router.post("/nodes/{nid}/deviations", status_code=201)
async def add_deviation(nid: str, body: DeviationIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n, s = await _node_and_study(db, nid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    gw, pa = _codes(body.guideword, body.parameter)
    text = _clean(body.deviation) or (lib.deviation_text(gw, pa) if gw and pa else None)
    if not text:
        raise HTTPException(422, "Describe the deviation, or pick a guideword and parameter.")
    d = PhaDeviation(
        studyId=s.id, nodeId=n.id, rowNo=await _next_row(db, n.id), guideword=gw, parameter=pa, deviation=text,
        causes=_clean(body.causes), consequences=_clean(body.consequences), safeguards=_clean(body.safeguards),
        severity=body.severity, likelihood=body.likelihood, residualSeverity=body.residualSeverity,
        residualLikelihood=body.residualLikelihood, remarks=_clean(body.remarks), aiSuggested=body.aiSuggested, createdBy=user.id,
    )
    try:
        svc.apply_rating(d, await svc.load_matrix(db, s.riskMatrixId))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.add(d)
    await db.flush()
    if _clean(body.recommendationText):
        db.add(PhaRecommendation(
            studyId=s.id, plantId=s.plantId, nodeId=n.id, deviationId=d.id, recNo=await _next_rec_no(db, s.id),
            text=_clean(body.recommendationText), priority="MEDIUM", status="OPEN", createdBy=user.id,
        ))
    await db.commit()
    return await _worksheet(db, n, s)


@router.patch("/deviations/{did}")
async def update_deviation(did: str, body: DeviationPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_live(db, PhaDeviation, did)
    s = await _study_of(db, d.studyId)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    data = body.model_dump(exclude_unset=True)
    if "guideword" in data or "parameter" in data:
        gw, pa = _codes(data.get("guideword", d.guideword), data.get("parameter", d.parameter))
        d.guideword, d.parameter = gw, pa
    for k in ("causes", "consequences", "safeguards", "remarks"):
        if k in data:
            setattr(d, k, _clean(data[k]))
    if "deviation" in data:
        if not _clean(data["deviation"]):
            raise HTTPException(422, "The deviation text cannot be empty.")
        d.deviation = _clean(data["deviation"])
    for k in ("severity", "likelihood", "residualSeverity", "residualLikelihood"):
        if k in data:
            setattr(d, k, data[k])
    try:
        svc.apply_rating(d, await svc.load_matrix(db, s.riskMatrixId))
    except ValueError as e:
        await db.rollback()
        raise HTTPException(422, str(e)) from e
    await db.commit()
    recs = (await db.execute(select(PhaRecommendation).where(PhaRecommendation.deviationId == d.id).where(PhaRecommendation.isDeleted.is_(False)).order_by(PhaRecommendation.recNo))).scalars().all()
    users, _ = await _names(db, [r.ownerId for r in recs], [])
    return _dev(d, [_rec(r, users) for r in recs])


@router.delete("/deviations/{did}")
async def delete_deviation(did: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_live(db, PhaDeviation, did)
    s = await _study_of(db, d.studyId)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    n_recs = await db.scalar(select(func.count()).select_from(PhaRecommendation).where(PhaRecommendation.deviationId == d.id).where(PhaRecommendation.isDeleted.is_(False)))
    if n_recs:
        raise HTTPException(409, "This row has recommendations. Delete them first.")
    soft_delete(d, user.id, "Worksheet row removed by the study team")
    await db.commit()
    n = await _get_live(db, PhaNode, d.nodeId)
    return await _worksheet(db, n, s)


@router.put("/nodes/{nid}/order")
async def reorder_rows(nid: str, body: OrderIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n, s = await _node_and_study(db, nid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    devs = {d.id: d for d in await _devs(db, node_id=n.id)}
    if set(body.ids) != set(devs) or len(body.ids) != len(devs):
        raise HTTPException(422, "The order must list every row of this node exactly once.")
    for i, rid in enumerate(body.ids, start=1):
        devs[rid].rowNo = i
    await db.commit()
    return await _worksheet(db, n, s)


@router.post("/nodes/{nid}/ai-suggest")
async def ai_suggest(nid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Suggested rows for review. Nothing is saved: the team accepts each one by
    posting it to /nodes/{id}/deviations."""
    n, s = await _node_and_study(db, nid)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    matrix = (await svc.load_matrix(db, s.riskMatrixId)).to_dict()
    existing = [(d.guideword, d.parameter, d.deviation) for d in await _devs(db, node_id=n.id)]
    suggestions = await pha_ai.suggest_for_node(
        study={"title": s.title, "method": s.method, "unit": s.unit, "area": s.area, "scope": s.scope},
        node=_node(n), matrix=matrix, existing=existing,
    )
    if suggestions is None:
        return {"available": False, "suggestions": [], "message": "AI suggestions are unavailable right now. Use the guideword library or add rows by hand."}
    return {"available": True, "suggestions": suggestions,
            "message": None if suggestions else "No further deviations suggested for this node."}


# ── recommendations ──────────────────────────────────────────────────────────
class RecIn(BaseModel):
    text: str = Field(min_length=5, max_length=4000)
    ownerId: str | None = None
    dueDate: datetime | None = None
    priority: str = "MEDIUM"
    raiseCapa: bool = False


class RecPatch(BaseModel):
    text: str | None = Field(None, min_length=5, max_length=4000)
    ownerId: str | None = None
    dueDate: datetime | None = None
    priority: str | None = None


class RecMoveIn(BaseModel):
    status: str
    justification: str | None = Field(None, max_length=4000)
    closureNote: str | None = Field(None, max_length=4000)


async def _rec_ctx(db: AsyncSession, recs: list[PhaRecommendation]) -> dict[str, dict[str, Any]]:
    """Node / deviation / study labels for the register."""
    if not recs:
        return {}
    nodes = {n.id: n for n in (await db.execute(select(PhaNode).where(PhaNode.id.in_({r.nodeId for r in recs})).execution_options(include_deleted=True))).scalars().all()}
    devs = {d.id: d for d in (await db.execute(select(PhaDeviation).where(PhaDeviation.id.in_({r.deviationId for r in recs})).execution_options(include_deleted=True))).scalars().all()}
    studies = {x.id: x for x in (await db.execute(select(PhaStudy).where(PhaStudy.id.in_({r.studyId for r in recs})).execution_options(include_deleted=True))).scalars().all()}
    out = {}
    for r in recs:
        n, d, st = nodes.get(r.nodeId), devs.get(r.deviationId), studies.get(r.studyId)
        out[r.id] = {
            "nodeLabel": _node_label(n) if n else None, "deviationText": d.deviation if d else None,
            "deviationRef": f"{n.nodeNo}.{d.rowNo}" if n and d else None, "riskLevel": d.riskLevel if d else None,
            "riskColor": d.riskColor if d else None, "studyNo": st.studyNo if st else None,
            "studyTitle": st.title if st else None, "studyStatus": st.status if st else None,
        }
    return out


async def _rec_out(db: AsyncSession, r: PhaRecommendation, warning: str | None = None) -> dict[str, Any]:
    users, _ = await _names(db, [r.ownerId], [])
    ctx = await _rec_ctx(db, [r])
    return {**_rec(r, users, ctx.get(r.id)), "warning": warning}


async def _push_to_capa(db: AsyncSession, r: PhaRecommendation) -> None:
    """Owner / due date edits follow onto a live CAPA (the CAPA does the chasing)."""
    from app.models.capa import Capa

    if not r.capaId:
        return
    try:
        capa = await db.get(Capa, r.capaId)
    except Exception:  # noqa: BLE001
        return
    if capa is None or capa.state in svc.CAPA_DONE or capa.state in svc.CAPA_DEAD:
        return
    if r.ownerId and capa.primaryOwnerUserId != r.ownerId:
        capa.primaryOwnerUserId = r.ownerId
    if r.dueDate is not None:
        capa.closureTargetDate = r.dueDate


@router.get("/recommendations")
async def list_recs(
    plantId: str | None = Query(None), studyId: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    overdueOnly: bool = Query(False), mine: bool = Query(False),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    stmt = select(PhaRecommendation).where(PhaRecommendation.isDeleted.is_(False)).where(_plant_where(PhaRecommendation.plantId, plants))
    if studyId:
        stmt = stmt.where(PhaRecommendation.studyId == studyId)
    if status_ == "OPEN":
        stmt = stmt.where(PhaRecommendation.status.in_(svc.REC_OPEN))
    elif status_:
        stmt = stmt.where(PhaRecommendation.status == status_)
    if overdueOnly:
        stmt = stmt.where(PhaRecommendation.status.in_(svc.REC_OPEN)).where(PhaRecommendation.dueDate < svc.utcnow())
    if mine:
        stmt = stmt.where(PhaRecommendation.ownerId == user.id)
    rows = list((await db.execute(stmt.order_by(PhaRecommendation.dueDate.asc().nulls_last(), PhaRecommendation.recNo).limit(1000))).scalars().all())
    await _sync(db, rows)
    users, _ = await _names(db, [r.ownerId for r in rows], [])
    ctx = await _rec_ctx(db, rows)
    return {"items": [_rec(r, users, ctx.get(r.id)) for r in rows], "total": len(rows)}


@router.post("/deviations/{did}/recommendations", status_code=201)
async def add_rec(did: str, body: RecIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_live(db, PhaDeviation, did)
    s = await _study_of(db, d.studyId)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    _check_choice(body.priority, svc.PRIORITIES, "priority")
    r = PhaRecommendation(
        studyId=s.id, plantId=s.plantId, nodeId=d.nodeId, deviationId=d.id, recNo=await _next_rec_no(db, s.id),
        text=body.text.strip(), ownerId=body.ownerId, dueDate=body.dueDate, priority=body.priority, status="OPEN", createdBy=user.id,
    )
    db.add(r)
    await db.flush()
    warning = None
    if body.raiseCapa:
        node = await _get_live(db, PhaNode, d.nodeId)
        _, warning = await svc.spawn_rec_capa(db, rec=r, study=s, deviation=d, node=node, actor_id=user.id)
    await db.commit()
    return await _rec_out(db, r, warning)


@router.patch("/recommendations/{rid}")
async def update_rec(rid: str, body: RecPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _get_live(db, PhaRecommendation, rid)
    s = await _study_of(db, r.studyId)
    await _require(db, user, UPDATE, s.plantId)
    if s.status == "CLOSED":
        raise HTTPException(409, "The study is closed.")
    if r.status not in svc.REC_OPEN:
        raise HTTPException(409, f"Reopen the recommendation to edit it (it is {r.status}).")
    data = body.model_dump(exclude_unset=True)
    if "text" in data:
        if not svc.is_editable(s.status):
            raise HTTPException(409, "The recommendation text is locked once the study is submitted.")
        r.text = (data["text"] or "").strip() or r.text
    if "priority" in data and data["priority"] is not None:
        _check_choice(data["priority"], svc.PRIORITIES, "priority")
        r.priority = data["priority"]
    if "ownerId" in data:
        r.ownerId = data["ownerId"]
    if "dueDate" in data:
        r.dueDate = data["dueDate"]
    await _push_to_capa(db, r)
    await db.commit()
    return await _rec_out(db, r)


@router.post("/recommendations/{rid}/status")
async def move_rec(rid: str, body: RecMoveIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _get_live(db, PhaRecommendation, rid)
    s = await _study_of(db, r.studyId)
    if r.ownerId != user.id:
        await _require(db, user, UPDATE, s.plantId)
    if s.status == "CLOSED":
        raise HTTPException(409, "The study is closed.")
    await _sync(db, [r])
    blockers = svc.rec_move_blockers(current=r.status, target=body.status, justification=body.justification,
                                     closure_note=body.closureNote, capa_state=r.capaState)
    if blockers:
        raise HTTPException(409 if "Cannot move" in blockers[0] or "CAPA" in " ".join(blockers) else 422, " ".join(blockers))
    r.status = body.status
    if body.status == "REJECTED":
        r.justification = body.justification.strip()
        r.closedAt, r.closedById = svc.utcnow(), user.id
        await svc.cancel_rec_capa(db, r)
    elif body.status == "CLOSED":
        r.closureNote = body.closureNote.strip()
        r.closedAt, r.closedById = svc.utcnow(), user.id
    else:
        r.closedAt = r.closedById = None
    await db.commit()
    return await _rec_out(db, r)


@router.post("/recommendations/{rid}/capa")
async def raise_rec_capa(rid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _get_live(db, PhaRecommendation, rid)
    s = await _study_of(db, r.studyId)
    await _require(db, user, UPDATE, s.plantId)
    if r.capaId:
        raise HTTPException(409, f"Already tracked by {r.capaNumber or 'a CAPA'}.")
    if r.status not in svc.REC_OPEN:
        raise HTTPException(409, f"Only an open recommendation can be raised as a CAPA (this one is {r.status}).")
    d = await _get_live(db, PhaDeviation, r.deviationId)
    node = await db.get(PhaNode, r.nodeId)
    capa, warning = await svc.spawn_rec_capa(db, rec=r, study=s, deviation=d, node=node, actor_id=user.id)
    if capa is None:
        await db.rollback()
        raise HTTPException(409, warning or "CAPA not raised.")
    await db.commit()
    return await _rec_out(db, r)


@router.delete("/recommendations/{rid}")
async def delete_rec(rid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _get_live(db, PhaRecommendation, rid)
    s = await _study_of(db, r.studyId)
    await _require(db, user, UPDATE, s.plantId)
    _ensure_editable(s)
    if r.capaId:
        raise HTTPException(409, f"Tracked by {r.capaNumber or 'a CAPA'}: reject it with a justification instead.")
    soft_delete(r, user.id, "Recommendation withdrawn by the study team")
    await db.commit()
    return {"ok": True}


# ── report + export ──────────────────────────────────────────────────────────
async def _report(db: AsyncSession, s: PhaStudy, user: User) -> dict[str, Any]:
    detail = await _study_detail(db, s, user)
    nodes = await _nodes(db, s.id)
    devs = await _devs(db, study_id=s.id)
    recs = await _recs(db, study_id=s.id)
    users, _ = await _names(db, [r.ownerId for r in recs], [])
    ctx = await _rec_ctx(db, recs)
    rec_out = [_rec(r, users, ctx.get(r.id)) for r in recs]
    by_dev: dict[str, list[dict[str, Any]]] = {}
    for r in rec_out:
        by_dev.setdefault(r["deviationId"], []).append(r)
    by_node: dict[str, list[dict[str, Any]]] = {}
    for d in devs:
        by_node.setdefault(d.nodeId, []).append(_dev(d, by_dev.get(d.id, [])))
    stats = {n["id"]: n for n in detail["nodes"]}
    role_label = {r: r.replace("_", " ").title() for r in svc.TEAM_ROLES}
    return {
        "study": {k: v for k, v in detail.items() if k not in ("team", "sessions", "nodes", "matrix", "perms")},
        "team": [{**m, "roleLabel": role_label.get(m["role"], m["role"])} for m in detail["team"]],
        "sessions": detail["sessions"], "matrix": detail["matrix"],
        "nodes": [{**_node(n), **{k: stats.get(n.id, {}).get(k) for k in ("rowCount", "ratedCount", "openRecs", "recCount", "highestRisk")},
                   "deviations": by_node.get(n.id, [])} for n in nodes],
        "recommendations": rec_out, "generatedAt": svc.utcnow().isoformat(),
    }


@router.get("/studies/{sid}/report")
async def study_report(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = await _study_of(db, sid)
    await _require(db, user, READ, s.plantId)
    return await _report(db, s, user)


@router.get("/studies/{sid}/export.xlsx")
async def export_xlsx(sid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    s = await _study_of(db, sid)
    await _require(db, user, READ, s.plantId)
    data = worksheet_xlsx(await _report(db, s, user))
    suffix = "" if s.status in ("APPROVED", "CLOSED") else "_DRAFT"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{s.studyNo}_{s.method}_worksheet{suffix}.xlsx"'},
    )
