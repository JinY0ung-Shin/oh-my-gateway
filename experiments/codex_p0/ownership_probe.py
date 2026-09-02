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
    ) -> None:
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.cwd = cwd
        self.timeout_s = timeout_s
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

        surviving_descendants = [
            child_pid for child_pid in descendant_pids_before if pid_alive(child_pid)
        ]
        return {
            "mode": mode,
            "elapsed_s": round(time.monotonic() - started, 6),
            "returncode": proc.poll(),
            "before": before,
            "descendant_pids_before": descendant_pids_before,
            "surviving_descendants": surviving_descendants,
            "error": error,
        }


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


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    proc_root = Path("/proc") / str(pid)
    if Path("/proc").exists():
        return proc_root.exists()
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
    lock_dir = codex_home / "thread-writer-locks"
    if not lock_dir.exists():
        return []
    return sorted(path.name for path in lock_dir.glob("*.lock"))


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
) -> dict[str, Any]:
    case_dir = root / action
    home = case_dir / "codex-home"
    workspace = case_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    a = AppServer(codex_bin, home, cwd=workspace, timeout_s=timeout_s)
    b: AppServer | None = None
    result: dict[str, Any] = {"action": action, "home": str(home)}
    try:
        result["a_startup_s"] = round(a.start(), 6)
        started = a.start_thread(model=model)
        thread_id = _thread_id(started)
        result["thread_id"] = thread_id
        result["a_resource"] = sample_process(a.pid or -1)
        result["locks_before"] = writer_lock_snapshot(home)

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

        b = AppServer(codex_bin, home, cwd=workspace, timeout_s=timeout_s)
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
    try:
        completed = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return None
    value = (completed.stdout or completed.stderr).strip()
    return value or None


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
        report: dict[str, Any] = {
            "probe": "codex-p0-ownership-resource",
            "created_at_unix": time.time(),
            "platform": platform.platform(),
            "python": sys.version,
            "codex_bin": str(args.codex_bin),
            "codex_version": codex_version(args.codex_bin),
            "root": str(root),
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
