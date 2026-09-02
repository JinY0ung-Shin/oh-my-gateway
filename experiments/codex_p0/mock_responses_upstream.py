#!/usr/bin/env python3
"""Hermetic OpenAI Responses upstream for Codex P0a evidence (#163).

Purpose
-------
`real_path_conformance.py` (P0a-1) drives the pinned Codex binary against the
*actual* enterprise Responses path and must never have faults injected into it.
This module is the isolated counterpart: a dependency-free Responses server that

1. lets the same P0a corpus run offline, which makes it a positive control for
   the runner's own plumbing, and
2. injects deterministic transport faults (429, 5xx, mid-stream abort, truncated
   SSE, malformed frame, idle stall) that a shared production endpoint must not
   be asked to produce.

It also records every request it receives, which is how the captured Codex
request contract in ``fixtures/`` was produced. Header *values* are redacted
unless the name is on ``SAFE_HEADER_VALUES``, so a capture log never becomes a
credential dump; header names are always kept.

A pass against this replica is NOT a P0a-1 result
-------------------------------------------------
The replica answers protocol-shaped prompts by construction: it echoes the
corpus markers and reports the expected image color without looking at pixels.
That is deliberate — it isolates wire/plumbing behavior from model capability.
Never record a replica run as enterprise-path conformance.

Usage
-----
    python3 mock_responses_upstream.py --port 8099 --log requests.jsonl
    python3 mock_responses_upstream.py --port 8099 --fault http_429
    python3 mock_responses_upstream.py --port 8099 --fault drop_mid_stream --fault-after 3

Then point either Codex or the P0a-1 runner at it:

    uv run python experiments/codex_p0/real_path_conformance.py \
      --codex-bin /path/to/codex --base-url http://127.0.0.1:8099/v1 \
      --model replica-model --artifact-dir /tmp/codex-p0a2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MARKER_RE = re.compile(r"CHATDRAGON_P0_[A-Z_]*OK")
# The P0a-1 corpus asks for `printf <token>` inside a shell tool call.
SHELL_RE = re.compile(r"`([^`]*printf[^`]*)`")

# Deliberately non-conformant *shapes* (as opposed to transport FAULTS). These
# exist so the replay's classification can be exercised against a controlled
# upstream: each must produce a FAIL, never a pass.
SHAPES = (
    "conformant",
    "wrong_tool",      # emits update_plan instead of exec_command
    "bad_arguments",   # exec_command with arguments that are not JSON
    "no_reasoning",    # never emits a reasoning item even when requested
    "turn2_error",     # continuation stream carries an explicit error event
)

FAULTS = (
    "none",
    "http_429",
    "http_500",
    "mid_stream_500",
    "drop_mid_stream",
    "truncated_sse",
    "malformed_json",
    "idle_stall",
)


class Config:
    """Process-wide replica behavior, set once from the command line."""

    fault = "none"
    shape = "conformant"
    fault_after = 2
    idle_stall_s = 30.0
    log_path: str | None = None
    model = "replica-model"


_log_lock = threading.Lock()

# Header values are recorded ONLY for names on this allowlist. Everything else
# is reduced to a redaction marker: the runner and the enterprise path supply
# Authorization, API-key and deployment-specific `env_http_headers`, and a
# capture log must never become a credential dump. Names are always kept so the
# capture still shows which headers arrived.
SAFE_HEADER_VALUES = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-length",
        "content-type",
        "host",
        "user-agent",
        "openai-beta",
        "originator",
        "version",
    }
)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Keep values only for allowlisted header names; redact the rest."""
    redacted: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in SAFE_HEADER_VALUES:
            redacted[name] = value
        else:
            redacted[name] = f"<redacted:{len(value)} chars>"
    return redacted


def log_request(kind: str, payload: dict[str, Any]) -> None:
    if not Config.log_path:
        return
    line = json.dumps({"kind": kind, "at": time.time(), "payload": payload})
    with _log_lock:
        with open(Config.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _iter_text(value: Any):
    """Yield every string reachable in an arbitrary Responses input tree."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_text(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_text(item)


def plan_reply(body: dict[str, Any]) -> dict[str, Any]:
    """Decide what this replica should emit for the given Responses request."""
    items = [i for i in (body.get("input") or []) if isinstance(i, dict)]
    joined = "\n".join(_iter_text(items))
    tool_names = {
        t.get("name") for t in (body.get("tools") or []) if isinstance(t, dict)
    }

    already_ran_tool = any(i.get("type") == "function_call_output" for i in items)
    has_image = any("input_image" in json.dumps(i) for i in items)
    marker_match = MARKER_RE.search(joined)
    marker = marker_match.group(0) if marker_match else "replica ok"
    shell_match = SHELL_RE.search(joined)

    # Reasoning is requested via the top-level `reasoning` field, and the corpus
    # additionally sets a detailed summary; emit a reasoning item when asked.
    reasoning = isinstance(body.get("reasoning"), dict)

    if Config.shape == "no_reasoning":
        reasoning = False
    if shell_match and not already_ran_tool and "exec_command" in tool_names:
        plan = {
            "kind": "tool_call",
            "cmd": shell_match.group(1).strip(),
            "tool": "exec_command",
            "arguments": None,
            "reasoning": reasoning,
        }
        if Config.shape == "wrong_tool":
            plan["tool"] = "update_plan"
        elif Config.shape == "bad_arguments":
            plan["arguments"] = "{not json"
        return plan
    if already_ran_tool and Config.shape == "turn2_error":
        return {"kind": "error", "reasoning": reasoning}
    if has_image and not marker_match:
        # The P0a-1 image case generates a solid red PNG and asks for one word.
        return {"kind": "text", "text": "red", "reasoning": reasoning}
    return {"kind": "text", "text": marker, "reasoning": reasoning}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "P0aReplica/1"

    def log_message(self, *args: Any) -> None:  # keep stderr clean
        pass

    # ---- plumbing -----------------------------------------------------------

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _sse(self, payload: dict[str, Any]) -> None:
        event = payload.get("type", "message")
        self.wfile.write(
            f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
        )
        self.wfile.flush()

    # ---- routes -------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        if path.endswith("/models"):
            log_request("models", {"path": self.path})
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": Config.model, "object": "model", "owned_by": "p0a-replica"}
                    ],
                },
            )
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_unparsed": raw[:2000]}

        if not self.path.rstrip("/").endswith("/responses"):
            log_request("unexpected_path", {"path": self.path})
            self._json(404, {"error": {"message": f"unsupported path {self.path}"}})
            return

        log_request(
            "responses",
            {
                "path": self.path,
                "headers": redact_headers(dict(self.headers.items())),
                "body": body,
            },
        )

        if Config.fault == "http_429":
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if Config.fault == "http_500":
            self._json(500, {"error": {"message": "replica injected 500"}})
            return
        if Config.fault == "idle_stall":
            self._begin_sse()
            time.sleep(Config.idle_stall_s)
            return

        plan = plan_reply(body)
        if not body.get("stream"):
            self._json(200, self._final_response(plan))
            return
        self._stream(plan)

    # ---- response construction ---------------------------------------------

    def _items(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if plan["kind"] == "error":
            return items
        # A reasoning item precedes whatever the turn produces — a tool call or a
        # message — the same way a real reasoning-parser-backed server emits it.
        if plan.get("reasoning"):
            items.append(
                {
                    "type": "reasoning",
                    "id": "rs_replica_1",
                    "summary": [
                        {"type": "summary_text", "text": "replica reasoning summary"}
                    ],
                    "content": [],
                }
            )
        if plan["kind"] == "tool_call":
            items.append(
                {
                    "type": "function_call",
                    "id": "fc_replica_1",
                    "call_id": "call_replica_1",
                    "name": plan["tool"],
                    "arguments": plan.get("arguments") or json.dumps({"cmd": plan["cmd"]}),
                    "status": "completed",
                }
            )
            return items
        items.append(
            {
                "type": "message",
                "id": "msg_replica_1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": plan["text"], "annotations": []}
                ],
            }
        )
        return items

    def _final_response(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "resp_replica_1",
            "object": "response",
            "created_at": int(time.time()),
            "model": Config.model,
            "status": "completed",
            "output": self._items(plan),
            "usage": {
                "input_tokens": 21,
                "output_tokens": 7,
                "total_tokens": 28,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }

    def _stream(self, plan: dict[str, Any]) -> None:
        final = self._final_response(plan)
        frames: list[dict[str, Any]] = []
        opening = dict(final, status="in_progress", output=[], usage=None)
        frames.append({"type": "response.created", "response": opening})
        frames.append({"type": "response.in_progress", "response": opening})

        for index, item in enumerate(final["output"]):
            frames.append(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": dict(item, status="in_progress"),
                }
            )
            if item["type"] == "function_call":
                frames.append(
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item["id"],
                        "output_index": index,
                        "delta": item["arguments"],
                    }
                )
                frames.append(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item["id"],
                        "output_index": index,
                        "arguments": item["arguments"],
                    }
                )
            elif item["type"] == "message":
                text = item["content"][0]["text"]
                frames.append(
                    {
                        "type": "response.content_part.added",
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    }
                )
                frames.append(
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "delta": text,
                    }
                )
                frames.append(
                    {
                        "type": "response.output_text.done",
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "text": text,
                    }
                )
            frames.append(
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": item,
                }
            )
        frames.append({"type": "response.completed", "response": final})

        self._begin_sse()
        if plan["kind"] == "error":
            # Shape knob: a continuation that reports an error event and then
            # still emits response.completed. Must classify as FAIL.
            frames.insert(
                max(1, len(frames) - 1),
                {
                    "type": "error",
                    "error": {"type": "server_error", "message": "replica shape turn2_error"},
                },
            )
        for position, frame in enumerate(frames):
            if position == Config.fault_after:
                if Config.fault == "drop_mid_stream":
                    # Close the connection without a terminal event.
                    self.close_connection = True
                    return
                if Config.fault == "mid_stream_500":
                    self._sse(
                        {
                            "type": "error",
                            "error": {
                                "type": "server_error",
                                "message": "replica injected mid-stream error",
                            },
                        }
                    )
                    self.close_connection = True
                    return
                if Config.fault == "truncated_sse":
                    body = json.dumps(frame)[: max(1, len(json.dumps(frame)) // 2)]
                    self.wfile.write(f"event: {frame['type']}\ndata: {body}".encode())
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if Config.fault == "malformed_json":
                    self.wfile.write(b"event: response.output_text.delta\ndata: {not-json\n\n")
                    self.wfile.flush()
            self._sse(frame)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--model", default="replica-model")
    parser.add_argument("--fault", choices=FAULTS, default="none")
    parser.add_argument(
        "--shape",
        choices=SHAPES,
        default="conformant",
        help="deliberately non-conformant response shape, for exercising replay classification",
    )
    parser.add_argument(
        "--fault-after",
        type=int,
        default=2,
        help="frame index at which a mid-stream fault fires",
    )
    parser.add_argument("--idle-stall-s", type=float, default=30.0)
    parser.add_argument(
        "--log",
        help=(
            "append every received request to this JSONL file (contract capture); "
            "header values are redacted unless allowlisted"
        ),
    )
    args = parser.parse_args()

    if args.fault_after < 0:
        parser.error("--fault-after must be >= 0")

    Config.fault = args.fault
    Config.shape = args.shape
    Config.fault_after = args.fault_after
    Config.idle_stall_s = args.idle_stall_s
    Config.log_path = args.log
    Config.model = args.model

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"P0a hermetic upstream listening on http://{args.host}:{args.port}/v1 "
        f"(model={args.model}, fault={args.fault}, shape={args.shape})",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
