"""SSE event builders for the OpenAI Responses API wire format."""

import json
from typing import Any, Dict, Optional

from claude_agent_sdk.types import ToolResultBlock


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

    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _build_task_event(chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a structured task event dict from a system chunk, or None."""
    subtype = chunk.get("subtype")
    if subtype == "task_started":
        return {
            "type": "task_started",
            "task_id": chunk.get("task_id", ""),
            "description": chunk.get("description", ""),
            "session_id": chunk.get("session_id", ""),
        }
    if subtype == "task_progress":
        return {
            "type": "task_progress",
            "task_id": chunk.get("task_id", ""),
            "description": chunk.get("description", ""),
            "last_tool_name": chunk.get("last_tool_name"),
            "usage": chunk.get("usage"),
        }
    if subtype == "task_notification":
        return {
            "type": "task_notification",
            "task_id": chunk.get("task_id", ""),
            "status": chunk.get("status", ""),
            "summary": chunk.get("summary", ""),
            "usage": chunk.get("usage"),
        }
    return None


def make_task_response_sse(task_event: Dict[str, Any], *, sequence_number: int = 0) -> str:
    """Build an SSE line for Responses API with a custom task event type."""
    event_type = f"response.{task_event['type']}"
    data = {**task_event, "type": event_type, "sequence_number": sequence_number}
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


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
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _normalize_tool_result(result_block) -> Dict[str, Any]:
    """Normalize a ToolResultBlock or dict into a plain tool_result dict."""
    if isinstance(result_block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": result_block.tool_use_id or "",
            "content": result_block.content or "",
            "is_error": bool(result_block.is_error),
        }
    if hasattr(result_block, "tool_use_id"):
        return {
            "type": "tool_result",
            "tool_use_id": getattr(result_block, "tool_use_id", "") or "",
            "content": getattr(result_block, "content", "") or "",
            "is_error": bool(getattr(result_block, "is_error", False)),
        }
    if isinstance(result_block, dict):
        return {
            "type": "tool_result",
            "tool_use_id": result_block.get("tool_use_id", ""),
            "content": result_block.get("content", ""),
            "is_error": bool(result_block.get("is_error", False)),
        }
    return {
        "type": "tool_result",
        "tool_use_id": "",
        "content": str(result_block),
        "is_error": False,
    }


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
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


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
    return f"event: response.output_item.added\ndata: {json.dumps(event_data)}\n\n"
