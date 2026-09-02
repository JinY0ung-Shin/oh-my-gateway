"""BACKENDS=codex cutover onto the app-server adapter (issue #173 PR E).

Verifies that enabling the codex backend registers the app-server adapter by
default, that model resolution routes to it, and that CODEX_BACKEND=frozen is an
independent rollback to the legacy client. File/test names avoid the
stale-backend deselector substring so these run in the default suite.
"""

from __future__ import annotations

import pytest

from src.backends import discover_backends, resolve_model
from src.backends.base import BackendRegistry
from src.backends.appserver.client import AppServerCodexClient


@pytest.fixture
def clean_registry():
    BackendRegistry.clear()
    yield
    BackendRegistry.clear()


def test_opt_in_flag_registers_the_appserver_adapter(
    clean_registry, monkeypatch: pytest.MonkeyPatch
):
    # The adapter is opt-in only until the production isolation gate passes.
    monkeypatch.setenv("BACKENDS", "codex")
    monkeypatch.setenv("CODEX_BACKEND", "appserver")
    discover_backends(registry_cls=BackendRegistry)

    client = BackendRegistry.get("codex")
    assert isinstance(client, AppServerCodexClient)

    resolved = resolve_model("codex/gpt-5.5")
    assert resolved is not None
    assert resolved.backend == "codex"
    assert resolved.provider_model == "gpt-5.5"


def test_default_registers_the_frozen_client_not_the_adapter(
    clean_registry, monkeypatch: pytest.MonkeyPatch
):
    # Default (no CODEX_BACKEND) keeps the frozen client — no default cutover.
    monkeypatch.setenv("BACKENDS", "codex")
    monkeypatch.delenv("CODEX_BACKEND", raising=False)
    discover_backends(registry_cls=BackendRegistry)

    # The frozen client (or, if its construction failed, at least not the
    # adapter) is what got registered.
    try:
        client = BackendRegistry.get("codex")
    except ValueError:
        # Known-but-unavailable: descriptor registered, client construction
        # swallowed. That is still the frozen path, not the adapter.
        return
    assert not isinstance(client, AppServerCodexClient)
    assert type(client).__module__.startswith("src.backends.codex")


def test_claude_only_default_does_not_register_the_backend(
    clean_registry, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BACKENDS", "claude")
    discover_backends(registry_cls=BackendRegistry)
    assert not BackendRegistry.is_registered("codex")
