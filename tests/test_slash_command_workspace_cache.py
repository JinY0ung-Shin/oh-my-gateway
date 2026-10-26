"""Regression tests for per-workspace Claude slash-command caches."""

from src.backends.claude import slash_commands as sc


def _reset_details_cache() -> None:
    sc._details_cache.reset()


async def test_available_command_cache_never_crosses_workspace_boundary(monkeypatch, tmp_path):
    sc._cache.reset()
    calls: list[str] = []
    a = tmp_path / "alice"
    b = tmp_path / "bob"
    a.mkdir()
    b.mkdir()

    async def _fake_fetch(cwd, user=None):
        key = str(cwd)
        calls.append(key)
        return {"alice-only"} if cwd == a else {"bob-only"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    assert await sc.get_available_commands(a) == {"alice-only"}
    assert await sc.get_available_commands(a) == {"alice-only"}
    assert await sc.get_available_commands(b) == {"bob-only"}
    assert await sc.get_available_commands(b) == {"bob-only"}

    # One fetch per workspace: Bob can never hit Alice's fresh cache entry.
    assert calls == [str(a), str(b)]
    sc._cache.reset()


async def test_validate_prompt_does_not_accept_other_users_project_skill(monkeypatch, tmp_path):
    sc._cache.reset()
    a = tmp_path / "alice"
    b = tmp_path / "bob"
    a.mkdir()
    b.mkdir()

    async def _fake_fetch(cwd, user=None):
        return {"private-review"} if cwd == a else {"help"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)

    await sc.validate_prompt("/private-review", cwd=a)
    try:
        await sc.validate_prompt("/private-review", cwd=b)
    except sc.SlashCommandError as exc:
        assert exc.code == "unknown_command"
    else:  # pragma: no cover - explicit failure message is clearer than pytest.raises here
        raise AssertionError("workspace B accepted workspace A's private slash command")
    sc._cache.reset()


async def test_command_details_cache_is_scoped_by_workspace(monkeypatch, tmp_path):
    _reset_details_cache()
    calls: list[str] = []
    a = tmp_path / "alice"
    b = tmp_path / "bob"
    a.mkdir()
    b.mkdir()

    async def _fake_fetch(cwd, user=None):
        calls.append(str(cwd))
        name = "alice-only" if cwd == a else "bob-only"
        return {name: {"description": name, "argument_hint": ""}}

    monkeypatch.setattr(sc, "_fetch_command_details", _fake_fetch)

    assert set((await sc.get_command_details(a)).keys()) == {"alice-only"}
    assert set((await sc.get_command_details(a)).keys()) == {"alice-only"}
    assert set((await sc.get_command_details(b)).keys()) == {"bob-only"}
    assert set((await sc.get_command_details(b)).keys()) == {"bob-only"}
    assert calls == [str(a), str(b)]
    _reset_details_cache()


async def test_preflights_of_different_workspaces_run_concurrently(monkeypatch, tmp_path):
    """First turns of different users must not queue behind each other: each
    workspace has its own lock (they used to share one process-wide lock, so the
    N-th concurrent new session waited for N-1 preflight CLIs)."""
    import asyncio

    sc._cache.reset()
    monkeypatch.setattr(sc, "_preflight_slots", asyncio.Semaphore(8))
    monkeypatch.setattr(sc, "_preflight_slots_loop", asyncio.get_running_loop())
    running = {"now": 0, "peak": 0}

    async def _fake_fetch(cwd, user=None):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.05)
        running["now"] -= 1
        return {"help"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)
    dirs = [tmp_path / f"user{i}" for i in range(5)]
    for d in dirs:
        d.mkdir()
    await asyncio.gather(*(sc.get_available_commands(d) for d in dirs))
    assert running["peak"] == 5
    sc._cache.reset()


async def test_concurrent_first_turns_in_one_workspace_share_one_fetch(monkeypatch, tmp_path):
    import asyncio

    sc._cache.reset()
    calls = {"n": 0}

    async def _fake_fetch(cwd, user=None):
        calls["n"] += 1
        await asyncio.sleep(0.02)
        return {"help"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)
    results = await asyncio.gather(*(sc.get_available_commands(tmp_path) for _ in range(4)))
    assert results == [{"help"}] * 4
    assert calls["n"] == 1
    sc._cache.reset()


async def test_preflight_cap_bounds_simultaneous_clis(monkeypatch, tmp_path):
    import asyncio

    sc._cache.reset()
    monkeypatch.setattr(sc, "_preflight_slots", asyncio.Semaphore(2))
    monkeypatch.setattr(sc, "_preflight_slots_loop", asyncio.get_running_loop())
    running = {"now": 0, "peak": 0}

    async def _fake_fetch(cwd, user=None):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.02)
        running["now"] -= 1
        return {"help"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)
    dirs = [tmp_path / f"w{i}" for i in range(6)]
    for d in dirs:
        d.mkdir()
    await asyncio.gather(*(sc.get_available_commands(d) for d in dirs))
    assert running["peak"] == 2
    sc._cache.reset()


async def test_expired_entries_and_idle_locks_are_pruned(monkeypatch, tmp_path):
    sc._cache.reset()

    async def _fake_fetch(cwd, user=None):
        return {"help"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)
    for i in range(3):
        d = tmp_path / f"old{i}"
        d.mkdir()
        await sc.get_available_commands(d)
    sc._cache.expire_all()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    await sc.get_available_commands(fresh)
    assert set(sc._cache.entries) == {sc._cwd_key(fresh)}
    assert set(sc._cache.locks) == {sc._cwd_key(fresh)}
    sc._cache.reset()
