"""Contractor Safety Management router, part 1. Mounts at /api/csm.

Covers: meta, hub, expiry list, questionnaire templates, contractor register
(registration + Contractor 360 + worker onboarding), prequalification
applications (internal entry, review, override, recommendation, plant-head
decision, portal invitation) and contractor documents.
Work orders, evaluations and sanctions are in routers/csm_ops.py; the
contractor-facing token endpoints in routers/csm_portal.py.

RBAC (granted by safeops_360/prisma/apply-csm-permissions.ts):
  CSM.READ      view everything (plant-scoped via build_query_scope)
  CSM.MANAGE    register contractors, enter / review prequalification, issue
                portal links, upload documents, work orders and rosters,
                request sanctions. Template admin needs CSM.MANAGE at ALL_PLANTS.
  CSM.EVALUATE  create and submit performance evaluations
  CSM.APPROVE   plant-head / corporate-HSE decisions: prequalification
                approval, evaluation sign-off, sanction approval
A contractor company is global (ContractorCompany); a plant-scoped user sees a
contractor once it has a prequalification or work order at one of their plants.

Mounted ungated, like ims / fire_safety: a new licence module code would 403
under the signed licence that predates it. Per-endpoint RBAC is the gate.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.csm import CsmDocument, CsmPrequalification, CsmQuestionnaireTemplate, CsmWorkOrder, CsmEvaluation
from app.models.epc import ContractorCompany, ContractorWorker
from app.models.user import User
from app.services import csm as svc
from app.services import csm_portal as portal
from app.services.access_scope import build_query_scope
from app.services.numbering import next_number
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names
from app.services.user_directory import resolve_user_directory

router = APIRouter(prefix="/api/csm", tags=["csm"])

READ, MANAGE, EVALUATE, APPROVE = "CSM.READ", "CSM.MANAGE", "CSM.EVALUATE", "CSM.APPROVE"


# ── shared helpers (also used by csm_ops) ────────────────────────────────────
async def require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None, *, all_plants: bool = False) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")
    if all_plants and res.matched_scope != "ALL_PLANTS":
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{perm} at all plants (corporate) is required")


async def scoped_plants(db: AsyncSession, user: User, plant_id: str | None) -> list[str] | None:
    """Plant filter for reads: None = all plants. A requested plant outside the
    caller's scope is refused, not silently emptied."""
    await require(db, user, READ)
    scope = await build_query_scope(db, user.id, READ)
    if plant_id:
        if not scope.allows_plant(plant_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant is outside your access scope")
        return [plant_id]
    return None if scope.all_plants else scope.plant_ids


def plant_where(col, plants: list[str] | None):
    return True if plants is None else col.in_(plants or ["__none__"])


def iso(d: datetime | None) -> str | None:
    return svc.aware(d).isoformat() if d else None


async def names(db: AsyncSession, user_ids, plant_ids) -> tuple[dict[str, str], dict[str, str]]:
    users = await resolve_user_directory(db, user_ids)
    plants = await resolve_plant_names(db, plant_ids)
    return {k: v.name for k, v in users.items()}, plants


async def get_live(db: AsyncSession, model, rid: str):
    row = await db.get(model, rid)
    if row is None or getattr(row, "isDeleted", False):
        raise HTTPException(404, f"{model.__name__.replace('Csm', '')} not found")
    return row


def check_choice(value: str | None, allowed: tuple[str, ...], field: str) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(422, f"{field} must be one of {', '.join(allowed)}")


def conflict(blockers: list[str]) -> None:
    if blockers:
        raise HTTPException(409, " ".join(blockers))


async def company_plants(db: AsyncSession, company_id: str) -> set[str]:
    a = (await db.execute(
        select(CsmPrequalification.plantId).where(CsmPrequalification.contractorCompanyId == company_id)
        .where(CsmPrequalification.isDeleted.is_(False))
    )).scalars().all()
    b = (await db.execute(
        select(CsmWorkOrder.plantId).where(CsmWorkOrder.contractorCompanyId == company_id)
        .where(CsmWorkOrder.isDeleted.is_(False))
    )).scalars().all()
    return set(a) | set(b)


async def visible_company(db: AsyncSession, user: User, company_id: str) -> tuple[ContractorCompany, list[str] | None]:
    """The company, and the plant filter to apply to its data."""
    plants = await scoped_plants(db, user, None)
    c = await db.get(ContractorCompany, company_id)
    if c is None:
        raise HTTPException(404, "Contractor not found")
    if plants is not None and not (await company_plants(db, company_id)) & set(plants):
        raise HTTPException(403, "This contractor has no prequalification or work order at your plants")
    return c, plants


def company_dict(c: ContractorCompany, now: datetime | None = None) -> dict[str, Any]:
    return {
        "id": c.id, "name": c.name, "code": c.code, "tradeName": c.tradeName, "status": c.status or "ACTIVE",
        "score": c.score, "panNumber": c.panNumber, "gstNumber": c.gstNumber,
        "registrationNumber": c.registrationNumber, "tradeCategories": c.tradeCategories or [],
        "sizeCategory": c.sizeCategory, "contactPerson": c.contactPerson, "contactEmail": c.contactEmail,
        "contactPhone": c.contactPhone, "representativeName": c.representativeName,
        "representativePhone": c.representativePhone, "representativeEmail": c.representativeEmail,
        "safetyOfficerName": c.safetyOfficerName, "safetyOfficerPhone": c.safetyOfficerPhone,
        "prequalificationStatus": c.prequalificationStatus, "prequalificationScore": c.prequalificationScore,
        "prequalificationValidUntil": iso(c.prequalificationValidUntil),
        "qualification": svc.effective_qualification(c.prequalificationStatus, c.prequalificationValidUntil, now),
        "blockReason": svc.company_block_reason(c.status),
        "suspensionHistory": c.suspensionHistory or [],
    }


def add_history(row, user: User, frm: str | None, to: str, note: str | None = None) -> None:
    row.history = [*(row.history or []), {"at": svc.utcnow().isoformat(), "byId": user.id,
                                          "byName": getattr(user, "name", None), "from": frm, "to": to, "note": note}]


# ── meta + hub ───────────────────────────────────────────────────────────────
@router.get("/meta")
async def meta(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "answerTypes": svc.ANSWER_TYPES, "prequalStatuses": svc.PQ_STATUSES, "decisions": svc.PQ_DECISIONS,
        "docTypes": svc.DOC_TYPES, "companyStatuses": svc.COMPANY_STATUSES, "workTypes": svc.WORK_TYPES,
        "highRiskActivities": svc.HIGH_RISK_ACTIVITIES,
        "safetyChecklist": [{"key": k, "label": l} for k, l in svc.SAFETY_CHECKLIST],
        "supervisorRatioMax": svc.SUPERVISOR_RATIO_MAX, "workOrderStatuses": svc.WO_STATUSES,
        "evaluationTypes": svc.EVAL_TYPES, "evaluationStatuses": svc.EVAL_STATUSES,
        "kpis": svc.KPI_DEFS, "manualCriteria": svc.MANUAL_CRITERIA,
        "ratingBands": [{"min": m, "band": b} for m, b in svc.RATING_BANDS],
        "sanctionActions": svc.SANCTION_ACTIONS, "sanctionReasons": svc.SANCTION_REASONS,
        "templateStatuses": svc.TEMPLATE_STATUSES,
    }


@router.get("/dashboard")
async def dashboard(plantId: str | None = Query(None), user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plants = await scoped_plants(db, user, plantId)
    data = await svc.hub(db, plant_ids=plants)
    exp = await svc.expiring(db, plant_ids=plants, company_ids=await _scoped_company_ids(db, plants))
    data["expiring"] = {
        "prequalExpired": sum(1 for p in exp["prequalifications"] if p["state"] == "EXPIRED"),
        "prequalDue": sum(1 for p in exp["prequalifications"] if p["state"] == "DUE"),
        "docsExpired": sum(1 for d in exp["documents"] if d["state"] == "EXPIRED"),
        "docsDue": sum(1 for d in exp["documents"] if d["state"] == "DUE"),
    }
    return {**data, "plantId": plantId}


async def _scoped_company_ids(db: AsyncSession, plants: list[str] | None) -> list[str] | None:
    if plants is None:
        return None
    a = (await db.execute(select(CsmPrequalification.contractorCompanyId).where(CsmPrequalification.plantId.in_(plants or ["__none__"]))
                          .where(CsmPrequalification.isDeleted.is_(False)))).scalars().all()
    b = (await db.execute(select(CsmWorkOrder.contractorCompanyId).where(CsmWorkOrder.plantId.in_(plants or ["__none__"]))
                          .where(CsmWorkOrder.isDeleted.is_(False)))).scalars().all()
    return sorted(set(a) | set(b))


@router.get("/expiring")
async def expiring(plantId: str | None = Query(None), days: int = Query(30, ge=1, le=180),
                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plants = await scoped_plants(db, user, plantId)
    data = await svc.expiring(db, plant_ids=plants, company_ids=await _scoped_company_ids(db, plants), days=days)
    _, pl = await names(db, [], [p["plantId"] for p in data["prequalifications"]])
    for p in data["prequalifications"]:
        p["plantName"] = pl.get(p["plantId"])
    return data


# ── questionnaire templates ──────────────────────────────────────────────────
class TemplateIn(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    code: str | None = Field(None, max_length=60)
    description: str | None = None
    sections: list[dict] = []
    passScore: float = Field(70, ge=0, le=100)
    conditionalScore: float = Field(55, ge=0, le=100)
    validityMonths: int = Field(12, ge=1, le=60)
    conditionalValidityMonths: int = Field(6, ge=1, le=60)
    cloneFromId: str | None = None


def _template(t: CsmQuestionnaireTemplate, full: bool = True) -> dict[str, Any]:
    qn = sum(len(s.get("questions") or []) for s in (t.sections or []))
    out = {
        "id": t.id, "code": t.code, "version": t.version, "name": t.name, "description": t.description,
        "status": t.status, "passScore": t.passScore, "conditionalScore": t.conditionalScore,
        "validityMonths": t.validityMonths, "conditionalValidityMonths": t.conditionalValidityMonths,
        "sectionCount": len(t.sections or []), "questionCount": qn, "activatedAt": iso(t.activatedAt),
        "updatedAt": iso(t.updatedAt),
    }
    if full:
        out["sections"] = t.sections or []
    return out


def _template_checks(body: TemplateIn) -> None:
    problems = svc.template_problems(body.sections)
    if body.conditionalScore > body.passScore:
        problems.append("The conditional score cannot be higher than the pass score.")
    if problems:
        raise HTTPException(422, " ".join(problems))


@router.get("/templates")
async def list_templates(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, READ)
    rows = (await db.execute(
        select(CsmQuestionnaireTemplate).where(CsmQuestionnaireTemplate.isDeleted.is_(False))
        .order_by(CsmQuestionnaireTemplate.code, CsmQuestionnaireTemplate.version.desc())
    )).scalars().all()
    return {"items": [_template(t, full=False) for t in rows], "total": len(rows)}


@router.get("/templates/{tid}")
async def get_template(tid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, READ)
    return _template(await get_live(db, CsmQuestionnaireTemplate, tid))


@router.post("/templates", status_code=201)
async def create_template(body: TemplateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE, all_plants=True)
    if body.cloneFromId:
        src = await get_live(db, CsmQuestionnaireTemplate, body.cloneFromId)
        code, sections = src.code, src.sections
        pass_s, cond_s, vm, cvm = src.passScore, src.conditionalScore, src.validityMonths, src.conditionalValidityMonths
        desc = body.description or src.description
    else:
        _template_checks(body)
        code = re.sub(r"[^A-Z0-9_]", "_", (body.code or body.name).upper())[:60]
        sections, pass_s, cond_s = body.sections, body.passScore, body.conditionalScore
        vm, cvm, desc = body.validityMonths, body.conditionalValidityMonths, body.description
    highest = await db.scalar(select(func.max(CsmQuestionnaireTemplate.version)).where(CsmQuestionnaireTemplate.code == code))
    t = CsmQuestionnaireTemplate(
        code=code, version=int(highest or 0) + 1, name=body.name.strip(), description=desc, status="DRAFT",
        sections=sections, passScore=pass_s, conditionalScore=cond_s, validityMonths=vm,
        conditionalValidityMonths=cvm, createdBy=user.id,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _template(t)


@router.put("/templates/{tid}")
async def update_template(tid: str, body: TemplateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE, all_plants=True)
    t = await get_live(db, CsmQuestionnaireTemplate, tid)
    if t.status != "DRAFT":
        raise HTTPException(409, "Only a draft template can be edited. Clone the active one to make a new version.")
    _template_checks(body)
    t.name, t.description, t.sections = body.name.strip(), body.description, body.sections
    t.passScore, t.conditionalScore = body.passScore, body.conditionalScore
    t.validityMonths, t.conditionalValidityMonths = body.validityMonths, body.conditionalValidityMonths
    await db.commit()
    await db.refresh(t)
    return _template(t)


@router.post("/templates/{tid}/activate")
async def activate_template(tid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE, all_plants=True)
    t = await get_live(db, CsmQuestionnaireTemplate, tid)
    if t.status != "DRAFT":
        raise HTTPException(409, f"Template is already {t.status.lower()}")
    problems = svc.template_problems(t.sections or [])
    if problems:
        raise HTTPException(409, " ".join(problems))
    for prior in (await db.execute(
        select(CsmQuestionnaireTemplate).where(CsmQuestionnaireTemplate.code == t.code)
        .where(CsmQuestionnaireTemplate.status == "ACTIVE")
    )).scalars().all():
        prior.status = "RETIRED"
    t.status = "ACTIVE"
    t.activatedAt = svc.utcnow()
    await db.commit()
    await db.refresh(t)
    return _template(t)


@router.post("/templates/{tid}/retire")
async def retire_template(tid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE, all_plants=True)
    t = await get_live(db, CsmQuestionnaireTemplate, tid)
    if t.status != "ACTIVE":
        raise HTTPException(409, "Only an active template can be retired")
    t.status = "RETIRED"
    await db.commit()
    await db.refresh(t)
    return _template(t)


async def active_template(db: AsyncSession, template_id: str | None = None) -> CsmQuestionnaireTemplate:
    if template_id:
        t = await get_live(db, CsmQuestionnaireTemplate, template_id)
        if t.status != "ACTIVE":
            raise HTTPException(409, "That questionnaire template is not active")
        return t
    t = (await db.execute(
        select(CsmQuestionnaireTemplate).where(CsmQuestionnaireTemplate.isDeleted.is_(False))
        .where(CsmQuestionnaireTemplate.status == "ACTIVE")
        .order_by(CsmQuestionnaireTemplate.activatedAt.desc())
    )).scalars().first()
    if t is None:
        raise HTTPException(409, "No active prequalification template. Activate one under CSM > Templates.")
    return t


def snapshot_of(t: CsmQuestionnaireTemplate) -> dict[str, Any]:
    return {"id": t.id, "code": t.code, "version": t.version, "name": t.name, "sections": t.sections or [],
            "passScore": t.passScore, "conditionalScore": t.conditionalScore, "validityMonths": t.validityMonths,
            "conditionalValidityMonths": t.conditionalValidityMonths}


# ── contractors ──────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    plantId: str
    tradeName: str | None = None
    registrationNumber: str | None = None
    panNumber: str | None = Field(None, max_length=20)
    gstNumber: str | None = Field(None, max_length=20)
    tradeCategories: list[str] = []
    sizeCategory: str | None = None
    contactPerson: str | None = None
    contactEmail: str | None = None
    contactPhone: str | None = None
    safetyOfficerName: str | None = None
    safetyOfficerPhone: str | None = None
    templateId: str | None = None
    sendInvite: bool = False
    inviteTtlDays: int = Field(30, ge=1, le=90)


@router.get("/contractors")
async def list_contractors(
    plantId: str | None = Query(None), q: str | None = Query(None), qualification: str | None = Query(None),
    companyStatus: str | None = Query(None, alias="status"),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await scoped_plants(db, user, plantId)
    stmt = select(ContractorCompany)
    ids = await _scoped_company_ids(db, plants)
    if ids is not None:
        stmt = stmt.where(ContractorCompany.id.in_(ids or ["__none__"]))
    if q:
        stmt = stmt.where(or_(ContractorCompany.name.ilike(f"%{q}%"), ContractorCompany.code.ilike(f"%{q}%")))
    if companyStatus:
        stmt = stmt.where(func.upper(ContractorCompany.status) == companyStatus.upper())
    rows = (await db.execute(stmt.order_by(ContractorCompany.name).limit(500))).scalars().all()
    now = svc.utcnow()
    items = [company_dict(c, now) for c in rows]
    if qualification:
        items = [i for i in items if i["qualification"] == qualification]
    cids = [i["id"] for i in items] or ["__none__"]
    wo = dict((await db.execute(
        select(CsmWorkOrder.contractorCompanyId, func.count()).where(CsmWorkOrder.contractorCompanyId.in_(cids))
        .where(CsmWorkOrder.status == "ACTIVE").where(CsmWorkOrder.isDeleted.is_(False))
        .where(plant_where(CsmWorkOrder.plantId, plants)).group_by(CsmWorkOrder.contractorCompanyId)
    )).all())
    last_eval: dict[str, CsmEvaluation] = {}
    for e in (await db.execute(
        select(CsmEvaluation).where(CsmEvaluation.contractorCompanyId.in_(cids)).where(CsmEvaluation.isDeleted.is_(False))
        .where(CsmEvaluation.status == "SIGNED_OFF").where(plant_where(CsmEvaluation.plantId, plants))
        .order_by(CsmEvaluation.periodEnd.asc())
    )).scalars().all():
        last_eval[e.contractorCompanyId] = e
    open_pq = dict((await db.execute(
        select(CsmPrequalification.contractorCompanyId, CsmPrequalification.status)
        .where(CsmPrequalification.contractorCompanyId.in_(cids)).where(CsmPrequalification.isDeleted.is_(False))
        .where(CsmPrequalification.status.in_(svc.PQ_OPEN))
    )).all())
    for i in items:
        i["activeWorkOrders"] = int(wo.get(i["id"], 0))
        e = last_eval.get(i["id"])
        i["lastEvaluation"] = {"finalScore": e.finalScore, "ratingBand": e.ratingBand, "periodEnd": iso(e.periodEnd)} if e else None
        i["openApplicationStatus"] = open_pq.get(i["id"])
    return {"items": items, "total": len(items)}


async def _new_application(db: AsyncSession, user: User, *, company: ContractorCompany, plant_id: str,
                           template: CsmQuestionnaireTemplate, renewal_of: str | None = None) -> CsmPrequalification:
    open_one = (await db.execute(
        select(CsmPrequalification).where(CsmPrequalification.contractorCompanyId == company.id)
        .where(CsmPrequalification.plantId == plant_id).where(CsmPrequalification.isDeleted.is_(False))
        .where(CsmPrequalification.status.in_(svc.PQ_OPEN))
    )).scalars().first()
    if open_one is not None:
        raise HTTPException(409, f"{open_one.applicationNo} is already in progress for this contractor at this plant")
    no = await next_number(db, column=CsmPrequalification.applicationNo, stem=f"PQ-{svc.utcnow().year}-")
    pq = CsmPrequalification(
        applicationNo=no, contractorCompanyId=company.id, plantId=plant_id, templateId=template.id,
        templateSnapshot=snapshot_of(template), status="DRAFT", answers={}, overrides={}, criticalFailures=[],
        isRenewal=renewal_of is not None, previousPrequalificationId=renewal_of, history=[], createdBy=user.id,
        isCurrent=False,
    )
    add_history(pq, user, None, "DRAFT", "Renewal started" if renewal_of else "Application created")
    db.add(pq)
    await db.flush()
    return pq


@router.post("/contractors", status_code=201)
async def register_contractor(body: RegisterIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Register a new plant contractor and open its first prequalification."""
    await require(db, user, MANAGE, body.plantId)
    dup = (await db.execute(select(ContractorCompany).where(func.lower(ContractorCompany.name) == body.name.strip().lower()))).scalars().first()
    if dup is not None:
        raise HTTPException(409, f"'{dup.name}' is already registered ({dup.code}). Open it and start a prequalification instead.")
    template = await active_template(db, body.templateId)
    code = await next_number(db, column=ContractorCompany.code, stem="CC-")
    c = ContractorCompany(
        name=body.name.strip(), code=code, tradeName=body.tradeName, registrationNumber=body.registrationNumber,
        panNumber=(body.panNumber or None) and body.panNumber.upper(), gstNumber=(body.gstNumber or None) and body.gstNumber.upper(),
        tradeCategories=body.tradeCategories, sizeCategory=body.sizeCategory, contactPerson=body.contactPerson,
        contactEmail=body.contactEmail, contactPhone=body.contactPhone, representativeName=body.contactPerson,
        representativePhone=body.contactPhone, representativeEmail=body.contactEmail,
        safetyOfficerName=body.safetyOfficerName, safetyOfficerPhone=body.safetyOfficerPhone,
        status="ACTIVE", score=100, prequalificationStatus="not_applied", complianceDocuments=[], suspensionHistory=[],
    )
    db.add(c)
    await db.flush()
    pq = await _new_application(db, user, company=c, plant_id=body.plantId, template=template)
    invite = None
    if body.sendInvite:
        issued = await portal.issue_token(db, prequalification_id=pq.id, company_id=c.id, contact_email=body.contactEmail,
                                          contact_name=body.contactPerson, actor_id=user.id, ttl_days=body.inviteTtlDays)
        invite = {"portalPath": issued.portalPath, "expiresAt": iso(issued.token.expiresAt)}
    await db.commit()
    await db.refresh(c)
    return {"contractor": company_dict(c), "prequalificationId": pq.id, "applicationNo": pq.applicationNo, "invite": invite}


@router.get("/contractors/{cid}")
async def contractor_360(cid: str, days: int = Query(365, ge=30, le=1095), user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    c, plants = await visible_company(db, user, cid)
    data = await svc.contractor_360(db, company_id=cid, plant_ids=plants, days=days)
    pqs = (await db.execute(
        select(CsmPrequalification).where(CsmPrequalification.contractorCompanyId == cid)
        .where(CsmPrequalification.isDeleted.is_(False)).where(plant_where(CsmPrequalification.plantId, plants))
        .order_by(CsmPrequalification.createdAt.desc()).limit(20)
    )).scalars().all()
    docs = await _documents(db, cid)
    plant_ids = {p.plantId for p in pqs} | {w["plantId"] for w in data["workOrders"]["items"]} | {s["plantId"] for s in data["sanctions"]}
    users, pl = await names(db, [s["requestedById"] for s in data["sanctions"]] + [s["decidedById"] for s in data["sanctions"]], plant_ids)
    for w in data["workOrders"]["items"]:
        w["plantName"] = pl.get(w["plantId"])
    for s in data["sanctions"]:
        s["plantName"] = pl.get(s["plantId"])
        s["requestedByName"] = users.get(s["requestedById"])
        s["decidedByName"] = users.get(s["decidedById"] or "")
    return {
        "contractor": company_dict(c), **data,
        "prequalifications": [_pq_row(p, pl) for p in pqs],
        "documents": docs,
    }


@router.get("/contractors/{cid}/workers")
async def contractor_workers(cid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await visible_company(db, user, cid)
    rows = (await db.execute(
        select(ContractorWorker).where(ContractorWorker.contractorCompanyId == cid).order_by(ContractorWorker.fullName)
    )).scalars().all()
    return {"items": [worker_dict(w) for w in rows], "total": len(rows)}


def worker_dict(w: ContractorWorker) -> dict[str, Any]:
    return {
        "id": w.id, "workerCode": w.workerCode, "fullName": w.fullName, "primaryTrade": w.primaryTrade,
        "mobileNumber": w.mobileNumber, "overallStatus": w.overallStatus,
        "rosterStatus": getattr(w, "rosterStatus", "active") or "active",
        "currentMedicalValidUntil": iso(w.currentMedicalValidUntil),
        "medicalState": svc.expiry_state(w.currentMedicalValidUntil) or "MISSING",
    }


class WorkerIn(BaseModel):
    fullName: str = Field(min_length=2, max_length=120)
    primaryTrade: str | None = None
    mobileNumber: str | None = None
    aadhaarLast4: str | None = Field(None, pattern=r"^\d{4}$")
    medicalValidUntil: datetime | None = None


class MedicalIn(BaseModel):
    validUntil: datetime
    examinedOn: datetime | None = None
    doctorName: str | None = None
    remarks: str | None = None


@router.post("/contractors/{cid}/workers", status_code=201)
async def register_worker(cid: str, body: WorkerIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Quick onboarding of a ContractorWorker for plant work (same table the
    EPC worker register uses)."""
    await require(db, user, MANAGE)
    c = await db.get(ContractorCompany, cid)
    if c is None:
        raise HTTPException(404, "Contractor not found")
    if body.aadhaarLast4:
        dup = (await db.execute(select(ContractorWorker).where(ContractorWorker.contractorCompanyId == cid)
                                .where(ContractorWorker.aadhaarLast4 == body.aadhaarLast4))).scalars().first()
        if dup:
            raise HTTPException(409, f"A worker with Aadhaar ending {body.aadhaarLast4} is already registered ({dup.workerCode})")
    code = await next_number(db, column=ContractorWorker.workerCode, stem=f"CW-{svc.utcnow().year}-")
    w = ContractorWorker(
        contractorCompanyId=cid, workerCode=code, fullName=body.fullName.strip(), primaryTrade=body.primaryTrade,
        mobileNumber=body.mobileNumber, aadhaarLast4=body.aadhaarLast4, secondaryTrades=[],
        medicalFitnessRecords=[], currentMedicalValidUntil=body.medicalValidUntil, overallStatus="active",
        rosterStatus="active", trainingCertificates=[], competencyRecords=[], ppeIssuances=[], createdById=user.id,
    )
    if body.medicalValidUntil:
        w.medicalFitnessRecords = [{"validUntil": body.medicalValidUntil.isoformat(), "recordedAt": svc.utcnow().isoformat(),
                                    "recordedById": user.id, "source": "CSM"}]
    db.add(w)
    await db.commit()
    await db.refresh(w)
    return worker_dict(w)


@router.post("/contractors/{cid}/workers/{wid}/medical")
async def record_medical(cid: str, wid: str, body: MedicalIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE)
    w = await db.get(ContractorWorker, wid)
    if w is None or w.contractorCompanyId != cid:
        raise HTTPException(404, "Worker not found")
    w.medicalFitnessRecords = [*(w.medicalFitnessRecords or []), {
        "validUntil": body.validUntil.isoformat(), "examinedOn": body.examinedOn.isoformat() if body.examinedOn else None,
        "doctorName": body.doctorName, "remarks": body.remarks, "recordedAt": svc.utcnow().isoformat(),
        "recordedById": user.id, "source": "CSM",
    }]
    w.currentMedicalValidUntil = body.validUntil
    await db.commit()
    await db.refresh(w)
    return worker_dict(w)


class RenewIn(BaseModel):
    plantId: str
    templateId: str | None = None
    sendInvite: bool = False
    contactEmail: str | None = None
    inviteTtlDays: int = Field(30, ge=1, le=90)


@router.post("/contractors/{cid}/renew", status_code=201)
async def renew(cid: str, body: RenewIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Renewal = a fresh prequalification against the active template. The
    current qualification stays in force until it expires or this one is decided."""
    await require(db, user, MANAGE, body.plantId)
    c = await db.get(ContractorCompany, cid)
    if c is None:
        raise HTTPException(404, "Contractor not found")
    block = svc.company_block_reason(c.status)
    if block and (c.status or "").upper() == "BLACKLISTED":
        raise HTTPException(409, f"{block}. Reinstate it through a sanction approval first.")
    current = (await db.execute(
        select(CsmPrequalification).where(CsmPrequalification.contractorCompanyId == cid)
        .where(CsmPrequalification.isCurrent.is_(True))
    )).scalars().first()
    pq = await _new_application(db, user, company=c, plant_id=body.plantId, template=await active_template(db, body.templateId),
                                renewal_of=current.id if current else None)
    invite = None
    if body.sendInvite:
        issued = await portal.issue_token(db, prequalification_id=pq.id, company_id=cid,
                                          contact_email=body.contactEmail or c.contactEmail or c.representativeEmail,
                                          contact_name=c.contactPerson or c.representativeName, actor_id=user.id, ttl_days=body.inviteTtlDays)
        invite = {"portalPath": issued.portalPath, "expiresAt": iso(issued.token.expiresAt)}
    await db.commit()
    return {"prequalificationId": pq.id, "applicationNo": pq.applicationNo, "invite": invite}


# ── prequalification applications ────────────────────────────────────────────
def _pq_row(p: CsmPrequalification, plants: dict[str, str], company: ContractorCompany | None = None) -> dict[str, Any]:
    return {
        "id": p.id, "applicationNo": p.applicationNo, "contractorCompanyId": p.contractorCompanyId,
        "contractorName": company.name if company else None, "plantId": p.plantId, "plantName": plants.get(p.plantId),
        "status": p.status, "autoScore": p.autoScore, "finalScore": p.finalScore,
        "criticalFailureCount": len(p.criticalFailures or []), "recommendation": p.recommendation,
        "decision": p.decision, "validUntil": iso(p.validUntil), "isCurrent": p.isCurrent, "isRenewal": p.isRenewal,
        "submittedVia": p.submittedVia, "submittedAt": iso(p.submittedAt), "decidedAt": iso(p.decidedAt),
        "createdAt": iso(p.createdAt), "templateName": (p.templateSnapshot or {}).get("name"),
        "templateVersion": (p.templateSnapshot or {}).get("version"),
    }


async def rescore(db: AsyncSession, pq: CsmPrequalification) -> dict[str, Any]:
    snap = pq.templateSnapshot or {}
    docs = await svc.valid_doc_types(db, pq.contractorCompanyId)
    sc = svc.score_application(snap.get("sections") or [], pq.answers or {}, pq.overrides or {}, docs)
    if pq.status in svc.PQ_OPEN:  # decided applications keep the score they were decided on
        pq.autoScore = sc["autoScore"]
        pq.finalScore = sc["finalScore"]
        pq.criticalFailures = sc["criticalFailures"]
    sc["autoRecommendation"] = svc.recommend(sc["finalScore"], sc["criticalFailures"], float(snap.get("passScore") or 70),
                                             float(snap.get("conditionalScore") or 55))
    return sc


async def _documents(db: AsyncSession, company_id: str, prequal_id: str | None = None) -> list[dict[str, Any]]:
    stmt = select(CsmDocument).where(CsmDocument.contractorCompanyId == company_id).where(CsmDocument.isDeleted.is_(False))
    if prequal_id:
        stmt = stmt.where(or_(CsmDocument.prequalificationId == prequal_id, CsmDocument.status == "ACTIVE"))
    rows = (await db.execute(stmt.order_by(CsmDocument.createdAt.desc()))).scalars().all()
    return [doc_dict(d) for d in rows]


def doc_dict(d: CsmDocument) -> dict[str, Any]:
    return {
        "id": d.id, "contractorCompanyId": d.contractorCompanyId, "prequalificationId": d.prequalificationId,
        "questionCode": d.questionCode, "docType": d.docType, "title": d.title, "documentNo": d.documentNo,
        "fileName": d.fileName, "mimeType": d.mimeType, "fileSize": d.fileSize, "issuedOn": iso(d.issuedOn),
        "expiresOn": iso(d.expiresOn), "state": svc.expiry_state(d.expiresOn), "status": d.status,
        "uploadedVia": d.uploadedVia, "uploadedByName": d.uploadedByName, "verifiedAt": iso(d.verifiedAt),
        "createdAt": iso(d.createdAt),
    }


async def pq_detail(db: AsyncSession, pq: CsmPrequalification) -> dict[str, Any]:
    c = await db.get(ContractorCompany, pq.contractorCompanyId)
    sc = await rescore(db, pq)
    ids = [h.get("byId") for h in pq.history or []] + [pq.reviewerId, pq.recommendedById, pq.decidedById]
    ids += [o.get("byId") for o in (pq.overrides or {}).values()]
    users, pl = await names(db, ids, [pq.plantId])
    tok = await portal.live_token(db, pq.id)
    return {
        **_pq_row(pq, pl, c), "contractor": company_dict(c) if c else None,
        "template": pq.templateSnapshot, "answers": pq.answers or {},
        "overrides": {k: {**v, "byName": users.get(v.get("byId") or "")} for k, v in (pq.overrides or {}).items()},
        "scoring": sc, "criticalFailures": pq.criticalFailures or [], "reviewNotes": pq.reviewNotes,
        "conditions": pq.conditions, "decisionNotes": pq.decisionNotes, "validFrom": iso(pq.validFrom),
        "reviewerName": users.get(pq.reviewerId or ""), "recommendedByName": users.get(pq.recommendedById or ""),
        "recommendedById": pq.recommendedById, "recommendedAt": iso(pq.recommendedAt),
        "decidedByName": users.get(pq.decidedById or ""), "submittedByName": pq.submittedByName,
        "history": [{**h, "byName": h.get("byName") or users.get(h.get("byId") or "")} for h in pq.history or []],
        "documents": await _documents(db, pq.contractorCompanyId, pq.id),
        "portal": {"live": tok is not None, "expiresAt": iso(tok.expiresAt) if tok else None,
                   "contactEmail": tok.contactEmail if tok else None, "lastAccessedAt": iso(tok.lastAccessedAt) if tok else None},
    }


async def _pq_for(db: AsyncSession, user: User, pid: str, perm: str = READ) -> CsmPrequalification:
    pq = await get_live(db, CsmPrequalification, pid)
    await require(db, user, perm, pq.plantId)
    if perm == READ:
        scope = await build_query_scope(db, user.id, READ)
        if not scope.allows_plant(pq.plantId):
            raise HTTPException(403, "Plant is outside your access scope")
    return pq


@router.get("/prequalifications")
async def list_prequals(
    plantId: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    contractorCompanyId: str | None = Query(None), q: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await scoped_plants(db, user, plantId)
    stmt = select(CsmPrequalification, ContractorCompany).join(ContractorCompany, ContractorCompany.id == CsmPrequalification.contractorCompanyId) \
        .where(CsmPrequalification.isDeleted.is_(False)).where(plant_where(CsmPrequalification.plantId, plants))
    if status_ == "OPEN":
        stmt = stmt.where(CsmPrequalification.status.in_(svc.PQ_OPEN))
    elif status_:
        stmt = stmt.where(CsmPrequalification.status == status_)
    if contractorCompanyId:
        stmt = stmt.where(CsmPrequalification.contractorCompanyId == contractorCompanyId)
    if q:
        stmt = stmt.where(or_(ContractorCompany.name.ilike(f"%{q}%"), CsmPrequalification.applicationNo.ilike(f"%{q}%")))
    rows = (await db.execute(stmt.order_by(CsmPrequalification.createdAt.desc()).limit(500))).all()
    _, pl = await names(db, [], [p.plantId for p, _c in rows])
    return {"items": [_pq_row(p, pl, c) for p, c in rows], "total": len(rows)}


class NewPqIn(BaseModel):
    contractorCompanyId: str
    plantId: str
    templateId: str | None = None


@router.post("/prequalifications", status_code=201)
async def create_prequal(body: NewPqIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE, body.plantId)
    c = await db.get(ContractorCompany, body.contractorCompanyId)
    if c is None:
        raise HTTPException(404, "Contractor not found")
    if (c.status or "").upper() == "BLACKLISTED":
        raise HTTPException(409, "Contractor company is blacklisted. Reinstate it through a sanction approval first.")
    pq = await _new_application(db, user, company=c, plant_id=body.plantId, template=await active_template(db, body.templateId))
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


@router.get("/prequalifications/{pid}")
async def get_prequal(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pq = await _pq_for(db, user, pid)
    out = await pq_detail(db, pq)
    await db.commit()  # persist a refreshed score on an open application
    return out


class AnswersIn(BaseModel):
    answers: dict[str, dict[str, Any]]


def apply_answers(pq: CsmPrequalification, answers: dict[str, dict[str, Any]]) -> None:
    sections = (pq.templateSnapshot or {}).get("sections") or []
    merged = dict(pq.answers or {})
    errors: list[str] = []
    for code, a in (answers or {}).items():
        q = svc.find_question(sections, code)
        if q is None:
            errors.append(f"Unknown question {code}")
            continue
        val, err = svc.normalise_answer(q, (a or {}).get("value"))
        if err:
            errors.append(err)
            continue
        comment = ((a or {}).get("comment") or "").strip()[:2000] or None
        if val is None and not comment:
            merged.pop(code, None)
        else:
            merged[code] = {"value": val, "comment": comment}
    if errors:
        raise HTTPException(422, "; ".join(errors[:10]))
    pq.answers = merged


@router.put("/prequalifications/{pid}/answers")
async def save_answers(pid: str, body: AnswersIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pq = await _pq_for(db, user, pid, MANAGE)
    if pq.status != "DRAFT":
        raise HTTPException(409, f"Answers can be changed only while the application is a draft (it is {pq.status})")
    apply_answers(pq, body.answers)
    await rescore(db, pq)
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


class NoteIn(BaseModel):
    note: str | None = Field(None, max_length=4000)


async def _move(db: AsyncSession, user: User, pq: CsmPrequalification, target: str, note: str | None = None) -> None:
    if not svc.can_move_pq(pq.status, target):
        raise HTTPException(409, f"Cannot move an application from {pq.status} to {target}")
    add_history(pq, user, pq.status, target, note)
    pq.status = target


@router.post("/prequalifications/{pid}/submit")
async def submit(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Internal entry: HSE submits on the contractor's behalf."""
    pq = await _pq_for(db, user, pid, MANAGE)
    sc = await rescore(db, pq)
    conflict(svc.submit_blockers(status=pq.status, unanswered=sc["unanswered"]))
    await _move(db, user, pq, "SUBMITTED", "Entered internally by HSE")
    pq.submittedAt, pq.submittedVia, pq.submittedByName = svc.utcnow(), "INTERNAL", getattr(user, "name", None)
    await portal.revoke_for_application(db, prequalification_id=pq.id, actor_id=user.id, reason="Submitted internally")
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


@router.post("/prequalifications/{pid}/start-review")
async def start_review(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pq = await _pq_for(db, user, pid, MANAGE)
    await _move(db, user, pq, "HSE_REVIEW", "HSE review started")
    pq.reviewStartedAt, pq.reviewerId = svc.utcnow(), user.id
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


class OverrideIn(BaseModel):
    questionCode: str
    score: float | None = Field(None, ge=0, le=1)
    reason: str | None = Field(None, max_length=1000)


@router.post("/prequalifications/{pid}/override")
async def override(pid: str, body: OverrideIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Evaluator override of one question's score (0..1), with a reason.
    score=null removes the override."""
    pq = await _pq_for(db, user, pid, MANAGE)
    if pq.status != "HSE_REVIEW":
        raise HTTPException(409, "Scores can be overridden only during HSE review")
    q = svc.find_question((pq.templateSnapshot or {}).get("sections") or [], body.questionCode)
    if q is None or not svc.is_scored(q):
        raise HTTPException(422, "That question is not scored")
    ov = dict(pq.overrides or {})
    if body.score is None:
        ov.pop(body.questionCode, None)
    else:
        if not (body.reason and len(body.reason.strip()) >= 5):
            raise HTTPException(422, "Give a reason for the override (at least 5 characters)")
        ov[body.questionCode] = {"score": body.score, "reason": body.reason.strip(), "byId": user.id,
                                 "at": svc.utcnow().isoformat()}
    pq.overrides = ov
    await rescore(db, pq)
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


class RecommendIn(BaseModel):
    recommendation: str
    reviewNotes: str = Field(min_length=3, max_length=4000)
    conditions: str | None = Field(None, max_length=4000)


@router.post("/prequalifications/{pid}/recommend")
async def recommend(pid: str, body: RecommendIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pq = await _pq_for(db, user, pid, MANAGE)
    check_choice(body.recommendation, svc.PQ_DECISIONS, "recommendation")
    if body.recommendation == "CONDITIONAL" and not (body.conditions and body.conditions.strip()):
        raise HTTPException(422, "State the conditions for a conditional recommendation")
    await rescore(db, pq)
    await _move(db, user, pq, "APPROVAL", f"Recommended {body.recommendation}")
    pq.recommendation, pq.reviewNotes, pq.conditions = body.recommendation, body.reviewNotes, body.conditions
    pq.recommendedAt, pq.recommendedById = svc.utcnow(), user.id
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


class ReturnIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/prequalifications/{pid}/return")
async def return_to_contractor(pid: str, body: ReturnIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Send back to DRAFT for the contractor to complete (portal link, if live, works again)."""
    pq = await _pq_for(db, user, pid, MANAGE)
    await _move(db, user, pq, "DRAFT", f"Returned for more information: {body.reason}")
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


@router.post("/prequalifications/{pid}/send-back")
async def send_back(pid: str, body: ReturnIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Approver returns the recommendation to HSE review."""
    pq = await _pq_for(db, user, pid, APPROVE)
    await _move(db, user, pq, "HSE_REVIEW", f"Sent back by approver: {body.reason}")
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


class DecideIn(BaseModel):
    decision: str
    conditions: str | None = Field(None, max_length=4000)
    decisionNotes: str | None = Field(None, max_length=4000)
    validityMonths: int | None = Field(None, ge=1, le=60)


@router.post("/prequalifications/{pid}/decide")
async def decide(pid: str, body: DecideIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Plant head / corporate HSE decision. Writes the outcome onto the
    ContractorCompany prequalification fields (what the EPC gate reads)."""
    pq = await _pq_for(db, user, pid, APPROVE)
    conditions = body.conditions if body.conditions is not None else pq.conditions
    conflict(svc.decision_blockers(
        status=pq.status, decision=body.decision, conditions=conditions, notes=body.decisionNotes,
        recommendation=pq.recommendation, recommended_by=pq.recommendedById, decided_by=user.id,
    ))
    snap = pq.templateSnapshot or {}
    now = svc.utcnow()
    months = body.validityMonths or (snap.get("validityMonths") if body.decision == "QUALIFIED" else snap.get("conditionalValidityMonths")) or 12
    valid_until = svc.validity_for(body.decision, now, int(months), int(months))
    await _move(db, user, pq, body.decision, body.decisionNotes)
    pq.decision, pq.decisionNotes, pq.decidedAt, pq.decidedById = body.decision, body.decisionNotes, now, user.id
    pq.conditions = conditions if body.decision == "CONDITIONAL" else pq.conditions
    pq.validFrom = now if valid_until else None
    pq.validUntil = valid_until
    for prior in (await db.execute(
        select(CsmPrequalification).where(CsmPrequalification.contractorCompanyId == pq.contractorCompanyId)
        .where(CsmPrequalification.isCurrent.is_(True)).where(CsmPrequalification.id != pq.id)
    )).scalars().all():
        prior.isCurrent = False
    pq.isCurrent = True
    c = await db.get(ContractorCompany, pq.contractorCompanyId)
    c.prequalificationStatus = svc.PQ_TO_EPC[body.decision]
    c.prequalificationScore = pq.finalScore
    c.prequalificationValidUntil = valid_until
    c.prequalificationReviewedById = user.id
    c.prequalificationReviewedAt = now
    await portal.revoke_for_application(db, prequalification_id=pq.id, actor_id=user.id, reason=f"Application {body.decision.lower()}")
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


@router.post("/prequalifications/{pid}/withdraw")
async def withdraw(pid: str, body: ReturnIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pq = await _pq_for(db, user, pid, MANAGE)
    await _move(db, user, pq, "WITHDRAWN", body.reason)
    await portal.revoke_for_application(db, prequalification_id=pq.id, actor_id=user.id, reason="Application withdrawn")
    await db.commit()
    await db.refresh(pq)
    return await pq_detail(db, pq)


class InviteIn(BaseModel):
    contactEmail: str | None = None
    contactName: str | None = None
    ttlDays: int = Field(30, ge=1, le=90)


@router.post("/prequalifications/{pid}/invite")
async def invite(pid: str, body: InviteIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Mint the contractor's link. The raw token is returned once, here only."""
    pq = await _pq_for(db, user, pid, MANAGE)
    if pq.status != "DRAFT":
        raise HTTPException(409, "A link can be issued only while the application is a draft")
    c = await db.get(ContractorCompany, pq.contractorCompanyId)
    issued = await portal.issue_token(
        db, prequalification_id=pq.id, company_id=pq.contractorCompanyId,
        contact_email=body.contactEmail or c.contactEmail or c.representativeEmail,
        contact_name=body.contactName or c.contactPerson or c.representativeName, actor_id=user.id, ttl_days=body.ttlDays,
    )
    add_history(pq, user, pq.status, pq.status, f"Contractor link issued to {issued.token.contactEmail or 'contractor'}")
    await db.commit()
    return {"portalPath": issued.portalPath, "expiresAt": iso(issued.token.expiresAt), "contactEmail": issued.token.contactEmail}


@router.post("/prequalifications/{pid}/invite/revoke")
async def revoke_invite(pid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    pq = await _pq_for(db, user, pid, MANAGE)
    n = await portal.revoke_for_application(db, prequalification_id=pq.id, actor_id=user.id, reason="Revoked by HSE")
    await db.commit()
    return {"revoked": n}


# ── documents ────────────────────────────────────────────────────────────────
def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "document"


class UploadUrlIn(BaseModel):
    contractorCompanyId: str
    fileName: str = Field(min_length=1, max_length=200)
    mimeType: str
    fileSize: int = Field(gt=0, le=svc.DOC_MAX_BYTES)


class DocumentIn(BaseModel):
    contractorCompanyId: str
    prequalificationId: str | None = None
    questionCode: str | None = None
    docType: str
    title: str | None = Field(None, max_length=200)
    documentNo: str | None = Field(None, max_length=100)
    issuedOn: datetime | None = None
    expiresOn: datetime | None = None
    fileName: str = Field(min_length=1, max_length=200)
    storagePath: str
    mimeType: str | None = None
    fileSize: int | None = Field(None, gt=0, le=svc.DOC_MAX_BYTES)


def _storage():
    # Imported lazily so the module (and its tests) load without Supabase settings.
    from app.services import storage
    return storage


@router.post("/documents/upload-url")
async def document_upload_url(body: UploadUrlIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE)
    if body.mimeType not in svc.DOC_MIME:
        raise HTTPException(400, "Upload a PDF or a photo (JPG, PNG, WEBP, HEIC)")
    st = _storage()
    if not st.is_storage_configured():
        raise HTTPException(503, "File storage is not configured")
    path = f"csm/{body.contractorCompanyId}/{secrets.token_hex(4)}-{safe_name(body.fileName)}"
    try:
        signed = st.create_signed_upload_url(path)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    return {"storagePath": path, **signed}


async def record_document(db: AsyncSession, body: DocumentIn, *, via: str, by_id: str | None, by_name: str | None) -> CsmDocument:
    check_choice(body.docType, svc.DOC_TYPES, "docType")
    if body.expiresOn and body.issuedOn and body.expiresOn <= body.issuedOn:
        raise HTTPException(422, "Expiry date must be after the issue date")
    if body.docType in svc.SINGLE_INSTANCE_DOCS:
        for old in (await db.execute(
            select(CsmDocument).where(CsmDocument.contractorCompanyId == body.contractorCompanyId)
            .where(CsmDocument.docType == body.docType).where(CsmDocument.status == "ACTIVE")
            .where(CsmDocument.isDeleted.is_(False))
        )).scalars().all():
            old.status = "SUPERSEDED"
    d = CsmDocument(
        contractorCompanyId=body.contractorCompanyId, prequalificationId=body.prequalificationId,
        questionCode=body.questionCode, docType=body.docType, title=body.title, documentNo=body.documentNo,
        fileName=body.fileName, storagePath=body.storagePath, mimeType=body.mimeType, fileSize=body.fileSize,
        issuedOn=body.issuedOn, expiresOn=body.expiresOn, status="ACTIVE", uploadedVia=via,
        uploadedById=by_id, uploadedByName=by_name,
    )
    db.add(d)
    await db.flush()
    return d


@router.post("/documents", status_code=201)
async def add_document(body: DocumentIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE)
    if await db.get(ContractorCompany, body.contractorCompanyId) is None:
        raise HTTPException(404, "Contractor not found")
    # The path must be one we issued for this company; otherwise any object in
    # the bucket could be attached to a contractor record.
    if not body.storagePath.startswith(f"csm/{body.contractorCompanyId}/"):
        raise HTTPException(400, "Unrecognised upload reference")
    if body.prequalificationId:
        pq = await get_live(db, CsmPrequalification, body.prequalificationId)
        if pq.contractorCompanyId != body.contractorCompanyId:
            raise HTTPException(422, "That application belongs to another contractor")
    d = await record_document(db, body, via="INTERNAL", by_id=user.id, by_name=getattr(user, "name", None))
    await db.commit()
    await db.refresh(d)
    return doc_dict(d)


@router.get("/contractors/{cid}/documents")
async def list_documents(cid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await visible_company(db, user, cid)
    items = await _documents(db, cid)
    return {"items": items, "total": len(items)}


@router.get("/documents/{did}/download")
async def download_document(did: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    d = await get_live(db, CsmDocument, did)
    await visible_company(db, user, d.contractorCompanyId)
    try:
        return {"url": _storage().create_signed_download_url(d.storagePath, 300)}
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e


@router.post("/documents/{did}/verify")
async def verify_document(did: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE)
    d = await get_live(db, CsmDocument, did)
    d.verifiedAt, d.verifiedById = svc.utcnow(), user.id
    await db.commit()
    await db.refresh(d)
    return doc_dict(d)


class RemoveIn(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


@router.post("/documents/{did}/remove")
async def remove_document(did: str, body: RemoveIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await require(db, user, MANAGE)
    d = await get_live(db, CsmDocument, did)
    d.isDeleted, d.deletedAt, d.deletedBy, d.deletionReason = True, svc.utcnow(), user.id, body.reason.strip()
    await db.commit()
    return {"ok": True}
