import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "install_plugins.sh"


def _run(cmd, *, cwd=None, env=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _write_plugin_files(plugin_dir: Path, *, name: str = "demo") -> None:
    skill = plugin_dir / "skills" / name
    meta = plugin_dir / ".claude-plugin"
    skill.mkdir(parents=True)
    meta.mkdir(parents=True)
    (meta / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "main",
                "description": f"{name} plugin",
                "skills": "./skills/",
            }
        )
    )
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\n"
    )


def _make_plugin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write_plugin_files(repo)
    _run(["git", "init", "-q", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "review@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "review"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "init"], cwd=repo)
    return repo


def test_plugin_installer_autodetects_marketplace_plugin_directory(tmp_path):
    repo = tmp_path / "repo"
    _write_plugin_files(repo / "plugins" / "MonSemi", name="MonSemi")
    _run(["git", "init", "-q", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "review@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "review"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "init"], cwd=repo)

    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PLUGIN_REPO": str(repo),
        "CLAUDE_PLUGIN_NAME": "MonSemi",
        "CLAUDE_PLUGIN_MARKETPLACE": "external",
        "CLAUDE_PLUGIN_VERSION": "main",
    }

    _run(["sh", str(SCRIPT)], env=env)

    registry = json.loads((home / ".claude/plugins/installed_plugins.json").read_text())
    install_path = Path(registry["plugins"]["MonSemi@external"][0]["installPath"])
    assert install_path.name == "MonSemi"
    assert install_path.parent.name == "plugins"


def test_plugin_installer_uses_configured_repo_relative_plugin_path(tmp_path):
    repo = tmp_path / "repo"
    _write_plugin_files(repo / "packages" / "MonSemi", name="MonSemi")
    _run(["git", "init", "-q", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "review@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "review"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "init"], cwd=repo)

    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PLUGIN_REPO": str(repo),
        "CLAUDE_PLUGIN_NAME": "MonSemi",
        "CLAUDE_PLUGIN_MARKETPLACE": "external",
        "CLAUDE_PLUGIN_VERSION": "main",
        "CLAUDE_PLUGIN_PATH": "packages/MonSemi",
    }

    _run(["sh", str(SCRIPT)], env=env)

    registry = json.loads((home / ".claude/plugins/installed_plugins.json").read_text())
    install_path = Path(registry["plugins"]["MonSemi@external"][0]["installPath"])
    assert install_path.name == "MonSemi"
    assert install_path.parent.name == "packages"


def test_plugin_installer_uses_askpass_for_https_git_credentials(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    log = tmp_path / "git.log"
    bin_dir.mkdir()

    fake_git = bin_dir / "git"
    fake_git.write_text(
        """#!/bin/sh
set -eu

printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"

if [ "${1:-}" = "clone" ]; then
    "$GIT_ASKPASS" "Username for 'https://github.example'" >> "$FAKE_GIT_LOG"
    "$GIT_ASKPASS" "Password for 'https://bot@github.example'" >> "$FAKE_GIT_LOG"
    target="$7"
    mkdir -p "$target/.git" "$target/.claude-plugin"
    printf '%s\\n' '{"name":"demo","version":"main","description":"Demo plugin"}' > "$target/.claude-plugin/plugin.json"
    exit 0
fi

if [ "${1:-}" = "-C" ] && [ "${3:-}" = "rev-parse" ]; then
    printf '%s\\n' "0123456789abcdef0123456789abcdef01234567"
    exit 0
fi

printf '%s\\n' "unexpected git invocation: $*" >&2
exit 1
"""
    )
    fake_git.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_GIT_LOG": str(log),
        "CLAUDE_PLUGIN_REPO": "https://github.example/acme/demo.git",
        "CLAUDE_PLUGIN_NAME": "demo",
        "CLAUDE_PLUGIN_MARKETPLACE": "external",
        "CLAUDE_PLUGIN_VERSION": "main",
        "CLAUDE_PLUGIN_GIT_USERNAME": "bot",
        "CLAUDE_PLUGIN_GIT_TOKEN": "secret-token",
    }

    _run(["sh", str(SCRIPT)], env=env)

    git_log = log.read_text()
    assert "bot\n" in git_log
    assert "secret-token\n" in git_log


def test_direct_plugin_install_registers_cli_usable_marketplace(tmp_path):
    if shutil.which("claude") is None:
        pytest.skip("Claude CLI is not installed")

    repo = _make_plugin_repo(tmp_path)
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PLUGIN_REPO": str(repo),
        "CLAUDE_PLUGIN_NAME": "demo",
        "CLAUDE_PLUGIN_MARKETPLACE": "external",
        "CLAUDE_PLUGIN_VERSION": "main",
    }

    _run(["sh", str(SCRIPT)], env=env)

    listed = _run(["claude", "plugin", "list", "--json"], env={**os.environ, "HOME": str(home)})
    plugins = json.loads(listed.stdout)
    plugin = next(p for p in plugins if p["id"] == "demo@external")

    assert plugin["enabled"] is True
    assert "errors" not in plugin
