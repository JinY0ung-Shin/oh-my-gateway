"""Per-user Docker container sandbox for complete user isolation."""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, Optional

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)


def _safe_container_name(user: str) -> str:
    safe = re.sub(r"[.@]+", "-", user)
    return "claude-sandbox-" + safe


@dataclass
class SandboxContainer:
    user: str
    container_name: str
    container_id: str
    internal_url: str
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_accessed = time.time()


class DockerSandboxManager:

    def __init__(self, *, image="claude-code-gateway:latest", network="",
                 cpu_limit="1.0", memory_limit="2g", idle_timeout=3600,
                 max_containers=50, workspace_base="/data/sandboxes"):
        self.image = image
        self._configured_network = network
        self._resolved_network: Optional[str] = None
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        self.idle_timeout = idle_timeout
        self.max_containers = max_containers
        self.workspace_base = workspace_base
        self.containers: Dict[str, SandboxContainer] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def network(self) -> str:
        return self._resolved_network or self._configured_network or "bridge"

    async def get_or_create(self, user: str) -> SandboxContainer:
        async with self._lock:
            if user in self.containers:
                c = self.containers[user]
                if await self._is_running(c.container_id):
                    c.touch()
                    return c
                del self.containers[user]
            if len(self.containers) >= self.max_containers:
                await self._evict_idle_unlocked()
                if len(self.containers) >= self.max_containers:
                    raise RuntimeError("Max sandbox containers (%d) reached" % self.max_containers)
            container = await self._create_container(user)
            self.containers[user] = container
            return container

    async def remove_container(self, user: str) -> None:
        async with self._lock:
            container = self.containers.pop(user, None)
        if container:
            await _run_cmd(["docker", "rm", "-f", container.container_name], check=False)
            logger.info("Removed sandbox for user=%s", user)

    async def cleanup_idle(self) -> int:
        now = time.time()
        to_remove = [u for u, c in self.containers.items() if now - c.last_accessed > self.idle_timeout]
        for user in to_remove:
            await self.remove_container(user)
        return len(to_remove)

    async def shutdown(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for user in list(self.containers):
            await self.remove_container(user)

    def start_cleanup_task(self, interval_minutes=5):
        async def _loop():
            while True:
                await asyncio.sleep(interval_minutes * 60)
                try: await self.cleanup_idle()
                except asyncio.CancelledError: raise
                except Exception: logger.exception("Sandbox cleanup failed")
        try:
            asyncio.get_running_loop().create_task(_loop())
        except RuntimeError:
            pass

    def get_stats(self) -> dict:
        now = time.time()
        return {"active": len(self.containers), "max": self.max_containers,
                "containers": {u: {"id": c.container_id, "name": c.container_name,
                    "url": c.internal_url, "idle": int(now - c.last_accessed)}
                    for u, c in self.containers.items()}}

    async def _create_container(self, user: str) -> SandboxContainer:
        name = _safe_container_name(user)
        wdir = os.path.join(self.workspace_base, user)
        os.makedirs(wdir, exist_ok=True)

        await self._resolve_network()
        await _run_cmd(["docker", "rm", "-f", name], check=False)

        cmd = [
            "docker", "run", "-d",
            "--name", name, "--network", self.network,
            "--cpus", self.cpu_limit, "--memory", self.memory_limit,
            "--memory-swap", self.memory_limit, "--pids-limit", "512",
            "--security-opt", "no-new-privileges",
            "-v", "%s:/workspace" % wdir,
        ]
        # Mount .claude read-write so CLI can update tokens/config
        claude_home = os.path.expanduser("~/.claude")
        if os.path.isdir(claude_home):
            cmd.extend(["-v", "%s:/root/.claude" % claude_home])

        for k, v in self._build_env_vars().items():
            cmd.extend(["-e", "%s=%s" % (k, v)])
        cmd.append(self.image)

        try:
            result = await _run_cmd(cmd)
            cid = result.strip()[:12]
            ip = await self._get_container_ip(name)
            url = "http://%s:8000" % ip
            await self._wait_for_healthy(url, timeout=120)
            logger.info("Created sandbox user=%s name=%s ip=%s net=%s", user, name, ip, self.network)
            return SandboxContainer(user=user, container_name=name, container_id=cid, internal_url=url)
        except Exception as e:
            await _run_cmd(["docker", "rm", "-f", name], check=False)
            raise RuntimeError("Failed to create sandbox for %r: %s" % (user, e)) from e

    def _build_env_vars(self) -> Dict[str, str]:
        env = {"DOCKER_SANDBOX_ROLE": "worker", "CLAUDE_CWD": "/workspace",
               "PORT": "8000", "ADMIN_API_KEY": "sandbox-internal-key",
               "RATE_LIMIT_ENABLED": "false"}
        for key in ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                    "DEFAULT_MODEL", "THINKING_MODE", "THINKING_BUDGET_TOKENS",
                    "TOKEN_STREAMING", "DEFAULT_MAX_TURNS", "MAX_TIMEOUT",
                    "CLAUDE_SANDBOX_ENABLED", "CLAUDE_SANDBOX_AUTO_ALLOW_BASH",
                    "http_proxy", "https_proxy", "no_proxy",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                    "ALL_PROXY", "all_proxy",
                    "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE",
                    "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"]:
            val = os.getenv(key, "")
            if val:
                env[key] = val
        env["CLAUDE_SANDBOX_WEAKER_NESTED"] = os.getenv("CLAUDE_SANDBOX_WEAKER_NESTED", "true")
        return env

    async def _resolve_network(self) -> None:
        if self._resolved_network:
            return
        if self._configured_network:
            self._resolved_network = self._configured_network
            return
        my_id = os.getenv("HOSTNAME", "")
        if my_id:
            try:
                result = await _run_cmd(["docker", "inspect", "-f",
                    "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}", my_id])
                for net in result.strip().split():
                    if net not in ("bridge", "host", "none"):
                        self._resolved_network = net
                        logger.info("Auto-detected network: %s", net)
                        return
            except Exception as e:
                logger.warning("Network auto-detect failed: %s", e)
        self._resolved_network = "bridge"

    async def _get_container_ip(self, name: str) -> str:
        result = await _run_cmd(["docker", "inspect", "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name])
        ip = result.strip()
        if not ip:
            raise RuntimeError("No IP for container %s" % name)
        return ip

    async def _is_running(self, cid: str) -> bool:
        try:
            r = await _run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", cid], check=False)
            return r.strip() == "true"
        except Exception:
            return False

    async def _wait_for_healthy(self, base_url: str, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        url = "%s/health" % base_url
        async with httpx.AsyncClient(trust_env=False) as client:
            while time.time() < deadline:
                try:
                    if (await client.get(url, timeout=3)).status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(2)
        raise TimeoutError("Container at %s not healthy after %ds" % (base_url, timeout))

    async def _evict_idle_unlocked(self) -> None:
        if not self.containers:
            return
        u = min(self.containers, key=lambda u: self.containers[u].last_accessed)
        c = self.containers.pop(u)
        await _run_cmd(["docker", "rm", "-f", c.container_name], check=False)


class SandboxProxy:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0), trust_env=False)

    async def forward_json(self, base_url, body, api_key=None):
        url = "%s/v1/responses" % base_url
        try:
            resp = await self._client.post(url, json=body, headers=self._headers(api_key))
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="Sandbox: %s" % resp.text[:500])
            return resp.json()
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Sandbox timed out")
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail="Sandbox unavailable")

    async def forward_stream(self, base_url, body, api_key=None):
        url = "%s/v1/responses" % base_url
        headers = self._headers(api_key)
        async def _gen():
            try:
                async with self._client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        err = await resp.aread()
                        yield b'data: {"error": "Sandbox %d"}\n\n' % resp.status_code
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            except httpx.TimeoutException:
                yield b'data: {"error": "Sandbox timed out"}\n\n'
            except httpx.ConnectError:
                yield b'data: {"error": "Sandbox unavailable"}\n\n'
        return StreamingResponse(_gen(), media_type="text/event-stream")

    @staticmethod
    def _headers(api_key):
        h = {"Content-Type": "application/json"}
        if api_key:
            h["Authorization"] = "Bearer %s" % api_key
        return h

    async def close(self):
        await self._client.aclose()


async def _run_cmd(cmd, *, check=True):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError("%s failed (rc=%d): %s" % (cmd[0], proc.returncode, stderr.decode().strip()))
    return stdout.decode()


sandbox_manager: Optional[DockerSandboxManager] = None
sandbox_proxy: Optional[SandboxProxy] = None

def init_sandbox():
    from src.constants import (DOCKER_SANDBOX_CPU_LIMIT, DOCKER_SANDBOX_IDLE_TIMEOUT,
        DOCKER_SANDBOX_IMAGE, DOCKER_SANDBOX_MAX_CONTAINERS, DOCKER_SANDBOX_MEMORY_LIMIT,
        DOCKER_SANDBOX_NETWORK, DOCKER_SANDBOX_WORKSPACE_BASE)
    global sandbox_manager, sandbox_proxy
    sandbox_manager = DockerSandboxManager(image=DOCKER_SANDBOX_IMAGE, network=DOCKER_SANDBOX_NETWORK,
        cpu_limit=DOCKER_SANDBOX_CPU_LIMIT, memory_limit=DOCKER_SANDBOX_MEMORY_LIMIT,
        idle_timeout=DOCKER_SANDBOX_IDLE_TIMEOUT, max_containers=DOCKER_SANDBOX_MAX_CONTAINERS,
        workspace_base=DOCKER_SANDBOX_WORKSPACE_BASE)
    sandbox_proxy = SandboxProxy()
    logger.info("Docker sandbox initialised (image=%s, max=%d)", DOCKER_SANDBOX_IMAGE, DOCKER_SANDBOX_MAX_CONTAINERS)

async def shutdown_sandbox():
    global sandbox_manager, sandbox_proxy
    if sandbox_manager: await sandbox_manager.shutdown()
    if sandbox_proxy: await sandbox_proxy.close()
