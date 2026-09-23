"""HIRA Section 6 → universal CAPA producer (gap G18).

The `HIRA_CONTROL` CAPA source type has been seeded since the CAPA-Universal
build, but nothing ever created a CAPA against it: the consumer existed, the
producer never did. So a recommended additional control could be assigned to a
named person with a target date and then sit in the HIRA entry forever with no
SLA, no escalation, no inbox task and no appearance in the CAPA register — the
"we identified it but nobody chased it" failure the whole ALARP argument depends
on not happening.

This module closes that loop. Every recommended control that names a responsible
person and is in a live status gets exactly one universal CAPA, owned by that
person, due on the proposed implementation date. Once the proposal reaches a
terminal status the CAPA follows it.

Design notes:
  • Idempotency is keyed on (sourceTypeCode=HIRA_CONTROL, sourceReferenceId=the
    HiraEntryRecommendedControl id) — NOT on the row's `capaId` column, which is
    a foreign key to the module-local `HiraCapa` table and cannot hold a
    universal `Capa` id.
  • No CAPA without a responsible person. An unowned CAPA is worse than none —
    it lands in the register with nobody accountable and skews every open-action
    metric. QA §4.2/4 asserts exactly this.
  • Severity is taken from the entry's residual risk, because that is what the
    proposal is there to reduce.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.services.capa_spawn import spawn_capa

SOURCE_CODE = "HIRA_CONTROL"

# Proposal statuses that represent live, chaseable work.
_ACTIVE_STATUSES = {"PROPOSED", "APPROVED", "PLANNED", "IN_PROGRESS"}
# Proposal reached the end of the road — the CAPA should stop chasing it.
_ABANDONED_STATUSES = {"DEFERRED", "REJECTED"}
_DONE_STATUSES = {"IMPLEMENTED"}

# CAPA states that are already finished; never re-drive these from HIRA.
_TERMINAL_CAPA_STATES = {"VERIFIED", "CLOSED", "CLOSED_RECURRED", "CANCELLED", "REJECTED"}

# Residual risk level → (CAPA severity, priority, default due-days when the
# proposal carries no date). Mirrors the response-time intent of the matrix.
_RESIDUAL_SEVERITY_MAP = {
    "CRITICAL": ("CRITICAL", "CRITICAL", 30),
    "HIGH": ("HIGH", "HIGH", 45),
    "MODERATE": ("MODERATE", "MODERATE", 60),
    "LOW": ("LOW", "LOW", 90),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _due_days(proposed: datetime | None, fallback: int) -> int:
    """Days from now to the proposal's target date, floored at 1 so a date that
    has already passed still produces a valid (immediately overdue) CAPA rather
    than a negative closure target."""
    if proposed is None:
        return fallback
    when = proposed if proposed.tzinfo else proposed.replace(tzinfo=timezone.utc)
    return max(1, (when - _now()).days)


async def _capas_by_control_id(db: AsyncSession, control_ids: list[str]) -> dict[str, Capa]:
    """Existing HIRA_CONTROL CAPAs keyed by the proposal they came from."""
    if not control_ids:
        return {}
    rows = (
        await db.execute(
            select(Capa)
            .where(Capa.sourceTypeCode == SOURCE_CODE)
            .where(Capa.sourceReferenceId.in_(control_ids))
            .execution_options(include_deleted=True)
        )
    ).scalars().all()
    return {c.sourceReferenceId: c for c in rows if c.sourceReferenceId}


async def control_ids_with_capas(db: AsyncSession, control_ids: list[str]) -> set[str]:
    """Which of these proposals already have a universal CAPA.

    Used by the replace handler to protect a CAPA-backed row from being deleted
    by a wholesale sync — the local `capaId` guard only covers module-local
    HiraCapa rows and would have let these through.
    """
    return set((await _capas_by_control_id(db, control_ids)).keys())


async def sync_recommended_control_capas(
    db: AsyncSession,
    *,
    entry: Any,
    plant_id: str,
    controls: list[Any],
    actor_id: str | None,
) -> dict[str, Any]:
    """Reconcile the universal CAPA register against this entry's Section 6.

    Creates a CAPA for every newly-owned live proposal, keeps the owner and due
    date in step, and retires the CAPA when the proposal is implemented or
    abandoned. Caller commits.
    """
    control_ids = [c.id for c in controls if getattr(c, "id", None)]
    by_control = await _capas_by_control_id(db, control_ids)

    severity, priority, fallback_days = _RESIDUAL_SEVERITY_MAP.get(
        (entry.residualRiskLevel or "MODERATE").upper(), ("MODERATE", "MODERATE", 60)
    )

    created: list[str] = []
    updated: list[str] = []
    retired: list[str] = []

    for control in controls:
        capa = by_control.get(control.id)
        status = (control.status or "PROPOSED").upper()
        live = status in _ACTIVE_STATUSES and bool(control.responsibleId)

        if capa is None:
            # Nothing to retire, and we only mint a CAPA for an owned, live
            # proposal — an unassigned one has nobody to chase (QA §4.2/4).
            if not live:
                continue
            new_capa = await spawn_capa(
                db,
                source_code=SOURCE_CODE,
                plant_id=plant_id,
                title=f"HIRA control: {control.description[:150]}",
                problem=(
                    f"Recommended additional control from HIRA entry #{entry.sequenceNumber} "
                    f"({entry.activityDescription[:200]}). "
                    f"Residual risk {entry.residualRiskLevel or 'unrated'}"
                    f"{f' — {entry.residualRiskScore}' if entry.residualRiskScore is not None else ''}. "
                    f"Proposed control ({control.hierarchy}): {control.description[:500]}"
                ),
                ref_id=control.id,
                ref_url=f"/hira/{entry.studyId}/entries/{entry.id}",
                ref_summary=f"HIRA entry #{entry.sequenceNumber} — {control.hierarchy} control",
                metadata={
                    "hiraEntryId": entry.id,
                    "hiraStudyId": entry.studyId,
                    "recommendedControlId": control.id,
                    "controlHierarchy": control.hierarchy,
                    "controlStatus": status,
                    "residualRiskLevel": entry.residualRiskLevel,
                    "residualRiskScore": entry.residualRiskScore,
                    "targetRiskLevel": entry.targetRiskLevel,
                    "estimatedCostBand": control.estimatedCostBand,
                    "rationale": control.rationale,
                },
                severity=severity,
                priority=priority,
                detected_method="HIRA_ASSESSMENT",
                owner_id=control.responsibleId,
                actor_id=actor_id,
                due_days=_due_days(control.proposedImplementationDate, fallback_days),
            )
            created.append(new_capa.capaNumber)
            continue

        if (capa.state or "") in _TERMINAL_CAPA_STATES:
            continue

        # Keep an open CAPA in step with the proposal it mirrors.
        changed = False
        if status in _ABANDONED_STATUSES:
            capa.state = "CANCELLED"
            capa.stateChangedAt = _now()
            retired.append(capa.capaNumber)
            changed = True
        elif status in _DONE_STATUSES:
            # Implemented is not closed: the CAPA engine still owns verification
            # and effectiveness. Move it to the verification gate, don't close it.
            if capa.state != "PENDING_VERIFICATION":
                capa.state = "PENDING_VERIFICATION"
                capa.stateChangedAt = _now()
                retired.append(capa.capaNumber)
                changed = True
        else:
            if control.responsibleId and capa.primaryOwnerUserId != control.responsibleId:
                capa.primaryOwnerUserId = control.responsibleId
                changed = True
            if control.proposedImplementationDate is not None:
                target = control.proposedImplementationDate
                target = target if target.tzinfo else target.replace(tzinfo=timezone.utc)
                if capa.closureTargetDate != target:
                    capa.closureTargetDate = target
                    changed = True

        meta = dict(capa.sourceMetadata or {})
        if meta.get("controlStatus") != status:
            meta["controlStatus"] = status
            # JSON columns mutated in place do not mark the attribute dirty, so
            # the write silently no-ops. Always reassign.
            capa.sourceMetadata = meta
            changed = True
        if changed and capa.capaNumber not in retired:
            updated.append(capa.capaNumber)

    return {"created": created, "updated": updated, "retired": retired}
