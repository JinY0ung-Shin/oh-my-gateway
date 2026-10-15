"""What a session leaves on disk, and when it goes (PR #244 review 4).

#9: ``session-env/<session id>`` holds the env scripts the CLI's SessionStart /
Setup / CwdChanged / FileChanged hooks wrote through ``$CLAUDE_ENV_FILE``. A
named session that expires, is evicted or outlives a restart is resumed later
under the same id, and the resumed CLI fires SessionStart with
``source=resume`` only, so the dir has to survive those teardowns. Only a
finished session loses it (an explicit delete, the end of an anonymous session,
which nothing can resume), and never from under a session that rehydrated the
same id while the old incarnation's CLI was still shutting down.

#12: under uid isolation the session uid owns the transcript (mode 0600), so
the gateway can neither read it to rehydrate nor delete it after a stateless
run. Both failed silently: a 404 "not found or expired", a DEBUG line.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.session_manager as sm
from src.session_manager import Session, SessionManager, TranscriptUnreadable

SID = "6f1c8f9e-2b1d-4c3e-9a55-0d6f2f0b7c11"
OTHER_SID = "0b3e7a52-9d4c-4f1e-8c2a-5e6f7a8b9c0d"


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """A throwaway Claude config dir; ``session-env`` lives in it."""
    path = tmp_path / "cfg"
    (path / "session-env").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
    return path


@pytest.fixture
def projects(monkeypatch, tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(sm, "_PROJECTS_ROOT", root)
    return root


def _env_dir(cfg: Path, sid: str = SID) -> Path:
    path = cfg / "session-env" / sid
    path.mkdir(parents=True, exist_ok=True)
    (path / "sessionstart-hook-0.sh").write_text("export FOO=bar\n")
    return path


def _transcript(projects: Path, workspace: Path, sid: str = SID) -> Path:
    path = projects / sm._encode_cwd(workspace) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"type": "user", "cwd": str(workspace), "message": {"content": "hi"}}
    path.write_text(json.dumps(line) + "\n")
    return path


@contextlib.contextmanager
def _lru_policy(cap: int):
    """MAX_LIVE_SESSIONS at *cap* with SESSION_EVICTION_POLICY=lru."""
    with patch("src.constants.MAX_LIVE_SESSIONS", cap):
        with patch("src.constants.SESSION_EVICTION_POLICY", "lru"):
            yield


class _GatedClient:
    """An SDK client whose disconnect waits until the test lets it finish."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def disconnect(self) -> None:
        self.entered.set()
        await self.gate.wait()


async def _tear_down(manager: SessionManager, session: Session, how: str) -> None:
    """End *session* the way *how* names and await its teardown."""
    if how == "expire":
        session.expires_at = sm._utcnow() - timedelta(seconds=1)
        if session.client is None:
            manager.list_sessions()  # the synchronous sweep, under the lock
        else:
            await manager.cleanup_expired_sessions()
    elif how == "evict":
        session.last_accessed = sm._utcnow() - timedelta(hours=1)
        with _lru_policy(cap=1):
            manager.get_or_create_session("newcomer")  # evicts *session*
        await asyncio.gather(*list(manager._evict_tasks))
    elif how == "shutdown":
        await manager.async_shutdown()
    elif how == "delete":
        assert manager.delete_session(session.session_id)
    elif how == "delete_async":
        assert await manager.delete_session_async(session.session_id)
    else:  # pragma: no cover - a typo in a parametrization
        raise AssertionError(how)


class TestSessionEnvPolicy:
    @pytest.mark.parametrize("with_client", [False, True], ids=["idle", "client"])
    @pytest.mark.parametrize("how", ["expire", "evict", "shutdown"])
    async def test_a_named_sessions_teardown_keeps_its_dir(self, cfg, how, with_client):
        manager = SessionManager()
        session = manager.get_or_create_session(SID)
        session.user = "alice"
        if with_client:
            session.client = AsyncMock()
        env = _env_dir(cfg)

        await _tear_down(manager, session, how)

        assert SID not in manager.sessions
        assert (env / "sessionstart-hook-0.sh").is_file()

    @pytest.mark.parametrize("with_client", [False, True], ids=["idle", "client"])
    @pytest.mark.parametrize("how", ["expire", "evict", "shutdown"])
    async def test_an_anonymous_sessions_teardown_removes_its_dir(
        self, cfg, how, with_client
    ):
        # Nothing can resume an anonymous session (rehydration needs a user),
        # so its teardown is its end; a confined CLI cannot sweep the dir.
        manager = SessionManager()
        session = manager.get_or_create_session(SID)
        if with_client:
            session.client = AsyncMock()
        env = _env_dir(cfg)
        other = _env_dir(cfg, OTHER_SID)

        await _tear_down(manager, session, how)

        assert not env.exists()
        assert other.is_dir()

    @pytest.mark.parametrize("how", ["delete", "delete_async"])
    async def test_an_explicit_delete_removes_a_named_sessions_dir(self, cfg, how):
        manager = SessionManager()
        session = manager.get_or_create_session(SID)
        session.user = "alice"
        env = _env_dir(cfg)
        other = _env_dir(cfg, OTHER_SID)

        await _tear_down(manager, session, how)

        assert not env.exists()
        assert other.is_dir()

    @pytest.mark.parametrize("user, kept", [("alice", True), (None, False)])
    async def test_a_delete_that_is_not_the_end_keeps_a_named_sessions_dir(
        self, cfg, user, kept
    ):
        # finished=False: a caller dropping a session a continuation may still
        # resume (a failed client creation).
        manager = SessionManager()
        session = manager.get_or_create_session(SID)
        session.user = user
        session.client = AsyncMock()
        env = _env_dir(cfg)

        assert await manager.delete_session_async(SID, finished=False)

        assert session.client is None  # disconnected all the same
        assert env.is_dir() is kept

    def test_the_workspace_cleanup_is_unchanged(self, cfg, tmp_path):
        # An anonymous workspace still goes on expiry; a named one stays.
        manager = SessionManager()
        anon_ws = tmp_path / "_tmp_anon"
        named_ws = tmp_path / "alice"
        anon_ws.mkdir()
        named_ws.mkdir()
        anon = manager.get_or_create_session(SID)
        anon.workspace = str(anon_ws)
        named = manager.get_or_create_session(OTHER_SID)
        named.user, named.workspace = "alice", str(named_ws)
        for session in (anon, named):
            session.expires_at = sm._utcnow() - timedelta(seconds=1)

        manager.list_sessions()

        assert not anon_ws.exists()
        assert named_ws.is_dir()


class TestRehydrateRace:
    """The old incarnation's teardown awaits its CLI's exit with the session
    already out of the map. A continuation rehydrates the same id meanwhile,
    and its CLI is granted that same ``session-env`` dir."""

    @pytest.mark.parametrize("how", ["expire", "evict", "delete_async"])
    async def test_a_rehydrated_incarnation_keeps_its_dir(
        self, cfg, projects, tmp_path, how
    ):
        workspace = tmp_path / "workspaces" / "alice" / "claude"
        workspace.mkdir(parents=True)
        _transcript(projects, workspace)
        manager = SessionManager()
        old = manager.get_or_create_session(SID)
        old.user, old.workspace = "alice", str(workspace)
        client = _GatedClient()
        old.client = client
        env = _env_dir(cfg)

        teardown = asyncio.create_task(_tear_down(manager, old, how))
        await asyncio.wait_for(client.entered.wait(), timeout=5)
        assert SID not in manager.sessions  # gone from the map, CLI not yet
        new = manager.get_session(SID, user="alice", cwd=str(workspace))
        assert new is not None and new is not old
        client.gate.set()
        await asyncio.wait_for(teardown, timeout=5)

        assert manager.sessions[SID] is new
        assert (env / "sessionstart-hook-0.sh").is_file()

    async def test_the_guard_also_covers_an_anonymous_teardown(self, cfg):
        manager = SessionManager()
        old = manager.get_or_create_session(SID)
        client = _GatedClient()
        old.client = client
        env = _env_dir(cfg)

        teardown = asyncio.create_task(manager.delete_session_async(SID))
        await asyncio.wait_for(client.entered.wait(), timeout=5)
        newer = manager.get_or_create_session(SID)  # a live session, same id
        client.gate.set()
        await asyncio.wait_for(teardown, timeout=5)

        assert manager.sessions[SID] is newer
        assert env.is_dir()


class TestExpiredOnLookup:
    """A lookup (``peek_session``, ``get_session``) that finds a session
    expired before the sweep does used to drop it with nothing released: its
    CLI kept running, its temp workspace stayed and, under uid isolation, the
    ephemeral uid it leased was never returned (only the workspace's removal
    ends a lease)."""

    @pytest.fixture
    def uid_pool(self, monkeypatch, tmp_path):
        from src.backends.claude import uid_isolation as ui

        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        monkeypatch.setattr(ui, "_registry", None)
        monkeypatch.setattr(ui, "_ephemeral_pool", None)
        return ui

    @staticmethod
    def _expired_anonymous(manager, tmp_path, client=None):
        workspace = tmp_path / "workspaces" / "_tmp_anon"
        workspace.mkdir(parents=True)
        session = manager.get_or_create_session(SID)
        session.workspace = str(workspace)
        session.client = client
        session.expires_at = sm._utcnow() - timedelta(seconds=1)
        return session, workspace

    @pytest.mark.parametrize("lookup", ["peek", "get"])
    def test_an_idle_session_is_released_on_the_spot(
        self, cfg, uid_pool, tmp_path, lookup
    ):
        manager = SessionManager()
        _session, workspace = self._expired_anonymous(manager, tmp_path)
        key = os.path.abspath(workspace)
        uid = uid_pool.resolve_uid(key, persist=False)
        env = _env_dir(cfg)

        found = (
            manager.peek_session(SID) if lookup == "peek" else manager.get_session(SID)
        )

        assert found is None
        assert not workspace.exists()
        assert not env.exists()
        assert uid_pool.get_ephemeral_pool().release(key) is None  # lease ended
        assert uid_pool.get_ephemeral_pool().is_free(uid)

    async def test_a_client_holding_session_is_torn_down_in_the_background(
        self, cfg, tmp_path
    ):
        manager = SessionManager()
        client = AsyncMock()
        _session, workspace = self._expired_anonymous(manager, tmp_path, client)

        assert manager.peek_session(SID) is None
        assert workspace.exists()  # never under a CLI that may still run
        await asyncio.gather(*list(manager._evict_tasks))

        client.disconnect.assert_awaited_once()
        assert not workspace.exists()

    def test_without_a_loop_a_client_holding_session_is_left_alone(self, tmp_path):
        manager = SessionManager()
        _session, workspace = self._expired_anonymous(manager, tmp_path, MagicMock())

        assert manager.peek_session(SID) is None
        assert workspace.exists()  # nothing could await its disconnect
        assert not manager._evict_tasks


class TestUnreadableTranscript:
    """#12, rehydrate side: EACCES on the transcript is an explicit error."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        if os.geteuid() == 0:
            pytest.skip("root reads a 000 file")
        monkeypatch.setattr(sm, "_UNREADABLE_WARNED", set())

    @staticmethod
    @contextlib.contextmanager
    def _unreadable(path: Path):
        """*path* as the gateway sees a session uid's 0600 file."""
        path.chmod(0)
        try:
            yield path
        finally:
            path.chmod(0o600)

    def test_rehydrate_raises_and_warns_once_per_path(self, projects, tmp_path, caplog):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        transcript = _transcript(projects, workspace)
        with (
            self._unreadable(transcript),
            caplog.at_level(logging.DEBUG, logger=sm.__name__),
        ):
            for _ in range(2):
                with pytest.raises(TranscriptUnreadable) as excinfo:
                    sm._try_rehydrate_from_jsonl(SID, user="alice", cwd=str(workspace))
                assert excinfo.value.path == transcript
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert str(transcript) in warnings[0].getMessage()
        assert "CLAUDE_ISOLATION_MODE=uid" in warnings[0].getMessage()

    def test_get_session_propagates_it_as_a_miss(self, projects, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        transcript = _transcript(projects, workspace)
        manager = SessionManager()
        with self._unreadable(transcript):
            with pytest.raises(TranscriptUnreadable):
                manager.get_session(SID, user="alice", cwd=str(workspace))
        assert SID not in manager.sessions
        assert manager.stats()["rehydrate_misses"] == 1
        # Readable again, the same transcript rehydrates.
        assert manager.get_session(SID, user="alice", cwd=str(workspace))

    def test_other_os_errors_still_read_as_missing(self, projects, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _transcript(projects, workspace)
        with patch.object(Path, "open", side_effect=OSError(5, "I/O error")):
            assert (
                sm._try_rehydrate_from_jsonl(SID, user="alice", cwd=str(workspace))
                is None
            )

    def test_the_route_answers_500_naming_the_cause(
        self, projects, tmp_path, isolated_session_manager
    ):
        # 500, not a retryable 503: nothing clears it before the broker reads
        # transcripts as the session uid, and OpenAI SDKs retry a 503 alone.
        from src.constants import DEFAULT_MODEL

        workspace = tmp_path / "workspaces" / "alice" / "claude"
        workspace.mkdir(parents=True)
        transcript = _transcript(projects, workspace)
        with self._unreadable(transcript), _responses_client(workspace) as client:
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "and then?",
                    "user": "alice",
                    "previous_response_id": f"resp_{SID}_1",
                },
            )
        assert resp.status_code == 500
        assert resp.headers["x-should-retry"] == "false"
        error = resp.json()["error"]
        assert error["code"] == "transcript_unreadable"
        assert error["type"] == "server_error"
        assert "cannot be resumed" in error["message"]
        assert "not found" not in error["message"]
        assert SID not in isolated_session_manager.sessions


class TestStatelessTranscriptCleanup:
    """#12, cleanup side: the stateless contract promises the transcript goes."""

    def test_a_permission_error_is_a_warning(self, cfg, projects, tmp_path, caplog):
        from src.routes.agent_messages import _cleanup_sdk_transcript

        if os.geteuid() == 0:
            pytest.skip("root unlinks in a 0555 directory")
        workspace = tmp_path / "_tmp_run"
        workspace.mkdir()
        transcript = _transcript(projects, workspace)
        # The gateway's view of the project dir the session uid created.
        transcript.parent.chmod(0o555)
        try:
            with caplog.at_level(logging.DEBUG, logger="src.routes.agent_messages"):
                _cleanup_sdk_transcript(
                    Session(session_id=SID, workspace=str(workspace))
                )
        finally:
            transcript.parent.chmod(0o755)
        assert transcript.is_file()  # removing it is the follow-up's job
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert str(transcript) in warnings[0].getMessage()

    def test_other_os_errors_stay_at_debug(self, cfg, projects, tmp_path, caplog):
        from src.routes.agent_messages import _cleanup_sdk_transcript

        workspace = tmp_path / "_tmp_run"
        workspace.mkdir()
        _transcript(projects, workspace)
        with patch.object(Path, "unlink", side_effect=OSError(5, "I/O error")):
            # The removal (and its log line) lives in uid_cleanup now.
            uid_cleanup_log = "src.backends.claude.uid_cleanup"
            with caplog.at_level(logging.DEBUG, logger=uid_cleanup_log):
                _cleanup_sdk_transcript(
                    Session(session_id=SID, workspace=str(workspace))
                )
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert "transcript cleanup failed" in caplog.text


@contextlib.contextmanager
def _responses_client(workspace: Path):
    """A TestClient over the app with a mock Claude backend whose workspace
    manager resolves every user to *workspace*."""
    from fastapi.testclient import TestClient

    import src.main as main
    import src.routes.general as general_module
    import src.routes.responses as responses_module
    from src.backend_registry import BackendRegistry

    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)

    def _discover():
        from tests.conftest import register_all_descriptors

        register_all_descriptors()
        BackendRegistry.register("claude", mock_cli)

    workspaces = MagicMock()
    workspaces.resolve.return_value = workspace
    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()
    with contextlib.ExitStack() as stack:
        for target, name, value in (
            (main, "discover_backends", _discover),
            (responses_module, "verify_api_key", AsyncMock(return_value=True)),
            (general_module, "verify_api_key", AsyncMock(return_value=True)),
            (
                main,
                "validate_claude_code_auth",
                MagicMock(return_value=(True, {"method": "test"})),
            ),
            (responses_module, "validate_backend_auth_or_raise", MagicMock()),
            (main.session_manager, "start_cleanup_task", MagicMock()),
            (main.session_manager, "async_shutdown", AsyncMock()),
            (responses_module, "workspace_manager", workspaces),
        ):
            stack.enter_context(patch.object(target, name, value))
        yield stack.enter_context(TestClient(main.app))
    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()
