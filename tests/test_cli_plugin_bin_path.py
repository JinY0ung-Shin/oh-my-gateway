"""A confined session runs an enabled plugin's ``bin/`` tools by name (#267).

The CLI (2.1.283) puts an enabled plugin's ``bin/`` on the Bash tool's PATH
only while it writes its shell snapshot. A confined CLI cannot write the
shared ``shell-snapshots`` dir (by design: every session sources its snapshot
before each command), so every bare-name plugin command exited 127 and only
``${CLAUDE_PLUGIN_ROOT}/bin/<tool>`` worked. The gateway now appends those dirs
to the confined CLI's own PATH (``landlock._plugin_path_env``).

Drives the real CLI through the launcher with the gateway's own
``landlock.options_env`` and a fake Messages API; the control drops the PATH
entry and sees the 127, so the pin is not vacuous.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from src.backends.claude import landlock
from tests.fixtures.cli_plugin import install_plugin
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI
from tests.test_cli_landlock_hooks import _HARNESS_ENV, _landlock_unusable

_TIMEOUT = 120
_TOOL = "omg-probe-tool"
_COMMAND = f"{_TOOL} hello; echo rc=$?"


def _plan(body: Dict[str, Any]) -> Dict[str, Any]:
    last = (body.get("messages") or [{}])[-1].get("content")
    if isinstance(last, list) and any(b.get("type") == "tool_result" for b in last):
        return {"text": "done"}
    return {"tool_use": {"name": "Bash", "input": {"command": _COMMAND}}}


def _bash_output(api: FakeAnthropicAPI) -> str:
    for req in reversed(api.model_requests()):
        for message in req["body"].get("messages", []):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result":
                    out = block.get("content")
                    if isinstance(out, list):
                        return "".join(part.get("text", "") for part in out)
                    return str(out)
    return ""


@pytest.fixture
def confined_plugin(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)
    # The grant filter refuses entries inside the system temp dir, where all of
    # this lives: give the gateway a temp dir of its own below it instead.
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    with FakeAnthropicAPI(plan=_plan) as api:
        env = api.cli_env(tmp_path / "home")
        real_cli = landlock.resolve_real_cli()
        assert real_cli, "no bundled CLI"
        install_plugin(
            real_cli,
            env,
            tmp_path,
            name="binplug",
            bins={_TOOL: '#!/bin/sh\necho "probe-ok $1"\n'},
        )
        settings = json.loads(
            (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").read_text()
        )
        assert settings["enabledPlugins"]["binplug@testmkt"] is True
        monkeypatch.setenv("HOME", env["HOME"])
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
        monkeypatch.setenv(landlock.MODE_ENV, "landlock")
        landlock.reset_mode_cache()
        ws = tmp_path / "ws"
        ws.mkdir()
        yield api, env, ws
    landlock.reset_mode_cache()


async def _one_turn(env: Dict[str, str], ws: Path, confinement: Dict[str, str]):
    sid = str(uuid.uuid4())
    options = ClaudeAgentOptions(
        cwd=str(ws),
        model="claude-sonnet-5",
        cli_path=landlock.launcher_path(),
        # The CLI's PATH comes from the SDK's {**os.environ, **options.env}.
        env={**env, **confinement, "CLAUDE_CODE_HARBOR_KITE": "0"},
        setting_sources=["user", "project", "local"],
        permission_mode="bypassPermissions",
        session_id=sid,
        max_turns=3,
    )
    client = ClaudeSDKClient(options=options)
    await asyncio.wait_for(client.connect(), _TIMEOUT)
    try:

        async def turn() -> None:
            await client.query("run the tool")
            async for _ in client.receive_response():
                pass

        await asyncio.wait_for(turn(), _TIMEOUT)
    finally:
        await client.disconnect()


@pytest.mark.integration
@pytest.mark.skipif(bool(_landlock_unusable()), reason=_landlock_unusable() or "")
class TestPluginBinOnPath:
    async def test_a_bare_name_plugin_command_runs_confined(self, confined_plugin):
        api, env, ws = confined_plugin
        confinement = landlock.options_env(ws)
        assert confinement["PATH"].split(":")[-1].endswith("/bin")
        await _one_turn(env, ws, confinement)
        output = _bash_output(api)
        assert "probe-ok hello" in output and "rc=0" in output, output

    async def test_control_without_the_path_entry_it_is_127(
        self, confined_plugin, monkeypatch
    ):
        api, env, ws = confined_plugin
        monkeypatch.setattr(landlock, "_plugin_path_env", lambda ro: {})
        confinement = landlock.options_env(ws)
        assert "PATH" not in confinement
        await _one_turn(env, ws, confinement)
        output = _bash_output(api)
        assert "rc=127" in output, output
