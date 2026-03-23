"""
title: Domain Expert (Claude Research + Domain LLM Answer)
author: claude-code-openai-wrapper
version: 0.1.0
description: .
    Two-phase pipe: Claude does research/tool use (shown as collapsed thought),
    then a separate domain LLM generates the final user-facing answer using
    Claude's research context.
    Features:
    - All chatdragon_completions features (context injection, credentials, tool display)
    - Configurable domain LLM endpoint (litellm proxy, vLLM, Ollama, etc.)
    - Claude research streamed inside <thought> collapsible
    - Domain LLM answer streamed as visible response
license: MIT
"""

import base64
import html
import json
import logging
import random
import re
import time
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
    """Sanitize a string for use inside a double-quoted HTML attribute."""
    return (
        value.replace("&", "+")
        .replace('"', "'")
        .replace("<", "[")
        .replace(">", "]")
        .replace("\n", " ")
        .replace("\r", "")
    )


log = logging.getLogger(__name__)


class Pipeline:
    class Valves(BaseModel):
        # ── Claude Gateway settings ──────────────────────────────────
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
            description="Claude model to use for research (e.g. sonnet, opus, haiku)",
        )
        TIMEOUT: int = Field(
            default=600,
            description="Total request timeout in seconds for Claude gateway",
        )

        # ── Domain LLM settings ─────────────────────────────────────
        DOMAIN_LLM_URL: str = Field(
            default="http://127.0.0.1:4000/v1/chat/completions",
            description="Domain LLM endpoint (litellm proxy, vLLM, Ollama, etc.)",
        )
        DOMAIN_LLM_API_KEY: str = Field(
            default="sk-1234",
            description="API key for the domain LLM endpoint",
        )
        DOMAIN_LLM_MODEL: str = Field(
            default="my_model_name",
            description="Model name to send to the domain LLM endpoint",
        )
        DOMAIN_LLM_MAX_TOKENS: int = Field(
            default=4096,
            description="Max tokens for the domain LLM response",
        )
        DOMAIN_LLM_TIMEOUT: int = Field(
            default=120,
            description="Timeout in seconds for the domain LLM request",
        )
        DOMAIN_LLM_SYSTEM_PROMPT: str = Field(
            default=(
                "You are a domain expert assistant. You will be given research context "
                "gathered by a research agent (including tool results, document searches, "
                "and analysis). Use this context to provide a clear, accurate, and "
                "well-structured answer to the user's question. "
                "Always answer in the same language as the user's question."
            ),
            description="System prompt for the domain LLM that generates the final answer",
        )

        # ── Context injection settings ───────────────────────────────
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

        # ── Display settings ─────────────────────────────────────────
        TOOL_DISPLAY: bool = Field(
            default=True,
            description="Show detailed tool blocks with args and result; when off, show a short status line",
        )
        MCP_TOOL_ONLY: bool = Field(
            default=False,
            description="Only display MCP tool results; hide all built-in SDK tools",
        )
        VQA_IMAGE_DIR: str = Field(
            default="/app/shared_images",
            description="Shared directory for saving uploaded images",
        )
        PRINT_TOOL_ACTIVITY: bool = Field(
            default=True,
            description="Show tool use/result details inline. Disable for cleaner output.",
        )
        PRINT_RESEARCH_TEXT: bool = Field(
            default=True,
            description="Show Claude's narration text in the thought collapsible. Disable to show only tool blocks.",
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
        self._extra_headers: dict = {}

    def pipes(self) -> list:
        return [
            {
                "id": "chatdragon-domain",
                "name": "Chatdragon Domain Expert",
            }
        ]

    # ------------------------------------------------------------------
    # Context injection (shared with chatdragon_completions)
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
                # Save uploaded images to shared volume
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
                                    ext = (
                                        header.split("/")[1].split(";")[0]
                                        if "/" in header
                                        else "png"
                                    )
                                    filename = f"{uuid4().hex}.{ext}"
                                    filepath = image_dir / filename
                                    filepath.write_bytes(base64.b64decode(encoded))
                                    saved_paths.append(str(filepath))
                                    log.info("[IMAGE] saved image part[%d] -> %s", j, filepath)
                                except Exception:
                                    log.exception("[IMAGE] failed to save image part[%d]", j)
                                    new_content.append(part)
                            else:
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
                    messages[i] = {**messages[i], "content": content}
                elif isinstance(content, list):
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
                        content = list(content)
                        content[last_text_idx] = {**content[last_text_idx], "text": text}
                    messages[i] = {**messages[i], "content": content}
                break

        chat_id = __metadata__.get("chat_id", "")

        # For task requests (title generation, etc.), use a simple non-stream call
        # directly to the domain LLM — no Claude research needed.
        if __task__:
            return self._fallback_task(messages, body)

        use_stream = body.get("stream", True)

        payload = {
            "model": self.valves.MODEL,
            "messages": messages,
            "stream": True,  # Always stream from Claude for research
        }
        if chat_id:
            payload["session_id"] = chat_id

        if use_stream:
            return self._stream_dual(payload, messages)
        else:
            return self._non_stream_dual(payload, messages)

    # ------------------------------------------------------------------
    # Phase 1: Stream Claude research (always inside <thought>)
    # ------------------------------------------------------------------

    def _stream_claude_research(self, payload: dict) -> Iterator[str]:
        """Stream Claude's research output, yielding display chunks.

        Also collects the full research text (tool results + Claude's narration)
        in a list that the caller can read after iteration completes.
        """
        self._research_text_parts: list[str] = []
        self._research_tool_results: list[dict] = []

        tool_names: dict = {}
        tool_pending: dict = {}
        show_tools = self.valves.PRINT_TOOL_ACTIVITY
        KEEPALIVE_INTERVAL = 15
        last_yield_time = time.monotonic()

        def _keepalive_yield() -> Optional[str]:
            nonlocal last_yield_time
            now = time.monotonic()
            if now - last_yield_time > KEEPALIVE_INTERVAL:
                last_yield_time = now
                return "\u200b"
            return None

        def _track_yield():
            nonlocal last_yield_time
            last_yield_time = time.monotonic()

        url = f"{self.valves.BASE_URL.rstrip('/')}/v1/chat/completions"
        timeout = httpx.Timeout(
            connect=30.0,
            read=float(self.valves.TIMEOUT),
            write=30.0,
            pool=float(self.valves.TIMEOUT),
        )
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, json=payload, headers=self._make_headers()) as resp:
                if resp.status_code != 200:
                    body_text = resp.read().decode()
                    raise Exception(f"Gateway error ({resp.status_code}): {body_text}")

                for line in resp.iter_lines():
                    ka = _keepalive_yield()
                    if ka:
                        yield ka

                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # Handle system_event (tool_use, tool_result)
                    sys_event = event.get("system_event")
                    if sys_event:
                        event_type = sys_event.get("type", "")
                        log.info(
                            "[DOMAIN] system_event type=%s keys=%s",
                            event_type,
                            list(sys_event.keys()),
                        )
                        if event_type in ("tool_use", "tool_result"):
                            log.info(
                                "[DOMAIN-DEBUG] %s raw=%s",
                                event_type,
                                json.dumps(sys_event, default=str)[:500],
                            )

                        rendered = self._render_system_event(
                            event_type, sys_event, tool_names, tool_pending
                        )

                        # Collect tool results for domain LLM context
                        if event_type == "tool_result":
                            tool_id = sys_event.get("tool_use_id", "")
                            name = tool_names.get(tool_id, "")
                            raw_content = (
                                sys_event.get("content", "")
                                or sys_event.get("output", "")
                                or sys_event.get("result", "")
                            )
                            result_text = self._extract_tool_result_text(raw_content)[:5000]
                            self._research_tool_results.append(
                                {"tool": name, "result": result_text}
                            )

                        if rendered and show_tools:
                            yield rendered
                            _track_yield()
                        continue

                    choices = event.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    chunk = delta.get("content", "")
                    if not chunk:
                        continue

                    stripped = chunk.strip()
                    if _is_tool_noise(stripped):
                        continue

                    # Collect Claude's narration as research context
                    self._research_text_parts.append(chunk)

                    # Show Claude's narration inside thought (unless suppressed)
                    if self.valves.PRINT_RESEARCH_TEXT:
                        yield chunk
                        _track_yield()

    # ------------------------------------------------------------------
    # Phase 2: Stream domain LLM answer
    # ------------------------------------------------------------------

    def _stream_domain_llm(
        self,
        original_messages: list,
    ) -> Iterator[str]:
        """Send research context to the domain LLM and stream the answer."""
        research_text = "".join(self._research_text_parts).strip()
        tool_summary = ""
        if self._research_tool_results:
            parts = []
            for tr in self._research_tool_results:
                parts.append(f"[Tool: {tr['tool']}]\n{tr['result']}")
            tool_summary = "\n\n".join(parts)

        # Build the context message for the domain LLM
        # Include the original user question + Claude's research
        user_question = ""
        for msg in reversed(original_messages):
            if msg.get("role") == "user":
                c = msg.get("content", "")
                if isinstance(c, str):
                    user_question = c
                elif isinstance(c, list):
                    user_question = " ".join(
                        p.get("text", "")
                        for p in c
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                break

        research_context = ""
        if tool_summary:
            research_context += f"## Research Tool Results\n\n{tool_summary}\n\n"
        if research_text:
            research_context += f"## Research Agent Analysis\n\n{research_text}\n\n"

        domain_messages = []
        if self.valves.DOMAIN_LLM_SYSTEM_PROMPT:
            domain_messages.append(
                {
                    "role": "system",
                    "content": self.valves.DOMAIN_LLM_SYSTEM_PROMPT,
                }
            )

        domain_messages.append(
            {
                "role": "user",
                "content": (
                    f"## User Question\n\n{user_question}\n\n"
                    f"{research_context}"
                    f"Based on the research above, provide a comprehensive answer to the user's question."
                ),
            }
        )

        domain_payload = {
            "model": self.valves.DOMAIN_LLM_MODEL,
            "messages": domain_messages,
            "max_tokens": self.valves.DOMAIN_LLM_MAX_TOKENS,
            "stream": True,
        }

        headers = {"Content-Type": "application/json"}
        if self.valves.DOMAIN_LLM_API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.DOMAIN_LLM_API_KEY}"

        timeout = httpx.Timeout(
            connect=30.0,
            read=float(self.valves.DOMAIN_LLM_TIMEOUT),
            write=30.0,
            pool=float(self.valves.DOMAIN_LLM_TIMEOUT),
        )

        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST", self.valves.DOMAIN_LLM_URL, json=domain_payload, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    body_text = resp.read().decode()
                    raise Exception(f"Domain LLM error ({resp.status_code}): {body_text}")

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

                    choices = event.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    chunk = delta.get("content", "")
                    if chunk:
                        yield chunk

    # ------------------------------------------------------------------
    # Combined streaming: Claude research → Domain LLM answer
    # ------------------------------------------------------------------

    def _stream_dual(self, payload: dict, original_messages: list) -> Iterator[str]:
        """Two-phase stream: Claude research in <thought>, domain LLM as answer."""
        try:
            # Phase 1: Stream Claude research inside <thought>
            yield "<thought>\n"

            for chunk in self._stream_claude_research(payload):
                yield chunk

            yield "\n</thought>\n\n"

            # Phase 2: Stream domain LLM answer
            for chunk in self._stream_domain_llm(original_messages):
                yield chunk

        except Exception as e:
            log.error("[DOMAIN] Stream error: %s", e)
            yield f"\n\nError: {e}"

    # ------------------------------------------------------------------
    # Non-streaming dual mode
    # ------------------------------------------------------------------

    def _non_stream_dual(self, payload: dict, original_messages: list) -> str:
        """Non-streaming: collect Claude research, then get domain LLM answer."""
        try:
            # Phase 1: Collect Claude research (non-streaming from gateway)
            ns_payload = {**payload, "stream": False}
            url = f"{self.valves.BASE_URL.rstrip('/')}/v1/chat/completions"
            timeout = httpx.Timeout(self.valves.TIMEOUT)

            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=ns_payload, headers=self._make_headers())
                if resp.status_code != 200:
                    return f"Error: Gateway error ({resp.status_code}): {resp.text}"

                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return "Error: No research response from Claude"

                research_text = choices[0].get("message", {}).get("content", "")

            # Store research for _stream_domain_llm compatibility
            self._research_text_parts = [research_text]
            self._research_tool_results = []

            # Phase 2: Get domain LLM answer
            answer_parts = list(self._stream_domain_llm(original_messages))
            answer = "".join(answer_parts)

            return f"<thought>\n{research_text}\n</thought>\n\n{answer}"

        except Exception as e:
            log.error("[DOMAIN] Non-stream error: %s", e)
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Task fallback (title generation, etc.) — direct to domain LLM
    # ------------------------------------------------------------------

    def _fallback_task(self, messages: list, body: dict) -> str:
        """Handle task requests (title gen, tags, etc.) via domain LLM directly."""
        payload = {
            "model": self.valves.DOMAIN_LLM_MODEL,
            "messages": messages,
            "max_tokens": 200,
            "stream": False,
        }

        headers = {"Content-Type": "application/json"}
        if self.valves.DOMAIN_LLM_API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.DOMAIN_LLM_API_KEY}"

        try:
            timeout = httpx.Timeout(30.0)
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(self.valves.DOMAIN_LLM_URL, json=payload, headers=headers)
                if resp.status_code != 200:
                    return f"Error: Domain LLM ({resp.status_code}): {resp.text}"
                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return "Error: No response from domain LLM"
                return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            log.error("[DOMAIN] Task fallback error: %s", e)
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # System event rendering (shared with chatdragon_completions)
    # ------------------------------------------------------------------

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
            raw_content = (
                event.get("content", "") or event.get("output", "") or event.get("result", "")
            )
            result_content = self._extract_tool_result_text(raw_content)
            if not result_content and is_error:
                result_content = event.get("error", "Tool execution failed")
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
            return details_tag

        return None

    # ── Friendly tool notification helpers ──────────────────────────────
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
        label = cls._tool_label(raw_name)
        if is_error:
            template = random.choice(cls._ERROR_TEMPLATES)
            return f"❌ {template.format(label=label)}"
        template = random.choice(cls._DONE_TEMPLATES)
        return f"✅ {template.format(label=label)}"

    @staticmethod
    def _extract_tool_result_text(raw_content) -> str:
        if not raw_content:
            return ""
        if isinstance(raw_content, list):
            parts = []
            for b in raw_content:
                if isinstance(b, dict):
                    parts.append(b.get("text", ""))
                else:
                    parts.append(str(b))
            return " ".join(parts).strip()
        text = str(raw_content).strip()
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
    # Helpers
    # ------------------------------------------------------------------

    def _make_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.valves.API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.API_KEY}"
        headers.update(self._extra_headers)
        return headers
