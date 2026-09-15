"""Full-stack regression for the per-user Responses rate-limit key (#200).

SlowAPI computes the bucket key before the endpoint body runs ``verify_api_key``,
so the key must be derived from a credential the gateway verified:

* a ``USER_API_KEYS`` bearer is the identity — rotating ``X-User-Email`` on the
  same bearer must land in the SAME bucket;
* an unauthenticated caller carrying a victim's header must NOT touch the
  victim's bucket (it falls back to the IP bucket, as before this key existed);
* the legacy service ``API_KEY`` keeps the trusted-BFF behaviour: distinct
  forwarded headers get distinct buckets.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import src.auth as auth_module
import src.main as main
from src.constants import DEFAULT_MODEL, RATE_LIMITS
from tests.test_responses_user import client_context_with_workspace

pytestmark = pytest.mark.skipif(main.limiter is None, reason="rate limiting disabled")

_LIMIT = RATE_LIMITS["responses"]


def _reset_limiter():
    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


def _post(client, headers):
    return client.post(
        "/v1/responses",
        json={"model": DEFAULT_MODEL, "input": "hello", "user": "alice"},
        headers=headers,
    )


@pytest.fixture(autouse=True)
def _roomy_session_cap(monkeypatch, isolated_session_manager):
    """Every request below opens a new session; keep MAX_LIVE_SESSIONS out of the way."""
    import src.constants as constants

    monkeypatch.setattr(constants, "MAX_LIVE_SESSIONS", 10 * _LIMIT)


def _configure_auth(monkeypatch, *, env_api_key, user_api_keys):
    # Resolve the manager through the module at call time: other test files
    # ``importlib.reload`` ``src.auth``, and the key function looks the manager
    # up the same way, so a name bound at import would patch a stale instance.
    manager = auth_module.auth_manager
    monkeypatch.setattr(manager, "env_api_key", env_api_key)
    monkeypatch.setattr(manager, "runtime_api_key", None)
    monkeypatch.setattr(manager, "user_api_keys", user_api_keys)


@pytest.fixture
def user_keyed_gateway(monkeypatch):
    _configure_auth(
        monkeypatch,
        env_api_key=None,
        user_api_keys={"alice": "alice-key", "bob": "bob-key"},
    )
    monkeypatch.delenv("RATE_LIMIT_KEY_BY_USER", raising=False)
    monkeypatch.delenv("WORKSPACE_USER_HEADER", raising=False)
    mock_wm = MagicMock()
    mock_wm.resolve.return_value = Path("/tmp/ws/alice")
    with client_context_with_workspace(mock_wm) as (client, _cli):
        _reset_limiter()
        yield client
    _reset_limiter()


def test_rotating_user_header_on_one_user_key_shares_one_bucket(user_keyed_gateway):
    client = user_keyed_gateway
    for i in range(_LIMIT):
        resp = _post(
            client,
            {
                "Authorization": "Bearer alice-key",
                "X-User-Email": f"spoof-{i}@example.com",
            },
        )
        assert resp.status_code == 200, (i, resp.text)

    # The (LIMIT+1)th request on the same credential is over budget no matter
    # what identity header rides along.
    over = _post(
        client,
        {"Authorization": "Bearer alice-key", "X-User-Email": "fresh@example.com"},
    )
    assert over.status_code == 429, over.text

    # Another credential-bound principal is untouched.
    assert _post(client, {"Authorization": "Bearer bob-key"}).status_code == 200


def test_unauthenticated_caller_cannot_spend_a_victims_bucket(user_keyed_gateway):
    client = user_keyed_gateway
    # Exhaust the IP bucket with unauthenticated requests that all *claim* to be
    # alice (invalid bearer, then no bearer at all). Endpoint auth is patched
    # permissive in this fixture, so the requests reach the limiter and succeed
    # — what matters is WHICH bucket they charge.
    for i in range(_LIMIT):
        headers = {"X-User-Email": "alice"}
        if i % 2:
            headers["Authorization"] = "Bearer not-a-key"
        assert _post(client, headers).status_code == 200, i
    assert _post(client, {"X-User-Email": "alice"}).status_code == 429

    # alice's own credential-bound bucket is still full-budget.
    for _ in range(_LIMIT):
        assert _post(client, {"Authorization": "Bearer alice-key"}).status_code == 200
    assert _post(client, {"Authorization": "Bearer alice-key"}).status_code == 429


def test_service_key_keeps_per_forwarded_user_buckets(monkeypatch):
    _configure_auth(monkeypatch, env_api_key="svc-key", user_api_keys={})
    monkeypatch.delenv("RATE_LIMIT_KEY_BY_USER", raising=False)
    monkeypatch.delenv("WORKSPACE_USER_HEADER", raising=False)
    mock_wm = MagicMock()
    mock_wm.resolve.return_value = Path("/tmp/ws/alice")
    with client_context_with_workspace(mock_wm) as (client, _cli):
        _reset_limiter()
        svc = {"Authorization": "Bearer svc-key"}
        for _ in range(_LIMIT):
            assert _post(client, {**svc, "X-User-Email": "alice"}).status_code == 200
        assert _post(client, {**svc, "X-User-Email": "alice"}).status_code == 429
        # The BFF's other users are not starved by alice.
        assert _post(client, {**svc, "X-User-Email": "bob"}).status_code == 200
        # And a header without a valid credential is still just the IP bucket,
        # not alice's.
        assert _post(client, {"X-User-Email": "alice"}).status_code == 200
    _reset_limiter()
