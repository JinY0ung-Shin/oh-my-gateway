#!/usr/bin/env python3
"""Run the P0a-2 deterministic fault corpus through an isolated Responses proxy (#163)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from real_path_conformance import (
    PROVIDER_ID,
    build_config,
    codex_version,
    parse_header_env,
    require_env,
    resolve_binary,
    run_case,
    sha256_file,
)


FAULT_MARKER = "CHATDRAGON_P0_FAULT_OK"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def split_provider_base(base_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--upstream-base-url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("credentials must not be embedded in --upstream-base-url")
    if parsed.query or parsed.fragment:
        raise ValueError("query/fragment on provider base URL is not supported by this P0 proxy")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    prefix = parsed.path.rstrip("/")
    return origin, prefix


def wait_for_proxy(port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/__p0_health"
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.05)
    raise TimeoutError(f"fault proxy did not become healthy: {last_error}")


def stop_process(proc: subprocess.Popen[bytes], timeout_s: float = 5.0) -> dict[str, Any]:
    started = time.monotonic()
    forced = False
    if proc.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        forced = True
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        proc.wait(timeout=timeout_s)
    return {
        "returncode": proc.returncode,
        "forced_kill": forced,
        "elapsed_s": round(time.monotonic() - started, 6),
    }


def classify_observation(case: dict[str, Any]) -> str:
    if case.get("timed_out"):
        return "hang"
    summary = case.get("summary") or {}
    events = summary.get("event_types") or {}
    if case.get("exit_code") == 0 and events.get("turn.completed", 0) == 1:
        return "success"
    return "terminal_failure"


def evaluate_fault(expected: str, case: dict[str, Any]) -> tuple[str, str]:
    observed = classify_observation(case)
    if expected == "success":
        passed = case.get("status") == "pass" and observed == "success"
    elif expected == "terminal_failure":
        passed = observed == "terminal_failure"
    elif expected == "bounded_terminal":
        passed = observed in {"success", "terminal_failure"}
    else:
        raise ValueError(expected)
    return ("pass" if passed else "fail"), observed


def proxy_command(
    *,
    proxy_path: Path,
    upstream_origin: str,
    port: int,
    mode: str,
    after_events: int,
    delay_s: float,
) -> list[str]:
    return [
        sys.executable,
        str(proxy_path),
        "--upstream-base-url",
        upstream_origin,
        "--port",
        str(port),
        "--mode",
        mode,
        "--after-events",
        str(after_events),
        "--delay-s",
        str(delay_s),
        "--i-understand-isolated-test-only",
    ]


def run_fault(
    *,
    spec: dict[str, Any],
    proxy_path: Path,
    upstream_origin: str,
    upstream_prefix: str,
    binary: Path,
    model: str,
    api_key_env: str | None,
    header_env: list[tuple[str, str]],
    artifact_dir: Path,
    timeout_s: float,
    stream_idle_timeout_ms: int,
) -> dict[str, Any]:
    port = free_local_port()
    local_base = f"http://127.0.0.1:{port}{upstream_prefix}"
    mode = str(spec["mode"])
    name = str(spec["name"])
    case_dir = artifact_dir / name
    case_dir.mkdir(parents=True, exist_ok=True)
    proxy_stdout_path = case_dir / "proxy.stdout.txt"
    proxy_stderr_path = case_dir / "proxy.stderr.txt"

    with proxy_stdout_path.open("wb") as proxy_stdout, proxy_stderr_path.open("wb") as proxy_stderr:
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proxy = subprocess.Popen(
            proxy_command(
                proxy_path=proxy_path,
                upstream_origin=upstream_origin,
                port=port,
                mode=mode,
                after_events=int(spec.get("after_events", 1)),
                delay_s=float(spec.get("delay_s", 0.0)),
            ),
            stdout=proxy_stdout,
            stderr=proxy_stderr,
            **kwargs,
        )
        proxy_stop: dict[str, Any] | None = None
        try:
            wait_for_proxy(port)
            with tempfile.TemporaryDirectory(prefix=f"codex-p0-fault-{name}-") as root_str:
                root = Path(root_str)
                codex_home = root / "codex-home"
                workspace = root / "workspace"
                codex_home.mkdir()
                workspace.mkdir()
                config = build_config(
                    model=model,
                    base_url=local_base,
                    api_key_env=api_key_env,
                    header_env=header_env,
                    idle_timeout_ms=stream_idle_timeout_ms,
                    extra_toml=None,
                )
                (codex_home / "config.toml").write_text(config, encoding="utf-8")
                case = run_case(
                    name=name,
                    binary=binary,
                    codex_home=codex_home,
                    workspace=workspace,
                    model=model,
                    prompt=f"Reply with exactly {FAULT_MARKER} and nothing else.",
                    artifact_dir=case_dir,
                    timeout_s=timeout_s,
                    expected={"marker": FAULT_MARKER, "usage": True},
                )
                verdict, observed = evaluate_fault(str(spec["expected"]), case)
                return {
                    "name": name,
                    "fault_mode": mode,
                    "expected": spec["expected"],
                    "observed": observed,
                    "status": verdict,
                    "codex_case": case,
                    "local_proxy_base_path": upstream_prefix or "/",
                    "proxy_artifacts": {
                        "stdout": str(proxy_stdout_path.relative_to(artifact_dir)),
                        "stderr": str(proxy_stderr_path.relative_to(artifact_dir)),
                    },
                }
        except Exception as exc:
            return {
                "name": name,
                "fault_mode": mode,
                "expected": spec["expected"],
                "observed": "harness_error",
                "status": "fail",
                "harness_error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            proxy_stop = stop_process(proxy)
            # proxy_stop is written below by reopening the return object is not
            # possible after `return`, so persist a tiny local lifecycle artifact.
            (case_dir / "proxy.lifecycle.json").write_text(
                json.dumps(proxy_stop, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env")
    parser.add_argument(
        "--header-env",
        action="append",
        default=[],
        type=parse_header_env,
        metavar="HEADER=ENV_VAR",
    )
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=45.0)
    parser.add_argument("--stream-idle-timeout-ms", type=int, default=3000)
    parser.add_argument("--within-idle-delay-s", type=float, default=1.0)
    parser.add_argument("--beyond-idle-delay-s", type=float, default=5.0)
    parser.add_argument(
        "--i-understand-isolated-test-only",
        action="store_true",
        help="required acknowledgement that upstream is an isolated test route/replica",
    )
    args = parser.parse_args()

    if not args.i_understand_isolated_test_only:
        parser.error("refusing to run P0a-2 without --i-understand-isolated-test-only")
    if args.timeout_s <= 0 or args.stream_idle_timeout_ms <= 0:
        parser.error("timeouts must be positive")
    if args.within_idle_delay_s < 0 or args.beyond_idle_delay_s <= 0:
        parser.error("delay values are invalid")
    if args.within_idle_delay_s * 1000 >= args.stream_idle_timeout_ms:
        parser.error("--within-idle-delay-s must be below the Codex stream idle timeout")
    if args.beyond_idle_delay_s * 1000 <= args.stream_idle_timeout_ms:
        parser.error("--beyond-idle-delay-s must exceed the Codex stream idle timeout")
    if args.api_key_env:
        require_env(args.api_key_env, "provider API key")
    for header, env_name in args.header_env:
        require_env(env_name, f"enterprise header {header}")

    binary = resolve_binary(args.codex_bin)
    upstream_origin, upstream_prefix = split_provider_base(args.upstream_base_url)
    proxy_path = Path(__file__).with_name("fault_proxy.py").resolve()
    if not proxy_path.is_file():
        raise FileNotFoundError(proxy_path)
    args.artifact_dir.mkdir(parents=True, exist_ok=True)

    specs = [
        {"name": "control", "mode": "passthrough", "expected": "success"},
        {"name": "http_429", "mode": "http_429", "expected": "terminal_failure"},
        {"name": "http_500", "mode": "http_500", "expected": "terminal_failure"},
        {"name": "drop_before_body", "mode": "drop_before_body", "expected": "terminal_failure"},
        {
            "name": "truncate_after_first_event",
            "mode": "truncate_after_events",
            "after_events": 1,
            "expected": "terminal_failure",
        },
        {
            "name": "abort_after_first_event",
            "mode": "abort_after_events",
            "after_events": 1,
            "expected": "terminal_failure",
        },
        {
            "name": "malformed_after_first_event",
            "mode": "malformed_event_after",
            "after_events": 1,
            "expected": "terminal_failure",
        },
        {
            "name": "delay_within_idle",
            "mode": "delay_first_event",
            "delay_s": args.within_idle_delay_s,
            "expected": "success",
        },
        {
            "name": "delay_beyond_idle",
            "mode": "delay_first_event",
            "delay_s": args.beyond_idle_delay_s,
            "expected": "terminal_failure",
        },
        {
            "name": "duplicate_event_observation",
            "mode": "duplicate_event",
            "after_events": 2,
            "expected": "bounded_terminal",
        },
        {
            "name": "reorder_event_observation",
            "mode": "reorder_adjacent",
            "after_events": 2,
            "expected": "bounded_terminal",
        },
    ]

    report: dict[str, Any] = {
        "p0": "P0a-2",
        "canonical_issue": 163,
        "codex": {
            "path": str(binary),
            "version": codex_version(binary),
            "sha256": sha256_file(binary),
        },
        "provider": {
            "model": args.model,
            "upstream_base_url_sha256": sha256_text(args.upstream_base_url.rstrip("/")),
            "api_key_env_name": args.api_key_env,
            "enterprise_header_env": [
                {"header": header, "env_var": env_name} for header, env_name in args.header_env
            ],
            "stream_idle_timeout_ms": args.stream_idle_timeout_ms,
            "request_max_retries": 0,
            "stream_max_retries": 0,
        },
        "cases": [],
        "notes": [
            "Run only against an isolated LiteLLM/model-gateway route or replica.",
            "duplicate/reorder cases only require bounded terminal behavior; inspect raw artifacts for semantic corruption.",
            "side-effect-after-drop requires a separate deterministic tool-call fixture and is not claimed by this matrix.",
        ],
    }

    for spec in specs:
        report["cases"].append(
            run_fault(
                spec=spec,
                proxy_path=proxy_path,
                upstream_origin=upstream_origin,
                upstream_prefix=upstream_prefix,
                binary=binary,
                model=args.model,
                api_key_env=args.api_key_env,
                header_env=args.header_env,
                artifact_dir=args.artifact_dir,
                timeout_s=args.timeout_s,
                stream_idle_timeout_ms=args.stream_idle_timeout_ms,
            )
        )

    failed = [case["name"] for case in report["cases"] if case.get("status") != "pass"]
    report["overall_status"] = "pass" if not failed else "fail"
    report["failed_cases"] = failed
    report_path = args.artifact_dir / "p0a-fault-matrix-summary.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(report_path)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
