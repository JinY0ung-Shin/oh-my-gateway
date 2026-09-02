"""Codex app-server notification -> canonical gateway chunk mapping.

This is the vendor-translation core of issue #173: it turns native Codex
thread/turn/item notifications (as delivered by ``AppServerTransport``) into the
plain ``dict`` chunks that ``streaming_utils.stream_response_chunks`` already
knows how to render into the canonical ``/v1/responses`` SSE contract. The same
frontend reducer therefore renders Claude and Codex without learning Codex RPC.

The mapping is deliberately conservative (issue §2): a native primitive is
translated only where the semantics actually match. Fields Codex does not
expose are left absent rather than faked. Method-name spellings follow the
current in-tree app-server conventions and the caveat in ``docs/codex`` -- they
should be rechecked when the app-server protocol changes.

One :class:`TurnMapper` instance maps exactly one turn. It is fed each
subscriber notification via :meth:`map_notification`, and the terminal
:meth:`finish` (or a ``turn/completed`` notification) yields the completion
chunks. All state (accumulated agent-message items, token usage, open reasoning
block) is per turn, so a new turn always starts from a fresh mapper.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional

from src.backends.common import completion_chunks, error_chunk
from src.backends.appserver.subagents import SubAgentEvent, normalize_subagent_event

# Native item types the adapter surfaces as tool activity. Everything else is
# either visible text (``agentMessage``), reasoning, or accumulation-only.
TOOL_ITEM_TYPES = {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}

# Reasoning delta methods. The app-server may emit reasoning either as a visible
# summary stream or as raw reasoning; both map to the canonical reasoning
# (thinking) channel. Absent from a given build => no reasoning events, which is
# the correct "do not fake" behavior.
REASONING_DELTA_METHODS = {
    "item/reasoning/delta",
    "item/reasoningSummary/delta",
    "item/agentReasoning/delta",
    "item/agentReasoningSummary/delta",
}

# Wrapper stream_event helpers -------------------------------------------------


def _text_delta(text: str) -> Dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _thinking_start() -> Dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "content_block": {"type": "thinking"},
        },
    }


def _thinking_delta(text: str) -> Dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    }


def _thinking_stop() -> Dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_stop"},
    }


class TurnMapper:
    """Maps one Codex turn's notifications into canonical gateway chunks.

    Args:
        thread_id: the Codex thread this turn belongs to (identity/dedup).
        turn_id: the Codex turn id; notifications for other turns are ignored so
            a stray notification from a sibling turn can never contaminate this
            stream.
    """

    def __init__(self, *, thread_id: Optional[str], turn_id: Optional[str]) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._items: List[Dict[str, Any]] = []
        self._usage: Optional[Dict[str, int]] = None
        self._in_reasoning = False
        self._finished = False
        self._errored = False
        # Live child subagents (child thread id -> parent thread id), so a turn
        # that terminalizes with children still open never leaves a
        # forever-running task row in the UI (issue §5).
        self._open_subagents: Dict[str, Optional[str]] = {}

    # -- public API --------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def succeeded(self) -> bool:
        """True once the turn reached a clean (non-error) terminal state."""
        return self._finished and not self._errored

    def map_notification(self, method: str, params: Any) -> Iterator[Dict[str, Any]]:
        """Yield canonical chunks for one native notification.

        A ``turn/completed`` for this turn drives the mapper terminal and emits
        the completion (or error) chunks; callers stop consuming once
        :attr:`finished` is set.
        """
        if self._finished or not isinstance(params, dict):
            return

        turn = params.get("turn") if isinstance(params.get("turn"), dict) else None
        notification_turn_id = params.get("turnId")
        if turn is not None:
            notification_turn_id = turn.get("id") or notification_turn_id

        # Subagent lifecycle is handled BEFORE the turn-id gate: child threads
        # run their own turns, so their notifications carry a different turnId
        # and would otherwise be dropped as sibling-turn traffic.
        subagent = normalize_subagent_event(method, params)
        if subagent is not None:
            yield from self._map_subagent(subagent)
            return

        # Thread-idle is a terminal fallback for a turn that never emitted an
        # explicit ``turn/completed`` (matches the frozen backend's safety net).
        if self._is_thread_idle(method, params):
            yield from self.finish()
            return

        # A turn id that does not match ours is a sibling turn's traffic.
        if self.turn_id is not None and notification_turn_id not in (
            None,
            self.turn_id,
        ):
            return

        if method == "item/agentMessage/delta":
            yield from self._close_reasoning()
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                yield _text_delta(delta)
            return

        if method in REASONING_DELTA_METHODS:
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                yield from self._open_reasoning()
                yield _thinking_delta(delta)
            return

        if method == "item/started":
            yield from self._close_reasoning()
            tool_use = self._tool_use_from_item(params.get("item"))
            if tool_use:
                yield {"type": "assistant", "content": [tool_use]}
            return

        if method == "item/completed":
            item = params.get("item")
            if not isinstance(item, dict):
                return
            tool_result = self._tool_result_from_item(item)
            if tool_result:
                yield from self._close_reasoning()
                yield {"type": "user", "content": [tool_result]}
                return
            # A non-tool item (typically agentMessage) accumulates so its text
            # can become the final answer at turn completion.
            self._items.append(item)
            return

        if method == "thread/tokenUsage/updated":
            usage = self._extract_usage(params.get("tokenUsage"))
            if usage is not None:
                self._usage = usage
            return

        if method == "turn/completed":
            if isinstance(turn, dict) and turn.get("status") == "failed":
                yield from self._close_reasoning()
                yield from self.drain_open_subagents("failed")
                self._finished = True
                self._errored = True
                yield error_chunk(self._turn_error_message(turn))
                return
            yield from self.finish()
            return

    def finish(self) -> Iterator[Dict[str, Any]]:
        """Emit the terminal completion chunks for a normally-ended turn."""
        if self._finished:
            return
        yield from self._close_reasoning()
        # A clean turn end with children still open is anomalous; retire them as
        # ``stopped`` so no task row is left running forever.
        yield from self.drain_open_subagents("stopped")
        self._finished = True
        final_text = self._final_response_from_items() or ""
        yield from completion_chunks(final_text, self._usage)

    # -- subagent mapping -------------------------------------------------

    def _map_subagent(self, event: SubAgentEvent) -> Iterator[Dict[str, Any]]:
        """Map a normalized subagent event into canonical ``task_*`` chunks.

        Child identity (``task_id``) and the parent thread id are preserved so
        nested agents render under the correct parent; absent fields stay absent.
        """
        if event.kind == "spawned":
            if event.child_id in self._open_subagents:
                yield self._task_chunk("task_progress", event)
                return
            self._open_subagents[event.child_id] = event.parent_id
            yield self._task_chunk("task_started", event)
            return
        if event.kind == "progress":
            yield self._task_chunk("task_progress", event)
            return
        # terminal
        self._open_subagents.pop(event.child_id, None)
        status = event.status or "stopped"
        if status == "completed":
            yield self._task_chunk("task_notification", event, status=status)
        else:
            yield self._task_chunk("task_updated", event, status=status)

    def drain_open_subagents(self, status: str) -> Iterator[Dict[str, Any]]:
        """Terminalize every still-open child (turn ended / interrupted / lost).

        Emits one ``task_updated`` per open child so the UI never shows a
        forever-running descendant row. Idempotent: the open set is cleared.
        """
        if not self._open_subagents:
            return
        open_children = list(self._open_subagents.items())
        self._open_subagents.clear()
        for child_id, parent_id in open_children:
            yield self._task_chunk(
                "task_updated",
                SubAgentEvent(kind="terminal", child_id=child_id, parent_id=parent_id),
                status=status,
            )

    def _task_chunk(
        self,
        subtype: str,
        event: SubAgentEvent,
        *,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        chunk: Dict[str, Any] = {
            "type": "system",
            "subtype": subtype,
            "task_id": event.child_id,
            "task_type": "local_agent",
            # Nest under the parent thread: the route surfaces
            # ``parent_tool_use_id`` as the attribution node, and Codex nests by
            # thread tree, so the parent thread id is the correct node id.
            "parent_tool_use_id": event.parent_id,
        }
        if event.description is not None:
            chunk["description"] = event.description
        if event.role is not None:
            chunk["subagent_type"] = event.role
        if subtype == "task_notification":
            chunk["status"] = status or "completed"
        elif subtype == "task_updated":
            chunk["status"] = status
            chunk["patch"] = {"status": status}
        return chunk

    # -- reasoning block bookkeeping --------------------------------------

    def _open_reasoning(self) -> Iterator[Dict[str, Any]]:
        if not self._in_reasoning:
            self._in_reasoning = True
            yield _thinking_start()

    def _close_reasoning(self) -> Iterator[Dict[str, Any]]:
        if self._in_reasoning:
            self._in_reasoning = False
            yield _thinking_stop()

    # -- native item translation ------------------------------------------

    def _tool_use_from_item(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type not in TOOL_ITEM_TYPES:
            return None
        if not isinstance(item_id, str) or not item_id:
            return None
        tool_input = {
            k: v for k, v in item.items() if k not in {"id", "type", "aggregatedOutput"}
        }
        return {
            "type": "tool_use",
            "id": item_id,
            "name": str(item_type),
            "input": tool_input,
        }

    def _tool_result_from_item(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type not in TOOL_ITEM_TYPES:
            return None
        if not isinstance(item_id, str) or not item_id:
            return None
        status = str(item.get("status") or "")
        is_error = status in {"failed", "declined"}
        if item_type == "commandExecution":
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                is_error = True
            content = item.get("aggregatedOutput")
            if not isinstance(content, str) or not content:
                content = json.dumps(
                    {
                        "status": status,
                        "exitCode": exit_code,
                        "command": item.get("command"),
                    },
                    ensure_ascii=False,
                )
        else:
            content = json.dumps(
                {k: v for k, v in item.items() if k not in {"id", "type"}},
                ensure_ascii=False,
            )
        return {
            "type": "tool_result",
            "tool_use_id": item_id,
            "content": content,
            "is_error": is_error,
        }

    def _final_response_from_items(self) -> Optional[str]:
        last_unknown_phase: Optional[str] = None
        for item in reversed(self._items):
            if item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            if item.get("phase") == "final_answer":
                return text
            if item.get("phase") is None and last_unknown_phase is None:
                last_unknown_phase = text
        return last_unknown_phase

    def _extract_usage(self, token_usage: Any) -> Optional[Dict[str, int]]:
        if not isinstance(token_usage, dict):
            return None
        last = token_usage.get("last")
        if not isinstance(last, dict):
            return None
        input_tokens = int(last.get("inputTokens") or 0) + int(
            last.get("cachedInputTokens") or 0
        )
        # Reasoning tokens are billed as output so input + output == totalTokens.
        output_tokens = int(last.get("outputTokens") or 0) + int(
            last.get("reasoningOutputTokens") or 0
        )
        return {"input_tokens": input_tokens, "output_tokens": output_tokens}

    def _turn_error_message(self, turn: Dict[str, Any]) -> str:
        error = turn.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return "Codex turn failed"

    def _is_thread_idle(self, method: str, params: Dict[str, Any]) -> bool:
        if not self.thread_id or method != "thread/status/changed":
            return False
        if params.get("threadId") != self.thread_id:
            return False
        status = params.get("status")
        return isinstance(status, dict) and status.get("type") == "idle"
