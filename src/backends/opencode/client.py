"""OpenCode backend client.

Wraps the OpenCode headless HTTP server into the gateway ``BackendClient``
protocol.  The official OpenCode SDK is TypeScript; this Python backend talks
to the same server API directly with httpx.

Two operating modes:

- **managed** (default) — the gateway spawns ``opencode serve`` as a
  subprocess and feeds it a generated config built from
  ``OPENCODE_CONFIG_CONTENT`` and (optionally) the wrapper's ``MCP_CONFIG``.
- **external** — when ``OPENCODE_BASE_URL`` is set, the gateway points its
  HTTP client at that URL instead of starting a subprocess.  The external
  server owns its own config, so wrapper-side options that affect server
  startup (``OPENCODE_CONFIG_CONTENT``, ``OPENCODE_USE_WRAPPER_MCP_CONFIG``,
  ``OPENCODE_BIN``, ``OPENCODE_HOST``, ``OPENCODE_PORT``,
  ``OPENCODE_START_TIMEOUT_MS``) become no-ops.  Request-time parameters
  (``OPENCODE_AGENT``, ``OPENCODE_DEFAULT_MODEL``,
  ``OPENCODE_QUESTION_PERMISSION``, ``OPENCODE_MODELS``) and basic-auth
  credentials (``OPENCODE_SERVER_USERNAME`` / ``OPENCODE_SERVER_PASSWORD``)
  still apply.
"""

from __future__ import annotations

import asyncio
import base64
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

_ATTACHED_IMAGE_RE = re.compile(r'<attached_image\s+path="([^"]+)"\s*/>')
_IMAGE_FILE_RE = re.compile(r"^img_[0-9a-f]{16}\.(?:png|jpg|jpeg|gif|webp)$")
_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_PERMISSION_REPLIES = {"once", "always", "reject"}


@dataclass
class OpenCodeSessionClient:
    """Lightweight handle for one OpenCode session."""

    session_id: str
    cwd: Optional[str]
    model: Optional[str]
    system_prompt: Optional[str]
    stream_events: bool = False
    base_url: Optional[str] = None
    timeout: Optional[float] = None
    auth: Optional[httpx.Auth] = None

    async def disconnect(self) -> None:
        """Delete the OpenCode session when the gateway session is cleaned up."""
        if not self.base_url:
            return
        kwargs: Dict[str, Any] = {"base_url": self.base_url}
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        if self.auth is not None:
            kwargs["auth"] = self.auth

        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.delete(
                    f"/session/{self.session_id}",
                    params={"directory": self.cwd} if self.cwd else None,
                )
                if getattr(response, "status_code", None) == 404:
                    return
                response.raise_for_status()
        except Exception:
            logger.warning("OpenCode session delete failed for %s", self.session_id, exc_info=True)


class OpenCodeClient:
    """BackendClient implementation for OpenCode."""

    def __init__(
        self,
        timeout: Optional[int] = None,
    ) -> None:
        self.timeout = (timeout if timeout is not None else DEFAULT_TIMEOUT_MS) / 1000
        self._process: Optional[subprocess.Popen[str]] = None
        self._server_username = os.getenv("OPENCODE_SERVER_USERNAME", "opencode")
        self._server_password = os.getenv("OPENCODE_SERVER_PASSWORD")
        self._agent = os.getenv("OPENCODE_AGENT", "general").strip() or "general"

        external_url = os.getenv("OPENCODE_BASE_URL")
        if external_url:
            self._mode = "external"
            self.base_url = external_url.rstrip("/")
            logger.info(
                "OpenCode backend in external mode: %s "
                "(OPENCODE_CONFIG_CONTENT and OPENCODE_USE_WRAPPER_MCP_CONFIG "
                "are no-ops; the external server owns its config)",
                self.base_url,
            )
        else:
            self._mode = "managed"
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
        return {
            "mode": self._mode,
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
            f"Timeout waiting for OpenCode server after {timeout_ms}ms: {''.join(output).strip()}"
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
            base_url=self.base_url,
            timeout=self.timeout,
            auth=self._auth(),
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
            "parts": self._prompt_parts(prompt, client.cwd),
        }
        model = self._split_provider_model(client.model)
        if model:
            body["model"] = model
        if client.system_prompt:
            body["system"] = client.system_prompt
        return body

    async def _prompt_body_async(
        self,
        client: OpenCodeSessionClient,
        prompt: str,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self._prompt_body, client, prompt)

    def _prompt_parts(self, prompt: str, cwd: Optional[str]) -> List[Dict[str, Any]]:
        parts: List[Dict[str, Any]] = []
        cursor = 0
        for match in _ATTACHED_IMAGE_RE.finditer(prompt):
            raw_path = match.group(1)
            file_part = self._file_part_from_path(raw_path, cwd)
            if file_part is None:
                logger.warning("OpenCode dropped untrusted attached_image marker: %s", raw_path)
                continue
            if match.start() > cursor:
                parts.append({"type": "text", "text": prompt[cursor : match.start()]})
            parts.append(file_part)
            cursor = match.end()

        if cursor < len(prompt) or not parts:
            parts.append({"type": "text", "text": prompt[cursor:]})
        return parts

    def _trusted_attached_image_path(self, raw_path: str, cwd: Optional[str]) -> Optional[Path]:
        if not cwd:
            return None
        try:
            path = Path(raw_path).resolve(strict=True)
            image_dir = (Path(cwd).resolve() / ".claude_images").resolve()
        except (OSError, RuntimeError):
            return None
        if not path.is_file() or path.parent != image_dir:
            return None
        if path.suffix.lower() not in _IMAGE_MIME_BY_SUFFIX:
            return None
        if not _IMAGE_FILE_RE.fullmatch(path.name):
            return None
        return path

    def _file_part_from_path(self, raw_path: str, cwd: Optional[str]) -> Optional[Dict[str, Any]]:
        path = self._trusted_attached_image_path(raw_path, cwd)
        if path is None:
            return None
        mime = _IMAGE_MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "type": "file",
            "mime": mime,
            "filename": path.name,
            "url": f"data:{mime};base64,{encoded}",
        }

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

    def _permission_reply_body(self, output: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = output

        message: Optional[str] = None
        candidates: list[str] = []
        if isinstance(parsed, dict):
            for key in ("reply", "answer", "output"):
                value = parsed.get(key)
                if isinstance(value, str):
                    candidates.append(value)
            value = parsed.get("message")
            if isinstance(value, str) and value:
                message = value
        elif isinstance(parsed, list):
            candidates.extend(item for item in parsed if isinstance(item, str))
        elif isinstance(parsed, str):
            candidates.append(parsed)

        reply: Optional[str] = None
        for candidate in candidates:
            normalized = candidate.strip().lower()
            if normalized in _PERMISSION_REPLIES:
                reply = normalized
                break
            if normalized in {"yes", "y", "allow", "approve", "approved", "ok", "okay"}:
                reply = "once"
                break
            if normalized in {"no", "n", "deny", "denied", "reject", "rejected"}:
                reply = "reject"
                break

        body = {"reply": reply or "reject"}
        if message:
            body["message"] = message
        return body

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
        body = await self._prompt_body_async(client, prompt)
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

        body = await self._prompt_body_async(client, prompt)

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

    async def _resume_with_client(
        self,
        client: OpenCodeSessionClient,
        reply_path: str,
        reply_body: Dict[str, Any],
        session: Any,
        label: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        _ = session
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
                            reply_path,
                            json=reply_body,
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
            logger.error("OpenCode %s continuation failed: %s", label, exc, exc_info=True)
            yield {"type": "error", "is_error": True, "error_message": str(exc)}
            return

        text = converter.final_text()
        yield {"type": "assistant", "content": [{"type": "text", "text": text}]}
        yield {"type": "result", "subtype": "success", "result": text}

    async def resume_question_with_client(
        self,
        client: OpenCodeSessionClient,
        call_id: str,
        output: str,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Resume an OpenCode question tool call with the user's answer."""
        async for chunk in self._resume_with_client(
            client,
            f"/question/{call_id}/reply",
            self._question_reply_body(output),
            session,
            "question",
        ):
            yield chunk

    async def resume_permission_with_client(
        self,
        client: OpenCodeSessionClient,
        call_id: str,
        output: str,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Resume an OpenCode permission prompt with the user's decision."""
        async for chunk in self._resume_with_client(
            client,
            f"/permission/{call_id}/reply",
            self._permission_reply_body(output),
            session,
            "permission",
        ):
            yield chunk

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
