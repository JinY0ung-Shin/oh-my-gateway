"""Tests targeting the lowest-coverage modules to close branch gaps.

Modules covered here:
- ``src.sanitizer.config`` — env-driven helpers
- ``src.tool_stats`` — pairing/error fallback branches
- ``src.routes.deps`` — error paths and image-handling branches
- ``src.backends.claude.constants`` — parser helper and warning path
- ``src.runtime_config`` — type-coercion branches in ``_coerce``
"""

from __future__ import annotations

import importlib
import logging
import time

import pytest
from fastapi import HTTPException

from src.backends.claude import constants as claude_constants
from src.routes import deps as deps_module
from src.runtime_config import RuntimeConfig
from src.sanitizer import config as sanitizer_config
from src.tool_stats import ToolStatsCollector


# ---------------------------------------------------------------------------
# src/sanitizer/config.py
# ---------------------------------------------------------------------------


class TestSanitizerConfig:
    def test_get_upstream_url_strips_and_normalises(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "  https://api.example.com/  ")
        assert sanitizer_config.get_upstream_url() == "https://api.example.com"

    def test_get_upstream_url_raises_when_unset(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_BASE_URL is required"):
            sanitizer_config.get_upstream_url()

    def test_get_upstream_url_raises_on_blank(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "   ")
        with pytest.raises(RuntimeError):
            sanitizer_config.get_upstream_url()

    def test_has_upstream_url_true_and_false(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://x")
        assert sanitizer_config.has_upstream_url() is True
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
        assert sanitizer_config.has_upstream_url() is False
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        assert sanitizer_config.has_upstream_url() is False

    def test_get_gateway_base_url_uses_port_env(self, monkeypatch):
        monkeypatch.setenv("PORT", "9123")
        assert sanitizer_config.get_gateway_base_url() == "http://127.0.0.1:9123"

    def test_get_gateway_base_url_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        assert sanitizer_config.get_gateway_base_url() == "http://127.0.0.1:8000"

    def test_request_timeout_none_when_zero_or_unset(self, monkeypatch):
        monkeypatch.delenv("SANITIZER_REQUEST_TIMEOUT", raising=False)
        assert sanitizer_config.get_request_timeout_seconds() is None

        monkeypatch.setenv("SANITIZER_REQUEST_TIMEOUT", "0")
        assert sanitizer_config.get_request_timeout_seconds() is None

    def test_request_timeout_positive_value(self, monkeypatch):
        monkeypatch.setenv("SANITIZER_REQUEST_TIMEOUT", "30")
        assert sanitizer_config.get_request_timeout_seconds() == 30.0

    def test_tls_verify_default_true_when_unset(self, monkeypatch):
        monkeypatch.delenv("SANITIZER_TLS_VERIFY", raising=False)
        assert sanitizer_config.get_tls_verify() is True

    def test_tls_verify_blank_returns_true(self, monkeypatch):
        monkeypatch.setenv("SANITIZER_TLS_VERIFY", "   ")
        assert sanitizer_config.get_tls_verify() is True

    @pytest.mark.parametrize("falsy", ["false", "0", "no", "off", "FALSE", "Off"])
    def test_tls_verify_falsy_values(self, monkeypatch, falsy):
        monkeypatch.setenv("SANITIZER_TLS_VERIFY", falsy)
        assert sanitizer_config.get_tls_verify() is False

    @pytest.mark.parametrize("truthy", ["true", "1", "yes", "on", "TRUE", "On"])
    def test_tls_verify_truthy_values(self, monkeypatch, truthy):
        monkeypatch.setenv("SANITIZER_TLS_VERIFY", truthy)
        assert sanitizer_config.get_tls_verify() is True

    def test_tls_verify_custom_cert_bundle_path(self, monkeypatch):
        monkeypatch.setenv("SANITIZER_TLS_VERIFY", "/etc/ssl/custom-ca.pem")
        assert sanitizer_config.get_tls_verify() == "/etc/ssl/custom-ca.pem"

    def test_openai_bridge_env_toggle(self, monkeypatch):
        monkeypatch.setenv("SANITIZER_USE_OPENAI_BRIDGE", "true")
        assert sanitizer_config.is_openai_bridge_enabled() is True
        monkeypatch.setenv("SANITIZER_USE_OPENAI_BRIDGE", "false")
        assert sanitizer_config.is_openai_bridge_enabled() is False

    def test_is_enabled_requires_upstream(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        # Even with the runtime toggle on, an unset upstream disables sanitizer.
        from src.runtime_config import runtime_config

        original = runtime_config.get("sanitizer_enabled")
        try:
            runtime_config.set("sanitizer_enabled", True)
            assert sanitizer_config.is_enabled() is False
        finally:
            runtime_config.set("sanitizer_enabled", original)

    def test_is_enabled_true_when_upstream_and_toggle(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")
        from src.runtime_config import runtime_config

        original = runtime_config.get("sanitizer_enabled")
        try:
            runtime_config.set("sanitizer_enabled", True)
            assert sanitizer_config.is_enabled() is True
            runtime_config.set("sanitizer_enabled", False)
            assert sanitizer_config.is_enabled() is False
        finally:
            runtime_config.set("sanitizer_enabled", original)


# ---------------------------------------------------------------------------
# src/tool_stats.py
# ---------------------------------------------------------------------------


class TestToolStatsCollector:
    def test_record_use_ignores_empty_name(self):
        c = ToolStatsCollector()
        c.record_use("id-1", "")
        assert c.snapshot() == {}

    def test_record_use_with_id_then_result_paired(self):
        c = ToolStatsCollector()
        c.record_use("u1", "Read")
        c.record_result("u1", is_error=False)
        snap = c.snapshot()
        assert snap["Read"]["count"] == 1
        assert snap["Read"]["errors"] == 0
        assert snap["Read"]["total_ms"] >= 0

    def test_record_use_with_id_then_error_result_paired(self):
        c = ToolStatsCollector()
        c.record_use("u1", "Write")
        # Sleep briefly so total_ms is non-trivial
        time.sleep(0.005)
        c.record_result("u1", is_error=True)
        snap = c.snapshot()
        assert snap["Write"]["count"] == 1
        assert snap["Write"]["errors"] == 1
        assert snap["Write"]["total_ms"] >= 1

    def test_record_use_without_id_skips_pairing(self):
        c = ToolStatsCollector()
        c.record_use(None, "Glob")
        snap = c.snapshot()
        assert snap["Glob"]["count"] == 1
        # No starts recorded — second record_result with same id pairs nothing.
        c.record_result(None, is_error=False)
        # Still no change for a non-error unpaired result.
        assert c.snapshot()["Glob"]["count"] == 1

    def test_record_result_unpaired_error_uses_fallback_name(self):
        c = ToolStatsCollector()
        c.record_result("missing-id", is_error=True, fallback_name="Bash")
        snap = c.snapshot()
        assert snap["Bash"]["errors"] == 1
        assert snap["Bash"]["count"] == 0

    def test_record_result_unpaired_error_defaults_unknown(self):
        c = ToolStatsCollector()
        c.record_result(None, is_error=True)
        snap = c.snapshot()
        assert snap["unknown"]["errors"] == 1

    def test_record_result_unpaired_non_error_is_noop(self):
        c = ToolStatsCollector()
        c.record_result(None, is_error=False)
        c.record_result("nope", is_error=False)
        assert c.snapshot() == {}

    def test_snapshot_returns_shallow_copy(self):
        c = ToolStatsCollector()
        c.record_use("u1", "Read")
        snap = c.snapshot()
        snap["Read"]["count"] = 999
        # Mutating the snapshot must not mutate the collector's internal state.
        assert c.snapshot()["Read"]["count"] == 1


# ---------------------------------------------------------------------------
# src/routes/deps.py
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self, name: str, has_image_handler: bool = False) -> None:
        self.name = name
        if has_image_handler:
            self.image_handler = object()


class TestRoutesDeps:
    def test_resolve_and_get_backend_raises_for_unknown_model(self, monkeypatch):
        monkeypatch.setattr(deps_module, "resolve_model", lambda _m: None)

        class _Reg:
            @staticmethod
            def all_model_ids():
                return ["claude", "codex"]

        monkeypatch.setattr(deps_module, "BackendRegistry", _Reg)

        with pytest.raises(HTTPException) as ei:
            deps_module.resolve_and_get_backend("does-not-exist")
        assert ei.value.status_code == 400
        assert "not supported" in ei.value.detail

    def test_resolve_and_get_backend_raises_when_backend_not_registered(self, monkeypatch):
        class _Resolved:
            backend = "claude"

        monkeypatch.setattr(deps_module, "resolve_model", lambda _m: _Resolved())

        class _Reg:
            @staticmethod
            def all_model_ids():
                return ["claude"]

            @staticmethod
            def is_registered(_n):
                return False

        monkeypatch.setattr(deps_module, "BackendRegistry", _Reg)

        with pytest.raises(HTTPException) as ei:
            deps_module.resolve_and_get_backend("claude-opus")
        assert ei.value.status_code == 400
        assert "is not available" in ei.value.detail

    def test_validate_backend_auth_or_raise_raises_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            deps_module,
            "validate_backend_auth",
            lambda _n: (False, {"errors": ["missing OPENAI_API_KEY", "bad token"]}),
        )
        with pytest.raises(HTTPException) as ei:
            deps_module.validate_backend_auth_or_raise("codex")
        assert ei.value.status_code == 503
        assert "missing OPENAI_API_KEY" in ei.value.detail

    def test_validate_backend_auth_or_raise_passes_when_valid(self, monkeypatch):
        monkeypatch.setattr(
            deps_module, "validate_backend_auth", lambda _n: (True, {})
        )
        # No exception expected.
        deps_module.validate_backend_auth_or_raise("claude")

    def test_validate_backend_auth_or_raise_error_without_details(self, monkeypatch):
        monkeypatch.setattr(
            deps_module, "validate_backend_auth", lambda _n: (False, {})
        )
        with pytest.raises(HTTPException) as ei:
            deps_module.validate_backend_auth_or_raise("claude")
        # No error suffix when errors list is empty.
        assert ei.value.detail.startswith("claude backend authentication failed.")

    def test_request_has_images_detects_input_image(self):
        class _Req:
            input = [
                {
                    "content": [
                        {"type": "input_text", "text": "hi"},
                        {"type": "input_image", "image_url": "data:..."},
                    ]
                }
            ]

        assert deps_module.request_has_images(_Req()) is True

    def test_request_has_images_handles_object_attrs(self):
        class _Part:
            type = "input_image"

        class _Item:
            content = [_Part()]

        class _Req:
            input = [_Item()]

        assert deps_module.request_has_images(_Req()) is True

    def test_request_has_images_false_for_text_only(self):
        class _Req:
            input = [{"content": [{"type": "input_text", "text": "hi"}]}]

        assert deps_module.request_has_images(_Req()) is False

    def test_request_has_images_false_for_non_list_input(self):
        class _Req:
            input = "plain string"

        assert deps_module.request_has_images(_Req()) is False

    def test_validate_image_request_allows_text_only(self):
        class _Req:
            input = [{"content": [{"type": "input_text", "text": "hi"}]}]

        deps_module.validate_image_request(_Req(), _FakeBackend("claude"))

    def test_validate_image_request_passes_for_opencode(self):
        class _Req:
            input = [{"content": [{"type": "input_image"}]}]

        deps_module.validate_image_request(_Req(), _FakeBackend("opencode"))

    def test_validate_image_request_passes_for_codex(self):
        class _Req:
            input = [{"content": [{"type": "input_image"}]}]

        deps_module.validate_image_request(_Req(), _FakeBackend("codex"))

    def test_validate_image_request_rejects_when_handler_missing(self):
        class _Req:
            input = [{"content": [{"type": "input_image"}]}]

        with pytest.raises(HTTPException) as ei:
            deps_module.validate_image_request(_Req(), _FakeBackend("claude"))
        assert ei.value.status_code == 400
        assert "Image input is not supported" in ei.value.detail

    def test_validate_image_request_allows_when_handler_present(self):
        class _Req:
            input = [{"content": [{"type": "input_image"}]}]

        deps_module.validate_image_request(
            _Req(), _FakeBackend("claude", has_image_handler=True)
        )

    def test_truncate_image_data_truncates_base64_string(self):
        big_b64 = "data:image/png;base64," + ("A" * 500)
        out = deps_module.truncate_image_data({"data": big_b64})
        assert "...[truncated]" in out["data"]
        assert len(out["data"]) < len(big_b64)

    def test_truncate_image_data_recurses_into_lists_and_dicts(self):
        big_b64 = "data:image/png;base64," + ("B" * 500)
        payload = {"messages": [{"image_url": big_b64, "text": "ok"}]}
        out = deps_module.truncate_image_data(payload)
        assert "...[truncated]" in out["messages"][0]["image_url"]
        assert out["messages"][0]["text"] == "ok"

    def test_truncate_image_data_passthrough_for_scalars(self):
        assert deps_module.truncate_image_data(42) == 42
        assert deps_module.truncate_image_data("hello") == "hello"
        assert deps_module.truncate_image_data(None) is None

    def test_truncate_image_data_does_not_truncate_short_values(self):
        out = deps_module.truncate_image_data({"data": "short-value"})
        assert out["data"] == "short-value"

    def test_truncate_image_data_does_not_truncate_non_image_long_string(self):
        long_text = "x" * 500
        out = deps_module.truncate_image_data({"data": long_text})
        assert out["data"] == long_text  # not base64, not data:image -> unchanged


# ---------------------------------------------------------------------------
# src/backends/claude/constants.py
# ---------------------------------------------------------------------------


class TestClaudeConstants:
    def test_parse_sandbox_bool_valid_true(self, monkeypatch):
        monkeypatch.setenv("FAKE_SANDBOX_BOOL", "1")
        assert claude_constants._parse_sandbox_bool("FAKE_SANDBOX_BOOL", "false") is True

    def test_parse_sandbox_bool_valid_false(self, monkeypatch):
        monkeypatch.setenv("FAKE_SANDBOX_BOOL", "no")
        assert claude_constants._parse_sandbox_bool("FAKE_SANDBOX_BOOL", "true") is False

    def test_parse_sandbox_bool_invalid_warns_and_uses_default(self, monkeypatch, caplog):
        monkeypatch.setenv("FAKE_SANDBOX_BOOL", "maybe")
        with caplog.at_level(logging.WARNING, logger=claude_constants.__name__):
            result = claude_constants._parse_sandbox_bool("FAKE_SANDBOX_BOOL", "true")
        assert result is True
        assert any("Invalid FAKE_SANDBOX_BOOL" in rec.message for rec in caplog.records)

    def test_parse_sandbox_bool_unset_uses_parse_bool_env(self, monkeypatch):
        monkeypatch.delenv("FAKE_SANDBOX_BOOL", raising=False)
        assert claude_constants._parse_sandbox_bool("FAKE_SANDBOX_BOOL", "true") is True
        assert claude_constants._parse_sandbox_bool("FAKE_SANDBOX_BOOL", "false") is False

    def test_invalid_task_budget_logs_warning(self, monkeypatch, caplog):
        """Reimport the module with an invalid TASK_BUDGET to exercise the warning branch."""
        monkeypatch.setenv("TASK_BUDGET", "not-a-number")
        with caplog.at_level(logging.WARNING, logger="src.backends.claude.constants"):
            reloaded = importlib.reload(claude_constants)
        try:
            assert reloaded.DEFAULT_TASK_BUDGET is None
            assert any("Invalid TASK_BUDGET" in rec.message for rec in caplog.records)
        finally:
            # Restore default behavior so subsequent tests see the original module state.
            monkeypatch.delenv("TASK_BUDGET", raising=False)
            importlib.reload(claude_constants)

    def test_invalid_sandbox_enabled_logs_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("CLAUDE_SANDBOX_ENABLED", "definitely")
        with caplog.at_level(logging.WARNING, logger="src.backends.claude.constants"):
            reloaded = importlib.reload(claude_constants)
        try:
            assert reloaded.CLAUDE_SANDBOX_ENABLED is None
            assert any(
                "Invalid CLAUDE_SANDBOX_ENABLED" in rec.message for rec in caplog.records
            )
        finally:
            monkeypatch.delenv("CLAUDE_SANDBOX_ENABLED", raising=False)
            importlib.reload(claude_constants)


# ---------------------------------------------------------------------------
# src/runtime_config.py — _coerce branches
# ---------------------------------------------------------------------------


class TestRuntimeConfigCoercion:
    def test_int_below_minimum_raises(self):
        rc = RuntimeConfig()
        # default_max_turns has a min of 1 enforced inside _coerce.
        with pytest.raises(ValueError, match=">= 1"):
            rc.set("default_max_turns", 0)

    def test_bool_passthrough_for_bool_value(self):
        rc = RuntimeConfig()
        rc.set("sanitizer_enabled", True)
        assert rc.get("sanitizer_enabled") is True
        rc.set("sanitizer_enabled", False)
        assert rc.get("sanitizer_enabled") is False

    def test_bool_from_numeric_value(self):
        rc = RuntimeConfig()
        rc.set("sanitizer_enabled", 1)
        assert rc.get("sanitizer_enabled") is True
        rc.set("sanitizer_enabled", 0.0)
        assert rc.get("sanitizer_enabled") is False

    def test_bool_invalid_string_raises(self):
        rc = RuntimeConfig()
        with pytest.raises(ValueError, match="boolean"):
            rc.set("sanitizer_enabled", "maybe")

    def test_bool_invalid_type_raises(self):
        rc = RuntimeConfig()
        with pytest.raises(ValueError, match="boolean"):
            rc.set("sanitizer_enabled", ["not", "bool"])

    def test_string_options_validation_rejects_unknown(self):
        rc = RuntimeConfig()
        # thinking_mode has options ['adaptive', 'enabled', 'disabled']
        with pytest.raises(ValueError, match="must be one of"):
            rc.set("thinking_mode", "explosive")

    def test_string_options_validation_accepts_known(self):
        rc = RuntimeConfig()
        rc.set("thinking_mode", "enabled")
        assert rc.get("thinking_mode") == "enabled"
