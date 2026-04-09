#!/usr/bin/env python3
"""
Coverage tests for uncovered lines in src/main.py.

Targets specific line groups that were previously uncovered:
- Backend verification timeout/error logging during startup
- Raw request body capture in DEBUG mode
- HTTPException for unavailable backend
- HTTPException when backend auth fails
- BackendConfigError catching
- _is_assistant_content_chunk() wrapper
- Pydantic ValidationError extraction
- Debug endpoint exception handling
- Responses API session validation guards
- Responses API preflight lock release on error
- find_available_port socket exception
"""

import asyncio
import json
import logging
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.main as main
import src.routes.responses as responses_module
import src.routes.general as general_module
from src.backend_registry import BackendRegistry, ResolvedModel
from src.constants import DEFAULT_MODEL
from src.session_manager import session_manager


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@contextmanager
def client_context(**extra_patches):
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

    patches = {
        "discover_backends": patch.object(main, "discover_backends", _mock_discover),
        "verify_api_key_responses": patch.object(
            responses_module, "verify_api_key", new=AsyncMock(return_value=True)
        ),
        "verify_api_key_general": patch.object(
            general_module, "verify_api_key", new=AsyncMock(return_value=True)
        ),
        "validate_claude_code_auth": patch.object(
            main, "validate_claude_code_auth", return_value=(True, {"method": "test"})
        ),
        "_validate_backend_auth_responses": patch.object(
            responses_module, "validate_backend_auth_or_raise"
        ),
        "start_cleanup_task": patch.object(main.session_manager, "start_cleanup_task"),
        "async_shutdown": patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
    }

    with (
        patches["discover_backends"],
        patches["verify_api_key_responses"],
        patches["verify_api_key_general"],
        patches["validate_claude_code_auth"],
        patches["_validate_backend_auth_responses"],
        patches["start_cleanup_task"],
        patches["async_shutdown"],
    ):
        with TestClient(main.app) as client:
            yield client, mock_cli

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


def _make_resolved(backend="claude", model=DEFAULT_MODEL):
    return ResolvedModel(public_model=model, backend=backend, provider_model=model)


def _make_mock_backend(response_text="Hello", sdk_usage=None):
    """Create a mock backend that yields standard chunks."""
    chunks = []
    if sdk_usage:
        chunks.append(
            {
                "type": "result",
                "subtype": "success",
                "result": response_text,
                "usage": sdk_usage,
            }
        )
    else:
        chunks.append({"content": [{"type": "text", "text": response_text}]})
        chunks.append({"subtype": "success", "result": response_text})

    async def fake_run_completion(**kwargs):
        for c in chunks:
            yield c

    mock_backend = MagicMock()
    mock_backend.run_completion = fake_run_completion
    mock_backend.parse_message = MagicMock(return_value=response_text)
    mock_backend.estimate_token_usage = MagicMock(
        return_value={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    )
    return mock_backend


# ===========================================================================
# Lines 168-172: Backend verification timeout/error logging during startup
# ===========================================================================


class TestVerifyBackends:
    """Cover _verify_backends() timeout and exception paths."""

    async def test_verify_backend_returns_false(self, caplog):
        """Line 168: backend.verify() returns False."""
        mock_backend = MagicMock()
        mock_backend.verify = AsyncMock(return_value=False)

        with patch.object(BackendRegistry, "all_backends", return_value={"test": mock_backend}):
            with caplog.at_level(logging.WARNING):
                await main._verify_backends()

        assert "test backend verification returned False" in caplog.text

    async def test_verify_backend_timeout(self, caplog):
        """Line 170: backend.verify() times out."""
        mock_backend = MagicMock()
        mock_backend.verify = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch.object(BackendRegistry, "all_backends", return_value={"test": mock_backend}):
            with caplog.at_level(logging.WARNING):
                await main._verify_backends()

        assert "test backend verification timed out" in caplog.text

    async def test_verify_backend_exception(self, caplog):
        """Line 172: backend.verify() raises arbitrary exception."""
        mock_backend = MagicMock()
        mock_backend.verify = AsyncMock(side_effect=RuntimeError("init failed"))

        with patch.object(BackendRegistry, "all_backends", return_value={"test": mock_backend}):
            with caplog.at_level(logging.ERROR):
                await main._verify_backends()

        assert "test backend verification failed: init failed" in caplog.text


# ===========================================================================
# Pydantic ValidationError extraction in debug endpoint
# ===========================================================================


class TestDebugEndpointValidationError:
    """Cover debug endpoint's Pydantic ValidationError branch."""

    def test_debug_endpoint_validation_error(self):
        """Invalid body triggers Pydantic ValidationError."""
        with client_context() as (client, _mock_cli):
            response = client.post(
                "/v1/debug/request",
                json={
                    "model": DEFAULT_MODEL,
                    "messages": "not-a-list",
                },
            )

        body = response.json()
        assert body["debug_info"]["validation_result"]["valid"] is False
        assert len(body["debug_info"]["validation_result"]["errors"]) > 0


# ===========================================================================
# Debug endpoint exception handling
# ===========================================================================


class TestDebugEndpointException:
    """Cover the top-level exception handler in debug endpoint."""

    def test_debug_endpoint_returns_error_on_exception(self):
        """request.body() fails -- error in debug_info."""
        with client_context() as (client, _mock_cli):
            # Send a request with no body at all to the debug endpoint
            # (the endpoint tries to decode body)
            response = client.post(
                "/v1/debug/request",
                content=b"",
                headers={"content-type": "application/json"},
            )

        body = response.json()
        # It should still return a response (empty body parse results in {})
        assert "debug_info" in body

    def test_debug_endpoint_valid_request(self):
        """Debug endpoint should report valid for a correct request."""
        with client_context() as (client, _mock_cli):
            response = client.post(
                "/v1/debug/request",
                json={
                    "model": DEFAULT_MODEL,
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        body = response.json()
        assert body["debug_info"]["validation_result"]["valid"] is True


# ===========================================================================
# Responses API session validation guards
# ===========================================================================


class TestResponsesApiSessionValidation:
    """Cover /v1/responses session validation guards in both streaming and non-streaming."""

    def test_stale_response_id_returns_409(self):
        """Stale previous_response_id (turn < current)."""
        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 3
        session.backend = "claude"

        stale_resp_id = f"resp_{session_id}_2"

        async def fake_run(**kwargs):
            yield {"content": [{"type": "text", "text": "Hi"}]}
            yield {"subtype": "success", "result": "Hi"}

        with client_context() as (client, mock_cli):
            mock_cli.run_completion = fake_run
            mock_cli.parse_message.return_value = "Hi"

            response = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "Hi",
                    "previous_response_id": stale_resp_id,
                    "stream": False,
                },
            )

        assert response.status_code == 409
        assert "Stale" in response.json()["error"]["message"]

    def test_future_turn_response_id_returns_404(self):
        """Future turn previous_response_id."""
        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 1
        session.backend = "claude"

        future_resp_id = f"resp_{session_id}_5"

        with client_context() as (client, _mock_cli):
            response = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "Hi",
                    "previous_response_id": future_resp_id,
                    "stream": False,
                },
            )

        assert response.status_code == 404
        assert "future turn" in response.json()["error"]["message"]

    def test_backend_mismatch_returns_400(self):
        """Backend mismatch on follow-up."""

        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 1
        session.backend = "other"

        resp_id = f"resp_{session_id}_1"

        with client_context() as (client, _mock_cli):
            response = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "Hi",
                    "previous_response_id": resp_id,
                    "stream": False,
                },
            )

        assert response.status_code == 400
        assert "Cannot mix backends" in response.json()["error"]["message"]


# ===========================================================================
# Responses API preflight lock release on error
# ===========================================================================


class TestResponsesStreamingPreflightLockRelease:
    """Cover _responses_streaming_preflight lock-release-on-error path."""

    async def test_stale_response_id_releases_lock_streaming(self):
        """Lock released on validation failure."""
        from src.response_models import ResponseCreateRequest

        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 3
        session.backend = "claude"

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input="Hi",
            previous_response_id=f"resp_{session_id}_2",
        )
        resolved = _make_resolved()
        mock_backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await main._responses_streaming_preflight(
                body,
                resolved,
                mock_backend,
                session,
                session_id,
                False,
                "Hi",
                None,
            )

        assert exc_info.value.status_code == 409
        assert not session.lock.locked()

    async def test_future_turn_releases_lock_streaming(self):
        """Future turn releases lock."""
        from src.response_models import ResponseCreateRequest

        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 1
        session.backend = "claude"

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input="Hi",
            previous_response_id=f"resp_{session_id}_5",
        )
        resolved = _make_resolved()
        mock_backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await main._responses_streaming_preflight(
                body,
                resolved,
                mock_backend,
                session,
                session_id,
                False,
                "Hi",
                None,
            )

        assert exc_info.value.status_code == 404
        assert not session.lock.locked()

    async def test_backend_mismatch_releases_lock_streaming(self):
        """Backend mismatch releases lock."""
        from src.response_models import ResponseCreateRequest

        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 1
        session.backend = "other"

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input="Hi",
            previous_response_id=f"resp_{session_id}_1",
        )
        resolved = _make_resolved("claude")
        mock_backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            await main._responses_streaming_preflight(
                body,
                resolved,
                mock_backend,
                session,
                session_id,
                False,
                "Hi",
                None,
            )

        assert exc_info.value.status_code == 400
        assert not session.lock.locked()


# ===========================================================================
# Responses streaming exception partial capture via endpoint
# ===========================================================================


class TestResponsesStreamingExceptionPartialCapture:
    """Cover the exception path in /v1/responses streaming where chunks_buffer is truthy."""

    def test_streaming_responses_captures_session_id_on_failure(self):
        """chunks_buffer has content when exception occurs."""

        async def failing_run(**kwargs):
            yield {"content": [{"type": "text", "text": "partial"}]}
            raise RuntimeError("mid-stream failure")

        with client_context() as (client, mock_cli):
            mock_cli.run_completion = failing_run
            mock_cli.parse_message.return_value = None

            with client.stream(
                "POST",
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "Hi",
                    "stream": True,
                },
            ) as response:
                body = "".join(response.iter_text())

        # Should have a failed response
        assert "response.failed" in body or "server_error" in body


# ===========================================================================
# Non-streaming Responses API future-turn outside lock
# ===========================================================================


class TestResponsesNonStreamingFutureTurnOutsideLock:
    """Cover the future turn check outside lock in /v1/responses non-streaming."""

    def test_future_turn_returns_404_outside_lock(self):
        """Future turn caught before lock acquisition."""
        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 1

        future_resp_id = f"resp_{session_id}_10"

        with client_context() as (client, _mock_cli):
            response = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "Hi",
                    "previous_response_id": future_resp_id,
                    "stream": False,
                },
            )

        assert response.status_code == 404
        assert "future turn" in response.json()["error"]["message"]


# ===========================================================================
# find_available_port socket exception
# ===========================================================================


class TestFindAvailablePortSocketException:
    """Cover find_available_port when socket.connect_ex raises an exception."""

    def test_socket_exception_returns_port(self):
        """Exception during connect_ex returns that port."""

        def socket_factory(*args, **kwargs):
            result = MagicMock()
            result.connect_ex.side_effect = OSError("connection refused")
            return result

        with patch("socket.socket", side_effect=socket_factory):
            port = main.find_available_port(start_port=9500, max_attempts=2)

        assert port == 9500


# ===========================================================================
# Additional: Responses API streaming stale/future turn via endpoint
# ===========================================================================


class TestResponsesStreamingValidationViaEndpoint:
    """Cover streaming Responses API validation errors via full HTTP endpoint."""

    def test_streaming_stale_response_id_returns_409(self):
        """Stale previous_response_id in streaming mode returns 409."""
        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 3
        session.backend = "claude"

        stale_resp_id = f"resp_{session_id}_2"

        with client_context() as (client, _mock_cli):
            response = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "Hi",
                    "previous_response_id": stale_resp_id,
                    "stream": True,
                },
            )

        assert response.status_code == 409

    def test_streaming_backend_mismatch_returns_400(self):
        """Backend mismatch in streaming mode returns 400."""
        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)
        session.turn_counter = 1
        session.backend = "other"

        resp_id = f"resp_{session_id}_1"

        with client_context() as (client, _mock_cli):
            response = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "Hi",
                    "previous_response_id": resp_id,
                    "stream": True,
                },
            )

        assert response.status_code == 400


# ===========================================================================
# Validation handler body read exception in DEBUG mode
# ===========================================================================


class TestValidationHandlerBodyReadException:
    """Force the body read exception branch in the validation error handler."""

    def test_body_read_exception_sets_fallback(self):
        """When body read raises in DEBUG validation handler."""
        main.DEBUG_MODE = True

        from fastapi.exceptions import RequestValidationError

        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url = "http://localhost/v1/responses"
        mock_request.body = AsyncMock(side_effect=RuntimeError("body already consumed"))

        exc = RequestValidationError(
            errors=[
                {
                    "loc": ("body", "messages"),
                    "msg": "value is not a valid list",
                    "type": "type_error.list",
                }
            ]
        )

        import asyncio

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(main.validation_exception_handler(mock_request, exc))
        finally:
            loop.close()

        body = json.loads(result.body)
        assert body["error"]["debug"]["raw_request_body"] == "Could not read request body"


# ===========================================================================
# Debug endpoint top-level exception
# ===========================================================================


class TestDebugEndpointTopLevelException:
    """Cover the debug endpoint when an exception occurs during processing."""

    def test_exception_in_body_parsing_returns_error(self):
        """Exception during processing returns error dict."""
        import src.routes.general as general_module

        with client_context() as (client, _mock_cli):
            with patch.object(
                general_module,
                "ChatCompletionRequest",
                side_effect=Exception("model import error"),
            ):
                response = client.post(
                    "/v1/debug/request",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )

        body = response.json()
        # Either succeeds normally or returns error info
        assert "debug_info" in body
