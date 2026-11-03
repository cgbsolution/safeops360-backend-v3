"""SafeOps Copilot endpoint.

  POST /api/copilot/chat   one user turn, streamed back as Server-Sent Events
  GET  /api/copilot/status whether the copilot is available to this user

Auth, licence (AI_ASSIST), rate limit and request validation all happen
before the stream opens, so failures there are ordinary HTTP errors. Inside
the stream every tool re-checks the user's permissions on its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services.ai.anthropic_client import is_configured
from app.services.copilot import form_drafts, ptw_draft
from app.services.copilot.runtime import (
    CopilotTurn,
    enforce_rate_limit,
    ensure_copilot_agent,
    stream_turn,
    validate_history,
)

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


class PageContext(BaseModel):
    path: str = Field(default="/", max_length=300)
    module: str | None = Field(default=None, max_length=40)
    recordId: str | None = Field(default=None, max_length=64)
    recordLabel: str | None = Field(default=None, max_length=120)
    # The HIRA study the user has open (its page, or one of its entries).
    studyId: str | None = Field(default=None, max_length=64)


class CopilotTurnRequest(BaseModel):
    conversationId: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    page: PageContext = Field(default_factory=PageContext)
    # Present only while the New Permit form is open.
    ptwDraft: dict[str, Any] | None = None
    # Present while the New Observation form or a HIRA entry editor is open:
    # {"kind": "OBSERVATION" | "HIRA_ENTRY", "draft": {...}}.
    formDraft: dict[str, Any] | None = None
    nowLocal: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
    timezoneOffsetMinutes: int = Field(default=0, ge=-840, le=840)


@router.get("/status")
async def copilot_status(user: User = Depends(get_current_user)) -> dict[str, Any]:
    settings = get_settings()
    return {
        "enabled": bool(settings.copilot_enabled and is_configured()),
        "reason": None if settings.copilot_enabled and is_configured()
        else ("Copilot is turned off" if not settings.copilot_enabled else "AI is not configured on this server"),
    }


@router.get("/hira-review/{entry_id}")
async def hira_entry_review(
    entry_id: str,
    preview: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The copilot's review of one HIRA entry: what is wrong, what to correct in
    the residual risk, and what to improve. Deterministic (no AI call), so the
    entry page can show it as soon as the activity is opened.

    Feature flag hira.calibration.v2 (per plant): OFF -> v1; SHADOW -> v1 shown,
    v1 and v2 logged side by side; ON -> v2. `?preview=v2` returns v2 for
    calibration managers (HIRA.LIBRARY_MANAGE) whatever the flag."""
    from app.services.agents.source_access import assert_source_access
    from app.services.hira_calibration import service as calibration
    from app.services.permissions import PermissionContext, can

    await assert_source_access(db, user, "HIRA", entry_id)
    want_preview = (preview or "").lower() == "v2"
    if want_preview:
        plant_id = await calibration.plant_of_entry(db, entry_id)
        check = await can(db, user.id, "HIRA.LIBRARY_MANAGE", PermissionContext(plant_id=plant_id))
        if not check.allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Preview of the calibrated review needs HIRA.LIBRARY_MANAGE")
    try:
        return await calibration.review(db, entry_id, user_id=user.id, preview=want_preview)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/chat")
async def copilot_chat(
    payload: CopilotTurnRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    settings = get_settings()
    if not settings.copilot_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The copilot is turned off on this deployment.")
    if not is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI is not configured on this server.")

    history = validate_history(payload.messages)
    agent, prompt = await ensure_copilot_agent(db, user.id)
    if not agent.isActive:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The copilot has been disabled by an administrator.")
    await enforce_rate_limit(db, agent, user.id)

    draft = ptw_draft.normalise_client_draft(payload.ptwDraft) if payload.ptwDraft is not None else None
    form_kind, form_draft = form_drafts.normalise_client_form(payload.formDraft)
    turn = CopilotTurn(
        user_id=user.id,
        user_plant_id=user.plantId,
        agent=agent,
        prompt=prompt,
        conversation_id=payload.conversationId,
        history=history,
        user_text=payload.message,
        page=payload.page.model_dump(),
        draft=draft,
        now_local=payload.nowLocal[:16] if payload.nowLocal else None,
        tz_offset=payload.timezoneOffsetMinutes,
        form_kind=form_kind,
        form_draft=form_draft,
    )
    return StreamingResponse(
        stream_turn(turn),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
