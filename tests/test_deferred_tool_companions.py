"""A blocked scheduler must take its companions down with it (ChatDRAGON #397).

Blocking only ``CronCreate`` left ``CronList``/``CronDelete`` in the catalog. The
model read the half-present family as a naming problem rather than a missing
capability: a measured ``/loop`` turn spent its budget on "I don't see CronCreate
available even though CronDelete and CronList are listed. Let me try to invoke it
directly…". Nothing can be scheduled, so nothing can be listed or deleted either.
"""

import importlib

import pytest


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
