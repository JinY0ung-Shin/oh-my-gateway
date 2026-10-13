"""Gateway side of the Landlock confinement launcher (issue #241, PR-A).

This wires the fixed launcher stub (``docker/landlock_launcher.py``) into the
CLI spawn path. When ``CLAUDE_ISOLATION_MODE`` selects a Landlock mode
(``landlock`` now; ``uid`` once the broker lands):

* ``_get_cli_path()`` points ``cli_path`` at the launcher instead of the real
  CLI, so every Claude child (session turns, slash preflight, ``verify()``) is
  confined with a Landlock ruleset inherited across ``execve``;
* per spawn, the gateway hands the launcher the read-only and read-write path
  lists for that workspace through ``options.env`` — built from the **same**
  helpers the always-on ``~/.claude`` guard uses, so a script confined here
  reaches exactly what a file tool may (its workspace, its own project dir, the
  shared read-only assets) and nothing else;
* the CLI's own OS sandbox is forced off, because bwrap cannot start inside a
  Landlock domain (see ``docs/security-process-isolation.md`` §3).

This is the no-privilege interim of the design (no uid switch, no broker). It
confines filesystem access; cross-session DAC and signal isolation are the
uid-per-user follow-up. Fail-closed: if confinement is requested but the
launcher or the real CLI cannot be resolved, startup refuses rather than
spawn an unconfined CLI (``check_config``).
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MODE_ENV = "CLAUDE_ISOLATION_MODE"
FALLBACK_ENV = "CLAUDE_ISOLATION_FALLBACK"
LAUNCHER_ENV = "GATEWAY_LANDLOCK_LAUNCHER"
REAL_CLI_ENV = "GATEWAY_REAL_CLI"
RO_ENV = "GATEWAY_LANDLOCK_RO"
RW_ENV = "GATEWAY_LANDLOCK_RW"
BROKER_SOCKET_ENV = "GATEWAY_ISOLATION_BROKER_SOCKET"
TICKET_ENV = "GATEWAY_ISOLATION_TICKET"
_SKIP_VERSION_CHECK_ENV = "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"

# The single operator knob for session isolation:
#   off      — no OS boundary (default)
#   landlock — Landlock ruleset, no privilege (kernel >= 5.19)
#   uid      — uid-per-user broker + Landlock (DAC + signal isolation too)
# Both landlock and uid apply the Landlock ruleset. An unknown value is rejected
# fail-closed in :func:`check_config`.
_LANDLOCK_MODES = {"landlock", "uid"}
_KNOWN_MODES = {"off", "landlock", "uid"}

# CAP_SETGID is bit 6, CAP_SETUID is bit 7 (linux/capability.h).
_CAP_SETGID_BIT = 1 << 6
_CAP_SETUID_BIT = 1 << 7

# The launcher ships beside this repo's docker/ directory. In the image it is a
# fixed, root-owned path; an operator may override the location, but never make
# it agent-writable (the design requires a fixed, non-agent-writable launcher).
_DEFAULT_LAUNCHER = (
    Path(__file__).resolve().parents[3] / "docker" / "landlock_launcher.py"
)


_effective_mode_cache: Optional[str] = None


def isolation_mode() -> str:
    """The configured isolation mode (lowercased); ``off`` when unset."""
    return (os.getenv(MODE_ENV) or "off").strip().lower()


def _cap_bnd_has_setuid_setgid() -> bool:
    """Whether this process's capability bounding set carries SETUID+SETGID.

    In uid mode the broker (not the gateway) wields them, but the gateway cannot
    read the broker's caps; the container-level signal is that they are in the
    bounding set (inherited from container start). Informative for diagnostics —
    the authoritative runtime signal is broker reachability.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("CapBnd:"):
                    bits = int(line.split()[1], 16)
                    return bool(bits & _CAP_SETUID_BIT) and bool(
                        bits & _CAP_SETGID_BIT
                    )
    except (OSError, ValueError):
        pass
    return False


def broker_socket_path() -> str:
    return (os.getenv(BROKER_SOCKET_ENV) or "").strip()


def _broker_reachable() -> bool:
    """Whether the resident broker is up and accepting on its socket."""
    import socket

    path = broker_socket_path()
    if not path:
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(path)
        s.close()
        return True
    except OSError:
        return False


def uid_mode_available() -> bool:
    """Whether ``uid`` mode can actually run here: Landlock usable and the
    broker reachable (the broker being up means the entrypoint gave it the
    privilege to switch uid)."""
    return landlock_abi() >= 2 and _broker_reachable()


def effective_mode() -> str:
    """The mode actually in force, after the fail-open-never fallback rule.

    ``uid`` stays ``uid`` when available. When it is not, it downgrades to
    ``landlock`` only if the operator explicitly set
    ``CLAUDE_ISOLATION_FALLBACK=landlock`` (with a warning); otherwise it stays
    ``uid`` and :func:`check_config` turns the unavailability into a startup
    error — never a silent drop to a weaker or absent boundary.

    Resolved once and cached (broker availability is fixed post-startup);
    :func:`reset_mode_cache` clears it for tests.
    """
    global _effective_mode_cache
    if _effective_mode_cache is not None:
        return _effective_mode_cache
    mode = isolation_mode()
    resolved = mode
    if mode == "uid" and not uid_mode_available():
        fallback = (os.getenv(FALLBACK_ENV) or "").strip().lower()
        if fallback == "landlock":
            logger.warning(
                "%s=uid is not available (Landlock ABI or broker); falling back "
                "to landlock per %s=landlock",
                MODE_ENV,
                FALLBACK_ENV,
            )
            resolved = "landlock"
    _effective_mode_cache = resolved
    return resolved


def reset_mode_cache() -> None:
    """Clear the memoized effective mode (tests, or a deliberate re-resolve)."""
    global _effective_mode_cache
    _effective_mode_cache = None


def confine_enabled() -> bool:
    """Whether the Landlock ruleset is applied to CLI children (landlock/uid).

    An unknown mode reads as not-confined here; startup (:func:`check_config`)
    rejects it fail-closed so it never silently runs off.
    """
    return effective_mode() in _LANDLOCK_MODES


def launcher_path() -> str:
    """The fixed launcher stub ``cli_path`` points at when confinement is on."""
    raw = (os.getenv(LAUNCHER_ENV) or "").strip()
    return raw or str(_DEFAULT_LAUNCHER)


def resolve_real_cli() -> Optional[str]:
    """The real CLI the launcher execs: operator override, else the SDK bundle.

    ``CLAUDE_CLI_PATH`` keeps its existing meaning (try a newer CLI build); when
    it is unset or not executable, fall back to the SDK's bundled binary via the
    SDK's own resolver. Returns ``None`` only when nothing resolves — the caller
    turns that into a fail-closed startup error.
    """
    from .constants import CLAUDE_CLI_PATH

    if CLAUDE_CLI_PATH:
        p = Path(CLAUDE_CLI_PATH)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        logger.warning(
            "%s=%r is not an executable file; falling back to the bundled CLI",
            "CLAUDE_CLI_PATH",
            CLAUDE_CLI_PATH,
        )
    return _bundled_cli()


def _bundled_cli() -> Optional[str]:
    """The SDK's bundled CLI path, via the SDK resolver (robust to layout)."""
    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        # _find_cli / _find_bundled_cli touch no instance state on the bundled
        # branch, so a no-__init__ instance resolves the path without building a
        # full transport (which needs prompt/options).
        transport = SubprocessCLITransport.__new__(SubprocessCLITransport)
        cli = transport._find_cli()
        if cli and os.path.isfile(cli) and os.access(cli, os.X_OK):
            return cli
    except Exception as exc:  # pragma: no cover - defensive, exercised at startup
        logger.warning("Could not resolve the bundled CLI via the SDK: %s", exc)
    return None


def install_process_env() -> None:
    """Install the process-wide env the launcher needs, at startup.

    ``GATEWAY_REAL_CLI`` must be visible to spawns that carry no
    ``options.env`` (the SDK's ``claude -v`` check uses the process env), so it
    goes in ``os.environ``. We also skip that version-check spawn: it would run
    confined with only the default ruleset and adds nothing. Per-workspace path
    lists still ride ``options.env`` (see :func:`options_env`). No-op unless
    confinement is on.
    """
    # Resolve and lock the effective mode once, before the first spawn, and log
    # it (a uid->landlock fallback is surfaced here, not silently per-call).
    mode = effective_mode()
    requested = isolation_mode()
    if requested != mode:
        logger.warning("isolation mode requested=%s, effective=%s", requested, mode)
    elif mode in _LANDLOCK_MODES:
        logger.info("isolation mode: %s", mode)
    if mode not in _LANDLOCK_MODES:
        return
    real_cli = resolve_real_cli()
    if real_cli:
        os.environ[REAL_CLI_ENV] = real_cli
    if not (os.environ.get(_SKIP_VERSION_CHECK_ENV) or "").strip():
        os.environ[_SKIP_VERSION_CHECK_ENV] = "1"


def landlock_abi() -> int:
    """The kernel's Landlock ABI, or 0/-1 when unavailable.

    A read-only version query (``landlock_create_ruleset(NULL, 0, VERSION)``);
    it changes nothing. Used by :func:`check_config` to fail closed before the
    first spawn on a kernel that cannot enforce the ruleset.
    """
    machine = os.uname().machine
    if machine.startswith(("alpha", "ia64", "mips")):
        return -1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        ctypes.set_errno(0)
        res = libc.syscall(ctypes.c_long(444), 0, 0, 1 << 0)
        return int(res)
    except Exception:  # pragma: no cover - defensive
        return -1


def _ro_rw_roots(effective_cwd: Path) -> Tuple[List[str], List[str]]:
    """Build the read-only and read-write path lists for *effective_cwd*.

    Reuses the guard's helpers so confinement and the file-tool guard grant the
    same set. The launcher adds the static system roots, the real CLI dir,
    ``/proc`` and ``/dev`` entries itself; here we add only what depends on the
    workspace and the shared assets.
    """
    from . import workspace_sandbox as ws

    claude_home = ws._claude_home()
    ro: List[str] = []
    rw: List[str] = []

    # Read-only shared assets: skills, plugins, agents, commands, output-styles,
    # user CLAUDE.md, and the plugin clone roots the CLI executes from.
    for root in ws._shared_asset_roots(claude_home):
        ro.append(str(root))
    for root in ws.plugin_resource_roots():
        ro.append(str(root))
    # Operator add-dirs are read-only (see #237/#240); keep them readable here.
    for raw in ws.operator_add_dirs():
        ro.append(raw)

    # Read-write: the workspace itself and this session's own ~/.claude project
    # dir (transcripts, auto memory, spilled tool results).
    rw.append(str(effective_cwd))
    for root in ws._writable_extra_roots(Path(effective_cwd)):
        rw.append(str(root))

    return _dedupe(ro), _dedupe(rw)


def _dedupe(paths: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def options_env(effective_cwd: Path, user: Optional[str] = None) -> Dict[str, str]:
    """The ``options.env`` entries that confine a spawn for *effective_cwd*.

    Empty when confinement is off. The effective mode decides the shape:

    * **landlock** — the read-only / read-write path lists the launcher enforces
      in place, plus the real CLI it execs.
    * **uid** — a signed single-use ticket carrying the per-user uid and the
      path lists, which the launcher presents to the broker; the broker (not the
      launcher) holds the real CLI and does the uid switch. The path lists do
      **not** go in the clear here — only inside the signed ticket.
    """
    mode = effective_mode()
    if mode not in _LANDLOCK_MODES:
        return {}
    ro, rw = _ro_rw_roots(Path(effective_cwd))

    if mode == "uid":
        from . import uid_isolation as ui

        # Key the uid on the user when known, else on the (per-run) workspace so
        # concurrent anonymous runs still get distinct uids. (Recycling dead
        # anonymous entries is a follow-up; the band is wide.)
        key = user or str(effective_cwd)
        uid = ui.resolve_uid(key)
        ticket = ui.mint_ticket(
            uid=uid,
            workspace=str(effective_cwd),
            ro=ro,
            rw=rw,
            env_allow=[],  # pass-through (gateway already scrubbed secrets)
        )
        return {
            MODE_ENV: "uid",
            TICKET_ENV: ticket,
            BROKER_SOCKET_ENV: broker_socket_path(),
        }

    env: Dict[str, str] = {
        MODE_ENV: "landlock",
        RO_ENV: os.pathsep.join(ro),
        RW_ENV: os.pathsep.join(rw),
    }
    real_cli = os.environ.get(REAL_CLI_ENV) or resolve_real_cli()
    if real_cli:
        env[REAL_CLI_ENV] = real_cli
    return env


def check_config() -> List[str]:
    """Fail-closed preconditions for confinement, as error message strings.

    An empty list means OK (or confinement is off). The caller wraps each in a
    ``ConfigIssue("error", ...)`` so the gateway refuses to start rather than
    spawn an unconfined or broken CLI.
    """
    requested = isolation_mode()
    if requested not in _KNOWN_MODES:
        return [
            f"{MODE_ENV}={requested!r} is not a valid isolation mode "
            f"(expected one of: {', '.join(sorted(_KNOWN_MODES))}). Refusing to "
            "start rather than run an unexpected (possibly unconfined) mode."
        ]
    mode = effective_mode()
    if mode not in _LANDLOCK_MODES:
        return []
    errors: List[str] = []

    # uid mode that could not be satisfied (and was not allowed to fall back to
    # landlock) is a hard startup error — never a silent drop.
    if requested == "uid" and mode == "uid" and not uid_mode_available():
        why = []
        if landlock_abi() < 2:
            why.append("Landlock ABI < 2 (kernel >= 5.19 needed)")
        if not broker_socket_path():
            why.append(f"{BROKER_SOCKET_ENV} is unset")
        elif not _broker_reachable():
            why.append("the broker is not reachable on its socket")
        if not _cap_bnd_has_setuid_setgid():
            why.append("SETUID/SETGID are not in the capability bounding set")
        errors.append(
            f"{MODE_ENV}=uid but uid isolation is unavailable here ("
            + "; ".join(why)
            + f"). Fix the broker/caps, or set {FALLBACK_ENV}=landlock to run the "
            "no-privilege Landlock boundary instead."
        )
        return errors
    if requested == "uid" and mode == "uid":
        from . import uid_isolation as ui

        if not (os.getenv(ui.TICKET_SECRET_ENV) or "").strip():
            errors.append(
                f"{MODE_ENV}=uid but {ui.TICKET_SECRET_ENV} is unset; the gateway "
                "cannot mint a broker ticket. The entrypoint must generate it and "
                "share it with the gateway and broker."
            )

    launcher = Path(launcher_path())
    if not (launcher.is_file() and os.access(launcher, os.X_OK)):
        errors.append(
            f"{MODE_ENV}={mode} but the launcher {str(launcher)!r} is missing or "
            f"not executable. Point {LAUNCHER_ENV} at the fixed, root-owned "
            "landlock_launcher.py (chmod +x)."
        )

    if resolve_real_cli() is None:
        errors.append(
            f"{MODE_ENV}={mode} but no real CLI resolves (operator CLAUDE_CLI_PATH "
            "is unset/not executable and the SDK bundled CLI was not found). The "
            "launcher would have nothing to exec."
        )

    abi = landlock_abi()
    if abi < 2:
        detail = (
            f"ABI {abi}" if abi >= 0 else "the syscall is unavailable on this arch"
        )
        errors.append(
            f"{MODE_ENV}={mode} but Landlock is not usable here ({detail}; need "
            "ABI >= 2, kernel >= 5.19). Confinement cannot be enforced."
        )

    return errors
