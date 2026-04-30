"""Admin service — filesystem operations, config redaction, and security logic.

Keeps route handlers thin by centralising file I/O, path validation,
secret masking, and atomic-write semantics in one place.
"""

import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml  # type: ignore[import-untyped]

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


# ---------------------------------------------------------------------------
# Session message history (read-only, no TTL refresh)
# ---------------------------------------------------------------------------


def get_session_messages(
    session_id: str,
    truncate: int = 500,
) -> Optional[List[Dict[str, Any]]]:
    """Return message history for a session without refreshing TTL.

    Returns ``None`` when the session does not exist.  Content is truncated
    to *truncate* characters in the response; set to ``0`` for full content.

    Content is truncated to *truncate* characters for display.
    """
    from src.session_manager import session_manager

    session = session_manager.peek_session(session_id)
    if session is None:
        return None

    messages = session.get_all_messages()  # returns a shallow copy
    result: List[Dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        content = msg.content
        display = str(content) if content else ""

        truncated = False
        if truncate > 0 and len(display) > truncate:
            display = display[:truncate]
            truncated = True

        result.append(
            {
                "index": idx,
                "role": msg.role,
                "content": display,
                "truncated": truncated,
                "name": msg.name,
            }
        )

    return result


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
        if key == "MCP_CONFIG" or _SECRET_PATTERNS.search(key):
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
        "GATEWAY_HOST",
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


# ---------------------------------------------------------------------------
# Backend health & auth inspection
# ---------------------------------------------------------------------------


async def get_backends_health() -> List[Dict[str, Any]]:
    """Return detailed health and auth info for every known backend."""
    from src.backends.base import BackendRegistry
    from src.auth import auth_manager

    descriptors = BackendRegistry.all_descriptors()
    backend_names = sorted(set(descriptors.keys()) | {"claude"})

    results: List[Dict[str, Any]] = []
    for name in backend_names:
        info: Dict[str, Any] = {"name": name, "registered": False}
        client = None

        # Registration status
        if BackendRegistry.is_registered(name):
            info["registered"] = True
            client = BackendRegistry.get(name)
            info["models"] = client.supported_models()

            # Active health check via verify()
            try:
                info["healthy"] = await client.verify()
            except Exception as e:
                info["healthy"] = False
                info["health_error"] = str(e)
        else:
            desc = descriptors.get(name)
            info["models"] = list(desc.models) if desc else []
            info["healthy"] = False
            info["health_error"] = "Backend not registered"

        # Auth details
        try:
            provider = auth_manager.get_provider(name)
            auth_status = provider.validate()
            info["auth"] = {
                "valid": auth_status.get("valid", False),
                "method": auth_status.get("config", {}).get("auth_method", "unknown"),
                "errors": auth_status.get("errors", []),
                "env_vars": list(provider.build_env().keys()),
                "isolation_vars": provider.get_isolation_vars(),
            }
        except Exception as e:
            info["auth"] = {"valid": False, "method": "unknown", "errors": [str(e)]}

        metadata: Dict[str, Any] = {}
        runtime_metadata = getattr(client, "runtime_metadata", None) if client else None
        if callable(runtime_metadata):
            try:
                metadata = runtime_metadata()
            except Exception:
                logger.warning(
                    "Failed to collect backend runtime metadata for %s",
                    name,
                    exc_info=True,
                )
        info["metadata"] = metadata

        results.append(info)

    return results


def get_sandbox_config() -> Dict[str, Any]:
    """Return sandbox and permission mode configuration."""
    return {
        "permission_mode": os.getenv("PERMISSION_MODE", "default"),
        "sandbox_enabled": os.getenv("CLAUDE_SANDBOX_ENABLED", "true"),
        "sandbox_auto_allow_bash": os.getenv("CLAUDE_SANDBOX_AUTO_ALLOW_BASH", "false"),
        "metadata_env_allowlist": sorted(
            k.strip() for k in os.getenv("METADATA_ENV_ALLOWLIST", "").split(",") if k.strip()
        ),
    }


def get_tools_registry() -> Dict[str, Any]:
    """Return available tools and their configuration."""
    result: Dict[str, Any] = {"backends": {}}

    try:
        from src.backends.claude.constants import CLAUDE_TOOLS, DEFAULT_ALLOWED_TOOLS

        result["backends"]["claude"] = {
            "all_tools": CLAUDE_TOOLS,
            "default_allowed": DEFAULT_ALLOWED_TOOLS,
        }
    except ImportError:
        pass

    # MCP tool patterns
    try:
        from src.mcp_config import get_mcp_servers, get_mcp_tool_patterns

        servers = get_mcp_servers()
        if servers:
            result["mcp_tools"] = get_mcp_tool_patterns(servers)
        else:
            result["mcp_tools"] = []
    except Exception:
        result["mcp_tools"] = []

    return result


def get_mcp_servers_detail() -> List[Dict[str, Any]]:
    """Return detailed MCP server configuration (names, types, tool patterns)."""
    try:
        from src.mcp_config import get_mcp_servers, get_mcp_tool_patterns

        servers = get_mcp_servers()
        if not servers:
            return []

        patterns = get_mcp_tool_patterns(servers)
        result = []
        for name, config in servers.items():
            server_patterns = [p for p in patterns if p.startswith(f"mcp__{name}__")]
            result.append(
                {
                    "name": name,
                    "type": config.get("type", "unknown"),
                    "tools": server_patterns,
                    "config_keys": [k for k in config.keys() if k not in ("type",)],
                }
            )
        return result
    except Exception:
        return []


def get_session_detail(session_id: str) -> Optional[Dict[str, Any]]:
    """Return detailed session metadata (beyond message history)."""
    from src.session_manager import session_manager

    session = session_manager.peek_session(session_id)
    if session is None:
        return None

    return {
        "session_id": session.session_id,
        "backend": session.backend,
        "turn_counter": session.turn_counter,
        "ttl_minutes": session.ttl_minutes,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_accessed": session.last_accessed.isoformat() if session.last_accessed else None,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "message_count": len(session.messages),
        "has_system_prompt": session.base_system_prompt is not None,
    }


def export_session_json(session_id: str) -> Optional[Dict[str, Any]]:
    """Export full session data as JSON-serializable dict."""
    from src.session_manager import session_manager

    session = session_manager.peek_session(session_id)
    if session is None:
        return None

    messages = []
    for msg in session.get_all_messages():
        content = msg.content
        display = str(content) if content else ""

        messages.append({"role": msg.role, "content": display, "name": msg.name})

    return {
        "session_id": session.session_id,
        "backend": session.backend,
        "turn_counter": session.turn_counter,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Skills management
# ---------------------------------------------------------------------------

# Valid skill name: lowercase alphanumeric + hyphens, must start with a letter/digit
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_SKILL_DIR_PREFIX = ".claude/skills/"


def _parse_skill_frontmatter(content: str) -> Dict[str, Any]:
    """Parse YAML frontmatter from a SKILL.md file.

    Returns parsed metadata dict.  Falls back to empty dict on parse errors.
    """
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        meta = yaml.safe_load(parts[1])
        return meta if isinstance(meta, dict) else {}
    except yaml.YAMLError:
        return {}


def list_skills() -> List[Dict[str, Any]]:
    """List all skills in ``.claude/skills/``.

    Scans for ``SKILL.md`` files inside each subdirectory and parses their
    YAML frontmatter for metadata.
    """
    root = get_workspace_root()
    skills_dir = root / ".claude" / "skills"
    result: List[Dict[str, Any]] = []

    if not skills_dir.is_dir():
        return result

    # Skip if skills_dir itself or any ancestor (.claude) is a symlink
    if skills_dir.is_symlink() or _has_symlink_ancestor(skills_dir, root):
        return result

    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.is_file() or skill_file.is_symlink():
            continue
        try:
            stat = skill_file.stat()
            if stat.st_size > MAX_FILE_SIZE:
                continue
            raw = skill_file.read_bytes()
            text = raw.decode("utf-8")
            meta = _parse_skill_frontmatter(text)
            result.append(
                {
                    "name": child.name,
                    "description": meta.get("description", ""),
                    "version": meta.get("metadata", {}).get("version", "")
                    if isinstance(meta.get("metadata"), dict)
                    else "",
                    "author": meta.get("metadata", {}).get("author", "")
                    if isinstance(meta.get("metadata"), dict)
                    else "",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
        except (OSError, UnicodeDecodeError):
            continue

    return result


def get_skill(name: str) -> Tuple[Dict[str, Any], str, str]:
    """Read a single skill.

    Returns ``(metadata, content, etag)`` where *metadata* is the parsed
    frontmatter and *content* is the full file text.

    Raises ``FileNotFoundError`` if the skill does not exist.
    Raises ``ValueError`` for invalid skill names.
    """
    _validate_skill_name(name)
    rel_path = f"{_SKILL_DIR_PREFIX}{name}/SKILL.md"
    target = validate_file_path(rel_path)
    validate_file_for_read(target)

    raw = target.read_bytes()
    text = raw.decode("utf-8")
    meta = _parse_skill_frontmatter(text)
    etag = compute_etag(raw)
    return meta, text, etag


def create_or_update_skill(
    name: str,
    content: str,
    expected_etag: Optional[str] = None,
) -> Tuple[str, bool]:
    """Create or update a skill.

    Returns ``(new_etag, created)`` where *created* is ``True`` when a new
    skill directory was made.

    Raises ``ValueError`` for invalid names, oversized content, or ETag
    mismatches.
    """
    _validate_skill_name(name)
    rel_path = f"{_SKILL_DIR_PREFIX}{name}/SKILL.md"
    target = validate_file_path(rel_path)

    created = not target.is_file()
    new_etag = write_file(rel_path, content, expected_etag)
    return new_etag, created


def delete_skill(name: str) -> None:
    """Delete a skill directory and all its contents.

    Raises ``FileNotFoundError`` if the skill does not exist.
    Raises ``ValueError`` for invalid skill names.
    """
    _validate_skill_name(name)
    root = get_workspace_root()
    skill_dir = root / ".claude" / "skills" / name

    # Security: reject symlink ancestors (.claude or .claude/skills being symlinks)
    if _has_symlink_ancestor(skill_dir, root):
        raise ValueError("Symlinks in path ancestors are not allowed")

    # Security: ensure the path is within the workspace
    try:
        skill_dir.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError("Path escapes workspace root")

    if not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill not found: {name}")

    if skill_dir.is_symlink():
        raise ValueError("Symlinks are not allowed")

    shutil.rmtree(skill_dir)
    logger.info(f"Admin: deleted skill '{name}'")


def _validate_skill_name(name: str) -> None:
    """Raise ``ValueError`` if *name* is not a valid skill identifier."""
    if not name or not _SKILL_NAME_RE.match(name):
        raise ValueError(
            f"Invalid skill name: '{name}'. "
            "Use lowercase letters, digits, and hyphens only (must start with a letter or digit)."
        )
