"""Best-effort per-user workspace storage quota helpers.

The quota is intentionally defined over the *user root* (``<base>/<user>``), not
one backend directory, so changing Claude's on-disk alias or enabling another
backend cannot create a fresh bucket. Usage is logical regular-file bytes across
all backend directories below that root.

This is a gateway-level *soft* quota, not a filesystem project quota. File-API
upload/copy paths can preflight mutations inside their process-local quota lock.
Agent hooks use the same accounting for best-effort projected-size checks, but do
not reserve bytes between hook approval and the later tool commit, so concurrent
sessions or direct subprocess writers can race past the threshold. Accounting
failures are never treated as zero usage: benign concurrent disappearance is
skipped, while unreadable/I/O-failed subtrees raise an explicit error so growth
paths can fail closed. Deployments that need an unbreakable byte ceiling should
use a filesystem quota in addition to this policy.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from fastapi import HTTPException

_MIB = 1024 * 1024
_ENV_NAME = "USER_WORKSPACE_QUOTA_MB"


class WorkspaceQuotaConfigError(ValueError):
    """Raised when ``USER_WORKSPACE_QUOTA_MB`` is not a non-negative integer."""


class WorkspaceQuotaAccountingError(HTTPException):
    """Fail-closed 503 when workspace usage cannot be measured safely.

    This exception is intentionally HTTP-aware because the same accounting helpers
    run inside the file API's threadpool. Letting the error propagate as a FastAPI
    ``HTTPException`` guarantees every quota-dependent route fails closed without
    duplicating catch/translation logic around each scan. Non-HTTP callers still
    receive an exception and therefore cannot silently treat failed accounting as
    zero usage.
    """

    def __init__(self, path: Path, cause: OSError) -> None:
        self.path = Path(path)
        self.errno = getattr(cause, "errno", None)
        super().__init__(status_code=503, detail=self.as_detail())

    def as_detail(self) -> dict:
        return {
            "error": {
                "message": "Workspace storage quota could not be measured safely.",
                "type": "service_unavailable",
                "code": "workspace_quota_accounting_unavailable",
            }
        }


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
        """Return the gateway's standard OpenAI-style HTTP error envelope.

        ``src.main`` passes through ``detail`` unchanged when ``detail['error']``
        is already an object. Keeping the quota metadata inside that object gives
        production clients the same shape as every other gateway error while a
        bare FastAPI router test still sees the data under ``detail.error``.
        """
        return {
            "error": {
                "message": "Workspace storage quota exceeded.",
                "type": "insufficient_storage",
                "code": "workspace_quota_exceeded",
                "used_bytes": self.used_bytes,
                "limit_bytes": self.limit_bytes,
                "projected_bytes": self.projected_bytes,
                "added_bytes": self.added_bytes,
                "reclaimed_bytes": self.reclaimed_bytes,
            }
        }


def workspace_quota_env_limit_bytes() -> int:
    """Return the startup quota from ``USER_WORKSPACE_QUOTA_MB`` in bytes.

    Keep the strict environment validation separate from the effective getter so
    runtime-config can use this exact value as its reset/restart baseline without
    duplicating policy. The env uses MiB-sized units despite the conventional
    ``_MB`` spelling: ``500`` means ``500 * 1024 * 1024`` bytes.
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


def workspace_quota_limit_bytes() -> int:
    """Return the effective per-user quota in bytes; ``0`` means unlimited.

    ``USER_WORKSPACE_QUOTA_MB`` seeds the startup value. Admin runtime-config may
    override it in bytes, matching the existing workspace upload-limit contract.
    The local import avoids a module cycle while keeping every quota consumer on
    one effective source of truth.
    """

    from src.runtime_config import get_workspace_quota_bytes

    return max(0, get_workspace_quota_bytes())


def _accounting_error(path: Path, exc: OSError) -> WorkspaceQuotaAccountingError:
    return WorkspaceQuotaAccountingError(path, exc)


def _iter_regular_files(root: Path) -> Iterable[os.stat_result]:
    """Yield lstat results for regular files below *root*, never following links.

    Concurrent disappearance is benign: an entry that is gone by the time we
    inspect it contributes no current bytes. Any other stat/scandir failure could
    hide real storage, so it is surfaced rather than silently under-counted.
    """

    stack = [Path(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    except OSError as exc:
                        raise _accounting_error(Path(entry.path), exc) from exc
                    mode = st.st_mode
                    if stat.S_ISLNK(mode):
                        continue
                    if stat.S_ISDIR(mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(mode):
                        yield st
        except (FileNotFoundError, NotADirectoryError):
            # The directory disappeared or stopped being a directory after its
            # parent was scanned; the current tree legitimately no longer owns it.
            continue
        except WorkspaceQuotaAccountingError:
            raise
        except OSError as exc:
            raise _accounting_error(directory, exc) from exc


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
    except OSError as exc:
        raise _accounting_error(path, exc) from exc

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
    except OSError as exc:
        raise _accounting_error(path, exc) from exc
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
    Accounting failures propagate so callers can fail closed instead of treating
    an unreadable subtree as zero bytes.
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
