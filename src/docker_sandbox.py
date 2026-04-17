"""Per-user Docker container sandbox for complete user isolation."""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional

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

    def __init__(
        self,
        *,
        image: str = "claude-code-gateway:latest",
        network: str = "",
        cpu_limit: str = "1.0",
        memory_limit: str = "2g",
        idle_timeout: int = 3600,
        max_containers: int = 50,
        workspace_base: str = "/data/sandboxes",
    ) -> None:
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
                container = self.containers[user]
                if await self._is_running(container.container_id):
                    container.touch()
                    return container
                del self.containers[user]
                logger.warning("Container for user %s died, recreating", user)

            if len(self.containers) >= self.max_containers:
                await self._evict_idle_unlocked()
                if len(self.containers) >= self.max_containers:
                    raise RuntimeError(
                        "Maximum sandbox containers (%d) reached" % self.max_containers
                    )

            container = await self._create_container(user)
            self.containers[user] = container
            return container

    async def remove_container(self, user: str) -> None:
        async with self._lock:
            container = self.containers.pop(user, None)
        if container:
            await _run_cmd(
                ["docker", "rm", "-f", container.container_name], check=False
            )
            logger.info("Removed sandbox container for user=%s", user)

    async def cleanup_idle(self) -> int:
        now = time.time()
        to_remove: list[str] = []
        async with self._lock:
            for user, container in self.containers.items():
                if now - container.last_accessed > self.idle_timeout:
                    to_remove.append(user)
        for user in to_remove:
            await self.remove_container(user)
        if to_remove:
            logger.info("Cleaned up %d idle sandbox containers", len(to_remove))
        return len(to_remove)

    async def shutdown(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for user in list(self.containers.keys()):
            await self.remove_container(user)
        logger.info("Docker sandbox manager shutdown complete")

    def start_cleanup_task(self, interval_minutes: int = 5) -> None:
        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval_minutes * 60)
                try:
                    await self.cleanup_idle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Sandbox cleanup cycle failed")
        try:
            loop = asyncio.get_running_loop()
            self._cleanup_task = loop.create_task(_loop())
            logger.info("Started sandbox cleanup task (interval: %d min)", interval_minutes)
        except RuntimeError:
            logger.warning("No event loop, sandbox cleanup disabled")

    def get_stats(self) -> dict:
        now = time.time()
        return {
            "active_containers": len(self.containers),
            "max_containers": self.max_containers,
            "image": self.image,
            "network": self.network,
            "containers": {
                user: {
                    "container_id": c.container_id,
                    "container_name": c.container_name,
                    "url": c.internal_url,
                    "idle_seconds": int(now - c.last_accessed),
                    "uptime_seconds": int(now - c.created_at),
                }
                for user, c in self.containers.items()
            },
        }

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    async def _create_container(self, user: str) -> SandboxContainer:
        container_name = _safe_container_name(user)
        workspace_dir = os.path.join(self.workspace_base, user)
        os.makedirs(workspace_dir, exist_ok=True)

        # Auto-detect the orchestrator's Docker network on first call
        await self._resolve_network()

        await _run_cmd(["docker", "rm", "-f", container_name], check=False)

        env_vars = self._build_env_vars()

        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "--network", self.network,
            "--cpus", self.cpu_limit,
            "--memory", self.memory_limit,
            "--memory-swap", self.memory_limit,
            "--pids-limit", "512",
            "--security-opt", "no-new-privileges",
            "-v", "%s:/workspace" % workspace_dir,
        ]

        claude_home = os.path.expanduser("~/.claude")
        if os.path.isdir(claude_home):
            cmd.extend(["-v", "%s:/root/.claude:ro" % claude_home])

        for key, value in env_vars.items():
            cmd.extend(["-e", "%s=%s" % (key, value)])

        cmd.append(self.image)

        try:
            result = await _run_cmd(cmd)
            container_id = result.strip()[:12]

            container_ip = await self._get_container_ip(container_name)
            internal_url = "http://%s:8000" % container_ip

            await self._wait_for_healthy(internal_url, timeout=120)

            logger.info(
                "Created sandbox container user=%s name=%s ip=%s network=%s",
                user, container_name, container_ip, self.network,
            )
            return SandboxContainer(
                user=user,
                container_name=container_name,
                container_id=container_id,
                internal_url=internal_url,
            )
        except Exception as e:
            await _run_cmd(["docker", "rm", "-f", container_name], check=False)
            raise RuntimeError(
                "Failed to create sandbox for user %r: %s" % (user, e)
            ) from e

    def _build_env_vars(self) -> Dict[str, str]:
        env: Dict[str, str] = {
            "DOCKER_SANDBOX_ROLE": "worker",
            "CLAUDE_CWD": "/workspace",
            "PORT": "8000",
            "ADMIN_API_KEY": "sandbox-internal-key",
            "RATE_LIMIT_ENABLED": "false",
        }

        _passthrough = [
            # Authentication
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            # Model configuration
            "DEFAULT_MODEL",
            "THINKING_MODE",
            "THINKING_BUDGET_TOKENS",
            "TOKEN_STREAMING",
            "DEFAULT_MAX_TURNS",
            "MAX_TIMEOUT",
            # Bash sandbox
            "CLAUDE_SANDBOX_ENABLED",
            "CLAUDE_SANDBOX_AUTO_ALLOW_BASH",
            # Proxy settings
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "all_proxy",
            # SSL/TLS
            "REQUESTS_CA_BUNDLE",
            "SSL_CERT_FILE",
            "CURL_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
        ]
        for key in _passthrough:
            val = os.getenv(key, "")
            if val:
                env[key] = val

        env["CLAUDE_SANDBOX_WEAKER_NESTED"] = os.getenv(
            "CLAUDE_SANDBOX_WEAKER_NESTED", "true"
        )
        return env

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_network(self) -> None:
        """Auto-detect the orchestrator's Docker network.

        Uses the same network the orchestrator container is on so that
        sandbox containers inherit the same DNS, proxy, and internet
        access configuration.
        """
        if self._resolved_network:
            return

        # If user explicitly configured a network, use it
        if self._configured_network:
            self._resolved_network = self._configured_network
            logger.info("Using configured sandbox network: %s", self._resolved_network)
            return

        # Auto-detect from orchestrator container
        my_id = os.getenv("HOSTNAME", "")
        if my_id:
            try:
                result = await _run_cmd([
                    "docker", "inspect", "-f",
                    "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                    my_id,
                ])
                networks = result.strip().split()
                # Pick the first non-default network
                for net in networks:
                    if net not in ("bridge", "host", "none"):
                        self._resolved_network = net
                        logger.info(
                            "Auto-detected orchestrator network: %s", net
                        )
                        return
                if networks:
                    self._resolved_network = networks[0]
                    logger.info(
                        "Using orchestrator network: %s", self._resolved_network
                    )
                    return
            except Exception as e:
                logger.warning("Could not auto-detect network: %s", e)

        self._resolved_network = "bridge"
        logger.info("Falling back to default bridge network")

    async def _get_container_ip(self, container_name: str) -> str:
        result = await _run_cmd([
            "docker", "inspect", "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container_name,
        ])
        ip = result.strip()
        if not ip:
            raise RuntimeError(
                "Could not determine IP for container %s" % container_name
            )
        return ip

    async def _is_running(self, container_id: str) -> bool:
        try:
            result = await _run_cmd(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
                check=False,
            )
            return result.strip() == "true"
        except Exception:
            return False

    async def _wait_for_healthy(self, base_url: str, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        url = "%s/health" % base_url
        async with httpx.AsyncClient(trust_env=False) as client:
            while time.time() < deadline:
                try:
                    resp = await client.get(url, timeout=3)
                    if resp.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(2)
        raise TimeoutError(
            "Sandbox container at %s not healthy after %ds" % (base_url, timeout)
        )

    async def _evict_idle_unlocked(self) -> None:
        if not self.containers:
            return
        oldest_user = min(
            self.containers, key=lambda u: self.containers[u].last_accessed
        )
        container = self.containers.pop(oldest_user)
        await _run_cmd(
            ["docker", "rm", "-f", container.container_name], check=False
        )
        logger.info("Evicted idle sandbox for user=%s", oldest_user)


class SandboxProxy:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=10.0),
            trust_env=False,
        )

    async def forward_json(
        self, base_url: str, body: dict, api_key: Optional[str] = None
    ) -> dict:
        url = "%s/v1/responses" % base_url
        headers = self._headers(api_key)
        try:
            resp = await self._client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail="Sandbox error: %s" % resp.text[:500],
                )
            return resp.json()
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Sandbox container timed out")
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail="Sandbox container unavailable")

    async def forward_stream(
        self, base_url: str, body: dict, api_key: Optional[str] = None
    ) -> StreamingResponse:
        url = "%s/v1/responses" % base_url
        headers = self._headers(api_key)

        async def _generate() -> AsyncIterator[bytes]:
            try:
                async with self._client.stream(
                    "POST", url, json=body, headers=headers
                ) as resp:
                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        logger.error(
                            "Sandbox stream error %d: %s",
                            resp.status_code,
                            error_body.decode()[:500],
                        )
                        yield (
                            b'data: {"error": "Sandbox error '
                            + str(resp.status_code).encode()
                            + b'"}\n\n'
                        )
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            except httpx.TimeoutException:
                yield b'data: {"error": "Sandbox container timed out"}\n\n'
            except httpx.ConnectError:
                yield b'data: {"error": "Sandbox container unavailable"}\n\n'

        return StreamingResponse(_generate(), media_type="text/event-stream")

    @staticmethod
    def _headers(api_key: Optional[str]) -> dict:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer %s" % api_key
        return headers

    async def close(self) -> None:
        await self._client.aclose()


async def _run_cmd(cmd: list, *, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(
            "Command %s failed (rc=%d): %s"
            % (cmd[0], proc.returncode, stderr.decode().strip())
        )
    return stdout.decode()


sandbox_manager: Optional[DockerSandboxManager] = None
sandbox_proxy: Optional[SandboxProxy] = None


def init_sandbox() -> None:
    from src.constants import (
        DOCKER_SANDBOX_CPU_LIMIT,
        DOCKER_SANDBOX_IDLE_TIMEOUT,
        DOCKER_SANDBOX_IMAGE,
        DOCKER_SANDBOX_MAX_CONTAINERS,
        DOCKER_SANDBOX_MEMORY_LIMIT,
        DOCKER_SANDBOX_NETWORK,
        DOCKER_SANDBOX_WORKSPACE_BASE,
    )

    global sandbox_manager, sandbox_proxy
    sandbox_manager = DockerSandboxManager(
        image=DOCKER_SANDBOX_IMAGE,
        network=DOCKER_SANDBOX_NETWORK,
        cpu_limit=DOCKER_SANDBOX_CPU_LIMIT,
        memory_limit=DOCKER_SANDBOX_MEMORY_LIMIT,
        idle_timeout=DOCKER_SANDBOX_IDLE_TIMEOUT,
        max_containers=DOCKER_SANDBOX_MAX_CONTAINERS,
        workspace_base=DOCKER_SANDBOX_WORKSPACE_BASE,
    )
    sandbox_proxy = SandboxProxy()
    logger.info(
        "Docker sandbox initialised (image=%s, max=%d)",
        DOCKER_SANDBOX_IMAGE,
        DOCKER_SANDBOX_MAX_CONTAINERS,
    )


async def shutdown_sandbox() -> None:
    global sandbox_manager, sandbox_proxy
    if sandbox_manager:
        await sandbox_manager.shutdown()
    if sandbox_proxy:
        await sandbox_proxy.close()
