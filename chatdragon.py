"""
title: Development Assistant
author: claude-code-openai-wrapper
version: 0.7.0
description: .
    True multi-turn conversations via /v1/responses API with previous_response_id chaining.
    Features:
    - User context injection (mlm_username from metadata.user_id or user.name)
    - Credential fetching from Open WebUI API for MCP authentication
    - thought_wrapped mode: wraps thinking in <thought> tags and detects <response> tag
license: MIT
"""

import hashlib
import html
import json
import re
import logging
import os
import threading
from typing import Iterator, Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class ChainResetError(Exception):
    """Raised when the conversation chain needs to be reset and retried."""
    pass


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
        FALLBACK_TO_CHAT_COMPLETIONS: bool = Field(
            default=False,
            description="Fall back to /v1/chat/completions on persistent failure",
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

    def __init__(self):
        self.valves = self.Valves()
        self.chat_state: dict = {}
        self._locks: dict = {}
        self._lock = threading.Lock()
        self._extra_headers: dict = {}

    def pipes(self) -> list:
        return [
            {
                "id": "chatdragon",
                "name": "Chatdragon",
            }
        ]

    def _get_lock(self, chat_id: str) -> threading.Lock:
        with self._lock:
            if chat_id not in self._locks:
                self._locks[chat_id] = threading.Lock()
            return self._locks[chat_id]

    def _fetch_confluence_credential(self, user_id: str) -> Optional[str]:
        """Fetch Confluence credential (dscrowd.token_key) from Open WebUI API.

        The credential is stored in the database when the user logs in via OAuth.
        """
        if not user_id:
            return None

        try:
            # Try the custom credentials API endpoint first
            url = f"{self.valves.OPEN_WEBUI_URL}/api/v1/custom/credentials/confluence/token"
            params = {"user_id": user_id}

            with httpx.Client(timeout=10) as client:
                resp = client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    token = data.get("token")
                    if token:
                        log.info("[PIPE] Fetched dscrowd.token_key for user_id=%s", user_id)
                        return token
                else:
                    log.debug("[PIPE] Failed to fetch credential: status=%s, trying fallback", resp.status_code)

                    # Fallback to original path
                    url = f"{self.valves.OPEN_WEBUI_URL}/api/v1/credentials/confluence/token"
                    resp = client.get(url, params=params)
                    if resp.status_code == 200:
                        data = resp.json()
                        token = data.get("token")
                        if token:
                            log.info("[PIPE] Fetched dscrowd.token_key (fallback) for user_id=%s", user_id)
                            return token
        except Exception as e:
            log.warning("[PIPE] Error fetching credential: %s", e)

        return None

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

        # Inject username as mlm_username
        if self.valves.INJECT_USER_CONTEXT:
            if mlm_username:
                context_parts.append(f"<mlm_username>{mlm_username}</mlm_username>")
                log.info("[PIPE] Injected mlm_username: %s", mlm_username)
            elif __user__:
                user_name = __user__.get("name", "")
                if user_name:
                    context_parts.append(f"<mlm_username>{user_name}</mlm_username>")
                    log.info("[PIPE] Injected mlm_username: %s", user_name)

        # Inject Confluence credential (dscrowd.token_key)
        if self.valves.INJECT_CREDENTIALS:
            # Use pre-extracted token from headers/cookies
            if dscrowd_token:
                context_parts.append(f"<dscrowd.token_key>{dscrowd_token}</dscrowd.token_key>")
                log.info("[PIPE] Injected dscrowd.token_key (len=%d)", len(dscrowd_token))
            # Fall back to cookies dict
            elif cookies:
                token = cookies.get("dscrowd.token_key")
                if token:
                    context_parts.append(f"<dscrowd.token_key>{token}</dscrowd.token_key>")
                    log.info("[PIPE] Injected dscrowd.token_key from cookies (len=%d)", len(token))

        if context_parts:
            return text + "\n\n" + "\n".join(context_parts)
        return text

    def _get_thought_wrapped_instruction(self) -> str:
        """Get the instruction for model to output <response> tag."""
        return """

## 검색 완료 후 답변 작성

모든 검색이 완료되면 답변 작성을 시작하기 직전에 반드시 `<response>` 토큰을 출력한다. 이 토큰은 검색 단계가 끝났음을 명시적으로 나타낸다.

```
<response>
```

이 후 검색 결과를 바탕으로 답변을 작성한다."""

    def _wrap_thought_content(self, text: str) -> str:
        """Wrap content in thought tags and handle <response> tag detection."""
        if not text:
            return text

        if "<response>" in text:
            parts = text.split("<response>", 1)
            thought_content = parts[0].strip()
            response_content = parts[1].strip() if len(parts) > 1 else ""
            result = f"<thought>\n{thought_content}\n</thought>\n\n{response_content}"
            return result
        else:
            return f"<thought>\n{text}\n</thought>"

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: list,
        body: dict,
    ):
        """Main pipe entry point."""
        # User info is passed directly in body["user"] by Open WebUI's patch.py
        # (see patch.py line 184-191: payload["user"] = {...})
        __user__ = body.get("user", {})
        __user_id__ = __user__.get("id", "")

        # Metadata may also be present at body level (Open WebUI adds it before patch.py pops it)
        __metadata__ = body.get("metadata", {})
        __task__ = __metadata__.get("task")
        __chat_id__ = __metadata__.get("chat_id", "")

        # Extract headers from metadata (forwarded by Open WebUI with ENABLE_FORWARD_USER_INFO_HEADERS)
        meta_headers = __metadata__.get("headers", {})

        # Forward cookies and user info as headers to the gateway
        self._extra_headers = {}

        # Forward dscrowd.token_key cookie for MCP Confluence auth
        dscrowd_token = meta_headers.get("x-cookie-dscrowd.token_key", "")
        if dscrowd_token:
            self._extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token
            log.info("[PIPE] Forwarding X-Cookie-dscrowd.token_key header (len=%d)", len(dscrowd_token))

        # Forward username from ENABLE_FORWARD_USER_INFO_HEADERS (lowercased in metadata)
        owui_username = meta_headers.get("x-openwebui-user-name", "")
        if not owui_username and __user__:
            owui_username = __user__.get("name", "") or __user__.get("email", "")
        if owui_username:
            self._extra_headers["X-OpenWebUI-User-Name"] = owui_username
            log.info("[PIPE] Forwarding X-OpenWebUI-User-Name header: %s", owui_username)

        # Also check for cookies dict (legacy support)
        __cookies__ = body.get("cookies", {})
        if __cookies__ and not dscrowd_token:
            dscrowd_token = __cookies__.get("dscrowd.token_key", "")
            if dscrowd_token:
                self._extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token
                log.info("[PIPE] Forwarding X-Cookie-dscrowd.token_key from cookies dict (len=%d)", len(dscrowd_token))

        log.info("[PIPE] __task__=%s, user_id=%s, chat_id=%s", __task__, __user_id__, __chat_id__)
        log.info("[PIPE] __user__=%s", __user__)
        log.info("[PIPE] meta_headers keys=%s", list(meta_headers.keys()) if meta_headers else "none")
        log.info("[PIPE] __cookies__=%s", __cookies__)
        log.info("[PIPE] body keys=%s", list(body.keys()))
        log.info("[PIPE] _extra_headers keys=%s", list(self._extra_headers.keys()))
        log.info("[PIPE] chat_state keys=%s", list(self.chat_state.keys()))
        log.info("[PIPE] OUTPUT_FORMAT=%s", self.valves.OUTPUT_FORMAT)

        if __task__:
            log.info("[PIPE] Skipping internal task: %s", __task__)
            return self._fallback_chat_completions_sync(messages, body)

        chat_id = __chat_id__

        if not chat_id:
            log.warning("[PIPE] No chat_id in metadata, multi-turn chaining disabled")

        if not messages:
            return "No messages provided."

        instructions = self._extract_instructions(messages)
        current_input = self._extract_current_input(messages)
        if not current_input:
            return "Error: No user message found."

        # Inject context into current_input (credentials from headers or cookies)
        current_input = self._inject_context(
            current_input,
            __user__,
            __user_id__,
            __cookies__,
            dscrowd_token=dscrowd_token if dscrowd_token else None,
            mlm_username=owui_username if owui_username else None,
        )

        # Add thought_wrapped instruction to user message if enabled
        if self.valves.OUTPUT_FORMAT == "thought_wrapped" and self.valves.THOUGHT_WRAPPED_INSTRUCTION:
            thought_instruction = self._get_thought_wrapped_instruction()
            # Inject into user message (current_input)
            current_input = current_input + thought_instruction
            log.info("[PIPE] Injected thought_wrapped instruction into user message")

        instructions_hash = self._hash(instructions) if instructions else "_none_"
        model = self.valves.MODEL
        use_stream = body.get("stream", True)

        lock = self._get_lock(chat_id) if chat_id else threading.Lock()
        with lock:
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
                return self._pipe_stream(
                    chat_id, payload, instructions, instructions_hash, messages, body
                )
            else:
                return self._pipe_non_stream(
                    chat_id, payload, instructions, instructions_hash, messages, body
                )

    def _pipe_stream(
        self,
        chat_id: str,
        payload: dict,
        instructions: str,
        instructions_hash: str,
        messages: list,
        body: dict,
    ) -> Iterator[str]:
        """Handle streaming response with thought_wrapped mode support."""
        thought_wrapped = self.valves.OUTPUT_FORMAT == "thought_wrapped"
        thought_opened = False
        response_tag_sent = False
        text_buffer = ""
        BUFFER_SIZE = 50
        RESPONSE_TAG = "<response>"
        TOOL_DETAILS_PREFIX = "\n\n<details "

        try:
            if thought_wrapped:
                yield "<thought>\n"
                thought_opened = True

            for chunk in self._stream_responses_raw_sync(chat_id, payload, instructions_hash):
                if thought_wrapped:
                    if response_tag_sent:
                        yield chunk
                    elif chunk.startswith(TOOL_DETAILS_PREFIX):
                        # Tool call <details> blocks must bypass the buffer
                        # entirely. The buffer holds back the last 10 chars to
                        # avoid splitting "<response>", but that also splits
                        # "</details>" and breaks Open WebUI rendering.
                        if text_buffer:
                            yield text_buffer
                            text_buffer = ""
                        yield chunk
                    else:
                        text_buffer += chunk

                        if RESPONSE_TAG in text_buffer:
                            idx = text_buffer.index(RESPONSE_TAG)
                            before = text_buffer[:idx]
                            after = text_buffer[len(RESPONSE_TAG) + idx:]

                            if before:
                                yield before

                            yield "\n</thought>\n\n"
                            response_tag_sent = True

                            if after:
                                yield after

                            text_buffer = ""
                        else:
                            if len(text_buffer) > BUFFER_SIZE:
                                safe_len = len(text_buffer) - len(RESPONSE_TAG)
                                if safe_len > 0:
                                    yield text_buffer[:safe_len]
                                    text_buffer = text_buffer[safe_len:]
                else:
                    yield chunk

        except ChainResetError as e:
            log.info("Chain reset for chat %s: %s. Retrying as new conversation.", chat_id, e)
            if chat_id:
                self.chat_state.pop(chat_id, None)
            payload.pop("previous_response_id", None)
            if instructions:
                payload["instructions"] = instructions
            try:
                if thought_wrapped and thought_opened and not response_tag_sent:
                    yield "\n</thought>\n\n"

                thought_opened = False
                response_tag_sent = False
                text_buffer = ""

                if thought_wrapped:
                    yield "<thought>\n"
                    thought_opened = True

                for chunk in self._stream_responses_raw_sync(chat_id, payload, instructions_hash):
                    if thought_wrapped:
                        if response_tag_sent:
                            yield chunk
                        elif chunk.startswith(TOOL_DETAILS_PREFIX):
                            if text_buffer:
                                yield text_buffer
                                text_buffer = ""
                            yield chunk
                        else:
                            text_buffer += chunk
                            if RESPONSE_TAG in text_buffer:
                                idx = text_buffer.index(RESPONSE_TAG)
                                before = text_buffer[:idx]
                                after = text_buffer[len(RESPONSE_TAG) + idx:]
                                if before:
                                    yield before
                                yield "\n</thought>\n\n"
                                response_tag_sent = True
                                if after:
                                    yield after
                                text_buffer = ""
                            else:
                                if len(text_buffer) > BUFFER_SIZE:
                                    safe_len = len(text_buffer) - len(RESPONSE_TAG)
                                    if safe_len > 0:
                                        yield text_buffer[:safe_len]
                                        text_buffer = text_buffer[safe_len:]
                    else:
                        yield chunk

            except ChainResetError:
                log.error("Chain reset retry also failed for chat %s", chat_id)
                if self.valves.FALLBACK_TO_CHAT_COMPLETIONS:
                    yield self._fallback_chat_completions_sync(messages, body)
                else:
                    yield "Error: Failed to establish conversation chain. Please start a new chat."
        except Exception as e:
            log.error("Unexpected error for chat %s: %s", chat_id, e)
            if self.valves.FALLBACK_TO_CHAT_COMPLETIONS:
                yield self._fallback_chat_completions_sync(messages, body)
            else:
                yield f"Error: {e}"
        finally:
            if thought_wrapped and thought_opened and not response_tag_sent:
                if text_buffer:
                    yield text_buffer
                yield "\n</thought>"

    def _stream_responses_raw_sync(
        self, chat_id: str, payload: dict, instructions_hash: str
    ) -> Iterator[str]:
        """Streaming /v1/responses call. Yields raw text deltas."""
        log.info(
            "[STREAM] Starting request to %s with payload keys: %s",
            self._responses_url(),
            list(payload.keys()),
        )
        log.info("[STREAM] previous_response_id=%s", payload.get("previous_response_id"))

        with httpx.Client(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
            with client.stream("POST", self._responses_url(), json=payload, headers=self._make_headers()) as resp:
                log.info("[STREAM] Response status: %s", resp.status_code)
                if resp.status_code != 200:
                    body_text = ""
                    for line in resp.iter_lines():
                        body_text += line
                    log.error("[STREAM] Error body: %s", body_text)
                    if self._is_chain_error(resp.status_code, body_text):
                        raise ChainResetError(f"{resp.status_code}: {body_text}")
                    raise Exception(f"Server error ({resp.status_code}): {body_text}")

                completed = False
                line_count = 0
                tool_names: dict = {}
                tool_pending: dict = {}
                for line in resp.iter_lines():
                    line_count += 1
                    if line_count <= 5 or (line.startswith("data: ") and "completed" in line):
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
                            yield f"\n\n> **Task**: {desc}\n"

                    elif event_type == "response.task_progress":
                        desc = event.get("description", "")
                        tool = event.get("last_tool_name", "")
                        usage = event.get("usage") or {}
                        uses = usage.get("tool_uses", 0)
                        text = f"\n> **Progress**: {desc}"
                        if tool:
                            text += f" ({tool}, {uses} uses)"
                        yield text + "\n"

                    elif event_type == "response.task_notification":
                        status = event.get("status", "")
                        summary = event.get("summary", "")
                        if summary:
                            yield f"\n> **Task {status}**: {summary}\n\n"

                    elif event_type == "response.tool_use":
                        tool_id = event.get("tool_use_id", "")
                        name = event.get("name", "")
                        if tool_id:
                            tool_names[tool_id] = name
                        # Store tool_use args for pairing with result later
                        tool_args = json.dumps(
                            event.get("input", event.get("arguments", {})),
                            ensure_ascii=False,
                        )
                        tool_pending[tool_id] = {"name": name, "args": tool_args}

                    elif event_type == "response.tool_result":
                        tool_id = event.get("tool_use_id", "")
                        pending = tool_pending.pop(tool_id, {})
                        name = pending.get("name", tool_names.get(tool_id, ""))
                        args = pending.get("args", "{}")
                        is_error = event.get("is_error", False)
                        # Extract text from content — may be a string, list of
                        # content blocks, or empty/None.
                        raw_content = event.get("content", "")
                        if isinstance(raw_content, list):
                            result_content = " ".join(
                                b.get("text", "") if isinstance(b, dict) else str(b)
                                for b in raw_content
                            ).strip()
                        else:
                            result_content = str(raw_content or "").strip()
                        if not result_content and is_error:
                            result_content = event.get("error", "Tool execution failed")
                        # SDK overflow: shorten the verbose message.
                        if result_content.startswith("Error: result ("):
                            m = re.search(r"\(([0-9,]+) characters?\)", result_content)
                            chars = m.group(1) if m else "large"
                            result_content = f"Result truncated ({chars} chars)"
                        result_content = result_content[:10000]
                        esc_name = html.escape(name)
                        esc_args = html.escape(args)
                        esc_result = html.escape(result_content)
                        yield (
                            f'\n\n<details type="tool_calls"'
                            f' name="{esc_name}"'
                            f' arguments="{esc_args}"'
                            f' result="{esc_result}"'
                            f' done="true">\n'
                            f"<summary>Tool: {esc_name}</summary>\n"
                            f"</details>\n\n"
                        )

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
                        yield "\n"

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

    def _pipe_non_stream(
        self,
        chat_id: str,
        payload: dict,
        instructions: str,
        instructions_hash: str,
        messages: list,
        body: dict,
    ) -> str:
        """Handle non-streaming response with thought_wrapped mode support."""
        try:
            result = self._call_responses(chat_id, payload, instructions_hash)

            if self.valves.OUTPUT_FORMAT == "thought_wrapped":
                result = self._wrap_thought_content(result)

            return result
        except ChainResetError as e:
            log.info("Chain reset for chat %s: %s. Retrying as new conversation.", chat_id, e)
            if chat_id:
                self.chat_state.pop(chat_id, None)
            payload.pop("previous_response_id", None)
            if instructions:
                payload["instructions"] = instructions
            try:
                result = self._call_responses(chat_id, payload, instructions_hash)
                if self.valves.OUTPUT_FORMAT == "thought_wrapped":
                    result = self._wrap_thought_content(result)
                return result
            except ChainResetError:
                log.error("Chain reset retry also failed for chat %s", chat_id)
                if self.valves.FALLBACK_TO_CHAT_COMPLETIONS:
                    return self._fallback_chat_completions_sync(messages, body)
                return "Error: Failed to establish conversation chain. Please start a new chat."
        except Exception as e:
            log.error("Unexpected error for chat %s: %s", chat_id, e)
            if self.valves.FALLBACK_TO_CHAT_COMPLETIONS:
                return self._fallback_chat_completions_sync(messages, body)
            return f"Error: {e}"

    def _make_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.valves.API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.API_KEY}"
        # Add extra headers (forwarded from Open WebUI metadata)
        headers.update(self._extra_headers)
        return headers

    def _responses_url(self) -> str:
        return f"{self.valves.BASE_URL.rstrip('/')}/v1/responses"

    def _read_error_body(self, resp) -> str:
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

    def _call_responses(self, chat_id: str, payload: dict, instructions_hash: str) -> str:
        """Non-streaming /v1/responses call."""
        with httpx.Client(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
            resp = client.post(
                self._responses_url(), json=payload, headers=self._make_headers()
            )

            if resp.status_code != 200:
                detail = self._read_error_body(resp)
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

    def _fallback_chat_completions_sync(self, messages: list, body: dict) -> str:
        """Fallback to /v1/chat/completions endpoint."""
        url = f"{self.valves.BASE_URL.rstrip('/')}/v1/chat/completions"

        # Extract user info and cookies from body
        __user__ = body.get("user", {})
        __cookies__ = body.get("cookies", {})
        __user_id__ = __user__.get("id", "")

        # Inject context into the last user message
        if messages:
            messages = list(messages)  # Make a copy
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    content = messages[i].get("content", "")
                    if isinstance(content, str):
                        messages[i] = {
                            **messages[i],
                            "content": self._inject_context(content, __user__, __user_id__, __cookies__)
                        }
                    break

        payload = {
            "model": self.valves.MODEL,
            "messages": messages,
            "stream": False,
        }

        with httpx.Client(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
            resp = client.post(url, json=payload, headers=self._make_headers())
            if resp.status_code != 200:
                return f"Error: Fallback also failed ({resp.status_code})"
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if self.valves.OUTPUT_FORMAT == "thought_wrapped":
                    content = self._wrap_thought_content(content)
                return content
            return "Error: No response from fallback"

    def _extract_instructions(self, messages: list) -> str:
        """Extract system/developer messages from the leading position only."""
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
        """Extract the latest user message as a string or Responses API input array."""
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
