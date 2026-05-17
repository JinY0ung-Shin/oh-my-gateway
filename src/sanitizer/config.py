"""Configuration for the Anthropic Messages sanitizer proxy.

``ANTHROPIC_BASE_URL`` keeps its normal meaning: the Anthropic-compatible
upstream Claude Code would have called directly. When the sanitizer toggle is
enabled and ``ANTHROPIC_BASE_URL`` is explicitly configured, the Claude SDK
environment is rewritten to call this gateway's local ``/v1/messages`` route
instead, and the route forwards to the original ``ANTHROPIC_BASE_URL`` value.
"""

from __future__ import annotations

import os

from src.env_utils import parse_bool_env, parse_int_env


def get_upstream_url() -> str:
    """Original Anthropic-compatible upstream base URL.

    This intentionally reads ``ANTHROPIC_BASE_URL`` rather than a sanitizer-
    specific env var. Operators keep configuring Claude Code's real upstream
    in the usual place; the gateway only overrides Claude's view of that env
    var while launching the SDK subprocess.
    """
    raw = os.getenv("ANTHROPIC_BASE_URL")
    if raw is None or not raw.strip():
        raise RuntimeError("ANTHROPIC_BASE_URL is required when the sanitizer is enabled")
    return raw.strip().rstrip("/")


def has_upstream_url() -> bool:
    """Whether a sanitizer upstream was explicitly configured."""
    raw = os.getenv("ANTHROPIC_BASE_URL")
    return raw is not None and bool(raw.strip())


def get_gateway_base_url() -> str:
    """Local base URL Claude SDK should call when the sanitizer is enabled."""
    port = parse_int_env("PORT", 8000)
    return f"http://127.0.0.1:{port}"


def get_request_timeout_seconds() -> float | None:
    """Per-request timeout in seconds, or ``None`` to disable.

    Streaming responses can be long-lived, so the default disables the timeout.
    Override via ``SANITIZER_REQUEST_TIMEOUT`` (integer seconds; ``0`` = no
    timeout).
    """
    raw = parse_int_env("SANITIZER_REQUEST_TIMEOUT", 0)
    return None if raw <= 0 else float(raw)


def _env_enabled() -> bool:
    """Boot-time default from ``SANITIZER_ENABLED``."""
    return parse_bool_env("SANITIZER_ENABLED", "false")


def is_enabled() -> bool:
    """Whether the sanitizer should accept requests right now.

    The admin panel may override the boot-time env value at runtime via
    ``runtime_config.set("sanitizer_enabled", ...)``. Restarting reverts to
    the ``SANITIZER_ENABLED`` env value. A configured upstream is also required;
    if ``ANTHROPIC_BASE_URL`` is unset, enabling the toggle has no effect.
    """
    # Local import avoids a circular dependency: ``runtime_config._get_original``
    # imports from this module to resolve the boot default.
    from src.runtime_config import runtime_config

    return bool(runtime_config.get("sanitizer_enabled")) and has_upstream_url()
