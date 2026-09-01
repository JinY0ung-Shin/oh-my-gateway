"""Dynamic /v1/models discovery and Claude resolution."""

from unittest.mock import MagicMock

import httpx
import pytest

from src.backends.base import BackendDescriptor, BackendRegistry
from src.backends.claude import _claude_resolve
from src.backends.claude import model_discovery
from tests.test_main_api_unit import client_context


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    async def get(self, url, *, headers):
        self.calls.append((url, dict(headers)))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self):
        self.closed = True


def response(status: int, payload: object) -> httpx.Response:
    request = httpx.Request("GET", "https://upstream.example/v1/models?limit=1000")
    return httpx.Response(status, json=payload, request=request)


@pytest.fixture(autouse=True)
def reset_discovery(monkeypatch):
    model_discovery._reset_cache_for_tests()
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "MODEL_DISCOVERY_ENABLED",
        "MODEL_DISCOVERY_TTL_SECONDS",
        "MODEL_DISCOVERY_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    # Discovery is opt-in, so every test that exercises it turns it on. The
    # unset default is covered by test_discovery_is_off_by_default.
    monkeypatch.setenv("MODEL_DISCOVERY_ENABLED", "true")
    yield
    model_discovery._reset_cache_for_tests()


async def test_discovery_fetches_upstream_once_per_ttl(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fallback-api-key")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Tenant: team-a")
    client = StubClient(
        [
            response(
                200,
                {
                    "data": [
                        {"id": "Qwen3.8-27B"},
                        {"id": "GLM-5.3-Flash"},
                        {"id": "Qwen3.8-27B"},
                    ]
                },
            )
        ]
    )
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    first = await model_discovery.discover_models()
    second = await model_discovery.discover_models()

    assert first == ["Qwen3.8-27B", "GLM-5.3-Flash"]
    assert second == first
    assert len(client.calls) == 1
    url, headers = client.calls[0]
    assert url == "https://upstream.example/v1/models?limit=1000"
    assert headers["authorization"] == "Bearer secret-token"
    assert "x-api-key" not in headers
    assert headers["X-Tenant"] == "team-a"
    assert client.closed is True


async def test_discovery_uses_api_key_when_auth_token_is_absent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    client = StubClient([response(200, {"data": [{"id": "model-a"}]})])
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    assert await model_discovery.discover_models() == ["model-a"]
    _, headers = client.calls[0]
    assert headers["x-api-key"] == "api-key"
    assert "authorization" not in headers


async def test_discovery_failure_uses_last_successful_snapshot(monkeypatch):
    clock = [10.0]
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    monkeypatch.setenv("MODEL_DISCOVERY_TTL_SECONDS", "5")
    monkeypatch.setattr(model_discovery.time, "monotonic", lambda: clock[0])

    first_client = StubClient([response(200, {"data": [{"id": "model-a"}]})])
    monkeypatch.setattr(model_discovery, "_make_client", lambda: first_client)
    assert await model_discovery.discover_models() == ["model-a"]

    clock[0] = 20.0
    failed_client = StubClient([httpx.ConnectError("offline")])
    monkeypatch.setattr(model_discovery, "_make_client", lambda: failed_client)

    assert await model_discovery.discover_models() == ["model-a"]
    assert model_discovery.discovered_model_ids() == frozenset({"model-a"})

    # Failure is negative-cached for min(success TTL, 10s), so readers inside
    # that window get the stale snapshot immediately instead of serially paying
    # another upstream timeout.
    clock[0] = 22.0
    assert await model_discovery.discover_models() == ["model-a"]
    assert len(failed_client.calls) == 1


async def test_discovery_failure_without_cache_is_negative_cached(monkeypatch):
    clock = [10.0]
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    monkeypatch.setattr(model_discovery.time, "monotonic", lambda: clock[0])
    client = StubClient([httpx.ConnectError("offline")])
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    assert await model_discovery.discover_models() == []
    assert model_discovery.discovered_model_ids() == frozenset()

    clock[0] = 11.0
    assert await model_discovery.discover_models() == []
    assert len(client.calls) == 1


async def test_registry_warm_model_discovery_primes_registered_hooks(clean_registry):
    calls = []

    async def discover_active():
        calls.append("active")
        return ["dynamic-a"]

    async def discover_inactive():
        calls.append("inactive")
        return ["dynamic-b"]

    BackendRegistry.register_descriptor(
        BackendDescriptor(
            name="active",
            owned_by="test",
            models=[],
            resolve_fn=lambda model: None,
            model_discovery_fn=discover_active,
        )
    )
    BackendRegistry.register_descriptor(
        BackendDescriptor(
            name="inactive",
            owned_by="test",
            models=[],
            resolve_fn=lambda model: None,
            model_discovery_fn=discover_inactive,
        )
    )
    BackendRegistry.register("active", MagicMock())

    await BackendRegistry.warm_model_discovery()

    assert calls == ["active"]


async def test_discovery_is_off_by_default(monkeypatch):
    """Configuring an upstream alone must not widen the advertised models."""
    monkeypatch.delenv("MODEL_DISCOVERY_ENABLED", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    client = StubClient([response(200, {"data": [{"id": "GLM-5.3-Flash"}]})])
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    assert await model_discovery.discover_models() == []
    assert client.calls == []
    assert _claude_resolve("GLM-5.3-Flash") is None


async def test_discovery_disabled_skips_upstream_entirely(monkeypatch):
    """The kill switch stops the fetch and the /v1/models merge."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    monkeypatch.setenv("MODEL_DISCOVERY_ENABLED", "false")
    client = StubClient([response(200, {"data": [{"id": "GLM-5.3-Flash"}]})])
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    assert await model_discovery.discover_models() == []
    assert client.calls == []


async def test_disabling_discovery_revokes_cached_ids_from_resolution(monkeypatch):
    """A snapshot cached before the switch flipped must stop routing traffic."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    client = StubClient([response(200, {"data": [{"id": "GLM-5.3-Flash"}]})])
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    await model_discovery.discover_models()
    assert _claude_resolve("GLM-5.3-Flash") is not None

    monkeypatch.setenv("MODEL_DISCOVERY_ENABLED", "false")
    assert model_discovery.discovered_model_ids() == frozenset()
    assert _claude_resolve("GLM-5.3-Flash") is None
    # Static aliases are unaffected by the switch.
    assert _claude_resolve("sonnet") is not None


async def test_disabled_discovery_leaves_static_models_on_v1_models(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    monkeypatch.setenv("MODEL_DISCOVERY_ENABLED", "false")
    client = StubClient([response(200, {"data": [{"id": "GLM-5.3-Flash"}]})])
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    with client_context() as (api, _mock_cli):
        result = api.get("/v1/models")

    assert result.status_code == 200
    claude_ids = [
        entry["id"] for entry in result.json()["data"] if entry["backend"] == "claude"
    ]
    assert claude_ids == ["opus", "sonnet", "haiku"]
    assert client.calls == []


async def test_discovered_bare_and_provider_qualified_ids_resolve(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    client = StubClient(
        [
            response(
                200,
                {"data": [{"id": "qwen3.8-27b-rc"}, {"id": "openai/gpt-5.5"}]},
            )
        ]
    )
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    await model_discovery.discover_models()

    bare = _claude_resolve("qwen3.8-27b-rc")
    qualified = _claude_resolve("openai/gpt-5.5")
    assert bare is not None and bare.provider_model == "qwen3.8-27b-rc"
    assert qualified is not None and qualified.provider_model == "openai/gpt-5.5"
    assert _claude_resolve("typo-model") is None


async def test_registry_discovery_hook_is_generic(clean_registry):
    async def discover():
        return ["static-model", "dynamic-a", "dynamic-b", "dynamic-a"]

    desc = BackendDescriptor(
        name="dynamic",
        owned_by="test",
        models=["static-model"],
        resolve_fn=lambda model: None,
        capabilities={"image_input": True},
        model_discovery_fn=discover,
    )
    BackendRegistry.register_descriptor(desc)
    BackendRegistry.register("dynamic", MagicMock())

    entries = [
        item
        for item in await BackendRegistry.available_models_async()
        if item["backend"] == "dynamic"
    ]

    assert [item["id"] for item in entries] == [
        "static-model",
        "dynamic-a",
        "dynamic-b",
    ]
    assert all(item["capabilities"] == {"image_input": True} for item in entries)


async def test_registry_discovery_error_keeps_static_models(clean_registry):
    async def fail():
        raise RuntimeError("upstream unavailable")

    desc = BackendDescriptor(
        name="dynamic",
        owned_by="test",
        models=["static-model"],
        resolve_fn=lambda model: None,
        model_discovery_fn=fail,
    )
    BackendRegistry.register_descriptor(desc)
    BackendRegistry.register("dynamic", MagicMock())

    entries = [
        item
        for item in await BackendRegistry.available_models_async()
        if item["backend"] == "dynamic"
    ]

    assert [item["id"] for item in entries] == ["static-model"]


def test_v1_models_merges_discovered_claude_models(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://upstream.example")
    client = StubClient(
        [
            response(
                200,
                {"data": [{"id": "GLM-5.3-Flash"}, {"id": "Qwen3.8-27B"}]},
            )
        ]
    )
    monkeypatch.setattr(model_discovery, "_make_client", lambda: client)

    with client_context() as (api, _mock_cli):
        result = api.get("/v1/models")

    assert result.status_code == 200
    entries = result.json()["data"]
    claude_ids = [entry["id"] for entry in entries if entry["backend"] == "claude"]
    assert claude_ids[:3] == ["opus", "sonnet", "haiku"]
    assert "GLM-5.3-Flash" in claude_ids
    assert "Qwen3.8-27B" in claude_ids
