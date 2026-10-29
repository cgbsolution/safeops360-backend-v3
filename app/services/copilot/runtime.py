"""Streaming tool loop for the copilot.

One HTTP request = one user turn. The browser holds the conversation (the
exact Anthropic `messages` list, thinking blocks included) and sends it back
each turn; we append to it and never rewrite it, which keeps the prompt cache
warm and keeps earlier thinking blocks valid. The server stores no transcript
of its own; every turn is audited as an `AgentInvocation` row
(sourceModule COPILOT) with its tool calls, tokens and cost.

Why the browser and not the DB holds the history: the copilot needed no new
table (schema changes go through hand-written DDL on this deployment). A
user who edits their own history only affects their own chat, because every
tool re-checks permissions server-side.

SSE events sent to the browser:
  text         {delta}                         streamed assistant text
  tool         {id, name, label, status, error?}
  draft_patch  {patch, check}                  apply to the open permit form
  draft_check  {check}
  form_patch   {kind, patch}                    apply to the open observation form / HIRA entry editor
  people       {userIds}                       ids the browser may resolve to names
  action       {type: navigate|specialist, ...}
  done         {appendMessages, replaceHistory?, usage, turnNumber}
  error        {message}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.models.agent import Agent, AgentInvocation, AgentPrompt, AgentToolCall
from app.services.ai.redaction import redact_data, scrub_text
from app.services.copilot import ptw_draft
from app.services.copilot.tools import (
    TOOL_DEFINITIONS,
    TOOL_LABELS,
    TOOL_NAMES,
    ToolError,
    TurnContext,
    dispatch,
)

log = logging.getLogger("safeops360.copilot")

AGENT_CODE = "SAFEOPS_COPILOT"
SOURCE_MODULE = "COPILOT"
_PROMPT_PATH = Path(__file__).parent / "prompts" / "copilot_v1.md"
_AUTO_SYNC_TAG = "Auto-synced from code"

# Request guards. The history comes from the browser, so it is bounded.
MAX_HISTORY_MESSAGES = 160
MAX_HISTORY_BYTES = 900_000
MAX_USER_TEXT = 4000

# USD per million tokens (input, output). Copilot-only additions; the agent
# platform's own table covers the older models.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

_EFFORT_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-fable-5", "claude-opus-4-7", "claude-opus-4-8")
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_ANSWER_NUDGE = (
    "[app] Your last reply had no visible text, so the user saw nothing. Write your full answer to the "
    "user's message now, using the tool results above. Do not call more tools unless something is missing."
)

_TOOLS_FOR_API: list[dict[str, Any]] = [{**t, "eager_input_streaming": True} for t in TOOL_DEFINITIONS]


def load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


# ─── Agent bootstrap ────────────────────────────────────────────────────


async def ensure_copilot_agent(db: AsyncSession, user_id: str) -> tuple[Agent, AgentPrompt]:
    """The copilot is an Agent row like the specialists, so it shows up in
    Configuration → Agents (model, on/off, rate limit, cost). Created on first
    use; the prompt is re-synced from code when the file changes, unless an
    admin has promoted a prompt of their own."""
    settings = get_settings()
    agent = (await db.execute(select(Agent).where(Agent.code == AGENT_CODE))).scalar_one_or_none()
    if agent is None:
        agent = Agent(
            code=AGENT_CODE,
            name="SafeOps Copilot",
            description=(
                "Conversational assistant in front of every SafeOps360 agent: drafts permits to work on the "
                "New Permit form, looks up records, and starts the specialist agents on request."
            ),
            module=SOURCE_MODULE,
            capabilities={
                "ptw_drafting": "Fills the New Permit form from a job description and checks it against the rules",
                "record_lookup": "Finds the user's permits, incidents, observations, near misses and CAPAs",
                "orchestration": "Starts RCA, permit review, triage, HIRA and CAPA specialists",
            },
            primaryModelId=settings.copilot_model,
            escalationModelId=None,
            currentAuthorityLevel="L1",
            maxAuthorityLevel="L1",
            authorityRationale="Drafts and suggests; the user reviews every field and submits.",
            availableTools=TOOL_NAMES,
            estimatedTokensPerInvocation=20000,
            estimatedCostPerInvocation=0.06,
            isActive=True,
            isInPilot=True,
            rateLimit=settings.copilot_rate_limit_per_hour,
        )
        db.add(agent)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            agent = (await db.execute(select(Agent).where(Agent.code == AGENT_CODE))).scalar_one()

    text = load_system_prompt()
    active = await db.get(AgentPrompt, agent.activePromptId) if agent.activePromptId else None
    admin_override = active is not None and not (active.promptDescription or "").startswith(_AUTO_SYNC_TAG)
    if active is None or (not admin_override and active.systemPrompt != text):
        last = (
            await db.execute(select(func.max(AgentPrompt.version)).where(AgentPrompt.agentId == agent.id))
        ).scalar_one_or_none() or 0
        prompt = AgentPrompt(
            agentId=agent.id,
            version=last + 1,
            systemPrompt=text,
            promptDescription=f"{_AUTO_SYNC_TAG} (app/services/copilot/prompts/copilot_v1.md)",
            createdById=user_id,
        )
        db.add(prompt)
        await db.flush()
        agent.activePromptId = prompt.id
        active = prompt
    if set(agent.availableTools or []) != set(TOOL_NAMES):
        agent.availableTools = TOOL_NAMES
    await db.commit()
    return agent, active


async def enforce_rate_limit(db: AsyncSession, agent: Agent, user_id: str) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    used = (
        await db.execute(
            select(func.count())
            .select_from(AgentInvocation)
            .where(AgentInvocation.agentId == agent.id, AgentInvocation.invokedById == user_id)
            .where(AgentInvocation.invokedAt > since)
        )
    ).scalar_one()
    if used >= agent.rateLimit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Copilot limit reached ({agent.rateLimit} messages per hour). Try again later.",
        )


async def _next_turn_number(db: AsyncSession) -> str:
    prefix = f"ACOP-{datetime.now(timezone.utc).year}-"
    last = (
        await db.execute(
            select(AgentInvocation.invocationNumber)
            .where(AgentInvocation.invocationNumber.like(f"{prefix}%"))
            .order_by(AgentInvocation.invocationNumber.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    n = int(last.rsplit("-", 1)[-1]) + 1 if last else 1
    return f"{prefix}{n:06d}"


# ─── History validation ─────────────────────────────────────────────────


def validate_history(history: Any) -> list[dict[str, Any]]:
    """Shape-check the browser-held transcript. The API validates the rest."""
    if history in (None, []):
        return []
    if not isinstance(history, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "messages must be a list")
    if len(history) > MAX_HISTORY_MESSAGES or len(json.dumps(history)) > MAX_HISTORY_BYTES:
        raise HTTPException(
            413,
            "This conversation is too long. Start a new chat to continue.",
        )
    for m in history:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed conversation history")
        if not isinstance(m.get("content"), (str, list)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed conversation history")
    if history[0].get("role") != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Conversation must start with a user message")
    return history


def _strip_thinking(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in history:
        content = m.get("content")
        if m.get("role") == "assistant" and isinstance(content, list):
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
            out.append({**m, "content": kept or [{"type": "text", "text": "(no text)"}]})
        else:
            out.append(m)
    return out


# ─── Turn ───────────────────────────────────────────────────────────────


def build_user_message(*, text: str, page: dict[str, Any], draft: dict[str, Any] | None,
                       now_local: str | None, tz_offset: int, user_plant_id: str | None,
                       user_id: str | None = None, form_kind: str | None = None,
                       form_draft: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "path": page.get("path"),
        "module": page.get("module"),
        "recordId": page.get("recordId"),
        "recordLabel": page.get("recordLabel"),
        "nowLocal": now_local,
        "timezoneOffsetMinutes": tz_offset,
        "userId": user_id,
        "userPlantId": user_plant_id,
        "ptwFormOpen": draft is not None,
    }
    if draft is not None:
        ctx["ptwDraft"] = redact_data(draft)
    if form_kind == "OBSERVATION" and form_draft is not None:
        ctx["observationFormOpen"] = True
        ctx["observationDraft"] = redact_data(form_draft)
    elif form_kind == "HIRA_ENTRY" and form_draft is not None:
        ctx["hiraEntryOpen"] = True
        ctx["hiraEntryDraft"] = redact_data(form_draft)
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "<page_context>\n" + json.dumps(ctx, default=str, ensure_ascii=False) + "\n</page_context>"},
            {"type": "text", "text": scrub_text(text) or "(empty message)"},
        ],
    }


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    try:
        return block.model_dump(mode="json", exclude_none=True)
    except Exception:  # noqa: BLE001
        return json.loads(json.dumps(block, default=lambda o: getattr(o, "__dict__", str(o))))


def _cost(model: str, usage: dict[str, int]) -> float:
    inp, out = _PRICES.get(model, (3.0, 15.0))
    total = (
        usage["input"] * inp
        + usage["cacheWrite"] * inp * 1.25
        + usage["cacheRead"] * inp * 0.1
        + usage["output"] * out
    )
    return round(total / 1_000_000, 6)


def _request_options(model: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Per-model request extras, passed raw so they work on every SDK version
    the backend pins (0.100 – 2.x)."""
    settings = get_settings()
    extra_body: dict[str, Any] = {}
    headers: dict[str, str] = {}
    m = model.lower()
    if any(m.startswith(p) for p in _EFFORT_MODELS) and settings.copilot_effort:
        extra_body["output_config"] = {"effort": settings.copilot_effort}
    if m in ("claude-opus-4-6", "claude-sonnet-4-6"):
        extra_body["thinking"] = {"type": "adaptive"}
    if m in _FALLBACK_MODELS and settings.copilot_refusal_fallback:
        extra_body["fallbacks"] = "default"
        headers["anthropic-beta"] = _FALLBACK_BETA
    return extra_body, headers


class CopilotTurn:
    """Runs one turn and pushes SSE events onto a queue."""

    def __init__(
        self,
        *,
        user_id: str,
        user_plant_id: str | None,
        agent: Agent,
        prompt: AgentPrompt,
        conversation_id: str,
        history: list[dict[str, Any]],
        user_text: str,
        page: dict[str, Any],
        draft: dict[str, Any] | None,
        now_local: str | None,
        tz_offset: int,
        form_kind: str | None = None,
        form_draft: dict[str, Any] | None = None,
    ) -> None:
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        self.user_id = user_id
        self.agent_id = agent.id
        self.prompt_id = prompt.id
        self.system_prompt = prompt.systemPrompt
        self.model = agent.primaryModelId
        self.authority = agent.currentAuthorityLevel
        self.conversation_id = conversation_id
        self.history = history
        self.user_text = user_text[:MAX_USER_TEXT]
        self.page = page
        self.ctx = TurnContext(
            user_id=user_id,
            user_plant_id=user_plant_id,
            conversation_id=conversation_id,
            page=page,
            emit=self._emit,
            draft=draft,
            now_local=now_local,
            tz_offset_minutes=tz_offset,
            form_kind=form_kind,
            form_draft=form_draft,
        )
        self.user_message = build_user_message(
            text=self.user_text, page=page, draft=draft, now_local=now_local, tz_offset=tz_offset,
            user_plant_id=user_plant_id, user_id=user_id, form_kind=form_kind, form_draft=form_draft,
        )
        self.usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        self.tool_records: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        if event == "draft_patch":
            self.patches.append(data.get("patch") or {})
        elif event == "form_patch":
            self.patches.append({"form": data.get("kind"), **(data.get("patch") or {})})
        await self.queue.put((event, data))

    # The audit row is written before the model is called, so a turn that
    # dies mid-way still leaves a record (the reaper marks it TIMED_OUT).
    async def _open_audit_row(self) -> tuple[str, str]:
        async with AsyncSessionLocal() as db:
            for _ in range(3):
                number = await _next_turn_number(db)
                row = AgentInvocation(
                    invocationNumber=number,
                    agentId=self.agent_id,
                    invokedById=self.user_id,
                    invocationTrigger="USER_INITIATED",
                    sourceModule=SOURCE_MODULE,
                    sourceRecordId=self.conversation_id,
                    sourceRecordType="CONVERSATION",
                    sourcePlantId=(self.ctx.draft or {}).get("plantId") or self.ctx.user_plant_id,
                    authorityLevelUsed=self.authority,
                    promptVersionId=self.prompt_id,
                    modelUsed=self.model,
                    inputContext={
                        "page": {k: v for k, v in self.page.items() if k != "ptwDraft"},
                        "userMessage": scrub_text(self.user_text),
                        "historyMessages": len(self.history),
                        "ptwDraftBefore": redact_data(self.ctx.draft) if self.ctx.draft is not None else None,
                        "formKind": self.ctx.form_kind,
                        "formDraftBefore": redact_data(self.ctx.form_draft) if self.ctx.form_draft is not None else None,
                    },
                    status="RUNNING",
                )
                db.add(row)
                try:
                    await db.commit()
                    return row.id, number
                except IntegrityError:
                    await db.rollback()
            raise RuntimeError("could not allocate a copilot turn number")

    async def _close_audit_row(self, row_id: str, *, started: float, error: str | None,
                               last_response: dict[str, Any] | None) -> None:
        try:
            async with AsyncSessionLocal() as db:
                values: dict[str, Any] = {
                    "status": "ERRORED" if error else "COMPLETED",
                    "inputTokens": self.usage["input"] + self.usage["cacheRead"] + self.usage["cacheWrite"],
                    "outputTokens": self.usage["output"],
                    "totalCostUsd": _cost(self.model, self.usage),
                    "latencyMs": int((time.monotonic() - started) * 1000),
                    "agentSuggestion": {
                        "draftPatches": self.patches,
                        "specialistsStarted": self.ctx.specialists_started,
                        "usage": self.usage,
                    },
                    "rawApiResponse": last_response,
                    "hadError": bool(error),
                    "errorType": "API_ERROR" if error else None,
                    "errorDetails": error[:2000] if error else None,
                }
                await db.execute(update(AgentInvocation).where(AgentInvocation.id == row_id).values(**values))
                for i, rec in enumerate(self.tool_records, start=1):
                    db.add(AgentToolCall(invocationId=row_id, sequence=i, **rec))
                await db.execute(
                    update(Agent).where(Agent.id == self.agent_id).values(totalInvocations=Agent.totalInvocations + 1)
                )
                await db.commit()
        except Exception:  # noqa: BLE001 — auditing must never break the chat
            log.exception("copilot: failed to close audit row %s", row_id)

    async def run(self) -> None:
        started = time.monotonic()
        error: str | None = None
        last_response: dict[str, Any] | None = None
        new_messages: list[dict[str, Any]] = [self.user_message]
        replace_history: list[dict[str, Any]] | None = None
        turn_number = None
        row_id = None
        try:
            row_id, turn_number = await self._open_audit_row()
            last_response, replace_history = await self._loop(new_messages)
        except asyncio.CancelledError:
            error = "Client disconnected"
            raise
        except Exception as e:  # noqa: BLE001
            error = _friendly_error(e)
            log.warning("copilot turn failed: %s", e)
            await self.queue.put(("error", {"message": error}))
        finally:
            if row_id:
                await self._close_audit_row(row_id, started=started, error=error, last_response=last_response)
            if error != "Client disconnected":
                # A turn that failed before the model answered leaves the
                # transcript as it was, so the user can simply resend.
                append = new_messages if (not error or len(new_messages) > 1) else []
                await self.queue.put(("done", {
                    "appendMessages": append,
                    "replaceHistory": replace_history,
                    "usage": {**self.usage, "costUsd": _cost(self.model, self.usage)},
                    "turnNumber": turn_number,
                    "model": self.model,
                }))
            await self.queue.put(None)

    async def _loop(self, new_messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
        import anthropic  # noqa: PLC0415

        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("AI is not configured on this server (ANTHROPIC_API_KEY missing)")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=180.0, max_retries=1)
        extra_body, headers = _request_options(self.model)
        system = [{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}]
        history = list(self.history)
        replace_history: list[dict[str, Any]] | None = None
        last_dump: dict[str, Any] | None = None
        stripped_once = False
        nudged = False
        json_retries = 0
        truncation_retries = 0

        iteration = 0
        while iteration < settings.copilot_max_iterations:
            iteration += 1
            messages = history + new_messages
            try:
                final = await self._stream_once(client, system, messages, extra_body, headers)
                json_retries = 0
            except ValueError:
                # Tool-input JSON the SDK could not parse (eager input streaming).
                # No tool_use id to answer, so re-issue the request (bounded).
                json_retries += 1
                if json_retries > 2:
                    raise
                iteration -= 1
                continue
            except anthropic.BadRequestError as e:
                # Earlier thinking blocks no longer verify (e.g. the history was
                # trimmed in the browser). One-time recovery: drop them and retry.
                if not stripped_once and "signature" in str(e).lower():
                    stripped_once = True
                    history = _strip_thinking(history)
                    replace_history = history
                    iteration -= 1
                    continue
                raise

            last_dump = final.model_dump(mode="json", exclude_none=True)
            u = final.usage
            self.usage["input"] += getattr(u, "input_tokens", 0) or 0
            self.usage["output"] += getattr(u, "output_tokens", 0) or 0
            self.usage["cacheRead"] += getattr(u, "cache_read_input_tokens", 0) or 0
            self.usage["cacheWrite"] += getattr(u, "cache_creation_input_tokens", 0) or 0

            stop = final.stop_reason
            has_text = any(getattr(b, "type", None) == "text" and (getattr(b, "text", "") or "").strip()
                           for b in final.content)
            has_tools = any(getattr(b, "type", None) == "tool_use" for b in final.content)
            if not has_text and not has_tools and stop == "end_turn" and not nudged:
                # The model ended without a visible reply. On the newest models
                # text written before a tool call comes back as hidden progress
                # notes, so an answer written before the last tool call never
                # reaches the user. Drop the empty reply (it is the tail, so this
                # is not a history edit) and ask once for the written answer.
                nudged = True
                last = new_messages[-1]
                note = {"type": "text", "text": _ANSWER_NUDGE}
                if last["role"] == "user" and isinstance(last["content"], list):
                    last["content"] = [*last["content"], note]
                else:
                    new_messages.append({"role": "user", "content": [note]})
                iteration -= 1
                continue

            assistant = {"role": "assistant", "content": [_block_to_dict(b) for b in final.content]}
            new_messages.append(assistant)

            if stop == "refusal":
                await self._emit("text", {"delta": "\n\nI can't help with that request."})
                break
            if stop == "pause_turn":
                continue
            tool_uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                break

            results: list[dict[str, Any]] = []
            if stop == "max_tokens":
                # A truncated tool input parses as a valid-looking partial object.
                # Never run it; tell the model and let it retry smaller.
                truncation_retries += 1
                for tu in tool_uses:
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                    "content": "Output limit reached before this tool input finished. Send a smaller update."})
                new_messages.append({"role": "user", "content": results})
                if truncation_retries > 1:
                    break
                continue

            for tu in tool_uses:
                results.append(await self._run_tool(tu))
            new_messages.append({"role": "user", "content": results})
        else:
            await self._emit("text", {"delta": "\n\n(I stopped after several steps — tell me what to do next.)"})
        return last_dump, replace_history

    async def _stream_once(self, client: Any, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                           extra_body: dict[str, Any], headers: dict[str, str]) -> Any:
        settings = get_settings()
        announced: set[str] = set()
        async with client.messages.stream(
            model=self.model,
            max_tokens=settings.copilot_max_tokens,
            system=system,
            tools=_TOOLS_FOR_API,
            messages=messages,
            extra_body=extra_body or None,
            extra_headers=headers or None,
        ) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "text":
                    await self._emit("text", {"delta": event.text})
                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if getattr(block, "type", None) == "tool_use" and block.id not in announced:
                        announced.add(block.id)
                        await self._emit("tool", {"id": block.id, "name": block.name,
                                                  "label": TOOL_LABELS.get(block.name, block.name), "status": "pending"})
            return await stream.get_final_message()

    async def _run_tool(self, tu: Any) -> dict[str, Any]:
        name, tool_id = tu.name, tu.id
        args = tu.input if isinstance(tu.input, dict) else {}
        await self._emit("tool", {"id": tool_id, "name": name, "label": TOOL_LABELS.get(name, name), "status": "running"})
        t0 = time.monotonic()
        is_error = False
        try:
            output: Any = await dispatch(name, args, self.ctx)
        except ToolError as e:
            is_error, output = True, {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("copilot tool %s crashed", name)
            is_error, output = True, {"error": f"Internal error in {name}: {type(e).__name__}"}
        ms = int((time.monotonic() - t0) * 1000)
        safe = redact_data(output)
        self.tool_records.append({
            "toolName": name,
            "toolInput": redact_data(args),
            "toolOutput": safe,
            "executionMs": ms,
            "hadError": is_error,
            "errorDetails": safe.get("error") if is_error and isinstance(safe, dict) else None,
        })
        await self._emit("tool", {"id": tool_id, "name": name, "label": TOOL_LABELS.get(name, name),
                                  "status": "error" if is_error else "done",
                                  "error": safe.get("error") if is_error and isinstance(safe, dict) else None})
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id,
                                 "content": json.dumps(safe, default=str, ensure_ascii=False)}
        if is_error:
            block["is_error"] = True
        return block


def _friendly_error(e: Exception) -> str:
    try:
        import anthropic  # noqa: PLC0415

        if isinstance(e, anthropic.RateLimitError):
            return "The AI service is busy right now. Try again in a minute."
        if isinstance(e, anthropic.AuthenticationError):
            return "The AI service rejected this server's credentials. Ask an administrator to check the API key."
        if isinstance(e, anthropic.NotFoundError):
            return "The configured AI model is not available. Ask an administrator to check the copilot model."
        if isinstance(e, anthropic.BadRequestError):
            return f"The AI service rejected the request: {getattr(e, 'message', str(e))[:300]}"
        if isinstance(e, anthropic.APIConnectionError):
            return "Could not reach the AI service. Check the connection and try again."
        if isinstance(e, anthropic.APIStatusError):
            return f"The AI service returned an error ({e.status_code}). Try again."
    except ImportError:
        pass
    msg = str(e)
    return msg if msg.startswith("AI is not configured") else "Something went wrong running the copilot. Try again."


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n".encode()


async def stream_turn(turn: CopilotTurn) -> AsyncIterator[bytes]:
    """Drive the turn as a task and relay its events. A keepalive comment every
    15 s stops proxies from closing a quiet connection while a tool runs."""
    task = asyncio.create_task(turn.run())
    try:
        yield b": open\n\n"
        while True:
            try:
                item = await asyncio.wait_for(turn.queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield b": ping\n\n"
                continue
            if item is None:
                break
            yield _sse(*item)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


__all__ = [
    "AGENT_CODE",
    "CopilotTurn",
    "enforce_rate_limit",
    "ensure_copilot_agent",
    "load_system_prompt",
    "ptw_draft",
    "stream_turn",
    "validate_history",
]
