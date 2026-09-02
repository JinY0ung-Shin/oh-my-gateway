"""Unit tests for Codex subagent -> canonical task-event mapping (issue #173 §5).

Covers the pure normalization (native notification -> SubAgentEvent) and the
TurnMapper's task-event emission, including nested identity preservation and the
"no forever-running row" terminalization on turn end / failure.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.backends.appserver.events import TurnMapper
from src.backends.appserver.subagents import normalize_subagent_event


def _mapper() -> TurnMapper:
    return TurnMapper(thread_id="thread-1", turn_id="turn-1")


def _drain(
    mapper: TurnMapper, method: str, params: Dict[str, Any]
) -> List[Dict[str, Any]]:
    return list(mapper.map_notification(method, params))


# -- normalization -----------------------------------------------------------


def test_spawn_notification_with_parent_is_a_child():
    event = normalize_subagent_event(
        "thread/started",
        {"thread": {"id": "child-1", "parentThreadId": "thread-1", "role": "explorer"}},
    )
    assert event is not None
    assert event.kind == "spawned"
    assert event.child_id == "child-1"
    assert event.parent_id == "thread-1"
    assert event.role == "explorer"


def test_root_thread_start_is_not_a_subagent():
    # No parentThreadId -> the root thread, not a child.
    assert (
        normalize_subagent_event("thread/started", {"thread": {"id": "thread-1"}})
        is None
    )


def test_subagent_activity_item_states():
    started = normalize_subagent_event(
        "item/started",
        {"item": {"type": "subAgentActivity", "threadId": "c1", "state": "started"}},
    )
    assert started is not None and started.kind == "spawned"
    interacted = normalize_subagent_event(
        "item/updated",
        {"item": {"type": "subAgentActivity", "threadId": "c1", "state": "interacted"}},
    )
    assert interacted is not None and interacted.kind == "progress"
    interrupted = normalize_subagent_event(
        "item/completed",
        {
            "item": {
                "type": "subAgentActivity",
                "threadId": "c1",
                "state": "interrupted",
            }
        },
    )
    assert interrupted is not None and interrupted.kind == "terminal"
    assert interrupted.status == "killed"


def test_non_subagent_notification_is_ignored():
    assert normalize_subagent_event("item/agentMessage/delta", {"delta": "hi"}) is None


# -- mapper task events ------------------------------------------------------


def test_child_spawn_maps_to_task_started_with_nested_identity():
    mapper = _mapper()
    chunks = _drain(
        mapper,
        "thread/started",
        {"thread": {"id": "child-1", "parentThreadId": "thread-1", "role": "explorer"}},
    )
    assert len(chunks) == 1
    started = chunks[0]
    assert started["type"] == "system"
    assert started["subtype"] == "task_started"
    assert started["task_id"] == "child-1"
    assert started["task_type"] == "local_agent"
    assert started["parent_tool_use_id"] == "thread-1"
    assert started["subagent_type"] == "explorer"


def test_nested_child_preserves_its_own_parent():
    mapper = _mapper()
    # child-1 under root, child-2 under child-1 (depth 2).
    _drain(
        mapper,
        "thread/started",
        {"thread": {"id": "child-1", "parentThreadId": "thread-1"}},
    )
    chunks = _drain(
        mapper,
        "thread/started",
        {"thread": {"id": "child-2", "parentThreadId": "child-1"}},
    )
    assert chunks[0]["task_id"] == "child-2"
    assert chunks[0]["parent_tool_use_id"] == "child-1"


def test_child_completion_maps_to_task_notification():
    mapper = _mapper()
    _drain(
        mapper, "thread/started", {"thread": {"id": "c1", "parentThreadId": "thread-1"}}
    )
    chunks = _drain(
        mapper,
        "item/completed",
        {"item": {"type": "subAgentActivity", "threadId": "c1", "state": "completed"}},
    )
    assert chunks[0]["subtype"] == "task_notification"
    assert chunks[0]["status"] == "completed"
    assert chunks[0]["task_id"] == "c1"


def test_turn_completion_terminalizes_still_open_children():
    mapper = _mapper()
    _drain(
        mapper, "thread/started", {"thread": {"id": "c1", "parentThreadId": "thread-1"}}
    )
    # The turn ends while c1 is still open.
    chunks = _drain(
        mapper, "turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}
    )
    task_updates = [c for c in chunks if c.get("subtype") == "task_updated"]
    assert len(task_updates) == 1
    assert task_updates[0]["task_id"] == "c1"
    assert task_updates[0]["status"] == "stopped"
    assert task_updates[0]["parent_tool_use_id"] == "thread-1"


def test_turn_failure_terminalizes_open_children_as_failed():
    mapper = _mapper()
    _drain(
        mapper, "thread/started", {"thread": {"id": "c1", "parentThreadId": "thread-1"}}
    )
    chunks = _drain(
        mapper,
        "turn/completed",
        {"turn": {"id": "turn-1", "status": "failed", "error": {"message": "x"}}},
    )
    task_updates = [c for c in chunks if c.get("subtype") == "task_updated"]
    assert task_updates[0]["status"] == "failed"


def test_completed_child_is_not_terminalized_again_at_turn_end():
    mapper = _mapper()
    _drain(
        mapper, "thread/started", {"thread": {"id": "c1", "parentThreadId": "thread-1"}}
    )
    _drain(
        mapper,
        "item/completed",
        {"item": {"type": "subAgentActivity", "threadId": "c1", "state": "completed"}},
    )
    chunks = _drain(
        mapper, "turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}
    )
    assert not [c for c in chunks if c.get("subtype") == "task_updated"]
