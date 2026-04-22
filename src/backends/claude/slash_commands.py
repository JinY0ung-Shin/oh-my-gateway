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

* A small **blocklist** of destructive built-ins is always rejected with
  ``blocked_command``.
* For other slash-prefixed prompts, the command name is checked against a
  **TTL-cached allowlist** pulled from ``ClaudeSDKClient.get_server_info()``.
  Unknown names are rejected with ``unknown_command``; recognised names are
  allowed through so that intentional skills (e.g. ``/dev-server``) still work.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

BLOCKED_COMMANDS: frozenset[str] = frozenset({"compact", "init", "heapdump"})
CACHE_TTL_SECONDS: float = 60.0


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

    Uses ``setting_sources=["project", "local"]`` to match the backend's own
    configuration (see ``src/backends/claude/client.py``).
    """
    opts = ClaudeAgentOptions(cwd=cwd, setting_sources=["project", "local"])
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


def extract_command_name(prompt: str) -> Optional[str]:
    """Return the command name (without the leading ``/``) or ``None``.

    The SDK itself strips leading whitespace before dispatching, so we do the
    same — ``"  /help"`` is equivalent to ``"/help"``.
    """
    stripped = prompt.lstrip()
    if not stripped.startswith("/"):
        return None
    rest = stripped[1:]
    if not rest or rest[0].isspace():
        # A lone "/" or "/ text" — the SDK returns a syntax error; treat as
        # unknown so the caller sees a proper error response.
        return ""
    name = rest.split(None, 1)[0]
    # Trim any trailing punctuation the user might have attached; the SDK
    # only dispatches on the bare name.  Keep ``:`` because it's used in
    # namespaced skills like ``superpowers:brainstorming``.
    return name


async def validate_prompt(prompt: str, cwd: Optional[Path] = None) -> None:
    """Raise ``SlashCommandError`` if ``prompt`` is a slash command we reject.

    No-ops for prompts that don't start with ``/``.
    """
    name = extract_command_name(prompt)
    if name is None:
        return

    if name == "":
        raise SlashCommandError(
            code="unknown_command",
            message="Slash-prefixed input without a command name is not supported.",
        )

    if name in BLOCKED_COMMANDS:
        raise SlashCommandError(
            code="blocked_command",
            message=(
                f"Slash command '/{name}' is blocked by this server because it "
                "has side effects (history compaction, file creation, etc.). "
                "Prefix your message with a non-slash character if you intended "
                "a plain user message."
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
