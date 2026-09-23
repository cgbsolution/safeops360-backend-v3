"""Business Excellence Phase 2 router. Mounted at /api/be, alongside Phase 1.

  Meta
    GET    /api/be/meta/p2                                — Phase 2 vocabularies

  Suggestion Scheme (§3)
    GET    /api/be/suggestions                            — register
    POST   /api/be/suggestions                            — create (DRAFT)
    GET    /api/be/suggestions/{id}                       — detail + availableActions
    PATCH  /api/be/suggestions/{id}                       — edit
    POST   /api/be/suggestions/{id}/submit                — DRAFT → workflow
    POST   /api/be/suggestions/{id}/screen                — triage (stage one)
    POST   /api/be/suggestions/{id}/decide                — accept/reject/defer (stage two)
    POST   /api/be/suggestions/{id}/transition/{to}       — the state machine
    POST   /api/be/suggestions/{id}/incentive             — reward tracking
    DELETE /api/be/suggestions/{id}                       — soft-delete

  Quality Circle (§6)
    GET    /api/be/qcc/teams                              — circle register
    POST   /api/be/qcc/teams                              — form a circle
    GET    /api/be/qcc/teams/{id}                         — detail + roster
    PATCH  /api/be/qcc/teams/{id}                         — edit
    POST   /api/be/qcc/teams/{id}/members                 — add to the roster
    DELETE /api/be/qcc/teams/{id}/members/{userId}        — stand down (leftAt, never deleted)
    GET    /api/be/qcc/teams/{id}/projects                — this circle's history
    DELETE /api/be/qcc/teams/{id}                         — soft-delete
    GET    /api/be/qcc/projects                           — project register
    POST   /api/be/qcc/projects                           — create (DRAFT)
    GET    /api/be/qcc/projects/{id}                      — board + gates + benefit
    PATCH  /api/be/qcc/projects/{id}                      — edit the charter
    POST   /api/be/qcc/projects/{id}/charter              — DRAFT → CHARTERED, builds gates
    POST   /api/be/qcc/projects/{id}/rca                  — open the SHARED RCA
    PATCH  /api/be/qcc/projects/{id}/stages/{stage}       — work a gate
    POST   /api/be/qcc/projects/{id}/stages/{stage}/signoff
    POST   /api/be/qcc/projects/{id}/evaluate             — rubric score
    POST   /api/be/qcc/projects/{id}/transition/{to}
    DELETE /api/be/qcc/projects/{id}

  Structured Improvement Project (§7)
    GET    /api/be/sip                                    — portfolio
    POST   /api/be/sip                                    — create (DRAFT)
    GET    /api/be/sip/{id}                               — detail + milestones + metric
    PATCH  /api/be/sip/{id}                               — edit
    POST   /api/be/sip/{id}/milestones                    — add
    PATCH  /api/be/sip/{id}/milestones/{mid}              — update (never plannedDate)
    DELETE /api/be/sip/{id}/milestones/{mid}              — remove (pre-approval only)
    POST   /api/be/sip/{id}/transition/{to}
    DELETE /api/be/sip/{id}

  Benefit realisation (§2/§6/§7/§8)
    GET    /api/be/benefits                               — all lines, filterable
    POST   /api/be/benefits                               — open a line
    GET    /api/be/benefits/{id}                          — detail + readings
    PATCH  /api/be/benefits/{id}                          — edit the projection
    POST   /api/be/benefits/{id}/claim                    — submit realised for validation
    POST   /api/be/benefits/{id}/validate                 — independent sign-off
    POST   /api/be/benefits/{id}/readings                 — append a measurement

  Cross-workflow (§8)
    GET    /api/be/dashboards/summary                     — all six workflows

Plant scoping is fail-closed on every list via access_scope.build_query_scope and
re-checked per record on every read and write, exactly as in Phase 1. Transitions
are gated server-side and the permitted set — plus the reason any of them would
still fail — is returned on the payload, so the UI never has to re-derive the
rules.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.core.soft_delete import soft_delete
from app.models.business_excellence import BeKaizen, BeOpl, BePokaYoke
from app.models.business_excellence_p2 import (
    BE_CATEGORIES,
    BENEFIT_SOURCE_TYPES,
    BENEFIT_STATUSES,
    BENEFIT_TYPES,
    BENEFIT_VALUE_KINDS,
    INCENTIVE_STATUSES,
    MILESTONE_STATUSES,
    QCC_MEMBER_ROLES,
    QCC_METHODOLOGIES,
    QCC_PROJECT_STATUSES,
    QCC_STAGE_STATUSES,
    QCC_STAGES_BY_METHODOLOGY,
    QCC_TEAM_STATUSES,
    RAG_STATUSES,
    SCREENING_OUTCOMES,
    SIP_STATUSES,
    SUGGESTION_DECISIONS,
    SUGGESTION_STATUSES,
    VALIDATION_WINDOW_MONTHS,
    BeBenefit,
    BeBenefitReading,
    BeQccProject,
    BeQccProjectStage,
    BeQccTeam,
    BeQccTeamMember,
    BeSip,
    BeSipMilestone,
    BeSuggestion,
)
from app.models.user import User
from app.schemas.business_excellence_p2 import (
    BeDashboardOut,
    BenefitClaim,
    BenefitCreate,
    BenefitListResponse,
    BenefitOut,
    BenefitReadingCreate,
    BenefitUpdate,
    BenefitValidate,
    P2MetaOut,
    QccEvaluation,
    QccMemberIn,
    QccProjectCreate,
    QccProjectListResponse,
    QccProjectOut,
    QccProjectTransition,
    QccProjectUpdate,
    QccRcaRequest,
    QccStageSignOff,
    QccStageUpdate,
    QccTeamCreate,
    QccTeamListResponse,
    QccTeamOut,
    QccTeamUpdate,
    SipCreate,
    SipListResponse,
    SipMilestoneIn,
    SipMilestoneUpdate,
    SipOut,
    SipTransition,
    SipUpdate,
    SuggestionCreate,
    SuggestionDecide,
    SuggestionIncentive,
    SuggestionListResponse,
    SuggestionOut,
    SuggestionReject,
    SuggestionScreen,
    SuggestionTransition,
    SuggestionUpdate,
    WorkflowSummary,
)
from app.services import business_excellence as be
from app.services import business_excellence_p2 as be2
from app.services.access_scope import build_query_scope
from app.services.permissions import get_permissions
from app.services.user_directory import resolve_user_directory

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/be", tags=["business-excellence"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _granted(db: AsyncSession, user: User) -> set[str]:
    perms = await get_permissions(db, user.id)
    return {code for code, ok in perms.items() if ok}


async def _audit_reason(request: Request) -> str:
    return (
        request.headers.get("x-audit-reason")
        or "Deleted from the Business Excellence register"
    )


def _days_between(a: datetime | None, b: datetime | None) -> int | None:
    """Whole days from a to b, both coerced to UTC-aware first."""
    a2, b2 = be._aware(a), be._aware(b)
    if a2 is None or b2 is None:
        return None
    return (b2 - a2).days


# ─────────────────────────────────────────────────────────────────────────────
# Meta
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/meta/p2", response_model=P2MetaOut)
async def meta_p2(user: User = Depends(get_current_user)) -> P2MetaOut:
    """Served from the same tuples Pydantic validates against, so a dropdown can
    never offer a value the API will reject."""
    return P2MetaOut(
        categories=list(BE_CATEGORIES),
        suggestionStatuses=list(SUGGESTION_STATUSES),
        screeningOutcomes=list(SCREENING_OUTCOMES),
        suggestionDecisions=list(SUGGESTION_DECISIONS),
        incentiveStatuses=list(INCENTIVE_STATUSES),
        qccTeamStatuses=list(QCC_TEAM_STATUSES),
        qccMemberRoles=list(QCC_MEMBER_ROLES),
        qccMethodologies=list(QCC_METHODOLOGIES),
        qccStagesByMethodology={k: list(v) for k, v in QCC_STAGES_BY_METHODOLOGY.items()},
        qccProjectStatuses=list(QCC_PROJECT_STATUSES),
        qccStageStatuses=list(QCC_STAGE_STATUSES),
        sipStatuses=list(SIP_STATUSES),
        milestoneStatuses=list(MILESTONE_STATUSES),
        ragStatuses=list(RAG_STATUSES),
        benefitTypes=list(BENEFIT_TYPES),
        benefitValueKinds=list(BENEFIT_VALUE_KINDS),
        benefitStatuses=list(BENEFIT_STATUSES),
        benefitSourceTypes=list(BENEFIT_SOURCE_TYPES),
        validationWindowMonths=list(VALIDATION_WINDOW_MONTHS),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Suggestion Scheme
# ═════════════════════════════════════════════════════════════════════════════
def _suggestion_item(s: BeSuggestion, users: dict, viewer_id: str, granted: set[str]) -> dict[str, Any]:
    # The anonymity gate. `submitter_visible_to` is the ONLY place that decides;
    # every payload shape funnels through this one helper so a new endpoint
    # cannot accidentally leak the name by building its own dict.
    show = be2.submitter_visible_to(s, viewer_id, granted)
    return {
        "id": s.id,
        "suggestionNo": s.suggestionNo,
        "title": s.title,
        "category": s.category,
        "status": s.status,
        "plantId": s.plantId,
        "siteName": s.siteName,
        "areaName": s.areaName,
        "isAnonymous": s.isAnonymous,
        "submittedBy": users.get(s.createdById) if show else None,
        "owner": users.get(s.ownerId) if s.ownerId else None,
        "screeningOutcome": s.screeningOutcome,
        "decision": s.decision,
        "incentiveStatus": s.incentiveStatus,
        "targetDate": s.targetDate,
        "implementedAt": s.implementedAt,
        "isOverdue": be2.is_suggestion_overdue(s),
        "deferralDue": be2.is_deferral_due(s),
        "createdAt": s.createdAt,
    }


async def _suggestion_out(
    db: AsyncSession, s: BeSuggestion, user: User, granted: set[str]
) -> SuggestionOut:
    users = await resolve_user_directory(
        db, [s.createdById, s.ownerId, s.screenedById, s.decidedById]
    )
    base = _suggestion_item(s, users, user.id, granted)
    return SuggestionOut(
        **base,
        description=s.description,
        expectedBenefit=s.expectedBenefit,
        screeningNote=s.screeningNote,
        screenedBy=users.get(s.screenedById) if s.screenedById else None,
        screenedAt=s.screenedAt,
        duplicateOfSuggestionId=s.duplicateOfSuggestionId,
        decisionRationale=s.decisionRationale,
        decidedBy=users.get(s.decidedById) if s.decidedById else None,
        decidedAt=s.decidedAt,
        deferredUntil=s.deferredUntil,
        implementationNote=s.implementationNote,
        incentivePoints=s.incentivePoints,
        incentiveAmount=s.incentiveAmount,
        currency=s.currency,
        incentiveNote=s.incentiveNote,
        convertedToKaizenId=s.convertedToKaizenId,
        rejectionReason=s.rejectionReason,
        closedAt=s.closedAt,
        workflowInstanceId=s.workflowInstanceId,
        updatedAt=s.updatedAt,
        availableActions=be2.allowed_suggestion_actions(s, granted),
    )


async def _load_suggestion(
    db: AsyncSession, user: User, sid: str, perm: str
) -> BeSuggestion:
    record = await db.get(BeSuggestion, sid)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Suggestion not found")
    await require_permission_with_context(
        perm, user, db, record_id=record.id, plant_id=record.plantId
    )
    return record


@router.get("/suggestions", response_model=SuggestionListResponse)
async def list_suggestions(
    plantId: str | None = Query(default=None),
    sstatus: str | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    decision: str | None = Query(default=None),
    mine: bool | None = Query(default=None),
    overdue: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionListResponse:
    scope = await build_query_scope(db, user.id, "SUGGESTION.READ")

    stmt = select(BeSuggestion)
    stmt = scope.apply(stmt, BeSuggestion)
    if plantId:
        stmt = stmt.where(BeSuggestion.plantId == plantId)
    if sstatus:
        stmt = stmt.where(BeSuggestion.status == sstatus)
    if category:
        stmt = stmt.where(BeSuggestion.category == category)
    if outcome:
        stmt = stmt.where(BeSuggestion.screeningOutcome == outcome)
    if decision:
        stmt = stmt.where(BeSuggestion.decision == decision)
    if mine:
        stmt = stmt.where(
            or_(BeSuggestion.createdById == user.id, BeSuggestion.ownerId == user.id)
        )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                BeSuggestion.title.ilike(like),
                BeSuggestion.description.ilike(like),
                BeSuggestion.suggestionNo.ilike(like),
            )
        )

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0
    counts_rows = (
        await db.execute(
            select(BeSuggestion.status, func.count())
            .where(BeSuggestion.id.in_(select(stmt.subquery().c.id)))
            .group_by(BeSuggestion.status)
        )
    ).all()

    # House rule: registers sort createdAt DESC.
    rows = list(
        (
            await db.execute(
                stmt.order_by(BeSuggestion.createdAt.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    if overdue:
        rows = [r for r in rows if be2.is_suggestion_overdue(r)]

    granted = await _granted(db, user)
    users = await resolve_user_directory(
        db, [r.createdById for r in rows] + [r.ownerId for r in rows]
    )
    return SuggestionListResponse(
        items=[_suggestion_item(r, users, user.id, granted) for r in rows],
        total=total,
        statusCounts={s: c for s, c in counts_rows},
    )


@router.post(
    "/suggestions", response_model=SuggestionOut, status_code=status.HTTP_201_CREATED
)
async def create_suggestion(
    body: SuggestionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionOut:
    await require_permission_with_context(
        "SUGGESTION.CREATE", user, db, plant_id=body.plantId
    )
    if not await be.validate_area(db, plant_id=body.plantId, area_id=body.areaId):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That area does not belong to the selected plant.",
        )
    site_name, area_name = await be.resolve_site_labels(
        db, plant_id=body.plantId, area_id=body.areaId
    )

    record = BeSuggestion(
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
    # is re-read — see _base.py. Without this the response carries a null the
    # client renders as "never updated".
    await db.refresh(record)
    return await _suggestion_out(db, record, user, await _granted(db, user))


@router.get("/suggestions/{sid}", response_model=SuggestionOut)
async def get_suggestion(
    sid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionOut:
    record = await _load_suggestion(db, user, sid, "SUGGESTION.READ")
    return await _suggestion_out(db, record, user, await _granted(db, user))


@router.patch("/suggestions/{sid}", response_model=SuggestionOut)
async def update_suggestion(
    sid: str,
    body: SuggestionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionOut:
    record = await _load_suggestion(db, user, sid, "SUGGESTION.UPDATE")
    if record.status in be2.SUGGESTION_TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A {record.status.lower()} suggestion is final and cannot be edited.",
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
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _suggestion_out(db, record, user, await _granted(db, user))


@router.post("/suggestions/{sid}/submit", response_model=SuggestionOut)
async def submit_suggestion(
    sid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionOut:
    """DRAFT → the screening workflow. Numbers on submit, not on create: an
    abandoned draft must not burn a number out of the sequence."""
    record = await _load_suggestion(db, user, sid, "SUGGESTION.CREATE")
    if record.status != "DRAFT":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a draft suggestion can be submitted."
        )

    if not record.suggestionNo:
        record.suggestionNo = await be.next_record_number(
            db,
            model=BeSuggestion,
            column=BeSuggestion.suggestionNo,
            prefix="SGN",
            plant_id=record.plantId,
        )

    from app.services import workflow_engine

    try:
        instance = await workflow_engine.initiate(
            db,
            module=be2.WF_MODULE_SUGGESTION,
            record_id=record.id,
            record_number=record.suggestionNo,
            record_title=record.title,
            record_data={"category": record.category, "anonymous": record.isAnonymous},
            initiator_id=user.id,
            plant_id=record.plantId,
        )
        record.workflowInstanceId = instance.id
    except Exception as e:  # noqa: BLE001
        # No workflow definition configured is a deployment state, not a bug in
        # the caller's request. The record still advances so the register is
        # usable before the workflow is set up.
        log.warning("Suggestion %s submitted with no workflow: %s", record.id, e)

    record.status = "SUBMITTED"
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _suggestion_out(db, record, user, await _granted(db, user))


@router.post("/suggestions/{sid}/screen", response_model=SuggestionOut)
async def screen_suggestion(
    sid: str,
    body: SuggestionScreen,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionOut:
    """Stage one of §3's two-stage decision — triage."""
    record = await _load_suggestion(db, user, sid, "SUGGESTION.SCREEN")
    if record.status not in {"SUBMITTED", "SCREENING"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A suggestion at {record.status} is not awaiting triage.",
        )

    if body.duplicateOfSuggestionId:
        other = await db.get(BeSuggestion, body.duplicateOfSuggestionId)
        if other is None or other.plantId != record.plantId:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "The suggestion this duplicates was not found at this plant.",
            )
        if other.id == record.id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A suggestion cannot duplicate itself.",
            )

    record.screeningOutcome = body.outcome
    record.screeningNote = body.note
    record.screenedById = user.id
    record.screenedAt = _now()
    record.duplicateOfSuggestionId = body.duplicateOfSuggestionId

    if body.outcome == "DUPLICATE":
        # Closed at triage — it never reaches the committee, so it never counts
        # in the acceptance rate.
        record.status = "DUPLICATE"
        record.closedAt = _now()
    elif body.outcome == "NOT_RELEVANT":
        record.status = "SCREENING"
    else:
        record.status = "SCREENING"

    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _suggestion_out(db, record, user, await _granted(db, user))


@router.post("/suggestions/{sid}/decide", response_model=SuggestionOut)
async def decide_suggestion(
    sid: str,
    body: SuggestionDecide,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionOut:
    """Stage two — the committee's formal accept / reject / defer."""
    record = await _load_suggestion(db, user, sid, "SUGGESTION.DECIDE")
    if record.status not in {"SCREENING", "DEFERRED"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A suggestion at {record.status} is not awaiting a decision.",
        )
    if not record.screeningOutcome:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Triage this suggestion before deciding on it.",
        )

    now = _now()
    record.decision = body.decision
    record.decisionRationale = body.rationale
    record.decidedById = user.id
    record.decidedAt = now

    if body.decision == "ACCEPT":
        record.status = "ACCEPTED"
    elif body.decision == "REJECT":
        record.status = "REJECTED"
        record.rejectionReason = body.rationale
        record.closedAt = now
    else:
        record.status = "DEFERRED"
        record.deferredUntil = body.deferredUntil

    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _suggestion_out(db, record, user, await _granted(db, user))


@router.post("/suggestions/{sid}/transition/{target}", response_model=SuggestionOut)
async def transition_suggestion(
    sid: str,
    target: str,
    body: SuggestionTransition,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionOut:
    record = await db.get(BeSuggestion, sid)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Suggestion not found")
    await require_permission_with_context(
        "SUGGESTION.READ", user, db, record_id=record.id, plant_id=record.plantId
    )

    target = target.upper()
    granted = await _granted(db, user)
    if not be2.suggestion_transition_permitted(record, target, granted):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A suggestion at {record.status} cannot move to {target}, or you do "
            "not hold the permission that move requires.",
        )

    now = _now()
    if target == "IMPLEMENTED":
        record.implementedAt = record.implementedAt or now
        if body.note:
            record.implementationNote = body.note
    if target == "CLOSED":
        record.closedAt = now
    if target == "SUBMITTED" and record.status == "DEFERRED":
        # Re-entering screening: the previous decision is history, not the
        # current state, so it is cleared rather than left to contradict the
        # status the record now shows.
        record.decision = None
        record.deferredUntil = None

    record.status = target
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _suggestion_out(db, record, user, granted)


@router.post("/suggestions/{sid}/incentive", response_model=SuggestionOut)
async def set_suggestion_incentive(
    sid: str,
    body: SuggestionIncentive,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuggestionOut:
    """§3 incentive tracking. Records the entitlement; paying it is out of scope
    and no payroll or rewards system is called from here."""
    record = await _load_suggestion(db, user, sid, "SUGGESTION.DECIDE")
    if body.status != "NOT_APPLICABLE" and record.status not in {
        "ACCEPTED",
        "IN_IMPLEMENTATION",
        "IMPLEMENTED",
        "CLOSED",
    }:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "§3 ties an incentive to an accepted and implemented suggestion — "
            f"this one is at {record.status}.",
        )

    record.incentiveStatus = body.status
    record.incentivePoints = body.points
    record.incentiveAmount = body.amount
    if body.currency:
        record.currency = body.currency
    record.incentiveNote = body.note
    if body.status in {"APPROVED", "PAID"}:
        record.incentiveApprovedById = user.id
        record.incentiveApprovedAt = _now()
    record.updatedById = user.id
    await db.commit()
    await db.refresh(record)
    return await _suggestion_out(db, record, user, await _granted(db, user))


@router.delete("/suggestions/{sid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_suggestion(
    sid: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    record = await _load_suggestion(db, user, sid, "SUGGESTION.DELETE")
    reason = await _audit_reason(request)
    soft_delete(record, user.id, reason)
    # A withdrawn record must take its benefit lines with it, or the
    # cross-workflow dashboard keeps counting money whose source no
    # longer appears in any register.
    withdrawn = await _withdraw_benefits(
        db, source_type="SUGGESTION", source_id=record.id, actor_id=user.id, reason=reason
    )
    if withdrawn:
        log.info("withdrew %s benefit line(s) with SUGGESTION %s", withdrawn, record.id)
    await db.commit()


# ═════════════════════════════════════════════════════════════════════════════
# QCC — teams
# ═════════════════════════════════════════════════════════════════════════════
async def _load_team(db: AsyncSession, user: User, tid: str, perm: str) -> BeQccTeam:
    record = (
        await db.execute(
            select(BeQccTeam)
            .where(BeQccTeam.id == tid)
            .options(selectinload(BeQccTeam.members))
            # populate_existing: the session is created with
            # expire_on_commit=False, so after a handler adds child rows and
            # commits, the parent is STILL in the identity map with its
            # already-loaded (and now stale) collection. Without this the
            # re-read returns that cached instance and the response says
            # "0 gates" / "0 members" / "0 milestones" about rows that were
            # written correctly a millisecond earlier. The DB was never wrong;
            # the payload was.
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quality circle not found")
    await require_permission_with_context(
        perm, user, db, record_id=record.id, plant_id=record.plantId
    )
    return record


async def _team_project_counts(db: AsyncSession, team_ids: Sequence[str]) -> dict[str, dict[str, int]]:
    """Active and closed project counts per circle, in one query.

    Per-row counting here would be N+1 across the register — the exact shape
    that made the older list screens slow enough to notice.
    """
    if not team_ids:
        return {}
    rows = (
        await db.execute(
            select(BeQccProject.teamId, BeQccProject.status, func.count())
            .where(BeQccProject.teamId.in_(list(team_ids)))
            .group_by(BeQccProject.teamId, BeQccProject.status)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for team_id, st, n in rows:
        bucket = out.setdefault(team_id, {"active": 0, "closed": 0})
        if st == "CLOSED":
            bucket["closed"] += n
        elif st not in {"ABANDONED", "REJECTED"}:
            bucket["active"] += n
    return out


def _team_item(t: BeQccTeam, users: dict, counts: dict, member_count: int) -> dict[str, Any]:
    c = counts.get(t.id, {})
    return {
        "id": t.id,
        "teamNo": t.teamNo,
        "name": t.name,
        "department": t.department,
        "status": t.status,
        "plantId": t.plantId,
        "siteName": t.siteName,
        "areaName": t.areaName,
        "leader": users.get(t.leaderId) if t.leaderId else None,
        "facilitator": users.get(t.facilitatorId) if t.facilitatorId else None,
        "memberCount": member_count,
        "activeProjects": c.get("active", 0),
        "closedProjects": c.get("closed", 0),
        "formedOn": t.formedOn,
        "createdAt": t.createdAt,
    }


@router.get("/qcc/teams", response_model=QccTeamListResponse)
async def list_teams(
    plantId: str | None = Query(default=None),
    tstatus: str | None = Query(default=None, alias="status"),
    department: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccTeamListResponse:
    scope = await build_query_scope(db, user.id, "QCC.READ")

    stmt = select(BeQccTeam).options(selectinload(BeQccTeam.members))
    stmt = scope.apply(stmt, BeQccTeam)
    if plantId:
        stmt = stmt.where(BeQccTeam.plantId == plantId)
    if tstatus:
        stmt = stmt.where(BeQccTeam.status == tstatus)
    if department:
        stmt = stmt.where(BeQccTeam.department == department)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(BeQccTeam.name.ilike(like), BeQccTeam.teamNo.ilike(like)))

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0
    rows = list(
        (
            await db.execute(
                stmt.order_by(BeQccTeam.createdAt.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    counts = await _team_project_counts(db, [r.id for r in rows])
    users = await resolve_user_directory(
        db, [r.leaderId for r in rows] + [r.facilitatorId for r in rows]
    )
    status_counts = {}
    for r in rows:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    return QccTeamListResponse(
        items=[
            _team_item(
                r, users, counts, sum(1 for m in r.members if m.leftAt is None)
            )
            for r in rows
        ],
        total=total,
        statusCounts=status_counts,
    )


async def _team_out(db: AsyncSession, t: BeQccTeam) -> QccTeamOut:
    member_ids = [m.userId for m in t.members]
    users = await resolve_user_directory(db, member_ids + [t.leaderId, t.facilitatorId])
    counts = await _team_project_counts(db, [t.id])
    base = _team_item(t, users, counts, sum(1 for m in t.members if m.leftAt is None))
    return QccTeamOut(
        **base,
        motto=t.motto,
        disbandedOn=t.disbandedOn,
        members=[
            {
                "id": m.id,
                "userId": m.userId,
                "user": users.get(m.userId),
                "memberRole": m.memberRole,
                "joinedAt": m.joinedAt,
                "leftAt": m.leftAt,
            }
            for m in sorted(t.members, key=lambda x: (x.leftAt is not None, x.joinedAt))
        ],
        updatedAt=t.updatedAt,
    )


@router.post("/qcc/teams", response_model=QccTeamOut, status_code=status.HTTP_201_CREATED)
async def create_team(
    body: QccTeamCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccTeamOut:
    await require_permission_with_context("QCC.CREATE", user, db, plant_id=body.plantId)
    if not await be.validate_area(db, plant_id=body.plantId, area_id=body.areaId):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That area does not belong to the selected plant.",
        )
    site_name, area_name = await be.resolve_site_labels(
        db, plant_id=body.plantId, area_id=body.areaId
    )

    team = BeQccTeam(
        **body.model_dump(exclude={"plantId", "areaId", "members"}),
        plantId=body.plantId,
        areaId=body.areaId,
        siteName=site_name,
        areaName=area_name,
        status="FORMING",
        createdById=user.id,
    )
    # A circle is numbered on creation, not on first project: the number IS the
    # circle's identity and it appears on a certificate long before it has a
    # project to show.
    team.teamNo = await be.next_record_number(
        db, model=BeQccTeam, column=BeQccTeam.teamNo, prefix="QCC", plant_id=body.plantId
    )
    db.add(team)
    await db.flush()

    # The two named offices are also membership rows, so "who was in this circle"
    # has exactly one answer.
    seeded = {m.userId: m.memberRole for m in body.members}
    if body.leaderId:
        seeded.setdefault(body.leaderId, "LEADER")
    if body.facilitatorId:
        seeded.setdefault(body.facilitatorId, "FACILITATOR")
    for uid, role in seeded.items():
        db.add(BeQccTeamMember(teamId=team.id, userId=uid, memberRole=role))

    await db.commit()
    return await _team_out(db, await _load_team(db, user, team.id, "QCC.READ"))


@router.get("/qcc/teams/{tid}", response_model=QccTeamOut)
async def get_team(
    tid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccTeamOut:
    return await _team_out(db, await _load_team(db, user, tid, "QCC.READ"))


@router.patch("/qcc/teams/{tid}", response_model=QccTeamOut)
async def update_team(
    tid: str,
    body: QccTeamUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccTeamOut:
    team = await _load_team(db, user, tid, "QCC.UPDATE")
    patch = body.model_dump(include=body.model_fields_set)
    if "areaId" in patch:
        if not await be.validate_area(db, plant_id=team.plantId, area_id=patch["areaId"]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That area does not belong to this circle's plant.",
            )
        _, area_name = await be.resolve_site_labels(
            db, plant_id=team.plantId, area_id=patch["areaId"]
        )
        team.areaName = area_name
    if patch.get("status") == "DISBANDED" and not patch.get("disbandedOn"):
        patch["disbandedOn"] = _now()
    for field, value in patch.items():
        setattr(team, field, value)
    team.updatedById = user.id
    await db.commit()
    return await _team_out(db, await _load_team(db, user, tid, "QCC.READ"))


@router.post("/qcc/teams/{tid}/members", response_model=QccTeamOut)
async def add_team_member(
    tid: str,
    body: QccMemberIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccTeamOut:
    team = await _load_team(db, user, tid, "QCC.UPDATE")
    if team.status == "DISBANDED":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A disbanded circle cannot take new members."
        )

    active = next(
        (m for m in team.members if m.userId == body.userId and m.leftAt is None), None
    )
    if active is not None:
        # Idempotent on the role rather than an error: re-adding somebody to
        # change their role is the obvious reading of this call.
        active.memberRole = body.memberRole
    else:
        db.add(
            BeQccTeamMember(
                teamId=team.id, userId=body.userId, memberRole=body.memberRole
            )
        )
    if body.memberRole == "LEADER":
        team.leaderId = body.userId
    elif body.memberRole == "FACILITATOR":
        team.facilitatorId = body.userId
    team.updatedById = user.id
    await db.commit()
    return await _team_out(db, await _load_team(db, user, tid, "QCC.READ"))


@router.delete("/qcc/teams/{tid}/members/{user_id}", response_model=QccTeamOut)
async def remove_team_member(
    tid: str,
    user_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccTeamOut:
    """Stand a member down. The row is stamped `leftAt`, never deleted — the
    separation-of-duties check on benefit validation has to know who was ever in
    the circle, not just who is in it today."""
    team = await _load_team(db, user, tid, "QCC.UPDATE")
    member = next(
        (m for m in team.members if m.userId == user_id and m.leftAt is None), None
    )
    if member is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "That person is not currently in this circle."
        )
    member.leftAt = _now()
    if team.leaderId == user_id:
        team.leaderId = None
    if team.facilitatorId == user_id:
        team.facilitatorId = None
    team.updatedById = user.id
    await db.commit()
    return await _team_out(db, await _load_team(db, user, tid, "QCC.READ"))


@router.delete("/qcc/teams/{tid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    tid: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    team = await _load_team(db, user, tid, "QCC.DELETE")
    live = (
        await db.execute(
            select(func.count())
            .select_from(BeQccProject)
            .where(
                BeQccProject.teamId == team.id,
                BeQccProject.status.notin_(["CLOSED", "ABANDONED", "REJECTED"]),
            )
        )
    ).scalar() or 0
    if live:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This circle has {live} project(s) still running. Close or abandon "
            "them first, or disband the circle instead of deleting it.",
        )
    soft_delete(team, user.id, await _audit_reason(request))
    await db.commit()


# ═════════════════════════════════════════════════════════════════════════════
# QCC — projects
# ═════════════════════════════════════════════════════════════════════════════
async def _load_project(
    db: AsyncSession, user: User, pid: str, perm: str
) -> BeQccProject:
    record = (
        await db.execute(
            select(BeQccProject)
            .where(BeQccProject.id == pid)
            .options(selectinload(BeQccProject.stages))
            # populate_existing: the session is created with
            # expire_on_commit=False, so after a handler adds child rows and
            # commits, the parent is STILL in the identity map with its
            # already-loaded (and now stale) collection. Without this the
            # re-read returns that cached instance and the response says
            # "0 gates" / "0 members" / "0 milestones" about rows that were
            # written correctly a millisecond earlier. The DB was never wrong;
            # the payload was.
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    await require_permission_with_context(
        perm, user, db, record_id=record.id, plant_id=record.plantId
    )
    return record


def _project_item(p: BeQccProject, team_names: dict, stages: Sequence[BeQccProjectStage]) -> dict[str, Any]:
    progress = be2.project_progress(stages)
    return {
        "id": p.id,
        "projectNo": p.projectNo,
        "title": p.title,
        "category": p.category,
        "status": p.status,
        "plantId": p.plantId,
        "siteName": p.siteName,
        "areaName": p.areaName,
        "teamId": p.teamId,
        "teamName": team_names.get(p.teamId),
        "methodology": p.methodology,
        "rag": be2.project_rag(p, stages),
        "currentStage": progress["currentStage"],
        "signedOffStages": progress["signedOffStages"],
        "totalStages": progress["totalStages"],
        "stagePercent": progress["percent"],
        "baselineValue": p.baselineValue,
        "targetValue": p.targetValue,
        "actualValue": p.actualValue,
        "metricUnit": p.metricUnit,
        "targetDate": p.targetDate,
        "createdAt": p.createdAt,
    }


async def _project_out(
    db: AsyncSession, p: BeQccProject, user: User, granted: set[str]
) -> QccProjectOut:
    stages = sorted(p.stages, key=lambda s: s.sequence)
    team = await db.get(BeQccTeam, p.teamId)
    users = await resolve_user_directory(
        db,
        [p.createdById, p.evaluatedById] + [s.signedOffById for s in stages],
    )
    benefit_rows = await be2.benefits_for(db, source_type="QCC", source_id=p.id)
    benefits = [
        await _benefit_out(db, b, user.id, with_readings=False) for b in benefit_rows
    ]
    summary = be2.summarise_benefits(benefit_rows)

    blockers: dict[str, list[str]] = {}
    for target in be2.allowed_qcc_project_actions(p, granted):
        why = be2.close_gate_blockers(p, stages, benefit_rows, target)
        if why:
            blockers[target] = why

    base = _project_item(p, {p.teamId: team.name if team else None}, stages)
    return QccProjectOut(
        **base,
        problemStatement=p.problemStatement,
        selectionRationale=p.selectionRationale,
        priorityScore=p.priorityScore,
        scope=p.scope,
        baselineMetric=p.baselineMetric,
        charteredAt=p.charteredAt,
        rcaId=p.rcaId,
        rcaStage=be2.rca_stage_for(p),
        evaluationScore=p.evaluationScore,
        evaluationRubric=p.evaluationRubric,
        evaluatedBy=users.get(p.evaluatedById) if p.evaluatedById else None,
        evaluatedAt=p.evaluatedAt,
        presentedAt=p.presentedAt,
        presentationRef=p.presentationRef,
        completedAt=p.completedAt,
        closedAt=p.closedAt,
        rejectionReason=p.rejectionReason,
        workflowInstanceId=p.workflowInstanceId,
        createdBy=users.get(p.createdById),
        updatedAt=p.updatedAt,
        stages=[
            {
                "id": s.id,
                "stage": s.stage,
                "sequence": s.sequence,
                "status": s.status,
                "summary": s.summary,
                "startedAt": s.startedAt,
                "targetDate": s.targetDate,
                "signedOffBy": users.get(s.signedOffById) if s.signedOffById else None,
                "signedOffAt": s.signedOffAt,
                "signOffNote": s.signOffNote,
                "signOffBlockers": be2.stage_signoff_blockers(p, s, stages),
            }
            for s in stages
        ],
        benefits=benefits,
        benefitSummary=summary,
        availableActions=be2.allowed_qcc_project_actions(p, granted),
        transitionBlockers=blockers,
    )


@router.get("/qcc/projects", response_model=QccProjectListResponse)
async def list_projects(
    plantId: str | None = Query(default=None),
    pstatus: str | None = Query(default=None, alias="status"),
    teamId: str | None = Query(default=None),
    category: str | None = Query(default=None),
    rag: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectListResponse:
    scope = await build_query_scope(db, user.id, "QCC.READ")

    stmt = select(BeQccProject).options(selectinload(BeQccProject.stages))
    stmt = scope.apply(stmt, BeQccProject)
    if plantId:
        stmt = stmt.where(BeQccProject.plantId == plantId)
    if pstatus:
        stmt = stmt.where(BeQccProject.status == pstatus)
    if teamId:
        stmt = stmt.where(BeQccProject.teamId == teamId)
    if category:
        stmt = stmt.where(BeQccProject.category == category)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(BeQccProject.title.ilike(like), BeQccProject.projectNo.ilike(like))
        )

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0
    rows = list(
        (
            await db.execute(
                stmt.order_by(BeQccProject.createdAt.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    team_names = {}
    if rows:
        for tid, name in (
            await db.execute(
                select(BeQccTeam.id, BeQccTeam.name).where(
                    BeQccTeam.id.in_([r.teamId for r in rows])
                )
            )
        ).all():
            team_names[tid] = name

    items = [
        _project_item(r, team_names, sorted(r.stages, key=lambda s: s.sequence))
        for r in rows
    ]
    # RAG is computed, so it can only be filtered after the fact. Applied to the
    # page rather than the query — and the count above is deliberately the
    # unfiltered total, because a "3 of 47" that silently became "3 of 3" is
    # worse than an honest one.
    if rag:
        items = [i for i in items if i["rag"] == rag.upper()]

    status_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    return QccProjectListResponse(items=items, total=total, statusCounts=status_counts)


@router.post(
    "/qcc/projects", response_model=QccProjectOut, status_code=status.HTTP_201_CREATED
)
async def create_project(
    body: QccProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    await require_permission_with_context("QCC.CREATE", user, db, plant_id=body.plantId)

    team = await db.get(BeQccTeam, body.teamId)
    if team is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quality circle not found")
    if team.plantId != body.plantId:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That circle belongs to a different plant.",
        )
    if team.status == "DISBANDED":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A disbanded circle cannot take on a new project."
        )
    if not await be.validate_area(db, plant_id=body.plantId, area_id=body.areaId):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That area does not belong to the selected plant.",
        )
    site_name, area_name = await be.resolve_site_labels(
        db, plant_id=body.plantId, area_id=body.areaId
    )

    project = BeQccProject(
        **body.model_dump(exclude={"plantId", "areaId"}),
        plantId=body.plantId,
        areaId=body.areaId,
        siteName=site_name,
        areaName=area_name,
        status="DRAFT",
        createdById=user.id,
    )
    db.add(project)
    await db.flush()
    await db.commit()
    return await _project_out(
        db, await _load_project(db, user, project.id, "QCC.READ"), user, await _granted(db, user)
    )


@router.get("/qcc/projects/{pid}", response_model=QccProjectOut)
async def get_project(
    pid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    record = await _load_project(db, user, pid, "QCC.READ")
    return await _project_out(db, record, user, await _granted(db, user))


@router.patch("/qcc/projects/{pid}", response_model=QccProjectOut)
async def update_project(
    pid: str,
    body: QccProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    project = await _load_project(db, user, pid, "QCC.UPDATE")
    if project.status in be2.QCC_TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A {project.status.lower()} project is final and cannot be edited.",
        )
    patch = body.model_dump(include=body.model_fields_set)
    if "areaId" in patch:
        if not await be.validate_area(db, plant_id=project.plantId, area_id=patch["areaId"]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That area does not belong to this project's plant.",
            )
        _, area_name = await be.resolve_site_labels(
            db, plant_id=project.plantId, area_id=patch["areaId"]
        )
        project.areaName = area_name
    for field, value in patch.items():
        setattr(project, field, value)
    project.updatedById = user.id
    await db.commit()
    return await _project_out(
        db, await _load_project(db, user, pid, "QCC.READ"), user, await _granted(db, user)
    )


@router.post("/qcc/projects/{pid}/charter", response_model=QccProjectOut)
async def charter_project(
    pid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    """DRAFT → CHARTERED. Numbers the project and materialises the full gate
    sequence for its methodology."""
    project = await _load_project(db, user, pid, "QCC.CREATE")
    if project.status != "DRAFT":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a draft project can be chartered."
        )
    if project.baselineValue is None or project.targetValue is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "§6 requires a baseline and a target on the charter — without both "
            "the project's benefit cannot be measured at the end.",
        )

    if not project.projectNo:
        project.projectNo = await be.next_record_number(
            db,
            model=BeQccProject,
            column=BeQccProject.projectNo,
            prefix="QCP",
            plant_id=project.plantId,
        )

    if not project.stages:
        for stage in be2.build_project_stages(project):
            db.add(stage)

    from app.services import workflow_engine

    try:
        instance = await workflow_engine.initiate(
            db,
            module=be2.WF_MODULE_QCC,
            record_id=project.id,
            record_number=project.projectNo,
            record_title=project.title,
            record_data={"methodology": project.methodology, "category": project.category},
            initiator_id=user.id,
            plant_id=project.plantId,
        )
        project.workflowInstanceId = instance.id
    except Exception as e:  # noqa: BLE001
        log.warning("QCC project %s chartered with no workflow: %s", project.id, e)

    project.status = "CHARTERED"
    project.charteredAt = _now()
    project.updatedById = user.id

    team = await db.get(BeQccTeam, project.teamId)
    if team is not None and team.status == "FORMING":
        team.status = "ACTIVE"

    await db.commit()
    return await _project_out(
        db, await _load_project(db, user, pid, "QCC.READ"), user, await _granted(db, user)
    )


@router.post("/qcc/projects/{pid}/rca", response_model=QccProjectOut)
async def open_project_rca(
    pid: str,
    body: QccRcaRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    """Open (or return) the SHARED RootCauseAnalysis for this project.

    §6: "draws on the platform's shared RCA engine — no separate RCA tool or
    duplicate record". The analysis itself is edited in the platform's RCA
    register; this endpoint only creates the link.
    """
    project = await _load_project(db, user, pid, "QCC.UPDATE")
    if project.status in be2.QCC_TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A finished project cannot open a new analysis.",
        )
    try:
        await be2.ensure_project_rca(
            db, project, actor_id=user.id, methodology=body.methodology
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    project.updatedById = user.id
    await db.commit()
    return await _project_out(
        db, await _load_project(db, user, pid, "QCC.READ"), user, await _granted(db, user)
    )


async def _load_stage(
    project: BeQccProject, stage_name: str
) -> BeQccProjectStage:
    stage = next(
        (s for s in project.stages if s.stage == stage_name.upper()), None
    )
    if stage is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{stage_name.upper()} is not a stage of this project "
            f"({project.methodology}).",
        )
    return stage


@router.patch("/qcc/projects/{pid}/stages/{stage_name}", response_model=QccProjectOut)
async def update_stage(
    pid: str,
    stage_name: str,
    body: QccStageUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    project = await _load_project(db, user, pid, "QCC.UPDATE")
    stage = await _load_stage(project, stage_name)
    if stage.status == "SIGNED_OFF":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A signed-off gate cannot be edited. Reopen it first.",
        )
    patch = body.model_dump(include=body.model_fields_set)
    for field, value in patch.items():
        setattr(stage, field, value)
    if stage.status == "IN_PROGRESS" and stage.startedAt is None:
        stage.startedAt = _now()
    project.updatedById = user.id
    await db.commit()
    return await _project_out(
        db, await _load_project(db, user, pid, "QCC.READ"), user, await _granted(db, user)
    )


@router.post(
    "/qcc/projects/{pid}/stages/{stage_name}/signoff", response_model=QccProjectOut
)
async def signoff_stage(
    pid: str,
    stage_name: str,
    body: QccStageSignOff,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    """Close a gate and open the next. §6: "Each stage requires a completion
    sign-off before the project advances"."""
    project = await _load_project(db, user, pid, "QCC.SIGNOFF")
    stages = sorted(project.stages, key=lambda s: s.sequence)
    stage = await _load_stage(project, stage_name)

    blockers = be2.stage_signoff_blockers(project, stage, stages)
    if blockers:
        raise HTTPException(status.HTTP_409_CONFLICT, " ".join(blockers))

    now = _now()
    stage.status = "SIGNED_OFF"
    stage.signedOffById = user.id
    stage.signedOffAt = now
    stage.signOffNote = body.note

    nxt = next(
        (s for s in stages if s.sequence > stage.sequence and s.status == "NOT_STARTED"),
        None,
    )
    if nxt is not None:
        nxt.status = "IN_PROGRESS"
        nxt.startedAt = now

    if project.status == "CHARTERED":
        project.status = "IN_PROGRESS"
    project.updatedById = user.id
    await db.commit()
    return await _project_out(
        db, await _load_project(db, user, pid, "QCC.READ"), user, await _granted(db, user)
    )


@router.post("/qcc/projects/{pid}/evaluate", response_model=QccProjectOut)
async def evaluate_project(
    pid: str,
    body: QccEvaluation,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    """§6 evaluation against a configurable rubric. Convention and competition
    management is explicitly out of scope — this records a score, not an event."""
    project = await _load_project(db, user, pid, "QCC.EVALUATE")
    if project.status not in {"COMPLETED", "BENEFIT_VALIDATION", "CLOSED"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A project is evaluated once its work is complete — this one is at "
            f"{project.status.replace('_', ' ').lower()}.",
        )
    members = await be2.team_member_ids(db, project.teamId, include_past=True)
    if user.id in members:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "A circle cannot score its own project.",
        )
    project.evaluationScore = body.score
    project.evaluationRubric = body.rubric
    project.evaluatedById = user.id
    project.evaluatedAt = _now()
    if body.presentedAt:
        project.presentedAt = body.presentedAt
    if body.presentationRef:
        project.presentationRef = body.presentationRef
    project.updatedById = user.id
    await db.commit()
    return await _project_out(
        db, await _load_project(db, user, pid, "QCC.READ"), user, await _granted(db, user)
    )


@router.post("/qcc/projects/{pid}/transition/{target}", response_model=QccProjectOut)
async def transition_project(
    pid: str,
    target: str,
    body: QccProjectTransition,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectOut:
    project = await _load_project(db, user, pid, "QCC.READ")
    target = target.upper()
    granted = await _granted(db, user)

    if target not in be2.allowed_qcc_project_actions(project, granted):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A project at {project.status} cannot move to {target}, or you do "
            "not hold the permission that move requires.",
        )

    stages = sorted(project.stages, key=lambda s: s.sequence)
    benefits = await be2.benefits_for(db, source_type="QCC", source_id=project.id)
    blockers = be2.close_gate_blockers(project, stages, benefits, target)
    if blockers:
        raise HTTPException(status.HTTP_409_CONFLICT, " ".join(blockers))

    now = _now()
    if target == "COMPLETED":
        project.completedAt = now
    if target == "CLOSED":
        project.closedAt = now
    if target in {"ABANDONED", "REJECTED"} and body.note:
        project.rejectionReason = body.note

    project.status = target
    project.updatedById = user.id
    await db.commit()
    return await _project_out(
        db, await _load_project(db, user, pid, "QCC.READ"), user, granted
    )


@router.get("/qcc/teams/{tid}/projects", response_model=QccProjectListResponse)
async def team_projects(
    tid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QccProjectListResponse:
    """This circle's full history — §6's "team-level history retained across
    multiple projects"."""
    team = await _load_team(db, user, tid, "QCC.READ")
    rows = list(
        (
            await db.execute(
                select(BeQccProject)
                .where(BeQccProject.teamId == team.id)
                .options(selectinload(BeQccProject.stages))
                .order_by(BeQccProject.createdAt.desc())
            )
        )
        .scalars()
        .unique()
        .all()
    )
    status_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
    return QccProjectListResponse(
        items=[
            _project_item(r, {team.id: team.name}, sorted(r.stages, key=lambda s: s.sequence))
            for r in rows
        ],
        total=len(rows),
        statusCounts=status_counts,
    )


@router.delete("/qcc/projects/{pid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    pid: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _load_project(db, user, pid, "QCC.DELETE")
    reason = await _audit_reason(request)
    soft_delete(project, user.id, reason)
    # A withdrawn record must take its benefit lines with it, or the
    # cross-workflow dashboard keeps counting money whose source no
    # longer appears in any register.
    withdrawn = await _withdraw_benefits(
        db, source_type="QCC", source_id=project.id, actor_id=user.id, reason=reason
    )
    if withdrawn:
        log.info("withdrew %s benefit line(s) with QCC %s", withdrawn, project.id)
    await db.commit()


# ═════════════════════════════════════════════════════════════════════════════
# SIP
# ═════════════════════════════════════════════════════════════════════════════
async def _load_sip(db: AsyncSession, user: User, sid: str, perm: str) -> BeSip:
    record = (
        await db.execute(
            select(BeSip)
            .where(BeSip.id == sid)
            .options(selectinload(BeSip.milestones))
            # populate_existing: the session is created with
            # expire_on_commit=False, so after a handler adds child rows and
            # commits, the parent is STILL in the identity map with its
            # already-loaded (and now stale) collection. Without this the
            # re-read returns that cached instance and the response says
            # "0 gates" / "0 members" / "0 milestones" about rows that were
            # written correctly a millisecond earlier. The DB was never wrong;
            # the payload was.
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    await require_permission_with_context(
        perm, user, db, record_id=record.id, plant_id=record.plantId
    )
    return record


def _sip_item(s: BeSip, users: dict, milestones: Sequence[BeSipMilestone]) -> dict[str, Any]:
    ms = be2.milestone_summary(milestones)
    return {
        "id": s.id,
        "sipNo": s.sipNo,
        "title": s.title,
        "category": s.category,
        "status": s.status,
        "plantId": s.plantId,
        "siteName": s.siteName,
        "areaName": s.areaName,
        "department": s.department,
        "sponsor": users.get(s.sponsorId) if s.sponsorId else None,
        "owner": users.get(s.ownerId) if s.ownerId else None,
        "rag": be2.sip_rag(s, milestones),
        "metricName": s.metricName,
        "metricUnit": s.metricUnit,
        "baselineValue": s.baselineValue,
        "targetValue": s.targetValue,
        "latestActualValue": s.latestActualValue,
        "metricPercent": be2.sip_metric_progress(s)["percent"],
        "milestoneTotal": ms["total"],
        "milestoneCompleted": ms["completed"],
        "milestoneLate": ms["late"],
        "startDate": s.startDate,
        "targetDate": s.targetDate,
        "priorityScore": s.priorityScore,
        "createdAt": s.createdAt,
    }


async def _sip_out(db: AsyncSession, s: BeSip, user: User, granted: set[str]) -> SipOut:
    milestones = sorted(s.milestones, key=lambda m: (m.sequence, m.createdAt))
    users = await resolve_user_directory(
        db,
        [s.sponsorId, s.ownerId, s.createdById] + [m.ownerId for m in milestones],
    )
    benefits_rows = await be2.benefits_for(db, source_type="SIP", source_id=s.id)
    benefits = [await _benefit_out(db, b, user.id, with_readings=True) for b in benefits_rows]

    blockers: dict[str, list[str]] = {}
    for target in be2.allowed_sip_actions(s, granted):
        why = be2.sip_close_blockers(s, milestones, benefits_rows, target)
        if why:
            blockers[target] = why

    base = _sip_item(s, users, milestones)
    return SipOut(
        **base,
        scope=s.scope,
        problemStatement=s.problemStatement,
        latestReadingAt=s.latestReadingAt,
        feasibilityScore=s.feasibilityScore,
        impactScore=s.impactScore,
        currency=s.currency,
        investmentCost=s.investmentCost,
        lessonsLearned=s.lessonsLearned,
        lessonsLearnedOplId=s.lessonsLearnedOplId,
        rcaId=s.rcaId,
        completedAt=s.completedAt,
        closedAt=s.closedAt,
        holdReason=s.holdReason,
        rejectionReason=s.rejectionReason,
        workflowInstanceId=s.workflowInstanceId,
        createdBy=users.get(s.createdById),
        updatedAt=s.updatedAt,
        milestones=[
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "sequence": m.sequence,
                "owner": users.get(m.ownerId) if m.ownerId else None,
                "plannedDate": m.plannedDate,
                "revisedDate": m.revisedDate,
                "actualDate": m.actualDate,
                "status": m.status,
                "progressPercent": m.progressPercent,
                "note": m.note,
                "isLate": be2.is_milestone_late(m),
                "slipDays": _days_between(
                    m.plannedDate, m.actualDate or m.revisedDate or _now()
                )
                if m.plannedDate
                else None,
            }
            for m in milestones
        ],
        benefits=benefits,
        benefitSummary=be2.summarise_benefits(benefits_rows),
        metricProgress=be2.sip_metric_progress(s),
        availableActions=be2.allowed_sip_actions(s, granted),
        transitionBlockers=blockers,
    )


@router.get("/sip", response_model=SipListResponse)
async def list_sips(
    plantId: str | None = Query(default=None),
    sstatus: str | None = Query(default=None, alias="status"),
    department: str | None = Query(default=None),
    ownerId: str | None = Query(default=None),
    rag: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SipListResponse:
    scope = await build_query_scope(db, user.id, "SIP.READ")

    stmt = select(BeSip).options(selectinload(BeSip.milestones))
    stmt = scope.apply(stmt, BeSip)
    if plantId:
        stmt = stmt.where(BeSip.plantId == plantId)
    if sstatus:
        stmt = stmt.where(BeSip.status == sstatus)
    if department:
        stmt = stmt.where(BeSip.department == department)
    if ownerId:
        stmt = stmt.where(BeSip.ownerId == ownerId)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(BeSip.title.ilike(like), BeSip.sipNo.ilike(like)))

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0
    rows = list(
        (
            await db.execute(
                stmt.order_by(BeSip.createdAt.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    users = await resolve_user_directory(
        db, [r.sponsorId for r in rows] + [r.ownerId for r in rows]
    )
    items = [
        _sip_item(r, users, sorted(r.milestones, key=lambda m: m.sequence)) for r in rows
    ]
    rag_counts: dict[str, int] = {}
    for i in items:
        rag_counts[i["rag"]] = rag_counts.get(i["rag"], 0) + 1
    if rag:
        items = [i for i in items if i["rag"] == rag.upper()]

    status_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    return SipListResponse(
        items=items, total=total, statusCounts=status_counts, ragCounts=rag_counts
    )


@router.post("/sip", response_model=SipOut, status_code=status.HTTP_201_CREATED)
async def create_sip(
    body: SipCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SipOut:
    await require_permission_with_context("SIP.CREATE", user, db, plant_id=body.plantId)
    if not await be.validate_area(db, plant_id=body.plantId, area_id=body.areaId):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That area does not belong to the selected plant.",
        )
    site_name, area_name = await be.resolve_site_labels(
        db, plant_id=body.plantId, area_id=body.areaId
    )

    data = body.model_dump(exclude={"plantId", "areaId", "milestones"})
    sip = BeSip(
        **data,
        plantId=body.plantId,
        areaId=body.areaId,
        siteName=site_name,
        areaName=area_name,
        status="DRAFT",
        createdById=user.id,
    )
    # §7 "Feasibility and impact scoring used to prioritize competing SIPs".
    # Combined once, here, so the portfolio sorts on one comparable number
    # instead of every screen inventing its own weighting.
    if body.feasibilityScore is not None and body.impactScore is not None:
        sip.priorityScore = round(body.feasibilityScore * body.impactScore, 2)
    db.add(sip)
    await db.flush()

    for i, m in enumerate(body.milestones):
        db.add(
            BeSipMilestone(
                sipId=sip.id,
                plantId=sip.plantId,
                name=m.name,
                description=m.description,
                sequence=m.sequence or i,
                ownerId=m.ownerId,
                plannedDate=m.plannedDate,
                createdById=user.id,
            )
        )

    await db.commit()
    return await _sip_out(
        db, await _load_sip(db, user, sip.id, "SIP.READ"), user, await _granted(db, user)
    )


@router.get("/sip/{sid}", response_model=SipOut)
async def get_sip(
    sid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SipOut:
    record = await _load_sip(db, user, sid, "SIP.READ")
    return await _sip_out(db, record, user, await _granted(db, user))


@router.patch("/sip/{sid}", response_model=SipOut)
async def update_sip(
    sid: str,
    body: SipUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SipOut:
    sip = await _load_sip(db, user, sid, "SIP.UPDATE")
    if sip.status in be2.SIP_TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A {sip.status.lower()} project is final and cannot be edited.",
        )
    patch = body.model_dump(include=body.model_fields_set)
    if "areaId" in patch:
        if not await be.validate_area(db, plant_id=sip.plantId, area_id=patch["areaId"]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That area does not belong to this project's plant.",
            )
        _, area_name = await be.resolve_site_labels(
            db, plant_id=sip.plantId, area_id=patch["areaId"]
        )
        sip.areaName = area_name
    for field, value in patch.items():
        setattr(sip, field, value)
    if sip.sponsorId and sip.ownerId and sip.sponsorId == sip.ownerId:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The sponsor and the project owner must be different people.",
        )
    if sip.feasibilityScore is not None and sip.impactScore is not None:
        sip.priorityScore = round(sip.feasibilityScore * sip.impactScore, 2)
    sip.updatedById = user.id
    await db.commit()
    return await _sip_out(
        db, await _load_sip(db, user, sid, "SIP.READ"), user, await _granted(db, user)
    )


@router.post("/sip/{sid}/milestones", response_model=SipOut)
async def add_milestone(
    sid: str,
    body: SipMilestoneIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SipOut:
    sip = await _load_sip(db, user, sid, "SIP.UPDATE")
    if sip.status in be2.SIP_TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A finished project cannot take new milestones."
        )
    db.add(
        BeSipMilestone(
            sipId=sip.id,
            plantId=sip.plantId,
            name=body.name,
            description=body.description,
            sequence=body.sequence or len(sip.milestones),
            ownerId=body.ownerId,
            plannedDate=body.plannedDate,
            createdById=user.id,
        )
    )
    sip.updatedById = user.id
    await db.commit()
    return await _sip_out(
        db, await _load_sip(db, user, sid, "SIP.READ"), user, await _granted(db, user)
    )


@router.patch("/sip/{sid}/milestones/{mid}", response_model=SipOut)
async def update_milestone(
    sid: str,
    mid: str,
    body: SipMilestoneUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SipOut:
    sip = await _load_sip(db, user, sid, "SIP.UPDATE")
    milestone = next((m for m in sip.milestones if m.id == mid), None)
    if milestone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Milestone not found")

    patch = body.model_dump(include=body.model_fields_set)
    for field, value in patch.items():
        setattr(milestone, field, value)
    if milestone.status == "COMPLETED" and milestone.actualDate is None:
        milestone.actualDate = _now()
    if milestone.progressPercent == 100 and milestone.status == "IN_PROGRESS":
        # 100% and still "in progress" is a contradiction the board would render
        # as a full bar on an open item.
        milestone.status = "COMPLETED"
        milestone.actualDate = milestone.actualDate or _now()
    sip.updatedById = user.id
    await db.commit()
    return await _sip_out(
        db, await _load_sip(db, user, sid, "SIP.READ"), user, await _granted(db, user)
    )


@router.delete("/sip/{sid}/milestones/{mid}", response_model=SipOut)
async def delete_milestone(
    sid: str,
    mid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SipOut:
    """Remove a milestone. Only before the project is approved — after that the
    plan is a commitment, and deleting a milestone is how a tracker quietly loses
    the thing it was late on."""
    sip = await _load_sip(db, user, sid, "SIP.UPDATE")
    if sip.status not in {"DRAFT", "SUBMITTED", "UNDER_REVIEW"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Once a project is approved its milestones can be cancelled but not "
            "deleted — the plan is a record of what was committed to.",
        )
    milestone = next((m for m in sip.milestones if m.id == mid), None)
    if milestone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Milestone not found")
    await db.delete(milestone)
    sip.updatedById = user.id
    await db.commit()
    return await _sip_out(
        db, await _load_sip(db, user, sid, "SIP.READ"), user, await _granted(db, user)
    )


@router.post("/sip/{sid}/transition/{target}", response_model=SipOut)
async def transition_sip(
    sid: str,
    target: str,
    body: SipTransition,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SipOut:
    sip = await _load_sip(db, user, sid, "SIP.READ")
    target = target.upper()
    granted = await _granted(db, user)

    if target not in be2.allowed_sip_actions(sip, granted):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A project at {sip.status} cannot move to {target}, or you do not "
            "hold the permission that move requires.",
        )

    milestones = list(sip.milestones)
    benefits = await be2.benefits_for(db, source_type="SIP", source_id=sip.id)
    blockers = be2.sip_close_blockers(sip, milestones, benefits, target)
    if blockers:
        raise HTTPException(status.HTTP_409_CONFLICT, " ".join(blockers))

    now = _now()
    if target == "SUBMITTED" and not sip.sipNo:
        sip.sipNo = await be.next_record_number(
            db, model=BeSip, column=BeSip.sipNo, prefix="SIP", plant_id=sip.plantId
        )
        from app.services import workflow_engine

        try:
            instance = await workflow_engine.initiate(
                db,
                module=be2.WF_MODULE_SIP,
                record_id=sip.id,
                record_number=sip.sipNo,
                record_title=sip.title,
                record_data={"category": sip.category, "department": sip.department},
                initiator_id=user.id,
                plant_id=sip.plantId,
            )
            sip.workflowInstanceId = instance.id
        except Exception as e:  # noqa: BLE001
            log.warning("SIP %s submitted with no workflow: %s", sip.id, e)

    if target == "IN_PROGRESS" and sip.startDate is None:
        sip.startDate = now
    if target == "ON_HOLD":
        sip.holdReason = body.note
    if target == "IN_PROGRESS":
        sip.holdReason = None
    if target == "COMPLETED":
        sip.completedAt = now
    if target == "CLOSED":
        sip.closedAt = now
    if target in {"REJECTED", "CANCELLED"} and body.note:
        sip.rejectionReason = body.note

    sip.status = target
    sip.updatedById = user.id
    await db.commit()
    return await _sip_out(db, await _load_sip(db, user, sid, "SIP.READ"), user, granted)


@router.delete("/sip/{sid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sip(
    sid: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    sip = await _load_sip(db, user, sid, "SIP.DELETE")
    reason = await _audit_reason(request)
    soft_delete(sip, user.id, reason)
    # A withdrawn record must take its benefit lines with it, or the
    # cross-workflow dashboard keeps counting money whose source no
    # longer appears in any register.
    withdrawn = await _withdraw_benefits(
        db, source_type="SIP", source_id=sip.id, actor_id=user.id, reason=reason
    )
    if withdrawn:
        log.info("withdrew %s benefit line(s) with SIP %s", withdrawn, sip.id)
    await db.commit()


# ═════════════════════════════════════════════════════════════════════════════
# Benefit realisation
# ═════════════════════════════════════════════════════════════════════════════
#: Which register each source type lives in, and the field that names a record.
_BENEFIT_SOURCES: dict[str, tuple[type, str]] = {
    "KAIZEN": (BeKaizen, "kaizenNo"),
    "SUGGESTION": (BeSuggestion, "suggestionNo"),
    "OPL": (BeOpl, "oplNo"),
    "POKA_YOKE": (BePokaYoke, "deviceNo"),
    "QCC": (BeQccProject, "projectNo"),
    "SIP": (BeSip, "sipNo"),
}


async def _withdraw_benefits(
    db: AsyncSession, *, source_type: str, source_id: str, actor_id: str, reason: str
) -> int:
    """Soft-delete every benefit line hanging off a record being withdrawn.

    Without this, a deleted project keeps contributing to the cross-workflow
    "cumulative benefit realised": the register stops showing the project, the
    dashboard keeps counting its money, and the two never reconcile.

    Found by live verification. The offline tests never deleted anything, so
    they could not see it.

    Soft-deleted, not removed: a validated benefit is a financial claim somebody
    signed their name against, so it is withdrawn and kept.
    """
    rows = await be2.benefits_for(db, source_type=source_type, source_id=source_id)
    for b in rows:
        soft_delete(b, actor_id, reason)
    return len(rows)


async def _benefit_out(
    db: AsyncSession, b: BeBenefit, actor_id: str, *, with_readings: bool
) -> BenefitOut:
    users = await resolve_user_directory(
        db, [b.createdById, b.validatedById, b.validatingAuthorityId]
    )
    readings: list[dict[str, Any]] = []
    if with_readings:
        rows = list(
            (
                await db.execute(
                    select(BeBenefitReading)
                    .where(BeBenefitReading.benefitId == b.id)
                    .order_by(BeBenefitReading.readingAt.desc())
                )
            )
            .scalars()
            .all()
        )
        reader_ids = [r.recordedById for r in rows]
        readers = await resolve_user_directory(db, reader_ids)
        readings = [
            {
                "id": r.id,
                "readingAt": r.readingAt,
                "periodLabel": r.periodLabel,
                "actualValue": r.actualValue,
                "targetValue": r.targetValue,
                "note": r.note,
                "recordedBy": readers.get(r.recordedById),
            }
            for r in rows
        ]

    return BenefitOut(
        id=b.id,
        plantId=b.plantId,
        siteName=b.siteName,
        sourceType=b.sourceType,
        sourceId=b.sourceId,
        sourceRef=b.sourceRef,
        benefitType=b.benefitType,
        valueKind=b.valueKind,
        currency=b.currency,
        unit=b.unit,
        projectedValue=b.projectedValue,
        realizedValue=b.realizedValue,
        annualisedValue=b.annualisedValue,
        validationWindowMonths=b.validationWindowMonths,
        validationDueAt=b.validationDueAt,
        validatingAuthority=users.get(b.validatingAuthorityId)
        if b.validatingAuthorityId
        else None,
        validatedBy=users.get(b.validatedById) if b.validatedById else None,
        validatedAt=b.validatedAt,
        validationNote=b.validationNote,
        status=b.status,
        note=b.note,
        isValidationDue=be2.is_validation_due(b),
        createdBy=users.get(b.createdById),
        createdAt=b.createdAt,
        updatedAt=b.updatedAt,
        readings=readings,
        validationBlockers=await be2.validation_blockers(db, b, actor_id=actor_id),
    )


async def _load_benefit(db: AsyncSession, user: User, bid: str, perm: str) -> BeBenefit:
    record = await db.get(BeBenefit, bid)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Benefit line not found")
    await require_permission_with_context(
        perm, user, db, record_id=record.id, plant_id=record.plantId
    )
    return record


@router.get("/benefits", response_model=BenefitListResponse)
async def list_benefits(
    plantId: str | None = Query(default=None),
    sourceType: str | None = Query(default=None),
    sourceId: str | None = Query(default=None),
    bstatus: str | None = Query(default=None, alias="status"),
    dueOnly: bool | None = Query(default=None),
    limit: int = Query(default=200, le=1000),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BenefitListResponse:
    scope = await build_query_scope(db, user.id, "BENEFIT.READ")

    stmt = select(BeBenefit)
    stmt = scope.apply(stmt, BeBenefit)
    if plantId:
        stmt = stmt.where(BeBenefit.plantId == plantId)
    if sourceType:
        stmt = stmt.where(BeBenefit.sourceType == sourceType)
    if sourceId:
        stmt = stmt.where(BeBenefit.sourceId == sourceId)
    if bstatus:
        stmt = stmt.where(BeBenefit.status == bstatus)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0
    rows = list(
        (
            await db.execute(
                stmt.order_by(BeBenefit.createdAt.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    if dueOnly:
        rows = [r for r in rows if be2.is_validation_due(r)]

    return BenefitListResponse(
        items=[await _benefit_out(db, r, user.id, with_readings=False) for r in rows],
        total=total,
        summary=be2.summarise_benefits(rows),
    )


@router.post("/benefits", response_model=BenefitOut, status_code=status.HTTP_201_CREATED)
async def create_benefit(
    body: BenefitCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BenefitOut:
    entry = _BENEFIT_SOURCES.get(body.sourceType)
    if entry is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown benefit source type."
        )
    model, ref_field = entry
    source = await db.get(model, body.sourceId)
    if source is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No {body.sourceType.replace('_', ' ').lower()} record with that id.",
        )
    await require_permission_with_context(
        "BENEFIT.RECORD", user, db, plant_id=source.plantId
    )

    existing = await be2.benefits_for(
        db, source_type=body.sourceType, source_id=body.sourceId
    )
    if any(b.benefitType == body.benefitType for b in existing):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This record already has a {body.benefitType.replace('_', ' ').lower()} "
            "benefit line. Edit it rather than adding a second — two lines of the "
            "same type on one record double-count on the dashboard.",
        )

    benefit = BeBenefit(
        **body.model_dump(exclude={"validationWindowMonths"}),
        plantId=source.plantId,
        siteName=getattr(source, "siteName", None),
        sourceRef=getattr(source, ref_field, None),
        validationWindowMonths=body.validationWindowMonths,
        validationDueAt=be2.validation_due_from(body.validationWindowMonths),
        status="PROJECTED",
        createdById=user.id,
    )
    db.add(benefit)
    await db.commit()
    await db.refresh(benefit)
    return await _benefit_out(db, benefit, user.id, with_readings=True)


@router.get("/benefits/{bid}", response_model=BenefitOut)
async def get_benefit(
    bid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BenefitOut:
    record = await _load_benefit(db, user, bid, "BENEFIT.READ")
    return await _benefit_out(db, record, user.id, with_readings=True)


@router.patch("/benefits/{bid}", response_model=BenefitOut)
async def update_benefit(
    bid: str,
    body: BenefitUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BenefitOut:
    benefit = await _load_benefit(db, user, bid, "BENEFIT.RECORD")
    if benefit.status == "VALIDATED":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A validated benefit is final. Editing the figure after sign-off "
            "would make the validation a claim about a number that no longer "
            "exists.",
        )
    patch = body.model_dump(include=body.model_fields_set)
    if "validationWindowMonths" in patch:
        benefit.validationDueAt = be2.validation_due_from(patch["validationWindowMonths"])
    for field, value in patch.items():
        setattr(benefit, field, value)
    benefit.updatedById = user.id
    await db.commit()
    await db.refresh(benefit)
    return await _benefit_out(db, benefit, user.id, with_readings=True)


@router.post("/benefits/{bid}/claim", response_model=BenefitOut)
async def claim_benefit(
    bid: str,
    body: BenefitClaim,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BenefitOut:
    """Submit the realised figure for independent validation.

    Deliberately does NOT set status to VALIDATED — that is somebody else's act,
    and the whole point of the window is that the two are separated in time and
    in person.
    """
    benefit = await _load_benefit(db, user, bid, "BENEFIT.RECORD")
    if benefit.status == "VALIDATED":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This benefit has already been validated."
        )
    benefit.realizedValue = body.realizedValue
    benefit.status = "PENDING_VALIDATION"
    if body.note:
        benefit.note = body.note
    if benefit.validationDueAt is None and benefit.validationWindowMonths:
        benefit.validationDueAt = be2.validation_due_from(benefit.validationWindowMonths)
    benefit.updatedById = user.id
    await db.commit()
    await db.refresh(benefit)
    return await _benefit_out(db, benefit, user.id, with_readings=True)


@router.post("/benefits/{bid}/validate", response_model=BenefitOut)
async def validate_benefit(
    bid: str,
    body: BenefitValidate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BenefitOut:
    """Independent sign-off. §6/§7's separation-of-duties rule is enforced in
    services/business_excellence_p2.validation_blockers() — one implementation
    for all six workflows."""
    benefit = await _load_benefit(db, user, bid, "BENEFIT.VALIDATE")

    if body.realizedValue is not None:
        benefit.realizedValue = body.realizedValue

    blockers = await be2.validation_blockers(db, benefit, actor_id=user.id)
    if blockers:
        raise HTTPException(status.HTTP_403_FORBIDDEN, " ".join(blockers))

    now = _now()
    benefit.validatedById = user.id
    benefit.validatedAt = now
    benefit.validationNote = body.note
    benefit.status = "VALIDATED" if body.accept else "REJECTED"
    benefit.updatedById = user.id

    # Mirror onto Kaizen's own verified-savings columns, which are live in prod
    # and read by the Phase 1 register. One number, two places, written together
    # so they cannot disagree.
    if body.accept and benefit.sourceType == "KAIZEN" and benefit.valueKind == "FINANCIAL":
        kaizen = await db.get(BeKaizen, benefit.sourceId)
        if kaizen is not None:
            kaizen.verifiedAnnualSaving = benefit.annualisedValue or benefit.realizedValue
            kaizen.verifiedById = user.id
            kaizen.verifiedAt = now
            kaizen.verificationNote = body.note

    await db.commit()
    await db.refresh(benefit)
    return await _benefit_out(db, benefit, user.id, with_readings=True)


@router.post("/benefits/{bid}/readings", response_model=BenefitOut)
async def add_benefit_reading(
    bid: str,
    body: BenefitReadingCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BenefitOut:
    """§7: actual-vs-target over the life of the project, not just at closure."""
    benefit = await _load_benefit(db, user, bid, "BENEFIT.RECORD")
    await be2.record_reading(
        db,
        benefit,
        actual_value=body.actualValue,
        target_value=body.targetValue,
        period_label=body.periodLabel,
        note=body.note,
        actor_id=user.id,
    )
    await db.commit()
    await db.refresh(benefit)
    return await _benefit_out(db, benefit, user.id, with_readings=True)


# ═════════════════════════════════════════════════════════════════════════════
# Cross-workflow dashboard (§8)
# ═════════════════════════════════════════════════════════════════════════════
@router.get("/dashboards/summary", response_model=BeDashboardOut)
async def dashboard_summary(
    plantId: str | None = Query(default=None),
    department: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BeDashboardOut:
    """§8's consolidated Business Excellence dashboard, across all six workflows.

    SCOPED SERVER-SIDE, PER WORKFLOW. Each register is filtered by the plants the
    caller may see for THAT register's own READ permission — someone who can read
    Kaizen but not SIP gets Kaizen numbers and a zeroed SIP row, not a total that
    quietly includes records they are not entitled to. Returning everything and
    filtering in the browser would leak the totals even if it hid the rows.
    """
    from app.models.business_excellence import (
        KAIZEN_OPEN_STATUSES,
        OPL_OPEN_STATUSES,
        POKA_YOKE_OPEN_STATUSES,
    )
    from app.models.business_excellence_p2 import (
        QCC_PROJECT_OPEN_STATUSES,
        SIP_OPEN_STATUSES,
        SUGGESTION_OPEN_STATUSES,
    )

    registers: list[tuple[str, type, str, tuple[str, ...], tuple[str, ...], str]] = [
        ("KAIZEN", BeKaizen, "KAIZEN.READ", KAIZEN_OPEN_STATUSES, ("REJECTED",), "CLOSED"),
        ("SUGGESTION", BeSuggestion, "SUGGESTION.READ", SUGGESTION_OPEN_STATUSES, ("REJECTED", "DUPLICATE"), "CLOSED"),
        ("OPL", BeOpl, "OPL.READ", OPL_OPEN_STATUSES, ("REJECTED",), "PUBLISHED"),
        ("POKA_YOKE", BePokaYoke, "POKAYOKE.READ", POKA_YOKE_OPEN_STATUSES, ("REJECTED",), "VERIFIED"),
        ("QCC", BeQccProject, "QCC.READ", QCC_PROJECT_OPEN_STATUSES, ("REJECTED", "ABANDONED"), "CLOSED"),
        ("SIP", BeSip, "SIP.READ", SIP_OPEN_STATUSES, ("REJECTED", "CANCELLED"), "CLOSED"),
    ]

    summaries: list[WorkflowSummary] = []
    for label, model, perm, open_statuses, rejected_statuses, closed_status in registers:
        scope = await build_query_scope(db, user.id, perm)
        # Cycle time = createdAt → closedAt where the register has a closedAt,
        # and createdAt → updatedAt where it does not (OPL, Poka Yoke).
        #
        # ⚠ For those two that is a PROXY, not the real figure: it measures time
        # to last edit, not time to publication or first verification. Reported
        # anyway because "no cycle time for two of six workflows" is less useful
        # than an approximation, but it should not be quoted as precise without
        # adding the columns those registers lack.
        end_col = getattr(model, "closedAt", None)
        elapsed = (end_col if end_col is not None else model.updatedAt) - model.createdAt
        base = select(
            model.status, func.count(), func.avg(func.extract("epoch", elapsed))
        )
        if not scope.all_plants and not scope.plant_ids:
            # Fail closed: no readable plants means a zeroed row, not all rows.
            # This is the whole point of scoping the dashboard server-side —
            # returning the totals and hiding the rows still leaks the totals.
            summaries.append(WorkflowSummary(workflowType=label))
            continue
        base = base.where(scope.plant_filter(model.plantId))
        if plantId:
            base = base.where(model.plantId == plantId)
        if department and hasattr(model, "department"):
            base = base.where(model.department == department)

        rows = (await db.execute(base.group_by(model.status))).all()
        by_status = {st: (n, secs) for st, n, secs in rows}
        total = sum(n for n, _ in by_status.values())
        closed = by_status.get(closed_status, (0, None))[0]
        rejected = sum(by_status.get(s, (0, None))[0] for s in rejected_statuses)
        open_n = sum(by_status.get(s, (0, None))[0] for s in open_statuses)

        decided = closed + rejected
        cycle_secs = by_status.get(closed_status, (0, None))[1]
        summaries.append(
            WorkflowSummary(
                workflowType=label,
                total=total,
                open=open_n,
                closed=closed,
                rejected=rejected,
                # None, not 0, when nothing has been decided — see the schema.
                conversionRate=round(closed * 100.0 / decided, 1) if decided else None,
                avgCycleTimeDays=round(float(cycle_secs) / 86400.0, 1) if cycle_secs else None,
            )
        )

    # Benefit rollup, scoped on its own permission.
    bscope = await build_query_scope(db, user.id, "BENEFIT.READ")
    bstmt = bscope.apply(select(BeBenefit), BeBenefit)
    if plantId:
        bstmt = bstmt.where(BeBenefit.plantId == plantId)
    benefit_rows = list((await db.execute(bstmt)).scalars().all())

    # §6 leaderboard — closed projects and validated benefit per circle.
    qscope = await build_query_scope(db, user.id, "QCC.READ")
    lstmt = (
        select(BeQccTeam.id, BeQccTeam.name, func.count(BeQccProject.id))
        .join(BeQccProject, BeQccProject.teamId == BeQccTeam.id)
        .where(BeQccProject.status == "CLOSED")
        .group_by(BeQccTeam.id, BeQccTeam.name)
        .order_by(func.count(BeQccProject.id).desc())
        .limit(10)
    )
    lstmt = lstmt.where(qscope.plant_filter(BeQccTeam.plantId))
    if plantId:
        lstmt = lstmt.where(BeQccTeam.plantId == plantId)
    leaderboard = [
        {"teamId": tid, "teamName": name, "closedProjects": n}
        for tid, name, n in (await db.execute(lstmt)).all()
    ]

    # §7 portfolio by RAG. Computed, so the rows are loaded and bucketed here.
    sscope = await build_query_scope(db, user.id, "SIP.READ")
    sstmt = sscope.apply(select(BeSip).options(selectinload(BeSip.milestones)), BeSip)
    if plantId:
        sstmt = sstmt.where(BeSip.plantId == plantId)
    portfolio: dict[str, int] = {}
    for s in (await db.execute(sstmt)).scalars().unique().all():
        rag = be2.sip_rag(s, list(s.milestones))
        portfolio[rag] = portfolio.get(rag, 0) + 1

    return BeDashboardOut(
        workflows=summaries,
        benefit=be2.summarise_benefits(benefit_rows),
        qccLeaderboard=leaderboard,
        sipPortfolio=portfolio,
        generatedAt=_now(),
    )
