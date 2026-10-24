"""Keep a confined session's secrets off its command line (#241 §6.5).

Landlock leaves ``/proc`` readable (the bundled CLI aborts without it), and
``/proc/<pid>/cmdline`` is world-readable whatever the uid, so in both
isolation modes any session could read every other CLI's argv: the SDK passes
the MCP config (``MCP_FORWARD_CONTEXT`` ``{{header:…}}`` values included) as
``--mcp-config <json>`` and the system prompt (``Current user:``) as
``--system-prompt`` / ``--append-system-prompt``. With confinement on,
:func:`externalize` moves both into per-spawn files in the session's own
workspace — the one directory the ruleset grants that session and no other —
and leaves only their names on the command line:

* the MCP config: ``options.mcp_servers`` becomes the file name (the SDK then
  passes ``--mcp-config <name>``); skipped when an in-process ``sdk`` server is
  present, since the SDK needs the dict to find its instance;
* a custom system prompt: ``{"type": "file"}`` → ``--system-prompt-file``;
* a preset's ``append``: dropped from the preset and passed as
  ``--append-system-prompt-file`` through ``extra_args``.

The names are relative to the CLI's working directory, which is the workspace
(the launcher and the broker refuse a spawn whose cwd is anything else), so
they do not put the workspace path — and the user key it is named after — on
the CLI's command line. That covers the CLI's own argv only: each Bash tool
call still runs a shell whose argv carries the command text and a
``<workspace>/.cli-tmp/claude-<hex>-cwd`` path (the CLI's ``pwd -P >|`` tail;
its temp dir lives in the workspace), readable by every session while the
command runs — a separate exposure of §6.5 this module does not close. The
CLI resolves all three names against its cwd while parsing its arguments and
never reads them again, so the caller removes the files once the CLI has
started (:meth:`ArgvFiles.cleanup`, also on failure): ``create_client`` after
``connect()``, ``verify()`` after its one-shot query.
``tests/test_cli_argv_files.py`` pins both against the bundled CLI. A gateway
that dies in between leaves the files behind: :func:`sweep_stale` removes those
in workspaces at the next startup. In landlock mode ``verify()`` runs in the
client's private temp cwd instead (0700, outside every session's ruleset),
which a crash leaves behind with its files, as it always did. The workspace
file API never lists or serves the :data:`PREFIX`. ``--json-schema`` has no
file form in CLI 2.1.283 and stays inline: it is the request's own
``text.format`` schema, not gateway configuration.

Files are created with ``O_EXCL | O_NOFOLLOW`` under a directory fd of the
workspace, so a name the agent planted (a symlink) fails the spawn instead of
redirecting the write. Mode 0600 in landlock mode (the CLI runs as the
gateway's uid); 0644 in uid mode, where the CLI runs as the session uid and the
ruleset — not DAC — keeps every other session out of the workspace.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Every argv file's name starts with this. The workspace file API hides the
# whole prefix (src/routes/terminal_files.py).
PREFIX = ".gateway-argv-"
_KINDS = ("mcp", "system-prompt", "append-prompt")
# Exactly what _write names a file, so the sweep never takes a user file that
# merely starts with the prefix.
_NAME = re.compile(re.escape(PREFIX) + "(?:" + "|".join(_KINDS) + r")-[0-9a-f]{16}\Z")

# A session spawn's files live until its CLI has started: at most the SDK's
# initialize handshake, which times out after 60 s unless
# CLAUDE_CODE_STREAM_CLOSE_TIMEOUT raises it. Fifteen minutes is far above
# that, so the startup sweep never takes the files of a spawn still starting,
# not even one of another gateway process sharing the workspace base. A uid-mode
# verify() keeps its files for its whole one-shot query, which has no timeout;
# should the sweep take them meanwhile, the CLI has already parsed them.
STALE_AFTER_SECONDS = 15 * 60


class ArgvFiles:
    """The files one spawn's argv points at; remove them after ``connect()``."""

    def __init__(self, workspace: Optional[Path] = None) -> None:
        self.workspace = workspace
        self.names: List[str] = []

    @property
    def paths(self) -> List[Path]:
        if self.workspace is None:
            return []
        return [self.workspace / name for name in self.names]

    def cleanup(self) -> None:
        if self.workspace is None or not self.names:
            return
        try:
            dir_fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            logger.warning("argv files in %s not removed: %s", self.workspace, exc)
            return
        try:
            for name in self.names:
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning(
                        "argv file %s/%s not removed: %s", self.workspace, name, exc
                    )
        finally:
            os.close(dir_fd)
        self.names = []


def _write(dir_fd: int, kind: str, text: str, mode: int, files: ArgvFiles) -> str:
    name = f"{PREFIX}{kind}-{secrets.token_hex(8)}"
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=dir_fd,
    )
    files.names.append(name)
    try:
        os.fchmod(fd, mode)  # the umask may have narrowed it
        data = text.encode("utf-8")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
    # Relative to the CLI's cwd (the workspace): an absolute path would put the
    # workspace, named after the user, on the command line.
    return name


def _has_sdk_server(servers: Any) -> bool:
    return isinstance(servers, dict) and any(
        isinstance(cfg, dict) and cfg.get("type") == "sdk" for cfg in servers.values()
    )


def externalize(options: Any, *, shared_uid: bool) -> ArgvFiles:
    """Move *options*' MCP config and system prompt into workspace files.

    *shared_uid* is true in landlock mode (the CLI runs as the gateway's uid,
    files are 0600) and false in uid mode (0644, readable by the session uid).
    Mutates *options*; the names it puts there are relative to
    ``options.cwd``, which must be the directory the CLI runs in. Returns the
    files to remove once the CLI has started. A failure to write raises, so the
    spawn fails closed instead of falling back to the inline argv.
    """
    if not options.cwd:
        return ArgvFiles()
    workspace = Path(os.path.abspath(str(options.cwd)))
    files = ArgvFiles(workspace)
    mode = 0o600 if shared_uid else 0o644
    dir_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        servers = options.mcp_servers
        if isinstance(servers, dict) and servers:
            if _has_sdk_server(servers):
                logger.warning(
                    "an in-process SDK MCP server is configured: the MCP config "
                    "stays on the CLI command line"
                )
            else:
                options.mcp_servers = _write(
                    dir_fd, "mcp", json.dumps({"mcpServers": servers}), mode, files
                )

        prompt = options.system_prompt
        if isinstance(prompt, str) and prompt:
            path = _write(dir_fd, "system-prompt", prompt, mode, files)
            options.system_prompt = {"type": "file", "path": path}
        elif isinstance(prompt, dict):
            if prompt.get("type") == "custom" and prompt.get("prompt"):
                path = _write(dir_fd, "system-prompt", prompt["prompt"], mode, files)
                options.system_prompt = {"type": "file", "path": path}
            elif prompt.get("type") == "preset" and prompt.get("append"):
                path = _write(dir_fd, "append-prompt", prompt["append"], mode, files)
                options.system_prompt = {
                    k: v for k, v in prompt.items() if k != "append"
                }
                extra = dict(options.extra_args or {})
                extra["append-system-prompt-file"] = path
                options.extra_args = extra
    except BaseException:
        files.cleanup()
        raise
    finally:
        os.close(dir_fd)
    return files


def sweep_stale(base: Path, max_age_seconds: float = STALE_AFTER_SECONDS) -> int:
    """Remove argv files a dead gateway left in the workspaces under *base*.

    :meth:`ArgvFiles.cleanup` runs even when the spawn fails, so only a gateway
    that died between :func:`externalize` and the cleanup leaves files behind,
    and nothing else ever removes them. Called at startup. Looks only where a
    workspace can be: directly in each directory under *base*
    (``_tmp_<id>``, a legacy ``<user>`` workspace) and in each directory below
    those (``<user>/<backend dir>``). Takes only names :func:`_write` makes,
    only regular files and symlinks, and only those not modified for
    *max_age_seconds*. Never follows a symlinked directory: each one is opened
    ``O_NOFOLLOW`` beneath its parent's fd and every unlink goes through that
    fd. Never raises; returns how many files it removed.
    """
    cutoff = time.time() - max_age_seconds
    try:
        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return 0
    removed = 0
    try:
        for user_dir in _subdirs(base_fd):
            fd = _open_beneath(base_fd, user_dir)
            if fd is None:
                continue
            try:
                removed += _sweep_files(fd, cutoff)
                for backend_dir in _subdirs(fd):
                    sub = _open_beneath(fd, backend_dir)
                    if sub is None:
                        continue
                    try:
                        removed += _sweep_files(sub, cutoff)
                    finally:
                        os.close(sub)
            finally:
                os.close(fd)
    except Exception:
        logger.warning("argv file sweep under %s stopped early", base, exc_info=True)
    finally:
        os.close(base_fd)
    if removed:
        logger.info("Removed %d stale argv file(s) under %s", removed, base)
    return removed


def _subdirs(dir_fd: int) -> List[str]:
    """Names of the real (not symlinked) directories in *dir_fd*."""
    try:
        with os.scandir(dir_fd) as entries:
            return [e.name for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _open_beneath(dir_fd: int, name: str) -> Optional[int]:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
    except OSError:
        return None


def _sweep_files(dir_fd: int, cutoff: float) -> int:
    try:
        with os.scandir(dir_fd) as entries:
            names = [e.name for e in entries if _NAME.match(e.name)]
    except OSError:
        return 0
    removed = 0
    for name in names:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                continue
            if st.st_mtime > cutoff:
                continue
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            continue
        removed += 1
    return removed
