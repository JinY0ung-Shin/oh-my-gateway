"""P0 black-box probes for the optional official Codex Python SDK.

Run intentionally with a released SDK, for example:

    uv run --with openai-codex pytest tests/p0/test_python_sdk_transport.py -q -rxX

The project does not depend on openai-codex in production or normal dev installs.
Known upstream failure classes are strict xfails so a fixed SDK becomes XPASS and
forces the P0 decision record to be revisited.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

import pytest
from pydantic import BaseModel

pytest.importorskip("openai_codex", reason="P0 probe: run with `uv run --with openai-codex ...`")

from openai_codex import CodexConfig, TransportClosedError  # noqa: E402
from openai_codex.client import CodexClient  # noqa: E402

FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_app_server.py"


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


@pytest.mark.integration
@pytest.mark.xfail(
    reason="openai/codex#41078: early turn/completed is dropped before turn registration",
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
    executor = ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        response = client.request("turn/start", {}, response_model=ProbeResponse)
        assert response.turnId == "turn-1"
        client.register_turn_notifications("turn-1")
        future = executor.submit(client.next_turn_notification, "turn-1")
        notification = future.result(timeout=0.5)
        assert notification.method == "turn/completed"
    finally:
        client.close()
        if future is not None:
            try:
                future.result(timeout=1)
            except BaseException:
                pass
        executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.integration
@pytest.mark.xfail(
    reason="openai/codex#40399: reads arriving after terminal transport failure can block forever",
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
    executor = ThreadPoolExecutor(max_workers=1)
    second_read = None
    try:
        with pytest.raises(TransportClosedError):
            client.request("probe", {}, response_model=ProbeResponse)

        with pytest.raises(TransportClosedError):
            client.next_notification()

        second_read = executor.submit(client.next_notification)
        with pytest.raises(TransportClosedError):
            second_read.result(timeout=0.5)
    finally:
        client.close()
        if second_read is not None:
            try:
                second_read.result(timeout=1)
            except (BaseException, FutureTimeout):
                pass
        executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "B0 hazard: sync approval handler owns the sole reader, so child death is not observed "
        "until the human future returns"
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
    executor = ThreadPoolExecutor(max_workers=1)
    request = executor.submit(client.request, "probe", {}, response_model=ProbeResponse)
    try:
        approval_seen.result(timeout=1)
        with pytest.raises(TransportClosedError):
            request.result(timeout=0.5)
    finally:
        if not human_decision.done():
            human_decision.set_result({"decision": "decline"})
        client.close()
        try:
            request.result(timeout=1)
        except BaseException:
            pass
        executor.shutdown(wait=False, cancel_futures=True)
