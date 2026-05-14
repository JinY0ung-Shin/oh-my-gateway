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
    # outputTokens=4 + reasoningOutputTokens=1; reasoning tokens are rolled into output.
    assert chunks[-2]["usage"] == {"input_tokens": 3, "output_tokens": 5}
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


def _command_approval_notifications(*, request_id: str = "approval_1", command: str = "ls"):
    return [
        {
            "id": request_id,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "cmd_1",
                "command": command,
                "availableDecisions": ["accept", "decline"],
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        },
    ]


def _file_change_approval_notifications(*, request_id: str = "approval_1"):
    return [
        {
            "id": request_id,
            "method": "item/fileChange/requestApproval",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "file_1",
                "grantRoot": "/repo",
                "availableDecisions": ["accept", "decline"],
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        },
    ]


def _collect_approval_tool_uses(chunks):
    return [
        block
        for chunk in chunks
        if chunk.get("type") == "assistant"
        for block in chunk.get("content", [])
        if block.get("type") == "tool_use" and block.get("name") == "codex_approval"
    ]


@pytest.mark.asyncio
async def test_codex_disallowed_tools_blocks_bash_command_approval(monkeypatch):
    """When ``Bash`` is in disallowed_tools, commandExecution approvals are auto-denied."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _command_approval_notifications(command="rm -rf /")
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["Bash"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []
    assert session.pending_tool_call is None


@pytest.mark.asyncio
async def test_codex_disallowed_tools_accepts_codex_native_command_name(monkeypatch):
    """``commandExecution`` (Codex-native name) in disallowed_tools auto-denies command approvals."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _command_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["commandExecution"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["Edit", "Write", "NotebookEdit", "fileChange"])
async def test_codex_disallowed_tools_blocks_file_change_approval(monkeypatch, alias):
    """Any of Edit/Write/NotebookEdit (or Codex-native fileChange) auto-denies fileChange approvals."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _file_change_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=[alias],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []
    assert session.pending_tool_call is None


@pytest.mark.asyncio
async def test_codex_disallowed_tools_does_not_affect_permissions_approval(monkeypatch):
    """Permissions approvals are not gated by tool name lists (they request scope, not tools)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "id": "approval_1",
            "method": "item/permissions/requestApproval",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "perm_1",
                "permissions": {"fileSystem": {"read": ["/repo"]}},
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        },
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["Bash", "Edit"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    # Permission approval bubbles to user — no auto-deny via respond_calls
    assert fake_rpc.respond_calls == []
    assert len(_collect_approval_tool_uses(chunks)) == 1
    assert session.pending_tool_call is not None


@pytest.mark.asyncio
async def test_codex_allowed_tools_whitelist_blocks_unlisted_command(monkeypatch):
    """allowed_tools whitelist mode auto-denies approvals not in the list."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _command_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    # allowed contains only Edit (fileChange) — command should be blocked.
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        allowed_tools=["Edit"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []


@pytest.mark.asyncio
async def test_codex_allowed_tools_whitelist_lets_listed_approval_bubble(monkeypatch):
    """allowed_tools whitelist mode still lets listed tools surface as user approvals."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _command_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        allowed_tools=["Bash"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == []
    assert len(_collect_approval_tool_uses(chunks)) == 1
    assert session.pending_tool_call is not None


@pytest.mark.asyncio
async def test_codex_permission_mode_bypass_overrides_thread_approval_policy(monkeypatch):
    """permission_mode='bypassPermissions' sets Codex approvalPolicy='never' at thread start."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "on-request")  # env says ask; permission_mode overrides

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="bypassPermissions",
    )

    assert fake_rpc.thread_start_calls
    assert fake_rpc.thread_start_calls[0]["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_codex_permission_mode_default_maps_to_on_request(monkeypatch):
    """permission_mode='default' maps to Codex approvalPolicy='on-request' even if env says 'never'."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "never")

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="default",
    )

    assert fake_rpc.thread_start_calls
    assert fake_rpc.thread_start_calls[0]["approvalPolicy"] == "on-request"


@pytest.mark.asyncio
async def test_codex_permission_mode_propagates_to_turn_params(monkeypatch):
    """permission_mode affects turn/start approvalPolicy too (per-turn override)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "on-request")

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="bypassPermissions",
    )
    [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert fake_rpc.turn_start_calls
    _, _, turn_params = fake_rpc.turn_start_calls[0]
    assert turn_params["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_codex_disallowed_tools_forces_approval_policy_off_never(monkeypatch):
    """Setting disallowed_tools must upgrade approvalPolicy=never to on-request so Codex emits approvals."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.delenv("CODEX_APPROVAL_POLICY", raising=False)  # defaults to "never"

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="bypassPermissions",
        disallowed_tools=["Bash"],
    )

    assert fake_rpc.thread_start_calls
    policy = fake_rpc.thread_start_calls[0]["approvalPolicy"]
    assert policy != "never", (
        f"approvalPolicy must not be 'never' when disallowed_tools is set "
        f"(otherwise Codex skips approval emission and enforcement is bypassed); got {policy!r}"
    )


@pytest.mark.asyncio
async def test_codex_allowed_tools_forces_approval_policy_off_never(monkeypatch):
    """allowed_tools whitelist also upgrades approvalPolicy off 'never' so enforcement runs."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "never")

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="bypassPermissions",
        allowed_tools=["Edit"],
    )

    assert fake_rpc.thread_start_calls
    policy = fake_rpc.thread_start_calls[0]["approvalPolicy"]
    assert policy != "never", (
        f"approvalPolicy must not be 'never' when allowed_tools is set; got {policy!r}"
    )


@pytest.mark.asyncio
async def test_codex_disallowed_tools_env_forces_approval_policy_off_never(monkeypatch):
    """Global DISALLOWED_TOOLS env also upgrades approvalPolicy so enforcement runs."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "never")
    monkeypatch.setenv("DISALLOWED_TOOLS", "Bash")

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="bypassPermissions",
    )

    assert fake_rpc.thread_start_calls
    policy = fake_rpc.thread_start_calls[0]["approvalPolicy"]
    assert policy != "never", f"got {policy!r}"


@pytest.mark.asyncio
async def test_codex_no_tool_policy_keeps_never_approval_policy(monkeypatch):
    """Without any tool policy, approvalPolicy still resolves to 'never' (no unsolicited upgrade)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "never")
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="bypassPermissions",
    )

    assert fake_rpc.thread_start_calls
    assert fake_rpc.thread_start_calls[0]["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_codex_accept_edits_auto_accepts_file_change_approval(monkeypatch):
    """permission_mode='acceptEdits' auto-accepts fileChange approvals (Claude parity)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _file_change_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="acceptEdits",
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "accept"})]
    assert _collect_approval_tool_uses(chunks) == []
    assert session.pending_tool_call is None


@pytest.mark.asyncio
async def test_codex_accept_edits_still_bubbles_command_approval(monkeypatch):
    """acceptEdits only auto-accepts fileChange; commandExecution still asks the user."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _command_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="acceptEdits",
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    # No auto-respond; the command approval bubbles to the user as a tool_use.
    assert fake_rpc.respond_calls == []
    approvals = _collect_approval_tool_uses(chunks)
    assert len(approvals) == 1
    assert session.pending_tool_call is not None


@pytest.mark.asyncio
async def test_codex_accept_edits_respects_disallowed_file_change(monkeypatch):
    """Disallowing fileChange takes precedence over acceptEdits (deny > accept)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _file_change_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="acceptEdits",
        disallowed_tools=["fileChange"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    # Disallowed wins; reject even though acceptEdits would normally accept.
    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []


@pytest.mark.asyncio
async def test_codex_accept_edits_does_not_accept_permissions_approval(monkeypatch):
    """acceptEdits is fileChange-specific; permissions approvals still bubble."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "id": "approval_1",
            "method": "item/permissions/requestApproval",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "perm_1",
                "permissions": {"fileSystem": {"read": ["/repo"]}},
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        },
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="acceptEdits",
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == []
    assert len(_collect_approval_tool_uses(chunks)) == 1


@pytest.mark.asyncio
async def test_codex_no_permission_mode_falls_back_to_env(monkeypatch):
    """When permission_mode is None, approvalPolicy still comes from CODEX_APPROVAL_POLICY env."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "on-request")

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(session=session, model="gpt-5.5")

    assert fake_rpc.thread_start_calls
    assert fake_rpc.thread_start_calls[0]["approvalPolicy"] == "on-request"


@pytest.mark.asyncio
async def test_codex_global_disallowed_tools_env_blocks_command(monkeypatch):
    """The DISALLOWED_TOOLS env var hard-blocks approvals even without per-request setting."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _command_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("DISALLOWED_TOOLS", "Bash")

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []


@pytest.mark.asyncio
async def test_codex_global_disallowed_tools_merges_with_per_request(monkeypatch):
    """env DISALLOWED_TOOLS and per-request disallowed_tools both apply (union semantics)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _file_change_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("DISALLOWED_TOOLS", "Bash")

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    # Env blocks Bash; per-request blocks Edit. The file change request should be auto-denied
    # because Edit is listed per-request.
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["Edit"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []


def _mcp_tool_call_approval_notifications(*, request_id: str = "approval_1"):
    return [
        {
            "id": request_id,
            "method": "item/mcpToolCall/requestApproval",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "mcp_1",
                "serverLabel": "fs",
                "toolName": "read_file",
                "availableDecisions": ["accept", "decline"],
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        },
    ]


def _dynamic_tool_call_approval_notifications(*, request_id: str = "approval_1"):
    return [
        {
            "id": request_id,
            "method": "item/dynamicToolCall/requestApproval",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "dyn_1",
                "toolName": "search",
                "availableDecisions": ["accept", "decline"],
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        },
    ]


@pytest.mark.asyncio
async def test_codex_disallowed_tools_blocks_mcp_tool_call_approval(monkeypatch):
    """disallowed_tools containing 'mcpToolCall' auto-denies MCP tool approval requests."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _mcp_tool_call_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["mcpToolCall"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []
    assert session.pending_tool_call is None


@pytest.mark.asyncio
async def test_codex_disallowed_tools_blocks_dynamic_tool_call_approval(monkeypatch):
    """disallowed_tools containing 'dynamicToolCall' auto-denies dynamic tool approvals."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _dynamic_tool_call_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["dynamicToolCall"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []


@pytest.mark.asyncio
async def test_codex_allowed_tools_whitelist_blocks_mcp_when_not_listed(monkeypatch):
    """allowed_tools whitelist auto-denies MCP approvals that aren't listed."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _mcp_tool_call_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        allowed_tools=["Bash"],
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []


@pytest.mark.asyncio
async def test_codex_empty_allowed_tools_blocks_all_command_approvals(monkeypatch):
    """allowed_tools=[] is an explicit block-all whitelist (distinct from None / unset)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = _command_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        allowed_tools=[],  # explicit empty list = block everything
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "decline"})]
    assert _collect_approval_tool_uses(chunks) == []


@pytest.mark.asyncio
async def test_codex_empty_allowed_tools_forces_approval_policy_off_never(monkeypatch):
    """allowed_tools=[] is a real policy and upgrades approvalPolicy off 'never'."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "never")

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="bypassPermissions",
        allowed_tools=[],
    )

    assert fake_rpc.thread_start_calls
    assert fake_rpc.thread_start_calls[0]["approvalPolicy"] != "never"


@pytest.mark.asyncio
async def test_codex_permission_mode_update_takes_effect_on_next_turn(monkeypatch):
    """Updating permission_mode mid-session changes the next turn's approval semantics."""
    fake_rpc = FakeRpc()
    # First call: no notifications needed (we just construct the client).
    # Second call: fileChange approval that should be auto-accepted after switch.
    fake_rpc.notifications = _file_change_approval_notifications()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="default",  # initially: bubble approvals
    )

    backend.update_request_policy(client, permission_mode="acceptEdits")
    assert client.permission_mode == "acceptEdits"

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == [("approval_1", {"decision": "accept"})]
    assert _collect_approval_tool_uses(chunks) == []


@pytest.mark.asyncio
async def test_codex_update_request_policy_replaces_permission_mode(monkeypatch):
    """update_request_policy refreshes permission_mode so continuation requests can change it."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="default",
    )
    assert client.permission_mode == "default"

    backend.update_request_policy(client, permission_mode="acceptEdits")
    assert client.permission_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_codex_update_request_policy_preserves_permission_mode_when_omitted(monkeypatch):
    """Omitting permission_mode in update_request_policy keeps the existing value."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="acceptEdits",
    )

    backend.update_request_policy(client, disallowed_tools=["Bash"])
    assert client.permission_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_codex_unknown_permission_mode_uses_safe_on_request(monkeypatch, caplog):
    """Unknown permission_mode strings fall back to a safe ``on-request``, not env=never.

    Falling back to env=never would let a typo silently disable approval-time
    enforcement. Use a safe explicit default instead, and warn so operators see
    the malformed input.
    """
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)
    monkeypatch.setenv("CODEX_APPROVAL_POLICY", "never")  # operator default

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    with caplog.at_level("WARNING", logger="src.backends.codex.client"):
        await backend.create_client(
            session=session,
            model="gpt-5.5",
            permission_mode="definitely-invalid-mode",
        )

    assert fake_rpc.thread_start_calls
    assert fake_rpc.thread_start_calls[0]["approvalPolicy"] == "on-request"
    assert "unknown permission_mode" in caplog.text.lower()
    assert "definitely-invalid-mode" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_method", "notifications_fn"),
    [
        ("item/mcpToolCall/requestApproval", _mcp_tool_call_approval_notifications),
        ("item/dynamicToolCall/requestApproval", _dynamic_tool_call_approval_notifications),
    ],
)
async def test_codex_accept_edits_does_not_accept_non_file_tool_calls(
    monkeypatch, expected_method, notifications_fn
):
    """acceptEdits is fileChange-only; mcpToolCall and dynamicToolCall still bubble."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = notifications_fn()
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        permission_mode="acceptEdits",
    )

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]

    assert fake_rpc.respond_calls == []
    approvals = _collect_approval_tool_uses(chunks)
    assert len(approvals) == 1
    # Anchor the regression: the bubbled tool_use must come from the expected method.
    assert approvals[0]["metadata"]["codex_approval_method"] == expected_method


@pytest.mark.asyncio
async def test_codex_update_request_policy_replaces_session_tool_lists(monkeypatch):
    """update_request_policy() lets continuation requests refresh per-request tool policy."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["Bash"],
    )
    assert client.disallowed_tools == ["Bash"]
    assert client.allowed_tools is None

    backend.update_request_policy(
        client,
        allowed_tools=["Edit"],
        disallowed_tools=["Write"],
    )
    assert client.allowed_tools == ["Edit"]
    assert client.disallowed_tools == ["Write"]


@pytest.mark.asyncio
async def test_codex_update_request_policy_clears_with_none(monkeypatch):
    """Passing None explicitly clears a previously-set policy field."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["Bash"],
        allowed_tools=["Edit"],
    )

    backend.update_request_policy(client, allowed_tools=None, disallowed_tools=None)
    assert client.allowed_tools is None
    assert client.disallowed_tools is None


@pytest.mark.asyncio
async def test_codex_disallowed_tools_applies_on_approval_resume(monkeypatch):
    """Auto-deny policy applies to approvals surfaced during approval-resume turns."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thr_codex", "requestId": "approval_0"},
        },
        {
            "id": "approval_1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "itemId": "cmd_2",
                "command": "rm -rf /",
                "availableDecisions": ["accept", "decline"],
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        },
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        disallowed_tools=["Bash"],
    )
    client.pending_approval_request_id = "approval_0"
    client.pending_approval_method = "item/commandExecution/requestApproval"
    client.pending_approval_turn_id = "turn_1"
    client.pending_approval_params = {"turnId": "turn_1"}

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            client, "approval_0", "accept", session
        )
    ]

    # First respond is the user's "accept" for approval_0; second is the auto-deny for approval_1.
    assert fake_rpc.respond_calls == [
        ("approval_0", {"decision": "accept"}),
        ("approval_1", {"decision": "decline"}),
    ]
    assert _collect_approval_tool_uses(chunks) == []
    assert session.pending_tool_call is None


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
    """If turn/start fails on every attempt, the shared RPC is left closed so the next request restarts it.

    With the conservative in-request retry path, a persistent transport failure
    burns through both attempts and surfaces an error chunk; the dead shared
    RPC must still be cleared so a *subsequent* request brings up a fresh one.
    """

    class FailingTurnRpc(FakeRpc):
        def turn_start(self, thread_id, input_items, params):
            self.turn_start_calls.append((thread_id, input_items, params))
            raise RuntimeError("transport failed")

    created = []

    def fake_factory(**kwargs):
        rpc = FailingTurnRpc()
        created.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", fake_factory)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session")
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    # The user sees a final error chunk after retry exhausts.
    assert chunks[-1] == {
        "type": "error",
        "is_error": True,
        "error_message": "transport failed",
    }
    # Each failing RPC instance was closed.
    for rpc in created:
        assert rpc.closed is True

    # A subsequent create_client constructs a fresh RPC (the dead shared one was cleared).
    fresh_count_before = len(created)
    await backend.create_client(session=SimpleNamespace(session_id="gw-session-2"), model="gpt-5.5")
    assert len(created) == fresh_count_before + 1


@pytest.mark.asyncio
async def test_codex_client_retries_turn_start_once_after_rpc_transport_error_before_turn_is_accepted(
    monkeypatch,
):
    """A transport error on turn/start (before any output is yielded) triggers exactly one retry.

    Scenario:
      1. First RPC accepts thread_start during create_client.
      2. turn_start raises a transport error.
      3. The first RPC is closed; a fresh RPC is started.
      4. The new RPC restores the same thread via thread_resume, then turn_start
         succeeds and the turn completes normally — the user sees a successful
         result, not an error chunk.
    """
    from src.backends.codex.client import CodexAppServerError, CodexClient

    rpcs = []

    class FailingTurnStartRpc(FakeRpc):
        def turn_start(self, thread_id, input_items, params):
            self.turn_start_calls.append((thread_id, input_items, params))
            raise CodexAppServerError("transport failed during turn/start")

    def factory(**kwargs):
        if not rpcs:
            rpc = FailingTurnStartRpc()
        else:
            rpc = FakeRpc()
            rpc.notifications = [
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thr_codex",
                        "turnId": "turn_1",
                        "turn": {"id": "turn_1", "status": "completed", "items": []},
                    },
                },
            ]
        rpcs.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", factory)

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    # Two RPCs were created total (initial + retry).
    assert len(rpcs) == 2
    # First RPC was closed when its turn_start failed.
    assert rpcs[0].closed is True
    # Second RPC re-established the same thread before retrying turn_start.
    assert rpcs[1].thread_resume_calls
    assert rpcs[1].thread_resume_calls[0][0] == "thr_codex"
    assert len(rpcs[1].turn_start_calls) == 1
    # The user-visible output is a normal result, not an error chunk.
    assert not any(c.get("is_error") for c in chunks)
    assert chunks[-1]["type"] == "result"


@pytest.mark.asyncio
async def test_codex_client_does_not_retry_after_partial_output(monkeypatch):
    """Once any chunk has been yielded for a turn, errors no longer trigger a retry.

    The first RPC accepts turn_start and emits one delta; the next read raises
    a transport error. Because output was already sent to the user, the gateway
    must surface an error chunk rather than silently re-running the turn (which
    would risk duplicate side effects on the app-server side).
    """
    from src.backends.codex.client import CodexAppServerError, CodexClient

    rpcs = []
    consume_count = [0]

    class FailingMidStreamRpc(FakeRpc):
        def next_notification(self):
            consume_count[0] += 1
            if consume_count[0] == 1:
                return {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thr_codex",
                        "turnId": "turn_1",
                        "itemId": "item_1",
                        "delta": "partial",
                    },
                }
            raise CodexAppServerError("transport failed mid-stream")

    def factory(**kwargs):
        if not rpcs:
            rpc = FailingMidStreamRpc()
        else:
            # If we did retry, the second RPC would be used here.
            rpc = FakeRpc()
        rpcs.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", factory)

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    # No retry: only one RPC was ever created.
    assert len(rpcs) == 1
    # The partial delta was delivered, then an error chunk closes the turn.
    assert any(c.get("type") == "stream_event" for c in chunks)
    assert chunks[-1] == {
        "type": "error",
        "is_error": True,
        "error_message": "transport failed mid-stream",
    }


@pytest.mark.asyncio
async def test_codex_client_does_not_retry_when_turn_start_queued_notifications_before_failing(
    monkeypatch,
):
    """If the app-server queued a notification for this turn before the transport
    error, treat the turn as possibly accepted and skip retry to avoid duplicate
    side effects on the app-server side.
    """
    from src.backends.codex.client import CodexAppServerError, CodexClient

    rpcs = []

    class QueuesThenFailsRpc(FakeRpc):
        def __init__(self):
            super().__init__()
            self._pending_notifications: list = []

        def turn_start(self, thread_id, input_items, params):
            self.turn_start_calls.append((thread_id, input_items, params))
            # Simulate the JSON-RPC client having buffered an inbound notification
            # for this turn before the transport went down.
            self._pending_notifications.append(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thr_codex",
                        "turnId": "turn_1",
                        "item": {"type": "commandExecution", "id": "cmd_1"},
                    },
                }
            )
            raise CodexAppServerError("transport failed after queuing notification")

    def factory(**kwargs):
        rpc = QueuesThenFailsRpc()
        rpcs.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", factory)

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    # Only the original RPC was used — no retry, because queuing implies the
    # app-server may have already started executing the turn.
    assert len(rpcs) == 1
    assert chunks[-1]["type"] == "error"
    assert chunks[-1]["is_error"] is True


@pytest.mark.asyncio
async def test_codex_client_thread_resume_failure_on_retry_surfaces_error(monkeypatch):
    """thread_resume failing on the retry attempt is treated as the retry failure."""
    from src.backends.codex.client import CodexAppServerError, CodexClient

    rpcs = []

    class FailingRpc(FakeRpc):
        def __init__(self, fail_turn_start=False, fail_resume=False):
            super().__init__()
            self._fail_turn_start = fail_turn_start
            self._fail_resume = fail_resume

        def turn_start(self, thread_id, input_items, params):
            self.turn_start_calls.append((thread_id, input_items, params))
            if self._fail_turn_start:
                raise CodexAppServerError("turn/start transport failed")
            return {"turn": {"id": "turn_1", "status": "inProgress"}}

        def thread_resume(self, thread_id, params):
            self.thread_resume_calls.append((thread_id, params))
            if self._fail_resume:
                raise CodexAppServerError("thread/resume transport failed")
            return {"thread": {"id": thread_id}}

    def factory(**kwargs):
        if not rpcs:
            # initial RPC: thread_start fine (FakeRpc default), turn/start blows up.
            rpc = FailingRpc(fail_turn_start=True)
        else:
            # retry RPC: thread/resume also blows up before we even reach turn/start.
            rpc = FailingRpc(fail_resume=True)
        rpcs.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", factory)

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert len(rpcs) == 2
    assert rpcs[1].thread_resume_calls  # we tried to recover, but resume blew up
    assert chunks[-1]["type"] == "error"
    assert "thread/resume transport failed" in chunks[-1]["error_message"]


@pytest.mark.asyncio
async def test_codex_client_session_rpc_field_is_refreshed_after_successful_retry(monkeypatch):
    """``CodexSessionClient.rpc`` points at the live RPC after retry recovery."""
    from src.backends.codex.client import CodexAppServerError, CodexClient

    rpcs = []

    class FailingFirstTurnRpc(FakeRpc):
        def turn_start(self, thread_id, input_items, params):
            self.turn_start_calls.append((thread_id, input_items, params))
            raise CodexAppServerError("first attempt failed")

    def factory(**kwargs):
        if not rpcs:
            rpc = FailingFirstTurnRpc()
        else:
            rpc = FakeRpc()
            rpc.notifications = [
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thr_codex",
                        "turnId": "turn_1",
                        "turn": {"id": "turn_1", "status": "completed", "items": []},
                    },
                }
            ]
        rpcs.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", factory)

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")
    assert client.rpc is rpcs[0]

    [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    # After successful retry, the session client should track the live RPC.
    assert client.rpc is rpcs[1]


@pytest.mark.asyncio
async def test_codex_resume_approval_fails_fast_through_full_approval_flow(monkeypatch):
    """End-to-end variant: the pending RPC field is populated by the real
    ``_store_pending_approval`` path (not test-set), then a transport reset
    swaps the shared RPC. resume_approval must surface a transport-lost error.
    """
    from src.backends.codex.client import CodexClient

    rpcs = []

    def factory(**kwargs):
        rpc = FakeRpc()
        rpcs.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", factory)

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    # Run a turn whose only notification is a command approval; this exercises
    # the real _store_pending_approval path (including pending_approval_rpc).
    rpcs[0].notifications = _command_approval_notifications()
    [chunk async for chunk in backend.run_completion_with_client(client, "test", session)]
    assert client.pending_approval_rpc is rpcs[0]
    assert session.pending_tool_call is not None

    # Simulate a transport reset between approval and user response.
    await backend._close_rpc_locked()

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            client, "approval_1", "accept", session
        )
    ]

    assert chunks
    assert chunks[-1]["type"] == "error"
    assert chunks[-1]["is_error"] is True
    assert "transport" in chunks[-1]["error_message"].lower()
    # Pending state is cleared so subsequent operations don't see stale data.
    assert client.pending_approval_rpc is None
    assert client.pending_approval_request_id is None


@pytest.mark.asyncio
async def test_codex_resume_approval_fails_fast_when_pending_rpc_is_gone(monkeypatch):
    """If the RPC that received the approval request is gone, fail fast with a clear error.

    A transport-level reset between an approval being surfaced and the user's
    response means the new RPC has no record of the pending approval; trying
    to ``rpc.respond`` would either silently drop the message or hit a server
    that has no idea what we're talking about.
    """
    from src.backends.codex.client import CodexClient

    fake_rpc_a = FakeRpc()
    fake_rpc_b = FakeRpc()
    rpcs = [fake_rpc_a, fake_rpc_b]

    def factory(**kwargs):
        # Pop in order: A for create_client, B for the post-failure recovery.
        if rpcs:
            return rpcs.pop(0)
        return FakeRpc()

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", factory)

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")
    # Pretend an approval was surfaced on the original RPC.
    client.pending_approval_request_id = "approval_1"
    client.pending_approval_method = "item/commandExecution/requestApproval"
    client.pending_approval_turn_id = "turn_1"
    client.pending_approval_params = {"turnId": "turn_1"}
    client.pending_approval_rpc = fake_rpc_a

    # Simulate transport reset between approval and user response: shared RPC
    # was closed and replaced by ``fake_rpc_b``.
    await backend._close_rpc_locked()

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            client, "approval_1", "accept", session
        )
    ]

    assert chunks
    assert chunks[-1]["type"] == "error"
    assert chunks[-1]["is_error"] is True
    assert "transport" in chunks[-1]["error_message"].lower() or "lost" in chunks[-1]["error_message"].lower()


@pytest.mark.asyncio
async def test_codex_client_does_not_retry_when_retry_also_fails(monkeypatch):
    """If the retry also raises, the gateway gives up and surfaces an error."""
    from src.backends.codex.client import CodexAppServerError, CodexClient

    rpcs = []

    class FailingTurnStartRpc(FakeRpc):
        def turn_start(self, thread_id, input_items, params):
            self.turn_start_calls.append((thread_id, input_items, params))
            raise CodexAppServerError("transport failed again")

    def factory(**kwargs):
        rpc = FailingTurnStartRpc()
        rpcs.append(rpc)
        return rpc

    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", factory)

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    # Initial RPC + one retry attempt = 2 RPCs created.
    assert len(rpcs) == 2
    assert chunks[-1]["type"] == "error"
    assert chunks[-1]["is_error"] is True


@pytest.mark.asyncio
async def test_codex_client_forwards_response_model_params_to_turn_start(monkeypatch):
    """temperature and max_output_tokens flow into the Codex turn/start payload."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        model_params={"temperature": 0.3, "max_output_tokens": 1024},
    )

    [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert fake_rpc.turn_start_calls
    _, _, turn_params = fake_rpc.turn_start_calls[0]
    assert turn_params.get("temperature") == 0.3
    assert turn_params.get("maxOutputTokens") == 1024


@pytest.mark.asyncio
async def test_codex_client_omits_model_params_when_unset(monkeypatch):
    """When no model_params are provided, turn/start payload stays minimal (no defaults injected)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")
    [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    _, _, turn_params = fake_rpc.turn_start_calls[0]
    assert "temperature" not in turn_params
    assert "maxOutputTokens" not in turn_params


@pytest.mark.asyncio
async def test_codex_client_translates_legacy_max_tokens_alias(monkeypatch):
    """The OpenAI 'max_tokens' alias maps to Codex maxOutputTokens for compatibility."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        model_params={"max_tokens": 512},
    )
    [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    _, _, turn_params = fake_rpc.turn_start_calls[0]
    assert turn_params.get("maxOutputTokens") == 512


@pytest.mark.asyncio
async def test_codex_client_drops_none_model_param_values(monkeypatch):
    """None values in model_params are skipped (so Responses bodies with unset fields are safe)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        model_params={"temperature": None, "max_output_tokens": 100},
    )
    [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    _, _, turn_params = fake_rpc.turn_start_calls[0]
    assert "temperature" not in turn_params
    assert turn_params.get("maxOutputTokens") == 100


@pytest.mark.asyncio
async def test_codex_create_client_forwards_mcp_servers_to_thread_params(monkeypatch):
    """mcp_servers passed to create_client lands in the Codex thread/start payload."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    mcp = {
        "fs": {"type": "stdio", "command": "fs-server", "args": []},
        "web": {"type": "http", "url": "http://localhost:9000"},
    }
    await backend.create_client(
        session=session,
        model="gpt-5.5",
        mcp_servers=mcp,
    )

    assert fake_rpc.thread_start_calls
    sent_params = fake_rpc.thread_start_calls[0]
    assert sent_params.get("mcpServers") == mcp


@pytest.mark.asyncio
async def test_codex_create_client_omits_mcp_servers_when_unset(monkeypatch):
    """No mcp_servers -> no mcpServers key in the payload (backend defaults stand)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(session=session, model="gpt-5.5")

    assert fake_rpc.thread_start_calls
    assert "mcpServers" not in fake_rpc.thread_start_calls[0]


@pytest.mark.asyncio
async def test_codex_create_client_omits_mcp_servers_when_empty_dict(monkeypatch):
    """An empty dict is treated as "no servers" and the key is omitted."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    await backend.create_client(session=session, model="gpt-5.5", mcp_servers={})

    assert fake_rpc.thread_start_calls
    assert "mcpServers" not in fake_rpc.thread_start_calls[0]


@pytest.mark.asyncio
async def test_codex_thread_resume_includes_mcp_servers(monkeypatch):
    """Resuming a thread also re-asserts the mcp_servers config (so reconnects don't lose it)."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(
        session_id="gw-session",
        pending_tool_call=None,
        codex_thread_id="thr_existing",
    )
    mcp = {"fs": {"type": "stdio", "command": "fs-server"}}
    await backend.create_client(session=session, model="gpt-5.5", mcp_servers=mcp)

    assert fake_rpc.thread_resume_calls
    _, resume_params = fake_rpc.thread_resume_calls[0]
    assert resume_params.get("mcpServers") == mcp


@pytest.mark.asyncio
async def test_codex_turn_preserves_multimodal_items(monkeypatch):
    """Codex turn/start carries a multi-item payload (text + image) verbatim."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    items = [
        {"type": "text", "text": "look at this"},
        {"type": "image", "url": "https://example.com/foo.png"},
        {"type": "text", "text": "any thoughts?"},
    ]

    [chunk async for chunk in backend.run_completion_with_client(client, items, session)]

    assert fake_rpc.turn_start_calls
    _, sent_items, _ = fake_rpc.turn_start_calls[0]
    assert sent_items == items


@pytest.mark.asyncio
async def test_codex_turn_wraps_string_prompt_into_text_item(monkeypatch):
    """Plain-string prompts stay compatible: they're wrapped into a single text item."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = [
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thr_codex",
                "turnId": "turn_1",
                "turn": {"id": "turn_1", "status": "completed", "items": []},
            },
        }
    ]
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    [chunk async for chunk in backend.run_completion_with_client(client, "hello there", session)]

    _, sent_items, _ = fake_rpc.turn_start_calls[0]
    assert sent_items == [{"type": "text", "text": "hello there"}]


@pytest.mark.asyncio
async def test_codex_turn_rejects_empty_input_list(monkeypatch):
    """Empty input list is rejected — Codex won't accept a turn with no content."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, [], session)]
    assert fake_rpc.turn_start_calls == []
    assert chunks[-1]["type"] == "error"
    assert chunks[-1]["is_error"] is True


@pytest.mark.asyncio
async def test_codex_turn_rejects_invalid_input_item_shape(monkeypatch):
    """Non-text/non-dict items in the input list surface a clear error."""
    fake_rpc = FakeRpc()
    fake_rpc.notifications = []
    monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc)

    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")

    bad_items = [
        {"type": "text", "text": "ok"},
        "this should be a dict, not a string",
    ]
    chunks = [
        chunk async for chunk in backend.run_completion_with_client(client, bad_items, session)
    ]
    # No turn/start was reached for an invalid payload.
    assert fake_rpc.turn_start_calls == []
    assert chunks
    assert chunks[-1]["type"] == "error"
    assert chunks[-1]["is_error"] is True


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


@pytest.mark.asyncio
async def test_codex_function_call_output_refreshes_session_tool_policy(monkeypatch):
    """A function_call_output body with new disallowed_tools updates the session client policy."""
    from src.backends import ResolvedModel
    from src.response_models import ResponseCreateRequest
    from src.routes.responses import _handle_function_call_output
    from src.session_manager import Session

    session = Session(session_id="00000000-0000-0000-0000-000000000001", backend="codex")
    session.client = SimpleNamespace(allowed_tools=None, disallowed_tools=None)
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
        previous_response_id="resp_00000000-0000-0000-0000-000000000001_1",
        stream=False,
        disallowed_tools=["Bash"],
    )
    resolved = ResolvedModel("codex/gpt-5.5", "codex", "gpt-5.5")

    update_calls = []

    class FakeBackend:
        name = "codex"

        def update_request_policy(
            self,
            client,
            *,
            allowed_tools=None,
            disallowed_tools=None,
            permission_mode=None,
            model_params=None,
        ):
            update_calls.append(
                (client, allowed_tools, disallowed_tools, permission_mode, model_params)
            )
            client.allowed_tools = list(allowed_tools) if allowed_tools is not None else None
            client.disallowed_tools = (
                list(disallowed_tools) if disallowed_tools is not None else None
            )
            client.model_params = dict(model_params) if model_params else None
            if permission_mode is not None:
                client.permission_mode = permission_mode

        async def resume_approval_with_client(self, client, call_id, output, sess):
            yield {"type": "result", "subtype": "success", "result": "approved"}

        def parse_message(self, chunks):
            return "approved"

        def estimate_token_usage(self, prompt, completion, model=None):
            return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(
        "src.routes.responses.usage_logger.log_turn_from_context",
        AsyncMock(),
    )

    await _handle_function_call_output(
        body,
        resolved,
        FakeBackend(),
        session,
        session.session_id,
        "/tmp/ws/test",
        {"call_id": "approval_1", "output": "accept"},
    )

    assert update_calls == [(session.client, None, ["Bash"], None, None)]
    assert session.client.disallowed_tools == ["Bash"]


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


def test_codex_extract_usage_includes_reasoning_output_tokens():
    """Reasoning output tokens are part of the model's output and must be reported."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    usage = backend._extract_usage(
        {
            "last": {
                "inputTokens": 10,
                "cachedInputTokens": 2,
                "outputTokens": 5,
                "reasoningOutputTokens": 7,
            }
        }
    )
    assert usage == {"input_tokens": 12, "output_tokens": 12}


def test_codex_extract_usage_handles_missing_reasoning_tokens():
    """Notifications without reasoningOutputTokens fall back to outputTokens only."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    usage = backend._extract_usage(
        {"last": {"inputTokens": 3, "outputTokens": 4}}
    )
    assert usage == {"input_tokens": 3, "output_tokens": 4}


def test_codex_extract_usage_total_matches_total_tokens():
    """Reported (input + output) equals notification totalTokens, so totals reconcile."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    last = {
        "inputTokens": 6,
        "cachedInputTokens": 4,
        "outputTokens": 8,
        "reasoningOutputTokens": 3,
        "totalTokens": 21,
    }
    usage = backend._extract_usage({"last": last})
    assert usage["input_tokens"] + usage["output_tokens"] == last["totalTokens"]


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


# ---------------------------------------------------------------------------
# Group 4: pure helpers — small utilities (params, message parsing, env, errors)
# ---------------------------------------------------------------------------


def test_codex_public_error_message_strips_stderr_tail_for_app_server_error():
    """CodexAppServerError messages drop the verbose stderr_tail suffix."""
    from src.backends.codex.client import CodexAppServerError, CodexClient

    client = CodexClient()

    exc = CodexAppServerError("Timed out. stderr_tail=verbose internal logs")
    assert client._public_error_message(exc) == "Timed out."

    # Empty message returns generic fallback.
    assert client._public_error_message(CodexAppServerError("")) == "Codex app-server error"


def test_codex_public_error_message_passes_through_other_exceptions():
    """Non CodexAppServerError exceptions retain their str() form."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._public_error_message(ValueError("bad input")) == "bad input"


def test_codex_combine_system_prompt_combinations():
    """All four combinations of (custom_base, system_prompt) produce the right output."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._combine_system_prompt(None, None) is None
    assert client._combine_system_prompt("base", None) == "base"
    assert client._combine_system_prompt(None, "user") == "user"
    assert client._combine_system_prompt("base", "user") == "base\n\nuser"


def test_codex_thread_params_includes_only_set_fields():
    """Optional thread params are omitted when their inputs are None / empty."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    bare = client._thread_params(model=None, cwd=None, system_prompt=None)
    assert "model" not in bare
    assert "cwd" not in bare
    assert "developerInstructions" not in bare
    assert "approvalPolicy" in bare
    assert "sandbox" in bare

    full = client._thread_params(model="gpt-5", cwd="/tmp", system_prompt="hello")
    assert full["model"] == "gpt-5"
    assert full["cwd"] == "/tmp"
    assert full["developerInstructions"] == "hello"


def test_codex_turn_params_uses_session_client_fields():
    """Turn params reflect the session client's model/cwd, including 'unset' case."""
    from src.backends.codex.client import CodexClient, CodexJsonRpcClient, CodexSessionClient

    client = CodexClient()
    rpc = CodexJsonRpcClient()
    session_client = CodexSessionClient(rpc=rpc, thread_id="t", model=None, cwd=None)

    bare = client._turn_params(session_client)
    assert "model" not in bare
    assert "cwd" not in bare
    assert "approvalPolicy" in bare

    session_client.model = "gpt-5"
    session_client.cwd = "/tmp"
    full = client._turn_params(session_client)
    assert full["model"] == "gpt-5"
    assert full["cwd"] == "/tmp"


def test_codex_metadata_env_filters_by_allowlist(monkeypatch):
    """Only allowlisted metadata keys are forwarded as env vars; None becomes {}."""
    from src import constants as constants_module
    from src.backends.codex.client import CodexClient

    monkeypatch.setattr(
        constants_module, "METADATA_ENV_ALLOWLIST", frozenset({"ALLOWED_KEY"})
    )

    client = CodexClient()

    assert client._metadata_env(None) == {}
    assert client._metadata_env({}) == {}
    assert client._metadata_env({"ALLOWED_KEY": "value", "BLOCKED_KEY": "no"}) == {
        "ALLOWED_KEY": "value"
    }


def test_codex_parse_message_prefers_success_result():
    """The newest success/result string wins over assistant content blocks."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    messages = [
        {"type": "assistant", "content": [{"type": "text", "text": "fallback"}]},
        {"subtype": "success", "result": "winning result"},
    ]

    assert client.parse_message(messages) == "winning result"


def test_codex_parse_message_falls_back_to_assistant_content():
    """Without a success/result, parse_message stitches assistant text blocks."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    messages = [
        {"type": "assistant", "content": [{"type": "text", "text": "first"}]},
        {"type": "assistant", "content": [{"type": "text", "text": "second"}]},
    ]

    result = client.parse_message(messages)

    assert result is not None
    assert "first" in result
    assert "second" in result


def test_codex_parse_message_returns_none_for_empty_inputs():
    """Empty list and whitespace-only success result both yield None."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client.parse_message([]) is None
    assert client.parse_message([{"subtype": "success", "result": "   "}]) is None


def test_codex_estimate_token_usage_uses_length_heuristic():
    """Token estimate is ceil(len/4) with a floor of 1 each."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    usage = client.estimate_token_usage("a" * 40, "b" * 80)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    floor = client.estimate_token_usage("", "")
    assert floor["prompt_tokens"] >= 1
    assert floor["completion_tokens"] >= 1


# ---------------------------------------------------------------------------
# Group 5: CodexJsonRpcClient I/O error branches
# ---------------------------------------------------------------------------


def _make_rpc_with_queued_line(line: str):
    """Construct an RPC instance whose stdout queue has one preloaded line."""
    import queue as queue_module

    from src.backends.codex.client import CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    rpc._proc = SimpleNamespace(stdout=SimpleNamespace())
    rpc._stdout_queue = queue_module.Queue()
    rpc._stdout_queue.put(line)
    return rpc


def test_codex_rpc_read_message_raises_on_invalid_json():
    """Garbage on the wire becomes a CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError

    rpc = _make_rpc_with_queued_line("not valid json\n")

    with pytest.raises(CodexAppServerError, match="Invalid Codex JSON-RPC line"):
        rpc._read_message()


def test_codex_rpc_read_message_raises_on_non_dict_payload():
    """A JSON array (or other non-object) is also rejected."""
    from src.backends.codex.client import CodexAppServerError

    rpc = _make_rpc_with_queued_line('["array", "not dict"]\n')

    with pytest.raises(CodexAppServerError, match="Invalid Codex JSON-RPC payload"):
        rpc._read_message()


def test_codex_rpc_read_message_raises_when_stdout_closed():
    """Sentinel None from the drain thread surfaces as 'closed stdout'."""
    from src.backends.codex.client import CodexAppServerError

    rpc = _make_rpc_with_queued_line(None)

    with pytest.raises(CodexAppServerError, match="closed stdout"):
        rpc._read_message()


def test_codex_rpc_read_message_raises_when_proc_missing():
    """An unstarted RPC instance refuses to read."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()

    with pytest.raises(CodexAppServerError, match="not running"):
        rpc._read_message()


def test_codex_rpc_write_message_raises_when_proc_missing():
    """An unstarted RPC instance refuses to write."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()

    with pytest.raises(CodexAppServerError, match="not running"):
        rpc._write_message({"id": "x", "method": "ping"})


def test_codex_rpc_close_is_noop_when_proc_missing():
    """close() on a never-started client is a no-op and idempotent."""
    from src.backends.codex.client import CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    rpc.close()
    rpc.close()


def test_codex_rpc_thread_start_raises_when_response_not_dict(monkeypatch):
    """Bare list / string responses for thread/start raise CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(rpc, "request", lambda method, params=None: ["not", "dict"])

    with pytest.raises(CodexAppServerError, match="thread/start"):
        rpc.thread_start({})


def test_codex_rpc_thread_resume_raises_when_response_not_dict(monkeypatch):
    """Bare list / string responses for thread/resume raise CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(rpc, "request", lambda method, params=None: "string")

    with pytest.raises(CodexAppServerError, match="thread/resume"):
        rpc.thread_resume("thr_1", {})


def test_codex_rpc_turn_start_raises_when_response_not_dict(monkeypatch):
    """Bare list / string responses for turn/start raise CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(rpc, "request", lambda method, params=None: "string")

    with pytest.raises(CodexAppServerError, match="turn/start"):
        rpc.turn_start("thr_1", [], {})


def test_codex_rpc_model_list_raises_when_response_not_dict(monkeypatch):
    """Bare list / string responses for model/list raise CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(rpc, "request", lambda method, params=None: None)

    with pytest.raises(CodexAppServerError, match="model/list"):
        rpc.model_list()


# ---------------------------------------------------------------------------
# Group 6: CodexClient async error paths and simple accessors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_verify_returns_false_when_model_list_raises(monkeypatch):
    """If the live process rejects model/list, verify() reports failure."""
    from src.backends.codex.client import CodexClient

    class ExplodingRpc:
        def start(self):
            return None

        def model_list(self):
            raise RuntimeError("nope")

        def close(self):
            return None

    monkeypatch.setattr(
        "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: ExplodingRpc()
    )

    backend = CodexClient()
    assert await backend.verify() is False


@pytest.mark.asyncio
async def test_codex_verify_returns_false_when_data_not_list(monkeypatch):
    """A model/list response missing the 'data' list also yields False."""
    from src.backends.codex.client import CodexClient

    class WrongShapeRpc:
        def start(self):
            return None

        def model_list(self):
            return {"data": "not a list"}

        def close(self):
            return None

    monkeypatch.setattr(
        "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: WrongShapeRpc()
    )

    backend = CodexClient()
    assert await backend.verify() is False


def test_codex_runtime_metadata_includes_expected_keys():
    """runtime_metadata returns a stable dict shape for diagnostics."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    metadata = backend.runtime_metadata()

    assert metadata["mode"] == "app-server"
    assert isinstance(metadata["models"], list)
    assert "approval_policy" in metadata
    assert "sandbox" in metadata
    assert metadata["shared_process"] is False


def test_codex_client_simple_accessors():
    """name, supported_models, get_auth_provider expose the expected types."""
    from src.backends.codex.auth import CodexAuthProvider
    from src.backends.codex.client import CodexClient

    backend = CodexClient()

    assert backend.name == "codex"
    assert isinstance(backend.supported_models(), list)
    assert isinstance(backend.get_auth_provider(), CodexAuthProvider)


@pytest.mark.asyncio
async def test_codex_run_completion_yields_error_when_turn_id_missing(monkeypatch):
    """A turn/start response without turn.id surfaces as an error chunk."""
    from src.backends.codex.client import CodexClient

    class TurnLessRpc:
        def __init__(self):
            self.closed = False

        def is_running(self):
            return not self.closed

        def start(self):
            return None

        def close(self):
            self.closed = True

        def thread_start(self, params):
            return {"thread": {"id": "thr_1"}}

        def thread_resume(self, thread_id, params):
            return {"thread": {"id": thread_id}}

        def turn_start(self, thread_id, input_items, params):
            return {"turn": {}}

        def next_notification(self):
            raise AssertionError("should not be reached")

    monkeypatch.setattr(
        "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: TurnLessRpc()
    )

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw")
    client = await backend.create_client(session=session)

    chunks = [
        chunk async for chunk in backend.run_completion_with_client(client, "hi", session)
    ]

    assert len(chunks) == 1
    assert chunks[0]["type"] == "error"
    assert chunks[0]["is_error"] is True
    assert "turn.id" in chunks[0]["error_message"]


@pytest.mark.asyncio
async def test_codex_resume_approval_errors_when_request_id_missing(monkeypatch):
    """resume_approval rejects a session whose pending request_id was never set."""
    from src.backends.codex.client import (
        CodexClient,
        CodexJsonRpcClient,
        CodexSessionClient,
    )

    backend = CodexClient()

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(backend, "_ensure_rpc_locked", AsyncMock(return_value=rpc))
    monkeypatch.setattr(backend, "_close_rpc_locked", AsyncMock())

    session_client = CodexSessionClient(
        rpc=rpc,
        thread_id="thr_1",
        model=None,
        cwd=None,
        env={},
    )

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            session_client, "call_xyz", "accept", session=SimpleNamespace()
        )
    ]

    assert len(chunks) == 1
    assert chunks[0]["type"] == "error"
    assert "request id" in chunks[0]["error_message"]


@pytest.mark.asyncio
async def test_codex_resume_approval_errors_when_turn_id_missing(monkeypatch):
    """resume_approval rejects a session whose turn id was lost."""
    from src.backends.codex.client import (
        CodexClient,
        CodexJsonRpcClient,
        CodexSessionClient,
    )

    backend = CodexClient()

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(backend, "_ensure_rpc_locked", AsyncMock(return_value=rpc))
    monkeypatch.setattr(backend, "_close_rpc_locked", AsyncMock())

    session_client = CodexSessionClient(
        rpc=rpc,
        thread_id="thr_1",
        model=None,
        cwd=None,
        env={},
        pending_approval_request_id="req_1",
        pending_approval_method="item/commandExecution/requestApproval",
        pending_approval_turn_id=None,
        pending_approval_params={},
    )

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            session_client, "req_1", "accept", session=SimpleNamespace()
        )
    ]

    assert len(chunks) == 1
    assert chunks[0]["type"] == "error"
    assert "turn id" in chunks[0]["error_message"]
