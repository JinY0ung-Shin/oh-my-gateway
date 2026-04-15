#!/usr/bin/env python3
"""
Unit tests for helper functions in src.main.
"""

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.main as main
from src.constants import DEFAULT_HOST, DEFAULT_PORT
from src.streaming_utils import (
    is_assistant_content_chunk,
    extract_stream_event_delta,
    process_chunk_content,
)

_test_logger = logging.getLogger("test")


def test_generate_secure_token_uses_requested_length():
    token = main.generate_secure_token(24)

    assert len(token) == 24
    assert all(ch.isalnum() or ch in "-_" for ch in token)


def test_prompt_for_api_protection_skips_when_api_key_exists(monkeypatch):
    monkeypatch.setenv("API_KEY", "already-set")

    assert main.prompt_for_api_protection() is None


def test_prompt_for_api_protection_returns_none_for_no(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)

    with patch("builtins.input", return_value="n"):
        assert main.prompt_for_api_protection() is None


def test_prompt_for_api_protection_returns_generated_token(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)

    with (
        patch("builtins.input", return_value="yes"),
        patch.object(main, "generate_secure_token", return_value="generated-token"),
    ):
        assert main.prompt_for_api_protection() == "generated-token"


def test_prompt_for_api_protection_handles_invalid_input_then_interrupt(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)

    with patch("builtins.input", side_effect=["maybe", KeyboardInterrupt]):
        assert main.prompt_for_api_protection() is None


def test_process_chunk_content_handles_old_and_result_formats():
    old_format = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "legacy"}]},
    }
    result_format = {"subtype": "success", "result": "final"}

    assert process_chunk_content(old_format) == [{"type": "text", "text": "legacy"}]
    assert process_chunk_content(result_format, content_sent=False) == "final"
    assert process_chunk_content(result_format, content_sent=True) is None


@pytest.mark.asyncio
async def test_lifespan_handles_auth_failure_timeout_and_debug_logging():
    main.DEBUG_MODE = True
    main.runtime_api_key = "runtime-token"

    with (
        patch.object(
            main,
            "validate_claude_code_auth",
            return_value=(False, {"errors": ["missing auth"], "method": "none"}),
        ),
        patch.object(main, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
        patch.object(main, "discover_backends"),
        patch.object(main, "_verify_backends", AsyncMock()),
        patch.object(main.session_manager, "start_cleanup_task") as start_cleanup,
        patch.object(main.session_manager, "async_shutdown", AsyncMock()) as async_shutdown,
    ):
        async with main.lifespan(main.app):
            pass

    start_cleanup.assert_called_once()
    async_shutdown.assert_awaited_once()


def test_find_available_port_returns_first_free_port():
    socket_instances = []
    connect_results = iter([0, 1])

    def socket_factory(*args, **kwargs):
        result = MagicMock()
        result.connect_ex.side_effect = lambda *_args, **_kwargs: next(connect_results)
        socket_instances.append(result)
        return result

    with patch("socket.socket", side_effect=socket_factory):
        port = main.find_available_port(start_port=8100, max_attempts=2)

    assert port == 8101
    assert len(socket_instances) == 2
    for sock in socket_instances:
        sock.close.assert_called_once()


def test_find_available_port_raises_when_all_ports_are_taken():
    def socket_factory(*args, **kwargs):
        result = MagicMock()
        result.connect_ex.return_value = 0
        return result

    with patch("socket.socket", side_effect=socket_factory):
        with pytest.raises(RuntimeError, match="No available ports found"):
            main.find_available_port(start_port=8200, max_attempts=2)


def test_run_server_uses_default_host_and_port():
    with (
        patch.object(main, "prompt_for_api_protection", return_value=None),
        patch("uvicorn.run") as run,
    ):
        main.run_server()

    run.assert_called_once_with(main.app, host=DEFAULT_HOST, port=DEFAULT_PORT)


def test_run_server_falls_back_to_alternative_port():
    address_in_use = OSError("Address already in use")
    address_in_use.errno = 48

    with (
        patch.object(main, "prompt_for_api_protection", return_value="runtime-token"),
        patch.object(main, "find_available_port", return_value=9001),
        patch("builtins.print"),
        patch("uvicorn.run", side_effect=[address_in_use, None]) as run,
    ):
        main.run_server(port=8000, host="127.0.0.1")

    assert main.runtime_api_key == "runtime-token"
    assert run.call_args_list[0].kwargs == {"host": "127.0.0.1", "port": 8000}
    assert run.call_args_list[1].kwargs == {"host": "127.0.0.1", "port": 9001}


# --- Token-level streaming helper tests ---


def test_extract_stream_event_delta_text():
    chunk = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello"},
        },
    }
    text, in_thinking = extract_stream_event_delta(chunk)
    assert text == "Hello"
    assert in_thinking is False


def test_extract_stream_event_delta_thinking():
    chunk = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        },
    }
    text, in_thinking = extract_stream_event_delta(chunk)
    assert text == "hmm"
    assert in_thinking is False


def test_extract_stream_event_delta_thinking_block_boundaries():
    """content_block_start(thinking) emits <think>, content_block_stop emits </think>."""
    start_chunk = {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "content_block": {"type": "thinking"},
        },
    }
    text, in_thinking = extract_stream_event_delta(start_chunk, in_thinking=False)
    assert text == "<think>"
    assert in_thinking is True

    stop_chunk = {
        "type": "stream_event",
        "event": {"type": "content_block_stop"},
    }
    text, in_thinking = extract_stream_event_delta(stop_chunk, in_thinking=True)
    assert text == "</think>"
    assert in_thinking is False


def test_extract_stream_event_delta_non_stream_event():
    chunk = {"type": "assistant", "content": [{"type": "text", "text": "hi"}]}
    text, _ = extract_stream_event_delta(chunk)
    assert text is None


def test_extract_stream_event_delta_subagent_skipped():
    chunk = {
        "type": "stream_event",
        "parent_tool_use_id": "tool-123",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "sub"},
        },
    }
    text, _ = extract_stream_event_delta(chunk)
    assert text is None


def test_run_server_reraises_when_no_alternative_port_is_found():
    address_in_use = OSError("Address already in use")
    address_in_use.errno = 48

    with (
        patch.object(main, "prompt_for_api_protection", return_value=None),
        patch.object(main, "find_available_port", side_effect=RuntimeError("no ports")),
        patch("builtins.print"),
        patch("uvicorn.run", side_effect=address_in_use),
    ):
        with pytest.raises(RuntimeError, match="no ports"):
            main.run_server(port=8000, host="127.0.0.1")


def test_run_server_reraises_unrelated_oserror():
    unexpected_error = OSError("permission denied")
    unexpected_error.errno = 13

    with (
        patch.object(main, "prompt_for_api_protection", return_value=None),
        patch("uvicorn.run", side_effect=unexpected_error),
    ):
        with pytest.raises(OSError, match="permission denied"):
            main.run_server(port=8000, host="127.0.0.1")


@pytest.mark.asyncio
async def test_debug_logging_middleware_logs_raw_body_when_json_parse_fails(caplog):
    middleware = main.DebugLoggingMiddleware(app=main.app)
    request = MagicMock()
    request.state = SimpleNamespace(request_id="req-debug-raw")
    request.method = "POST"
    request.url = SimpleNamespace(path="/v1/responses")
    request.headers = {"content-length": "8"}
    request.body = AsyncMock(return_value=b"not-json")
    response = MagicMock(status_code=200)
    call_next = AsyncMock(return_value=response)

    with (
        patch.object(main, "DEBUG_MODE", True),
        patch.object(main, "VERBOSE", False),
        caplog.at_level(logging.DEBUG),
    ):
        result = await middleware.dispatch(request, call_next)

    assert result is response
    assert "Request body (raw): not-json..." in caplog.text
    assert "Response: 200 in" in caplog.text


@pytest.mark.asyncio
async def test_debug_logging_middleware_handles_body_read_and_downstream_failures(caplog):
    middleware = main.DebugLoggingMiddleware(app=main.app)
    request = MagicMock()
    request.state = SimpleNamespace(request_id="req-debug-fail")
    request.method = "POST"
    request.url = SimpleNamespace(path="/v1/responses")
    request.headers = {"content-length": "10"}
    request.body = AsyncMock(side_effect=RuntimeError("read failed"))
    call_next = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch.object(main, "DEBUG_MODE", True),
        patch.object(main, "VERBOSE", False),
        caplog.at_level(logging.DEBUG),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await middleware.dispatch(request, call_next)

    assert "Could not read request body: read failed" in caplog.text
    assert "Request body: [not logged - streaming or large payload]" in caplog.text
    assert "Request failed after" in caplog.text


@pytest.mark.asyncio
async def test_request_logging_middleware_logs_parse_failures(caplog):
    middleware = main.RequestLoggingMiddleware(app=main.app)
    request = MagicMock()
    request.method = "POST"
    request.url = SimpleNamespace(path="/v1/responses")
    request.headers = {"content-length": "8"}
    request.body = AsyncMock(side_effect=RuntimeError("read failed"))
    request.client = SimpleNamespace(host="127.0.0.1")
    response = MagicMock(status_code=200)
    call_next = AsyncMock(return_value=response)

    with caplog.at_level(logging.DEBUG):
        result = await middleware.dispatch(request, call_next)

    assert result is response
    assert "Could not inspect request body for request logging: read failed" in caplog.text


@pytest.mark.asyncio
async def test_request_logging_middleware_logs_backend_resolution_failures(caplog):
    middleware = main.RequestLoggingMiddleware(app=main.app)
    request = MagicMock()
    request.method = "POST"
    request.url = SimpleNamespace(path="/v1/responses")
    request.headers = {"content-length": "32"}
    request.body = AsyncMock(return_value=b'{"model":"sonnet"}')
    request.client = SimpleNamespace(host="127.0.0.1")
    response = MagicMock(status_code=200)
    call_next = AsyncMock(return_value=response)

    with (
        patch("src.backends.resolve_model", side_effect=RuntimeError("resolver boom")),
        caplog.at_level(logging.DEBUG),
    ):
        result = await middleware.dispatch(request, call_next)

    assert result is response
    assert "Could not resolve backend for request logging: resolver boom" in caplog.text


def test_response_id_helpers_round_trip():
    session_id = str(uuid.uuid4())

    response_id = main._make_response_id(session_id, 3)
    parsed_session_id, turn = main._parse_response_id(response_id)
    message_id = main._generate_msg_id()

    assert parsed_session_id == session_id
    assert turn == 3
    assert message_id.startswith("msg_")
    assert len(message_id) == 28


@pytest.mark.parametrize(
    "response_id",
    [
        "bad-prefix",
        "resp_not-a-uuid_1",
        "resp_123_invalid-turn",
        f"resp_{uuid.uuid4()}_0",
    ],
)
def test_parse_response_id_rejects_invalid_formats(response_id):
    assert main._parse_response_id(response_id) is None


class TestIsAssistantContentChunk:
    """Test is_assistant_content_chunk for various chunk formats."""

    def test_type_assistant_returns_true(self):
        assert is_assistant_content_chunk({"type": "assistant", "message": {}}) is True

    def test_content_list_returns_true(self):
        assert is_assistant_content_chunk({"content": [{"type": "text", "text": "hi"}]}) is True

    def test_no_content_key_returns_false(self):
        assert is_assistant_content_chunk({"type": "metadata"}) is False

    def test_content_string_returns_false(self):
        # content exists but is not a list
        assert is_assistant_content_chunk({"content": "just a string"}) is False

    def test_empty_content_list_returns_true(self):
        # content is a list (even if empty), still matches the condition
        assert is_assistant_content_chunk({"content": []}) is True

    def test_type_user_without_content_list_returns_false(self):
        assert is_assistant_content_chunk({"type": "user", "result": "ok"}) is False

    def test_type_user_with_content_list_returns_false(self):
        # user chunks may carry tool results, but they are not assistant content
        assert is_assistant_content_chunk({"type": "user", "content": [{"type": "text"}]}) is False
