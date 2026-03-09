"""Unit tests for src/codex_cli.py — Codex CLI subprocess wrapper.

Covers:
- JSONL parsing and preamble filtering
- Event normalization (all Codex event types → internal chunk dicts)
- Binary discovery
- Environment construction and config isolation
- Subprocess lifecycle (run_completion)
- parse_message and estimate_token_usage
- verify
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.codex_cli import (
    CodexCLI,
    normalize_codex_event,
    _build_content_blocks,
    _extract_text_and_collab,
    _normalize_usage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def codex_cli(tmp_path, monkeypatch):
    """Create a CodexCLI with mocked binary discovery."""
    fake_bin = tmp_path / "codex"
    fake_bin.touch()
    fake_bin.chmod(0o755)
    monkeypatch.setattr("src.codex_cli.CODEX_CLI_PATH", str(fake_bin))
    monkeypatch.setattr("src.codex_cli.CODEX_CONFIG_ISOLATION", True)
    return CodexCLI(timeout=5000, cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


class TestNormalizeCodexEvent:
    def test_item_completed_agent_message(self):
        event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Hello world"},
        }
        result = normalize_codex_event(event)
        assert result["type"] == "assistant"
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Hello world"

    def test_item_completed_reasoning(self):
        event = {
            "type": "item.completed",
            "item": {"type": "reasoning", "text": "Let me think..."},
        }
        result = normalize_codex_event(event)
        assert result["type"] == "assistant"
        assert result["content"][0]["type"] == "thinking"
        assert result["content"][0]["thinking"] == "Let me think..."

    def test_item_completed_command_execution(self):
        event = {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "ls -la"},
        }
        result = normalize_codex_event(event)
        assert result["type"] == "assistant"
        block = result["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "Bash"
        assert block["input"]["command"] == "ls -la"

    def test_item_completed_file_change(self):
        event = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [
                    {"path": "src/foo.py", "kind": "update"},
                    {"path": "src/bar.py", "kind": "add"},
                ],
            },
        }
        result = normalize_codex_event(event)
        assert result["type"] == "assistant"
        assert len(result["content"]) == 2
        assert result["content"][0]["name"] == "Edit"
        assert result["content"][1]["name"] == "Write"

    def test_item_completed_mcp_tool_call(self):
        event = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "filesystem",
                "tool": "read_file",
                "arguments": {"path": "/tmp/test.txt"},
            },
        }
        result = normalize_codex_event(event)
        block = result["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "mcp_filesystem_read_file"
        assert block["input"]["path"] == "/tmp/test.txt"

    def test_item_completed_web_search(self):
        event = {
            "type": "item.completed",
            "item": {"type": "web_search", "query": "Python asyncio"},
        }
        result = normalize_codex_event(event)
        block = result["content"][0]
        assert block["name"] == "WebSearch"
        assert block["input"]["query"] == "Python asyncio"

    def test_item_completed_error(self):
        event = {
            "type": "item.completed",
            "item": {"type": "error", "message": "Something broke"},
        }
        result = normalize_codex_event(event)
        assert "[Codex error: Something broke]" in result["content"][0]["text"]

    def test_item_started_command(self):
        event = {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "npm test"},
        }
        result = normalize_codex_event(event)
        assert result["type"] == "system"
        assert result["subtype"] == "task_started"
        assert "npm test" in result["description"]

    def test_item_started_non_command_returns_none(self):
        event = {
            "type": "item.started",
            "item": {"type": "agent_message"},
        }
        assert normalize_codex_event(event) is None

    def test_item_updated_todo_list(self):
        event = {
            "type": "item.updated",
            "item": {
                "type": "todo_list",
                "items": [
                    {"text": "Task A", "completed": True},
                    {"text": "Task B", "completed": False},
                ],
            },
        }
        result = normalize_codex_event(event)
        assert result["type"] == "system"
        assert result["subtype"] == "task_progress"
        assert "1/2" in result["description"]

    def test_turn_started_returns_none(self):
        assert normalize_codex_event({"type": "turn.started"}) is None

    def test_turn_completed(self):
        event = {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cached_tokens": 10},
        }
        result = normalize_codex_event(event)
        assert result["type"] == "result"
        assert result["subtype"] == "success"
        assert result["usage"]["input_tokens"] == 100
        assert result["usage"]["output_tokens"] == 50
        assert result["usage"]["cache_read_input_tokens"] == 10

    def test_turn_completed_no_usage(self):
        event = {"type": "turn.completed"}
        result = normalize_codex_event(event)
        assert result["type"] == "result"
        assert result["subtype"] == "success"

    def test_turn_failed(self):
        event = {"type": "turn.failed", "message": "Rate limited"}
        result = normalize_codex_event(event)
        assert result["is_error"] is True
        assert "Rate limited" in result["error_message"]

    def test_error_event(self):
        event = {"type": "error", "message": "Connection lost"}
        result = normalize_codex_event(event)
        assert result["is_error"] is True
        assert "Connection lost" in result["error_message"]

    def test_unknown_event_returns_none(self):
        assert normalize_codex_event({"type": "unknown.event"}) is None

    def test_thread_started_not_normalized(self):
        """thread.started is handled by run_completion, not normalize."""
        assert normalize_codex_event({"type": "thread.started", "thread_id": "t1"}) is None


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


class TestBuildContentBlocks:
    def test_unknown_item_type_fallback(self):
        item = {"type": "new_future_type", "data": 42}
        blocks = _build_content_blocks(item)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"

    def test_file_change_empty_changes(self):
        item = {"type": "file_change", "changes": []}
        blocks = _build_content_blocks(item)
        assert blocks[0]["text"] == "[file change]"

    def test_agent_message_empty_text(self):
        item = {"type": "agent_message", "text": ""}
        blocks = _build_content_blocks(item)
        assert blocks == []

    def test_file_change_delete_kind(self):
        item = {"type": "file_change", "changes": [{"path": "old.py", "kind": "delete"}]}
        blocks = _build_content_blocks(item)
        # delete is not "update" so should map to Write
        assert blocks[0]["name"] == "Write"


# ---------------------------------------------------------------------------
# Usage normalization
# ---------------------------------------------------------------------------


class TestNormalizeUsage:
    def test_full_usage(self):
        raw = {"input_tokens": 200, "output_tokens": 80, "cached_tokens": 50}
        result = _normalize_usage(raw)
        assert result["input_tokens"] == 200
        assert result["output_tokens"] == 80
        assert result["cache_read_input_tokens"] == 50
        assert result["cache_creation_input_tokens"] == 0

    def test_empty_usage(self):
        result = _normalize_usage({})
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


def _make_reader(lines: list) -> asyncio.StreamReader:
    """Create a StreamReader pre-loaded with byte lines."""
    reader = asyncio.StreamReader()
    for line in lines:
        data = line if isinstance(line, bytes) else line.encode()
        reader.feed_data(data)
    reader.feed_eof()
    return reader


class TestParseJsonl:
    async def test_parses_valid_jsonl(self):
        lines = [
            b'{"type": "thread.started", "thread_id": "t1"}\n',
            b'{"type": "turn.completed", "usage": {}}\n',
        ]
        reader = _make_reader(lines)
        events = [e async for e in CodexCLI._parse_jsonl(reader)]
        assert len(events) == 2
        assert events[0]["type"] == "thread.started"
        assert events[1]["type"] == "turn.completed"

    async def test_skips_preamble_lines(self):
        lines = [
            b"Reading prompt from stdin...\n",
            b"\n",
            b'{"type": "turn.started"}\n',
        ]
        reader = _make_reader(lines)
        events = [e async for e in CodexCLI._parse_jsonl(reader)]
        assert len(events) == 1
        assert events[0]["type"] == "turn.started"

    async def test_skips_malformed_json(self):
        lines = [
            b"{broken json\n",
            b'{"type": "ok"}\n',
        ]
        reader = _make_reader(lines)
        events = [e async for e in CodexCLI._parse_jsonl(reader)]
        assert len(events) == 1
        assert events[0]["type"] == "ok"

    async def test_empty_input(self):
        reader = _make_reader([])
        events = [e async for e in CodexCLI._parse_jsonl(reader)]
        assert events == []

    async def test_mixed_preamble_and_events(self):
        lines = [
            b"Warning: something\n",
            b'{"type": "a"}\n',
            b"Another warning\n",
            b'{"type": "b"}\n',
        ]
        reader = _make_reader(lines)
        events = [e async for e in CodexCLI._parse_jsonl(reader)]
        assert len(events) == 2
        assert events[0]["type"] == "a"
        assert events[1]["type"] == "b"

    async def test_line_timeout_none_behaves_normally(self):
        """Explicit line_timeout=None should behave like the default."""
        lines = [b'{"type": "ok"}\n']
        reader = _make_reader(lines)
        events = [e async for e in CodexCLI._parse_jsonl(reader, line_timeout=None)]
        assert len(events) == 1

    async def test_line_timeout_breaks_on_stalled_readline(self):
        """When readline() blocks longer than line_timeout, the generator exits."""
        reader = asyncio.StreamReader()
        # Feed one valid line, then stall (no EOF, no more data)
        reader.feed_data(b'{"type": "first"}\n')

        events = []
        async for event in CodexCLI._parse_jsonl(reader, line_timeout=0.05):
            events.append(event)

        # Should have parsed the first event, then timed out on the second readline
        assert len(events) == 1
        assert events[0]["type"] == "first"

    async def test_line_timeout_immediate_stall(self):
        """When no data arrives at all, line_timeout should break immediately."""
        reader = asyncio.StreamReader()
        # No data fed, no EOF — pure stall

        events = []
        async for event in CodexCLI._parse_jsonl(reader, line_timeout=0.05):
            events.append(event)

        assert events == []

    async def test_line_timeout_with_normal_eof(self):
        """When EOF arrives before timeout, should parse all events normally."""
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type": "a"}\n{"type": "b"}\n')
        reader.feed_eof()

        events = [e async for e in CodexCLI._parse_jsonl(reader, line_timeout=5.0)]
        assert len(events) == 2


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------


class TestFindCodexBinary:
    def test_explicit_path(self, tmp_path, monkeypatch):
        fake_bin = tmp_path / "my-codex"
        fake_bin.touch()
        monkeypatch.setattr("src.codex_cli.CODEX_CLI_PATH", str(fake_bin))
        assert CodexCLI._find_codex_binary() == str(fake_bin)

    def test_explicit_path_missing_raises(self, monkeypatch):
        monkeypatch.setattr("src.codex_cli.CODEX_CLI_PATH", "/nonexistent/codex")
        with pytest.raises(RuntimeError, match="non-existent"):
            CodexCLI._find_codex_binary()

    def test_which_fallback(self, monkeypatch):
        monkeypatch.setattr("src.codex_cli.CODEX_CLI_PATH", "codex")
        monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/codex")
        assert CodexCLI._find_codex_binary() == "/usr/local/bin/codex"

    def test_not_found_raises(self, monkeypatch):
        monkeypatch.setattr("src.codex_cli.CODEX_CLI_PATH", "codex")
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(RuntimeError, match="not found"):
            CodexCLI._find_codex_binary()


# ---------------------------------------------------------------------------
# Environment construction
# ---------------------------------------------------------------------------


class TestBuildEnv:
    def test_openai_key_injected(self, codex_cli, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        codex_cli._api_key = "sk-test-key"  # simulate init-time capture
        env = codex_cli._build_env()
        assert env["OPENAI_API_KEY"] == "sk-test-key"

    def test_anthropic_keys_removed(self, codex_cli, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "also-secret")
        env = codex_cli._build_env()
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_API_KEY" not in env

    def test_config_isolation_sets_codex_home(self, codex_cli):
        env = codex_cli._build_env()
        assert "CODEX_HOME" in env
        assert ".codex-server" in env["CODEX_HOME"]

    def test_config_isolation_disabled(self, codex_cli, monkeypatch):
        monkeypatch.setattr("src.codex_cli.CODEX_CONFIG_ISOLATION", False)
        # Remove CODEX_HOME from env if present
        monkeypatch.delenv("CODEX_HOME", raising=False)
        env = codex_cli._build_env()
        # Should not set CODEX_HOME when isolation is off
        assert "CODEX_HOME" not in env


# ---------------------------------------------------------------------------
# run_completion integration (subprocess mocked)
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager wrapper for mocking."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        pass


def _make_mock_process(jsonl_lines: list, returncode: int = 0) -> MagicMock:
    """Create a mock subprocess.Process with stdout feeding JSONL lines."""
    stdout_data = (
        b"\n".join(line.encode() if isinstance(line, str) else line for line in jsonl_lines) + b"\n"
    )

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    proc.stdout = asyncio.StreamReader()
    proc.stdout.feed_data(stdout_data)
    proc.stdout.feed_eof()

    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_eof()

    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    return proc


async def _run_with_mock(codex_cli, jsonl_lines, prompt="Hello", **kwargs):
    """Run codex_cli.run_completion with a mocked subprocess."""
    with patch.object(codex_cli, "_spawn_codex") as mock_spawn:
        mock_proc = _make_mock_process(jsonl_lines)
        mock_spawn.return_value = _AsyncCM(mock_proc)

        chunks = []
        async for chunk in codex_cli.run_completion(prompt, **kwargs):
            chunks.append(chunk)

    return chunks


class TestRunCompletion:
    async def test_basic_flow(self, codex_cli):
        """Full conversation: thread.started → item → turn.completed."""
        jsonl_lines = [
            json.dumps({"type": "thread.started", "thread_id": "t-abc123"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Hello from Codex"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 50, "output_tokens": 25},
                }
            ),
        ]
        chunks = await _run_with_mock(codex_cli, jsonl_lines)

        types = [c["type"] for c in chunks]
        assert "assistant" in types
        assert "result" in types
        assert "codex_session" in types

        session_chunk = next(c for c in chunks if c["type"] == "codex_session")
        assert session_chunk["session_id"] == "t-abc123"

        assistant = next(c for c in chunks if c["type"] == "assistant")
        assert assistant["content"][0]["text"] == "Hello from Codex"

    async def test_resume_passes_thread_id(self, codex_cli):
        """Verify resume parameter is forwarded to _spawn_codex."""
        jsonl_lines = [
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Resumed"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        with patch.object(codex_cli, "_spawn_codex") as mock_spawn:
            mock_proc = _make_mock_process(jsonl_lines)
            mock_spawn.return_value = _AsyncCM(mock_proc)

            chunks = []
            async for chunk in codex_cli.run_completion("Continue", resume="t-prev"):
                chunks.append(chunk)

            mock_spawn.assert_called_once_with(thread_id="t-prev", model=None, system_prompt=None)

    async def test_model_passed_to_spawn(self, codex_cli):
        """Verify model parameter is forwarded to _spawn_codex."""
        jsonl_lines = [
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        with patch.object(codex_cli, "_spawn_codex") as mock_spawn:
            mock_proc = _make_mock_process(jsonl_lines)
            mock_spawn.return_value = _AsyncCM(mock_proc)

            chunks = []
            async for chunk in codex_cli.run_completion("Test", model="o3"):
                chunks.append(chunk)

            mock_spawn.assert_called_once_with(thread_id=None, model="o3", system_prompt=None)

    async def test_system_prompt_passed_to_spawn(self, codex_cli):
        """Verify system_prompt parameter is forwarded to _spawn_codex."""
        jsonl_lines = [
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        with patch.object(codex_cli, "_spawn_codex") as mock_spawn:
            mock_proc = _make_mock_process(jsonl_lines)
            mock_spawn.return_value = _AsyncCM(mock_proc)

            chunks = []
            async for chunk in codex_cli.run_completion(
                "Hello", system_prompt="You are a coding assistant"
            ):
                chunks.append(chunk)

            mock_spawn.assert_called_once_with(
                thread_id=None, model=None, system_prompt="You are a coding assistant"
            )

    async def test_system_prompt_in_command_args(self, codex_cli):
        """Verify system_prompt is passed as -c instructions=... to the Codex binary."""
        jsonl_lines = [
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        captured_cmds = []

        async def fake_exec(*args, **kwargs):
            captured_cmds.append(list(args))
            proc = _make_mock_process(jsonl_lines)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            chunks = []
            async for chunk in codex_cli.run_completion("Hello", system_prompt="Be helpful"):
                chunks.append(chunk)

        # Check that -c instructions=... was in the command
        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "-c" in cmd
        c_idx = cmd.index("-c")
        config_arg = cmd[c_idx + 1]
        assert config_arg.startswith("instructions=")
        assert "Be helpful" in config_arg

    async def test_system_prompt_with_special_chars(self, codex_cli):
        """Verify system_prompt with quotes and newlines is JSON-encoded properly."""
        prompt_with_special = 'You are a "helpful" assistant.\nBe concise.'

        captured_cmds = []

        async def fake_exec(*args, **kwargs):
            captured_cmds.append(list(args))
            jsonl_lines = [
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "OK"},
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
            return _make_mock_process(jsonl_lines)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            chunks = []
            async for chunk in codex_cli.run_completion("Hello", system_prompt=prompt_with_special):
                chunks.append(chunk)

        cmd = captured_cmds[0]
        c_idx = cmd.index("-c")
        config_arg = cmd[c_idx + 1]
        # The value after "instructions=" should be valid JSON
        json_value = config_arg[len("instructions=") :]
        decoded = json.loads(json_value)
        assert decoded == prompt_with_special

    async def test_no_system_prompt_no_config_flag(self, codex_cli):
        """Without system_prompt, -c instructions should NOT appear in command."""
        captured_cmds = []

        async def fake_exec(*args, **kwargs):
            captured_cmds.append(list(args))
            jsonl_lines = [
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "OK"},
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
            return _make_mock_process(jsonl_lines)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            chunks = []
            async for chunk in codex_cli.run_completion("Hello"):
                chunks.append(chunk)

        cmd = captured_cmds[0]
        # Find any -c flag with instructions
        instructions_args = [
            cmd[i + 1]
            for i in range(len(cmd) - 1)
            if cmd[i] == "-c" and cmd[i + 1].startswith("instructions=")
        ]
        assert instructions_args == []

    async def test_preamble_filtered(self, codex_cli):
        """Non-JSON preamble lines should be filtered out."""
        jsonl_lines_raw = [
            "Reading prompt from stdin...",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OK"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        chunks = await _run_with_mock(codex_cli, jsonl_lines_raw)
        assistant_chunks = [c for c in chunks if c["type"] == "assistant"]
        assert len(assistant_chunks) == 1

    async def test_process_error_yields_error_chunk(self, codex_cli):
        """Process failure should yield an error chunk."""
        with patch.object(codex_cli, "_spawn_codex") as mock_spawn:
            mock_spawn.side_effect = OSError("No such file")

            chunks = []
            async for chunk in codex_cli.run_completion("test"):
                chunks.append(chunk)

            assert any(c.get("is_error") for c in chunks)

    async def test_no_thread_started_no_session_chunk(self, codex_cli):
        """If no thread.started event, no codex_session chunk should be yielded."""
        jsonl_lines = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "No thread"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        chunks = await _run_with_mock(codex_cli, jsonl_lines)
        assert not any(c.get("type") == "codex_session" for c in chunks)

    async def test_multiple_items(self, codex_cli):
        """Multiple item events should each produce a chunk."""
        jsonl_lines = [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "Thinking..."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "command": "echo hi"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Done"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        chunks = await _run_with_mock(codex_cli, jsonl_lines)
        assistant_chunks = [c for c in chunks if c["type"] == "assistant"]
        assert len(assistant_chunks) == 3


# ---------------------------------------------------------------------------
# parse_message
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_extracts_from_result(self, codex_cli):
        messages = [
            {"type": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            {"type": "result", "subtype": "success", "result": "Final answer"},
        ]
        assert codex_cli.parse_message(messages) == "Final answer"

    def test_falls_back_to_assistant_content(self, codex_cli):
        messages = [
            {"type": "assistant", "content": [{"type": "text", "text": "Inline text"}]},
            {"type": "result", "subtype": "success", "result": ""},
        ]
        assert codex_cli.parse_message(messages) == "Inline text"

    def test_multiple_text_blocks(self, codex_cli):
        messages = [
            {
                "type": "assistant",
                "content": [
                    {"type": "text", "text": "Part 1"},
                    {"type": "text", "text": "Part 2"},
                ],
            },
        ]
        result = codex_cli.parse_message(messages)
        assert "Part 1" in result
        assert "Part 2" in result

    def test_empty_messages(self, codex_cli):
        assert codex_cli.parse_message([]) is None

    def test_skips_tool_blocks(self, codex_cli):
        messages = [
            {
                "type": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {}},
                    {"type": "text", "text": "Output"},
                ],
            },
        ]
        assert codex_cli.parse_message(messages) == "Output"

    def test_includes_thinking(self, codex_cli):
        messages = [
            {
                "type": "assistant",
                "content": [{"type": "thinking", "thinking": "Deep thought"}],
            },
        ]
        assert codex_cli.parse_message(messages) == "Deep thought"


# ---------------------------------------------------------------------------
# estimate_token_usage
# ---------------------------------------------------------------------------


class TestEstimateTokenUsage:
    def test_basic_estimate(self, codex_cli):
        usage = codex_cli.estimate_token_usage("Hello world", "Response text")
        assert usage["prompt_tokens"] > 0
        assert usage["completion_tokens"] > 0
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    def test_minimum_one_token(self, codex_cli):
        usage = codex_cli.estimate_token_usage("Hi", "Ok")
        assert usage["prompt_tokens"] >= 1
        assert usage["completion_tokens"] >= 1


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerify:
    async def test_verify_success(self, codex_cli):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"codex 0.111.0\n", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            assert await codex_cli.verify() is True

    async def test_verify_not_found(self, codex_cli):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            assert await codex_cli.verify() is False

    async def test_verify_nonzero_exit(self, codex_cli):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"error\n")
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            assert await codex_cli.verify() is False

    async def test_verify_timeout(self, codex_cli):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.side_effect = asyncio.TimeoutError()
            mock_exec.return_value = mock_proc

            assert await codex_cli.verify() is False


# ---------------------------------------------------------------------------
# String-aware brace-counting parser
# ---------------------------------------------------------------------------


class TestExtractTextAndCollab:
    def test_basic_collab_extraction(self):
        text = 'Hello{"id":"1","type":"collab_tool_call","tool":"spawn_agent","prompt":"do stuff","agents_states":{},"status":"completed"}World'
        cleaned, events = _extract_text_and_collab(text)
        assert cleaned == "HelloWorld"
        assert len(events) == 1
        assert events[0]["tool"] == "spawn_agent"

    def test_braces_inside_json_string_values(self):
        """Braces inside JSON string values must not break the parser."""
        collab = json.dumps(
            {
                "id": "item_44",
                "type": "collab_tool_call",
                "tool": "wait",
                "agents_states": {
                    "thread-1": {
                        "status": "completed",
                        "message": "Found {3} files in src/ and fixed }bugs{",
                    }
                },
                "status": "completed",
            }
        )
        text = f"Before{collab}After"
        cleaned, events = _extract_text_and_collab(text)
        assert cleaned == "BeforeAfter"
        assert len(events) == 1
        assert events[0]["tool"] == "wait"
        assert "{3}" in events[0]["agents_states"]["thread-1"]["message"]

    def test_unbalanced_braces_in_string(self):
        """Unbalanced braces inside a string value should be handled."""
        collab = json.dumps(
            {
                "type": "collab_tool_call",
                "tool": "wait",
                "agents_states": {
                    "t1": {"status": "completed", "message": "The { is unclosed here"}
                },
                "status": "completed",
            }
        )
        text = f"Start {collab} End"
        cleaned, events = _extract_text_and_collab(text)
        assert "collab_tool_call" not in cleaned
        assert len(events) == 1

    def test_no_collab_events(self):
        text = "Just regular text with {some braces} inside"
        cleaned, events = _extract_text_and_collab(text)
        assert events == []
        assert "regular text" in cleaned

    def test_multiple_collab_events(self):
        spawn = json.dumps({"type": "collab_tool_call", "tool": "spawn_agent", "prompt": "go"})
        wait = json.dumps(
            {
                "type": "collab_tool_call",
                "tool": "wait",
                "agents_states": {"t1": {"status": "completed", "message": "done"}},
            }
        )
        text = f"Text1\n{spawn}\nText2\n{wait}\nText3"
        cleaned, events = _extract_text_and_collab(text)
        assert len(events) == 2
        assert events[0]["tool"] == "spawn_agent"
        assert events[1]["tool"] == "wait"
        assert "Text1" in cleaned
        assert "Text2" in cleaned
        assert "Text3" in cleaned


class TestNormalizeCollabToolCallEvent:
    """Test top-level collab_tool_call JSONL events handled by normalize_codex_event."""

    def test_spawn_agent_event(self):
        event = {
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "prompt": "Explore the codebase",
            "receiver_thread_ids": ["t1"],
            "agents_states": {},
        }
        result = normalize_codex_event(event)
        assert result is not None
        assert result["type"] == "assistant"
        blocks = result["content"]
        assert any(b["type"] == "tool_use" and b["name"] == "Agent" for b in blocks)

    def test_wait_completed_event(self):
        event = {
            "type": "collab_tool_call",
            "tool": "wait",
            "agents_states": {
                "t1": {"status": "completed", "message": "Found 5 files"},
            },
        }
        result = normalize_codex_event(event)
        assert result is not None
        blocks = result["content"]
        assert any(b["type"] == "tool_result" for b in blocks)

    def test_close_agent_returns_none(self):
        event = {
            "type": "collab_tool_call",
            "tool": "close_agent",
            "agents_states": {},
        }
        result = normalize_codex_event(event)
        # close_agent produces no blocks → None
        assert result is None

    def test_cross_event_spawn_wait_correlation_via_shared_dict(self):
        """spawn_agent and wait in separate normalize_codex_event calls
        share the same agent_tool_ids dict, so tool_use_id correlates."""
        shared_ids: dict = {}

        spawn_event = {
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "prompt": "Do something",
            "receiver_thread_ids": ["thread-A"],
            "agents_states": {},
        }
        r1 = normalize_codex_event(spawn_event, agent_tool_ids=shared_ids)
        assert r1 is not None
        spawn_block = next(b for b in r1["content"] if b["type"] == "tool_use")
        spawn_tool_id = spawn_block["id"]
        assert "thread-A" in shared_ids
        assert shared_ids["thread-A"] == spawn_tool_id

        wait_event = {
            "type": "collab_tool_call",
            "tool": "wait",
            "agents_states": {
                "thread-A": {"status": "completed", "message": "Result here"},
            },
        }
        r2 = normalize_codex_event(wait_event, agent_tool_ids=shared_ids)
        assert r2 is not None
        result_block = next(b for b in r2["content"] if b["type"] == "tool_result")
        assert result_block["tool_use_id"] == spawn_tool_id

    def test_embedded_collab_in_separate_items_correlation(self):
        """spawn_agent and wait embedded in separate agent_message items
        correlate through the shared agent_tool_ids dict."""
        shared_ids: dict = {}

        spawn_json = json.dumps(
            {
                "collab_tool_call": {
                    "tool": "spawn_agent",
                    "prompt": "Research task",
                    "receiver_thread_ids": ["t-99"],
                    "agents_states": {},
                }
            }
        )
        item1_event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": f"Starting\n{spawn_json}"},
        }
        r1 = normalize_codex_event(item1_event, agent_tool_ids=shared_ids)
        assert r1 is not None
        spawn_block = next(b for b in r1["content"] if b["type"] == "tool_use")
        spawn_tool_id = spawn_block["id"]
        assert shared_ids.get("t-99") == spawn_tool_id

        wait_json = json.dumps(
            {
                "collab_tool_call": {
                    "tool": "wait",
                    "agents_states": {
                        "t-99": {"status": "completed", "message": "Done"},
                    },
                }
            }
        )
        item2_event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": f"Finished\n{wait_json}"},
        }
        r2 = normalize_codex_event(item2_event, agent_tool_ids=shared_ids)
        assert r2 is not None
        result_block = next(b for b in r2["content"] if b["type"] == "tool_result")
        assert result_block["tool_use_id"] == spawn_tool_id


class TestConcurrentRunCompletionIsolation:
    """Verify that concurrent run_completion calls on the same CodexCLI
    instance do not cross-wire collab agent_tool_ids (Round 3 regression)."""

    async def test_concurrent_completions_isolated(self, codex_cli):
        """Two concurrent run_completion() calls each track their own
        collab spawn→wait correlation without interference."""
        spawn_a = json.dumps(
            {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "prompt": "Task A",
                "receiver_thread_ids": ["a-thread"],
                "agents_states": {},
            }
        )
        wait_a = json.dumps(
            {
                "type": "collab_tool_call",
                "tool": "wait",
                "agents_states": {
                    "a-thread": {"status": "completed", "message": "Result A"},
                },
            }
        )
        lines_a = [
            json.dumps({"type": "thread.started", "thread_id": "t-a"}),
            spawn_a,
            wait_a,
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]

        spawn_b = json.dumps(
            {
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "prompt": "Task B",
                "receiver_thread_ids": ["b-thread"],
                "agents_states": {},
            }
        )
        wait_b = json.dumps(
            {
                "type": "collab_tool_call",
                "tool": "wait",
                "agents_states": {
                    "b-thread": {"status": "completed", "message": "Result B"},
                },
            }
        )
        lines_b = [
            json.dumps({"type": "thread.started", "thread_id": "t-b"}),
            spawn_b,
            wait_b,
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]

        async def collect(jsonl_lines):
            return await _run_with_mock(codex_cli, jsonl_lines)

        # Run both completions concurrently on the SAME instance
        import asyncio

        chunks_a, chunks_b = await asyncio.gather(
            collect(lines_a),
            collect(lines_b),
        )

        # Extract tool_use and tool_result blocks from each stream
        def extract_blocks(chunks):
            tool_uses = []
            tool_results = []
            for c in chunks:
                if c.get("type") == "assistant":
                    for b in c.get("content", []):
                        if b.get("type") == "tool_use":
                            tool_uses.append(b)
                        elif b.get("type") == "tool_result":
                            tool_results.append(b)
            return tool_uses, tool_results

        uses_a, results_a = extract_blocks(chunks_a)
        uses_b, results_b = extract_blocks(chunks_b)

        # Each stream should have its own tool_use
        assert len(uses_a) >= 1
        assert len(uses_b) >= 1
        assert len(results_a) >= 1
        assert len(results_b) >= 1

        # Correlation: A's tool_result references A's tool_use, not B's
        a_use_ids = {u["id"] for u in uses_a}
        b_use_ids = {u["id"] for u in uses_b}
        a_result_refs = {r["tool_use_id"] for r in results_a}
        b_result_refs = {r["tool_use_id"] for r in results_b}

        # No overlap between A and B tool_use_ids
        assert a_use_ids.isdisjoint(b_use_ids), "Concurrent streams share tool_use ids!"
        # Each result references its own stream's tool_use
        assert a_result_refs.issubset(a_use_ids), f"A results reference wrong ids: {a_result_refs - a_use_ids}"
        assert b_result_refs.issubset(b_use_ids), f"B results reference wrong ids: {b_result_refs - b_use_ids}"
