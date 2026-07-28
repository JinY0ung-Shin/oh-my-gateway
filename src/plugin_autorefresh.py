"""Periodic auto-refresh for gateway-managed plugin marketplaces.

The startup installer (``docker/install_plugins.py``) pins plugin versions to
container start, and the admin panel's per-marketplace Refresh button is the
only runtime update path. This module is the periodic version of that button:
a single poll loop re-reads the manifest's ``auto_refresh`` record every tick
(so the admin toggle takes effect without a restart) and, when enabled and an
interval has elapsed, refreshes every gateway-managed marketplace through
``plugin_admin_service.refresh_marketplace`` — the same fresh-clone +
``claude plugin update`` path the button uses.

Only gateway-managed marketplaces are refreshed: names recorded in the managed
manifest (admin-added). Marketplaces registered outside the gateway are left
alone.

The first automatic cycle runs one full interval after process start — the
startup installer has just refreshed everything, so refreshing again at boot
would be duplicate work. The admin API's "run now" trigger bypasses the timer.
"""

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src import plugin_manifest

logger = logging.getLogger(__name__)

# How often the loop re-reads the manifest config and checks whether a cycle is
# due. Short enough that an admin toggle feels immediate, long enough that an
# idle (disabled) poller costs nothing.
POLL_SECONDS = 30.0

# Bound on waiting out an in-flight refresh at shutdown. A cancelled cycle's
# current git/claude subprocess cannot be interrupted mid-thread, so give it a
# moment to finish and otherwise abandon the thread — the process is exiting.
_STOP_TIMEOUT_SECONDS = 5.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PluginAutoRefresher:
    """Poll loop + on-demand trigger for marketplace refresh cycles."""

    def __init__(self, poll_seconds: float = POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self._task: Optional[asyncio.Task] = None
        self._manual_task: Optional[asyncio.Task] = None
        self._cycle_lock = asyncio.Lock()
        # Interval baseline: monotonic drives scheduling, wall clock is kept
        # alongside purely so status() can show a next-run timestamp.
        self._baseline_monotonic = time.monotonic()
        self._baseline_wall = time.time()
        self._last_run_at: Optional[str] = None
        self._last_results: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the poller — call after the event loop is running.

        Idempotent only while a live task exists. A leftover *done* task (e.g.
        from a previous, now-closed loop when the singleton is reused across
        FastAPI lifespans in tests) is replaced rather than blocking start.
        """
        if self._task is not None and not self._task.done():
            return  # Already running on this loop

        try:
            loop = asyncio.get_running_loop()
            self._baseline_monotonic = time.monotonic()
            self._baseline_wall = time.time()
            self._task = loop.create_task(self._loop())
            logger.info(
                "Started plugin auto-refresh poller (poll every %.0fs; "
                "enabled/interval read from the manifest each tick)",
                self.poll_seconds,
            )
        except RuntimeError:
            logger.warning("No running event loop, plugin auto-refresh disabled")

    async def stop(self) -> None:
        """Cancel the poller and any in-flight manual cycle (bounded wait).

        Suppresses ``RuntimeError`` alongside the expected cancellation/timeout:
        cancelling a task bound to an already-closed loop (cross-lifespan singleton
        reuse) can raise it, and shutdown must not abort the rest of the chain.
        The task references are always cleared so a later ``start()`` succeeds.
        """
        for task in (self._task, self._manual_task):
            if task is None or task.done():
                continue
            task.cancel()
            with contextlib.suppress(
                asyncio.CancelledError, asyncio.TimeoutError, RuntimeError
            ):
                await asyncio.wait_for(task, timeout=_STOP_TIMEOUT_SECONDS)
        self._task = None
        self._manual_task = None

    def reset_schedule(self) -> None:
        """Restart the interval countdown from now.

        Called when the admin saves the config so enabling never fires an
        immediate cycle (the baseline would otherwise still sit at process start
        / last cycle end, already past the interval) and ``status().next_run_at``
        reflects the real next run instead of a past timestamp.
        """
        self._baseline_monotonic = time.monotonic()
        self._baseline_wall = time.time()

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.poll_seconds)
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Plugin auto-refresh tick failed, will retry next poll"
                    )
        except asyncio.CancelledError:
            logger.info("Plugin auto-refresh poller cancelled")
            raise

    async def _tick(self) -> None:
        config = await asyncio.to_thread(plugin_manifest.get_auto_refresh)
        if not config["enabled"]:
            return
        elapsed = time.monotonic() - self._baseline_monotonic
        if elapsed < config["interval_minutes"] * 60:
            return
        await self.run_cycle()

    # ------------------------------------------------------------------
    # Refresh cycle
    # ------------------------------------------------------------------

    def trigger(self) -> Dict[str, Any]:
        """Start a cycle in the background now (admin "run now").

        Guards on both the cycle lock and a still-running manual task so two
        near-simultaneous triggers can't orphan the first task (leaving it
        un-cancellable by ``stop()``); the second is reported as already running.
        """
        if self._cycle_lock.locked() or (
            self._manual_task is not None and not self._manual_task.done()
        ):
            return {"status": "already_running"}
        task = asyncio.get_running_loop().create_task(self.run_cycle())
        self._manual_task = task
        # Drop the reference once done, but only if it still points at this task.
        task.add_done_callback(
            lambda t: setattr(self, "_manual_task", None)
            if self._manual_task is t
            else None
        )
        return {"status": "started"}

    async def run_cycle(self) -> Dict[str, Any]:
        """Refresh every gateway-managed marketplace, one at a time.

        Marketplaces are refreshed sequentially: each refresh spawns git and
        claude CLI subprocesses, and the claude CLI mutates shared registry
        files (known_marketplaces.json et al.) that concurrent invocations
        could race on.
        """
        if self._cycle_lock.locked():
            return {"status": "already_running"}
        async with self._cycle_lock:
            names = sorted(await asyncio.to_thread(self._managed_marketplace_names))
            logger.info(
                "Plugin auto-refresh cycle starting (%d marketplace(s))", len(names)
            )
            results: List[Dict[str, Any]] = []
            for name in names:
                results.append(await asyncio.to_thread(self._refresh_one, name))
            # Measure the next interval from cycle END so a cycle slower than
            # the interval cannot immediately re-trigger itself.
            self._baseline_monotonic = time.monotonic()
            self._baseline_wall = time.time()
            self._last_run_at = _utc_now_iso()
            self._last_results = results
            ok = sum(1 for r in results if r["status"] == "refreshed")
            logger.info(
                "Plugin auto-refresh cycle done: %d/%d marketplace(s) refreshed",
                ok,
                len(results),
            )
            return {"status": "completed", "results": results}

    @staticmethod
    def _refresh_one(name: str) -> Dict[str, Any]:
        """Refresh one marketplace; a failure is a result row, never fatal."""
        from src import plugin_admin_service

        try:
            res = plugin_admin_service.refresh_marketplace(name)
            return {
                "marketplace": name,
                "status": "refreshed",
                "updated_plugins": len(res.get("updated_plugins") or []),
                "failed_updates": len(res.get("failed_updates") or []),
            }
        except plugin_admin_service.PluginAdminError as exc:
            logger.warning("Auto-refresh failed for marketplace %r: %s", name, exc)
            return {"marketplace": name, "status": "error", "error": str(exc)}
        except Exception as exc:
            logger.exception("Unexpected auto-refresh failure for marketplace %r", name)
            return {"marketplace": name, "status": "error", "error": repr(exc)}

    @staticmethod
    def _managed_marketplace_names() -> set:
        """Marketplaces the gateway manages: the manifest's marketplace records."""
        return {n for n in plugin_manifest.list_marketplace_records() if n}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Config + poller state for the admin API (safe from any thread)."""
        config = plugin_manifest.get_auto_refresh()
        next_run_at = None
        if config["enabled"]:
            next_run_at = datetime.fromtimestamp(
                self._baseline_wall + config["interval_minutes"] * 60,
                tz=timezone.utc,
            ).isoformat(timespec="seconds")
        return {
            **config,
            "running": self._cycle_lock.locked(),
            "last_run_at": self._last_run_at,
            "next_run_at": next_run_at,
            "last_results": self._last_results,
        }


# Module-level singleton, mirroring session_manager: main.py starts/stops it,
# the admin routes read status and trigger runs.
auto_refresher = PluginAutoRefresher()
