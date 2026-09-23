"""Signal Engine API — cross-module signal feed, rule admin, run history.

Paths follow the house convention (`/api/signals`, not the spec's `/api/v1/...`)
— every other router on this backend is unversioned and the frontend proxy
assumes it.

Two audiences, one gate. DATA_QUALITY signals name unpopulated columns and
broken references: that is engineering's backlog, not a safety officer's, and
putting it in front of the wrong reader devalues both. So the feed silently
scopes DATA_QUALITY to admin roles rather than 403-ing — a non-admin simply
sees the safety-facing signals, which is all Stream 2/3 will emit anyway.

Stream 5 owns `POST /signals/{id}/escalate-capa` and `/enhance`. They are NOT
stubbed here: an endpoint that exists and does nothing is worse than one that
does not exist, because a caller will wire to it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.signal_engine import (
    CATEGORIES,
    DEFAULT_TENANT,
    SEVERITIES,
    STATUSES,
    Signal,
    SignalRule,
    SignalRuleOverride,
    SignalRunLog,
)
from app.models.user import User
from app.schemas.signal_engine import (
    DismissPayload,
    ResolvePayload,
    RuleOverridePatch,
    SignalListResponse,
    SignalOut,
    SignalRuleOut,
    SignalRunOut,
    SignalSummary,
)
from app.services.permissions import get_user_role_codes
from app.services.plant_directory import resolve_plant_names, site_label
from app.services.signal_engine.registry import RULES_BY_CODE

router = APIRouter(prefix="/api", tags=["signals"])

_ADMIN_ROLES = {"SYSTEM_ADMIN", "SUPER_ADMIN", "ADMIN", "PLATFORM_ADMIN"}


async def _is_admin(db: AsyncSession, user: User) -> bool:
    return bool(set(await get_user_role_codes(db, user.id)) & _ADMIN_ROLES)


async def _require_admin(db: AsyncSession, user: User) -> None:
    if not await _is_admin(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Signal Engine administration is System-Admin only.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _to_out(db: AsyncSession, rows: list[Signal], *, with_evidence: bool) -> list[SignalOut]:
    names = await resolve_plant_names(db, [r.siteId for r in rows])
    rule_names = {
        r.code: r.name
        for r in (await db.execute(select(SignalRule))).scalars().all()
    }
    out: list[SignalOut] = []
    for r in rows:
        o = SignalOut.model_validate(r)
        o.ruleName = rule_names.get(r.ruleCode)
        # From the code registry, not the catalog row: the registry is the
        # source of truth and stays right even if a catalog row is stale.
        impl = RULES_BY_CODE.get(r.ruleCode)
        o.ruleClass = impl.rule_class if impl else None
        # House rule: never render a Plant cuid. A null site is "Estate-wide",
        # which is a real scope for a platform-level data-quality finding.
        o.siteName = site_label(names, r.siteId)
        o.evidenceCount = len(r.evidence or [])
        if not with_evidence:
            o.evidence = []
        out.append(o)
    return out


@router.get("/signals", response_model=SignalListResponse)
async def list_signals(
    status_: list[str] | None = Query(default=None, alias="status"),
    severity: list[str] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    module: str | None = Query(default=None, description="Filter to signals whose rule reads this module"),
    ruleClass: list[str] | None = Query(
        default=None, description="CORRELATION | STATISTICAL | DATA_QUALITY"),
    site: str | None = Query(default=None),
    rule: str | None = Query(default=None, description="Rule code, e.g. XCORR-019"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignalListResponse:
    admin = await _is_admin(db, user)
    stmt = select(Signal).where(Signal.tenantId == DEFAULT_TENANT)

    if status_:
        stmt = stmt.where(Signal.status.in_([s.upper() for s in status_]))
    if severity:
        stmt = stmt.where(Signal.severity.in_([s.upper() for s in severity]))
    if category:
        wanted = [c.upper() for c in category]
        if not admin:
            wanted = [c for c in wanted if c != "DATA_QUALITY"]
            if not wanted:
                return SignalListResponse(signals=[], total=0, limit=limit, offset=offset)
        stmt = stmt.where(Signal.category.in_(wanted))
    elif not admin:
        stmt = stmt.where(Signal.category != "DATA_QUALITY")
    if site:
        stmt = stmt.where(Signal.siteId == site)
    if rule:
        stmt = stmt.where(Signal.ruleCode == rule)
    if module:
        codes = [c for c, r in RULES_BY_CODE.items() if module.upper() in set(r.source_modules)]
        stmt = stmt.where(Signal.ruleCode.in_(codes or ["__none__"]))
    if ruleClass:
        wanted_cls = {c.upper() for c in ruleClass}
        codes = [c for c, r in RULES_BY_CODE.items() if r.rule_class in wanted_cls]
        stmt = stmt.where(Signal.ruleCode.in_(codes or ["__none__"]))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    # Severity first, then most-recently-computed — ordered in SQL, not after
    # the LIMIT, or paging would rank each page independently and a CRITICAL on
    # page 2 would stay on page 2 forever.
    sev_order = case(
        {s: i for i, s in enumerate(SEVERITIES)}, value=Signal.severity, else_=len(SEVERITIES)
    )
    rows = (
        await db.execute(
            stmt.options(selectinload(Signal.evidence))
            .order_by(sev_order, Signal.computedAt.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    return SignalListResponse(
        signals=await _to_out(db, list(rows), with_evidence=False),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/signals/summary", response_model=SignalSummary)
async def signal_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignalSummary:
    admin = await _is_admin(db, user)
    base = select(Signal).where(Signal.tenantId == DEFAULT_TENANT)
    if not admin:
        base = base.where(Signal.category != "DATA_QUALITY")
    rows = (await db.execute(base)).scalars().all()

    by_status: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    by_mod: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.status in ("OPEN", "ACKNOWLEDGED"):
            by_sev[r.severity] = by_sev.get(r.severity, 0) + 1
            by_cat[r.category] = by_cat.get(r.category, 0) + 1
            impl = RULES_BY_CODE.get(r.ruleCode)
            if impl:
                by_class[impl.rule_class] = by_class.get(impl.rule_class, 0) + 1
            for m in (impl.source_modules if impl else ()):
                by_mod[m] = by_mod.get(m, 0) + 1
    return SignalSummary(
        byStatus=by_status,
        bySeverity=by_sev,
        byCategory=by_cat,
        byModule=by_mod,
        byRuleClass=by_class,
        openTotal=by_status.get("OPEN", 0),
    )


@router.get("/signals/{signal_id}", response_model=SignalOut)
async def get_signal(
    signal_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignalOut:
    row = (
        await db.execute(
            select(Signal).options(selectinload(Signal.evidence)).where(Signal.id == signal_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signal not found")
    if row.category == "DATA_QUALITY" and not await _is_admin(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This signal is on the platform data-quality register.")
    return (await _to_out(db, [row], with_evidence=True))[0]


@router.get("/signals/{signal_id}/evidence")
async def get_evidence(
    signal_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    sig = await get_signal(signal_id, user=user, db=db)
    return {"signalId": signal_id, "evidence": [e.model_dump() for e in sig.evidence]}


@router.post("/signals/{signal_id}/acknowledge", response_model=SignalOut)
async def acknowledge(
    signal_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignalOut:
    row = await db.get(Signal, signal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signal not found")
    if row.status in ("DISMISSED", "EXPIRED"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"A {row.status.lower()} signal cannot be acknowledged.")
    row.status = "ACKNOWLEDGED"
    row.acknowledgedBy = user.id
    row.acknowledgedAt = _now()
    await db.commit()
    return await get_signal(signal_id, user=user, db=db)


@router.post("/signals/{signal_id}/dismiss", response_model=SignalOut)
async def dismiss(
    signal_id: str,
    payload: DismissPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignalOut:
    """Dismiss with a reason. The reason is mandatory and kept: a later run will
    NOT resurrect a dismissed signal, so the record of who decided it was not
    worth acting on is the only remaining trace of that judgement."""
    row = await db.get(Signal, signal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signal not found")
    row.status = "DISMISSED"
    row.dismissedBy = user.id
    row.dismissedAt = _now()
    row.dismissedReason = payload.reason.strip()
    await db.commit()
    return await get_signal(signal_id, user=user, db=db)


@router.post("/signals/{signal_id}/resolve", response_model=SignalOut)
async def resolve(
    signal_id: str,
    payload: ResolvePayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignalOut:
    """Mark a signal ACTIONED — the condition has been dealt with.

    Deliberately different from DISMISS. Dismiss says "this is not worth acting
    on" and is never resurrected. Resolve says "we acted"; the finding stays a
    LIVE status, so if the next scan still sees the condition the signal simply
    refreshes and stays visible. That is the honest behaviour: a rule that can
    still see the problem should not be silenced by someone asserting it is
    fixed. Once the condition genuinely clears, the run expires it.
    """
    row = await db.get(Signal, signal_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signal not found")
    if row.category == "DATA_QUALITY" and not await _is_admin(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This signal is on the platform data-quality register.")
    if row.status == "DISMISSED":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This signal was dismissed. Re-open it before resolving.",
        )
    row.status = "ACTIONED"
    # Reuse the acknowledgement fields: resolving implies having seen it, and a
    # second near-identical pair of who/when columns would drift out of step.
    row.acknowledgedBy = user.id
    row.acknowledgedAt = row.acknowledgedAt or _now()
    if payload.note and payload.note.strip():
        row.dismissedReason = payload.note.strip()
    await db.commit()
    return await get_signal(signal_id, user=user, db=db)


# ── Rule administration ──────────────────────────────────────────────────────
@router.get("/signal-rules", response_model=list[SignalRuleOut])
async def list_rules(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SignalRuleOut]:
    await _require_admin(db, user)
    from app.services.signal_engine.runner import resolve_thresholds, sync_rule_catalog

    await sync_rule_catalog(db)
    rules = (await db.execute(select(SignalRule).order_by(SignalRule.code))).scalars().all()
    ovs = {
        o.ruleId: o
        for o in (
            await db.execute(
                select(SignalRuleOverride).where(SignalRuleOverride.tenantId == DEFAULT_TENANT)
            )
        ).scalars().all()
    }
    open_counts = dict(
        (
            await db.execute(
                select(Signal.ruleCode, func.count(Signal.id))
                .where(Signal.tenantId == DEFAULT_TENANT, Signal.status.in_(("OPEN", "ACKNOWLEDGED")))
                .group_by(Signal.ruleCode)
            )
        ).all()
    )

    out: list[SignalRuleOut] = []
    for r in rules:
        ov = ovs.get(r.id)
        impl = RULES_BY_CODE.get(r.code)
        o = SignalRuleOut.model_validate(r)
        o.effectiveEnabled = (ov.enabled if (ov and ov.enabled is not None) else r.enabled)
        o.effectiveThresholds = resolve_thresholds(impl, ov) if impl else r.defaultThresholds
        o.severityOverride = ov.severityOverride if ov else None
        o.openSignals = int(open_counts.get(r.code, 0))
        o.implemented = impl is not None
        out.append(o)
    return out


@router.patch("/signal-rules/{code}/override", response_model=SignalRuleOut)
async def patch_override(
    code: str,
    payload: RuleOverridePatch,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SignalRuleOut:
    """Per-tenant tuning, so an implementation team can adjust a threshold
    without an engineering ticket (spec §5.1)."""
    await _require_admin(db, user)
    if payload.severityOverride and payload.severityOverride.upper() not in SEVERITIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"severityOverride must be one of {', '.join(SEVERITIES)}")

    rule = (await db.execute(select(SignalRule).where(SignalRule.code == code))).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown rule {code}")

    impl = RULES_BY_CODE.get(code)
    if payload.thresholdJson and impl:
        unknown = set(payload.thresholdJson) - set(impl.default_thresholds)
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{code} has no threshold(s) named: {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(impl.default_thresholds))}",
            )

    ov = (
        await db.execute(
            select(SignalRuleOverride).where(
                SignalRuleOverride.tenantId == DEFAULT_TENANT, SignalRuleOverride.ruleId == rule.id
            )
        )
    ).scalar_one_or_none()
    if ov is None:
        ov = SignalRuleOverride(tenantId=DEFAULT_TENANT, ruleId=rule.id)
        db.add(ov)
    if payload.enabled is not None:
        ov.enabled = payload.enabled
    if payload.thresholdJson is not None:
        ov.thresholdJson = payload.thresholdJson
    if payload.severityOverride is not None:
        ov.severityOverride = payload.severityOverride.upper()
    ov.updatedBy = user.id
    await db.commit()

    return next(r for r in await list_rules(user=user, db=db) if r.code == code)


# ── Run history / manual run ─────────────────────────────────────────────────
@router.get("/signal-runs", response_model=list[SignalRunOut])
async def list_runs(
    limit: int = Query(default=25, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SignalRunOut]:
    await _require_admin(db, user)
    rows = (
        await db.execute(
            select(SignalRunLog)
            .where(SignalRunLog.tenantId == DEFAULT_TENANT)
            .order_by(SignalRunLog.startedAt.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [SignalRunOut.model_validate(r) for r in rows]


@router.post("/signal-runs/run")
async def run_now(
    rule: str | None = Query(default=None, description="Run a single rule code; omit for all"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require_admin(db, user)
    from app.services.signal_engine.runner import run_signal_engine

    return await run_signal_engine(
        db, rule_codes=[rule] if rule else None, run_type="MANUAL", trigger_event=f"user:{user.id}"
    )


@router.get("/signal-meta")
async def signal_meta(user: User = Depends(get_current_user)) -> dict:  # noqa: ARG001 — auth gate only
    """Controlled vocabularies, so the UI never hard-codes them."""
    return {
        "severities": list(SEVERITIES),
        "statuses": list(STATUSES),
        "categories": list(CATEGORIES),
        "rules": [
            {"code": r.code, "name": r.name, "category": r.category, "modules": list(r.source_modules)}
            for r in RULES_BY_CODE.values()
        ],
    }
