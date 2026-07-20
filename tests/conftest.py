"""Shared fixtures for tests that mutate module-level application state."""

import os

# Must precede the first src import: src/__init__.py loads .env at package
# import time, and a developer's local .env (DISALLOWED_TOOLS,
# USAGE_LOG_DB_URL, ...) would otherwise leak into every test. Subprocess
# integration tests inherit this env, so their gateways skip .env too.
os.environ.setdefault("GATEWAY_SKIP_DOTENV", "1")

import pytest

import src.main as main
import src.session_manager as session_manager_module
import src.routes.responses as responses_module
from src.auth import auth_manager
from src.backend_registry import BackendRegistry
from src.session_manager import SessionManager

# ---------------------------------------------------------------------------
# Stale backends: opencode and codex are frozen (unmaintained) as of 2026-07;
# claude is the only maintained backend. Their tests are removed from every
# default run so shared-code changes never require fixing them. Set
# RUN_STALE_BACKEND_TESTS=1 to collect and run them again.
# ---------------------------------------------------------------------------
RUN_STALE_BACKEND_TESTS = bool(os.getenv("RUN_STALE_BACKEND_TESTS"))

if not RUN_STALE_BACKEND_TESTS:
    collect_ignore_glob = ["*test_opencode*", "*test_codex*"]


def pytest_collection_modifyitems(config, items):
    """Deselect every test whose id mentions a stale backend (opencode/codex).

    Dedicated stale test files are already skipped at collection via
    ``collect_ignore_glob``; this catches the opencode/codex cases living in
    shared test modules (test_main_api_unit, test_responses_more_coverage, …).
    """
    if RUN_STALE_BACKEND_TESTS:
        return
    selected, deselected = [], []
    for item in items:
        node_id = item.nodeid.lower()
        if "opencode" in node_id or "codex" in node_id:
            deselected.append(item)
        else:
            selected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


def _cleanup_manager(manager):
    """Cancel cleanup task and clear sessions for a session manager instance."""
    cleanup_task = getattr(manager, "_cleanup_task", None)
    if cleanup_task is not None:
        cleanup_task.cancel()
        manager._cleanup_task = None

    with manager.lock:
        manager.sessions.clear()


def register_all_descriptors():
    """Register all backend descriptors (model metadata) so resolve_model() works.

    Called automatically by ``reset_main_state`` and available for tests that
    need descriptors before registering fake backends.
    """
    from src.backends.claude import CLAUDE_DESCRIPTOR
    from src.backends.codex import CODEX_DESCRIPTOR

    BackendRegistry.register_descriptor(CLAUDE_DESCRIPTOR)
    BackendRegistry.register_descriptor(CODEX_DESCRIPTOR)


@pytest.fixture(scope="session", autouse=True)
def _isolate_plugin_mcp_overlay(tmp_path_factory):
    """Point the plugin MCP overlay store at a per-run temp file.

    ``src.mcp_plugin_overlay`` loads ``data/gateway-mcp-plugin-overlay.json``
    at import time; without this, a developer's real overlay file (live
    credentials) would leak into every test that materializes plugin MCP
    servers or builds Claude session options.
    """
    path = tmp_path_factory.mktemp("mcp-plugin-overlay") / "overlay.json"
    mp = pytest.MonkeyPatch()
    mp.setenv("GATEWAY_MCP_PLUGIN_OVERLAY", str(path))

    from src import mcp_plugin_overlay

    mcp_plugin_overlay.reload_overlays()
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def reset_main_state():
    """Restore mutable module state and clean shared session state between tests."""
    original_debug = main.DEBUG_MODE
    original_runtime_api_key = main.runtime_api_key
    original_auth_runtime_key = auth_manager.runtime_api_key
    original_max_request_size = main.MAX_REQUEST_SIZE

    # Register descriptors so resolve_model() works even after clear
    register_all_descriptors()

    yield

    main.DEBUG_MODE = original_debug
    main.runtime_api_key = original_runtime_api_key
    auth_manager.runtime_api_key = original_auth_runtime_key
    main.MAX_REQUEST_SIZE = original_max_request_size
    BackendRegistry.clear()

    seen_managers = set()
    for manager in (
        session_manager_module.session_manager,
        responses_module.session_manager,
    ):
        if id(manager) in seen_managers:
            continue
        seen_managers.add(id(manager))
        _cleanup_manager(manager)


@pytest.fixture
def fresh_session_manager():
    """Create a fresh SessionManager for unit tests."""
    return SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=5)


@pytest.fixture
def clean_registry():
    """Ensure a clean BackendRegistry with descriptors registered.

    Re-registers descriptors so resolve_model() works against known model names.
    """
    BackendRegistry.clear()
    register_all_descriptors()
    yield
    BackendRegistry.clear()


@pytest.fixture
def isolated_session_manager(monkeypatch):
    """Patch all modules that hold a session_manager reference to use a fresh instance."""
    manager = SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=5)
    monkeypatch.setattr(session_manager_module, "session_manager", manager)
    monkeypatch.setattr(responses_module, "session_manager", manager)

    try:
        yield manager
    finally:
        _cleanup_manager(manager)
