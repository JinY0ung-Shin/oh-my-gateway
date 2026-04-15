#!/usr/bin/env python3
"""
Unit tests for src/message_adapter.py

Tests the MessageAdapter class for message format conversion.
These are pure unit tests that don't require a running server.
"""

from types import SimpleNamespace

from src.message_adapter import MessageAdapter
from src.response_models import ResponseInputItem


class TestFilterContent:
    """Test MessageAdapter.filter_content()"""

    def test_empty_content_returns_empty(self):
        """Empty content returns empty."""
        assert MessageAdapter.filter_content("") == ""
        assert MessageAdapter.filter_content(None) is None

    def test_plain_text_unchanged(self):
        """Plain text content is unchanged."""
        content = "Hello, how can I help you today?"
        result = MessageAdapter.filter_content(content)
        assert result == content

    def test_removes_thinking_blocks(self):
        """Thinking blocks are removed."""
        content = "<thinking>Let me think about this...</thinking>Here is my answer."
        result = MessageAdapter.filter_content(content)

        assert "<thinking>" not in result
        assert "Let me think" not in result
        assert "Here is my answer" in result

    def test_removes_multiline_thinking_blocks(self):
        """Multiline thinking blocks are removed."""
        content = """<thinking>
        Line 1 of thinking
        Line 2 of thinking
        </thinking>
        The actual response."""
        result = MessageAdapter.filter_content(content)

        assert "<thinking>" not in result
        assert "The actual response" in result

    def test_preserves_image_bracket_references(self):
        """[Image:...] references are preserved (no longer stripped)."""
        content = "Here's the image: [Image: screenshot.png] as you can see"
        result = MessageAdapter.filter_content(content)

        assert "[Image: screenshot.png]" in result

    def test_replaces_base64_image_data(self):
        """Base64 image data URIs are replaced with placeholder."""
        content = "Image: data:image/png;base64,iVBORw0KGgoAAAANSUhE end"
        result = MessageAdapter.filter_content(content)

        assert "[base64 image data removed]" in result
        assert "iVBORw0" not in result

    def test_collapses_multiple_newlines(self):
        """Multiple consecutive newlines are collapsed."""
        content = "Line 1\n\n\n\n\nLine 2"
        result = MessageAdapter.filter_content(content)

        # Should have at most double newlines
        assert "\n\n\n" not in result

    def test_empty_after_filtering_returns_empty(self):
        """If content is empty after filtering, returns empty string."""
        content = "<thinking>Only thinking content</thinking>"
        result = MessageAdapter.filter_content(content)

        assert result == ""

    def test_whitespace_only_after_filtering_returns_empty(self):
        """If content is only whitespace after filtering, returns empty string."""
        content = "<thinking>content</thinking>   \n   \n   "
        result = MessageAdapter.filter_content(content)

        assert result == ""


class TestTruncateToolContent:
    """Test MessageAdapter._truncate_tool_content()"""

    def test_short_content_not_truncated(self):
        content = "short result"
        assert MessageAdapter._truncate_tool_content(content) == content

    def test_long_content_truncated(self):
        max_len = MessageAdapter.TOOL_RESULT_MAX_LENGTH
        content = "a" * (max_len + 100)
        result = MessageAdapter._truncate_tool_content(content)

        assert len(result) == max_len + len("\n... (truncated)")
        assert result.endswith("\n... (truncated)")
        assert result.startswith("a" * max_len)

    def test_non_string_not_truncated(self):
        content = {"data": "some data"}
        assert MessageAdapter._truncate_tool_content(content) == content


class TestEstimateTokens:
    """Test MessageAdapter.estimate_tokens()"""

    def test_short_text(self):
        """Short text token estimation."""
        # 12 chars / 4 = 3 tokens
        result = MessageAdapter.estimate_tokens("Hello World!")
        assert result == 3

    def test_empty_text(self):
        """Empty text returns 0 tokens."""
        result = MessageAdapter.estimate_tokens("")
        assert result == 0

    def test_long_text(self):
        """Longer text estimation."""
        # 100 chars / 4 = 25 tokens
        text = "a" * 100
        result = MessageAdapter.estimate_tokens(text)
        assert result == 25

    def test_realistic_text(self):
        """Realistic text estimation."""
        text = "This is a realistic sentence that might appear in a conversation."
        result = MessageAdapter.estimate_tokens(text)
        # 67 chars / 4 = 16 tokens
        assert result == 16


class TestBlockFormatting:
    """Test block conversion and formatting helpers."""

    def test_block_to_dict_supports_object_variants(self):
        text_block = SimpleNamespace(text="plain text")
        thinking_block = SimpleNamespace(thinking="plan")
        tool_use_block = SimpleNamespace(id="tool-1", name="Read", input="README.md")
        tool_result_block = SimpleNamespace(tool_use_id="tool-1", content="result", is_error=True)

        assert MessageAdapter._block_to_dict(text_block) == {"type": "text", "text": "plain text"}
        assert MessageAdapter._block_to_dict(thinking_block) == {
            "type": "thinking",
            "thinking": "plan",
        }
        assert MessageAdapter._block_to_dict(tool_use_block) == {
            "type": "tool_use",
            "id": "tool-1",
            "name": "Read",
            "input": "README.md",
        }
        assert MessageAdapter._block_to_dict(tool_result_block) == {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "content": "result",
            "is_error": True,
        }

    def test_block_to_dict_supports_dict_and_string_variants(self):
        tool_result = {
            "type": "tool_result",
            "tool_use_id": "tool-2",
            "content": "x" * (MessageAdapter.TOOL_RESULT_MAX_LENGTH + 20),
        }

        assert MessageAdapter._block_to_dict({"type": "text", "text": "text"}) == {
            "type": "text",
            "text": "text",
        }
        assert MessageAdapter._block_to_dict({"type": "thinking", "thinking": "ponder"}) == {
            "type": "thinking",
            "thinking": "ponder",
        }
        assert MessageAdapter._block_to_dict("raw text") == {"type": "text", "text": "raw text"}
        assert MessageAdapter._block_to_dict(tool_result)["content"].endswith("\n... (truncated)")

    def test_format_block_renders_thinking_and_json_blocks(self):
        assert (
            MessageAdapter.format_block({"type": "thinking", "thinking": "plan"})
            == "<think>plan</think>"
        )

        formatted = MessageAdapter.format_block(
            {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"path": "README.md"}}
        )
        assert formatted.startswith("\n```json\n")
        assert '"tool_use"' in formatted
        assert '"README.md"' in formatted


class TestResponseInputToPrompt:
    """Test Responses API input conversion."""

    def test_string_input_is_returned_directly(self):
        assert MessageAdapter.response_input_to_prompt("plain input") == "plain input"

    def test_array_input_joins_text_and_skips_empty_items(self):
        items = [
            ResponseInputItem(role="user", content="First"),
            ResponseInputItem(
                role="assistant",
                content=[
                    {"type": "input_text", "text": "Second line"},
                    {"type": "input_text", "text": "Third line"},
                    {"type": "input_image", "image_url": "ignored"},
                ],
            ),
            SimpleNamespace(role="user", content=[]),
            SimpleNamespace(role="tool", content=None),
        ]

        prompt = MessageAdapter.response_input_to_prompt(items)

        assert prompt == "First\n\nSecond line\nThird line"
