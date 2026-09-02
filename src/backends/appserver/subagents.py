"""Codex subagent thread/activity -> canonical task-event normalization (#173 §5).

Codex supports native multi-agent/subagent threads. This module normalizes the
native subagent lifecycle notifications into a small vendor-neutral event that
:class:`~src.backends.appserver.events.TurnMapper` turns into the canonical
``response.task_*`` events the ChatDRAGON UI already renders. The adapter maps
Codex *semantics*, not Claude tool names (``Task``/``Agent``).

Native identity is preserved (issue §5): the child thread id becomes the
canonical ``task_id`` and the parent thread id is carried so nested agents
render under the correct parent rather than being flattened onto the leader.
Fields Codex does not expose (usage, description, role) stay absent -- they are
never invented for symmetry with Claude.

The native method/field spellings follow the current app-server conventions and
the caveat in ``docs/codex`` (recheck when the protocol changes). Recognition is
intentionally tolerant of several spellings so a minor upstream rename degrades
to "no subagent rows" rather than a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

# Notifications that announce a spawned child thread.
SPAWN_METHODS = {"thread/started", "thread/spawned", "thread/spawn"}
# Item-carrying notifications that may hold subagent activity.
ITEM_METHODS = {"item/started", "item/updated", "item/completed"}
# Item types that denote subagent activity.
SUBAGENT_ITEM_TYPES = {
    "subAgentActivity",
    "subagentActivity",
    "subAgent",
    "threadSpawn",
}

# Terminal vs live activity states, mapped onto canonical task statuses.
_TERMINAL_STATES = {
    "completed": "completed",
    "succeeded": "completed",
    "success": "completed",
    "failed": "failed",
    "error": "failed",
    "interrupted": "killed",
    "cancelled": "killed",
    "canceled": "killed",
    "closed": "stopped",
    "stopped": "stopped",
}
_LIVE_STATES = {"started", "interacted", "running", "active", "progress"}


@dataclass(frozen=True)
class SubAgentEvent:
    """A normalized subagent lifecycle event.

    Attributes:
        kind: ``spawned`` (first appearance), ``progress`` (intermediate), or
            ``terminal`` (child ended).
        child_id: stable child-thread-derived id -> canonical ``task_id``.
        parent_id: parent thread id for nesting (``None`` for a top-level child).
        status: canonical terminal status when ``kind == "terminal"``.
        description: human label when upstream exposes one, else ``None``.
        role: subagent role/nickname when exposed, else ``None``.
    """

    kind: str
    child_id: str
    parent_id: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None
    role: Optional[str] = None


def _first_str(source: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _from_thread(thread: Dict[str, Any]) -> Optional[SubAgentEvent]:
    child_id = _first_str(thread, "id", "threadId")
    parent_id = _first_str(thread, "parentThreadId", "parent_thread_id")
    # A spawn notification without a parent id is the root thread, not a child.
    if not child_id or not parent_id:
        return None
    return SubAgentEvent(
        kind="spawned",
        child_id=child_id,
        parent_id=parent_id,
        description=_first_str(thread, "description", "title", "path"),
        role=_first_str(thread, "role", "nickname", "name"),
    )


def _from_item(item: Dict[str, Any]) -> Optional[SubAgentEvent]:
    if item.get("type") not in SUBAGENT_ITEM_TYPES:
        return None
    child_id = _first_str(
        item, "threadId", "childThreadId", "child_thread_id", "targetThreadId", "id"
    )
    if not child_id:
        return None
    parent_id = _first_str(item, "parentThreadId", "parent_thread_id")
    role = _first_str(item, "role", "nickname", "name")
    description = _first_str(item, "description", "title")
    raw_state = (
        _first_str(item, "state", "status", "activity", "subAgentState") or ""
    ).lower()
    if raw_state in _TERMINAL_STATES:
        return SubAgentEvent(
            kind="terminal",
            child_id=child_id,
            parent_id=parent_id,
            status=_TERMINAL_STATES[raw_state],
            description=description,
            role=role,
        )
    if raw_state == "started":
        return SubAgentEvent(
            kind="spawned",
            child_id=child_id,
            parent_id=parent_id,
            description=description,
            role=role,
        )
    # interacted / running / unknown-but-live -> progress
    return SubAgentEvent(
        kind="progress",
        child_id=child_id,
        parent_id=parent_id,
        description=description,
        role=role,
    )


def normalize_subagent_event(
    method: str, params: Dict[str, Any]
) -> Optional[SubAgentEvent]:
    """Return a normalized subagent event for a native notification, or None.

    Recognizes both a dedicated spawn notification (``thread/started`` carrying a
    thread with a ``parentThreadId``) and ``SubAgentActivity`` items. Any
    notification that is not subagent-related returns ``None`` so the caller
    falls through to normal turn handling.
    """
    if method in SPAWN_METHODS:
        thread = params.get("thread")
        if isinstance(thread, dict):
            return _from_thread(thread)
        return None
    if method in ITEM_METHODS:
        item = params.get("item")
        if isinstance(item, dict):
            return _from_item(item)
    return None
