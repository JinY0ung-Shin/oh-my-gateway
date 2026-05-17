"""Configuration for the Anthropic Messages sanitizer proxy.

The upstream is intentionally locked to ``127.0.0.1`` — the sanitizer is meant
to sit in front of a co-located LiteLLM (or similar) instance, not to proxy
to arbitrary remote endpoints. This keeps the security model simple: same
trust boundary, no leaked credentials, no SSRF surface from the gateway.
"""

from __future__ import annotations

from src.env_utils import parse_bool_env, parse_int_env


# Host portion of the upstream URL — fixed by design.
UPSTREAM_HOST = "127.0.0.1"


def get_upstream_port() -> int:
    """TCP port of the upstream Anthropic Messages service on localhost.

    Defaults to a high port (54000) since the upstream LiteLLM is expected to
    be bound to loopback only — it is not a service users hit directly.
    """
    return parse_int_env("SANITIZER_UPSTREAM_PORT", 54000)


def get_upstream_url() -> str:
    """Full upstream base URL (``http://127.0.0.1:<port>``)."""
    return f"http://{UPSTREAM_HOST}:{get_upstream_port()}"


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
    the ``SANITIZER_ENABLED`` env value.
    """
    # Local import avoids a circular dependency: ``runtime_config._get_original``
    # imports from this module to resolve the boot default.
    from src.runtime_config import runtime_config

    return bool(runtime_config.get("sanitizer_enabled"))
