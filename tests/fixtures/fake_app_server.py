#!/usr/bin/env python3
"""Deterministic stdio JSON-RPC adversary for agent-runtime transport probes.

The fixture deliberately knows almost nothing about Codex. A scenario is a JSON
object containing ordered request steps and actions to emit after each request.
This keeps transport/failure probes independent from a particular protocol
schema and reusable across the official SDK and a future direct transport.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn


def _write_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _fail(message: str, code: int = 64) -> NoReturn:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    raise SystemExit(code)


def _matches(step: dict[str, Any], message: dict[str, Any]) -> bool:
    expected_method = step.get("expect_method")
    if expected_method is not None and message.get("method") != expected_method:
        return False
    if "expect_id" in step and message.get("id") != step["expect_id"]:
        return False
    if "expect_has_id" in step and ("id" in message) != bool(step["expect_has_id"]):
        return False
    if "expect_has_error" in step and ("error" in message) != bool(step["expect_has_error"]):
        return False
    return True


def _run_action(action: dict[str, Any], request: dict[str, Any]) -> None:
    kind = action.get("type")
    if kind == "response":
        if "id" not in request:
            _fail("cannot emit response for input without id")
        payload: dict[str, Any] = {"id": action.get("id", request["id"])}
        if "error" in action:
            payload["error"] = action["error"]
        else:
            payload["result"] = action.get("result")
        _write_json(payload)
        return
    if kind == "message":
        _write_json(action.get("message", {}))
        return
    if kind == "malformed":
        sys.stdout.write(str(action.get("text", "{")) + "\n")
        sys.stdout.flush()
        return
    if kind == "stderr":
        sys.stderr.write(str(action.get("text", "")) + "\n")
        sys.stderr.flush()
        return
    if kind == "sleep":
        time.sleep(float(action.get("seconds", 0)))
        return
    if kind == "spawn":
        # A long-lived descendant in the same process group, to prove that a
        # transport's teardown reaches the whole group and not just the root.
        # With ``inherit_stdout`` the descendant keeps the transport's stdout
        # pipe open after this process exits, so EOF cannot be the signal.
        subprocess.Popen(  # noqa: S603 - fixture-owned interpreter
            [sys.executable, "-c", f"import time; time.sleep({float(action.get('seconds', 60))})"],
            stdin=subprocess.DEVNULL,
            stdout=None if action.get("inherit_stdout") else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    if kind == "exit":
        raise SystemExit(int(action.get("code", 0)))
    _fail(f"unknown action type: {kind!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", type=Path)
    args = parser.parse_args()

    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    steps = scenario.get("steps", [])
    if not isinstance(steps, list):
        _fail("scenario.steps must be a list")

    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            _fail(f"scenario step {index} must be an object")
        line = sys.stdin.readline()
        if not line:
            _fail(f"stdin closed before scenario step {index}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _fail(f"invalid input JSON at step {index}: {exc}")
        if not isinstance(message, dict):
            _fail(f"input at step {index} must be a JSON object")
        if not _matches(step, message):
            _fail(
                f"unexpected input at step {index}: "
                f"expected method={step.get('expect_method')!r} "
                f"id={step.get('expect_id')!r}, "
                f"got {message!r}"
            )
        actions = step.get("actions", [])
        if not isinstance(actions, list):
            _fail(f"scenario step {index}.actions must be a list")
        for action in actions:
            if not isinstance(action, dict):
                _fail(f"scenario step {index} action must be an object")
            _run_action(action, message)

    if scenario.get("linger"):
        for _line in sys.stdin:
            pass
    return int(scenario.get("exit_code", 0))


if __name__ == "__main__":
    raise SystemExit(main())
