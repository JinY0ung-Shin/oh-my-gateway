"""The verify probe must surface the CLI's *result* text, not the subtype.

Observed: an api_key-mode gateway pointed at a LiteLLM proxy failed with
``Claude Code returned an error result: success`` because the pinned SDK
reports the result ``subtype``; the actionable text (``API Error: 400 …``)
only lives in the result message itself (issue #198).
"""

from types import SimpleNamespace

from src.backends.claude.client import _error_result_text


def test_dict_error_result_returns_result_text():
    msg = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "API Error: 400 No connected db.",
    }
    assert _error_result_text(msg) == "API Error: 400 No connected db."


def test_dict_success_result_is_ignored():
    assert _error_result_text({"type": "result", "is_error": False, "result": "ok"}) == ""


def test_object_result_message_is_supported():
    msg = SimpleNamespace(is_error=True, result="API Error: 401 invalid x-api-key")
    assert _error_result_text(msg) == "API Error: 401 invalid x-api-key"


def test_non_result_messages_are_ignored():
    assert _error_result_text({"type": "assistant"}) == ""
    assert _error_result_text(SimpleNamespace(is_error=False, result="x")) == ""
