"""Coverage tests for src/backends/codex/client.py — targeting uncovered lines.

Covers:
- Lines 53-58: _extract_text_from_item (agent_message, reasoning, unknown)
- Lines 84, 86: Escape sequence handling in collab JSON parser
- Lines 173-179: send_input tool handling in _collab_to_tool_blocks
- Line 349: item.updated non-todo_list returns None
- Line 424: Working directory existence validation
- Lines 427-429: Temporary workspace fallback when no cwd
- Lines 446, 450, 453: name, owned_by, supported_models properties
- Lines 464-481: resolve() method (slash syntax, exact match, None returns)
- Lines 493-516: build_options() (validation, overrides, tools disabled)
- Lines 520-522: get_auth_provider()
- Line 601, 606: _spawn_codex model and system_prompt cmd building
- Lines 645-649: Process termination fallback (kill after terminate)
- Lines 776-779, 785-792: Timeout detection in run_completion
- Lines 798-800, 802: Stderr capture and return code logging
- Lines 805-806: Outer asyncio.TimeoutError yielding
- Line 853: Subprocess return code checking (via run_completion)
- Lines 913-915: verify() exception handling
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backends.codex.client import (
    CodexCLI,
    _collab_to_tool_blocks,
    _extract_text_and_collab,
    _extract_text_from_item,
    normalize_codex_event,
)
from src.backends.base import BackendConfigError, ResolvedModel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def codex_cli(tmp_path, monkeypatch):
    """Create a CodexCLI with mocked binary discovery."""
    fake_bin = tmp_path / "codex"
    fake_bin.touch()
    fake_bin.chmod(0o755)
    monkeypatch.setattr("src.backends.codex.client.CODEX_CLI_PATH", str(fake_bin))
    monkeypatch.setattr("src.backends.codex.client.CODEX_CONFIG_ISOLATION", False)
    return CodexCLI(timeout=5000, cwd=str(tmp_path))


def _make_mock_proc(
    stdout_lines: list[bytes],
    returncode: int = 0,
    stderr_data: bytes = b"",
    stall_after_stdout: bool = False,
):
    """Build a mock asyncio subprocess with controllable stdout/stderr/returncode."""
    proc = MagicMock()

    # stdin
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    # stdout — async readline that yields lines then b""
    line_iter = iter(stdout_lines)

    async def readline():
        try:
            return next(line_iter)
        except StopIteration:
            return b""

    proc.stdout = MagicMock()
    proc.stdout.readline = readline

    # stderr
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr_data)

    # returncode: None while running, then set after wait
    if stall_after_stdout:
        # Process still alive after stdout drains (simulates timeout)
        proc.returncode = None

        async def wait_forever():
            await asyncio.sleep(100)  # Will be cancelled by wait_for

        proc.wait = wait_forever
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
    else:
        proc.returncode = returncode
        proc.wait = AsyncMock(return_value=returncode)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()

    return proc


# ---------------------------------------------------------------------------
# Lines 53-58: _extract_text_from_item
# ---------------------------------------------------------------------------


class TestExtractTextFromItem:
    def test_agent_message_returns_text(self):
        item = {"type": "agent_message", "text": "Hello from agent"}
        assert _extract_text_from_item(item) == "Hello from agent"

    def test_reasoning_returns_text(self):
        item = {"type": "reasoning", "text": "Thinking step..."}
        assert _extract_text_from_item(item) == "Thinking step..."

    def test_unknown_type_returns_none(self):
        item = {"type": "command_execution", "command": "ls"}
        assert _extract_text_from_item(item) is None

    def test_missing_type_returns_none(self):
        item = {"text": "orphan text"}
        assert _extract_text_from_item(item) is None


# ---------------------------------------------------------------------------
# Lines 84, 86: Escape sequence handling in _extract_text_and_collab
# ---------------------------------------------------------------------------


class TestCollabParserEscapeHandling:
    def test_escaped_backslash_in_json_string(self):
        """Backslash-escape inside JSON string does not break brace counting."""
        obj = {"collab_tool_call": {"tool": "spawn_agent", "path": "C:\\\\Users"}}
        text = f"prefix {json.dumps(obj)} suffix"
        cleaned, collab = _extract_text_and_collab(text)
        assert len(collab) == 1
        assert collab[0]["collab_tool_call"]["tool"] == "spawn_agent"
        assert "prefix" in cleaned
        assert "suffix" in cleaned

    def test_escaped_quote_in_json_string(self):
        """Escaped double-quote inside JSON string value."""
        obj = {"collab_tool_call": {"tool": "wait", "msg": 'said \\"hello\\"'}}
        raw_json = json.dumps(obj)
        text = f"before{raw_json}after"
        cleaned, collab = _extract_text_and_collab(text)
        assert len(collab) == 1
        assert "before" in cleaned
        assert "after" in cleaned

    def test_escaped_brace_in_string_value(self):
        """Braces inside a JSON string value are ignored by depth tracker."""
        obj = {"collab_tool_call": {"tool": "spawn_agent", "message": "Found {3} files"}}
        text = json.dumps(obj)
        cleaned, collab = _extract_text_and_collab(text)
        assert len(collab) == 1
        assert cleaned.strip() == ""


# ---------------------------------------------------------------------------
# Lines 173-179: send_input tool handling in _collab_to_tool_blocks
# ---------------------------------------------------------------------------


class TestCollabSendInput:
    def test_send_input_creates_tool_use(self):
        """send_input with prompt generates a tool_use block."""
        events = [
            {
                "tool": "send_input",
                "prompt": "Run the tests",
                "receiver_thread_ids": ["thread-abc"],
            }
        ]
        agent_ids: dict[str, str] = {}
        blocks = _collab_to_tool_blocks(events, agent_tool_ids=agent_ids)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_use"
        assert blocks[0]["name"] == "Agent"
        assert blocks[0]["input"]["prompt"] == "Run the tests"
        # receiver_thread_id should be tracked
        assert "thread-abc" in agent_ids

    def test_send_input_empty_prompt_no_block(self):
        """send_input with empty prompt produces no tool_use block."""
        events = [
            {
                "tool": "send_input",
                "prompt": "",
                "receiver_thread_ids": ["thread-xyz"],
            }
        ]
        agent_ids: dict[str, str] = {}
        blocks = _collab_to_tool_blocks(events, agent_tool_ids=agent_ids)
        assert len(blocks) == 0
        # ID is still tracked even without a block
        assert "thread-xyz" in agent_ids

    def test_send_input_multiple_receivers(self):
        """send_input maps all receiver_thread_ids to the same tool_use_id."""
        events = [
            {
                "tool": "send_input",
                "prompt": "multi-target",
                "receiver_thread_ids": ["t1", "t2", "t3"],
            }
        ]
        agent_ids: dict[str, str] = {}
        blocks = _collab_to_tool_blocks(events, agent_tool_ids=agent_ids)
        assert len(blocks) == 1
        tool_id = blocks[0]["id"]
        assert agent_ids["t1"] == tool_id
        assert agent_ids["t2"] == tool_id
        assert agent_ids["t3"] == tool_id


# ---------------------------------------------------------------------------
# Line 349: item.updated with non-todo_list type returns None
# ---------------------------------------------------------------------------


class TestItemUpdatedNonTodo:
    def test_item_updated_non_todo_returns_none(self):
        """item.updated events that aren't todo_list should return None."""
        event = {
            "type": "item.updated",
            "item": {"type": "agent_message", "text": "partial update"},
        }
        result = normalize_codex_event(event)
        assert result is None


# ---------------------------------------------------------------------------
# Line 424: Working directory existence validation
# ---------------------------------------------------------------------------


class TestCwdValidation:
    def test_nonexistent_cwd_raises(self, monkeypatch):
        """Passing a nonexistent cwd raises ValueError."""
        monkeypatch.setattr(
            "src.backends.codex.client.CODEX_CLI_PATH", "/usr/bin/true"
        )
        with pytest.raises(ValueError, match="does not exist"):
            CodexCLI(cwd="/nonexistent/path/that/does/not/exist")


# ---------------------------------------------------------------------------
# Lines 427-429: Temporary workspace fallback when no cwd
# ---------------------------------------------------------------------------


class TestTempWorkspaceFallback:
    def test_no_cwd_creates_temp_dir(self, monkeypatch):
        """When no cwd is given, a temporary workspace is created."""
        fake_bin = "/usr/bin/true"
        monkeypatch.setattr(
            "src.backends.codex.client.CODEX_CLI_PATH", fake_bin
        )
        monkeypatch.setattr(
            "src.backends.codex.client.CodexCLI._find_codex_binary",
            staticmethod(lambda: fake_bin),
        )
        cli = CodexCLI(cwd=None)
        assert cli.cwd.exists()
        assert "codex_workspace_" in str(cli.cwd)
        # Cleanup
        import shutil

        shutil.rmtree(str(cli.cwd), ignore_errors=True)


# ---------------------------------------------------------------------------
# Lines 446, 450, 453: name, owned_by, supported_models
# ---------------------------------------------------------------------------


class TestBackendProtocolProperties:
    def test_name(self, codex_cli):
        assert codex_cli.name == "codex"

    def test_owned_by(self, codex_cli):
        assert codex_cli.owned_by == "openai"

    def test_supported_models(self, codex_cli):
        models = codex_cli.supported_models()
        assert isinstance(models, list)
        assert "codex" in models


# ---------------------------------------------------------------------------
# Lines 464-481: resolve() — slash syntax, exact match, None returns
# ---------------------------------------------------------------------------


class TestResolve:
    def test_slash_syntax_codex_o3(self, codex_cli):
        result = codex_cli.resolve("codex/o3")
        assert result is not None
        assert result.backend == "codex"
        assert result.provider_model == "o3"
        assert result.public_model == "codex/o3"

    def test_slash_syntax_empty_submodel(self, codex_cli):
        result = codex_cli.resolve("codex/")
        assert result is not None
        assert result.backend == "codex"
        assert result.provider_model is None

    def test_slash_syntax_unrecognized_prefix_returns_none(self, codex_cli):
        result = codex_cli.resolve("gpt4/turbo")
        assert result is None

    def test_exact_match_codex(self, codex_cli):
        result = codex_cli.resolve("codex")
        assert result is not None
        assert result.backend == "codex"

    def test_unknown_model_returns_none(self, codex_cli):
        result = codex_cli.resolve("claude-3-opus")
        assert result is None


# ---------------------------------------------------------------------------
# Lines 493-516: build_options()
# ---------------------------------------------------------------------------


class TestBuildOptions:
    def test_basic_build_options(self, codex_cli, monkeypatch):
        """build_options produces permission_mode and model."""
        # Avoid importing the real ParameterValidator validation
        monkeypatch.setattr(
            "src.parameter_validator.ParameterValidator.validate_model",
            staticmethod(lambda m: None),
        )
        request = MagicMock()
        request.to_claude_options.return_value = {"model": "o3"}
        request.enable_tools = True
        resolved = ResolvedModel(
            public_model="codex/o3", backend="codex", provider_model="o3"
        )
        opts = codex_cli.build_options(request, resolved)
        assert opts["permission_mode"] == "bypassPermissions"
        assert opts["model"] == "o3"

    def test_build_options_with_overrides(self, codex_cli, monkeypatch):
        """Overrides are merged into options."""
        monkeypatch.setattr(
            "src.parameter_validator.ParameterValidator.validate_model",
            staticmethod(lambda m: None),
        )
        request = MagicMock()
        request.to_claude_options.return_value = {"model": "o3"}
        request.enable_tools = True
        resolved = ResolvedModel(
            public_model="codex/o3", backend="codex", provider_model="o3"
        )
        opts = codex_cli.build_options(
            request, resolved, overrides={"temperature": 0.5}
        )
        assert opts["temperature"] == 0.5

    def test_build_options_tools_disabled_raises(self, codex_cli, monkeypatch):
        """Disabling tools raises BackendConfigError."""
        monkeypatch.setattr(
            "src.parameter_validator.ParameterValidator.validate_model",
            staticmethod(lambda m: None),
        )
        request = MagicMock()
        request.to_claude_options.return_value = {"model": "o3"}
        request.enable_tools = False
        resolved = ResolvedModel(
            public_model="codex/o3", backend="codex", provider_model="o3"
        )
        with pytest.raises(BackendConfigError, match="does not support disabling tools"):
            codex_cli.build_options(request, resolved)


# ---------------------------------------------------------------------------
# Lines 520-522: get_auth_provider()
# ---------------------------------------------------------------------------


class TestGetAuthProvider:
    def test_returns_codex_auth_provider(self, codex_cli):
        provider = codex_cli.get_auth_provider()
        assert provider is not None
        assert provider.name == "codex"


# ---------------------------------------------------------------------------
# Lines 601, 606, 645-649: _spawn_codex with model/system_prompt and kill
# ---------------------------------------------------------------------------


class TestSpawnCodex:
    async def test_spawn_with_model_and_system_prompt(self, codex_cli):
        """_spawn_codex passes --model and -c instructions= to cmd."""
        captured_cmd = []

        async def fake_create(*args, **kwargs):
            captured_cmd.extend(args)
            proc = MagicMock()
            proc.returncode = 0
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            async with codex_cli._spawn_codex(
                model="o3", system_prompt="You are helpful"
            ) as proc:
                pass

        cmd_str = " ".join(str(c) for c in captured_cmd)
        assert "--model" in cmd_str
        assert "o3" in cmd_str
        assert "-c" in cmd_str
        assert "instructions=" in cmd_str

    async def test_spawn_with_resume_thread_id(self, codex_cli):
        """_spawn_codex passes resume <thread_id> when thread_id is given."""
        captured_cmd = []

        async def fake_create(*args, **kwargs):
            captured_cmd.extend(args)
            proc = MagicMock()
            proc.returncode = 0
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            async with codex_cli._spawn_codex(thread_id="thread-abc-123") as proc:
                pass

        cmd_str = " ".join(str(c) for c in captured_cmd)
        assert "resume" in cmd_str
        assert "thread-abc-123" in cmd_str

    async def test_spawn_kills_after_terminate_timeout(self, codex_cli):
        """If process doesn't exit after terminate, kill is called."""
        proc = MagicMock()
        proc.returncode = None  # Still running
        proc.terminate = MagicMock()
        proc.kill = MagicMock()

        # wait() raises TimeoutError (process won't die on terminate)
        async def slow_wait():
            await asyncio.sleep(100)

        proc.wait = slow_wait

        async def fake_create(*args, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            async with codex_cli._spawn_codex() as p:
                # Simulate early exit from context — process still running
                pass

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# Lines 776-792: Timeout detection in run_completion (process stalls)
# ---------------------------------------------------------------------------


class TestRunCompletionTimeout:
    async def test_timeout_when_process_stalls(self, codex_cli):
        """When process is still alive after stdout ends, yield timeout error."""
        events = [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
        ]
        stdout_lines = [(json.dumps(e) + "\n").encode() for e in events]

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()

        line_iter = iter(stdout_lines)

        async def readline():
            try:
                return next(line_iter)
            except StopIteration:
                return b""

        proc.stdout = MagicMock()
        proc.stdout.readline = readline

        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")

        # Process still alive after stdout drains
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()

        # wait() times out (process won't exit naturally)
        async def stall_wait():
            await asyncio.sleep(100)

        proc.wait = stall_wait

        async def fake_create(*args, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            chunks = []
            async for chunk in codex_cli.run_completion("hello"):
                chunks.append(chunk)

        # Should get: codex_session, assistant content, timeout error
        types = [c.get("type") for c in chunks]
        assert "codex_session" in types
        error_chunks = [c for c in chunks if c.get("is_error")]
        assert len(error_chunks) == 1
        assert "timed out" in error_chunks[0]["error_message"]


# ---------------------------------------------------------------------------
# Lines 798-800, 802: Stderr capture and nonzero return code
# ---------------------------------------------------------------------------


class TestStderrAndReturnCode:
    async def test_stderr_captured_and_nonzero_rc_logged(self, codex_cli):
        """Stderr is read and nonzero return code is logged."""
        events = [
            {"type": "thread.started", "thread_id": "t-2"},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ]
        stdout_lines = [(json.dumps(e) + "\n").encode() for e in events]

        proc = _make_mock_proc(
            stdout_lines=stdout_lines,
            returncode=1,
            stderr_data=b"Error: something went wrong\n",
        )

        async def fake_create(*args, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            chunks = []
            async for chunk in codex_cli.run_completion("hello"):
                chunks.append(chunk)

        # Should complete normally (nonzero RC is just logged, not fatal)
        types = [c.get("type") for c in chunks]
        assert "codex_session" in types
        assert "result" in types


# ---------------------------------------------------------------------------
# Lines 805-806: Outer asyncio.TimeoutError handling
# ---------------------------------------------------------------------------


class TestOuterTimeoutError:
    async def test_outer_timeout_error_yields_error_chunk(self, codex_cli):
        """If _spawn_codex raises TimeoutError, yield an error chunk."""

        async def fake_create(*args, **kwargs):
            raise asyncio.TimeoutError("global timeout")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            chunks = []
            async for chunk in codex_cli.run_completion("test prompt"):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0]["is_error"] is True
        assert "timed out" in chunks[0]["error_message"]


# ---------------------------------------------------------------------------
# Line 853: General execution exception yields error chunk
# ---------------------------------------------------------------------------


class TestGeneralExecutionError:
    async def test_general_exception_yields_error_chunk(self, codex_cli):
        """Unexpected exceptions yield error chunks."""

        async def fake_create(*args, **kwargs):
            raise RuntimeError("unexpected subprocess failure")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            chunks = []
            async for chunk in codex_cli.run_completion("test"):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0]["is_error"] is True
        assert "unexpected subprocess failure" in chunks[0]["error_message"]


# ---------------------------------------------------------------------------
# Lines 913-915: verify() exception handling
# ---------------------------------------------------------------------------


class TestParseMessageNonListContent:
    def test_non_list_content_skipped(self, codex_cli):
        """parse_message skips assistant messages with non-list content."""
        messages = [
            {"type": "assistant", "content": "plain string, not a list"},
            {"type": "assistant", "content": [{"type": "text", "text": "actual text"}]},
        ]
        result = codex_cli.parse_message(messages)
        assert result == "actual text"


class TestVerifyExceptions:
    async def test_verify_file_not_found(self, codex_cli):
        """verify() returns False when binary not found."""
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("No such file"),
        ):
            result = await codex_cli.verify()
        assert result is False

    async def test_verify_timeout(self, codex_cli):
        """verify() returns False on timeout."""
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            # Patch wait_for to propagate the timeout
            result = await codex_cli.verify()
        assert result is False

    async def test_verify_general_exception(self, codex_cli):
        """verify() returns False on unexpected exceptions."""
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("permission denied"),
        ):
            result = await codex_cli.verify()
        assert result is False
