"""Process-level hardening applied once when the gateway starts serving.

Every Claude CLI child runs as the gateway's own uid, so anything a session
executes (Bash, a hook, an MCP server) can open the gateway's ``/proc/<pid>/``
entries: ``environ``, ``mem`` and the ``fd/`` links. Marking the serving
process non-dumpable (``prctl(PR_SET_DUMPABLE, 0)``) makes the kernel hand
them to root, so a same-uid process gets EACCES, with no capability and no
container change.

What that adds is narrow. The SDK builds each CLI child's env as
``{**os.environ, **options.env}``. For session CLI children the gateway scrubs
a few names first: ``child_env_mask()`` blanks ``ADMIN_API_KEY``, ``API_KEY``
and the ``SYSINFO_CHILD_ENV_MASK`` names (only while ``SYSINFO_REDACTION`` is
on), and ``_sdk_env`` drops ``OPENAI_API_KEY`` (plus ``CLAUDE_CODE_OAUTH_TOKEN``
in api-key auth). Those values then sit only in this process's environ; this
closes that ``/proc/<gateway>/environ`` bypass and keeps the gateway's memory
and open fds out of reach. Not every spawn path goes through that scrubbing
yet (#230 follow-up). Every other variable (``ANTHROPIC_AUTH_TOKEN`` among
them) is still in each session's own env, and dumpable resets to 1 on
``execve`` of an ordinary binary, so a CLI child's environ stays readable by
other same-uid processes. ``SYSINFO_CHILD_ENV_MASK`` adds gateway-only names to
the session-child mask. The protection starts at the prctl call; it does not
revoke fds another process opened on this one before it.

Side effects on this process: no core dumps; attaching py-spy/gdb needs
CAP_SYS_PTRACE (host root, or ``cap_add: [SYS_PTRACE]``; a ``docker exec``
root shell has none by default); and it loses its own owner-only
``/proc/self`` files (``environ``, ``io``, ``auxv``, ...) and writes to its own
``oom_score_adj``. Nothing in the gateway uses them; ``/proc/self/fd`` and the
files prometheus' process collector reads stay accessible.
``GATEWAY_NON_DUMPABLE=false`` keeps the process dumpable.
"""

import ctypes
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

NON_DUMPABLE_ENV = "GATEWAY_NON_DUMPABLE"

_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4
_SUID_DUMP_DISABLE = 0
_OFF_VALUES = {"0", "false", "no", "off"}


def non_dumpable_enabled() -> bool:
    """``GATEWAY_NON_DUMPABLE`` (default on): only an explicit false value opts out.

    Blank counts as unset, because a compose ``- VAR`` passthrough or an empty
    .env line produces one, and neither should silently drop the protection.
    """
    raw = os.getenv(NON_DUMPABLE_ENV, "true")
    return raw.strip().lower() not in _OFF_VALUES


def _call(prctl, option: int, arg2: int = 0) -> int:
    # prctl is variadic and the kernel reads every argument as unsigned long.
    zero = ctypes.c_ulong(0)
    return prctl(ctypes.c_int(option), ctypes.c_ulong(arg2), zero, zero, zero)


def _set_non_dumpable() -> Optional[str]:
    """``prctl(PR_SET_DUMPABLE, 0)`` and read it back; the failure, or ``None``."""
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError) as exc:
        return f"prctl unavailable ({exc})"
    if _call(prctl, _PR_SET_DUMPABLE, _SUID_DUMP_DISABLE) != 0:
        return f"prctl(PR_SET_DUMPABLE, 0) failed: {os.strerror(ctypes.get_errno())}"
    state = _call(prctl, _PR_GET_DUMPABLE)
    if state != 0:
        return f"still dumpable after prctl (PR_GET_DUMPABLE returned {state})"
    return None


def apply_non_dumpable_policy() -> str:
    """Mark this process non-dumpable unless opted out. Never raises.

    Returns ``"on"``, ``"off"`` (opted out), ``"unsupported"`` (not Linux) or
    ``"failed"`` (no ``prctl``, or the kernel refused) and logs one line
    saying which.
    """
    if not non_dumpable_enabled():
        logger.warning(
            "%s=%s: the gateway process stays dumpable, so a same-uid process "
            "(a session's Bash, hook or MCP server) can open its "
            "/proc/%d/environ, mem and fds",
            NON_DUMPABLE_ENV,
            os.getenv(NON_DUMPABLE_ENV, "").strip(),
            os.getpid(),
        )
        return "off"
    if not sys.platform.startswith("linux"):
        logger.info("Non-dumpable process hardening: unsupported on %s", sys.platform)
        return "unsupported"
    try:
        error = _set_non_dumpable()
    except Exception as exc:  # never let hardening stop the gateway
        error = f"unexpected error: {exc!r}"
    if error is None:
        logger.info(
            "Gateway process is non-dumpable (PR_SET_DUMPABLE=0): same-uid "
            "processes cannot open its /proc/<pid>/environ, mem or fds "
            "(%s=false keeps it dumpable)",
            NON_DUMPABLE_ENV,
        )
        return "on"
    logger.warning(
        "Could not make the gateway process non-dumpable (%s); same-uid "
        "processes can open its /proc/<pid>/environ, mem and fds",
        error,
    )
    return "failed"
