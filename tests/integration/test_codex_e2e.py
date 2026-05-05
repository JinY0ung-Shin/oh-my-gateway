"""Codex backend end-to-end tests with a fake app-server process."""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import src.main as main
import src.routes.general as general_module
import src.routes.responses as responses_module
from src.backends.base import BackendRegistry


FAKE_CODEX_APP_SERVER = r"""#!/usr/bin/env python3
import json
import sys

thread_id = "thr_e2e"
turn_id = "turn_1"
approval_mode = "command"


def log(message):
    print(message, file=sys.stderr, flush=True)


def send(payload):
    log("OUT " + json.dumps(payload, sort_keys=True))
    print(json.dumps(payload), flush=True)


for raw in sys.stdin:
    log("IN " + raw.strip())
    msg = json.loads(raw)
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        send({"id": msg_id, "result": {"protocolVersion": "2"}})
        continue
    if method == "initialized":
        continue
    if method == "model/list":
        send({"id": msg_id, "result": {"data": [{"id": "gpt-5.5"}]}})
        continue
    if method == "thread/start":
        send({"id": msg_id, "result": {"thread": {"id": thread_id, "path": None, "ephemeral": True}}})
        continue
    if method == "thread/resume":
        send({"id": msg_id, "result": {"thread": {"id": params.get("threadId", thread_id)}}})
        continue
    if method == "turn/start":
        input_items = params.get("input") or []
        prompt = ""
        if input_items and isinstance(input_items[0], dict):
            prompt = input_items[0].get("text") or ""
        if "file change" in prompt:
            approval_mode = "file_change"
            approval_method = "item/fileChange/requestApproval"
            approval_params = {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "file_1",
                "grantRoot": params.get("cwd") or "/tmp",
                "reason": "e2e file approval",
            }
        elif "permissions" in prompt:
            approval_mode = "permissions"
            approval_method = "item/permissions/requestApproval"
            approval_params = {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "perm_1",
                "cwd": params.get("cwd") or "/tmp",
                "permissions": {"fileSystem": {"read": [params.get("cwd") or "/tmp"]}},
                "reason": "e2e permissions approval",
            }
        else:
            approval_mode = "command"
            approval_method = "item/commandExecution/requestApproval"
            approval_params = {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "cmd_1",
                "command": "printf e2e",
                "cwd": params.get("cwd") or "/tmp",
                "reason": "e2e approval",
                "availableDecisions": ["accept", "decline", "cancel"],
            }
        send({"id": msg_id, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        send({
            "id": "approval_1",
            "method": approval_method,
            "params": approval_params,
        })
        continue

    if msg_id == "approval_1":
        if approval_mode == "file_change":
            completed_item = {
                "type": "fileChange",
                "id": "file_1",
                "status": "completed",
                "changes": [{"path": "example.txt", "kind": "update"}],
            }
        elif approval_mode == "permissions":
            completed_item = {
                "type": "dynamicToolCall",
                "id": "perm_1",
                "status": "completed",
                "name": "permissions",
                "output": "permissions granted",
            }
        else:
            completed_item = {
                "type": "commandExecution",
                "id": "cmd_1",
                "command": "printf e2e",
                "cwd": "/tmp",
                "status": "completed",
                "exitCode": 0,
                "aggregatedOutput": "e2e",
                "commandActions": [],
            }
        send({
            "method": "serverRequest/resolved",
            "params": {"threadId": thread_id, "requestId": "approval_1"},
        })
        send({
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": completed_item,
            },
        })
        send({
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "id": "msg_1",
                    "phase": "final_answer",
                    "text": "Codex e2e approved.",
                },
            },
        })
        send({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "tokenUsage": {
                    "last": {"inputTokens": 2, "cachedInputTokens": 0, "outputTokens": 3}
                },
            },
        })
        send({
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed", "items": []},
            },
        })
        continue
"""


def _write_fake_codex(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fake-codex"
    fake_bin.write_text(FAKE_CODEX_APP_SERVER)
    fake_bin.chmod(0o755)
    return fake_bin


@contextmanager
def codex_client_context(fake_bin: Path):
    """Create a TestClient with the real Codex backend and fake app-server binary."""

    def _mock_discover():
        from src.backends.codex import CODEX_DESCRIPTOR
        from src.backends.codex.client import CodexClient

        BackendRegistry.register_descriptor(CODEX_DESCRIPTOR)
        BackendRegistry.register("codex", CodexClient(timeout=3000))

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()

    with (
        patch.dict(
            "os.environ",
            {
                "CODEX_BIN": str(fake_bin),
                "CODEX_MODELS": "gpt-5.5",
                "BACKENDS": "codex",
            },
            clear=False,
        ),
        patch.object(main, "discover_backends", _mock_discover),
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(general_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(main, "validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
    ):
        with TestClient(main.app) as client:
            yield client

    for backend in BackendRegistry.all_backends().values():
        close = getattr(backend, "close", None)
        if callable(close):
            close()
    BackendRegistry.clear()
    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


@pytest.mark.parametrize(
    ("prompt", "expected_kind"),
    [
        ("run a command", "command"),
        ("request file change approval", "file_change"),
        ("request permissions approval", "permissions"),
    ],
)
def test_codex_responses_e2e_approval_continuation(tmp_path, prompt, expected_kind):
    fake_bin = _write_fake_codex(tmp_path)

    with codex_client_context(fake_bin) as client:
        first = client.post(
            "/v1/responses",
            json={"model": "codex/gpt-5.5", "input": prompt, "stream": False},
        )

        assert first.status_code == 200
        first_body = first.json()
        assert first_body["status"] == "requires_action"
        tool_call = first_body["output"][0]
        assert tool_call["type"] == "function_call"
        assert tool_call["name"] == "AskUserQuestion"
        assert tool_call["call_id"] == "approval_1"
        arguments = json.loads(tool_call["arguments"])
        assert arguments["kind"] == expected_kind
        if expected_kind == "command":
            assert arguments["command"] == "printf e2e"
        elif expected_kind == "file_change":
            assert arguments["grantRoot"]
        else:
            assert arguments["permissions"]["fileSystem"]["read"]

        second = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "previous_response_id": first_body["id"],
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "approval_1",
                        "output": "accept",
                    }
                ],
                "stream": False,
            },
        )

        assert second.status_code == 200
        second_body = second.json()
        assert second_body["status"] == "completed"
        assert second_body["output"][0]["content"][0]["text"] == "Codex e2e approved."
        assert second_body["usage"] == {"input_tokens": 2, "output_tokens": 3}


def test_codex_streaming_approval_exposes_only_ask_user_question(tmp_path):
    fake_bin = _write_fake_codex(tmp_path)

    with codex_client_context(fake_bin) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "input": "run a command",
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert '"status": "requires_action"' in response.text
    assert '"name": "AskUserQuestion"' in response.text
    assert "codex_approval" not in response.text
