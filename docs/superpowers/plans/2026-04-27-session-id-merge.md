# Session ID Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the gateway's `session.session_id` and the SDK's per-client UUID into one identifier so the OpenAI response ID, the SDK CLI session, the on-disk `.jsonl` filename, and operator-visible logs all agree.

**Architecture:** Make the persistent-client path (`backends/claude/client.py::create_client`) reuse `session.session_id` instead of generating a fresh UUID. Decide between starting a new SDK session and resuming by checking whether the on-disk `.jsonl` already exists. Remove the `run_completion` fallback so persistent-client failures fail fast as HTTP 503. Drop `Session.provider_session_id` and `SessionPreflight.resume_id` since both become redundant.

**Tech Stack:** Python 3.13, FastAPI, `claude-agent-sdk`, pytest.

**Spec:** `docs/superpowers/specs/2026-04-27-session-id-merge-design.md`

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/session_manager.py` | Modify | Add `_session_jsonl_path`, `_session_jsonl_exists`; refactor `_try_rehydrate_from_jsonl`; drop `Session.provider_session_id` |
| `src/session_guard.py` | Modify | Drop `SessionPreflight.resume_id` field and its computation |
| `src/backends/base.py` | Modify | Replace `run_completion` in `BackendClient` Protocol with `create_client` + `run_completion_with_client` |
| `src/backends/claude/client.py` | Modify | Drop `client_session_id = uuid4()`; disk-check in `create_client`; inline `_configure_session`; delete `_configure_session` and `run_completion` |
| `src/routes/responses.py` | Modify | Drop `run_completion` fallback in both streaming and non-streaming paths; 503 fail-fast on `create_client` failure; slim `_responses_streaming_preflight` |
| `src/admin_service.py` | Modify | Remove `provider_session_id` from session detail responses |
| `src/admin_html_sessions.py` | Modify | Remove `provider_session_id` UI display |
| `tests/test_session_manager_rehydrate.py` | Modify | Drop `provider_session_id` assertion; add helper tests |
| `tests/test_session_guard_unit.py` | Modify | Drop `provider_session_id`/`resume_id` references |
| `tests/test_sdk_client_session.py` | Modify | Drop `provider_session_id=` in fixtures |
| `tests/test_claude_cli_unit.py` | Modify | Delete `run_completion` test class; add `create_client` disk-check tests |
| `tests/test_main_coverage_unit.py` | Modify | Replace `run_completion` mocks with `run_completion_with_client` / `create_client` mocks |
| `tests/test_tool_execution.py` | Modify | Delete `run_completion` signature test |
| `tests/test_session_id_merge_integration.py` | Create | E2E: rehydrate → new client → `--resume` sent |

---

## Task 1: Add `_session_jsonl_path` and `_session_jsonl_exists` helpers

**Files:**
- Modify: `src/session_manager.py`
- Test: `tests/test_session_manager_rehydrate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_manager_rehydrate.py`:

```python
def test_session_jsonl_path_constructs_expected_path(tmp_path, monkeypatch):
    from src import session_manager

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    p = session_manager._session_jsonl_path("abc", "/x/y_z")
    encoded = session_manager._encode_cwd("/x/y_z")
    assert p == tmp_path / encoded / "abc.jsonl"


def test_session_jsonl_exists_returns_false_when_workspace_missing(monkeypatch):
    from src import session_manager
    from src.session_manager import Session

    sess = Session(session_id="sid", workspace=None)
    assert session_manager._session_jsonl_exists(sess) is False


def test_session_jsonl_exists_reflects_filesystem(tmp_path, monkeypatch):
    from src import session_manager
    from src.session_manager import Session

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
    cwd = "/x/y"
    encoded = session_manager._encode_cwd(cwd)
    sid = "sid-1"

    sess = Session(session_id=sid, workspace=cwd)
    assert session_manager._session_jsonl_exists(sess) is False

    target = tmp_path / encoded / f"{sid}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n")
    assert session_manager._session_jsonl_exists(sess) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_session_manager_rehydrate.py -v -k "session_jsonl"`
Expected: 3 failures with `AttributeError: module 'src.session_manager' has no attribute '_session_jsonl_path'` (or `_session_jsonl_exists`).

- [ ] **Step 3: Add the helpers**

In `src/session_manager.py`, after `_encode_cwd` (around line 49), insert:

```python
def _session_jsonl_path(session_id: str, workspace) -> Path:
    """Return the on-disk path the SDK uses for this session's transcript.

    Path layout: ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``.
    """
    return _PROJECTS_ROOT / _encode_cwd(workspace) / f"{session_id}.jsonl"


def _session_jsonl_exists(session: "Session") -> bool:
    """True when the SDK has already written a transcript for *session*."""
    if not session.workspace:
        return False
    return _session_jsonl_path(session.session_id, session.workspace).is_file()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_session_manager_rehydrate.py -v -k "session_jsonl"`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/session_manager.py tests/test_session_manager_rehydrate.py
git commit -m "$(cat <<'EOF'
feat(session): add jsonl path/exists helpers

Centralize the SDK transcript path computation so create_client
and _try_rehydrate_from_jsonl share one source of truth.
EOF
)"
```

---

## Task 2: Refactor `_try_rehydrate_from_jsonl` to use the new helper

**Files:**
- Modify: `src/session_manager.py:51-86`
- Test: `tests/test_session_manager_rehydrate.py` (existing tests act as regression)

- [ ] **Step 1: Confirm existing rehydrate tests pass before changes**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_session_manager_rehydrate.py -v`
Expected: all PASS.

- [ ] **Step 2: Refactor to use `_session_jsonl_path`**

Replace the body of `_try_rehydrate_from_jsonl` in `src/session_manager.py`:

```python
def _try_rehydrate_from_jsonl(
    session_id: str, *, user: Optional[str], cwd
) -> Optional["Session"]:
    """Reconstruct a Session from the Claude SDK on-disk jsonl, if present.

    Returns None when the jsonl file is missing, unreadable, or malformed
    enough that we can't establish a turn count. The caller treats None as
    cache-miss-and-on-disk-miss → existing 404 path.
    """
    if not user or not cwd:
        return None
    try:
        jsonl_path = _session_jsonl_path(session_id, cwd)
        if not jsonl_path.is_file():
            return None
        user_msg_count = 0
        with jsonl_path.open("r") as fh:
            for raw in fh:
                try:
                    line = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    return None  # corrupt — refuse to guess
                if line.get("type") == "user":
                    user_msg_count += 1
        return Session(
            session_id=session_id,
            backend="claude",
            provider_session_id=session_id,
            messages=[],
            turn_counter=user_msg_count,
            workspace=str(cwd),
            user=user,
        )
    except OSError:
        return None
```

(Note: `provider_session_id=session_id` is still present here — Task 9 removes it once the field is dropped.)

- [ ] **Step 3: Run rehydrate tests**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_session_manager_rehydrate.py -v`
Expected: all PASS (no behavior change).

- [ ] **Step 4: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/session_manager.py
git commit -m "$(cat <<'EOF'
refactor(session): _try_rehydrate_from_jsonl uses _session_jsonl_path

Pure refactor. Removes duplicated path construction now that
the helper exists.
EOF
)"
```

---

## Task 3: Use `session.session_id` and disk check in `create_client`

**Files:**
- Modify: `src/backends/claude/client.py:538-592`
- Test: `tests/test_claude_cli_unit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_cli_unit.py` (in a new test class):

```python
class TestCreateClientSessionId:
    """create_client uses session.session_id; disk presence chooses session_id vs resume."""

    @pytest.fixture
    def gateway_session(self):
        from src.session_manager import Session

        return Session(
            session_id="gw-uuid-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            workspace="/tmp/ws",
        )

    @pytest.mark.asyncio
    async def test_no_jsonl_passes_session_id(self, monkeypatch, tmp_path, gateway_session):
        """When no transcript exists, create_client passes session_id (not resume)."""
        from src import session_manager
        from src.backends.claude import client as claude_client_mod

        monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)

        captured: dict = {}

        class FakeSDKClient:
            def __init__(self, *, options):
                captured["session_id"] = options.session_id
                captured["resume"] = options.resume

            async def connect(self, prompt=None):
                return None

        monkeypatch.setattr(claude_client_mod, "ClaudeSDKClient", FakeSDKClient)

        cli = claude_client_mod.ClaudeCodeCLI()
        await cli.create_client(
            session=gateway_session,
            cwd="/tmp/ws",
        )

        assert captured["session_id"] == gateway_session.session_id
        assert captured["resume"] is None

    @pytest.mark.asyncio
    async def test_existing_jsonl_passes_resume(self, monkeypatch, tmp_path, gateway_session):
        """When a transcript exists, create_client passes resume (not session_id)."""
        from src import session_manager
        from src.backends.claude import client as claude_client_mod

        monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)

        # Pre-create the .jsonl file
        target = session_manager._session_jsonl_path(
            gateway_session.session_id, gateway_session.workspace
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n")

        captured: dict = {}

        class FakeSDKClient:
            def __init__(self, *, options):
                captured["session_id"] = options.session_id
                captured["resume"] = options.resume

            async def connect(self, prompt=None):
                return None

        monkeypatch.setattr(claude_client_mod, "ClaudeSDKClient", FakeSDKClient)

        cli = claude_client_mod.ClaudeCodeCLI()
        await cli.create_client(
            session=gateway_session,
            cwd="/tmp/ws",
        )

        assert captured["session_id"] is None
        assert captured["resume"] == gateway_session.session_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_claude_cli_unit.py::TestCreateClientSessionId -v`
Expected: 2 FAIL — `captured["session_id"]` shows a fresh UUID (not `gateway_session.session_id`).

- [ ] **Step 3: Update `create_client`**

In `src/backends/claude/client.py`, replace lines 561-578:

```python
        from src.session_manager import _session_jsonl_exists

        has_history = _session_jsonl_exists(session)
        options = self._build_sdk_options(
            model=model,
            system_prompt=system_prompt,
            max_turns=get_default_max_turns(),
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            session_id=None if has_history else session.session_id,
            resume=session.session_id if has_history else None,
            permission_mode=permission_mode,
            mcp_servers=mcp_servers,
            task_budget=task_budget,
            cwd=Path(cwd) if cwd else None,
            extra_env=extra_env,
            _custom_base=_custom_base,
        )
```

Also delete the import of `uuid` at the top of the file if no other use remains. Check with: `grep -n "uuid" src/backends/claude/client.py`. If only the now-deleted line referenced `uuid`, remove `import uuid`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_claude_cli_unit.py::TestCreateClientSessionId -v`
Expected: 2 PASS.

- [ ] **Step 5: Run the broader test file to catch regressions**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_claude_cli_unit.py -v`
Expected: previously-passing tests still PASS (the 2 new ones now also PASS). Some `run_completion` tests may still pass; they will be removed in Task 6.

- [ ] **Step 6: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/backends/claude/client.py tests/test_claude_cli_unit.py
git commit -m "$(cat <<'EOF'
feat(claude): create_client uses session.session_id

The persistent ClaudeSDKClient now reuses the gateway's
session_id as the SDK CLI session id (and therefore the
on-disk .jsonl filename). Disk presence selects between
options.session_id (new) and options.resume (continue).

This also fixes resume after rehydrate: a session restored
from disk on cache miss now spawns the SDK with --resume
instead of starting a fresh conversation.
EOF
)"
```

---

## Task 4: Remove `run_completion` fallback in `routes/responses.py`

**Files:**
- Modify: `src/routes/responses.py:403-470, 600-624`
- Test: `tests/test_main_coverage_unit.py`, new test for 503 path

- [ ] **Step 1: Write the failing test for 503 fail-fast**

Append to `tests/test_main_coverage_unit.py`:

```python
@pytest.mark.asyncio
async def test_create_client_failure_returns_503_and_deletes_session(monkeypatch):
    """When backend.create_client raises, the route returns 503 and clears the session."""
    from fastapi.testclient import TestClient
    from src import main, session_manager as sm_mod
    from src.backends.base import BackendRegistry

    class FailingBackend:
        name = "claude"
        def supported_models(self): return ["claude-test"]
        def get_auth_provider(self): return None
        async def verify(self): return True
        def parse_message(self, msgs): return None
        def estimate_token_usage(self, *a, **k): return {}

        async def create_client(self, **kwargs):
            raise RuntimeError("simulated SDK boot failure")

        async def run_completion_with_client(self, client, prompt, session):
            yield {"type": "noop"}

    BackendRegistry.register("claude", FailingBackend())

    client = TestClient(main.app)
    resp = client.post(
        "/v1/responses",
        json={"model": "claude-test", "input": "hi", "user": "u1"},
    )
    assert resp.status_code == 503
    # Session must not be left orphaned
    assert sm_mod.session_manager.get_stats()["active_sessions"] == 0
```

If the existing tests in `test_main_coverage_unit.py` use `tests/conftest.py` fixtures to seed `BackendRegistry`, prefer that over the inline `BackendRegistry.register(...)` call shown above (the inline form leaks state between tests). Look for an existing `mock_backend` or `claude_backend` fixture and accept it as a parameter, then `monkeypatch.setattr` its `create_client` to raise `RuntimeError("simulated SDK boot failure")`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_main_coverage_unit.py::test_create_client_failure_returns_503_and_deletes_session -v`
Expected: FAIL — current code swallows the exception and falls back to `run_completion`, returning 200 (or a different error shape).

- [ ] **Step 3: Replace persistent-client creation block**

In `src/routes/responses.py`, replace lines 403-434:

```python
    # ------------------------------------------------------------------
    # Create the persistent ClaudeSDKClient.  We require it on every
    # turn (no run_completion fallback) so PreToolUse hooks for
    # AskUserQuestion fire reliably and the on-disk transcript matches
    # session.session_id one-to-one.
    # ------------------------------------------------------------------
    if session.client is None:
        from src.system_prompt import get_system_prompt, resolve_request_placeholders

        if session.base_system_prompt is not None:
            resolved_base = session.base_system_prompt
        else:
            resolved_base = resolve_request_placeholders(get_system_prompt(), workspace_str)
        try:
            session.client = await backend.create_client(
                session=session,
                model=resolved.provider_model,
                system_prompt=system_prompt if len(session.messages) == 0 else None,
                permission_mode=PERMISSION_MODE_BYPASS,
                allowed_tools=body.allowed_tools,
                mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
                cwd=workspace_str,
                extra_env=body.metadata,
                _custom_base=resolved_base,
            )
        except Exception:
            logger.error("create_client failed", exc_info=True)
            await session_manager.delete_session_async(session_id)
            raise HTTPException(
                status_code=503,
                detail="Claude Code SDK unavailable; retry shortly",
            )
```

- [ ] **Step 4: Replace streaming dispatch (former lines 436-470)**

Delete `use_sdk_client = ...` and the `if/else`. Replace the streaming chunk-source block with:

```python
        async def _run_stream():
            lock_acquired = preflight["lock_acquired"]
            stream_result: dict = {"success": False}
            active_client = session.client
            try:
                chunks_buffer = []

                chunk_source = backend.run_completion_with_client(
                    session.client, prompt, session
                )
                # ... rest of the existing _run_stream body unchanged ...
```

- [ ] **Step 5: Replace non-streaming dispatch (former lines 600-624)**

Find the block:

```python
            chunks = []
            if use_sdk_client:
                active_client = session.client
                async for chunk in backend.run_completion_with_client(
                    active_client, prompt, session
                ):
                    chunks.append(chunk)
            else:
                async for chunk in backend.run_completion(
                    prompt=prompt,
                    ...
                ):
                    chunks.append(chunk)
```

Replace with:

```python
            chunks = []
            active_client = session.client
            async for chunk in backend.run_completion_with_client(
                active_client, prompt, session
            ):
                chunks.append(chunk)
```

- [ ] **Step 6: Run the new 503 test and the rest of the responses test suite**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_main_coverage_unit.py -v`
Expected: new 503 test PASS. Other tests in this file may FAIL because they mock `run_completion`; those are repaired in Task 6.

- [ ] **Step 7: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/routes/responses.py tests/test_main_coverage_unit.py
git commit -m "$(cat <<'EOF'
feat(responses): drop run_completion fallback; 503 on SDK failure

When create_client fails we now delete the half-formed session
and return HTTP 503 instead of silently degrading to a
hookless query() path. Also collapses the if/else dispatch
in the streaming and non-streaming bodies — every turn flows
through the persistent client.
EOF
)"
```

---

## Task 5: Slim `_responses_streaming_preflight`

**Files:**
- Modify: `src/routes/responses.py:154-220`

- [ ] **Step 1: Drop `chunk_kwargs` from the returned dict**

In `_responses_streaming_preflight`, delete the `"chunk_kwargs": dict(...)` block (lines 206-219):

```python
    return {
        "session": pf.session,
        "lock_acquired": pf.lock_acquired,
        "next_turn": pf.next_turn,
        "resume_id": pf.resume_id,
    }
```

(`resume_id` is removed in Task 8; `chunk_kwargs` goes now because no caller consumes it post-Task 4.)

- [ ] **Step 2: Confirm no remaining caller reads `preflight["chunk_kwargs"]`**

Run: `cd ~/world/claude-code-gateway && grep -n 'chunk_kwargs' src/`
Expected: empty output.

- [ ] **Step 3: Run the responses tests**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_main_coverage_unit.py tests/test_main_helpers_unit.py -v`
Expected: tests still pass at the rate established by Task 4 (failing test count unchanged).

- [ ] **Step 4: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/routes/responses.py
git commit -m "refactor(responses): drop chunk_kwargs from preflight (now unused)"
```

---

## Task 6: Delete `run_completion` from Claude backend; inline `_configure_session`

**Files:**
- Modify: `src/backends/claude/client.py:194-210, 280-330, 675-740`
- Modify: `tests/test_claude_cli_unit.py`
- Modify: `tests/test_main_coverage_unit.py`
- Modify: `tests/test_tool_execution.py`

- [ ] **Step 1: Inline `_configure_session` into `_build_sdk_options`**

In `src/backends/claude/client.py`, find the call site inside `_build_sdk_options` (around line 316):

```python
        self._configure_session(options, session_id, resume)
```

Replace with:

```python
        if resume:
            options.resume = resume
        elif session_id:
            options.session_id = session_id
```

Then delete the standalone `_configure_session` method (lines 194-209 in the current file).

- [ ] **Step 2: Delete the `run_completion` method**

In `src/backends/claude/client.py`, delete the entire `async def run_completion(...)` method (currently lines 675-739).

- [ ] **Step 3: Delete `run_completion`-only tests in `test_claude_cli_unit.py`**

Open `tests/test_claude_cli_unit.py`. Delete the test class that wraps `run_completion` (the `class Test...RunCompletion` block, currently around lines 433-680). Keep tests that exercise `_build_sdk_options`, `_convert_message`, and other helpers.

- [ ] **Step 4: Update `test_main_coverage_unit.py` mocks**

For each occurrence of `mock_backend.run_completion = ...` (currently lines ~110-115, 187, 372), replace with the persistent-client pair. Concrete pattern:

```python
class _FakeClient:
    """Stand-in for a connected ClaudeSDKClient."""
    pass

async def fake_create_client(**kwargs):
    return _FakeClient()

async def fake_run_completion_with_client(client, prompt, session):
    # Yield the same chunks the old fake_run_completion produced.
    yield {"type": "assistant", "message": {"role": "assistant", "content": "ok"}}
    yield {"type": "result", "is_error": False}

mock_backend.create_client = fake_create_client
mock_backend.run_completion_with_client = fake_run_completion_with_client
```

Apply this shape at each of the three sites; vary the yielded chunks to match what the original `fake_run_completion`/`fake_run`/`failing_run` returned at that site (`failing_run` becomes a `create_client` that raises, since the failure-path test now exercises the 503 branch).

- [ ] **Step 5: Delete `test_tool_execution.py:143-153`**

Remove the test that introspects `run_completion`'s signature for `permission_mode`. The persistent-client path's permission handling is already covered by route-level tests.

- [ ] **Step 6: Run the full Claude-related test suite**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_claude_cli_unit.py tests/test_main_coverage_unit.py tests/test_tool_execution.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/backends/claude/client.py \
        tests/test_claude_cli_unit.py \
        tests/test_main_coverage_unit.py \
        tests/test_tool_execution.py
git commit -m "$(cat <<'EOF'
refactor(claude): drop run_completion, inline _configure_session

run_completion is no longer wired from any production path
(see prior commit) so we delete the method, its tests, and
the now-trivial _configure_session helper. Persistent-client
flows are the single SDK invocation path.
EOF
)"
```

---

## Task 7: Update `BackendClient` Protocol

**Files:**
- Modify: `src/backends/base.py:69-111`
- Test: `tests/test_backend_contract.py`

- [ ] **Step 1: Replace the Protocol definition**

In `src/backends/base.py`, replace the `BackendClient` Protocol body:

```python
class BackendClient(Protocol):
    """Interface that every backend must satisfy.

    Method names intentionally match ``ClaudeCodeCLI`` so the existing
    implementation is already structurally compatible.
    """

    @property
    def name(self) -> str: ...

    def supported_models(self) -> List[str]: ...

    def get_auth_provider(self) -> Any: ...

    async def create_client(
        self,
        *,
        session: Any,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        _custom_base: Any = None,
    ) -> Any: ...

    def run_completion_with_client(
        self,
        client: Any,
        prompt: str,
        session: Any,
    ) -> AsyncIterator[Dict[str, Any]]: ...

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]: ...

    def estimate_token_usage(
        self,
        prompt: str,
        completion: str,
        model: Optional[str] = None,
    ) -> Dict[str, int]: ...

    async def verify(self) -> bool: ...
```

Also update the docstring example at lines 122-130 to call `create_client` + `run_completion_with_client` instead of `run_completion`.

- [ ] **Step 2: Run backend contract tests**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_backend_contract.py tests/test_backend_registry_unit.py -v`
Expected: PASS. Update assertions in `test_backend_contract.py` if it checks for `run_completion` in the Protocol.

- [ ] **Step 3: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/backends/base.py tests/test_backend_contract.py
git commit -m "$(cat <<'EOF'
refactor(backends): require create_client + run_completion_with_client

Update BackendClient Protocol to formalize the persistent-client
contract. run_completion is no longer required (or implemented
by Claude).
EOF
)"
```

---

## Task 8: Drop `SessionPreflight.resume_id`

**Files:**
- Modify: `src/session_guard.py:21-30, 107-140`
- Modify: `src/routes/responses.py:200-205`
- Test: `tests/test_session_guard_unit.py`

- [ ] **Step 1: Update tests**

In `tests/test_session_guard_unit.py`, drop any assertion or fixture argument touching `resume_id` (currently around line 86, plus any `assert pf.resume_id == ...` lines). Keep the rest of the file's coverage intact.

- [ ] **Step 2: Run tests to verify they fail (or pass trivially)**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_session_guard_unit.py -v`
Expected: PASS (after the test edits — the production code still defines `resume_id`, so unchanged assertions about other fields keep working).

- [ ] **Step 3: Drop `resume_id` from `SessionPreflight`**

In `src/session_guard.py`, delete the field:

```python
@dataclass
class SessionPreflight:
    """Result of session validation and state mutation."""

    session: Session
    is_new: bool
    next_turn: int
    lock_acquired: bool
```

Delete the `resume_id` computation block (lines 107-110 in the current file):

```python
        # --- Compute resume_id ---
        resume_id: Optional[str] = None
        if not is_new:
            resume_id = session.provider_session_id or session_id
```

And drop `resume_id=resume_id` from the `return SessionPreflight(...)` constructor near the bottom.

- [ ] **Step 4: Drop `resume_id` from the preflight dict in `routes/responses.py`**

In `_responses_streaming_preflight`, remove `"resume_id": pf.resume_id,` from the returned dict.

- [ ] **Step 5: Confirm no other readers**

Run: `cd ~/world/claude-code-gateway && grep -rn 'resume_id' src/ tests/`
Expected: empty (or only matches inside comments/docstrings — clean those up too).

- [ ] **Step 6: Run the test suite**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/ -v -x`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/session_guard.py src/routes/responses.py tests/test_session_guard_unit.py
git commit -m "refactor(session-guard): drop resume_id; create_client owns resume"
```

---

## Task 9: Drop `Session.provider_session_id`

**Files:**
- Modify: `src/session_manager.py:79, 112`
- Modify: `src/admin_service.py:604, 633`
- Modify: `src/admin_html_sessions.py:59`
- Test: `tests/test_session_manager_rehydrate.py:69`
- Test: `tests/test_session_guard_unit.py:86`
- Test: `tests/test_sdk_client_session.py:54`

- [ ] **Step 1: Update tests first**

`tests/test_session_manager_rehydrate.py:69`: delete the line `assert sess.provider_session_id == sid`.

`tests/test_session_guard_unit.py:86`: drop `provider_session_id="sdk-123"` from `_make_session(...)` (and any earlier helper definition that lists the field as a parameter).

`tests/test_sdk_client_session.py:54`: drop `provider_session_id=s1.provider_session_id,` from the `Session(...)` constructor call.

- [ ] **Step 2: Run tests to confirm they still pass before the field is removed**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_session_manager_rehydrate.py tests/test_session_guard_unit.py tests/test_sdk_client_session.py -v`
Expected: PASS.

- [ ] **Step 3: Remove the field from `Session`**

In `src/session_manager.py:112`, delete the `provider_session_id: Optional[str] = None` line. In `_try_rehydrate_from_jsonl` (around line 79), drop the `provider_session_id=session_id,` argument from the `Session(...)` constructor.

- [ ] **Step 4: Remove from admin service**

In `src/admin_service.py`, delete `"provider_session_id": session.provider_session_id,` from the two locations (lines ~604 and ~633).

- [ ] **Step 5: Remove from admin HTML**

In `src/admin_html_sessions.py:59`, delete the `<span x-show="sessionDetail?.provider_session_id">...</span>` element.

- [ ] **Step 6: Confirm no remaining readers**

Run: `cd ~/world/claude-code-gateway && grep -rn 'provider_session_id' src/ tests/`
Expected: empty.

- [ ] **Step 7: Run the full suite**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/ -v -x`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd ~/world/claude-code-gateway
git add src/session_manager.py src/admin_service.py src/admin_html_sessions.py \
        tests/test_session_manager_rehydrate.py tests/test_session_guard_unit.py \
        tests/test_sdk_client_session.py
git commit -m "$(cat <<'EOF'
refactor(session): drop provider_session_id

session.session_id is now the single identifier across the
gateway, the SDK CLI session, and the on-disk transcript.
The separate provider field is redundant — its only assigner
(rehydrate) was setting it equal to session_id, and its only
reader (session_guard) used it as an OR-fallback that always
resolved to the same value.
EOF
)"
```

---

## Task 10: End-to-end integration test

**Files:**
- Create: `tests/test_session_id_merge_integration.py`

- [ ] **Step 1: Write the integration test**

```python
"""End-to-end: rehydrate from disk → new persistent client → SDK gets --resume."""

import json
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_rehydrated_session_resumes_sdk(tmp_path, monkeypatch):
    """A session loaded from on-disk jsonl spawns a ClaudeSDKClient with options.resume."""
    from src import session_manager
    from src.backends.claude import client as claude_client_mod

    monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)

    sid = "11111111-2222-3333-4444-555555555555"
    cwd = "/tmp/integration-ws"
    target = session_manager._session_jsonl_path(sid, cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "hello"}}) + "\n")

    sess = session_manager._try_rehydrate_from_jsonl(sid, user="u", cwd=cwd)
    assert sess is not None
    assert sess.session_id == sid
    assert sess.turn_counter == 1

    captured: dict = {}

    class FakeSDKClient:
        def __init__(self, *, options):
            captured["session_id"] = options.session_id
            captured["resume"] = options.resume

        async def connect(self, prompt=None):
            return None

    monkeypatch.setattr(claude_client_mod, "ClaudeSDKClient", FakeSDKClient)

    cli = claude_client_mod.ClaudeCodeCLI()
    await cli.create_client(session=sess, cwd=cwd)

    assert captured["resume"] == sid
    assert captured["session_id"] is None
```

- [ ] **Step 2: Run it**

Run: `cd ~/world/claude-code-gateway && uv run pytest tests/test_session_id_merge_integration.py -v`
Expected: PASS.

- [ ] **Step 3: Run the whole suite once more**

Run: `cd ~/world/claude-code-gateway && uv run pytest -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd ~/world/claude-code-gateway
git add tests/test_session_id_merge_integration.py
git commit -m "test(session): e2e rehydrate→resume integration"
```

---

## Task 11: Push and update the existing PR

**Files:** none (publish only)

- [ ] **Step 1: Push the branch**

```bash
cd ~/world/claude-code-gateway
git push origin feat/session-jsonl-rehydrate
```

- [ ] **Step 2: Update PR #90 description**

```bash
cd ~/world/claude-code-gateway
gh pr view 90 --json body --jq .body  # snapshot current body
```

Append a new section to the PR body:

```markdown
## Session ID Merge (added 2026-04-27)

Collapses the gateway `session.session_id` and the SDK's per-client UUID
into a single identifier. After this change `resp_<id>_<turn>`,
`~/.claude/projects/.../<id>.jsonl`, and `src.session_manager` logs all
share the same UUID. Spec: `docs/superpowers/specs/2026-04-27-session-id-merge-design.md`.

Side-effects:
- `run_completion` fallback removed; `create_client` failures surface as HTTP 503.
- `Session.provider_session_id` and `SessionPreflight.resume_id` deleted.
- Rehydrated sessions correctly resume their SDK transcript via `--resume`.
```

Run:

```bash
gh pr edit 90 --body "$(printf '%s\n\n%s' "$(gh pr view 90 --json body --jq .body)" "<paste section above>")"
```

(Or edit through the web UI.)

- [ ] **Step 3: Verify CI green**

```bash
gh pr checks 90 --watch
```

Expected: all checks PASS.

---

## Verification Summary

After completing all tasks:

```bash
cd ~/world/claude-code-gateway
uv run pytest -v
```

Expect: every test passes, with the new `TestCreateClientSessionId` class and `test_session_id_merge_integration.py` covering the merge behavior. Manual smoke: drive a fresh request through `/v1/responses`, observe the gateway log line `Added assistant response to session <ID>`, then confirm `~/.claude/projects/<encoded-cwd>/<ID>.jsonl` exists with that exact `<ID>`.
