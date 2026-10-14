"""Keep a confined session's secrets off its command line (#241 §6.5).

Landlock leaves ``/proc`` readable (the bundled CLI aborts without it), and
``/proc/<pid>/cmdline`` is world-readable whatever the uid, so in both
isolation modes any session could read every other CLI's argv: the SDK passes
the MCP config (``MCP_FORWARD_CONTEXT`` ``{{header:…}}`` values included) as
``--mcp-config <json>`` and the system prompt (``Current user:``) as
``--system-prompt`` / ``--append-system-prompt``. With confinement on,
:func:`externalize` moves both into per-spawn files in the session's own
workspace — the one directory the ruleset grants that session and no other —
and leaves only their paths on the command line:

* the MCP config: ``options.mcp_servers`` becomes the file path (the SDK then
  passes ``--mcp-config <path>``); skipped when an in-process ``sdk`` server is
  present, since the SDK needs the dict to find its instance;
* a custom system prompt: ``{"type": "file"}`` → ``--system-prompt-file``;
* a preset's ``append``: dropped from the preset and passed as
  ``--append-system-prompt-file`` through ``extra_args``.

The CLI reads all of them while parsing its arguments, so the caller removes
the files once ``connect()`` returns (:meth:`ArgvFiles.cleanup`, also on
failure); ``tests/test_cli_argv_files.py`` pins that against the bundled CLI.
``--json-schema`` has no file form in CLI 2.1.283 and stays inline: it is the
request's own ``text.format`` schema, not gateway configuration.

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
import secrets
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_PREFIX = ".gateway-argv-"


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
    name = f"{_PREFIX}{kind}-{secrets.token_hex(8)}"
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
    return str(files.workspace / name)


def _has_sdk_server(servers: Any) -> bool:
    return isinstance(servers, dict) and any(
        isinstance(cfg, dict) and cfg.get("type") == "sdk" for cfg in servers.values()
    )


def externalize(options: Any, *, shared_uid: bool) -> ArgvFiles:
    """Move *options*' MCP config and system prompt into workspace files.

    *shared_uid* is true in landlock mode (the CLI runs as the gateway's uid,
    files are 0600) and false in uid mode (0644, readable by the session uid).
    Mutates *options*; returns the files to remove after ``connect()``. A
    failure to write raises, so the spawn fails closed instead of falling back
    to the inline argv.
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
