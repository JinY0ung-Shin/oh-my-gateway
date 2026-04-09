"""Endpoint-level tests for image input support."""

import base64
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.main as main
import src.routes.responses as responses_module
from src.backend_registry import BackendRegistry
from src.routes.deps import (
    request_has_images as _request_has_images,
    validate_image_request as _validate_image_request,
    truncate_image_data as _truncate_image_data,
)

# Tiny valid PNG for testing
TINY_PNG_B64 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
).decode()

DATA_URL = f"data:image/png;base64,{TINY_PNG_B64}"


# ---------------------------------------------------------------------------
# Helper: fake request objects for unit-testing the helper functions
# ---------------------------------------------------------------------------


class _FakeContentPart:
    def __init__(self, ptype, **kwargs):
        self.type = ptype
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeMessage:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class _FakeRequest:
    """Mimics ChatCompletionRequest shape for helper function tests."""

    def __init__(self, messages=None, input=None, enable_tools=True):
        self.messages = messages
        self.input = input
        self.enable_tools = enable_tools


# ---------------------------------------------------------------------------
# Client context helper (matches pattern from test_main_api_unit.py)
# ---------------------------------------------------------------------------


@contextmanager
def client_context():
    """Create a TestClient with startup/shutdown side effects patched out."""
    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)

    from src.backends.claude.client import ClaudeCodeCLI

    mock_cli.build_options = ClaudeCodeCLI.build_options.__get__(mock_cli, type(mock_cli))

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()

    def _mock_discover():
        from tests.conftest import register_all_descriptors

        register_all_descriptors()
        BackendRegistry.register("claude", mock_cli)

    with (
        patch.object(main, "discover_backends", _mock_discover),
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(main, "validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch.object(responses_module, "validate_backend_auth_or_raise"),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
    ):
        with TestClient(main.app) as client:
            yield client, mock_cli

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


# ===========================================================================
# Unit tests for _request_has_images
# ===========================================================================


class TestRequestHasImages:
    """Tests for _request_has_images() helper."""

    def test_request_has_images_with_image(self):
        """Returns True when a message contains an image_url content part."""
        msg = _FakeMessage(
            "user",
            [
                _FakeContentPart("text", text="describe this"),
                _FakeContentPart("image_url", image_url={"url": DATA_URL}),
            ],
        )
        req = _FakeRequest(messages=[msg])
        assert _request_has_images(req) is True

    def test_request_has_images_text_only(self):
        """Returns False when all content parts are text."""
        msg = _FakeMessage(
            "user",
            [_FakeContentPart("text", text="just text")],
        )
        req = _FakeRequest(messages=[msg])
        assert _request_has_images(req) is False

    def test_request_has_images_string_content(self):
        """Returns False for plain string content."""
        msg = _FakeMessage("user", "plain string")
        req = _FakeRequest(messages=[msg])
        assert _request_has_images(req) is False

    def test_request_has_images_responses_input(self):
        """Returns True for Responses API input containing input_image."""
        input_data = [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": DATA_URL},
                ],
            }
        ]
        req = _FakeRequest(messages=None, input=input_data)
        assert _request_has_images(req) is True

    def test_request_has_images_responses_input_text_only(self):
        """Returns False for Responses API input with text only."""
        input_data = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "hello"},
                ],
            }
        ]
        req = _FakeRequest(messages=None, input=input_data)
        assert _request_has_images(req) is False


# ===========================================================================
# Unit tests for _validate_image_request
# ===========================================================================


class TestValidateImageRequest:
    """Tests for _validate_image_request() helper."""

    def _make_image_request(self, enable_tools=True):
        """Create a fake request that contains an image."""
        msg = _FakeMessage(
            "user",
            [_FakeContentPart("image_url", image_url={"url": DATA_URL})],
        )
        return _FakeRequest(messages=[msg], enable_tools=enable_tools)

    def test_validate_image_request_tools_disabled(self):
        """Raises HTTPException(400) when enable_tools=False and images present."""
        req = self._make_image_request(enable_tools=False)
        backend = MagicMock()
        backend.image_handler = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            _validate_image_request(req, backend)

        assert exc_info.value.status_code == 400
        assert "enable_tools" in str(exc_info.value.detail)

    def test_validate_image_request_no_image_handler(self):
        """Raises HTTPException(400) when backend has no image_handler attribute."""
        req = self._make_image_request(enable_tools=True)
        backend = MagicMock(spec=[])  # spec=[] -> no attributes at all
        backend.name = "test-backend"

        with pytest.raises(HTTPException) as exc_info:
            _validate_image_request(req, backend)

        assert exc_info.value.status_code == 400
        assert "not supported" in str(exc_info.value.detail)

    def test_validate_image_request_passes(self):
        """No error when tools are enabled and backend has image_handler."""
        req = self._make_image_request(enable_tools=True)
        backend = MagicMock()
        backend.image_handler = MagicMock()

        # Should not raise
        _validate_image_request(req, backend)

    def test_validate_image_request_no_images_always_passes(self):
        """No error when request contains no images, regardless of backend support."""
        msg = _FakeMessage("user", [_FakeContentPart("text", text="no image")])
        req = _FakeRequest(messages=[msg], enable_tools=False)
        backend = MagicMock(spec=[])  # no image_handler

        # Should not raise even with tools disabled and no image_handler
        _validate_image_request(req, backend)


# ===========================================================================
# Unit test for _truncate_image_data
# ===========================================================================


class TestTruncateImageData:
    """Tests for _truncate_image_data() helper."""

    def test_truncate_image_data(self):
        """Truncates base64 data in nested dicts while preserving structure."""
        long_b64 = "data:image/png;base64," + "A" * 300
        obj = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "url": long_b64},
                    ],
                }
            ]
        }

        result = _truncate_image_data(obj)

        # Structure is preserved
        assert result["messages"][0]["content"][0]["text"] == "describe this"
        # Image URL is truncated
        truncated_url = result["messages"][0]["content"][1]["url"]
        assert truncated_url.endswith("...[truncated]")
        assert len(truncated_url) < len(long_b64)

    def test_truncate_image_data_short_string_untouched(self):
        """Short strings are not truncated even in url/data fields."""
        obj = {"url": "https://example.com/short.png"}
        result = _truncate_image_data(obj)
        assert result["url"] == "https://example.com/short.png"

    def test_truncate_image_data_preserves_non_image_long_string(self):
        """Long non-base64 strings in url/data fields are not truncated."""
        long_text = "x" * 300  # long but not base64/data:image
        obj = {"url": long_text}
        result = _truncate_image_data(obj)
        assert result["url"] == long_text

    def test_truncate_image_data_nested_data_field(self):
        """Truncates long base64 in 'data' fields too."""
        long_b64 = "data:image/jpeg;base64," + "B" * 300
        obj = {"source": {"data": long_b64}}
        result = _truncate_image_data(obj)
        assert result["source"]["data"].endswith("...[truncated]")
