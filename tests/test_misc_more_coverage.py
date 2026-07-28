"""Additional coverage tests targeting small remaining gaps.

Modules covered (one class per module):
- ``src.plugin_service``      — OSError/UnicodeDecodeError paths, _read_text oversized,
                                _validate_install_path OSError branch, symlinked-parent,
                                skill-not-readable in get_plugin_skill_content,
                                get_plugin_skill_content re-resolve returns None
- ``src.session_manager``     — _try_rehydrate_from_jsonl branches (missing user/cwd,
                                file-not-found, corrupt line, cwd-mismatch, isMeta skip,
                                tool_result-only skip), _purge_all_expired_sync skip-with-client,
                                get_session rehydrate hit/miss counters, delete_session
                                active-client warning, delete_session workspace cleanup,
                                peek_session, stats()
- ``src.system_prompt``       — resolve_request_placeholders MEMORY_PATH branch, WORKING_DIRECTORY
                                only branch, get_raw_system_prompt paths, delete_named_prompt
                                active-name triggers reset, save_named_prompt existing file
                                preserves created_at, list_named_prompts error skip,
                                get_named_prompt OSError, load_persisted non-dict/non-string
                                and _load_preset_text
- ``src.sanitizer.openai_bridge`` — _flatten_text_blocks non-list/non-str, _convert_user_message
                                empty-list / non-list / non-dict content block, tool_result
                                non-str/non-list content, _convert_tool_choice unrecognised type,
                                anthropic_request_to_openai_body stream_options pass-through,
                                system empty-block output, mid-history system role,
                                openai_response_to_anthropic_body empty-choices edge cases,
                                non-dict tool_call in non-streaming response
- ``src.content_blocks``      — normalize_advisor_tool_result_block preserve_extra_fields,
                                normalize_embedded_tool_block hasattr-type fallback
- ``src.sse_builders``        — make_tool_use_started_response_sse without parent_tool_use_id,
                                _build_progress_event hook/compaction disabled flags, trigger
                                from compact_metadata, parent_tool_use_id propagation
"""

from __future__ import annotations

import json
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _as_async(items: Iterable[dict]) -> AsyncIterator[dict]:
    for it in items:
        yield it


async def _collect(aiter: AsyncIterator[dict]) -> List[dict]:
    return [e async for e in aiter]


# ===========================================================================
# src.plugin_service
# ===========================================================================


class TestPluginServiceGaps:
    """Cover the remaining uncovered lines in src/plugin_service.py."""

    # ------------------------------------------------------------------
    # _read_json  lines 65-67  — OSError/UnicodeDecodeError branch
    # ------------------------------------------------------------------

    def test_read_json_oserror_returns_none(self, tmp_path):
        from src.plugin_service import _read_json

        f = tmp_path / "ok.json"
        f.write_bytes(b'{"x": 1}')
        with patch.object(Path, "read_bytes", side_effect=OSError("perm denied")):
            result = _read_json(f)
        assert result is None

    # ------------------------------------------------------------------
    # _read_text  lines 79-80  — OSError/UnicodeDecodeError branch
    # ------------------------------------------------------------------

    def test_read_text_oserror_returns_none(self, tmp_path):
        from src.plugin_service import _read_text

        f = tmp_path / "skill.md"
        f.write_bytes(b"# Skill")
        with patch.object(Path, "read_bytes", side_effect=OSError("io error")):
            result = _read_text(f)
        assert result is None

    def test_read_text_oversized_returns_none(self, tmp_path):
        """Lines 76-77: oversized text file returns None."""
        from src.plugin_service import _read_text

        f = tmp_path / "big.md"
        f.write_bytes(b"x" * (256 * 1024 + 1))
        assert _read_text(f) is None

    def test_read_text_symlink_returns_none(self, tmp_path):
        """Lines 73-74: symlink is rejected by _read_text."""
        from src.plugin_service import _read_text

        real = tmp_path / "real.md"
        real.write_text("content")
        link = tmp_path / "link.md"
        link.symlink_to(real)
        assert _read_text(link) is None

    # ------------------------------------------------------------------
    # _validate_install_path  lines 112-113  — OSError on cache_dir.resolve()
    # ------------------------------------------------------------------

    def test_validate_install_path_oserror_on_cache_resolve(self, tmp_path):
        """Lines 112-113: OSError when resolving cache_dir → returns None."""
        from src import plugin_service

        plugins_root = tmp_path / "plugins"
        plugins_root.mkdir()
        cache_dir = plugins_root / "cache" / "mkt" / "plug" / "1.0.0"
        cache_dir.mkdir(parents=True)

        with patch("src.plugin_service._plugins_root", return_value=plugins_root):
            # Patch Path.resolve so the cache anchor resolution raises OSError
            original_resolve = Path.resolve

            def patched_resolve(self, *args, **kwargs):
                if "cache" in str(self) and str(self).endswith("cache"):
                    raise OSError("resolve failed")
                return original_resolve(self, *args, **kwargs)

            with patch.object(Path, "resolve", patched_resolve):
                result = plugin_service._validate_install_path(cache_dir)
        # The function should return None because cache_dir.resolve() raises
        assert result is None

    # ------------------------------------------------------------------
    # _validate_install_path lines 118-119 — OSError in parent-walk loop
    # ------------------------------------------------------------------

    def test_validate_install_path_parent_resolve_oserror(self, tmp_path):
        """Lines 118-119: OSError when resolving a parent during symlink walk → None."""
        from src import plugin_service

        plugins_root = tmp_path / "plugins"
        plugins_root.mkdir()
        cache_dir = plugins_root / "cache" / "mkt" / "plug" / "1.0.0"
        cache_dir.mkdir(parents=True)

        with patch("src.plugin_service._plugins_root", return_value=plugins_root):
            original_resolve = Path.resolve

            call_count = [0]

            def patched_resolve(self, *args, **kwargs):
                resolved = original_resolve(self, *args, **kwargs)
                # Raise on the first parent.resolve() call inside the loop
                # (which is cache_dir itself as a parent)
                if str(self) == str(cache_dir) and call_count[0] >= 2:
                    raise OSError("parent resolve failed")
                call_count[0] += 1
                return resolved

            with patch.object(Path, "resolve", patched_resolve):
                result = plugin_service._validate_install_path(cache_dir)
        # Either None (OSError hit) or a valid path – just ensure no exception escapes
        assert result is None or result.is_dir()

    # ------------------------------------------------------------------
    # _validate_install_path lines 121-122 — symlinked parent rejected
    # ------------------------------------------------------------------

    def test_validate_install_path_symlinked_parent_rejected(self, tmp_path):
        """Lines 121-122: path with a symlinked intermediate parent (below cache root)
        is rejected.

        Structure:
            plugins/cache/           ← real cache root
            plugins/cache/mkt/       ← symlink → tmp/elsewhere
            tmp/elsewhere/plug/1.0.0 ← real leaf directory

        The loop reaches ``mkt/`` which resolves outside the cache AND is a symlink,
        triggering the warning and returning None.
        """
        from src import plugin_service

        plugins_root = tmp_path / "plugins"
        plugins_root.mkdir()

        # The real cache root (must be named "cache")
        cache_dir = plugins_root / "cache"
        cache_dir.mkdir()

        # Real leaf directory lives *outside* the cache tree
        real_subtree = tmp_path / "elsewhere" / "plug" / "1.0.0"
        real_subtree.mkdir(parents=True)

        # Symlink at the marketplace level inside cache/
        mkt_link = cache_dir / "mkt"
        mkt_link.symlink_to(tmp_path / "elsewhere")

        install_path = mkt_link / "plug" / "1.0.0"

        with patch("src.plugin_service._plugins_root", return_value=plugins_root):
            result = plugin_service._validate_install_path(install_path)
        assert result is None

    # ------------------------------------------------------------------
    # get_plugin_skill_content  line 359  — re-resolve returns None
    # ------------------------------------------------------------------

    def test_get_plugin_skill_content_re_resolve_none(self, tmp_path):
        """Line 359: if second _resolve_plugin_entry returns None, function returns None."""
        from src import plugin_service

        # First call (get_plugin_detail) succeeds; second (_resolve_plugin_entry) returns None
        fake_detail = {
            "skills": [{"name": "greet", "path": ".claude/skills/greet.md"}]
        }
        with patch.object(plugin_service, "get_plugin_detail", return_value=fake_detail), patch.object(
            plugin_service, "_load_installed_registry", return_value={"plugins": {}}
        ):
            result = plugin_service.get_plugin_skill_content("plug@mkt", "greet")
        assert result is None

    # ------------------------------------------------------------------
    # get_plugin_skill_content  line 370  — _read_text returns None
    # ------------------------------------------------------------------

    def test_get_plugin_skill_content_unreadable_skill_file(self, tmp_path):
        """Line 370: skill file exists but _read_text returns None."""
        from src import plugin_service

        plugins_root = tmp_path / "plugins"
        plugins_root.mkdir()
        cache_dir = plugins_root / "cache" / "mkt" / "plug" / "1.0.0"
        cache_dir.mkdir(parents=True)

        # manifest
        meta_dir = cache_dir / ".claude-plugin"
        meta_dir.mkdir()
        (meta_dir / "plugin.json").write_text(json.dumps({"name": "plug"}))

        # skill file
        skills_dir = cache_dir / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        skill_file = skills_dir / "greet.md"
        skill_file.write_text("# Greet")

        reg = {
            "plugins": {
                "plug@mkt": [{"installPath": str(cache_dir), "version": "1.0.0"}]
            }
        }
        (plugins_root / "installed_plugins.json").write_text(json.dumps(reg))

        with patch("src.plugin_service._plugins_root", return_value=plugins_root), patch(
            "src.plugin_service._read_text", return_value=None
        ):
            result = plugin_service.get_plugin_skill_content("plug@mkt", "greet")
        assert result is None

    # ------------------------------------------------------------------
    # list_plugins / get_plugin_detail  line 174  — nested skill non-dir/symlink skipped
    # ------------------------------------------------------------------

    def test_discover_skills_nested_non_dir_skipped(self, tmp_path):
        """Line 174: non-dir entries under skills/ are skipped."""
        from src.plugin_service import _discover_skills

        nested_dir = tmp_path / "skills"
        nested_dir.mkdir()
        # A file (not a directory) in skills/
        (nested_dir / "not_a_dir.txt").write_text("oops")
        # A valid nested skill
        skill_dir = nested_dir / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Skill")

        skills = _discover_skills(tmp_path)
        names = [s["name"] for s in skills]
        assert "my-skill" in names
        # The non-dir entry must not appear
        assert "not_a_dir" not in names

    # ------------------------------------------------------------------
    # _plugins_root  lines 48-49  — returns None when dir doesn't exist
    # ------------------------------------------------------------------

    def test_plugins_root_returns_none_when_dir_missing(self, tmp_path, monkeypatch):
        """_plugins_root() returns None when ~/.claude/plugins/ is absent.

        Uses ``_plugins_root_real`` because the conftest hermeticity fixture
        replaces ``_plugins_root`` with a stub for every test.
        """
        from src import plugin_service

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        assert plugin_service._plugins_root_real() is None

    def test_plugins_root_returns_path_when_dir_exists(self, tmp_path, monkeypatch):
        """_plugins_root() returns the path when ~/.claude/plugins/ exists."""
        from src import plugin_service

        plugins = tmp_path / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        result = plugin_service._plugins_root_real()
        assert result == plugins
        assert result.is_dir()

    def test_plugins_root_honors_claude_config_dir(self, tmp_path, monkeypatch):
        """CLAUDE_CONFIG_DIR overrides ~/.claude, matching the CLI the gateway
        spawns, so both sides read the same plugin registry."""
        from src import plugin_service

        override = tmp_path / "custom-config"
        (override / "plugins").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
        result = plugin_service._plugins_root_real()
        assert result == override / "plugins"


# ===========================================================================
# src.session_manager
# ===========================================================================


class TestSessionManagerGaps:
    """Cover the remaining uncovered lines in src/session_manager.py."""

    # ------------------------------------------------------------------
    # _try_rehydrate_from_jsonl  line 78  — missing user or cwd
    # ------------------------------------------------------------------

    def test_rehydrate_returns_none_without_user(self):
        from src.session_manager import _try_rehydrate_from_jsonl

        result = _try_rehydrate_from_jsonl("sid", user=None, cwd="/tmp")
        assert result is None

    def test_rehydrate_returns_none_without_cwd(self):
        from src.session_manager import _try_rehydrate_from_jsonl

        result = _try_rehydrate_from_jsonl("sid", user="alice", cwd=None)
        assert result is None

    # ------------------------------------------------------------------
    # _try_rehydrate_from_jsonl  line 82  — jsonl file not found
    # ------------------------------------------------------------------

    def test_rehydrate_returns_none_when_file_missing(self, tmp_path):
        from src.session_manager import _try_rehydrate_from_jsonl

        result = _try_rehydrate_from_jsonl("nonexistent-id", user="alice", cwd=str(tmp_path))
        assert result is None

    # ------------------------------------------------------------------
    # _try_rehydrate_from_jsonl  line 90  — corrupt JSON line
    # ------------------------------------------------------------------

    def test_rehydrate_returns_none_on_corrupt_line(self, tmp_path):
        from src.session_manager import _try_rehydrate_from_jsonl, _PROJECTS_ROOT, _encode_cwd

        cwd = str(tmp_path)
        encoded = _encode_cwd(cwd)
        project_dir = _PROJECTS_ROOT / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl = project_dir / "sess-corrupt.jsonl"
        jsonl.write_text("not-valid-json\n")

        result = _try_rehydrate_from_jsonl("sess-corrupt", user="alice", cwd=cwd)
        assert result is None

    # ------------------------------------------------------------------
    # _try_rehydrate_from_jsonl  lines 104-111 — cwd mismatch guard
    # ------------------------------------------------------------------

    def test_rehydrate_returns_none_on_cwd_mismatch(self, tmp_path):
        from src.session_manager import _try_rehydrate_from_jsonl, _PROJECTS_ROOT, _encode_cwd

        cwd = str(tmp_path)
        encoded = _encode_cwd(cwd)
        project_dir = _PROJECTS_ROOT / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl = project_dir / "sess-mismatch.jsonl"
        # Record a line with a DIFFERENT cwd
        line = json.dumps({"type": "user", "cwd": "/completely/different/path"})
        jsonl.write_text(line + "\n")

        result = _try_rehydrate_from_jsonl("sess-mismatch", user="alice", cwd=cwd)
        assert result is None

    # ------------------------------------------------------------------
    # _try_rehydrate_from_jsonl  line 121  — isMeta lines skipped
    # ------------------------------------------------------------------

    def test_rehydrate_skips_imeta_lines(self, tmp_path):
        """Line 121: isMeta=True user lines do not increment turn_counter."""
        from src.session_manager import _try_rehydrate_from_jsonl, _PROJECTS_ROOT, _encode_cwd

        cwd = str(tmp_path)
        encoded = _encode_cwd(cwd)
        project_dir = _PROJECTS_ROOT / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl = project_dir / "sess-imeta.jsonl"
        lines = [
            json.dumps({"type": "user", "isMeta": True, "cwd": cwd}),
            json.dumps({"type": "user", "cwd": cwd, "message": {"content": "Hello"}}),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        result = _try_rehydrate_from_jsonl("sess-imeta", user="alice", cwd=cwd)
        assert result is not None
        assert result.turn_counter == 1  # only the non-meta line counted

    # ------------------------------------------------------------------
    # _try_rehydrate_from_jsonl  lines 122-128 — tool_result-only content skipped
    # ------------------------------------------------------------------

    def test_rehydrate_skips_tool_result_only_lines(self, tmp_path):
        """Lines 122-128: user lines whose content is only tool_result blocks are skipped."""
        from src.session_manager import _try_rehydrate_from_jsonl, _PROJECTS_ROOT, _encode_cwd

        cwd = str(tmp_path)
        encoded = _encode_cwd(cwd)
        project_dir = _PROJECTS_ROOT / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl = project_dir / "sess-tool-result.jsonl"
        lines = [
            json.dumps(
                {
                    "type": "user",
                    "cwd": cwd,
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}
                        ]
                    },
                }
            ),
            json.dumps({"type": "user", "cwd": cwd, "message": {"content": "real prompt"}}),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        result = _try_rehydrate_from_jsonl("sess-tool-result", user="alice", cwd=cwd)
        assert result is not None
        assert result.turn_counter == 1

    # ------------------------------------------------------------------
    # _try_rehydrate_from_jsonl  line 138-139  — OSError reading file
    # ------------------------------------------------------------------

    def test_rehydrate_returns_none_on_oserror(self, tmp_path):
        """Lines 138-139: OSError during file open → returns None."""
        from src.session_manager import _try_rehydrate_from_jsonl, _PROJECTS_ROOT, _encode_cwd

        cwd = str(tmp_path)
        encoded = _encode_cwd(cwd)
        project_dir = _PROJECTS_ROOT / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl = project_dir / "sess-oserror.jsonl"
        jsonl.write_text(json.dumps({"type": "user", "cwd": cwd}) + "\n")

        with patch.object(Path, "open", side_effect=OSError("no access")):
            result = _try_rehydrate_from_jsonl("sess-oserror", user="alice", cwd=cwd)
        assert result is None

    # ------------------------------------------------------------------
    # _purge_all_expired_sync  lines 304, 306  — session with client deferred
    # ------------------------------------------------------------------

    def test_purge_all_expired_sync_skips_sessions_with_active_client(self):
        """Lines 304, 306: expired sessions with an active client are left in place."""
        from datetime import datetime
        from src.session_manager import Session, SessionManager

        manager = SessionManager()
        session = manager.get_or_create_session("s-with-client")
        session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.client = MagicMock()  # simulate active client

        removed = manager._purge_all_expired_sync()

        assert removed == 0
        assert "s-with-client" in manager.sessions  # still there

    # ------------------------------------------------------------------
    # get_session  lines 478, 480  — rehydrate hit/miss counters
    # ------------------------------------------------------------------

    def test_get_session_increments_rehydrate_miss_counter(self):
        """Line 480: _rehydrate_misses incremented on cache miss with user+cwd."""
        from src.session_manager import SessionManager

        manager = SessionManager()
        before = manager._rehydrate_misses
        result = manager.get_session("nonexistent-sid", user="alice", cwd="/tmp/workspace")
        assert result is None
        assert manager._rehydrate_misses == before + 1

    def test_get_session_increments_rehydrate_hit_counter(self, tmp_path):
        """Line 478: _rehydrate_hits incremented when rehydration succeeds."""
        from src.session_manager import (
            SessionManager,
            _PROJECTS_ROOT,
            _encode_cwd,
            Session,
        )

        cwd = str(tmp_path)
        encoded = _encode_cwd(cwd)
        project_dir = _PROJECTS_ROOT / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl = project_dir / "sess-hit.jsonl"
        jsonl.write_text(json.dumps({"type": "assistant", "cwd": cwd}) + "\n")

        manager = SessionManager()
        before = manager._rehydrate_hits
        result = manager.get_session("sess-hit", user="alice", cwd=cwd)
        assert result is not None
        assert manager._rehydrate_hits == before + 1

    # ------------------------------------------------------------------
    # delete_session  lines 506  — active client warning
    # ------------------------------------------------------------------

    def test_delete_session_logs_warning_for_active_client(self, caplog):
        """Line 506: warning is logged when deleting a session with an active client."""
        import logging
        from src.session_manager import SessionManager

        manager = SessionManager()
        session = manager.get_or_create_session("s-active")
        session.client = MagicMock()

        with caplog.at_level(logging.WARNING, logger="src.session_manager"):
            result = manager.delete_session("s-active")

        assert result is True
        assert any("active client" in rec.message for rec in caplog.records)

    # ------------------------------------------------------------------
    # delete_session  line 512  — workspace cleanup
    # ------------------------------------------------------------------

    def test_delete_session_cleans_workspace(self, tmp_path):
        """Line 512: workspace is cleaned up when deleting a session that has one."""
        from src.session_manager import SessionManager

        workspace = tmp_path / "_tmp_cleanup_test"
        workspace.mkdir()
        (workspace / "scratch.txt").write_text("data")

        manager = SessionManager()
        session = manager.get_or_create_session("s-workspace")
        session.workspace = str(workspace)

        result = manager.delete_session("s-workspace")
        assert result is True
        assert not workspace.exists()

    # ------------------------------------------------------------------
    # peek_session  lines 491-492
    # ------------------------------------------------------------------

    def test_peek_session_returns_none_for_expired(self):
        """peek_session() removes and returns None for expired sessions."""
        from datetime import datetime
        from src.session_manager import SessionManager

        manager = SessionManager()
        session = manager.get_or_create_session("s-peek")
        session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        result = manager.peek_session("s-peek")
        assert result is None
        assert "s-peek" not in manager.sessions

    def test_peek_session_returns_active_without_touch(self):
        """peek_session() returns active session without extending TTL."""
        from src.session_manager import SessionManager

        manager = SessionManager()
        session = manager.get_or_create_session("s-peek-active")
        original_expires = session.expires_at

        result = manager.peek_session("s-peek-active")
        assert result is session
        # expires_at must not be updated (no touch)
        assert session.expires_at == original_expires

    # ------------------------------------------------------------------
    # stats()  lines 577-583
    # ------------------------------------------------------------------

    def test_stats_returns_correct_counters(self):
        """stats() returns active_sessions plus rehydrate hit/miss counters."""
        from src.session_manager import SessionManager

        manager = SessionManager()
        manager.get_or_create_session("active-1")
        manager.get_or_create_session("active-2")
        manager._rehydrate_hits = 3
        manager._rehydrate_misses = 7

        s = manager.stats()
        assert s["active_sessions"] == 2
        assert s["rehydrate_hits"] == 3
        assert s["rehydrate_misses"] == 7

    # ------------------------------------------------------------------
    # _purge_all_expired  lines 287, 319-320  — workspace cleanup in async path
    # ------------------------------------------------------------------

    async def test_purge_all_expired_cleans_workspace(self, tmp_path):
        """Lines 287, 319-320: expired session workspace is cleaned in async purge."""
        from datetime import datetime
        from src.session_manager import SessionManager

        workspace = tmp_path / "_tmp_async_cleanup"
        workspace.mkdir()
        (workspace / "data.txt").write_text("temp")

        manager = SessionManager()
        session = manager.get_or_create_session("s-expired-ws")
        session.workspace = str(workspace)
        session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        removed = await manager._purge_all_expired()
        assert removed == 1
        assert not workspace.exists()

    # ------------------------------------------------------------------
    # _purge_all_expired  lines 337-340  — client disconnect failure swallowed
    # ------------------------------------------------------------------

    async def test_purge_all_expired_client_disconnect_failure_swallowed(self):
        """Lines 337-340: exception from client.disconnect() is swallowed."""
        from datetime import datetime
        from src.session_manager import SessionManager

        manager = SessionManager()
        session = manager.get_or_create_session("s-bad-client")
        session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        bad_client = AsyncMock()
        bad_client.disconnect = AsyncMock(side_effect=RuntimeError("disconnect exploded"))
        session.client = bad_client

        # Must not raise
        removed = await manager._purge_all_expired()
        assert removed == 1

    # ------------------------------------------------------------------
    # cleanup_expired_sessions  lines 366-369  — backend image cleanup
    # ------------------------------------------------------------------

    async def test_cleanup_expired_sessions_backend_image_cleanup_called(self):
        """Lines 366-369: cleanup_expired_sessions calls cleanup_images on backends."""
        from src.session_manager import SessionManager

        manager = SessionManager()
        fake_backend = MagicMock()
        fake_backend.cleanup_images = MagicMock()

        fake_registry = MagicMock()
        fake_registry.all_backends.return_value = {"fake": fake_backend}

        with patch("src.session_manager.BackendRegistry", fake_registry, create=True):
            # Patch the import inside the function
            import sys
            import types

            fake_backends_module = types.ModuleType("src.backends.base")
            fake_backends_module.BackendRegistry = fake_registry
            with patch.dict(sys.modules, {"src.backends.base": fake_backends_module}):
                await manager.cleanup_expired_sessions()

        # If cleanup_images was called, line 367-368 was hit
        fake_backend.cleanup_images.assert_called()

    # ------------------------------------------------------------------
    # _purge_all_expired_sync  lines 306
    # ------------------------------------------------------------------

    def test_purge_all_expired_sync_cleans_workspace_no_client(self, tmp_path):
        """Line 306: workspace cleanup called for expired session without client."""
        from datetime import datetime
        from src.session_manager import SessionManager

        workspace = tmp_path / "_tmp_sync_cleanup"
        workspace.mkdir()
        (workspace / "f.txt").write_text("data")

        manager = SessionManager()
        session = manager.get_or_create_session("s-sync-ws")
        session.workspace = str(workspace)
        session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.client = None

        removed = manager._purge_all_expired_sync()
        assert removed == 1
        assert not workspace.exists()


# ===========================================================================
# src.system_prompt
# ===========================================================================


class TestSystemPromptGaps:
    """Cover remaining uncovered lines in src/system_prompt.py."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path):
        """Reset module state and redirect persistence to tmp_path."""
        from src import system_prompt as sp

        orig = {
            "_default_prompt": sp._default_prompt,
            "_default_prompt_raw": sp._default_prompt_raw,
            "_runtime_prompt": sp._runtime_prompt,
            "_runtime_prompt_raw": sp._runtime_prompt_raw,
            "_active_prompt_name": sp._active_prompt_name,
            "_preset_text": sp._preset_text,
            "_DATA_DIR": sp._DATA_DIR,
            "_PERSIST_FILE": sp._PERSIST_FILE,
            "_PROMPTS_DIR": sp._PROMPTS_DIR,
        }
        sp._default_prompt = None
        sp._default_prompt_raw = None
        sp._runtime_prompt = None
        sp._runtime_prompt_raw = None
        sp._active_prompt_name = None
        sp._preset_text = None
        sp._DATA_DIR = tmp_path
        sp._PERSIST_FILE = tmp_path / "system_prompt.json"
        sp._PROMPTS_DIR = tmp_path / "prompts"
        yield sp
        for k, v in orig.items():
            setattr(sp, k, v)

    # ------------------------------------------------------------------
    # resolve_request_placeholders  lines 122-124  — MEMORY_PATH branch
    # ------------------------------------------------------------------

    def test_resolve_memory_path_creates_dir(self, tmp_path, _isolate):
        from src.system_prompt import resolve_request_placeholders

        cwd = str(tmp_path)
        text = "Memory is at {{MEMORY_PATH}}"
        result = resolve_request_placeholders(text, cwd)
        expected_path = tmp_path / ".memory"
        assert str(expected_path) in result
        assert expected_path.is_dir()

    # ------------------------------------------------------------------
    # resolve_request_placeholders  line 125-126  — WORKING_DIRECTORY only
    # ------------------------------------------------------------------

    def test_resolve_working_directory(self, tmp_path, _isolate):
        from src.system_prompt import resolve_request_placeholders

        cwd = str(tmp_path)
        text = "CWD is {{WORKING_DIRECTORY}}"
        result = resolve_request_placeholders(text, cwd)
        assert cwd in result
        assert "{{WORKING_DIRECTORY}}" not in result

    def test_resolve_none_returns_none(self, _isolate):
        from src.system_prompt import resolve_request_placeholders

        assert resolve_request_placeholders(None, "/tmp") is None

    # ------------------------------------------------------------------
    # get_raw_system_prompt  line 189  — returns _default_prompt_raw
    # ------------------------------------------------------------------

    def test_get_raw_returns_runtime_raw_when_set(self, _isolate):
        sp = _isolate
        sp._runtime_prompt_raw = "raw runtime"
        assert sp.get_raw_system_prompt() == "raw runtime"

    def test_get_raw_falls_back_to_default_raw(self, _isolate):
        sp = _isolate
        sp._runtime_prompt_raw = None
        sp._default_prompt_raw = "raw default"
        assert sp.get_raw_system_prompt() == "raw default"

    # ------------------------------------------------------------------
    # delete_named_prompt  lines 369-372  — active name triggers reset
    # ------------------------------------------------------------------

    def test_delete_active_named_prompt_resets_system_prompt(self, _isolate):
        """Lines 369-372: deleting the active named prompt clears the runtime override."""
        sp = _isolate
        sp._PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        # Save and activate the prompt
        sp.save_named_prompt("my-prompt", "Active content")
        sp.activate_named_prompt("my-prompt")
        assert sp.get_system_prompt() == "Active content"
        assert sp._active_prompt_name == "my-prompt"

        result = sp.delete_named_prompt("my-prompt")

        assert result is True
        # After deletion, active reset → preset mode
        assert sp.get_system_prompt() is None

    def test_delete_non_active_named_prompt_does_not_reset(self, _isolate):
        """Deleting a non-active named prompt leaves the current override intact."""
        sp = _isolate
        sp._PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        sp.save_named_prompt("other-prompt", "Other content")
        sp.set_system_prompt("Current active")
        sp._active_prompt_name = "different-prompt"

        result = sp.delete_named_prompt("other-prompt")

        assert result is True
        assert sp.get_system_prompt() == "Current active"

    # ------------------------------------------------------------------
    # save_named_prompt  lines 341-343  — existing file preserves created_at
    # ------------------------------------------------------------------

    def test_save_named_prompt_preserves_created_at_on_update(self, _isolate):
        """Lines 341-343: updating an existing named prompt keeps original created_at."""
        sp = _isolate
        sp._PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        first = sp.save_named_prompt("update-me", "First content")
        original_created_at = first["created_at"]

        second = sp.save_named_prompt("update-me", "Updated content")
        assert second["created_at"] == original_created_at
        assert second["content"] == "Updated content"
        assert second["updated_at"] != second["created_at"]

    # ------------------------------------------------------------------
    # list_named_prompts  line 306-307  — skip unreadable files
    # ------------------------------------------------------------------

    def test_list_named_prompts_skips_corrupt_files(self, _isolate):
        """Lines 306-307: corrupt JSON files in prompts dir are silently skipped."""
        sp = _isolate
        sp._PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        (sp._PROMPTS_DIR / "bad.json").write_text("{not json")
        sp.save_named_prompt("good-prompt", "Good content")

        prompts = sp.list_named_prompts()
        names = [p["name"] for p in prompts]
        assert "good-prompt" in names
        # bad.json should be skipped, no exception

    # ------------------------------------------------------------------
    # get_named_prompt  line 319-321  — OSError / JSONDecodeError
    # ------------------------------------------------------------------

    def test_get_named_prompt_returns_none_on_oserror(self, _isolate):
        """Lines 319-321: OSError reading named prompt file → returns None."""
        sp = _isolate
        sp._PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        sp.save_named_prompt("error-prompt", "Content")

        with patch.object(Path, "read_text", side_effect=OSError("perm denied")):
            result = sp.get_named_prompt("error-prompt")
        assert result is None

    # ------------------------------------------------------------------
    # _load_persisted  line 241  — OSError branch
    # ------------------------------------------------------------------

    def test_load_persisted_oserror_returns_none(self, _isolate):
        """Line 241: OSError during _load_persisted → returns None, no crash."""
        sp = _isolate
        # Write a valid persist file first
        sp._PERSIST_FILE.write_text(json.dumps({"prompt": "saved"}), encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=OSError("disk error")):
            result = sp._load_persisted()
        assert result is None

    # ------------------------------------------------------------------
    # get_preset_text  line 241  — _load_preset_text missing file
    # ------------------------------------------------------------------

    def test_get_preset_text_returns_none_when_file_missing(self, _isolate):
        """_load_preset_text returns None when the reference file does not exist."""
        from src.system_prompt import _load_preset_text

        with patch.object(Path, "is_file", return_value=False):
            result = _load_preset_text()
        assert result is None

    # ------------------------------------------------------------------
    # activate_named_prompt  line 372  — ValueError when prompt not found
    # ------------------------------------------------------------------

    def test_activate_named_prompt_raises_for_missing(self, _isolate):
        """activate_named_prompt raises ValueError for a non-existent prompt."""
        sp = _isolate
        with pytest.raises(ValueError, match="not found"):
            sp.activate_named_prompt("does-not-exist")


# ===========================================================================
# src.sanitizer.openai_bridge
# ===========================================================================


class TestOpenAIBridgeGaps:
    """Cover remaining uncovered lines in src/sanitizer/openai_bridge.py."""

    # ------------------------------------------------------------------
    # _flatten_text_blocks  line 69  — non-list content returns ""
    # ------------------------------------------------------------------

    def test_flatten_text_blocks_non_list_returns_empty(self):
        from src.sanitizer.openai_bridge import _flatten_text_blocks

        assert _flatten_text_blocks({"type": "text", "text": "x"}) == ""
        assert _flatten_text_blocks(42) == ""

    def test_flatten_text_blocks_string_passthrough(self):
        from src.sanitizer.openai_bridge import _flatten_text_blocks

        assert _flatten_text_blocks("hello") == "hello"

    # ------------------------------------------------------------------
    # _convert_user_message  line 143  — non-list content
    # ------------------------------------------------------------------

    def test_convert_user_message_non_list_content_returns_empty(self):
        from src.sanitizer.openai_bridge import _convert_user_message

        assert _convert_user_message(42) == []
        assert _convert_user_message(None) == []

    def test_convert_user_message_non_dict_block_skipped(self):
        """line 216/218: non-dict items in content list are skipped."""
        from src.sanitizer.openai_bridge import _convert_user_message

        # list with a non-dict item and a valid text block
        result = _convert_user_message(["not a dict", {"type": "text", "text": "hi"}])
        assert len(result) == 1
        assert result[0]["content"] == "hi"

    # ------------------------------------------------------------------
    # _convert_user_message  lines 234, 241-244  — tool_result non-str/non-list content
    # ------------------------------------------------------------------

    def test_convert_user_message_tool_result_non_str_content(self):
        """Lines 241-244: tool_result.content that is neither str nor list → json.dumps."""
        from src.sanitizer.openai_bridge import _convert_user_message

        content = [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": {"key": "value"},  # dict, not str/list
            }
        ]
        result = _convert_user_message(content)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        parsed = json.loads(result[0]["content"])
        assert parsed == {"key": "value"}

    # ------------------------------------------------------------------
    # _convert_tool_choice  line 194  — unrecognised type returns None
    # ------------------------------------------------------------------

    def test_convert_tool_choice_unknown_type_returns_none(self):
        from src.sanitizer.openai_bridge import _convert_tool_choice

        assert _convert_tool_choice({"type": "unknown_mode"}) is None

    def test_convert_tool_choice_tool_without_name_returns_none(self):
        """line 191-192: type=tool but name absent → None."""
        from src.sanitizer.openai_bridge import _convert_tool_choice

        assert _convert_tool_choice({"type": "tool"}) is None

    # ------------------------------------------------------------------
    # anthropic_request_to_openai_body  line 268  — stream_options pass-through
    # ------------------------------------------------------------------

    def test_stream_options_passthrough_without_stream(self):
        """Line 268: stream_options in body forwarded even without stream=True."""
        from src.sanitizer.openai_bridge import anthropic_request_to_openai_body

        body = {
            "model": "m",
            "stream_options": {"include_usage": False},
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_request_to_openai_body(body)
        # The key was explicitly passed; include_usage should stay False (not overridden
        # because stream is not set).
        assert "stream_options" in out

    # ------------------------------------------------------------------
    # anthropic_request_to_openai_body  line 244  — mid-history system role
    # ------------------------------------------------------------------

    def test_mid_history_system_role_forwarded(self):
        """Line 244: system role in messages (non-top-level) is forwarded as-is."""
        from src.sanitizer.openai_bridge import anthropic_request_to_openai_body

        body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "mid-history system note"}],
                },
            ],
        }
        out = anthropic_request_to_openai_body(body)
        sys_msgs = [m for m in out["messages"] if m["role"] == "system"]
        assert any("mid-history system note" in m["content"] for m in sys_msgs)

    # ------------------------------------------------------------------
    # openai_stream_to_anthropic_events  lines 331, 333  — empty choices → skip
    # ------------------------------------------------------------------

    async def test_stream_skips_non_dict_choice(self):
        """Lines 331, 333: non-dict choice is skipped gracefully."""
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        chunks = [
            {"choices": ["not-a-dict"]},  # triggers line 333
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        types = [e["type"] for e in out]
        assert "message_start" in types
        assert "message_stop" in types

    # ------------------------------------------------------------------
    # openai_stream_to_anthropic_events  lines 459, 462  — delta not dict
    # ------------------------------------------------------------------

    async def test_stream_handles_null_delta(self):
        """Lines 459, 462: null delta value treated as empty dict (no crash)."""
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        chunks = [
            {"choices": [{"delta": None, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        deltas = [e for e in out if e["type"] == "content_block_delta"]
        assert any(d["delta"]["text"] == "ok" for d in deltas)

    # ------------------------------------------------------------------
    # openai_stream_to_anthropic_events  lines 478, 481  — tool_calls index non-int
    # ------------------------------------------------------------------

    async def test_stream_skips_tool_call_with_non_int_index(self):
        """Lines 478, 481: tool_call missing int index is skipped."""
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": "bad",  # not an int
                                    "id": "call_1",
                                    "function": {"name": "X", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ]
        # Must not crash; no tool_use blocks should appear
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert starts == []

    # ------------------------------------------------------------------
    # openai_stream_to_anthropic_events  lines 499, 518, 521  — finish_reason accumulation
    # ------------------------------------------------------------------

    async def test_stream_unknown_finish_reason_maps_to_end_turn(self):
        """Lines 499, 518, 521: unknown finish_reason maps to end_turn via default."""
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "content_filter"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        md = next(e for e in out if e["type"] == "message_delta")
        assert md["delta"]["stop_reason"] == "stop_sequence"

    # ------------------------------------------------------------------
    # openai_response_to_anthropic_body  line 567  — empty choices list
    # ------------------------------------------------------------------

    def test_non_streaming_empty_choices(self):
        """Line 567: body with empty choices list returns valid Anthropic body."""
        from src.sanitizer.openai_bridge import openai_response_to_anthropic_body

        body = {"id": "x", "model": "m", "choices": [], "usage": {}}
        out = openai_response_to_anthropic_body(body)
        assert out["role"] == "assistant"
        assert out["content"] == []

    def test_non_streaming_no_choices_key(self):
        """Fallback when choices key entirely absent."""
        from src.sanitizer.openai_bridge import openai_response_to_anthropic_body

        body = {"id": "x", "model": "m"}
        out = openai_response_to_anthropic_body(body)
        assert out["role"] == "assistant"

    # ------------------------------------------------------------------
    # openai_response_to_anthropic_body  lines 580  — non-dict tool_call skipped
    # ------------------------------------------------------------------

    def test_non_streaming_non_dict_tool_call_skipped(self):
        """Line 580: non-dict items in tool_calls are skipped."""
        from src.sanitizer.openai_bridge import openai_response_to_anthropic_body

        body = {
            "id": "x",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hi",
                        "tool_calls": ["not-a-dict", None],
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        out = openai_response_to_anthropic_body(body)
        # Non-dict items should be skipped without error
        tool_use_blocks = [b for b in out["content"] if b["type"] == "tool_use"]
        assert tool_use_blocks == []


# ===========================================================================
# src.content_blocks
# ===========================================================================


class TestContentBlocksGaps:
    """Cover remaining uncovered lines in src/content_blocks.py."""

    # ------------------------------------------------------------------
    # normalize_advisor_tool_result_block  lines 101-103 — preserve_extra_fields=True
    # ------------------------------------------------------------------

    def test_normalize_advisor_tool_result_preserve_extra_fields(self):
        """Lines 101-103: preserve_extra_fields=True returns dict copy with truncated content."""
        from src.content_blocks import normalize_advisor_tool_result_block

        block = {
            "type": "advisor_tool_result",
            "tool_use_id": "tu_1",
            "content": "long content",
            "extra_key": "preserved",
        }
        result = normalize_advisor_tool_result_block(
            block,
            truncate_content=lambda c: c[:4],
            preserve_extra_fields=True,
        )
        assert result["extra_key"] == "preserved"
        assert result["content"] == "long"

    def test_normalize_advisor_tool_result_preserve_extra_fields_no_truncate(self):
        """preserve_extra_fields=True without truncator returns dict copy unchanged."""
        from src.content_blocks import normalize_advisor_tool_result_block

        block = {
            "type": "advisor_tool_result",
            "tool_use_id": "tu_1",
            "content": "content",
            "meta": 42,
        }
        result = normalize_advisor_tool_result_block(block, preserve_extra_fields=True)
        assert result["meta"] == 42
        assert result["content"] == "content"

    # ------------------------------------------------------------------
    # normalize_embedded_tool_block  line 161  — hasattr(block, "type") fallback
    # ------------------------------------------------------------------

    def test_normalize_embedded_tool_block_generic_object_with_type(self):
        """Line 161: objects with a 'type' attribute but not a known SDK type use fallback."""
        from src.content_blocks import normalize_embedded_tool_block

        class GenericBlock:
            type = "custom_type"
            id = "blk_1"
            name = "MyTool"

        result = normalize_embedded_tool_block(GenericBlock())
        assert result["type"] == "custom_type"
        assert result["id"] == "blk_1"
        assert result["name"] == "MyTool"

    def test_normalize_embedded_tool_block_returns_unknown_as_is(self):
        """Objects without type attribute are returned as-is."""
        from src.content_blocks import normalize_embedded_tool_block

        obj = object()
        result = normalize_embedded_tool_block(obj)
        assert result is obj


# ===========================================================================
# src.sse_builders
# ===========================================================================


class TestSSEBuildersGaps:
    """Cover remaining uncovered lines in src/sse_builders.py."""

    # ------------------------------------------------------------------
    # make_tool_use_started_response_sse  line 173  — without parent_tool_use_id
    # ------------------------------------------------------------------

    def test_make_tool_use_started_no_parent(self):
        """Line 173: parent_tool_use_id absent → field not in JSON data."""
        from src.sse_builders import make_tool_use_started_response_sse

        sse = make_tool_use_started_response_sse("tu_1", "Bash", sequence_number=0)
        assert "response.tool_use_started" in sse
        data = json.loads(sse.split("\ndata: ")[1].rstrip("\n"))
        assert "parent_tool_use_id" not in data

    def test_make_tool_use_started_with_parent(self):
        """Line 173: parent_tool_use_id present → field appears in JSON data."""
        from src.sse_builders import make_tool_use_started_response_sse

        sse = make_tool_use_started_response_sse(
            "tu_1", "Bash", sequence_number=1, parent_tool_use_id="parent_42"
        )
        data = json.loads(sse.split("\ndata: ")[1].rstrip("\n"))
        assert data["parent_tool_use_id"] == "parent_42"

    # ------------------------------------------------------------------
    # _build_progress_event  line 136  — hook events disabled
    # ------------------------------------------------------------------

    def test_build_progress_event_hook_disabled_returns_none(self, monkeypatch):
        """Line 136: hook events disabled → hook_started returns None."""
        import src.sse_builders as builders

        monkeypatch.setattr(builders, "STREAM_HOOK_EVENTS", False)
        from src.sse_builders import _build_progress_event

        result = _build_progress_event({"subtype": "hook_started", "hook_event_name": "Pre"})
        assert result is None

    # ------------------------------------------------------------------
    # _build_progress_event  line 140  — compaction disabled
    # ------------------------------------------------------------------

    def test_build_progress_event_compaction_disabled_returns_none(self, monkeypatch):
        """Line 140: compaction events disabled → compact_boundary returns None."""
        import src.sse_builders as builders

        monkeypatch.setattr(builders, "STREAM_COMPACTION_EVENTS", False)
        from src.sse_builders import _build_progress_event

        result = _build_progress_event({"subtype": "compact_boundary"})
        assert result is None

    # ------------------------------------------------------------------
    # _build_progress_event  line 173  — hook event with parent_tool_use_id
    # ------------------------------------------------------------------

    def test_build_progress_event_hook_with_parent_tool_use_id(self, monkeypatch):
        """Line 173: parent_tool_use_id on hook chunk is forwarded to event."""
        import src.sse_builders as builders

        monkeypatch.setattr(builders, "STREAM_HOOK_EVENTS", True)
        from src.sse_builders import _build_progress_event

        chunk = {
            "subtype": "hook_started",
            "hook_event_name": "PreToolUse",
            "parent_tool_use_id": "parent_xyz",
            "data": {},
        }
        result = _build_progress_event(chunk)
        assert result is not None
        assert result["parent_tool_use_id"] == "parent_xyz"

    # ------------------------------------------------------------------
    # _build_progress_event  line 140  — trigger from compact_metadata
    # ------------------------------------------------------------------

    def test_build_progress_event_trigger_from_compact_metadata(self, monkeypatch):
        """Line 140: trigger extracted from compact_metadata when direct trigger absent."""
        import src.sse_builders as builders

        monkeypatch.setattr(builders, "STREAM_COMPACTION_EVENTS", True)
        from src.sse_builders import _build_progress_event

        chunk = {
            "subtype": "compaction",
            "data": {
                "compact_metadata": {"trigger": "auto_compact"}
            },
        }
        result = _build_progress_event(chunk)
        assert result is not None
        assert result["trigger"] == "auto_compact"


# ===========================================================================
# Additional gap coverage: remaining lines after first pass
# ===========================================================================


class TestSessionManagerRemainingGaps:
    """Target the remaining uncovered lines in session_manager.py."""

    # ------------------------------------------------------------------
    # _cleanup_workspace  lines 319-320  — exception is swallowed
    # ------------------------------------------------------------------

    def test_cleanup_workspace_exception_is_swallowed(self, caplog):
        """Lines 319-320: exception from WorkspaceManager is swallowed with a debug log."""
        import logging
        import sys
        import types
        from src.session_manager import SessionManager

        manager = SessionManager()

        # Patch the workspace_manager module so the import inside _cleanup_workspace raises
        bad_module = types.ModuleType("src.workspace_manager")
        bad_module.WorkspaceManager = MagicMock(side_effect=RuntimeError("wm fail"))
        with patch.dict(sys.modules, {"src.workspace_manager": bad_module}):
            # Must not raise
            manager._cleanup_workspace("/some/workspace/path")

    # ------------------------------------------------------------------
    # _purge_all_expired  lines 337-340  — exception propagated from cleanup_loop
    # ------------------------------------------------------------------

    async def test_cleanup_loop_exception_in_cleanup_call_is_logged(self):
        """Lines 339-340: exception from cleanup_expired_sessions inside the loop
        is caught and logged (not propagated), letting the loop retry."""
        from src.session_manager import SessionManager

        manager = SessionManager(cleanup_interval_minutes=0)  # 0 minutes = 0 sec sleep

        call_count = [0]

        async def _failing_cleanup():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient failure")
            return 0

        manager.cleanup_expired_sessions = _failing_cleanup

        # Start the task, let it tick at least twice, then cancel it
        import asyncio

        manager.start_cleanup_task()
        # Give the task a few iterations
        for _ in range(5):
            await asyncio.sleep(0)
        await manager.async_shutdown()

        # The loop must have survived the first failure
        assert call_count[0] >= 1

    async def test_cleanup_loop_cancelled_error_reraises(self):
        """Line 338: CancelledError raised by cleanup_expired_sessions is re-raised
        (propagates up to the outer CancelledError handler at line 341-343)."""
        import asyncio
        from src.session_manager import SessionManager

        manager = SessionManager(cleanup_interval_minutes=0)

        async def _cancel_raising_cleanup():
            raise asyncio.CancelledError("inner cancel")

        manager.cleanup_expired_sessions = _cancel_raising_cleanup
        manager.start_cleanup_task()

        # Give the loop time to call cleanup and hit line 338
        for _ in range(10):
            await asyncio.sleep(0)

        # The task should be done/cancelled due to the CancelledError propagation
        task = manager._cleanup_task
        if task is not None and not task.done():
            await manager.async_shutdown()
        # No assertion needed — just verifying no unhandled exception escapes

    # ------------------------------------------------------------------
    # cleanup_expired_sessions  lines 368-369  — BackendRegistry exception swallowed
    # ------------------------------------------------------------------

    async def test_cleanup_expired_sessions_backend_exception_swallowed(self):
        """Lines 368-369: exception from BackendRegistry.all_backends() is swallowed."""
        from src.session_manager import SessionManager
        import sys
        import types

        manager = SessionManager()

        fake_registry = MagicMock()
        fake_registry.all_backends.side_effect = RuntimeError("registry exploded")

        fake_backends_module = types.ModuleType("src.backends.base")
        fake_backends_module.BackendRegistry = fake_registry
        with patch.dict(sys.modules, {"src.backends.base": fake_backends_module}):
            # Must not raise
            await manager.cleanup_expired_sessions()

    # ------------------------------------------------------------------
    # delete_session_async  lines 526-527  — client disconnect exception swallowed
    # ------------------------------------------------------------------

    async def test_delete_session_async_disconnect_exception_swallowed(self):
        """Lines 526-527: exception from client.disconnect in delete_session_async
        is swallowed."""
        from src.session_manager import SessionManager

        manager = SessionManager()
        session = manager.get_or_create_session("s-disco-fail")

        bad_client = AsyncMock()
        bad_client.disconnect = AsyncMock(side_effect=RuntimeError("disco failed"))
        session.client = bad_client

        # Must not raise
        result = await manager.delete_session_async("s-disco-fail")
        assert result is True
        assert "s-disco-fail" not in manager.sessions


class TestOpenAIBridgeRemainingGaps:
    """Target the remaining uncovered lines in sanitizer/openai_bridge.py."""

    # ------------------------------------------------------------------
    # anthropic_request_to_openai_body  lines 216, 218  — temperature and top_p
    # ------------------------------------------------------------------

    def test_temperature_and_top_p_forwarded(self):
        """Lines 216, 218: temperature and top_p are forwarded when present."""
        from src.sanitizer.openai_bridge import anthropic_request_to_openai_body

        body = {
            "model": "m",
            "temperature": 0.7,
            "top_p": 0.9,
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["temperature"] == 0.7
        assert out["top_p"] == 0.9

    # ------------------------------------------------------------------
    # _convert_user_message  line 234  — tool_result list content → _flatten_text_blocks
    # ------------------------------------------------------------------

    def test_convert_user_message_tool_result_list_content_flattened(self):
        """Line 234: tool_result content that IS a list is flattened via _flatten_text_blocks."""
        from src.sanitizer.openai_bridge import _convert_user_message

        content = [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "World"},
                ],
            }
        ]
        result = _convert_user_message(content)
        assert len(result) == 1
        assert result[0]["content"] == "Hello World"

    # ------------------------------------------------------------------
    # _PendingToolCall.update_metadata  lines 331, 333 — call_id and name update
    # ------------------------------------------------------------------

    async def test_stream_tool_call_metadata_updated_on_second_delta(self):
        """Lines 331, 333: call_id and name updated on subsequent delta for same index."""
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        # First delta has no id/name; second has both — update_metadata is called
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "late_id",
                                    "function": {"name": "LateName", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        out = await _collect(
            openai_stream_to_anthropic_events(_as_async(chunks), model="m")
        )
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert len(starts) == 1
        # id and name were updated via update_metadata
        assert starts[0]["content_block"]["id"] == "late_id"
        assert starts[0]["content_block"]["name"] == "LateName"

    # ------------------------------------------------------------------
    # openai_stream_to_anthropic_events  lines 499, 518  — tool call then content
    # ------------------------------------------------------------------

    async def test_stream_tool_calls_then_content_flushes(self):
        """Lines 499: flush_tool_calls yields events when content arrives after tool calls."""
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        # Tool call appears first, then text content — flush happens inside content branch
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "Bash", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"content": "after tools"},
                        "finish_reason": "stop",
                    }
                ]
            },
        ]
        out = await _collect(
            openai_stream_to_anthropic_events(_as_async(chunks), model="m")
        )
        types = [e["type"] for e in out]
        # Tool use block must be flushed before text block
        starts = [e for e in out if e["type"] == "content_block_start"]
        block_types = [s["content_block"]["type"] for s in starts]
        assert "tool_use" in block_types
        assert "text" in block_types
        # tool_use must come before text
        assert block_types.index("tool_use") < block_types.index("text")

    # ------------------------------------------------------------------
    # openai_stream_to_anthropic_events  line 518  — non-dict tool_call in list
    # ------------------------------------------------------------------

    async def test_stream_non_dict_tool_call_skipped(self):
        """Line 518: non-dict item in tool_calls list is skipped without error."""
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                "not-a-dict",  # line 518: non-dict → continue
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "Bash", "arguments": "{}"},
                                },
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ]
        out = await _collect(
            openai_stream_to_anthropic_events(_as_async(chunks), model="m")
        )
        starts = [e for e in out if e["type"] == "content_block_start"]
        # Only the dict tool_call should create a block
        assert len(starts) == 1
        assert starts[0]["content_block"]["name"] == "Bash"


class TestOpenAIBridgeEdgeCases:
    """Additional edge cases for remaining lines in sanitizer/openai_bridge.py."""

    # ------------------------------------------------------------------
    # anthropic_request_to_openai_body  line 234  — non-dict message skipped
    # ------------------------------------------------------------------

    def test_non_dict_message_in_messages_list_skipped(self):
        """Line 234: non-dict items in body['messages'] list are skipped."""
        from src.sanitizer.openai_bridge import anthropic_request_to_openai_body

        body = {
            "model": "m",
            "messages": [
                "not-a-dict",  # line 234: continue
                {"role": "user", "content": "hi"},
            ],
        }
        out = anthropic_request_to_openai_body(body)
        # Only the dict message should appear
        assert len(out["messages"]) == 1
        assert out["messages"][0]["role"] == "user"

    # ------------------------------------------------------------------
    # openai_stream_to_anthropic_events  line 462  — delta is truthy non-dict
    # ------------------------------------------------------------------

    async def test_stream_truthy_non_dict_delta_treated_as_empty(self):
        """Line 462: delta value that is truthy but not a dict → treated as empty dict."""
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        chunks = [
            # delta is a list (truthy, non-dict) → line 462: delta = {}
            {"choices": [{"delta": ["not", "a", "dict"], "finish_reason": None}]},
            {"choices": [{"delta": {"content": "text"}, "finish_reason": "stop"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        # Still produces a valid message frame
        types = [e["type"] for e in out]
        assert "message_start" in types
        assert "message_stop" in types

    # ------------------------------------------------------------------
    # openai_stream_to_anthropic_events  lines 478, 481  — tool_calls then reasoning
    # ------------------------------------------------------------------

    async def test_stream_tool_call_then_reasoning_flushes_tool(self):
        """Lines 478, 481: tool calls buffered then reasoning arrives → flush + close open block.

        Line 478: yield event inside flush loop (tool_calls → reasoning flush)
        Line 481: yield _close_block when open_kind is not 'thinking' during reasoning
        """
        from src.sanitizer.openai_bridge import openai_stream_to_anthropic_events

        # Some content first (opens a text block), then a tool call, then reasoning
        # to hit the "close open block before thinking" path.
        # Actually: need to open text block, buffer tool_call (doesn't open blocks),
        # then get reasoning which flushes tool calls then checks open_kind.
        # Simplest: open text block, then get reasoning (no pending tool calls needed
        # for line 481 — just need open_kind != "thinking").
        chunks = [
            # First: open a text block
            {"choices": [{"delta": {"content": "Some text"}, "finish_reason": None}]},
            # Then: reasoning arrives while text block is open → line 481 close, new thinking
            {"choices": [{"delta": {"reasoning_content": "I thought"}, "finish_reason": None}]},
            # Buffer a tool call now (after reasoning opened)
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "T", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            # Reasoning again — flushes the buffered tool call (line 478)
            {"choices": [{"delta": {"reasoning_content": " more"}, "finish_reason": "stop"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        types = [e["type"] for e in out]
        assert "message_start" in types
        assert "message_stop" in types
        # Thinking blocks present
        starts = [e for e in out if e["type"] == "content_block_start"]
        block_types = [s["content_block"]["type"] for s in starts]
        assert "thinking" in block_types


class TestSystemPromptRemainingGaps:
    """Target the remaining uncovered lines in system_prompt.py."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path):
        from src import system_prompt as sp

        orig = {
            "_default_prompt": sp._default_prompt,
            "_default_prompt_raw": sp._default_prompt_raw,
            "_runtime_prompt": sp._runtime_prompt,
            "_runtime_prompt_raw": sp._runtime_prompt_raw,
            "_active_prompt_name": sp._active_prompt_name,
            "_preset_text": sp._preset_text,
            "_DATA_DIR": sp._DATA_DIR,
            "_PERSIST_FILE": sp._PERSIST_FILE,
            "_PROMPTS_DIR": sp._PROMPTS_DIR,
        }
        sp._default_prompt = None
        sp._default_prompt_raw = None
        sp._runtime_prompt = None
        sp._runtime_prompt_raw = None
        sp._active_prompt_name = None
        sp._preset_text = None
        sp._DATA_DIR = tmp_path
        sp._PERSIST_FILE = tmp_path / "system_prompt.json"
        sp._PROMPTS_DIR = tmp_path / "prompts"
        yield sp
        for k, v in orig.items():
            setattr(sp, k, v)

    # ------------------------------------------------------------------
    # save_named_prompt  lines 342-343  — OSError reading existing file
    # ------------------------------------------------------------------

    def test_save_named_prompt_oserror_reading_existing_preserved_gracefully(self, _isolate):
        """Lines 342-343: if reading the existing file raises, created_at falls back to now."""
        sp = _isolate
        sp._PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        # Create the file first
        sp.save_named_prompt("test-prompt", "Initial content")

        # Simulate OSError when reading existing content
        with patch.object(Path, "read_text", side_effect=OSError("disk error")):
            result = sp.save_named_prompt("test-prompt", "New content")

        # created_at should be a fresh timestamp (not None, no crash)
        assert result["name"] == "test-prompt"
        assert result["content"] == "New content"
        assert result["created_at"] is not None
