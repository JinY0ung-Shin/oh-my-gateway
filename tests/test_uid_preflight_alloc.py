"""uid mode: no durable uid for a preflight, and a full band is not "retry"
(PR #244 review 4, finding #8).

Every distinct ``user`` took a stable uid for good, in the slash-command
preflight too — which runs before the request is validated, so a request then
refused with 400 (an unknown ``/command``) still used one up. Once the band was
full, every new named user got a 503 "retry shortly" (500 on the slash path)
that no retry could fix. Now:

* ``uid_isolation.existing_uid`` looks a user up without allocating;
* a uid-mode preflight for a named user with no uid yet runs landlock-shaped in
  that user's workspace, so it allocates nothing;
* exhaustion raises ``UidBandExhausted``, which the request path reports as a
  non-retryable operator error, and the band warns once at 90%.

Reclaiming the uids of users who are gone is a follow-up.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.backends.claude import landlock
from src.backends.claude import uid_isolation as ui


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in (
        landlock.MODE_ENV,
        landlock.FALLBACK_ENV,
        landlock.BROKER_SOCKET_ENV,
        ui.TICKET_SECRET_ENV,
        ui.UID_MIN_ENV,
        ui.UID_MAX_ENV,
        ui.EPHEMERAL_UIDS_ENV,
        ui.BROKER_UID_ENV,
        "APP_UID",
    ):
        monkeypatch.delenv(var, raising=False)
    # Building the path lists pre-creates dirs in the config dir.
    (tmp_path / "cfg").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
    landlock.reset_mode_cache()
    ui.reset_captured_secret()
    ui.reset_registry_cache()
    yield
    landlock.reset_mode_cache()
    ui.reset_captured_secret()
    ui.reset_registry_cache()


@pytest.fixture
def registry(tmp_path) -> Path:
    return tmp_path / "uids.json"


@pytest.fixture
def uid_on(monkeypatch):
    """uid mode with a reachable broker faked and a ticket secret."""
    monkeypatch.setenv(landlock.MODE_ENV, "uid")
    monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
    monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, "/run/x.sock")
    monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
    landlock.reset_mode_cache()


def _band(monkeypatch, lo: int, hi: int, ephemeral: int = 1) -> None:
    monkeypatch.setenv(ui.UID_MIN_ENV, str(lo))
    monkeypatch.setenv(ui.UID_MAX_ENV, str(hi))
    monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, str(ephemeral))
    ui.reset_registry_cache()


def _persisted(registry: Path) -> dict:
    return json.loads(registry.read_text()) if registry.exists() else {}


def _fill_warnings(caplog) -> list:
    return [r for r in caplog.records if "never reclaimed" in r.getMessage()]


class TestExistingUid:
    def test_unknown_user_is_none_and_nothing_is_written(self, registry):
        assert ui.existing_uid("alice") is None
        assert not registry.exists()
        assert ui.existing_uid("alice") is None  # still nothing allocated

    def test_registered_user_gets_their_uid(self, registry):
        uid = ui.resolve_uid("alice")
        assert ui.existing_uid("alice") == uid
        assert _persisted(registry) == {"alice": uid}

    def test_a_held_entry_refuses_like_resolve(self, registry):
        registry.write_text(json.dumps({"alice": 5}))  # outside the band
        with pytest.raises(ui.UidAllocationError) as looked_up:
            ui.existing_uid("alice")
        with pytest.raises(ui.UidAllocationError) as resolved:
            ui.resolve_uid("alice")
        assert str(looked_up.value) == str(resolved.value)
        assert _persisted(registry) == {"alice": 5}

    def test_a_corrupt_registry_refuses(self, registry):
        registry.write_text("{")
        with pytest.raises(ui.UidAllocationError):
            ui.existing_uid("alice")


class TestPreflightAllocatesNothing:
    def test_a_new_named_user_runs_landlock_shaped_in_the_workspace(
        self, uid_on, registry, tmp_path
    ):
        env = landlock.preflight_options_env(tmp_path / "ws", user="alice")
        assert env[landlock.MODE_ENV] == "landlock"
        assert landlock.RO_ENV in env and landlock.RW_ENV in env  # the lists
        assert landlock.TICKET_ENV not in env
        assert ui.existing_uid("alice") is None
        assert not registry.exists()

    def test_a_known_user_runs_as_their_uid(self, uid_on, registry, tmp_path):
        uid = ui.resolve_uid("alice")
        env = landlock.preflight_options_env(tmp_path / "ws", user="alice")
        assert env[landlock.MODE_ENV] == "uid"
        assert ui.verify_ticket(env[landlock.TICKET_ENV])["uid"] == uid
        assert _persisted(registry) == {"alice": uid}

    def test_an_anonymous_preflight_keeps_its_ephemeral_ticket(
        self, uid_on, registry, tmp_path
    ):
        env = landlock.preflight_options_env(tmp_path / "ws")
        assert env[landlock.MODE_ENV] == "uid"
        assert ui.verify_ticket(env[landlock.TICKET_ENV])["uid"]
        assert not registry.exists()

    def test_a_held_user_is_still_refused(self, uid_on, registry, tmp_path):
        registry.write_text(json.dumps({"alice": 5}))
        with pytest.raises(ui.UidAllocationError):
            landlock.preflight_options_env(tmp_path / "ws", user="alice")

    def test_landlock_mode_is_unchanged(self, monkeypatch, registry, tmp_path):
        monkeypatch.setenv(landlock.MODE_ENV, "landlock")
        landlock.reset_mode_cache()
        env = landlock.preflight_options_env(tmp_path / "ws", user="alice")
        assert env[landlock.MODE_ENV] == "landlock"
        assert not registry.exists()

    def test_the_sessions_own_spawn_still_allocates(self, uid_on, registry, tmp_path):
        env = landlock.options_env(tmp_path / "ws", user="alice")
        uid = ui.verify_ticket(env[landlock.TICKET_ENV])["uid"]
        assert _persisted(registry) == {"alice": uid}

    async def test_a_request_refused_with_400_takes_no_uid(
        self, uid_on, registry, monkeypatch, tmp_path
    ):
        # The whole slash path: validate_prompt -> catalog preflight (once: the
        # forced refresh reuses the fresh catalog) -> SlashCommandError. The fake SDK
        # client records which confinement each preflight spawn would get.
        from src.backends.claude import slash_commands as sc

        seen = []

        class _FakeClient:
            def __init__(self, options=None, **_kwargs):
                seen.append(options.env.get(landlock.MODE_ENV))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get_server_info(self):
                return {"commands": [{"name": "review"}]}

        monkeypatch.setattr(sc, "ClaudeSDKClient", _FakeClient)
        ws = tmp_path / "ws"
        ws.mkdir()
        sc._cache.reset()
        try:
            with pytest.raises(sc.SlashCommandError) as refused:
                await sc.validate_prompt("/no-such-command", cwd=ws, user="mallory")
        finally:
            sc._cache.reset()
        assert refused.value.code == "unknown_command"
        # One preflight: the forced refresh reuses the catalog the cache miss just read.
        assert seen == ["landlock"]
        assert not registry.exists()


class TestBandExhaustion:
    def test_exhaustion_is_its_own_allocation_error(self, monkeypatch, registry):
        _band(monkeypatch, 20000, 20003)  # stable [20000, 20002]
        for name in ("a", "b", "c"):
            ui.resolve_uid(name)
        with pytest.raises(ui.UidBandExhausted, match="exhausted") as full:
            ui.resolve_uid("d")
        assert isinstance(full.value, ui.UidAllocationError)
        assert set(_persisted(registry)) == {"a", "b", "c"}

    def test_other_refusals_are_not_exhaustion(self, registry):
        registry.write_text(json.dumps({"alice": 5}))
        with pytest.raises(ui.UidAllocationError) as held:
            ui.resolve_uid("alice")
        assert not isinstance(held.value, ui.UidBandExhausted)

    def test_warns_once_when_the_band_crosses_90_percent(
        self, monkeypatch, registry, caplog
    ):
        _band(monkeypatch, 20000, 20010)  # stable [20000, 20009]: 10 uids
        with caplog.at_level(logging.WARNING, logger=ui.logger.name):
            for i in range(8):
                ui.resolve_uid(f"u{i}")
            assert _fill_warnings(caplog) == []
            ui.resolve_uid("u8")  # 9 of 10: crosses 90%
            assert len(_fill_warnings(caplog)) == 1
            ui.resolve_uid("u9")
            ui.resolve_uid("u0")  # an existing user: no allocation
            with pytest.raises(ui.UidBandExhausted):
                ui.resolve_uid("u10")
        assert len(_fill_warnings(caplog)) == 1  # once per crossing
        assert "9 of the 10" in _fill_warnings(caplog)[0].getMessage()

    def test_a_registry_loaded_nearly_full_warns_at_startup(
        self, monkeypatch, registry, caplog
    ):
        _band(monkeypatch, 20000, 20010)
        registry.write_text(json.dumps({f"u{i}": 20000 + i for i in range(9)}))
        ui.reset_registry_cache()
        with caplog.at_level(logging.WARNING, logger=ui.logger.name):
            ui.get_registry()
            ui.resolve_uid("u0")
        assert len(_fill_warnings(caplog)) == 1

    def test_reserved_uids_do_not_count_as_capacity(
        self, monkeypatch, registry, caplog
    ):
        # 10 uids in the band, one reserved (the app uid): 9 allocatable.
        _band(monkeypatch, 20000, 20010)
        monkeypatch.setenv("APP_UID", "20000")
        ui.reset_registry_cache()
        with caplog.at_level(logging.WARNING, logger=ui.logger.name):
            for i in range(8):
                ui.resolve_uid(f"u{i}")  # 8 of 9: 88%
            assert _fill_warnings(caplog) == []
            ui.resolve_uid("u8")  # 9 of 9
        assert "9 of the 9" in _fill_warnings(caplog)[0].getMessage()


# ---------------------------------------------------------------------------
# The request path: a full band is not "retry shortly"
# ---------------------------------------------------------------------------


def _resolved():
    from src.backend_registry import ResolvedModel
    from src.constants import DEFAULT_MODEL

    return ResolvedModel(DEFAULT_MODEL, "claude", DEFAULT_MODEL)


async def _ensure_client_with(exc: BaseException):
    from src.constants import DEFAULT_MODEL
    from src.response_models import ResponseCreateRequest
    from src.routes import responses as responses_module

    async def boom(**_kwargs):
        raise exc

    backend = SimpleNamespace(create_client=boom)
    session = SimpleNamespace(client=None, base_system_prompt=None)
    body = ResponseCreateRequest(model=DEFAULT_MODEL, input="hello")
    delete = AsyncMock()
    with (
        patch.object(responses_module, "get_mcp_servers", return_value={}),
        patch.object(responses_module.session_manager, "delete_session_async", delete),
        # The route hands a failed start back only for a session the manager
        # still holds (SessionManager.finish_client_start).
        patch.dict(responses_module.session_manager.sessions, {"sess-1": session}),
        patch.object(responses_module, "_clear_stale_pending_tool_call"),
        patch.object(responses_module.session_outbox, "reset_active_tasks"),
        pytest.raises(HTTPException) as raised,
    ):
        await responses_module._ensure_response_session_client(
            body, _resolved(), backend, session, "sess-1", True, None, "/tmp/ws"
        )
    # A new session (this call's is_new_session=True) was never handed out, so
    # it is finished; a continuation's would keep its CLI session-env dir (see
    # SessionManager._release_session_dirs).
    delete.assert_awaited_once_with("sess-1", finished=True)
    return raised.value


class TestRequestPathMapping:
    async def test_create_client_band_exhausted_is_not_retry_shortly(self):
        err = await _ensure_client_with(ui.UidBandExhausted("stable uid band full"))
        assert err.status_code == 500
        assert err.headers == {"x-should-retry": "false"}
        assert err.detail["error"]["code"] == "uid_band_exhausted"
        message = err.detail["error"]["message"]
        assert "retry shortly" not in message and "CLAUDE_ISOLATION_UID_MAX" in message

    @pytest.mark.parametrize(
        "exc",
        [ui.UidAllocationError("ephemeral pool momentarily full"), RuntimeError("x")],
    )
    async def test_other_create_client_failures_stay_retryable(self, exc):
        err = await _ensure_client_with(exc)
        assert err.status_code == 503
        assert not err.headers
        assert err.detail == "claude backend unavailable; retry shortly"

    async def test_the_error_handler_keeps_exception_headers(self):
        # The OpenAI-style handler dropped them: x-should-retry here, and the
        # WWW-Authenticate / Retry-After headers other routes set.
        from src.main import http_exception_handler

        for detail in ({"error": {"code": "x"}}, "plain"):
            resp = await http_exception_handler(
                None,
                HTTPException(
                    status_code=500, detail=detail, headers={"x-should-retry": "false"}
                ),
            )
            assert resp.headers["x-should-retry"] == "false"

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            (ui.UidBandExhausted("stable uid band full"), 500),
            (ui.UidAllocationError("ephemeral pool momentarily full"), 503),
        ],
    )
    async def test_slash_preflight_uid_failures(self, monkeypatch, exc, status):
        # Used to escape as an unhandled 500 whatever the cause.
        from src.routes import responses as responses_module

        async def boom(prompt, cwd=None, user=None):
            raise exc

        monkeypatch.setattr(responses_module, "validate_slash_prompt", boom)
        with pytest.raises(HTTPException) as raised:
            await responses_module._validate_backend_prompt(
                _resolved(), "/review", "/tmp/ws", "alice"
            )
        assert raised.value.status_code == status
        if status == 500:
            assert raised.value.detail["error"]["code"] == "uid_band_exhausted"
        else:
            assert "retry shortly" in raised.value.detail

    async def test_slash_command_errors_stay_400(self, monkeypatch):
        from src.backends.claude.slash_commands import SlashCommandError
        from src.routes import responses as responses_module

        async def refuse(prompt, cwd=None, user=None):
            raise SlashCommandError(code="unknown_command", message="no")

        monkeypatch.setattr(responses_module, "validate_slash_prompt", refuse)
        with pytest.raises(HTTPException) as raised:
            await responses_module._validate_backend_prompt(
                _resolved(), "/nope", "/tmp/ws", "alice"
            )
        assert raised.value.status_code == 400

    def test_the_wire_response_names_the_cause(self, isolated_session_manager):
        from tests.test_responses_more_coverage import DEFAULT_MODEL, client_context

        async def full(**_kwargs):
            raise ui.UidBandExhausted("stable uid band [20000, 29743] is exhausted")

        with client_context() as (client, mock_cli, _wm):
            mock_cli.create_client = full
            resp = client.post(
                "/v1/responses", json={"model": DEFAULT_MODEL, "input": "hello"}
            )
        assert resp.status_code == 500
        error = resp.json()["error"]
        assert error["code"] == "uid_band_exhausted"
        assert error["type"] == "server_error"
        assert "29743" not in error["message"]  # band layout stays operator-side


class TestWorkspaceRefusedMapping:
    """A confined spawn that refuses its workspace (a planted symlink, a layout
    the isolation checks reject) lasts until an operator fixes it: a
    non-retryable 500 ``workspace_refused`` on both request paths, with the
    path and the reason kept in the gateway log."""

    _SECRET = "/srv/ws/alice/claude/.cli-tmp/link"

    def _refused(self):
        return landlock.WorkspaceRefused(
            f"refusing a confined spawn in {self._SECRET}: it is a symlink"
        )

    def _assert_mapped(self, err, caplog):
        assert err.status_code == 500
        assert err.headers == {"x-should-retry": "false"}
        error = err.detail["error"]
        assert error["code"] == "workspace_refused"
        assert error["type"] == "server_error"
        assert "retry shortly" not in error["message"]
        assert self._SECRET not in error["message"]  # no paths to the caller
        assert self._SECRET in caplog.text  # the operator sees them

    async def test_create_client(self, caplog):
        with caplog.at_level(logging.ERROR):
            err = await _ensure_client_with(self._refused())
        self._assert_mapped(err, caplog)

    async def test_slash_preflight(self, monkeypatch, caplog):
        # Used to escape the slash path as an unhandled 500.
        from src.routes import responses as responses_module

        async def refuse(prompt, cwd=None, user=None):
            raise self._refused()

        monkeypatch.setattr(responses_module, "validate_slash_prompt", refuse)
        with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as raised:
            await responses_module._validate_backend_prompt(
                _resolved(), "/review", "/tmp/ws", "alice"
            )
        self._assert_mapped(raised.value, caplog)

    def test_the_wire_response(self, isolated_session_manager):
        from tests.test_responses_more_coverage import DEFAULT_MODEL, client_context

        async def refuse(**_kwargs):
            raise self._refused()

        with client_context() as (client, mock_cli, _wm):
            mock_cli.create_client = refuse
            resp = client.post(
                "/v1/responses", json={"model": DEFAULT_MODEL, "input": "hello"}
            )
        assert resp.status_code == 500
        assert resp.headers["x-should-retry"] == "false"
        error = resp.json()["error"]
        assert error["code"] == "workspace_refused"
        assert self._SECRET not in error["message"]
