"""Unit tests for the app-server notification -> canonical chunk mapper.

These exercise :class:`src.backends.appserver.events.TurnMapper` in isolation:
no subprocess, no transport -- just native notifications in, canonical gateway
chunks out. The chunk shapes here are exactly what
``streaming_utils.stream_response_chunks`` renders into the ``/v1/responses``
SSE contract, so this is the vendor-translation contract for issue #173 PR A.
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.backends.appserver.events import TurnMapper


def _drain(
    mapper: TurnMapper, method: str, params: Dict[str, Any]
) -> List[Dict[str, Any]]:
    return list(mapper.map_notification(method, params))


def _mapper() -> TurnMapper:
    return TurnMapper(thread_id="thread-1", turn_id="turn-1")


def test_agent_message_delta_maps_to_text_delta():
    mapper = _mapper()
    chunks = _drain(
        mapper, "item/agentMessage/delta", {"turnId": "turn-1", "delta": "Hello"}
    )
    assert chunks == [
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }
    ]


def test_empty_agent_message_delta_is_dropped():
    mapper = _mapper()
    assert (
        _drain(mapper, "item/agentMessage/delta", {"turnId": "turn-1", "delta": ""})
        == []
    )


def test_reasoning_delta_opens_thinking_block_then_deltas():
    mapper = _mapper()
    first = _drain(
        mapper, "item/reasoning/textDelta", {"turnId": "turn-1", "delta": "thinking..."}
    )
    assert first[0]["event"]["type"] == "content_block_start"
    assert first[0]["event"]["content_block"] == {"type": "thinking"}
    assert first[1]["event"]["delta"] == {
        "type": "thinking_delta",
        "thinking": "thinking...",
    }

    # A second reasoning delta does not reopen the block.
    second = _drain(
        mapper, "item/reasoning/textDelta", {"turnId": "turn-1", "delta": " more"}
    )
    assert [c["event"]["type"] for c in second] == ["content_block_delta"]


def test_reasoning_then_text_closes_the_thinking_block_first():
    mapper = _mapper()
    _drain(mapper, "item/reasoning/textDelta", {"turnId": "turn-1", "delta": "hmm"})
    chunks = _drain(
        mapper, "item/agentMessage/delta", {"turnId": "turn-1", "delta": "answer"}
    )
    # The thinking block is explicitly closed (content_block_stop) so the route
    # flips out of the reasoning item before the visible text delta.
    assert chunks[0]["event"]["type"] == "content_block_stop"
    assert chunks[1]["event"]["delta"] == {"type": "text_delta", "text": "answer"}


def test_command_execution_item_started_maps_to_tool_use():
    mapper = _mapper()
    chunks = _drain(
        mapper,
        "item/started",
        {
            "turnId": "turn-1",
            "item": {
                "type": "commandExecution",
                "id": "item-9",
                "command": "ls -la",
                "cwd": "/w",
            },
        },
    )
    assert chunks == [
        {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "item-9",
                    "name": "commandExecution",
                    "input": {"command": "ls -la", "cwd": "/w"},
                }
            ],
        }
    ]


def test_command_execution_completion_maps_to_tool_result():
    mapper = _mapper()
    chunks = _drain(
        mapper,
        "item/completed",
        {
            "turnId": "turn-1",
            "item": {
                "type": "commandExecution",
                "id": "item-9",
                "status": "completed",
                "exitCode": 0,
                "aggregatedOutput": "total 0\n",
            },
        },
    )
    assert chunks == [
        {
            "type": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "item-9",
                    "content": "total 0\n",
                    "is_error": False,
                }
            ],
        }
    ]


def test_nonzero_exit_code_marks_tool_result_error():
    mapper = _mapper()
    chunks = _drain(
        mapper,
        "item/completed",
        {
            "turnId": "turn-1",
            "item": {
                "type": "commandExecution",
                "id": "x",
                "status": "completed",
                "exitCode": 2,
                "aggregatedOutput": "boom",
            },
        },
    )
    assert chunks[0]["content"][0]["is_error"] is True


def test_turn_completed_emits_completion_with_final_text_and_usage():
    mapper = _mapper()
    # Accumulate the final agent message + usage, then complete.
    assert (
        _drain(
            mapper,
            "item/completed",
            {
                "turnId": "turn-1",
                "item": {
                    "type": "agentMessage",
                    "id": "m1",
                    "phase": "final_answer",
                    "text": "Done.",
                },
            },
        )
        == []
    )
    assert (
        _drain(
            mapper,
            "thread/tokenUsage/updated",
            {
                "turnId": "turn-1",
                "tokenUsage": {"last": {"inputTokens": 10, "outputTokens": 5}},
            },
        )
        == []
    )
    chunks = _drain(
        mapper, "turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}
    )
    assert mapper.finished is True
    assert mapper.succeeded is True
    assert chunks[0]["type"] == "assistant"
    assert chunks[0]["content"] == [{"type": "text", "text": "Done."}]
    result = chunks[1]
    assert result["type"] == "result"
    assert result["subtype"] == "success"
    assert result["result"] == "Done."
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_cached_input_is_not_double_counted_and_reasoning_rolls_into_output():
    mapper = _mapper()
    # inputTokens already INCLUDES cachedInputTokens (cached is a subset), so the
    # prompt total is inputTokens (10), not inputTokens + cached (#174 review §8).
    # totalTokens cross-checks: 10 input + (3 output + 7 reasoning) = 20.
    _drain(
        mapper,
        "thread/tokenUsage/updated",
        {
            "turnId": "turn-1",
            "tokenUsage": {
                "last": {
                    "inputTokens": 10,
                    "cachedInputTokens": 6,
                    "outputTokens": 3,
                    "reasoningOutputTokens": 7,
                    "totalTokens": 20,
                }
            },
        },
    )
    chunks = _drain(
        mapper, "turn/completed", {"turn": {"id": "turn-1", "status": "completed"}}
    )
    usage = chunks[1]["usage"]
    assert usage == {"input_tokens": 10, "output_tokens": 10}
    assert usage["input_tokens"] + usage["output_tokens"] == 20  # == totalTokens


def test_reasoning_summary_delta_is_recognized():
    mapper = _mapper()
    chunks = _drain(
        mapper,
        "item/reasoning/summaryTextDelta",
        {"turnId": "turn-1", "delta": "summary..."},
    )
    assert chunks[0]["event"]["type"] == "content_block_start"
    assert chunks[1]["event"]["delta"] == {
        "type": "thinking_delta",
        "thinking": "summary...",
    }


def test_turn_failed_emits_error_chunk_and_is_not_success():
    mapper = _mapper()
    chunks = _drain(
        mapper,
        "turn/completed",
        {
            "turn": {
                "id": "turn-1",
                "status": "failed",
                "error": {"message": "model exploded"},
            }
        },
    )
    assert chunks == [
        {"type": "error", "is_error": True, "error_message": "model exploded"}
    ]
    assert mapper.finished is True
    assert mapper.succeeded is False


def test_notifications_for_other_turns_are_ignored():
    mapper = _mapper()
    assert (
        _drain(mapper, "item/agentMessage/delta", {"turnId": "turn-2", "delta": "nope"})
        == []
    )


def test_thread_idle_is_a_terminal_fallback():
    mapper = _mapper()
    _drain(
        mapper,
        "item/completed",
        {
            "turnId": "turn-1",
            "item": {"type": "agentMessage", "phase": "final_answer", "text": "hi"},
        },
    )
    chunks = _drain(
        mapper,
        "thread/status/changed",
        {"threadId": "thread-1", "status": {"type": "idle"}},
    )
    assert mapper.finished is True
    assert chunks[-1]["result"] == "hi"


def test_no_more_chunks_after_finished():
    mapper = _mapper()
    _drain(mapper, "turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})
    assert mapper.finished is True
    # Further notifications are inert once the turn is terminal.
    assert (
        _drain(mapper, "item/agentMessage/delta", {"turnId": "turn-1", "delta": "late"})
        == []
    )
