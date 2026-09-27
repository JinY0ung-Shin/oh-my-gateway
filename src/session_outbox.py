"""Between-turn message capture for persistent Claude SDK sessions.

A Responses API turn reads ``client.receive_response()`` and stops at the
turn's ``ResultMessage``. Anything the CLI emits *after* that — background
task lifecycle events (``task_started`` / ``task_progress`` /
``task_notification`` / ``task_updated``), and the assistant messages the
harness produces when it re-invokes the model after a background task
finishes — used to pile up unread in the SDK's bounded message stream
(``max_buffer_size=100``) until the next turn's reader stole them.

This module gives each session:

* an **idle reader** — a task that consumes ``client.receive_messages()``
  between turns and stops before the next turn reads the client, and
* an **outbox** — a bounded, seq-cursored buffer of the captured events,
  served to pollers by ``GET /v1/sessions/{id}/pending-events``.

Turn readers must call :func:`pause_idle_reader` before touching the client
(the Claude backend does this at the top of ``run_completion_with_client`` /
``receive_response_from_client``); turn teardown calls
:func:`resume_idle_reader`.

Stop protocol: the reader races ``__anext__`` against a stop event. On stop
it first grants the pending read a short grace window (so a message that was
being handed over at that exact moment is captured, not lost to anyio's
cancel-during-delivery window), then cancels **and awaits** the leftover task
so it can never steal the next turn's first message.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.backends.claude.constants import SUBAGENT_TOOL_NAMES

logger = logging.getLogger(__name__)

# Outbox bounds: per-session ring buffer and per-poll page size.
OUTBOX_MAX_EVENTS = 500
OUTBOX_MAX_EVENTS_PER_POLL = 200

# Stop protocol: grace window for an in-flight __anext__ before cancelling,
# and the overall budget pause_idle_reader() waits for the pump to exit.
_STOP_GRACE_S = 0.2
_PAUSE_TIMEOUT_S = 3.0

# Turn-start sweep: how long to wait for one more buffered message before
# concluding the backlog is drained. Buffered messages resolve instantly, so
# this only bounds the final "is there more?" probe.
_DRAIN_QUIET_S = 0.05

# Task statuses that mean the task has finished (mirrors the SDK's
# TERMINAL_TASK_STATUSES across both lifecycle vocabularies).
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})

# Agent-name lookups (spawn tool_use id → name, task id → name) live as long
# as the session, so both are capped; the least recently used entry goes
# first. Names are the model-chosen ``name`` input of an Agent/Task call,
# which CLI 2.1.283 validates as a ≤ 64-char identifier; the length cap here
# only bounds memory for calls the CLI would reject anyway.
_AGENT_NAME_MAP_MAX = 256
_AGENT_NAME_MAX_LEN = 256


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_put(mapping: "OrderedDict[str, str]", key: str, value: str) -> None:
    """Insert or refresh *key*, evicting the least recently used overflow."""
    mapping[key] = value
    mapping.move_to_end(key)
    while len(mapping) > _AGENT_NAME_MAP_MAX:
        mapping.popitem(last=False)


class SessionOutbox:
    """Seq-cursored ring buffer of between-turn events plus a live task map."""

    def __init__(self, maxlen: int = OUTBOX_MAX_EVENTS) -> None:
        self.events: deque = deque(maxlen=maxlen)
        self.next_seq = 1
        self.active_tasks: Dict[str, Dict[str, Any]] = {}
        # Names given by Agent/Task calls, keyed by the call's tool_use id
        # (its task_started carries the same id) and by task id: a task
        # resumed via SendMessage is re-announced under the SendMessage
        # call's id, so only the task id still finds its name.
        self._spawn_names: "OrderedDict[str, str]" = OrderedDict()
        self._task_names: "OrderedDict[str, str]" = OrderedDict()
        # The tool call behind each task's first announcement. A resumed agent
        # keeps it: its own messages still carry that call as their
        # ``parent_tool_use_id``, not the SendMessage call it resumed under.
        self._task_spawn_ids: "OrderedDict[str, str]" = OrderedDict()

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Stamp *event* with ``seq``/``ts`` and append it to the buffer."""
        event = dict(event)
        event["seq"] = self.next_seq
        event["ts"] = _utcnow_iso()
        self.next_seq += 1
        self.events.append(event)
        return event

    def events_after(self, after: int, limit: int = OUTBOX_MAX_EVENTS_PER_POLL) -> List[Dict[str, Any]]:
        """Return up to *limit* events with ``seq > after`` (oldest first)."""
        return [e for e in self.events if e["seq"] > after][:limit]

    # ------------------------------------------------------------------
    # Active-task registry
    # ------------------------------------------------------------------

    def _task_entry(self, task_id: str) -> Dict[str, Any]:
        """Get or synthesize the registry entry for *task_id*.

        Synthesis matters: a task started *during* a turn (idle reader off)
        keeps reporting progress after the turn ends, so the first thing the
        reader sees for it may be a progress/updated event.
        """
        entry = self.active_tasks.get(task_id)
        if entry is None:
            entry = {
                "task_id": task_id,
                "name": None,
                "tool_use_id": None,
                "description": "",
                "status": "running",
                "task_type": None,
                "subagent_type": None,
                "last_tool_name": None,
                "usage": None,
                "started_at": _utcnow_iso(),
                "updated_at": _utcnow_iso(),
            }
            self.active_tasks[task_id] = entry
        return entry

    def note_agent_spawn(self, tool_use_id: str, name: str) -> None:
        """Remember the ``name`` an Agent/Task call gave the agent it spawns.

        The call normally arrives before its ``task_started``; a task that is
        already registered under the same tool_use id (out-of-order delivery)
        is labelled right away.
        """
        _bounded_put(self._spawn_names, tool_use_id, name)
        for entry in self.active_tasks.values():
            if entry.get("tool_use_id") == tool_use_id and not entry.get("name"):
                entry["name"] = name
                _bounded_put(self._task_names, entry["task_id"], name)

    def agent_name_for(
        self, task_id: Optional[str], tool_use_id: Optional[str] = None
    ) -> Optional[str]:
        """Name of the agent behind a task, when its spawning call named it."""
        name = self._spawn_names.get(tool_use_id) if tool_use_id else None
        if name is None and task_id:
            name = self._task_names.get(task_id)
        return name

    def apply_task_event(self, event: Dict[str, Any]) -> None:
        """Fold a task event into the active-task map."""
        task_id = event.get("task_id")
        if not task_id:
            return
        etype = event.get("type")
        status = event.get("status")
        if status in _TERMINAL_TASK_STATUSES:
            self.active_tasks.pop(task_id, None)
            return
        entry = self._task_entry(task_id)
        entry["updated_at"] = _utcnow_iso()
        tool_use_id = event.get("tool_use_id")
        spawn_id = self._task_spawn_ids.get(task_id)
        if spawn_id is None and tool_use_id:
            spawn_id = tool_use_id
            _bounded_put(self._task_spawn_ids, task_id, spawn_id)
        if spawn_id:
            entry["tool_use_id"] = spawn_id
        name = self.agent_name_for(task_id, tool_use_id)
        if name:
            entry["name"] = name
            _bounded_put(self._task_names, task_id, name)
        if event.get("description"):
            entry["description"] = event["description"]
        if etype == "task_started":
            for key in ("task_type", "subagent_type"):
                if event.get(key):
                    entry[key] = event[key]
        if event.get("last_tool_name"):
            entry["last_tool_name"] = event["last_tool_name"]
        if event.get("usage"):
            entry["usage"] = event["usage"]
        if etype == "task_updated" and status:
            entry["status"] = status

    def snapshot_active_tasks(self) -> List[Dict[str, Any]]:
        return [dict(v) for v in self.active_tasks.values()]

    def clear_active_tasks(self) -> int:
        """Forget every running task; return how many were dropped.

        Agent names and spawn ids are kept: a resumed conversation can
        re-announce an old agent under the same task id.
        """
        dropped = len(self.active_tasks)
        self.active_tasks.clear()
        return dropped


def get_outbox(session) -> SessionOutbox:
    """Return the session's outbox, creating it lazily."""
    outbox = getattr(session, "outbox", None)
    if outbox is None:
        outbox = SessionOutbox()
        session.outbox = outbox
    return outbox


def reset_active_tasks(session, reason: str) -> None:
    """Drop the task registry once the CLI that owned its tasks is gone.

    Tasks live inside the session's CLI process, which reports their end only
    on its own message stream. After the gateway drops that client (a failed
    or cancelled turn, an SDK stream error, a fresh client replacing it) or
    the idle reader loses the stream, no terminal event can arrive: a leftover
    entry would read as running forever, and a stop request for it would be
    accepted, because the CLI acknowledges ids it does not know. Never raises.
    """
    outbox = getattr(session, "outbox", None)
    if outbox is None:
        return
    try:
        dropped = outbox.clear_active_tasks()
    except Exception:  # never let bookkeeping break a teardown path
        logger.debug("task registry reset failed", exc_info=True)
        return
    if dropped:
        logger.info(
            "[task-registry] session=%s dropped %d task(s) after %s",
            getattr(session, "session_id", "?"),
            dropped,
            reason,
        )


# ---------------------------------------------------------------------------
# SDK message → outbox event conversion
# ---------------------------------------------------------------------------


def _message_to_event(message: Any) -> Optional[Dict[str, Any]]:
    """Convert one SDK message into an outbox event dict, or None to skip.

    Imports the SDK types lazily so this module stays importable in test
    environments that stub the SDK.
    """
    try:
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            TaskNotificationMessage,
            TaskProgressMessage,
            TaskStartedMessage,
            TaskUpdatedMessage,
            TextBlock,
        )
    except Exception:  # pragma: no cover - SDK unavailable in stubbed tests
        return _duck_typed_event(message)

    if isinstance(message, TaskStartedMessage):
        data = message.data if isinstance(getattr(message, "data", None), dict) else {}
        return {
            "type": "task_started",
            "task_id": message.task_id,
            "tool_use_id": _message_tool_use_id(message),
            "description": message.description,
            "task_type": getattr(message, "task_type", None) or data.get("task_type"),
            "subagent_type": getattr(message, "subagent_type", None)
            or data.get("subagent_type"),
        }
    if isinstance(message, TaskProgressMessage):
        return {
            "type": "task_progress",
            "task_id": message.task_id,
            "tool_use_id": _message_tool_use_id(message),
            "description": message.description,
            "last_tool_name": message.last_tool_name,
            "usage": dict(message.usage) if message.usage else None,
        }
    if isinstance(message, TaskNotificationMessage):
        return {
            "type": "task_notification",
            "task_id": message.task_id,
            "tool_use_id": _message_tool_use_id(message),
            "status": message.status,
            "summary": message.summary,
            "output_file": message.output_file,
            "usage": dict(message.usage) if message.usage else None,
        }
    if isinstance(message, TaskUpdatedMessage):
        patch = message.patch if isinstance(message.patch, dict) else {}
        return {
            "type": "task_updated",
            "task_id": message.task_id,
            "tool_use_id": _message_tool_use_id(message),
            "status": message.status or patch.get("status"),
            "patch": patch,
        }
    if isinstance(message, AssistantMessage):
        # Subagent-internal narration is not user-facing (mirrors the turn
        # stream's SUBAGENT_STREAM_TEXT=false default) — only the main
        # agent's messages surface as background replies.
        if getattr(message, "parent_tool_use_id", None) is not None:
            return None
        texts = [
            block.text
            for block in (message.content or [])
            if isinstance(block, TextBlock) and getattr(block, "text", "")
        ]
        if not texts:
            return None
        # Delivered immediately, one event per (complete) assistant message:
        # a re-invocation that narrates progress across several messages
        # surfaces each as its own reply instead of buffering minutes of
        # silence until the run's ResultMessage.
        return {"type": "assistant_message", "text": "".join(texts)}
    if isinstance(message, ResultMessage):
        return {
            "type": "turn_result",
            "subtype": getattr(message, "subtype", None),
            "is_error": bool(getattr(message, "is_error", False)),
        }
    if isinstance(message, SystemMessage):
        return None  # init/status/hook noise
    return None


_TASK_SUBTYPES = frozenset(
    {"task_started", "task_progress", "task_notification", "task_updated"}
)


def _duck_typed_event(message: Any) -> Optional[Dict[str, Any]]:
    """Best-effort conversion for dict-shaped messages (tests, raw chunks)."""
    if not isinstance(message, dict):
        return None
    subtype = message.get("subtype")
    if subtype in _TASK_SUBTYPES:
        event = {k: v for k, v in message.items() if k != "subtype"}
        event["type"] = subtype
        return event
    return None


def _message_tool_use_id(message: Any) -> Optional[str]:
    """The spawning tool call's id on an SDK task message, if the CLI sent one.

    ``TaskUpdatedMessage`` has no such attribute, so fall back to the raw CLI
    payload the SDK keeps under ``data``.
    """
    tool_use_id = getattr(message, "tool_use_id", None)
    if not tool_use_id:
        data = getattr(message, "data", None)
        if isinstance(data, dict):
            tool_use_id = data.get("tool_use_id")
    return tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None


# ---------------------------------------------------------------------------
# Agent identity: names from Agent/Task tool calls
# ---------------------------------------------------------------------------


def _agent_spawns(content: Any) -> List[Tuple[str, str]]:
    """``(tool_use_id, name)`` for each named Agent/Task call in *content*.

    Blocks are SDK ``ToolUseBlock`` objects on real messages (the Claude
    client's ``_convert_message`` converts the message, not its blocks) and
    plain dicts in raw chunks.
    """
    spawns: List[Tuple[str, str]] = []
    if not isinstance(content, list):
        return spawns
    for block in content:
        if isinstance(block, dict):
            if block.get("type") != "tool_use":
                continue
            tool = block.get("name")
            block_id = block.get("id")
            tool_input = block.get("input")
        else:
            tool = getattr(block, "name", None)
            block_id = getattr(block, "id", None)
            tool_input = getattr(block, "input", None)
        if tool not in SUBAGENT_TOOL_NAMES:
            continue
        if not isinstance(block_id, str) or not block_id:
            continue
        name = tool_input.get("name") if isinstance(tool_input, dict) else None
        if isinstance(name, str) and 0 < len(name) <= _AGENT_NAME_MAX_LEN:
            spawns.append((block_id, name))
    return spawns


def _note_agent_spawns(session, content: Any) -> None:
    """Record the names of the Agent/Task calls in *content*. Never raises."""
    try:
        spawns = _agent_spawns(content)
        if not spawns:
            return
        outbox = get_outbox(session)
        for tool_use_id, name in spawns:
            outbox.note_agent_spawn(tool_use_id, name)
    except Exception:  # never let tracking break a stream
        logger.debug("agent spawn tracking failed", exc_info=True)


def apply_turn_task_chunk(session, chunk: Any) -> None:
    """Track task lifecycle chunks streamed *inside* a turn.

    A task started during a turn outlives it with the idle reader off, so
    without this the poll endpoint reports no active tasks until the job next
    emits an event — which a silent ``run_in_background`` Bash job may never
    do before its terminal patch. The turn paths feed every chunk through
    here (via ``_capture_pending_tool_questions``) to keep the registry warm.

    Assistant chunks are scanned too: a spawning Agent/Task call streams
    before its ``task_started`` and carries the ``name`` the registry reports.

    Registry only: the turn's own stream already delivered these events to
    the client, so nothing is appended to the outbox event buffer.
    """
    if not isinstance(chunk, dict):
        return
    if chunk.get("subtype") not in _TASK_SUBTYPES:
        if chunk.get("type") == "assistant":
            _note_agent_spawns(session, chunk.get("content"))
        return
    event = _duck_typed_event(chunk)
    if event is None or not event.get("task_id"):
        return
    # Converted SDK chunks keep the raw CLI payload under ``data`` and may
    # carry terminal status only inside the ``task_updated`` patch.
    data = chunk.get("data") if isinstance(chunk.get("data"), dict) else {}
    for key in ("task_type", "subagent_type", "tool_use_id"):
        if not event.get(key) and data.get(key):
            event[key] = data[key]
    if event["type"] == "task_updated" and not event.get("status"):
        patch = event.get("patch") if isinstance(event.get("patch"), dict) else {}
        if patch.get("status"):
            event["status"] = patch["status"]
    try:
        get_outbox(session).apply_task_event(event)
    except Exception:  # never let tracking break the turn stream
        logger.debug("turn task chunk tracking failed", exc_info=True)


# ---------------------------------------------------------------------------
# Idle reader lifecycle
# ---------------------------------------------------------------------------


def _handle_idle_message(session, message: Any) -> None:
    # Agent/Task calls name the tasks they spawn — including calls made
    # inside a subagent, whose messages are otherwise not forwarded.
    if isinstance(message, dict):
        if message.get("type") == "assistant":
            _note_agent_spawns(session, message.get("content"))
    else:
        _note_agent_spawns(session, getattr(message, "content", None))
    event = _message_to_event(message)
    if event is None:
        return
    outbox = get_outbox(session)
    if event["type"] == "task_started":
        event["name"] = outbox.agent_name_for(
            event.get("task_id"), event.get("tool_use_id")
        )
    stamped = outbox.append(event)
    if stamped["type"].startswith("task_"):
        outbox.apply_task_event(stamped)
    # Background output is live activity: keep the session (and with it the
    # SDK client owning the background process) from expiring mid-task.
    with suppress(Exception):
        session.touch()
    logger.debug(
        "[idle-reader] session=%s captured %s seq=%d",
        getattr(session, "session_id", "?"),
        stamped["type"],
        stamped["seq"],
    )


def _reader_failure_ends_stream(exc: BaseException) -> bool:
    """Whether an idle-reader failure means the client's stream is dead.

    The SDK re-raises its reader's fatal error — whatever type the transport
    raised — and then ends the stream. ``MessageParseError`` is the exception:
    it comes from the per-message parse layer above a stream that keeps
    flowing (its CLI and tasks are fine, and a restarted reader resumes).
    """
    try:
        from claude_agent_sdk._errors import MessageParseError
    except Exception:  # pragma: no cover - SDK stubbed or reorganized
        return True
    return not isinstance(exc, MessageParseError)


async def _idle_pump(session, client) -> None:
    """Consume ``client.receive_messages()`` until stopped or stream end."""
    stop_event: asyncio.Event = session.idle_reader_stop
    get_next: Optional[asyncio.Task] = None
    stop_wait: Optional[asyncio.Task] = None
    try:
        message_iter = client.receive_messages().__aiter__()
        while True:
            if get_next is None:
                get_next = asyncio.ensure_future(message_iter.__anext__())
            stop_wait = asyncio.ensure_future(stop_event.wait())
            done, _pending = await asyncio.wait(
                {get_next, stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            stop_wait.cancel()
            with suppress(asyncio.CancelledError):
                await stop_wait
            stop_wait = None

            if get_next in done:
                try:
                    message = get_next.result()
                except StopAsyncIteration:
                    # The SDK ends the stream once its reader is done (the CLI
                    # exited): no task can report its end any more.
                    reset_active_tasks(session, "client stream end")
                    return  # client stream closed
                get_next = None
                _handle_idle_message(session, message)
                if stop_event.is_set():
                    return
                continue

            # Stop requested with a read still pending: grace-wait so a
            # message mid-handoff is captured instead of lost, then cancel.
            done, _pending = await asyncio.wait({get_next}, timeout=_STOP_GRACE_S)
            if get_next in done:
                with suppress(Exception):
                    _handle_idle_message(session, get_next.result())
                get_next = None
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Disconnects surface here (ClosedResourceError etc.) — quiet exit,
        # but leave a marker so pollers can tell the stream broke.
        logger.debug(
            "[idle-reader] session=%s exited: %s",
            getattr(session, "session_id", "?"),
            exc,
        )
        with suppress(Exception):
            get_outbox(session).append({"type": "reader_error", "message": str(exc)})
        if _reader_failure_ends_stream(exc):
            # Without the stream no task can report its end any more.
            reset_active_tasks(session, "idle reader error")
    finally:
        for leftover in (get_next, stop_wait):
            if leftover is not None and not leftover.done():
                leftover.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await leftover
        if getattr(session, "idle_reader_task", None) is asyncio.current_task():
            session.idle_reader_task = None


def idle_reader_running(session) -> bool:
    task = getattr(session, "idle_reader_task", None)
    return task is not None and not task.done()


async def drain_backlog_to_outbox(session, client) -> int:
    """Route unread SDK messages into the outbox before a turn reader starts.

    A reader gap — a consumer disconnect whose stream teardown was itself
    cancelled, an AskUserQuestion pause (the idle reader is gated off while
    ``pending_tool_call`` is set), or a crashed idle pump — leaves whatever
    the CLI emitted sitting unread in the SDK's bounded message stream. The
    next turn's ``receive_response()`` would otherwise drain that backlog
    into *its* response: stale tool events surface as the new turn's
    activity, stale assistant text prepends to the new answer, and a stale
    ResultMessage terminates the new turn's read before its real output
    arrives.

    Call sites must run before the new turn can produce messages of its own
    (before ``client.query()`` / before the AskUserQuestion wake-up), so
    everything captured here is by construction between-turn output. Never
    raises; returns the number of captured messages.
    """
    if client is None or not hasattr(client, "receive_messages"):
        return 0
    captured = 0
    get_next: Optional[asyncio.Task] = None
    try:
        message_iter = client.receive_messages().__aiter__()
        while captured < OUTBOX_MAX_EVENTS:
            get_next = asyncio.ensure_future(message_iter.__anext__())
            done, _pending = await asyncio.wait({get_next}, timeout=_DRAIN_QUIET_S)
            if get_next not in done:
                # Stream quiet — grant the same grace the idle pump's stop
                # protocol grants, so a message mid-handoff is captured
                # instead of lost to the cancel-during-delivery window.
                done, _pending = await asyncio.wait({get_next}, timeout=_STOP_GRACE_S)
                if get_next not in done:
                    return captured  # backlog drained; finally cancels the read
            try:
                message = get_next.result()
            except StopAsyncIteration:
                get_next = None
                return captured  # client stream closed
            get_next = None
            _handle_idle_message(session, message)
            captured += 1
        return captured
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "[backlog-drain] session=%s sweep failed",
            getattr(session, "session_id", "?"),
            exc_info=True,
        )
        return captured
    finally:
        if get_next is not None:
            if get_next.done():
                # Landed between the timeout and cleanup — capture, don't drop.
                with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    _handle_idle_message(session, get_next.result())
                    captured += 1
            else:
                get_next.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await get_next
        if captured:
            # Count = messages consumed off the stream; noise (SystemMessage,
            # subagent narration) is swept but not forwarded to the outbox.
            logger.info(
                "[backlog-drain] session=%s swept %d stale message(s)",
                getattr(session, "session_id", "?"),
                captured,
            )


def resume_idle_reader(session) -> bool:
    """Start the idle reader if the session is between turns. Idempotent.

    Gates: a connected SDK client that exposes ``receive_messages`` (Claude
    backend only), no in-flight response, and no paused AskUserQuestion turn
    (its continuation will read the client next, not us).
    """
    client = getattr(session, "client", None)
    if client is None or not hasattr(client, "receive_messages"):
        return False
    if getattr(session, "active_response_id", None) is not None:
        return False
    if getattr(session, "pending_tool_call", None) is not None:
        return False
    if idle_reader_running(session):
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    session.idle_reader_stop = asyncio.Event()
    session.idle_reader_task = loop.create_task(
        _idle_pump(session, client),
        name=f"idle-reader:{getattr(session, 'session_id', '?')}",
    )
    logger.debug(
        "[idle-reader] session=%s started", getattr(session, "session_id", "?")
    )
    return True


def resume_idle_reader_between_turns(session) -> bool:
    """:func:`resume_idle_reader` for callers outside the turn paths (HTTP).

    A non-streaming turn reads the client while holding ``session.lock``
    without publishing ``active_response_id``, so the base gates cannot see
    it, and a reader started then would split that turn's message stream. The
    turn paths themselves resume the reader while still holding the lock,
    which is why this check lives here rather than in ``resume_idle_reader``.
    """
    lock = getattr(session, "lock", None)
    if lock is not None and lock.locked():
        return False
    return resume_idle_reader(session)


async def pause_idle_reader(session) -> None:
    """Stop the idle reader and wait until it no longer reads the client.

    Must complete before a turn reader touches the client. No-op when the
    reader is not running.
    """
    task = getattr(session, "idle_reader_task", None)
    if task is None or task.done():
        if task is not None:
            session.idle_reader_task = None
        return
    stop_event = getattr(session, "idle_reader_stop", None)
    if stop_event is not None:
        stop_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_PAUSE_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "[idle-reader] session=%s did not stop within %.1fs; cancelling",
            getattr(session, "session_id", "?"),
            _PAUSE_TIMEOUT_S,
        )
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    except Exception:
        pass  # pump's own error handling already logged it
    if getattr(session, "idle_reader_task", None) is task:
        session.idle_reader_task = None


def stop_idle_reader_nowait(session) -> None:
    """Fire-and-forget stop for sync teardown paths (session deletion)."""
    stop_event = getattr(session, "idle_reader_stop", None)
    if stop_event is not None:
        with suppress(Exception):
            stop_event.set()
    task = getattr(session, "idle_reader_task", None)
    if task is not None and not task.done():
        task.cancel()
    if task is not None:
        session.idle_reader_task = None
