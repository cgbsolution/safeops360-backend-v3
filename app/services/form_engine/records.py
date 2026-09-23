"""Record lifecycle: draft → submit → the platform's approval engine.

The important design point is what this file does NOT do. It does not implement
approvals, tasks, assignees, SLAs, escalation or transition history — all of
that already exists in `app/services/workflow_engine.py` and drives PTW, HIRA,
Observations, Incidents and CAPA. A FormRecord submits into that engine exactly
the way a Permit does, which means a form's approvals appear in the SAME inbox,
under the same `WorkflowTask` rows, with the same unread semantics and the same
immutable `WorkflowHistory`.

That is the whole reuse: two forms whose definitions name the same
`workflowModule` share one `WorkflowDefinition` — the requirement §8 places on
the four Business Excellence registers — and it costs no engine code, because
sharing is what `WorkflowDefinition.module` already means.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.form_engine import (
    REC_APPROVED,
    REC_DRAFT,
    REC_IN_REVIEW,
    REC_SUBMITTED,
    RECORD_LOCKED_STATUSES,
    FormDefinition,
    FormRecord,
)
from app.services.form_engine.binding import resolve as resolve_binding
from app.services.form_engine.numbering import next_reference
from app.services.form_engine.validation import ValidationError, evaluate

__all__ = [
    "RecordError",
    "apply_data",
    "submit",
    "sync_status_from_workflow",
    "is_locked",
]


class RecordError(ValueError):
    """A lifecycle violation (editing a locked record, submitting twice)."""


def is_locked(record: FormRecord) -> bool:
    return record.status in RECORD_LOCKED_STATUSES


def _now() -> datetime:
    return datetime.now(timezone.utc)


def apply_data(
    record: FormRecord,
    definition: FormDefinition,
    data: dict[str, Any] | None,
    *,
    require_all: bool,
) -> None:
    """Validate and write `data` onto `record`.

    Both halves of `evaluate()` are assigned to their own column, and the
    client's values for calculated fields never appear in either — see
    validation.evaluate() for why the split is structural rather than a rule.
    """
    if is_locked(record):
        raise RecordError(
            f"{record.referenceNo or 'This record'} is {record.status.lower()} and can no longer be edited."
        )
    # A non-native binding is refused here as well as at publish: a definition
    # could have been re-bound after this record was created.
    resolve_binding(definition.storageBinding)

    clean, computed = evaluate(definition.schemaJson, data, require_all=require_all)
    record.dataJson = clean
    record.computedJson = computed


async def submit(
    db: AsyncSession,
    *,
    record: FormRecord,
    definition: FormDefinition,
    actor_id: str,
    site_code: str | None,
) -> str | None:
    """Move a DRAFT into its approval workflow.

    Returns the WorkflowInstance id, or None when the definition declares no
    workflow (the record is complete on submit).

    Ordering matters: the reference number is assigned BEFORE the workflow is
    initiated, because the engine stamps `recordNumber` onto every task and
    history row it creates, and an inbox row reading "(no number)" is not
    recoverable after the fact.
    """
    if record.status != REC_DRAFT:
        raise RecordError(f"Only a draft can be submitted (this record is {record.status}).")

    # Re-validate with require_all=True. A draft is allowed to be incomplete;
    # a submission is not, and the client is never trusted to have checked.
    apply_data(record, definition, record.dataJson, require_all=True)

    if record.referenceNo is None and definition.numberPattern:
        record.referenceNo = await next_reference(
            db,
            definition_key=definition.key,
            pattern=definition.numberPattern,
            site_code=site_code,
        )

    record.submittedById = actor_id
    record.submittedAt = _now()
    record.status = REC_SUBMITTED

    if not definition.workflowModule:
        # No approval configured — submitted IS final. Deliberately not
        # auto-APPROVED: nobody approved it, and a register that says "approved"
        # about an unreviewed record is a compliance defect, not a convenience.
        await db.flush()
        return None

    from app.services.workflow_engine import initiate

    # The engine resolves approvers from record data (approverField), so it is
    # given the flattened field values plus the computed ones. `type` is what
    # WorkflowDefinition.recordType matches on.
    record_data: dict[str, Any] = {
        **(record.dataJson or {}),
        **(record.computedJson or {}),
        "type": definition.workflowRecordType,
        "formKey": definition.key,
        "siteId": record.siteId,
    }

    instance = await initiate(
        db,
        module=definition.workflowModule,
        record_id=record.id,
        record_number=record.referenceNo,
        record_title=definition.title,
        record_data=record_data,
        initiator_id=actor_id,
        plant_id=record.siteId,
    )
    record.workflowInstanceId = instance.id
    record.status = REC_IN_REVIEW
    await db.flush()
    return instance.id


async def sync_status_from_workflow(
    db: AsyncSession,
    *,
    record_id: str,
    instance_completed: bool,
    rejected: bool = False,
) -> None:
    """Mirror a workflow transition onto the record's coarse status.

    Called from `workflow_engine._sync_record_status` for any module backed by
    the form engine. Kept here rather than inline in the engine so the engine
    holds no knowledge of form storage — it asks the form engine to sync, the
    same way it asks PTW.
    """
    from app.models.form_engine import REC_REJECTED

    record = await db.get(FormRecord, record_id)
    if record is None:
        return
    if rejected:
        record.status = REC_REJECTED
    elif instance_completed:
        record.status = REC_APPROVED
        record.closedAt = _now()
    elif record.status in (REC_DRAFT, REC_SUBMITTED):
        record.status = REC_IN_REVIEW
    await db.flush()


def validation_error_payload(e: ValidationError) -> dict[str, Any]:
    """422 body the form renderer can anchor per-field messages against."""
    return {"detail": "The form has validation errors.", "errors": e.errors}
