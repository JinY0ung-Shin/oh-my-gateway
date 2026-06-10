"""Tests for src/metrics.py and its wiring (middleware, usage hook, endpoint)."""

import time

import pytest
from prometheus_client import REGISTRY

from src import metrics
from src.usage_logger import UsageLogger

from tests.test_main_api_unit import client_context


def _sample(name, labels):
    """Read a sample value from the default registry (0.0 when absent)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ---------------------------------------------------------------------------
# path_group
# ---------------------------------------------------------------------------


class TestPathGroup:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/v1/responses", "responses"),
            ("/v1/sessions", "sessions"),
            ("/v1/sessions/abc-123", "sessions"),
            ("/v1/models", "models"),
            ("/v1/auth/status", "auth"),
            ("/v1/mcp/servers", "mcp"),
            ("/health", "health"),
            ("/health/ready", "health"),
            ("/version", "version"),
            ("/metrics", "metrics"),
            ("/", "root"),
            ("/v1/responsesfoo", "other"),
            ("/some/unknown/path", "other"),
        ],
    )
    def test_groups(self, path, expected):
        assert metrics.path_group(path) == expected


# ---------------------------------------------------------------------------
# record_request
# ---------------------------------------------------------------------------


class TestRecordRequest:
    def test_increments_request_counter_and_latency(self):
        counter_labels = {"path_group": "responses", "method": "POST", "status": "200"}
        latency_labels = {"path_group": "responses", "method": "POST"}
        before_count = _sample("gateway_requests_total", counter_labels)
        before_lat = _sample("gateway_request_latency_seconds_count", latency_labels)

        metrics.record_request(
            path="/v1/responses", method="POST", status_code=200, duration_seconds=0.05
        )

        assert _sample("gateway_requests_total", counter_labels) - before_count == 1.0
        assert (
            _sample("gateway_request_latency_seconds_count", latency_labels)
            - before_lat
            == 1.0
        )

    def test_streaming_mode_counter(self):
        streaming = {"mode": "streaming"}
        non_streaming = {"mode": "non_streaming"}
        before_s = _sample("gateway_responses_requests_total", streaming)
        before_ns = _sample("gateway_responses_requests_total", non_streaming)

        metrics.record_request(
            path="/v1/responses",
            method="POST",
            status_code=200,
            duration_seconds=0.01,
            streaming=True,
        )
        metrics.record_request(
            path="/v1/responses",
            method="POST",
            status_code=200,
            duration_seconds=0.01,
            streaming=False,
        )

        assert _sample("gateway_responses_requests_total", streaming) - before_s == 1.0
        assert (
            _sample("gateway_responses_requests_total", non_streaming) - before_ns
            == 1.0
        )

    def test_streaming_none_skips_mode_counter(self):
        streaming = {"mode": "streaming"}
        non_streaming = {"mode": "non_streaming"}
        before_s = _sample("gateway_responses_requests_total", streaming)
        before_ns = _sample("gateway_responses_requests_total", non_streaming)

        metrics.record_request(
            path="/health", method="GET", status_code=200, duration_seconds=0.01
        )

        assert _sample("gateway_responses_requests_total", streaming) == before_s
        assert _sample("gateway_responses_requests_total", non_streaming) == before_ns


# ---------------------------------------------------------------------------
# record_token_usage
# ---------------------------------------------------------------------------


class TestRecordTokenUsage:
    def test_records_all_token_kinds(self):
        labels = lambda kind: {
            "backend": "claude",
            "model": "opus",
            "kind": kind,
        }  # noqa: E731
        before = {
            k: _sample("gateway_tokens_total", labels(k)) for k in ("input", "output")
        }

        metrics.record_token_usage(
            backend="claude",
            model="opus",
            usage={
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 5,
                "cache_creation_tokens": 3,
            },
        )

        assert (
            _sample("gateway_tokens_total", labels("input")) - before["input"] == 11.0
        )
        assert (
            _sample("gateway_tokens_total", labels("output")) - before["output"] == 7.0
        )

    def test_zero_amounts_create_no_samples(self):
        metrics.record_token_usage(
            backend="zero-backend", model="zero-model", usage={"input_tokens": 0}
        )
        assert (
            REGISTRY.get_sample_value(
                "gateway_tokens_total",
                {"backend": "zero-backend", "model": "zero-model", "kind": "input"},
            )
            is None
        )

    def test_missing_backend_and_model_fall_back_to_unknown(self):
        labels = {"backend": "unknown", "model": "unknown", "kind": "output"}
        before = _sample("gateway_tokens_total", labels)
        metrics.record_token_usage(backend=None, model=None, usage={"output_tokens": 2})
        assert _sample("gateway_tokens_total", labels) - before == 2.0


# ---------------------------------------------------------------------------
# usage_logger hook — token metrics recorded even when DB logging is disabled
# ---------------------------------------------------------------------------


async def test_log_turn_from_context_records_token_metrics_without_db():
    usage_logger = UsageLogger()  # engine is None — DB logging disabled
    labels = {"backend": "claude", "model": "sonnet", "kind": "input"}
    before = _sample("gateway_tokens_total", labels)

    await usage_logger.log_turn_from_context(
        request_context={"backend": "claude"},
        response_id="resp_metrics_test",
        model="sonnet",
        chunks=[
            {
                "type": "result",
                "usage": {
                    "input_tokens": 13,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 2,
                    "cache_creation_input_tokens": 1,
                },
            }
        ],
        tool_stats=None,
        started_monotonic=time.monotonic(),
        status="success",
    )

    assert _sample("gateway_tokens_total", labels) - before == 13.0


# ---------------------------------------------------------------------------
# /metrics endpoint + middleware wiring
# ---------------------------------------------------------------------------


def test_metrics_endpoint_returns_prometheus_payload():
    with client_context() as (client, _mock_cli):
        client.get("/health")  # ensure at least one recorded request
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert b"gateway_requests_total" in response.content
    assert b"gateway_request_latency_seconds" in response.content


def test_metrics_endpoint_does_not_require_api_key():
    with client_context() as (client, _mock_cli):
        # client_context patches verify_api_key, but /metrics never calls it;
        # an unauthenticated GET must succeed regardless.
        response = client.get("/metrics")
    assert response.status_code == 200


def test_middleware_records_health_requests():
    labels = {"path_group": "health", "method": "GET", "status": "200"}
    with client_context() as (client, _mock_cli):
        before = _sample("gateway_requests_total", labels)
        client.get("/health")
    assert _sample("gateway_requests_total", labels) - before == 1.0


def test_middleware_records_responses_streaming_mode():
    non_streaming = {"mode": "non_streaming"}
    with client_context() as (client, _mock_cli):
        before = _sample("gateway_responses_requests_total", non_streaming)
        client.post("/v1/responses", json={"model": "sonnet", "input": "Hi"})
    assert _sample("gateway_responses_requests_total", non_streaming) - before == 1.0
