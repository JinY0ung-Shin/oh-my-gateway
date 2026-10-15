#!/usr/bin/env python3
"""Docker entrypoint: repairs writable bind mounts before startup and, in uid
mode, provisions what session uids need and forks the isolation broker."""

from __future__ import annotations

import errno
import json
import math
import os
import socket
import stat
import sys
import time
from collections.abc import Container
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
ISOLATION_MODE_ENV = "CLAUDE_ISOLATION_MODE"
# uid mode's per-user band, with the gateway's names and defaults
# (src/backends/claude/uid_isolation.py; tests/test_entrypoint_broker.py pins
# the parity).
SESSION_UID_MIN_ENV = "CLAUDE_ISOLATION_UID_MIN"
SESSION_UID_MAX_ENV = "CLAUDE_ISOLATION_UID_MAX"
DEFAULT_SESSION_UID_MIN = 20000
DEFAULT_SESSION_UID_MAX = 29999
BROKER_UID_ENV = "CLAUDE_ISOLATION_BROKER_UID"
BROKER_GID_ENV = "CLAUDE_ISOLATION_BROKER_GID"
# The gateway's persisted user -> uid map: uid_isolation._registry_path() is
# <app root>/data/isolation_uids.json, and the image's app root is /app.
UID_REGISTRY_ENV = "CLAUDE_ISOLATION_UID_REGISTRY"
DEFAULT_UID_REGISTRY = DEFAULT_DATA_DIR / "isolation_uids.json"
MAX_UID_REGISTRY_BYTES = 16 << 20
# Just below the default per-user band (20000-29999) and kept equal to the
# gateway's default in uid_isolation, so the broker's uid is never a session's.
DEFAULT_BROKER_UID = 19999
DEFAULT_BROKER_SOCKET = "/run/gateway-isolation/broker.sock"
# One operator knob bounds both waits for the broker: this one, before the
# gateway is exec'd, and the gateway's own (src/backends/claude/landlock.py).
BROKER_WAIT_ENV = "CLAUDE_ISOLATION_BROKER_WAIT_SECONDS"
DEFAULT_BROKER_WAIT_SECONDS = 10.0
MAX_BROKER_WAIT_SECONDS = 300.0


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


def _gateway_int(name: str, default: int) -> int:
    """An integer knob read like the gateway's ``uid_isolation._int_env``:
    blank gives *default*, and so does a value that is not an integer (with a
    warning)."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"warning: ignoring {name}={raw!r}: not an integer; using {default}",
            file=sys.stderr,
        )
        return default


def _session_uid_band() -> tuple[int, int]:
    """``[CLAUDE_ISOLATION_UID_MIN, CLAUDE_ISOLATION_UID_MAX]``, parsed exactly
    like the gateway's ``uid_isolation.uid_range()``."""
    return (
        _gateway_int(SESSION_UID_MIN_ENV, DEFAULT_SESSION_UID_MIN),
        _gateway_int(SESSION_UID_MAX_ENV, DEFAULT_SESSION_UID_MAX),
    )


def _uid_mode() -> bool:
    return (os.environ.get(ISOLATION_MODE_ENV) or "").strip().lower() == "uid"


def _uid_registry_path() -> Path:
    """``CLAUDE_ISOLATION_UID_REGISTRY`` read like ``_registry_path()``."""
    raw = (os.environ.get(UID_REGISTRY_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_UID_REGISTRY


def _read_uid_registry(path: Path) -> bytes | None:
    """The registry file's bytes, or ``None`` when there is none yet.

    Root reads a file the app uid can replace, so: no symlink anywhere on its
    path, a regular file only (never block on a FIFO or open a device), at most
    ``MAX_UID_REGISTRY_BYTES``. Raises ``ValueError`` or ``OSError`` otherwise.
    """
    absolute = Path(os.path.abspath(path))
    link = _first_symlink(absolute, anchor=Path(absolute.anchor))
    if link is not None:
        raise ValueError(f"{link} is a symlink")
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(absolute, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("replaced while it was opened")
        if opened.st_size > MAX_UID_REGISTRY_BYTES:
            raise ValueError(f"larger than {MAX_UID_REGISTRY_BYTES} bytes")
        chunks, size = [], 0
        while size <= MAX_UID_REGISTRY_BYTES:
            chunk = os.read(fd, MAX_UID_REGISTRY_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_UID_REGISTRY_BYTES:
        raise ValueError(f"larger than {MAX_UID_REGISTRY_BYTES} bytes")
    return b"".join(chunks)


def _registry_uids() -> frozenset[int]:
    """Every uid the gateway's uid registry records, honored or held.

    Values count as uids exactly as ``UidRegistry._load`` takes them (ints, not
    bools). A file the gateway would refuse (unreadable, not UTF-8 JSON, not an
    object), or one that is symlinked, not a regular file or oversized, is
    ignored with a warning and the band alone decides: never fatal here.
    """
    path = _uid_registry_path()
    try:
        data = _read_uid_registry(path)
        if data is None:
            return frozenset()
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, RecursionError) as exc:
        print(f"warning: ignoring the uid registry {path}: {exc}", file=sys.stderr)
        return frozenset()
    return frozenset(
        uid
        for uid in raw.values()
        if isinstance(uid, int) and not isinstance(uid, bool)
    )


class _UidBand:
    """Membership in ``[lo, hi]`` or *extra*, minus *reserved* (not a set: a
    band can be huge)."""

    def __init__(
        self,
        lo: int,
        hi: int,
        reserved: set[int],
        extra: frozenset[int] = frozenset(),
    ) -> None:
        self.lo, self.hi, self.reserved, self.extra = lo, hi, reserved, extra

    def __contains__(self, uid: object) -> bool:
        return (
            isinstance(uid, int)
            and (self.lo <= uid <= self.hi or uid in self.extra)
            and uid not in self.reserved
        )


def _session_owned_uids(app_uid: int) -> Container[int]:
    """The owners whose entries the root step leaves alone.

    In uid mode, the uids the gateway can give a session, the band
    (``uid_isolation.uid_range()``), and every uid its registry records: after
    the band is narrowed a named user's uid can lie outside it, and the gateway
    refuses to start over that user only after this step has run. Minus
    ``reserved_uids()``, i.e. root, the app uid and gid and the broker uid and
    gid, so an entry this step must repair is never taken for a session's. (No
    session ever ran as one of those gids: the broker refuses them as a uid.)
    None in any other mode: those sessions run as the app uid, and a mode
    change hands uid mode's leftovers back to it.
    """
    if not _uid_mode():
        return ()
    lo, hi = _session_uid_band()
    return _UidBand(lo, hi, _reserved_ids(app_uid), _registry_uids())


def _reserved_ids(app_uid: int) -> set[int]:
    """The ids no session gets as its uid or gid, read like the gateway's
    ``uid_isolation.reserved_uids()``: root, the app uid and gid, and the
    broker uid and gid (a session's gid is its uid)."""
    app_gid = _gateway_int("APP_GID", DEFAULT_GID)
    return {
        0,
        app_uid,
        app_gid,
        _gateway_int(BROKER_UID_ENV, DEFAULT_BROKER_UID),
        _gateway_int(BROKER_GID_ENV, app_gid),
    }


def _chown(path: Path, uid: int, gid: int, skip_owners: Container[int] = ()) -> bool:
    """Chown ``path`` itself; return False when the walk must not enter it: it
    sits on a read-only mount, or its owner is in ``skip_owners``."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"warning: could not chown {path}: {exc}", file=sys.stderr)
        return True
    # uid mode: a session uid's own state (its projects/<name> transcripts,
    # memory, subagent logs) stays its own across restarts. Re-owned to the
    # app uid with its 0600/0700 modes, it locked the session out of resuming
    # (#244 review 4). Nor is it walked: the session controls its contents.
    if info.st_uid in skip_owners:
        return False
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


def _chown_tree(
    root: Path, uid: int, gid: int, skip_owners: Container[int] = ()
) -> None:
    # Stop at a read-only mount, and never walk from a link: os.walk() lists
    # a symlinked top directory's target even with followlinks=False.
    if (
        not _chown(root, uid, gid, skip_owners)
        or root.is_symlink()
        or not root.is_dir()
    ):
        return
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        writable_dirs = []
        for name in dirs:
            if _chown(current_path / name, uid, gid, skip_owners):
                writable_dirs.append(name)
        # Pruning keeps os.walk() out of read-only mounts below the root, and
        # out of directories a session uid owns (uid mode).
        dirs[:] = writable_dirs
        for name in files:
            _chown(current_path / name, uid, gid, skip_owners)


def _chown_path_with_parents(
    path: Path, *, stop: Path, uid: int, gid: int, skip_owners: Container[int] = ()
) -> None:
    try:
        path.relative_to(stop)
    except ValueError:
        _chown(path, uid, gid, skip_owners)
        return

    paths = [path]
    current = path
    while current != stop and current.parent != current:
        current = current.parent
        paths.append(current)
    for item in reversed(paths):
        _chown(item, uid, gid, skip_owners)


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
    skip_owners: Container[int] = (),
) -> None:
    """Ensure gateway-owned writable paths are usable by the app process.

    Entries owned by a uid in ``skip_owners`` (uid mode's session uids, see
    :func:`_session_owned_uids`) are neither chowned nor walked.
    """
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

    if data_ready and _chown(data_dir, uid, gid, skip_owners):
        for child in data_dir.iterdir():
            if child.name == MYSQL_DATA_DIR_NAME:
                continue
            if child == prompts_dir:
                _chown_tree(child, uid, gid, skip_owners)
            elif child.is_file() or child.is_symlink():
                _chown(child, uid, gid, skip_owners)

    for path in ready_home_paths:
        _chown_path_with_parents(
            path, stop=home_dir, uid=uid, gid=gid, skip_owners=skip_owners
        )
        _chown_tree(path, uid, gid, skip_owners)


def drop_privileges(uid: int, gid: int) -> None:
    """Switch from root to the runtime app uid/gid."""
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


# ===========================================================================
# uid mode: the accounts and directories session uids need (root start)
# ===========================================================================
#
# docs/isolation-uid-ownership-design.md §2.1, §4 and §8. The image has
# accounts for root and app only, and HOME (/home/app) is 0700, so a session
# uid had no passwd entry and could reach neither the shared Claude config dir
# nor a config dir of its own. main() runs these in uid mode, after the
# ownership repair and before the broker that runs the session uids starts.
# Everything here only warns: the gateway's startup probe spawns a CLI as a
# session uid and refuses uid mode, with the reason, when the result does not
# work.

PASSWD_PATH = Path("/etc/passwd")
GROUP_PATH = Path("/etc/group")
BROKER_ACCOUNT = "claude-broker"
# A session uid's account and group are named after it: claude-u20000, ...
SESSION_ACCOUNT_PREFIX = "claude-u"
ACCOUNT_HOME = "/nonexistent"
ACCOUNT_SHELL = "/usr/sbin/nologin"
# A wider band is not written into the files (every lookup scans them): the
# operator provisions NSS for it instead.
MAX_PROVISIONED_UIDS = 65536
# The largest id a session may get (uid_isolation._MAX_ID): (uid_t)-1 tells
# setresuid/setresgid to leave the id unchanged.
MAX_SESSION_ID = 4294967294


class _AccountFile:
    """``/etc/passwd`` or ``/etc/group``: its bytes and status, the names and
    ids its lines claim, and the lines to append."""

    def __init__(self, path: Path, data: bytes, info: os.stat_result) -> None:
        self.path, self.data, self.info = path, data, info
        self.names: set[str] = set()
        self.ids: set[int] = set()
        self.added: list[str] = []
        for raw in data.split(b"\n"):
            # A line counts by its name and id fields even when the rest is
            # malformed, so neither is ever claimed twice.
            fields = raw.strip().split(b":")
            if not fields[0] or fields[0].startswith(b"#"):
                continue
            self.names.add(fields[0].decode("utf-8", "surrogateescape"))
            if len(fields) > 2 and fields[2].strip().isdigit():
                self.ids.add(int(fields[2].strip()))

    def add(self, line: str, name: str, xid: int) -> None:
        self.added.append(line)
        self.names.add(name)
        self.ids.add(xid)


def _read_account_file(path: Path) -> _AccountFile | None:
    """*path* read for :func:`provision_session_accounts`; None, with a
    warning, when it is not a regular file (a symlink would be replaced by a
    copy) or cannot be read."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("replaced while it was opened")
            chunks = []
            while chunk := os.read(fd, 1 << 20):
                chunks.append(chunk)
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        print(f"warning: not adding session accounts to {path}: {exc}", file=sys.stderr)
        return None
    return _AccountFile(path, b"".join(chunks), opened)


def _replace_file(path: Path, data: bytes, info: os.stat_result) -> None:
    """Atomically replace *path* with *data*, keeping its owner and mode.

    The bytes go to a temp file beside it, which gets the owner and mode and
    an fsync before it is renamed over *path*; the directory is synced after.
    On failure the temp file is removed and *path* is unchanged.
    """
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fchown(fd, info.st_uid, info.st_gid)
            os.fchmod(fd, stat.S_IMODE(info.st_mode))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _write_account_file(account_file: _AccountFile) -> None:
    """Append the added lines to the file; a no-op when there are none."""
    if not account_file.added:
        return
    data = account_file.data
    if data and not data.endswith(b"\n"):
        data += b"\n"
    data += "".join(f"{line}\n" for line in account_file.added).encode()
    try:
        _replace_file(account_file.path, data, account_file.info)
    except OSError as exc:
        print(
            f"warning: could not add {len(account_file.added)} session accounts "
            f"to {account_file.path}: {exc}",
            file=sys.stderr,
        )


def _broker_ids(app_gid: int) -> tuple[int, int] | None:
    """The broker's uid and gid as :func:`launch_isolation_broker` reads them;
    None, with a warning, when one is invalid (that launch refuses it too)."""
    try:
        return (
            _parse_id(BROKER_UID_ENV, DEFAULT_BROKER_UID),
            _parse_id(BROKER_GID_ENV, app_gid),
        )
    except SystemExit as exc:
        print(f"warning: not adding the broker account: {exc}", file=sys.stderr)
        return None


def _provisioned_band() -> range:
    """The uids of the session band to add accounts for: none, with a warning,
    when the gateway refuses the band or it is wider than
    ``MAX_PROVISIONED_UIDS`` (an empty band the gateway reports itself)."""
    lo, hi = _session_uid_band()
    if hi < lo:
        return range(0)
    if lo < 1 or hi > MAX_SESSION_ID:
        print(
            f"warning: not adding session accounts for the uid band [{lo}, {hi}]: "
            f"the gateway refuses a band outside [1, {MAX_SESSION_ID}]",
            file=sys.stderr,
        )
        return range(0)
    if hi - lo + 1 > MAX_PROVISIONED_UIDS:
        print(
            f"warning: not adding accounts for the {hi - lo + 1} session uids of "
            f"[{lo}, {hi}] to {PASSWD_PATH} and {GROUP_PATH} (more than "
            f"{MAX_PROVISIONED_UIDS}): provision NSS entries for them, or whoami "
            "and node's os.userInfo() fail in the sessions",
            file=sys.stderr,
        )
        return range(0)
    return range(lo, hi + 1)


def provision_session_accounts(
    app_uid: int,
    app_gid: int,
    *,
    passwd_path: Path = PASSWD_PATH,
    group_path: Path = GROUP_PATH,
) -> None:
    """Add ``/etc/passwd`` and ``/etc/group`` entries for the broker and the
    session uids (uid mode, root start).

    uid mode runs the broker and every session as uids the image has no
    account for, and userland needs one: ``whoami`` and node's
    ``os.userInfo()`` (the CLI's runtime) fail for a uid without a passwd
    entry. So this adds ``claude-broker`` for the broker uid (primary group =
    the broker gid; that gid's group is added as ``claude-broker`` only when it
    has none, and by default it is the app's) and, for every uid of the band
    ``[CLAUDE_ISOLATION_UID_MIN, CLAUDE_ISOLATION_UID_MAX]`` (read like the
    gateway) except the ids no session gets (:func:`_reserved_ids`), a user
    ``claude-u<uid>`` with a group ``claude-u<uid>`` of gid = uid (the broker
    gives a session the gid equal to its uid). Home ``/nonexistent``, shell
    ``/usr/sbin/nologin``, no password.

    Existing lines are never changed, malformed ones included, and no entry is
    added whose name or id a line already claims: an operator's own entries
    win, and a second start writes nothing. A band wider than
    ``MAX_PROVISIONED_UIDS`` is skipped with a warning; the operator then
    provisions NSS for it. A file is replaced atomically (owner and mode kept)
    and only when an entry is missing. Failures only warn.
    """
    passwd = _read_account_file(passwd_path)
    group = _read_account_file(group_path)
    unnamed: dict[Path, list[int]] = {}

    def add(account_file: _AccountFile | None, name: str, xid: int, line: str) -> None:
        if account_file is None or xid in account_file.ids:
            return
        if name in account_file.names:
            unnamed.setdefault(account_file.path, []).append(xid)
            return
        account_file.add(line, name, xid)

    def add_account(name: str, uid: int, gid: int, gecos: str) -> None:
        tail = f"{gecos}:{ACCOUNT_HOME}:{ACCOUNT_SHELL}"
        add(passwd, name, uid, f"{name}:x:{uid}:{gid}:{tail}")

    broker = _broker_ids(app_gid)
    if broker is not None:
        broker_uid, broker_gid = broker
        add(group, BROKER_ACCOUNT, broker_gid, f"{BROKER_ACCOUNT}:x:{broker_gid}:")
        add_account(BROKER_ACCOUNT, broker_uid, broker_gid, "claude isolation broker")
    reserved = _reserved_ids(app_uid)
    for uid in _provisioned_band():
        if uid in reserved:
            continue
        name = f"{SESSION_ACCOUNT_PREFIX}{uid}"
        add(group, name, uid, f"{name}:x:{uid}:")
        add_account(name, uid, uid, "claude session uid")
    for path, ids in unnamed.items():
        shown = ", ".join(str(xid) for xid in ids[:5]) + (", ..." if ids[5:] else "")
        print(
            f"warning: {path}: no entry added for {len(ids)} id(s) ({shown}): "
            "the name is taken by another id",
            file=sys.stderr,
        )
    # Groups first: an account's primary group exists before the account.
    for account_file in (group, passwd):
        if account_file is not None:
            _write_account_file(account_file)


CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
CONFIG_BASE_ENV = "CLAUDE_ISOLATION_CONFIG_BASE"
PLUGIN_CLONE_ROOT_ENV = "CLAUDE_PLUGIN_CLONE_ROOT"
WORKSPACES_DIR_ENV = "USER_WORKSPACES_DIR"
DEFAULT_APP_ROOT = DEFAULT_DATA_DIR.parent
# What this root step hands to the app uid (the shared config dir, the
# per-uid config base) is never one of these, inside one or above one: an
# app-owned /usr or /etc would let the app uid replace what root and the
# broker run. The launcher grants /opt and the other system roots read-only
# to every session, and the gateway refuses its own state there.
SYSTEM_DIRS = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/opt",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/tmp",
    "/usr",
    "/var/run",
    "/var/tmp",
)


def _beneath(path: str, root: str) -> bool:
    """Whether *path* is *root* or lies below it (both absolute)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _overlap(path: str, other: str) -> str | None:
    """How *path* overlaps *other* ("is", "contains" or "lies inside"), each
    taken as given and with its symlinks resolved; None when they are apart."""
    for mine in dict.fromkeys((path, os.path.realpath(path))):
        for theirs in dict.fromkeys((other, os.path.realpath(other))):
            if mine == theirs:
                return "is"
            if _beneath(theirs, mine):
                return "contains"
            if _beneath(mine, theirs):
                return "lies inside"
    return None


def _placement_refusal(path: str, protected: list[tuple[str, str]]) -> str | None:
    """Why this root step must not hand *path* to the app uid, or None: it is
    ``/``, or it is, contains or lies inside a *protected* ``(label, path)``
    or one of :data:`SYSTEM_DIRS`."""
    if path == "/":
        return "it is the filesystem root"
    system = [("the system directory", root) for root in SYSTEM_DIRS]
    for label, other in [*protected, *system]:
        relation = _overlap(path, other)
        if relation is not None:
            return f"it {relation} {label} {other}"
    return None


def _component_problem(path: str, exc: OSError) -> str:
    """*exc*, raised at the path component *path*, described."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return f"{path}: {exc.strerror}"
    if stat.S_ISLNK(mode):
        return f"{path} is a symlink"
    if not stat.S_ISDIR(mode):
        return f"{path} is not a directory"
    return f"{path}: {exc.strerror}"


def _make_dir_nofollow(path: str, mode: int, label: str) -> int | None:
    """Create the absolute *path* and its missing parents without following a
    symlink, and return an fd of it; None, with a warning, when that fails.

    Every component is opened ``O_NOFOLLOW`` from ``/`` down, so an existing
    one that is a symlink or not a directory fails the path before anything is
    created through it. A parent created here gets 0755 and *path* *mode*,
    whatever the umask.
    """
    parts = Path(path).parts[1:]
    if not parts:
        print(f"warning: not preparing {label}: it is /", file=sys.stderr)
        return None
    current = "/"
    fd = os.open(current, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, name in enumerate(parts):
            current = os.path.join(current, name)
            want = mode if index == len(parts) - 1 else 0o755
            created = True
            try:
                os.mkdir(name, want, dir_fd=fd)
            except FileExistsError:
                created = False
            flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            if not created and index < len(parts) - 1:
                flags |= os.O_PATH  # only traversed: needs no read access
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            if created:
                os.fchmod(fd, want)
    except OSError as exc:
        os.close(fd)
        print(
            f"warning: not preparing {label}: {_component_problem(current, exc)}",
            file=sys.stderr,
        )
        return None
    return fd


def _prepare_shared_config_dir(
    shared: str,
    *,
    uid: int,
    gid: int,
    skip_owners: Container[int],
    protected: list[tuple[str, str]],
) -> bool:
    """Create the shared config dir *shared* (S), hand its tree to the app uid
    like ``~/.claude`` and make S itself 0755. False (warned) when refused."""
    label = f"{CONFIG_DIR_ENV} {shared}"
    reason = _placement_refusal(shared, protected)
    if reason is not None:
        print(f"warning: not preparing {label}: {reason}", file=sys.stderr)
        return False
    fd = _make_dir_nofollow(shared, 0o755, label)
    if fd is None:
        return False
    try:
        _chown_tree(Path(shared), uid, gid, skip_owners)
        owner = os.fstat(fd).st_uid
        if owner != uid:
            print(
                f"warning: not opening {label} to session uids: it is owned by "
                f"uid {owner}, not the app uid {uid}",
                file=sys.stderr,
            )
            return False
        os.fchmod(fd, 0o755)
    except OSError as exc:
        print(f"warning: could not prepare {label}: {exc}", file=sys.stderr)
        return False
    finally:
        os.close(fd)
    return True


def _prepare_config_base(
    *, uid: int, gid: int, protected: list[tuple[str, str]]
) -> str | None:
    """Create the per-uid config base (read like the gateway's
    ``claude_config_paths.config_base()``), owned by app:app and 0711, its
    contents never walked; the path, or None (warned) when refused."""
    raw = (os.environ.get(CONFIG_BASE_ENV) or "").strip()
    if not raw:
        print(
            f"warning: {CONFIG_BASE_ENV} is unset: uid mode has no per-uid config "
            "dirs to give the session uids (docker-compose.uid.yml sets "
            "/data/claude/users)",
            file=sys.stderr,
        )
        return None
    if not os.path.isabs(raw):
        print(
            f"warning: not preparing {CONFIG_BASE_ENV} {raw}: not an absolute path",
            file=sys.stderr,
        )
        return None
    base = os.path.normpath(raw)
    label = f"{CONFIG_BASE_ENV} {base}"
    reason = _placement_refusal(base, protected)
    if reason is not None:
        print(f"warning: not preparing {label}: {reason}", file=sys.stderr)
        return None
    fd = _make_dir_nofollow(base, 0o711, label)
    if fd is None:
        return None
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o711)
    except OSError as exc:
        print(f"warning: could not prepare {label}: {exc}", file=sys.stderr)
        return None
    finally:
        os.close(fd)
    return base


def _workspaces_dir() -> str:
    """The workspace base, read like the gateway's ``_resolve_base_path()``."""
    raw = os.environ.get(WORKSPACES_DIR_ENV, "")
    if raw:
        return os.path.abspath(raw)
    import tempfile

    return os.path.join(tempfile.gettempdir(), "oh-my-gateway-workspaces")


def _warn_unreachable(label: str, path: str) -> None:
    """Warn when others may not search a directory from ``/`` down to *path*
    (itself included): a session uid has to traverse every one of them."""
    target = Path(path)
    blocked = []
    for step in (*reversed(target.parents), target):
        try:
            info = os.stat(step)
        except OSError:
            break
        if stat.S_ISDIR(info.st_mode) and not info.st_mode & stat.S_IXOTH:
            blocked.append(f"{step} (mode {stat.S_IMODE(info.st_mode):04o})")
    if blocked:
        print(
            f"warning: session uids cannot reach {label} {path}: others may not "
            f"search {', '.join(blocked)}",
            file=sys.stderr,
        )


def _check_clone_root(shared: str, home: str) -> None:
    """Warn when marketplace clones land outside the shared config dir, where
    the session uids read them."""
    raw = (os.environ.get(PLUGIN_CLONE_ROOT_ENV) or "").strip()
    example = os.path.join(shared, "plugin-marketplaces")
    if not raw:
        print(
            f"warning: {PLUGIN_CLONE_ROOT_ENV} is unset, so marketplaces are cloned "
            f"under {os.path.join(home, '.claude', 'plugin-marketplaces')}, which "
            f"session uids cannot reach; set it inside {CONFIG_DIR_ENV} (for "
            f"example {example})",
            file=sys.stderr,
        )
    elif _overlap(os.path.abspath(raw), shared) != "lies inside":
        print(
            f"warning: {PLUGIN_CLONE_ROOT_ENV} {raw} is not inside "
            f"{CONFIG_DIR_ENV} {shared}: uid-mode sessions read the marketplace "
            "clones through the shared config dir, so plugins cloned elsewhere "
            f"may not load (for example {example})",
            file=sys.stderr,
        )


def prepare_isolation_dirs(
    *,
    uid: int,
    gid: int,
    skip_owners: Container[int] = (),
    home: Path | None = None,
    app_root: Path = DEFAULT_APP_ROOT,
) -> None:
    """Prepare the config dirs uid mode's session uids use (root start).

    * The shared config dir S (``CLAUDE_CONFIG_DIR``): the gateway's own, whose
      skills, plugins and settings every per-uid config dir links in. Unset, it
      is ``$HOME/.claude``; ``$HOME`` is 0700 in the image, so S unset or inside
      ``$HOME`` only warns that session uids cannot reach it (the gateway's
      startup probe then refuses uid mode). Otherwise S is created without
      following a symlink anywhere on its path, its tree handed to the app uid
      like ``~/.claude`` (entries a session uid owns, *skip_owners*, are left
      alone) and S itself made 0755.
    * The per-uid config base (``CLAUDE_ISOLATION_CONFIG_BASE``), absolute and
      apart from S: created the same way, owned by app:app and 0711, but never
      walked, since the per-uid dirs in it hold files the session uids own.

    Neither may be ``/``, nor be, contain or lie inside ``$HOME``, the app root,
    the workspace base or a system directory (:data:`SYSTEM_DIRS`): this root
    step would hand them to the app uid. A refused one is skipped with a
    warning. Also warns, without failing, when a directory on the way to S,
    the base or the workspace base is not searchable by others, and when
    ``CLAUDE_PLUGIN_CLONE_ROOT`` does not lie inside S.
    """
    if home is None:
        home = (os.environ.get("HOME") or "").strip() or DEFAULT_CLAUDE_HOME.parent
    home_dir = os.path.abspath(home)
    app = os.path.abspath(app_root)
    workspaces = _workspaces_dir()
    raw = (os.environ.get(CONFIG_DIR_ENV) or "").strip()
    shared = os.path.abspath(raw) if raw else None

    ready_shared = False
    if shared is None or _overlap(shared, home_dir) in ("is", "lies inside"):
        where = (
            f"is unset, so the shared Claude config dir is {home_dir}/.claude"
            if shared is None
            else f"is {shared}"
        )
        print(
            f"warning: {CONFIG_DIR_ENV} {where}, inside $HOME {home_dir}: session "
            "uids cannot reach it (the image makes $HOME 0700), so uid-mode "
            "sessions get no skills, plugins or settings and the gateway's "
            f"startup probe refuses uid mode; set {CONFIG_DIR_ENV} outside $HOME "
            "(docker-compose.uid.yml uses /data/claude/shared)",
            file=sys.stderr,
        )
    else:
        ready_shared = _prepare_shared_config_dir(
            shared,
            uid=uid,
            gid=gid,
            skip_owners=skip_owners,
            protected=[
                ("$HOME", home_dir),
                ("the gateway's app root", app),
                ("the workspace base", workspaces),
            ],
        )

    others = [] if shared is None else [(CONFIG_DIR_ENV, shared)]
    base = _prepare_config_base(
        uid=uid,
        gid=gid,
        protected=[
            *others,
            ("$HOME", home_dir),
            ("the gateway's app root", app),
            ("the workspace base", workspaces),
        ],
    )

    for label, path in (
        (CONFIG_DIR_ENV, shared if ready_shared else None),
        (CONFIG_BASE_ENV, base),
        (WORKSPACES_DIR_ENV, workspaces),
    ):
        if path is not None:
            _warn_unreachable(label, path)
    if ready_shared:
        _check_clone_root(shared, home_dir)


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
_CAP_SETPCAP = 8
_CAP_SETUID = 7
_PR_SET_KEEPCAPS = 8
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_RAISE = 2
_SECUREBITS_LOCKDOWN = 0x2F  # NOROOT|NOROOT_LOCKED|NO_SETUID_FIXUP|..._LOCKED
_PR_SET_SECUREBITS = 28
_CAP_VERSION_3 = 0x20080522


def _capset(libc, effective: int, permitted: int, inheritable: int) -> None:
    import ctypes

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
    data[0].effective = effective
    data[0].permitted = permitted
    data[0].inheritable = inheritable
    if libc.capset(ctypes.byref(hdr), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "capset failed")


def _keep_only_setuid_setgid() -> None:
    """Retain only CAP_SETUID/SETGID (effective+permitted+inheritable+ambient).

    Called in the broker child after it has dropped to its dedicated uid with
    PR_SET_KEEPCAPS set. ctypes because the stdlib has no capset binding.

    Order matters: locking securebits needs CAP_SETPCAP *effective*, so we keep
    SETPCAP through the securebits step and only then drop it. Doing the drop
    first (as before) made PR_SET_SECUREBITS fail with EPERM — the NOROOT lock
    that stops a later setuid(0) from regaining root never took (#241 review).
    """
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    keep = (1 << _CAP_SETUID) | (1 << _CAP_SETGID)
    with_setpcap = keep | (1 << _CAP_SETPCAP)

    # 1. Keep SETUID/SETGID + SETPCAP (effective) so the securebits lock works.
    _capset(libc, effective=with_setpcap, permitted=with_setpcap, inheritable=keep)
    # 2. Lock securebits: a later setuid(0) cannot regain root (NOROOT_LOCKED).
    if libc.prctl(_PR_SET_SECUREBITS, _SECUREBITS_LOCKDOWN, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_SECUREBITS failed")
    # 3. Now drop SETPCAP; keep only SETUID/SETGID.
    _capset(libc, effective=keep, permitted=keep, inheritable=keep)
    # 4. Ambient so the caps survive the broker's own execve of the script.
    for cap in (_CAP_SETUID, _CAP_SETGID):
        if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_RAISE, cap, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_CAP_AMBIENT_RAISE failed")
    # 5. No new privileges: the broker never needs to gain any via execve.
    libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)


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
        # -I: this runs as root with cwd=/app and HOME=/home/app, so without it
        # the app user's site-packages and cwd are on sys.path and an app-uid
        # process could plant a module that executes as root here (#233 / #241).
        out = subprocess.run(
            [sys.executable, "-I", "-c", snippet],
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


def _untrusted_component(path: Path, trusted_owners: frozenset[int]) -> str | None:
    """Why *path* cannot safely hold the broker's socket directory, or None.

    Every component from ``/`` down must be a real directory (no symlink to
    retarget later), owned by a trusted uid, and not writable by other users
    unless sticky — otherwise someone could rename the socket directory away
    and plant a fake broker socket in its place.
    """
    current = Path(path.anchor)
    for part in (None, *path.parts[1:]):
        if part is not None:
            current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            return f"{current}: {exc.strerror}"
        if stat.S_ISLNK(info.st_mode):
            return f"{current} is a symlink"
        if not stat.S_ISDIR(info.st_mode):
            return f"{current} is not a directory"
        if info.st_uid not in trusted_owners:
            return f"{current} is owned by uid {info.st_uid}"
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            return f"{current} is writable by other users"
    return None


def _prepare_broker_socket_dir(
    sock_dir: Path,
    uid: int,
    gid: int,
    *,
    trusted_owners: frozenset[int] = frozenset({0}),
) -> bool:
    """Create (or adopt) the broker's private socket directory, fail-closed.

    The broker must own it to bind its socket, the gateway gid needs traverse
    (0750), and nothing else may write it. This root step must never follow a
    symlink or re-own a shared directory (a socket configured as
    ``/tmp/broker.sock`` used to turn ``/tmp`` into 0750): the directory is
    either created here, or adopted only when it already belongs to the broker
    uid and is not group/other-writable, and its owner and mode are set through
    an ``O_NOFOLLOW | O_DIRECTORY`` fd.
    """
    if not sock_dir.is_absolute():
        print(f"warning: broker socket dir {sock_dir} is not absolute", file=sys.stderr)
        return False
    reason = _untrusted_component(sock_dir.parent, trusted_owners)
    if reason:
        print(
            f"warning: refusing broker socket dir {sock_dir}: {reason}; point "
            "GATEWAY_ISOLATION_BROKER_SOCKET into a dedicated directory under a "
            "root-owned path such as /run",
            file=sys.stderr,
        )
        return False
    created = True
    try:
        os.mkdir(sock_dir, 0o700)
    except FileExistsError:
        created = False
    except OSError as exc:
        print(f"warning: cannot create {sock_dir}: {exc}", file=sys.stderr)
        return False
    try:
        fd = os.open(sock_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        print(
            f"warning: refusing broker socket dir {sock_dir}: a symlink or not a "
            f"directory ({exc.strerror})",
            file=sys.stderr,
        )
        return False
    try:
        info = os.fstat(fd)
        if not created and (info.st_uid != uid or info.st_mode & 0o022):
            print(
                f"warning: refusing pre-existing broker socket dir {sock_dir} "
                f"(uid {info.st_uid}, mode {stat.S_IMODE(info.st_mode):o}): not a "
                f"private directory of the broker uid {uid}",
                file=sys.stderr,
            )
            return False
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o750)
    except OSError as exc:
        print(f"warning: cannot prepare {sock_dir}: {exc}", file=sys.stderr)
        return False
    finally:
        os.close(fd)
    return True


def _broker_wait_seconds() -> float:
    """``CLAUDE_ISOLATION_BROKER_WAIT_SECONDS``, parsed exactly like the gateway.

    Blank gives the default. Anything that is not a finite number warns and
    gives the default (``inf``/``1e400`` would wait forever, ``nan`` used to
    read as 0). Otherwise the value is clamped to ``[0, MAX_BROKER_WAIT_SECONDS]``,
    with a warning above the cap.
    """
    raw = (os.environ.get(BROKER_WAIT_ENV) or "").strip()
    if not raw:
        return DEFAULT_BROKER_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        print(
            f"warning: ignoring {BROKER_WAIT_ENV}={raw!r}: not a finite number of "
            f"seconds; using {DEFAULT_BROKER_WAIT_SECONDS}",
            file=sys.stderr,
        )
        return DEFAULT_BROKER_WAIT_SECONDS
    if value > MAX_BROKER_WAIT_SECONDS:
        print(
            f"warning: {BROKER_WAIT_ENV}={raw!r} is above the "
            f"{MAX_BROKER_WAIT_SECONDS}s cap; using the cap",
            file=sys.stderr,
        )
        return MAX_BROKER_WAIT_SECONDS
    return max(0.0, value)


def _wait_for_broker(
    sock: str, pid: int, timeout: float = DEFAULT_BROKER_WAIT_SECONDS
) -> bool:
    """Wait (bounded) until the forked broker accepts on *sock*.

    The gateway probes the broker once at startup and caches the answer, so it
    must not race the broker's bind. Polls ``connect()``; an early broker exit
    ends the wait at once. On failure only warns: the gateway's own startup
    check still refuses uid mode (fail-closed).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            done, status = pid, 0
        if done == pid:
            print(
                "warning: isolation broker exited during startup (exit code "
                f"{os.waitstatus_to_exitcode(status)}); the gateway will refuse "
                "uid mode",
                file=sys.stderr,
            )
            return False
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(sock)
            return True
        except OSError:
            pass
        finally:
            probe.close()
        if time.monotonic() >= deadline:
            print(
                f"warning: isolation broker not accepting on {sock} after "
                f"{timeout:.1f}s; the gateway will refuse uid mode",
                file=sys.stderr,
            )
            return False
        time.sleep(0.05)


def launch_isolation_broker(app_uid: int, app_gid: int) -> None:
    """Fork the resident broker before privilege drop. No-op unless uid mode.

    Generates the ticket secret (if unset) into the environment the broker
    inherits, and exports the socket path and the real CLI for the gateway
    (exec'd next). :func:`main` then moves the secret out of that environment
    into a pipe for the gateway (:func:`hand_off_ticket_secret`), so the gateway
    can mint tickets the broker verifies.
    """
    if not _uid_mode():
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
        sock = DEFAULT_BROKER_SOCKET
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

    # The broker runs under a dedicated non-root uid but keeps the gateway's gid
    # by default, so the socket it binds (mode 0660, group = that gid) is
    # connectable by the gateway-uid launcher without a world-writable socket.
    broker_uid = _parse_id(BROKER_UID_ENV, DEFAULT_BROKER_UID)
    broker_gid = _parse_id(BROKER_GID_ENV, app_gid)
    # The broker pins its only legitimate client to the gateway's uid.
    os.environ["GATEWAY_ISOLATION_PEER_UID"] = str(app_uid)

    # The broker (dropped to broker_uid) must own the dir to bind its socket;
    # the gateway gid gets traverse (x) to reach it. Not world.
    if not _prepare_broker_socket_dir(Path(sock).parent, broker_uid, broker_gid):
        return

    pid = os.fork()
    if pid > 0:
        # Hold the gateway back until the broker accepts: its one-shot startup
        # probe would otherwise race the bind and cache "unavailable".
        _wait_for_broker(sock, pid, _broker_wait_seconds())
        return

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


# The gateway's copy of the ticket secret travels in an inherited pipe named by
# TICKET_SECRET_FD_ENV, not in the environment (uid_isolation reads it at
# startup: capture_ticket_secret).
TICKET_SECRET_ENV = "GATEWAY_ISOLATION_TICKET_SECRET"
TICKET_SECRET_FD_ENV = "GATEWAY_ISOLATION_TICKET_SECRET_FD"
# Linux's PIPE_BUF, and a pipe holds at least one page: a write this small into
# a fresh pipe is atomic and never blocks, though nothing reads it until the
# gateway starts. uid_isolation refuses a pipe holding more.
MAX_TICKET_SECRET_BYTES = 4096


def hand_off_ticket_secret() -> None:
    """Move the ticket secret from the environment into a pipe for the gateway.

    Everything the image CMD runs before uvicorn (the ``sh -c`` wrapper,
    ``GATEWAY_STARTUP_SCRIPT``, ``install_plugins.py`` and its git and
    ``claude plugin`` children) would otherwise inherit the secret while the
    broker already accepts tickets signed with it, and uvicorn's exec-time
    environment would keep a copy. Instead the secret is written to a pipe
    whose write end is closed here; only the read end is inherited across the
    exec, and the gateway reads and closes it at startup. The variable names
    the pipe's number, device and inode (``<fd>:<dev>:<ino>``), so a process
    that inherits only the variable (a ``uvicorn --workers``/``--reload``
    worker) never reads a descriptor of its own by mistake. The CMD steps still
    inherit the descriptor: one that reads it by accident consumes the secret,
    the gateway then finds the pipe empty and uid mode refuses to start. A step
    that means harm could read it and write it back unnoticed, but it runs as
    the app uid, which can already rewrite the gateway's code: this is hygiene,
    not a boundary. Python ``subprocess`` children (git, ``claude plugin``)
    never get it, since they close inherited descriptors.

    Runs for root and non-root starts alike, whenever the variable holds a
    secret: the one :func:`launch_isolation_broker` generated or an operator's.
    The broker was forked before this and keeps its own copy. A secret too large
    to write without blocking stops the container rather than exec the command
    with it in the environment: uid mode needs the secret, and the default one
    is 64 characters.
    """
    secret = os.environ.get(TICKET_SECRET_ENV)
    if secret is None or not secret.strip():
        return
    data = secret.encode("utf-8", "surrogateescape")
    if len(data) > MAX_TICKET_SECRET_BYTES:
        raise SystemExit(
            f"{TICKET_SECRET_ENV} is {len(data)} bytes; the entrypoint hands it "
            f"to the gateway through a pipe and accepts at most "
            f"{MAX_TICKET_SECRET_BYTES}. Use a shorter secret, or leave it unset "
            "so uid mode generates one."
        )
    import fcntl

    try:
        read_fd, write_fd = os.pipe()
    except OSError as exc:
        raise SystemExit(f"cannot hand {TICKET_SECRET_ENV} to the gateway: {exc}")
    try:
        try:
            os.set_blocking(write_fd, False)
            if os.write(write_fd, data) != len(data):
                raise OSError(errno.EAGAIN, "short write")
        finally:
            os.close(write_fd)
        if read_fd <= 2:
            # A closed stdin/stdout/stderr gave the pipe a stdio number: the
            # command would read (or write) the secret as one of its streams.
            moved = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 3)
            os.close(read_fd)
            read_fd = moved
        os.set_inheritable(read_fd, True)
        pipe = os.fstat(read_fd)
    except OSError as exc:
        try:
            os.close(read_fd)
        except OSError:
            pass
        raise SystemExit(f"cannot hand {TICKET_SECRET_ENV} to the gateway: {exc}")
    # uid_isolation._parse_secret_fd reads this exact shape.
    os.environ[TICKET_SECRET_FD_ENV] = f"{read_fd}:{pipe.st_dev}:{pipe.st_ino}"
    del os.environ[TICKET_SECRET_ENV]


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("no command provided")

    uid = _parse_id("APP_UID", DEFAULT_UID)
    gid = _parse_id("APP_GID", DEFAULT_GID)

    if os.geteuid() == 0:
        skip_owners = _session_owned_uids(uid)
        prepare_writable_paths(uid=uid, gid=gid, skip_owners=skip_owners)
        if _uid_mode():
            # What the session uids need, before the broker that runs them.
            provision_session_accounts(uid, gid)
            prepare_isolation_dirs(uid=uid, gid=gid, skip_owners=skip_owners)
        launch_isolation_broker(uid, gid)  # before the drop; no-op unless uid mode
        drop_privileges(uid, gid)

    hand_off_ticket_secret()
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
