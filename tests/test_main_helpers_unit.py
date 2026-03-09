#!/usr/bin/env python3
"""
Unit tests for helper functions in src.main.
"""

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.main as main
from src.constants import DEFAULT_HOST, DEFAULT_MODEL, DEFAULT_PORT
from src.models import ChatCompletionRequest, Message, StreamOptions, Usage
from src.streaming_utils import is_assistant_content_chunk, make_sse, map_stop_reason, stream_chunks


def _parse_chat_sse(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: ") :])


def test_map_stop_reason_and_extract_stop_reason():
    assert main.map_stop_reason("max_tokens") == "length"
    assert main.map_stop_reason("end_turn") == "stop"

    assert (
        main.extract_stop_reason([{"type": "assistant"}, {"stop_reason": "end_turn"}]) == "end_turn"
    )
    assert main.extract_stop_reason([{"type": "assistant"}]) is None


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


def test_build_backend_options_disables_tools_and_adds_mcp_servers():
    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        enable_tools=False,
    )
    resolved = main.ResolvedModel(
        public_model=DEFAULT_MODEL, backend="claude", provider_model=DEFAULT_MODEL
    )

    with patch.object(
        main, "get_mcp_servers", return_value={"demo": {"type": "stdio", "command": "demo"}}
    ):
        options = main._build_backend_options(request, resolved, {"max_turns": 9})

    assert options["model"] == DEFAULT_MODEL
    assert options["max_turns"] == 1
    assert options["disallowed_tools"] == main.CLAUDE_TOOLS
    assert options["mcp_servers"] == {"demo": {"type": "stdio", "command": "demo"}}


def test_build_backend_options_enables_tools():
    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        enable_tools=True,
    )
    resolved = main.ResolvedModel(
        public_model=DEFAULT_MODEL, backend="claude", provider_model=DEFAULT_MODEL
    )

    with patch.object(main, "get_mcp_servers", return_value={}):
        options = main._build_backend_options(request, resolved)

    assert options["allowed_tools"] == main.DEFAULT_ALLOWED_TOOLS
    assert options["permission_mode"] == main.PERMISSION_MODE_BYPASS


def test_process_chunk_content_handles_old_and_result_formats():
    old_format = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "legacy"}]},
    }
    result_format = {"subtype": "success", "result": "final"}

    assert main._process_chunk_content(old_format) == [{"type": "text", "text": "legacy"}]
    assert main._process_chunk_content(result_format, content_sent=False) == "final"
    assert main._process_chunk_content(result_format, content_sent=True) is None


def test_prepare_stateless_completion_filters_system_prompt():
    messages = [
        Message(role="system", content="sys <thinking>hidden</thinking>"),
        Message(role="user", content="Hello"),
    ]

    prompt, run_kwargs = main._prepare_stateless_completion(messages, {"model": DEFAULT_MODEL})

    assert prompt == "Hello"
    assert run_kwargs["system_prompt"] == "sys"
    assert run_kwargs["model"] == DEFAULT_MODEL


def test_prepare_session_prompt_uses_last_user_message():
    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[
            Message(role="system", content="system prompt"),
            Message(role="assistant", content="Earlier response"),
            Message(role="user", content="Newest user prompt"),
        ],
        session_id="session-helper-test",
    )

    prompt, system_prompt, session = main._prepare_session_prompt(request)

    assert prompt == "Newest user prompt"
    assert system_prompt == "system prompt"
    assert session.session_id == "session-helper-test"
    assert len(session.messages) == 0  # no mutation, session is still empty


@pytest.mark.asyncio
async def test_stream_chunks_emits_fallback_when_no_content():
    async def empty_source():
        yield {"type": "metadata"}

    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    chunks = []
    streamed = []
    async for line in main._stream_chunks(empty_source(), request, "req-1", chunks):
        streamed.append(line)

    assert chunks == [{"type": "metadata"}]
    assert any("assistant" in line for line in streamed)
    assert any("unable to provide a response" in line for line in streamed)


@pytest.mark.asyncio
async def test_generate_streaming_response_returns_error_chunk_on_exception():
    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    with patch.object(main, "_resolve_and_get_backend", side_effect=RuntimeError("boom")):
        lines = [line async for line in main.generate_streaming_response(request, "req-2")]

    assert lines == ['data: {"error": {"message": "boom", "type": "streaming_error"}}\n\n']


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
        patch.object(main.claude_cli, "verify_cli", AsyncMock(side_effect=asyncio.TimeoutError)),
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


# --- Token-level streaming tests ---


def test_extract_stream_event_delta_text():
    chunk = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello"},
        },
    }
    text, in_thinking = main._extract_stream_event_delta(chunk)
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
    text, in_thinking = main._extract_stream_event_delta(chunk)
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
    text, in_thinking = main._extract_stream_event_delta(start_chunk, in_thinking=False)
    assert text == "<think>"
    assert in_thinking is True

    stop_chunk = {
        "type": "stream_event",
        "event": {"type": "content_block_stop"},
    }
    text, in_thinking = main._extract_stream_event_delta(stop_chunk, in_thinking=True)
    assert text == "</think>"
    assert in_thinking is False


def test_extract_stream_event_delta_non_stream_event():
    chunk = {"type": "assistant", "content": [{"type": "text", "text": "hi"}]}
    text, _ = main._extract_stream_event_delta(chunk)
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
    text, _ = main._extract_stream_event_delta(chunk)
    assert text is None


@pytest.mark.asyncio
async def test_stream_chunks_token_mode():
    """StreamEvent text deltas should produce individual SSE chunks."""

    async def token_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": " world"},
            },
        }
        # AssistantMessage (should be skipped in token mode)
        yield {"content": [{"type": "text", "text": "Hello world"}]}
        # ResultMessage
        yield {"subtype": "success", "result": "Hello world"}

    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    chunks = []
    streamed = []
    async for line in main._stream_chunks(token_source(), request, "req-tok", chunks):
        streamed.append(line)

    payloads = [_parse_chat_sse(line) for line in streamed]
    deltas = [payload["choices"][0]["delta"] for payload in payloads]

    assert deltas[0]["role"] == "assistant"
    assert [delta["content"] for delta in deltas[1:]] == ["Hello", " world"]


@pytest.mark.asyncio
async def test_stream_chunks_fallback_without_stream_events():
    """Without StreamEvent chunks, fallback to message-level streaming."""

    async def message_source():
        yield {"content": [{"type": "text", "text": "response"}]}

    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    chunks = []
    streamed = []
    async for line in main._stream_chunks(message_source(), request, "req-fb", chunks):
        streamed.append(line)

    assert any("response" in line for line in streamed)


@pytest.mark.asyncio
async def test_stream_chunks_skips_assistant_in_token_mode():
    """In token mode, AssistantMessage should not produce duplicate content."""

    async def mixed_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "tok"},
            },
        }
        # This AssistantMessage is a duplicate and should be skipped
        yield {"type": "assistant", "content": [{"type": "text", "text": "tok"}]}

    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    chunks = []
    streamed = []
    async for line in main._stream_chunks(mixed_source(), request, "req-skip", chunks):
        streamed.append(line)

    payloads = [_parse_chat_sse(line) for line in streamed]
    content_deltas = [
        payload["choices"][0]["delta"]["content"]
        for payload in payloads
        if "content" in payload["choices"][0]["delta"]
        and payload["choices"][0]["delta"].get("role") != "assistant"
    ]

    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    assert content_deltas == ["tok"]


@pytest.mark.asyncio
async def test_stream_chunks_thinking_with_tags():
    """Thinking blocks should be streamed with <think>...</think> tags."""

    async def thinking_source():
        # content_block_start for thinking
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {"type": "thinking"},
            },
        }
        # thinking deltas
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "Let me think"},
            },
        }
        # content_block_stop (closes thinking)
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop"},
        }
        # text content
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Answer"},
            },
        }
        yield {"subtype": "success", "result": "Answer"}

    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    chunks = []
    streamed = []
    async for line in main._stream_chunks(thinking_source(), request, "req-think", chunks):
        streamed.append(line)

    payloads = [_parse_chat_sse(line) for line in streamed]
    content_deltas = [
        payload["choices"][0]["delta"]["content"]
        for payload in payloads
        if "content" in payload["choices"][0]["delta"]
        and payload["choices"][0]["delta"].get("role") != "assistant"
    ]

    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    assert content_deltas == ["<think>", "Let me think", "</think>", "Answer"]


@pytest.mark.asyncio
async def test_stream_chunks_token_mode_skips_assistantmessage_tool_use_duplicates():
    """In token mode, AssistantMessage tool_use content is skipped as a duplicate fallback payload."""

    async def tool_use_source():
        # First, some text deltas (triggers token mode)
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Let me check"},
            },
        }
        # AssistantMessage with both text and tool_use blocks
        yield {
            "content": [
                {"type": "text", "text": "Let me check"},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/test.txt"},
                },
            ]
        }
        yield {"subtype": "success", "result": "done"}

    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    chunks = []
    streamed = []
    async for line in main._stream_chunks(tool_use_source(), request, "req-tool", chunks):
        streamed.append(line)

    payloads = [_parse_chat_sse(line) for line in streamed]
    content_deltas = [
        payload["choices"][0]["delta"]["content"]
        for payload in payloads
        if "content" in payload["choices"][0]["delta"]
        and payload["choices"][0]["delta"].get("role") != "assistant"
    ]

    assert content_deltas == ["Let me check"]
    assert all("tool_use" not in content for content in content_deltas)


@pytest.mark.asyncio
async def test_stream_chunks_token_mode_emits_tool_use_from_stream_events():
    """In token mode, tool_use blocks are emitted from stream_event JSON deltas."""

    async def tool_use_event_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Let me check"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Read",
                },
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"file_path":"/tmp/test.txt"}',
                },
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_stop",
                "index": 1,
            },
        }
        yield {"subtype": "success", "result": "done"}

    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    chunks = []
    streamed = []
    async for line in main._stream_chunks(
        tool_use_event_source(), request, "req-tool-events", chunks
    ):
        streamed.append(line)

    payloads = [_parse_chat_sse(line) for line in streamed]
    content_deltas = [
        payload["choices"][0]["delta"]["content"]
        for payload in payloads
        if "content" in payload["choices"][0]["delta"]
        and payload["choices"][0]["delta"].get("role") != "assistant"
    ]

    assert content_deltas[0] == "Let me check"

    # tool_use is now emitted as a system_event, not as content
    system_events = [payload["system_event"] for payload in payloads if "system_event" in payload]
    tool_use_events = [e for e in system_events if e.get("type") == "tool_use"]
    assert len(tool_use_events) == 1
    assert tool_use_events[0]["name"] == "Read"
    assert tool_use_events[0]["input"] == {"file_path": "/tmp/test.txt"}


@pytest.mark.asyncio
async def test_stream_chunks_thinking_and_tool_use_reassembly_complex():
    """
    Test complex scenario:
    1. thinking_delta -> <think>...</think>
    2. tool_use with input_json_delta reassembly
    3. skip nested tool events (parent_tool_use_id)
    4. content_block_start/delta/stop flow
    """

    async def complex_source():
        # 1. Thinking block
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Let me think"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 0},
        }

        # 2. Text delta
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "I will read the file."},
            },
        }

        # 3. Tool use block (reassembly)
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Read",
                },
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '"/tmp/test.txt"}'},
            },
        }

        # 4. Nested tool event (should be skipped)
        yield {
            "type": "stream_event",
            "parent_tool_use_id": "sub-tool-id",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "sub-agent-output"},
            },
        }

        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 2},
        }

        # 5. Final result
        yield {"subtype": "success", "result": "done"}

    request = ChatCompletionRequest(
        model=DEFAULT_MODEL,
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    chunks = []
    streamed = []
    logger = logging.getLogger("test")

    async for line in stream_chunks(complex_source(), request, "req-complex", chunks, logger):
        streamed.append(line)

    payloads = [_parse_chat_sse(line) for line in streamed]
    deltas = [payload["choices"][0]["delta"] for payload in payloads]
    content_deltas = [
        delta["content"]
        for delta in deltas
        if "content" in delta and delta.get("role") != "assistant"
    ]

    assert deltas[0]["role"] == "assistant"
    assert "<think>" in content_deltas
    assert "Let me think" in content_deltas
    assert "</think>" in content_deltas
    assert "I will read the file." in content_deltas

    # tool_use is now emitted as a system_event
    system_events = [payload["system_event"] for payload in payloads if "system_event" in payload]
    tool_use_events = [e for e in system_events if e.get("type") == "tool_use"]
    assert len(tool_use_events) == 1
    assert tool_use_events[0]["id"] == "tool-1"
    assert tool_use_events[0]["name"] == "Read"
    assert tool_use_events[0]["input"] == {"path": "/tmp/test.txt"}
    assert all("sub-agent-output" not in content for content in content_deltas)


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
    request.url = SimpleNamespace(path="/v1/chat/completions")
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
    request.url = SimpleNamespace(path="/v1/chat/completions")
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


# ---------------------------------------------------------------------------
# NEW TESTS: Additional coverage
# ---------------------------------------------------------------------------


class TestBuildBackendOptionsWithHeaders:
    """Test _build_backend_options merges claude_headers into options."""

    def test_headers_override_max_turns(self):
        request = ChatCompletionRequest(
            model="claude-sonnet-4-20250514",
            messages=[Message(role="user", content="Hi")],
            enable_tools=True,
        )
        headers = {"max_turns": 25}
        resolved = main.ResolvedModel(
            public_model="claude-sonnet-4-20250514",
            backend="claude",
            provider_model="claude-sonnet-4-20250514",
        )

        with patch.object(main, "get_mcp_servers", return_value={}):
            options = main._build_backend_options(request, resolved, headers)

        # claude_headers should override the default max_turns
        assert options["max_turns"] == 25

    def test_headers_none_keeps_defaults(self):
        request = ChatCompletionRequest(
            model="claude-sonnet-4-20250514",
            messages=[Message(role="user", content="Hi")],
            enable_tools=False,
        )
        resolved = main.ResolvedModel(
            public_model="claude-sonnet-4-20250514",
            backend="claude",
            provider_model="claude-sonnet-4-20250514",
        )

        with patch.object(main, "get_mcp_servers", return_value={}):
            options = main._build_backend_options(request, resolved, None)

        # With tools disabled, max_turns is forced to 1
        assert options["max_turns"] == 1


class TestPrepareSessionPromptEdgeCases:
    """Test _prepare_session_prompt with edge-case message lists."""

    def test_no_user_message_falls_back_to_message_adapter(self):
        """When messages contain only system/assistant, should fallback to MessageAdapter."""
        request = ChatCompletionRequest(
            model="claude-sonnet-4-20250514",
            messages=[
                Message(role="system", content="You are helpful"),
                Message(role="assistant", content="Hello there"),
            ],
            session_id="session-no-user-test",
        )

        prompt, _sys, session = main._prepare_session_prompt(request)

        # last_user_msg is None, so it falls back to MessageAdapter.messages_to_prompt
        assert prompt is not None
        assert len(prompt) > 0
        assert session.session_id == "session-no-user-test"

    def test_multiple_user_messages_uses_last(self):
        """When multiple user messages exist, the last one should be used."""
        request = ChatCompletionRequest(
            model="claude-sonnet-4-20250514",
            messages=[
                Message(role="user", content="First question"),
                Message(role="assistant", content="First answer"),
                Message(role="user", content="Second question"),
                Message(role="assistant", content="Second answer"),
                Message(role="user", content="Third question"),
            ],
            session_id="session-multi-user-test",
        )

        prompt, _sys, session = main._prepare_session_prompt(request)

        assert prompt == "Third question"


class TestMakeSSE:
    """Test make_sse (streaming_utils) for proper SSE line format."""

    def test_delta_with_content(self):
        line = make_sse("req-100", "test-model", {"content": "hello"})
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        payload = json.loads(line[len("data: ") :])
        assert payload["id"] == "req-100"
        assert payload["model"] == "test-model"
        assert payload["choices"][0]["delta"] == {"content": "hello"}
        assert payload["choices"][0]["finish_reason"] is None

    def test_finish_reason_included(self):
        line = make_sse("req-101", "test-model", {}, finish_reason="stop")
        payload = json.loads(line[len("data: ") :])
        assert payload["choices"][0]["finish_reason"] == "stop"

    def test_usage_included(self):
        usage = Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        line = make_sse("req-102", "test-model", {}, usage=usage)
        payload = json.loads(line[len("data: ") :])
        assert payload["usage"]["prompt_tokens"] == 10
        assert payload["usage"]["completion_tokens"] == 20
        assert payload["usage"]["total_tokens"] == 30


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


class TestMapStopReasonAdditional:
    """Test map_stop_reason with None and unknown inputs."""

    def test_none_returns_stop(self):
        assert map_stop_reason(None) == "stop"

    def test_unknown_reason_returns_stop(self):
        assert map_stop_reason("some_unknown_reason") == "stop"

    def test_empty_string_returns_stop(self):
        assert map_stop_reason("") == "stop"

    def test_max_tokens_returns_length(self):
        assert map_stop_reason("max_tokens") == "length"


async def _fake_hello_chunk_source():
    """Shared async generator yielding a single text delta and success result."""
    yield {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello"},
        },
    }
    yield {"subtype": "success", "result": "Hello"}


@pytest.mark.asyncio
async def test_generate_streaming_response_with_include_usage():
    """Verify usage data is emitted in the final chunk when stream_options.include_usage is True."""
    request = ChatCompletionRequest(
        model="claude-sonnet-4-20250514",
        messages=[Message(role="user", content="Hi")],
        stream=True,
        stream_options=StreamOptions(include_usage=True),
    )

    mock_backend = MagicMock()
    mock_backend.run_completion = MagicMock(return_value=_fake_hello_chunk_source())
    mock_backend.parse_message = MagicMock(return_value="Hello")
    mock_backend.estimate_token_usage = MagicMock(
        return_value={
            "prompt_tokens": 5,
            "completion_tokens": 10,
            "total_tokens": 15,
        }
    )

    with (
        patch.object(
            main,
            "_resolve_and_get_backend",
            return_value=(
                main.ResolvedModel(
                    public_model="claude-sonnet-4-20250514",
                    backend="claude",
                    provider_model="claude-sonnet-4-20250514",
                ),
                mock_backend,
            ),
        ),
        patch.object(main, "_validate_backend_auth"),
    ):
        lines = [line async for line in main.generate_streaming_response(request, "req-usage")]

    # Last two lines should be the final chunk (with finish_reason+usage) and [DONE]
    assert lines[-1] == "data: [DONE]\n\n"
    final_chunk = json.loads(lines[-2][len("data: ") :])
    assert final_chunk["usage"]["prompt_tokens"] == 5
    assert final_chunk["usage"]["completion_tokens"] == 10
    assert final_chunk["usage"]["total_tokens"] == 15
    assert final_chunk["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_generate_streaming_response_without_include_usage():
    """Verify usage data is NOT emitted when stream_options is absent."""
    request = ChatCompletionRequest(
        model="claude-sonnet-4-20250514",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )

    mock_backend = MagicMock()
    mock_backend.run_completion = MagicMock(return_value=_fake_hello_chunk_source())
    mock_backend.parse_message = MagicMock(return_value="Hello")
    mock_backend.estimate_token_usage = MagicMock(
        return_value={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )

    with (
        patch.object(
            main,
            "_resolve_and_get_backend",
            return_value=(
                main.ResolvedModel(
                    public_model="claude-sonnet-4-20250514",
                    backend="claude",
                    provider_model="claude-sonnet-4-20250514",
                ),
                mock_backend,
            ),
        ),
        patch.object(main, "_validate_backend_auth"),
    ):
        lines = [line async for line in main.generate_streaming_response(request, "req-no-usage")]

    assert lines[-1] == "data: [DONE]\n\n"
    final_chunk = json.loads(lines[-2][len("data: ") :])
    assert final_chunk["usage"] is None
