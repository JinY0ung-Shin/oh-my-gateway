#!/usr/bin/env python3
"""Docker entrypoint for repairing writable bind mounts before startup."""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path


DEFAULT_UID = 1000
DEFAULT_GID = 1000
DEFAULT_DATA_DIR = Path("/app/data")
DEFAULT_CLAUDE_HOME = Path("/home/app/.claude")
DEFAULT_CODEX_HOME = Path("/home/app/.codex")
DEFAULT_OPENCODE_HOME = Path("/home/app/.local/share/opencode")
DEFAULT_OPENCODE_CONFIG = Path("/home/app/.config/opencode")
DEFAULT_UV_CACHE_DIR = Path("/home/app/.cache/uv")
MYSQL_DATA_DIR_NAME = "mysql_data"


def _parse_id(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be positive, got {value}")
    return value


def _chown(path: Path, uid: int, gid: int) -> bool:
    """Chown ``path`` itself; return False when it sits on a read-only mount."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"warning: could not chown {path}: {exc}", file=sys.stderr)
        return True
    # Another hard link to a file the app user does not own can live outside
    # these trees, and changing this inode would change that file as well.
    if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1 and info.st_uid != uid:
        reason = f"{info.st_nlink} hard links, owned by uid {info.st_uid}"
        print(f"warning: skipping {path}: {reason}", file=sys.stderr)
        return True
    try:
        # These trees are writable by the app user, so a link found in them
        # must never redirect a root chown: change the link itself.
        os.chown(path, uid, gid, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"warning: could not chown {path}: {exc}", file=sys.stderr)
        return exc.errno != errno.EROFS
    return True


def _first_symlink(path: Path, *, anchor: Path) -> Path | None:
    """Return the first symlink from ``anchor`` (inclusive) down to ``path``."""
    current = anchor
    if current.is_symlink():
        return current
    for part in path.relative_to(anchor).parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _prepare_dir(path: Path, *, anchor: Path) -> bool:
    """Create ``path`` below ``anchor`` unless a symlink sits on the way."""
    if path != anchor and anchor not in path.parents:
        print(f"warning: skipping {path}: not under {anchor}", file=sys.stderr)
        return False
    link = _first_symlink(path, anchor=anchor)
    if link is not None:
        print(f"warning: skipping {path}: {link} is a symlink", file=sys.stderr)
        return False
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"warning: skipping {path}: {exc}", file=sys.stderr)
        return False
    return True


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    # Stop at a read-only mount, and never walk from a link: os.walk() lists
    # a symlinked top directory's target even with followlinks=False.
    if not _chown(root, uid, gid) or root.is_symlink() or not root.is_dir():
        return
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        writable_dirs = []
        for name in dirs:
            if _chown(current_path / name, uid, gid):
                writable_dirs.append(name)
        # Pruning keeps os.walk() out of read-only mounts below the root.
        dirs[:] = writable_dirs
        for name in files:
            _chown(current_path / name, uid, gid)


def _chown_path_with_parents(path: Path, *, stop: Path, uid: int, gid: int) -> None:
    try:
        path.relative_to(stop)
    except ValueError:
        _chown(path, uid, gid)
        return

    paths = [path]
    current = path
    while current != stop and current.parent != current:
        current = current.parent
        paths.append(current)
    for item in reversed(paths):
        _chown(item, uid, gid)


def prepare_writable_paths(
    *,
    uid: int,
    gid: int,
    data_dir: Path = DEFAULT_DATA_DIR,
    claude_home: Path = DEFAULT_CLAUDE_HOME,
    codex_home: Path = DEFAULT_CODEX_HOME,
    opencode_home: Path = DEFAULT_OPENCODE_HOME,
    opencode_config: Path = DEFAULT_OPENCODE_CONFIG,
    uv_cache_dir: Path = DEFAULT_UV_CACHE_DIR,
) -> None:
    """Ensure gateway-owned writable paths are usable by the app process."""
    data_dir = Path(data_dir)
    prompts_dir = data_dir / "prompts"
    claude_home = Path(claude_home)
    codex_home = Path(codex_home)
    opencode_home = Path(opencode_home)
    opencode_config = Path(opencode_config)
    uv_cache_dir = Path(uv_cache_dir)
    home_dir = claude_home.parent

    # Never create or chown through a link between an anchor (data_dir,
    # home_dir) and a repaired path; skip that path with a warning instead.
    data_ready = _prepare_dir(data_dir, anchor=data_dir)
    if data_ready:
        _prepare_dir(prompts_dir, anchor=data_dir)
    _prepare_dir(home_dir, anchor=home_dir)
    ready_home_paths = []
    for path in (claude_home, codex_home, opencode_home, opencode_config, uv_cache_dir):
        if _prepare_dir(path, anchor=home_dir):
            ready_home_paths.append(path)

    if data_ready and _chown(data_dir, uid, gid):
        for child in data_dir.iterdir():
            if child.name == MYSQL_DATA_DIR_NAME:
                continue
            if child == prompts_dir:
                _chown_tree(child, uid, gid)
            elif child.is_file() or child.is_symlink():
                _chown(child, uid, gid)

    for path in ready_home_paths:
        _chown_path_with_parents(path, stop=home_dir, uid=uid, gid=gid)
        _chown_tree(path, uid, gid)


def drop_privileges(uid: int, gid: int) -> None:
    """Switch from root to the runtime app uid/gid."""
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


# ===========================================================================
# uid-isolation broker launch (issue #241, PR-C) — CONTAINER-VERIFY REQUIRED
# ===========================================================================
#
# When CLAUDE_ISOLATION_MODE=uid, the gateway needs a resident privileged broker
# to fork each CLI as its session's uid (see docs/security-process-isolation.md
# §3 and src/backends/claude/isolation flow). The gateway body stays unprivileged;
# this root entrypoint forks the broker BEFORE dropping to the app uid, and the
# broker drops to a dedicated non-root uid that keeps ONLY CAP_SETUID/CAP_SETGID
# (KEEPCAPS + ambient + securebits lockdown) — never full root.
#
# This privileged path CANNOT be exercised on a dev workstation (no root, no
# container). It is written to the design and gated fail-closed: if the broker
# does not come up, the gateway's startup check refuses uid mode rather than
# running unconfined. VERIFY IN THE TARGET CONTAINER (as the app user) with
# scripts/isolation_probe.py and the acceptance tests before enabling uid mode
# in production. The remaining hardening (a seccomp arg filter pinning set*id to
# the session uid band; bounding-set minimization needs CAP_SETPCAP) is a
# container/image step.

_CAP_SETGID = 6
_CAP_SETUID = 7
_PR_SET_KEEPCAPS = 8
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_RAISE = 2
_SECUREBITS_LOCKDOWN = 0x2F  # NOROOT|NOROOT_LOCKED|NO_SETUID_FIXUP|..._LOCKED
_PR_SET_SECUREBITS = 28
_CAP_VERSION_3 = 0x20080522


def _keep_only_setuid_setgid() -> None:
    """Retain only CAP_SETUID/SETGID (effective+permitted+inheritable+ambient).

    Called in the broker child after it has dropped to its dedicated uid with
    PR_SET_KEEPCAPS set. ctypes because the stdlib has no capset binding.
    """
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    mask = (1 << _CAP_SETUID) | (1 << _CAP_SETGID)

    class _Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class _Data(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    hdr = _Header(_CAP_VERSION_3, 0)
    data = (_Data * 2)()
    data[0].effective = mask
    data[0].permitted = mask
    data[0].inheritable = mask
    if libc.capset(ctypes.byref(hdr), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "capset failed")
    # Ambient so the caps survive the broker's own execve of the script.
    for cap in (_CAP_SETUID, _CAP_SETGID):
        if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_RAISE, cap, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_CAP_AMBIENT_RAISE failed")
    # Lock securebits so a later setuid(0) cannot regain root.
    libc.prctl(_PR_SET_SECUREBITS, _SECUREBITS_LOCKDOWN, 0, 0, 0)


def _resolve_real_cli() -> str | None:
    """The real CLI the broker execs: CLAUDE_CLI_PATH, else the SDK bundle."""
    override = (os.environ.get("CLAUDE_CLI_PATH") or "").strip()
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    import subprocess

    snippet = (
        "import claude_agent_sdk._internal.transport.subprocess_cli as m; "
        "t = m.SubprocessCLITransport.__new__(m.SubprocessCLITransport); "
        "print(t._find_cli())"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            timeout=30,
        )
        cli = out.stdout.strip()
        if cli and os.path.isfile(cli) and os.access(cli, os.X_OK):
            return cli
    except Exception as exc:  # pragma: no cover - startup diagnostics only
        print(f"warning: could not resolve real CLI for broker: {exc}", file=sys.stderr)
    return None


def launch_isolation_broker() -> None:
    """Fork the resident broker before privilege drop. No-op unless uid mode.

    Generates the ticket secret (if unset) and exports it, the socket path and
    the real CLI into the environment the gateway (exec'd next) inherits, so the
    gateway can mint tickets the broker verifies.
    """
    if (os.environ.get("CLAUDE_ISOLATION_MODE") or "").strip().lower() != "uid":
        return
    if os.geteuid() != 0:
        print(
            "warning: CLAUDE_ISOLATION_MODE=uid but entrypoint is not root; cannot "
            "launch the broker (use a root start or a file-cap broker). The gateway "
            "will refuse uid mode at startup.",
            file=sys.stderr,
        )
        return

    broker = Path(__file__).resolve().parent / "isolation_broker.py"
    sock = (os.environ.get("GATEWAY_ISOLATION_BROKER_SOCKET") or "").strip()
    if not sock:
        sock = "/run/gateway-isolation/broker.sock"
        os.environ["GATEWAY_ISOLATION_BROKER_SOCKET"] = sock
    import secrets as _secrets

    if not (os.environ.get("GATEWAY_ISOLATION_TICKET_SECRET") or "").strip():
        os.environ["GATEWAY_ISOLATION_TICKET_SECRET"] = _secrets.token_hex(32)
    real_cli = _resolve_real_cli()
    if not real_cli:
        print(
            "warning: no real CLI resolved for the broker; not launching it.",
            file=sys.stderr,
        )
        return
    os.environ["GATEWAY_REAL_CLI"] = real_cli

    sock_dir = Path(sock).parent
    try:
        sock_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(sock_dir, 0o711)  # traversable; the socket itself is the gate
    except OSError as exc:
        print(f"warning: could not prepare broker socket dir: {exc}", file=sys.stderr)
        return

    broker_uid = _parse_id("CLAUDE_ISOLATION_BROKER_UID", 21000)
    broker_gid = _parse_id("CLAUDE_ISOLATION_BROKER_GID", broker_uid)

    pid = os.fork()
    if pid > 0:
        return  # entrypoint continues; the gateway will verify reachability

    # --- broker child: drop to the dedicated uid keeping SETUID/SETGID ------
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_KEEPCAPS, 1, 0, 0, 0)
        os.setgroups([])
        os.setgid(broker_gid)
        os.setuid(broker_uid)
        _keep_only_setuid_setgid()
        os.execv(sys.executable, [sys.executable, "-I", str(broker)])
    except Exception as exc:  # pragma: no cover - privileged path
        print(f"isolation broker failed to start: {exc}", file=sys.stderr)
        os._exit(78)


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("no command provided")

    uid = _parse_id("APP_UID", DEFAULT_UID)
    gid = _parse_id("APP_GID", DEFAULT_GID)

    if os.geteuid() == 0:
        prepare_writable_paths(uid=uid, gid=gid)
        launch_isolation_broker()  # before the drop; no-op unless uid mode
        drop_privileges(uid, gid)

    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
