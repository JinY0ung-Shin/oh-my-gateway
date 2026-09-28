"""PreToolUse hook enforcing per-user workspace boundaries and soft quotas.

Claude Code's ``cwd`` option sets a starting directory but does not constrain
file operations to it (see anthropic/claude-agent-sdk-python issues #36/#457).
``acceptEdits`` mode is documented to restrict edits to ``cwd`` +
``additionalDirectories`` but the current SDK does not enforce that boundary,
and ``bypassPermissions`` bypasses the check entirely. As a result, absolute
paths in Read/Write/Edit/Bash calls reach other users' workspaces under the
shared root.

This hook closes that gap when ``WORKSPACE_SANDBOX_ENABLED`` is enabled. The
same hook is also installed when ``USER_WORKSPACE_QUOTA_MB`` is enabled so
predictable in-workspace writes (Write/Edit/MultiEdit) can be preflighted without
implicitly turning on the otherwise opt-in path-boundary policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.workspace_manager import workspace_manager
from src.workspace_quota import (
    WorkspaceQuotaAccountingError,
    WorkspaceQuotaExceeded,
    ensure_growth_fits,
    quota_snapshot,
    workspace_quota_limit_bytes,
)

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


def _boundary_enabled() -> bool:
    return _env_flag("WORKSPACE_SANDBOX_ENABLED", False)


def sandbox_enabled() -> bool:
    """Whether the shared workspace policy hook must be installed.

    The historical sandbox remains opt-in. A storage quota needs the same hook
    transport for deterministic Claude writes, but does not itself enable path
    confinement.
    """
    return _boundary_enabled() or workspace_quota_limit_bytes() > 0


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


def plugin_resource_roots() -> List[Path]:
    """Public: absolute plugin/skill/marketplace roots outside the workspace.

    Two consumers share this list: the sandbox hook (as a read/exec allow-list)
    and the gateway's CLI ``--add-dir`` configuration. Claude Code confines
    ``cd`` and file operations to the session cwd plus its additional working
    directories — independently of this hook — so an admin-installed plugin
    skill that ``cd``s into its own plugin/resource directory is otherwise
    rejected ("...may only change directories to the allowed working
    directories..."). Granting these as additional directories unblocks that
    while writes stay confined by :func:`make_workspace_sandbox_hook`.
    """
    return _plugin_resource_roots(_claude_home())


def _read_json(path: Path) -> Any:
    """Read and parse a JSON file, returning ``None`` on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# Shared assets under ``$HOME/.claude`` a session may only READ: skills, plugins,
# subagents, commands, output styles and the user-scope CLAUDE.md. Every user's
# session uses them, so writes stay denied.
_SHARED_CLAUDE_ASSETS = (
    "skills",
    "plugins",
    "agents",
    "commands",
    "output-styles",
    "CLAUDE.md",
)
# The CLI names a project directory ``projects/<cwd with every non-alphanumeric
# UTF-16 code unit replaced by '-'>``; past 200 units it keeps the first 200 and
# appends ``-<hash of the raw cwd>`` (CLI 2.1.283 ``qx``/``Le``, reproduced by
# ``_project_dir_name`` and pinned by ``tests/test_cli_project_dir_name.py``).
_PROJECT_NAME_MAX = 200
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _utf16_units(value: str) -> List[int]:
    data = value.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def _encode_project_dir(path: str) -> str:
    return "".join(
        chr(u) if u < 128 and chr(u).isalnum() else "-" for u in _utf16_units(path)
    )


def _cli_path_hash(value: str) -> str:
    """The CLI's short hash: the 31-multiplier string hash over UTF-16 units,
    wrapped to a signed 32-bit int, absolute value, in base 36."""
    h = 0
    for unit in _utf16_units(value):
        h = (h * 31 + unit) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    n = abs(h)
    digits = ""
    while True:
        n, r = divmod(n, 36)
        digits = _BASE36[r] + digits
        if n == 0:
            return digits


def _project_dir_name(path: str) -> str:
    """The exact ``projects/`` entry name the CLI uses for cwd *path*."""
    name = _encode_project_dir(path)
    if len(name) <= _PROJECT_NAME_MAX:
        return name
    return f"{name[:_PROJECT_NAME_MAX]}-{_cli_path_hash(path)}"


def _own_project_roots(
    workspace_root: Path, claude_home: Optional[Path]
) -> List[Path]:
    """This session's own ``$HOME/.claude/projects/<encoded cwd>``.

    That directory holds the session's tool results, auto memory and
    transcripts. Exactly the entry named after **this** workspace is returned,
    never ``projects/`` itself or a prefix match, since its siblings are other
    users' sessions. Both the given and the resolved workspace path are
    encoded, because the CLI names the directory after the cwd it was started
    with.
    """
    if claude_home is None:
        return []
    projects = claude_home / "projects"
    roots: List[Path] = []
    seen: Set[Path] = set()
    for variant in (str(workspace_root), str(Path(workspace_root).resolve())):
        _add_root(roots, seen, projects / _project_dir_name(variant))
    return roots


def _writable_extra_roots(workspace_root: Path) -> List[Path]:
    """Full-access (read + write + exec) roots outside the per-user workspace.

    The CLI keeps this session's own state under
    ``$HOME/.claude/projects/<encoded cwd>`` (large tool results the model is
    told to Read, auto memory), which the model reaches via ``~/.claude/...``,
    outside the workspace. Only that one directory is granted, not
    ``$HOME/.claude`` as a whole: every user's CLI child shares one HOME, so
    ``~/.claude/plans`` and the other ``projects/*`` directories hold **other
    users'** plans and transcripts. Returns an empty list when ``$HOME`` is
    unset (issue #115).
    """
    return _own_project_roots(workspace_root, _claude_home())


def _shared_asset_roots(claude_home: Optional[Path]) -> List[Path]:
    """Read-only shared assets under ``$HOME/.claude``.

    See ``_SHARED_CLAUDE_ASSETS``.
    """
    if claude_home is None:
        return []
    roots: List[Path] = []
    seen: Set[Path] = set()
    for name in _SHARED_CLAUDE_ASSETS:
        _add_root(roots, seen, claude_home / name)
    return roots


def _deny(reason: str) -> Dict[str, Any]:
    logger.warning("Workspace policy denied tool call: %s", reason)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _quota_user_root(workspace_root: Path) -> Optional[Path]:
    """Return the named user's aggregate root for a managed backend workspace.

    Anonymous workspaces are direct ``_tmp_*`` children of the base path, while
    named backend workspaces have exactly ``<user>/<backend-dir>`` below it.
    Custom ClaudeCodeCLI cwd values outside the managed workspace tree must not
    accidentally inherit a per-user quota.
    """
    if workspace_quota_limit_bytes() <= 0:
        return None
    try:
        base = workspace_manager.base_path.resolve()
        workspace = Path(workspace_root).resolve()
        relative = workspace.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    if len(relative.parts) != 2 or relative.parts[0].startswith("_tmp_"):
        return None
    return base / relative.parts[0]


def _apply_text_edits(text: str, edits: list[dict]) -> str:
    """Best-effort mirror of Claude text edit semantics for size preflight."""
    result = text
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        if edit.get("replace_all"):
            result = result.replace(old, new)
        else:
            result = result.replace(old, new, 1)
    return result


def _quota_write_projection(
    tool_name: str, tool_input: dict, target: Path
) -> Optional[tuple[int, int]]:
    """Return ``(new_bytes, reclaimed_bytes)`` when a write is predictable."""
    try:
        old_bytes = target.stat().st_size if target.is_file() else 0
    except OSError:
        old_bytes = 0

    if tool_name == "Write":
        content = tool_input.get("content")
        if isinstance(content, str):
            return len(content.encode("utf-8")), old_bytes
        return None

    if tool_name in {"Edit", "MultiEdit"}:
        try:
            original = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if tool_name == "Edit":
            edits = [tool_input]
        else:
            raw_edits = tool_input.get("edits")
            if not isinstance(raw_edits, list):
                return None
            edits = raw_edits
        updated = _apply_text_edits(original, edits)
        return len(updated.encode("utf-8")), old_bytes

    return None


def _quota_preflight_reason(
    workspace_root: Path,
    user_root: Path,
    tool_name: str,
    tool_input: dict,
    path: str,
) -> Optional[str]:
    """Blocking half of the quota preflight: return a denial reason or ``None``.

    Everything here touches the filesystem — ``Path.resolve``, ``stat``, the
    ``read_text`` an Edit projection needs, and the recursive ``scandir`` walk
    behind ``quota_snapshot``/``ensure_growth_fits`` — so it must run in a
    worker thread (see :func:`_quota_denial`), never on the gateway event loop.
    """
    target = _resolve_within_any([workspace_root], path)
    if target is None:
        return None

    try:
        projection = _quota_write_projection(tool_name, tool_input, target)
        if projection is None:
            # NotebookEdit and un-decodable/racy edits cannot be predicted safely.
            # If the workspace is already full, refuse another opaque write; below
            # the limit the soft quota permits it and the next check observes usage.
            snapshot = quota_snapshot(user_root)
            if snapshot.enabled and snapshot.used_bytes >= snapshot.limit_bytes:
                return (
                    "Workspace quota: storage is full "
                    f"({snapshot.used_bytes}/{snapshot.limit_bytes} bytes). "
                    "Delete files before another write."
                )
            return None

        added, reclaimed = projection
        ensure_growth_fits(
            user_root,
            added_bytes=added,
            reclaimed_bytes=reclaimed,
        )
    except WorkspaceQuotaExceeded as exc:
        return (
            "Workspace quota exceeded: "
            f"used {exc.used_bytes} bytes, limit {exc.limit_bytes} bytes, "
            f"projected {exc.projected_bytes} bytes. Delete files or reduce "
            "the write before retrying."
        )
    except WorkspaceQuotaAccountingError as exc:
        # Same fail-closed rule as the file API: an unreadable or I/O-failed
        # subtree is never counted as zero bytes, so a quota-growing write is
        # refused rather than allowed on an undercounted total.
        return (
            "Workspace quota: storage usage could not be measured safely "
            f"({exc.detail.get('error', {}).get('message', 'accounting failed')}). "
            "The write was refused rather than allowed against an incomplete total."
        )
    return None


async def _quota_denial(
    workspace_root: Path,
    user_root: Optional[Path],
    tool_name: str,
    tool_input: dict,
    path: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Preflight deterministic Claude writes against the aggregate user quota.

    The projection/accounting work is O(files) filesystem I/O (recursive
    ``scandir``/``stat``, plus ``read_text`` of an Edit target). The hook is
    an ``async`` callback on the gateway's event loop — the same loop serving
    every ``/v1/responses`` stream and websocket — so that work is offloaded
    to a worker thread exactly like the file API's ``run_in_threadpool``
    quota scans. Only the decision shaping happens on the loop.
    """
    if user_root is None or not isinstance(path, str) or not path:
        return None
    reason = await asyncio.to_thread(
        _quota_preflight_reason, workspace_root, user_root, tool_name, tool_input, path
    )
    if reason is None:
        return None
    return _deny(reason)


# Matches path-like substrings beginning with ``/`` (absolute), ``./`` or
# ``../`` (relative traversal) or ``~`` (home). Used as a fallback for shell
# constructs ``shlex`` cannot tokenize cleanly (here-docs, ``$(...)``).
_PATH_RE = re.compile(r"(?:^|[\s=:'\"`(])((?:/|\.\.?/|~/?)[\w./\-+@]*)")


# Network URLs (``scheme://authority/...``) are blanked out before path
# extraction: the ``:`` before ``//host`` is a path boundary for ``_PATH_RE``
# and ``//host/...`` then reads as an absolute path outside the workspace, so
# every ``curl https://...`` / ``git clone https://...`` was denied (#176).
# Two forms are deliberately left visible because they address the local
# filesystem: an empty authority (``file:///etc/x``, ``sqlite:////srv/x.db``)
# and any ``file`` scheme (``file://localhost/etc/x``, ``git+file://...``).
# The scheme must start a token so ``file://`` cannot be matched one
# character in as ``ile://``.
_NET_URL_RE = re.compile(
    r"(?<![\w+.\-])"
    r"(?!(?:[\w+.\-]*\+)?file:)"
    r"[A-Za-z][\w+.\-]*://"
    r"[^\s'\"`()<>/]"
    r"[^\s'\"`()<>]*",
    re.IGNORECASE,
)


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

    Network URLs are blanked out first (see ``_NET_URL_RE``) so ``https://host``
    is not mistaken for an absolute path. ``shlex`` handles quoting and
    ``--flag=value`` / ``VAR=value`` forms (the value after ``=`` is inspected
    separately so an escape hidden in a flag is not masked by the flag name). A
    regex pass over the raw command is layered on top for shell constructs
    ``shlex`` cannot split cleanly.
    """
    command = _NET_URL_RE.sub(" ", command)
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
    """Build the shared PreToolUse workspace policy hook.

    Quota preflight is independent of the path sandbox. When the sandbox is
    enabled, the historical two-tier roots remain unchanged:

    * ``write_roots`` — workspace plus this session's own
      ``$HOME/.claude/projects/<encoded cwd>``;
    * ``read_roots`` — write roots plus the read-only shared assets under
      ``$HOME/.claude`` (skills, plugins, agents, …) and plugin resource
      roots. The rest of ``$HOME/.claude`` (``plans/``, other ``projects/*``)
      belongs to other users.

    Direct callers historically receive a boundary-enforcing hook even when the
    env flag is off; preserve that behavior when quota is disabled. The special
    quota-only mode (quota on, sandbox flag off) is the only case where boundary
    checks are intentionally skipped.
    """
    given_root = Path(workspace_root)
    workspace_root = given_root.resolve()
    quota_enabled = workspace_quota_limit_bytes() > 0
    quota_user_root = _quota_user_root(workspace_root)
    boundary_enabled = _boundary_enabled() or not quota_enabled

    claude_home = _claude_home()
    write_roots = [workspace_root] + _writable_extra_roots(given_root)
    read_roots = list(write_roots)
    seen: Set[Path] = set(read_roots)
    for root in _shared_asset_roots(claude_home) + _plugin_resource_roots(claude_home):
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
            path = tool_input.get(key)

            if category == "write":
                quota_result = await _quota_denial(
                    workspace_root,
                    quota_user_root,
                    tool_name,
                    tool_input,
                    path if isinstance(path, str) else None,
                )
                if quota_result:
                    return quota_result

            if not boundary_enabled or category in allowed:
                return {}
            roots = read_roots if category == "read" else write_roots
            if isinstance(path, str) and path:
                if _resolve_within_any(roots, path) is None:
                    return _deny(
                        f"Workspace sandbox: {tool_name} target {path!r} is "
                        f"outside the session workspace ({write_roots[0]})."
                    )
            return {}

        if tool_name == "Bash":
            # Bash is intentionally best-effort for storage quota: an arbitrary
            # shell command has no reliable pre-execution byte delta. The file API
            # and deterministic Write/Edit paths are exact; Bash may temporarily
            # exceed the soft quota and subsequent quota checks observe that state.
            if not boundary_enabled or "bash" in allowed:
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


# Characters that make a path a pattern the shell or Glob expands later.
_GLOB_CHARS = "*?["


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def make_claude_home_guard_hook(workspace_root: Path):
    """Build the always-on PreToolUse guard for the shared ``$HOME/.claude``.

    Every gateway CLI child shares one HOME, so ``~/.claude/projects/*`` holds
    every user's transcripts, tool results and auto memory, and ``plans/`` the
    CLI's default plan files. That is cross-user confidentiality, not the
    opt-in workspace policy, so this guard is installed for every session
    regardless of ``WORKSPACE_SANDBOX_ENABLED`` / ``WORKSPACE_SANDBOX_ALLOW_OUTSIDE``
    and whatever the permission mode (PreToolUse hooks run under
    ``bypassPermissions`` too). Inside ``$HOME/.claude`` it allows:

    * read + write: this session's own ``projects/<encoded cwd>``;
    * read only: the shared assets (skills, plugins, agents, commands,
      output styles, ``CLAUDE.md``) and the plugin resource roots, including
      the ones outside ``$HOME/.claude`` (``CLAUDE_PLUGIN_CLONE_ROOT``, a
      marketplace ``installLocation``, an ``installPath`` outside the cache).

    Everything else under ``$HOME/.claude`` is denied. Other paths outside it
    are left to the workspace sandbox.

    The guarantee covers the file tools (Read, Grep, Glob, Write, Edit,
    MultiEdit, NotebookEdit), whose target is a structured path. For Bash
    this is **defense in depth only**, not a security boundary: it catches the
    statically visible paths (plus ``$VAR`` expansion and glob prefixes such
    as ``~/.cl*``), but shell semantics defeat any static parse. Shell-local
    variables (``H=$HOME; cat $H/.claude/...``), a ``cd`` followed by a
    relative path, and command substitution all get through, and Bash is not
    split into read and write intent, so it can also modify the shared
    assets. The gateway does not isolate Bash across users today: even the
    CLI's OS-level bash sandbox (``CLAUDE_SANDBOX_ENABLED``) only restricts
    reads through ``Read`` deny rules, which are not configured (#218). Do not
    describe this guard as covering Bash.
    """
    workspace = Path(workspace_root).resolve()
    claude_home = _claude_home()
    own = _own_project_roots(Path(workspace_root), claude_home)
    readable = list(own)
    for root in _shared_asset_roots(claude_home) + _plugin_resource_roots(claude_home):
        if root not in readable:
            readable.append(root)
    # Shared, read-only for the write tools wherever they live: plugin resource
    # roots may sit outside $HOME/.claude (CLAUDE_PLUGIN_CLONE_ROOT, a
    # marketplace installLocation, an installPath outside the cache). A root
    # that contains the workspace itself is left out so the session can still
    # write its own files.
    shared_read_only = [
        root
        for root in readable
        if root not in own and not _is_within(workspace, root)
    ]

    def _absolute(candidate: str) -> str:
        expanded = os.path.expandvars(os.path.expanduser(candidate))
        if not os.path.isabs(expanded):
            expanded = os.path.join(str(workspace), expanded)
        return expanded

    def violation(candidate: str, write: bool) -> Optional[str]:
        if claude_home is None or not candidate:
            return None
        allowed = own if write else readable
        absolute = _absolute(candidate)
        cut = min((absolute.find(c) for c in _GLOB_CHARS if c in absolute), default=-1)
        if cut >= 0:
            # A pattern: deny when its fixed prefix could expand into
            # $HOME/.claude outside the allowed roots.
            prefix = os.path.normpath(absolute[:cut]) if cut else "/"
            fixed = absolute[:cut]
            home = str(claude_home)
            reaches = home.startswith(fixed) or _is_within(Path(prefix), claude_home)
            if not reaches:
                return None
            if any(_is_within(Path(prefix), root) for root in allowed):
                return None
            return candidate
        try:
            resolved = Path(absolute).resolve()
        except (OSError, RuntimeError, ValueError):
            return candidate
        if write and not any(_is_within(resolved, root) for root in own):
            if any(_is_within(resolved, root) for root in shared_read_only):
                return candidate
        if not _is_within(resolved, claude_home):
            return None
        if any(_is_within(resolved, root) for root in allowed):
            return None
        return candidate

    def deny(tool: str, path: str) -> Dict[str, Any]:
        return _deny(
            f"{tool} target {path!r} is shared Claude state ({claude_home} or a "
            "shared plugin root), which holds other users' sessions and plans. "
            "Only this session's own project directory is available there; "
            "shared skills and plugins are read-only."
        )

    async def hook(input_data, _tool_use_id, _context):
        if claude_home is None or not isinstance(input_data, dict):
            return {}
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
        if tool_name in _TOOL_TABLE:
            category, key, _optional = _TOOL_TABLE[tool_name]
            write = category == "write"
            candidates = [tool_input.get(key)]
            if tool_name == "Glob":
                candidates.append(tool_input.get("pattern"))
            for candidate in candidates:
                if isinstance(candidate, str) and violation(candidate, write):
                    return deny(tool_name, candidate)
            return {}
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            if not isinstance(command, str):
                return {}
            for candidate in _bash_path_candidates(command):
                if violation(candidate, write=False):
                    return deny("Bash", candidate)
        return {}

    return hook
