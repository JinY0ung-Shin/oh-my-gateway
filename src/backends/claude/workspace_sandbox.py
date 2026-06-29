"""PreToolUse hook enforcing per-user workspace boundaries.

Claude Code's ``cwd`` option sets a starting directory but does not constrain
file operations to it (see anthropic/claude-agent-sdk-python issues #36/#457).
``acceptEdits`` mode is documented to restrict edits to ``cwd`` +
``additionalDirectories`` but the current SDK does not enforce that boundary,
and ``bypassPermissions`` bypasses the check entirely. As a result, absolute
paths in Read/Write/Edit/Bash calls reach other users' workspaces under the
shared root.

This hook closes that gap by rejecting any PreToolUse call whose resolved path
escapes the session's workspace root.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# Tool → (category, path-key, is_optional)
# Category groups several tools so users can release a whole class at once via
# WORKSPACE_SANDBOX_ALLOW_OUTSIDE.
_TOOL_TABLE: Dict[str, tuple[str, str, bool]] = {
    "Read": ("read", "file_path", False),
    "Write": ("write", "file_path", False),
    "Edit": ("write", "file_path", False),
    "MultiEdit": ("write", "file_path", False),
    "NotebookEdit": ("write", "notebook_path", False),
    "Glob": ("read", "path", True),
    "Grep": ("read", "path", True),
}
_VALID_CATEGORIES = {"read", "write", "bash"}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def sandbox_enabled() -> bool:
    return _env_flag("WORKSPACE_SANDBOX_ENABLED", False)


def _allow_outside() -> Set[str]:
    """Categories permitted to reach paths outside the workspace.

    ``WORKSPACE_SANDBOX_ALLOW_OUTSIDE`` is a comma-separated list of
    ``read`` / ``write`` / ``bash``. Unknown entries are ignored with a warning.
    """
    raw = os.getenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "")
    if not raw.strip():
        return set()
    entries = {part.strip().lower() for part in raw.split(",") if part.strip()}
    invalid = entries - _VALID_CATEGORIES
    if invalid:
        logger.warning(
            "Ignoring unknown WORKSPACE_SANDBOX_ALLOW_OUTSIDE entries: %s",
            sorted(invalid),
        )
    return entries & _VALID_CATEGORIES


def _resolve_within_any(roots: list[Path], candidate: str) -> Optional[Path]:
    """Return the resolved path if it lies inside any of *roots*, else ``None``.

    Relative paths are resolved against ``roots[0]`` (Claude's cwd / workspace
    root). ``resolve()`` collapses ``..`` and symlinks so escape attempts via
    either are caught. A candidate passes as soon as it falls inside one root.
    """
    try:
        p = Path(candidate)
        if not p.is_absolute():
            p = roots[0] / p
        resolved = p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    return None


def _claude_home() -> Optional[Path]:
    """Resolve ``$HOME/.claude``, or ``None`` when ``$HOME`` is unset.

    Only ``$HOME/.claude`` is allowed — not all of ``$HOME`` — so ``~/.ssh`` and
    other home paths stay denied (issue #115). Returns ``None`` when ``$HOME`` is
    unset so the canonical state dir is not silently inferred; the gateway
    process always has ``$HOME`` set (``/home/app`` in the container).
    """
    home = os.getenv("HOME")
    if not home:
        return None
    try:
        return (Path(home) / ".claude").resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _add_root(roots: List[Path], seen: Set[Path], candidate: Path) -> None:
    """Append *candidate*'s resolved path to *roots* if not already present.

    Only absolute candidates are accepted: a relative env/registry value would
    resolve against the gateway's working directory and silently grant an
    unintended root. The admin ``claude`` CLI always records absolute paths.
    """
    if not candidate.is_absolute():
        return
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return
    if resolved not in seen:
        seen.add(resolved)
        roots.append(resolved)


def _plugin_resource_roots(claude_home: Optional[Path]) -> List[Path]:
    """Plugin/skill/marketplace roots that may live outside the workspace.

    Plugin skills are shared, read-only assets — not per-user data — so the
    sandbox must let a session *read* the skill document (``SKILL.md``) and its
    bundled resources even when they sit outside the per-user workspace. These
    roots are granted to read/exec (Read/Glob/Grep/Bash) but **not** to the
    write tools (Write/Edit/MultiEdit/NotebookEdit), so a session cannot modify
    shared plugin source that other users' sessions execute (see
    :func:`make_workspace_sandbox_hook`). Most installs keep these under
    ``$HOME/.claude`` (already a full-access root via :func:`_claude_home`), but
    several legitimate layouts escape it:

    * ``CLAUDE_PLUGIN_CLONE_ROOT`` pointed at a volume outside ``~/.claude``;
    * a ``project``/``local``-scope marketplace added from an arbitrary local
      clone path (its ``installLocation`` is that path);
    * a plugin whose ``installPath`` resolves outside the cache.

    Without these roots, invoking such a plugin's skill is denied with
    "target ... is outside the session workspace". Sources are read
    best-effort from the admin-written plugin registry; any failure is ignored.
    """
    roots: List[Path] = []
    seen: Set[Path] = set()

    clone_root = os.getenv("CLAUDE_PLUGIN_CLONE_ROOT", "").strip()
    if clone_root:
        _add_root(roots, seen, Path(clone_root))

    if claude_home is None:
        return roots
    plugins_dir = claude_home / "plugins"

    # Marketplace clones registered with `claude plugin marketplace add`.
    known = _read_json(plugins_dir / "known_marketplaces.json")
    if isinstance(known, dict):
        for info in known.values():
            if not isinstance(info, dict):
                continue
            loc = info.get("installLocation")
            if isinstance(loc, str) and loc:
                _add_root(roots, seen, Path(loc))

    # Installed plugin cache directories (one entry per scope).
    installed = _read_json(plugins_dir / "installed_plugins.json")
    if isinstance(installed, dict):
        plugins = installed.get("plugins")
        if isinstance(plugins, dict):
            for entries in plugins.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(
                        entry.get("installPath"), str
                    ):
                        _add_root(roots, seen, Path(entry["installPath"]))

    return roots


def _read_json(path: Path) -> Any:
    """Read and parse a JSON file, returning ``None`` on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _writable_extra_roots() -> List[Path]:
    """Full-access (read + write + exec) roots outside the per-user workspace.

    The Claude Code SDK keeps its own internal state under ``$HOME/.claude``
    (e.g. ``projects/.../tool-results``) and reaches it via ``~/.claude/...``,
    which expands outside the workspace root. That access must be permitted
    explicitly or the sandbox breaks normal SDK operation. Only ``$HOME/.claude``
    is added — not all of ``$HOME`` — so ``~/.ssh`` and other home paths stay
    denied. Returns an empty list when ``$HOME`` is unset (issue #115).
    """
    roots: List[Path] = []
    seen: Set[Path] = set()
    claude_home = _claude_home()
    if claude_home is not None:
        _add_root(roots, seen, claude_home)
    return roots


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# Matches path-like substrings beginning with ``/`` (absolute), ``./`` or
# ``../`` (relative traversal) or ``~`` (home). Used as a fallback for shell
# constructs ``shlex`` cannot tokenize cleanly (here-docs, ``$(...)``).
_PATH_RE = re.compile(r"(?:^|[\s=:'\"`(])((?:/|\.\.?/|~/?)[\w./\-+@]*)")


def _is_path_like(token: str) -> bool:
    """Whether *token* looks like a filesystem path worth boundary-checking.

    Covers absolute paths, any token containing a separator, bare parent refs
    (``..``) and home refs (``~``/``~user``). Inside-workspace relative paths
    also match but resolve within *root*, so checking them is harmless.
    """
    return bool(token) and ("/" in token or token == ".." or token.startswith("~"))


def _expand_user(path: str) -> str:
    """Expand a leading ``~`` so home-relative escapes are resolved statically."""
    return os.path.expanduser(path) if path.startswith("~") else path


def _bash_path_candidates(command: str) -> list[str]:
    """Extract path-like substrings from a Bash *command*.

    ``shlex`` handles quoting and ``--flag=value`` / ``VAR=value`` forms (the
    value after ``=`` is inspected separately so an escape hidden in a flag is
    not masked by the flag name). A regex pass over the raw command is layered
    on top for shell constructs ``shlex`` cannot split cleanly.
    """
    candidates: list[str] = []
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
    for tok in tokens:
        parts = (tok, tok.split("=", 1)[1]) if "=" in tok else (tok,)
        candidates.extend(p for p in parts if _is_path_like(p))
    candidates.extend(m.group(1) for m in _PATH_RE.finditer(command))
    return candidates


def _check_bash(command: str, roots: list[Path]) -> Optional[str]:
    """Return a deny reason if *command* references paths outside *roots*.

    Catches absolute paths, ``..`` traversal and ``~`` references — including
    relative symlink targets such as ``ln -s ../../secret link``. Only
    statically visible paths are inspected: runtime shell expansions (``$VAR``,
    command substitution output) cannot be resolved here and are deferred to the
    OS-level sandbox (``CLAUDE_SANDBOX_ENABLED``).
    """
    if not command:
        return None
    for path in _bash_path_candidates(command):
        if _resolve_within_any(roots, _expand_user(path)) is None:
            return (
                f"Workspace sandbox: Bash referenced path outside workspace "
                f"({path}). Allowed root: {roots[0]}."
            )
    return None


def make_workspace_sandbox_hook(workspace_root: Path):
    """Build a PreToolUse hook that confines tool calls to *workspace_root*.

    The returned callable matches the signature expected by
    :class:`claude_agent_sdk.types.HookMatcher`. It returns an empty dict to
    let the call proceed, or a ``deny`` decision to block it.

    Two tiers of allowed roots are kept:

    * ``write_roots`` — the workspace plus the SDK's own ``$HOME/.claude`` state
      dir; full read + write + exec.
    * ``read_roots`` — ``write_roots`` plus the shared plugin/skill/marketplace
      resource roots (:func:`_plugin_resource_roots`). These are granted to
      read/exec tools (Read/Glob/Grep/Bash) so plugin skills are usable, but
      **not** to the write tools, so a session cannot overwrite shared plugin
      source that other users' sessions execute.
    """
    claude_home = _claude_home()
    write_roots = [Path(workspace_root).resolve()] + _writable_extra_roots()
    read_roots = list(write_roots)
    seen: Set[Path] = set(read_roots)
    for root in _plugin_resource_roots(claude_home):
        if root not in seen:
            seen.add(root)
            read_roots.append(root)
    allowed = _allow_outside()

    async def hook(input_data, _tool_use_id, _context):
        if not isinstance(input_data, dict):
            return {}
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}

        if tool_name in _TOOL_TABLE:
            category, key, _optional = _TOOL_TABLE[tool_name]
            if category in allowed:
                return {}
            # Read-class tools may reach shared plugin resources; write-class
            # tools are confined to the workspace + $HOME/.claude.
            roots = read_roots if category == "read" else write_roots
            path = tool_input.get(key)
            if isinstance(path, str) and path:
                if _resolve_within_any(roots, path) is None:
                    return _deny(
                        f"Workspace sandbox: {tool_name} target {path!r} is "
                        f"outside the session workspace ({write_roots[0]})."
                    )
            return {}

        if tool_name == "Bash":
            if "bash" in allowed:
                return {}
            command = tool_input.get("command", "")
            reason = (
                _check_bash(command, read_roots) if isinstance(command, str) else None
            )
            if reason:
                return _deny(reason)
            return {}

        return {}

    return hook
