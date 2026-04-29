# OpenCode Post-MVP 1-4 Implementation Plan

> **SUPERSEDED:** The current OpenCode backend is managed-only. The earlier
> dual-mode implementation plan that referenced `OPENCODE_BASE_URL` was
> replaced; external OpenCode servers are no longer supported.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the first four OpenCode post-MVP tracks: operational hardening, streaming quality, OpenCode question continuation, and MCP/config integration.

**Architecture:** Keep the existing backend-neutral Responses route as the orchestration layer. Add focused OpenCode helpers inside `src/backends/opencode/` for health metadata, event conversion, question state, and config generation, then expose those capabilities through existing admin, response, and MCP boundaries. Each track must be independently testable and should preserve Claude behavior by gating OpenCode-specific logic on `resolved.backend == "opencode"` or OpenCode client capabilities.

**Tech Stack:** Python 3.10+, FastAPI, httpx, pytest, existing `BackendRegistry`, `BackendClient`, `SessionManager`, and Responses API models.

---

## File Map

- `README.md`: Add OpenCode setup, external/managed mode examples, streaming notes, and MCP notes.
- `.env.example`: Add any new OpenCode environment variables introduced by this plan.
- `src/backends/opencode/client.py`: Add health metadata, stronger streaming lifecycle handling, question event conversion, and generated config support.
- `src/backends/opencode/constants.py`: Add parsing helpers for new OpenCode toggles.
- `src/backends/opencode/config.py`: New focused module for merging OpenCode base config with wrapper MCP config.
- `src/backends/opencode/events.py`: New focused module for converting OpenCode event payloads into gateway chunks and question state.
- `src/mcp_config.py`: Add an exported helper that returns the validated raw MCP server config for reuse by OpenCode config conversion.
- `src/admin_service.py`: Include OpenCode mode, base URL, configured models, config source, and health data in backend diagnostics.
- `src/routes/responses.py`: Route OpenCode question continuations through backend-specific continuation support instead of Claude-only hook state.
- `src/session_manager.py`: Continue using `Session.pending_tool_call`, adding a `"backend": "opencode"` marker for OpenCode question state.
- `tests/test_opencode_backend.py`: Unit tests for OpenCode health, streaming event conversion, question state, and generated config.
- `tests/test_main_api_unit.py`: Route-level tests for OpenCode streaming and question continuation.
- `tests/test_admin_service_unit.py`: Admin diagnostics tests for OpenCode status fields.
- `tests/test_mcp_config.py`: MCP-to-OpenCode config conversion tests.
- `tests/integration/test_opencode_smoke.py`: Optional live smoke test gated by environment variables.

---

### Task 1: Operational Hardening and Documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `src/backends/opencode/client.py`
- Modify: `src/admin_service.py`
- Test: `tests/test_opencode_backend.py`
- Test: `tests/test_admin_service_unit.py`
- Create: `tests/integration/test_opencode_smoke.py`

- [ ] **Step 1: Write failing tests for OpenCode runtime metadata**

Add tests that define the metadata surface expected from the OpenCode client and admin diagnostics:

```python
def test_opencode_client_exposes_runtime_metadata(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setenv("OPENCODE_MODELS", "openai/gpt-5.5")

    from src.backends.opencode.client import OpenCodeClient

    client = OpenCodeClient()

    assert client.runtime_metadata() == {
        "mode": "external",
        "base_url": "http://127.0.0.1:4096",
        "agent": "general",
        "models": ["opencode/openai/gpt-5.5"],
        "managed_process": False,
    }
```

```python
async def test_admin_backend_health_includes_opencode_metadata(monkeypatch):
    from src.backends.base import BackendRegistry
    from src.admin_service import get_backends_health

    class FakeOpenCodeBackend:
        name = "opencode"

        def supported_models(self):
            return ["opencode/openai/gpt-5.5"]

        def get_auth_provider(self):
            class Provider:
                name = "opencode"

                def validate(self):
                    return {"valid": True, "errors": [], "config": {"mode": "external"}}

            return Provider()

        async def verify(self):
            return True

        def runtime_metadata(self):
            return {
                "mode": "external",
                "base_url": "http://127.0.0.1:4096",
                "agent": "general",
                "models": ["opencode/openai/gpt-5.5"],
                "managed_process": False,
            }

    BackendRegistry.clear()
    BackendRegistry.register("opencode", FakeOpenCodeBackend())

    health = await get_backends_health()

    opencode = next(item for item in health if item["name"] == "opencode")
    assert opencode["metadata"]["mode"] == "external"
    assert opencode["metadata"]["base_url"] == "http://127.0.0.1:4096"
    assert opencode["metadata"]["models"] == ["opencode/openai/gpt-5.5"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_opencode_backend.py::test_opencode_client_exposes_runtime_metadata tests/test_admin_service_unit.py::test_admin_backend_health_includes_opencode_metadata -q
```

Expected: FAIL because `runtime_metadata()` and admin `metadata` fields do not exist yet.

- [ ] **Step 3: Implement runtime metadata**

Add `runtime_metadata()` to `OpenCodeClient`:

```python
def runtime_metadata(self) -> Dict[str, Any]:
    mode = "external" if os.getenv("OPENCODE_BASE_URL") else "managed"
    return {
        "mode": mode,
        "base_url": self.base_url,
        "agent": self._agent,
        "models": self.supported_models(),
        "managed_process": self._process is not None,
    }
```

Update `src/admin_service.py::get_backends_health()` so each backend item includes metadata when the live backend exposes it:

```python
metadata: Dict[str, Any] = {}
runtime_metadata = getattr(backend, "runtime_metadata", None) if backend else None
if callable(runtime_metadata):
    try:
        metadata = runtime_metadata()
    except Exception:
        logger.warning("Failed to collect backend runtime metadata for %s", name, exc_info=True)
        metadata = {}
item["metadata"] = metadata
```

- [ ] **Step 4: Add live smoke test gated by explicit env**

Create `tests/integration/test_opencode_smoke.py`:

```python
import os

import pytest


pytestmark = pytest.mark.skipif(
    not os.getenv("OPENCODE_SMOKE_BASE_URL"),
    reason="OPENCODE_SMOKE_BASE_URL is required for live OpenCode smoke tests",
)


async def test_live_opencode_health_and_prompt(monkeypatch):
    monkeypatch.setenv("OPENCODE_BASE_URL", os.environ["OPENCODE_SMOKE_BASE_URL"])

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    assert await backend.verify() is True

    session = Session(session_id="opencode-smoke")
    client = await backend.create_client(
        session=session,
        model=os.getenv("OPENCODE_SMOKE_MODEL", "openai/gpt-5.5"),
        cwd=os.getenv("OPENCODE_SMOKE_CWD") or None,
    )
    chunks = [
        chunk
        async for chunk in backend.run_completion_with_client(
            client,
            "Reply with exactly: smoke-ok",
            session,
        )
    ]

    assert "smoke-ok" in (backend.parse_message(chunks) or "")
```

- [ ] **Step 5: Document OpenCode operations**

Add README sections under Configuration and Usage:

````markdown
### OpenCode Backend

OpenCode is opt-in. Claude remains the default backend when `BACKENDS` is unset.

```bash
export BACKENDS=claude,opencode
export OPENCODE_MODELS=openai/gpt-5.5
```

External server mode:

```bash
opencode serve --hostname 127.0.0.1 --port 4096
export OPENCODE_BASE_URL=http://127.0.0.1:4096
```

Managed server mode starts `opencode serve` automatically when `OPENCODE_BASE_URL` is unset. Managed mode requires the `opencode` binary on `PATH`.
````

Add smoke test instructions:

````markdown
Live OpenCode smoke test:

```bash
OPENCODE_SMOKE_BASE_URL=http://127.0.0.1:4096 \
OPENCODE_SMOKE_MODEL=openai/gpt-5.5 \
uv run pytest tests/integration/test_opencode_smoke.py -q
```
````

- [ ] **Step 6: Run verification**

Run:

```bash
uv run pytest tests/test_opencode_backend.py tests/test_admin_service_unit.py tests/integration/test_opencode_smoke.py -q
```

Expected: PASS, with the live smoke test skipped unless `OPENCODE_SMOKE_BASE_URL` is set.

- [ ] **Step 7: Commit**

```bash
git add README.md .env.example src/backends/opencode/client.py src/admin_service.py tests/test_opencode_backend.py tests/test_admin_service_unit.py tests/integration/test_opencode_smoke.py
git commit -m "docs: document and expose opencode runtime status"
```

---

### Task 2: Streaming Quality and Event Conversion

**Files:**
- Create: `src/backends/opencode/events.py`
- Modify: `src/backends/opencode/client.py`
- Modify: `src/routes/responses.py`
- Test: `tests/test_opencode_backend.py`
- Test: `tests/test_main_api_unit.py`

- [ ] **Step 1: Write failing tests for isolated event conversion**

Add tests that define a pure converter API before moving conversion logic out of `client.py`:

```python
def test_opencode_event_converter_emits_text_delta_and_final_text():
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")

    chunks = converter.convert(
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "oc-session",
                "partID": "p1",
                "field": "text",
                "delta": "hello",
            },
        }
    )

    assert chunks == [
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    ]
    assert converter.final_text() == "hello"
```

```python
def test_opencode_event_converter_ignores_other_sessions():
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")

    chunks = converter.convert(
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "other-session",
                "partID": "p1",
                "field": "text",
                "delta": "wrong",
            },
        }
    )

    assert chunks == []
    assert converter.final_text() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_opencode_backend.py::test_opencode_event_converter_emits_text_delta_and_final_text tests/test_opencode_backend.py::test_opencode_event_converter_ignores_other_sessions -q
```

Expected: FAIL because `src.backends.opencode.events` does not exist.

- [ ] **Step 3: Create event converter module**

Create `src/backends/opencode/events.py` with a small public API:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OpenCodeEventConverter:
    session_id: str
    text_by_part: Dict[str, str] = field(default_factory=dict)
    text_parts: List[str] = field(default_factory=list)
    emitted_tool_uses: set[str] = field(default_factory=set)
    emitted_tool_results: set[str] = field(default_factory=set)
    usage: Optional[Dict[str, int]] = None
    saw_activity: bool = False

    def convert(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self._event_session_id(event) != self.session_id:
            return []
        chunks: List[Dict[str, Any]] = []
        self._convert_usage_event(event)
        text_chunk = self._convert_text_event(event)
        if text_chunk:
            chunks.append(text_chunk)
        chunks.extend(self._convert_tool_event(event))
        return chunks

    def final_text(self) -> str:
        return "".join(self.text_parts)

    def finished(self, event: Dict[str, Any]) -> bool:
        return (
            event.get("type") == "session.idle"
            and self._event_session_id(event) == self.session_id
            and self.saw_activity
        )

    def error_message(self, event: Dict[str, Any]) -> Optional[str]:
        if event.get("type") != "session.error":
            return None
        event_session = self._event_session_id(event)
        if event_session not in (None, self.session_id):
            return None
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        error = props.get("error") or props.get("message") or props
        return str(error)

    def _event_session_id(self, event: Dict[str, Any]) -> Optional[str]:
        props = event.get("properties")
        if not isinstance(props, dict):
            return None
        if isinstance(props.get("sessionID"), str):
            return props["sessionID"]
        part = props.get("part")
        if isinstance(part, dict) and isinstance(part.get("sessionID"), str):
            return part["sessionID"]
        return None

    def _text_delta_chunk(self, delta: str) -> Dict[str, Any]:
        return {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": delta},
            },
        }

    def _convert_text_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        event_type = event.get("type")
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        if event_type == "message.part.delta":
            if props.get("field") not in (None, "text"):
                return None
            delta = props.get("delta")
            if not isinstance(delta, str) or not delta:
                return None
            part_id = str(props.get("partID") or props.get("partId") or "")
            if part_id:
                self.text_by_part[part_id] = self.text_by_part.get(part_id, "") + delta
            self.text_parts.append(delta)
            self.saw_activity = True
            return self._text_delta_chunk(delta)
        return None

    def _convert_usage_event(self, event: Dict[str, Any]) -> None:
        return None

    def _convert_tool_event(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        return []
```

Move the existing usage, tool, `message.part.updated`, idle, and error conversion branches from `client.py` into this module after the minimal module passes the first tests.

- [ ] **Step 4: Refactor OpenCodeClient to use the converter**

In `OpenCodeClient._run_completion_streaming()`, replace direct `OpenCodeStreamState` usage with:

```python
from src.backends.opencode.events import OpenCodeEventConverter

converter = OpenCodeEventConverter(session_id=client.session_id)

async for event in self._iter_sse_events(event_response):
    error_message = converter.error_message(event)
    if error_message:
        yield {"type": "error", "is_error": True, "error_message": error_message}
        return
    if converter.finished(event):
        break
    for chunk in converter.convert(event):
        yield chunk

text = converter.final_text()
usage = converter.usage
```

Keep the output chunk shape unchanged:

```python
assistant: Dict[str, Any] = {
    "type": "assistant",
    "content": [{"type": "text", "text": text}],
}
result: Dict[str, Any] = {"type": "result", "subtype": "success", "result": text}
```

- [ ] **Step 5: Add streaming cancellation cleanup test**

Add a route-level test that proves cancellation disconnects an OpenCode session client:

```python
def test_opencode_streaming_disconnects_client_on_error(isolated_session_manager):
    from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel
    import src.main as main

    class FakeOpenCodeSessionClient:
        stream_events = False

        def __init__(self):
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    created_client = FakeOpenCodeSessionClient()

    def resolve(model):
        if model == "opencode/openai/gpt-5.5":
            return ResolvedModel(model, "opencode", "openai/gpt-5.5")
        return None

    async def create_client(**kwargs):
        return created_client

    async def run_completion_with_client(client, prompt, session):
        yield {"type": "error", "is_error": True, "error_message": "stream failed"}

    backend = MagicMock()
    backend.name = "opencode"
    backend.create_client = create_client
    backend.run_completion_with_client = run_completion_with_client
    backend.parse_message.return_value = None
    backend.estimate_token_usage.return_value = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    BackendRegistry.clear()
    BackendRegistry.register_descriptor(
        BackendDescriptor("opencode", "opencode", ["opencode/openai/gpt-5.5"], resolve)
    )
    BackendRegistry.register("opencode", backend)

    with client_context() as (client, _):
        response = client.post(
            "/v1/responses",
            json={"model": "opencode/openai/gpt-5.5", "input": "hi", "stream": True},
        )

    assert response.status_code == 200
    assert "response.failed" in response.text
    assert created_client.disconnected is True
```

- [ ] **Step 6: Run streaming tests**

Run:

```bash
uv run pytest tests/test_opencode_backend.py tests/test_main_api_unit.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/backends/opencode/events.py src/backends/opencode/client.py src/routes/responses.py tests/test_opencode_backend.py tests/test_main_api_unit.py
git commit -m "refactor: isolate opencode event conversion"
```

---

### Task 3: OpenCode Question Continuation

**Files:**
- Modify: `src/backends/opencode/events.py`
- Modify: `src/backends/opencode/client.py`
- Modify: `src/routes/responses.py`
- Modify: `src/session_manager.py`
- Test: `tests/test_opencode_backend.py`
- Test: `tests/test_main_api_unit.py`
- Test: `tests/test_ask_user_question.py`

- [ ] **Step 1: Define OpenCode question event behavior with failing tests**

Add converter tests for OpenCode question events. The route should ultimately emit the existing Responses `requires_action` shape, so the backend chunk should use the same `tool_use` content type the current route already understands:

```python
def test_opencode_event_converter_emits_question_tool_use():
    from src.backends.opencode.events import OpenCodeEventConverter

    converter = OpenCodeEventConverter(session_id="oc-session")
    chunks = converter.convert(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "sessionID": "oc-session",
                    "type": "tool",
                    "tool": "question",
                    "callID": "q1",
                    "state": {
                        "status": "running",
                        "input": {
                            "question": "Continue?",
                            "options": [{"label": "Yes"}, {"label": "No"}],
                        },
                    },
                }
            },
        }
    )

    assert chunks == [
        {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "q1",
                    "name": "question",
                    "input": {
                        "question": "Continue?",
                        "options": [{"label": "Yes"}, {"label": "No"}],
                    },
                }
            ],
        }
    ]
    assert converter.pending_question == {
        "call_id": "q1",
        "name": "question",
        "arguments": {
            "question": "Continue?",
            "options": [{"label": "Yes"}, {"label": "No"}],
        },
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_opencode_backend.py::test_opencode_event_converter_emits_question_tool_use -q
```

Expected: FAIL because `pending_question` does not exist.

- [ ] **Step 3: Extend converter state**

Add `pending_question` to `OpenCodeEventConverter`:

```python
pending_question: Optional[Dict[str, Any]] = None
```

When converting a tool event where `part["tool"] == "question"` and the input contains a non-empty `question`, set:

```python
self.pending_question = {
    "call_id": call_id,
    "name": "question",
    "arguments": input_value,
}
```

Keep emitting the existing `tool_use` chunk so current Responses helpers can build `requires_action`.

- [ ] **Step 4: Add backend continuation API**

Add an OpenCode-specific continuation method to `OpenCodeClient`:

```python
async def resume_question_with_client(
    self,
    client: OpenCodeSessionClient,
    call_id: str,
    output: str,
    session: Any,
) -> AsyncGenerator[Dict[str, Any], None]:
    body = {
        "agent": self._agent,
        "parts": [
            {
                "type": "tool",
                "callID": call_id,
                "tool": "question",
                "state": {"status": "completed", "output": output},
            }
        ],
    }
    model = self._split_provider_model(client.model)
    if model:
        body["model"] = model
    async with httpx.AsyncClient(**self._client_kwargs()) as http_client:
        response = await http_client.post(
            f"/session/{client.session_id}/message",
            json=body,
            params=self._directory_params(client.cwd),
        )
        response.raise_for_status()
        payload = response.json()
    text = self._extract_text(payload)
    yield {"type": "assistant", "content": [{"type": "text", "text": text}]}
    yield {"type": "result", "subtype": "success", "result": text}
```

Use this method as the only route-facing continuation API. If the live smoke test shows OpenCode requires a different HTTP endpoint or body shape, update `resume_question_with_client()` and keep `_handle_function_call_output()` unchanged.

- [ ] **Step 5: Store OpenCode pending question on session**

When streaming or non-streaming chunks include a `question` tool use from OpenCode, set session state:

```python
session.pending_tool_call = {
    "call_id": tool_use["id"],
    "name": tool_use["name"],
    "arguments": tool_use.get("input", {}),
    "backend": "opencode",
}
```

Keep the existing Claude fields intact for Claude sessions.

- [ ] **Step 6: Route function_call_output to OpenCode continuation**

In `_handle_function_call_output()`, branch on `resolved.backend`:

```python
if resolved.backend == "opencode":
    resume = getattr(backend, "resume_question_with_client", None)
    if resume is None:
        raise HTTPException(
            status_code=400,
            detail="OpenCode question continuation is not supported by this backend",
        )
    chunks = [
        chunk
        async for chunk in resume(
            session.client,
            fc_output["call_id"],
            fc_output["output"],
            session,
        )
    ]
    assistant_text = backend.parse_message(chunks) or ""
    if not assistant_text:
        raise HTTPException(status_code=502, detail="No response from backend")
```

Use the same response ID, turn counter, usage logging, and streaming/non-streaming output rules already used by the Claude continuation path.

- [ ] **Step 7: Add route-level continuation test**

Add a test that starts with an OpenCode session containing a pending question and then sends `function_call_output`:

```python
def test_opencode_function_call_output_resumes_question(isolated_session_manager):
    from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel
    from src.session_manager import session_manager

    session_id = str(uuid.uuid4())
    session = session_manager.get_or_create_session(session_id)
    session.backend = "opencode"
    session.turn_counter = 1
    session.last_response_id = f"resp_{session_id}_1"
    session.client = object()
    session.pending_tool_call = {
        "call_id": "q1",
        "name": "question",
        "arguments": {"question": "Continue?"},
        "backend": "opencode",
    }

    def resolve(model):
        if model == "opencode/openai/gpt-5.5":
            return ResolvedModel(model, "opencode", "openai/gpt-5.5")
        return None

    async def resume_question_with_client(client, call_id, output, session):
        assert call_id == "q1"
        assert output == "yes"
        yield {"type": "assistant", "content": [{"type": "text", "text": "continued"}]}
        yield {"type": "result", "subtype": "success", "result": "continued"}

    backend = MagicMock()
    backend.name = "opencode"
    backend.resume_question_with_client = resume_question_with_client
    backend.parse_message.return_value = "continued"
    backend.estimate_token_usage.return_value = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    BackendRegistry.clear()
    BackendRegistry.register_descriptor(
        BackendDescriptor("opencode", "opencode", ["opencode/openai/gpt-5.5"], resolve)
    )
    BackendRegistry.register("opencode", backend)

    with client_context() as (client, _):
        response = client.post(
            "/v1/responses",
            json={
                "model": "opencode/openai/gpt-5.5",
                "previous_response_id": session.last_response_id,
                "input": [{"type": "function_call_output", "call_id": "q1", "output": "yes"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["output"][0]["content"][0]["text"] == "continued"
```

- [ ] **Step 8: Run continuation tests**

Run:

```bash
uv run pytest tests/test_opencode_backend.py tests/test_main_api_unit.py tests/test_ask_user_question.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/backends/opencode/events.py src/backends/opencode/client.py src/routes/responses.py src/session_manager.py tests/test_opencode_backend.py tests/test_main_api_unit.py tests/test_ask_user_question.py
git commit -m "feat: support opencode question continuation"
```

---

### Task 4: MCP and OpenCode Config Integration

**Files:**
- Create: `src/backends/opencode/config.py`
- Modify: `src/backends/opencode/client.py`
- Modify: `src/backends/opencode/constants.py`
- Modify: `src/mcp_config.py`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `tests/test_mcp_config.py`
- Test: `tests/test_opencode_backend.py`

- [ ] **Step 1: Write failing tests for config generation**

Add tests for converting wrapper MCP config into OpenCode config:

```python
def test_build_opencode_config_includes_safe_defaults_and_mcp_servers():
    from src.backends.opencode.config import build_opencode_config

    config = build_opencode_config(
        base_config={},
        mcp_servers={
            "filesystem": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
            }
        },
        default_model="openai/gpt-5.5",
        question_permission="deny",
    )

    assert config["permission"]["question"] == "deny"
    assert config["share"] == "disabled"
    assert config["model"] == "openai/gpt-5.5"
    assert config["mcp"]["filesystem"]["type"] == "stdio"
    assert config["mcp"]["filesystem"]["command"] == "npx"
```

```python
def test_build_opencode_config_preserves_explicit_base_config_over_defaults():
    from src.backends.opencode.config import build_opencode_config

    config = build_opencode_config(
        base_config={"share": "enabled", "permission": {"question": "ask"}},
        mcp_servers={},
        default_model=None,
        question_permission="deny",
    )

    assert config["share"] == "enabled"
    assert config["permission"]["question"] == "ask"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_mcp_config.py::test_build_opencode_config_includes_safe_defaults_and_mcp_servers tests/test_mcp_config.py::test_build_opencode_config_preserves_explicit_base_config_over_defaults -q
```

Expected: FAIL because `src.backends.opencode.config` does not exist.

- [ ] **Step 3: Create config builder**

Create `src/backends/opencode/config.py`:

```python
from __future__ import annotations

import copy
import json
from typing import Any, Dict, Optional


def _deep_merge_missing(target: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
            continue
        if isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge_missing(target[key], value)
    return target


def build_opencode_config(
    *,
    base_config: Dict[str, Any],
    mcp_servers: Dict[str, Dict[str, Any]],
    default_model: Optional[str],
    question_permission: str,
) -> Dict[str, Any]:
    config = copy.deepcopy(base_config)
    defaults: Dict[str, Any] = {
        "permission": {"question": question_permission},
        "share": "disabled",
    }
    if default_model:
        defaults["model"] = default_model
    _deep_merge_missing(config, defaults)
    if mcp_servers:
        config.setdefault("mcp", {})
        for name, server in mcp_servers.items():
            config["mcp"].setdefault(name, copy.deepcopy(server))
    return config


def parse_opencode_config_content(content: Optional[str]) -> Dict[str, Any]:
    if not content:
        return {}
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("OPENCODE_CONFIG_CONTENT must be a JSON object")
    return parsed
```

- [ ] **Step 4: Export reusable MCP config**

In `src/mcp_config.py`, keep `get_mcp_servers()` as-is and add:

```python
def get_validated_mcp_config() -> McpServersDict:
    """Return the validated wrapper MCP config for backend-specific conversion."""
    return dict(_server_mcp_config)
```

Use this helper from OpenCode code rather than re-reading `MCP_CONFIG`.

- [ ] **Step 5: Wire generated config into managed OpenCode startup**

Update `OpenCodeClient._managed_config_content()`:

```python
def _managed_config_content(self) -> str:
    from src.backends.opencode.config import (
        build_opencode_config,
        parse_opencode_config_content,
    )
    from src.mcp_config import get_validated_mcp_config

    base_config = parse_opencode_config_content(os.getenv("OPENCODE_CONFIG_CONTENT"))
    config = build_opencode_config(
        base_config=base_config,
        mcp_servers=get_validated_mcp_config()
        if os.getenv("OPENCODE_USE_WRAPPER_MCP_CONFIG", "false").lower() == "true"
        else {},
        default_model=os.getenv("OPENCODE_DEFAULT_MODEL") or None,
        question_permission=os.getenv("OPENCODE_QUESTION_PERMISSION", "deny"),
    )
    return json.dumps(config)
```

External server mode should not generate or send config because the gateway does not own that process.

- [ ] **Step 6: Add client-level config test**

Add:

```python
def test_managed_config_can_include_wrapper_mcp(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    monkeypatch.setenv("OPENCODE_USE_WRAPPER_MCP_CONFIG", "true")
    monkeypatch.setenv("OPENCODE_DEFAULT_MODEL", "openai/gpt-5.5")
    monkeypatch.setattr(
        "src.mcp_config.get_validated_mcp_config",
        lambda: {"demo": {"type": "stdio", "command": "uvx", "args": ["demo"]}},
    )

    from src.backends.opencode.client import OpenCodeClient

    backend = OpenCodeClient(base_url="http://127.0.0.1:4096")
    config = json.loads(backend._managed_config_content())

    assert config["model"] == "openai/gpt-5.5"
    assert config["permission"]["question"] == "deny"
    assert config["mcp"]["demo"]["command"] == "uvx"
```

- [ ] **Step 7: Document MCP integration controls**

Add to `.env.example`:

```bash
# Include validated wrapper MCP_CONFIG in managed OpenCode config.
# External OpenCode servers must be configured separately.
# OPENCODE_USE_WRAPPER_MCP_CONFIG=false
# OPENCODE_QUESTION_PERMISSION=deny
```

Add README notes:

```markdown
OpenCode MCP integration is available only in managed mode. Set `OPENCODE_USE_WRAPPER_MCP_CONFIG=true` to copy the validated wrapper `MCP_CONFIG` into the generated OpenCode config. External OpenCode servers keep their own config and are not modified by the gateway.
```

- [ ] **Step 8: Run MCP/config tests**

Run:

```bash
uv run pytest tests/test_mcp_config.py tests/test_opencode_backend.py -q
```

Expected: PASS.

- [ ] **Step 9: Run final verification**

Run:

```bash
uv run pytest tests/test_opencode_backend.py tests/test_main_api_unit.py tests/test_ask_user_question.py tests/test_mcp_config.py tests/test_admin_service_unit.py -q
uv run pytest -q
```

Expected: PASS, with live OpenCode smoke tests skipped unless their env vars are set.

- [ ] **Step 10: Commit**

```bash
git add src/backends/opencode/config.py src/backends/opencode/client.py src/backends/opencode/constants.py src/mcp_config.py .env.example README.md tests/test_mcp_config.py tests/test_opencode_backend.py
git commit -m "feat: generate opencode config from wrapper mcp"
```
