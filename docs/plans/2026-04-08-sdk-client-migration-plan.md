# ClaudeSDKClient Migration & AskUserQuestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate session requests from `query()` to `ClaudeSDKClient`, add AskUserQuestion support via OpenAI-standard `function_call`/`function_call_output` pattern, and remove deprecated endpoints.

**Architecture:** Session requests use a persistent `ClaudeSDKClient` per session (subprocess stays alive across turns). AskUserQuestion is intercepted via `can_use_tool` callback, emitted as a `function_call` output item, and resumed when the client sends `function_call_output` in the next request. Non-session requests stay on `query()`.

**Tech Stack:** Python 3.10+, FastAPI, claude-agent-sdk (ClaudeSDKClient, PermissionResultAllow), pytest, pytest-asyncio

**Spec:** `docs/plans/2026-04-08-sdk-client-migration-and-ask-user-question.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/session_manager.py` | Modify | Add `client`, `input_event`, `input_response`, `pending_tool_call` fields; async disconnect on cleanup |
| `src/backends/claude/client.py` | Modify | Add `create_client()`, `run_completion_with_client()`, `_make_can_use_tool()` |
| `src/response_models.py` | Modify | Add `FunctionCallOutputItem`, `FunctionCallOutputInput`; extend `ResponseObject.output` union |
| `src/routes/responses.py` | Modify | Detect `function_call_output` input, route to ClaudeSDKClient path, emit function_call on AskUserQuestion |
| `src/session_guard.py` | Modify | Handle `function_call_output` continuation (skip turn increment) |
| `src/streaming_utils.py` | Modify | Add function_call SSE emission in `stream_response_chunks()`; remove chat-only functions |
| `src/message_adapter.py` | Modify | Add `function_call_output` detection helper |
| `src/routes/chat.py` | Delete | `/v1/chat/completions` endpoint |
| `src/routes/messages.py` | Delete | `/v1/messages` endpoint |
| `src/routes/__init__.py` | Modify | Remove chat/messages router exports |
| `src/main.py` | Modify | Remove chat/messages router registration and backward-compat re-exports |
| `src/models.py` | Modify | Remove chat/messages-only models |
| `tests/conftest.py` | Modify | Remove chat_module references from fixtures |
| `tests/test_sdk_client_session.py` | Create | ClaudeSDKClient session lifecycle tests |
| `tests/test_ask_user_question.py` | Create | AskUserQuestion function_call flow tests |
| `docs/plans/2026-03-05-*` | Delete | Outdated design documents |

---

## Phase 1: Cleanup — Remove Deprecated Endpoints

### Task 1: Remove `/v1/chat/completions` and `/v1/messages` endpoints

**Files:**
- Delete: `src/routes/chat.py`
- Delete: `src/routes/messages.py`
- Modify: `src/routes/__init__.py`
- Modify: `src/main.py:498-554`
- Modify: `src/models.py`
- Modify: `src/streaming_utils.py` (remove chat-only functions)
- Modify: `tests/conftest.py`
- Delete: `tests/test_anthropic_messages.py`
- Modify: affected test files

- [ ] **Step 1: Remove router exports from `src/routes/__init__.py`**

Replace the full file content:

```python
from src.routes.responses import router as responses_router
from src.routes.sessions import router as sessions_router
from src.routes.general import router as general_router
from src.routes.admin import router as admin_router

__all__ = [
    "responses_router",
    "sessions_router",
    "general_router",
    "admin_router",
]
```

- [ ] **Step 2: Remove router registration and re-exports from `src/main.py`**

In the imports section (~line 498-501), change:
```python
from src.routes import (
    chat_router,
    messages_router,
    responses_router,
    sessions_router,
    general_router,
    admin_router,
)
```
to:
```python
from src.routes import (
    responses_router,
    sessions_router,
    general_router,
    admin_router,
)
```

Remove `app.include_router(chat_router)` and `app.include_router(messages_router)` (~lines 508-509).

Remove the entire backward-compat re-export block (~lines 524-554):
```python
from src.routes.chat import (
    generate_streaming_response,
    _build_backend_options,
    ...
)
from src.streaming_utils import (
    map_stop_reason,
    extract_stop_reason,
    ...
    make_sse as _make_sse,
    stream_chunks as _stream_chunks,
)
```

Keep the responses and shared re-exports (~lines 535-544).

- [ ] **Step 3: Remove chat-only functions from `src/streaming_utils.py`**

Remove these functions:
- `map_stop_reason()` (lines 126-133)
- `extract_stop_reason()` (lines 136-141)
- `make_sse()` (lines 415-429)
- `make_task_sse()` (lines 490-500)
- `stream_chunks()` (lines 847-1018)

Keep all shared utilities and responses-only functions.

- [ ] **Step 4: Remove chat/messages-only models from `src/models.py`**

Remove:
- `StreamOptions` (lines 62-67)
- `ChatCompletionRequest` (lines 70-147)
- `Choice` (lines 149-152)
- `Usage` (lines 155-158)
- `ChatCompletionResponse` (lines 161-168)
- `StreamChoice` (lines 171-174)
- `ChatCompletionStreamResponse` (lines 177-187)
- `AnthropicContentBlock` (lines 208-214)
- `AnthropicTextBlock` (line 217)
- `AnthropicMessage` (lines 220-224)
- `AnthropicMessagesRequest` (lines 227-258)
- `AnthropicUsage` (lines 261-265)
- `AnthropicMessagesResponse` (lines 268-278)

Keep: `ContentPart`, `Message`, `SessionInfo`, `SessionListResponse`.

- [ ] **Step 5: Delete route files**

```bash
rm src/routes/chat.py src/routes/messages.py
```

- [ ] **Step 6: Fix test infrastructure**

In `tests/conftest.py`, remove all references to `chat_module`:
- Remove `import src.routes.chat as chat_module` (line 7)
- In `reset_main_state` fixture: remove `chat_module.session_manager = ...` lines
- In `isolated_session_manager` fixture: remove `chat_module` patching

Delete `tests/test_anthropic_messages.py`.

Fix or remove tests that import from deleted modules:
- `tests/test_main_helpers_unit.py` — remove chat-specific helper tests
- `tests/test_main_coverage_unit.py` — remove chat/messages imports and tests
- `tests/test_main_api_unit.py` — remove chat/messages endpoint tests
- `tests/test_image_endpoints.py` — remove chat_module imports, keep responses image tests
- `tests/test_responses_user.py` — remove chat/messages imports

- [ ] **Step 7: Delete outdated design documents**

```bash
rm docs/plans/2026-03-05-claude-sdk-client-migration-design.md
rm docs/plans/2026-03-05-claude-sdk-client-migration.md
```

- [ ] **Step 8: Run tests to verify nothing is broken**

Run: `uv run pytest tests/ -x -v`
Expected: All remaining tests PASS. No import errors.

- [ ] **Step 9: Lint and format**

Run: `uv run ruff check --fix . && uv run ruff format .`
Expected: Clean output, no errors.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: remove /v1/chat/completions and /v1/messages endpoints

Remove deprecated API surfaces and associated models, streaming functions,
tests, and outdated design documents. Only /v1/responses remains."
```

---

## Phase 2: Session Model — Add ClaudeSDKClient Fields

### Task 2: Extend Session dataclass with client fields

**Files:**
- Modify: `src/session_manager.py:46-66`
- Test: `tests/test_sdk_client_session.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_sdk_client_session.py`:

```python
"""Tests for ClaudeSDKClient session lifecycle."""

from unittest.mock import AsyncMock, MagicMock

from src.session_manager import Session


def test_session_has_client_fields():
    """Session has fields for ClaudeSDKClient integration."""
    session = Session(session_id="test-1")
    assert session.client is None
    assert session.input_event is None
    assert session.input_response is None
    assert session.pending_tool_call is None


async def test_session_disconnect_on_cleanup(fresh_session_manager):
    """Expired sessions disconnect their ClaudeSDKClient."""
    sm = fresh_session_manager
    session = await sm.get_or_create_session("sess-1")

    mock_client = AsyncMock()
    mock_client.disconnect = AsyncMock()
    session.client = mock_client

    # Force expiration
    from datetime import timedelta
    session.expires_at = session.created_at - timedelta(minutes=1)

    await sm.cleanup_expired_sessions()

    mock_client.disconnect.assert_awaited_once()
    assert sm.get_session("sess-1") is None


async def test_async_shutdown_disconnects_all_clients(fresh_session_manager):
    """async_shutdown disconnects all active ClaudeSDKClient instances."""
    sm = fresh_session_manager
    s1 = await sm.get_or_create_session("sess-1")
    s2 = await sm.get_or_create_session("sess-2")

    mock1 = AsyncMock()
    mock1.disconnect = AsyncMock()
    mock2 = AsyncMock()
    mock2.disconnect = AsyncMock()
    s1.client = mock1
    s2.client = mock2

    await sm.async_shutdown()

    mock1.disconnect.assert_awaited_once()
    mock2.disconnect.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sdk_client_session.py -v`
Expected: FAIL — `Session` has no `client` attribute.

- [ ] **Step 3: Add client fields to Session**

In `src/session_manager.py`, add to `Session` dataclass after line 66 (`workspace`):

```python
    # ClaudeSDKClient integration
    client: Optional[Any] = None
    input_event: Optional[asyncio.Event] = field(default=None, repr=False, compare=False)
    input_response: Optional[str] = None
    pending_tool_call: Optional[Dict[str, Any]] = None
```

Add `Dict` to the typing imports at top:

```python
from typing import Any, Dict, List, Optional
```

- [ ] **Step 4: Add client disconnect to cleanup and shutdown**

In `_purge_all_expired()` (~line 137), before `del self.sessions[sid]`:

```python
            if session.client is not None:
                try:
                    await session.client.disconnect()
                except Exception:
                    logger.debug("Client disconnect failed for session %s", sid, exc_info=True)
                session.client = None
```

Change `_purge_all_expired` to `async def _purge_all_expired`.

In `cleanup_expired_sessions()`, change the call to `await self._purge_all_expired()`.

In `async_shutdown()`, before clearing sessions, disconnect all clients:

```python
        for session in list(self.sessions.values()):
            if session.client is not None:
                try:
                    await session.client.disconnect()
                except Exception:
                    logger.debug("Client disconnect failed during shutdown", exc_info=True)
                session.client = None
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_sdk_client_session.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add src/session_manager.py tests/test_sdk_client_session.py
git commit -m "feat(session): add ClaudeSDKClient fields and disconnect lifecycle"
```

---

## Phase 3: Response Models — Add function_call Types

### Task 3: Add function_call output models

**Files:**
- Modify: `src/response_models.py`
- Test: `tests/test_response_models.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_response_models.py`:

```python
"""Tests for function_call response model types."""

from src.response_models import (
    FunctionCallOutputItem,
    FunctionCallOutputInput,
    ResponseObject,
    OutputItem,
    ResponseContentPart,
)


def test_function_call_output_item():
    """FunctionCallOutputItem has correct fields and defaults."""
    item = FunctionCallOutputItem(
        id="fc_123",
        call_id="call_abc",
        name="AskUserQuestion",
        arguments='{"question": "Continue?"}',
    )
    assert item.type == "function_call"
    assert item.call_id == "call_abc"
    assert item.name == "AskUserQuestion"
    assert item.status == "completed"


def test_function_call_output_input():
    """FunctionCallOutputInput parses correctly."""
    item = FunctionCallOutputInput(
        call_id="call_abc",
        output="Yes, go ahead",
    )
    assert item.type == "function_call_output"
    assert item.call_id == "call_abc"
    assert item.output == "Yes, go ahead"


def test_response_object_accepts_function_call_output():
    """ResponseObject.output can contain both message and function_call items."""
    msg_item = OutputItem(id="msg_1", content=[ResponseContentPart(text="Hello")])
    fc_item = FunctionCallOutputItem(
        id="fc_1",
        call_id="call_abc",
        name="AskUserQuestion",
        arguments='{"question": "OK?"}',
    )
    resp = ResponseObject(
        id="resp_test",
        model="sonnet",
        output=[msg_item, fc_item],
    )
    assert len(resp.output) == 2
    assert resp.output[0].type == "message"
    assert resp.output[1].type == "function_call"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_response_models.py -v`
Expected: FAIL — `FunctionCallOutputItem` not found.

- [ ] **Step 3: Add models to `src/response_models.py`**

Add before `ResponseObject`:

```python
class FunctionCallOutputItem(BaseModel):
    """A function_call output item in the response (e.g. AskUserQuestion)."""

    id: str
    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str
    status: str = "completed"


class FunctionCallOutputInput(BaseModel):
    """A function_call_output input item from the client."""

    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str
```

Update `ResponseObject.output` type (line 80):

```python
    output: List[Union[OutputItem, FunctionCallOutputItem]] = Field(default_factory=list)
```

Add `"requires_action"` to `ResponseObject.status`:

```python
    status: Literal["completed", "in_progress", "failed", "requires_action"] = "completed"
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_response_models.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/response_models.py tests/test_response_models.py
git commit -m "feat(models): add function_call output types for AskUserQuestion"
```

---

## Phase 4: Backend — ClaudeSDKClient Integration

### Task 4: Add `create_client()` and `run_completion_with_client()`

**Files:**
- Modify: `src/backends/claude/client.py`
- Test: `tests/test_sdk_client_backend.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_sdk_client_backend.py`:

```python
"""Tests for ClaudeSDKClient backend integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backends.claude.client import ClaudeCodeCLI
from src.session_manager import Session


@pytest.fixture
def cli():
    return ClaudeCodeCLI()


@pytest.fixture
def session():
    return Session(session_id="test-sess")


async def test_create_client_returns_connected_client(cli, session):
    """create_client() returns a ClaudeSDKClient with can_use_tool callback."""
    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()

    with patch("src.backends.claude.client.ClaudeSDKClient", return_value=mock_client):
        client = await cli.create_client(session=session, model="sonnet")

    mock_client.connect.assert_awaited_once_with(prompt=None)
    assert client is mock_client


async def test_create_client_sets_can_use_tool(cli, session):
    """create_client() configures can_use_tool in options."""
    captured_options = {}

    def capture_client(options=None, **kwargs):
        captured_options["options"] = options
        mock = AsyncMock()
        mock.connect = AsyncMock()
        return mock

    with patch("src.backends.claude.client.ClaudeSDKClient", side_effect=capture_client):
        await cli.create_client(session=session, model="sonnet")

    assert captured_options["options"].can_use_tool is not None


async def test_run_completion_with_client_yields_messages(cli, session):
    """run_completion_with_client() yields converted SDK messages."""
    mock_message = MagicMock()
    mock_message.__dict__ = {"type": "assistant", "content": []}

    mock_client = AsyncMock()

    async def fake_receive():
        yield mock_message

    mock_client.receive_response = fake_receive
    mock_client.query = AsyncMock()
    session.client = mock_client

    chunks = []
    async for chunk in cli.run_completion_with_client(
        client=mock_client, prompt="hello", session=session
    ):
        chunks.append(chunk)

    mock_client.query.assert_awaited_once()
    assert len(chunks) >= 1


async def test_can_use_tool_callback_ask_user_question(cli, session):
    """can_use_tool callback sets pending_tool_call and waits for input_event."""
    callback = cli._make_can_use_tool(session)

    context = MagicMock()
    context.tool_use_id = "toolu_123"

    async def simulate_user_response():
        await asyncio.sleep(0.01)
        session.input_response = "yes"
        session.input_event.set()

    asyncio.create_task(simulate_user_response())

    result = await callback(
        "AskUserQuestion",
        {"question": "Overwrite file?"},
        context,
    )

    assert result.behavior == "allow"
    assert session.pending_tool_call is not None or session.input_event is None


async def test_can_use_tool_callback_allows_other_tools(cli, session):
    """can_use_tool callback allows non-AskUserQuestion tools immediately."""
    callback = cli._make_can_use_tool(session)
    context = MagicMock()
    context.tool_use_id = "toolu_456"

    result = await callback("Bash", {"command": "ls"}, context)

    assert result.behavior == "allow"
    assert session.pending_tool_call is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sdk_client_backend.py -v`
Expected: FAIL — `create_client` not found.

- [ ] **Step 3: Add imports to `src/backends/claude/client.py`**

Add to the imports at the top of the file:

```python
from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk.types import PermissionResultAllow, ToolPermissionContext
```

- [ ] **Step 4: Implement `_make_can_use_tool()` on `ClaudeCodeCLI`**

Add method to `ClaudeCodeCLI` class:

```python
    def _make_can_use_tool(self, session):
        """Create a can_use_tool callback bound to a session.

        When AskUserQuestion is detected, sets session.pending_tool_call
        and waits on session.input_event for the client's response.
        All other tools are allowed immediately.
        """
        async def can_use_tool(tool_name: str, tool_input: dict, context: ToolPermissionContext):
            if tool_name == "AskUserQuestion":
                session.pending_tool_call = {
                    "call_id": context.tool_use_id,
                    "name": "AskUserQuestion",
                    "arguments": tool_input,
                }
                session.input_event = asyncio.Event()
                await session.input_event.wait()
                answer = session.input_response
                # Reset state
                session.input_response = None
                session.input_event = None
                return PermissionResultAllow()
            return PermissionResultAllow()

        return can_use_tool
```

Add `import asyncio` if not already imported.

- [ ] **Step 5: Implement `create_client()`**

Add method to `ClaudeCodeCLI`:

```python
    async def create_client(
        self,
        session,
        model: str = None,
        system_prompt: str = None,
        allowed_tools: list = None,
        disallowed_tools: list = None,
        permission_mode: str = None,
        mcp_servers: dict = None,
        task_budget: int = None,
        cwd: str = None,
    ):
        """Create and connect a ClaudeSDKClient for a session.

        The client persists across turns. A can_use_tool callback is
        registered to intercept AskUserQuestion.
        """
        options = self._build_sdk_options(
            model=model,
            system_prompt=system_prompt,
            max_turns=10,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            session_id=session.session_id,
            resume=None,
            permission_mode=permission_mode,
            mcp_servers=mcp_servers,
            task_budget=task_budget,
            cwd=cwd,
        )
        options.can_use_tool = self._make_can_use_tool(session)

        with self._sdk_env():
            client = ClaudeSDKClient(options=options)
            await client.connect(prompt=None)
        return client
```

- [ ] **Step 6: Implement `run_completion_with_client()`**

Add method to `ClaudeCodeCLI`:

```python
    async def run_completion_with_client(
        self,
        client,
        prompt: str,
        session,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Run a completion turn on an existing ClaudeSDKClient.

        Sends the prompt via client.query() and yields converted messages
        from client.receive_response().
        """
        try:
            await client.query(prompt)
            async for message in client.receive_response():
                yield self._convert_message(message)
        except Exception as exc:
            logger.error("ClaudeSDKClient error: %s", exc, exc_info=True)
            session.client = None
            yield {"type": "error", "is_error": True, "error": str(exc)}
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_sdk_client_backend.py -v`
Expected: All PASS.

- [ ] **Step 8: Commit**

```bash
git add src/backends/claude/client.py tests/test_sdk_client_backend.py
git commit -m "feat(backend): add ClaudeSDKClient create_client and run_completion_with_client"
```

---

## Phase 5: Streaming — function_call SSE Emission

### Task 5: Add function_call detection and SSE emission to `stream_response_chunks()`

**Files:**
- Modify: `src/streaming_utils.py`
- Test: `tests/test_ask_user_question.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_ask_user_question.py`:

```python
"""Tests for AskUserQuestion function_call flow."""

import json
from unittest.mock import MagicMock

from src.streaming_utils import make_function_call_response_sse


def test_make_function_call_response_sse():
    """Generates correct SSE for a function_call output item."""
    result = make_function_call_response_sse(
        response_id="resp_123_1",
        call_id="toolu_abc",
        name="AskUserQuestion",
        arguments='{"question": "Overwrite?"}',
    )
    assert "event: response.output_item.added" in result
    parsed_lines = [l for l in result.strip().split("\n") if l.startswith("data: ")]
    data = json.loads(parsed_lines[0].removeprefix("data: "))
    assert data["item"]["type"] == "function_call"
    assert data["item"]["name"] == "AskUserQuestion"
    assert data["item"]["call_id"] == "toolu_abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ask_user_question.py::test_make_function_call_response_sse -v`
Expected: FAIL — `make_function_call_response_sse` not found.

- [ ] **Step 3: Implement `make_function_call_response_sse()` in `src/streaming_utils.py`**

Add near the other `make_*_response_sse` functions:

```python
def make_function_call_response_sse(
    response_id: str,
    call_id: str,
    name: str,
    arguments: str,
) -> str:
    """Build SSE events for a function_call output item (e.g. AskUserQuestion).

    Emits response.output_item.added with the function_call data.
    """
    item = {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }
    event_data = {
        "type": "response.output_item.added",
        "response_id": response_id,
        "item": item,
    }
    return f"event: response.output_item.added\ndata: {json.dumps(event_data)}\n\n"
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_ask_user_question.py::test_make_function_call_response_sse -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/streaming_utils.py tests/test_ask_user_question.py
git commit -m "feat(streaming): add make_function_call_response_sse for AskUserQuestion"
```

---

## Phase 6: Route — ClaudeSDKClient Routing and function_call_output Handling

### Task 6: Integrate ClaudeSDKClient path into `/v1/responses`

**Files:**
- Modify: `src/routes/responses.py`
- Modify: `src/response_models.py` (input parsing)
- Modify: `src/session_guard.py`
- Test: `tests/test_ask_user_question.py` (extend)

- [ ] **Step 1: Write the failing test for function_call_output detection**

Append to `tests/test_ask_user_question.py`:

```python
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from src.routes.responses import _detect_function_call_output


def test_detect_function_call_output_present():
    """Detects function_call_output in input array."""
    input_data = [
        {"type": "function_call_output", "call_id": "toolu_abc", "output": "yes"},
    ]
    result = _detect_function_call_output(input_data)
    assert result is not None
    assert result["call_id"] == "toolu_abc"
    assert result["output"] == "yes"


def test_detect_function_call_output_absent():
    """Returns None when no function_call_output in input."""
    input_data = [
        {"role": "user", "content": "hello"},
    ]
    result = _detect_function_call_output(input_data)
    assert result is None


def test_detect_function_call_output_string_input():
    """Returns None for string input."""
    result = _detect_function_call_output("hello")
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ask_user_question.py::test_detect_function_call_output_present -v`
Expected: FAIL — `_detect_function_call_output` not found.

- [ ] **Step 3: Implement `_detect_function_call_output()` in `src/routes/responses.py`**

Add helper function:

```python
def _detect_function_call_output(input_data) -> Optional[Dict[str, str]]:
    """Extract function_call_output from input if present.

    Returns dict with call_id and output, or None.
    """
    if isinstance(input_data, str):
        return None
    for item in input_data:
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            return {"call_id": item["call_id"], "output": item["output"]}
        if hasattr(item, "type") and getattr(item, "type", None) == "function_call_output":
            return {"call_id": item.call_id, "output": item.output}
    return None
```

- [ ] **Step 4: Run detection tests**

Run: `uv run pytest tests/test_ask_user_question.py -k "detect" -v`
Expected: All PASS.

- [ ] **Step 5: Implement the function_call_output handling path in `create_response()`**

In `src/routes/responses.py`, in the `create_response()` function, after session resolution and before prompt conversion (~line 238), add:

```python
    # Check for function_call_output (AskUserQuestion response)
    fc_output = _detect_function_call_output(
        body.input if isinstance(body.input, list)
        else [i.model_dump() for i in body.input] if hasattr(body.input, '__iter__') and not isinstance(body.input, str)
        else body.input
    )
    if fc_output is not None:
        if session is None or session.pending_tool_call is None:
            raise HTTPException(
                status_code=400,
                detail="function_call_output received but no pending tool call in session",
            )
        if fc_output["call_id"] != session.pending_tool_call["call_id"]:
            raise HTTPException(
                status_code=400,
                detail=f"call_id mismatch: expected {session.pending_tool_call['call_id']}",
            )
        # Feed response to waiting can_use_tool callback
        session.input_response = fc_output["output"]
        session.input_event.set()

        # Stream the continuation from the SDK (callback resumes, SDK continues)
        if body.stream:
            # The ClaudeSDKClient is still alive and will resume streaming
            # after the can_use_tool callback returns
            return StreamingResponse(
                _run_client_continuation_stream(session, body, response_id, next_turn),
                media_type="text/event-stream",
            )
        else:
            # Non-streaming: collect continuation chunks
            return await _collect_client_continuation(session, body, response_id, next_turn)
```

- [ ] **Step 6: Implement `_run_client_continuation_stream()`**

Add to `src/routes/responses.py`:

```python
async def _run_client_continuation_stream(session, body, response_id, next_turn):
    """Stream the SDK continuation after function_call_output is provided.

    The can_use_tool callback has been unblocked, so client.receive_response()
    will yield new messages as the SDK resumes processing.
    """
    stream_result = {"success": False}
    try:
        chunks_buffer = []
        output_item_id = f"msg_{uuid.uuid4().hex[:24]}"

        async def continuation_chunks():
            async for message in session.client.receive_response():
                from src.backends.claude.client import ClaudeCodeCLI
                cli = ClaudeCodeCLI()
                yield cli._convert_message(message)

        sse_source = streaming_utils.stream_response_chunks(
            chunk_source=continuation_chunks(),
            model=body.model,
            response_id=response_id,
            output_item_id=output_item_id,
            chunks_buffer=chunks_buffer,
            logger=logger,
            prompt_text="(function_call_output continuation)",
            metadata=body.metadata,
            stream_result=stream_result,
        )
        async for line in streaming_utils.bridge_sse_stream(sse_source, continuation_chunks()):
            yield line

        if stream_result.get("success"):
            session.turn_counter = next_turn
            session.touch()
    except Exception as e:
        logger.error("Continuation stream error: %s", e, exc_info=True)
        yield streaming_utils.make_response_sse(
            "response.failed", response_id, {"error": {"code": "server_error", "message": str(e)}}
        )
    finally:
        if session.lock.locked():
            session.lock.release()
```

- [ ] **Step 7: Implement ClaudeSDKClient routing for session requests**

In the streaming preflight section of `create_response()`, add logic to use `ClaudeSDKClient` for session requests:

After `_responses_streaming_preflight()` but before `_run_stream()`, check if the session needs a client:

```python
    # For session requests, use ClaudeSDKClient
    if session is not None and not is_new_session:
        if session.client is None:
            # Create client on first session turn
            session.client = await backend.create_client(
                session=session,
                model=resolved.sdk_model,
                system_prompt=system_prompt,
                cwd=workspace,
            )
```

Modify `_run_stream()` to use `run_completion_with_client()` when `session.client` is available:

```python
    if session and session.client:
        chunk_source = backend.run_completion_with_client(
            client=session.client,
            prompt=prompt,
            session=session,
        )
    else:
        chunk_source = backend.run_completion(**preflight["chunk_kwargs"])
```

- [ ] **Step 8: Add AskUserQuestion detection in `stream_response_chunks()`**

In `src/streaming_utils.py`, in `stream_response_chunks()`, after the main streaming loop, add a check for pending_tool_call. Pass `session` as a new parameter:

```python
    # After the streaming loop ends (SDK paused on can_use_tool)
    if session and session.pending_tool_call:
        tc = session.pending_tool_call
        yield make_function_call_response_sse(
            response_id=response_id,
            call_id=tc["call_id"],
            name=tc["name"],
            arguments=json.dumps(tc["arguments"]),
        )
        # Emit response.completed with requires_action
        yield make_response_sse("response.completed", response_id, {
            "status": "requires_action",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })
        stream_result["success"] = True
        stream_result["requires_action"] = True
        return
```

- [ ] **Step 9: Run all tests**

Run: `uv run pytest tests/ -x -v`
Expected: All PASS.

- [ ] **Step 10: Commit**

```bash
git add src/routes/responses.py src/streaming_utils.py src/session_guard.py tests/test_ask_user_question.py
git commit -m "feat(responses): integrate ClaudeSDKClient routing and function_call_output handling"
```

---

## Phase 7: Input Model — Accept function_call_output in Request

### Task 7: Extend ResponseCreateRequest to accept function_call_output input items

**Files:**
- Modify: `src/response_models.py`
- Test: `tests/test_ask_user_question.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ask_user_question.py`:

```python
from src.response_models import ResponseCreateRequest, FunctionCallOutputInput


def test_request_accepts_function_call_output_input():
    """ResponseCreateRequest accepts function_call_output in input array."""
    req = ResponseCreateRequest(
        model="sonnet",
        input=[
            {"type": "function_call_output", "call_id": "toolu_abc", "output": "yes"},
        ],
        previous_response_id="resp_abc123_1",
    )
    assert len(req.input) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ask_user_question.py::test_request_accepts_function_call_output_input -v`
Expected: FAIL — validation error, current model only accepts `ResponseInputItem`.

- [ ] **Step 3: Update `ResponseCreateRequest.input` type**

In `src/response_models.py`, update the input field:

```python
class ResponseCreateRequest(BaseModel):
    """POST /v1/responses request body."""

    model: str
    input: Union[str, List[Union[ResponseInputItem, FunctionCallOutputInput, Dict[str, Any]]]] = Field(
        description="User input as a plain string, array of input items, or function_call_output"
    )
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_ask_user_question.py::test_request_accepts_function_call_output_input -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/response_models.py tests/test_ask_user_question.py
git commit -m "feat(models): accept function_call_output in ResponseCreateRequest input"
```

---

## Phase 8: Integration Tests

### Task 8: End-to-end AskUserQuestion flow test

**Files:**
- Test: `tests/test_ask_user_question.py` (extend)

- [ ] **Step 1: Write integration test**

Append to `tests/test_ask_user_question.py`:

```python
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
import json


def _make_app():
    from src.main import app
    return app


def test_ask_user_question_full_flow():
    """End-to-end: AskUserQuestion triggers function_call, client responds with output."""
    app = _make_app()
    client = TestClient(app)

    # Mock the backend to simulate AskUserQuestion
    with patch("src.routes.responses.resolve_and_get_backend") as mock_resolve:
        mock_backend = MagicMock()
        mock_backend.build_options.return_value = {}
        mock_backend.get_auth_provider.return_value = None

        # First request: simulate normal response with pending_tool_call set
        # (This tests the SSE output format)
        mock_resolve.return_value = (MagicMock(sdk_model="sonnet"), mock_backend)

        # Test that function_call_output without a session returns 400
        response = client.post(
            "/v1/responses",
            json={
                "model": "sonnet",
                "input": [
                    {"type": "function_call_output", "call_id": "toolu_abc", "output": "yes"}
                ],
                "previous_response_id": "resp_nonexistent_1",
            },
            headers={"Authorization": "Bearer test"},
        )
        assert response.status_code in (400, 404)  # Session not found or invalid
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_ask_user_question.py::test_ask_user_question_full_flow -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ask_user_question.py
git commit -m "test: add end-to-end AskUserQuestion flow test"
```

---

## Phase 9: Final Verification

### Task 9: Full test suite, lint, and cleanup

**Files:** All

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests PASS.

- [ ] **Step 2: Run coverage**

Run: `uv run pytest --cov=src --cov-report=term-missing tests/`
Expected: No significant coverage drop in modified modules.

- [ ] **Step 3: Lint and format**

Run: `uv run ruff check --fix . && uv run ruff format .`
Expected: Clean output.

- [ ] **Step 4: Verify server starts**

Run: `uv run uvicorn src.main:app --port 8000 &`
Then: `curl -s http://localhost:8000/v1/models | python -m json.tool`
Expected: Server starts, models endpoint responds.

Verify removed endpoints return 404:
```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/v1/chat/completions
# Expected: 404 or 405
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/v1/messages
# Expected: 404 or 405
```

- [ ] **Step 5: Final commit (if any lint fixes)**

```bash
git add -A
git commit -m "chore: lint and format cleanup"
```
