"""Unit tests for OpenCodeClient pure helpers (no live server)."""

from typing import Any

import httpx
import pytest

from src.backends.opencode.client import OpenCodeClient, OpenCodeSessionClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://example.com")
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    return OpenCodeClient()


# ---------------------------------------------------------------------------
# OpenCodeSessionClient.disconnect
# ---------------------------------------------------------------------------


async def test_session_disconnect_no_op_when_base_url_missing():
    sc = OpenCodeSessionClient(
        session_id="s1", cwd=None, model=None, system_prompt=None,
    )
    await sc.disconnect()  # No exception, no httpx call.


async def test_session_disconnect_short_circuits_on_404(monkeypatch):
    deleted: list[tuple[str, Any]] = []

    class FakeResponse:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("should not be called for 404")

    class FakeClient:
        def __init__(self, **kwargs): self.kwargs = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def delete(self, path, params=None):
            deleted.append((path, params))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    sc = OpenCodeSessionClient(
        session_id="s1", cwd="/tmp", model=None, system_prompt=None,
        base_url="http://x", timeout=1.0,
    )
    await sc.disconnect()
    assert deleted == [("/session/s1", {"directory": "/tmp"})]


async def test_session_disconnect_swallows_exceptions(monkeypatch):
    class BoomClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): raise RuntimeError("network down")
        async def __aexit__(self, *_): return False

    monkeypatch.setattr(httpx, "AsyncClient", BoomClient)

    sc = OpenCodeSessionClient(
        session_id="s1", cwd=None, model=None, system_prompt=None,
        base_url="http://x", timeout=1.0,
    )
    await sc.disconnect()  # Logs warning, does not raise.


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_auth_returns_none_when_password_unset(client):
    assert client._auth() is None


def test_auth_returns_basic_auth_when_password_set(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://example.com")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    c = OpenCodeClient()
    auth = c._auth()
    assert isinstance(auth, httpx.BasicAuth)


def test_client_kwargs_omits_auth_when_none(client):
    kwargs = client._client_kwargs()
    assert "auth" not in kwargs
    assert kwargs["base_url"] == "http://example.com"


def test_client_kwargs_includes_auth_when_present(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://example.com")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    c = OpenCodeClient()
    kwargs = c._client_kwargs()
    assert isinstance(kwargs["auth"], httpx.BasicAuth)


def test_event_client_kwargs_overrides_timeout_for_streaming(client):
    kwargs = client._event_client_kwargs()
    timeout = kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read is None  # Stream reads must not time out.


def test_directory_params_returns_none_when_cwd_empty(client):
    assert client._directory_params(None) is None
    assert client._directory_params("") is None


def test_directory_params_returns_dict_when_cwd_set(client):
    assert client._directory_params("/work") == {"directory": "/work"}


def test_combine_system_prompt_both_present(client):
    assert client._combine_system_prompt("base", "extra") == "base\n\nextra"


def test_combine_system_prompt_only_one_present(client):
    assert client._combine_system_prompt("base", None) == "base"
    assert client._combine_system_prompt(None, "extra") == "extra"


def test_combine_system_prompt_neither_present(client):
    assert client._combine_system_prompt(None, None) is None


def test_split_provider_model_returns_none_for_unsplittable(client):
    assert client._split_provider_model(None) is None
    assert client._split_provider_model("noslash") is None


def test_split_provider_model_returns_dict_for_valid(client):
    assert client._split_provider_model("anthropic/claude-sonnet") == {
        "providerID": "anthropic",
        "modelID": "claude-sonnet",
    }


def test_extract_text_handles_non_list_parts(client):
    assert client._extract_text({"parts": "not-a-list"}) == ""
    assert client._extract_text({}) == ""


def test_extract_text_concatenates_text_parts_only(client):
    payload = {
        "parts": [
            {"type": "text", "text": "hello "},
            {"type": "image"},
            {"type": "text", "text": "world"},
            {"type": "text", "text": ""},
            "not-a-dict",
        ]
    }
    assert client._extract_text(payload) == "hello world"


def test_extract_usage_returns_none_when_info_missing(client):
    assert client._extract_usage({}) is None
    assert client._extract_usage({"info": "not-a-dict"}) is None


def test_extract_usage_returns_none_when_tokens_missing(client):
    assert client._extract_usage({"info": {}}) is None
    assert client._extract_usage({"info": {"tokens": "bad"}}) is None


def test_extract_usage_sums_tokens(client):
    payload = {"info": {"tokens": {"input": 10, "output": 20, "reasoning": 5}}}
    assert client._extract_usage(payload) == {
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 35,
    }


def test_describe_non_json_response_includes_status_and_body_snippet(client):
    class FakeResp:
        status_code = 502
        headers = {"content-type": "text/html"}
        text = "<html>oops</html>"

    desc = client._describe_non_json_response(FakeResp())
    assert "status=502" in desc
    assert "content-type=text/html" in desc
    assert "<html>oops</html>" in desc


def test_prompt_parts_returns_single_text_when_no_image(client):
    parts = client._prompt_parts("hello world", cwd=None)
    assert parts == [{"type": "text", "text": "hello world"}]


def test_prompt_parts_treats_untrusted_image_marker_as_text(client):
    text = 'before <attached_image path="/tmp/x.png" /> after'
    parts = client._prompt_parts(text, cwd=None)
    assert parts == [{"type": "text", "text": text}]


# ---------------------------------------------------------------------------
# Trivial wrappers and verify path
# ---------------------------------------------------------------------------


def test_supported_models_returns_configured_list(client):
    assert isinstance(client.supported_models(), list)


def test_get_auth_provider_returns_provider_instance(client):
    from src.backends.opencode.auth import OpenCodeAuthProvider

    assert isinstance(client.get_auth_provider(), OpenCodeAuthProvider)


async def test_verify_returns_true_when_health_endpoint_reports_healthy(client, monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"healthy": True}

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, path):
            assert path == "/global/health"
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert await client.verify() is True


async def test_verify_returns_false_when_request_raises(client, monkeypatch):
    class BoomClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): raise RuntimeError("network down")
        async def __aexit__(self, *_): return False

    monkeypatch.setattr(httpx, "AsyncClient", BoomClient)
    assert await client.verify() is False
