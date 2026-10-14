"""Gateway side of the Landlock confinement launcher (issue #241).

This wires the fixed launcher stub (``docker/landlock_launcher.py``) into the
CLI spawn path. When ``CLAUDE_ISOLATION_MODE`` selects a Landlock mode
(``landlock``, or ``uid``, which hands the spawn to the broker — see
``uid_isolation``):

* ``_get_cli_path()`` points ``cli_path`` at the launcher instead of the real
  CLI, so every Claude child (session turns, slash preflight, ``verify()``) is
  confined with a Landlock ruleset inherited across ``execve``;
* per spawn, the gateway hands the launcher the read-only and read-write path
  lists for that workspace through ``options.env``, every entry absolute. The
  shared part comes from the same shared-asset names and plugin roots the
  always-on ``~/.claude`` guard uses. Read-write: the workspace, the
  ``projects/`` dir the CLI names after it (lossy: workspaces whose paths
  differ only in non-alphanumeric characters share one — a pre-existing gap
  tracked separately) and, when the session id is known, its own
  ``session-env/<session id>`` and ``tasks/<session id>`` dirs; the launcher
  adds ``/dev/shm``. Read-only: the shared assets a file tool may read
  (``plugins`` among them, so ``plugins/data`` too — each installed plugin's
  data dir is pre-created by the gateway), the user rules, hook scripts and
  workflows the CLI reads itself, the CLI's own config files and the system
  roots. Every entry but the workspace passes one filter (``_GrantFilter``)
  and goes in resolved: no symlink or registry path can widen it to the config
  dir, ``$HOME``, the workspace base, ``/app``, the temp dir or ``/``. The
  workspace itself is checked before every spawn (``_confined_workspace``: no
  symlink below the workspace base) and goes first in the read-write list,
  resolved, as the kernel cwd the launcher requires. No shared cache
  (``~/.npm``, the uv cache) is writable; the launcher and broker point those
  at the session's tmpdir;
* the CLI's own OS sandbox is forced off, because bwrap cannot start inside a
  Landlock domain (see ``docs/security-process-isolation.md`` §3).

``landlock`` mode is the no-privilege interim of the design: it confines
filesystem access only; cross-session DAC and signal isolation come with
``uid`` mode. Fail-closed: if confinement is requested but the launcher or the
real CLI cannot be resolved, startup refuses rather than spawn an unconfined
CLI (``check_config``).
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from src.env_utils import parse_bool_env

logger = logging.getLogger(__name__)

MODE_ENV = "CLAUDE_ISOLATION_MODE"
FALLBACK_ENV = "CLAUDE_ISOLATION_FALLBACK"
BROKER_WAIT_ENV = "CLAUDE_ISOLATION_BROKER_WAIT_SECONDS"
LAUNCHER_ENV = "GATEWAY_LANDLOCK_LAUNCHER"
REAL_CLI_ENV = "GATEWAY_REAL_CLI"
RO_ENV = "GATEWAY_LANDLOCK_RO"
RW_ENV = "GATEWAY_LANDLOCK_RW"
BROKER_SOCKET_ENV = "GATEWAY_ISOLATION_BROKER_SOCKET"
TICKET_ENV = "GATEWAY_ISOLATION_TICKET"
_SKIP_VERSION_CHECK_ENV = "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"

# How long startup waits for the broker socket in uid mode (seconds). The
# entrypoint forks the broker and execs the gateway with no readiness handshake.
_DEFAULT_BROKER_WAIT_SECONDS = 10.0
_MAX_BROKER_WAIT_SECONDS = 300.0
_BROKER_POLL_SECONDS = 0.2

# The single operator knob for session isolation:
#   off      — no OS boundary (default)
#   landlock — Landlock ruleset, no privilege (ABI >= 3, kernel >= 6.2)
#   uid      — uid-per-user broker + Landlock (DAC + signal isolation too)
# Both landlock and uid apply the Landlock ruleset. An unknown value is rejected
# fail-closed in :func:`check_config`.
_LANDLOCK_MODES = {"landlock", "uid"}
_KNOWN_MODES = {"off", "landlock", "uid"}

# CAP_SETGID is bit 6, CAP_SETUID is bit 7 (linux/capability.h).
_CAP_SETGID_BIT = 1 << 6
_CAP_SETUID_BIT = 1 << 7

# The gateway's own tree: /app in the image — the code, and /app/data with the
# admin credentials, the usage log and the uid registry. Denied by rule absence,
# and never a grant (see _GrantFilter).
_APP_ROOT = Path(__file__).resolve().parents[3]

# The launcher ships beside this repo's docker/ directory. In the image it is a
# fixed, root-owned path; an operator may override the location, but never make
# it agent-writable (the design requires a fixed, non-agent-writable launcher).
_DEFAULT_LAUNCHER = _APP_ROOT / "docker" / "landlock_launcher.py"


_effective_mode_cache: Optional[str] = None

# What the uid-mode startup probe (:func:`_probe_uid_sessions`) found: ``None``
# when it has not run (tests, or a precondition ``check_config`` reports on its
# own), otherwise the reasons a session uid cannot run the CLI here — empty when
# the probe spawn worked.
_uid_probe_problems: Optional[List[str]] = None

# Exit statuses of a confined spawn that never reached the CLI
# (docker/landlock_launcher.py ``_die``; docker/isolation_broker.py ``_EXIT_*``).
_LAUNCHER_EXITS = {
    70: "the isolation launcher or broker refused the spawn",
    113: (
        "the session uid cannot enter the workspace, or the workspace resolves "
        "somewhere else than the ticket says"
    ),
    114: (
        "the Landlock ruleset could not be applied (or the real CLI's directory "
        "is / or holds the workspace)"
    ),
    115: "the switch to the session uid did not take",
    116: "the spawn's supervisor exited before the CLI started",
    117: "the confined CLI child failed during setup",
    118: (
        "<workspace>/.cli-tmp could not be made a usable directory as the "
        "session uid (most often: the workspace is not writable by it)"
    ),
    127: "the real CLI could not be executed",
}


def describe_launcher_exit(code: Optional[int]) -> Optional[str]:
    """What a confined spawn's exit status means, when it is the launcher's or
    the broker's own refusal rather than the CLI's; ``None`` otherwise."""
    if code is None:
        return None
    return _LAUNCHER_EXITS.get(code)


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
                    return bool(bits & _CAP_SETUID_BIT) and bool(bits & _CAP_SETGID_BIT)
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
    """Whether ``uid`` mode can actually run here: Landlock usable, the broker
    reachable (the broker being up means the entrypoint gave it the privilege
    to switch uid) and — once startup has probed it — a CLI spawned as a
    session uid actually started (:func:`_probe_uid_sessions`)."""
    return landlock_abi() >= MIN_LANDLOCK_ABI and _broker_reachable() and not _uid_probe_problems


def _broker_wait_seconds() -> float:
    """``CLAUDE_ISOLATION_BROKER_WAIT_SECONDS``: blank → default, negative → 0.

    Not a finite number (``x``, ``nan``, ``inf``, ``1e400``) → the default,
    with a warning: ``inf`` would wait forever and ``nan`` compare as no wait.
    Capped at ``_MAX_BROKER_WAIT_SECONDS``. ``docker/entrypoint.py`` parses the
    same knob identically (tests/test_entrypoint_broker.py pins the parity).
    """
    raw = (os.getenv(BROKER_WAIT_ENV) or "").strip()
    if not raw:
        return _DEFAULT_BROKER_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        logger.warning(
            "Ignoring %s=%r: not a finite number of seconds; using %s",
            BROKER_WAIT_ENV,
            raw,
            _DEFAULT_BROKER_WAIT_SECONDS,
        )
        return _DEFAULT_BROKER_WAIT_SECONDS
    if value > _MAX_BROKER_WAIT_SECONDS:
        logger.warning(
            "%s=%r is above the %s s cap; waiting %s s",
            BROKER_WAIT_ENV,
            raw,
            _MAX_BROKER_WAIT_SECONDS,
            _MAX_BROKER_WAIT_SECONDS,
        )
        return _MAX_BROKER_WAIT_SECONDS
    return max(0.0, value)


def _wait_for_broker(timeout: float) -> bool:
    """Poll the broker socket until it accepts or *timeout* seconds pass."""
    deadline = time.monotonic() + timeout
    while True:
        if _broker_reachable():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_BROKER_POLL_SECONDS, remaining))


def effective_mode() -> str:
    """The mode actually in force, after the fail-open-never fallback rule.

    ``uid`` stays ``uid`` when available. When it is not, it downgrades to
    ``landlock`` only if the operator explicitly set
    ``CLAUDE_ISOLATION_FALLBACK=landlock`` (with a warning); otherwise it stays
    ``uid`` and :func:`check_config` turns the unavailability into a startup
    error — never a silent drop to a weaker or absent boundary.

    Resolved once and cached (broker availability is fixed post-startup) and
    never blocks: :func:`install_process_env` waits for the broker first and
    re-resolves; :func:`reset_mode_cache` clears it for tests.
    """
    global _effective_mode_cache
    if _effective_mode_cache is not None:
        return _effective_mode_cache
    mode = isolation_mode()
    resolved = mode
    if mode == "uid" and not uid_mode_available():
        fallback = (os.getenv(FALLBACK_ENV) or "").strip().lower()
        if fallback == "landlock":
            why = "; ".join(_uid_probe_problems or ["Landlock ABI or broker"])
            logger.warning(
                "%s=uid is not available (%s); falling back to landlock per "
                "%s=landlock",
                MODE_ENV,
                why,
                FALLBACK_ENV,
            )
            resolved = "landlock"
    _effective_mode_cache = resolved
    return resolved


def reset_mode_cache() -> None:
    """Clear the memoized effective mode (tests, or a deliberate re-resolve).

    Keeps the startup probe's result: :func:`install_process_env` re-resolves
    the mode right after probing (:func:`reset_uid_probe` clears that, tests).
    """
    global _effective_mode_cache
    _effective_mode_cache = None


def reset_uid_probe() -> None:
    """Forget the uid-mode startup probe's result (tests)."""
    global _uid_probe_problems
    _uid_probe_problems = None


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

    In ``uid`` mode this is the one place that waits for the broker
    (``CLAUDE_ISOLATION_BROKER_WAIT_SECONDS``, default 10): the entrypoint
    starts it just before the gateway, and the availability resolved here is
    kept for the process lifetime, so an unlucky first connect would otherwise
    fall back to landlock (or refuse startup) for good. :func:`effective_mode`
    itself never blocks. Once the broker answers, one probe spawn checks that a
    session uid can actually run the CLI here (:func:`_probe_uid_sessions`).
    """
    if isolation_mode() == "uid" and broker_socket_path() and landlock_abi() >= MIN_LANDLOCK_ABI:
        timeout = _broker_wait_seconds()
        if not _wait_for_broker(timeout):
            logger.warning(
                "isolation broker not reachable on %s after %.1fs",
                broker_socket_path(),
                timeout,
            )
        else:
            _probe_uid_sessions()
        reset_mode_cache()
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


# How long the startup probe's spawn (a ``--version`` CLI start) may take.
_UID_PROBE_TIMEOUT_SECONDS = 30.0


def _probe_uid_sessions() -> None:
    """Spawn one CLI the way a uid-mode session would, once, at startup.

    A reachable broker says nothing about whether a session uid can work in
    this container's layout: with the shipped image every turn failed (the
    session uid cannot write the app-owned workspace) while startup looked
    healthy and every request got a 503. So startup runs the real spawn path —
    a fresh anonymous workspace from the workspace manager, the ticket and path
    lists a session gets, the launcher, the broker, the uid switch, Landlock —
    with ``--version`` (no network, no model), and checks that the session uid
    can reach the Claude config dir and create its transcript dir there. Any
    problem makes uid mode unavailable: a startup error, or the landlock
    fallback when the operator allowed one (:func:`effective_mode`).

    Skipped when the ticket secret, the uid bands or the registry are unusable:
    :func:`check_config` reports those with their own, clearer errors.
    """
    global _uid_probe_problems
    from . import uid_isolation as ui

    if not ui.ticket_secret() or ui.validate_bands() or ui.registry_errors():
        _uid_probe_problems = None
        return
    try:
        problems = _uid_session_problems()
    except Exception as exc:  # the probe itself must never crash startup
        problems = [f"the uid-mode startup probe could not run: {exc}"]
    for problem in problems:
        logger.error("uid isolation cannot run sessions here: %s", problem)
    _uid_probe_problems = problems


def _uid_session_problems() -> List[str]:
    """Why a uid-mode session cannot run here (empty list when it can)."""
    import subprocess

    from src.workspace_manager import workspace_manager

    from . import workspace_sandbox as ws
    from .client import child_secret_env

    workspace = workspace_manager.resolve(None)
    cwd = _absolute(workspace)
    try:
        ro, rw = _ro_rw_roots(Path(cwd))
        env, uid = _uid_spawn_env(cwd, ro, rw, user=None)
        # The broker gives the session the gid equal to its uid.
        problems = _session_dir_problems(uid, uid)
        try:
            result = subprocess.run(
                [launcher_path(), "--version"],
                cwd=cwd,
                env={**os.environ, **child_secret_env(), **env},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_UID_PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return [
                "a CLI spawned as a session uid did not finish `--version` within "
                f"{_UID_PROBE_TIMEOUT_SECONDS:.0f} s",
                *problems,
            ]
        if result.returncode != 0:
            meaning = describe_launcher_exit(result.returncode) or "the CLI failed"
            detail = _last_line(result.stderr)
            problems.insert(
                0,
                f"a CLI spawned as a session uid exited {result.returncode}: "
                f"{meaning}" + (f" ({detail})" if detail else ""),
            )
        return problems
    finally:
        workspace_manager.cleanup_temp_workspace(Path(cwd))
        # The probe workspace's projects/ entry, if the spawn created one.
        for root in ws._writable_extra_roots(Path(cwd)):
            try:
                os.rmdir(root)
            except OSError:
                pass


def _last_line(raw: bytes) -> str:
    lines = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines()]
    lines = [ln for ln in lines if ln]
    return lines[-1][:300] if lines else ""


def _dac_allows(path: Path, uid: int, gid: int, want: int) -> bool:
    """Whether mode bits give *uid*/*gid* (no supplementary groups — the broker
    drops them) the ``R_OK``/``W_OK``/``X_OK`` bits in *want* on *path*.
    POSIX ACLs are not evaluated."""
    st = os.stat(path)
    if st.st_uid == uid:
        bits = st.st_mode >> 6
    elif st.st_gid == gid:
        bits = st.st_mode >> 3
    else:
        bits = st.st_mode
    return bits & want == want


def _describe_mode(path: Path) -> str:
    st = os.stat(path)
    return f"{path} is mode {st.st_mode & 0o7777:04o}, owner uid {st.st_uid}"


def _first_unsearchable(path: Path, uid: int, gid: int) -> Optional[str]:
    """The first directory on the way to *path* (itself included) the session
    uid may not search, described; ``None`` when every one is searchable or the
    path does not exist."""
    walks = [path, Path(os.path.realpath(path))]
    for target in walks:
        for step in [*reversed(target.parents), target]:
            try:
                if not _dac_allows(step, uid, gid, os.X_OK):
                    return _describe_mode(step)
            except OSError:
                return None
    return None


def _session_dir_problems(uid: int, gid: int) -> List[str]:
    """Whether a session uid can reach the Claude config dir and create its
    ``projects/<name>`` transcript dir there (mode bits only)."""
    from . import workspace_sandbox as ws

    claude_home = ws._claude_home()
    if claude_home is None or not claude_home.exists():
        return []
    blocked = _first_unsearchable(claude_home, uid, gid)
    if blocked is not None:
        return [
            f"a session uid cannot reach the Claude config dir {claude_home} "
            f"({blocked}), so sessions would run without the user settings, "
            "skills and plugins and could not keep transcripts"
        ]
    projects = claude_home / "projects"
    target = projects if projects.is_dir() else claude_home
    try:
        writable = _dac_allows(target, uid, gid, os.W_OK | os.X_OK)
    except OSError:
        return []
    if not writable:
        return [
            f"a session uid cannot create its transcript dir in {projects} "
            f"({_describe_mode(target)}), so /v1/responses sessions could not "
            "be resumed"
        ]
    return []


# The lowest Landlock ABI either mode accepts: ABI 3 (kernel >= 6.2) is the
# first that handles TRUNCATE, so below it a confined session could still
# truncate files outside the ruleset that DAC lets it write (#244 review 4).
# Mirrors ``docker/_landlock_rules.MIN_ABI`` (parity-tested).
MIN_LANDLOCK_ABI = 3
_MIN_ABI_TEXT = f"ABI >= {MIN_LANDLOCK_ABI}, kernel >= 6.2"


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


# A self-contained probe: build a trivial ruleset and ``restrict_self``, in a
# throwaway interpreter that confines itself and exits. Run as ``python -I -c``
# (not os.fork — the gateway is multi-threaded at startup and a forked child
# could deadlock on an inherited malloc/logging lock). Raw syscalls only.
_ENFORCE_PROBE = (
    "import ctypes,os,struct,sys\n"
    "a=int(sys.argv[1])\n"
    "h=(1<<16)-1 if a>=5 else (1<<15)-1 if a>=3 else (1<<14)-1\n"
    "L=ctypes.CDLL(None,use_errno=True)\n"
    "C=ctypes.c_long\n"
    "L.syscall.restype=C\n"  # addresses must be c_long, or 64-bit args truncate
    "attr=ctypes.create_string_buffer(struct.pack('=Q',h))\n"
    "rs=L.syscall(C(444),C(ctypes.addressof(attr)),C(8),C(0))\n"
    "sys.exit(2) if rs<0 else 0\n"
    "fd=os.open('/',os.O_PATH|os.O_CLOEXEC)\n"
    "buf=ctypes.create_string_buffer(struct.pack('=Qi',1<<2,fd))\n"
    "rc=L.syscall(C(445),C(rs),C(1),C(ctypes.addressof(buf)),C(0))\n"
    "os.close(fd)\n"
    "sys.exit(3) if rc!=0 else 0\n"
    "sys.exit(4) if L.prctl(38,1,0,0,0)!=0 else 0\n"
    "sys.exit(0 if L.syscall(C(446),C(rs),C(0))==0 else 5)\n"
)


def can_enforce_landlock() -> Tuple[bool, str]:
    """Whether Landlock can actually be *enforced* here, not merely queried.

    :func:`landlock_abi` is a version query; it can succeed while a seccomp
    filter blocks ``landlock_add_rule``/``landlock_restrict_self``, in which
    case every real spawn would die while startup looked healthy. Probe the
    whole path once in a throwaway interpreter that confines itself and exits,
    so startup fails closed instead of every turn failing (#241 review).
    """
    abi = landlock_abi()
    if abi < MIN_LANDLOCK_ABI:
        return False, (
            f"Landlock ABI {abi}"
            if abi >= 0
            else "the syscall is unavailable on " "this arch"
        )
    import subprocess
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _ENFORCE_PROBE, str(abi)],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Fail closed: an enforcement check that could not run proved nothing
        # (#244 review 4).
        return False, f"the Landlock enforcement probe could not run ({exc})"
    if result.returncode == 0:
        return True, ""
    return False, (
        "Landlock reports a usable ABI but a ruleset could not be created or "
        "enforced (a seccomp filter may block landlock_add_rule/restrict_self)"
    )


def isolation_mode_is_valid() -> bool:
    """Whether ``CLAUDE_ISOLATION_MODE`` is one of the known modes."""
    return isolation_mode() in _KNOWN_MODES


# Dev-checkout opt-out for :func:`launcher_write_problems`: a checkout's
# ``docker/landlock_launcher.py`` is owned by the developer, so with this set the
# violations are logged instead of refusing startup. Never set it in production.
ALLOW_UNSAFE_LAUNCHER_ENV = "CLAUDE_ISOLATION_ALLOW_UNSAFE_LAUNCHER"


def _describe_owner_mode(st: os.stat_result) -> str:
    return f"uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):04o}"


def launcher_write_problems(launcher: Path) -> List[str]:
    """Why a session could replace the launcher (empty when it cannot).

    The launcher is the boundary — it applies the ruleset / hands off to the
    broker — so whoever can rewrite it, or the ``_landlock_rules.py`` it loads
    from its own directory, or rename either away in any ancestor directory,
    removes confinement from the next spawn. Every one of those must be
    root-owned and not group/other-writable (a sticky directory such as /tmp
    stops others replacing a root-owned entry, so it is allowed). Owned by the
    gateway's or app's uid is a violation even with mode 0755: that owner can
    chmod it (#244 review 4). The session shell wrapper (``claude-session-bash``,
    which every confined Bash call runs) is held to the same rule when it is
    present; without it the CLI keeps its own shell. It sits beside the rules
    file's real path (``_landlock_rules.SESSION_SHELL``), and a linked rules
    file is loaded from its target, so that directory chain counts too.
    """
    problems: List[str] = []
    rules = launcher.parent / "_landlock_rules.py"
    rules_dir = Path(os.path.realpath(rules)).parent
    targets = [launcher, rules]
    shell = rules_dir / "claude-session-bash"
    if os.path.lexists(shell):
        targets.append(shell)
    for target in targets:
        try:
            st = target.stat()
        except OSError as exc:
            problems.append(f"{target} cannot be checked ({exc.strerror})")
            continue
        if st.st_uid != 0 or st.st_mode & 0o022:
            problems.append(
                f"{target} is not root-owned and non-writable "
                f"({_describe_owner_mode(st)})"
            )
    seen = set()
    for directory in [*launcher.parents, rules_dir, *rules_dir.parents]:
        if directory in seen:
            continue
        seen.add(directory)
        try:
            st = directory.stat()
        except OSError as exc:
            problems.append(f"{directory} cannot be checked ({exc.strerror})")
            continue
        writable = st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX
        if st.st_uid != 0 or writable:
            problems.append(
                f"the directory {directory} is not root-owned and non-writable "
                f"({_describe_owner_mode(st)})"
            )
    return problems


def _launcher_ownership_errors(launcher: Path) -> List[str]:
    """Startup errors for an agent-replaceable launcher (the dev opt-out turns
    them into a warning)."""
    problems = launcher_write_problems(launcher)
    if not problems:
        return []
    if parse_bool_env(ALLOW_UNSAFE_LAUNCHER_ENV):
        logger.warning(
            "isolation launcher %s could be replaced by a session (%s); allowed "
            "by %s — a dev checkout only, never production",
            launcher,
            "; ".join(problems),
            ALLOW_UNSAFE_LAUNCHER_ENV,
        )
        return []
    return [
        f"{MODE_ENV}={isolation_mode()} but the launcher could be replaced by a "
        "session: " + "; ".join(problems) + ". Ship it (and _landlock_rules.py) "
        "root-owned and non-writable outside /app, as the image does at "
        f"/usr/local/bin, or set {ALLOW_UNSAFE_LAUNCHER_ENV}=true in a dev checkout."
    ]


def _absolute(path) -> str:
    """*path* as an absolute, normalized string (no filesystem access).

    The launcher and broker resolve a relative entry against the CLI's cwd —
    the workspace — not the gateway's, so with ``USER_WORKSPACES_DIR=./...``
    a relative workspace granted ``<ws>/<ws>`` instead of itself (#241 review).
    Every entry and the workspace itself therefore leave here absolute.
    """
    return os.path.abspath(str(path))


# Landlock-only shared assets in the config dir. The CLI reads or runs them
# itself — user rules (``rules/*.md``), the scripts a user-settings hook runs
# (``hooks/``), user workflows — but they are not file-tool targets, so they
# stay out of ``workspace_sandbox._SHARED_CLAUDE_ASSETS``, which scopes the file
# tools too. Read + execute (``RO_RIGHTS`` carries EXECUTE: a hook script is
# exec'd), never write. Without them CLI 2.1.283 silently drops user rules, and
# a user hook script fails open (exit 126) or blocks every call (exit 2).
_LANDLOCK_ONLY_ASSETS = ("rules", "hooks", "workflows")

# The CLI's own config files in the config dir (see :func:`_claude_config_files`).
_CONFIG_FILES = ("settings.json", "settings.local.json", ".config.json")

# Per-session dirs the CLI keeps in the config dir, each granted read-write to
# its own session only: the hook env files (``session-env/<id>``, sourced into
# every Bash call) and the task list (``tasks/<id>``, which TaskCreate, TaskGet,
# TaskUpdate and TaskList keep on disk; CLI 2.1.283 names it after the session
# id). Their siblings belong to other sessions.
_SESSION_DIR_KINDS = ("session-env", "tasks")

# Outside the default ruleset on purpose, and never granted: runtime state (the
# broker's socket among it), devices, kernel interfaces, root's home and the
# persistent temp dir.
_PRIVATE_SYSTEM_ROOTS = ("/run", "/var/run", "/dev", "/sys", "/root", "/var/tmp")

# What the launcher and broker grant every confined session read + execute
# unconditionally (``docker/_landlock_rules.SYSTEM_RO``; kept equal by
# tests/test_landlock_grants.py). Gateway state under one of them is readable
# by every session whatever the grant filter decides (:func:`_system_ro_errors`).
_LAUNCHER_SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc")

# The shared assets that are files, not trees (``_SHARED_CLAUDE_ASSETS``).
_FILE_ASSETS = frozenset({"CLAUDE.md"})


def _config_dir_readable() -> Tuple[str, ...]:
    """What a confined CLI may read inside the config dir, by name.

    The file-tool guard's shared assets, the Landlock-only ones, the default
    plugin clone root (``claude_config_paths.plugin_clone_root``) and the CLI's
    config files: ``.claude.json`` is the global config when
    ``CLAUDE_CONFIG_DIR`` is set (or a ``~/.claude.json`` kept in the config
    volume through a symlink). A name only matches what really lies below it
    (the filter compares resolved paths): a symlinked ``plugin-marketplaces``
    grants nothing by its name, its target is judged where it lands. Everything
    else there is per-user or per-session state: ``projects/``, ``plans/``,
    ``session-env/``, ``tasks/``, ``history.jsonl``, ``.credentials.json``.
    """
    from . import workspace_sandbox as ws

    return (
        *ws._SHARED_CLAUDE_ASSETS,
        *_LANDLOCK_ONLY_ASSETS,
        "plugin-marketplaces",
        *_CONFIG_FILES,
        ".claude.json",
    )


def _real(path) -> str:
    """*path* absolute, with every symlink resolved (missing tails kept)."""
    return os.path.realpath(os.fspath(path))


def _beneath(path: str, root: str) -> bool:
    """Whether *path* is *root* or lies below it (both real and absolute)."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# Refusals already logged: the lists are rebuilt for every spawn, and one bad
# entry should not log on each of them. Bounded; a full set starts over.
_warned: Set[tuple] = set()
_WARNED_MAX = 1024


def _warn_once(key: tuple, msg: str, *args) -> None:
    """``logger.warning`` once per *key* per process."""
    if key in _warned:
        return
    if len(_warned) >= _WARNED_MAX:
        _warned.clear()
    _warned.add(key)
    logger.warning(msg, *args)


class WorkspaceRefused(RuntimeError):
    """A confined spawn's working directory is not one it may run in.

    Raised before the spawn by :func:`_confined_workspace`. It is not transient
    (an operator has to fix the layout), so ``/v1/responses`` answers a
    non-retryable 500 ``workspace_refused`` on both the session and the
    slash-preflight path, naming no path (the details are logged); a stateless
    run gets an ``api_error`` event.
    """


# Protected-root kinds in :class:`_GrantFilter`. Inside a "config" or "home"
# root that root's own rule decides; nothing inside a "deny" root is granted,
# and nothing inside the "temp" dir either, except what a config dir or $HOME
# strictly nested in it grants (tests and throwaway deployments keep HOME
# there). A "contain" root (the session's workspace) only refuses an entry that
# is it or contains it.
_DENY_KINDS = ("deny", "temp")


def _target_refusal(path: str, *, file: bool) -> Optional[str]:
    """Why the rule target *path* (resolved) is the wrong kind, or ``None``.

    A rule on a file grants its inode, and with it every other name the inode
    has: a ``CLAUDE.md`` hard-linked to another user's transcript gave every
    confined session that transcript, turns written later included (#244
    review). So a file target must be a regular file with a single link, and a
    target the CLI reads as a file (*file*: ``CLAUDE.md``, the config files)
    must not be a directory either. A missing target stays listed: the launcher
    skips what does not exist.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"it cannot be checked ({exc})"
    if stat.S_ISDIR(st.st_mode):
        return "it is a directory, not the file the CLI reads" if file else None
    if not stat.S_ISREG(st.st_mode):
        return "it is not a regular file or a directory"
    if st.st_nlink > 1:
        return (
            f"it is a hard-linked file ({st.st_nlink} names): a rule on it "
            "grants every other name too"
        )
    return None


class _GrantFilter:
    """The one bound on every gateway-built Landlock entry but the workspace.

    The ruleset grants what an entry *resolves to*: the launcher opens a rule
    target with ``O_PATH``, which follows symlinks, and a directory target keeps
    the directory rights. A symlinked shared asset (``~/.claude/agents -> .``),
    a symlinked config file or a broad registry or env path (a marketplace
    ``installLocation`` of ``/``, ``CLAUDE_PLUGIN_CLONE_ROOT=/data`` above the
    workspace base) therefore widened every confined session's read + execute
    grant to the config dir, ``$HOME``, ``/app`` or ``/`` (#241 review). A
    confined session cannot plant one; an unconfined writer can (off-mode
    history on the persisted config volume, a co-resident unconfined backend,
    an operator symlink).

    Each candidate is resolved and the *resolved* path is what goes in the list,
    so the launcher opens exactly the path checked here. It is refused —
    skipped, with a warning logged once per process — when the resolved path

    * is ``/``, or is or contains a protected root: the config dir, ``$HOME``,
      the workspace base, this session's workspace, the gateway app root, the
      temp dir or one of the private system roots;
    * lies inside the workspace base, the app root, the temp dir or one of
      ``/run``, ``/var/run``, ``/dev``, ``/sys``, ``/root``, ``/var/tmp``;
    * lies inside the config dir outside what a confined CLI may read
      (:func:`_config_dir_readable`). A read-write entry must be exactly the
      per-session directory it was built as, so a symlinked one is always
      refused — a planted ``projects/<name> -> /`` would be a read-write grant
      on ``/``;
    * lies inside ``$HOME`` outside the config dir, unless it is the CLI's
      global config file or inside the operator's ``CLAUDE_PLUGIN_CLONE_ROOT``
      (:meth:`_operator_clone_root`) — two exemptions that never open anything
      inside the workspace base, the app root or a private system root;
    * is a file with more than one name, or not a regular file or directory
      (:func:`_target_refusal`).

    A path inside several protected roots follows the innermost one, so a
    config dir or ``$HOME`` nested in the temp dir (as in tests) applies its own
    rule there; protected roots that are the same directory all apply, so the
    strictest wins (``USER_WORKSPACES_DIR=$HOME`` refuses everything in
    ``$HOME``). Anything else is granted, so a shared asset symlinked to
    ``/opt/shared-skills`` keeps working (``docs/security-process-isolation.md``
    requires a symlinked asset's target to stay reachable). This is a
    deny-list of the gateway's known shared state, not an allow-list: another
    secret an operator keeps elsewhere is not known here. ``$HOME=/`` makes
    every grant outside the config dir a ``$HOME`` path, so those are refused:
    a gateway that runs confined needs a real home directory.
    """

    def __init__(self, claude_home: Optional[Path], workspace: Optional[Path]) -> None:
        from src.claude_config_paths import _home, global_config_file
        from src.workspace_manager import workspace_manager

        def resolved(get) -> Optional[str]:
            try:
                value = get()
                return None if value is None else _real(value)
            except (OSError, RuntimeError, KeyError, ValueError):
                return None

        self._config = resolved(lambda: claude_home)
        self._base = resolved(lambda: workspace_manager.base_path)
        candidates = [
            ("config dir", self._config, "config"),
            ("$HOME", resolved(_home), "home"),
            ("workspace base", self._base, "deny"),
            ("gateway app root", resolved(lambda: _APP_ROOT), "deny"),
            ("temp dir", resolved(tempfile.gettempdir), "temp"),
            ("session workspace", resolved(lambda: workspace), "contain"),
        ]
        candidates += [(p, _real(p), "deny") for p in _PRIVATE_SYSTEM_ROOTS]
        self._roots = [(label, r, kind) for label, r, kind in candidates if r]
        self._readable = (
            [os.path.join(self._config, n) for n in _config_dir_readable()]
            if self._config
            else []
        )
        # The global config file where the CLI reads it — its directory
        # resolved, not the file: a symlinked one must not exempt its target.
        gc = os.path.abspath(global_config_file())
        self._global_config = os.path.join(
            _real(os.path.dirname(gc)), os.path.basename(gc)
        )
        self._clone_root = self._operator_clone_root()

    def _operator_clone_root(self) -> Optional[str]:
        """The operator's ``CLAUDE_PLUGIN_CLONE_ROOT``, resolved: the one anchor
        that exempts paths in ``$HOME`` outside the config dir.

        Only the env value, which the operator set, is trusted. The default
        clone root lies in the shared config dir, where it is just a name
        (:func:`_config_dir_readable`): anchoring on its *resolved* path let a
        planted ``plugin-marketplaces -> ~/.ssh`` put ``~/.ssh`` into every
        session's grant (#244 review). No anchor when the configured path is
        itself a symlink (then it is whatever was swapped in) or fails
        :meth:`_placement`.
        """
        from src.claude_config_paths import PLUGIN_CLONE_ROOT_ENV

        raw = (os.getenv(PLUGIN_CLONE_ROOT_ENV) or "").strip()
        if not raw:
            return None
        configured = _absolute(raw)
        if os.path.islink(configured):
            return None
        path = _real(configured)
        return path if self._placement(path) is None else None

    def _containing(self, path: str) -> List[Tuple[str, str, str]]:
        """The protected roots (not the session workspace) *path* lies in."""
        return [e for e in self._roots if e[2] != "contain" and _beneath(path, e[1])]

    def _innermost(self, path: str) -> List[Tuple[str, str, str]]:
        """The deepest protected roots *path* lies in: several when they are
        the same directory, and then every one of their rules applies."""
        inside = self._containing(path)
        if not inside:
            return []
        depth = max(len(root) for _label, root, _kind in inside)
        return [e for e in inside if len(e[1]) == depth]

    def _placement(self, path: str) -> Optional[str]:
        """Why *path* can never be granted, wherever it sits, or ``None``."""
        if path == os.sep:
            return "it is the filesystem root"
        for label, root, _kind in self._roots:
            if _beneath(root, path):
                verb = "is" if root == path else "contains"
                return f"it {verb} the {label} {root}"
        for label, root, kind in self._innermost(path):
            if kind in _DENY_KINDS:
                return f"it lies inside the {label} {root}"
        return None

    def _read_refusal(self, path: str) -> Optional[str]:
        """Why the read-only entry *path* (resolved) is refused, or ``None``."""
        reason = self._placement(path)
        if reason is not None:
            return reason
        for _label, root, kind in self._innermost(path):
            if kind == "config":
                if not any(_beneath(path, r) for r in self._readable):
                    return (
                        f"it lies inside the config dir {root} outside the shared "
                        "assets a confined CLI may read; other users' transcripts, "
                        "plans and session state live there"
                    )
                continue
            exempt = path == self._global_config or (
                self._clone_root is not None and _beneath(path, self._clone_root)
            )
            if not exempt:
                reason = f"it lies inside $HOME {root} outside the config dir"
                if root == os.sep:
                    reason += " ($HOME is /: run the gateway with a real home)"
                return reason
            outer = next((e for e in self._containing(path) if e[2] == "deny"), None)
            if outer is not None:
                label, outer_root, _kind = outer
                return f"it lies inside the {label} {outer_root}: no exemption there"
        return None

    def ro(self, candidate, *, file: bool = False) -> Optional[str]:
        """*candidate* resolved, or ``None`` (logged once) when it is refused.
        *file*: the CLI reads it as a file (``CLAUDE.md``, a config file)."""
        path = _real(candidate)
        reason = self._read_refusal(path) or _target_refusal(path, file=file)
        if reason is None:
            return path
        self._refuse("read-only", candidate, path, reason)
        return None

    def rw(self, expected: Path) -> Optional[str]:
        """The per-session dir *expected* when it resolves to exactly itself;
        ``None`` (logged once) when anything at or above it is a symlink, it is
        not inside the config dir, or something other than a directory sits
        there (a planted file or hard link would be a read-write file grant)."""
        want = os.path.abspath(expected)
        path = _real(want)
        if path != want:
            reason = (
                f"it resolves to {path} (a symlink at or above it); only the "
                "exact per-session path is granted read-write"
            )
        elif self._config is None or not _beneath(path, self._config):
            reason = "it lies outside the config dir"
        elif os.path.lexists(path) and not os.path.isdir(path):
            reason = "it is not a directory"
        else:
            reason = self._placement(path)
        if reason is None:
            return path
        self._refuse("read-write", expected, path, reason)
        return None

    def workspace_refusal(self, path: str, *, in_base: bool) -> Optional[str]:
        """Why the resolved working directory *path* may not be a confined
        spawn's (read-write) workspace, or ``None``.

        It may not be ``/`` or be or contain a protected root. *in_base*: it was
        checked as a workspace-manager workspace, and wherever the operator put
        the workspace base is fine for it. Otherwise (a gateway-owned dir, the
        client's own temp dir, a configured cwd) it may not lie inside the
        config dir, the workspace base, the app root or a private system root;
        inside ``$HOME`` or the temp dir is fine.
        """
        if path == os.sep:
            return "it is the filesystem root"
        for label, root, kind in self._roots:
            if kind != "contain" and _beneath(root, path):
                verb = "is" if root == path else "contains"
                return f"it {verb} the {label} {root}"
        for label, root, kind in self._containing(path):
            if kind in ("home", "temp"):
                continue
            if in_base and self._base is not None and _beneath(self._base, root):
                continue
            return f"it lies inside the {label} {root}"
        return None

    def program_dir_refusal(self, path: str) -> Optional[str]:
        """Why the resolved directory *path* of a program the launcher grants
        read + execute to every session (the real CLI's) is too broad, or
        ``None``: it may not be or contain ``/`` or a protected root, nor lie
        inside the workspace base, where a session could replace the binary
        every other session then runs. Inside ``$HOME`` is fine (the native
        installer keeps versions in ``~/.local/share/claude/versions``)."""
        if path == os.sep:
            return "it is the filesystem root"
        for label, root, kind in self._roots:
            if kind != "contain" and _beneath(root, path):
                verb = "is" if root == path else "contains"
                return f"it {verb} the {label} {root}"
        if self._base is not None and _beneath(path, self._base):
            return f"it lies inside the workspace base {self._base}"
        return None

    @staticmethod
    def _refuse(kind: str, candidate, path: str, reason: str) -> None:
        shown = os.fspath(candidate)
        if os.path.abspath(shown) != path:
            shown = f"{shown} (resolves to {path})"
        _warn_once(
            ("grant", kind, os.fspath(candidate), path),
            "Not granting %s %s to confined sessions: %s",
            kind,
            shown,
            reason,
        )


def _private_dir_refusal(path: str) -> Optional[str]:
    """Why *path* is not the gateway's own private dir, or ``None``: it must
    exist as a real directory — not a symlink — owned by the gateway's uid."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return f"it cannot be checked ({exc})"
    if not stat.S_ISDIR(st.st_mode):
        return "it is not a real directory (a symlink or a file)"
    if st.st_uid != os.geteuid():
        return f"it is owned by uid {st.st_uid}, not the gateway's"
    return None


def _confined_workspace(
    effective_cwd, claude_home: Optional[Path], *, gateway_owned: bool = False
) -> str:
    """The resolved working directory of a confined spawn; refuses an unsafe one.

    The launcher grants the CLI's kernel cwd read-write — the resolved path — so
    a planted symlink in place of a workspace (``<base>/alice/claude ->
    ~/.claude``, or ``-> <base>``) gave that user's sessions read-write on the
    whole config dir (other users' transcripts and session-env scripts) or on
    every workspace (#244 review). So before every confined spawn the cwd must
    be, by where it is:

    * inside the workspace base (a workspace-manager workspace): strictly
      inside, and resolving to exactly the same path below the resolved base —
      no symlink anywhere below the base;
    * *gateway_owned* (the slash preflight's private dir): an existing real
      directory, not a symlink, owned by the gateway's uid;
    * anywhere else (the client's own temp dir that ``verify()`` runs in, a
      configured cwd): not a symlink and, if it exists, a directory.

    The resolved path must then pass :meth:`_GrantFilter.workspace_refusal`. A
    cwd that does not exist yet is checked as far as it resolves: the spawn
    fails on it, and should something be planted there meanwhile, the launcher
    refuses a kernel cwd that is not exactly the first read-write entry.
    Returns the resolved path; raises :class:`WorkspaceRefused` (warned once).
    """
    from src.workspace_manager import workspace_manager

    lexical = _absolute(effective_cwd)
    path = _real(lexical)
    base_lexical = _absolute(workspace_manager.base_path)
    base = _real(base_lexical)
    under = None
    if not gateway_owned:
        spellings = (base_lexical, base)
        under = next(
            (b for b in spellings if lexical.startswith(b.rstrip(os.sep) + os.sep)),
            None,
        )
    if gateway_owned:
        reason = _private_dir_refusal(lexical)
    elif under is not None:
        expected = os.path.join(base, lexical[len(under.rstrip(os.sep)) + 1 :])
        reason = (
            None
            if path == expected
            else f"it resolves to {path}, not {expected}: a symlink below the "
            "workspace base"
        )
    elif os.path.islink(lexical):
        reason = "it is a symlink"
    elif os.path.lexists(lexical) and not os.path.isdir(lexical):
        reason = "it is not a directory"
    else:
        reason = None
    if reason is None:
        filter_ = _GrantFilter(claude_home, None)
        reason = filter_.workspace_refusal(path, in_base=under is not None)
    if reason is not None:
        _warn_once(
            ("workspace", lexical, reason),
            "Refusing a confined spawn in %s: %s",
            lexical,
            reason,
        )
        raise WorkspaceRefused(f"refusing a confined spawn in {lexical}: {reason}")
    return path


def _ro_rw_roots(
    effective_cwd: Path,
    session_id: Optional[str] = None,
    *,
    precreate: bool = True,
    gateway_owned: bool = False,
) -> Tuple[List[str], List[str]]:
    """Build the read-only and read-write path lists for *effective_cwd*.

    Uses the guard's shared-asset names and plugin roots so confinement and the
    file-tool guard grant the same shared set, plus what only the CLI itself
    reads (:data:`_LANDLOCK_ONLY_ASSETS`, its config files). The launcher adds
    the static system roots, the real CLI dir, ``/proc``, ``/dev`` entries and
    ``/dev/shm`` itself; here we add only what depends on the workspace, the
    session and the shared assets. Every entry except the workspace passes
    :class:`_GrantFilter` and goes in resolved; every entry is absolute. The
    first read-write entry is the workspace, resolved and checked by
    :func:`_confined_workspace` (*gateway_owned*: the slash preflight's private
    dir), which raises :class:`WorkspaceRefused` for one a confined spawn may
    not use; the launcher requires the CLI's kernel cwd to be exactly it.

    Side effects, as the gateway (unconfined) before the spawn: each installed
    plugin's data dir is pre-created (:func:`_ensure_plugin_data_dirs`) and,
    with *precreate* (landlock mode), so are this session's ``session-env`` and
    ``tasks`` dirs (:func:`_session_dir`). uid mode passes ``precreate=False``:
    a gateway-owned 0700 dir is unusable by the session uid, so the broker's
    child creates them as that uid.
    """
    from . import workspace_sandbox as ws

    claude_home = ws._claude_home()
    workspace = _confined_workspace(
        effective_cwd, claude_home, gateway_owned=gateway_owned
    )
    cwd = Path(workspace)
    grants = _GrantFilter(claude_home, cwd)
    ro: List[str] = []
    rw: List[str] = []

    def grant_ro(candidate, *, file: bool = False) -> None:
        path = grants.ro(candidate, file=file)
        if path is not None:
            ro.append(path)

    def grant_rw(expected: Path) -> None:
        path = grants.rw(expected)
        if path is not None:
            rw.append(path)

    # Read-only shared assets: skills, plugins (plugins/data included), agents,
    # commands, output-styles, user CLAUDE.md, then the user rules, hook scripts
    # and workflows only the CLI reads, and the plugin clone roots the CLI
    # executes from. Named unresolved, so a refused symlink is logged by name.
    if claude_home is not None:
        for name in (*ws._SHARED_CLAUDE_ASSETS, *_LANDLOCK_ONLY_ASSETS):
            grant_ro(claude_home / name, file=name in _FILE_ASSETS)
    for root in ws.plugin_resource_roots():
        grant_ro(root)
    _ensure_plugin_data_dirs(claude_home)
    # The CLI's own config files (settings.json, the global .claude.json): the
    # confined CLI process reads them at startup to load user-scope plugins, the
    # admin env block and the output style. These are not file-tool targets
    # (the home guard never grants them), so they must be listed here
    # explicitly or the CLI silently starts with user settings dropped (#241
    # review). Shared, read-only — writes stay denied.
    for path in _claude_config_files(claude_home):
        grant_ro(path, file=True)
    # Operator add-dirs are read-only (see #237/#240). Use the *filtered* roots
    # (the #240 check that refuses an entry overlapping ~/.claude or the
    # workspace base), not the raw CLAUDE_ADD_DIRS strings — otherwise a
    # confined script could read every user's transcripts/plans/workspaces.
    for root in ws._operator_add_dir_roots(claude_home, cwd):
        grant_ro(root)

    # Read-write: the workspace itself, the ~/.claude project dir the CLI names
    # after it (transcripts, auto memory, spilled tool results) and this
    # session's session-env and tasks dirs. No shared cache: ~/.npm and the uv
    # cache stay out (the launcher points npm_config_cache / UV_CACHE_DIR at the
    # session tmpdir), and so does plugins/data. Every session shares those, so
    # a write there is what another session's `npx`, `uv run` or plugin hook
    # runs next.
    # Caveat (pre-existing, tracked separately): the CLI's project-dir name is
    # lossy — every non-alphanumeric character becomes "-" — so workspaces
    # whose paths differ only there (users a.b / a_b / a-b, alice@x.com /
    # alice.x.com) share one projects/ entry, and with it each other's
    # transcripts and memory.
    rw.append(workspace)
    for path in _own_project_dirs(cwd, claude_home):
        grant_rw(path)
    for kind in _SESSION_DIR_KINDS:
        path = _session_dir(claude_home, kind, session_id, create=precreate)
        if path is not None:
            grant_rw(path)

    return _dedupe([_absolute(p) for p in ro]), _dedupe([_absolute(p) for p in rw])


def _own_project_dirs(cwd: Path, claude_home: Optional[Path]) -> List[Path]:
    """This workspace's ``projects/<name>``, unresolved: one entry.

    *cwd* is the resolved workspace. The CLI names the dir after its kernel
    cwd, not the ``PWD`` the SDK sets (measured on CLI 2.1.283 with a symlinked
    cwd), so only that name is granted — a second entry for the lexical
    spelling was another session's dir as soon as the two differed. Unresolved:
    ``workspace_sandbox._own_project_roots`` resolves its entries, so a planted
    ``projects/<name> -> /`` came back as ``/``; :meth:`_GrantFilter.rw`
    refuses such an entry instead.
    """
    from . import workspace_sandbox as ws

    if claude_home is None:
        return []
    return [claude_home / "projects" / ws._project_dir_name(str(cwd))]


def _claude_config_files(claude_home: Optional[Path]) -> List[str]:
    """The CLI's own config files the confined process must be able to read.

    ``settings.json`` / ``settings.local.json`` in the config dir and the
    global config. CLI 2.1.283 reads the global config from the legacy
    ``<config dir>/.config.json`` when that file exists, else from
    ``join(CLAUDE_CONFIG_DIR || homedir(), ".claude.json")`` — which
    :func:`~src.claude_config_paths.global_config_file` mirrors. Not
    ``<config dir>.parent``: that misses the file under ``CLAUDE_CONFIG_DIR``
    or a symlinked ``~/.claude`` and grants an unrelated one (#241 review).
    """
    if claude_home is None:
        return []
    from src.claude_config_paths import global_config_file

    return [
        *(str(claude_home / name) for name in _CONFIG_FILES),
        str(global_config_file()),
    ]


def _make_dirs_beneath(base: Path, parts: Sequence[str], mode: int = 0o777) -> None:
    """``mkdir -p base/parts...`` that never follows a symlink below *base*.

    The gateway runs this unconfined, on paths in the shared config dir that an
    unconfined writer may have replaced with a symlink. Each component is made
    if missing (the last with *mode*, the others with the default) and opened
    ``O_NOFOLLOW | O_DIRECTORY`` before the next is made inside it, so a planted
    link — or a non-directory — fails the call (``ELOOP``/``ENOTDIR``) instead of
    creating dirs wherever it points. Raises ``OSError``.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    fd = os.open(base, flags)
    try:
        for i, part in enumerate(parts):
            try:
                os.mkdir(part, mode if i == len(parts) - 1 else 0o777, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, flags | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


# CLI 2.1.283 keeps a plugin's ``${CLAUDE_PLUGIN_DATA}`` at
# ``<config dir>/plugins/data/<name>``: the plugin id with every character
# outside ``[A-Za-z0-9_-]`` replaced by ``-`` (``bX``/``lct``); an
# ``<npm package>@npm`` id uses the package name with every character outside
# ``[a-z0-9-]`` hex-escaped, plus ``-npm`` (``hHr``).
_PLUGIN_DATA_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_NPM_DATA_UNSAFE = re.compile(r"[^a-z0-9-]")


def _npm_escape(match: "re.Match[str]") -> str:
    code = ord(match.group(0))
    return f"_{code:02x}" if code <= 255 else f"__{code:x}_"


def _plugin_data_names(plugin_id: str) -> List[str]:
    """The data dir name(s) the CLI may use for *plugin_id*."""
    names = [_PLUGIN_DATA_UNSAFE.sub("-", plugin_id)]
    if plugin_id.endswith("@npm"):
        package = plugin_id[: plugin_id.rindex("@")]
        names.append(_NPM_DATA_UNSAFE.sub(_npm_escape, package) + "-npm")
    return [n for n in names if n.strip("-")]


def _ensure_plugin_data_dirs(claude_home: Optional[Path]) -> None:
    """Pre-create each installed plugin's data dir, which sessions only read.

    ``plugins/data`` is read-only to a confined session (see
    :func:`_ro_rw_roots`), but the CLI ``mkdirSync``s a plugin's
    ``${CLAUDE_PLUGIN_DATA}`` before running each of its hooks and fails the
    hook when that throws: "Hook failed to run ... EACCES ... mkdir
    plugins/data" on CLI 2.1.283. The gateway, unconfined, creates the dirs
    first, so the CLI's recursive mkdir sees them and the hook runs. A hook that
    writes into its data dir then fails visibly with EACCES. Reads the same
    registry as :func:`workspace_sandbox.plugin_resource_roots`, every spawn,
    so a plugin installed later is covered by its first session. Never through
    a symlinked ``plugins`` or ``plugins/data`` (:func:`_make_dirs_beneath`):
    the gateway would create dirs wherever a planted link points.
    """
    if claude_home is None:
        return
    from .workspace_sandbox import _read_json

    plugins_dir = claude_home / "plugins"
    installed = _read_json(plugins_dir / "installed_plugins.json")
    plugins = installed.get("plugins") if isinstance(installed, dict) else None
    if not isinstance(plugins, dict):
        return
    for plugin_id in plugins:
        if not isinstance(plugin_id, str):
            continue
        for name in _plugin_data_names(plugin_id):
            try:
                _make_dirs_beneath(claude_home, ("plugins", "data", name))
            except OSError as exc:
                _warn_once(
                    ("plugin-data", plugin_id, name, exc.errno),
                    "Could not pre-create the data dir of plugin %s; its hooks "
                    "fail under confinement: %s",
                    plugin_id,
                    exc,
                )


# A session id becomes one path component: no separator, no dot-dot.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_SESSION_DIR_USE = {
    "session-env": "SessionStart hooks fail",
    "tasks": "TaskCreate fails",
}


def _session_dir(
    claude_home: Optional[Path],
    kind: str,
    session_id: Optional[str],
    *,
    create: bool = True,
) -> Optional[Path]:
    """This session's ``<config dir>/<kind>/<session id>``, or ``None``.

    *kind* is one of :data:`_SESSION_DIR_KINDS`. The CLI keeps the env files of
    SessionStart/Setup/CwdChanged/FileChanged hooks (``$CLAUDE_ENV_FILE``) in
    ``session-env/<id>`` and sources them into every Bash call, and its task
    list in ``tasks/<id>``; a resumed session keeps its id, so it is the same
    dir. Under Landlock the CLI cannot create either ("Hook failed to run
    (SessionStart:startup): EACCES ... mkdir session-env/<id>"; TaskCreate
    failing with "ENOENT ... realpath tasks/<id>/.lock" on CLI 2.1.283), so with
    *create* the gateway makes it 0700 first (:func:`_make_dirs_beneath`,
    parents included). Without (uid mode) it is only checked: the broker's
    child creates it as the session uid. Only this session's entry is granted:
    its siblings are other sessions' env scripts and task lists, so the parent
    stays out of reach. ``None`` when the id is unknown (``verify()``, where the
    CLI picks its own), not a single safe path component, or a planted symlink
    or non-directory (at it or above it).
    """
    if claude_home is None or not session_id or not _SESSION_ID_RE.match(session_id):
        return None
    path = claude_home / kind / session_id
    try:
        if create:
            _make_dirs_beneath(claude_home, (kind, session_id), 0o700)
        elif os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
            raise NotADirectoryError(f"{path} is a symlink or not a directory")
    except OSError as exc:
        _warn_once(
            ("session-dir", kind, exc.errno),
            "Not granting %s to confined sessions; %s there: %s",
            path,
            _SESSION_DIR_USE.get(kind, "the CLI fails"),
            exc,
        )
        return None
    return path


def remove_session_dirs(session_id: Optional[str]) -> None:
    """Remove a finished session's per-session dirs in the config dir.

    Its ``session-env/<session id>`` dir always, and under confinement its
    ``tasks/<session id>`` dir too: :func:`_session_dir` creates both per
    confined spawn with a session id, and the CLI's own sweep of stale ones
    cannot reach them from inside Landlock, so every session — every stateless
    run (``/v1/agents/messages``, an admin prompt try-run) above all — would
    leave them behind. Called once the CLI is dead and the session is
    finished: by the stateless cleanups, and when a ``/v1/responses`` session
    is deleted or an anonymous one is torn down. A named session torn down by
    expiry, eviction or shutdown keeps both, since a continuation resumes the
    same id (``SessionManager._release_session_dirs``). With confinement off
    the task list stays the CLI's own, as before. Only
    those exact dirs: the id is validated as at creation, nothing is removed
    through a symlink in its place or above it, and links inside it are
    removed, not followed.
    """
    from . import workspace_sandbox as ws

    claude_home = ws._claude_home()
    if claude_home is None or not session_id or not _SESSION_ID_RE.match(session_id):
        return
    kinds = _SESSION_DIR_KINDS if confine_enabled() else ("session-env",)
    for kind in kinds:
        path = claude_home / kind / session_id
        try:
            if _real(path) != str(path) or not path.is_dir():
                continue
            shutil.rmtree(path)
        except OSError:
            logger.debug("Could not remove %s", path, exc_info=True)


# The original name, which the session and stateless cleanups call.
remove_session_env_dir = remove_session_dirs


def _dedupe(paths: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _landlock_env(ro: List[str], rw: List[str]) -> Dict[str, str]:
    """The landlock-mode ``options.env``: the path lists plus the real CLI.

    Each list is a JSON array (the launcher's ``_parse_paths``), never a
    ``:``-joined string: a granted path may contain ``:`` — an admin-added
    marketplace cloned from ``https://git.internal:8443``, a workspace base or
    config dir with one — and splitting on it made every confined spawn exit 70
    on a relative fragment, or turned ``.../x:/secret`` into a grant of
    ``/secret`` (#244 review).
    """
    env: Dict[str, str] = {
        MODE_ENV: "landlock",
        RO_ENV: json.dumps(ro),
        RW_ENV: json.dumps(rw),
    }
    real_cli = os.environ.get(REAL_CLI_ENV) or resolve_real_cli()
    if real_cli:
        env[REAL_CLI_ENV] = real_cli
    return env


def options_env(
    effective_cwd: Path,
    user: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, str]:
    """The ``options.env`` entries that confine a spawn for *effective_cwd*.

    Empty when confinement is off. The effective mode decides the shape:

    * **landlock** — the read-only / read-write path lists the launcher enforces
      in place, plus the real CLI it execs.
    * **uid** — a signed single-use ticket carrying the per-user uid and the
      path lists, which the launcher presents to the broker; the broker (not the
      launcher) holds the real CLI and does the uid switch. The path lists do
      **not** go in the clear here — only inside the signed ticket. The ticket
      expires two minutes after it is minted, so a caller that awaits before
      spawning calls this again right before (``create_client``).

    ``session_id`` (the CLI's session id: ``--session-id`` or ``--resume``)
    grants that session's own ``session-env`` and ``tasks`` dirs. landlock mode
    pre-creates them as the gateway; uid mode leaves that to the broker's child,
    which runs as the session uid (a gateway-owned 0700 dir is unusable to it).

    Raises :class:`WorkspaceRefused` for a working directory a confined spawn
    may not run in (:func:`_confined_workspace`).
    """
    mode = effective_mode()
    if mode not in _LANDLOCK_MODES:
        return {}
    cwd = _absolute(effective_cwd)
    ro, rw = _ro_rw_roots(Path(cwd), session_id, precreate=mode != "uid")

    if mode == "uid":
        return _uid_spawn_env(cwd, ro, rw, user)[0]

    return _landlock_env(ro, rw)


def _uid_spawn_env(
    cwd: str, ro: List[str], rw: List[str], user: Optional[str]
) -> Tuple[Dict[str, str], int]:
    """The uid-mode ``options.env`` for a spawn in *cwd*, and the uid it runs as."""
    from . import uid_isolation as ui

    # Key the uid on the user when known, else on the (per-run) workspace so
    # concurrent anonymous runs still get distinct uids. A named user gets a
    # durable uid (persist=True); an anonymous/stateless run leases one from
    # the in-memory ephemeral band (persist=False) until its workspace is
    # cleaned up, so it cannot grow the registry; with every ephemeral uid
    # leased the spawn is refused, retryably (#241 review). The absolute path,
    # so a session and its preflight land on the same lease however the cwd
    # was spelled — and the workspace cleanup releases it under that key.
    key = user or cwd
    uid = ui.resolve_uid(key, persist=bool(user))
    ticket = ui.mint_ticket(
        uid=uid,
        # The resolved workspace (the first rw entry, see _ro_rw_roots): the
        # broker grants it and starts the CLI there.
        workspace=rw[0] if rw else cwd,
        ro=ro,
        rw=rw,
        env_allow=[],  # pass-through (gateway already scrubbed secrets)
    )
    env = {
        MODE_ENV: "uid",
        TICKET_ENV: ticket,
        BROKER_SOCKET_ENV: broker_socket_path(),
    }
    return env, uid


def preflight_options_env(
    effective_cwd: Path,
    user: Optional[str] = None,
    *,
    gateway_owned: bool = False,
) -> Dict[str, str]:
    """Confinement env for an internal catalog / slash-preflight spawn.

    The slash-command preflight and catalog run a throwaway CLI only to read the
    registered command list. In a session workspace that CLI still loads the
    workspace's project settings, skills and commands, so it is confined as the
    session would be (:func:`options_env`, same uid key): in uid mode it goes
    through the broker as that session's uid, not the gateway uid — the
    workspace's hooks and ``.mcp.json`` servers ran as the gateway uid before
    (#241 review; the preflight also disables both, see ``slash_commands``). A
    preflight with no workspace runs in a private gateway-owned dir
    (``gateway_owned=True``): there is no session uid to run it as, so it stays
    landlock-shaped in every Landlock mode — with the ro/rw lists, so the
    confined CLI can read ``~/.claude`` (without them the catalog loses every
    user/plugin skill). So does the uid-mode preflight of a named user who has
    no uid yet, in that user's workspace: it never allocates one. No-op off.
    """
    if not confine_enabled():
        return {}
    landlock_shaped = gateway_owned
    if not gateway_owned and user and effective_mode() == "uid":
        from . import uid_isolation as ui

        # A uid is durable and never reclaimed, and the preflight runs before
        # the request is validated: every distinct ``user`` of a request then
        # refused with 400 (an unknown /command) took one for good, until the
        # band was full (#241 review). Running this one spawn as the gateway uid
        # instead is acceptable: the preflight runs with every hook, MCP server
        # and startup helper command disabled in every mode
        # (``slash_commands._build_preflight_options``), and landlock mode runs
        # every preflight that way already. The session's own spawn
        # (``create_client``) allocates the uid; a held registry entry still
        # refuses here, as it would there.
        if ui.existing_uid(user) is None:
            landlock_shaped = True
    if not landlock_shaped:
        return options_env(effective_cwd, user=user)
    # gateway_owned, not landlock_shaped: a user's workspace is checked as one.
    ro, rw = _ro_rw_roots(Path(_absolute(effective_cwd)), gateway_owned=gateway_owned)
    return _landlock_env(ro, rw)


def _system_ro_errors(mode: str) -> List[str]:
    """Startup errors for gateway state under a root every session can read.

    The launcher and broker grant the system roots (:data:`_LAUNCHER_SYSTEM_RO`)
    read + execute to every confined session, before and regardless of the
    gateway's lists. A workspace base, config dir, ``$HOME``, app root, temp
    dir or uid registry under one of them — ``USER_WORKSPACES_DIR=/opt/ws``, a
    checkout in ``/opt/oh-my-gateway`` — is therefore readable by every
    session: other users' workspaces and transcripts, the gateway's ``.env``
    and ``data/`` (#244 review). Refuse to start instead. So does a system root
    that resolves to ``/``: the launcher follows that link and would grant all
    of ``/``.
    """
    from src.workspace_manager import workspace_manager

    from . import uid_isolation as ui
    from . import workspace_sandbox as ws

    candidates = [
        ("the workspace base", workspace_manager.base_path),
        ("the Claude config dir", ws._claude_home()),
        # Unset HOME: the passwd home the CLI's os.homedir() falls back to.
        ("$HOME", os.environ.get("HOME") or str(Path.home())),
        ("the gateway's app root", _APP_ROOT),
        ("the temp dir", tempfile.gettempdir()),
    ]
    if mode == "uid":
        candidates.append(("the uid registry", ui._registry_path()))
    roots = [(root, _real(root)) for root in _LAUNCHER_SYSTEM_RO]
    errors: List[str] = [
        f"{MODE_ENV}={mode} but the system root {root} resolves to /, and the "
        "launcher would grant all of / read-only to every confined session."
        for root, real_root in roots
        if real_root == "/"
    ]
    for label, path in candidates:
        if not path:
            continue
        real = _real(path)
        for root, real_root in roots:
            if real_root != "/" and _beneath(real, real_root):
                errors.append(
                    f"{MODE_ENV}={mode} but {label} {real} lies under {root}, "
                    "which the launcher grants read-only to every confined "
                    "session, so every session could read it. Move it out of "
                    f"{', '.join(_LAUNCHER_SYSTEM_RO)}."
                )
                break
    return errors


def _real_cli_dir_errors(mode: str) -> List[str]:
    """Startup errors for a real CLI whose directory is too broad to grant.

    The launcher grants ``dirname(realpath(GATEWAY_REAL_CLI))`` read + execute
    to every confined session, so an operator ``CLAUDE_CLI_PATH=$HOME/claude``
    or ``/app/claude`` granted all of ``$HOME`` (every user's transcripts, the
    credentials) or ``/app`` (#244 review). Checks the value the launcher gets
    — the process env's, else :func:`resolve_real_cli`'s — against
    :meth:`_GrantFilter.program_dir_refusal`.
    """
    from . import workspace_sandbox as ws

    candidates = {os.environ.get(REAL_CLI_ENV) or "", resolve_real_cli() or ""}
    grants = _GrantFilter(ws._claude_home(), None)
    errors: List[str] = []
    for real_cli in sorted(c for c in candidates if c):
        cli_dir = os.path.dirname(_real(real_cli))
        reason = grants.program_dir_refusal(cli_dir)
        if reason is not None:
            errors.append(
                f"{MODE_ENV}={mode} but the real CLI {real_cli!r} is in {cli_dir} "
                f"({reason}), and the launcher grants that whole directory read + "
                "execute to every confined session. Install the CLI in a "
                "directory of its own (the native installer's "
                "~/.local/share/claude/versions/<version> is fine) and point "
                "CLAUDE_CLI_PATH at it."
            )
    return errors


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
        if _uid_probe_problems:
            errors.append(
                f"{MODE_ENV}=uid but a session uid cannot run the CLI here: "
                + "; ".join(_uid_probe_problems)
                + ". uid mode needs per-user workspace ownership and container "
                "provisioning this image does not provide yet (see "
                "docs/isolation-uid-verification.md); set "
                f"{FALLBACK_ENV}=landlock to run the Landlock boundary instead."
            )
            return errors
        why = []
        if landlock_abi() < MIN_LANDLOCK_ABI:
            why.append(f"Landlock ABI < {MIN_LANDLOCK_ABI} ({_MIN_ABI_TEXT} needed)")
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

        # ticket_secret(), not the env: startup moves the secret out of
        # os.environ into memory before this check runs.
        if not ui.ticket_secret():
            errors.append(
                f"{MODE_ENV}=uid but {ui.TICKET_SECRET_ENV} is unset; the gateway "
                "cannot mint a broker ticket. The entrypoint must generate it and "
                "share it with the gateway and broker."
            )
        # The per-user uid band must be usable and must not force a session onto
        # root, the gateway's own uid or the broker's uid.
        errors.extend(ui.validate_bands())

    launcher = Path(launcher_path())
    if not (launcher.is_file() and os.access(launcher, os.X_OK)):
        errors.append(
            f"{MODE_ENV}={mode} but the launcher {str(launcher)!r} is missing or "
            f"not executable. Point {LAUNCHER_ENV} at the fixed, root-owned "
            "landlock_launcher.py (chmod +x)."
        )
    else:
        errors.extend(_launcher_ownership_errors(launcher.resolve()))

    if resolve_real_cli() is None:
        errors.append(
            f"{MODE_ENV}={mode} but no real CLI resolves (operator CLAUDE_CLI_PATH "
            "is unset/not executable and the SDK bundled CLI was not found). The "
            "launcher would have nothing to exec."
        )
    errors.extend(_real_cli_dir_errors(mode))
    errors.extend(_system_ro_errors(mode))

    ok, why = can_enforce_landlock()
    if not ok:
        errors.append(
            f"{MODE_ENV}={mode} but Landlock cannot be enforced here ({why}; need "
            f"{_MIN_ABI_TEXT}). Confinement cannot be enforced."
        )

    return errors
