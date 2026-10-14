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
  shared part comes from the **same** helpers the always-on ``~/.claude`` guard
  uses. Read-write: the workspace, the ``projects/`` dir the CLI names after it
  (lossy: workspaces whose paths differ only in non-alphanumeric characters
  share one — a pre-existing gap tracked separately) and, when the session id
  is known, its own ``session-env/<session id>`` dir; the launcher adds
  ``/dev/shm``. Read-only: the shared assets a file tool may read
  (``plugins`` among them, so ``plugins/data`` too — each installed plugin's
  data dir is pre-created by the gateway), the CLI's own config files and the
  system roots. No shared cache (``~/.npm``, the uv cache) is writable; the
  launcher and broker point those at the session's tmpdir;
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
import logging
import math
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

# The launcher ships beside this repo's docker/ directory. In the image it is a
# fixed, root-owned path; an operator may override the location, but never make
# it agent-writable (the design requires a fixed, non-agent-writable launcher).
_DEFAULT_LAUNCHER = (
    Path(__file__).resolve().parents[3] / "docker" / "landlock_launcher.py"
)


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
    113: "the session uid cannot enter the workspace",
    114: "the Landlock ruleset could not be applied",
    115: "the switch to the session uid did not take",
    116: "the spawn's supervisor exited before the CLI started",
    117: "the confined CLI child failed during setup",
    118: (
        "the session uid cannot create <workspace>/.cli-tmp (the workspace is "
        "not writable by it)"
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
    chmod it (#244 review 4).
    """
    problems: List[str] = []
    for target in (launcher, launcher.parent / "_landlock_rules.py"):
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
    for directory in launcher.parents:
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


def _ro_rw_roots(
    effective_cwd: Path, session_id: Optional[str] = None
) -> Tuple[List[str], List[str]]:
    """Build the read-only and read-write path lists for *effective_cwd*.

    Reuses the guard's helpers so confinement and the file-tool guard grant the
    same shared set. The launcher adds the static system roots, the real CLI
    dir, ``/proc``, ``/dev`` entries and ``/dev/shm`` itself; here we add only
    what depends on the workspace, the session and the shared assets. Every
    entry is absolute (:func:`_absolute`).

    Two side effects, both as the gateway (unconfined) before the spawn: each
    installed plugin's data dir is pre-created (:func:`_ensure_plugin_data_dirs`)
    and so is this session's ``session-env`` dir (:func:`_session_env_dir`).
    """
    from . import workspace_sandbox as ws

    cwd = Path(_absolute(effective_cwd))
    claude_home = ws._claude_home()
    ro: List[str] = []
    rw: List[str] = []

    # Read-only shared assets: skills, plugins (plugins/data included), agents,
    # commands, output-styles, user CLAUDE.md, and the plugin clone roots the
    # CLI executes from.
    for root in ws._shared_asset_roots(claude_home):
        ro.append(str(root))
    for root in ws.plugin_resource_roots():
        ro.append(str(root))
    _ensure_plugin_data_dirs(claude_home)
    # The CLI's own config files (settings.json, the global .claude.json): the
    # confined CLI process reads them at startup to load user-scope plugins, the
    # admin env block and the output style. These are not file-tool targets
    # (the home guard never grants them), so they must be listed here
    # explicitly or the CLI silently starts with user settings dropped (#241
    # review). Shared, read-only — writes stay denied.
    for path in _claude_config_files(claude_home):
        ro.append(path)
    # Operator add-dirs are read-only (see #237/#240). Use the *filtered* roots
    # (the #240 check that refuses an entry overlapping ~/.claude or the
    # workspace base), not the raw CLAUDE_ADD_DIRS strings — otherwise a
    # confined script could read every user's transcripts/plans/workspaces.
    for root in ws._operator_add_dir_roots(claude_home, cwd):
        ro.append(str(root))

    # Read-write: the workspace itself, the ~/.claude project dir the CLI names
    # after it (transcripts, auto memory, spilled tool results) and this
    # session's session-env dir. No shared cache: ~/.npm and the uv cache stay
    # out (the launcher points npm_config_cache / UV_CACHE_DIR at the session
    # tmpdir), and so does plugins/data. Every session shares those, so a write
    # there is what another session's `npx`, `uv run` or plugin hook runs next.
    # Caveat (pre-existing, tracked separately): the CLI's project-dir name is
    # lossy — every non-alphanumeric character becomes "-" — so workspaces
    # whose paths differ only there (users a.b / a_b / a-b, alice@x.com /
    # alice.x.com) share one projects/ entry, and with it each other's
    # transcripts and memory.
    rw.append(str(cwd))
    for root in ws._writable_extra_roots(cwd):
        rw.append(str(root))
    env_dir = _session_env_dir(claude_home, session_id)
    if env_dir is not None:
        rw.append(str(env_dir))

    return _dedupe([_absolute(p) for p in ro]), _dedupe([_absolute(p) for p in rw])


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
        str(claude_home / "settings.json"),
        str(claude_home / "settings.local.json"),
        str(claude_home / ".config.json"),
        str(global_config_file()),
    ]


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
    so a plugin installed later is covered by its first session.
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
                (plugins_dir / "data" / name).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "Could not pre-create the data dir of plugin %s; its hooks "
                    "fail under confinement: %s",
                    plugin_id,
                    exc,
                )


# A session id becomes one path component: no separator, no dot-dot.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _session_env_dir(
    claude_home: Optional[Path], session_id: Optional[str]
) -> Optional[Path]:
    """This session's ``<config dir>/session-env/<session id>``, pre-created 0700.

    The CLI keeps the env files of SessionStart/Setup/CwdChanged/FileChanged
    hooks (``$CLAUDE_ENV_FILE``) there and sources them into every Bash call;
    a resumed session keeps its id, so it is the same dir. Under Landlock the
    CLI cannot create it and fails those hooks ("Hook failed to run
    (SessionStart:startup): EACCES ... mkdir session-env/<id>" on CLI 2.1.283).
    Only this session's entry is created and granted: its siblings are other
    sessions' env scripts, so ``session-env`` itself stays out of reach.
    ``None`` when the id is unknown (``verify()``, where the CLI picks its
    own), not a single safe path component, or a planted symlink.
    """
    if claude_home is None or not session_id or not _SESSION_ID_RE.match(session_id):
        return None
    path = claude_home / "session-env" / session_id
    try:
        if path.is_symlink():
            logger.warning("Not granting %s: it is a symlink", path)
            return None
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not pre-create %s; SessionStart hooks fail under " "confinement: %s",
            path,
            exc,
        )
        return None
    return path


def remove_session_env_dir(session_id: Optional[str]) -> None:
    """Remove a finished one-shot session's ``session-env/<session id>`` dir.

    :func:`_session_env_dir` creates one per confined spawn with a session id,
    and the CLI's own sweep of stale ones cannot reach them from inside
    Landlock, so every stateless run (``/v1/agents/messages``, an admin prompt
    try-run) would leave one behind. Their cleanup calls this once the CLI is
    dead; persistent ``/v1/responses`` sessions keep theirs (a resume reuses
    the id). Only that exact dir: the id is validated as at creation, a symlink
    in its place is left alone, and links inside it are removed, not followed.
    """
    from . import workspace_sandbox as ws

    claude_home = ws._claude_home()
    if claude_home is None or not session_id or not _SESSION_ID_RE.match(session_id):
        return
    path = claude_home / "session-env" / session_id
    try:
        if path.is_symlink() or not path.is_dir():
            return
        shutil.rmtree(path)
    except OSError:
        logger.debug("Could not remove %s", path, exc_info=True)


def _dedupe(paths: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _landlock_env(ro: List[str], rw: List[str]) -> Dict[str, str]:
    """The landlock-mode ``options.env``: the path lists plus the real CLI."""
    env: Dict[str, str] = {
        MODE_ENV: "landlock",
        RO_ENV: os.pathsep.join(ro),
        RW_ENV: os.pathsep.join(rw),
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
    grants that session's own ``session-env`` dir.
    """
    mode = effective_mode()
    if mode not in _LANDLOCK_MODES:
        return {}
    cwd = _absolute(effective_cwd)
    ro, rw = _ro_rw_roots(Path(cwd), session_id)

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
    # durable uid (persist=True); an anonymous/stateless run uses the
    # recycled in-memory ephemeral band (persist=False) so it cannot grow
    # the registry or exhaust the band (#241 review). The absolute path, so
    # a session and its preflight land on the same uid however the cwd was
    # spelled.
    key = user or cwd
    uid = ui.resolve_uid(key, persist=bool(user))
    ticket = ui.mint_ticket(
        uid=uid,
        workspace=cwd,
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
    user/plugin skill). No-op off.
    """
    if not confine_enabled():
        return {}
    if not gateway_owned:
        return options_env(effective_cwd, user=user)
    ro, rw = _ro_rw_roots(Path(_absolute(effective_cwd)))
    return _landlock_env(ro, rw)


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

    ok, why = can_enforce_landlock()
    if not ok:
        errors.append(
            f"{MODE_ENV}={mode} but Landlock cannot be enforced here ({why}; need "
            f"{_MIN_ABI_TEXT}). Confinement cannot be enforced."
        )

    return errors
