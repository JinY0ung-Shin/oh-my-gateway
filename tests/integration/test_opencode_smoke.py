"""Optional live smoke test for managed OpenCode."""

import os

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("OPENCODE_SMOKE_ENABLED") != "1",
    reason="OPENCODE_SMOKE_ENABLED=1 is required for live OpenCode smoke tests",
)


async def test_live_opencode_health_and_prompt(monkeypatch):
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    chunks = []
    backend = OpenCodeClient()
    try:
        assert await backend.verify() is True

        session = Session(session_id="opencode-smoke")
        client = await backend.create_client(
            session=session,
            model=os.environ["OPENCODE_SMOKE_MODEL"],
            cwd=os.getenv("OPENCODE_SMOKE_CWD") or None,
        )
        chunks = [
            chunk
            async for chunk in backend.run_completion_with_client(
                client,
                "Reply with exactly: smoke-ok",
                session,
            )
        ]
    finally:
        backend.close()

    assert "smoke-ok" in (backend.parse_message(chunks) or "")
