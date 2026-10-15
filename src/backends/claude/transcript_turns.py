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
from pathlib import PurePosixPath
from typing import Iterable

# Negative results; a count is >= 0. MISSING and UNREADABLE come from whoever
# opens the file (no such file / not a readable regular file, too large, I/O
# error), MISMATCH and CORRUPT from the content.
TRANSCRIPT_MISSING = -1
TRANSCRIPT_MISMATCH = -2
TRANSCRIPT_CORRUPT = -3
TRANSCRIPT_UNREADABLE = -4


def count_transcript_turns(lines: Iterable[str], expected_cwd: str) -> int:
    """The external user turns in *lines*, or a negative code.

    * :data:`TRANSCRIPT_CORRUPT` — a line that is not a JSON object (a blank
      line included: the CLI writes none), or a user line whose ``message`` is
      not an object. Refuse to guess.
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
        try:
            line = json.loads(raw)
        except ValueError:  # JSONDecodeError is a ValueError
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
