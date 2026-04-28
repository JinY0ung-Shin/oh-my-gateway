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

    async def run_completion_with_client(
        self,
        client: OpenCodeSessionClient,
        prompt: str,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        _ = session
        body: Dict[str, Any] = {
            "agent": self._agent,
            "parts": [{"type": "text", "text": prompt}],
        }
        model = self._split_provider_model(client.model)
        if model:
            body["model"] = model
        if client.system_prompt:
            body["system"] = client.system_prompt

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
