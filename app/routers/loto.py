"""LOTO (Lockout/Tagout) router.

Mounted at /api/loto. Endpoints:

  Procedure library
    GET    /api/loto/meta                          — vocabularies for the forms
    GET    /api/loto/procedures                    — list (site/area/status/overdue)
    POST   /api/loto/procedures                    — create (draft)
    GET    /api/loto/procedures/{id}               — detail + versions + publish gate
    PUT    /api/loto/procedures/{id}               — versioned edit
    POST   /api/loto/procedures/{id}/publish       — draft/under_review → active
    POST   /api/loto/procedures/{id}/retire        — withdraw from use
    POST   /api/loto/procedures/{id}/review        — complete a scheduled evaluation
    DELETE /api/loto/procedures/{id}               — soft-delete
    GET    /api/loto/procedures/{id}/versions/{n}  — an historical version body

  Public field access (NO AUTH — see the note on the endpoint)
    GET    /api/loto/qr/{token}

  Execution
    POST   /api/loto/executions                    — start (freezes the snapshot)
    GET    /api/loto/executions                    — list; ?ptwId= for the PTW panel
    GET    /api/loto/executions/{id}               — detail + gate
    PATCH  /api/loto/executions/{id}/lock          — per-participant, caller's own row
    PATCH  /api/loto/executions/{id}/verify        — zero-energy steps
    PATCH  /api/loto/executions/{id}/start-work    — verified → work_in_progress
    PATCH  /api/loto/executions/{id}/unlock        — per-participant, caller's own row
    POST   /api/loto/executions/{id}/close         — final sign-off (gated)
    POST   /api/loto/executions/{id}/abort         — recorded exception path

  PTW cross-reference (spec §6 — the LOTO side of the link)
    GET    /api/loto/permits/{permitId}/status     — panel state + closure verdict
    POST   /api/loto/permits/{permitId}/link       — link/unlink an execution

Permission model mirrors HIRA's split: CREATE/UPDATE author, APPROVE publishes,
EXECUTE runs a lockout, REVIEW completes an evaluation cycle. Plant scoping is
fail-closed via access_scope.build_query_scope on every list, and re-checked
per record on every read/write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.core.soft_delete import soft_delete
from app.models.loto import (
    ENERGY_TYPES,
    EXECUTION_STATUSES,
    HARDWARE_ITEM_TYPES,
    ISOLATION_METHODS,
    OPEN_EXECUTION_STATUSES,
    PARTICIPANT_ROLES,
    PROCEDURE_STATUSES,
    LOCK_HOLDER_ROLES,
    LotoExecution,
    LotoExecutionParticipant,
    LotoProcedure,
    LotoProcedureVersion,
    LotoReviewLog,
)
from app.models.user import User
from app.schemas.loto import (
    ExecutionAbortRequest,
    ExecutionCloseRequest,
    ExecutionCreate,
    ExecutionListItem,
    ExecutionListResponse,
    ExecutionOut,
    ExecutionStartWorkRequest,
    LockConfirmRequest,
    LotoMeta,
    PermitLinkRequest,
    PermitLotoStatus,
    ProcedureCreate,
    ProcedureDeleteRequest,
    ProcedureListItem,
    ProcedureListResponse,
    ProcedureOut,
    ProcedurePublishRequest,
    ProcedureReviewRequest,
    ProcedureUpdate,
    QrProcedureView,
    UnlockConfirmRequest,
    VerifyRequest,
)
from app.services import loto as svc
from app.services.access_scope import build_query_scope
from app.services.plant_directory import resolve_plant_names, site_label
from app.services.user_directory import resolve_user_directory

log = logging.getLogger("safeops360.loto")

router = APIRouter(prefix="/api/loto", tags=["loto"])

READ = "LOTO.READ"
CREATE = "LOTO.CREATE"
UPDATE = "LOTO.UPDATE"
APPROVE = "LOTO.APPROVE"
EXECUTE = "LOTO.EXECUTE"
REVIEW = "LOTO.REVIEW"
DELETE = "LOTO.DELETE"


def _err(e: svc.LotoError) -> HTTPException:
    return HTTPException(e.status_code, e.message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
#  Meta
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/meta", response_model=LotoMeta)
async def get_meta(user: User = Depends(get_current_user)) -> LotoMeta:
    """Vocabularies for the builder dropdowns.

    Served from the same tuples the models and Pydantic layer use, so a form can
    never offer a value the API would 422. Auth-gated but not permission-gated —
    it is a static word list, and gating it would break the form for a user who
    can read a procedure but not author one.
    """
    return LotoMeta(
        energyTypes=list(ENERGY_TYPES),
        isolationMethods=list(ISOLATION_METHODS),
        hardwareItemTypes=list(HARDWARE_ITEM_TYPES),
        procedureStatuses=list(PROCEDURE_STATUSES),
        executionStatuses=list(EXECUTION_STATUSES),
        participantRoles=list(PARTICIPANT_ROLES),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Serialisers
# ═══════════════════════════════════════════════════════════════════════════


async def _procedure_list_item(
    db: AsyncSession,
    proc: LotoProcedure,
    *,
    site_names: dict[str, str],
    user_refs: dict[str, Any],
    pending_review_id: str | None,
    counts: dict[str, dict[str, int]] | None = None,
    open_exec_count: int = 0,
) -> ProcedureListItem:
    c = (counts or {}).get(proc.id, {})
    creator = user_refs.get(proc.createdById or "")
    return ProcedureListItem(
        id=proc.id,
        procedureCode=proc.procedureCode,
        title=proc.title,
        status=proc.status,
        version=proc.version,
        publishedVersion=await _published_version_number(db, proc),
        siteId=proc.siteId,
        # Never render a raw Plant cuid — site_label resolves or says "Unknown
        # site", which a reader can act on.
        siteName=proc.siteName or site_label(site_names, proc.siteId),
        area=proc.area,
        equipmentId=proc.equipmentId,
        equipmentName=proc.equipmentName,
        equipmentTag=proc.equipmentTag,
        qrCodeToken=proc.qrCodeToken,
        energySourceCount=c.get("energySources", 0),
        isolationPointCount=c.get("isolationPoints", 0),
        verificationStepCount=c.get("verificationSteps", 0),
        openExecutionCount=open_exec_count,
        review=svc.review_status_fields(proc, pending_review_id=pending_review_id),
        createdById=proc.createdById,
        createdByName=creator.name if creator else None,
        createdAt=proc.createdAt,
        updatedAt=proc.updatedAt,
    )


async def _published_version_number(db: AsyncSession, proc: LotoProcedure) -> int | None:
    if not proc.publishedVersionId:
        return None
    v = await db.get(LotoProcedureVersion, proc.publishedVersionId)
    return v.version if v else None


async def _procedure_out(db: AsyncSession, proc: LotoProcedure) -> ProcedureOut:
    site_names = await resolve_plant_names(db, [proc.siteId])
    version_actor_ids = [v.createdById for v in proc.versions] + [
        v.publishedById for v in proc.versions
    ]
    user_refs = await resolve_user_directory(
        db, [proc.createdById, proc.lastReviewedById, *version_actor_ids]
    )
    pending = await svc.review_state(db, [proc.id])
    open_execs = (
        await db.execute(
            select(func.count(LotoExecution.id))
            .where(LotoExecution.procedureId == proc.id)
            .where(LotoExecution.isDeleted.is_(False))
            .where(LotoExecution.status.in_(OPEN_EXECUTION_STATUSES))
        )
    ).scalar() or 0

    base = await _procedure_list_item(
        db,
        proc,
        site_names=site_names,
        user_refs=user_refs,
        pending_review_id=pending.get(proc.id),
        counts={
            proc.id: {
                "energySources": len(proc.energySources),
                "isolationPoints": len(proc.isolationPoints),
                "verificationSteps": len(proc.verificationSteps),
            }
        },
        open_exec_count=open_execs,
    )

    blockers = svc.publish_blockers(proc)
    published_no = base.publishedVersion
    return ProcedureOut(
        **base.model_dump(),
        description=proc.description,
        reviewFrequencyMonths=proc.reviewFrequencyMonths,
        energySources=[
            {
                "id": e.id, "sequence": e.sequence, "energyType": e.energyType,
                "magnitude": e.magnitude, "locationDescription": e.locationDescription,
            }
            for e in sorted(proc.energySources, key=lambda x: x.sequence)
        ],
        isolationPoints=[
            {
                "id": p.id, "sequence": p.sequence, "energySourceId": p.energySourceId,
                "location": p.location, "isolationMethod": p.isolationMethod,
                "lockType": p.lockType, "verificationMethod": p.verificationMethod,
                "notes": p.notes,
            }
            for p in sorted(proc.isolationPoints, key=lambda x: x.sequence)
        ],
        hardware=[
            {
                "id": h.id, "itemType": h.itemType, "description": h.description,
                "quantityRequired": h.quantityRequired,
            }
            for h in proc.hardware
        ],
        verificationSteps=[
            {
                "id": s.id, "sequence": s.sequence, "stepText": s.stepText,
                "requiresPhoto": s.requiresPhoto, "requiresSignoff": s.requiresSignoff,
            }
            for s in sorted(proc.verificationSteps, key=lambda x: x.sequence)
        ],
        versions=[
            {
                "id": v.id, "version": v.version, "isPublished": v.isPublished,
                "publishedAt": v.publishedAt, "publishedById": v.publishedById,
                "publishedByName": (
                    user_refs[v.publishedById].name if v.publishedById in user_refs else None
                ),
                "supersededAt": v.supersededAt, "changeType": v.changeType,
                "changeSummary": v.changeSummary, "createdById": v.createdById,
                "createdByName": (
                    user_refs[v.createdById].name if v.createdById in user_refs else None
                ),
                "createdAt": v.createdAt,
            }
            for v in sorted(proc.versions, key=lambda x: x.version, reverse=True)
        ],
        canPublish=not blockers and proc.status in {"draft", "under_review"},
        publishBlockers=blockers,
        # The live body has moved past what the field can see. True during the
        # whole re-approval window — which is exactly when a supervisor needs
        # to know the QR label is showing an older sequence.
        hasUnpublishedChanges=published_no is not None and published_no != proc.version,
    )


def _execution_list_item(
    ex: LotoExecution, *, site_names: dict[str, str], user_refs: dict[str, Any]
) -> ExecutionListItem:
    header = (ex.procedureVersionSnapshot or {}).get("header") or {}
    holders = svc.lock_holders(ex)
    initiator = user_refs.get(ex.initiatedById)
    return ExecutionListItem(
        id=ex.id,
        number=ex.number,
        status=ex.status,
        procedureId=ex.procedureId,
        procedureCode=header.get("procedureCode"),
        procedureTitle=header.get("title"),
        snapshotVersion=ex.snapshotVersion,
        equipmentName=header.get("equipmentName"),
        equipmentTag=header.get("equipmentTag"),
        ptwId=ex.ptwId,
        ptwNumber=ex.ptwNumber,
        siteId=ex.siteId,
        siteName=ex.siteName or site_label(site_names, ex.siteId),
        initiatedById=ex.initiatedById,
        initiatedByName=ex.initiatedByName or (initiator.name if initiator else None),
        initiatedAt=ex.initiatedAt,
        isGroupLockout=ex.isGroupLockout,
        participantCount=len(ex.participants),
        lockHolderCount=len(holders),
        locksConfirmedCount=sum(1 for p in holders if p.lockAppliedConfirmed),
        locksRemovedCount=sum(1 for p in holders if p.lockRemovedConfirmed),
        closedAt=ex.closedAt,
        createdAt=ex.createdAt,
    )


async def _execution_out(db: AsyncSession, ex: LotoExecution) -> ExecutionOut:
    site_names = await resolve_plant_names(db, [ex.siteId])
    user_refs = await resolve_user_directory(
        db,
        [ex.initiatedById, ex.closedById, *[p.userId for p in ex.participants]],
    )
    base = _execution_list_item(ex, site_names=site_names, user_refs=user_refs)
    closer = user_refs.get(ex.closedById or "")

    return ExecutionOut(
        **base.model_dump(),
        procedureVersionSnapshot=ex.procedureVersionSnapshot or {},
        participants=[
            {
                "id": p.id,
                "userId": p.userId,
                # Resolved live so a roster shows the current name even if the
                # denormalised copy was written before a rename.
                "userName": (
                    user_refs[p.userId].name if p.userId in user_refs else p.userName
                ),
                "userRole": (
                    user_refs[p.userId].role if p.userId in user_refs else p.userRole
                ),
                "participantRole": p.participantRole,
                "assignedIsolationPointIds": list(p.assignedIsolationPointIds or []),
                "lockTagNumber": p.lockTagNumber,
                "lockAppliedAt": p.lockAppliedAt,
                "lockAppliedConfirmed": p.lockAppliedConfirmed,
                "lockRemovedAt": p.lockRemovedAt,
                "lockRemovedConfirmed": p.lockRemovedConfirmed,
                "notes": p.notes,
                "isLockHolder": p.participantRole in LOCK_HOLDER_ROLES,
            }
            for p in ex.participants
        ],
        verificationRecords=[
            {
                "id": r.id, "stepId": r.stepId, "sequence": r.sequence,
                "stepText": r.stepText, "completedById": r.completedById,
                "completedByName": (
                    user_refs[r.completedById].name
                    if r.completedById in user_refs
                    else r.completedByName
                ),
                "completedAt": r.completedAt, "photoUrl": r.photoUrl,
                "signoff": r.signoff, "notes": r.notes,
            }
            for r in sorted(ex.verificationRecords, key=lambda x: x.sequence)
        ],
        workStartedAt=ex.workStartedAt,
        locksRemovedAt=ex.locksRemovedAt,
        closedById=ex.closedById,
        closedByName=closer.name if closer else None,
        closureNotes=ex.closureNotes,
        abortedById=ex.abortedById,
        abortedAt=ex.abortedAt,
        abortReason=ex.abortReason,
        gate=svc.execution_gate(ex),
        procedureHasChangedSinceStart=await svc.procedure_changed_since_start(db, ex),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Procedure library
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/procedures", response_model=ProcedureListResponse)
async def list_procedures(
    siteId: str | None = Query(default=None),
    area: str | None = Query(default=None),
    status_: str | None = Query(default=None, alias="status"),
    equipmentId: str | None = Query(default=None),
    overdue: bool | None = Query(
        default=None,
        description="true → only procedures whose scheduled review has lapsed",
    ),
    q: str | None = Query(default=None, description="code / title / equipment search"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProcedureListResponse:
    # Fail-closed plant scoping: no accessible plants → no rows, never "all rows".
    scope = await build_query_scope(db, user.id, READ)

    stmt = (
        select(LotoProcedure)
        .options(
            selectinload(LotoProcedure.energySources),
            selectinload(LotoProcedure.isolationPoints),
            selectinload(LotoProcedure.verificationSteps),
        )
        .where(LotoProcedure.isDeleted.is_(False))
    )
    stmt = scope.apply(stmt, LotoProcedure, plant_attr="siteId")

    if siteId:
        if not scope.allows_plant(siteId):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied for this site")
        stmt = stmt.where(LotoProcedure.siteId == siteId)
    if area:
        stmt = stmt.where(LotoProcedure.area == area)
    if status_:
        stmt = stmt.where(LotoProcedure.status == status_)
    if equipmentId:
        stmt = stmt.where(LotoProcedure.equipmentId == equipmentId)
    if overdue:
        # Retired procedures are out of service, so a lapsed review on one is not
        # a finding — filtering them out here keeps the flag meaningful.
        stmt = (
            stmt.where(LotoProcedure.nextReviewDueAt.isnot(None))
            .where(LotoProcedure.nextReviewDueAt < _now())
            .where(LotoProcedure.status != "retired")
        )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                LotoProcedure.procedureCode.ilike(like),
                LotoProcedure.title.ilike(like),
                LotoProcedure.equipmentName.ilike(like),
                LotoProcedure.equipmentTag.ilike(like),
            )
        )

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0

    # Platform-wide register convention: newest first.
    rows = (
        await db.execute(
            stmt.order_by(LotoProcedure.createdAt.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    site_names = await resolve_plant_names(db, [r.siteId for r in rows])
    user_refs = await resolve_user_directory(db, [r.createdById for r in rows])
    pending = await svc.review_state(db, [r.id for r in rows])

    # Open-execution counts in ONE grouped query rather than per row.
    open_counts: dict[str, int] = {}
    if rows:
        exec_rows = (
            await db.execute(
                select(LotoExecution.procedureId, func.count(LotoExecution.id))
                .where(LotoExecution.procedureId.in_([r.id for r in rows]))
                .where(LotoExecution.isDeleted.is_(False))
                .where(LotoExecution.status.in_(OPEN_EXECUTION_STATUSES))
                .group_by(LotoExecution.procedureId)
            )
        ).all()
        open_counts = {r[0]: r[1] for r in exec_rows}

    counts = {
        r.id: {
            "energySources": len(r.energySources),
            "isolationPoints": len(r.isolationPoints),
            "verificationSteps": len(r.verificationSteps),
        }
        for r in rows
    }

    items = [
        await _procedure_list_item(
            db, r,
            site_names=site_names,
            user_refs=user_refs,
            pending_review_id=pending.get(r.id),
            counts=counts,
            open_exec_count=open_counts.get(r.id, 0),
        )
        for r in rows
    ]
    return ProcedureListResponse(items=items, total=total)


@router.post("/procedures", response_model=ProcedureOut, status_code=status.HTTP_201_CREATED)
async def create_procedure(
    payload: ProcedureCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProcedureOut:
    await require_permission_with_context(CREATE, user, db, plant_id=payload.siteId)

    site_names = await resolve_plant_names(db, [payload.siteId])
    proc = LotoProcedure(
        procedureCode=payload.procedureCode
        or await svc.next_procedure_code(db, payload.siteId),
        siteId=payload.siteId,
        siteName=site_names.get(payload.siteId),
        areaId=payload.areaId,
        area=payload.area,
        equipmentId=payload.equipmentId,
        equipmentName=payload.equipmentName,
        equipmentTag=payload.equipmentTag,
        title=payload.title,
        description=payload.description,
        reviewFrequencyMonths=payload.reviewFrequencyMonths,
        status="draft",
        version=1,
        createdById=user.id,
        updatedById=user.id,
    )
    db.add(proc)
    await db.flush()

    # Publish the (known-empty) collections so replace_body can diff against
    # them. `set_committed_value`, NOT plain assignment: `proc` is persistent
    # after the flush above and every one of these cascades delete-orphan, so an
    # assignment would make SQLAlchemy LOAD the collection first to find
    # orphans — lazy I/O in an async session, i.e. MissingGreenlet and a 500 on
    # "New Procedure". The row was just created, so "empty" is a fact we can
    # state without asking the database.
    for _rel in ("energySources", "isolationPoints", "hardware", "verificationSteps"):
        set_committed_value(proc, _rel, [])

    try:
        await svc.replace_body(
            db, proc,
            energy_sources=payload.energySources,
            isolation_points=payload.isolationPoints,
            hardware=payload.hardware,
            verification_steps=payload.verificationSteps,
        )
    except svc.LotoError as e:
        raise _err(e) from e

    await db.commit()
    proc = await svc.load_procedure(db, proc.id, with_versions=True)
    return await _procedure_out(db, proc)


@router.get("/procedures/{procedure_id}", response_model=ProcedureOut)
async def get_procedure(
    procedure_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProcedureOut:
    try:
        proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    except svc.LotoError as e:
        raise _err(e) from e
    await require_permission_with_context(
        READ, user, db, plant_id=proc.siteId, record_id=proc.id
    )
    return await _procedure_out(db, proc)


@router.put("/procedures/{procedure_id}", response_model=ProcedureOut)
async def update_procedure(
    procedure_id: str,
    payload: ProcedureUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProcedureOut:
    """Versioned edit.

    A MATERIAL change (isolation points / hardware / verification steps) bumps
    the version, writes an immutable snapshot of the NEW body, and — if the
    procedure was live — moves it to `under_review` so it must be re-approved
    before the field sees it. v1's snapshot is never overwritten.

    A retired procedure is not editable: bring it back with /publish first, so
    the reactivation is an explicit, permissioned act rather than a side effect
    of a save.
    """
    try:
        proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    except svc.LotoError as e:
        raise _err(e) from e

    await require_permission_with_context(
        UPDATE, user, db, plant_id=proc.siteId, record_id=proc.id
    )
    if proc.status == "retired":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This procedure is retired. Publish it again before editing it.",
        )

    data = payload.model_dump(exclude_unset=True)
    for field in (
        "areaId", "area", "equipmentId", "equipmentName", "equipmentTag",
        "title", "description", "reviewFrequencyMonths",
    ):
        if field in data and data[field] is not None:
            setattr(proc, field, data[field])

    try:
        material = await svc.replace_body(
            db, proc,
            energy_sources=payload.energySources,
            isolation_points=payload.isolationPoints,
            hardware=payload.hardware,
            verification_steps=payload.verificationSteps,
        )
        withdrew = await svc.apply_body_edit(
            db, proc, actor_id=user.id, material=material,
            change_summary=payload.changeSummary,
        )
    except svc.LotoError as e:
        raise _err(e) from e

    if withdrew:
        log.info(
            "LOTO %s: material edit withdrew live approval → under_review (now v%s)",
            proc.procedureCode, proc.version,
        )

    await db.commit()
    proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    return await _procedure_out(db, proc)


@router.post("/procedures/{procedure_id}/publish", response_model=ProcedureOut)
async def publish_procedure(
    procedure_id: str,
    payload: ProcedurePublishRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProcedureOut:
    """draft / under_review / retired → active.

    Requires LOTO.APPROVE, which is deliberately a narrower grant than UPDATE:
    publishing is the act that asserts the isolation sequence is correct, and an
    approval the author can grant themselves is not an approval.

    Generates the QR token on FIRST publish only. The token is immutable
    thereafter — it is printed on a label bolted to a machine, and rotating it
    would silently orphan every label in the plant.
    """
    try:
        proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    except svc.LotoError as e:
        raise _err(e) from e

    await require_permission_with_context(
        APPROVE, user, db, plant_id=proc.siteId, record_id=proc.id
    )

    if proc.status == "active" and proc.publishedVersionId:
        published = await db.get(LotoProcedureVersion, proc.publishedVersionId)
        if published and published.version == proc.version:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Version {proc.version} is already published and live.",
            )

    # §2.1 — validation, not a UI hint. Same list the builder showed.
    blockers = svc.publish_blockers(proc)
    if blockers:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This procedure cannot be published yet:\n• " + "\n• ".join(blockers),
        )

    if not proc.qrCodeToken:
        proc.qrCodeToken = svc.new_qr_token()

    await svc.record_version(
        db, proc, actor_id=user.id, change_type="MATERIAL",
        change_summary=payload.notes, publish=True,
    )
    proc.status = "active"
    if proc.nextReviewDueAt is None:
        proc.nextReviewDueAt = svc.compute_next_review(proc)
    proc.updatedById = user.id

    await db.commit()
    proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    return await _procedure_out(db, proc)


@router.post("/procedures/{procedure_id}/retire", response_model=ProcedureOut)
async def retire_procedure(
    procedure_id: str,
    payload: ProcedurePublishRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProcedureOut:
    """Withdraw a procedure from use without deleting it.

    Refuses while a lockout is running against it: retiring the procedure a crew
    is currently following would make the register claim the equipment is out of
    service while people are locked onto it.
    """
    try:
        proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    except svc.LotoError as e:
        raise _err(e) from e

    await require_permission_with_context(
        APPROVE, user, db, plant_id=proc.siteId, record_id=proc.id
    )

    open_execs = (
        await db.execute(
            select(LotoExecution.number)
            .where(LotoExecution.procedureId == proc.id)
            .where(LotoExecution.isDeleted.is_(False))
            .where(LotoExecution.status.in_(OPEN_EXECUTION_STATUSES))
        )
    ).scalars().all()
    if open_execs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Lockouts are still running against this procedure: "
            + ", ".join(open_execs)
            + ". Close them before retiring it.",
        )

    proc.status = "retired"
    proc.updatedById = user.id
    await db.commit()
    proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    return await _procedure_out(db, proc)


@router.post("/procedures/{procedure_id}/review", response_model=ProcedureOut)
async def complete_review(
    procedure_id: str,
    payload: ProcedureReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProcedureOut:
    """Complete a scheduled evaluation cycle (spec §3).

    Closes whatever pending LotoReviewLog exists — and creates a completed one if
    the scan has not run yet, so an early review is still recorded rather than
    silently discarded. Rolls `nextReviewDueAt` forward from NOW, not from the
    old due date: a procedure reviewed three months late gets a full fresh cycle,
    not one that is immediately three months old.
    """
    try:
        proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    except svc.LotoError as e:
        raise _err(e) from e

    await require_permission_with_context(
        REVIEW, user, db, plant_id=proc.siteId, record_id=proc.id
    )

    review = (
        await db.execute(
            select(LotoReviewLog)
            .where(LotoReviewLog.procedureId == proc.id)
            .where(LotoReviewLog.status == "pending")
            .order_by(LotoReviewLog.dueAt.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    now = _now()
    if review is None:
        review = LotoReviewLog(
            procedureId=proc.id,
            status="pending",
            dueAt=proc.nextReviewDueAt or now,
        )
        db.add(review)
        await db.flush()

    review.status = "completed"
    review.reviewedById = user.id
    review.reviewedByName = user.name
    review.reviewedAt = now
    review.outcome = payload.outcome
    review.notes = payload.notes
    if payload.outcome == "updated":
        review.newVersionId = proc.publishedVersionId

    proc.lastReviewedAt = now
    proc.lastReviewedById = user.id
    proc.nextReviewDueAt = svc.compute_next_review(proc, from_dt=now)

    # A failed review means the procedure no longer matches the plant. Leaving it
    # `active` would keep serving a sequence a reviewer has just said is wrong,
    # so it drops out of field circulation until it is corrected and re-approved.
    if payload.outcome == "fail" and proc.status == "active":
        proc.status = "under_review"

    await db.commit()
    proc = await svc.load_procedure(db, procedure_id, with_versions=True)
    return await _procedure_out(db, proc)


@router.get("/procedures/{procedure_id}/versions/{version_number}")
async def get_procedure_version(
    procedure_id: str,
    version_number: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """An historical version body — the proof of what the procedure said then."""
    try:
        proc = await svc.load_procedure(db, procedure_id)
    except svc.LotoError as e:
        raise _err(e) from e
    await require_permission_with_context(
        READ, user, db, plant_id=proc.siteId, record_id=proc.id
    )

    row = (
        await db.execute(
            select(LotoProcedureVersion)
            .where(LotoProcedureVersion.procedureId == procedure_id)
            .where(LotoProcedureVersion.version == version_number)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Version {version_number} does not exist"
        )
    return {
        "id": row.id,
        "version": row.version,
        "isPublished": row.isPublished,
        "publishedAt": row.publishedAt,
        "supersededAt": row.supersededAt,
        "changeType": row.changeType,
        "changeSummary": row.changeSummary,
        "createdAt": row.createdAt,
        "body": row.snapshotJson or {},
    }


@router.delete("/procedures/{procedure_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_procedure(
    procedure_id: str,
    payload: ProcedureDeleteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Soft-delete. Governed entity — the ORM guard blocks a hard delete outright."""
    try:
        proc = await svc.load_procedure(db, procedure_id)
    except svc.LotoError as e:
        raise _err(e) from e

    await require_permission_with_context(
        DELETE, user, db, plant_id=proc.siteId, record_id=proc.id
    )

    open_execs = (
        await db.execute(
            select(LotoExecution.number)
            .where(LotoExecution.procedureId == proc.id)
            .where(LotoExecution.isDeleted.is_(False))
            .where(LotoExecution.status.in_(OPEN_EXECUTION_STATUSES))
        )
    ).scalars().all()
    if open_execs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Lockouts are still running against this procedure: "
            + ", ".join(open_execs)
            + ". Close them first.",
        )

    soft_delete(proc, user.id, payload.reason)
    await db.commit()


# ═══════════════════════════════════════════════════════════════════════════
#  Public QR field access
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/qr/{token}", response_model=QrProcedureView)
async def resolve_qr(token: str, db: AsyncSession = Depends(get_db)) -> QrProcedureView:
    """Read-only field view. **NO AUTHENTICATION** — spec §2.3.

    This is the second unauthenticated endpoint in the product (after the
    supplier portal) and it is deliberate: someone standing at a machine with a
    phone must be able to read the isolation procedure without a login, and a
    login wall between a person and an energy-isolation procedure is a safety
    hazard, not a control.

    Three properties make that safe:

      1. **The token is the capability, and it is unguessable.** 24 random bytes,
         url-safe. There is no listing endpoint and no enumeration path.
      2. **The payload is narrow.** `QrProcedureView` carries the procedure body
         and nothing else — no author, no reviewer, no execution history, no
         other equipment, no site roster. Read the schema: it is the access
         boundary.
      3. **It is read-only and cannot start anything.** Starting a lockout is
         POST /executions, which requires a session and LOTO.EXECUTE. The field
         page's "Start lockout" button routes into the authenticated app.

    The page that consumes this is served at `/lockout/{token}` — deliberately
    NOT under `/loto/…`, which is the authenticated register. A public
    `[token]` route under that path collides with the register's `[id]` and
    takes every page in the Next app down with a 500.

    A bad token 404s with the same message whether it never existed, belongs to a
    deleted procedure, or was never published — a caller probing tokens learns
    nothing from the response.
    """
    NOT_FOUND = "This code does not match a published LOTO procedure."

    proc = (
        await db.execute(
            select(LotoProcedure)
            .options(
                selectinload(LotoProcedure.energySources),
                selectinload(LotoProcedure.isolationPoints),
                selectinload(LotoProcedure.hardware),
                selectinload(LotoProcedure.verificationSteps),
            )
            .where(LotoProcedure.qrCodeToken == token)
        )
    ).scalar_one_or_none()

    if proc is None or proc.isDeleted or not proc.publishedVersionId:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)

    version = await db.get(LotoProcedureVersion, proc.publishedVersionId)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)

    # THE PUBLISHED body, not the live one. While a material edit awaits
    # re-approval the field keeps seeing the last APPROVED sequence — handing a
    # crew an unreviewed isolation sequence is the failure this prevents.
    body = version.snapshotJson or {}
    header = body.get("header") or {}

    return QrProcedureView(
        procedureId=proc.id,
        procedureCode=proc.procedureCode,
        title=header.get("title") or proc.title,
        description=header.get("description") or proc.description,
        equipmentName=header.get("equipmentName") or proc.equipmentName,
        equipmentTag=header.get("equipmentTag") or proc.equipmentTag,
        siteName=header.get("siteName") or proc.siteName,
        area=header.get("area") or proc.area,
        version=version.version,
        publishedAt=version.publishedAt,
        procedureStatus=proc.status,
        isRetired=proc.status == "retired",
        hasUnpublishedChanges=proc.version != version.version,
        energySources=body.get("energySources") or [],
        isolationPoints=body.get("isolationPoints") or [],
        hardware=body.get("hardware") or [],
        verificationSteps=body.get("verificationSteps") or [],
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Execution
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/executions", response_model=ExecutionOut, status_code=status.HTTP_201_CREATED)
async def start_execution(
    payload: ExecutionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionOut:
    """Start a lockout. This is the moment the procedure snapshot is FROZEN."""
    try:
        proc = await svc.load_procedure(db, payload.procedureId)
    except svc.LotoError as e:
        raise _err(e) from e

    await require_permission_with_context(
        EXECUTE, user, db, plant_id=proc.siteId, record_id=proc.id
    )

    ptw_number: str | None = None
    if payload.ptwId:
        from app.models.permit import Permit

        permit = await db.get(Permit, payload.ptwId)
        if permit is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
        if permit.plantId != proc.siteId:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The permit and the LOTO procedure are at different sites.",
            )
        ptw_number = permit.number

    try:
        ex = await svc.start_execution(
            db,
            proc=proc,
            actor_id=user.id,
            actor_name=user.name,
            is_group=payload.isGroupLockout,
            participants=payload.participants,
            ptw_id=payload.ptwId,
            ptw_number=ptw_number,
        )
    except svc.LotoError as e:
        raise _err(e) from e

    # Denormalise participant display fields at enrolment so a roster is legible
    # even if a user row is later deactivated.
    refs = await resolve_user_directory(db, [p.userId for p in ex.participants])
    for p in ex.participants:
        ref = refs.get(p.userId)
        if ref:
            p.userName = ref.name
            p.userRole = ref.role

    # Link back onto the permit so the PTW closure gate can find it without a
    # scan. Writing only when the permit has no other live link avoids silently
    # re-pointing a permit that is already tied to a different lockout.
    if payload.ptwId:
        from app.models.permit import Permit

        permit = await db.get(Permit, payload.ptwId)
        if permit is not None and not permit.lotoExecutionId:
            permit.lotoExecutionId = ex.id

    await db.commit()
    ex = await svc.load_execution(db, ex.id)
    return await _execution_out(db, ex)


@router.get("/executions", response_model=ExecutionListResponse)
async def list_executions(
    ptwId: str | None = Query(default=None, description="Used by the PTW cross-reference panel"),
    procedureId: str | None = Query(default=None),
    siteId: str | None = Query(default=None),
    status_: str | None = Query(default=None, alias="status"),
    openOnly: bool = Query(default=False, description="Only lockouts still holding locks"),
    mine: bool = Query(default=False, description="Only lockouts I am enrolled on"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionListResponse:
    scope = await build_query_scope(db, user.id, READ)

    stmt = (
        select(LotoExecution)
        .options(selectinload(LotoExecution.participants))
        .where(LotoExecution.isDeleted.is_(False))
    )
    stmt = scope.apply(stmt, LotoExecution, plant_attr="siteId")

    if ptwId:
        stmt = stmt.where(LotoExecution.ptwId == ptwId)
    if procedureId:
        stmt = stmt.where(LotoExecution.procedureId == procedureId)
    if siteId:
        if not scope.allows_plant(siteId):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied for this site")
        stmt = stmt.where(LotoExecution.siteId == siteId)
    if status_:
        stmt = stmt.where(LotoExecution.status == status_)
    if openOnly:
        stmt = stmt.where(LotoExecution.status.in_(OPEN_EXECUTION_STATUSES))
    if mine:
        stmt = stmt.where(
            LotoExecution.id.in_(
                select(LotoExecutionParticipant.executionId).where(
                    LotoExecutionParticipant.userId == user.id
                )
            )
        )

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0
    rows = (
        await db.execute(
            stmt.order_by(LotoExecution.createdAt.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    site_names = await resolve_plant_names(db, [r.siteId for r in rows])
    user_refs = await resolve_user_directory(db, [r.initiatedById for r in rows])
    return ExecutionListResponse(
        items=[
            _execution_list_item(r, site_names=site_names, user_refs=user_refs)
            for r in rows
        ],
        total=total,
    )


@router.get("/executions/{execution_id}", response_model=ExecutionOut)
async def get_execution(
    execution_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionOut:
    try:
        ex = await svc.load_execution(db, execution_id)
    except svc.LotoError as e:
        raise _err(e) from e
    await require_permission_with_context(
        READ, user, db, plant_id=ex.siteId, record_id=ex.id
    )
    return await _execution_out(db, ex)


@router.patch("/executions/{execution_id}/lock", response_model=ExecutionOut)
async def confirm_lock(
    execution_id: str,
    payload: LockConfirmRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionOut:
    """Confirm YOUR OWN lock is on the equipment.

    Per-participant by construction: the service resolves the target row from the
    authenticated user, and refuses a `participantId` that is not theirs. Status
    reaches `locks_applied`-complete only when every lock holder has done this
    individually — there is no path by which one person's confirmation counts
    for the group.
    """
    try:
        ex = await svc.load_execution(db, execution_id)
        await require_permission_with_context(
            EXECUTE, user, db, plant_id=ex.siteId, record_id=ex.id
        )
        await svc.confirm_lock(
            db, ex,
            user_id=user.id,
            participant_id=payload.participantId,
            lock_tag_number=payload.lockTagNumber,
            notes=payload.notes,
        )
    except svc.LotoError as e:
        raise _err(e) from e

    await db.commit()
    ex = await svc.load_execution(db, execution_id)
    return await _execution_out(db, ex)


@router.patch("/executions/{execution_id}/verify", response_model=ExecutionOut)
async def verify_execution(
    execution_id: str,
    payload: VerifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionOut:
    """Record completed zero-energy verification steps.

    Steps are validated against the execution's FROZEN snapshot, so a step added
    to the live procedure mid-job is never silently demanded of a crew that never
    saw it, and a step deleted from the live procedure is still completable here.
    """
    try:
        ex = await svc.load_execution(db, execution_id)
        await require_permission_with_context(
            EXECUTE, user, db, plant_id=ex.siteId, record_id=ex.id
        )
        await svc.record_verification(
            db, ex, records=payload.records, user_id=user.id, user_name=user.name
        )
    except svc.LotoError as e:
        raise _err(e) from e

    await db.commit()
    ex = await svc.load_execution(db, execution_id)
    return await _execution_out(db, ex)


@router.patch("/executions/{execution_id}/start-work", response_model=ExecutionOut)
async def start_work(
    execution_id: str,
    payload: ExecutionStartWorkRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionOut:
    """verified → work_in_progress. A state marker (spec §2.7); no side effects."""
    try:
        ex = await svc.load_execution(db, execution_id)
        await require_permission_with_context(
            EXECUTE, user, db, plant_id=ex.siteId, record_id=ex.id
        )
        await svc.start_work(db, ex)
    except svc.LotoError as e:
        raise _err(e) from e

    await db.commit()
    ex = await svc.load_execution(db, execution_id)
    return await _execution_out(db, ex)


@router.patch("/executions/{execution_id}/unlock", response_model=ExecutionOut)
async def confirm_unlock(
    execution_id: str,
    payload: UnlockConfirmRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionOut:
    """Confirm YOUR OWN lock has come off.

    Deliberately the mirror image of /lock, with NO bulk counterpart: there is no
    "remove all" endpoint anywhere in this module. A single action that clears
    every participant's lock is indistinguishable, in the record, from one person
    removing another's lock — the precise thing lockout procedure exists to
    prevent. Where a lock genuinely must come off without its owner, that is
    POST /abort with a recorded reason: a visible exception, not a silent one.
    """
    try:
        ex = await svc.load_execution(db, execution_id)
        await require_permission_with_context(
            EXECUTE, user, db, plant_id=ex.siteId, record_id=ex.id
        )
        await svc.confirm_unlock(
            db, ex, user_id=user.id, participant_id=payload.participantId,
            notes=payload.notes,
        )
    except svc.LotoError as e:
        raise _err(e) from e

    await db.commit()
    ex = await svc.load_execution(db, execution_id)
    return await _execution_out(db, ex)


@router.post("/executions/{execution_id}/close", response_model=ExecutionOut)
async def close_execution(
    execution_id: str,
    payload: ExecutionCloseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionOut:
    """Final sign-off.

    Blocked, with specifics, if any participant never confirmed removal or any
    verification step is outstanding — and the 409 names who and what, so the
    closer can go and fix it. A silent close over an incomplete record is exactly
    the failure pattern found in the HIRA re-approval gap; it is not repeated.
    """
    try:
        ex = await svc.load_execution(db, execution_id)
        await require_permission_with_context(
            EXECUTE, user, db, plant_id=ex.siteId, record_id=ex.id
        )
        await svc.close_execution(db, ex, user_id=user.id, notes=payload.closureNotes)
    except svc.LotoError as e:
        raise _err(e) from e

    await db.commit()
    ex = await svc.load_execution(db, execution_id)
    return await _execution_out(db, ex)


@router.post("/executions/{execution_id}/abort", response_model=ExecutionOut)
async def abort_execution(
    execution_id: str,
    payload: ExecutionAbortRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExecutionOut:
    """Abandon a lockout with a recorded reason.

    Requires LOTO.APPROVE, not EXECUTE: this is the one path that ends a lockout
    without every lock being individually accounted for, so it sits with the
    people who publish procedures rather than the people who run them.
    """
    try:
        ex = await svc.load_execution(db, execution_id)
        await require_permission_with_context(
            APPROVE, user, db, plant_id=ex.siteId, record_id=ex.id
        )
        await svc.abort_execution(db, ex, user_id=user.id, reason=payload.reason)
    except svc.LotoError as e:
        raise _err(e) from e

    await db.commit()
    ex = await svc.load_execution(db, execution_id)
    return await _execution_out(db, ex)


# ═══════════════════════════════════════════════════════════════════════════
#  PTW cross-reference (spec §6)
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/permits/{permit_id}/status", response_model=PermitLotoStatus)
async def permit_loto_status(
    permit_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PermitLotoStatus:
    """What the PTW detail panel renders, and the same verdict its closure gate
    reads — one source of truth, so the panel cannot say "clear" about a permit
    the engine will refuse to close."""
    from app.models.permit import Permit

    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    await require_permission_with_context(
        "PTW.READ", user, db, plant_id=permit.plantId, record_id=permit.id
    )

    item: ExecutionListItem | None = None
    if permit.lotoExecutionId:
        ex = (
            await db.execute(
                select(LotoExecution)
                .options(selectinload(LotoExecution.participants))
                .where(LotoExecution.id == permit.lotoExecutionId)
            )
        ).scalar_one_or_none()
        if ex is not None:
            site_names = await resolve_plant_names(db, [ex.siteId])
            user_refs = await resolve_user_directory(db, [ex.initiatedById])
            item = _execution_list_item(ex, site_names=site_names, user_refs=user_refs)

    blocker = await svc.permit_closure_blocker(db, permit_id)
    return PermitLotoStatus(
        permitId=permit.id,
        lotoExecutionId=permit.lotoExecutionId,
        execution=item,
        blocksPermitClosure=blocker is not None,
        blockReason=blocker,
    )


@router.post("/permits/{permit_id}/link", response_model=PermitLotoStatus)
async def link_permit(
    permit_id: str,
    payload: PermitLinkRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PermitLotoStatus:
    """Link a permit to an existing lockout, or unlink it.

    A REFERENCE action, not a merge: it writes one nullable column on Permit and
    touches nothing else in PTW. Requires PTW.UPDATE (it is the permit being
    modified) plus LOTO.READ (you cannot link to a lockout you cannot see).

    Unlinking is refused while the linked lockout is still open — otherwise the
    closure gate could be bypassed by dropping the link rather than closing the
    lockout, which would defeat the entire point of §6.
    """
    from app.models.permit import Permit

    permit = await db.get(Permit, permit_id)
    if permit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permit not found")
    await require_permission_with_context(
        "PTW.UPDATE", user, db, plant_id=permit.plantId, record_id=permit.id
    )

    if payload.lotoExecutionId is None:
        blocker = await svc.permit_closure_blocker(db, permit_id)
        if blocker:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This lockout is still open — close or abort it rather than "
                "unlinking it from the permit.",
            )
        permit.lotoExecutionId = None
    else:
        try:
            ex = await svc.load_execution(db, payload.lotoExecutionId)
        except svc.LotoError as e:
            raise _err(e) from e
        await require_permission_with_context(
            READ, user, db, plant_id=ex.siteId, record_id=ex.id
        )
        if ex.siteId != permit.plantId:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The lockout and the permit are at different sites.",
            )
        permit.lotoExecutionId = ex.id
        # Keep the back-reference in step so the LOTO register shows the permit
        # too. Never steal a lockout already claimed by a different permit.
        if not ex.ptwId:
            ex.ptwId = permit.id
            ex.ptwNumber = permit.number

    await db.commit()
    return await permit_loto_status(permit_id, db, user)
