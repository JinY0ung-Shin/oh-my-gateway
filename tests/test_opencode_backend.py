"""OpenCode backend tests."""

import importlib
import json


def test_opencode_client_exposes_runtime_metadata(monkeypatch):
    """OpenCode client reports operational metadata for diagnostics."""
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setenv("OPENCODE_MODELS", "openai/gpt-5.5")

    import src.backends.opencode.client as client_module
    import src.backends.opencode.constants as constants_module

    importlib.reload(constants_module)
    client_module = importlib.reload(client_module)

    client = client_module.OpenCodeClient()

    assert client.runtime_metadata() == {
        "mode": "external",
        "base_url": "http://127.0.0.1:4096",
        "agent": "general",
        "models": ["opencode/openai/gpt-5.5"],
        "managed_process": False,
    }


def test_opencode_runtime_metadata_treats_explicit_base_url_as_external(monkeypatch):
    """Direct base_url construction is external mode even without OPENCODE_BASE_URL."""
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.setenv("OPENCODE_MODELS", "openai/gpt-5.5")

    import src.backends.opencode.client as client_module
    import src.backends.opencode.constants as constants_module

    importlib.reload(constants_module)
    client_module = importlib.reload(client_module)

    client = client_module.OpenCodeClient(base_url="http://127.0.0.1:4096")

    assert client.runtime_metadata()["mode"] == "external"
    assert client.runtime_metadata()["managed_process"] is False


def test_opencode_descriptor_resolves_prefixed_model(monkeypatch):
    """OpenCode descriptor resolves opencode/<provider>/<model> IDs."""
    monkeypatch.setenv("OPENCODE_MODELS", "anthropic/claude-sonnet-4-5")

    import src.backends.opencode as opencode_pkg

    opencode_pkg = importlib.reload(opencode_pkg)

    resolved = opencode_pkg.OPENCODE_DESCRIPTOR.resolve_fn(
        "opencode/anthropic/claude-sonnet-4-5"
    )

    assert resolved is not None
    assert resolved.public_model == "opencode/anthropic/claude-sonnet-4-5"
    assert resolved.backend == "opencode"
    assert resolved.provider_model == "anthropic/claude-sonnet-4-5"
    assert opencode_pkg.OPENCODE_DESCRIPTOR.models == [
        "opencode/anthropic/claude-sonnet-4-5"
    ]


def test_opencode_descriptor_rejects_unprefixed_model(monkeypatch):
    """OpenCode descriptor does not claim bare provider/model IDs."""
    monkeypatch.setenv("OPENCODE_MODELS", "anthropic/claude-sonnet-4-5")

    import src.backends.opencode as opencode_pkg

    opencode_pkg = importlib.reload(opencode_pkg)

    assert opencode_pkg.OPENCODE_DESCRIPTOR.resolve_fn("anthropic/claude-sonnet-4-5") is None
    assert opencode_pkg.OPENCODE_DESCRIPTOR.resolve_fn("opencode/missing_provider_model") is None


def test_opencode_auth_provider_validates_managed_binary(monkeypatch):
    """Managed mode is valid when the opencode binary is available."""
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.setattr("src.backends.opencode.auth.shutil.which", lambda name: "/bin/opencode")

    from src.backends.opencode.auth import OpenCodeAuthProvider

    status = OpenCodeAuthProvider().validate()

    assert status["valid"] is True
    assert status["errors"] == []
    assert status["config"]["mode"] == "managed"


def test_auth_manager_returns_opencode_provider():
    """auth_manager can instantiate OpenCode auth before live backend registration."""
    from src.auth import auth_manager

    provider = auth_manager.get_provider("opencode")

    assert provider.name == "opencode"


def test_opencode_event_converter_emits_text_delta_and_final_text():
    """OpenCode event converter emits gateway text deltas and accumulates final text."""
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")

    chunks = converter.convert(
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "oc-session",
                "partID": "p1",
                "field": "text",
                "delta": "hello",
            },
        }
    )

    assert chunks == [
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    ]
    assert converter.final_text() == "hello"


def test_opencode_event_converter_ignores_other_sessions():
    """OpenCode event converter ignores events for unrelated sessions."""
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")

    chunks = converter.convert(
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "other-session",
                "partID": "p1",
                "field": "text",
                "delta": "wrong",
            },
        }
    )

    assert chunks == []
    assert converter.final_text() == ""


def test_opencode_event_converter_emits_tool_use_and_result_once():
    """OpenCode event converter emits a tool_use and a completed tool_result."""
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")

    running_chunks = converter.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-tool",
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "state": {"status": "running", "input": {"command": "pwd"}},
                },
            },
        }
    )
    completed_chunks = converter.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-tool",
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pwd"},
                        "output": "/tmp/work\n",
                    },
                },
            },
        }
    )

    assert running_chunks == [
        {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "bash",
                    "input": {"command": "pwd"},
                }
            ],
        }
    ]
    assert completed_chunks == [
        {
            "type": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "/tmp/work\n",
                    "is_error": False,
                }
            ],
        }
    ]


def test_opencode_event_converter_emits_error_tool_result():
    """OpenCode event converter emits error tool results from failed tool updates."""
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")

    chunks = converter.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "state": {
                        "status": "error",
                        "input": {"command": "exit 1"},
                        "error": "failed",
                    },
                },
            },
        }
    )

    assert chunks == [
        {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "bash",
                    "input": {"command": "exit 1"},
                }
            ],
        },
        {
            "type": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "failed",
                    "is_error": True,
                }
            ],
        },
    ]


def test_opencode_event_converter_emits_question_tool_use():
    """Question tool updates are exposed as Responses-compatible tool_use chunks."""
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")
    chunks = converter.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "sessionID": "oc-session",
                    "type": "tool",
                    "tool": "question",
                    "callID": "q1",
                    "state": {
                        "status": "running",
                        "input": {
                            "question": "Continue?",
                            "options": [{"label": "Yes"}, {"label": "No"}],
                        },
                    },
                }
            },
        }
    )

    assert chunks == [
        {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "q1",
                    "name": "question",
                    "input": {
                        "question": "Continue?",
                        "options": [{"label": "Yes"}, {"label": "No"}],
                    },
                }
            ],
        }
    ]
    assert converter.pending_question == {
        "call_id": "q1",
        "name": "question",
        "arguments": {
            "question": "Continue?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
    }


def test_opencode_event_converter_accumulates_step_finish_usage():
    """OpenCode event converter accumulates usage from step-finish parts."""
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")

    chunks = converter.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "type": "step-finish",
                    "tokens": {
                        "input": 11,
                        "output": 5,
                        "reasoning": 2,
                        "cache": {"read": 3, "write": 7},
                    },
                },
            },
        }
    )

    assert chunks == []
    assert converter.usage == {
        "input_tokens": 21,
        "output_tokens": 5,
        "total_tokens": 28,
    }
    assert converter.saw_activity is True


def test_opencode_event_converter_finishes_only_after_activity():
    """OpenCode event converter requires session activity before idle finishes."""
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")
    idle_event = {"type": "session.idle", "properties": {"sessionID": "oc-session"}}

    assert converter.finished(idle_event) is False

    converter.convert(
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "oc-session",
                "partID": "part-text",
                "field": "text",
                "delta": "ok",
            },
        }
    )

    assert converter.finished(
        {"type": "session.idle", "properties": {"sessionID": "other-session"}}
    ) is False
    assert converter.finished(idle_event) is True


async def test_opencode_client_sends_prompt_to_existing_server(monkeypatch):
    """OpenCode client creates a session and sends prompt bodies over HTTP."""
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, path):
            calls.append(("GET", path, {}))
            return FakeResponse({"healthy": True, "version": "test"})

        async def post(self, path, **kwargs):
            calls.append(("POST", path, kwargs))
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse(
                {
                    "info": {
                        "role": "assistant",
                        "tokens": {
                            "input": 7,
                            "output": 3,
                            "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        },
                    },
                    "parts": [{"type": "text", "text": "hello from opencode"}],
                }
            )

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(
        session=session,
        model="anthropic/claude-sonnet-4-5",
        system_prompt="request instructions",
        _custom_base="base prompt",
        cwd="/tmp/work",
    )
    chunks = [
        chunk async for chunk in backend.run_completion_with_client(client, "say hi", session)
    ]

    assert calls[0] == (
        "POST",
        "/session",
        {"json": {"title": "gw-session"}, "params": {"directory": "/tmp/work"}},
    )
    assert calls[1][0] == "POST"
    assert calls[1][1] == "/session/oc-session/message"
    assert calls[1][2]["json"]["system"] == "base prompt\n\nrequest instructions"
    assert calls[1][2]["json"]["model"] == {
        "providerID": "anthropic",
        "modelID": "claude-sonnet-4-5",
    }
    assert calls[1][2]["json"]["agent"] == "general"
    assert calls[1][2]["json"]["parts"] == [{"type": "text", "text": "say hi"}]
    assert getattr(session, "opencode_session_id") == "oc-session"
    assert chunks[-1]["result"] == "hello from opencode"
    assert chunks[-1]["usage"]["input_tokens"] == 7
    assert chunks[-1]["usage"]["output_tokens"] == 3
    assert backend.parse_message(chunks) == "hello from opencode"


async def test_opencode_client_uses_configured_agent(monkeypatch):
    """OPENCODE_AGENT overrides the default OpenCode request agent."""
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse(
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "text", "text": "ok"}],
                }
            )

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setenv("OPENCODE_AGENT", "plan")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(session=session, model="openai/gpt-5.1-codex")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert calls[1][1]["json"]["agent"] == "plan"
    assert chunks[-1]["result"] == "ok"


async def test_opencode_client_resumes_question_with_tool_output(monkeypatch):
    """OpenCode question continuation sends a completed question tool part."""
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "continued"}],
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, **kwargs):
            calls.append((path, kwargs))
            return FakeResponse()

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setenv("OPENCODE_AGENT", "plan")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient, OpenCodeSessionClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    client = OpenCodeSessionClient(
        session_id="oc-session",
        cwd="/tmp/work",
        model="openai/gpt-5.5",
        system_prompt=None,
    )
    chunks = [
        chunk
        async for chunk in backend.resume_question_with_client(
            client,
            "q1",
            "yes",
            Session(session_id="gw-session"),
        )
    ]

    assert calls == [
        (
            "/session/oc-session/message",
            {
                "json": {
                    "agent": "plan",
                    "parts": [
                        {
                            "type": "tool",
                            "callID": "q1",
                            "tool": "question",
                            "state": {"status": "completed", "output": "yes"},
                        }
                    ],
                    "model": {"providerID": "openai", "modelID": "gpt-5.5"},
                },
                "params": {"directory": "/tmp/work"},
            },
        )
    ]
    assert chunks == [
        {"type": "assistant", "content": [{"type": "text", "text": "continued"}]},
        {"type": "result", "subtype": "success", "result": "continued"},
    ]


async def test_opencode_client_reports_empty_json_response(monkeypatch):
    """Empty OpenCode response bodies produce actionable backend errors."""

    class EmptyResponse:
        status_code = 200
        text = ""
        headers = {"content-type": "application/json", "content-length": "0"}

        def raise_for_status(self):
            return None

        def json(self):
            raise json.JSONDecodeError("Expecting value", "", 0)

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, **kwargs):
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return EmptyResponse()

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(session=session, model="openai/gpt-5.1-codex")

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert chunks == [
        {
            "type": "error",
            "is_error": True,
            "error_message": (
                "OpenCode returned an empty or non-JSON response "
                "(status=200, content-type=application/json, body='')"
            ),
        }
    ]


async def test_opencode_client_streams_text_deltas_from_event_sse(monkeypatch):
    """Streaming mode uses prompt_async and converts OpenCode text deltas."""
    calls = []

    events = [
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "oc-session",
                "messageID": "msg-1",
                "partID": "part-1",
                "field": "text",
                "delta": "hel",
            },
        },
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "oc-session",
                "messageID": "msg-1",
                "partID": "part-1",
                "field": "text",
                "delta": "lo",
            },
        },
        {"type": "session.idle", "properties": {"sessionID": "oc-session"}},
    ]

    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for event in events:
                yield f"event: {event['type']}"
                yield "data: " + json.dumps(event)
                yield ""

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, path, **kwargs):
            calls.append(("STREAM", method, path, kwargs))
            return FakeStreamContext()

        async def post(self, path, **kwargs):
            calls.append(("POST", path, kwargs))
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse()

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(session=session, model="openai/gpt-5.1-codex")
    client.stream_events = True

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert calls[1] == (
        "STREAM",
        "GET",
        "/event",
        {"params": None},
    )
    assert calls[2][0] == "POST"
    assert calls[2][1] == "/session/oc-session/prompt_async"
    assert [chunk["event"]["delta"]["text"] for chunk in chunks[:2]] == ["hel", "lo"]
    assert chunks[-1] == {"type": "result", "subtype": "success", "result": "hello"}


async def test_opencode_client_streams_tool_use_and_result_from_event_sse(monkeypatch):
    """Streaming mode converts OpenCode tool updates to gateway tool chunks."""
    events = [
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-tool",
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "state": {
                        "status": "running",
                        "input": {"command": "pwd"},
                        "title": "pwd",
                    },
                },
            },
        },
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-tool",
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pwd"},
                        "output": "/tmp/work\n",
                        "title": "pwd",
                        "metadata": {},
                    },
                },
            },
        },
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-text",
                    "type": "text",
                    "text": "done",
                },
            },
        },
        {"type": "session.idle", "properties": {"sessionID": "oc-session"}},
    ]

    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for event in events:
                yield "data: " + json.dumps(event)
                yield ""

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def stream(self, method, path, **kwargs):
            return FakeStreamContext()

        async def post(self, path, **kwargs):
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse()

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(session=session, model="openai/gpt-5.1-codex")
    client.stream_events = True

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert {
        "type": "tool_use",
        "id": "call-1",
        "name": "bash",
        "input": {"command": "pwd"},
    } in [block for chunk in chunks for block in chunk.get("content", [])]
    assert {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": "/tmp/work\n",
        "is_error": False,
    } in [block for chunk in chunks for block in chunk.get("content", [])]
    assert chunks[-1]["result"] == "done"


async def test_opencode_streaming_aggregates_step_finish_usage(monkeypatch):
    """Streaming mode aggregates per-step OpenCode token usage."""
    events = [
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "oc-session",
                "partID": "part-text",
                "field": "text",
                "delta": "ok",
            },
        },
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-step-1",
                    "type": "step-finish",
                    "tokens": {
                        "input": 11,
                        "output": 5,
                        "reasoning": 2,
                        "cache": {"read": 3, "write": 7},
                    },
                },
            },
        },
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-step-2",
                    "type": "step-finish",
                    "tokens": {
                        "input": 13,
                        "output": 7,
                        "reasoning": 1,
                        "cache": {"read": 0, "write": 4},
                    },
                },
            },
        },
        {"type": "session.idle", "properties": {"sessionID": "oc-session"}},
    ]

    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for event in events:
                yield "data: " + json.dumps(event)
                yield ""

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, path, **kwargs):
            return FakeStreamContext()

        async def post(self, path, **kwargs):
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse()

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(session=session, model="openai/gpt-5.1-codex")
    client.stream_events = True

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert chunks[-1]["usage"] == {
        "input_tokens": 38,
        "output_tokens": 12,
    }


async def test_opencode_streaming_ignores_initial_idle_until_activity(monkeypatch):
    """An idle snapshot before any session activity must not end the stream."""
    events = [
        {"type": "session.idle", "properties": {"sessionID": "oc-session"}},
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "oc-session",
                "partID": "part-text",
                "field": "text",
                "delta": "after-idle",
            },
        },
        {"type": "session.idle", "properties": {"sessionID": "oc-session"}},
    ]

    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for event in events:
                yield "data: " + json.dumps(event)
                yield ""

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, path, **kwargs):
            return FakeStreamContext()

        async def post(self, path, **kwargs):
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse()

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(session=session, model="openai/gpt-5.1-codex")
    client.stream_events = True

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    assert chunks[-1]["result"] == "after-idle"


async def test_opencode_streaming_event_client_disables_read_timeout(monkeypatch):
    """The /event stream client disables read timeout while keeping connect timeout."""
    client_kwargs = []

    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"session.idle","properties":{"sessionID":"oc-session"}}'
            yield ""

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            client_kwargs.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, path, **kwargs):
            return FakeStreamContext()

        async def post(self, path, **kwargs):
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse()

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(session=session, model="openai/gpt-5.1-codex")
    client.stream_events = True

    _ = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]

    event_timeout = client_kwargs[1]["timeout"]
    assert event_timeout.connect == backend.timeout
    assert event_timeout.read is None


async def test_opencode_streaming_waits_for_non_empty_tool_input(monkeypatch):
    """Do not emit tool_use on pending empty input when a later update has input."""
    events = [
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-tool",
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "state": {"status": "pending", "input": {}},
                },
            },
        },
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "oc-session",
                "part": {
                    "id": "part-tool",
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "state": {"status": "running", "input": {"command": "pwd"}},
                },
            },
        },
        {"type": "session.idle", "properties": {"sessionID": "oc-session"}},
    ]

    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for event in events:
                yield "data: " + json.dumps(event)
                yield ""

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, *args):
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, path, **kwargs):
            return FakeStreamContext()

        async def post(self, path, **kwargs):
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse()

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(session=session, model="openai/gpt-5.1-codex")
    client.stream_events = True

    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "hi", session)]
    tool_uses = [
        block
        for chunk in chunks
        for block in chunk.get("content", [])
        if block.get("type") == "tool_use"
    ]

    assert tool_uses == [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "bash",
            "input": {"command": "pwd"},
        }
    ]
