"""Acceptance corpus for the direct app-server transport (C0 core; #163, #170).

These are the invariants the public Python SDK failed in the pinned B0 run
(#166), re-run against the direct transport on the same schema-light stdio
adversary. Every "must arrive" wait is bounded by ``HEALTHY_S``; every "must
progress while something is parked" wait by ``CONTROL_S``, which is far below
any human interaction TTL.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path

import pytest

from src.backends.appserver import (
    OWNER_CLOSED,
    AmbiguousRequest,
    RUNTIME_LOST,
    SERVER_RESOLVED,
    AppServerTransport,
    HandshakeError,
    Notification,
    OrphanedResponse,
    PendingInteraction,
    RequestOutcomeUnknown,
    RpcError,
    RuntimeLost,
    StaleAnswer,
    SubscriberOverflow,
    TerminalEvent,
)

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "fake_app_server.py"
HEALTHY_S = 8.0
CONTROL_S = 2.0

# Every scenario starts with the production handshake so the transport is
# exercised in the state a real run is in.
HANDSHAKE_STEPS = [
    {
        "expect_method": "initialize",
        "actions": [{"type": "response", "result": {"userAgent": "fake-app-server"}}],
    },
    {"expect_method": "initialized", "actions": []},
]

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
TURN_START_RESPONSE = {
    "type": "response",
    "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}},
}
APPROVAL_REQUEST = {
    "type": "message",
    "message": {
        "id": "approval-1",
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1"},
    },
}


def _scenario(
    tmp_path: Path, steps: list, *, linger: bool = True, handshake: bool = True
) -> Path:
    payload = {
        "steps": (HANDSHAKE_STEPS if handshake else []) + steps,
        "linger": linger,
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def _transport(tmp_path: Path, steps: list, **kwargs) -> AppServerTransport:
    scenario = _scenario(tmp_path, steps)
    transport = AppServerTransport(
        [sys.executable, str(FIXTURE), str(scenario)], **kwargs
    )
    await transport.start()
    return transport


async def _next(queue, timeout: float = HEALTHY_S):
    return await asyncio.wait_for(queue.get(), timeout)


async def _next_of(queue, kind: type, timeout: float = HEALTHY_S):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"no {kind.__name__} within {timeout}s")
        item = await asyncio.wait_for(queue.get(), remaining)
        if isinstance(item, kind):
            return item


# ---------------------------------------------------------------------------
# handshake and routing
# ---------------------------------------------------------------------------


async def test_handshake_then_request_roundtrip(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": {"ok": True}}],
            }
        ],
    )
    try:
        assert await transport.request("probe", timeout=HEALTHY_S) == {"ok": True}
        assert transport.alive
    finally:
        report = await transport.close()
    assert report["pending_waiters"] == 0
    assert report["running_descendants"] == 0


async def test_rpc_error_response_is_raised_to_the_waiter_only(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {"type": "response", "error": {"code": -32000, "message": "nope"}}
                ],
            },
            {"expect_method": "probe", "actions": [{"type": "response", "result": 1}]},
        ],
    )
    try:
        with pytest.raises(RpcError) as info:
            await transport.request("probe", timeout=HEALTHY_S)
        assert info.value.code == -32000
        # The transport is still alive and routing after an RPC error.
        assert await transport.request("probe", timeout=HEALTHY_S) == 1
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# 1. no notification-loss window (openai/codex#41078 class)
# ---------------------------------------------------------------------------


async def test_notification_emitted_before_response_is_delivered(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "turn/start",
                "actions": [TURN_COMPLETED, TURN_START_RESPONSE],
            }
        ],
    )
    try:
        events = transport.subscribe()
        result = await transport.request(
            "turn/start", {"threadId": "thread-1"}, timeout=HEALTHY_S
        )
        assert result["turn"]["id"] == "turn-1"
        note = await _next_of(events, Notification)
        assert note.method == "turn/completed"
        assert note.params["turn"]["id"] == "turn-1"
    finally:
        await transport.close()


async def test_notification_written_right_after_response_is_delivered(tmp_path):
    """The SDK's loss window: response processed, notification arrives before
    the caller registers a turn queue. Subscribers here are process-wide and
    ordered, so there is no window to fall into."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "turn/start",
                "actions": [TURN_START_RESPONSE, TURN_COMPLETED],
            }
        ],
    )
    try:
        events = transport.subscribe()
        await transport.request(
            "turn/start", {"threadId": "thread-1"}, timeout=HEALTHY_S
        )
        note = await _next_of(events, Notification)
        assert note.method == "turn/completed"
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# 2 + 8. process death: bounded terminal state for current AND later reads,
#         fanned out to every waiter exactly once (openai/codex#40399 class)
# ---------------------------------------------------------------------------


async def test_death_fails_every_concurrent_waiter_exactly_once(tmp_path):
    waiters = 5
    steps = [{"expect_method": "probe", "actions": []} for _ in range(waiters - 1)]
    steps.append({"expect_method": "probe", "actions": [{"type": "exit", "code": 23}]})
    transport = await _transport(tmp_path, steps)
    events = transport.subscribe()
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            *(transport.request("probe", {"n": n}) for n in range(waiters)),
            return_exceptions=True,
        ),
        HEALTHY_S,
    )
    assert len(outcomes) == waiters
    assert all(isinstance(outcome, RuntimeLost) for outcome in outcomes), outcomes
    assert {outcome.reason for outcome in outcomes} <= {"eof", "exit"}
    assert all(outcome.exit_code == 23 for outcome in outcomes)
    terminal = await _next_of(events, TerminalEvent)
    assert terminal.exit_code == 23
    # Exactly one terminal event per subscriber, and nothing after it.
    await asyncio.sleep(0.2)
    assert events.empty()
    assert transport.pending_waiters == 0
    report = await transport.close()
    assert report["pending_waiters"] == 0


async def test_reads_after_death_terminate_immediately(tmp_path):
    transport = await _transport(
        tmp_path, [{"expect_method": "probe", "actions": [{"type": "exit", "code": 7}]}]
    )
    with pytest.raises(RuntimeLost):
        await transport.request("probe", timeout=HEALTHY_S)
    await transport.wait_terminal(timeout=HEALTHY_S)
    for _ in range(3):
        started = asyncio.get_running_loop().time()
        with pytest.raises(RuntimeLost) as info:
            await transport.request("probe")
        assert asyncio.get_running_loop().time() - started < CONTROL_S
        assert info.value.exit_code == 7
    with pytest.raises(RuntimeLost):
        await transport.notify("noop")
    # A subscriber that arrives after the loss still learns about it.
    late = transport.subscribe()
    assert isinstance(await _next(late), TerminalEvent)
    await transport.close()


# ---------------------------------------------------------------------------
# 3. a parked interaction blocks nothing: control traffic keeps flowing
# ---------------------------------------------------------------------------


async def test_interrupt_progresses_while_interaction_is_parked(tmp_path):
    human: asyncio.Future = asyncio.get_running_loop().create_future()
    seen: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handler(interaction: PendingInteraction):
        seen.set_result(interaction)
        return await human  # parks until the "human" answers

    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST]},
            {
                "expect_method": "turn/interrupt",
                "actions": [{"type": "response", "result": {"turnId": "turn-1"}}],
            },
            {"expect_id": "approval-1", "actions": []},
        ],
        interaction_handler=handler,
    )
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await asyncio.wait_for(seen, HEALTHY_S)
        assert interaction.open and interaction.generation == transport.generation
        # The reader is free while the handler parks: the interrupt round-trips
        # within the control bound, and notifications would too.
        result = await transport.interrupt("thread-1", "turn-1", timeout=CONTROL_S)
        assert result == {"turnId": "turn-1"}
        assert not human.done()
        human.set_result({"decision": "decline"})
        await asyncio.sleep(0.2)
        assert interaction.state == "answered"
    finally:
        probe.cancel()
        report = await transport.close()
    assert report["pending_interactions"] == 0


# ---------------------------------------------------------------------------
# 4 + 5. death invalidates every pending interaction immediately; late and
#        wrong-generation answers never reach a runtime
# ---------------------------------------------------------------------------


async def test_death_while_parked_invalidates_interaction_and_rejects_late_answer(
    tmp_path,
):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [APPROVAL_REQUEST, {"type": "exit", "code": 23}],
            }
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    assert interaction.method == "item/commandExecution/requestApproval"
    terminal = await _next_of(events, TerminalEvent, timeout=CONTROL_S)
    assert terminal.exit_code == 23
    # Invalidated at the moment of loss -- not when a human eventually answers.
    assert interaction.state == "invalidated"
    assert interaction.invalidation_reason == RUNTIME_LOST
    assert transport.pending_interactions == []
    with pytest.raises(StaleAnswer):
        await transport.answer(
            interaction.id,
            {"decision": "accept"},
            generation=transport.generation,
            token=interaction.token,
        )
    with pytest.raises(RuntimeLost):
        await probe
    await transport.close()


async def test_wrong_generation_answer_is_rejected_before_reaching_runtime(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST]},
            {
                "expect_id": "approval-1",
                "actions": [
                    {
                        "type": "message",
                        "message": {"method": "probe/answered", "params": {}},
                    }
                ],
            },
        ],
        generation=2,
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        assert interaction.generation == 2
        # An answer minted against the previous process generation.
        with pytest.raises(StaleAnswer):
            await transport.answer(
                interaction.id,
                {"decision": "accept"},
                generation=1,
                token=interaction.token,
            )
        assert interaction.open
        # The correct generation goes through, exactly once.
        await transport.answer(
            interaction.id,
            {"decision": "decline"},
            generation=2,
            token=interaction.token,
        )
        ack = await _next_of(events, Notification)
        assert ack.method == "probe/answered"
        with pytest.raises(StaleAnswer):
            await transport.answer(
                interaction.id,
                {"decision": "decline"},
                generation=2,
                token=interaction.token,
            )
    finally:
        probe.cancel()
        await transport.close()


async def test_answer_after_close_is_stale(tmp_path):
    transport = await _transport(
        tmp_path, [{"expect_method": "probe", "actions": [APPROVAL_REQUEST]}]
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    probe.cancel()
    report = await transport.close()
    assert report["pending_interactions"] == 0
    assert interaction.state == "invalidated"
    with pytest.raises(StaleAnswer):
        await transport.answer(
            interaction.id,
            {"decision": "accept"},
            generation=transport.generation,
            token=interaction.token,
        )


# ---------------------------------------------------------------------------
# 6. unsupported server requests fail closed and the reader stays alive
# ---------------------------------------------------------------------------


async def test_unsupported_server_request_fails_closed_and_reader_survives(tmp_path):
    handled: list = []

    async def handler(interaction: PendingInteraction):
        handled.append(interaction)
        return {"decision": "accept"}

    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {
                        "type": "message",
                        "message": {
                            "id": "sr-1",
                            "method": "item/experimental/requestSomething",
                            "params": {},
                        },
                    }
                ],
            },
            # The fixture exits non-zero unless the transport answered sr-1 with
            # an *error*; a permissive result would desync the scenario here.
            {
                "expect_id": "sr-1",
                "expect_has_error": True,
                "actions": [
                    {
                        "type": "message",
                        "message": {"method": "probe/rejected", "params": {}},
                    }
                ],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "still-alive"}],
            },
        ],
        interaction_handler=handler,
    )
    events = transport.subscribe()
    first = asyncio.create_task(transport.request("probe"))
    try:
        rejected = await _next_of(events, Notification)
        assert rejected.method == "probe/rejected"
        assert handled == []
        assert transport.pending_interactions == []
        assert transport.rejected_server_requests == [
            {"id": "sr-1", "method": "item/experimental/requestSomething"}
        ]
        assert await transport.request("probe", timeout=HEALTHY_S) == "still-alive"
    finally:
        first.cancel()
        await transport.close()


# ---------------------------------------------------------------------------
# protocol error: the stream stopped speaking JSON-RPC
# ---------------------------------------------------------------------------


async def test_malformed_output_terminalizes_every_waiter(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": []},
            {
                "expect_method": "probe",
                "actions": [{"type": "malformed", "text": "{not json"}],
            },
        ],
    )
    events = transport.subscribe()
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            transport.request("probe"),
            transport.request("probe"),
            return_exceptions=True,
        ),
        HEALTHY_S,
    )
    assert all(
        isinstance(o, RuntimeLost) and o.reason == "protocol_error" for o in outcomes
    ), outcomes
    terminal = await _next_of(events, TerminalEvent, timeout=CONTROL_S)
    assert terminal.reason == "protocol_error"
    report = await transport.close()
    assert report["running_descendants"] == 0


# ---------------------------------------------------------------------------
# 7. teardown: nothing pending, nothing running, and the same answer twice
# ---------------------------------------------------------------------------


async def test_close_reaps_the_whole_process_group(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [{"type": "spawn", "seconds": 120}, APPROVAL_REQUEST],
            },
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    assert interaction.open
    # Open waiter, open interaction, a 120 s descendant, a lingering fixture.
    report = await transport.close(grace_s=1.0)
    assert report["pending_waiters"] == 0
    assert report["pending_interactions"] == 0
    assert report["running_descendants"] == 0, report
    assert report["exit_code"] is not None
    with pytest.raises(RuntimeLost) as info:
        await probe
    assert info.value.reason == "closed"
    assert isinstance(
        await _next_of(events, TerminalEvent, timeout=CONTROL_S), TerminalEvent
    )
    # Idempotent: the second close returns the identical report and does no work.
    assert await transport.close() == report


# ---------------------------------------------------------------------------
# upstream-retired interactions are non-actionable at once (serverRequest/resolved)
# ---------------------------------------------------------------------------

RESOLVED_APPROVAL = {
    "type": "message",
    "message": {
        "method": "serverRequest/resolved",
        "params": {"threadId": "thread-1", "requestId": "approval-1"},
    },
}


async def test_server_resolved_after_interrupt_retires_interaction(tmp_path):
    """Mirrors upstream's turn_interrupt ordering: approval request ->
    turn/interrupt -> serverRequest/resolved for that id -> the turn ends.
    A late UI answer must be rejected and the fixture must never see a
    response for the retired id (it would desync the scenario)."""
    human: asyncio.Future = asyncio.get_running_loop().create_future()
    cancelled: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handler(interaction: PendingInteraction):
        try:
            return await human
        except asyncio.CancelledError:
            cancelled.set_result(interaction.id)
            raise

    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST]},
            {
                "expect_method": "turn/interrupt",
                "actions": [
                    RESOLVED_APPROVAL,
                    {"type": "response", "result": {"turnId": "turn-1"}},
                ],
            },
            # Any stray response for approval-1 would land here instead and
            # desync the adversary (exit 64) -> the follow-up probe would fail.
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "no-stray-write"}],
            },
        ],
        interaction_handler=handler,
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        assert interaction.open
        await transport.interrupt("thread-1", "turn-1", timeout=CONTROL_S)
        resolved = await _next_of(events, Notification, timeout=CONTROL_S)
        assert resolved.method == "serverRequest/resolved"
        # Retired at the moment upstream said so, not when the human answers.
        assert interaction.state == "resolved"
        assert interaction.invalidation_reason == SERVER_RESOLVED
        assert transport.pending_interactions == []
        assert await asyncio.wait_for(cancelled, CONTROL_S) == "approval-1"
        with pytest.raises(StaleAnswer):
            await transport.answer(
                interaction.id,
                {"decision": "accept"},
                generation=transport.generation,
                token=interaction.token,
            )
        with pytest.raises(StaleAnswer):
            await transport.fail_interaction(
                interaction.id, generation=transport.generation, token=interaction.token
            )
        assert await transport.request("probe", timeout=HEALTHY_S) == "no-stray-write"
        assert transport.alive
    finally:
        probe.cancel()
        await transport.close()


async def test_server_driven_resolution_without_interrupt_or_death(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [APPROVAL_REQUEST, RESOLVED_APPROVAL],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "no-stray-write"}],
            },
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        resolved = await _next_of(events, Notification, timeout=CONTROL_S)
        assert resolved.method == "serverRequest/resolved"
        assert interaction.state == "resolved"
        with pytest.raises(StaleAnswer):
            await transport.answer(
                interaction.id,
                {"decision": "accept"},
                generation=transport.generation,
                token=interaction.token,
            )
        assert await transport.request("probe", timeout=HEALTHY_S) == "no-stray-write"
    finally:
        probe.cancel()
        await transport.close()


async def test_resolution_for_unknown_request_id_is_harmless(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {
                        "type": "message",
                        "message": {
                            "method": "serverRequest/resolved",
                            "params": {
                                "threadId": "thread-1",
                                "requestId": "never-seen",
                            },
                        },
                    },
                    {"type": "response", "result": "ok"},
                ],
            }
        ],
    )
    try:
        assert await transport.request("probe", timeout=HEALTHY_S) == "ok"
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# process exit never beats the stdout drain: a flushed final response and a
# terminal notification are routed before the runtime is reported lost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", range(12))
async def test_final_response_and_event_before_exit_are_never_lost(tmp_path, attempt):
    # A payload large enough to sit in the pipe when the child exits.
    payload = "x" * 300_000
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {
                        "type": "response",
                        "result": {"payload": payload, "attempt": attempt},
                    },
                    TURN_COMPLETED,
                    {"type": "exit", "code": 0},
                ],
            }
        ],
    )
    events = transport.subscribe()
    result = await transport.request("probe", timeout=HEALTHY_S)
    assert result == {"payload": payload, "attempt": attempt}
    note = await _next_of(events, Notification)
    assert note.method == "turn/completed"
    terminal = await _next_of(events, TerminalEvent)
    assert terminal.reason == "exit"
    assert terminal.exit_code == 0
    await transport.close()


async def test_exit_with_stdout_held_open_by_descendant_still_terminalizes(tmp_path):
    """The one case where the watcher must act: the child exits but a
    descendant inherited stdout, so EOF never comes. Terminal state must
    still arrive within the bounded drain grace."""
    scenario = _scenario(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {"type": "response", "result": "before-exit"},
                    {"type": "spawn", "seconds": 120, "inherit_stdout": True},
                    {"type": "exit", "code": 5},
                ],
            }
        ],
    )
    transport = AppServerTransport(
        [sys.executable, str(FIXTURE), str(scenario)], exit_drain_grace_s=1.0
    )
    await transport.start()
    events = transport.subscribe()
    assert await transport.request("probe", timeout=HEALTHY_S) == "before-exit"
    terminal = await _next_of(events, TerminalEvent, timeout=HEALTHY_S)
    assert terminal.reason == "exit"
    assert terminal.exit_code == 5
    # Transport-owned teardown, without close(): the 120 s descendant is gone.
    await transport.wait_reaped(timeout=HEALTHY_S)
    assert transport.running_group_members() == []
    report = await transport.close()
    assert report["running_descendants"] == 0


# ---------------------------------------------------------------------------
# bounded handshake: a failed or silent initialize never leaks the runtime
# ---------------------------------------------------------------------------


def _group_is_gone(transport: AppServerTransport) -> bool:
    return transport._running_group_members() == []  # experiment-owned class


async def test_silent_initialize_fails_within_deadline_and_tears_down(tmp_path):
    scenario = _scenario(
        tmp_path,
        [
            {
                "expect_method": "initialize",
                "actions": [{"type": "spawn", "seconds": 120}],
            }
        ],
        handshake=False,
    )
    transport = AppServerTransport(
        [sys.executable, str(FIXTURE), str(scenario)], initialize_timeout_s=0.5
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(HandshakeError) as info:
        await transport.start()
    assert asyncio.get_running_loop().time() - started < CONTROL_S
    assert isinstance(info.value.cause, asyncio.TimeoutError)
    assert transport.exit_code is not None
    assert _group_is_gone(transport)
    assert all(task.done() for task in transport._tasks)
    assert transport.pending_waiters == 0
    with pytest.raises(RuntimeLost):
        await transport.request("probe")


async def test_initialize_rpc_error_raises_and_tears_down(tmp_path):
    scenario = _scenario(
        tmp_path,
        [
            {
                "expect_method": "initialize",
                "actions": [
                    {"type": "response", "error": {"code": -32000, "message": "no"}}
                ],
            }
        ],
        handshake=False,
    )
    transport = AppServerTransport([sys.executable, str(FIXTURE), str(scenario)])
    with pytest.raises(RpcError) as info:
        await transport.start()
    assert info.value.code == -32000
    assert transport.exit_code is not None
    assert _group_is_gone(transport)
    assert all(task.done() for task in transport._tasks)


async def test_initialize_cancelled_by_caller_tears_down(tmp_path):
    scenario = _scenario(
        tmp_path, [{"expect_method": "initialize", "actions": []}], handshake=False
    )
    transport = AppServerTransport([sys.executable, str(FIXTURE), str(scenario)])
    starter = asyncio.create_task(transport.start())
    await asyncio.sleep(0.3)
    starter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starter
    assert transport.exit_code is not None
    assert _group_is_gone(transport)


# ---------------------------------------------------------------------------
# exactly-once answer commit under caller cancellation
# ---------------------------------------------------------------------------


async def test_cancelled_answer_caller_never_leaves_answered_without_bytes(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST]},
            {
                "expect_id": "approval-1",
                "actions": [
                    {
                        "type": "message",
                        "message": {"method": "probe/answered", "params": {}},
                    }
                ],
            },
            # A duplicate response for approval-1 would land here and desync.
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "exactly-once"}],
            },
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        # Hold the writer so the answer parks before any byte is written...
        await transport._write_lock.acquire()
        caller = asyncio.create_task(
            transport.answer(
                interaction.id,
                {"decision": "decline"},
                generation=transport.generation,
                token=interaction.token,
            )
        )
        await asyncio.sleep(0.1)
        assert interaction.state == "resolving"
        # ...and cancel the caller while it is parked.
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        # The transport owns the commit: it is neither abandoned nor duplicated.
        assert interaction.state == "resolving"
        with pytest.raises(StaleAnswer):
            await transport.answer(
                interaction.id,
                {"decision": "accept"},
                generation=transport.generation,
                token=interaction.token,
            )
        transport._write_lock.release()
        await asyncio.wait_for(interaction.commit, CONTROL_S)
        assert interaction.state == "answered"
        ack = await _next_of(events, Notification, timeout=CONTROL_S)
        assert ack.method == "probe/answered"
        assert await transport.request("probe", timeout=HEALTHY_S) == "exactly-once"
    finally:
        probe.cancel()
        await transport.close()


async def test_answer_commit_records_runtime_loss(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    APPROVAL_REQUEST,
                    {"type": "sleep", "seconds": 0.3},
                    {"type": "exit", "code": 3},
                ],
            }
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    await transport._write_lock.acquire()
    caller = asyncio.create_task(
        transport.answer(
            interaction.id,
            {"decision": "decline"},
            generation=transport.generation,
            token=interaction.token,
        )
    )
    await _next_of(events, TerminalEvent)
    transport._write_lock.release()
    with pytest.raises(StaleAnswer):
        await caller
    assert interaction.state == "invalidated"
    assert interaction.invalidation_reason == RUNTIME_LOST
    with pytest.raises(RuntimeLost):
        await probe
    await transport.close()


# ---------------------------------------------------------------------------
# bounded subscribers: deltas droppable, lossless never silently dropped
# ---------------------------------------------------------------------------


def _delta(n: int) -> dict:
    return {
        "type": "message",
        "message": {
            "method": "item/agentMessage/delta",
            "params": {"delta": f"chunk-{n}"},
        },
    }


async def test_slow_subscriber_drops_deltas_but_keeps_lossless_items(tmp_path):
    lifecycle = {
        "type": "message",
        "message": {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}},
    }
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    *(_delta(n) for n in range(20)),
                    lifecycle,
                    APPROVAL_REQUEST,
                    {"type": "response", "result": "ok"},
                ],
            }
        ],
    )
    slow = transport.subscribe(max_pending=3)
    fast = transport.subscribe()
    try:
        assert await transport.request("probe", timeout=HEALTHY_S) == "ok"
        await asyncio.sleep(0.2)  # let the reader route everything before draining
        items = []
        while not slow.empty():
            items.append(slow.get_nowait())
        deltas = [
            i
            for i in items
            if isinstance(i, Notification) and i.method.endswith("/delta")
        ]
        assert len(deltas) == 3
        assert slow.dropped_deltas == 17
        assert any(
            isinstance(i, Notification) and i.method == "turn/completed" for i in items
        )
        assert any(isinstance(i, PendingInteraction) for i in items)
        assert not slow.disconnected
        fast_items = []
        while not fast.empty():
            fast_items.append(fast.get_nowait())
        assert (
            sum(
                isinstance(i, Notification) and i.method.endswith("/delta")
                for i in fast_items
            )
            == 20
        )
        assert fast.dropped_deltas == 0
    finally:
        await transport.close()


async def test_subscriber_far_behind_on_lossless_items_is_disconnected_not_grown(
    tmp_path,
):
    lifecycle = [
        {"type": "message", "message": {"method": "item/completed", "params": {"n": n}}}
        for n in range(10)
    ]
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [*lifecycle, {"type": "response", "result": "first"}],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "second"}],
            },
        ],
    )
    stalled = transport.subscribe(max_pending=1, hard_limit=4)
    try:
        assert await transport.request("probe", timeout=HEALTHY_S) == "first"
        await asyncio.sleep(0.2)
        items = []
        while not stalled.empty():
            items.append(stalled.get_nowait())
        assert stalled.disconnected
        assert isinstance(items[-1], SubscriberOverflow)
        assert items[-1].pending == 4
        assert (
            len(items) == 5
        )  # 4 retained lossless items + the overflow marker, nothing after
        # The reader and the other side of the transport are unaffected.
        assert await transport.request("probe", timeout=HEALTHY_S) == "second"
        assert stalled.empty()
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# bounded writes: a live runtime that stops reading stdin cannot pin a caller
# ---------------------------------------------------------------------------


async def test_write_blocked_by_stalled_reader_is_bounded_and_reaps(tmp_path):
    """Child completes the handshake, reads one request, then stops reading
    stdin for 30 s (a SIGSTOP / wedged-runtime stand-in). A payload larger
    than the pipe + stream buffer cannot drain; the request must fail within
    its deadline, the generation must terminalize, and the group must be
    reaped without close()."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {"type": "spawn", "seconds": 120},
                    {"type": "sleep", "seconds": 30},
                ],
            },
        ],
        write_timeout_s=0.5,
    )
    events = transport.subscribe()
    stalled = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.3)  # the child has read the line and is now asleep
    started = asyncio.get_running_loop().time()
    with pytest.raises(
        RequestOutcomeUnknown
    ) as info:  # bytes accepted, drain never completed
        await transport.request("probe", {"blob": "x" * 4_000_000}, timeout=HEALTHY_S)
    assert asyncio.get_running_loop().time() - started < CONTROL_S
    assert info.value.reason == "write_timeout" and info.value.method == "probe"
    # Every other waiter and later control call sees the same bounded state.
    with pytest.raises(RuntimeLost):
        await stalled
    with pytest.raises(RuntimeLost):
        await transport.interrupt("thread-1", "turn-1", timeout=CONTROL_S)
    terminal = await _next_of(events, TerminalEvent, timeout=CONTROL_S)
    assert terminal.reason == "write_timeout"
    await transport.wait_reaped(timeout=HEALTHY_S)
    assert transport.running_group_members() == []
    report = await transport.close()
    assert report["running_descendants"] == 0
    assert await transport.close() == report


async def test_interrupt_deadline_covers_write_and_response(tmp_path):
    """Small interrupt while the child is asleep: the write lands in the
    buffer, no response comes; the caller's deadline still bounds the whole
    operation and the transport stays alive for the runtime to recover."""
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [{"type": "sleep", "seconds": 1.0}]},
            {
                "expect_method": "turn/interrupt",
                "actions": [{"type": "response", "result": {"turnId": "turn-1"}}],
            },
        ],
    )
    stalled = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.2)
    started = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.TimeoutError):
        await transport.interrupt("thread-1", "turn-1", timeout=0.3)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert transport.alive
    stalled.cancel()
    await transport.close()


# ---------------------------------------------------------------------------
# unexpected loss reaps the owned group without waiting for close()
# ---------------------------------------------------------------------------


async def test_eof_from_live_root_reaps_group_without_close(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {"type": "response", "result": "last-words"},
                    {"type": "spawn", "seconds": 120},
                    {"type": "close_stdout"},
                ],
            }
        ],
        exit_drain_grace_s=0.5,
    )
    assert await transport.request("probe", timeout=HEALTHY_S) == "last-words"
    terminal = await transport.wait_terminal(timeout=HEALTHY_S)
    assert terminal.reason == "eof"
    assert terminal.exit_code is None  # the root was alive when stdout went away
    await transport.wait_reaped(timeout=HEALTHY_S)
    assert transport.running_group_members() == []
    assert transport.exit_code is not None
    report = await transport.close()
    assert report["running_descendants"] == 0
    assert await transport.close() == report


async def test_close_after_owner_initiated_shutdown_does_not_double_reap(tmp_path):
    transport = await _transport(
        tmp_path,
        [{"expect_method": "probe", "actions": [{"type": "spawn", "seconds": 120}]}],
    )
    probe = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.2)
    report = await transport.close()
    assert report["reason"] == "closed"
    assert report["running_descendants"] == 0
    assert (
        transport._reap_task is None
    )  # owner-initiated close never schedules loss teardown
    with pytest.raises(RuntimeLost):
        await probe
    await transport.wait_reaped(timeout=CONTROL_S)  # resolved by close(); must not hang


# ---------------------------------------------------------------------------
# two clocks: a caller's deadline queued behind a healthy writer is local;
# the transport health bound is judged against the writer that holds the lock
# ---------------------------------------------------------------------------


async def test_caller_deadline_behind_healthy_writer_is_local_not_a_kill(tmp_path):
    """A owns the writer for ~0.4 s (the child pauses reading), well under
    write_timeout_s. B arrives with a 50 ms deadline and cannot get the lock.
    B must time out locally with nothing written; A must complete; the same
    generation must keep serving."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {"type": "sleep", "seconds": 0.4},
                    {"type": "response", "result": "warm"},
                ],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "A"}],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "after"}],
            },
        ],
        write_timeout_s=5.0,
    )
    events = transport.subscribe()
    try:
        warm = asyncio.create_task(transport.request("probe"))
        await asyncio.sleep(
            0.1
        )  # the child has read "warm" and is asleep: nobody reads stdin
        a = asyncio.create_task(transport.request("probe", {"blob": "x" * 4_000_000}))
        await asyncio.sleep(0.05)  # A now owns the writer, blocked in drain()
        started = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.TimeoutError):
            await transport.request("probe", timeout=0.05)
        assert asyncio.get_running_loop().time() - started < 0.5
        # Local: the transport is alive, A finishes, the generation keeps serving.
        assert transport.alive
        assert await asyncio.wait_for(warm, HEALTHY_S) == "warm"
        assert await asyncio.wait_for(a, HEALTHY_S) == "A"
        assert await transport.request("probe", timeout=HEALTHY_S) == "after"
        assert transport.terminal is None
        assert not any(
            isinstance(events.get_nowait(), TerminalEvent)
            for _ in range(events.qsize())
        )
    finally:
        await transport.close()


async def test_caller_deadline_expiring_mid_drain_terminalizes(tmp_path):
    """Once bytes are in flight, a caller deadline during drain() leaves an
    ambiguous partial line: that is a generation-level loss, not a local one."""
    transport = await _transport(
        tmp_path,
        [{"expect_method": "probe", "actions": [{"type": "sleep", "seconds": 30}]}],
        write_timeout_s=10.0,
    )
    warm = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.2)
    with pytest.raises(
        RequestOutcomeUnknown
    ) as info:  # bytes accepted: ambiguous, not known-not-sent
        await transport.request("probe", {"blob": "x" * 4_000_000}, timeout=0.3)
    assert info.value.reason == "write_timeout"
    assert "caller deadline" in info.value.detail
    with pytest.raises(RuntimeLost):
        await warm
    await transport.wait_reaped(timeout=HEALTHY_S)
    assert transport.running_group_members() == []
    await transport.close()


async def test_contention_between_fast_writes_never_trips_health_bound(tmp_path):
    """Many small concurrent writes queue on the lock for longer in total than
    write_timeout_s, but no single holder is slow: no terminalization."""
    n = 40
    steps = [
        {"expect_method": "probe", "actions": [{"type": "response", "result": i}]}
        for i in range(n)
    ]
    transport = await _transport(tmp_path, steps, write_timeout_s=0.05)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    transport.request("probe", {"blob": "y" * 20_000, "i": i})
                    for i in range(n)
                )
            ),
            HEALTHY_S,
        )
        assert sorted(results) == list(range(n))
        assert transport.alive and transport.terminal is None
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# the WIRE is the boundary: serverRequest/resolved beats an answer that has
# not begun reaching the process, and never unsends one that has
# ---------------------------------------------------------------------------


async def test_server_resolved_beats_resolving_answer_before_wire_commit(tmp_path):
    """UI answer accepted locally (state=resolving) but queued behind the
    writer with zero bytes written; upstream then retires the request. The
    answer must be dropped, and the adversary must observe NO response for
    approval-1 after the writer frees."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    APPROVAL_REQUEST,
                    {"type": "sleep", "seconds": 0.5},
                    RESOLVED_APPROVAL,
                ],
            },
            # A stray response for approval-1 would land here and desync.
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "no-stray-write"}],
            },
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        await transport._write_lock.acquire()  # the writer is busy elsewhere
        caller = asyncio.create_task(
            transport.answer(
                interaction.id,
                {"decision": "accept"},
                generation=transport.generation,
                token=interaction.token,
            )
        )
        await asyncio.sleep(0.1)
        assert interaction.state == "resolving"
        assert not interaction.wire_committed
        resolved = await _next_of(events, Notification, timeout=CONTROL_S)
        assert resolved.method == "serverRequest/resolved"
        # Upstream won while the answer was still queued.
        assert interaction.state == "resolved"
        assert interaction.invalidation_reason == SERVER_RESOLVED
        transport._write_lock.release()
        with pytest.raises(StaleAnswer):
            await caller
        assert not interaction.wire_committed
        assert await transport.request("probe", timeout=HEALTHY_S) == "no-stray-write"
    finally:
        probe.cancel()
        await transport.close()


async def test_server_resolved_after_wire_commit_is_recorded_not_unsent(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST]},
            {"expect_id": "approval-1", "actions": [RESOLVED_APPROVAL]},
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "ok"}],
            },
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        await transport.answer(
            interaction.id,
            {"decision": "decline"},
            generation=transport.generation,
            token=interaction.token,
        )
        assert interaction.state == "answered" and interaction.wire_committed
        resolved = await _next_of(events, Notification, timeout=CONTROL_S)
        assert resolved.method == "serverRequest/resolved"
        assert interaction.state == "answered"
        assert interaction.resolved_after_commit
        assert await transport.request("probe", timeout=HEALTHY_S) == "ok"
    finally:
        probe.cancel()
        await transport.close()


# ---------------------------------------------------------------------------
# cancellation phases: queued = local; after bytes began = transport-owned
# ---------------------------------------------------------------------------


async def test_request_cancelled_while_queued_for_writer_is_local(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "later"}],
            }
        ],
    )
    try:
        await transport._write_lock.acquire()
        caller = asyncio.create_task(transport.request("probe"))
        await asyncio.sleep(0.1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        transport._write_lock.release()
        assert transport.alive
        assert transport._orphaned_requests == {}
        # Nothing was written: the adversary's first probe step is still unconsumed.
        assert await transport.request("probe", timeout=HEALTHY_S) == "later"
    finally:
        await transport.close()


async def test_request_cancelled_during_drain_completes_write_and_orphans_response(
    tmp_path,
):
    """The child pauses reading so a 4 MB request blocks in drain(); the
    caller is cancelled mid-drain. The writer must NOT be released early: the
    transport finishes the write, the request really reaches the child, and
    its response comes back as OrphanedResponse for the owner to reconcile."""
    payload = "x" * 4_000_000
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {"type": "sleep", "seconds": 0.6},
                    {"type": "response", "result": "warm"},
                ],
            },
            {"expect_method": "turn/start", "actions": [TURN_START_RESPONSE]},
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "after"}],
            },
        ],
        write_timeout_s=10.0,
    )
    events = transport.subscribe()
    try:
        warm = asyncio.create_task(transport.request("probe"))
        await asyncio.sleep(0.1)  # child has read "warm" and is asleep
        big = asyncio.create_task(
            transport.request("turn/start", {"threadId": "thread-1", "blob": payload})
        )
        await asyncio.sleep(0.1)  # big owns the writer, blocked in drain()
        assert transport._write_lock.locked()
        big.cancel()
        with pytest.raises(asyncio.CancelledError):
            await big
        # The writer is still held by the transport-owned committed write.
        assert transport._write_lock.locked()
        assert list(transport._orphaned_requests.values()) == ["turn/start"]
        # A concurrent small request queues behind it rather than interleaving.
        follow = asyncio.create_task(transport.request("probe"))
        assert await asyncio.wait_for(warm, HEALTHY_S) == "warm"
        orphan = await _next_of(events, OrphanedResponse)
        assert orphan.method == "turn/start"
        assert orphan.result["turn"]["id"] == "turn-1"
        assert transport._orphaned_requests == {}
        assert await asyncio.wait_for(follow, HEALTHY_S) == "after"
        assert transport.alive and transport.terminal is None
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# hardening: a duplicate live server-request id fails closed
# ---------------------------------------------------------------------------


async def test_duplicate_live_server_request_id_fails_closed(tmp_path):
    handled: list = []

    async def handler(interaction: PendingInteraction):
        handled.append(interaction.id)
        await asyncio.sleep(3600)  # parked

    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST, APPROVAL_REQUEST]},
            # The duplicate must be answered with an ERROR; the original stays live.
            {
                "expect_id": "approval-1",
                "expect_has_error": True,
                "actions": [
                    {
                        "type": "message",
                        "message": {"method": "probe/dup-rejected", "params": {}},
                    }
                ],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "alive"}],
            },
        ],
        interaction_handler=handler,
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        first = await _next_of(events, PendingInteraction)
        ack = await _next_of(events, Notification, timeout=CONTROL_S)
        assert ack.method == "probe/dup-rejected"
        assert transport.interaction("approval-1") is first and first.open
        assert handled == ["approval-1"]
        assert transport.rejected_server_requests[-1]["reason"] == "duplicate_live_id"
        assert await transport.request("probe", timeout=HEALTHY_S) == "alive"
    finally:
        probe.cancel()
        await transport.close()


# ---------------------------------------------------------------------------
# commit boundary: gate and first byte are indivisible; orphan ownership is
# race-complete for cancellation and for post-commit response timeouts
# ---------------------------------------------------------------------------


async def test_resolved_arriving_before_committed_task_runs_writes_nothing(
    tmp_path, monkeypatch
):
    """The window the marker used to hide: the answer is accepted, the writer
    is free, the committed task is scheduled but has not run yet, and
    `serverRequest/resolved(X)` arrives. The committed task is parked before
    its gate (monkeypatched to await a test-owned event) so the ordering is
    deterministic. Required: X retired, nothing written, adversary sees no
    response for X (it would desync on one)."""
    hold = asyncio.Event()
    hold.set()  # handshake and probe writes run normally
    original = AppServerTransport._committed_write

    async def parked(self, *args, **kwargs):
        await hold.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AppServerTransport, "_committed_write", parked)
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    APPROVAL_REQUEST,
                    {"type": "sleep", "seconds": 0.3},
                    RESOLVED_APPROVAL,
                ],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "no-stray-write"}],
            },
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    hold.clear()  # from here on committed tasks are scheduled but do not run
    caller = asyncio.create_task(
        transport.answer(
            interaction.id,
            {"decision": "accept"},
            generation=transport.generation,
            token=interaction.token,
        )
    )
    await asyncio.sleep(0.05)
    assert interaction.state == "resolving"
    assert not interaction.wire_committed
    assert (
        transport._write_lock.locked()
    )  # writer handed to the (parked) committed task
    resolved = await _next_of(events, Notification, timeout=CONTROL_S)
    assert resolved.method == "serverRequest/resolved"
    assert interaction.state == "resolved"  # upstream won: nothing was on the wire
    hold.set()  # now the committed task runs its gate
    with pytest.raises(StaleAnswer):
        await caller
    assert not interaction.wire_committed
    assert await transport.request("probe", timeout=HEALTHY_S) == "no-stray-write"
    probe.cancel()
    await transport.close()


async def test_response_that_beats_cancelled_caller_becomes_exactly_one_orphan(
    tmp_path,
):
    """Ordering: request committed -> cancellation requested -> the reader
    delivers the response BEFORE the cancelled task runs its handler. The
    response must surface as exactly one OrphanedResponse, with no orphan id
    left behind. The adversary sends no response of its own so the injected
    one is the only one in play."""
    transport = await _transport(
        tmp_path, [{"expect_method": "turn/start", "actions": []}]
    )
    events = transport.subscribe()
    task = asyncio.create_task(
        transport.request("turn/start", {"threadId": "thread-1"})
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if transport._waiters and not transport._write_lock.locked():
            break
    (request_id,) = transport._waiters.keys()
    task.cancel()
    # Same tick, before the cancelled task resumes: the reader routes the response.
    transport._dispatch({"id": request_id, "result": {"turn": {"id": "turn-1"}}})
    with pytest.raises(asyncio.CancelledError):
        await task
    orphan = await _next_of(events, OrphanedResponse, timeout=CONTROL_S)
    assert orphan.request_id == request_id and orphan.method == "turn/start"
    assert orphan.result == {"turn": {"id": "turn-1"}}
    assert transport._orphaned_requests == {}
    assert transport._waiters == {}
    await asyncio.sleep(0.1)
    assert not any(
        isinstance(events.get_nowait(), OrphanedResponse) for _ in range(events.qsize())
    )
    await transport.close()


async def test_response_timeout_after_committed_send_is_orphaned_not_dropped(tmp_path):
    """turn/start is sent cleanly; the caller's response deadline expires; the
    server completes it later. The owner must still receive exactly one
    OrphanedResponse -- a stateful call that really happened is never lost."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "turn/start",
                "actions": [{"type": "sleep", "seconds": 0.5}, TURN_START_RESPONSE],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "alive"}],
            },
        ],
    )
    events = transport.subscribe()
    with pytest.raises(asyncio.TimeoutError):
        await transport.request("turn/start", {"threadId": "thread-1"}, timeout=0.15)
    assert list(transport._orphaned_requests.values()) == ["turn/start"]
    assert transport.alive
    orphan = await _next_of(events, OrphanedResponse)
    assert orphan.method == "turn/start"
    assert orphan.result["turn"]["id"] == "turn-1"
    assert transport._orphaned_requests == {}
    assert await transport.request("probe", timeout=HEALTHY_S) == "alive"
    await transport.close()


async def test_rpc_error_for_orphaned_request_is_surfaced_as_error(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "turn/start",
                "actions": [
                    {"type": "sleep", "seconds": 0.4},
                    {
                        "type": "response",
                        "error": {"code": -32000, "message": "late no"},
                    },
                ],
            }
        ],
    )
    events = transport.subscribe()
    with pytest.raises(asyncio.TimeoutError):
        await transport.request("turn/start", {"threadId": "thread-1"}, timeout=0.15)
    orphan = await _next_of(events, OrphanedResponse)
    assert orphan.result is None and orphan.error["code"] == -32000
    await transport.close()


# ---------------------------------------------------------------------------
# owner close owns accepted answers: unsent ones lose (policy B), accepted
# bytes settle first; the report counts unsettled interactions
# ---------------------------------------------------------------------------


async def test_close_retires_queued_unsent_answer_and_owns_its_commit(tmp_path):
    """A owns the writer; answer(X) is accepted (resolving, zero bytes) and its
    commit queues for the lock; close() starts while A still holds the writer.
    Policy B: X is invalidated OWNER_CLOSED, its commit settles with nothing
    written, no owned task remains, and the adversary never sees a response
    for X -- it exits 64 ("stdin closed before step") rather than 77, which the
    scenario reserves for "a response for X arrived"."""
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST]},
            {"expect_id": "approval-1", "actions": [{"type": "exit", "code": 77}]},
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    await transport._write_lock.acquire()  # "A" owns the writer
    transport._writer_since = asyncio.get_running_loop().time()
    caller = asyncio.create_task(
        transport.answer(
            interaction.id,
            {"decision": "accept"},
            generation=transport.generation,
            token=interaction.token,
        )
    )
    await asyncio.sleep(0.1)
    assert interaction.state == "resolving"
    assert not interaction.wire_committed
    assert interaction.commit is not None and not interaction.commit.done()
    closing = asyncio.create_task(transport.close(grace_s=1.0))
    await asyncio.sleep(0.1)
    assert not closing.done()  # close is waiting on the owned commit
    transport._writer_since = None
    transport._write_lock.release()  # A finishes
    report = await asyncio.wait_for(closing, HEALTHY_S)
    with pytest.raises(StaleAnswer):
        await caller
    assert interaction.state == "invalidated"
    assert interaction.invalidation_reason == OWNER_CLOSED
    assert not interaction.wire_committed
    assert interaction.commit.done()
    assert transport._interaction_commits == set()
    assert report["pending_waiters"] == 0
    assert report["pending_interactions"] == 0
    assert report["unsettled_interaction_commits"] == 0
    assert report["running_descendants"] == 0
    assert (
        report["exit_code"] == 64
    )  # stdin EOF reached the adversary; no response for X ever did
    with pytest.raises(RuntimeLost):
        await probe
    assert await transport.close() == report


async def test_close_lets_accepted_answer_bytes_settle_before_shutdown(tmp_path):
    """The other branch: the answer's bytes are accepted (child paused reading,
    large payload mid-drain) when close() starts. The commit settles first and
    the adversary observes exactly that response (exit 77) before the runtime
    is shut down."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [APPROVAL_REQUEST, {"type": "sleep", "seconds": 0.5}],
            },
            {"expect_id": "approval-1", "actions": [{"type": "exit", "code": 77}]},
        ],
        write_timeout_s=10.0,
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    caller = asyncio.create_task(
        transport.answer(
            interaction.id,
            {"decision": "accept", "note": "x" * 4_000_000},
            generation=transport.generation,
            token=interaction.token,
        )
    )
    await asyncio.sleep(0.1)
    assert interaction.state == "resolving" and interaction.wire_committed
    assert transport._write_lock.locked()  # committed write mid-drain
    report = await asyncio.wait_for(transport.close(grace_s=1.0), HEALTHY_S)
    await caller
    assert interaction.state == "answered"
    assert report["pending_interactions"] == 0
    assert report["unsettled_interaction_commits"] == 0
    assert report["exit_code"] == 77  # the adversary read the answer before shutdown
    with pytest.raises(RuntimeLost):
        await probe


async def test_runtime_loss_retires_unsent_accepted_answer(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    APPROVAL_REQUEST,
                    {"type": "sleep", "seconds": 0.3},
                    {"type": "exit", "code": 9},
                ],
            }
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    await transport._write_lock.acquire()
    caller = asyncio.create_task(
        transport.answer(
            interaction.id,
            {"decision": "decline"},
            generation=transport.generation,
            token=interaction.token,
        )
    )
    await _next_of(events, TerminalEvent)
    assert interaction.state == "invalidated"
    assert interaction.invalidation_reason == RUNTIME_LOST
    transport._write_lock.release()
    with pytest.raises(StaleAnswer):
        await caller
    with pytest.raises(RuntimeLost):
        await probe
    report = await transport.close()
    assert (
        report["pending_interactions"] == 0
        and report["unsettled_interaction_commits"] == 0
    )


# ---------------------------------------------------------------------------
# close() is the admission barrier; concurrent closers share one teardown
# ---------------------------------------------------------------------------


async def test_close_admits_no_new_work_once_started(tmp_path):
    """close() is blocked (bounded) behind an in-flight committed write, so it
    has crossed its barrier but has not torn anything down. Work submitted in
    that window must be rejected with zero bytes: answer(X) -> StaleAnswer,
    request -> RuntimeLost("closing"). The adversary's catch-all step turns ANY
    further line into exit 77; stdin EOF alone is exit 64."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [APPROVAL_REQUEST, {"type": "sleep", "seconds": 0.6}],
            },
            {"expect_method": "turn/start", "actions": [TURN_START_RESPONSE]},
            {"actions": [{"type": "exit", "code": 77}]},  # anything else that arrives
        ],
        write_timeout_s=10.0,
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    big = asyncio.create_task(
        transport.request(
            "turn/start", {"threadId": "thread-1", "blob": "x" * 4_000_000}
        )
    )
    await asyncio.sleep(0.1)
    assert (
        transport._write_lock.locked()
    )  # committed write mid-drain: close will wait on it
    closing = asyncio.create_task(transport.close(grace_s=1.0))
    await asyncio.sleep(0.05)
    assert transport._closing and not closing.done() and transport.terminal is None
    # --- concurrent admissions while close is in progress ---
    with pytest.raises(StaleAnswer):
        await transport.answer(
            interaction.id,
            {"decision": "accept"},
            generation=transport.generation,
            token=interaction.token,
        )
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeLost) as info:
        await transport.request("probe")
    assert info.value.reason == "closing"
    assert asyncio.get_running_loop().time() - started < 0.5
    with pytest.raises(RuntimeLost):
        await transport.notify("noop")
    with pytest.raises(RuntimeLost):
        await transport.interrupt("thread-1", "turn-1")
    assert (
        interaction.state == "pending"
    )  # never accepted; retired by terminalize below
    report = await asyncio.wait_for(closing, HEALTHY_S)
    assert report["pending_waiters"] == 0
    assert report["pending_interactions"] == 0
    assert report["unsettled_interaction_commits"] == 0
    assert report["running_descendants"] == 0
    assert (
        report["exit_code"] == 64
    )  # the adversary saw the big request, then EOF -- nothing else
    assert interaction.state == "invalidated"
    with pytest.raises(RuntimeLost):
        await probe  # never answered by the adversary
    # big's bytes were accepted before close: it settles -- with its response
    # if the adversary answered before teardown, else with the terminal error.
    try:
        assert (await big)["turn"]["id"] == "turn-1"
    except RuntimeLost:
        pass
    assert await transport.close() == report


async def test_queued_request_admitted_before_close_is_retired_not_written(tmp_path):
    """A request that was admitted before close() but whose bytes were never
    accepted (queued behind the writer) is retired at close, not flushed."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "first"}],
            },
            {"actions": [{"type": "exit", "code": 77}]},
        ],
    )
    assert await transport.request("probe", timeout=HEALTHY_S) == "first"
    await transport._write_lock.acquire()  # "A" holds the writer
    transport._writer_since = asyncio.get_running_loop().time()
    queued = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.05)
    closing = asyncio.create_task(transport.close(grace_s=1.0))
    await asyncio.sleep(0.05)
    transport._writer_since = None
    transport._write_lock.release()
    report = await asyncio.wait_for(closing, HEALTHY_S)
    with pytest.raises(RuntimeLost):
        await queued
    assert report["exit_code"] == 64  # the queued probe never reached the adversary
    assert report["pending_waiters"] == 0


async def test_concurrent_closers_share_one_teardown(tmp_path):
    transport = await _transport(
        tmp_path,
        [{"expect_method": "probe", "actions": [{"type": "spawn", "seconds": 120}]}],
    )
    probe = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.2)
    signals: list = []
    original = transport._signal_group

    def counting(sig):
        signals.append(sig)
        original(sig)

    transport._signal_group = counting  # type: ignore[method-assign]
    reports = await asyncio.wait_for(
        asyncio.gather(
            transport.close(grace_s=0.5),
            transport.close(grace_s=0.5),
            transport.close(grace_s=0.5),
        ),
        HEALTHY_S,
    )
    assert reports[0] is reports[1] is reports[2]
    assert reports[0]["running_descendants"] == 0
    # One teardown sequence ran: at most one SIGTERM and one SIGKILL to the group.
    assert signals.count(signal.SIGTERM) <= 1 and signals.count(signal.SIGKILL) <= 1
    with pytest.raises(RuntimeLost):
        await probe


# ---------------------------------------------------------------------------
# an orphaned (committed, unanswered) request reaches one terminal outcome
# when the generation ends -- runtime loss or owner close alike
# ---------------------------------------------------------------------------


async def test_orphan_without_response_is_surfaced_as_ambiguous_on_runtime_loss(
    tmp_path,
):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "turn/start",
                "actions": [
                    {"type": "sleep", "seconds": 0.5},
                    {"type": "exit", "code": 9},
                ],
            }
        ],
    )
    events = transport.subscribe()
    with pytest.raises(asyncio.TimeoutError):
        await transport.request("turn/start", {"threadId": "thread-1"}, timeout=0.15)
    assert list(transport._orphaned_requests.values()) == ["turn/start"]
    # The server dies before ever answering.
    first = await _next(events)
    assert isinstance(first, AmbiguousRequest), first  # BEFORE the terminal event
    assert first.method == "turn/start" and first.terminal_reason in {"exit", "eof"}
    terminal = await _next(events)
    assert isinstance(terminal, TerminalEvent) and terminal.exit_code == 9
    assert transport._orphaned_requests == {}
    await asyncio.sleep(0.1)
    assert not any(
        isinstance(events.get_nowait(), AmbiguousRequest) for _ in range(events.qsize())
    )
    report = await transport.close()
    assert report["unresolved_orphans"] == 0 and report["pending_waiters"] == 0


async def test_orphan_without_response_is_surfaced_as_ambiguous_on_owner_close(
    tmp_path,
):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "turn/start",
                "actions": [{"type": "sleep", "seconds": 30}],
            }
        ],
    )
    events = transport.subscribe()
    with pytest.raises(asyncio.TimeoutError):
        await transport.request("turn/start", {"threadId": "thread-1"}, timeout=0.15)
    assert list(transport._orphaned_requests.values()) == ["turn/start"]
    report = await asyncio.wait_for(transport.close(grace_s=0.5), HEALTHY_S)
    first = await _next(events)
    assert isinstance(first, AmbiguousRequest) and first.method == "turn/start"
    assert first.terminal_reason == "closed"
    assert isinstance(await _next(events), TerminalEvent)
    assert report["unresolved_orphans"] == 0
    assert transport._orphaned_requests == {}


async def test_orphan_that_does_get_its_response_is_not_also_ambiguous(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "turn/start",
                "actions": [
                    {"type": "sleep", "seconds": 0.4},
                    TURN_START_RESPONSE,
                    {"type": "exit", "code": 0},
                ],
            }
        ],
    )
    events = transport.subscribe()
    with pytest.raises(asyncio.TimeoutError):
        await transport.request("turn/start", {"threadId": "thread-1"}, timeout=0.15)
    items = []
    while True:
        item = await _next(events)
        items.append(item)
        if isinstance(item, TerminalEvent):
            break
    kinds = [type(i).__name__ for i in items]
    assert (
        kinds.count("OrphanedResponse") == 1 and kinds.count("AmbiguousRequest") == 0
    ), kinds
    await transport.close()


async def test_delivered_orphan_is_not_also_ambiguous_when_generation_ends_before_caller_unwinds(
    tmp_path,
):
    """Forced ordering: turn/start committed -> Task.cancel(caller) (the
    awaited future is cancelled synchronously) -> response injected before the
    caller's handler runs (tombstone + OrphanedResponse) -> the generation
    ends, still before the caller's handler runs -> caller unwinds. Exactly one
    OrphanedResponse(X), zero AmbiguousRequest(X), no leftover tombstone."""
    transport = await _transport(
        tmp_path, [{"expect_method": "turn/start", "actions": []}]
    )
    events = transport.subscribe()
    task = asyncio.create_task(
        transport.request("turn/start", {"threadId": "thread-1"})
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if transport._waiters and not transport._write_lock.locked():
            break
    (request_id,) = transport._waiters.keys()
    task.cancel()
    # Same tick: the reader delivers the response (definitive outcome)...
    transport._dispatch({"id": request_id, "result": {"turn": {"id": "turn-1"}}})
    assert request_id in transport._orphan_delivered
    # ...and the generation ends, all before the cancelled caller resumes.
    transport._terminalize("exit", exit_code=9, detail="forced")
    with pytest.raises(asyncio.CancelledError):
        await task
    items = []
    while True:
        item = await _next(events)
        items.append(item)
        if isinstance(item, TerminalEvent):
            break
    orphaned = [
        i
        for i in items
        if isinstance(i, OrphanedResponse) and i.request_id == request_id
    ]
    ambiguous = [
        i
        for i in items
        if isinstance(i, AmbiguousRequest) and i.request_id == request_id
    ]
    assert len(orphaned) == 1 and orphaned[0].result == {"turn": {"id": "turn-1"}}
    assert ambiguous == []
    await asyncio.sleep(0.1)
    late = []
    while not events.empty():
        late.append(events.get_nowait())
    assert not any(
        isinstance(i, (AmbiguousRequest, OrphanedResponse)) for i in late
    ), late
    assert transport._orphan_delivered == set()
    assert transport._orphaned_requests == {}
    report = await transport.close()
    assert (
        report["unresolved_orphans"] == 0
        and report["unconsumed_orphan_tombstones"] == 0
    )


# ---------------------------------------------------------------------------
# a live caller learns whether its request crossed the wire
# ---------------------------------------------------------------------------


async def test_committed_live_caller_gets_request_outcome_unknown_on_loss(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "turn/start",
                "actions": [
                    {"type": "sleep", "seconds": 0.4},
                    {"type": "exit", "code": 9},
                ],
            }
        ],
    )
    events = transport.subscribe()
    with pytest.raises(RequestOutcomeUnknown) as info:
        await transport.request("turn/start", {"threadId": "thread-1"})
    assert info.value.method == "turn/start" and info.value.request_id
    assert info.value.terminal_reason in {"exit", "eof"} and info.value.exit_code == 9
    assert isinstance(
        info.value, RuntimeLost
    )  # existing RuntimeLost handlers still catch it
    # The live caller owns the outcome: no subscriber-level AmbiguousRequest for it.
    items = []
    while True:
        item = await _next(events)
        items.append(item)
        if isinstance(item, TerminalEvent):
            break
    assert not any(isinstance(i, AmbiguousRequest) for i in items)
    await transport.close()


async def test_queued_zero_byte_caller_gets_plain_runtime_lost_on_loss(tmp_path):
    """Control: the same loss while turn/start is still queued behind the
    writer (zero bytes) is a plain RuntimeLost -- known not sent -- and no
    ambiguity outcome exists for that request."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [
                    {"type": "sleep", "seconds": 0.3},
                    {"type": "exit", "code": 9},
                ],
            }
        ],
    )
    events = transport.subscribe()
    warm = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.05)
    await transport._write_lock.acquire()  # writer held: turn/start can only queue
    transport._writer_since = asyncio.get_running_loop().time()
    queued = asyncio.create_task(
        transport.request("turn/start", {"threadId": "thread-1"})
    )
    await _next_of(events, TerminalEvent)
    with pytest.raises(RuntimeLost) as info:
        await queued
    assert not isinstance(info.value, RequestOutcomeUnknown)
    with pytest.raises(RequestOutcomeUnknown):
        await warm  # the probe WAS on the wire when the runtime died
    transport._writer_since = None
    transport._write_lock.release()
    await asyncio.sleep(0.1)
    assert not any(
        isinstance(events.get_nowait(), AmbiguousRequest) for _ in range(events.qsize())
    )
    await transport.close()


# ---------------------------------------------------------------------------
# settled server-request id reuse: the registry stays identity-safe
# ---------------------------------------------------------------------------


async def test_forget_interaction_task_is_identity_safe(tmp_path):
    transport = await _transport(tmp_path, [])
    try:

        async def park():
            await asyncio.sleep(3600)

        old = asyncio.create_task(park())
        new = asyncio.create_task(park())
        transport._interaction_tasks["X"] = (
            new  # the id was reused; a NEW handler is parked
        )
        transport._forget_interaction_task(
            "X", old
        )  # the OLD handler's completion callback
        assert transport._interaction_tasks["X"] is new
        transport._forget_interaction_task("X", new)
        assert "X" not in transport._interaction_tasks
        old.cancel()
        new.cancel()
    finally:
        await transport.close()


async def test_reused_settled_server_request_id_is_handled_and_retired(tmp_path):
    """Upstream reuses X after the first X settled. The second X must be
    accepted (not a live duplicate), its parked handler must remain in the
    registry despite the first handler finishing, and serverRequest/resolved(X)
    must retire it and cancel that parked handler."""
    seen: list = []
    cancelled: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handler(interaction: PendingInteraction):
        seen.append(interaction)
        if len(seen) == 1:
            return {"decision": "decline"}
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set_result(interaction)
            raise

    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST]},
            {
                "expect_id": "approval-1",
                "actions": [
                    APPROVAL_REQUEST,
                    {"type": "sleep", "seconds": 0.3},
                    RESOLVED_APPROVAL,
                ],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "ok"}],
            },
        ],
        interaction_handler=handler,
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        first = await _next_of(events, PendingInteraction)
        second = await _next_of(events, PendingInteraction)
        assert first is not second and first.id == second.id == "approval-1"
        assert first.state == "answered" and second.open
        assert transport.rejected_server_requests == []  # a settled id may be reused
        await asyncio.sleep(0.05)
        assert (
            transport._interaction_tasks.get("approval-1") is not None
        )  # new handler still registered
        resolved = await _next_of(events, Notification, timeout=CONTROL_S)
        assert resolved.method == "serverRequest/resolved"
        assert second.state == "resolved"
        assert (await asyncio.wait_for(cancelled, CONTROL_S)) is second
        assert await transport.request("probe", timeout=HEALTHY_S) == "ok"
    finally:
        probe.cancel()
        await transport.close()


# ---------------------------------------------------------------------------
# occurrence identity: a settled id reused by upstream cannot be answered
# with a previous occurrence's credentials -- by a stale card or a late handler
# ---------------------------------------------------------------------------


async def test_stale_occurrence_credentials_cannot_answer_reused_id(tmp_path):
    """old X -> resolved(X) -> new X (same generation). Answering with the OLD
    occurrence's token must be StaleAnswer and the adversary must see no
    response for new X; the NEW token succeeds exactly once."""
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [APPROVAL_REQUEST, RESOLVED_APPROVAL, APPROVAL_REQUEST],
            },
            # exactly one response for X is expected here; a second would desync
            {
                "expect_id": "approval-1",
                "actions": [
                    {
                        "type": "message",
                        "message": {"method": "probe/answered", "params": {}},
                    }
                ],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "ok"}],
            },
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        old = await _next_of(events, PendingInteraction)
        resolved = await _next_of(events, Notification, timeout=CONTROL_S)
        assert resolved.method == "serverRequest/resolved" and old.state == "resolved"
        new = await _next_of(events, PendingInteraction)
        assert new is not old and new.id == old.id and new.generation == old.generation
        assert new.token != old.token and new.open
        # The stale card is clicked.
        with pytest.raises(StaleAnswer):
            await transport.answer(
                old.id,
                {"decision": "accept"},
                generation=old.generation,
                token=old.token,
            )
        with pytest.raises(StaleAnswer):
            await transport.fail_interaction(
                old.id, generation=old.generation, token=old.token
            )
        assert new.open  # untouched
        # The right occurrence answers, exactly once.
        await transport.answer(
            new.id, {"decision": "decline"}, generation=new.generation, token=new.token
        )
        assert new.state == "answered"
        ack = await _next_of(events, Notification, timeout=CONTROL_S)
        assert ack.method == "probe/answered"
        with pytest.raises(StaleAnswer):
            await transport.answer(
                new.id,
                {"decision": "decline"},
                generation=new.generation,
                token=new.token,
            )
        assert await transport.request("probe", timeout=HEALTHY_S) == "ok"
    finally:
        probe.cancel()
        await transport.close()


async def test_late_old_handler_result_cannot_answer_reused_id(tmp_path):
    """Same ordering through the interaction_handler: the OLD occurrence's
    handler is a misbehaving one that survives cancellation and returns late,
    after new X exists. Its result must be dropped (StaleAnswer inside the
    transport), new X stays pending, and only new X's own handler answers."""
    release_old: asyncio.Future = asyncio.get_running_loop().create_future()
    new_seen: asyncio.Future = asyncio.get_running_loop().create_future()
    calls: list = []

    async def handler(interaction: PendingInteraction):
        calls.append(interaction)
        if len(calls) == 1:
            try:
                await release_old
            except asyncio.CancelledError:
                pass  # misbehaving: swallows the retirement and still answers
            return {"decision": "accept", "who": "old"}
        new_seen.set_result(interaction)
        await asyncio.sleep(0.3)  # arrives after the old result
        return {"decision": "decline", "who": "new"}

    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                # The old handler must already be RUNNING (parked) when the
                # retirement arrives, otherwise its task is cancelled before it
                # starts and there is no late result to test.
                "actions": [
                    APPROVAL_REQUEST,
                    {"type": "sleep", "seconds": 0.2},
                    RESOLVED_APPROVAL,
                    APPROVAL_REQUEST,
                ],
            },
            {
                "expect_id": "approval-1",
                "actions": [
                    {
                        "type": "message",
                        "message": {"method": "probe/answered", "params": {}},
                    }
                ],
            },
            {
                "expect_method": "probe",
                "actions": [{"type": "response", "result": "ok"}],
            },
        ],
        interaction_handler=handler,
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        new = await asyncio.wait_for(new_seen, HEALTHY_S)
        assert (
            len(calls) == 2
            and calls[0].token != new.token
            and calls[0].state == "resolved"
        )
        await asyncio.sleep(0.05)
        # The old handler has been cancelled by resolved(X) and swallowed it; it
        # returns its stale "accept" now, while new X is pending.
        if not release_old.done():
            release_old.set_result(None)
        await asyncio.sleep(0.05)
        assert new.open  # the stale old result did not land on the new occurrence
        while True:  # skip the earlier serverRequest/resolved notification
            ack = await _next_of(events, Notification, timeout=CONTROL_S)
            if ack.method == "probe/answered":
                break
            assert ack.method == "serverRequest/resolved"
        # the ONE response came from new X's own handler
        assert new.state == "answered"
        assert await transport.request("probe", timeout=HEALTHY_S) == "ok"
    finally:
        probe.cancel()
        await transport.close()
