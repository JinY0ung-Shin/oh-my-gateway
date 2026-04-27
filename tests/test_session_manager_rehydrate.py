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
    assert sess.provider_session_id == sid
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
