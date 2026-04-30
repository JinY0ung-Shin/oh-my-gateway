"""Edge-case coverage for OpenCodeEventConverter helpers."""

from src.backends.opencode.events import OpenCodeEventConverter


def _conv(session_id="sess-1"):
    return OpenCodeEventConverter(session_id=session_id)


# ---------------------------------------------------------------------------
# error_message — session.error mapping (lines 60-65)
# ---------------------------------------------------------------------------


def test_error_message_returns_none_for_non_error_event():
    assert _conv().error_message({"type": "session.idle"}) is None


def test_error_message_returns_none_for_other_session():
    conv = _conv("mine")
    event = {
        "type": "session.error",
        "properties": {"sessionID": "other", "error": "boom"},
    }
    assert conv.error_message(event) is None


def test_error_message_returns_string_for_matching_session():
    conv = _conv("mine")
    event = {
        "type": "session.error",
        "properties": {"sessionID": "mine", "error": "kaboom"},
    }
    assert conv.error_message(event) == "kaboom"


def test_error_message_falls_back_to_message_or_props():
    conv = _conv("mine")
    e1 = {"type": "session.error", "properties": {"sessionID": "mine", "message": "msg"}}
    assert conv.error_message(e1) == "msg"
    e2 = {"type": "session.error", "properties": {"sessionID": "mine", "code": 500}}
    result = conv.error_message(e2)
    # Falls through to str(props) — assert content rather than exact dict-string formatting.
    assert result is not None
    assert "sessionID" in result
    assert "mine" in result
    assert "500" in result


# ---------------------------------------------------------------------------
# _event_session_id / _event_message_id falsy branches (lines 70, 76, 81, 86)
# ---------------------------------------------------------------------------


def test_event_session_id_returns_none_when_properties_missing():
    assert _conv()._event_session_id({"type": "x"}) is None


def test_event_session_id_returns_none_when_part_missing():
    assert _conv()._event_session_id({"properties": {}}) is None


def test_event_message_id_returns_none_when_properties_missing():
    assert _conv()._event_message_id({"type": "x"}) is None


def test_event_message_id_uses_info_id_when_messageID_absent():
    event = {"properties": {"info": {"id": "msg-123"}}}
    assert _conv()._event_message_id(event) == "msg-123"


def test_event_message_id_uses_part_messageID_as_last_fallback():
    event = {"properties": {"part": {"messageID": "msg-abc"}}}
    assert _conv()._event_message_id(event) == "msg-abc"


def test_event_message_id_returns_none_when_no_id_anywhere():
    assert _conv()._event_message_id({"properties": {}}) is None


# ---------------------------------------------------------------------------
# _record_message_role early-return (line 98)
# ---------------------------------------------------------------------------


def test_record_message_role_ignores_event_when_info_not_dict():
    conv = _conv()
    conv._record_message_role(
        {"type": "message.updated", "properties": {"info": "not-a-dict"}}
    )
    assert conv.message_roles == {}


# ---------------------------------------------------------------------------
# _convert_question_event guard (line 127)
# ---------------------------------------------------------------------------


def test_convert_question_event_returns_none_when_request_id_missing():
    event = {
        "type": "question.asked",
        "properties": {"questions": ["q1"]},
    }
    assert _conv().convert(event) == []


# ---------------------------------------------------------------------------
# _convert_permission_event guards (lines 154, 156)
# ---------------------------------------------------------------------------


def test_convert_permission_event_returns_none_for_missing_id():
    event = {
        "type": "permission.asked",
        "properties": {"permission": "read"},
    }
    assert _conv().convert(event) == []


def test_convert_permission_event_returns_none_for_missing_permission():
    event = {
        "type": "permission.asked",
        "properties": {"id": "req-1", "permission": ""},
    }
    assert _conv().convert(event) == []


# ---------------------------------------------------------------------------
# _convert_text_event guards (lines 195, 198, 216-220, 231)
# ---------------------------------------------------------------------------


def test_message_part_delta_with_non_text_field_is_dropped():
    event = {
        "type": "message.part.delta",
        "properties": {"sessionID": "sess-1", "field": "thinking", "delta": "ignore"},
    }
    assert _conv().convert(event) == []


def test_message_part_delta_with_empty_delta_is_dropped():
    event = {
        "type": "message.part.delta",
        "properties": {"sessionID": "sess-1", "delta": ""},
    }
    assert _conv().convert(event) == []


def test_message_part_updated_uses_text_fallback_when_no_delta():
    event = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {"id": "p1", "type": "text", "text": "hello"},
        },
    }
    chunks = _conv().convert(event)
    assert chunks == [
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    ]


def test_message_part_updated_text_fallback_returns_empty_when_no_change():
    conv = _conv()
    conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {"id": "p1", "type": "text", "text": "hello"},
            },
        }
    )
    chunks = conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {"id": "p1", "type": "text", "text": "hello"},
            },
        }
    )
    assert chunks == []


# ---------------------------------------------------------------------------
# _convert_usage_event guard (line 251)
# ---------------------------------------------------------------------------


def test_usage_event_skipped_when_tokens_not_dict():
    conv = _conv()
    conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {"type": "step-finish", "tokens": "not-a-dict"},
            },
        }
    )
    assert conv.usage is None


# ---------------------------------------------------------------------------
# _convert_tool_event guards (lines 282, 285, 300-301)
# ---------------------------------------------------------------------------


def test_tool_event_skipped_when_state_not_dict():
    event = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {"type": "tool", "state": "broken", "callID": "c1", "tool": "Read"},
        },
    }
    assert _conv().convert(event) == []


def test_tool_event_skipped_when_call_id_missing():
    event = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {"type": "tool", "state": {"status": "running"}, "tool": "Read"},
        },
    }
    assert _conv().convert(event) == []


def test_question_tool_emits_no_chunk_but_marks_results_on_completed():
    conv = _conv()
    chunks = conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {
                    "type": "tool",
                    "callID": "q1",
                    "tool": "question",
                    "state": {"status": "completed", "input": {"a": 1}},
                },
            },
        }
    )
    assert chunks == []
    assert "q1" in conv.emitted_tool_uses
    assert "q1" in conv.emitted_tool_results


def test_question_tool_error_status_marks_results():
    conv = _conv()
    chunks = conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {
                    "type": "tool",
                    "callID": "q2",
                    "tool": "question",
                    "state": {"status": "error", "input": {}},
                },
            },
        }
    )
    assert chunks == []
    assert "q2" in conv.emitted_tool_results
    assert "q2" in conv.emitted_tool_uses
