"""End-to-end integration tests for Codex CLI backend.

Tests exercise real subprocess spawning via the mock Codex binary
(tests/fixtures/mock_codex_binary.py) to verify the full pipeline:
  stdin prompt → subprocess → JSONL stdout → event normalization → chunk dicts

These tests do NOT require a real OpenAI API key or the actual Codex binary.
"""

import asyncio
import contextlib
import json
import os

import pytest

from src.codex_cli import CodexCLI

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect_chunks(cli: CodexCLI, **kwargs) -> list[dict]:
    """Collect all chunks from run_completion into a list."""
    chunks = []
    async for chunk in cli.run_completion(**kwargs):
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# 1. Basic flow: thread.started → item.completed → turn.completed
# ---------------------------------------------------------------------------


async def test_basic_flow(integration_codex_cli):
    """Default scenario produces codex_session, assistant, and result chunks."""
    chunks = await _collect_chunks(integration_codex_cli, prompt="Hello")

    assert len(chunks) >= 3

    # First: codex_session with thread id
    assert chunks[0]["type"] == "codex_session"
    assert chunks[0]["session_id"] == "mock-thread-001"

    # Assistant chunk with agent_message text
    assistant_chunks = [c for c in chunks if c.get("type") == "assistant"]
    assert len(assistant_chunks) >= 1
    first_assistant = assistant_chunks[0]
    assert first_assistant["content"][0]["type"] == "text"
    assert first_assistant["content"][0]["text"] == "Mock response"

    # Result with success subtype and usage (find by subtype, not position,
    # because a pipe-close race may append a trailing timeout chunk)
    success_results = [
        c for c in chunks if c.get("type") == "result" and c.get("subtype") == "success"
    ]
    assert len(success_results) >= 1, f"No success result in chunks: {chunks}"
    result = success_results[0]
    assert "usage" in result
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5


# ---------------------------------------------------------------------------
# 2. Multi-item: reasoning, command, file_change, agent_message
# ---------------------------------------------------------------------------


async def test_multi_item(integration_codex_cli, monkeypatch):
    """multi_item scenario yields multiple assistant chunk types."""
    monkeypatch.setenv("MOCK_CODEX_SCENARIO", "multi_item")

    chunks = await _collect_chunks(integration_codex_cli, prompt="Do work")

    # codex_session first
    assert chunks[0]["type"] == "codex_session"

    # Collect assistant and system chunks (item.started → system task_started)
    assistant_chunks = [c for c in chunks if c.get("type") == "assistant"]
    system_chunks = [c for c in chunks if c.get("type") == "system"]

    # reasoning → thinking block
    reasoning = assistant_chunks[0]
    assert reasoning["content"][0]["type"] == "thinking"
    assert "think" in reasoning["content"][0]["thinking"].lower()

    # command_execution → tool_use (Bash)
    cmd_chunks = [
        c
        for c in assistant_chunks
        if any(b.get("name") == "Bash" for b in c.get("content", []) if isinstance(b, dict))
    ]
    assert len(cmd_chunks) >= 1

    # file_change → tool_use (Edit)
    file_chunks = [
        c
        for c in assistant_chunks
        if any(b.get("name") == "Edit" for b in c.get("content", []) if isinstance(b, dict))
    ]
    assert len(file_chunks) >= 1

    # agent_message → text
    msg_chunks = [
        c
        for c in assistant_chunks
        if any(
            b.get("type") == "text" and b.get("text") == "I found the files."
            for b in c.get("content", [])
            if isinstance(b, dict)
        )
    ]
    assert len(msg_chunks) >= 1

    # item.started for command_execution → system task_started
    assert len(system_chunks) >= 1
    assert system_chunks[0]["subtype"] == "task_started"

    # Result with usage (find by subtype to avoid pipe-close race)
    success_results = [
        c for c in chunks if c.get("type") == "result" and c.get("subtype") == "success"
    ]
    assert len(success_results) >= 1, f"No success result in chunks: {chunks}"
    result = success_results[0]
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["output_tokens"] == 60


# ---------------------------------------------------------------------------
# 3. Resume: second call passes resume thread_id to subprocess
# ---------------------------------------------------------------------------


async def test_resume_argv(integration_codex_cli, monkeypatch, tmp_path):
    """resume= parameter passes the thread_id to the mock binary as 'resume <id>'."""
    # First call: get thread_id
    chunks_1 = await _collect_chunks(integration_codex_cli, prompt="First turn")
    session_chunk = next(c for c in chunks_1 if c["type"] == "codex_session")
    thread_id = session_chunk["session_id"]

    # Second call with resume and info file to inspect argv
    info_file = tmp_path / "resume_info.json"
    monkeypatch.setenv("MOCK_CODEX_INFO_FILE", str(info_file))

    chunks_2 = await _collect_chunks(
        integration_codex_cli,
        prompt="Follow up",
        resume=thread_id,
    )

    # Verify mock binary received resume <thread_id> in args
    assert info_file.exists(), "Mock binary should write info file"
    info = json.loads(info_file.read_text())
    assert info["resume_id"] == thread_id
    assert "resume" in info["args"]
    assert thread_id in info["args"]

    # Second call should also yield a codex_session chunk
    assert any(c["type"] == "codex_session" for c in chunks_2)


# ---------------------------------------------------------------------------
# 4. Process diagnostics: non-zero exit + error event
# ---------------------------------------------------------------------------


async def test_process_diagnostics_nonzero_exit(integration_codex_cli, monkeypatch):
    """Non-zero exit code alone does NOT produce an error chunk; the JSON
    error event scenario does."""
    # Part A: non-zero exit with basic scenario → stream completes normally
    monkeypatch.setenv("MOCK_CODEX_EXIT_CODE", "1")
    monkeypatch.setenv("MOCK_CODEX_STDERR", "something went wrong")

    chunks = await _collect_chunks(integration_codex_cli, prompt="Test exit")

    # Should still have codex_session + assistant + result (success from JSONL)
    types = [c["type"] for c in chunks]
    assert "codex_session" in types
    assert "result" in types
    # The result comes from the JSONL turn.completed, not from exit code
    success_results = [
        c for c in chunks if c.get("type") == "result" and c.get("subtype") == "success"
    ]
    assert len(success_results) >= 1, "Non-zero exit should not prevent success result"

    # Part B: error scenario → error chunk with message
    monkeypatch.setenv("MOCK_CODEX_SCENARIO", "error")
    monkeypatch.setenv("MOCK_CODEX_EXIT_CODE", "0")
    monkeypatch.delenv("MOCK_CODEX_STDERR", raising=False)

    chunks_err = await _collect_chunks(integration_codex_cli, prompt="Test error")

    error_chunks = [c for c in chunks_err if c.get("is_error")]
    assert len(error_chunks) >= 1
    assert "Rate limit exceeded" in error_chunks[0]["error_message"]


# ---------------------------------------------------------------------------
# 5. Timeout: slow subprocess triggers timeout error
# ---------------------------------------------------------------------------


async def test_timeout(mock_codex_bin, tmp_path, monkeypatch):
    """A subprocess that stalls triggers a timeout error chunk and is cleaned up."""
    monkeypatch.setenv("MOCK_CODEX_SCENARIO", "timeout")
    monkeypatch.setattr("src.codex_cli.CODEX_CLI_PATH", mock_codex_bin)
    monkeypatch.setattr("src.codex_cli.CODEX_CONFIG_ISOLATION", True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-integration")

    # Very short timeout (500ms) to make the test fast
    cli = CodexCLI(timeout=500, cwd=str(tmp_path))

    # Wrap _spawn_codex to capture the subprocess PID for cleanup verification
    captured_pid = None
    original_spawn = cli._spawn_codex

    @contextlib.asynccontextmanager
    async def _capturing_spawn(*args, **kwargs):
        nonlocal captured_pid
        async with original_spawn(*args, **kwargs) as proc:
            captured_pid = proc.pid
            yield proc

    monkeypatch.setattr(cli, "_spawn_codex", _capturing_spawn)

    chunks = await _collect_chunks(cli, prompt="Hang forever")

    # Should yield a timeout error
    error_chunks = [c for c in chunks if c.get("is_error")]
    assert len(error_chunks) >= 1
    assert "timed out" in error_chunks[0]["error_message"].lower()

    # Verify subprocess was terminated by _spawn_codex's finally block
    assert captured_pid is not None, "Should have captured subprocess PID"
    await asyncio.sleep(0.2)  # Brief wait for OS process table cleanup
    try:
        os.kill(captured_pid, 0)  # signal 0 = existence check only
        pytest.fail(f"Mock subprocess (PID {captured_pid}) still running after timeout")
    except ProcessLookupError:
        pass  # Process cleaned up — expected


# ---------------------------------------------------------------------------
# 6. Model and instructions passthrough via argv
# ---------------------------------------------------------------------------


async def test_model_and_instructions_passthrough(integration_codex_cli, monkeypatch, tmp_path):
    """model= and system_prompt= are forwarded to the subprocess as --model and -c flags."""
    info_file = tmp_path / "passthrough_info.json"
    monkeypatch.setenv("MOCK_CODEX_INFO_FILE", str(info_file))

    chunks = await _collect_chunks(
        integration_codex_cli,
        prompt="What is 2+2?",
        model="o3",
        system_prompt="Be helpful",
    )

    assert info_file.exists(), "Mock binary should write info file"
    info = json.loads(info_file.read_text())

    # --model o3
    assert info["model"] == "o3"
    assert "--model" in info["args"]

    # -c instructions=... containing system prompt
    assert info["instructions"] is not None
    assert "Be helpful" in info["instructions"]

    # Stream still completes normally
    assert any(c["type"] == "codex_session" for c in chunks)
    assert any(c["type"] == "result" for c in chunks)


# ---------------------------------------------------------------------------
# 7. Preamble filtering: non-JSON lines are skipped
# ---------------------------------------------------------------------------


async def test_preamble_filtering(integration_codex_cli, monkeypatch):
    """Preamble text lines before JSON are silently filtered out."""
    monkeypatch.setenv("MOCK_CODEX_SCENARIO", "preamble")

    chunks = await _collect_chunks(integration_codex_cli, prompt="Test preamble")

    # All chunks should be valid internal chunk types — no raw text leaks
    valid_types = {"codex_session", "assistant", "result", "system"}
    for chunk in chunks:
        assert chunk["type"] in valid_types, f"Unexpected chunk type: {chunk['type']}"

    # Should still produce the full basic flow
    assert chunks[0]["type"] == "codex_session"

    assistant_chunks = [c for c in chunks if c["type"] == "assistant"]
    assert len(assistant_chunks) >= 1

    success_results = [
        c for c in chunks if c.get("type") == "result" and c.get("subtype") == "success"
    ]
    assert len(success_results) >= 1, f"No success result in preamble test: {chunks}"
