"""
title: Development Assistant (Completions)
author: claude-code-openai-wrapper
version: 0.1.0
description: .
    Session-aware chat completions pipe via /v1/chat/completions.
    Uses Open WebUI chat_id as session_id for conversation continuity and SDK auto-compaction.
    Features:
    - User context injection (mlm_username from metadata.user_id or user.name)
    - Credential fetching from Open WebUI API for MCP authentication
    - thought_wrapped mode: wraps thinking in <thought> tags and detects <response> tag
license: MIT
"""

import html
import json
import logging
import re
from typing import Iterator, Optional

import httpx

# Regex to detect SDK tool-execution noise that leaks into text deltas:
#   - Bare tool names like "mcp__mcp_router__cql", "Read", "Bash"
#   - "Executing tool_name..." status lines
_TOOL_NOISE_RE = re.compile(
    r"^(?:Executing\s+)?(?:mcp__\w+|Read|Bash|Write|Edit|Glob|Grep|WebFetch|WebSearch|"
    r"NotebookEdit|Agent|TodoWrite|Skill)(?:\.\.\.)?\s*$"
)


def _is_tool_noise(text: str) -> bool:
    """Return True if *text* is SDK tool-execution noise."""
    return bool(text) and _TOOL_NOISE_RE.match(text) is not None


def _safe_attr(value: str) -> str:
    """Sanitize a string for use inside a double-quoted HTML attribute.

    Open WebUI reads raw attribute values without decoding HTML entities,
    so we use plain character substitution instead of entity encoding.
    ``&`` is neutralised so pre-existing entities in Confluence content
    (e.g. ``&quot;``) cannot be decoded by the browser into ``"`` which
    would break the attribute boundary.
    """
    return (
        value
        .replace("&", "+")   # neutralise entities (must be first)
        .replace('"', "'")   # prevent closing the attribute
        .replace("<", "[")
        .replace(">", "]")
        .replace("\n", " ")
        .replace("\r", "")
    )
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Pipeline:
    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="http://host.docker.internal:17995",
            description="Claude Code Gateway server URL",
        )
        API_KEY: str = Field(
            default="",
            description="API key for the gateway server (leave empty if not required)",
        )
        MODEL: str = Field(
            default="sonnet",
            description="Claude model to use (e.g. sonnet, opus, haiku)",
        )
        TIMEOUT: int = Field(
            default=300,
            description="Request timeout in seconds",
        )
        # Context injection settings
        INJECT_USER_CONTEXT: bool = Field(
            default=True,
            description="Inject user context (username as mlm_username) into prompt",
        )
        INJECT_CREDENTIALS: bool = Field(
            default=True,
            description="Fetch and inject credentials from Open WebUI for MCP authentication",
        )
        OPEN_WEBUI_URL: str = Field(
            default="http://host.docker.internal:10088",
            description="Open WebUI base URL for fetching credentials",
        )
        # Thought wrapped mode settings
        OUTPUT_FORMAT: str = Field(
            default="default",
            description="Output format: 'default' (stream as-is) or 'thought_wrapped' (wrap thinking in <thought> tags)",
        )
        THOUGHT_WRAPPED_INSTRUCTION: bool = Field(
            default=True,
            description="Inject instruction for model to output <response> tag when done thinking",
        )
        TOOL_DISPLAY: str = Field(
            default="detailed",
            description="Tool display mode: 'detailed' (full <details> block with args/result), 'simple' (short status message), or 'mcp_only' (show only MCP tool results)",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._extra_headers: dict = {}

    def pipes(self) -> list:
        return [
            {
                "id": "chatdragon-completions",
                "name": "Chatdragon Completions",
            }
        ]

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def _inject_context(
        self,
        text: str,
        __user__: Optional[dict],
        user_id: Optional[str] = None,
        cookies: Optional[dict] = None,
        dscrowd_token: Optional[str] = None,
        mlm_username: Optional[str] = None,
    ) -> str:
        """Inject user and credential context into the prompt text."""
        context_parts = []

        if self.valves.INJECT_USER_CONTEXT:
            if mlm_username:
                context_parts.append(f"<mlm_username>{mlm_username}</mlm_username>")
            elif __user__:
                user_name = __user__.get("name", "")
                if user_name:
                    context_parts.append(f"<mlm_username>{user_name}</mlm_username>")

        if self.valves.INJECT_CREDENTIALS:
            if dscrowd_token:
                context_parts.append(f"<dscrowd.token_key>{dscrowd_token}</dscrowd.token_key>")
            elif cookies:
                token = cookies.get("dscrowd.token_key")
                if token:
                    context_parts.append(f"<dscrowd.token_key>{token}</dscrowd.token_key>")

        if context_parts:
            return text + "\n\n" + "\n".join(context_parts)
        return text

    def _get_thought_wrapped_instruction(self) -> str:
        return """

## 답변 작성 규칙

사용자에게 보여줄 최종 답변을 작성하기 직전에 반드시 `<response>` 토큰을 한 번 출력한다.
검색이나 도구 사용 여부와 관계없이 항상 `<response>` 토큰을 출력해야 한다.

- 검색/도구를 사용한 경우: 모든 검색이 끝난 뒤 답변 직전에 `<response>` 출력
- 검색/도구 없이 바로 답변하는 경우: 답변 시작 직전에 `<response>` 출력

```
<response>
```

이 토큰 이후에 최종 답변을 작성한다."""

    def _wrap_thought_content(self, text: str) -> str:
        if not text:
            return text
        if "<response>" in text:
            parts = text.split("<response>", 1)
            thought_content = parts[0].strip()
            response_content = parts[1].strip() if len(parts) > 1 else ""
            return f"<thought>\n{thought_content}\n</thought>\n\n{response_content}"
        return f"<thought>\n{text}\n</thought>"

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: list,
        body: dict,
    ):
        __user__ = body.get("user", {})
        __user_id__ = __user__.get("id", "")
        __metadata__ = body.get("metadata", {})
        __task__ = __metadata__.get("task")

        meta_headers = __metadata__.get("headers", {})

        self._extra_headers = {}

        dscrowd_token = meta_headers.get("x-cookie-dscrowd.token_key", "")
        if dscrowd_token:
            self._extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token

        owui_username = meta_headers.get("x-openwebui-user-name", "")
        if not owui_username and __user__:
            owui_username = __user__.get("name", "") or __user__.get("email", "")
        if owui_username:
            self._extra_headers["X-OpenWebUI-User-Name"] = owui_username

        __cookies__ = body.get("cookies", {})
        if __cookies__ and not dscrowd_token:
            dscrowd_token = __cookies__.get("dscrowd.token_key", "")
            if dscrowd_token:
                self._extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token

        if not messages:
            return "No messages provided."

        # Build messages list — inject context into the last user message
        messages = list(messages)
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                content = messages[i].get("content", "")
                if isinstance(content, str):
                    content = self._inject_context(
                        content,
                        __user__,
                        __user_id__,
                        __cookies__,
                        dscrowd_token=dscrowd_token or None,
                        mlm_username=owui_username or None,
                    )
                    if (
                        self.valves.OUTPUT_FORMAT == "thought_wrapped"
                        and self.valves.THOUGHT_WRAPPED_INSTRUCTION
                        and not __task__
                    ):
                        content += self._get_thought_wrapped_instruction()
                    messages[i] = {**messages[i], "content": content}
                break

        use_stream = body.get("stream", True)
        chat_id = __metadata__.get("chat_id", "")

        payload = {
            "model": self.valves.MODEL,
            "messages": messages,
            "stream": use_stream,
        }
        if chat_id:
            payload["session_id"] = chat_id

        if use_stream:
            return self._stream(payload, __task__)
        else:
            return self._non_stream(payload, __task__)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _stream(self, payload: dict, task: Optional[str]) -> Iterator[str]:
        thought_wrapped = self.valves.OUTPUT_FORMAT == "thought_wrapped" and not task
        thought_opened = False
        response_tag_sent = False
        text_buffer = ""
        BUFFER_SIZE = 50
        RESPONSE_TAG = "<response>"
        TOOL_DETAILS_PREFIX = "\n\n<details "

        tool_names: dict = {}
        tool_pending: dict = {}
        any_tool_used = False
        try:
            if thought_wrapped:
                yield "<thought>\n"
                thought_opened = True

            url = f"{self.valves.BASE_URL.rstrip('/')}/v1/chat/completions"
            with httpx.Client(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
                with client.stream("POST", url, json=payload, headers=self._make_headers()) as resp:
                    if resp.status_code != 200:
                        body_text = resp.read().decode()
                        raise Exception(f"Server error ({resp.status_code}): {body_text}")

                    for line in resp.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break

                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # Log every SSE event's top-level keys so we can
                        # spot tool_use arriving in an unexpected shape.
                        evt_keys = list(event.keys())
                        if "system_event" in evt_keys or "choices" not in evt_keys:
                            log.info(
                                "[PIPE-DEBUG] sse_event keys=%s preview=%s",
                                evt_keys, data_str[:300],
                            )

                        # Handle system_event (tool_use, tool_result, task events)
                        sys_event = event.get("system_event")
                        if sys_event:
                            event_type = sys_event.get("type", "")
                            log.info(
                                "[PIPE] system_event type=%s keys=%s",
                                event_type, list(sys_event.keys()),
                            )
                            if event_type in ("tool_use", "tool_result"):
                                any_tool_used = True
                                log.info(
                                    "[PIPE-DEBUG] %s raw_event=%s",
                                    event_type, json.dumps(sys_event, default=str)[:500],
                                )
                            rendered = self._render_system_event(
                                event_type, sys_event, tool_names, tool_pending,
                            )
                            if rendered:
                                if thought_wrapped and not response_tag_sent:
                                    # Tool <details> blocks bypass the buffer
                                    if text_buffer:
                                        yield text_buffer
                                        text_buffer = ""
                                    yield rendered
                                else:
                                    yield rendered
                            continue

                        choices = event.get("choices", [])
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        chunk = delta.get("content", "")
                        if not chunk:
                            continue

                        # Filter SDK tool-execution noise: bare tool
                        # names and "Executing tool_name..." status lines.
                        stripped = chunk.strip()
                        if _is_tool_noise(stripped):
                            continue

                        if thought_wrapped:
                            if response_tag_sent:
                                yield chunk
                            elif chunk.startswith(TOOL_DETAILS_PREFIX):
                                # Tool <details> blocks bypass the buffer
                                if text_buffer:
                                    yield text_buffer
                                    text_buffer = ""
                                yield chunk
                            else:
                                text_buffer += chunk
                                if RESPONSE_TAG in text_buffer:
                                    idx = text_buffer.index(RESPONSE_TAG)
                                    before = text_buffer[:idx]
                                    after = text_buffer[idx + len(RESPONSE_TAG):]
                                    if before:
                                        yield before
                                    yield "\n</thought>\n\n"
                                    response_tag_sent = True
                                    if after:
                                        yield after
                                    text_buffer = ""
                                elif len(text_buffer) > BUFFER_SIZE:
                                    safe_len = len(text_buffer) - len(RESPONSE_TAG)
                                    if safe_len > 0:
                                        yield text_buffer[:safe_len]
                                        text_buffer = text_buffer[safe_len:]
                        else:
                            yield chunk

        except Exception as e:
            log.error("Stream error: %s", e)
            yield f"\n\nError: {e}"
        finally:
            if thought_wrapped and thought_opened and not response_tag_sent:
                if not any_tool_used and text_buffer:
                    # No tools were used and model didn't emit <response> —
                    # treat the entire content as the response, not thought.
                    yield "\n</thought>\n\n"
                    yield text_buffer
                else:
                    if text_buffer:
                        yield text_buffer
                    yield "\n</thought>"

    def _render_system_event(
        self,
        event_type: str,
        event: dict,
        tool_names: dict,
        tool_pending: dict,
    ) -> Optional[str]:
        """Render a system_event into display text (tool blocks, task progress)."""

        if event_type == "task_started":
            desc = event.get("description", "")
            if desc:
                return f"\n\n> **Task**: {desc}\n"

        elif event_type == "task_progress":
            desc = event.get("description", "")
            tool = event.get("last_tool_name", "")
            usage = event.get("usage") or {}
            uses = usage.get("tool_uses", 0)
            text = f"\n> **Progress**: {desc}"
            if tool:
                text += f" ({tool}, {uses} uses)"
            return text + "\n"

        elif event_type == "task_notification":
            status = event.get("status", "")
            summary = event.get("summary", "")
            if summary:
                return f"\n> **Task {status}**: {summary}\n\n"

        elif event_type == "tool_use":
            log.info("[PIPE] tool_use event keys=%s", list(event.keys()))
            tool_id = event.get("tool_use_id", event.get("id", ""))
            name = event.get("name", "")
            if tool_id:
                tool_names[tool_id] = name
            tool_args = json.dumps(
                event.get("input", event.get("arguments", {})),
                ensure_ascii=False,
            )
            tool_pending[tool_id] = {"name": name, "args": tool_args}

        elif event_type == "tool_result":
            tool_id = event.get("tool_use_id", "")
            pending = tool_pending.pop(tool_id, {})
            name = pending.get("name", tool_names.get(tool_id, ""))
            args = pending.get("args", "{}")
            is_error = event.get("is_error", False)
            raw_content = event.get("content", "") or event.get("output", "") or event.get("result", "")
            log.info(
                "[PIPE] tool_result id=%s name=%s content_type=%s content_preview=%s",
                tool_id, name, type(raw_content).__name__,
                str(raw_content)[:300],
            )
            result_content = self._extract_tool_result_text(raw_content)
            if not result_content and is_error:
                result_content = event.get("error", "Tool execution failed")
            # SDK overflow: shorten the verbose message.
            if result_content.startswith("Error: result ("):
                m = re.search(r"\(([0-9,]+) characters?\)", result_content)
                chars = m.group(1) if m else "large"
                result_content = f"Result truncated ({chars} chars)"
            result_content = result_content[:10000]
            esc_name = html.escape(name)

            if self.valves.TOOL_DISPLAY == "mcp_only" and not name.startswith("mcp__"):
                return None

            if self.valves.TOOL_DISPLAY == "simple":
                status = "error" if is_error else "done"
                details_tag = f"\n> **Tool**: {esc_name} — {status}\n"
            else:
                safe_args = _safe_attr(args)
                safe_result = _safe_attr(result_content)
                details_tag = (
                    f'\n\n<details type="tool_calls"'
                    f' name="{esc_name}"'
                    f' arguments="{safe_args}"'
                    f' result="{safe_result}"'
                    f' done="true">\n'
                    f"<summary>Tool: {esc_name}</summary>\n"
                    f"</details>\n\n"
                )
                log.info(
                    "[PIPE-DEBUG] tool_id=%s name=%s args_len=%d result_len=%d",
                    tool_id, name, len(safe_args), len(safe_result),
                )
                log.info("[PIPE-DEBUG] raw_args=%s", args[:500])
                log.info("[PIPE-DEBUG] safe_args=%s", safe_args[:500])
                log.info("[PIPE-DEBUG] result_preview=%s", result_content[:500])
                log.info("[PIPE-DEBUG] safe_result_preview=%s", safe_result[:500])
            log.info("[PIPE-DEBUG] details_tag_first_300=%s", details_tag[:300])
            return details_tag

        return None

    @staticmethod
    def _extract_tool_result_text(raw_content) -> str:
        """Extract plain text from tool result content.

        Content may be a string, a list of text-block dicts, or a JSON-serialized
        version of either.  This method normalises all variants into a single
        plain-text string so the result can be safely placed in an HTML attribute.
        """
        if not raw_content:
            return ""

        # List of content blocks: [{"type": "text", "text": "..."}]
        if isinstance(raw_content, list):
            parts = []
            for b in raw_content:
                if isinstance(b, dict):
                    parts.append(b.get("text", ""))
                else:
                    parts.append(str(b))
            return " ".join(parts).strip()

        text = str(raw_content).strip()

        # If the string looks like a JSON array of text blocks, parse it
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    parts = []
                    for b in parsed:
                        if isinstance(b, dict):
                            parts.append(b.get("text", ""))
                        else:
                            parts.append(str(b))
                    return " ".join(parts).strip()
            except (json.JSONDecodeError, TypeError):
                pass

        return text

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    def _non_stream(self, payload: dict, task: Optional[str]) -> str:
        url = f"{self.valves.BASE_URL.rstrip('/')}/v1/chat/completions"
        try:
            with httpx.Client(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
                resp = client.post(url, json=payload, headers=self._make_headers())
                if resp.status_code != 200:
                    return f"Error: Server error ({resp.status_code}): {resp.text}"

                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return "Error: No response from server"

                content = choices[0].get("message", {}).get("content", "")
                if self.valves.OUTPUT_FORMAT == "thought_wrapped" and not task:
                    content = self._wrap_thought_content(content)
                return content
        except Exception as e:
            log.error("Non-stream error: %s", e)
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.valves.API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.API_KEY}"
        headers.update(self._extra_headers)
        return headers
