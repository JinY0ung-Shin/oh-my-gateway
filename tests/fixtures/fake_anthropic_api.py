"""Minimal fake Anthropic Messages API for zero-cost tests of the real bundled CLI.

Serves ``POST /v1/messages`` (streaming SSE or plain JSON) and
``/v1/messages/count_tokens`` on 127.0.0.1, and records every Messages request
(headers + body) so a test can assert exactly what the CLI sent upstream.
Everything else returns 404, which the CLI tolerates for its side requests.

A ``plan`` callable picks each reply from the request body:

* ``{"text": "..."}`` — one text block, ``end_turn``;
* ``{"tool_use": {"name": "...", "input": {...}}}`` — one tool call;
* ``{"tool_uses": [{"name": ...}, ...]}`` — several tool calls in ONE assistant
  message (a parallel batch), in that order;
* ``{"text_chunks": ["...", ...]}`` — one text block streamed as one
  ``text_delta`` per chunk (the non-streaming reply joins them).

The default plan answers ``"ok"`` to everything. Use :meth:`cli_env` for the
``ClaudeAgentOptions.env`` that points a CLI child at this server with an
isolated config dir and no telemetry.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

Reply = Dict[str, Any]
Plan = Callable[[Dict[str, Any]], Reply]


def _default_plan(body: Dict[str, Any]) -> Reply:
    return {"text": "ok"}


class FakeAnthropicAPI:
    """Threaded fake Messages API; use as a context manager."""

    def __init__(self, plan: Optional[Plan] = None) -> None:
        self.plan: Plan = plan or _default_plan
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FakeAnthropicAPI":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def cli_env(self, home: Path) -> Dict[str, str]:
        """Env for a CLI child: this server, an isolated config dir, no telemetry."""
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        return {
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "ANTHROPIC_BASE_URL": self.base_url,
            "ANTHROPIC_AUTH_TOKEN": "sk-fake-test",
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }

    def model_requests(self) -> List[Dict[str, Any]]:
        """Recorded requests that offered tools (the agent turn, not side calls)."""
        with self._lock:
            return [r for r in self.requests if r["body"].get("tools")]

    def _record(self, headers: Dict[str, str], body: Dict[str, Any]) -> None:
        with self._lock:
            self.requests.append({"headers": headers, "body": body})

    def _handler_class(self) -> type:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep test output clean
                pass

            def _json(self, code: int, payload: Any) -> None:
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._json(404, {"type": "error", "error": {"type": "not_found_error"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                path = self.path.split("?")[0]
                if path.endswith("/count_tokens"):
                    return self._json(200, {"input_tokens": 1})
                if not path.endswith("/v1/messages"):
                    return self._json(
                        404, {"type": "error", "error": {"type": "not_found_error"}}
                    )
                api._record(dict(self.headers.items()), body)
                self._reply(body, api.plan(body))

            def _reply(self, body: Dict[str, Any], reply: Reply) -> None:
                calls = reply.get("tool_uses") or (
                    [reply["tool_use"]] if "tool_use" in reply else []
                )
                if calls:
                    blocks = [
                        {
                            "type": "tool_use",
                            "id": "toolu_" + uuid.uuid4().hex[:12],
                            "name": call["name"],
                            "input": call.get("input", {}),
                        }
                        for call in calls
                    ]
                    stop = "tool_use"
                else:
                    chunks = reply.get("text_chunks") or [reply.get("text", "ok")]
                    blocks = [{"type": "text", "text": "".join(chunks), "_chunks": chunks}]
                    stop = "end_turn"
                message = {
                    "id": "msg_" + uuid.uuid4().hex[:12],
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model") or "claude-sonnet-5",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
                if not body.get("stream"):
                    plain = [{k: v for k, v in b.items() if k != "_chunks"} for b in blocks]
                    return self._json(
                        200, {**message, "content": plain, "stop_reason": stop}
                    )

                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("connection", "close")
                self.end_headers()

                def event(name: str, data: Dict[str, Any]) -> None:
                    self.wfile.write(
                        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
                    )
                    self.wfile.flush()

                event(
                    "message_start",
                    {"type": "message_start", "message": {**message, "content": []}},
                )
                for index, block in enumerate(blocks):
                    if block["type"] == "text":
                        start = {"type": "text", "text": ""}
                        deltas = [
                            {"type": "text_delta", "text": chunk}
                            for chunk in block["_chunks"]
                        ]
                    else:
                        start = {**block, "input": {}}
                        deltas = [
                            {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(block["input"]),
                            }
                        ]
                    event(
                        "content_block_start",
                        {"type": "content_block_start", "index": index, "content_block": start},
                    )
                    for delta in deltas:
                        event(
                            "content_block_delta",
                            {"type": "content_block_delta", "index": index, "delta": delta},
                        )
                    event("content_block_stop", {"type": "content_block_stop", "index": index})
                event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 2},
                    },
                )
                event("message_stop", {"type": "message_stop"})
                self.close_connection = True

        return Handler
