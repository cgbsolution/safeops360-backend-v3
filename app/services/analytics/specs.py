"""Per-flow analytics — the declarative spec each flow contributes.

One `FlowSpec` per flow describes where its dates, states, severities and
dimensions live. The engine (engine.py) is generic over these, so adding a flow
is a spec entry, not a new analytics implementation. That is the whole point:
eight bespoke analytics screens would drift apart within two sprints.

Every column name, state vocabulary and drill-through parameter below was read
off the models and off live prod, not assumed. Where a register does NOT accept
a filter parameter, `drill_param` is None and the chart segment renders
unclickable — a link that silently ignores its filter is worse than no link,
because the reader believes they are looking at a filtered set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.audit_compliance import ComplianceAudit
from app.models.capa import Capa
from app.models.equipment import Inspection
from app.models.eai import EaiEntry, EaiStudy
from app.models.erm import EnterpriseRisk
from app.models.hira import HiraEntry, HiraStudy
from app.models.incident import Incident
from app.models.loto import LotoExecution
from app.models.moc import ChangeRequest
from app.models.near_miss import NearMiss
from app.models.observation import Observation
from app.models.permit import Permit
from app.models.training_engine import TrainingAssignment


@dataclass(frozen=True)
class Dimension:
    """One breakdown axis."""

    key: str
    label: str
    column: str
    # Resolve ids to names before display (house rule: never render a cuid).
    lookup: str | None = None  # "plant" | "area" | "user" | "risk_category"
    # Register query parameter this dimension can filter on, or None if the
    # register does not accept one.
    drill_param: str | None = None
    # Stable display order for ordinal values; anything unlisted sorts after,
    # by count. Keeps severity reading CRITICAL→LOW rather than alphabetically.
    order: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowSpec:
    key: str
    label: str
    href: str                       # register route, for drill-through
    model: type
    ref_column: str                 # human record ref — never render the id
    date_column: str                # the date the record is "of"
    status_column: str
    open_states: tuple[str, ...]    # everything else counts as closed/terminal
    terminal_states: tuple[str, ...]
    dimensions: tuple[Dimension, ...]
    plant_column: str | None = None
    area_column: str | None = None
    closed_at_column: str | None = None
    due_column: str | None = None
    owner_column: str | None = None
    status_drill_param: str | None = None
    soft_delete: bool = False
    # For flows whose rows carry no plant of their own (HIRA/EAI entries hang
    # off a study): (fk column, parent model, parent's plant column).
    plant_via: tuple[str, type, str] | None = None
    # Case-insensitive value normalisation. EnterpriseRisk.residualBand holds
    # both "HIGH" and "High" on prod; without this they render as two bars.
    upper_values: bool = True
    # What "open" MEANS for this flow, in the reader's language.
    #
    # Not decoration. EAI's open states are DRAFT and PENDING_REAPPROVAL, so its
    # Open tile reads 0 against 16 records — correct, because an ACTIVE
    # environmental aspect is in force rather than outstanding. A reader who
    # assumes "open = live" reads that zero as "we have no environmental
    # aspects", which is the opposite of the truth. The tile says what it counts.
    open_meaning: str = "records still requiring action"
    # Columns a PUBLISHED metric is derived from, as (column, metric, effect).
    # Checked for emptiness by the data-quality pass: an empty column here means
    # the metric on screen has no basis, which is a different and more dangerous
    # thing than a sparse breakdown dimension.
    metric_columns: tuple[tuple[str, str, str], ...] = ()
    notes: str = ""


_SEV = ("CRITICAL", "HIGH", "MEDIUM", "MODERATE", "LOW")

SPECS: dict[str, FlowSpec] = {
    "observation": FlowSpec(
        key="observation",
        open_meaning=(
            "observations still open, assigned or in progress"
        ),
        label="Safety Observations",
        href="/observations",
        model=Observation,
        ref_column="number",
        date_column="date",
        status_column="status",
        open_states=("OPEN", "ASSIGNED", "IN_PROGRESS"),
        terminal_states=("CLOSED",),
        plant_column="plantId",
        area_column="areaId",
        closed_at_column="closedAt",
        due_column="targetDate",
        owner_column="responsiblePersonId",
        status_drill_param="status",
        dimensions=(
            Dimension("severity", "Severity", "severity", order=_SEV),
            Dimension("type", "Act / Condition", "type"),
            Dimension("category", "Category", "category", drill_param="cat"),
            Dimension("area", "Area", "areaId", lookup="area", drill_param="area"),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
    ),
    "incident": FlowSpec(
        key="incident",
        open_meaning=(
            "incidents still reported, under investigation or awaiting CAPA"
        ),
        label="Incidents",
        href="/incidents",
        model=Incident,
        ref_column="number",
        date_column="date",
        status_column="status",
        open_states=("REPORTED", "INVESTIGATION", "CAPA_ASSIGNED"),
        terminal_states=("CLOSED",),
        plant_column="plantId",
        area_column="areaId",
        closed_at_column="closedAt",
        due_column="statutoryDeadline",
        owner_column="investigationTeamLead",
        status_drill_param="status",
        soft_delete=True,
        dimensions=(
            Dimension("type", "Incident type", "type", drill_param="type"),
            Dimension("severity", "Severity", "severity", order=_SEV),
            Dimension("area", "Area", "areaId", lookup="area"),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
    ),
    "nearmiss": FlowSpec(
        key="nearmiss",
        open_meaning=(
            "near misses still reported, under review or with actions assigned"
        ),
        label="Near Misses",
        href="/near-miss",
        model=NearMiss,
        ref_column="number",
        date_column="date",
        status_column="status",
        open_states=("REPORTED", "UNDER_REVIEW", "ACTION_ASSIGNED"),
        terminal_states=("CLOSED",),
        plant_column="plantId",
        area_column="areaId",
        closed_at_column="closedAt",
        due_column="targetDate",
        owner_column="actionOwnerId",
        status_drill_param="status",
        dimensions=(
            Dimension("potentialSeverity", "Potential severity", "potentialSeverity", order=_SEV),
            Dimension("riskLevel", "Risk level", "riskLevel", order=_SEV),
            Dimension("area", "Area", "areaId", lookup="area"),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
        # hazardCategory holds a cuid and is null on 189 of 191 prod rows, so it
        # is deliberately NOT a dimension — it would render one "Not set" bar.
        notes="hazardCategory excluded: 99% unpopulated on prod (see XCORR-019).",
    ),
    "capa": FlowSpec(
        key="capa",
        open_meaning=(
            "CAPAs not yet verified or closed"
        ),
        label="CAPA",
        href="/capa",
        model=Capa,
        ref_column="capaNumber",
        date_column="detectedAt",
        status_column="state",
        open_states=(
            "DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED",
            "ACTIONS_IN_PROGRESS", "PENDING_VERIFICATION",
        ),
        terminal_states=("CLOSED", "VERIFIED", "CANCELLED", "REJECTED", "CLOSED_RECURRED"),
        plant_column="plantId",
        closed_at_column="closedAt",
        due_column="closureTargetDate",
        owner_column="primaryOwnerUserId",
        status_drill_param="state",
        soft_delete=True,
        dimensions=(
            Dimension("severity", "Severity", "severity", drill_param="severity", order=_SEV),
            Dimension("priority", "Priority", "priority", order=("URGENT",) + _SEV),
            # No drill_param: the CAPA register's `?source=` filters on the
            # source CATEGORY code, while this dimension breaks down by source
            # TYPE code — two different vocabularies. Linking them would send
            # the reader to a register that silently matches nothing.
            Dimension("source", "Raised from", "sourceTypeCode"),
            Dimension("category", "Category", "primaryCategory"),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
    ),
    "hira": FlowSpec(
        key="hira",
        open_meaning=(
            "hazard entries in draft or awaiting re-approval — an ACTIVE or "
            "APPROVED entry is in force, not outstanding"
        ),
        label="HIRA Entries",
        href="/hira/entries",
        model=HiraEntry,
        ref_column="sequenceNumber",
        date_column="createdAt",
        status_column="status",
        open_states=("DRAFT", "PENDING_REAPPROVAL"),
        terminal_states=("ACTIVE", "APPROVED", "SUPERSEDED", "ARCHIVED"),
        area_column="areaId",
        due_column="nextReviewDue",
        plant_via=("studyId", HiraStudy, "plantId"),
        dimensions=(
            Dimension("residualRiskLevel", "Residual risk", "residualRiskLevel", order=_SEV),
            Dimension("initialRiskLevel", "Initial risk", "initialRiskLevel", order=_SEV),
            Dimension("alarpStatus", "ALARP status", "alarpStatus"),
            Dimension("area", "Area", "areaId", lookup="area"),
        ),
        # An entry's terminal state is ACTIVE/APPROVED — a live hazard entry is
        # "done" in workflow terms. Ageing therefore reads as time-since-raised
        # for DRAFT/PENDING_REAPPROVAL only, which is the real backlog.
        notes="ACTIVE/APPROVED are terminal: an approved hazard entry is complete work.",
    ),
    "eai": FlowSpec(
        key="eai",
        open_meaning=(
            "aspects awaiting approval or re-approval — an ACTIVE aspect is in "
            "force and being managed, not outstanding"
        ),
        label="Environmental Aspects",
        href="/eai",
        model=EaiEntry,
        ref_column="sequenceNumber",
        date_column="createdAt",
        status_column="status",
        open_states=("DRAFT", "PENDING_REAPPROVAL"),
        terminal_states=("ACTIVE", "APPROVED", "SUPERSEDED", "ARCHIVED"),
        area_column="areaId",
        due_column="nextReviewDue",
        plant_via=("studyId", EaiStudy, "plantId"),
        dimensions=(
            Dimension("initialImpactLevel", "Impact level", "initialImpactLevel",
                      order=("SIGNIFICANT", "MAJOR", "MODERATE", "MINOR", "LOW")),
            Dimension("residualImpactLevel", "Residual impact", "residualImpactLevel",
                      order=("SIGNIFICANT", "MAJOR", "MODERATE", "MINOR", "LOW")),
            Dimension("area", "Area", "areaId", lookup="area"),
        ),
    ),
    "moc": FlowSpec(
        key="moc",
        open_meaning=(
            "change requests not yet closed"
        ),
        label="Management of Change",
        href="/moc",
        model=ChangeRequest,
        ref_column="number",
        date_column="initiatedAt",
        status_column="status",
        open_states=(
            "draft", "submitted", "under_impact_assessment",
            "approved_pending_implementation", "implementation_in_progress",
            "implementation_complete_pending_verification",
        ),
        terminal_states=("closed_successful", "closed_unsuccessful", "cancelled", "rejected"),
        plant_column="plantId",
        closed_at_column="actualCompletionDate",
        due_column="targetCompletionDate",
        owner_column="initiatedByUserId",
        dimensions=(
            Dimension("category", "Change category", "category"),
            Dimension("classification", "Classification", "classification",
                      drill_param="classification"),
            Dimension("overallResidualRisk", "Residual risk", "overallResidualRisk", order=_SEV),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
        # MOC statuses are lowercase in the DB, unlike every other flow.
        upper_values=False,
        notes="MOC state vocabulary is lowercase on prod; values are not upper-cased.",
    ),
    "risk": FlowSpec(
        key="risk",
        open_meaning=(
            "risks not yet accepted, monitoring-only or closed"
        ),
        label="Enterprise Risks",
        # /erm/register, NOT /risk-register. This spec's model is
        # EnterpriseRisk; /risk-register is the COMBINED register and lists
        # HIRA + EAI rows, a different population entirely. While the two were
        # only linked from a sidebar the mismatch was invisible; once the
        # analytics became a TAB on the register it would have put "Enterprise
        # Risks: 41 open" one click from a list of HIRA hazards and invited the
        # reader to believe they were the same 41 rows.
        href="/erm/register",
        model=EnterpriseRisk,
        ref_column="riskCode",
        date_column="identifiedDate",
        status_column="lifecycleState",
        open_states=("DRAFT", "SUBMITTED", "ASSESSED", "TREATMENT_ACTIVE", "ESCALATED", "ACTIVE"),
        terminal_states=("MONITORING", "ACCEPTED", "CLOSED", "RETIRED"),
        plant_column="plantId",
        due_column="nextReviewDate",
        owner_column="riskOwnerId",
        soft_delete=True,
        dimensions=(
            Dimension("residualBand", "Residual band", "residualBand", order=_SEV),
            Dimension("inherentBand", "Inherent band", "inherentBand", order=_SEV),
            Dimension("orgLevel", "Org level", "orgLevel"),
            Dimension("category", "Category", "categoryId", lookup="risk_category"),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
    ),
    # ── Added in the Analytics Screen Contract rollout ───────────────────────
    # Inspection, Training and Audit had bespoke analytics screens built on
    # their own queries. Giving them specs here rather than wiring the contract
    # components onto that bespoke data is the whole point of the rollout: they
    # now get prior-period deltas, data-quality detection, the filter bar and
    # the trend chart from the same engine as every other flow, and "overdue"
    # cannot come to mean something different on Inspection than on CAPA.
    "inspection": FlowSpec(
        key="inspection",
        label="Inspections",
        href="/inspections",
        model=Inspection,
        ref_column="number",
        date_column="scheduledDate",
        status_column="status",
        open_states=("SCHEDULED", "IN_PROGRESS", "OVERDUE", "DEFERRED"),
        terminal_states=("COMPLETED", "CANCELLED", "CLOSED"),
        plant_column="plantId",
        # An inspection's scheduled date IS its target date — the whole control
        # is "was it done when it was due". Using it for both is not a shortcut;
        # any other column would be inventing a deadline the process does not have.
        closed_at_column="completedDate",
        due_column="scheduledDate",
        owner_column="inspectorId",
        status_drill_param="status",
        dimensions=(
            Dimension("result", "Result", "result"),
            # `isStatutory` exists on the TABLE but is not mapped on the ORM
            # model, so referencing it produced a dimension that was null on
            # every row — which the data-quality pass then reported, correctly
            # but uselessly, as "21 of 21 records missing is statutory". A spec
            # may only name columns the model actually maps.
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
        open_meaning="inspections not yet completed",
        metric_columns=(
            ("result", "Pass rate", "inspections with no result are excluded from the pass rate"),
        ),
        notes=(
            "An inspection's scheduled date doubles as its target date, so "
            "'overdue' here means the inspection did not happen when it was due."
        ),
    ),
    "training": FlowSpec(
        key="training",
        open_meaning=(
            "assignments not yet completed"
        ),
        label="Training Assignments",
        href="/training",
        model=TrainingAssignment,
        # No human record number exists on an assignment; the source record it
        # was raised from is the closest thing, and it is a ref rather than a
        # cuid, which is what the house rule actually requires.
        ref_column="sourceRecordRef",
        date_column="assignedAt",
        status_column="status",
        open_states=("ASSIGNED", "IN_PROGRESS", "OVERDUE", "ESCALATED"),
        terminal_states=("COMPLETED", "CANCELLED", "WAIVED", "EXPIRED"),
        plant_column="plantId",
        closed_at_column="completedAt",
        due_column="dueDate",
        owner_column="personUserId",
        soft_delete=True,
        dimensions=(
            Dimension("ruleType", "Trigger type", "ruleType"),
            Dimension("sourceModule", "Raised from", "sourceModule"),
            Dimension("mandatory", "Mandatory", "isMandatory"),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
    ),
    "audit": FlowSpec(
        key="audit",
        label="Compliance Audits",
        href="/cams/audits",
        model=ComplianceAudit,
        ref_column="auditNumber",
        date_column="scheduledDate",
        status_column="status",
        open_states=(
            "DRAFT", "SCHEDULED", "IN_PROGRESS",
            "SUBMITTED_PENDING_RESPONSE", "RESPONSE_SUBMITTED", "UNDER_REVIEW",
        ),
        terminal_states=("CLOSED", "CANCELLED"),
        plant_column="plantId",
        closed_at_column="closedAt",
        due_column="scheduledDate",
        owner_column="leadAuditorUserId",
        status_drill_param="status",
        soft_delete=True,
        dimensions=(
            Dimension("auditType", "Audit type", "auditType"),
            Dimension("industry", "Industry", "industryCode"),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
        open_meaning="audits not yet closed out",
        metric_columns=(
            ("overallCompliancePct", "Site benchmarking score",
             "audits with no compliance score are excluded from the site average, so a site "
             "can look compliant on the strength of a handful of scored audits"),
            # `score` is deliberately NOT listed: it holds a JSON scoring
            # breakdown, not a number, and an emptiness test over a JSON blob
            # answers a question nobody asked. `overallCompliancePct` is the
            # scalar the benchmarking chart actually averages.
        ),
        notes=(
            "A compliance audit's scheduled date is also its due date; an open "
            "audit past that date did not run when the programme said it would."
        ),
    ),
    # ── Added in the Analytics Navigation Reset ──────────────────────────────
    # PTW and LOTO get register workspaces with an Analytics tab like every
    # other register, so they need specs. Both are lifecycle registers with a
    # real open/closed distinction, which is what the engine models; the LOTO
    # PROCEDURE library deliberately gets no spec, because a published
    # procedure is in force rather than outstanding and "opened vs closed"
    # over a document library answers a question nobody asked.
    "ptw": FlowSpec(
        key="ptw",
        label="Work Permits",
        href="/ptw",
        model=Permit,
        ref_column="number",
        date_column="validFrom",
        status_column="status",
        # EXPIRED counts as OPEN: an expired permit still has to be closed out
        # (or withdrawn), and 37 of them sat unclosed on prod precisely because
        # nothing counted them. See ptw-expired-permit-closure-deadlock.
        open_states=(
            "DRAFT", "SUBMITTED", "APPROVED", "ISSUED", "ACTIVE", "SUSPENDED",
            "WORK_COMPLETED", "HANDBACK_INSPECTION", "EXPIRED",
            # Deprecated states the native PG enum still carries. Listed so a
            # legacy row lands in the backlog rather than reading as closed.
            "ISSUER_APPROVED", "SAFETY_APPROVED", "PLANT_HEAD_APPROVED",
        ),
        terminal_states=("CLOSED", "REJECTED", "CANCELLED"),
        plant_column="plantId",
        area_column="areaId",
        closed_at_column="closedAt",
        # validTo is the permit's validity window end — the deadline the work
        # was authorised against. An open permit past it is exactly the
        # overdue condition a permit system exists to surface.
        due_column="validTo",
        owner_column="issuerId",
        status_drill_param="status",
        soft_delete=True,
        dimensions=(
            Dimension("type", "Permit type", "type", drill_param="type"),
            Dimension("executionState", "Execution state", "executionState"),
            Dimension("closureType", "Closure path", "closureType"),
            Dimension("plant", "Plant", "plantId", lookup="plant"),
        ),
        open_meaning="permits not yet closed out or withdrawn",
        notes=(
            "A permit's validity window end doubles as its due date, so "
            "'overdue' here means the permit outlived the work it authorised "
            "without being closed."
        ),
    ),
    "loto": FlowSpec(
        key="loto",
        label="Lockout Records",
        href="/loto/executions",
        model=LotoExecution,
        ref_column="number",
        date_column="initiatedAt",
        status_column="status",
        # LotoExecution.status is stored lower_snake ("locks_applied"). The
        # engine upper-cases both sides before comparing (upper_values), so the
        # vocabulary is written here in the same shape as every other spec.
        open_states=("LOCKS_APPLIED", "VERIFIED", "WORK_IN_PROGRESS", "LOCKS_REMOVED"),
        terminal_states=("CLOSED", "ABORTED"),
        # LotoExecution carries siteId, not plantId — the column name differs,
        # the meaning does not, and plant scoping reads this one.
        plant_column="siteId",
        closed_at_column="closedAt",
        # A lockout has no scheduled deadline: locks come off when the work is
        # done. No due column, so the engine renders no overdue tile rather
        # than inventing a deadline the process does not have.
        owner_column="initiatedById",
        status_drill_param="status",
        soft_delete=True,
        dimensions=(
            Dimension("groupLockout", "Group lockout", "isGroupLockout"),
            Dimension("plant", "Site", "siteId", lookup="plant"),
        ),
        open_meaning="lockouts whose locks are not yet signed off",
        notes=(
            "A lockout is open from the moment the first lock goes on until "
            "the closure sign-off; there is no scheduled due date, so ageing "
            "reads as how long energy has been isolated."
        ),
    ),
}

FLOW_KEYS: tuple[str, ...] = tuple(SPECS)


def get_spec(key: str) -> FlowSpec | None:
    return SPECS.get(key)


__all__ = ["Dimension", "FLOW_KEYS", "FlowSpec", "SPECS", "get_spec"]
