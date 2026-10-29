"""One place that decides whether a user may read an observation.

Observations carry no department column, so an OWN_DEPARTMENT grant (held by
department heads and supervisors) is resolved through the *observer's*
department: a department head sees the observations raised by people in their
department at their plants. The register (`GET /api/observations`) and every
record-level read use these helpers, so the two can never disagree.

Workflow assignees keep their existing fallback: anyone holding a workflow
task on the observation may read it.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import Observation
from app.models.user import User
from app.models.workflow import WorkflowTask
from app.services.permissions import PermissionContext, can


async def observation_access_record(db: AsyncSession, obs: Observation, **extra: Any) -> dict[str, Any]:
    """The record dict `can()` matches: owner fields + the observer's department."""
    department = None
    if obs.observerId:
        observer = await db.get(User, obs.observerId)
        department = observer.department if observer else None
    return {
        "observerId": obs.observerId,
        "responsiblePersonId": obs.responsiblePersonId,
        "departmentName": department,
        **extra,
    }


def observation_owner_clause(user_id: str):
    return (Observation.observerId == user_id) | (Observation.responsiblePersonId == user_id)


def observation_department_clause(department: str, _plant_ids: list[str]):
    """Observations raised by someone in `department` (register counterpart of
    the departmentName check above)."""
    members = select(User.id).where(func.lower(User.department) == department.strip().lower())
    return Observation.observerId.in_(members)


async def is_observation_workflow_actor(db: AsyncSession, user_id: str, observation_id: str) -> bool:
    stmt = (
        select(WorkflowTask.id)
        .where(WorkflowTask.module == "OBSERVATION")
        .where(WorkflowTask.recordId == observation_id)
        .where(WorkflowTask.assignedToId == user_id)
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def require_observation_read(db: AsyncSession, user: User, obs: Observation) -> None:
    """403 unless the user may read this observation (same gate as GET /{id})."""
    record = await observation_access_record(db, obs)
    result = await can(
        db, user.id, "OBSERVATION.READ",
        PermissionContext(record_id=obs.id, plant_id=obs.plantId, record=record),
    )
    if not result.allowed and not await is_observation_workflow_actor(db, user.id, obs.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or "Access denied")


__all__ = [
    "is_observation_workflow_actor",
    "observation_access_record",
    "observation_department_clause",
    "observation_owner_clause",
    "require_observation_read",
]
