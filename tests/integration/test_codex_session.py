"""Integration tests for Codex session management.

Tests backend dispatch, thread_id persistence, concurrent lock contention,
backend invariant enforcement, TTL expiry, and end-to-end HTTP session flow.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import patch

import httpx
import pytest

import src.main as main
from src.backend_registry import BackendRegistry, resolve_model
from src.models import Message
from src.rate_limiter import limiter as _global_limiter

pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset slowapi in-memory storage so HTTP tests don't hit rate limits."""
    if _global_limiter is not None:
        try:
            _global_limiter.reset()
        except Exception:
            pass
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClaudeBackend:
    """Minimal BackendClient for Claude, used in dispatch and invariant tests."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        self.calls.append({"prompt": prompt, "resume": resume, "session_id": session_id})
        yield {"type": "assistant", "content": [{"type": "text", "text": "claude reply"}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": "claude reply",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        return "claude reply"

    def estimate_token_usage(
        self, prompt: str, completion: str, model: Optional[str] = None
    ) -> Dict[str, int]:
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def verify(self) -> bool:
        return True


class FailingCodexBackend:
    """A Codex backend that raises on run_completion after emitting codex_session."""

    def __init__(self, thread_id: str = "fail-thread"):
        self.thread_id = thread_id
        self.calls: List[Dict[str, Any]] = []
        self.should_fail = False

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        self.calls.append({"prompt": prompt, "resume": resume, "session_id": session_id})
        if self.should_fail:
            raise RuntimeError("Codex backend exploded")
        yield {"type": "codex_session", "session_id": self.thread_id}
        yield {"type": "assistant", "content": [{"type": "text", "text": "codex reply"}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": "codex reply",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        return "codex reply"

    def estimate_token_usage(
        self, prompt: str, completion: str, model: Optional[str] = None
    ) -> Dict[str, int]:
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def verify(self) -> bool:
        return True


class SlowCodexBackend:
    """Codex backend with configurable delay for concurrency testing.

    Records execution order via a shared list so callers can verify
    that requests holding the per-session lock are serialized.
    """

    def __init__(
        self,
        thread_id: str = "slow-thread",
        delay: float = 0.1,
        order_log: Optional[List[str]] = None,
    ):
        self.thread_id = thread_id
        self.delay = delay
        self.order_log: List[str] = order_log if order_log is not None else []
        self.calls: List[Dict[str, Any]] = []
        self.should_fail_prompt: Optional[str] = None

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        self.calls.append({"prompt": prompt, "resume": resume, "session_id": session_id})
        self.order_log.append(f"start:{prompt}")

        if self.should_fail_prompt and self.should_fail_prompt in prompt:
            self.order_log.append(f"fail:{prompt}")
            raise RuntimeError("Deliberate backend failure")

        await asyncio.sleep(self.delay)
        self.order_log.append(f"end:{prompt}")

        yield {"type": "codex_session", "session_id": self.thread_id}
        yield {"type": "assistant", "content": [{"type": "text", "text": f"reply to {prompt}"}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": f"reply to {prompt}",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("type") == "assistant":
                content = m.get("content", [])
                if isinstance(content, list) and content:
                    return content[0].get("text", "")
        return "slow reply"

    def estimate_token_usage(
        self, prompt: str, completion: str, model: Optional[str] = None
    ) -> Dict[str, int]:
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def verify(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBackendDispatchViaRegistry:
    """Test that resolve_model and BackendRegistry dispatch correctly."""

    def test_resolve_codex_model(self, fake_codex_backend):
        resolved = resolve_model("codex")
        assert resolved.backend == "codex"
        assert BackendRegistry.get("codex") is fake_codex_backend

    def test_resolve_claude_model(self, fake_codex_backend):
        claude = FakeClaudeBackend()
        BackendRegistry.register("claude", claude)

        resolved = resolve_model("sonnet")
        assert resolved.backend == "claude"
        assert BackendRegistry.get("claude") is claude

    def test_both_backends_coexist(self, fake_codex_backend):
        claude = FakeClaudeBackend()
        BackendRegistry.register("claude", claude)

        codex_resolved = resolve_model("codex")
        claude_resolved = resolve_model("sonnet")

        assert codex_resolved.backend == "codex"
        assert claude_resolved.backend == "claude"
        assert BackendRegistry.get("codex") is fake_codex_backend
        assert BackendRegistry.get("claude") is claude


class TestCodexThreadIdPersistence:
    """Test that Codex thread_id is captured and used for resume."""

    async def test_turn1_captures_provider_session_id(
        self, isolated_session_manager, fake_codex_backend
    ):
        manager = isolated_session_manager
        session = manager.get_or_create_session("codex-persist-1")
        session.backend = "codex"

        # Simulate turn 1: collect chunks from fake backend
        chunks = []
        async for chunk in fake_codex_backend.run_completion(
            prompt="hello", session_id="codex-persist-1"
        ):
            chunks.append(chunk)
            # Capture codex_session event like _capture_provider_session_id does
            if chunk.get("type") == "codex_session":
                session.provider_session_id = chunk["session_id"]

        assert session.provider_session_id == "fake-thread-123"

    async def test_turn2_passes_resume(self, isolated_session_manager, fake_codex_backend):
        manager = isolated_session_manager
        session = manager.get_or_create_session("codex-persist-2")
        session.backend = "codex"
        session.provider_session_id = "fake-thread-123"
        session.add_messages([Message(role="user", content="first turn")])

        # Turn 2: resume should be the provider_session_id
        resume_id = session.provider_session_id
        async for _ in fake_codex_backend.run_completion(prompt="second turn", resume=resume_id):
            pass

        assert len(fake_codex_backend.calls) == 1
        assert fake_codex_backend.calls[0]["resume"] == "fake-thread-123"

    async def test_failure_preserves_thread_id(self, isolated_session_manager):
        manager = isolated_session_manager
        session = manager.get_or_create_session("codex-persist-3")
        session.backend = "codex"

        # Turn 1 succeeds, thread_id captured
        failing_backend = FailingCodexBackend(thread_id="thread-survive")
        chunks = []
        async for chunk in failing_backend.run_completion(prompt="turn1"):
            chunks.append(chunk)
            if chunk.get("type") == "codex_session":
                session.provider_session_id = chunk["session_id"]

        assert session.provider_session_id == "thread-survive"

        # Turn 2 fails
        failing_backend.should_fail = True
        with pytest.raises(RuntimeError, match="exploded"):
            async for _ in failing_backend.run_completion(prompt="turn2"):
                pass

        # thread_id survives the failure
        assert session.provider_session_id == "thread-survive"


class TestConcurrentLockContention:
    """Test per-session lock serialization through the HTTP gateway."""

    async def test_concurrent_requests_are_serialized(self, isolated_session_manager):
        """3 concurrent HTTP requests to the same session are serialized by the lock."""
        order_log: List[str] = []
        codex = SlowCodexBackend(thread_id="lock-thread", delay=0.1, order_log=order_log)
        BackendRegistry.register("codex", codex)
        BackendRegistry.register("claude", FakeClaudeBackend())

        with (
            patch.object(main, "runtime_api_key", None),
            patch("src.main.verify_api_key", return_value=True),
            patch("src.main._validate_backend_auth"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:

                async def send_request(msg: str):
                    return await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "codex",
                            "messages": [{"role": "user", "content": msg}],
                            "session_id": "lock-serial-test",
                            "stream": False,
                        },
                    )

                # Fire 3 concurrent requests to the same session
                results = await asyncio.gather(
                    send_request("req-1"),
                    send_request("req-2"),
                    send_request("req-3"),
                )

                # All 3 must succeed
                for resp in results:
                    assert resp.status_code == 200

                # Backend was called 3 times
                assert len(codex.calls) == 3

                # Serialization proof: each "start:X" must be followed by "end:X"
                # before the next "start:Y" — no interleaving.
                starts = [e for e in order_log if e.startswith("start:")]
                ends = [e for e in order_log if e.startswith("end:")]
                assert len(starts) == 3
                assert len(ends) == 3

                # Verify strict start-end-start-end ordering
                se_events = [e for e in order_log if e.startswith(("start:", "end:"))]
                for i in range(0, len(se_events) - 1, 2):
                    tag = se_events[i].split(":")[1]
                    assert se_events[i] == f"start:{tag}"
                    assert se_events[i + 1] == f"end:{tag}"

    async def test_lock_released_after_failure(self, isolated_session_manager):
        """After one request fails, subsequent requests still acquire the lock."""
        order_log: List[str] = []
        codex = SlowCodexBackend(thread_id="lock-fail-thread", delay=0.05, order_log=order_log)
        codex.should_fail_prompt = "fail-me"
        BackendRegistry.register("codex", codex)
        BackendRegistry.register("claude", FakeClaudeBackend())

        with (
            patch.object(main, "runtime_api_key", None),
            patch("src.main.verify_api_key", return_value=True),
            patch("src.main._validate_backend_auth"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                # First request: creates the session (succeeds)
                resp1 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "setup"}],
                        "session_id": "lock-fail-session",
                        "stream": False,
                    },
                )
                assert resp1.status_code == 200

                # Second request: triggers backend failure
                resp2 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "fail-me"}],
                        "session_id": "lock-fail-session",
                        "stream": False,
                    },
                )
                assert resp2.status_code == 500

                # Third request: must succeed — lock was released despite failure
                resp3 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "recover"}],
                        "session_id": "lock-fail-session",
                        "stream": False,
                    },
                )
                assert resp3.status_code == 200


class TestBackendInvariantApiLevel:
    """Test that switching backends within a session returns HTTP 400."""

    async def test_backend_mismatch_returns_400(self, isolated_session_manager):
        # Register both backends
        claude = FakeClaudeBackend()
        BackendRegistry.register("claude", claude)
        from tests.integration.conftest import FakeCodexBackend

        codex = FakeCodexBackend()
        BackendRegistry.register("codex", codex)

        with (
            patch.object(main, "runtime_api_key", None),
            patch("src.main.verify_api_key", return_value=True),
            patch("src.main._validate_backend_auth"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                # First request: create session with Claude backend
                resp1 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [{"role": "user", "content": "hello"}],
                        "session_id": "invariant-test",
                        "stream": False,
                    },
                )
                assert resp1.status_code == 200

                # Second request: try Codex model on same session → 400
                resp2 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "switch"}],
                        "session_id": "invariant-test",
                        "stream": False,
                    },
                )
                assert resp2.status_code == 400
                body = resp2.json()
                detail = body.get("detail") or body.get("error", {}).get("message", "")
                assert "Cannot mix backends" in detail

                # Third request: Claude again on same session still works
                resp3 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "sonnet",
                        "messages": [{"role": "user", "content": "recover"}],
                        "session_id": "invariant-test",
                        "stream": False,
                    },
                )
                assert resp3.status_code == 200


class TestSessionTtlWithCodex:
    """Test that expired Codex sessions are cleaned up properly."""

    async def test_expired_codex_session_is_cleaned(self, isolated_session_manager):
        manager = isolated_session_manager

        # Create session with very short TTL
        session = manager.get_or_create_session("ttl-codex")
        session.backend = "codex"
        session.provider_session_id = "thread-ttl-test"
        session.ttl_minutes = 0  # Effectively immediate expiry

        # Force expiry by setting expires_at in the past
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)

        # Run cleanup
        removed = await manager.cleanup_expired_sessions()
        assert removed == 1

        # Session is gone
        assert manager.get_session("ttl-codex") is None

        # New request creates a fresh session with defaults
        fresh = manager.get_or_create_session("ttl-codex")
        assert fresh.backend == "claude"  # Default backend
        assert fresh.provider_session_id is None
        assert len(fresh.messages) == 0


class TestE2eHttpSessionFlow:
    """End-to-end HTTP test: two sequential requests with Codex backend."""

    async def test_two_turn_codex_session(self, isolated_session_manager):
        from tests.integration.conftest import FakeCodexBackend

        codex = FakeCodexBackend(thread_id="e2e-thread-42")
        BackendRegistry.register("codex", codex)
        claude = FakeClaudeBackend()
        BackendRegistry.register("claude", claude)

        with (
            patch.object(main, "runtime_api_key", None),
            patch("src.main.verify_api_key", return_value=True),
            patch("src.main._validate_backend_auth"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                # Turn 1
                resp1 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "first turn"}],
                        "session_id": "e2e-codex-session",
                        "stream": False,
                    },
                )
                assert resp1.status_code == 200
                data1 = resp1.json()
                assert data1["choices"][0]["message"]["content"]

                # Verify session state after turn 1
                session = isolated_session_manager.get_session("e2e-codex-session")
                assert session is not None
                assert session.backend == "codex"
                assert session.provider_session_id == "e2e-thread-42"

                # Turn 2
                resp2 = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "second turn"}],
                        "session_id": "e2e-codex-session",
                        "stream": False,
                    },
                )
                assert resp2.status_code == 200

                # Verify resume was passed in turn 2
                assert len(codex.calls) == 2
                assert codex.calls[0]["session_id"] == "e2e-codex-session"
                assert codex.calls[0]["resume"] is None
                assert codex.calls[1]["resume"] == "e2e-thread-42"
                assert codex.calls[1]["session_id"] is None
