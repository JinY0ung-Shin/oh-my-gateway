"""Tests for jsonl rehydrate helpers in session_manager."""

from pathlib import Path

import pytest

from src.session_manager import _encode_cwd


def test_encode_cwd_replaces_slash_underscore_dot_with_dash():
    assert (
        _encode_cwd("/home/mireiffe/world/claude-code-gateway/working_dir/se91.kim")
        == "-home-mireiffe-world-claude-code-gateway-working-dir-se91-kim"
    )


def test_encode_cwd_path_object_supported():
    assert (
        _encode_cwd(Path("/x/y_z/q.r"))
        == "-x-y-z-q-r"
    )


def test_encode_cwd_handles_repeated_separators():
    assert _encode_cwd("/_./") == "----"


import json


def _write_jsonl(p: Path, lines: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")


def test_try_rehydrate_returns_none_when_file_missing(tmp_path, monkeypatch):
    from src import session_manager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    result = session_manager._try_rehydrate_from_jsonl(
        "missing-sid", user="u", cwd="/some/cwd"
    )
    assert result is None


def test_try_rehydrate_reconstructs_session(tmp_path, monkeypatch):
    from src import session_manager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    cwd = "/x/y_z"
    encoded = session_manager._encode_cwd(cwd)
    sid = "abc-123"
    jsonl = tmp_path / encoded / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "queue-operation"},
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
            {"type": "user", "message": {"role": "user", "content": "more"}},
        ],
    )

    sess = session_manager._try_rehydrate_from_jsonl(sid, user="u", cwd=cwd)
    assert sess is not None
    assert sess.session_id == sid
    assert sess.workspace == cwd
    assert sess.user == "u"
    assert sess.turn_counter == 2  # 2 user-role lines
    assert sess.messages == []  # in-memory bookkeeping intentionally empty


def test_try_rehydrate_returns_none_on_corrupt_file(tmp_path, monkeypatch):
    from src import session_manager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    cwd = "/x/y"
    encoded = session_manager._encode_cwd(cwd)
    sid = "bad"
    jsonl = tmp_path / encoded / f"{sid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text("not-json-at-all\n")

    sess = session_manager._try_rehydrate_from_jsonl(sid, user="u", cwd=cwd)
    assert sess is None


def test_get_session_returns_in_memory_hit_unchanged(tmp_path, monkeypatch):
    from src.session_manager import SessionManager, Session

    sm = SessionManager()
    sm.sessions["sid-1"] = Session(
        session_id="sid-1", user="u", workspace="/x"
    )
    got = sm.get_session("sid-1")
    assert got is not None
    assert got.session_id == "sid-1"


def test_get_session_rehydrates_when_disk_only(tmp_path, monkeypatch):
    from src import session_manager
    from src.session_manager import SessionManager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    cwd = "/x/y"
    sid = "disk-1"
    jsonl = tmp_path / session_manager._encode_cwd(cwd) / f"{sid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n'
    )

    sm = SessionManager()
    got = sm.get_session(sid, user="u", cwd=cwd)
    assert got is not None
    assert got.session_id == sid
    assert got.turn_counter == 1
    assert sid in sm.sessions  # cached now


def test_get_session_returns_none_when_neither_memory_nor_disk(tmp_path, monkeypatch):
    from src import session_manager
    from src.session_manager import SessionManager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    sm = SessionManager()
    assert sm.get_session("nope", user="u", cwd="/x") is None


def test_rehydrate_misses_only_counts_real_attempts(tmp_path, monkeypatch):
    """The miss counter should reflect rehydrate failures, not generic
    cache misses. A get_session call without user/cwd cannot rehydrate at
    all, so it must not bump the miss counter."""
    from src import session_manager
    from src.session_manager import SessionManager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    sm = SessionManager()

    # No user/cwd → rehydrate not even attempted.
    assert sm.get_session("absent") is None
    assert sm._rehydrate_misses == 0

    # Only user provided → still cannot attempt.
    assert sm.get_session("absent", user="u") is None
    assert sm._rehydrate_misses == 0

    # Only cwd provided → still cannot attempt.
    assert sm.get_session("absent", cwd="/x") is None
    assert sm._rehydrate_misses == 0

    # Both provided AND no jsonl on disk → real miss.
    assert sm.get_session("absent", user="u", cwd="/x") is None
    assert sm._rehydrate_misses == 1


def test_session_jsonl_path_constructs_expected_path(tmp_path, monkeypatch):
    from src import session_manager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    p = session_manager._session_jsonl_path("abc", "/x/y_z")
    encoded = session_manager._encode_cwd("/x/y_z")
    assert p == tmp_path / encoded / "abc.jsonl"


def test_session_jsonl_exists_returns_false_when_workspace_missing():
    from src import session_manager
    from src.session_manager import Session

    sess = Session(session_id="sid", workspace=None)
    assert session_manager._session_jsonl_exists(sess) is False


def test_try_rehydrate_skips_tool_result_user_entries(tmp_path, monkeypatch):
    """Claude jsonl records tool_results as top-level type=user with
    content=[{"type":"tool_result", ...}]. Those must not bump turn_counter
    or the gateway will stamp follow-ups with a future turn id."""
    from src import session_manager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    cwd = "/x/y"
    encoded = session_manager._encode_cwd(cwd)
    sid = "tool-1"
    jsonl = tmp_path / encoded / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            # One real external user prompt
            {"type": "user", "message": {"role": "user", "content": "Use the tool"}},
            # Assistant decides to call a tool
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use"}]}},
            # Tool result — recorded as type=user but should NOT count as a turn
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "abc", "content": "ok"}],
                },
            },
            # Final assistant answer
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
        ],
    )

    sess = session_manager._try_rehydrate_from_jsonl(sid, user="u", cwd=cwd)
    assert sess is not None
    assert sess.turn_counter == 1, (
        f"expected 1 user turn, got {sess.turn_counter} (tool_result counted)"
    )


def test_try_rehydrate_skips_isMeta_user_entries(tmp_path, monkeypatch):
    """Meta user entries (system reminders etc.) are not real turns."""
    from src import session_manager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    cwd = "/x/y"
    encoded = session_manager._encode_cwd(cwd)
    sid = "meta-1"
    jsonl = tmp_path / encoded / f"{sid}.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<system>"}},
            {"type": "user", "message": {"role": "user", "content": "real prompt"}},
        ],
    )

    sess = session_manager._try_rehydrate_from_jsonl(sid, user="u", cwd=cwd)
    assert sess is not None
    assert sess.turn_counter == 1


def test_session_jsonl_exists_reflects_filesystem(tmp_path, monkeypatch):
    from src import session_manager
    from src.session_manager import Session

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    cwd = "/x/y"
    encoded = session_manager._encode_cwd(cwd)
    sid = "sid-1"

    sess = Session(session_id=sid, workspace=cwd)
    assert session_manager._session_jsonl_exists(sess) is False

    target = tmp_path / encoded / f"{sid}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n")
    assert session_manager._session_jsonl_exists(sess) is True
