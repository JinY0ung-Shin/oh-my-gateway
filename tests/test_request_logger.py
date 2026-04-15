"""Tests for request_logger module."""

import time

from src.request_logger import (
    RequestLogEntry,
    RequestLogger,
    _bucket_for_path,
)


# ---------------------------------------------------------------------------
# Bucket mapping
# ---------------------------------------------------------------------------


class TestBucketMapping:
    def test_exact_paths(self):
        assert _bucket_for_path("/v1/chat/completions") is None
        assert _bucket_for_path("/v1/messages") is None
        assert _bucket_for_path("/v1/responses") == "responses"
        assert _bucket_for_path("/health") == "health"
        assert _bucket_for_path("/v1/mcp/servers") == "general"
        assert _bucket_for_path("/v1/auth/status") == "auth"
        assert _bucket_for_path("/v1/sessions") == "session"

    def test_parameterised_paths(self):
        assert _bucket_for_path("/v1/sessions/abc-123") == "session"
        assert _bucket_for_path("/v1/sessions/abc-123/messages") == "session"

    def test_unknown_path_returns_none(self):
        """Untracked paths (not rate-limited) return None."""
        assert _bucket_for_path("/unknown/path") is None
        assert _bucket_for_path("/v1/something-new") is None
        assert _bucket_for_path("/v1/models") is None  # not rate-limited


# ---------------------------------------------------------------------------
# should_log
# ---------------------------------------------------------------------------


class TestShouldLog:
    def test_excludes_admin_paths(self):
        logger = RequestLogger()
        assert logger.should_log("/admin/api/summary") is False
        assert logger.should_log("/admin/api/logs") is False
        assert logger.should_log("/admin") is False

    def test_includes_api_paths(self):
        logger = RequestLogger()
        assert logger.should_log("/v1/responses") is True
        assert logger.should_log("/v1/models") is True
        assert logger.should_log("/health") is True

    def test_excludes_docs(self):
        logger = RequestLogger()
        assert logger.should_log("/docs") is False
        assert logger.should_log("/openapi.json") is False

    def test_custom_exclude_prefixes(self):
        logger = RequestLogger(exclude_prefixes=["/internal/"])
        assert logger.should_log("/internal/metrics") is False
        assert logger.should_log("/v1/responses") is True
        assert logger.should_log("/admin/api/logs") is True  # not excluded with custom


# ---------------------------------------------------------------------------
# Log + Query
# ---------------------------------------------------------------------------


def _make_entry(**overrides):
    defaults = {
        "timestamp": time.time(),
        "method": "POST",
        "path": "/v1/responses",
        "status_code": 200,
        "response_time_ms": 50.0,
        "client_ip": "127.0.0.1",
    }
    defaults.update(overrides)
    return RequestLogEntry(**defaults)


class TestLogAndQuery:
    def test_log_and_retrieve(self):
        logger = RequestLogger(maxlen=10)
        entry = _make_entry()
        logger.log(entry)
        result = logger.query()
        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["method"] == "POST"

    def test_buffer_bounded(self):
        logger = RequestLogger(maxlen=5)
        for i in range(10):
            logger.log(_make_entry(response_time_ms=float(i)))
        result = logger.query(limit=100)
        assert result["total"] == 5  # only last 5 kept
        assert result["total_logged"] == 10  # all-time counter

    def test_filter_by_endpoint(self):
        logger = RequestLogger(maxlen=100)
        logger.log(_make_entry(path="/v1/responses"))
        logger.log(_make_entry(path="/v1/models"))
        logger.log(_make_entry(path="/v1/responses"))

        result = logger.query(endpoint="responses")
        assert result["total"] == 2

    def test_filter_by_status_exact(self):
        logger = RequestLogger(maxlen=100)
        logger.log(_make_entry(status_code=200))
        logger.log(_make_entry(status_code=500))
        logger.log(_make_entry(status_code=200))

        result = logger.query(status="500")
        assert result["total"] == 1

    def test_filter_by_status_range_4xx(self):
        logger = RequestLogger(maxlen=100)
        logger.log(_make_entry(status_code=200))
        logger.log(_make_entry(status_code=400))
        logger.log(_make_entry(status_code=401))
        logger.log(_make_entry(status_code=404))
        logger.log(_make_entry(status_code=429))
        logger.log(_make_entry(status_code=500))

        result = logger.query(status="4xx")
        assert result["total"] == 4  # 400, 401, 404, 429

    def test_filter_by_status_range_5xx(self):
        logger = RequestLogger(maxlen=100)
        logger.log(_make_entry(status_code=200))
        logger.log(_make_entry(status_code=500))
        logger.log(_make_entry(status_code=502))
        logger.log(_make_entry(status_code=503))

        result = logger.query(status="5xx")
        assert result["total"] == 3

    def test_filter_by_malformed_status_does_not_crash(self):
        """Malformed status filter like 'axx' should not raise."""
        logger = RequestLogger(maxlen=100)
        logger.log(_make_entry(status_code=200))
        # These should not raise — they just return unfiltered
        result = logger.query(status="axx")
        assert result["total"] == 1
        result = logger.query(status="notanumber")
        assert result["total"] == 1

    def test_pagination(self):
        logger = RequestLogger(maxlen=100)
        for i in range(20):
            logger.log(_make_entry(response_time_ms=float(i)))

        page1 = logger.query(limit=5, offset=0)
        page2 = logger.query(limit=5, offset=5)
        assert len(page1["items"]) == 5
        assert len(page2["items"]) == 5
        # Items are newest-first; page1 should have higher timestamps
        assert page1["items"][0]["response_time_ms"] != page2["items"][0]["response_time_ms"]

    def test_stats_computation(self):
        logger = RequestLogger(maxlen=100)
        logger.log(_make_entry(status_code=200, response_time_ms=10.0))
        logger.log(_make_entry(status_code=200, response_time_ms=20.0))
        logger.log(_make_entry(status_code=500, response_time_ms=100.0))

        result = logger.query()
        stats = result["stats"]
        assert stats["total_requests"] == 3
        assert stats["error_count"] == 1
        assert stats["error_rate"] == round(1 / 3, 4)
        assert stats["avg_latency_ms"] == round((10 + 20 + 100) / 3, 2)

    def test_empty_stats(self):
        logger = RequestLogger(maxlen=100)
        result = logger.query()
        assert result["stats"]["total_requests"] == 0
        assert result["stats"]["avg_latency_ms"] == 0.0

    def test_entry_includes_bucket(self):
        logger = RequestLogger(maxlen=10)
        logger.log(_make_entry(path="/v1/responses"))
        result = logger.query()
        assert result["items"][0]["bucket"] == "responses"

    def test_entry_untracked_bucket(self):
        logger = RequestLogger(maxlen=10)
        logger.log(_make_entry(path="/v1/models"))
        result = logger.query()
        assert result["items"][0]["bucket"] == "untracked"

    def test_p95_small_sample(self):
        """p95 with 2 entries should return the max, not the min."""
        logger = RequestLogger(maxlen=100)
        logger.log(_make_entry(response_time_ms=10.0))
        logger.log(_make_entry(response_time_ms=100.0))
        result = logger.query()
        # With 2 entries, p95 should be the higher value (100)
        assert result["stats"]["p95_latency_ms"] == 100.0


# ---------------------------------------------------------------------------
# Rate limit snapshot
# ---------------------------------------------------------------------------


class TestRateLimitSnapshot:
    def test_empty_snapshot(self):
        logger = RequestLogger(maxlen=100)
        snapshot = logger.get_rate_limit_snapshot()
        # Should have entries for all configured buckets
        assert "responses" in snapshot
        assert "general" in snapshot
        assert snapshot["responses"]["total_usage"] == 0
        assert snapshot["responses"]["clients"] == []

    def test_snapshot_counts_recent_requests(self):
        logger = RequestLogger(maxlen=100)
        now = time.time()
        logger.log(_make_entry(timestamp=now, path="/v1/responses", client_ip="1.1.1.1"))
        logger.log(_make_entry(timestamp=now, path="/v1/responses", client_ip="1.1.1.1"))
        logger.log(_make_entry(timestamp=now, path="/v1/responses", client_ip="2.2.2.2"))

        snapshot = logger.get_rate_limit_snapshot()
        assert snapshot["responses"]["total_usage"] == 3
        assert len(snapshot["responses"]["clients"]) == 2
        # Top consumer should be first
        assert snapshot["responses"]["clients"][0]["ip"] == "1.1.1.1"
        assert snapshot["responses"]["clients"][0]["count"] == 2

    def test_snapshot_excludes_old_requests(self):
        logger = RequestLogger(maxlen=100)
        old_ts = time.time() - 120  # 2 minutes ago
        logger.log(_make_entry(timestamp=old_ts, path="/v1/responses"))

        snapshot = logger.get_rate_limit_snapshot(window_seconds=60)
        assert snapshot["responses"]["total_usage"] == 0

    def test_snapshot_pct_used(self):
        logger = RequestLogger(maxlen=100)
        now = time.time()
        # Responses limit is 10 req/min by default
        for _ in range(5):
            logger.log(_make_entry(timestamp=now, path="/v1/responses", client_ip="1.1.1.1"))

        snapshot = logger.get_rate_limit_snapshot()
        client = snapshot["responses"]["clients"][0]
        assert client["pct_used"] == 50.0  # 5/10 * 100

    def test_snapshot_excludes_untracked_endpoints(self):
        """Endpoints without rate limiting should not inflate bucket counts."""
        logger = RequestLogger(maxlen=100)
        now = time.time()
        # /v1/models is NOT rate-limited, should not appear in any bucket
        for _ in range(10):
            logger.log(_make_entry(timestamp=now, path="/v1/models", client_ip="1.1.1.1"))

        snapshot = logger.get_rate_limit_snapshot()
        # general bucket should have 0 usage despite /v1/models traffic
        assert snapshot["general"]["total_usage"] == 0
