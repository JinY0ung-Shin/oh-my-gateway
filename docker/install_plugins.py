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
      place (no clone) — unless Landlock confinement is on and sessions could
      not read them there (``/app``, the workspace base, the temp dir): then
      they are copied into the clone root on every run (#258).
    * Each plugin is ``install``'d (first run) and then ``update``'d (later
      runs) so the installed version always tracks the marketplace's latest.
    * Specs in ``manifest.removed`` are skipped and actively uninstalled so an
      admin uninstall stays applied after a plugin-cache wipe.
    * A failure for one plugin or one repo is logged and skipped; it never
      aborts the run, so the gateway always starts. Exit status is always 0.
    * The whole replay holds ``<claude config dir>/.plugin-replay.lock``
      (``flock``; the admin plugin paths take it too), so two gateway colors
      never run ``claude plugin ...`` at once (blue/green deploy, §5.4).
    * A replay with no failure records the digest of its inputs (manifest,
      the commit ``git ls-remote`` reports per remote marketplace, a tree
      digest per local one, the CLI version, the registry it left) in
      ``plugin-replay.digest`` beside the manifest. A boot whose inputs match
      it skips the replay: no clone, no ``claude plugin`` call. Any failure
      computing it replays, as before.

The git token is fed to ``git`` through a temporary ``GIT_ASKPASS`` helper and
is never passed to the ``claude`` CLI or embedded in any registered marketplace
URL.
"""
from __future__ import annotations

import contextlib
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
    cmd: List[str], *, token: str = "", check: bool = True, capture: bool = False
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
        proc = subprocess.run(
            cmd,
            env=env,
            timeout=_timeout_seconds(),
            stdout=subprocess.PIPE if capture else None,
            stdin=subprocess.DEVNULL if capture else None,
            text=capture,
        )
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


def _load_local_copy():
    """The gateway's ``plugin_local_copy`` module: beside this script in the
    image (``/usr/local/bin``), ``../src`` in a checkout. ``None`` if absent."""
    import importlib.util

    here = Path(__file__).resolve().parent
    candidates = (
        here / "plugin_local_copy.py",
        here.parent / "src" / "plugin_local_copy.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location(
                "plugin_local_copy", candidate
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def _app_root() -> Path:
    """The gateway app root: the CMD runs from it (``/app``)."""
    cwd = Path.cwd()
    return cwd if (cwd / "src" / "main.py").is_file() else Path("/app")


def _local_copy(entry: PluginEntry, root: Path) -> Path:
    """The local repo itself, or a fresh copy in the clone root when a confined
    session could not read it in place (#258; same check as the admin path)."""
    lc = _load_local_copy()
    if lc is None:
        if os.environ.get("CLAUDE_ISOLATION_MODE", "").strip():
            log(
                f"{entry.source_label}: plugin_local_copy.py missing; "
                f"{entry.repo} registered in place"
            )
        return Path(entry.repo)
    app_root = _app_root()
    if not lc.needs_copy(entry.repo, app_root=app_root):
        return Path(entry.repo)
    dest = _clone_dir(root, entry.repo)
    log(
        f"{entry.source_label}: copying local {entry.repo} -> {dest} "
        "(confined sessions cannot read it in place)"
    )
    try:
        skipped = lc.copy_marketplace(entry.repo, dest, app_root=app_root)
    except lc.LocalCopyError as exc:
        raise OSError(str(exc)) from exc
    if skipped:
        log(
            f"{entry.source_label}: copied without {len(skipped)} "
            "entries (links out of the tree, hard links, special files): "
            + ", ".join(skipped[:10])
        )
    return dest


def prepare_repo(entry: PluginEntry, root: Path) -> Path:
    """Return the local marketplace path for *entry*, fresh-cloning if remote.

    Local-path repos are used in place (``entry.branch`` is ignored). Remote
    repos are freshly cloned from ``entry.branch`` every run so the latest
    marketplace content is always on disk, into a fresh staging dir published
    by ``plugin_local_copy.replace_tree`` (#262): serialized with an admin
    refresh of the same clone, swapped in atomically, and a failed re-clone or
    swap leaves the previous clone (and its registered installLocation) intact.
    """
    if Path(entry.repo).exists():
        return _local_copy(entry, root)

    lc = _load_local_copy()
    if lc is None:
        raise OSError("plugin_local_copy.py is missing beside install_plugins.py")
    dest = _clone_dir(root, entry.repo)
    log(
        f"{entry.source_label}: fresh-cloning {entry.repo} "
        f"(branch {entry.branch}) -> {dest}"
    )

    def clone(staging: Path) -> None:
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

    try:
        lc.replace_tree(dest, clone)
    except lc.LocalCopyError as exc:
        raise OSError(str(exc)) from exc
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
    lc = _load_local_copy()
    _sweep_parked_trees(lc)
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

    with _replay_lock(lc):
        return _replay(entries, claude_bin, lc)


def _replay(entries: List[PluginEntry], claude_bin: str, lc) -> int:
    """The manifest replay, under the replay lock (see :func:`main`)."""
    inputs = _input_material(entries, claude_bin) if lc is not None else None
    data_dir = manifest_path().parent
    if inputs is not None:
        digest = _digest(inputs, _registry_material())
        if digest is not None and digest == lc.read_replay_digest(data_dir):
            log(
                f"inputs unchanged since the last replay (digest {digest[:12]}); "
                f"skipping {sum(len(e.names) for e in entries)} plugin(s) across "
                f"{len(entries)} repo(s)"
            )
            return 0
    if lc is not None:
        try:
            lc.invalidate_replay_digest(data_dir)
        except OSError as exc:
            log(f"could not remove the replay digest: {exc}")
            inputs = None  # a stale digest stays: never write a new one over it

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
    if inputs is not None and total_failed == 0:
        digest = _digest(inputs, _registry_material())
        if digest is not None:
            try:
                lc.write_replay_digest(data_dir, digest)
            except OSError as exc:
                log(f"could not write the replay digest: {exc}")
    return 0


# ---------------------------------------------------------------------------
# Blue/green: replay lock, input digest, parked-tree sweep (§5.4)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _replay_lock(lc):
    """``plugin_local_copy.replay_lock()``; without it (module missing, or the
    lock cannot be taken) the replay runs unlocked, as before."""
    if lc is None:
        yield
        return
    lock = lc.replay_lock()
    try:
        lock.__enter__()
    except OSError as exc:
        log(f"could not take the plugin replay lock, replaying unlocked: {exc}")
        yield
        return
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _sweep_parked_trees(lc) -> None:
    """Remove replaced clone-root trees past their grace (best effort; none
    while another gateway instance is alive)."""
    if lc is None:
        return
    try:
        root = clone_root()
        if root.is_dir():
            lc.sweep_parked_trees(root)
    except Exception as exc:  # never block startup
        log(f"parked-tree sweep skipped: {exc!r}")


# Bound on the local-source tree digest: a larger tree is not digested (the
# replay then always runs, as before). ``.git`` internals are not walked.
_TREE_MAX_ENTRIES = 20000
_TREE_SKIP_DIRS = frozenset({".git"})


def _tree_material(path: str) -> str:
    """sha256 over (path, mode, size, mtime) of every entry below *path*;
    links are recorded, never followed. Raises ``OSError`` past the cap."""
    root = os.path.realpath(path)
    h = hashlib.sha256(root.encode("utf-8", "surrogateescape"))
    count = 0

    def fail(exc: OSError) -> None:
        raise exc

    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        dirnames[:] = sorted(d for d in dirnames if d not in _TREE_SKIP_DIRS)
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            count += 1
            if count > _TREE_MAX_ENTRIES:
                raise OSError(f"{root} has more than {_TREE_MAX_ENTRIES} entries")
            target = os.readlink(full) if os.path.islink(full) else ""
            rel = os.path.relpath(full, root)
            line = f"{rel}\0{st.st_mode}\0{st.st_size}\0{st.st_mtime_ns}\0{target}\n"
            h.update(line.encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _remote_material(entry: PluginEntry) -> str:
    """What ``git ls-remote`` reports for the entry's branch (or tag)."""
    proc = run(
        ["git", "ls-remote", "--", entry.repo, entry.branch],
        token=entry.git_token,
        capture=True,
    )
    lines = sorted(line.strip() for line in (proc.stdout or "").splitlines())
    lines = [line for line in lines if line]
    if not lines:
        raise OSError(f"git ls-remote reported no ref {entry.branch!r}")
    return "\n".join(lines)


def _input_material(entries: List[PluginEntry], claude_bin: str) -> Optional[dict]:
    """Everything the replay's outcome depends on, or ``None`` (replay)."""
    try:
        version = run([claude_bin, "--version"], capture=True).stdout.strip()
        if not version:
            raise OSError("claude --version printed nothing")
        sources: Dict[str, str] = {}
        for entry in entries:
            key = f"{entry.repo}\0{entry.branch}"
            if key in sources:
                continue
            if Path(entry.repo).exists():
                sources[key] = "local:" + _tree_material(entry.repo)
            else:
                sources[key] = "remote:" + _remote_material(entry)
        manifest = load_manifest()
        return {
            "version": 1,
            "manifest": {"added": manifest["added"], "removed": manifest["removed"]},
            "token": bool(os.environ.get("CLAUDE_PLUGIN_GIT_TOKEN", "")),
            "sources": sources,
            "cli": version,
            "claude_bin": claude_bin,
            "clone_root": str(clone_root()),
            "app_root": str(_app_root()),
            "env": {
                k: os.environ.get(k, "")
                for k in (
                    "CLAUDE_ISOLATION_MODE",
                    "CLAUDE_CONFIG_DIR",
                    "HOME",
                    "USER_WORKSPACES_DIR",
                )
            },
        }
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        log(f"could not compute the replay inputs, replaying: {exc}")
        return None


def _registry_material() -> Optional[dict]:
    """What the replay left in the CLI's registry: marketplaces and installed
    plugins (timestamps dropped) and whether each recorded path still exists.
    A wiped plugin volume reads differently, so its boot replays."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    config = Path(configured) if configured else Path.home() / ".claude"
    plugins = config / "plugins"
    try:
        loaded = {}
        for name in ("known_marketplaces.json", "installed_plugins.json"):
            try:
                loaded[name] = json.loads((plugins / name).read_text("utf-8"))
            except FileNotFoundError:
                loaded[name] = None
        known = loaded["known_marketplaces.json"] or {}
        installed = (loaded["installed_plugins.json"] or {}).get("plugins") or {}
        marketplaces = {
            str(name): {
                "source": rec.get("source"),
                "installLocation": rec.get("installLocation"),
                "present": os.path.isdir(str(rec.get("installLocation") or "")),
            }
            for name, rec in sorted(known.items())
        }
        plugin_rows = {
            str(pid): sorted(
                json.dumps(
                    {
                        "scope": row.get("scope"),
                        "version": row.get("version"),
                        "installPath": row.get("installPath"),
                        "projectPath": row.get("projectPath"),
                        "present": os.path.isdir(str(row.get("installPath") or "")),
                    },
                    sort_keys=True,
                )
                for row in rows
            )
            for pid, rows in sorted(installed.items())
        }
        return {
            "absent": [k for k, v in sorted(loaded.items()) if v is None],
            "marketplaces": marketplaces,
            "plugins": plugin_rows,
        }
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        log(f"could not read the plugin registry, replaying: {exc}")
        return None


def _digest(inputs: dict, registry: Optional[dict]) -> Optional[str]:
    if registry is None:
        return None
    try:
        blob = json.dumps({"inputs": inputs, "registry": registry}, sort_keys=True)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
