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
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

from . import plugin_manifest

logger = logging.getLogger(__name__)

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

    # Reject credentials embedded in an http(s) URL (e.g.
    # https://user:token@host/... or https://TOKEN@host/...). The repo string is
    # stored in the manifest, returned in the API response, and printed in clone
    # error logs, so a secret there would leak; use the git_token field instead.
    scheme = repo.split("://", 1)[0].lower() if "://" in repo else ""
    if scheme in ("http", "https"):
        try:
            parts = urlsplit(repo)
        except ValueError:
            parts = None
        if parts is not None and (parts.username or parts.password):
            raise PluginAdminError(
                "invalid repo: credentials must not be embedded in the URL; "
                "use the git_token field for a private repo"
            )

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


def _marketplace_name_from_clone(local_path: Path, repo: str) -> str:
    """Read the marketplace name from a clone's ``marketplace.json``.

    This is the name ``claude`` keys the marketplace under, so it matches what a
    catalog install passes. Falls back to the repo basename. Never raises.
    """
    try:
        catalog = json.loads(
            (local_path / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        name = str(catalog.get("name", "")).strip()
        if name:
            return name
    except (OSError, ValueError, AttributeError):
        pass
    return _default_name(repo)


def _strip_url_credentials(repo: str) -> str:
    """Remove embedded credentials from an http(s) repo URL.

    ``_validate_repo`` rejects credential URLs on the interactive admin-add path,
    but env-bootstrap (``CLAUDE_PLUGIN_REPO*``) and ``known_marketplaces.json``
    fallbacks are not validated, so a ``https://user:token@host/...`` value could
    otherwise be persisted to the journal/manifest and returned by the admin API.
    Strip the userinfo before storing/returning; replay credentials must come
    from ``CLAUDE_PLUGIN_GIT_TOKEN*``, never the URL. Non-http(s) values (ssh,
    scp-like ``git@host:org/repo``, local paths) are returned unchanged — their
    userinfo is the ssh login, not a secret. Never raises.
    """
    repo = (repo or "").strip()
    scheme = repo.split("://", 1)[0].lower() if "://" in repo else ""
    if scheme not in ("http", "https"):
        return repo
    try:
        parts = urlsplit(repo)
    except ValueError:
        return repo
    if not (parts.username or parts.password):
        return repo
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _env_marketplace_record(marketplace: str) -> dict:
    """Env-bootstrap journal record (``{scope, branch, repo}``) for *marketplace*.

    Returns ``{}`` when there is no env-bootstrapped marketplace by that name or
    the journal is unavailable; never raises.
    """
    if not marketplace:
        return {}
    try:
        from . import plugin_service

        rec = plugin_service.env_bootstrap_records().get(marketplace)
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


def _resolve_marketplace_repo(marketplace: str) -> str:
    """Best-effort: resolve a registered marketplace name to a clonable repo.

    Used so a plugin installed from a *catalog* (where the caller supplies only a
    marketplace name, not a repo) still records a replayable ``repo`` in the
    manifest. The admin-managed manifest record is authoritative — it preserves
    the original remote even though ``claude plugin marketplace add`` is handed a
    local clone path and records ``source: directory``. Falls back to
    ``~/.claude/plugins/known_marketplaces.json`` (converting a GitHub
    ``owner/repo`` shorthand to a clone URL) for env/externally-added
    marketplaces. Returns ``""`` when nothing replayable is found. Never raises.

    Any embedded http(s) credential is stripped before returning, since the
    result is persisted to the manifest and surfaced by the admin API.
    """
    return _strip_url_credentials(_resolve_marketplace_repo_raw(marketplace))


def _resolve_marketplace_repo_raw(marketplace: str) -> str:
    if not marketplace:
        return ""
    record = plugin_manifest.get_marketplace(marketplace)
    if record.get("repo", "").strip():
        return record["repo"].strip()
    # Env-bootstrapped marketplace: the startup installer journals its remote,
    # which known_marketplaces.json (source: directory) does not preserve.
    env = _env_marketplace_record(marketplace)
    if env.get("repo", "").strip():
        return env["repo"].strip()
    home = os.environ.get("HOME", "").strip() or str(Path.home())
    known = Path(home) / ".claude" / "plugins" / "known_marketplaces.json"
    try:
        data = json.loads(known.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    entry = data.get(marketplace) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return ""
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    url = str(source.get("url", "")).strip()
    if url:
        return url
    repo = str(source.get("repo", "")).strip()
    if repo:
        if "://" in repo or repo.startswith("git@"):
            return repo
        # GitHub owner/repo shorthand -> clonable HTTPS URL.
        if source.get("source") == "github" or re.match(r"^[\w.-]+/[\w.-]+$", repo):
            return f"https://github.com/{repo}.git"
        return repo
    # Local marketplace: replayable only if the path still exists at startup.
    loc = str(entry.get("installLocation", "")).strip()
    if loc and Path(loc).is_dir():
        return loc
    return ""


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

    # Record the ORIGINAL repo/branch/scope keyed by the marketplace's own name.
    # claude only persists the local clone path (source: directory), so this is
    # the only place the remote survives a `docker compose down -v`.
    name = _marketplace_name_from_clone(Path(local_path), repo)
    plugin_manifest.set_marketplace(name, repo=repo, branch=branch, scope=scope)

    return {
        "status": "added",
        "repo": repo,
        "marketplace": name,
        "path": str(local_path),
        "branch": branch,
        "scope": scope,
    }


def remove_marketplace(name: str, *, scope: str = "user") -> dict:
    """Remove a configured marketplace and drop its managed ``added`` entries.

    known_marketplaces.json does not record a marketplace's scope, so an
    env-added project/local marketplace has no manifest record and the UI can
    only guess ``user``. Try the requested scope first, then the others, so a
    ``[DEL]`` on such a marketplace still removes it at its real scope.
    """
    name = _validate_name(name, what="marketplace name")
    scope = _validate_scope(scope)
    claude_bin = _require_claude_bin()

    scopes = [scope] + [s for s in ("user", "project", "local") if s != scope]
    last_rc = None
    removed_scope = None
    for sc in scopes:
        try:
            run([claude_bin, "plugin", "marketplace", "remove", name, "--scope", sc])
            removed_scope = sc
            break
        except subprocess.CalledProcessError as exc:
            last_rc = exc.returncode

    if removed_scope is None:
        raise PluginAdminError(
            f"claude plugin marketplace remove failed for {name!r} "
            f"(tried scopes {scopes}): rc={last_rc}"
        )

    plugin_manifest.remove_marketplace_entries(name)

    return {"status": "removed", "marketplace": name, "scope": removed_scope}


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

    # For a catalog install the caller passes only a marketplace name; resolve
    # its repo (and branch) from the manifest record so the entry is replayable
    # by the startup installer (which skips entries that carry no repo).
    manifest_repo = repo
    manifest_branch = branch
    if not manifest_repo and marketplace:
        manifest_repo = _resolve_marketplace_repo(marketplace)
        record = plugin_manifest.get_marketplace(marketplace)
        if record.get("branch", "").strip():
            manifest_branch = record["branch"].strip()
        else:
            # Env-bootstrapped marketplace: replay the branch it was cloned at,
            # not the default, so a `down -v` self-heal clones the same content.
            env = _env_marketplace_record(marketplace)
            if env.get("branch", "").strip():
                manifest_branch = env["branch"].strip()
    if not manifest_repo:
        logger.warning(
            "could not resolve a repo for marketplace %r; manifest entry for %r "
            "may not be reinstalled on startup after a plugin-cache wipe",
            marketplace,
            spec,
        )

    plugin_manifest.add_plugin(
        repo=manifest_repo,
        name=name,
        marketplace=marketplace,
        scope=scope,
        branch=manifest_branch,
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

    # scope is part of the identity. Drop any managed entry for THIS (spec,
    # scope) AND always record the removal: if the same plugin is also declared
    # by the env bootstrap, only the recorded removal stops startup from
    # resurrecting it. A later admin re-install clears the mark via add_plugin.
    plugin_manifest.remove_added(spec, scope)
    plugin_manifest.mark_removed(spec, scope)

    return {"status": "uninstalled", "plugin": spec, "scope": scope}
