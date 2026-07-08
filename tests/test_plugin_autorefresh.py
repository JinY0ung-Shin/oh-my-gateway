"""Tests for the marketplace auto-refresh poller.

``plugin_admin_service.refresh_marketplace`` is mocked — these cover the
scheduling and config plumbing: due/not-due timing, the disabled no-op,
per-marketplace failure isolation, already-running rejection, the managed
marketplace set (manifest ∪ env journal), and the status shape.
"""

import json
from unittest.mock import patch

import pytest

from src import plugin_manifest
from src.plugin_admin_service import PluginAdminError
from src.plugin_autorefresh import PluginAutoRefresher

_REFRESH_OK = {
    "updated_plugins": [{"spec": "p@m", "scope": "user"}],
    "failed_updates": [],
}


@pytest.fixture
def manifest_file(tmp_path, monkeypatch):
    path = tmp_path / "gateway-plugins.json"
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(path))
    monkeypatch.delenv("CLAUDE_PLUGIN_ENV_JOURNAL", raising=False)
    return path


# ---------------------------------------------------------------------------
# Refresh cycle
# ---------------------------------------------------------------------------


async def test_cycle_refreshes_managed_marketplaces(manifest_file):
    plugin_manifest.set_marketplace(
        "mkt-a", repo="https://x/a.git", branch="main", scope="user"
    )
    plugin_manifest.set_marketplace(
        "mkt-b", repo="https://x/b.git", branch="main", scope="user"
    )
    refresher = PluginAutoRefresher()
    with patch(
        "src.plugin_admin_service.refresh_marketplace", return_value=_REFRESH_OK
    ) as mock_refresh:
        result = await refresher.run_cycle()

    assert result["status"] == "completed"
    assert [r["marketplace"] for r in result["results"]] == ["mkt-a", "mkt-b"]
    assert all(r["status"] == "refreshed" for r in result["results"])
    assert result["results"][0]["updated_plugins"] == 1
    assert mock_refresh.call_count == 2


async def test_cycle_includes_env_journal_marketplaces(manifest_file):
    journal = manifest_file.with_name("gateway-plugins-env.json")
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "marketplaces": {
                    "env-mkt": {
                        "scope": "user",
                        "branch": "main",
                        "repo": "https://x/e.git",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    plugin_manifest.set_marketplace("mkt-a", repo="r", branch="main", scope="user")
    refresher = PluginAutoRefresher()
    with patch(
        "src.plugin_admin_service.refresh_marketplace", return_value=_REFRESH_OK
    ) as mock_refresh:
        result = await refresher.run_cycle()

    assert sorted(r["marketplace"] for r in result["results"]) == ["env-mkt", "mkt-a"]
    assert mock_refresh.call_count == 2


async def test_cycle_noop_when_nothing_managed(manifest_file):
    refresher = PluginAutoRefresher()
    with patch("src.plugin_admin_service.refresh_marketplace") as mock_refresh:
        result = await refresher.run_cycle()
    assert result == {"status": "completed", "results": []}
    mock_refresh.assert_not_called()


async def test_cycle_failure_is_isolated(manifest_file):
    plugin_manifest.set_marketplace("bad", repo="r1", branch="main", scope="user")
    plugin_manifest.set_marketplace("good", repo="r2", branch="main", scope="user")

    def fake_refresh(name):
        if name == "bad":
            raise PluginAdminError("clone failed")
        return _REFRESH_OK

    refresher = PluginAutoRefresher()
    with patch(
        "src.plugin_admin_service.refresh_marketplace", side_effect=fake_refresh
    ):
        result = await refresher.run_cycle()

    by_name = {r["marketplace"]: r for r in result["results"]}
    assert by_name["bad"]["status"] == "error"
    assert "clone failed" in by_name["bad"]["error"]
    assert by_name["good"]["status"] == "refreshed"
    # the failed row is surfaced through status() for the admin UI
    assert refresher.status()["last_results"] == result["results"]


async def test_run_cycle_rejects_concurrent(manifest_file):
    refresher = PluginAutoRefresher()
    async with refresher._cycle_lock:
        assert await refresher.run_cycle() == {"status": "already_running"}
        assert refresher.trigger() == {"status": "already_running"}


# ---------------------------------------------------------------------------
# Poll tick (config gating + due timing)
# ---------------------------------------------------------------------------


async def test_tick_noop_when_disabled(manifest_file):
    plugin_manifest.set_marketplace("mkt", repo="r", branch="main", scope="user")
    refresher = PluginAutoRefresher()
    refresher._baseline_monotonic = 0  # long overdue, but disabled
    with patch("src.plugin_admin_service.refresh_marketplace") as mock_refresh:
        await refresher._tick()
    mock_refresh.assert_not_called()


async def test_tick_runs_when_due_and_resets_baseline(manifest_file):
    plugin_manifest.set_auto_refresh(enabled=True, interval_minutes=5)
    plugin_manifest.set_marketplace("mkt", repo="r", branch="main", scope="user")
    refresher = PluginAutoRefresher()
    with patch(
        "src.plugin_admin_service.refresh_marketplace", return_value=_REFRESH_OK
    ) as mock_refresh:
        # baseline is process start -> first interval has not elapsed yet
        await refresher._tick()
        mock_refresh.assert_not_called()

        refresher._baseline_monotonic -= 5 * 60 + 1  # push past the interval
        await refresher._tick()
        assert mock_refresh.call_count == 1

        # cycle end reset the baseline -> immediately due no more
        await refresher._tick()
        assert mock_refresh.call_count == 1


async def test_trigger_starts_background_cycle(manifest_file):
    plugin_manifest.set_marketplace("mkt", repo="r", branch="main", scope="user")
    refresher = PluginAutoRefresher()
    with patch(
        "src.plugin_admin_service.refresh_marketplace", return_value=_REFRESH_OK
    ):
        assert refresher.trigger() == {"status": "started"}
        await refresher._manual_task
    assert refresher.status()["last_run_at"] is not None


async def test_trigger_rejects_second_while_first_pending(manifest_file):
    # Two triggers in the same tick: the first task has not run yet, so the
    # second must be rejected rather than orphaning the first (which stop()
    # could then never cancel).
    plugin_manifest.set_marketplace("mkt", repo="r", branch="main", scope="user")
    refresher = PluginAutoRefresher()
    with patch(
        "src.plugin_admin_service.refresh_marketplace", return_value=_REFRESH_OK
    ):
        assert refresher.trigger() == {"status": "started"}
        first = refresher._manual_task
        assert refresher.trigger() == {"status": "already_running"}
        assert refresher._manual_task is first  # not overwritten
        await first


async def test_reset_schedule_prevents_immediate_fire(manifest_file):
    # Enabling after the interval has long elapsed must not fire on the next
    # tick: reset_schedule (called from the PUT handler) restarts the countdown.
    plugin_manifest.set_auto_refresh(enabled=True, interval_minutes=5)
    plugin_manifest.set_marketplace("mkt", repo="r", branch="main", scope="user")
    refresher = PluginAutoRefresher()
    refresher._baseline_monotonic -= 10 * 60  # long overdue
    with patch(
        "src.plugin_admin_service.refresh_marketplace", return_value=_REFRESH_OK
    ) as mock_refresh:
        refresher.reset_schedule()
        await refresher._tick()
        mock_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# Lifecycle + status
# ---------------------------------------------------------------------------


async def test_start_stop_lifecycle(manifest_file):
    refresher = PluginAutoRefresher(poll_seconds=999)
    refresher.start()
    assert refresher._task is not None
    task = refresher._task
    refresher.start()  # idempotent while the task is live
    assert refresher._task is task
    await refresher.stop()
    assert refresher._task is None


async def test_start_after_stop_creates_fresh_task(manifest_file):
    # Mirrors cross-lifespan singleton reuse (a new event loop each time): a
    # stopped poller must be startable again rather than permanently no-op.
    refresher = PluginAutoRefresher(poll_seconds=999)
    refresher.start()
    first = refresher._task
    await refresher.stop()
    refresher.start()
    assert refresher._task is not None and refresher._task is not first
    await refresher.stop()


async def test_status_shape(manifest_file):
    plugin_manifest.set_auto_refresh(enabled=True, interval_minutes=15)
    refresher = PluginAutoRefresher()
    st = refresher.status()
    assert st["enabled"] is True
    assert st["interval_minutes"] == 15
    assert st["running"] is False
    assert st["last_run_at"] is None
    assert st["last_results"] == []
    assert isinstance(st["next_run_at"], str)  # scheduled when enabled

    plugin_manifest.set_auto_refresh(enabled=False, interval_minutes=15)
    assert refresher.status()["next_run_at"] is None
