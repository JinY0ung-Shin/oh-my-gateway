"""Startup configuration validation.

Detects contradictory or silently-ignored environment variable combinations
before the gateway starts serving traffic.  ``check_config()`` returns a list
of :class:`ConfigIssue` (severity ``"error"`` or ``"warning"``);
``run_startup_config_check()`` logs them and raises ``RuntimeError`` when any
error-severity issue is present, mirroring the ADMIN_API_KEY fail-fast in
``src.admin_auth.validate_admin_config``.

Operators can bypass the fail-fast with ``SKIP_CONFIG_CHECK=true``.

This module intentionally reads ``os.environ`` directly and imports only
``src.env_utils`` (which has no intra-project imports) so it can run very
early in any process without triggering backend/auth import side effects.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal

from src.env_utils import parse_bool_env

logger = logging.getLogger(__name__)

Severity = Literal["error", "warning"]

KNOWN_BACKENDS = ("claude", "opencode", "codex")

# Wrapper-side OpenCode vars that only affect the managed `opencode serve`
# subprocess.  When OPENCODE_BASE_URL selects external mode these are no-ops
# (see src/backends/opencode/client.py module docstring and __init__).
OPENCODE_MANAGED_ONLY_VARS = (
    "OPENCODE_BIN",
    "OPENCODE_HOST",
    "OPENCODE_PORT",
    "OPENCODE_START_TIMEOUT_MS",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_USE_WRAPPER_MCP_CONFIG",
)

# usage_logger relies on ``result.lastrowid`` for the usage_tool insert,
# which MySQL/MariaDB/SQLite support but PostgreSQL does not (see the
# module docstring in src/usage_logger.py).  The read-side analytics in
# src/usage_queries.py also branch their date/time SQL on the dialect so the
# admin dashboard works on all three.  Dialects after the shorthand
# normalisation performed by usage_logger._normalize_db_url:
USAGE_LOG_SUPPORTED_DIALECTS = ("mysql", "mariadb", "sqlite")


@dataclass(frozen=True)
class ConfigIssue:
    """One detected configuration problem."""

    severity: Severity
    message: str


def _enabled_backends() -> List[str]:
    """Parse BACKENDS exactly like ``src.backends._enabled_backend_names``."""
    raw = os.getenv("BACKENDS", "claude")
    names: List[str] = []
    for item in raw.split(","):
        name = item.strip().lower()
        if name and name not in names:
            names.append(name)
    return names or ["claude"]


def _is_set(name: str) -> bool:
    """True when the env var is set to a non-blank value."""
    return bool((os.getenv(name) or "").strip())


def _check_backends(backends: List[str]) -> List[ConfigIssue]:
    """Unknown names in BACKENDS are silently skipped at registration

    (src/backends/__init__.py discover_backends logs-and-skips), leaving the
    backend unusable — treat typos as fatal here instead.
    """
    issues: List[ConfigIssue] = []
    unknown = [b for b in backends if b not in KNOWN_BACKENDS]
    for name in unknown:
        issues.append(
            ConfigIssue(
                "error",
                f"BACKENDS contains unknown backend {name!r} "
                f"(known backends: {', '.join(KNOWN_BACKENDS)}). "
                "It would be silently skipped at registration.",
            )
        )
    return issues


def _check_opencode(backends: List[str]) -> List[ConfigIssue]:
    issues: List[ConfigIssue] = []
    opencode_enabled = "opencode" in backends
    external_mode = _is_set("OPENCODE_BASE_URL")

    if opencode_enabled:
        # OPENCODE_MODELS is the gateway-side /v1/models allowlist in BOTH
        # managed and external mode (src/backends/opencode/constants.py
        # configured_provider_models; client docstring: "still apply").
        if not _is_set("OPENCODE_MODELS"):
            issues.append(
                ConfigIssue(
                    "warning",
                    "BACKENDS enables 'opencode' but OPENCODE_MODELS is unset/empty: "
                    "no OpenCode models will be listed by /v1/models. Requests with "
                    "an explicit opencode/<provider>/<model> id still work, but "
                    "clients cannot discover them. Set OPENCODE_MODELS.",
                )
            )
        if external_mode:
            ignored = [v for v in OPENCODE_MANAGED_ONLY_VARS if _is_set(v)]
            if ignored:
                issues.append(
                    ConfigIssue(
                        "warning",
                        "OPENCODE_BASE_URL selects external mode, so these "
                        "managed-mode-only settings are silently ignored: "
                        f"{', '.join(ignored)}. The external `opencode serve` "
                        "owns its own config.",
                    )
                )
        elif parse_bool_env("OPENCODE_USE_WRAPPER_MCP_CONFIG", "false") and not _is_set(
            "MCP_CONFIG"
        ):
            issues.append(
                ConfigIssue(
                    "warning",
                    "OPENCODE_USE_WRAPPER_MCP_CONFIG=true but MCP_CONFIG is unset: "
                    "there is no gateway MCP config to inject into the managed "
                    "OpenCode server, so the flag is a no-op.",
                )
            )
    else:
        configured = [v for v in ("OPENCODE_BASE_URL", "OPENCODE_MODELS") if _is_set(v)]
        if configured:
            issues.append(
                ConfigIssue(
                    "warning",
                    f"{', '.join(configured)} set but 'opencode' is not in BACKENDS "
                    f"(BACKENDS={','.join(backends)}); the OpenCode backend will not "
                    "be registered. Add 'opencode' to BACKENDS or unset the vars.",
                )
            )
    return issues


def _check_codex(backends: List[str]) -> List[ConfigIssue]:
    issues: List[ConfigIssue] = []
    if "codex" in backends:
        # CODEX_MODELS unset falls back to the built-in "gpt-5.5" default
        # (src/backends/codex/constants.py configured_provider_models), which
        # may not match the installed Codex CLI; empty means nothing listed.
        raw = os.getenv("CODEX_MODELS")
        if raw is None:
            issues.append(
                ConfigIssue(
                    "warning",
                    "BACKENDS enables 'codex' but CODEX_MODELS is unset: the "
                    "built-in default model list ('gpt-5.5') will be advertised, "
                    "which may not match your Codex CLI. Set CODEX_MODELS "
                    "explicitly.",
                )
            )
        elif not raw.strip():
            issues.append(
                ConfigIssue(
                    "warning",
                    "BACKENDS enables 'codex' but CODEX_MODELS is empty: no Codex "
                    "models will be listed by /v1/models. Requests with an explicit "
                    "codex/<model> id still work.",
                )
            )
    elif _is_set("CODEX_MODELS"):
        issues.append(
            ConfigIssue(
                "warning",
                f"CODEX_MODELS set but 'codex' is not in BACKENDS "
                f"(BACKENDS={','.join(backends)}); the Codex backend will not be "
                "registered. Add 'codex' to BACKENDS or unset the var.",
            )
        )
    return issues


def _check_usage_log_db_url() -> List[ConfigIssue]:
    url = (os.getenv("USAGE_LOG_DB_URL") or "").strip()
    if not url:
        return []
    scheme = url.split("://", 1)[0] if "://" in url else ""
    dialect = scheme.split("+", 1)[0].lower()
    if dialect in USAGE_LOG_SUPPORTED_DIALECTS:
        return []
    return [
        ConfigIssue(
            "error",
            f"USAGE_LOG_DB_URL uses unsupported scheme {scheme or url!r}: the "
            "usage logger's insert path relies on result.lastrowid, which only "
            "MySQL/MariaDB/SQLite provide (see src/usage_logger.py). Use a "
            "mysql://, mariadb:// or sqlite:// URL, or unset USAGE_LOG_DB_URL.",
        )
    ]


def _check_cors() -> List[ConfigIssue]:
    """src/main.py passes CORS_ORIGINS to CORSMiddleware with a hard-coded
    ``allow_credentials=True``; wildcard origins therefore grant any site
    credentialed access (Starlette echoes the request Origin)."""
    raw = os.getenv("CORS_ORIGINS", '["*"]')
    try:
        origins = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [
            ConfigIssue(
                "error",
                f"CORS_ORIGINS is not valid JSON: {raw!r}. Expected a JSON list "
                'such as ["https://example.com"].',
            )
        ]
    if not isinstance(origins, list):
        return [
            ConfigIssue(
                "error",
                f"CORS_ORIGINS must be a JSON list of origins, got {type(origins).__name__}.",
            )
        ]
    if "*" in origins:
        return [
            ConfigIssue(
                "warning",
                "CORS_ORIGINS allows '*' while the gateway always sets "
                "allow_credentials=True (src/main.py); any website can make "
                "credentialed cross-origin requests. List explicit origins "
                "for non-local deployments.",
            )
        ]
    return []


def _check_api_key() -> List[ConfigIssue]:
    """verify_api_key (src/auth.py) allows ALL requests when no API key is
    configured — default-open."""
    if _is_set("API_KEY"):
        return []
    return [
        ConfigIssue(
            "warning",
            "API_KEY is unset: public /v1 endpoints accept unauthenticated "
            "requests (src/auth.py verify_api_key is default-open). Set API_KEY "
            "for any non-local deployment.",
        )
    ]


def _check_sanitizer() -> List[ConfigIssue]:
    """SANITIZER_ENABLED without ANTHROPIC_BASE_URL is a documented no-op
    (src/sanitizer/config.py requires an explicit upstream)."""
    if parse_bool_env("SANITIZER_ENABLED", "false") and not _is_set(
        "ANTHROPIC_BASE_URL"
    ):
        return [
            ConfigIssue(
                "warning",
                "SANITIZER_ENABLED=true but ANTHROPIC_BASE_URL is unset: the "
                "sanitizer has no upstream to forward to and is effectively "
                "disabled. Set ANTHROPIC_BASE_URL or remove SANITIZER_ENABLED.",
            )
        ]
    return []


def _check_default_model(backends: List[str]) -> List[ConfigIssue]:
    """DEFAULT_MODEL must be resolvable by an enabled backend, otherwise every
    request without an explicit model fails. Bare model names (sonnet/opus/…)
    resolve to the Claude backend only (src/backends/claude/__init__.py)."""
    default_model = (os.getenv("DEFAULT_MODEL") or "sonnet").strip()
    prefix = default_model.split("/", 1)[0].lower()
    if "/" in default_model:
        if prefix in backends:
            return []
    elif "claude" in backends:
        return []
    return [
        ConfigIssue(
            "warning",
            f"DEFAULT_MODEL={default_model!r} is not served by any enabled "
            f"backend (BACKENDS={','.join(backends)}); requests that omit "
            "'model' will fail to resolve. Set DEFAULT_MODEL to a model of an "
            "enabled backend.",
        )
    ]


def _check_mcp_manifest() -> List[ConfigIssue]:
    """The admin-managed MCP manifest is persisted to GATEWAY_MCP_MANIFEST (see
    src/mcp_manifest.py). If the override points at a directory that does not
    exist, admin CRUD writes will fail. Only os.environ is read here to keep the
    module's early-import invariant (no src.mcp_manifest import)."""
    override = (os.getenv("GATEWAY_MCP_MANIFEST") or "").strip()
    if (
        override
        and not os.path.isfile(override)
        and not os.path.isdir(os.path.dirname(override) or ".")
    ):
        return [
            ConfigIssue(
                "warning",
                f"GATEWAY_MCP_MANIFEST={override!r} directory does not exist; the "
                "admin-managed MCP manifest cannot be persisted there.",
            )
        ]
    return []


def _check_mcp_server_env() -> List[ConfigIssue]:
    """``GATEWAY_MCP_SERVER_ENV`` is a JSON object or a path to one.

    An unparseable value is dropped at load with an error log and the declared
    credentials silently never reach any MCP server, so flag it up front. Parsed
    here with the stdlib only, keeping the module's early-import invariant (no
    ``src.mcp_plugin_overlay`` import).
    """
    raw = (os.getenv("GATEWAY_MCP_SERVER_ENV") or "").strip()
    if not raw:
        return []
    # Same order as the loader: existing file first, else inline JSON.
    if os.path.isfile(raw):
        try:
            with open(raw, encoding="utf-8") as f:
                parsed = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return [
                ConfigIssue(
                    "warning",
                    f"GATEWAY_MCP_SERVER_ENV={raw!r} is not readable JSON ({e}); "
                    "the MCP credential overlay layer will be ignored.",
                )
            ]
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return [
                ConfigIssue(
                    "warning",
                    f"GATEWAY_MCP_SERVER_ENV={raw!r} is neither an existing file "
                    f"nor valid inline JSON ({e}); the MCP credential overlay "
                    "layer will be ignored.",
                )
            ]
    if not isinstance(parsed, dict):
        return [
            ConfigIssue(
                "warning",
                "GATEWAY_MCP_SERVER_ENV must be a JSON object of "
                "{server: {env, headers}}; the MCP credential overlay layer will "
                "be ignored.",
            )
        ]
    return []


def _check_claude_settings_env() -> List[ConfigIssue]:
    """``GATEWAY_CLAUDE_SETTINGS_ENV`` must parse, and needs the ``user`` source.

    The declared vars are projected into ``$HOME/.claude/settings.json``, which
    only reaches sessions when ``setting_sources`` includes ``user`` (the gateway
    default is project,local; Docker Compose sets user,project,local). Stdlib-only
    parsing keeps this module's early-import invariant.
    """
    raw = (os.getenv("GATEWAY_CLAUDE_SETTINGS_ENV") or "").strip()
    if not raw:
        return []

    issues: List[ConfigIssue] = []
    if os.path.isfile(raw):
        try:
            with open(raw, encoding="utf-8") as f:
                parsed = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return [
                ConfigIssue(
                    "warning",
                    f"GATEWAY_CLAUDE_SETTINGS_ENV={raw!r} is not readable JSON ({e}); "
                    "the declared Claude session env will be ignored.",
                )
            ]
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return [
                ConfigIssue(
                    "warning",
                    f"GATEWAY_CLAUDE_SETTINGS_ENV={raw!r} is neither an existing file "
                    f"nor valid inline JSON ({e}); the declared Claude session env "
                    "will be ignored.",
                )
            ]
    if not isinstance(parsed, dict):
        return [
            ConfigIssue(
                "warning",
                "GATEWAY_CLAUDE_SETTINGS_ENV must be a JSON object of {NAME: value}; "
                "the declared Claude session env will be ignored.",
            )
        ]

    sources_raw = (os.getenv("CLAUDE_SETTING_SOURCES") or "").strip()
    sources = (
        [p.strip() for p in sources_raw.split(",") if p.strip()]
        if sources_raw
        # Mirrors src.backends.claude.client._DEFAULT_SETTING_SOURCES.
        else ["project", "local"]
    )
    if "user" not in sources:
        issues.append(
            ConfigIssue(
                "warning",
                "GATEWAY_CLAUDE_SETTINGS_ENV is set but CLAUDE_SETTING_SOURCES "
                f"({','.join(sources)}) does not include 'user', so Claude sessions "
                "will not read ~/.claude/settings.json and the declared env will "
                "have no effect.",
            )
        )
    return issues


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _check_stall_hierarchy() -> List[ConfigIssue]:
    """The silence budgets must nest, or a healthy long tool call gets killed.

    Order (innermost first): the CLI's per-call watchdogs — the gateway-owned
    effective ``MCP_TOOL_TIMEOUT`` (positive operator value, else 600000 ms) for
    MCP tools, and ``BASH_MAX_TIMEOUT_MS`` (else the CLI's built-in 600000 ms;
    ``BASH_DEFAULT_TIMEOUT_MS`` is a per-call default, not a ceiling) for Bash.
    Per-server MCP ``timeout`` values above the global MCP ceiling are clamped by
    ``src.mcp_config.resolve_mcp_servers`` before the CLI sees them. A tool
    watchdog breach returns a tool *error* and the turn survives — < the
    gateway's in-flight-tool stall budget ``TOOL_STALL_TIMEOUT`` (defaults to
    the larger watchdog + grace; a breach fails the whole turn) <
    ``ACTIVE_TURN_MAX_AGE`` (the expiry sweep's no-progress valve; a breach
    reclaims the worker underneath a still-open stream). Both guards tick on the
    keepalive timer, so ``SSE_KEEPALIVE_INTERVAL=0`` disables them silently.
    Mirrors the derivation in ``src.constants`` with os.environ only
    (early-import rule). The two timeout inequalities are hard startup
    requirements: if either is impossible, serving traffic would knowingly
    reintroduce timeout inversion, so they are error-severity rather than advice.
    """
    issues: List[ConfigIssue] = []
    keepalive = _int_env("SSE_KEEPALIVE_INTERVAL", 15)
    stream_stall = _int_env("STREAM_STALL_TIMEOUT", 600)
    # src.constants.effective_mcp_tool_timeout_ms(): positive explicit value,
    # otherwise the gateway-owned 600000 ms product default. Non-positive and
    # invalid values are treated as unset.
    mcp_tool_ms = max(_int_env("MCP_TOOL_TIMEOUT", 600_000), 0) or 600_000
    bash_ms = max(_int_env("BASH_MAX_TIMEOUT_MS", 0), 0) or 600_000
    watchdog_ms = max(mcp_tool_ms, bash_ms)
    derived_tool_stall = -(-watchdog_ms // 1000) + 60
    tool_stall = _int_env("TOOL_STALL_TIMEOUT", derived_tool_stall)
    effective_tool_stall = tool_stall or stream_stall
    max_age = _int_env("ACTIVE_TURN_MAX_AGE", 1800)

    if keepalive <= 0 and (stream_stall > 0 or tool_stall > 0):
        issues.append(
            ConfigIssue(
                "warning",
                "SSE_KEEPALIVE_INTERVAL=0 disables the stall guards: "
                "STREAM_STALL_TIMEOUT / TOOL_STALL_TIMEOUT tick on the keepalive "
                "timer and will never fire, so a wedged turn leaks its worker.",
            )
        )
    if effective_tool_stall > 0 and watchdog_ms // 1000 >= effective_tool_stall:
        culprit = (
            f"MCP_TOOL_TIMEOUT={mcp_tool_ms}ms (effective gateway ceiling)"
            if mcp_tool_ms >= bash_ms
            else f"Bash timeout {bash_ms}ms (BASH_MAX_TIMEOUT_MS / CLI max 600000)"
        )
        issues.append(
            ConfigIssue(
                "error",
                f"{culprit} is not below the in-flight tool stall budget "
                f"({effective_tool_stall}s): the gateway will fail the whole turn "
                "before the CLI can time the tool call out and hand the model a tool "
                "error. Raise TOOL_STALL_TIMEOUT above the watchdog/1000 (+ grace) or "
                "lower the watchdog.",
            )
        )
    if max_age > 0 and effective_tool_stall > 0 and max_age <= effective_tool_stall:
        issues.append(
            ConfigIssue(
                "error",
                f"ACTIVE_TURN_MAX_AGE={max_age}s is not above the tool stall budget "
                f"({effective_tool_stall}s): the expiry sweep can reclaim a worker "
                "while a legitimately slow tool call is still within budget. Set "
                "ACTIVE_TURN_MAX_AGE above TOOL_STALL_TIMEOUT / STREAM_STALL_TIMEOUT.",
            )
        )
    return issues


def _check_sdk_buffer() -> List[ConfigIssue]:
    """``CLAUDE_MAX_BUFFER_SIZE`` bounds ONE CLI stdout message, tool results
    included; a breach is a fatal SDK reader error, not a tool error (#183).

    Unset, the gateway installs its own 16 Mi product default
    (``src.constants.GATEWAY_MAX_BUFFER_SIZE_DEFAULT``) instead of the SDK's
    1 Mi. An invalid value silently falls back to that default, and a value at
    or below the SDK's 1 Mi re-creates the oversized-tool-result failure the
    default exists to prevent — both are worth a startup warning. The unit is
    decoded text characters in the pinned SDK, not bytes (see
    ``_get_max_buffer_size``); the messages say so. Mirrors
    ``src.backends.claude.client._get_max_buffer_size`` with os.environ only
    (early-import rule).
    """
    raw = (os.getenv("CLAUDE_MAX_BUFFER_SIZE") or "").strip()
    if not raw:
        return []
    gateway_default = 16 * 1024 * 1024
    sdk_default = 1024 * 1024
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        return [
            ConfigIssue(
                "warning",
                f"CLAUDE_MAX_BUFFER_SIZE={raw!r} is not a positive integer; the "
                f"gateway default ({gateway_default}) applies.",
            )
        ]
    if value <= sdk_default:
        return [
            ConfigIssue(
                "warning",
                f"CLAUDE_MAX_BUFFER_SIZE={value} is at or below the Claude SDK's own "
                f"default of {sdk_default} (decoded text characters, not bytes, in "
                "the pinned SDK): any single tool result larger than this (e.g. an "
                "MCP tool returning inline base64 images) aborts the whole turn "
                "with sdk_error. Unset it to use the gateway default "
                f"({gateway_default}) or raise it.",
            )
        ]
    return []


def _check_workspace_initial_dirs() -> List[ConfigIssue]:
    """Validate creation-only workspace seed directories at startup.

    Mirrors src.workspace_manager._initial_workspace_dirs without importing
    workspace/backend modules during the early startup config check.
    """
    raw = os.getenv("WORKSPACE_INITIAL_DIRS", "")
    if not raw.strip():
        return []

    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        path = Path(value)
        if path.is_absolute() or not path.parts or any(part == ".." for part in path.parts):
            return [
                ConfigIssue(
                    "error",
                    f"WORKSPACE_INITIAL_DIRS contains unsafe entry {value!r}. "
                    "Entries must be relative workspace paths without '..'.",
                )
            ]
    return []

def _check_workspace_quota() -> List[ConfigIssue]:
    """``USER_WORKSPACE_QUOTA_MB`` must be a non-negative integer (MiB).

    ``src.workspace_quota.workspace_quota_limit_bytes`` raises on anything else,
    and it is consulted on every Claude turn (the PreToolUse hook is installed
    when a quota is configured) and on every quota-dependent ``/files/*`` call.
    Left to runtime, a typo therefore fails every turn with an opaque error
    instead of refusing to start with the reason. Mirrors that parser with
    os.environ only (early-import rule).
    """
    raw = (os.getenv("USER_WORKSPACE_QUOTA_MB") or "").strip()
    if not raw:
        return []
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        return [
            ConfigIssue(
                "error",
                f"USER_WORKSPACE_QUOTA_MB={raw!r} is not a non-negative integer "
                "(MiB). Every Claude turn and quota-dependent /files/* call would "
                "fail at runtime; unset it for no quota or set a whole number of MiB.",
            )
        ]
    return []


def check_config() -> List[ConfigIssue]:
    """Inspect the environment and return all detected configuration issues."""
    backends = _enabled_backends()
    issues: List[ConfigIssue] = []
    issues.extend(_check_backends(backends))
    issues.extend(_check_opencode(backends))
    issues.extend(_check_codex(backends))
    issues.extend(_check_usage_log_db_url())
    issues.extend(_check_cors())
    issues.extend(_check_api_key())
    issues.extend(_check_sanitizer())
    issues.extend(_check_default_model(backends))
    issues.extend(_check_mcp_manifest())
    issues.extend(_check_mcp_server_env())
    issues.extend(_check_claude_settings_env())
    issues.extend(_check_stall_hierarchy())
    issues.extend(_check_sdk_buffer())
    issues.extend(_check_workspace_initial_dirs())
    issues.extend(_check_workspace_quota())
    return issues


def run_startup_config_check() -> List[ConfigIssue]:
    """Log all issues; raise ``RuntimeError`` on error-severity ones.

    Bypass entirely with ``SKIP_CONFIG_CHECK=true`` (escape hatch for
    operators who accept the flagged configuration).
    """
    if parse_bool_env("SKIP_CONFIG_CHECK", "false"):
        logger.warning(
            "SKIP_CONFIG_CHECK=true — startup configuration validation skipped"
        )
        return []

    issues = check_config()
    errors = [i for i in issues if i.severity == "error"]
    for issue in issues:
        if issue.severity == "error":
            logger.error("Config error: %s", issue.message)
        else:
            logger.warning("Config warning: %s", issue.message)

    if errors:
        raise RuntimeError(
            f"Refusing to start: {len(errors)} configuration error(s) detected:\n"
            + "\n".join(f"  - {e.message}" for e in errors)
            + "\nFix the settings above, or set SKIP_CONFIG_CHECK=true to bypass "
            "this check."
        )
    return issues
