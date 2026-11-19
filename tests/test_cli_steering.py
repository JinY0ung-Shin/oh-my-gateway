"""Pin the deployed Claude SDK's steering order without a real model provider."""

from __future__ import annotations

import asyncio
import threading
import uuid

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import ToolUseBlock
from starlette.requests import Request

from src.backends.claude.client import ClaudeCodeCLI
from src.backends.claude.sdk_client import GatewayClaudeSDKClient
from src.routes import responses
from src.session_manager import Session
from src.streaming_utils import stream_response_chunks
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI


@pytest.mark.integration
@pytest.mark.parametrize("during_tool", [True, False], ids=["tool-boundary", "text-continuation"])
async def test_steering_keeps_one_gateway_stream(tmp_path, monkeypatch, during_tool):
    began = threading.Event()
    release = threading.Event()
    tool_seen = asyncio.Event()
    followup = "use the updated scope"
    first = "start the original task"
    session_id = str(uuid.uuid4())
    run_id = uuid.uuid4().hex
    response_id = responses._make_response_id(session_id, 1)
    steer_id = uuid.uuid4()

    def plan(body):
        messages = repr(body.get("messages"))
        if followup in messages:
            return {"text": "updated answer"}
        if first not in messages:
            return {"text": "side request"}
        if during_tool and "tool_result" not in messages:
            return {"tool_use": {
                "name": "Bash",
                "input": {"command": "sleep 2", "description": "Wait briefly for test input"},
            }}
        began.set()
        release.wait(15)
        return {"text": "first answer"}

    async def permit(*args, **kwargs):
        pass

    cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
    session = Session(session_id=session_id, backend="claude", user="alice")
    monkeypatch.setattr(responses, "verify_api_key", permit)
    monkeypatch.setattr(responses, "enforce_scoped_identity", lambda request: None)
    monkeypatch.setattr(responses.session_manager, "get_session", lambda sid: session if sid == session_id else None)
    monkeypatch.setattr(responses.BackendRegistry, "get", lambda name: cli)
    request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": []})

    with FakeAnthropicAPI(plan) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(tmp_path),
            cwd=str(tmp_path),
            include_partial_messages=True,
            allowed_tools=["Bash"],
            extra_args={"replay-user-messages": None},
        )
        async with GatewayClaudeSDKClient(options=options) as client:
            await responses._begin_active_response(
                session, response_id, 1, client, steering_run_id=run_id
            )

            async def source():
                async for chunk in cli.run_completion_with_client(client, first, session):
                    if any(isinstance(b, ToolUseBlock) for b in (chunk.get("content") or [])):
                        tool_seen.set()
                    yield chunk

            async def collect():
                return [event async for event in stream_response_chunks(
                    chunk_source=source(), model="sonnet", response_id=response_id,
                    output_item_id="test-item", chunks_buffer=[], logger=responses.logger
                )]

            task = asyncio.create_task(collect())
            try:
                if during_tool:
                    await asyncio.wait_for(tool_seen.wait(), 20)
                else:
                    assert await asyncio.to_thread(began.wait, 20)
                body = responses.SteeringRequest(run_id=run_id, steer_id=steer_id, input=followup)
                with pytest.raises(responses.HTTPException) as wrong_user:
                    await responses.steer_response(request, response_id, body, "bob")
                assert wrong_user.value.status_code == 404
                with pytest.raises(responses.HTTPException) as wrong_run:
                    await responses.steer_response(
                        request, response_id, body.model_copy(update={"run_id": uuid.uuid4().hex}), "alice"
                    )
                assert wrong_run.value.status_code == 404
                receipt = await responses.steer_response(request, response_id, body, "alice")
                assert receipt == await responses.steer_response(request, response_id, body, "alice")
                release.set()
                frames = await asyncio.wait_for(task, 20)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    stream = "".join(frames)
    assert stream.count("event: response.steering_applied") == 1
    assert stream.count("event: response.completed") == 1
    assert stream.index("event: response.steering_applied") < stream.index("event: response.completed")
    assert "updated answer" in stream
    assert ("first answer" in stream) == (not during_tool)
    steered = [
        call["body"] for call in api.requests
        if followup in repr(call["body"].get("messages"))
    ]
    assert len(steered) == 1
    assert ("tool_result" in repr(steered[0]["messages"])) == during_tool
