"""Unit tests for ``src/backends/claude/argv_files.py`` (#241 §6.5).

The bundled-CLI behavior (the CLI reads the files at argument parsing, resolves
the relative names against its cwd) is pinned in tests/test_cli_argv_files.py.
Here: the names left on the options, the startup sweep of files a dead gateway
left behind, and ``verify()`` externalizing like ``create_client``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from src.backends.claude import argv_files, landlock

_OLD = argv_files.STALE_AFTER_SECONDS + 60


def _command(options) -> list:
    """The argv the SDK would spawn the CLI with (no spawn)."""
    options = dataclasses.replace(options, cli_path=options.cli_path or "claude")
    return SubprocessCLITransport(prompt="x", options=options)._build_command()


def _argv(directory: Path, kind: str = "mcp", age: float | None = _OLD) -> Path:
    path = directory / f"{argv_files.PREFIX}{kind}-{secrets.token_hex(8)}"
    path.write_text("{}")
    if age is not None:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


# ---------------------------------------------------------------------------
# externalize(): names relative to the CLI's cwd
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "Current user: alice",
        {"type": "custom", "prompt": "Current user: alice"},
        {"type": "preset", "preset": "claude_code", "append": "Current user: alice"},
    ],
    ids=["str", "custom", "preset-append"],
)
def test_externalize_leaves_only_relative_names(tmp_path, prompt):
    ws = tmp_path / "alice" / "claude"
    ws.mkdir(parents=True)
    options = ClaudeAgentOptions(
        mcp_servers={"probe": {"type": "http", "url": "http://x"}},
        system_prompt=prompt,
        cwd=str(ws),
    )
    files = argv_files.externalize(options, shared_uid=True)
    try:
        names = [options.mcp_servers]
        if isinstance(options.system_prompt, dict) and "path" in options.system_prompt:
            names.append(options.system_prompt["path"])
        names += list((options.extra_args or {}).values())
        assert len(names) == 2
        for name in names:
            assert not os.path.isabs(name) and os.sep not in name
            assert (ws / name).is_file()
        # cleanup() still gets the absolute paths.
        assert all(p.is_absolute() and p.parent == ws for p in files.paths)
        cmd = _command(options)
        assert not any("alice" in arg for arg in cmd), cmd
    finally:
        files.cleanup()
    assert not list(ws.iterdir())


# ---------------------------------------------------------------------------
# sweep_stale(): files a gateway that died mid-spawn left behind
# ---------------------------------------------------------------------------


@pytest.fixture
def base(tmp_path):
    base = tmp_path / "workspaces"
    base.mkdir()
    return base


def test_sweep_removes_stale_files_where_workspaces_live(base):
    named = base / "alice" / "claude"
    legacy = base / "bob"
    anon = base / "_tmp_0123"
    for d in (named, legacy, anon):
        d.mkdir(parents=True)
    stale = [
        _argv(named),
        _argv(named, "system-prompt"),
        _argv(legacy, "append-prompt"),
        _argv(anon),
    ]
    fresh = _argv(named, age=None)

    assert argv_files.sweep_stale(base) == len(stale)
    assert not any(p.exists() for p in stale)
    assert fresh.exists()


def test_sweep_leaves_everything_else_alone(base, tmp_path):
    ws = base / "alice" / "claude"
    (ws / "sub").mkdir(parents=True)
    # Deeper than a workspace root, directly in the base, or merely sharing
    # the prefix: not a file _write made.
    kept = [
        _argv(ws / "sub"),
        _argv(base),
        ws / f"{argv_files.PREFIX}notes.txt",
        ws / f"{argv_files.PREFIX}mcp-{'z' * 16}",
    ]
    for path in kept[2:]:
        path.write_text("mine")
        os.utime(path, (time.time() - _OLD, time.time() - _OLD))
    directory = ws / f"{argv_files.PREFIX}mcp-{'0' * 16}"
    directory.mkdir()
    os.utime(directory, (time.time() - _OLD, time.time() - _OLD))

    assert argv_files.sweep_stale(base) == 0
    assert all(p.exists() for p in kept) and directory.is_dir()


def test_sweep_never_follows_a_symlinked_directory(base, tmp_path):
    outside = tmp_path / "outside"
    (outside / "claude").mkdir(parents=True)
    victims = [_argv(outside), _argv(outside / "claude")]
    (base / "eve").symlink_to(outside)  # a user dir
    (base / "mallory").mkdir()
    (base / "mallory" / "claude").symlink_to(outside / "claude")  # a backend dir

    assert argv_files.sweep_stale(base) == 0
    assert all(p.exists() for p in victims)


def test_sweep_unlinks_a_stale_symlink_not_its_target(base, tmp_path):
    ws = base / "alice" / "claude"
    ws.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("keep")
    link = ws / f"{argv_files.PREFIX}mcp-{secrets.token_hex(8)}"
    link.symlink_to(target)
    stamp = time.time() - _OLD
    os.utime(link, (stamp, stamp), follow_symlinks=False)

    assert argv_files.sweep_stale(base) == 1
    assert not os.path.lexists(link) and target.read_text() == "keep"


def test_sweep_never_raises(base, tmp_path):
    assert argv_files.sweep_stale(tmp_path / "missing") == 0
    locked = base / "carol"
    locked.mkdir()
    victim = _argv(locked)
    readable = base / "dave"
    readable.mkdir()
    swept = _argv(readable)
    locked.chmod(0)
    try:
        removed = argv_files.sweep_stale(base)
    finally:
        locked.chmod(0o755)
    assert not swept.exists()
    if os.geteuid() != 0:  # root reads a 0000 directory anyway
        assert removed == 1 and victim.exists()


# ---------------------------------------------------------------------------
# verify(): externalized like create_client under confinement
# ---------------------------------------------------------------------------


_HEADER = "verify-hdr-secret"
_PROMPT = "verify-base-prompt"


def _make_cli(cwd: Path):
    with patch(
        "src.auth.validate_claude_code_auth",
        return_value=(True, {"method": "anthropic"}),
    ):
        from src.backends.claude.client import ClaudeCodeCLI

        return ClaudeCodeCLI(cwd=str(cwd))


@pytest.fixture
def verify_cli(tmp_path, monkeypatch):
    """A client whose verify options carry an MCP secret and a prompt."""
    cwd = tmp_path / "verify"
    cwd.mkdir()
    cli = _make_cli(cwd)
    build = cli._build_sdk_options

    def _build(**kwargs):
        options = build(**kwargs)
        options.mcp_servers = {
            "probe": {"type": "http", "url": "http://x", "headers": {"X": _HEADER}}
        }
        options.system_prompt = _PROMPT
        return options

    monkeypatch.setattr(cli, "_build_sdk_options", _build)
    return cli, cwd


@pytest.fixture
def landlock_on(monkeypatch):
    monkeypatch.setenv(landlock.MODE_ENV, "landlock")
    landlock.reset_mode_cache()
    # Only the wiring matters here, not a confinable host's path lists.
    monkeypatch.setattr(landlock, "options_env", lambda *a, **k: {"X_CONFINED": "1"})
    yield
    landlock.reset_mode_cache()


def _recording_query(seen: list, error: Exception | None = None):
    async def fake_query(*, prompt, options):
        cmd = _command(options)
        on_disk = sorted(p.name for p in Path(options.cwd).iterdir())
        seen.append((cmd, on_disk, Path(options.cwd)))
        if error is not None:
            raise error
        yield SimpleNamespace(type="assistant", content="Hello")

    return fake_query


async def test_verify_externalizes_under_confinement(verify_cli, landlock_on):
    cli, cwd = verify_cli
    seen: list = []
    with patch("src.backends.claude.client.query", _recording_query(seen)):
        assert await cli.verify() is True
    ((cmd, on_disk, run_cwd),) = seen
    joined = "\0".join(cmd)
    assert _HEADER not in joined and _PROMPT not in joined
    mcp = cmd[cmd.index("--mcp-config") + 1]
    prompt = cmd[cmd.index("--system-prompt-file") + 1]
    # The files existed in the directory the CLI runs in when it was spawned,
    # under names relative to it, and are gone once verify() returns.
    assert run_cwd == cwd and {mcp, prompt} <= set(on_disk)
    assert not list(cwd.glob(f"{argv_files.PREFIX}*"))


async def test_verify_removes_the_files_when_the_cli_fails(verify_cli, landlock_on):
    cli, cwd = verify_cli
    seen: list = []
    fake = _recording_query(seen, RuntimeError("CLI died"))
    with patch("src.backends.claude.client.query", fake):
        assert await cli.verify() is False
    assert seen and any(n.startswith(argv_files.PREFIX) for n in seen[0][1])
    assert not list(cwd.glob(f"{argv_files.PREFIX}*"))


async def test_verify_keeps_the_inline_argv_when_confinement_is_off(
    verify_cli, monkeypatch
):
    monkeypatch.delenv(landlock.MODE_ENV, raising=False)
    landlock.reset_mode_cache()
    cli, cwd = verify_cli
    seen: list = []
    with patch("src.backends.claude.client.query", _recording_query(seen)):
        assert await cli.verify() is True
    cmd, on_disk, _cwd = seen[0]
    assert _HEADER in "\0".join(cmd) and _PROMPT in cmd
    assert not any(n.startswith(argv_files.PREFIX) for n in on_disk)


@pytest.mark.parametrize(
    "close_cancelled", [False, True], ids=["mid-stream", "again-in-aclose"]
)
async def test_verify_removes_the_files_when_cancelled(
    verify_cli, landlock_on, monkeypatch, close_cancelled
):
    """A verify() cancelled mid-stream (the startup ``wait_for`` timing out, a
    health check going away) still removes its argv files — also when closing
    the stream is cancelled again, which skips the cleanup right after it: the
    outer ``finally`` must catch that one."""
    from src.backends.claude import client as client_module

    cli, cwd = verify_cli
    streaming = asyncio.Event()

    async def blocked_query(*, prompt, options):
        yield {"type": "system", "subtype": "init"}
        streaming.set()
        await asyncio.Event().wait()  # until the task is cancelled
        yield {"type": "assistant"}

    async def cancelled_close(stream):
        raise asyncio.CancelledError

    if close_cancelled:
        monkeypatch.setattr(client_module, "_aclose_query", cancelled_close)
    with patch("src.backends.claude.client.query", blocked_query):
        task = asyncio.create_task(cli.verify())
        await asyncio.wait_for(streaming.wait(), 10)
        assert len(list(cwd.glob(f"{argv_files.PREFIX}*"))) == 2  # mcp + prompt
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not list(cwd.glob(f"{argv_files.PREFIX}*"))
