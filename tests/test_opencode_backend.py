"""OpenCode backend tests."""

import importlib


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
