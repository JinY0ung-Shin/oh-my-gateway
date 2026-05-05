"""Codex app-server backend client.

The official Python SDK currently wraps the same ``codex app-server``
JSON-RPC protocol.  The package is experimental and may not be available from
PyPI, so this backend keeps a small protocol client in-tree for the MVP.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, Iterable, Iterator, List, Optional

from src.backends.codex.auth import CodexAuthProvider
from src.backends.codex.constants import (
    CODEX_MODELS,
    approval_policy,
    codex_bin,
    configured_config_overrides,
    sandbox_mode,
)
from src.constants import DEFAULT_TIMEOUT_MS
from src.message_adapter import MessageAdapter

logger = logging.getLogger(__name__)

CODEX_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"


class CodexAppServerError(RuntimeError):
    """Raised when the Codex app-server JSON-RPC transport fails."""


class CodexJsonRpcClient:
    """Minimal JSON-RPC client for ``codex app-server --listen stdio://``."""

    def __init__(
        self,
        *,
        binary: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        config_overrides: Optional[Iterable[str]] = None,
        read_timeout: Optional[float] = None,
    ) -> None:
        self.binary = binary or codex_bin()
        self.cwd = cwd
        self.env = env or {}
        self.config_overrides = list(config_overrides or [])
        self.read_timeout = read_timeout
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._pending_notifications: deque[dict[str, Any]] = deque()
        self._stdout_queue: queue.Queue[Optional[str]] = queue.Queue()
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_lines: deque[str] = deque(maxlen=400)
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._proc is not None:
            return
        args = [self.binary]
        for override in self.config_overrides:
            args.extend(["--config", override])
        args.extend(["app-server", "--listen", "stdio://"])

        # Inherit the gateway environment so Codex CLI auth/runtime settings
        # such as OPENAI_API_KEY and CODEX_HOME remain available. Request
        # metadata is allowlisted separately and overlaid below.
        proc_env = os.environ.copy()
        proc_env.update(self.env)
        self._proc = subprocess.Popen(  # noqa: S603 - binary is operator-configured
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.cwd,
            env=proc_env,
            bufsize=1,
        )
        self._stdout_queue = queue.Queue()
        self._start_stdout_drain_thread()
        self._start_stderr_drain_thread()
        self._initialize()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        if proc.stdin:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if self._stdout_thread and self._stdout_thread.is_alive():
            self._stdout_thread.join(timeout=0.5)
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.5)

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "oh_my_gateway",
                    "title": "Oh My Gateway",
                    "version": "0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized", {})

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        request_id = str(uuid.uuid4())
        self._write_message({"id": request_id, "method": method, "params": params or {}})
        while True:
            msg = self._read_message()
            if "method" in msg and "id" in msg:
                if msg.get("method") in CODEX_APPROVAL_METHODS:
                    self._pending_notifications.append(msg)
                    continue
                self._write_message({"id": msg["id"], "result": self._handle_server_request(msg)})
                continue
            if "method" in msg and "id" not in msg:
                self._pending_notifications.append(msg)
                continue
            if msg.get("id") != request_id:
                continue
            if "error" in msg:
                error = msg["error"]
                if isinstance(error, dict):
                    raise CodexAppServerError(str(error.get("message", "Codex app-server error")))
                raise CodexAppServerError("Codex app-server error")
            return msg.get("result")

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._write_message({"method": method, "params": params or {}})

    def next_notification(self) -> dict[str, Any]:
        if self._pending_notifications:
            return self._pending_notifications.popleft()
        while True:
            msg = self._read_message()
            if "method" in msg and "id" in msg:
                if msg.get("method") in CODEX_APPROVAL_METHODS:
                    return msg
                self._write_message({"id": msg["id"], "result": self._handle_server_request(msg)})
                continue
            if "method" in msg and "id" not in msg:
                return msg

    def respond(self, request_id: Any, result: Dict[str, Any]) -> None:
        self._write_message({"id": request_id, "result": result})

    def thread_start(self, params: Dict[str, Any]) -> Dict[str, Any]:
        result = self.request("thread/start", params)
        if not isinstance(result, dict):
            raise CodexAppServerError("thread/start response must be an object")
        return result

    def thread_resume(self, thread_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        result = self.request("thread/resume", {"threadId": thread_id, **params})
        if not isinstance(result, dict):
            raise CodexAppServerError("thread/resume response must be an object")
        return result

    def turn_start(
        self,
        thread_id: str,
        input_items: list[Dict[str, Any]],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = self.request(
            "turn/start",
            {"threadId": thread_id, "input": input_items, **params},
        )
        if not isinstance(result, dict):
            raise CodexAppServerError("turn/start response must be an object")
        return result

    def model_list(self) -> Dict[str, Any]:
        result = self.request("model/list", {"includeHidden": False})
        if not isinstance(result, dict):
            raise CodexAppServerError("model/list response must be an object")
        return result

    def _handle_server_request(self, msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "cancel"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        return {}

    def _write_message(self, payload: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise CodexAppServerError("Codex app-server is not running")
        with self._lock:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()

    def _read_message(self) -> Dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise CodexAppServerError("Codex app-server is not running")
        try:
            if self.read_timeout is None:
                line = self._stdout_queue.get()
            else:
                line = self._stdout_queue.get(timeout=self.read_timeout)
        except queue.Empty as exc:
            raise CodexAppServerError(
                "Timed out waiting for Codex app-server message "
                f"after {self.read_timeout:.3g}s. stderr_tail={self._stderr_tail()[:2000]}"
            ) from exc
        if line is None:
            raise CodexAppServerError(
                f"Codex app-server closed stdout. stderr_tail={self._stderr_tail()[:2000]}"
            )
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAppServerError(f"Invalid Codex JSON-RPC line: {line!r}") from exc
        if not isinstance(message, dict):
            raise CodexAppServerError(f"Invalid Codex JSON-RPC payload: {message!r}")
        return message

    def _start_stdout_drain_thread(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        stdout = self._proc.stdout

        def _drain() -> None:
            try:
                for line in stdout:
                    self._stdout_queue.put(line)
            finally:
                self._stdout_queue.put(None)

        self._stdout_thread = threading.Thread(target=_drain, daemon=True)
        self._stdout_thread.start()

    def _start_stderr_drain_thread(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return

        def _drain() -> None:
            if self._proc is None or self._proc.stderr is None:
                return
            for line in self._proc.stderr:
                self._stderr_lines.append(line.rstrip("\n"))

        self._stderr_thread = threading.Thread(target=_drain, daemon=True)
        self._stderr_thread.start()

    def _stderr_tail(self, limit: int = 40) -> str:
        return "\n".join(list(self._stderr_lines)[-limit:])


@dataclass
class CodexSessionClient:
    """Handle for one gateway session mapped to one Codex thread."""

    rpc: CodexJsonRpcClient
    thread_id: str
    model: Optional[str]
    cwd: Optional[str]
    stream_events: bool = False
    env: Optional[Dict[str, str]] = None
    owns_rpc: bool = False
    pending_approval_request_id: Optional[Any] = None
    pending_approval_method: Optional[str] = None
    pending_approval_turn_id: Optional[str] = None
    pending_approval_params: Optional[Dict[str, Any]] = None

    async def disconnect(self) -> None:
        if self.owns_rpc:
            await asyncio.to_thread(self.rpc.close)


class CodexClient:
    """BackendClient implementation for local Codex app-server."""

    def __init__(self, timeout: Optional[int] = None) -> None:
        self.timeout = (timeout if timeout is not None else DEFAULT_TIMEOUT_MS) / 1000
        self._rpc: Optional[CodexJsonRpcClient] = None
        self._rpc_env: Dict[str, str] = {}
        self._rpc_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "codex"

    def supported_models(self) -> List[str]:
        return list(CODEX_MODELS)

    def get_auth_provider(self) -> CodexAuthProvider:
        return CodexAuthProvider()

    def runtime_metadata(self) -> Dict[str, Any]:
        return {
            "mode": "app-server",
            "models": self.supported_models(),
            "approval_policy": approval_policy(),
            "sandbox": sandbox_mode(),
            "shared_process": self._rpc_is_usable(self._rpc),
        }

    def close(self) -> None:
        rpc = self._rpc
        self._rpc = None
        self._rpc_env = {}
        if rpc is not None:
            rpc.close()

    shutdown = close

    async def verify(self) -> bool:
        rpc = CodexJsonRpcClient(
            config_overrides=configured_config_overrides(),
            read_timeout=self.timeout,
        )
        try:
            await asyncio.to_thread(rpc.start)
            payload = await asyncio.to_thread(rpc.model_list)
            return isinstance(payload.get("data"), list)
        except Exception as exc:
            logger.error("Codex backend verification failed: %s", exc)
            return False
        finally:
            await asyncio.to_thread(rpc.close)

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
    ) -> CodexSessionClient:
        _ = (allowed_tools, disallowed_tools, permission_mode, mcp_servers, task_budget)
        env = self._metadata_env(extra_env)
        async with self._rpc_lock:
            try:
                rpc = await self._ensure_rpc_locked(env)

                params = self._thread_params(
                    model=model,
                    cwd=cwd,
                    system_prompt=self._combine_system_prompt(_custom_base, system_prompt),
                )
                thread_id = getattr(session, "codex_thread_id", None)
                if thread_id:
                    await asyncio.to_thread(rpc.thread_resume, thread_id, params)
                else:
                    result = await asyncio.to_thread(
                        rpc.thread_start,
                        {**params, "serviceName": "oh-my-gateway"},
                    )
                    thread = result.get("thread")
                    if not isinstance(thread, dict) or not thread.get("id"):
                        raise CodexAppServerError("thread/start response missing thread.id")
                    thread_id = str(thread["id"])
                    setattr(session, "codex_thread_id", thread_id)
            except Exception:
                await self._close_rpc_locked()
                raise

        return CodexSessionClient(
            rpc=rpc,
            thread_id=thread_id,
            model=model,
            cwd=cwd,
            env=env,
            owns_rpc=False,
        )

    async def _ensure_rpc_locked(self, env: Dict[str, str]) -> CodexJsonRpcClient:
        if self._rpc is not None and env != self._rpc_env:
            await self._close_rpc_locked()

        if not self._rpc_is_usable(self._rpc):
            if self._rpc is not None:
                await self._close_rpc_locked()
            rpc = CodexJsonRpcClient(
                binary=codex_bin(),
                cwd=None,
                env=env,
                config_overrides=configured_config_overrides(),
                read_timeout=self.timeout,
            )
            try:
                await asyncio.to_thread(rpc.start)
            except Exception:
                await asyncio.to_thread(rpc.close)
                raise
            self._rpc = rpc
            self._rpc_env = dict(env)

        assert self._rpc is not None
        return self._rpc

    async def _close_rpc_locked(self) -> None:
        rpc = self._rpc
        self._rpc = None
        self._rpc_env = {}
        if rpc is not None:
            await asyncio.to_thread(rpc.close)

    def _rpc_is_usable(self, rpc: Optional[CodexJsonRpcClient]) -> bool:
        if rpc is None:
            return False
        is_running = getattr(rpc, "is_running", None)
        if callable(is_running):
            return bool(is_running())
        return not bool(getattr(rpc, "closed", False))

    def _metadata_env(self, extra_env: Optional[Dict[str, str]]) -> Dict[str, str]:
        if not extra_env:
            return {}
        from src.constants import METADATA_ENV_ALLOWLIST

        return {k: v for k, v in extra_env.items() if k in METADATA_ENV_ALLOWLIST}

    def _combine_system_prompt(
        self,
        custom_base: Optional[str],
        system_prompt: Optional[str],
    ) -> Optional[str]:
        if custom_base and system_prompt:
            return f"{custom_base}\n\n{system_prompt}"
        return custom_base or system_prompt

    def _thread_params(
        self,
        *,
        model: Optional[str],
        cwd: Optional[str],
        system_prompt: Optional[str],
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "approvalPolicy": approval_policy(),
            "sandbox": sandbox_mode(),
        }
        if model:
            params["model"] = model
        if cwd:
            params["cwd"] = cwd
        if system_prompt:
            params["developerInstructions"] = system_prompt
        return params

    def _turn_params(self, client: CodexSessionClient) -> Dict[str, Any]:
        params: Dict[str, Any] = {"approvalPolicy": approval_policy()}
        if client.model:
            params["model"] = client.model
        if client.cwd:
            params["cwd"] = client.cwd
        return params

    async def run_completion_with_client(
        self,
        client: CodexSessionClient,
        prompt: str,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        _ = session
        async with self._rpc_lock:
            try:
                rpc = await self._ensure_rpc_locked(client.env or {})
                turn = await asyncio.to_thread(
                    rpc.turn_start,
                    client.thread_id,
                    [{"type": "text", "text": prompt}],
                    self._turn_params(client),
                )
                turn_obj = turn.get("turn")
                if not isinstance(turn_obj, dict) or not turn_obj.get("id"):
                    yield {
                        "type": "error",
                        "is_error": True,
                        "error_message": "turn/start response missing turn.id",
                    }
                    return
                turn_id = str(turn_obj["id"])
                notification_iter = self._notification_iterator(rpc, client.thread_id, turn_id)
                while True:
                    has_value, chunk = await asyncio.to_thread(
                        self._next_chunk,
                        notification_iter,
                    )
                    if not has_value:
                        break
                    if chunk is not None:
                        if chunk.get("type") == "codex_approval_request":
                            tool_chunk = self._store_pending_approval(session, client, chunk)
                            yield tool_chunk
                            return
                        yield chunk
            except Exception as exc:
                await self._close_rpc_locked()
                logger.error("Codex app-server turn failed: %s", exc, exc_info=True)
                yield {"type": "error", "is_error": True, "error_message": str(exc)}

    async def resume_approval_with_client(
        self,
        client: CodexSessionClient,
        call_id: str,
        output: str,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        _ = session
        async with self._rpc_lock:
            try:
                rpc = await self._ensure_rpc_locked(client.env or {})
                method = client.pending_approval_method or ""
                params = client.pending_approval_params or {}
                request_id = client.pending_approval_request_id
                if request_id is None or str(request_id) != str(call_id):
                    request_id = call_id
                turn_id = client.pending_approval_turn_id or str(params.get("turnId") or "")
                if not turn_id:
                    yield {
                        "type": "error",
                        "is_error": True,
                        "error_message": "Codex approval continuation is missing turn id",
                    }
                    return

                result = self._approval_result_from_output(method, output, params)
                await asyncio.to_thread(rpc.respond, request_id, result)
                client.pending_approval_request_id = None
                client.pending_approval_method = None
                client.pending_approval_turn_id = None
                client.pending_approval_params = None

                notification_iter = self._notification_iterator(rpc, client.thread_id, turn_id)
                while True:
                    has_value, chunk = await asyncio.to_thread(
                        self._next_chunk,
                        notification_iter,
                    )
                    if not has_value:
                        break
                    if chunk is not None:
                        if chunk.get("type") == "codex_approval_request":
                            tool_chunk = self._store_pending_approval(session, client, chunk)
                            yield tool_chunk
                            return
                        yield chunk
            except Exception as exc:
                await self._close_rpc_locked()
                logger.error("Codex approval continuation failed: %s", exc, exc_info=True)
                yield {"type": "error", "is_error": True, "error_message": str(exc)}

    @staticmethod
    def _next_chunk(iterator: Iterator[Dict[str, Any]]) -> tuple[bool, Optional[Dict[str, Any]]]:
        try:
            return True, next(iterator)
        except StopIteration:
            return False, None

    def _notification_iterator(
        self,
        rpc: CodexJsonRpcClient,
        thread_id: str,
        turn_id: str,
    ) -> Iterator[Dict[str, Any]]:
        items: list[dict[str, Any]] = []
        usage_box: dict[str, Optional[dict[str, int]]] = {"usage": None}
        while True:
            notification = rpc.next_notification()
            yield from self._chunks_from_notification(
                thread_id=thread_id,
                turn_id=turn_id,
                notification=notification,
                items=items,
                usage_box=usage_box,
            )
            if self._is_terminal_notification(
                thread_id=thread_id,
                turn_id=turn_id,
                notification=notification,
            ):
                break

    def _chunks_from_notifications(
        self,
        *,
        thread_id: Optional[str] = None,
        turn_id: str,
        notifications: Iterable[Dict[str, Any]],
    ) -> Iterator[Dict[str, Any]]:
        items: list[dict[str, Any]] = []
        usage_box: dict[str, Optional[dict[str, int]]] = {"usage": None}
        for notification in notifications:
            yield from self._chunks_from_notification(
                thread_id=thread_id,
                turn_id=turn_id,
                notification=notification,
                items=items,
                usage_box=usage_box,
            )

    def _chunks_from_notification(
        self,
        *,
        thread_id: Optional[str],
        turn_id: str,
        notification: Dict[str, Any],
        items: list[dict[str, Any]],
        usage_box: dict[str, Optional[dict[str, int]]],
    ) -> Iterator[Dict[str, Any]]:
        method = notification.get("method")
        params = notification.get("params") if isinstance(notification, dict) else None
        if not isinstance(params, dict):
            return

        if self._is_approval_request(notification):
            yield self._approval_request_chunk(notification, params)
            return

        notification_turn_id = params.get("turnId")
        turn = params.get("turn")
        if isinstance(turn, dict):
            notification_turn_id = turn.get("id") or notification_turn_id

        if self._is_thread_idle_notification(thread_id, notification):
            yield from self._completion_chunks(items, usage_box)
            return

        if notification_turn_id != turn_id:
            return

        if method == "item/started":
            item = params.get("item")
            tool_use = self._tool_use_from_item(item)
            if tool_use:
                yield {"type": "assistant", "content": [tool_use]}
            return

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                yield {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": delta},
                    },
                }
            return

        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict):
                tool_result = self._tool_result_from_item(item)
                if tool_result:
                    yield {"type": "user", "content": [tool_result]}
                    return
                items.append(item)
            return

        if method == "thread/tokenUsage/updated":
            usage_box["usage"] = self._extract_usage(params.get("tokenUsage"))
            return

        if method != "turn/completed":
            return

        if isinstance(turn, dict) and turn.get("status") == "failed":
            yield {
                "type": "error",
                "is_error": True,
                "error_message": self._turn_error_message(turn),
            }
            return

        yield from self._completion_chunks(items, usage_box)

    def _is_terminal_notification(
        self,
        *,
        thread_id: str,
        turn_id: str,
        notification: Dict[str, Any],
    ) -> bool:
        method = notification.get("method")
        params = notification.get("params")
        if not isinstance(params, dict):
            return False

        if method == "turn/completed":
            notification_turn_id = params.get("turnId")
            turn = params.get("turn")
            if isinstance(turn, dict):
                notification_turn_id = turn.get("id") or notification_turn_id
            return notification_turn_id == turn_id

        if self._is_approval_request(notification):
            return True

        return self._is_thread_idle_notification(thread_id, notification)

    def _is_approval_request(self, notification: Dict[str, Any]) -> bool:
        return "id" in notification and notification.get("method") in CODEX_APPROVAL_METHODS

    def _approval_request_chunk(
        self,
        notification: Dict[str, Any],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        request_id = notification.get("id")
        method = str(notification.get("method") or "")
        arguments = self._approval_arguments(method, params)
        tool_block = {
            "type": "tool_use",
            "id": str(request_id),
            "name": "codex_approval",
            "input": arguments,
            "metadata": {
                "codex_approval_request_id": str(request_id),
                "codex_approval_method": method,
                "codex_thread_id": str(params.get("threadId") or ""),
                "codex_turn_id": str(params.get("turnId") or ""),
            },
        }
        return {
            "type": "codex_approval_request",
            "request_id": request_id,
            "method": method,
            "params": params,
            "tool_chunk": {"type": "assistant", "content": [tool_block]},
        }

    def _approval_arguments(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        kind = self._approval_kind(method)
        reason = params.get("reason")
        arguments: Dict[str, Any] = {
            "kind": kind,
            "question": self._approval_question(kind, params),
        }
        if isinstance(params.get("command"), str):
            arguments["command"] = params["command"]
        if isinstance(params.get("cwd"), str):
            arguments["cwd"] = params["cwd"]
        if isinstance(reason, str) and reason:
            arguments["reason"] = reason
        if "permissions" in params:
            arguments["permissions"] = params.get("permissions") or {}
        if "grantRoot" in params and params.get("grantRoot"):
            arguments["grantRoot"] = params["grantRoot"]
        for key in (
            "itemId",
            "approvalId",
            "additionalPermissions",
            "commandActions",
            "networkApprovalContext",
            "proposedExecpolicyAmendment",
            "proposedNetworkPolicyAmendments",
        ):
            if key in params and params.get(key) is not None:
                arguments[key] = params[key]
        arguments["options"] = self._approval_options(kind, params)
        return arguments

    def _approval_kind(self, method: str) -> str:
        if method == "item/commandExecution/requestApproval":
            return "command"
        if method == "item/fileChange/requestApproval":
            return "file_change"
        if method == "item/permissions/requestApproval":
            return "permissions"
        return "approval"

    def _approval_question(self, kind: str, params: Dict[str, Any]) -> str:
        if kind == "command":
            command = params.get("command")
            if isinstance(command, str) and command:
                return f"Codex requests approval to run command: {command}"
            return "Codex requests approval to run a command."
        if kind == "file_change":
            return "Codex requests approval to apply file changes."
        if kind == "permissions":
            return "Codex requests additional permissions."
        return "Codex requests approval."

    def _approval_options(self, kind: str, params: Dict[str, Any]) -> list[dict[str, Any]]:
        if kind == "permissions":
            decisions: list[Any] = ["accept", "acceptForSession", "decline"]
        else:
            raw = params.get("availableDecisions")
            decisions = raw if isinstance(raw, list) else []
            if not decisions:
                decisions = ["accept", "acceptForSession", "decline", "cancel"]
        descriptions = {
            "accept": "Approve this request once.",
            "acceptForSession": "Approve matching requests for this session.",
            "acceptWithExecpolicyAmendment": (
                "Approve and apply the proposed execpolicy amendment."
            ),
            "applyNetworkPolicyAmendment": "Apply the proposed network policy rule.",
            "decline": "Deny and let Codex continue.",
            "cancel": "Deny and interrupt the turn.",
        }
        options = []
        for decision in decisions:
            label = self._approval_decision_label(decision)
            if not label:
                continue
            option = {
                "label": label,
                "description": descriptions.get(label, f"Choose {label}."),
            }
            if isinstance(decision, dict):
                option["decision"] = decision
            options.append(option)
        return options

    def _approval_decision_label(self, decision: Any) -> str:
        if isinstance(decision, str):
            return decision
        if not isinstance(decision, dict) or not decision:
            return ""
        if "acceptWithExecpolicyAmendment" in decision:
            return "acceptWithExecpolicyAmendment"
        if "applyNetworkPolicyAmendment" in decision:
            amendment = decision.get("applyNetworkPolicyAmendment")
            if isinstance(amendment, dict):
                policy = amendment.get("network_policy_amendment")
                if isinstance(policy, dict):
                    action = policy.get("action")
                    host = policy.get("host")
                    if action and host:
                        return f"applyNetworkPolicyAmendment:{action}:{host}"
            return "applyNetworkPolicyAmendment"
        return next(iter(decision.keys()), "")

    def _store_pending_approval(
        self,
        session: Any,
        client: CodexSessionClient,
        chunk: Dict[str, Any],
    ) -> Dict[str, Any]:
        tool_chunk = chunk["tool_chunk"]
        tool_block = tool_chunk["content"][0]
        metadata = tool_block["metadata"]
        client.pending_approval_request_id = chunk.get("request_id")
        client.pending_approval_method = chunk.get("method")
        client.pending_approval_params = (
            chunk.get("params") if isinstance(chunk.get("params"), dict) else {}
        )
        client.pending_approval_turn_id = metadata.get("codex_turn_id")
        session.pending_tool_call = {
            "call_id": metadata["codex_approval_request_id"],
            "name": ASK_USER_QUESTION_TOOL_NAME,
            "arguments": tool_block["input"],
            "backend": "codex",
            "codex_resume": "approval",
        }
        return tool_chunk

    def _tool_use_from_item(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type not in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}:
            return None
        if not isinstance(item_id, str) or not item_id:
            return None
        tool_input = {k: v for k, v in item.items() if k not in {"id", "type", "aggregatedOutput"}}
        return {
            "type": "tool_use",
            "id": item_id,
            "name": str(item_type),
            "input": tool_input,
        }

    def _tool_result_from_item(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type not in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}:
            return None
        if not isinstance(item_id, str) or not item_id:
            return None
        status = str(item.get("status") or "")
        is_error = status in {"failed", "declined"}
        if item_type == "commandExecution":
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                is_error = True
            content = item.get("aggregatedOutput")
            if not isinstance(content, str) or not content:
                content = json.dumps(
                    {
                        "status": status,
                        "exitCode": exit_code,
                        "command": item.get("command"),
                    },
                    ensure_ascii=False,
                )
        else:
            content = json.dumps(
                {k: v for k, v in item.items() if k not in {"id", "type"}},
                ensure_ascii=False,
            )
        return {
            "type": "tool_result",
            "tool_use_id": item_id,
            "content": content,
            "is_error": is_error,
        }

    def _approval_result_from_output(
        self,
        method: str,
        output: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        parsed: Any = None
        if isinstance(output, str):
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(output)
        if isinstance(parsed, dict):
            if method == "item/permissions/requestApproval" and "permissions" in parsed:
                return parsed
            if "decision" in parsed:
                return {"decision": parsed["decision"]}
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                return {"decision": parsed}

        decision = self._normalize_approval_decision(parsed if parsed is not None else output)
        if method == "item/permissions/requestApproval":
            if decision in {"accept", "acceptForSession"}:
                result: Dict[str, Any] = {"permissions": params.get("permissions") or {}}
                result["scope"] = "session" if decision == "acceptForSession" else "turn"
                return result
            return {"permissions": {}, "scope": "turn"}
        selected_decision = self._approval_decision_from_available_options(output, params)
        if selected_decision is not None:
            return {"decision": selected_decision}
        return {"decision": decision}

    def _approval_decision_from_available_options(
        self,
        output: str,
        params: Dict[str, Any],
    ) -> Optional[Any]:
        raw = str(output or "").strip()
        decisions = params.get("availableDecisions")
        if not isinstance(decisions, list):
            return None
        for decision in decisions:
            label = self._approval_decision_label(decision)
            if raw == label:
                return decision
        return None

    def _normalize_approval_decision(self, value: Any) -> str:
        if isinstance(value, list) and value:
            value = value[0]
        raw = str(value or "").strip()
        aliases = {
            "": "decline",
            "yes": "accept",
            "y": "accept",
            "allow": "accept",
            "approve": "accept",
            "approved": "accept",
            "once": "accept",
            "no": "decline",
            "n": "decline",
            "deny": "decline",
            "denied": "decline",
            "reject": "decline",
            "rejected": "decline",
            "always": "acceptForSession",
            "session": "acceptForSession",
            "stop": "cancel",
        }
        if raw in {
            "accept",
            "acceptForSession",
            "decline",
            "cancel",
        }:
            return raw
        return aliases.get(raw, "decline")

    def _is_thread_idle_notification(
        self,
        thread_id: Optional[str],
        notification: Dict[str, Any],
    ) -> bool:
        if not thread_id or notification.get("method") != "thread/status/changed":
            return False
        params = notification.get("params")
        if not isinstance(params, dict) or params.get("threadId") != thread_id:
            return False
        status = params.get("status")
        return isinstance(status, dict) and status.get("type") == "idle"

    def _completion_chunks(
        self,
        items: list[dict[str, Any]],
        usage_box: dict[str, Optional[dict[str, int]]],
    ) -> Iterator[Dict[str, Any]]:
        final_text = self._final_response_from_items(items) or ""

        assistant: Dict[str, Any] = {
            "type": "assistant",
            "content": [{"type": "text", "text": final_text}],
        }
        result: Dict[str, Any] = {
            "type": "result",
            "subtype": "success",
            "result": final_text,
        }
        usage = usage_box.get("usage")
        if usage:
            assistant["usage"] = usage
            result["usage"] = usage
        yield assistant
        yield result

    def _extract_usage(self, token_usage: Any) -> Optional[dict[str, int]]:
        if not isinstance(token_usage, dict):
            return None
        last = token_usage.get("last")
        if not isinstance(last, dict):
            return None
        input_tokens = int(last.get("inputTokens") or 0) + int(last.get("cachedInputTokens") or 0)
        output_tokens = int(last.get("outputTokens") or 0)
        return {"input_tokens": input_tokens, "output_tokens": output_tokens}

    def _turn_error_message(self, turn: dict[str, Any]) -> str:
        error = turn.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return "Codex turn failed"

    def _final_response_from_items(self, items: list[dict[str, Any]]) -> Optional[str]:
        last_unknown_phase: Optional[str] = None
        for item in reversed(items):
            if item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            if item.get("phase") == "final_answer":
                return text
            if item.get("phase") is None and last_unknown_phase is None:
                last_unknown_phase = text
        return last_unknown_phase

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        for message in reversed(messages):
            if message.get("subtype") == "success" and isinstance(message.get("result"), str):
                result = message["result"]
                if result.strip():
                    return result
        parts = []
        for message in messages:
            if message.get("type") == "assistant" and isinstance(message.get("content"), list):
                text = MessageAdapter.format_blocks(message["content"])
                if text:
                    parts.append(text)
        return "\n".join(parts) if parts else None

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
