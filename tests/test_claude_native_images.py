"""Tests for native inline image blocks on the Claude backend (issue #140).

Covers the full Fix A path: data-URL → Anthropic image block conversion
(no disk round-trip), Responses input → content blocks, the route branch
helper, and the SDK streaming-input wrapping in the Claude client.
"""

import base64
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.constants import DEFAULT_MODEL
from src.image_handler import MAX_IMAGE_SIZE, ImageHandler
from src.message_adapter import MessageAdapter

# 1x1 transparent PNG
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
PNG_DATA_URL = f"data:image/png;base64,{PNG_B64}"


def _make_cli(cwd: Optional[str] = None):
    """Create a ClaudeCodeCLI instance with auth mocked out."""
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd=cwd or "/tmp")


# ---------------------------------------------------------------------------
# ImageHandler.data_url_to_image_block
# ---------------------------------------------------------------------------


class TestDataUrlToImageBlock:
    def test_valid_png_block(self):
        block = ImageHandler.data_url_to_image_block(PNG_DATA_URL)
        assert block == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
        }

    def test_unsupported_media_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported image type"):
            ImageHandler.data_url_to_image_block(f"data:image/tiff;base64,{PNG_B64}")

    def test_malformed_base64_raises(self):
        with pytest.raises(ValueError, match="Malformed image base64"):
            ImageHandler.data_url_to_image_block("data:image/png;base64,@@not-base64@@")

    def test_non_data_url_raises(self):
        with pytest.raises(ValueError, match="Only data: URLs"):
            ImageHandler.data_url_to_image_block("https://example.com/img.png")

    def test_oversize_image_raises(self):
        big = base64.b64encode(b"\x00" * (MAX_IMAGE_SIZE + 1)).decode()
        with pytest.raises(ValueError, match="exceeds"):
            ImageHandler.data_url_to_image_block(f"data:image/png;base64,{big}")

    def test_no_disk_write(self, tmp_path):
        """Block conversion never touches the workspace image dir."""
        handler = ImageHandler(tmp_path)
        ImageHandler.data_url_to_image_block(PNG_DATA_URL)
        assert list(handler.image_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# MessageAdapter.response_input_to_claude_blocks
# ---------------------------------------------------------------------------


class TestResponseInputToClaudeBlocks:
    def test_string_input(self):
        blocks = MessageAdapter.response_input_to_claude_blocks("hello")
        assert blocks == [{"type": "text", "text": "hello"}]

    def test_empty_string_input(self):
        assert MessageAdapter.response_input_to_claude_blocks("   ") == []

    def test_text_and_image_order_preserved(self):
        from src.response_models import ResponseInputItem

        items = [
            ResponseInputItem(
                role="user",
                content=[
                    {"type": "input_text", "text": "what is in this image?"},
                    {"type": "input_image", "image_url": PNG_DATA_URL},
                ],
            )
        ]
        blocks = MessageAdapter.response_input_to_claude_blocks(items)
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == "what is in this image?"
        assert blocks[1]["source"]["data"] == PNG_B64

    def test_image_before_text(self):
        from src.response_models import ResponseInputItem

        items = [
            ResponseInputItem(
                role="user",
                content=[
                    {"type": "input_image", "image_url": PNG_DATA_URL},
                    {"type": "input_text", "text": "describe it"},
                ],
            )
        ]
        blocks = MessageAdapter.response_input_to_claude_blocks(items)
        assert [b["type"] for b in blocks] == ["image", "text"]

    def test_multiple_items_concatenate(self):
        from src.response_models import ResponseInputItem

        items = [
            ResponseInputItem(role="user", content="first"),
            ResponseInputItem(
                role="user",
                content=[{"type": "input_image", "image_url": PNG_DATA_URL}],
            ),
        ]
        blocks = MessageAdapter.response_input_to_claude_blocks(items)
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == "first"

    def test_empty_image_url_skipped(self):
        from src.response_models import ResponseInputItem

        items = [
            ResponseInputItem(
                role="user",
                content=[
                    {"type": "input_text", "text": "no image attached"},
                    {"type": "input_image", "image_url": ""},
                ],
            )
        ]
        blocks = MessageAdapter.response_input_to_claude_blocks(items)
        assert blocks == [{"type": "text", "text": "no image attached"}]

    def test_invalid_image_raises_value_error(self):
        from src.response_models import ResponseInputItem

        items = [
            ResponseInputItem(
                role="user",
                content=[{"type": "input_image", "image_url": "https://x/y.png"}],
            )
        ]
        with pytest.raises(ValueError):
            MessageAdapter.response_input_to_claude_blocks(items)

    def test_text_blocks_are_content_filtered(self):
        """Raw base64 data URIs in *text* are stripped, same as the string path."""
        from src.response_models import ResponseInputItem

        items = [
            ResponseInputItem(
                role="user",
                content=[
                    {
                        "type": "input_text",
                        "text": f"look: {PNG_DATA_URL}",
                    }
                ],
            )
        ]
        blocks = MessageAdapter.response_input_to_claude_blocks(items)
        assert len(blocks) == 1
        assert PNG_B64 not in blocks[0]["text"]
        assert "[base64 image data removed]" in blocks[0]["text"]


# ---------------------------------------------------------------------------
# Route helper: _response_prompt_blocks_and_system
# ---------------------------------------------------------------------------


class TestResponsePromptBlocksAndSystem:
    def test_blocks_and_system_split(self):
        from src.response_models import ResponseCreateRequest, ResponseInputItem
        from src.routes.responses import _response_prompt_blocks_and_system

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[
                ResponseInputItem(role="system", content="be terse"),
                ResponseInputItem(
                    role="user",
                    content=[
                        {"type": "input_text", "text": "what color?"},
                        {"type": "input_image", "image_url": PNG_DATA_URL},
                    ],
                ),
            ],
        )
        blocks, system_prompt = _response_prompt_blocks_and_system(body)
        assert system_prompt == "be terse"
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_invalid_image_becomes_400(self):
        from src.response_models import ResponseCreateRequest, ResponseInputItem
        from src.routes.responses import _response_prompt_blocks_and_system

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[
                ResponseInputItem(
                    role="user",
                    content=[{"type": "input_image", "image_url": "http://remote/x.png"}],
                )
            ],
        )
        with pytest.raises(HTTPException) as exc_info:
            _response_prompt_blocks_and_system(body)
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Claude client: block-list prompt sent as SDK streaming input
# ---------------------------------------------------------------------------


class TestRunCompletionWithBlockPrompt:
    async def test_block_prompt_wrapped_as_streaming_input(self):
        from src.session_manager import Session

        cli = _make_cli()
        session = Session(session_id="sess-img")

        captured = {}

        async def capture_query(prompt):
            if isinstance(prompt, str):
                captured["messages"] = prompt
            else:
                captured["messages"] = [msg async for msg in prompt]

        mock_client = AsyncMock()
        mock_client.query.side_effect = capture_query

        async def empty_receive():
            return
            yield

        mock_client.receive_response = empty_receive

        blocks = [
            {"type": "text", "text": "what color?"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
            },
        ]
        async for _ in cli.run_completion_with_client(mock_client, blocks, session):
            pass

        assert captured["messages"] == [
            {
                "type": "user",
                "message": {"role": "user", "content": blocks},
                "parent_tool_use_id": None,
            }
        ]

    async def test_string_prompt_still_passed_verbatim(self):
        from src.session_manager import Session

        cli = _make_cli()
        session = Session(session_id="sess-str")

        mock_client = AsyncMock()

        async def empty_receive():
            return
            yield

        mock_client.receive_response = empty_receive

        async for _ in cli.run_completion_with_client(mock_client, "plain text", session):
            pass

        mock_client.query.assert_awaited_once_with("plain text")
