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
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Frontmatter is the contract skills/subagents already use; cap the read so a
# huge file (or a non-markdown file with an .md name) can't stall a request.
_MAX_FRONTMATTER_BYTES = 8 * 1024
_MAX_DESCRIPTION_CHARS = 400
_MANAGED_MARKER = ".oh-my-gateway-managed"


def _opened_beneath(fd: int, root: Optional[str]) -> bool:
    """Whether the open *fd* is a file below the resolved *root* (no root: yes).

    The checks before the open looked at each name, but a session can swap an
    intermediate directory (``.claude``, ``skills/x``) for a link between them
    and the open; what the descriptor really is decides."""
    if root is None:
        return True
    try:
        actual = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return False
    return actual.startswith(root.rstrip(os.sep) + os.sep)


def _parse_frontmatter(path: Path, root: Optional[str] = None) -> Dict[str, Any]:
    """Return the YAML frontmatter of *path* as a dict (``{}`` when absent).

    *root*: the resolved directory the file must really lie below."""
    # The file was checked with lstat, but the workspace keeps changing: open
    # without following a symlink and without blocking on a FIFO, then confirm
    # a regular file below *root* on the open descriptor.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or not _opened_beneath(fd, root):
            return {}
        os.set_blocking(fd, True)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(_MAX_FRONTMATTER_BYTES)
    except OSError:
        return {}
    finally:
        os.close(fd)
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


def _meta(
    path: Path, fallback_name: str, root: Optional[str] = None
) -> Dict[str, str]:
    """``{name, description}`` for one skill/agent file.

    Frontmatter ``name`` wins over the filename (that is what the CLI keys on),
    and the description is trimmed to a single line.
    """
    front = _parse_frontmatter(path, root)
    name = str(front.get("name") or fallback_name).strip() or fallback_name
    description = " ".join(str(front.get("description") or "").split())
    return {"name": name, "description": description[:_MAX_DESCRIPTION_CHARS]}


def _lstat_mode(path: Path) -> int:
    """*path*'s own file type bits (a symlink is a symlink), ``0`` when it
    cannot be looked at. ``Path.is_file()``/``is_dir()`` follow links and, on
    an unsearchable parent, raise ``PermissionError`` on Python 3.12 instead of
    answering ``False``: a session uid that closes one of its own dirs must not
    turn the catalog into a 500 (uid isolation)."""
    try:
        return os.lstat(path).st_mode
    except OSError:
        return 0


def _real_file(path: Path) -> bool:
    return stat.S_ISREG(_lstat_mode(path))


def _real_dir(path: Path) -> bool:
    return stat.S_ISDIR(_lstat_mode(path))


def _skill_files(base: Path) -> List[Path]:
    """Skill definition files under a project/user skill directory.

    Both layouts the CLI accepts: ``<dir>/<name>.md`` (flat) and
    ``<dir>/<name>/SKILL.md`` (nested). The base directory itself must be real;
    this keeps catalog reads from following an operator-created resource-root
    symlink outside the expected scope. Individual symlinked entries are skipped
    for the same reason.
    """
    files: List[Path] = []
    if not _real_dir(base):
        return files
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return files
    for entry in entries:
        if _real_file(entry):
            if entry.suffix == ".md":
                files.append(entry)
        elif _real_dir(entry):
            nested = entry / "SKILL.md"
            if _real_file(nested):
                files.append(nested)
    return files


def _agent_files(base: Path) -> List[Path]:
    """Subagent definition files under a project/user agent directory."""
    files: List[Path] = []
    if not _real_dir(base):
        return files
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return files
    for entry in entries:
        if _real_file(entry):
            if entry.suffix == ".md":
                files.append(entry)
        elif _real_dir(entry):
            for filename in ("AGENT.md", "agent.md"):
                nested = entry / filename
                if _real_file(nested):
                    files.append(nested)
                    break
    return files


def _dir_entries(
    base: Path, source: str, kind: str, root: Optional[Path] = None
) -> List[Dict[str, str]]:
    """Collect ``{name, description, source}`` for one scope directory.

    *root*: a session-writable tree (the workspace) every file read must lie in.
    """
    files = _skill_files(base) if kind == "skills" else _agent_files(base)
    real_root = None
    if root is not None:
        try:
            real_root = os.path.realpath(root)
        except (OSError, ValueError):
            return []
    out: List[Dict[str, str]] = []
    for path in files:
        fallback = (
            path.parent.name
            if path.name in ("SKILL.md", "AGENT.md", "agent.md")
            else path.stem
        )
        meta = _meta(path, fallback, real_root)
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
    from src.claude_config_paths import config_dir

    base = config_dir()
    # Preserve the old gate: return the dir only when its parent (the home dir,
    # or the CLAUDE_CONFIG_DIR parent) exists; the dir itself need not.
    return base if base.parent.is_dir() else None


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
    intentionally skipped to avoid overwriting independently managed data, and no
    mirror is built (see ``_prepare_resource_dir``). In that conflict state the
    only definitions Claude Code discovers are the native ones, so the catalog is
    **exactly** the native directory — not a union with the canonical side.

    A union would advertise a canonical-only entry the backend cannot run: a name
    present under ``skills/`` but absent from the unmanaged ``.claude/skills/``
    would appear in the picker and then fail to load, which is the opposite of
    "the picker describes the definition Claude will actually execute". Same-name
    collisions were already resolved in favour of native; this extends the same
    reasoning to names that exist on one side only. The shadowed names are logged
    once so an operator can see what the conflict is costing them.
    """
    from src.backends.claude.workspace_resources import is_managed_mirror

    visible_dir = workspace / kind
    native_dir = workspace / ".claude" / kind
    visible = _dir_entries(visible_dir, "project", kind, root=workspace)

    if is_managed_mirror(workspace, kind):
        return visible
    # Read the native side only through real directories: a ``.claude`` the
    # session replaced with a link would have the gateway catalog whatever it
    # points at.
    if not (_real_dir(workspace / ".claude") and _real_dir(native_dir)):
        return visible

    legacy = _dir_entries(native_dir, "project", kind, root=workspace)
    if visible and legacy:
        shadowed = sorted({e["name"] for e in visible} - {e["name"] for e in legacy})
        if shadowed:
            logger.warning(
                "Workspace %s has an unmanaged .claude/%s, so canonical-only %s "
                "are not discoverable by Claude and are left out of the catalog: %s",
                workspace,
                kind,
                kind,
                ", ".join(shadowed),
            )
        return legacy
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
