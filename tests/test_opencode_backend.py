"""OpenCode backend tests."""

import importlib
import json


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
