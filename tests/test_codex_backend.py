"""Codex backend tests."""

import asyncio
import importlib
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_codex_descriptor_resolves_prefixed_models(monkeypatch):
    """Codex descriptor resolves codex/<model> IDs without claiming bare models."""
    monkeypatch.setenv("CODEX_MODELS", "gpt-5.5,gpt-5.3-codex")

    import src.backends.codex as codex_pkg

    codex_pkg = importlib.reload(codex_pkg)

    resolved = codex_pkg.CODEX_DESCRIPTOR.resolve_fn("codex/gpt-5.5")

    assert resolved is not None
    assert resolved.public_model == "codex/gpt-5.5"
    assert resolved.backend == "codex"
    assert resolved.provider_model == "gpt-5.5"
    assert codex_pkg.CODEX_DESCRIPTOR.models == ["codex/gpt-5.5", "codex/gpt-5.3-codex"]
    assert codex_pkg.CODEX_DESCRIPTOR.resolve_fn("gpt-5.5") is None
    assert codex_pkg.CODEX_DESCRIPTOR.resolve_fn("codex/") is None


def test_codex_auth_provider_validates_binary(monkeypatch):
    """Codex auth is valid when the local codex binary is available."""
    monkeypatch.setattr("src.backends.codex.auth.shutil.which", lambda name: "/bin/codex")
    monkeypatch.setenv("CODEX_BIN", "codex")

    from src.backends.codex.auth import CodexAuthProvider

    status = CodexAuthProvider().validate()

    assert status["valid"] is True
    assert status["errors"] == []
    assert status["config"] == {"mode": "app-server", "binary": "/bin/codex"}


def test_codex_auth_provider_reports_missing_binary(monkeypatch):
    """Auth diagnostics report when Codex CLI is unavailable."""
    monkeypatch.setattr("src.backends.codex.auth.shutil.which", lambda name: None)
    monkeypatch.setenv("CODEX_BIN", "codex-missing")

    from src.backends.codex.auth import CodexAuthProvider

    status = CodexAuthProvider().validate()

    assert status["valid"] is False
    assert status["errors"] == ["codex binary not found on PATH"]
    assert status["config"] == {"mode": "app-server", "binary": "codex-missing"}


def test_codex_auth_env_includes_codex_settings(monkeypatch):
    """Backend env diagnostics expose Codex-specific runtime settings."""
    monkeypatch.setenv("CODEX_BIN", "/opt/codex")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "never")
    monkeypatch.setenv("CODEX_SANDBOX", "workspaceWrite")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from src.backends.codex.auth import CodexAuthProvider

    env = CodexAuthProvider().build_env()

    assert env["CODEX_BIN"] == "/opt/codex"
    assert env["CODEX_HOME"] == "/tmp/codex-home"
    assert env["CODEX_APPROVAL_POLICY"] == "never"
    assert env["CODEX_SANDBOX"] == "workspaceWrite"
    assert env["OPENAI_API_KEY"] == "sk-test"


def test_codex_sandbox_mode_uses_cli_enum_and_normalizes_legacy_aliases(monkeypatch):
    """Codex sandbox values sent to app-server match the current CLI schema."""
    from src.backends.codex.constants import sandbox_mode

    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    assert sandbox_mode() == "danger-full-access"

    monkeypatch.setenv("CODEX_SANDBOX", "workspaceWrite")
    assert sandbox_mode() == "workspace-write"

    monkeypatch.setenv("CODEX_SANDBOX", "readOnly")
    assert sandbox_mode() == "read-only"

    monkeypatch.setenv("CODEX_SANDBOX", "dangerFullAccess")
    assert sandbox_mode() == "danger-full-access"


class FakeRpc:
    def __init__(self):
        self.closed = False
        self.thread_start_calls = []
        self.thread_resume_calls = []
        self.turn_start_calls = []
        self.notifications = []

    def start(self):
        pass

    def close(self):
        self.closed = True

    def thread_start(self, params):
        self.thread_start_calls.append(params)
        return {"thread": {"id": "thr_codex"}}

    def thread_resume(self, thread_id, params):
        self.thread_resume_calls.append((thread_id, params))
        return {"thread": {"id": thread_id}}

    def turn_start(self, thread_id, input_items, params):
        self.turn_start_calls.append((thread_id, input_items, params))
        return {"turn": {"id": "turn_1", "status": "inProgress"}}

    def next_notification(self):
        if not self.notifications:
            raise AssertionError("test exhausted notifications")
        return self.notifications.pop(0)


@pytest.mark.asyncio
async def test_codex_client_starts_thread_and_converts_completed_turn(monkeypatch, tmp_path):
    """Codex client converts app-server final agent messages into gateway chunks."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "item_1",
                "delta": "Hello",
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "item": {
                    "type": "agentMessage",
                    "id": "item_1",
                    "phase": "final_answer",
                    "text": "Hello from Codex",
                },
            },
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 3,
                        "cachedInputTokens": 0,
                        "outputTokens": 4,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 8,
                    },
                    "total": {
                        "inputTokens": 3,
                        "cachedInputTokens": 0,
                        "outputTokens": 4,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 8,
                    },
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        },
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        system_prompt="extra instructions",
        cwd=str(tmp_path),
    )
    chunks = [
        chunk async for chunk in backend.run_completion_with_client(client, "say hello", session)
    ]

    assert fake_rpc.thread_start_calls == [
        {
            "model": "gpt-5.5",
            "cwd": str(tmp_path),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "developerInstructions": "extra instructions",
            "serviceName": "oh-my-gateway",
        }
    ]
    assert fake_rpc.turn_start_calls == [
        (
            "thr_codex",
            [{"type": "text", "text": "say hello"}],
            {"model": "gpt-5.5", "cwd": str(tmp_path), "approvalPolicy": "never"},
        )
    ]
    assert chunks[0] == {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello"},
        },
    }
    assert chunks[-2]["content"] == [{"type": "text", "text": "Hello from Codex"}]
    assert chunks[-2]["usage"] == {"input_tokens": 3, "output_tokens": 4}
    assert chunks[-1]["type"] == "result"
    assert chunks[-1]["result"] == "Hello from Codex"
    assert backend.parse_message(chunks) == "Hello from Codex"
    assert getattr(session, "codex_thread_id") == "thr_codex"

    await client.disconnect()
    assert fake_rpc.closed is False
    backend.close()
    assert fake_rpc.closed is True


@pytest.mark.asyncio
async def test_codex_client_finishes_when_thread_returns_idle_without_turn_completed(
    monkeypatch,
):
    """Current Codex CLI can end turns with thread idle instead of turn/completed."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "item_1",
                "delta": "hi",
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "item": {
                    "type": "agentMessage",
                    "id": "item_1",
                    "phase": "final_answer",
                    "text": "hi",
                },
            },
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 2,
                        "cachedInputTokens": 1,
                        "outputTokens": 1,
                    },
                },
            },
        },
        {
            "method": "thread/status/changed",
            "params": {
                "threadId": "thr_codex",
                "status": {"type": "idle"},
            },
        },
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert chunks[0] == {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hi"},
        },
    }
    assert chunks[-2]["content"] == [{"type": "text", "text": "hi"}]
    assert chunks[-2]["usage"] == {"input_tokens": 3, "output_tokens": 1}
    assert chunks[-1]["type"] == "result"
    assert chunks[-1]["result"] == "hi"


@pytest.mark.asyncio
async def test_codex_client_reuses_shared_rpc_process(monkeypatch):
    """One Codex backend process is reused across gateway sessions."""
    created = []

    def fake_factory(**kwargs):
        rpc = FakeRpc()
        created.append((rpc, kwargs))
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", fake_factory)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session_one = SimpleNamespace(session_id="gw-session-1")
    session_two = SimpleNamespace(session_id="gw-session-2")

    client_one = await backend.create_client(session=session_one, model="gpt-5.5")
    client_two = await backend.create_client(session=session_two, model="gpt-5.5")

    assert client_one.thread_id == "thr_codex"
    assert client_two.thread_id == "thr_codex"
    assert len(created) == 1
    rpc, kwargs = created[0]
    assert kwargs["cwd"] is None
    assert len(rpc.thread_start_calls) == 2

    await client_one.disconnect()
    await client_two.disconnect()
    assert rpc.closed is False

    backend.close()
    assert rpc.closed is True


@pytest.mark.asyncio
async def test_codex_client_reuses_session_thread(monkeypatch):
    """Existing gateway sessions resume the stored Codex thread id."""
    fake_rpc = FakeRpc()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", codex_thread_id="thr_existing")

    client = await backend.create_client(session=session, model="gpt-5.5")

    assert client.thread_id == "thr_existing"
    assert fake_rpc.thread_start_calls == []
    assert fake_rpc.thread_resume_calls == [
        (
            "thr_existing",
            {"model": "gpt-5.5", "approvalPolicy": "never", "sandbox": "danger-full-access"},
        )
    ]


@pytest.mark.asyncio
async def test_codex_client_closes_rpc_when_thread_start_fails(monkeypatch):
    """Partially-created Codex subprocesses are closed when thread setup fails."""

    class FailingThreadStartRpc(FakeRpc):
        def thread_start(self, params):
            self.thread_start_calls.append(params)
            raise RuntimeError("thread start failed")

    fake_rpc = FailingThreadStartRpc()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")

    with pytest.raises(RuntimeError, match="thread start failed"):
        await backend.create_client(session=session, model="gpt-5.5")

    assert fake_rpc.closed is True


@pytest.mark.asyncio
async def test_codex_client_restarts_shared_rpc_after_turn_error(monkeypatch):
    """Transport failures close the shared app-server so the next request restarts it."""

    class FailingTurnRpc(FakeRpc):
        def turn_start(self, thread_id, input_items, params):
            self.turn_start_calls.append((thread_id, input_items, params))
            raise RuntimeError("transport failed")

    created = []

    def fake_factory(**kwargs):
        rpc = FailingTurnRpc() if not created else FakeRpc()
        created.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", fake_factory)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert chunks == [
        {"type": "error", "is_error": True, "error_message": "transport failed"}
    ]
    assert created[0].closed is True

    await backend.create_client(session=SimpleNamespace(session_id="gw-session-2"), model="gpt-5.5")

    assert len(created) == 2
    assert created[1].closed is False


@pytest.mark.asyncio
async def test_codex_client_filters_metadata_env(monkeypatch):
    """Only allowlisted metadata keys are passed to the Codex subprocess env."""
    fake_rpc = FakeRpc()
    created_kwargs = {}

    def fake_factory(**kwargs):
        created_kwargs.update(kwargs)
        return fake_rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", fake_factory)
    monkeypatch.setattr("src.constants.METADATA_ENV_ALLOWLIST", frozenset({"SAFE_ENV"}))

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")

    await backend.create_client(
        session=session,
        model="gpt-5.5",
        extra_env={"SAFE_ENV": "1", "DROP_ENV": "2"},
    )

    assert created_kwargs["env"] == {"SAFE_ENV": "1"}


def test_codex_client_reports_failed_turn():
    """Failed Codex turns become gateway backend error chunks."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    chunks = list(
        backend._chunks_from_notifications(
            turn_id="turn_1",
            notifications=[
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "id": "turn_1",
                            "status": "failed",
                            "error": {"message": "auth failed"},
                        }
                    },
                }
            ],
        )
    )

    assert chunks == [{"type": "error", "is_error": True, "error_message": "auth failed"}]


def test_codex_json_rpc_client_times_out_waiting_for_message():
    """JSON-RPC reads fail fast instead of blocking forever on silent app-server."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    rpc = CodexJsonRpcClient(read_timeout=0.01)
    rpc._proc = proc
    try:
        with pytest.raises(CodexAppServerError, match="Timed out waiting"):
            rpc._read_message()
    finally:
        rpc.close()


def test_codex_json_rpc_client_does_not_auto_accept_approval_requests():
    """Approval requests are cancelled until the gateway has an explicit approval flow."""
    from src.backends.codex.client import CodexJsonRpcClient

    rpc = CodexJsonRpcClient()

    assert rpc._handle_server_request(
        {"method": "item/commandExecution/requestApproval"}
    ) == {"decision": "cancel"}
    assert rpc._handle_server_request({"method": "item/fileChange/requestApproval"}) == {
        "decision": "cancel"
    }
    assert rpc._handle_server_request({"method": "item/permissions/requestApproval"}) == {
        "permissions": {},
        "scope": "turn",
    }


@pytest.mark.asyncio
async def test_codex_session_disconnect_is_async(monkeypatch):
    """Session cleanup can await Codex handles without closing shared backend RPC."""
    fake_rpc = FakeRpc()

    from src.backends.codex.client import CodexSessionClient

    client = CodexSessionClient(rpc=fake_rpc, thread_id="thr", model=None, cwd=None)

    await asyncio.wait_for(client.disconnect(), timeout=1)

    assert fake_rpc.closed is False
