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


def _make_plugin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    skill = repo / "skills" / "demo"
    meta = repo / ".claude-plugin"
    skill.mkdir(parents=True)
    meta.mkdir()
    (meta / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "main",
                "description": "Demo plugin",
                "skills": "./skills/",
            }
        )
    )
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: Demo skill\n---\n\n# Demo\n")
    _run(["git", "init", "-q", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "review@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "review"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "init"], cwd=repo)
    return repo


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
