"""Per-user isolation + capability-policy tests for the Codex adapter (#173 §6-7).

The full runtime filesystem adversarial matrix (§8: two users with marker
secrets, path/traversal/symlink/shell read attempts) runs against the real
production container/runtime; it cannot run in unit CI without a codex binary.
These tests instead prove the *enforceable in-code* boundary:

* sibling-backend secrets are stripped from the child process (proved at the
  real process boundary via a probe subprocess),
* each user gets a distinct CODEX_HOME and a per-user CODEX_HOME always wins,
* a capability deny that Codex cannot enforce refuses the session (fail closed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.backends.appserver import client as adapter_client
from src.backends.appserver.client import AppServerCodexClient
from src.backends.appserver.isolation import (
    ISOLATION_ENV_REMOVE,
    build_isolated_env,
    resolve_codex_home,
)
from src.backends.appserver.policy import CapabilityError, resolve_runtime_policy
from src.backends.appserver.transport import AppServerTransport

PROBE = Path(__file__).parent / "fixtures" / "env_probe_app_server.py"


# -- isolation env construction ---------------------------------------------


def test_per_user_runtime_home_is_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME_BASE", str(tmp_path))
    home_a = resolve_codex_home("alice", "s1")
    home_b = resolve_codex_home("bob", "s2")
    assert home_a != home_b
    assert home_a.is_dir() and home_b.is_dir()


def test_anonymous_sessions_get_distinct_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME_BASE", str(tmp_path))
    assert resolve_codex_home(None, "s1") != resolve_codex_home(None, "s2")


def test_build_isolated_env_forces_per_user_runtime_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME_BASE", str(tmp_path))
    env = build_isolated_env(
        auth_env={"CODEX_BIN": "codex"},
        # A caller-supplied CODEX_HOME must NOT override the per-user one.
        extra_env={"CODEX_HOME": "/tmp/shared-evil", "CODEX_MODELS": "gpt-5.5"},
        user="alice",
        session_id="s1",
        metadata_allowlist=frozenset({"CODEX_MODELS", "CODEX_HOME"}),
    )
    assert env["CODEX_HOME"] == str(resolve_codex_home("alice", "s1"))
    assert env["CODEX_HOME"] != "/tmp/shared-evil"
    assert env["CODEX_MODELS"] == "gpt-5.5"


# -- capability policy (fail closed) ----------------------------------------


def test_default_policy_uses_configured_sandbox():
    policy = resolve_runtime_policy(
        default_sandbox="workspace-write", default_approval="never"
    )
    assert policy == {"sandbox": "workspace-write", "approvalPolicy": "never"}


def test_denying_filesystem_write_drops_to_read_only():
    policy = resolve_runtime_policy(
        default_sandbox="workspace-write",
        default_approval="never",
        disallowed_tools=["Write"],
    )
    assert policy["sandbox"] == "read-only"


def test_denying_network_drops_to_read_only():
    policy = resolve_runtime_policy(
        default_sandbox="workspace-write",
        default_approval="never",
        disallowed_tools=["web_search"],
    )
    assert policy["sandbox"] == "read-only"


def test_denying_shell_is_fail_closed():
    with pytest.raises(CapabilityError):
        resolve_runtime_policy(
            default_sandbox="workspace-write",
            default_approval="never",
            disallowed_tools=["Bash"],
        )


# -- real process boundary: secrets stripped, per-user CODEX_HOME injected ---


async def _probe_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("CODEX_HOME_BASE", str(tmp_path / "homes"))
    # A sibling-backend secret and a marker are present in the gateway env.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "super-secret")
    monkeypatch.setenv("OMG_ISOLATION_MARKER", "leak-me")

    env = build_isolated_env(
        auth_env={},
        extra_env=None,
        user="alice",
        session_id="s1",
        metadata_allowlist=frozenset(),
    )
    transport = AppServerTransport(
        [sys.executable, str(PROBE)],
        env=env,
        env_remove=ISOLATION_ENV_REMOVE | {"OMG_ISOLATION_MARKER"},
    )
    await transport.start(initialize=False)
    try:
        result = await transport.request("initialize", {}, timeout=10.0)
    finally:
        await transport.close()
    return result


async def test_child_env_strips_secrets_and_injects_runtime_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    result = await _probe_env(tmp_path, monkeypatch)
    # The sibling-backend token and the marker were stripped from the child.
    assert result["has_anthropic_token"] is False
    assert result["marker_present"] is False
    # The per-user CODEX_HOME reached the child.
    assert result["codex_home"] == str(resolve_codex_home("alice", "s1"))


# -- two users never share Codex state / workspace --------------------------


async def test_two_users_get_isolated_workspaces_and_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """create_client for two users must not share a CODEX_HOME (nor, via the
    route's per-user cwd, a workspace)."""
    from types import SimpleNamespace

    monkeypatch.setenv("CODEX_HOME_BASE", str(tmp_path / "homes"))
    scenario_steps = [
        {
            "expect_method": "initialize",
            "actions": [{"type": "response", "result": {}}],
        },
        {"expect_method": "initialized", "actions": []},
        {
            "expect_method": "thread/start",
            "actions": [{"type": "response", "result": {"thread": {"id": "t"}}}],
        },
    ]
    import json

    def _make_scenario(name: str) -> Path:
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps({"steps": scenario_steps, "linger": True}), encoding="utf-8"
        )
        return path

    captured_envs = {}
    real_transport = adapter_client.AppServerTransport

    def _spy(argv, **kwargs):
        captured_envs.setdefault("list", []).append(kwargs.get("env") or {})
        return real_transport(argv, **kwargs)

    wsa = tmp_path / "wsA"
    wsb = tmp_path / "wsB"
    wsa.mkdir()
    wsb.mkdir()

    backend = AppServerCodexClient()
    for name, user, cwd in [("a", "alice", wsa), ("b", "bob", wsb)]:
        scenario = _make_scenario(name)
        monkeypatch.setattr(
            adapter_client,
            "app_server_argv",
            lambda s=scenario: [
                sys.executable,
                str(PROBE.parent / "fake_app_server.py"),
                str(s),
            ],
        )
        monkeypatch.setattr(adapter_client, "AppServerTransport", _spy)
        session = SimpleNamespace(user=user, session_id=f"sess-{name}")
        handle = await backend.create_client(session=session, cwd=str(cwd))
        try:
            assert handle.cwd == str(cwd)
        finally:
            await handle.disconnect()

    home_a = captured_envs["list"][0]["CODEX_HOME"]
    home_b = captured_envs["list"][1]["CODEX_HOME"]
    assert home_a != home_b
