"""Pin the read boundary (#234): reads stay inside the working directories.

``CLAUDE_BLOCK_READS_OUTSIDE_WORKSPACE`` hands the CLI its native
``permissions.blockReadsOutsideWorkingDirectories`` through ``--settings``, runs
the execution modes as ``bypassPermissions`` and makes the permission callback
refuse the read block's asks (``src/backends/claude/client.py``,
``READ_BOUNDARY_ENV``).

The unit tests pin the options and the callback; the integration tests drive
the real bundled CLI (CLI 2.1.283) against a fake Messages API with the
gateway's own settings, mode and callback, and read every tool result the CLI
sent back upstream. When a CLI bump breaks them, re-measure which asks reach
the callback before shipping — the boundary leans on that shape.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
)

from src.backends.claude.client import (
    READ_BOUNDARY_DENY_MESSAGE,
    READ_BOUNDARY_ENV,
    effective_permission_mode,
    plan_decision_result,
    plan_execution_mode,
)
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

_TURN_TIMEOUT = 120
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)
SECRET = "OUTSIDE_SECRET_7f3a"
INSIDE = "INSIDE_OK_51c2"


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


@pytest.fixture
def boundary_on(monkeypatch):
    monkeypatch.setenv(READ_BOUNDARY_ENV, "true")


@pytest.fixture
def boundary_off(monkeypatch):
    monkeypatch.delenv(READ_BOUNDARY_ENV, raising=False)


class TestOptions:
    def test_settings_carry_the_read_block(self, boundary_on, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLANS_DIRECTORY", raising=False)
        options = _make_cli()._build_sdk_options()
        assert json.loads(options.settings) == {
            "plansDirectory": ".claude/plans",
            "permissions": {"blockReadsOutsideWorkingDirectories": True},
        }

    def test_off_by_default(self, boundary_off):
        options = _make_cli()._build_sdk_options(permission_mode="default")
        assert "permissions" not in json.loads(options.settings)
        assert options.permission_mode == "default"

    @pytest.mark.parametrize("raw", ["0", "false", "", "no"])
    def test_falsy_values_stay_off(self, monkeypatch, raw):
        monkeypatch.setenv(READ_BOUNDARY_ENV, raw)
        assert effective_permission_mode("default") == "default"

    @pytest.mark.parametrize(
        ("requested", "runs_as"),
        [
            (None, "bypassPermissions"),
            ("default", "bypassPermissions"),
            ("acceptEdits", "bypassPermissions"),
            ("bypassPermissions", "bypassPermissions"),
            ("plan", "plan"),
        ],
    )
    def test_execution_modes_run_as_bypass(self, boundary_on, requested, runs_as):
        options = _make_cli()._build_sdk_options(permission_mode=requested)
        assert options.permission_mode == runs_as

    async def test_continuation_mode_is_mapped_too(self, boundary_on):
        client = SimpleNamespace(set_permission_mode=AsyncMock())
        await _make_cli().update_request_policy(client, permission_mode="default")
        client.set_permission_mode.assert_awaited_once_with("bypassPermissions")
        client.set_permission_mode.reset_mock()
        await _make_cli().update_request_policy(client, permission_mode="plan")
        client.set_permission_mode.assert_awaited_once_with("plan")

    def test_approved_plan_continues_inside_the_boundary(
        self, boundary_on, monkeypatch
    ):
        monkeypatch.delenv("PERMISSION_MODE", raising=False)
        assert plan_execution_mode() == "bypassPermissions"
        result = plan_decision_result('{"approved": true}')
        assert result.updated_permissions[0].mode == "bypassPermissions"


class TestCallback:
    @staticmethod
    def _ctx(blocked_path: Optional[str] = None):
        return SimpleNamespace(tool_use_id="t1", blocked_path=blocked_path)

    async def test_refuses_the_read_blocks_asks(self, boundary_on):
        cb = _make_cli()._make_ask_user_can_use_tool(SimpleNamespace())
        bash = await cb("Bash", {"command": "cat /etc/passwd"}, self._ctx())
        assert isinstance(bash, PermissionResultDeny)
        assert bash.message == READ_BOUNDARY_DENY_MESSAGE
        named = await cb("Read", {"file_path": "/x"}, self._ctx("/x"))
        assert isinstance(named, PermissionResultDeny)

    async def test_other_asks_are_still_approved(self, boundary_on):
        cb = _make_cli()._make_ask_user_can_use_tool(SimpleNamespace())
        result = await cb("WebFetch", {"url": "https://example.com"}, self._ctx())
        assert isinstance(result, PermissionResultAllow)

    async def test_off_approves_bash_as_before(self, boundary_off):
        cb = _make_cli()._make_ask_user_can_use_tool(SimpleNamespace())
        result = await cb("Bash", {"command": "cat /etc/passwd"}, self._ctx("/etc"))
        assert isinstance(result, PermissionResultAllow)


# --------------------------------------------------------------------------
# Bundled CLI
# --------------------------------------------------------------------------


@pytest.fixture
def _no_inherited_claude_env(monkeypatch):
    """Keep the runner's own Claude env out of the CLI child."""
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


def _tool_results(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        block
        for message in body.get("messages", [])
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


def _bash(command: str) -> Tuple[str, Dict[str, Any]]:
    return ("Bash", {"command": command, "description": "probe"})


async def _run(
    tmp_path: Path,
    calls: List[Tuple[str, Dict[str, Any]]],
    mode: str,
    *,
    confine: bool = False,
) -> Tuple[List[str], List[str]]:
    """Run *calls* in one turn with the gateway's options; tool results + asks.

    With ``confine=True`` the CLI is spawned through the Landlock launcher
    (#241) with the gateway's own path lists (``landlock.options_env``, the
    ``landlock_on`` fixture points the gateway at the test HOME): the
    workspace, the session's project and session-env dirs are read-write, the
    shared assets read-only, everything else is denied.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "in.txt").write_text(f"{INSIDE}\n")
    (workspace / "ok.py").write_text("print(open('in.txt').read())\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(f"{SECRET}\n")
    (workspace / "rd.py").write_text(f"print(open('{outside}/secret.txt').read())\n")
    calls = [
        (name, json.loads(json.dumps(args).replace("@OUT", str(outside))))
        for name, args in calls
    ]

    def plan(body):
        done = len(_tool_results(body))
        if done < len(calls):
            name, args = calls[done]
            return {"tool_use": {"name": name, "input": args}}
        return {"text": "done"}

    cli = _make_cli()
    built = cli._build_sdk_options(permission_mode=mode, cwd=workspace)
    gateway_cb = cli._make_ask_user_can_use_tool(SimpleNamespace())
    asked: List[str] = []

    async def can_use_tool(tool_name, input_data, context):
        asked.append(tool_name)
        if tool_name == "AskUserQuestion":  # parking needs a live route
            return PermissionResultDeny(message="User responded: fine")
        return await gateway_cb(tool_name, input_data, context)

    env = {"CLAUDE_CODE_HARBOR_KITE": "0"}
    if os.geteuid() == 0:  # the CLI refuses bypassPermissions as root otherwise
        env["IS_SANDBOX"] = "1"
    session_id = str(uuid.uuid4())
    cli_path = None
    if confine:
        import sys

        from src.backends.claude import landlock

        cli_path = landlock.launcher_path()
        (tmp_path / "home" / ".claude").mkdir(parents=True, exist_ok=True)
        # The lists a session gets: everything else — the sibling 'outside'
        # dir, the gateway code — is denied by omission.
        confinement = landlock.options_env(workspace, session_id=session_id)
        assert confinement[landlock.MODE_ENV] == "landlock", confinement
        assert confinement[landlock.REAL_CLI_ENV], "no real CLI to confine"
        # sys.prefix is the dev venv, which lives UNDER the repo root. In
        # production ``python3`` is /usr/bin/python3 (already in the launcher's
        # ro system roots); here the CLI's Bash resolves it to the venv python,
        # so grant the venv read-only — only then does python actually start and
        # the closure tests prove the OUTSIDE read is blocked, not that python
        # failed to boot. It is a subtree, so the repo's src/ and data/ stay
        # denied (the /app protection the other tests check).
        ro = json.loads(confinement[landlock.RO_ENV])  # a JSON array
        confinement[landlock.RO_ENV] = json.dumps([*ro, sys.prefix])
        env.update(confinement)
    with FakeAnthropicAPI(plan) as api:
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-5",
            env={**api.cli_env(tmp_path / "home"), **env},
            permission_mode=built.permission_mode,
            settings=built.settings,
            max_turns=len(calls) + 2,
            session_id=session_id,
            can_use_tool=can_use_tool,
            **({"cli_path": cli_path} if cli_path else {}),
        )
        client = ClaudeSDKClient(options=options)
        await asyncio.wait_for(client.connect(), timeout=_TURN_TIMEOUT)
        try:

            async def turn() -> None:
                await client.query("go")
                async for _ in client.receive_response():
                    pass

            await asyncio.wait_for(turn(), timeout=_TURN_TIMEOUT)
        finally:
            await client.disconnect()
        requests = api.model_requests()
    assert requests, "the CLI never reached the fake Messages API"
    results = _tool_results(requests[-1]["body"])
    assert len(results) == len(calls), results
    return [json.dumps(r.get("content"), ensure_ascii=False) for r in results], asked


_OUTSIDE_READS = [
    ("Read", {"file_path": "@OUT/secret.txt"}),
    ("Read", {"file_path": "/etc/passwd"}),
    _bash("cat @OUT/secret.txt"),
    _bash("head -1 /etc/passwd"),
    _bash("cd ../outside && cat secret.txt"),
    _bash("D=@OUT; cat $D/secret.txt"),
    _bash("python3 -c \"print(open('@OUT/secret.txt').read())\""),
]
_INSIDE_WORK = [
    ("Read", {"file_path": "in.txt"}),
    _bash("cat in.txt"),
    _bash("python3 ok.py"),
]


def _leaks(result: str) -> bool:
    return SECRET in result or "root:x:0:0" in result


@pytest.mark.integration
@pytest.mark.usefixtures("_no_inherited_claude_env")
# The SDK's "can_use_tool will not be invoked" under bypassPermissions is false:
# these tests are the proof (the gateway filters it at import, see client.py).
@pytest.mark.filterwarnings("ignore::claude_agent_sdk.types.CanUseToolShadowedWarning")
class TestBundledCliReadBoundary:
    async def test_default_requests_cannot_read_outside(self, tmp_path, boundary_on):
        # ChatDRAGON sends permission_mode=default for ordinary turns.
        results, _ = await _run(tmp_path, _OUTSIDE_READS + _INSIDE_WORK, "default")
        outside, inside = results[: len(_OUTSIDE_READS)], results[len(_OUTSIDE_READS) :]
        assert not [r for r in outside if _leaks(r)], outside
        assert all(INSIDE in r for r in inside), inside

    async def test_plan_mode_cannot_read_outside(self, tmp_path, boundary_on):
        calls = [_bash("cat @OUT/secret.txt"), _bash("cat in.txt")]
        results, _ = await _run(tmp_path, calls, "plan")
        assert not _leaks(results[0]), results[0]
        assert INSIDE in results[1], results[1]

    async def test_ask_user_question_still_reaches_the_callback(
        self, tmp_path, boundary_on
    ):
        # The boundary moves ordinary turns to bypassPermissions; the human
        # decision must still be routed to the gateway.
        ask = (
            "AskUserQuestion",
            {
                "questions": [
                    {
                        "question": "Which one?",
                        "header": "Pick",
                        "multiSelect": False,
                        "options": [
                            {"label": "A", "description": "a"},
                            {"label": "B", "description": "b"},
                        ],
                    }
                ]
            },
        )
        results, asked = await _run(tmp_path, [ask], "default")
        assert "AskUserQuestion" in asked
        assert "fine" in results[0], results[0]

    async def test_known_gap_a_workspace_script_reads_outside(
        self, tmp_path, boundary_on
    ):
        # Control (#241): the read boundary alone cannot see inside a script the
        # agent wrote, so the written-script read path stays open WITHOUT an OS
        # boundary. This keeps TestLandlockConfinement below non-vacuous: that
        # class runs the same script under the Landlock launcher and asserts the
        # secret no longer comes back. If this ever stops leaking on its own,
        # the CLI closed the gap — update the docs that describe it.
        results, _ = await _run(tmp_path, [_bash("python3 rd.py")], "default")
        assert SECRET in results[0], results[0]

    async def test_control_without_the_boundary_bash_reads_outside(
        self, tmp_path, boundary_off
    ):
        # Proves the tests above are not vacuous.
        results, _ = await _run(tmp_path, [_bash("cat @OUT/secret.txt")], "default")
        assert SECRET in results[0], results[0]


def _landlock_unusable() -> Optional[str]:
    from src.backends.claude import landlock

    abi = landlock.landlock_abi()
    if abi < landlock.MIN_LANDLOCK_ABI:
        return f"Landlock ABI {abi} < {landlock.MIN_LANDLOCK_ABI} (need kernel >= 6.2)"
    launcher = Path(landlock.launcher_path())
    if not (launcher.is_file() and os.access(launcher, os.X_OK)):
        return f"launcher {launcher} not executable"
    return None


@pytest.fixture
def landlock_on(_no_inherited_claude_env, monkeypatch, tmp_path):
    """Landlock mode, with the gateway's config dir at the CLI's test HOME."""
    from src.backends.claude import landlock

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv(landlock.MODE_ENV, "landlock")
    landlock.reset_mode_cache()
    yield
    landlock.reset_mode_cache()


@pytest.mark.integration
@pytest.mark.usefixtures("landlock_on")
@pytest.mark.filterwarnings("ignore::claude_agent_sdk.types.CanUseToolShadowedWarning")
@pytest.mark.skipif(bool(_landlock_unusable()), reason=_landlock_unusable() or "")
class TestLandlockConfinement:
    """The OS boundary (#241, PR-A): a Landlock ruleset inherited across execve
    confines every CLI descendant's file access, closing the written-script read
    path the command-line checks cannot — while workspace work still runs."""

    async def test_written_script_cannot_read_outside(self, tmp_path, boundary_on):
        # The exact scenario of the control known-gap test, now confined: the
        # agent writes rd.py into its workspace and runs it; the outside read
        # must come back empty/denied, not the secret.
        results, _ = await _run(
            tmp_path, [_bash("python3 rd.py")], "default", confine=True
        )
        assert not _leaks(results[0]), results[0]

    async def test_confined_direct_bash_read_outside_also_blocked(
        self, tmp_path, boundary_on
    ):
        # Beyond the written script: a plain outside read is denied at the OS
        # layer too (belt-and-suspenders with the read boundary's own refusal).
        results, _ = await _run(
            tmp_path, [_bash("cat @OUT/secret.txt")], "default", confine=True
        )
        assert not _leaks(results[0]), results[0]

    async def test_confined_workspace_work_still_runs(self, tmp_path, boundary_on):
        # Confinement must not break ordinary in-workspace work: reading a file,
        # running a script that reads an in-workspace file, and writing.
        calls = [
            _bash("cat in.txt"),
            _bash("python3 ok.py"),
            _bash("echo written > out.txt && cat out.txt"),
        ]
        results, _ = await _run(tmp_path, calls, "default", confine=True)
        assert all(INSIDE in r for r in results[:2]), results
        assert "written" in results[2], results[2]

    async def test_confined_bash_is_not_a_login_shell(
        self, tmp_path, boundary_off, monkeypatch
    ):
        # #244 review 4: confined, the CLI cannot write its shell snapshot and
        # ran every Bash call as `bash -c -l`, so a ~/.profile (which the
        # ruleset denies) put "Permission denied" into every result and a PATH
        # reset in /etc/profile dropped operator PATH entries. The launcher
        # points CLAUDE_CODE_SHELL at docker/claude-session-bash, which drops
        # the -l. The other tests' HOME has no .profile, so they never saw it.
        # (boundary_off: the read boundary refuses `echo $PATH` as unverifiable.)
        bash = next(
            (b for b in ("/bin/bash", "/usr/bin/bash") if os.access(b, os.X_OK)), None
        )
        if bash is None:
            pytest.skip("no system bash")
        # SHELL names a bash, so it is the CLI's pick as in the image (no zsh);
        # a zsh the CLI would pick is left alone.
        monkeypatch.setenv("SHELL", bash)
        home = tmp_path / "home"
        home.mkdir()
        (home / ".profile").write_text("PATH=/profile/reset\n")
        marker = str(tmp_path / "operator-bin")
        monkeypatch.setenv("PATH", f"{marker}{os.pathsep}{os.environ['PATH']}")
        probe = "shopt -q login_shell && echo LOGIN || echo NOLOGIN; echo P=$PATH"
        results, _ = await _run(tmp_path, [_bash(probe)], "default", confine=True)
        assert "NOLOGIN" in results[0], results[0]
        assert "Permission denied" not in results[0], results[0]
        assert f"P={marker}{os.pathsep}" in results[0], results[0]
