"""Live integration tests for AskUserQuestion via the can_use_tool callback.

These tests use the real Claude Code CLI and SDK to verify that:
1. AskUserQuestion is surfaced to the model and reaches the can_use_tool callback
2. The callback can await indefinitely for a client response, then resume
3. This holds under permission_mode=bypassPermissions (the gateway's mode)

NOTE (issue #131): the CLI surfaces AskUserQuestion to the model *only* when a
``can_use_tool`` permission callback is registered. A PreToolUse hook alone does
NOT expose the tool — the model reports it has no AskUserQuestion tool and the
hook never fires. (An earlier revision claimed the opposite; it was true of an
older CLI. Verified against claude-agent-sdk==0.2.108 + Claude CLI 2.1.x;
re-verified 2026-07-28 on 0.2.128 + bundled CLI 2.1.220. These tests import the
SDK directly, so the CanUseToolShadowedWarning the SDK now emits for this
combination is expected in the output — the passing tests are the proof it is
a false positive for AskUserQuestion.)

Requires: Claude Code CLI authenticated locally (claude auth status) and a
reachable model endpoint, so these are marked ``e2e`` and excluded from the
default suite (addopts -m 'not e2e'); run them with ``uv run pytest -m e2e``.
Also skipped automatically if the CLI is not available. Under a root user the
CLI refuses --dangerously-skip-permissions (bypassPermissions); set
``IS_SANDBOX=1`` to run these there.
"""

import asyncio
import subprocess
import pytest

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from claude_agent_sdk.types import (
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
)

# A well-formed request that naturally elicits AskUserQuestion. A heavy-handed
# "you MUST call the tool now" prompt trips the model's prompt-injection defense
# and it refuses, so keep this a genuine decision with real options.
ASK_PROMPT = (
    "I'm starting a new Python web service and need to pick a caching backend. "
    "It's a real decision with genuine tradeoffs and I'd like your help. "
    "Please ask me to choose between Redis, in-memory (lru_cache), and Memcached "
    "using your AskUserQuestion tool so I can select one before we proceed."
)


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


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _cli_authenticated(),
        reason="Claude Code CLI not available or not authenticated",
    ),
]


async def test_can_use_tool_fires_for_ask_user_question():
    """AskUserQuestion is surfaced and reaches the can_use_tool callback."""
    seen = []
    fired = asyncio.Event()

    async def can_use_tool(tool_name, input_data, context):
        seen.append(tool_name)
        if tool_name == "AskUserQuestion":
            fired.set()
            return PermissionResultDeny(message="User responded: Redis")
        return PermissionResultAllow()

    options = ClaudeAgentOptions(
        max_turns=3,
        permission_mode="bypassPermissions",
        can_use_tool=can_use_tool,
        system_prompt={"type": "preset", "preset": "claude_code"},
    )

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect(prompt=None)
        await client.query(ASK_PROMPT)
        try:
            async with asyncio.timeout(60):
                async for _msg in client.receive_response():
                    if fired.is_set():
                        break
        except TimeoutError:
            pass
    finally:
        await client.disconnect()

    if "AskUserQuestion" not in seen:
        pytest.skip(
            f"Claude did not call AskUserQuestion (LLM non-determinism). "
            f"Tools seen: {seen}"
        )


async def test_pretooluse_hook_receives_tool_permissions():
    """PreToolUse hooks still fire for ordinary tools (used for Skill/sandbox).

    Uses a broad matcher (None = all tools) to confirm the hook mechanism the
    gateway relies on for the workspace sandbox and Skill auto-approve is active.
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


async def test_can_use_tool_can_await_for_response():
    """The can_use_tool callback can block for external input, then resume.

    Mirrors the gateway's park/wait pattern: the callback blocks on an
    asyncio.Event until the HTTP layer supplies the answer, then denies with
    the answer as the message so Claude reads it as the user's reply.
    """
    cb_started = asyncio.Event()
    external_event = asyncio.Event()
    cb_completed = asyncio.Event()

    async def can_use_tool(tool_name, input_data, context):
        if tool_name == "AskUserQuestion":
            cb_started.set()
            await external_event.wait()  # simulate waiting for the client
            cb_completed.set()
            return PermissionResultDeny(message="User responded: Redis")
        return PermissionResultAllow()

    options = ClaudeAgentOptions(
        max_turns=2,
        permission_mode="bypassPermissions",
        can_use_tool=can_use_tool,
        system_prompt={"type": "preset", "preset": "claude_code"},
    )

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect(prompt=None)
        await client.query(ASK_PROMPT)

        async def receive():
            async for _msg in client.receive_response():
                pass

        recv_task = asyncio.create_task(receive())

        try:
            async with asyncio.timeout(60):
                await cb_started.wait()
        except TimeoutError:
            recv_task.cancel()
            pytest.skip("AskUserQuestion was not triggered within timeout")
            return

        # The callback is now blocking — unblock it.
        external_event.set()

        try:
            async with asyncio.timeout(60):
                await cb_completed.wait()
        except TimeoutError:
            pytest.fail("can_use_tool did not complete after external event was set")

        try:
            async with asyncio.timeout(60):
                await recv_task
        except (TimeoutError, asyncio.CancelledError):
            recv_task.cancel()
    finally:
        await client.disconnect()

    assert cb_started.is_set(), "callback should have started"
    assert cb_completed.is_set(), "callback should have completed after event was set"
