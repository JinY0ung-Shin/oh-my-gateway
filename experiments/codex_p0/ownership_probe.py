#!/usr/bin/env python3
"""P0 Codex app-server ownership/resource probe.

This intentionally lives outside ``src/backends/codex``. The production Codex
backend is frozen; issue #164 requires evidence about the upstream runtime
before choosing the replacement transport/topology.

The probe never starts a model turn. It exercises only app-server lifecycle,
thread persistence/resume, writer ownership, and process resource facts.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import platform
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


_EOF = object()


class AppServer:
    """Small synchronous JSON-RPC probe client for one app-server process."""

    def __init__(
        self,
        codex_bin: str,
        codex_home: Path,
        *,
        cwd: Path,
        timeout_s: float,
        materialize_upstream: str | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.cwd = cwd
        self.timeout_s = timeout_s
        self.materialize_upstream = materialize_upstream
        self.proc: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[object] = queue.Queue()
        self._stderr = collections.deque(maxlen=200)
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self.notifications: list[dict[str, Any]] = []
        self.server_requests: list[dict[str, Any]] = []
        self._next_id = 1

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def start(self) -> float:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        self.codex_home.mkdir(parents=True, exist_ok=True)
        if self.materialize_upstream:
            write_provider_config(self.codex_home, self.materialize_upstream)
        started_at = time.monotonic()
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(
            [self.codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.cwd,
            env=env,
            bufsize=1,
            **kwargs,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        self.initialize()
        return time.monotonic() - started_at

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for raw in self.proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    self._messages.put(json.loads(raw))
                except json.JSONDecodeError:
                    self._messages.put({"_invalid_json": raw})
        finally:
            self._messages.put(_EOF)

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in self.proc.stderr:
            self._stderr.append(raw.rstrip("\n"))

    def _write(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            raise RuntimeError("app-server is not running")
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(self._next_id)
        self._next_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {method}; stderr={self.stderr_tail()}")
            try:
                item = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(
                    f"timed out waiting for {method}; stderr={self.stderr_tail()}"
                ) from exc
            if item is _EOF:
                raise RuntimeError(
                    f"app-server stdout closed while waiting for {method}; stderr={self.stderr_tail()}"
                )
            if not isinstance(item, dict):
                continue
            if item.get("_invalid_json"):
                raise RuntimeError(f"invalid app-server JSON: {item['_invalid_json']!r}")

            # Server-originated request. The ownership probe never expects to
            # service one, but reply deterministically rather than hanging the server.
            if "method" in item and "id" in item:
                self.server_requests.append(item)
                self._write(
                    {
                        "id": item["id"],
                        "error": {"code": -32601, "message": "P0 probe does not service requests"},
                    }
                )
                continue

            if "method" in item:
                self.notifications.append(item)
                continue

            if str(item.get("id")) != request_id:
                # This probe is intentionally single-request-at-a-time. Keep
                # unexpected responses visible in the output rather than hiding them.
                self.notifications.append({"_unexpected_response": item})
                continue

            if "error" in item:
                return {"ok": False, "error": item["error"]}
            return {"ok": True, "result": item.get("result")}

    def initialize(self) -> None:
        response = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "oh_my_gateway_codex_p0",
                    "title": "oh-my-gateway Codex P0 probe",
                    "version": "0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        if not response["ok"]:
            raise RuntimeError(f"initialize failed: {response['error']}")
        self.notify("initialized")

    def start_thread(self, *, model: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": str(self.cwd), "ephemeral": False}
        if model:
            params["model"] = model
        return self.request("thread/start", params)

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        return self.request("thread/resume", {"threadId": thread_id, "cwd": str(self.cwd)})

    def wait_for_notification(self, predicate, timeout_s: float) -> dict[str, Any] | None:
        """Drain notifications until ``predicate(msg)`` is true or the deadline passes.

        Server-originated requests met here are declined deterministically, as in
        ``request()``; EOF raises so a dead runtime is never mistaken for a slow one.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                item = self._messages.get(timeout=remaining)
            except queue.Empty:
                return None
            if item is _EOF:
                raise RuntimeError(
                    f"app-server stdout closed while waiting for a notification; stderr={self.stderr_tail()}"
                )
            if not isinstance(item, dict):
                continue
            if "method" in item and "id" in item:
                self.server_requests.append(item)
                self._write(
                    {
                        "id": item["id"],
                        "error": {"code": -32601, "message": "P0 probe does not service requests"},
                    }
                )
                continue
            if "method" in item:
                self.notifications.append(item)
                if predicate(item):
                    return item

    def run_text_turn(self, thread_id: str, *, timeout_s: float = 60.0) -> dict[str, Any]:
        """Run one text-only turn so the thread is materialized (rollout written).

        ``thread/start`` alone persists nothing: app-server reports the thread as
        "not materialized yet ... before first user message" and ``thread/resume``
        in a replacement process fails with "no rollout found". Ownership and
        resume measurements are therefore only meaningful on a thread that has
        completed at least one turn. The upstream is expected to be the hermetic
        Responses upstream from this directory (see README), not a real model.
        """
        started = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Reply with exactly CHATDRAGON_P0_MATERIALIZE_OK"}],
            },
        )
        if not started.get("ok"):
            return {"ok": False, "stage": "turn/start", "error": started.get("error")}
        turn = (started.get("result") or {}).get("turn") or {}
        turn_id = turn.get("id")
        began = time.monotonic()
        done = self.wait_for_notification(
            lambda m: m.get("method") == "turn/completed"
            and ((m.get("params") or {}).get("turn") or {}).get("id") == turn_id,
            timeout_s,
        )
        return {
            "ok": done is not None,
            "turn_id": turn_id,
            "elapsed_s": round(time.monotonic() - began, 6),
            "status": ((done or {}).get("params") or {}).get("turn", {}).get("status"),
            "error": None if done is not None else f"no turn/completed within {timeout_s}s",
        }

    def stderr_tail(self, limit: int = 40) -> str:
        return "\n".join(list(self._stderr)[-limit:])

    def stop(self, mode: str = "stdin_eof", timeout_s: float = 5.0) -> dict[str, Any]:
        if self.proc is None:
            return {"mode": mode, "already_stopped": True}
        proc = self.proc
        before = sample_process(proc.pid)
        descendant_pids_before = before.get("descendants", [])
        started = time.monotonic()
        error: str | None = None
        try:
            if mode == "stdin_eof":
                if proc.stdin is not None:
                    proc.stdin.close()
            elif mode == "sigterm":
                _signal_process_tree(proc, signal.SIGTERM)
            elif mode == "sigkill":
                kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
                _signal_process_tree(proc, kill_signal)
            else:
                raise ValueError(f"unsupported stop mode: {mode}")

            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                error = f"{mode} did not exit within {timeout_s}s; forced kill"
                _signal_process_tree(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
                proc.wait(timeout=timeout_s)
        except Exception as exc:  # probe must report cleanup failures, not hide them
            error = f"{type(exc).__name__}: {exc}"

        survivors = classify_survivors(descendant_pids_before)
        return {
            "mode": mode,
            "elapsed_s": round(time.monotonic() - started, 6),
            "returncode": proc.poll(),
            "before": before,
            "descendant_pids_before": descendant_pids_before,
            # Genuinely running orphans only; zombies awaiting reap are listed
            # separately and are not leaks.
            "surviving_descendants": sorted(survivors["running"]),
            "surviving_descendant_states": survivors["running"],
            "zombie_descendants": survivors["zombie"],
            "survivor_grace_s": survivors["grace_s"],
            "error": error,
        }


def write_provider_config(codex_home: Path, base_url: str) -> None:
    """Point this CODEX_HOME at a Responses upstream using public provider fields only."""
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                'model = "replica-model"',
                'model_provider = "p0"',
                'approval_policy = "never"',
                'sandbox_mode = "read-only"',
                'web_search = "disabled"',
                "",
                "[model_providers.p0]",
                'name = "P0 hermetic upstream"',
                f'base_url = "{base_url.rstrip("/")}"',
                'wire_api = "responses"',
                "requires_openai_auth = false",
                "request_max_retries = 0",
                "stream_max_retries = 0",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _signal_process_tree(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return
        except ProcessLookupError:
            return
    proc.send_signal(sig)


def pid_state(pid: int) -> tuple[str | None, str | None]:
    """(State letter, comm) from /proc, or (None, None) if gone/unavailable."""
    try:
        text = (Path("/proc") / str(pid) / "status").read_text()
    except OSError:
        return None, None
    state = comm = None
    for line in text.splitlines():
        if line.startswith("State:"):
            state = line.split()[1]
        elif line.startswith("Name:"):
            comm = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
    return state, comm


def classify_survivors(pids: list[int], grace_s: float = 2.0) -> dict[str, Any]:
    """Split descendant PIDs into genuinely running vs zombie after a grace poll.

    Only ``running`` counts as an orphan. Zombies are dead processes awaiting
    reap by their reparented parent -- a transient /proc entry, not a leak.
    """
    deadline = time.monotonic() + grace_s
    running: dict[int, str] = {}
    zombies: dict[int, str] = {}
    while True:
        running.clear()
        zombies.clear()
        for pid in pids:
            state, comm = pid_state(pid)
            if state is None:
                continue
            (zombies if state.startswith("Z") else running)[pid] = f"{state} {comm or ''}".strip()
        if not running or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    return {
        "running": running,
        "zombie": zombies,
        "grace_s": grace_s,
    }


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    proc_root = Path("/proc") / str(pid)
    if Path("/proc").exists():
        state, _ = pid_state(pid)
        return state is not None and not state.startswith("Z")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def descendants(root_pid: int) -> list[int]:
    """Best-effort descendant PID list using `ps`; empty when unavailable."""
    ps = shutil.which("ps")
    if ps is None:
        return []
    try:
        result = subprocess.run(
            [ps, "-eo", "pid=,ppid="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = collections.defaultdict(list)
    for raw in result.stdout.splitlines():
        parts = raw.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children[ppid].append(pid)
    found: list[int] = []
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, []))
    return sorted(found)


def sample_process(pid: int) -> dict[str, Any]:
    sample: dict[str, Any] = {"pid": pid, "descendants": descendants(pid)}
    proc_root = Path("/proc") / str(pid)
    if not proc_root.exists():
        sample["alive"] = pid_alive(pid)
        return sample
    sample["alive"] = True
    try:
        sample["fd_count"] = len(list((proc_root / "fd").iterdir()))
    except OSError:
        sample["fd_count"] = None
    try:
        for line in (proc_root / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                sample["rss_kib"] = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        sample["rss_kib"] = None
    return sample


def writer_lock_snapshot(codex_home: Path) -> list[str]:
    """Per-thread writer lock files only.

    ``.coordination.lock`` lives in the same directory but is the serialization
    primitive *around* the per-thread locks (acquire/cleanup/drop all take it
    first), not a writer claim. ``pathlib.Path.glob`` matches dotfiles -- unlike
    ``glob.glob`` -- so it must be excluded explicitly or every snapshot and
    ``writer_lock_count`` is inflated by one.
    """
    lock_dir = codex_home / "thread-writer-locks"
    if not lock_dir.exists():
        return []
    return sorted(
        path.name for path in lock_dir.glob("*.lock") if not path.name.startswith(".")
    )


def coordination_lock_present(codex_home: Path) -> bool:
    return (codex_home / "thread-writer-locks" / ".coordination.lock").exists()


def _thread_id(response: dict[str, Any]) -> str:
    if not response.get("ok"):
        raise RuntimeError(f"thread/start failed: {response.get('error')}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"thread/start returned non-object: {result!r}")
    thread = result.get("thread")
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        raise RuntimeError(f"thread/start response missing thread id: {result!r}")
    return thread["id"]


def run_ownership_case(
    codex_bin: str,
    root: Path,
    *,
    action: str,
    model: str | None,
    timeout_s: float,
    materialize_upstream: str | None = None,
) -> dict[str, Any]:
    case_dir = root / action
    home = case_dir / "codex-home"
    workspace = case_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    a = AppServer(
        codex_bin, home, cwd=workspace, timeout_s=timeout_s, materialize_upstream=materialize_upstream
    )
    b: AppServer | None = None
    result: dict[str, Any] = {"action": action, "home": str(home)}
    try:
        result["a_startup_s"] = round(a.start(), 6)
        started = a.start_thread(model=model)
        thread_id = _thread_id(started)
        result["thread_id"] = thread_id
        if materialize_upstream:
            result["materialize"] = a.run_text_turn(thread_id)
            if not result["materialize"]["ok"]:
                raise RuntimeError(f"materializing turn failed: {result['materialize']}")
        else:
            result["materialize"] = {
                "ok": False,
                "skipped": True,
                "warning": (
                    "thread not materialized: thread/start alone writes no rollout, so "
                    "thread/resume is expected to fail with 'no rollout found' before "
                    "any writer-lock contention is reached"
                ),
            }
        result["a_resource"] = sample_process(a.pid or -1)
        result["locks_before"] = writer_lock_snapshot(home)
        result["coordination_lock_present"] = coordination_lock_present(home)

        if action == "healthy_conflict":
            pass
        elif action == "sigstop":
            if os.name != "posix" or not hasattr(signal, "SIGSTOP"):
                result["skipped"] = "SIGSTOP is unavailable on this platform"
                return result
            os.kill(a.pid, signal.SIGSTOP)  # type: ignore[arg-type]
            result["a_sigstop"] = True
        elif action in {"stdin_eof", "sigterm", "sigkill"}:
            result["a_stop"] = a.stop(action)
        else:
            raise ValueError(action)

        b = AppServer(
            codex_bin, home, cwd=workspace, timeout_s=timeout_s, materialize_upstream=materialize_upstream
        )
        result["b_startup_s"] = round(b.start(), 6)
        result["b_resume"] = b.resume_thread(thread_id)
        result["b_resource"] = sample_process(b.pid or -1)
        result["locks_during_b"] = writer_lock_snapshot(home)

        if action == "sigstop":
            os.kill(a.pid, signal.SIGCONT)  # type: ignore[arg-type]
            result["a_sigcont"] = True
            time.sleep(0.1)
            result["a_after_sigcont"] = sample_process(a.pid or -1)
        return result
    except Exception as exc:
        result["exception"] = f"{type(exc).__name__}: {exc}"
        result["a_stderr"] = a.stderr_tail()
        if b is not None:
            result["b_stderr"] = b.stderr_tail()
        return result
    finally:
        if action == "sigstop" and a.proc is not None and a.proc.poll() is None:
            try:
                os.kill(a.proc.pid, signal.SIGCONT)
            except (ProcessLookupError, AttributeError):
                pass
        if b is not None:
            result.setdefault("b_cleanup", b.stop("stdin_eof"))
        result.setdefault("a_cleanup", a.stop("stdin_eof"))
        result["locks_after_cleanup"] = writer_lock_snapshot(home)


def run_density_case(
    codex_bin: str,
    root: Path,
    *,
    density: int,
    model: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    case_dir = root / f"density-{density}"
    home = case_dir / "codex-home"
    workspace = case_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    server = AppServer(codex_bin, home, cwd=workspace, timeout_s=timeout_s)
    result: dict[str, Any] = {"density": density, "home": str(home)}
    try:
        result["startup_s"] = round(server.start(), 6)
        thread_ids: list[str] = []
        started = time.monotonic()
        for _ in range(density):
            thread_ids.append(_thread_id(server.start_thread(model=model)))
        result["thread_start_total_s"] = round(time.monotonic() - started, 6)
        result["thread_count"] = len(thread_ids)
        result["resource"] = sample_process(server.pid or -1)
        result["writer_lock_count"] = len(writer_lock_snapshot(home))
        return result
    except Exception as exc:
        result["exception"] = f"{type(exc).__name__}: {exc}"
        result["stderr"] = server.stderr_tail()
        return result
    finally:
        result["cleanup"] = server.stop("stdin_eof")


def codex_version(codex_bin: str) -> str | None:
    """Best-effort ``codex --version``; never raises.

    A cold-cache first invocation of the pinned binary was measured at ~8 s, so
    the bound is generous and a timeout is recorded rather than crashing the
    probe before any case runs.
    """
    try:
        completed = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "unavailable: --version timed out after 30s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {type(exc).__name__}"
    value = (completed.stdout or completed.stderr).strip()
    return value or None


def runtime_identity(codex_bin: str) -> dict[str, Any]:
    """Exact identity of the runtime under test, for every standalone artifact.

    A path is not a pin: the file behind ``/opt/codex/codex`` can change between
    runs. Every probe report therefore carries the resolved path, the binary's
    SHA-256 and size, and ``codex --version`` together, so a JSON artifact can
    be interpreted on its own without trusting the path it names.
    """
    identity: dict[str, Any] = {
        "codex_bin": str(codex_bin),
        "resolved_path": None,
        "sha256": None,
        "size_bytes": None,
        "codex_version": codex_version(codex_bin),
    }
    candidate = shutil.which(codex_bin) or codex_bin
    try:
        path = Path(candidate).resolve(strict=True)
    except OSError:
        return identity
    identity["resolved_path"] = str(path)
    if path.is_file():
        digest = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            identity["sha256"] = digest.hexdigest()
            identity["size_bytes"] = path.stat().st_size
        except OSError as exc:
            identity["sha256_error"] = type(exc).__name__
    return identity


def parse_densities(raw: str) -> list[int]:
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value <= 0:
            raise ValueError("densities must be positive integers")
        values.append(value)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default=os.getenv("CODEX_BIN", "codex"))
    parser.add_argument("--model", default=os.getenv("CODEX_P0_MODEL"))
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--densities", default="1,10,50,100")
    parser.add_argument("--skip-density", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-root", type=Path)
    parser.add_argument(
        "--materialize-upstream",
        metavar="BASE_URL",
        help=(
            "Responses upstream used to run one text turn per thread before ownership "
            "actions, e.g. http://127.0.0.1:8099/v1 from mock_responses_upstream.py. "
            "Without it threads are never materialized and resume cannot succeed."
        ),
    )
    args = parser.parse_args()

    if shutil.which(args.codex_bin) is None and not Path(args.codex_bin).exists():
        parser.error(f"Codex binary not found: {args.codex_bin}")

    if args.keep_root:
        root = args.keep_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        temp_context = None
    else:
        temp_context = tempfile.TemporaryDirectory(prefix="omg-codex-p0-")
        root = Path(temp_context.name)

    try:
        runtime = runtime_identity(args.codex_bin)
        report: dict[str, Any] = {
            "probe": "codex-p0-ownership-resource",
            "created_at_unix": time.time(),
            "platform": platform.platform(),
            "python": sys.version,
            "codex_bin": str(args.codex_bin),
            "codex_version": runtime["codex_version"],
            "runtime": runtime,
            "root": str(root),
            "materialization": {
                "enabled": bool(args.materialize_upstream),
                "note": (
                    "thread/start alone persists no rollout (app-server: 'not materialized "
                    "yet ... before first user message'); ownership/resume evidence requires "
                    "one completed turn per thread"
                ),
            },
            "ownership": [],
            "density": [],
        }

        for action in ("healthy_conflict", "sigstop", "stdin_eof", "sigterm", "sigkill"):
            report["ownership"].append(
                run_ownership_case(
                    args.codex_bin,
                    root,
                    action=action,
                    model=args.model,
                    timeout_s=args.timeout,
                    materialize_upstream=args.materialize_upstream,
                )
            )

        if not args.skip_density:
            for density in parse_densities(args.densities):
                report["density"].append(
                    run_density_case(
                        args.codex_bin,
                        root,
                        density=density,
                        model=args.model,
                        timeout_s=args.timeout,
                    )
                )

        encoded = json.dumps(report, indent=2, sort_keys=True)
        print(encoded)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n", encoding="utf-8")
        return 0
    finally:
        if temp_context is not None:
            temp_context.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
