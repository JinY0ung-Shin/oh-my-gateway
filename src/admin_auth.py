"""Admin authentication — separate from the main API key auth.

Admin endpoints require a dedicated ``ADMIN_API_KEY``.  Authentication uses
a short-lived HttpOnly cookie (``admin_session``) scoped to ``/admin`` with
``SameSite=Strict``.

The admin UI is always enabled.  If ``ADMIN_API_KEY`` is not set the server
refuses to start — this is enforced in ``validate_admin_config()``.

Flow:
1. ``POST /admin/api/login`` — validates ADMIN_API_KEY, sets cookie
2. All ``/admin/api/*`` routes — checked by ``require_admin`` dependency
3. ``POST /admin/api/logout`` — clears cookie

The cookie signing key comes from ``ADMIN_COOKIE_SECRET`` or the shared
``data/admin_cookie_secret`` file (see ``_resolve_cookie_secret``), so a
session survives a restart and is valid on both colors of a blue/green deploy.
"""

import hashlib
import hmac
import logging
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Optional
from fastapi import HTTPException, Request, Response

from src.env_utils import parse_bool_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
ADMIN_SESSION_TTL = int(os.getenv("ADMIN_SESSION_TTL", "3600"))  # 1 hour default

_COOKIE_NAME = "admin_session"

# ---------------------------------------------------------------------------
# Cookie signing secret
# ---------------------------------------------------------------------------
#
# Blue/green deploys run two gateways side by side (docs/zero-downtime-deploy.md
# §5.5), so the key that signs admin cookies must be the same in both or every
# swap logs the admin out. Resolved once per process, on first use:
#
# 1. ``ADMIN_COOKIE_SECRET`` — any non-empty string (hex or not). The key is
#    SHA-256 over a fixed label and the UTF-8 value, so every process given
#    the same string derives the same 32 bytes.
# 2. Else ``data/admin_cookie_secret`` (``ADMIN_COOKIE_SECRET_FILE`` overrides
#    the path): 64 hex chars, created once with O_CREAT|O_EXCL|O_NOFOLLOW 0600;
#    a process that loses the creation race reads the winner's value.
# 3. A file that cannot be created, read or parsed logs a warning and falls back
#    to a per-process random key (sessions then end with the process, as before).

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SECRET_LABEL = b"oh-my-gateway admin cookie v1\0"
_SECRET_FILE_BYTES = 32
# How long a process that lost the creation race waits for the winner's write.
_RACE_READ_ATTEMPTS = 50
_RACE_READ_DELAY = 0.02

_cookie_secret: Optional[bytes] = None
_cookie_secret_lock = threading.Lock()


def cookie_secret_path() -> Path:
    raw = os.getenv("ADMIN_COOKIE_SECRET_FILE", "").strip()
    return Path(raw) if raw else _DATA_DIR / "admin_cookie_secret"


def _derive_env_secret(value: str) -> bytes:
    return hashlib.sha256(_SECRET_LABEL + value.encode("utf-8")).digest()


def _read_secret_file(path: Path) -> Optional[bytes]:
    """The file's key, or ``None`` while it is still empty (creation race)."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise ValueError("not a regular file")
        text = fh.read(4096).decode("ascii").strip()
    if not text:
        return None
    key = bytes.fromhex(text)
    if len(key) < _SECRET_FILE_BYTES:
        raise ValueError(f"key is {len(key)} bytes, need {_SECRET_FILE_BYTES}")
    return key


def _load_or_create_file_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(_SECRET_FILE_BYTES)
    try:
        fd = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        # Already there (or another gateway just won the race): read it,
        # waiting briefly for a winner that has created but not yet written it.
        for _ in range(_RACE_READ_ATTEMPTS):
            key = _read_secret_file(path)
            if key is not None:
                return key
            time.sleep(_RACE_READ_DELAY)
        raise ValueError("file stayed empty")
    try:
        os.write(fd, value.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    logger.info("Admin cookie secret created at %s", path)
    return bytes.fromhex(value)


def _resolve_cookie_secret() -> bytes:
    env_value = os.getenv("ADMIN_COOKIE_SECRET", "")
    if env_value.strip():
        return _derive_env_secret(env_value.strip())
    path = cookie_secret_path()
    try:
        return _load_or_create_file_secret(path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning(
            "Admin cookie secret file %s unusable (%s); using a per-process "
            "random secret: admin sessions end with this process and are not "
            "shared with another gateway instance",
            path,
            exc,
        )
        return secrets.token_bytes(32)


def _get_cookie_secret() -> bytes:
    global _cookie_secret
    if _cookie_secret is None:
        with _cookie_secret_lock:
            if _cookie_secret is None:
                _cookie_secret = _resolve_cookie_secret()
    return _cookie_secret


def _reset_cookie_secret_for_tests() -> None:
    global _cookie_secret
    with _cookie_secret_lock:
        _cookie_secret = None

# ---------------------------------------------------------------------------
# Constant-time comparison
# ---------------------------------------------------------------------------


def _secure_eq(a: Optional[str], b: Optional[str]) -> bool:
    """Constant-time string comparison tolerant of non-ASCII input.

    ``hmac.compare_digest`` raises ``TypeError`` for ``str`` operands that
    contain non-ASCII code points, so a crafted admin key, session cookie, or
    bearer token would surface as a 500 instead of a clean auth failure.
    Comparing the UTF-8 encodings avoids that while staying constant-time.
    ``None`` (e.g. a missing cookie/header) never matches.
    """
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Session token helpers
# ---------------------------------------------------------------------------


def _make_session_token(issued_at: int) -> str:
    """Create an HMAC-signed session token encoding the issue timestamp."""
    payload = f"{issued_at}".encode()
    sig = hmac.new(_get_cookie_secret(), payload, hashlib.sha256).hexdigest()
    return f"{issued_at}.{sig}"


def _verify_session_token(token: str) -> bool:
    """Verify an admin session token's signature and TTL."""
    if not token or "." not in token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    try:
        issued_at = int(parts[0])
    except ValueError:
        return False

    # Check TTL
    if time.time() - issued_at > ADMIN_SESSION_TTL:
        return False

    # Verify HMAC (timing-safe)
    expected = _make_session_token(issued_at)
    return _secure_eq(token, expected)


# ---------------------------------------------------------------------------
# Login / logout helpers
# ---------------------------------------------------------------------------


def validate_admin_config() -> None:
    """Validate admin configuration at startup.

    Raises ``RuntimeError`` if ``ADMIN_API_KEY`` is not set, preventing
    the server from starting without admin protection.
    """
    if not ADMIN_API_KEY:
        raise RuntimeError(
            "ADMIN_API_KEY environment variable is required. "
            "Set it in your .env file or environment before starting the server."
        )


def login(provided_key: str, response: Response) -> dict:
    """Validate the admin key and set an HttpOnly session cookie."""
    if not _secure_eq(provided_key, ADMIN_API_KEY):
        logger.warning("Admin login failed: invalid API key")
        raise HTTPException(status_code=401, detail="Invalid admin API key")

    token = _make_session_token(int(time.time()))
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        path="/admin",
        max_age=ADMIN_SESSION_TTL,
        # Secure by default — the session cookie should only travel over HTTPS.
        # For local HTTP development set ``ADMIN_COOKIE_SECURE=false`` to opt out.
        secure=parse_bool_env("ADMIN_COOKIE_SECURE", "true"),
    )
    logger.info("Admin login successful")
    return {"status": "ok", "ttl": ADMIN_SESSION_TTL}


def logout(response: Response) -> dict:
    """Clear the admin session cookie."""
    response.delete_cookie(key=_COOKIE_NAME, path="/admin", samesite="strict")
    return {"status": "logged_out"}


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def require_admin(request: Request) -> bool:
    """FastAPI dependency that enforces admin authentication.

    Checks valid session cookie OR valid Bearer token in Authorization header.
    Bearer token fallback allows programmatic/curl access without cookies.
    """
    # Try cookie first
    token = request.cookies.get(_COOKIE_NAME)
    if token and _verify_session_token(token):
        return True

    # Fallback: Bearer token in Authorization header
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
        if _secure_eq(bearer_token, ADMIN_API_KEY):
            return True

    raise HTTPException(
        status_code=401,
        detail="Admin authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_admin_status() -> dict:
    """Return admin UI status for diagnostics (no secrets)."""
    return {
        "enabled": True,
        "configured": bool(ADMIN_API_KEY),
        "session_ttl": ADMIN_SESSION_TTL,
    }
