"""Shared fixtures for tests that mutate module-level application state."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

import src.main as main
import src.session_manager as session_manager_module
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


@pytest.fixture(autouse=True)
def reset_main_state():
    """Restore mutable module state and clean shared session state between tests."""
    original_debug = main.DEBUG_MODE
    original_runtime_api_key = main.runtime_api_key
    original_max_request_size = main.MAX_REQUEST_SIZE

    yield

    main.DEBUG_MODE = original_debug
    main.runtime_api_key = original_runtime_api_key
    main.MAX_REQUEST_SIZE = original_max_request_size
    BackendRegistry.clear()

    seen_managers = set()
    for manager in (main.session_manager, session_manager_module.session_manager):
        if id(manager) in seen_managers:
            continue
        seen_managers.add(id(manager))
        _cleanup_manager(manager)


@pytest.fixture
def isolated_session_manager(monkeypatch):
    """Patch both modules to use a fresh SessionManager for a test."""
    manager = SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=5)
    monkeypatch.setattr(main, "session_manager", manager)
    monkeypatch.setattr(session_manager_module, "session_manager", manager)

    try:
        yield manager
    finally:
        _cleanup_manager(manager)


# ============================================================================
# Codex subprocess mock fixture
# ============================================================================


def _make_codex_stdout_lines(events: list[dict] | None = None, preamble: str = ""):
    """Build a list of stdout byte-lines for a mocked Codex subprocess.

    Args:
        events: JSONL event dicts to emit. Defaults to a simple assistant response.
        preamble: Optional non-JSON text that codex-cli emits before JSONL.
    """
    if events is None:
        events = [
            {"type": "message.delta", "delta": {"content": "Hello from Codex"}},
            {"type": "message.completed"},
        ]

    lines: list[bytes] = []
    if preamble:
        lines.append((preamble + "\n").encode())
    for ev in events:
        lines.append((json.dumps(ev) + "\n").encode())
    return lines


@pytest.fixture
def mock_codex_subprocess(monkeypatch):
    """Mock asyncio.create_subprocess_exec for Codex CLI calls.

    Returns a factory that lets tests customise the stdout output.
    The mock process is pre-configured with a default assistant response.
    """

    class _Factory:
        def __init__(self):
            self.proc = MagicMock()
            self.proc.returncode = 0
            self.proc.pid = 12345
            self._stdout_lines = _make_codex_stdout_lines()
            self._apply_stdout()
            self.proc.stderr = AsyncMock()
            self.proc.stderr.readline = AsyncMock(return_value=b"")
            self.proc.wait = AsyncMock(return_value=0)
            self.captured_env = None

        def set_stdout(self, events: list[dict] | None = None, preamble: str = ""):
            """Override the stdout lines that the mock process will emit."""
            self._stdout_lines = _make_codex_stdout_lines(events, preamble)
            self._apply_stdout()

        def _apply_stdout(self):
            line_iter = iter(self._stdout_lines)

            async def readline():
                try:
                    return next(line_iter)
                except StopIteration:
                    return b""

            self.proc.stdout = MagicMock()
            self.proc.stdout.readline = readline

    factory = _Factory()

    async def fake_create_subprocess_exec(*args, **kwargs):
        factory.captured_env = kwargs.get("env")
        return factory.proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return factory
