"""Security regression tests for admin login.

Covers two fixes:
1. ``POST /admin/api/login`` is rate-limited (``admin_login``) so the
   ADMIN_API_KEY is not full-speed brute-forceable.
2. The ``admin_session`` cookie is marked ``Secure`` by default.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.admin_auth import login
from src.constants import RATE_LIMITS


# ---------------------------------------------------------------------------
# Issue 1: login rate limiting
# ---------------------------------------------------------------------------


def test_admin_login_has_rate_limit_entry():
    """A dedicated, strict limit is configured for admin login."""
    assert "admin_login" in RATE_LIMITS
    assert RATE_LIMITS["admin_login"] <= 10  # strict by design


def test_admin_login_rate_limited_returns_429():
    """Exceeding the per-minute limit on /admin/api/login returns 429.

    The limit applies before key validation, so repeated wrong-key
    attempts (the brute-force surface) are throttled.
    """
    with patch.dict(os.environ, {"ADMIN_API_KEY": "correct-key"}):
        from src.main import app
        from src.rate_limiter import limiter

        # The route is only rate-limited when a limiter is active. The default
        # env enables it; guard so the test is meaningful.
        assert limiter is not None, "rate limiting must be enabled for this test"

        # Reset slowapi in-memory storage so prior tests don't poison counts.
        limiter.reset()

        client = TestClient(app)
        limit = RATE_LIMITS["admin_login"]

        statuses = []
        for _ in range(limit + 3):
            resp = client.post("/admin/api/login", json={"api_key": "wrong-key"})
            statuses.append(resp.status_code)

        # First `limit` requests are processed (401 wrong key); the rest are 429.
        assert 429 in statuses, f"expected a 429 after {limit} attempts, got {statuses}"
        assert statuses[:limit] == [401] * limit, statuses

        limiter.reset()


# ---------------------------------------------------------------------------
# Issue 2: cookie Secure default
# ---------------------------------------------------------------------------


@patch("src.admin_auth.ADMIN_API_KEY", "correct-key")
def test_session_cookie_secure_by_default():
    """With no ADMIN_COOKIE_SECURE override, the cookie is Secure."""
    env = {k: v for k, v in os.environ.items() if k != "ADMIN_COOKIE_SECURE"}
    with patch.dict(os.environ, env, clear=True):
        response = MagicMock()
        login("correct-key", response)
        call = response.set_cookie.call_args
        kwargs = call.kwargs if call.kwargs else call[1]
        assert kwargs.get("secure") is True


@patch("src.admin_auth.ADMIN_API_KEY", "correct-key")
def test_session_cookie_secure_opt_out():
    """ADMIN_COOKIE_SECURE=false still disables Secure for local HTTP dev."""
    with patch.dict(os.environ, {"ADMIN_COOKIE_SECURE": "false"}):
        response = MagicMock()
        login("correct-key", response)
        call = response.set_cookie.call_args
        kwargs = call.kwargs if call.kwargs else call[1]
        assert kwargs.get("secure") is False
