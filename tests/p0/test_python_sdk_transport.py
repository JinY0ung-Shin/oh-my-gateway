"""P0 black-box probes for the optional official Codex Python SDK.

Run intentionally with a released SDK, for example:

    uv run --with openai-codex pytest tests/p0/test_python_sdk_transport.py -q -rxX

The project does not depend on openai-codex in production or normal dev installs.
Known upstream failure classes are strict xfails so a fixed SDK becomes XPASS and
forces the P0 decision record to be revisited.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Callable, TypeVar

import pytest
from pydantic import BaseModel

pytest.importorskip(
    "openai_codex", reason="P0 probe: run with `uv run --with openai-codex ...`"
)

from openai_codex import CodexConfig, TransportClosedError  # noqa: E402
from openai_codex.client import CodexClient  # noqa: E402

FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_app_server.py"
T = TypeVar("T")


class ProbeResponse(BaseModel):
    turnId: str | None = None


def _scenario(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _client(tmp_path: Path, payload: dict, *, approval_handler=None) -> CodexClient:
    scenario = _scenario(tmp_path, payload)
    config = CodexConfig(
        launch_args_override=(sys.executable, str(FIXTURE), str(scenario)),
    )
    client = CodexClient(config=config, approval_handler=approval_handler)
    client.start()
    return client


def _start_daemon_call(fn: Callable[[], T]) -> queue.Queue[tuple[str, object]]:
    result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            result.put(("result", fn()))
        except BaseException as exc:
            result.put(("error", exc))

    threading.Thread(target=_target, daemon=True).start()
    return result


def _await_call(result: queue.Queue[tuple[str, object]], timeout: float = 0.5):
    try:
        kind, value = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError(f"call did not terminate within {timeout}s") from exc
    if kind == "error":
        raise value  # type: ignore[misc]
    return value


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "openai/codex#41078: early turn/completed is dropped before "
        "turn registration"
    ),
    strict=True,
)
def test_sdk_retains_terminal_notification_emitted_before_turn_start_response(tmp_path):
    client = _client(
        tmp_path,
        {
            "steps": [
                {
                    "expect_method": "turn/start",
                    "actions": [
                        {
                            "type": "message",
                            "message": {
                                "method": "turn/completed",
                                "params": {
                                    "threadId": "thread-1",
                                    "turn": {
                                        "id": "turn-1",
                                        "status": "completed",
                                        "items": [],
                                    },
                                },
                            },
                        },
                        {"type": "response", "result": {"turnId": "turn-1"}},
                    ],
                }
            ],
            "linger": True,
        },
    )
    try:
        response = client.request("turn/start", {}, response_model=ProbeResponse)
        assert response.turnId == "turn-1"
        client.register_turn_notifications("turn-1")
        pending = _start_daemon_call(lambda: client.next_turn_notification("turn-1"))
        notification = _await_call(pending)
        assert notification.method == "turn/completed"
    finally:
        client.close()


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "openai/codex#40399: reads arriving after terminal transport failure "
        "can block forever"
    ),
    strict=True,
)
def test_sdk_subsequent_global_read_fails_after_transport_death(tmp_path):
    client = _client(
        tmp_path,
        {
            "steps": [
                {
                    "expect_method": "probe",
                    "actions": [{"type": "exit", "code": 23}],
                }
            ]
        },
    )
    try:
        with pytest.raises(TransportClosedError):
            client.request("probe", {}, response_model=ProbeResponse)

        with pytest.raises(TransportClosedError):
            client.next_notification()

        pending = _start_daemon_call(client.next_notification)
        with pytest.raises(TransportClosedError):
            _await_call(pending)
    finally:
        client.close()


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "B0 hazard: sync approval handler owns the sole reader, so child death "
        "is not observed until the human future returns"
    ),
    strict=True,
)
def test_sdk_detects_runtime_death_while_approval_handler_is_parked(tmp_path):
    approval_seen: Future[None] = Future()
    human_decision: Future[dict] = Future()

    def approval_handler(_method, _params):
        if not approval_seen.done():
            approval_seen.set_result(None)
        return human_decision.result(timeout=30)

    client = _client(
        tmp_path,
        {
            "steps": [
                {
                    "expect_method": "probe",
                    "actions": [
                        {
                            "type": "message",
                            "message": {
                                "id": "approval-1",
                                "method": "item/commandExecution/requestApproval",
                                "params": {
                                    "threadId": "thread-1",
                                    "turnId": "turn-1",
                                    "itemId": "item-1",
                                    "command": "echo probe",
                                },
                            },
                        },
                        {"type": "exit", "code": 23},
                    ],
                }
            ]
        },
        approval_handler=approval_handler,
    )
    request = _start_daemon_call(
        lambda: client.request("probe", {}, response_model=ProbeResponse)
    )
    try:
        approval_seen.result(timeout=1)
        with pytest.raises(TransportClosedError):
            _await_call(request)
    finally:
        if not human_decision.done():
            human_decision.set_result({"decision": "decline"})
        client.close()
