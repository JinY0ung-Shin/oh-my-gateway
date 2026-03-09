"""Integration test fixtures for Codex backend.

These fixtures are intentionally separate from tests/conftest.py so that
existing unit tests remain completely unaffected.
"""

import os
import shutil
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import pytest

from src.backend_registry import BackendRegistry

# ---------------------------------------------------------------------------
# Skip markers
# ---------------------------------------------------------------------------

requires_codex_binary = pytest.mark.skipif(
    not (os.getenv("RUN_CODEX_BINARY_TESTS") and shutil.which("codex")),
    reason="Set RUN_CODEX_BINARY_TESTS=1 and install Codex CLI to run",
)

requires_openai_key = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)

# ---------------------------------------------------------------------------
# JSONL scenario constants (aligned to src/codex_cli.py normalizer)
# ---------------------------------------------------------------------------

CODEX_EVENTS_BASIC = [
    {"type": "thread.started", "thread_id": "t-test-001"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Hello from Codex"}},
    {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 25}},
]

CODEX_EVENTS_MULTI_ITEM = [
    {"type": "thread.started", "thread_id": "t-test-002"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "Let me think..."}},
    {
        "type": "item.started",
        "item": {"type": "command_execution", "command": "ls -la"},
    },
    {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "ls -la",
            "exit_code": 0,
            "output": "file1.py\nfile2.py",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "changes": [{"kind": "update", "path": "main.py"}],
        },
    },
    {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "I found the files."},
    },
    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 60}},
]

CODEX_EVENTS_ERROR = [
    {"type": "thread.started", "thread_id": "t-test-err"},
    {"type": "error", "message": "Rate limit exceeded"},
]

CODEX_EVENTS_TURN_FAILED = [
    {"type": "thread.started", "thread_id": "t-test-fail"},
    {"type": "turn.started"},
    {"type": "turn.failed", "message": "Internal error"},
]

# ---------------------------------------------------------------------------
# Mock binary fixtures
# ---------------------------------------------------------------------------

_MOCK_BINARY_PATH = Path(__file__).parent.parent / "fixtures" / "mock_codex_binary.py"


@pytest.fixture
def mock_codex_bin(tmp_path):
    """Create an executable shell wrapper that forwards args to the mock script."""
    wrapper = tmp_path / "codex"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {_MOCK_BINARY_PATH} "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.fixture
def integration_codex_cli(mock_codex_bin, tmp_path, monkeypatch):
    """Return a CodexCLI instance backed by the mock binary.

    Uses monkeypatch on the module-level constant so the constructor's
    ``_find_codex_binary()`` returns our mock wrapper.
    """
    monkeypatch.setattr("src.codex_cli.CODEX_CLI_PATH", mock_codex_bin)
    monkeypatch.setattr("src.codex_cli.CODEX_CONFIG_ISOLATION", True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-integration")

    from src.codex_cli import CodexCLI

    return CodexCLI(timeout=5000, cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# FakeCodexBackend (for session integration tests)
# ---------------------------------------------------------------------------


class FakeCodexBackend:
    """Minimal BackendClient Protocol implementation for session tests."""

    def __init__(self, thread_id: str = "fake-thread-123"):
        self.thread_id = thread_id
        self.calls: List[Dict[str, Any]] = []

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        self.calls.append(
            {"prompt": prompt, "resume": resume, "model": model, "session_id": session_id}
        )
        yield {"type": "codex_session", "session_id": self.thread_id}
        yield {"type": "assistant", "content": [{"type": "text", "text": "codex reply"}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": "codex reply",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        return "codex reply"

    def estimate_token_usage(
        self, prompt: str, completion: str, model: Optional[str] = None
    ) -> Dict[str, int]:
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def verify(self) -> bool:
        return True


@pytest.fixture
def fake_codex_backend():
    """Register a FakeCodexBackend and clean up after the test."""
    backend = FakeCodexBackend()
    BackendRegistry.register("codex", backend)
    yield backend
    BackendRegistry.unregister("codex")
