"""Pre-Startup Safety Review router (JSL Hisar "MOC & PSSR"). Mounts at /api/pssr.

Standalone PSSR register: reviews for new facilities, modifications,
turnarounds and restarts; a checklist copied from a template; a punch list
(category A before start-up, B after); a sign-off matrix; and the go / no-go
gate (rules in app/services/pssr.py).

MOC link: POST /reviews with `mocId` (or POST /reviews/from-moc/{mocId}) starts
a PSSR prefilled from the change request. GET /by-moc/{mocId} is the read the
MOC closure gate can call. This router only READS ChangeRequest; it never
changes MOC state.

RBAC (granted by safeops_360/prisma/apply-pssr-psi-permissions.ts):
  PSSR.READ     view the register, reviews, punch list (plant-scoped)
  PSSR.MANAGE   create / edit reviews, answer items, manage punch items,
                submit, reopen, close; administer templates
  PSSR.SIGNOFF  record a discipline sign-off (APPROVE / REJECT)
The review leader and team members may answer items and work punch items
without PSSR.MANAGE; a punch owner may close their own punch item.

Evidence per checklist item rides the shared evidence layer
(/api/evidence/pssr_item/{itemId}; registry entry in services/evidence_registry.py).

Mounted ungated like ims / fire_safety: per-endpoint RBAC is the gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.pssr import PssrItem, PssrReview, PssrSignoff, PssrTemplate, PssrTemplateItem
from app.models.user import User
from app.services import pssr as svc
from app.services.access_scope import build_query_scope
from app.services.numbering import next_number
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names
from app.services.user_directory import resolve_user_directory

router = APIRouter(prefix="/api/pssr", tags=["pssr"])

READ, MANAGE, SIGNOFF = "PSSR.READ", "PSSR.MANAGE", "PSSR.SIGNOFF"
CAPA_SOURCE = "PSSR_PUNCH"          # seeded by apply-pssr-psi-ddl.ts
CAPA_SOURCE_FALLBACK = "MOC_ACTION"  # if the PSSR source type has not been seeded yet
# MOC statuses after which a change no longer needs a PSSR started (mirrors routers/moc.py).
MOC_TERMINAL = ("closed_successful", "closed_aborted", "closed_rejected", "withdrawn", "expired", "rolled_back")


# ── helpers ──────────────────────────────────────────────────────────────────
async def _allowed(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> bool:
    return (await can(db, user.id, perm, PermissionContext(plant_id=plant_id))).allowed


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _require_team(db: AsyncSession, user: User, r: PssrReview) -> None:
    """Leader and team members work their own review; anyone else needs PSSR.MANAGE."""
    if user.id == r.leaderId or user.id in (r.teamUserIds or []):
        return
    await _require(db, user, MANAGE, r.plantId)


async def _scoped_plants(db: AsyncSession, user: User, plant_id: str | None) -> list[str] | None:
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


def _check_choice(value: str | None, allowed: tuple[str, ...], field: str) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(422, f"{field} must be one of {', '.join(allowed)}")


async def _review(db: AsyncSession, rid: str) -> PssrReview:
    r = await db.get(PssrReview, rid)
    if r is None or r.isDeleted:
        raise HTTPException(404, "PSSR not found")
    return r


async def _item(db: AsyncSession, iid: str) -> tuple[PssrItem, PssrReview]:
    it = await db.get(PssrItem, iid)
    if it is None:
        raise HTTPException(404, "Checklist item not found")
    return it, await _review(db, it.reviewId)


async def _items(db: AsyncSession, rid: str) -> list[PssrItem]:
    return list((await db.execute(
        select(PssrItem).where(PssrItem.reviewId == rid)
        .order_by(PssrItem.sectionOrder, PssrItem.itemOrder)
        .execution_options(populate_existing=True)
    )).scalars().all())


async def _signoffs(db: AsyncSession, rid: str, current_only: bool = True) -> list[PssrSignoff]:
    stmt = select(PssrSignoff).where(PssrSignoff.reviewId == rid)
    if current_only:
        stmt = stmt.where(PssrSignoff.isCurrent.is_(True))
    return list((await db.execute(stmt.order_by(PssrSignoff.signedAt))).scalars().all())


def _item_state(i: PssrItem) -> svc.ItemState:
    return svc.ItemState(
        id=i.id, question=i.question, response=i.response, punchCategory=i.punchCategory,
        punchStatus=i.punchStatus, punchOwnerId=i.punchOwnerId, punchDueDate=i.punchDueDate,
    )


def _sign_state(s: PssrSignoff) -> svc.SignState:
    return svc.SignState(discipline=s.discipline, decision=s.decision, userId=s.userId, comment=s.comment, signedAt=s.signedAt)


async def _evaluate(db: AsyncSession, r: PssrReview) -> dict[str, Any]:
    """Run the gate and apply the status move it implies (caller commits)."""
    items = await _items(db, r.id)
    sigs = await _signoffs(db, r.id)
    g = svc.gate([_item_state(i) for i in items], [_sign_state(s) for s in sigs], r.requiredDisciplines or [])
    new_status = svc.status_after_evaluation(r.status, g)
    if new_status != r.status:
        r.status = new_status
        r.decidedAt = svc.utcnow()
        r.noGoReason = g["noGoReason"] if new_status == "NO_GO" else None
    return g


# ── serialisers ──────────────────────────────────────────────────────────────
def _review_row(r: PssrReview, users: dict[str, str], plants: dict[str, str], counts: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": r.id, "pssrNo": r.pssrNo, "plantId": r.plantId, "plantName": plants.get(r.plantId),
        "area": r.area, "unit": r.unit, "title": r.title, "scope": r.scope, "trigger": r.trigger,
        "mocId": r.mocId, "mocNumber": r.mocNumber, "phaStudyId": r.phaStudyId, "templateId": r.templateId,
        "plannedStartupDate": _iso(r.plannedStartupDate), "actualStartupDate": _iso(r.actualStartupDate),
        "leaderId": r.leaderId, "leaderName": users.get(r.leaderId),
        "teamUserIds": r.teamUserIds or [],
        "team": [{"id": u, "name": users.get(u, u)} for u in (r.teamUserIds or [])],
        "requiredDisciplines": r.requiredDisciplines or [],
        "status": r.status, "submittedAt": _iso(r.submittedAt), "decidedAt": _iso(r.decidedAt),
        "noGoReason": r.noGoReason, "closedAt": _iso(r.closedAt), "closedByName": users.get(r.closedById or ""),
        "reopenCount": r.reopenCount or 0, "lastReopenReason": r.lastReopenReason, "lastReopenedAt": _iso(r.lastReopenedAt),
        "createdAt": _iso(r.createdAt),
        "counts": counts or {},
    }


def _item_row(i: PssrItem, users: dict[str, str]) -> dict[str, Any]:
    st = _item_state(i)
    return {
        "id": i.id, "reviewId": i.reviewId, "section": i.section, "sectionOrder": i.sectionOrder,
        "itemOrder": i.itemOrder, "question": i.question, "guidance": i.guidance, "discipline": i.discipline,
        "defaultPunchCategory": i.defaultPunchCategory, "response": i.response, "remarks": i.remarks,
        "verifiedById": i.verifiedById, "verifiedByName": users.get(i.verifiedById or ""), "verifiedAt": _iso(i.verifiedAt),
        "isPunch": svc.is_punch(st), "punchCategory": i.punchCategory, "punchDescription": i.punchDescription,
        "punchStatus": i.punchStatus, "punchOwnerId": i.punchOwnerId, "punchOwnerName": users.get(i.punchOwnerId or ""),
        "punchDueDate": _iso(i.punchDueDate), "punchClosedAt": _iso(i.punchClosedAt),
        "punchClosedByName": users.get(i.punchClosedById or ""), "punchClosureNote": i.punchClosureNote,
        "punchComplete": svc.punch_complete(st), "punchOverdue": svc.punch_overdue(st),
        "capaId": i.capaId, "capaNumber": i.capaNumber,
    }


async def _detail(db: AsyncSession, r: PssrReview, user: User) -> dict[str, Any]:
    items = await _items(db, r.id)
    sigs = await _signoffs(db, r.id)
    history = await _signoffs(db, r.id, current_only=False)
    states = [_item_state(i) for i in items]
    g = svc.gate(states, [_sign_state(s) for s in sigs], r.requiredDisciplines or [])
    ids = [r.leaderId, r.closedById, *(r.teamUserIds or []),
           *[i.verifiedById for i in items], *[i.punchOwnerId for i in items], *[i.punchClosedById for i in items],
           *[s.userId for s in history]]
    users = {k: v.name for k, v in (await resolve_user_directory(db, ids)).items()}
    plants = await resolve_plant_names(db, [r.plantId])
    matrix = svc.signoff_matrix(r.requiredDisciplines or [], [_sign_state(s) for s in sigs])
    for row in matrix:
        row["userName"] = users.get(row["userId"] or "")
    can_manage = await _allowed(db, user, MANAGE, r.plantId)
    on_team = user.id == r.leaderId or user.id in (r.teamUserIds or [])
    return {
        **_review_row(r, users, plants, g["summary"]),
        "items": [_item_row(i, users) for i in items],
        "signoffs": matrix,
        "signoffHistory": [
            {"id": s.id, "discipline": s.discipline, "decision": s.decision, "userId": s.userId,
             "userName": users.get(s.userId), "comment": s.comment, "signedAt": _iso(s.signedAt), "isCurrent": s.isCurrent}
            for s in history
        ],
        "gate": g,
        "submitBlockers": svc.submit_blockers(r.status, states),
        "closeBlockers": svc.close_blockers(r.status, states),
        "permissions": {
            "canManage": can_manage, "onTeam": on_team, "canEdit": (can_manage or on_team),
            "canSignoff": await _allowed(db, user, SIGNOFF, r.plantId),
        },
    }


async def _counts(db: AsyncSession, review_ids: list[str]) -> dict[str, dict[str, int]]:
    if not review_ids:
        return {}
    rows = (await db.execute(
        select(PssrItem.reviewId, PssrItem.response, PssrItem.punchCategory, PssrItem.punchStatus)
        .where(PssrItem.reviewId.in_(review_ids))
    )).all()
    out: dict[str, dict[str, int]] = {}
    for rid, resp, cat, pst in rows:
        c = out.setdefault(rid, {"total": 0, "answered": 0, "punchAOpen": 0, "punchBOpen": 0})
        c["total"] += 1
        if resp in svc.RESPONSES:
            c["answered"] += 1
        if resp == "NO" and pst != "CLOSED":
            c["punchAOpen" if (cat or "A") == "A" else "punchBOpen"] += 1
    return out


# ── meta ─────────────────────────────────────────────────────────────────────
@router.get("/meta")
async def meta(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "triggers": svc.TRIGGERS, "statuses": svc.STATUSES, "responses": svc.RESPONSES,
        "punchCategories": svc.PUNCH_CATEGORIES, "punchStatuses": svc.PUNCH_STATUSES,
        "decisions": svc.DECISIONS, "disciplines": svc.DISCIPLINES,
        "disciplineLabels": svc.DISCIPLINE_LABEL, "mandatoryDisciplines": svc.MANDATORY_DISCIPLINES,
    }


# ── register ─────────────────────────────────────────────────────────────────
@router.get("/reviews")
async def list_reviews(
    plantId: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    trigger: str | None = Query(None), q: str | None = Query(None), mocId: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    stmt = select(PssrReview).where(PssrReview.isDeleted.is_(False)).where(_plant_where(PssrReview.plantId, plants))
    if status_ == "OPEN":
        stmt = stmt.where(PssrReview.status.in_(svc.OPEN_STATUSES))
    elif status_:
        stmt = stmt.where(PssrReview.status == status_)
    if trigger:
        stmt = stmt.where(PssrReview.trigger == trigger)
    if mocId:
        stmt = stmt.where(PssrReview.mocId == mocId)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(PssrReview.title.ilike(like), PssrReview.pssrNo.ilike(like), PssrReview.unit.ilike(like),
                              PssrReview.area.ilike(like), PssrReview.mocNumber.ilike(like)))
    rows = (await db.execute(stmt.order_by(PssrReview.createdAt.desc()).limit(500))).scalars().all()
    counts = await _counts(db, [r.id for r in rows])
    users = {k: v.name for k, v in (await resolve_user_directory(db, [r.leaderId for r in rows])).items()}
    pl = await resolve_plant_names(db, [r.plantId for r in rows])
    items = [_review_row(r, users, pl, counts.get(r.id)) for r in rows]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {"items": items, "total": len(items), "byStatus": by_status}


class ReviewIn(BaseModel):
    plantId: str | None = None
    title: str | None = Field(None, max_length=200)
    trigger: str | None = None
    area: str | None = None
    unit: str | None = None
    scope: str | None = Field(None, max_length=4000)
    mocId: str | None = None
    phaStudyId: str | None = None
    templateId: str | None = None
    plannedStartupDate: datetime | None = None
    leaderId: str | None = None
    teamUserIds: list[str] = []
    requiredDisciplines: list[str] = []
    allowAdditional: bool = False  # a second PSSR on the same MOC (phased start-up)


async def _load_moc(db: AsyncSession, moc_id: str):
    from app.models.moc import ChangeRequest  # read-only use of the MOC table

    cr = await db.get(ChangeRequest, moc_id)
    if cr is None:
        raise HTTPException(404, "Change request (MOC) not found")
    return cr


async def _template_rows(db: AsyncSession, template_id: str | None, plant_id: str) -> tuple[str | None, list[dict[str, Any]]]:
    """Checklist rows to copy into a new review: the named template, else the
    plant's default, else the global default, else the built-in default."""
    tpl: PssrTemplate | None = None
    if template_id:
        tpl = await db.get(PssrTemplate, template_id)
        if tpl is None or tpl.isDeleted or not tpl.isActive:
            raise HTTPException(422, "Template not found or inactive")
        if tpl.plantId and tpl.plantId != plant_id:
            raise HTTPException(422, "That template belongs to another plant")
    else:
        cands = (await db.execute(
            select(PssrTemplate).where(PssrTemplate.isDeleted.is_(False)).where(PssrTemplate.isActive.is_(True))
            .where(PssrTemplate.isDefault.is_(True)).where(or_(PssrTemplate.plantId == plant_id, PssrTemplate.plantId.is_(None)))
        )).scalars().all()
        cands = sorted(cands, key=lambda t: 0 if t.plantId == plant_id else 1)
        tpl = cands[0] if cands else None
    if tpl is None:
        return None, svc.default_template_rows()
    rows = (await db.execute(
        select(PssrTemplateItem).where(PssrTemplateItem.templateId == tpl.id).where(PssrTemplateItem.isDeleted.is_(False))
        .order_by(PssrTemplateItem.sectionOrder, PssrTemplateItem.itemOrder)
    )).scalars().all()
    if not rows:
        raise HTTPException(422, f"Template '{tpl.name}' has no items")
    return tpl.id, [
        {"id": t.id, "section": t.section, "sectionOrder": t.sectionOrder, "itemOrder": t.itemOrder,
         "question": t.question, "guidance": t.guidance, "discipline": t.discipline,
         "defaultPunchCategory": t.defaultPunchCategory}
        for t in rows
    ]


async def _create_review(db: AsyncSession, body: ReviewIn, user: User) -> PssrReview:
    moc = None
    if body.mocId:
        moc = await _load_moc(db, body.mocId)
        if body.plantId and body.plantId != moc.plantId:
            raise HTTPException(422, "The PSSR plant must match the MOC's plant")
        if not body.allowAdditional:
            existing = (await db.execute(
                select(PssrReview).where(PssrReview.mocId == moc.id).where(PssrReview.isDeleted.is_(False)).limit(1)
            )).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(409, f"{moc.number} already has {existing.pssrNo}. Open it, or set allowAdditional for a phased start-up.")
    plant_id = body.plantId or (moc.plantId if moc else None)
    if not plant_id:
        raise HTTPException(422, "plantId is required")
    await _require(db, user, MANAGE, plant_id)
    trigger = body.trigger or (svc.MOC_TRIGGER if moc else None)
    if not trigger:
        raise HTTPException(422, "trigger is required")
    _check_choice(trigger, svc.TRIGGERS, "trigger")
    title = (body.title or "").strip() or (f"PSSR for {moc.number}: {moc.title}" if moc else "")
    if len(title) < 3:
        raise HTTPException(422, "title is required (min 3 characters)")
    try:
        required = svc.normalise_disciplines(body.requiredDisciplines or list(svc.DISCIPLINES))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    tpl_id, rows = await _template_rows(db, body.templateId, plant_id)
    planned = body.plannedStartupDate or (
        (moc.proposedImplementationDate or moc.targetCompletionDate) if moc else None
    )
    now = svc.utcnow()
    no = await next_number(db, column=PssrReview.pssrNo, stem=f"PSSR-{now.year}-")
    leader = body.leaderId or user.id
    r = PssrReview(
        pssrNo=no, plantId=plant_id, area=body.area, unit=body.unit, title=title[:200],
        scope=body.scope if body.scope is not None else ((moc.description or "")[:4000] if moc else None),
        trigger=trigger, mocId=moc.id if moc else None, mocNumber=moc.number if moc else None,
        phaStudyId=(body.phaStudyId or None), templateId=tpl_id, plannedStartupDate=planned,
        leaderId=leader, teamUserIds=list(dict.fromkeys(u for u in body.teamUserIds if u and u != leader)),
        requiredDisciplines=required, status="DRAFT", createdBy=user.id,
    )
    db.add(r)
    await db.flush()
    for row in rows:
        db.add(PssrItem(
            reviewId=r.id, plantId=plant_id, templateItemId=row["id"], section=row["section"],
            sectionOrder=row["sectionOrder"], itemOrder=row["itemOrder"], question=row["question"],
            guidance=row["guidance"], discipline=row["discipline"], defaultPunchCategory=row["defaultPunchCategory"],
        ))
    return r


@router.post("/reviews", status_code=201)
async def create_review(body: ReviewIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _create_review(db, body, user)
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


@router.post("/reviews/from-moc/{moc_id}")
async def start_from_moc(moc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """'Start PSSR' on a MOC: returns the existing linked review, or creates one
    prefilled from the change request (leader = caller)."""
    moc = await _load_moc(db, moc_id)
    existing = (await db.execute(
        select(PssrReview).where(PssrReview.mocId == moc.id).where(PssrReview.isDeleted.is_(False))
        .order_by(PssrReview.createdAt.desc()).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        await _require(db, user, READ, existing.plantId)
        return {"created": False, "review": await _detail(db, existing, user)}
    r = await _create_review(db, ReviewIn(mocId=moc.id), user)
    await db.commit()
    await db.refresh(r)
    return {"created": True, "review": await _detail(db, r, user)}


@router.get("/reviews/{rid}")
async def get_review(rid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _review(db, rid)
    await _require(db, user, READ, r.plantId)
    return await _detail(db, r, user)


class ReviewPatch(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=200)
    area: str | None = None
    unit: str | None = None
    scope: str | None = Field(None, max_length=4000)
    phaStudyId: str | None = None
    plannedStartupDate: datetime | None = None
    leaderId: str | None = None
    teamUserIds: list[str] | None = None
    requiredDisciplines: list[str] | None = None


@router.patch("/reviews/{rid}")
async def update_review(rid: str, body: ReviewPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _review(db, rid)
    await _require(db, user, MANAGE, r.plantId)
    if r.status in ("APPROVED_GO", "CLOSED"):
        raise HTTPException(409, f"A {r.status} review cannot be edited")
    data = body.model_dump(exclude_unset=True)
    if "requiredDisciplines" in data:
        if r.status not in svc.EDITABLE_STATUSES:
            raise HTTPException(409, "The sign-off matrix can change only before the review is submitted")
        try:
            r.requiredDisciplines = svc.normalise_disciplines(data.pop("requiredDisciplines") or [])
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    if "teamUserIds" in data:
        r.teamUserIds = list(dict.fromkeys(u for u in (data.pop("teamUserIds") or []) if u))
    for k, v in data.items():
        if k == "leaderId" and not v:
            continue
        setattr(r, k, v.strip() if isinstance(v, str) and k == "title" else v)
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


class DeleteIn(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


@router.delete("/reviews/{rid}")
async def delete_review(rid: str, body: DeleteIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _review(db, rid)
    await _require(db, user, MANAGE, r.plantId)
    if r.status != "DRAFT":
        raise HTTPException(409, "Only a DRAFT review can be deleted; reopen or close it instead")
    r.isDeleted = True
    r.deletedAt = svc.utcnow()
    r.deletedBy = user.id
    r.deletionReason = body.reason
    await db.commit()
    return {"ok": True}


# ── checklist ────────────────────────────────────────────────────────────────
class ResponseIn(BaseModel):
    response: str | None = None  # YES | NO | NA | null (clear)
    remarks: str | None = Field(None, max_length=4000)


@router.put("/items/{iid}")
async def answer_item(iid: str, body: ResponseIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    it, r = await _item(db, iid)
    await _require_team(db, user, r)
    if r.status not in svc.EDITABLE_STATUSES:
        raise HTTPException(409, f"Responses are locked while the review is {r.status}. Reopen it to change answers.")
    _check_choice(body.response, svc.RESPONSES, "response")
    if body.response != it.response:
        if it.capaId and it.response == "NO":
            raise HTTPException(409, f"A CAPA ({it.capaNumber or it.capaId}) was raised for this punch item; close the punch item instead of changing the answer")
        for k, v in svc.punch_fields_for_response(body.response, it.defaultPunchCategory).items():
            setattr(it, k, v)
        if body.response == "NO" and not it.punchDescription:
            it.punchDescription = body.remarks or None
        it.response = body.response
        it.verifiedById = user.id if body.response else None
        it.verifiedAt = svc.utcnow() if body.response else None
    if "remarks" in body.model_fields_set:
        it.remarks = body.remarks
    if r.status == "DRAFT" and body.response:
        r.status = "IN_PROGRESS"
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


class PunchIn(BaseModel):
    punchCategory: str | None = None
    punchDescription: str | None = Field(None, max_length=4000)
    punchOwnerId: str | None = None
    punchDueDate: datetime | None = None


@router.patch("/items/{iid}/punch")
async def update_punch(iid: str, body: PunchIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    it, r = await _item(db, iid)
    await _require_team(db, user, r)
    if it.response != "NO":
        raise HTTPException(409, "Only a NO answer is a punch item")
    if r.status not in svc.PUNCH_WORK_STATUSES:
        raise HTTPException(409, f"Punch items are locked on a {r.status} review")
    data = body.model_dump(exclude_unset=True)
    if "punchCategory" in data and data["punchCategory"] != it.punchCategory:
        _check_choice(data["punchCategory"], svc.PUNCH_CATEGORIES, "punchCategory")
        if not svc.can_move_punch_category(r.status):
            raise HTTPException(409, "The punch category can change only before the review is submitted for sign-off")
    for k, v in data.items():
        setattr(it, k, v)
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


class PunchCloseIn(BaseModel):
    closureNote: str = Field(min_length=3, max_length=4000)


@router.post("/items/{iid}/punch/close")
async def close_punch(iid: str, body: PunchCloseIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    it, r = await _item(db, iid)
    if it.punchOwnerId != user.id:
        await _require_team(db, user, r)
    if it.response != "NO":
        raise HTTPException(409, "Only a NO answer is a punch item")
    if it.punchStatus == "CLOSED":
        raise HTTPException(409, "The punch item is already closed")
    if r.status not in svc.PUNCH_WORK_STATUSES:
        raise HTTPException(409, f"Punch items are locked on a {r.status} review")
    it.punchStatus = "CLOSED"
    it.punchClosedAt = svc.utcnow()
    it.punchClosedById = user.id
    it.punchClosureNote = body.closureNote.strip()
    await db.flush()
    await _evaluate(db, r)
    # After GO, closing the last B item closes the review.
    if r.status == "APPROVED_GO":
        states = [_item_state(i) for i in await _items(db, r.id)]
        if not svc.close_blockers(r.status, states):
            r.status = "CLOSED"
            r.closedAt = svc.utcnow()
            r.closedById = user.id
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


@router.post("/items/{iid}/punch/reopen")
async def reopen_punch(iid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    it, r = await _item(db, iid)
    await _require_team(db, user, r)
    if it.punchStatus != "CLOSED":
        raise HTTPException(409, "The punch item is not closed")
    if r.status in ("APPROVED_GO", "CLOSED"):
        raise HTTPException(409, f"Punch items cannot be reopened on a {r.status} review; raise a new review or a CAPA")
    it.punchStatus = "OPEN"
    it.punchClosedAt = None
    it.punchClosedById = None
    it.punchClosureNote = None
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


@router.post("/items/{iid}/capa")
async def raise_capa(iid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    it, r = await _item(db, iid)
    await _require(db, user, MANAGE, r.plantId)
    blockers = svc.capa_blockers(_item_state(it), it.capaId)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    from app.services.capa_spawn import spawn_capa

    kwargs = dict(
        plant_id=r.plantId, title=f"PSSR punch: {it.question}"[:200],
        problem=f"{r.pssrNo} ({r.title}), punch item B: {it.question}. {it.punchDescription or it.remarks or ''}".strip()[:1000],
        ref_id=it.id, ref_url=f"/process-safety/pssr/{r.id}", ref_summary=f"{r.pssrNo}: {it.section}",
        metadata={"pssrNo": r.pssrNo, "pssrReviewId": r.id, "pssrItemId": it.id, "punchCategory": it.punchCategory,
                  "mocId": r.mocId},
        severity="MODERATE", priority="MEDIUM", detected_method="PSSR_PUNCH",
        owner_id=it.punchOwnerId or r.leaderId, actor_id=user.id, due_days=30,
    )
    if it.punchDueDate:
        due = it.punchDueDate if it.punchDueDate.tzinfo else it.punchDueDate.replace(tzinfo=timezone.utc)
        kwargs["due_days"] = max(1, (due - svc.utcnow()).days)
    try:
        capa = await spawn_capa(db, source_code=CAPA_SOURCE, **kwargs)
    except ValueError:
        try:
            capa = await spawn_capa(db, source_code=CAPA_SOURCE_FALLBACK, **kwargs)
        except ValueError as e:
            raise HTTPException(409, f"CAPA engine not ready: {e}") from e
    await db.flush()
    it.capaId = capa.id
    it.capaNumber = capa.capaNumber
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


# ── lifecycle ────────────────────────────────────────────────────────────────
@router.post("/reviews/{rid}/submit")
async def submit_review(rid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _review(db, rid)
    await _require_team(db, user, r)
    items = await _items(db, r.id)
    blockers = svc.submit_blockers(r.status, [_item_state(i) for i in items])
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    r.status = "PENDING_APPROVAL"
    r.submittedAt = svc.utcnow()
    r.noGoReason = None
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


class SignoffIn(BaseModel):
    discipline: str
    decision: str
    comment: str | None = Field(None, max_length=2000)


@router.post("/reviews/{rid}/signoff")
async def sign_off(rid: str, body: SignoffIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _review(db, rid)
    await _require(db, user, SIGNOFF, r.plantId)
    discipline = (body.discipline or "").upper()
    blockers = svc.signoff_blockers(r.status, discipline, r.requiredDisciplines or [], body.decision, body.comment)
    if blockers:
        raise HTTPException(409 if r.status != "PENDING_APPROVAL" else 422, " ".join(blockers))
    for s in await _signoffs(db, r.id):
        if s.discipline == discipline:
            s.isCurrent = False
    await db.flush()  # retire first: one current decision per discipline (partial unique index)
    db.add(PssrSignoff(
        reviewId=r.id, discipline=discipline, userId=user.id, decision=body.decision,
        comment=(body.comment or "").strip() or None, signedAt=svc.utcnow(), isCurrent=True,
    ))
    await db.flush()
    await _evaluate(db, r)
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


class ReopenIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


@router.post("/reviews/{rid}/reopen")
async def reopen_review(rid: str, body: ReopenIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """PENDING_APPROVAL or NO_GO -> IN_PROGRESS. Every current sign-off is
    retired: the matrix signs the checklist as it stands, and that is changing."""
    r = await _review(db, rid)
    await _require(db, user, MANAGE, r.plantId)
    if not svc.can_reopen(r.status):
        raise HTTPException(409, f"A {r.status} review cannot be reopened")
    for s in await _signoffs(db, r.id):
        s.isCurrent = False
    prior = f" (was NO_GO: {r.noGoReason})" if r.noGoReason else f" (was {r.status})"
    r.status = "IN_PROGRESS"
    r.decidedAt = None
    r.submittedAt = None
    r.noGoReason = None
    r.reopenCount = (r.reopenCount or 0) + 1
    r.lastReopenReason = f"{body.reason.strip()}{prior}"[:2000]
    r.lastReopenedAt = svc.utcnow()
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


class CloseIn(BaseModel):
    actualStartupDate: datetime | None = None


@router.post("/reviews/{rid}/close")
async def close_review(rid: str, body: CloseIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _review(db, rid)
    await _require(db, user, MANAGE, r.plantId)
    blockers = svc.close_blockers(r.status, [_item_state(i) for i in await _items(db, r.id)])
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    r.status = "CLOSED"
    r.closedAt = svc.utcnow()
    r.closedById = user.id
    if body.actualStartupDate:
        r.actualStartupDate = body.actualStartupDate
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


class StartupIn(BaseModel):
    actualStartupDate: datetime


@router.post("/reviews/{rid}/startup")
async def record_startup(rid: str, body: StartupIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await _review(db, rid)
    await _require(db, user, MANAGE, r.plantId)
    if r.status not in ("APPROVED_GO", "CLOSED"):
        raise HTTPException(409, "Start-up can be recorded only after a GO decision")
    r.actualStartupDate = body.actualStartupDate
    await db.commit()
    await db.refresh(r)
    return await _detail(db, r, user)


# ── punch list across reviews ────────────────────────────────────────────────
@router.get("/punch")
async def punch_list(
    plantId: str | None = Query(None), category: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    mine: bool = Query(False), overdueOnly: bool = Query(False),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    stmt = (
        select(PssrItem, PssrReview).join(PssrReview, PssrReview.id == PssrItem.reviewId)
        .where(PssrReview.isDeleted.is_(False)).where(_plant_where(PssrReview.plantId, plants))
        .where(PssrItem.response == "NO")
    )
    if category:
        stmt = stmt.where(PssrItem.punchCategory == category)
    if status_:
        stmt = stmt.where(PssrItem.punchStatus == status_)
    if mine:
        stmt = stmt.where(PssrItem.punchOwnerId == user.id)
    if overdueOnly:
        stmt = stmt.where(PssrItem.punchStatus == "OPEN").where(PssrItem.punchDueDate < svc.utcnow())
    rows = (await db.execute(stmt.order_by(PssrItem.punchDueDate.asc()).limit(1000))).all()
    users = {k: v.name for k, v in (await resolve_user_directory(db, [i.punchOwnerId for i, _r in rows] + [i.punchClosedById for i, _r in rows])).items()}
    return {
        "items": [{**_item_row(i, users), "pssrNo": rv.pssrNo, "reviewTitle": rv.title, "reviewStatus": rv.status, "plantId": rv.plantId}
                  for i, rv in rows],
        "total": len(rows),
    }


# ── MOC link ─────────────────────────────────────────────────────────────────
@router.get("/by-moc/{moc_id}")
async def by_moc(moc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """PSSR state for a change request. `pssrComplete` is what a MOC closure gate
    should test: the latest linked review reached APPROVED_GO or CLOSED."""
    rows = (await db.execute(
        select(PssrReview).where(PssrReview.mocId == moc_id).where(PssrReview.isDeleted.is_(False))
        .order_by(PssrReview.createdAt.desc())
    )).scalars().all()
    plant_id = rows[0].plantId if rows else None
    if plant_id is None:
        from app.models.moc import ChangeRequest

        cr = await db.get(ChangeRequest, moc_id)
        if cr is None:
            raise HTTPException(404, "Change request (MOC) not found")
        plant_id = cr.plantId
    await _require(db, user, READ, plant_id)
    counts = await _counts(db, [r.id for r in rows])
    users = {k: v.name for k, v in (await resolve_user_directory(db, [r.leaderId for r in rows])).items()}
    pl = await resolve_plant_names(db, [plant_id])
    reviews = [_review_row(r, users, pl, counts.get(r.id)) for r in rows]
    latest = reviews[0] if reviews else None
    complete = bool(latest and latest["status"] in ("APPROVED_GO", "CLOSED"))
    outcome = None
    if latest and latest["status"] in ("APPROVED_GO", "CLOSED"):
        outcome = "GO"
    elif latest and latest["status"] == "NO_GO":
        outcome = "NO_GO"
    return {
        "mocId": moc_id, "reviews": reviews, "latest": latest, "pssrComplete": complete, "outcome": outcome,
        "message": (
            None if complete else
            "No PSSR has been started for this change." if not latest else
            f"{latest['pssrNo']} is NO_GO: {latest['noGoReason'] or 'see the review'}." if outcome == "NO_GO" else
            f"{latest['pssrNo']} is {latest['status']}; a GO decision is required before start-up."
        ),
    }


@router.get("/moc-candidates")
async def moc_candidates(
    plantId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Change requests that need (or may take) a PSSR, for the 'link a MOC' picker."""
    from app.models.moc import ChangeRequest

    plants = await _scoped_plants(db, user, plantId)
    stmt = (
        select(ChangeRequest).where(_plant_where(ChangeRequest.plantId, plants))
        .where(ChangeRequest.status.notin_(MOC_TERMINAL + ("draft",)))
        .order_by(ChangeRequest.pssrRequired.desc(), ChangeRequest.number.desc()).limit(300)
    )
    rows = (await db.execute(stmt)).scalars().all()
    linked = {}
    if rows:
        for rid, mid, no, st in (await db.execute(
            select(PssrReview.id, PssrReview.mocId, PssrReview.pssrNo, PssrReview.status)
            .where(PssrReview.mocId.in_([c.id for c in rows])).where(PssrReview.isDeleted.is_(False))
        )).all():
            linked[mid] = {"id": rid, "pssrNo": no, "status": st}
    return {"items": [
        {"id": c.id, "number": c.number, "title": c.title, "plantId": c.plantId, "status": c.status,
         "pssrRequired": bool(c.pssrRequired), "classification": c.classification,
         "proposedImplementationDate": _iso(c.proposedImplementationDate), "targetCompletionDate": _iso(c.targetCompletionDate),
         "linkedPssr": linked.get(c.id)}
        for c in rows
    ]}


# ── templates ────────────────────────────────────────────────────────────────
def _tpl_row(t: PssrTemplate, item_count: int, plants: dict[str, str]) -> dict[str, Any]:
    return {"id": t.id, "code": t.code, "name": t.name, "description": t.description, "plantId": t.plantId,
            "plantName": plants.get(t.plantId or ""), "isDefault": t.isDefault, "isActive": t.isActive,
            "itemCount": item_count, "updatedAt": _iso(t.updatedAt)}


def _tpl_item_row(t: PssrTemplateItem) -> dict[str, Any]:
    return {"id": t.id, "templateId": t.templateId, "section": t.section, "sectionOrder": t.sectionOrder,
            "itemOrder": t.itemOrder, "question": t.question, "guidance": t.guidance, "discipline": t.discipline,
            "defaultPunchCategory": t.defaultPunchCategory}


async def _tpl_items(db: AsyncSession, tid: str) -> list[PssrTemplateItem]:
    return list((await db.execute(
        select(PssrTemplateItem).where(PssrTemplateItem.templateId == tid).where(PssrTemplateItem.isDeleted.is_(False))
        .order_by(PssrTemplateItem.sectionOrder, PssrTemplateItem.itemOrder)
        .execution_options(populate_existing=True)
    )).scalars().all())


@router.get("/templates")
async def list_templates(
    plantId: str | None = Query(None), includeInactive: bool = Query(False),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, READ)
    stmt = select(PssrTemplate).where(PssrTemplate.isDeleted.is_(False))
    if plantId:
        stmt = stmt.where(or_(PssrTemplate.plantId == plantId, PssrTemplate.plantId.is_(None)))
    if not includeInactive:
        stmt = stmt.where(PssrTemplate.isActive.is_(True))
    rows = (await db.execute(stmt.order_by(PssrTemplate.isDefault.desc(), PssrTemplate.name))).scalars().all()
    counts: dict[str, int] = {}
    if rows:
        for (tid,) in (await db.execute(
            select(PssrTemplateItem.templateId).where(PssrTemplateItem.templateId.in_([t.id for t in rows]))
            .where(PssrTemplateItem.isDeleted.is_(False))
        )).all():
            counts[tid] = counts.get(tid, 0) + 1
    pl = await resolve_plant_names(db, [t.plantId for t in rows])
    return {
        "items": [_tpl_row(t, counts.get(t.id, 0), pl) for t in rows],
        # True when the DDL seed has not run: new reviews use the built-in default.
        "builtInDefaultInUse": not any(t.isDefault and t.isActive for t in rows),
        "builtInDefaultItemCount": len(svc.default_template_rows()),
    }


@router.get("/templates/{tid}")
async def get_template(tid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    t = await db.get(PssrTemplate, tid)
    if t is None or t.isDeleted:
        raise HTTPException(404, "Template not found")
    items = await _tpl_items(db, t.id)
    pl = await resolve_plant_names(db, [t.plantId])
    return {**_tpl_row(t, len(items), pl), "items": [_tpl_item_row(i) for i in items]}


class TemplateIn(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    code: str | None = Field(None, max_length=60)
    description: str | None = None
    plantId: str | None = None
    isDefault: bool = False
    cloneFromId: str | None = None  # "builtin" clones the built-in default


async def _clear_other_defaults(db: AsyncSession, keep: PssrTemplate) -> None:
    others = (await db.execute(
        select(PssrTemplate).where(PssrTemplate.id != keep.id).where(PssrTemplate.isDefault.is_(True))
    )).scalars().all()
    for o in others:
        if (o.plantId or None) == (keep.plantId or None):
            o.isDefault = False


@router.post("/templates", status_code=201)
async def create_template(body: TemplateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE, body.plantId)
    code = (body.code or body.name).strip().upper()
    code = "".join(ch if ch.isalnum() else "_" for ch in code)[:60]
    clash = (await db.execute(select(PssrTemplate.id).where(PssrTemplate.code == code).execution_options(include_deleted=True))).first()
    if clash:
        raise HTTPException(409, f"Template code '{code}' is already used")
    t = PssrTemplate(code=code, name=body.name.strip(), description=body.description, plantId=body.plantId,
                     isDefault=body.isDefault, isActive=True, createdBy=user.id)
    db.add(t)
    await db.flush()
    src: list[dict[str, Any]] = []
    if body.cloneFromId == "builtin":
        src = svc.default_template_rows()
    elif body.cloneFromId:
        src = [_tpl_item_row(i) for i in await _tpl_items(db, body.cloneFromId)]
    for row in src:
        db.add(PssrTemplateItem(templateId=t.id, section=row["section"], sectionOrder=row["sectionOrder"],
                                itemOrder=row["itemOrder"], question=row["question"], guidance=row["guidance"],
                                discipline=row["discipline"], defaultPunchCategory=row["defaultPunchCategory"]))
    if t.isDefault:
        await _clear_other_defaults(db, t)
    await db.commit()
    await db.refresh(t)
    return await get_template(t.id, user, db)


class TemplatePatch(BaseModel):
    name: str | None = Field(None, min_length=3, max_length=200)
    description: str | None = None
    isDefault: bool | None = None
    isActive: bool | None = None


@router.patch("/templates/{tid}")
async def update_template(tid: str, body: TemplatePatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    t = await db.get(PssrTemplate, tid)
    if t is None or t.isDeleted:
        raise HTTPException(404, "Template not found")
    await _require(db, user, MANAGE, t.plantId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    if t.isDefault and not t.isActive:
        raise HTTPException(422, "The default template must be active")
    if body.isDefault:
        await _clear_other_defaults(db, t)
    await db.commit()
    await db.refresh(t)
    return await get_template(t.id, user, db)


class TemplateItemIn(BaseModel):
    section: str = Field(min_length=2, max_length=200)
    sectionOrder: int | None = Field(None, ge=0, le=999)
    itemOrder: int | None = Field(None, ge=0, le=9999)
    question: str = Field(min_length=5, max_length=2000)
    guidance: str | None = Field(None, max_length=2000)
    discipline: str
    defaultPunchCategory: str = "A"


@router.post("/templates/{tid}/items", status_code=201)
async def add_template_item(tid: str, body: TemplateItemIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    t = await db.get(PssrTemplate, tid)
    if t is None or t.isDeleted:
        raise HTTPException(404, "Template not found")
    await _require(db, user, MANAGE, t.plantId)
    _check_choice(body.discipline, svc.DISCIPLINES, "discipline")
    _check_choice(body.defaultPunchCategory, svc.PUNCH_CATEGORIES, "defaultPunchCategory")
    existing = await _tpl_items(db, t.id)
    same = [i for i in existing if i.section == body.section.strip()]
    section_order = body.sectionOrder if body.sectionOrder is not None else (
        same[0].sectionOrder if same else (max((i.sectionOrder for i in existing), default=0) + 1)
    )
    item_order = body.itemOrder if body.itemOrder is not None else (max((i.itemOrder for i in same), default=0) + 1)
    db.add(PssrTemplateItem(templateId=t.id, section=body.section.strip(), sectionOrder=section_order, itemOrder=item_order,
                            question=body.question.strip(), guidance=body.guidance, discipline=body.discipline,
                            defaultPunchCategory=body.defaultPunchCategory))
    await db.commit()
    return await get_template(t.id, user, db)


class TemplateItemPatch(BaseModel):
    section: str | None = Field(None, min_length=2, max_length=200)
    sectionOrder: int | None = Field(None, ge=0, le=999)
    itemOrder: int | None = Field(None, ge=0, le=9999)
    question: str | None = Field(None, min_length=5, max_length=2000)
    guidance: str | None = Field(None, max_length=2000)
    discipline: str | None = None
    defaultPunchCategory: str | None = None


@router.patch("/template-items/{item_id}")
async def update_template_item(item_id: str, body: TemplateItemPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    ti = await db.get(PssrTemplateItem, item_id)
    if ti is None or ti.isDeleted:
        raise HTTPException(404, "Template item not found")
    t = await db.get(PssrTemplate, ti.templateId)
    await _require(db, user, MANAGE, t.plantId if t else None)
    _check_choice(body.discipline, svc.DISCIPLINES, "discipline")
    _check_choice(body.defaultPunchCategory, svc.PUNCH_CATEGORIES, "defaultPunchCategory")
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is not None or k == "guidance":
            setattr(ti, k, v.strip() if isinstance(v, str) and k in ("section", "question") else v)
    await db.commit()
    return await get_template(ti.templateId, user, db)


@router.delete("/template-items/{item_id}")
async def delete_template_item(item_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    ti = await db.get(PssrTemplateItem, item_id)
    if ti is None or ti.isDeleted:
        raise HTTPException(404, "Template item not found")
    t = await db.get(PssrTemplate, ti.templateId)
    await _require(db, user, MANAGE, t.plantId if t else None)
    ti.isDeleted = True  # reviews keep their copied checklist; this only affects new reviews
    await db.commit()
    return await get_template(ti.templateId, user, db)
