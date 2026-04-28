"""Optional live smoke test for an external OpenCode server."""

import os

import pytest


pytestmark = pytest.mark.skipif(
    not os.getenv("OPENCODE_SMOKE_BASE_URL"),
    reason="OPENCODE_SMOKE_BASE_URL is required for live OpenCode smoke tests",
)


async def test_live_opencode_health_and_prompt(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", os.environ["OPENCODE_SMOKE_BASE_URL"])

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    assert await backend.verify() is True

    session = Session(session_id="opencode-smoke")
    client = await backend.create_client(
        session=session,
        model=os.getenv("OPENCODE_SMOKE_MODEL", "openai/gpt-5.5"),
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

    assert "smoke-ok" in (backend.parse_message(chunks) or "")
