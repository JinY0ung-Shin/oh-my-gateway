"""Integration tests for user parameter in /v1/responses."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from fastapi.testclient import TestClient

import src.main as main
import src.routes.responses as responses_module
import src.routes.general as general_module
from src.backend_registry import BackendRegistry
from src.constants import DEFAULT_MODEL


@contextmanager
def client_context_with_workspace(mock_wm):
    """Create a TestClient with workspace_manager patched alongside standard mocks."""
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

    with (
        patch.object(main, "discover_backends", _mock_discover),
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(general_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(main, "validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch.object(responses_module, "validate_backend_auth_or_raise"),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
        patch.object(responses_module, "workspace_manager", mock_wm),
    ):
        with TestClient(main.app) as client:
            yield client, mock_cli

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


class TestUserParam:
    def test_user_field_accepted(self, isolated_session_manager):
        mock_wm = MagicMock()
        mock_wm.resolve.return_value = Path("/tmp/ws/alice")

        async def fake_run_completion(client, prompt, session):
            yield {"subtype": "success", "result": "Hello"}

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            mock_cli.run_completion_with_client = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Hello")
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hello", "user": "alice"},
            )

        assert resp.status_code == 200
        mock_wm.resolve.assert_called_once_with(
            "alice", sync_template=True, backend="claude"
        )

    def test_user_none_creates_temp_workspace(self, isolated_session_manager):
        mock_wm = MagicMock()
        mock_wm.resolve.return_value = Path("/tmp/ws/_tmp_abc123")

        async def fake_run_completion(client, prompt, session):
            yield {"subtype": "success", "result": "Hello"}

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            mock_cli.run_completion_with_client = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Hello")
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hello"},
            )

        assert resp.status_code == 200
        mock_wm.resolve.assert_called_once_with(
            None, sync_template=True, backend="claude"
        )

    def test_cwd_passed_to_run_completion(self, isolated_session_manager):
        """cwd is forwarded to create_client (which spawns the persistent SDK session)."""
        mock_wm = MagicMock()
        mock_wm.resolve.return_value = Path("/tmp/ws/alice/claude")
        create_calls = []

        async def fake_create_client(**kwargs):
            create_calls.append(kwargs)
            return object()

        async def fake_run_completion(client, prompt, session):
            yield {"subtype": "success", "result": "Hello"}

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            mock_cli.create_client = fake_create_client
            mock_cli.run_completion_with_client = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Hello")
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hello", "user": "alice"},
            )

        assert resp.status_code == 200
        assert len(create_calls) == 1
        assert create_calls[0]["cwd"] == "/tmp/ws/alice/claude"

    def test_responses_resolves_codex_workspace_with_codex_backend(
        self, isolated_session_manager
    ):
        mock_wm = MagicMock()
        mock_wm.resolve.return_value = Path("/tmp/ws/alice/codex")
        create_calls = []

        async def fake_create_client(**kwargs):
            create_calls.append(kwargs)
            return object()

        async def fake_run_completion(client, prompt, session):
            yield {"subtype": "success", "result": "Hello from Codex"}

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            BackendRegistry.unregister("claude")
            BackendRegistry.register("codex", mock_cli)
            mock_cli.create_client = fake_create_client
            mock_cli.run_completion_with_client = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Hello from Codex")
            resp = client.post(
                "/v1/responses",
                json={
                    "model": "codex/gpt-5.5",
                    "input": "hello",
                    "user": "alice",
                },
            )

        assert resp.status_code == 200
        mock_wm.resolve.assert_called_once_with(
            "alice", sync_template=True, backend="codex"
        )
        assert create_calls[0]["cwd"] == "/tmp/ws/alice/codex"

    def test_invalid_user_returns_400(self, isolated_session_manager):
        mock_wm = MagicMock()
        mock_wm.resolve.side_effect = ValueError("Invalid user identifier: '../bad'")

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hello", "user": "../bad"},
            )

        assert resp.status_code == 400
        assert "Invalid user" in resp.json()["error"]["message"]
        assert isolated_session_manager.sessions == {}


class TestUserSessionBinding:
    def test_followup_with_same_user_succeeds(self, isolated_session_manager):
        existing_session_id = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
        session = isolated_session_manager.get_or_create_session(existing_session_id)
        session.user = "alice"
        session.workspace = "/tmp/ws/alice"
        session.turn_counter = 1

        mock_wm = MagicMock()
        mock_wm.resolve.return_value = Path("/tmp/ws/alice")

        async def fake_run_completion(client, prompt, session):
            yield {"subtype": "success", "result": "Follow-up answer"}

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            mock_cli.run_completion_with_client = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Follow-up answer")
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "follow up",
                    "user": "alice",
                    "previous_response_id": f"resp_{existing_session_id}_1",
                },
            )

        assert resp.status_code == 200

    def test_followup_with_different_user_returns_400(self, isolated_session_manager):
        existing_session_id = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
        session = isolated_session_manager.get_or_create_session(existing_session_id)
        session.user = "alice"
        session.workspace = "/tmp/ws/alice"
        session.turn_counter = 1

        mock_wm = MagicMock()

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hijack",
                    "user": "eve",
                    "previous_response_id": f"resp_{existing_session_id}_1",
                },
            )

        assert resp.status_code == 400
        assert "user mismatch" in resp.json()["error"]["message"].lower()

    def test_followup_reuses_stored_workspace(self, isolated_session_manager):
        """Follow-up requests reuse the workspace stored in the session."""
        existing_session_id = "c2f6d3fd-1f1a-4c13-9c60-46b4df1d4d5f"
        session = isolated_session_manager.get_or_create_session(existing_session_id)
        session.user = "alice"
        session.workspace = "/tmp/ws/alice"
        session.turn_counter = 1

        mock_wm = MagicMock()
        create_calls = []

        async def fake_create_client(**kwargs):
            create_calls.append(kwargs)
            return object()

        async def fake_run_completion(client, prompt, session):
            yield {"subtype": "success", "result": "Follow-up answer"}

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            mock_cli.create_client = fake_create_client
            mock_cli.run_completion_with_client = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Follow-up answer")
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "follow up",
                    "user": "alice",
                    "previous_response_id": f"resp_{existing_session_id}_1",
                },
            )

        assert resp.status_code == 200
        # workspace_manager.resolve is called once with sync_template=False for the
        # early cwd lookup used by get_session rehydrate-on-miss; never with sync_template=True.
        mock_wm.resolve.assert_called_once_with(
            "alice", sync_template=False, backend="claude"
        )
        # cwd should be the stored workspace path (from session.workspace, not the early resolve)
        assert create_calls[0]["cwd"] == "/tmp/ws/alice"
