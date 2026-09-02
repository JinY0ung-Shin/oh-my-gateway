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
import sys
from pathlib import Path

import pytest

from src.backends.appserver import (
    RUNTIME_LOST,
    SERVER_RESOLVED,
    AppServerTransport,
    HandshakeError,
    Notification,
    OrphanedResponse,
    PendingInteraction,
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


def _scenario(tmp_path: Path, steps: list, *, linger: bool = True, handshake: bool = True) -> Path:
    payload = {"steps": (HANDSHAKE_STEPS if handshake else []) + steps, "linger": linger}
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def _transport(tmp_path: Path, steps: list, **kwargs) -> AppServerTransport:
    scenario = _scenario(tmp_path, steps)
    transport = AppServerTransport([sys.executable, str(FIXTURE), str(scenario)], **kwargs)
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
        [{"expect_method": "probe", "actions": [{"type": "response", "result": {"ok": True}}]}],
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
                "actions": [{"type": "response", "error": {"code": -32000, "message": "nope"}}],
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
        [{"expect_method": "turn/start", "actions": [TURN_COMPLETED, TURN_START_RESPONSE]}],
    )
    try:
        events = transport.subscribe()
        result = await transport.request("turn/start", {"threadId": "thread-1"}, timeout=HEALTHY_S)
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
        [{"expect_method": "turn/start", "actions": [TURN_START_RESPONSE, TURN_COMPLETED]}],
    )
    try:
        events = transport.subscribe()
        await transport.request("turn/start", {"threadId": "thread-1"}, timeout=HEALTHY_S)
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


async def test_death_while_parked_invalidates_interaction_and_rejects_late_answer(tmp_path):
    transport = await _transport(
        tmp_path,
        [{"expect_method": "probe", "actions": [APPROVAL_REQUEST, {"type": "exit", "code": 23}]}],
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
        await transport.answer(interaction.id, {"decision": "accept"}, generation=transport.generation)
    with pytest.raises(RuntimeLost):
        await probe
    await transport.close()


async def test_wrong_generation_answer_is_rejected_before_reaching_runtime(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST]},
            {"expect_id": "approval-1", "actions": [{"type": "message", "message": {"method": "probe/answered", "params": {}}}]},
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
            await transport.answer(interaction.id, {"decision": "accept"}, generation=1)
        assert interaction.open
        # The correct generation goes through, exactly once.
        await transport.answer(interaction.id, {"decision": "decline"}, generation=2)
        ack = await _next_of(events, Notification)
        assert ack.method == "probe/answered"
        with pytest.raises(StaleAnswer):
            await transport.answer(interaction.id, {"decision": "decline"}, generation=2)
    finally:
        probe.cancel()
        await transport.close()


async def test_answer_after_close_is_stale(tmp_path):
    transport = await _transport(tmp_path, [{"expect_method": "probe", "actions": [APPROVAL_REQUEST]}])
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    probe.cancel()
    report = await transport.close()
    assert report["pending_interactions"] == 0
    assert interaction.state == "invalidated"
    with pytest.raises(StaleAnswer):
        await transport.answer(interaction.id, {"decision": "accept"}, generation=transport.generation)


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
                "actions": [{"type": "message", "message": {"method": "probe/rejected", "params": {}}}],
            },
            {"expect_method": "probe", "actions": [{"type": "response", "result": "still-alive"}]},
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
            {"expect_method": "probe", "actions": [{"type": "malformed", "text": "{not json"}]},
        ],
    )
    events = transport.subscribe()
    outcomes = await asyncio.wait_for(
        asyncio.gather(transport.request("probe"), transport.request("probe"), return_exceptions=True),
        HEALTHY_S,
    )
    assert all(isinstance(o, RuntimeLost) and o.reason == "protocol_error" for o in outcomes), outcomes
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
            {"expect_method": "probe", "actions": [{"type": "spawn", "seconds": 120}, APPROVAL_REQUEST]},
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
    assert isinstance(await _next_of(events, TerminalEvent, timeout=CONTROL_S), TerminalEvent)
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
                "actions": [RESOLVED_APPROVAL, {"type": "response", "result": {"turnId": "turn-1"}}],
            },
            # Any stray response for approval-1 would land here instead and
            # desync the adversary (exit 64) -> the follow-up probe would fail.
            {"expect_method": "probe", "actions": [{"type": "response", "result": "no-stray-write"}]},
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
            await transport.answer(interaction.id, {"decision": "accept"}, generation=transport.generation)
        with pytest.raises(StaleAnswer):
            await transport.fail_interaction(interaction.id, generation=transport.generation)
        assert await transport.request("probe", timeout=HEALTHY_S) == "no-stray-write"
        assert transport.alive
    finally:
        probe.cancel()
        await transport.close()


async def test_server_driven_resolution_without_interrupt_or_death(tmp_path):
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST, RESOLVED_APPROVAL]},
            {"expect_method": "probe", "actions": [{"type": "response", "result": "no-stray-write"}]},
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
            await transport.answer(interaction.id, {"decision": "accept"}, generation=transport.generation)
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
                            "params": {"threadId": "thread-1", "requestId": "never-seen"},
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
                    {"type": "response", "result": {"payload": payload, "attempt": attempt}},
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
        [{"expect_method": "initialize", "actions": [{"type": "spawn", "seconds": 120}]}],
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
                "actions": [{"type": "response", "error": {"code": -32000, "message": "no"}}],
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
                "actions": [{"type": "message", "message": {"method": "probe/answered", "params": {}}}],
            },
            # A duplicate response for approval-1 would land here and desync.
            {"expect_method": "probe", "actions": [{"type": "response", "result": "exactly-once"}]},
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        # Hold the writer so the answer parks before any byte is written...
        await transport._write_lock.acquire()
        caller = asyncio.create_task(
            transport.answer(interaction.id, {"decision": "decline"}, generation=transport.generation)
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
            await transport.answer(interaction.id, {"decision": "accept"}, generation=transport.generation)
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
        [{"expect_method": "probe", "actions": [APPROVAL_REQUEST, {"type": "sleep", "seconds": 0.3}, {"type": "exit", "code": 3}]}],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    await transport._write_lock.acquire()
    caller = asyncio.create_task(
        transport.answer(interaction.id, {"decision": "decline"}, generation=transport.generation)
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
        "message": {"method": "item/agentMessage/delta", "params": {"delta": f"chunk-{n}"}},
    }


async def test_slow_subscriber_drops_deltas_but_keeps_lossless_items(tmp_path):
    lifecycle = {"type": "message", "message": {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}}
    transport = await _transport(
        tmp_path,
        [
            {
                "expect_method": "probe",
                "actions": [*(_delta(n) for n in range(20)), lifecycle, APPROVAL_REQUEST, {"type": "response", "result": "ok"}],
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
        deltas = [i for i in items if isinstance(i, Notification) and i.method.endswith("/delta")]
        assert len(deltas) == 3
        assert slow.dropped_deltas == 17
        assert any(isinstance(i, Notification) and i.method == "turn/completed" for i in items)
        assert any(isinstance(i, PendingInteraction) for i in items)
        assert not slow.disconnected
        fast_items = []
        while not fast.empty():
            fast_items.append(fast.get_nowait())
        assert sum(isinstance(i, Notification) and i.method.endswith("/delta") for i in fast_items) == 20
        assert fast.dropped_deltas == 0
    finally:
        await transport.close()


async def test_subscriber_far_behind_on_lossless_items_is_disconnected_not_grown(tmp_path):
    lifecycle = [
        {"type": "message", "message": {"method": "item/completed", "params": {"n": n}}} for n in range(10)
    ]
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [*lifecycle, {"type": "response", "result": "first"}]},
            {"expect_method": "probe", "actions": [{"type": "response", "result": "second"}]},
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
        assert len(items) == 5  # 4 retained lossless items + the overflow marker, nothing after
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
            {"expect_method": "probe", "actions": [{"type": "spawn", "seconds": 120}, {"type": "sleep", "seconds": 30}]},
        ],
        write_timeout_s=0.5,
    )
    events = transport.subscribe()
    stalled = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.3)  # the child has read the line and is now asleep
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeLost) as info:
        await transport.request("probe", {"blob": "x" * 4_000_000}, timeout=HEALTHY_S)
    assert asyncio.get_running_loop().time() - started < CONTROL_S
    assert info.value.reason == "write_timeout"
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
            {"expect_method": "turn/interrupt", "actions": [{"type": "response", "result": {"turnId": "turn-1"}}]},
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
    transport = await _transport(tmp_path, [{"expect_method": "probe", "actions": [{"type": "spawn", "seconds": 120}]}])
    probe = asyncio.create_task(transport.request("probe"))
    await asyncio.sleep(0.2)
    report = await transport.close()
    assert report["reason"] == "closed"
    assert report["running_descendants"] == 0
    assert transport._reap_task is None  # owner-initiated close never schedules loss teardown
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
            {"expect_method": "probe", "actions": [{"type": "sleep", "seconds": 0.4}, {"type": "response", "result": "warm"}]},
            {"expect_method": "probe", "actions": [{"type": "response", "result": "A"}]},
            {"expect_method": "probe", "actions": [{"type": "response", "result": "after"}]},
        ],
        write_timeout_s=5.0,
    )
    events = transport.subscribe()
    try:
        warm = asyncio.create_task(transport.request("probe"))
        await asyncio.sleep(0.1)  # the child has read "warm" and is asleep: nobody reads stdin
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
        assert not any(isinstance(events.get_nowait(), TerminalEvent) for _ in range(events.qsize()))
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
    with pytest.raises(RuntimeLost) as info:
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
    steps = [{"expect_method": "probe", "actions": [{"type": "response", "result": i}]} for i in range(n)]
    transport = await _transport(tmp_path, steps, write_timeout_s=0.05)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(transport.request("probe", {"blob": "y" * 20_000, "i": i}) for i in range(n))),
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
                "actions": [APPROVAL_REQUEST, {"type": "sleep", "seconds": 0.5}, RESOLVED_APPROVAL],
            },
            # A stray response for approval-1 would land here and desync.
            {"expect_method": "probe", "actions": [{"type": "response", "result": "no-stray-write"}]},
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        await transport._write_lock.acquire()  # the writer is busy elsewhere
        caller = asyncio.create_task(
            transport.answer(interaction.id, {"decision": "accept"}, generation=transport.generation)
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
            {"expect_method": "probe", "actions": [{"type": "response", "result": "ok"}]},
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    try:
        interaction = await _next_of(events, PendingInteraction)
        await transport.answer(interaction.id, {"decision": "decline"}, generation=transport.generation)
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
        tmp_path, [{"expect_method": "probe", "actions": [{"type": "response", "result": "later"}]}]
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


async def test_request_cancelled_during_drain_completes_write_and_orphans_response(tmp_path):
    """The child pauses reading so a 4 MB request blocks in drain(); the
    caller is cancelled mid-drain. The writer must NOT be released early: the
    transport finishes the write, the request really reaches the child, and
    its response comes back as OrphanedResponse for the owner to reconcile."""
    payload = "x" * 4_000_000
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "probe", "actions": [{"type": "sleep", "seconds": 0.6}, {"type": "response", "result": "warm"}]},
            {"expect_method": "turn/start", "actions": [TURN_START_RESPONSE]},
            {"expect_method": "probe", "actions": [{"type": "response", "result": "after"}]},
        ],
        write_timeout_s=10.0,
    )
    events = transport.subscribe()
    try:
        warm = asyncio.create_task(transport.request("probe"))
        await asyncio.sleep(0.1)  # child has read "warm" and is asleep
        big = asyncio.create_task(transport.request("turn/start", {"threadId": "thread-1", "blob": payload}))
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
                "actions": [{"type": "message", "message": {"method": "probe/dup-rejected", "params": {}}}],
            },
            {"expect_method": "probe", "actions": [{"type": "response", "result": "alive"}]},
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


async def test_resolved_arriving_before_committed_task_runs_writes_nothing(tmp_path, monkeypatch):
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
            {"expect_method": "probe", "actions": [APPROVAL_REQUEST, {"type": "sleep", "seconds": 0.3}, RESOLVED_APPROVAL]},
            {"expect_method": "probe", "actions": [{"type": "response", "result": "no-stray-write"}]},
        ],
    )
    events = transport.subscribe()
    probe = asyncio.create_task(transport.request("probe"))
    interaction = await _next_of(events, PendingInteraction)
    hold.clear()  # from here on committed tasks are scheduled but do not run
    caller = asyncio.create_task(
        transport.answer(interaction.id, {"decision": "accept"}, generation=transport.generation)
    )
    await asyncio.sleep(0.05)
    assert interaction.state == "resolving"
    assert not interaction.wire_committed
    assert transport._write_lock.locked()  # writer handed to the (parked) committed task
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


async def test_response_that_beats_cancelled_caller_becomes_exactly_one_orphan(tmp_path):
    """Ordering: request committed -> cancellation requested -> the reader
    delivers the response BEFORE the cancelled task runs its handler. The
    response must surface as exactly one OrphanedResponse, with no orphan id
    left behind. The adversary sends no response of its own so the injected
    one is the only one in play."""
    transport = await _transport(tmp_path, [{"expect_method": "turn/start", "actions": []}])
    events = transport.subscribe()
    task = asyncio.create_task(transport.request("turn/start", {"threadId": "thread-1"}))
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
    assert not any(isinstance(events.get_nowait(), OrphanedResponse) for _ in range(events.qsize()))
    await transport.close()


async def test_response_timeout_after_committed_send_is_orphaned_not_dropped(tmp_path):
    """turn/start is sent cleanly; the caller's response deadline expires; the
    server completes it later. The owner must still receive exactly one
    OrphanedResponse -- a stateful call that really happened is never lost."""
    transport = await _transport(
        tmp_path,
        [
            {"expect_method": "turn/start", "actions": [{"type": "sleep", "seconds": 0.5}, TURN_START_RESPONSE]},
            {"expect_method": "probe", "actions": [{"type": "response", "result": "alive"}]},
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
                "actions": [{"type": "sleep", "seconds": 0.4}, {"type": "response", "error": {"code": -32000, "message": "late no"}}],
            }
        ],
    )
    events = transport.subscribe()
    with pytest.raises(asyncio.TimeoutError):
        await transport.request("turn/start", {"threadId": "thread-1"}, timeout=0.15)
    orphan = await _next_of(events, OrphanedResponse)
    assert orphan.result is None and orphan.error["code"] == -32000
    await transport.close()
