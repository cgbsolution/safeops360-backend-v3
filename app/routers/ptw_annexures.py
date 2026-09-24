"""PTW hazard annexures + precaution checklists.

  GET    /api/ptw/precautions/catalog             — checklist master (wizard)
  GET    /api/ptw/{id}/annexures                  — annexures + shared verdict
  POST   /api/ptw/{id}/annexures                  — attach a hazard
  DELETE /api/ptw/{id}/annexures/{hazardType}     — detach a hazard
  PUT    /api/ptw/{id}/annexures/{hazardType}/answers  — record answers

Model: a permit carries ONE base `type` plus N hazard annexures. Attaching a
hazard brings its precaution checklist, tightens the validity cap and can
escalate the approval chain — see `app/services/ptw_hazards.py`.

Approval model — ONE signature for the whole permit. There is no per-annexure
sign-off endpoint here, and that is deliberate: the site EHS officer certifies
every attached checklist at once via the single `Safety Officer Approval` step
in the resolved workflow chain, exactly as the paper form does (the same four
signatories sign the permit, not each checklist). The engine stamps
`Permit.precautionsCertifiedAt` at that step and refuses it while any
checklist is incomplete.

Mutation window: annexures and answers are editable only while the permit is
still in the approval chain (DRAFT / SUBMITTED) AND uncertified. Once the EHS
officer has signed, the checklist is the certified record — changing it would
silently alter what was signed for.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.permit import (
    Permit,
    PermitHazardAnnexure,
    PermitHazardType,
    PermitPrecautionItem,
    PermitPrecautionResponse,
    PermitStatus,
    PrecautionResponse,
)
from app.models.user import User
from app.schemas.permit import (
    AnnexureAnswersRequest,
    AnnexureAttachRequest,
    AnnexureCompletenessOut,
    HazardAnnexureOut,
    PrecautionAnswerOut,
    PrecautionItemOut,
)
from app.services import ptw_annexures
from app.services.ptw_hazards import (
    HAZARD_LABELS,
    effective_hazards,
    hazard_for_base_type,
    required_controls,
    validity_cap_hours,
    workflow_type_for,
)
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/ptw", tags=["ptw-annexures"])

# Statuses at which the checklist may still be edited. Past these the permit
# is either certified, live, or terminal — in every case the checklist is a
# record of what was signed for, not a working document.
_EDITABLE_STATUSES = {PermitStatus.DRAFT, PermitStatus.SUBMITTED}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Thin alias over the shared implementation. This helper used to be copied
# byte-for-byte into four PTW routers, so a rule fixed in one silently drifted
# from the other three — the named-party read rule now lives in one place.
from app.services.ptw_access import load_permit_or_403 as _load_permit_or_403  # noqa: E402


def _assert_editable(permit: Permit) -> None:
    """One place that decides whether the checklist may still change, so the
    attach / detach / answer endpoints can never disagree."""
    if permit.precautionsCertifiedAt is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The site EHS officer has already certified this permit's precaution "
            "checklists. Reopen the approval (reject the permit) to change them.",
        )
    if permit.status not in _EDITABLE_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Precaution checklists cannot be edited once the permit is {permit.status.value}.",
        )


# ─── Catalog ────────────────────────────────────────────────────────────


@router.get("/precautions/catalog", response_model=dict[str, list[PrecautionItemOut]])
async def get_precaution_catalog(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[PrecautionItemOut]]:
    """Active checklist master, grouped by hazard type.

    Read-only reference data behind plain authentication — the wizard needs it
    before a permit exists, so there is no record to scope against.

    Note the route is declared BEFORE `/{permit_id}/annexures`: FastAPI matches
    in declaration order, and `precautions` would otherwise be captured as a
    permit id.
    """
    grouped = await ptw_annexures.load_catalog(db)
    return {
        hazard.value: [PrecautionItemOut.model_validate(i) for i in items]
        for hazard, items in grouped.items()
    }


# ─── Read ───────────────────────────────────────────────────────────────


async def build_completeness_payload(
    db: AsyncSession, permit: Permit
) -> AnnexureCompletenessOut:
    """The shared read model — used by GET here and by the permit detail page.

    Derived facts (effective risk type, validity cap, required controls) are
    recomputed from the CURRENT annexures rather than read off the permit's
    snapshot columns, so the panel shows what a change would mean before it is
    saved. The snapshot is what the workflow actually ran on.
    """
    annexures = (
        (
            await db.execute(
                select(PermitHazardAnnexure)
                .where(PermitHazardAnnexure.permitId == permit.id)
                .options(selectinload(PermitHazardAnnexure.responses))
            )
        )
        .scalars()
        .all()
    )
    verdict = await ptw_annexures.evaluate(db, permit.id)
    by_hazard = {v.hazardType: v for v in verdict.annexures}
    catalog = await ptw_annexures.load_catalog(db, [a.hazardType for a in annexures])

    attached = [a.hazardType for a in annexures]
    out: list[HazardAnnexureOut] = []
    for a in annexures:
        v = by_hazard.get(a.hazardType.value)
        out.append(
            HazardAnnexureOut(
                id=a.id,
                hazardType=a.hazardType,
                label=HAZARD_LABELS.get(a.hazardType, a.hazardType.value),
                isPrimary=a.isPrimary,
                completedAt=a.completedAt,
                completedById=a.completedById,
                notes=a.notes,
                items=[
                    PrecautionItemOut.model_validate(i)
                    for i in catalog.get(a.hazardType, [])
                ],
                answers=[PrecautionAnswerOut.model_validate(r) for r in (a.responses or [])],
                complete=v.complete if v else True,
                unanswered=v.unanswered if v else [],
                refused=v.refused if v else [],
                naWithoutRemark=v.naWithoutRemark if v else [],
            )
        )
    out.sort(key=lambda o: (not o.isPrimary, o.label))

    return AnnexureCompletenessOut(
        complete=verdict.complete,
        blocker=verdict.blocker,
        effectiveRiskType=workflow_type_for(permit.type, attached),
        validityCapHours=validity_cap_hours(permit.type, attached),
        requiredControls=sorted(required_controls(permit.type, attached)),
        precautionsCertifiedAt=permit.precautionsCertifiedAt,
        precautionsCertifiedById=permit.precautionsCertifiedById,
        annexures=out,
    )


@router.get("/{permit_id}/annexures", response_model=AnnexureCompletenessOut)
async def get_annexures(
    permit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnexureCompletenessOut:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.READ")
    return await build_completeness_payload(db, permit)


# ─── Attach / detach ────────────────────────────────────────────────────


async def attach_annexure(
    db: AsyncSession,
    *,
    permit: Permit,
    hazard: PermitHazardType,
    user_id: str | None,
    is_primary: bool = False,
    notes: str | None = None,
) -> PermitHazardAnnexure:
    """Idempotent attach — returns the existing row if the hazard is already
    on the permit. Shared with `create_permit`, which attaches the base
    type's annexure plus whatever the wizard sent."""
    existing = (
        await db.execute(
            select(PermitHazardAnnexure).where(
                PermitHazardAnnexure.permitId == permit.id,
                PermitHazardAnnexure.hazardType == hazard,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if is_primary and not existing.isPrimary:
            existing.isPrimary = True
        if notes:
            existing.notes = notes
        return existing

    row = PermitHazardAnnexure(
        permitId=permit.id,
        hazardType=hazard,
        isPrimary=is_primary,
        addedById=user_id,
        notes=notes,
    )
    db.add(row)
    await db.flush()
    return row


@router.post("/{permit_id}/annexures", response_model=AnnexureCompletenessOut)
async def add_annexure(
    permit_id: str,
    payload: AnnexureAttachRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnexureCompletenessOut:
    """Attach a hazard to an existing permit.

    Refused when the new hazard would tighten the validity cap below the
    window already granted — silently shortening a live authorisation is worse
    than making the user re-plan the job.
    """
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    _assert_editable(permit)

    current = [
        a.hazardType
        for a in (
            await db.execute(
                select(PermitHazardAnnexure).where(
                    PermitHazardAnnexure.permitId == permit.id
                )
            )
        ).scalars().all()
    ]
    if payload.hazardType in current:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{HAZARD_LABELS.get(payload.hazardType, payload.hazardType.value)} is "
            "already attached to this permit.",
        )

    from app.services.ptw_type_config import assert_type_allowed

    await assert_type_allowed(db, permit.plantId, permit.type, [payload.hazardType])

    proposed = [*current, payload.hazardType]
    cap = validity_cap_hours(permit.type, proposed)
    granted_h = (permit.validTo - permit.validFrom).total_seconds() / 3600.0
    if granted_h > cap:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            (
                f"Adding {HAZARD_LABELS.get(payload.hazardType, payload.hazardType.value)} "
                f"caps this permit at {cap}h, but its window is {granted_h:.0f}h. "
                "Shorten the validity window first, or raise a separate permit."
            ),
        )

    await attach_annexure(
        db, permit=permit, hazard=payload.hazardType, user_id=user.id
    )
    # The union chain may have changed. The snapshot is refreshed here so the
    # detail page reads true; the workflow instance itself is NOT re-routed —
    # see the note in `remove_annexure`.
    permit.effectiveRiskType = workflow_type_for(permit.type, proposed)
    await db.flush()
    return await build_completeness_payload(db, permit)


@router.delete("/{permit_id}/annexures/{hazard_type}", response_model=AnnexureCompletenessOut)
async def remove_annexure(
    permit_id: str,
    hazard_type: PermitHazardType,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnexureCompletenessOut:
    """Detach a hazard. The base type's own annexure cannot be detached — it
    is what the permit number and the register column mean.

    ⚠ Detaching does NOT re-route an in-flight workflow instance to a shorter
    chain. The approvals already collected stay collected; dropping a hazard
    cannot retroactively un-require the Plant Head who already signed. The
    snapshot column is updated so reporting reflects the final shape.
    """
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    _assert_editable(permit)

    row = (
        await db.execute(
            select(PermitHazardAnnexure).where(
                PermitHazardAnnexure.permitId == permit.id,
                PermitHazardAnnexure.hazardType == hazard_type,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Annexure not attached to this permit.")
    if row.isPrimary or hazard_type == hazard_for_base_type(permit.type):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The base permit type's own annexure cannot be removed. Change the "
            "permit type instead.",
        )

    await db.delete(row)
    await db.flush()

    remaining = [
        a.hazardType
        for a in (
            await db.execute(
                select(PermitHazardAnnexure).where(
                    PermitHazardAnnexure.permitId == permit.id
                )
            )
        ).scalars().all()
    ]
    permit.effectiveRiskType = workflow_type_for(permit.type, remaining)
    await db.flush()
    return await build_completeness_payload(db, permit)


# ─── Answers ────────────────────────────────────────────────────────────


async def record_answers(
    db: AsyncSession,
    *,
    annexure: PermitHazardAnnexure,
    answers: list[Any],
    user_id: str | None,
) -> None:
    """Upsert answers onto one annexure.

    Every `itemId` is validated against the catalog for THIS annexure's hazard
    — an answer to a hot-work line cannot be smuggled onto the height
    checklist to make it look complete.
    """
    if not answers:
        return

    valid_ids = {
        i.id
        for i in (
            await db.execute(
                select(PermitPrecautionItem).where(
                    PermitPrecautionItem.hazardType == annexure.hazardType,
                    PermitPrecautionItem.isActive.is_(True),
                )
            )
        ).scalars().all()
    }
    unknown = [a.itemId for a in answers if a.itemId not in valid_ids]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            (
                f"{len(unknown)} answer(s) do not belong to the "
                f"{HAZARD_LABELS.get(annexure.hazardType, annexure.hazardType.value)} "
                "checklist."
            ),
        )

    existing = {
        r.itemId: r
        for r in (
            await db.execute(
                select(PermitPrecautionResponse).where(
                    PermitPrecautionResponse.annexureId == annexure.id
                )
            )
        ).scalars().all()
    }

    for a in answers:
        resp = a.response if isinstance(a.response, PrecautionResponse) else PrecautionResponse(a.response)
        remark = (a.remark or "").strip() or None
        row = existing.get(a.itemId)
        if row is None:
            db.add(
                PermitPrecautionResponse(
                    annexureId=annexure.id,
                    itemId=a.itemId,
                    response=resp,
                    remark=remark,
                    respondedById=user_id,
                )
            )
        else:
            row.response = resp
            row.remark = remark
            row.respondedById = user_id
            row.respondedAt = _now()
    await db.flush()


@router.put("/{permit_id}/annexures/{hazard_type}/answers", response_model=AnnexureCompletenessOut)
async def put_answers(
    permit_id: str,
    hazard_type: PermitHazardType,
    payload: AnnexureAnswersRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnexureCompletenessOut:
    permit = await _load_permit_or_403(db, permit_id, user, "PTW.UPDATE")
    _assert_editable(permit)

    annexure = (
        await db.execute(
            select(PermitHazardAnnexure).where(
                PermitHazardAnnexure.permitId == permit.id,
                PermitHazardAnnexure.hazardType == hazard_type,
            )
        )
    ).scalar_one_or_none()
    if annexure is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Annexure not attached to this permit.")

    await record_answers(db, annexure=annexure, answers=payload.answers, user_id=user.id)
    if payload.notes is not None:
        annexure.notes = payload.notes.strip() or None
    await ptw_annexures.stamp_annexure_completion(db, permit.id, user.id)
    return await build_completeness_payload(db, permit)
