#!/usr/bin/env python3
"""Install admin-managed Claude Code marketplace plugins at container startup.

Reinstalls every plugin recorded in the admin-managed manifest
(``manifest.added``) on each run, and **fresh-clones every remote repo on each
run** so the latest plugin version is always installed. Plugins are added and
removed from the admin panel; this script makes those choices survive container
restarts and a plugin-cache volume wipe (``docker compose down -v``).

Environment::

    CLAUDE_PLUGIN_MANIFEST         admin-managed manifest path
                                   (default: /app/data/gateway-plugins.json)
    CLAUDE_PLUGIN_GIT_TOKEN        HTTPS git token; the admin panel never
                                   persists tokens, so PRIVATE manifest
                                   marketplaces inherit this as their clone
                                   credential
    CLAUDE_PLUGIN_CLAUDE_BIN       claude CLI path override
    CLAUDE_PLUGIN_CLONE_ROOT       clone root for remote repos
                                   (default: $HOME/.claude/plugin-marketplaces)
    CLAUDE_PLUGIN_TIMEOUT_SECONDS  per git/claude call timeout (default: 300)

Behavior:
    * Manifest repos that point at a clone URL are removed and freshly
      ``git clone --depth 1 --branch <branch>``'d on every run (branch defaults
      to ``main``); values that point at an existing local path are used in
      place (no clone).
    * Each plugin is ``install``'d (first run) and then ``update``'d (later
      runs) so the installed version always tracks the marketplace's latest.
    * Specs in ``manifest.removed`` are skipped and actively uninstalled so an
      admin uninstall stays applied after a plugin-cache wipe.
    * A failure for one plugin or one repo is logged and skipped; it never
      aborts the run, so the gateway always starts. Exit status is always 0.

The git token is fed to ``git`` through a temporary ``GIT_ASKPASS`` helper and
is never passed to the ``claude`` CLI or embedded in any registered marketplace
URL.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOG_PREFIX = "[install_plugins]"

# Container default for the admin-managed manifest. The app side derives this
# from the project root; this standalone script can't, so docker-compose pins
# CLAUDE_PLUGIN_MANIFEST so both sides agree exactly.
DEFAULT_MANIFEST_PATH = "/app/data/gateway-plugins.json"

# Per-subprocess timeout (seconds). Bounds every git/claude call so a stuck
# clone or credential challenge can never wedge container startup. Override with
# CLAUDE_PLUGIN_TIMEOUT_SECONDS.
DEFAULT_TIMEOUT_SECONDS = 300

# Branch/tag fresh-cloned from a remote marketplace repo when the manifest
# entry has no branch. Local-path repos ignore this (they are used in place).
DEFAULT_BRANCH = "main"

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


def log(message: str) -> None:
    """Emit a prefixed diagnostic line to stderr."""
    print(f"{LOG_PREFIX} {message}", file=sys.stderr, flush=True)


@dataclass
class PluginEntry:
    """One marketplace repo and the plugins to install from it."""

    repo: str
    names: List[str] = field(default_factory=list)
    marketplace: str = ""
    scope: str = "user"
    branch: str = DEFAULT_BRANCH
    git_token: str = ""
    source_label: str = ""


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------


def _default_name(repo: str) -> str:
    """Infer a plugin name from a repo URL/path basename.

    Mirrors the previous shell installer: strip a trailing ``/``, take the
    basename, then drop any ``#ref``, ``@version`` and ``.git`` suffix.
    """
    base = repo.rstrip("/").split("/")[-1]
    base = base.split("#", 1)[0]
    base = base.split("@", 1)[0]
    if base.endswith(".git"):
        base = base[: -len(".git")]
    return base


def collect_entries() -> List[PluginEntry]:
    """Build the install list from the admin-managed manifest (``manifest.added``).

    Reinstalling every added plugin on each boot is what makes admin installs
    self-healing after a plugin-cache volume wipe.
    """
    return _manifest_entries()


# ---------------------------------------------------------------------------
# Admin-managed manifest (read inline; stdlib only — never import src)
# ---------------------------------------------------------------------------


def manifest_path() -> Path:
    """Resolve the manifest path, matching the app side's env override."""
    configured = os.environ.get("CLAUDE_PLUGIN_MANIFEST", "").strip()
    return Path(configured) if configured else Path(DEFAULT_MANIFEST_PATH)


def load_manifest() -> Dict[str, list]:
    """Read the manifest, tolerating a missing/corrupt file.

    Always returns ``{"added": [...], "removed": [...]}`` with ``added`` filtered
    to dicts and ``removed`` normalized to ``{"spec","scope"}`` dicts (scope is
    part of a plugin's identity); never raises.
    """
    empty: Dict[str, list] = {"added": [], "removed": []}
    try:
        raw = manifest_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return empty
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    added = data.get("added")
    removed = data.get("removed")
    removed_norm = []
    if isinstance(removed, list):
        for r in removed:
            if isinstance(r, dict) and isinstance(r.get("spec"), str) and r["spec"]:
                scope = r.get("scope")
                removed_norm.append(
                    {
                        "spec": r["spec"],
                        "scope": scope if isinstance(scope, str) and scope else "user",
                    }
                )
            elif isinstance(r, str) and r:  # legacy spec-only
                removed_norm.append({"spec": r, "scope": "user"})
    return {
        "added": [e for e in added if isinstance(e, dict)]
        if isinstance(added, list)
        else [],
        "removed": removed_norm,
    }


def _manifest_entries() -> List[PluginEntry]:
    """Build :class:`PluginEntry` objects from ``manifest.added``.

    Each added record carries its own repo/name/marketplace/scope/branch, so it
    becomes a single-plugin entry. Records missing both a repo and a name are
    dropped (nothing actionable to install).

    The admin panel never persists a git token, so a PRIVATE admin-added
    marketplace would otherwise clone unauthenticated and fail. As a fallback,
    manifest entries inherit ``CLAUDE_PLUGIN_GIT_TOKEN`` so a private
    marketplace replays when that token is provided at startup.
    """
    manifest_token = os.environ.get("CLAUDE_PLUGIN_GIT_TOKEN", "")
    entries: List[PluginEntry] = []
    for record in load_manifest()["added"]:
        repo = str(record.get("repo", "")).strip()
        name = str(record.get("name", "")).strip()
        if not name:
            name = _default_name(repo)
        if not repo or not name:
            log(f"manifest: skipping entry with no actionable repo/name: {record!r}")
            continue
        entries.append(
            PluginEntry(
                repo=repo,
                names=[name],
                marketplace=str(record.get("marketplace", "")).strip(),
                scope=str(record.get("scope", "")).strip() or "user",
                branch=str(record.get("branch", "")).strip() or DEFAULT_BRANCH,
                git_token=manifest_token,
                source_label="manifest",
            )
        )
    return entries


def _spec(name: str, marketplace: str) -> str:
    """``name@marketplace`` when *marketplace* is truthy, else ``name``."""
    return f"{name}@{marketplace}" if marketplace else name


# ---------------------------------------------------------------------------
# claude CLI discovery
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
        # Accept either a path or a bare command name resolvable on PATH; fall
        # back to the verbatim value so main() can name it in the skip message.
        if _is_executable(override):
            return override
        return shutil.which(override) or override

    on_path = shutil.which("claude")
    if on_path:
        return on_path

    return _bundled_claude()


# ---------------------------------------------------------------------------
# Command execution
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


def run(
    cmd: List[str], *, token: str = "", check: bool = True
) -> subprocess.CompletedProcess:
    """Run *cmd*, streaming its output straight to the container logs.

    Three guarantees that matter for an unattended startup installer:

    * The git token reaches git only through a per-call ``GIT_ASKPASS`` helper.
      Every child runs with the persistent ``CLAUDE_PLUGIN_GIT_TOKEN*`` vars
      stripped from its environment, so the secret never reaches the claude CLI
      (or one repo's plugin code while another repo's token is in scope).
    * Terminal prompting is disabled (``GIT_TERMINAL_PROMPT=0``) so a missing
      credential fails fast instead of blocking forever on a prompt.
    * A timeout bounds the call; a stuck clone/install can never wedge startup.

    Raises :class:`subprocess.CalledProcessError` on a non-zero exit or timeout
    when *check* is true.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE_PLUGIN_GIT_TOKEN")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    askpass_path = _write_askpass(token, env) if token else None
    try:
        proc = subprocess.run(cmd, env=env, timeout=_timeout_seconds())
    except subprocess.TimeoutExpired as exc:
        raise subprocess.CalledProcessError(124, cmd) from exc
    finally:
        if askpass_path is not None:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return proc


# ---------------------------------------------------------------------------
# Repo preparation and plugin install/update
# ---------------------------------------------------------------------------


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


def prepare_repo(entry: PluginEntry, root: Path) -> Path:
    """Return the local marketplace path for *entry*, fresh-cloning if remote.

    Local-path repos are used in place (``entry.branch`` is ignored). Remote
    repos are removed and freshly cloned from ``entry.branch`` every run so the
    latest marketplace content is always on disk.
    """
    if Path(entry.repo).exists():
        return Path(entry.repo)

    dest = _clone_dir(root, entry.repo)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Clone into a staging dir and swap it in only on success, so a failed
    # re-clone leaves any previous clone (and its registered marketplace
    # installLocation) intact rather than dangling at a deleted path.
    staging = dest.with_name(dest.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    log(f"{entry.source_label}: fresh-cloning {entry.repo} (branch {entry.branch}) -> {dest}")
    run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            entry.branch,
            entry.repo,
            str(staging),
        ],
        token=entry.git_token,
    )
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(staging, dest)
    return dest


def install_and_update(claude_bin: str, spec: str, scope: str) -> bool:
    """Install *spec* if missing, then update it to the marketplace's latest.

    ``claude plugin install`` is idempotent and will NOT bump an
    already-installed plugin, so ``claude plugin update`` is run afterward to
    pull the latest version of the freshly-cloned marketplace. Returns ``True``
    when the plugin is installed (regardless of whether a newer version existed).
    """
    try:
        run([claude_bin, "plugin", "install", spec, "--scope", scope])
    except subprocess.CalledProcessError as exc:
        log(f"install failed for {spec!r} (scope {scope}): rc={exc.returncode}")
        return False

    try:
        run([claude_bin, "plugin", "update", spec, "--scope", scope])
    except subprocess.CalledProcessError as exc:
        # Non-fatal: the plugin is installed, it just may not have been bumped.
        log(
            f"update for {spec!r} returned rc={exc.returncode}; keeping installed version"
        )

    log(f"ensured latest {spec} (scope: {scope})")
    return True


def process_entry(
    entry: PluginEntry,
    claude_bin: str,
    root: Path,
    removed_set: Optional[set] = None,
) -> Tuple[int, int]:
    """Prepare one repo and install/update its plugins. Returns ``(ok, failed)``.

    Any (spec, scope) present in *removed_set* (the admin-uninstalled set from the
    manifest) is skipped before any subprocess for that plugin runs. scope is part
    of the identity, so an entry is only skipped when its own scope was removed.
    """
    removed_set = removed_set or set()
    names = [
        n
        for n in entry.names
        if (_spec(n, entry.marketplace), entry.scope) not in removed_set
    ]
    skipped = [
        n
        for n in entry.names
        if (_spec(n, entry.marketplace), entry.scope) in removed_set
    ]
    for name in skipped:
        log(
            f"{entry.source_label}: skipping {_spec(name, entry.marketplace)!r} "
            f"(marked removed in manifest)"
        )
    if not names:
        return (0, 0)

    try:
        local_path = prepare_repo(entry, root)
    except subprocess.CalledProcessError as exc:
        log(
            f"{entry.source_label}: clone failed for {entry.repo!r}: rc={exc.returncode}"
        )
        return (0, len(entry.names))
    except OSError as exc:
        log(f"{entry.source_label}: could not prepare {entry.repo!r}: {exc}")
        return (0, len(entry.names))

    try:
        run(
            [
                claude_bin,
                "plugin",
                "marketplace",
                "add",
                str(local_path),
                "--scope",
                entry.scope,
            ]
        )
    except subprocess.CalledProcessError as exc:
        log(
            f"{entry.source_label}: marketplace add failed for {local_path} "
            f"(scope {entry.scope}): rc={exc.returncode}"
        )
        return (0, len(entry.names))

    ok = 0
    failed = 0
    for name in names:
        spec = _spec(name, entry.marketplace)
        if install_and_update(claude_bin, spec, entry.scope):
            ok += 1
        else:
            failed += 1
    return (ok, failed)


def main() -> int:
    entries = collect_entries()
    if not entries:
        return 0  # nothing in the manifest — no-op

    total_plugins = sum(len(entry.names) for entry in entries)
    claude_bin = resolve_claude_bin()
    if not _is_executable(claude_bin or ""):
        log("claude CLI not found or not executable")
        log(
            "set CLAUDE_PLUGIN_CLAUDE_BIN or install claude-agent-sdk with its bundled CLI"
        )
        log(
            f"skipping {total_plugins} configured plugin(s) across {len(entries)} repo(s)"
        )
        return 0  # never block server startup

    removed_set = {(r["spec"], r["scope"]) for r in load_manifest()["removed"]}

    root = clone_root()
    total_ok = 0
    total_failed = 0
    for entry in entries:
        ok, failed = process_entry(entry, claude_bin, root, removed_set)
        total_ok += ok
        total_failed += failed

    apply_removed(claude_bin, removed_set)

    log(
        f"done: {total_ok} plugin(s) ensured, {total_failed} failed, across {len(entries)} repo(s)"
    )
    return 0


def apply_removed(claude_bin: str, removed_set: set) -> None:
    """Best-effort uninstall every spec the admin marked removed.

    Install for these specs was skipped above; this actively uninstalls them at
    the scope they were removed from so an admin uninstall stays applied even
    when the plugin survives in the plugin cache from an earlier run. Failures
    are logged and ignored — uninstalling an already-absent plugin is expected
    and must never block startup.

    *removed_set* is a set of ``(spec, scope)`` tuples.
    """
    for spec, scope in sorted(removed_set):
        try:
            run([claude_bin, "plugin", "uninstall", spec, "--scope", scope])
            log(f"uninstalled {spec!r} (scope {scope}; marked removed in manifest)")
        except subprocess.CalledProcessError as exc:
            log(
                f"uninstall for {spec!r} (scope {scope}) returned rc={exc.returncode}; "
                f"likely already absent, continuing"
            )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let plugin install block the gateway server
        log(f"unexpected error, skipping plugin install: {exc!r}")
        sys.exit(0)
