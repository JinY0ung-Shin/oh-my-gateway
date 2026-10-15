"""Pin argv externalization (``src/backends/claude/argv_files.py``) against the
bundled CLI (2.1.283).

A confined session can read every CLI's ``/proc/<pid>/cmdline``, so with
isolation on the gateway hands the MCP config and the system prompt over as
per-spawn files and removes them as soon as ``connect()`` returns. That is only
safe while the CLI reads those files during argument parsing and never again:

* after the files are gone, a turn still calls the MCP server (with the header
  that only the config file carried) and the model request still carries the
  appended / custom system prompt;
* the live CLI's command line holds neither secret, only the file names —
  relative to the CLI's cwd (the workspace), so not the workspace path or the
  user key it is named after either: the CLI resolves ``--mcp-config``,
  ``--system-prompt-file`` and ``--append-system-prompt-file`` against its cwd.

Both hold for the CLI spawned directly, through the Landlock launcher (which
execs it in place) and in uid mode through the broker, whose child chdirs into
the ticket's workspace before it execs the CLI — with the gateway's own options
for a user workspace reached through a symlinked workspace base.

If a CLI bump starts reading ``--mcp-config`` / ``--*system-prompt-file``
lazily, these fail: keep the files until the session ends instead.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import uvicorn
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import ToolResultBlock, UserMessage
from mcp.server.fastmcp import Context, FastMCP

from src.backends.claude import argv_files, landlock
from src.backends.claude.sdk_client import GatewayClaudeSDKClient
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI
from tests.test_cli_read_boundary import (  # noqa: F401 - landlock_on: a fixture
    _landlock_unusable,
    _make_cli,
    landlock_on,
)
from tests.test_cli_slash_preflight import _same_uid_broker_unusable

pytestmark = pytest.mark.integration

_TURN_TIMEOUT = 90
_HEADER_SECRET = "hdr-secret-4f1c9a"
_PROMPT_SECRET = "Current user: prompt-secret-7b2e"
# Workspaces are named after the user (<base>/<user key>/claude).
_USER_KEY = "alice.user-5d3e"
_FILE_FLAGS = ("--mcp-config", "--system-prompt-file", "--append-system-prompt-file")
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)
_BROKER = Path(__file__).resolve().parents[1] / "docker" / "isolation_broker.py"


@pytest.fixture(autouse=True)
def _no_inherited_claude_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)
    # The bundled CLI everywhere: what the SDK, the launcher and the broker run.
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mcp_server():
    seen: List[str] = []
    mcp = FastMCP("probe")

    @mcp.tool()
    async def echo(ctx: Context) -> str:
        """Echo."""
        request = ctx.request_context.request
        seen.append(request.headers.get("x-secret", "") if request else "")
        return "echo-done"

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", seen
    server.should_exit = True


def _plan(body: Dict[str, Any]) -> Dict[str, Any]:
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        ):
            return {"text": "finished"}
    return {"tool_use": {"name": "mcp__probe__echo"}}


def _system_text(body: Dict[str, Any]) -> str:
    system = body.get("system")
    if isinstance(system, str):
        return system
    return "".join(b.get("text", "") for b in system or [] if isinstance(b, dict))


def _argv_processes(names: List[str]) -> List[Tuple[str, str]]:
    """``(exe, cmdline)`` of every live process with one of *names* as an
    argument: the CLI, and in uid mode also the launcher in front of it."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        cmdline = raw.decode("utf-8", "replace")
        if not set(names) & set(cmdline.split("\0")):
            continue
        try:
            exe = os.path.realpath(os.readlink(f"/proc/{entry}/exe"))
        except OSError:
            exe = ""
        found.append((exe, cmdline))
    return found


async def _session(options: ClaudeAgentOptions, *, shared_uid: bool):
    """Externalize, connect, remove the files, then run one turn.

    Returns the processes naming the argv files, read while the session is up,
    and the turn's tool results.
    """
    files = argv_files.externalize(options, shared_uid=shared_uid)
    names = list(files.names)
    assert names and all(p.is_file() for p in files.paths)
    workspace = files.workspace
    client = GatewayClaudeSDKClient(options=options)
    try:
        await client.connect(prompt=None)
    finally:
        files.cleanup()
    results: List[str] = []
    try:
        assert not list(workspace.glob(".gateway-argv-*")), "argv files left behind"
        processes = _argv_processes(names)
        await client.query("go")
        async for message in client.receive_response():
            if isinstance(message, UserMessage) and isinstance(message.content, list):
                results += [
                    str(b.content)
                    for b in message.content
                    if isinstance(b, ToolResultBlock)
                ]
    finally:
        await client.disconnect()
    return processes, results


def _assert_only_names(processes: List[Tuple[str, str]], *hidden: str) -> None:
    """The CLI was among *processes*, and every one of them carries only the
    relative file names — none of *hidden*."""
    real_cli = os.path.realpath(landlock.resolve_real_cli() or "")
    assert any(exe == real_cli for exe, _ in processes), processes
    for _exe, cmdline in processes:
        args = cmdline.split("\0")
        values = [args[i + 1] for i, a in enumerate(args) if a in _FILE_FLAGS]
        assert len(values) == 2, args  # the MCP config + one prompt flag
        assert all(v.startswith(".gateway-argv-") for v in values), values
        for text in hidden:
            assert text not in cmdline, (text, args)


def _assert_delivered(seen: List[str], results: List[str], systems, prompt: str):
    """The CLI kept what it parsed: the MCP server got the header only the
    deleted config carried and the model got the deleted prompt — so it
    resolved every relative name against its cwd."""
    assert any("echo-done" in r for r in results), results
    assert seen and all(h == _HEADER_SECRET for h in seen)
    assert systems and all(prompt in s for s in systems)


@pytest.mark.parametrize(
    "system_prompt",
    [
        {"type": "preset", "preset": "claude_code", "append": _PROMPT_SECRET},
        _PROMPT_SECRET,
    ],
    ids=["preset-append", "custom"],
)
async def test_files_removed_after_connect_still_configure_the_session(
    mcp_server, tmp_path, system_prompt
):
    url, seen = mcp_server
    ws = tmp_path / _USER_KEY / "claude"
    ws.mkdir(parents=True)
    with FakeAnthropicAPI(_plan) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(tmp_path / "home"),
            mcp_servers={
                "probe": {
                    "type": "http",
                    "url": url,
                    "headers": {"X-Secret": _HEADER_SECRET},
                }
            },
            system_prompt=system_prompt,
            allowed_tools=["mcp__probe__echo"],
            cwd=str(ws),
        )
        processes, results = await asyncio.wait_for(
            _session(options, shared_uid=True), _TURN_TIMEOUT
        )
        systems = [_system_text(r["body"]) for r in api.model_requests()]
    _assert_only_names(
        processes, _HEADER_SECRET, _PROMPT_SECRET, _USER_KEY, str(tmp_path)
    )
    _assert_delivered(seen, results, systems, _PROMPT_SECRET)


def _user_workspace(tmp_path: Path, monkeypatch) -> Path:
    """``<base>/<user key>/claude`` reached through a symlinked workspace base,
    with a gateway temp dir of its own (the grant filter refuses entries inside
    the system temp dir, where pytest's tmp_path lives)."""
    from src.workspace_manager import workspace_manager

    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    real_base = tmp_path / "realbase"
    (real_base / _USER_KEY / "claude").mkdir(parents=True)
    base = tmp_path / "base"
    base.symlink_to(real_base, target_is_directory=True)
    monkeypatch.setattr(workspace_manager, "base_path", base)
    return base / _USER_KEY / "claude"


def _gateway_options(ws: Path, url: str, env: Dict[str, str]) -> ClaudeAgentOptions:
    """What the gateway builds for the user's session (``_build_sdk_options``,
    confined by the current isolation mode), talking to the fake API."""
    options = _make_cli()._build_sdk_options(
        cwd=ws,
        user=_USER_KEY,
        mcp_servers={
            "probe": {
                "type": "http",
                "url": url,
                "headers": {"X-Secret": _HEADER_SECRET},
            }
        },
        allowed_tools=["mcp__probe__*"],
        permission_mode="bypassPermissions",
        session_id=str(uuid.uuid4()),
    )
    assert options.cli_path == landlock.launcher_path()
    assert options.mcp_servers["probe"]["headers"] == {"X-Secret": _HEADER_SECRET}
    options.env.update(env)
    options.env["CLAUDE_CODE_HARBOR_KITE"] = "0"
    if os.geteuid() == 0:  # the CLI refuses bypassPermissions as root otherwise
        options.env["IS_SANDBOX"] = "1"
    return options


@pytest.mark.usefixtures("landlock_on")
@pytest.mark.filterwarnings("ignore::claude_agent_sdk.types.CanUseToolShadowedWarning")
@pytest.mark.skipif(bool(_landlock_unusable()), reason=_landlock_unusable() or "")
async def test_landlock_launcher_resolves_the_names_in_a_symlinked_base(
    mcp_server, tmp_path, monkeypatch
):
    # The launcher confines itself and execs the CLI in place, in the kernel
    # cwd: the workspace resolved through the symlinked base.
    url, seen = mcp_server
    ws = _user_workspace(tmp_path, monkeypatch)
    with FakeAnthropicAPI(_plan) as api:
        env = api.cli_env(tmp_path / "home")
        options = _gateway_options(ws, url, env)
        assert options.env[landlock.MODE_ENV] == "landlock"
        processes, results = await asyncio.wait_for(
            _session(options, shared_uid=True), _TURN_TIMEOUT
        )
        systems = [_system_text(r["body"]) for r in api.model_requests()]
    _assert_only_names(processes, _HEADER_SECRET, _USER_KEY, str(tmp_path))
    _assert_delivered(seen, results, systems, f"Current user: {_USER_KEY}")


@pytest.mark.filterwarnings("ignore::claude_agent_sdk.types.CanUseToolShadowedWarning")
@pytest.mark.skipif(
    bool(_same_uid_broker_unusable()), reason=_same_uid_broker_unusable() or ""
)
async def test_uid_broker_resolves_the_names_in_the_ticket_workspace(
    mcp_server, tmp_path, monkeypatch
):
    # uid mode end to end: gateway ticket -> launcher -> broker (--test-same-uid:
    # the band maps the user onto this uid; the real switch is container-only),
    # whose child chdirs into the ticket's workspace and execs the CLI there.
    from src.backends.claude import uid_isolation as ui

    url, seen = mcp_server
    uid = os.getuid()
    ws = _user_workspace(tmp_path, monkeypatch)
    with FakeAnthropicAPI(_plan) as api:
        env = api.cli_env(tmp_path / "home")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        for key, value in {
            landlock.MODE_ENV: "uid",
            landlock.BROKER_SOCKET_ENV: str(tmp_path / "b.sock"),
            ui.TICKET_SECRET_ENV: secrets.token_hex(16),
            "CLAUDE_ISOLATION_UID_MIN": str(uid),
            "CLAUDE_ISOLATION_UID_MAX": str(uid + 1),
            "APP_UID": str(uid - 1),
            "APP_GID": str(uid - 1),  # reserved too; default 1000 = runner uid
            "CLAUDE_ISOLATION_BROKER_UID": str(uid - 2),
            "CLAUDE_ISOLATION_UID_REGISTRY": str(tmp_path / "uids.json"),
        }.items():
            monkeypatch.setenv(key, value)
        # install_process_env writes these; let monkeypatch restore them.
        monkeypatch.delenv(landlock.REAL_CLI_ENV, raising=False)
        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
        broker_env = {
            **os.environ,
            landlock.REAL_CLI_ENV: landlock.resolve_real_cli() or "",
            "GATEWAY_ISOLATION_PEER_UID": str(uid),
        }
        with open(tmp_path / "broker.log", "wb") as log:
            broker = subprocess.Popen(
                [sys.executable, str(_BROKER), "--test-same-uid"],
                env=broker_env,
                stderr=log,
            )
        landlock.reset_mode_cache()
        ui.reset_registry_cache()
        try:
            ui.capture_ticket_secret()  # as the lifespan does
            landlock.install_process_env()  # waits for the broker socket
            assert landlock.effective_mode() == "uid"
            options = _gateway_options(ws, url, env)
            assert options.env[landlock.MODE_ENV] == "uid"
            processes, results = await asyncio.wait_for(
                _session(options, shared_uid=False), _TURN_TIMEOUT
            )
        finally:
            broker.kill()
            broker.wait(timeout=10)
            landlock.reset_mode_cache()
            landlock.reset_uid_probe()
            ui.reset_registry_cache()
            ui.reset_captured_secret()
        systems = [_system_text(r["body"]) for r in api.model_requests()]
    _assert_only_names(processes, _HEADER_SECRET, _USER_KEY, str(tmp_path))
    _assert_delivered(seen, results, systems, f"Current user: {_USER_KEY}")


async def test_inline_argv_is_the_control(mcp_server, tmp_path):
    """Without externalize() both secrets sit on the command line — the
    exposure the files close (keeps the assertions above non-vacuous)."""
    url, _seen = mcp_server
    ws = tmp_path / "ws"
    ws.mkdir()
    with FakeAnthropicAPI(_plan) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(tmp_path / "home"),
            mcp_servers={
                "probe": {
                    "type": "http",
                    "url": url,
                    "headers": {"X-Secret": _HEADER_SECRET},
                }
            },
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": _PROMPT_SECRET,
            },
            cwd=str(ws),
        )
        client = GatewayClaudeSDKClient(options=options)
        await asyncio.wait_for(client.connect(prompt=None), _TURN_TIMEOUT)
        try:
            pid = client._transport._process.pid
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
        finally:
            await client.disconnect()
    assert _HEADER_SECRET in cmdline and _PROMPT_SECRET in cmdline


def test_a_planted_symlink_fails_the_spawn(tmp_path, monkeypatch):
    """The file is created O_EXCL|O_NOFOLLOW: a name the agent pre-planted as a
    symlink never redirects the write."""
    ws = tmp_path / "ws"
    ws.mkdir()
    target = tmp_path / "victim"
    monkeypatch.setattr(argv_files.secrets, "token_hex", lambda _n: "fixed")
    (ws / ".gateway-argv-mcp-fixed").symlink_to(target)
    options = ClaudeAgentOptions(
        mcp_servers={"probe": {"type": "http", "url": "http://x"}}, cwd=str(ws)
    )
    with pytest.raises(FileExistsError):
        argv_files.externalize(options, shared_uid=True)
    assert not target.exists()
    assert isinstance(options.mcp_servers, dict)


def test_modes_and_sdk_servers(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    options = ClaudeAgentOptions(
        mcp_servers={"probe": {"type": "http", "url": "http://x"}},
        system_prompt="p",
        cwd=str(ws),
    )
    files = argv_files.externalize(options, shared_uid=False)
    modes = {p.name.split("-")[2]: p.stat().st_mode & 0o777 for p in files.paths}
    assert modes == {"mcp": 0o644, "system": 0o644}
    files.cleanup()
    assert not files.paths and not list(ws.iterdir())
    sdk = ClaudeAgentOptions(
        mcp_servers={"inproc": {"type": "sdk", "name": "inproc"}}, cwd=str(ws)
    )
    files = argv_files.externalize(sdk, shared_uid=True)
    assert isinstance(sdk.mcp_servers, dict) and not files.paths
