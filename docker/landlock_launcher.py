#!/usr/bin/env -S python3 -I
"""Isolation launcher for Claude CLI children (issue #241).

The gateway points ``cli_path`` at this fixed, root-owned stub instead of at the
real CLI. The SDK execs it as ``[launcher, *cli_args]``. What it does depends on
``CLAUDE_ISOLATION_MODE`` (passed through the environment):

* **landlock** — apply a Landlock ruleset to itself and ``execv`` the real CLI.
  Landlock is inherited across ``execve``, so the CLI, its Bash tool, a script
  the agent wrote and stdio MCP are all confined at ``open()``. No privilege.
* **uid** — hand the spawn to the resident broker: present the signed ticket
  (``GATEWAY_ISOLATION_TICKET``) and pass fds 0/1/2 over ``SCM_RIGHTS`` to the
  broker socket, then stay as the SDK's child, mirroring the CLI's exit status
  and relaying SIGTERM/SIGINT. The broker forks the CLI as the session's uid and
  applies the same Landlock ruleset after the switch. See ``isolation_broker.py``.

Both the written-script read path (F1) and — in uid mode — cross-user DAC/signal
isolation are closed structurally, not by prompt text. Fail-closed: a missing
real CLI, an unusable Landlock, or (uid mode) an unreachable broker aborts the
spawn with a non-zero exit; the gateway must never fall through to an unconfined
CLI once it has decided to confine.

stdlib only. The shebang starts the interpreter isolated (``-I``: no user
site-packages, no ``PYTHON*`` variables, no script or cwd directory on
``sys.path``), so nothing planted in the agent-reachable environment — a
user-site ``.pth``, a ``PYTHONPATH`` ``sitecustomize`` — runs before the ruleset
is built. The guard below is only a fallback for an explicit
``python3 landlock_launcher.py`` (dev, tests): it re-execs under ``-I`` before
importing anything else, but that first interpreter has already run its site
hooks, so production must exec the launcher by its path.
"""

import os
import sys

if __name__ == "__main__" and not sys.flags.isolated:
    os.execv(
        sys.executable,
        [sys.executable, "-I", os.path.realpath(__file__), *sys.argv[1:]],
    )

import array
import json
import signal
import socket
import struct

# Sibling under docker/ (fixed, root-owned); -I keeps this import off cwd/env.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _landlock_rules as ll  # noqa: E402


def _die(msg):
    sys.stderr.write("landlock_launcher: %s\n" % msg)
    raise SystemExit(70)


# --- real CLI + path lists ---------------------------------------------------


def _real_cli():
    real = os.environ.get("GATEWAY_REAL_CLI", "").strip()
    if not real:
        _die("GATEWAY_REAL_CLI is unset; refusing to spawn an unconfined CLI")
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        _die("GATEWAY_REAL_CLI=%r is not an executable file" % real)
    return real


def _cwd():
    # The kernel's cwd, never $PWD: the SDK sets PWD to the cwd it was handed,
    # which is relative when USER_WORKSPACES_DIR is, and a relative PWD granted
    # <ws>/<ws> instead of the workspace.
    try:
        return os.getcwd()
    except OSError as exc:
        _die("cannot resolve the working directory: %s" % exc)


def _parse_paths(var):
    """The JSON array of absolute paths in *var*; unset or empty is none.

    JSON, never a ``:``-joined list: a path may contain ``:`` (a marketplace
    cloned from ``https://git.internal:8443``), and splitting on it turned one
    grant into others — a relative fragment refused on every spawn, or
    ``.../x:/secret`` granting ``/secret`` (#244 review)."""
    raw = os.environ.get(var, "")
    if not raw.strip():
        return []
    try:
        paths = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        _die("%s is not a JSON array of paths (%s); refusing it" % (var, exc))
    if not isinstance(paths, list):
        _die("%s is not a JSON array of paths; refusing it" % var)
    for path in paths:
        if not isinstance(path, str) or not path or "\0" in path:
            _die("%s entry %r is not a path; refusing it" % (var, path))
        try:
            os.fsencode(path)
        except UnicodeError:
            _die("%s entry %r is not a path; refusing it" % (var, path))
        # A relative rule would resolve against the cwd, granting something
        # other than what the gateway meant; refuse instead of guessing.
        if not os.path.isabs(path):
            _die("%s entry %r is not an absolute path; refusing it" % (var, path))
    return paths


def _tmpdir_name(cwd):
    """The session tmp dir's entry in the workspace *cwd*: an operator
    ``CLAUDE_CODE_TMPDIR`` naming one entry directly in it (``tmp``,
    ``<cwd>/tmp``), else ``.cli-tmp``.

    One entry only: ``ll.session_tmpdir`` prepares it relative to the
    workspace fd without following a link, and every further component would
    be one more name the agent controls between a check and its use — a
    swapped ``a`` in ``a/b`` steered the old realpath-checked branch outside
    the workspace (#244 review). A fixed path can name one workspace at most,
    so a per-workspace tmp dir is a single relative entry anyway."""
    configured = os.environ.get("CLAUDE_CODE_TMPDIR", "").strip()
    if configured:
        path = os.path.abspath(configured)  # against the kernel cwd: lexical
        name = os.path.basename(path)
        if os.path.dirname(path) == cwd and name not in ("", ".", ".."):
            return name
    return ".cli-tmp"


def _session_tmpdir(cwd):
    """A workspace-local tmpdir, honored over the shared /tmp.

    Points TMPDIR/CLAUDE_CODE_TMPDIR at ``<cwd>/.cli-tmp`` (or the operator's
    single-entry name, :func:`_tmpdir_name`) so the CLI's temp writes stay
    inside the granted workspace, not the shared /tmp (a cross-session channel
    when sessions share a uid). The npm and uv caches and the git global config
    move beneath it too (``ll.session_tmp_env``). Whatever the agent left in
    its place is repaired without following a link (``ll.session_tmpdir``).
    Exported for the real CLI.
    """
    real_cwd = cwd  # os.getcwd(), just checked equal to the gateway's grant
    try:
        # "." is the kernel cwd just checked against the gateway's grant, so
        # the repair works on that very directory, not on a path lookup.
        dot = os.open(".", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            tmp = ll.session_tmpdir(real_cwd, _tmpdir_name(real_cwd), cwd_fd=dot)
        finally:
            os.close(dot)
    except OSError as exc:
        # The CLI cannot start without it; say why instead of its bare exit 1.
        _die("cannot create the session tmp dir in %r: %s" % (real_cwd, exc))
    os.environ.update(ll.session_tmp_env(tmp))
    return tmp


def _ro_rw(real_cli, cwd):
    """The path lists for a confinement at *cwd*: ``(ro, rw)`` of our own,
    opened following links, and ``(gateway_ro, gateway_rw)``, the gateway's
    resolved entries, opened following none (``ll.build_and_restrict``)."""
    cli_dir = os.path.dirname(os.path.realpath(real_cli))
    problem = ll.cli_dir_problem(cli_dir, cwd)
    if problem:
        _die("refusing to grant the real CLI's directory %r: %s" % (cli_dir, problem))
    ro = list(ll.SYSTEM_RO)
    ro.append(cli_dir)
    # The session shell wrapper, which the CLI execs for every Bash call: the
    # file, never its directory (in a checkout that is the gateway's docker/).
    ro.append(ll.SESSION_SHELL)
    gateway_ro = _parse_paths("GATEWAY_LANDLOCK_RO")
    gateway_rw = _parse_paths("GATEWAY_LANDLOCK_RW")
    # The workspace is granted as the gateway named it, never as the kernel's
    # cwd: the SDK's chdir follows links, so a workspace planted as a link
    # granted its target rw — the whole config dir, or every user's workspace
    # (#244 review). The gateway sends it resolved, as its first read-write
    # entry, so the kernel cwd must be exactly that entry.
    if not gateway_rw or cwd != gateway_rw[0]:
        _die(
            "the working directory %r is not the gateway's workspace grant (the "
            "first GATEWAY_LANDLOCK_RW entry); refusing to confine elsewhere" % cwd
        )
    # TMPDIR is set inside the workspace but is NOT a separate rule: it lies
    # beneath the granted cwd, so a rule would be redundant and (because it is
    # agent-controlled) a symlink-escape vector — see ll.session_tmpdir.
    _session_tmpdir(cwd)
    os.environ.update(ll.session_shell_env(os.environ))
    # /dev/shm stays rw for POSIX semaphores (multiprocessing, joblib). In this
    # mode it is a surface shared by every same-uid session. uid mode runs each
    # session as its own uid with umask 077, so new objects there are 0600 to
    # the other session uids (an agent can still chmod its own open), and the
    # broker's cleanup op removes what a run left before its uid is leased
    # again.
    rw = ["/dev/shm"]
    return ro, rw, gateway_ro, gateway_rw


# --- landlock mode: confine in place, then exec ------------------------------


def _run_landlock(real_cli):
    cwd = _cwd()
    ro, rw, gateway_ro, gateway_rw = _ro_rw(real_cli, cwd)
    try:
        ll.build_and_restrict(ro, rw, gateway_ro=gateway_ro, gateway_rw=gateway_rw)
    except ll.LandlockError as exc:
        _die(str(exc))
    ll.restore_exec_signals()
    os.execv(real_cli, [real_cli, *sys.argv[1:]])


# --- uid mode: hand the spawn (fds + ticket) to the broker -------------------

# Not sent in the header env: the ticket travels as its own field, and the
# broker strips all three before the CLI's env is built anyway.
_PLUMBING_ENV = (
    "GATEWAY_ISOLATION_TICKET",
    "GATEWAY_ISOLATION_TICKET_SECRET",
    "GATEWAY_ISOLATION_BROKER_SOCKET",
)

# Signals whose default action stops, continues or is ignored. Raising one on
# ourselves would not end the launcher the way the CLI ended (a stop leaves the
# SDK's wait() hanging), so those are reported as the shell's 128+N instead.
_NO_SELF_RAISE = frozenset(
    {
        signal.SIGTSTP,
        signal.SIGSTOP,
        signal.SIGTTIN,
        signal.SIGTTOU,
        signal.SIGCHLD,
        signal.SIGCONT,
        signal.SIGWINCH,
        signal.SIGURG,
    }
)


def _recv_exact(conn, size):
    """Read exactly *size* bytes, or ``None`` if the link closes first."""
    buf = b""
    while len(buf) < size:
        try:
            chunk = conn.recv(size - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _die_by_signal(sig):
    """End this process by *sig*, so the SDK sees the CLI's ``-sig``."""
    import resource

    try:
        # A self-raised SIGSEGV/SIGABRT must not drop a core into the workspace.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        pass
    if sig != signal.SIGKILL:  # SIGKILL's disposition cannot be set (EINVAL)
        try:
            signal.signal(sig, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {sig})
        except (OSError, ValueError):
            pass
    os.kill(os.getpid(), sig)


def _mirror(kind, value):
    """End the launcher exactly as the broker reports the CLI ended."""
    if kind == ll.ST_EXITED and 0 <= value <= 255:
        raise SystemExit(value)
    if kind == ll.ST_SIGNALED and 0 < value < signal.NSIG:
        if value not in _NO_SELF_RAISE:
            _die_by_signal(value)
        os._exit(128 + value)  # also the fallback if the signal did not kill us
    if kind == ll.ST_REFUSED:
        _die("the isolation broker refused the spawn; the CLI never started")
    _die("the isolation broker sent an invalid status (%d, %d)" % (kind, value))


def _run_uid():
    ticket = os.environ.get("GATEWAY_ISOLATION_TICKET", "").strip()
    sock_path = os.environ.get("GATEWAY_ISOLATION_BROKER_SOCKET", "").strip()
    if not ticket:
        _die("GATEWAY_ISOLATION_TICKET is unset; refusing an unconfined uid spawn")
    if not sock_path:
        _die("GATEWAY_ISOLATION_BROKER_SOCKET is unset; no broker to hand off to")

    # Pass our own environment too: it is the env the SDK built for this child
    # (ANTHROPIC_*, PATH, HOME, …), already scrubbed of gateway secrets by the
    # gateway's _configure_sdk_env. The broker uses it as the CLI's env (the
    # broker's own env would otherwise be inherited, losing the SDK's values),
    # filtered by the ticket's env allowlist.
    env = {k: v for k, v in os.environ.items() if k not in _PLUMBING_ENV}
    header = json.dumps({"ticket": ticket, "argv": sys.argv[1:], "env": env})
    header = header.encode("utf-8")
    if len(header) > ll.MAX_HEADER:
        _die(
            "spawn header is %d bytes, above the broker's %d-byte limit"
            % (len(header), ll.MAX_HEADER)
        )

    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(sock_path)
    except OSError as exc:
        _die("cannot reach the isolation broker at %s: %s" % (sock_path, exc))

    # The length-prefixed header, with fds 0/1/2 as SCM_RIGHTS ancillary data
    # on its first byte. The broker forks the CLI onto them.
    data = struct.pack("=I", len(header)) + header
    fds = array.array("i", [0, 1, 2])
    try:
        sent = conn.sendmsg(
            [data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds.tobytes())]
        )
        if sent < len(data):
            conn.sendall(data[sent:])
    except OSError as exc:
        _die("failed to hand the spawn to the broker: %s" % exc)

    # Stay as the SDK's child: wait for the broker's status record and mirror
    # it so the SDK's wait()/returncode track the real CLI. Relaying SIGTERM/
    # SIGINT keeps the SDK's terminate() working; if the SDK kills us instead,
    # the broker sees the link drop and tears the CLI tree down itself. The
    # relay is installed only after the header is out, so a record can never
    # interleave with it.
    def _relay(signum, _frame):
        try:
            conn.sendall(ll.REC.pack(ll.REC_RELAY, signum))
        except OSError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _relay)
        except (OSError, ValueError):
            pass

    record = _recv_exact(conn, ll.REC.size)
    if record is None:
        _die("the isolation broker closed the link without a status")
    _mirror(*ll.REC.unpack(record))


def main():
    mode = os.environ.get("CLAUDE_ISOLATION_MODE", "").strip().lower()
    if mode == "uid":
        _run_uid()
    else:
        # landlock (default when the gateway routed here); fail closed on the
        # real CLI first so a misconfig never execs unconfined.
        _run_landlock(_real_cli())


if __name__ == "__main__":
    main()
