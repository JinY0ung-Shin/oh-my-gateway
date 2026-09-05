"""Response-side tests for the chat/completions -> Responses data-plane bridge.

Covers ``src/codex_responses_bridge`` PR-2 (the response half): the non-streaming
``chat_response_to_responses_body`` and the streaming
``chat_stream_to_responses_events`` state machine, plus the fail-closed seams
(refusal, n>1, malformed shapes) and the namespace re-stamp on returned tool
calls.

File/function names avoid the substring the stale-backend deselector matches, so
these run in the default suite.
"""

from __future__ import annotations

import pytest

from src.codex_responses_bridge import (
    BridgeCapabilityError,
    chat_response_to_responses_body,
    chat_stream_to_responses_events,
)


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# =====================================================================
# Non-streaming: chat_response_to_responses_body
# =====================================================================


def test_text_completion_becomes_message_item():
    out = chat_response_to_responses_body(
        {
            "model": "m",
            "created": 123,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hi"},
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
        response_id="resp_1",
    )
    assert out["id"] == "resp_1"
    assert out["object"] == "response"
    assert out["created_at"] == 123
    assert out["status"] == "completed"
    assert out["model"] == "m"
    assert out["output"] == [
        {
            "id": out["output"][0]["id"],
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "hi", "annotations": []}],
        }
    ]
    assert out["output"][0]["id"].startswith("msg_")
    assert out["usage"] == {
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 8,
        "input_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0},
    }


def test_usage_null_counters_are_coerced():
    out = chat_response_to_responses_body(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
            "usage": {"prompt_tokens": None, "completion_tokens": None},
        },
        response_id="r",
    )
    assert out["usage"]["input_tokens"] == 0
    assert out["usage"]["output_tokens"] == 0
    assert out["usage"]["total_tokens"] == 0


def test_cached_tokens_mapped():
    out = chat_response_to_responses_body(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        },
        response_id="r",
    )
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 4


def test_reasoning_then_message_order():
    out = chat_response_to_responses_body(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "answer", "reasoning_content": "think"},
                }
            ]
        },
        response_id="r",
    )
    assert [i["type"] for i in out["output"]] == ["reasoning", "message"]
    reasoning = out["output"][0]
    assert reasoning["summary"] == [{"type": "summary_text", "text": "think"}]
    assert reasoning["content"] == [{"type": "reasoning_text", "text": "think"}]
    assert reasoning["id"].startswith("rs_")


def test_emit_reasoning_false_drops_reasoning():
    out = chat_response_to_responses_body(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "a", "reasoning_content": "t"},
                }
            ]
        },
        response_id="r",
        emit_reasoning=False,
    )
    assert [i["type"] for i in out["output"]] == ["message"]


def test_tool_call_becomes_function_call_item():
    out = chat_response_to_responses_body(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {"name": "f", "arguments": '{"a":1}'},
                            }
                        ],
                    },
                }
            ]
        },
        response_id="r",
    )
    assert out["output"] == [
        {
            "id": "fc_call_abc",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_abc",
            "name": "f",
            "arguments": '{"a":1}',
        }
    ]


def test_tool_call_missing_id_is_synthesized():
    out = chat_response_to_responses_body(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]
                    },
                }
            ]
        },
        response_id="r",
    )
    item = out["output"][0]
    assert item["call_id"].startswith("call_")
    assert item["id"] == f"fc_{item['call_id']}"


def test_tool_call_non_string_arguments_serialized():
    out = chat_response_to_responses_body(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "f", "arguments": {"a": 1}},
                            }
                        ]
                    },
                }
            ]
        },
        response_id="r",
    )
    assert out["output"][0]["arguments"] == '{"a": 1}'


def test_namespace_restamp_on_returned_tool_call():
    out = chat_response_to_responses_body(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "spawn_agent", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        },
        response_id="r",
        namespace_map={"spawn_agent": "multi_agent_v1"},
    )
    assert out["output"][0]["namespace"] == "multi_agent_v1"


def test_unmapped_tool_call_has_no_namespace():
    out = chat_response_to_responses_body(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "other", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        },
        response_id="r",
        namespace_map={"spawn_agent": "multi_agent_v1"},
    )
    assert "namespace" not in out["output"][0]


def test_length_finish_is_incomplete():
    out = chat_response_to_responses_body(
        {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
        response_id="r",
    )
    assert out["status"] == "incomplete"
    assert out["incomplete_details"] == {"reason": "max_output_tokens"}
    assert out["output"][0]["status"] == "incomplete"


def test_length_as_completed_flag():
    out = chat_response_to_responses_body(
        {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
        response_id="r",
        length_as_completed=True,
    )
    assert out["status"] == "completed"
    assert "incomplete_details" not in out


def test_content_filter_is_incomplete():
    out = chat_response_to_responses_body(
        {"choices": [{"finish_reason": "content_filter", "message": {"content": "x"}}]},
        response_id="r",
    )
    assert out["status"] == "incomplete"
    assert out["incomplete_details"] == {"reason": "content_filter"}


def test_refusal_is_refused():
    with pytest.raises(BridgeCapabilityError):
        chat_response_to_responses_body(
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"refusal": "I can't help"}}
                ]
            },
            response_id="r",
        )


def test_multiple_choices_is_refused():
    with pytest.raises(BridgeCapabilityError):
        chat_response_to_responses_body(
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "a"}},
                    {"finish_reason": "stop", "message": {"content": "b"}},
                ]
            },
            response_id="r",
        )


def test_no_choices_is_refused():
    with pytest.raises(BridgeCapabilityError):
        chat_response_to_responses_body({"choices": []}, response_id="r")


def test_tool_call_missing_name_is_refused():
    with pytest.raises(BridgeCapabilityError):
        chat_response_to_responses_body(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {"id": "c1", "function": {"arguments": "{}"}}
                            ]
                        },
                    }
                ]
            },
            response_id="r",
        )


def test_non_string_content_is_refused():
    with pytest.raises(BridgeCapabilityError):
        chat_response_to_responses_body(
            {"choices": [{"finish_reason": "stop", "message": {"content": [1, 2]}}]},
            response_id="r",
        )


def test_metadata_carried():
    out = chat_response_to_responses_body(
        {"choices": [{"finish_reason": "stop", "message": {"content": "x"}}]},
        response_id="r",
        metadata={"k": "v"},
    )
    assert out["metadata"] == {"k": "v"}


# =====================================================================
# Streaming: chat_stream_to_responses_events
# =====================================================================


def _text_chunk(text, finish=None):
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


def test_stream_preamble_and_text_sequence():
    chunks = [
        {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
        _text_chunk("Hel"),
        _text_chunk("lo"),
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = list(chat_stream_to_responses_events(chunks, response_id="r", model="m"))
    assert _types(events) == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    # sequence numbers strictly increasing from 0
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    # deltas
    deltas = [e for e in events if e["type"] == "response.output_text.delta"]
    assert [d["delta"] for d in deltas] == ["Hel", "lo"]
    assert all(d["logprobs"] == [] for d in deltas)
    # terminal object carries the full message
    completed = events[-1]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == "Hello"


def test_stream_text_done_has_full_text():
    events = list(
        chat_stream_to_responses_events(
            [_text_chunk("ab"), _text_chunk("cd"), _text_chunk("", "stop")],
            response_id="r",
        )
    )
    done = [e for e in events if e["type"] == "response.output_text.done"][0]
    assert done["text"] == "abcd"
    assert done["logprobs"] == []


def test_stream_created_object_is_in_progress():
    events = list(
        chat_stream_to_responses_events([_text_chunk("x", "stop")], response_id="r")
    )
    created = events[0]
    assert created["type"] == "response.created"
    assert created["response"]["status"] == "in_progress"
    assert created["response"]["output"] == []


def test_stream_reasoning_sequence():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "th"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_content": "ink"}, "finish_reason": None}]},
        _text_chunk("answer"),
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events = list(chat_stream_to_responses_events(chunks, response_id="r"))
    types = _types(events)
    assert types[:4] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.reasoning_summary_part.added",
    ]
    assert "response.reasoning_summary_text.delta" in types
    assert "response.reasoning_text.delta" in types
    # reasoning closes (4 done events) before the message opens
    close = [
        "response.reasoning_summary_text.done",
        "response.reasoning_text.done",
        "response.reasoning_summary_part.done",
        "response.output_item.done",
    ]
    for t in close:
        assert t in types
    assert types.index("response.reasoning_summary_text.done") < types.index(
        "response.output_text.delta"
    )
    # reasoning item lands first in the terminal output
    out = events[-1]["response"]["output"]
    assert out[0]["type"] == "reasoning"
    assert out[0]["summary"][0]["text"] == "think"
    assert out[1]["type"] == "message"


def test_stream_tool_call_sequence():
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_x",
                                "function": {"name": "f", "arguments": ""},
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
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"a":'}}]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = list(
        chat_stream_to_responses_events(
            chunks, response_id="r", namespace_map={"f": "ns1"}
        )
    )
    types = _types(events)
    assert "response.output_item.added" in types
    added = [e for e in events if e["type"] == "response.output_item.added"][0]
    assert added["item"]["type"] == "function_call"
    assert added["item"]["id"] == "fc_call_x"
    assert added["item"]["call_id"] == "call_x"
    assert added["item"]["namespace"] == "ns1"
    arg_deltas = [
        e for e in events if e["type"] == "response.function_call_arguments.delta"
    ]
    assert "".join(d["delta"] for d in arg_deltas) == '{"a":1}'
    done = [e for e in events if e["type"] == "response.function_call_arguments.done"][
        0
    ]
    assert done["arguments"] == '{"a":1}'
    assert done["name"] == "f"
    # terminal output carries the namespace-stamped call
    call = events[-1]["response"]["output"][0]
    assert call["type"] == "function_call"
    assert call["namespace"] == "ns1"
    assert call["arguments"] == '{"a":1}'


def test_stream_parallel_tool_calls():
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c0",
                                "function": {"name": "a", "arguments": "{}"},
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
                            {
                                "index": 1,
                                "id": "c1",
                                "function": {"name": "b", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = list(chat_stream_to_responses_events(chunks, response_id="r"))
    out = events[-1]["response"]["output"]
    assert [i["call_id"] for i in out] == ["c0", "c1"]


def test_stream_tool_id_reuse_splits_calls():
    # Same index but a new id => a second call reusing the slot; args must not merge.
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c0",
                                "function": {"name": "a", "arguments": '{"x":1}'},
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
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "b", "arguments": '{"y":2}'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = list(chat_stream_to_responses_events(chunks, response_id="r"))
    out = events[-1]["response"]["output"]
    assert [i["call_id"] for i in out] == ["c0", "c1"]
    assert out[0]["arguments"] == '{"x":1}'
    assert out[1]["arguments"] == '{"y":2}'


def test_stream_text_then_tool_closes_text_first():
    chunks = [
        _text_chunk("note"),
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c0",
                                "function": {"name": "a", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    types = _types(list(chat_stream_to_responses_events(chunks, response_id="r")))
    # message closes before the tool's item opens
    assert types.index("response.output_item.done") < types.index(
        "response.function_call_arguments.done"
    )


def test_stream_length_is_incomplete_terminal():
    events = list(
        chat_stream_to_responses_events(
            [
                _text_chunk("partial"),
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
            ],
            response_id="r",
        )
    )
    terminal = events[-1]
    assert terminal["type"] == "response.incomplete"
    assert terminal["response"]["incomplete_details"] == {"reason": "max_output_tokens"}
    # the open message item inherits the incomplete status
    msg = terminal["response"]["output"][0]
    assert msg["status"] == "incomplete"


def test_stream_usage_on_trailing_chunk():
    chunks = [
        _text_chunk("x", "stop"),
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
    ]
    events = list(chat_stream_to_responses_events(chunks, response_id="r"))
    usage = events[-1]["response"]["usage"]
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 2
    assert usage["total_tokens"] == 9


def test_stream_error_chunk_is_failed_terminal():
    chunks = [
        _text_chunk("partial"),
        {"error": {"code": "server_error", "message": "boom"}},
    ]
    events = list(chat_stream_to_responses_events(chunks, response_id="r"))
    terminal = events[-1]
    assert terminal["type"] == "response.failed"
    assert terminal["response"]["status"] == "failed"
    assert terminal["response"]["error"] == {"code": "server_error", "message": "boom"}
    # no response.completed after a failure
    assert "response.completed" not in _types(events)


def test_stream_refusal_delta_is_refused():
    with pytest.raises(BridgeCapabilityError):
        list(
            chat_stream_to_responses_events(
                [{"choices": [{"delta": {"refusal": "no"}, "finish_reason": None}]}],
                response_id="r",
            )
        )


def test_stream_multiple_choices_is_refused():
    with pytest.raises(BridgeCapabilityError):
        list(
            chat_stream_to_responses_events(
                [
                    {
                        "choices": [
                            {"delta": {"content": "a"}},
                            {"delta": {"content": "b"}},
                        ]
                    }
                ],
                response_id="r",
            )
        )


def test_stream_non_string_arguments_fragment_is_refused():
    with pytest.raises(BridgeCapabilityError):
        list(
            chat_stream_to_responses_events(
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "c0",
                                            "function": {
                                                "name": "a",
                                                "arguments": {"x": 1},
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                ],
                response_id="r",
            )
        )


def test_stream_disconnect_emits_no_terminal():
    import itertools

    gen = chat_stream_to_responses_events(
        [_text_chunk("a"), _text_chunk("b"), _text_chunk("", "stop")], response_id="r"
    )
    first_two = list(itertools.islice(gen, 2))
    assert _types(first_two) == ["response.created", "response.in_progress"]
    gen.close()  # simulate client disconnect before exhaustion
    # nothing more was produced; no terminal completed event was forced


def test_stream_emit_reasoning_false_drops_reasoning():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "think"}, "finish_reason": None}]},
        _text_chunk("a", "stop"),
    ]
    events = list(
        chat_stream_to_responses_events(chunks, response_id="r", emit_reasoning=False)
    )
    assert not any("reasoning" in t for t in _types(events))
