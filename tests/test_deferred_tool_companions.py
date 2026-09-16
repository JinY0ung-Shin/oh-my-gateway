"""A blocked scheduler must take its companions down with it (ChatDRAGON #397).

Blocking only ``CronCreate`` left ``CronList``/``CronDelete`` in the catalog. The
model read the half-present family as a naming problem rather than a missing
capability: a measured ``/loop`` turn spent its budget on "I don't see CronCreate
available even though CronDelete and CronList are listed. Let me try to invoke it
directly…". Nothing can be scheduled, so nothing can be listed or deleted either.
"""

import importlib
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client():
    """FastAPI TestClient with admin auth bypassed (mirrors the admin-route tests)."""
    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-admin-key"}):
        from src.admin_auth import require_admin
        from src.main import app

        app.dependency_overrides[require_admin] = lambda: True
        client = TestClient(app)
        yield client
        app.dependency_overrides.pop(require_admin, None)


def _reload_constants(monkeypatch, raw: str | None):
    if raw is None:
        monkeypatch.delenv("BLOCKED_DEFERRED_TOOLS", raising=False)
    else:
        monkeypatch.setenv("BLOCKED_DEFERRED_TOOLS", raw)
    import src.backends.claude.constants as constants

    return importlib.reload(constants)


def test_default_block_covers_the_whole_cron_family(monkeypatch):
    constants = _reload_constants(monkeypatch, None)
    blocked = set(constants.BLOCKED_DEFERRED_TOOLS)
    assert {"ScheduleWakeup", "CronCreate", "CronList", "CronDelete"} <= blocked


def test_clearing_the_var_restores_every_deferred_tool(monkeypatch):
    constants = _reload_constants(monkeypatch, "")
    assert constants.BLOCKED_DEFERRED_TOOLS == []


def test_blocking_only_the_wakeup_tool_leaves_cron_alone(monkeypatch):
    """Companions expand per blocked name, not as one all-or-nothing family."""
    constants = _reload_constants(monkeypatch, "ScheduleWakeup")
    assert constants.BLOCKED_DEFERRED_TOOLS == ["ScheduleWakeup"]


def test_explicit_companion_is_not_duplicated(monkeypatch):
    constants = _reload_constants(monkeypatch, "CronCreate,CronList")
    blocked = constants.BLOCKED_DEFERRED_TOOLS
    assert sorted(blocked) == ["CronCreate", "CronDelete", "CronList"]
    assert len(blocked) == len(set(blocked))


@pytest.fixture(autouse=True)
def _restore_constants():
    yield
    import src.backends.claude.constants as constants

    importlib.reload(constants)


# ---------------------------------------------------------------------------
# The advertised capability must agree with the tool surface a client meets
# ---------------------------------------------------------------------------
#
# Review on #202: `deferred_delivery_available = not BLOCKED_DEFERRED_TOOLS` made the
# flag false whenever *any* deferred tool was blocked. With
# `BLOCKED_DEFERRED_TOOLS=ScheduleWakeup` cron still works and `/loop <interval>` can
# still schedule, yet the flag said false — a client using it as intended would disable
# a working feature. Each capability now derives from the tool it requires, and these
# tests compare the advertised value against the *actual* disallowed_tools a turn gets,
# not against a re-spelling of the same expression.


def _effective_disallowed(monkeypatch, constants) -> set[str]:
    """The deny list a client's turn actually carries, from the real code path.

    `client.py` binds `BLOCKED_DEFERRED_TOOLS` at import, so the reloaded constants
    are patched in rather than reloading the module: reloading `client` rebuilds its
    exception classes, and every other test that catches them by identity then fails.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    import src.backends.claude.client as client_mod

    monkeypatch.setattr(
        client_mod, "BLOCKED_DEFERRED_TOOLS", constants.BLOCKED_DEFERRED_TOOLS
    )
    options = ClaudeAgentOptions()
    client_mod.ClaudeCodeCLI._configure_tools(
        client_mod.ClaudeCodeCLI.__new__(client_mod.ClaudeCodeCLI),
        options,
        None,
        None,
    )
    disallowed = set(options.disallowed_tools or [])
    assert set(constants.BLOCKED_DEFERRED_TOOLS) <= disallowed, (
        "fixture precondition: the blocked list must reach disallowed_tools"
    )
    return disallowed


@pytest.mark.parametrize(
    "raw,expected",
    [
        # (env, {capability: advertised value})
        (
            None,  # default: both mechanisms blocked
            {
                "cron_scheduling_available": False,
                "wakeup_scheduling_available": False,
                "deferred_delivery_available": False,
            },
        ),
        (
            "",  # fully enabled
            {
                "cron_scheduling_available": True,
                "wakeup_scheduling_available": True,
                "deferred_delivery_available": True,
            },
        ),
        (
            "ScheduleWakeup",  # the review's case: cron survives
            {
                "cron_scheduling_available": True,
                "wakeup_scheduling_available": False,
                "deferred_delivery_available": True,
            },
        ),
        (
            "CronCreate",  # the mirror: self-paced wakeup survives
            {
                "cron_scheduling_available": False,
                "wakeup_scheduling_available": True,
                "deferred_delivery_available": True,
            },
        ),
    ],
)
def test_server_info_capability_matches_the_real_tool_surface(
    monkeypatch, admin_client, raw, expected
):
    constants = _reload_constants(monkeypatch, raw)
    # `server-info` imports the constants inside the handler, so the reload above is
    # what the endpoint sees — no route-module reload (and no re-registration) needed.
    body = admin_client.get("/admin/api/server-info").json()
    for name, value in expected.items():
        assert body[name] is value, f"{name} with BLOCKED_DEFERRED_TOOLS={raw!r}"
    assert body["blocked_deferred_tools"] == list(constants.BLOCKED_DEFERRED_TOOLS)

    # the acceptance criterion: agreement with what a client's turn really gets
    disallowed = _effective_disallowed(monkeypatch, constants)
    assert body["cron_scheduling_available"] is ("CronCreate" not in disallowed)
    assert body["wakeup_scheduling_available"] is ("ScheduleWakeup" not in disallowed)
    assert body["deferred_delivery_available"] is (
        body["cron_scheduling_available"] or body["wakeup_scheduling_available"]
    )


def test_every_advertised_capability_names_the_tools_it_needs():
    """A future capability must not be added as a broader emptiness check."""
    import src.backends.claude.constants as constants

    for name, required in constants._DEFERRED_CAPABILITY_TOOLS.items():
        assert required, f"{name} advertises nothing concrete"
        assert all(isinstance(t, str) and t for t in required)
    # the rollup is derived, never declared as a required-tool entry
    assert "deferred_delivery_available" not in constants._DEFERRED_CAPABILITY_TOOLS
    assert "deferred_delivery_available" in constants.deferred_capabilities()
