"""Gateway-owned ``ClaudeSDKClient`` that surfaces the CLI's ``tool_progress`` frames.

Why this exists: during a long tool call (a slow MCP server, a minutes-long
Bash job) the CLI emits ``tool_progress`` messages — ``mcp_progress``,
``bash_progress`` … — carrying ``elapsed_time_seconds``. The pinned
``claude-agent-sdk`` (see pyproject) parses stream-json with a forward-compatible
``case _: return None``: an unknown message type is *silently dropped*, so those
frames never reach the gateway and the turn looks wedged for exactly as long as
the tool runs. Clients (ChatDRAGON) then cannot tell "tool still running" from
"stream dead", and the stall guard cannot either.

The override re-implements the SDK's four-line ``receive_messages`` on top of
the same private ``_query`` the SDK itself uses, adding one branch for
``tool_progress``. Everything else goes through the SDK's own ``parse_message``
unchanged. ``receive_response`` (what turns read) is inherited and iterates
``self.receive_messages()``, so it sees the new message too.

Deliberately narrow: the SDK pin is exact, an upgrade is a gap-analyzed event
(CLAUDE.md), and the day the SDK grows its own ``ToolProgressMessage`` this
module should be deleted in favour of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional

from claude_agent_sdk import ClaudeSDKClient

STEERING_HEADER = "X-Gateway-Steering-Run"


@dataclass
class SteeringInput:
    # Track one physical CLI across response segments, including an approval pause.

    run_id: str = ""
    accepting: bool = False
    submitted: dict[str, str] = field(default_factory=dict)
    pending: dict[str, str] = field(default_factory=dict)



@dataclass
class ToolProgressMessage:
    """A CLI ``tool_progress`` frame (shape after the TS SDK's
    ``SDKToolProgressMessage``): the tool is still running after
    ``elapsed_time_seconds``. Extra fields ride along in ``data``."""

    tool_use_id: str
    tool_name: str
    elapsed_time_seconds: float
    parent_tool_use_id: Optional[str] = None
    task_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


_KNOWN_FIELDS = frozenset(
    {
        "type",
        "tool_use_id",
        "tool_name",
        "elapsed_time_seconds",
        "parent_tool_use_id",
        "task_id",
    }
)


def parse_tool_progress(data: Dict[str, Any]) -> Optional[ToolProgressMessage]:
    """Build a :class:`ToolProgressMessage` from a raw CLI frame, or ``None``
    when the frame lacks the one field a client can key on (``tool_use_id``)."""
    tool_use_id = data.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    elapsed = data.get("elapsed_time_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        elapsed = 0.0
    parent = data.get("parent_tool_use_id")
    task_id = data.get("task_id")
    return ToolProgressMessage(
        tool_use_id=tool_use_id,
        tool_name=str(data.get("tool_name") or ""),
        elapsed_time_seconds=float(elapsed),
        parent_tool_use_id=parent if isinstance(parent, str) and parent else None,
        task_id=task_id if isinstance(task_id, str) and task_id else None,
        data={k: v for k, v in data.items() if k not in _KNOWN_FIELDS},
    )


class GatewayClaudeSDKClient(ClaudeSDKClient):
    """``ClaudeSDKClient`` whose message stream includes ``tool_progress``."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.steering = SteeringInput()

    async def receive_messages(self) -> AsyncIterator[Any]:
        query = getattr(self, "_query", None)
        if not query:
            # Same failure the SDK raises; import lazily like the SDK does.
            from claude_agent_sdk._errors import CLIConnectionError

            raise CLIConnectionError("Not connected. Call connect() first.")

        from claude_agent_sdk._internal.message_parser import parse_message

        async for data in query.receive_messages():
            if isinstance(data, dict) and data.get("type") == "user" and data.get("isReplay") is True:
                steer_id = data.get("uuid")
                if steer_id in self.steering.pending:
                    yield {"type": "steering_echo", "steer_id": steer_id}
                continue
            if isinstance(data, dict) and data.get("type") == "tool_progress":
                progress = parse_tool_progress(data)
                if progress is not None:
                    yield progress
                continue
            message = parse_message(data)
            if message is not None:
                yield message
