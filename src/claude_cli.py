"""Backward compatibility — use src.backends.claude instead.

Provides lazy re-exports of ClaudeCodeCLI and Claude-specific constants
so that existing ``from src.claude_cli import ClaudeCodeCLI`` continues to work.

NOTE: To monkeypatch constants for testing, patch the canonical location::

    monkeypatch.setattr("src.backends.claude.client.THINKING_MODE", "disabled")

Patching ``src.claude_cli.THINKING_MODE`` sets a module-level attr on this
shim but does NOT change what ``ClaudeCodeCLI`` reads at runtime.
"""


def __getattr__(name):
    if name == "ClaudeCodeCLI":
        from src.backends.claude.client import ClaudeCodeCLI

        return ClaudeCodeCLI

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
