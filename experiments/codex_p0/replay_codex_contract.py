#!/usr/bin/env python3
"""Replay the captured Codex Responses contract against any upstream (#163 P0a).

Why this exists alongside `real_path_conformance.py`
----------------------------------------------------
#168's runner drives the **pinned Codex binary**, which is the right instrument
for P0a-1 but needs that binary present and exercises Codex's own prompting.
This script instead replays the **captured request contract** directly over
HTTP: no Codex binary, no model prompt engineering, no agent loop. That makes it
the cheapest way to answer one narrow question about a model server:

    does this upstream accept Codex 0.147.0's exact Responses payload and
    produce the stream shape Codex needs, including the tool continuation?

The tool continuation is the part a single-turn smoke test cannot reach. Two
checks in vLLM's Responses path live on turn 2 rather than turn 1: the
`Encrypted content is not supported.` rejection when an input reasoning item
carries `encrypted_content`, and the `last_message.type != "function_call"`
guard in the continuation builder. So this script always attempts turn 2 when
turn 1 yields a `function_call`.

Structural fields (`tools`, `include`, `reasoning`, `store`, `tool_choice`,
`parallel_tool_calls`) come verbatim from the fixture, because those are what is
under test. `instructions` and `input` are synthetic and short: the captured
prose is truncated and environment-specific, and Codex's wording is not the
contract.

Secret boundary: the API key is referenced by environment-variable name only,
the base URL is reported as a SHA-256 digest, and no header values are recorded.

Usage
-----
    # self-test against the hermetic upstream (proves the payload and script)
    python3 experiments/codex_p0/mock_responses_upstream.py --port 8099 &
    python3 experiments/codex_p0/replay_codex_contract.py \
        --base-url http://127.0.0.1:8099/v1 --model replica-model

    # against a real model server
    python3 experiments/codex_p0/replay_codex_contract.py \
        --base-url http://<vllm-host>:8000/v1 \
        --model <deployment alias> --api-key-env P0_API_KEY \
        --report /tmp/p0a-replay.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "codex_responses_request_0.147.0.json"

TOOL_TOKEN = "CHATDRAGON_P0_REPLAY_TOOL"
FINAL_TOKEN = "CHATDRAGON_P0_REPLAY_OK"

INSTRUCTIONS = (
    "You are a coding agent running in a terminal. Use the shell tool when the "
    "user asks you to run a command, then report the result."
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_contract() -> dict[str, Any]:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return doc["request"]


def user_message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def build_payload(
    contract: dict[str, Any],
    model: str,
    input_items: list[dict[str, Any]],
    *,
    include_extra_field: bool,
) -> dict[str, Any]:
    """Codex's structural fields, verbatim, with our own short prompt."""
    payload: dict[str, Any] = {
        "model": model,
        "instructions": INSTRUCTIONS,
        "input": input_items,
        "stream": True,
        "store": contract.get("store", False),
        "tools": contract.get("tools") or [],
        "tool_choice": contract.get("tool_choice", "auto"),
        "parallel_tool_calls": contract.get("parallel_tool_calls", False),
        "reasoning": contract.get("reasoning") or {"summary": "auto"},
        "include": contract.get("include") or [],
        "prompt_cache_key": "00000000-0000-0000-0000-000000000001",
    }
    if include_extra_field:
        # Codex sends this; LiteLLM strips it. vLLM's OpenAIBaseModel is
        # extra="allow", so a conforming upstream must not reject it.
        payload["client_metadata"] = {"turn_id": "00000000-0000-0000-0000-000000000002"}
    return payload


def post_stream(
    base_url: str, payload: dict[str, Any], api_key: str | None, timeout: float
) -> dict[str, Any]:
    """POST /responses and collect the SSE stream. Never records header values."""
    url = base_url.rstrip("/") + "/responses"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "text/event-stream")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")

    result: dict[str, Any] = {
        "http_status": None,
        "event_types": {},
        "output_items": [],
        "usage": None,
        "errors": [],
        "raw_frame_count": 0,
        "malformed_frames": 0,
        "transport_error": None,
    }
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["http_status"] = response.status
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    continue
                result["raw_frame_count"] += 1
                try:
                    frame = json.loads(data)
                except json.JSONDecodeError:
                    result["malformed_frames"] += 1
                    continue
                kind = frame.get("type", "<untyped>")
                result["event_types"][kind] = result["event_types"].get(kind, 0) + 1
                if kind == "error" or "error" in frame and kind.endswith("failed"):
                    result["errors"].append(str(frame.get("error"))[:400])
                if kind == "response.output_item.done":
                    item = frame.get("item")
                    if isinstance(item, dict):
                        result["output_items"].append(item)
                if kind in ("response.completed", "response.incomplete"):
                    resp = frame.get("response") or {}
                    if isinstance(resp.get("usage"), dict):
                        result["usage"] = resp["usage"]
                    if not result["output_items"] and isinstance(
                        resp.get("output"), list
                    ):
                        result["output_items"] = [
                            i for i in resp["output"] if isinstance(i, dict)
                        ]
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result["transport_error"] = f"HTTPError {exc.code}: {exc.read()[:400]!r}"
    except Exception as exc:  # noqa: BLE001 - report any transport failure
        result["transport_error"] = f"{type(exc).__name__}: {exc}"
    result["duration_s"] = round(time.monotonic() - started, 3)
    return result


def first_function_call(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if item.get("type") == "function_call":
            return item
    return None


def has_item_type(items: list[dict[str, Any]], kind: str) -> bool:
    return any(item.get("type") == kind for item in items)


def completed(turn: dict[str, Any]) -> bool:
    return turn["event_types"].get("response.completed", 0) >= 1


def clean_stream(turn: dict[str, Any]) -> bool:
    """A stream is clean only if it was accepted, carried no error event, had no
    malformed frame, and reached ``response.completed``. Applied identically to
    turn 1 and turn 2: a continuation that limps to a completed frame past an
    explicit error is not a pass."""
    return (
        turn["http_status"] == 200
        and not turn["transport_error"]
        and not turn["errors"]
        and turn["malformed_frames"] == 0
        and completed(turn)
    )


def validate_tool_call(call: dict[str, Any]) -> list[str]:
    """Reasons the emitted tool call does NOT match what this prompt requires.

    "A function call happened" is too weak: with ten tools in the contract, a
    parser/model mismatch can emit the wrong tool, unparsable arguments, or a
    missing call_id and still complete a continuation. Any of those must fail
    tool-parser compatibility rather than certify it.
    """
    reasons: list[str] = []
    if call.get("name") != "exec_command":
        reasons.append(f"tool name {call.get('name')!r} != 'exec_command'")
    call_id = call.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        reasons.append("call_id missing or empty")
    raw = call.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        args = None
        reasons.append("arguments is not valid JSON")
    if args is None and not reasons:
        reasons.append("arguments missing")
    if isinstance(args, dict):
        cmd = args.get("cmd")
        if not isinstance(cmd, str) or TOOL_TOKEN not in cmd:
            reasons.append(f"arguments.cmd does not contain {TOOL_TOKEN}")
    elif args is not None:
        reasons.append("arguments is not a JSON object")
    return reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--no-extra-field",
        action="store_true",
        help="omit client_metadata (use if an upstream rejects unknown fields)",
    )
    args = parser.parse_args()

    api_key = None
    if args.api_key_env:
        api_key = os.getenv(args.api_key_env)
        if not api_key:
            parser.error(f"environment variable {args.api_key_env!r} is empty")

    contract = load_contract()
    tool_names = [t.get("name") for t in (contract.get("tools") or []) if t.get("name")]

    turn1_input = [
        user_message(
            f"Use the shell tool to run `printf {TOOL_TOKEN}` exactly once. "
            f"After it succeeds, reply with exactly {FINAL_TOKEN}."
        )
    ]
    turn1 = post_stream(
        args.base_url,
        build_payload(
            contract,
            args.model,
            turn1_input,
            include_extra_field=not args.no_extra_field,
        ),
        api_key,
        args.timeout_s,
    )

    call = first_function_call(turn1["output_items"])
    tool_reasons = validate_tool_call(call) if call is not None else []
    turn2: dict[str, Any] | None = None
    if call is not None:
        # The continuation Codex itself would send: original input, the emitted
        # function_call, then its output. This is the turn that reaches the
        # encrypted-content and last_message-type checks.
        turn2_input = list(turn1_input) + [
            {
                "type": "function_call",
                "call_id": call.get("call_id"),
                "name": call.get("name"),
                "arguments": call.get("arguments", "{}"),
            },
            {
                "type": "function_call_output",
                "call_id": call.get("call_id"),
                "output": f"{TOOL_TOKEN}\n",
            },
        ]
        turn2 = post_stream(
            args.base_url,
            build_payload(
                contract,
                args.model,
                turn2_input,
                include_extra_field=not args.no_extra_field,
            ),
            api_key,
            args.timeout_s,
        )

    reasoning_requested = bool(contract.get("reasoning"))
    checks: dict[str, Any] = {
        "accepts_payload": turn1["http_status"] == 200 and not turn1["transport_error"],
        "no_error_events": not turn1["errors"],
        "stream_completed": completed(turn1),
        "usage_present": isinstance(turn1["usage"], dict),
        "no_malformed_frames": turn1["malformed_frames"] == 0,
        # Reasoning can legitimately land on either turn: a turn that emits a
        # tool call often carries no assistant reasoning item of its own.
        "reasoning_item": has_item_type(turn1["output_items"], "reasoning")
        or bool(turn2 and has_item_type(turn2["output_items"], "reasoning")),
        "tool_call_emitted": call is not None,
        "tool_call_correct": call is not None and not tool_reasons,
        # Turn 2 is held to the same stream-integrity bar as turn 1, plus usage.
        "continuation_accepted": bool(
            turn2 and clean_stream(turn2) and isinstance(turn2["usage"], dict)
        ),
    }

    blocking = [
        "accepts_payload",
        "no_error_events",
        "stream_completed",
        "usage_present",
        "no_malformed_frames",
    ]
    if reasoning_requested:
        # The contract asks for reasoning; a real --reasoning-parser that emits
        # no reasoning item is a P0a failure, not a cosmetic gap.
        blocking.append("reasoning_item")
    if checks["tool_call_emitted"]:
        blocking.extend(["tool_call_correct", "continuation_accepted"])
    overall = "pass" if all(checks[name] for name in blocking) else "fail"
    if overall == "pass" and not checks["tool_call_emitted"]:
        # A clean stream with no tool call is the parser-mismatch signature, not
        # a pass: Codex would simply never receive a function_call.
        overall = "incomplete"

    report = {
        "p0": "P0a payload replay",
        "canonical_issue": 163,
        "generated_at_unix": int(time.time()),
        "upstream": {
            "base_url_sha256": sha256_text(args.base_url.rstrip("/")),
            "model": args.model,
            "api_key_env_name": args.api_key_env,
            "extra_field_sent": not args.no_extra_field,
        },
        "contract": {
            "fixture": FIXTURE.name,
            "tool_names": tool_names,
            "include": contract.get("include"),
            "store": contract.get("store"),
            "parallel_tool_calls": contract.get("parallel_tool_calls"),
        },
        "turn1": {k: v for k, v in turn1.items() if k != "output_items"},
        "turn1_item_types": [i.get("type") for i in turn1["output_items"]],
        "turn2": None if turn2 is None else {k: v for k, v in turn2.items() if k != "output_items"},
        "turn2_item_types": None if turn2 is None else [i.get("type") for i in turn2["output_items"]],
        "checks": checks,
        "blocking_checks": blocking,
        "tool_call_reasons": tool_reasons,
        "reasoning_requested": reasoning_requested,
        "overall_status": overall,
        "interpretation": {
            "clean_stream_without_tool_call": (
                "tool parser emitted nothing: suspect --tool-call-parser vs model version"
            ),
            "turn2_only_failure": (
                "suspect the encrypted-content rejection or the continuation "
                "last_message type guard in the upstream Responses path"
            ),
            "missing_reasoning_item": "suspect --reasoning-parser vs model",
            "wrong_tool_call": (
                "a function_call was emitted but not the one this prompt requires "
                "(see tool_call_reasons): parser/model mismatch on tool selection, "
                "argument encoding, or call_id propagation"
            ),
            "turn2_unclean": (
                "continuation reached the server but the stream carried an error "
                "event, a malformed frame, or no usage"
            ),
        },
    }

    text = json.dumps(report, indent=2, sort_keys=False)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
        print(args.report)
    else:
        print(text)
    return 0 if overall == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
