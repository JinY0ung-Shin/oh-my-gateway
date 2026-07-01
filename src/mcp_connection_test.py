"""Safe, timeout-bounded, non-destructive per-server MCP reachability probe.

SECURITY: stdio 'command' is admin input. By default a stdio "test" only
resolves the executable on PATH (shutil.which + X_OK) -- it does NOT spawn it.
Spawning arbitrary operator-supplied commands is an RCE surface with side
effects; gate any spawn behind MCP_TEST_ALLOW_SPAWN. Remote types get a bounded
httpx reachability probe: ANY HTTP response == reachable (MCP endpoints expect
POST/JSON-RPC or SSE, so 4xx/405 is still "reachable"); only connect/timeout/DNS
errors are failure. Never raises into the caller. Never echoes env/headers/secrets.
"""

import asyncio
import os
import shutil
import time
from typing import Any, Dict

import httpx

from src.constants import MCP_TEST_ALLOW_SPAWN, MCP_TEST_TIMEOUT_SECONDS


async def test_mcp_server(
    name: str,
    config: Dict[str, Any],
    *,
    timeout: float = MCP_TEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Probe one MCP server for reachability. Never raises.

    Returns ``{ok, detail, latency_ms, transport}``. Secrets in the config
    (env, headers, tokens) are never echoed into ``detail``.
    """
    transport = config.get("type", "stdio")
    start = time.monotonic()
    try:
        if transport == "stdio":
            result = await asyncio.wait_for(_test_stdio(config), timeout=timeout)
        else:
            result = await asyncio.wait_for(
                _test_remote(config, timeout), timeout=timeout
            )
    except asyncio.TimeoutError:
        result = {"ok": False, "detail": f"timed out after {timeout}s"}
    except Exception as e:
        result = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    result["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
    result["transport"] = transport
    return result


async def _test_stdio(config: Dict[str, Any]) -> Dict[str, Any]:
    command = config.get("command")
    args = config.get("args") if isinstance(config.get("args"), list) else []
    if isinstance(command, list) and command:
        exe = command[0]
        args = list(command[1:]) + list(args)
    else:
        exe = command
    if not exe or not isinstance(exe, str):
        return {"ok": False, "detail": "no command"}
    resolved = await asyncio.to_thread(shutil.which, exe)
    if not resolved:
        if os.path.isabs(exe) and os.path.isfile(exe) and os.access(exe, os.X_OK):
            resolved = exe
        else:
            return {"ok": False, "detail": f"command not found on PATH: {exe}"}
    if not MCP_TEST_ALLOW_SPAWN:
        return {
            "ok": True,
            "detail": (
                f"resolvable: {resolved} "
                "(not spawned; set MCP_TEST_ALLOW_SPAWN to launch)"
            ),
        }
    return await _spawn_stdio(resolved, [str(a) for a in args])


async def _spawn_stdio(resolved: str, args: list) -> Dict[str, Any]:
    """Opt-in spawn: minimal env, short-lived, success == stayed alive briefly.

    NEVER uses shell=True: the resolved path and args are passed as an arg list
    to ``create_subprocess_exec``. The child gets a minimal env (no inherited
    secrets) and is hard-killed regardless of outcome.
    """
    minimal_env = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT")
        if k in os.environ
    }
    proc = await asyncio.create_subprocess_exec(
        resolved,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=minimal_env,
    )
    try:
        # If it exits within ~1s it failed to come up; if it is still running
        # it started successfully. Either way we tear it down immediately.
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            return {"ok": True, "detail": "spawned; still running (killed)"}
        return {"ok": False, "detail": f"exited immediately with rc={rc}"}
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass


async def _test_remote(config: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    url = config.get("url")
    if not url:
        return {"ok": False, "detail": "no url"}
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        # Any response == reachable (MCP endpoints reject GET but still answer).
        return {"ok": True, "detail": f"HTTP {resp.status_code}"}
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        return {"ok": False, "detail": f"connect failed: {type(e).__name__}"}
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"{type(e).__name__}"}
