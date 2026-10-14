"""Pin the slash/skill preflight's side effects against the bundled CLI (#241).

``slash_commands`` reads the command catalog with a throwaway CLI started in the
session workspace. With project settings loaded, that CLI used to run the
workspace's own hooks (InstructionsLoaded, SessionStart, SessionEnd), a plugin's
hooks and the workspace ``.mcp.json`` stdio servers during connect +
``get_server_info`` — as the gateway uid, for whatever ``/name`` a client sent.
A workspace ``apiKeyHelper`` ran too, and saw ``ADMIN_API_KEY`` / ``API_KEY``.
The preflight now runs with every hook disabled and every startup settings
command blanked (``--settings {"disableAllHooks": true, "apiKeyHelper": "",
...}``), no MCP server (``--strict-mcp-config``), and the same secret blanks a
session child gets (``client.child_secret_env``).

The first test drives the same workspace twice against the bundled CLI
(2.1.283): with plain options (every side effect fires) and through the real
preflight (none fires), and the two catalogs must be identical — workspace,
user and plugin commands and skills included. The second lets the helper run
(the settings blanks off) and checks it sees no gateway secret, against a plain
control that sees them all. When a CLI bump breaks either, re-measure both
halves before shipping.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Set

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from src.backends.claude import landlock
from src.backends.claude import slash_commands as sc
from tests.fixtures.cli_plugin import install_plugin
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

_TIMEOUT = 120
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)
_PROJECT_EVENTS = ("InstructionsLoaded", "SessionStart", "SessionEnd")
_SECRETS = {
    "ADMIN_API_KEY": "admin-key-7c1e",
    "API_KEY": "api-key-5d2a",
    "OPENAI_API_KEY": "openai-key-9b3f",
}
_EXPECTED_HOOKS = {*_PROJECT_EVENTS, "plugin-SessionStart"}
_CATALOG = {"wscmd", "wsskill", "usercmd", "userskill", "hookplug:plugskill"}

# A stdio MCP server that records that it started, then answers the handshake.
_MCP_SERVER = """\
import json, sys
open(sys.argv[1], "a").write("started\\n")
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    result = {}
    if msg.get("method") == "initialize":
        result = {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "marker", "version": "0"},
        }
    elif msg.get("method") == "tools/list":
        result = {"tools": []}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
"""


@pytest.fixture
def _no_inherited_claude_env(monkeypatch):
    """Keep the runner's own Claude env out of the CLI child."""
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)
    monkeypatch.delenv(landlock.MODE_ENV, raising=False)
    landlock.reset_mode_cache()
    yield
    landlock.reset_mode_cache()


def _hook(marks: Path, name: str) -> Dict:
    return {
        "hooks": [{"type": "command", "command": f'echo {name} >> "{marks}/hooks.txt"'}]
    }


def _side_effects(marks: Path) -> Dict[str, List[str]]:
    return {
        f.stem: f.read_text().split()
        for f in sorted(marks.iterdir())
        if f.suffix == ".txt"
    }


def _helper(marks: Path) -> str:
    """An apiKeyHelper that records that it ran and the env it saw."""
    return (
        f'echo ran >> "{marks}/helper.txt"; env > "{marks}/helper.env";'
        " echo sk-from-helper"
    )


async def _plain_catalog(ws: Path) -> Set[str]:
    options = ClaudeAgentOptions(
        cwd=str(ws), setting_sources=["user", "project", "local"]
    )
    async with ClaudeSDKClient(options=options) as client:
        info = await client.get_server_info()
    return {c["name"] for c in (info or {}).get("commands") or []}


async def _settle(marks: Path, expected: Set[str]) -> None:
    # SessionEnd runs while the CLI shuts down, after the client returned.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if expected <= set(_side_effects(marks).get("hooks", [])):
            return
        await asyncio.sleep(0.1)


@pytest.mark.integration
@pytest.mark.usefixtures("_no_inherited_claude_env")
async def test_preflight_runs_no_hook_and_no_mcp_server(monkeypatch, tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    home = tmp_path / "home"
    cfg = home / ".claude"
    with FakeAnthropicAPI() as api:
        env = api.cli_env(home)
        real_cli = landlock.resolve_real_cli()
        assert real_cli, "no bundled CLI"
        install_plugin(
            real_cli,
            env,
            tmp_path,
            hooks={"SessionStart": [_hook(marks, "plugin-SessionStart")]},
            skill="plugskill",
        )
        (cfg / "skills" / "userskill").mkdir(parents=True)
        (cfg / "skills" / "userskill" / "SKILL.md").write_text(
            "---\nname: userskill\ndescription: u\n---\nbody\n"
        )
        (cfg / "commands").mkdir()
        (cfg / "commands" / "usercmd.md").write_text("---\ndescription: u\n---\nb\n")

        ws = tmp_path / "ws"
        (ws / ".claude" / "commands").mkdir(parents=True)
        (ws / ".claude" / "skills" / "wsskill").mkdir(parents=True)
        (ws / ".claude" / "commands" / "wscmd.md").write_text(
            "---\ndescription: w\n---\nb\n"
        )
        (ws / ".claude" / "skills" / "wsskill" / "SKILL.md").write_text(
            "---\nname: wsskill\ndescription: w\n---\nbody\n"
        )
        (ws / "CLAUDE.md").write_text("# project instructions\n")
        (ws / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "apiKeyHelper": _helper(marks),
                    "enableAllProjectMcpServers": True,
                    "hooks": {e: [_hook(marks, e)] for e in _PROJECT_EVENTS},
                }
            )
        )
        server = tmp_path / "mcp_marker.py"
        server.write_text(_MCP_SERVER)
        (ws / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "wsmcp": {
                            "command": sys.executable,
                            "args": ["-I", str(server), str(marks / "mcp.txt")],
                        }
                    }
                }
            )
        )
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,project,local")

        # Control: the same workspace with plain options runs everything.
        plain = await asyncio.wait_for(_plain_catalog(ws), _TIMEOUT)
        await _settle(marks, _EXPECTED_HOOKS)
        control = _side_effects(marks)
        for f in marks.iterdir():
            f.unlink()

        sc._cache.reset()
        names = await asyncio.wait_for(sc._fetch_commands(ws), _TIMEOUT)
        await asyncio.sleep(1.5)  # as long as SessionEnd needs in the control
        gated = _side_effects(marks)
        messages = list(api.requests)

    assert set(control["hooks"]) == _EXPECTED_HOOKS, control
    assert control["mcp"] == ["started"], control
    assert control["helper"] == ["ran"], control
    assert gated == {}, gated
    assert _CATALOG <= plain, sorted(plain)
    assert names == plain
    assert messages == []  # the preflight never reaches the Messages API


@pytest.mark.integration
@pytest.mark.usefixtures("_no_inherited_claude_env")
async def test_a_helper_that_runs_sees_no_gateway_secret(monkeypatch, tmp_path):
    # Belt and braces: even with the settings blanks off (so the workspace
    # helper runs), the preflight child carries the session's secret blanks.
    marks = tmp_path / "marks"
    marks.mkdir()
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(
        json.dumps({"apiKeyHelper": _helper(marks)})
    )
    with FakeAnthropicAPI() as api:
        for key, value in api.cli_env(home).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,project,local")
        monkeypatch.setenv("SYSINFO_REDACTION", "true")
        for key, value in _SECRETS.items():
            monkeypatch.setenv(key, value)

        await asyncio.wait_for(_plain_catalog(ws), _TIMEOUT)
        await asyncio.sleep(1.0)
        control = (marks / "helper.env").read_text()
        for f in marks.iterdir():
            f.unlink()

        monkeypatch.setattr(
            sc, "_PREFLIGHT_SETTINGS", json.dumps({"disableAllHooks": True})
        )
        sc._cache.reset()
        await asyncio.wait_for(sc._fetch_commands(ws), _TIMEOUT)
        await asyncio.sleep(1.0)
        gated = (marks / "helper.env").read_text()

    assert all(value in control for value in _SECRETS.values())
    assert not [k for k, value in _SECRETS.items() if value in gated], gated


_REPO = Path(__file__).resolve().parents[1]
_BROKER = _REPO / "docker" / "isolation_broker.py"


def _same_uid_broker_unusable() -> str:
    if landlock.landlock_abi() < landlock.MIN_LANDLOCK_ABI:
        return f"Landlock ABI {landlock.landlock_abi()} < {landlock.MIN_LANDLOCK_ABI}"
    if os.getuid() == 0 or os.getuid() != os.getgid():
        return "the --test-same-uid broker needs a non-root uid equal to its gid"
    return ""


@pytest.mark.integration
@pytest.mark.usefixtures("_no_inherited_claude_env")
@pytest.mark.skipif(
    bool(_same_uid_broker_unusable()), reason=_same_uid_broker_unusable() or ""
)
async def test_uid_mode_preflight_runs_through_the_broker(monkeypatch, tmp_path):
    # uid mode end to end: the session-workspace preflight goes gateway ticket
    # -> launcher -> broker (--test-same-uid: the band maps "alice" onto this
    # uid, the real cross-uid switch is container-only) -> confined CLI, and
    # still returns the workspace and user catalog, with no helper or hook run.
    from src.backends.claude import uid_isolation as ui

    uid = os.getuid()
    marks = tmp_path / "marks"
    marks.mkdir()
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    (ws / ".claude" / "commands").mkdir(parents=True)
    (ws / ".claude" / "commands" / "wscmd.md").write_text(
        "---\ndescription: w\n---\nb\n"
    )
    (ws / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "apiKeyHelper": _helper(marks),
                "hooks": {"SessionStart": [_hook(marks, "SessionStart")]},
            }
        )
    )
    with FakeAnthropicAPI() as api:
        for key, value in api.cli_env(home).items():
            monkeypatch.setenv(key, value)
        (home / ".claude" / "skills" / "userskill").mkdir(parents=True)
        (home / ".claude" / "skills" / "userskill" / "SKILL.md").write_text(
            "---\nname: userskill\ndescription: u\n---\nbody\n"
        )
        sock = tmp_path / "b.sock"
        for key, value in {
            "CLAUDE_SETTING_SOURCES": "user,project,local",
            landlock.MODE_ENV: "uid",
            landlock.BROKER_SOCKET_ENV: str(sock),
            ui.TICKET_SECRET_ENV: secrets.token_hex(16),
            "CLAUDE_ISOLATION_UID_MIN": str(uid),
            "CLAUDE_ISOLATION_UID_MAX": str(uid + 1),
            "APP_UID": str(uid - 1),
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
            # The preflight runs as the user's uid only once the user has one
            # (a session spawn allocates it); give alice hers first.
            ui.resolve_uid("alice")
            names = await asyncio.wait_for(
                sc._fetch_commands(ws, user="alice"), _TIMEOUT
            )
            await asyncio.sleep(1.0)
        finally:
            broker.kill()
            broker.wait(timeout=10)
            landlock.reset_mode_cache()
            landlock.reset_uid_probe()
            ui.reset_registry_cache()
            ui.reset_captured_secret()
        messages = list(api.requests)

    assert {"wscmd", "userskill"} <= names, sorted(names)
    assert json.loads((tmp_path / "uids.json").read_text()) == {"alice": uid}
    assert _side_effects(marks) == {}  # no helper, no hook
    assert messages == []
