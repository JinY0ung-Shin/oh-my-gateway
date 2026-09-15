"""The verify probe must surface the CLI's *result* text, not the subtype.

Observed: an api_key-mode gateway pointed at a LiteLLM proxy failed with
``Claude Code returned an error result: success`` because the pinned SDK
reports the result ``subtype``; the actionable text (``API Error: 400 …``)
only lives in the result message itself (issue #198).
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

from src.backends.claude.client import ClaudeCodeCLI, _error_result_text


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


async def test_verify_preserves_original_error_when_options_build_fails(tmp_path, caplog):
    """An exception BEFORE any result message must not be masked.

    ``verify()`` reads its diagnostic state in the ``except`` handler; if that
    state were initialised inside the ``try`` after ``_build_sdk_options()``, an
    early failure would surface as ``UnboundLocalError`` and escape the
    ``False``-and-log contract.
    """
    with patch("src.auth.validate_claude_code_auth", return_value=(True, {})), patch(
        "src.auth.auth_manager"
    ) as mock_auth:
        mock_auth.get_claude_code_env_vars.return_value = {}
        cli = ClaudeCodeCLI(cwd=str(tmp_path))

    with patch.object(cli, "_build_sdk_options", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.ERROR):
            assert await cli.verify() is False

    failures = [r.getMessage() for r in caplog.records if "verification failed" in r.getMessage()]
    assert failures, caplog.text
    assert "boom" in failures[-1]
    assert "UnboundLocalError" not in caplog.text
    assert "last_error_result" not in caplog.text
