"""Install a throwaway plugin into an isolated CLI config dir (bundled CLI).

The bundled CLI's own ``plugin marketplace add`` / ``plugin install`` write the
registry exactly as an admin install does, so a test sees the real
``installed_plugins.json`` and the CLI's own ``${CLAUDE_PLUGIN_DATA}`` naming.

The CLI always runs with an explicit ``env``, and HOME / CLAUDE_CONFIG_DIR must
point inside *root*: with an inherited or missing HOME the CLI falls back to the
passwd home and installs into the developer's real ``~/.claude``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional


def install_plugin(
    cli: str,
    env: Dict[str, str],
    root: Path,
    *,
    name: str = "hookplug",
    marketplace: str = "testmkt",
    hooks: Optional[Dict[str, Any]] = None,
    skill: Optional[str] = None,
) -> str:
    """Write a local marketplace with one plugin under *root*, install it.

    *hooks* becomes the plugin's ``hooks/hooks.json`` ``hooks`` object; *skill*
    adds ``skills/<skill>/SKILL.md``. Returns the plugin id.
    """
    root = Path(root).resolve()
    for var in ("HOME", "CLAUDE_CONFIG_DIR"):
        target = Path(env[var]).resolve()
        assert target == root or root in target.parents, (var, target, root)

    mkt = root / "marketplace"
    (mkt / ".claude-plugin").mkdir(parents=True)
    (mkt / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": marketplace,
                "owner": {"name": "test"},
                "plugins": [{"name": name, "source": f"./{name}"}],
            }
        )
    )
    plugin = mkt / name
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "0.0.1", "description": "test"})
    )
    if hooks is not None:
        (plugin / "hooks").mkdir()
        (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    if skill is not None:
        (plugin / "skills" / skill).mkdir(parents=True)
        (plugin / "skills" / skill / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: test skill\n---\nbody\n"
        )

    plugin_id = f"{name}@{marketplace}"
    child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    for args in (
        ["plugin", "marketplace", "add", str(mkt)],
        ["plugin", "install", plugin_id],
    ):
        proc = subprocess.run(
            [cli, *args],
            env=child_env,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    registry = Path(env["CLAUDE_CONFIG_DIR"]) / "plugins" / "installed_plugins.json"
    assert plugin_id in json.loads(registry.read_text())["plugins"]
    return plugin_id
