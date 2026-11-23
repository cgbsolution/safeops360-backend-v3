"""CSM contractor portal: the contractor fills its prequalification by link.
UNAUTHENTICATED, mounted at /api/csm-portal.

Same three properties as routers/supplier_portal.py:
  1. No get_current_user. The contractor has no seat.
  2. Scope comes from the token row (one application), never from the request.
     Every write is re-validated against that application.
  3. Every attempt is logged (CsmPortalAccessLog); every token failure answers
     the same 404.
What crosses the boundary is an explicit allow-list: question text and answer
types, never weights, scoring bands, critical flags, scores, overrides or
reviewer notes.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.csm import CsmDocument, CsmPrequalification
from app.models.epc import ContractorCompany
from app.routers import csm as base
from app.services import csm as svc
from app.services import csm_portal as portal
from app.services.plant_directory import resolve_plant_names

router = APIRouter(prefix="/api/csm-portal", tags=["csm-portal"])

_TOKEN_FAIL = "This link is not valid. It may have expired or been withdrawn."
_PROFILE_FIELDS = ("tradeName", "registrationNumber", "panNumber", "gstNumber", "contactPerson", "contactEmail",
                   "contactPhone", "safetyOfficerName", "safetyOfficerPhone")


def _ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


async def _resolve(db: AsyncSession, token: str, request: Request, *, action: str, is_write: bool = False):
    try:
        row = await portal.resolve_token(db, token, action=action, is_write=is_write, ip=_ip(request),
                                         user_agent=request.headers.get("user-agent"))
    except portal.PortalError as e:
        await db.commit()  # keep the access-log row
        raise HTTPException(e.status_code, "Too many requests. Please wait a few minutes and try again."
                            if e.status_code == 429 else _TOKEN_FAIL) from e
    pq = await db.get(CsmPrequalification, row.prequalificationId)
    if pq is None or pq.isDeleted:
        await db.commit()
        raise HTTPException(404, _TOKEN_FAIL)
    return row, pq


def _editable(pq: CsmPrequalification) -> None:
    if pq.status != "DRAFT":
        raise HTTPException(409, "This questionnaire has been submitted and can no longer be changed.")


def _public_sections(snapshot: dict) -> list[dict]:
    out = []
    for s in snapshot.get("sections") or []:
        out.append({"code": s.get("code"), "title": s.get("title"), "questions": [
            {"code": q.get("code"), "text": q.get("text"), "answerType": q.get("answerType"),
             "required": bool(q.get("required", svc.is_scored(q))) and q.get("answerType") != "DOCUMENT_REQUIRED",
             "help": q.get("help"), "docType": q.get("docType")}
            for q in s.get("questions") or []
        ]})
    return out


def _public_doc(d: CsmDocument) -> dict[str, Any]:
    return {"id": d.id, "docType": d.docType, "title": d.title, "documentNo": d.documentNo, "fileName": d.fileName,
            "questionCode": d.questionCode, "expiresOn": base.iso(d.expiresOn), "state": svc.expiry_state(d.expiresOn),
            "uploadedVia": d.uploadedVia, "createdAt": base.iso(d.createdAt)}


async def _view(db: AsyncSession, row, pq: CsmPrequalification) -> dict[str, Any]:
    c = await db.get(ContractorCompany, pq.contractorCompanyId)
    plants = await resolve_plant_names(db, [pq.plantId])
    docs = (await db.execute(
        select(CsmDocument).where(CsmDocument.contractorCompanyId == pq.contractorCompanyId)
        .where(CsmDocument.isDeleted.is_(False))
        .where(or_(CsmDocument.prequalificationId == pq.id, CsmDocument.status == "ACTIVE"))
        .order_by(CsmDocument.createdAt.desc())
    )).scalars().all()
    returned = next((h.get("note") for h in reversed(pq.history or []) if h.get("to") == "DRAFT" and h.get("from")), None)
    sc = svc.score_application((pq.templateSnapshot or {}).get("sections") or [], pq.answers or {}, {},
                               await svc.valid_doc_types(db, pq.contractorCompanyId))
    return {
        "application": {"applicationNo": pq.applicationNo, "status": pq.status, "editable": pq.status == "DRAFT",
                        "plantName": plants.get(pq.plantId), "submittedAt": base.iso(pq.submittedAt),
                        "returnedNote": returned if pq.status == "DRAFT" else None,
                        "templateName": (pq.templateSnapshot or {}).get("name")},
        "company": {"name": c.name if c else None, **{f: getattr(c, f, None) for f in _PROFILE_FIELDS}},
        "sections": _public_sections(pq.templateSnapshot or {}),
        "answers": pq.answers or {},
        "unanswered": sc["unanswered"],
        "documents": [_public_doc(d) for d in docs],
        "docTypes": svc.DOC_TYPES,
        "contactName": row.contactName,
        "expiresAt": base.iso(row.expiresAt),
    }


@router.get("/{token}")
async def view(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row, pq = await _resolve(db, token, request, action="VIEW")
    out = await _view(db, row, pq)
    await db.commit()
    return out


class AnswersIn(BaseModel):
    answers: dict[str, dict[str, Any]]


@router.put("/{token}/answers")
async def save_answers(token: str, body: AnswersIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row, pq = await _resolve(db, token, request, action="ANSWERS", is_write=True)
    _editable(pq)
    base.apply_answers(pq, body.answers)
    await db.commit()
    return await _view(db, row, pq)


class ProfileIn(BaseModel):
    tradeName: str | None = Field(None, max_length=200)
    registrationNumber: str | None = Field(None, max_length=100)
    panNumber: str | None = Field(None, max_length=20)
    gstNumber: str | None = Field(None, max_length=20)
    contactPerson: str | None = Field(None, max_length=120)
    contactEmail: str | None = Field(None, max_length=200)
    contactPhone: str | None = Field(None, max_length=30)
    safetyOfficerName: str | None = Field(None, max_length=120)
    safetyOfficerPhone: str | None = Field(None, max_length=30)


@router.put("/{token}/profile")
async def save_profile(token: str, body: ProfileIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row, pq = await _resolve(db, token, request, action="PROFILE", is_write=True)
    _editable(pq)
    c = await db.get(ContractorCompany, pq.contractorCompanyId)
    for k, v in body.model_dump(exclude_unset=True).items():
        v = (v or "").strip() or None
        if k in ("panNumber", "gstNumber") and v:
            v = v.upper()
        setattr(c, k, v)
    c.representativeName = c.representativeName or c.contactPerson
    c.representativeEmail = c.representativeEmail or c.contactEmail
    c.representativePhone = c.representativePhone or c.contactPhone
    await db.commit()
    return await _view(db, row, pq)


class UploadUrlIn(BaseModel):
    fileName: str = Field(min_length=1, max_length=200)
    mimeType: str
    fileSize: int = Field(gt=0, le=svc.DOC_MAX_BYTES)


@router.post("/{token}/upload-url")
async def upload_url(token: str, body: UploadUrlIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Signed upload target under a token-scoped path, so contractor uploads are
    segregated from internal files in storage as well as in the database."""
    row, pq = await _resolve(db, token, request, action="UPLOAD_URL", is_write=True)
    await db.commit()
    _editable(pq)
    if body.mimeType not in svc.DOC_MIME:
        raise HTTPException(400, "Please upload a PDF or a photo (JPG, PNG, WEBP, HEIC).")
    st = base._storage()
    if not st.is_storage_configured():
        raise HTTPException(503, "File upload is not available.")
    path = f"csm-portal/{row.id}/{secrets.token_hex(4)}-{base.safe_name(body.fileName)}"
    try:
        signed = st.create_signed_upload_url(path)
    except RuntimeError as e:
        raise HTTPException(503, "File upload is not available.") from e
    return {"storagePath": path, **signed}


class DocumentIn(BaseModel):
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


@router.post("/{token}/documents", status_code=201)
async def add_document(token: str, body: DocumentIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row, pq = await _resolve(db, token, request, action="DOCUMENT", is_write=True)
    _editable(pq)
    # Only a path we issued for THIS token; otherwise a contractor could attach
    # any object in the bucket, internal documents included.
    if not body.storagePath.startswith(f"csm-portal/{row.id}/"):
        raise HTTPException(400, "Unrecognised upload reference.")
    if body.questionCode and svc.find_question((pq.templateSnapshot or {}).get("sections") or [], body.questionCode) is None:
        raise HTTPException(422, "Unknown question.")
    d = await base.record_document(
        db, base.DocumentIn(contractorCompanyId=pq.contractorCompanyId, prequalificationId=pq.id, **body.model_dump()),
        via="PORTAL", by_id=None, by_name=row.contactName or row.contactEmail,
    )
    await db.commit()
    await db.refresh(d)
    return _public_doc(d)


class SubmitIn(BaseModel):
    declarantName: str = Field(min_length=2, max_length=120)
    declaration: bool


@router.post("/{token}/submit")
async def submit(token: str, body: SubmitIn, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row, pq = await _resolve(db, token, request, action="SUBMIT", is_write=True)
    if not body.declaration:
        raise HTTPException(422, "Confirm the declaration before submitting.")
    sc = await base.rescore(db, pq)
    blockers = svc.submit_blockers(status=pq.status, unanswered=sc["unanswered"])
    if blockers:
        await db.commit()
        raise HTTPException(409, " ".join(blockers))
    pq.history = [*(pq.history or []), {"at": svc.utcnow().isoformat(), "byId": None,
                                        "byName": f"{body.declarantName} (contractor)", "from": "DRAFT",
                                        "to": "SUBMITTED", "note": "Submitted through the contractor link"}]
    pq.status = "SUBMITTED"
    pq.submittedAt, pq.submittedVia, pq.submittedByName = svc.utcnow(), "PORTAL", body.declarantName.strip()
    await db.commit()
    return await _view(db, row, pq)
