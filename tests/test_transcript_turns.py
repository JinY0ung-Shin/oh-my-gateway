"""``count_transcript_turns`` and the broker's copy of it agree (design §3.6).

Under ``CLAUDE_ISOLATION_MODE=uid`` the broker reads a transcript as the session
uid and returns only this count, and it must not import the gateway tree, so
``docker/isolation_broker.py`` carries a verbatim copy of the rule in
``src/backends/claude/transcript_turns.py`` — the counter, ``bounded_lines``,
``too_many_values`` and their constants. These tests keep the two equal: the
functions' source text and the constants, and a corpus of the CLI's line
shapes fed to both through the same text-mode file iteration
(``encoding="utf-8"``, ``newline=None``, each copy's ``bounded_lines``), and
through the broker's whole read path (``_read_transcript``, in-process as this
test's uid) as well.
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
    # Deeper than the JSON parser's recursion limit: RecursionError, not a
    # ValueError, and still no transcript line.
    ("a line nested too deeply", _text(PROMPT) + b"[" * 100000 + b"\n", -3),
]


def _evaluate(
    rule, path: Path, cwd: str = _CWD, max_values=None, **bound
) -> int:
    """*rule*'s counter over the file as both callers read it: a text-mode
    iteration with universal newlines through the same copy's
    ``bounded_lines`` (*bound*: its ``max_line``), where a UnicodeDecodeError
    while iterating means CORRUPT (the contract in ``transcript_turns``).
    *max_values*: the counter's value bound, when not the default."""
    values = {} if max_values is None else {"max_values": max_values}
    with open(path, "r", encoding="utf-8", newline=None) as fh:
        try:
            return rule.count_transcript_turns(
                rule.bounded_lines(fh, **bound), cwd, **values
            )
        except UnicodeDecodeError:
            return gateway.TRANSCRIPT_CORRUPT


@pytest.mark.parametrize(
    "name", ["count_transcript_turns", "bounded_lines", "too_many_values"]
)
def test_the_copy_is_verbatim(broker, name):
    copy = inspect.getsource(getattr(broker, name))
    assert copy == inspect.getsource(getattr(gateway, name))


def test_the_constants_match(broker):
    for name in (
        "TRANSCRIPT_MISSING",
        "TRANSCRIPT_MISMATCH",
        "TRANSCRIPT_CORRUPT",
        "TRANSCRIPT_UNREADABLE",
        "MAX_TRANSCRIPT_BYTES",
        "MAX_LINE_CHARS",
        "MAX_LINE_VALUES",
    ):
        assert getattr(broker, name) == getattr(gateway, name), name
    assert broker._VALUE_CHARS.pattern == gateway._VALUE_CHARS.pattern
    assert broker._scanstring is gateway._scanstring is json.decoder.scanstring


@pytest.mark.parametrize(
    "data, expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_both_copies_agree(broker, tmp_path, data, expected):
    path = tmp_path / "s.jsonl"
    path.write_bytes(data)
    assert _evaluate(gateway, path) == expected
    assert _evaluate(broker, path) == expected
    # And the broker's whole read: the nofollow walk, the open, the checks.
    assert broker._read_transcript(os.path.realpath(path), _CWD) == expected


def test_deep_nesting_reads_as_corrupt_in_both(broker, tmp_path):
    # json.loads raises RecursionError, not ValueError, on a line nested this
    # deep; the rule counts it as CORRUPT in both copies (the CLI writes no
    # such line), and so does the broker's read.
    path = tmp_path / "s.jsonl"
    path.write_bytes(_text(PROMPT) + b"[" * 100000 + b"]" * 100000 + b"\n")
    for rule in (gateway, broker):
        assert _evaluate(rule, path) == gateway.TRANSCRIPT_CORRUPT
    read = broker._read_transcript(os.path.realpath(path), _CWD)
    assert read == gateway.TRANSCRIPT_CORRUPT


@pytest.mark.parametrize(
    "tail, expected",
    [
        (b"x" * 4096 + b"\n", -3),  # a line over the bound: not read past it
        (b"x" * 4096, -3),  # the same without a newline (a sparse file's end)
        (_text(_user("y" * 20)), 2),  # well within the bound: counted
    ],
    ids=["over-long", "over-long-unterminated", "within"],
)
def test_a_line_over_the_bound_is_corrupt_in_both(broker, tmp_path, tail, expected):
    # A small explicit bound stands in for MAX_LINE_CHARS (64 Mi characters);
    # it still holds the CLI's own lines here (some 330 characters).
    path = tmp_path / "s.jsonl"
    path.write_bytes(_text(PROMPT) + tail)
    assert len(_text(PROMPT)) < 2048
    for rule in (gateway, broker):
        assert _evaluate(rule, path, max_line=2048) == expected, rule.__name__


def test_the_bound_stops_reading(broker):
    # Nothing after an over-long line is read, in either copy.
    for rule in (gateway, broker):
        reads = []

        class _File:
            def readline(self, limit):
                reads.append(limit)
                return "z" * limit  # an endless line

        assert list(rule.bounded_lines(_File(), max_line=8)) == [""]
        assert reads == [9]


# --- the value bound ---------------------------------------------------------
#
# Parsing builds an object per JSON value: a dense line takes many times its
# size in memory. A line that may hold more values than the bound is CORRUPT
# without being parsed; a long line of long strings (the CLI's own images and
# tool output) is not.

_SMALL = 64  # a value bound standing in for MAX_LINE_VALUES (1 Mi values)
_FILLER = "y" * 4096  # makes a line longer than _SMALL characters


def _tokens(line: str) -> int:
    """What the scanner counts for *line*: quotes of strings plus the
    brackets, braces, commas and colons outside them."""
    count = pos = 0
    while True:
        match = gateway._VALUE_CHARS.search(line, pos)
        if match is None:
            return count
        count += 1
        pos = match.end()
        if line[pos - 1] == '"':
            pos = json.decoder.scanstring(line, pos, True)[1]


@pytest.mark.parametrize(
    "tail, expected",
    [
        ("[" + "[]," * 100 + "[]]", -3),  # dense: refused unparsed
        (json.dumps(_user(_FILLER)), 2),  # long, but few values: counted
        ('"' + '\\"' * 3000, -3),  # an unterminated string full of escapes
        ('{"type": "user", "x": "\\q' + "z" * 4096 + '"}', -3),  # a bad escape
        ('{"type": "user", "x": "a\x01' + "z" * 4096 + '"}', -3),  # a control char
    ],
    ids=["dense", "long-string", "unterminated", "bad-escape", "control-char"],
)
def test_the_value_bound_in_both(broker, tmp_path, tail, expected):
    path = tmp_path / "s.jsonl"
    path.write_bytes(_text(PROMPT) + tail.encode() + b"\n")
    for rule in (gateway, broker):
        assert _evaluate(rule, path, max_values=_SMALL) == expected, rule.__name__


def test_the_bound_is_exact(broker):
    # A line of exactly the bound passes, one more value does not; a line no
    # longer than the bound is never scanned (it cannot hold more).
    line = '{"type": "user", "pad": "' + "p" * 200 + '", "x": [' + "0," * 12
    while _tokens(line + "0]}") < _SMALL:
        line += "0,"
    exact = line + "0]}"
    assert _tokens(exact) == _SMALL and len(exact) > _SMALL
    over = line + "0,0]}"
    for rule in (gateway, broker):
        assert rule.too_many_values(exact, _SMALL) is False
        assert rule.too_many_values(over, _SMALL) is True
        assert rule.too_many_values("[" * _SMALL, _SMALL) is False
        assert rule.count_transcript_turns([exact], _CWD, _SMALL) == 1
        assert rule.count_transcript_turns([over], _CWD, _SMALL) == -3


def test_the_default_bound(broker, tmp_path):
    # MAX_LINE_VALUES itself: a valid line just past it is CORRUPT unparsed,
    # one just within it is counted.
    limit = gateway.MAX_LINE_VALUES
    path = tmp_path / "s.jsonl"
    over = '{"type": "user", "x": [' + "0," * limit + "0]}\n"
    within = '{"type": "user", "x": [' + "0," * (limit - 8) + "0]}\n"
    for line, expected in ((over, -3), (within, 1)):
        path.write_text(line)
        assert _evaluate(gateway, path) == expected
        assert _evaluate(broker, path) == expected
        assert broker._read_transcript(os.path.realpath(path), _CWD) == expected
