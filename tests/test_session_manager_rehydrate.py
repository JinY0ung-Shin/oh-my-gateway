"""Tests for jsonl rehydrate helpers in session_manager."""

from pathlib import Path

import pytest

from src.session_manager import _encode_cwd


def test_encode_cwd_replaces_slash_underscore_dot_with_dash():
    assert (
        _encode_cwd("/home/mireiffe/world/claude-code-gateway/working_dir/se91.kim")
        == "-home-mireiffe-world-claude-code-gateway-working-dir-se91-kim"
    )


def test_encode_cwd_path_object_supported():
    assert (
        _encode_cwd(Path("/x/y_z/q.r"))
        == "-x-y-z-q-r"
    )


def test_encode_cwd_handles_repeated_separators():
    assert _encode_cwd("/_./") == "----"
