"""Stateless Claude Agent SDK message streaming endpoint."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from src.agent_message_models import AgentMessagesRequest
from src.auth import security, verify_api_key
from src.constants import SSE_KEEPALIVE_INTERVAL
from src.mcp_config import get_mcp_servers
from src.rate_limiter import rate_limit_endpoint
from src.routes.deps import resolve_and_get_backend, validate_backend_auth_or_raise
from src.session_manager import Session, _session_jsonl_path
from src import streaming_utils
from src.workspace_manager import workspace_manager

logger = logging.getLogger(__name__)
router = APIRouter()

AGENT_MESSAGE_SCHEMA = "claude-agent-sdk-message-v1"
ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"
_MAX_SDK_EVENT_BYTES = 2 * 1024 * 1024 - 1024
_MAX_STRING_CHARS = 512 * 1024
_REDACTED = "***REDACTED***"
_DROP = object()

_SDK_BLOCK_TYPES = {
    "TextBlock": "text",
    "ThinkingBlock": "thinking",
    "ToolUseBlock": "tool_use",
    "ToolResultBlock": "tool_result",
    "ServerToolUseBlock": "server_tool_use",
    "ServerToolResultBlock": "server_tool_result",
}

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"
    r"[A-Z0-9_]*)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+\-/]+=*")
_CLI_SECRET_RE = re.compile(
    r"(?i)(--(?:api[-_]?key|token|password|secret)\s+)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_URL_USERINFO_RE = re.compile(r"(https?://[^/\s:@]+:)[^@\s/]+@", re.IGNORECASE)

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "xapikey",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "password",
        "secret",
        "clientsecret",
        "privatekey",
        "credential",
        "credentials",
    }
)

# SystemMessage keeps most version-specific fields in ``data`` in the Python
# SDK, while Noah's JavaScript SDK handlers read them from the envelope root.
# Flatten only fields used by those handlers instead of exposing every new SDK
# metadata field as a de-facto public contract.
_SYSTEM_DATA_FIELDS = frozenset(
    {
        "model",
        "plugins",
        "loadedPlugins",
        "loaded_plugins",
        "name",
        "status",
        "task_id",
        "description",
        "usage",
        "tool_use_id",
        "tool_name",
        "agent_id",
        "decision_reason",
        "message",
        "task_type",
        "subagent_type",
        "workflow_name",
        "prompt",
        "last_tool_name",
        "summary",
        "patch",
        "skip_transcript",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "type",
        "subtype",
        "is_error",
        "result",
        "structured_output",
        "usage",
        "model_usage",
        "modelUsage",
        "stop_reason",
        "api_error_status",
    }
)


def _compact_key(key: object) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _is_sensitive_key(key: object) -> bool:
    compact = _compact_key(key)
    return compact in _SENSITIVE_KEYS or compact.endswith(("token", "secret", "password"))


def _redact_sensitive_text(value: str) -> str:
    value = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}={_REDACTED}", value)
    value = _BEARER_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    value = _CLI_SECRET_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    return _URL_USERINFO_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}@", value)


def _normalize_sdk_value(
    value: Any,
    *,
    key: object = "",
    depth: int = 0,
    seen: Optional[set[int]] = None,
) -> Any:
    """Recursively turn SDK/Pydantic values into safe JSON primitives.

    ``ClaudeCodeCLI._convert_message`` deliberately performs only a shallow
    conversion for the existing Responses pipeline. This endpoint needs a
    recursive conversion because its public payload is the SDK envelope itself.
    Unknown objects are represented by their type name rather than ``str`` or
    ``repr`` so exception details, filesystem paths, and credentials cannot leak
    through a serialization fallback.
    """
    if _compact_key(key) in {"sessionid", "uuid", "outputfile", "signature"}:
        return _DROP
    if _is_sensitive_key(key):
        return _REDACTED
    if depth > 32:
        return "[maximum nesting depth exceeded]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        value = _redact_sensitive_text(value)
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return value[:_MAX_STRING_CHARS] + "\n... (truncated)"
    if isinstance(value, Enum):
        return _normalize_sdk_value(value.value, key=key, depth=depth + 1, seen=seen)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"[binary data: {len(value)} bytes]"

    if seen is None:
        seen = set()
    object_id = id(value)
    if object_id in seen:
        return "[circular reference]"
    seen.add(object_id)
    try:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python")
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            sdk_type = _SDK_BLOCK_TYPES.get(type(value).__name__)
            value = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
            if sdk_type is not None:
                value.setdefault("type", sdk_type)

        if isinstance(value, Mapping):
            result: Dict[str, Any] = {}
            for raw_key, raw_value in value.items():
                output_key = str(raw_key)
                normalized = _normalize_sdk_value(
                    raw_value,
                    key=output_key,
                    depth=depth + 1,
                    seen=seen,
                )
                if normalized is not _DROP:
                    result[output_key] = normalized
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            sequence_result: list[Any] = []
            for item in value:
                normalized = _normalize_sdk_value(item, depth=depth + 1, seen=seen)
                if normalized is not _DROP:
                    sequence_result.append(normalized)
            return sequence_result
        if hasattr(value, "__dict__"):
            public = {
                name: item
                for name, item in vars(value).items()
                if not name.startswith("_") and not callable(item)
            }
            return _normalize_sdk_value(public, key=key, depth=depth + 1, seen=seen)
        return f"[unsupported {type(value).__name__}]"
    finally:
        seen.discard(object_id)


def _camelize_model_usage(value: Any) -> Any:
    """Add the field spelling consumed by Noah's JS SDK usage handler."""
    if not isinstance(value, dict):
        return value
    mapped: Dict[str, Any] = {}
    for model, raw_usage in value.items():
        if not isinstance(raw_usage, dict):
            mapped[model] = raw_usage
            continue
        usage = dict(raw_usage)
        if "context_window" in usage and "contextWindow" not in usage:
            usage["contextWindow"] = usage["context_window"]
        if "max_output_tokens" in usage and "maxOutputTokens" not in usage:
            usage["maxOutputTokens"] = usage["max_output_tokens"]
        mapped[model] = usage
    return mapped


def _safe_plugins(value: Any) -> list[Any]:
    """Project plugin metadata to the name/status fields Noah displays."""
    if not isinstance(value, list):
        return []
    plugins: list[Any] = []
    for entry in value:
        if isinstance(entry, str):
            # SDK versions differ between returning a declared plugin name and
            # an installation path. Noah needs only the display name.
            name = re.split(r"[/\\]", entry.rstrip("/\\"))[-1]
            if name:
                plugins.append(name)
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = re.split(r"[/\\]", entry["name"].rstrip("/\\"))[-1]
        if not name:
            continue
        plugin = {"name": name}
        if isinstance(entry.get("status"), str):
            plugin["status"] = entry["status"]
        plugins.append(plugin)
    return plugins


def _project_user_content(value: Any) -> list[Dict[str, Any]]:
    """Keep tool lifecycle identity while dropping potentially secret output."""
    if not isinstance(value, list):
        return []
    projected: list[Dict[str, Any]] = []
    for block in value:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        item: Dict[str, Any] = {"type": "tool_result"}
        if isinstance(block.get("tool_use_id"), str):
            item["tool_use_id"] = block["tool_use_id"]
        if isinstance(block.get("is_error"), bool):
            item["is_error"] = block["is_error"]
        projected.append(item)
    return projected


def _project_assistant_content(value: Any) -> list[Dict[str, Any]]:
    """Expose answer/thinking blocks and the tool fields Noah renders."""
    if not isinstance(value, list):
        return []
    projected: list[Dict[str, Any]] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            projected.append({"type": "text", "text": block["text"]})
        elif block_type == "thinking" and isinstance(block.get("thinking"), str):
            projected.append({"type": "thinking", "thinking": block["thinking"]})
        elif block_type in {"tool_use", "server_tool_use"}:
            item = {
                field: block[field] for field in ("type", "id", "name", "input") if field in block
            }
            projected.append(item)
    return projected


def _project_stream_event(value: Any) -> Dict[str, Any]:
    """Keep only stream fields consumed by Noah, excluding tool-input JSON deltas."""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        return {}
    event_type = value["type"]
    projected: Dict[str, Any] = {"type": event_type}
    if isinstance(value.get("index"), int):
        projected["index"] = value["index"]

    if event_type == "content_block_delta" and isinstance(value.get("delta"), dict):
        delta = value["delta"]
        delta_type = delta.get("type")
        if delta_type == "text_delta" and isinstance(delta.get("text"), str):
            projected["delta"] = {"type": delta_type, "text": delta["text"]}
        elif delta_type == "thinking_delta" and isinstance(delta.get("thinking"), str):
            projected["delta"] = {"type": delta_type, "thinking": delta["thinking"]}
    elif event_type == "content_block_start" and isinstance(value.get("content_block"), dict):
        block = value["content_block"]
        projected["content_block"] = {
            field: block[field] for field in ("type", "id", "name") if field in block
        }
    elif event_type == "message_start" and isinstance(value.get("message"), dict):
        message = value["message"]
        projected["message"] = {
            field: message[field]
            for field in ("id", "type", "role", "model", "usage")
            if field in message
        }
    elif event_type == "message_delta":
        if isinstance(value.get("delta"), dict):
            delta = value["delta"]
            projected["delta"] = {
                field: delta[field] for field in ("stop_reason", "stop_sequence") if field in delta
            }
        if isinstance(value.get("usage"), dict):
            projected["usage"] = value["usage"]
    return projected


def _prepare_sdk_message(message: Any) -> Dict[str, Any]:
    """Build the versioned endpoint envelope without changing Responses data."""
    normalized = _normalize_sdk_value(message)
    if not isinstance(normalized, dict):
        raise ValueError("SDK message is not an object")

    message_type = normalized.get("type")
    if not isinstance(message_type, str):
        raise ValueError("SDK message has no type")

    # Python SDK AssistantMessage/UserMessage dataclasses keep content at the
    # root. The JS SDK envelopes consumed by Noah keep it in ``message.content``.
    # Preserve the Python fields and add the compatible nested view.
    if message_type in {"assistant", "user"} and not isinstance(normalized.get("message"), dict):
        inner: Dict[str, Any] = {
            "role": message_type,
            "content": normalized.get("content", []),
        }
        if message_type == "assistant":
            for source, target in (
                ("message_id", "id"),
                ("model", "model"),
                ("usage", "usage"),
                ("stop_reason", "stop_reason"),
            ):
                if normalized.get(source) is not None:
                    inner[target] = normalized[source]
        normalized["message"] = inner

    if message_type == "assistant":
        assistant_inner = normalized.get("message")
        if isinstance(assistant_inner, dict):
            assistant_inner["content"] = _project_assistant_content(assistant_inner.get("content"))
        normalized["content"] = _project_assistant_content(normalized.get("content"))

    if message_type == "user":
        user_inner = normalized.get("message")
        if isinstance(user_inner, dict):
            user_inner["content"] = _project_user_content(user_inner.get("content"))
        normalized["content"] = _project_user_content(normalized.get("content"))
        normalized.pop("tool_use_result", None)

    if message_type == "stream_event":
        normalized["event"] = _project_stream_event(normalized.get("event"))

    if message_type == "system":
        raw_data = normalized.get("data")
        data = (
            {field: raw_data[field] for field in _SYSTEM_DATA_FIELDS if field in raw_data}
            if isinstance(raw_data, dict)
            else {}
        )
        for plugin_field in ("plugins", "loadedPlugins", "loaded_plugins"):
            if plugin_field in data:
                data[plugin_field] = _safe_plugins(data[plugin_field])
        system_message = {
            field: normalized[field]
            for field in {"type", "subtype", *_SYSTEM_DATA_FIELDS}
            if field in normalized
        }
        system_message["data"] = data
        normalized = system_message
        for field in _SYSTEM_DATA_FIELDS:
            if field in data and field not in normalized:
                normalized[field] = data[field]
        for plugin_field in ("plugins", "loadedPlugins", "loaded_plugins"):
            if plugin_field in normalized:
                normalized[plugin_field] = _safe_plugins(normalized[plugin_field])
        if "loaded_plugins" in normalized and "loadedPlugins" not in normalized:
            normalized["loadedPlugins"] = normalized["loaded_plugins"]

    if message_type == "result" and "model_usage" in normalized:
        normalized["modelUsage"] = _camelize_model_usage(normalized["model_usage"])
    if message_type == "result":
        normalized = {field: normalized[field] for field in _RESULT_FIELDS if field in normalized}

    return normalized


def _json_data(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_SDK_EVENT_BYTES:
        raise ValueError("SDK event exceeds the maximum SSE frame size")
    return encoded


def _sse(event: str, payload: Any) -> str:
    return f"event: {event}\ndata: {_json_data(payload)}\n\n"


def _error_sse(error_type: str, message: str) -> str:
    return _sse(
        "error",
        {
            "type": "error",
            "error": {"type": error_type, "message": message},
        },
    )


def _render_transcript(body: AgentMessagesRequest) -> str:
    transcript = [{"role": message.role, "content": message.text()} for message in body.messages]
    return (
        "The following JSON array is the complete conversation history. "
        "Continue the conversation by answering the final user message.\n\n"
        + json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
    )


async def _disconnect_client(client: Any) -> None:
    if client is None:
        return
    disconnect = getattr(client, "disconnect", None)
    if disconnect is None:
        return
    try:
        await asyncio.wait_for(disconnect(), timeout=2.0)
    except Exception:
        logger.debug("Stateless agent client disconnect failed", exc_info=True)


def _cleanup_sdk_transcript(session: Optional[Session]) -> None:
    """Delete SDK artifacts created for one endpoint-local ephemeral session."""
    if session is None or not session.workspace:
        return
    transcript = _session_jsonl_path(session.session_id, session.workspace)
    session_artifacts = transcript.parent / session.session_id
    try:
        transcript.unlink(missing_ok=True)
        shutil.rmtree(session_artifacts, ignore_errors=True)
        # Anonymous workspaces get unique encoded project directories. Remove
        # the now-empty directory, but never disturb unexpected sibling files.
        try:
            transcript.parent.rmdir()
        except OSError:
            pass
    except OSError:
        logger.debug("Stateless agent transcript cleanup failed", exc_info=True)


async def _stream_agent_messages(body: AgentMessagesRequest, resolved, backend):
    message_id = f"msg_{uuid.uuid4().hex}"
    workspace = None
    client = None
    session = None
    saw_result = False
    failed = False

    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "schema": AGENT_MESSAGE_SCHEMA,
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "agent": body.agent,
                "model": body.model,
            },
        },
    )

    try:
        # Always use an anonymous request-scoped workspace. Conversation state,
        # SDK client state, and filesystem side effects all disappear together
        # after the stream finalizes.
        workspace = workspace_manager.resolve(None, backend="claude")
        session = Session(
            session_id=str(uuid.uuid4()),
            backend="claude",
            workspace=str(workspace),
        )
        client = await backend.create_client(
            session=session,
            model=resolved.provider_model,
            system_prompt=body.system_text(),
            disallowed_tools=[ASK_USER_QUESTION_TOOL_NAME],
            permission_mode=os.getenv("PERMISSION_MODE") or None,
            mcp_servers=get_mcp_servers(),
            cwd=str(workspace),
            include_partial_messages=True,
        )
        session.client = client

        async for message in backend.run_completion_with_client(
            client,
            _render_transcript(body),
            session,
        ):
            if isinstance(message, dict) and message.get("type") == "error":
                failed = True
                yield _error_sse(
                    "backend_error",
                    "The agent backend failed while processing the request.",
                )
                return

            prepared = _prepare_sdk_message(message)
            yield _sse("sdk_message", prepared)
            if prepared.get("type") == "result":
                saw_result = True
                failed = bool(prepared.get("is_error")) or str(
                    prepared.get("subtype", "")
                ).startswith("error")

        if session.pending_tool_call is not None:
            yield _error_sse(
                "interactive_question_unsupported",
                "Interactive agent questions are not supported by this "
                "stateless endpoint; the agent must ask in normal text.",
            )
            return
        if not saw_result:
            yield _error_sse(
                "incomplete_stream",
                "The agent stream ended before a terminal SDK result message.",
            )
            return

        yield _sse(
            "message_stop",
            {
                "type": "message_stop",
                "message_id": message_id,
                "status": "failed" if failed else "completed",
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("Stateless agent message stream failed", exc_info=True)
        yield _error_sse(
            "api_error",
            "The agent request failed before the stream completed.",
        )
    finally:
        if session is not None:
            session.client = None
        await _disconnect_client(client)
        _cleanup_sdk_transcript(session)
        if workspace is not None:
            workspace_manager.cleanup_temp_workspace(workspace)


@router.post("/v1/agents/messages")
@rate_limit_endpoint("responses")
async def create_agent_message(
    request: Request,
    body: AgentMessagesRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Run one stateless Claude agent turn and stream SDK-compatible envelopes."""
    await verify_api_key(request, credentials)
    if body.agent != "claude":
        raise HTTPException(
            status_code=400,
            detail="Only agent 'claude' is supported by /v1/agents/messages v1",
        )
    if body.stream is not True:
        raise HTTPException(
            status_code=400,
            detail="/v1/agents/messages v1 requires stream=true",
        )

    resolved, backend = resolve_and_get_backend(body.model)
    if resolved.backend != "claude":
        raise HTTPException(
            status_code=400,
            detail="/v1/agents/messages v1 supports Claude models only",
        )
    validate_backend_auth_or_raise("claude")

    source = _stream_agent_messages(body, resolved, backend)
    stream = streaming_utils._keepalive_wrapper(source, SSE_KEEPALIVE_INTERVAL)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Agent-Message-Schema": AGENT_MESSAGE_SCHEMA,
        },
    )
