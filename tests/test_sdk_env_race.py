"""Overlapping Claude client creation must not leak isolated credentials.

The pinned SDK copies ``os.environ`` into the CLI child only AFTER awaiting its
``claude -v`` version check (``SubprocessCLITransport.connect``), then merges
``ClaudeAgentOptions.env`` over that copy. The gateway used to pop
``OPENAI_API_KEY`` (and, in api_key auth mode, ``CLAUDE_CODE_OAUTH_TOKEN``) from
the process-global environment around ``connect()`` and restore it afterwards.
Client creation is serialized only per session, so a second session that began
connecting while the first was mid-connect popped nothing, and when its version
check ended after the first session's restore, its CLI child (and the agent's
Bash inside it) inherited the keys.

Isolation now rides ``options.env`` as blanks and ``os.environ`` is never
mutated. The race test runs the SDK's real ``SubprocessCLITransport.connect()``
(only the version probe and the spawn are replaced by recorders) with two
sessions interleaved in exactly the order that leaked. That the bundled CLI
reads a blank token as unset is pinned in ``tests/test_cli_blank_oauth_token.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import pytest
from claude_agent_sdk._internal.transport import subprocess_cli
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
)

from src.session_manager import Session

AUTH_TOKEN = "sk-ant-gateway-auth-token"
OPENAI_SECRET = "sk-openai-race-sentinel"
OAUTH_SECRET = "oauth-race-sentinel"


def _make_cli(cwd: Path):
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": AUTH_TOKEN
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd=str(cwd))


@pytest.fixture
def gateway_env(monkeypatch):
    """Gateway process env: api_key auth plus credentials the CLI must not get."""
    monkeypatch.setenv("CLAUDE_AUTH_METHOD", "api_key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_SECRET)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_SECRET)
    # Keep the SDK's version check: it is the await the leak needed.
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    # No relay self-probe over the network.
    monkeypatch.setenv("MCP_PROGRESS_RELAY", "false")


class _TransportOnlyClient:
    """``ClaudeSDKClient`` stand-in that runs only the SDK's transport connect.

    The transport is where the child env is assembled; the control protocol the
    real client speaks next needs a live CLI and does not touch the env.
    """

    def __init__(self, options=None, **_kwargs):
        self.options = options

    async def connect(self, prompt=None):
        await SubprocessCLITransport(prompt="", options=self.options).connect()


class _FakeProcess:
    stdin = stdout = stderr = None
    returncode = 0


async def test_overlapping_connects_never_leak_isolated_credentials(
    gateway_env, monkeypatch, tmp_path
):
    cli = _make_cli(tmp_path)
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    before = dict(os.environ)
    timeline: List[str] = []
    environ_drift: List[str] = []
    child_env: Dict[str, Dict[str, str]] = {}
    spawned: List[_FakeProcess] = []
    a_checking = asyncio.Event()
    b_checking = asyncio.Event()
    a_connected = asyncio.Event()

    def observe(event: str) -> None:
        timeline.append(event)
        if dict(os.environ) != before:
            environ_drift.append(event)

    async def version_check(transport):
        name = Path(transport._cwd).name
        observe(f"{name}:version-check")
        if name == "a":
            a_checking.set()
            await b_checking.wait()  # B starts connecting while A is mid-connect
        else:
            b_checking.set()
            await a_connected.wait()  # B's check outlives A's whole connect
        observe(f"{name}:version-checked")

    async def open_process(cmd, **kwargs):
        name = Path(kwargs["cwd"]).name
        observe(f"{name}:spawn")
        child_env[name] = kwargs["env"]
        process = _FakeProcess()
        spawned.append(process)
        return process

    monkeypatch.setattr(SubprocessCLITransport, "_check_claude_version", version_check)
    monkeypatch.setattr(subprocess_cli.anyio, "open_process", open_process)
    monkeypatch.setattr(
        "src.backends.claude.client._get_cli_path", lambda: "/opt/fake/claude"
    )
    monkeypatch.setattr(
        "src.backends.claude.client.ClaudeSDKClient", _TransportOnlyClient
    )

    async def create(name: str) -> None:
        session = Session(session_id=str(uuid.uuid4()))
        await cli.create_client(session, cwd=str(tmp_path / name))
        observe(f"{name}:connected")

    tasks = [asyncio.create_task(create("a"))]
    try:
        await asyncio.wait_for(a_checking.wait(), 10)
        tasks.append(asyncio.create_task(create("b")))
        await asyncio.wait_for(tasks[0], 10)
        a_connected.set()
        await asyncio.wait_for(tasks[1], 10)
    finally:
        for task in tasks:
            task.cancel()
        for process in spawned:
            subprocess_cli._ACTIVE_CHILDREN.discard(process)

    # The interleaving that leaked: B spawns only after A's connect returned.
    assert timeline == [
        "a:version-check",
        "b:version-check",
        "a:version-checked",
        "a:spawn",
        "a:connected",
        "b:version-checked",
        "b:spawn",
        "b:connected",
    ]
    leaked = {
        name: sorted({OPENAI_SECRET, OAUTH_SECRET} & set(env.values()))
        for name, env in child_env.items()
    }
    assert leaked == {"a": [], "b": []}
    assert environ_drift == []
    assert dict(os.environ) == before
    for name, env in child_env.items():
        assert env["OPENAI_API_KEY"] == "", name
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "", name
        assert env["ANTHROPIC_AUTH_TOKEN"] == AUTH_TOKEN, name


@pytest.mark.parametrize(
    ("method", "blanked", "inherited"),
    [
        ("api_key", {"OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}, set()),
        # CLI auth runs on the OAuth token: only the other backend's key goes.
        ("cli", {"OPENAI_API_KEY"}, {"CLAUDE_CODE_OAUTH_TOKEN"}),
    ],
)
def test_child_env_blanks_isolation_vars_per_auth_mode(
    gateway_env, monkeypatch, tmp_path, method, blanked, inherited
):
    monkeypatch.setenv("CLAUDE_AUTH_METHOD", method)
    before = dict(os.environ)

    options = _make_cli(tmp_path)._build_sdk_options()

    assert {key: options.env.get(key) for key in blanked} == dict.fromkeys(blanked, "")
    assert not inherited & set(options.env)  # left to the inherited process env
    assert dict(os.environ) == before
