#!/usr/bin/env -S python3 -I
"""Per-user sandbox spawner sidecar (issue #248 spike).

The only process that talks to the Docker daemon. The gateway never gets the
Docker socket (holding it is host root); it gets this spawner's unix socket,
whose one operation is "run the Claude CLI for user U, on these stdio fds".

Per spawn:

1. The ``cli_path`` stub (``sandbox_launcher.py``) connects, sends a JSON
   header ``{user, argv, env, cwd}`` and its fds 0/1/2 over ``SCM_RIGHTS`` —
   the same hand-off the uid broker uses, so the SDK's pipes end up attached
   to a process the spawner owns.
2. The spawner makes sure user U's long-lived container is running
   (``docker run --runtime=runsc`` on first use: no capabilities, read-only
   root, internal network, cgroup limits, and only U's workspace + config dir
   mounted read-write).
3. It runs ``docker exec -i <container> sandbox-exec run <id> <cli> <argv>``
   with the received fds as stdio. The env reaching the CLI is the stub's
   environ filtered through an allowlist of names; values travel in the
   ``docker exec`` client's environment (``-e NAME``), never on its argv.
4. Relayed signals and a vanished stub become ``sandbox-exec kill`` inside the
   container; the exit status goes back as fixed ``=Ii`` records.

Wire format (mirrors ``isolation_broker.py``): ``=I`` length + JSON header,
fds on its first byte; then ``=Ii`` ``(kind, value)`` records —
stub→spawner ``(1, signum)``; spawner→stub ``(1, code)`` EXITED,
``(2, signum)`` SIGNALED, ``(3, 0)`` REFUSED.

Spike scope: no ticket (any process that can reach the socket may name any
user — in the target layout only the gateway container can), no idle reaper,
no resource accounting. stdlib only.
"""

import array
import hashlib
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

log = logging.getLogger("sandbox_spawner")

HDR = struct.Struct("=I")
REC = struct.Struct("=Ii")
REC_SIGNAL = 1
EXITED, SIGNALED, REFUSED = 1, 2, 3
MAX_HEADER = 8 << 20
TERM_GRACE_S = 5.0
_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,126}$")

# Env names that may reach the sandboxed CLI. Everything else in the stub's
# environ (gateway secrets, DB URLs, admin keys) is dropped.
ENV_ALLOW_EXACT = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",  # spike: still handed over (see #248 step 2)
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDECODE",
    "LANG",
    "TZ",
    "TRACEPARENT",
    "TRACESTATE",
    "NO_PROXY",
    "no_proxy",
}
ENV_ALLOW_PREFIX = ("CLAUDE_", "ANTHROPIC_DEFAULT_", "DISABLE_", "MCP_", "LC_")
ENV_DROP = {"CLAUDE_CODE_OAUTH_TOKEN"}
SANDBOX_HOME = "/home/agent"
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"


def _env_list(name):
    raw = os.environ.get(name, "").strip()
    return json.loads(raw) if raw else []


class Config:
    def __init__(self):
        self.socket = os.environ["SANDBOX_SPAWNER_SOCKET"]
        self.image = os.environ.get("SANDBOX_IMAGE", "omg-sandbox:spike")
        self.runtime = os.environ.get("SANDBOX_RUNTIME", "runsc")
        self.network = os.environ["SANDBOX_NETWORK"]
        self.cli = os.path.realpath(os.environ["SANDBOX_CLI"])
        self.workspace_base = os.path.realpath(os.environ["SANDBOX_WORKSPACE_BASE"])
        self.config_base = os.path.realpath(os.environ["SANDBOX_CONFIG_BASE"])
        self.ro_mounts = [os.path.realpath(p) for p in _env_list("SANDBOX_RO_MOUNTS")]
        self.memory = os.environ.get("SANDBOX_MEMORY", "2g")
        self.cpus = os.environ.get("SANDBOX_CPUS", "2")
        self.pids = os.environ.get("SANDBOX_PIDS", "512")
        self.prefix = os.environ.get("SANDBOX_NAME_PREFIX", "omg-sbx")
        # "user": one long-lived container per user, one `docker exec` per CLI.
        # "session": one `docker run --rm` container per CLI (#180's unit).
        self.unit = os.environ.get("SANDBOX_UNIT", "user")
        if self.unit not in ("user", "session"):
            raise ValueError("SANDBOX_UNIT must be user or session")
        # name -> IP pinned into /etc/hosts. Docker's embedded DNS (127.0.0.11)
        # is an iptables redirect inside the container's netns, which gVisor's
        # own netstack never sees, so names on the sandbox network do not
        # resolve under runsc; the gateway's address has to be static.
        hosts = os.environ.get("SANDBOX_ADD_HOSTS", "").strip()
        self.add_hosts = json.loads(hosts) if hosts else {}
        # Set = one internal network per sandbox, with this (gateway) container
        # attached to each and pinned as "gateway". On a shared network the
        # sandboxes reach each other; icc=false would cut the gateway off too.
        self.gateway_container = os.environ.get("SANDBOX_GATEWAY_CONTAINER", "").strip()


def container_name(cfg, user):
    slug = re.sub(r"[^a-z0-9]+", "-", user.lower()).strip("-")[:40] or "u"
    digest = hashlib.sha256(user.encode()).hexdigest()[:10]
    return "%s-%s-%s" % (cfg.prefix, slug, digest)


def user_dirs(cfg, user):
    return (
        os.path.join(cfg.workspace_base, user),
        os.path.join(cfg.config_base, user),
    )


def filter_env(env, cwd, user_cfg):
    out = {}
    for name, value in env.items():
        if name in ENV_DROP or name.startswith("OMG_SANDBOX_"):
            continue
        if name in ENV_ALLOW_EXACT or name.startswith(ENV_ALLOW_PREFIX):
            out[name] = value
    out.update(
        HOME=SANDBOX_HOME,
        PATH=SANDBOX_PATH,
        PWD=cwd,
        CLAUDE_CONFIG_DIR=user_cfg,
    )
    return out


def _docker(*args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kw)


class Spawner:
    def __init__(self, cfg):
        self.cfg = cfg
        self._locks = {}
        self._locks_guard = threading.Lock()
        self.timings = {}
        self.disabled = None

    def _user_lock(self, user):
        with self._locks_guard:
            return self._locks.setdefault(user, threading.Lock())

    def ensure_container(self, user):
        """Start (or create) user's container; returns (name, seconds, how)."""
        name = container_name(self.cfg, user)
        with self._user_lock(user):
            t0 = time.monotonic()
            state = _docker("inspect", "-f", "{{.State.Running}}", name)
            if state.returncode == 0 and state.stdout.strip() == "true":
                return name, time.monotonic() - t0, "warm"
            if state.returncode == 0:
                res = _docker("start", name)
                how = "restart"
            else:
                res = _docker(*self._run_args(name, user))
                how = "create"
            if res.returncode != 0:
                raise RuntimeError("docker %s failed: %s" % (how, res.stderr.strip()))
            return name, time.monotonic() - t0, how

    def _run_args(self, name, user):
        return ["run", "-d", *self._common_args(name, user), self.cfg.image]

    def _own_network(self, name):
        """Create *name*'s private internal network, attach the gateway to it;
        returns (network, gateway IP on it)."""
        net = name + "-net"
        if _docker("network", "inspect", net).returncode != 0:
            res = _docker(
                "network", "create", "--internal", "--label", "omg.sandbox=1", net
            )
            if res.returncode != 0:
                raise RuntimeError("network create failed: %s" % res.stderr.strip())
        res = _docker("network", "connect", net, self.cfg.gateway_container)
        if res.returncode != 0 and "already exists" not in res.stderr:
            raise RuntimeError("gateway attach failed: %s" % res.stderr.strip())
        ip = _docker(
            "inspect",
            "-f",
            '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % net,
            self.cfg.gateway_container,
        ).stdout.strip()
        if not ip:
            raise RuntimeError("gateway has no address on %s" % net)
        return net, ip

    def drop_network(self, name):
        net = name + "-net"
        if self.cfg.gateway_container:
            _docker("network", "disconnect", "-f", net, self.cfg.gateway_container)
            _docker("network", "rm", net)

    def _common_args(self, name, user):
        """The server-owned container template: callers pick only the user."""
        cfg = self.cfg
        ws, ucfg = user_dirs(cfg, user)
        for path in (ws, ucfg):
            if not os.path.isdir(path):
                raise RuntimeError("missing per-user dir %s" % path)
        network, hosts = cfg.network, dict(cfg.add_hosts)
        if cfg.gateway_container:
            network, hosts["gateway"] = self._own_network(name)
        args = [
            "--name",
            name,
            "--label",
            "omg.sandbox=1",
            "--label",
            "omg.sandbox.user=%s" % user,
            "--runtime",
            cfg.runtime,
            "--network",
            network,
            "--init",
            "--hostname",
            "sandbox",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m",
            "--tmpfs",
            "%s:rw,size=256m,uid=1000,gid=1000" % SANDBOX_HOME,
            "--memory",
            cfg.memory,
            "--cpus",
            cfg.cpus,
            "--pids-limit",
            cfg.pids,
            "-v",
            "%s:%s:rw" % (ws, ws),
            "-v",
            "%s:%s:rw" % (ucfg, ucfg),
            "-v",
            "%s:%s:ro" % (cfg.cli, cfg.cli),
        ]
        for path in cfg.ro_mounts:
            args += ["-v", "%s:%s:ro" % (path, path)]
        for host, ip in sorted(hosts.items()):
            args += ["--add-host", "%s:%s" % (host, ip)]
        return args

    def _stop_session(self, name, sig):
        if sig == "KILL":
            return self._remove(name)
        return _docker("kill", "-s", sig, name).returncode

    def _remove(self, name):
        rc = _docker("rm", "-f", name).returncode
        if self.cfg.unit == "session":
            self.drop_network(name)
        return rc

    def kill_tagged(self, name, exec_id, sig):
        res = _docker(
            "exec", "-u", "1000:1000", name, "sandbox-exec", "kill", exec_id, sig
        )
        return res.stdout.strip()

    # --- one spawn ---------------------------------------------------------

    def handle(self, conn):
        try:
            header, fds = _recv_header(conn)
        except Exception as exc:  # noqa: BLE001 - refuse and keep serving
            log.warning("refused: bad header: %s", exc)
            _send(conn, REFUSED, 0)
            conn.close()
            return
        try:
            self._spawn(conn, header, fds)
        except Exception as exc:  # noqa: BLE001
            log.warning("refused: %s", exc)
            _send(conn, REFUSED, 0)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            conn.close()

    def _spawn(self, conn, header, fds):
        if self.disabled:
            raise RuntimeError(self.disabled)
        user, argv, env, cwd = _validate(header)
        ws, ucfg = user_dirs(self.cfg, user)
        real_cwd = os.path.realpath(cwd)
        if real_cwd != ws and not real_cwd.startswith(ws + os.sep):
            raise RuntimeError("cwd %r is not inside %s's workspace" % (cwd, user))
        exec_id = uuid.uuid4().hex
        child_env = filter_env(env, real_cwd, ucfg)
        env_flags = []
        for key in sorted(child_env):
            env_flags += ["-e", key]
        inner = ["sandbox-exec", "run", exec_id, self.cfg.cli, *argv]
        if self.cfg.unit == "user":
            name, secs, how = self.ensure_container(user)
            cmd = [
                "docker",
                "exec",
                "-i",
                "-u",
                "1000:1000",
                "-w",
                real_cwd,
                *env_flags,
                name,
                *inner,
            ]
            stop = lambda sig: self.kill_tagged(name, exec_id, sig)  # noqa: E731
        else:
            name = "%s-s-%s" % (self.cfg.prefix, exec_id[:16])
            secs, how = 0.0, "session"
            cmd = [
                "docker",
                "run",
                "-i",
                "--rm",
                *self._common_args(name, user),
                "-u",
                "1000:1000",
                "-w",
                real_cwd,
                *env_flags,
                self.cfg.image,
                *inner,
            ]
            stop = lambda sig: self._stop_session(name, sig)  # noqa: E731
        docker_env = dict(os.environ)
        docker_env.update(child_env)
        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdin=fds[0],
            stdout=fds[1],
            stderr=fds[2],
            env=docker_env,
            close_fds=True,
        )
        for fd in fds:
            os.close(fd)
        fds.clear()
        log.info(
            "spawn user=%s container=%s (%s %.2fs) exec=%s",
            user,
            name,
            how,
            secs,
            exec_id,
        )
        self.timings[exec_id] = {
            "container": how,
            "container_s": secs,
            "exec_started": t0,
        }

        stopping = {"sig": None}

        def watch_stub():
            # Relay records from the stub; EOF before the CLI exits = stub died.
            while proc.poll() is None:
                try:
                    data = _recv_exact(conn, REC.size)
                except OSError:
                    data = b""
                if not data:
                    break
                kind, value = REC.unpack(data)
                if kind == REC_SIGNAL and value in (signal.SIGTERM, signal.SIGINT):
                    stopping["sig"] = value
                    stop(signal.Signals(value).name[3:])
            if proc.poll() is None:
                stopping["sig"] = stopping["sig"] or signal.SIGTERM
                stop("TERM")
                try:
                    proc.wait(TERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    stop("KILL")

        watcher = threading.Thread(target=watch_stub, daemon=True)
        watcher.start()
        rc = proc.wait()
        # Background jobs the CLI started (Bash `cmd &`) outlive it; sweep them
        # (session unit: the whole container goes).
        left = stop("KILL") if self.cfg.unit == "user" else self._remove(name)
        log.info("exit exec=%s rc=%s swept=%s", exec_id, rc, left)
        if stopping["sig"]:
            _send(conn, SIGNALED, int(stopping["sig"]))
        else:
            _send(conn, EXITED, rc if rc >= 0 else 128 - rc)


def _validate(header):
    if not isinstance(header, dict):
        raise ValueError("header is not an object")
    user, argv, env, cwd = (header.get(k) for k in ("user", "argv", "env", "cwd"))
    if not isinstance(user, str) or not _USER_RE.match(user) or ".." in user:
        raise ValueError("bad user %r" % (user,))
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError("bad argv")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ValueError("bad env")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError("bad cwd")
    return user, argv, env, cwd


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def _recv_header(conn):
    fds = array.array("i")
    msg, anc, _flags, _addr = conn.recvmsg(
        HDR.size, socket.CMSG_SPACE(3 * fds.itemsize)
    )
    for level, kind, data in anc:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(data[: len(data) - (len(data) % fds.itemsize)])
    fds = list(fds)
    if len(fds) != 3:
        for fd in fds:
            os.close(fd)
        raise ValueError("expected 3 fds, got %d" % len(fds))
    if len(msg) < HDR.size:
        msg += _recv_exact(conn, HDR.size - len(msg))
    (length,) = HDR.unpack(msg)
    if not 0 < length <= MAX_HEADER:
        raise ValueError("header length %d" % length)
    return json.loads(_recv_exact(conn, length)), fds


def _send(conn, kind, value):
    try:
        conn.sendall(REC.pack(kind, value))
    except OSError:
        pass


def runtime_problem(runtime):
    """Why the daemon cannot run *runtime*, or None. Checked once at startup:
    a spawner whose runtime is missing refuses every spawn (fail closed) — it
    never lets Docker fall back to its default runtime."""
    res = _docker("info", "-f", "{{json .Runtimes}}")
    if res.returncode != 0:
        return "docker info failed: %s" % res.stderr.strip()
    try:
        runtimes = json.loads(res.stdout)
    except ValueError:
        return "docker info returned no runtime list"
    if runtime not in runtimes:
        return "runtime %r is not registered (have: %s)" % (
            runtime,
            ", ".join(sorted(runtimes)),
        )
    return None


def serve(cfg, ready=None):
    spawner = Spawner(cfg)
    spawner.disabled = runtime_problem(cfg.runtime)
    if spawner.disabled:
        log.error("refusing every spawn: %s", spawner.disabled)
    try:
        os.unlink(cfg.socket)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(cfg.socket)
    os.chmod(cfg.socket, 0o660)
    srv.listen(64)
    log.info("listening on %s", cfg.socket)
    if ready is not None:
        ready.set()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=spawner.handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr
    )
    serve(Config())
