"""Drain state of this instance (``docs/zero-downtime-deploy.md`` §4 step 4).

``POST /internal/drain`` fixes a **drain target** (instance name + boot id),
validated before anything changes: not this instance, alive, and answering
``/internal/ping`` with that boot id. While draining, new sessions, sessions this
instance no longer holds, and admin writes go to the target — never to the
nginx "active" color, which this instance does not read. A background task
releases idle sessions as soon as they are idle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from src import instance_registry
from src.handoff import peer

logger = logging.getLogger(__name__)

_RELEASE_INTERVAL_SECONDS = 2.0


class DrainError(ValueError):
    pass


class DrainState:
    def __init__(self) -> None:
        self.target: Optional[instance_registry.Instance] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def draining(self) -> bool:
        return self.target is not None

    def describe_target(self) -> Optional[Dict[str, str]]:
        if self.target is None:
            return None
        return {"name": self.target.name, "boot_id": self.target.boot_id}

    async def start(self, name: str, boot_id: str) -> None:
        """Validate and fix the drain target (also re-targets a running drain)."""
        if not name or not boot_id:
            raise DrainError("target and target_boot_id are required")
        if name == instance_registry.instance_name():
            raise DrainError("an instance cannot drain into itself")
        instance = instance_registry.lookup(name)
        if instance is None:
            raise DrainError(f"target {name!r} is not running")
        if instance.boot_id != boot_id:
            raise DrainError(
                f"target {name!r} runs boot {instance.boot_id[:8]}, not {boot_id[:8]}"
            )
        try:
            res = await peer.call(instance, "GET", "/internal/ping", timeout=3.0)
            seen = res.json().get("boot_id") if res.status_code == 200 else None
        except Exception as exc:  # unreachable, bad JSON
            raise DrainError(f"target {name!r} did not answer /internal/ping: {exc}") from exc
        if seen != boot_id:
            raise DrainError(f"target {name!r} answered boot {str(seen)[:8]}")
        self.target = instance
        logger.warning("Draining into %s (boot %s)", name, boot_id[:8])
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._release_loop())

    async def stop(self) -> None:
        self.target = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        logger.warning("Drain cancelled; accepting new sessions again")

    def live_target(self) -> Optional[instance_registry.Instance]:
        """The target while it is still the same live process, else None."""
        if self.target is None:
            return None
        now = instance_registry.lookup(self.target.name)
        if now is None or now.boot_id != self.target.boot_id:
            return None
        return now

    async def _release_loop(self) -> None:
        from src.session_manager import session_manager

        while self.target is not None:
            try:
                for sid in list(session_manager.sessions):
                    await session_manager.release_for_handoff(sid)
            except Exception:
                logger.exception("Drain release pass failed")
            await asyncio.sleep(_RELEASE_INTERVAL_SECONDS)


drain_state = DrainState()


def snapshot(forwarding: int, open_requests: int) -> Dict[str, Any]:
    """``GET /health/drain``."""
    from src.session_manager import session_manager

    try:
        # Live background tasks — the run registry also keeps failed runs.
        from src.routes.responses import _BACKGROUND_RESPONSE_TASKS

        background = sum(1 for t in _BACKGROUND_RESPONSE_TASKS if not t.done())
    except Exception:
        background = 0
    sessions = list(session_manager.sessions.values())
    return {
        "instance": instance_registry.instance_name(),
        "boot_id": instance_registry.BOOT_ID,
        "draining": drain_state.draining,
        "drain_target": drain_state.describe_target(),
        "active_turns": sum(1 for s in sessions if s.active_response_id is not None),
        "background_turns": background,
        "pending_interactions": sum(
            1 for s in sessions if s.pending_tool_call is not None or s.input_event is not None
        ),
        "forwarding": forwarding,
        "open_requests": open_requests,
        "owned_sessions": len(sessions),
    }
