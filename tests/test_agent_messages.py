"""Focused contract tests for POST /v1/agents/messages."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import src.main as main
import src.routes.agent_messages as agent_messages
from claude_agent_sdk.types import (
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from src.backends.base import BackendRegistry
from src.session_manager import session_manager


def _frames(payload: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in payload.split("\n\n"):
        if not block or block.startswith(":"):
            continue
        event = "message"
        data = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data.append(line.removeprefix("data: "))
        if data:
            frames.append((event, json.loads("\n".join(data))))
    return frames


class _FakeClient:
    def __init__(self):
        self.disconnect = AsyncMock()


class _FakeBackend:
    name = "claude"

    def __init__(self, messages, *, pending_question: bool = False):
        self.messages = messages
        self.pending_question = pending_question
        self.client = _FakeClient()
        self.create_kwargs = None
        self.prompt = None
        self.run_session = None

    async def create_client(self, **kwargs):
        self.create_kwargs = kwargs
        return self.client

    async def run_completion_with_client(self, client, prompt, session):
        self.prompt = prompt
        self.run_session = session
        for message in self.messages:
            yield message
        if self.pending_question:
            session.pending_tool_call = {
                "call_id": "toolu-question",
                "name": "AskUserQuestion",
            }


class _Workspace:
    def __init__(self, path: Path):
        self.path = path
        self.resolve_calls = []
        self.cleaned = []

    def resolve(self, user=None, backend=None):
        self.resolve_calls.append((user, backend))
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def cleanup_temp_workspace(self, workspace):
        self.cleaned.append(workspace)


def _install_endpoint_fakes(monkeypatch, tmp_path, backend):
    workspace = _Workspace(tmp_path / "_tmp_agent_messages")
    transcript_root = tmp_path / "sdk-project"

    def transcript_path(session_id, _workspace):
        return transcript_root / f"{session_id}.jsonl"

    async def verify_api_key(_request, _credentials):
        return True

    monkeypatch.setattr(agent_messages, "workspace_manager", workspace)
    monkeypatch.setattr(agent_messages, "_session_jsonl_path", transcript_path)
    monkeypatch.setattr(
        agent_messages,
        "get_mcp_servers",
        lambda: {"test": {"type": "stdio"}},
    )
    monkeypatch.setattr(agent_messages, "verify_api_key", verify_api_key)
    monkeypatch.setattr(
        agent_messages,
        "validate_backend_auth_or_raise",
        lambda _name: None,
    )
    BackendRegistry.register("claude", backend)
    return workspace, transcript_root


def test_stateless_stream_projects_sdk_messages_for_noah(monkeypatch, tmp_path):
    messages = [
        {
            "type": "system",
            "subtype": "init",
            "data": {
                "model": "sonnet",
                "session_id": "sdk-session-secret",
                "cwd": "/private/gateway/path",
                "plugins": [
                    {"name": "review", "status": "loaded", "path": "/private/plugin"},
                    "/private/plugins/design-helper",
                ],
                # CLI 2.1.219+ startup diagnostics: gateway-log-only, must
                # never reach the Noah wire (not in _SYSTEM_DATA_FIELDS).
                "mcp_server_errors": [
                    {
                        "name": "brokensrv",
                        "type": "invalid_config",
                        "message": "Skipped — invalid MCP server config",
                    }
                ],
            },
        },
        {
            "type": "system",
            "subtype": "task_started",
            "data": {
                "task_id": "task-1",
                "description": "Inspect code",
                "task_type": "subagent",
                "uuid": "internal-task-uuid",
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
            "session_id": "sdk-session-secret",
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "considering"},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"api_key":"stream-secret"}',
                },
            },
        },
        {
            "type": "assistant",
            "content": [
                TextBlock(text="final text"),
                ThinkingBlock(thinking="private thought", signature="opaque"),
                ToolUseBlock(
                    id="tool-1",
                    name="Bash",
                    input={
                        "command": ("curl -H 'Authorization: Bearer abc123' https://example.test"),
                        "api_key": "top-secret",
                    },
                ),
            ],
            "model": "sonnet",
            "message_id": "api-message-1",
            "usage": {"input_tokens": 21, "output_tokens": 3},
            "stop_reason": "tool_use",
            "session_id": "sdk-session-secret",
        },
        {
            "type": "user",
            "content": [
                ToolResultBlock(
                    tool_use_id="tool-1",
                    content="PASSWORD=hunter2 and private command output",
                    is_error=False,
                )
            ],
            "tool_use_result": {"stdout": "private command output"},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "final text",
            "usage": {"input_tokens": 30, "output_tokens": 4},
            "model_usage": {"sonnet": {"context_window": 200_000}},
            "session_id": "sdk-session-secret",
        },
    ]
    backend = _FakeBackend(messages)
    workspace, transcript_root = _install_endpoint_fakes(monkeypatch, tmp_path, backend)

    # Simulate the SDK's out-of-workspace transcript and tool-result artifacts.
    original_create = backend.create_client

    async def create_with_transcript(**kwargs):
        client = await original_create(**kwargs)
        transcript = transcript_root / f"{kwargs['session'].session_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("ephemeral transcript")
        artifact_dir = transcript.parent / kwargs["session"].session_id / "tool-results"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "tool.txt").write_text("secret")
        return client

    backend.create_client = create_with_transcript
    assert not session_manager.sessions

    response = TestClient(main.app).post(
        "/v1/agents/messages",
        json={
            "agent": "claude",
            "model": "sonnet",
            "system": "Be concise",
            "messages": [
                {"role": "user", "content": "First question"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "First answer"}],
                },
                {"role": "user", "content": "Follow up"},
            ],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["x-agent-message-schema"] == agent_messages.AGENT_MESSAGE_SCHEMA
    frames = _frames(response.text)
    assert [event for event, _ in frames] == [
        "message_start",
        "sdk_message",
        "sdk_message",
        "sdk_message",
        "sdk_message",
        "sdk_message",
        "sdk_message",
        "sdk_message",
        "sdk_message",
        "message_stop",
    ]
    sdk = [data for event, data in frames if event == "sdk_message"]

    init = sdk[0]
    assert init["model"] == "sonnet"
    assert init["plugins"] == [
        {"name": "review", "status": "loaded"},
        "design-helper",
    ]
    assert "cwd" not in init["data"]
    assert "brokensrv" not in response.text  # startup diagnostics stay log-only
    task = sdk[1]
    assert task["task_id"] == "task-1"
    assert task["task_type"] == "subagent"
    assert sdk[2]["event"]["delta"] == {"type": "text_delta", "text": "hello"}
    assert sdk[3]["event"]["delta"] == {
        "type": "thinking_delta",
        "thinking": "considering",
    }

    assert sdk[4]["event"] == {"type": "content_block_delta"}
    assert "stream-secret" not in response.text

    assistant = sdk[5]
    assert assistant["message"]["content"][0] == {"type": "text", "text": "final text"}
    tool = assistant["message"]["content"][2]
    assert tool["type"] == "tool_use"
    assert tool["input"]["api_key"] == "***REDACTED***"
    assert "abc123" not in tool["input"]["command"]

    user = sdk[6]
    assert user["message"]["content"] == [
        {"type": "tool_result", "tool_use_id": "tool-1", "is_error": False}
    ]
    assert "tool_use_result" not in user
    assert "private command output" not in response.text

    result = sdk[7]
    assert result["usage"] == {"input_tokens": 30, "output_tokens": 4}
    assert result["modelUsage"]["sonnet"]["contextWindow"] == 200_000
    assert all("session_id" not in json.dumps(item) for item in sdk)
    assert "internal-task-uuid" not in response.text
    assert frames[-1][1]["status"] == "completed"

    assert backend.create_kwargs["cwd"] == str(workspace.path)
    assert backend.create_kwargs["include_partial_messages"] is True
    assert backend.create_kwargs["disallowed_tools"] == ["AskUserQuestion"]
    assert backend.create_kwargs["system_prompt"] == "Be concise"
    transcript = json.loads(backend.prompt.split("\n\n", 1)[1])
    assert transcript == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Follow up"},
    ]
    assert workspace.resolve_calls == [(None, "claude")]
    assert workspace.cleaned == [workspace.path]
    backend.client.disconnect.assert_awaited_once()
    assert not transcript_root.exists()
    assert not session_manager.sessions


def test_sdk_error_is_redacted_and_resources_are_cleaned(monkeypatch, tmp_path):
    backend = _FakeBackend(
        [
            {
                "type": "error",
                "is_error": True,
                "error_message": "failed with API_KEY=do-not-leak at /private/path",
            }
        ]
    )
    workspace, _ = _install_endpoint_fakes(monkeypatch, tmp_path, backend)

    response = TestClient(main.app).post(
        "/v1/agents/messages",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    frames = _frames(response.text)
    assert [event for event, _ in frames] == ["message_start", "error"]
    assert frames[-1][1]["error"]["type"] == "backend_error"
    assert "do-not-leak" not in response.text
    backend.client.disconnect.assert_awaited_once()
    assert workspace.cleaned == [workspace.path]


def test_pending_interactive_question_ends_with_explicit_error(monkeypatch, tmp_path):
    backend = _FakeBackend([], pending_question=True)
    _install_endpoint_fakes(monkeypatch, tmp_path, backend)

    response = TestClient(main.app).post(
        "/v1/agents/messages",
        json={"messages": [{"role": "user", "content": "ask me something"}]},
    )

    frames = _frames(response.text)
    assert [event for event, _ in frames] == ["message_start", "error"]
    assert frames[-1][1]["error"]["type"] == "interactive_question_unsupported"
    assert backend.create_kwargs["disallowed_tools"] == ["AskUserQuestion"]


def test_result_error_is_forwarded_then_stops_failed(monkeypatch, tmp_path):
    backend = _FakeBackend(
        [
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "errors": ["maximum turns"],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        ]
    )
    _install_endpoint_fakes(monkeypatch, tmp_path, backend)

    response = TestClient(main.app).post(
        "/v1/agents/messages",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    frames = _frames(response.text)
    assert [event for event, _ in frames] == [
        "message_start",
        "sdk_message",
        "message_stop",
    ]
    assert frames[1][1]["subtype"] == "error_max_turns"
    assert frames[-1][1]["status"] == "failed"


def test_v1_rejects_unsupported_agent_and_non_stream(monkeypatch, tmp_path):
    backend = _FakeBackend([])
    _install_endpoint_fakes(monkeypatch, tmp_path, backend)
    client = TestClient(main.app)

    unsupported = client.post(
        "/v1/agents/messages",
        json={
            "agent": "codex",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    non_stream = client.post(
        "/v1/agents/messages",
        json={
            "stream": False,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert unsupported.status_code == 400
    assert "Only agent 'claude'" in unsupported.json()["error"]["message"]
    assert non_stream.status_code == 400
    assert "requires stream=true" in non_stream.json()["error"]["message"]
    assert backend.create_kwargs is None


def test_effort_is_forwarded_to_create_client(monkeypatch, tmp_path):
    backend = _FakeBackend(
        [{"type": "result", "subtype": "success", "is_error": False, "result": "ok"}]
    )
    _install_endpoint_fakes(monkeypatch, tmp_path, backend)

    response = TestClient(main.app).post(
        "/v1/agents/messages",
        json={"effort": "max", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert backend.create_kwargs["effort"] == "max"


def test_effort_defaults_to_none_and_unknown_level_is_rejected(monkeypatch, tmp_path):
    backend = _FakeBackend(
        [{"type": "result", "subtype": "success", "is_error": False, "result": "ok"}]
    )
    _install_endpoint_fakes(monkeypatch, tmp_path, backend)
    client = TestClient(main.app)

    ok = client.post(
        "/v1/agents/messages",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    bad = client.post(
        "/v1/agents/messages",
        json={"effort": "minimal", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert ok.status_code == 200
    assert backend.create_kwargs["effort"] is None
    assert bad.status_code == 422


def test_partial_message_override_does_not_change_default_responses_behavior():
    with (
        patch(
            "src.auth.validate_claude_code_auth",
            return_value=(True, {"method": "test"}),
        ),
        patch("src.auth.auth_manager") as auth_manager,
    ):
        auth_manager.get_claude_code_env_vars.return_value = {}
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI(cwd="/tmp")

    with patch("src.runtime_config.get_token_streaming", return_value=False):
        default_options = backend._build_sdk_options(_custom_base=None)
        endpoint_options = backend._build_sdk_options(
            _custom_base=None,
            include_partial_messages=True,
        )

    assert default_options.include_partial_messages is False
    assert endpoint_options.include_partial_messages is True


async def test_cancelled_stream_interrupts_and_stops_the_turn(monkeypatch, tmp_path):
    """Client abort mid-turn must stop the CLI turn, not just the HTTP stream.

    The cancelled generator unwind cannot await reliably (a further pending
    cancellation aborts the first await in its finally), so the teardown runs
    in a detached task: interrupt → disconnect → cleanup, in that order.
    """
    import asyncio
    from types import SimpleNamespace

    import pytest

    from src.agent_message_models import AgentMessagesRequest

    class _HangingBackend(_FakeBackend):
        async def run_completion_with_client(self, client, prompt, session):
            self.run_session = session
            n = 0
            while True:
                n += 1
                yield {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": f"chunk {n} "},
                    },
                }
                await asyncio.sleep(0.005)

    backend = _HangingBackend([])
    backend.client.interrupt = AsyncMock()
    workspace, _ = _install_endpoint_fakes(monkeypatch, tmp_path, backend)

    body = AgentMessagesRequest(messages=[{"role": "user", "content": "멈춰볼 턴"}])
    resolved = SimpleNamespace(backend="claude", provider_model="sonnet")
    stream = agent_messages._stream_agent_messages(body, resolved, backend)
    received = []

    async def consume():
        async for chunk in stream:
            received.append(chunk)

    task = asyncio.create_task(consume())
    while len(received) < 3:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The detached teardown finishes on its own; cleanup is its LAST step.
    for _ in range(200):
        if workspace.cleaned:
            break
        await asyncio.sleep(0.01)

    backend.client.interrupt.assert_awaited_once()
    backend.client.disconnect.assert_awaited_once()
    assert workspace.cleaned == [workspace.path]
