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
    if abi < 2:
        return False, (
            f"Landlock ABI {abi}" if abi >= 0 else "the syscall is unavailable on "
            "this arch"
        )
    import subprocess
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _ENFORCE_PROBE, str(abi)],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return True, f"could not run a Landlock probe ({exc})"
    if result.returncode == 0:
        return True, ""
    return False, (
        "Landlock reports a usable ABI but a ruleset could not be created or "
        "enforced (a seccomp filter may block landlock_add_rule/restrict_self)"
    )


def isolation_mode_is_valid() -> bool:
    """Whether ``CLAUDE_ISOLATION_MODE`` is one of the known modes."""
    return isolation_mode() in _KNOWN_MODES


def _warn_if_launcher_agent_writable(launcher: Path) -> None:
    """Warn (not fail) if the launcher is agent-writable.

    The launcher is privileged — it applies the ruleset / hands off to the
    broker — so an agent-writable copy (group/other-writable, or owned by the
    app uid, e.g. a ``docker/`` tree copied under ``/app`` and chowned to
    ``app``) would let a session rewrite it. ``check_config`` cannot hard-fail
    here (a dev checkout runs as a normal user that owns the file), so it warns.
    """
    try:
        st = launcher.stat()
    except OSError:
        return
    if st.st_mode & 0o022:
        logger.warning(
            "isolation launcher %s is group/other-writable; ship it root-owned "
            "and non-agent-writable (outside /app)",
            launcher,
        )
    raw = (os.getenv("APP_UID") or "").strip()
    if raw.isdigit() and st.st_uid == int(raw):
        logger.warning(
            "isolation launcher %s is owned by the app uid %s; a session could "
            "rewrite it — ship it root-owned outside /app",
            launcher,
            raw,
        )


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
    # The CLI's own config files (settings.json, ~/.claude.json): the confined
    # CLI process reads them at startup to load user-scope plugins, the admin
    # env block and the output style. These are not file-tool targets (the home
    # guard never grants them), so they must be listed here explicitly or the
    # CLI silently starts with user settings dropped (#241 review). Shared,
    # read-only — writes stay denied.
    for path in _claude_config_files(claude_home):
        ro.append(path)
    # Operator add-dirs are read-only (see #237/#240). Use the *filtered* roots
    # (the #240 check that refuses an entry overlapping ~/.claude or the
    # workspace base), not the raw CLAUDE_ADD_DIRS strings — otherwise a
    # confined script could read every user's transcripts/plans/workspaces.
    for root in ws._operator_add_dir_roots(claude_home, Path(effective_cwd)):
        ro.append(str(root))

    # Read-write: the workspace itself and this session's own ~/.claude project
    # dir (transcripts, auto memory, spilled tool results).
    rw.append(str(effective_cwd))
    for root in ws._writable_extra_roots(Path(effective_cwd)):
        rw.append(str(root))
    # Writable runtime state the CLI and its tools need, or plugin hooks fail
    # open (reporting success while not running), npx-launched MCP servers fail
    # on ~/.npm and `uv run` fails on its cache (#241 review).
    for root in _runtime_writable_roots(claude_home):
        rw.append(root)

    return _dedupe(ro), _dedupe(rw)


def _claude_config_files(claude_home: Optional[Path]) -> List[str]:
    """The CLI's own config files the confined process must be able to read."""
    if claude_home is None:
        return []
    out = [
        str(claude_home / "settings.json"),
        str(claude_home / "settings.local.json"),
    ]
    # ~/.claude.json sits beside the config dir (the CLI's project/account state).
    try:
        out.append(str(claude_home.parent / ".claude.json"))
    except (OSError, RuntimeError, ValueError):
        pass
    return out


def _runtime_writable_roots(claude_home: Optional[Path]) -> List[str]:
    """Writable caches/state the CLI and its tools create outside the workspace.

    Plugin hook state (``~/.claude/plugins/data``), the npm cache (``~/.npm``,
    for ``npx -y`` plugin MCP servers) and the uv cache (``$UV_CACHE_DIR`` or
    ``~/.cache/uv``, for skill ``uv run``). Shared HOME today; per-user
    ownership under uid mode is a tracked follow-up.
    """
    roots: List[str] = []
    if claude_home is not None:
        roots.append(str(claude_home / "plugins" / "data"))
    try:
        home = Path(os.path.expanduser("~"))
    except (OSError, RuntimeError, ValueError):
        home = None
    if home is not None and str(home) not in ("", "~"):
        roots.append(str(home / ".npm"))
    uv_cache = (os.getenv("UV_CACHE_DIR") or "").strip()
    if uv_cache:
        roots.append(uv_cache)
    elif home is not None and str(home) not in ("", "~"):
        roots.append(str(home / ".cache" / "uv"))
    return roots


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
        # concurrent anonymous runs still get distinct uids. A named user gets a
        # durable uid (persist=True); an anonymous/stateless run uses the
        # recycled in-memory ephemeral band (persist=False) so it cannot grow
        # the registry or exhaust the band (#241 review).
        key = user or str(effective_cwd)
        uid = ui.resolve_uid(key, persist=bool(user))
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


def preflight_options_env(effective_cwd: Path) -> Dict[str, str]:
    """Confinement env for an internal catalog / slash-preflight spawn.

    The slash-command preflight and catalog run a throwaway CLI only to read the
    registered command list; they carry no user data and need no per-user
    identity, so they are confined in **landlock** mode regardless of the
    effective mode. This keeps them off the broker/ticket path (uid mode) — an
    unticketed uid-mode spawn would exit non-zero and turn every ``/skill``
    prompt into a 500, and would consume a per-user uid per preflight — while
    still handing the launcher the ro/rw lists so the confined CLI can read
    ``~/.claude`` (without them the catalog loses every user/plugin skill and a
    ``None`` cwd lets the launcher grab ``/app`` as writable). No-op off.
    """
    if not confine_enabled():
        return {}
    ro, rw = _ro_rw_roots(Path(effective_cwd))
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
        _warn_if_launcher_agent_writable(launcher)

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
            "ABI >= 2, kernel >= 5.19). Confinement cannot be enforced."
        )

    return errors
