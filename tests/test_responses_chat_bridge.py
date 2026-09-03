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


def test_function_call_output_array_extracts_text_and_counts_non_text():
    out = responses_request_to_chat_body(
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
    tool_msg = out["messages"][0]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    assert "result" in tool_msg["content"]
    assert "1 non-text tool output part" in tool_msg["content"]


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


def test_input_image_file_id_only_is_skipped():
    out = responses_request_to_chat_body(
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
    # No usable image url -> text-only string content, image dropped.
    assert out["messages"][0]["content"] == "hi"


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


def test_unknown_reasoning_effort_is_dropped():
    out = responses_request_to_chat_body(
        {"input": "x", "reasoning": {"effort": "bogus"}}
    )
    assert "reasoning_effort" not in out
    assert "allowed_openai_params" not in out


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


def test_request_applies_mid_system_gate(monkeypatch: pytest.MonkeyPatch):
    # With a qwen3.x model the mid-array system reminder is demoted to user.
    body = {
        "model": "qwen3.5-8b",
        "input": [
            {"type": "message", "role": "user", "content": "u"},
            {"type": "message", "role": "system", "content": "reminder"},
        ],
    }
    out = responses_request_to_chat_body(body)
    assert out["messages"][1]["role"] == "user"
    # A non-matching model leaves it as system.
    body["model"] = "gpt-4o"
    out2 = responses_request_to_chat_body(body)
    assert out2["messages"][1]["role"] == "system"
