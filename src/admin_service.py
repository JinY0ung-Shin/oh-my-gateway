"""Admin service — filesystem operations, config redaction, and security logic.

Keeps route handlers thin by centralising file I/O, path validation,
secret masking, and atomic-write semantics in one place.
"""

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path security
# ---------------------------------------------------------------------------

# Strict allowlist of directories/files that the admin API may touch.
# Paths are relative to the workspace root (CLAUDE_CWD).
# Directory entries end with "/"; file entries do not.
_ALLOWED_DIRS: Tuple[str, ...] = (
    ".claude/agents/",
    ".claude/skills/",
    ".claude/commands/",
)
_ALLOWED_FILES: Tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    "CLAUDE.md",
)
# Combined for listing iteration
_ALLOWED_PREFIXES: Tuple[str, ...] = _ALLOWED_DIRS + _ALLOWED_FILES

# Maximum file size the admin API will read or write (256 KB).
MAX_FILE_SIZE = 256 * 1024

# Allowed file extensions for editing.
_ALLOWED_EXTENSIONS = {".md", ".json", ".yaml", ".yml", ".txt", ".toml"}

# Secrets that must be redacted in config output.
_SECRET_PATTERNS = re.compile(
    r"(ANTHROPIC_AUTH_TOKEN|API_KEY|ADMIN_API_KEY|OPENAI_API_KEY"
    r"|SECRET|PASSWORD|TOKEN|CREDENTIAL)",
    re.IGNORECASE,
)


def _resolve_workspace_root() -> Optional[Path]:
    """Return the effective workspace root used by backends at runtime.

    Checks ``CLAUDE_CWD`` first, then falls back to any live Claude
    backend instance's ``cwd`` attribute.
    """
    env_cwd = os.getenv("CLAUDE_CWD")
    if env_cwd:
        p = Path(env_cwd)
        if p.is_dir():
            return p.resolve()

    # Fallback: ask the live Claude backend for its cwd
    try:
        from src.backends.base import BackendRegistry

        if BackendRegistry.is_registered("claude"):
            client = BackendRegistry.get("claude")
            if hasattr(client, "cwd"):
                return Path(client.cwd).resolve()
    except Exception:
        pass

    return None


def get_workspace_root() -> Path:
    """Return the workspace root or raise."""
    root = _resolve_workspace_root()
    if root is None:
        raise RuntimeError(
            "Workspace root not available. Set CLAUDE_CWD or ensure a backend is registered."
        )
    return root


# ---------------------------------------------------------------------------
# Path validation (security-critical)
# ---------------------------------------------------------------------------


def validate_file_path(relative_path: str) -> Path:
    """Validate and resolve a relative path against the workspace root.

    Raises ``ValueError`` for any disallowed access:
    * path traversal (``..``)
    * symlinks (rejected unconditionally to prevent escape and allowlist bypass)
    * outside the allowlist
    * binary / oversized files
    """
    # Reject backslashes — on POSIX they are valid filename characters, not
    # separators, so Path('a\\b') creates a single component 'a\b' rather
    # than 'a/b'.  This mismatch between allowlist matching (which normalises
    # backslashes to '/') and filesystem access would let an attacker bypass
    # the allowlist on Linux.
    if "\\" in relative_path:
        raise ValueError("Backslash paths are not allowed")

    # Block obvious traversal attempts before touching the filesystem
    if ".." in relative_path.split("/"):
        raise ValueError("Path traversal is not allowed")

    # Normalise and ensure relative
    clean = Path(relative_path)
    if clean.is_absolute():
        raise ValueError("Absolute paths are not allowed")

    root = get_workspace_root()
    raw_target = root / clean

    # Reject symlinks unconditionally — prevents both escape from workspace
    # root (sibling-prefix attack) and allowlist bypass (symlink inside an
    # allowed dir pointing to a non-allowed file within the workspace).
    if raw_target.is_symlink():
        raise ValueError("Symlinks are not allowed")
    # Also check each parent component for symlinks
    for parent in raw_target.relative_to(root).parents:
        if parent != Path(".") and (root / parent).is_symlink():
            raise ValueError("Symlinks in path components are not allowed")

    target = raw_target.resolve()

    # Must still be under root after resolution (use is_relative_to to
    # avoid the sibling-prefix false-positive from string startswith).
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("Path escapes workspace root")

    # Must match the allowlist — distinguish exact-file entries from dir prefixes
    rel = str(clean).replace("\\", "/")
    allowed = False
    # Check exact file matches (e.g. "CLAUDE.md", ".claude/settings.json")
    if rel in _ALLOWED_FILES:
        allowed = True
    else:
        # Check directory prefixes (e.g. ".claude/agents/something.md")
        for prefix in _ALLOWED_DIRS:
            if rel.startswith(prefix):
                allowed = True
                break
    if not allowed:
        raise ValueError(f"Path not in allowlist: {rel}")

    return target


def validate_file_for_read(target: Path) -> None:
    """Extra checks before reading a file."""
    if not target.is_file():
        raise FileNotFoundError(f"Not a file: {target.name}")
    if target.stat().st_size > MAX_FILE_SIZE:
        raise ValueError(f"File too large (>{MAX_FILE_SIZE} bytes)")
    if target.suffix not in _ALLOWED_EXTENSIONS:
        raise ValueError(f"File type not allowed: {target.suffix}")


def validate_file_for_write(target: Path, content: str) -> None:
    """Extra checks before writing a file."""
    if len(content.encode()) > MAX_FILE_SIZE:
        raise ValueError(f"Content too large (>{MAX_FILE_SIZE} bytes)")
    if target.suffix not in _ALLOWED_EXTENSIONS:
        raise ValueError(f"File type not allowed for writing: {target.suffix}")
    # JSON validation for settings files
    if target.suffix == ".json":
        import json

        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e


# ---------------------------------------------------------------------------
# File tree
# ---------------------------------------------------------------------------


def _has_symlink_ancestor(path: Path, root: Path) -> bool:
    """Check if any component between *root* and *path* is a symlink."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True  # Outside root — treat as unsafe
    current = root
    for part in rel.parts:
        current = current / part
        if current == path:
            break  # Don't check the target itself (handled separately)
        if current.is_symlink():
            return True
    return False


def list_workspace_files() -> List[Dict[str, Any]]:
    """Return an allowlisted file tree under the workspace root."""
    root = get_workspace_root()
    result: List[Dict[str, Any]] = []

    for prefix in _ALLOWED_PREFIXES:
        target = root / prefix
        # Skip symlinked allowlist roots to prevent leaking external paths
        if target.is_symlink():
            continue
        # Skip entries whose ancestor components (e.g. .claude itself) are symlinks
        if _has_symlink_ancestor(target, root):
            continue
        if target.is_file():
            try:
                stat = target.stat()
                result.append(
                    {
                        "path": prefix,
                        "type": "file",
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    }
                )
            except OSError:
                pass
        elif target.is_dir():
            for child in sorted(target.rglob("*")):
                if not child.is_file():
                    continue
                if child.is_symlink():
                    continue  # Skip symlinks in listings
                # Also check parent components for symlinks
                try:
                    rel_to_target = child.relative_to(target)
                    for parent in rel_to_target.parents:
                        if parent != Path(".") and (target / parent).is_symlink():
                            raise ValueError("symlink parent")
                except ValueError:
                    continue
                if child.suffix not in _ALLOWED_EXTENSIONS:
                    continue
                rel = str(child.relative_to(root)).replace("\\", "/")
                try:
                    stat = child.stat()
                    result.append(
                        {
                            "path": rel,
                            "type": "file",
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        }
                    )
                except OSError:
                    pass

    return result


# ---------------------------------------------------------------------------
# File read / write with ETag support
# ---------------------------------------------------------------------------


def compute_etag(content: bytes) -> str:
    """Compute a strong ETag from file content."""
    return hashlib.sha256(content).hexdigest()[:16]


def read_file(relative_path: str) -> Tuple[str, str]:
    """Read a file and return (content, etag)."""
    target = validate_file_path(relative_path)
    validate_file_for_read(target)
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"File is not valid UTF-8: {target.name}")
    return text, compute_etag(raw)


def write_file(relative_path: str, content: str, expected_etag: Optional[str] = None) -> str:
    """Atomically write a file and return the new ETag.

    If ``expected_etag`` is given, the write is rejected when the current
    file's ETag does not match (optimistic concurrency / ``If-Match``).
    """
    target = validate_file_path(relative_path)
    validate_file_for_write(target, content)

    # Optimistic concurrency check
    if expected_etag is not None and target.is_file():
        current_etag = compute_etag(target.read_bytes())
        if current_etag != expected_etag:
            raise ValueError(
                f"ETag mismatch: expected {expected_etag}, current {current_etag}. "
                "File was modified by another process."
            )

    # Ensure parent directories exist (e.g. new skill directory)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: write to temp file in same directory, then rename
    new_bytes = content.encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    fd_closed = False
    try:
        os.write(fd, new_bytes)
        os.fsync(fd)
        os.close(fd)
        fd_closed = True
        os.replace(tmp_path, target)
    except Exception:
        if not fd_closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    new_etag = compute_etag(new_bytes)
    logger.info(f"Admin: wrote {relative_path} ({len(new_bytes)} bytes, etag={new_etag})")
    return new_etag


# ---------------------------------------------------------------------------
# Config redaction
# ---------------------------------------------------------------------------


def get_redacted_config() -> Dict[str, Any]:
    """Return runtime configuration with secrets masked."""
    from src.constants import (
        DEFAULT_MODEL,
        DEFAULT_MAX_TURNS,
        DEFAULT_TIMEOUT_MS,
        DEFAULT_PORT,
        DEFAULT_HOST,
        MAX_REQUEST_SIZE,
        SESSION_CLEANUP_INTERVAL_MINUTES,
        SESSION_MAX_AGE_MINUTES,
        RATE_LIMITS,
        THINKING_MODE,
        TOKEN_STREAMING,
    )

    def _redact(key: str, value: Any) -> Any:
        if _SECRET_PATTERNS.search(key):
            if value and str(value).strip():
                return "***REDACTED***"
            return "(not set)"
        return value

    # Collect all relevant env-based settings
    env_keys = [
        "DEFAULT_MODEL",
        "DEFAULT_MAX_TURNS",
        "MAX_TIMEOUT",
        "PORT",
        "CLAUDE_WRAPPER_HOST",
        "MAX_REQUEST_SIZE",
        "CORS_ORIGINS",
        "CLAUDE_CWD",
        "DEBUG_MODE",
        "VERBOSE",
        "THINKING_MODE",
        "TOKEN_STREAMING",
        "SESSION_CLEANUP_INTERVAL_MINUTES",
        "SESSION_MAX_AGE_MINUTES",
        "RATE_LIMIT_ENABLED",
        "ADMIN_API_KEY",
        "API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "MCP_CONFIG",
        "CLAUDE_SANDBOX_ENABLED",
        "PERMISSION_MODE",
    ]

    env_snapshot = {}
    for k in env_keys:
        raw = os.getenv(k)
        env_snapshot[k] = _redact(k, raw) if raw else "(not set)"

    # MCP config: show server names only, not full config
    mcp_servers_info = None
    try:
        from src.mcp_config import get_mcp_servers

        servers = get_mcp_servers()
        if servers:
            mcp_servers_info = list(servers.keys())
    except Exception:
        pass

    return {
        "runtime": {
            "default_model": DEFAULT_MODEL,
            "default_max_turns": DEFAULT_MAX_TURNS,
            "timeout_ms": DEFAULT_TIMEOUT_MS,
            "port": DEFAULT_PORT,
            "host": DEFAULT_HOST,
            "max_request_size": MAX_REQUEST_SIZE,
            "thinking_mode": THINKING_MODE,
            "token_streaming": TOKEN_STREAMING,
        },
        "sessions": {
            "cleanup_interval_minutes": SESSION_CLEANUP_INTERVAL_MINUTES,
            "max_age_minutes": SESSION_MAX_AGE_MINUTES,
        },
        "rate_limits": dict(RATE_LIMITS),
        "mcp_servers": mcp_servers_info,
        "environment": env_snapshot,
        "_note": "Values marked ***REDACTED*** contain secrets. "
        "Most settings require server restart to take effect.",
    }
