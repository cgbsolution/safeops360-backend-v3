"""Business Excellence router. Mounted at /api/be.

  Meta
    GET    /api/be/meta                          — every vocabulary the forms need

  Kaizen
    GET    /api/be/kaizen                        — register (search/filter/sort/export)
    GET    /api/be/kaizen/search                 — similar-idea lookup, scoped
    GET    /api/be/kaizen/participation          — engagement figures for a plant
    GET    /api/be/kaizen/export                 — CSV / PDF of the filtered set
    POST   /api/be/kaizen                        — create (DRAFT)
    GET    /api/be/kaizen/{id}                   — detail + availableActions
    PATCH  /api/be/kaizen/{id}                   — edit (blocked once terminal)
    POST   /api/be/kaizen/{id}/submit            — DRAFT → workflow
    POST   /api/be/kaizen/{id}/transition/{to}   — the state machine, server-gated
    POST   /api/be/kaizen/{id}/replicate         — deploy the idea at another plant
    POST   /api/be/kaizen/{id}/create-opl        — closed loop into standard work
    POST   /api/be/kaizen/{id}/reject            — with a reason
    DELETE /api/be/kaizen/{id}                   — soft-delete

  One Point Lesson
    GET    /api/be/opl                           — register
    POST   /api/be/opl                           — create (DRAFT)
    GET    /api/be/opl/{id}                      — detail + my own acknowledgement
    PATCH  /api/be/opl/{id}                      — edit (DRAFT / IN_REVIEW only)
    POST   /api/be/opl/{id}/submit               — DRAFT → workflow
    POST   /api/be/opl/{id}/publish              — APPROVED → PUBLISHED + assign audience
    POST   /api/be/opl/{id}/retire               — withdraw from the floor
    GET    /api/be/opl/{id}/acknowledgements     — the matrix (Phase 2 UI reads this)
    POST   /api/be/opl/{id}/acknowledge          — the caller's own confirmation
    GET    /api/be/opl/mine                      — what the caller still has to read
    DELETE /api/be/opl/{id}                      — soft-delete

  Poka Yoke
    GET    /api/be/poka-yoke                     — register (+ overdue filter)
    POST   /api/be/poka-yoke                     — create (PROPOSED)
    GET    /api/be/poka-yoke/{id}                — detail + verification history
    PATCH  /api/be/poka-yoke/{id}                — edit
    POST   /api/be/poka-yoke/{id}/submit         — PROPOSED → workflow
    POST   /api/be/poka-yoke/{id}/install        — APPROVED → INSTALLED
    POST   /api/be/poka-yoke/{id}/verify         — record a check (FAIL raises CAPA)
    POST   /api/be/poka-yoke/{id}/bypass         — log an override (opens an episode)
    POST   /api/be/poka-yoke/{id}/restore        — end this device's open bypass
    POST   /api/be/poka-yoke/bypasses/{bid}/restore — end one bypass by its own id
    POST   /api/be/poka-yoke/{id}/retire         — withdraw the device
    DELETE /api/be/poka-yoke/{id}                — soft-delete

⚠ ROUTE ORDER. /kaizen/search, /kaizen/participation and /kaizen/export are
declared BEFORE /kaizen/{kaizen_id}. FastAPI matches in declaration order, so
declaring them after would make "search" a kaizen_id and 404 every call. The
same trap already exists on /opl/mine above /opl/{opl_id}.

Plant scoping is fail-closed on every list via access_scope.build_query_scope,
and re-checked per record on every read and write. Transitions are gated
server-side and the permitted set is returned on the payload, so the UI never
has to re-derive the rules — a client that guesses produces either a button that
403s or a hidden action the user was entitled to.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.core.soft_delete import soft_delete
from app.models.business_excellence import (
    KAIZEN_CATEGORIES,
    KAIZEN_LANES,
    KAIZEN_STATUSES,
    OPL_CATEGORIES,
    OPL_STATUSES,
    POKA_YOKE_APPROACHES,
    POKA_YOKE_DEVICE_TYPES,
    POKA_YOKE_REACTIONS,
    POKA_YOKE_STATUSES,
    ACK_STATUSES,
    BE_SOURCE_MODULES,
    SAVING_TYPES,
    VERIFICATION_FREQUENCIES,
    VERIFICATION_RESULTS,
    BeKaizen,
    BeKaizenReplication,
    BeOpl,
    BeOplAcknowledgement,
    BePokaYoke,
    BePokaYokeBypass,
    BePokaYokeVerification,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas.business_excellence import (
    AckConfirm,
    AckListResponse,
    AckOut,
    BypassRequest,
    KaizenContributorOut,
    KaizenCreate,
    KaizenCycleTimeOut,
    KaizenListResponse,
    KaizenOut,
    KaizenParticipationOut,
    KaizenReject,
    KaizenReplicateIn,
    KaizenReplicationOut,
    KaizenSearchHit,
    KaizenSearchResponse,
    KaizenTransition,
    KaizenUpdate,
    MetaOut,
    OplCreate,
    OplListResponse,
    OplOut,
    OplUpdate,
    BypassOut,
    BypassRestore,
    PokaYokeCreate,
    PokaYokeListResponse,
    PokaYokeOut,
    PokaYokeUpdate,
    VerificationCreate,
    VerificationOut,
)
from app.services import business_excellence as be
from app.services.access_scope import QueryScope, build_query_scope
from app.services.permissions import get_permissions
from app.services.user_directory import resolve_user_directory

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/be", tags=["business-excellence"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _granted(db: AsyncSession, user: User) -> set[str]:
    """The caller's permission codes as a set, for the availableActions gate."""
    perms = await get_permissions(db, user.id)
    return {code for code, ok in perms.items() if ok}


async def _audit_reason(request: Request) -> str:
    return request.headers.get("x-audit-reason") or "Deleted from the Business Excellence register"


# ─────────────────────────────────────────────────────────────────────────────
# Meta
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/meta", response_model=MetaOut)
async def meta(user: User = Depends(get_current_user)) -> MetaOut:
    """Served from the same tuples Pydantic validates against, so a dropdown
    can never offer a value the API will reject."""
    return MetaOut(
        kaizenCategories=list(KAIZEN_CATEGORIES),
        kaizenLanes=list(KAIZEN_LANES),
        kaizenStatuses=list(KAIZEN_STATUSES),
        savingTypes=list(SAVING_TYPES),
        oplCategories=list(OPL_CATEGORIES),
        oplStatuses=list(OPL_STATUSES),
        ackStatuses=list(ACK_STATUSES),
        pokaYokeDeviceTypes=list(POKA_YOKE_DEVICE_TYPES),
        pokaYokeApproaches=list(POKA_YOKE_APPROACHES),
        pokaYokeReactions=list(POKA_YOKE_REACTIONS),
        pokaYokeStatuses=list(POKA_YOKE_STATUSES),
        verificationFrequencies=list(VERIFICATION_FREQUENCIES),
        verificationResults=list(VERIFICATION_RESULTS),
        sourceModules=list(BE_SOURCE_MODULES),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen
# ─────────────────────────────────────────────────────────────────────────────
def _kaizen_list_item(
    k: BeKaizen, users: dict, *, replications: dict[str, int] | None = None
) -> dict[str, Any]:
    return {
        "id": k.id,
        "kaizenNo": k.kaizenNo,
        "title": k.title,
        "category": k.category,
        "lane": k.lane,
        "status": k.status,
        "plantId": k.plantId,
        "siteName": k.siteName,
        "areaName": k.areaName,
        "lineOrMachine": k.lineOrMachine,
        "owner": users.get(k.ownerId) if k.ownerId else None,
        "raisedBy": users.get(k.createdById),
        "targetDate": k.targetDate,
        "implementedAt": k.implementedAt,
        "currency": k.currency,
        "estimatedAnnualSaving": k.estimatedAnnualSaving,
        "verifiedAnnualSaving": k.verifiedAnnualSaving,
        "isOverdue": be.is_kaizen_overdue(k),
        "createdAt": k.createdAt,
        # Derived on every read, never stored. `lane` still says which approval
        # workflow this record runs through; `fastTrack` says whether the money
        # and time figures actually support the claim the badge makes.
        "fastTrack": be.fast_track_eligibility(k)["eligible"],
        "replicationCount": (replications or {}).get(k.id, 0),
    }


async def _replication_counts(db: AsyncSession, kaizen_ids: list[str]) -> dict[str, int]:
    """How many plants each of these ideas has actually been deployed at.

    One grouped query for the whole page. Doing it per row would be N+1 on the
    register, which is the screen most likely to be left open on a wall display.
    """
    if not kaizen_ids:
        return {}
    rows = (
        await db.execute(
            select(
                BeKaizenReplication.sourceKaizenId, func.count(BeKaizenReplication.id)
            )
            .where(BeKaizenReplication.sourceKaizenId.in_(kaizen_ids))
            .group_by(BeKaizenReplication.sourceKaizenId)
        )
    ).all()
    return {source_id: count for source_id, count in rows}


class _CycleRow(NamedTuple):
    """The three columns kaizen_cycle_times reads, and nothing else.

    The aggregate is computed over the caller's ENTIRE filtered scope, which on
    a real plant is thousands of rows. Hydrating whole BeKaizen entities to read
    three timestamps off each one is the shape of query that is invisible on a
    four-record demo and is the first thing to fall over at scale.
    """

    createdAt: datetime | None
    implementedAt: datetime | None
    verifiedAt: datetime | None


#: What the register may be sorted by, and the column each name maps to. A
#: whitelist rather than getattr(BeKaizen, sort): a client-supplied attribute
#: name reaching getattr is how a sort parameter becomes a way to probe the
#: model. Unknown values fall back to the platform default (newest first).
KAIZEN_SORTS: dict[str, Any] = {
    "createdAt": BeKaizen.createdAt,
    "targetDate": BeKaizen.targetDate,
    "estimatedAnnualSaving": BeKaizen.estimatedAnnualSaving,
    "verifiedAnnualSaving": BeKaizen.verifiedAnnualSaving,
    "title": BeKaizen.title,
    "status": BeKaizen.status,
}


def _kaizen_filters(
    stmt,
    *,
    plantId: str | None,
    statuses: list[str] | None,
    category: str | None,
    lane: str | None,
    mine_user_id: str | None,
    q: str | None,
    raisedFrom: datetime | None,
    raisedTo: datetime | None,
    savingMin: float | None,
    savingMax: float | None,
):
    """Every filter the register and the export share, applied once.

    Shared rather than duplicated because the export's whole job is to hand
    somebody the rows they are looking at. Two copies of this drift, and the
    failure is silent: the CSV quietly contains different records from the
    screen it was downloaded from, and nobody checks.
    """
    if plantId:
        # Narrowing WITHIN the scope, never widening it — a plantId the scope
        # excludes still yields nothing.
        stmt = stmt.where(BeKaizen.plantId == plantId)
    if statuses:
        stmt = stmt.where(BeKaizen.status.in_(statuses))
    if category:
        stmt = stmt.where(BeKaizen.category == category)
    if lane:
        stmt = stmt.where(BeKaizen.lane == lane)
    if mine_user_id:
        stmt = stmt.where(
            or_(BeKaizen.createdById == mine_user_id, BeKaizen.ownerId == mine_user_id)
        )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                BeKaizen.title.ilike(like),
                BeKaizen.kaizenNo.ilike(like),
                BeKaizen.problemStatement.ilike(like),
                BeKaizen.proposedImprovement.ilike(like),
                BeKaizen.lineOrMachine.ilike(like),
            )
        )
    if raisedFrom:
        stmt = stmt.where(BeKaizen.createdAt >= raisedFrom)
    if raisedTo:
        stmt = stmt.where(BeKaizen.createdAt <= raisedTo)
    # Saving range reads the VERIFIED figure where one exists and falls back to
    # the estimate. Filtering on the estimate alone would drop every closed
    # record whose verified figure landed in range but whose estimate did not —
    # which is exactly the set somebody filtering by saving is looking for.
    if savingMin is not None or savingMax is not None:
        saving = func.coalesce(BeKaizen.verifiedAnnualSaving, BeKaizen.estimatedAnnualSaving)
        if savingMin is not None:
            stmt = stmt.where(saving >= savingMin)
        if savingMax is not None:
            stmt = stmt.where(saving <= savingMax)
    return stmt


def _split_statuses(raw: str | None) -> list[str] | None:
    """Parse the multi-select status parameter: "SUBMITTED,SCREENED".

    Unknown tokens are dropped rather than 422'd. A stale bookmark carrying a
    status this build renamed should show the rest of the filter, not an error
    page — and an all-unknown list becomes None, which shows everything, not
    nothing.
    """
    if not raw:
        return None
    wanted = [t.strip().upper() for t in raw.split(",") if t.strip()]
    valid = [t for t in wanted if t in KAIZEN_STATUSES]
    return valid or None


@router.get("/kaizen", response_model=KaizenListResponse)
async def list_kaizen(
    plantId: str | None = Query(default=None),
    kstatus: str | None = Query(
        default=None,
        alias="status",
        description="One status, or several comma-separated: 'SUBMITTED,SCREENED'.",
    ),
    category: str | None = Query(default=None),
    lane: str | None = Query(default=None),
    overdue: bool | None = Query(default=None),
    mine: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    raisedFrom: datetime | None = Query(default=None),
    raisedTo: datetime | None = Query(default=None),
    savingMin: float | None = Query(default=None, ge=0),
    savingMax: float | None = Query(default=None, ge=0),
    sort: str = Query(default="createdAt"),
    direction: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenListResponse:
    scope = await build_query_scope(db, user.id, "KAIZEN.READ")
    statuses = _split_statuses(kstatus)

    def scoped(stmt):
        return _kaizen_filters(
            scope.apply(stmt, BeKaizen, plant_attr="plantId"),
            plantId=plantId,
            statuses=statuses,
            category=category,
            lane=lane,
            mine_user_id=user.id if mine else None,
            q=q,
            raisedFrom=raisedFrom,
            raisedTo=raisedTo,
            savingMin=savingMin,
            savingMax=savingMax,
        )

    column = KAIZEN_SORTS.get(sort, BeKaizen.createdAt)
    # NULLS LAST in both directions. targetDate and the two saving columns are
    # nullable, and Postgres sorts NULLs first on ASC — so "cheapest first"
    # would open with a page of records that have no figure at all.
    order = column.asc().nullslast() if direction == "asc" else column.desc().nullslast()

    rows = (
        await db.execute(
            scoped(select(BeKaizen))
            # Tiebreak on id so pagination is stable: without it, two records
            # sharing a targetDate can swap places between page 1 and page 2 and
            # one of them is never seen.
            .order_by(order, BeKaizen.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    # Overdue is derived, so it filters in Python rather than SQL. Bounded by
    # `limit`, and the alternative — persisting an overdue flag — needs a job
    # and is wrong for as long as that job is broken.
    if overdue:
        rows = [k for k in rows if be.is_kaizen_overdue(k)]

    # `total` and `statusCounts` answer the SAME filtered question the page
    # does, minus pagination. They used to be computed over the unfiltered
    # scope, which meant the tile said "22 ideas" above a list showing four.
    total = (
        await db.execute(scoped(select(func.count(BeKaizen.id))))
    ).scalar_one() or 0

    counts_rows = (
        await db.execute(
            scoped(select(BeKaizen.status, func.count(BeKaizen.id))).group_by(BeKaizen.status)
        )
    ).all()

    # Cycle time over the whole filtered set, not the returned page — a median
    # of whatever fitted on page one is not a median. Only the four timestamp
    # columns it needs are selected; pulling whole entities for a plant with
    # thousands of records to compute two numbers is the kind of thing that
    # works fine on a demo and falls over in production.
    cycle_rows = (
        await db.execute(
            scoped(
                select(
                    BeKaizen.createdAt,
                    BeKaizen.implementedAt,
                    BeKaizen.verifiedAt,
                )
            )
        )
    ).all()
    cycle = be.kaizen_cycle_times(
        [
            _CycleRow(createdAt=c, implementedAt=i, verifiedAt=v)
            for c, i, v in cycle_rows
        ]
    )

    users = await resolve_user_directory(
        db, [k.ownerId for k in rows] + [k.createdById for k in rows]
    )
    reps = await _replication_counts(db, [k.id for k in rows])
    return KaizenListResponse(
        items=[_kaizen_list_item(k, users, replications=reps) for k in rows],
        total=total,
        statusCounts={s: c for s, c in counts_rows},
        cycleTime=KaizenCycleTimeOut(**cycle),
    )


async def _load_kaizen(db: AsyncSession, user: User, kaizen_id: str, perm: str) -> BeKaizen:
    record = await db.get(BeKaizen, kaizen_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kaizen not found")
    await require_permission_with_context(
        perm, user, db, record_id=record.id, plant_id=record.plantId
    )
    return record


async def _kaizen_replications(db: AsyncSession, kaizen_id: str) -> list[KaizenReplicationOut]:
    """This idea's recorded deployments at other plants, newest first."""
    rows = (
        await db.execute(
            select(BeKaizenReplication)
            .where(BeKaizenReplication.sourceKaizenId == kaizen_id)
            .order_by(BeKaizenReplication.replicatedAt.desc())
        )
    ).scalars().all()
    if not rows:
        return []

    users = await resolve_user_directory(db, [r.replicatedById for r in rows])
    # Resolve the copy's NUMBER, never hand the client a cuid to render. The
    # replica rows are read with include_deleted so a deleted copy still shows
    # as "was replicated here"; the deployment happened whatever became of the
    # record raised for it.
    replica_ids = [r.replicaKaizenId for r in rows if r.replicaKaizenId]
    numbers: dict[str, str | None] = {}
    if replica_ids:
        numbers = {
            rid: no
            for rid, no in (
                await db.execute(
                    select(BeKaizen.id, BeKaizen.kaizenNo).where(BeKaizen.id.in_(replica_ids))
                    .execution_options(include_deleted=True)
                )
            ).all()
        }

    return [
        KaizenReplicationOut(
            id=r.id,
            replicatedAtPlantId=r.replicatedAtPlantId,
            replicatedAtPlantName=r.replicatedAtPlantName,
            replicaKaizenId=r.replicaKaizenId,
            replicaKaizenNo=numbers.get(r.replicaKaizenId) if r.replicaKaizenId else None,
            replicatedBy=users.get(r.replicatedById),
            replicatedAt=r.replicatedAt,
            notes=r.notes,
        )
        for r in rows
    ]


async def _kaizen_out(db: AsyncSession, k: BeKaizen, granted: set[str]) -> KaizenOut:
    users = await resolve_user_directory(db, [k.ownerId, k.createdById, k.verifiedById])
    base = _kaizen_list_item(k, users)

    eligibility = be.fast_track_eligibility(k)

    origin_no: str | None = None
    if k.originKaizenId:
        origin_no = (
            await db.execute(
                select(BeKaizen.kaizenNo).where(BeKaizen.id == k.originKaizenId)
                .execution_options(include_deleted=True)
            )
        ).scalar_one_or_none()

    opl_no: str | None = None
    opl_title: str | None = None
    if k.generatedOplId:
        row = (
            await db.execute(
                select(BeOpl.oplNo, BeOpl.title).where(BeOpl.id == k.generatedOplId)
            )
        ).first()
        if row is not None:
            opl_no, opl_title = row

    return KaizenOut(
        **base,
        screenedAt=k.screenedAt,
        approvedAt=k.approvedAt,
        fastTrackReasons=eligibility["reasons"],
        fastTrackMaxInvestment=eligibility["maxInvestment"],
        fastTrackMaxImplementationDays=eligibility["maxImplementationDays"],
        # Same function the transition endpoint enforces. The button's disabled
        # state and the server's 409 are one rule read twice, not two rules that
        # have to be kept in step by hand.
        approvalBlockers=be.kaizen_approval_blockers(k),
        originKaizenId=k.originKaizenId,
        originKaizenNo=origin_no,
        replications=await _kaizen_replications(db, k.id),
        generatedOplId=k.generatedOplId,
        generatedOplNo=opl_no,
        generatedOplTitle=opl_title,
        recognitionEventId=k.recognitionEventId,
        problemStatement=k.problemStatement,
        currentState=k.currentState,
        proposedImprovement=k.proposedImprovement,
        expectedBenefit=k.expectedBenefit,
        processStep=k.processStep,
        investmentCost=k.investmentCost,
        savingType=k.savingType,
        implementationNote=k.implementationNote,
        verifiedBy=users.get(k.verifiedById) if k.verifiedById else None,
        verifiedAt=k.verifiedAt,
        verificationNote=k.verificationNote,
        yokotenScope=k.yokotenScope,
        rewardPoints=k.rewardPoints,
        rejectionReason=k.rejectionReason,
        closedAt=k.closedAt,
        sourceModule=k.sourceModule,
        sourceRecordId=k.sourceRecordId,
        sourceRecordRef=k.sourceRecordRef,
        workflowInstanceId=k.workflowInstanceId,
        updatedAt=k.updatedAt,
        availableActions=be.allowed_kaizen_actions(k, granted),
    )


@router.post("/kaizen", response_model=KaizenOut, status_code=status.HTTP_201_CREATED)
async def create_kaizen(
    body: KaizenCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenOut:
    await require_permission_with_context("KAIZEN.CREATE", user, db, plant_id=body.plantId)

    if not await be.validate_area(db, plant_id=body.plantId, area_id=body.areaId):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That area does not belong to the selected plant.",
        )
    site_name, area_name = await be.resolve_site_labels(
        db, plant_id=body.plantId, area_id=body.areaId
    )

    record = BeKaizen(
        **body.model_dump(exclude={"plantId", "areaId"}),
        plantId=body.plantId,
        areaId=body.areaId,
        siteName=site_name,
        areaName=area_name,
        status="DRAFT",
        createdById=user.id,
    )
    db.add(record)
    await db.flush()
    await db.commit()
    # updatedAt is populated by an inline NOW() the ORM cannot see until the row
    # is re-read — see _base.py. Without this refresh the response carries a
    # null the client then renders as "never updated".
    await db.refresh(record)
    return await _kaizen_out(db, record, await _granted(db, user))


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen — search, participation, export
#
# ⚠ These three are declared BEFORE /kaizen/{kaizen_id}. FastAPI matches routes
# in declaration order, so moving any of them below it makes "search" a
# kaizen_id and 404s every call.
# ─────────────────────────────────────────────────────────────────────────────
#: Below this trigram similarity two ideas are not the same idea. Tuned to be
#: forgiving: this fires while somebody is still typing a title, so it must
#: tolerate a half-typed word and a misspelling. A duplicate shown and dismissed
#: costs a glance; a duplicate never shown costs the whole feature.
SIMILARITY_FLOOR = 0.12


@router.get("/kaizen/search", response_model=KaizenSearchResponse)
async def search_kaizen(
    q: str = Query(min_length=3, max_length=200),
    limit: int = Query(default=8, ge=1, le=50),
    excludeId: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenSearchResponse:
    """"Has this already been solved somewhere else?"

    Ranked by pg_trgm similarity across title, problem and countermeasure, so a
    half-typed title still matches. Full-text search was the other candidate and
    is wrong for this: the match has to fire on "conveyor gu" before the word
    "guard" exists, and to_tsquery has nothing to match on yet.

    SCOPE. This searches every plant the CALLER MAY READ, resolved through the
    same fail-closed `build_query_scope` every other list uses — never the whole
    table. That is what makes "never cross-tenant" true by construction rather
    than by a filter someone has to remember: a user who cannot read a plant
    cannot discover its ideas through the search box either. `plantsSearched` is
    returned so the UI can say how wide the search actually was, and a nil result
    reads as "not raised at the plants you can see" rather than "not raised
    anywhere".
    """
    scope = await build_query_scope(db, user.id, "KAIZEN.READ")

    similarity = func.greatest(
        func.similarity(BeKaizen.title, q),
        func.similarity(BeKaizen.problemStatement, q),
        func.similarity(BeKaizen.proposedImprovement, q),
    ).label("similarity")

    stmt = scope.apply(select(BeKaizen, similarity), BeKaizen, plant_attr="plantId")
    # DRAFT is somebody's unfinished note. Surfacing it as prior art invites a
    # user to abandon their own idea in favour of one that was never submitted.
    stmt = stmt.where(BeKaizen.status != "DRAFT")
    if excludeId:
        stmt = stmt.where(BeKaizen.id != excludeId)
    stmt = stmt.where(similarity >= SIMILARITY_FLOOR)
    stmt = stmt.order_by(similarity.desc(), BeKaizen.createdAt.desc()).limit(limit)

    rows = (await db.execute(stmt)).all()

    plants_searched = (
        len(scope.plant_ids)
        if not scope.all_plants
        else ((await db.execute(select(func.count(Plant.id)))).scalar_one() or 0)
    )

    return KaizenSearchResponse(
        items=[
            KaizenSearchHit(
                id=k.id,
                kaizenNo=k.kaizenNo,
                title=k.title,
                status=k.status,
                category=k.category,
                plantId=k.plantId,
                siteName=k.siteName,
                createdAt=k.createdAt,
                similarity=round(float(score), 3),
            )
            for k, score in rows
        ],
        plantsSearched=plants_searched,
    )


@router.get("/kaizen/participation", response_model=KaizenParticipationOut)
async def kaizen_participation(
    plantId: str | None = Query(default=None),
    raisedFrom: datetime | None = Query(default=None),
    raisedTo: datetime | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenParticipationOut:
    """§3 — how many people are actually contributing, against real headcount.

    The denominator is `FactoryProfile.totalEmployees`, which is the ONLY
    populated headcount on this platform: Manhours.headcount is 0 across all 204
    rows, ScorecardPeriod.headcount is 0 across all 396, and
    ManhoursEmployeeCategory has no rows at all. When a plant has no factory
    profile the rate comes back null and the screen says so — 16 of 28 plants are
    in that state, so it is the normal case, not an edge one.

    Counting platform `User` rows instead was the tempting shortcut and would
    have been wrong by an order of magnitude: a plant with 880 workers has 59
    logins, so 2 submitters would have read as 3% engagement instead of 0.2%.
    """
    scope = await build_query_scope(db, user.id, "KAIZEN.READ")

    stmt = scope.apply(select(BeKaizen), BeKaizen, plant_attr="plantId")
    if plantId:
        if not scope.allows_plant(plantId):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "You cannot read Kaizen records at that plant."
            )
        stmt = stmt.where(BeKaizen.plantId == plantId)
    # A draft is not a contribution — it has not been offered to anybody yet.
    stmt = stmt.where(BeKaizen.status != "DRAFT")
    if raisedFrom:
        stmt = stmt.where(BeKaizen.createdAt >= raisedFrom)
    if raisedTo:
        stmt = stmt.where(BeKaizen.createdAt <= raisedTo)

    rows = (await db.execute(stmt)).scalars().all()

    # Headcount is only meaningful for ONE plant. Summing profiles across a
    # multi-plant scope and dividing the combined submitter count by it would
    # produce a number that is not any plant's participation rate and is not the
    # group's either — plants with no profile would silently vanish from the
    # denominator while their submitters stayed in the numerator.
    headcount: int | None = None
    site_name: str | None = None
    if plantId:
        headcount = (await be.resolve_plant_headcount(db, [plantId])).get(plantId)
        site_name = next((k.siteName for k in rows if k.siteName), None)
        if site_name is None:
            site_name = (
                await db.execute(select(Plant.name).where(Plant.id == plantId))
            ).scalar_one_or_none()

    summary = be.summarise_participation(rows, headcount=headcount)

    # Top contributors by idea count, then by verified saving. Reported, not
    # rewarded: see recognitionNote.
    tally: dict[str, dict[str, float]] = {}
    for k in rows:
        entry = tally.setdefault(k.createdById, {"ideas": 0, "saving": 0.0})
        entry["ideas"] += 1
        if k.verifiedAnnualSaving:
            entry["saving"] += k.verifiedAnnualSaving
    top = sorted(tally.items(), key=lambda kv: (-kv[1]["ideas"], -kv[1]["saving"]))[:10]
    users = await resolve_user_directory(db, [uid for uid, _ in top])

    return KaizenParticipationOut(
        plantId=plantId,
        siteName=site_name,
        periodFrom=raisedFrom,
        periodTo=raisedTo,
        topContributors=[
            KaizenContributorOut(
                user=users.get(uid),
                ideas=int(v["ideas"]),
                verifiedSaving=v["saving"] or None,
            )
            for uid, v in top
        ],
        # Stated on the payload rather than buried in a docstring, because the
        # obvious next request is "wire this to Recognition" and the honest
        # answer is that Recognition does not know Kaizen exists. Its award
        # categories are observation- and leadership-walk-based and no job scores
        # improvement ideas. Building a second points ledger here to fill the gap
        # is exactly what BeKaizen.recognitionEventId exists to prevent.
        recognitionNote=(
            "Recognition is not currently wired to Kaizen — RecognitionEntry "
            "awards observation and leadership-walk categories only. These "
            "figures report contribution; no points are awarded from them."
        ),
        **summary,
    )


#: A hard ceiling on an export. Beyond this the PDF is unreadable and the
#: request holds a worker for long enough to matter. The count that did NOT fit
#: is reported ON THE DOCUMENT rather than silently dropped — a truncated
#: extract read as a complete register is how a programme gets reported as
#: smaller than it is.
EXPORT_MAX_ROWS = 5000
EXPORT_PDF_MAX_ROWS = 800


@router.get("/kaizen/export")
async def export_kaizen(
    format: str = Query(default="csv", pattern="^(csv|pdf)$"),
    plantId: str | None = Query(default=None),
    kstatus: str | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    lane: str | None = Query(default=None),
    mine: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    raisedFrom: datetime | None = Query(default=None),
    raisedTo: datetime | None = Query(default=None),
    savingMin: float | None = Query(default=None, ge=0),
    savingMax: float | None = Query(default=None, ge=0),
    sort: str = Query(default="createdAt"),
    direction: str = Query(default="desc", pattern="^(asc|desc)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The filtered register as CSV or PDF.

    Takes the SAME filter parameters as the list endpoint and runs them through
    the SAME `_kaizen_filters` helper, because the export's whole job is to hand
    somebody the rows they are looking at. A second copy of the filter logic
    drifts silently: the file quietly contains different records from the screen
    it was downloaded from, and nobody checks a CSV against a list.

    PDF goes through the platform's one PDF service (`services/report_pdf.py`,
    the same `_Report` the audit and BRSR reports render through) rather than a
    second exporter.
    """
    scope = await build_query_scope(db, user.id, "KAIZEN.READ")
    statuses = _split_statuses(kstatus)

    stmt = _kaizen_filters(
        scope.apply(select(BeKaizen), BeKaizen, plant_attr="plantId"),
        plantId=plantId,
        statuses=statuses,
        category=category,
        lane=lane,
        mine_user_id=user.id if mine else None,
        q=q,
        raisedFrom=raisedFrom,
        raisedTo=raisedTo,
        savingMin=savingMin,
        savingMax=savingMax,
    )

    total = (
        await db.execute(
            _kaizen_filters(
                scope.apply(select(func.count(BeKaizen.id)), BeKaizen, plant_attr="plantId"),
                plantId=plantId,
                statuses=statuses,
                category=category,
                lane=lane,
                mine_user_id=user.id if mine else None,
                q=q,
                raisedFrom=raisedFrom,
                raisedTo=raisedTo,
                savingMin=savingMin,
                savingMax=savingMax,
            )
        )
    ).scalar_one() or 0

    cap = EXPORT_PDF_MAX_ROWS if format == "pdf" else EXPORT_MAX_ROWS
    column = KAIZEN_SORTS.get(sort, BeKaizen.createdAt)
    order = column.asc().nullslast() if direction == "asc" else column.desc().nullslast()
    rows = (
        await db.execute(stmt.order_by(order, BeKaizen.id.desc()).limit(cap))
    ).scalars().all()
    truncated = max(0, int(total) - len(rows))

    users = await resolve_user_directory(
        db, [k.ownerId for k in rows] + [k.createdById for k in rows]
    )

    def person(user_id: str | None) -> str:
        # Never a raw cuid in an exported file — the platform rule holds outside
        # the UI too. A spreadsheet full of cuids is worse than blanks, because
        # somebody will paste them into an email.
        if not user_id:
            return ""
        ref = users.get(user_id)
        return (ref.name if ref else "") or ""

    filter_summary = _describe_filters(
        plantId=plantId,
        statuses=statuses,
        category=category,
        lane=lane,
        mine=bool(mine),
        q=q,
        raisedFrom=raisedFrom,
        raisedTo=raisedTo,
        savingMin=savingMin,
        savingMax=savingMax,
    )
    stamp = _now().strftime("%Y%m%d-%H%M")

    if format == "pdf":
        from app.services.report_pdf import render_kaizen_register_pdf

        plant_label = "All plants in your scope"
        if plantId:
            plant_label = next(
                (k.siteName for k in rows if k.siteName),
                (await db.execute(select(Plant.name).where(Plant.id == plantId)))
                .scalar_one_or_none()
                or plantId,
            )
        from app.services.display_labels import labels_for_plant, labels_for_plants

        kaizen_labels = (
            await labels_for_plant(db, plantId) if plantId
            else await labels_for_plants(db, sorted({k.plantId for k in rows if k.plantId}))
        )
        pdf = render_kaizen_register_pdf(
            [
                {
                    "kaizenNo": k.kaizenNo,
                    "title": k.title,
                    "siteName": k.siteName,
                    "category": k.category,
                    "status": k.status,
                    "ownerName": person(k.ownerId),
                    "targetDate": k.targetDate.isoformat() if k.targetDate else None,
                    "saving": (
                        k.verifiedAnnualSaving
                        if k.verifiedAnnualSaving is not None
                        else k.estimatedAnnualSaving
                    ),
                    "currency": k.currency,
                    "fastTrack": be.fast_track_eligibility(k)["eligible"],
                }
                for k in rows
            ],
            filter_summary=filter_summary,
            generated_by_name=user.name or user.email,
            plant_label=plant_label,
            truncated=truncated,
            labels=kaizen_labels,
        )
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="kaizen-register-{stamp}.pdf"'
            },
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Kaizen No", "Title", "Plant", "Area", "Line / machine", "Category",
            "Approval lane", "Fast track (earned)", "Status", "Raised by",
            "Raised on", "Owner", "Target date", "Screened on", "Approved on",
            "Implemented on", "Verified on", "Closed on", "Currency",
            "Investment", "Estimated saving / yr", "Verified saving / yr",
            "Saving type", "Replicated at plants", "Origin Kaizen", "OPL created",
            "Problem", "Countermeasure",
        ]
    )

    reps = await _replication_counts(db, [k.id for k in rows])

    def iso(value: datetime | None) -> str:
        return value.isoformat() if value else ""

    for k in rows:
        writer.writerow(
            [
                k.kaizenNo or "",
                k.title,
                k.siteName or "",
                k.areaName or "",
                k.lineOrMachine or "",
                k.category,
                k.lane,
                # Two separate columns on purpose. `lane` is the approval route
                # somebody chose; "fast track (earned)" is whether the record's
                # own investment and time figures support the claim. Collapsing
                # them back into one column is the defect this build removed.
                "Y" if be.fast_track_eligibility(k)["eligible"] else "N",
                k.status,
                person(k.createdById),
                iso(k.createdAt),
                person(k.ownerId),
                iso(k.targetDate),
                iso(k.screenedAt),
                iso(k.approvedAt),
                iso(k.implementedAt),
                iso(k.verifiedAt),
                iso(k.closedAt),
                k.currency,
                # Blank, never 0, for a figure nobody entered. A zero investment
                # is a claim; an empty cell is the absence of one, and a
                # spreadsheet that turns the second into the first is how the
                # fast-track badge went wrong in the first place.
                "" if k.investmentCost is None else k.investmentCost,
                "" if k.estimatedAnnualSaving is None else k.estimatedAnnualSaving,
                "" if k.verifiedAnnualSaving is None else k.verifiedAnnualSaving,
                k.savingType or "",
                reps.get(k.id, 0),
                k.sourceRecordRef if k.originKaizenId else "",
                "Y" if k.generatedOplId else "",
                k.problemStatement,
                k.proposedImprovement,
            ]
        )

    if truncated:
        writer.writerow([])
        writer.writerow(
            [
                f"NOTE: {truncated} further record(s) matched these filters and are "
                f"not included. This export is capped at {cap} rows."
            ]
        )

    # utf-8-sig, not utf-8. Excel on Windows reads a BOM-less UTF-8 CSV as the
    # system codepage, so "Meridian Apparel — Surat" arrives as mojibake — and
    # this platform's plant names are full of em-dashes.
    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="kaizen-register-{stamp}.csv"'
        },
    )


def _describe_filters(
    *,
    plantId: str | None,
    statuses: list[str] | None,
    category: str | None,
    lane: str | None,
    mine: bool,
    q: str | None,
    raisedFrom: datetime | None,
    raisedTo: datetime | None,
    savingMin: float | None,
    savingMax: float | None,
) -> str:
    """A one-line description of what this export contains.

    Printed on the PDF cover. An extract handed to somebody else is otherwise
    indistinguishable from the whole register.
    """
    bits: list[str] = []
    if statuses:
        bits.append("status " + ", ".join(statuses))
    if category:
        bits.append(f"category {category}")
    if lane:
        bits.append(f"lane {lane}")
    if mine:
        bits.append("raised by or owned by me")
    if q:
        bits.append(f'text "{q}"')
    if raisedFrom:
        bits.append(f"raised from {raisedFrom.date()}")
    if raisedTo:
        bits.append(f"raised to {raisedTo.date()}")
    if savingMin is not None:
        bits.append(f"saving >= {savingMin:,.0f}")
    if savingMax is not None:
        bits.append(f"saving <= {savingMax:,.0f}")
    return "; ".join(bits) if bits else "No filters — the whole register in your scope"


@router.get("/kaizen/{kaizen_id}", response_model=KaizenOut)
async def get_kaizen(
    kaizen_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenOut:
    record = await _load_kaizen(db, user, kaizen_id, "KAIZEN.READ")
    return await _kaizen_out(db, record, await _granted(db, user))


@router.patch("/kaizen/{kaizen_id}", response_model=KaizenOut)
async def update_kaizen(
    kaizen_id: str,
    body: KaizenUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenOut:
    record = await _load_kaizen(db, user, kaizen_id, "KAIZEN.UPDATE")
    if record.status in {"CLOSED", "REJECTED"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A {record.status.lower()} Kaizen is final and cannot be edited.",
        )

    # `model_fields_set`, not `exclude_unset` on a dump: a caller must be able to
    # CLEAR a nullable field by sending null, and an unset-vs-null distinction
    # is the only way to tell that from "leave it alone".
    patch = body.model_dump(include=body.model_fields_set)
    if "areaId" in patch:
        if not await be.validate_area(db, plant_id=record.plantId, area_id=patch["areaId"]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That area does not belong to this record's plant.",
            )
        _, area_name = await be.resolve_site_labels(
            db, plant_id=record.plantId, area_id=patch["areaId"]
        )
        record.areaName = area_name
    for field, value in patch.items():
        setattr(record, field, value)
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _kaizen_out(db, record, await _granted(db, user))


@router.post("/kaizen/{kaizen_id}/submit", response_model=KaizenOut)
async def submit_kaizen(
    kaizen_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenOut:
    """DRAFT → the approval workflow. Numbers the record on submit, not on
    create: an abandoned draft must not burn a number out of the sequence."""
    record = await _load_kaizen(db, user, kaizen_id, "KAIZEN.CREATE")
    if record.status != "DRAFT":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a draft Kaizen can be submitted."
        )

    if not record.kaizenNo:
        record.kaizenNo = await be.next_record_number(
            db, model=BeKaizen, column=BeKaizen.kaizenNo, prefix="KZN", plant_id=record.plantId
        )

    from app.services import workflow_engine

    try:
        instance = await workflow_engine.initiate(
            db,
            module=be.WF_MODULE_KAIZEN,
            record_id=record.id,
            record_number=record.kaizenNo,
            record_title=record.title,
            record_data={"type": record.lane, "category": record.category},
            initiator_id=user.id,
            plant_id=record.plantId,
        )
        record.workflowInstanceId = instance.id
    except Exception as e:  # noqa: BLE001
        # No workflow definition configured is a deployment state, not a bug in
        # the caller's request — say so plainly rather than 500ing. The record
        # still advances so the register is usable before the workflow is set up.
        log.warning("Kaizen %s submitted with no workflow: %s", record.id, e)

    record.status = "SUBMITTED"
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _kaizen_out(db, record, await _granted(db, user))


@router.post("/kaizen/{kaizen_id}/transition/{target}", response_model=KaizenOut)
async def transition_kaizen(
    kaizen_id: str,
    target: str,
    body: KaizenTransition,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenOut:
    """Drive the state machine. The permitted set is computed server-side from
    the same table the detail payload's `availableActions` comes from, so the
    button and the gate can never disagree."""
    record = await db.get(BeKaizen, kaizen_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kaizen not found")
    await require_permission_with_context(
        "KAIZEN.READ", user, db, record_id=record.id, plant_id=record.plantId
    )

    target = target.upper()
    granted = await _granted(db, user)
    if not be.kaizen_transition_permitted(record, target, granted):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A Kaizen at {record.status} cannot move to {target}, "
            "or you do not hold the permission that move requires.",
        )

    now = _now()

    # §6 — an approval with no owner is a decision that produces no action, and
    # it is how a register fills with approved ideas nobody is doing. Enforced
    # HERE, not only in the UI: the disabled button is a courtesy, this is the
    # gate. All four live records were at or past APPROVED with a null owner.
    if target == "APPROVED":
        blockers = be.kaizen_approval_blockers(record)
        if blockers:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, " ".join(blockers))

    if target == "VERIFIED":
        # The figure arrives WITH the transition. It is deliberately not on
        # KaizenUpdate: this endpoint requires KAIZEN.VERIFY and refuses the
        # originator, and KAIZEN.UPDATE is held far too widely to be trusted
        # with the number finance is later asked to stand behind.
        if body.verifiedAnnualSaving is not None:
            record.verifiedAnnualSaving = body.verifiedAnnualSaving
        # The one transition with a data precondition rather than just a status
        # one. Verifying savings that were never recorded would let the register
        # report a benefit nobody measured.
        if record.verifiedAnnualSaving is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Record the verified annual saving before marking this Kaizen verified.",
            )
        if record.createdById == user.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Savings must be verified by someone other than the person who "
                "raised the idea.",
            )
        record.verifiedById = user.id
        record.verifiedAt = now
        if body.note:
            record.verificationNote = body.note
    # §2 — one call stamps whichever lifecycle clock this target owns, first
    # time only. Replaces the two hand-written ifs that used to live here and
    # covers SCREENED and APPROVED, which had no clock at all. `verifiedAt` is
    # already set above by the VERIFIED branch, and the stamp is a no-op once a
    # value is present, so the two do not fight.
    be.stamp_kaizen_transition(record, target, at=now)

    record.status = target
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _kaizen_out(db, record, granted)


@router.post("/kaizen/{kaizen_id}/reject", response_model=KaizenOut)
async def reject_kaizen(
    kaizen_id: str,
    body: KaizenReject,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenOut:
    record = await _load_kaizen(db, user, kaizen_id, "KAIZEN.APPROVE")
    if record.status in {"CLOSED", "REJECTED"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "This Kaizen is already final.")
    record.status = "REJECTED"
    record.rejectionReason = body.reason
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _kaizen_out(db, record, await _granted(db, user))


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen — horizontal deployment (§4) and the closed loop into standard work (§7)
# ─────────────────────────────────────────────────────────────────────────────
#: A record must have reached at least this far before it can be spread. There
#: is no point deploying an idea at four other plants before anyone has run it
#: once — yokoten of an unproven idea is just four more places for it to fail.
REPLICABLE_STATUSES: frozenset[str] = frozenset(
    {"IN_IMPLEMENTATION", "IMPLEMENTED", "VERIFIED", "CLOSED"}
)


@router.post(
    "/kaizen/{kaizen_id}/replicate",
    response_model=KaizenOut,
    status_code=status.HTTP_201_CREATED,
)
async def replicate_kaizen(
    kaizen_id: str,
    body: KaizenReplicateIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenOut:
    """Raise this idea again at another plant, and record that it spread.

    Two permissions, not one: KAIZEN.READ on the SOURCE (you must be entitled to
    see the idea you are copying) and KAIZEN.CREATE on the TARGET (you must be
    entitled to raise records at the plant you are copying it to). Checking only
    the source would let somebody seed records into a plant they have no standing
    at; checking only the target would make the search endpoint's scoping
    pointless, since anything you could not read you could still copy.

    The copy starts at DRAFT, unnumbered. It is a proposal at the new plant and
    has to be submitted and screened there like any other — a plant is entitled
    to decide an idea that worked elsewhere does not suit its line.
    """
    source = await _load_kaizen(db, user, kaizen_id, "KAIZEN.READ")

    if source.status not in REPLICABLE_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An idea can be replicated once it is being implemented. "
            f"This one is still at {source.status}.",
        )
    if body.plantId == source.plantId:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That is the plant this idea was raised at. Pick a different one.",
        )

    await require_permission_with_context("KAIZEN.CREATE", user, db, plant_id=body.plantId)

    if not await be.validate_area(db, plant_id=body.plantId, area_id=body.areaId):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That area does not belong to the selected plant.",
        )

    # Checked before the insert so the caller gets a sentence rather than a
    # unique-violation 500. The DB constraint is still the real guard — it is
    # what stops a double-click from double-counting the spread figure.
    existing = (
        await db.execute(
            select(BeKaizenReplication).where(
                BeKaizenReplication.sourceKaizenId == source.id,
                BeKaizenReplication.replicatedAtPlantId == body.plantId,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This idea has already been replicated at that plant.",
        )

    site_name, area_name = await be.resolve_site_labels(
        db, plant_id=body.plantId, area_id=body.areaId
    )

    copy = BeKaizen(
        plantId=body.plantId,
        areaId=body.areaId,
        siteName=site_name,
        areaName=area_name,
        title=body.title or source.title,
        category=source.category,
        # The copy always starts on the STANDARD lane whatever the source ran
        # on. A fast lane is a judgement the receiving plant's supervisor makes
        # about their own line, not one that travels with the idea.
        lane="STANDARD",
        lineOrMachine=source.lineOrMachine,
        processStep=source.processStep,
        problemStatement=source.problemStatement,
        currentState=source.currentState,
        proposedImprovement=source.proposedImprovement,
        expectedBenefit=source.expectedBenefit,
        # Owner is NOT copied by default: the person who ran it at one plant is
        # rarely the person who will run it at another, and quietly assigning
        # them work at a site they do not work at is worse than leaving it
        # unassigned, where the approval gate catches it.
        ownerId=source.ownerId if body.copyOwner else None,
        currency=source.currency,
        # Money is deliberately NOT copied. An investment and a saving are
        # properties of one plant's line, its labour rate and its volumes;
        # carrying them across is how a programme reports the same saving
        # several times. The receiving plant enters its own figures.
        status="DRAFT",
        originKaizenId=source.id,
        sourceModule="KAIZEN",
        sourceRecordId=source.id,
        sourceRecordRef=source.kaizenNo,
        createdById=user.id,
    )
    db.add(copy)
    await db.flush()

    plant_name = (
        await db.execute(select(Plant.name).where(Plant.id == body.plantId))
    ).scalar_one_or_none()

    db.add(
        BeKaizenReplication(
            sourceKaizenId=source.id,
            replicaKaizenId=copy.id,
            replicatedAtPlantId=body.plantId,
            replicatedAtPlantName=plant_name or site_name,
            replicatedById=user.id,
            notes=body.notes,
        )
    )
    await db.commit()
    await db.refresh(copy)
    return await _kaizen_out(db, copy, await _granted(db, user))


@router.post(
    "/kaizen/{kaizen_id}/create-opl",
    response_model=KaizenOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_opl_from_kaizen(
    kaizen_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KaizenOut:
    """§7 — turn a closed Kaizen into a One Point Lesson.

    A Kaizen that closes without changing the standard is an improvement that
    leaves with the person who made it. This is the step that keeps it: the
    countermeasure becomes a lesson the floor has to read and acknowledge.

    The OPL is created at DRAFT with NO audience. That is deliberate and not an
    omission — publishing assigns a dated, escalating acknowledgement to every
    person in the audience, and an audience the platform picked on somebody's
    behalf is a plant-wide notification nobody asked for. The author sets the
    audience and publishes, exactly as for a hand-written OPL, so the
    acknowledgement mechanics on a generated record are the ordinary ones.

    Requires OPL.CREATE at the Kaizen's plant, not merely KAIZEN.UPDATE: this
    creates a record in a different register that other people will be required
    to read.
    """
    record = await _load_kaizen(db, user, kaizen_id, "KAIZEN.READ")

    if record.status != "CLOSED":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A One Point Lesson is written from a closed Kaizen — this one is at "
            f"{record.status}.",
        )
    if record.generatedOplId:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A One Point Lesson has already been created from this Kaizen.",
        )

    await require_permission_with_context(
        "OPL.CREATE", user, db, plant_id=record.plantId
    )

    key_points = [
        p
        for p in (
            _first_sentences(record.proposedImprovement, 2),
            _first_sentences(record.problemStatement, 1),
        )
        if p
    ]

    opl = BeOpl(
        plantId=record.plantId,
        siteName=record.siteName,
        areaId=record.areaId,
        areaName=record.areaName,
        title=record.title,
        # IMPROVEMENT_CASE is the TPM category for exactly this: a lesson drawn
        # from a change that worked. A safety Kaizen keeps its own axis, because
        # an operator scanning the OPL board sorts by what the lesson is about.
        category="SAFETY" if record.category == "SAFETY" else "IMPROVEMENT_CASE",
        lineOrMachine=record.lineOrMachine,
        contentHtml=_opl_content_from_kaizen(record),
        keyPoints=key_points,
        authorId=user.id,
        # NOT NULL on BeOpl, and distinct from authorId: the author is who the
        # lesson is attributed to, createdById is who made the row. They are the
        # same person here, but omitting the second is a 500 at flush time — as
        # this endpoint did until the live diagnostic ran it against the real
        # table. The Pydantic path never exercised it because OplCreate has no
        # createdById field to leave out.
        createdById=user.id,
        # An EXPLICIT empty audience rather than a null one, so the publish
        # screen renders "nobody selected yet" instead of an absent object it
        # has to guess about. Populating it here would be worse: publishing
        # assigns a dated, escalating acknowledgement to every person in it.
        audience={"roleCodes": [], "areaIds": [], "userIds": [], "allPlant": False},
        status="DRAFT",
        sourceModule="KAIZEN",
        sourceRecordId=record.id,
        sourceRecordRef=record.kaizenNo,
    )
    db.add(opl)
    await db.flush()

    record.generatedOplId = opl.id
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _kaizen_out(db, record, await _granted(db, user))


def _first_sentences(text: str | None, count: int) -> str:
    """The first `count` sentences of a block, for an OPL key point.

    A key point is a line an operator reads in seconds. Pasting the whole
    countermeasure paragraph in produces a "one point" lesson with six points,
    which is the one thing the format cannot survive.
    """
    if not text:
        return ""
    parts = [p.strip() for p in text.replace("\n", " ").split(".") if p.strip()]
    return ". ".join(parts[:count])[:200]


def _opl_content_from_kaizen(k: BeKaizen) -> str:
    """The lesson body, as the plain three-part story an OPL tells.

    Escaped, because this is rendered as HTML on the OPL page and the source
    fields are free text a shop-floor user typed. An apostrophe in "operator's
    guard" must not become markup, and neither must anything worse.
    """
    from html import escape

    sections = [
        ("What the problem was", k.problemStatement),
        ("What we changed", k.proposedImprovement),
        ("What it achieved", k.expectedBenefit),
    ]
    parts = [
        f"<h3>{escape(heading)}</h3><p>{escape(body).replace(chr(10), '<br/>')}</p>"
        for heading, body in sections
        if body
    ]
    if k.kaizenNo:
        parts.append(
            f"<p><em>Written from Kaizen {escape(k.kaizenNo)}.</em></p>"
        )
    return "".join(parts)


@router.delete("/kaizen/{kaizen_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kaizen(
    kaizen_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    record = await _load_kaizen(db, user, kaizen_id, "KAIZEN.DELETE")
    soft_delete(record, user.id, await _audit_reason(request))
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# One Point Lesson
# ─────────────────────────────────────────────────────────────────────────────
async def _opl_ack_summaries(
    db: AsyncSession, opl_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Acknowledgement counts for a page of OPLs, in one query.

    Per-row lazy loading here would be N+1 on the register, which is the screen
    most likely to be left open on a wall display.
    """
    if not opl_ids:
        return {}
    rows = (
        await db.execute(
            select(BeOplAcknowledgement).where(BeOplAcknowledgement.oplId.in_(opl_ids))
        )
    ).scalars().all()
    grouped: dict[str, list[BeOplAcknowledgement]] = {}
    for a in rows:
        grouped.setdefault(a.oplId, []).append(a)
    return {oid: be.summarise_acknowledgements(grouped.get(oid, [])) for oid in opl_ids}


def _opl_list_item(o: BeOpl, users: dict, ack: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": o.id,
        "oplNo": o.oplNo,
        "title": o.title,
        "category": o.category,
        "status": o.status,
        "revision": o.revision,
        "plantId": o.plantId,
        "siteName": o.siteName,
        "areaName": o.areaName,
        "lineOrMachine": o.lineOrMachine,
        "author": users.get(o.authorId),
        "effectiveFrom": o.effectiveFrom,
        "reviewDueAt": o.reviewDueAt,
        "publishedAt": o.publishedAt,
        "isReviewOverdue": be.is_opl_review_overdue(o),
        "acknowledgement": ack,
        "createdAt": o.createdAt,
    }


@router.get("/opl/mine")
async def my_opls(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What the caller still has to read.

    Declared BEFORE `/opl/{opl_id}` — this dict's route order is match order,
    and a path parameter would otherwise swallow the literal "mine" and try to
    load an OPL with that id. The same ordering trap already bit
    /api/observations/sla-config.
    """
    rows = (
        await db.execute(
            select(BeOplAcknowledgement, BeOpl)
            .join(BeOpl, BeOpl.id == BeOplAcknowledgement.oplId)
            .where(
                BeOplAcknowledgement.personUserId == user.id,
                BeOplAcknowledgement.status.in_(("ASSIGNED", "READ")),
                BeOpl.status == "PUBLISHED",
            )
            .order_by(BeOplAcknowledgement.dueAt.asc().nullslast())
        )
    ).all()

    items = [
        {
            "acknowledgementId": ack.id,
            "oplId": opl.id,
            "oplNo": opl.oplNo,
            "title": opl.title,
            "category": opl.category,
            "siteName": opl.siteName,
            "revision": opl.revision,
            "assignedAt": ack.assignedAt,
            "dueAt": ack.dueAt,
            "readAt": ack.readAt,
            "status": ack.status,
            "isOverdue": be.is_ack_overdue(ack),
        }
        for ack, opl in rows
    ]
    return {
        "items": items,
        "total": len(items),
        "overdue": sum(1 for i in items if i["isOverdue"]),
    }


@router.get("/opl", response_model=OplListResponse)
async def list_opl(
    plantId: str | None = Query(default=None),
    ostatus: str | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OplListResponse:
    scope = await build_query_scope(db, user.id, "OPL.READ")

    stmt = scope.apply(select(BeOpl), BeOpl, plant_attr="plantId")
    if plantId:
        stmt = stmt.where(BeOpl.plantId == plantId)
    if ostatus:
        stmt = stmt.where(BeOpl.status == ostatus)
    if category:
        stmt = stmt.where(BeOpl.category == category)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(BeOpl.title.ilike(like), BeOpl.oplNo.ilike(like)))

    rows = (
        await db.execute(stmt.order_by(BeOpl.createdAt.desc()).limit(limit).offset(offset))
    ).scalars().all()

    total = (
        await db.execute(
            scope.apply(select(func.count(BeOpl.id)), BeOpl, plant_attr="plantId")
        )
    ).scalar_one() or 0
    counts_rows = (
        await db.execute(
            scope.apply(
                select(BeOpl.status, func.count(BeOpl.id)).group_by(BeOpl.status),
                BeOpl,
                plant_attr="plantId",
            )
        )
    ).all()

    users = await resolve_user_directory(db, [o.authorId for o in rows])
    acks = await _opl_ack_summaries(db, [o.id for o in rows])
    return OplListResponse(
        items=[_opl_list_item(o, users, acks.get(o.id, {})) for o in rows],
        total=total,
        statusCounts={s: c for s, c in counts_rows},
    )


async def _load_opl(db: AsyncSession, user: User, opl_id: str, perm: str) -> BeOpl:
    record = await db.get(BeOpl, opl_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OPL not found")
    await require_permission_with_context(
        perm, user, db, record_id=record.id, plant_id=record.plantId
    )
    return record


async def _opl_out(
    db: AsyncSession, o: BeOpl, granted: set[str], *, viewer_id: str
) -> OplOut:
    users = await resolve_user_directory(db, [o.authorId, o.approverId])
    acks = (
        await db.execute(
            select(BeOplAcknowledgement).where(BeOplAcknowledgement.oplId == o.id)
        )
    ).scalars().all()
    mine = next(
        (a for a in acks if a.personUserId == viewer_id and a.oplRevision == o.revision),
        None,
    )
    base = _opl_list_item(o, users, be.summarise_acknowledgements(acks))
    return OplOut(
        **base,
        contentHtml=o.contentHtml,
        keyPoints=list(o.keyPoints or []),
        audience=o.audience or {},
        acknowledgementDueDays=o.acknowledgementDueDays,
        competencyId=o.competencyId,
        approver=users.get(o.approverId) if o.approverId else None,
        approvedAt=o.approvedAt,
        supersedesOplId=o.supersedesOplId,
        supersededByOplId=o.supersededByOplId,
        rejectionReason=o.rejectionReason,
        retiredAt=o.retiredAt,
        sourceModule=o.sourceModule,
        sourceRecordId=o.sourceRecordId,
        sourceRecordRef=o.sourceRecordRef,
        workflowInstanceId=o.workflowInstanceId,
        updatedAt=o.updatedAt,
        availableActions=be.allowed_opl_actions(o, granted),
        myAcknowledgement=(
            AckOut(
                id=mine.id,
                oplId=mine.oplId,
                oplRevision=mine.oplRevision,
                person=None,
                status=mine.status,
                assignedAt=mine.assignedAt,
                dueAt=mine.dueAt,
                readAt=mine.readAt,
                acknowledgedAt=mine.acknowledgedAt,
                acknowledgementNote=mine.acknowledgementNote,
                isOverdue=be.is_ack_overdue(mine),
            )
            if mine
            else None
        ),
    )


@router.post("/opl", response_model=OplOut, status_code=status.HTTP_201_CREATED)
async def create_opl(
    body: OplCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OplOut:
    await require_permission_with_context("OPL.CREATE", user, db, plant_id=body.plantId)
    if not await be.validate_area(db, plant_id=body.plantId, area_id=body.areaId):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That area does not belong to the selected plant.",
        )
    site_name, area_name = await be.resolve_site_labels(
        db, plant_id=body.plantId, area_id=body.areaId
    )
    record = BeOpl(
        **body.model_dump(exclude={"plantId", "areaId", "audience", "keyPoints"}),
        plantId=body.plantId,
        areaId=body.areaId,
        siteName=site_name,
        areaName=area_name,
        audience=body.audience.model_dump(),
        keyPoints=body.keyPoints,
        status="DRAFT",
        authorId=user.id,
        createdById=user.id,
    )
    db.add(record)
    await db.flush()
    await db.commit()
    await db.refresh(record)
    return await _opl_out(db, record, await _granted(db, user), viewer_id=user.id)


@router.get("/opl/{opl_id}", response_model=OplOut)
async def get_opl(
    opl_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OplOut:
    record = await _load_opl(db, user, opl_id, "OPL.READ")

    # Opening a published lesson you were assigned marks it read. Read is not
    # acknowledged — the acknowledgement is a separate, deliberate act, and
    # conflating them would let scrolling past a page count as training.
    if record.status == "PUBLISHED":
        mine = (
            await db.execute(
                select(BeOplAcknowledgement).where(
                    BeOplAcknowledgement.oplId == record.id,
                    BeOplAcknowledgement.personUserId == user.id,
                    BeOplAcknowledgement.oplRevision == record.revision,
                )
            )
        ).scalar_one_or_none()
        if mine is not None and mine.readAt is None:
            mine.readAt = _now()
            if mine.status == "ASSIGNED":
                mine.status = "READ"
            await db.commit()

    return await _opl_out(db, record, await _granted(db, user), viewer_id=user.id)


@router.patch("/opl/{opl_id}", response_model=OplOut)
async def update_opl(
    opl_id: str,
    body: OplUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OplOut:
    record = await _load_opl(db, user, opl_id, "OPL.UPDATE")
    if record.status not in {"DRAFT", "IN_REVIEW"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Published content is immutable — create a new revision instead. "
            "An acknowledgement is a claim about specific content, and editing "
            "the page under the people who signed it turns a training record "
            "into a fiction.",
        )
    patch = body.model_dump(include=body.model_fields_set)
    if "audience" in patch and patch["audience"] is not None:
        patch["audience"] = body.audience.model_dump() if body.audience else None
    if "areaId" in patch:
        if not await be.validate_area(db, plant_id=record.plantId, area_id=patch["areaId"]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That area does not belong to this record's plant.",
            )
        _, area_name = await be.resolve_site_labels(
            db, plant_id=record.plantId, area_id=patch["areaId"]
        )
        record.areaName = area_name
    for field, value in patch.items():
        setattr(record, field, value)
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _opl_out(db, record, await _granted(db, user), viewer_id=user.id)


@router.post("/opl/{opl_id}/submit", response_model=OplOut)
async def submit_opl(
    opl_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OplOut:
    record = await _load_opl(db, user, opl_id, "OPL.CREATE")
    if record.status != "DRAFT":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a draft OPL can be submitted.")
    if not (record.contentHtml or "").strip() and not (record.keyPoints or []):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "An OPL needs content or key points before it can go for review.",
        )
    if not record.oplNo:
        record.oplNo = await be.next_record_number(
            db, model=BeOpl, column=BeOpl.oplNo, prefix="OPL", plant_id=record.plantId
        )

    from app.services import workflow_engine

    try:
        instance = await workflow_engine.initiate(
            db,
            module=be.WF_MODULE_OPL,
            record_id=record.id,
            record_number=record.oplNo,
            record_title=record.title,
            record_data={"type": record.category},
            initiator_id=user.id,
            plant_id=record.plantId,
        )
        record.workflowInstanceId = instance.id
    except Exception as e:  # noqa: BLE001
        log.warning("OPL %s submitted with no workflow: %s", record.id, e)

    record.status = "IN_REVIEW"
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _opl_out(db, record, await _granted(db, user), viewer_id=user.id)


@router.post("/opl/{opl_id}/publish", response_model=OplOut)
async def publish_opl(
    opl_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OplOut:
    """APPROVED → PUBLISHED, and freeze the audience as acknowledgement rows.

    The audience is resolved exactly once, here. Re-resolving it later would
    make "94% acknowledged" a statement about today's roster rather than about
    the people who were actually asked to read the lesson.
    """
    record = await _load_opl(db, user, opl_id, "OPL.APPROVE")
    if record.status != "APPROVED":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An OPL must be approved before it can be published to the floor.",
        )

    audience = await be.resolve_opl_audience(db, record)
    created = await be.create_acknowledgements(db, record, audience)

    record.status = "PUBLISHED"
    record.publishedAt = _now()
    if record.effectiveFrom is None:
        record.effectiveFrom = record.publishedAt
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    log.info("OPL %s published to %d people (%d new)", record.oplNo, len(audience), created)
    return await _opl_out(db, record, await _granted(db, user), viewer_id=user.id)


@router.post("/opl/{opl_id}/retire", response_model=OplOut)
async def retire_opl(
    opl_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OplOut:
    record = await _load_opl(db, user, opl_id, "OPL.APPROVE")
    if record.status in {"RETIRED", "SUPERSEDED"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "This OPL is already withdrawn.")
    record.status = "RETIRED"
    record.retiredAt = _now()
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _opl_out(db, record, await _granted(db, user), viewer_id=user.id)


@router.get("/opl/{opl_id}/acknowledgements", response_model=AckListResponse)
async def list_acknowledgements(
    opl_id: str,
    astatus: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AckListResponse:
    record = await _load_opl(db, user, opl_id, "OPL.READ")
    stmt = select(BeOplAcknowledgement).where(BeOplAcknowledgement.oplId == record.id)
    if astatus:
        stmt = stmt.where(BeOplAcknowledgement.status == astatus)
    rows = (
        await db.execute(stmt.order_by(BeOplAcknowledgement.assignedAt.asc()))
    ).scalars().all()
    users = await resolve_user_directory(db, [a.personUserId for a in rows])
    return AckListResponse(
        items=[
            AckOut(
                id=a.id,
                oplId=a.oplId,
                oplRevision=a.oplRevision,
                person=users.get(a.personUserId),
                status=a.status,
                assignedAt=a.assignedAt,
                dueAt=a.dueAt,
                readAt=a.readAt,
                acknowledgedAt=a.acknowledgedAt,
                acknowledgementNote=a.acknowledgementNote,
                isOverdue=be.is_ack_overdue(a),
            )
            for a in rows
        ],
        total=len(rows),
        summary=be.summarise_acknowledgements(rows),
    )


@router.post("/opl/{opl_id}/acknowledge", response_model=AckOut)
async def acknowledge_opl(
    opl_id: str,
    body: AckConfirm,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AckOut:
    """The caller confirms they have read this lesson.

    Only ever the caller's own row. An acknowledgement recorded on someone
    else's behalf is not evidence that they read anything, so there is
    deliberately no endpoint for it — a supervisor who needs to excuse someone
    uses a waiver, which is a different and visibly different thing.
    """
    record = await _load_opl(db, user, opl_id, "OPL.READ")
    if record.status != "PUBLISHED":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a published OPL can be acknowledged."
        )

    ack = (
        await db.execute(
            select(BeOplAcknowledgement).where(
                BeOplAcknowledgement.oplId == record.id,
                BeOplAcknowledgement.personUserId == user.id,
                BeOplAcknowledgement.oplRevision == record.revision,
            )
        )
    ).scalar_one_or_none()
    if ack is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This lesson was not assigned to you, so there is nothing to acknowledge.",
        )

    now = _now()
    if ack.acknowledgedAt is None:
        ack.acknowledgedAt = now
    if ack.readAt is None:
        ack.readAt = now
    ack.status = "ACKNOWLEDGED"
    if body.note:
        ack.acknowledgementNote = body.note
    await db.commit()
    await db.refresh(ack)
    return AckOut(
        id=ack.id,
        oplId=ack.oplId,
        oplRevision=ack.oplRevision,
        person=None,
        status=ack.status,
        assignedAt=ack.assignedAt,
        dueAt=ack.dueAt,
        readAt=ack.readAt,
        acknowledgedAt=ack.acknowledgedAt,
        acknowledgementNote=ack.acknowledgementNote,
        isOverdue=False,
    )


@router.delete("/opl/{opl_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_opl(
    opl_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    record = await _load_opl(db, user, opl_id, "OPL.DELETE")
    if record.status == "PUBLISHED":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Retire a published OPL rather than deleting it — people have "
            "acknowledgements against it.",
        )
    soft_delete(record, user.id, await _audit_reason(request))
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Poka Yoke
# ─────────────────────────────────────────────────────────────────────────────
def _bypass_out(b: BePokaYokeBypass, users: dict) -> BypassOut:
    return BypassOut(
        id=b.id,
        deviceId=b.deviceId,
        bypassedBy=users.get(b.bypassedById) if b.bypassedById else None,
        bypassedAt=b.bypassedAt,
        reason=b.reason,
        approvedBy=users.get(b.approvedById) if b.approvedById else None,
        statusAtBypass=b.statusAtBypass,
        restoredAt=b.restoredAt,
        restoredBy=users.get(b.restoredById) if b.restoredById else None,
        restoreNote=b.restoreNote,
        durationHours=be.bypass_duration_hours(b),
        isOpen=b.restoredAt is None,
    )


async def _open_bypass_for(db: AsyncSession, device_id: str) -> BePokaYokeBypass | None:
    """The device's currently-open episode, or None.

    `restoredAt IS NULL` is the ONLY definition of open the service layer
    trusts. The device's `isBypassed` flag is a cache the endpoints dual-write
    for the register's benefit; if the two ever disagree, this is the one that
    is right.
    """
    return (
        await db.execute(
            select(BePokaYokeBypass)
            .where(BePokaYokeBypass.deviceId == device_id)
            .where(BePokaYokeBypass.restoredAt.is_(None))
            .order_by(BePokaYokeBypass.bypassedAt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _open_bypasses_for(
    db: AsyncSession, device_ids: Sequence[str]
) -> dict[str, BePokaYokeBypass]:
    """Open episodes for a page of devices, in one query rather than N.

    A register that issued a query per row to answer "is this bypassed" would be
    slower than the flag it replaced, and the whole point of keeping the flag was
    to avoid exactly that.
    """
    ids = [i for i in device_ids if i]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(BePokaYokeBypass)
            .where(BePokaYokeBypass.deviceId.in_(ids))
            .where(BePokaYokeBypass.restoredAt.is_(None))
        )
    ).scalars().all()
    return {b.deviceId: b for b in rows}


def _poka_yoke_list_item(
    d: BePokaYoke, users: dict, open_bypass: BePokaYokeBypass | None = None
) -> dict[str, Any]:
    return {
        "id": d.id,
        "deviceNo": d.deviceNo,
        "title": d.title,
        "status": d.status,
        "deviceType": d.deviceType,
        "approach": d.approach,
        "reactionMode": d.reactionMode,
        "plantId": d.plantId,
        "siteName": d.siteName,
        "areaName": d.areaName,
        "lineOrMachine": d.lineOrMachine,
        "owner": users.get(d.ownerId) if d.ownerId else None,
        "defectModePrevented": d.defectModePrevented,
        "installedAt": d.installedAt,
        "verificationFrequency": d.verificationFrequency,
        "lastVerifiedAt": d.lastVerifiedAt,
        "lastVerificationResult": d.lastVerificationResult,
        "nextVerificationDueAt": d.nextVerificationDueAt,
        "isVerificationOverdue": be.is_verification_overdue(d),
        "isBypassed": d.isBypassed,
        # Derived, never stored. See services/business_excellence for why the
        # lifecycle column is left alone.
        "displayStatus": be.poka_yoke_display_status(
            d, has_open_bypass=None if open_bypass is None else True
        ),
        "bypassOpenHours": (
            be.bypass_duration_hours(open_bypass) if open_bypass is not None else None
        ),
        "createdAt": d.createdAt,
    }


@router.get("/poka-yoke", response_model=PokaYokeListResponse)
async def list_poka_yoke(
    plantId: str | None = Query(default=None),
    pstatus: str | None = Query(default=None, alias="status"),
    approach: str | None = Query(default=None),
    deviceType: str | None = Query(default=None),
    overdue: bool | None = Query(default=None),
    bypassed: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeListResponse:
    scope = await build_query_scope(db, user.id, "POKAYOKE.READ")

    stmt = scope.apply(select(BePokaYoke), BePokaYoke, plant_attr="plantId")
    if plantId:
        stmt = stmt.where(BePokaYoke.plantId == plantId)
    if pstatus:
        stmt = stmt.where(BePokaYoke.status == pstatus)
    if approach:
        stmt = stmt.where(BePokaYoke.approach == approach)
    if deviceType:
        stmt = stmt.where(BePokaYoke.deviceType == deviceType)
    if bypassed is not None:
        stmt = stmt.where(BePokaYoke.isBypassed.is_(bypassed))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                BePokaYoke.title.ilike(like),
                BePokaYoke.deviceNo.ilike(like),
                BePokaYoke.defectModePrevented.ilike(like),
                BePokaYoke.lineOrMachine.ilike(like),
            )
        )

    rows = (
        await db.execute(
            stmt.order_by(BePokaYoke.createdAt.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    if overdue:
        rows = [d for d in rows if be.is_verification_overdue(d)]

    total = (
        await db.execute(
            scope.apply(select(func.count(BePokaYoke.id)), BePokaYoke, plant_attr="plantId")
        )
    ).scalar_one() or 0
    counts_rows = (
        await db.execute(
            scope.apply(
                select(BePokaYoke.status, func.count(BePokaYoke.id)).group_by(
                    BePokaYoke.status
                ),
                BePokaYoke,
                plant_attr="plantId",
            )
        )
    ).all()

    # Overdue across the WHOLE scoped set, not just this page — the banner on
    # the register claims a plant-wide number and must not silently mean
    # "overdue among the hundred rows you happen to be looking at".
    all_live = (
        await db.execute(
            scope.apply(
                select(BePokaYoke).where(
                    BePokaYoke.status.in_(("VERIFIED", "ACTIVE", "DEGRADED"))
                ),
                BePokaYoke,
                plant_attr="plantId",
            )
        )
    ).scalars().all()
    overdue_count = sum(1 for d in all_live if be.is_verification_overdue(d))

    # Scope-wide too, and for the same reason. The register's banner claims a
    # plant-wide number; the previous version counted `isBypassed` across the
    # rows it had already fetched, so a plant with more devices than the page
    # size under-reported how much of its mistake-proofing was switched off —
    # silently, and in the direction that looks better.
    #
    # Counted off the log table rather than the flag: `restoredAt IS NULL` is
    # the definition of open, and a tile that disagreed with the device's own
    # bypass history would be the second source of truth this whole table
    # exists to remove. Excludes soft-deleted devices, which the scope's own
    # filter does not cover here because we are querying the child.
    active_bypasses = (
        await db.execute(
            scope.apply(
                select(func.count(BePokaYokeBypass.id))
                .join(BePokaYoke, BePokaYoke.id == BePokaYokeBypass.deviceId)
                .where(BePokaYokeBypass.restoredAt.is_(None))
                .where(BePokaYoke.isDeleted.is_(False)),
                BePokaYoke,
                plant_attr="plantId",
            )
        )
    ).scalar_one() or 0

    open_map = await _open_bypasses_for(db, [d.id for d in rows])
    users = await resolve_user_directory(db, [d.ownerId for d in rows])
    return PokaYokeListResponse(
        items=[_poka_yoke_list_item(d, users, open_map.get(d.id)) for d in rows],
        total=total,
        statusCounts={s: c for s, c in counts_rows},
        overdueCount=overdue_count,
        activeBypasses=active_bypasses,
    )


async def _load_device(db: AsyncSession, user: User, device_id: str, perm: str) -> BePokaYoke:
    record = await db.get(BePokaYoke, device_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Poka Yoke device not found")
    await require_permission_with_context(
        perm, user, db, record_id=record.id, plant_id=record.plantId
    )
    return record


async def _device_out(db: AsyncSession, d: BePokaYoke, granted: set[str]) -> PokaYokeOut:
    checks = (
        await db.execute(
            select(BePokaYokeVerification)
            .where(BePokaYokeVerification.deviceId == d.id)
            .order_by(BePokaYokeVerification.verifiedAt.desc())
            .limit(50)
        )
    ).scalars().all()
    # The full bypass history, newest first. Not capped alongside the checks:
    # a device with fifty bypass episodes is precisely the device whose history
    # nobody should have to page through to see.
    bypasses = (
        await db.execute(
            select(BePokaYokeBypass)
            .where(BePokaYokeBypass.deviceId == d.id)
            .order_by(BePokaYokeBypass.bypassedAt.desc())
            .limit(100)
        )
    ).scalars().all()
    open_bypass = next((b for b in bypasses if b.restoredAt is None), None)

    users = await resolve_user_directory(
        db,
        [d.ownerId, d.bypassedById, d.bypassApprovedById]
        + [c.verifiedById for c in checks]
        + [b.bypassedById for b in bypasses]
        + [b.approvedById for b in bypasses]
        + [b.restoredById for b in bypasses],
    )
    base = _poka_yoke_list_item(d, users, open_bypass)
    return PokaYokeOut(
        **base,
        bypasses=[_bypass_out(b, users) for b in bypasses],
        activeBypass=_bypass_out(open_bypass, users) if open_bypass else None,
        description=d.description,
        processStep=d.processStep,
        beforeCondition=d.beforeCondition,
        afterCondition=d.afterCondition,
        currency=d.currency,
        cost=d.cost,
        bypassReason=d.bypassReason,
        bypassedAt=d.bypassedAt,
        bypassedBy=users.get(d.bypassedById) if d.bypassedById else None,
        bypassApprovedBy=users.get(d.bypassApprovedById) if d.bypassApprovedById else None,
        rejectionReason=d.rejectionReason,
        retiredAt=d.retiredAt,
        sourceKaizenId=d.sourceKaizenId,
        sourceRcaId=d.sourceRcaId,
        workflowInstanceId=d.workflowInstanceId,
        updatedAt=d.updatedAt,
        availableActions=be.allowed_poka_yoke_actions(d, granted),
        verifications=[
            VerificationOut(
                id=c.id,
                deviceId=c.deviceId,
                verifiedBy=users.get(c.verifiedById),
                verifiedAt=c.verifiedAt,
                result=c.result,
                note=c.note,
                dueAt=c.dueAt,
                capaId=c.capaId,
            )
            for c in checks
        ],
    )


@router.post("/poka-yoke", response_model=PokaYokeOut, status_code=status.HTTP_201_CREATED)
async def create_poka_yoke(
    body: PokaYokeCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    await require_permission_with_context("POKAYOKE.CREATE", user, db, plant_id=body.plantId)
    if not await be.validate_area(db, plant_id=body.plantId, area_id=body.areaId):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That area does not belong to the selected plant.",
        )
    site_name, area_name = await be.resolve_site_labels(
        db, plant_id=body.plantId, area_id=body.areaId
    )
    record = BePokaYoke(
        **body.model_dump(exclude={"plantId", "areaId"}),
        plantId=body.plantId,
        areaId=body.areaId,
        siteName=site_name,
        areaName=area_name,
        status="PROPOSED",
        createdById=user.id,
    )
    db.add(record)
    await db.flush()
    await db.commit()
    await db.refresh(record)
    return await _device_out(db, record, await _granted(db, user))


@router.get("/poka-yoke/{device_id}", response_model=PokaYokeOut)
async def get_poka_yoke(
    device_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    record = await _load_device(db, user, device_id, "POKAYOKE.READ")
    return await _device_out(db, record, await _granted(db, user))


@router.patch("/poka-yoke/{device_id}", response_model=PokaYokeOut)
async def update_poka_yoke(
    device_id: str,
    body: PokaYokeUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    record = await _load_device(db, user, device_id, "POKAYOKE.UPDATE")
    if record.status in {"RETIRED", "REJECTED"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A withdrawn device cannot be edited."
        )
    patch = body.model_dump(include=body.model_fields_set)
    if "areaId" in patch:
        if not await be.validate_area(db, plant_id=record.plantId, area_id=patch["areaId"]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That area does not belong to this record's plant.",
            )
        _, area_name = await be.resolve_site_labels(
            db, plant_id=record.plantId, area_id=patch["areaId"]
        )
        record.areaName = area_name
    for field, value in patch.items():
        setattr(record, field, value)
    # Changing the cadence has to move the due date, or the register keeps
    # chasing the old schedule.
    if "verificationFrequency" in patch and record.lastVerifiedAt is not None:
        record.nextVerificationDueAt = be.next_verification_due(
            record.verificationFrequency, from_dt=record.lastVerifiedAt
        )
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _device_out(db, record, await _granted(db, user))


@router.post("/poka-yoke/{device_id}/submit", response_model=PokaYokeOut)
async def submit_poka_yoke(
    device_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    record = await _load_device(db, user, device_id, "POKAYOKE.CREATE")
    if record.status != "PROPOSED":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a proposed device can be submitted."
        )
    if not record.deviceNo:
        record.deviceNo = await be.next_record_number(
            db, model=BePokaYoke, column=BePokaYoke.deviceNo, prefix="PY", plant_id=record.plantId
        )

    from app.services import workflow_engine

    try:
        instance = await workflow_engine.initiate(
            db,
            module=be.WF_MODULE_POKA_YOKE,
            record_id=record.id,
            record_number=record.deviceNo,
            record_title=record.title,
            record_data={"type": record.approach},
            initiator_id=user.id,
            plant_id=record.plantId,
        )
        record.workflowInstanceId = instance.id
    except Exception as e:  # noqa: BLE001
        log.warning("Poka Yoke %s submitted with no workflow: %s", record.id, e)

    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _device_out(db, record, await _granted(db, user))


@router.post("/poka-yoke/{device_id}/install", response_model=PokaYokeOut)
async def install_poka_yoke(
    device_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    record = await _load_device(db, user, device_id, "POKAYOKE.UPDATE")
    if record.status != "APPROVED":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The device must be approved before installation."
        )
    now = _now()
    record.status = "INSTALLED"
    if record.installedAt is None:
        record.installedAt = now
    # An installed device owes its first verification immediately — it has never
    # been proven to work in place.
    record.nextVerificationDueAt = now
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _device_out(db, record, await _granted(db, user))


@router.post("/poka-yoke/{device_id}/verify", response_model=PokaYokeOut)
async def verify_poka_yoke(
    device_id: str,
    body: VerificationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    """Record one periodic check. A FAIL raises a CAPA on the universal engine.

    The check is append-only. A device that failed and was fixed gets a second
    PASS row, never an edited FAIL — the history of what was wrong when is the
    reason the register exists.
    """
    record = await _load_device(db, user, device_id, "POKAYOKE.VERIFY")
    if record.status not in {"INSTALLED", "VERIFIED", "ACTIVE", "DEGRADED"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A device at {record.status} has no verification obligation.",
        )

    when = body.verifiedAt or _now()
    check = BePokaYokeVerification(
        deviceId=record.id,
        plantId=record.plantId,
        verifiedById=user.id,
        verifiedAt=when,
        result=body.result,
        note=body.note,
        dueAt=record.nextVerificationDueAt,
    )
    db.add(check)

    record.lastVerifiedAt = when
    record.lastVerificationResult = body.result
    record.nextVerificationDueAt = be.next_verification_due(
        record.verificationFrequency, from_dt=when
    )
    # PASS promotes an installed device to ACTIVE and recovers a DEGRADED one.
    # FAIL degrades it — the device is still fitted, but it is not currently
    # protecting anything, and the register must say so.
    record.status = "ACTIVE" if body.result == "PASS" else "DEGRADED"
    record.updatedById = user.id
    await db.flush()

    if body.result == "FAIL":
        # Best-effort + savepoint: a missing CAPA source type (seed not yet run)
        # must not lose the verification record itself, which is the evidence.
        try:
            async with db.begin_nested():
                from app.services.capa_spawn import spawn_capa

                capa = await spawn_capa(
                    db,
                    source_code=be.CAPA_SOURCE_POKA_YOKE_FAILURE,
                    plant_id=record.plantId,
                    title=f"Poka Yoke failed verification — {record.title}",
                    problem=(
                        f"Device {record.deviceNo or record.id} "
                        f"({record.approach.lower()}, {record.reactionMode.lower()}) "
                        f"failed its scheduled check. Defect mode it is supposed to "
                        f"prevent: {record.defectModePrevented}. "
                        f"Verifier's note: {body.note or 'none recorded'}."
                    ),
                    ref_id=record.id,
                    ref_summary=record.title,
                    metadata={
                        "deviceNo": record.deviceNo,
                        "lineOrMachine": record.lineOrMachine,
                        "verificationId": check.id,
                    },
                    severity="HIGH",
                    priority="HIGH",
                    detected_method="POKA_YOKE_VERIFICATION",
                    owner_id=record.ownerId,
                    actor_id=user.id,
                    due_days=14,
                )
                if capa is not None:
                    await db.flush()
                    check.capaId = capa.id
        except Exception:  # noqa: BLE001
            log.exception(
                "Poka Yoke %s: CAPA spawn failed for failed verification", record.id
            )

    await db.commit()
    await db.refresh(record)
    return await _device_out(db, record, await _granted(db, user))


# ⚠ ROUTE ORDER. /poka-yoke/bypasses/... is declared BEFORE /poka-yoke/{id}/...
# even though the depths differ, matching the convention the Kaizen half of this
# router documents at the top: a literal segment that could ever be read as an
# id goes first, so a later edit that shortens the path cannot silently turn
# "bypasses" into a device id.
@router.post("/poka-yoke/bypasses/{bypass_id}/restore", response_model=PokaYokeOut)
async def restore_poka_yoke_bypass(
    bypass_id: str,
    body: BypassRestore | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    """End ONE bypass episode, addressed by its own id.

    Distinct from /poka-yoke/{id}/restore, which ends whatever episode that
    device currently has open — this one names the episode, so a screen showing
    a history cannot close the wrong row if two requests race.
    """
    bypass = await db.get(BePokaYokeBypass, bypass_id)
    if bypass is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bypass record not found")
    record = await _load_device(db, user, bypass.deviceId, "POKAYOKE.UPDATE")
    return await _close_bypass(
        db, record, bypass, user, note=(body.note if body else None)
    )


async def _close_bypass(
    db: AsyncSession,
    record: BePokaYoke,
    bypass: BePokaYokeBypass,
    user: User,
    *,
    note: str | None,
) -> PokaYokeOut:
    """Stamp an episode closed and put the device back into service.

    The episode is CLOSED, never cleared. The previous implementation ended a
    bypass by NULLing `bypassedAt`, `bypassedById` and `bypassApprovedById` on
    the device row, which erased who switched the device off and when at the
    exact moment the record became useful. Nothing here writes a null over a
    fact.
    """
    if bypass.restoredAt is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That bypass has already been ended."
        )

    now = _now()
    bypass.restoredAt = now
    bypass.restoredById = user.id
    bypass.restoreNote = note

    # The flat columns are a cache of the OPEN episode, so only the flag is
    # cleared. `bypassedAt` / `bypassedById` / `bypassReason` stay as provenance
    # of the most recent episode — a screen still reading the old fields sees
    # the last bypass rather than a row of blanks.
    record.isBypassed = False

    # A restored device is unproven until somebody checks it, so it owes a
    # verification now rather than at the end of its normal cycle. DEGRADED is
    # honest: fitted, back in circuit, and nobody has confirmed it works.
    record.status = "DEGRADED"
    record.nextVerificationDueAt = now
    record.updatedById = user.id

    await db.commit()
    await db.refresh(record)
    return await _device_out(db, record, await _granted(db, user))


@router.post("/poka-yoke/{device_id}/bypass", response_model=PokaYokeOut)
async def bypass_poka_yoke(
    device_id: str,
    body: BypassRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    """Log that a device has been overridden.

    This is the record an auditor asks for, so it demands a reason and captures
    who authorised it. It opens an append-only episode in BePokaYokeBypass; the
    flat columns on the device row are written too, but only as a cache of the
    currently-open episode for the register's benefit.

    Bypassing does not change the lifecycle `status`: the device is still
    fitted, and a register that showed it as removed would understate how much
    mistake-proofing the line has lost. What changes is the DISPLAYED status,
    derived in the service layer — the badge says Bypassed, and the record still
    knows the device was Active underneath.
    """
    record = await _load_device(db, user, device_id, "POKAYOKE.UPDATE")
    if record.status in {"RETIRED", "REJECTED"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A withdrawn device has nothing to bypass."
        )

    # Checked against the LOG, not the flag: if the two ever disagree the log is
    # right, and a check against a stale flag would open a second episode that
    # the partial unique index then rejects with a 500 instead of a sentence.
    existing = await _open_bypass_for(db, record.id)
    if existing is not None or record.isBypassed:
        raise HTTPException(status.HTTP_409_CONFLICT, "This device is already bypassed.")

    now = _now()
    episode = BePokaYokeBypass(
        deviceId=record.id,
        plantId=record.plantId,
        bypassedById=user.id,
        bypassedAt=now,
        reason=body.reason,
        approvedById=body.approvedById,
        statusAtBypass=record.status,
    )
    db.add(episode)

    record.isBypassed = True
    record.bypassReason = body.reason
    record.bypassedAt = now
    record.bypassedById = user.id
    record.bypassApprovedById = body.approvedById
    record.updatedById = user.id

    await db.commit()
    await db.refresh(record)
    return await _device_out(db, record, await _granted(db, user))


@router.post("/poka-yoke/{device_id}/restore", response_model=PokaYokeOut)
async def restore_poka_yoke(
    device_id: str,
    body: BypassRestore | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    """End whatever bypass this device currently has open.

    Kept at its original path because the web action panel and the mobile app
    both call it; the by-id door above is the addressable one.
    """
    record = await _load_device(db, user, device_id, "POKAYOKE.UPDATE")
    episode = await _open_bypass_for(db, record.id)

    if episode is None:
        if record.isBypassed:
            # Flag set, no episode — a device bypassed before the log table
            # existed, or a backfill that never ran. Heal it rather than
            # refusing: leaving the register asserting a bypass nobody can end
            # is worse than a repaired row, and the repair says so in its reason.
            log.warning(
                "Poka Yoke %s had isBypassed set with no open episode; healing.",
                record.id,
            )
            episode = BePokaYokeBypass(
                deviceId=record.id,
                plantId=record.plantId,
                bypassedById=record.bypassedById or record.createdById,
                bypassedAt=record.bypassedAt or record.updatedAt or _now(),
                reason=(
                    record.bypassReason
                    or "Reason not recorded — predates the bypass log."
                ),
                approvedById=record.bypassApprovedById,
                statusAtBypass=record.status,
            )
            db.add(episode)
            await db.flush()
        else:
            raise HTTPException(status.HTTP_409_CONFLICT, "This device is not bypassed.")

    return await _close_bypass(
        db, record, episode, user, note=(body.note if body else None)
    )


@router.post("/poka-yoke/{device_id}/retire", response_model=PokaYokeOut)
async def retire_poka_yoke(
    device_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PokaYokeOut:
    record = await _load_device(db, user, device_id, "POKAYOKE.APPROVE")
    if record.status == "RETIRED":
        raise HTTPException(status.HTTP_409_CONFLICT, "This device is already retired.")
    record.status = "RETIRED"
    record.retiredAt = _now()
    record.nextVerificationDueAt = None
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _device_out(db, record, await _granted(db, user))


@router.delete("/poka-yoke/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_poka_yoke(
    device_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    record = await _load_device(db, user, device_id, "POKAYOKE.DELETE")
    soft_delete(record, user.id, await _audit_reason(request))
    await db.commit()
