"""``count_transcript_turns`` and the broker's copy of it agree (design §3.6).

Under ``CLAUDE_ISOLATION_MODE=uid`` the broker reads a transcript as the session
uid and returns only this count, and it must not import the gateway tree, so
``docker/isolation_broker.py`` carries a verbatim copy of the rule in
``src/backends/claude/transcript_turns.py``. These tests keep the two equal:
the function's source text and constants, and a corpus of the CLI's line shapes
fed to both through the same text-mode file iteration (``encoding="utf-8"``,
``newline=None``), and through the broker's whole read path
(``_read_transcript``, in-process as this test's uid) as well.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

from src.backends.claude import transcript_turns as gateway

_BROKER = Path(__file__).resolve().parents[1] / "docker" / "isolation_broker.py"
_CWD = "/data/workspaces/alice/claude"
_DROP = object()  # a field _line leaves out


@pytest.fixture(scope="module")
def broker():
    # The broker puts docker/ on sys.path when it is loaded; keep that local.
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("isolation_broker_tt", _BROKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


def _line(**fields) -> dict:
    """A transcript line with the fields CLI 2.1.283 writes on every turn."""
    line = {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": _CWD,
        "sessionId": "0b9f5c0e-4a8e-4c3f-9d1e-2f6c7a8b9c0d",
        "version": "2.1.283",
        "gitBranch": "",
        "uuid": "6f1d2c3b-0000-4000-8000-000000000001",
        "timestamp": "2026-10-15T00:00:00.000Z",
    }
    line.update(fields)
    return {k: v for k, v in line.items() if v is not _DROP}


def _user(content, **fields) -> dict:
    return _line(type="user", message={"role": "user", "content": content}, **fields)


PROMPT = _user("fix the failing test")
PROMPT_BLOCKS = _user([{"type": "text", "text": "and this one"}])
META = _user("<system-reminder>context</system-reminder>", isMeta=True)
TOOL_USE = _line(
    type="assistant",
    message={
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}],
    },
)
TOOL_RESULT = _user(
    [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a\nb"}],
    toolUseResult={"stdout": "a\nb", "stderr": "", "interrupted": False},
)
ANSWER = _line(
    type="assistant",
    message={"role": "assistant", "content": [{"type": "text", "text": "done"}]},
)
SUMMARY = {"type": "summary", "summary": "Test fix", "leafUuid": "u"}
SNAPSHOT = {
    "type": "file-history-snapshot",
    "messageId": "m",
    "snapshot": {"trackedFileBackups": {}},
    "isSnapshotUpdate": False,
}
COMPACT = _line(type="system", subtype="compact_boundary", content="Compacted")


def _text(*lines, end="\n", trailing=True) -> bytes:
    body = end.join(json.dumps(line, ensure_ascii=False) for line in lines)
    return (body + (end if trailing else "")).encode("utf-8")


ELSEWHERE = _line(type="assistant", cwd="/data/workspaces/bob/claude")

CASES = [
    (
        "a session",
        _text(
            SNAPSHOT,
            PROMPT,
            TOOL_USE,
            TOOL_RESULT,
            ANSWER,
            META,
            PROMPT_BLOCKS,
            ANSWER,
            SUMMARY,
            COMPACT,
        ),
        2,
    ),
    ("crlf line endings", _text(PROMPT, ANSWER, PROMPT, end="\r\n"), 2),
    ("cr line endings", _text(PROMPT, ANSWER, PROMPT, end="\r"), 2),
    ("u2028 inside a string", _text(_user("a\u2028b\u2029c\x85d")), 1),
    ("no trailing newline", _text(PROMPT, PROMPT, trailing=False), 2),
    ("an empty file", b"", 0),
    ("lines without cwd", _text(_user("x", cwd=_DROP), SUMMARY, SNAPSHOT), 1),
    ("an empty cwd", _text(_user("x", cwd="")), 1),
    ("a cwd that is no string", _text(_user("x", cwd=5)), 1),
    ("a cwd with a trailing slash", _text(_user("x", cwd=_CWD + "/")), 1),
    ("a cwd with a dot component", _text(_user("x", cwd=_CWD + "/.")), 1),
    ("a cwd with a double slash", _text(_user("x", cwd="/" + _CWD)), -2),
    ("a cwd with a dotdot", _text(_user("x", cwd=_CWD + "/../claude")), -2),
    ("another workspace", _text(PROMPT, ELSEWHERE), -2),
    ("a mismatch before corruption", _text(PROMPT, ELSEWHERE) + b"{\n", -2),
    ("corruption before a mismatch", _text(PROMPT) + b"{\n" + _text(ELSEWHERE), -3),
    ("a blank line", _text(PROMPT) + b"\n" + _text(PROMPT), -3),
    ("a trailing blank line", _text(PROMPT) + b"\n", -3),
    ("a whitespace line", _text(PROMPT) + b"   \n", -3),
    ("bad JSON", _text(PROMPT) + b'{"type": "user"\n', -3),
    ("a list line", b"[1, 2]\n", -3),
    ("a number line", b"5\n", -3),
    ("a string line", b'"user"\n', -3),
    ("a null line", b"null\n", -3),
    ("a NaN line", b"NaN\n", -3),
    ("a user message that is no object", _text(_line(type="user", message="x")), -3),
    (
        "a meta line's message is not looked at",
        _text(_line(type="user", isMeta=True, message="x")),
        0,
    ),
    (
        "an assistant message that is no object",
        _text(_line(type="assistant", message="x")),
        0,
    ),
    ("a user line without a message", _text(_line(type="user")), 1),
    ("an empty content list", _text(_user([])), 1),
    (
        "a tool result beside text",
        _text(
            _user([TOOL_RESULT["message"]["content"][0], {"type": "text", "text": "x"}])
        ),
        1,
    ),
    (
        "a tool result list with a non-object",
        _text(_user([{"type": "tool_result"}, "x"])),
        1,
    ),
    ("a truthy non-bool isMeta", _text(_user("x", isMeta=1)), 0),
    ("isMeta false", _text(_user("x", isMeta=False)), 1),
    ("a UTF-8 BOM", b"\xef\xbb\xbf" + _text(PROMPT), -3),
    ("duplicate keys: the last wins", b'{"type": "assistant", "type": "user"}\n', 1),
    (
        "an escaped lone surrogate",
        _text(PROMPT) + b'{"type": "user", "message": {"content": "\\ud800"}}\n',
        2,
    ),
    ("not UTF-8", _text(PROMPT) + b'{"type": "user", "x": "\xff"}\n', -3),
]


def _evaluate(fn, path: Path, cwd: str = _CWD) -> int:
    """*fn* over the file as both callers read it: a text-mode iteration with
    universal newlines, where a UnicodeDecodeError while iterating means
    CORRUPT (the contract in ``transcript_turns``)."""
    with open(path, "r", encoding="utf-8", newline=None) as lines:
        try:
            return fn(lines, cwd)
        except UnicodeDecodeError:
            return gateway.TRANSCRIPT_CORRUPT


def test_the_copy_is_verbatim(broker):
    assert inspect.getsource(broker.count_transcript_turns) == inspect.getsource(
        gateway.count_transcript_turns
    )
    for name in (
        "TRANSCRIPT_MISSING",
        "TRANSCRIPT_MISMATCH",
        "TRANSCRIPT_CORRUPT",
        "TRANSCRIPT_UNREADABLE",
    ):
        assert getattr(broker, name) == getattr(gateway, name), name


@pytest.mark.parametrize(
    "data, expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_both_copies_agree(broker, tmp_path, data, expected):
    path = tmp_path / "s.jsonl"
    path.write_bytes(data)
    assert _evaluate(gateway.count_transcript_turns, path) == expected
    assert _evaluate(broker.count_transcript_turns, path) == expected
    # And the broker's whole read: the nofollow walk, the open, the checks.
    assert broker._read_transcript(os.path.realpath(path), _CWD) == expected


def test_deep_nesting_raises_in_both_and_reads_as_corrupt(broker, tmp_path):
    # json.loads raises RecursionError, not ValueError, on a line nested this
    # deep, and the rule does not catch it: both copies raise alike. The
    # broker's read maps it to CORRUPT (the CLI writes no such line).
    path = tmp_path / "s.jsonl"
    path.write_bytes(_text(PROMPT) + b"[" * 100000 + b"]" * 100000 + b"\n")
    for fn in (gateway.count_transcript_turns, broker.count_transcript_turns):
        with pytest.raises(RecursionError):
            _evaluate(fn, path)
    read = broker._read_transcript(os.path.realpath(path), _CWD)
    assert read == gateway.TRANSCRIPT_CORRUPT
