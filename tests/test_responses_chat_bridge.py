"""Request-side tests for the Responses -> chat/completions data-plane bridge.

Covers ``src/codex_responses_bridge`` PR-1 (Responses request -> chat request):
the ported translation corpus (adapted from the UniBridge reference) plus the
gateway's fail-closed hardening -- an unsupported/unknown tool type or a
flattened-name collision is REFUSED, never silently dropped/last-wins.

File/function names avoid the substring the stale-backend deselector matches, so
these run in the default suite.
"""

from __future__ import annotations

import re

import pytest

from src.codex_responses_bridge import (
    BridgeCapabilityError,
    clamp_reasoning_effort,
    namespace_map_from_tools,
    responses_request_to_chat_body,
)
from src.codex_responses_bridge.system_norm import normalize_system_messages


@pytest.fixture(autouse=True)
def _clean_bridge_env(monkeypatch: pytest.MonkeyPatch):
    # Deterministic defaults regardless of the ambient environment.
    for key in (
        "CODEX_BRIDGE_FLATTEN_NAMESPACE_TOOLS",
        "CODEX_BRIDGE_REASONING_EFFORT_LEVELS",
        "CODEX_BRIDGE_MID_SYSTEM_POLICY",
        "CODEX_BRIDGE_MID_SYSTEM_MODEL_PATTERN",
    ):
        monkeypatch.delenv(key, raising=False)


# -- basic request translation ----------------------------------------------


def test_string_input_becomes_user_message():
    out = responses_request_to_chat_body({"model": "m", "input": "hello"})
    assert out["model"] == "m"
    assert out["messages"] == [{"role": "user", "content": "hello"}]


def test_instructions_prepended_as_system():
    out = responses_request_to_chat_body({"input": "hi", "instructions": "be nice"})
    assert out["messages"][0] == {"role": "system", "content": "be nice"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_prior_messages_prepended_then_followup_instructions():
    prior = [{"role": "system", "content": "orig"}, {"role": "user", "content": "q"}]
    out = responses_request_to_chat_body(
        {"input": "again", "instructions": "new"}, prior_messages=prior
    )
    assert out["messages"][:2] == prior
    assert out["messages"][2] == {"role": "system", "content": "new"}
    assert out["messages"][3] == {"role": "user", "content": "again"}


def test_developer_role_maps_to_system():
    out = responses_request_to_chat_body(
        {"input": [{"type": "message", "role": "developer", "content": "sys"}]}
    )
    assert out["messages"] == [{"role": "system", "content": "sys"}]


def test_sampling_and_token_fields_passthrough():
    out = responses_request_to_chat_body(
        {
            "input": "x",
            "max_output_tokens": 256,
            "temperature": 0.3,
            "top_p": 0.9,
            "user": "u1",
        }
    )
    assert out["max_completion_tokens"] == 256
    assert out["temperature"] == 0.3
    assert out["top_p"] == 0.9
    assert out["user"] == "u1"


def test_safety_identifier_used_as_user_when_user_absent():
    out = responses_request_to_chat_body({"input": "x", "safety_identifier": "sid"})
    assert out["user"] == "sid"


def test_stream_forces_include_usage():
    out = responses_request_to_chat_body({"input": "x", "stream": True})
    assert out["stream"] is True
    assert out["stream_options"]["include_usage"] is True


def test_text_json_schema_becomes_response_format():
    out = responses_request_to_chat_body(
        {
            "input": "x",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "S",
                    "schema": {"type": "object"},
                    "strict": True,
                }
            },
        }
    )
    assert out["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "S", "schema": {"type": "object"}, "strict": True},
    }


# -- tool-call coalescing ----------------------------------------------------


def test_parallel_function_calls_coalesce_into_single_assistant_message():
    out = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "a",
                    "arguments": "{}",
                },
                {
                    "type": "function_call",
                    "call_id": "c2",
                    "name": "b",
                    "arguments": "{}",
                },
            ]
        }
    )
    assert len(out["messages"]) == 1
    tc = out["messages"][0]["tool_calls"]
    assert [c["id"] for c in tc] == ["c1", "c2"]


def test_assistant_text_then_tool_calls_merge_into_one_message():
    out = responses_request_to_chat_body(
        {
            "input": [
                {"type": "message", "role": "assistant", "content": "thinking"},
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "a",
                    "arguments": "{}",
                },
            ]
        }
    )
    assert len(out["messages"]) == 1
    assert out["messages"][0]["content"] == "thinking"
    assert out["messages"][0]["tool_calls"][0]["id"] == "c1"


def test_tool_result_starts_a_fresh_assistant_block():
    out = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "a",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
                {
                    "type": "function_call",
                    "call_id": "c2",
                    "name": "b",
                    "arguments": "{}",
                },
            ]
        }
    )
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["assistant", "tool", "assistant"]
    assert out["messages"][0]["tool_calls"][0]["id"] == "c1"
    assert out["messages"][2]["tool_calls"][0]["id"] == "c2"


def test_function_call_output_text_only_array_collapses_to_string():
    out = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": [
                        {"type": "output_text", "text": "part-a "},
                        {"type": "output_text", "text": "part-b"},
                    ],
                }
            ]
        }
    )
    tool_msg = out["messages"][0]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"] == "part-a part-b"


def test_function_call_output_with_image_part_is_refused():
    # Codex view_image / image-gen tools return an input_image data URL. A chat
    # role:"tool" message is text-only, so refuse rather than drop the image
    # behind a placeholder -- the model would otherwise act on an emptier tool
    # result than the tool produced (round-3 finding 1).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": [
                            {"type": "output_text", "text": "result"},
                            {"type": "input_image", "image_url": "data:..."},
                        ],
                    }
                ]
            }
        )


def test_function_call_output_with_unknown_part_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": [{"type": "some_new_part", "data": {}}],
                    }
                ]
            }
        )


def test_function_call_output_without_call_id_is_refused():
    # A tool result with no correlation id has no assistant tool call to attach
    # to; refuse rather than emit an empty tool_call_id (round-3 finding 2).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": [{"type": "function_call_output", "output": "ok"}]}
        )


def test_function_call_without_name_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": [{"type": "function_call", "call_id": "c1", "arguments": "{}"}]}
        )


def test_function_call_without_call_id_is_refused():
    # Missing call_id/id -> no correlation id; refuse rather than synthesize one
    # that no tool result can match (round-3 finding 2).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": [{"type": "function_call", "name": "a", "arguments": "{}"}]}
        )


def test_function_call_item_id_is_not_used_as_call_id():
    # The response item 'id' is a distinct identity from the tool 'call_id'; an
    # item with only 'id' must be refused, not correlated on 'id' (round-4
    # finding 3) -- a later result keyed by the real call_id could not match it.
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": [
                    {
                        "type": "function_call",
                        "id": "fc_item_1",
                        "name": "f",
                        "arguments": "{}",
                    }
                ]
            }
        )


def test_function_call_correlates_on_call_id_not_item_id():
    # When both are present the emitted chat tool-call id is the call_id.
    out = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "id": "fc_item_1",
                    "call_id": "call_real",
                    "name": "f",
                    "arguments": "{}",
                }
            ]
        }
    )
    assert out["messages"][0]["tool_calls"][0]["id"] == "call_real"


def test_function_call_non_string_arguments_are_refused():
    # arguments is a JSON string; None/{} must not be coerced into a different
    # (zero-argument) call (round-4 finding 4).
    for bad in (None, {}, {"a": 1}, 5):
        with pytest.raises(BridgeCapabilityError):
            responses_request_to_chat_body(
                {
                    "input": [
                        {
                            "type": "function_call",
                            "call_id": "c1",
                            "name": "f",
                            "arguments": bad,
                        }
                    ]
                }
            )


def test_function_call_string_arguments_unchanged():
    args = '{"path": "/x", "n": 3}'
    out = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "f",
                    "arguments": args,
                }
            ]
        }
    )
    assert out["messages"][0]["tool_calls"][0]["function"]["arguments"] == args


def test_function_call_output_scalar_shape_is_refused():
    # An arbitrary dict/number output must not become JSON text (round-4
    # finding 4); only a string or the handled content-part array is accepted.
    for bad in ({"k": "v"}, 5, True):
        with pytest.raises(BridgeCapabilityError):
            responses_request_to_chat_body(
                {
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "c1",
                            "output": bad,
                        }
                    ]
                }
            )


# -- multimodal input --------------------------------------------------------


def test_input_image_by_url_becomes_multimodal():
    out = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "look"},
                        {"type": "input_image", "image_url": "http://x/y.png"},
                    ],
                }
            ]
        }
    )
    content = out["messages"][0]["content"]
    assert {"type": "text", "text": "look"} in content
    assert {"type": "image_url", "image_url": {"url": "http://x/y.png"}} in content


def test_input_image_file_id_only_is_refused():
    # No usable image url -> refuse rather than silently running a text-only
    # request while the caller believes an image was supplied (#173 image gate).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "hi"},
                            {"type": "input_image", "file_id": "f1"},
                        ],
                    }
                ]
            }
        )


# -- unknown input items fail closed; ignorable ones pass --------------------


def test_unknown_input_item_type_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": [{"type": "item_reference", "id": "abc"}]}
        )


def test_reasoning_input_item_is_refused():
    # A reasoning item participates in Codex continuation state (store:false +
    # include:["reasoning.encrypted_content"]); chat/completions cannot carry it,
    # so refuse rather than silently strip it (round-4 finding 1).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": [
                    {"type": "reasoning", "summary": "thinking"},
                    {"type": "message", "role": "user", "content": "hi"},
                ]
            }
        )


def test_message_item_without_role_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": [{"type": "message", "content": "no role"}]}
        )


# -- tool_choice fails closed on unrepresentable forms ----------------------


def test_unsupported_tool_choice_string_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [{"type": "function", "name": "f"}],
                "tool_choice": "sometimes",
            }
        )


def test_unsupported_tool_choice_dict_form_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [{"type": "function", "name": "f"}],
                "tool_choice": {"type": "mcp", "server": "s"},
            }
        )


def test_function_tool_choice_without_name_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [{"type": "function", "name": "f"}],
                "tool_choice": {"type": "function"},
            }
        )


def test_unsupported_tool_choice_refused_even_with_empty_tools():
    # An unrepresentable constraint must not slip through just because the tool
    # set is empty and tool_choice would be dropped anyway.
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "tools": [], "tool_choice": "sometimes"}
        )


def test_function_tool_choice_by_name_is_forwarded():
    out = responses_request_to_chat_body(
        {
            "input": "x",
            "tools": [{"type": "function", "name": "f"}],
            "tool_choice": {"type": "function", "name": "f"},
        }
    )
    assert out["tool_choice"] == {"type": "function", "function": {"name": "f"}}


def test_empty_tools_required_choice_is_refused():
    # 'required' demands a tool call; zero tools cannot satisfy it, so refuse
    # rather than relax it into an unconstrained completion (round-5 finding 2).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "tools": [], "tool_choice": "required"}
        )


def test_empty_tools_named_choice_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [],
                "tool_choice": {"type": "function", "name": "f"},
            }
        )


def test_empty_tools_none_choice_is_omitted():
    # 'none' is equivalent to no constraint when there are no tools -> omitted.
    out = responses_request_to_chat_body(
        {"input": "x", "tools": [], "tool_choice": "none"}
    )
    assert "tool_choice" not in out
    assert "tools" not in out


def test_named_choice_absent_from_translated_tool_set_is_refused():
    # A named choice must reference a tool that survived translation; a missing
    # name is an impossible constraint, not a forwardable one (round-5 finding 2).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [{"type": "function", "name": "g"}],
                "tool_choice": {"type": "function", "name": "f"},
            }
        )


# -- tools translation -------------------------------------------------------


def test_function_tool_and_tool_choice_reshape():
    out = responses_request_to_chat_body(
        {
            "input": "x",
            "tools": [
                {"type": "function", "name": "f", "description": "d", "parameters": {}}
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
    )
    assert out["tools"] == [
        {
            "type": "function",
            "function": {"name": "f", "description": "d", "parameters": {}},
        }
    ]
    assert out["tool_choice"] == "auto"
    assert out["parallel_tool_calls"] is True


def test_function_tool_strict_preserved():
    out = responses_request_to_chat_body(
        {"input": "x", "tools": [{"type": "function", "name": "f", "strict": True}]}
    )
    assert out["tools"][0]["function"]["strict"] is True


def test_compaction_empty_tools_drops_tool_params():
    # Codex sends tools:[] + tool_choice:auto + parallel_tool_calls on tool-less
    # turns; both tool-dependent params must be dropped for vLLM/SGLang.
    out = responses_request_to_chat_body(
        {
            "input": "x",
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
    )
    assert "tools" not in out
    assert "tool_choice" not in out
    assert "parallel_tool_calls" not in out


# -- namespace flatten + re-stamp map ---------------------------------------


def _namespace_body() -> dict:
    return {
        "input": "x",
        "tools": [
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [
                    {"type": "function", "name": "spawn_agent", "parameters": {}},
                    {"type": "function", "name": "close_agent", "parameters": {}},
                ],
            }
        ],
    }


def test_namespace_inner_functions_flattened_to_top_level():
    out = responses_request_to_chat_body(_namespace_body())
    names = [t["function"]["name"] for t in out["tools"]]
    assert names == ["spawn_agent", "close_agent"]


def test_namespace_map_maps_inner_functions_to_namespace():
    m = namespace_map_from_tools(_namespace_body()["tools"])
    assert m == {"spawn_agent": "multi_agent_v1", "close_agent": "multi_agent_v1"}


def test_namespace_map_empty_without_a_namespace():
    assert namespace_map_from_tools([{"type": "function", "name": "f"}]) == {}


# -- fail-closed hardening (our contract, stricter than the reference) -------


def test_unsupported_tool_type_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "tools": [{"type": "web_search"}]}
        )


def test_unknown_tool_type_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "tools": [{"type": "file_search", "name": "fs"}]}
        )


def test_namespace_refused_when_flattening_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_BRIDGE_FLATTEN_NAMESPACE_TOOLS", "false")
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(_namespace_body())


def test_flattened_name_collision_across_namespaces_is_refused():
    body = {
        "input": "x",
        "tools": [
            {
                "type": "namespace",
                "name": "ns1",
                "tools": [{"type": "function", "name": "dup"}],
            },
            {
                "type": "namespace",
                "name": "ns2",
                "tools": [{"type": "function", "name": "dup"}],
            },
        ],
    }
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(body)


def test_flattened_name_collision_with_top_level_function_is_refused():
    body = {
        "input": "x",
        "tools": [
            {"type": "function", "name": "dup"},
            {
                "type": "namespace",
                "name": "ns",
                "tools": [{"type": "function", "name": "dup"}],
            },
        ],
    }
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(body)


def test_function_tool_without_name_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "tools": [{"type": "function", "parameters": {}}]}
        )


def test_non_list_tools_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "tools": {"type": "function"}})


# -- nested tool-object contract fails closed (round-7 finding) ---------------


def test_namespace_custom_freeform_tool_is_refused():
    # A custom/freeform tool has its own invocation grammar; it must never be
    # coerced into a JSON function with empty parameters.
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "ns",
                        "tools": [
                            {
                                "type": "custom",
                                "name": "apply_patch",
                                "description": "Apply a patch",
                                "format": {"type": "grammar", "definition": "..."},
                            }
                        ],
                    }
                ],
            }
        )


def test_unknown_namespace_inner_tool_type_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "ns",
                        "tools": [{"type": "widget", "name": "w"}],
                    }
                ],
            }
        )


def test_top_level_function_defer_loading_is_refused():
    # Lazy/deferred exposure has no equivalent here; emitting the tool eagerly
    # would broaden the model-visible tool set -> refuse.
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [{"type": "function", "name": "f", "defer_loading": True}],
            }
        )


def test_namespaced_function_defer_loading_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "ns",
                        "tools": [
                            {
                                "type": "function",
                                "name": "f",
                                "defer_loading": True,
                            }
                        ],
                    }
                ],
            }
        )


def test_function_defer_loading_false_is_accepted():
    out = responses_request_to_chat_body(
        {
            "input": "x",
            "tools": [{"type": "function", "name": "f", "defer_loading": False}],
        }
    )
    assert out["tools"][0]["function"]["name"] == "f"


def test_unknown_function_tool_field_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "tools": [{"type": "function", "name": "f", "mystery": 1}],
            }
        )


def test_namespace_map_refuses_collision_consistently():
    tools = [
        {
            "type": "namespace",
            "name": "a",
            "tools": [{"type": "function", "name": "dup"}],
        },
        {
            "type": "namespace",
            "name": "b",
            "tools": [{"type": "function", "name": "dup"}],
        },
    ]
    with pytest.raises(BridgeCapabilityError):
        namespace_map_from_tools(tools)


# -- role / content semantics fail closed -----------------------------------


def test_unknown_role_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": [{"type": "message", "role": "root", "content": "x"}]}
        )


def test_unknown_content_part_type_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_file", "file_id": "f"}],
                    }
                ]
            }
        )


def test_non_object_content_part_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": [{"type": "message", "role": "user", "content": ["bare string"]}]}
        )


def test_non_list_non_string_content_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": [{"type": "message", "role": "user", "content": 123}]}
        )


# -- structured-output constraint fails closed ------------------------------


def test_unsupported_text_format_type_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "text": {"format": {"type": "grammar", "grammar": "..."}}}
        )


def test_text_format_type_text_is_omitted():
    out = responses_request_to_chat_body(
        {"input": "x", "text": {"format": {"type": "text"}}}
    )
    assert "response_format" not in out


def test_text_format_json_object_is_kept():
    out = responses_request_to_chat_body(
        {"input": "x", "text": {"format": {"type": "json_object"}}}
    )
    assert out["response_format"] == {"type": "json_object"}


def test_absent_text_format_omits_response_format():
    out = responses_request_to_chat_body(
        {"input": "x", "text": {"verbosity": "medium"}}
    )
    assert "response_format" not in out


# -- inbound field contract fails closed (round-4 finding 2) -----------------


def test_unknown_top_level_field_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "frobnicate": True})


def test_neutral_consumed_fields_translate_and_consume():
    # Consumed controls at their proven-neutral value: translated controls apply,
    # neutral provider controls are consumed (not forwarded), nothing raises.
    out = responses_request_to_chat_body(
        {
            "model": "m",
            "input": "hi",
            "store": False,
            "include": [],
            "service_tier": "auto",
            "prompt_cache_key": "k",
            "client_metadata": {"trace": "1"},
            "reasoning": {"effort": "high"},
            "text": {"verbosity": "medium", "format": {"type": "json_object"}},
        }
    )
    assert out["reasoning_effort"] == "high"
    assert out["response_format"] == {"type": "json_object"}
    for consumed in (
        "store",
        "include",
        "service_tier",
        "prompt_cache_key",
        "client_metadata",
    ):
        assert consumed not in out


def test_unknown_top_level_field_still_refused_with_neutral_siblings():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "store": False, "frobnicate": 1})


# consumed fields are value-sensitive: an ACTIVE (non-neutral) value is refused,
# never silently erased (round-5 finding 1).


def test_store_true_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "store": True})


def test_non_empty_include_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "include": ["reasoning.encrypted_content"]}
        )


def test_non_default_service_tier_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "service_tier": "flex"})


def test_non_null_access_programs_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "access_programs": ["daybreak_red"]}
        )


def test_raw_previous_response_id_is_refused():
    # A raw previous_response_id must be resolved+stripped upstream (round-6).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "delta", "previous_response_id": "resp_R"}
        )


def test_raw_previous_response_id_refused_even_with_prior_history():
    # A non-empty prior_messages list is NOT proof it was resolved from THIS id;
    # accepting the pair could run history for A while the caller named B.
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "delta", "previous_response_id": "resp_B"},
            prior_messages=[{"role": "user", "content": "from A"}],
        )


def test_resolved_prior_messages_without_raw_id_ok():
    # The supported follow-up shape: history materialized upstream, raw field
    # stripped.
    out = responses_request_to_chat_body(
        {"input": "delta"},
        prior_messages=[{"role": "user", "content": "q"}],
    )
    assert out["messages"][0] == {"role": "user", "content": "q"}
    assert "previous_response_id" not in out


def test_null_previous_response_id_is_accepted():
    # An explicit None is equivalent to absent -> accepted, not refused.
    out = responses_request_to_chat_body({"input": "hi", "previous_response_id": None})
    assert out["messages"] == [{"role": "user", "content": "hi"}]


# stream_options is value-sensitive: an active delivery control is refused, never
# accepted-and-erased (round-6 finding 1).


def test_active_stream_options_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": "x",
                "stream_options": {"reasoning_summary_delivery": "sequential_cutoff"},
            }
        )


def test_empty_stream_options_is_accepted():
    out = responses_request_to_chat_body({"input": "x", "stream_options": {}})
    assert "stream_options" not in out


def test_stream_options_not_populated_from_request_on_stream():
    # With stream:true the bridge builds its OWN stream_options (include_usage);
    # a request-supplied value is never the source, so an empty one stays neutral.
    out = responses_request_to_chat_body(
        {"input": "x", "stream": True, "stream_options": {}}
    )
    assert out["stream_options"] == {"include_usage": True}


def test_present_non_object_reasoning_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "reasoning": "high"})


def test_unknown_reasoning_subfield_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "reasoning": {"effort": "high", "bogus": 1}}
        )


def test_active_reasoning_summary_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "reasoning": {"effort": "high", "summary": "auto"}}
        )


def test_active_reasoning_context_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "reasoning": {"context": "all_turns"}}
        )


def test_unknown_text_subfield_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {"input": "x", "text": {"format": {"type": "text"}, "bogus": 1}}
        )


def test_active_text_verbosity_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "text": {"verbosity": "low"}})


def test_neutral_text_verbosity_medium_is_consumed():
    out = responses_request_to_chat_body(
        {"input": "x", "text": {"verbosity": "medium"}}
    )
    assert "response_format" not in out


# -- reasoning effort --------------------------------------------------------


def test_reasoning_effort_forwarded_with_allowed_openai_params():
    out = responses_request_to_chat_body(
        {"input": "x", "reasoning": {"effort": "high"}}
    )
    assert out["reasoning_effort"] == "high"
    assert out["allowed_openai_params"] == ["reasoning_effort"]


def test_reasoning_effort_above_vocabulary_is_clamped():
    # Default vocabulary is low,medium,high; Codex 'xhigh' clamps to 'high'.
    out = responses_request_to_chat_body(
        {"input": "x", "reasoning": {"effort": "xhigh"}}
    )
    assert out["reasoning_effort"] == "high"


def test_unknown_reasoning_effort_is_refused():
    # An off-ladder effort has no deterministic mapping; refuse rather than drop
    # the caller's reasoning constraint and silently run at backend default
    # effort (round-3 finding 4).
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "reasoning": {"effort": "bogus"}})


def test_no_reasoning_omits_both_keys():
    out = responses_request_to_chat_body({"input": "x"})
    assert "reasoning_effort" not in out
    assert "allowed_openai_params" not in out


def test_clamp_reasoning_effort_ties_go_cheaper():
    allowed = frozenset({"low", "high"})
    # 'medium' is equidistant from low and high -> cheaper (low).
    assert clamp_reasoning_effort("medium", allowed) == "low"


def test_clamp_reasoning_effort_passthrough_when_allowed_none():
    assert clamp_reasoning_effort("ultra", None) == "ultra"


# -- system-message normalization (direct, unconditional) --------------------


def test_normalize_user_policy_demotes_late_system_to_user():
    msgs = [
        {"role": "system", "content": "a"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "reminder"},
    ]
    out = normalize_system_messages(msgs, "user")
    assert out[0] == {"role": "system", "content": "a"}
    assert out[2] == {"role": "user", "content": "reminder"}


def test_normalize_hoist_merges_all_system_to_head():
    msgs = [
        {"role": "system", "content": "a"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "b"},
    ]
    out = normalize_system_messages(msgs, "hoist")
    assert out[0] == {"role": "system", "content": "a\n\nb"}
    assert all(m["role"] != "system" for m in out[1:])


def test_normalize_asis_passes_through():
    msgs = [{"role": "user", "content": "u"}, {"role": "system", "content": "s"}]
    assert normalize_system_messages(msgs, "asis") is msgs


def test_normalize_reject_policy_raises_when_rewrite_needed():
    msgs = [{"role": "user", "content": "u"}, {"role": "system", "content": "s"}]
    with pytest.raises(BridgeCapabilityError):
        normalize_system_messages(msgs, "reject")


def test_normalize_reject_policy_passes_valid_placement():
    # A single leading system message needs no rewrite -> accepted, not raised.
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert normalize_system_messages(msgs, "reject") is msgs


def test_normalize_unknown_policy_does_not_demote_it_rejects():
    # An unrecognized policy must take the fail-closed reject path, never a
    # silent demote (round-3 finding 3).
    msgs = [{"role": "user", "content": "u"}, {"role": "system", "content": "s"}]
    with pytest.raises(BridgeCapabilityError):
        normalize_system_messages(msgs, "bogus-policy")


def test_normalize_model_gate_skips_non_matching_model():
    msgs = [{"role": "user", "content": "u"}, {"role": "system", "content": "s"}]
    pattern = re.compile(r"qwen3\.\d", re.IGNORECASE)
    # A model that does not match the strict-template gate is left untouched.
    assert (
        normalize_system_messages(msgs, "user", model="gpt-4o", model_pattern=pattern)
        is msgs
    )
    # A matching model gets rewritten.
    rewritten = normalize_system_messages(
        msgs, "user", model="qwen3.5-32b", model_pattern=pattern
    )
    assert rewritten[1]["role"] == "user"


def test_request_default_policy_refuses_mid_system_for_gated_model():
    # Default policy is fail-closed reject: a qwen3.x model with a mid-array
    # system reminder is refused, not silently demoted (round-3 finding 3).
    body = {
        "model": "qwen3.5-8b",
        "input": [
            {"type": "message", "role": "user", "content": "u"},
            {"type": "message", "role": "system", "content": "reminder"},
        ],
    }
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(body)


def test_request_default_policy_passes_non_matching_model():
    # A non-matching model is not gated, so the mid-array system stays system.
    body = {
        "model": "gpt-4o",
        "input": [
            {"type": "message", "role": "user", "content": "u"},
            {"type": "message", "role": "system", "content": "reminder"},
        ],
    }
    out = responses_request_to_chat_body(body)
    assert out["messages"][1]["role"] == "system"


def test_request_user_policy_opt_in_demotes_mid_system(
    monkeypatch: pytest.MonkeyPatch,
):
    # The demote rewrite is now an explicit opt-in.
    monkeypatch.setenv("CODEX_BRIDGE_MID_SYSTEM_POLICY", "user")
    body = {
        "model": "qwen3.5-8b",
        "input": [
            {"type": "message", "role": "user", "content": "u"},
            {"type": "message", "role": "system", "content": "reminder"},
        ],
    }
    out = responses_request_to_chat_body(body)
    assert out["messages"][1]["role"] == "user"


def test_request_default_policy_allows_single_leading_system_for_gated_model():
    # A single leading system message needs no rewrite, so the fail-closed
    # default accepts it even for a gated model.
    body = {
        "model": "qwen3.5-8b",
        "instructions": "lead",
        "input": [{"type": "message", "role": "user", "content": "u"}],
    }
    out = responses_request_to_chat_body(body)
    assert out["messages"][0] == {"role": "system", "content": "lead"}
    assert out["messages"][1]["role"] == "user"


# -- malformed request shapes fail closed (round-3 finding 4) ----------------


def test_non_list_non_string_input_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": {"role": "user"}})


def test_non_object_input_item_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": ["bare string"]})


def test_non_string_text_content_payload_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body(
            {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": 123}],
                    }
                ]
            }
        )


def test_present_non_object_text_is_refused():
    with pytest.raises(BridgeCapabilityError):
        responses_request_to_chat_body({"input": "x", "text": "structured"})
