"""Issue #248 spike: run the real SDK + bundled CLI inside gVisor sandbox containers.

Drives ``ClaudeSDKClient`` with ``cli_path`` = ``docker/sandbox/sandbox_launcher.py``
against ``tests/fixtures/fake_anthropic_api.py`` (no model cost, fixed replies)
for every (unit, runtime) in the matrix:

  unit     user    = one long-lived container per user, ``docker exec`` per CLI
           session = one ``docker run --rm`` container per CLI (#180's unit)
  runtime  runsc (gVisor) and runc (A/B baseline)

and reports:

  S1  a Bash turn as alice: SDK hooks + can_use_tool still run in the gateway
      process; what the sandbox sees (pid 1, bob's files, gateway env, DB,
      internet, metadata IP); a written file lands in alice's workspace
  S2  a stdio MCP server starts inside alice's sandbox
  S3  bob runs in his own container and cannot see alice's files
  S4  SDK disconnect mid-tool and a SIGKILLed stub leave nothing running
  S5  connect / first-turn latency, cold vs warm
  S6  alice's two concurrent sessions: cancelling one leaves the other working
  S7  fail closed: an unknown runtime refuses the spawn (stub exit 70), no
      fallback to runc
  B   workload benchmarks on the workspace bind mount (runc vs runsc)
  M   memory of N idle CLI sessions (runc vs runsc)

Needs a Docker daemon with the ``runsc`` runtime registered. Not part of the
test suite; run by hand:

    GATEWAY_SKIP_DOTENV=1 uv run python -u scripts/sandbox_spike.py
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "docker" / "sandbox"))

import sandbox_spawner  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
)

from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI  # noqa: E402

IMAGE = "omg-sandbox:spike"
NETWORK = "omg-sbx-net"
FORWARDER = "omg-sbx-gw"
LAUNCHER = REPO / "docker" / "sandbox" / "sandbox_launcher.py"
DECOY = "mysql://gateway:decoy-secret@gateway-log:3306/gateway_log"
MATRIX = [
    (u, r)
    for u in os.environ.get("SPIKE_UNITS", "user,session").split(",")
    for r in os.environ.get("SPIKE_RUNTIMES", "runsc,runc").split(",")
]
IDLE_SESSIONS = int(os.environ.get("SPIKE_IDLE_SESSIONS", "20"))
# "per-sandbox": every sandbox on its own internal network with the gateway
# attached; "shared": all sandboxes on NETWORK (they can reach each other).
NET_MODE = os.environ.get("SPIKE_NET", "per-sandbox")

MCP_SERVER = """\
import json, os, sys
open(sys.argv[1], "a").write("started uid=%d host=%s\\n" % (os.getuid(), os.uname()[1]))
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    result = {}
    if msg.get("method") == "initialize":
        result = {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "marker", "version": "0"},
        }
    elif msg.get("method") == "tools/list":
        result = {"tools": []}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
"""

BENCH = r"""
import os, subprocess, sys, time
d = sys.argv[1]
os.makedirs(d, exist_ok=True)
out = {}
t = time.perf_counter()
for i in range(2000):
    with open(os.path.join(d, "f%04d.txt" % i), "w") as f:
        f.write(("line %d needle\n" % i) * 64)
out["write_2000_files"] = time.perf_counter() - t
t = time.perf_counter()
n = 0
for name in os.listdir(d):
    with open(os.path.join(d, name)) as f:
        n += len(f.read())
out["read_2000_files"] = time.perf_counter() - t
t = time.perf_counter()
for root, dirs, files in os.walk(d):
    for name in files:
        os.stat(os.path.join(root, name))
out["stat_walk"] = time.perf_counter() - t
t = time.perf_counter()
subprocess.run(["grep", "-rl", "needle", d], stdout=subprocess.DEVNULL, check=True)
out["grep_r"] = time.perf_counter() - t
t = time.perf_counter()
for _ in range(200):
    subprocess.run(["/bin/true"], check=True)
out["spawn_200_procs"] = time.perf_counter() - t
t = time.perf_counter()
x = 0
for i in range(5_000_000):
    x += i * i
out["cpu_loop_5M"] = time.perf_counter() - t
t = time.perf_counter()
for name in os.listdir(d):
    os.unlink(os.path.join(d, name))
out["unlink_2000"] = time.perf_counter() - t
print(" ".join("%s=%.3f" % kv for kv in out.items()))
"""

report = {}


def note(section, key, value):
    report.setdefault(section, {})[key] = value
    print(f"  [{section}] {key}: {value}", flush=True)


def sh(*args, check=True, **kw):
    res = subprocess.run(args, capture_output=True, text=True, **kw)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {res.stderr.strip()}")
    return res


def scrub_runner_env():
    """Keep this runner's own Claude env (a Claude Code session, say) out of the
    CLI: the SDK builds the child env from ``os.environ`` first."""
    for key in list(os.environ):
        if key.startswith(("CLAUDE", "ANTHROPIC_")):
            del os.environ[key]


def bundled_cli():
    import claude_agent_sdk

    return str(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")


def setup_infra(port):
    """Build the image, the internal network and a stand-in for the gateway's
    leg on it; returns the address sandboxes must pin for ``gateway``."""
    sh("docker", "build", "-q", "-t", IMAGE, str(REPO / "docker" / "sandbox"))
    if sh("docker", "network", "inspect", NETWORK, check=False).returncode != 0:
        sh("docker", "network", "create", "--internal", NETWORK)
    sh("docker", "rm", "-f", FORWARDER, check=False)
    bridge_gw = sh(
        "docker",
        "network",
        "inspect",
        "bridge",
        "-f",
        "{{(index .IPAM.Config 0).Gateway}}",
    ).stdout.strip()
    # In production the gateway container itself joins the sandbox network;
    # here a forwarder relays "gateway:<port>" to this test process.
    sh(
        "docker",
        "run",
        "-d",
        "--name",
        FORWARDER,
        "--network",
        NETWORK,
        "alpine/socat",
        f"TCP-LISTEN:{port},fork,reuseaddr",
        f"TCP:{bridge_gw}:{port}",
    )
    sh("docker", "network", "connect", "bridge", FORWARDER)
    return sh(
        "docker",
        "inspect",
        "-f",
        '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % NETWORK,
        FORWARDER,
    ).stdout.strip()


def remove_sandboxes():
    ids = sh("docker", "ps", "-aq", "--filter", "label=omg.sandbox=1").stdout.split()
    if ids:
        sh("docker", "rm", "-f", *ids)
    nets = sh(
        "docker", "network", "ls", "-q", "--filter", "label=omg.sandbox=1"
    ).stdout.split()
    for net in nets:
        sh("docker", "network", "disconnect", "-f", net, FORWARDER, check=False)
        sh("docker", "network", "rm", net, check=False)


def make_user(root, user):
    ws = root / "ws" / user
    cfg = root / "cfg" / user
    for d in (ws, cfg):
        d.mkdir(parents=True, exist_ok=True)
        os.chown(d, 1000, 1000)
    return ws, cfg


class Spawner:
    """One in-process spawner per (unit, runtime), on its own socket."""

    def __init__(self, root, unit, runtime, gw_ip):
        self.sock = str(root / f"spawner-{unit}-{runtime}.sock")
        os.environ.update(
            SANDBOX_SPAWNER_SOCKET=self.sock,
            SANDBOX_IMAGE=IMAGE,
            SANDBOX_RUNTIME=runtime,
            SANDBOX_NETWORK=NETWORK,
            SANDBOX_CLI=bundled_cli(),
            SANDBOX_WORKSPACE_BASE=str(root / "ws"),
            SANDBOX_CONFIG_BASE=str(root / "cfg"),
            SANDBOX_UNIT=unit,
            SANDBOX_ADD_HOSTS=json.dumps({"gateway": gw_ip}),
            SANDBOX_NAME_PREFIX=f"omg-sbx-{runtime}",
            SANDBOX_GATEWAY_CONTAINER=FORWARDER if NET_MODE == "per-sandbox" else "",
        )
        self.cfg = sandbox_spawner.Config()
        ready = threading.Event()
        threading.Thread(
            target=sandbox_spawner.serve, args=(self.cfg, ready), daemon=True
        ).start()
        ready.wait(5)

    def container(self, user):
        return sandbox_spawner.container_name(self.cfg, user)


def options(api, user, ws, cfg, sock, **extra):
    env = {
        "OMG_SANDBOX_SPAWNER": sock,
        "OMG_SANDBOX_USER": user,
        "ANTHROPIC_BASE_URL": api.base_url,
        "ANTHROPIC_AUTH_TOKEN": "sk-fake-test",
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CONFIG_DIR": str(cfg),
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
        # A gateway-only secret: must not reach the sandbox.
        "USAGE_LOG_DB_URL": DECOY,
    }
    return ClaudeAgentOptions(
        cli_path=str(LAUNCHER), cwd=str(ws), env=env, setting_sources=[], **extra
    )


def tool_result_text(body):
    for msg in reversed(body.get("messages", [])):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):
                        return "".join(x.get("text", "") for x in c)
                    return c
    return None


def tool_plan(commands):
    """Reply with a Bash call keyed off the prompt; then 'done' after its result.

    ``commands`` maps a prompt word to the command, so concurrent sessions on
    one fake API each get their own."""
    results = {}

    def plan(body):
        msgs = body.get("messages", [])
        first = msgs[0].get("content") if msgs else ""
        if isinstance(first, list):
            first = " ".join(b.get("text", "") for b in first if isinstance(b, dict))
        key = next((k for k in commands if k in (first or "")), None)
        text = tool_result_text(body)
        if text is not None:
            results[key] = text
            return {"text": "done"}
        if key is None:
            return {"text": "ok"}
        return {"tool_use": {"name": "Bash", "input": {"command": commands[key]}}}

    plan.results = results
    return plan


async def run_turn(opts, prompt="go"):
    msgs = []
    client = ClaudeSDKClient(options=opts)
    t0 = time.monotonic()
    await client.connect()
    t_conn = time.monotonic() - t0
    t1 = time.monotonic()
    await client.query(prompt)
    async for m in client.receive_response():
        msgs.append(m)
    t_turn = time.monotonic() - t1
    proc = client._transport._process
    await client.disconnect()
    return msgs, t_conn, t_turn, proc.returncode


def result_of(msgs):
    r = next((m for m in msgs if isinstance(m, ResultMessage)), None)
    return getattr(r, "result", None)


def tagged_count(container):
    out = sh(
        "docker",
        "exec",
        container,
        "sh",
        "-c",
        "grep -lsz '^OMG_EXEC_ID=' /proc/[0-9]*/environ | wc -l",
        check=False,
    )
    return out.stdout.strip() or out.stderr.strip()


def live_sandboxes(prefix):
    return [
        n
        for n in sh(
            "docker", "ps", "--filter", "label=omg.sandbox=1", "--format", "{{.Names}}"
        ).stdout.split()
        if n.startswith(prefix)
    ]


async def scenarios(api, root, users, unit, runtime, gw_ip):
    sec = f"{unit}/{runtime}/{NET_MODE}"
    print(f"== {sec}", flush=True)
    sp = Spawner(root, unit, runtime, gw_ip)
    (alice_ws, alice_cfg), (bob_ws, bob_cfg) = users["alice"], users["bob"]
    prefix = f"omg-sbx-{runtime}-"

    # S1 --------------------------------------------------------------------
    probe = (
        "echo uid=$(id -u) host=$(hostname) kernel=$(uname -r); "
        "echo pid1=$(tr '\\0' ' ' </proc/1/cmdline); "
        "echo procs=$(ls -d /proc/[0-9]* | wc -l); "
        f"echo bob=$(ls {bob_ws} 2>&1 | head -1); "
        f"echo base=$(ls {root / 'ws'} 2>&1 | tr '\\n' ' '); "
        "echo decoy_env=$(env | grep -c USAGE_LOG_DB_URL); "
        "echo token_env=$(env | grep -c ANTHROPIC_AUTH_TOKEN); "
        "echo app=$(ls /app 2>&1 | head -1); "
        "echo docker_sock=$(ls /var/run/docker.sock 2>&1 | head -1); "
        'py() { python3 -c "import socket,sys;s=socket.socket();s.settimeout(3);'
        's.connect((sys.argv[1],int(sys.argv[2])));print(\'OPEN\')" "$@" 2>&1 | tail -1; }; '
        "echo internet=$(py 1.1.1.1 80); "
        "echo db=$(py gateway-log 3306); "
        "echo metadata=$(py 169.254.169.254 80); "
        f"echo gateway_api=$(py gateway {api.port}); "
        "echo written > out.txt"
    )
    plan = tool_plan({"probe": probe})
    api.plan = plan
    hooks, asks = [], []

    async def pre_hook(inp, tool_use_id, ctx):
        hooks.append(inp.get("tool_name"))
        return {}

    async def can_use(tool, inp, ctx):
        asks.append(tool)
        return PermissionResultAllow()

    (alice_ws / "out.txt").unlink(missing_ok=True)
    opts = options(
        api,
        "alice",
        alice_ws,
        alice_cfg,
        sp.sock,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_hook])]},
        can_use_tool=can_use,
    )
    msgs, t_conn, t_turn, rc = await run_turn(opts, "probe")
    note(sec, "S1 result", result_of(msgs))
    note(sec, "S1 PreToolUse hook ran in gateway", hooks)
    note(sec, "S1 can_use_tool ran in gateway", asks)
    note(sec, "S1 CLI exit code via stub", rc)
    note(sec, "S1 sandbox view", (plan.results.get("probe") or "").splitlines())
    out = alice_ws / "out.txt"
    note(
        sec,
        "S1 out.txt on host",
        (
            f"{out.read_text().strip()} uid={out.stat().st_uid}"
            if out.exists()
            else "missing"
        ),
    )
    note(sec, "S5 cold connect s", round(t_conn, 2))
    note(sec, "S5 cold first turn s", round(t_turn, 2))
    if unit == "user":
        mem = sh(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.MemUsage}}",
            sp.container("alice"),
        )
        note(sec, "S5 alice container mem after turn (CLI exited)", mem.stdout.strip())

    # S5 warm -----------------------------------------------------------------
    api.plan = tool_plan({"warm": "echo warm"})
    conns, turns = [], []
    for _ in range(3):
        _, c, t, _ = await run_turn(
            options(api, "alice", alice_ws, alice_cfg, sp.sock), "warm"
        )
        conns.append(round(c, 2))
        turns.append(round(t, 2))
    note(sec, "S5 warm connect s (3 runs)", conns)
    note(sec, "S5 warm first turn s (3 runs)", turns)

    # S2 ----------------------------------------------------------------------
    (alice_ws / "mcp_marker.py").write_text(MCP_SERVER)
    marker = alice_ws / "mcp.log"
    marker.unlink(missing_ok=True)
    api.plan = lambda body: {"text": "ok"}
    opts = options(
        api,
        "alice",
        alice_ws,
        alice_cfg,
        sp.sock,
        mcp_servers={
            "marker": {
                "type": "stdio",
                "command": "python3",
                "args": [str(alice_ws / "mcp_marker.py"), str(marker)],
            }
        },
    )
    msgs, *_ = await run_turn(opts)
    init = next(
        (m for m in msgs if isinstance(m, SystemMessage) and m.subtype == "init"), None
    )
    note(sec, "S2 mcp status", init.data.get("mcp_servers") if init else None)
    note(sec, "S2 marker", marker.read_text().strip() if marker.exists() else "missing")

    # S3 ----------------------------------------------------------------------
    plan = tool_plan({"bob": f"ls {alice_ws} 2>&1 | head -1; ls; cat bob-secret.txt"})
    api.plan = plan
    await run_turn(
        options(
            api, "bob", bob_ws, bob_cfg, sp.sock, permission_mode="bypassPermissions"
        ),
        "bob",
    )
    note(sec, "S3 bob sandbox view", (plan.results.get("bob") or "").splitlines())
    if unit == "user":
        note(sec, "S3 per-user containers", live_sandboxes(prefix))
        # Network between sandboxes: a listener in bob's container, dialled
        # from alice's (both sit on the one internal network).
        bob_c, alice_c = sp.container("bob"), sp.container("alice")
        bob_ip = sh(
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            bob_c,
        ).stdout.strip()
        sh("docker", "exec", "-d", bob_c, "python3", "-m", "http.server", "8765")
        await asyncio.sleep(2)
        dial = sh(
            "docker",
            "exec",
            alice_c,
            "python3",
            "-c",
            "import socket;s=socket.socket();s.settimeout(3);"
            f"s.connect(('{bob_ip}',8765));print('OPEN')",
            check=False,
        )
        note(
            sec,
            "S3 alice -> bob container tcp",
            (dial.stdout.strip() or dial.stderr.strip().splitlines()[-1]),
        )

    # S4 ----------------------------------------------------------------------
    api.plan = tool_plan({"sleep": "sleep 600 & sleep 600"})
    started = asyncio.Event()

    async def mark(inp, tool_use_id, ctx):
        started.set()
        return {}

    def s4_opts():
        return options(
            api,
            "alice",
            alice_ws,
            alice_cfg,
            sp.sock,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[mark])]},
            permission_mode="bypassPermissions",
        )

    def leftovers():
        if unit == "user":
            return "tagged procs=" + tagged_count(sp.container("alice"))
        return "session containers=%d" % len(
            [n for n in live_sandboxes(prefix) if "-s-" in n]
        )

    client = ClaudeSDKClient(options=s4_opts())
    await client.connect()
    await client.query("sleep")
    await asyncio.wait_for(started.wait(), 60)
    await asyncio.sleep(2)
    note(sec, "S4 while sleeping", leftovers())
    t0 = time.monotonic()
    await client.disconnect()
    note(sec, "S4 SDK disconnect s", round(time.monotonic() - t0, 2))
    await asyncio.sleep(1)
    note(sec, "S4 after disconnect", leftovers())

    started.clear()
    client = ClaudeSDKClient(options=s4_opts())
    await client.connect()
    await client.query("sleep")
    await asyncio.wait_for(started.wait(), 60)
    await asyncio.sleep(2)
    os.kill(client._transport._process.pid, signal.SIGKILL)
    await asyncio.sleep(sandbox_spawner.TERM_GRACE_S + 3)
    note(sec, "S4 after stub SIGKILL", leftovers())
    try:
        await client.disconnect()
    except Exception as exc:  # noqa: BLE001
        note(sec, "S4 disconnect after SIGKILL raised", type(exc).__name__)

    # S6 ----------------------------------------------------------------------
    plan = tool_plan({"one": "sleep 600", "two": "sleep 4; echo second-ok"})
    api.plan = plan
    started.clear()
    c1 = ClaudeSDKClient(options=s4_opts())
    await c1.connect()
    await c1.query("one")
    await asyncio.wait_for(started.wait(), 60)
    c2 = ClaudeSDKClient(
        options=options(
            api,
            "alice",
            alice_ws,
            alice_cfg,
            sp.sock,
            permission_mode="bypassPermissions",
        )
    )
    await c2.connect()
    await c2.query("two")
    await asyncio.sleep(1)
    await c1.disconnect()  # cancel session one mid-tool
    msgs = [m async for m in c2.receive_response()]
    note(
        sec,
        "S6 session two after cancelling one",
        {"result": result_of(msgs), "tool": plan.results.get("two")},
    )
    await c2.disconnect()

    # S7 ----------------------------------------------------------------------
    if runtime == "runsc" and unit == "user":
        bad = Spawner(root, "session", "no-such-runtime", gw_ip)
        client = ClaudeSDKClient(
            options=options(api, "carol", *users["carol"], bad.sock)
        )
        try:
            await client.connect()
            await client.query("hi")
            msgs = [m async for m in client.receive_response()]
            note(sec, "S7 unknown runtime", f"NOT REFUSED: {result_of(msgs)}")
        except Exception as exc:  # noqa: BLE001
            note(
                sec,
                "S7 unknown runtime",
                f"refused ({type(exc).__name__}): " f"{str(exc).splitlines()[0][:160]}",
            )
        proc = client._transport._process if client._transport else None
        note(sec, "S7 stub exit code", proc.returncode if proc else None)
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        note(
            sec,
            "S7 containers for carol",
            [
                n
                for n in sh(
                    "docker", "ps", "-a", "--format", "{{.Names}}"
                ).stdout.split()
                if "no-such-runtime" in n
            ],
        )

    note(
        sec,
        "runtime inspected",
        sh(
            "docker",
            "inspect",
            "-f",
            "{{.HostConfig.Runtime}}",
            sp.container("alice"),
            check=False,
        ).stdout.strip()
        or "(session containers removed)",
    )
    remove_sandboxes()


def benchmarks(root):
    ws = root / "ws" / "bench"
    ws.mkdir(parents=True, exist_ok=True)
    os.chown(ws, 1000, 1000)
    (ws / "bench.py").write_text(BENCH)
    for runtime in ("runc", "runsc"):
        runs = []
        for _ in range(3):
            res = sh(
                "docker",
                "run",
                "--rm",
                "--runtime",
                runtime,
                "--network",
                "none",
                "-u",
                "1000:1000",
                "-v",
                f"{ws}:{ws}",
                IMAGE,
                "python3",
                str(ws / "bench.py"),
                str(ws / "data"),
            )
            runs.append(dict(kv.split("=") for kv in res.stdout.split()))
        med = {k: sorted(float(r[k]) for r in runs)[1] for k in runs[0]}
        note("B workload (median of 3, s)", runtime, med)
        t = []
        for _ in range(5):
            t0 = time.monotonic()
            sh(
                "docker",
                "run",
                "--rm",
                "--runtime",
                runtime,
                "--network",
                "none",
                IMAGE,
                "true",
            )
            t.append(time.monotonic() - t0)
        note("B docker run --rm true (s)", runtime, sorted(round(x, 2) for x in t))


async def idle_memory(api, root, gw_ip):
    for runtime in ("runc", "runsc"):
        sp = Spawner(root, "user", runtime, gw_ip)
        clients = []
        api.plan = lambda body: {"text": "ok"}
        free0 = int(sh("sh", "-c", "free -m | awk '/Mem:/{print $3}'").stdout)
        for i in range(IDLE_SESSIONS):
            ws, cfg = make_user(root, f"idle{i}")
            c = ClaudeSDKClient(options=options(api, f"idle{i}", ws, cfg, sp.sock))
            await c.connect()
            await c.query("hi")
            [m async for m in c.receive_response()]
            clients.append(c)
        await asyncio.sleep(3)
        free1 = int(sh("sh", "-c", "free -m | awk '/Mem:/{print $3}'").stdout)
        stats = sh(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.MemUsage}}",
            *[sp.container(f"idle{i}") for i in range(IDLE_SESSIONS)],
        ).stdout.split("\n")
        per = [s.split("/")[0].strip() for s in stats if s.strip()]
        note(
            f"M {IDLE_SESSIONS} idle CLI sessions",
            runtime,
            {
                "host used MiB delta": free1 - free0,
                "per-container (docker stats)": per[:5] + ["..."],
            },
        )
        for c in clients:
            await c.disconnect()
        remove_sandboxes()


async def main():
    scrub_runner_env()
    root = Path(tempfile.mkdtemp(prefix="omg-sbx-", dir=os.environ.get("SPIKE_ROOT")))
    os.chmod(root, 0o755)
    users = {u: make_user(root, u) for u in ("alice", "bob", "carol")}
    (users["bob"][0] / "bob-secret.txt").write_text("bob's private notes\n")

    with FakeAnthropicAPI(host="0.0.0.0") as api:
        print("== setup", flush=True)
        gw_ip = setup_infra(api.port)
        api._advertise = "http://gateway:{port}"
        remove_sandboxes()
        note("env", "runsc", sh("runsc", "--version").stdout.splitlines()[0])
        rt = json.loads(sh("docker", "info", "-f", "{{json .Runtimes}}").stdout)
        note(
            "env",
            "runsc args (none = default platform systrap)",
            rt.get("runsc", {}).get("runtimeArgs"),
        )
        mem_kb = int(Path("/proc/meminfo").read_text().split()[1])
        note(
            "env",
            "kernel / cpus / mem GiB",
            f"{os.uname().release} / {os.cpu_count()} / {mem_kb // (1 << 20)}",
        )
        for unit, runtime in MATRIX:
            await scenarios(api, root, users, unit, runtime, gw_ip)
        if os.environ.get("SPIKE_BENCH", "1") == "1":
            print("== benchmarks", flush=True)
            benchmarks(root)
        if IDLE_SESSIONS:
            print("== idle memory", flush=True)
            await idle_memory(api, root, gw_ip)

    remove_sandboxes()
    sh("docker", "rm", "-f", FORWARDER, check=False)
    shutil.rmtree(root, ignore_errors=True)
    out = Path(
        os.environ.get("SPIKE_REPORT", root.parent / "sandbox_spike_report.json")
    )
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    print(f"\n== report written to {out}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
