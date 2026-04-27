"""End-to-end: rehydrate from disk → new persistent client → SDK gets --resume."""

import json

import pytest


@pytest.mark.asyncio
async def test_rehydrated_session_resumes_sdk(tmp_path, monkeypatch):
    """A session loaded from on-disk jsonl spawns ClaudeSDKClient with options.resume.

    This is the round-trip the merge enables: when a request arrives with a
    previous_response_id whose in-memory session has expired, the gateway
    rehydrates from ``~/.claude/projects/<encoded>/<session_id>.jsonl`` and
    then creates a new persistent SDK client.  The merge ensures that client
    is opened with ``--resume <session_id>`` rather than starting a fresh
    SDK conversation.
    """
    from src import session_manager
    from src.backends.claude import client as claude_client_mod

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)

    sid = "11111111-2222-3333-4444-555555555555"
    cwd_dir = tmp_path / "integration-ws"
    cwd_dir.mkdir()
    cwd = str(cwd_dir)
    target = session_manager._session_jsonl_path(sid, cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w") as fh:
        fh.write(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
        )
        fh.write(
            json.dumps(
                {"type": "assistant", "message": {"role": "assistant", "content": "hello"}}
            )
            + "\n"
        )

    sess = session_manager._try_rehydrate_from_jsonl(sid, user="u", cwd=cwd)
    assert sess is not None
    assert sess.session_id == sid
    assert sess.turn_counter == 1

    captured: dict = {}

    class FakeSDKClient:
        def __init__(self, *, options):
            captured["session_id"] = options.session_id
            captured["resume"] = options.resume

        async def connect(self, prompt=None):
            return None

    monkeypatch.setattr(claude_client_mod, "ClaudeSDKClient", FakeSDKClient)

    cli = claude_client_mod.ClaudeCodeCLI(cwd=cwd)
    await cli.create_client(session=sess, cwd=cwd)

    assert captured["resume"] == sid
    assert captured["session_id"] is None
