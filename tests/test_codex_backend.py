"""Unit tests for the Codex backend (official openai-codex SDK based).

Transport, process lifecycle, and typed protocol handling live in the SDK;
these tests cover the gateway-owned layers: descriptor/auth wiring, thread &
turn parameter building, the notification→chunk mapping, the approval bridge
(policy auto-decisions + the interactive continuation), and model discovery.
The full HTTP-route flow against a scripted app-server binary lives in
tests/integration/test_codex_e2e.py.
"""

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from openai_codex.models import UnknownNotification

from src.backends.base import BackendRegistry, ResolvedModel
from src.backends.codex import CODEX_DESCRIPTOR, _codex_resolve, register
from src.backends.codex.auth import CodexAuthProvider
from src.backends.codex.client import (
    CODEX_APPROVAL_METHODS,
    CodexAppServerError,
    CodexClient,
    CodexSessionClient,
    _translate_model_params,
    _resolve_approval_policy,
)
from src.backends.codex.constants import sandbox_mode

# ---------------------------------------------------------------------------
# Descriptor / registration / auth
# ---------------------------------------------------------------------------


def test_codex_descriptor_resolves_prefixed_models(monkeypatch):
    resolved = _codex_resolve("codex/gpt-5.5")
    assert resolved == ResolvedModel("codex/gpt-5.5", "codex", "gpt-5.5")
    assert _codex_resolve("codex/") is None
    assert _codex_resolve("claude/opus") is None
    assert CODEX_DESCRIPTOR.name == "codex"
    assert CODEX_DESCRIPTOR.capabilities == {"image_input": True}
    assert CODEX_DESCRIPTOR.model_discovery_fn is not None


def test_codex_register_records_descriptor_and_live_client():
    class FakeRegistry:
        descriptors: Dict[str, Any] = {}
        backends: Dict[str, Any] = {}

        @classmethod
        def register_descriptor(cls, descriptor):
            cls.descriptors[descriptor.name] = descriptor

        @classmethod
        def register(cls, name, client):
            cls.backends[name] = client

    register(registry_cls=FakeRegistry)
    assert "codex" in FakeRegistry.descriptors
    assert isinstance(FakeRegistry.backends["codex"], CodexClient)


def test_codex_register_logs_error_when_client_init_fails(monkeypatch, caplog):
    import src.backends.codex as codex_pkg

    class FakeRegistry:
        @classmethod
        def register_descriptor(cls, descriptor):
            pass

        @classmethod
        def register(cls, name, client):
            raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        codex_pkg.register(registry_cls=FakeRegistry)
    assert any(
        "Codex backend client creation failed" in r.message for r in caplog.records
    )


def test_codex_init_lazy_imports():
    import src.backends.codex as codex_pkg

    assert codex_pkg.CodexClient is CodexClient
    assert codex_pkg.CodexAuthProvider is CodexAuthProvider
    with pytest.raises(AttributeError):
        codex_pkg.no_such_attribute


def test_codex_auth_provider_validates_sdk_bundled_binary(monkeypatch):
    monkeypatch.delenv("CODEX_BIN", raising=False)
    provider = CodexAuthProvider()
    result = provider.validate()
    # The openai-codex package bundles the CLI binary, so validation succeeds
    # without a codex on PATH.
    assert result["valid"] is True
    assert result["config"]["mode"] == "sdk"
    assert result["config"]["binary"]


def test_codex_auth_provider_reports_missing_binary_override(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "/nonexistent/codex-binary")
    provider = CodexAuthProvider()
    result = provider.validate()
    assert result["valid"] is False
    assert result["errors"]


def test_codex_auth_env_includes_codex_settings(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("CODEX_BIN", raising=False)
    env = CodexAuthProvider().build_env()
    assert env["CODEX_HOME"] == "/tmp/codex-home"
    assert env["OPENAI_API_KEY"] == "sk-test"
    assert "ANTHROPIC_AUTH_TOKEN" in CodexAuthProvider().get_isolation_vars()


def test_codex_sandbox_mode_normalizes_legacy_and_sdk_aliases(monkeypatch):
    monkeypatch.setenv("CODEX_SANDBOX", "workspaceWrite")
    assert sandbox_mode() == "workspace-write"
    monkeypatch.setenv("CODEX_SANDBOX", "full-access")
    assert sandbox_mode() == "danger-full-access"
    monkeypatch.setenv("CODEX_SANDBOX", "read-only")
    assert sandbox_mode() == "read-only"
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    assert sandbox_mode() == "danger-full-access"


# ---------------------------------------------------------------------------
# Param building
# ---------------------------------------------------------------------------


def test_codex_resolve_approval_policy_mapping(monkeypatch):
    monkeypatch.delenv("CODEX_APPROVAL_POLICY", raising=False)
    assert _resolve_approval_policy(None) == "never"
    assert _resolve_approval_policy("bypassPermissions") == "never"
    assert _resolve_approval_policy("default") == "on-request"
    assert _resolve_approval_policy("acceptEdits") == "on-request"
    assert _resolve_approval_policy("plan") == "on-request"
    # Unknown modes fall back to on-request, not the env.
    assert _resolve_approval_policy("bogus") == "on-request"
    # A tool policy upgrades never -> on-request so enforcement can run.
    assert (
        _resolve_approval_policy("bypassPermissions", has_tool_policy=True)
        == "on-request"
    )


def test_codex_translate_model_params_maps_reasoning_and_drops_sampling():
    assert _translate_model_params(None) == {}
    assert _translate_model_params({}) == {}
    out = _translate_model_params(
        {
            "effort": "low",
            "summary": "concise",
            "temperature": 0.5,
            "top_p": 0.9,
            "max_output_tokens": 64,
            "max_tokens": 64,
            "unknown_key": "x",
            "skipped": None,
        }
    )
    assert out == {"effort": "low", "summary": "concise"}
    assert _translate_model_params({"reasoning_effort": "high"}) == {"effort": "high"}


def test_codex_thread_params_includes_only_set_fields(monkeypatch):
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("CODEX_APPROVAL_POLICY", raising=False)
    client = CodexClient()
    params = client._thread_params(
        model="gpt-5.5",
        cwd="/workspaces/x",
        system_prompt="be helpful",
        permission_mode="default",
    )
    assert params == {
        "approvalPolicy": "on-request",
        "sandbox": "danger-full-access",
        "model": "gpt-5.5",
        "cwd": "/workspaces/x",
        "developerInstructions": "be helpful",
    }
    bare = client._thread_params(model=None, cwd=None, system_prompt=None)
    assert set(bare) == {"approvalPolicy", "sandbox"}


def test_codex_thread_params_converts_mcp_servers_to_config(monkeypatch):
    client = CodexClient()
    params = client._thread_params(
        model=None,
        cwd=None,
        system_prompt=None,
        mcp_servers={
            "files": {"command": "npx", "args": ["-y", "files-mcp"], "env": {"A": "1"}},
            "search": {
                "type": "http",
                "url": "https://mcp.example/sse",
                "headers": {"X-K": "v"},
            },
            "broken": "not-a-dict",
        },
    )
    assert params["config"] == {
        "mcp_servers": {
            "files": {"command": "npx", "args": ["-y", "files-mcp"], "env": {"A": "1"}},
            "search": {"url": "https://mcp.example/sse", "http_headers": {"X-K": "v"}},
        }
    }


def test_codex_turn_params_uses_session_client_fields(monkeypatch):
    monkeypatch.delenv("CODEX_APPROVAL_POLICY", raising=False)
    client = CodexClient()
    session_client = _make_session_client(
        model="gpt-5.5",
        cwd="/w",
        permission_mode="default",
        model_params={"effort": "high", "temperature": 0.4},
        effort="low",
    )
    params = client._turn_params(session_client)
    assert params["approvalPolicy"] == "on-request"
    assert params["model"] == "gpt-5.5"
    assert params["cwd"] == "/w"
    # Per-request model_params effort overrides the session-level one.
    assert params["effort"] == "high"
    assert "temperature" not in params


def test_codex_session_client_options_shim_for_continuation_validation():
    client = _make_session_client(effort="low")
    assert client.options.effort == "low"
    assert client.options.thinking is None
    disabled = _make_session_client(effort="none")
    assert disabled.options.thinking == {"type": "disabled"}


def test_codex_coerce_turn_input_items():
    assert CodexClient._coerce_turn_input_items("hi") == [
        {"type": "text", "text": "hi"}
    ]
    items = [
        {"type": "text", "text": "a"},
        {"type": "image", "url": "data:image/png;base64,x"},
    ]
    assert CodexClient._coerce_turn_input_items(items) == items
    with pytest.raises(ValueError):
        CodexClient._coerce_turn_input_items([])
    with pytest.raises(ValueError):
        CodexClient._coerce_turn_input_items([{"type": "text"}, "oops"])
    with pytest.raises(ValueError):
        CodexClient._coerce_turn_input_items(42)


def test_codex_metadata_env_filters_by_allowlist(monkeypatch):
    # The allowlist is frozen at import from METADATA_ENV_ALLOWLIST, empty by
    # default; patch the constant to exercise the filter deterministically.
    import src.constants as constants

    monkeypatch.setattr(constants, "METADATA_ENV_ALLOWLIST", frozenset({"THREAD_ID"}))
    client = CodexClient()
    assert client._metadata_env(None) == {}
    env = client._metadata_env({"THREAD_ID": "v", "NOT_ALLOWED_XYZ": "w"})
    assert env == {"THREAD_ID": "v"}


def test_codex_update_request_policy_semantics():
    backend = CodexClient()
    client = _make_session_client(
        allowed_tools=["Bash"],
        disallowed_tools=["Edit"],
        permission_mode="default",
        model_params={"effort": "low"},
    )
    backend.update_request_policy(client, allowed_tools=[], disallowed_tools=None)
    assert client.allowed_tools == []  # explicit block-all preserved
    assert client.disallowed_tools is None
    assert client.model_params is None  # reset when the request has none
    assert client.permission_mode == "default"  # None keeps existing
    backend.update_request_policy(client, permission_mode="acceptEdits")
    assert client.permission_mode == "acceptEdits"


def test_codex_runtime_metadata_includes_expected_keys():
    metadata = CodexClient().runtime_metadata()
    assert metadata["mode"] == "sdk"
    assert "sdk_version" in metadata
    assert isinstance(metadata["models"], list)


# ---------------------------------------------------------------------------
# Approval decisions / arguments (pure logic)
# ---------------------------------------------------------------------------


def test_codex_normalize_approval_decision_aliases():
    client = CodexClient()
    assert client._normalize_approval_decision("accept") == "accept"
    assert client._normalize_approval_decision("yes") == "accept"
    assert client._normalize_approval_decision("always") == "acceptForSession"
    assert client._normalize_approval_decision("deny") == "decline"
    assert client._normalize_approval_decision("stop") == "cancel"
    assert client._normalize_approval_decision("") == "decline"
    assert client._normalize_approval_decision("garbage") == "decline"
    assert client._normalize_approval_decision(["yes", "no"]) == "accept"
    assert client._normalize_approval_decision(None) == "decline"


def test_codex_approval_kind_and_question():
    client = CodexClient()
    assert client._approval_kind("item/commandExecution/requestApproval") == "command"
    assert client._approval_kind("item/fileChange/requestApproval") == "file_change"
    assert client._approval_kind("item/permissions/requestApproval") == "permissions"
    assert client._approval_kind("item/other/requestApproval") == "approval"
    assert "printf x" in client._approval_question("command", {"command": "printf x"})
    assert client._approval_question("command", {})
    assert client._approval_question("file_change", {})
    assert client._approval_question("permissions", {})
    assert client._approval_question("approval", {})


def test_codex_approval_arguments_exposes_command_and_options():
    client = CodexClient()
    arguments = client._approval_arguments(
        "item/commandExecution/requestApproval",
        {
            "command": "printf e2e",
            "cwd": "/tmp",
            "reason": "why",
            "itemId": "cmd_1",
            "availableDecisions": ["accept", "decline"],
        },
    )
    assert arguments["kind"] == "command"
    assert arguments["command"] == "printf e2e"
    assert arguments["reason"] == "why"
    assert arguments["itemId"] == "cmd_1"
    assert [o["label"] for o in arguments["options"]] == ["accept", "decline"]


def test_codex_approval_decision_label_handles_dict_decisions():
    client = CodexClient()
    assert client._approval_decision_label("accept") == "accept"
    assert client._approval_decision_label({}) == ""
    assert (
        client._approval_decision_label({"acceptWithExecpolicyAmendment": {}})
        == "acceptWithExecpolicyAmendment"
    )
    label = client._approval_decision_label(
        {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {"action": "allow", "host": "example.com"}
            }
        }
    )
    assert label == "applyNetworkPolicyAmendment:allow:example.com"


def test_codex_approval_result_from_output_paths():
    client = CodexClient()
    method = "item/commandExecution/requestApproval"
    assert client._approval_result_from_output(method, "accept", {}) == {
        "decision": "accept"
    }
    assert client._approval_result_from_output(
        method, '{"decision": "decline"}', {}
    ) == {"decision": "decline"}
    # Structured decisions matching availableDecisions are preserved verbatim.
    structured = {"acceptWithExecpolicyAmendment": {"rule": "x"}}
    params = {"availableDecisions": ["accept", structured]}
    assert client._approval_result_from_output(
        method, "acceptWithExecpolicyAmendment", params
    ) == {"decision": structured}
    # Permissions accept echoes requested permissions with scope.
    perm_method = "item/permissions/requestApproval"
    perm_params = {"permissions": {"fileSystem": {"read": ["/tmp"]}}}
    accept = client._approval_result_from_output(
        perm_method, "acceptForSession", perm_params
    )
    assert accept == {"permissions": perm_params["permissions"], "scope": "session"}
    decline = client._approval_result_from_output(perm_method, "decline", perm_params)
    assert decline == {"permissions": {}, "scope": "turn"}


def test_codex_tool_policy_identities_and_matching():
    client = CodexClient()
    identities = client._approval_tool_identities(
        {
            "method": "item/mcpToolCall/requestApproval",
            "params": {"serverLabel": "my-server", "toolName": "search"},
        }
    )
    assert "mcpToolCall" in identities
    assert "mcp__my-server__search" in identities
    assert "mcp__my_server__search" in identities
    assert client._tool_policy_matches({"mcp__my_server__*"}, identities)
    assert client._normalize_tool_names(["Bash", "custom"]) == {
        "commandExecution",
        "custom",
    }


# ---------------------------------------------------------------------------
# Item / usage / message mapping (pure logic)
# ---------------------------------------------------------------------------


def test_codex_tool_use_from_item_strips_meta_fields():
    client = CodexClient()
    item = {
        "type": "commandExecution",
        "id": "cmd_1",
        "command": "ls",
        "aggregatedOutput": "big",
    }
    tool_use = client._tool_use_from_item(item)
    assert tool_use == {
        "type": "tool_use",
        "id": "cmd_1",
        "name": "commandExecution",
        "input": {"command": "ls"},
    }
    assert client._tool_use_from_item({"type": "agentMessage", "id": "x"}) is None
    assert client._tool_use_from_item({"type": "commandExecution"}) is None
    assert client._tool_use_from_item("nope") is None


def test_codex_tool_result_from_item_command_exit_code_and_errors():
    client = CodexClient()
    failed = client._tool_result_from_item(
        {
            "type": "commandExecution",
            "id": "cmd_1",
            "status": "completed",
            "exitCode": 2,
            "aggregatedOutput": "boom",
        }
    )
    assert failed["is_error"] is True
    assert failed["content"] == "boom"
    declined = client._tool_result_from_item(
        {"type": "fileChange", "id": "f1", "status": "declined"}
    )
    assert declined["is_error"] is True
    assert json.loads(declined["content"])["status"] == "declined"
    missing_output = client._tool_result_from_item(
        {"type": "commandExecution", "id": "c2", "status": "completed", "exitCode": 0}
    )
    assert json.loads(missing_output["content"])["exitCode"] == 0
    assert client._tool_result_from_item({"type": "agentMessage", "id": "m"}) is None


def test_codex_extract_usage_includes_reasoning_and_cached_tokens():
    client = CodexClient()
    usage = client._extract_usage(
        {
            "last": {
                "inputTokens": 10,
                "cachedInputTokens": 5,
                "outputTokens": 3,
                "reasoningOutputTokens": 2,
            }
        }
    )
    assert usage == {"input_tokens": 15, "output_tokens": 5}
    assert client._extract_usage(None) is None
    assert client._extract_usage({"last": "nope"}) is None


def test_codex_final_response_prefers_final_answer_phase():
    client = CodexClient()
    items = [
        {"type": "agentMessage", "text": "draft", "phase": None},
        {"type": "agentMessage", "text": "final", "phase": "final_answer"},
        {"type": "commandExecution", "id": "c"},
    ]
    assert client._final_response_from_items(items) == "final"
    assert (
        client._final_response_from_items([{"type": "agentMessage", "text": "d"}])
        == "d"
    )
    assert client._final_response_from_items([]) is None


def test_codex_turn_error_message_and_public_error():
    client = CodexClient()
    assert client._turn_error_message({"error": {"message": "boom"}}) == "boom"
    assert client._turn_error_message({}) == "Codex turn failed"
    err = CodexAppServerError("failed. stderr_tail=secret stuff")
    assert client._public_error_message(err) == "failed."
    assert client._public_error_message(RuntimeError("")) == "Codex app-server error"


def test_codex_parse_message_prefers_success_result():
    client = CodexClient()
    messages = [
        {"type": "assistant", "content": [{"type": "text", "text": "partial"}]},
        {"type": "result", "subtype": "success", "result": "final answer"},
    ]
    assert client.parse_message(messages) == "final answer"
    fallback = client.parse_message(
        [{"type": "assistant", "content": [{"type": "text", "text": "only text"}]}]
    )
    assert fallback == "only text"
    assert client.parse_message([]) is None


def test_codex_estimate_token_usage_uses_length_heuristic():
    usage = CodexClient().estimate_token_usage("p" * 40, "c" * 8)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


def test_codex_chunks_from_notifications_end_to_end_mapping():
    client = CodexClient()
    notifications = [
        {
            "method": "item/started",
            "params": {
                "turnId": "t1",
                "item": {"type": "commandExecution", "id": "c1", "command": "ls"},
            },
        },
        {
            "method": "item/agentMessage/delta",
            "params": {"turnId": "t1", "delta": "he"},
        },
        {
            "method": "item/agentMessage/delta",
            "params": {"turnId": "t1", "delta": "llo"},
        },
        {"method": "other/turn", "params": {"turnId": "other"}},
        {
            "method": "item/completed",
            "params": {
                "turnId": "t1",
                "item": {
                    "type": "commandExecution",
                    "id": "c1",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "ok",
                },
            },
        },
        {
            "method": "item/completed",
            "params": {
                "turnId": "t1",
                "item": {
                    "type": "agentMessage",
                    "id": "m1",
                    "phase": "final_answer",
                    "text": "hello",
                },
            },
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "t1",
                "tokenUsage": {"last": {"inputTokens": 1, "outputTokens": 2}},
            },
        },
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "t1", "status": "completed"}},
        },
    ]
    chunks = list(
        client._chunks_from_notifications(turn_id="t1", notifications=notifications)
    )
    types = [c["type"] for c in chunks]
    assert types == [
        "assistant",
        "stream_event",
        "stream_event",
        "user",
        "assistant",
        "result",
    ]
    assert chunks[-1]["result"] == "hello"
    assert chunks[-1]["usage"] == {"input_tokens": 1, "output_tokens": 2}


def test_codex_chunks_report_failed_turn():
    client = CodexClient()
    chunks = list(
        client._chunks_from_notifications(
            turn_id="t1",
            notifications=[
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "id": "t1",
                            "status": "failed",
                            "error": {"message": "x"},
                        }
                    },
                }
            ],
        )
    )
    assert chunks == [{"type": "error", "is_error": True, "error_message": "x"}]


# ---------------------------------------------------------------------------
# SDK-facing fakes
# ---------------------------------------------------------------------------


class FakeRawClient:
    """Stands in for the SDK's AsyncCodexClient."""

    def __init__(self):
        self.thread_start_calls: List[Dict[str, Any]] = []
        self.thread_resume_calls: List[Any] = []
        self.turn_start_calls: List[Any] = []
        self.interrupts: List[Any] = []
        self.queues: Dict[str, asyncio.Queue] = {}
        self.next_turn_id = "turn_1"

    async def thread_start(self, params=None):
        self.thread_start_calls.append(params)
        return SimpleNamespace(thread=SimpleNamespace(id="thr_1"))

    async def thread_resume(self, thread_id, params=None):
        self.thread_resume_calls.append((thread_id, params))
        return SimpleNamespace(thread=SimpleNamespace(id=thread_id))

    async def turn_start(self, thread_id, input_items, params=None):
        self.turn_start_calls.append((thread_id, input_items, params))
        return SimpleNamespace(turn=SimpleNamespace(id=self.next_turn_id))

    def register_turn_notifications(self, turn_id):
        self.queues.setdefault(turn_id, asyncio.Queue())

    def unregister_turn_notifications(self, turn_id):
        self.queues.pop(turn_id, None)

    async def next_turn_notification(self, turn_id):
        return await self.queues[turn_id].get()

    async def turn_interrupt(self, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))
        return SimpleNamespace()

    async def model_list(self, include_hidden=False):
        return SimpleNamespace(
            data=[SimpleNamespace(id="gpt-5.5"), SimpleNamespace(id="gpt-5.2")]
        )

    def feed(self, turn_id, method, params):
        self.queues[turn_id].put_nowait(
            SimpleNamespace(method=method, payload=UnknownNotification(params=params))
        )


class FakeCodex:
    def __init__(self):
        self._client = FakeRawClient()
        self.closed = False
        self.approval_handler = None

    async def close(self):
        self.closed = True


@pytest.fixture
def sdk_backend(monkeypatch):
    """CodexClient wired to a FakeCodex; returns (backend, fake holder)."""
    backend = CodexClient()
    holder: Dict[str, FakeCodex] = {}

    def _new_codex(env=None, approval_handler=None):
        fake = FakeCodex()
        fake.approval_handler = approval_handler
        holder["codex"] = fake
        return fake

    async def _start_codex(codex):
        return None

    monkeypatch.setattr(backend, "_new_codex", _new_codex)
    monkeypatch.setattr(backend, "_start_codex", _start_codex)
    return backend, holder


def _make_session_client(**overrides) -> CodexSessionClient:
    defaults = dict(
        codex=None,
        thread_id="thr_1",
        model=None,
        cwd=None,
        loop=None,
    )
    defaults.update(overrides)
    return CodexSessionClient(**defaults)


def _feed_happy_turn(raw: FakeRawClient, turn_id: str = "turn_1", text: str = "done"):
    raw.feed(
        turn_id,
        "item/completed",
        {
            "turnId": turn_id,
            "item": {
                "type": "agentMessage",
                "id": "m1",
                "phase": "final_answer",
                "text": text,
            },
        },
    )
    raw.feed(
        turn_id,
        "turn/completed",
        {"turn": {"id": turn_id, "status": "completed"}},
    )


# ---------------------------------------------------------------------------
# create_client / run_completion / approval bridge
# ---------------------------------------------------------------------------


async def test_codex_create_client_starts_thread_and_reuses_on_resume(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(
        session=session,
        model="gpt-5.5",
        system_prompt="sys",
        permission_mode="default",
        cwd="/w",
        _custom_base="base",
    )
    assert client.thread_id == "thr_1"
    assert session.codex_thread_id == "thr_1"
    fake = holder["codex"]
    (params,) = fake._client.thread_start_calls
    assert params["developerInstructions"] == "base\n\nsys"
    assert params["serviceName"] == "oh-my-gateway"

    # Second client for the same session resumes the recorded thread.
    client2 = await backend.create_client(session=session, model="gpt-5.5")
    fake2 = holder["codex"]
    assert client2.thread_id == "thr_1"
    assert fake2._client.thread_resume_calls[0][0] == "thr_1"


async def test_codex_create_client_closes_codex_on_thread_start_failure(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)

    async def boom(params=None):
        raise RuntimeError("no thread")

    original_new = backend._new_codex

    def new_codex(env=None, approval_handler=None):
        fake = original_new(env, approval_handler)
        fake._client.thread_start = boom
        return fake

    backend._new_codex = new_codex
    with pytest.raises(RuntimeError):
        await backend.create_client(session=session)
    assert holder["codex"].closed is True


async def test_codex_run_completion_streams_chunks(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session, model="gpt-5.5")
    fake = holder["codex"]

    chunks: List[Dict[str, Any]] = []

    async def consume():
        async for chunk in backend.run_completion_with_client(client, "hi", session):
            chunks.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    raw = fake._client
    assert raw.turn_start_calls[0][1] == [{"type": "text", "text": "hi"}]
    raw.feed("turn_1", "item/agentMessage/delta", {"turnId": "turn_1", "delta": "do"})
    _feed_happy_turn(raw)
    await asyncio.wait_for(task, timeout=5)

    types = [c["type"] for c in chunks]
    assert types == ["stream_event", "assistant", "result"]
    assert chunks[-1]["result"] == "done"
    assert client.active_turn is None


async def test_codex_run_completion_multimodal_items_pass_verbatim(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session)
    fake = holder["codex"]

    items = [
        {"type": "text", "text": "what is this?"},
        {"type": "image", "url": "data:image/png;base64,xyz"},
    ]

    async def consume():
        return [
            c async for c in backend.run_completion_with_client(client, items, session)
        ]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    raw = fake._client
    assert raw.turn_start_calls[0][1] == items
    _feed_happy_turn(raw)
    await asyncio.wait_for(task, timeout=5)


async def test_codex_turn_start_failure_yields_error_chunk(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session)

    async def boom(thread_id, input_items, params=None):
        raise CodexAppServerError("turn refused. stderr_tail=noise")

    holder["codex"]._client.turn_start = boom
    chunks = [
        c async for c in backend.run_completion_with_client(client, "hi", session)
    ]
    assert chunks == [
        {"type": "error", "is_error": True, "error_message": "turn refused."}
    ]
    assert client.active_turn is None


async def test_codex_interactive_approval_surfaces_and_resumes(sdk_backend):
    """Full approval bridge: reader-thread handler -> tool chunk -> resume."""
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session, permission_mode="default")
    fake = holder["codex"]
    raw = fake._client

    first_chunks: List[Dict[str, Any]] = []

    async def consume_first():
        async for chunk in backend.run_completion_with_client(
            client, "run it", session
        ):
            first_chunks.append(chunk)

    task = asyncio.create_task(consume_first())
    await asyncio.sleep(0.05)

    # Simulate the SDK reader thread delivering an approval request.
    handler_result: Dict[str, Any] = {}

    def reader_thread():
        handler_result["result"] = fake.approval_handler(
            "item/commandExecution/requestApproval",
            {
                "turnId": "turn_1",
                "command": "printf x",
                "availableDecisions": ["accept"],
            },
        )

    thread = threading.Thread(target=reader_thread)
    thread.start()
    await asyncio.wait_for(task, timeout=5)

    # The stream ended with the approval surfaced as an AskUserQuestion call.
    assert len(first_chunks) == 1
    tool_block = first_chunks[0]["content"][0]
    assert tool_block["name"] == "codex_approval"
    call_id = tool_block["metadata"]["codex_approval_request_id"]
    assert session.pending_tool_call["call_id"] == call_id
    assert session.pending_tool_call["name"] == "AskUserQuestion"
    assert session.pending_tool_call["codex_resume"] == "approval"
    assert client.pending_approval is not None
    assert thread.is_alive()  # reader still blocked awaiting the decision

    # Continuation: supply the decision and drain the rest of the turn.
    second_chunks: List[Dict[str, Any]] = []

    async def consume_second():
        async for chunk in backend.resume_approval_with_client(
            client, call_id, "accept", session
        ):
            second_chunks.append(chunk)

    resume_task = asyncio.create_task(consume_second())
    await asyncio.sleep(0.05)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert handler_result["result"] == {"decision": "accept"}

    _feed_happy_turn(raw, text="approved run")
    await asyncio.wait_for(resume_task, timeout=5)
    assert second_chunks[-1]["result"] == "approved run"
    assert client.pending_approval is None


async def test_codex_resume_approval_rejects_mismatched_call_id(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session)
    from src.backends.codex.client import _PendingApproval, _ActiveTurn

    client.pending_approval = _PendingApproval(
        request_id="right", method="item/commandExecution/requestApproval", params={}
    )
    client.active_turn = _ActiveTurn(queue=asyncio.Queue(), turn_id="turn_1")
    chunks = [
        c
        async for c in backend.resume_approval_with_client(
            client, "wrong", "accept", session
        )
    ]
    assert chunks[0]["type"] == "error"
    assert "mismatch" in chunks[0]["error_message"]


async def test_codex_resume_approval_without_pending_state_errors(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session)
    chunks = [
        c
        async for c in backend.resume_approval_with_client(
            client, "x", "accept", session
        )
    ]
    assert chunks[0]["type"] == "error"


async def test_codex_approval_auto_deny_by_policy_returns_inline(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(
        session=session, disallowed_tools=["Bash"], permission_mode="default"
    )
    fake = holder["codex"]
    result = fake.approval_handler(
        "item/commandExecution/requestApproval", {"turnId": "t", "command": "rm -rf"}
    )
    assert result == {"decision": "decline"}
    # Allow-list that doesn't cover the tool also denies.
    backend.update_request_policy(client, allowed_tools=["Edit"])
    result = fake.approval_handler(
        "item/commandExecution/requestApproval", {"turnId": "t"}
    )
    assert result == {"decision": "decline"}


async def test_codex_approval_accept_edits_auto_accepts_file_change_only(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session, permission_mode="acceptEdits")
    fake = holder["codex"]
    assert fake.approval_handler(
        "item/fileChange/requestApproval", {"turnId": "t"}
    ) == {"decision": "accept"}
    # Commands are NOT auto-accepted; with no active turn they fail closed.
    assert fake.approval_handler(
        "item/commandExecution/requestApproval", {"turnId": "t"}
    ) == {"decision": "decline"}


async def test_codex_approval_unknown_server_request_returns_empty(sdk_backend, caplog):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    await backend.create_client(session=session)
    fake = holder["codex"]
    with caplog.at_level(logging.WARNING):
        assert fake.approval_handler("some/new/serverRequest", {}) == {}
    assert any("Unknown Codex server request" in r.message for r in caplog.records)


async def test_codex_approval_timeout_cancels(sdk_backend, monkeypatch):
    monkeypatch.setenv("CODEX_APPROVAL_TIMEOUT_MS", "50")
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session, permission_mode="default")
    fake = holder["codex"]
    from src.backends.codex.client import _ActiveTurn

    client.active_turn = _ActiveTurn(queue=asyncio.Queue(), turn_id="turn_1")

    result_box: Dict[str, Any] = {}

    def reader_thread():
        result_box["result"] = fake.approval_handler(
            "item/commandExecution/requestApproval", {"turnId": "turn_1"}
        )

    thread = threading.Thread(target=reader_thread)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result_box["result"] == {"decision": "cancel"}
    assert client.pending_approval is None


async def test_codex_idle_timeout_interrupts_turn(sdk_backend):
    backend, holder = sdk_backend
    backend.read_idle_timeout = 0.05
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session)
    chunks = [
        c async for c in backend.run_completion_with_client(client, "hi", session)
    ]
    assert chunks[-1]["type"] == "error"
    assert "Timed out" in chunks[-1]["error_message"]
    raw = holder["codex"]._client
    assert raw.interrupts  # best-effort interrupt fired
    # Teardown leaves the pump running (see _teardown_turn); the interrupted
    # turn's completion lets it exit cleanly instead of dying with the loop.
    raw.feed(
        "turn_1", "turn/completed", {"turn": {"id": "turn_1", "status": "interrupted"}}
    )
    await asyncio.sleep(0.05)


async def test_codex_disconnect_closes_process_and_unblocks_approval(sdk_backend):
    backend, holder = sdk_backend
    session = SimpleNamespace(pending_tool_call=None)
    client = await backend.create_client(session=session, permission_mode="default")
    fake = holder["codex"]
    from src.backends.codex.client import _ActiveTurn

    client.active_turn = _ActiveTurn(queue=asyncio.Queue(), turn_id="turn_1")

    result_box: Dict[str, Any] = {}

    def reader_thread():
        result_box["result"] = fake.approval_handler(
            "item/commandExecution/requestApproval", {"turnId": "turn_1"}
        )

    thread = threading.Thread(target=reader_thread)
    thread.start()
    await asyncio.sleep(0.05)
    await client.disconnect()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result_box["result"] == {"decision": "cancel"}
    assert fake.closed is True


async def test_codex_verify_reports_model_list(sdk_backend):
    backend, holder = sdk_backend
    assert await backend.verify() is True
    assert holder["codex"].closed is True

    async def boom(include_hidden=False):
        raise RuntimeError("down")

    original_new = backend._new_codex

    def new_codex(env=None, approval_handler=None):
        fake = original_new(env, approval_handler)
        fake._client.model_list = boom
        return fake

    backend._new_codex = new_codex
    assert await backend.verify() is False


def test_codex_wire_notification_prefers_unknown_params():
    client = CodexClient()
    unknown = SimpleNamespace(
        method="item/agentMessage/delta",
        payload=UnknownNotification(params={"turnId": "t", "delta": "x"}),
    )
    assert client._wire_notification(unknown) == {
        "method": "item/agentMessage/delta",
        "params": {"turnId": "t", "delta": "x"},
    }

    from openai_codex.generated.v2_all import AgentMessageDeltaNotification

    typed = SimpleNamespace(
        method="item/agentMessage/delta",
        payload=AgentMessageDeltaNotification(
            thread_id="th", turn_id="t", item_id="i", delta="y"
        ),
    )
    wire = client._wire_notification(typed)
    assert wire["params"]["turnId"] == "t"
    assert wire["params"]["delta"] == "y"


def test_codex_sdk_approval_handler_seam_still_exists():
    """Canary for the pinned-SDK private seam create_client relies on."""
    from openai_codex import AsyncCodex

    codex = AsyncCodex()
    assert hasattr(codex._client._sync, "_approval_handler")


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


async def test_codex_model_discovery_disabled_by_default(monkeypatch):
    from src.backends.codex import model_discovery

    model_discovery._reset_cache_for_tests()
    monkeypatch.delenv("CODEX_MODEL_DISCOVERY_ENABLED", raising=False)
    assert await model_discovery.discover_models() == []
    assert model_discovery.discovered_model_ids() == frozenset()


async def test_codex_model_discovery_caches_and_prefixes(monkeypatch):
    from src.backends.codex import model_discovery

    model_discovery._reset_cache_for_tests()
    monkeypatch.setenv("CODEX_MODEL_DISCOVERY_ENABLED", "true")
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return ["codex/gpt-5.5", "codex/gpt-5.2"]

    monkeypatch.setattr(model_discovery, "_fetch_model_ids", fake_fetch)
    assert await model_discovery.discover_models() == ["codex/gpt-5.5", "codex/gpt-5.2"]
    assert await model_discovery.discover_models() == ["codex/gpt-5.5", "codex/gpt-5.2"]
    assert calls["n"] == 1  # TTL cache absorbed the second read
    assert model_discovery.discovered_model_ids() == frozenset(
        {"codex/gpt-5.5", "codex/gpt-5.2"}
    )
    model_discovery._reset_cache_for_tests()


async def test_codex_model_discovery_failure_keeps_stale_snapshot(monkeypatch):
    from src.backends.codex import model_discovery

    model_discovery._reset_cache_for_tests()
    monkeypatch.setenv("CODEX_MODEL_DISCOVERY_ENABLED", "true")

    async def ok_fetch():
        return ["codex/gpt-5.5"]

    monkeypatch.setattr(model_discovery, "_fetch_model_ids", ok_fetch)
    assert await model_discovery.discover_models() == ["codex/gpt-5.5"]

    async def bad_fetch():
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(model_discovery, "_fetch_model_ids", bad_fetch)
    model_discovery._cache.expires_at = 0  # force refresh
    assert await model_discovery.discover_models() == ["codex/gpt-5.5"]
    model_discovery._reset_cache_for_tests()
