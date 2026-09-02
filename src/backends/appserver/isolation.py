"""Per-user runtime/workspace isolation for the Codex adapter (issue #173 §6).

The v1 security rule: until a pinned Codex runtime demonstrates reliable
restricted-read enforcement on the deployment platform, different users must not
share an OS-visible filesystem namespace merely because they use different Codex
threads. This adapter's C0 topology already gives each session its own
app-server process; this module adds the rest of what is enforceable in code:

* a per-user workspace directory (write confinement, via the app-server ``cwd``
  and Codex ``workspace-write`` sandbox),
* a per-user ``CODEX_HOME`` so Codex config/plugin/auth state is never a shared
  user-writable surface,
* removal of sibling-backend secrets from the child environment.

An OS/container/mount-namespace sandbox around the process (visible writable =
the user's workspace only) remains a deployment responsibility and the true
confidentiality boundary; this module makes the in-process configuration
correct and fail-closed so that sandbox has one user's roots to expose, never
several.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional

# Secrets that belong to sibling backends (or the gateway itself) and must never
# be readable by a Codex subprocess/model. Removed from the child environment
# after the os.environ merge (an override cannot unset an inherited var).
ISOLATION_ENV_REMOVE = frozenset(
    {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    }
)

_SAFE_SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")


def codex_home_base() -> Path:
    """Root under which per-user ``CODEX_HOME`` directories live.

    ``CODEX_HOME_BASE`` overrides it; otherwise a stable per-host directory so
    the state survives within a deployment but stays outside any single user's
    workspace.
    """
    base = os.getenv("CODEX_HOME_BASE")
    if base:
        return Path(base)
    import tempfile

    return Path(tempfile.gettempdir()) / "oh-my-gateway-codex-home"


def resolve_codex_home(
    user: Optional[str], session_id: Optional[str], *, create: bool = True
) -> Path:
    """Return a ``CODEX_HOME`` scoped to *user* (per-session when anonymous).

    A named user gets a stable ``<base>/<user>`` home; an anonymous session gets
    a per-session ``<base>/_session_<id>`` home so two anonymous sessions never
    share Codex state. The directory is created (mode 0700) by default.
    """
    base = codex_home_base()
    if user:
        segment = user if _SAFE_SEGMENT.match(user) else _hash_segment(user)
        home = base / segment
    else:
        sid = session_id or "anon"
        segment = sid if _SAFE_SEGMENT.match(sid) else _hash_segment(sid)
        home = base / f"_session_{segment}"
    if create:
        home.mkdir(parents=True, exist_ok=True)
        # Best-effort tighten; on platforms without chmod semantics this is a
        # no-op and the OS sandbox remains the real boundary.
        try:
            home.chmod(0o700)
        except OSError:
            pass
    return home


def _hash_segment(value: str) -> str:
    import hashlib

    return "u_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def build_isolated_env(
    *,
    auth_env: Dict[str, str],
    extra_env: Optional[Dict[str, str]],
    user: Optional[str],
    session_id: Optional[str],
    metadata_allowlist: frozenset,
) -> Dict[str, str]:
    """Compute the environment OVERRIDES for an isolated Codex subprocess.

    Combines the backend auth env with a per-user ``CODEX_HOME`` and the
    allowlisted request metadata. The returned dict is only the overrides; the
    caller pairs it with :data:`ISOLATION_ENV_REMOVE` so inherited sibling
    secrets are stripped from the child (see ``AppServerTransport(env_remove=)``).
    """
    env: Dict[str, str] = dict(auth_env)
    # A per-user CODEX_HOME always wins over any inherited/allowlisted value so
    # users can never be pointed at a shared Codex state directory.
    env["CODEX_HOME"] = str(resolve_codex_home(user, session_id))
    if extra_env:
        for key, value in extra_env.items():
            if key in metadata_allowlist and isinstance(value, str):
                env.setdefault(key, value)
    return env
