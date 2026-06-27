"""Runtime plugin / marketplace mutations via the ``claude`` CLI.

The admin panel uses this module to add/remove Claude Code marketplaces and
install/uninstall plugins at runtime. Every successful mutation is recorded in
the persistent managed manifest (:mod:`src.plugin_manifest`) so the startup
installer (:mod:`docker.install_plugins`) can replay it after a plugin-cache
volume wipe (manifest ``added`` is reinstalled, ``removed`` is reapplied).

Behavior mirrors ``docker/install_plugins.py``:

* A **remote** repo is fresh-cloned into the shared clone root
  (``CLAUDE_PLUGIN_CLONE_ROOT`` or ``$HOME/.claude/plugin-marketplaces``) and
  then registered with ``claude plugin marketplace add <local_path>``.
* A **local-path** repo is added in place (no clone).
* Plugins are installed with ``claude plugin install <spec> --scope`` followed
  by ``claude plugin update``; uninstall is ``claude plugin uninstall``.

Security:

* The git token reaches git only through a temporary ``GIT_ASKPASS`` helper; it
  never appears on a command line, in a registered marketplace URL, or in a log.
* Every child runs with ``CLAUDE_PLUGIN_GIT_TOKEN*`` stripped and
  ``GIT_TERMINAL_PROMPT=0``.
* All inputs are validated before any subprocess runs; commands use arg lists
  (never ``shell=True``).

All functions here are synchronous; routes wrap them in
``fastapi.concurrency.run_in_threadpool``.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from . import plugin_manifest

# Branch fresh-cloned from a remote marketplace repo when none is given.
DEFAULT_BRANCH = "main"

# Bound every git/claude call so a stuck clone or credential challenge can never
# wedge the request thread. Override with CLAUDE_PLUGIN_TIMEOUT_SECONDS.
DEFAULT_TIMEOUT_SECONDS = 300

# Allowed scopes for marketplace/plugin declarations.
_VALID_SCOPES = frozenset({"user", "project", "local"})

# Plugin / marketplace names: letters, digits and a small punctuation set. No
# whitespace, no shell metacharacters.
_NAME_RE = re.compile(r"^[A-Za-z0-9._@/-]+$")

# A spec is name[@marketplace]; the "@" is allowed by _NAME_RE itself, so the
# whole spec validates with the same pattern.

# Helper that answers git's credential prompts using a token supplied via the
# CLAUDE_PLUGIN_ASKPASS_TOKEN environment variable. The token itself never
# appears on a command line or in the marketplace URL handed to ``claude``.
_ASKPASS_SCRIPT = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    "    *Username*) printf '%s\\n' \"x-access-token\" ;;\n"
    "    *Password*) printf '%s\\n' \"$CLAUDE_PLUGIN_ASKPASS_TOKEN\" ;;\n"
    "    *) printf '\\n' ;;\n"
    "esac\n"
)


class PluginAdminError(ValueError):
    """Raised on invalid input or a failed ``claude``/``git`` invocation."""


# ---------------------------------------------------------------------------
# claude CLI discovery (mirrors docker/install_plugins.py)
# ---------------------------------------------------------------------------


def _is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _bundled_claude() -> Optional[str]:
    """Locate the ``claude`` CLI bundled with ``claude_agent_sdk``."""
    try:
        import claude_agent_sdk  # type: ignore
    except Exception:  # pragma: no cover - import failure path
        return None
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    candidate = Path(claude_agent_sdk.__file__).parent / "_bundled" / cli_name
    return str(candidate) if candidate.is_file() else None


def resolve_claude_bin() -> Optional[str]:
    """Resolve the claude CLI: explicit override, then PATH, then bundled SDK."""
    override = os.environ.get("CLAUDE_PLUGIN_CLAUDE_BIN", "").strip()
    if override:
        if _is_executable(override):
            return override
        return shutil.which(override) or override

    on_path = shutil.which("claude")
    if on_path:
        return on_path

    return _bundled_claude()


def _require_claude_bin() -> str:
    claude_bin = resolve_claude_bin()
    if not _is_executable(claude_bin or ""):
        raise PluginAdminError(
            "claude CLI not found or not executable; set CLAUDE_PLUGIN_CLAUDE_BIN "
            "or install claude-agent-sdk with its bundled CLI"
        )
    return claude_bin  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Command execution (mirrors docker/install_plugins.py)
# ---------------------------------------------------------------------------


def _write_askpass(token: str, env: dict) -> str:
    """Write a temporary GIT_ASKPASS helper and wire it into *env*."""
    fd, path = tempfile.mkstemp(prefix="git-askpass-", suffix=".sh")
    with os.fdopen(fd, "w") as handle:
        handle.write(_ASKPASS_SCRIPT)
    os.chmod(path, 0o700)
    env["GIT_ASKPASS"] = path
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["CLAUDE_PLUGIN_ASKPASS_TOKEN"] = token
    return path


def _timeout_seconds() -> int:
    raw = os.environ.get("CLAUDE_PLUGIN_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_SECONDS


def run(cmd: List[str], *, token: str = "") -> subprocess.CompletedProcess:
    """Run *cmd* as an arg list and capture its output.

    The git token reaches git only through a per-call ``GIT_ASKPASS`` helper.
    Every child runs with the persistent ``CLAUDE_PLUGIN_GIT_TOKEN*`` vars
    stripped and ``GIT_TERMINAL_PROMPT=0`` so a missing credential fails fast.
    A timeout bounds the call. Raises :class:`subprocess.CalledProcessError` on
    a non-zero exit or timeout.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE_PLUGIN_GIT_TOKEN")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    askpass_path = _write_askpass(token, env) if token else None
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            timeout=_timeout_seconds(),
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise subprocess.CalledProcessError(124, cmd) from exc
    finally:
        if askpass_path is not None:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )
    return proc


# ---------------------------------------------------------------------------
# Input validation (runs before ANY subprocess)
# ---------------------------------------------------------------------------


def _validate_name(value: str, *, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise PluginAdminError(f"{what} is required")
    if not _NAME_RE.match(value):
        raise PluginAdminError(
            f"invalid {what} {value!r}: only letters, digits and ._@/- are allowed"
        )
    return value


def _validate_scope(scope: str) -> str:
    scope = (scope or "").strip() or "user"
    if scope not in _VALID_SCOPES:
        raise PluginAdminError(
            f"invalid scope {scope!r}: expected one of {sorted(_VALID_SCOPES)}"
        )
    return scope


def _validate_branch(branch: str) -> str:
    branch = (branch or "").strip() or DEFAULT_BRANCH
    if not _NAME_RE.match(branch):
        raise PluginAdminError(
            f"invalid branch {branch!r}: only letters, digits and ._@/- are allowed"
        )
    return branch


def _has_shell_metacharacters(value: str) -> bool:
    # Whitespace or any of the usual shell-injection characters.
    if any(c.isspace() for c in value):
        return True
    return bool(re.search(r"""[;&|`$<>(){}\[\]'"\\!*?~]""", value))


def _validate_repo(repo: str) -> str:
    """Validate *repo* is an http(s)/git/ssh URL or an existing local path."""
    repo = (repo or "").strip()
    if not repo:
        raise PluginAdminError("repo is required")

    # An existing local path is always accepted (it is used in place; never
    # interpolated into a shell).
    if Path(repo).exists():
        return repo

    if _has_shell_metacharacters(repo):
        raise PluginAdminError(f"invalid repo {repo!r}: contains illegal characters")

    # Accept common remote forms: http(s)://, git://, ssh://, and scp-like
    # git@host:org/repo.git.
    if re.match(r"^(https?|git|ssh)://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+$", repo):
        return repo
    if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[A-Za-z0-9._/~-]+$", repo):
        return repo

    raise PluginAdminError(
        f"invalid repo {repo!r}: expected an http(s)/git/ssh URL or an existing "
        "local path"
    )


# ---------------------------------------------------------------------------
# Repo preparation (mirrors docker/install_plugins.py)
# ---------------------------------------------------------------------------


def _default_name(repo: str) -> str:
    """Infer a marketplace/plugin name from a repo URL/path basename."""
    base = repo.rstrip("/").split("/")[-1]
    base = base.split("#", 1)[0]
    base = base.split("@", 1)[0]
    if base.endswith(".git"):
        base = base[: -len(".git")]
    return base


def clone_root() -> Path:
    """Resolve the directory remote marketplace repos are cloned into."""
    configured = os.environ.get("CLAUDE_PLUGIN_CLONE_ROOT", "").strip()
    if configured:
        return Path(configured)
    home = os.environ.get("HOME", "").strip() or tempfile.gettempdir()
    return Path(home) / ".claude" / "plugin-marketplaces"


def _clone_dir(root: Path, repo: str) -> Path:
    """Return a collision-free per-repo clone directory under *root*."""
    base = _default_name(repo) or "marketplace"
    digest = hashlib.sha1(repo.encode("utf-8")).hexdigest()[:8]
    return root / f"{base}-{digest}"


def prepare_repo(repo: str, *, branch: str, token: str, root: Path) -> Path:
    """Return the local marketplace path for *repo*, fresh-cloning if remote.

    A local-path repo is used in place (*branch* is ignored). A remote repo is
    cloned into a staging dir and swapped in only on success, so a failed
    re-clone leaves any previous clone intact.
    """
    if Path(repo).exists():
        return Path(repo)

    dest = _clone_dir(root, repo)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(dest.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                branch,
                repo,
                str(staging),
            ],
            token=token,
        )
    except subprocess.CalledProcessError as exc:
        raise PluginAdminError(
            f"git clone failed for {repo!r} (branch {branch}): rc={exc.returncode}"
        ) from exc
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(staging, dest)
    return dest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_marketplace(
    repo: str,
    *,
    branch: str = "main",
    scope: str = "user",
    git_token: str = "",
) -> dict:
    """Register a marketplace from *repo* (clone-then-add for a remote repo)."""
    repo = _validate_repo(repo)
    branch = _validate_branch(branch)
    scope = _validate_scope(scope)
    claude_bin = _require_claude_bin()

    local_path = prepare_repo(repo, branch=branch, token=git_token, root=clone_root())
    try:
        run(
            [
                claude_bin,
                "plugin",
                "marketplace",
                "add",
                str(local_path),
                "--scope",
                scope,
            ]
        )
    except subprocess.CalledProcessError as exc:
        raise PluginAdminError(
            f"claude plugin marketplace add failed for {local_path} "
            f"(scope {scope}): rc={exc.returncode}"
        ) from exc

    return {
        "status": "added",
        "repo": repo,
        "path": str(local_path),
        "branch": branch,
        "scope": scope,
    }


def remove_marketplace(name: str, *, scope: str = "user") -> dict:
    """Remove a configured marketplace and drop its managed ``added`` entries."""
    name = _validate_name(name, what="marketplace name")
    scope = _validate_scope(scope)
    claude_bin = _require_claude_bin()

    try:
        run(
            [
                claude_bin,
                "plugin",
                "marketplace",
                "remove",
                name,
                "--scope",
                scope,
            ]
        )
    except subprocess.CalledProcessError as exc:
        raise PluginAdminError(
            f"claude plugin marketplace remove failed for {name!r} "
            f"(scope {scope}): rc={exc.returncode}"
        ) from exc

    plugin_manifest.remove_marketplace_entries(name)

    return {"status": "removed", "marketplace": name, "scope": scope}


def install_plugin(
    name: str,
    *,
    marketplace: str = "",
    scope: str = "user",
    repo: str = "",
    branch: str = "main",
) -> dict:
    """Install (and update) a plugin, recording it in the managed manifest.

    If *repo* is given it is registered as a marketplace first (clone-then-add
    for a remote repo), exactly like the startup installer.
    """
    name = _validate_name(name, what="plugin name")
    scope = _validate_scope(scope)
    if marketplace:
        marketplace = _validate_name(marketplace, what="marketplace name")
    branch = _validate_branch(branch)
    if repo:
        # add_marketplace validates repo/branch/scope itself and resolves the
        # claude bin; let it raise PluginAdminError before we install.
        add_marketplace(repo, branch=branch, scope=scope, git_token="")

    claude_bin = _require_claude_bin()
    spec = plugin_manifest.spec_for(name, marketplace)

    try:
        run([claude_bin, "plugin", "install", spec, "--scope", scope])
    except subprocess.CalledProcessError as exc:
        raise PluginAdminError(
            f"claude plugin install failed for {spec!r} (scope {scope}): "
            f"rc={exc.returncode}"
        ) from exc

    # Update is best-effort: the plugin is installed even if no newer version
    # exists, so a non-zero update rc is not fatal.
    try:
        run([claude_bin, "plugin", "update", spec, "--scope", scope])
    except subprocess.CalledProcessError:
        pass

    plugin_manifest.add_plugin(
        repo=repo,
        name=name,
        marketplace=marketplace,
        scope=scope,
        branch=branch,
    )

    return {
        "status": "installed",
        "plugin": name,
        "marketplace": marketplace,
        "spec": spec,
        "scope": scope,
    }


def uninstall_plugin(plugin_id: str, *, scope: str = "user") -> dict:
    """Uninstall *plugin_id* (registry key ``name@marketplace``).

    If the spec is an admin-managed ``added`` entry it is dropped from the
    manifest; otherwise it is marked ``removed`` (it came from env bootstrap, so
    startup must skip reinstalling it).
    """
    spec = _validate_name(plugin_id, what="plugin id")
    scope = _validate_scope(scope)
    claude_bin = _require_claude_bin()

    try:
        run([claude_bin, "plugin", "uninstall", spec, "--scope", scope])
    except subprocess.CalledProcessError as exc:
        raise PluginAdminError(
            f"claude plugin uninstall failed for {spec!r} (scope {scope}): "
            f"rc={exc.returncode}"
        ) from exc

    added_specs = {
        plugin_manifest.spec_for(e.get("name", ""), e.get("marketplace", ""))
        for e in plugin_manifest.list_added()
    }
    if spec in added_specs:
        plugin_manifest.remove_added(spec)
    else:
        plugin_manifest.mark_removed(spec)

    return {"status": "uninstalled", "plugin": spec, "scope": scope}
