"""P0 black-box probes for the optional official Codex Python SDK (#163, B0).

Run with the exact pinned SDK and the stale-backend opt-in switch:

    RUN_STALE_BACKEND_TESTS=1 uv run --with 'openai-codex==0.147.0' \
      pytest tests/p0/test_codex_p0_python_sdk_transport.py -q -rxX

The project does not depend on openai-codex in production or normal dev installs,
and this file is named ``test_codex_*`` so the repo's stale-backend deselection
keeps it out of the default suite even when the package happens to be installed.

Known upstream failure classes are strict xfails with ``raises=TimeoutError``, so
an unrelated failure fails the test instead of masquerading as the upstream bug,
and a fixed SDK becomes XPASS(strict) and forces the P0 decision record to be
revisited. Every known-hang probe has a positive control on the same scenario
machinery that must pass; if a control fails the harness is broken and the
neighbouring xfail carries no information.
"""

from __future__ import annotations

import importlib.metadata
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
    "openai_codex",
    reason="P0 probe: run with `uv run --with 'openai-codex==0.147.0' ...`",
)

from openai_codex import CodexConfig, TransportClosedError  # noqa: E402
from openai_codex.client import CodexClient  # noqa: E402

FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_app_server.py"
T = TypeVar("T")

# The version this corpus certifies. Results against any other version must not
# be recorded as B0 evidence for 0.147.0; ``test_sdk_version_matches_pin`` fails
# loudly on drift while the other probes still run, so a re-evaluation on a
# newer SDK shows its XPASSes next to one unambiguous version-drift failure.
PINNED_SDK_VERSION = "0.147.0"

# Deadline for the *should-eventually-arrive* direction. A healthy SDK answers in
# milliseconds and only the genuinely-buggy path pays the full wait, so a generous
# bound cannot turn an upstream fix into a false xfail under CI load.
HEALTHY_DEADLINE_S = 8.0


class ProbeResponse(BaseModel):
    turnId: str | None = None


# ---------------------------------------------------------------------------
# harness helpers
# ---------------------------------------------------------------------------


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
    """Run a potentially blocking SDK call off the main thread.

    Every SDK read in this file goes through here, including the first read
    after a transport death: openai/codex#40399 is precisely that such a read
    can block forever, and a blocked main thread would wedge pytest itself.
    """
    result: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            result.put(("result", fn()))
        except BaseException as exc:
            result.put(("error", exc))

    threading.Thread(target=_target, daemon=True).start()
    return result


def _await_call(
    result: queue.Queue[tuple[str, object]], timeout: float = HEALTHY_DEADLINE_S
):
    try:
        kind, value = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError(f"call did not terminate within {timeout}s") from exc
    if kind == "error":
        raise value  # type: ignore[misc]
    return value


# ``turn_start()`` validates its response as ``TurnStartResponse`` and reads
# ``started.turn.id``; the model requires ``turn.items``. Anything less fails
# validation and would surface as a *false* xfail if the xfail did not pin
# ``raises=TimeoutError``. Keep the shape here so every turn/start scenario
# satisfies the real model.
TURN_START_RESULT = {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}
TURN_COMPLETED = {
    "type": "message",
    "message": {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "completed", "items": []},
        },
    },
}


def _turn_start_scenario(*, terminal_first: bool, gap_s: float = 0.0) -> dict:
    """turn/start scenario.

    ``terminal_first`` reproduces the #41078 wire ordering (terminal event before
    the RPC response). ``gap_s`` inserts a pause between the response and the
    terminal event: ``turn_start()`` registers the turn queue only *after* the
    response has been processed on the calling thread, so a notification written
    immediately after the response can still reach the reader before
    registration and be dropped. A positive control therefore needs a gap wide
    enough for registration to happen; the zero-gap case is its own probe.
    """
    response = {"type": "response", "result": TURN_START_RESULT}
    if terminal_first:
        actions = [TURN_COMPLETED, response]
    elif gap_s > 0:
        actions = [response, {"type": "sleep", "seconds": gap_s}, TURN_COMPLETED]
    else:
        actions = [response, TURN_COMPLETED]
    return {
        "steps": [{"expect_method": "turn/start", "actions": actions}],
        "linger": True,
    }


APPROVAL_REQUEST = {
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
}


def _approval_then_exit_scenario() -> dict:
    return {
        "steps": [
            {
                "expect_method": "probe",
                "actions": [APPROVAL_REQUEST, {"type": "exit", "code": 23}],
            }
        ]
    }


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sdk_version_matches_pin():
    """Refuse to certify results against an unintended SDK version."""
    installed = importlib.metadata.version("openai-codex")
    assert installed == PINNED_SDK_VERSION, (
        f"installed openai-codex {installed} != certified {PINNED_SDK_VERSION}; "
        "run with `uv run --with 'openai-codex==0.147.0'` or update the pin and "
        "the decision record together"
    )


# ---------------------------------------------------------------------------
# openai/codex#41078 — terminal notification before the turn/start response
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sdk_turn_start_control_safe_ordering(tmp_path):
    """Control for the #41078 probes: response first, then the terminal event.

    Must pass. Proves the fixture shape satisfies ``TurnStartResponse`` and that
    the routing path works when nothing pathological happens.
    """
    client = _client(tmp_path, _turn_start_scenario(terminal_first=False, gap_s=0.5))
    try:
        started = client.turn_start("thread-1", "probe")
        assert started.turn.id == "turn-1"
        pending = _start_daemon_call(
            lambda: client.next_turn_notification(started.turn.id)
        )
        assert _await_call(pending).method == "turn/completed"
    finally:
        client.close()


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "openai/codex#41078 (registration window): turn_start() registers the "
        "queue after processing the response, so a terminal event written "
        "immediately after the response -- correct wire order -- is still dropped"
    ),
    raises=TimeoutError,
    strict=True,
)
def test_sdk_turn_start_loses_terminal_notification_written_right_after_response(tmp_path):
    """Correct wire ordering, zero gap: still lost.

    This is a sharper statement of #41078 than "notification before response".
    The loss window is the interval between the response reaching the reader
    and ``register_turn_notifications`` running on the calling thread, so any
    notification a fast server emits in that window is dropped even though it
    arrives after the response on the wire. Discovered when the safe-ordering
    control initially failed with no gap.
    """
    client = _client(tmp_path, _turn_start_scenario(terminal_first=False, gap_s=0.0))
    try:
        started = client.turn_start("thread-1", "probe")
        assert started.turn.id == "turn-1"
        pending = _start_daemon_call(
            lambda: client.next_turn_notification(started.turn.id)
        )
        assert _await_call(pending).method == "turn/completed"
    finally:
        client.close()


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "openai/codex#41078: turn_start() registers the turn queue after the "
        "turn/start response, so an earlier turn/completed is dropped"
    ),
    raises=TimeoutError,
    strict=True,
)
def test_sdk_turn_start_retains_terminal_notification_emitted_before_response(tmp_path):
    """Probe #41078 through the public ``turn_start()`` helper.

    The registration ordering that causes the loss lives inside ``turn_start()``
    (released 0.147.0 still calls ``request`` then ``register_turn_notifications``
    despite a docstring promising early registration). An upstream fix landing
    inside the helper is only observable by calling the helper.
    """
    client = _client(tmp_path, _turn_start_scenario(terminal_first=True))
    try:
        started = client.turn_start("thread-1", "probe")
        assert started.turn.id == "turn-1"
        pending = _start_daemon_call(
            lambda: client.next_turn_notification(started.turn.id)
        )
        assert _await_call(pending).method == "turn/completed"
    finally:
        client.close()


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "openai/codex#41078 (router variant): a notification for a turn that is "
        "not yet registered is dropped rather than buffered"
    ),
    raises=TimeoutError,
    strict=True,
)
def test_sdk_raw_request_retains_terminal_notification_emitted_before_response(tmp_path):
    """Second data point: raw ``request()`` + manual registration.

    Kept alongside the helper probe because the two isolate different fixes —
    a fix inside ``turn_start()`` flips only the helper probe, a router-level
    buffer flips both.
    """
    client = _client(tmp_path, _turn_start_scenario(terminal_first=True))
    try:
        response = client.request("turn/start", {}, response_model=ProbeResponse)
        assert response.turnId is None  # ProbeResponse ignores the real shape
        client.register_turn_notifications("turn-1")
        pending = _start_daemon_call(lambda: client.next_turn_notification("turn-1"))
        assert _await_call(pending).method == "turn/completed"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# openai/codex#40399 — reads after terminal transport failure
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "openai/codex#40399: reads arriving after terminal transport failure "
        "can block forever"
    ),
    raises=TimeoutError,
    strict=True,
)
def test_sdk_subsequent_global_read_fails_after_transport_death(tmp_path):
    client = _client(
        tmp_path,
        {"steps": [{"expect_method": "probe", "actions": [{"type": "exit", "code": 23}]}]},
    )
    try:
        # Every read here is deadline-isolated, including the first post-death
        # one: the reported defect is exactly that a read after the first
        # failure item is drained never returns.
        first = _start_daemon_call(
            lambda: client.request("probe", {}, response_model=ProbeResponse)
        )
        with pytest.raises(TransportClosedError):
            _await_call(first)

        second = _start_daemon_call(client.next_notification)
        with pytest.raises(TransportClosedError):
            _await_call(second)

        third = _start_daemon_call(client.next_notification)
        with pytest.raises(TransportClosedError):
            _await_call(third)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# B0 hazard — synchronous approval handler owns the sole reader
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sdk_control_runtime_death_detected_when_handler_does_not_park(tmp_path):
    """Control for the parked probe: identical scenario, handler returns at once.

    Must pass. With the handler not parking, the reader proceeds to its next
    read, hits EOF, and fails every waiter promptly. Together with the parked
    probe below this is the causal proof that the *park* is what delays death
    detection, not the SDK in general.
    """
    approval_seen: Future[None] = Future()

    def approval_handler(_method, _params):
        if not approval_seen.done():
            approval_seen.set_result(None)
        return {"decision": "decline"}

    client = _client(
        tmp_path, _approval_then_exit_scenario(), approval_handler=approval_handler
    )
    request = _start_daemon_call(
        lambda: client.request("probe", {}, response_model=ProbeResponse)
    )
    try:
        approval_seen.result(timeout=HEALTHY_DEADLINE_S)
        with pytest.raises(TransportClosedError):
            _await_call(request)
    finally:
        client.close()


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "B0 hazard: sync approval handler owns the sole reader, so child death "
        "is not observed until the human future returns"
    ),
    raises=TimeoutError,
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
        tmp_path, _approval_then_exit_scenario(), approval_handler=approval_handler
    )
    request = _start_daemon_call(
        lambda: client.request("probe", {}, response_model=ProbeResponse)
    )
    try:
        approval_seen.result(timeout=HEALTHY_DEADLINE_S)
        # The bound here is the runtime-health interval, deliberately far below
        # the 30 s human TTL the handler is parked on. Detection only when the
        # human future resolves is the failure this probe exists to record.
        with pytest.raises(TransportClosedError):
            _await_call(request, timeout=2.0)
    finally:
        if not human_decision.done():
            human_decision.set_result({"decision": "decline"})
        client.close()


@pytest.mark.integration
def test_sdk_parked_approval_does_not_block_unrelated_session(tmp_path):
    approval_seen: Future[None] = Future()
    human_decision: Future[dict] = Future()

    def approval_handler(_method, _params):
        if not approval_seen.done():
            approval_seen.set_result(None)
        return human_decision.result(timeout=30)

    session_a_dir = tmp_path / "session-a"
    session_a_dir.mkdir()
    client_a = _client(
        session_a_dir,
        {
            "steps": [
                {
                    "expect_method": "probe",
                    "actions": [
                        {
                            "type": "message",
                            "message": {
                                "id": "approval-a",
                                "method": "item/commandExecution/requestApproval",
                                "params": {"threadId": "thread-a", "turnId": "turn-a"},
                            },
                        }
                    ],
                }
            ],
            "linger": True,
        },
        approval_handler=approval_handler,
    )
    request_a = _start_daemon_call(
        lambda: client_a.request("probe", {}, response_model=ProbeResponse)
    )

    session_b_dir = tmp_path / "session-b"
    session_b_dir.mkdir()
    client_b = _client(
        session_b_dir,
        {
            "steps": [
                {
                    "expect_method": "probe",
                    "actions": [{"type": "response", "result": {"turnId": "turn-b"}}],
                }
            ]
        },
    )
    try:
        approval_seen.result(timeout=HEALTHY_DEADLINE_S)
        response_b = _await_call(
            _start_daemon_call(
                lambda: client_b.request("probe", {}, response_model=ProbeResponse)
            )
        )
        assert response_b.turnId == "turn-b"
    finally:
        if not human_decision.done():
            human_decision.set_result({"decision": "decline"})
        client_a.close()
        client_b.close()
        try:
            _await_call(request_a, timeout=0.5)
        except BaseException:
            pass


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "B0 hazard: turn/interrupt response cannot be routed while the sole reader "
        "is blocked inside approval_handler"
    ),
    raises=TimeoutError,
    strict=True,
)
def test_sdk_interrupt_progresses_while_approval_handler_is_parked(tmp_path):
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
                                "params": {"threadId": "thread-1", "turnId": "turn-1"},
                            },
                        }
                    ],
                },
                {
                    "expect_method": "turn/interrupt",
                    "actions": [{"type": "response", "result": {"turnId": "turn-1"}}],
                },
            ],
            "linger": True,
        },
        approval_handler=approval_handler,
    )
    request = _start_daemon_call(
        lambda: client.request("probe", {}, response_model=ProbeResponse)
    )
    try:
        approval_seen.result(timeout=HEALTHY_DEADLINE_S)
        interrupt = _start_daemon_call(
            lambda: client.request(
                "turn/interrupt",
                {"threadId": "thread-1", "turnId": "turn-1"},
                response_model=ProbeResponse,
            )
        )
        # Bounded by the runtime-health interval, not the human TTL.
        result = _await_call(interrupt, timeout=2.0)
        assert result.turnId == "turn-1"
    finally:
        if not human_decision.done():
            human_decision.set_result({"decision": "decline"})
        client.close()
        try:
            _await_call(request, timeout=0.5)
        except BaseException:
            pass
