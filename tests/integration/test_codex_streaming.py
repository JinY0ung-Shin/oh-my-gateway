"""Integration tests: Codex normalized chunks → SSE streaming builders.

Exercises the full path from Codex JSONL events through normalize_codex_event()
into stream_chunks() (Chat Completions) and stream_response_chunks() (Responses API).
Includes an HTTP-level E2E test that exercises the full endpoint path via mock binary.
"""

import json
import logging
from typing import Any, AsyncGenerator, Dict, List
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.codex_cli import CodexCLI, normalize_codex_event
from src.backend_registry import BackendRegistry
from src.models import ChatCompletionRequest, Message
from src.streaming_utils import extract_sdk_usage, stream_chunks, stream_response_chunks

from tests.integration.conftest import (
    CODEX_EVENTS_BASIC,
    CODEX_EVENTS_ERROR,
    CODEX_EVENTS_MULTI_ITEM,
    CODEX_EVENTS_TURN_FAILED,
)

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def codex_chunk_source(events: List[Dict[str, Any]]) -> AsyncGenerator[Dict[str, Any], None]:
    """Simulate CodexCLI.run_completion: normalize raw Codex JSONL events."""
    for event in events:
        if event.get("type") == "thread.started":
            yield {"type": "codex_session", "session_id": event["thread_id"]}
            continue
        normalized = normalize_codex_event(event)
        if normalized is not None:
            yield normalized


def _parse_chat_sse(line: str) -> dict:
    """Parse a chat-completions SSE line (data: {...})."""
    assert line.startswith("data: "), f"Expected 'data: ' prefix, got: {line[:30]}"
    payload = line[len("data: ") :].strip()
    return json.loads(payload)


def _parse_response_sse(line: str) -> tuple:
    """Parse a responses-API SSE line (event: <type>\\ndata: {...})."""
    parts = line.strip().splitlines()
    assert len(parts) == 2, f"Expected 2 lines, got {len(parts)}: {parts}"
    event_line, data_line = parts
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


def _make_request(model: str = "codex") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content="test")],
        stream=True,
    )


# ============================================================================
# Chat SSE format tests (4 tests)
# ============================================================================


async def test_chat_sse_basic_flow():
    """CODEX_EVENTS_BASIC → stream_chunks produces role, content, and buffers result."""
    request = _make_request()
    chunks_buffer: list = []
    logger_ = logging.getLogger("test-chat-sse-basic")

    lines = [
        line
        async for line in stream_chunks(
            codex_chunk_source(CODEX_EVENTS_BASIC),
            request,
            "req-basic",
            chunks_buffer,
            logger_,
        )
    ]

    assert len(lines) >= 2, f"Expected at least 2 SSE lines, got {len(lines)}"

    # First SSE should carry role: assistant
    first = _parse_chat_sse(lines[0])
    assert first["choices"][0]["delta"]["role"] == "assistant"

    # At least one SSE should contain "Hello from Codex"
    all_content = "".join(lines)
    assert "Hello from Codex" in all_content

    # chunks_buffer should contain the assistant and result chunks
    buffer_types = [c.get("type") for c in chunks_buffer]
    assert "codex_session" in buffer_types
    assert "assistant" in buffer_types
    assert "result" in buffer_types


async def test_chat_sse_multi_item():
    """CODEX_EVENTS_MULTI_ITEM → system_event for task_started, tool chunks buffered, text emitted."""
    request = _make_request()
    chunks_buffer: list = []
    logger_ = logging.getLogger("test-chat-sse-multi")

    lines = [
        line
        async for line in stream_chunks(
            codex_chunk_source(CODEX_EVENTS_MULTI_ITEM),
            request,
            "req-multi",
            chunks_buffer,
            logger_,
        )
    ]

    # Should have system_event with task_started (from item.started command_execution)
    task_events = []
    for line in lines:
        if "system_event" in line:
            parsed = _parse_chat_sse(line)
            task_events.append(parsed["system_event"])

    assert any(te.get("type") == "task_started" for te in task_events), (
        f"No task_started event found in: {task_events}"
    )

    # Content should include "I found the files."
    all_content = "".join(lines)
    assert "I found the files." in all_content

    # Buffer should contain tool_use chunks (Bash from command_execution, Edit from file_change)
    tool_use_chunks = []
    for chunk in chunks_buffer:
        if chunk.get("type") == "assistant":
            for block in chunk.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_chunks.append(block)

    tool_names = [t["name"] for t in tool_use_chunks]
    assert "Bash" in tool_names, f"Expected Bash tool_use in buffer, got: {tool_names}"
    assert "Edit" in tool_names, f"Expected Edit tool_use in buffer, got: {tool_names}"


async def test_chat_sse_usage_propagation():
    """CODEX_EVENTS_BASIC (has usage in turn.completed) → usage extractable from chunks_buffer."""
    request = _make_request()
    chunks_buffer: list = []
    logger_ = logging.getLogger("test-chat-sse-usage")

    # Consume the stream fully
    _ = [
        line
        async for line in stream_chunks(
            codex_chunk_source(CODEX_EVENTS_BASIC),
            request,
            "req-usage",
            chunks_buffer,
            logger_,
        )
    ]

    # extract_sdk_usage should find the result chunk with usage
    usage = extract_sdk_usage(chunks_buffer)
    assert usage is not None, "extract_sdk_usage returned None — no result chunk in buffer"
    assert usage["prompt_tokens"] == 50  # input_tokens=50, no cache
    assert usage["completion_tokens"] == 25
    assert usage["total_tokens"] == 75


async def test_codex_session_in_chunks_buffer():
    """codex_session meta-chunk appears in chunks_buffer for _capture_provider_session_id."""

    async def mixed_source():
        yield {"type": "codex_session", "session_id": "t-manual-001"}
        yield {"type": "assistant", "content": [{"type": "text", "text": "reply"}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": "",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    request = _make_request()
    chunks_buffer: list = []
    logger_ = logging.getLogger("test-codex-session-buffer")

    _ = [
        line
        async for line in stream_chunks(
            mixed_source(), request, "req-session", chunks_buffer, logger_
        )
    ]

    # codex_session must be in chunks_buffer so the endpoint can extract provider_session_id
    session_chunks = [c for c in chunks_buffer if c.get("type") == "codex_session"]
    assert len(session_chunks) == 1
    assert session_chunks[0]["session_id"] == "t-manual-001"


# ============================================================================
# Responses SSE format tests (3 tests — utility level, NOT endpoint)
# ============================================================================


async def test_responses_utility_basic_flow():
    """CODEX_EVENTS_BASIC → stream_response_chunks emits proper event ordering."""
    chunks_buffer: list = []
    stream_result: dict = {}
    logger_ = logging.getLogger("test-resp-basic")

    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=codex_chunk_source(CODEX_EVENTS_BASIC),
            model="codex",
            response_id="resp-codex-basic",
            output_item_id="msg-codex-basic",
            chunks_buffer=chunks_buffer,
            logger=logger_,
            prompt_text="test",
            stream_result=stream_result,
        )
    ]

    parsed = [_parse_response_sse(line) for line in lines]
    event_types = [et for et, _ in parsed]

    # Verify preamble ordering
    assert event_types[0] == "response.created"
    assert event_types[1] == "response.in_progress"
    assert "response.output_item.added" in event_types
    assert "response.content_part.added" in event_types

    # Verify content delta carries Codex text
    deltas = [payload["delta"] for et, payload in parsed if et == "response.output_text.delta"]
    assert any("Hello from Codex" in d for d in deltas), f"No Codex text in deltas: {deltas}"

    # Verify closing event ordering
    assert event_types[-1] == "response.completed"
    assert event_types.index("response.output_text.done") < event_types.index(
        "response.content_part.done"
    )
    assert event_types.index("response.content_part.done") < event_types.index(
        "response.output_item.done"
    )
    assert event_types.index("response.output_item.done") < event_types.index("response.completed")

    # Completed payload should contain usage from Codex turn.completed
    completed_payload = parsed[-1][1]
    assert completed_payload["response"]["status"] == "completed"
    usage = completed_payload["response"]["usage"]
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 25

    assert stream_result["success"] is True


async def test_responses_utility_tool_events():
    """Events with command_execution → response.task_started in Responses API stream."""
    chunks_buffer: list = []
    stream_result: dict = {}
    logger_ = logging.getLogger("test-resp-tool")

    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=codex_chunk_source(CODEX_EVENTS_MULTI_ITEM),
            model="codex",
            response_id="resp-codex-tool",
            output_item_id="msg-codex-tool",
            chunks_buffer=chunks_buffer,
            logger=logger_,
            prompt_text="test",
            stream_result=stream_result,
        )
    ]

    parsed = [_parse_response_sse(line) for line in lines]
    event_types = [et for et, _ in parsed]

    # item.started command_execution → system task_started → response.task_started
    assert "response.task_started" in event_types, (
        f"Expected response.task_started in events: {event_types}"
    )

    task_started = next(p for et, p in parsed if et == "response.task_started")
    assert "Running:" in task_started.get("description", "")

    # Content deltas should include "I found the files."
    deltas = [
        payload.get("delta", "") for et, payload in parsed if et == "response.output_text.delta"
    ]
    assert any("I found the files." in d for d in deltas), f"No 'I found the files.' in: {deltas}"

    assert stream_result["success"] is True


async def test_responses_utility_error():
    """CODEX_EVENTS_ERROR and CODEX_EVENTS_TURN_FAILED → response.failed."""
    logger_ = logging.getLogger("test-resp-error")

    # --- Test with CODEX_EVENTS_ERROR (rate limit) ---
    stream_result_err: dict = {}
    lines_err = [
        line
        async for line in stream_response_chunks(
            chunk_source=codex_chunk_source(CODEX_EVENTS_ERROR),
            model="codex",
            response_id="resp-codex-err",
            output_item_id="msg-codex-err",
            chunks_buffer=[],
            logger=logger_,
            stream_result=stream_result_err,
        )
    ]

    parsed_err = [_parse_response_sse(line) for line in lines_err]
    final_err = parsed_err[-1]
    assert final_err[0] == "response.failed"
    assert final_err[1]["response"]["error"]["code"] == "sdk_error"
    assert "Rate limit exceeded" in final_err[1]["response"]["error"]["message"]
    assert stream_result_err["success"] is False

    # --- Test with CODEX_EVENTS_TURN_FAILED (internal error) ---
    stream_result_fail: dict = {}
    lines_fail = [
        line
        async for line in stream_response_chunks(
            chunk_source=codex_chunk_source(CODEX_EVENTS_TURN_FAILED),
            model="codex",
            response_id="resp-codex-fail",
            output_item_id="msg-codex-fail",
            chunks_buffer=[],
            logger=logger_,
            stream_result=stream_result_fail,
        )
    ]

    parsed_fail = [_parse_response_sse(line) for line in lines_fail]
    final_fail = parsed_fail[-1]
    assert final_fail[0] == "response.failed"
    assert final_fail[1]["response"]["error"]["code"] == "sdk_error"
    assert "Internal error" in final_fail[1]["response"]["error"]["message"]
    assert stream_result_fail["success"] is False


# ============================================================================
# HTTP streaming E2E test (mock binary → subprocess → normalize → endpoint SSE)
# ============================================================================


async def test_http_streaming_e2e_with_mock_codex(mock_codex_bin, tmp_path, monkeypatch):
    """Full HTTP streaming: POST /v1/chat/completions?stream=true → SSE via mock Codex binary."""
    # Patch module-level constants so CodexCLI uses the mock binary
    monkeypatch.setattr("src.codex_cli.CODEX_CLI_PATH", mock_codex_bin)
    monkeypatch.setattr("src.codex_cli.CODEX_CONFIG_ISOLATION", True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-e2e")
    monkeypatch.setenv("MOCK_CODEX_SCENARIO", "basic")
    monkeypatch.setenv("MOCK_CODEX_RESPONSE", "Hello from E2E")

    # Register a real CodexCLI backed by the mock binary
    codex_backend = CodexCLI(timeout=10000, cwd=str(tmp_path))
    BackendRegistry.register("codex", codex_backend)

    try:
        from src.main import app

        # Bypass auth: verify_api_key and _validate_backend_auth
        with (
            patch("src.main.verify_api_key", new_callable=AsyncMock),
            patch("src.main._validate_backend_auth"),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "Hi"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]

                body = resp.text
    finally:
        BackendRegistry.unregister("codex")

    # Parse all SSE lines
    sse_lines = [line for line in body.split("\n\n") if line.strip().startswith("data: ")]
    assert len(sse_lines) >= 2, f"Expected at least 2 SSE lines, got {len(sse_lines)}"

    # Verify role:assistant is present
    has_role = any(
        '"role": "assistant"' in line or '"role":"assistant"' in line for line in sse_lines
    )
    assert has_role, "No role:assistant found in SSE output"

    # Verify content includes the mock response
    assert "Hello from E2E" in body, "Mock response text not found in SSE output"

    # Verify finish_reason:stop in final data chunk (before [DONE])
    data_chunks = []
    for line in sse_lines:
        payload_str = line.strip()[len("data: ") :]
        if payload_str == "[DONE]":
            continue
        data_chunks.append(json.loads(payload_str))

    last_chunk = data_chunks[-1]
    assert last_chunk["choices"][0]["finish_reason"] == "stop"

    # Verify [DONE] marker
    assert "data: [DONE]" in body, "[DONE] marker not found in SSE output"
