"""Configuration for the Anthropic Messages sanitizer proxy."""

from __future__ import annotations

import os

from src.env_utils import parse_bool_env, parse_int_env


def get_upstream_url() -> str:
    """URL of the upstream service that speaks Anthropic Messages API.

    Typically a LiteLLM ``--port 4000`` instance. The sanitizer forwards
    ``POST /v1/messages`` to ``{upstream}/v1/messages`` verbatim and post-
    processes the SSE stream.
    """
    return os.getenv("SANITIZER_UPSTREAM_URL", "http://localhost:4000").rstrip("/")


def get_request_timeout_seconds() -> float | None:
    """Per-request timeout in seconds, or ``None`` to disable.

    Streaming responses can be long-lived, so the default disables the timeout.
    Override via ``SANITIZER_REQUEST_TIMEOUT`` (integer seconds; ``0`` = no
    timeout).
    """
    raw = parse_int_env("SANITIZER_REQUEST_TIMEOUT", 0)
    return None if raw <= 0 else float(raw)


def is_enabled() -> bool:
    """Whether the sanitizer route should be mounted on the gateway.

    Disabled by default so operators must opt in via ``SANITIZER_ENABLED=true``.
    """
    return parse_bool_env("SANITIZER_ENABLED", "false")


def get_upstream_api_key() -> str | None:
    """Bearer token to present to the upstream service, if any.

    The client's ``Authorization`` header authenticates against the gateway and
    is **not** forwarded — upstream lives in a different trust boundary and
    typically has its own credential. Operators set ``SANITIZER_UPSTREAM_API_KEY``
    to inject the upstream bearer; if unset, the upstream request goes out
    without an ``Authorization`` header.
    """
    raw = os.getenv("SANITIZER_UPSTREAM_API_KEY")
    return raw if raw else None
