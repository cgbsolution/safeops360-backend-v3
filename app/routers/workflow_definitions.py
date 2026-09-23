"""Workflow definition admin router. Mirror of /api/workflow/definitions/*.

Gated on CONFIGURATION.WORKFLOWS — only ADMIN / SYSTEM_ADMIN / CORPORATE_HSE
hold this in the default matrix. The visual workflow editor in the React app
hits these endpoints to create, edit, version, restore, toggle, and test-run
workflow definitions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.core.deps import require_permission
from app.models.user import User
from app.models.workflow import (
    StepType,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
    WorkflowInstance,
    WorkflowStep,
)
from app.schemas.workflow import (
    DefinitionCreate,
    DefinitionOut,
    DefinitionUpdate,
)

router = APIRouter(
    prefix="/api/workflow/definitions",
    tags=["workflow-admin"],
    dependencies=[Depends(require_permission("CONFIGURATION.WORKFLOWS"))],
)

_wfdef_logger = logging.getLogger(__name__)

VALID_STEP_TYPES = {"MAKER", "CHECKER", "ASSIGNEE_TASK", "VERIFIER", "CLOSURE"}


def _validate_steps(steps: list[dict[str, Any]]) -> str | None:
    if not steps:
        return "Workflow must have at least one step."
    for i, s in enumerate(steps):
        if not (s.get("name") or "").strip():
            return f"Step {i + 1} is missing a name."
        if s.get("stepType") not in VALID_STEP_TYPES:
            return f"Step {i + 1} has an unknown type: {s.get('stepType')}."
    if sum(1 for s in steps if s["stepType"] == "MAKER") != 1:
        return "Workflow must have exactly one Maker step."
    if steps[0]["stepType"] != "MAKER":
        return "The first step must be the Maker."
    # MULTIPLE CLOSURE STEPS ARE LEGITIMATE, and demanding exactly one was a
    # real defect: INCIDENT ships two — "Plant Head Final Close" gated on
    # severity LOW/MEDIUM and "Plant Head + Corporate HSE Joint Close" gated on
    # HIGH/CRITICAL. The engine executes that happily (_find_next_applicable_step
    # simply takes the first step whose condition passes), so the rule here was
    # stricter than the runtime and made a live, working workflow impossible to
    # save from the admin UI at all — every edit 400'd.
    closures = [i for i, s in enumerate(steps) if s["stepType"] == "CLOSURE"]
    if not closures:
        return "Workflow must have at least one Closure step."
    if steps[-1]["stepType"] != "CLOSURE":
        return "The last step must be a Closure."
    # Relaxing the count without adding this would trade one bad rule for a
    # silent trap: the engine picks the FIRST matching step, so an unconditional
    # Closure always wins and every Closure after it is unreachable config that
    # looks configured and never runs.
    for i in closures[:-1]:
        if not (steps[i].get("conditionExpr") or "").strip():
            return (
                f"Step {i + 1} ('{steps[i].get('name')}') is a Closure with no condition, "
                "so it would always run and make the Closure steps after it unreachable. "
                "Give it a condition, or make it the last step."
            )
    middle = any(s["stepType"] in {"CHECKER", "ASSIGNEE_TASK"} for s in steps)
    if not middle:
        return "Workflow must have at least one Checker or Assignee step between Maker and Closure."
    return None


@router.get("")
async def list_definitions(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(WorkflowDefinition)
            .options(selectinload(WorkflowDefinition.steps))
            .order_by(WorkflowDefinition.module, WorkflowDefinition.recordType, WorkflowDefinition.name)
        )
    ).scalars().all()
    return {"definitions": [DefinitionOut.model_validate(d) for d in rows]}


@router.post("")
async def create_definition(
    payload: DefinitionCreate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not payload.module or not payload.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "module and name are required")
    definition = WorkflowDefinition(
        module=payload.module,
        recordType=payload.recordType,
        name=payload.name,
        description=payload.description,
        isActive=payload.isActive,
    )
    db.add(definition)
    await db.flush()
    # Skeleton: Maker → Checker(HSE) → Closure(HSE)
    db.add_all([
        WorkflowStep(definitionId=definition.id, sequence=1, stepType=StepType.MAKER, name="Submitted by Initiator"),
        WorkflowStep(
            definitionId=definition.id,
            sequence=2,
            stepType=StepType.CHECKER,
            name="Review",
            approverRole="HSE_MANAGER",
            slaHours=24,
        ),
        WorkflowStep(
            definitionId=definition.id,
            sequence=3,
            stepType=StepType.CLOSURE,
            name="Closure",
            approverRole="HSE_MANAGER",
        ),
    ])
    await db.flush()
    # Re-load with steps so the response matches DefinitionOut
    fresh = await db.get(
        WorkflowDefinition, definition.id, options=[selectinload(WorkflowDefinition.steps)]
    )
    return {"definition": DefinitionOut.model_validate(fresh)}


@router.get("/{definition_id}")
async def get_definition(
    definition_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    definition = await db.get(
        WorkflowDefinition, definition_id, options=[selectinload(WorkflowDefinition.steps)]
    )
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    # Which Form-Engine forms run on this workflow (Part B §3.4).
    #
    # One workflow serving several forms is the confirmed architecture, not an
    # edge case — the Business Excellence registers are designed to share one.
    # Editing a workflow is therefore never a single-form act, and an editor
    # that does not say so invites someone to "fix" a step for Kaizen and
    # silently change Suggestion Scheme, OPL and Poka Yoke too.
    #
    # Tolerates the FormDefinition table being absent: this endpoint predates
    # the Form Engine and must keep working on a deployment where the form DDL
    # has not been applied.
    attached: list[dict[str, Any]] = []
    try:
        from app.models.form_engine import DEF_PUBLISHED, FormDefinition

        rows = (
            await db.execute(
                select(
                    FormDefinition.key,
                    FormDefinition.title,
                    FormDefinition.version,
                    FormDefinition.status,
                    FormDefinition.workflowRecordType,
                )
                .where(FormDefinition.workflowModule == definition.module)
                .where(FormDefinition.isDeleted.is_(False))
                .where(FormDefinition.status == DEF_PUBLISHED)
                .order_by(FormDefinition.key)
            )
        ).all()
        attached = [
            {
                "key": k,
                "title": t,
                "version": v,
                "status": st,
                "recordType": rt,
            }
            for k, t, v, st, rt in rows
        ]
    except Exception:  # noqa: BLE001
        _wfdef_logger.debug("Form engine not available; skipping attached-form lookup")

    return {
        "definition": DefinitionOut.model_validate(definition),
        "attachedForms": attached,
    }


@router.put("/{definition_id}")
async def update_definition(
    definition_id: str,
    payload: DefinitionUpdate,
    user: User = Depends(require_permission("CONFIGURATION.WORKFLOWS")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    definition = await db.get(
        WorkflowDefinition, definition_id, options=[selectinload(WorkflowDefinition.steps)]
    )
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    if payload.steps is not None:
        ordered = sorted(
            [s.model_dump() for s in payload.steps], key=lambda s: s.get("sequence", 0)
        )
        for i, s in enumerate(ordered):
            s["sequence"] = i + 1
        err = _validate_steps(ordered)
        if err:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, err)

        # Snapshot the current definition before mutation — versioning audit trail
        snapshot = {
            "name": definition.name,
            "description": definition.description,
            "module": definition.module,
            "recordType": definition.recordType,
            "isActive": definition.isActive,
            "steps": [
                {
                    "sequence": st.sequence,
                    "stepType": st.stepType.value if hasattr(st.stepType, "value") else st.stepType,
                    "name": st.name,
                    "approverRole": st.approverRole,
                    "approverField": st.approverField,
                    "approverUserId": st.approverUserId,
                    "approverGroupRoles": st.approverGroupRoles,
                    "slaHours": st.slaHours,
                    "slaUnit": st.slaUnit,
                    "escalationRole": st.escalationRole,
                    "isOptional": st.isOptional,
                    "conditionExpr": st.conditionExpr,
                    "notes": st.notes,
                    "parallelStrategy": st.parallelStrategy,
                    "slaBySeverity": st.slaBySeverity,
                }
                for st in sorted(definition.steps, key=lambda x: x.sequence)
            ],
        }
        last_version = (
            await db.execute(
                select(func.max(WorkflowDefinitionVersion.version)).where(
                    WorkflowDefinitionVersion.definitionId == definition.id
                )
            )
        ).scalar_one() or 0
        db.add(
            WorkflowDefinitionVersion(
                definitionId=definition.id,
                version=last_version + 1,
                snapshot=json.dumps(snapshot),
                editedById=user.id,
                changeNote=payload.changeNote,
            )
        )

        # Replace steps. NOTE: this mints new step ids, so any in-flight
        # instance's currentStepId / WorkflowTask.stepId stops resolving.
        # Preserve every column here — a field omitted below is erased on
        # save even though the caller never asked to change it.
        #
        # Keyed by STEP ID, not by sequence. Sequence is re-numbered densely
        # above, so a save that reordered steps would look up the previous
        # occupant of that position and graft ITS parallelStrategy /
        # slaBySeverity onto a different step — silently turning an ordinary
        # approval into a JOINT_APPROVAL, or moving a severity SLA matrix onto
        # the wrong gate. An id survives reordering; a position does not.
        preserved = {
            st.id: (st.parallelStrategy, st.slaBySeverity) for st in definition.steps
        }
        # Which fields the caller actually sent. A modern editor round-trips
        # both columns, and must be able to CLEAR one by sending null — the
        # previous `value or previous` fallback made that impossible, so a
        # JOINT_APPROVAL step could never be demoted back to a single approver.
        explicit_by_index = [s.model_fields_set for s in sorted(payload.steps, key=lambda x: x.sequence)]

        for st in list(definition.steps):
            await db.delete(st)
        await db.flush()
        for idx, s in enumerate(ordered):
            prev_parallel, prev_sla_by_sev = preserved.get(s.get("id"), (None, None))
            sent = explicit_by_index[idx] if idx < len(explicit_by_index) else set()
            parallel_strategy = (
                s.get("parallelStrategy") if "parallelStrategy" in sent else prev_parallel
            )
            sla_by_severity = (
                s.get("slaBySeverity") if "slaBySeverity" in sent else prev_sla_by_sev
            )
            db.add(
                WorkflowStep(
                    definitionId=definition.id,
                    sequence=s["sequence"],
                    stepType=StepType(s["stepType"]),
                    name=s["name"],
                    approverRole=s.get("approverRole"),
                    approverField=s.get("approverField"),
                    approverUserId=s.get("approverUserId"),
                    approverGroupRoles=s.get("approverGroupRoles"),
                    slaHours=s.get("slaHours"),
                    slaUnit=s.get("slaUnit"),
                    escalationRole=s.get("escalationRole"),
                    isOptional=s.get("isOptional", False),
                    conditionExpr=s.get("conditionExpr"),
                    notes=s.get("notes"),
                    parallelStrategy=parallel_strategy,
                    slaBySeverity=sla_by_severity,
                )
            )

    if payload.name is not None:
        definition.name = payload.name
    if payload.description is not None:
        definition.description = payload.description or None
    if payload.recordType is not None:
        definition.recordType = payload.recordType or None
    if payload.isActive is not None:
        definition.isActive = payload.isActive

    await db.flush()
    fresh = await db.get(
        WorkflowDefinition, definition.id, options=[selectinload(WorkflowDefinition.steps)]
    )
    return {"definition": DefinitionOut.model_validate(fresh)}


@router.delete("/{definition_id}")
async def delete_definition(
    definition_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    in_use = (
        await db.execute(
            select(func.count()).select_from(WorkflowInstance).where(WorkflowInstance.definitionId == definition_id)
        )
    ).scalar_one()
    if in_use > 0:
        # Soft-delete to preserve in-flight instances
        definition = await db.get(WorkflowDefinition, definition_id)
        if definition is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        definition.isActive = False
        await db.flush()
        return {"ok": True, "softDeleted": True, "instanceCount": int(in_use)}
    definition = await db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    await db.delete(definition)
    await db.flush()
    return {"ok": True}


@router.patch("/{definition_id}/toggle")
async def toggle_definition(
    definition_id: str,
    body: dict[str, bool] = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    definition = await db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    definition.isActive = bool(body.get("isActive"))
    await db.flush()
    return {"definition": {"id": definition.id, "isActive": definition.isActive}}


@router.get("/{definition_id}/versions")
async def list_versions(
    definition_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(WorkflowDefinitionVersion)
            .where(WorkflowDefinitionVersion.definitionId == definition_id)
            .order_by(WorkflowDefinitionVersion.version.desc())
        )
    ).scalars().all()
    return {
        "versions": [
            {
                "id": v.id,
                "version": v.version,
                "editedById": v.editedById,
                "changeNote": v.changeNote,
                # Frontend reads `createdAt` from this list; the DB column is
                # actually `editedAt`. Map for back-compat with the existing UI.
                "createdAt": v.editedAt,
            }
            for v in rows
        ]
    }


@router.post("/{definition_id}/restore/{version_id}")
async def restore_version(
    definition_id: str,
    version_id: str,
    user: User = Depends(require_permission("CONFIGURATION.WORKFLOWS")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    version = await db.get(WorkflowDefinitionVersion, version_id)
    if version is None or version.definitionId != definition_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    try:
        snapshot = json.loads(version.snapshot)
    except json.JSONDecodeError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Snapshot is corrupted") from e

    last_version = (
        await db.execute(
            select(func.max(WorkflowDefinitionVersion.version)).where(
                WorkflowDefinitionVersion.definitionId == definition_id
            )
        )
    ).scalar_one() or 0

    definition = await db.get(
        WorkflowDefinition, definition_id, options=[selectinload(WorkflowDefinition.steps)]
    )
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Definition not found")

    definition.name = snapshot["name"]
    definition.description = snapshot.get("description")
    definition.module = snapshot["module"]
    definition.recordType = snapshot.get("recordType")
    definition.isActive = snapshot.get("isActive", True)
    for st in list(definition.steps):
        await db.delete(st)
    await db.flush()
    for s in snapshot.get("steps", []):
        db.add(
            WorkflowStep(
                definitionId=definition.id,
                sequence=s["sequence"],
                stepType=StepType(s["stepType"]),
                name=s["name"],
                approverRole=s.get("approverRole"),
                approverField=s.get("approverField"),
                approverUserId=s.get("approverUserId"),
                approverGroupRoles=s.get("approverGroupRoles"),
                slaHours=s.get("slaHours"),
                slaUnit=s.get("slaUnit"),
                escalationRole=s.get("escalationRole"),
                isOptional=s.get("isOptional", False),
                conditionExpr=s.get("conditionExpr"),
                notes=s.get("notes"),
                parallelStrategy=s.get("parallelStrategy"),
                slaBySeverity=s.get("slaBySeverity"),
            )
        )
    db.add(
        WorkflowDefinitionVersion(
            definitionId=definition.id,
            version=last_version + 1,
            snapshot=version.snapshot,
            editedById=user.id,
            changeNote=f"Restored from v{version.version}",
        )
    )
    await db.flush()
    return {"ok": True, "restoredFromVersion": version.version, "newVersion": last_version + 1}


@router.post("/{definition_id}/test-run")
async def test_run(
    definition_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Dry-run a workflow against a sample record without creating real tasks.
    Returns the trace of which steps would fire vs skip."""
    definition = await db.get(
        WorkflowDefinition, definition_id, options=[selectinload(WorkflowDefinition.steps)]
    )
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Definition not found")

    record_data = body.get("recordData") or {}
    from app.services.workflow_engine import _evaluate_condition  # internal helper

    trace: list[dict[str, Any]] = []
    for step in sorted(definition.steps, key=lambda s: s.sequence):
        applies = _evaluate_condition(step.conditionExpr, record_data)
        trace.append(
            {
                "sequence": step.sequence,
                "stepType": step.stepType.value,
                "name": step.name,
                "status": "AUTO" if step.stepType == StepType.MAKER or step.stepType == StepType.CLOSURE else ("EXECUTED" if applies else "SKIPPED"),
                "reason": None if applies else "Step condition not met for sample record",
                "conditionExpr": step.conditionExpr,
                "approverRole": step.approverRole,
                "approverField": step.approverField,
                "slaHours": step.slaHours,
            }
        )
    return {"trace": trace, "errors": [], "evaluatedAt": datetime.now(timezone.utc).isoformat()}
