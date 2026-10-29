"""May this user see the record an agent runs on?

An agent run reads one source record (an incident, permit, HIRA entry, CAPA
or observation) and stores what it found. Holding an AGENT.*.INVOKE
permission says nothing about *which* records the user may read, so the
agent endpoints used to let anyone with the permission run an agent on — or
read the stored result for — any plant's record. The rule now: you can run
an agent on, or view its result for, exactly the records whose detail page
you could open. Each check below mirrors that module's own GET-by-id.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capa import Capa
from app.models.hira import HiraEntry, HiraStudy
from app.models.incident import Incident
from app.models.near_miss import NearMiss
from app.models.observation import Observation
from app.models.permit import Permit
from app.models.user import User
from app.models.workflow import WorkflowTask
from app.services.permissions import PermissionContext, can
from app.services.ptw_access import check_permit_access

SUPPORTED_SOURCE_MODULES = ("INCIDENT", "PTW", "HIRA", "CAPA", "OBSERVATION", "NEAR_MISS")


async def _is_observation_workflow_actor(db: AsyncSession, user_id: str, observation_id: str) -> bool:
    # Same fallback as GET /api/observations/{id}: workflow assignees may read.
    stmt = (
        select(WorkflowTask.id)
        .where(WorkflowTask.module == "OBSERVATION")
        .where(WorkflowTask.recordId == observation_id)
        .where(WorkflowTask.assignedToId == user_id)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def check_source_access(
    db: AsyncSession, user: User, source_module: str, source_record_id: str
) -> tuple[bool, str | None]:
    """(allowed, reason). Raises 404 when the record does not exist and 400
    for an unsupported module."""
    async def _can(code: str, row: Any, plant_id: str | None, record: dict[str, Any] | None = None):
        result = await can(
            db, user.id, code,
            PermissionContext(record_id=row.id, plant_id=plant_id, record=record),
        )
        return result.allowed, result.reason

    if source_module == "INCIDENT":
        row = await db.get(Incident, source_record_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source incident not found")
        return await _can("INCIDENT.READ", row, row.plantId, {"reporterId": row.reporterId})

    if source_module == "NEAR_MISS":
        row = await db.get(NearMiss, source_record_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source near miss not found")
        return await _can(
            "NEAR_MISS.READ", row, row.plantId,
            {"reporterId": row.reporterId, "actionOwnerId": row.actionOwnerId},
        )

    if source_module == "OBSERVATION":
        row = await db.get(Observation, source_record_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source observation not found")
        allowed, reason = await _can(
            "OBSERVATION.READ", row, row.plantId,
            {"observerId": row.observerId, "responsiblePersonId": row.responsiblePersonId},
        )
        if not allowed and await _is_observation_workflow_actor(db, user.id, row.id):
            return True, None
        return allowed, reason

    if source_module == "PTW":
        row = await db.get(Permit, source_record_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source permit not found")
        return await check_permit_access(db, row, user, "PTW.READ")

    if source_module == "CAPA":
        row = await db.get(Capa, source_record_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source CAPA not found")
        return await _can(
            "CAPA.READ", row, row.plantId,
            {"primaryOwnerUserId": row.primaryOwnerUserId, "raisedByUserId": row.raisedByUserId},
        )

    if source_module == "COPILOT":
        # A copilot "record" is a conversation (AgentInvocation rows sharing a
        # conversation id). Its owner may read it; so may agent auditors.
        from app.models.agent import AgentInvocation

        owner = (
            await db.execute(
                select(AgentInvocation.invokedById)
                .where(AgentInvocation.sourceModule == "COPILOT")
                .where(AgentInvocation.sourceRecordId == source_record_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if owner is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Copilot conversation not found")
        if owner == user.id:
            return True, None
        result = await can(db, user.id, "AGENT.AUDIT_VIEW", PermissionContext())
        return result.allowed, result.reason

    if source_module == "HIRA":
        row = await db.get(HiraEntry, source_record_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source HIRA entry not found")
        study = await db.get(HiraStudy, row.studyId)
        return await _can("HIRA.READ", row, study.plantId if study else None)

    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"Unsupported sourceModule {source_module!r}. Expected one of {', '.join(SUPPORTED_SOURCE_MODULES)}.",
    )


async def assert_source_access(
    db: AsyncSession, user: User, source_module: str, source_record_id: str
) -> None:
    allowed, reason = await check_source_access(db, user, source_module, source_record_id)
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, reason or "Access denied to the source record")


__all__ = ["SUPPORTED_SOURCE_MODULES", "assert_source_access", "check_source_access"]
