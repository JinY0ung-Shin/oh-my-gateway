"""Smoke test: a previous_response_id targeting a memory-evicted but on-disk
session resolves successfully via rehydration."""

from src import session_manager
from src.session_manager import session_manager as global_sm


def test_evicted_session_with_jsonl_rehydrates_via_get_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    cwd = "/home/mireiffe/world/oh-my-gateway/working_dir/u"
    sid = "rehydrate-me-001"
    encoded = session_manager._encode_cwd(cwd)
    jsonl = tmp_path / encoded / f"{sid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":"ok"}}\n'
        '{"type":"user","message":{"role":"user","content":"more"}}\n'
    )

    # Ensure global SessionManager has no in-memory entry.
    global_sm.sessions.pop(sid, None)

    got = global_sm.get_session(sid, user="u", cwd=cwd)
    assert got is not None
    assert got.turn_counter == 2
    assert sid in global_sm.sessions  # promoted to cache
