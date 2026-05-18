"""Unit tests for src.sanitizer.openai_bridge.

The bridge swaps in a hand-rolled Anthropic ↔ OpenAI translator in place of
the LiteLLM Anthropic adapter that mis-serializes tool calls and reasoning
content for the GaussO3.2 / vLLM stack. The conversion is purely structural,
so all tests work on plain Python dicts — no HTTP, no SSE encoding.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Iterable, List

from src.sanitizer.openai_bridge import (
    anthropic_request_to_openai_body,
    openai_response_to_anthropic_body,
    openai_stream_to_anthropic_events,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _as_async(items: Iterable[dict]) -> AsyncIterator[dict]:
    for it in items:
        yield it


async def _collect(aiter: AsyncIterator[dict]) -> List[dict]:
    return [e async for e in aiter]


# ---------------------------------------------------------------------------
# Request: Anthropic → OpenAI
# ---------------------------------------------------------------------------


class TestRequestConversion:
    def test_simple_user_text(self):
        body = {
            "model": "GaussO3.2-260402-vllm",
            "max_tokens": 1024,
            "stream": True,
            "messages": [{"role": "user", "content": "흐음"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["model"] == "GaussO3.2-260402-vllm"
        assert out["max_tokens"] == 1024
        assert out["stream"] is True
        assert out["messages"] == [{"role": "user", "content": "흐음"}]
        # Streaming requests must ask for inline usage so message_delta carries
        # real ``output_tokens`` instead of zero.
        assert out["stream_options"]["include_usage"] is True

    def test_system_top_level_becomes_first_message(self):
        body = {
            "model": "m",
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert out["messages"][1] == {"role": "user", "content": "hi"}

    def test_structured_system_blocks_flattened(self):
        body = {
            "model": "m",
            "system": [
                {"type": "text", "text": "Part A. "},
                {"type": "text", "text": "Part B."},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["messages"][0] == {"role": "system", "content": "Part A. Part B."}

    def test_assistant_history_with_tool_use(self):
        body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "what's in /tmp?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "I'll run ls."},
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {"command": "ls /tmp"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": "file1\nfile2",
                        }
                    ],
                },
            ],
        }
        out = anthropic_request_to_openai_body(body)
        # Thinking is dropped; text + tool_use collapse into one assistant
        # message; tool_result becomes a tool message.
        assert out["messages"] == [
            {"role": "user", "content": "what's in /tmp?"},
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [
                    {
                        "id": "tu_1",
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "arguments": json.dumps({"command": "ls /tmp"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "file1\nfile2"},
        ]

    def test_assistant_message_with_only_tool_use_has_null_content(self):
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {},
                        }
                    ],
                }
            ],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["messages"][0]["content"] is None
        assert "tool_calls" in out["messages"][0]

    def test_tool_result_structured_content_flattened(self):
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [
                                {"type": "text", "text": "line1\n"},
                                {"type": "text", "text": "line2"},
                            ],
                        }
                    ],
                }
            ],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["messages"] == [
            {"role": "tool", "tool_call_id": "tu_1", "content": "line1\nline2"}
        ]

    def test_tools_definition_converted(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "Bash",
                    "description": "Run a shell command",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                }
            ],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "Bash",
                    "description": "Run a shell command",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
            }
        ]

    def test_tool_choice_variants(self):
        for ant, openai in (
            ({"type": "auto"}, "auto"),
            ({"type": "any"}, "required"),
            ({"type": "none"}, "none"),
            (
                {"type": "tool", "name": "Bash"},
                {"type": "function", "function": {"name": "Bash"}},
            ),
        ):
            body = {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": "Bash", "description": "", "input_schema": {}}],
                "tool_choice": ant,
            }
            out = anthropic_request_to_openai_body(body)
            assert out["tool_choice"] == openai, f"failed for {ant!r}"

    def test_thinking_field_is_dropped(self):
        """vLLM enables reasoning via chat template, not via a request flag —
        forwarding Anthropic's ``thinking`` would be a no-op at best and an
        unknown-field error at worst."""
        body = {
            "model": "m",
            "thinking": {"type": "enabled", "budget_tokens": 512},
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert "thinking" not in out

    def test_stop_sequences_renamed(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "stop_sequences": ["\n\n", "END"],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["stop"] == ["\n\n", "END"]
        assert "stop_sequences" not in out


# ---------------------------------------------------------------------------
# Streaming response: OpenAI SSE → Anthropic SSE
# ---------------------------------------------------------------------------


class TestStreamConversion:
    async def test_reasoning_then_content_then_tool_call(self):
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": "The user"}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": " said hi."}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "!"}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "Bash", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"command":'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"ls"}'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 30}},
        ]

        out = await _collect(
            openai_stream_to_anthropic_events(_as_async(chunks), model="m")
        )
        types = [e["type"] for e in out]

        # Expected structure: message_start → thinking block → text block →
        # tool_use block → message_delta → message_stop.
        assert types == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

        starts = [e for e in out if e["type"] == "content_block_start"]
        assert [s["content_block"]["type"] for s in starts] == ["thinking", "text", "tool_use"]
        assert [s["index"] for s in starts] == [0, 1, 2]
        # The synthesized tool_use must carry the real upstream id/name.
        assert starts[2]["content_block"]["id"] == "call_1"
        assert starts[2]["content_block"]["name"] == "Bash"

        # finish_reason mapping.
        msg_delta = next(e for e in out if e["type"] == "message_delta")
        assert msg_delta["delta"]["stop_reason"] == "tool_use"
        assert msg_delta["usage"]["output_tokens"] == 30

    async def test_finish_reason_stop_maps_to_end_turn(self):
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        md = next(e for e in out if e["type"] == "message_delta")
        assert md["delta"]["stop_reason"] == "end_turn"

    async def test_finish_reason_length_maps_to_max_tokens(self):
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        md = next(e for e in out if e["type"] == "message_delta")
        assert md["delta"]["stop_reason"] == "max_tokens"

    async def test_empty_content_chunks_do_not_split(self):
        """An empty ``delta.content`` arriving mid-reasoning must not open a
        spurious text block — the bridge treats falsy payload values as
        no-ops, mirroring the upstream-side empty-delta drop sanitizer."""
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "a"}}]},
            {"choices": [{"delta": {"content": ""}}]},
            {"choices": [{"delta": {"reasoning_content": "b"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        starts = [e for e in out if e["type"] == "content_block_start"]
        # Only one thinking block — empty content was a no-op.
        assert [s["content_block"]["type"] for s in starts] == ["thinking"]

    async def test_empty_stream_still_emits_valid_frame(self):
        """A completely empty upstream stream must still produce a structurally
        valid Anthropic response so the downstream SDK can finalize cleanly."""
        out = await _collect(openai_stream_to_anthropic_events(_as_async([]), model="m"))
        types = [e["type"] for e in out]
        assert types == ["message_start", "message_delta", "message_stop"]

    async def test_two_parallel_tool_calls_get_separate_blocks(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "function": {"name": "ToolA", "arguments": "{"},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "}"}}
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_b",
                                    "function": {"name": "ToolB", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert len(starts) == 2
        assert starts[0]["content_block"]["name"] == "ToolA"
        assert starts[0]["content_block"]["id"] == "call_a"
        assert starts[1]["content_block"]["name"] == "ToolB"
        assert starts[1]["content_block"]["id"] == "call_b"


# ---------------------------------------------------------------------------
# Non-streaming response: OpenAI body → Anthropic body
# ---------------------------------------------------------------------------


class TestNonStreamingResponseConversion:
    def test_basic_text_response(self):
        body = {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello!",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        out = openai_response_to_anthropic_body(body)
        assert out["role"] == "assistant"
        assert out["model"] == "m"
        assert out["stop_reason"] == "end_turn"
        assert out["content"] == [{"type": "text", "text": "Hello!"}]
        assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_reasoning_plus_text_plus_tool(self):
        body = {
            "id": "chatcmpl-2",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "Let me think.",
                        "content": "Sure.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "Bash",
                                    "arguments": '{"command":"ls"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        out = openai_response_to_anthropic_body(body)
        assert out["stop_reason"] == "tool_use"
        # Order: thinking → text → tool_use.
        types = [b["type"] for b in out["content"]]
        assert types == ["thinking", "text", "tool_use"]
        tu = out["content"][2]
        assert tu["id"] == "call_1"
        assert tu["name"] == "Bash"
        assert tu["input"] == {"command": "ls"}

    def test_malformed_tool_arguments_fall_back_to_empty_input(self):
        body = {
            "id": "chatcmpl-3",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "X", "arguments": "{not valid"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        out = openai_response_to_anthropic_body(body)
        tu = next(b for b in out["content"] if b["type"] == "tool_use")
        assert tu["input"] == {}
