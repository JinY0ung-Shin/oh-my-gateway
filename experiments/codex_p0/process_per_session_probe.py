#!/usr/bin/env python3
"""Measure process-per-session Codex runtime cost against a predeclared P0 budget.

This is an evidence probe for canonical P0 issue #163. It intentionally imports
only the standalone experiment client from ``ownership_probe.py`` and does not
modify or depend on the frozen production Codex backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ownership_probe import (
    AppServer,
    _thread_id,
    descendants,
    pid_alive,
    pid_state,
    runtime_identity,
    sample_process,
)


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


def budget_provenance(path: Path, budget: dict[str, Any]) -> dict[str, Any]:
    """Identity of the budget every verdict in the artifact was judged against.

    A locally filled placeholder budget can make every check pass mechanically,
    so the JSON must be impossible to quote out of context: it names the budget
    file, its SHA-256, the deployment it claims to describe
    (`deployment_budget_name`, default "unlabeled") and whether the budget is
    `budget_finalized`. Topology certification is asserted only when the budget
    itself is finalized; otherwise a pass is `passes_supplied_c0_budget` and no
    more.
    """
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "deployment_budget_name": budget.get("deployment_budget_name") or "unlabeled",
        "finalized": bool(budget.get("budget_finalized", False)),
    }


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


def proc_stat_times(pid: int) -> tuple[int, int, int, int] | None:
    """(utime, stime, cutime, cstime) clock ticks from /proc/<pid>/stat.

    Parsed after the closing parenthesis of ``comm`` so a command name with
    spaces cannot shift the fields. Readable for zombies too, which is what
    makes the process-tree accounting below complete.
    """
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        rest = text[text.rindex(")") + 2 :].split()
        # rest[0] is field 3 (state); utime/stime/cutime/cstime are fields 14-17.
        return int(rest[11]), int(rest[12]), int(rest[13]), int(rest[14])
    except (ValueError, IndexError):
        return None


def tree_pids(root_pid: int) -> list[int]:
    """Root plus every descendant, zombies included (their CPU is still readable)."""
    if pid_state(root_pid)[0] is None:
        return []
    return [root_pid, *descendants(root_pid)]


def tree_cpu_snapshot(servers: list[AppServer]) -> dict[str, Any] | None:
    """Total CPU ticks consumed by every server's process tree since spawn.

    Complete under PID churn, which per-PID deltas are not: a live or zombie
    process contributes its own utime+stime, and once it is reaped that time
    moves into its parent's cutime+cstime. A child that is born and dies
    between two snapshots is therefore still counted, in the parent. A
    snapshot is retried when a PID vanishes between listing and read; None
    means no consistent snapshot could be taken.
    """
    for _attempt in range(5):
        pids = sorted(
            {pid for server in servers if server.pid is not None for pid in tree_pids(server.pid)}
        )
        total = 0
        consistent = True
        for pid in pids:
            times = proc_stat_times(pid)
            if times is None:
                consistent = False
                break
            total += sum(times)
        if consistent:
            return {"ticks": total, "pid_count": len(pids)}
    return None


def settle_process_tree(
    servers: list[AppServer],
    *,
    stable_s: float = 1.0,
    max_s: float = 20.0,
    quiescent_cpu_percent: float = 2.0,
) -> dict[str, Any]:
    """Wait until the tree is idle: running PID set unchanged AND tree CPU below
    ``quiescent_cpu_percent`` for ``stable_s``.

    app-server shells out to git on every thread/start and does post-turn work
    after a materialized turn; sampling while that is in flight measures the
    tail of startup, not the parked-session steady state. The CPU burned while
    settling is recorded (``cpu_percent_during_settle``) so the transient is
    visible, not hidden. If the tree never quiesces within ``max_s`` the sample
    proceeds anyway with ``settled: False`` and the measured number stands.
    """
    try:
        ticks_per_second = float(os.sysconf(os.sysconf_names["SC_CLK_TCK"]))
    except (AttributeError, KeyError, OSError, ValueError):
        ticks_per_second = None
    started = time.monotonic()
    previous_pids: tuple[int, ...] | None = None
    stable_since = started
    first = tree_cpu_snapshot(servers)
    last_snapshot = first
    last_at = started
    while True:
        current = tuple(
            sorted(
                pid
                for server in servers
                if server.pid is not None
                for pid in tree_pids(server.pid)
                if not (pid_state(pid)[0] or "Z").startswith("Z")
            )
        )
        now = time.monotonic()
        snapshot = tree_cpu_snapshot(servers)
        recent_cpu = None
        if (
            snapshot is not None
            and last_snapshot is not None
            and ticks_per_second
            and now > last_at
        ):
            recent_cpu = (
                (snapshot["ticks"] - last_snapshot["ticks"]) / ticks_per_second / (now - last_at) * 100.0
            )
        busy = recent_cpu is None or recent_cpu > quiescent_cpu_percent
        if current != previous_pids or busy:
            previous_pids = current
            stable_since = now
        settle_cpu = None
        if first is not None and snapshot is not None and ticks_per_second and now > started:
            settle_cpu = round(
                (snapshot["ticks"] - first["ticks"]) / ticks_per_second / (now - started) * 100.0, 4
            )
        if now - stable_since >= stable_s or now - started >= max_s:
            return {
                "settled": now - stable_since >= stable_s,
                "elapsed_s": round(now - started, 6),
                "running_pids": len(current),
                "cpu_percent_during_settle": settle_cpu,
                "quiescence_threshold_percent": quiescent_cpu_percent,
            }
        last_snapshot = snapshot
        last_at = now
        time.sleep(0.25)


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
        # A zombie holds no RSS and its fds are closed; it has no VmRSS line, so
        # counting it would only turn the aggregate into "unknown". It is still
        # counted by the CPU accounting (its ticks remain readable until reap).
        if (pid_state(pid)[0] or "Z").startswith("Z"):
            continue
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
    """Aggregate idle CPU of every server's process tree over a bounded window.

    Accounting is tree-wide (own utime+stime plus reaped children's
    cutime+cstime, zombies included) after a settling phase, so short-lived
    git children no longer make the sample permanently incomplete. The result
    is ``complete: False`` only when no consistent snapshot could be taken, a
    server root died during the window, or the tree lost members to
    reparenting (negative delta) -- never a partial sum presented as a number.
    """
    settle = settle_process_tree(servers)
    before = tree_cpu_snapshot(servers)
    started = time.monotonic()
    time.sleep(seconds)
    elapsed = time.monotonic() - started
    after = tree_cpu_snapshot(servers)
    roots_alive = all(server.pid is not None and pid_alive(server.pid) for server in servers)

    base: dict[str, Any] = {
        "elapsed_s": round(elapsed, 6),
        "settle": settle,
        "accounting": (
            "process tree: own utime+stime plus reaped-children cutime+cstime, "
            "zombies included, delta over the window"
        ),
    }
    try:
        ticks_per_second = float(os.sysconf(os.sysconf_names["SC_CLK_TCK"]))
    except (AttributeError, KeyError, OSError, ValueError):
        return {**base, "aggregate_cpu_percent": None, "complete": False, "reason": "CLK_TCK unavailable"}
    if before is None or after is None:
        return {
            **base,
            "aggregate_cpu_percent": None,
            "complete": False,
            "reason": "no consistent process-tree snapshot after retries",
        }
    delta = after["ticks"] - before["ticks"]
    if not roots_alive or delta < 0:
        return {
            **base,
            "aggregate_cpu_percent": None,
            "complete": False,
            "reason": "server root died or tree lost members during the window",
            "pid_count_before": before["pid_count"],
            "pid_count_after": after["pid_count"],
        }
    cpu_seconds = delta / ticks_per_second
    return {
        **base,
        "aggregate_cpu_percent": round(cpu_seconds / elapsed * 100.0, 4) if elapsed > 0 else None,
        "complete": True,
        "pid_count_before": before["pid_count"],
        "pid_count_after": after["pid_count"],
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
    materialize_upstream: str | None = None,
    budget_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case_root = root / f"process-per-session-{count}"
    shared_home = case_root / "codex-home"
    servers: list[AppServer] = []
    startup_times: list[float] = []
    thread_start_times: list[float] = []
    materializations: list[dict[str, Any]] = []
    mode = "materialized" if materialize_upstream else "unmaterialized"
    result: dict[str, Any] = {
        "count": count,
        "shared_codex_home": str(shared_home),
        # unmaterialized = cold/idle lower bound; materialized = the C0 1:1
        # topology certification case. thread/start alone persists no rollout,
        # so only a session that completed a turn is a live durable session.
        "mode": mode,
        "materialized_count": 0,
    }

    try:
        for index in range(count):
            workspace = case_root / "workspaces" / str(index)
            workspace.mkdir(parents=True, exist_ok=True)
            server = AppServer(
                codex_bin,
                shared_home,
                cwd=workspace,
                timeout_s=timeout_s,
                materialize_upstream=materialize_upstream,
            )
            # Register BEFORE start(): start() both spawns the process and runs
            # initialize(), and initialize() raises on timeout. Registering after
            # it would leave a spawned runtime outside the cleanup loop -- the
            # exact orphan this probe exists to count. stop() tolerates an
            # object that never spawned.
            servers.append(server)
            startup_times.append(server.start())
            thread_started = time.monotonic()
            thread_id = _thread_id(server.start_thread(model=model))
            thread_start_times.append(time.monotonic() - thread_started)
            if materialize_upstream:
                materialized = server.run_text_turn(thread_id)
                materializations.append(materialized)
                if not materialized.get("ok"):
                    raise RuntimeError(
                        f"materialization failed for process {index}: {materialized}"
                    )
                result["materialized_count"] = len(materializations)

        runtime_pids = all_runtime_pids(servers)
        result["resource"] = aggregate_process_sample(runtime_pids)
        result["cold_start_times_s"] = [round(value, 6) for value in startup_times]
        result["cold_start_p95_s"] = percentile95(startup_times)
        result["thread_start_times_s"] = [round(value, 6) for value in thread_start_times]
        result["idle_cpu"] = measure_idle_cpu(servers, idle_sample_s)
        result["stderr"] = [stderr_tail_stats(server) for server in servers]
        if materialize_upstream:
            result["materialization_turn_times_s"] = [
                m.get("elapsed_s") for m in materializations
            ]
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
        evaluation = evaluate(result, budget)
        evaluation["mode"] = mode
        materialized_ok = mode == "materialized" and result["materialized_count"] == count
        evaluation["materialized_all_sessions"] = materialized_ok
        # Only a materialized run in which every session completed a turn can
        # satisfy the 1 process : 1 live durable session budget. An
        # unmaterialized pass is a lower bound and must not satisfy the budget.
        evaluation["passes_supplied_c0_budget"] = bool(
            materialized_ok and evaluation.get("status") == "pass"
        )
        # ...and even that only becomes a topology certification when the budget
        # it was judged against is the finalized deployment budget, not a
        # placeholder. The budget identity rides along so neither can be quoted
        # without the other.
        evaluation["budget"] = budget_meta or {"finalized": False, "deployment_budget_name": "unlabeled"}
        evaluation["certifies_c0_topology"] = bool(
            evaluation["passes_supplied_c0_budget"] and evaluation["budget"]["finalized"]
        )
        result["evaluation"] = evaluation
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
    parser.add_argument(
        "--materialize-upstream",
        metavar="BASE_URL",
        help=(
            "Responses upstream for one text turn per session (mock_responses_upstream.py). "
            "With it each process holds a materialized durable session -- the C0 1:1 "
            "topology certification case; without it the run is an unmaterialized idle "
            "lower bound and cannot certify the budget"
        ),
    )
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

    runtime = runtime_identity(args.codex_bin)
    budget_meta = budget_provenance(args.budget, budget)
    report = {
        "budget": budget,
        "budget_provenance": budget_meta,
        "codex_bin": args.codex_bin,
        "codex_version": runtime["codex_version"],
        "runtime": runtime,
        "model": args.model,
        "mode": "materialized" if args.materialize_upstream else "unmaterialized",
        "mode_note": (
            "unmaterialized = cold/idle lower bound (thread/start only, no rollout); "
            "materialized = one completed turn per session, the only mode whose pass "
            "certifies the C0 1 process : 1 session topology"
        ),
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
                    materialize_upstream=args.materialize_upstream,
                    budget_meta=budget_meta,
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
