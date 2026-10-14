"""Process-wide env hygiene of tests/conftest.py (PR #244 review 4)."""

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def test_an_inherited_claude_config_dir_is_dropped(tmp_path):
    # The gateway prefers $CLAUDE_CONFIG_DIR over ~/.claude, so a shell that
    # exported a profile dir (such as ~/.claude-work) made the suite read and
    # write that real dir: 28 tests failed and one replaced its settings.json
    # symlink with a plain file. tests/fixtures/hermetic_home drops it.
    profile = tmp_path / "profile"
    profile.mkdir()
    probe = (
        "import os, runpy\n"
        "runpy.run_path('tests/conftest.py')\n"
        "print(os.environ.get('CLAUDE_CONFIG_DIR', '<unset>'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(profile)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "<unset>"
    assert list(profile.iterdir()) == []
