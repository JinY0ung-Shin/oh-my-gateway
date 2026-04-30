"""Unit tests for shared gateway constants."""

import importlib


def test_default_host_prefers_gateway_host_over_legacy_name(monkeypatch):
    import src.constants as constants

    with monkeypatch.context() as env:
        env.setenv("GATEWAY_HOST", "127.0.0.2")
        env.setenv("CLAUDE_WRAPPER_HOST", "127.0.0.3")

        reloaded = importlib.reload(constants)
        assert reloaded.DEFAULT_HOST == "127.0.0.2"

    importlib.reload(constants)


def test_default_host_keeps_legacy_host_fallback(monkeypatch):
    import src.constants as constants

    with monkeypatch.context() as env:
        env.delenv("GATEWAY_HOST", raising=False)
        env.setenv("CLAUDE_WRAPPER_HOST", "127.0.0.4")

        reloaded = importlib.reload(constants)
        assert reloaded.DEFAULT_HOST == "127.0.0.4"

    importlib.reload(constants)


def test_default_host_treats_empty_gateway_host_as_unset(monkeypatch):
    import src.constants as constants

    with monkeypatch.context() as env:
        env.setenv("GATEWAY_HOST", "")
        env.setenv("CLAUDE_WRAPPER_HOST", "127.0.0.5")

        reloaded = importlib.reload(constants)
        assert reloaded.DEFAULT_HOST == "127.0.0.5"

    importlib.reload(constants)
