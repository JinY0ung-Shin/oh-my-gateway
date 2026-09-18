"""Unit tests for src.config_check startup configuration validation."""

from unittest.mock import AsyncMock, patch

import pytest

import src.main as main
from src.config_check import (
    ConfigIssue,
    check_config,
    run_startup_config_check,
)

# Every env var the checker inspects — cleared before each test so the
# developer's real .env never leaks into assertions.
_CHECKED_VARS = [
    "BACKENDS",
    "OPENCODE_MODELS",
    "OPENCODE_BASE_URL",
    "OPENCODE_BIN",
    "OPENCODE_HOST",
    "OPENCODE_PORT",
    "OPENCODE_START_TIMEOUT_MS",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_USE_WRAPPER_MCP_CONFIG",
    "MCP_CONFIG",
    "CODEX_MODELS",
    "USAGE_LOG_DB_URL",
    "CORS_ORIGINS",
    "API_KEY",
    "SANITIZER_ENABLED",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS",
    "DEFAULT_MODEL",
    "SKIP_CONFIG_CHECK",
    "GATEWAY_MCP_MANIFEST",
    "GATEWAY_MCP_SERVER_ENV",
    "GATEWAY_CLAUDE_SETTINGS_ENV",
    "CLAUDE_SETTING_SOURCES",
    "SSE_KEEPALIVE_INTERVAL",
    "STREAM_STALL_TIMEOUT",
    "TOOL_STALL_TIMEOUT",
    "ACTIVE_TURN_MAX_AGE",
    "MCP_TOOL_TIMEOUT",
    "BASH_MAX_TIMEOUT_MS",
    "BASH_DEFAULT_TIMEOUT_MS",
    "CLAUDE_MAX_BUFFER_SIZE",
    "WORKSPACE_INITIAL_DIRS",
    "USER_WORKSPACE_QUOTA_MB",
]


@pytest.fixture
def clean_env(monkeypatch):
    """Start each test from a known-good environment (zero issues)."""
    for var in _CHECKED_VARS:
        monkeypatch.delenv(var, raising=False)
    # Baseline that produces no warnings: authenticated, explicit CORS origin.
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("CORS_ORIGINS", '["https://example.com"]')
    return monkeypatch


def _messages(issues, severity):
    return [i.message for i in issues if i.severity == severity]


def _has(issues, severity, *fragments):
    return any(
        all(fragment in msg for fragment in fragments) for msg in _messages(issues, severity)
    )


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_default_config_is_clean(clean_env):
    assert check_config() == []


# ---------------------------------------------------------------------------
# BACKENDS
# ---------------------------------------------------------------------------


def test_unknown_backend_is_error(clean_env):
    clean_env.setenv("BACKENDS", "claude,opencod")

    issues = check_config()

    assert _has(issues, "error", "unknown backend 'opencod'")


def test_known_backends_no_error(clean_env):
    clean_env.setenv("BACKENDS", "claude,opencode,codex")
    clean_env.setenv("OPENCODE_MODELS", "openai/gpt-5.5")
    clean_env.setenv("CODEX_MODELS", "gpt-5.5")

    assert _messages(check_config(), "error") == []


# ---------------------------------------------------------------------------
# OpenCode rules
# ---------------------------------------------------------------------------


def test_opencode_without_models_warns(clean_env):
    clean_env.setenv("BACKENDS", "claude,opencode")

    issues = check_config()

    assert _has(issues, "warning", "OPENCODE_MODELS is unset/empty")


def test_opencode_with_models_no_warning(clean_env):
    clean_env.setenv("BACKENDS", "claude,opencode")
    clean_env.setenv("OPENCODE_MODELS", "openai/gpt-5.5")

    assert not _has(check_config(), "warning", "OPENCODE_MODELS")


def test_opencode_external_mode_warns_on_ignored_managed_vars(clean_env):
    clean_env.setenv("BACKENDS", "claude,opencode")
    clean_env.setenv("OPENCODE_MODELS", "openai/gpt-5.5")
    clean_env.setenv("OPENCODE_BASE_URL", "http://opencode-host:7891")
    clean_env.setenv("OPENCODE_BIN", "/usr/local/bin/opencode")
    clean_env.setenv("OPENCODE_CONFIG_CONTENT", "{}")

    issues = check_config()

    assert _has(
        issues,
        "warning",
        "external mode",
        "OPENCODE_BIN",
        "OPENCODE_CONFIG_CONTENT",
    )


def test_opencode_external_mode_without_managed_vars_is_quiet(clean_env):
    clean_env.setenv("BACKENDS", "claude,opencode")
    clean_env.setenv("OPENCODE_MODELS", "openai/gpt-5.5")
    clean_env.setenv("OPENCODE_BASE_URL", "http://opencode-host:7891")

    assert not _has(check_config(), "warning", "external mode")


def test_opencode_wrapper_mcp_flag_without_mcp_config_warns(clean_env):
    clean_env.setenv("BACKENDS", "claude,opencode")
    clean_env.setenv("OPENCODE_MODELS", "openai/gpt-5.5")
    clean_env.setenv("OPENCODE_USE_WRAPPER_MCP_CONFIG", "true")

    issues = check_config()

    assert _has(issues, "warning", "OPENCODE_USE_WRAPPER_MCP_CONFIG=true", "MCP_CONFIG")


def test_opencode_vars_without_backend_warn(clean_env):
    clean_env.setenv("OPENCODE_MODELS", "openai/gpt-5.5")

    issues = check_config()

    assert _has(issues, "warning", "OPENCODE_MODELS", "not in BACKENDS")


# ---------------------------------------------------------------------------
# Codex rules
# ---------------------------------------------------------------------------


def test_codex_without_models_warns_about_default(clean_env):
    clean_env.setenv("BACKENDS", "claude,codex")

    issues = check_config()

    assert _has(issues, "warning", "CODEX_MODELS is unset", "gpt-5.5")


def test_codex_with_empty_models_warns(clean_env):
    clean_env.setenv("BACKENDS", "claude,codex")
    clean_env.setenv("CODEX_MODELS", "  ")

    issues = check_config()

    assert _has(issues, "warning", "CODEX_MODELS is empty")


def test_codex_models_without_backend_warn(clean_env):
    clean_env.setenv("CODEX_MODELS", "gpt-5.5")

    issues = check_config()

    assert _has(issues, "warning", "CODEX_MODELS", "not in BACKENDS")


# ---------------------------------------------------------------------------
# USAGE_LOG_DB_URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "mysql://gateway:gw@db:3306/gateway_log",
        "mysql+aiomysql://gateway:gw@db:3306/gateway_log",
        "mariadb://gateway:gw@db:3306/gateway_log",
        "sqlite+aiosqlite:///./data/gateway_log.sqlite",
    ],
)
def test_usage_log_supported_schemes_pass(clean_env, url):
    clean_env.setenv("USAGE_LOG_DB_URL", url)

    assert _messages(check_config(), "error") == []


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://gateway:gw@db:5432/gateway_log",
        "postgresql+asyncpg://gateway:gw@db:5432/gateway_log",
        "not-a-url",
    ],
)
def test_usage_log_unsupported_scheme_is_error(clean_env, url):
    clean_env.setenv("USAGE_LOG_DB_URL", url)

    issues = check_config()

    assert _has(issues, "error", "USAGE_LOG_DB_URL", "lastrowid")


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_cors_wildcard_warns_about_credentials(clean_env):
    clean_env.setenv("CORS_ORIGINS", '["*"]')

    issues = check_config()

    assert _has(issues, "warning", "CORS_ORIGINS", "allow_credentials")


def test_cors_unset_defaults_to_wildcard_warning(clean_env):
    clean_env.delenv("CORS_ORIGINS", raising=False)

    issues = check_config()

    assert _has(issues, "warning", "CORS_ORIGINS", "allow_credentials")


def test_cors_invalid_json_is_error(clean_env):
    clean_env.setenv("CORS_ORIGINS", "https://example.com")

    issues = check_config()

    assert _has(issues, "error", "CORS_ORIGINS is not valid JSON")


def test_cors_non_list_json_is_error(clean_env):
    clean_env.setenv("CORS_ORIGINS", '{"origin": "https://example.com"}')

    issues = check_config()

    assert _has(issues, "error", "CORS_ORIGINS must be a JSON list")


# ---------------------------------------------------------------------------
# API_KEY
# ---------------------------------------------------------------------------


def test_missing_api_key_warns(clean_env):
    clean_env.delenv("API_KEY", raising=False)

    issues = check_config()

    assert _has(issues, "warning", "API_KEY is unset")


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------


def test_sanitizer_without_upstream_warns(clean_env):
    clean_env.setenv("SANITIZER_ENABLED", "true")

    issues = check_config()

    assert _has(issues, "warning", "SANITIZER_ENABLED=true", "ANTHROPIC_BASE_URL")


def test_sanitizer_with_upstream_is_quiet(clean_env):
    clean_env.setenv("SANITIZER_ENABLED", "true")
    clean_env.setenv("ANTHROPIC_BASE_URL", "http://litellm:4000")

    assert not _has(check_config(), "warning", "SANITIZER_ENABLED")


# ---------------------------------------------------------------------------
# DEFAULT_MODEL vs enabled backends
# ---------------------------------------------------------------------------


def test_default_model_unresolvable_without_claude_warns(clean_env):
    clean_env.setenv("BACKENDS", "codex")
    clean_env.setenv("CODEX_MODELS", "gpt-5.5")
    # DEFAULT_MODEL unset -> "sonnet", a Claude-only bare alias.

    issues = check_config()

    assert _has(issues, "warning", "DEFAULT_MODEL", "not served by any enabled backend")


def test_default_model_matching_enabled_backend_is_quiet(clean_env):
    clean_env.setenv("BACKENDS", "codex")
    clean_env.setenv("CODEX_MODELS", "gpt-5.5")
    clean_env.setenv("DEFAULT_MODEL", "codex/gpt-5.5")

    assert not _has(check_config(), "warning", "DEFAULT_MODEL")


# ---------------------------------------------------------------------------
# GATEWAY_MCP_SERVER_ENV (per-MCP-server credential overlay declared in env)
# ---------------------------------------------------------------------------


def test_mcp_server_env_valid_inline_json_is_quiet(clean_env):
    clean_env.setenv(
        "GATEWAY_MCP_SERVER_ENV", '{"context7": {"env": {"K": "{{env:TOK}}"}}}'
    )

    assert not _has(check_config(), "warning", "GATEWAY_MCP_SERVER_ENV")


def test_mcp_server_env_invalid_json_warns(clean_env):
    clean_env.setenv("GATEWAY_MCP_SERVER_ENV", '{"context7": ')

    assert _has(
        check_config(), "warning", "GATEWAY_MCP_SERVER_ENV", "valid inline JSON"
    )


def test_mcp_server_env_non_object_warns(clean_env):
    clean_env.setenv("GATEWAY_MCP_SERVER_ENV", '["context7"]')

    assert _has(check_config(), "warning", "GATEWAY_MCP_SERVER_ENV", "JSON object")


def test_mcp_server_env_missing_file_warns(clean_env):
    """A path typo parses as neither file nor JSON — the loader would drop it."""
    clean_env.setenv("GATEWAY_MCP_SERVER_ENV", "/nope/mcp-server-env.json")

    assert _has(
        check_config(), "warning", "GATEWAY_MCP_SERVER_ENV", "neither an existing file"
    )


def test_mcp_server_env_file_path_is_quiet(clean_env, tmp_path):
    path = tmp_path / "mcp-server-env.json"
    path.write_text('{"context7": {"env": {"K": "v"}}}', encoding="utf-8")
    clean_env.setenv("GATEWAY_MCP_SERVER_ENV", str(path))

    assert not _has(check_config(), "warning", "GATEWAY_MCP_SERVER_ENV")


def test_mcp_server_env_unreadable_file_warns(clean_env, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")
    clean_env.setenv("GATEWAY_MCP_SERVER_ENV", str(path))

    assert _has(
        check_config(), "warning", "GATEWAY_MCP_SERVER_ENV", "not readable JSON"
    )


# ---------------------------------------------------------------------------
# GATEWAY_CLAUDE_SETTINGS_ENV (env vars projected into ~/.claude/settings.json)
# ---------------------------------------------------------------------------


def test_claude_settings_env_with_user_source_is_quiet(clean_env):
    clean_env.setenv("GATEWAY_CLAUDE_SETTINGS_ENV", '{"TEAM_ID": "platform"}')
    clean_env.setenv("CLAUDE_SETTING_SOURCES", "user,project,local")

    assert not _has(check_config(), "warning", "GATEWAY_CLAUDE_SETTINGS_ENV")


def test_claude_settings_env_without_user_source_warns(clean_env):
    """Default sources omit 'user', so the projected file would never be read."""
    clean_env.setenv("GATEWAY_CLAUDE_SETTINGS_ENV", '{"TEAM_ID": "platform"}')
    # CLAUDE_SETTING_SOURCES unset -> project,local

    assert _has(
        check_config(),
        "warning",
        "GATEWAY_CLAUDE_SETTINGS_ENV",
        "does not include 'user'",
    )


def test_claude_settings_env_invalid_json_warns(clean_env):
    clean_env.setenv("GATEWAY_CLAUDE_SETTINGS_ENV", "{oops")
    clean_env.setenv("CLAUDE_SETTING_SOURCES", "user")

    assert _has(
        check_config(), "warning", "GATEWAY_CLAUDE_SETTINGS_ENV", "valid inline JSON"
    )


def test_claude_settings_env_non_object_warns(clean_env):
    clean_env.setenv("GATEWAY_CLAUDE_SETTINGS_ENV", '["TEAM_ID"]')
    clean_env.setenv("CLAUDE_SETTING_SOURCES", "user")

    assert _has(check_config(), "warning", "GATEWAY_CLAUDE_SETTINGS_ENV", "JSON object")


def test_claude_settings_env_file_path_is_quiet(clean_env, tmp_path):
    path = tmp_path / "session-env.json"
    path.write_text('{"TEAM_ID": "platform"}', encoding="utf-8")
    clean_env.setenv("GATEWAY_CLAUDE_SETTINGS_ENV", str(path))
    clean_env.setenv("CLAUDE_SETTING_SOURCES", "user,project,local")

    assert not _has(check_config(), "warning", "GATEWAY_CLAUDE_SETTINGS_ENV")


# ---------------------------------------------------------------------------
# run_startup_config_check: fail-fast + escape hatch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLAUDE_MAX_BUFFER_SIZE — one CLI stdout message bounds a tool result (#183)
# ---------------------------------------------------------------------------


def test_sdk_buffer_unset_is_quiet(clean_env):
    """Unset → the gateway's 16 MiB default applies silently."""
    assert not [m for m in _messages(check_config(), "warning") if "BUFFER" in m]


def test_sdk_buffer_above_sdk_default_is_quiet(clean_env):
    clean_env.setenv("CLAUDE_MAX_BUFFER_SIZE", str(2 * 1024 * 1024))
    assert not [m for m in _messages(check_config(), "warning") if "BUFFER" in m]


@pytest.mark.parametrize("value", ["1048576", "4096"])
def test_sdk_buffer_at_or_below_sdk_default_warns(clean_env, value):
    """Re-creating the SDK's 1 MiB framing limit is the #183 failure mode."""
    clean_env.setenv("CLAUDE_MAX_BUFFER_SIZE", value)
    warnings = [m for m in _messages(check_config(), "warning") if "BUFFER" in m]
    assert len(warnings) == 1
    assert "sdk_error" in warnings[0]
    assert "1048576" in warnings[0]
    assert not _messages(check_config(), "error")


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_sdk_buffer_invalid_value_warns_about_fallback(clean_env, value):
    clean_env.setenv("CLAUDE_MAX_BUFFER_SIZE", value)
    warnings = [m for m in _messages(check_config(), "warning") if "BUFFER" in m]
    assert len(warnings) == 1
    assert "not a positive integer" in warnings[0]
    assert str(16 * 1024 * 1024) in warnings[0]


def test_run_startup_check_raises_on_error_severity(clean_env):
    clean_env.setenv("BACKENDS", "claude,bogus")

    with pytest.raises(RuntimeError, match="Refusing to start"):
        run_startup_config_check()


def test_run_startup_check_returns_warnings_without_raising(clean_env):
    clean_env.delenv("API_KEY", raising=False)

    issues = run_startup_config_check()

    assert all(i.severity == "warning" for i in issues)
    assert _has(issues, "warning", "API_KEY is unset")


def test_skip_config_check_escape_hatch(clean_env):
    clean_env.setenv("BACKENDS", "claude,bogus")
    clean_env.setenv("SKIP_CONFIG_CHECK", "true")

    assert run_startup_config_check() == []


def test_config_issue_shape():
    issue = ConfigIssue(severity="warning", message="something odd")

    assert issue.severity == "warning"
    assert issue.message == "something odd"


# ---------------------------------------------------------------------------
# Lifespan integration: error blocks startup, escape hatch lets it proceed
# ---------------------------------------------------------------------------


def _lifespan_patches():
    return (
        patch("src.admin_auth.validate_admin_config"),
        patch.object(
            main,
            "validate_claude_code_auth",
            return_value=(True, {"method": "claude_cli"}),
        ),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(main, "discover_backends"),
        patch.object(main, "_verify_backends", AsyncMock()),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", AsyncMock()),
    )


async def test_lifespan_blocks_startup_on_config_error(clean_env):
    clean_env.setenv("BACKENDS", "claude,bogus")
    p1, p2, p3, p4, p5, p6, p7 = _lifespan_patches()

    with p1, p2, p3, p4, p5, p6, p7:
        with pytest.raises(RuntimeError, match="Refusing to start"):
            async with main.lifespan(main.app):
                pass


async def test_lifespan_escape_hatch_allows_startup(clean_env):
    clean_env.setenv("BACKENDS", "claude,bogus")
    clean_env.setenv("SKIP_CONFIG_CHECK", "true")
    p1, p2, p3, p4, p5, p6, p7 = _lifespan_patches()

    started = False
    with p1, p2, p3, p4, p5, p6, p7:
        async with main.lifespan(main.app):
            started = True

    assert started


# ---------------------------------------------------------------------------
# WORKSPACE_INITIAL_DIRS — creation-only starter directory configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Documents,Projects/AI,.config",
        " Documents , Projects/AI ",
        "",
        ",,",
    ],
)
def test_workspace_initial_dirs_valid_values_are_quiet(clean_env, value):
    clean_env.setenv("WORKSPACE_INITIAL_DIRS", value)
    assert not [
        m
        for m in _messages(check_config(), "error")
        if "WORKSPACE_INITIAL_DIRS" in m
    ]


@pytest.mark.parametrize(
    "value",
    [
        "../escape",
        "safe/../../escape",
        "/absolute/path",
        ".",
    ],
)
def test_workspace_initial_dirs_unsafe_value_is_startup_error(clean_env, value):
    clean_env.setenv("WORKSPACE_INITIAL_DIRS", value)
    errors = [
        m
        for m in _messages(check_config(), "error")
        if "WORKSPACE_INITIAL_DIRS" in m
    ]
    assert len(errors) == 1
    assert "relative workspace paths" in errors[0]
    assert repr(value) in errors[0]


@pytest.mark.parametrize(
    "value",
    [
        ".claude/skills",
        ".claude/skills/review",
        ".claude/agents",
        "Documents,.claude/agents/triage",
    ],
)
def test_workspace_initial_dirs_reserved_namespace_is_startup_error(clean_env, value):
    """The startup check must refuse what the workspace manager refuses.

    ``.claude/{skills,agents}`` is the Claude backend's managed mirror of the
    canonical resource roots. Both surfaces parse through
    ``env_utils.parse_workspace_initial_dirs`` precisely so a reserved path
    cannot pass startup and then be seeded at resolve time.
    """
    clean_env.setenv("WORKSPACE_INITIAL_DIRS", value)
    errors = [m for m in _messages(check_config(), "error") if "WORKSPACE_INITIAL_DIRS" in m]
    assert len(errors) == 1
    assert "manages as a mirror" in errors[0]
    assert "skills/ or agents/" in errors[0]


def test_workspace_initial_dirs_allows_the_canonical_resource_roots(clean_env):
    clean_env.setenv("WORKSPACE_INITIAL_DIRS", "skills/review,agents/triage,.claude")
    assert not [m for m in _messages(check_config(), "error") if "WORKSPACE_INITIAL_DIRS" in m]


# ---------------------------------------------------------------------------
# USER_WORKSPACE_QUOTA_MB — consulted on every Claude turn once configured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["500", "0", " 12 "])
def test_workspace_quota_valid_values_are_quiet(clean_env, value):
    clean_env.setenv("USER_WORKSPACE_QUOTA_MB", value)
    assert not [m for m in _messages(check_config(), "error") if "QUOTA" in m]
    assert not [m for m in _messages(check_config(), "warning") if "QUOTA" in m]


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "500MB"])
def test_workspace_quota_invalid_value_is_startup_error(clean_env, value):
    """A typo must refuse startup, not fail every turn at runtime."""
    clean_env.setenv("USER_WORKSPACE_QUOTA_MB", value)
    errors = [m for m in _messages(check_config(), "error") if "QUOTA" in m]
    assert len(errors) == 1
    assert "non-negative integer" in errors[0]
    assert repr(value) in errors[0]


# ---------------------------------------------------------------------------
# CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS
# ---------------------------------------------------------------------------


def _effort_issues():
    return [i for i in check_config() if "CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS" in i.message]


def test_effort_certification_unset_is_quiet(clean_env):
    assert _effort_issues() == []


def test_effort_certification_valid_on_custom_upstream_is_quiet(clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
    monkeypatch.setenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", "qwen3.6-27b=low|medium|xhigh,*")
    assert _effort_issues() == []


@pytest.mark.parametrize("raw", ["qwen3.6-27b=low|medium|xhi", "qwen3.6-27b=", "=low"])
def test_effort_certification_typo_is_a_startup_error(clean_env, monkeypatch, raw):
    """A bad declaration certifies nothing at runtime — the control it was meant to
    enable would just stay hidden. Refuse to start rather than ship that."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
    monkeypatch.setenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", raw)
    issues = _effort_issues()
    assert len(issues) == 1 and issues[0].severity == "error", issues
    with pytest.raises(RuntimeError, match="CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS"):
        run_startup_config_check()


def test_effort_certification_without_custom_upstream_warns(clean_env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", "*")
    issues = _effort_issues()
    assert len(issues) == 1 and issues[0].severity == "warning", issues
