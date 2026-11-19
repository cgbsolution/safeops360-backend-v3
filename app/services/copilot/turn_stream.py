"""Copilot turns that outlive the HTTP request that started them.

A long answer (several model rounds plus tool calls) can run past what any one
connection survives: Vercel ends a function after 300 s, proxies drop quiet
connections, phones switch networks. So a turn is not tied to a connection:

  * POST /api/copilot/chat starts the turn as a background task and streams its
    events. The browser picks the turn id, so a retried POST joins the turn it
    already started instead of starting a second one.
  * Every event stream ends by itself after a segment (default 200 s) with a
    `reconnect` event. The browser then calls GET /chat/{turnId}/events?after=N
    and carries on from event N. A dropped connection resumes the same way.
  * A turn stops only when it finishes or the user presses Stop
    (POST /chat/{turnId}/cancel), never because a connection closed.

Production runs more than one uvicorn worker, so a resume can reach a worker
that is not running the turn. The worker that runs it saves the event log to
the turn's AgentInvocation row (agentSuggestion.stream) about once a second,
with a heartbeat, and other workers stream from that copy. No new table.

Every event carries a sequence number (the SSE `id:`). Text events also carry
`at`, their offset in the reply in UTF-16 units (what JavaScript string indexes
count), so a client that receives part of the text twice can drop the overlap,
and `rewind {to, dropTools}` can undo a model round that failed half-way and
is being retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.agent import AgentInvocation

log = logging.getLogger("safeops360.copilot")

# 32 lowercase hex characters: the same shape as gen_id(), because the turn id
# is also the audit row's primary key.
TURN_ID_PATTERN = r"^[0-9a-f]{32}$"

SEGMENT_SECONDS = 200.0       # one response stream: under Vercel (300 s) and Azure front-end (230 s) limits
KEEPALIVE_SECONDS = 15.0      # comment line so proxies keep a quiet stream open
SAVE_SECONDS = 1.0            # how often the running worker saves new events
HEARTBEAT_SECONDS = 5.0       # save at least this often, even with nothing new
STALE_SECONDS = 45.0          # no heartbeat for this long: the worker died
POLL_SECONDS = 1.0            # how often another worker re-reads the saved copy
KEEP_FINISHED_SECONDS = 300.0

STOPPED_TYPE = "CANCELLED"
STOPPED_TEXT = "Stopped by the user"
LOST_MESSAGE = "The server restarted while answering. Send your message again."
GONE_MESSAGE = "This answer is no longer available. Send your message again."


class TurnExistsError(Exception):
    """A turn with this id was already started (a retried request)."""


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _utf16_tail(s: str, start: int) -> str:
    """`s` without its first `start` UTF-16 units."""
    if start <= 0:
        return s
    return s.encode("utf-16-le")[start * 2:].decode("utf-16-le", errors="ignore")


def sse(event: str, data: dict[str, Any], seq: int | None = None) -> bytes:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n".encode()


# ─── Event log ──────────────────────────────────────────────────────────


class TurnLog:
    """The ordered events of one turn, readable from any point."""

    def __init__(self) -> None:
        self.events: list[tuple[int, str, dict[str, Any]]] = []
        self.text_len = 0
        self.finished = False
        self.version = 0
        self._changed = asyncio.Event()

    def append(self, event: str, data: dict[str, Any]) -> None:
        if event == "text":
            delta = str(data.get("delta") or "")
            if not delta:
                return
            data = {"delta": delta, "at": self.text_len}
            self.text_len += _utf16_len(delta)
        self.events.append((len(self.events) + 1, event, data))
        self._touch()

    def rewind(self, text_len: int, drop_tools: list[str]) -> None:
        """Undo the text and tool chips of a model round that is being retried."""
        if text_len == self.text_len and not drop_tools:
            return
        self.append("rewind", {"to": text_len, "dropTools": list(drop_tools)})
        self.text_len = text_len

    def finish(self) -> None:
        self.finished = True
        self._touch()

    def since(self, after: int) -> list[tuple[int, str, dict[str, Any]]]:
        return self.events[max(after, 0):]

    def waiter(self) -> asyncio.Event:
        return self._changed

    def snapshot(self, *, done: bool | None = None) -> dict[str, Any]:
        """Copy for the database, with runs of text events merged into one."""
        out: list[list[Any]] = []
        parts: list[str] = []
        for seq, event, data in self.events:
            if event == "text" and out and out[-1][1] == "text":
                out[-1][0] = seq
                parts.append(data["delta"])
                continue
            if parts:
                out[-1][2] = {"delta": "".join(parts), "at": out[-1][2]["at"]}
                parts = []
            out.append([seq, event, data])
            if event == "text":
                parts = [data["delta"]]
        if parts:
            out[-1][2] = {"delta": "".join(parts), "at": out[-1][2]["at"]}
        snap = {
            "v": 1,
            "events": out,
            "seq": len(self.events),
            "done": self.finished if done is None else done,
            "beat": datetime.now(UTC).isoformat(),
        }
        # Round-trip so anything the browser got via default=str also fits the JSON column.
        return json.loads(json.dumps(snap, default=str, ensure_ascii=False))

    def _touch(self) -> None:
        self.version += 1
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()


# ─── Running turns (this worker) ────────────────────────────────────────


@dataclass
class _Handle:
    turn: Any  # CopilotTurn (not imported: runtime imports this module)
    task: asyncio.Task | None = None
    finished_at: float | None = None


_LOCAL: dict[str, _Handle] = {}


def _purge() -> None:
    now = time.monotonic()
    for key, handle in list(_LOCAL.items()):
        if handle.finished_at is not None and now - handle.finished_at > KEEP_FINISHED_SECONDS:
            _LOCAL.pop(key, None)


async def start_turn(turn: Any) -> None:
    """Open the turn's audit row (id = turn id) and run the turn in the
    background. Raises TurnExistsError when that id was already started."""
    turn.row_id, turn.turn_number = await turn._open_audit_row()
    _purge()
    handle = _Handle(turn=turn)
    _LOCAL[turn.turn_id] = handle
    handle.task = asyncio.create_task(_drive(handle), name=f"copilot-turn-{turn.turn_id}")


async def _drive(handle: _Handle) -> None:
    turn = handle.turn
    saver = asyncio.create_task(_saver(turn))
    try:
        await turn.run()
    except asyncio.CancelledError:
        pass  # run() has already recorded how the turn ended
    except Exception:  # noqa: BLE001
        log.exception("copilot turn %s crashed", turn.turn_id)
    finally:
        saver.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await saver
        handle.finished_at = time.monotonic()


def _stop_local(handle: _Handle) -> bool:
    turn = handle.turn
    if turn.finishing or handle.task is None or handle.task.done():
        return False
    turn.stop_requested = True
    handle.task.cancel()
    return True


async def request_stop(turn_id: str, user_id: str) -> bool:
    """Stop a running turn. If another worker runs it, mark the row; that
    worker sees the mark within a second and stops the turn."""
    handle = _LOCAL.get(turn_id)
    if handle is not None and handle.turn.user_id == user_id:
        return _stop_local(handle)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AgentInvocation)
            .where(AgentInvocation.id == turn_id, AgentInvocation.invokedById == user_id,
                   AgentInvocation.status == "RUNNING")
            .values(status="ERRORED", hadError=True, errorType=STOPPED_TYPE, errorDetails=STOPPED_TEXT)
        )
        await db.commit()
        return bool(result.rowcount)


# ─── Saved copy (for the other workers) ─────────────────────────────────


async def _saver(turn: Any) -> None:
    """Keep the row's copy of the event log current, and pick up a Stop that
    was pressed on another worker."""
    saved_version = -1
    last_write = 0.0
    while True:
        await asyncio.sleep(SAVE_SECONDS)
        if turn.row_id is None or turn.finishing:
            continue
        try:
            now = time.monotonic()
            if turn.log.version != saved_version or now - last_write >= HEARTBEAT_SECONDS:
                version = turn.log.version
                running = await _write_snapshot(turn.row_id, turn.log.snapshot())
                last_write = now
                if running:
                    saved_version = version
            else:
                running = await _is_running(turn.row_id)
            if not running:
                await _not_running(turn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — saving must never break the turn
            log.warning("copilot: could not save progress of turn %s", turn.turn_id, exc_info=True)


async def _write_snapshot(row_id: str, snapshot: dict[str, Any]) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AgentInvocation)
            .where(AgentInvocation.id == row_id, AgentInvocation.status == "RUNNING")
            .values(agentSuggestion={"stream": snapshot})
        )
        await db.commit()
        return bool(result.rowcount)


async def _is_running(row_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        query = select(AgentInvocation.status).where(AgentInvocation.id == row_id)
        return (await db.execute(query)).scalar_one_or_none() == "RUNNING"


async def _not_running(turn: Any) -> None:
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(AgentInvocation.errorType).where(AgentInvocation.id == turn.row_id)
        )).one_or_none()
        if row is None:
            return
        if row.errorType == STOPPED_TYPE:
            handle = _LOCAL.get(turn.turn_id)
            if handle is not None:
                _stop_local(handle)
        elif row.errorType == "TIMED_OUT" and not turn.finishing:
            # The sweep for abandoned agent runs (15 min) caught a turn that is
            # still working. Put it back; the turn writes its own result at the end.
            await db.execute(
                update(AgentInvocation)
                .where(AgentInvocation.id == turn.row_id, AgentInvocation.errorType == "TIMED_OUT")
                .values(status="RUNNING", hadError=False, errorType=None, errorDetails=None)
            )
            await db.commit()


# ─── Lookup ─────────────────────────────────────────────────────────────


async def find_turn(db: AsyncSession, turn_id: str) -> Any | None:
    """(id, invokedById, sourceModule) of the audit row, or None."""
    return (await db.execute(
        select(AgentInvocation.id, AgentInvocation.invokedById, AgentInvocation.sourceModule)
        .where(AgentInvocation.id == turn_id)
    )).one_or_none()


# ─── Streaming to the browser ───────────────────────────────────────────


async def stream_events(turn_id: str, user_id: str, *, after: int = 0,
                        segment_seconds: float = SEGMENT_SECONDS) -> AsyncIterator[bytes]:
    """SSE for one segment of a turn, starting after event `after`. Ends with
    `end` when the turn is over, or `reconnect` when the segment is used up."""
    handle = _LOCAL.get(turn_id)
    if handle is not None and handle.turn.user_id == user_id:
        source = _local_events(handle.turn.log, after, segment_seconds)
    else:
        source = _saved_events(turn_id, after, segment_seconds)
    async for chunk in source:
        yield chunk


async def _local_events(turn_log: TurnLog, after: int, segment_seconds: float) -> AsyncIterator[bytes]:
    deadline = time.monotonic() + segment_seconds
    yield b": open\n\n"
    while True:
        waiter = turn_log.waiter()
        for seq, event, data in turn_log.since(after):
            yield sse(event, data, seq)
            after = seq
        if turn_log.finished:
            yield sse("end", {})
            return
        left = deadline - time.monotonic()
        if left <= 0:
            yield sse("reconnect", {"after": after})
            return
        try:
            await asyncio.wait_for(waiter.wait(), timeout=min(KEEPALIVE_SECONDS, left))
        except TimeoutError:
            yield b": ping\n\n"


async def _load_saved(turn_id: str) -> Any | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(AgentInvocation.status, AgentInvocation.agentSuggestion, AgentInvocation.invokedAt)
            .where(AgentInvocation.id == turn_id)
        )).one_or_none()


def _age_seconds(saved: dict[str, Any], invoked_at: datetime | None) -> float:
    ref: datetime | None = None
    beat = saved.get("beat")
    if isinstance(beat, str):
        with contextlib.suppress(ValueError):
            ref = datetime.fromisoformat(beat)
    if ref is None:
        ref = invoked_at
    if ref is None:
        return 0.0
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ref).total_seconds()


async def _saved_events(turn_id: str, after: int, segment_seconds: float) -> AsyncIterator[bytes]:
    """Stream a turn that another worker runs, from the copy on its row."""
    deadline = time.monotonic() + segment_seconds
    last_ping = time.monotonic()
    sent_text: int | None = None  # reply text already sent on this stream (UTF-16 units)
    yield b": open\n\n"
    while True:
        row = await _load_saved(turn_id)
        if row is None:
            yield sse("error", {"message": GONE_MESSAGE})
            yield sse("end", {})
            return
        suggestion = row.agentSuggestion if isinstance(row.agentSuggestion, dict) else {}
        saved = suggestion.get("stream") if isinstance(suggestion.get("stream"), dict) else {}
        for seq, event, data in saved.get("events") or []:
            if seq <= after:
                continue
            if event == "text":
                at, delta = data.get("at", 0), data.get("delta", "")
                end = at + _utf16_len(delta)
                if sent_text is not None and sent_text > at:
                    if sent_text >= end:
                        after = seq
                        continue
                    data = {"delta": _utf16_tail(delta, sent_text - at), "at": sent_text}
                sent_text = end
            elif event == "rewind":
                sent_text = data.get("to")
            yield sse(event, data, seq)
            after = seq
        if saved.get("done"):
            yield sse("end", {})
            return
        if row.status != "RUNNING" and not saved:
            yield sse("error", {"message": GONE_MESSAGE})
            yield sse("end", {})
            return
        if _age_seconds(saved, row.invokedAt) > STALE_SECONDS:
            yield sse("error", {"message": LOST_MESSAGE})
            yield sse("end", {})
            return
        if time.monotonic() >= deadline:
            yield sse("reconnect", {"after": after})
            return
        await asyncio.sleep(POLL_SECONDS)
        if time.monotonic() - last_ping >= KEEPALIVE_SECONDS:
            last_ping = time.monotonic()
            yield b": ping\n\n"
