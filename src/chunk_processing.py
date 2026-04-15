"""Content extraction, filtering, and chunk classification for streaming."""

import json
from typing import Any, Dict, Optional

from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock

from src.collab_filter import strip_collab_json
from src.constants import (
    SUBAGENT_STREAM_TEXT,
)
from src.message_adapter import MessageAdapter
from src.sse_builders import _normalize_tool_result


def _extract_tool_blocks(content) -> tuple[list, list]:
    """Separate tool_use/tool_result blocks from other content.

    Returns (tool_blocks, non_tool_content).
    tool_blocks: list of tool_use and tool_result dicts/objects
    non_tool_content: remaining content blocks (text, thinking, etc.)
    """
    if not isinstance(content, list):
        return [], content if content else []
    tool_blocks: list[Any] = []
    non_tool: list[Any] = []
    for b in content:
        if isinstance(b, (ToolUseBlock, ToolResultBlock)):
            tool_blocks.append(b)
        elif isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result"):
            tool_blocks.append(b)
        elif hasattr(b, "type") and getattr(b, "type", None) in ("tool_use", "tool_result"):
            tool_blocks.append(b)
        else:
            non_tool.append(b)
    return tool_blocks, non_tool


def _filter_tool_blocks(content):
    """Filter out tool_use and tool_result blocks from a content list.

    Only filters by block type (dict or SDK object). Text blocks are never
    filtered to avoid suppressing legitimate user-visible content.
    """
    _, non_tool = _extract_tool_blocks(content)
    return non_tool or None


def process_chunk_content(chunk: Dict[str, Any], content_sent: bool = False):
    """Extract content from a chunk message. Returns content list, result string, or None."""
    if chunk.get("type") == "assistant" and "message" in chunk:
        message = chunk["message"]
        if isinstance(message, dict) and "content" in message:
            return _filter_tool_blocks(message["content"])

    if "content" in chunk and isinstance(chunk["content"], list):
        return _filter_tool_blocks(chunk["content"])

    if chunk.get("subtype") == "success" and "result" in chunk and not content_sent:
        return chunk["result"]

    return None


def extract_stream_event_delta(chunk: Dict[str, Any], in_thinking: bool = False) -> tuple:
    """Extract streamable text from a StreamEvent chunk."""
    if chunk.get("type") != "stream_event":
        return None, in_thinking
    if chunk.get("parent_tool_use_id") is not None and not SUBAGENT_STREAM_TEXT:
        return None, in_thinking

    event = chunk.get("event", {})
    event_type = event.get("type")
    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            return delta.get("text", ""), in_thinking
        if delta_type == "thinking_delta":
            return delta.get("thinking", ""), in_thinking
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        if block.get("type") == "thinking":
            return "<think>", True
    if event_type == "content_block_stop" and in_thinking:
        return "</think>", False
    return None, in_thinking


def is_assistant_content_chunk(chunk: Dict[str, Any]) -> bool:
    """Return True for assistant chunks, including the SDK's untyped content-list shape."""
    chunk_type = chunk.get("type")
    if chunk_type == "assistant":
        return True
    if chunk_type is not None:
        return False
    return isinstance(chunk.get("content"), list)


def extract_embedded_tool_blocks(chunk: Dict[str, Any]) -> list:
    """Extract tool_use/tool_result blocks embedded in assistant content.

    Extract tool_use/tool_result blocks embedded in assistant content arrays.
    This function lets the streaming loop emit them as structured SSE events.

    Returns a (possibly empty) list of tool block dicts.
    """
    if not is_assistant_content_chunk(chunk):
        return []
    content = chunk.get("content")
    if content is None:
        msg = chunk.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    tool_blocks, _ = _extract_tool_blocks(content)
    # Normalize SDK objects (ToolUseBlock, ToolResultBlock) to plain dicts
    # so callers can safely use .get() on every returned block.
    normalized: list[Dict[str, Any]] = []
    for tb in tool_blocks:
        if isinstance(tb, dict):
            normalized.append(tb)
        elif isinstance(tb, ToolUseBlock):
            normalized.append(
                {
                    "type": "tool_use",
                    "id": getattr(tb, "id", ""),
                    "name": getattr(tb, "name", ""),
                    "input": getattr(tb, "input", {}),
                }
            )
        elif isinstance(tb, ToolResultBlock):
            normalized.append(_normalize_tool_result(tb))
        elif hasattr(tb, "type"):
            # Generic SDK object fallback
            d: Dict[str, Any] = {"type": getattr(tb, "type", "")}
            for attr in ("id", "name", "input", "tool_use_id", "content", "is_error"):
                if hasattr(tb, attr):
                    d[attr] = getattr(tb, attr)
            normalized.append(d)
        else:
            normalized.append(tb)
    return normalized


class ToolUseAccumulator:
    """Accumulates tool_use blocks from streamed content_block events.

    Tracks partial tool_use blocks across content_block_start / content_block_delta
    (input_json_delta) / content_block_stop events and assembles them into complete
    tool_block dicts.
    """

    def __init__(self):
        self._acc: Dict[tuple, Dict[str, Any]] = {}

    def process_stream_event(self, chunk: Dict[str, Any]) -> tuple[bool, Optional[Dict[str, Any]]]:
        """Process a stream_event chunk for tool_use accumulation.

        Returns (handled, completed_tool_block):
            (True, None) — event consumed (start/delta/subagent skip), caller should continue
            (True, tool_block) — tool_use completed, caller should emit and continue
            (False, None) — not a tool_use event, caller should fall through
        """
        if chunk.get("type") != "stream_event":
            return False, None

        parent_id = chunk.get("parent_tool_use_id")
        event = chunk.get("event", {})
        event_type = event.get("type")

        if event_type == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                idx = (parent_id or "", event.get("index", 0))
                self._acc[idx] = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input_parts": [],
                    "parent_tool_use_id": parent_id,
                }
                return True, None
            return False, None

        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "input_json_delta":
                idx = (parent_id or "", event.get("index", 0))
                if idx in self._acc:
                    self._acc[idx]["input_parts"].append(delta.get("partial_json", ""))
                return True, None
            # Skip sub-agent text deltas (noise)
            if parent_id is not None:
                return True, None
            return False, None

        if event_type == "content_block_stop":
            idx = (parent_id or "", event.get("index", 0))
            if idx in self._acc:
                acc = self._acc.pop(idx)
                input_str = "".join(acc["input_parts"])
                try:
                    input_parsed = json.loads(input_str) if input_str else {}
                except json.JSONDecodeError:
                    input_parsed = {"raw_input": input_str}
                tool_block: Dict[str, Any] = {
                    "type": "tool_use",
                    "id": acc["id"],
                    "name": acc["name"],
                    "input": input_parsed,
                }
                if acc["parent_tool_use_id"]:
                    tool_block["parent_tool_use_id"] = acc["parent_tool_use_id"]
                return True, tool_block
            # Skip sub-agent non-tool stream events
            if parent_id is not None:
                return True, None
            return False, None

        return False, None

    @property
    def has_incomplete(self) -> bool:
        return bool(self._acc)

    @property
    def incomplete_keys(self) -> list:
        return list(self._acc.keys())


def extract_user_tool_results(chunk: Dict[str, Any]) -> tuple[list, Optional[str]]:
    """Extract tool_result blocks and parent_tool_use_id from a user chunk.

    Returns (tool_result_blocks, parent_id).
    """
    parent_id = chunk.get("parent_tool_use_id")
    content_blocks = chunk.get("content", [])
    if not isinstance(content_blocks, list):
        msg = chunk.get("message", {})
        content_blocks = msg.get("content", []) if isinstance(msg, dict) else []
    if not content_blocks:
        return [], parent_id
    tool_result_blocks = [
        b
        for b in content_blocks
        if (b.get("type") if isinstance(b, dict) else None) == "tool_result"
        or isinstance(b, ToolResultBlock)
    ]
    return tool_result_blocks, parent_id


def format_chunk_content(chunk: Dict[str, Any], content_sent: bool) -> Optional[str]:
    """Extract content from a chunk and format as a single text string.

    Strips any embedded collab_tool_call JSON before returning.
    Returns non-empty, non-whitespace text or None.
    """
    content = process_chunk_content(chunk, content_sent=content_sent)
    if content is None:
        return None
    if isinstance(content, list):
        formatted = MessageAdapter.format_blocks(content)
        if formatted and not formatted.isspace():
            formatted = strip_collab_json(formatted)
            return formatted if formatted else None
    elif isinstance(content, str) and content and not content.isspace():
        content = strip_collab_json(content)
        return content if content else None
    return None
