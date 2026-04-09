"""Shared fixtures for tests that mutate module-level application state."""

import pytest

import src.main as main
import src.session_manager as session_manager_module
import src.routes.responses as responses_module
from src.auth import auth_manager
from src.backend_registry import BackendRegistry
from src.session_manager import SessionManager


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

    BackendRegistry.register_descriptor(CLAUDE_DESCRIPTOR)


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
