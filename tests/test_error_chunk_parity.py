"""Error-chunk parity contract: stream vs non-stream vs continuation.

The streaming loop (streaming_utils.stream_response_chunks), the non-streaming
collection path, and the non-streaming continuation path must all classify the
same SDK error chunks as failures via the shared
``chunk_processing.classify_error_chunk`` helper:

- ``is_error`` result chunks            -> code "sdk_error"
- AssistantMessage.error                -> code = the SDK error type
- rate_limit events with status rejected -> code "rate_limit"

Each path must surface the same error code/message (response.failed event for
streams, HTTPException for non-streaming) and record the turn as failed in the
usage log.
"""

import asyncio
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.main as main
import src.routes.general as general_module
import src.routes.responses as responses_module
from src.backend_registry import BackendRegistry
from src.chunk_processing import classify_error_chunk
from src.constants import DEFAULT_MODEL
from src.response_models import ResponseCreateRequest
from src.streaming_utils import stream_response_chunks
from src.usage_logger import usage_logger


# ---------------------------------------------------------------------------
# Shared error-chunk fixtures: (chunk, expected_code, expected_message,
# expected_http_status)
# ---------------------------------------------------------------------------

ERROR_CHUNK_CASES = [
    pytest.param(
        {"is_error": True, "error_message": "sdk failed"},
        "sdk_error",
        "sdk failed",
        502,
        id="is_error_result",
    ),
    pytest.param(
        {"type": "assistant", "error": "authentication_failed", "content": []},
        "authentication_failed",
        "Claude error: authentication_failed",
        502,
        id="assistant_error",
    ),
    pytest.param(
        {"type": "rate_limit", "rate_limit_info": {"status": "rejected"}},
        "rate_limit",
        "Rate limit rejected",
        429,
        id="rate_limit_rejected",
    ),
]


def _parse_response_sse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


# ---------------------------------------------------------------------------
# classify_error_chunk unit tests
# ---------------------------------------------------------------------------


class TestClassifyErrorChunk:
    @pytest.mark.parametrize("chunk,code,message,_http", ERROR_CHUNK_CASES)
    def test_classifies_error_chunks(self, chunk, code, message, _http):
        assert classify_error_chunk(chunk) == {"code": code, "message": message}

    def test_is_error_without_message_uses_default(self):
        result = classify_error_chunk({"is_error": True})
        assert result == {"code": "sdk_error", "message": "Unknown SDK error"}

    def test_non_dict_returns_none(self):
        assert classify_error_chunk("not a dict") is None
        assert classify_error_chunk(None) is None

    def test_plain_assistant_chunk_is_not_an_error(self):
        chunk = {"type": "assistant", "content": [{"type": "text", "text": "hi"}]}
        assert classify_error_chunk(chunk) is None

    def test_non_rejected_rate_limit_is_not_an_error(self):
        chunk = {"type": "rate_limit", "rate_limit_info": {"status": "allowed_warning"}}
        assert classify_error_chunk(chunk) is None

    def test_rate_limit_without_info_is_not_an_error(self):
        assert classify_error_chunk({"type": "rate_limit"}) is None

    def test_sdk_rate_limit_info_object(self):
        class FakeRateLimitInfo:
            status = "rejected"

        chunk = {"type": "rate_limit", "rate_limit_info": FakeRateLimitInfo()}
        assert classify_error_chunk(chunk) == {
            "code": "rate_limit",
            "message": "Rate limit rejected",
        }


# ---------------------------------------------------------------------------
# Path 1: streaming
# ---------------------------------------------------------------------------


class TestStreamPathFailureSemantics:
    @pytest.mark.parametrize("chunk,code,message,_http", ERROR_CHUNK_CASES)
    async def test_stream_emits_failed_and_logs_failed_usage(
        self, chunk, code, message, _http
    ):
        async def error_source():
            yield chunk

        stream_result = {}
        log_mock = AsyncMock()
        with patch.object(usage_logger, "log_turn_from_context", new=log_mock):
            lines = [
                line
                async for line in stream_response_chunks(
                    chunk_source=error_source(),
                    model="claude-test",
                    response_id="resp-parity-stream",
                    output_item_id="msg-parity-stream",
                    chunks_buffer=[],
                    logger=logging.getLogger("test-error-parity-stream"),
                    stream_result=stream_result,
                )
            ]

        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.failed"
        assert parsed[-1][1]["response"]["error"]["code"] == code
        assert parsed[-1][1]["response"]["error"]["message"] == message
        assert stream_result["success"] is False

        log_mock.assert_awaited_once()
        log_kwargs = log_mock.await_args.kwargs
        assert log_kwargs["status"] == "failed"
        assert log_kwargs["error_code"] == code


# ---------------------------------------------------------------------------
# Path 2: non-streaming /v1/responses (route level, mocked backend)
# ---------------------------------------------------------------------------


@contextmanager
def _client_context():
    """TestClient with startup/shutdown side effects patched out."""
    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)

    async def _default_create_client(**kwargs):
        return object()

    async def _default_run_with_client(client, prompt, session):
        yield {"subtype": "success", "result": "Hi"}

    mock_cli.create_client = _default_create_client
    mock_cli.run_completion_with_client = _default_run_with_client
    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()

    def _mock_discover():
        from tests.conftest import register_all_descriptors

        register_all_descriptors()
        BackendRegistry.register("claude", mock_cli)

    mock_wm = MagicMock()
    mock_wm.resolve.return_value = Path("/tmp/ws/test")

    with (
        patch.object(main, "discover_backends", _mock_discover),
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(general_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(main, "validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch.object(responses_module, "validate_backend_auth_or_raise"),
        patch.object(responses_module, "workspace_manager", mock_wm),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
    ):
        with TestClient(main.app) as client:
            yield client, mock_cli

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


class TestNonStreamPathFailureSemantics:
    @pytest.mark.parametrize("chunk,code,message,http_status", ERROR_CHUNK_CASES)
    def test_non_stream_raises_http_error_and_logs_failed_usage(
        self, isolated_session_manager, chunk, code, message, http_status
    ):
        async def error_run_completion(client, prompt, session, **kwargs):
            yield chunk

        log_mock = AsyncMock()
        with (
            _client_context() as (client, mock_cli),
            patch.object(usage_logger, "log_turn_from_context", new=log_mock),
        ):
            mock_cli.run_completion_with_client = error_run_completion

            response = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "Hello"},
                headers={"Authorization": "Bearer test"},
            )

        assert response.status_code == http_status
        if http_status == 429:
            # The app-level 429 handler (rate_limit_exceeded_handler) shapes
            # every 429 body into a standard OpenAI-style rate-limit error.
            assert response.json()["error"]["type"] == "rate_limit_exceeded"
        else:
            assert response.json()["error"]["message"] == f"Backend error: {message}"

        log_mock.assert_awaited_once()
        log_kwargs = log_mock.await_args.kwargs
        assert log_kwargs["status"] == "failed"
        assert log_kwargs["error_code"] == code


class TestNonStreamTurnTimeout:
    """MAX_TIMEOUT bounds a fresh non-streaming turn.

    A hung backend must 504 and disconnect the session's SDK client instead
    of pinning the HTTP request, the session lock, and the CLI subprocess.
    Continuation turns share the same collector; their timeout is covered in
    test_ask_user_question.py.
    """

    def test_hung_backend_times_out_504_and_disconnects(
        self, isolated_session_manager, monkeypatch
    ):
        monkeypatch.setattr(responses_module, "NON_STREAM_TURN_TIMEOUT_SECONDS", 0.05)

        async def hung_run_completion(client, prompt, session, **kwargs):
            await asyncio.Event().wait()
            yield {"subtype": "success", "result": "unreachable"}

        sdk_client = MagicMock(name="mock_sdk_client")
        sdk_client.disconnect = AsyncMock()

        async def _create_client(**kwargs):
            return sdk_client

        with _client_context() as (client, mock_cli):
            mock_cli.create_client = _create_client
            mock_cli.run_completion_with_client = hung_run_completion

            response = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "Hello"},
                headers={"Authorization": "Bearer test"},
            )

        assert response.status_code == 504
        assert "timed out" in response.json()["error"]["message"]
        sdk_client.disconnect.assert_awaited()


# ---------------------------------------------------------------------------
# Path 3: non-streaming continuation (_handle_function_call_output)
# ---------------------------------------------------------------------------


def _make_continuation_session(session_id: str, turn: int = 1):
    from src.session_manager import Session

    session = Session(session_id=session_id, backend="claude")
    session.pending_tool_call = {
        "call_id": "toolu_abc",
        "name": "AskUserQuestion",
        "arguments": {"question": "Overwrite file?"},
    }
    session.input_event = asyncio.Event()
    session.client = MagicMock(name="mock_sdk_client")
    session.turn_counter = turn
    session.workspace = "/tmp/ws/test"
    return session


class TestContinuationNonStreamFailureSemantics:
    @pytest.mark.parametrize("chunk,code,message,http_status", ERROR_CHUNK_CASES)
    async def test_continuation_raises_http_error_and_logs_failed_usage(
        self, chunk, code, message, http_status
    ):
        from src.backends import ResolvedModel
        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[
                {"type": "function_call_output", "call_id": "toolu_abc", "output": "yes"},
            ],
            previous_response_id=f"resp_{session_id}_1",
            stream=False,
        )
        resolved = ResolvedModel(
            public_model=DEFAULT_MODEL, backend="claude", provider_model=None
        )

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            yield chunk

        backend.receive_response_from_client = fake_receive
        backend.parse_message = MagicMock(return_value="")

        log_mock = AsyncMock()
        with (
            patch.object(responses_module, "session_manager") as mock_sm,
            patch.object(usage_logger, "log_turn_from_context", new=log_mock),
        ):
            mock_sm.add_assistant_response = MagicMock()

            with pytest.raises(HTTPException) as exc_info:
                await _handle_function_call_output(
                    body,
                    resolved,
                    backend,
                    session,
                    session_id,
                    "/tmp/ws/test",
                    {"call_id": "toolu_abc", "output": "yes"},
                )

        assert exc_info.value.status_code == http_status
        assert exc_info.value.detail == f"Backend error: {message}"
        # Failed continuation must not commit the turn.
        assert session.turn_counter == 1

        log_mock.assert_awaited_once()
        log_kwargs = log_mock.await_args.kwargs
        assert log_kwargs["status"] == "failed"
        assert log_kwargs["error_code"] == code
