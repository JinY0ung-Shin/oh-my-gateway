"""Pin hooks under Landlock confinement against the bundled CLI (#241).

A confined session writes only what is its own: the workspace, its project dir
and its ``session-env/<session id>`` dir. ``plugins/data`` and the npm cache are
read-only, because in ``landlock`` mode every session shares one uid and a
writable shared store lets one session plant what another's plugin hooks,
``npx`` or ``uv run`` run next. Before it runs a hook, the CLI (2.1.283)
creates two dirs: the plugin's ``${CLAUDE_PLUGIN_DATA}`` and, for SessionStart,
the session's ``$CLAUDE_ENV_FILE`` dir. When either mkdir fails it does not run
the hook ("Hook failed to run ... EACCES ... mkdir"). The gateway pre-creates
both (``landlock._ensure_plugin_data_dirs`` / ``_session_env_dir``), so the
hooks still run.

These tests drive the real CLI through the launcher with the gateway's own
``landlock.options_env`` lists and a fake Messages API. The control skips both
pre-creations and sees every hook fail, so the pin is not vacuous.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Dict, Optional

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from src.backends.claude import landlock
from tests.fixtures.cli_plugin import install_plugin
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

_TIMEOUT = 120
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)


def _landlock_unusable() -> Optional[str]:
    abi = landlock.landlock_abi()
    if abi < landlock.MIN_LANDLOCK_ABI:
        return f"Landlock ABI {abi} < {landlock.MIN_LANDLOCK_ABI} (need kernel >= 6.2)"
    launcher = Path(landlock.launcher_path())
    if not (launcher.is_file() and os.access(launcher, os.X_OK)):
        return f"launcher {launcher} not executable"
    return None


def _command(cmd: str) -> Dict:
    return {"hooks": [{"type": "command", "command": cmd}]}


# Each hook appends one word to a file in the workspace (always writable): that
# it ran, and whether a write it attempts outside the session's own set landed.
_TRY_PLUGIN_DATA = (
    '( touch "$CLAUDE_PLUGIN_DATA/probe" 2>/dev/null && echo wrote || echo denied )'
    ' >> "$CLAUDE_PROJECT_DIR/plugin-prompt.txt"'
)
_TRY_NPM_CACHE = (
    '( mkdir -p "$HOME/.npm/probe" 2>/dev/null && echo wrote || echo denied )'
    ' >> "$CLAUDE_PROJECT_DIR/project-start.txt"'
)
_PLUGIN_HOOKS = {
    "SessionStart": [_command('echo ran >> "$CLAUDE_PROJECT_DIR/plugin-start.txt"')],
    "UserPromptSubmit": [_command(_TRY_PLUGIN_DATA)],
}


@pytest.fixture
def confined_home(monkeypatch, tmp_path):
    """An isolated CLI home with the hook plugin installed, landlock mode on."""
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)
    with FakeAnthropicAPI() as api:
        home = tmp_path / "home"
        env = api.cli_env(home)
        real_cli = landlock.resolve_real_cli()
        assert real_cli, "no bundled CLI"
        install_plugin(real_cli, env, tmp_path, hooks=_PLUGIN_HOOKS)
        # The gateway process sees the same config dir the CLI child does.
        monkeypatch.setenv("HOME", env["HOME"])
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
        monkeypatch.setenv(landlock.MODE_ENV, "landlock")
        landlock.reset_mode_cache()
        ws = tmp_path / "ws"
        (ws / ".claude").mkdir(parents=True)
        (ws / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": [_command(_TRY_NPM_CACHE)]}})
        )
        yield api, env, ws
    landlock.reset_mode_cache()


async def _one_turn(api, env, ws: Path, sid: str, confinement: Dict[str, str]):
    options = ClaudeAgentOptions(
        cwd=str(ws),
        model="claude-sonnet-5",
        cli_path=landlock.launcher_path(),
        env={**env, **confinement, "CLAUDE_CODE_HARBOR_KITE": "0"},
        setting_sources=["user", "project", "local"],
        session_id=sid,
        max_turns=2,
    )
    client = ClaudeSDKClient(options=options)
    await asyncio.wait_for(client.connect(), _TIMEOUT)
    try:

        async def turn() -> None:
            await client.query("hi")
            async for _ in client.receive_response():
                pass

        await asyncio.wait_for(turn(), _TIMEOUT)
    finally:
        await client.disconnect()
    assert api.model_requests(), "the CLI never reached the fake Messages API"


def _words(ws: Path, name: str) -> Optional[list]:
    path = ws / name
    return path.read_text().split() if path.exists() else None


@pytest.mark.integration
@pytest.mark.skipif(bool(_landlock_unusable()), reason=_landlock_unusable() or "")
class TestConfinedHooks:
    async def test_plugin_and_session_start_hooks_run(self, confined_home):
        api, env, ws = confined_home
        sid = str(uuid.uuid4())
        await _one_turn(api, env, ws, sid, landlock.options_env(ws, session_id=sid))
        assert _words(ws, "plugin-start.txt") == ["ran"]
        # The hooks ran, and the shared stores stayed read-only to them.
        assert _words(ws, "plugin-prompt.txt") == ["denied"]
        assert _words(ws, "project-start.txt") == ["denied"]

    async def test_control_without_the_precreated_dirs_hooks_fail(
        self, confined_home, monkeypatch
    ):
        api, env, ws = confined_home
        monkeypatch.setattr(landlock, "_ensure_plugin_data_dirs", lambda _home: None)
        sid = str(uuid.uuid4())
        # No session id: session-env/<sid> is neither created nor granted.
        await _one_turn(api, env, ws, sid, landlock.options_env(ws))
        assert _words(ws, "plugin-start.txt") is None
        assert _words(ws, "plugin-prompt.txt") is None  # data dir missing only
        assert _words(ws, "project-start.txt") is None  # session-env missing only
