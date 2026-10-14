"""Pin argv externalization (``src/backends/claude/argv_files.py``) against the
bundled CLI (2.1.283).

A confined session can read every CLI's ``/proc/<pid>/cmdline``, so with
isolation on the gateway hands the MCP config and the system prompt over as
per-spawn files and removes them as soon as ``connect()`` returns. That is only
safe while the CLI reads those files during argument parsing and never again:

* after the files are gone, a turn still calls the MCP server (with the header
  that only the config file carried) and the model request still carries the
  appended / custom system prompt;
* the live CLI's command line holds neither secret, only the file paths.

If a CLI bump starts reading ``--mcp-config`` / ``--*system-prompt-file``
lazily, these fail: keep the files until the session ends instead.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
import uvicorn
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import ToolResultBlock, UserMessage
from mcp.server.fastmcp import Context, FastMCP

from src.backends.claude import argv_files
from src.backends.claude.sdk_client import GatewayClaudeSDKClient
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

_TURN_TIMEOUT = 90
_HEADER_SECRET = "hdr-secret-4f1c9a"
_PROMPT_SECRET = "Current user: prompt-secret-7b2e"
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)


@pytest.fixture(autouse=True)
def _no_inherited_claude_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


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


async def _turn(url: str, root: Path, system_prompt: Any):
    ws = root / "ws"
    ws.mkdir()
    results: List[str] = []
    with FakeAnthropicAPI(_plan) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(root / "home"),
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
        files = argv_files.externalize(options, shared_uid=True)
        assert files.paths and all(p.is_file() for p in files.paths)
        client = GatewayClaudeSDKClient(options=options)
        try:
            await client.connect(prompt=None)
        finally:
            files.cleanup()
        try:
            assert not list(ws.glob(".gateway-argv-*")), "argv files left behind"
            pid = client._transport._process.pid
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
            await client.query("go")
            async for message in client.receive_response():
                if isinstance(message, UserMessage) and isinstance(
                    message.content, list
                ):
                    results += [
                        str(b.content)
                        for b in message.content
                        if isinstance(b, ToolResultBlock)
                    ]
        finally:
            await client.disconnect()
        systems = [_system_text(r["body"]) for r in api.model_requests()]
    return cmdline, results, systems


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
    cmdline, results, systems = await asyncio.wait_for(
        _turn(url, tmp_path, system_prompt), _TURN_TIMEOUT
    )
    assert _HEADER_SECRET not in cmdline
    assert _PROMPT_SECRET not in cmdline
    assert ".gateway-argv-mcp-" in cmdline
    # The CLI kept what it parsed: the MCP server got the header only the
    # deleted config carried, and the model got the deleted prompt.
    assert any("echo-done" in r for r in results), results
    assert seen and all(h == _HEADER_SECRET for h in seen)
    assert systems and all(_PROMPT_SECRET in s for s in systems)


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
