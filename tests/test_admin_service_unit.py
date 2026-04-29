"""Unit tests for admin_service — path validation, redaction, file ops."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.admin_service import (
    MAX_FILE_SIZE,
    compute_etag,
    get_redacted_config,
    get_session_messages,
    list_workspace_files,
    read_file,
    validate_file_path,
    write_file,
)


async def test_admin_backend_health_includes_opencode_metadata(monkeypatch):
    """Backend health includes runtime metadata exposed by OpenCode."""
    from src.admin_service import get_backends_health
    from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel

    def resolve(model):
        if model == "opencode/openai/gpt-5.5":
            return ResolvedModel(model, "opencode", "openai/gpt-5.5")
        return None

    class FakeOpenCodeBackend:
        name = "opencode"

        def supported_models(self):
            return ["opencode/openai/gpt-5.5"]

        async def verify(self):
            return True

        def runtime_metadata(self):
            return {
                "mode": "managed",
                "base_url": "http://127.0.0.1:4096",
                "agent": "general",
                "models": ["opencode/openai/gpt-5.5"],
                "managed_process": True,
            }

    BackendRegistry.clear()
    BackendRegistry.register_descriptor(
        BackendDescriptor("opencode", "opencode", ["opencode/openai/gpt-5.5"], resolve)
    )
    BackendRegistry.register("opencode", FakeOpenCodeBackend())

    health = await get_backends_health()

    opencode = next(item for item in health if item["name"] == "opencode")
    assert opencode["metadata"]["mode"] == "managed"
    assert opencode["metadata"]["base_url"] == "http://127.0.0.1:4096"
    assert opencode["metadata"]["models"] == ["opencode/openai/gpt-5.5"]


@pytest.fixture
def workspace(tmp_path):
    """Create a realistic workspace with .claude directory."""
    claude_dir = tmp_path / ".claude"
    agents_dir = claude_dir / "agents"
    skills_dir = claude_dir / "skills" / "dev-server"
    agents_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)

    # Agents
    (agents_dir / "test-code.md").write_text("---\nname: test-code\n---\nAgent body")
    # Skills
    (skills_dir / "SKILL.md").write_text("---\nname: dev-server\n---\nSkill body")
    # Settings
    (claude_dir / "settings.local.json").write_text('{"permissions": {"allow": ["WebSearch"]}}')
    # CLAUDE.md at root
    (tmp_path / "CLAUDE.md").write_text("# Project instructions")
    # A file outside the allowlist (should never be accessible)
    (tmp_path / "secret.env").write_text("API_KEY=sk-secret")
    # A symlink that escapes
    (agents_dir / "escape.md").symlink_to(tmp_path / "secret.env")

    with patch("src.admin_service.get_workspace_root", return_value=tmp_path):
        yield tmp_path


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class TestValidateFilePath:
    def test_valid_agent(self, workspace):
        target = validate_file_path(".claude/agents/test-code.md")
        assert target.name == "test-code.md"

    def test_valid_skill(self, workspace):
        target = validate_file_path(".claude/skills/dev-server/SKILL.md")
        assert target.name == "SKILL.md"

    def test_valid_settings(self, workspace):
        target = validate_file_path(".claude/settings.local.json")
        assert target.name == "settings.local.json"

    def test_valid_claude_md(self, workspace):
        target = validate_file_path("CLAUDE.md")
        assert target.name == "CLAUDE.md"

    def test_reject_traversal(self, workspace):
        with pytest.raises(ValueError, match="traversal"):
            validate_file_path(".claude/agents/../../secret.env")

    def test_reject_absolute(self, workspace):
        with pytest.raises(ValueError, match="Absolute"):
            validate_file_path("/etc/passwd")

    def test_reject_outside_allowlist(self, workspace):
        with pytest.raises(ValueError, match="allowlist"):
            validate_file_path("secret.env")

    def test_reject_symlink_escape_outside_root(self, workspace):
        """Symlink pointing outside workspace root must be caught."""
        agents_dir = workspace / ".claude" / "agents"
        outside = Path(tempfile.mkdtemp()) / "evil.md"
        outside.write_text("evil")
        escape_link = agents_dir / "evil-link.md"
        escape_link.symlink_to(outside)

        with pytest.raises(ValueError, match="Symlinks are not allowed"):
            validate_file_path(".claude/agents/evil-link.md")

    def test_reject_symlink_within_workspace(self, workspace):
        """Symlink inside allowed dir pointing to non-allowed file within workspace.

        This was the allowlist-bypass bug: .claude/agents/link.md → ../../secret.env
        resolves within workspace but reads a non-allowlisted file.
        """
        agents_dir = workspace / ".claude" / "agents"
        (agents_dir / "sneaky.md").symlink_to(workspace / "secret.env")

        with pytest.raises(ValueError, match="Symlinks are not allowed"):
            validate_file_path(".claude/agents/sneaky.md")

    def test_reject_symlink_in_parent_component(self, workspace):
        """Symlink in a parent directory component must be caught."""
        # Create a symlink dir: .claude/skills/evil-skill → /tmp/somewhere
        outside_dir = Path(tempfile.mkdtemp())
        (outside_dir / "steal.md").write_text("stolen")
        fake_link = workspace / ".claude" / "skills" / "evil-skill"
        fake_link.symlink_to(outside_dir)

        with pytest.raises(ValueError, match="Symlinks"):
            validate_file_path(".claude/skills/evil-skill/steal.md")

    def test_reject_sibling_prefix_escape(self, workspace):
        """Prevent sibling-prefix bypass: /tmp/root vs /tmp/root_sibling.

        Previous bug: str.startswith('/tmp/root') matches '/tmp/root_sibling'.
        Now using Path.relative_to() which is exact.
        """
        # This test uses the real workspace fixture which is already a tmp_path,
        # so we can't easily create a sibling. Instead, we test via a symlink
        # that resolves to a sibling directory.
        agents_dir = workspace / ".claude" / "agents"
        sibling = workspace.parent / (workspace.name + "_sibling")
        sibling.mkdir(exist_ok=True)
        (sibling / "leak.md").write_text("leaked")
        (agents_dir / "sibling-link.md").symlink_to(sibling / "leak.md")

        with pytest.raises(ValueError, match="Symlinks are not allowed"):
            validate_file_path(".claude/agents/sibling-link.md")

    def test_reject_exact_file_used_as_directory(self, workspace):
        """Exact-file allowlist entries must not accept subpaths.

        Previous bug: '.claude/settings.json' prefix-matched
        '.claude/settings.json/extra.md', letting write_file create
        an arbitrary directory structure.
        """
        with pytest.raises(ValueError, match="allowlist"):
            validate_file_path(".claude/settings.json/extra.md")

    def test_reject_claude_md_used_as_directory(self, workspace):
        with pytest.raises(ValueError, match="allowlist"):
            validate_file_path("CLAUDE.md/notes.md")

    def test_reject_backslash_paths(self, workspace):
        """Backslash paths must be rejected to prevent POSIX allowlist bypass."""
        with pytest.raises(ValueError, match="Backslash"):
            validate_file_path(".claude\\agents\\test-code.md")

    def test_reject_mixed_slash_backslash(self, workspace):
        with pytest.raises(ValueError, match="Backslash"):
            validate_file_path(".claude/agents\\test-code.md")

    def test_reject_dotdot_in_components(self, workspace):
        with pytest.raises(ValueError, match="traversal"):
            validate_file_path(".claude/../.claude/agents/test-code.md")


# ---------------------------------------------------------------------------
# File read
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_read_agent(self, workspace):
        content, etag = read_file(".claude/agents/test-code.md")
        assert "test-code" in content
        assert etag  # non-empty

    def test_read_nonexistent(self, workspace):
        with pytest.raises(FileNotFoundError):
            read_file(".claude/agents/missing.md")

    def test_read_oversized(self, workspace):
        big_file = workspace / ".claude" / "agents" / "big.md"
        big_file.write_bytes(b"x" * (MAX_FILE_SIZE + 1))
        with pytest.raises(ValueError, match="too large"):
            read_file(".claude/agents/big.md")

    def test_read_disallowed_extension(self, workspace):
        bin_file = workspace / ".claude" / "agents" / "data.bin"
        bin_file.write_bytes(b"\x00\x01")
        with pytest.raises(ValueError, match="not allowed"):
            read_file(".claude/agents/data.bin")

    def test_read_invalid_utf8(self, workspace):
        bad_file = workspace / ".claude" / "agents" / "bad-utf8.md"
        bad_file.write_bytes(b"\x80\x81\x82\x83")
        with pytest.raises(ValueError, match="not valid UTF-8"):
            read_file(".claude/agents/bad-utf8.md")


# ---------------------------------------------------------------------------
# File write
# ---------------------------------------------------------------------------


class TestWriteFile:
    def test_write_and_read(self, workspace):
        new_etag = write_file(".claude/agents/test-code.md", "# Updated")
        content, etag = read_file(".claude/agents/test-code.md")
        assert content == "# Updated"
        assert etag == new_etag

    def test_write_etag_match(self, workspace):
        _, etag = read_file(".claude/agents/test-code.md")
        new_etag = write_file(".claude/agents/test-code.md", "# V2", expected_etag=etag)
        assert new_etag != etag

    def test_write_etag_mismatch(self, workspace):
        with pytest.raises(ValueError, match="ETag mismatch"):
            write_file(".claude/agents/test-code.md", "# Conflict", expected_etag="wrong")

    def test_write_json_validation(self, workspace):
        with pytest.raises(ValueError, match="Invalid JSON"):
            write_file(".claude/settings.local.json", "{bad json")

    def test_write_valid_json(self, workspace):
        new_etag = write_file(
            ".claude/settings.local.json", json.dumps({"permissions": {"allow": []}})
        )
        assert new_etag

    def test_write_oversized(self, workspace):
        with pytest.raises(ValueError, match="too large"):
            write_file(".claude/agents/test-code.md", "x" * (MAX_FILE_SIZE + 1))

    def test_write_creates_parent_dirs(self, workspace):
        new_etag = write_file(".claude/skills/new-skill/SKILL.md", "# New Skill")
        assert new_etag
        content, _ = read_file(".claude/skills/new-skill/SKILL.md")
        assert "New Skill" in content

    def test_write_failure_cleans_up_temp(self, workspace):
        """os.replace failure must not leave .tmp files or double-close fd."""
        import glob

        target_dir = workspace / ".claude" / "agents"
        before_tmps = set(glob.glob(str(target_dir / "*.tmp")))

        with patch("src.admin_service.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                write_file(".claude/agents/test-code.md", "# fail")

        after_tmps = set(glob.glob(str(target_dir / "*.tmp")))
        # No new temp files should remain
        assert after_tmps == before_tmps


# ---------------------------------------------------------------------------
# File listing
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_lists_allowed_files(self, workspace):
        files = list_workspace_files()
        paths = [f["path"] for f in files]
        assert ".claude/agents/test-code.md" in paths
        assert ".claude/skills/dev-server/SKILL.md" in paths
        assert ".claude/settings.local.json" in paths
        assert "CLAUDE.md" in paths

    def test_excludes_disallowed(self, workspace):
        files = list_workspace_files()
        paths = [f["path"] for f in files]
        assert "secret.env" not in paths

    def test_excludes_symlinks_from_listing(self, workspace):
        """Symlinks inside allowed dirs should not appear in file listings."""
        files = list_workspace_files()
        paths = [f["path"] for f in files]
        # escape.md is a symlink created in the fixture
        assert ".claude/agents/escape.md" not in paths

    def test_excludes_symlinked_allowlist_root(self, workspace):
        """A symlinked allowlist root dir should not be enumerated."""
        import shutil

        # Replace .claude/commands/ with a symlink to an external dir
        commands_dir = workspace / ".claude" / "commands"
        commands_dir.mkdir(exist_ok=True)
        external = Path(tempfile.mkdtemp())
        (external / "leaked.md").write_text("external secret")
        shutil.rmtree(commands_dir)
        commands_dir.symlink_to(external)

        files = list_workspace_files()
        paths = [f["path"] for f in files]
        assert not any("leaked" in p for p in paths)

    def test_excludes_symlinked_ancestor(self, workspace):
        """If .claude itself is a symlink, no entries should be listed."""
        import shutil

        external = Path(tempfile.mkdtemp())
        ext_agents = external / "agents"
        ext_agents.mkdir()
        (ext_agents / "external-agent.md").write_text("external")

        claude_dir = workspace / ".claude"
        shutil.rmtree(claude_dir)
        claude_dir.symlink_to(external)

        files = list_workspace_files()
        paths = [f["path"] for f in files]
        # Only CLAUDE.md (at root, not under .claude) should remain
        assert not any("external-agent" in p for p in paths)
        assert not any(p.startswith(".claude/") for p in paths)


# ---------------------------------------------------------------------------
# ETag
# ---------------------------------------------------------------------------


class TestETag:
    def test_deterministic(self):
        assert compute_etag(b"hello") == compute_etag(b"hello")

    def test_different_content(self):
        assert compute_etag(b"hello") != compute_etag(b"world")


# ---------------------------------------------------------------------------
# Config redaction
# ---------------------------------------------------------------------------


class TestRedactedConfig:
    def test_secrets_redacted(self):
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "sk-secret", "API_KEY": "my-key"}):
            config = get_redacted_config()
            env = config["environment"]
            assert env["ANTHROPIC_AUTH_TOKEN"] == "***REDACTED***"
            assert env["API_KEY"] == "***REDACTED***"

    def test_mcp_config_redacted(self):
        mcp_config = '{"servers":{"demo":{"headers":{"Authorization":"Bearer secret-token"}}}}'
        with patch.dict(os.environ, {"MCP_CONFIG": mcp_config}):
            config = get_redacted_config()
            env = config["environment"]
            assert env["MCP_CONFIG"] == "***REDACTED***"

    def test_non_secrets_visible(self):
        config = get_redacted_config()
        assert "runtime" in config
        assert "rate_limits" in config
        assert config["runtime"]["default_model"]  # should have a value


# ---------------------------------------------------------------------------
# Session message history
# ---------------------------------------------------------------------------


class TestGetSessionMessages:
    def test_nonexistent_session(self):
        """Nonexistent session returns None."""
        result = get_session_messages("nonexistent-id")
        assert result is None

    def test_returns_messages(self):
        """Messages are returned from a real session."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-001"
        try:
            session = session_manager.get_or_create_session(sid)
            session.add_messages(
                [
                    Message(role="user", content="Hello"),
                    Message(role="assistant", content="Hi there!"),
                ]
            )

            result = get_session_messages(sid)
            assert result is not None
            assert len(result) == 2
            assert result[0]["role"] == "user"
            assert result[0]["content"] == "Hello"
            assert result[0]["index"] == 0
            assert result[1]["role"] == "assistant"
            assert result[1]["content"] == "Hi there!"
            assert result[1]["index"] == 1
        finally:
            session_manager.delete_session(sid)

    def test_truncation(self):
        """Long messages are truncated."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-002"
        try:
            session = session_manager.get_or_create_session(sid)
            long_msg = "x" * 1000
            session.add_messages([Message(role="user", content=long_msg)])

            result = get_session_messages(sid, truncate=100)
            assert result is not None
            assert len(result[0]["content"]) == 100
            assert result[0]["truncated"] is True

            # No truncation
            result_full = get_session_messages(sid, truncate=0)
            assert len(result_full[0]["content"]) == 1000
            assert result_full[0]["truncated"] is False
        finally:
            session_manager.delete_session(sid)

    def test_no_ttl_refresh(self):
        """peek_session does not refresh TTL."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-003"
        try:
            session = session_manager.get_or_create_session(sid)
            session.add_messages([Message(role="user", content="test")])
            original_last_accessed = session.last_accessed

            # Small delay to detect TTL change
            import time

            time.sleep(0.01)

            get_session_messages(sid)
            # peek_session should NOT have changed last_accessed
            assert session.last_accessed == original_last_accessed
        finally:
            session_manager.delete_session(sid)

    def test_string_content(self):
        """String content is displayed correctly."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-004"
        try:
            session = session_manager.get_or_create_session(sid)
            session.messages.append(
                Message(role="user", content="Look at this image description")
            )

            result = get_session_messages(sid)
            assert result is not None
            assert "Look at this image description" in result[0]["content"]
        finally:
            session_manager.delete_session(sid)
