from src.streaming_utils import extract_context_tokens, resolve_usage_details


def _assistant(
    *,
    input_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    output_tokens: int = 7,
    parent_tool_use_id: str | None = None,
) -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": parent_tool_use_id,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output_tokens,
        },
    }


def test_context_snapshot_uses_latest_main_agent_request_not_turn_total() -> None:
    chunks = [
        _assistant(input_tokens=100, cache_creation=20, cache_read=30),
        _assistant(
            input_tokens=50_000,
            cache_creation=10_000,
            cache_read=40_000,
            parent_tool_use_id="toolu_subagent",
        ),
        _assistant(input_tokens=150, cache_creation=40, cache_read=50),
        {
            "type": "result",
            "usage": {
                "input_tokens": 200_000,
                "cache_creation_input_tokens": 30_000,
                "cache_read_input_tokens": 70_000,
                "output_tokens": 999,
            },
        },
    ]

    # Claude Code context accounting includes input + cache creation + cache
    # reads + output for the final top-level AssistantMessage: 150+40+50+7.
    # The much larger ResultMessage and subagent request are irrelevant here.
    assert extract_context_tokens(chunks) == 247
    details = resolve_usage_details(chunks)
    assert details.context_tokens == 247

    # Billing/cache fields keep their existing turn-cumulative ResultMessage semantics.
    assert details.cached_tokens == 70_000
    assert details.cache_creation_tokens == 30_000


def test_context_snapshot_does_not_fall_back_to_cumulative_result_usage() -> None:
    chunks = [
        {
            "type": "result",
            "usage": {
                "input_tokens": 200_000,
                "cache_creation_input_tokens": 30_000,
                "cache_read_input_tokens": 70_000,
                "output_tokens": 999,
            },
        }
    ]

    assert extract_context_tokens(chunks) is None
    assert resolve_usage_details(chunks).context_tokens is None


def test_context_snapshot_tracks_post_compaction_request() -> None:
    chunks = [
        _assistant(input_tokens=8_000, cache_read=180_000),
        {"type": "system", "subtype": "compact_boundary", "trigger": "auto"},
        _assistant(input_tokens=3_000, cache_read=22_000),
    ]

    # The latest main-agent request is the post-compaction window, so the
    # snapshot falls instead of accumulating both requests (3000+22000+7).
    assert extract_context_tokens(chunks) == 25_007
