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

import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Optional, Set

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


def _resolve_within(root: Path, candidate: str) -> Optional[Path]:
    """Return the resolved path if it lies inside *root*, else ``None``.

    Relative paths are resolved against *root* (Claude's cwd). ``resolve()``
    collapses ``..`` and symlinks so escape attempts via either are caught.
    """
    try:
        p = Path(candidate)
        if not p.is_absolute():
            p = root / p
        resolved = p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


_ABS_PATH_RE = re.compile(r"(?:^|[\s=:'\"`(])(/[\w./\-+@]+)")


def _check_bash(command: str, root: Path) -> Optional[str]:
    """Return a deny reason if *command* references paths outside *root*."""
    if not command:
        return None
    # shlex catches quoted arguments; fall back to regex for shell constructs
    # (here-docs, $(...)) that shlex can't tokenize cleanly.
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
    candidates = [t for t in tokens if t.startswith("/")]
    candidates.extend(m.group(1) for m in _ABS_PATH_RE.finditer(command))
    for path in candidates:
        if _resolve_within(root, path) is None:
            return (
                f"Workspace sandbox: Bash referenced path outside workspace "
                f"({path}). Allowed root: {root}."
            )
    return None


def make_workspace_sandbox_hook(workspace_root: Path):
    """Build a PreToolUse hook that confines tool calls to *workspace_root*.

    The returned callable matches the signature expected by
    :class:`claude_agent_sdk.types.HookMatcher`. It returns an empty dict to
    let the call proceed, or a ``deny`` decision to block it.
    """
    root = Path(workspace_root).resolve()
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
            path = tool_input.get(key)
            if isinstance(path, str) and path:
                if _resolve_within(root, path) is None:
                    return _deny(
                        f"Workspace sandbox: {tool_name} target {path!r} is "
                        f"outside the session workspace ({root})."
                    )
            return {}

        if tool_name == "Bash":
            if "bash" in allowed:
                return {}
            command = tool_input.get("command", "")
            reason = _check_bash(command, root) if isinstance(command, str) else None
            if reason:
                return _deny(reason)
            return {}

        return {}

    return hook
