"""Tests for function_call output models in response_models.py."""

from src.response_models import (
    FunctionCallOutputItem,
    FunctionCallOutputInput,
    ResponseObject,
    OutputItem,
    ResponseContentPart,
)


def test_function_call_output_item():
    item = FunctionCallOutputItem(
        id="fc_123",
        call_id="call_abc",
        name="AskUserQuestion",
        arguments='{"question": "Continue?"}',
    )
    assert item.type == "function_call"
    assert item.call_id == "call_abc"
    assert item.name == "AskUserQuestion"
    assert item.status == "completed"


def test_function_call_output_input():
    item = FunctionCallOutputInput(
        call_id="call_abc",
        output="Yes, go ahead",
    )
    assert item.type == "function_call_output"
    assert item.call_id == "call_abc"


def test_response_object_accepts_function_call_output():
    msg_item = OutputItem(id="msg_1", content=[ResponseContentPart(text="Hello")])
    fc_item = FunctionCallOutputItem(
        id="fc_1",
        call_id="call_abc",
        name="AskUserQuestion",
        arguments='{"question": "OK?"}',
    )
    resp = ResponseObject(id="resp_test", model="sonnet", output=[msg_item, fc_item])
    assert len(resp.output) == 2
    assert resp.output[1].type == "function_call"


def test_response_object_requires_action_status():
    resp = ResponseObject(id="resp_test", model="sonnet", status="requires_action")
    assert resp.status == "requires_action"
