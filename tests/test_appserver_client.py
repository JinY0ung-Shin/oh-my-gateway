"""Integration tests for the app-server-backed Codex ``BackendClient``.

These drive the real :class:`~src.backends.appserver.transport.AppServerTransport`
against the deterministic ``fake_app_server`` stdio adversary, through the
adapter's ``create_client`` / ``run_completion_with_client`` surface -- the same
path ``/v1/responses`` uses. They assert the adapter satisfies the internal
chunk contract (issue #173 PR A) end to end: handshake, thread/turn lifecycle,
canonical event mapping, the #165 durable-thread rule, cancellation, and
runtime-loss terminalization.

File/test/param names avoid the substring the stale-backend deselector matches,
so these run in the default suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.backends.appserver import client as adapter_client
from src.backends.appserver.client import (
    AppServerCodexClient,
    DESCRIPTOR,
    _resolve_model,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_app_server.py"

# Handshake steps every scenario begins with (initialize request + initialized
# notification), mirroring the transport's own test corpus.
HANDSHAKE_STEPS: List[Dict[str, Any]] = [
    {"expect_method": "initialize", "actions": [{"type": "response", "result": {}}]},
    {"expect_method": "initialized", "actions": []},
]

THREAD_START_STEP: Dict[str, Any] = {
    "expect_method": "thread/start",
    "actions": [{"type": "response", "result": {"thread": {"id": "thread-1"}}}],
}


def _message(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "message", "message": payload}


def _write_scenario(
    tmp_path: Path, steps: List[Dict[str, Any]], *, exit_code: int = 0
) -> Path:
    scenario = {"steps": steps, "linger": True, "exit_code": exit_code}
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    return path


def _install_argv(monkeypatch: pytest.MonkeyPatch, scenario: Path) -> None:
    monkeypatch.setattr(
        adapter_client,
        "app_server_argv",
        lambda: [sys.executable, str(FIXTURE), str(scenario)],
    )


def _session(**attrs: Any) -> SimpleNamespace:
    return SimpleNamespace(**attrs)


async def _collect(agen) -> List[Dict[str, Any]]:
    return [chunk async for chunk in agen]


# -- descriptor / resolution -------------------------------------------------


def test_descriptor_and_model_resolution():
    assert DESCRIPTOR.name == "codex"
    assert DESCRIPTOR.owned_by == "openai"
    assert DESCRIPTOR.capabilities == {"image_input": True}

    resolved = _resolve_model("codex/gpt-5.5")
    assert resolved is not None
    assert resolved.backend == "codex"
    assert resolved.provider_model == "gpt-5.5"
    assert _resolve_model("sonnet") is None


async def test_verify_reports_binary_availability(monkeypatch: pytest.MonkeyPatch):
    backend = AppServerCodexClient()
    monkeypatch.setattr(adapter_client, "app_server_argv", lambda: ["/no/such/bin"])
    monkeypatch.setattr(
        backend._auth_provider,
        "validate",
        lambda: {"valid": False, "errors": ["x"], "config": {}},
    )
    assert await backend.verify() is False
    monkeypatch.setattr(backend._auth_provider, "validate", lambda: {"valid": True})
    assert await backend.verify() is True


# -- lifecycle ---------------------------------------------------------------


async def test_create_client_starts_thread_without_seeding_durable_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    scenario = _write_scenario(tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP])
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(
        session=session, model="gpt-5.5", cwd=str(tmp_path)
    )
    try:
        assert handle.thread_id == "thread-1"
        # #165: the durable marker is NOT written on thread/start.
        assert getattr(session, "codex_thread_id", None) is None
    finally:
        await handle.disconnect()


async def test_full_text_turn_maps_to_canonical_chunks_and_seeds_durable_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            _message(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"turnId": "turn-1", "delta": "Hi"},
                }
            ),
            _message(
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-1",
                        "item": {
                            "type": "agentMessage",
                            "id": "m1",
                            "phase": "final_answer",
                            "text": "Hi there",
                        },
                    },
                }
            ),
            _message(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "turnId": "turn-1",
                        "tokenUsage": {"last": {"inputTokens": 3, "outputTokens": 2}},
                    },
                }
            ),
            _message(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                }
            ),
        ],
    }
    scenario = _write_scenario(
        tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step]
    )
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(session=session, model="gpt-5.5")
    try:
        chunks = await _collect(
            backend.run_completion_with_client(handle, "hello", session)
        )
    finally:
        await handle.disconnect()

    # A visible text delta was mapped.
    assert any(
        c.get("type") == "stream_event"
        and c["event"].get("delta", {}).get("type") == "text_delta"
        and c["event"]["delta"]["text"] == "Hi"
        for c in chunks
    )
    # The turn terminated with a success result carrying final text + usage.
    result = chunks[-1]
    assert result["type"] == "result"
    assert result["subtype"] == "success"
    assert result["result"] == "Hi there"
    assert result["usage"] == {"input_tokens": 3, "output_tokens": 2}
    # #165: durable thread id is seeded only after the turn completes.
    assert session.codex_thread_id == "thread-1"
    # parse_message reads the same terminal chunk (non-stream/background path).
    assert backend.parse_message(chunks) == "Hi there"


async def test_tool_turn_maps_tool_use_and_tool_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            _message(
                {
                    "method": "item/started",
                    "params": {
                        "turnId": "turn-1",
                        "item": {
                            "type": "commandExecution",
                            "id": "c1",
                            "command": "ls",
                        },
                    },
                }
            ),
            _message(
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-1",
                        "item": {
                            "type": "commandExecution",
                            "id": "c1",
                            "status": "completed",
                            "exitCode": 0,
                            "aggregatedOutput": "file.txt\n",
                        },
                    },
                }
            ),
            _message(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                }
            ),
        ],
    }
    scenario = _write_scenario(
        tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step]
    )
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(session=session)
    try:
        chunks = await _collect(
            backend.run_completion_with_client(handle, "run ls", session)
        )
    finally:
        await handle.disconnect()

    tool_use = next(
        b
        for c in chunks
        if c.get("type") == "assistant"
        for b in c.get("content", [])
        if b.get("type") == "tool_use"
    )
    assert tool_use["name"] == "commandExecution"
    assert tool_use["id"] == "c1"
    tool_result = next(
        b
        for c in chunks
        if c.get("type") == "user"
        for b in c.get("content", [])
        if b.get("type") == "tool_result"
    )
    assert tool_result["tool_use_id"] == "c1"
    assert tool_result["content"] == "file.txt\n"
    assert tool_result["is_error"] is False


async def test_turn_failure_maps_to_error_chunk_and_no_durable_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            _message(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "id": "turn-1",
                            "status": "failed",
                            "error": {"message": "nope"},
                        }
                    },
                }
            ),
        ],
    }
    scenario = _write_scenario(
        tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step]
    )
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(session=session)
    try:
        chunks = await _collect(
            backend.run_completion_with_client(handle, "hi", session)
        )
    finally:
        await handle.disconnect()

    assert chunks[-1] == {"type": "error", "is_error": True, "error_message": "nope"}
    # A failed turn must not seed a resume marker.
    assert getattr(session, "codex_thread_id", None) is None


async def test_cancellation_emits_gateway_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            _message(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                }
            ),
        ],
    }
    scenario = _write_scenario(
        tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step]
    )
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    # The route flips this to "cancelling" when POST /cancel lands.
    session = _session(active_response_state="cancelling")

    handle = await backend.create_client(session=session)
    try:
        chunks = await _collect(
            backend.run_completion_with_client(handle, "hi", session)
        )
    finally:
        await handle.disconnect()

    terminal = chunks[-1]
    assert terminal.get("gateway_interrupted") is True
    assert terminal["subtype"] == "error_during_execution"
    # An interrupted turn is not a clean completion, so no durable id.
    assert getattr(session, "codex_thread_id", None) is None


async def test_runtime_loss_terminalizes_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            {"type": "exit", "code": 3},
        ],
    }
    # No linger: the process exits mid-turn.
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(
        json.dumps(
            {"steps": HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step], "linger": False}
        ),
        encoding="utf-8",
    )
    _install_argv(monkeypatch, scenario_path)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(session=session)
    try:
        chunks = await _collect(
            backend.run_completion_with_client(handle, "hi", session)
        )
    finally:
        await handle.disconnect()

    assert chunks[-1]["type"] == "error"
    assert chunks[-1]["is_error"] is True
    assert "terminated" in chunks[-1]["error_message"]


async def test_resume_uses_durable_thread_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    resume_step = {
        "expect_method": "thread/resume",
        "actions": [{"type": "response", "result": {"thread": {"id": "thread-1"}}}],
    }
    scenario = _write_scenario(tmp_path, HANDSHAKE_STEPS + [resume_step])
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    # A prior completed turn left a durable id on the session.
    session = _session(codex_thread_id="thread-1")

    handle = await backend.create_client(session=session)
    try:
        assert handle.thread_id == "thread-1"
    finally:
        await handle.disconnect()


# -- interaction bridge (PR B) ----------------------------------------------


def _approval_request(request_id: str = "approval-1") -> Dict[str, Any]:
    return _message(
        {
            "id": request_id,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "command": "ls -la",
                "availableDecisions": ["accept", "decline"],
            },
        }
    )


async def test_interaction_parks_as_askuserquestion_then_resumes_same_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            _approval_request(),
        ],
    }
    # The approval answer ({"id":"approval-1","result":{"decision":"accept"}}) is
    # the next stdin line; after it, the turn continues to completion.
    answer_step = {
        "expect_id": "approval-1",
        "expect_has_id": True,
        "actions": [
            _message(
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-1",
                        "item": {
                            "type": "agentMessage",
                            "id": "m1",
                            "phase": "final_answer",
                            "text": "listed",
                        },
                    },
                }
            ),
            _message(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                }
            ),
        ],
    }
    scenario = _write_scenario(
        tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step, answer_step]
    )
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(session=session)
    try:
        first = await _collect(
            backend.run_completion_with_client(handle, "run ls", session)
        )
        # The turn parked: a codex_approval tool_use chunk was emitted and the
        # route-facing pending_tool_call is set for requires_action.
        approval_block = first[-1]["content"][0]
        assert approval_block["name"] == "codex_approval"
        assert approval_block["metadata"]["codex_approval_request_id"] == "approval-1"
        assert session.pending_tool_call["name"] == "AskUserQuestion"
        assert session.pending_tool_call["call_id"] == "approval-1"
        assert session.pending_tool_call["codex_resume"] == "approval"
        # The subscription persisted across the pause (turn still live).
        assert handle._subscription is not None

        # Answer -> continues the SAME turn to completion.
        second = await _collect(
            backend.resume_approval_with_client(handle, "approval-1", "accept", session)
        )
        assert second[-1]["type"] == "result"
        assert second[-1]["result"] == "listed"
        assert session.codex_thread_id == "thread-1"
        # The turn is fully torn down after completion.
        assert handle._subscription is None
    finally:
        await handle.disconnect()


async def test_resume_with_mismatched_call_id_is_deterministic_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            _approval_request(),
        ],
    }
    scenario = _write_scenario(
        tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step]
    )
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(session=session)
    try:
        await _collect(backend.run_completion_with_client(handle, "run ls", session))
        chunks = await _collect(
            backend.resume_approval_with_client(handle, "wrong-id", "accept", session)
        )
        assert chunks[-1]["type"] == "error"
        assert "mismatch" in chunks[-1]["error_message"]
    finally:
        await handle.disconnect()


async def test_upstream_resolved_interaction_answer_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            _approval_request(),
            # Codex resolves the request itself before the user answers.
            _message(
                {
                    "method": "serverRequest/resolved",
                    "params": {"threadId": "thread-1", "requestId": "approval-1"},
                }
            ),
        ],
    }
    scenario = _write_scenario(
        tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step]
    )
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(session=session)
    try:
        await _collect(backend.run_completion_with_client(handle, "run ls", session))
        # Give the reader a moment to process the resolved notification.
        chunks = await _collect(
            backend.resume_approval_with_client(handle, "approval-1", "accept", session)
        )
        assert chunks[-1]["type"] == "error"
        assert "no longer actionable" in chunks[-1]["error_message"]
    finally:
        await handle.disconnect()


async def test_route_aclose_after_park_keeps_turn_live_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The route calls aclose() on the run_completion generator right after a
    park (to end the requires_action stream). That must not tear down the live
    turn -- the resume needs the same subscription."""
    turn_step = {
        "expect_method": "turn/start",
        "actions": [
            {
                "type": "response",
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            },
            _approval_request(),
        ],
    }
    answer_step = {
        "expect_id": "approval-1",
        "expect_has_id": True,
        "actions": [
            _message(
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-1",
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "ok",
                        },
                    },
                }
            ),
            _message(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                }
            ),
        ],
    }
    scenario = _write_scenario(
        tmp_path, HANDSHAKE_STEPS + [THREAD_START_STEP, turn_step, answer_step]
    )
    _install_argv(monkeypatch, scenario)
    backend = AppServerCodexClient()
    session = _session()

    handle = await backend.create_client(session=session)
    try:
        # Drive until the park chunk, then aclose the generator like the route.
        agen = backend.run_completion_with_client(handle, "run ls", session)
        park_chunk = None
        async for chunk in agen:
            park_chunk = chunk
            if (
                isinstance(chunk, dict)
                and chunk.get("content")
                and chunk["content"][0].get("name") == "codex_approval"
            ):
                await agen.aclose()
                break
        assert park_chunk["content"][0]["name"] == "codex_approval"
        # Subscription survived the aclose.
        assert handle._subscription is not None

        resumed = await _collect(
            backend.resume_approval_with_client(handle, "approval-1", "accept", session)
        )
        assert resumed[-1]["result"] == "ok"
    finally:
        await handle.disconnect()
