# Coverage Stage 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise total `src` coverage from `91%` (post-Stage-1) to **at least 93%** by adding focused tests for admin usage HTTP routes and the OpenCode backend's smaller modules and pure helpers.

**Architecture:** Test-only plan. Use `TestClient` with admin auth bypassed for the route layer (existing pattern in `tests/test_admin_routes_coverage.py`); use plain unit tests with `monkeypatch` for OpenCode helpers. Do not modify production code unless a test reveals a real defect.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio (`auto` mode — no `@pytest.mark.asyncio` markers), FastAPI TestClient, httpx (mocked via `monkeypatch`).

---

## Baseline

```bash
.venv/bin/python -m pytest --cov=src --cov-report=term-missing -q
```

After Stage 1:
```text
TOTAL 5792 statements, 502 missed, 91% coverage
```

Stage 2 targets (lines uncovered):

```text
src/routes/admin.py             833-842, 854-864, 876-886, 896-902, 913-923, 934-943   (~60 lines)
src/backends/opencode/__init__.py        38-46, 52, 62-63                              (10 lines)
src/backends/opencode/auth.py            55                                            (1 line)
src/backends/opencode/config.py          41, 66, 103-105                               (5 lines)
src/backends/opencode/constants.py       22                                            (1 line)
src/backends/opencode/events.py          60-65, 70, 76, 81, 86, 98, 127, 154, 156,
                                         195, 198, 216-220, 231, 251, 282, 285,
                                         300-301                                       (27 lines)
src/backends/opencode/client.py          (pure helpers: ~40 lines from the larger
                                          87-line gap; the rest is network-path
                                          code left for Stage 3)
```

Covering these moves total to ~93%.

## File Structure

- Create: `tests/test_admin_usage_routes.py` (Task 1)
- Create: `tests/test_opencode_misc_unit.py` (Task 2)
- Create: `tests/test_opencode_events_unit.py` (Task 3)
- Create: `tests/test_opencode_client_unit.py` (Task 4)
- No production files modified.

---

### Task 1: Admin Usage HTTP Routes

**Files:**
- Create: `tests/test_admin_usage_routes.py`

**Routes under test** (from `src/routes/admin.py`):

| Endpoint | Production fn | usage_queries fn |
|---|---|---|
| `GET /admin/api/usage/summary` | `usage_summary_endpoint` | `get_summary` |
| `GET /admin/api/usage/users` | `usage_users_endpoint` | `get_top_users` |
| `GET /admin/api/usage/tools` | `usage_tools_endpoint` | `get_top_tools` |
| `GET /admin/api/usage/series` | `usage_series_endpoint` | `get_time_series` |
| `GET /admin/api/usage/tools-series` | `usage_tools_series_endpoint` | `get_tool_breakdown_series` |
| `GET /admin/api/usage/turns` | `usage_turns_endpoint` | `get_recent_turns` |

Each endpoint imports its `usage_queries.<fn>` lazily at call time (inside the function body — see `src/routes/admin.py` lines ~833, 854, ...). Tests patch the function on the `src.usage_queries` module so the route's `from src.usage_queries import get_summary` picks up the fake.

- [ ] **Step 1: Read existing patterns**

Read `tests/test_admin_routes_coverage.py` to understand how the project bypasses admin auth and uses `TestClient`. Match that style.

- [ ] **Step 2: Write the test file**

Create `tests/test_admin_usage_routes.py` with this exact content:

```python
"""HTTP coverage for the admin usage analytics endpoints."""

from fastapi.testclient import TestClient

from src import usage_queries
from src.admin_auth import require_admin
from src.main import app


def _bypass_admin_auth():
    """Override the admin auth dependency for the duration of a TestClient."""
    return None


def _client():
    app.dependency_overrides[require_admin] = _bypass_admin_auth
    return TestClient(app)


def _restore():
    app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# /admin/api/usage/summary
# ---------------------------------------------------------------------------


def test_usage_summary_returns_disabled_when_query_returns_none(monkeypatch):
    async def fake_get_summary(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_summary", fake_get_summary)
    try:
        resp = _client().get("/admin/api/usage/summary")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_usage_summary_returns_payload_when_query_succeeds(monkeypatch):
    captured = {}

    async def fake_get_summary(**kwargs):
        captured.update(kwargs)
        return {"turns_window": 5}

    monkeypatch.setattr(usage_queries, "get_summary", fake_get_summary)
    try:
        resp = _client().get(
            "/admin/api/usage/summary",
            params={"window_days": 14, "start_date": "2026-04-01", "end_date": "2026-04-30"},
        )
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": True,
        "window_days": 14,
        "summary": {"turns_window": 5},
    }
    assert captured["window_days"] == 14
    assert captured["start_date"] == "2026-04-01"
    assert captured["end_date"] == "2026-04-30"


def test_usage_summary_clamps_window_days(monkeypatch):
    captured = {}

    async def fake_get_summary(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(usage_queries, "get_summary", fake_get_summary)
    try:
        # Above 365 -> clamped to 365; below 1 -> clamped to 1.
        _client().get("/admin/api/usage/summary", params={"window_days": 9999})
        assert captured["window_days"] == 365
        _client().get("/admin/api/usage/summary", params={"window_days": 0})
        assert captured["window_days"] == 1
    finally:
        _restore()


# ---------------------------------------------------------------------------
# /admin/api/usage/users
# ---------------------------------------------------------------------------


def test_usage_users_returns_empty_items_when_disabled(monkeypatch):
    async def fake_get_top_users(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_top_users", fake_get_top_users)
    try:
        resp = _client().get("/admin/api/usage/users")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "items": []}


def test_usage_users_returns_rows_with_clamped_limit(monkeypatch):
    captured = {}

    async def fake_get_top_users(**kwargs):
        captured.update(kwargs)
        return [{"user": "alice", "tokens": 100}]

    monkeypatch.setattr(usage_queries, "get_top_users", fake_get_top_users)
    try:
        resp = _client().get("/admin/api/usage/users", params={"limit": 9999})
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "items": [{"user": "alice", "tokens": 100}]}
    assert captured["limit"] == 500  # clamped


# ---------------------------------------------------------------------------
# /admin/api/usage/tools
# ---------------------------------------------------------------------------


def test_usage_tools_returns_empty_items_when_disabled(monkeypatch):
    async def fake_get_top_tools(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_top_tools", fake_get_top_tools)
    try:
        resp = _client().get("/admin/api/usage/tools")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "items": []}


def test_usage_tools_returns_rows(monkeypatch):
    async def fake_get_top_tools(**kwargs):
        return [{"tool_name": "Read", "calls": 3}]

    monkeypatch.setattr(usage_queries, "get_top_tools", fake_get_top_tools)
    try:
        resp = _client().get("/admin/api/usage/tools", params={"window_days": 3, "limit": 7})
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": True,
        "items": [{"tool_name": "Read", "calls": 3}],
    }


# ---------------------------------------------------------------------------
# /admin/api/usage/series
# ---------------------------------------------------------------------------


def test_usage_series_returns_disabled_payload_when_logger_off(monkeypatch):
    async def fake_get_time_series(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_time_series", fake_get_time_series)
    try:
        resp = _client().get("/admin/api/usage/series", params={"granularity": "week"})
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "granularity": "week", "buckets": []}


def test_usage_series_falls_back_to_day_for_unknown_granularity(monkeypatch):
    captured = {}

    async def fake_get_time_series(**kwargs):
        captured.update(kwargs)
        return [{"bucket": "2026-04-30", "turns": 1}]

    monkeypatch.setattr(usage_queries, "get_time_series", fake_get_time_series)
    try:
        resp = _client().get(
            "/admin/api/usage/series",
            params={"granularity": "minute", "buckets": 200},
        )
    finally:
        _restore()

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["granularity"] == "day"
    assert body["buckets"] == [{"bucket": "2026-04-30", "turns": 1}]
    assert captured["granularity"] == "day"
    assert captured["buckets"] == 60  # clamped from 200


# ---------------------------------------------------------------------------
# /admin/api/usage/tools-series
# ---------------------------------------------------------------------------


def test_usage_tools_series_returns_disabled_payload(monkeypatch):
    async def fake_get_tool_breakdown_series(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_tool_breakdown_series", fake_get_tool_breakdown_series)
    try:
        resp = _client().get("/admin/api/usage/tools-series")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "granularity": "day", "tools": [], "buckets": []}


def test_usage_tools_series_returns_payload_with_top_clamp(monkeypatch):
    captured = {}

    async def fake_get_tool_breakdown_series(**kwargs):
        captured.update(kwargs)
        return {"tools": ["Read"], "buckets": [{"bucket": "2026-04-30", "values": {"Read": 3}}]}

    monkeypatch.setattr(usage_queries, "get_tool_breakdown_series", fake_get_tool_breakdown_series)
    try:
        resp = _client().get(
            "/admin/api/usage/tools-series",
            params={"granularity": "month", "top": 100},
        )
    finally:
        _restore()

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["granularity"] == "month"
    assert body["tools"] == ["Read"]
    assert captured["top_n"] == 20  # clamped from 100


# ---------------------------------------------------------------------------
# /admin/api/usage/turns
# ---------------------------------------------------------------------------


def test_usage_turns_returns_disabled_payload(monkeypatch):
    async def fake_get_recent_turns(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_recent_turns", fake_get_recent_turns)
    try:
        resp = _client().get("/admin/api/usage/turns")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "items": []}


def test_usage_turns_returns_rows_with_user_filter(monkeypatch):
    captured = {}

    async def fake_get_recent_turns(**kwargs):
        captured.update(kwargs)
        return [{"id": 7, "user": "alice"}]

    monkeypatch.setattr(usage_queries, "get_recent_turns", fake_get_recent_turns)
    try:
        resp = _client().get(
            "/admin/api/usage/turns",
            params={"user": "alice", "limit": 9999, "offset": -1},
        )
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "items": [{"id": 7, "user": "alice"}]}
    assert captured["user"] == "alice"
    assert captured["limit"] == 500  # clamped
    assert captured["offset"] == 0   # clamped to non-negative
```

- [ ] **Step 3: Run focused coverage**

```bash
.venv/bin/python -m pytest tests/test_admin_usage_routes.py --cov=src.routes.admin --cov-report=term-missing -q
```

Expected: **all tests pass**. The 6 usage endpoints in `src/routes/admin.py` (lines 833-943) should be fully covered.

- [ ] **Step 4: Commit**

```bash
git -C /home/jinyoung/claude-code-openai-wrapper add tests/test_admin_usage_routes.py
git -C /home/jinyoung/claude-code-openai-wrapper commit -m "test: cover admin usage routes"
```

---

### Task 2: OpenCode `__init__`, auth, config, constants residual coverage

**Files:**
- Create: `tests/test_opencode_misc_unit.py`

**Targets:**

`src/backends/opencode/__init__.py`
- Lines 38-46: `__getattr__` lazy-import branches for `OpenCodeClient`, `OpenCodeAuthProvider`, unknown name (raises `AttributeError`).
- Line 52: `register(registry_cls=None)` defaults to `BackendRegistry`.
- Lines 62-63: client construction failure path during `register`.

`src/backends/opencode/auth.py`
- Line 55: managed-mode failure when `opencode` binary missing on PATH (the `return {"valid": False, ...}` after `shutil.which` returns `None`).

`src/backends/opencode/config.py`
- Line 41: `_command_list` when `command` is already a list.
- Line 66: `_convert_mcp_server` raises for unsupported type.
- Lines 103-105: `parse_opencode_config_content` non-JSON / non-object.

`src/backends/opencode/constants.py`
- Line 22: `_parse_bool` empty / whitespace input returns `default`.

- [ ] **Step 1: Write the test file**

Create `tests/test_opencode_misc_unit.py`:

```python
"""Residual coverage for small OpenCode modules."""

import pytest

from src.backends import opencode as opencode_pkg
from src.backends.opencode import auth as opencode_auth
from src.backends.opencode import config as opencode_config
from src.backends.opencode import constants as opencode_constants


# ---------------------------------------------------------------------------
# __init__.py — lazy attribute access and registration
# ---------------------------------------------------------------------------


def test_pkg_lazy_attr_returns_client_class():
    cls = opencode_pkg.OpenCodeClient
    from src.backends.opencode.client import OpenCodeClient

    assert cls is OpenCodeClient


def test_pkg_lazy_attr_returns_auth_provider_class():
    cls = opencode_pkg.OpenCodeAuthProvider
    from src.backends.opencode.auth import OpenCodeAuthProvider

    assert cls is OpenCodeAuthProvider


def test_pkg_lazy_attr_raises_for_unknown_name():
    with pytest.raises(AttributeError, match="no attribute 'Bogus'"):
        opencode_pkg.Bogus  # noqa: B018


def test_register_uses_default_backend_registry_and_logs_failure(monkeypatch, caplog):
    """Default registry path: OpenCodeClient instantiation fails -> logs error."""

    class FakeRegistry:
        descriptors = []

        @classmethod
        def register_descriptor(cls, descriptor):
            cls.descriptors.append(descriptor)

        @classmethod
        def register(cls, name, instance):
            raise AssertionError("should not register live client when ctor raises")

    # Patch the import target the function uses at call time.
    def boom(*_args, **_kwargs):
        raise RuntimeError("ctor exploded")

    monkeypatch.setattr("src.backends.opencode.client.OpenCodeClient", boom)

    with caplog.at_level("ERROR", logger="src.backends.opencode"):
        opencode_pkg.register(FakeRegistry)

    assert FakeRegistry.descriptors == [opencode_pkg.OPENCODE_DESCRIPTOR]
    assert any("OpenCode backend client creation failed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# auth.py — managed mode missing binary
# ---------------------------------------------------------------------------


def test_auth_managed_mode_reports_missing_binary(monkeypatch):
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setattr(opencode_auth.shutil, "which", lambda _name: None)

    result = opencode_auth.OpenCodeAuthProvider().validate()

    assert result == {
        "valid": False,
        "errors": ["opencode binary not found on PATH"],
        "config": {"mode": "managed"},
    }


# ---------------------------------------------------------------------------
# config.py — list-shaped command, unsupported type, malformed JSON
# ---------------------------------------------------------------------------


def test_command_list_handles_list_command_with_extra_args():
    server = {"command": ["uvx", "tool"], "args": ["--flag", 1]}
    assert opencode_config._command_list(server) == ["uvx", "tool", "--flag", "1"]


def test_convert_mcp_server_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        opencode_config._convert_mcp_server({"type": "websocket", "url": "ws://x"})


def test_parse_opencode_config_content_rejects_invalid_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        opencode_config.parse_opencode_config_content("{not json")


def test_parse_opencode_config_content_rejects_non_object_json():
    with pytest.raises(ValueError, match="must be a JSON object"):
        opencode_config.parse_opencode_config_content("[1, 2, 3]")


# ---------------------------------------------------------------------------
# constants.py — _parse_bool default branch
# ---------------------------------------------------------------------------


def test_parse_bool_returns_default_for_empty_input():
    assert opencode_constants._parse_bool("   ", default=True) is True
    assert opencode_constants._parse_bool("", default=False) is False
```

- [ ] **Step 2: Run focused coverage**

```bash
.venv/bin/python -m pytest tests/test_opencode_misc_unit.py \
  --cov=src.backends.opencode \
  --cov-report=term-missing -q
```

Expected: all tests pass; `__init__.py`, `auth.py`, `config.py`, `constants.py` reach **≥ 95%** each.

- [ ] **Step 3: Commit**

```bash
git -C /home/jinyoung/claude-code-openai-wrapper add tests/test_opencode_misc_unit.py
git -C /home/jinyoung/claude-code-openai-wrapper commit -m "test: cover opencode misc residuals"
```

---

### Task 3: OpenCode `events.py` residual coverage

**Files:**
- Create: `tests/test_opencode_events_unit.py`

**Targets** (uncovered lines in `src/backends/opencode/events.py`): `60-65, 70, 76, 81, 86, 98, 127, 154, 156, 195, 198, 216-220, 231, 251, 282, 285, 300-301`.

These are private-helper edge cases not exercised by the existing happy-path tests in `tests/test_opencode_backend.py`. Each test below targets a specific uncovered branch with a minimal event dict.

- [ ] **Step 1: Write the test file**

Create `tests/test_opencode_events_unit.py`:

```python
"""Edge-case coverage for OpenCodeEventConverter helpers."""

from src.backends.opencode.events import OpenCodeEventConverter


def _conv(session_id="sess-1"):
    return OpenCodeEventConverter(session_id=session_id)


# ---------------------------------------------------------------------------
# error_message — session.error mapping (lines 60-65)
# ---------------------------------------------------------------------------


def test_error_message_returns_none_for_non_error_event():
    assert _conv().error_message({"type": "session.idle"}) is None


def test_error_message_returns_none_for_other_session():
    conv = _conv("mine")
    event = {
        "type": "session.error",
        "properties": {"sessionID": "other", "error": "boom"},
    }
    assert conv.error_message(event) is None


def test_error_message_returns_string_for_matching_session():
    conv = _conv("mine")
    event = {
        "type": "session.error",
        "properties": {"sessionID": "mine", "error": "kaboom"},
    }
    assert conv.error_message(event) == "kaboom"


def test_error_message_falls_back_to_message_or_props():
    conv = _conv("mine")
    # Falls through to props.get("message")
    e1 = {"type": "session.error", "properties": {"sessionID": "mine", "message": "msg"}}
    assert conv.error_message(e1) == "msg"
    # Falls through to str(props)
    e2 = {"type": "session.error", "properties": {"sessionID": "mine", "code": 500}}
    assert conv.error_message(e2) == str({"sessionID": "mine", "code": 500})


# ---------------------------------------------------------------------------
# _event_session_id / _event_message_id falsy branches (lines 70, 76, 81, 86)
# ---------------------------------------------------------------------------


def test_event_session_id_returns_none_when_properties_missing():
    assert _conv()._event_session_id({"type": "x"}) is None


def test_event_session_id_returns_none_when_part_missing():
    # properties present but neither sessionID nor part.sessionID
    assert _conv()._event_session_id({"properties": {}}) is None


def test_event_message_id_returns_none_when_properties_missing():
    assert _conv()._event_message_id({"type": "x"}) is None


def test_event_message_id_uses_info_id_when_messageID_absent():
    event = {"properties": {"info": {"id": "msg-123"}}}
    assert _conv()._event_message_id(event) == "msg-123"


def test_event_message_id_uses_part_messageID_as_last_fallback():
    event = {"properties": {"part": {"messageID": "msg-abc"}}}
    assert _conv()._event_message_id(event) == "msg-abc"


def test_event_message_id_returns_none_when_no_id_anywhere():
    assert _conv()._event_message_id({"properties": {}}) is None


# ---------------------------------------------------------------------------
# _record_message_role early-return (line 98)
# ---------------------------------------------------------------------------


def test_record_message_role_ignores_event_when_info_not_dict():
    conv = _conv()
    conv._record_message_role(
        {"type": "message.updated", "properties": {"info": "not-a-dict"}}
    )
    assert conv.message_roles == {}


# ---------------------------------------------------------------------------
# _convert_question_event guard (line 127)
# ---------------------------------------------------------------------------


def test_convert_question_event_returns_none_when_request_id_missing():
    event = {
        "type": "question.asked",
        "properties": {"questions": ["q1"]},  # no id
    }
    assert _conv().convert(event) == []


# ---------------------------------------------------------------------------
# _convert_permission_event guards (lines 154, 156)
# ---------------------------------------------------------------------------


def test_convert_permission_event_returns_none_for_missing_id():
    event = {
        "type": "permission.asked",
        "properties": {"permission": "read"},
    }
    assert _conv().convert(event) == []


def test_convert_permission_event_returns_none_for_missing_permission():
    event = {
        "type": "permission.asked",
        "properties": {"id": "req-1", "permission": ""},
    }
    assert _conv().convert(event) == []


# ---------------------------------------------------------------------------
# _convert_text_event guards (lines 195, 198, 216-220, 231)
# ---------------------------------------------------------------------------


def test_message_part_delta_with_non_text_field_is_dropped():
    event = {
        "type": "message.part.delta",
        "properties": {"sessionID": "sess-1", "field": "thinking", "delta": "ignore"},
    }
    assert _conv().convert(event) == []


def test_message_part_delta_with_empty_delta_is_dropped():
    event = {
        "type": "message.part.delta",
        "properties": {"sessionID": "sess-1", "delta": ""},
    }
    assert _conv().convert(event) == []


def test_message_part_updated_uses_text_fallback_when_no_delta():
    event = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {"id": "p1", "type": "text", "text": "hello"},
        },
    }
    chunks = _conv().convert(event)
    assert chunks == [
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    ]


def test_message_part_updated_text_fallback_returns_empty_when_no_change():
    conv = _conv()
    # First updated establishes "hello"
    conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {"id": "p1", "type": "text", "text": "hello"},
            },
        }
    )
    # Second updated with the same text -> computed_delta == "" -> returns None
    chunks = conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {"id": "p1", "type": "text", "text": "hello"},
            },
        }
    )
    assert chunks == []


# ---------------------------------------------------------------------------
# _convert_usage_event guards (line 251)
# ---------------------------------------------------------------------------


def test_usage_event_skipped_when_tokens_not_dict():
    conv = _conv()
    conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {"type": "step-finish", "tokens": "not-a-dict"},
            },
        }
    )
    assert conv.usage is None


# ---------------------------------------------------------------------------
# _convert_tool_event guards (lines 282, 285, 300-301)
# ---------------------------------------------------------------------------


def test_tool_event_skipped_when_state_not_dict():
    event = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {"type": "tool", "state": "broken", "callID": "c1", "tool": "Read"},
        },
    }
    assert _conv().convert(event) == []


def test_tool_event_skipped_when_call_id_missing():
    event = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {"type": "tool", "state": {"status": "running"}, "tool": "Read"},
        },
    }
    assert _conv().convert(event) == []


def test_question_tool_emits_no_chunk_but_marks_results_on_completed():
    conv = _conv()
    chunks = conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {
                    "type": "tool",
                    "callID": "q1",
                    "tool": "question",
                    "state": {"status": "completed", "input": {"a": 1}},
                },
            },
        }
    )
    assert chunks == []
    assert "q1" in conv.emitted_tool_uses
    assert "q1" in conv.emitted_tool_results


def test_question_tool_error_status_marks_results():
    conv = _conv()
    chunks = conv.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "sess-1",
                "part": {
                    "type": "tool",
                    "callID": "q2",
                    "tool": "question",
                    "state": {"status": "error", "input": {}},
                },
            },
        }
    )
    assert chunks == []
    assert "q2" in conv.emitted_tool_results
```

- [ ] **Step 2: Run focused coverage**

```bash
.venv/bin/python -m pytest tests/test_opencode_events_unit.py \
  --cov=src.backends.opencode.events --cov-report=term-missing -q
```

Expected: all tests pass; `events.py` coverage **≥ 95%**.

- [ ] **Step 3: Commit**

```bash
git -C /home/jinyoung/claude-code-openai-wrapper add tests/test_opencode_events_unit.py
git -C /home/jinyoung/claude-code-openai-wrapper commit -m "test: cover opencode event converter edge cases"
```

---

### Task 4: OpenCode `client.py` pure-helper coverage

**Files:**
- Create: `tests/test_opencode_client_unit.py`

**Targets:** the *pure* helpers on `OpenCodeClient` that don't require a running OpenCode server. Exclude all network-path code (`verify`, `create_client`, `_run_completion_streaming`, `_iter_sse_events`, etc.) — those move to Stage 3.

Helpers to test:
- `OpenCodeSessionClient.disconnect`: `base_url is None` early-return (line 78), normal `httpx.AsyncClient` flow with `404` short-circuit, normal flow with non-404 success, exception path that swallows errors.
- `OpenCodeClient._auth`: `None` when password unset, `BasicAuth` when set.
- `OpenCodeClient._client_kwargs` / `_event_client_kwargs`: includes `auth` when present, omits when not; `_event_client_kwargs` overrides `timeout` with an `httpx.Timeout` having `read=None`.
- `OpenCodeClient._directory_params`: returns `None` when `cwd` is falsy, dict otherwise.
- `OpenCodeClient._combine_system_prompt`: both / one / neither branches.
- `OpenCodeClient._split_provider_model`: missing slash returns `None`; valid input splits into `{providerID, modelID}`.
- `OpenCodeClient._extract_text`: parts not a list, parts present, non-text parts filtered.
- `OpenCodeClient._extract_usage`: info missing, tokens missing, full payload.
- `OpenCodeClient._describe_non_json_response`: produces a status/content-type/body string.
- `OpenCodeClient._prompt_parts`: leading-text + image, image-only, no images.

Construction note: `OpenCodeClient.__init__` starts a managed server when `OPENCODE_BASE_URL` is unset. To get an instance for unit tests, set `OPENCODE_BASE_URL` to a fake URL via `monkeypatch` so `_start_managed_server` is bypassed.

- [ ] **Step 1: Write the test file**

Create `tests/test_opencode_client_unit.py`:

```python
"""Unit tests for OpenCodeClient pure helpers (no live server)."""

from typing import Any

import httpx
import pytest

from src.backends.opencode.client import OpenCodeClient, OpenCodeSessionClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://example.com")
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    return OpenCodeClient()


# ---------------------------------------------------------------------------
# OpenCodeSessionClient.disconnect
# ---------------------------------------------------------------------------


async def test_session_disconnect_no_op_when_base_url_missing():
    sc = OpenCodeSessionClient(
        session_id="s1", cwd=None, model=None, system_prompt=None,
    )
    await sc.disconnect()  # No exception, no httpx call.


async def test_session_disconnect_short_circuits_on_404(monkeypatch):
    deleted: list[tuple[str, Any]] = []

    class FakeResponse:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("should not be called for 404")

    class FakeClient:
        def __init__(self, **kwargs): self.kwargs = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def delete(self, path, params=None):
            deleted.append((path, params))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    sc = OpenCodeSessionClient(
        session_id="s1", cwd="/tmp", model=None, system_prompt=None,
        base_url="http://x", timeout=1.0,
    )
    await sc.disconnect()
    assert deleted == [("/session/s1", {"directory": "/tmp"})]


async def test_session_disconnect_swallows_exceptions(monkeypatch):
    class BoomClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): raise RuntimeError("network down")
        async def __aexit__(self, *_): return False

    monkeypatch.setattr(httpx, "AsyncClient", BoomClient)

    sc = OpenCodeSessionClient(
        session_id="s1", cwd=None, model=None, system_prompt=None,
        base_url="http://x", timeout=1.0,
    )
    await sc.disconnect()  # Logs warning, does not raise.


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_auth_returns_none_when_password_unset(client):
    assert client._auth() is None


def test_auth_returns_basic_auth_when_password_set(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://example.com")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    c = OpenCodeClient()
    auth = c._auth()
    assert isinstance(auth, httpx.BasicAuth)


def test_client_kwargs_omits_auth_when_none(client):
    kwargs = client._client_kwargs()
    assert "auth" not in kwargs
    assert kwargs["base_url"] == "http://example.com"


def test_client_kwargs_includes_auth_when_present(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://example.com")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "pw")
    c = OpenCodeClient()
    kwargs = c._client_kwargs()
    assert isinstance(kwargs["auth"], httpx.BasicAuth)


def test_event_client_kwargs_overrides_timeout_for_streaming(client):
    kwargs = client._event_client_kwargs()
    timeout = kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read is None  # Stream reads must not time out.


def test_directory_params_returns_none_when_cwd_empty(client):
    assert client._directory_params(None) is None
    assert client._directory_params("") is None


def test_directory_params_returns_dict_when_cwd_set(client):
    assert client._directory_params("/work") == {"directory": "/work"}


def test_combine_system_prompt_both_present(client):
    assert client._combine_system_prompt("base", "extra") == "base\n\nextra"


def test_combine_system_prompt_only_one_present(client):
    assert client._combine_system_prompt("base", None) == "base"
    assert client._combine_system_prompt(None, "extra") == "extra"


def test_combine_system_prompt_neither_present(client):
    assert client._combine_system_prompt(None, None) is None


def test_split_provider_model_returns_none_for_unsplittable(client):
    assert client._split_provider_model(None) is None
    assert client._split_provider_model("noslash") is None


def test_split_provider_model_returns_dict_for_valid(client):
    assert client._split_provider_model("anthropic/claude-sonnet") == {
        "providerID": "anthropic",
        "modelID": "claude-sonnet",
    }


def test_extract_text_handles_non_list_parts(client):
    assert client._extract_text({"parts": "not-a-list"}) == ""
    assert client._extract_text({}) == ""


def test_extract_text_concatenates_text_parts_only(client):
    payload = {
        "parts": [
            {"type": "text", "text": "hello "},
            {"type": "image"},
            {"type": "text", "text": "world"},
            {"type": "text", "text": ""},
            "not-a-dict",
        ]
    }
    assert client._extract_text(payload) == "hello world"


def test_extract_usage_returns_none_when_info_missing(client):
    assert client._extract_usage({}) is None
    assert client._extract_usage({"info": "not-a-dict"}) is None


def test_extract_usage_returns_none_when_tokens_missing(client):
    assert client._extract_usage({"info": {}}) is None
    assert client._extract_usage({"info": {"tokens": "bad"}}) is None


def test_extract_usage_sums_tokens(client):
    payload = {"info": {"tokens": {"input": 10, "output": 20, "reasoning": 5}}}
    assert client._extract_usage(payload) == {
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 35,
    }


def test_describe_non_json_response_includes_status_and_body_snippet(client):
    class FakeResp:
        status_code = 502
        headers = {"content-type": "text/html"}
        text = "<html>oops</html>"

    desc = client._describe_non_json_response(FakeResp())
    assert "status=502" in desc
    assert "content-type=text/html" in desc
    assert "<html>oops</html>" in desc


def test_prompt_parts_returns_single_text_when_no_image(client):
    parts = client._prompt_parts("hello world", cwd=None)
    assert parts == [{"type": "text", "text": "hello world"}]


def test_prompt_parts_drops_untrusted_image_marker(client):
    # Image marker present but cwd=None -> _trusted_attached_image_path returns None
    # -> file_part is None -> marker dropped, surrounding text concatenated.
    text = "before [attached_image:/tmp/x.png] after"
    parts = client._prompt_parts(text, cwd=None)
    # Marker removed, rest concatenated as plain text part(s).
    joined = "".join(p["text"] for p in parts if p["type"] == "text")
    assert "[attached_image:" not in joined
    assert "before" in joined
    assert "after" in joined
```

- [ ] **Step 2: Run focused coverage**

```bash
.venv/bin/python -m pytest tests/test_opencode_client_unit.py \
  --cov=src.backends.opencode.client --cov-report=term-missing -q
```

Expected: all tests pass; `client.py` coverage rises significantly (target **≥ 90%**, but ≥ 87% acceptable since deep network-path code is out of scope here).

If the test file references a constant that doesn't exist (e.g., `_ATTACHED_IMAGE_RE` regex literal), adapt the test prompt to match the production regex — read `src/backends/opencode/client.py` lines around 380 to see the exact marker format.

- [ ] **Step 3: Commit**

```bash
git -C /home/jinyoung/claude-code-openai-wrapper add tests/test_opencode_client_unit.py
git -C /home/jinyoung/claude-code-openai-wrapper commit -m "test: cover opencode client pure helpers"
```

---

### Task 5: Full coverage verification

**Files:**
- No file changes expected.

- [ ] **Step 1: Run full coverage**

```bash
.venv/bin/python -m pytest --cov=src --cov-report=term-missing -q
```

Expected:
```text
all non-e2e tests pass
TOTAL coverage is at least 93%
```

- [ ] **Step 2: Inspect**

If TOTAL < 93%, identify which targeted module fell short of expectations and add 1–2 follow-up tests in that module's test file (re-running focused coverage to confirm). Do not touch production code.

- [ ] **Step 3: Commit only if tests changed**

If Step 2 added tests, commit them with a message like `test: top up <module> coverage`. Otherwise, no commit.

---

## Self-Review

- **Spec coverage:** All Stage 2 modules from `docs/superpowers/specs/2026-04-30-test-coverage-95-design.md` are addressed: admin usage routes (Task 1), opencode `__init__/auth/config/constants` residuals (Task 2), opencode `events.py` (Task 3), opencode `client.py` pure helpers (Task 4). Task 5 verifies the overall ≥ 93% target.
- **Placeholder scan:** All test code is concrete; no TBD/TODO. Task 4 contains one explicit fallback instruction ("if `_ATTACHED_IMAGE_RE` doesn't match, read source") because the regex format is implementation-detail-sensitive — that's a real escape hatch, not a placeholder.
- **Type consistency:** Helper names match production (`OpenCodeClient`, `OpenCodeSessionClient`, `_combine_system_prompt`, `_split_provider_model`, `_extract_text`, `_extract_usage`, `_describe_non_json_response`, `_prompt_parts`, `_directory_params`, `_client_kwargs`, `_event_client_kwargs`, `_auth`, `disconnect`). Admin route paths and query parameter names match `src/routes/admin.py`. `usage_queries` function names (`get_summary`, `get_top_users`, `get_top_tools`, `get_time_series`, `get_tool_breakdown_series`, `get_recent_turns`) match Stage 1.
- **Out-of-scope reminder:** Network-path tests for `client.py` (`verify`, `create_client`, streaming) are deferred to Stage 3 by design. The coverage target accommodates this.
