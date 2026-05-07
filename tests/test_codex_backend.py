"""Codex backend tests."""

import asyncio
import importlib
import subprocess
import sys
from unittest.mock import AsyncMock
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
        self.respond_calls = []
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

    def respond(self, request_id, result):
        self.respond_calls.append((request_id, result))


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
async def test_codex_client_exposes_command_approval_as_pending_tool_call(monkeypatch):
    """Codex approval JSON-RPC requests pause the turn as AskUserQuestion."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "id": "approval_1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "cmd_1",
                "command": "pytest -q",
                "cwd": "/repo",
                "reason": "Run the test suite",
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert chunks == [
        {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "approval_1",
                    "name": "codex_approval",
                    "input": {
                        "kind": "command",
                        "question": "Codex requests approval to run command: pytest -q",
                        "command": "pytest -q",
                        "cwd": "/repo",
                        "reason": "Run the test suite",
                        "itemId": "cmd_1",
                        "options": [
                            {"label": "accept", "description": "Approve this request once."},
                            {
                                "label": "acceptForSession",
                                "description": "Approve matching requests for this session.",
                            },
                            {"label": "decline", "description": "Deny and let Codex continue."},
                        ],
                    },
                    "metadata": {
                        "codex_approval_request_id": "approval_1",
                        "codex_approval_method": "item/commandExecution/requestApproval",
                        "codex_thread_id": "thr_codex",
                        "codex_turn_id": "turn_1",
                    },
                }
            ],
        }
    ]
    assert session.pending_tool_call == {
        "call_id": "approval_1",
        "name": "AskUserQuestion",
        "arguments": {
            "kind": "command",
            "question": "Codex requests approval to run command: pytest -q",
            "command": "pytest -q",
            "cwd": "/repo",
            "reason": "Run the test suite",
            "itemId": "cmd_1",
            "options": [
                {"label": "accept", "description": "Approve this request once."},
                {
                    "label": "acceptForSession",
                    "description": "Approve matching requests for this session.",
                },
                {"label": "decline", "description": "Deny and let Codex continue."},
            ],
        },
        "backend": "codex",
        "codex_resume": "approval",
    }


def test_codex_client_exposes_file_change_and_permission_approval_arguments():
    """Non-command approval kinds preserve the app-server approval context."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()

    file_chunks = list(
        backend._chunks_from_notifications(
            thread_id="thr_codex",
            turn_id="turn_1",
            notifications=[
                {
                    "id": "file_approval_1",
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "threadId": "thr_codex",
                        "turnId": "turn_1",
                        "itemId": "file_1",
                        "grantRoot": "/repo",
                        "reason": "Need write access",
                    },
                }
            ],
        )
    )
    file_input = file_chunks[0]["tool_chunk"]["content"][0]["input"]
    assert file_input["kind"] == "file_change"
    assert file_input["grantRoot"] == "/repo"
    assert file_input["itemId"] == "file_1"
    assert [option["label"] for option in file_input["options"]] == [
        "accept",
        "acceptForSession",
        "decline",
        "cancel",
    ]

    permissions = {"fileSystem": {"read": ["/repo"]}, "network": {"enabled": True}}
    permission_chunks = list(
        backend._chunks_from_notifications(
            thread_id="thr_codex",
            turn_id="turn_1",
            notifications=[
                {
                    "id": "permission_approval_1",
                    "method": "item/permissions/requestApproval",
                    "params": {
                        "threadId": "thr_codex",
                        "turnId": "turn_1",
                        "itemId": "perm_1",
                        "cwd": "/repo",
                        "permissions": permissions,
                        "reason": "Need broader access",
                    },
                }
            ],
        )
    )
    permission_input = permission_chunks[0]["tool_chunk"]["content"][0]["input"]
    assert permission_input["kind"] == "permissions"
    assert permission_input["cwd"] == "/repo"
    assert permission_input["permissions"] == permissions
    assert permission_input["itemId"] == "perm_1"
    assert [option["label"] for option in permission_input["options"]] == [
        "accept",
        "acceptForSession",
        "decline",
    ]


def test_codex_client_preserves_structured_command_approval_decisions():
    """Structured Codex decisions can be displayed and selected by label."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    execpolicy_decision = {
        "acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow command pytest"]}
    }
    network_decision = {
        "applyNetworkPolicyAmendment": {
            "network_policy_amendment": {"action": "allow", "host": "example.com"}
        }
    }
    params = {
        "threadId": "thr_codex",
        "turnId": "turn_1",
        "itemId": "cmd_1",
        "command": "curl https://example.com",
        "proposedExecpolicyAmendment": ["allow command pytest"],
        "proposedNetworkPolicyAmendments": [{"action": "allow", "host": "example.com"}],
        "availableDecisions": [execpolicy_decision, network_decision, "decline"],
    }

    arguments = backend._approval_arguments(
        "item/commandExecution/requestApproval",
        params,
    )

    assert arguments["proposedExecpolicyAmendment"] == ["allow command pytest"]
    assert arguments["proposedNetworkPolicyAmendments"] == [
        {"action": "allow", "host": "example.com"}
    ]
    assert arguments["options"] == [
        {
            "label": "acceptWithExecpolicyAmendment",
            "description": "Approve and apply the proposed execpolicy amendment.",
            "decision": execpolicy_decision,
        },
        {
            "label": "applyNetworkPolicyAmendment:allow:example.com",
            "description": "Choose applyNetworkPolicyAmendment:allow:example.com.",
            "decision": network_decision,
        },
        {"label": "decline", "description": "Deny and let Codex continue."},
    ]
    assert backend._approval_result_from_output(
        "item/commandExecution/requestApproval",
        "acceptWithExecpolicyAmendment",
        params,
    ) == {"decision": execpolicy_decision}
    assert backend._approval_result_from_output(
        "item/commandExecution/requestApproval",
        "applyNetworkPolicyAmendment:allow:example.com",
        params,
    ) == {"decision": network_decision}


def test_codex_client_maps_permission_approval_outputs():
    """Permission approvals return the schema-required permissions/scope object."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    permissions = {"fileSystem": {"read": ["/repo"]}, "network": {"enabled": True}}
    params = {"permissions": permissions}

    assert backend._approval_result_from_output(
        "item/permissions/requestApproval",
        "accept",
        params,
    ) == {"permissions": permissions, "scope": "turn"}
    assert backend._approval_result_from_output(
        "item/permissions/requestApproval",
        "always",
        params,
    ) == {"permissions": permissions, "scope": "session"}
    assert backend._approval_result_from_output(
        "item/permissions/requestApproval",
        "decline",
        params,
    ) == {"permissions": {}, "scope": "turn"}


def test_codex_client_logs_unrecognized_structured_approval_output(caplog):
    """Unknown structured approval outputs fail closed but leave an operator breadcrumb."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()

    with caplog.at_level("WARNING", logger="src.backends.codex.client"):
        result = backend._approval_result_from_output(
            "item/permissions/requestApproval",
            '{"foo": 1}',
            {"permissions": {"fileSystem": {"read": ["/repo"]}}},
        )

    assert result == {"permissions": {}, "scope": "turn"}
    assert "Unrecognized Codex approval output" in caplog.text
    assert "{'foo': 1}" in caplog.text


@pytest.mark.asyncio
async def test_codex_client_resumes_command_approval_and_continues_turn(monkeypatch):
    """Codex approval continuation responds to app-server and reads remaining events."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thr_codex", "requestId": "approval_1"},
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "item": {
                    "type": "commandExecution",
                    "id": "cmd_1",
                    "command": "pytest -q",
                    "cwd": "/repo",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "18 passed",
                    "commandActions": [],
                },
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "item": {
                    "type": "agentMessage",
                    "id": "msg_1",
                    "phase": "final_answer",
                    "text": "Tests passed.",
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
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")
    client.pending_approval_request_id = "approval_1"
    client.pending_approval_method = "item/commandExecution/requestApproval"
    client.pending_approval_turn_id = "turn_1"
    client.pending_approval_params = {"turnId": "turn_1"}

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            client,
            "approval_1",
            "accept",
            session,
        )
    ]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "accept"})]
    assert {
        "type": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "cmd_1",
                "content": "18 passed",
                "is_error": False,
            }
        ],
    } in chunks
    assert chunks[-2]["content"] == [{"type": "text", "text": "Tests passed."}]
    assert chunks[-1]["result"] == "Tests passed."


@pytest.mark.asyncio
async def test_codex_run_completion_redacts_stderr_tail_from_public_error(monkeypatch):
    """Transport details are logged internally but not returned to API clients."""
    from src.backends.codex.client import CodexAppServerError, CodexClient, CodexSessionClient

    backend = CodexClient()

    async def fail_ensure_rpc(_env):
        raise CodexAppServerError("Timed out waiting. stderr_tail=/repo/secret-token")

    monkeypatch.setattr(backend, "_ensure_rpc_locked", fail_ensure_rpc)
    monkeypatch.setattr(backend, "_close_rpc_locked", AsyncMock())

    chunks = [
        chunk
        async for chunk in backend.run_completion_with_client(
            CodexSessionClient(
                rpc=FakeRpc(),
                thread_id="thr_codex",
                model=None,
                cwd="/repo",
                env={},
            ),
            "hello",
            SimpleNamespace(session_id="gw-session"),
        )
    ]

    assert chunks == [
        {
            "type": "error",
            "is_error": True,
            "error_message": "Timed out waiting.",
        }
    ]


@pytest.mark.asyncio
async def test_codex_resume_approval_rejects_request_id_mismatch(monkeypatch):
    """Approval resume refuses corrupted request state instead of falling back silently."""
    fake_rpc = FakeRpc()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")
    client.pending_approval_request_id = "approval_other"
    client.pending_approval_method = "item/commandExecution/requestApproval"
    client.pending_approval_turn_id = "turn_1"
    client.pending_approval_params = {"turnId": "turn_1"}

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            client,
            "approval_1",
            "accept",
            session,
        )
    ]

    assert fake_rpc.respond_calls == []
    assert chunks == [
        {
            "type": "error",
            "is_error": True,
            "error_message": (
                "Codex approval request id mismatch: pending 'approval_other', "
                "received 'approval_1'"
            ),
        }
    ]


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

    assert chunks == [{"type": "error", "is_error": True, "error_message": "transport failed"}]
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
    """Unexpected direct approval requests use a deny-safe fallback."""
    from src.backends.codex.client import CodexJsonRpcClient

    rpc = CodexJsonRpcClient()

    assert rpc._handle_server_request({"method": "item/commandExecution/requestApproval"}) == {
        "decision": "cancel"
    }
    assert rpc._handle_server_request({"method": "item/fileChange/requestApproval"}) == {
        "decision": "cancel"
    }
    assert rpc._handle_server_request({"method": "item/permissions/requestApproval"}) == {
        "permissions": {},
        "scope": "turn",
    }


def test_codex_json_rpc_client_logs_unknown_server_request(caplog):
    """Unknown app-server request methods stay deny-neutral but visible in logs."""
    from src.backends.codex.client import CodexJsonRpcClient

    rpc = CodexJsonRpcClient()

    with caplog.at_level("WARNING", logger="src.backends.codex.client"):
        assert rpc._handle_server_request({"method": "item/newFeature/requestApproval"}) == {}

    assert "Unknown Codex server request method" in caplog.text
    assert "item/newFeature/requestApproval" in caplog.text


def test_codex_json_rpc_client_queues_approval_requests_while_waiting_for_response(
    monkeypatch,
):
    """Approval requests interleaved with regular responses are not cancelled."""
    from src.backends.codex.client import CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    writes = []
    messages = iter(
        [
            {
                "id": "approval_1",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thr", "turnId": "turn"},
            },
            {"id": "req_1", "result": {"ok": True}},
        ]
    )

    monkeypatch.setattr("src.backends.codex.client.uuid.uuid4", lambda: "req_1")
    monkeypatch.setattr(rpc, "_write_message", writes.append)
    monkeypatch.setattr(rpc, "_read_message", lambda: next(messages))

    assert rpc.request("turn/start", {"threadId": "thr"}) == {"ok": True}
    assert writes == [{"id": "req_1", "method": "turn/start", "params": {"threadId": "thr"}}]
    assert rpc.next_notification()["id"] == "approval_1"


@pytest.mark.asyncio
async def test_codex_session_disconnect_is_async(monkeypatch):
    """Session cleanup can await Codex handles without closing shared backend RPC."""
    fake_rpc = FakeRpc()

    from src.backends.codex.client import CodexSessionClient

    client = CodexSessionClient(rpc=fake_rpc, thread_id="thr", model=None, cwd=None)

    await asyncio.wait_for(client.disconnect(), timeout=1)

    assert fake_rpc.closed is False


@pytest.mark.asyncio
async def test_codex_function_call_output_uses_approval_resume_without_input_event(monkeypatch):
    """Codex approval continuations use the Codex resume hook, not Claude input_event."""
    from src.backends import ResolvedModel
    from src.response_models import ResponseCreateRequest
    from src.routes.responses import _handle_function_call_output
    from src.session_manager import Session

    session = Session(session_id="00000000-0000-0000-0000-000000000000", backend="codex")
    session.client = object()
    session.workspace = "/tmp/ws/test"
    session.turn_counter = 1
    session.pending_tool_call = {
        "call_id": "approval_1",
        "name": "AskUserQuestion",
        "arguments": {"question": "Approve?"},
        "backend": "codex",
        "codex_resume": "approval",
    }
    session.input_event = None

    body = ResponseCreateRequest(
        model="codex/gpt-5.5",
        input=[
            {
                "type": "function_call_output",
                "call_id": "approval_1",
                "output": "accept",
            }
        ],
        previous_response_id="resp_00000000-0000-0000-0000-000000000000_1",
        stream=False,
    )
    resolved = ResolvedModel("codex/gpt-5.5", "codex", "gpt-5.5")

    calls = []

    class FakeBackend:
        name = "codex"

        async def resume_approval_with_client(self, client, call_id, output, sess):
            calls.append((client, call_id, output, sess))
            yield {"type": "result", "subtype": "success", "result": "approved"}

        def parse_message(self, chunks):
            return "approved"

        def estimate_token_usage(self, prompt, completion, model=None):
            return {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}

    monkeypatch.setattr(
        "src.routes.responses.usage_logger.log_turn_from_context",
        AsyncMock(),
    )

    result = await _handle_function_call_output(
        body,
        resolved,
        FakeBackend(),
        session,
        session.session_id,
        "/tmp/ws/test",
        {"call_id": "approval_1", "output": "accept"},
    )

    assert result["status"] == "completed"
    assert result["output"][0]["content"][0]["text"] == "approved"
    assert session.turn_counter == 2
    assert session.pending_tool_call is None
    assert calls == [(session.client, "approval_1", "accept", session)]


# ---------------------------------------------------------------------------
# Group 1: src/backends/codex/__init__.py lazy imports and register failure
# ---------------------------------------------------------------------------


def test_codex_init_lazy_imports_codex_client():
    """Accessing CodexClient on the package triggers lazy import."""
    import src.backends.codex as codex_pkg
    from src.backends.codex.client import CodexClient

    assert codex_pkg.CodexClient is CodexClient


def test_codex_init_lazy_imports_codex_auth_provider():
    """Accessing CodexAuthProvider on the package triggers lazy import."""
    import src.backends.codex as codex_pkg
    from src.backends.codex.auth import CodexAuthProvider

    assert codex_pkg.CodexAuthProvider is CodexAuthProvider


def test_codex_init_unknown_attribute_raises_attribute_error():
    """Unknown package attributes raise AttributeError with helpful message."""
    import src.backends.codex as codex_pkg

    with pytest.raises(AttributeError, match="DoesNotExist"):
        codex_pkg.DoesNotExist  # noqa: B018


def test_codex_register_records_descriptor_and_live_client():
    """register() registers the descriptor and a CodexClient instance."""
    import src.backends.codex as codex_pkg

    descriptors = []
    registered = []

    class FakeRegistry:
        @classmethod
        def register_descriptor(cls, descriptor):
            descriptors.append(descriptor)

        @classmethod
        def register(cls, name, client):
            registered.append((name, client))

    codex_pkg.register(FakeRegistry)

    assert descriptors == [codex_pkg.CODEX_DESCRIPTOR]
    assert len(registered) == 1
    assert registered[0][0] == "codex"


def test_codex_register_logs_error_when_client_init_fails(monkeypatch, caplog):
    """If CodexClient() raises, register() still installs the descriptor and logs."""
    import src.backends.codex as codex_pkg

    class BoomClient:
        def __init__(self):
            raise RuntimeError("boom from CodexClient init")

    monkeypatch.setattr("src.backends.codex.client.CodexClient", BoomClient)

    descriptors = []
    registered = []

    class FakeRegistry:
        @classmethod
        def register_descriptor(cls, descriptor):
            descriptors.append(descriptor)

        @classmethod
        def register(cls, name, client):
            registered.append((name, client))

    with caplog.at_level("ERROR", logger="src.backends.codex"):
        codex_pkg.register(FakeRegistry)

    assert descriptors == [codex_pkg.CODEX_DESCRIPTOR]
    assert registered == []
    assert "Codex backend client creation failed" in caplog.text


# ---------------------------------------------------------------------------
# Group 2: pure helpers — approval decisions, kinds, options
# ---------------------------------------------------------------------------


def test_codex_normalize_approval_decision_aliases():
    """All alias strings map to canonical decisions."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    for value in ["yes", "y", "allow", "approve", "approved", "once"]:
        assert client._normalize_approval_decision(value) == "accept", value
    for value in ["no", "n", "deny", "denied", "reject", "rejected", ""]:
        assert client._normalize_approval_decision(value) == "decline", value
    for value in ["always", "session"]:
        assert client._normalize_approval_decision(value) == "acceptForSession", value
    assert client._normalize_approval_decision("stop") == "cancel"

    # Canonical values pass through unchanged.
    for value in ["accept", "acceptForSession", "decline", "cancel"]:
        assert client._normalize_approval_decision(value) == value, value

    # Unknown value falls through to decline.
    assert client._normalize_approval_decision("unknown_value") == "decline"


def test_codex_normalize_approval_decision_handles_list_and_none():
    """Non-string inputs go through string coercion / list head extraction."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._normalize_approval_decision(["yes", "no"]) == "accept"
    assert client._normalize_approval_decision([]) == "decline"
    assert client._normalize_approval_decision(None) == "decline"


def test_codex_approval_kind_falls_back_for_unknown_method():
    """Known methods map to known kinds; everything else is generic 'approval'."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._approval_kind("item/commandExecution/requestApproval") == "command"
    assert client._approval_kind("item/fileChange/requestApproval") == "file_change"
    assert client._approval_kind("item/permissions/requestApproval") == "permissions"
    assert client._approval_kind("item/newFeature/requestApproval") == "approval"


def test_codex_approval_question_covers_all_kinds():
    """Each approval kind produces a human-readable question."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._approval_question("command", {"command": "ls"}) == (
        "Codex requests approval to run command: ls"
    )
    assert client._approval_question("command", {}) == (
        "Codex requests approval to run a command."
    )
    assert client._approval_question("command", {"command": ""}) == (
        "Codex requests approval to run a command."
    )
    assert client._approval_question("file_change", {}) == (
        "Codex requests approval to apply file changes."
    )
    assert client._approval_question("permissions", {}) == "Codex requests additional permissions."
    assert client._approval_question("approval", {}) == "Codex requests approval."


def test_codex_approval_decision_label_handles_dict_decisions():
    """Dict-shaped decisions produce labels covering every supported branch."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    # Plain string passes through.
    assert client._approval_decision_label("accept") == "accept"

    # Empty / non-dict / non-string return "".
    assert client._approval_decision_label({}) == ""
    assert client._approval_decision_label(None) == ""
    assert client._approval_decision_label(123) == ""

    # acceptWithExecpolicyAmendment.
    assert (
        client._approval_decision_label({"acceptWithExecpolicyAmendment": {}})
        == "acceptWithExecpolicyAmendment"
    )

    # applyNetworkPolicyAmendment with full action+host returns enriched label.
    full = {
        "applyNetworkPolicyAmendment": {
            "network_policy_amendment": {"action": "allow", "host": "api.example.com"},
        }
    }
    assert (
        client._approval_decision_label(full)
        == "applyNetworkPolicyAmendment:allow:api.example.com"
    )

    # applyNetworkPolicyAmendment missing host falls back to bare name.
    partial = {"applyNetworkPolicyAmendment": {"network_policy_amendment": {"action": "allow"}}}
    assert client._approval_decision_label(partial) == "applyNetworkPolicyAmendment"

    # applyNetworkPolicyAmendment with non-dict body falls back to bare name.
    bare = {"applyNetworkPolicyAmendment": "raw"}
    assert client._approval_decision_label(bare) == "applyNetworkPolicyAmendment"

    # Other dict shapes return the first key.
    assert client._approval_decision_label({"customDecision": {}}) == "customDecision"


def test_codex_approval_decision_from_available_options_matches_dict_decision():
    """Dict decisions can be selected by their generated label."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    decisions = ["accept", {"acceptWithExecpolicyAmendment": {"foo": "bar"}}]

    matched = client._approval_decision_from_available_options(
        "acceptWithExecpolicyAmendment",
        {"availableDecisions": decisions},
    )
    assert matched == {"acceptWithExecpolicyAmendment": {"foo": "bar"}}


def test_codex_approval_decision_from_available_options_returns_none_when_no_match():
    """Non-matching label or missing/invalid availableDecisions returns None."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert (
        client._approval_decision_from_available_options(
            "nothing", {"availableDecisions": ["accept"]}
        )
        is None
    )
    assert client._approval_decision_from_available_options("accept", {}) is None
    assert (
        client._approval_decision_from_available_options(
            "accept", {"availableDecisions": "not-a-list"}
        )
        is None
    )


# ---------------------------------------------------------------------------
# Group 3: pure helpers — item parsing, token usage, final-response selection
# ---------------------------------------------------------------------------


def test_codex_tool_use_from_item_returns_none_for_invalid_inputs():
    """Non-dict / unknown type / missing or non-string id all skip tool_use conversion."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._tool_use_from_item(None) is None
    assert client._tool_use_from_item("string") is None
    assert client._tool_use_from_item({"type": "agentMessage", "id": "x"}) is None
    assert client._tool_use_from_item({"type": "commandExecution"}) is None
    assert client._tool_use_from_item({"type": "commandExecution", "id": 123}) is None
    assert client._tool_use_from_item({"type": "commandExecution", "id": ""}) is None


def test_codex_tool_use_from_item_strips_meta_fields():
    """Valid items are converted, dropping id / type / aggregatedOutput from input."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    item = {
        "type": "commandExecution",
        "id": "tool_1",
        "command": "ls",
        "aggregatedOutput": "should be dropped",
    }

    assert client._tool_use_from_item(item) == {
        "type": "tool_use",
        "id": "tool_1",
        "name": "commandExecution",
        "input": {"command": "ls"},
    }


def test_codex_tool_result_from_item_command_with_non_zero_exit_is_error():
    """commandExecution items with a non-zero exitCode flip is_error to True."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    item = {
        "type": "commandExecution",
        "id": "tool_1",
        "status": "completed",
        "exitCode": 1,
        "aggregatedOutput": "boom",
    }

    assert client._tool_result_from_item(item) == {
        "type": "tool_result",
        "tool_use_id": "tool_1",
        "content": "boom",
        "is_error": True,
    }


def test_codex_tool_result_from_item_declined_status_is_error():
    """Declined / failed status flags is_error and falls back to JSON dump when output is empty."""
    import json

    from src.backends.codex.client import CodexClient

    client = CodexClient()

    item = {
        "type": "commandExecution",
        "id": "tool_1",
        "status": "declined",
        "exitCode": 0,
        "aggregatedOutput": "",
        "command": "rm -rf /",
    }

    result = client._tool_result_from_item(item)

    assert result["is_error"] is True
    parsed = json.loads(result["content"])
    assert parsed == {"status": "declined", "exitCode": 0, "command": "rm -rf /"}


def test_codex_tool_result_from_item_non_command_uses_json_dump():
    """Non-command tool items dump remaining fields as JSON content."""
    import json

    from src.backends.codex.client import CodexClient

    client = CodexClient()

    item = {
        "type": "fileChange",
        "id": "tool_2",
        "status": "completed",
        "path": "/tmp/file.txt",
        "patch": "diff --git",
    }

    result = client._tool_result_from_item(item)

    assert result["tool_use_id"] == "tool_2"
    assert result["is_error"] is False
    assert json.loads(result["content"]) == {
        "status": "completed",
        "path": "/tmp/file.txt",
        "patch": "diff --git",
    }


def test_codex_tool_result_from_item_returns_none_for_invalid_inputs():
    """Mirror of tool_use_from_item: filters non-dict / unknown type / bad id."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._tool_result_from_item(None) is None
    assert client._tool_result_from_item({"type": "agentMessage", "id": "x"}) is None
    assert client._tool_result_from_item({"type": "commandExecution"}) is None
    assert client._tool_result_from_item({"type": "commandExecution", "id": 123}) is None


def test_codex_extract_usage_returns_none_for_invalid_inputs():
    """Non-dict tokenUsage and missing / non-dict 'last' return None."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._extract_usage(None) is None
    assert client._extract_usage("string") is None
    assert client._extract_usage({}) is None
    assert client._extract_usage({"last": "not-a-dict"}) is None


def test_codex_final_response_falls_back_to_unknown_phase():
    """When no item has phase=final_answer, fall back to the most recent unknown-phase agentMessage."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    items = [
        {"type": "agentMessage", "phase": None, "text": "thinking out loud"},
        {"type": "agentMessage", "phase": "intermediate", "text": "skipped"},
        {"type": "commandExecution", "phase": None, "text": "ignored"},
    ]

    assert client._final_response_from_items(items) == "thinking out loud"


def test_codex_final_response_returns_none_for_no_match():
    """Empty input or items lacking string text return None."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._final_response_from_items([]) is None
    assert client._final_response_from_items([{"type": "commandExecution"}]) is None
    assert (
        client._final_response_from_items(
            [{"type": "agentMessage", "phase": "final_answer", "text": None}]
        )
        is None
    )


def test_codex_turn_error_message_uses_default_when_missing():
    """Missing or message-less turn errors fall back to a default string."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._turn_error_message({}) == "Codex turn failed"
    assert client._turn_error_message({"error": None}) == "Codex turn failed"
    assert client._turn_error_message({"error": {}}) == "Codex turn failed"
    assert client._turn_error_message({"error": {"message": "oops"}}) == "oops"
