"""Best-effort per-user workspace storage quota helpers.

The quota is intentionally defined over the *user root* (``<base>/<user>``), not
one backend directory, so changing Claude's on-disk alias or enabling another
backend cannot create a fresh bucket. Usage is logical regular-file bytes across
all backend directories below that root.

This is a gateway-level *soft* quota, not a filesystem project quota. Gateway
write paths can preflight mutations exactly, while an arbitrary subprocess (most
notably Claude's Bash tool) can still grow the workspace between checks. Agent
hooks use the same accounting to refuse deterministic writes and to surface an
over-quota state; deployments that need an unbreakable byte ceiling should use a
filesystem quota in addition to this policy.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

_MIB = 1024 * 1024
_ENV_NAME = "USER_WORKSPACE_QUOTA_MB"


class WorkspaceQuotaConfigError(ValueError):
    """Raised when ``USER_WORKSPACE_QUOTA_MB`` is not a non-negative integer."""


@dataclass(frozen=True)
class WorkspaceQuotaSnapshot:
    used_bytes: int
    limit_bytes: int

    @property
    def enabled(self) -> bool:
        return self.limit_bytes > 0

    @property
    def remaining_bytes(self) -> Optional[int]:
        if not self.enabled:
            return None
        return max(0, self.limit_bytes - self.used_bytes)

    @property
    def over_quota(self) -> bool:
        return self.enabled and self.used_bytes > self.limit_bytes

    def as_dict(self) -> dict:
        return {
            "used_bytes": self.used_bytes,
            "limit_bytes": self.limit_bytes,
            "remaining_bytes": self.remaining_bytes,
            "enabled": self.enabled,
            "over_quota": self.over_quota,
        }


class WorkspaceQuotaExceeded(Exception):
    """A proposed growth would put a user root above its configured quota."""

    def __init__(
        self,
        *,
        used_bytes: int,
        limit_bytes: int,
        added_bytes: int,
        reclaimed_bytes: int = 0,
    ) -> None:
        self.used_bytes = max(0, int(used_bytes))
        self.limit_bytes = max(0, int(limit_bytes))
        self.added_bytes = max(0, int(added_bytes))
        self.reclaimed_bytes = max(0, int(reclaimed_bytes))
        self.projected_bytes = max(
            0, self.used_bytes - self.reclaimed_bytes + self.added_bytes
        )
        super().__init__(
            "workspace quota exceeded: "
            f"used={self.used_bytes} limit={self.limit_bytes} "
            f"projected={self.projected_bytes}"
        )

    def as_detail(self) -> dict:
        return {
            "error": "workspace_quota_exceeded",
            "used_bytes": self.used_bytes,
            "limit_bytes": self.limit_bytes,
            "projected_bytes": self.projected_bytes,
            "added_bytes": self.added_bytes,
            "reclaimed_bytes": self.reclaimed_bytes,
        }


def workspace_quota_limit_bytes() -> int:
    """Return the configured per-user quota in bytes; ``0`` means unlimited.

    The environment uses MiB-sized units despite the conventional ``_MB`` spelling
    used elsewhere in the project: ``500`` means ``500 * 1024 * 1024`` bytes.
    Invalid or negative values are configuration errors rather than silently
    disabling a resource guard.
    """

    raw = os.getenv(_ENV_NAME, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkspaceQuotaConfigError(
            f"{_ENV_NAME} must be a non-negative integer (MiB), got {raw!r}"
        ) from exc
    if value < 0:
        raise WorkspaceQuotaConfigError(
            f"{_ENV_NAME} must be a non-negative integer (MiB), got {raw!r}"
        )
    return value * _MIB


def _iter_regular_files(root: Path) -> Iterable[os.stat_result]:
    """Yield lstat results for regular files below *root*, never following links."""

    stack = [Path(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except (FileNotFoundError, PermissionError, OSError):
                        # Workspaces can mutate concurrently (agent + file UI).
                        # A disappearing/unreadable entry should not turn a usage
                        # query into a 500; the next scan observes the new state.
                        continue
                    mode = st.st_mode
                    if stat.S_ISLNK(mode):
                        continue
                    if stat.S_ISDIR(mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(mode):
                        yield st
        except (FileNotFoundError, NotADirectoryError):
            continue


def logical_size_bytes(path: Path) -> int:
    """Logical regular-file bytes at *path*, without following symlinks.

    Hard-linked files are counted once per inode. Special files and symlinks are
    zero-byte for quota accounting; following either could block or escape the
    user root and would not represent storage owned by this workspace anyway.
    """

    path = Path(path)
    try:
        st = path.lstat()
    except FileNotFoundError:
        return 0

    if stat.S_ISLNK(st.st_mode):
        return 0
    if stat.S_ISREG(st.st_mode):
        return st.st_size
    if not stat.S_ISDIR(st.st_mode):
        return 0

    seen: set[tuple[int, int]] = set()
    total = 0
    for item in _iter_regular_files(path):
        key = (item.st_dev, item.st_ino)
        if key in seen:
            continue
        seen.add(key)
        total += item.st_size
    return total


def copy_growth_bytes(path: Path) -> int:
    """Bytes a normal file/directory copy would add at a new destination.

    Unlike :func:`logical_size_bytes`, hard-linked source names are counted
    separately because ``shutil.copytree``/``copyfile`` materialize each path as
    a new destination file. Symlinks remain links and therefore contribute no
    regular-file payload bytes.
    """

    path = Path(path)
    try:
        st = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISLNK(st.st_mode):
        return 0
    if stat.S_ISREG(st.st_mode):
        return st.st_size
    if not stat.S_ISDIR(st.st_mode):
        return 0
    return sum(item.st_size for item in _iter_regular_files(path))


def quota_snapshot(user_root: Path) -> WorkspaceQuotaSnapshot:
    """Return current usage and configured limit for one named user's root."""

    return WorkspaceQuotaSnapshot(
        used_bytes=logical_size_bytes(user_root),
        limit_bytes=workspace_quota_limit_bytes(),
    )


def ensure_growth_fits(
    user_root: Path,
    *,
    added_bytes: int,
    reclaimed_bytes: int = 0,
) -> WorkspaceQuotaSnapshot:
    """Raise if a mutation's projected usage would exceed the user quota.

    ``reclaimed_bytes`` is used for overwrite semantics: replacing a 10 MiB file
    with an 8 MiB file must still be allowed when the workspace is at its limit.
    It is clamped to current usage so a stale caller cannot manufacture negative
    projected usage. When quota is disabled this fast-path does not walk the
    filesystem, so the feature has no per-write scan cost unless configured.
    """

    limit = workspace_quota_limit_bytes()
    if limit <= 0:
        return WorkspaceQuotaSnapshot(used_bytes=0, limit_bytes=0)

    used = logical_size_bytes(user_root)
    snapshot = WorkspaceQuotaSnapshot(used_bytes=used, limit_bytes=limit)
    reclaimed = min(snapshot.used_bytes, max(0, int(reclaimed_bytes)))
    added = max(0, int(added_bytes))
    projected = snapshot.used_bytes - reclaimed + added
    if projected > snapshot.limit_bytes:
        raise WorkspaceQuotaExceeded(
            used_bytes=snapshot.used_bytes,
            limit_bytes=snapshot.limit_bytes,
            added_bytes=added,
            reclaimed_bytes=reclaimed,
        )
    return snapshot
