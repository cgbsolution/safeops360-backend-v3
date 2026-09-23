"""BRSR Reporting API.

Mounted at `/api/brsr`. Gated by BRSR.* permission codes; the module is
registered in the licensing registry as `BRSR`.

Two invariants every write path here enforces, because neither can be enforced
in the UI alone:

* **A FILED cycle is immutable.** `_assert_writable` guards every mutation.
  The report snapshot is already frozen at that point, so a write that slipped
  through would put the database and the filed disclosure permanently out of
  step with no way to tell which was right.

* **The server decides provenance.** A client cannot declare its own figure
  AUTO, and overriding an auto-populated number requires a reason. If the
  requirement to justify an override were keyed on something the client sent,
  any client could dodge it — the same reasoning the observation severity
  engine is built on.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_permission_with_context
from app.models.brsr import (
    CYCLE_APPROVED,
    CYCLE_DATA_COLLECTION,
    CYCLE_DRAFT,
    CYCLE_FILED,
    CYCLE_LOCKED_STATUSES,
    CYCLE_REVIEW,
    CYCLE_STATUSES,
    ENV_DRAFT,
    ENV_STATUSES,
    ENV_SUBMITTED,
    ENV_VERIFIED,
    INDICATOR_ESSENTIAL,
    INDICATOR_LEADERSHIP,
    MANUAL_ONLY_PRINCIPLES,
    PLATFORM_SOURCED_PRINCIPLES,
    PRINCIPLES,
    PROVENANCE_AUTO,
    PROVENANCE_AUTO_OVERRIDDEN,
    PROVENANCE_MANUAL,
    PROVENANCE_NOT_APPLICABLE,
    SECTION_C,
    BrsrEmissionFactor,
    BrsrEnvironmentalMetric,
    BrsrEnvMetricLine,
    BrsrIndicator,
    BrsrIndicatorValue,
    BrsrPrincipleResponse,
    BrsrReportingCycle,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas.brsr import (
    CycleCreate,
    CycleDashboardOut,
    CycleOut,
    CycleTransition,
    CycleUpdate,
    EmissionFactorOut,
    EnvLineOut,
    EnvMetricCreate,
    EnvMetricOut,
    EnvMetricUpdate,
    EnvSlotOut,
    EnvTotalsOut,
    IndicatorOut,
    IndicatorValueOut,
    IndicatorValueWrite,
    IndicatorVerify,
    IndicatorWithValue,
    PrincipleDetailOut,
    PrincipleProgressOut,
    PrincipleResponseOut,
    PrincipleResponseUpdate,
    SweepResultOut,
    TrendPointOut,
)
from app.services.report_pdf import render_brsr_report_pdf
from app.services import (
    brsr_completion,
    brsr_env,
    brsr_env_rollup,
    brsr_mapping,
    brsr_report,
    brsr_taxonomy,
)

router = APIRouter(prefix="/api/brsr", tags=["brsr"])

# Minimum length of an override justification. Enforced server-side as well as
# in the form — a form-only rule is not a rule.
MIN_OVERRIDE_REASON_CHARS = 10

# Legal forward transitions. A cycle may be sent back to DATA_COLLECTION from
# REVIEW (a reviewer finding a gap is normal), but never out of APPROVED or
# FILED — reopening an approved disclosure has to be a new cycle, not an edit.
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    CYCLE_DRAFT: (CYCLE_DATA_COLLECTION,),
    CYCLE_DATA_COLLECTION: (CYCLE_REVIEW,),
    CYCLE_REVIEW: (CYCLE_DATA_COLLECTION, CYCLE_APPROVED),
    CYCLE_APPROVED: (CYCLE_FILED,),
    CYCLE_FILED: (),
}


# ── helpers ─────────────────────────────────────────────────────────────────


async def _get_cycle(db: AsyncSession, cycle_id: str) -> BrsrReportingCycle:
    cycle = (
        await db.execute(
            select(BrsrReportingCycle).where(
                BrsrReportingCycle.id == cycle_id,
                BrsrReportingCycle.isDeleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "BRSR cycle not found")
    return cycle


def _assert_writable(cycle: BrsrReportingCycle) -> None:
    """Refuse any mutation to a filed cycle."""
    if cycle.status in CYCLE_LOCKED_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cycle {cycle.financialYear} is {cycle.status} and immutable. "
            "A filed disclosure cannot be edited; raise a new cycle instead.",
        )


async def _ensure_principle_rows(
    db: AsyncSession, cycle: BrsrReportingCycle, actor_id: str | None
) -> list[BrsrPrincipleResponse]:
    """Create the nine principle rows for a cycle if they are not there yet."""
    existing = {
        r.principle: r
        for r in (
            await db.execute(
                select(BrsrPrincipleResponse).where(BrsrPrincipleResponse.cycleId == cycle.id)
            )
        ).scalars()
    }
    for principle in PRINCIPLES:
        if principle in existing:
            continue
        row = BrsrPrincipleResponse(
            cycleId=cycle.id,
            principle=principle,
            isPlatformSourced=principle in PLATFORM_SOURCED_PRINCIPLES,
            createdBy=actor_id,
        )
        db.add(row)
        existing[principle] = row
    await db.flush()
    return [existing[p] for p in PRINCIPLES]


def _progress(resp: BrsrPrincipleResponse) -> PrincipleProgressOut:
    return PrincipleProgressOut(
        principle=resp.principle,
        title=brsr_report.PRINCIPLE_TITLES.get(resp.principle, resp.principle),
        status=resp.status,
        isPlatformSourced=resp.isPlatformSourced,
        totalIndicators=resp.totalIndicators,
        answeredIndicators=resp.answeredIndicators,
        autoPopulatedIndicators=resp.autoPopulatedIndicators,
        manualPendingIndicators=max(0, resp.totalIndicators - resp.answeredIndicators),
        completionPct=resp.completionPct,
        autoPopulatedPct=resp.autoPopulatedPct,
    )


async def _env_metric_out(db: AsyncSession, row: BrsrEnvironmentalMetric) -> EnvMetricOut:
    """Serialise an environmental submission WITHOUT touching `row.lines`.

    `EnvMetricOut` declares a `lines` field, so `model_validate(row)` on the ORM
    object would read the lazy relationship *during validation* — inside
    pydantic's synchronous path, which raises MissingGreenlet and 500s the
    request. Assigning `out.lines` afterwards is too late: validation has
    already touched the attribute. `db.refresh()` does not help either, because
    it reloads columns, not relationships.

    So the DTO is built from the row's COLUMNS only, and the child lines are
    fetched with an explicit query. Every env endpoint goes through here, so the
    fix cannot be half-applied to three of the four call sites.
    """
    lines = list(
        (
            await db.execute(
                select(BrsrEnvMetricLine)
                .where(BrsrEnvMetricLine.metricId == row.id)
                # Deterministic order — the capture grid renders in this order,
                # and an unordered read makes the form jump between saves.
                .order_by(
                    BrsrEnvMetricLine.stream,
                    BrsrEnvMetricLine.categoryCode,
                    BrsrEnvMetricLine.flowType,
                    BrsrEnvMetricLine.destination,
                )
            )
        ).scalars()
    )
    columns = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    out = EnvMetricOut.model_validate(columns)
    out.lines = [EnvLineOut.model_validate(x) for x in lines]
    return out


def _totals_out(t: brsr_env.EnvTotals) -> EnvTotalsOut:
    return EnvTotalsOut(
        sitesReporting=t.siteCount,
        energyTotalGj=t.energyTotalGj,
        energyRenewableGj=t.energyRenewableGj,
        energyNonRenewableGj=t.energyNonRenewableGj,
        waterWithdrawnKl=t.waterWithdrawnKl,
        waterDischargedKl=t.waterDischargedKl,
        waterConsumedKl=t.waterConsumedKl,
        waterRecycledKl=t.waterRecycledKl,
        scope1TCo2e=t.scope1TCo2e,
        scope2TCo2e=t.scope2TCo2e,
        scope3TCo2e=t.scope3TCo2e,
        wasteGeneratedT=t.wasteGeneratedT,
        wasteRecoveredT=t.wasteRecoveredT,
        wasteDisposedT=t.wasteDisposedT,
        wasteDivertedPct=t.wasteDivertedPct,
        turnoverInr=t.turnoverInr,
        unresolvedEmissionLines=t.unresolvedEmissionLines,
    )


# ── Cycles ──────────────────────────────────────────────────────────────────


@router.get("/cycles", response_model=list[CycleOut])
async def list_cycles(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CycleOut]:
    await require_permission_with_context("BRSR.READ", user, db)
    rows = list(
        (
            await db.execute(
                select(BrsrReportingCycle)
                .where(BrsrReportingCycle.isDeleted.is_(False))
                # Registers sort newest-first, platform-wide.
                .order_by(BrsrReportingCycle.createdAt.desc())
            )
        ).scalars()
    )
    return [CycleOut.model_validate(r) for r in rows]


@router.post("/cycles", response_model=CycleOut, status_code=status.HTTP_201_CREATED)
async def create_cycle(
    payload: CycleCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CycleOut:
    """Open a reporting cycle and its nine principle rows."""
    await require_permission_with_context("BRSR.CREATE", user, db)

    clash = (
        await db.execute(
            select(BrsrReportingCycle).where(
                BrsrReportingCycle.financialYear == payload.financialYear,
                BrsrReportingCycle.isDeleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A BRSR cycle for {payload.financialYear} already exists.",
        )
    if payload.periodEnd <= payload.periodStart:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "periodEnd must be after periodStart"
        )

    cycle = BrsrReportingCycle(
        **payload.model_dump(exclude={"stockExchangeCodes"}),
        stockExchangeCodes=payload.stockExchangeCodes,
        createdBy=user.id,
    )
    db.add(cycle)
    await db.flush()
    await _ensure_principle_rows(db, cycle, user.id)
    await brsr_completion.recompute(db, cycle)
    await db.commit()
    await db.refresh(cycle)
    return CycleOut.model_validate(cycle)


@router.get("/cycles/{cycle_id}", response_model=CycleOut)
async def get_cycle(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CycleOut:
    await require_permission_with_context("BRSR.READ", user, db)
    return CycleOut.model_validate(await _get_cycle(db, cycle_id))


@router.patch("/cycles/{cycle_id}", response_model=CycleOut)
async def update_cycle(
    cycle_id: str,
    payload: CycleUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CycleOut:
    await require_permission_with_context("BRSR.UPDATE", user, db)
    cycle = await _get_cycle(db, cycle_id)
    _assert_writable(cycle)

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(cycle, key, value)
    cycle.updatedBy = user.id
    await db.commit()
    await db.refresh(cycle)
    return CycleOut.model_validate(cycle)


@router.post("/cycles/{cycle_id}/transition", response_model=CycleOut)
async def transition_cycle(
    cycle_id: str,
    payload: CycleTransition,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CycleOut:
    """Advance a cycle's status, freezing the report on the move to FILED."""
    cycle = await _get_cycle(db, cycle_id)

    target = payload.status
    if target not in CYCLE_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown status {target}")
    allowed = _ALLOWED_TRANSITIONS.get(cycle.status, ())
    if target not in allowed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot move a {cycle.status} cycle to {target}. "
            f"Allowed from here: {', '.join(allowed) or 'none'}.",
        )

    # APPROVE and FILE are separate authorities from ordinary editing.
    perm = "BRSR.APPROVE" if target in (CYCLE_APPROVED, CYCLE_FILED) else "BRSR.UPDATE"
    await require_permission_with_context(perm, user, db)

    now = datetime.now(timezone.utc)

    if target == CYCLE_APPROVED:
        # Refuse to approve a disclosure whose auto-populated figures nobody has
        # confirmed. This is the whole point of tracking provenance — without
        # this gate it is decoration.
        unverified = await brsr_completion.unverified_auto_values(db, cycle.id)
        if unverified:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{len(unverified)} auto-populated indicator(s) have not been verified. "
                "Every platform-derived figure must be confirmed by a person before "
                "the disclosure can be approved.",
            )
        cycle.approvedById = user.id
        cycle.approvedAt = now

    if target == CYCLE_FILED:
        if not payload.filingReference:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "filingReference is required when marking a cycle FILED — record what was "
                "filed through the entity's own SEBI process.",
            )
        # Freeze BEFORE flipping the status: assemble() reads live rows, and
        # get_report() switches to the snapshot the moment status is FILED.
        await brsr_report.freeze(db, cycle)
        cycle.filedById = user.id
        cycle.filedAt = now
        cycle.filingReference = payload.filingReference

    cycle.status = target
    cycle.updatedBy = user.id
    if payload.notes:
        cycle.notes = payload.notes
    await db.commit()
    await db.refresh(cycle)
    return CycleOut.model_validate(cycle)


@router.get("/cycles/{cycle_id}/dashboard", response_model=CycleDashboardOut)
async def cycle_dashboard(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CycleDashboardOut:
    """Completion across all nine principles, plus environmental coverage."""
    await require_permission_with_context("BRSR.READ", user, db)
    cycle = await _get_cycle(db, cycle_id)

    responses = await _ensure_principle_rows(db, cycle, user.id)
    await brsr_completion.recompute(db, cycle)
    await db.commit()
    # MUST refresh before serialising. `recompute` dirties the cycle, so the
    # UPDATE fires `updatedAt`'s onupdate=func.now() — an SQL-side expression
    # whose new value is NOT in memory afterwards. Pydantic's validation is
    # synchronous, so reading the stale attribute there raises MissingGreenlet
    # and the endpoint 500s. See the warning in models/_base.TimestampMixin.
    # (`expire_on_commit=False` does not save us: this column is expired by the
    # flush itself, not by the commit.)
    await db.refresh(cycle)

    totals = await brsr_env.load_totals(db, cycle.id)
    unverified = await brsr_completion.unverified_auto_values(db, cycle.id)

    sites_expected = len(
        list((await db.execute(select(Plant.id))).scalars())
    )

    return CycleDashboardOut(
        cycle=CycleOut.model_validate(cycle),
        principles=[_progress(r) for r in responses],
        environmental=_totals_out(totals),
        unverifiedAutoCount=len(unverified),
        sitesExpected=sites_expected,
        sitesReported=totals.siteCount,
    )


@router.get("/trend", response_model=list[TrendPointOut])
async def trend(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TrendPointOut]:
    """Year-over-year series. Empty until a second cycle exists.

    Intensities are None where the denominator is absent rather than zero — a
    zero would plot as a real datapoint and misrepresent the year.
    """
    await require_permission_with_context("BRSR.READ", user, db)
    cycles = list(
        (
            await db.execute(
                select(BrsrReportingCycle)
                .where(BrsrReportingCycle.isDeleted.is_(False))
                .order_by(BrsrReportingCycle.periodStart.asc())
            )
        ).scalars()
    )
    out: list[TrendPointOut] = []
    for cycle in cycles:
        t = await brsr_env.load_totals(db, cycle.id)
        turnover = t.turnoverInr or None
        out.append(
            TrendPointOut(
                financialYear=cycle.financialYear,
                status=cycle.status,
                energyTotalGj=t.energyTotalGj or None,
                scope1TCo2e=t.scope1TCo2e or None,
                scope2TCo2e=t.scope2TCo2e or None,
                waterConsumedKl=t.waterConsumedKl or None,
                wasteGeneratedT=t.wasteGeneratedT or None,
                turnoverInr=turnover,
                energyIntensity=(t.energyTotalGj / turnover) if turnover else None,
                emissionsIntensity=(
                    (t.scope1TCo2e + t.scope2TCo2e) / turnover if turnover else None
                ),
                waterIntensity=(t.waterConsumedKl / turnover) if turnover else None,
            )
        )
    return out


# ── Principles + indicator values ───────────────────────────────────────────


@router.get("/cycles/{cycle_id}/principles", response_model=list[PrincipleResponseOut])
async def list_principles(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PrincipleResponseOut]:
    await require_permission_with_context("BRSR.READ", user, db)
    cycle = await _get_cycle(db, cycle_id)
    rows = await _ensure_principle_rows(db, cycle, user.id)
    await db.commit()
    out = []
    for r in rows:
        item = PrincipleResponseOut.model_validate(r)
        item.title = brsr_report.PRINCIPLE_TITLES.get(r.principle)
        item.manualPendingIndicators = max(0, r.totalIndicators - r.answeredIndicators)
        out.append(item)
    return out


@router.get(
    "/cycles/{cycle_id}/principles/{principle}", response_model=PrincipleDetailOut
)
async def get_principle(
    cycle_id: str,
    principle: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PrincipleDetailOut:
    """One principle with every indicator and its value + provenance.

    This is the drill-through screen's payload: `sourceRecordRefs` on each
    auto-populated value is what the UI expands into a list of source records.
    """
    await require_permission_with_context("BRSR.READ", user, db)
    if principle not in PRINCIPLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown principle {principle}")
    cycle = await _get_cycle(db, cycle_id)
    await _ensure_principle_rows(db, cycle, user.id)
    await db.commit()

    resp = (
        await db.execute(
            select(BrsrPrincipleResponse).where(
                BrsrPrincipleResponse.cycleId == cycle.id,
                BrsrPrincipleResponse.principle == principle,
            )
        )
    ).scalar_one()

    indicators = sorted(
        (
            await db.execute(
                select(BrsrIndicator).where(
                    BrsrIndicator.isActive.is_(True),
                    BrsrIndicator.section == SECTION_C,
                    BrsrIndicator.principle == principle,
                )
            )
        )
        .scalars()
        .all(),
        key=lambda i: (i.displayOrder, i.code),
    )
    values = {
        v.indicatorCode: v
        for v in (
            await db.execute(
                select(BrsrIndicatorValue).where(BrsrIndicatorValue.cycleId == cycle.id)
            )
        ).scalars()
    }

    def _pair(i: BrsrIndicator) -> IndicatorWithValue:
        v = values.get(i.code)
        return IndicatorWithValue(
            indicator=IndicatorOut.model_validate(i),
            value=IndicatorValueOut.model_validate(v) if v else None,
        )

    out = PrincipleResponseOut.model_validate(resp)
    out.title = brsr_report.PRINCIPLE_TITLES.get(principle)
    out.manualPendingIndicators = max(0, resp.totalIndicators - resp.answeredIndicators)

    return PrincipleDetailOut(
        response=out,
        essentialIndicators=[
            _pair(i) for i in indicators if i.indicatorClass == INDICATOR_ESSENTIAL
        ],
        leadershipIndicators=[
            _pair(i) for i in indicators if i.indicatorClass == INDICATOR_LEADERSHIP
        ],
    )


@router.patch(
    "/cycles/{cycle_id}/principles/{principle}", response_model=PrincipleResponseOut
)
async def update_principle(
    cycle_id: str,
    principle: str,
    payload: PrincipleResponseUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PrincipleResponseOut:
    await require_permission_with_context("BRSR.UPDATE", user, db)
    cycle = await _get_cycle(db, cycle_id)
    _assert_writable(cycle)

    resp = (
        await db.execute(
            select(BrsrPrincipleResponse).where(
                BrsrPrincipleResponse.cycleId == cycle.id,
                BrsrPrincipleResponse.principle == principle,
            )
        )
    ).scalar_one_or_none()
    if resp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Principle response not found")

    data = payload.model_dump(exclude_unset=True)
    if data.get("status") == "REVIEWED":
        await require_permission_with_context("BRSR.APPROVE", user, db)
        resp.reviewedById = user.id
        resp.reviewedAt = datetime.now(timezone.utc)
    for key, value in data.items():
        setattr(resp, key, value)
    resp.updatedBy = user.id
    await db.commit()
    await db.refresh(resp)

    out = PrincipleResponseOut.model_validate(resp)
    out.title = brsr_report.PRINCIPLE_TITLES.get(principle)
    out.manualPendingIndicators = max(0, resp.totalIndicators - resp.answeredIndicators)
    return out


@router.get("/indicators", response_model=list[IndicatorOut])
async def list_indicators(
    section: str | None = Query(None),
    principle: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[IndicatorOut]:
    """The seeded SEBI catalogue. Renders the manual forms for P2/P4/P7/P8."""
    await require_permission_with_context("BRSR.READ", user, db)
    stmt = select(BrsrIndicator).where(BrsrIndicator.isActive.is_(True))
    if section:
        stmt = stmt.where(BrsrIndicator.section == section)
    if principle:
        stmt = stmt.where(BrsrIndicator.principle == principle)
    rows = sorted(
        (await db.execute(stmt)).scalars().all(),
        key=lambda i: (i.section, i.principle or "", i.displayOrder, i.code),
    )
    return [IndicatorOut.model_validate(r) for r in rows]


@router.put(
    "/cycles/{cycle_id}/values/{indicator_code}", response_model=IndicatorValueOut
)
async def write_value(
    cycle_id: str,
    indicator_code: str,
    payload: IndicatorValueWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IndicatorValueOut:
    """Enter a figure manually, or override an auto-populated one.

    The server sets provenance; the client never does. Overriding an AUTO value
    demands a reason and freezes the platform's number onto the row first, so
    the disagreement stays reviewable after the fact.
    """
    await require_permission_with_context("BRSR.UPDATE", user, db)
    cycle = await _get_cycle(db, cycle_id)
    _assert_writable(cycle)

    indicator = (
        await db.execute(
            select(BrsrIndicator).where(
                BrsrIndicator.code == indicator_code, BrsrIndicator.isActive.is_(True)
            )
        )
    ).scalar_one_or_none()
    if indicator is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown indicator {indicator_code}")

    row = (
        await db.execute(
            select(BrsrIndicatorValue).where(
                BrsrIndicatorValue.cycleId == cycle.id,
                BrsrIndicatorValue.indicatorCode == indicator_code,
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    was_auto = row is not None and row.provenance == PROVENANCE_AUTO

    if was_auto and not payload.notApplicable:
        reason = (payload.overrideReason or "").strip()
        if len(reason) < MIN_OVERRIDE_REASON_CHARS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"This figure was derived from platform data. Overriding it requires a "
                f"justification of at least {MIN_OVERRIDE_REASON_CHARS} characters.",
            )
        # Freeze what the platform said, before overwriting it.
        row.autoValueNumber = row.valueNumber
        row.autoValueText = row.valueText
        row.overrideReason = reason
        row.overriddenById = user.id
        row.overriddenAt = now

    if row is None:
        principle_response_id = None
        if indicator.principle:
            resp = (
                await db.execute(
                    select(BrsrPrincipleResponse).where(
                        BrsrPrincipleResponse.cycleId == cycle.id,
                        BrsrPrincipleResponse.principle == indicator.principle,
                    )
                )
            ).scalar_one_or_none()
            principle_response_id = resp.id if resp else None
        row = BrsrIndicatorValue(
            cycleId=cycle.id,
            principleResponseId=principle_response_id,
            indicatorCode=indicator_code,
            createdBy=user.id,
        )
        db.add(row)

    if payload.notApplicable:
        reason = (payload.notApplicableReason or "").strip()
        if not reason:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "notApplicableReason is required when marking an indicator not applicable. "
                "An unexplained N/A on a mandatory SEBI indicator is a finding waiting "
                "to happen.",
            )
        row.provenance = PROVENANCE_NOT_APPLICABLE
        row.notApplicableReason = reason
        row.valueNumber = None
        row.valueText = None
        row.valueBoolean = None
        row.valueJson = None
    else:
        row.provenance = PROVENANCE_AUTO_OVERRIDDEN if was_auto else PROVENANCE_MANUAL
        row.notApplicableReason = None
        row.valueNumber = payload.valueNumber
        row.valueText = payload.valueText
        row.valueBoolean = payload.valueBoolean
        row.valueJson = payload.valueJson
        row.unit = payload.unit or indicator.unit

    if payload.evidenceNote is not None:
        row.evidenceNote = payload.evidenceNote
    # A human-entered figure is self-verified — the person entering it IS the
    # verification. Only auto-populated numbers need a separate confirmation.
    row.isVerified = True
    row.verifiedById = user.id
    row.verifiedAt = now
    row.updatedBy = user.id

    await db.flush()
    await brsr_completion.recompute(db, cycle)
    await db.commit()
    await db.refresh(row)
    return IndicatorValueOut.model_validate(row)


@router.post(
    "/cycles/{cycle_id}/values/{indicator_code}/verify", response_model=IndicatorValueOut
)
async def verify_value(
    cycle_id: str,
    indicator_code: str,
    payload: IndicatorVerify,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> IndicatorValueOut:
    """Confirm an auto-populated figure. Gates approval of the cycle."""
    await require_permission_with_context("BRSR.UPDATE", user, db)
    cycle = await _get_cycle(db, cycle_id)
    _assert_writable(cycle)

    row = (
        await db.execute(
            select(BrsrIndicatorValue).where(
                BrsrIndicatorValue.cycleId == cycle.id,
                BrsrIndicatorValue.indicatorCode == indicator_code,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No value recorded for this indicator")

    row.isVerified = payload.isVerified
    row.verifiedById = user.id if payload.isVerified else None
    row.verifiedAt = datetime.now(timezone.utc) if payload.isVerified else None
    if payload.evidenceNote is not None:
        row.evidenceNote = payload.evidenceNote
    row.updatedBy = user.id
    await db.commit()
    await db.refresh(row)
    return IndicatorValueOut.model_validate(row)


# ── Mapping engine ──────────────────────────────────────────────────────────


@router.post("/cycles/{cycle_id}/sweep", response_model=SweepResultOut)
async def run_sweep(
    cycle_id: str,
    principle: str | None = Query(None, description="Limit the run to one principle."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SweepResultOut:
    """Auto-populate from platform data. Never overwrites a human-entered figure."""
    await require_permission_with_context("BRSR.UPDATE", user, db)
    cycle = await _get_cycle(db, cycle_id)
    _assert_writable(cycle)

    await _ensure_principle_rows(db, cycle, user.id)
    result = await brsr_mapping.sweep(db, cycle, only_principle=principle, actor_id=user.id)
    completion = await brsr_completion.recompute(db, cycle)
    await db.commit()

    return SweepResultOut(
        populated=result.populated,
        skippedManual=result.skippedManual,
        noData=result.noData,
        failed=result.failed,
        misconfigured=result.misconfigured,
        completion=completion,
    )


# ── Environmental capture ───────────────────────────────────────────────────


@router.get("/env/slots", response_model=list[EnvSlotOut])
async def env_slots(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EnvSlotOut]:
    """Every capture slot the BRSR taxonomy defines — the form's skeleton."""
    await require_permission_with_context("BRSR.READ", user, db)
    return [EnvSlotOut(**s) for s in brsr_env.blank_slots()]


@router.get("/env/factors", response_model=list[EmissionFactorOut])
async def env_factors(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EmissionFactorOut]:
    await require_permission_with_context("BRSR.READ", user, db)
    rows = list(
        (
            await db.execute(
                select(BrsrEmissionFactor).where(BrsrEmissionFactor.isActive.is_(True))
            )
        ).scalars()
    )
    return [EmissionFactorOut.model_validate(r) for r in rows]


@router.get("/cycles/{cycle_id}/env", response_model=list[EnvMetricOut])
async def list_env_metrics(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EnvMetricOut]:
    await require_permission_with_context("BRSR.READ", user, db)
    cycle = await _get_cycle(db, cycle_id)
    rows = list(
        (
            await db.execute(
                select(BrsrEnvironmentalMetric)
                .where(
                    BrsrEnvironmentalMetric.cycleId == cycle.id,
                    BrsrEnvironmentalMetric.isDeleted.is_(False),
                )
                .order_by(BrsrEnvironmentalMetric.createdAt.desc())
            )
        ).scalars()
    )
    return [await _env_metric_out(db, row) for row in rows]


@router.post(
    "/cycles/{cycle_id}/env", response_model=EnvMetricOut, status_code=status.HTTP_201_CREATED
)
async def create_env_metric(
    cycle_id: str,
    payload: EnvMetricCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EnvMetricOut:
    """Open a facility's environmental submission for a period."""
    await require_permission_with_context(
        "BRSR.ENV_SUBMIT", user, db, plant_id=payload.siteId
    )
    cycle = await _get_cycle(db, cycle_id)
    _assert_writable(cycle)

    clash = (
        await db.execute(
            select(BrsrEnvironmentalMetric).where(
                BrsrEnvironmentalMetric.cycleId == cycle.id,
                BrsrEnvironmentalMetric.siteId == payload.siteId,
                BrsrEnvironmentalMetric.periodLabel == payload.periodLabel,
                BrsrEnvironmentalMetric.isDeleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"An environmental submission already exists for this site and "
            f"{payload.periodLabel}.",
        )

    plant = (
        await db.execute(select(Plant).where(Plant.id == payload.siteId))
    ).scalar_one_or_none()
    if plant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found")

    row = BrsrEnvironmentalMetric(
        cycleId=cycle.id,
        # Denormalised so no register row ever has to render a raw Plant cuid.
        siteName=getattr(plant, "name", None),
        createdBy=user.id,
        **payload.model_dump(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return await _env_metric_out(db, row)


@router.patch("/cycles/{cycle_id}/env/{metric_id}", response_model=EnvMetricOut)
async def update_env_metric(
    cycle_id: str,
    metric_id: str,
    payload: EnvMetricUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EnvMetricOut:
    """Save the capture form: header fields and the whole line set.

    Lines are replaced wholesale rather than patched individually — the form is
    a grid the operator saves as one thing, and a per-line diff would leave a
    deleted row alive whenever a request was lost.
    """
    cycle = await _get_cycle(db, cycle_id)
    _assert_writable(cycle)

    row = (
        await db.execute(
            select(BrsrEnvironmentalMetric).where(
                BrsrEnvironmentalMetric.id == metric_id,
                BrsrEnvironmentalMetric.cycleId == cycle.id,
                BrsrEnvironmentalMetric.isDeleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Environmental submission not found")

    await require_permission_with_context(
        "BRSR.ENV_SUBMIT", user, db, plant_id=row.siteId
    )
    if row.status == ENV_VERIFIED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This submission is VERIFIED. Reopen it before editing.",
        )

    data = payload.model_dump(exclude_unset=True)
    lines = data.pop("lines", None)
    for key, value in data.items():
        setattr(row, key, value)
    row.updatedBy = user.id

    if lines is not None:
        existing = list(
            (
                await db.execute(
                    select(BrsrEnvMetricLine).where(BrsrEnvMetricLine.metricId == row.id)
                )
            ).scalars()
        )
        for old in existing:
            await db.delete(old)
        await db.flush()

        for spec in lines:
            if spec.get("quantity") is None:
                # A blank slot is not persisted — "not applicable to this site"
                # and "measured zero" are different disclosures, and only the
                # second one is a number somebody entered.
                continue
            code = spec["categoryCode"]
            flow = spec.get("flowType") or "NA"
            dest = spec.get("destination") or "NA"
            if not brsr_taxonomy.is_valid_slot(code, flow, dest):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Not a valid BRSR capture slot: {code} / {flow} / {dest}",
                )
            cat = brsr_taxonomy.get(code)
            line = BrsrEnvMetricLine(
                metricId=row.id,
                stream=spec["stream"],
                categoryCode=code,
                categoryLabel=cat.label if cat else None,
                flowType=flow,
                destination=dest,
                treatmentLevel=spec.get("treatmentLevel"),
                quantity=spec.get("quantity"),
                unit=spec.get("unit") or (cat.unit if cat else None),
                dataQuality=spec.get("dataQuality"),
                evidenceNote=spec.get("evidenceNote"),
                notes=spec.get("notes"),
                createdBy=user.id,
            )
            await brsr_env.compute_line_emission(db, line, as_of=cycle.periodEnd)
            db.add(line)

    await db.commit()
    await db.refresh(row)
    return await _env_metric_out(db, row)


@router.post("/cycles/{cycle_id}/env/{metric_id}/status", response_model=EnvMetricOut)
async def set_env_status(
    cycle_id: str,
    metric_id: str,
    target: str = Query(..., description="DRAFT | SUBMITTED | VERIFIED"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EnvMetricOut:
    """Submit or verify a facility submission, and refresh Facilities from it.

    Crossing into SUBMITTED / VERIFIED is what makes the data visible to the
    disclosure totals AND recomputes the derived `FactoryEnvPeriod` row the
    Facilities ESG tab reads.
    """
    if target not in ENV_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown status {target}")

    cycle = await _get_cycle(db, cycle_id)
    _assert_writable(cycle)

    row = (
        await db.execute(
            select(BrsrEnvironmentalMetric).where(
                BrsrEnvironmentalMetric.id == metric_id,
                BrsrEnvironmentalMetric.cycleId == cycle.id,
                BrsrEnvironmentalMetric.isDeleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Environmental submission not found")

    # Verifying is a different authority from submitting — the point of the
    # step is that a second person looks.
    perm = "BRSR.ENV_VERIFY" if target == ENV_VERIFIED else "BRSR.ENV_SUBMIT"
    await require_permission_with_context(perm, user, db, plant_id=row.siteId)

    now = datetime.now(timezone.utc)
    if target == ENV_SUBMITTED:
        row.submittedById = user.id
        row.submittedAt = now
    elif target == ENV_VERIFIED:
        row.verifiedById = user.id
        row.verifiedAt = now
    elif target == ENV_DRAFT:
        row.verifiedById = None
        row.verifiedAt = None

    row.status = target
    row.updatedBy = user.id
    await db.flush()

    # Keep Facilities in step. A failure here must not lose the submission —
    # the BRSR row is the source of truth and the derivation can be re-run.
    try:
        await brsr_env_rollup.derive_for_metric(db, row)
    except Exception:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Submission saved but the Facilities environmental rollup failed. "
            "Re-run the status change to retry.",
        )

    await db.commit()
    await db.refresh(row)
    return await _env_metric_out(db, row)


@router.get("/cycles/{cycle_id}/env/totals", response_model=EnvTotalsOut)
async def env_totals(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EnvTotalsOut:
    await require_permission_with_context("BRSR.READ", user, db)
    cycle = await _get_cycle(db, cycle_id)
    return _totals_out(await brsr_env.load_totals(db, cycle.id))


# ── Report ──────────────────────────────────────────────────────────────────


@router.get("/cycles/{cycle_id}/report")
async def get_report(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The assembled report — frozen if the cycle is FILED, live otherwise."""
    await require_permission_with_context("BRSR.READ", user, db)
    cycle = await _get_cycle(db, cycle_id)
    return await brsr_report.get_report(db, cycle)


@router.get("/cycles/{cycle_id}/report.pdf")
async def export_report_pdf(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """The BRSR disclosure as a PDF.

    Rendered by the shared `services/report_pdf` generator — the same one CAMS
    and ERM use — rather than a BRSR-specific one, so the latin-1 sanitisation,
    pagination and integrity footer stay in a single place. A cycle that is not
    yet FILED renders from live data and carries the PROVISIONAL watermark.
    """
    await require_permission_with_context("BRSR.EXPORT", user, db)
    cycle = await _get_cycle(db, cycle_id)
    payload = await brsr_report.get_report(db, cycle)
    pdf_bytes = render_brsr_report_pdf(payload)

    suffix = "" if cycle.status == CYCLE_FILED else "_DRAFT"
    filename = f"BRSR_{cycle.financialYear.replace(' ', '_')}{suffix}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/cycles/{cycle_id}/report/export.csv")
async def export_report_csv(
    cycle_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Structured data export — one row per indicator, provenance included.

    For filing through the entity's own SEBI process. SafeOps360 does not submit.
    """
    await require_permission_with_context("BRSR.EXPORT", user, db)
    cycle = await _get_cycle(db, cycle_id)
    payload = await brsr_report.get_report(db, cycle)
    rows = brsr_report.flatten_for_export(payload)

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    filename = f"BRSR_{cycle.financialYear.replace(' ', '_')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["router"]
