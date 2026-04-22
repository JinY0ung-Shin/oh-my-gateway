"""Unit tests for src.backends.claude.slash_commands."""

from __future__ import annotations

import pytest

from src.backends.claude import slash_commands as sc


@pytest.fixture(autouse=True)
def _reset_cache():
    sc._cache.reset()
    yield
    sc._cache.reset()


# --- extract_command_name -------------------------------------------------


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("hello", None),
        ("  hi there", None),
        ("/help", "help"),
        ("  /help  ", "help"),
        ("/compact foo bar", "compact"),
        ("/api/v1/users", "api/v1/users"),
        ("/dev-server status", "dev-server"),
        ("/superpowers:brainstorming", "superpowers:brainstorming"),
        ("/", ""),
        ("/ tell me a joke", ""),
    ],
)
def test_extract_command_name(prompt, expected):
    assert sc.extract_command_name(prompt) == expected


# --- validate_prompt ------------------------------------------------------


async def test_non_slash_is_noop(monkeypatch):
    called = {"n": 0}

    async def _fake_fetch(cwd):
        called["n"] += 1
        return set()

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    await sc.validate_prompt("hello world")
    await sc.validate_prompt("  how are you")
    assert called["n"] == 0  # fetch never triggered


async def test_blocked_command_raises(monkeypatch):
    async def _fake_fetch(cwd):
        return {"compact", "init", "heapdump", "dev-server"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    with pytest.raises(sc.SlashCommandError) as ei:
        await sc.validate_prompt("/compact please")
    assert ei.value.code == "blocked_command"


async def test_known_command_passes(monkeypatch):
    async def _fake_fetch(cwd):
        return {"dev-server", "review"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    await sc.validate_prompt("/dev-server status")
    await sc.validate_prompt("/review")


async def test_unknown_command_raises(monkeypatch):
    calls = {"n": 0}

    async def _fake_fetch(cwd):
        calls["n"] += 1
        return {"review"}  # does not contain "help"

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    with pytest.raises(sc.SlashCommandError) as ei:
        await sc.validate_prompt("/help")
    assert ei.value.code == "unknown_command"
    # First fetch for cache miss, second forced refresh before giving up.
    assert calls["n"] == 2


async def test_empty_slash_is_unknown(monkeypatch):
    async def _fake_fetch(cwd):
        return {"review"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    with pytest.raises(sc.SlashCommandError) as ei:
        await sc.validate_prompt("/ hello")
    assert ei.value.code == "unknown_command"


async def test_ttl_cache_reuses_within_window(monkeypatch):
    calls = {"n": 0}

    async def _fake_fetch(cwd):
        calls["n"] += 1
        return {"review"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    await sc.get_available_commands()
    await sc.get_available_commands()
    await sc.get_available_commands()
    assert calls["n"] == 1  # subsequent calls within TTL hit cache


async def test_ttl_cache_refreshes_after_expiry(monkeypatch):
    calls = {"n": 0}

    async def _fake_fetch(cwd):
        calls["n"] += 1
        return {"review"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    await sc.get_available_commands()
    assert calls["n"] == 1

    # Force expiry by rewinding the cache timestamp past the TTL.
    sc._cache.fetched_at -= sc.CACHE_TTL_SECONDS + 1

    await sc.get_available_commands()
    assert calls["n"] == 2


async def test_refresh_picks_up_newly_added_skill(monkeypatch):
    """If the cache is stale and the skill was just added, refresh rescues it."""
    state = {"commands": {"review"}, "calls": 0}

    async def _fake_fetch(cwd):
        state["calls"] += 1
        # Second call (the forced refresh) reveals the new skill.
        if state["calls"] >= 2:
            return state["commands"] | {"newly-added"}
        return state["commands"]

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    await sc.validate_prompt("/newly-added")
    assert state["calls"] == 2
