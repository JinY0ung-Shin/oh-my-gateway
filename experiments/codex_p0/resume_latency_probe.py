#!/usr/bin/env python3
"""Measure safe writer-release -> cross-process thread/resume latency for P0b (#163)."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ownership_probe import AppServer, _thread_id
from process_per_session_probe import load_budget


def percentile95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def stop_is_clean(stop: object) -> bool:
    if not isinstance(stop, dict):
        return False
    return (
        stop.get("returncode") is not None
        and stop.get("error") is None
        and not (stop.get("surviving_descendants") or [])
    )


def run_iteration(
    *,
    codex_bin: str,
    root: Path,
    index: int,
    stop_mode: str,
    model: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    case = root / stop_mode / str(index)
    home = case / "codex-home"
    workspace = case / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    owner = AppServer(codex_bin, home, cwd=workspace, timeout_s=timeout_s)
    replacement: AppServer | None = None
    result: dict[str, Any] = {"index": index, "stop_mode": stop_mode}
    try:
        result["owner_startup_s"] = owner.start()
        thread_id = _thread_id(owner.start_thread(model=model))
        result["thread_id"] = thread_id
        result["owner_stop"] = owner.stop(stop_mode, timeout_s=timeout_s)

        replacement = AppServer(codex_bin, home, cwd=workspace, timeout_s=timeout_s)
        result["replacement_startup_s"] = replacement.start()
        started = time.monotonic()
        response = replacement.resume_thread(thread_id)
        result["resume_latency_s"] = round(time.monotonic() - started, 6)
        result["resume_response"] = response
        result["resume_ok"] = bool(response.get("ok"))
    except Exception as exc:
        result["exception"] = f"{type(exc).__name__}: {exc}"
        result["owner_stderr"] = owner.stderr_tail()
        if replacement is not None:
            result["replacement_stderr"] = replacement.stderr_tail()
    finally:
        if replacement is not None:
            result["replacement_cleanup"] = replacement.stop("stdin_eof", timeout_s=timeout_s)
        result.setdefault("owner_cleanup", owner.stop("stdin_eof", timeout_s=timeout_s))
        result["iteration_ok"] = bool(
            result.get("resume_ok")
            and not result.get("exception")
            and stop_is_clean(result.get("owner_stop"))
            and stop_is_clean(result.get("replacement_cleanup"))
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--budget", required=True, type=Path)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--stop-modes", default="stdin_eof,sigterm,sigkill")
    parser.add_argument("--timeout-s", type=float, default=10.0)
    parser.add_argument("--model", default=os.getenv("CODEX_P0_MODEL"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-root", type=Path)
    args = parser.parse_args()

    if args.iterations <= 0:
        parser.error("--iterations must be > 0")
    budget = load_budget(args.budget)
    stop_modes = [value.strip() for value in args.stop_modes.split(",") if value.strip()]
    allowed = {"stdin_eof", "sigterm", "sigkill"}
    unknown = sorted(set(stop_modes) - allowed)
    if unknown:
        parser.error(f"unsupported stop modes: {', '.join(unknown)}")

    if args.keep_root:
        root = args.keep_root
        root.mkdir(parents=True, exist_ok=True)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="codex-p0-resume-")
        root = Path(cleanup.name)

    report: dict[str, Any] = {
        "codex_bin": args.codex_bin,
        "model": args.model,
        "iterations": args.iterations,
        "budget_resume_p95_s": float(budget["acceptable_resume_p95_s"]),
        "modes": {},
    }
    try:
        for mode in stop_modes:
            cases = [
                run_iteration(
                    codex_bin=args.codex_bin,
                    root=root,
                    index=index,
                    stop_mode=mode,
                    model=args.model,
                    timeout_s=args.timeout_s,
                )
                for index in range(args.iterations)
            ]
            latencies = [
                float(case["resume_latency_s"])
                for case in cases
                if case.get("iteration_ok")
                and isinstance(case.get("resume_latency_s"), (int, float))
            ]
            p95 = percentile95(latencies)
            all_ok = len(latencies) == args.iterations
            if not all_ok:
                status = "fail"
            elif p95 is None:
                status = "inconclusive"
            elif p95 <= float(budget["acceptable_resume_p95_s"]):
                status = "pass"
            else:
                status = "fail"
            report["modes"][mode] = {
                "cases": cases,
                "successful_clean_resumes": len(latencies),
                "resume_p95_s": p95,
                "status": status,
            }
    finally:
        if cleanup is not None:
            cleanup.cleanup()

    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
