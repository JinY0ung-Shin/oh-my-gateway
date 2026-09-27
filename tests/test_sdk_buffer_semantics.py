"""Pin the Claude SDK stdout framing semantics the gateway's #183 fix relies on.

`CLAUDE_MAX_BUFFER_SIZE` / `ClaudeAgentOptions.max_buffer_size` bounds ONE
NDJSON message on the CLI's stdout. These tests run the pinned SDK's real
transport reader (`SubprocessCLITransport.read_messages`) over a fake stdout
stream so three facts stay executable rather than folklore:

1. The #183 fixture — a ~1.2 Mi-character base64 tool result — completes under
   the gateway default and dies under the SDK's own 1 Mi default.
2. An above-limit frame fails deterministically as `CLIJSONDecodeError`, which
   `describe_sdk_stream_error` turns into the actionable `sdk_error` text.
3. The pinned SDK (0.2.160; unchanged since 0.2.128) counts decoded text
   CHARACTERS (`len(str)`), not encoded UTF-8 bytes, even though its message
   says "bytes" (anthropics/claude-agent-sdk-python#1165). If an SDK upgrade
   switches the unit, `test_multibyte_*` fail on purpose: update the docs/error
   text in `_get_max_buffer_size` / README / .env.example along with the pin.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions, CLIJSONDecodeError
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
)

from src.backends.claude.client import describe_sdk_stream_error
from src.constants import GATEWAY_MAX_BUFFER_SIZE_DEFAULT, SDK_DEFAULT_MAX_BUFFER_SIZE

CHUNK = 64 * 1024  # anyio's TextReceiveStream yields ≤64 KiB chunks on asyncio


def _tool_result_line(payload: str) -> str:
    """One CLI stdout frame: a user message carrying a single tool_result."""
    message = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_183",
                    "content": [{"type": "text", "text": payload}],
                }
            ],
        },
    }
    return json.dumps(message, ensure_ascii=False) + "\n"


def _transport(limit: int, stdout_text: str) -> SubprocessCLITransport:
    """A transport whose stdout is *stdout_text*, delivered in 64 KiB chunks."""
    transport = SubprocessCLITransport("hi", ClaudeAgentOptions(max_buffer_size=limit))

    async def chunks():
        for i in range(0, len(stdout_text), CHUNK):
            yield stdout_text[i : i + CHUNK]

    transport._stdout_stream = chunks()
    transport._process = SimpleNamespace(wait=AsyncMock(return_value=0))
    return transport


async def _read_all(transport: SubprocessCLITransport) -> list[dict]:
    return [message async for message in transport.read_messages()]


def _padding(line_overhead: int, target_chars: int, char: str) -> str:
    return char * (target_chars - line_overhead)


# Character count of the frame with an empty payload.
_OVERHEAD = len(_tool_result_line(""))


# ---------------------------------------------------------------------------
# 1. The #183 fixture: ~1.2 Mi characters of base64-looking ASCII
# ---------------------------------------------------------------------------

_INCIDENT_CHARS = 1_200_000  # > the SDK's 1 MiB, well under the gateway default


async def test_incident_fixture_completes_under_gateway_default():
    payload = _padding(_OVERHEAD, _INCIDENT_CHARS, "A")  # base64 alphabet, 1 byte each
    line = _tool_result_line(payload)
    assert len(line) == _INCIDENT_CHARS > SDK_DEFAULT_MAX_BUFFER_SIZE

    messages = await _read_all(_transport(GATEWAY_MAX_BUFFER_SIZE_DEFAULT, line))

    assert len(messages) == 1
    assert messages[0]["message"]["content"][0]["content"][0]["text"] == payload


async def test_incident_fixture_dies_under_sdk_default():
    """What production hit before #184: a fatal reader abort, not a tool error."""
    line = _tool_result_line(_padding(_OVERHEAD, _INCIDENT_CHARS, "A"))

    with pytest.raises(CLIJSONDecodeError) as excinfo:
        await _read_all(_transport(SDK_DEFAULT_MAX_BUFFER_SIZE, line))

    assert "JSON message exceeded maximum buffer size" in excinfo.value.line


# ---------------------------------------------------------------------------
# 2. Above the configured limit → deterministic, actionable sdk_error text
# ---------------------------------------------------------------------------


async def test_above_limit_frame_produces_actionable_error_text():
    limit = 200_000
    line = _tool_result_line(_padding(_OVERHEAD, limit + 2, "A"))
    assert len(line.rstrip("\n")) == limit + 1  # one character over

    with pytest.raises(CLIJSONDecodeError) as excinfo:
        await _read_all(_transport(limit, line))

    text = describe_sdk_stream_error(excinfo.value)
    assert text.startswith("Claude SDK stream aborted")
    assert f"limit {limit}" in text
    assert "CLAUDE_MAX_BUFFER_SIZE" in text
    assert "not UTF-8 bytes" in text


async def test_frame_at_limit_is_accepted():
    """The guard is strictly greater-than: a frame exactly at the limit passes."""
    limit = 200_000
    # The guard runs on the line WITHOUT its trailing newline.
    line = _tool_result_line(_padding(_OVERHEAD, limit + 1, "A"))
    assert len(line.rstrip("\n")) == limit

    messages = await _read_all(_transport(limit, line))

    assert len(messages) == 1


# ---------------------------------------------------------------------------
# 3. Unit pin: the pinned SDK counts str characters, not encoded UTF-8 bytes
# ---------------------------------------------------------------------------

_HANGUL = "한"  # 3 bytes in UTF-8, 1 Python character


async def test_multibyte_frame_under_limit_in_chars_passes_despite_more_bytes():
    """Documented caveat: a frame can carry ~3x the limit in real bytes."""
    limit = 200_000
    payload = _padding(_OVERHEAD, limit, _HANGUL)
    line = _tool_result_line(payload)
    assert len(line.rstrip("\n")) == limit - 1 < limit
    assert len(line.encode("utf-8")) > 2 * limit  # far over the limit in bytes

    messages = await _read_all(_transport(limit, line))

    assert len(messages) == 1
    assert messages[0]["message"]["content"][0]["content"][0]["text"] == payload


async def test_multibyte_frame_over_limit_in_chars_fails():
    limit = 200_000
    line = _tool_result_line(_padding(_OVERHEAD, limit + 2, _HANGUL))
    assert len(line.rstrip("\n")) == limit + 1

    with pytest.raises(CLIJSONDecodeError):
        await _read_all(_transport(limit, line))
