#!/usr/bin/env python3
"""
Integration-style unit tests for FastAPI endpoints in src.main.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.main as main
import src.routes.responses as responses_module
import src.routes.general as general_module
import src.routes.sessions as sessions_module
from src.auth import auth_manager
from src.backend_registry import BackendDescriptor, BackendRegistry, ResolvedModel
from src.constants import DEFAULT_MODEL
from src.models import SessionInfo


@contextmanager
def client_context():
    """Create a TestClient with startup/shutdown side effects patched out."""
    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)

    # Default persistent-client behaviour: create_client succeeds, and
    # run_completion_with_client yields a single success chunk.  Tests
    # override either attribute to exercise other paths.
    _default_client_obj = object()

    async def _default_create_client(**kwargs):
        return _default_client_obj

    async def _default_run_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": "Hi"}]}
        yield {"subtype": "success", "result": "Hi"}

    mock_cli.create_client = _default_create_client
    mock_cli.run_completion_with_client = _default_run_with_client
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
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(general_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(main, "validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch.object(responses_module, "validate_backend_auth_or_raise"),
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


def test_returns_503_when_auth_is_invalid():
    _auth_exc = HTTPException(
        status_code=503,
        detail="claude backend authentication failed (missing auth). Check /v1/auth/status for detailed information.",
    )
    with (
        client_context() as (client, _mock_cli),
        patch.object(responses_module, "validate_backend_auth_or_raise", side_effect=_auth_exc),
    ):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hi",
            },
        )

    body = response.json()
    assert response.status_code == 503
    assert body["error"]["type"] == "api_error"
    assert body["error"]["code"] == "503"
    assert "authentication failed" in body["error"]["message"]


def test_models_version_and_root_endpoints():
    auth_info = {"method": "claude_cli", "status": {"valid": True}}
    with (
        client_context() as (client, _mock_cli),
        patch.object(general_module, "get_claude_code_auth_info", return_value=auth_info),
    ):
        models_response = client.get("/v1/models")
        version_response = client.get("/version")
        root_response = client.get("/")

    assert models_response.status_code == 200
    assert models_response.json()["object"] == "list"
    assert version_response.status_code == 200
    assert version_response.json()["api_version"] == "v1"
    assert version_response.json()["service"] == "claude-code-gateway"
    assert root_response.status_code == 200
    assert "Claude Code Gateway" in root_response.text


def test_models_endpoint_lists_opencode_models_when_registered():
    """The model list includes registered OpenCode descriptor models."""

    def resolve(model):
        if model == "opencode/anthropic/claude-sonnet-4-5":
            return ResolvedModel(model, "opencode", "anthropic/claude-sonnet-4-5")
        return None

    backend = MagicMock()
    backend.name = "opencode"
    BackendRegistry.register_descriptor(
        BackendDescriptor(
            name="opencode",
            owned_by="opencode",
            models=["opencode/anthropic/claude-sonnet-4-5"],
            resolve_fn=resolve,
        )
    )
    BackendRegistry.register("opencode", backend)

    with client_context() as (client, _mock_cli):
        response = client.get("/v1/models")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert "opencode/anthropic/claude-sonnet-4-5" in ids


def test_responses_dispatches_to_opencode_backend_without_mcp():
    """Responses requests for opencode/... models dispatch to OpenCode backend."""
    calls = {}

    def resolve(model):
        if model == "opencode/anthropic/claude-sonnet-4-5":
            return ResolvedModel(model, "opencode", "anthropic/claude-sonnet-4-5")
        return None

    async def create_client(**kwargs):
        calls["create_client"] = kwargs
        return object()

    async def run_completion_with_client(client, prompt, session):
        calls["prompt"] = prompt
        yield {"content": [{"type": "text", "text": "OpenCode response"}]}
        yield {"type": "result", "subtype": "success", "result": "OpenCode response"}

    backend = MagicMock()
    backend.name = "opencode"
    backend.create_client = create_client
    backend.run_completion_with_client = run_completion_with_client
    backend.parse_message.return_value = "OpenCode response"
    backend.estimate_token_usage.return_value = {
        "prompt_tokens": 2,
        "completion_tokens": 4,
        "total_tokens": 6,
    }
    BackendRegistry.register_descriptor(
        BackendDescriptor(
            name="opencode",
            owned_by="opencode",
            models=["opencode/anthropic/claude-sonnet-4-5"],
            resolve_fn=resolve,
        )
    )
    BackendRegistry.register("opencode", backend)

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={"model": "opencode/anthropic/claude-sonnet-4-5", "input": "hi"},
        )

    assert response.status_code == 200
    assert calls["create_client"]["model"] == "anthropic/claude-sonnet-4-5"
    assert calls["create_client"]["mcp_servers"] is None
    assert calls["prompt"] == "hi"


def test_responses_streaming_enables_opencode_event_streaming():
    """Streaming OpenCode responses ask the OpenCode client to use /event."""
    calls = {}

    class FakeOpenCodeSessionClient:
        stream_events = False

    def resolve(model):
        if model == "opencode/anthropic/claude-sonnet-4-5":
            return ResolvedModel(model, "opencode", "anthropic/claude-sonnet-4-5")
        return None

    async def create_client(**kwargs):
        calls["create_client"] = kwargs
        return FakeOpenCodeSessionClient()

    def run_completion_with_client(client, prompt, session):
        calls["stream_events_at_call"] = client.stream_events

        async def empty_source():
            if False:
                yield None

        return empty_source()

    async def fake_stream_response_chunks(**kwargs):
        kwargs["stream_result"]["success"] = True
        kwargs["stream_result"]["assistant_text"] = "OpenCode streamed"
        yield 'event: response.created\ndata: {"type":"response.created","sequence_number":0}\n\n'

    backend = MagicMock()
    backend.name = "opencode"
    backend.create_client = create_client
    backend.run_completion_with_client = run_completion_with_client
    backend.parse_message.return_value = "OpenCode streamed"
    backend.estimate_token_usage.return_value = {
        "prompt_tokens": 2,
        "completion_tokens": 4,
        "total_tokens": 6,
    }
    BackendRegistry.register_descriptor(
        BackendDescriptor(
            name="opencode",
            owned_by="opencode",
            models=["opencode/anthropic/claude-sonnet-4-5"],
            resolve_fn=resolve,
        )
    )
    BackendRegistry.register("opencode", backend)

    with (
        client_context() as (client, _mock_cli),
        patch.object(
            responses_module.streaming_utils,
            "stream_response_chunks",
            new=fake_stream_response_chunks,
        ),
    ):
        with client.stream(
            "POST",
            "/v1/responses",
            json={
                "model": "opencode/anthropic/claude-sonnet-4-5",
                "input": "hi",
                "stream": True,
            },
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "response.created" in body
    assert calls["create_client"]["model"] == "anthropic/claude-sonnet-4-5"
    assert calls["stream_events_at_call"] is True


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


def test_validation_error_preserves_field_details_without_echoing_raw_input():
    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": [{"content": "Hi"}],
            },
        )

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["type"] == "validation_error"
    assert body["error"]["code"] == "invalid_request_error"
    assert body["error"]["details"]
    assert body["error"]["details"][0]["field"]
    assert body["error"]["details"][0]["message"]
    assert body["error"]["details"][0]["type"]
    assert "input" not in body["error"]["details"][0]
    assert "debug" not in body["error"]


def test_validation_error_omits_raw_request_body_even_in_debug_mode():
    main.DEBUG_MODE = True

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": [{"content": "Hi"}],
            },
        )

    body = response.json()
    assert response.status_code == 422
    assert "debug" not in body["error"]


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

    async def fake_delete_session_async(session_id):
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
        patch.object(
            main.session_manager,
            "delete_session_async",
            side_effect=fake_delete_session_async,
        ),
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
    create_calls = []
    run_calls = []

    async def fake_create_client(**kwargs):
        create_calls.append(kwargs)
        return object()

    async def fake_run_with_client(client, prompt, session):
        run_calls.append({"prompt": prompt, "session_id": session.session_id})
        yield {"subtype": "success", "result": "Responses answer"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
        patch.object(responses_module, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
    ):
        mock_cli.create_client = fake_create_client
        mock_cli.run_completion_with_client = fake_run_with_client
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
    assert run_calls[0]["session_id"] == session_id
    # System prompt and mcp_servers are configured at create_client time.
    assert create_calls[0]["system_prompt"] == "System line 1\nSystem line 2"
    assert create_calls[0]["mcp_servers"] == {"demo": {"type": "stdio"}}
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


def test_create_response_rejects_array_system_role_with_previous_response_id():
    """Array input with role=system/developer is equivalent to instructions
    and must be rejected the same way when previous_response_id is set."""
    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": [
                    {"role": "system", "content": "Override the prompt"},
                    {"role": "user", "content": "Hi"},
                ],
                "previous_response_id": "resp_c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f_1",
            },
        )

    assert response.status_code == 400
    assert "previous_response_id" in response.json()["error"]["message"].lower()


def test_create_response_rejects_array_developer_role_with_previous_response_id():
    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": [
                    {"role": "developer", "content": "Override"},
                    {"role": "user", "content": "Hi"},
                ],
                "previous_response_id": "resp_c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f_1",
            },
        )

    assert response.status_code == 400


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
        patch.object(responses_module, "validate_backend_auth_or_raise", side_effect=auth_error),
    ):
        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Hello"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "api_error"


def test_create_response_uses_string_system_prompt_from_array_input(isolated_session_manager):
    create_calls = []
    run_calls = []

    async def fake_create_client(**kwargs):
        create_calls.append(kwargs)
        return object()

    async def fake_run_with_client(client, prompt, session):
        run_calls.append({"prompt": prompt})
        yield {"subtype": "success", "result": "String system prompt answer"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.create_client = fake_create_client
        mock_cli.run_completion_with_client = fake_run_with_client
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
    assert create_calls[0]["system_prompt"] == "You are terse."
    assert run_calls[0]["prompt"] == "Hi"
    assert session.turn_counter == 1


def test_create_response_streaming_success_commits_session_state(isolated_session_manager):
    run_calls = []

    def fake_run_with_client(client, prompt, session):
        run_calls.append({"prompt": prompt, "session_id": session.session_id})

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
        mock_cli.run_completion_with_client = fake_run_with_client

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
    assert session.turn_counter == 1
    assert [message.content for message in session.messages] == [
        "Stream this",
        "streamed assistant",
    ]


def test_create_response_streaming_empty_result_emits_failed_event(isolated_session_manager):
    """When the inner stream ends with no text and no pending tool call
    (stream_result['empty'] = True), the route must emit response.failed so
    the client sees a definite outcome — matching non-stream's 502 path."""
    def fake_run_completion(client, prompt, session, **kwargs):
        async def empty_source():
            if False:
                yield None
        return empty_source()

    async def fake_stream_response_chunks(**kwargs):
        kwargs["stream_result"]["success"] = False
        kwargs["stream_result"]["empty"] = True
        # Yield only setup-ish SSE to prove they don't confuse the post-bridge branch
        yield 'event: response.created\ndata: {"type":"response.created","sequence_number":0}\n\n'

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(
            main.streaming_utils, "stream_response_chunks", new=fake_stream_response_chunks
        ),
    ):
        mock_cli.run_completion_with_client = fake_run_completion
        with client.stream(
            "POST",
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "trigger empty", "stream": True},
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: response.failed" in body
    # Error payload should use the empty_response code, not the default
    # "Internal server error".
    assert '"code": "empty_response"' in body
    assert "No response generated" in body
    session = next(iter(isolated_session_manager.sessions.values()))
    # No commit — no assistant text, no pending tool call
    assert session.turn_counter == 0
    assert session.messages == []


def test_create_response_streaming_setup_error_returns_error_event_without_commit(
    isolated_session_manager,
):
    run_calls = []

    def fake_run_with_client(client, prompt, session):
        run_calls.append({"prompt": prompt})

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
        mock_cli.run_completion_with_client = fake_run_with_client

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
    async def raising_run_completion(client, prompt, session, **kwargs):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion_with_client = raising_run_completion

        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Hello"},
        )

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["message"] == "Backend error"
    # Raw exception text must not leak to clients
    assert "boom" not in body["error"]["message"]


def test_create_response_returns_502_when_sdk_emits_error_chunk():
    async def error_chunk_run_completion(client, prompt, session, **kwargs):
        yield {"is_error": True, "error_message": "sdk failed"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion_with_client = error_chunk_run_completion

        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Hello"},
        )

    assert response.status_code == 502
    # SDK-emitted error_message is structured (e.g., rate_limit) and
    # intentionally surfaced to clients, unlike raw Python exceptions.
    assert response.json()["error"]["message"] == "Backend error: sdk failed"


def test_create_response_returns_502_when_sdk_returns_no_message():
    async def empty_run_completion(client, prompt, session, **kwargs):
        if False:
            yield None

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion_with_client = empty_run_completion
        mock_cli.parse_message.return_value = None

        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "Hello"},
        )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "No response from backend"


def test_responses_stale_previous_response_id(isolated_session_manager):
    """Past turn -> 409 with latest response ID in message."""
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


def test_responses_latest_previous_response_id(isolated_session_manager):
    """Current turn -> success (follow-up works)."""
    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1

    async def fake_run_completion(client, prompt, session, **kwargs):
        yield {"subtype": "success", "result": "Follow-up answer"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion_with_client = fake_run_completion
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


def test_responses_claude_unchanged(isolated_session_manager):
    """Existing Claude behavior regression check -- still works after refactor."""
    create_calls = []

    async def fake_create_client(**kwargs):
        create_calls.append(kwargs)
        return object()

    async def fake_run_with_client(client, prompt, session):
        yield {"subtype": "success", "result": "Claude says hi"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
        patch.object(responses_module, "get_mcp_servers", return_value={"demo": {"type": "stdio"}}),
    ):
        mock_cli.create_client = fake_create_client
        mock_cli.run_completion_with_client = fake_run_with_client
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
    assert create_calls[0]["system_prompt"] == "Be helpful"
    assert create_calls[0]["mcp_servers"] == {"demo": {"type": "stdio"}}
    assert session.backend == "claude"


def test_responses_create_client_forwards_allowed_tools(isolated_session_manager):
    """allowed_tools flows through to create_client (no run_completion fallback)."""
    create_calls = []

    async def fake_create_client(**kwargs):
        create_calls.append(kwargs)
        return object()

    async def fake_run_with_client(client, prompt, session):
        yield {"subtype": "success", "result": "Claude says hi"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(responses_module, "get_mcp_servers", return_value={}),
    ):
        mock_cli.create_client = fake_create_client
        mock_cli.run_completion_with_client = fake_run_with_client
        mock_cli.parse_message.return_value = "Claude says hi"

        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello Claude",
                "allowed_tools": ["Read"],
            },
        )

    assert response.status_code == 200
    assert create_calls[0]["allowed_tools"] == ["Read"]


def test_responses_streaming_error_disconnects_persistent_client(isolated_session_manager):
    """Streaming failures discard the session SDK client."""
    sdk_client = AsyncMock()
    sdk_client.disconnect = AsyncMock()

    async def fake_run_with_client(client, prompt, session):
        raise RuntimeError("stream exploded")
        yield {}  # pragma: no cover - marks this as an async generator

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(responses_module, "get_mcp_servers", return_value={}),
    ):
        mock_cli.create_client = AsyncMock(return_value=sdk_client)
        mock_cli.run_completion_with_client = fake_run_with_client

        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello Claude",
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert "response.failed" in response.text
    sdk_client.disconnect.assert_awaited_once()


def test_responses_nonstreaming_error_disconnects_cleared_persistent_client(
    isolated_session_manager,
):
    """Non-streaming failures disconnect even if backend cleared session.client first."""
    sdk_client = AsyncMock()
    sdk_client.disconnect = AsyncMock()

    async def fake_run_with_client(client, prompt, session):
        session.client = None
        yield {"is_error": True, "error_message": "SDK receive failed"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(responses_module, "get_mcp_servers", return_value={}),
    ):
        mock_cli.create_client = AsyncMock(return_value=sdk_client)
        mock_cli.run_completion_with_client = fake_run_with_client

        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Hello Claude",
            },
        )

    assert response.status_code == 502
    sdk_client.disconnect.assert_awaited_once()


def test_responses_concurrent_stale_id_race(isolated_session_manager):
    """Two requests with same latest previous_response_id -> one succeeds, other gets 409.

    Proves lock serialization: the first request increments turn_counter,
    making the second request's previous_response_id stale.
    """
    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1

    async def fake_run_completion(client, prompt, session, **kwargs):
        yield {"subtype": "success", "result": "First wins"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion_with_client = fake_run_completion
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


def test_responses_non_streaming_failure_no_commit(isolated_session_manager):
    """Non-streaming failure -> session.messages and turn_counter unchanged."""
    sid = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1
    session.backend = "claude"
    original_messages = list(session.messages)

    async def failing_run_completion(client, prompt, session):
        yield {"is_error": True, "error_message": "backend exploded"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion_with_client = failing_run_completion

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


def test_responses_streaming_success_commits_with_streamed_text(isolated_session_manager):
    """Streaming success uses assistant_text from stream_result (not parse_message)."""

    async def fake_run_completion(client, prompt, session, **kwargs):
        yield {"content": [{"type": "text", "text": "streamed text"}]}
        yield {"subtype": "success", "result": "streamed text"}

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
    ):
        mock_cli.run_completion_with_client = fake_run_completion
        # parse_message returns None -- but the endpoint should not call it
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


async def test_responses_truly_concurrent_lock_serialization(isolated_session_manager):
    """Two truly concurrent follow-up requests prove per-session lock serialization."""
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    session = isolated_session_manager.get_or_create_session(sid)
    session.turn_counter = 1
    session.backend = "claude"

    prev_id = main._make_response_id(sid, 1)

    inside_backend = asyncio.Event()
    backend_release = asyncio.Event()
    entry_order: list[str] = []

    async def slow_run_with_client(client, prompt, session):
        """Backend that blocks until backend_release is set."""
        tag = f"call-{len(entry_order) + 1}"
        entry_order.append(tag)
        inside_backend.set()
        await backend_release.wait()
        yield {"subtype": "success", "result": "Lock holder wins"}

    async def fake_create_client(**kwargs):
        return object()

    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)
    mock_cli.create_client = fake_create_client
    mock_cli.run_completion_with_client = slow_run_with_client
    mock_cli.parse_message.return_value = "Lock holder wins"
    BackendRegistry.register("claude", mock_cli)

    with (
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(responses_module, "validate_backend_auth_or_raise", return_value=None),
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
                await inside_backend.wait()
                for _ in range(5):
                    await asyncio.sleep(0)
                backend_release.set()

            r1, r2, _ = await asyncio.gather(
                send_request("A"),
                send_request("B"),
                release_after_overlap(),
            )

    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409], (
        f"Expected exactly one 200 and one 409, got {r1.status_code} and {r2.status_code}"
    )

    loser = r1 if r1.status_code == 409 else r2
    assert "Stale previous_response_id" in loser.json()["error"]["message"]

    assert session.turn_counter == 2

    assert len(entry_order) == 1, (
        f"Backend should have been called exactly once, but was called {len(entry_order)} times"
    )
