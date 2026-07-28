"""Tests for the container-startup marketplace plugin installer.

Unit tests exercise the manifest parsing / claude discovery helpers directly;
the integration tests run ``docker/install_plugins.py`` as a subprocess with
fake ``git`` and ``claude`` binaries on PATH, mirroring the production code
path. Installs are declared exclusively by the admin-managed manifest
(``manifest.added``); the former CLAUDE_PLUGIN_REPO* env bootstrap was removed
and must stay inert.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "install_plugins.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_plugins", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module's annotations.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = _load_installer()


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


def _write_fake_claude(bin_dir: Path) -> Path:
    """Fake claude CLI: logs args (and, when asked, its env); fails on sentinels.

    Fails for any invocation whose args contain FAILPLUGIN (a plugin spec) or
    FAILMKT (a marketplace path), so tests can exercise per-plugin and
    per-marketplace failure isolation.
    """
    fake = bin_dir / "claude"
    fake.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_CLAUDE_LOG"
if [ -n "${FAKE_CLAUDE_ENV_LOG:-}" ]; then
    env >> "$FAKE_CLAUDE_ENV_LOG"
fi
case "$*" in
    *FAILPLUGIN*) exit 1 ;;
    *FAILMKT*) exit 1 ;;
esac
exit 0
"""
    )
    fake.chmod(0o755)
    return fake


def _write_fake_git(bin_dir: Path) -> Path:
    """Fake git: logs args; on clone exercises GIT_ASKPASS and creates the dest."""
    fake = bin_dir / "git"
    fake.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
if [ "${1:-}" = "clone" ]; then
    # repo and dest are always the final two positional args, regardless of
    # which options (--depth, --branch, ...) precede them.
    repo=""
    dest=""
    for a in "$@"; do
        repo="$dest"
        dest="$a"
    done
    if [ -n "${GIT_ASKPASS:-}" ]; then
        "$GIT_ASKPASS" "Username for 'https://example'" >> "$FAKE_ASKPASS_LOG"
        "$GIT_ASKPASS" "Password for 'https://example'" >> "$FAKE_ASKPASS_LOG"
    fi
    case "$repo" in
        *FAILREPO*) exit 1 ;;
    esac
    mkdir -p "$dest/.claude-plugin"
fi
exit 0
"""
    )
    fake.chmod(0o755)
    return fake


def _write_local_marketplace(
    repo: Path, *, marketplace: str = "local", plugin: str = "demo"
) -> None:
    meta = repo / ".claude-plugin"
    meta.mkdir(parents=True)
    (meta / "marketplace.json").write_text(
        json.dumps(
            {
                "name": marketplace,
                "owner": {"name": "Test"},
                "plugins": [
                    {
                        "name": plugin,
                        "description": "d",
                        "version": "0.1.0",
                        "source": f"./{plugin}",
                    }
                ],
            }
        )
    )


def _write_manifest(path: Path, added: list, removed: list = ()) -> Path:
    """Write an admin-managed manifest file with the given records."""
    path.write_text(
        json.dumps({"version": 1, "added": added, "removed": list(removed)}),
        encoding="utf-8",
    )
    return path


def _record(repo: str, name: str, marketplace: str = "", **extra) -> dict:
    """One ``manifest.added`` record (repo/name plus optional overrides)."""
    record = {"repo": repo, "name": name}
    if marketplace:
        record["marketplace"] = marketplace
    record.update(extra)
    return record


def _base_env(tmp_path: Path, bin_dir: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude.log"),
        "FAKE_GIT_LOG": str(tmp_path / "git.log"),
        "FAKE_ASKPASS_LOG": str(tmp_path / "askpass.log"),
        # Strip any CLAUDE_PLUGIN_* the host may have set so tests are hermetic.
        **{k: "" for k in os.environ if k.startswith("CLAUDE_PLUGIN")},
    }


def _run_installer(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _clear_plugin_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Unit tests — manifest parsing & discovery
# ---------------------------------------------------------------------------


def test_default_name_strips_ref_version_and_git_suffix():
    assert (
        installer._default_name("https://github.com/owner/marketplace.git")
        == "marketplace"
    )
    assert installer._default_name("/local/path/my-mkt/") == "my-mkt"
    assert installer._default_name("https://host/owner/repo.git#main") == "repo"


def test_collect_entries_reads_manifest_added(tmp_path, monkeypatch):
    _clear_plugin_env(monkeypatch)
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            _record(
                "https://host/acme/mkt.git", "foo", "acme", scope="project"
            ),
            # Name omitted -> defaults to the repo basename.
            {"repo": "https://host/beta/mkt.git"},
        ],
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(manifest))

    entries = installer.collect_entries()

    assert [e.repo for e in entries] == [
        "https://host/acme/mkt.git",
        "https://host/beta/mkt.git",
    ]
    assert entries[0].names == ["foo"]
    assert entries[0].marketplace == "acme"
    assert entries[0].scope == "project"
    assert entries[0].source_label == "manifest"
    assert entries[1].names == ["mkt"]
    assert entries[1].scope == "user"


def test_env_bootstrap_vars_are_ignored(tmp_path, monkeypatch):
    # The CLAUDE_PLUGIN_REPO* env bootstrap (legacy + indexed) was removed in
    # favor of the admin-managed manifest; the vars must not produce entries.
    _clear_plugin_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(tmp_path / "missing.json"))
    monkeypatch.setenv("CLAUDE_PLUGIN_REPO", "https://host/owner/legacy.git")
    monkeypatch.setenv("CLAUDE_PLUGIN_REPO_1", "https://host/acme/mkt.git")
    monkeypatch.setenv("CLAUDE_PLUGIN_NAME_1", "foo")

    assert installer.collect_entries() == []


def test_manifest_private_entry_inherits_git_token(tmp_path, monkeypatch):
    # A private marketplace added via the admin panel must replay on startup
    # when CLAUDE_PLUGIN_GIT_TOKEN is provided: the manifest entry has no token
    # of its own, so it inherits that env credential for the clone.
    _clear_plugin_env(monkeypatch)
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            _record(
                "https://host/acme/private.git",
                "octo",
                "acme",
                scope="user",
                branch="main",
            )
        ],
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(manifest))
    monkeypatch.setenv("CLAUDE_PLUGIN_GIT_TOKEN", "secret-token")

    entries = installer.collect_entries()

    assert len(entries) == 1
    assert entries[0].source_label == "manifest"
    assert entries[0].repo == "https://host/acme/private.git"
    assert entries[0].git_token == "secret-token"


def test_manifest_entry_no_token_when_env_unset(tmp_path, monkeypatch):
    _clear_plugin_env(monkeypatch)
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[_record("https://host/x/pub.git", "p")],
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(manifest))
    entries = installer.collect_entries()
    assert entries[0].git_token == ""


def test_manifest_branch_defaults_to_main_and_honors_override(tmp_path, monkeypatch):
    _clear_plugin_env(monkeypatch)
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            # No branch -> defaults to main; explicit branch is honored.
            _record("https://host/acme/mkt.git", "foo"),
            _record("https://host/beta/mkt.git", "bar", branch="develop"),
        ],
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(manifest))

    entries = installer.collect_entries()

    assert entries[0].branch == installer.DEFAULT_BRANCH == "main"
    assert entries[1].branch == "develop"


def test_collect_entries_empty_when_no_manifest(tmp_path, monkeypatch):
    _clear_plugin_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(tmp_path / "missing.json"))
    assert installer.collect_entries() == []


def test_manifest_entry_with_uninferable_name_is_dropped(tmp_path, monkeypatch):
    _clear_plugin_env(monkeypatch)
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        # Repo basename is empty and no explicit name given -> entry is dropped.
        added=[{"repo": "/"}],
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(manifest))
    assert installer.collect_entries() == []


def test_resolve_claude_bin_prefers_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_CLAUDE_BIN", "/custom/claude")
    assert installer.resolve_claude_bin() == "/custom/claude"


# ---------------------------------------------------------------------------
# Integration tests — full run with fake binaries
# ---------------------------------------------------------------------------


def test_installs_multiple_plugins_from_multiple_repos(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    repo1 = "https://example/acme/mkt.git"
    repo2 = "https://example/beta/other.git"
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            _record(repo1, "foo", "acme"),
            _record(repo1, "bar", "acme"),
            _record(repo2, "baz", "beta"),
        ],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    git_log = _read(tmp_path / "git.log")
    claude_log = _read(tmp_path / "claude.log")

    # Each remote repo is cloned (into a per-repo staging dir, swapped into the
    # collision-free clone directory on success).
    repo1_dir = installer._clone_dir(
        Path(env["HOME"]) / ".claude" / "plugin-marketplaces", repo1
    )
    repo2_dir = installer._clone_dir(
        Path(env["HOME"]) / ".claude" / "plugin-marketplaces", repo2
    )
    assert f"clone --depth 1 --branch main {repo1}" in git_log
    assert f"clone --depth 1 --branch main {repo2}" in git_log
    assert repo1_dir.is_dir() and repo2_dir.is_dir()

    # One marketplace add per entry, install + update per plugin.
    assert f"plugin marketplace add {repo1_dir} --scope user" in claude_log
    assert f"plugin marketplace add {repo2_dir} --scope user" in claude_log
    assert "plugin install foo@acme --scope user" in claude_log
    assert "plugin update foo@acme --scope user" in claude_log
    assert "plugin install bar@acme --scope user" in claude_log
    assert "plugin update bar@acme --scope user" in claude_log
    assert "plugin install baz@beta --scope user" in claude_log
    assert "plugin update baz@beta --scope user" in claude_log

    # Core invariant: install runs BEFORE update for each plugin (install is
    # idempotent and won't bump an already-installed plugin; update does).
    lines = claude_log.splitlines()

    def _idx(substr):
        return next(i for i, line in enumerate(lines) if substr in line)

    assert _idx("plugin install foo@acme") < _idx("plugin update foo@acme")
    assert _idx("plugin install baz@beta") < _idx("plugin update baz@beta")


def test_branch_override_passed_to_git_clone(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    repo = "https://example/acme/mkt.git"
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[_record(repo, "foo", branch="release-1.2")],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    git_log = _read(tmp_path / "git.log")
    assert f"clone --depth 1 --branch release-1.2 {repo}" in git_log


def test_fresh_clone_removes_existing_clone(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    repo = "https://example/acme/mkt.git"
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json", added=[_record(repo, "foo")]
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    clone_dir = installer._clone_dir(
        Path(env["HOME"]) / ".claude" / "plugin-marketplaces", repo
    )
    # Seed a stale clone with a sentinel file that a fresh clone must discard.
    clone_dir.mkdir(parents=True)
    stale = clone_dir / "stale.txt"
    stale.write_text("stale")

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    assert not stale.exists(), "stale clone contents must be removed before re-clone"
    assert (
        clone_dir / ".claude-plugin"
    ).is_dir(), "fresh clone must replace the stale dir"
    assert f"clone --depth 1 --branch main {repo}" in _read(tmp_path / "git.log")


def test_private_repo_token_routed_through_askpass_not_claude(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    repo = "https://example/acme/private.git"
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json", added=[_record(repo, "foo")]
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
        "CLAUDE_PLUGIN_GIT_TOKEN": "secret-token",
        "FAKE_CLAUDE_ENV_LOG": str(tmp_path / "claude_env.log"),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    askpass_log = _read(tmp_path / "askpass.log")
    claude_log = _read(tmp_path / "claude.log")
    claude_env_log = _read(tmp_path / "claude_env.log")

    # The token reaches git via the askpass helper, answering BOTH prompts...
    assert "x-access-token" in askpass_log
    assert "secret-token" in askpass_log
    assert len(askpass_log.strip().splitlines()) == 2
    # ...but never leaks to the claude CLI: not on its command line, not in any
    # registered marketplace URL, and not in its inherited environment.
    assert "secret-token" not in claude_log
    assert repo not in claude_log
    assert claude_env_log, "fake claude should have recorded its environment"
    assert "secret-token" not in claude_env_log
    assert "CLAUDE_PLUGIN_GIT_TOKEN" not in claude_env_log


def test_env_bootstrap_vars_are_ignored_end_to_end(tmp_path):
    # Legacy + indexed CLAUDE_PLUGIN_REPO* env declarations must be completely
    # inert: no clone, no marketplace add, no install.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO": "https://example/acme/mkt.git",
        "CLAUDE_PLUGIN_NAME": "MonSemi",
        "CLAUDE_PLUGIN_MARKETPLACE": "monsemi",
        "CLAUDE_PLUGIN_REPO_1": "https://example/beta/other.git",
        "CLAUDE_PLUGIN_NAME_1": "baz",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr
    assert _read(tmp_path / "claude.log") == ""
    assert _read(tmp_path / "git.log") == ""


def test_failing_plugin_does_not_block_others(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    repo1 = "https://example/acme/mkt.git"
    repo2 = "https://example/beta/other.git"
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            _record(repo1, "good-a", "acme"),
            # Middle plugin induces an install failure in the fake claude.
            _record(repo1, "FAILPLUGIN", "acme"),
            _record(repo1, "good-b", "acme"),
            _record(repo2, "good-c", "beta"),
        ],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    # A failing plugin never blocks the run / server startup.
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    assert "plugin install good-a@acme --scope user" in claude_log
    assert "plugin install good-b@acme --scope user" in claude_log
    assert "plugin install good-c@beta --scope user" in claude_log
    # The failed install must not be followed by an update for that plugin.
    assert "plugin update FAILPLUGIN@acme" not in claude_log


def test_failing_marketplace_add_does_not_block_other_repos(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    # A local-path repo whose path contains FAILMKT makes the fake claude's
    # `marketplace add <path>` fail for that repo only.
    bad_repo = tmp_path / "FAILMKT_repo"
    _write_local_marketplace(bad_repo, marketplace="bad")

    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            _record(str(bad_repo), "demo", "bad"),
            _record("https://example/beta/other.git", "baz", "beta"),
        ],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    # The bad repo's plugins are skipped (marketplace add failed)...
    assert "plugin install demo@bad" not in claude_log
    # ...but a healthy repo still installs.
    assert "plugin install baz@beta --scope user" in claude_log


def test_failing_repo_clone_does_not_block_other_repos(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            _record("https://example/acme/FAILREPO.git", "foo"),
            _record("https://example/beta/other.git", "baz", "beta"),
        ],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    # The healthy repo still installs even though the other repo's clone failed.
    assert "plugin install baz@beta --scope user" in claude_log
    assert "plugin install foo" not in claude_log


def test_local_path_repo_is_used_without_cloning(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    repo = tmp_path / "local_mkt"
    _write_local_marketplace(repo)

    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[_record(str(repo), "demo", "local")],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    assert _read(tmp_path / "git.log") == "", "local-path repos must not be cloned"
    claude_log = _read(tmp_path / "claude.log")
    assert f"plugin marketplace add {repo} --scope user" in claude_log
    assert "plugin install demo@local --scope user" in claude_log


def test_falls_back_to_sdk_bundled_claude(tmp_path):
    # PATH has git but NOT claude, so the installer must use the bundled CLI.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_git(bin_dir)

    sdk_dir = tmp_path / "site"
    bundled = sdk_dir / "claude_agent_sdk" / "_bundled"
    bundled.mkdir(parents=True)
    (sdk_dir / "claude_agent_sdk" / "__init__.py").write_text("")
    fake_claude = bundled / "claude"
    fake_claude.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_CLAUDE_LOG"
exit 0
"""
    )
    fake_claude.chmod(0o755)

    repo = tmp_path / "local_mkt"
    _write_local_marketplace(repo)

    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[_record(str(repo), "demo", "local")],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "PYTHONPATH": str(sdk_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    assert "plugin install demo@local --scope user" in claude_log


def test_no_configuration_is_a_noop(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    env = _base_env(tmp_path, bin_dir)  # no manifest configured

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr
    assert _read(tmp_path / "claude.log") == ""
    assert _read(tmp_path / "git.log") == ""


def test_missing_claude_cli_skips_without_blocking_startup(tmp_path):
    # An unusable claude binary must be skipped, never block the gateway startup.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_git(bin_dir)

    repo = tmp_path / "local_mkt"
    _write_local_marketplace(repo)

    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[_record(str(repo), "demo")],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_CLAUDE_BIN": str(tmp_path / "nope" / "claude"),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr
    assert "claude CLI not found" in result.stderr
    assert _read(tmp_path / "claude.log") == ""


# ---------------------------------------------------------------------------
# Integration tests — manifest reconciliation (removed specs)
# ---------------------------------------------------------------------------


def test_manifest_removed_spec_is_skipped_and_uninstalled(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    # The manifest still lists a plugin the admin has removed (e.g. a stale
    # added record or a hand-edited file), plus a second one that must remain
    # installed.
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            _record("https://example/env/mkt.git", "oldplug", "envmkt"),
            _record("https://example/env/mkt.git", "keepplug", "envmkt"),
        ],
        removed=["oldplug@envmkt"],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    # The removed spec is never (re)installed...
    assert "plugin install oldplug@envmkt" not in claude_log
    assert "plugin update oldplug@envmkt" not in claude_log
    # ...but it IS actively uninstalled.
    assert "plugin uninstall oldplug@envmkt --scope user" in claude_log
    # The sibling plugin from the same repo is unaffected.
    assert "plugin install keepplug@envmkt --scope user" in claude_log


def test_manifest_removed_honors_scope(tmp_path):
    # A project-scope plugin removed via the admin panel must be uninstalled
    # at --scope project on startup (a --scope user uninstall would fail and
    # leave it installed), and skipped from reinstall only at that scope.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[
            _record(
                "https://example/env/mkt.git", "oldplug", "envmkt", scope="project"
            )
        ],
        removed=[{"spec": "oldplug@envmkt", "scope": "project"}],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    assert "plugin uninstall oldplug@envmkt --scope project" in claude_log
    assert "plugin install oldplug@envmkt" not in claude_log


def test_manifest_removed_uninstall_failure_does_not_block_startup(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    # FAILPLUGIN makes the fake claude exit 1 on the uninstall call.
    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[_record("https://example/env/mkt.git", "keepplug", "envmkt")],
        removed=["FAILPLUGIN@x"],
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    # A failing uninstall must never block the run / server startup.
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    assert "plugin uninstall FAILPLUGIN@x --scope user" in claude_log
    assert "plugin install keepplug@envmkt --scope user" in claude_log


def test_no_manifest_file_is_a_noop(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    env = {
        **_base_env(tmp_path, bin_dir),
        # Point at a path that does not exist; the installer must tolerate it.
        "CLAUDE_PLUGIN_MANIFEST": str(tmp_path / "missing.json"),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr
    assert _read(tmp_path / "claude.log") == ""
    assert _read(tmp_path / "git.log") == ""


def test_corrupt_manifest_is_tolerated(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    manifest = tmp_path / "gateway-plugins.json"
    manifest.write_text("{not valid json")

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr
    # A corrupt manifest reads as empty: nothing installed, nothing removed.
    assert _read(tmp_path / "claude.log") == ""
    assert _read(tmp_path / "git.log") == ""


def test_marketplace_install_registers_cli_usable_plugin_from_local_directory(tmp_path):
    """End-to-end against the real Claude CLI when it is available."""
    if shutil.which("claude") is None:
        pytest.skip("Claude CLI is not installed")

    repo = tmp_path / "repo"
    meta = repo / ".claude-plugin"
    plugin_meta = repo / "plugins" / "demo" / ".claude-plugin"
    skill = repo / "plugins" / "demo" / "skills" / "demo"
    meta.mkdir(parents=True)
    plugin_meta.mkdir(parents=True)
    skill.mkdir(parents=True)
    (meta / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "external",
                "owner": {"name": "Test"},
                "plugins": [
                    {
                        "name": "demo",
                        "description": "demo plugin",
                        "version": "0.1.0",
                        "source": "./plugins/demo",
                    }
                ],
            }
        )
    )
    (plugin_meta / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "0.1.0", "description": "demo plugin"})
    )
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\n\n# demo\n"
    )

    manifest = _write_manifest(
        tmp_path / "gateway-plugins.json",
        added=[_record(str(repo), "demo", "external")],
    )

    home = tmp_path / "home"
    env = {
        **os.environ,
        # Strip any host CLAUDE_PLUGIN_* so a developer's exported config cannot
        # bleed an extra real clone/install into this test.
        **{k: "" for k in os.environ if k.startswith("CLAUDE_PLUGIN")},
        "HOME": str(home),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    listed = subprocess.run(
        ["claude", "plugin", "list", "--json"],
        env={**os.environ, "HOME": str(home)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    plugins = json.loads(listed.stdout)
    plugin = next(p for p in plugins if p["id"] == "demo@external")
    assert plugin["enabled"] is True
    assert "errors" not in plugin
