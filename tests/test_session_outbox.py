"""Tests for the between-turn idle reader and session outbox."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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
    ToolUseBlock,
)

import src.routes.sessions as sessions_module
import src.session_outbox as session_outbox_module
from src.session_manager import Session, session_manager
from src.session_outbox import (
    SessionOutbox,
    _message_to_event,
    apply_turn_task_chunk,
    drain_backlog_to_outbox,
    get_outbox,
    idle_reader_running,
    pause_idle_reader,
    reset_active_tasks,
    resume_idle_reader,
    resume_idle_reader_between_turns,
    stop_idle_reader_nowait,
)


def _task_started(task_id="t1", description="build the report", tool_use_id=None):
    return TaskStartedMessage(
        subtype="task_started",
        data={"task_type": "local_agent", "subagent_type": "Explore"},
        task_id=task_id,
        description=description,
        uuid="u1",
        session_id="s1",
        tool_use_id=tool_use_id,
    )


def _agent_call(tool_use_id="toolu_spawn", name="worker-a", tool="Agent"):
    """An Agent/Task tool_use block as the SDK delivers it (an object)."""
    tool_input = {"description": "d", "prompt": "p", "subagent_type": "general-purpose"}
    if name is not None:
        tool_input["name"] = name
    return ToolUseBlock(id=tool_use_id, name=tool, input=tool_input)


def _spawn_message(*blocks, parent_tool_use_id=None):
    return AssistantMessage(
        content=list(blocks),
        model="claude",
        parent_tool_use_id=parent_tool_use_id,
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

    def test_entries_always_carry_identity_keys(self):
        outbox = SessionOutbox()
        outbox.apply_task_event({"type": "task_progress", "task_id": "bare"})
        entry = outbox.snapshot_active_tasks()[0]
        assert entry["name"] is None
        assert entry["tool_use_id"] is None

    def test_spawn_name_joins_task_by_tool_use_id(self):
        outbox = SessionOutbox()
        outbox.note_agent_spawn("toolu_a", "worker-a")
        outbox.apply_task_event(
            {"type": "task_started", "task_id": "t1", "tool_use_id": "toolu_a"}
        )
        entry = outbox.active_tasks["t1"]
        assert entry["name"] == "worker-a"
        assert entry["tool_use_id"] == "toolu_a"

    def test_unnamed_spawn_has_no_name(self):
        outbox = SessionOutbox()
        outbox.apply_task_event(
            {"type": "task_started", "task_id": "t1", "tool_use_id": "toolu_x"}
        )
        assert outbox.active_tasks["t1"]["name"] is None
        assert outbox.active_tasks["t1"]["tool_use_id"] == "toolu_x"

    def test_resumed_task_keeps_name_and_spawn_id(self):
        """A SendMessage resume re-announces the SAME task id under the
        SendMessage call's id (observed on CLI 2.1.283). Name and spawn id
        survive even though the entry was dropped when the first run finished:
        the resumed run's own messages still hang off the spawning call."""
        outbox = SessionOutbox()
        outbox.note_agent_spawn("toolu_spawn", "worker-a")
        outbox.apply_task_event(
            {"type": "task_started", "task_id": "t1", "tool_use_id": "toolu_spawn"}
        )
        outbox.apply_task_event(
            {"type": "task_notification", "task_id": "t1", "status": "completed"}
        )
        assert "t1" not in outbox.active_tasks

        outbox.apply_task_event(
            {"type": "task_started", "task_id": "t1", "tool_use_id": "toolu_send"}
        )
        entry = outbox.active_tasks["t1"]
        assert entry["name"] == "worker-a"
        assert entry["tool_use_id"] == "toolu_spawn"

    def test_late_spawn_note_labels_registered_task(self):
        """Out-of-order delivery: task_started before its spawning call."""
        outbox = SessionOutbox()
        outbox.apply_task_event(
            {"type": "task_started", "task_id": "t1", "tool_use_id": "toolu_a"}
        )
        outbox.note_agent_spawn("toolu_a", "worker-a")
        assert outbox.active_tasks["t1"]["name"] == "worker-a"

    def test_events_without_identity_do_not_clobber_it(self):
        outbox = SessionOutbox()
        outbox.note_agent_spawn("toolu_a", "worker-a")
        outbox.apply_task_event(
            {"type": "task_started", "task_id": "t1", "tool_use_id": "toolu_a"}
        )
        # task_updated carries no tool_use_id; progress may omit it.
        outbox.apply_task_event(
            {"type": "task_updated", "task_id": "t1", "status": "running"}
        )
        outbox.apply_task_event(
            {"type": "task_progress", "task_id": "t1", "tool_use_id": None}
        )
        entry = outbox.active_tasks["t1"]
        assert (entry["name"], entry["tool_use_id"]) == ("worker-a", "toolu_a")

    def test_name_maps_are_bounded(self):
        outbox = SessionOutbox()
        limit = session_outbox_module._AGENT_NAME_MAP_MAX
        for i in range(limit + 10):
            outbox.note_agent_spawn(f"toolu_{i}", f"w{i}")
            outbox.apply_task_event(
                {
                    "type": "task_started",
                    "task_id": f"t{i}",
                    "tool_use_id": f"toolu_{i}",
                }
            )
            outbox.apply_task_event(
                {"type": "task_updated", "task_id": f"t{i}", "status": "completed"}
            )
        assert len(outbox._spawn_names) == limit
        assert len(outbox._task_names) == limit
        # Oldest evicted first; the newest are still resolvable.
        assert outbox.agent_name_for("t0", "toolu_0") is None
        newest = limit + 9
        assert outbox.agent_name_for(f"t{newest}") == f"w{newest}"


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

    def test_agent_call_object_blocks_name_the_task(self):
        """Shape of _convert_message(AssistantMessage): a dict whose content
        still holds SDK block objects."""
        session = Session(session_id="s-turn")
        apply_turn_task_chunk(
            session,
            {
                "type": "assistant",
                "content": [TextBlock(text="spawning"), _agent_call("toolu_a")],
                "parent_tool_use_id": None,
            },
        )
        apply_turn_task_chunk(
            session,
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": "t1",
                "tool_use_id": "toolu_a",
                "description": "count primes",
                "data": {"task_type": "local_agent"},
            },
        )
        entry = get_outbox(session).active_tasks["t1"]
        assert entry["name"] == "worker-a"
        assert entry["tool_use_id"] == "toolu_a"
        assert entry["task_type"] == "local_agent"

    def test_agent_call_dict_blocks_and_task_spelling(self):
        """Raw dict blocks, the legacy ``Task`` spelling, and a tool_use_id
        that only rides in the raw ``data`` payload."""
        session = Session(session_id="s-turn")
        apply_turn_task_chunk(
            session,
            {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_b",
                        "name": "Task",
                        "input": {"name": "worker-b", "prompt": "p"},
                    }
                ],
            },
        )
        apply_turn_task_chunk(
            session,
            {
                "subtype": "task_started",
                "task_id": "t2",
                "description": "d",
                "data": {"tool_use_id": "toolu_b"},
            },
        )
        entry = get_outbox(session).active_tasks["t2"]
        assert (entry["name"], entry["tool_use_id"]) == ("worker-b", "toolu_b")

    def test_nested_spawn_inside_subagent_counts(self):
        session = Session(session_id="s-turn")
        apply_turn_task_chunk(
            session,
            {
                "type": "assistant",
                "content": [_agent_call("toolu_child", name="grandchild")],
                "parent_tool_use_id": "toolu_parent",
            },
        )
        apply_turn_task_chunk(
            session,
            {"subtype": "task_started", "task_id": "t3", "tool_use_id": "toolu_child"},
        )
        assert get_outbox(session).active_tasks["t3"]["name"] == "grandchild"

    def test_resume_through_turn_chunks_keeps_name(self):
        """The live CLI 2.1.283 sequence for a SendMessage resume."""
        session = Session(session_id="s-turn")
        apply_turn_task_chunk(
            session, {"type": "assistant", "content": [_agent_call("toolu_spawn")]}
        )
        for chunk in (
            {"subtype": "task_started", "task_id": "t1", "tool_use_id": "toolu_spawn"},
            {"subtype": "task_notification", "task_id": "t1", "status": "completed"},
            {
                "type": "assistant",
                "content": [
                    ToolUseBlock(
                        id="toolu_send",
                        name="SendMessage",
                        input={"to": "worker-a", "message": "again"},
                    )
                ],
            },
            {"subtype": "task_started", "task_id": "t1", "tool_use_id": "toolu_send"},
        ):
            apply_turn_task_chunk(session, chunk)
        entry = get_outbox(session).active_tasks["t1"]
        assert (entry["name"], entry["tool_use_id"]) == ("worker-a", "toolu_spawn")

    def test_only_named_subagent_calls_are_recorded(self):
        session = Session(session_id="s-turn")
        apply_turn_task_chunk(
            session,
            {
                "type": "assistant",
                "content": [
                    ToolUseBlock(
                        id="toolu_1", name="Bash", input={"name": "not-agent"}
                    ),
                    _agent_call("toolu_2", name=None),  # unnamed spawn
                    _agent_call("toolu_3", name=123),  # not a string
                    _agent_call("toolu_4", name=""),
                    _agent_call("toolu_5", name="x" * 1000),  # not a CLI name
                    ToolUseBlock(id="", name="Agent", input={"name": "no-id"}),
                ],
            },
        )
        # Nothing worth recording → no outbox is even created.
        assert getattr(session, "outbox", None) is None

    def test_spawn_tracking_never_raises(self):
        class ExplodingBlock:
            @property
            def name(self):
                raise RuntimeError("boom")

        session = Session(session_id="s-turn")
        apply_turn_task_chunk(
            session, {"type": "assistant", "content": [ExplodingBlock()]}
        )  # must not raise
        assert getattr(session, "outbox", None) is None


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

    def test_task_events_carry_tool_use_id(self):
        started = _message_to_event(_task_started(tool_use_id="toolu_1"))
        assert started["tool_use_id"] == "toolu_1"
        assert _message_to_event(_task_started())["tool_use_id"] is None

        progress = _task_progress()
        progress.tool_use_id = "toolu_1"
        assert _message_to_event(progress)["tool_use_id"] == "toolu_1"

        notification = _task_notification()
        notification.tool_use_id = "toolu_1"
        assert _message_to_event(notification)["tool_use_id"] == "toolu_1"

        # TaskUpdatedMessage has no attribute for it: read the raw payload.
        updated = _task_updated()
        assert _message_to_event(updated)["tool_use_id"] is None
        updated.data = {"tool_use_id": "toolu_2"}
        assert _message_to_event(updated)["tool_use_id"] == "toolu_2"


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

    async def test_spawn_names_the_task_and_its_outbox_event(self):
        client = FakeSDKClient()
        session = _make_session(client)
        resume_idle_reader(session)
        await client.queue.put(_spawn_message(_agent_call("toolu_b", name="worker-b")))
        await client.queue.put(_task_started(task_id="t9", tool_use_id="toolu_b"))
        await client.queue.put(_task_started(task_id="t10", tool_use_id="toolu_z"))
        outbox = get_outbox(session)
        await _drain_until(lambda: outbox.next_seq > 2)

        named, unnamed = outbox.events_after(0)
        assert (named["name"], named["tool_use_id"]) == ("worker-b", "toolu_b")
        # The key is always present on task_started events.
        assert (unnamed["name"], unnamed["tool_use_id"]) == (None, "toolu_z")
        entry = outbox.active_tasks["t9"]
        assert (entry["name"], entry["tool_use_id"]) == ("worker-b", "toolu_b")
        await pause_idle_reader(session)

    async def test_subagent_spawn_is_noted_but_not_forwarded(self):
        client = FakeSDKClient()
        session = _make_session(client)
        resume_idle_reader(session)
        await client.queue.put(
            _spawn_message(
                TextBlock(text="delegating"),
                _agent_call("toolu_c", name="helper"),
                parent_tool_use_id="toolu_parent",
            )
        )
        await client.queue.put(_task_started(task_id="t11", tool_use_id="toolu_c"))
        outbox = get_outbox(session)
        await _drain_until(lambda: outbox.next_seq > 1)
        # Subagent narration stays out of the outbox; its spawn still counts.
        assert [e["type"] for e in outbox.events_after(0)] == ["task_started"]
        assert outbox.active_tasks["t11"]["name"] == "helper"
        await pause_idle_reader(session)


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

    def test_active_tasks_expose_name_and_tool_use_id(
        self, pending_events_client, stored_session
    ):
        outbox = get_outbox(stored_session)
        outbox.note_agent_spawn("toolu_a", "worker-a")
        outbox.apply_task_event(
            outbox.append(
                {
                    "type": "task_started",
                    "task_id": "t1",
                    "tool_use_id": "toolu_a",
                    "description": "d",
                }
            )
        )
        body = pending_events_client.get(
            "/v1/sessions/sess-pe/pending-events", params={"user": "alice"}
        ).json()
        task = body["active_tasks"][0]
        assert (task["name"], task["tool_use_id"]) == ("worker-a", "toolu_a")

    def test_turn_in_progress_flag(self, pending_events_client, stored_session):
        stored_session.active_response_id = "resp_sess-pe_3"
        try:
            body = pending_events_client.get(
                "/v1/sessions/sess-pe/pending-events"
            ).json()
            assert body["turn_in_progress"] is True
        finally:
            stored_session.active_response_id = None


# ---------------------------------------------------------------------------
# Turn-start backlog sweep
# ---------------------------------------------------------------------------


class TestBacklogDrain:
    @pytest.mark.asyncio
    async def test_drains_stale_backlog_into_outbox(self):
        """Messages piled while no reader was attached go to the outbox, not
        to the next turn's reader — including a stale ResultMessage that would
        otherwise terminate the next turn's receive_response() early."""
        client = FakeSDKClient()
        session = _make_session(client)
        await client.queue.put(_task_started())
        await client.queue.put(_assistant())
        await client.queue.put(_result())

        captured = await drain_backlog_to_outbox(session, client)

        assert captured == 3
        types = [e["type"] for e in get_outbox(session).events_after(0)]
        assert types == ["task_started", "assistant_message", "turn_result"]
        # Nothing left in the stream for the next turn's reader to steal.
        assert client.queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_quiet_stream_returns_zero(self):
        client = FakeSDKClient()
        session = _make_session(client)
        assert await drain_backlog_to_outbox(session, client) == 0
        assert get_outbox(session).events_after(0) == []

    @pytest.mark.asyncio
    async def test_clients_without_receive_messages_are_noops(self):
        session = _make_session(None)
        assert await drain_backlog_to_outbox(session, None) == 0
        assert await drain_backlog_to_outbox(session, object()) == 0

    async def test_drain_notes_agent_spawns(self):
        client = FakeSDKClient()
        session = _make_session(client)
        await client.queue.put(_spawn_message(_agent_call("toolu_d", name="worker-d")))
        await client.queue.put(_task_started(task_id="t12", tool_use_id="toolu_d"))

        assert await drain_backlog_to_outbox(session, client) == 2
        assert get_outbox(session).active_tasks["t12"]["name"] == "worker-d"

    @pytest.mark.asyncio
    async def test_stream_end_stops_drain(self):
        client = FakeSDKClient()
        session = _make_session(client)
        await client.queue.put(_assistant())
        await client.queue.put(None)  # sentinel: stream closed

        captured = await drain_backlog_to_outbox(session, client)

        assert captured == 1
        types = [e["type"] for e in get_outbox(session).events_after(0)]
        assert types == ["assistant_message"]


# ---------------------------------------------------------------------------
# Registry reset when the CLI that owns the tasks goes away
# ---------------------------------------------------------------------------


class _BreakingSDKClient:
    """Yields the given messages, then the stream breaks."""

    def __init__(self, *messages):
        self.messages = messages

    async def receive_messages(self):
        for message in self.messages:
            yield message
        raise RuntimeError("stream broke")


class _DisconnectableClient:
    def __init__(self):
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


def _register(session, task_id, tool_use_id=None):
    get_outbox(session).apply_task_event(
        {"type": "task_started", "task_id": task_id, "tool_use_id": tool_use_id}
    )


class TestRegistryResetWithClient:
    def test_reset_drops_tasks_but_keeps_agent_names(self):
        session = _make_session(None)
        outbox = get_outbox(session)
        outbox.note_agent_spawn("toolu_n", "worker-n")
        _register(session, "t21", "toolu_n")

        reset_active_tasks(session, "test")

        assert outbox.snapshot_active_tasks() == []
        # A resumed conversation re-announces the agent under its task id.
        _register(session, "t21", "toolu_send")
        assert outbox.active_tasks["t21"]["name"] == "worker-n"

    def test_reset_without_outbox_creates_none(self):
        session = _make_session(None)

        reset_active_tasks(session, "test")

        assert session.outbox is None

    async def test_idle_reader_error_resets_registry(self):
        client = _BreakingSDKClient(
            _spawn_message(_agent_call("toolu_r", name="worker-r")),
            _task_started(task_id="t20", tool_use_id="toolu_r"),
        )
        session = _make_session(client)
        outbox = get_outbox(session)

        assert resume_idle_reader(session) is True
        await _drain_until(lambda: not idle_reader_running(session))

        types = [e["type"] for e in outbox.events_after(0)]
        assert types == ["task_started", "reader_error"]
        assert outbox.snapshot_active_tasks() == []

    async def test_disconnecting_the_session_client_resets_registry(self):
        from src.routes.responses import _disconnect_session_client

        client = _DisconnectableClient()
        session = _make_session(client)
        _register(session, "t22")

        await _disconnect_session_client(session, "stream failure")

        assert session.client is None
        assert client.disconnected is True
        assert get_outbox(session).snapshot_active_tasks() == []

    async def test_disconnecting_a_superseded_client_keeps_live_registry(self):
        from src.routes.responses import _disconnect_session_client

        old, live = _DisconnectableClient(), _DisconnectableClient()
        session = _make_session(live)
        _register(session, "t23")

        await _disconnect_session_client(session, "old teardown", client=old)

        assert old.disconnected is True
        assert session.client is live
        assert "t23" in get_outbox(session).active_tasks

    async def test_fresh_client_replacement_resets_registry(self):
        from src.backend_registry import ResolvedModel
        from src.constants import DEFAULT_MODEL
        from src.response_models import ResponseCreateRequest
        from src.routes.responses import _ensure_response_session_client

        session = _make_session(None)
        _register(session, "t24")
        backend = MagicMock()
        backend.create_client = AsyncMock(return_value=object())
        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="hello")
        resolved = ResolvedModel(DEFAULT_MODEL, "claude", DEFAULT_MODEL)

        with patch("src.routes.responses.get_mcp_servers", return_value={}):
            await _ensure_response_session_client(
                body, resolved, backend, session, "sess-outbox", False, None, "/tmp/ws"
            )

        backend.create_client.assert_awaited_once()
        assert get_outbox(session).snapshot_active_tasks() == []


class _ParseFailingSDKClient:
    """Yields the given messages, then its parse layer rejects a frame."""

    def __init__(self, *messages):
        self.messages = messages

    async def receive_messages(self):
        from claude_agent_sdk._errors import MessageParseError

        for message in self.messages:
            yield message
        raise MessageParseError("malformed frame", {"type": "assistant"})


class TestReaderFailureScope:
    async def test_parse_error_keeps_the_live_registry(self):
        # A malformed frame kills only the gateway's receive_messages()
        # generator: the SDK stream, the CLI and its tasks keep running.
        client = _ParseFailingSDKClient(_task_started(task_id="t30"))
        session = _make_session(client)
        outbox = get_outbox(session)

        assert resume_idle_reader(session) is True
        await _drain_until(lambda: not idle_reader_running(session))

        assert [e["type"] for e in outbox.events_after(0)] == [
            "task_started",
            "reader_error",
        ]
        entry = outbox.active_tasks["t30"]
        assert entry["task_type"] == "local_agent"

    async def test_stream_end_resets_registry(self):
        # The SDK ends the stream once its reader is done: the CLI exited.
        client = FakeSDKClient()
        session = _make_session(client)
        outbox = get_outbox(session)
        assert resume_idle_reader(session) is True

        await client.queue.put(_task_started(task_id="t31"))
        await _drain_until(lambda: "t31" in outbox.active_tasks)
        await client.queue.put(None)  # sentinel: stream closed
        await _drain_until(lambda: not idle_reader_running(session))

        assert outbox.snapshot_active_tasks() == []

    def test_sdk_parse_layer_raises_the_exempt_error_type(self):
        # Pins the exemption above: an SDK that moves or renames the parse
        # error would silently turn parse failures back into registry wipes.
        from claude_agent_sdk._errors import MessageParseError
        from claude_agent_sdk._internal.message_parser import parse_message

        with pytest.raises(MessageParseError):
            parse_message({"type": "assistant"})


class TestResumeBetweenTurns:
    async def test_a_turn_holding_the_session_lock_blocks_the_reader(self):
        # Non-streaming turns hold session.lock without active_response_id.
        client = FakeSDKClient()
        session = _make_session(client)

        async with session.lock:
            assert resume_idle_reader_between_turns(session) is False
            assert not idle_reader_running(session)

        assert resume_idle_reader_between_turns(session) is True
        await pause_idle_reader(session)

    def test_pending_events_during_a_locked_turn_leaves_the_reader_off(
        self, pending_events_client, stored_session
    ):
        stored_session.client = FakeSDKClient()
        with patch.object(stored_session.lock, "locked", return_value=True):
            body = pending_events_client.get(
                "/v1/sessions/sess-pe/pending-events", params={"user": "alice"}
            ).json()

        assert body["turn_in_progress"] is True
        assert body["reader_active"] is False
