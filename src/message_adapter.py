from typing import Optional
import re
import json

from claude_agent_sdk.types import ServerToolResultBlock, ServerToolUseBlock

from src.content_blocks import (
    normalize_advisor_tool_result_block,
    normalize_tool_result_block,
    normalize_tool_use_block,
)


class MessageAdapter:
    """Converts between OpenAI message format and Claude Code prompts."""

    # Cap on tool_result content when blocks are rendered to text. Only the
    # fallback paths hit this (no ResultMessage.result); streaming SSE
    # tool_result events are never truncated.
    TOOL_RESULT_MAX_LENGTH = 2000

    @staticmethod
    def _truncate_tool_content(content):
        """Truncate tool result content if too long."""
        if isinstance(content, str) and len(content) > MessageAdapter.TOOL_RESULT_MAX_LENGTH:
            return content[: MessageAdapter.TOOL_RESULT_MAX_LENGTH] + "\n... (truncated)"
        return content

    @staticmethod
    def _block_to_dict(block) -> Optional[dict]:
        """Convert a ContentBlock object to a plain dict."""
        # TextBlock (object)
        if hasattr(block, "text") and not hasattr(block, "thinking"):
            return {"type": "text", "text": block.text}

        # TextBlock (dict)
        if isinstance(block, dict) and block.get("type") == "text":
            return block

        # ThinkingBlock (object)
        if hasattr(block, "thinking"):
            return {"type": "thinking", "thinking": block.thinking}

        # ThinkingBlock (dict)
        if isinstance(block, dict) and block.get("type") == "thinking":
            return block

        # ServerToolUseBlock (object)
        if isinstance(block, ServerToolUseBlock):
            return normalize_tool_use_block(
                block,
                block_type="server_tool_use",
                stringify_non_dict_input=True,
            )

        # ServerToolUseBlock (dict)
        if isinstance(block, dict) and block.get("type") == "server_tool_use":
            return block

        # ServerToolResultBlock / advisor_tool_result (object)
        if isinstance(block, ServerToolResultBlock):
            return normalize_advisor_tool_result_block(
                block,
                truncate_content=MessageAdapter._truncate_tool_content,
            )

        # ServerToolResultBlock / advisor_tool_result (dict)
        if isinstance(block, dict) and block.get("type") == "advisor_tool_result":
            return normalize_advisor_tool_result_block(
                block,
                truncate_content=MessageAdapter._truncate_tool_content,
                preserve_extra_fields=True,
            )

        # ToolUseBlock (object)
        if hasattr(block, "name") and hasattr(block, "input"):
            return normalize_tool_use_block(block, stringify_non_dict_input=True)

        # ToolUseBlock (dict)
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block

        # ToolResultBlock (object)
        if hasattr(block, "tool_use_id") and hasattr(block, "content"):
            return normalize_tool_result_block(
                block,
                truncate_content=MessageAdapter._truncate_tool_content,
            )

        # ToolResultBlock (dict)
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return normalize_tool_result_block(
                block,
                truncate_content=MessageAdapter._truncate_tool_content,
                preserve_extra_fields=True,
            )

        # Plain string
        if isinstance(block, str):
            return {"type": "text", "text": block}

        return None

    @staticmethod
    def format_block(block) -> Optional[str]:
        """Convert a ContentBlock to a formatted string.

        Handles: TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock.
        TextBlock returns plain text. ThinkingBlock returns <think> tags.
        Other blocks return fenced JSON.
        Returns None for unrecognized blocks.
        """
        # Fast path: TextBlock object — skip dict allocation
        if hasattr(block, "text") and not hasattr(block, "thinking"):
            return block.text
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
        if isinstance(block, str):
            return block

        # ThinkingBlock — render as <think> tags (OpenAI-compatible)
        if hasattr(block, "thinking"):
            return "<think>" + (block.thinking or "") + "</think>"
        if isinstance(block, dict) and block.get("type") == "thinking":
            return "<think>" + block.get("thinking", "") + "</think>"

        block_dict = MessageAdapter._block_to_dict(block)
        if block_dict is None:
            return None

        return "\n```json\n" + json.dumps(block_dict, ensure_ascii=False) + "\n```\n"

    @staticmethod
    def format_blocks(content_blocks: list) -> Optional[str]:
        """Convert a list of ContentBlocks to a single text string."""
        parts = []
        for block in content_blocks:
            formatted = MessageAdapter.format_block(block)
            if formatted:
                parts.append(formatted)
        return "".join(parts) if parts else None

    @staticmethod
    def filter_content(content: str) -> str:
        """
        Filter content for unsupported features and tool usage.
        Remove thinking blocks, tool calls, and image references.
        """
        if not content:
            return content

        # Remove thinking blocks
        thinking_pattern = r"<thinking>.*?</thinking>"
        content = re.sub(thinking_pattern, "", content, flags=re.DOTALL)

        # Strip raw base64 data URIs but preserve <attached_image> file references
        image_pattern = r"data:image/.*?;base64,.*?(?=\s|$)"
        content = re.sub(image_pattern, "[base64 image data removed]", content)

        # Clean up extra whitespace and newlines
        content = re.sub(r"\n\s*\n\s*\n", "\n\n", content)  # Multiple newlines to double
        content = content.strip()

        return content

    @staticmethod
    def response_input_to_prompt(input_data, image_handler=None) -> str:
        """Convert Responses API input to a Claude prompt string.

        Accepts either a plain string or an array of input items
        (OpenAI Responses API format).
        """
        if isinstance(input_data, str):
            return input_data

        # Array format: extract text from message items
        parts = []
        for item in input_data:
            content = item.content

            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # content is list of content parts, e.g. [{"type": "input_text", "text": "..."}]
                text_parts = []
                for part in content:
                    ptype = (
                        part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
                    )
                    text_part = (
                        part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
                    )

                    if text_part:
                        text_parts.append(text_part)
                    elif ptype == "input_image" and image_handler:
                        image_url = (
                            part.get("image_url", "")
                            if isinstance(part, dict)
                            else getattr(part, "image_url", "")
                        )
                        if image_url:
                            path = image_handler.save_responses_image(image_url)
                            text_parts.append(f'<attached_image path="{path}" />')
                text = "\n".join(text_parts)
            else:
                continue

            if not text:
                continue

            parts.append(text)

        return "\n\n".join(parts)

    @staticmethod
    def response_input_to_claude_blocks(input_data) -> list:
        """Convert Responses API input to native Anthropic content blocks.

        Used for multimodal Claude turns: ``input_image`` parts become inline
        ``{"type": "image", "source": {...}}`` blocks (no disk round-trip, no
        ``<attached_image>`` placeholder — issue #140), text parts become
        ``{"type": "text", ...}`` blocks with the same ``filter_content``
        treatment as the collapsed-string path. Part order within and across
        messages is preserved; no separator text is injected — content blocks
        are already structurally delimited for the model.

        Raises ``ValueError`` for invalid image payloads (non-``data:`` URL,
        unsupported media type, malformed base64, oversize).
        """
        from src.image_handler import ImageHandler

        if isinstance(input_data, str):
            filtered = MessageAdapter.filter_content(input_data)
            return [{"type": "text", "text": filtered}] if filtered else []

        blocks: list = []
        for item in input_data:
            content = item.content

            if isinstance(content, str):
                parts: list = [{"type": "text", "text": content}] if content else []
            elif isinstance(content, list):
                parts = []
                for part in content:
                    ptype = (
                        part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
                    )
                    text_part = (
                        part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
                    )

                    if text_part:
                        parts.append({"type": "text", "text": text_part})
                    elif ptype == "input_image":
                        image_url = (
                            part.get("image_url", "")
                            if isinstance(part, dict)
                            else getattr(part, "image_url", "")
                        )
                        if image_url:
                            parts.append(ImageHandler.data_url_to_image_block(image_url))
            else:
                continue

            blocks.extend(parts)

        # Apply the same content filtering the string path gets, per text block.
        filtered_blocks: list = []
        for block in blocks:
            if block.get("type") == "text":
                text = MessageAdapter.filter_content(block["text"])
                if not text:
                    continue
                filtered_blocks.append({"type": "text", "text": text})
            else:
                filtered_blocks.append(block)
        return filtered_blocks

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Rough estimation of token count.
        OpenAI's rule of thumb: ~4 characters per token for English text.
        """
        return len(text) // 4
