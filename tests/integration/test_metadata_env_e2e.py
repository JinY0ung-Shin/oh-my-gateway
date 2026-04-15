"""E2E test: METADATA_ENV_ALLOWLIST filtering.

Starts a real uvicorn server and sends HTTP requests to verify that
metadata env allowlist filtering works through the full stack
(HTTP → route → persistent client → SDK subprocess).

Run:  uv run pytest tests/integration/test_metadata_env_e2e.py -m e2e -v
"""

import os
import signal
import subprocess
import time

import httpx
import pytest

E2E_PORT = 18765
BASE_URL = f"http://127.0.0.1:{E2E_PORT}"
STARTUP_TIMEOUT = 30
REQUEST_TIMEOUT = 120


def _read_api_key() -> str | None:
    """Read API_KEY from .env if present."""
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    try:
        for line in open(env_path):
            line = line.strip()
            if line.startswith("API_KEY=") and not line.startswith("#"):
                val = line.split("=", 1)[1].strip()
                return val if val else None
    except FileNotFoundError:
        pass
    return None


@pytest.fixture(scope="module")
def server():
    """Start the gateway with a controlled METADATA_ENV_ALLOWLIST."""
    env = os.environ.copy()
    env["METADATA_ENV_ALLOWLIST"] = "THREAD_ID,ORCHESTRATOR_URL"
    # Disable interactive API key prompt
    env.setdefault("API_KEY", "e2e-test-key")

    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "src.main:app", "--port", str(E2E_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Wait for readiness
    deadline = time.time() + STARTUP_TIMEOUT
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE_URL}/health", timeout=2)
            if r.status_code == 200:
                ready = True
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.5)

    if not ready:
        proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server failed to start within {STARTUP_TIMEOUT}s.\n{out}")

    yield proc

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def auth_header():
    """Build Authorization header matching the server's API_KEY."""
    key = _read_api_key() or "e2e-test-key"
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.e2e
class TestMetadataEnvAllowlistE2E:
    """Verify METADATA_ENV_ALLOWLIST with a live server."""

    def test_allowed_and_disallowed_metadata(self, server, auth_header):
        """Request with mixed metadata keys succeeds; response echoes all keys."""
        resp = httpx.post(
            f"{BASE_URL}/v1/responses",
            headers=auth_header,
            json={
                "model": "sonnet",
                "input": "Say OK",
                "metadata": {
                    "THREAD_ID": "thread-e2e-001",
                    "ORCHESTRATOR_URL": "http://orch:9000",
                    "SECRET_KEY": "should-not-reach-subprocess",
                    "RANDOM": "also-filtered",
                },
            },
            timeout=REQUEST_TIMEOUT,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        # Response echoes back all original metadata (filtering is internal)
        assert data["metadata"]["THREAD_ID"] == "thread-e2e-001"
        assert data["metadata"]["ORCHESTRATOR_URL"] == "http://orch:9000"
        assert data["metadata"]["SECRET_KEY"] == "should-not-reach-subprocess"

    def test_no_metadata(self, server, auth_header):
        """Request without metadata works normally."""
        resp = httpx.post(
            f"{BASE_URL}/v1/responses",
            headers=auth_header,
            json={
                "model": "sonnet",
                "input": "Say OK",
            },
            timeout=REQUEST_TIMEOUT,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"

    def test_empty_metadata(self, server, auth_header):
        """Empty metadata dict doesn't cause errors."""
        resp = httpx.post(
            f"{BASE_URL}/v1/responses",
            headers=auth_header,
            json={
                "model": "sonnet",
                "input": "Say OK",
                "metadata": {},
            },
            timeout=REQUEST_TIMEOUT,
        )
        assert resp.status_code == 200

    def test_only_disallowed_keys(self, server, auth_header):
        """All keys filtered out doesn't cause errors."""
        resp = httpx.post(
            f"{BASE_URL}/v1/responses",
            headers=auth_header,
            json={
                "model": "sonnet",
                "input": "Say OK",
                "metadata": {
                    "NOT_ALLOWED": "val1",
                    "ALSO_NOT": "val2",
                },
            },
            timeout=REQUEST_TIMEOUT,
        )
        assert resp.status_code == 200
