"""Tests for the GET /health/ready readiness probe."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import src.routes.general as general_module
from src.backend_registry import BackendRegistry

from tests.test_main_api_unit import client_context


def _register_opencode(verify_result=True, verify_side_effect=None):
    """Register a mock OpenCode backend on the live registry."""
    backend = MagicMock()
    backend.name = "opencode"
    backend.verify = AsyncMock(
        return_value=verify_result, side_effect=verify_side_effect
    )
    BackendRegistry.register("opencode", backend)
    return backend


class _FakeUsageLogger:
    def __init__(self, enabled, rows=None):
        self.enabled = enabled
        self.fetch_rows = AsyncMock(return_value=rows)


def test_ready_returns_200_when_all_checks_pass():
    with (
        client_context() as (client, _mock_cli),
        patch.object(general_module, "validate_backend_auth", return_value=(True, {})),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["claude_auth"]["ok"] is True
    assert body["checks"]["opencode_server"] == {
        "ok": True,
        "skipped": True,
        "reason": "opencode backend not registered",
    }
    assert body["checks"]["usage_log_db"] == {
        "ok": True,
        "skipped": True,
        "reason": "usage logging disabled",
    }


def test_ready_returns_503_when_backend_auth_fails():
    with (
        client_context() as (client, _mock_cli),
        patch.object(
            general_module,
            "validate_backend_auth",
            return_value=(False, {"errors": ["missing ANTHROPIC_AUTH_TOKEN"]}),
        ),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["claude_auth"]["ok"] is False
    assert body["checks"]["claude_auth"]["errors"] == ["missing ANTHROPIC_AUTH_TOKEN"]


def test_ready_returns_503_when_auth_check_raises():
    with (
        client_context() as (client, _mock_cli),
        patch.object(
            general_module,
            "validate_backend_auth",
            side_effect=RuntimeError("provider boom"),
        ),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["claude_auth"]["errors"] == ["provider boom"]


def test_ready_checks_opencode_server_when_registered():
    with (
        client_context() as (client, _mock_cli),
        patch.object(general_module, "validate_backend_auth", return_value=(True, {})),
    ):
        backend = _register_opencode(verify_result=True)
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["opencode_server"] == {"ok": True}
    assert "opencode_auth" in response.json()["checks"]
    backend.verify.assert_awaited()


def test_ready_returns_503_when_opencode_server_unreachable():
    with (
        client_context() as (client, _mock_cli),
        patch.object(general_module, "validate_backend_auth", return_value=(True, {})),
    ):
        _register_opencode(verify_result=False)
        response = client.get("/health/ready")

    assert response.status_code == 503
    check = response.json()["checks"]["opencode_server"]
    assert check["ok"] is False
    assert check["errors"] == ["OpenCode server health check failed"]


def test_ready_returns_503_when_opencode_check_times_out():
    async def _hang():
        await asyncio.sleep(5)

    with (
        client_context() as (client, _mock_cli),
        patch.object(general_module, "validate_backend_auth", return_value=(True, {})),
        patch.object(general_module, "READINESS_CHECK_TIMEOUT_SECONDS", 0.05),
    ):
        backend = MagicMock()
        backend.name = "opencode"
        backend.verify = _hang
        BackendRegistry.register("opencode", backend)
        response = client.get("/health/ready")

    assert response.status_code == 503
    check = response.json()["checks"]["opencode_server"]
    assert check["ok"] is False
    assert check["errors"] == ["OpenCode server health check timed out"]


def test_ready_returns_503_when_usage_db_probe_fails():
    fake = _FakeUsageLogger(enabled=True, rows=None)
    with (
        client_context() as (client, _mock_cli),
        patch.object(general_module, "validate_backend_auth", return_value=(True, {})),
        patch.object(general_module, "usage_logger", fake),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 503
    check = response.json()["checks"]["usage_log_db"]
    assert check["ok"] is False
    assert check["errors"] == ["usage-log DB probe failed"]


def test_ready_passes_when_usage_db_probe_succeeds():
    fake = _FakeUsageLogger(enabled=True, rows=[{"1": 1}])
    with (
        client_context() as (client, _mock_cli),
        patch.object(general_module, "validate_backend_auth", return_value=(True, {})),
        patch.object(general_module, "usage_logger", fake),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["usage_log_db"] == {"ok": True}
    fake.fetch_rows.assert_awaited_once_with("SELECT 1")


def test_existing_health_endpoint_contract_unchanged():
    with client_context() as (client, _mock_cli):
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "oh-my-gateway"
    assert "claude" in body["backends"]
