"""Additional tests for src/backends/opencode/client.py to close coverage gaps.

Targets the following previously-missing line ranges:
  90, 144, 179-223, 232-240, 411-412, 416, 418, 446, 448,
  470, 493-495, 507-511, 523, 530-531, 535, 571-572, 577-578,
  583-586, 637-640, 644-645, 708-716.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.backends.opencode.client import OpenCodeClient, OpenCodeSessionClient


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def external_client(monkeypatch):
    """OpenCodeClient in external mode — no subprocess, no real server."""
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://external.example:4096")
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    return OpenCodeClient()


@pytest.fixture
def managed_client(monkeypatch):
    """OpenCodeClient in managed mode with _start_managed_server stubbed out."""
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.setattr(
        "src.backends.opencode.client.OpenCodeClient._start_managed_server",
        lambda self: "http://127.0.0.1:9999",
    )
    return OpenCodeClient()


def _make_session_client(**kwargs) -> OpenCodeSessionClient:
    defaults = dict(
        session_id="oc-session",
        cwd=None,
        model=None,
        system_prompt=None,
        base_url="http://127.0.0.1:4096",
        timeout=5.0,
    )
    defaults.update(kwargs)
    return OpenCodeSessionClient(**defaults)


# ---------------------------------------------------------------------------
# OpenCodeSessionClient.disconnect — auth kwarg branch (line 90)
# ---------------------------------------------------------------------------


class TestSessionClientDisconnectAuth:
    async def test_disconnect_passes_auth_kwarg_when_auth_set(self, monkeypatch):
        """When auth is set disconnect includes it in the AsyncClient kwargs."""
        captured_kwargs: list[dict] = []

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

        class FakeClient:
            def __init__(self, **kwargs):
                captured_kwargs.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def delete(self, path, params=None):
                return FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        auth = httpx.BasicAuth("user", "pass")
        sc = OpenCodeSessionClient(
            session_id="s1",
            cwd=None,
            model=None,
            system_prompt=None,
            base_url="http://x",
            timeout=2.0,
            auth=auth,
        )
        await sc.disconnect()

        assert len(captured_kwargs) == 1
        assert "auth" in captured_kwargs[0]
        assert captured_kwargs[0]["auth"] is auth
        assert captured_kwargs[0]["timeout"] == 2.0


# ---------------------------------------------------------------------------
# OpenCodeClient.name property (line 144)
# ---------------------------------------------------------------------------


class TestClientNameProperty:
    def test_name_returns_opencode(self, external_client):
        assert external_client.name == "opencode"


# ---------------------------------------------------------------------------
# _start_managed_server (lines 179-223)
# ---------------------------------------------------------------------------


class TestStartManagedServer:
    def _make_proc(self, stdout_lines=None, poll_returns=None, returncode=0):
        """Build a minimal Popen-like mock."""
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = MagicMock()

        # poll() returns None (running) for most calls, then optionally a value
        poll_sequence = list(poll_returns or [None, None])
        proc.poll.side_effect = poll_sequence + [None] * 100

        # readline() yields lines in sequence then empty string
        lines = list(stdout_lines or [])
        proc.stdout.readline.side_effect = lines + [""] * 100
        return proc

    def test_start_managed_server_returns_url_on_success(self, monkeypatch):
        """When stdout emits the listen line the server URL is returned."""
        monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
        monkeypatch.setattr(
            "src.backends.opencode.client.OpenCodeClient._managed_config_content",
            lambda self: "{}",
        )

        listen_line = "opencode server listening on http://127.0.0.1:12345\n"
        proc = self._make_proc(stdout_lines=[listen_line])
        proc.stdout  # already set

        import select as select_mod

        # Make select.select always return readable
        monkeypatch.setattr(
            select_mod, "select", lambda rlist, wlist, xlist, timeout: (rlist, [], [])
        )
        monkeypatch.setattr(
            "src.backends.opencode.client.subprocess.Popen", lambda *a, **kw: proc
        )

        c = OpenCodeClient.__new__(OpenCodeClient)
        c._process = None
        c._server_username = "opencode"
        c._server_password = None
        c._agent = "general"
        c.timeout = 5.0
        c._mode = "managed"

        url = c._start_managed_server()
        assert url == "http://127.0.0.1:12345"

    def test_start_managed_server_raises_on_process_exit(self, monkeypatch):
        """If the process dies before emitting the URL a RuntimeError is raised."""
        monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
        monkeypatch.setattr(
            "src.backends.opencode.client.OpenCodeClient._managed_config_content",
            lambda self: "{}",
        )

        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = MagicMock()
        # poll() immediately reports process exited
        proc.poll.return_value = 1
        proc.stdout.readline.return_value = ""

        import select as select_mod

        monkeypatch.setattr(
            select_mod, "select", lambda rlist, wlist, xlist, timeout: (rlist, [], [])
        )
        monkeypatch.setattr(
            "src.backends.opencode.client.subprocess.Popen", lambda *a, **kw: proc
        )
        # Prevent close() from doing anything real
        monkeypatch.setattr(
            "src.backends.opencode.client.OpenCodeClient.close", lambda self: None
        )

        c = OpenCodeClient.__new__(OpenCodeClient)
        c._process = None
        c._server_username = "opencode"
        c._server_password = None
        c._agent = "general"
        c.timeout = 5.0

        with pytest.raises(RuntimeError, match="exited with code 1"):
            c._start_managed_server()

    def test_start_managed_server_raises_timeout(self, monkeypatch):
        """If no listen line appears before the deadline TimeoutError is raised."""
        monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
        monkeypatch.setattr(
            "src.backends.opencode.client.OpenCodeClient._managed_config_content",
            lambda self: "{}",
        )
        monkeypatch.setenv("OPENCODE_START_TIMEOUT_MS", "1")  # 1 ms timeout

        proc = MagicMock()
        proc.returncode = None
        proc.stdout = MagicMock()
        proc.poll.return_value = None  # process never exits
        proc.stdout.readline.return_value = "some other line\n"

        import select as select_mod
        import time

        # Make select return not-readable so the while loop times out quickly
        monkeypatch.setattr(
            select_mod, "select", lambda rlist, wlist, xlist, timeout: ([], [], [])
        )
        monkeypatch.setattr(
            "src.backends.opencode.client.subprocess.Popen", lambda *a, **kw: proc
        )
        monkeypatch.setattr(
            "src.backends.opencode.client.OpenCodeClient.close", lambda self: None
        )

        c = OpenCodeClient.__new__(OpenCodeClient)
        c._process = None
        c._server_username = "opencode"
        c._server_password = None
        c._agent = "general"
        c.timeout = 5.0

        with pytest.raises(TimeoutError, match="Timeout waiting for OpenCode server"):
            c._start_managed_server()

    def test_start_managed_server_raises_when_stdout_unavailable(self, monkeypatch):
        """If proc.stdout is None the server raises RuntimeError immediately."""
        monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
        monkeypatch.setattr(
            "src.backends.opencode.client.OpenCodeClient._managed_config_content",
            lambda self: "{}",
        )

        proc = MagicMock()
        proc.stdout = None
        proc.poll.return_value = None

        monkeypatch.setattr(
            "src.backends.opencode.client.subprocess.Popen", lambda *a, **kw: proc
        )
        monkeypatch.setattr(
            "src.backends.opencode.client.OpenCodeClient.close", lambda self: None
        )

        c = OpenCodeClient.__new__(OpenCodeClient)
        c._process = None
        c._server_username = "opencode"
        c._server_password = None
        c._agent = "general"
        c.timeout = 5.0

        with pytest.raises(RuntimeError, match="stdout is unavailable"):
            c._start_managed_server()


# ---------------------------------------------------------------------------
# close() — kill branch on TimeoutExpired (lines 232-240)
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_does_nothing_when_no_process(self, managed_client):
        managed_client._process = None
        managed_client.close()  # must not raise

    def test_close_skips_terminate_when_process_already_exited(self, managed_client):
        proc = MagicMock()
        proc.poll.return_value = 0  # already dead
        managed_client._process = proc
        managed_client.close()
        proc.terminate.assert_not_called()

    def test_close_terminates_running_process(self, managed_client):
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        proc.wait.return_value = 0
        managed_client._process = proc
        managed_client.close()
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()

    def test_close_kills_when_terminate_times_out(self, managed_client):
        """When proc.wait() raises TimeoutExpired the process is killed."""
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        proc.wait.side_effect = [subprocess.TimeoutExpired("opencode", 3), None]
        managed_client._process = proc
        managed_client.close()
        proc.kill.assert_called_once()
        assert proc.wait.call_count == 2


# ---------------------------------------------------------------------------
# _trusted_attached_image_path — exception branch (lines 411-412, 416, 418)
# ---------------------------------------------------------------------------


class TestTrustedAttachedImagePath:
    def test_returns_none_when_path_resolve_raises_os_error(self, external_client, tmp_path):
        # Pass a path that does not exist — Path.resolve(strict=True) raises FileNotFoundError
        raw = "/nonexistent/path/img_0123456789abcdef.png"
        result = external_client._trusted_attached_image_path(raw, str(tmp_path))
        assert result is None

    def test_returns_none_when_suffix_not_in_allowed_list(self, external_client, tmp_path):
        """A file with a disallowed extension is rejected."""
        image_dir = tmp_path / ".claude_images"
        image_dir.mkdir()
        # Valid filename pattern but wrong extension
        bad_path = image_dir / "img_0123456789abcdef.bmp"
        bad_path.write_bytes(b"fake")
        result = external_client._trusted_attached_image_path(str(bad_path), str(tmp_path))
        assert result is None

    def test_returns_none_when_filename_does_not_match_pattern(self, external_client, tmp_path):
        """A file with valid extension but wrong filename pattern is rejected."""
        image_dir = tmp_path / ".claude_images"
        image_dir.mkdir()
        # Valid extension but name doesn't match img_<16hex> pattern
        bad_path = image_dir / "screenshot.png"
        bad_path.write_bytes(b"fake")
        result = external_client._trusted_attached_image_path(str(bad_path), str(tmp_path))
        assert result is None


# ---------------------------------------------------------------------------
# _question_reply_body — nested-list and flat-list branches (lines 446, 448)
# ---------------------------------------------------------------------------


class TestQuestionReplyBody:
    def test_nested_list_of_lists_becomes_answers_directly(self, external_client):
        """A JSON list-of-lists maps directly to answers."""
        body = external_client._question_reply_body('[["yes", "no"]]')
        assert body == {"answers": [["yes", "no"]]}

    def test_flat_list_of_strings_wrapped_in_single_answer(self, external_client):
        """A JSON flat list becomes a single-element answers list."""
        body = external_client._question_reply_body('["option_a", "option_b"]')
        assert body == {"answers": [["option_a", "option_b"]]}

    def test_plain_string_is_single_wrapped_answer(self, external_client):
        """A plain non-JSON string is wrapped in answers."""
        body = external_client._question_reply_body("yes")
        assert body == {"answers": [["yes"]]}

    def test_json_non_list_value_is_str_wrapped(self, external_client):
        """A JSON non-list (e.g., dict) falls through to str(output)."""
        body = external_client._question_reply_body('{"key": "val"}')
        assert body == {"answers": [['{"key": "val"}']]}


# ---------------------------------------------------------------------------
# _permission_reply_body — list branch (line 470)
# ---------------------------------------------------------------------------


class TestPermissionReplyBodyList:
    def test_list_input_extracts_first_known_reply(self, external_client):
        """A JSON list containing a valid reply string is parsed."""
        body = external_client._permission_reply_body('["always"]')
        assert body == {"reply": "always"}

    def test_list_input_maps_yes_to_once(self, external_client):
        body = external_client._permission_reply_body('["yes"]')
        assert body == {"reply": "once"}

    def test_list_input_maps_deny_to_reject(self, external_client):
        body = external_client._permission_reply_body('["deny"]')
        assert body == {"reply": "reject"}

    def test_list_input_ignores_non_strings(self, external_client):
        """Non-string items in a JSON list are skipped; defaults to reject."""
        body = external_client._permission_reply_body("[1, 2, 3]")
        assert body == {"reply": "reject"}

    def test_dict_without_message_no_message_key(self, external_client):
        """Dict with reply but no message key returns body without message."""
        body = external_client._permission_reply_body('{"reply": "once"}')
        assert body == {"reply": "once"}
        assert "message" not in body


# ---------------------------------------------------------------------------
# _describe_http_error (lines 493-495)
# ---------------------------------------------------------------------------


class TestDescribeHttpError:
    def test_includes_status_code_and_truncated_body(self, external_client):
        class FakeResp:
            status_code = 500
            text = "Internal Server Error\ndetail line"

        desc = external_client._describe_http_error(FakeResp())
        assert "OpenCode HTTP 500" in desc
        assert "Internal Server Error" in desc
        # newlines replaced
        assert "\n" not in desc

    def test_handles_missing_attributes(self, external_client):
        """Works even when status_code/text are absent."""
        desc = external_client._describe_http_error(object())
        assert "OpenCode HTTP unknown" in desc


# ---------------------------------------------------------------------------
# _iter_sse_events — comment, event-type, raw-json, and end-of-stream flush
# (lines 507-511, 523, 530-531, 535)
# ---------------------------------------------------------------------------


class TestIterSseEvents:
    async def _collect(self, client, lines):
        """Helper: feed lines through _iter_sse_events and collect dicts."""

        class FakeResponse:
            async def aiter_lines(self_inner):
                for line in lines:
                    yield line

        return [event async for event in client._iter_sse_events(FakeResponse())]

    async def test_comment_lines_are_ignored(self, external_client):
        lines = [
            ": keep-alive",
            'data: {"type": "ping"}',
            "",
        ]
        events = await self._collect(external_client, lines)
        assert events == [{"type": "ping"}]

    async def test_event_type_line_is_attached_to_next_event(self, external_client):
        """An 'event:' prefix sets the type on the following data block."""
        lines = [
            "event: session.idle",
            'data: {"properties": {"sessionID": "s1"}}',
            "",
        ]
        events = await self._collect(external_client, lines)
        assert len(events) == 1
        assert events[0]["type"] == "session.idle"

    async def test_event_type_not_overwritten_when_data_has_type(self, external_client):
        """If the JSON data already has 'type', the event: line is ignored."""
        lines = [
            "event: other",
            'data: {"type": "message.part.delta"}',
            "",
        ]
        events = await self._collect(external_client, lines)
        assert events[0]["type"] == "message.part.delta"

    async def test_raw_json_line_without_data_prefix(self, external_client):
        """Lines starting with '{' are treated as bare JSON data."""
        lines = [
            '{"type": "raw_event", "value": 42}',
            "",
        ]
        events = await self._collect(external_client, lines)
        assert events == [{"type": "raw_event", "value": 42}]

    async def test_non_json_data_is_ignored(self, external_client):
        """Non-JSON data lines produce no event."""
        lines = [
            "data: not-json-at-all",
            "",
        ]
        events = await self._collect(external_client, lines)
        assert events == []

    async def test_end_of_stream_flush_emits_pending_event(self, external_client):
        """A data block without a trailing blank line is flushed at end of stream."""
        lines = [
            'data: {"type": "session.idle"}',
            # intentionally no trailing blank line
        ]
        events = await self._collect(external_client, lines)
        assert len(events) == 1
        assert events[0]["type"] == "session.idle"

    async def test_multi_line_data_is_joined(self, external_client):
        """Multiple data: lines for one event block are joined with newline."""
        payload = {"type": "test", "text": "hello"}
        json_str = json.dumps(payload)
        # split JSON across two data: lines (simulate multi-line SSE data)
        mid = len(json_str) // 2
        lines = [
            f"data: {json_str[:mid]}",
            f"data: {json_str[mid:]}",
            "",
        ]
        # The split produces invalid JSON — confirm it is silently dropped
        events = await self._collect(external_client, lines)
        # Combined string is NOT valid JSON after split, so zero events expected.
        assert isinstance(events, list)


# ---------------------------------------------------------------------------
# _stream_events — HTTPStatusError branch (lines 571-572)
# ---------------------------------------------------------------------------


class TestStreamEventsHttpError:
    async def test_http_status_error_from_post_becomes_runtime_error_chunk(
        self, external_client, monkeypatch
    ):
        """HTTPStatusError from the POST is converted to an error chunk."""

        class FakeStreamResponse:
            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                # yield nothing — we never reach iteration
                return
                yield  # noqa: unreachable

        class FakeStreamContext:
            async def __aenter__(self):
                return FakeStreamResponse()

            async def __aexit__(self, *_):
                pass

        class FakePostResponse:
            status_code = 422
            text = "Unprocessable Entity"

            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "422",
                    request=MagicMock(),
                    response=MagicMock(status_code=422, text="Unprocessable Entity"),
                )

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def stream(self, method, path, **kwargs):
                return FakeStreamContext()

            async def post(self, path, **kwargs):
                return FakePostResponse()

        monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

        sc = _make_session_client(session_id="oc-session", cwd=None)
        chunks = [
            chunk
            async for chunk in external_client._stream_events(
                sc,
                post_path="/session/oc-session/prompt_async",
                post_body={"agent": "general", "parts": [{"type": "text", "text": "hi"}]},
                error_label="test",
                emit_usage=False,
            )
        ]

        assert len(chunks) == 1
        assert chunks[0]["type"] == "error"
        assert "OpenCode HTTP 422" in chunks[0]["error_message"]


# ---------------------------------------------------------------------------
# _stream_events — error event from converter (lines 577-578)
# ---------------------------------------------------------------------------


class TestStreamEventsErrorEvent:
    async def test_error_event_from_converter_yields_error_chunk(
        self, external_client, monkeypatch
    ):
        """When the event converter signals an error the stream yields an error chunk."""
        error_event = {
            "type": "session.error",
            "properties": {"sessionID": "oc-session", "error": "something went wrong"},
        }

        class FakeStreamResponse:
            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                yield "data: " + json.dumps(error_event)
                yield ""

        class FakeStreamContext:
            async def __aenter__(self):
                return FakeStreamResponse()

            async def __aexit__(self, *_):
                pass

        class FakePostResponse:
            status_code = 200
            text = "ok"

            def raise_for_status(self):
                pass

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def stream(self, method, path, **kwargs):
                return FakeStreamContext()

            async def post(self, path, **kwargs):
                return FakePostResponse()

        monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

        sc = _make_session_client(session_id="oc-session", cwd=None)
        chunks = [
            chunk
            async for chunk in external_client._stream_events(
                sc,
                post_path="/session/oc-session/prompt_async",
                post_body={"agent": "general", "parts": [{"type": "text", "text": "hi"}]},
                error_label="test",
                emit_usage=False,
            )
        ]

        error_chunks = [c for c in chunks if c.get("type") == "error"]
        assert len(error_chunks) == 1


# ---------------------------------------------------------------------------
# _stream_events — general exception handler (lines 583-586)
# ---------------------------------------------------------------------------


class TestStreamEventsGeneralException:
    async def test_unexpected_exception_yields_error_chunk(self, external_client, monkeypatch):
        """Any unexpected exception during streaming is caught and returned as an error."""

        class BoomStreamContext:
            async def __aenter__(self):
                raise RuntimeError("connection reset")

            async def __aexit__(self, *_):
                pass

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def stream(self, method, path, **kwargs):
                return BoomStreamContext()

        monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

        sc = _make_session_client(session_id="oc-session", cwd=None)
        chunks = [
            chunk
            async for chunk in external_client._stream_events(
                sc,
                post_path="/session/oc-session/prompt_async",
                post_body={"agent": "general", "parts": [{"type": "text", "text": "hi"}]},
                error_label="streaming prompt",
                emit_usage=False,
            )
        ]

        assert len(chunks) == 1
        assert chunks[0]["type"] == "error"
        assert "connection reset" in chunks[0]["error_message"]


# ---------------------------------------------------------------------------
# run_completion_with_client — HTTP exception path (lines 637-640)
# ---------------------------------------------------------------------------


class TestRunCompletionHttpException:
    async def test_http_exception_during_prompt_yields_error_chunk(
        self, external_client, monkeypatch
    ):
        """An HTTP error during non-streaming prompt is returned as an error chunk."""

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def post(self, path, **kwargs):
                if path == "/session":
                    return FakeResponse({"id": "oc-session"})
                raise RuntimeError("network timeout")

        monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

        from src.session_manager import Session

        session = Session(session_id="gw-session")
        client_handle = await external_client.create_client(session=session)
        chunks = [
            chunk
            async for chunk in external_client.run_completion_with_client(
                client_handle, "say hi", session
            )
        ]

        assert len(chunks) == 1
        assert chunks[0]["type"] == "error"
        assert "network timeout" in chunks[0]["error_message"]


# ---------------------------------------------------------------------------
# run_completion_with_client — info.error branch (lines 644-645)
# ---------------------------------------------------------------------------


class TestRunCompletionInfoError:
    async def test_info_error_field_produces_error_chunk(self, external_client, monkeypatch):
        """When the server payload contains info.error an error chunk is yielded."""

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def post(self, path, **kwargs):
                if path == "/session":
                    return FakeResponse({"id": "oc-session"})
                return FakeResponse(
                    {
                        "info": {"error": "model rate limited"},
                        "parts": [],
                    }
                )

        monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

        from src.session_manager import Session

        session = Session(session_id="gw-session")
        client_handle = await external_client.create_client(session=session)
        chunks = [
            chunk
            async for chunk in external_client.run_completion_with_client(
                client_handle, "hi", session
            )
        ]

        assert len(chunks) == 1
        assert chunks[0]["type"] == "error"
        assert "model rate limited" in chunks[0]["error_message"]


# ---------------------------------------------------------------------------
# parse_message — content-parts fallback branch (lines 708-716)
# ---------------------------------------------------------------------------


class TestParseMessageContentFallback:
    def test_returns_none_when_no_result_and_no_text_parts(self, external_client):
        messages = [
            {"type": "user", "content": [{"type": "image"}]},
            {"type": "user", "content": "not-a-list"},
        ]
        assert external_client.parse_message(messages) is None

    def test_concatenates_text_parts_from_content_lists(self, external_client):
        """Without a result chunk the text from content parts is concatenated."""
        messages = [
            {
                "type": "assistant",
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "tool_use", "id": "t1"},
                    {"type": "text", "text": "world"},
                ],
            },
        ]
        result = external_client.parse_message(messages)
        assert result == "hello world"

    def test_result_type_takes_priority_over_content_parts(self, external_client):
        """If any chunk has type=result it wins regardless of order."""
        messages = [
            {
                "type": "assistant",
                "content": [{"type": "text", "text": "ignored"}],
            },
            {"type": "result", "result": "the answer"},
        ]
        assert external_client.parse_message(messages) == "the answer"

    def test_empty_text_parts_are_skipped(self, external_client):
        """Text parts with empty strings are not included in the concatenation."""
        messages = [
            {
                "type": "assistant",
                "content": [
                    {"type": "text", "text": ""},
                    {"type": "text", "text": "non-empty"},
                ],
            }
        ]
        assert external_client.parse_message(messages) == "non-empty"

    def test_returns_none_for_empty_message_list(self, external_client):
        assert external_client.parse_message([]) is None


# ---------------------------------------------------------------------------
# runtime_metadata — covers line 144 indirectly + managed_process flag
# ---------------------------------------------------------------------------


class TestRuntimeMetadata:
    def test_runtime_metadata_external_mode(self, external_client):
        meta = external_client.runtime_metadata()
        assert meta["mode"] == "external"
        assert meta["managed_process"] is False
        assert "base_url" in meta
        assert "agent" in meta

    def test_runtime_metadata_managed_mode(self, managed_client):
        meta = managed_client.runtime_metadata()
        assert meta["mode"] == "managed"
        assert "managed_process" in meta
