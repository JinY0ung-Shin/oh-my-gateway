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

import base64
import html
import json
import logging
import random
import re
import threading
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

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
            default=600,
            description="Total request timeout in seconds (increase for heavy MCP/search workloads)",
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
        TOOL_DISPLAY: bool = Field(
            default=True,
            description="Show detailed tool blocks with args and result; when off, show a short status line instead",
        )
        MCP_TOOL_ONLY: bool = Field(
            default=False,
            description="Only display MCP tool results; hide all built-in SDK tools (Read, Bash, Edit, etc.)",
        )
        VQA_IMAGE_DIR: str = Field(
            default="/app/shared_images",
            description="Shared directory for saving uploaded images (must be mounted in both Open WebUI and gateway containers)",
        )
        IMAGE_SERVER_BASE: str = Field(
            default="",
            description="Base URL pattern for the image server (e.g. 'https://image-server.example.com'). "
                        "When set, image links matching this URL will trigger the gallery sidebar in Open WebUI.",
        )


        @field_validator("TOOL_DISPLAY", mode="before")
        @classmethod
        def _coerce_tool_display(cls, v):
            """Accept legacy string values from stored configs."""
            if isinstance(v, str):
                return v.lower() not in ("simple", "mcp_only", "false", "0", "no", "off")
            return v

    def __init__(self):
        self.valves = self.Valves()
        self._local = threading.local()  # per-thread request context

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
    # Image gallery detection
    # ------------------------------------------------------------------

    def _detect_image_gallery_urls(self, text: str) -> list[dict]:
        """Detect image URLs from IMAGE_SERVER_BASE in text and return gallery info."""
        if not self.valves.IMAGE_SERVER_BASE:
            return []

        base = self.valves.IMAGE_SERVER_BASE.rstrip("/")
        # Match URLs that look like image paths from the configured server
        # Pattern: base_url/path/to/folder/image.ext
        import re
        pattern = re.escape(base) + r"(/[^\s\)\"'<>]+\.(?:jpg|jpeg|png|gif|webp|bmp|tiff))"
        matches = re.findall(pattern, text, re.IGNORECASE)

        results = []
        seen_folders = set()
        for match in matches:
            import os.path
            folder = os.path.dirname(match)
            filename = os.path.basename(match)
            if folder not in seen_folders:
                seen_folders.add(folder)
                results.append({"folder": folder, "current": filename, "base_url": base})
        return results

    @staticmethod
    def _parse_tool_content(raw_content):
        """Normalise raw MCP tool result into a Python object.

        Handles: direct dict/list, JSON string, content-block list
        ``[{type: text, text: ...}]``, and Python-repr single-quote strings.
        Returns the parsed object or ``None`` on failure.
        """
        if not raw_content:
            return None

        data = raw_content

        # Content-block list: [{"type": "text", "text": "..."}]
        if isinstance(data, list):
            texts = []
            for b in data:
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(b.get("text", ""))
                elif isinstance(b, str):
                    texts.append(b)
            if texts:
                data = " ".join(texts).strip()
            else:
                # Already a plain list of results
                return data

        if isinstance(data, dict):
            return data

        if not isinstance(data, str):
            return None

        text = data.strip()

        # Try standard JSON first
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try Python literal (single quotes, True/False/None)
        import ast
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            pass

        return None

    @staticmethod
    def _extract_thumbnails_from_tool_result(raw_content) -> list[str]:
        """Extract thumbnail URLs from MCP tool result content."""
        data = Pipeline._parse_tool_content(raw_content)
        if not data:
            return []

        thumbnails: list[str] = []

        def _collect(items):
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                meta = item.get("metadata") or {}
                thumb = (
                    item.get("thumbnail") or item.get("thumbnail_url") or ""
                    or meta.get("thumbnail") or meta.get("thumbnail_url") or ""
                )
                if thumb and isinstance(thumb, str):
                    thumbnails.append(thumb)

        if isinstance(data, dict):
            for key in ("responses", "results", "data", "items"):
                if isinstance(data.get(key), list):
                    _collect(data[key])
                    break
        elif isinstance(data, list):
            _collect(data)

        return thumbnails

    @staticmethod
    def _extract_tool_results_for_explorer(raw_content) -> list[dict]:
        """Extract structured results from MCP tool result for the explorer sidebar."""
        data = Pipeline._parse_tool_content(raw_content)
        if not data:
            return []

        items_list = None
        if isinstance(data, dict):
            for key in ("responses", "results", "data", "items"):
                if isinstance(data.get(key), list):
                    items_list = data[key]
                    break
        elif isinstance(data, list):
            items_list = data

        if not items_list:
            return []

        results = []
        for item in items_list:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            # URL: try multiple field names and Confluence _links
            url = (
                meta.get("url") or meta.get("edm_link")
                or item.get("url") or item.get("edm_link") or ""
            )
            if not url:
                # Confluence: build URL from _links.webui or page id
                links = meta.get("_links") or item.get("_links") or {}
                if links.get("webui"):
                    # Try to get base from space self link
                    space = meta.get("space") or {}
                    space_links = space.get("_links") or {}
                    base = ""
                    if space_links.get("self"):
                        # e.g. https://confluence.example.com/rest/api/space/KEY
                        base = space_links["self"].split("/rest/")[0]
                    if base:
                        url = f"{base}{links['webui']}"
                    else:
                        url = links["webui"]
                elif meta.get("page_id") or meta.get("id"):
                    page_id = meta.get("page_id") or meta.get("id")
                    space = meta.get("space") or {}
                    space_links = space.get("_links") or {}
                    if space_links.get("self"):
                        base = space_links["self"].split("/rest/")[0]
                        url = f"{base}/pages/viewpage.action?pageId={page_id}"
            # Thumbnail
            thumbnail = (
                meta.get("thumbnail") or meta.get("thumbnail_url")
                or item.get("thumbnail") or item.get("thumbnail_url") or ""
            )
            results.append({
                "title": item.get("title", ""),
                "content": (item.get("content") or "")[:200],
                "url": url,
                "thumbnail": thumbnail,
                "doc_type": item.get("doc_type") or meta.get("type") or "",
            })
        return results

    def _build_gallery_tag(
        self,
        folder: str = "",
        current: str = "",
        base_url: str = "",
        images: list[str] | None = None,
    ) -> str:
        """Build a <details type='image_gallery'> tag for the frontend.

        When *images* is provided the tag carries an inline JSON array of
        image URLs so the frontend can display them without an extra API
        call.  Otherwise the folder-based approach is used.
        """
        parts = ['type="image_gallery"', 'done="true"']
        if folder:
            parts.append(f'folder="{_safe_attr(folder)}"')
        if current:
            parts.append(f'current="{_safe_attr(current)}"')
        if base_url:
            parts.append(f'base_url="{_safe_attr(base_url)}"')
        if images:
            safe_images = _safe_attr(json.dumps(images, ensure_ascii=False))
            parts.append(f'images="{safe_images}"')
        attrs = " ".join(parts)
        return (
            f'\n\n<details {attrs}>\n'
            f'<summary>Image Gallery</summary>\n'
            f'</details>\n\n'
        )

    @staticmethod
    def _build_tool_explorer_tag(tool_data: dict) -> str:
        """Build a <details type='tool_explorer'> tag with JSON body.

        *tool_data* is a dict keyed by tool label, each value being a list
        of call dicts with ``query`` and ``results`` keys.
        """
        body = json.dumps(tool_data, ensure_ascii=False)
        return (
            f'\n\n<details type="tool_explorer" done="true">\n'
            f'<summary>Tool Results</summary>\n'
            f'{body}\n'
            f'</details>\n\n'
        )

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

        extra_headers: dict = {}

        dscrowd_token = meta_headers.get("x-cookie-dscrowd.token_key", "")
        if dscrowd_token:
            extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token

        owui_username = meta_headers.get("x-openwebui-user-name", "")
        if not owui_username and __user__:
            owui_username = __user__.get("name", "") or __user__.get("email", "")
        if owui_username:
            try:
                owui_username.encode("ascii")
                extra_headers["X-OpenWebUI-User-Name"] = owui_username
            except UnicodeEncodeError:
                from urllib.parse import quote
                extra_headers["X-OpenWebUI-User-Name"] = quote(owui_username)

        __cookies__ = body.get("cookies", {})
        if __cookies__ and not dscrowd_token:
            dscrowd_token = __cookies__.get("dscrowd.token_key", "")
            if dscrowd_token:
                extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token

        self._local.extra_headers = extra_headers

        if not messages:
            return "No messages provided."

        # Build messages list — inject context into the last user message
        messages = list(messages)
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                content = messages[i].get("content", "")
                # Save uploaded images to shared volume and replace image_url
                # parts with text references so the text-only LLM can call the
                # VQA tool with the file path.
                if isinstance(content, list):
                    image_dir = Path(self.valves.VQA_IMAGE_DIR)
                    image_dir.mkdir(parents=True, exist_ok=True)
                    new_content = []
                    saved_paths: list[str] = []
                    for j, part in enumerate(content):
                        if isinstance(part, dict) and part.get("type") == "image_url":
                            url = ""
                            img_field = part.get("image_url", {})
                            if isinstance(img_field, dict):
                                url = img_field.get("url", "")
                            elif isinstance(img_field, str):
                                url = img_field
                            if url.startswith("data:image/"):
                                try:
                                    header, encoded = url.split(",", 1)
                                    # e.g. data:image/png;base64 -> png
                                    ext = header.split("/")[1].split(";")[0] if "/" in header else "png"
                                    filename = f"{uuid4().hex}.{ext}"
                                    filepath = image_dir / filename
                                    filepath.write_bytes(base64.b64decode(encoded))
                                    saved_paths.append(str(filepath))
                                    log.info("[IMAGE] saved image part[%d] -> %s", j, filepath)
                                except Exception:
                                    log.exception("[IMAGE] failed to save image part[%d]", j)
                                    new_content.append(part)
                            else:
                                # Non-base64 URL (http, file path, etc.) — keep as-is for VQA
                                saved_paths.append(url)
                                log.info("[IMAGE] non-base64 image part[%d] url=%s", j, url[:120])
                        else:
                            new_content.append(part)
                    if saved_paths:
                        paths_str = ", ".join(saved_paths)
                        hint = (
                            f"[사용자가 이미지를 업로드했습니다. 이미지 경로: {paths_str}. "
                            f"이미지 분석이 필요하면 vqa_search 도구를 호출하세요.]"
                        )
                        new_content.append({"type": "text", "text": hint})
                        content = new_content
                        messages[i] = {**messages[i], "content": content}
                        log.info("[IMAGE] rewrote message with %d image path(s)", len(saved_paths))
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
                elif isinstance(content, list):
                    # Multimodal content (e.g. image + text from VQA queries).
                    # Find the last text part and inject context into it.
                    last_text_idx = None
                    for j in range(len(content) - 1, -1, -1):
                        part = content[j]
                        if isinstance(part, dict) and part.get("type") == "text":
                            last_text_idx = j
                            break
                    if last_text_idx is not None:
                        text = content[last_text_idx].get("text", "")
                        text = self._inject_context(
                            text,
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
                            text += self._get_thought_wrapped_instruction()
                        content = list(content)
                        content[last_text_idx] = {**content[last_text_idx], "text": text}
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
        full_text_acc = ""  # Accumulate full response for image URL detection
        BUFFER_SIZE = 50
        RESPONSE_TAG = "<response>"
        TOOL_DETAILS_PREFIX = "\n\n<details "

        tool_names: dict = {}
        tool_pending: dict = {}
        any_tool_used = False
        collected_thumbnails: list[str] = []  # Thumbnails from MCP tool results
        # Tool explorer: {tool_label: [{query, results}]}
        tool_explorer_data: dict[str, list[dict]] = {}
        try:
            if thought_wrapped:
                yield "<thought>\n"
                thought_opened = True

            url = f"{self.valves.BASE_URL.rstrip('/')}/v1/chat/completions"
            # Use a generous read timeout — the gateway sends SSE keepalive
            # comments every ~15s during idle periods (tool execution, context
            # compaction), so a 60s read timeout catches truly dead connections
            # while not killing active-but-quiet streams.
            timeout = httpx.Timeout(
                connect=30.0,
                read=60.0,
                write=30.0,
                pool=self.valves.TIMEOUT,
            )
            with httpx.Client(timeout=timeout) as client:
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
                            # Extract data from MCP tool results
                            if event_type == "tool_result":
                                tool_id = sys_event.get("tool_use_id", "")
                                raw = (
                                    sys_event.get("content", "")
                                    or sys_event.get("output", "")
                                    or sys_event.get("result", "")
                                )
                                log.info(
                                    "[PIPE-PARSE] raw type=%s len=%s preview=%s",
                                    type(raw).__name__,
                                    len(str(raw)),
                                    str(raw)[:300],
                                )
                                parsed = self._parse_tool_content(raw)
                                log.info(
                                    "[PIPE-PARSE] parsed type=%s result=%s",
                                    type(parsed).__name__ if parsed else "None",
                                    str(parsed)[:300] if parsed else "None",
                                )
                                # Thumbnails for gallery
                                thumbs = self._extract_thumbnails_from_tool_result(raw)
                                if thumbs:
                                    collected_thumbnails.extend(thumbs)
                                    log.info("[PIPE] collected %d thumbnails", len(thumbs))
                                # Structured results for tool explorer
                                t_name = tool_names.get(tool_id, "")
                                if t_name.startswith("mcp__"):
                                    results = self._extract_tool_results_for_explorer(raw)
                                    if results:
                                        # Derive label from mcp__server__tool
                                        parts = t_name.split("__")
                                        label = parts[1] if len(parts) >= 2 else t_name
                                        # Get query from pending args
                                        pending = tool_pending.get(tool_id, {})
                                        query = pending.get("args", "{}")
                                        try:
                                            q_parsed = json.loads(query)
                                            # Use first string value as display query
                                            query_str = ""
                                            for v in q_parsed.values():
                                                if isinstance(v, str) and len(v) > 2:
                                                    query_str = v
                                                    break
                                            query = query_str or query
                                        except (json.JSONDecodeError, AttributeError):
                                            pass
                                        if label not in tool_explorer_data:
                                            tool_explorer_data[label] = []
                                        tool_explorer_data[label].append({
                                            "query": query[:200],
                                            "results": results,
                                        })
                                        log.info(
                                            "[PIPE] tool_explorer: %s +%d results",
                                            label, len(results),
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
                                full_text_acc += chunk
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
                                        full_text_acc += after
                                        yield after
                                    text_buffer = ""
                                elif len(text_buffer) > BUFFER_SIZE:
                                    safe_len = len(text_buffer) - len(RESPONSE_TAG)
                                    if safe_len > 0:
                                        yield text_buffer[:safe_len]
                                        text_buffer = text_buffer[safe_len:]
                        else:
                            full_text_acc += chunk
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
                    full_text_acc += text_buffer
                    yield text_buffer
                else:
                    if text_buffer:
                        full_text_acc += text_buffer
                        yield text_buffer
                    yield "\n</thought>"

            # Emit tool explorer sidebar for collected MCP results
            if tool_explorer_data:
                yield self._build_tool_explorer_tag(tool_explorer_data)

            # Emit image gallery for collected MCP thumbnails
            if collected_thumbnails:
                yield self._build_gallery_tag(images=collected_thumbnails)

            # Emit image gallery tags for any IMAGE_SERVER_BASE URLs found
            gallery_matches = self._detect_image_gallery_urls(full_text_acc)
            for match in gallery_matches:
                yield self._build_gallery_tag(
                    folder=match["folder"],
                    current=match["current"],
                    base_url=match["base_url"],
                )

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

            if self.valves.MCP_TOOL_ONLY and not name.startswith("mcp__"):
                return None

            if not self.valves.TOOL_DISPLAY:
                friendly = self._friendly_tool_notification(name, is_error)
                details_tag = f"\n> {friendly}\n"
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

    # ── Friendly tool notification helpers ──────────────────────────────
    # Maps raw MCP tool-name suffix → friendly display name.
    _MCP_LABELS: dict[str, str] = {
        "mlm_cql": "MLM Confluence",
        "cql": "Confluence",
        "basic_knowledge": "knowledge base",
        "jira_search": "Jira",
        "jira_issue": "Jira issue",
        "web_search": "the web",
        "slack_search": "Slack",
        "google_drive": "Google Drive",
    }

    # Built-in SDK tools → friendly display name.
    _BUILTIN_LABELS: dict[str, str] = {
        "read": "a file",
        "edit": "a file",
        "write": "a file",
        "bash": "a command",
        "grep": "the codebase",
        "glob": "files",
        "todowrite": "the task list",
        "webfetch": "a webpage",
        "websearch": "the web",
        "notebookedit": "a notebook",
    }

    # Completion templates – "{label}" is replaced with the tool's display name.
    _DONE_TEMPLATES: list[str] = [
        "Finished searching {label}",
        "Done looking through {label}",
        "Completed {label} search",
        "Searched {label} successfully",
        "Got results from {label}",
        "Pulled data from {label}",
        "Wrapped up {label} lookup",
        "{label} search complete",
        "Retrieved results from {label}",
        "All done with {label}",
    ]

    _ERROR_TEMPLATES: list[str] = [
        "Failed to search {label}",
        "Something went wrong with {label}",
        "Could not complete {label} search",
    ]

    @classmethod
    def _tool_label(cls, raw_name: str) -> str:
        """Return a short, human-friendly label for a tool name."""
        lower = raw_name.lower()
        if lower in cls._BUILTIN_LABELS:
            return cls._BUILTIN_LABELS[lower]
        if lower.startswith("mcp__"):
            parts = raw_name.split("__")
            tool_key = parts[-1] if len(parts) >= 3 else parts[-1]
            if tool_key.lower() in cls._MCP_LABELS:
                return cls._MCP_LABELS[tool_key.lower()]
            return tool_key.replace("_", " ")
        return raw_name

    @classmethod
    def _friendly_tool_notification(cls, raw_name: str, is_error: bool = False) -> str:
        """Build a single-tool notification (fallback when buffer is unavailable)."""
        label = cls._tool_label(raw_name)
        if is_error:
            template = random.choice(cls._ERROR_TEMPLATES)
            return f"❌ {template.format(label=label)}"
        template = random.choice(cls._DONE_TEMPLATES)
        return f"✅ {template.format(label=label)}"

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
        extra = getattr(self._local, "extra_headers", None)
        if extra:
            headers.update(extra)
        return headers
