#!/usr/bin/env python3
"""Measure process-per-session Codex runtime cost against a predeclared P0 budget.

This is an evidence probe for canonical P0 issue #163. It intentionally imports
only the standalone experiment client from ``ownership_probe.py`` and does not
modify or depend on the frozen production Codex backend.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ownership_probe import AppServer, _thread_id, descendants, pid_alive, sample_process


_REQUIRED_BUDGET_FIELDS = (
    "expected_concurrent_live_sessions",
    "expected_concurrent_active_turns",
    "expected_parked_human_interactions",
    "container_memory_budget_mib",
    "process_pid_budget",
    "fd_budget",
    "acceptable_cold_start_p95_s",
    "acceptable_resume_p95_s",
    "acceptable_teardown_p95_s",
    "acceptable_idle_cpu_percent",
    "maximum_orphan_descendants_after_hard_kill",
    "process_per_session_counts",
)


def load_budget(path: Path) -> dict[str, Any]:
    budget = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in _REQUIRED_BUDGET_FIELDS if key not in budget]
    if missing:
        raise ValueError(f"budget is missing fields: {', '.join(missing)}")
    unset = [
        key
        for key in _REQUIRED_BUDGET_FIELDS
        if key != "maximum_orphan_descendants_after_hard_kill"
        and (budget[key] is None or budget[key] == [] or budget[key] == "")
    ]
    if unset:
        raise ValueError(
            "budget must be filled before measurement; unset fields: " + ", ".join(unset)
        )
    counts = budget["process_per_session_counts"]
    if not isinstance(counts, list) or not counts or any(
        not isinstance(value, int) or value <= 0 for value in counts
    ):
        raise ValueError("process_per_session_counts must be a non-empty list of positive integers")
    if budget["maximum_orphan_descendants_after_hard_kill"] != 0:
        raise ValueError("#163 requires maximum_orphan_descendants_after_hard_kill to be exactly 0")

    live = int(budget["expected_concurrent_live_sessions"])
    parked = int(budget["expected_parked_human_interactions"])
    if live <= 0:
        raise ValueError("expected_concurrent_live_sessions must be > 0")
    if parked < 0:
        raise ValueError("expected_parked_human_interactions must be >= 0")
    if max(counts) < live:
        raise ValueError(
            "process_per_session_counts must include a count at least as large as "
            "expected_concurrent_live_sessions"
        )
    if parked > 0 and max(counts) < parked:
        raise ValueError(
            "process_per_session_counts must cover expected_parked_human_interactions"
        )
    return budget


def percentile95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def proc_cpu_ticks(pid: int) -> int | None:
    stat = Path("/proc") / str(pid) / "stat"
    if not stat.exists():
        return None
    try:
        fields = stat.read_text(encoding="utf-8").split()
        return int(fields[13]) + int(fields[14])
    except (OSError, ValueError, IndexError):
        return None


def all_runtime_pids(servers: list[AppServer]) -> list[int]:
    pids: set[int] = set()
    for server in servers:
        if server.pid is None or not pid_alive(server.pid):
            continue
        pids.add(server.pid)
        pids.update(descendants(server.pid))
    return sorted(pids)


def aggregate_process_sample(pids: list[int]) -> dict[str, Any]:
    rss_kib = 0
    fd_count = 0
    rss_complete = True
    fd_complete = True
    alive = []
    for pid in pids:
        sample = sample_process(pid)
        if not sample.get("alive"):
            continue
        alive.append(pid)
        rss = sample.get("rss_kib")
        fds = sample.get("fd_count")
        if isinstance(rss, int):
            rss_kib += rss
        else:
            rss_complete = False
        if isinstance(fds, int):
            fd_count += fds
        else:
            fd_complete = False
    return {
        "alive_pids": alive,
        "pid_count": len(alive),
        "rss_kib": rss_kib if rss_complete else None,
        "fd_count": fd_count if fd_complete else None,
    }


def measure_idle_cpu(servers: list[AppServer], seconds: float) -> dict[str, Any]:
    pids_before = all_runtime_pids(servers)
    ticks_before = {pid: proc_cpu_ticks(pid) for pid in pids_before}
    started = time.monotonic()
    time.sleep(seconds)
    elapsed = time.monotonic() - started
    pids_after = all_runtime_pids(servers)
    ticks_after = {pid: proc_cpu_ticks(pid) for pid in pids_after}

    try:
        ticks_per_second = float(os.sysconf(os.sysconf_names["SC_CLK_TCK"]))
    except (AttributeError, KeyError, OSError, ValueError):
        return {
            "elapsed_s": round(elapsed, 6),
            "aggregate_cpu_percent": None,
            "complete": False,
            "reason": "CLK_TCK unavailable",
        }

    delta = 0
    complete = True
    for pid in set(pids_before) | set(pids_after):
        before = ticks_before.get(pid)
        after = ticks_after.get(pid)
        if before is None or after is None:
            complete = False
            continue
        delta += max(0, after - before)
    cpu_seconds = delta / ticks_per_second
    return {
        "elapsed_s": round(elapsed, 6),
        "aggregate_cpu_percent": round(cpu_seconds / elapsed * 100.0, 4) if elapsed > 0 else None,
        "complete": complete,
    }


def stderr_tail_stats(server: AppServer) -> dict[str, Any]:
    lines = list(server._stderr)  # experiment-owned class; not an upstream/private SDK access
    capacity = server._stderr.maxlen  # single source of truth for the ring bound
    return {
        "retained_lines": len(lines),
        "retained_bytes": sum(len(line.encode("utf-8")) + 1 for line in lines),
        "tail_capacity_lines": capacity,
        "may_be_truncated": capacity is not None and len(lines) >= capacity,
    }


def evaluate(case: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any]:
    resource = case.get("resource", {})
    idle = case.get("idle_cpu", {})
    limits = {
        "rss_mib": (
            resource.get("rss_kib") / 1024.0 if isinstance(resource.get("rss_kib"), int) else None,
            float(budget["container_memory_budget_mib"]),
        ),
        "pid_count": (resource.get("pid_count"), int(budget["process_pid_budget"])),
        "fd_count": (resource.get("fd_count"), int(budget["fd_budget"])),
        "cold_start_p95_s": (
            case.get("cold_start_p95_s"),
            float(budget["acceptable_cold_start_p95_s"]),
        ),
        "teardown_p95_s": (
            case.get("teardown_p95_s"),
            float(budget["acceptable_teardown_p95_s"]),
        ),
        "idle_cpu_percent": (
            # An incomplete sample (some PIDs' ticks unreadable, typical when
            # short-lived descendants churn at high counts) under-counts CPU and
            # would pass in the permissive direction. Treat it as unknown so the
            # case becomes inconclusive rather than pass.
            idle.get("aggregate_cpu_percent") if idle.get("complete") else None,
            float(budget["acceptable_idle_cpu_percent"]),
        ),
        # Judged against the *hard-kill* budget only when teardown actually used
        # SIGKILL; a graceful SIGTERM measures a different property and must not
        # certify process-group ownership it never exercised.
        "surviving_descendants_after_hard_kill": (
            case.get("surviving_descendant_count")
            if case.get("teardown_mode") == "sigkill"
            else None,
            int(budget["maximum_orphan_descendants_after_hard_kill"]),
        ),
    }
    checks: dict[str, Any] = {}
    any_failed = False
    any_unknown = bool(case.get("exception"))
    for name, (actual, maximum) in limits.items():
        if actual is None:
            checks[name] = {"actual": None, "maximum": maximum, "pass": None}
            any_unknown = True
            continue
        passed = actual <= maximum
        checks[name] = {"actual": actual, "maximum": maximum, "pass": passed}
        any_failed = any_failed or not passed

    if any_failed:
        status = "fail"
    elif any_unknown:
        status = "inconclusive"
    else:
        status = "pass"
    return {"checks": checks, "status": status, "pass": status == "pass"}


def run_case(
    *,
    codex_bin: str,
    root: Path,
    count: int,
    model: str | None,
    timeout_s: float,
    idle_sample_s: float,
    budget: dict[str, Any],
    teardown_mode: str = "sigkill",
) -> dict[str, Any]:
    case_root = root / f"process-per-session-{count}"
    shared_home = case_root / "codex-home"
    servers: list[AppServer] = []
    startup_times: list[float] = []
    thread_start_times: list[float] = []
    result: dict[str, Any] = {"count": count, "shared_codex_home": str(shared_home)}

    try:
        for index in range(count):
            workspace = case_root / "workspaces" / str(index)
            workspace.mkdir(parents=True, exist_ok=True)
            server = AppServer(codex_bin, shared_home, cwd=workspace, timeout_s=timeout_s)
            # Register BEFORE start(): start() both spawns the process and runs
            # initialize(), and initialize() raises on timeout. Registering after
            # it would leave a spawned runtime outside the cleanup loop -- the
            # exact orphan this probe exists to count. stop() tolerates an
            # object that never spawned.
            servers.append(server)
            startup_times.append(server.start())
            thread_started = time.monotonic()
            _thread_id(server.start_thread(model=model))
            thread_start_times.append(time.monotonic() - thread_started)

        runtime_pids = all_runtime_pids(servers)
        result["resource"] = aggregate_process_sample(runtime_pids)
        result["cold_start_times_s"] = [round(value, 6) for value in startup_times]
        result["cold_start_p95_s"] = percentile95(startup_times)
        result["thread_start_times_s"] = [round(value, 6) for value in thread_start_times]
        result["idle_cpu"] = measure_idle_cpu(servers, idle_sample_s)
        result["stderr"] = [stderr_tail_stats(server) for server in servers]
    except Exception as exc:
        result["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        teardown_times: list[float] = []
        surviving: set[int] = set()
        teardown = []
        for server in reversed(servers):
            stopped = server.stop(teardown_mode, timeout_s=timeout_s)
            teardown.append(stopped)
            elapsed = stopped.get("elapsed_s")
            if isinstance(elapsed, (int, float)):
                teardown_times.append(float(elapsed))
            surviving.update(stopped.get("surviving_descendants") or [])
        result["teardown"] = teardown
        result["teardown_mode"] = teardown_mode
        result["teardown_p95_s"] = percentile95(teardown_times)
        result["surviving_descendant_pids"] = sorted(surviving)
        result["surviving_descendant_count"] = len(surviving)
        result["evaluation"] = evaluate(result, budget)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--budget", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=os.getenv("CODEX_P0_MODEL"))
    parser.add_argument("--timeout-s", type=float, default=10.0)
    parser.add_argument("--idle-sample-s", type=float, default=2.0)
    parser.add_argument(
        "--teardown-mode",
        choices=("sigkill", "sigterm"),
        default="sigkill",
        help=(
            "sigkill exercises process-group ownership and is judged against the "
            "hard-kill orphan budget; sigterm measures graceful shutdown only and "
            "leaves that check unknown"
        ),
    )
    parser.add_argument("--keep-root", type=Path)
    args = parser.parse_args()

    budget = load_budget(args.budget)
    counts = sorted(set(int(value) for value in budget["process_per_session_counts"]))

    if args.keep_root:
        root = args.keep_root
        root.mkdir(parents=True, exist_ok=True)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="codex-p0-pps-")
        root = Path(cleanup.name)

    report = {
        "budget": budget,
        "codex_bin": args.codex_bin,
        "model": args.model,
        "cases": [],
    }
    try:
        for count in counts:
            report["cases"].append(
                run_case(
                    codex_bin=args.codex_bin,
                    root=root,
                    count=count,
                    model=args.model,
                    timeout_s=args.timeout_s,
                    idle_sample_s=args.idle_sample_s,
                    budget=budget,
                    teardown_mode=args.teardown_mode,
                )
            )
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
