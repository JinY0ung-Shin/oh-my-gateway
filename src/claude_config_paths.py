"""One resolver for the Claude config dir and plugin roots (issue #241, PR-B).

Before this, the ``.claude`` config dir was resolved six different ways with
inconsistent precedence: only ``plugin_service`` honored ``CLAUDE_CONFIG_DIR``,
``_claude_home``/``settings_path`` read ``$HOME`` strictly (``None`` when unset),
and ``session_manager`` froze ``~/.claude/projects`` at **import** time. That
divergence is a latent bug (consumers disagree once the config dir moves) and
blocks the per-user HOME split (PR-C), where every consumer must follow the same
per-session config dir.

This module centralizes the precedence the CLI itself uses —
``$CLAUDE_CONFIG_DIR`` else ``~/.claude`` — so every gateway consumer reads the
same dir the spawned CLI will. It is **behavior-preserving** for the shipped
config (``CLAUDE_CONFIG_DIR`` unset and ``$HOME`` set, so every variant already
resolves to ``$HOME/.claude``); the only observable change is in edge cases the
old code handled inconsistently (``CLAUDE_CONFIG_DIR`` set, or ``$HOME`` unset),
which now align on the CLI's rule. Two variants differ only in the ``$HOME``
contract:

* :func:`config_dir` falls back to ``Path.home()`` (the passwd database) when
  ``$HOME`` is unset — for consumers that want a best-effort path;
* :func:`config_dir_strict` returns ``None`` when ``$HOME`` is unset and no
  override is set — for consumers that must deny rather than infer a home
  (the ``~/.claude`` guard, issue #115; the settings writer).

PR-C turns per-session isolation on by pointing ``CLAUDE_CONFIG_DIR`` (or the
equivalent) per session; because every consumer now routes through here, that is
a change in one place rather than six.

uid mode (``docs/isolation-uid-ownership-design.md`` §4) gives every session
uid its own config dir ``P = <CLAUDE_ISOLATION_CONFIG_BASE>/<uid>``, whose
shared assets are links into the gateway's own config dir (the shared tree
``S``). :class:`ConfigLayout` names both for one spawn; :func:`layout` builds
it. Every other mode keeps one config dir for everything (``P == S``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
PLUGIN_CLONE_ROOT_ENV = "CLAUDE_PLUGIN_CLONE_ROOT"
# Where operators keep local marketplace sources registered in place (#258,
# #260): the compose file mounts them at /data/plugins and sets this.
PLUGIN_SOURCES_DIR_ENV = "PLUGIN_SOURCES_DIR"
# uid mode only: where the per-uid config dirs live (``<base>/<uid>``).
CONFIG_BASE_ENV = "CLAUDE_ISOLATION_CONFIG_BASE"
# uid mode only: the config dir whose ``projects/<name>`` a named user's first
# uid-mode spawn copies into its own config dir. Unset: the shared config dir
# (where the CLI kept every transcript before); ``off`` turns the copy off.
MIGRATE_FROM_ENV = "CLAUDE_ISOLATION_MIGRATE_FROM"
_MIGRATE_OFF = frozenset({"off", "none", "false", "0", "no"})

_CONFIG_DIR_NAME = ".claude"
_GLOBAL_CONFIG_NAME = ".claude.json"
_PLUGIN_MARKETPLACES = "plugin-marketplaces"


def _override() -> str:
    return os.environ.get(CONFIG_DIR_ENV, "").strip()


def _home() -> Path:
    """``$HOME`` when set, else the passwd home — Node's ``os.homedir()`` rule.

    Never a shared temp dir: a path derived from here may be created or read
    on behalf of every session, so it must not land somewhere another uid can
    pre-create (#241 review).
    """
    home = (os.getenv("HOME") or "").strip()
    if home:
        return Path(home)
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


def config_dir() -> Path:
    """The Claude config dir: ``$CLAUDE_CONFIG_DIR`` else ``~/.claude``.

    When ``$HOME`` is unset, ``Path.home()`` falls back to the passwd database
    (never ``None``) — matching the old ``session_manager``/``plugin_service``/
    ``agent_catalog`` behavior. Use :func:`config_dir_strict` where ``None`` is
    required instead.
    """
    override = _override()
    return Path(override) if override else Path.home() / _CONFIG_DIR_NAME


def config_dir_strict() -> Optional[Path]:
    """The Claude config dir, or ``None`` when ``$HOME`` is unset and unoverridden.

    ``$CLAUDE_CONFIG_DIR`` still wins when set. Otherwise ``$HOME`` must be set:
    returning ``None`` lets the caller deny (the ``~/.claude`` guard) or warn
    (the settings writer) rather than infer a home from the passwd database.
    """
    override = _override()
    if override:
        return Path(override)
    home = (os.getenv("HOME") or "").strip()
    return (Path(home) / _CONFIG_DIR_NAME) if home else None


def projects_root() -> Path:
    """``<config dir>/projects`` — where the CLI stores per-session transcripts."""
    return config_dir() / "projects"


def global_config_file() -> Path:
    """The CLI's global config file (``.claude.json``), located as the CLI does.

    CLI 2.1.283 reads ``join(CLAUDE_CONFIG_DIR || homedir(), ".claude.json")``:
    with ``$CLAUDE_CONFIG_DIR`` set the file lives *inside* that dir, otherwise
    in the home dir beside ``~/.claude`` — never ``<config dir>.parent`` (wrong
    for an override or a symlinked ``~/.claude``). Returned unresolved.
    """
    override = _override()
    base = Path(override) if override else _home()
    return base / _GLOBAL_CONFIG_NAME


def settings_path_default() -> Optional[Path]:
    """``<config dir>/settings.json`` with the strict ``$HOME`` contract.

    The settings writer prefers its own ``GATEWAY_CLAUDE_SETTINGS_PATH`` override
    (handled in ``claude_settings_env``); this is the fallback base.
    """
    base = config_dir_strict()
    return (base / "settings.json") if base else None


def plugin_clone_root() -> Path:
    """Where remote marketplace repos are cloned.

    ``$CLAUDE_PLUGIN_CLONE_ROOT`` else ``<home>/.claude/plugin-marketplaces``.
    This root keys off the home dir directly (not ``CLAUDE_CONFIG_DIR``),
    matching the CLI; when ``$HOME`` is unset it falls back to the passwd home
    like the CLI's ``os.homedir()`` — never the shared temp dir, where another
    uid could pre-create the clone root the gateway then trusts.
    """
    configured = os.environ.get(PLUGIN_CLONE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured)
    return _home() / _CONFIG_DIR_NAME / _PLUGIN_MARKETPLACES


def plugin_sources_dir() -> Optional[Path]:
    """The local marketplace source root (``$PLUGIN_SOURCES_DIR``), or ``None``
    when unset, relative, or resolving to ``/`` (which would admit every
    path)."""
    configured = os.environ.get(PLUGIN_SOURCES_DIR_ENV, "").strip()
    if not configured or not os.path.isabs(configured):
        return None
    if os.path.realpath(configured) == "/":
        return None
    return Path(configured)


@dataclass(frozen=True)
class ConfigLayout:
    """Where one spawn's CLI keeps its state and finds the shared assets.

    * ``config_dir`` — the CLI child's ``CLAUDE_CONFIG_DIR``: the per-uid dir
      ``P`` in uid mode, else the gateway's own config dir;
    * ``shared_dir`` — where the shared assets (skills, plugins, agents, …,
      ``settings.json``) physically live: the gateway's own config dir ``S``.
      ``P`` links them in, so the two differ only for a per-uid layout;
    * ``uid`` — the session uid of a per-uid layout, ``None`` otherwise.

    Paths are as configured, not resolved.
    """

    config_dir: Path
    shared_dir: Path
    uid: Optional[int] = None

    @property
    def per_uid(self) -> bool:
        return self.uid is not None

    @property
    def projects(self) -> Path:
        """``<config dir>/projects``: this layout's transcripts."""
        return self.config_dir / "projects"


def config_base() -> Optional[Path]:
    """``$CLAUDE_ISOLATION_CONFIG_BASE`` as an absolute path, else ``None``.

    The startup check refuses a relative value in uid mode (it would resolve
    against the gateway's cwd, the app root); here it is only normalized.
    """
    raw = os.environ.get(CONFIG_BASE_ENV, "").strip()
    return Path(os.path.abspath(raw)) if raw else None


def user_config_dir(uid: int) -> Optional[Path]:
    """The per-uid config dir ``<config base>/<uid>``, or ``None`` without a base."""
    base = config_base()
    return None if base is None else base / str(int(uid))


def layout(uid: Optional[int] = None) -> ConfigLayout:
    """The config layout of a spawn as *uid* (``None``: not a per-uid spawn).

    A per-uid layout needs a config base; without one this is the shared
    layout (uid mode's startup check refuses that configuration).
    """
    shared = config_dir()
    if uid is not None:
        per_uid = user_config_dir(uid)
        if per_uid is not None:
            return ConfigLayout(per_uid, shared, int(uid))
    return ConfigLayout(shared, shared, None)


def legacy_config_dir() -> Optional[Path]:
    """The config dir a named user's legacy transcripts are migrated from.

    ``$CLAUDE_ISOLATION_MIGRATE_FROM`` when set (absolute), the shared config
    dir when unset, ``None`` when it says ``off``.
    """
    raw = os.environ.get(MIGRATE_FROM_ENV)
    if raw is None:
        return config_dir()
    raw = raw.strip()
    if not raw or raw.lower() in _MIGRATE_OFF:
        return None
    return Path(os.path.abspath(raw))
