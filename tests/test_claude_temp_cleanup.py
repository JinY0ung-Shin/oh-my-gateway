"""Tests for ClaudeCodeCLI temporary-workspace cleanup.

These cover ``_cleanup_temp_dir`` — the ``atexit`` handler that removes the
per-instance isolated workspace. The handler used to log unconditionally, which
produced ``ValueError: I/O operation on closed file`` tracebacks when it fired
at process exit after pytest had closed its log-capture streams (and
``sys.is_finalizing()`` is ``False`` at that point, so the timing cannot be
guarded). The cleanup is now silent and unregisters itself so repeated client
churn cannot accumulate stale callbacks.
"""

import os

from unittest.mock import patch

import src.backends.claude.client as claude_client
from src.claude_cli import ClaudeCodeCLI


def _make_cli_with_temp_dir():
    """Build a CLI with a real isolated temp workspace (no explicit cwd)."""
    cli = ClaudeCodeCLI()
    assert cli.temp_dir is not None
    assert os.path.isdir(cli.temp_dir)
    return cli


def test_cleanup_removes_temp_dir():
    cli = _make_cli_with_temp_dir()
    temp_dir = cli.temp_dir

    cli._cleanup_temp_dir()

    assert not os.path.exists(temp_dir)


def test_cleanup_is_idempotent_when_dir_already_gone():
    cli = _make_cli_with_temp_dir()
    cli._cleanup_temp_dir()

    # A second call (e.g. explicit cleanup followed by the atexit hook) must
    # not raise even though the directory no longer exists.
    cli._cleanup_temp_dir()
    assert not os.path.exists(cli.temp_dir)


def test_cleanup_never_logs():
    """Regression guard: the atexit cleanup must emit no log records, because
    it can run after the logging streams are closed (the source of the old
    'I/O operation on closed file' noise)."""
    cli = _make_cli_with_temp_dir()
    temp_dir = cli.temp_dir

    with patch.object(claude_client, "logger") as mock_logger:
        cli._cleanup_temp_dir()

    assert not os.path.exists(temp_dir)
    assert mock_logger.info.call_count == 0
    assert mock_logger.warning.call_count == 0
    assert mock_logger.error.call_count == 0


def test_cleanup_unregisters_atexit_handler():
    cli = _make_cli_with_temp_dir()

    with patch.object(claude_client.atexit, "unregister") as mock_unregister:
        cli._cleanup_temp_dir()

    mock_unregister.assert_called_once_with(cli._cleanup_temp_dir)


def test_cleanup_swallows_rmtree_failure_silently():
    cli = _make_cli_with_temp_dir()
    temp_dir = cli.temp_dir

    try:
        with (
            patch.object(claude_client.shutil, "rmtree", side_effect=OSError("boom")),
            patch.object(claude_client, "logger") as mock_logger,
        ):
            # Must not raise even though rmtree failed, and must stay silent.
            cli._cleanup_temp_dir()

        assert mock_logger.warning.call_count == 0
        assert mock_logger.error.call_count == 0
    finally:
        if os.path.exists(temp_dir):
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)
