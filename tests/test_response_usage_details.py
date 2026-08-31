"""Usage breakdown exposed on /v1/responses payloads.

Covers ``total_tokens`` derivation and the ``input_tokens_details`` cache
breakdown added so cost-tracking clients (open-webui, LiteLLM) can read
cached-token counts the gateway already stores in its usage log.
"""

from src.response_models import InputTokensDetails, ResponseUsage
from src.streaming_utils import resolve_usage_details


def _result_chunk(**usage):
    return [{"type": "result", "usage": usage}]


class TestTotalTokens:
    def test_total_is_derived_from_input_and_output(self):
        usage = ResponseUsage(input_tokens=100, output_tokens=50)
        assert usage.total_tokens == 150

    def test_caller_supplied_total_is_ignored_in_favor_of_the_sum(self):
        """``total_tokens`` is derived state, never caller-controlled."""
        usage = ResponseUsage(input_tokens=10, output_tokens=5, total_tokens=9999)
        assert usage.total_tokens == 15

    def test_defaults_are_zero(self):
        usage = ResponseUsage()
        assert usage.total_tokens == 0
        assert usage.input_tokens_details.cached_tokens == 0
        assert usage.input_tokens_details.cache_creation_tokens == 0


class TestReasoningTokensDeliberatelyAbsent:
    def test_no_output_tokens_details_field(self):
        """Claude folds thinking tokens into output_tokens and never reports
        them separately, so the gateway must not emit a fabricated zero."""
        assert "output_tokens_details" not in ResponseUsage().model_dump()


class TestResolveUsageDetails:
    def test_maps_cache_read_to_openai_cached_tokens(self):
        details = resolve_usage_details(
            _result_chunk(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=300,
                cache_creation_input_tokens=200,
            )
        )
        assert details.cached_tokens == 300
        assert details.cache_creation_tokens == 200

    def test_cached_tokens_stay_a_subset_of_input_tokens(self):
        """OpenAI semantics: cached_tokens ⊆ input_tokens.

        ``extract_sdk_usage`` folds both cache counters into the reported
        input total, so the invariant must hold for the emitted payload.
        """
        chunks = _result_chunk(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=300,
            cache_creation_input_tokens=200,
        )
        from src.streaming_utils import resolve_token_usage

        input_tokens, output_tokens = resolve_token_usage(chunks, "", "")
        usage = ResponseUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=resolve_usage_details(chunks),
        )
        assert usage.input_tokens == 600
        assert usage.input_tokens_details.cached_tokens <= usage.input_tokens
        assert usage.total_tokens == 650

    def test_both_detail_fields_are_subsets_of_input_tokens(self):
        """input_tokens = uncached + cache_creation + cached.

        Cache-creation tokens are folded into the prompt total just like
        cache reads, so a cost calculator must subtract *both* to get the
        full-price remainder. Documenting only ``cached_tokens`` as a subset
        led a reviewer to compute 3x the true uncached count.
        """
        from src.streaming_utils import resolve_token_usage

        chunks = _result_chunk(
            input_tokens=100,  # the uncached remainder
            output_tokens=50,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=300,
        )
        input_tokens, _ = resolve_token_usage(chunks, "", "")
        details = resolve_usage_details(chunks)

        assert input_tokens == 600
        uncached = input_tokens - details.cached_tokens - details.cache_creation_tokens
        assert uncached == 100

    def test_zeroes_when_turn_carried_no_sdk_usage(self):
        """The estimation fallback has no cache information to report."""
        details = resolve_usage_details([{"type": "assistant"}])
        assert details == InputTokensDetails(cached_tokens=0, cache_creation_tokens=0)

    def test_sums_assistant_chunks_when_result_usage_missing(self):
        chunks = [
            {"type": "assistant", "usage": {"cache_read_input_tokens": 10}},
            {"type": "assistant", "usage": {"cache_read_input_tokens": 7}},
        ]
        assert resolve_usage_details(chunks).cached_tokens == 17

    def test_agrees_with_the_usage_log_row_for_the_same_turn(self):
        """The API payload and the usage-log row must never disagree."""
        from src.usage_logger import extract_sdk_usage_detail

        chunks = _result_chunk(
            input_tokens=1,
            output_tokens=2,
            cache_read_input_tokens=33,
            cache_creation_input_tokens=44,
        )
        row = extract_sdk_usage_detail(chunks)
        details = resolve_usage_details(chunks)
        assert details.cached_tokens == row["cache_read_tokens"]
        assert details.cache_creation_tokens == row["cache_creation_tokens"]


def _assistant(prompt, *, cache_read=0, cache_creation=0, parent=None, output=7):
    """One main-agent (or subagent) model request with the given prompt size."""
    chunk = {
        "type": "assistant",
        "usage": {
            "input_tokens": prompt,
            "output_tokens": output,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }
    if parent:
        chunk["parent_tool_use_id"] = parent
    return chunk


class TestContextTokensSnapshot:
    """``context_tokens`` is a window-occupancy SNAPSHOT, not a turn total.

    The bug this pins: ChatDRAGON's context chip refuses to estimate and reads
    this field only, so while the gateway omitted it the chip fell to "—" on
    every conversation that had seen a single turn. And it must never be the
    cumulative prompt total — an agentic turn re-sends the transcript per tool
    round, which is what once drew 263k/250k.
    """

    def test_snapshot_is_the_last_request_not_the_sum_of_all_rounds(self):
        # Three tool rounds, each re-sending a longer transcript.
        chunks = [_assistant(1000), _assistant(1800), _assistant(2500)]
        details = resolve_usage_details(chunks)
        assert details.context_tokens == 2500, "must be the final prompt, not 5300"

    def test_cache_reads_still_occupy_the_window(self):
        """A cached prompt is cheaper, not smaller — it holds the same space."""
        chunks = [_assistant(200, cache_read=9000, cache_creation=800)]
        assert resolve_usage_details(chunks).context_tokens == 10000

    def test_subagent_prompt_never_stands_in_for_the_conversation(self):
        """Subagents run their own context; the last MAIN request is the answer."""
        chunks = [
            _assistant(4000),
            _assistant(120, parent="toolu_sub"),
            _assistant(90, parent="toolu_sub"),
        ]
        assert resolve_usage_details(chunks).context_tokens == 4000

    def test_result_totals_alone_are_not_a_snapshot(self):
        """``ResultMessage.usage`` carries the same cumulative totals, so a turn
        with no assistant usage yields None — the client says "unmeasured"."""
        details = resolve_usage_details(
            _result_chunk(input_tokens=99000, output_tokens=400)
        )
        assert details.context_tokens is None
        assert details.cached_tokens == 0

    def test_no_usage_at_all_is_unmeasured_not_zero(self):
        assert resolve_usage_details([]).context_tokens is None

    def test_field_is_published_on_the_wire(self):
        """The frontend reads ``input_tokens_details.context_tokens``; the key
        has to survive serialization or the chip is back to "—"."""
        payload = ResponseUsage(
            input_tokens=5300,
            output_tokens=21,
            input_tokens_details=InputTokensDetails(context_tokens=2500),
        ).model_dump()
        assert payload["input_tokens_details"]["context_tokens"] == 2500
