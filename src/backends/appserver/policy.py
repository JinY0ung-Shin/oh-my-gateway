"""Canonical capability -> Codex runtime policy mapping (issue #173 §7).

The product policy boundary is expressed in canonical capabilities, not Claude
vendor vocabulary. This module derives the requested capability denies from a
request's ``disallowed_tools`` (which may use canonical tokens or common tool
names) and maps them onto Codex runtime settings (sandbox mode, approval
policy). Per §7, **if a requested deny cannot be enforced, session creation is
rejected** (``CapabilityError``) rather than silently weakening the harness.

Only denies that are actually requested constrain the session; a request that
denies nothing gets the configured default sandbox, so normal flows are never
over-restricted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Canonical capability tokens (issue §7).
FILESYSTEM_WRITE = "filesystem.write"
SHELL_EXECUTE = "shell.execute"
NETWORK = "network"

# Map common tool-name spellings a caller may put in disallowed_tools onto the
# canonical capability they gate.
_TOOL_TO_CAPABILITY = {
    "filesystem.write": FILESYSTEM_WRITE,
    "write": FILESYSTEM_WRITE,
    "edit": FILESYSTEM_WRITE,
    "applypatch": FILESYSTEM_WRITE,
    "apply_patch": FILESYSTEM_WRITE,
    "shell.execute": SHELL_EXECUTE,
    "shell": SHELL_EXECUTE,
    "bash": SHELL_EXECUTE,
    "exec": SHELL_EXECUTE,
    "exec_command": SHELL_EXECUTE,
    "network": NETWORK,
    "web_search": NETWORK,
    "websearch": NETWORK,
}


class CapabilityError(ValueError):
    """A requested capability deny cannot be enforced on the Codex runtime."""


def requested_denies(disallowed_tools: Optional[List[str]]) -> set:
    """Canonical capabilities the request asks to deny (from disallowed_tools)."""
    denies: set = set()
    for name in disallowed_tools or []:
        if not isinstance(name, str):
            continue
        cap = _TOOL_TO_CAPABILITY.get(name.strip().lower())
        if cap:
            denies.add(cap)
    return denies


def resolve_runtime_policy(
    *,
    default_sandbox: str,
    default_approval: str,
    disallowed_tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return the Codex ``sandbox``/``approvalPolicy`` for the requested policy.

    Enforceable denies tighten the sandbox:
      * ``filesystem.write`` denied -> ``read-only`` (writes confined away).
      * ``network`` denied -> ``read-only`` (workspace-write does not reliably
        confine network on the current runtime, so drop to the mode that does).

    A deny that no Codex setting can enforce is fatal:
      * ``shell.execute`` denied -> :class:`CapabilityError` (even a read-only
        sandbox still executes commands), so the session is refused rather than
        run with shell silently available.
    """
    denies = requested_denies(disallowed_tools)

    if SHELL_EXECUTE in denies:
        raise CapabilityError(
            "Codex runtime cannot deny shell.execute; refusing the session "
            "rather than running with shell available"
        )

    sandbox = default_sandbox
    if FILESYSTEM_WRITE in denies or NETWORK in denies:
        sandbox = "read-only"

    return {"sandbox": sandbox, "approvalPolicy": default_approval}
