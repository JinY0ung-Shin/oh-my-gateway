#!/usr/bin/env python3
"""P0a-1 real-path Codex -> enterprise Responses conformance runner (#163).

This runner does not import oh-my-gateway backend code. It launches the exact
Codex binary under test, writes an isolated CODEX_HOME/config.toml using only
public Codex model-provider fields, and records Codex's own JSONL exec events.

Secrets stay in environment variables. The generated provider config contains
only environment-variable *names* for API keys / enterprise headers, and the
summary report never contains their values or the raw base URL.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import platform
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any


PROVIDER_ID = "chatdragon_p0"
TEXT_MARKER = "CHATDRAGON_P0_TEXT_OK"
REASONING_MARKER = "CHATDRAGON_P0_REASONING_OK"
TOOL_MARKER = "CHATDRAGON_P0_TOOL_OK"
LONG_MARKER = "CHATDRAGON_P0_LONG_OK"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_header_env(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("header mapping must be HEADER=ENV_VAR")
    header, env_name = (part.strip() for part in value.split("=", 1))
    if not header or not env_name:
        raise argparse.ArgumentTypeError("header mapping must be HEADER=ENV_VAR")
    return header, env_name


def require_env(name: str, purpose: str) -> None:
    if not os.getenv(name):
        raise ValueError(f"environment variable {name!r} is required for {purpose}")


def build_config(
    *,
    model: str,
    base_url: str,
    api_key_env: str | None,
    header_env: list[tuple[str, str]],
    idle_timeout_ms: int,
    extra_toml: str | None,
) -> str:
    lines = [
        f"model = {toml_string(model)}",
        f"model_provider = {toml_string(PROVIDER_ID)}",
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        'web_search = "disabled"',
    ]
    if extra_toml:
        lines.extend(["", "# Deployment-specific P0 additions (for example MCP).", extra_toml.rstrip()])
    lines.extend(
        [
            "",
            f"[model_providers.{PROVIDER_ID}]",
            f"name = {toml_string('ChatDRAGON P0 enterprise Responses')}",
            f"base_url = {toml_string(base_url.rstrip('/'))}",
            'wire_api = "responses"',
            "requires_openai_auth = false",
            "request_max_retries = 0",
            "stream_max_retries = 0",
            f"stream_idle_timeout_ms = {idle_timeout_ms}",
        ]
    )
    if api_key_env:
        lines.append(f"env_key = {toml_string(api_key_env)}")
    if header_env:
        lines.extend(["", f"[model_providers.{PROVIDER_ID}.env_http_headers]"])
        for header, env_name in header_env:
            lines.append(f"{toml_string(header)} = {toml_string(env_name)}")
    return "\n".join(lines) + "\n"


def resolve_binary(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which(value)
    if found:
        return Path(found).resolve()
    raise FileNotFoundError(f"Codex binary not found: {value}")


def codex_version(binary: Path) -> str:
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {type(exc).__name__}: {exc}"
    text = (completed.stdout or completed.stderr).strip()
    return text or f"exit={completed.returncode}"


def png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def write_solid_png(path: Path, rgb: tuple[int, int, int], size: int = 32) -> None:
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(raw))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def signal_process_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return
        except ProcessLookupError:
            return
    proc.send_signal(sig)


def parse_jsonl(stdout: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    invalid: list[str] = []
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            invalid.append(raw[:500])
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            invalid.append(raw[:500])
    return events, invalid


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    event_types: collections.Counter[str] = collections.Counter()
    item_types: collections.Counter[str] = collections.Counter()
    agent_texts: list[str] = []
    reasoning_texts: list[str] = []
    errors: list[str] = []
    usage: dict[str, Any] | None = None

    for event in events:
        event_type = event.get("type")
        if isinstance(event_type, str):
            event_types[event_type] += 1
        if event_type == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if event_type in {"turn.failed", "error"}:
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                errors.append(error["message"])
            elif isinstance(event.get("message"), str):
                errors.append(event["message"])
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if isinstance(item_type, str):
            item_types[item_type] += 1
        text = item.get("text")
        if item_type == "agent_message" and isinstance(text, str):
            agent_texts.append(text)
        if item_type == "reasoning" and isinstance(text, str):
            reasoning_texts.append(text)

    return {
        "event_types": dict(sorted(event_types.items())),
        "item_types": dict(sorted(item_types.items())),
        "agent_texts": agent_texts,
        "reasoning_texts": reasoning_texts,
        "errors": errors,
        "usage": usage,
    }


def case_ok_base(result: dict[str, Any]) -> bool:
    summary = result["summary"]
    return (
        not result["timed_out"]
        and result["exit_code"] == 0
        and not result["invalid_jsonl_lines"]
        and not summary["errors"]
        and summary["event_types"].get("turn.completed", 0) == 1
    )


def has_agent_marker(summary: dict[str, Any], marker: str) -> bool:
    return any(marker in text for text in summary["agent_texts"])


def evaluate_case(
    name: str,
    result: dict[str, Any],
    expected: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if name == "cancel":
        if result.get("cancel_signal_sent") is not True:
            return "inconclusive", ["process completed before cancellation signal"]
        if result.get("timed_out"):
            return "fail", ["SIGINT did not terminalize Codex before hard-kill deadline"]
        if result["exit_code"] == 0 and result["summary"]["event_types"].get("turn.completed", 0):
            return "fail", ["turn completed normally after cancellation signal"]
        return "pass", []

    if not case_ok_base(result):
        reasons.append("exec did not produce exactly one clean turn.completed")
    marker = expected.get("marker")
    if marker and not has_agent_marker(result["summary"], marker):
        reasons.append(f"missing final marker {marker}")
    item_type = expected.get("item_type")
    if item_type and result["summary"]["item_types"].get(item_type, 0) < 1:
        reasons.append(f"missing required item type {item_type}")
    if expected.get("usage") and not isinstance(result["summary"].get("usage"), dict):
        reasons.append("turn.completed did not include usage")
    color = expected.get("image_color")
    if color and not any(
        color.lower() in text.lower() for text in result["summary"]["agent_texts"]
    ):
        reasons.append(f"assistant did not identify generated image color {color}")
    return ("pass" if not reasons else "fail"), reasons


def run_case(
    *,
    name: str,
    binary: Path,
    codex_home: Path,
    workspace: Path,
    model: str,
    prompt: str,
    artifact_dir: Path,
    timeout_s: float,
    config_overrides: list[str] | None = None,
    image: Path | None = None,
    cancel_after_s: float | None = None,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command = [
        str(binary),
        "exec",
        "--experimental-json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        model,
    ]
    for override in config_overrides or []:
        command.extend(["--config", override])
    if image is not None:
        command.extend(["--image", str(image)])

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    kwargs: dict[str, Any] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True

    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workspace,
        env=env,
        **kwargs,
    )
    cancel_signal_sent = False
    timed_out = False
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        proc.stdin = None
        if cancel_after_s is None:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        else:
            deadline = time.monotonic() + timeout_s
            while proc.poll() is None and time.monotonic() - started < cancel_after_s:
                time.sleep(0.05)
            if proc.poll() is None:
                signal_process_group(proc, signal.SIGINT)
                cancel_signal_sent = True
            remaining = max(0.1, deadline - time.monotonic())
            try:
                stdout, stderr = proc.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                signal_process_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
                stdout, stderr = proc.communicate(timeout=5)
                timed_out = True
    except subprocess.TimeoutExpired:
        timed_out = True
        signal_process_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
        stdout, stderr = proc.communicate(timeout=5)

    duration = time.monotonic() - started
    stdout_path = artifact_dir / f"{name}.stdout.jsonl"
    stderr_path = artifact_dir / f"{name}.stderr.txt"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    events, invalid = parse_jsonl(stdout)
    summary = summarize_events(events)
    result: dict[str, Any] = {
        "name": name,
        "exit_code": proc.returncode,
        "duration_s": round(duration, 6),
        "timed_out": timed_out,
        "cancel_signal_sent": cancel_signal_sent,
        "invalid_jsonl_lines": len(invalid),
        "summary": summary,
        "artifacts": {
            "stdout": stdout_path.name,
            "stdout_sha256": sha256_file(stdout_path),
            "stderr": stderr_path.name,
            "stderr_sha256": sha256_file(stderr_path),
        },
    }
    status, reasons = evaluate_case(name, result, expected or {})
    result["status"] = status
    result["failure_reasons"] = reasons
    result["summary"] = {
        "event_types": summary["event_types"],
        "item_types": summary["item_types"],
        "errors_present": bool(summary["errors"]),
        "usage": summary["usage"],
        "agent_message_count": len(summary["agent_texts"]),
        "reasoning_item_count": len(summary["reasoning_texts"]),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env")
    parser.add_argument(
        "--header-env",
        action="append",
        default=[],
        type=parse_header_env,
        metavar="HEADER=ENV_VAR",
    )
    parser.add_argument(
        "--extra-config-toml",
        type=Path,
        help="deployment-specific sections such as MCP config; content is never copied into report",
    )
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--timeout-s", type=float, default=180.0)
    parser.add_argument("--stream-idle-timeout-ms", type=int, default=120000)
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--long-turn-s", type=float, default=5.0)
    parser.add_argument("--cancel-after-s", type=float, default=1.0)
    parser.add_argument("--mcp-prompt")
    args = parser.parse_args()

    if args.timeout_s <= 0 or args.stream_idle_timeout_ms <= 0:
        parser.error("timeouts must be positive")
    if args.long_turn_s <= 0 or args.cancel_after_s <= 0:
        parser.error("turn/cancel durations must be positive")
    if bool(args.mcp_prompt) != bool(args.extra_config_toml):
        parser.error("MCP case requires both --mcp-prompt and --extra-config-toml")
    if args.api_key_env:
        require_env(args.api_key_env, "provider API key")
    for header, env_name in args.header_env:
        require_env(env_name, f"enterprise header {header}")

    binary = resolve_binary(args.codex_bin)
    extra_toml: str | None = None
    extra_toml_hash: str | None = None
    if args.extra_config_toml:
        extra_toml = args.extra_config_toml.read_text(encoding="utf-8")
        extra_toml_hash = sha256_bytes(extra_toml.encode("utf-8"))

    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    temp = tempfile.TemporaryDirectory(prefix="codex-p0-real-path-")
    root = Path(temp.name)
    codex_home = root / "codex-home"
    workspace = root / "workspace"
    codex_home.mkdir(parents=True)
    workspace.mkdir(parents=True)

    config = build_config(
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        header_env=args.header_env,
        idle_timeout_ms=args.stream_idle_timeout_ms,
        extra_toml=extra_toml,
    )
    (codex_home / "config.toml").write_text(config, encoding="utf-8")

    report: dict[str, Any] = {
        "p0": "P0a-1",
        "canonical_issue": 163,
        "generated_at_unix": int(time.time()),
        "platform": platform.platform(),
        "codex": {
            "path": str(binary),
            "version": codex_version(binary),
            "sha256": sha256_file(binary),
        },
        "provider": {
            "id": PROVIDER_ID,
            "model": args.model,
            "base_url_sha256": sha256_bytes(args.base_url.rstrip("/").encode("utf-8")),
            "api_key_env_name": args.api_key_env,
            "enterprise_header_env": [
                {"header": header, "env_var": env_name} for header, env_name in args.header_env
            ],
            "config_sha256": sha256_bytes(config.encode("utf-8")),
            "extra_config_sha256": extra_toml_hash,
            "wire_api": "responses",
            "request_max_retries": 0,
            "stream_max_retries": 0,
            "stream_idle_timeout_ms": args.stream_idle_timeout_ms,
        },
        "cases": [],
        "failure_owner": None,
        "notes": [],
    }

    try:
        report["cases"].append(
            run_case(
                name="text",
                binary=binary,
                codex_home=codex_home,
                workspace=workspace,
                model=args.model,
                prompt=f"Reply with exactly {TEXT_MARKER} and nothing else.",
                artifact_dir=args.artifact_dir,
                timeout_s=args.timeout_s,
                expected={"marker": TEXT_MARKER, "usage": True},
            )
        )
        report["cases"].append(
            run_case(
                name="reasoning",
                binary=binary,
                codex_home=codex_home,
                workspace=workspace,
                model=args.model,
                prompt=f"Think about 17 * 19, then reply with exactly {REASONING_MARKER}.",
                artifact_dir=args.artifact_dir,
                timeout_s=args.timeout_s,
                config_overrides=[
                    f'model_reasoning_effort={toml_string(args.reasoning_effort)}',
                    'model_reasoning_summary="detailed"',
                ],
                expected={"marker": REASONING_MARKER, "item_type": "reasoning", "usage": True},
            )
        )
        report["cases"].append(
            run_case(
                name="tool",
                binary=binary,
                codex_home=codex_home,
                workspace=workspace,
                model=args.model,
                prompt=(
                    "You must use the shell to run `printf CHATDRAGON_P0_TOOL_EXEC` exactly once. "
                    f"After the command succeeds, reply with exactly {TOOL_MARKER}."
                ),
                artifact_dir=args.artifact_dir,
                timeout_s=args.timeout_s,
                expected={"marker": TOOL_MARKER, "item_type": "command_execution", "usage": True},
            )
        )

        image_path = root / "image-red.png"
        write_solid_png(image_path, (255, 0, 0))
        report["cases"].append(
            run_case(
                name="image",
                binary=binary,
                codex_home=codex_home,
                workspace=workspace,
                model=args.model,
                prompt="Inspect the attached image and state its dominant color in one word.",
                artifact_dir=args.artifact_dir,
                timeout_s=args.timeout_s,
                image=image_path,
                expected={"image_color": "red", "usage": True},
            )
        )
        report["cases"].append(
            run_case(
                name="long_turn",
                binary=binary,
                codex_home=codex_home,
                workspace=workspace,
                model=args.model,
                prompt=(
                    f"Use the shell to run `sleep {args.long_turn_s:g}; printf CHATDRAGON_P0_LONG_EXEC`. "
                    f"After it succeeds, reply with exactly {LONG_MARKER}."
                ),
                artifact_dir=args.artifact_dir,
                timeout_s=max(args.timeout_s, args.long_turn_s + 30),
                expected={"marker": LONG_MARKER, "item_type": "command_execution", "usage": True},
            )
        )
        report["cases"].append(
            run_case(
                name="cancel",
                binary=binary,
                codex_home=codex_home,
                workspace=workspace,
                model=args.model,
                prompt=(
                    "Use the shell to run `sleep 60; printf SHOULD_NOT_COMPLETE`. "
                    "Do not answer before the command finishes."
                ),
                artifact_dir=args.artifact_dir,
                timeout_s=max(args.timeout_s, args.cancel_after_s + 15),
                cancel_after_s=args.cancel_after_s,
                expected={},
            )
        )
        if args.mcp_prompt:
            report["cases"].append(
                run_case(
                    name="mcp",
                    binary=binary,
                    codex_home=codex_home,
                    workspace=workspace,
                    model=args.model,
                    prompt=args.mcp_prompt,
                    artifact_dir=args.artifact_dir,
                    timeout_s=args.timeout_s,
                    expected={"item_type": "mcp_tool_call", "usage": True},
                )
            )
        else:
            report["cases"].append(
                {
                    "name": "mcp",
                    "status": "not_run",
                    "failure_reasons": [
                        "deployment-specific MCP case requires --mcp-prompt and --extra-config-toml"
                    ],
                }
            )
    finally:
        temp.cleanup()

    statuses = {case["name"]: case["status"] for case in report["cases"]}
    required = {"text", "reasoning", "tool", "image", "long_turn", "cancel", "mcp"}
    missing = sorted(name for name in required if statuses.get(name) == "not_run")
    failed = sorted(name for name in required if statuses.get(name) == "fail")
    inconclusive = sorted(name for name in required if statuses.get(name) == "inconclusive")
    if failed:
        overall = "fail"
    elif missing or inconclusive:
        overall = "incomplete"
    elif all(statuses.get(name) == "pass" for name in required):
        overall = "pass"
    else:
        overall = "incomplete"
    report["overall_status"] = overall
    report["failed_cases"] = failed
    report["missing_required_cases"] = missing
    report["inconclusive_cases"] = inconclusive
    report["notes"].append(
        "Assign failure_owner manually: Codex | LiteLLM/model-gateway | backend/provider | configuration."
    )
    report["notes"].append(
        "P0a-2 injected 429/5xx/drop/malformed-stream tests belong on an isolated replica/proxy, not this real-path runner."
    )

    report_path = args.artifact_dir / "p0a-real-path-summary.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(report_path)
    return 0 if overall == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
