"""Tests for the between-turn idle reader and session outbox."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
)

import src.routes.sessions as sessions_module
from src.session_manager import Session, session_manager
from src.session_outbox import (
    SessionOutbox,
    _message_to_event,
    apply_turn_task_chunk,
    get_outbox,
    idle_reader_running,
    pause_idle_reader,
    resume_idle_reader,
    stop_idle_reader_nowait,
)


def _task_started(task_id="t1", description="build the report"):
    return TaskStartedMessage(
        subtype="task_started",
        data={"task_type": "local_agent", "subagent_type": "Explore"},
        task_id=task_id,
        description=description,
        uuid="u1",
        session_id="s1",
    )


def _task_progress(task_id="t1", tool="Bash"):
    return TaskProgressMessage(
        subtype="task_progress",
        data={},
        task_id=task_id,
        description="crunching",
        usage={"total_tokens": 10, "tool_uses": 2, "duration_ms": 1500},
        uuid="u2",
        session_id="s1",
        last_tool_name=tool,
    )


def _task_notification(task_id="t1", status="completed"):
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="/tmp/out.txt",
        summary="done",
        uuid="u3",
        session_id="s1",
    )


def _task_updated(task_id="t1", status="completed", patch_dict=None):
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id=task_id,
        patch=patch_dict if patch_dict is not None else {"status": status},
        status=status,
    )


def _assistant(text="백그라운드 작업이 끝났습니다", message_id=None, parent_tool_use_id=None):
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude",
        message_id=message_id,
        parent_tool_use_id=parent_tool_use_id,
    )


def _result():
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="s1",
    )


# ---------------------------------------------------------------------------
# SessionOutbox
# ---------------------------------------------------------------------------


class TestSessionOutbox:
    def test_append_stamps_monotonic_seq_and_ts(self):
        outbox = SessionOutbox()
        first = outbox.append({"type": "task_started", "task_id": "a"})
        second = outbox.append({"type": "task_updated", "task_id": "a"})
        assert (first["seq"], second["seq"]) == (1, 2)
        assert first["ts"] and second["ts"]

    def test_events_after_cursor_and_limit(self):
        outbox = SessionOutbox()
        for i in range(10):
            outbox.append({"type": "task_progress", "task_id": str(i)})
        assert [e["seq"] for e in outbox.events_after(7)] == [8, 9, 10]
        assert [e["seq"] for e in outbox.events_after(0, limit=2)] == [1, 2]

    def test_ring_buffer_drops_oldest(self):
        outbox = SessionOutbox(maxlen=3)
        for i in range(5):
            outbox.append({"type": "task_progress", "task_id": str(i)})
        assert [e["seq"] for e in outbox.events_after(0)] == [3, 4, 5]

    def test_active_task_lifecycle(self):
        outbox = SessionOutbox()
        started = outbox.append(
            {
                "type": "task_started",
                "task_id": "t1",
                "description": "d",
                "task_type": "local_agent",
                "subagent_type": "Explore",
            }
        )
        outbox.apply_task_event(started)
        assert outbox.active_tasks["t1"]["subagent_type"] == "Explore"

        progress = outbox.append(
            {
                "type": "task_progress",
                "task_id": "t1",
                "description": "further",
                "last_tool_name": "Bash",
                "usage": {"total_tokens": 5, "tool_uses": 1, "duration_ms": 9},
            }
        )
        outbox.apply_task_event(progress)
        entry = outbox.active_tasks["t1"]
        assert entry["last_tool_name"] == "Bash"
        assert entry["description"] == "further"
        assert entry["usage"]["total_tokens"] == 5

        done = outbox.append(
            {"type": "task_notification", "task_id": "t1", "status": "completed"}
        )
        outbox.apply_task_event(done)
        assert "t1" not in outbox.active_tasks

    def test_terminal_via_task_updated_only(self):
        """Background tasks may end with task_updated and no notification."""
        outbox = SessionOutbox()
        for event in (
            {"type": "task_started", "task_id": "bg", "description": "d"},
            {"type": "task_updated", "task_id": "bg", "status": "killed", "patch": {}},
        ):
            outbox.apply_task_event(outbox.append(event))
        assert "bg" not in outbox.active_tasks

    def test_unknown_task_progress_synthesizes_entry(self):
        """A task started mid-turn (reader off) must still appear via progress."""
        outbox = SessionOutbox()
        progress = outbox.append(
            {"type": "task_progress", "task_id": "late", "description": "d"}
        )
        outbox.apply_task_event(progress)
        assert outbox.active_tasks["late"]["status"] == "running"


class TestApplyTurnTaskChunk:
    """Task chunks streamed during a turn must pre-seed the active registry
    so a silent background job is visible to pollers right after run end."""

    def test_started_chunk_seeds_registry(self):
        session = Session(session_id="s-turn")
        # Shape of ClaudeCodeCLI._convert_message(TaskStartedMessage):
        # top-level fields + subtype + raw payload under ``data``.
        apply_turn_task_chunk(
            session,
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": "bg1",
                "description": "long build",
                "data": {"task_type": "local_bash", "subagent_type": None},
            },
        )
        outbox = get_outbox(session)
        entry = outbox.active_tasks["bg1"]
        assert entry["description"] == "long build"
        assert entry["task_type"] == "local_bash"
        # Registry only — the turn stream already delivered the event.
        assert outbox.events_after(0) == []

    def test_terminal_patch_status_clears_registry(self):
        session = Session(session_id="s-turn")
        apply_turn_task_chunk(
            session,
            {"subtype": "task_started", "task_id": "bg1", "description": "d"},
        )
        apply_turn_task_chunk(
            session,
            {"subtype": "task_updated", "task_id": "bg1", "patch": {"status": "completed"}},
        )
        assert get_outbox(session).active_tasks == {}

    def test_non_task_chunks_are_ignored(self):
        session = Session(session_id="s-turn")
        apply_turn_task_chunk(session, {"type": "assistant", "content": []})
        apply_turn_task_chunk(session, "not a dict")
        apply_turn_task_chunk(session, {"subtype": "task_progress"})  # no task_id
        assert getattr(session, "outbox", None) is None or not session.outbox.active_tasks


# ---------------------------------------------------------------------------
# SDK message conversion
# ---------------------------------------------------------------------------


class TestMessageToEvent:
    def test_task_messages(self):
        started = _message_to_event(_task_started())
        assert started["type"] == "task_started"
        assert started["task_type"] == "local_agent"
        assert started["subagent_type"] == "Explore"

        progress = _message_to_event(_task_progress())
        assert progress["type"] == "task_progress"
        assert progress["last_tool_name"] == "Bash"
        assert progress["usage"]["total_tokens"] == 10

        notification = _message_to_event(_task_notification())
        assert notification["type"] == "task_notification"
        assert notification["status"] == "completed"
        assert notification["output_file"] == "/tmp/out.txt"

        updated = _message_to_event(_task_updated(status=None, patch_dict={"status": "failed"}))
        assert updated["type"] == "task_updated"
        assert updated["status"] == "failed"  # falls back to patch.status

    def test_assistant_text_and_result(self):
        assistant = _message_to_event(_assistant("완료 요약", message_id="m1"))
        assert assistant == {"type": "assistant_message", "text": "완료 요약"}
        assert _message_to_event(_assistant("")) is None

        result = _message_to_event(_result())
        assert result == {"type": "turn_result", "subtype": "success", "is_error": False}

    def test_subagent_assistant_text_is_skipped(self):
        """Subagent-internal narration must not leak as background replies."""
        message = _assistant("This is a large file...", parent_tool_use_id="toolu_1")
        assert _message_to_event(message) is None

    def test_noise_is_skipped(self):
        assert _message_to_event(SystemMessage(subtype="status", data={})) is None
        assert _message_to_event({"type": "stream_event"}) is None
        assert _message_to_event("garbage") is None


# ---------------------------------------------------------------------------
# Idle reader lifecycle
# ---------------------------------------------------------------------------


class FakeSDKClient:
    """Minimal stand-in exposing the queue-fed receive_messages stream."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    async def receive_messages(self):
        while True:
            message = await self.queue.get()
            if message is None:  # sentinel: stream closed
                return
            yield message


def _make_session(client=None) -> Session:
    return Session(session_id="sess-outbox", user="alice", client=client)


async def _drain_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


class TestIdleReader:
    @pytest.mark.asyncio
    async def test_captures_between_turn_messages(self):
        client = FakeSDKClient()
        session = _make_session(client)
        assert resume_idle_reader(session) is True

        await client.queue.put(_task_started())
        await client.queue.put(_task_progress())
        await client.queue.put(_task_notification())
        await client.queue.put(_assistant())
        await client.queue.put(_result())

        outbox = get_outbox(session)
        await _drain_until(lambda: outbox.next_seq > 5)
        types = [e["type"] for e in outbox.events_after(0)]
        assert types == [
            "task_started",
            "task_progress",
            "task_notification",
            "assistant_message",
            "turn_result",
        ]
        # notification was terminal — no active task left
        assert outbox.snapshot_active_tasks() == []

        await pause_idle_reader(session)
        assert not idle_reader_running(session)

    @pytest.mark.asyncio
    async def test_pause_stops_consumption_for_next_turn(self):
        """After pause, queued messages stay for the turn reader to consume."""
        client = FakeSDKClient()
        session = _make_session(client)
        resume_idle_reader(session)
        await client.queue.put(_task_started())
        outbox = get_outbox(session)
        await _drain_until(lambda: outbox.next_seq > 1)

        await pause_idle_reader(session)
        await client.queue.put(_task_progress(task_id="t2"))
        await asyncio.sleep(0.05)
        # Not consumed by the (stopped) reader:
        assert outbox.events_after(1) == []
        assert client.queue.qsize() == 1

        # Reader restart picks it back up.
        resume_idle_reader(session)
        await _drain_until(lambda: outbox.next_seq > 2)
        assert outbox.events_after(1)[0]["type"] == "task_progress"
        await pause_idle_reader(session)

    @pytest.mark.asyncio
    async def test_assistant_messages_deliver_immediately(self):
        """Each complete assistant message becomes its own event right away —
        a long re-invocation must not sit silent until its ResultMessage."""
        client = FakeSDKClient()
        session = _make_session(client)
        resume_idle_reader(session)

        await client.queue.put(_assistant("첫 번째 에이전트 완료.", message_id="m1"))
        outbox = get_outbox(session)
        await _drain_until(lambda: outbox.next_seq > 1)
        assert [e["type"] for e in outbox.events_after(0)] == ["assistant_message"]
        assert outbox.events_after(0)[0]["text"] == "첫 번째 에이전트 완료."

        await client.queue.put(_assistant("두 번째도 완료.", message_id="m2"))
        await client.queue.put(_result())
        await _drain_until(lambda: outbox.next_seq > 3)
        assert [e["type"] for e in outbox.events_after(1)] == [
            "assistant_message",
            "turn_result",
        ]
        await pause_idle_reader(session)

    @pytest.mark.asyncio
    async def test_subagent_narration_not_captured(self):
        client = FakeSDKClient()
        session = _make_session(client)
        resume_idle_reader(session)
        await client.queue.put(
            _assistant("This is a large file...", parent_tool_use_id="toolu_9")
        )
        await client.queue.put(_result())
        outbox = get_outbox(session)
        await _drain_until(lambda: outbox.next_seq > 1)
        assert [e["type"] for e in outbox.events_after(0)] == ["turn_result"]
        await pause_idle_reader(session)

    @pytest.mark.asyncio
    async def test_resume_is_idempotent_and_gated(self):
        client = FakeSDKClient()
        session = _make_session(client)
        assert resume_idle_reader(session) is True
        first_task = session.idle_reader_task
        assert resume_idle_reader(session) is True
        assert session.idle_reader_task is first_task
        await pause_idle_reader(session)

        session.active_response_id = "resp_x_1"
        assert resume_idle_reader(session) is False
        session.active_response_id = None

        session.pending_tool_call = {"call_id": "c1"}
        assert resume_idle_reader(session) is False
        session.pending_tool_call = None

        session.client = None
        assert resume_idle_reader(session) is False

        session.client = object()  # no receive_messages (codex/opencode)
        assert resume_idle_reader(session) is False

    @pytest.mark.asyncio
    async def test_reader_exits_on_stream_end(self):
        client = FakeSDKClient()
        session = _make_session(client)
        resume_idle_reader(session)
        await client.queue.put(None)  # close the stream
        await _drain_until(lambda: not idle_reader_running(session))
        assert session.idle_reader_task is None

    @pytest.mark.asyncio
    async def test_stop_nowait_cancels(self):
        client = FakeSDKClient()
        session = _make_session(client)
        resume_idle_reader(session)
        task = session.idle_reader_task
        stop_idle_reader_nowait(session)
        with pytest.raises((asyncio.CancelledError, Exception)):
            await asyncio.wait_for(task, timeout=1.0)
        assert session.idle_reader_task is None

    @pytest.mark.asyncio
    async def test_pause_without_reader_is_noop(self):
        session = _make_session()
        await pause_idle_reader(session)  # must not raise


# ---------------------------------------------------------------------------
# GET /v1/sessions/{id}/pending-events
# ---------------------------------------------------------------------------


@pytest.fixture()
def pending_events_client():
    app = FastAPI()
    app.include_router(sessions_module.router)
    with patch.object(
        sessions_module, "verify_api_key", new=AsyncMock(return_value=True)
    ):
        with TestClient(app) as client:
            yield client


@pytest.fixture()
def stored_session():
    session = Session(session_id="sess-pe", user="alice")
    with session_manager.lock:
        session_manager.sessions["sess-pe"] = session
    yield session
    with session_manager.lock:
        session_manager.sessions.pop("sess-pe", None)


class TestPendingEventsEndpoint:
    def test_unknown_session_404(self, pending_events_client):
        response = pending_events_client.get("/v1/sessions/nope/pending-events")
        assert response.status_code == 404

    def test_user_mismatch_404(self, pending_events_client, stored_session):
        response = pending_events_client.get(
            "/v1/sessions/sess-pe/pending-events", params={"user": "mallory"}
        )
        assert response.status_code == 404

    def test_empty_then_incremental_poll(self, pending_events_client, stored_session):
        response = pending_events_client.get(
            "/v1/sessions/sess-pe/pending-events", params={"user": "alice"}
        )
        body = response.json()
        assert response.status_code == 200
        assert body["events"] == []
        assert body["next_after"] == 0
        assert body["active_tasks"] == []
        assert body["turn_in_progress"] is False
        assert body["client_connected"] is False
        # No client → reader cannot run
        assert body["reader_active"] is False

        outbox = get_outbox(stored_session)
        for event in (
            {"type": "task_started", "task_id": "t1", "description": "d"},
            {"type": "task_progress", "task_id": "t1", "description": "d2"},
        ):
            outbox.apply_task_event(outbox.append(event))

        body = pending_events_client.get(
            "/v1/sessions/sess-pe/pending-events", params={"user": "alice"}
        ).json()
        assert [e["seq"] for e in body["events"]] == [1, 2]
        assert body["next_after"] == 2
        assert body["active_tasks"][0]["task_id"] == "t1"

        body = pending_events_client.get(
            "/v1/sessions/sess-pe/pending-events",
            params={"user": "alice", "after": 2},
        ).json()
        assert body["events"] == []
        assert body["next_after"] == 2

    def test_stale_high_cursor_clamps(self, pending_events_client, stored_session):
        get_outbox(stored_session).append({"type": "task_started", "task_id": "t"})
        body = pending_events_client.get(
            "/v1/sessions/sess-pe/pending-events", params={"after": 999}
        ).json()
        assert body["events"] == []
        assert body["next_after"] == 1

    def test_turn_in_progress_flag(self, pending_events_client, stored_session):
        stored_session.active_response_id = "resp_sess-pe_3"
        try:
            body = pending_events_client.get(
                "/v1/sessions/sess-pe/pending-events"
            ).json()
            assert body["turn_in_progress"] is True
        finally:
            stored_session.active_response_id = None
