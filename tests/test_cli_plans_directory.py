"""Pin where plan mode writes its plan file.

The CLI defaults ``plansDirectory`` to ``~/.claude/plans``. Every gateway child
shares one HOME, so without an override every user's plans land in one shared
directory, which the workspace sandbox (rightly) no longer lets a session read
or write. ``ClaudeCodeCLI._configure_plans_directory`` hands the CLI a
workspace-relative ``plansDirectory`` through ``--settings``.

The unit tests pin the option the gateway builds; the integration test drives
the real bundled CLI (CLI 2.1.283) in plan mode against a fake Messages API and
reads the plan-file path from the plan-mode reminder the CLI sends upstream.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from src.backends.claude.client import DEFAULT_PLANS_DIRECTORY
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

_TURN_TIMEOUT = 60
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)
_PLAN_PATH_RE = re.compile(r"[\w/.-]*/plans/[\w.-]+\.md")


def _make_cli():
    """Create a ClaudeCodeCLI instance with auth mocked out."""
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd="/tmp")


class TestPlansDirectoryOption:
    def test_default_is_workspace_relative(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLANS_DIRECTORY", raising=False)
        options = _make_cli()._build_sdk_options()
        assert json.loads(options.settings) == {"plansDirectory": ".claude/plans"}
        assert DEFAULT_PLANS_DIRECTORY == ".claude/plans"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLANS_DIRECTORY", " docs/plans ")
        options = _make_cli()._build_sdk_options()
        assert json.loads(options.settings) == {"plansDirectory": "docs/plans"}

    def test_empty_env_keeps_cli_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLANS_DIRECTORY", "")
        options = _make_cli()._build_sdk_options()
        assert options.settings is None


@pytest.fixture
def _no_inherited_claude_env(monkeypatch):
    """Keep the runner's own Claude env out of the CLI child."""
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


async def _plan_file_paths(tmp_path: Path, settings: Optional[str]) -> set:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with FakeAnthropicAPI() as api:
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-5",
            env={**api.cli_env(tmp_path / "home"), "CLAUDE_CODE_HARBOR_KITE": "0"},
            permission_mode="plan",
            max_turns=2,
            session_id=str(uuid.uuid4()),
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
    return set(_PLAN_PATH_RE.findall(json.dumps(requests[-1]["body"])))


@pytest.mark.integration
@pytest.mark.usefixtures("_no_inherited_claude_env")
class TestBundledCliPlanFile:
    async def test_gateway_settings_put_the_plan_in_the_workspace(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("CLAUDE_PLANS_DIRECTORY", raising=False)
        settings = _make_cli()._build_sdk_options().settings
        paths = await _plan_file_paths(tmp_path, settings)
        assert paths, "plan-mode reminder carried no plan-file path"
        plans = tmp_path / "ws" / ".claude" / "plans"
        assert all(Path(p).parent == plans for p in paths), paths

    async def test_without_settings_the_cli_uses_shared_home(self, tmp_path):
        # Control: proves the reminder path moves with the setting.
        paths = await _plan_file_paths(tmp_path, None)
        plans = tmp_path / "home" / ".claude" / "plans"
        assert paths and all(Path(p).parent == plans for p in paths), paths
