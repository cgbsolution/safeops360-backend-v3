"""Process Safety Information + Chem advisor router (JSL Hisar "PSI advisor /
Chem advisor"). Mounts at /api/psi.

  /chemicals        plant-wide chemical register over the Facilities
                    HazardousMaterial rows (read-only here; the rows are still
                    created and edited in /facilities). Filters: GHS class,
                    signal word, SDS missing / expired, over max quantity,
                    storage location, hazard group.
  /compatibility    rule-based compatibility matrix for 2+ chemicals
                    (services/psi_compat.py; rules are data, unit-tested).
  /documents        PSI document library with revision history; files go to
                    platform storage through short-lived signed URLs. An SDS is
                    a document with docType SDS linked to a chemical.
  /completeness     which required PSI document types each unit / area holds.
  /advisor/ask      Chem advisor: answers grounded in the register + SDS
                    metadata + compatibility result, with citations. Soft-fails
                    to a rule-based answer when AI is not configured.

RBAC (granted by safeops_360/prisma/apply-pssr-psi-permissions.ts):
  PSI.READ    view the register, documents, completeness; use the advisor (wide)
  PSI.MANAGE  add document revisions, upload files (incl. SDS), delete revisions

Mounted ungated like ims: per-endpoint RBAC is the gate.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.factory import FactoryProfile
from app.models.factory_ext import HazardousMaterial
from app.models.psi import PsiDocument
from app.models.user import User
from app.services import psi as svc
from app.services import psi_compat as compat
from app.services import storage
from app.services.access_scope import build_query_scope
from app.services.ai import anthropic_client as ai
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names
from app.services.user_directory import resolve_user_directory

router = APIRouter(prefix="/api/psi", tags=["psi"])
log = logging.getLogger("safeops.psi.advisor")

READ, MANAGE = "PSI.READ", "PSI.MANAGE"
MAX_FILE_SIZE = 50 * 1024 * 1024  # P&IDs and design packs are large PDFs
ALLOWED_MIME = {
    "application/pdf", "image/png", "image/jpeg", "image/webp", "image/tiff",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/msword", "application/vnd.ms-excel", "text/csv", "text/plain",
    "application/acad", "image/vnd.dwg", "application/dxf", "application/octet-stream",
}


# ── helpers ──────────────────────────────────────────────────────────────────
async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


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


def _has_file(d: PsiDocument) -> bool:
    return bool(d.storagePath and d.fileUploadedAt)


def _doc_dict(d: PsiDocument) -> dict[str, Any]:
    """Plain view used by the pure rules (completeness, SDS status)."""
    return {"id": d.id, "unitArea": d.unitArea, "docType": d.docType, "status": d.status, "reviewDue": d.reviewDue,
            "issuedDate": d.issuedDate, "hasFile": _has_file(d), "docNo": d.docNo, "revision": d.revision}


def _doc_row(d: PsiDocument, users: dict[str, str], plants: dict[str, str], chem_names: dict[str, str] | None = None) -> dict[str, Any]:
    due = svc.effective_review_due(d.reviewDue, d.issuedDate) if d.docType == "SDS" else d.reviewDue
    return {
        "id": d.id, "plantId": d.plantId, "plantName": plants.get(d.plantId), "unitArea": d.unitArea,
        "docType": d.docType, "docTypeLabel": svc.DOC_TYPE_LABEL.get(d.docType, d.docType), "docNo": d.docNo,
        "revision": d.revision, "title": d.title, "description": d.description,
        "hazardousMaterialId": d.hazardousMaterialId, "chemicalName": (chem_names or {}).get(d.hazardousMaterialId or ""),
        "status": d.status, "supersedesId": d.supersedesId, "issuedDate": _iso(d.issuedDate), "reviewDue": _iso(due),
        "reviewOverdue": svc.doc_review_overdue(d.status, due), "hasFile": _has_file(d), "fileName": d.fileName,
        "fileSize": d.fileSize, "mimeType": d.mimeType, "fileUploadedAt": _iso(d.fileUploadedAt),
        "uploadedByName": users.get(d.uploadedById or ""), "createdAt": _iso(d.createdAt),
        "createdByName": users.get(d.createdBy or ""),
    }


def _current_first(docs) -> list[PsiDocument]:
    """Revision history: the CURRENT revision first, then newest first (stable
    sort keeps the createdAt order the query produced)."""
    return sorted(docs, key=lambda d: 0 if d.status == "CURRENT" else 1)


async def _doc(db: AsyncSession, did: str) -> PsiDocument:
    d = await db.get(PsiDocument, did)
    if d is None or d.isDeleted:
        raise HTTPException(404, "PSI document not found")
    return d


# ── meta ─────────────────────────────────────────────────────────────────────
@router.get("/meta")
async def meta(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "docTypes": svc.DOC_TYPES, "docTypeLabels": svc.DOC_TYPE_LABEL, "docStatuses": svc.DOC_STATUSES,
        "requiredUnitDocTypes": svc.REQUIRED_UNIT_DOC_TYPES, "sdsStatuses": svc.SDS_STATUSES,
        "sdsReviewYears": svc.SDS_REVIEW_YEARS, "hazardGroups": compat.GROUP_LABEL, "verdicts": compat.VERDICTS,
        "signalWords": ("DANGER", "WARNING", "NONE"),
    }


# ── chemical register ────────────────────────────────────────────────────────
async def _load_chemicals(db: AsyncSession, plants: list[str] | None, ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Register rows in scope with derived flags: hazard groups, SDS status,
    over-max, utilisation, storage label, facility and plant names."""
    stmt = select(HazardousMaterial).where(HazardousMaterial.isDeleted.is_(False)).where(_plant_where(HazardousMaterial.siteId, plants))
    if ids is not None:
        stmt = stmt.where(HazardousMaterial.id.in_(ids or ["__none__"]))
    rows = (await db.execute(stmt.order_by(HazardousMaterial.chemicalName).limit(3000))).scalars().all()
    if not rows:
        return []
    prof_ids = {r.factoryProfileId for r in rows}
    profiles = {p.id: p.factoryName for p in (await db.execute(
        select(FactoryProfile).where(FactoryProfile.id.in_(prof_ids)))).scalars().all()}
    sds_rows = (await db.execute(
        select(PsiDocument).where(PsiDocument.isDeleted.is_(False)).where(PsiDocument.docType == "SDS")
        .where(PsiDocument.status == "CURRENT").where(PsiDocument.hazardousMaterialId.in_([r.id for r in rows]))
        .order_by(PsiDocument.createdAt.desc())
    )).scalars().all()
    sds_by_chem: dict[str, PsiDocument] = {}
    for d in sds_rows:
        cur = sds_by_chem.get(d.hazardousMaterialId)
        # Prefer one with a file; among equals, the newest (query order).
        if cur is None or (not _has_file(cur) and _has_file(d)):
            sds_by_chem[d.hazardousMaterialId] = d
    plant_names = await resolve_plant_names(db, {r.siteId for r in rows})
    now = svc.utcnow()
    out = []
    for r in rows:
        c = compat.Chem(id=r.id, name=r.chemicalName, casNumber=r.casNumber, ghsHazardClasses=r.ghsHazardClasses or [],
                        ghsPictograms=r.ghsPictograms or [], incompatibleSubstances=r.incompatibleSubstances or [])
        groups = sorted(compat.derive_groups(c))
        sds = sds_by_chem.get(r.id)
        sds_d = _doc_dict(sds) if sds else None
        st = svc.sds_status(sds_d, now)
        due = svc.effective_review_due(sds.reviewDue, sds.issuedDate) if sds else None
        out.append({
            "id": r.id, "chemicalName": r.chemicalName, "casNumber": r.casNumber,
            "plantId": r.siteId, "plantName": plant_names.get(r.siteId), "factoryProfileId": r.factoryProfileId,
            "facilityName": profiles.get(r.factoryProfileId),
            "hazmatClassification": r.hazmatClassification, "regulatoryClassification": r.regulatoryClassification,
            "ghsSignalWord": r.ghsSignalWord, "ghsHazardClasses": r.ghsHazardClasses or [], "ghsPictograms": r.ghsPictograms or [],
            "hazardGroups": groups, "hazardGroupLabels": [compat.GROUP_LABEL[g] for g in groups],
            "quantityStored": r.quantityStored, "unit": r.unit, "maxAllowableQty": r.maxAllowableQty,
            "overMax": svc.over_max(r.quantityStored, r.maxAllowableQty),
            "utilisationPct": svc.utilisation(r.quantityStored, r.maxAllowableQty),
            "storageBuilding": r.storageBuilding, "storageRoom": r.storageRoom,
            "storage": svc.storage_label(r.storageBuilding, r.storageRoom), "containerType": r.containerType,
            "containerCount": r.containerCount, "secondaryContainmentPresent": r.secondaryContainmentPresent,
            "ventilationAvailable": r.ventilationAvailable, "signagePresent": r.signagePresent,
            "expiryDate": _iso(r.expiryDate), "sdsDocId": r.sdsDocId, "sdsVersion": r.sdsVersion,
            "sdsGhsCompliant": r.sdsGhsCompliant, "ppeRequired": r.ppeRequired or [],
            "incompatibleSubstances": r.incompatibleSubstances or [], "spillKitLocation": r.spillKitLocation,
            "emergencyContact": r.emergencyContact,
            "sdsStatus": st,
            "sdsDocument": ({"id": sds.id, "docNo": sds.docNo, "revision": sds.revision, "hasFile": _has_file(sds),
                             "issuedDate": _iso(sds.issuedDate), "reviewDue": _iso(due)} if sds else None),
        })
    return out


def _filter_chems(rows: list[dict[str, Any]], *, ghsClass: str | None, signalWord: str | None, sds: str | None,
                  overMax: bool, storage_q: str | None, hazardGroup: str | None, q: str | None) -> list[dict[str, Any]]:
    out = rows
    if ghsClass:
        g = ghsClass.lower()
        out = [r for r in out if any(g == c.lower() for c in r["ghsHazardClasses"])]
    if signalWord:
        out = [r for r in out if (r["ghsSignalWord"] or "NONE").upper() == signalWord.upper()]
    if sds == "MISSING":
        out = [r for r in out if r["sdsStatus"] == "MISSING"]
    elif sds == "EXPIRED":
        out = [r for r in out if r["sdsStatus"] == "EXPIRED"]
    elif sds == "ATTENTION":
        out = [r for r in out if r["sdsStatus"] != "CURRENT"]
    elif sds:
        out = [r for r in out if r["sdsStatus"] == sds]
    if overMax:
        out = [r for r in out if r["overMax"]]
    if storage_q:
        out = [r for r in out if (r["storage"] or "") == storage_q]
    if hazardGroup:
        out = [r for r in out if hazardGroup in r["hazardGroups"]]
    if q:
        ql = q.lower()
        out = [r for r in out if ql in (r["chemicalName"] or "").lower() or ql in (r["casNumber"] or "").lower()]
    return out


@router.get("/chemicals")
async def list_chemicals(
    plantId: str | None = Query(None), ghsClass: str | None = Query(None), signalWord: str | None = Query(None),
    sds: str | None = Query(None, description="MISSING | EXPIRED | DUE_SOON | CURRENT | ATTENTION"),
    overMax: bool = Query(False), storageLocation: str | None = Query(None), hazardGroup: str | None = Query(None),
    q: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    rows = await _load_chemicals(db, plants)
    items = _filter_chems(rows, ghsClass=ghsClass, signalWord=signalWord, sds=sds, overMax=overMax,
                          storage_q=storageLocation, hazardGroup=hazardGroup, q=q)
    return {
        "items": items, "total": len(items), "registerTotal": len(rows),
        "summary": {
            "sdsMissing": sum(1 for r in rows if r["sdsStatus"] == "MISSING"),
            "sdsExpired": sum(1 for r in rows if r["sdsStatus"] == "EXPIRED"),
            "sdsDueSoon": sum(1 for r in rows if r["sdsStatus"] == "DUE_SOON"),
            "overMax": sum(1 for r in rows if r["overMax"]),
            "danger": sum(1 for r in rows if (r["ghsSignalWord"] or "").upper() == "DANGER"),
            "noContainment": sum(1 for r in rows if not r["secondaryContainmentPresent"]),
        },
        "facets": {
            "ghsClasses": sorted({c for r in rows for c in r["ghsHazardClasses"] if c}, key=str.lower),
            "storageLocations": sorted({r["storage"] for r in rows if r["storage"]}, key=str.lower),
            "hazardGroups": sorted({g for r in rows for g in r["hazardGroups"]}),
        },
    }


@router.get("/chemicals/{cid}")
async def get_chemical(cid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    h = await db.get(HazardousMaterial, cid)
    if h is None or h.isDeleted:
        raise HTTPException(404, "Chemical not found")
    await _require(db, user, READ, h.siteId)
    rows = await _load_chemicals(db, [h.siteId], [cid])
    docs = _current_first((await db.execute(
        select(PsiDocument).where(PsiDocument.isDeleted.is_(False)).where(PsiDocument.hazardousMaterialId == cid)
        .order_by(PsiDocument.createdAt.desc())
    )).scalars().all())
    users = {k: v.name for k, v in (await resolve_user_directory(db, [d.uploadedById for d in docs] + [d.createdBy for d in docs])).items()}
    plants = await resolve_plant_names(db, [h.siteId])
    return {**rows[0], "sdsHistory": [_doc_row(d, users, plants) for d in docs]}


# ── compatibility ────────────────────────────────────────────────────────────
class CompatIn(BaseModel):
    chemicalIds: list[str] = Field(min_length=2, max_length=20)


def _chem_obj(r: dict[str, Any]) -> compat.Chem:
    return compat.Chem(id=r["id"], name=r["chemicalName"], casNumber=r["casNumber"], ghsHazardClasses=r["ghsHazardClasses"],
                       ghsPictograms=r["ghsPictograms"], incompatibleSubstances=r["incompatibleSubstances"])


@router.post("/compatibility")
async def compatibility(body: CompatIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, None)
    ids = list(dict.fromkeys(body.chemicalIds))
    if len(ids) < 2:
        raise HTTPException(422, "Pick at least two different chemicals")
    rows = await _load_chemicals(db, plants, ids)
    if len(rows) != len(ids):
        raise HTTPException(404, "One or more chemicals were not found in your register scope")
    order = {cid: i for i, cid in enumerate(ids)}
    rows.sort(key=lambda r: order[r["id"]])
    result = compat.matrix([_chem_obj(r) for r in rows])
    for c, r in zip(result["chemicals"], rows):
        c.update({"storage": r["storage"], "facilityName": r["facilityName"], "plantName": r["plantName"]})
    return result


@router.get("/compatibility/rules")
async def compatibility_rules(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    return {"rules": compat.rules_as_data(), "groups": compat.GROUP_LABEL}


# ── PSI documents ────────────────────────────────────────────────────────────
@router.get("/documents")
async def list_documents(
    plantId: str | None = Query(None), docType: str | None = Query(None), unitArea: str | None = Query(None),
    status_: str | None = Query("CURRENT", alias="status"), hazardousMaterialId: str | None = Query(None),
    reviewOverdue: bool = Query(False), q: str | None = Query(None),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    stmt = select(PsiDocument).where(PsiDocument.isDeleted.is_(False)).where(_plant_where(PsiDocument.plantId, plants))
    if status_ and status_ != "ALL":
        stmt = stmt.where(PsiDocument.status == status_)
    if docType:
        stmt = stmt.where(PsiDocument.docType == docType)
    if unitArea:
        stmt = stmt.where(PsiDocument.unitArea == unitArea)
    if hazardousMaterialId:
        stmt = stmt.where(PsiDocument.hazardousMaterialId == hazardousMaterialId)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(PsiDocument.docNo.ilike(like), PsiDocument.title.ilike(like)))
    rows = (await db.execute(stmt.order_by(PsiDocument.docType, PsiDocument.docNo, PsiDocument.createdAt.desc()).limit(2000))).scalars().all()
    users = {k: v.name for k, v in (await resolve_user_directory(db, [d.uploadedById for d in rows] + [d.createdBy for d in rows])).items()}
    pl = await resolve_plant_names(db, [d.plantId for d in rows])
    chem_ids = [d.hazardousMaterialId for d in rows if d.hazardousMaterialId]
    chem_names: dict[str, str] = {}
    if chem_ids:
        chem_names = {i: n for i, n in (await db.execute(
            select(HazardousMaterial.id, HazardousMaterial.chemicalName).where(HazardousMaterial.id.in_(chem_ids))
        )).all()}
    items = [_doc_row(d, users, pl, chem_names) for d in rows]
    if reviewOverdue:
        items = [i for i in items if i["reviewOverdue"]]
    units = sorted({d.unitArea for d in rows if d.unitArea}, key=str.lower)
    return {"items": items, "total": len(items), "facets": {"unitAreas": units}}


class DocIn(BaseModel):
    plantId: str
    docType: str
    docNo: str = Field(min_length=1, max_length=120)
    revision: str = Field(min_length=1, max_length=20)
    title: str = Field(min_length=2, max_length=300)
    unitArea: str | None = Field(None, max_length=120)
    description: str | None = Field(None, max_length=4000)
    hazardousMaterialId: str | None = None
    issuedDate: datetime | None = None
    reviewDue: datetime | None = None


@router.post("/documents", status_code=201)
async def create_document(body: DocIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Register a document revision. If (plant, docNo) already has a CURRENT
    revision, this one supersedes it and keeps the link back to it."""
    await _require(db, user, MANAGE, body.plantId)
    _check_choice(body.docType, svc.DOC_TYPES, "docType")
    doc_no = body.docNo.strip()
    if body.hazardousMaterialId:
        h = await db.get(HazardousMaterial, body.hazardousMaterialId)
        if h is None or h.isDeleted:
            raise HTTPException(422, "Chemical not found")
        if h.siteId != body.plantId:
            raise HTTPException(422, "The chemical belongs to another plant")
    elif body.docType == "SDS":
        raise HTTPException(422, "An SDS must be linked to a chemical from the register")
    prior = (await db.execute(
        select(PsiDocument).where(PsiDocument.isDeleted.is_(False)).where(PsiDocument.plantId == body.plantId)
        .where(PsiDocument.docNo == doc_no).order_by(PsiDocument.createdAt.desc())
    )).scalars().all()
    if prior and prior[0].docType != body.docType:
        raise HTTPException(409, f"{doc_no} is registered as {svc.DOC_TYPE_LABEL.get(prior[0].docType)}; a revision must keep the type")
    blockers = svc.revision_blockers([p.revision for p in prior], body.revision)
    if blockers:
        raise HTTPException(409, " ".join(blockers))
    review_due = body.reviewDue
    if review_due is None and body.docType == "SDS" and body.issuedDate:
        review_due = body.issuedDate + timedelta(days=365 * svc.SDS_REVIEW_YEARS)
    current = next((p for p in prior if p.status == "CURRENT"), None)
    # Supersede first and flush, so the partial unique index (one CURRENT per
    # plant + docNo) never sees two CURRENT rows.
    for p in prior:
        if p.status == "CURRENT":
            p.status = "SUPERSEDED"
    await db.flush()
    d = PsiDocument(
        plantId=body.plantId, unitArea=(body.unitArea or (current.unitArea if current else None) or None),
        docType=body.docType, docNo=doc_no, revision=body.revision.strip(), title=body.title.strip(),
        description=body.description, hazardousMaterialId=body.hazardousMaterialId or (current.hazardousMaterialId if current else None),
        status="CURRENT", supersedesId=current.id if current else None, issuedDate=body.issuedDate, reviewDue=review_due,
        createdBy=user.id,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return await _doc_detail(db, d)


async def _doc_detail(db: AsyncSession, d: PsiDocument) -> dict[str, Any]:
    history = _current_first((await db.execute(
        select(PsiDocument).where(PsiDocument.isDeleted.is_(False)).where(PsiDocument.plantId == d.plantId)
        .where(PsiDocument.docNo == d.docNo).order_by(PsiDocument.createdAt.desc())
        .execution_options(populate_existing=True)
    )).scalars().all())
    users = {k: v.name for k, v in (await resolve_user_directory(
        db, [h.uploadedById for h in history] + [h.createdBy for h in history])).items()}
    pl = await resolve_plant_names(db, [d.plantId])
    chem_names = {}
    if d.hazardousMaterialId:
        h = await db.get(HazardousMaterial, d.hazardousMaterialId)
        if h is not None:
            chem_names = {h.id: h.chemicalName}
    return {**_doc_row(d, users, pl, chem_names), "history": [_doc_row(h, users, pl, chem_names) for h in history]}


@router.get("/documents/{did}")
async def get_document(did: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _doc(db, did)
    await _require(db, user, READ, d.plantId)
    return await _doc_detail(db, d)


class DocPatch(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=300)
    description: str | None = Field(None, max_length=4000)
    unitArea: str | None = Field(None, max_length=120)
    issuedDate: datetime | None = None
    reviewDue: datetime | None = None


@router.patch("/documents/{did}")
async def update_document(did: str, body: DocPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    d = await _doc(db, did)
    await _require(db, user, MANAGE, d.plantId)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(d, k, v)
    await db.commit()
    await db.refresh(d)
    return await _doc_detail(db, d)


class DocDeleteIn(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


@router.delete("/documents/{did}")
async def delete_document(did: str, body: DocDeleteIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Withdraw a revision registered in error. If it was CURRENT, the revision
    it superseded becomes CURRENT again."""
    d = await _doc(db, did)
    await _require(db, user, MANAGE, d.plantId)
    was_current = d.status == "CURRENT"
    d.isDeleted = True
    d.deletedAt = svc.utcnow()
    d.deletedBy = user.id
    d.deletionReason = body.reason
    if was_current and d.supersedesId:
        prev = await db.get(PsiDocument, d.supersedesId)
        if prev is not None and not prev.isDeleted:
            prev.status = "CURRENT"
    await db.commit()
    return {"ok": True}


@router.post("/documents/{did}/file")
async def upload_file(did: str, payload: dict[str, Any], user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Two-phase upload, same contract as /api/evidence: phase=init returns a
    signed PUT URL; the browser PUTs the file; phase=complete records it. A
    revision's file is immutable once completed: upload a new revision instead."""
    d = await _doc(db, did)
    await _require(db, user, MANAGE, d.plantId)
    if not storage.is_storage_configured():
        raise HTTPException(503, "File storage isn't configured on this server.")
    phase = payload.get("phase")
    if phase == "init":
        if d.fileUploadedAt:
            raise HTTPException(409, "This revision already has its file. Register a new revision to change it.")
        file_name = str(payload.get("fileName") or "").strip()
        size = int(payload.get("fileSize") or 0)
        mime = str(payload.get("mimeType") or "application/octet-stream")
        if not file_name or size <= 0:
            raise HTTPException(400, "fileName and fileSize are required")
        if size > MAX_FILE_SIZE:
            raise HTTPException(400, f"File exceeds the {MAX_FILE_SIZE // 1024 // 1024} MB limit.")
        if mime not in ALLOWED_MIME:
            raise HTTPException(400, f"MIME type {mime} is not allowed.")
        path = storage.build_evidence_storage_path(entity_type="psi_document", entity_id=d.id, category=d.docType, file_name=file_name)
        try:
            signed = storage.create_signed_upload_url(path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"Storage init failed: {e}") from e
        d.storagePath, d.fileName, d.fileSize, d.mimeType = path, file_name, size, mime
        await db.commit()
        return {"phase": "init", "uploadUrl": signed["uploadUrl"], "token": signed.get("token", ""), "storagePath": path}
    if phase == "complete":
        if not d.storagePath:
            raise HTTPException(409, "No upload was started for this revision")
        if d.fileUploadedAt:
            raise HTTPException(409, "This revision's file is already recorded")
        d.fileUploadedAt = svc.utcnow()
        d.uploadedById = user.id
        await db.commit()
        await db.refresh(d)
        return await _doc_detail(db, d)
    raise HTTPException(400, "Unknown phase")


@router.get("/documents/{did}/download")
async def download_file(did: str, inline: int = 0, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    d = await db.get(PsiDocument, did)
    if d is None or d.isDeleted:
        raise HTTPException(404, "PSI document not found")
    await _require(db, user, READ, d.plantId)
    if not _has_file(d):
        raise HTTPException(404, "No file has been uploaded for this revision")
    try:
        url = storage.create_signed_download_url(d.storagePath, expires_in_sec=300, download=None if inline else d.fileName)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"Storage unavailable: {e}") from e
    return {"url": url}


# ── completeness ─────────────────────────────────────────────────────────────
@router.get("/completeness")
async def psi_completeness(plantId: str | None = Query(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    rows = (await db.execute(
        select(PsiDocument).where(PsiDocument.isDeleted.is_(False)).where(PsiDocument.status == "CURRENT")
        .where(_plant_where(PsiDocument.plantId, plants))
    )).scalars().all()
    by_plant: dict[str, list[dict[str, Any]]] = {}
    for d in rows:
        by_plant.setdefault(d.plantId, []).append(_doc_dict(d))
    names = await resolve_plant_names(db, by_plant.keys())
    out = []
    for pid, docs in sorted(by_plant.items(), key=lambda kv: names.get(kv[0], kv[0])):
        units = svc.completeness(docs)
        out.append({
            "plantId": pid, "plantName": names.get(pid), "units": units,
            "overallPercent": round(sum(u["percent"] for u in units) / len(units)) if units else 0,
        })
    return {"plants": out, "requiredDocTypes": [{"docType": t, "label": svc.DOC_TYPE_LABEL[t]} for t in svc.REQUIRED_UNIT_DOC_TYPES]}


# ── Chem advisor ─────────────────────────────────────────────────────────────
class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1500)
    plantId: str | None = None
    chemicalIds: list[str] = Field(default_factory=list, max_length=svc.MAX_CHEMICALS)


@router.get("/advisor/status")
async def advisor_status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    return {"aiAvailable": ai.is_configured(), "disclaimer": svc.ADVISOR_DISCLAIMER}


@router.post("/advisor/ask")
async def advisor_ask(body: AskIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, body.plantId)
    register = await _load_chemicals(db, plants)
    by_id = {r["id"]: r for r in register}
    picked = [cid for cid in dict.fromkeys(body.chemicalIds) if cid in by_id]
    unmatched = [cid for cid in body.chemicalIds if cid not in by_id]
    if not picked:
        picked = svc.match_chemicals(body.question, register)
    picked = picked[: svc.MAX_CHEMICALS]
    rows = [by_id[c] for c in picked]
    facts = [svc.chem_fact(r, r["hazardGroups"], ({**r["sdsDocument"], "sdsStatus": r["sdsStatus"],
                                                    "reviewDueIso": r["sdsDocument"]["reviewDue"]} if r["sdsDocument"] else None))
             for r in rows]
    matrix = compat.matrix([_chem_obj(r) for r in rows]) if len(rows) >= 2 else None
    plant_name = rows[0]["plantName"] if rows else None
    configured = ai.is_configured()
    reply = None
    if configured and facts:
        started = time.monotonic()
        reply = await ai.complete_json(
            system=svc.ADVISOR_SYSTEM, user=svc.build_advisor_prompt(body.question, facts, matrix, plant_name), max_tokens=1200,
        )
        # Model / latency stay in the server log, never in the response.
        log.info("chem advisor: user=%s model=%s chemicals=%d ai_ok=%s latency_ms=%d", user.id,
                 get_settings().anthropic_model, len(facts), reply is not None, int((time.monotonic() - started) * 1000))
    return svc.shape_advisor_response(
        question=body.question, facts=facts, compat=matrix, ai=reply, ai_configured=configured, unmatched=unmatched,
    )
