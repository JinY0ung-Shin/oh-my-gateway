"""Collab-tool-call JSON stripping for text content and streaming deltas."""

import json
import re


def strip_collab_json(text: str) -> str:
    """Remove collab_tool_call JSON blocks from text content.

    Uses a string-aware brace counter so
    that braces inside JSON string values are not misinterpreted.
    """
    plain_parts: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            j = i
            in_string = False
            escape_next = False
            while j < len(text):
                ch = text[j]
                if escape_next:
                    escape_next = False
                elif ch == "\\" and in_string:
                    escape_next = True
                elif ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            block = text[i : j + 1] if j < len(text) else text[i:]
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict) and (
                    "collab_tool_call" in parsed or parsed.get("type") == "collab_tool_call"
                ):
                    # Valid collab JSON — strip it
                    i = j + 1
                    continue
            except json.JSONDecodeError:
                pass
            plain_parts.append(block)
            i = j + 1
            continue
        plain_parts.append(text[i])
        i += 1
    cleaned = "".join(plain_parts)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class CollabJsonStreamFilter:
    """Streaming filter that strips collab_tool_call JSON from text deltas.

    Token-level streaming delivers text character by character across many
    deltas.  When a ``{`` is encountered, this filter buffers subsequent
    characters until it can determine whether the block is a collab_tool_call
    JSON object.  Non-collab content is flushed with minimal delay.
    """

    _COLLAB_MARKERS = ('"collab_tool_call"', '"collab_tool"')
    _MAX_BUFFER = 8192

    def __init__(self):
        self._buf = ""
        self._depth = 0
        self._in_string = False
        self._escape_next = False

    @property
    def buffering(self) -> bool:
        return bool(self._buf)

    def feed(self, text: str) -> str:
        """Process a text delta, returning cleaned text (collab JSON removed)."""
        output: list[str] = []

        for ch in text:
            if self._buf:
                self._buf += ch
                if self._escape_next:
                    self._escape_next = False
                elif ch == "\\" and self._in_string:
                    self._escape_next = True
                elif ch == '"':
                    self._in_string = not self._in_string
                elif not self._in_string:
                    if ch == "{":
                        self._depth += 1
                    elif ch == "}":
                        self._depth -= 1
                        if self._depth == 0:
                            if any(m in self._buf for m in self._COLLAB_MARKERS):
                                try:
                                    parsed = json.loads(self._buf)
                                    if isinstance(parsed, dict) and (
                                        "collab_tool_call" in parsed
                                        or parsed.get("type") == "collab_tool_call"
                                    ):
                                        # Valid collab — drop it
                                        self._reset()
                                        continue
                                except json.JSONDecodeError:
                                    pass
                            output.append(self._buf)
                            self._reset()
                            continue
                # Safety limit: flush if buffer grows unreasonably large
                if len(self._buf) > self._MAX_BUFFER:
                    output.append(self._buf)
                    self._reset()
            else:
                if ch == "{":
                    self._buf = ch
                    self._depth = 1
                    self._in_string = False
                    self._escape_next = False
                else:
                    output.append(ch)

        return "".join(output)

    def flush(self) -> str:
        """Return any remaining buffered text at stream end."""
        result = self._buf
        self._reset()
        return result

    def _reset(self):
        self._buf = ""
        self._depth = 0
        self._in_string = False
        self._escape_next = False
