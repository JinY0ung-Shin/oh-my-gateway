"""OpenCode backend client.

Wraps the OpenCode headless HTTP server into the gateway ``BackendClient``
protocol.  The official OpenCode SDK is TypeScript; this Python backend talks
to the same server API directly with httpx.
"""

from __future__ import annotations

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

import src.mcp_config as mcp_config
from src.backends.opencode.auth import OpenCodeAuthProvider
from src.backends.opencode.config import build_opencode_config, parse_opencode_config_content
from src.backends.opencode.constants import OPENCODE_MODELS, use_wrapper_mcp_config
from src.backends.opencode.events import OpenCodeEventConverter
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
        mode = "managed" if self._process is not None else "external"
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
        base_config = parse_opencode_config_content(os.getenv("OPENCODE_CONFIG_CONTENT"))
        mcp_servers = mcp_config.get_validated_mcp_config() if use_wrapper_mcp_config() else {}
        config = build_opencode_config(
            base_config=base_config,
            mcp_servers=mcp_servers,
            default_model=os.getenv("OPENCODE_DEFAULT_MODEL") or None,
            question_permission=os.getenv("OPENCODE_QUESTION_PERMISSION", "ask"),
        )
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

    def _question_reply_body(self, output: str) -> Dict[str, Any]:
        answers: list[list[str]]
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = output

        if (
            isinstance(parsed, list)
            and all(isinstance(item, list) for item in parsed)
            and all(isinstance(answer, str) for item in parsed for answer in item)
        ):
            answers = [[answer for answer in item] for item in parsed]
        elif isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            answers = [[item for item in parsed]]
        else:
            answers = [[str(output)]]
        return {"answers": answers}

    def _describe_http_error(self, response: Any) -> str:
        status = getattr(response, "status_code", "unknown")
        body = (getattr(response, "text", "") or "")[:500].replace("\n", "\\n")
        return f"OpenCode HTTP {status}: {body}"

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

    async def _run_completion_streaming(
        self,
        client: OpenCodeSessionClient,
        prompt: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        body = self._prompt_body(client, prompt)
        converter = OpenCodeEventConverter(session_id=client.session_id)

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
                        error_message = converter.error_message(event)
                        if error_message:
                            yield {
                                "type": "error",
                                "is_error": True,
                                "error_message": error_message,
                            }
                            return
                        if converter.finished(event):
                            break
                        for chunk in converter.convert(event):
                            yield chunk
        except Exception as exc:
            logger.error("OpenCode streaming prompt failed: %s", exc, exc_info=True)
            yield {"type": "error", "is_error": True, "error_message": str(exc)}
            return

        text = converter.final_text()
        usage = converter.usage
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

    async def resume_question_with_client(
        self,
        client: OpenCodeSessionClient,
        call_id: str,
        output: str,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Resume an OpenCode question tool call with the user's answer."""
        _ = session
        request_id = call_id
        converter = OpenCodeEventConverter(session_id=client.session_id)
        try:
            async with httpx.AsyncClient(**self._event_client_kwargs()) as event_client:
                async with event_client.stream(
                    "GET",
                    "/event",
                    params=self._directory_params(client.cwd),
                ) as event_response:
                    event_response.raise_for_status()
                    async with httpx.AsyncClient(**self._client_kwargs()) as reply_client:
                        response = await reply_client.post(
                            f"/question/{request_id}/reply",
                            json=self._question_reply_body(output),
                            params=self._directory_params(client.cwd),
                        )
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError:
                            raise RuntimeError(self._describe_http_error(response)) from None

                    async for event in self._iter_sse_events(event_response):
                        error_message = converter.error_message(event)
                        if error_message:
                            yield {
                                "type": "error",
                                "is_error": True,
                                "error_message": error_message,
                            }
                            return
                        if converter.finished(event):
                            break
                        for chunk in converter.convert(event):
                            yield chunk
        except Exception as exc:
            logger.error("OpenCode question continuation failed: %s", exc, exc_info=True)
            yield {"type": "error", "is_error": True, "error_message": str(exc)}
            return

        text = converter.final_text()
        yield {"type": "assistant", "content": [{"type": "text", "text": text}]}
        yield {"type": "result", "subtype": "success", "result": text}

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
