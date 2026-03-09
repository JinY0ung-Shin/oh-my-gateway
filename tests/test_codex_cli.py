#!/usr/bin/env python3
"""
Unit tests for Codex CLI subprocess integration.

Covers: subprocess mocking, JSONL preamble handling, cross-isolation,
authentication validation, and concurrency race conditions for the Codex backend.
"""

import asyncio
import contextlib
import json
import os
import pytest
from unittest.mock import patch

from src.auth import (
    BackendAuthProvider,
    ClaudeAuthProvider,
    CodexAuthProvider,
)


class TestCodexSubprocessMock:
    """Verify the mock_codex_subprocess fixture works correctly."""

    async def test_default_stdout_returns_events(self, mock_codex_subprocess):
        """Default mock produces JSONL lines."""
        proc = mock_codex_subprocess.proc
        lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            lines.append(line)

        assert len(lines) == 2
        event = json.loads(lines[0])
        assert event["type"] == "message.delta"

    async def test_custom_events(self, mock_codex_subprocess):
        """set_stdout allows custom events."""
        mock_codex_subprocess.set_stdout(events=[{"type": "error", "message": "something broke"}])
        proc = mock_codex_subprocess.proc
        line = await proc.stdout.readline()
        event = json.loads(line)
        assert event["type"] == "error"

    async def test_preamble_mixed_with_jsonl(self, mock_codex_subprocess):
        """Non-JSON preamble line is emitted before JSONL events."""
        mock_codex_subprocess.set_stdout(
            events=[{"type": "message.completed"}],
            preamble="Reading prompt from stdin...",
        )
        proc = mock_codex_subprocess.proc
        lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            lines.append(line)

        # First line is preamble (not valid JSON)
        assert lines[0] == b"Reading prompt from stdin...\n"
        with pytest.raises(json.JSONDecodeError):
            json.loads(lines[0])

        # Second line is valid JSONL
        event = json.loads(lines[1])
        assert event["type"] == "message.completed"


class TestJsonlPreambleHandling:
    """Verify a robust JSONL parser that skips non-JSON lines."""

    @staticmethod
    def parse_jsonl_robust(lines: list[bytes]) -> list[dict]:
        """Reference implementation of preamble-safe JSONL parsing."""
        events = []
        for raw in lines:
            text = raw.decode().strip()
            if not text:
                continue
            try:
                events.append(json.loads(text))
            except json.JSONDecodeError:
                # Skip non-JSON preamble (e.g. "Reading prompt from stdin...")
                continue
        return events

    async def test_parse_with_preamble(self, mock_codex_subprocess):
        """Parser correctly skips preamble and extracts JSONL events."""
        mock_codex_subprocess.set_stdout(
            events=[
                {"type": "message.delta", "delta": {"content": "hi"}},
                {"type": "message.completed"},
            ],
            preamble="Reading prompt from stdin...",
        )
        proc = mock_codex_subprocess.proc

        raw_lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            raw_lines.append(line)

        events = self.parse_jsonl_robust(raw_lines)
        assert len(events) == 2
        assert events[0]["type"] == "message.delta"
        assert events[1]["type"] == "message.completed"

    async def test_parse_without_preamble(self, mock_codex_subprocess):
        """Parser works when there is no preamble."""
        mock_codex_subprocess.set_stdout(
            events=[{"type": "message.completed"}],
            preamble="",
        )
        proc = mock_codex_subprocess.proc

        raw_lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            raw_lines.append(line)

        events = self.parse_jsonl_robust(raw_lines)
        assert len(events) == 1


class TestCrossIsolationEnv:
    """Verify cross-backend env var isolation at the subprocess env level."""

    async def test_codex_subprocess_excludes_anthropic_token(self, mock_codex_subprocess):
        """When spawning Codex, ANTHROPIC_AUTH_TOKEN must not be in env."""
        import asyncio

        codex_provider = CodexAuthProvider()

        # Simulate building a subprocess env for Codex
        base_env = {
            "PATH": "/usr/bin",
            "HOME": "/tmp/codex_home",
            "ANTHROPIC_AUTH_TOKEN": "should-not-leak",
            "OPENAI_API_KEY": "sk-test-key",
        }

        # Apply isolation: remove vars that Codex says should be isolated
        subprocess_env = dict(base_env)
        for var in codex_provider.get_isolation_vars():
            subprocess_env.pop(var, None)

        # Add Codex's own env
        subprocess_env.update(codex_provider.build_env())

        # Spawn mock subprocess with this env
        await asyncio.create_subprocess_exec("codex", "--json", env=subprocess_env)

        # Verify captured env
        captured = mock_codex_subprocess.captured_env
        assert "ANTHROPIC_AUTH_TOKEN" not in captured
        assert captured["OPENAI_API_KEY"] == "sk-test-key"

    async def test_claude_subprocess_excludes_openai_key(self, mock_codex_subprocess):
        """When spawning Claude, OPENAI_API_KEY must not be in env."""
        import asyncio

        claude_provider = ClaudeAuthProvider()

        base_env = {
            "PATH": "/usr/bin",
            "HOME": "/tmp/claude_home",
            "ANTHROPIC_AUTH_TOKEN": "ant-key-12345",
            "OPENAI_API_KEY": "should-not-leak",
        }

        subprocess_env = dict(base_env)
        for var in claude_provider.get_isolation_vars():
            subprocess_env.pop(var, None)
        subprocess_env.update(claude_provider.build_env())

        await asyncio.create_subprocess_exec("claude", "--json", env=subprocess_env)

        captured = mock_codex_subprocess.captured_env
        assert "OPENAI_API_KEY" not in captured
        assert captured["ANTHROPIC_AUTH_TOKEN"] == "ant-key-12345"


class TestCodexAuthValidation:
    """Test Codex authentication error handling."""

    def test_missing_openai_key_error_message(self):
        """Missing OPENAI_API_KEY produces a clear error."""
        env_copy = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env_copy, clear=True):
            provider = CodexAuthProvider()
            status = provider.validate()
            assert status["valid"] is False
            assert "OPENAI_API_KEY" in status["errors"][0]

    def test_non_sk_key_still_valid(self):
        """Non-sk- prefixed key is accepted (with warning)."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-non-sk-key"}):
            provider = CodexAuthProvider()
            status = provider.validate()
            assert status["valid"] is True

    def test_codex_auth_via_auth_manager(self):
        """auth_manager.get_provider('codex') validates correctly."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-valid-key-123"}):
            import importlib
            import src.auth

            importlib.reload(src.auth)
            provider = src.auth.auth_manager.get_provider("codex")
            status = provider.validate()
            assert status["valid"] is True


class TestBackendAuthProviderABC:
    """Verify the ABC contract cannot be instantiated directly."""

    def test_cannot_instantiate_abc(self):
        """BackendAuthProvider cannot be instantiated directly."""
        with pytest.raises(TypeError):
            BackendAuthProvider()

    def test_incomplete_subclass_raises(self):
        """Subclass missing abstract methods cannot be instantiated."""

        class IncompleteProvider(BackendAuthProvider):
            @property
            def name(self):
                return "incomplete"

        with pytest.raises(TypeError):
            IncompleteProvider()


class TestEnvIsolationRaceCondition:
    """Verify that concurrent Claude+Codex requests don't lose keys.

    Regression test for the race condition where Claude's _sdk_env()
    temporarily removes OPENAI_API_KEY from os.environ, causing a
    concurrent Codex _build_env() to see a missing key.
    """

    def test_codex_build_env_uses_captured_key_not_os_environ(self):
        """CodexCLI._build_env() must use the init-time captured key."""
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-init-time-key", "PATH": "/usr/bin"},
        ):
            from src.codex_cli import CodexCLI

            with patch.object(CodexCLI, "_find_codex_binary", return_value="/usr/bin/codex"):
                cli = CodexCLI(cwd="/tmp")

            # Verify the key was captured at init
            assert cli._api_key == "sk-init-time-key"

            # Simulate Claude's _sdk_env removing OPENAI_API_KEY from os.environ
            os.environ.pop("OPENAI_API_KEY", None)

            # _build_env should STILL have the key from init-time capture
            env = cli._build_env()
            assert env["OPENAI_API_KEY"] == "sk-init-time-key"

    def test_claude_sdk_env_does_not_affect_codex_captured_key(self):
        """Concurrent Claude _sdk_env() removal doesn't affect CodexCLI."""
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-codex-key-123",
                "ANTHROPIC_AUTH_TOKEN": "ant-claude-key-456",
                "PATH": "/usr/bin",
            },
        ):
            from src.codex_cli import CodexCLI

            with patch.object(CodexCLI, "_find_codex_binary", return_value="/usr/bin/codex"):
                codex = CodexCLI(cwd="/tmp")

            # Simulate what Claude's _sdk_env does: remove OPENAI_API_KEY
            removed_key = os.environ.pop("OPENAI_API_KEY", None)
            assert removed_key == "sk-codex-key-123"
            assert "OPENAI_API_KEY" not in os.environ

            # Codex should still produce correct env
            env = codex._build_env()
            assert env["OPENAI_API_KEY"] == "sk-codex-key-123"

    async def test_concurrent_claude_codex_env_isolation(self):
        """Simulated concurrent Claude+Codex env operations are safe."""
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-codex-key",
                "ANTHROPIC_AUTH_TOKEN": "ant-claude-key",
                "PATH": "/usr/bin",
            },
        ):
            from src.codex_cli import CodexCLI

            with patch.object(CodexCLI, "_find_codex_binary", return_value="/usr/bin/codex"):
                codex = CodexCLI(cwd="/tmp")

            # Simulate Claude's _sdk_env context (removes OPENAI_API_KEY)
            @contextlib.contextmanager
            def fake_claude_sdk_env():
                removed = {}
                try:
                    if "OPENAI_API_KEY" in os.environ:
                        removed["OPENAI_API_KEY"] = os.environ.pop("OPENAI_API_KEY")
                    yield
                finally:
                    for k, v in removed.items():
                        os.environ[k] = v

            results = {}

            async def claude_task():
                """Simulate a Claude request that holds _sdk_env for a while."""
                with fake_claude_sdk_env():
                    # OPENAI_API_KEY is gone from os.environ here
                    assert "OPENAI_API_KEY" not in os.environ
                    await asyncio.sleep(0.01)  # Simulate SDK call time
                    results["claude_during"] = "OPENAI_API_KEY" not in os.environ

            async def codex_task():
                """Simulate a Codex request that builds env concurrently."""
                await asyncio.sleep(0.005)  # Start slightly after Claude
                env = codex._build_env()
                results["codex_has_key"] = "OPENAI_API_KEY" in env
                results["codex_key_value"] = env.get("OPENAI_API_KEY")

            await asyncio.gather(claude_task(), codex_task())

            # Claude should have isolated OPENAI_API_KEY
            assert results["claude_during"] is True
            # Codex should still have its key from init-time capture
            assert results["codex_has_key"] is True
            assert results["codex_key_value"] == "sk-codex-key"

    def test_codex_captures_key_only_at_init(self):
        """Key changes after init don't affect CodexCLI._api_key."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-original", "PATH": "/usr/bin"}):
            from src.codex_cli import CodexCLI

            with patch.object(CodexCLI, "_find_codex_binary", return_value="/usr/bin/codex"):
                cli = CodexCLI(cwd="/tmp")

            assert cli._api_key == "sk-original"

        # Even if env changes after init, captured key remains
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-changed", "PATH": "/usr/bin"}):
            assert cli._api_key == "sk-original"
            env = cli._build_env()
            assert env["OPENAI_API_KEY"] == "sk-original"
