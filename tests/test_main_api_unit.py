#!/usr/bin/env python3
"""
Integration-style unit tests for FastAPI endpoints in src.main.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.main as main
import src.routes.chat as chat_module
import src.routes.messages as messages_module
import src.routes.responses as responses_module
import src.routes.general as general_module
import src.routes.sessions as sessions_module
from src.auth import auth_manager
from src.backend_registry import BackendRegistry
from src.backends.claude.constants import CLAUDE_TOOLS
from src.constants import DEFAULT_MODEL
from src.models import SessionInfo


@contextmanager
def client_context():
    """Create a TestClient with startup/shutdown side effects patched out."""
    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)
    # Use real Claude build_options so tests verify actual backend behavior
    from src.backends.claude.client import ClaudeCodeCLI

    mock_cli.build_options = ClaudeCodeCLI.build_options.__get__(mock_cli, type(mock_cli))

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()

    # Patch discover_backends to prevent real backend registration,
    # then register mock_cli as the "claude" backend for backend dispatch.
    def _mock_discover():
        from tests.conftest import register_all_descriptors

        register_all_descriptors()
        BackendRegistry.register("claude", mock_cli)

    with (
        patch.object(main, "discover_backends", _mock_discover),
        patch.object(chat_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(messages_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(general_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(main, "validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch.object(chat_module, "_validate_backend_auth"),
        patch.object(responses_module, "validate_backend_auth_or_raise"),
        patch.object(messages_module, "validate_backend_auth_or_raise"),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
    ):
        with TestClient(main.app) as client:
            yield client, mock_cli

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


def test_health_endpoint_returns_request_id_header():
    with client_context() as (client, _mock_cli):
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["service"] == "claude-code-gateway"
    assert response.headers["x-request-id"]


def test_request_size_limit_returns_413():
    main.MAX_REQUEST_SIZE = 10

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/compatibility",
            json={"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "request_too_large"


def test_validation_exception_handler_includes_debug_request_body():
    main.DEBUG_MODE = True

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/chat/completions",
            json={"model": DEFAULT_MODEL, "messages": "not-a-list"},
        )

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["type"] == "validation_error"
    assert body["error"]["details"][0]["field"].startswith("body -> messages")
    assert "raw_request_body" in body["error"]["debug"]


def test_chat_completions_non_streaming_success():
    run_calls = []

    async def fake_run_completion(**kwargs):
        run_calls.append(kwargs)
        yield {"content": [{"type": "text", "text": "Hello"}]}
        yield {"subtype": "success", "result": "Hello", "stop_reason": "max_tokens"}

    with (
        client_context() as (client, mock_cli),
        patch(
            "src.backends.claude.client.get_mcp_servers",
            return_value={"demo": {"type": "stdio", "command": "demo"}},
        ),
    ):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "Hello"

        response = client.post(
            "/v1/chat/completions",
            headers={"x-claude-max-turns": "7"},
            json={
                "model": DEFAULT_MODEL,
                "messages": [
                    {"role": "system", "content": "sys <thinking>ignore</thinking>"},
                    {"role": "user", "content": "Hi"},
                ],
                "stream": False,
                "enable_tools": False,
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["choices"][0]["message"]["content"] == "Hello"
    assert body["choices"][0]["finish_reason"] == "length"
    assert run_calls[0]["stream"] is False
    assert run_calls[0]["max_turns"] == 1
    assert run_calls[0]["disallowed_tools"] == CLAUDE_TOOLS
    assert run_calls[0]["system_prompt"] == "sys"
    assert run_calls[0]["mcp_servers"] == {"demo": {"type": "stdio", "command": "demo"}}


def test_chat_completions_metadata_forwarded_to_backend():
    """Metadata from chat completion requests should reach run_completion as _metadata."""
    run_calls = []

    async def fake_run_completion(**kwargs):
        run_calls.append(kwargs)
        yield {"content": [{"type": "text", "text": "OK"}]}
        yield {"subtype": "success", "result": "OK", "stop_reason": "end_turn"}

    with client_context() as (client, mock_cli):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "OK"

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
                "metadata": {"THREAD_ID": "thread-abc"},
            },
        )

    assert response.status_code == 200
    assert run_calls[0]["_metadata"] == {"THREAD_ID": "thread-abc"}


def test_chat_completions_no_metadata_passes_none():
    """Without metadata field, _metadata should be None."""
    run_calls = []

    async def fake_run_completion(**kwargs):
        run_calls.append(kwargs)
        yield {"content": [{"type": "text", "text": "OK"}]}
        yield {"subtype": "success", "result": "OK", "stop_reason": "end_turn"}

    with client_context() as (client, mock_cli):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "OK"

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert run_calls[0]["_metadata"] is None


def test_chat_completions_streaming_response_with_usage():
    async def fake_run_completion(**kwargs):
        yield {"content": [{"type": "text", "text": "Hello"}]}
        yield {"subtype": "success", "result": "Hello", "stop_reason": "end_turn"}

    with client_context() as (client, mock_cli):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "Hello"
        mock_cli.estimate_token_usage.return_value = {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
        }

        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"prompt_tokens":1' in body
    assert '"finish_reason":"stop"' in body
    assert "data: [DONE]" in body


@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/messages"])
def test_returns_503_when_auth_is_invalid(endpoint):
    _auth_exc = HTTPException(
        status_code=503,
        detail="claude backend authentication failed (missing auth). Check /v1/auth/status for detailed information.",
    )
    with (
        client_context() as (client, _mock_cli),
        patch.object(main, "_validate_backend_auth", side_effect=_auth_exc),
        patch.object(chat_module, "_validate_backend_auth", side_effect=_auth_exc),
        patch.object(messages_module, "validate_backend_auth_or_raise", side_effect=_auth_exc),
    ):
        response = client.post(
            endpoint,
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

    body = response.json()
    assert response.status_code == 503
    assert body["error"]["type"] == "api_error"
    assert body["error"]["code"] == "503"
    assert "authentication failed" in body["error"]["message"]


@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/messages"])
def test_returns_500_when_claude_returns_no_message(endpoint):
    async def fake_run_completion(**kwargs):
        if False:
            yield None

    with client_context() as (client, mock_cli):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = None

        response = client.post(
            endpoint,
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

    assert response.status_code == 500
    assert "No response from" in response.json()["error"]["message"]


def test_anthropic_messages_success():
    run_calls = []

    async def fake_run_completion(**kwargs):
        run_calls.append(kwargs)
        yield {"subtype": "success", "result": "Anthropic answer", "stop_reason": "end_turn"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
        patch.object(messages_module, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
    ):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "Anthropic answer"

        response = client.post(
            "/v1/messages",
            json={
                "model": DEFAULT_MODEL,
                "messages": [
                    {"role": "assistant", "content": "Earlier reply"},
                    {"role": "user", "content": "What now?"},
                ],
                "system": "system text",
                "stream": False,
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["content"][0]["text"] == "Anthropic answer"
    assert body["stop_reason"] == "end_turn"
    assert run_calls[0]["prompt"] == "Assistant: Earlier reply\n\nWhat now?"
    assert run_calls[0]["allowed_tools"] == main.DEFAULT_ALLOWED_TOOLS
    assert run_calls[0]["permission_mode"] == main.PERMISSION_MODE_BYPASS
    assert run_calls[0]["mcp_servers"] == {"demo": {"type": "stdio"}}


def test_models_compatibility_version_and_root_endpoints():
    auth_info = {"method": "claude_cli", "status": {"valid": True}}
    with (
        client_context() as (client, _mock_cli),
        patch.object(general_module, "get_claude_code_auth_info", return_value=auth_info),
    ):
        models_response = client.get("/v1/models")
        compatibility_response = client.post(
            "/v1/compatibility",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "temperature": 0.5,
            },
        )
        version_response = client.get("/version")
        root_response = client.get("/")

    assert models_response.status_code == 200
    assert models_response.json()["object"] == "list"
    assert compatibility_response.status_code == 200
    assert (
        "temperature"
        in compatibility_response.json()["compatibility_report"]["unsupported_parameters"]
    )
    assert version_response.status_code == 200
    assert version_response.json()["api_version"] == "v1"
    assert version_response.json()["service"] == "claude-code-gateway"
    assert root_response.status_code == 200
    assert "Claude Code Gateway" in root_response.text


def test_list_mcp_servers_filters_safe_fields():
    mcp_return = {
        "stdio-server": {
            "type": "stdio",
            "command": "demo",
            "args": ["--flag"],
            "secret": "ignored",
        },
        "remote-server": {
            "type": "sse",
            "url": "https://example.com/mcp",
            "token": "ignored",
        },
    }
    with (
        client_context() as (client, _mock_cli),
        patch.object(main, "get_mcp_servers", return_value=mcp_return),
        patch.object(general_module, "get_mcp_servers", return_value=mcp_return),
    ):
        response = client.get("/v1/mcp/servers")

    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 2
    assert "secret" not in body["servers"][0]["config"]
    assert "token" not in body["servers"][1]["config"]


def test_debug_request_endpoint_reports_parse_and_validation_results():
    with client_context() as (client, _mock_cli):
        invalid_response = client.post(
            "/v1/debug/request",
            content='{"model":',
            headers={"content-type": "application/json"},
        )
        valid_response = client.post(
            "/v1/debug/request",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

    assert invalid_response.status_code == 200
    assert invalid_response.json()["debug_info"]["json_parse_error"] is not None
    assert valid_response.status_code == 200
    assert valid_response.json()["debug_info"]["validation_result"]["valid"] is True


def test_auth_status_endpoint_uses_runtime_key_source():
    original_main_key = getattr(main, "runtime_api_key", None)
    main.runtime_api_key = "runtime-key"

    auth_info = {"method": "claude_cli", "status": {"valid": True}}
    original_runtime_key = auth_manager.runtime_api_key
    auth_manager.runtime_api_key = "runtime-key"
    try:
        with (
            client_context() as (client, _mock_cli),
            patch.object(general_module, "get_claude_code_auth_info", return_value=auth_info),
            patch("src.auth.auth_manager.get_api_key", return_value="runtime-key"),
            patch.dict("os.environ", {}, clear=True),
        ):
            response = client.get("/v1/auth/status")

        assert response.status_code == 200
        assert response.json()["server_info"]["api_key_required"] is True
        assert response.json()["server_info"]["api_key_source"] == "runtime"
    finally:
        auth_manager.runtime_api_key = original_runtime_key
        main.runtime_api_key = original_main_key


def test_session_endpoints_and_http_exception_handler():
    now = datetime.now(timezone.utc)
    session_info = SessionInfo(
        session_id="demo-session",
        created_at=now,
        last_accessed=now + timedelta(minutes=1),
        message_count=2,
        expires_at=now + timedelta(minutes=60),
    )
    session_obj = MagicMock()
    session_obj.to_session_info.return_value = session_info

    def fake_get_session(session_id):
        if session_id == "demo-session":
            return session_obj
        return None

    def fake_delete_session(session_id):
        return session_id == "demo-session"

    with (
        client_context() as (client, _mock_cli),
        patch.object(sessions_module, "verify_api_key", new_callable=AsyncMock),
        patch.object(
            main.session_manager,
            "get_stats",
            return_value={"active_sessions": 1, "expired_sessions": 0, "total_messages": 2},
        ),
        patch.object(main.session_manager, "list_sessions", return_value=[session_info]),
        patch.object(main.session_manager, "get_session", side_effect=fake_get_session),
        patch.object(main.session_manager, "delete_session", side_effect=fake_delete_session),
    ):
        stats_response = client.get("/v1/sessions/stats")
        list_response = client.get("/v1/sessions")
        get_response = client.get("/v1/sessions/demo-session")
        delete_response = client.delete("/v1/sessions/demo-session")
        missing_get = client.get("/v1/sessions/missing-session")
        missing_delete = client.delete("/v1/sessions/missing-session")

    assert stats_response.status_code == 200
    assert stats_response.json()["session_stats"]["active_sessions"] == 1
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert get_response.status_code == 200
    assert get_response.json()["session_id"] == "demo-session"
    assert delete_response.status_code == 200
    assert "deleted successfully" in delete_response.json()["message"]
    assert missing_get.status_code == 404
    assert missing_get.json()["error"]["type"] == "api_error"
    assert missing_get.json()["error"]["code"] == "404"
    assert missing_get.json()["error"]["message"] == "Session not found"
    assert missing_delete.status_code == 404
    assert missing_delete.json()["error"]["type"] == "api_error"
    assert missing_delete.json()["error"]["code"] == "404"
    assert missing_delete.json()["error"]["message"] == "Session not found"


def test_create_response_non_streaming_success_uses_array_system_prompt(isolated_session_manager):
    run_calls = []

    async def fake_run_completion(**kwargs):
        run_calls.append(kwargs)
        yield {"subtype": "success", "result": "Responses answer"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
        patch.object(responses_module, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
    ):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "Responses answer"

        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": [
                    {
                        "role": "system",
                        "content": [
                            {"type": "input_text", "text": "System line 1"},
                            {"type": "input_text", "text": "System line 2"},
                        ],
                    },
                    {"role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
                ],
                "metadata": {"ticket": "123"},
            },
        )

    body = response.json()
    session_id, turn = main._parse_response_id(body["id"])
    session = isolated_session_manager.get_session(session_id)

    assert response.status_code == 200
    assert turn == 1
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "Responses answer"
    assert body["metadata"] == {"ticket": "123"}
    assert run_calls[0]["prompt"] == "Hi"
    assert run_calls[0]["system_prompt"] == "System line 1\nSystem line 2"
    assert run_calls[0]["session_id"] == session_id
    assert run_calls[0]["resume"] is None
    assert run_calls[0]["mcp_servers"] == {"demo": {"type": "stdio"}}
    assert session.turn_counter == 1
    assert [message.content for message in session.messages] == ["Hi", "Responses answer"]


def test_create_response_rejects_invalid_or_future_previous_response_ids(isolated_session_manager):
    existing_session_id = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(existing_session_id)
    session.turn_counter = 1

    with client_context() as (client, _mock_cli):
        invalid_response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello",
                "previous_response_id": "resp_not-a-uuid_1",
            },
        )
        future_turn_response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello",
                "previous_response_id": main._make_response_id(existing_session_id, 2),
            },
        )

    assert invalid_response.status_code == 404
    assert "is invalid" in invalid_response.json()["error"]["message"]
    assert future_turn_response.status_code == 404
    assert "future turn" in future_turn_response.json()["error"]["message"]


def test_create_response_rejects_instructions_with_previous_response_id():
    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello",
                "instructions": "System prompt",
                "previous_response_id": "resp_c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f_1",
            },
        )

    assert response.status_code == 400
    assert (
        "instructions cannot be used with previous_response_id"
        in response.json()["error"]["message"]
    )


def test_create_response_returns_404_when_previous_response_session_is_missing():
    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello",
                "previous_response_id": "resp_c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f_1",
            },
        )

    assert response.status_code == 404
    assert "not found or expired" in response.json()["error"]["message"]


def test_create_response_returns_503_when_auth_is_invalid():
    auth_error = HTTPException(
        status_code=503,
        detail="claude backend authentication failed (missing auth). Check /v1/auth/status for detailed information.",
    )
    with (
        client_context() as (client, _mock_cli),
        patch.object(main, "_validate_backend_auth", side_effect=auth_error),
        patch.object(responses_module, "validate_backend_auth_or_raise", side_effect=auth_error),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Hello"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "api_error"


def test_create_response_uses_string_system_prompt_from_array_input(isolated_session_manager):
    run_calls = []

    async def fake_run_completion(**kwargs):
        run_calls.append(kwargs)
        yield {"subtype": "success", "result": "String system prompt answer"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "String system prompt answer"

        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": [
                    {"role": "developer", "content": "You are terse."},
                    {"role": "user", "content": "Hi"},
                ],
            },
        )

    session_id, turn = main._parse_response_id(response.json()["id"])
    session = isolated_session_manager.get_session(session_id)

    assert response.status_code == 200
    assert turn == 1
    assert run_calls[0]["system_prompt"] == "You are terse."
    assert run_calls[0]["prompt"] == "Hi"
    assert session.turn_counter == 1


def test_create_response_streaming_success_commits_session_state(isolated_session_manager):
    run_calls = []

    def fake_run_completion(**kwargs):
        run_calls.append(kwargs)

        async def empty_source():
            if False:
                yield None

        return empty_source()

    async def fake_stream_response_chunks(**kwargs):
        kwargs["chunks_buffer"].append(
            {"content": [{"type": "text", "text": "streamed assistant"}]}
        )
        kwargs["stream_result"]["success"] = True
        kwargs["stream_result"]["assistant_text"] = "streamed assistant"
        yield 'event: response.created\ndata: {"type":"response.created","sequence_number":0}\n\n'

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(
            main.streaming_utils, "stream_response_chunks", new=fake_stream_response_chunks
        ),
    ):
        mock_cli.run_completion = fake_run_completion

        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Stream this", "stream": True},
        ) as response:
            body = "".join(response.iter_text())

    session = next(iter(isolated_session_manager.sessions.values()))

    assert response.status_code == 200
    assert "response.created" in body
    assert run_calls[0]["prompt"] == "Stream this"
    assert run_calls[0]["session_id"] == session.session_id
    assert run_calls[0]["resume"] is None
    assert session.turn_counter == 1
    assert [message.content for message in session.messages] == [
        "Stream this",
        "streamed assistant",
    ]


def test_create_response_streaming_setup_error_returns_error_event_without_commit(
    isolated_session_manager,
):
    run_calls = []

    def fake_run_completion(**kwargs):
        run_calls.append(kwargs)

        async def empty_source():
            if False:
                yield None

        return empty_source()

    async def exploding_stream_response_chunks(**kwargs):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(
            main.streaming_utils, "stream_response_chunks", new=exploding_stream_response_chunks
        ),
    ):
        mock_cli.run_completion = fake_run_completion

        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Stream this", "stream": True},
        ) as response:
            body = "".join(response.iter_text())

    session = next(iter(isolated_session_manager.sessions.values()))

    assert response.status_code == 200
    assert "event: response.failed" in body
    assert '"status": "failed"' in body
    assert '"code": "server_error"' in body
    assert run_calls[0]["prompt"] == "Stream this"
    assert session.turn_counter == 0
    assert session.messages == []


def test_create_response_returns_502_when_claude_sdk_raises():
    async def raising_run_completion(**kwargs):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = raising_run_completion

        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Hello"},
        )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Backend error: boom"


def test_create_response_returns_502_when_sdk_emits_error_chunk():
    async def error_chunk_run_completion(**kwargs):
        yield {"is_error": True, "error_message": "sdk failed"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = error_chunk_run_completion

        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Hello"},
        )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Backend error: sdk failed"


def test_create_response_returns_502_when_sdk_returns_no_message():
    async def empty_run_completion(**kwargs):
        if False:
            yield None

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = empty_run_completion
        mock_cli.parse_message.return_value = None

        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Hello"},
        )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "No response from backend"


# ============================================================================
# Phase 3: /v1/responses Codex support & strict latest-only semantics
# ============================================================================


def _codex_run_completion_factory(thread_id="fake-thread-123", text="codex reply"):
    """Return an async generator factory mimicking FakeCodexBackend.run_completion."""

    async def fake_run_completion(**kwargs):
        yield {"type": "codex_session", "session_id": thread_id}
        yield {"type": "assistant", "content": [{"type": "text", "text": text}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": text,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }

    return fake_run_completion


def _setup_codex_backend(mock_cli, thread_id="fake-thread-123", text="codex reply"):
    """Register a codex backend with sensible defaults via mock_cli-like helpers."""
    from tests.conftest import FakeCodexBackend

    backend = FakeCodexBackend(thread_id=thread_id)
    BackendRegistry.register("codex", backend)
    return backend


# Helper: context manager to bypass codex auth validation
@contextmanager
def _bypass_codex_auth():
    """Patch _validate_backend_auth to be a no-op for codex tests."""
    with (
        patch.object(main, "_validate_backend_auth", return_value=None),
        patch.object(chat_module, "_validate_backend_auth", return_value=None),
        patch.object(responses_module, "validate_backend_auth_or_raise", return_value=None),
        patch.object(messages_module, "validate_backend_auth_or_raise", return_value=None),
    ):
        yield


# 1. test_responses_codex_streaming
def test_responses_codex_streaming(isolated_session_manager):
    """Codex model → backend dispatch → SSE stream with session state committed."""
    from tests.conftest import FakeCodexBackend

    backend = FakeCodexBackend(thread_id="stream-thread-1")
    BackendRegistry.register("codex", backend)

    def fake_run_completion(**kwargs):
        async def _gen():
            async for chunk in FakeCodexBackend(thread_id="stream-thread-1").run_completion(
                **kwargs
            ):
                yield chunk

        return _gen()

    async def fake_stream_response_chunks(**kwargs):
        kwargs["chunks_buffer"].extend(
            [
                {"type": "codex_session", "session_id": "stream-thread-1"},
                {"content": [{"type": "text", "text": "codex reply"}]},
            ]
        )
        kwargs["stream_result"]["success"] = True
        kwargs["stream_result"]["assistant_text"] = "codex reply"
        yield 'event: response.created\ndata: {"type":"response.created","sequence_number":0}\n\n'

    with (
        client_context() as (client, mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(
            main.streaming_utils, "stream_response_chunks", new=fake_stream_response_chunks
        ),
    ):
        mock_cli.run_completion = fake_run_completion

        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": "codex", "input": "Stream codex", "stream": True},
        ) as response:
            body = "".join(response.iter_text())

    session = next(iter(isolated_session_manager.sessions.values()))

    assert response.status_code == 200
    assert "response.created" in body
    assert session.turn_counter == 1
    assert session.backend == "codex"
    assert session.provider_session_id == "stream-thread-1"


# 2. test_responses_codex_non_streaming
def test_responses_codex_non_streaming(isolated_session_manager):
    """Codex model → non-streaming → ResponseObject with correct session state."""
    from tests.conftest import FakeCodexBackend

    backend = FakeCodexBackend(thread_id="nonstream-thread-1")
    BackendRegistry.register("codex", backend)

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "Non-stream codex"},
        )

    body = response.json()
    session_id, turn = main._parse_response_id(body["id"])
    session = isolated_session_manager.get_session(session_id)

    assert response.status_code == 200
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "codex reply"
    assert turn == 1
    assert session.turn_counter == 1
    assert session.backend == "codex"
    assert session.provider_session_id == "nonstream-thread-1"


# 3. test_responses_stale_previous_response_id
def test_responses_stale_previous_response_id(isolated_session_manager):
    """Past turn → 409 with latest response ID in message."""
    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 3

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello",
                "previous_response_id": main._make_response_id(sid, 1),
            },
        )

    assert response.status_code == 409
    body = response.json()
    assert "Stale previous_response_id" in body["error"]["message"]
    assert f"resp_{sid}_3" in body["error"]["message"]


# 4. test_responses_latest_previous_response_id
def test_responses_latest_previous_response_id(isolated_session_manager):
    """Current turn → success (follow-up works)."""
    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1

    async def fake_run_completion(**kwargs):
        yield {"subtype": "success", "result": "Follow-up answer"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "Follow-up answer"

        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Follow up",
                "previous_response_id": main._make_response_id(sid, 1),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["output"][0]["content"][0]["text"] == "Follow-up answer"
    assert session.turn_counter == 2


# 5. test_responses_backend_mismatch
def test_responses_backend_mismatch(isolated_session_manager):
    """Claude session + codex model → 400 backend mismatch."""
    from tests.conftest import FakeCodexBackend

    BackendRegistry.register("codex", FakeCodexBackend())

    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1
    session.backend = "claude"

    with client_context() as (client, _mock_cli), _bypass_codex_auth():
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex",
                "input": "Hello",
                "previous_response_id": main._make_response_id(sid, 1),
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert "backend" in body["error"]["message"].lower()
    assert "claude" in body["error"]["message"]
    assert "codex" in body["error"]["message"]


# 6. test_responses_codex_resume_with_thread_id
def test_responses_codex_resume_with_thread_id(isolated_session_manager):
    """Codex follow-up with captured thread_id passes resume correctly."""
    from tests.conftest import FakeCodexBackend

    backend = FakeCodexBackend(thread_id="resume-thread-2")
    BackendRegistry.register("codex", backend)

    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1
    session.backend = "codex"
    session.provider_session_id = "resume-thread-1"

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex",
                "input": "Continue",
                "previous_response_id": main._make_response_id(sid, 1),
            },
        )

    assert response.status_code == 200
    assert session.turn_counter == 2
    # Verify resume was passed to run_completion
    assert len(backend.calls) == 1
    assert backend.calls[0]["resume"] == "resume-thread-1"


# 7. test_responses_codex_resume_no_thread_id
def test_responses_codex_resume_no_thread_id(isolated_session_manager):
    """Codex follow-up without thread_id → 409."""
    from tests.conftest import FakeCodexBackend

    BackendRegistry.register("codex", FakeCodexBackend())

    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1
    session.backend = "codex"
    session.provider_session_id = None

    with client_context() as (client, _mock_cli), _bypass_codex_auth():
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex",
                "input": "Continue",
                "previous_response_id": main._make_response_id(sid, 1),
            },
        )

    assert response.status_code == 409
    assert "thread_id" in response.json()["error"]["message"]


# 8. test_responses_new_session_backend_tagged
def test_responses_new_session_backend_tagged(isolated_session_manager):
    """First turn sets session.backend correctly for both claude and codex."""
    from tests.conftest import FakeCodexBackend

    BackendRegistry.register("codex", FakeCodexBackend())

    # Codex new session
    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "Tag me"},
        )

    body = response.json()
    session_id, _ = main._parse_response_id(body["id"])
    session = isolated_session_manager.get_session(session_id)

    assert session.backend == "codex"

    # Claude new session
    async def fake_run_completion(**kwargs):
        yield {"subtype": "success", "result": "Claude answer"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "Claude answer"

        response2 = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Tag me too"},
        )

    body2 = response2.json()
    session_id2, _ = main._parse_response_id(body2["id"])
    session2 = isolated_session_manager.get_session(session_id2)

    assert session2.backend == "claude"


# 9. test_responses_claude_unchanged
def test_responses_claude_unchanged(isolated_session_manager):
    """Existing Claude behavior regression check — still works after refactor."""
    run_calls = []

    async def fake_run_completion(**kwargs):
        run_calls.append(kwargs)
        yield {"subtype": "success", "result": "Claude says hi"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
        patch.object(responses_module, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
    ):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "Claude says hi"

        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello Claude",
                "instructions": "Be helpful",
            },
        )

    body = response.json()
    session_id, turn = main._parse_response_id(body["id"])
    session = isolated_session_manager.get_session(session_id)

    assert response.status_code == 200
    assert body["output"][0]["content"][0]["text"] == "Claude says hi"
    assert turn == 1
    assert run_calls[0]["system_prompt"] == "Be helpful"
    assert run_calls[0]["mcp_servers"] == {"demo": {"type": "stdio"}}
    assert session.backend == "claude"


# 10. test_responses_codex_non_streaming_error
def test_responses_codex_non_streaming_error():
    """Codex error chunk → backend-agnostic 502."""
    from tests.conftest import FakeCodexBackend

    class ErrorCodexBackend(FakeCodexBackend):
        async def run_completion(self, **kwargs):
            yield {"is_error": True, "error_message": "codex rate limit"}

    BackendRegistry.register("codex", ErrorCodexBackend())

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "Fail me"},
        )

    assert response.status_code == 502
    assert "codex rate limit" in response.json()["error"]["message"]


# 11. test_responses_codex_streaming_error
def test_responses_codex_streaming_error(isolated_session_manager):
    """Codex mid-stream failure → error SSE event, no session commit."""
    from tests.conftest import FakeCodexBackend

    BackendRegistry.register("codex", FakeCodexBackend())

    def fake_run_completion(**kwargs):
        async def _gen():
            if False:
                yield None

        return _gen()

    async def exploding_stream(**kwargs):
        raise RuntimeError("codex stream boom")
        yield  # pragma: no cover

    with (
        client_context() as (client, mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(main.streaming_utils, "stream_response_chunks", new=exploding_stream),
    ):
        mock_cli.run_completion = fake_run_completion

        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": "codex", "input": "Stream fail", "stream": True},
        ) as response:
            body = "".join(response.iter_text())

    session = next(iter(isolated_session_manager.sessions.values()))

    assert response.status_code == 200
    assert "event: response.failed" in body
    assert '"status": "failed"' in body
    assert '"code": "server_error"' in body
    assert session.turn_counter == 0
    assert session.messages == []


# 12. test_responses_mcp_servers_not_passed_to_codex
def test_responses_mcp_servers_not_passed_to_codex(isolated_session_manager):
    """Verify mcp_servers=None for Codex backend."""
    from tests.conftest import FakeCodexBackend

    backend = FakeCodexBackend()
    BackendRegistry.register("codex", backend)

    run_calls = []
    original_run = backend.run_completion

    async def capturing_run(**kwargs):
        run_calls.append(kwargs)
        async for chunk in original_run(**kwargs):
            yield chunk

    backend.run_completion = capturing_run

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(
            main, "get_mcp_servers", return_value={"demo": {"type": "stdio", "command": "demo"}}
        ),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "MCP test"},
        )

    assert response.status_code == 200
    assert run_calls[0]["mcp_servers"] is None


# 13. test_responses_codex_token_estimation_fallback
def test_responses_codex_token_estimation_fallback(isolated_session_manager):
    """Verify backend.estimate_token_usage() when SDK usage unavailable."""
    from tests.conftest import FakeCodexBackend

    class NoUsageCodexBackend(FakeCodexBackend):
        async def run_completion(self, **kwargs):
            self.calls.append(kwargs)
            yield {"type": "codex_session", "session_id": self.thread_id}
            yield {"type": "assistant", "content": [{"type": "text", "text": "no usage"}]}
            # No result chunk with usage → forces estimate_token_usage fallback

    backend = NoUsageCodexBackend()
    BackendRegistry.register("codex", backend)

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "Estimate tokens"},
        )

    body = response.json()
    assert response.status_code == 200
    # Token usage should come from estimate_token_usage (10, 5)
    assert body["usage"]["input_tokens"] == 10
    assert body["usage"]["output_tokens"] == 5


# 14. test_responses_codex_session_meta_event_captured
def test_responses_codex_session_meta_event_captured(isolated_session_manager):
    """Verify codex_session meta-event flows into chunks and is captured."""
    from tests.conftest import FakeCodexBackend

    backend = FakeCodexBackend(thread_id="meta-thread-42")
    BackendRegistry.register("codex", backend)

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "Capture meta"},
        )

    body = response.json()
    session_id, _ = main._parse_response_id(body["id"])
    session = isolated_session_manager.get_session(session_id)

    assert response.status_code == 200
    assert session.provider_session_id == "meta-thread-42"


# 15. test_responses_concurrent_stale_id_race
def test_responses_concurrent_stale_id_race(isolated_session_manager):
    """Two requests with same latest previous_response_id → one succeeds, other gets 409.

    Proves lock serialization: the first request increments turn_counter,
    making the second request's previous_response_id stale.
    """
    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1

    async def fake_run_completion(**kwargs):
        yield {"subtype": "success", "result": "First wins"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = fake_run_completion
        mock_cli.parse_message.return_value = "First wins"

        prev_id = main._make_response_id(sid, 1)

        # First request succeeds
        response1 = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "First",
                "previous_response_id": prev_id,
            },
        )
        assert response1.status_code == 200
        assert session.turn_counter == 2

        # Second request with same (now stale) previous_response_id
        response2 = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Second",
                "previous_response_id": prev_id,
            },
        )

    assert response2.status_code == 409
    assert "Stale previous_response_id" in response2.json()["error"]["message"]


# 16. test_responses_non_streaming_failure_no_commit
def test_responses_non_streaming_failure_no_commit(isolated_session_manager):
    """Non-streaming failure → session.messages and turn_counter unchanged."""
    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1
    session.backend = "claude"
    original_messages = list(session.messages)

    async def failing_run_completion(**kwargs):
        yield {"is_error": True, "error_message": "backend exploded"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = failing_run_completion

        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Fail this",
                "previous_response_id": main._make_response_id(sid, 1),
            },
        )

    assert response.status_code == 502
    assert session.turn_counter == 1
    assert list(session.messages) == original_messages


# 17. test_responses_codex_failure_path_captures_thread_id
def test_responses_codex_failure_path_captures_thread_id(isolated_session_manager):
    """Codex turn fails but provider_session_id is still captured for next attempt."""
    from tests.conftest import FakeCodexBackend

    class FailAfterSessionCodexBackend(FakeCodexBackend):
        async def run_completion(self, **kwargs):
            self.calls.append(kwargs)
            # Emit session meta-event BEFORE failure
            yield {"type": "codex_session", "session_id": self.thread_id}
            # Then emit error
            yield {"is_error": True, "error_message": "turn failed after session start"}

    backend = FailAfterSessionCodexBackend(thread_id="fail-but-captured-thread")
    BackendRegistry.register("codex", backend)

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "Fail but capture"},
        )

    assert response.status_code == 502
    # turn_counter NOT incremented on failure
    session = next(iter(isolated_session_manager.sessions.values()))
    assert session.turn_counter == 0
    # But provider_session_id IS captured (for retry)
    assert session.provider_session_id == "fail-but-captured-thread"


# 18. test_responses_streaming_success_commits_with_streamed_text
def test_responses_streaming_success_commits_with_streamed_text(isolated_session_manager):
    """Streaming success uses assistant_text from stream_result (not parse_message).

    stream_response_chunks assembles full_text from actual deltas sent to the
    client and stores it in stream_result["assistant_text"].  The endpoint uses
    this value to commit the turn, avoiding a parse_message mismatch that would
    emit an error after response.completed.
    """

    async def fake_run_completion(**kwargs):
        yield {"content": [{"type": "text", "text": "streamed text"}]}
        yield {"subtype": "success", "result": "streamed text"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion = fake_run_completion
        # parse_message returns None — but the endpoint should not call it
        mock_cli.parse_message.return_value = None

        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Stream this", "stream": True},
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200

    # response.completed should be emitted with NO contradictory error
    assert "response.completed" in body
    assert "no parseable assistant text" not in body

    # Turn should be committed using the text from the stream
    session = next(iter(isolated_session_manager.sessions.values()))
    assert session.turn_counter == 1
    assert len(session.messages) == 2
    assert session.messages[0].content == "Stream this"
    # The committed text comes from stream_response_chunks' full_text
    assert session.messages[1].content == "streamed text"


# 19. test_responses_codex_exception_after_session_event_captures_thread_id
def test_responses_codex_exception_after_session_event_captures_thread_id(
    isolated_session_manager,
):
    """Codex run_completion raises after emitting codex_session → provider_session_id still captured."""
    from tests.conftest import FakeCodexBackend

    class ExceptionAfterSessionBackend(FakeCodexBackend):
        async def run_completion(self, **kwargs):
            self.calls.append(kwargs)
            yield {"type": "codex_session", "session_id": self.thread_id}
            raise RuntimeError("Codex subprocess crashed")

    backend = ExceptionAfterSessionBackend(thread_id="exception-thread-42")
    BackendRegistry.register("codex", backend)

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "Crash after session"},
        )

    assert response.status_code == 502
    session = next(iter(isolated_session_manager.sessions.values()))
    assert session.turn_counter == 0
    assert session.provider_session_id == "exception-thread-42"


# 20. test_responses_streaming_exception_after_session_event_captures_thread_id
def test_responses_streaming_exception_after_session_event_captures_thread_id(
    isolated_session_manager,
):
    """Streaming: Codex raises after emitting codex_session → provider_session_id still captured."""
    from tests.conftest import FakeCodexBackend

    class StreamExceptionBackend(FakeCodexBackend):
        async def run_completion(self, **kwargs):
            self.calls.append(kwargs)
            yield {"type": "codex_session", "session_id": self.thread_id}
            yield {"type": "assistant", "content": [{"type": "text", "text": "partial"}]}
            raise RuntimeError("Codex mid-stream crash")

    backend = StreamExceptionBackend(thread_id="stream-crash-thread")
    BackendRegistry.register("codex", backend)

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": "codex", "input": "Stream crash", "stream": True},
        )

    assert response.status_code == 200  # streaming already committed 200
    session = next(iter(isolated_session_manager.sessions.values()))
    assert session.turn_counter == 0  # NOT committed (stream failed)
    assert session.provider_session_id == "stream-crash-thread"


# 22. test_responses_streaming_immediate_exception_before_any_yield
def test_responses_streaming_immediate_exception_before_any_yield(
    isolated_session_manager,
):
    """Streaming: backend raises in run_completion() before yielding any chunks.

    Regression: tests #19 and #20 only exercise failures AFTER at least one
    chunk is yielded.  This covers the gap where run_completion() itself
    raises before producing any output.  The SSE stream must still return a
    200 (streaming already committed) with a response.failed error event,
    and session turn_counter must NOT be incremented.
    """
    from tests.conftest import FakeCodexBackend

    class ImmediateRaiseBackend(FakeCodexBackend):
        async def run_completion(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("Backend crashed before yielding")
            # Make this an async generator (unreachable yield keeps the signature)
            yield  # pragma: no cover

    backend = ImmediateRaiseBackend(thread_id="no-yield-crash")
    BackendRegistry.register("codex", backend)

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": "codex", "input": "Crash immediately", "stream": True},
        ) as response:
            body = "".join(response.iter_text())

    # Streaming already committed 200 before the backend was iterated
    assert response.status_code == 200

    # The SSE body must contain a failure event, NOT a success completion
    assert "response.failed" in body
    assert "response.completed" not in body
    assert "server_error" in body

    # Session turn_counter must NOT be incremented on failure
    session = next(iter(isolated_session_manager.sessions.values()))
    assert session.turn_counter == 0


# 23. test_responses_streaming_sync_raise_before_async_iteration
def test_responses_streaming_sync_raise_before_async_iteration(
    isolated_session_manager,
):
    """Streaming: run_completion() raises synchronously (not an async generator).

    Regression for the OUTER exception path in _run_stream().
    Tests #20 and #22 exercise failures that occur when iterating an async
    generator — those exceptions are caught inside stream_response_chunks().
    This test covers the case where run_completion() is NOT an async generator
    and raises before returning an iterable at all.  The exception hits the
    outer ``except`` block in _run_stream() (main.py) directly:

        chunk_source = backend.run_completion(**kwargs)   # <-- raises HERE
        async for line in stream_response_chunks(...):    # never reached
    """
    from tests.conftest import FakeCodexBackend

    class SyncRaiseBackend(FakeCodexBackend):
        def run_completion(self, **kwargs):
            """Plain sync method — NOT an async generator.

            Calling backend.run_completion(...) executes the body immediately
            and raises before any async iteration starts.
            """
            self.calls.append(kwargs)
            raise RuntimeError("Backend setup failed before streaming")

    backend = SyncRaiseBackend(thread_id="sync-raise-crash")
    BackendRegistry.register("codex", backend)

    with (
        client_context() as (client, _mock_cli),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": "codex", "input": "Sync crash", "stream": True},
        ) as response:
            body = "".join(response.iter_text())

    # Streaming already committed 200 before the backend was called
    assert response.status_code == 200

    # The SSE body must contain a response.failed event from the outer except
    assert "response.failed" in body
    assert "response.completed" not in body
    assert "server_error" in body

    # Session turn_counter must NOT be incremented on failure
    session = next(iter(isolated_session_manager.sessions.values()))
    assert session.turn_counter == 0


# 21. test_responses_truly_concurrent_lock_serialization
async def test_responses_truly_concurrent_lock_serialization(isolated_session_manager):
    """Two truly concurrent follow-up requests prove per-session lock serialization.

    Unlike test #15 (sequential), this test fires two requests simultaneously
    via asyncio.gather.  A slow backend holds the lock long enough to guarantee
    the second request is waiting on session.lock.  When the first completes and
    increments turn_counter (1 → 2), the second sees a stale previous_response_id
    (still pointing to turn 1, but turn_counter is now 2) and gets 409.

    Concurrency proof: the backend signals ``inside_backend`` when it starts,
    and the test records wall-clock ordering via ``entry_order`` to confirm
    both requests were dispatched before either completed.
    """
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1
    session.backend = "claude"

    prev_id = main._make_response_id(sid, 1)

    # -- Barrier machinery ------------------------------------------------
    # inside_backend is set when the first request enters the backend,
    # proving the lock is held.  backend_release lets the test unblock
    # the backend after the second request has had time to queue on the lock.
    inside_backend = asyncio.Event()
    backend_release = asyncio.Event()
    entry_order: list[str] = []  # tracks wall-clock ordering of backend calls

    async def slow_run_completion(**kwargs):
        """Backend that blocks until backend_release is set."""
        tag = f"call-{len(entry_order) + 1}"
        entry_order.append(tag)
        inside_backend.set()
        await backend_release.wait()
        yield {"subtype": "success", "result": "Lock holder wins"}

    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)
    mock_cli.run_completion = slow_run_completion
    mock_cli.parse_message.return_value = "Lock holder wins"
    mock_cli.build_options = MagicMock(
        side_effect=lambda req, resolved, overrides=None: {
            **(req.to_claude_options() if hasattr(req, "to_claude_options") else {}),
            **(overrides or {}),
            "model": resolved.provider_model,
            "permission_mode": "bypassPermissions",
        }
    )

    BackendRegistry.register("claude", mock_cli)

    with (
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        _bypass_codex_auth(),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
    ):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            payload = {
                "model": DEFAULT_MODEL,
                "input": "concurrent",
                "previous_response_id": prev_id,
            }

            async def send_request(label: str):
                return await client.post("/v1/responses", json=payload)

            async def release_after_overlap():
                """Wait until the first request is inside the backend, then
                yield control so the second request queues on session.lock,
                and finally unblock the backend."""
                await inside_backend.wait()
                # Yield a few times to let the event loop schedule the second
                # request's lock.acquire() coroutine.
                for _ in range(5):
                    await asyncio.sleep(0)
                backend_release.set()

            # Fire both requests + the release coordinator concurrently
            r1, r2, _ = await asyncio.gather(
                send_request("A"),
                send_request("B"),
                release_after_overlap(),
            )

    # -- Assertions -------------------------------------------------------
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409], (
        f"Expected exactly one 200 and one 409, got {r1.status_code} and {r2.status_code}"
    )

    # The 409 response must contain the stale-ID error message
    loser = r1 if r1.status_code == 409 else r2
    assert "Stale previous_response_id" in loser.json()["error"]["message"]

    # turn_counter advanced exactly once (from 1 → 2 by the winning request)
    assert session.turn_counter == 2

    # The backend was entered exactly once (the loser never reached it)
    assert len(entry_order) == 1, (
        f"Backend should have been called exactly once, but was called {len(entry_order)} times"
    )
