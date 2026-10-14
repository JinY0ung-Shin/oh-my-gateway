"""A uid lease does not end while a CLI the gateway started under it runs
(PR #244 review 4, round 2).

An anonymous run's ephemeral uid is leased on its workspace path and released
when that workspace is removed (``WorkspaceManager.cleanup_temp_workspace``).
These pin the paths that removed the workspace, and so released the lease,
while the run's CLI still ran:

* a session deleted (or expired) while its client was being created: the new
  client was handed to a session nobody held, its CLI ran on as an orphan, and
  ~256 runs later another run got its uid;
* a teardown whose disconnect wedged past the cap;
* ``verify()``, which removed its workspace before closing its query;

and the one that kept a lease (with a workspace and a session slot) for an
hour: a new session whose first turn was refused (an unknown ``/command``).
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import src.routes.responses as responses_module
import src.session_manager as sm_module
from src.backends.claude import uid_isolation as ui
from src.session_manager import SessionManager
from src.workspace_manager import WorkspaceManager

SID = "a1c2e3f4-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _uid_pool(monkeypatch, tmp_path):
    """A fresh ephemeral pool, and a throwaway config dir for session-env."""
    (tmp_path / "cfg").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
    monkeypatch.setattr(ui, "_registry", None)
    monkeypatch.setattr(ui, "_ephemeral_pool", None)


@pytest.fixture
def events(monkeypatch):
    """What happened, in order: client disconnects, kills, lease releases."""
    log: list = []
    real_release = ui.release_ephemeral

    def release(key):
        if key in ui.get_ephemeral_pool()._leases:
            log.append("released")
        real_release(key)

    monkeypatch.setattr(ui, "release_ephemeral", release)
    return log


@pytest.fixture
def wm(tmp_path):
    return WorkspaceManager(base_path=tmp_path / "workspaces")


def _lease(workspace) -> int:
    """Lease the uid an anonymous spawn in *workspace* runs as."""
    return ui.resolve_uid(os.path.abspath(workspace), persist=False)


def _leased(workspace) -> bool:
    return os.path.abspath(workspace) in ui.get_ephemeral_pool()._leases


class _Client:
    def __init__(self, events, uid=None):
        self.events = events
        self.uid = uid
        self.disconnected = False

    async def disconnect(self):
        self.events.append("disconnected")
        self.disconnected = True


class _GatedBackend:
    """``create_client`` as the Claude backend runs it: the options build leases
    the uid, the pre-connect re-mint resolves it again, then connect() spawns
    the CLI and waits for its initialize, which the test holds open."""

    def __init__(self, events):
        self.events = events
        self.connecting = asyncio.Event()
        self.gate = asyncio.Event()
        self.clients: list = []

    async def create_client(self, *, session, cwd, **_kwargs):
        uid = _lease(cwd)
        assert _lease(cwd) == uid
        self.connecting.set()
        await self.gate.wait()
        client = _Client(self.events, uid)
        self.clients.append(client)
        return client


def _resolved():
    from src.backend_registry import ResolvedModel
    from src.constants import DEFAULT_MODEL

    return ResolvedModel(DEFAULT_MODEL, "claude", DEFAULT_MODEL)


def _anonymous(manager, wm, sid=SID, turn_counter=0):
    session = manager.get_or_create_session(sid)
    workspace = wm.resolve(None)
    session.workspace = str(workspace)
    session.turn_counter = turn_counter
    return session, workspace


class TestDeleteWhileTheClientStarts:
    """N1: the route assigned the started client to a session that had been
    deleted meanwhile, and the delete had already removed the workspace."""

    @pytest.fixture
    def manager(self, monkeypatch):
        manager = SessionManager()
        monkeypatch.setattr(responses_module, "session_manager", manager)
        monkeypatch.setattr(responses_module, "get_mcp_servers", lambda: {})
        return manager

    async def test_the_new_client_is_stopped_before_the_lease_ends(
        self, manager, wm, events
    ):
        from src.constants import DEFAULT_MODEL
        from src.response_models import ResponseCreateRequest

        # An anonymous continuation re-creating its client (after a cancel).
        session, workspace = _anonymous(manager, wm, turn_counter=2)
        body = ResponseCreateRequest(
            model=DEFAULT_MODEL, input="go on", previous_response_id=f"resp_{SID}_2"
        )
        backend = _GatedBackend(events)
        start = asyncio.create_task(
            responses_module._ensure_response_session_client(
                body, _resolved(), backend, session, SID, False, None, str(workspace)
            )
        )
        await asyncio.wait_for(backend.connecting.wait(), timeout=5)

        assert await manager.delete_session_async(SID)  # DELETE /v1/sessions/{id}
        assert SID not in manager.sessions
        # The CLI may run already: its workspace and uid stay for now.
        assert workspace.exists() and _leased(workspace)

        backend.gate.set()
        with pytest.raises(HTTPException) as raised:
            await asyncio.wait_for(start, timeout=5)

        assert raised.value.status_code == 409
        assert raised.value.detail["error"]["code"] == "session_ended"
        # OpenAI SDKs retry a 409 on their own; a retry would start afresh.
        assert raised.value.headers == {"x-should-retry": "false"}
        (client,) = backend.clients
        assert client.disconnected and session.client is None
        assert not workspace.exists() and not _leased(workspace)
        assert events == ["disconnected", "released"]

    @pytest.mark.parametrize("deleted", [False, True], ids=["live", "deleted"])
    async def test_a_cancelled_start_still_ends(self, manager, wm, events, deleted):
        from src.constants import DEFAULT_MODEL
        from src.response_models import ResponseCreateRequest

        session, workspace = _anonymous(manager, wm, turn_counter=1)
        _lease(workspace)
        body = ResponseCreateRequest(
            model=DEFAULT_MODEL, input="go on", previous_response_id=f"resp_{SID}_1"
        )

        class _Cancelled:
            async def create_client(self, **_kwargs):
                if deleted:
                    assert await manager.delete_session_async(SID)
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await responses_module._ensure_response_session_client(
                body,
                _resolved(),
                _Cancelled(),
                session,
                SID,
                False,
                None,
                str(workspace),
            )

        assert session.client_starting is False  # evictable again
        if deleted:
            # The delete's deferred release ran once the start ended.
            assert not workspace.exists() and events == ["released"]
        else:
            assert manager.sessions[SID] is session and workspace.exists()

    @pytest.mark.parametrize("is_new_session", [True, False], ids=["new", "resumed"])
    async def test_a_failed_start_finishes_only_a_new_session(
        self, manager, wm, tmp_path, is_new_session
    ):
        # A new session was never handed out, so its CLI session-env dir goes
        # with it; a continuation's session may be resumed later and keeps it.
        from src.constants import DEFAULT_MODEL
        from src.response_models import ResponseCreateRequest

        session = manager.get_or_create_session(SID)
        session.user, session.workspace = "alice", str(wm.resolve("alice"))
        env = tmp_path / "cfg" / "session-env" / SID
        env.mkdir(parents=True)
        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="hello", user="alice")

        class _Down:
            async def create_client(self, **_kwargs):
                raise RuntimeError("backend down")

        with pytest.raises(HTTPException) as raised:
            await responses_module._ensure_response_session_client(
                body,
                _resolved(),
                _Down(),
                session,
                SID,
                is_new_session,
                None,
                session.workspace,
            )

        assert raised.value.status_code == 503
        assert SID not in manager.sessions
        assert env.is_dir() is not is_new_session

    async def test_an_expiry_while_starting_waits_for_the_new_client(
        self, manager, wm, events
    ):
        session, workspace = _anonymous(manager, wm)
        _lease(workspace)
        manager.begin_client_start(session)
        session.expires_at = sm_module._utcnow() - timedelta(seconds=1)

        assert await manager.cleanup_expired_sessions() == 1
        assert workspace.exists() and _leased(workspace)

        client = _Client(events)
        assert await manager.finish_client_start(SID, session, client) is False
        assert client.disconnected
        assert not workspace.exists()
        assert events == ["disconnected", "released"]

    def test_eviction_skips_a_session_whose_client_is_starting(self, manager, wm):
        from src.concurrency import SessionLimitExceeded

        session, workspace = _anonymous(manager, wm)
        session.last_accessed = sm_module._utcnow() - timedelta(hours=1)
        manager.begin_client_start(session)
        with (
            patch("src.constants.MAX_LIVE_SESSIONS", 1),
            patch("src.constants.SESSION_EVICTION_POLICY", "lru"),
        ):
            with pytest.raises(SessionLimitExceeded):
                manager.get_or_create_session("newcomer")
        assert manager.sessions[SID] is session and workspace.exists()

    async def test_a_failed_start_hands_a_live_session_back(self, manager, wm):
        session, workspace = _anonymous(manager, wm)
        manager.begin_client_start(session)

        assert await manager.finish_client_start(SID, session, None) is True

        assert session.client_starting is False and session.client is None
        assert manager.sessions[SID] is session and workspace.exists()

    async def test_a_failed_start_of_a_deleted_session_releases_its_dirs(
        self, manager, wm, events
    ):
        session, workspace = _anonymous(manager, wm)
        _lease(workspace)
        manager.begin_client_start(session)
        assert await manager.delete_session_async(SID)
        assert workspace.exists()

        assert await manager.finish_client_start(SID, session, None) is False

        assert not workspace.exists()
        assert events == ["released"]

    async def test_a_handed_over_client_is_stopped_by_a_later_delete(
        self, manager, wm, events
    ):
        session, workspace = _anonymous(manager, wm)
        _lease(workspace)
        manager.begin_client_start(session)
        client = _Client(events)
        assert await manager.finish_client_start(SID, session, client) is True
        assert session.client is client

        assert await manager.delete_session_async(SID)

        assert client.disconnected and not workspace.exists()
        assert events == ["disconnected", "released"]


class _FakeProcess:
    def __init__(self, events):
        self.events = events
        self.returncode = None
        self.pid = 4242

    def kill(self):
        self.events.append("killed")
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _WedgedClient:
    """A client whose disconnect never finishes, with its CLI still running."""

    def __init__(self, events, process):
        self.events = events
        self._transport = type("T", (), {"_process": process})()

    async def disconnect(self):
        self.events.append("disconnect wedged")
        await asyncio.Event().wait()


class TestWedgedDisconnect:
    """A disconnect stuck past the cap left the CLI running, and the teardown
    removed its workspace (ending its lease) all the same."""

    @pytest.fixture(autouse=True)
    def _short_cap(self, monkeypatch):
        monkeypatch.setattr(sm_module, "CLIENT_DISCONNECT_TIMEOUT_SECONDS", 0.05)

    @pytest.mark.parametrize("how", ["delete", "expire", "shutdown"])
    async def test_the_cli_is_killed_before_its_dirs_go(self, wm, events, how):
        manager = SessionManager()
        session, workspace = _anonymous(manager, wm)
        _lease(workspace)
        process = _FakeProcess(events)
        session.client = _WedgedClient(events, process)

        if how == "delete":
            assert await manager.delete_session_async(SID)
        elif how == "expire":
            session.expires_at = sm_module._utcnow() - timedelta(seconds=1)
            assert await manager.cleanup_expired_sessions() == 1
        else:
            await manager.async_shutdown()

        assert process.returncode == -9
        assert not workspace.exists()
        assert events == ["disconnect wedged", "killed", "released"]

    async def test_a_cli_the_disconnect_stopped_is_not_killed(self, events):
        client = _Client(events)
        client._transport = type("T", (), {"_process": None})()  # SDK cleared it

        await sm_module._stop_client(client, "a session")

        assert events == ["disconnected"]

    async def test_an_exited_cli_is_not_killed(self, events):
        process = _FakeProcess(events)
        process.returncode = 0
        client = _WedgedClient(events, process)

        await sm_module._stop_client(client, "a session")

        assert events == ["disconnect wedged"]


class TestRefusedFirstTurn:
    """N2: a new session refused before its first turn stayed for the TTL,
    with its workspace, a session slot and the uid its slash preflight leased.
    Three such requests exhausted a 3-uid pool for an hour."""

    @staticmethod
    def _refusing_preflight(leased_keys):
        from src.backends.claude.slash_commands import SlashCommandError

        async def refuse(prompt, cwd=None, user=None):
            # The uid-mode catalog preflight runs as the session's uid.
            leased_keys.append(os.path.abspath(cwd))
            _lease(cwd)
            raise SlashCommandError(code="unknown_command", message="no such")

        return refuse

    def _post(self, workspace, refuse, **body):
        from tests.test_responses_more_coverage import DEFAULT_MODEL, client_context

        with client_context(workspace_path=workspace) as (client, _cli, _wm):
            with patch.object(responses_module, "validate_slash_prompt", refuse):
                return client.post(
                    "/v1/responses",
                    json={"model": DEFAULT_MODEL, "input": "/bogus", **body},
                )

    def test_a_new_session_goes_at_once(self, isolated_session_manager, wm):
        workspace = wm.resolve(None)
        keys: list = []

        resp = self._post(workspace, self._refusing_preflight(keys))

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unknown_command"
        assert isolated_session_manager.sessions == {}
        assert not workspace.exists()
        assert keys and not _leased(workspace)

    def test_a_continuation_keeps_its_session(self, isolated_session_manager, wm):
        session, workspace = _anonymous(isolated_session_manager, wm, turn_counter=1)
        keys: list = []

        resp = self._post(
            workspace,
            self._refusing_preflight(keys),
            previous_response_id=f"resp_{SID}_1",
        )

        assert resp.status_code == 400
        assert isolated_session_manager.sessions[SID] is session
        assert workspace.exists() and _leased(workspace)


class TestVerify:
    """N3: verify() removed its workspace (ending the lease) before the query
    generator was closed. This pins only the order of verify()'s own steps,
    with a one-level fake generator: with the pinned SDK the CLI is stopped by
    draining the stream (its top-level ``query()`` has no ``try/finally``)."""

    async def test_the_query_is_closed_before_the_lease_ends(
        self, monkeypatch, wm, events
    ):
        import src.workspace_manager as wm_module
        from src.backends.claude import client as client_module
        from src.backends.claude import landlock

        async def fake_query(prompt, options):
            try:
                yield {"type": "assistant", "content": "hi"}
                await asyncio.sleep(10)
            finally:
                events.append("query closed")

        def build(self, max_turns, cwd=None, **_kwargs):
            _lease(cwd)  # what options_env does for this spawn in uid mode
            return object()

        monkeypatch.setattr(client_module, "query", fake_query)
        monkeypatch.setattr(landlock, "effective_mode", lambda: "uid")
        monkeypatch.setattr(wm_module, "workspace_manager", wm)
        monkeypatch.setattr(client_module.ClaudeCodeCLI, "_build_sdk_options", build)
        cli = client_module.ClaudeCodeCLI.__new__(client_module.ClaudeCodeCLI)

        assert await cli.verify() is True

        assert events == ["query closed", "released"]
        assert not list((wm.base_path).glob("_tmp_*"))


class _SlowClient:
    """A client whose disconnect takes a moment, like the SDK's own."""

    def __init__(self, events):
        self.events = events

    async def disconnect(self):
        await asyncio.sleep(0.05)
        self.events.append("disconnected")


class TestExpirySweepDuringFinish:
    """Round-2 verification N1: ``finish_client_start`` cleared the start flag
    before stopping the client of a session that had ended, so the expiry
    sweep's release task, running during that stop, released the workspace
    and uid lease under a CLI still running."""

    async def test_the_sweep_waits_for_the_stop(self, wm, events):
        manager = SessionManager()
        session, workspace = _anonymous(manager, wm)
        _lease(workspace)
        manager.begin_client_start(session)
        session.expires_at = sm_module._utcnow() - timedelta(seconds=1)

        sweep = asyncio.create_task(manager.cleanup_expired_sessions())
        await asyncio.sleep(0)  # the sweep deletes it and schedules its teardown
        assert SID not in manager.sessions

        live = await manager.finish_client_start(SID, session, _SlowClient(events))
        await sweep
        assert live is False
        assert events == ["disconnected", "released"], events
        assert not workspace.exists() and not _leased(workspace)
