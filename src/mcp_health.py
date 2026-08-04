"""Cached MCP reachability, safe to poll from a UI.

``POST /admin/api/mcp-servers/{name}/test`` probes one server on demand: it is
admin-only, serial, and each stdio probe spawns a process. A client that wants
to show "which MCP servers are actually up" needs the opposite shape — one cheap
call, for every server, poll-safe.

So this module keeps a **snapshot**: probes run at most once per
``MCP_HEALTH_TTL_SECONDS`` (bounded concurrency), and readers always get the
last known result immediately. A stale snapshot triggers a background refresh
and is still returned — a poll never waits on a process spawn. Before the first
probe completes, servers report ``unknown`` rather than a guess.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from src.constants import MCP_HEALTH_MAX_CONCURRENCY, MCP_HEALTH_TTL_SECONDS

logger = logging.getLogger(__name__)

# name -> {status, detail, latency_ms, transport, checked_at}
_snapshot: Dict[str, Dict[str, Any]] = {}
_checked_at: float = 0.0
_refresh_task: Optional["asyncio.Task[None]"] = None


def _server_names() -> List[str]:
    """Every server that could be attached to a session, config + plugin alike."""
    from src.mcp_config import get_mcp_servers

    names = list(get_mcp_servers().keys())
    try:
        from src.plugin_service import list_plugin_mcp_servers

        names.extend(
            str(s.get("server_name"))
            for s in list_plugin_mcp_servers()
            if s.get("server_name")
        )
    except Exception:  # noqa: BLE001 — plugin registry problems are not fatal here
        logger.debug("plugin MCP listing failed during health sweep", exc_info=True)
    return list(dict.fromkeys(n for n in names if n))


async def _probe(name: str, semaphore: asyncio.Semaphore) -> None:
    from src import mcp_admin_service

    async with semaphore:
        try:
            result = await mcp_admin_service.test_connection(name)
        except Exception as exc:  # noqa: BLE001 — test_connection is no-raise, belt and braces
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    _snapshot[name] = {
        "status": "up" if result.get("ok") else "down",
        # Secrets never reach ``detail`` (mcp_connection_test redacts config).
        "detail": str(result.get("detail") or "")[:300],
        "latency_ms": result.get("latency_ms"),
        "transport": result.get("transport", ""),
        "checked_at": time.time(),
    }


async def _sweep() -> None:
    global _checked_at
    names = _server_names()
    semaphore = asyncio.Semaphore(max(1, MCP_HEALTH_MAX_CONCURRENCY))
    await asyncio.gather(*(_probe(name, semaphore) for name in names))
    # Servers that disappeared from config drop out of the snapshot.
    for gone in set(_snapshot) - set(names):
        _snapshot.pop(gone, None)
    _checked_at = time.time()


def _start_refresh() -> bool:
    """Start a sweep unless one is already running. True when one is in flight."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return True

    async def runner() -> None:
        try:
            await _sweep()
        except Exception:  # noqa: BLE001 — a failed sweep keeps the old snapshot
            logger.warning("MCP health sweep failed", exc_info=True)

    _refresh_task = asyncio.create_task(runner())
    return True


async def get_health(refresh: bool = False) -> Dict[str, Any]:
    """Return the snapshot, refreshing in the background when stale.

    ``refresh=True`` forces a sweep and **waits** for it — for an operator who
    just fixed a server and wants the answer now, not on the next poll.
    """
    stale = refresh or not _snapshot or (time.time() - _checked_at) > MCP_HEALTH_TTL_SECONDS
    if refresh:
        await _sweep()
        refreshing = False
    elif stale:
        refreshing = _start_refresh()
    else:
        refreshing = _refresh_task is not None and not _refresh_task.done()

    names = _server_names()
    servers = [
        {
            "name": name,
            **_snapshot.get(
                name,
                {
                    "status": "unknown",
                    "detail": "아직 확인하지 않음",
                    "latency_ms": None,
                    "transport": "",
                    "checked_at": None,
                },
            ),
        }
        for name in names
    ]
    return {
        "servers": servers,
        "total": len(servers),
        "checked_at": _checked_at or None,
        "ttl_seconds": MCP_HEALTH_TTL_SECONDS,
        "refreshing": refreshing,
    }


def reset_for_tests() -> None:
    """Drop the snapshot (tests only)."""
    global _checked_at, _refresh_task
    _snapshot.clear()
    _checked_at = 0.0
    _refresh_task = None
