"""Shared helpers for Claude SDK and dict content blocks."""

from typing import Any, Callable, Dict, Optional

from claude_agent_sdk.types import (
    ServerToolResultBlock,
    ServerToolUseBlock,
    ToolResultBlock,
    ToolUseBlock,
)


TOOL_CONTENT_BLOCK_TYPES = {
    "tool_use",
    "tool_result",
    "server_tool_use",
    "advisor_tool_result",
}


def block_field(block: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict block or an SDK/object block."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def content_block_type(block: Any) -> Optional[str]:
    """Return the wire content-block type for dicts, SDK blocks, and typed objects."""
    explicit_type = block_field(block, "type")
    if explicit_type is not None:
        return explicit_type
    if isinstance(block, ToolUseBlock):
        return "tool_use"
    if isinstance(block, ToolResultBlock):
        return "tool_result"
    if isinstance(block, ServerToolUseBlock):
        return "server_tool_use"
    if isinstance(block, ServerToolResultBlock):
        return "advisor_tool_result"
    return None


def is_tool_content_block(block: Any) -> bool:
    """Return True when a block is one of the tool block wire types."""
    return content_block_type(block) in TOOL_CONTENT_BLOCK_TYPES


def _maybe_truncate(content: Any, truncate_content: Optional[Callable[[Any], Any]]) -> Any:
    if truncate_content is None:
        return content
    return truncate_content(content)


def normalize_tool_use_block(
    block: Any,
    *,
    block_type: str = "tool_use",
    stringify_non_dict_input: bool = False,
) -> Dict[str, Any]:
    """Normalize a tool-use-like object to a plain wire dict."""
    input_value = block_field(block, "input", {})
    if stringify_non_dict_input and not isinstance(input_value, dict):
        input_value = str(input_value)
    return {
        "type": block_type,
        "id": block_field(block, "id", ""),
        "name": block_field(block, "name", ""),
        "input": input_value,
    }


def normalize_tool_result_block(
    block: Any,
    *,
    truncate_content: Optional[Callable[[Any], Any]] = None,
    preserve_extra_fields: bool = False,
) -> Dict[str, Any]:
    """Normalize a tool_result block for user-visible message formatting."""
    if isinstance(block, dict) and preserve_extra_fields:
        result = dict(block)
        result["content"] = _maybe_truncate(result.get("content", ""), truncate_content)
        return result

    return {
        "type": "tool_result",
        "tool_use_id": block_field(block, "tool_use_id", ""),
        "content": _maybe_truncate(block_field(block, "content", ""), truncate_content),
        "is_error": bool(block_field(block, "is_error", False)),
    }


def normalize_advisor_tool_result_block(
    block: Any,
    *,
    truncate_content: Optional[Callable[[Any], Any]] = None,
    preserve_extra_fields: bool = False,
) -> Dict[str, Any]:
    """Normalize an advisor/server tool result without changing its wire type."""
    if isinstance(block, dict) and preserve_extra_fields:
        result = dict(block)
        result["content"] = _maybe_truncate(result.get("content", ""), truncate_content)
        return result

    return {
        "type": "advisor_tool_result",
        "tool_use_id": block_field(block, "tool_use_id", ""),
        "content": _maybe_truncate(block_field(block, "content", ""), truncate_content),
    }


def normalize_tool_result_for_sse(result_block: Any) -> Dict[str, Any]:
    """Normalize a ToolResultBlock or dict into the SSE tool_result shape."""
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


def normalize_embedded_tool_block(block: Any) -> Any:
    """Normalize embedded assistant tool blocks to plain dicts for streaming."""
    if isinstance(block, dict):
        return block
    if isinstance(block, ToolUseBlock):
        return normalize_tool_use_block(block)
    if isinstance(block, ToolResultBlock):
        return normalize_tool_result_for_sse(block)
    if isinstance(block, ServerToolUseBlock):
        return normalize_tool_use_block(block, block_type="server_tool_use")
    if isinstance(block, ServerToolResultBlock):
        return normalize_advisor_tool_result_block(block)
    if hasattr(block, "type"):
        result: Dict[str, Any] = {"type": getattr(block, "type", "")}
        for attr in ("id", "name", "input", "tool_use_id", "content", "is_error"):
            if hasattr(block, attr):
                result[attr] = getattr(block, attr)
        return result
    return block
