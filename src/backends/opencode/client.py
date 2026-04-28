"""OpenCode backend client.

Wraps the OpenCode headless HTTP server into the gateway ``BackendClient``
protocol.  The official OpenCode SDK is TypeScript; this Python backend talks
to the same server API directly with httpx.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from src.backends.opencode.auth import OpenCodeAuthProvider
from src.backends.opencode.constants import OPENCODE_MODELS
from src.constants import DEFAULT_TIMEOUT_MS

logger = logging.getLogger(__name__)


@dataclass
class OpenCodeSessionClient:
    """Lightweight handle for one OpenCode session."""

    session_id: str
    cwd: Optional[str]
    model: Optional[str]
    system_prompt: Optional[str]
    stream_events: bool = False

    async def disconnect(self) -> None:
        """Compatibility hook for SessionManager cleanup."""
        return None


@dataclass
class OpenCodeStreamState:
    """Mutable state used while converting OpenCode events to gateway chunks."""

    text_by_part: Dict[str, str]
    text_parts: List[str]
    emitted_tool_uses: set[str]
    emitted_tool_results: set[str]
    usage: Optional[Dict[str, int]]
    saw_activity: bool


class OpenCodeClient:
    """BackendClient implementation for OpenCode."""

    def __init__(
        self,
        timeout: Optional[int] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.timeout = (timeout if timeout is not None else DEFAULT_TIMEOUT_MS) / 1000
        self.base_url = (base_url or os.getenv("OPENCODE_BASE_URL") or "").rstrip("/")
        self._process: Optional[subprocess.Popen[str]] = None
        self._server_username = os.getenv("OPENCODE_SERVER_USERNAME", "opencode")
        self._server_password = os.getenv("OPENCODE_SERVER_PASSWORD")
        self._agent = os.getenv("OPENCODE_AGENT", "general").strip() or "general"

        if not self.base_url:
            self.base_url = self._start_managed_server()

    @property
    def name(self) -> str:
        return "opencode"

    def supported_models(self) -> List[str]:
        return list(OPENCODE_MODELS)

    def get_auth_provider(self):
        return OpenCodeAuthProvider()

    def runtime_metadata(self) -> Dict[str, Any]:
        """Return operational details for admin diagnostics."""
        mode = "external" if os.getenv("OPENCODE_BASE_URL") else "managed"
        return {
            "mode": mode,
            "base_url": self.base_url,
            "agent": self._agent,
            "models": self.supported_models(),
            "managed_process": self._process is not None,
        }

    def _auth(self) -> Optional[httpx.BasicAuth]:
        if not self._server_password:
            return None
        return httpx.BasicAuth(self._server_username, self._server_password)

    def _managed_config_content(self) -> str:
        existing = os.getenv("OPENCODE_CONFIG_CONTENT")
        if existing:
            return existing

        config: Dict[str, Any] = {
            "permission": {"question": "deny"},
            "share": "disabled",
        }
        default_model = os.getenv("OPENCODE_DEFAULT_MODEL")
        if default_model:
            config["model"] = default_model
        return json.dumps(config)

    def _start_managed_server(self) -> str:
        binary = os.getenv("OPENCODE_BIN", "opencode")
        host = os.getenv("OPENCODE_HOST", "127.0.0.1")
        port = os.getenv("OPENCODE_PORT", "0")
        timeout_ms = int(os.getenv("OPENCODE_START_TIMEOUT_MS", "5000"))
        command = [binary, "serve", "--hostname", host, "--port", str(port)]
        env = {
            **os.environ,
            "OPENCODE_CONFIG_CONTENT": self._managed_config_content(),
        }

        proc = subprocess.Popen(  # noqa: S603 - binary is operator-configured
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self._process = proc

        if proc.stdout is None:
            self.close()
            raise RuntimeError("OpenCode server stdout is unavailable")

        deadline = time.monotonic() + timeout_ms / 1000
        output: list[str] = []
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"OpenCode server exited with code {proc.returncode}: {''.join(output).strip()}"
                )
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([proc.stdout], [], [], remaining)
            if not readable:
                continue
            line = proc.stdout.readline()
            if not line:
                continue
            output.append(line)
            match = re.search(r"opencode server listening on\s+(https?://\S+)", line)
            if match:
                return match.group(1).rstrip("/")

        self.close()
        raise TimeoutError(
            f"Timeout waiting for OpenCode server after {timeout_ms}ms: "
            f"{''.join(output).strip()}"
        )

    def close(self) -> None:
        """Stop a managed OpenCode server if this backend owns one."""
        proc = self._process
        if proc is None:
            return
        self._process = None
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    shutdown = close

    def _client_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"base_url": self.base_url, "timeout": self.timeout}
        auth = self._auth()
        if auth is not None:
            kwargs["auth"] = auth
        return kwargs

    def _event_client_kwargs(self) -> Dict[str, Any]:
        kwargs = self._client_kwargs()
        kwargs["timeout"] = httpx.Timeout(
            connect=self.timeout,
            read=None,
            write=self.timeout,
            pool=self.timeout,
        )
        return kwargs

    def _directory_params(self, cwd: Optional[str]) -> Optional[Dict[str, str]]:
        if not cwd:
            return None
        return {"directory": cwd}

    def _combine_system_prompt(
        self,
        custom_base: Optional[str],
        system_prompt: Optional[str],
    ) -> Optional[str]:
        if custom_base and system_prompt:
            return f"{custom_base}\n\n{system_prompt}"
        return custom_base or system_prompt

    def _split_provider_model(self, model: Optional[str]) -> Optional[Dict[str, str]]:
        if not model or "/" not in model:
            return None
        provider_id, model_id = model.split("/", 1)
        return {"providerID": provider_id, "modelID": model_id}

    async def verify(self) -> bool:
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                response = await client.get("/global/health")
                response.raise_for_status()
                payload = response.json()
                return bool(payload.get("healthy", True))
        except Exception as exc:
            logger.error("OpenCode backend verification failed: %s", exc)
            return False

    async def create_client(
        self,
        *,
        session: Any,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        _custom_base: Any = None,
    ) -> OpenCodeSessionClient:
        _ = (allowed_tools, disallowed_tools, permission_mode, mcp_servers, task_budget, extra_env)
        opencode_session_id = getattr(session, "opencode_session_id", None)
        if opencode_session_id is None:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                response = await client.post(
                    "/session",
                    json={"title": session.session_id},
                    params=self._directory_params(cwd),
                )
                response.raise_for_status()
                payload = response.json()
            opencode_session_id = payload["id"]
            setattr(session, "opencode_session_id", opencode_session_id)

        return OpenCodeSessionClient(
            session_id=opencode_session_id,
            cwd=str(Path(cwd)) if cwd else None,
            model=model,
            system_prompt=self._combine_system_prompt(_custom_base, system_prompt),
        )

    def _extract_text(self, payload: Dict[str, Any]) -> str:
        parts = payload.get("parts")
        if not isinstance(parts, list):
            return ""
        text_parts = [
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        return "".join(text_parts)

    def _extract_usage(self, payload: Dict[str, Any]) -> Optional[Dict[str, int]]:
        info = payload.get("info")
        if not isinstance(info, dict):
            return None
        tokens = info.get("tokens")
        if not isinstance(tokens, dict):
            return None
        input_tokens = int(tokens.get("input") or 0)
        output_tokens = int(tokens.get("output") or 0)
        reasoning_tokens = int(tokens.get("reasoning") or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens + reasoning_tokens,
        }

    def _describe_non_json_response(self, response: Any) -> str:
        status = getattr(response, "status_code", "unknown")
        headers = getattr(response, "headers", {}) or {}
        content_type = headers.get("content-type", "unknown")
        body = (getattr(response, "text", "") or "")[:200].replace("\n", "\\n")
        return (
            "OpenCode returned an empty or non-JSON response "
            f"(status={status}, content-type={content_type}, body={body!r})"
        )

    def _prompt_body(self, client: OpenCodeSessionClient, prompt: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "agent": self._agent,
            "parts": [{"type": "text", "text": prompt}],
        }
        model = self._split_provider_model(client.model)
        if model:
            body["model"] = model
        if client.system_prompt:
            body["system"] = client.system_prompt
        return body

    async def _iter_sse_events(self, response: Any) -> AsyncGenerator[Dict[str, Any], None]:
        event_type: Optional[str] = None
        data_lines: list[str] = []

        def flush() -> Optional[Dict[str, Any]]:
            if not data_lines:
                return None
            raw = "\n".join(data_lines)
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Ignoring non-JSON OpenCode SSE data: %r", raw[:200])
                return None
            if event_type and isinstance(event, dict) and not event.get("type"):
                event["type"] = event_type
            return event if isinstance(event, dict) else None

        async for line in response.aiter_lines():
            if line == "":
                event = flush()
                if event:
                    yield event
                event_type = None
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
                continue
            if line.startswith("{"):
                data_lines.append(line)

        event = flush()
        if event:
            yield event

    def _text_delta_chunk(self, delta: str) -> Dict[str, Any]:
        return {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": delta},
            },
        }

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

    def _event_finished(
        self,
        event: Dict[str, Any],
        session_id: str,
        state: OpenCodeStreamState,
    ) -> bool:
        if event.get("type") != "session.idle":
            return False
        event_session = self._event_session_id(event)
        return event_session == session_id and state.saw_activity

    def _event_error_message(self, event: Dict[str, Any], session_id: str) -> Optional[str]:
        if event.get("type") != "session.error":
            return None
        event_session = self._event_session_id(event)
        if event_session not in (None, session_id):
            return None
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        error = props.get("error") or props.get("message") or props
        return str(error)

    def _convert_text_event(
        self,
        event: Dict[str, Any],
        state: OpenCodeStreamState,
    ) -> Optional[Dict[str, Any]]:
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
                state.text_by_part[part_id] = state.text_by_part.get(part_id, "") + delta
            state.text_parts.append(delta)
            state.saw_activity = True
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
                state.text_by_part[part_id] = state.text_by_part.get(part_id, "") + delta
            state.text_parts.append(delta)
            state.saw_activity = True
            return self._text_delta_chunk(delta)

        text = part.get("text")
        if not isinstance(text, str) or not text:
            return None
        previous = state.text_by_part.get(part_id, "") if part_id else ""
        if previous and text.startswith(previous):
            computed_delta = text[len(previous) :]
        elif text != previous:
            computed_delta = text
        else:
            computed_delta = ""
        if part_id:
            state.text_by_part[part_id] = text
        if not computed_delta:
            return None
        state.text_parts.append(computed_delta)
        state.saw_activity = True
        return self._text_delta_chunk(computed_delta)

    def _convert_usage_event(
        self,
        event: Dict[str, Any],
        state: OpenCodeStreamState,
    ) -> None:
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
        state.usage = {
            "input_tokens": (state.usage or {}).get("input_tokens", 0) + input_tokens,
            "output_tokens": (state.usage or {}).get("output_tokens", 0) + output_tokens,
            "total_tokens": (
                (state.usage or {}).get("total_tokens", 0)
                + input_tokens
                + output_tokens
                + reasoning_tokens
            ),
        }
        state.saw_activity = True

    def _convert_tool_event(
        self,
        event: Dict[str, Any],
        state: OpenCodeStreamState,
    ) -> list[Dict[str, Any]]:
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
        chunks: list[Dict[str, Any]] = []

        input_value = tool_state.get("input")
        has_input = bool(input_value)
        should_emit_use = (
            status in ("running", "completed", "error")
            or (status == "pending" and has_input)
        )
        if should_emit_use and call_id not in state.emitted_tool_uses:
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
            state.emitted_tool_uses.add(call_id)
            state.saw_activity = True

        if status in ("completed", "error") and call_id not in state.emitted_tool_results:
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
            state.emitted_tool_results.add(call_id)
            state.saw_activity = True

        return chunks

    def _convert_opencode_event(
        self,
        event: Dict[str, Any],
        client: OpenCodeSessionClient,
        state: OpenCodeStreamState,
    ) -> list[Dict[str, Any]]:
        event_session = self._event_session_id(event)
        if event_session != client.session_id:
            return []

        chunks: list[Dict[str, Any]] = []
        self._convert_usage_event(event, state)
        text_chunk = self._convert_text_event(event, state)
        if text_chunk:
            chunks.append(text_chunk)
        chunks.extend(self._convert_tool_event(event, state))
        return chunks

    async def _run_completion_streaming(
        self,
        client: OpenCodeSessionClient,
        prompt: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        body = self._prompt_body(client, prompt)
        state = OpenCodeStreamState(
            text_by_part={},
            text_parts=[],
            emitted_tool_uses=set(),
            emitted_tool_results=set(),
            usage=None,
            saw_activity=False,
        )

        try:
            async with httpx.AsyncClient(**self._event_client_kwargs()) as event_client:
                async with event_client.stream(
                    "GET",
                    "/event",
                    params=self._directory_params(client.cwd),
                ) as event_response:
                    event_response.raise_for_status()
                    async with httpx.AsyncClient(**self._client_kwargs()) as prompt_client:
                        response = await prompt_client.post(
                            f"/session/{client.session_id}/prompt_async",
                            json=body,
                            params=self._directory_params(client.cwd),
                        )
                        response.raise_for_status()

                    async for event in self._iter_sse_events(event_response):
                        error_message = self._event_error_message(event, client.session_id)
                        if error_message:
                            yield {
                                "type": "error",
                                "is_error": True,
                                "error_message": error_message,
                            }
                            return
                        if self._event_finished(event, client.session_id, state):
                            break
                        for chunk in self._convert_opencode_event(event, client, state):
                            yield chunk
        except Exception as exc:
            logger.error("OpenCode streaming prompt failed: %s", exc, exc_info=True)
            yield {"type": "error", "is_error": True, "error_message": str(exc)}
            return

        text = "".join(state.text_parts)
        assistant: Dict[str, Any] = {
            "type": "assistant",
            "content": [{"type": "text", "text": text}],
        }
        result: Dict[str, Any] = {"type": "result", "subtype": "success", "result": text}
        if state.usage:
            assistant["usage"] = {
                "input_tokens": state.usage["input_tokens"],
                "output_tokens": state.usage["output_tokens"],
            }
            result["usage"] = {
                "input_tokens": state.usage["input_tokens"],
                "output_tokens": state.usage["output_tokens"],
            }
        yield assistant
        yield result

    async def run_completion_with_client(
        self,
        client: OpenCodeSessionClient,
        prompt: str,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        _ = session
        if client.stream_events:
            async for chunk in self._run_completion_streaming(client, prompt):
                yield chunk
            return

        body = self._prompt_body(client, prompt)

        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as http_client:
                response = await http_client.post(
                    f"/session/{client.session_id}/message",
                    json=body,
                    params=self._directory_params(client.cwd),
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except json.JSONDecodeError:
                    error_message = self._describe_non_json_response(response)
                    logger.error("OpenCode session prompt returned invalid JSON: %s", error_message)
                    yield {"type": "error", "is_error": True, "error_message": error_message}
                    return
        except Exception as exc:
            logger.error("OpenCode session prompt failed: %s", exc, exc_info=True)
            yield {"type": "error", "is_error": True, "error_message": str(exc)}
            return

        info = payload.get("info")
        if isinstance(info, dict) and info.get("error"):
            yield {
                "type": "error",
                "is_error": True,
                "error_message": str(info["error"]),
            }
            return

        text = self._extract_text(payload)
        usage = self._extract_usage(payload)
        assistant: Dict[str, Any] = {
            "type": "assistant",
            "content": [{"type": "text", "text": text}],
        }
        result: Dict[str, Any] = {"type": "result", "subtype": "success", "result": text}
        if usage:
            assistant["usage"] = {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }
            result["usage"] = {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }

        yield assistant
        yield result

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        for message in reversed(messages):
            if message.get("type") == "result" and message.get("result"):
                return message["result"]
        parts: list[str] = []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    parts.append(part["text"])
        return "".join(parts) if parts else None

    def estimate_token_usage(
        self,
        prompt: str,
        completion: str,
        model: Optional[str] = None,
    ) -> Dict[str, int]:
        _ = model
        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(1, len(completion) // 4)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
