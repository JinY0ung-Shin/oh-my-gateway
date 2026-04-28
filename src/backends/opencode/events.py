"""OpenCode event conversion helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OpenCodeEventConverter:
    """Convert OpenCode session events into gateway backend chunks."""

    session_id: str
    text_by_part: Dict[str, str] = field(default_factory=dict)
    text_parts: List[str] = field(default_factory=list)
    emitted_tool_uses: set[str] = field(default_factory=set)
    emitted_tool_results: set[str] = field(default_factory=set)
    usage: Optional[Dict[str, int]] = None
    saw_activity: bool = False

    def convert(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Convert one OpenCode event into zero or more backend chunks."""
        if self._event_session_id(event) != self.session_id:
            return []

        chunks: List[Dict[str, Any]] = []
        self._convert_usage_event(event)
        text_chunk = self._convert_text_event(event)
        if text_chunk:
            chunks.append(text_chunk)
        chunks.extend(self._convert_tool_event(event))
        return chunks

    def final_text(self) -> str:
        """Return accumulated assistant text for the streamed turn."""
        return "".join(self.text_parts)

    def finished(self, event: Dict[str, Any]) -> bool:
        """Return whether this event marks the active OpenCode session idle."""
        return (
            event.get("type") == "session.idle"
            and self._event_session_id(event) == self.session_id
            and self.saw_activity
        )

    def error_message(self, event: Dict[str, Any]) -> Optional[str]:
        """Extract an OpenCode session error message when it applies here."""
        if event.get("type") != "session.error":
            return None
        event_session = self._event_session_id(event)
        if event_session not in (None, self.session_id):
            return None
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        error = props.get("error") or props.get("message") or props
        return str(error)

    def _event_session_id(self, event: Dict[str, Any]) -> Optional[str]:
        props = event.get("properties")
        if not isinstance(props, dict):
            return None
        if isinstance(props.get("sessionID"), str):
            return props["sessionID"]
        part = props.get("part")
        if isinstance(part, dict) and isinstance(part.get("sessionID"), str):
            return part["sessionID"]
        return None

    def _text_delta_chunk(self, delta: str) -> Dict[str, Any]:
        return {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": delta},
            },
        }

    def _convert_text_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        event_type = event.get("type")
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}

        if event_type == "message.part.delta":
            if props.get("field") not in (None, "text"):
                return None
            delta = props.get("delta")
            if not isinstance(delta, str) or not delta:
                return None
            part_id = str(props.get("partID") or props.get("partId") or "")
            if part_id:
                self.text_by_part[part_id] = self.text_by_part.get(part_id, "") + delta
            self.text_parts.append(delta)
            self.saw_activity = True
            return self._text_delta_chunk(delta)

        if event_type != "message.part.updated":
            return None

        delta = props.get("delta")
        part = props.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            return None

        part_id = str(part.get("id") or "")
        if isinstance(delta, str) and delta:
            if part_id:
                self.text_by_part[part_id] = self.text_by_part.get(part_id, "") + delta
            self.text_parts.append(delta)
            self.saw_activity = True
            return self._text_delta_chunk(delta)

        text = part.get("text")
        if not isinstance(text, str) or not text:
            return None
        previous = self.text_by_part.get(part_id, "") if part_id else ""
        if previous and text.startswith(previous):
            computed_delta = text[len(previous) :]
        elif text != previous:
            computed_delta = text
        else:
            computed_delta = ""
        if part_id:
            self.text_by_part[part_id] = text
        if not computed_delta:
            return None
        self.text_parts.append(computed_delta)
        self.saw_activity = True
        return self._text_delta_chunk(computed_delta)

    def _convert_usage_event(self, event: Dict[str, Any]) -> None:
        if event.get("type") != "message.part.updated":
            return
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        part = props.get("part")
        if not isinstance(part, dict) or part.get("type") != "step-finish":
            return
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            return
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        input_tokens = int(tokens.get("input") or 0)
        input_tokens += int(cache.get("read") or 0)
        input_tokens += int(cache.get("write") or 0)
        output_tokens = int(tokens.get("output") or 0)
        reasoning_tokens = int(tokens.get("reasoning") or 0)
        self.usage = {
            "input_tokens": (self.usage or {}).get("input_tokens", 0) + input_tokens,
            "output_tokens": (self.usage or {}).get("output_tokens", 0) + output_tokens,
            "total_tokens": (
                (self.usage or {}).get("total_tokens", 0)
                + input_tokens
                + output_tokens
                + reasoning_tokens
            ),
        }
        self.saw_activity = True

    def _convert_tool_event(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        if event.get("type") != "message.part.updated":
            return []
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        part = props.get("part")
        if not isinstance(part, dict) or part.get("type") != "tool":
            return []

        tool_state = part.get("state")
        if not isinstance(tool_state, dict):
            return []
        call_id = str(part.get("callID") or part.get("callId") or part.get("id") or "")
        if not call_id:
            return []
        status = tool_state.get("status")
        chunks: List[Dict[str, Any]] = []

        input_value = tool_state.get("input")
        has_input = bool(input_value)
        should_emit_use = (
            status in ("running", "completed", "error")
            or (status == "pending" and has_input)
        )
        if should_emit_use and call_id not in self.emitted_tool_uses:
            chunks.append(
                {
                    "type": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": str(part.get("tool") or "unknown"),
                            "input": input_value or {},
                        }
                    ],
                }
            )
            self.emitted_tool_uses.add(call_id)
            self.saw_activity = True

        if status in ("completed", "error") and call_id not in self.emitted_tool_results:
            is_error = status == "error"
            content = tool_state.get("error") if is_error else tool_state.get("output")
            chunks.append(
                {
                    "type": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": "" if content is None else str(content),
                            "is_error": is_error,
                        }
                    ],
                }
            )
            self.emitted_tool_results.add(call_id)
            self.saw_activity = True

        return chunks
