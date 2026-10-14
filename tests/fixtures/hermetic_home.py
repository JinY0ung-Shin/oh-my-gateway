"""Point the test run at a throwaway HOME before any ``src`` import.

Every config-dir consumer resolves ``$CLAUDE_CONFIG_DIR`` else
``$HOME/.claude`` at call time (``src/claude_config_paths.py``), and so does
every subprocess a test spawns. Left alone, rehydrate tests left
``projects/<cwd>`` entries in the developer's real ``~/.claude``, the plugin
installer script installed and enabled test plugins in an exported
``CLAUDE_CONFIG_DIR``, and preflights ran with the real profile.

Importing this module (``tests/conftest.py`` does, before ``src``) unsets
``CLAUDE_CONFIG_DIR`` and moves HOME to a temp dir removed at exit. Tests that
need a specific layout still monkeypatch HOME / CLAUDE_CONFIG_DIR themselves.
``REAL_HOME`` / ``REAL_CLAUDE_CONFIG_DIR`` keep the originals for the guard in
``conftest._guard_real_claude_home``. The module runs once per process, so a
re-import cannot mistake the temp HOME for the real one.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

REAL_HOME = os.environ.get("HOME")
REAL_CLAUDE_CONFIG_DIR = os.environ.pop("CLAUDE_CONFIG_DIR", None)
TEST_HOME = tempfile.mkdtemp(prefix="gateway-test-home-")
os.environ["HOME"] = TEST_HOME
atexit.register(shutil.rmtree, TEST_HOME, True)


def real_claude_dir() -> Optional[Path]:
    """The config dir the developer's own CLI uses."""
    if REAL_CLAUDE_CONFIG_DIR:
        return Path(REAL_CLAUDE_CONFIG_DIR)
    if REAL_HOME:
        return Path(REAL_HOME) / ".claude"
    return None
