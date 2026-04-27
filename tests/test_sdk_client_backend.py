"""Tests for ClaudeSDKClient integration methods on ClaudeCodeCLI.

Covers create_client(), run_completion_with_client(), and _make_ask_user_hook().
All SDK interactions are mocked — no real subprocess or Anthropic credentials required.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.session_manager import Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cli():
    """Create a ClaudeCodeCLI instance with auth mocked out."""
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd="/tmp")


# ---------------------------------------------------------------------------
# create_client
# ---------------------------------------------------------------------------


async def test_create_client_returns_connected_client():
    """create_client() calls ClaudeSDKClient(options=...) then connect(prompt=None)."""
    cli = _make_cli()
    session = Session(session_id="sess-1")

    mock_client_instance = AsyncMock()

    with patch("src.backends.claude.client.ClaudeSDKClient") as MockSDKClient:
        MockSDKClient.return_value = mock_client_instance
        client = await cli.create_client(session)

    # ClaudeSDKClient was constructed with an options object
    MockSDKClient.assert_called_once()
    call_kwargs = MockSDKClient.call_args
    assert "options" in call_kwargs.kwargs or len(call_kwargs.args) > 0

    # connect was called with prompt=None
    mock_client_instance.connect.assert_awaited_once_with(prompt=None)

    # Returns the client
    assert client is mock_client_instance


async def test_create_client_sets_hooks():
    """create_client() sets options.hooks with a PreToolUse entry for AskUserQuestion."""
    cli = _make_cli()
    session = Session(session_id="sess-2")

    captured_options = {}

    with patch("src.backends.claude.client.ClaudeSDKClient") as MockSDKClient:
        mock_instance = AsyncMock()
        MockSDKClient.return_value = mock_instance

        def capture_init(**kwargs):
            captured_options.update(kwargs)
            return mock_instance

        MockSDKClient.side_effect = capture_init
        await cli.create_client(session)

    # The options object passed to ClaudeSDKClient must have hooks set
    options = captured_options.get("options")
    assert options is not None
    assert options.hooks is not None
    assert "PreToolUse" in options.hooks
    matchers = options.hooks["PreToolUse"]
    assert len(matchers) == 1
    assert matchers[0].matcher == "AskUserQuestion"
    assert len(matchers[0].hooks) == 1
    assert callable(matchers[0].hooks[0])


async def test_create_client_uses_gateway_session_id():
    """create_client() reuses session.session_id so the SDK transcript filename matches."""
    cli = _make_cli()
    session = Session(session_id="my-session-id")

    with patch("src.backends.claude.client.ClaudeSDKClient") as MockSDKClient:
        mock_instance = AsyncMock()
        MockSDKClient.return_value = mock_instance

        captured_options = {}

        def capture_init(**kwargs):
            captured_options.update(kwargs)
            return mock_instance

        MockSDKClient.side_effect = capture_init
        await cli.create_client(session)

    options = captured_options.get("options")
    assert options is not None
    # No transcript on disk → use options.session_id (not resume).
    assert options.session_id == "my-session-id"
    assert options.resume is None


async def test_create_client_accepts_custom_base_with_resolved_cwd():
    """create_client() must use _custom_base verbatim — caller is responsible
    for resolving {{WORKING_DIRECTORY}} before passing it.

    Regression: previously the persistent-client path bypassed _custom_base
    and fell back to ``get_system_prompt()`` which returns the unresolved
    template, leaking ``{{WORKING_DIRECTORY}}`` into options.system_prompt.
    """
    cli = _make_cli()
    session = Session(session_id="sess-cwd")

    captured_options = {}

    with patch("src.backends.claude.client.ClaudeSDKClient") as MockSDKClient:
        mock_instance = AsyncMock()

        def capture_init(**kwargs):
            captured_options.update(kwargs)
            return mock_instance

        MockSDKClient.side_effect = capture_init

        # Caller pre-resolves the placeholder to the user workspace path.
        await cli.create_client(
            session,
            cwd="/var/workspaces/alice",
            _custom_base="Primary working directory: /var/workspaces/alice",
        )

    options = captured_options.get("options")
    assert options is not None
    # No unresolved placeholder in the system prompt sent to the SDK.
    assert "{{WORKING_DIRECTORY}}" not in options.system_prompt
    assert "/var/workspaces/alice" in options.system_prompt


async def test_create_client_unset_fallback_does_not_leak_placeholder(monkeypatch):
    """When _custom_base is omitted, fallback to get_system_prompt() must still
    resolve {{WORKING_DIRECTORY}} using the per-call cwd. This guards against
    callers that forget to pre-resolve."""
    cli = _make_cli()
    session = Session(session_id="sess-fallback")

    # Install an unresolved custom prompt as the global runtime override.
    from src import system_prompt as sp

    monkeypatch.setattr(sp, "_runtime_prompt", "cwd={{WORKING_DIRECTORY}}")
    monkeypatch.setattr(sp, "_runtime_prompt_raw", "cwd={{WORKING_DIRECTORY}}")

    captured_options = {}

    with patch("src.backends.claude.client.ClaudeSDKClient") as MockSDKClient:
        mock_instance = AsyncMock()

        def capture_init(**kwargs):
            captured_options.update(kwargs)
            return mock_instance

        MockSDKClient.side_effect = capture_init
        await cli.create_client(session, cwd="/ws/bob")

    options = captured_options.get("options")
    assert options is not None
    assert "{{WORKING_DIRECTORY}}" not in options.system_prompt
    assert "/ws/bob" in options.system_prompt


# ---------------------------------------------------------------------------
# run_completion_with_client
# ---------------------------------------------------------------------------


async def test_run_completion_with_client_yields_messages():
    """run_completion_with_client yields converted messages from client.receive_response()."""
    cli = _make_cli()
    session = Session(session_id="sess-3")

    # Build mock messages that SDK would return (SimpleNamespace mimics SDK objects)
    msg1 = SimpleNamespace(type="assistant", content="Hello")
    msg2 = SimpleNamespace(type="result", subtype="success", result="Done")

    mock_client = AsyncMock()

    async def mock_receive_response():
        yield msg1
        yield msg2

    mock_client.receive_response = mock_receive_response

    messages = []
    async for msg in cli.run_completion_with_client(mock_client, "Hi there", session):
        messages.append(msg)

    # query was called with prompt
    mock_client.query.assert_awaited_once_with("Hi there")

    # Two messages yielded
    assert len(messages) == 2
    assert messages[0]["type"] == "assistant"
    assert messages[1]["type"] == "result"


async def test_run_completion_with_client_error_clears_session_client():
    """On SDK error, session.client is set to None and error dict is yielded."""
    cli = _make_cli()
    session = Session(session_id="sess-err")
    session.client = MagicMock()

    mock_client = AsyncMock()
    mock_client.query.side_effect = RuntimeError("connection lost")

    messages = []
    async for msg in cli.run_completion_with_client(mock_client, "fail", session):
        messages.append(msg)

    assert len(messages) == 1
    assert messages[0]["type"] == "error"
    assert messages[0]["is_error"] is True
    assert "connection lost" in messages[0]["error_message"]
    # session.client cleared
    assert session.client is None


async def test_run_completion_with_client_error_during_receive():
    """An error during receive_response also clears client and yields error."""
    cli = _make_cli()
    session = Session(session_id="sess-recv-err")
    session.client = MagicMock()

    mock_client = AsyncMock()

    async def mock_receive_response():
        yield SimpleNamespace(type="assistant", content="partial")
        raise RuntimeError("stream broken")

    mock_client.receive_response = mock_receive_response

    messages = []
    async for msg in cli.run_completion_with_client(mock_client, "test", session):
        messages.append(msg)

    # First message is the partial assistant, then error
    assert len(messages) == 2
    assert messages[0]["type"] == "assistant"
    assert messages[1]["type"] == "error"
    assert session.client is None


# ---------------------------------------------------------------------------
# _make_ask_user_hook (PreToolUse hook)
# ---------------------------------------------------------------------------


async def test_hook_allows_other_tools():
    """Non-AskUserQuestion tools get an empty dict (allow and proceed)."""
    cli = _make_cli()
    session = Session(session_id="sess-other-tool")

    hook = cli._make_ask_user_hook(session)

    # input_data is a plain dict (not a dataclass) per SDK contract
    input_data = {
        "tool_name": "BashTool",
        "tool_input": {"command": "ls"},
        "tool_use_id": "tu_123",
    }
    result = await hook(input_data, "tu_123", {})

    assert result == {}
    # Session fields should NOT be modified
    assert session.pending_tool_call is None
    assert session.input_event is None


async def test_hook_intercepts_ask_user_question():
    """AskUserQuestion hook sets pending_tool_call, waits, then returns deny+reason."""
    cli = _make_cli()
    session = Session(session_id="sess-ask")

    hook = cli._make_ask_user_hook(session)

    # input_data is a plain dict per SDK contract
    input_data = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"question": "Continue?"},
        "tool_use_id": "tu_ask_1",
    }

    # Run hook in a task — it will block on input_event.wait()
    result_holder = []

    async def run_hook():
        result = await hook(input_data, None, {})
        result_holder.append(result)

    task = asyncio.create_task(run_hook())

    # Allow the hook to start and park
    await asyncio.sleep(0.05)

    # Verify the session was updated with pending_tool_call
    assert session.pending_tool_call is not None
    assert session.pending_tool_call["call_id"] == "tu_ask_1"
    assert session.pending_tool_call["name"] == "AskUserQuestion"
    assert session.pending_tool_call["arguments"] == {"question": "Continue?"}

    # Verify input_event was created
    assert session.input_event is not None

    # Simulate the HTTP layer providing a response
    session.input_response = "Yes, continue"
    session.input_event.set()

    # Wait for hook to complete
    await task

    # Hook should have returned deny + reason (user's answer)
    assert len(result_holder) == 1
    result = result_holder[0]
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Yes, continue" in result["hookSpecificOutput"]["permissionDecisionReason"]

    # After hook completes, input_response and input_event are reset
    assert session.input_response is None
    assert session.input_event is None


async def test_hook_times_out_when_client_does_not_respond():
    """Hook returns deny with timeout message when wait exceeds timeout."""
    cli = _make_cli()
    session = Session(session_id="sess-timeout")

    hook = cli._make_ask_user_hook(session)

    input_data = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"question": "Respond?"},
        "tool_use_id": "tu_timeout_1",
    }

    # Patch timeout to a very short value so the test completes quickly
    with patch("src.backends.claude.client.ASK_USER_TIMEOUT_SECONDS", 0.05):
        result = await hook(input_data, None, {})

    # Should have returned a deny with timeout message
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "timeout" in result["hookSpecificOutput"]["permissionDecisionReason"].lower()

    # Session state should be cleaned up
    assert session.pending_tool_call is None
    assert session.input_event is None
    assert session.input_response is None
