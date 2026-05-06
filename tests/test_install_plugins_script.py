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


def _write_marketplace_files(
    repo: Path, *, marketplace: str = "external", plugin: str = "demo"
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
                        "description": f"{plugin} plugin",
                        "version": "0.1.0",
                        "source": f"./plugins/{plugin}",
                    }
                ],
            }
        )
    )
    _write_plugin_files(repo / "plugins" / plugin, name=plugin)


def _make_marketplace_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write_marketplace_files(repo)
    return repo


def test_plugin_installer_clones_remote_marketplace_then_adds_local_path_with_token(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    log = tmp_path / "claude.log"
    git_log = tmp_path / "git.log"
    bin_dir.mkdir()

    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        """#!/bin/sh
set -eu

printf '%s\\n' "$*" >> "$FAKE_CLAUDE_LOG"
"""
    )
    fake_claude.chmod(0o755)

    fake_git = bin_dir / "git"
    fake_git.write_text(
        """#!/bin/sh
set -eu

printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"

if [ "${1:-}" = "clone" ]; then
    if [ -n "${GIT_ASKPASS:-}" ]; then
    "$GIT_ASKPASS" "Username for 'https://github.example'" >> "$FAKE_CLAUDE_LOG"
        "$GIT_ASKPASS" "Password for 'https://x-access-token@github.example'" >> "$FAKE_CLAUDE_LOG"
    fi
    mkdir -p "$5/.git"
fi
"""
    )
    fake_git.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_CLAUDE_LOG": str(log),
        "FAKE_GIT_LOG": str(git_log),
        "CLAUDE_PLUGIN_REPO": "https://github.example/acme/marketplace.git",
        "CLAUDE_PLUGIN_NAME": "MonSemi",
        "CLAUDE_PLUGIN_MARKETPLACE": "monsemi",
        "CLAUDE_PLUGIN_GIT_TOKEN": "secret-token",
    }

    _run(["sh", str(SCRIPT)], env=env)

    local_repo = home / ".claude" / "plugin-marketplaces" / "marketplace"
    git_output = git_log.read_text()
    assert (
        f"clone --depth 1 https://github.example/acme/marketplace.git {local_repo}\n"
        in git_output
    )

    claude_log = log.read_text()
    assert f"plugin marketplace add {local_repo} --scope user\n" in claude_log
    assert "https://github.example/acme/marketplace.git" not in claude_log
    assert "plugin install MonSemi@monsemi --scope user\n" in claude_log
    assert "x-access-token\n" in claude_log
    assert "secret-token\n" in claude_log


def test_plugin_installer_falls_back_to_sdk_bundled_claude(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bundled_dir = tmp_path / "sdk" / "_bundled"
    log = tmp_path / "claude.log"
    repo = _make_marketplace_repo(tmp_path)
    bin_dir.mkdir()
    bundled_dir.mkdir(parents=True)

    fake_python = bin_dir / "python3"
    fake_python.write_text(
        """#!/bin/sh
printf '%s\\n' "$FAKE_BUNDLED_CLAUDE"
"""
    )
    fake_python.chmod(0o755)

    fake_claude = bundled_dir / "claude"
    fake_claude.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_CLAUDE_LOG"
"""
    )
    fake_claude.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_BUNDLED_CLAUDE": str(fake_claude),
        "FAKE_CLAUDE_LOG": str(log),
        "CLAUDE_PLUGIN_REPO": str(repo),
        "CLAUDE_PLUGIN_NAME": "demo",
        "CLAUDE_PLUGIN_MARKETPLACE": "external",
    }

    _run(["sh", str(SCRIPT)], env=env)

    claude_log = log.read_text()
    assert f"plugin marketplace add {repo} --scope user\n" in claude_log
    assert "plugin install demo@external --scope user\n" in claude_log


def test_marketplace_install_registers_cli_usable_plugin_from_local_directory(tmp_path):
    if shutil.which("claude") is None:
        pytest.skip("Claude CLI is not installed")

    repo = _make_marketplace_repo(tmp_path)
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PLUGIN_REPO": str(repo),
        "CLAUDE_PLUGIN_NAME": "demo",
        "CLAUDE_PLUGIN_MARKETPLACE": "external",
    }

    _run(["sh", str(SCRIPT)], env=env)

    listed = _run(["claude", "plugin", "list", "--json"], env={**os.environ, "HOME": str(home)})
    plugins = json.loads(listed.stdout)
    plugin = next(p for p in plugins if p["id"] == "demo@external")

    assert plugin["enabled"] is True
    assert "errors" not in plugin
