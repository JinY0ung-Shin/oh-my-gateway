"""Slash-command validation for the Claude backend.

The Claude Agent SDK interprets any user message whose first non-whitespace
character is ``/`` as a slash-command invocation. If the command name is
registered (built-in or skill from ``.claude/skills/``), the SDK runs it and
returns the command output *instead of* calling the model; if the name is not
registered, the SDK returns ``"Unknown skill: <name>"`` with 0 tokens consumed.

From an OpenAI-compatible API perspective, both outcomes are problematic:
  1. Unknown commands silently return HTTP 200 + a non-model string.
  2. Destructive built-ins (``/compact``, ``/init``, ``/heapdump``) can mutate
     session history or the working directory before the caller realises it.

This module validates the prompt before it reaches the SDK:

* Input that only *starts* with ``/`` but is not a command — file paths
  (``/home/x``), URLs (``/api/v1/users``), a bare ``/`` — is **not** a slash
  command. The SDK passes it to the model as a plain message, so the gateway
  lets it through unchanged (issue #117). Only a command-name-shaped token
  (alphanumeric/kebab, optional ``:namespace``) is treated as a command.
* A small **blocklist** of destructive built-ins — plus any names in the
  ``BLOCKED_SLASH_COMMANDS`` env var — is always rejected with
  ``blocked_command``.
* Arguments containing any ``@`` are rejected with ``unsupported_argument``:
  a slash turn is the one turn the gateway sends without verbatim delivery,
  so the CLI would expand a file mention and inline the file. No pattern is
  tried — the CLI's mention grammar (CJK sentence punctuation and JavaScript
  whitespace such as U+FEFF as prefixes, quoting, expansion after
  ``$ARGUMENTS`` substitution) is wider than any text pre-screen.
* For other (command-shaped) slash prompts, the name is checked against a
  **TTL-cached allowlist** pulled from ``ClaudeSDKClient.get_server_info()``.
  The cache is bound to the effective cwd so one user's project skills can
  never validate another user's prompt. Unknown names are rejected with
  ``unknown_command``; recognised names are allowed through.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from .uid_layout import to_thread_to_completion

# Destructive built-ins are always blocked. Operators extend the set with the
# BLOCKED_SLASH_COMMANDS env var (comma-separated names, leading slash
# optional) to disable additional skills/commands gateway-wide. This gates the
# client-typed ``/name`` path only; blocking the model-initiated path takes a
# ``Skill(<name>)`` entry in DISALLOWED_TOOLS.
_BUILTIN_BLOCKED_COMMANDS: frozenset[str] = frozenset({"compact", "init", "heapdump"})


def _parse_blocked_commands_env(raw: str) -> frozenset[str]:
    """Parse BLOCKED_SLASH_COMMANDS: comma-separated, slash prefix optional."""
    return frozenset(
        name for name in (c.strip().lstrip("/") for c in raw.split(",")) if name
    )


BLOCKED_COMMANDS: frozenset[str] = _BUILTIN_BLOCKED_COMMANDS | (
    _parse_blocked_commands_env(os.getenv("BLOCKED_SLASH_COMMANDS", ""))
)
CACHE_TTL_SECONDS: float = 60.0
# A forced refresh ("a requested skill may have just been installed") is skipped
# when the cached catalog is younger than this: a catalog that was read a moment
# ago already is the current one, and a session whose Skill policy names a skill
# its workspace lacks otherwise paid for two preflight CLIs on every new session.
FORCE_REFRESH_MIN_AGE_SECONDS: float = 10.0


def _preflight_concurrency() -> int:
    raw = os.getenv("SLASH_PREFLIGHT_CONCURRENCY", "").strip()
    try:
        value = int(raw) if raw else 4
    except ValueError:
        value = 4
    return max(1, value)


# Every preflight spawns a whole CLI (several seconds). They used to run behind
# one process-wide lock with a single cached workspace, so concurrent first turns
# of different users queued one after another (a fifth user waited ~25 s before
# their own CLI even started). Now each workspace has its own entry and lock, and
# this semaphore only caps how many preflight CLIs run at once.
PREFLIGHT_CONCURRENCY: int = _preflight_concurrency()

# A slash-command name is alphanumeric/kebab-case, optionally namespaced with
# ``:`` (e.g. ``help``, ``dev-server``, ``superpowers:brainstorming``). Anything
# else after a leading ``/`` — file paths (``/home/x``), URLs (``/api/v1/users``)
# — is not a command name; the SDK passes such input to the model as a plain
# message, so the gateway must not reject it. See issue #117.
#
# The character class matches the CLI's own command-shape test
# (``!/[^a-zA-Z0-9:\-_]/``): any token of these chars is command-shaped and, if
# unregistered, the CLI returns ``"Unknown skill"`` with 0 tokens — so we keep
# such tokens on the validated path (→ a clear 400) rather than letting them
# through to that silent response.
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9:_-]+$")

# A slash command must go unstamped (no ``client_composed``) for the CLI to
# dispatch it, so the CLI expands ``@`` file mentions in its arguments and
# inlines the file — outside the workspace too, past the workspace sandbox hook.
# Every ``@`` is refused rather than pattern-matched: the CLI treats CJK
# sentence punctuation and JavaScript whitespace (e.g. U+FEFF) as mention
# prefixes, unquotes ``"@path"``, and expands ``@@/../x``, so a regex screen
# was bypassable (verified on CLI 2.1.283). Emails in arguments are the cost.


class SlashCommandError(Exception):
    """Raised when a slash-prefixed prompt is rejected."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _cwd_key(cwd: Optional[Path]) -> Optional[str]:
    """Stable cache identity for a session cwd without requiring it to exist."""
    if cwd is None:
        return None
    try:
        return str(Path(cwd).resolve())
    except (OSError, RuntimeError):
        return str(cwd)


def _materialize_project_resources(cwd: Optional[Path]) -> None:
    """Refresh a prepared workspace's Claude-native project resource mirrors."""
    if cwd is None:
        return
    from src.backends.claude.workspace_resources import materialize_workspace_resources

    materialize_workspace_resources(Path(cwd))


class _PerCwdCache:
    """TTL cache of one preflight result per workspace.

    Each workspace (``_cwd_key``) has its own entry and its own lock, so a
    preflight for one user's workspace never waits for another user's, and two
    concurrent first turns in the same workspace share one fetch. Expired
    entries and idle locks are pruned as new workspaces arrive, so the maps stay
    bounded by the workspaces active within one TTL.
    """

    def __init__(self) -> None:
        self.entries: dict[Optional[str], tuple[object, float]] = {}
        self.locks: dict[Optional[str], asyncio.Lock] = {}

    def fresh(
        self, key: Optional[str], ttl: float = CACHE_TTL_SECONDS
    ) -> Optional[object]:
        entry = self.entries.get(key)
        if entry is None or (time.monotonic() - entry[1]) >= ttl:
            return None
        return entry[0]

    def lock_for(self, key: Optional[str]) -> asyncio.Lock:
        lock = self.locks.get(key)
        if lock is None:
            self._prune()
            lock = self.locks[key] = asyncio.Lock()
        return lock

    def store(self, key: Optional[str], value: object) -> None:
        self.entries[key] = (value, time.monotonic())

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [
            k for k, (_, at) in self.entries.items() if now - at >= CACHE_TTL_SECONDS
        ]:
            del self.entries[key]
        for key in [
            k
            for k, lock in self.locks.items()
            if not lock.locked() and k not in self.entries
        ]:
            del self.locks[key]

    def reset(self) -> None:
        self.entries.clear()
        self.locks.clear()

    def expire_all(self) -> None:
        """Age every entry past the TTL (tests)."""
        self.entries = {
            k: (v, at - CACHE_TTL_SECONDS - 1) for k, (v, at) in self.entries.items()
        }


_preflight_slots: Optional[asyncio.Semaphore] = None
_preflight_slots_loop: Optional[asyncio.AbstractEventLoop] = None


def _slots() -> asyncio.Semaphore:
    """The preflight cap for the running loop (a semaphore binds to one loop)."""
    global _preflight_slots, _preflight_slots_loop
    loop = asyncio.get_running_loop()
    if _preflight_slots is None or _preflight_slots_loop is not loop:
        _preflight_slots = asyncio.Semaphore(PREFLIGHT_CONCURRENCY)
        _preflight_slots_loop = loop
    return _preflight_slots


async def _cached_preflight(
    cache: _PerCwdCache, cwd: Optional[Path], force: bool, fetch
):
    """The cached result for *cwd*, fetching it (under the per-workspace lock and
    the global preflight cap) when missing, expired or forced."""
    key = _cwd_key(cwd)
    hit = cache.fresh(key)
    if hit is not None and not force:
        return hit
    async with cache.lock_for(key):
        # Someone may have fetched while we waited for the lock.
        hit = cache.fresh(
            key, FORCE_REFRESH_MIN_AGE_SECONDS if force else CACHE_TTL_SECONDS
        )
        if hit is not None:
            return hit
        async with _slots():
            value = await fetch()
        cache.store(key, value)
        return value


_cache = _PerCwdCache()

_preflight_dir: Optional[Path] = None

# The preflight only reads the command catalog, so it runs with no MCP servers
# (strict and empty, as sessions are strict), every hook disabled and every
# settings command the CLI runs at startup blanked. In a session workspace the
# workspace's own hooks (InstructionsLoaded, SessionStart, SessionEnd), its
# ``.mcp.json`` stdio servers and its ``apiKeyHelper`` — plus the AWS / GCP
# auth helpers once its ``env`` block switches provider — otherwise ran during
# connect + get_server_info (#241 review). A ``--settings`` value outranks the
# project's, and ``""`` runs nothing. Measured on the bundled CLI 2.1.283: no
# hook, MCP or helper side effect, and the catalog is unchanged — workspace,
# user and plugin commands and skills included. Pinned by
# tests/test_cli_slash_preflight.py. statusLine is covered by disableAllHooks;
# subagentStatusLine and fileSuggestion never run headless.
_PREFLIGHT_HELPERS = (
    "apiKeyHelper",
    "proxyAuthHelper",
    "awsCredentialExport",
    "awsAuthRefresh",
    "gcpAuthRefresh",
    "otelHeadersHelper",
)
_PREFLIGHT_SETTINGS = json.dumps(
    {"disableAllHooks": True, **dict.fromkeys(_PREFLIGHT_HELPERS, "")}
)


def _preflight_cwd() -> Path:
    """A private, gateway-owned directory for a confined preflight with no cwd.

    When confinement is on, the launcher needs a real cwd (a ``None`` cwd makes
    it grab ``$PWD`` — ``/app`` in the image — as writable). This throwaway dir
    is never a session workspace; it only gives the confined catalog CLI a
    writable root of its own. ``mkdtemp`` makes it fresh and 0700 for this
    process: a fixed shared name under the temp dir could be pre-created by
    another uid with a ``.claude/settings.json`` the preflight then loads (#241
    review). Removed at exit.
    """
    global _preflight_dir
    if _preflight_dir is None:
        import atexit
        import shutil
        import tempfile

        _preflight_dir = Path(tempfile.mkdtemp(prefix="gateway-slash-preflight-"))
        atexit.register(shutil.rmtree, _preflight_dir, True)
    return _preflight_dir


def _build_preflight_options(
    cwd: Optional[Path], user: Optional[str] = None
) -> ClaudeAgentOptions:
    """Build the SDK options for a catalog/preflight spawn.

    Shared by the names and the details fetchers so they stay identical. Routes
    through the same cwd/confinement path a real session uses: ``cli_path`` is
    the launcher under confinement, and the per-workspace ro/rw lists (or, in
    uid mode, the session uid's broker ticket — ``user`` keys it, once that
    user has a uid) ride ``options.env`` so the confined catalog CLI can read
    ``~/.claude`` (#241).
    """
    from src.backends.claude import landlock
    from src.backends.claude.client import (
        _get_cli_path,
        _get_max_buffer_size,
        _get_setting_sources,
        child_secret_env,
    )

    if cwd is not None:
        _materialize_project_resources(cwd)
    opts = ClaudeAgentOptions(
        cwd=cwd,
        setting_sources=_get_setting_sources(),
        cli_path=_get_cli_path(),
        strict_mcp_config=True,
        settings=_PREFLIGHT_SETTINGS,
    )
    opts.max_buffer_size = _get_max_buffer_size()
    # The same secret blanks a session child gets (ADMIN_API_KEY, API_KEY, the
    # other backends' and auth-mode credentials, the ticket secret): whatever
    # still runs in the preflight sees none of them (#241 review). Then the
    # confinement env, in the order a session's options get them.
    env: dict[str, str] = dict(child_secret_env())
    if landlock.confine_enabled():
        if cwd is None:
            opts.cwd = _preflight_cwd()
            env.update(landlock.preflight_options_env(opts.cwd, gateway_owned=True))
        else:
            # Absolute like the path lists: the launcher reads its cwd from PWD.
            opts.cwd = Path(os.path.abspath(cwd))
            env.update(landlock.preflight_options_env(opts.cwd, user=user))
    opts.env = {**(getattr(opts, "env", None) or {}), **env}
    return opts


async def _fetch_commands(cwd: Optional[Path], user: Optional[str] = None) -> set[str]:
    """Pull the registered slash-command names from the SDK.

    Uses the same setting sources *and CLI binary* as the backend's SDK calls,
    so user-scope plugin skills installed by the admin panel are visible to the
    preflight and the answer describes the CLI that will actually run the turn.
    """
    # Off the event loop: the build refreshes the workspace's resource mirrors
    # and, under uid isolation, prepares the session uid's layout.
    opts = await to_thread_to_completion(_build_preflight_options, cwd, user)
    names: set[str] = set()
    async with ClaudeSDKClient(options=opts) as client:
        info = await client.get_server_info()
    if info:
        for c in info.get("commands") or []:
            if isinstance(c, dict):
                name = c.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
    return names


async def get_available_commands(
    cwd: Optional[Path] = None, force: bool = False, user: Optional[str] = None
) -> set[str]:
    """The registered command names for *cwd*, TTL-cached per cwd.

    ``user`` is whom the preflight runs as under ``uid`` isolation (the session
    in *cwd*) once that user has a uid; before that it runs landlock-shaped
    (``landlock.preflight_options_env``). The cache stays keyed on the cwd
    alone.
    """
    return await _cached_preflight(
        _cache, cwd, force, lambda: _fetch_commands(cwd, user)
    )


_details_cache = _PerCwdCache()


async def _fetch_command_details(
    cwd: Optional[Path], user: Optional[str] = None
) -> dict[str, dict[str, str]]:
    """Names plus SDK metadata (description, argumentHint) for completion UIs."""
    opts = await to_thread_to_completion(_build_preflight_options, cwd, user)
    details: dict[str, dict[str, str]] = {}
    async with ClaudeSDKClient(options=opts) as client:
        info = await client.get_server_info()
    if info:
        for c in info.get("commands") or []:
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            if isinstance(name, str) and name:
                details[name] = {
                    "description": str(c.get("description") or ""),
                    "argument_hint": str(
                        c.get("argumentHint") or c.get("argument_hint") or ""
                    ),
                }
    return details


async def get_command_details(
    cwd: Optional[Path] = None, force: bool = False, user: Optional[str] = None
) -> dict[str, dict[str, str]]:
    """Like :func:`get_available_commands` but with per-command metadata."""
    return await _cached_preflight(
        _details_cache, cwd, force, lambda: _fetch_command_details(cwd, user)
    )


def extract_command_name(prompt: str) -> Optional[str]:
    """Return the slash-command name (without the leading ``/``) or ``None``.

    Returns ``None`` for anything that is not a slash command — including
    slash-prefixed plain messages such as file paths (``/home/x``) and URLs
    (``/api/v1/users``), a bare ``/``, or ``/ text``. Such input passes through
    to the model unchanged, matching Claude Code (issue #117).

    The SDK itself strips leading whitespace before dispatching, so we do the
    same — ``"  /help"`` is equivalent to ``"/help"``.
    """
    stripped = prompt.lstrip()
    if not stripped.startswith("/"):
        return None
    rest = stripped[1:]
    if not rest or rest[0].isspace():
        # A lone "/" or "/ text" is not a command name — plain message.
        return None
    name = rest.split(None, 1)[0]
    # Only a command-name-shaped token is a slash command. A token carrying a
    # path separator or other punctuation (``home/ozymandias``, ``etc/passwd``)
    # is a slash-prefixed plain message, not a command, so let it through.
    if not _COMMAND_NAME_RE.match(name):
        return None
    return name


async def validate_prompt(
    prompt: str, cwd: Optional[Path] = None, user: Optional[str] = None
) -> None:
    """Raise ``SlashCommandError`` if ``prompt`` is a slash command we reject.

    No-ops for prompts that don't start with ``/``. ``user`` is the session's
    user, whom the catalog preflight in *cwd* runs as under ``uid`` isolation.
    """
    name = extract_command_name(prompt)
    if name is None:
        return

    if name in BLOCKED_COMMANDS:
        raise SlashCommandError(
            code="blocked_command",
            message=(
                f"Slash command '/{name}' is blocked by this server. "
                "Prefix your message with a non-slash character if you "
                "intended a plain user message."
            ),
        )

    arguments = prompt.lstrip()[1 + len(name) :]
    if "@" in arguments:
        raise SlashCommandError(
            code="unsupported_argument",
            message=(
                f"Slash command '/{name}' arguments cannot contain '@': the CLI "
                "would expand it as a file mention and inline the file. Remove "
                "the '@', or send the text as a plain message without the "
                "leading '/'."
            ),
        )

    known = await get_available_commands(cwd, user=user)
    if name in known:
        return

    # Refresh once in case a skill was added after the cache was populated.
    known = await get_available_commands(cwd, force=True, user=user)
    if name in known:
        return

    raise SlashCommandError(
        code="unknown_command",
        message=(
            f"Unknown slash command '/{name}'. Not a registered skill or "
            "built-in on this server."
        ),
    )
