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
    AppServerTransport,
    Notification,
    PendingInteraction,
    RpcError,
    RuntimeLost,
    StaleAnswer,
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


async def _next(queue: asyncio.Queue, timeout: float = HEALTHY_S):
    return await asyncio.wait_for(queue.get(), timeout)


async def _next_of(queue: asyncio.Queue, kind: type, timeout: float = HEALTHY_S):
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
