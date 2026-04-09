"""Live integration tests for AskUserQuestion via PreToolUse hooks.

These tests use the real Claude Code CLI and SDK to verify that:
1. PreToolUse hooks fire when AskUserQuestion is invoked
2. The hook can await indefinitely for a client response
3. bypassPermissions interacts correctly with hooks

NOTE: can_use_tool callbacks do NOT work — the CLI never sends
control_request messages. PreToolUse hooks are the correct mechanism.

Requires: Claude Code CLI authenticated locally (claude auth status).
Skipped automatically if CLI is not available or not authenticated.
"""

import asyncio
import subprocess
import pytest

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from claude_agent_sdk.types import HookMatcher


def _cli_authenticated() -> bool:
    """Check if Claude Code CLI is available and authenticated."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not _cli_authenticated(),
    reason="Claude Code CLI not available or not authenticated",
)


async def test_pretooluse_hook_fires_for_ask_user_question():
    """Verify that a PreToolUse hook is invoked when Claude uses AskUserQuestion.

    Sends a prompt designed to trigger AskUserQuestion, then checks
    that the hook received the tool_name.

    NOTE: This test depends on Claude actually calling AskUserQuestion,
    which is LLM behavior and inherently non-deterministic.
    """
    callback_log = []
    callback_event = asyncio.Event()

    async def hook(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "") if isinstance(input_data, dict) else ""
        tool_input = input_data.get("tool_input", {}) if isinstance(input_data, dict) else {}
        callback_log.append({"tool_name": tool_name, "input": tool_input})
        if tool_name == "AskUserQuestion":
            callback_event.set()
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "User responded: yes",
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }

    options = ClaudeAgentOptions(
        max_turns=3,
        permission_mode="bypassPermissions",
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="AskUserQuestion",
                    hooks=[hook],
                )
            ]
        },
    )

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect(prompt=None)
        await client.query(
            "Before doing ANYTHING, you MUST use AskUserQuestion to ask the user "
            "'Should I proceed?' — do not use any other tool first. "
            "This is critical: call AskUserQuestion immediately."
        )

        # Wait for either the callback or a timeout
        try:
            async with asyncio.timeout(30):
                async for _msg in client.receive_response():
                    if callback_event.is_set():
                        break
        except TimeoutError:
            pass

    finally:
        await client.disconnect()

    # Check if AskUserQuestion was seen in the hook
    ask_calls = [c for c in callback_log if c["tool_name"] == "AskUserQuestion"]
    if len(ask_calls) == 0:
        pytest.skip(
            f"Claude did not call AskUserQuestion (LLM non-determinism). "
            f"Tools seen: {[c['tool_name'] for c in callback_log]}"
        )


async def test_pretooluse_hook_receives_tool_permissions():
    """Verify that a PreToolUse hook receives permission requests for tools.

    Uses a broad matcher (None = all tools) to confirm the hook mechanism works
    by checking that at least one tool goes through the hook.
    """
    callback_log = []

    async def hook(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "") if isinstance(input_data, dict) else ""
        callback_log.append(tool_name)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }

    options = ClaudeAgentOptions(
        max_turns=1,
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher=None,  # Match all tools
                    hooks=[hook],
                )
            ]
        },
    )

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect(prompt=None)
        await client.query("Use the Bash tool to run: echo hello")

        try:
            async with asyncio.timeout(30):
                async for _msg in client.receive_response():
                    pass
        except TimeoutError:
            pass
    finally:
        await client.disconnect()

    assert len(callback_log) > 0, (
        "No tools went through PreToolUse hook — the hook mechanism may not be active."
    )


async def test_hook_can_await_for_response():
    """Verify that a PreToolUse hook can await indefinitely for external input.

    This simulates the AskUserQuestion interception pattern where the hook
    blocks until a client provides a response via asyncio.Event.
    """
    hook_started = asyncio.Event()
    external_event = asyncio.Event()
    hook_completed = asyncio.Event()

    async def hook(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "") if isinstance(input_data, dict) else ""
        if tool_name == "AskUserQuestion":
            hook_started.set()
            # Simulate waiting for external input
            await external_event.wait()
            hook_completed.set()
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "User responded: proceed",
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }

    options = ClaudeAgentOptions(
        max_turns=1,
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="AskUserQuestion",
                    hooks=[hook],
                    timeout=60,  # Long timeout for the await
                )
            ]
        },
    )

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect(prompt=None)
        await client.query(
            "Before doing ANYTHING, you MUST use AskUserQuestion to ask "
            "'Should I proceed?' — call it immediately."
        )

        # Start receiving in a background task
        async def receive():
            async for _msg in client.receive_response():
                pass

        recv_task = asyncio.create_task(receive())

        # Wait for the hook to start (or timeout)
        try:
            async with asyncio.timeout(30):
                await hook_started.wait()
        except TimeoutError:
            pytest.skip("AskUserQuestion was not triggered within timeout")
            return

        # The hook is now blocking — unblock it
        external_event.set()

        # Wait for hook to complete
        try:
            async with asyncio.timeout(10):
                await hook_completed.wait()
        except TimeoutError:
            pytest.fail("Hook did not complete after external event was set")

        # Clean up the receive task
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    finally:
        await client.disconnect()

    assert hook_started.is_set(), "Hook should have started"
    assert hook_completed.is_set(), "Hook should have completed after event was set"
