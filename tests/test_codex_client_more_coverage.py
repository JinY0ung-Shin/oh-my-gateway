"""Additional coverage tests for src/backends/codex/client.py.

Targets uncovered lines found in the 93%-coverage run:
  186, 189, 228-230, 232, 234, 259-260, 262-263, 265, 267-270, 284-285, 302,
  370, 385, 389, 426, 554, 579, 583, 593-595, 802, 819-820, 936-940, 942-945,
  1027, 1038-1039, 1066, 1069, 1098, 1137, 1156-1160, 1189, 1207, 1217,
  1326, 1450, 1452, 1457, 1527.
"""

import asyncio
import queue
import subprocess
import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class FakeRpc:
    """Minimal fake that mimics CodexJsonRpcClient for CodexClient tests."""

    def __init__(self):
        self.closed = False
        self._pending_notifications = deque()
        self.thread_start_calls = []
        self.thread_resume_calls = []
        self.turn_start_calls = []
        self.respond_calls = []
        self.notifications = []

    def start(self):
        pass

    def close(self):
        self.closed = True

    def is_running(self):
        return not self.closed

    def thread_start(self, params):
        self.thread_start_calls.append(params)
        return {"thread": {"id": "thr_codex"}}

    def thread_resume(self, thread_id, params):
        self.thread_resume_calls.append((thread_id, params))
        return {"thread": {"id": thread_id}}

    def turn_start(self, thread_id, input_items, params):
        self.turn_start_calls.append((thread_id, input_items, params))
        return {"turn": {"id": "turn_1", "status": "inProgress"}}

    def next_notification(self):
        if not self.notifications:
            raise AssertionError("test exhausted notifications")
        return self.notifications.pop(0)

    def respond(self, request_id, result):
        self.respond_calls.append((request_id, result))


def _turn_completed_notification():
    return {
        "method": "turn/completed",
        "params": {
            "threadId": "thr_codex",
            "turn": {"id": "turn_1", "status": "completed", "items": []},
        },
    }


# ---------------------------------------------------------------------------
# CodexJsonRpcClient — lines 186, 189
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientStart:
    """Tests for CodexJsonRpcClient.start() guard and config-override path."""

    def test_start_already_running_is_noop(self, monkeypatch):
        """start() returns immediately if _proc is already set (line 186)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "/bin/codex")
        client = CodexJsonRpcClient()
        # Inject a fake proc so _proc is not None
        client._proc = MagicMock()
        # Should not raise and should not call subprocess.Popen again
        with patch("subprocess.Popen") as mock_popen:
            client.start()
            mock_popen.assert_not_called()

    def test_start_uses_config_overrides(self, monkeypatch):
        """start() passes --config for each override entry (line 189)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient(config_overrides=["a=1", "b=2"])

        launched_args = {}

        def fake_popen(args, **kwargs):
            launched_args["args"] = args
            proc = MagicMock()
            proc.stdin = MagicMock()
            proc.stdout = MagicMock()
            proc.stdout.__iter__ = lambda s: iter([])
            proc.stderr = MagicMock()
            proc.stderr.__iter__ = lambda s: iter([])
            proc.poll = lambda: None
            return proc

        with patch("subprocess.Popen", side_effect=fake_popen):
            with patch.object(client, "_initialize"):
                with patch.object(client, "_start_stdout_drain_thread"):
                    with patch.object(client, "_start_stderr_drain_thread"):
                        client.start()

        args = launched_args["args"]
        assert args.count("--config") == 2
        assert "a=1" in args
        assert "b=2" in args


# ---------------------------------------------------------------------------
# CodexJsonRpcClient.close() — lines 228-230, 232, 234
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientClose:
    """Tests for close() branching paths."""

    def test_close_kills_process_when_terminate_times_out(self, monkeypatch):
        """If terminate() doesn't finish in time, kill() is called (lines 228-230)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.poll.return_value = None  # process alive
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("codex", 2), None]
        client._proc = mock_proc

        client.close()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        assert client._proc is None

    def test_close_joins_stdout_thread(self, monkeypatch):
        """close() joins the stdout thread if it is alive (line 232)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.poll.return_value = 0  # already exited
        client._proc = mock_proc

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        client._stdout_thread = mock_thread

        client.close()

        mock_thread.join.assert_called_once()

    def test_close_joins_stderr_thread(self, monkeypatch):
        """close() joins the stderr thread if it is alive (line 234)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.poll.return_value = 0
        client._proc = mock_proc

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        client._stderr_thread = mock_thread

        client.close()

        mock_thread.join.assert_called_once()


# ---------------------------------------------------------------------------
# CodexJsonRpcClient.request() — lines 259-270
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientRequest:
    """Tests for the request() loop branching paths."""

    def _make_client(self, monkeypatch):
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        client._proc = mock_proc
        return client

    def test_request_skips_messages_with_wrong_id(self, monkeypatch):
        """Messages with a non-matching id are discarded (line 265)."""
        client = self._make_client(monkeypatch)
        correct_id = None

        def fake_write(payload):
            nonlocal correct_id
            if "id" in payload and "method" in payload:
                correct_id = payload["id"]

        messages = [None]  # will be filled after we know the id

        def fake_read():
            msg = messages.pop(0)
            return msg

        wrong_msg = {"id": "wrong-id", "result": "ignore"}
        correct_msg = {"result": "ok"}  # will have correct id patched in

        call_count = [0]

        def fake_read2():
            call_count[0] += 1
            if call_count[0] == 1:
                return wrong_msg
            # Second read: return the correct response
            return {"id": correct_id, "result": "ok"}

        with patch.object(client, "_write_message", side_effect=fake_write):
            with patch.object(client, "_read_message", side_effect=fake_read2):
                result = client.request("ping", {})

        assert result == "ok"

    def test_request_queues_server_requests_with_approval_method(self, monkeypatch):
        """Server requests with approval method are queued as pending (lines 259-260)."""
        from src.backends.codex.client import CODEX_APPROVAL_METHODS

        client = self._make_client(monkeypatch)
        approval_method = next(iter(CODEX_APPROVAL_METHODS))
        correct_id = [None]

        def fake_write(payload):
            if "id" in payload and "method" in payload:
                correct_id[0] = payload["id"]

        call_count = [0]

        def fake_read():
            call_count[0] += 1
            if call_count[0] == 1:
                # A server request with id AND approval method — should be queued
                return {"id": "srv_req_1", "method": approval_method, "params": {}}
            # Final: matching response for our request
            return {"id": correct_id[0], "result": "done"}

        with patch.object(client, "_write_message", side_effect=fake_write):
            with patch.object(client, "_read_message", side_effect=fake_read):
                result = client.request("ping", {})

        assert result == "done"
        assert len(client._pending_notifications) == 1
        queued = client._pending_notifications[0]
        assert queued["method"] == approval_method

    def test_request_queues_server_notifications(self, monkeypatch):
        """Server notifications (method but no id) are queued (lines 262-263)."""
        client = self._make_client(monkeypatch)
        correct_id = [None]

        def fake_write(payload):
            if "id" in payload and "method" in payload:
                correct_id[0] = payload["id"]

        call_count = [0]

        def fake_read():
            call_count[0] += 1
            if call_count[0] == 1:
                # A plain notification (method, no id) — should be queued
                return {"method": "some/notification", "params": {}}
            return {"id": correct_id[0], "result": "done"}

        with patch.object(client, "_write_message", side_effect=fake_write):
            with patch.object(client, "_read_message", side_effect=fake_read):
                result = client.request("ping", {})

        assert result == "done"
        assert len(client._pending_notifications) == 1

    def test_request_raises_on_dict_error(self, monkeypatch):
        """Dict error payloads raise CodexAppServerError using the message field (line 268-269)."""
        from src.backends.codex.client import CodexAppServerError

        client = self._make_client(monkeypatch)
        correct_id = [None]

        def fake_write(payload):
            if "id" in payload and "method" in payload:
                correct_id[0] = payload["id"]

        def fake_read():
            return {"id": correct_id[0], "error": {"message": "server blew up"}}

        with patch.object(client, "_write_message", side_effect=fake_write):
            with patch.object(client, "_read_message", side_effect=fake_read):
                with pytest.raises(CodexAppServerError, match="server blew up"):
                    client.request("ping", {})

    def test_request_raises_on_non_dict_error(self, monkeypatch):
        """Non-dict error payloads still raise CodexAppServerError (line 270)."""
        from src.backends.codex.client import CodexAppServerError

        client = self._make_client(monkeypatch)
        correct_id = [None]

        def fake_write(payload):
            if "id" in payload and "method" in payload:
                correct_id[0] = payload["id"]

        def fake_read():
            return {"id": correct_id[0], "error": "string error"}

        with patch.object(client, "_write_message", side_effect=fake_write):
            with patch.object(client, "_read_message", side_effect=fake_read):
                with pytest.raises(CodexAppServerError):
                    client.request("ping", {})

    def test_request_handles_unknown_server_request(self, monkeypatch, caplog):
        """Non-approval server requests with id get a response and a warning (lines 259-260)."""
        client = self._make_client(monkeypatch)
        correct_id = [None]
        written = []

        def fake_write(payload):
            written.append(payload)
            if "id" in payload and "method" in payload:
                correct_id[0] = payload["id"]

        call_count = [0]

        def fake_read():
            call_count[0] += 1
            if call_count[0] == 1:
                # Unknown server request (has id + method, not an approval method)
                return {"id": "srv_1", "method": "unknown/serverMethod", "params": {}}
            return {"id": correct_id[0], "result": "final"}

        import logging

        with caplog.at_level(logging.WARNING, logger="src.backends.codex.client"):
            with patch.object(client, "_write_message", side_effect=fake_write):
                with patch.object(client, "_read_message", side_effect=fake_read):
                    result = client.request("ping", {})

        assert result == "final"
        # The client should have responded to the unknown server request
        response_msgs = [m for m in written if m.get("id") == "srv_1"]
        assert response_msgs, "should have written a response to the server request"


# ---------------------------------------------------------------------------
# CodexJsonRpcClient.next_notification() — lines 284-285
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientNextNotification:
    """Tests for next_notification() branching."""

    def _make_client(self, monkeypatch):
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        client._proc = mock_proc
        return client

    def test_next_notification_handles_server_request_in_read_loop(self, monkeypatch):
        """Non-approval server requests in next_notification get a response (lines 284-285)."""
        client = self._make_client(monkeypatch)
        written = []

        def fake_write(payload):
            written.append(payload)

        call_count = [0]

        def fake_read():
            call_count[0] += 1
            if call_count[0] == 1:
                # Server request (has id + method, not an approval method)
                return {"id": "srv_99", "method": "unknown/method", "params": {}}
            # notification (method, no id)
            return {"method": "my/notification", "params": {"data": 1}}

        with patch.object(client, "_write_message", side_effect=fake_write):
            with patch.object(client, "_read_message", side_effect=fake_read):
                result = client.next_notification()

        assert result["method"] == "my/notification"
        # Should have responded to the server request
        response_msgs = [m for m in written if m.get("id") == "srv_99"]
        assert response_msgs


# ---------------------------------------------------------------------------
# CodexJsonRpcClient.thread_resume() — line 302
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientThreadResume:
    """Tests for thread_resume() error path."""

    def test_thread_resume_raises_on_non_dict_result(self, monkeypatch):
        """thread_resume() raises CodexAppServerError when result is not a dict (line 302)."""
        from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()

        with patch.object(client, "request", return_value=None):
            with pytest.raises(CodexAppServerError, match="thread/resume"):
                client.thread_resume("thr_1", {})


# ---------------------------------------------------------------------------
# _start_stdout_drain_thread — line 370
# _start_stderr_drain_thread — lines 385, 389
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientDrainThreads:
    """Tests for the drain thread early-exit guards."""

    def test_stdout_drain_thread_no_op_when_no_proc(self, monkeypatch):
        """_start_stdout_drain_thread is a no-op when _proc is None (line 370)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()
        # _proc is None by default — should not raise
        client._start_stdout_drain_thread()
        assert client._stdout_thread is None

    def test_stderr_drain_thread_no_op_when_no_proc(self, monkeypatch):
        """_start_stderr_drain_thread is a no-op when _proc is None (line 385)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()
        client._start_stderr_drain_thread()
        assert client._stderr_thread is None

    def test_stderr_drain_inner_guard_exits_when_proc_clears(self, monkeypatch):
        """The inner _drain() inside stderr drain exits early if _proc clears (line 389)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()

        mock_proc = MagicMock()
        # Provide a real stderr iterator that yields nothing
        mock_proc.stderr = iter([])
        client._proc = mock_proc

        # Clear _proc before the drain thread reads it
        client._proc = None

        # Start the thread; inner guard fires immediately, thread finishes cleanly
        client._start_stderr_drain_thread()
        if client._stderr_thread:
            client._stderr_thread.join(timeout=1.0)
        # No assertion needed — just must not hang or raise


# ---------------------------------------------------------------------------
# CodexClient.create_client() error path — line 554, 426
# ---------------------------------------------------------------------------


class TestCodexSessionClientDisconnect:
    """Tests for CodexSessionClient.disconnect()."""

    async def test_disconnect_closes_rpc_when_owns_rpc(self):
        """disconnect() calls rpc.close() when owns_rpc=True (line 426)."""
        from src.backends.codex.client import CodexSessionClient

        fake_rpc = FakeRpc()
        client = CodexSessionClient(
            rpc=fake_rpc,
            thread_id="thr_1",
            model=None,
            cwd=None,
            owns_rpc=True,
        )
        await client.disconnect()
        assert fake_rpc.closed

    async def test_disconnect_is_noop_when_not_owns_rpc(self):
        """disconnect() is a no-op when owns_rpc=False."""
        from src.backends.codex.client import CodexSessionClient

        fake_rpc = FakeRpc()
        client = CodexSessionClient(
            rpc=fake_rpc,
            thread_id="thr_1",
            model=None,
            cwd=None,
            owns_rpc=False,
        )
        await client.disconnect()
        assert not fake_rpc.closed


class TestCodexClientCreateClientErrors:
    """Tests for create_client() edge cases."""

    async def test_create_client_raises_when_thread_start_returns_invalid_id(self, monkeypatch):
        """create_client() raises when thread/start response is missing thread.id (line 554)."""
        from src.backends.codex.client import CodexAppServerError, CodexClient

        fake_rpc = FakeRpc()

        def bad_thread_start(params):
            return {"thread": {}}  # missing "id"

        fake_rpc.thread_start = bad_thread_start
        monkeypatch.setattr(
            "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc
        )

        backend = CodexClient()
        session = SimpleNamespace(session_id="gw-session")

        with pytest.raises(CodexAppServerError, match="thread.id"):
            await backend.create_client(session=session)

    async def test_create_client_passes_model_params(self, monkeypatch):
        """create_client() stores model_params on the session client (line 426 area)."""
        fake_rpc = FakeRpc()
        fake_rpc.notifications = []
        monkeypatch.setattr(
            "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc
        )

        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        session = SimpleNamespace(session_id="gw-session")
        client = await backend.create_client(
            session=session, model_params={"temperature": 0.5}
        )

        assert client.model_params == {"temperature": 0.5}


# ---------------------------------------------------------------------------
# CodexClient._ensure_rpc_locked() — lines 579, 583, 593-595
# ---------------------------------------------------------------------------


class TestCodexClientEnsureRpcLocked:
    """Tests for _ensure_rpc_locked() env-change and start-failure paths."""

    async def test_ensure_rpc_locked_replaces_rpc_on_env_change(self, monkeypatch):
        """When env changes, the old RPC is closed and a new one is started (line 579)."""
        fake_rpc1 = FakeRpc()
        new_rpcs = []

        def make_rpc(**kwargs):
            rpc = FakeRpc()
            new_rpcs.append(rpc)
            return rpc

        monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", make_rpc)
        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        monkeypatch.setattr("src.backends.codex.client.configured_config_overrides", lambda: [])

        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        # Pre-seed the shared RPC with env1
        backend._rpc = fake_rpc1
        backend._rpc_env = {"X": "1"}

        # Call with different env — should close rpc1 and start a new RPC
        async with backend._rpc_lock:
            rpc = await backend._ensure_rpc_locked({"X": "2"})

        assert fake_rpc1.closed
        assert len(new_rpcs) == 1
        assert rpc is new_rpcs[0]
        assert not new_rpcs[0].closed

    async def test_ensure_rpc_locked_replaces_dead_rpc(self, monkeypatch):
        """When the existing RPC is not running, a new one is started (line 583)."""
        dead_rpc = FakeRpc()
        dead_rpc.closed = True  # is_running() returns False
        new_rpc = FakeRpc()
        rpcs = [new_rpc]

        def make_rpc(**kwargs):
            return rpcs.pop(0)

        monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", make_rpc)
        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        monkeypatch.setattr("src.backends.codex.client.configured_config_overrides", lambda: [])

        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        backend._rpc = dead_rpc
        backend._rpc_env = {}

        async with backend._rpc_lock:
            rpc = await backend._ensure_rpc_locked({})

        assert rpc is new_rpc

    async def test_ensure_rpc_locked_closes_rpc_when_start_fails(self, monkeypatch):
        """If the new RPC's start() raises, the RPC is closed and exception propagates (lines 593-595)."""
        from src.backends.codex.client import CodexClient

        class FailingRpc(FakeRpc):
            def start(self):
                raise RuntimeError("failed to start")

        failing_rpc = FailingRpc()
        monkeypatch.setattr(
            "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: failing_rpc
        )
        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        monkeypatch.setattr("src.backends.codex.client.configured_config_overrides", lambda: [])

        backend = CodexClient()

        with pytest.raises(RuntimeError, match="failed to start"):
            async with backend._rpc_lock:
                await backend._ensure_rpc_locked({})

        assert failing_rpc.closed


# ---------------------------------------------------------------------------
# CodexClient._coerce_turn_input_items() — lines 802, 819-820
# ---------------------------------------------------------------------------


class TestCodexClientCoerceTurnInputItems:
    """Tests for _coerce_turn_input_items() edge cases."""

    def test_coerce_list_with_valid_dicts(self):
        """A list of dicts is forwarded verbatim (line 801 area)."""
        from src.backends.codex.client import CodexClient

        items = [{"type": "text", "text": "hi"}, {"type": "image", "url": "x"}]
        result = CodexClient._coerce_turn_input_items(items)
        assert result == items

    def test_coerce_list_raises_on_non_dict_item(self):
        """A list containing a non-dict item raises ValueError (lines 802 area)."""
        from src.backends.codex.client import CodexClient

        with pytest.raises(ValueError, match="must be a dict"):
            CodexClient._coerce_turn_input_items(["not a dict"])

    def test_coerce_unsupported_type_raises(self):
        """Anything that's not a str or list raises ValueError (lines 819-820)."""
        from src.backends.codex.client import CodexClient

        with pytest.raises(ValueError, match="string or list"):
            CodexClient._coerce_turn_input_items(42)


# ---------------------------------------------------------------------------
# CodexClient.resume_approval_with_client() — lines 936-940, 942-945
# ---------------------------------------------------------------------------


class TestCodexClientResumeApprovalWithClient:
    """Tests for resume_approval_with_client() error paths."""

    def _make_backend_and_client(self, monkeypatch, rpc):
        monkeypatch.setattr(
            "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: rpc
        )
        from src.backends.codex.client import CodexClient

        return CodexClient()

    async def test_resume_approval_yields_error_on_rpc_exception(self, monkeypatch):
        """When an exception occurs inside resume_approval, error_chunk is yielded (lines 942-945)."""
        fake_rpc = FakeRpc()
        backend = self._make_backend_and_client(monkeypatch, fake_rpc)

        from src.backends.codex.client import CodexSessionClient

        session = SimpleNamespace(session_id="gw-session")
        client = CodexSessionClient(
            rpc=fake_rpc,
            thread_id="thr_1",
            model=None,
            cwd=None,
            pending_approval_request_id="appr_1",
            pending_approval_method="item/commandExecution/requestApproval",
            pending_approval_turn_id="turn_1",
            pending_approval_params={"threadId": "thr_1", "turnId": "turn_1"},
            pending_approval_rpc=fake_rpc,
        )

        # Make rpc.respond raise to trigger the exception path
        def failing_respond(request_id, result):
            raise RuntimeError("transport dead")

        fake_rpc.respond = failing_respond

        chunks = [
            chunk
            async for chunk in backend.resume_approval_with_client(
                client, "appr_1", "accept", session
            )
        ]

        assert len(chunks) == 1
        assert chunks[0]["type"] == "error"

    async def test_resume_approval_yields_approval_request_if_another_follows(self, monkeypatch):
        """A second approval during resume streams back out as another tool_use (lines 936-940)."""
        fake_rpc = FakeRpc()
        fake_rpc.notifications = [
            {
                "id": "appr_2",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thr_codex",
                    "turnId": "turn_1",
                    "itemId": "cmd_2",
                    "command": "ls",
                    "availableDecisions": ["accept", "decline"],
                },
            }
        ]
        backend = self._make_backend_and_client(monkeypatch, fake_rpc)

        from src.backends.codex.client import CodexSessionClient

        session = SimpleNamespace(session_id="gw-session", pending_tool_call=None)
        client = CodexSessionClient(
            rpc=fake_rpc,
            thread_id="thr_codex",
            model=None,
            cwd=None,
            pending_approval_request_id="appr_1",
            pending_approval_method="item/commandExecution/requestApproval",
            pending_approval_turn_id="turn_1",
            pending_approval_params={"threadId": "thr_codex", "turnId": "turn_1"},
            pending_approval_rpc=fake_rpc,
        )

        # respond is a no-op
        chunks = [
            chunk
            async for chunk in backend.resume_approval_with_client(
                client, "appr_1", "accept", session
            )
        ]

        # Second approval should surface as assistant tool_use
        assert any(c.get("type") == "assistant" for c in chunks)
        assert session.pending_tool_call is not None


# ---------------------------------------------------------------------------
# CodexClient._pending_notification_count() — lines 819-820 (TypeError branch)
# ---------------------------------------------------------------------------


class TestCodexClientPendingNotificationCount:
    """Tests for _pending_notification_count() edge cases."""

    def test_returns_zero_for_missing_attribute(self, monkeypatch):
        """Returns 0 when rpc has no _pending_notifications (line 1027)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()

        class Rpc:
            pass

        assert backend._pending_notification_count(Rpc()) == 0

    def test_returns_zero_for_unsized_attribute(self, monkeypatch):
        """Returns 0 when _pending_notifications has no len() (lines 1038-1039)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()

        class BadRpc:
            _pending_notifications = object()  # has no len()

        assert backend._pending_notification_count(BadRpc()) == 0


# ---------------------------------------------------------------------------
# CodexClient._auto_deny_approval() — lines 1066, 1069
# ---------------------------------------------------------------------------


class TestCodexClientAutoDenyApproval:
    """Tests for _auto_deny_approval() edge cases."""

    def test_auto_deny_no_op_when_no_id(self):
        """_auto_deny_approval is a no-op when notification has no id (line 1066)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        fake_rpc = FakeRpc()
        # No "id" key in the notification
        backend._auto_deny_approval(fake_rpc, {"method": "item/commandExecution/requestApproval"})
        assert fake_rpc.respond_calls == []

    def test_auto_deny_uses_permissions_result_for_permissions_method(self):
        """_auto_deny_approval uses permissions result for permissions method (line 1069)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        fake_rpc = FakeRpc()
        backend._auto_deny_approval(
            fake_rpc,
            {"id": "r1", "method": "item/permissions/requestApproval"},
        )
        assert fake_rpc.respond_calls == [("r1", {"permissions": {}, "scope": "turn"})]


# ---------------------------------------------------------------------------
# CodexClient._auto_accept_approval() — line 1098
# ---------------------------------------------------------------------------


class TestCodexClientAutoAcceptApproval:
    """Tests for _auto_accept_approval() edge cases."""

    def test_auto_accept_no_op_when_no_id(self):
        """_auto_accept_approval is a no-op when notification has no id (line 1098)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        fake_rpc = FakeRpc()
        backend._auto_accept_approval(
            fake_rpc, {"method": "item/fileChange/requestApproval"}
        )
        assert fake_rpc.respond_calls == []


# ---------------------------------------------------------------------------
# CodexClient._chunks_from_notification() turn_id filter — line 1137
# plus turn failed path — line 1189, 1207 (turn failed message)
# _is_terminal_notification approval path — line 1217
# ---------------------------------------------------------------------------


class TestCodexClientChunksFromNotification:
    """Tests for _chunks_from_notification() branching."""

    def _backend(self):
        from src.backends.codex.client import CodexClient

        return CodexClient()

    def test_notification_with_wrong_turn_id_is_skipped(self):
        """Notifications for a different turn_id are silently ignored (line 1137)."""
        backend = self._backend()
        items = []
        usage_box = {"usage": None}
        chunks = list(
            backend._chunks_from_notification(
                thread_id="thr",
                turn_id="turn_1",
                notification={
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thr", "turnId": "OTHER_TURN", "delta": "x"},
                },
                items=items,
                usage_box=usage_box,
            )
        )
        assert chunks == []

    def test_turn_completed_with_failed_status_yields_error_chunk(self):
        """turn/completed with status=failed yields an error chunk (line 1189-1192)."""
        backend = self._backend()
        items = []
        usage_box = {"usage": None}
        chunks = list(
            backend._chunks_from_notification(
                thread_id="thr",
                turn_id="turn_1",
                notification={
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thr",
                        "turn": {
                            "id": "turn_1",
                            "status": "failed",
                            "error": {"message": "model overloaded"},
                        },
                    },
                },
                items=items,
                usage_box=usage_box,
            )
        )
        assert len(chunks) == 1
        assert chunks[0]["type"] == "error"
        assert "model overloaded" in chunks[0]["error_message"]

    def test_turn_completed_failed_without_error_message(self):
        """turn/completed failed without error.message uses fallback text (line 1207)."""
        backend = self._backend()
        items = []
        usage_box = {"usage": None}
        chunks = list(
            backend._chunks_from_notification(
                thread_id="thr",
                turn_id="turn_1",
                notification={
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thr",
                        "turn": {"id": "turn_1", "status": "failed"},
                    },
                },
                items=items,
                usage_box=usage_box,
            )
        )
        assert len(chunks) == 1
        assert chunks[0]["type"] == "error"
        assert chunks[0]["error_message"] == "Codex turn failed"

    def test_is_terminal_notification_approval_request(self):
        """Approval requests are terminal in the notification iterator (line 1217)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        notification = {
            "id": "appr_1",
            "method": "item/commandExecution/requestApproval",
            "params": {},
        }
        assert backend._is_terminal_notification(
            thread_id="thr", turn_id="turn_1", notification=notification
        )

    def test_unknown_method_same_turn_id_returns_nothing(self):
        """An unknown method for the correct turn_id returns early (line 1189)."""
        backend = self._backend()
        items = []
        usage_box = {"usage": None}
        chunks = list(
            backend._chunks_from_notification(
                thread_id="thr",
                turn_id="turn_1",
                notification={
                    "method": "some/unknown/method",
                    "params": {"turnId": "turn_1"},
                },
                items=items,
                usage_box=usage_box,
            )
        )
        assert chunks == []

    def test_is_terminal_notification_returns_false_for_non_dict_params(self):
        """_is_terminal_notification returns False when params is not a dict (line 1207)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        notification = {"method": "turn/completed", "params": "not-a-dict"}
        assert not backend._is_terminal_notification(
            thread_id="thr", turn_id="turn_1", notification=notification
        )


# ---------------------------------------------------------------------------
# CodexClient._approval_result_from_output() — lines 1326, 1450, 1452, 1457
# ---------------------------------------------------------------------------


class TestCodexClientApprovalResultFromOutput:
    """Tests for _approval_result_from_output() branching."""

    def _backend(self):
        from src.backends.codex.client import CodexClient

        return CodexClient()

    def test_permissions_method_with_dict_and_permissions_key(self):
        """Dict output with 'permissions' for permissions method is returned directly (line 1450)."""
        backend = self._backend()
        permissions = {"fileSystem": {"read": ["/"]}}
        result = backend._approval_result_from_output(
            "item/permissions/requestApproval",
            '{"permissions": {"fileSystem": {"read": ["/"]}}, "scope": "turn"}',
            {"permissions": permissions},
        )
        assert result == {"permissions": permissions, "scope": "turn"}

    def test_dict_output_with_decision_key(self):
        """Dict output with 'decision' key is extracted as decision (line 1452)."""
        backend = self._backend()
        result = backend._approval_result_from_output(
            "item/commandExecution/requestApproval",
            '{"decision": "accept"}',
            {},
        )
        assert result == {"decision": "accept"}

    def test_dict_output_for_command_method_without_decision(self):
        """Dict output for command method without decision key is forwarded as-is (line 1457)."""
        backend = self._backend()
        # A dict that does not have 'decision' key, for a command method
        # → forwarded as {"decision": <the dict>}
        result = backend._approval_result_from_output(
            "item/commandExecution/requestApproval",
            '{"acceptWithExecpolicyAmendment": {}}',
            {},
        )
        assert result == {"decision": {"acceptWithExecpolicyAmendment": {}}}

    def test_approval_decision_label_for_complex_network_without_host(self):
        """applyNetworkPolicyAmendment without action+host falls back to bare label."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        # network_policy_amendment missing host → fallback
        decision = {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {"action": "allow"}
                # "host" is absent
            }
        }
        label = backend._approval_decision_label(decision)
        assert label == "applyNetworkPolicyAmendment"

    def test_approval_options_skips_decision_with_empty_label(self):
        """_approval_options skips decisions whose label is empty (line 1326 - continue)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        # Empty dict → _approval_decision_label returns ""  → skipped via continue
        params = {"availableDecisions": [{}, "accept"]}
        options = backend._approval_options("command", params)
        # Only "accept" should be in options; the empty dict should be skipped
        labels = [o["label"] for o in options]
        assert "accept" in labels
        # Empty dict produced no label and was skipped
        assert all(lbl != "" for lbl in labels)


# ---------------------------------------------------------------------------
# CodexClient._is_thread_idle_notification() — line 1527
# ---------------------------------------------------------------------------


class TestCodexClientIsThreadIdleNotification:
    """Tests for _is_thread_idle_notification() edge cases."""

    def _backend(self):
        from src.backends.codex.client import CodexClient

        return CodexClient()

    def test_returns_false_for_non_idle_status_type(self):
        """Returns False when status.type != 'idle' (line 1527)."""
        backend = self._backend()
        notification = {
            "method": "thread/status/changed",
            "params": {
                "threadId": "thr",
                "status": {"type": "running"},
            },
        }
        assert not backend._is_thread_idle_notification("thr", notification)

    def test_returns_false_when_thread_id_does_not_match(self):
        """Returns False when threadId in params doesn't match (line 1527 guard)."""
        backend = self._backend()
        notification = {
            "method": "thread/status/changed",
            "params": {
                "threadId": "OTHER_THREAD",
                "status": {"type": "idle"},
            },
        }
        assert not backend._is_thread_idle_notification("thr", notification)

    def test_returns_false_when_params_is_not_dict(self):
        """Returns False when params is not a dict."""
        backend = self._backend()
        notification = {"method": "thread/status/changed", "params": None}
        assert not backend._is_thread_idle_notification("thr", notification)

    def test_returns_true_for_idle_status(self):
        """Returns True for a proper idle status/changed notification."""
        backend = self._backend()
        notification = {
            "method": "thread/status/changed",
            "params": {
                "threadId": "thr",
                "status": {"type": "idle"},
            },
        }
        assert backend._is_thread_idle_notification("thr", notification)


# ---------------------------------------------------------------------------
# CodexJsonRpcClient.notify() — line 274
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientNotify:
    """Tests for notify()."""

    def test_notify_sends_message_without_id(self, monkeypatch):
        """notify() writes a message with method and params but no id (line 274)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        client._proc = mock_proc

        written = []
        with patch.object(client, "_write_message", side_effect=written.append):
            client.notify("initialized", {"foo": "bar"})

        assert len(written) == 1
        msg = written[0]
        assert "id" not in msg
        assert msg["method"] == "initialized"
        assert msg["params"] == {"foo": "bar"}


# ---------------------------------------------------------------------------
# CodexJsonRpcClient.next_notification() — line 283 (approval in read loop)
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientNextNotificationApproval:
    """next_notification() returns approval requests immediately (line 283)."""

    def _make_client(self, monkeypatch):
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        client._proc = mock_proc
        return client

    def test_next_notification_returns_approval_immediately(self, monkeypatch):
        """Approval method server requests are returned directly from next_notification (line 283)."""
        from src.backends.codex.client import CODEX_APPROVAL_METHODS

        client = self._make_client(monkeypatch)
        approval_method = next(iter(CODEX_APPROVAL_METHODS))

        def fake_read():
            return {"id": "appr_99", "method": approval_method, "params": {}}

        with patch.object(client, "_read_message", side_effect=fake_read):
            result = client.next_notification()

        assert result["method"] == approval_method
        assert result["id"] == "appr_99"


# ---------------------------------------------------------------------------
# CodexJsonRpcClient.respond() — line 290
# CodexJsonRpcClient.thread_start() — line 296 (non-dict error)
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientRespond:
    """Tests for respond() and thread_start() errors."""

    def test_respond_sends_result(self, monkeypatch):
        """respond() writes a result message with the given id (line 290)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        client._proc = mock_proc

        written = []
        with patch.object(client, "_write_message", side_effect=written.append):
            client.respond("req_1", {"decision": "accept"})

        assert len(written) == 1
        assert written[0] == {"id": "req_1", "result": {"decision": "accept"}}

    def test_thread_start_raises_on_non_dict_result(self, monkeypatch):
        """thread_start() raises CodexAppServerError when result is not a dict (line 296)."""
        from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()

        with patch.object(client, "request", return_value=None):
            with pytest.raises(CodexAppServerError, match="thread/start"):
                client.thread_start({})


# ---------------------------------------------------------------------------
# CodexJsonRpcClient.is_running() — line 214
# ---------------------------------------------------------------------------


class TestCodexJsonRpcClientIsRunning:
    """Tests for is_running()."""

    def test_is_running_false_when_no_proc(self, monkeypatch):
        """is_running() returns False when _proc is None (line 214)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()
        assert not client.is_running()

    def test_is_running_true_when_proc_alive(self, monkeypatch):
        """is_running() returns True when proc.poll() is None (line 214)."""
        from src.backends.codex.client import CodexJsonRpcClient

        monkeypatch.setattr("src.backends.codex.client.codex_bin", lambda: "codex")
        client = CodexJsonRpcClient()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        client._proc = mock_proc
        assert client.is_running()


# ---------------------------------------------------------------------------
# _chunks_from_notification() — line 1137 (params not dict)
# and item/started with tool_use — line 1156-1160
# ---------------------------------------------------------------------------


class TestChunksFromNotificationAdditional:
    """Additional _chunks_from_notification() path tests."""

    def _backend(self):
        from src.backends.codex.client import CodexClient

        return CodexClient()

    def test_notification_with_non_dict_params_yields_nothing(self):
        """When params is not a dict, nothing is yielded (line 1137)."""
        backend = self._backend()
        items = []
        usage_box = {"usage": None}
        chunks = list(
            backend._chunks_from_notification(
                thread_id="thr",
                turn_id="turn_1",
                notification={"method": "item/started", "params": None},
                items=items,
                usage_box=usage_box,
            )
        )
        assert chunks == []

    def test_item_started_with_tool_item_yields_tool_use(self):
        """item/started with a valid tool item yields an assistant chunk (lines 1156-1160)."""
        backend = self._backend()
        items = []
        usage_box = {"usage": None}
        chunks = list(
            backend._chunks_from_notification(
                thread_id="thr",
                turn_id="turn_1",
                notification={
                    "method": "item/started",
                    "params": {
                        "turnId": "turn_1",
                        "item": {
                            "type": "commandExecution",
                            "id": "cmd_1",
                            "command": "ls",
                        },
                    },
                },
                items=items,
                usage_box=usage_box,
            )
        )
        assert len(chunks) == 1
        assert chunks[0]["type"] == "assistant"
        assert chunks[0]["content"][0]["type"] == "tool_use"
        assert chunks[0]["content"][0]["name"] == "commandExecution"

    def test_item_started_with_non_tool_item_yields_nothing(self):
        """item/started with an item type that is not a tool does not yield (lines 1157-1160)."""
        backend = self._backend()
        items = []
        usage_box = {"usage": None}
        chunks = list(
            backend._chunks_from_notification(
                thread_id="thr",
                turn_id="turn_1",
                notification={
                    "method": "item/started",
                    "params": {
                        "turnId": "turn_1",
                        "item": {"type": "agentMessage", "id": "msg_1"},
                    },
                },
                items=items,
                usage_box=usage_box,
            )
        )
        assert chunks == []


# ---------------------------------------------------------------------------
# _approval_tool_identities() — lines 1027, 1038-1039 (mcpToolCall without tool name)
# ---------------------------------------------------------------------------


class TestCodexClientApprovalToolIdentities:
    """Tests for _approval_tool_identities() paths."""

    def test_returns_empty_set_for_unknown_method(self):
        """Unknown method yields an empty set (line 1022)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        result = backend._approval_tool_identities({"method": "unknown/method", "params": {}})
        assert result == set()

    def test_returns_identities_with_non_dict_params(self):
        """Non-dict params returns just the base identity (line 1027)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        result = backend._approval_tool_identities(
            {
                "method": "item/commandExecution/requestApproval",
                "params": "not-a-dict",
            }
        )
        assert result == {"commandExecution"}

    def test_mcp_tool_call_without_tool_name_uses_wildcard_pattern(self):
        """MCP approval without toolName uses wildcard identity (lines 1038-1039)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        result = backend._approval_tool_identities(
            {
                "method": "item/mcpToolCall/requestApproval",
                "params": {"serverLabel": "my-server"},  # no toolName
            }
        )
        assert "mcp__my-server__*" in result
        assert "mcp__my_server__*" in result


# ---------------------------------------------------------------------------
# CodexClient._approval_result_from_output() — line 1326
# (applyNetworkPolicyAmendment decision path)
# ---------------------------------------------------------------------------


class TestApprovalResultFromOutputAdditional:
    """Additional _approval_result_from_output() tests."""

    def test_command_method_with_available_decision_match(self):
        """Structured decision is returned if label matches an available decision (line 1326 area)."""
        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        network_decision = {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {"action": "allow", "host": "example.com"}
            }
        }
        params = {"availableDecisions": [network_decision, "decline"]}
        result = backend._approval_result_from_output(
            "item/commandExecution/requestApproval",
            "applyNetworkPolicyAmendment:allow:example.com",
            params,
        )
        assert result == {"decision": network_decision}


# ---------------------------------------------------------------------------
# CodexClient._start_turn_with_retry() — retry logic after notification queued
# ---------------------------------------------------------------------------


class TestCodexClientStartTurnWithRetry:
    """Tests for _start_turn_with_retry() retry logic."""

    async def test_turn_start_retries_on_transport_failure(self, monkeypatch):
        """turn/start retries once when the first attempt fails with no new notifications."""
        fake_rpc = FakeRpc()
        call_count = [0]

        def flaky_turn_start(thread_id, input_items, params):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transport failed")
            return {"turn": {"id": "turn_1"}}

        fake_rpc.turn_start = flaky_turn_start
        monkeypatch.setattr(
            "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: fake_rpc
        )

        from src.backends.codex.client import CodexClient

        backend = CodexClient()
        session = SimpleNamespace(session_id="gw-session")
        client = await backend.create_client(session=session)

        async with backend._rpc_lock:
            rpc, turn = await backend._start_turn_with_retry(client, "hello")

        assert turn == {"turn": {"id": "turn_1"}}
        assert call_count[0] == 2
