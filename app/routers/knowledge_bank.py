"""Knowledge bank router (JSL Hisar requirement). Mounts at /api/kb.

Safety standards, SOPs, checklists, policies and the rest, stored as
controlled documents that EVERYONE in the company can read.

RBAC (granted by safeops_360/prisma/apply-knowledge-bank-permissions.ts):
  KB.READ     every role, ALL_PLANTS. Read is deliberately not plant-scoped:
              a document's plantId says which plant it APPLIES to, not who may
              read it. Drafts and documents under review are not visible to
              plain readers.
  KB.MANAGE   create, upload versions, edit metadata, submit for review, mark
              reviewed, make obsolete, delete never-published drafts, and see
              acknowledgement reports. Plant-scoped holders manage their own
              plant's documents; company-wide documents (plantId NULL) need
              KB.MANAGE at ALL_PLANTS.
  KB.APPROVE  publish a version under review, or send it back. Same plant rule.

Upload is the platform's two-phase signed-URL flow: POST …/upload-url mints a
short-lived Supabase PUT URL, the browser PUTs the file, POST …/versions
registers it. The backend then reads the object once to verify type and size
and to extract its text for search and Q&A.

Mounted ungated (same reasoning as ims / fire_safety / capture): a new licence
module code would 403 under the signed dev licence that predates it.
Per-endpoint RBAC is the gate.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.knowledge_bank import KbAcknowledgement, KbCategory, KbDocument, KbDocumentVersion, KbView
from app.models.masters import Department
from app.models.plant import Plant
from app.models.user import Role, User, UserRole
from app.services import knowledge_bank as svc
from app.services.access_scope import build_query_scope
from app.services.ai import anthropic_client as ai
from app.services.numbering import next_number
from app.services.permissions import PermissionContext, can, get_user_role_codes
from app.services.plant_directory import resolve_plant_names
from app.services.storage import (
    create_signed_download_url,
    create_signed_upload_url,
    delete_storage_object,
    download_object,
    is_storage_configured,
)
from app.services.user_directory import resolve_user_directory

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb", tags=["knowledge-bank"])

READ, MANAGE, APPROVE = "KB.READ", "KB.MANAGE", "KB.APPROVE"
COMPANY = "COMPANY"  # plant filter value meaning "company-wide documents only"


# ── permission helpers ───────────────────────────────────────────────────────
async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _require_doc(db: AsyncSession, user: User, perm: str, plant_id: str | None) -> None:
    """Manage/approve a document. A plant document needs the permission for
    that plant; a company-wide one needs it at ALL_PLANTS, otherwise a single
    plant's safety officer could rewrite a corporate policy."""
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission '{perm}'")
    if plant_id is None and res.matched_scope != "ALL_PLANTS":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Company-wide documents need '{perm}' across all plants. Ask corporate HSE, or set the document to your plant.",
        )


@dataclass
class Caps:
    manage: bool
    manage_all: bool
    approve: bool
    approve_all: bool

    @property
    def staff(self) -> bool:
        """May see drafts / under-review documents and acknowledgement reports."""
        return self.manage or self.approve


async def _caps(db: AsyncSession, user: User) -> Caps:
    m = await can(db, user.id, MANAGE, PermissionContext())
    a = await can(db, user.id, APPROVE, PermissionContext())
    return Caps(
        manage=m.allowed, manage_all=m.allowed and m.matched_scope == "ALL_PLANTS",
        approve=a.allowed, approve_all=a.allowed and a.matched_scope == "ALL_PLANTS",
    )


async def _staff_plants(db: AsyncSession, user: User, caps: Caps) -> list[str] | None:
    """Plants a manager/approver acts for. None = all plants."""
    if caps.manage_all or caps.approve_all:
        return None
    plants: set[str] = set()
    for perm, held in ((MANAGE, caps.manage), (APPROVE, caps.approve)):
        if held:
            scope = await build_query_scope(db, user.id, perm)
            if scope.all_plants:
                return None
            plants.update(scope.plant_ids)
    return sorted(plants)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _check_choice(value: str | None, allowed, field: str) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(422, f"{field} must be one of {', '.join(allowed)}")


def _is_pg(db: AsyncSession) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:  # noqa: BLE001
        return False


async def _categories(db: AsyncSession) -> list[KbCategory]:
    return list((await db.execute(select(KbCategory).order_by(KbCategory.sortOrder, KbCategory.name))).scalars().all())


def _descendants(cats: list[KbCategory], root_id: str) -> set[str]:
    children: dict[str | None, list[str]] = {}
    for c in cats:
        children.setdefault(c.parentId, []).append(c.id)
    out, stack = {root_id}, [root_id]
    while stack:
        for ch in children.get(stack.pop(), []):
            if ch not in out:
                out.add(ch)
                stack.append(ch)
    return out


async def _get_doc(db: AsyncSession, doc_id: str) -> KbDocument:
    d = await db.get(KbDocument, doc_id, options=[defer(KbDocument.searchText)])
    if d is None or d.isDeleted:
        raise HTTPException(404, "Document not found")
    return d


async def _get_readable_doc(db: AsyncSession, user: User, doc_id: str) -> tuple[KbDocument, Caps]:
    await _require(db, user, READ)
    d = await _get_doc(db, doc_id)
    caps = await _caps(db, user)
    if d.status not in ("PUBLISHED", "OBSOLETE") and not caps.staff:
        raise HTTPException(404, "Document not found")
    return d, caps


async def _versions(db: AsyncSession, doc_id: str) -> list[KbDocumentVersion]:
    return list((await db.execute(
        select(KbDocumentVersion).options(defer(KbDocumentVersion.extractedText))
        .where(KbDocumentVersion.documentId == doc_id).order_by(KbDocumentVersion.versionNo.desc())
    )).scalars().all())


async def _pending_version(db: AsyncSession, doc_id: str) -> KbDocumentVersion | None:
    return (await db.execute(
        select(KbDocumentVersion).options(defer(KbDocumentVersion.extractedText))
        .where(KbDocumentVersion.documentId == doc_id, KbDocumentVersion.status.in_(svc.PENDING_VERSION))
        .order_by(KbDocumentVersion.versionNo.desc()).limit(1)
    )).scalars().first()


async def _version_text(db: AsyncSession, version_id: str | None) -> str | None:
    if not version_id:
        return None
    return (await db.execute(select(KbDocumentVersion.extractedText).where(KbDocumentVersion.id == version_id))).scalar()


async def _refresh_search_text(db: AsyncSession, d: KbDocument) -> None:
    """Search corpus = metadata + the text readers see (the current version),
    or the pending draft's text while the document was never published, so
    managers can find their own drafts."""
    vid = d.currentVersionId
    if not vid:
        pv = await _pending_version(db, d.id)
        vid = pv.id if pv else None
    body = await _version_text(db, vid)
    text = svc.build_search_text(
        doc_no=d.docNo, title=d.title, summary=d.summary, tags=d.tags, standard_ref=d.standardRef,
        department=d.department, body=body,
    )
    await db.execute(update(KbDocument).where(KbDocument.id == d.id).values(searchText=text, updatedAt=KbDocument.updatedAt))


# ── serialisers ──────────────────────────────────────────────────────────────
def _version(v: KbDocumentVersion, users: dict[str, str]) -> dict[str, Any]:
    return {
        "id": v.id, "documentId": v.documentId, "versionNo": v.versionNo, "status": v.status,
        "fileName": v.fileName, "mimeType": v.mimeType, "sizeBytes": v.sizeBytes,
        "extractionStatus": v.extractionStatus, "changeNote": v.changeNote, "reviewNote": v.reviewNote,
        "uploadedBy": v.uploadedBy, "uploadedByName": users.get(v.uploadedBy), "uploadedAt": _iso(v.uploadedAt),
        "submittedAt": _iso(v.submittedAt), "submittedByName": users.get(v.submittedBy or ""),
        "publishedAt": _iso(v.publishedAt), "publishedByName": users.get(v.publishedBy or ""),
        "viewable": v.mimeType == "application/pdf" or v.mimeType.startswith("image/") or v.mimeType.startswith("text/"),
    }


def _doc(
    d: KbDocument, cats: dict[str, KbCategory], users: dict[str, str], plants: dict[str, str],
    *, current: KbDocumentVersion | None = None, pending: KbDocumentVersion | None = None, snippet: str | None = None,
) -> dict[str, Any]:
    cat = cats.get(d.categoryId)
    return {
        "id": d.id, "docNo": d.docNo, "title": d.title, "summary": d.summary,
        "categoryId": d.categoryId, "categoryName": cat.name if cat else None,
        "docType": d.docType, "standardRef": d.standardRef, "department": d.department,
        "plantId": d.plantId, "plantName": plants.get(d.plantId) if d.plantId else None, "companyWide": d.plantId is None,
        "ownerUserId": d.ownerUserId, "ownerName": users.get(d.ownerUserId),
        "tags": d.tags or [], "status": d.status, "currentVersionId": d.currentVersionId,
        "effectiveDate": _iso(d.effectiveDate), "reviewDueDate": _iso(d.reviewDueDate),
        "reviewIntervalMonths": d.reviewIntervalMonths, "reviewState": svc.review_state(d.status, d.reviewDueDate),
        "lastReviewedAt": _iso(d.lastReviewedAt), "lastReviewedByName": users.get(d.lastReviewedBy or ""),
        "ackRequired": d.ackRequired, "audienceRoles": d.audienceRoles or [], "viewCount": d.viewCount,
        "publishedAt": _iso(d.publishedAt), "obsoletedAt": _iso(d.obsoletedAt), "obsoleteReason": d.obsoleteReason,
        "supersededByDocId": d.supersededByDocId, "updatedAt": _iso(d.updatedAt),
        "currentVersion": _version(current, users) if current else None,
        "pendingVersion": _version(pending, users) if pending else None,
        **({"snippet": snippet} if snippet is not None else {}),
    }


async def _out_many(db: AsyncSession, docs: list[KbDocument], *, snippets: dict[str, str | None] | None = None,
                    with_pending: bool = False) -> list[dict[str, Any]]:
    if not docs:
        return []
    cats = {c.id: c for c in await _categories(db)}
    vids = [d.currentVersionId for d in docs if d.currentVersionId]
    vers = {}
    if vids:
        vers = {v.id: v for v in (await db.execute(
            select(KbDocumentVersion).options(defer(KbDocumentVersion.extractedText)).where(KbDocumentVersion.id.in_(vids))
        )).scalars().all()}
    pend: dict[str, KbDocumentVersion] = {}
    if with_pending:
        for v in (await db.execute(
            select(KbDocumentVersion).options(defer(KbDocumentVersion.extractedText))
            .where(KbDocumentVersion.documentId.in_([d.id for d in docs]), KbDocumentVersion.status.in_(svc.PENDING_VERSION))
        )).scalars().all():
            if v.documentId not in pend or v.versionNo > pend[v.documentId].versionNo:
                pend[v.documentId] = v
    uids = [d.ownerUserId for d in docs] + [v.uploadedBy for v in [*vers.values(), *pend.values()]]
    users = {k: v.name for k, v in (await resolve_user_directory(db, uids)).items()}
    plants = await resolve_plant_names(db, [d.plantId for d in docs])
    return [
        _doc(d, cats, users, plants, current=vers.get(d.currentVersionId or ""), pending=pend.get(d.id),
             snippet=(snippets or {}).get(d.id) if snippets is not None else None)
        for d in docs
    ]


async def _detail(db: AsyncSession, user: User, d: KbDocument, caps: Caps) -> dict[str, Any]:
    versions = await _versions(db, d.id)
    if not caps.staff:
        versions = [v for v in versions if v.status in ("PUBLISHED", "SUPERSEDED")]
    cats = {c.id: c for c in await _categories(db)}
    uids = [d.ownerUserId, d.lastReviewedBy] + [x for v in versions for x in (v.uploadedBy, v.submittedBy, v.publishedBy)]
    users = {k: v.name for k, v in (await resolve_user_directory(db, uids)).items()}
    plants = await resolve_plant_names(db, [d.plantId])
    current = next((v for v in versions if v.id == d.currentVersionId), None)
    pending = next((v for v in versions if v.status in svc.PENDING_VERSION), None) if caps.staff else None
    out = _doc(d, cats, users, plants, current=current, pending=pending)
    out["versions"] = [_version(v, users) for v in versions]
    my_ack = None
    if d.currentVersionId:
        my_ack = (await db.execute(select(KbAcknowledgement).where(
            KbAcknowledgement.versionId == d.currentVersionId, KbAcknowledgement.userId == user.id,
        ))).scalars().first()
    out["myAcknowledgement"] = {"acknowledgedAt": _iso(my_ack.acknowledgedAt), "versionId": my_ack.versionId} if my_ack else None
    roles = set(await get_user_role_codes(db, user.id)) | {getattr(user, "role", None) or ""}
    out["appliesToMe"] = svc.applies_to_user(
        doc_plant_id=d.plantId, audience_roles=d.audienceRoles,
        user_plant_ids={getattr(user, "plantId", None) or ""}, user_role_codes=roles,
    )
    if d.supersededByDocId:
        nxt = await db.get(KbDocument, d.supersededByDocId, options=[defer(KbDocument.searchText)])
        out["supersededBy"] = {"id": nxt.id, "docNo": nxt.docNo, "title": nxt.title} if nxt and not nxt.isDeleted else None
    can_manage = can_approve = False
    if caps.manage:
        can_manage = (await can(db, user.id, MANAGE, PermissionContext(plant_id=d.plantId))).allowed and (d.plantId is not None or caps.manage_all)
    if caps.approve:
        can_approve = (await can(db, user.id, APPROVE, PermissionContext(plant_id=d.plantId))).allowed and (d.plantId is not None or caps.approve_all)
    out["permissions"] = {"manage": can_manage, "approve": can_approve, "viewAckReport": caps.staff}
    if caps.staff and d.currentVersionId:
        out["ackCount"] = (await db.execute(
            select(func.count()).select_from(KbAcknowledgement).where(KbAcknowledgement.versionId == d.currentVersionId)
        )).scalar() or 0
    return out


# ── meta + categories ────────────────────────────────────────────────────────
def _category(c: KbCategory, counts: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": c.id, "code": c.code, "name": c.name, "parentId": c.parentId, "description": c.description,
        "icon": c.icon, "sortOrder": c.sortOrder, "isActive": c.isActive,
        **({"documentCount": counts.get(c.id, 0)} if counts is not None else {}),
    }


@router.get("/meta")
async def meta(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    caps = await _caps(db, user)
    plants = (await db.execute(select(Plant.id, Plant.code, Plant.name).order_by(Plant.name))).all()
    depts = (await db.execute(
        select(Department.name).where(Department.active.is_(True)).distinct().order_by(Department.name)
    )).scalars().all()
    roles = (await db.execute(
        select(Role.code, Role.name).where(Role.isActive.is_(True)).order_by(Role.sortOrder, Role.name)
    )).all() if caps.staff else []
    return {
        "docTypes": list(svc.DOC_TYPES.keys()), "docStatuses": svc.DOC_STATUSES,
        "versionStatuses": svc.VERSION_STATUSES, "standardFamilies": svc.STANDARD_FAMILIES,
        "categories": [_category(c) for c in await _categories(db) if c.isActive],
        "plants": [{"id": p.id, "code": p.code, "name": p.name} for p in plants],
        "departments": sorted({d for d in depts if d}),
        "roles": [{"code": r.code, "name": r.name} for r in roles],
        "allowedMimeTypes": sorted(svc.ALLOWED_MIME), "allowedExtensions": sorted(svc.EXT_MIME.keys()),
        "maxFileBytes": svc.MAX_FILE_BYTES,
        "caps": {"manage": caps.manage, "manageAllPlants": caps.manage_all, "approve": caps.approve,
                 "approveAllPlants": caps.approve_all, "staffPlantIds": await _staff_plants(db, user, caps) if caps.staff else []},
        "aiAvailable": ai.is_configured(),
    }


@router.get("/categories")
async def list_categories(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    cats = await _categories(db)
    rows = (await db.execute(
        select(KbDocument.categoryId, func.count()).where(KbDocument.isDeleted.is_(False), KbDocument.status == "PUBLISHED")
        .group_by(KbDocument.categoryId)
    )).all()
    direct = {r[0]: int(r[1]) for r in rows}
    totals = {c.id: sum(direct.get(x, 0) for x in _descendants(cats, c.id)) for c in cats}
    return {"items": [_category(c, totals) for c in cats]}


class CategoryIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    parentId: str | None = None
    description: str | None = Field(None, max_length=500)
    sortOrder: int = Field(500, ge=0, le=10000)


class CategoryPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    parentId: str | None = None
    description: str | None = Field(None, max_length=500)
    sortOrder: int | None = Field(None, ge=0, le=10000)
    isActive: bool | None = None


async def _require_manage_all(db: AsyncSession, user: User) -> None:
    await _require_doc(db, user, MANAGE, None)


@router.post("/categories", status_code=201)
async def create_category(body: CategoryIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_manage_all(db, user)
    cats = await _categories(db)
    if body.parentId and body.parentId not in {c.id for c in cats}:
        raise HTTPException(422, "Parent category not found")
    if any(c.name.lower() == body.name.strip().lower() and c.parentId == body.parentId for c in cats):
        raise HTTPException(409, "A category with this name already exists here")
    base = re.sub(r"[^A-Z0-9]+", "_", body.name.upper()).strip("_")[:40] or "CATEGORY"
    code, n = base, 2
    while any(c.code == code for c in cats):
        code, n = f"{base}_{n}", n + 1
    c = KbCategory(code=code, name=body.name.strip(), parentId=body.parentId, description=body.description,
                   sortOrder=body.sortOrder, isActive=True, createdAt=svc.utcnow(), updatedAt=svc.utcnow())
    db.add(c)
    await db.commit()
    return _category(c)


@router.patch("/categories/{cid}")
async def update_category(cid: str, body: CategoryPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_manage_all(db, user)
    c = await db.get(KbCategory, cid)
    if c is None:
        raise HTTPException(404, "Category not found")
    fields = body.model_fields_set
    if "parentId" in fields:
        if body.parentId:
            cats = await _categories(db)
            if body.parentId not in {x.id for x in cats}:
                raise HTTPException(422, "Parent category not found")
            if body.parentId in _descendants(cats, c.id):
                raise HTTPException(422, "A category cannot sit under itself or one of its sub-categories")
        c.parentId = body.parentId
    if body.name is not None:
        c.name = body.name.strip()
    if "description" in fields:
        c.description = body.description
    if body.sortOrder is not None:
        c.sortOrder = body.sortOrder
    if body.isActive is not None:
        c.isActive = body.isActive
    c.updatedAt = svc.utcnow()
    await db.commit()
    return _category(c)


# ── home + search ────────────────────────────────────────────────────────────
async def _published_docs(db: AsyncSession, order, limit: int) -> list[KbDocument]:
    return list((await db.execute(
        select(KbDocument).options(defer(KbDocument.searchText))
        .where(KbDocument.isDeleted.is_(False), KbDocument.status == "PUBLISHED").order_by(*order).limit(limit)
    )).scalars().all())


@router.get("/home")
async def home(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    caps = await _caps(db, user)
    recent = await _published_docs(db, (KbDocument.publishedAt.desc(), KbDocument.docNo), 8)
    viewed = await _published_docs(db, (KbDocument.viewCount.desc(), KbDocument.docNo), 8)
    total = (await db.execute(select(func.count()).select_from(KbDocument).where(
        KbDocument.isDeleted.is_(False), KbDocument.status == "PUBLISHED"))).scalar() or 0
    pending = await _my_pending(db, user)
    out: dict[str, Any] = {
        "recent": await _out_many(db, recent), "mostViewed": await _out_many(db, [d for d in viewed if d.viewCount > 0]),
        "publishedCount": total, "myPendingAcks": len(pending),
    }
    if caps.staff:
        out["reviewDueCount"] = len(await _review_due_docs(db, user, caps, 30))
        out["awaitingReviewCount"] = (await db.execute(
            select(func.count()).select_from(KbDocumentVersion).where(KbDocumentVersion.status == "UNDER_REVIEW")
        )).scalar() or 0
    return out


SORTS = ("relevance", "recent", "views", "title", "docNo")


@router.get("/documents")
async def search_documents(
    q: str | None = Query(None, max_length=200),
    categoryId: str | None = Query(None), docType: str | None = Query(None),
    standardRef: str | None = Query(None, max_length=120), department: str | None = Query(None),
    plant: str | None = Query(None, description="COMPANY = company-wide only; a plant id = that plant's documents plus company-wide ones"),
    plantOnly: bool = Query(False, description="With a plant id: only that plant's own documents"),
    status_: str | None = Query(None, alias="status", description="PUBLISHED (default), OBSOLETE, DRAFT, UNDER_REVIEW, ALL"),
    includeObsolete: bool = Query(False),
    sort: str = Query("relevance"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, READ)
    _check_choice(sort, SORTS, "sort")
    _check_choice(docType, tuple(svc.DOC_TYPES), "docType")
    caps = await _caps(db, user)

    conds: list[Any] = [KbDocument.isDeleted.is_(False)]
    st = (status_ or "").upper() or None
    if st in ("DRAFT", "UNDER_REVIEW", "ALL") and not caps.staff:
        raise HTTPException(403, "Only knowledge-bank managers can list drafts and documents under review")
    if st == "ALL":
        pass
    elif st:
        _check_choice(st, svc.DOC_STATUSES, "status")
        conds.append(KbDocument.status == st)
    else:
        conds.append(KbDocument.status.in_(("PUBLISHED", "OBSOLETE") if includeObsolete else ("PUBLISHED",)))
    if categoryId:
        conds.append(KbDocument.categoryId.in_(_descendants(await _categories(db), categoryId)))
    if docType:
        conds.append(KbDocument.docType == docType)
    if standardRef:
        conds.append(KbDocument.standardRef.ilike(f"%{svc._like_escape(standardRef)}%", escape="\\"))
    if department:
        conds.append(func.lower(KbDocument.department) == department.lower())
    if plant == COMPANY:
        conds.append(KbDocument.plantId.is_(None))
    elif plant:
        conds.append(KbDocument.plantId == plant if plantOnly else or_(KbDocument.plantId == plant, KbDocument.plantId.is_(None)))

    q = (q or "").strip() or None
    mode = None
    match = None
    rank = None
    if q:
        if _is_pg(db):
            tsq = svc.fts_plain_query(q)
            match, rank, mode = svc.fts_match(tsq), svc.fts_rank(tsq), "fulltext"
            n = (await db.execute(select(func.count()).select_from(KbDocument).where(*conds, match))).scalar() or 0
            if n == 0:
                match, rank, mode = svc.ilike_terms(q), None, "keyword"
        else:
            match, mode = svc.ilike_terms(q), "keyword"
        if match is None:
            match = KbDocument.id == "__none__"
        conds.append(match)

    total = (await db.execute(select(func.count()).select_from(KbDocument).where(*conds))).scalar() or 0

    order: list[Any]
    if sort == "relevance" and rank is not None:
        order = [rank.desc(), KbDocument.docNo]
    elif sort == "views":
        order = [KbDocument.viewCount.desc(), KbDocument.docNo]
    elif sort == "title":
        order = [KbDocument.title]
    elif sort == "docNo":
        order = [KbDocument.docNo]
    else:  # recent, or relevance without a rank
        order = [func.coalesce(KbDocument.publishedAt, KbDocument.updatedAt).desc(), KbDocument.docNo]

    cols: list[Any] = [KbDocument]
    term = None
    if q:
        words = sorted((w for w in re.findall(r"[\w\-]+", q.lower()) if len(w) >= 3), key=len, reverse=True)
        term = words[0] if words else None
    if term:
        pos = func.strpos(func.lower(KbDocument.searchText), term) if _is_pg(db) else func.instr(func.lower(KbDocument.searchText), term)
        start = func.greatest(pos - 120, 1) if _is_pg(db) else func.max(pos - 120, 1)
        cols.append(func.substr(KbDocument.searchText, start, 420).label("excerpt"))
    stmt = select(*cols).options(defer(KbDocument.searchText)).where(*conds).order_by(*order).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).all()
    docs = [r[0] for r in rows]
    snippets = {r[0].id: svc.make_snippet(r[1], q) for r in rows} if term else ({d.id: d.summary for d in docs} if q else None)
    return {
        "items": await _out_many(db, docs, snippets=snippets, with_pending=caps.staff and st in ("DRAFT", "UNDER_REVIEW", "ALL")),
        "total": total, "limit": limit, "offset": offset, "matchMode": mode,
    }


# ── documents: create / read / edit / delete ─────────────────────────────────
class DocIn(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    summary: str | None = Field(None, max_length=4000)
    categoryId: str
    docType: str
    standardRef: str | None = Field(None, max_length=120)
    department: str | None = Field(None, max_length=120)
    plantId: str | None = None
    ownerUserId: str | None = None
    tags: list[str] = []
    ackRequired: bool = False
    audienceRoles: list[str] = []
    reviewIntervalMonths: int = Field(24, ge=1, le=120)


class DocPatch(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=300)
    summary: str | None = Field(None, max_length=4000)
    categoryId: str | None = None
    docType: str | None = None
    standardRef: str | None = Field(None, max_length=120)
    department: str | None = Field(None, max_length=120)
    plantId: str | None = None
    ownerUserId: str | None = None
    tags: list[str] | None = None
    ackRequired: bool | None = None
    audienceRoles: list[str] | None = None
    reviewIntervalMonths: int | None = Field(None, ge=1, le=120)
    reviewDueDate: datetime | None = None


async def _check_category(db: AsyncSession, cid: str) -> None:
    c = await db.get(KbCategory, cid)
    if c is None or not c.isActive:
        raise HTTPException(422, "Pick an active category")


def _clean_roles(roles: list[str] | None) -> list[str]:
    return sorted({r.strip().upper() for r in roles or [] if r and r.strip()})[:50]


@router.post("/documents", status_code=201)
async def create_document(body: DocIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require_doc(db, user, MANAGE, body.plantId)
    _check_choice(body.docType, tuple(svc.DOC_TYPES), "docType")
    await _check_category(db, body.categoryId)
    now = svc.utcnow()
    no = await next_number(db, column=KbDocument.docNo, stem=svc.doc_no_stem(body.docType), width=4)
    d = KbDocument(
        docNo=no, title=body.title.strip(), summary=(body.summary or "").strip() or None, categoryId=body.categoryId,
        docType=body.docType, standardRef=(body.standardRef or "").strip() or None,
        department=(body.department or "").strip() or None, plantId=body.plantId, ownerUserId=body.ownerUserId or user.id,
        tags=svc.normalize_tags(body.tags), status="DRAFT", ackRequired=body.ackRequired,
        audienceRoles=_clean_roles(body.audienceRoles), reviewIntervalMonths=body.reviewIntervalMonths, viewCount=0,
        createdAt=now, createdBy=user.id, updatedAt=now,
    )
    d.searchText = svc.build_search_text(doc_no=no, title=d.title, summary=d.summary, tags=d.tags,
                                         standard_ref=d.standardRef, department=d.department, body=None)
    db.add(d)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(409, "Document number clash, please retry") from e
    caps = await _caps(db, user)
    return await _detail(db, user, d, caps)


@router.get("/documents/{doc_id}")
async def get_document(doc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d, caps = await _get_readable_doc(db, user, doc_id)
    if d.status in ("PUBLISHED", "OBSOLETE"):
        await _record_view(db, d, user)
    return await _detail(db, user, d, caps)


async def _record_view(db: AsyncSession, d: KbDocument, user: User) -> None:
    """One view per person per version per hour, so a refresh is not a read."""
    now = svc.utcnow()
    recent = (await db.execute(select(KbView.id).where(
        KbView.documentId == d.id, KbView.userId == user.id, KbView.versionId == d.currentVersionId,
        KbView.viewedAt >= now - timedelta(hours=1),
    ).limit(1))).scalar()
    if recent:
        return
    db.add(KbView(documentId=d.id, versionId=d.currentVersionId, userId=user.id, viewedAt=now))
    # updatedAt pinned to itself: a read is not an edit (the column has an onupdate default).
    await db.execute(update(KbDocument).where(KbDocument.id == d.id).values(
        viewCount=KbDocument.viewCount + 1, updatedAt=KbDocument.updatedAt))
    await db.commit()
    await db.refresh(d)


@router.patch("/documents/{doc_id}")
async def update_document(doc_id: str, body: DocPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, MANAGE, d.plantId)
    if d.status == "OBSOLETE":
        raise HTTPException(409, "An obsolete document cannot be edited")
    fields = body.model_fields_set
    if "plantId" in fields and body.plantId != d.plantId:
        await _require_doc(db, user, MANAGE, body.plantId)
        d.plantId = body.plantId
    if body.title is not None:
        d.title = body.title.strip()
    if "summary" in fields:
        d.summary = (body.summary or "").strip() or None
    if body.categoryId is not None:
        await _check_category(db, body.categoryId)
        d.categoryId = body.categoryId
    if body.docType is not None:
        _check_choice(body.docType, tuple(svc.DOC_TYPES), "docType")
        d.docType = body.docType  # the number keeps its original prefix: a document number never changes
    if "standardRef" in fields:
        d.standardRef = (body.standardRef or "").strip() or None
    if "department" in fields:
        d.department = (body.department or "").strip() or None
    if body.ownerUserId is not None:
        d.ownerUserId = body.ownerUserId
    if body.tags is not None:
        d.tags = svc.normalize_tags(body.tags)
    if body.ackRequired is not None:
        d.ackRequired = body.ackRequired
    if body.audienceRoles is not None:
        d.audienceRoles = _clean_roles(body.audienceRoles)
    if body.reviewIntervalMonths is not None:
        d.reviewIntervalMonths = body.reviewIntervalMonths
    if "reviewDueDate" in fields and body.reviewDueDate is not None:
        d.reviewDueDate = body.reviewDueDate
    d.updatedAt = svc.utcnow()
    await db.flush()
    await _refresh_search_text(db, d)
    await db.commit()
    await db.refresh(d)
    return await _detail(db, user, d, await _caps(db, user))


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, MANAGE, d.plantId)
    blockers = svc.delete_blockers(doc_status=d.status, current_version_id=d.currentVersionId)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    d.isDeleted = True
    d.deletedAt = svc.utcnow()
    d.deletedBy = user.id
    d.deletionReason = "Draft deleted"
    await db.commit()
    return {"ok": True}


# ── upload + versions ────────────────────────────────────────────────────────
class UploadInitIn(BaseModel):
    fileName: str = Field(min_length=1, max_length=200)
    mimeType: str | None = None
    sizeBytes: int = Field(gt=0)


class VersionIn(BaseModel):
    storagePath: str = Field(min_length=5, max_length=500)
    fileName: str = Field(min_length=1, max_length=200)
    mimeType: str | None = None
    changeNote: str | None = Field(None, max_length=2000)


def _storage_guard() -> None:
    if not is_storage_configured():
        raise HTTPException(503, "File storage is not configured on this server.")


def _validate_file(file_name: str, mime: str | None, size: int | None) -> str:
    resolved = svc.resolve_mime(file_name, mime)
    if not resolved:
        raise HTTPException(422, "File type not allowed. Upload PDF, Word (DOCX), Excel (XLSX), PowerPoint (PPTX), an image (PNG, JPG, WEBP) or a text file.")
    if size is not None and size > svc.MAX_FILE_BYTES:
        raise HTTPException(422, f"File exceeds the {svc.MAX_FILE_BYTES // 1024 // 1024} MB limit.")
    return resolved


@router.post("/documents/{doc_id}/upload-url")
async def upload_url(doc_id: str, body: UploadInitIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, MANAGE, d.plantId)
    pending = await _pending_version(db, d.id)
    blockers = svc.upload_blockers(doc_status=d.status, pending_status=pending.status if pending else None)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    mime = _validate_file(body.fileName, body.mimeType, body.sizeBytes)
    _storage_guard()
    path = svc.build_kb_storage_path(d.id, body.fileName)
    try:
        signed = await asyncio.to_thread(create_signed_upload_url, path)
    except Exception as e:  # noqa: BLE001
        log.warning("[kb] signed upload url failed: %s", e)
        raise HTTPException(503, "Could not start the upload. Try again shortly.") from e
    return {"uploadUrl": signed["uploadUrl"], "token": signed.get("token", ""), "storagePath": path, "mimeType": mime,
            "maxBytes": svc.MAX_FILE_BYTES}


def _try_delete(path: str) -> None:
    try:
        delete_storage_object(path)
    except Exception as e:  # noqa: BLE001
        log.warning("[kb] could not remove rejected upload %s: %s", path, e)


@router.post("/documents/{doc_id}/versions", status_code=201)
async def register_version(doc_id: str, body: VersionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, MANAGE, d.plantId)
    if not body.storagePath.startswith(f"kb/{d.id}/") or ".." in body.storagePath:
        raise HTTPException(422, "storagePath does not belong to this document")
    pending = await _pending_version(db, d.id)
    blockers = svc.upload_blockers(doc_status=d.status, pending_status=pending.status if pending else None)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    mime = _validate_file(body.fileName, body.mimeType, None)
    _storage_guard()
    try:
        data = await asyncio.to_thread(download_object, body.storagePath)
    except Exception as e:  # noqa: BLE001
        log.warning("[kb] uploaded object not readable %s: %s", body.storagePath, e)
        raise HTTPException(409, "The uploaded file was not found in storage. Upload it again.") from e
    if len(data) > svc.MAX_FILE_BYTES:
        await asyncio.to_thread(_try_delete, body.storagePath)
        raise HTTPException(422, f"File exceeds the {svc.MAX_FILE_BYTES // 1024 // 1024} MB limit.")
    if not svc.sniff_matches(data, mime):
        await asyncio.to_thread(_try_delete, body.storagePath)
        raise HTTPException(422, "The file's content does not match its type. Upload the original file.")
    text, ext_status = await asyncio.to_thread(svc.extract_text, data, mime)

    if pending is not None and pending.status == "DRAFT":
        pending.status = "DISCARDED"  # a newer upload replaces the unsubmitted draft
        # Flushed on its own: the DDL allows one pending version per document
        # (partial unique index), so the old draft must leave first.
        await db.flush()
    last = (await db.execute(
        select(func.max(KbDocumentVersion.versionNo)).where(KbDocumentVersion.documentId == d.id)
    )).scalar() or 0
    v = KbDocumentVersion(
        documentId=d.id, versionNo=last + 1, status="DRAFT", storagePath=body.storagePath,
        fileName=body.fileName[:200], mimeType=mime, sizeBytes=len(data), sha256=svc.sha256_hex(data),
        extractedText=text or None, extractionStatus=ext_status, changeNote=(body.changeNote or "").strip() or None,
        uploadedBy=user.id, uploadedAt=svc.utcnow(),
    )
    db.add(v)
    d.updatedAt = svc.utcnow()
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(409, "Another version was registered at the same moment. Refresh and retry.") from e
    await _refresh_search_text(db, d)
    await db.commit()
    await db.refresh(d)
    return await _detail(db, user, d, await _caps(db, user))


@router.get("/documents/{doc_id}/versions/{vid}/url")
async def version_url(
    doc_id: str, vid: str, download: bool = Query(False),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    d, caps = await _get_readable_doc(db, user, doc_id)
    v = await db.get(KbDocumentVersion, vid, options=[defer(KbDocumentVersion.extractedText)])
    if v is None or v.documentId != d.id:
        raise HTTPException(404, "Version not found")
    if v.status not in ("PUBLISHED", "SUPERSEDED") and not caps.staff:
        raise HTTPException(404, "Version not found")
    _storage_guard()
    try:
        url = await asyncio.to_thread(create_signed_download_url, v.storagePath, 600, v.fileName if download else None)
    except Exception as e:  # noqa: BLE001
        log.warning("[kb] signed download url failed: %s", e)
        raise HTTPException(503, "Could not open the file right now. Try again shortly.") from e
    return {"url": url, "fileName": v.fileName, "mimeType": v.mimeType, "expiresIn": 600,
            "superseded": v.id != d.currentVersionId and v.status == "SUPERSEDED"}


# ── lifecycle ────────────────────────────────────────────────────────────────
class PublishIn(BaseModel):
    effectiveDate: datetime | None = None
    reviewDueDate: datetime | None = None
    note: str | None = Field(None, max_length=2000)


class NoteIn(BaseModel):
    note: str = Field(min_length=3, max_length=2000)


class ObsoleteIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    supersededByDocId: str | None = None


class ReviewedIn(BaseModel):
    note: str | None = Field(None, max_length=2000)
    nextReviewDueDate: datetime | None = None


@router.post("/documents/{doc_id}/submit")
async def submit_for_review(doc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, MANAGE, d.plantId)
    pending = await _pending_version(db, d.id)
    blockers = svc.submit_blockers(doc_status=d.status, pending_status=pending.status if pending else None)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    assert pending is not None
    pending.status = "UNDER_REVIEW"
    pending.submittedBy = user.id
    pending.submittedAt = svc.utcnow()
    pending.reviewNote = None
    d.status = svc.doc_status_after_submit(d.status)
    d.updatedAt = svc.utcnow()
    await db.commit()
    await db.refresh(d)
    return await _detail(db, user, d, await _caps(db, user))


@router.post("/documents/{doc_id}/publish")
async def publish(doc_id: str, body: PublishIn | None = None, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    body = body or PublishIn()
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, APPROVE, d.plantId)
    pending = await _pending_version(db, d.id)
    blockers = svc.publish_blockers(doc_status=d.status, pending_status=pending.status if pending else None)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    assert pending is not None
    now = svc.utcnow()
    effective = body.effectiveDate or now
    due = body.reviewDueDate or svc.default_review_due(effective, d.reviewIntervalMonths)
    if svc._aware(due) <= svc._aware(effective):
        raise HTTPException(422, "The review due date must be after the effective date")
    if d.currentVersionId:
        prev = await db.get(KbDocumentVersion, d.currentVersionId, options=[defer(KbDocumentVersion.extractedText)])
        if prev is not None:
            prev.status = "SUPERSEDED"
    pending.status = "PUBLISHED"
    pending.publishedBy = user.id
    pending.publishedAt = now
    if body.note:
        pending.reviewNote = body.note
    d.currentVersionId = pending.id
    d.status = "PUBLISHED"
    d.publishedAt = now
    d.effectiveDate = effective
    d.reviewDueDate = due
    d.updatedAt = now
    await db.flush()
    await _refresh_search_text(db, d)
    await db.commit()
    await db.refresh(d)
    return await _detail(db, user, d, await _caps(db, user))


@router.post("/documents/{doc_id}/reject")
async def send_back(doc_id: str, body: NoteIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, APPROVE, d.plantId)
    pending = await _pending_version(db, d.id)
    blockers = svc.reject_blockers(pending_status=pending.status if pending else None)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    assert pending is not None
    pending.status = "DRAFT"
    pending.reviewNote = body.note
    d.status = svc.doc_status_after_reject(d.status)
    d.updatedAt = svc.utcnow()
    await db.commit()
    await db.refresh(d)
    return await _detail(db, user, d, await _caps(db, user))


@router.post("/documents/{doc_id}/obsolete")
async def make_obsolete(doc_id: str, body: ObsoleteIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, MANAGE, d.plantId)
    blockers = svc.obsolete_blockers(doc_status=d.status)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    if body.supersededByDocId:
        if body.supersededByDocId == d.id:
            raise HTTPException(422, "A document cannot supersede itself")
        nxt = await db.get(KbDocument, body.supersededByDocId, options=[defer(KbDocument.searchText)])
        if nxt is None or nxt.isDeleted:
            raise HTTPException(422, "Superseding document not found")
    pending = await _pending_version(db, d.id)
    if pending is not None:
        pending.status = "DISCARDED"
    now = svc.utcnow()
    d.status = "OBSOLETE"
    d.obsoletedAt = now
    d.obsoletedById = user.id
    d.obsoleteReason = body.reason.strip()
    d.supersededByDocId = body.supersededByDocId
    d.updatedAt = now
    await db.commit()
    await db.refresh(d)
    return await _detail(db, user, d, await _caps(db, user))


@router.post("/documents/{doc_id}/mark-reviewed")
async def mark_reviewed(doc_id: str, body: ReviewedIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Periodic review found the document still valid: no new version, the
    review clock restarts."""
    d = await _get_doc(db, doc_id)
    await _require_doc(db, user, MANAGE, d.plantId)
    if d.status != "PUBLISHED":
        raise HTTPException(409, "Only a published document is periodically reviewed")
    now = svc.utcnow()
    due = body.nextReviewDueDate or svc.default_review_due(now, d.reviewIntervalMonths)
    if svc._aware(due) <= now:
        raise HTTPException(422, "The next review date must be in the future")
    d.lastReviewedAt = now
    d.lastReviewedBy = user.id
    d.reviewDueDate = due
    d.updatedAt = now
    await db.commit()
    await db.refresh(d)
    return await _detail(db, user, d, await _caps(db, user))


# ── manager queues ───────────────────────────────────────────────────────────
async def _review_due_docs(db: AsyncSession, user: User, caps: Caps, days: int) -> list[KbDocument]:
    plants = await _staff_plants(db, user, caps)
    stmt = select(KbDocument).options(defer(KbDocument.searchText)).where(
        KbDocument.isDeleted.is_(False), KbDocument.status == "PUBLISHED",
        KbDocument.reviewDueDate.is_not(None), KbDocument.reviewDueDate <= svc.utcnow() + timedelta(days=days),
    )
    if plants is not None:
        stmt = stmt.where(KbDocument.plantId.in_(plants or ["__none__"]))
    return list((await db.execute(stmt.order_by(KbDocument.reviewDueDate.asc()).limit(500))).scalars().all())


@router.get("/review-due")
async def review_due(days: int = Query(30, ge=0, le=365), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    caps = await _caps(db, user)
    if not caps.staff:
        raise HTTPException(403, "Only knowledge-bank managers see the review list")
    docs = await _review_due_docs(db, user, caps, days)
    items = await _out_many(db, docs)
    return {"items": items, "total": len(items), "overdue": sum(1 for i in items if i["reviewState"] == "OVERDUE")}


@router.get("/manage/queue")
async def manage_queue(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Work in flight: new drafts, revisions being drafted, and versions
    waiting for approval (including revisions of published documents)."""
    await _require(db, user, READ)
    caps = await _caps(db, user)
    if not caps.staff:
        raise HTTPException(403, "Only knowledge-bank managers see the work queue")
    plants = await _staff_plants(db, user, caps)
    pend_doc_ids = select(KbDocumentVersion.documentId).where(KbDocumentVersion.status.in_(svc.PENDING_VERSION))
    stmt = select(KbDocument).options(defer(KbDocument.searchText)).where(
        KbDocument.isDeleted.is_(False),
        or_(KbDocument.status.in_(("DRAFT", "UNDER_REVIEW")), and_(KbDocument.status == "PUBLISHED", KbDocument.id.in_(pend_doc_ids))),
    )
    if plants is not None:
        stmt = stmt.where(or_(KbDocument.plantId.in_(plants or ["__none__"]), KbDocument.plantId.is_(None)))
    docs = list((await db.execute(stmt.order_by(KbDocument.updatedAt.desc()).limit(500))).scalars().all())
    items = await _out_many(db, docs, with_pending=True)
    awaiting = [i for i in items if (i["pendingVersion"] or {}).get("status") == "UNDER_REVIEW"]
    drafting = [i for i in items if i not in awaiting]
    return {"awaitingApproval": awaiting, "inDraft": drafting}


# ── acknowledgements ─────────────────────────────────────────────────────────
@router.post("/documents/{doc_id}/acknowledge")
async def acknowledge(doc_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d, caps = await _get_readable_doc(db, user, doc_id)
    existing = (await db.execute(select(KbAcknowledgement).where(
        KbAcknowledgement.versionId == (d.currentVersionId or ""), KbAcknowledgement.userId == user.id,
    ))).scalars().first()
    if existing is None:
        viewed = (await db.execute(select(KbView.id).where(
            KbView.documentId == d.id, KbView.userId == user.id, KbView.versionId == d.currentVersionId,
        ).limit(1))).scalar() is not None
        blockers = svc.ack_blockers(doc_status=d.status, ack_required=d.ackRequired,
                                    current_version_id=d.currentVersionId, has_viewed=viewed)
        if blockers:
            raise HTTPException(409, " ".join(blockers))
        db.add(KbAcknowledgement(documentId=d.id, versionId=d.currentVersionId, userId=user.id, acknowledgedAt=svc.utcnow()))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()  # double click: the first one won, which is the same outcome
    return await _detail(db, user, d, caps)


async def _my_pending(db: AsyncSession, user: User) -> list[KbDocument]:
    docs = list((await db.execute(
        select(KbDocument).options(defer(KbDocument.searchText)).where(
            KbDocument.isDeleted.is_(False), KbDocument.status == "PUBLISHED", KbDocument.ackRequired.is_(True),
            KbDocument.currentVersionId.is_not(None),
        )
    )).scalars().all())
    if not docs:
        return []
    roles = set(await get_user_role_codes(db, user.id)) | {getattr(user, "role", None) or ""}
    plants = {getattr(user, "plantId", None) or ""}
    docs = [d for d in docs if svc.applies_to_user(doc_plant_id=d.plantId, audience_roles=d.audienceRoles,
                                                    user_plant_ids=plants, user_role_codes=roles)]
    done = set((await db.execute(select(KbAcknowledgement.versionId).where(
        KbAcknowledgement.userId == user.id, KbAcknowledgement.versionId.in_([d.currentVersionId for d in docs] or ["__none__"]),
    ))).scalars().all())
    return sorted((d for d in docs if d.currentVersionId not in done),
                  key=lambda d: svc._aware(d.publishedAt) if d.publishedAt else svc.utcnow())


@router.get("/my-acknowledgements")
async def my_acknowledgements(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    pending = await _my_pending(db, user)
    rows = (await db.execute(
        select(KbAcknowledgement, KbDocument, KbDocumentVersion.versionNo)
        .join(KbDocument, KbDocument.id == KbAcknowledgement.documentId)
        .join(KbDocumentVersion, KbDocumentVersion.id == KbAcknowledgement.versionId)
        .options(defer(KbDocument.searchText))
        .where(KbAcknowledgement.userId == user.id, KbDocument.isDeleted.is_(False))
        .order_by(KbAcknowledgement.acknowledgedAt.desc()).limit(300)
    )).all()
    done = [
        {"documentId": d.id, "docNo": d.docNo, "title": d.title, "status": d.status, "versionId": a.versionId,
         "versionNo": vno, "acknowledgedAt": _iso(a.acknowledgedAt), "isCurrentVersion": a.versionId == d.currentVersionId}
        for a, d, vno in rows
    ]
    return {"pending": await _out_many(db, pending), "acknowledged": done}


@router.get("/documents/{doc_id}/acknowledgements")
async def ack_report(
    doc_id: str, plantId: str | None = Query(None), department: str | None = Query(None),
    state: str = Query("all"), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Who has / has not acknowledged the current version, among the people
    the document applies to (its plant, or everyone for company-wide; narrowed
    to the audience roles when set). A plant-scoped manager sees their own
    plants' people only."""
    _check_choice(state, ("all", "pending", "done"), "state")
    d, caps = await _get_readable_doc(db, user, doc_id)
    if not caps.staff:
        raise HTTPException(403, "Only knowledge-bank managers see acknowledgement reports")
    if not d.currentVersionId:
        raise HTTPException(409, "The document has no published version yet")
    scope_plants = await _staff_plants(db, user, caps)
    if plantId and scope_plants is not None and plantId not in scope_plants:
        raise HTTPException(403, "Plant is outside your access scope")

    stmt = select(User.id, User.name, User.plantId, User.department, User.role)
    if d.plantId:
        stmt = stmt.where(User.plantId == d.plantId)
    if scope_plants is not None:
        stmt = stmt.where(User.plantId.in_(scope_plants or ["__none__"]))
    if plantId:
        stmt = stmt.where(User.plantId == plantId)
    if department:
        stmt = stmt.where(func.lower(User.department) == department.lower())
    roles = d.audienceRoles or []
    if roles:
        stmt = stmt.where(or_(
            User.role.in_(roles),
            User.id.in_(select(UserRole.userId).join(Role, Role.id == UserRole.roleId).where(Role.code.in_(roles))),
        ))
    cap = 5000
    people = (await db.execute(stmt.order_by(User.name).limit(cap + 1))).all()
    truncated = len(people) > cap
    people = people[:cap]
    acks = {r[0]: r[1] for r in (await db.execute(
        select(KbAcknowledgement.userId, KbAcknowledgement.acknowledgedAt).where(KbAcknowledgement.versionId == d.currentVersionId)
    )).all()}
    plant_names = await resolve_plant_names(db, [p.plantId for p in people])

    by_plant: dict[str, dict[str, Any]] = {}
    by_dept: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for p in people:
        done = p.id in acks
        pk = p.plantId or ""
        bp = by_plant.setdefault(pk, {"plantId": p.plantId, "plantName": plant_names.get(pk) or "No plant", "total": 0, "acknowledged": 0})
        dk = (p.department or "").strip() or "Unassigned"
        bd = by_dept.setdefault(dk, {"department": dk, "total": 0, "acknowledged": 0})
        for b in (bp, bd):
            b["total"] += 1
            b["acknowledged"] += int(done)
        if state == "all" or (state == "done") == done:
            rows.append({"userId": p.id, "name": p.name, "plantId": p.plantId, "plantName": plant_names.get(pk),
                         "department": p.department, "role": p.role, "acknowledged": done,
                         "acknowledgedAt": _iso(acks.get(p.id))})
    total = len(people)
    acked = sum(1 for p in people if p.id in acks)
    current = await db.get(KbDocumentVersion, d.currentVersionId, options=[defer(KbDocumentVersion.extractedText)])
    return {
        "documentId": d.id, "docNo": d.docNo, "versionId": d.currentVersionId, "versionNo": current.versionNo if current else None,
        "ackRequired": d.ackRequired, "audienceRoles": roles,
        "summary": {"audience": total, "acknowledged": acked, "pending": total - acked,
                    "percent": round(100 * acked / total, 1) if total else None},
        "byPlant": sorted(by_plant.values(), key=lambda x: x["plantName"]),
        "byDepartment": sorted(by_dept.values(), key=lambda x: x["department"]),
        "rows": rows[:2000], "truncated": truncated or len(rows) > 2000,
    }


# ── Ask the knowledge bank ───────────────────────────────────────────────────
class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    categoryId: str | None = None
    documentId: str | None = None


async def _ask_candidates(db: AsyncSession, body: AskIn, limit: int = 12) -> list[dict[str, Any]]:
    conds: list[Any] = [KbDocument.isDeleted.is_(False), KbDocument.status == "PUBLISHED", KbDocument.currentVersionId.is_not(None)]
    if body.documentId:
        conds.append(KbDocument.id == body.documentId)
    if body.categoryId:
        conds.append(KbDocument.categoryId.in_(_descendants(await _categories(db), body.categoryId)))
    base = select(KbDocument.id, KbDocument.docNo, KbDocument.title, KbDocument.currentVersionId)
    rows: list[Any] = []
    if _is_pg(db):
        tsq = svc.fts_or_query(body.question)
        if tsq is not None:
            rows = (await db.execute(base.where(*conds, svc.fts_match(tsq)).order_by(svc.fts_rank(tsq).desc()).limit(limit))).all()
    if not rows:
        like = svc.ilike_terms(body.question, any_term=True)
        if like is not None:
            rows = (await db.execute(base.where(*conds, like).order_by(KbDocument.viewCount.desc()).limit(limit))).all()
    if not rows and body.documentId:
        rows = (await db.execute(base.where(*conds))).all()
    if not rows:
        return []
    vers = {v.id: v for v in (await db.execute(
        select(KbDocumentVersion.id, KbDocumentVersion.versionNo, KbDocumentVersion.extractedText)
        .where(KbDocumentVersion.id.in_([r.currentVersionId for r in rows]))
    )).all()}
    out = []
    for r in rows:
        v = vers.get(r.currentVersionId)
        if v is not None and v.extractedText:
            out.append({"documentId": r.id, "docNo": r.docNo, "title": r.title, "versionId": v.id,
                        "versionNo": v.versionNo, "text": v.extractedText})
    return out


@router.post("/ask")
async def ask(body: AskIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Answer from published documents only, with a citation (document number,
    version, quote) behind every claim. Without AI the endpoint still returns
    the best-matching passages, so the page degrades to a passage search."""
    await _require(db, user, READ)
    question = re.sub(r"\s+", " ", body.question).strip()
    docs = await _ask_candidates(db, body)
    passages = await asyncio.to_thread(svc.rank_passages, question, docs)
    base = {"question": question, "passages": svc.passages_out(passages), "aiAvailable": ai.is_configured()}
    if not passages:
        return {**base, "covered": False, "answer": svc.NOT_COVERED, "citations": [], "sources": [], "mode": "none"}
    if not ai.is_configured():
        return {**base, "covered": False, "answer": None, "citations": [], "sources": [], "mode": "passages",
                "message": "AI answers are not available right now. These are the passages that best match your question."}
    started = time.monotonic()
    result = await ai.complete_json(system=svc.ASK_SYSTEM, user=svc.build_ask_prompt(question, passages), max_tokens=1500)
    # Model and latency are logged here, never returned to the browser.
    log.info("[kb.ask] model=%s latency_ms=%d passages=%d ok=%s", get_settings().anthropic_model,
             int((time.monotonic() - started) * 1000), len(passages), result is not None)
    if result is None:
        return {**base, "covered": False, "answer": None, "citations": [], "sources": [], "mode": "passages",
                "message": "Could not compose an answer right now. These are the passages that best match your question."}
    return {**base, **svc.format_answer(result, passages), "mode": "answer"}
