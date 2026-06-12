"""Regression tripwire for the OpenCode reasoning-token under-count.

OpenCode folds reasoning tokens into ``total_tokens`` only -- never into
``output_tokens`` -- on both usage paths:

* non-streaming: ``OpenCodeClient._extract_usage``
* streaming:     ``OpenCodeEventConverter._convert_usage_event``

The ``/v1/responses`` route reads ``(input_tokens, output_tokens)`` and discards
``total_tokens``, so reasoning-heavy OpenCode turns under-report output usage.

The Codex backend already does the OpenAI-compatible thing -- it rolls
``reasoningOutputTokens`` into ``output_tokens`` (``CodexClient._extract_usage``,
src/backends/codex/client.py) so that ``input + output == total``. OpenCode is
the odd one out.

The two ``xfail(strict=True)`` tests below assert the *desired* parity
behaviour, so the day OpenCode is fixed they flip to failures -- forcing this
file (and the existing characterization tests in test_opencode_backend.py that
assert the current under-count) to be updated together. Changing this is a
token-accounting / usage-reporting behaviour change, hence left as a tracked
gap rather than silently patched. Tracked in memory: opencode-reasoning-undercount.
"""

import pytest

from src.backends.codex.client import CodexClient
from src.backends.opencode.client import OpenCodeClient
from src.backends.opencode.events import OpenCodeEventConverter


def _step_finish_event(
    session_id="oc-session", *, input_tokens=11, output=5, reasoning=2
):
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": session_id,
            "part": {
                "type": "step-finish",
                "tokens": {
                    "input": input_tokens,
                    "output": output,
                    "reasoning": reasoning,
                },
            },
        },
    }


def test_codex_extract_usage_folds_reasoning_into_output_tokens():
    """The parity contract OpenCode should match: reasoning counts as output."""
    usage = CodexClient()._extract_usage(
        {"last": {"inputTokens": 11, "outputTokens": 5, "reasoningOutputTokens": 2}}
    )
    assert usage == {"input_tokens": 11, "output_tokens": 7}


@pytest.mark.xfail(
    strict=True,
    reason="opencode-reasoning-undercount: OpenCodeClient._extract_usage omits "
    "reasoning from output_tokens; Codex folds it in. Remove this xfail once "
    "OpenCode is brought to parity.",
)
def test_opencode_extract_usage_should_fold_reasoning_into_output_tokens():
    usage = OpenCodeClient()._extract_usage(
        {"info": {"tokens": {"input": 11, "output": 5, "reasoning": 2}}}
    )
    assert usage["output_tokens"] == 7  # 5 visible + 2 reasoning


@pytest.mark.xfail(
    strict=True,
    reason="opencode-reasoning-undercount: streaming OpenCodeEventConverter omits "
    "reasoning from output_tokens. Remove this xfail once OpenCode is at parity.",
)
def test_opencode_event_converter_should_fold_reasoning_into_output_tokens():
    converter = OpenCodeEventConverter(session_id="oc-session")
    converter.convert(_step_finish_event())
    assert converter.usage["output_tokens"] == 7  # 5 visible + 2 reasoning
