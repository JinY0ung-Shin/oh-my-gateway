"""
title: Claude Code Multi-Turn Pipe
author: claude-code-openai-wrapper
version: 0.3.0
description: True multi-turn conversations via /v1/responses API with previous_response_id chaining.
    The Claude SDK client on the wrapper server maintains conversation context natively.
    This pipe only tracks the chain link (previous_response_id) per Open WebUI chat.
license: MIT
"""

import asyncio
import hashlib
import json
import logging
from typing import AsyncGenerator

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


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

    def __init__(self):
        self.valves = self.Valves()
        # In-memory chain state: chat_id -> {previous_response_id, instructions_hash, model}
        self.chat_state: dict[str, dict] = {}
        # Per-chat locks for concurrency safety
        self._locks: dict[str, asyncio.Lock] = {}
        # Per-request extra headers forwarded to the gateway
        self._extra_headers: dict[str, str] = {}

    def pipes(self) -> list[dict]:
        return [
            {
                "id": "claude-code-multiturn",
                "name": "Claude Code (Multi-Turn)",
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
        # Debug logging
        log.info("[PIPE] __task__=%s, __metadata__=%s", __task__, __metadata__)
        log.info("[PIPE] chat_state keys=%s", list(self.chat_state.keys()))

        # Skip internal tasks (title generation, tags, query suggestions, etc.)
        if __task__:
            log.info("[PIPE] Skipping internal task: %s", __task__)
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
        # Extract username from Open WebUI user info
        if __user__ and isinstance(__user__, dict):
            username = __user__.get("name", "") or __user__.get("email", "")
            if username:
                self._extra_headers["X-MLM-Username"] = username

        if not chat_id:
            log.warning("[PIPE] No chat_id in metadata, multi-turn chaining disabled")

        messages = body.get("messages", [])
        if not messages:
            yield "No messages provided."
            return

        instructions = self._extract_instructions(messages)
        current_input = self._extract_current_input(messages)
        if not current_input:
            yield "Error: No user message found."
            return
        instructions_hash = self._hash(instructions) if instructions else "_none_"
        model = self.valves.MODEL
        use_stream = body.get("stream", True)

        lock = self._get_lock(chat_id) if chat_id else asyncio.Lock()
        async with lock:
            # Check if chain should be reset (instructions or model change)
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
            log.info(
                "[PIPE] chat_id=%s, prev_response_id=%s, state=%s", chat_id, prev_response_id, state
            )

            # Build /v1/responses payload
            payload = {
                "model": self.valves.MODEL,
                "input": current_input,
                "stream": use_stream,
            }
            if prev_response_id:
                payload["previous_response_id"] = prev_response_id
            elif instructions:
                payload["instructions"] = instructions

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

    async def _pipe_non_stream(
        self, chat_id: str, payload: dict, instructions: str, instructions_hash: str, body: dict
    ) -> str:
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

    def _make_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.valves.API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.API_KEY}"
        headers.update(self._extra_headers)
        return headers

    def _responses_url(self) -> str:
        return f"{self.valves.BASE_URL.rstrip('/')}/v1/responses"

    async def _read_error_body(self, resp) -> str:
        """Read error response body, trying JSON detail first."""
        try:
            data = resp.json()
            if isinstance(data, dict) and "detail" in data:
                detail = data["detail"]
                return detail if isinstance(detail, str) else json.dumps(detail)
        except Exception:
            pass
        return resp.text

    def _is_chain_error(self, status_code: int, detail: str) -> bool:
        """Determine if an HTTP error indicates a broken chain that should be reset."""
        if status_code == 404:
            return True
        if status_code == 400:
            chain_keywords = ("previous_response_id", "instructions", "session")
            return any(kw in detail.lower() for kw in chain_keywords)
        return False

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
            # Commit state on success
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

    async def _stream_responses(
        self, chat_id: str, payload: dict, instructions_hash: str
    ) -> AsyncGenerator[str, None]:
        """Streaming /v1/responses call. Yields text deltas."""
        log.info(
            "[STREAM] Starting request to %s with payload keys: %s",
            self._responses_url(),
            list(payload.keys()),
        )
        log.info("[STREAM] previous_response_id=%s", payload.get("previous_response_id"))
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
            async with client.stream(
                "POST", self._responses_url(), json=payload, headers=self._make_headers()
            ) as resp:
                log.info("[STREAM] Response status: %s", resp.status_code)
                if resp.status_code != 200:
                    body = ""
                    async for line in resp.aiter_lines():
                        body += line
                    log.error("[STREAM] Error body: %s", body)
                    if self._is_chain_error(resp.status_code, body):
                        raise ChainResetError(f"{resp.status_code}: {body}")
                    raise Exception(f"Server error ({resp.status_code}): {body}")

                completed = False
                line_count = 0
                # Track tool_use_id -> name for result display
                tool_names: dict[str, str] = {}
                async for line in resp.aiter_lines():
                    line_count += 1
                    if line_count <= 5 or line.startswith("data: ") and "completed" in line:
                        log.info("[STREAM] Line %d: %s", line_count, line[:200])

                    if not line.startswith("data: "):
                        continue

                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "response.output_text.delta":
                        delta = event.get("delta", "")
                        if delta:
                            yield delta

                    elif event_type == "response.task_started":
                        desc = event.get("description", "")
                        if desc:
                            yield f"\n\n> ⏳ **Task**: {desc}\n"

                    elif event_type == "response.task_progress":
                        desc = event.get("description", "")
                        tool = event.get("last_tool_name", "")
                        usage = event.get("usage") or {}
                        uses = usage.get("tool_uses", 0)
                        text = f"\n> 🔄 **Progress**: {desc}"
                        if tool:
                            text += f" ({tool}, {uses}회)"
                        yield text + "\n"

                    elif event_type == "response.task_notification":
                        status = event.get("status", "")
                        summary = event.get("summary", "")
                        if summary:
                            yield f"\n> ✅ **Task {status}**: {summary}\n\n"

                    elif event_type == "response.tool_use":
                        tool_id = event.get("tool_use_id", "")
                        name = event.get("name", "")
                        if tool_id:
                            tool_names[tool_id] = name
                        parent = event.get("parent_tool_use_id")
                        indent = "> > " if parent else "> "
                        event_json = json.dumps(event, indent=2, ensure_ascii=False)
                        quoted_json = ("\n" + indent).join(event_json.split("\n"))
                        yield f"\n\n{indent}🔧 **{name}**\n{indent}```json\n{indent}{quoted_json}\n{indent}```\n"

                    elif event_type == "response.tool_result":
                        tool_id = event.get("tool_use_id", "")
                        is_error = event.get("is_error", False)
                        parent = event.get("parent_tool_use_id")
                        tool_name = tool_names.get(tool_id, "")
                        prefix = "❌" if is_error else "📎"
                        label = f"{prefix} **Result**"
                        if tool_name:
                            label += f" ({tool_name})"
                        indent = "> > " if parent else "> "
                        event_json = json.dumps(event, indent=2, ensure_ascii=False)
                        quoted_json = ("\n" + indent).join(event_json.split("\n"))
                        yield f"\n{indent}{label}\n{indent}```json\n{indent}{quoted_json}\n{indent}```\n"

                    elif event_type == "response.completed":
                        completed = True
                        response_obj = event.get("response", {})
                        new_id = response_obj.get("id", "")
                        if new_id and chat_id:
                            self.chat_state[chat_id] = {
                                "previous_response_id": new_id,
                                "instructions_hash": instructions_hash,
                                "model": self.valves.MODEL,
                            }
                            log.debug("Chain updated: chat=%s, response_id=%s", chat_id, new_id)

                    elif event_type in ("response.failed", "error"):
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

                log.info(
                    "[STREAM] Stream ended. lines=%d, completed=%s, chat=%s",
                    line_count,
                    completed,
                    chat_id,
                )
                if not completed:
                    log.warning("[STREAM] No response.completed event received!")
                    yield "\n\n[Warning: Response may be incomplete]"

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
        """Extract system/developer messages from the leading position only.

        Open WebUI sends full history, so we only take system/developer messages
        that appear before the first user/assistant message to avoid duplicates.
        """
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
                break  # Stop at first non-system message
        return "\n".join(parts) if parts else ""

    def _extract_current_input(self, messages: list):
        """Extract the latest user message as a string or Responses API input array.

        Returns a plain string for text-only messages.  When image parts are
        present, returns a ``[{"role": "user", "content": [...]}]`` array so
        the ``/v1/responses`` endpoint can process images via ``input_image``.
        """
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
                        # Build Responses API input array with input_text / input_image parts
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
                    # Text-only list: concatenate as before
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
