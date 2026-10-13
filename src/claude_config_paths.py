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
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
PLUGIN_CLONE_ROOT_ENV = "CLAUDE_PLUGIN_CLONE_ROOT"

_CONFIG_DIR_NAME = ".claude"
_PLUGIN_MARKETPLACES = "plugin-marketplaces"


def _override() -> str:
    return os.environ.get(CONFIG_DIR_ENV, "").strip()


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
    This root keys off ``$HOME`` directly (not ``CLAUDE_CONFIG_DIR``), matching
    the CLI; when ``$HOME`` is unset it falls back to a temp dir so a clone still
    has somewhere to land.
    """
    configured = os.environ.get(PLUGIN_CLONE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured)
    home = os.environ.get("HOME", "").strip() or tempfile.gettempdir()
    return Path(home) / _CONFIG_DIR_NAME / _PLUGIN_MARKETPLACES
