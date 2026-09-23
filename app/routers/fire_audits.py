"""CAMS Fire Safety audit engagements — scheduling and asset scope.

A Fire Safety audit is an ordinary CamsEngagement (same workspace, findings,
lifecycle) typed by the FIRE_SAFETY_AUDIT audit type and tagged
sourceModule='FIRE'. What this router adds:

  * create — runs the independence engine over the lead auditor and team before
    anything is written (BLOCK → 409 with the verdicts; WARN is returned) and
    records the verdicts to the independence register, exactly as ComplianceAudit
    creation does. The CAMS create route has no such guard.
  * asset scope — "Include in audit" from a fire asset's detail page adds the
    asset to an open audit at the same site (CamsEngagementAsset).

Standards come from the audit type, so every Fire Safety audit cites the same
fire codes rather than whatever a scheduler typed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.cams import CamsAuditType, CamsEngagement
from app.models.fire_audit import FIRE_AUDIT_TYPE_CODE, CamsEngagementAsset
from app.models.fire_safety import FireEquipment
from app.models.user import User
from app.services import fire_permissions as perm
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/fire/audits", tags=["fire-audits"])

_OPEN = ("PLANNED", "SCHEDULED", "IN_PROGRESS", "FIELDWORK_COMPLETE", "FINDINGS_REVIEW")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require_cams(db: AsyncSession, user: User, code: str, plant_id: str | None) -> None:
    res = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or f"Missing permission {code}")


async def _audit_type(db: AsyncSession) -> CamsAuditType:
    t = (
        await db.execute(select(CamsAuditType).where(CamsAuditType.typeCode == FIRE_AUDIT_TYPE_CODE))
    ).scalars().first()
    if t is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The Fire Safety audit type is not configured (run scripts/seed_fire_audit_type.py).",
        )
    return t


async def _fire_audit(db: AsyncSession, engagement_id: str) -> CamsEngagement:
    eng = await db.get(CamsEngagement, engagement_id)
    if eng is None or eng.isDeleted or eng.sourceModule != "FIRE":
        raise HTTPException(404, "Fire Safety audit not found.")
    t = await _audit_type(db)
    if eng.auditTypeId != t.id:
        raise HTTPException(404, "Not a Fire Safety audit engagement.")
    return eng


def _out(e: CamsEngagement, asset_count: int = 0) -> dict[str, Any]:
    return {
        "id": e.id,
        "engagementCode": e.engagementCode,
        "title": e.title,
        "status": e.status,
        "siteId": e.siteId,
        "plannedDate": e.plannedDate.isoformat() if e.plannedDate else None,
        "leadAuditorId": e.leadAuditorId,
        "auditTeamIds": e.auditTeamIds or [],
        "standardRefs": e.standardRefs or [],
        "sourceModule": e.sourceModule,
        "assetCount": asset_count,
    }


async def _asset_counts(db: AsyncSession, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(CamsEngagementAsset.engagementId, func.count())
            .where(CamsEngagementAsset.engagementId.in_(ids))
            .group_by(CamsEngagementAsset.engagementId)
        )
    ).all()
    return {k: n for k, n in rows}


@router.get("")
async def list_fire_audits(
    siteId: str | None = Query(default=None),
    openOnly: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require_cams(db, user, "CAMS.READ", siteId)
    t = await _audit_type(db)
    stmt = select(CamsEngagement).where(
        CamsEngagement.auditTypeId == t.id,
        CamsEngagement.sourceModule == "FIRE",
        CamsEngagement.isDeleted.is_(False),
    )
    if siteId:
        stmt = stmt.where(CamsEngagement.siteId == siteId)
    if openOnly:
        stmt = stmt.where(CamsEngagement.status.in_(_OPEN))
    rows = (await db.execute(stmt.order_by(CamsEngagement.plannedDate.desc()))).scalars().all()
    counts = await _asset_counts(db, [r.id for r in rows])
    return {"items": [_out(r, counts.get(r.id, 0)) for r in rows], "auditType": {"id": t.id, "name": t.name}}


class FireAuditCreate(BaseModel):
    siteId: str
    title: str = Field(min_length=3)
    plannedDate: datetime
    leadAuditorId: str
    auditTeamIds: list[str] = Field(default_factory=list)
    auditeeOwnerId: str | None = None
    scopeStatement: str | None = None
    assetIds: list[str] = Field(default_factory=list)


@router.post("", status_code=201)
async def create_fire_audit(
    body: FireAuditCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.services import independence as inde
    from app.services.independence_events import record_verdicts

    await _require_cams(db, user, "CAMS.SCHEDULE", body.siteId)
    t = await _audit_type(db)
    eng = CamsEngagement(
        engagementCode=await _next_code(db),
        title=body.title,
        engagementType="COMPLIANCE_AUDIT",
        auditTypeId=t.id,
        standardRefs=list(t.standardRefs or []),
        siteId=body.siteId,
        scopeStatement=body.scopeStatement,
        leadAuditorId=body.leadAuditorId,
        auditTeamIds=body.auditTeamIds,
        auditeeOwnerId=body.auditeeOwnerId,
        plannedDate=body.plannedDate,
        templateId=t.defaultTemplateId,
        sourceModule="FIRE",
        status="PLANNED",
        createdBy=user.id,
    )
    # Independence BEFORE the write. The scope builder only reads fields, so an
    # unsaved engagement is a valid input.
    scope = inde.scope_for_engagement(eng)
    scope.kind = "AUDIT"
    auditors = [body.leadAuditorId, *[a for a in body.auditTeamIds if a != body.leadAuditorId]]
    verdicts = await inde.check_many(db, user_ids=auditors, scope=scope, assigning_as="AUDITOR")
    await record_verdicts(
        verdicts=verdicts, engagement_kind="AUDIT", origin="CREATE_AUDIT",
        attempted_by_user_id=user.id, engagement_code=eng.engagementCode, site_id=body.siteId,
    )
    blocked = {uid: v.as_dict() for uid, v in verdicts.items() if not v.allowed}
    if blocked:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": "Independence check blocked this assignment.", "verdicts": blocked},
        )
    db.add(eng)
    await db.flush()
    for aid in dict.fromkeys(body.assetIds):
        await _add_asset(db, eng, aid, user.id)
    await db.commit()
    await db.refresh(eng)
    return {
        **_out(eng, len(set(body.assetIds))),
        "independence": {uid: v.as_dict() for uid, v in verdicts.items()},
    }


async def _next_code(db: AsyncSession) -> str:
    year = _now().year
    n = (
        await db.execute(
            select(func.count()).select_from(CamsEngagement).where(CamsEngagement.engagementCode.like(f"FSA-{year}-%"))
        )
    ).scalar() or 0
    return f"FSA-{year}-{n + 1:04d}"


async def _add_asset(db: AsyncSession, eng: CamsEngagement, asset_id: str, user_id: str) -> bool:
    a = await db.get(FireEquipment, asset_id)
    if a is None or a.isDeleted:
        raise HTTPException(404, f"Fire asset {asset_id} not found.")
    if a.plantId != eng.siteId:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Asset is at a different site from the audit.")
    exists = (
        await db.execute(
            select(CamsEngagementAsset.id).where(
                CamsEngagementAsset.engagementId == eng.id,
                CamsEngagementAsset.sourceModule == "FIRE",
                CamsEngagementAsset.entityId == asset_id,
            )
        )
    ).scalar_one_or_none()
    if exists:
        return False
    db.add(CamsEngagementAsset(engagementId=eng.id, sourceModule="FIRE", entityId=asset_id, addedBy=user_id))
    return True


class IncludeAsset(BaseModel):
    assetId: str


@router.post("/{engagement_id}/assets", status_code=201)
async def include_asset(
    engagement_id: str,
    body: IncludeAsset,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    eng = await _fire_audit(db, engagement_id)
    await _require_cams(db, user, "CAMS.SCHEDULE", eng.siteId)
    if eng.status not in _OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Audit is {eng.status}; its scope is frozen.")
    added = await _add_asset(db, eng, body.assetId, user.id)
    await db.commit()
    return {"ok": True, "added": added, "engagementId": eng.id, "engagementCode": eng.engagementCode}


@router.delete("/{engagement_id}/assets/{asset_id}")
async def exclude_asset(
    engagement_id: str,
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    eng = await _fire_audit(db, engagement_id)
    await _require_cams(db, user, "CAMS.SCHEDULE", eng.siteId)
    if eng.status not in _OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Audit is {eng.status}; its scope is frozen.")
    row = (
        await db.execute(
            select(CamsEngagementAsset).where(
                CamsEngagementAsset.engagementId == eng.id, CamsEngagementAsset.entityId == asset_id
            )
        )
    ).scalars().first()
    if row:
        await db.delete(row)
        await db.commit()
    return {"ok": True, "removed": bool(row)}


@router.get("/{engagement_id}/assets")
async def audit_assets(
    engagement_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    eng = await _fire_audit(db, engagement_id)
    await perm.require(db, user, perm.READ, plant_id=eng.siteId)
    rows = (
        await db.execute(
            select(FireEquipment)
            .join(CamsEngagementAsset, CamsEngagementAsset.entityId == FireEquipment.id)
            .where(CamsEngagementAsset.engagementId == eng.id)
            .order_by(FireEquipment.type, FireEquipment.equipmentCode)
        )
    ).scalars().all()
    return {
        "items": [
            {"id": a.id, "equipmentCode": a.equipmentCode, "type": a.type, "location": a.location, "status": a.status}
            for a in rows
        ]
    }


@router.get("/for-asset/{asset_id}")
async def audits_for_asset(
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fire Safety audits covering this asset, plus the open audits at its site
    it could be included in — what the asset page's "Include in audit" needs."""
    a = await db.get(FireEquipment, asset_id)
    if a is None or a.isDeleted:
        raise HTTPException(404, "Fire asset not found.")
    await perm.require(db, user, perm.READ, plant_id=a.plantId)
    t = await _audit_type(db)
    site_audits = (
        await db.execute(
            select(CamsEngagement).where(
                CamsEngagement.auditTypeId == t.id,
                CamsEngagement.siteId == a.plantId,
                CamsEngagement.isDeleted.is_(False),
            ).order_by(CamsEngagement.plannedDate.desc())
        )
    ).scalars().all()
    linked = set(
        (
            await db.execute(
                select(CamsEngagementAsset.engagementId).where(
                    CamsEngagementAsset.sourceModule == "FIRE", CamsEngagementAsset.entityId == asset_id
                )
            )
        ).scalars().all()
    )
    counts = await _asset_counts(db, [e.id for e in site_audits])
    return {
        "included": [_out(e, counts.get(e.id, 0)) for e in site_audits if e.id in linked],
        "available": [
            _out(e, counts.get(e.id, 0)) for e in site_audits if e.id not in linked and e.status in _OPEN
        ],
    }
