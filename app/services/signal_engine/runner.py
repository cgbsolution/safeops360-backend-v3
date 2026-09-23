"""Signal Engine — the runner (spec §4.1, §4.2).

Owns everything a rule is forbidden to do: catalog reconciliation, threshold
resolution, persistence, dedupe, expiry, and the run log.

Four properties this file exists to guarantee:

**Idempotency.** A candidate's identity is `(tenantId, ruleCode, signalKey)`.
Re-running upserts; an unchanged finding keeps its `id`, its `status` and its
`acknowledgedAt`. See the deviation note in app/models/signal_engine.py for why
`windowStart` is deliberately NOT part of that key.

**No alert fatigue.** A re-confirmed signal increments `occurrenceCount` and
moves `lastSeenAt`; it does not become new again. A DISMISSED signal is never
resurrected by a later run — a human already answered the question.

**Failure isolation.** Each rule runs in its own try/except and commits its own
results. A rule that raises records an error and leaves every other rule's
output intact — and, critically, does NOT expire its own prior signals: a run
that failed knows nothing about whether those findings still hold, and silently
expiring them would present a crash as a resolution.

**Explainability.** The threshold dict actually used is snapshotted onto every
signal it produced, so a signal stays defensible against the config it ran
under rather than the config someone changed afterwards (NFR §7).

Zero network egress: nothing in this module or anything it calls opens a socket.
The optional LLM narrative layer (spec §4.5) is Stream 5 and is gated off by
tenant config; `narrativeLLM` stays NULL until then.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.signal_engine import (
    DEFAULT_TENANT,
    LIVE_STATUSES,
    Signal,
    SignalEvidence,
    SignalRule,
    SignalRuleOverride,
    SignalRunLog,
)
from app.services.signal_engine.base import RuleContext, SignalCandidate, SignalRuleImpl
from app.services.signal_engine.registry import RULES, RULES_BY_CODE

log = logging.getLogger("safeops360.signal_engine")


def _now() -> datetime:
    """Aware UTC — for the engine's OWN timestamptz columns."""
    return datetime.now(timezone.utc)


def _now_naive() -> datetime:
    """Naive UTC — the clock handed to rules, which query Prisma-era
    `timestamp without time zone` business tables. See RuleContext.now."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _aware(dt: datetime | None) -> datetime | None:
    """Naive → aware UTC at the persistence boundary. Idempotent."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ── Catalog reconciliation ───────────────────────────────────────────────────
async def sync_rule_catalog(db: AsyncSession) -> dict[str, str]:
    """Reconcile the SignalRule table to the code registry; return code → id.

    Metadata (name/description/category/window/default thresholds) is owned by
    the code and overwritten here. `enabled` is NOT: an operator who disabled a
    rule in the admin panel must not have that decision undone by a deploy.
    """
    rows = (await db.execute(select(SignalRule))).scalars().all()
    by_code = {r.code: r for r in rows}
    out: dict[str, str] = {}

    for impl in RULES:
        row = by_code.get(impl.code)
        if row is None:
            row = SignalRule(
                code=impl.code,
                name=impl.name,
                description=impl.description,
                category=impl.category,
                ruleClass=impl.rule_class,
                defaultSeverity=impl.default_severity,
                sourceModules=list(impl.source_modules),
                windowDays=impl.window_days,
                defaultThresholds=dict(impl.default_thresholds),
                enabled=True,
            )
            db.add(row)
            await db.flush()
        else:
            row.name = impl.name
            row.description = impl.description
            row.category = impl.category
            row.ruleClass = impl.rule_class
            row.defaultSeverity = impl.default_severity
            row.sourceModules = list(impl.source_modules)
            row.windowDays = impl.window_days
            row.defaultThresholds = dict(impl.default_thresholds)
        out[impl.code] = row.id

    await db.commit()
    return out


@dataclass(frozen=True)
class _RuleConfig:
    """The per-rule config the loop needs, as PLAIN VALUES.

    Deliberately not the ORM rows. `Session.rollback()` expires every loaded
    instance regardless of `expire_on_commit`, so after one rule fails, reading
    `catalog[code].id` on the next iteration is a lazy refresh — which, inside
    async SQLAlchemy, raises `greenlet_spawn has not been called` from a plain
    attribute access that no `await` guards.

    That is not hypothetical. It is what happened in prod: XCORR-019 raised, the
    handler rolled back, and the NEXT rule's `cat.id` — evaluated OUTSIDE the
    try block — threw the greenlet error straight out of `run_signal_engine`.
    The run log never got its completion write (`rulesRun` stayed 0), and
    XCORR-020's four perfectly good findings were never persisted. Every
    scheduled run since 2026-08-13 died this way, and the engine has emitted
    exactly zero signals in its lifetime.

    Detaching the config from the session removes the failure mode by
    construction rather than by remembering not to touch an ORM attribute.
    """

    rule_id: str
    enabled: bool
    window_days: int
    thresholds: dict[str, Any]
    severity_override: str | None


async def _overrides(db: AsyncSession, tenant: str) -> dict[str, SignalRuleOverride]:
    rows = (
        await db.execute(select(SignalRuleOverride).where(SignalRuleOverride.tenantId == tenant))
    ).scalars().all()
    return {r.ruleId: r for r in rows}


async def _rule_configs(db: AsyncSession, tenant: str) -> dict[str, _RuleConfig]:
    """Read the catalog and the tenant's overrides ONCE, into detached values."""
    catalog = {r.code: r for r in (await db.execute(select(SignalRule))).scalars().all()}
    overrides = await _overrides(db, tenant)
    out: dict[str, _RuleConfig] = {}
    for impl in RULES:
        cat = catalog.get(impl.code)
        ov = overrides.get(cat.id) if cat else None
        out[impl.code] = _RuleConfig(
            rule_id=(cat.id if cat else ""),
            enabled=(
                ov.enabled if (ov and ov.enabled is not None)
                else (cat.enabled if cat else True)
            ),
            window_days=(cat.windowDays if cat else impl.window_days),
            thresholds=resolve_thresholds(impl, ov),
            severity_override=(ov.severityOverride if ov else None),
        )
    return out


def resolve_thresholds(impl: SignalRuleImpl, ov: SignalRuleOverride | None) -> dict[str, Any]:
    """Defaults from code, overlaid with the tenant's tuning. Unknown override
    keys are kept rather than dropped — a rule may read a key the base class
    never declared, and silently discarding a value an operator typed into the
    admin panel is worse than passing it through unused."""
    merged = dict(impl.default_thresholds)
    if ov and ov.thresholdJson:
        merged.update(ov.thresholdJson)
    return merged


# ── Persistence ──────────────────────────────────────────────────────────────
async def _upsert(
    db: AsyncSession,
    *,
    impl: SignalRuleImpl,
    rule_id: str,
    tenant: str,
    candidate: SignalCandidate,
    thresholds: dict[str, Any],
    severity_override: str | None,
    now: datetime,
) -> str:
    """Insert or refresh one signal. Returns "NEW" | "UPDATED" | "SUPPRESSED"."""
    existing = (
        await db.execute(
            select(Signal).where(
                Signal.tenantId == tenant,
                Signal.ruleCode == impl.code,
                Signal.signalKey == candidate.signalKey,
            )
        )
    ).scalar_one_or_none()

    severity = severity_override or candidate.severity or impl.default_severity
    narrative = impl.render_narrative(candidate)
    action = impl.render_action(candidate)
    # Optional polish, never a dependency. Off by default and a no-op without a
    # key; `narrativeTemplate` below always carries the deterministic sentence,
    # so an airgapped tenant loses nothing and a rewrite can be audited against
    # the original or switched off at any time. See services/signal_engine/narrative.py.
    narrative_llm: str | None = None
    try:
        from app.services.signal_engine.narrative import maybe_rewrite

        narrative_llm = await maybe_rewrite(
            rule_code=impl.code, deterministic=narrative,
            facts=candidate.facts, severity=severity,
        )
    except Exception as e:  # noqa: BLE001 — prose must never break detection
        log.warning("narrative rewrite failed for %s (using template): %s", impl.code, e)
    # Rules work in naive UTC (business tables); these columns are timestamptz.
    now = _aware(now)
    window_start = _aware(candidate.windowStart) or now
    window_end = _aware(candidate.windowEnd) or now
    expires_at = _aware(candidate.expiresAt)

    if existing is None:
        sig = Signal(
            tenantId=tenant,
            siteId=candidate.siteId,
            areaId=candidate.areaId,
            ruleId=rule_id,
            ruleCode=impl.code,
            signalKey=candidate.signalKey,
            severity=severity,
            status="OPEN",
            category=candidate.category or impl.category,
            confidence=candidate.confidence,
            narrativeTemplate=narrative,
            narrativeLLM=narrative_llm,
            recommendedAction=action,
            windowStart=window_start,
            windowEnd=window_end,
            thresholdSnapshot=dict(thresholds),
            computedAt=now,
            firstSeenAt=now,
            lastSeenAt=now,
            occurrenceCount=1,
            expiresAt=expires_at,
        )
        db.add(sig)
        await db.flush()
        await _write_evidence(db, sig.id, candidate)
        return "NEW"

    # A dismissal is a human answer. Keep the sighting fresh so the admin panel
    # can show it is still true, but never re-open it.
    if existing.status == "DISMISSED":
        existing.lastSeenAt = now
        existing.occurrenceCount += 1
        return "SUPPRESSED"

    existing.siteId = candidate.siteId
    existing.areaId = candidate.areaId
    existing.ruleId = rule_id
    existing.severity = severity
    existing.category = candidate.category or impl.category
    existing.confidence = candidate.confidence
    existing.narrativeTemplate = narrative
    # Only overwrite the rewrite when we actually produced one — turning the
    # flag off must not blank prose a tenant already has.
    if narrative_llm is not None:
        existing.narrativeLLM = narrative_llm
    existing.recommendedAction = action
    existing.windowStart = window_start
    existing.windowEnd = window_end
    existing.thresholdSnapshot = dict(thresholds)
    existing.computedAt = now
    existing.lastSeenAt = now
    existing.occurrenceCount += 1
    existing.expiresAt = expires_at
    # A finding that expired and has recurred is live again — and the recurrence
    # is itself information, which `occurrenceCount` preserves.
    if existing.status == "EXPIRED":
        existing.status = "OPEN"
        existing.acknowledgedAt = None
        existing.acknowledgedBy = None

    await _write_evidence(db, existing.id, candidate, replace=True)
    return "UPDATED"


async def _write_evidence(
    db: AsyncSession, signal_id: str, candidate: SignalCandidate, *, replace: bool = False
) -> None:
    """Snapshot the evidence as of THIS compute.

    On refresh the previous rows are replaced rather than appended: the evidence
    shown must be the evidence behind the currently-displayed numbers. Each
    snapshot is still frozen, so it survives later edits or soft-deletion of the
    source record — which is the property NFR §7 actually requires."""
    if replace:
        rows = (
            await db.execute(select(SignalEvidence).where(SignalEvidence.signalId == signal_id))
        ).scalars().all()
        for r in rows:
            await db.delete(r)
    for e in candidate.evidence:
        db.add(SignalEvidence(
            signalId=signal_id,
            sourceModule=e.sourceModule,
            sourceRecordId=e.sourceRecordId,
            sourceRecordRef=e.sourceRecordRef,
            weight=e.weight,
            snapshotJson=e.snapshot,
        ))


async def _expire_stale(
    db: AsyncSession, *, tenant: str, rule_code: str, live_keys: set[str], now: datetime
) -> int:
    """Expire live signals of this rule that the current run did not re-emit —
    the finding no longer holds. Only ever called for a rule that COMPLETED."""
    stale = (
        await db.execute(
            select(Signal.id).where(
                Signal.tenantId == tenant,
                Signal.ruleCode == rule_code,
                Signal.status.in_(LIVE_STATUSES),
                Signal.signalKey.notin_(live_keys) if live_keys else Signal.id.is_not(None),
            )
        )
    ).scalars().all()
    if not stale:
        return 0
    await db.execute(
        update(Signal)
        .where(Signal.id.in_(stale))
        .values(status="EXPIRED", expiresAt=now, updatedAt=now)
    )
    return len(stale)


# ── Orchestration ────────────────────────────────────────────────────────────
async def run_signal_engine(
    db: AsyncSession,
    *,
    tenant: str = DEFAULT_TENANT,
    rule_codes: list[str] | None = None,
    run_type: str = "SCHEDULED",
    trigger_event: str | None = None,
) -> dict[str, Any]:
    """Evaluate every enabled rule (or just `rule_codes`) and persist the result.

    Never raises: a run that fails wholesale still leaves a SignalRunLog row
    explaining why, because "the nightly job silently stopped running" is the
    exact failure this platform has been bitten by before.
    """
    started = _now()
    await sync_rule_catalog(db)
    # Detached plain values, read once. Nothing inside the loop touches an ORM
    # instance loaded before it — see _RuleConfig for the failure this prevents.
    configs = await _rule_configs(db, tenant)

    runlog = SignalRunLog(
        tenantId=tenant, runType=run_type, triggerEvent=trigger_event, startedAt=started
    )
    db.add(runlog)
    await db.commit()
    # Captured immediately: a per-rule `rollback()` below expires every loaded
    # instance, and re-reading an expired attribute inside async SQLAlchemy is a
    # lazy IO fault, not a refresh.
    run_id = runlog.id

    selected = [r for r in RULES if rule_codes is None or r.code in set(rule_codes)]
    emitted = updated = expired = errors = 0
    errs: dict[str, str] = {}
    detail: dict[str, Any] = {}

    for impl in selected:
        cfg = configs.get(impl.code)
        if cfg is None or not cfg.enabled:
            detail[impl.code] = {"skipped": "disabled" if cfg else "not in catalog"}
            continue

        thresholds = cfg.thresholds
        ctx = RuleContext(
            db=db,
            tenant=tenant,
            now=_now_naive(),
            thresholds=thresholds,
            window_days=cfg.window_days,
        )
        try:
            candidates = await impl.evaluate(ctx)
            keys: set[str] = set()
            new_n = upd_n = 0
            for c in candidates:
                outcome = await _upsert(
                    db,
                    impl=impl,
                    rule_id=cfg.rule_id,
                    tenant=tenant,
                    candidate=c,
                    thresholds=thresholds,
                    severity_override=cfg.severity_override,
                    now=ctx.now,
                )
                keys.add(c.signalKey)
                if outcome == "NEW":
                    new_n += 1
                elif outcome == "UPDATED":
                    upd_n += 1
            exp_n = await _expire_stale(
                db, tenant=tenant, rule_code=impl.code, live_keys=keys, now=_now()
            )
            await db.commit()
            emitted += new_n
            updated += upd_n
            expired += exp_n
            detail[impl.code] = {
                "candidates": len(candidates), "new": new_n, "updated": upd_n,
                "expired": exp_n, "thresholds": thresholds, **ctx.notes,
            }
        except Exception as e:  # noqa: BLE001 — one bad rule must not sink the run
            await db.rollback()
            errors += 1
            errs[impl.code] = str(e)[:500]
            # No expiry on failure: a crashed rule knows nothing about whether
            # its prior findings still hold.
            detail[impl.code] = {"error": str(e)[:200]}
            log.warning("signal rule %s failed: %s", impl.code, e)

    finished = _now()
    # A rule that failed last leaves the session rolled back; the run log write
    # is the ONE thing that must land regardless, because "the nightly job
    # silently stopped" is the failure this platform keeps being bitten by.
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass
    row = await db.get(SignalRunLog, run_id)
    if row is not None:
        row.completedAt = finished
        row.durationMs = int((finished - started).total_seconds() * 1000)
        row.rulesRun = len(selected)
        row.signalsEmitted = emitted
        row.signalsUpdated = updated
        row.signalsExpired = expired
        row.errorCount = errors
        row.errorDetail = errs or None
        row.ruleDetail = detail
        await db.commit()

    return {
        "runId": run_id,
        "rulesRun": len(selected),
        "emitted": emitted,
        "updated": updated,
        "expired": expired,
        "errorCount": errors,
        "durationMs": int((finished - started).total_seconds() * 1000),
        "detail": detail,
    }


async def run_for_modules(
    db: AsyncSession, modules: set[str], *, tenant: str = DEFAULT_TENANT, event: str | None = None
) -> dict[str, Any]:
    """Event-triggered entry point (spec §4.1). Stream 1 wires no producers yet;
    Stream 2 calls this from the Incident / Near Miss / Audit / CAPA / PTW write
    paths so a CRITICAL correlation does not wait for the nightly batch."""
    from app.services.signal_engine.registry import rules_for_modules

    codes = [r.code for r in rules_for_modules(modules)]
    if not codes:
        return {"rulesRun": 0, "emitted": 0, "updated": 0, "expired": 0, "errorCount": 0}
    return await run_signal_engine(
        db, tenant=tenant, rule_codes=codes, run_type="EVENT_TRIGGERED", trigger_event=event
    )


__all__ = [
    "RULES_BY_CODE",
    "resolve_thresholds",
    "run_for_modules",
    "run_signal_engine",
    "sync_rule_catalog",
]
