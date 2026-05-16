"""Codex backend end-to-end tests with a fake app-server process."""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import src.admin_auth as admin_auth_module
import src.main as main
import src.routes.general as general_module
import src.routes.responses as responses_module
from src.backends.base import BackendRegistry


FAKE_CODEX_APP_SERVER = r"""#!/usr/bin/env python3
import json
import os
import sys

thread_id = "thr_e2e"
turn_id = "turn_1"
approval_mode = "command"
# Tests can point FAKE_CODEX_TURN_START_LOG at a file to capture every
# turn/start payload as JSONL for later assertions.
turn_start_log = os.environ.get("FAKE_CODEX_TURN_START_LOG")


def log(message):
    print(message, file=sys.stderr, flush=True)


def record_turn_start(params):
    if not turn_start_log:
        return
    try:
        with open(turn_start_log, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(params, sort_keys=True) + "\n")
    except OSError:
        pass


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
        record_turn_start(params)
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
        decision = ""
        result_payload = msg.get("result") or {}
        if isinstance(result_payload, dict):
            decision_field = result_payload.get("decision")
            if isinstance(decision_field, str):
                decision = decision_field
        if decision == "decline":
            send({
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "type": "agentMessage",
                        "id": "msg_decline",
                        "phase": "final_answer",
                        "text": "Codex stopped: approval was declined.",
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
                    "last": {
                        "inputTokens": 2,
                        "cachedInputTokens": 0,
                        "outputTokens": 3,
                        "reasoningOutputTokens": 2,
                    }
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
def codex_client_context(fake_bin: Path, extra_env: dict | None = None):
    """Create a TestClient with the real Codex backend and fake app-server binary."""

    def _mock_discover():
        from src.backends.codex import CODEX_DESCRIPTOR
        from src.backends.codex.client import CodexClient

        BackendRegistry.register_descriptor(CODEX_DESCRIPTOR)
        BackendRegistry.register("codex", CodexClient(timeout=3000))

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()

    env_overrides = {
        "CODEX_BIN": str(fake_bin),
        "CODEX_MODELS": "gpt-5.5",
        "BACKENDS": "codex",
    }
    if extra_env:
        env_overrides.update(extra_env)

    with (
        patch.dict("os.environ", env_overrides, clear=False),
        patch.object(admin_auth_module, "ADMIN_API_KEY", "test-admin-key"),
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
        # Reasoning tokens (2) roll into output (3) for OpenAI-compatible usage reporting.
        assert second_body["usage"] == {"input_tokens": 2, "output_tokens": 5}


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


def test_codex_global_disallowed_tools_blocks_command_approval_e2e(tmp_path):
    """DISALLOWED_TOOLS=Bash auto-denies commandExecution approvals at the route level."""
    fake_bin = _write_fake_codex(tmp_path)

    with codex_client_context(fake_bin, extra_env={"DISALLOWED_TOOLS": "Bash"}) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "codex/gpt-5.5", "input": "run a command", "stream": False},
        )

    assert response.status_code == 200
    body = response.json()
    # No requires_action: the approval was auto-denied and the turn completed without prompting.
    assert body["status"] == "completed"
    # The fake server's decline branch emits this exact final message.
    assert body["output"][0]["content"][0]["text"] == "Codex stopped: approval was declined."


def test_codex_request_body_disallowed_tools_blocks_command_approval_e2e(tmp_path):
    """Per-request ``disallowed_tools`` in the Responses API body reaches Codex enforcement."""
    fake_bin = _write_fake_codex(tmp_path)

    with codex_client_context(fake_bin) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "input": "run a command",
                "stream": False,
                "disallowed_tools": ["Bash"],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "Codex stopped: approval was declined."


def test_codex_accept_edits_auto_accepts_file_change_e2e(tmp_path):
    """permission_mode='acceptEdits' on the Responses body auto-accepts fileChange.

    With acceptEdits, the fake's fileChange approval flow runs to completion
    without surfacing a requires_action response.
    """
    fake_bin = _write_fake_codex(tmp_path)

    with codex_client_context(fake_bin) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "input": "request file change approval",
                "stream": False,
                "permission_mode": "acceptEdits",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "Codex e2e approved."


def test_codex_request_body_model_params_flow_through_to_turn_start_e2e(tmp_path):
    """temperature / max_output_tokens from the body reach Codex turn/start params.

    The fake codex app-server logs every received turn/start payload to a file
    pointed to by FAKE_CODEX_TURN_START_LOG. The test then reads the file and
    confirms the gateway forwarded the sampling overrides under the expected
    Codex keys.
    """
    fake_bin = _write_fake_codex(tmp_path)
    turn_log = tmp_path / "turn-start.jsonl"

    with codex_client_context(
        fake_bin, extra_env={"FAKE_CODEX_TURN_START_LOG": str(turn_log)}
    ) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "input": "request file change approval",
                "stream": False,
                "temperature": 0.25,
                "max_output_tokens": 64,
                # acceptEdits lets the fake's fileChange branch run to
                # completion so the e2e finishes cleanly.
                "permission_mode": "acceptEdits",
            },
        )

    assert response.status_code == 200

    assert turn_log.exists(), "fake codex did not record any turn/start params"
    lines = [json.loads(line) for line in turn_log.read_text().splitlines() if line.strip()]
    assert lines, "turn/start log is empty"
    first = lines[0]
    assert first.get("temperature") == 0.25
    assert first.get("maxOutputTokens") == 64


def test_codex_continuation_request_switches_permission_mode_to_accept_edits(tmp_path):
    """A continuation request can flip permission_mode to acceptEdits and have the next turn auto-accept fileChange.

    Scenario:
      1. First request: permission_mode default-equivalent (bypass) -> fake codex emits a command
         approval -> requires_action.
      2. Second request: function_call_output 'accept' to advance the first turn ->
         turn completes.
      3. Third request: same session, ``permission_mode='acceptEdits'`` + prompt that
         routes the fake to fileChange approval. The route's update_request_policy
         must propagate the new mode so the gateway auto-accepts fileChange
         without surfacing a requires_action response.
    """
    fake_bin = _write_fake_codex(tmp_path)

    with codex_client_context(fake_bin) as client:
        first = client.post(
            "/v1/responses",
            json={"model": "codex/gpt-5.5", "input": "run a command", "stream": False},
        )
        assert first.status_code == 200
        assert first.json()["status"] == "requires_action"
        first_body = first.json()

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
        assert second.json()["status"] == "completed"

        third = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "previous_response_id": second.json()["id"],
                "input": "request file change approval",
                "stream": False,
                "permission_mode": "acceptEdits",
            },
        )

    assert third.status_code == 200
    third_body = third.json()
    assert third_body["status"] == "completed"
    # The fake's "approved" branch fires when our auto-accept reaches it.
    assert third_body["output"][0]["content"][0]["text"] == "Codex e2e approved."


def test_codex_continuation_request_refreshes_disallowed_tools(tmp_path):
    """A continuation request's disallowed_tools applies even when the session client already exists.

    Scenario:
      1. First request has no tool policy -> Codex emits approval -> requires_action.
      2. Second request (continuation) provides function_call_output "accept" -> turn completes.
      3. Third request (new turn, same session) adds ``disallowed_tools=['Bash']``.
         The gateway must auto-deny that turn's command approval instead of
         silently dropping the new policy.
    """
    fake_bin = _write_fake_codex(tmp_path)

    with codex_client_context(fake_bin) as client:
        first = client.post(
            "/v1/responses",
            json={"model": "codex/gpt-5.5", "input": "run a command", "stream": False},
        )
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["status"] == "requires_action"

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
        assert second.json()["status"] == "completed"

        third = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "previous_response_id": second.json()["id"],
                "input": "run another command",
                "stream": False,
                "disallowed_tools": ["Bash"],
            },
        )

    assert third.status_code == 200
    third_body = third.json()
    assert third_body["status"] == "completed"
    assert third_body["output"][0]["content"][0]["text"] == "Codex stopped: approval was declined."
