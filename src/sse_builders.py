"""SSE event builders for the OpenAI Responses API wire format."""

import json
import re
from typing import Any, Dict, Optional

from src.constants import STREAM_COMPACTION_EVENTS, STREAM_HOOK_EVENTS
from src.content_blocks import normalize_tool_result_for_sse


def _sse_dumps(data: Any) -> str:
    """JSON-serialize SSE event data, tolerating non-serializable values.

    Some SDK payloads passed through verbatim (e.g. a ``task_updated`` registry
    ``patch``, which can carry callables from in-process teammates/background
    tasks) contain values that are not JSON-serializable. Coerce those to their
    string form via ``default=str`` so a single stray field degrades gracefully
    instead of raising ``TypeError`` and killing the entire SSE stream.
    """
    return json.dumps(data, default=str)


def make_response_sse(
    event_type: str,
    response_obj: Optional[Any] = None,
    *,
    sequence_number: int = 0,
    **kwargs,
) -> str:
    """Build a single SSE-formatted line for OpenAI Responses API.

    Uses proper SSE wire format: event: <type>\\ndata: <json>\\n\\n
    """
    data: Dict[str, Any] = {"type": event_type}
    if response_obj:
        if hasattr(response_obj, "model_dump"):
            data["response"] = response_obj.model_dump(mode="json", exclude_none=True)
        else:
            data["response"] = response_obj

    for key, value in kwargs.items():
        if hasattr(value, "model_dump"):
            data[key] = value.model_dump(mode="json", exclude_none=True)
        else:
            data[key] = value

    data["sequence_number"] = sequence_number

    return f"event: {event_type}\ndata: {_sse_dumps(data)}\n\n"


def _build_task_event(chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a structured task event dict from a system chunk, or None.

    Nesting reference: the Claude SDK's ``TaskStarted/Progress/Notification``
    messages carry ``tool_use_id`` — the id of the orchestrator's ``Task``
    tool_use that spawned this subagent. That is exactly the node the chat UI
    nests progress under, so we surface it as ``parent_tool_use_id`` (the field
    every other event uses for attribution). An explicit ``parent_tool_use_id``
    on the chunk still wins, for forward-compatibility and synthetic callers.
    ``task_updated`` is a task-registry patch and carries neither id on the
    wire; the same derivation applies if a caller supplies them.
    """
    subtype = chunk.get("subtype")
    tool_use_id = chunk.get("tool_use_id")
    parent_tool_use_id = chunk.get("parent_tool_use_id") or tool_use_id
    # SDK-typed chunks put task_type at the top level and keep the raw CLI
    # payload (incl. subagent_type) under ``data``; raw dict chunks carry both
    # at the top level.
    data = chunk.get("data") if isinstance(chunk.get("data"), dict) else {}
    if subtype == "task_started":
        return {
            "type": "task_started",
            "task_id": chunk.get("task_id", ""),
            "description": chunk.get("description", ""),
            "session_id": chunk.get("session_id", ""),
            "task_type": chunk.get("task_type") or data.get("task_type"),
            "subagent_type": chunk.get("subagent_type") or data.get("subagent_type"),
            "tool_use_id": tool_use_id,
            "parent_tool_use_id": parent_tool_use_id,
        }
    if subtype == "task_progress":
        return {
            "type": "task_progress",
            "task_id": chunk.get("task_id", ""),
            "description": chunk.get("description", ""),
            "last_tool_name": chunk.get("last_tool_name"),
            "usage": chunk.get("usage"),
            "tool_use_id": tool_use_id,
            "parent_tool_use_id": parent_tool_use_id,
        }
    if subtype == "task_notification":
        return {
            "type": "task_notification",
            "task_id": chunk.get("task_id", ""),
            "status": chunk.get("status", ""),
            "summary": chunk.get("summary", ""),
            "usage": chunk.get("usage"),
            "tool_use_id": tool_use_id,
            "parent_tool_use_id": parent_tool_use_id,
        }
    if subtype == "task_updated":
        # Terminal state (completed/failed/killed) can arrive ONLY as a
        # task_updated patch with no task_notification (e.g. a task killed via
        # TaskStop, background tasks, in-process teammates), so dropping this
        # subtype loses the task's ending. Status/patch are passed through raw
        # (``killed`` is not mapped to ``stopped``), per gateway philosophy.
        patch = chunk.get("patch") if isinstance(chunk.get("patch"), dict) else {}
        return {
            "type": "task_updated",
            "task_id": chunk.get("task_id", ""),
            "status": chunk.get("status") or patch.get("status"),
            "patch": patch,
            "session_id": chunk.get("session_id"),
            "tool_use_id": tool_use_id,
            "parent_tool_use_id": parent_tool_use_id,
        }
    return None


def make_task_response_sse(task_event: Dict[str, Any], *, sequence_number: int = 0) -> str:
    """Build an SSE line for Responses API with a custom task event type."""
    event_type = f"response.{task_event['type']}"
    data = {**task_event, "type": event_type, "sequence_number": sequence_number}
    return f"event: {event_type}\ndata: {_sse_dumps(data)}\n\n"


# Detection markers for a teammate's message injected as a user message.
_TEAMMATE_MESSAGE_MARKERS = (
    "Another Claude session sent a message",
    "This came from another Claude session",
)

# The sending teammate's address as it appears in the injected body. On the
# pinned CLI (2.1.220) the body carries a ``from=<address>`` marker; newer CLIs
# frame the body in a ``<teammate-message teammate_id="...">`` tag instead, so
# both are tried. At least one address character is required so the literal
# ``from=`` in the trailing reply guidance — immediately followed by a
# backtick — is not read as an address, and ``:`` is excluded so a
# ``from=name: message`` framing yields ``name``.
_TEAMMATE_FROM_RE = re.compile(r"\bfrom=([A-Za-z0-9._-]+)")
_TEAMMATE_TAG_ID_RE = re.compile(r'<teammate-message\b[^>]*\bteammate_id="([^"]+)"')


def is_teammate_message_text(text: Any) -> bool:
    """True when a user-message text is an agent-team teammate's message.

    Agent teams (experimental, gated behind the CLI's
    ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS``) deliver a teammate's
    ``SendMessage`` to the leader by injecting it into the leader's transcript
    as a plain ``user`` message — there is no dedicated SDK message type, so
    detection is by content. This is the CLI's own check: the opener (which has
    a ``while you were working`` variant, hence the shorter prefix) plus the
    explanation sentence must both be present.
    """
    if not isinstance(text, str):
        return False
    return all(marker in text for marker in _TEAMMATE_MESSAGE_MARKERS)


def parse_teammate_message_from(text: str) -> Optional[str]:
    """Best-effort address of the sending teammate, else None.

    The exact body framing is the CLI's and not fully pinned down, so a missing
    address is expected rather than an error.
    """
    match = _TEAMMATE_TAG_ID_RE.search(text) or _TEAMMATE_FROM_RE.search(text)
    return match.group(1) if match else None


def make_teammate_message_response_sse(
    text: str,
    *,
    session_id: Optional[str] = None,
    sequence_number: int = 0,
) -> str:
    """Build an SSE line for a teammate's message to the leader session.

    ``text`` is the injected user-message text passed through verbatim,
    CLI framing and all: the gateway does not re-word or trim what the SDK
    produced, and a client that renders it owns that presentation choice.
    """
    event_type = "response.teammate_message"
    data = {
        "type": event_type,
        "text": text,
        "from": parse_teammate_message_from(text),
        "session_id": session_id,
        "sequence_number": sequence_number,
    }
    return f"event: {event_type}\ndata: {_sse_dumps(data)}\n\n"


def _build_progress_event(chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a liveness ("still working") event dict from a system chunk, or None.

    Complements :func:`_build_task_event` (which only handles subagent task
    messages). Surfaces two more classes of progress that the SDK reports as
    ``system`` messages but the gateway previously dropped:

    * Hook lifecycle (``hook_started`` / ``hook_response``) — emitted when
      ``include_hook_events`` is on. Becomes ``response.hook_event`` carrying
      the hook name (e.g. ``PreToolUse``), the tool it fired for, and the run
      phase, so a UI can show "running <tool>…" / "<tool> finished".
    * Context compaction (``compact_boundary`` / ``compaction``) — becomes
      ``response.compaction`` so a UI can show "compacting context…" during the
      otherwise-silent pause.

    Returns ``None`` for any other subtype (caller leaves it dropped) and when
    the corresponding feature flag is disabled.
    """
    subtype = chunk.get("subtype")
    if subtype in ("hook_started", "hook_response"):
        if not STREAM_HOOK_EVENTS:
            return None
        data = chunk.get("data") if isinstance(chunk.get("data"), dict) else {}
        hook_event_name = (
            chunk.get("hook_event_name")
            or data.get("hook_event")
            or data.get("hook_event_name")
            or data.get("hook_name")
            or ""
        )
        tool_use_id = data.get("tool_use_id") or chunk.get("tool_use_id")
        event = {
            "type": "hook_event",
            "phase": subtype,  # "hook_started" | "hook_response"
            "hook_event_name": hook_event_name,
            "tool_name": data.get("tool_name") or data.get("tool"),
            "tool_use_id": tool_use_id,
            "outcome": data.get("outcome"),  # present on hook_response
            "session_id": chunk.get("session_id"),
        }
        if chunk.get("parent_tool_use_id") is not None:
            event["parent_tool_use_id"] = chunk.get("parent_tool_use_id")
        return event
    if subtype in ("compact_boundary", "compaction"):
        if not STREAM_COMPACTION_EVENTS:
            return None
        data = chunk.get("data") if isinstance(chunk.get("data"), dict) else {}
        trigger = data.get("trigger")
        if trigger is None and isinstance(data.get("compact_metadata"), dict):
            trigger = data["compact_metadata"].get("trigger")
        return {
            "type": "compaction",
            "subtype": subtype,
            "trigger": trigger,
            "session_id": chunk.get("session_id"),
        }
    return None


def make_tool_use_started_response_sse(
    tool_use_id: str,
    name: str,
    *,
    sequence_number: int = 0,
    parent_tool_use_id: Optional[str] = None,
) -> str:
    """Build an SSE line announcing a tool call is starting.

    Emitted at ``content_block_start`` for a tool_use block — before its JSON
    arguments finish streaming — so a UI can show "preparing <tool>…" instead
    of a silent gap while a large tool input is generated. The matching
    ``response.tool_use`` (same ``tool_use_id``) arrives once the input is
    complete.
    """
    event_type = "response.tool_use_started"
    data = {
        "type": event_type,
        "tool_use_id": tool_use_id or "",
        "name": name or "",
        "sequence_number": sequence_number,
    }
    if parent_tool_use_id:
        data["parent_tool_use_id"] = parent_tool_use_id
    return f"event: {event_type}\ndata: {_sse_dumps(data)}\n\n"


def make_tool_use_response_sse(
    tool_block: Dict[str, Any],
    *,
    sequence_number: int = 0,
    parent_tool_use_id: Optional[str] = None,
) -> str:
    """Build an SSE line for a tool_use block as a structured event."""
    event_type = "response.tool_use"
    data = {
        "type": event_type,
        "tool_use_id": tool_block.get("id", ""),
        "name": tool_block.get("name", ""),
        "input": tool_block.get("input", {}),
        "sequence_number": sequence_number,
    }
    if parent_tool_use_id:
        data["parent_tool_use_id"] = parent_tool_use_id
    return f"event: {event_type}\ndata: {_sse_dumps(data)}\n\n"


def _normalize_tool_result(result_block) -> Dict[str, Any]:
    """Normalize a ToolResultBlock or dict into a plain tool_result dict."""
    return normalize_tool_result_for_sse(result_block)


def make_tool_result_response_sse(
    result_block,
    *,
    sequence_number: int = 0,
    parent_tool_use_id: Optional[str] = None,
) -> str:
    """Build an SSE line for a tool_result block as a structured event."""
    event_type = "response.tool_result"
    data = _normalize_tool_result(result_block)
    data["type"] = event_type
    data["sequence_number"] = sequence_number
    if parent_tool_use_id:
        data["parent_tool_use_id"] = parent_tool_use_id
    return f"event: {event_type}\ndata: {_sse_dumps(data)}\n\n"


def make_function_call_response_sse(
    response_id: str,
    call_id: str,
    name: str,
    arguments: str,
) -> str:
    """Build SSE events for a function_call output item (e.g. AskUserQuestion).

    Emits response.output_item.added with the function_call data.
    """
    item = {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }
    event_data = {
        "type": "response.output_item.added",
        "response_id": response_id,
        "item": item,
    }
    return f"event: response.output_item.added\ndata: {_sse_dumps(event_data)}\n\n"
