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
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from fastapi import HTTPException, Request, Response

from src.env_utils import parse_bool_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
ADMIN_SESSION_TTL = int(os.getenv("ADMIN_SESSION_TTL", "3600"))  # 1 hour default

# HMAC key for signing session cookies — derived once at import time.
# Using a random key means sessions don't survive server restarts,
# which is acceptable for an admin panel.
_COOKIE_SECRET = secrets.token_bytes(32)
_COOKIE_NAME = "admin_session"

# ---------------------------------------------------------------------------
# Session token helpers
# ---------------------------------------------------------------------------


def _make_session_token(issued_at: int) -> str:
    """Create an HMAC-signed session token encoding the issue timestamp."""
    payload = f"{issued_at}".encode()
    sig = hmac.new(_COOKIE_SECRET, payload, hashlib.sha256).hexdigest()
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
    return hmac.compare_digest(token, expected)


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
    if not hmac.compare_digest(provided_key, ADMIN_API_KEY):
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
        secure=parse_bool_env("ADMIN_COOKIE_SECURE", "false"),
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
        if hmac.compare_digest(bearer_token, ADMIN_API_KEY):
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
