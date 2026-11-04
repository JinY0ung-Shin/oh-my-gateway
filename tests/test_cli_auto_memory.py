"""Pin that the CLI's auto-memory loads ``<workspace>/.memory/MEMORY.md`` (#270).

The base prompts promise that ``{{MEMORY_PATH}}/MEMORY.md`` (=
``<cwd>/.memory``) "is always loaded into your conversation context", but the
CLI loads auto-memory from ``autoMemoryDirectory``, which defaults to
``<config dir>/projects/<cwd>/memory``: the file users kept in ``.memory`` never
reached the model. ``ClaudeCodeCLI._configure_auto_memory`` hands the CLI the
workspace path through ``--settings`` (flag settings; the CLI ignores the key in
the agent-writable project settings).

The unit tests pin the option the gateway builds; the integration tests drive
the real bundled CLI (CLI 2.1.283) against a fake Messages API and count a
marker from ``MEMORY.md`` in what reaches the model, with a control that drops
the key (so the pin is not vacuous) and a resume that picks up an edit.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

_TURN_TIMEOUT = 60
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)


def _make_cli(cwd: str):
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd=cwd)


def _settings(cli_cwd: Path, **kwargs) -> dict:
    return json.loads(_make_cli(str(cli_cwd))._build_sdk_options(**kwargs).settings)


class TestAutoMemoryOption:
    def test_points_at_the_workspace_memory_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLANS_DIRECTORY", raising=False)
        settings = _settings(tmp_path)
        assert settings["autoMemoryDirectory"] == str(tmp_path / ".memory")
        assert settings["plansDirectory"] == ".claude/plans"

    def test_follows_the_session_workspace(self, tmp_path):
        ws = tmp_path / "users" / "alice" / "claude"
        settings = _settings(tmp_path, cwd=ws)
        assert settings["autoMemoryDirectory"] == str(ws / ".memory")

    def test_merges_with_plans_and_the_read_boundary(self, tmp_path, monkeypatch):
        from src.backends.claude.client import READ_BOUNDARY_ENV

        monkeypatch.setenv(READ_BOUNDARY_ENV, "true")
        monkeypatch.setenv("CLAUDE_PLANS_DIRECTORY", "docs/plans")
        assert _settings(tmp_path) == {
            "plansDirectory": "docs/plans",
            "autoMemoryDirectory": str(tmp_path / ".memory"),
            "permissions": {"blockReadsOutsideWorkingDirectories": True},
        }

    def test_creates_nothing(self, tmp_path):
        # A missing dir only loads nothing; the Write tool makes it on the
        # first memory, and resolve_request_placeholders keeps the prompt's
        # "directory already exists" when the prompt names it.
        _settings(tmp_path)
        assert not (tmp_path / ".memory").exists()


@pytest.fixture
def _no_inherited_claude_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


async def _turn(
    api: FakeAnthropicAPI,
    home: Path,
    ws: Path,
    settings: Optional[str],
    *,
    session_id: Optional[str] = None,
    resume: Optional[str] = None,
) -> str:
    options = ClaudeAgentOptions(
        cwd=str(ws),
        model="claude-sonnet-5",
        env={**api.cli_env(home), "CLAUDE_CODE_HARBOR_KITE": "0"},
        setting_sources=["user", "project", "local"],
        system_prompt=None if resume else "You are a test agent.",
        max_turns=1,
        session_id=session_id,
        resume=resume,
        settings=settings,
    )
    client = ClaudeSDKClient(options=options)
    await asyncio.wait_for(client.connect(), timeout=_TURN_TIMEOUT)
    try:

        async def run() -> None:
            await client.query("hello")
            async for _ in client.receive_response():
                pass

        await asyncio.wait_for(run(), timeout=_TURN_TIMEOUT)
    finally:
        await client.disconnect()
    requests = api.model_requests()
    assert requests, "the CLI never reached the fake Messages API"
    return json.dumps(requests[-1]["body"])


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".memory").mkdir(parents=True)
    (ws / ".memory" / "MEMORY.md").write_text("- [a](a.md) — MEMORY-MARKER-ONE\n")
    return ws


@pytest.mark.integration
@pytest.mark.usefixtures("_no_inherited_claude_env")
class TestBundledCliAutoMemory:
    async def test_gateway_settings_load_the_workspace_memory(self, tmp_path):
        ws = _workspace(tmp_path)
        settings = _make_cli(str(ws))._build_sdk_options().settings
        with FakeAnthropicAPI() as api:
            body = await _turn(api, tmp_path / "home", ws, settings)
        assert body.count("MEMORY-MARKER-ONE") == 1

    async def test_control_without_the_key_it_never_loads(self, tmp_path):
        ws = _workspace(tmp_path)
        settings = json.loads(_make_cli(str(ws))._build_sdk_options().settings)
        settings.pop("autoMemoryDirectory")
        with FakeAnthropicAPI() as api:
            body = await _turn(api, tmp_path / "home", ws, json.dumps(settings))
        assert "MEMORY-MARKER-ONE" not in body

    async def test_a_resumed_session_sees_an_edit(self, tmp_path):
        """MEMORY.md rides a system-reminder, not the system prompt, so the
        resume's system-prompt snapshot does not freeze it."""
        ws = _workspace(tmp_path)
        settings = _make_cli(str(ws))._build_sdk_options().settings
        sid = str(uuid.uuid4())
        with FakeAnthropicAPI() as api:
            home = tmp_path / "home"
            await _turn(api, home, ws, settings, session_id=sid)
            with (ws / ".memory" / "MEMORY.md").open("a") as fh:
                fh.write("- [b](b.md) — MEMORY-MARKER-TWO\n")
            body = await _turn(api, home, ws, settings, resume=sid)
        assert "MEMORY-MARKER-TWO" in body


@pytest.mark.integration
@pytest.mark.usefixtures("_no_inherited_claude_env")
class TestConfinedAutoMemory:
    """With confinement on, ``argv_files.externalize`` moves the settings (and
    their workspace path) into a file the CLI reads once by a relative name;
    the memory still loads after that file is gone."""

    async def test_it_loads_from_the_externalized_settings(self, tmp_path, monkeypatch):
        import tempfile

        from src.backends.claude import argv_files, landlock
        from tests.test_cli_landlock_hooks import _landlock_unusable

        reason = _landlock_unusable()
        if reason:
            pytest.skip(reason)
        # The grant filter refuses entries inside the system temp dir.
        systmp = tmp_path / "systmp"
        systmp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(systmp))
        ws = _workspace(tmp_path)
        with FakeAnthropicAPI() as api:
            env = api.cli_env(tmp_path / "home")
            monkeypatch.setenv("HOME", env["HOME"])
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
            monkeypatch.setenv(landlock.MODE_ENV, "landlock")
            landlock.reset_mode_cache()
            try:
                built = _make_cli(str(ws))._build_sdk_options()
                options = ClaudeAgentOptions(
                    cwd=str(ws),
                    model="claude-sonnet-5",
                    cli_path=landlock.launcher_path(),
                    env={
                        **env,
                        **landlock.options_env(ws),
                        "CLAUDE_CODE_HARBOR_KITE": "0",
                    },
                    setting_sources=["user", "project", "local"],
                    system_prompt="You are a test agent.",
                    settings=built.settings,
                    sandbox=built.sandbox,
                    max_turns=1,
                )
                files = argv_files.externalize(options, shared_uid=True)
                assert not options.settings.startswith("{")
                client = ClaudeSDKClient(options=options)
                await asyncio.wait_for(client.connect(), timeout=_TURN_TIMEOUT)
                files.cleanup()  # as create_client does once the CLI started
                try:

                    async def run() -> None:
                        await client.query("hello")
                        async for _ in client.receive_response():
                            pass

                    await asyncio.wait_for(run(), timeout=_TURN_TIMEOUT)
                finally:
                    await client.disconnect()
            finally:
                landlock.reset_mode_cache()
            body = json.dumps(api.model_requests()[-1]["body"])
        assert body.count("MEMORY-MARKER-ONE") == 1
