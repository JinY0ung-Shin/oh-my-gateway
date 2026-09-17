"""What skills and subagents a user's session can actually see.

``/admin/api/plugins`` answers "what plugins are installed"; it says nothing
about skills and agents that live in the user's own workspace. ChatDRAGON exposes
those project resources at backend-neutral ``skills/`` and ``agents/`` paths; the
Claude workspace compatibility layer mirrors them into Claude Code's native
``.claude/{skills,agents}`` discovery locations.

This module answers the client's real question — *for this user, right now,
which skills and subagents exist and where does each come from* — by walking the
same three scopes the CLI does:

``plugin``   installed plugins (via :mod:`src.plugin_service`)
``project``  the user's workspace ``skills/`` / ``agents/`` (with legacy
             ``.claude/{skills,agents}`` fallback)
``user``     ``~/.claude/{skills,agents}`` (only when ``setting_sources``
             includes ``user``, i.e. the scope the CLI would read)

Descriptions come from each file's YAML frontmatter, which is where skills and
subagents already declare them — so the picker can explain an entry without an
operator retyping it. Read-only and non-raising: a malformed file is skipped,
never fatal.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Frontmatter is the contract skills/subagents already use; cap the read so a
# huge file (or a non-markdown file with an .md name) can't stall a request.
_MAX_FRONTMATTER_BYTES = 8 * 1024
_MAX_DESCRIPTION_CHARS = 400
_MANAGED_MARKER = ".oh-my-gateway-managed"


def _parse_frontmatter(path: Path) -> Dict[str, Any]:
    """Return the YAML frontmatter of *path* as a dict (``{}`` when absent)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(_MAX_FRONTMATTER_BYTES)
    except OSError:
        return {}
    if not head.startswith("---"):
        return {}
    body = head[3:]
    end = body.find("\n---")
    if end == -1:
        return {}
    try:
        import yaml

        data = yaml.safe_load(body[:end])
    except Exception:  # noqa: BLE001 — malformed frontmatter is not fatal
        return {}
    return data if isinstance(data, dict) else {}


def _meta(path: Path, fallback_name: str) -> Dict[str, str]:
    """``{name, description}`` for one skill/agent file.

    Frontmatter ``name`` wins over the filename (that is what the CLI keys on),
    and the description is trimmed to a single line.
    """
    front = _parse_frontmatter(path)
    name = str(front.get("name") or fallback_name).strip() or fallback_name
    description = " ".join(str(front.get("description") or "").split())
    return {"name": name, "description": description[:_MAX_DESCRIPTION_CHARS]}


def _skill_files(base: Path) -> List[Path]:
    """Skill definition files under a project/user skill directory.

    Both layouts the CLI accepts: ``<dir>/<name>.md`` (flat) and
    ``<dir>/<name>/SKILL.md`` (nested). The base directory itself must be real;
    this keeps catalog reads from following an operator-created resource-root
    symlink outside the expected scope. Individual symlinked entries are skipped
    for the same reason.
    """
    files: List[Path] = []
    if not base.is_dir() or base.is_symlink():
        return files
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return files
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file() and entry.suffix == ".md":
            files.append(entry)
        elif entry.is_dir():
            nested = entry / "SKILL.md"
            if nested.is_file() and not nested.is_symlink():
                files.append(nested)
    return files


def _agent_files(base: Path) -> List[Path]:
    """Subagent definition files under a project/user agent directory."""
    files: List[Path] = []
    if not base.is_dir() or base.is_symlink():
        return files
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return files
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file() and entry.suffix == ".md":
            files.append(entry)
        elif entry.is_dir():
            for filename in ("AGENT.md", "agent.md"):
                nested = entry / filename
                if nested.is_file() and not nested.is_symlink():
                    files.append(nested)
                    break
    return files


def _dir_entries(base: Path, source: str, kind: str) -> List[Dict[str, str]]:
    """Collect ``{name, description, source}`` for one scope directory."""
    files = _skill_files(base) if kind == "skills" else _agent_files(base)
    out: List[Dict[str, str]] = []
    for path in files:
        fallback = (
            path.parent.name
            if path.name in ("SKILL.md", "AGENT.md", "agent.md")
            else path.stem
        )
        meta = _meta(path, fallback)
        out.append({**meta, "source": source, "plugin": ""})
    return out


def _user_scope_dir() -> Optional[Path]:
    """``~/.claude`` when the CLI would read user scope, else ``None``.

    Mirrors :func:`src.backends.claude.client._get_setting_sources` rather than
    guessing: listing a user-scope skill the session will not load would be a
    lie in the other direction.
    """
    from src.backends.claude.client import _get_setting_sources

    try:
        sources = _get_setting_sources()
    except Exception:  # noqa: BLE001 — never fail a catalog read on this
        return None
    if "user" not in sources:
        return None
    home = Path.home()
    return home / ".claude" if home.is_dir() else None


def _plugin_entries(kind: str) -> List[Dict[str, str]]:
    """Plugin-contributed skills/subagents, with descriptions read from disk."""
    from src import plugin_service

    out: List[Dict[str, str]] = []
    try:
        plugins = plugin_service.list_plugins()
    except Exception:  # noqa: BLE001 — a broken registry must not 500 the client
        logger.debug("plugin catalog read failed", exc_info=True)
        return out
    for plugin in plugins:
        install_path = plugin.get("install_path")
        for item in plugin.get(kind) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            description = ""
            rel = item.get("path")
            if install_path and isinstance(rel, str) and rel:
                candidate = Path(install_path) / rel
                if candidate.is_file():
                    description = _meta(candidate, name)["description"]
            out.append(
                {
                    "name": name,
                    "description": description,
                    "source": "plugin",
                    "plugin": str(plugin.get("name") or ""),
                }
            )
    return out


def _merge(*groups: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """First occurrence wins, in scope precedence order, sorted by name."""
    seen: Dict[str, Dict[str, str]] = {}
    for group in groups:
        for entry in group:
            seen.setdefault(entry["name"], entry)
    return sorted(seen.values(), key=lambda e: e["name"].lower())


def _project_entries(workspace: Path, kind: str) -> List[Dict[str, str]]:
    """Read the project resource definition Claude will actually execute.

    A normal gateway-managed workspace has a top-level canonical directory plus
    a native ``.claude/<kind>`` mirror marked with ``.oh-my-gateway-managed``;
    the canonical side is the user-facing source of truth and is sufficient for
    cataloging.  Legacy workspaces with only the native directory still work.

    If both directories exist but the native one is *unmanaged*, migration was
    intentionally skipped to avoid overwriting independently managed data. In
    that conflict state Claude Code still discovers the native definition, so it
    must win same-name catalog collisions; otherwise the picker would advertise a
    different resource from the one the backend actually runs.
    """
    visible_dir = workspace / kind
    native_dir = workspace / ".claude" / kind
    visible = _dir_entries(visible_dir, "project", kind)

    if (native_dir / _MANAGED_MARKER).is_file():
        return visible

    legacy = _dir_entries(native_dir, "project", kind)
    if visible and legacy:
        return _merge(legacy, visible)
    return _merge(visible, legacy)


def list_agent_resources(workspace: Optional[Path]) -> Dict[str, List[Dict[str, str]]]:
    """``{"skills": [...], "agents": [...]}`` for a workspace.

    Each entry is ``{name, description, source, plugin}``. *workspace* is the
    user's session cwd (``None`` skips project scope). Never raises.
    """
    project_skills: List[Dict[str, str]] = []
    project_agents: List[Dict[str, str]] = []
    if workspace is not None:
        project_skills = _project_entries(workspace, "skills")
        project_agents = _project_entries(workspace, "agents")

    user_dir = _user_scope_dir()
    user_skills = _dir_entries(user_dir / "skills", "user", "skills") if user_dir else []
    user_agents = _dir_entries(user_dir / "agents", "user", "agents") if user_dir else []

    return {
        "skills": _merge(project_skills, user_skills, _plugin_entries("skills")),
        "agents": _merge(project_agents, user_agents, _plugin_entries("agents")),
    }
