"""Tests for the container-startup marketplace plugin installer.

Unit tests exercise the env parsing / claude discovery helpers directly; the
integration tests run ``docker/install_plugins.py`` as a subprocess with fake
``git`` and ``claude`` binaries on PATH, mirroring the production code path.
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


# ---------------------------------------------------------------------------
# Unit tests — env parsing & discovery
# ---------------------------------------------------------------------------


def test_default_name_strips_ref_version_and_git_suffix():
    assert (
        installer._default_name("https://github.com/owner/marketplace.git")
        == "marketplace"
    )
    assert installer._default_name("/local/path/my-mkt/") == "my-mkt"
    assert installer._default_name("https://host/owner/repo.git#main") == "repo"


def test_collect_entries_parses_legacy_and_indexed(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_REPO", "https://host/owner/legacy.git")
    monkeypatch.setenv("CLAUDE_PLUGIN_REPO_1", "https://host/acme/mkt.git")
    monkeypatch.setenv("CLAUDE_PLUGIN_NAME_1", "foo, bar")
    monkeypatch.setenv("CLAUDE_PLUGIN_MARKETPLACE_1", "acme")
    monkeypatch.setenv("CLAUDE_PLUGIN_SCOPE_1", "project")
    # Gap at _2; _3 is still picked up.
    monkeypatch.setenv("CLAUDE_PLUGIN_REPO_3", "https://host/beta/mkt.git")

    entries = installer.collect_entries()

    assert [e.repo for e in entries] == [
        "https://host/owner/legacy.git",
        "https://host/acme/mkt.git",
        "https://host/beta/mkt.git",
    ]
    # Legacy entry defaults its name from the basename.
    assert entries[0].names == ["legacy"]
    assert entries[0].scope == "user"
    # Indexed entry splits comma-separated names and trims whitespace.
    assert entries[1].names == ["foo", "bar"]
    assert entries[1].marketplace == "acme"
    assert entries[1].scope == "project"
    # Repo without explicit name defaults to the basename.
    assert entries[2].names == ["mkt"]


def test_manifest_private_entry_inherits_git_token(tmp_path, monkeypatch):
    # A private marketplace added via the admin panel must replay on startup when
    # the un-indexed CLAUDE_PLUGIN_GIT_TOKEN is provided: the manifest entry has
    # no token of its own, so it inherits that env credential for the clone.
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)
    manifest = tmp_path / "gateway-plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "added": [
                    {
                        "repo": "https://host/acme/private.git",
                        "name": "octo",
                        "marketplace": "acme",
                        "scope": "user",
                        "branch": "main",
                    }
                ],
                "removed": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(manifest))
    monkeypatch.setenv("CLAUDE_PLUGIN_GIT_TOKEN", "secret-token")

    entries = installer.collect_entries()

    assert len(entries) == 1
    assert entries[0].source_label == "manifest"
    assert entries[0].repo == "https://host/acme/private.git"
    assert entries[0].git_token == "secret-token"


def test_manifest_entry_no_token_when_env_unset(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)
    manifest = tmp_path / "gateway-plugins.json"
    manifest.write_text(
        json.dumps(
            {"added": [{"repo": "https://host/x/pub.git", "name": "p"}], "removed": []}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(manifest))
    entries = installer.collect_entries()
    assert entries[0].git_token == ""


def test_collect_entries_branch_defaults_to_main_and_honors_override(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)
    # _1 omits the branch -> defaults to main; _2 pins an explicit branch.
    monkeypatch.setenv("CLAUDE_PLUGIN_REPO_1", "https://host/acme/mkt.git")
    monkeypatch.setenv("CLAUDE_PLUGIN_REPO_2", "https://host/beta/mkt.git")
    monkeypatch.setenv("CLAUDE_PLUGIN_BRANCH_2", "develop")

    entries = installer.collect_entries()

    assert entries[0].branch == installer.DEFAULT_BRANCH == "main"
    assert entries[1].branch == "develop"


def test_collect_entries_empty_when_unset(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)
    assert installer.collect_entries() == []


def test_env_journal_path_defaults_beside_manifest(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ENV_JOURNAL", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", "/data/gw.json")
    assert installer.env_journal_path() == Path("/data/gateway-plugins-env.json")


def test_env_journal_path_honors_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ENV_JOURNAL", "/x/custom.json")
    assert installer.env_journal_path() == Path("/x/custom.json")


def test_write_env_journal_records_real_name_scope_branch_repo(tmp_path, monkeypatch):
    # The journal key is the marketplace.json `name`, NOT the env declaration's
    # repo basename, so the app can match it against known_marketplaces.json.
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)
    journal = tmp_path / "env.json"
    monkeypatch.setenv("CLAUDE_PLUGIN_ENV_JOURNAL", str(journal))

    clone = tmp_path / "clone"
    (clone / ".claude-plugin").mkdir(parents=True)
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "real-mkt-name", "plugins": []})
    )
    entry = installer.PluginEntry(
        repo=str(clone),
        names=["p"],
        marketplace="",
        scope="project",
        branch="develop",
        source_label="CLAUDE_PLUGIN_REPO_1",
    )

    installer.write_env_journal([entry], tmp_path)

    data = json.loads(journal.read_text())
    assert data["marketplaces"] == {
        "real-mkt-name": {
            "scope": "project",
            "branch": "develop",
            "repo": str(clone),
        }
    }


def test_strip_url_credentials_installer():
    f = installer._strip_url_credentials
    assert f("https://user:tok@host/o/r.git") == "https://host/o/r.git"
    assert f("https://tok@host/o/r.git") == "https://host/o/r.git"
    assert f("ssh://git@host/o/r.git") == "ssh://git@host/o/r.git"
    assert f("/clones/local") == "/clones/local"


def test_write_env_journal_strips_credentials_from_repo(tmp_path, monkeypatch):
    # A token in CLAUDE_PLUGIN_REPO* is used to clone but must NEVER land in the
    # journal (which the app replays into the manifest/API).
    journal = tmp_path / "env.json"
    monkeypatch.setenv("CLAUDE_PLUGIN_ENV_JOURNAL", str(journal))
    entry = installer.PluginEntry(
        repo="https://user:secret-token@host/org/private.git",
        names=["p"],
        marketplace="priv",
        scope="user",
        branch="main",
        source_label="CLAUDE_PLUGIN_REPO_1",
    )
    installer.write_env_journal([entry], tmp_path)
    raw = journal.read_text()
    assert "secret-token" not in raw
    assert json.loads(raw)["marketplaces"]["priv"]["repo"] == (
        "https://host/org/private.git"
    )


def test_write_env_journal_skips_manifest_entries_and_clears(tmp_path, monkeypatch):
    journal = tmp_path / "env.json"
    monkeypatch.setenv("CLAUDE_PLUGIN_ENV_JOURNAL", str(journal))
    manifest_entry = installer.PluginEntry(
        repo="https://host/x/y.git", names=["p"], source_label="manifest"
    )
    installer.write_env_journal([manifest_entry], tmp_path)
    assert json.loads(journal.read_text())["marketplaces"] == {}


def test_write_env_journal_name_fallback_to_explicit_then_basename(
    tmp_path, monkeypatch
):
    journal = tmp_path / "env.json"
    monkeypatch.setenv("CLAUDE_PLUGIN_ENV_JOURNAL", str(journal))
    # No marketplace.json on disk -> explicit marketplace name wins.
    e1 = installer.PluginEntry(
        repo=str(tmp_path / "missing1"),
        names=["p"],
        marketplace="explicit-mkt",
        source_label="CLAUDE_PLUGIN_REPO_1",
    )
    # No marketplace.json and no explicit name -> repo basename.
    e2 = installer.PluginEntry(
        repo="https://host/acme/cool-mkt.git",
        names=["q"],
        source_label="CLAUDE_PLUGIN_REPO_2",
    )
    installer.write_env_journal([e1, e2], tmp_path)
    mkts = json.loads(journal.read_text())["marketplaces"]
    assert "explicit-mkt" in mkts
    assert "cool-mkt" in mkts


def test_collect_entries_drops_entry_with_uninferable_name(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)
    # Repo basename is empty and no explicit name given -> entry is dropped.
    monkeypatch.setenv("CLAUDE_PLUGIN_REPO_1", "/")
    assert installer.collect_entries() == []


def test_collect_entries_honors_max_index_boundary(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_PLUGIN"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        f"CLAUDE_PLUGIN_REPO_{installer.MAX_INDEX}", "https://host/o/at-cap.git"
    )
    monkeypatch.setenv(
        f"CLAUDE_PLUGIN_REPO_{installer.MAX_INDEX + 1}", "https://host/o/over.git"
    )
    repos = [e.repo for e in installer.collect_entries()]
    assert "https://host/o/at-cap.git" in repos
    assert "https://host/o/over.git" not in repos


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

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO_1": "https://example/acme/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "foo,bar",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "acme",
        "CLAUDE_PLUGIN_REPO_2": "https://example/beta/other.git",
        "CLAUDE_PLUGIN_NAME_2": "baz",
        "CLAUDE_PLUGIN_MARKETPLACE_2": "beta",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    git_log = _read(tmp_path / "git.log")
    claude_log = _read(tmp_path / "claude.log")

    # Each remote repo is cloned (into a per-repo staging dir, swapped into the
    # collision-free clone directory on success).
    repo1_dir = installer._clone_dir(
        Path(env["HOME"]) / ".claude" / "plugin-marketplaces",
        env["CLAUDE_PLUGIN_REPO_1"],
    )
    repo2_dir = installer._clone_dir(
        Path(env["HOME"]) / ".claude" / "plugin-marketplaces",
        env["CLAUDE_PLUGIN_REPO_2"],
    )
    assert f"clone --depth 1 --branch main {env['CLAUDE_PLUGIN_REPO_1']}" in git_log
    assert f"clone --depth 1 --branch main {env['CLAUDE_PLUGIN_REPO_2']}" in git_log
    assert repo1_dir.is_dir() and repo2_dir.is_dir()

    # One marketplace add per repo, install + update per plugin.
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

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO_1": "https://example/acme/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "foo",
        "CLAUDE_PLUGIN_BRANCH_1": "release-1.2",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    git_log = _read(tmp_path / "git.log")
    assert (
        f"clone --depth 1 --branch release-1.2 {env['CLAUDE_PLUGIN_REPO_1']}" in git_log
    )


def test_fresh_clone_removes_existing_clone(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO_1": "https://example/acme/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "foo",
    }

    clone_dir = installer._clone_dir(
        Path(env["HOME"]) / ".claude" / "plugin-marketplaces",
        env["CLAUDE_PLUGIN_REPO_1"],
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
    assert f"clone --depth 1 --branch main {env['CLAUDE_PLUGIN_REPO_1']}" in _read(
        tmp_path / "git.log"
    )


def test_private_repo_token_routed_through_askpass_not_claude(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO_1": "https://example/acme/private.git",
        "CLAUDE_PLUGIN_NAME_1": "foo",
        "CLAUDE_PLUGIN_GIT_TOKEN_1": "secret-token",
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
    assert "https://example/acme/private.git" not in claude_log
    assert claude_env_log, "fake claude should have recorded its environment"
    assert "secret-token" not in claude_env_log
    assert "CLAUDE_PLUGIN_GIT_TOKEN" not in claude_env_log


def test_legacy_single_repo_still_supported(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO": "https://example/acme/mkt.git",
        "CLAUDE_PLUGIN_NAME": "MonSemi",
        "CLAUDE_PLUGIN_MARKETPLACE": "monsemi",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    assert "plugin install MonSemi@monsemi --scope user" in claude_log
    assert "plugin update MonSemi@monsemi --scope user" in claude_log


def test_failing_plugin_does_not_block_others(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO_1": "https://example/acme/mkt.git",
        # Middle plugin induces an install failure in the fake claude.
        "CLAUDE_PLUGIN_NAME_1": "good-a,FAILPLUGIN,good-b",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "acme",
        "CLAUDE_PLUGIN_REPO_2": "https://example/beta/other.git",
        "CLAUDE_PLUGIN_NAME_2": "good-c",
        "CLAUDE_PLUGIN_MARKETPLACE_2": "beta",
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

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO_1": str(bad_repo),
        "CLAUDE_PLUGIN_NAME_1": "demo",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "bad",
        "CLAUDE_PLUGIN_REPO_2": "https://example/beta/other.git",
        "CLAUDE_PLUGIN_NAME_2": "baz",
        "CLAUDE_PLUGIN_MARKETPLACE_2": "beta",
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

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO_1": "https://example/acme/FAILREPO.git",
        "CLAUDE_PLUGIN_NAME_1": "foo",
        "CLAUDE_PLUGIN_REPO_2": "https://example/beta/other.git",
        "CLAUDE_PLUGIN_NAME_2": "baz",
        "CLAUDE_PLUGIN_MARKETPLACE_2": "beta",
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

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_REPO_1": str(repo),
        "CLAUDE_PLUGIN_NAME_1": "demo",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "local",
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

    env = {
        **_base_env(tmp_path, bin_dir),
        "PYTHONPATH": str(sdk_dir),
        "CLAUDE_PLUGIN_REPO_1": str(repo),
        "CLAUDE_PLUGIN_NAME_1": "demo",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "local",
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

    env = _base_env(tmp_path, bin_dir)  # no CLAUDE_PLUGIN_REPO* set

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

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_CLAUDE_BIN": str(tmp_path / "nope" / "claude"),
        "CLAUDE_PLUGIN_REPO_1": str(repo),
        "CLAUDE_PLUGIN_NAME_1": "demo",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr
    assert "claude CLI not found" in result.stderr
    assert _read(tmp_path / "claude.log") == ""


# ---------------------------------------------------------------------------
# Integration tests — admin-managed manifest reconciliation
# ---------------------------------------------------------------------------


def test_manifest_added_entries_are_installed(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    manifest = tmp_path / "gateway-plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "added": [
                    {
                        "repo": "https://example/admin/mkt.git",
                        "name": "adminplug",
                        "marketplace": "adminmkt",
                        "scope": "user",
                        "branch": "main",
                    }
                ],
                "removed": [],
            }
        )
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
        # An env-bootstrap entry alongside the manifest entry.
        "CLAUDE_PLUGIN_REPO_1": "https://example/env/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "envplug",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "envmkt",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    # Env bootstrap entry still installed.
    assert "plugin install envplug@envmkt --scope user" in claude_log
    # Manifest-added entry installed (and updated) too.
    assert "plugin install adminplug@adminmkt --scope user" in claude_log
    assert "plugin update adminplug@adminmkt --scope user" in claude_log

    git_log = _read(tmp_path / "git.log")
    assert "clone --depth 1 --branch main https://example/admin/mkt.git" in git_log


def test_manifest_removed_spec_is_skipped_and_uninstalled(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    manifest = tmp_path / "gateway-plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "added": [],
                "removed": ["envplug@envmkt"],
            }
        )
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
        # The env bootstrap still names a plugin the admin has removed, plus a
        # second one that must remain installed.
        "CLAUDE_PLUGIN_REPO_1": "https://example/env/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "envplug,keepplug",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "envmkt",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    # The removed spec is never (re)installed...
    assert "plugin install envplug@envmkt" not in claude_log
    assert "plugin update envplug@envmkt" not in claude_log
    # ...but it IS actively uninstalled.
    assert "plugin uninstall envplug@envmkt --scope user" in claude_log
    # The sibling plugin from the same repo is unaffected.
    assert "plugin install keepplug@envmkt --scope user" in claude_log


def test_manifest_removed_honors_scope(tmp_path):
    # A project-scope env plugin removed via the admin panel must be uninstalled
    # at --scope project on startup (a --scope user uninstall would fail and
    # leave it installed), and skipped from reinstall only at that scope.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    manifest = tmp_path / "gateway-plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "added": [],
                "removed": [{"spec": "envplug@envmkt", "scope": "project"}],
            }
        )
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
        "CLAUDE_PLUGIN_REPO_1": "https://example/env/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "envplug",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "envmkt",
        "CLAUDE_PLUGIN_SCOPE_1": "project",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    assert "plugin uninstall envplug@envmkt --scope project" in claude_log
    assert "plugin install envplug@envmkt" not in claude_log


def test_manifest_removed_uninstall_failure_does_not_block_startup(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    manifest = tmp_path / "gateway-plugins.json"
    # FAILPLUGIN makes the fake claude exit 1 on the uninstall call.
    manifest.write_text(
        json.dumps({"version": 1, "added": [], "removed": ["FAILPLUGIN@x"]})
    )

    env = {
        **_base_env(tmp_path, bin_dir),
        "CLAUDE_PLUGIN_MANIFEST": str(manifest),
        "CLAUDE_PLUGIN_REPO_1": "https://example/env/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "keepplug",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "envmkt",
    }

    result = _run_installer(env)
    # A failing uninstall must never block the run / server startup.
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    assert "plugin uninstall FAILPLUGIN@x --scope user" in claude_log
    assert "plugin install keepplug@envmkt --scope user" in claude_log


def test_no_manifest_file_behaves_as_before(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir)
    _write_fake_git(bin_dir)

    env = {
        **_base_env(tmp_path, bin_dir),
        # Point at a path that does not exist; the installer must tolerate it.
        "CLAUDE_PLUGIN_MANIFEST": str(tmp_path / "missing.json"),
        "CLAUDE_PLUGIN_REPO_1": "https://example/env/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "envplug",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "envmkt",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    # Behaves exactly as before: env entry installed, no uninstall calls.
    assert "plugin install envplug@envmkt --scope user" in claude_log
    assert "plugin uninstall" not in claude_log


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
        "CLAUDE_PLUGIN_REPO_1": "https://example/env/mkt.git",
        "CLAUDE_PLUGIN_NAME_1": "envplug",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "envmkt",
    }

    result = _run_installer(env)
    assert result.returncode == 0, result.stderr

    claude_log = _read(tmp_path / "claude.log")
    assert "plugin install envplug@envmkt --scope user" in claude_log
    assert "plugin uninstall" not in claude_log


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

    home = tmp_path / "home"
    env = {
        **os.environ,
        # Strip any host CLAUDE_PLUGIN_* so a developer's exported legacy config
        # cannot bleed an extra real clone/install into this test.
        **{k: "" for k in os.environ if k.startswith("CLAUDE_PLUGIN")},
        "HOME": str(home),
        "CLAUDE_PLUGIN_REPO_1": str(repo),
        "CLAUDE_PLUGIN_NAME_1": "demo",
        "CLAUDE_PLUGIN_MARKETPLACE_1": "external",
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
