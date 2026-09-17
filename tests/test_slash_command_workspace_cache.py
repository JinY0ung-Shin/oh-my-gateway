"""Regression tests for per-workspace Claude slash-command caches."""

from src.backends.claude import slash_commands as sc


def _reset_details_cache() -> None:
    sc._details_cache.details = None
    sc._details_cache.fetched_at = 0.0
    sc._details_cache.cwd_key = None


async def test_available_command_cache_never_crosses_workspace_boundary(monkeypatch, tmp_path):
    sc._cache.reset()
    calls: list[str] = []
    a = tmp_path / "alice"
    b = tmp_path / "bob"
    a.mkdir()
    b.mkdir()

    async def _fake_fetch(cwd):
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

    async def _fake_fetch(cwd):
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

    async def _fake_fetch(cwd):
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
