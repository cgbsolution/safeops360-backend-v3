"""RCA origination + domain derivation (ERM Cross-Domain RCA).

The four origination paths (all write the same RootCauseAnalysis entity):
  A — EVENT           : exposed from an Incident (incident stays system-of-record)
  B — RISK            : opened directly on an EnterpriseRisk (no incident required)
  C — LOSS_EVENT      : opened on a LossEvent (financial/compliance/cyber loss)
  D — PROCESS_PROBLEM : opened on a shop-floor problem (BeKaizen, BeQccProject
                        or BeSip) — Business Excellence. Reusing this entity
                        rather than standing up a fourth register is what puts
                        manufacturing problems on the cause-to-risk map and in
                        the cause analytics from day one. A QCC circle's analysis
                        stage resolves through here; it has no RCA of its own.

primaryDomain is derived from the source's RiskCategory.code (both EnterpriseRisk
and LossEvent reference the shared RiskCategory taxonomy). There is no RiskDomain
enum in the schema — this maps the 10 seeded category codes onto the 8 canonical
risk domains the cross-domain analytics aggregate over.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.erm import EnterpriseRisk, RiskCategory
from app.models.erm_p2 import LossEvent
from app.models.incident import Incident
from app.models.rca import RootCauseAnalysis
from app.services.rca import normalise_rca_method

RISK_DOMAINS = [
    "OPERATIONAL", "FINANCIAL", "COMPLIANCE", "EXTERNAL",
    "REPUTATIONAL", "CYBER", "STRATEGIC", "ESG",
    # Business Excellence domains — see schemas/rca.py RiskDomain for why a
    # manufacturing problem does not file under OPERATIONAL.
    "QUALITY", "PRODUCTIVITY", "COST",
]

# RiskCategory.code (seed-erm.ts) → canonical RCA risk domain.
CATEGORY_CODE_TO_DOMAIN: dict[str, str] = {
    "STR": "STRATEGIC",
    "FIN": "FINANCIAL",
    "OPS": "OPERATIONAL",
    "CMP": "COMPLIANCE",
    "REP": "REPUTATIONAL",
    "TEC": "CYBER",
    "ESG": "ESG",
    "SCM": "OPERATIONAL",   # supply-chain disruption presents operationally
    "PPL": "OPERATIONAL",   # people/talent
    "GEO": "EXTERNAL",      # geopolitical / external macro
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _domain_for_category(db: AsyncSession, category_id: str | None) -> str:
    if not category_id:
        return "OPERATIONAL"
    cat = await db.get(RiskCategory, category_id)
    if cat is None:
        return "OPERATIONAL"
    return CATEGORY_CODE_TO_DOMAIN.get(cat.code, "OPERATIONAL")


def assert_single_origin(
    origin_type: str,
    source_event_id: str | None,
    source_risk_id: str | None,
    source_loss_event_id: str | None,
) -> None:
    """Exactly one of the three origin references must be set, and it must match
    originType (RCA-T04)."""
    present = [
        ("EVENT", source_event_id),
        ("RISK", source_risk_id),
        ("LOSS_EVENT", source_loss_event_id),
    ]
    set_count = sum(1 for _, v in present if v)
    if set_count != 1:
        raise ValueError("Exactly one origin reference must be set (event XOR risk XOR loss).")
    set_type = next(t for t, v in present if v)
    if set_type != origin_type:
        raise ValueError(f"originType={origin_type} does not match the set source reference ({set_type}).")


async def next_rca_code(db: AsyncSession) -> str:
    year = _now().year
    n = (
        await db.execute(
            select(func.count())
            .select_from(RootCauseAnalysis)
            .where(RootCauseAnalysis.rcaCode.like(f"RCA-{year}-%"))
            .execution_options(include_deleted=True)
        )
    ).scalar() or 0
    return f"RCA-{year}-{n + 1:04d}"


async def derive_primary_domain(
    db: AsyncSession,
    *,
    origin_type: str,
    source_risk_id: str | None = None,
    source_loss_event_id: str | None = None,
    source_event_id: str | None = None,  # noqa: ARG001 — events are operational
) -> str:
    if origin_type == "RISK" and source_risk_id:
        risk = await db.get(EnterpriseRisk, source_risk_id)
        if risk is None:
            raise ValueError("Source risk not found.")
        return await _domain_for_category(db, risk.categoryId)
    if origin_type == "LOSS_EVENT" and source_loss_event_id:
        loss = await db.get(LossEvent, source_loss_event_id)
        if loss is None:
            raise ValueError("Source loss event not found.")
        return await _domain_for_category(db, loss.categoryId)
    # EVENT (incident / near-miss / audit finding) — operational volume driver.
    return "OPERATIONAL"


async def create_risk_rca(
    db: AsyncSession,
    *,
    source_risk_id: str,
    title: str,
    methodology: str = "FIVE_WHY",
    narrative: str | None = None,
    occurrence_date: datetime | None = None,
    actor_id: str | None = None,
) -> RootCauseAnalysis:
    """Path B — open an RCA directly on a risk (deterioration / appetite / KRI / deep-dive)."""
    risk = await db.get(EnterpriseRisk, source_risk_id)
    if risk is None:
        raise ValueError("Source risk not found.")
    domain = await _domain_for_category(db, risk.categoryId)
    rca = RootCauseAnalysis(
        rcaCode=await next_rca_code(db),
        title=title,
        originType="RISK",
        sourceRiskId=source_risk_id,
        primaryDomain=domain,
        methodology=normalise_rca_method(methodology) or "FIVE_WHY",
        status="DRAFT",
        analysisPayload={},
        narrative=narrative,
        analystId=actor_id or risk.riskOwnerId,
        occurrenceDate=occurrence_date,
        plantId=risk.plantId,
        createdBy=actor_id,
    )
    db.add(rca)
    return rca


#: SQCDM category on the originating Kaizen → the RCA risk domain it analyses
#: under. Deliberately not "everything is OPERATIONAL": a scrap rate, a line
#: stoppage and a unit cost are three different questions, and collapsing them
#: would make the domain breakdown on the cause analytics useless to a plant.
KAIZEN_CATEGORY_TO_DOMAIN: dict[str, str] = {
    "SAFETY": "OPERATIONAL",
    "QUALITY": "QUALITY",
    "COST": "COST",
    "DELIVERY": "PRODUCTIVITY",
    "PRODUCTIVITY": "PRODUCTIVITY",
    "MORALE": "OPERATIONAL",
    "ENVIRONMENT": "ESG",
}


async def create_problem_rca(
    db: AsyncSession,
    *,
    source_problem_id: str,
    title: str | None = None,
    methodology: str = "FIVE_WHY",
    narrative: str | None = None,
    occurrence_date: datetime | None = None,
    actor_id: str | None = None,
) -> RootCauseAnalysis:
    """Path D — open an RCA on a shop-floor problem (Business Excellence).

    Idempotent: a problem that already has an analysis returns the existing one
    rather than opening a second. Two competing RCAs on one problem is how a
    plant ends up with two different root causes for the same defect and no way
    to tell which countermeasure was actually adopted.

    THREE REGISTERS, ONE PATH
    Phase 1 resolved `source_problem_id` against BeKaizen only. Phase 2 added
    QCC and SIP, and §6 of the functional scope is explicit that a circle's
    analysis stage "draws on the platform's shared RCA engine — no separate RCA
    tool or duplicate record". Rather than give QCC its own opener (which is how
    a second RCA model gets written six months later), the source is resolved
    across all three registers here. Behaviour for a Kaizen id is unchanged.

    `sourceProblemId` carries no FK, so one column addresses all three. The
    lookup order is deterministic and ids are cuids, so a collision across
    registers is not a practical concern.
    """
    # Imported here, not at module scope: this file is imported by the ERM RCA
    # router, and a deployment that has not applied the BE DDL must still be
    # able to open risk and loss RCAs.
    source = await _resolve_be_problem(db, source_problem_id)
    if source is None:
        raise ValueError("Source problem not found.")

    existing = (
        await db.execute(
            select(RootCauseAnalysis).where(
                RootCauseAnalysis.originType == "PROCESS_PROBLEM",
                RootCauseAnalysis.sourceProblemId == source_problem_id,
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing

    rca = RootCauseAnalysis(
        rcaCode=await next_rca_code(db),
        title=title or f"Root cause — {source.title}",
        originType="PROCESS_PROBLEM",
        sourceProblemId=source_problem_id,
        primaryDomain=KAIZEN_CATEGORY_TO_DOMAIN.get(source.category, "OPERATIONAL"),
        methodology=normalise_rca_method(methodology) or "FIVE_WHY",
        status="DRAFT",
        analysisPayload={},
        # Seeded with the problem statement the source record already carries, so
        # the analyst opens a populated page instead of a blank one.
        narrative=narrative or source.narrative,
        analystId=actor_id or source.analyst_id,
        occurrenceDate=occurrence_date,
        plantId=source.plantId,
        createdBy=actor_id,
    )
    db.add(rca)
    return rca


@dataclass(frozen=True)
class _BeProblemSource:
    """The four things an RCA needs from whichever BE register raised it."""

    title: str
    category: str
    narrative: str | None
    analyst_id: str | None
    plantId: str


async def _resolve_be_problem(
    db: AsyncSession, source_problem_id: str
) -> _BeProblemSource | None:
    """Find a BE record by id across Kaizen, QCC project and SIP.

    Kaizen is tried FIRST and returns before any Phase 2 table is touched, so a
    deployment that has applied the Phase 1 DDL but not Phase 2 can still open a
    Kaizen RCA. There is deliberately no try/except around the Phase 2 lookups:
    on asyncpg a failed statement aborts the whole transaction, so swallowing an
    UndefinedTable here would hand the caller a poisoned session that fails
    later, somewhere else, with an unrelated message. That is exactly how eight
    agent RCA runs were stranded in RUNNING forever. An un-applied DDL is a
    deployment error and should say so at the point it happens.
    """
    from app.models.business_excellence import BeKaizen
    from app.models.business_excellence_p2 import BeQccProject, BeSip

    kaizen = await db.get(BeKaizen, source_problem_id)
    if kaizen is not None:
        return _BeProblemSource(
            title=kaizen.title,
            category=kaizen.category,
            narrative=kaizen.problemStatement,
            analyst_id=kaizen.ownerId or kaizen.createdById,
            plantId=kaizen.plantId,
        )

    project = await db.get(BeQccProject, source_problem_id)
    if project is not None:
        return _BeProblemSource(
            title=project.title,
            category=project.category,
            narrative=project.problemStatement,
            # The circle leader owns the analysis; the creator is the fallback.
            analyst_id=project.createdById,
            plantId=project.plantId,
        )

    sip = await db.get(BeSip, source_problem_id)
    if sip is not None:
        return _BeProblemSource(
            title=sip.title,
            category=sip.category,
            narrative=sip.problemStatement or sip.scope,
            analyst_id=sip.ownerId or sip.createdById,
            plantId=sip.plantId,
        )

    return None


async def create_loss_rca(
    db: AsyncSession,
    *,
    source_loss_event_id: str,
    title: str,
    methodology: str = "FIVE_WHY",
    narrative: str | None = None,
    occurrence_date: datetime | None = None,
    actor_id: str | None = None,
) -> RootCauseAnalysis:
    """Path C — open an RCA on a loss event (the cross-domain anchor)."""
    loss = await db.get(LossEvent, source_loss_event_id)
    if loss is None:
        raise ValueError("Source loss event not found.")
    domain = await _domain_for_category(db, loss.categoryId)
    rca = RootCauseAnalysis(
        rcaCode=await next_rca_code(db),
        title=title,
        originType="LOSS_EVENT",
        sourceLossEventId=source_loss_event_id,
        primaryDomain=domain,
        methodology=normalise_rca_method(methodology) or "FIVE_WHY",
        status="DRAFT",
        analysisPayload={},
        narrative=narrative,
        analystId=actor_id or "SYSTEM",
        occurrenceDate=occurrence_date or loss.eventDate,
        plantId=loss.siteId,
        createdBy=actor_id,
    )
    db.add(rca)
    return rca


async def expose_incident_rca(
    db: AsyncSession,
    incident: Incident,
    *,
    actor_id: str | None = None,
    approve: bool = False,
) -> RootCauseAnalysis:
    """Path A — expose an incident's completed RCA as a RootCauseAnalysis. The
    incident remains system-of-record; analysisPayload is a snapshot of its
    rootCauseData (no re-entry, no parallel store). Idempotent on sourceEventId."""
    existing = (
        await db.execute(
            select(RootCauseAnalysis)
            .where(RootCauseAnalysis.originType == "EVENT")
            .where(RootCauseAnalysis.sourceEventId == incident.id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none()

    method = normalise_rca_method(incident.rootCauseMethod) or "FIVE_WHY"
    payload = dict(incident.rootCauseData or {})
    title = f"RCA — Incident {incident.number}"
    occurred = incident.occurredAt or incident.date

    if existing is not None:
        existing.methodology = method
        existing.analysisPayload = payload
        existing.narrative = incident.rootCauseSummary
        existing.occurrenceDate = occurred
        existing.updatedBy = actor_id
        if approve and existing.status != "APPROVED":
            existing.status = "APPROVED"
            existing.approverId = actor_id
            existing.approvedAt = _now()
        return existing

    rca = RootCauseAnalysis(
        rcaCode=await next_rca_code(db),
        title=title,
        originType="EVENT",
        sourceEventId=incident.id,
        primaryDomain="OPERATIONAL",
        methodology=method,
        status="APPROVED" if approve else "IN_ANALYSIS",
        analysisPayload=payload,
        narrative=incident.rootCauseSummary,
        analystId=actor_id or incident.reporterId,
        approverId=actor_id if approve else None,
        approvedAt=_now() if approve else None,
        occurrenceDate=occurred,
        plantId=incident.plantId,
        createdBy=actor_id,
    )
    db.add(rca)
    return rca


__all__ = [
    "RISK_DOMAINS",
    "CATEGORY_CODE_TO_DOMAIN",
    "assert_single_origin",
    "next_rca_code",
    "derive_primary_domain",
    "create_risk_rca",
    "create_loss_rca",
    "expose_incident_rca",
]
