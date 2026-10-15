"""Count the external user turns of a CLI session transcript (``<sid>.jsonl``).

Rehydrating a session that is not in memory needs two facts from its transcript:
that every line belongs to the requester's workspace (the CLI's ``projects/``
names are lossy, so another workspace's transcript can sit under the same name)
and how many gateway-issued turns it holds (``max_turn``). The gateway reads the
file itself when it may; under ``CLAUDE_ISOLATION_MODE=uid`` the session uid owns
it (mode 0600), and the broker reads it as that uid and returns only this count
(``docs/isolation-uid-ownership-design.md`` §3.6).

:func:`count_transcript_turns` is the one rule both use. The broker must not
import the gateway tree, so ``docker/isolation_broker.py`` carries a copy, kept
equal by a parity test over a shared corpus (``tests/test_transcript_turns.py``).
Change both together.
"""

from __future__ import annotations

import json
import re
from json.decoder import scanstring as _scanstring  # type: ignore[attr-defined]
from pathlib import PurePosixPath
from typing import Iterable, Iterator

# Negative results; a count is >= 0. MISSING and UNREADABLE come from whoever
# opens the file (no such file / not a readable regular file, too large, I/O
# error), MISMATCH and CORRUPT from the content.
TRANSCRIPT_MISSING = -1
TRANSCRIPT_MISMATCH = -2
TRANSCRIPT_CORRUPT = -3
TRANSCRIPT_UNREADABLE = -4

# Bounds on what a reader takes in. A transcript is session-writable (a session
# uid may replace its own with a sparse file holding one endless "line"), and
# both the gateway and the broker's helper read it into memory: the caller
# refuses a file larger than MAX_TRANSCRIPT_BYTES (UNREADABLE) and iterates
# through :func:`bounded_lines`, which turns a line longer than MAX_LINE_CHARS
# into CORRUPT instead of reading all of it. Parsing a line builds one object
# per JSON value (a dense line of small lists takes some 24 times its size), so
# :func:`count_transcript_turns` refuses a line that may hold more than
# MAX_LINE_VALUES values before parsing it (CORRUPT, :func:`too_many_values`).
# The CLI writes none of these: its long lines are long strings (images, tool
# output), and its tool output is capped well below the value bound.
MAX_TRANSCRIPT_BYTES = 1 << 30
MAX_LINE_CHARS = 64 << 20
MAX_LINE_VALUES = 1 << 20
# What :func:`too_many_values` counts: a string's opening quote, and the
# bracket, brace, comma or colon every other value follows. One character
# class: a regex repeating a group keeps a backtracking record per repetition
# (gigabytes for a line of escapes); strings are skipped by json's own scanner.
_VALUE_CHARS = re.compile(r'["\[{,:]')


def count_transcript_turns(
    lines: Iterable[str], expected_cwd: str, max_values: int = MAX_LINE_VALUES
) -> int:
    """The external user turns in *lines*, or a negative code.

    * :data:`TRANSCRIPT_CORRUPT` — a line that is not a JSON object (a blank
      line included: the CLI writes none), one that may hold more than
      *max_values* JSON values (:func:`too_many_values`, not parsed), or a user
      line whose ``message`` is not an object. Refuse to guess.
    * :data:`TRANSCRIPT_MISMATCH` — a line records a ``cwd`` other than
      *expected_cwd*: the transcript belongs to another workspace that maps to
      the same ``projects/`` name.
    * otherwise the number of ``type == "user"`` lines that are neither
      ``isMeta`` system reminders nor tool results (a non-empty ``content``
      list of only ``tool_result`` blocks): the prompts the gateway sent, one
      per turn.

    *lines* are text lines as a file yields them (trailing newline allowed).
    """
    expected = PurePosixPath(expected_cwd)
    turns = 0
    for raw in lines:
        if too_many_values(raw, max_values):
            return TRANSCRIPT_CORRUPT
        try:
            line = json.loads(raw)
        except (ValueError, RecursionError):  # JSONDecodeError is a ValueError;
            # a line nested deeper than the parser's recursion limit is no
            # transcript line either
            return TRANSCRIPT_CORRUPT
        if not isinstance(line, dict):
            return TRANSCRIPT_CORRUPT
        recorded = line.get("cwd")
        if (
            isinstance(recorded, str)
            and recorded
            and PurePosixPath(recorded) != expected
        ):
            return TRANSCRIPT_MISMATCH
        if line.get("type") != "user" or line.get("isMeta"):
            continue
        message = line.get("message", {})
        if not isinstance(message, dict):
            return TRANSCRIPT_CORRUPT
        content = message.get("content")
        if (
            isinstance(content, list)
            and content
            and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
        ):
            continue
        turns += 1
    return turns


def bounded_lines(fh, max_line: int = MAX_LINE_CHARS) -> Iterator[str]:
    """The lines of the text file *fh*, at most *max_line* characters each.

    A longer line is not read past the bound: it is yielded as ``""``, which
    :func:`count_transcript_turns` counts as CORRUPT, and iteration stops.
    Mirrored by ``docker/isolation_broker.py`` like the counter.
    """
    while True:
        line = fh.readline(max_line + 1)
        if not line:
            return
        if len(line) > max_line:
            yield ""
            return
        yield line


def too_many_values(line: str, limit: int = MAX_LINE_VALUES) -> bool:
    """Whether the JSON text *line* may hold more than *limit* values.

    An upper bound, counted without parsing: every value is a string or
    follows a bracket, brace, comma or colon (:data:`_VALUE_CHARS`), and a
    string is skipped with json's own C scanner, so the scan sees the strings
    ``json.loads`` sees, in constant memory. A line holds no more values than
    characters, so only a longer line is scanned, and only until the count
    passes *limit*. Also true for a line with a string ``json.loads`` refuses
    (unterminated, a bad escape, a control character): CORRUPT either way.
    Mirrored by ``docker/isolation_broker.py`` like the counter.
    """
    if len(line) <= limit:
        return False
    count = pos = 0
    while count <= limit:
        match = _VALUE_CHARS.search(line, pos)
        if match is None:
            return False
        count += 1
        pos = match.end()
        if line[pos - 1] == '"':
            try:
                pos = _scanstring(line, pos, True)[1]
            except ValueError:
                return True
    return True
