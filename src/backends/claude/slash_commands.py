"""Slash-command validation for the Claude backend.

The Claude Agent SDK interprets any user message whose first non-whitespace
character is ``/`` as a slash-command invocation.  If the command name is
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
* For other (command-shaped) slash prompts, the name is checked against a
  **TTL-cached allowlist** pulled from ``ClaudeSDKClient.get_server_info()``.
  Unknown names are rejected with ``unknown_command`` (the SDK would otherwise
  silently return ``"Unknown skill: <name>"`` with 0 tokens); recognised names
  are allowed through so that intentional skills (e.g. ``/dev-server``) work.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

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


class SlashCommandError(Exception):
    """Raised when a slash-prefixed prompt is rejected."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class _Cache:
    def __init__(self) -> None:
        self.commands: Optional[set[str]] = None
        self.fetched_at: float = 0.0
        self.lock = asyncio.Lock()

    def is_fresh(self) -> bool:
        return (
            self.commands is not None
            and (time.monotonic() - self.fetched_at) < CACHE_TTL_SECONDS
        )

    def reset(self) -> None:
        self.commands = None
        self.fetched_at = 0.0


_cache = _Cache()


async def _fetch_commands(cwd: Optional[Path]) -> set[str]:
    """Pull the registered slash-command names from the SDK.

    Uses the same setting sources as the backend's SDK calls so user-scope
    plugin skills installed by the admin panel are visible to the preflight.
    """
    from src.backends.claude.client import _get_setting_sources

    opts = ClaudeAgentOptions(cwd=cwd, setting_sources=_get_setting_sources())
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
    cwd: Optional[Path] = None, force: bool = False
) -> set[str]:
    async with _cache.lock:
        if not force and _cache.is_fresh():
            assert _cache.commands is not None
            return _cache.commands
        _cache.commands = await _fetch_commands(cwd)
        _cache.fetched_at = time.monotonic()
        return _cache.commands


class _DetailsCache:
    def __init__(self) -> None:
        self.details: Optional[dict[str, dict[str, str]]] = None
        self.fetched_at: float = 0.0
        self.lock = asyncio.Lock()

    def is_fresh(self) -> bool:
        return (
            self.details is not None
            and (time.monotonic() - self.fetched_at) < CACHE_TTL_SECONDS
        )


_details_cache = _DetailsCache()


async def _fetch_command_details(cwd: Optional[Path]) -> dict[str, dict[str, str]]:
    """Names plus SDK metadata (description, argumentHint) for completion UIs."""
    from src.backends.claude.client import _get_setting_sources

    opts = ClaudeAgentOptions(cwd=cwd, setting_sources=_get_setting_sources())
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
    cwd: Optional[Path] = None, force: bool = False
) -> dict[str, dict[str, str]]:
    """Like :func:`get_available_commands` but with per-command metadata."""
    async with _details_cache.lock:
        if not force and _details_cache.is_fresh():
            assert _details_cache.details is not None
            return _details_cache.details
        _details_cache.details = await _fetch_command_details(cwd)
        _details_cache.fetched_at = time.monotonic()
        return _details_cache.details


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


async def validate_prompt(prompt: str, cwd: Optional[Path] = None) -> None:
    """Raise ``SlashCommandError`` if ``prompt`` is a slash command we reject.

    No-ops for prompts that don't start with ``/``.
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

    known = await get_available_commands(cwd)
    if name in known:
        return

    # Refresh once in case a skill was added after the cache was populated.
    known = await get_available_commands(cwd, force=True)
    if name in known:
        return

    raise SlashCommandError(
        code="unknown_command",
        message=(
            f"Unknown slash command '/{name}'. Not a registered skill or "
            "built-in on this server."
        ),
    )
