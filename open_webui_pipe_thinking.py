"""
title: Claude Code Thinking Pipe
author: claude-code-openai-wrapper
version: 0.1.0
description: Multi-turn pipe that wraps intermediate tool activity in <think> tags.
    Open WebUI collapses <think>...</think> blocks, so users see a clean final answer
    with expandable reasoning. Based on open_webui_pipe.py with thinking wrapper logic
    moved from the gateway into the pipe.
license: MIT
"""

import asyncio
import hashlib
import html
import json
import logging
from typing import AsyncGenerator

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

RESPONSE_SENTINEL = "<response>"


class SentinelFilter:
    """Detect a sentinel token across chunked text deltas.

    Buffers characters only while a potential prefix of the sentinel is
    accumulating.  Once the full sentinel is matched it is consumed and
    replaced by *replacement*.
    """

    def __init__(self, sentinel: str, replacement: str = ""):
        self._sentinel = sentinel
        self._replacement = replacement
        self._buf = ""
        self._triggered = False

    @property
    def triggered(self) -> bool:
        return self._triggered

    def feed(self, text: str) -> tuple[str, bool]:
        """Return ``(output_text, just_triggered)``."""
        if self._triggered:
            return text, False

        output: list[str] = []
        for i, ch in enumerate(text):
            candidate = self._buf + ch
            if self._sentinel.startswith(candidate):
                self._buf = candidate
                if candidate == self._sentinel:
                    self._triggered = True
                    self._buf = ""
                    output.append(self._replacement)
                    return "".join(output) + text[i + 1 :], True
            else:
                output.append(self._buf)
                self._buf = ""
                if ch == self._sentinel[0]:
                    self._buf = ch
                else:
                    output.append(ch)
        return "".join(output), False

    def flush(self) -> str:
        result = self._buf
        self._buf = ""
        return result


class ChainResetError(Exception):
    """Raised when the conversation chain needs to be reset and retried."""

    pass


class Pipe:
    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="http://localhost:8000",
            description="Claude Code OpenAI Wrapper server URL",
        )
        API_KEY: str = Field(
            default="your-optional-api-key-here",
            description="API key for the wrapper server (leave empty if not required)",
        )
        MODEL: str = Field(
            default="opus",
            description="Claude model to use (e.g. sonnet, opus, haiku)",
        )
        TIMEOUT: int = Field(
            default=300,
            description="Request timeout in seconds",
        )
        FALLBACK_TO_CHAT_COMPLETIONS: bool = Field(
            default=False,
            description="Fall back to /v1/chat/completions on persistent failure",
        )
        WRAP_THINKING: bool = Field(
            default=True,
            description=(
                "Wrap intermediate tool calls and progress in <think> tags. "
                "Open WebUI collapses these so only the final answer is visible."
            ),
        )
        RESPONSE_SENTINEL_INSTRUCTION: str = Field(
            default=(
                "\n\n## Response Format\n"
                "When you have finished all tool calls and are ready to write your final answer, "
                "you MUST output the exact token `<response>` on its own line before your answer. "
                "Do not include any other text on that line. Begin your answer immediately after."
            ),
            description=(
                "Instruction appended to the system prompt telling the model to emit "
                "<response> before its final answer. Set to empty to disable sentinel "
                "detection (all text will stay inside the <think> block)."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        # In-memory chain state: chat_id -> {previous_response_id, instructions_hash, model}
        self.chat_state: dict[str, dict] = {}
        # Per-chat locks for concurrency safety
        self._locks: dict[str, asyncio.Lock] = {}
        self._extra_headers: dict[str, str] = {}

    def pipes(self) -> list[dict]:
        return [
            {
                "id": "claude-code-thinking",
                "name": "Claude Code (Thinking)",
            }
        ]

    def _get_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    async def pipe(
        self,
        body: dict,
        __user__: dict = None,
        __metadata__: dict = None,
        __task__: str = None,
        __event_emitter__=None,
    ) -> AsyncGenerator[str, None]:
        log.info("[THINK-PIPE] __task__=%s", __task__)

        # Skip internal tasks (title generation, tags, query suggestions, etc.)
        if __task__:
            log.info("[THINK-PIPE] Skipping internal task: %s", __task__)
            result = await self._fallback_chat_completions(body)
            yield result
            return

        __metadata__ = __metadata__ or {}
        chat_id = __metadata__.get("chat_id", "")

        # Forward cookies and user info as headers to the gateway
        self._extra_headers = {}
        meta_headers = __metadata__.get("headers", {})
        # Forward dscrowd.token_key cookie for MCP Confluence auth
        dscrowd_token = meta_headers.get("x-cookie-dscrowd.token_key", "")
        if dscrowd_token:
            self._extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token
        # Forward username from ENABLE_FORWARD_USER_INFO_HEADERS (lowercased in metadata)
        owui_username = meta_headers.get("x-openwebui-user-name", "")
        if not owui_username and __user__ and isinstance(__user__, dict):
            owui_username = __user__.get("name", "") or __user__.get("email", "")
        if owui_username:
            self._extra_headers["X-MLM-Username"] = owui_username

        if not chat_id:
            log.warning("[THINK-PIPE] No chat_id in metadata, multi-turn chaining disabled")

        messages = body.get("messages", [])
        if not messages:
            yield "No messages provided."
            return

        instructions = self._extract_instructions(messages)
        # Prepend user context to instructions
        context_parts = []
        if dscrowd_token:
            context_parts.append(f"dscrowd.token_key: {dscrowd_token}")
        if owui_username:
            context_parts.append(f"mlm_username: {owui_username}")
        if context_parts:
            context = "\n\n".join(context_parts)
            instructions = f"{context}\n\n{instructions}" if instructions else context
        current_input = self._extract_current_input(messages)
        if not current_input:
            yield "Error: No user message found."
            return
        instructions_hash = self._hash(instructions) if instructions else "_none_"
        model = self.valves.MODEL
        use_stream = body.get("stream", True)

        lock = self._get_lock(chat_id) if chat_id else asyncio.Lock()
        async with lock:
            state = self.chat_state.get(chat_id) if chat_id else None
            if state:
                hash_changed = state.get("instructions_hash") != instructions_hash
                model_changed = state.get("model") != model
                if hash_changed or model_changed:
                    reason = "instructions" if hash_changed else "model"
                    log.info("Chain reset for chat %s: %s changed", chat_id, reason)
                    self.chat_state.pop(chat_id, None)
                    state = None

            prev_response_id = state["previous_response_id"] if state else None

            payload = {
                "model": self.valves.MODEL,
                "input": current_input,
                "stream": use_stream,
            }
            sentinel_instruction = (
                self.valves.RESPONSE_SENTINEL_INSTRUCTION.strip()
                if self.valves.WRAP_THINKING
                else ""
            )
            if prev_response_id:
                payload["previous_response_id"] = prev_response_id
            elif instructions:
                if sentinel_instruction:
                    payload["instructions"] = instructions + "\n\n" + sentinel_instruction
                else:
                    payload["instructions"] = instructions
            elif sentinel_instruction:
                payload["instructions"] = sentinel_instruction

            if use_stream:
                async for chunk in self._pipe_stream(
                    chat_id, payload, instructions, instructions_hash, body
                ):
                    yield chunk
            else:
                result = await self._pipe_non_stream(
                    chat_id, payload, instructions, instructions_hash, body
                )
                yield result

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def _pipe_stream(
        self, chat_id: str, payload: dict, instructions: str, instructions_hash: str, body: dict
    ) -> AsyncGenerator[str, None]:
        try:
            async for chunk in self._stream_responses(chat_id, payload, instructions_hash):
                yield chunk
        except ChainResetError as e:
            log.info("Chain reset for chat %s: %s. Retrying as new conversation.", chat_id, e)
            if chat_id:
                self.chat_state.pop(chat_id, None)
            payload.pop("previous_response_id", None)
            if instructions:
                payload["instructions"] = instructions
            try:
                async for chunk in self._stream_responses(chat_id, payload, instructions_hash):
                    yield chunk
            except ChainResetError:
                log.error("Chain reset retry also failed for chat %s", chat_id)
                if self.valves.FALLBACK_TO_CHAT_COMPLETIONS:
                    yield await self._fallback_chat_completions(body)
                else:
                    yield "Error: Failed to establish conversation chain. Please start a new chat."
        except Exception as e:
            log.error("Unexpected error for chat %s: %s", chat_id, e)
            if self.valves.FALLBACK_TO_CHAT_COMPLETIONS:
                yield await self._fallback_chat_completions(body)
            else:
                yield f"Error: {e}"

    async def _stream_responses(
        self, chat_id: str, payload: dict, instructions_hash: str    ) -> AsyncGenerator[str, None]:
        """Streaming /v1/responses call with <think> wrapping."""
        wrap = self.valves.WRAP_THINKING
        think_open = False
        sentinel_filter = SentinelFilter(RESPONSE_SENTINEL, replacement="\n</think>\n")
        tool_names: dict[str, str] = {}

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
            async with client.stream(
                "POST", self._responses_url(), json=payload, headers=self._make_headers()
            ) as resp:
                if resp.status_code != 200:
                    body = ""
                    async for line in resp.aiter_lines():
                        body += line
                    if self._is_chain_error(resp.status_code, body):
                        raise ChainResetError(f"{resp.status_code}: {body}")
                    raise Exception(f"Server error ({resp.status_code}): {body}")

                completed = False
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    # --- Text deltas (intermediate reasoning + final answer) ---
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta", "")
                        if delta:
                            if wrap:
                                # Open think block on first text
                                if not think_open:
                                    yield "<think>\n"
                                    think_open = True
                                # Sentinel filter replaces <response> with </think>
                                filtered, _triggered = sentinel_filter.feed(delta)
                                if filtered:
                                    yield filtered
                            else:
                                yield delta
                        continue

                    # --- Intermediate events: wrap in <think> ---

                    if event_type == "response.tool_use":
                        tool_id = event.get("tool_use_id", "")
                        name = event.get("name", "")
                        if tool_id:
                            tool_names[tool_id] = name
                        if wrap:
                            if not think_open:
                                yield "<think>\n"
                                think_open = True
                            yield f"[Tool: {name}]\n"
                        else:
                            escaped = html.escape(name)
                            event_json = json.dumps(event, indent=2, ensure_ascii=False)
                            yield f'\n<details>\n<summary>Tool: {escaped}</summary>\n\n````json\n{event_json}\n````\n\n</details>\n'
                        continue

                    if event_type == "response.tool_result":
                        tool_id = event.get("tool_use_id", "")
                        tool_name = tool_names.get(tool_id, "")
                        is_error = event.get("is_error", False)
                        content = event.get("content", "")
                        if wrap:
                            if not think_open:
                                yield "<think>\n"
                                think_open = True
                            prefix = "Error" if is_error else "Result"
                            label = f"{prefix}({tool_name})" if tool_name else prefix
                            yield f"[{label}: {str(content)[:200]}]\n"
                        else:
                            prefix = "Error" if is_error else "Tool Result"
                            escaped = html.escape(tool_name) if tool_name else ""
                            label = f"{prefix}: {escaped}" if escaped else prefix
                            result_text = str(content)[:500]
                            yield f'\n<details>\n<summary>{label}</summary>\n\n````\n{result_text}\n````\n\n</details>\n'
                        continue

                    if event_type == "response.task_started":
                        desc = event.get("description", "")
                        if desc:
                            if wrap:
                                if not think_open:
                                    yield "<think>\n"
                                    think_open = True
                                yield f"[Task: {desc}]\n"
                            else:
                                escaped = html.escape(desc)
                                yield f'\n<details>\n<summary>Task: {escaped}</summary>\n\n</details>\n'
                        continue

                    if event_type == "response.task_progress":
                        desc = event.get("description", "")
                        tool = event.get("last_tool_name", "")
                        if wrap:
                            if not think_open:
                                yield "<think>\n"
                                think_open = True
                            text = f"[Progress: {desc}"
                            if tool:
                                text += f" ({tool})"
                            yield text + "]\n"
                        else:
                            text = html.escape(desc)
                            if tool:
                                text += f" ({html.escape(tool)})"
                            yield f'\n<details>\n<summary>Progress: {text}</summary>\n\n</details>\n'
                        continue

                    if event_type == "response.task_notification":
                        status = event.get("status", "")
                        summary = event.get("summary", "")
                        if summary:
                            if wrap:
                                if not think_open:
                                    yield "<think>\n"
                                    think_open = True
                                yield f"[Task {status}: {summary}]\n"
                            else:
                                escaped = html.escape(summary)
                                yield f'\n<details>\n<summary>Task {html.escape(status)}: {escaped}</summary>\n\n</details>\n'
                        continue

                    if event_type == "response.completed":
                        completed = True
                        response_obj = event.get("response", {})
                        new_id = response_obj.get("id", "")
                        if new_id and chat_id:
                            self.chat_state[chat_id] = {
                                "previous_response_id": new_id,
                                "instructions_hash": instructions_hash,
                                "model": self.valves.MODEL,
                            }
                        # Flush so the renderer finalizes the last HTML block
                        yield "\n"
                        continue

                    if event_type in ("response.failed", "error"):
                        error = event.get("response", {}).get("error", {})
                        if not error:
                            error = {
                                "code": event.get("code", "unknown"),
                                "message": event.get("message", "Unknown error"),
                            }
                        error_msg = error.get("message", "Unknown error")
                        error_code = error.get("code", "")
                        if error_code in ("sdk_error", "empty_response", "server_error"):
                            raise Exception(f"Response failed: {error_msg}")
                        raise ChainResetError(f"Response failed: {error_code} - {error_msg}")

                # Flush sentinel filter buffer
                if wrap:
                    remaining = sentinel_filter.flush()
                    if remaining:
                        yield remaining
                    # If think was opened but sentinel never fired, close it
                    if think_open and not sentinel_filter.triggered:
                        yield "\n</think>\n"

                if not completed:
                    log.warning("[THINK-PIPE] No response.completed event received!")
                    yield "\n\n[Warning: Response may be incomplete]"

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    async def _pipe_non_stream(
        self, chat_id: str, payload: dict, instructions: str, instructions_hash: str, body: dict    ) -> str:
        try:
            return await self._call_responses(chat_id, payload, instructions_hash)
        except ChainResetError as e:
            log.info("Chain reset for chat %s: %s. Retrying as new conversation.", chat_id, e)
            if chat_id:
                self.chat_state.pop(chat_id, None)
            payload.pop("previous_response_id", None)
            if instructions:
                payload["instructions"] = instructions
            try:
                return await self._call_responses(chat_id, payload, instructions_hash)
            except ChainResetError:
                log.error("Chain reset retry also failed for chat %s", chat_id)
                if self.valves.FALLBACK_TO_CHAT_COMPLETIONS:
                    return await self._fallback_chat_completions(body)
                return "Error: Failed to establish conversation chain. Please start a new chat."
        except Exception as e:
            log.error("Unexpected error for chat %s: %s", chat_id, e)
            if self.valves.FALLBACK_TO_CHAT_COMPLETIONS:
                return await self._fallback_chat_completions(body)
            return f"Error: {e}"

    async def _call_responses(self, chat_id: str, payload: dict, instructions_hash: str) -> str:
        """Non-streaming /v1/responses call."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
            resp = await client.post(
                self._responses_url(), json=payload, headers=self._make_headers()
            )

            if resp.status_code != 200:
                detail = await self._read_error_body(resp)
                if self._is_chain_error(resp.status_code, detail):
                    raise ChainResetError(f"{resp.status_code}: {detail}")
                raise Exception(f"Server error ({resp.status_code}): {detail}")

            data = resp.json()
            new_id = data.get("id", "")
            if new_id and chat_id:
                self.chat_state[chat_id] = {
                    "previous_response_id": new_id,
                    "instructions_hash": instructions_hash,
                    "model": self.valves.MODEL,
                }

            output = data.get("output", [])
            if output:
                content_parts = output[0].get("content", [])
                if content_parts:
                    return content_parts[0].get("text", "")
            return "Error: Empty response from server"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.valves.API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.API_KEY}"
        headers.update(self._extra_headers)
        return headers

    def _responses_url(self) -> str:
        return f"{self.valves.BASE_URL.rstrip('/')}/v1/responses"

    async def _read_error_body(self, resp) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict) and "detail" in data:
                detail = data["detail"]
                return detail if isinstance(detail, str) else json.dumps(detail)
        except Exception:
            pass
        return resp.text

    def _is_chain_error(self, status_code: int, detail: str) -> bool:
        if status_code == 404:
            return True
        if status_code == 400:
            chain_keywords = ("previous_response_id", "instructions", "session")
            return any(kw in detail.lower() for kw in chain_keywords)
        return False

    async def _fallback_chat_completions(self, body: dict) -> str:
        url = f"{self.valves.BASE_URL.rstrip('/')}/v1/chat/completions"
        payload = {
            "model": self.valves.MODEL,
            "messages": body.get("messages", []),
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
            resp = await client.post(url, json=payload, headers=self._make_headers())
            if resp.status_code != 200:
                return f"Error: Fallback also failed ({resp.status_code})"
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return "Error: No response from fallback"

    def _extract_instructions(self, messages: list) -> str:
        parts = []
        for msg in messages:
            role = msg.get("role", "")
            if role in ("system", "developer"):
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("text"):
                            parts.append(item["text"])
            elif role in ("user", "assistant"):
                break
        return "\n".join(parts) if parts else ""

    def _extract_current_input(self, messages: list):
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    has_image = any(
                        isinstance(item, dict) and item.get("type") in ("image_url", "image")
                        for item in content
                    )
                    if has_image:
                        parts = []
                        for item in content:
                            if isinstance(item, str):
                                parts.append({"type": "input_text", "text": item})
                            elif isinstance(item, dict):
                                item_type = item.get("type", "")
                                if item_type == "text" and item.get("text"):
                                    parts.append({"type": "input_text", "text": item["text"]})
                                elif item_type == "image_url":
                                    url = ""
                                    image_url = item.get("image_url")
                                    if isinstance(image_url, dict):
                                        url = image_url.get("url", "")
                                    elif isinstance(image_url, str):
                                        url = image_url
                                    if url:
                                        parts.append({"type": "input_image", "image_url": url})
                        return [{"role": "user", "content": parts}] if parts else ""
                    texts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("text"):
                            texts.append(item["text"])
                        elif isinstance(item, str):
                            texts.append(item)
                    return "\n".join(texts) if texts else ""
        return ""

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]
