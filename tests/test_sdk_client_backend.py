"""Tests for ClaudeSDKClient integration methods on ClaudeCodeCLI.

Covers create_client(), run_completion_with_client(), and
_make_ask_user_can_use_tool().
All SDK interactions are mocked — no real subprocess or Anthropic credentials required.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

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
    """create_client() sets a can_use_tool callback (AskUserQuestion intercept)
    and a Skill auto-approve PreToolUse hook.

    AskUserQuestion is handled via can_use_tool — not a PreToolUse hook — because
    the CLI only surfaces it as a callable tool when a permission callback is
    present (issue #131)."""
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

    options = captured_options.get("options")
    assert options is not None
    # AskUserQuestion is intercepted via can_use_tool, no longer a hook.
    assert callable(options.can_use_tool)
    assert options.hooks is not None
    assert "PreToolUse" in options.hooks
    matchers = options.hooks["PreToolUse"]
    by_matcher = {m.matcher: m for m in matchers}
    # AskUserQuestion must NOT be a PreToolUse hook anymore; Skill remains.
    assert "AskUserQuestion" not in by_matcher
    assert "Skill" in by_matcher
    for matcher in by_matcher.values():
        assert len(matcher.hooks) == 1
        assert callable(matcher.hooks[0])


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
# _make_ask_user_can_use_tool (can_use_tool permission callback)
# ---------------------------------------------------------------------------


def _ctx(tool_use_id):
    """Minimal stand-in for the SDK's ToolPermissionContext."""
    return SimpleNamespace(tool_use_id=tool_use_id)


def test_can_use_tool_shadowed_warning_is_filtered():
    """Importing the Claude backend registers an ignore filter for the SDK's
    CanUseToolShadowedWarning.

    The gateway registers can_use_tool alongside bypassPermissions /
    whole-tool allowed_tools on purpose: the shadowing is intended for
    ordinary tools, and AskUserQuestion still reaches the callback (live
    coverage in test_ask_user_question_live.py). Without the filter every
    worker logs a false "can_use_tool will not be invoked" line.

    Runs in a subprocess because pytest's warnings plugin swaps out the
    global filter list per test, hiding module-registered filters.
    """
    import os
    import subprocess
    import sys

    probe = (
        "import warnings\n"
        "import src.backends.claude.client\n"
        "from claude_agent_sdk.types import (\n"
        "    CanUseToolShadowedWarning,\n"
        "    ClaudeAgentOptions,\n"
        "    _warn_if_can_use_tool_shadowed,\n"
        ")\n"
        "assert any(\n"
        "    entry[0] == 'ignore' and entry[2] is CanUseToolShadowedWarning\n"
        "    for entry in warnings.filters\n"
        ")\n"
        "async def cb(tool, tool_input, context): ...\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    _warn_if_can_use_tool_shadowed(\n"
        "        ClaudeAgentOptions(\n"
        "            can_use_tool=cb, permission_mode='bypassPermissions'\n"
        "        )\n"
        "    )\n"
        "assert not caught, caught\n"
    )
    env = {**os.environ, "GATEWAY_SKIP_DOTENV": "1"}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


async def test_can_use_tool_allows_other_tools():
    """Non-AskUserQuestion tools are approved (PermissionResultAllow)."""
    cli = _make_cli()
    session = Session(session_id="sess-other-tool")

    can_use_tool = cli._make_ask_user_can_use_tool(session)

    # can_use_tool receives the tool input dict directly (not wrapped).
    result = await can_use_tool("BashTool", {"command": "ls"}, _ctx("tu_123"))

    assert isinstance(result, PermissionResultAllow)
    # Session fields should NOT be modified
    assert session.pending_tool_call is None
    assert session.input_event is None


async def test_can_use_tool_intercepts_ask_user_question():
    """AskUserQuestion parks pending_tool_call, waits, then denies with the answer."""
    cli = _make_cli()
    session = Session(session_id="sess-ask")

    can_use_tool = cli._make_ask_user_can_use_tool(session)

    input_data = {"question": "Continue?"}

    # Run the callback in a task — it will block on input_event.wait()
    result_holder = []

    async def run_cb():
        result = await can_use_tool("AskUserQuestion", input_data, _ctx("tu_ask_1"))
        result_holder.append(result)

    task = asyncio.create_task(run_cb())

    # Allow the callback to start and park
    await asyncio.sleep(0.05)

    # Verify the session was updated with pending_tool_call. call_id comes from
    # context.tool_use_id; arguments are the input_data dict verbatim.
    assert session.pending_tool_call is not None
    assert session.pending_tool_call["call_id"] == "tu_ask_1"
    assert session.pending_tool_call["name"] == "AskUserQuestion"
    assert session.pending_tool_call["arguments"] == {"question": "Continue?"}

    assert session.input_event is not None

    # Simulate the HTTP layer providing a response
    session.input_response = "Yes, continue"
    session.input_event.set()

    await task

    # Callback should have returned deny + the user's answer as the message
    assert len(result_holder) == 1
    result = result_holder[0]
    assert isinstance(result, PermissionResultDeny)
    assert "Yes, continue" in result.message

    # After completion, input_response and input_event are reset
    assert session.input_response is None
    assert session.input_event is None


async def test_can_use_tool_times_out_when_client_does_not_respond():
    """Callback returns deny with a timeout message when the wait exceeds timeout."""
    cli = _make_cli()
    session = Session(session_id="sess-timeout")

    can_use_tool = cli._make_ask_user_can_use_tool(session)

    # Patch timeout to a very short value so the test completes quickly
    with patch("src.backends.claude.client.ASK_USER_TIMEOUT_SECONDS", 0.05):
        result = await can_use_tool(
            "AskUserQuestion", {"question": "Respond?"}, _ctx("tu_timeout_1")
        )

    assert isinstance(result, PermissionResultDeny)
    assert "timeout" in result.message.lower()

    # Session state should be cleaned up
    assert session.pending_tool_call is None
    assert session.input_event is None
    assert session.input_response is None
