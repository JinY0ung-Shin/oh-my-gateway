# Session ID Merge — Design Spec

**Date**: 2026-04-27
**Branch / PR**: `feat/session-jsonl-rehydrate` / [#90](https://github.com/JinY0ung-Shin/claude-code-gateway/pull/90)
**Status**: Approved, ready for implementation plan

## Problem

Today the gateway uses **two distinct UUIDs** for what is conceptually one session:

- **`session.session_id`** — gateway's session ID. Generated at `routes/responses.py:296` via `str(uuid.uuid4())`. Embedded in OpenAI Responses API IDs as `resp_{session_id}_{turn}` and surfaced in logs (`session_manager.py:490`).
- **`client_session_id`** — a separate UUID generated inside `backends/claude/client.py:563` whenever a persistent `ClaudeSDKClient` is created. This becomes the SDK's CLI session ID (`--session-id` flag) and the on-disk transcript filename (`~/.claude/projects/<encoded-cwd>/<client_session_id>.jsonl`).

The two are not linked. Consequences:

1. **Rehydration is broken on the default path.** `session_manager._try_rehydrate_from_jsonl` looks for `<session_id>.jsonl` (gateway ID), but the persistent client path writes the file under `<client_session_id>.jsonl` (different UUID). After a server restart, persistent-client sessions cannot be rehydrated. Single-shot `query()` sessions happen to work because that path passes the gateway ID through to `options.session_id`.
2. **Operator confusion.** Logs show one ID; disk files use another. Admin UI exposes `provider_session_id` as a separate string.
3. **Resume after persistent-client crash also fails.** When `session.client` is lost mid-conversation, the gateway re-enters `create_client` with `resume=None` hardcoded — the SDK starts a fresh conversation rather than picking up the existing transcript.

## Goal

Collapse to **one identifier** per session: `session.session_id`. This single UUID is:

- the OpenAI Responses API conversation key (`resp_{session_id}_{turn}`)
- the SDK CLI session ID (`options.session_id` for new SDK sessions, `options.resume` for continuations)
- the on-disk transcript filename
- the only ID surfaced in logs and admin

## Non-Goals

- Keeping a separate "provider" identifier for future backends. If a future backend cannot accept caller-provided UUIDs, it can be reintroduced behind a backend-specific abstraction at that time.
- Migrating existing on-disk `.jsonl` files. Legacy files named with the old `client_session_id` are left in place and naturally age out; rehydration of pre-merge sessions is not supported.

## Design

### Single source of truth

`session.session_id` is the only session identifier. `Session.provider_session_id` is removed entirely. The OR-fallback in `session_guard.py:110` (`session.provider_session_id or session_id`) collapses since callers no longer compute a separate `resume_id`.

**Invariant**: if `session` exists, the on-disk transcript at `~/.claude/projects/<encoded(session.workspace)>/<session.session_id>.jsonl` is the SDK-side mirror of that session. Either the file does not yet exist (new session) or it does (resumable session).

### Persistent-client path (`backends/claude/client.py::create_client`)

Drop `client_session_id = str(uuid.uuid4())`. Decide between fresh-session and resume by checking the on-disk transcript:

```python
async def create_client(self, *, session, ..., cwd, ...):
    has_history = _session_jsonl_exists(session)
    options = self._build_sdk_options(
        ...,
        session_id=None if has_history else session.session_id,
        resume=session.session_id if has_history else None,
        cwd=Path(cwd) if cwd else None,
        ...,
    )
    ...
    return ClaudeSDKClient(options=options)
```

`_build_sdk_options` keeps its existing `session_id` / `resume` parameters but inlines the 4-line `_configure_session` body (the standalone helper is removed — see Claude backend cleanup below):

```python
if resume:
    options.resume = resume
elif session_id:
    options.session_id = session_id
```

Helper extracted into `session_manager.py` next to existing `_encode_cwd`/`_try_rehydrate_from_jsonl`:

```python
def _session_jsonl_path(session_id: str, workspace: str) -> Path:
    return _PROJECTS_ROOT / _encode_cwd(workspace) / f"{session_id}.jsonl"

def _session_jsonl_exists(session) -> bool:
    if not session.workspace:
        return False
    return _session_jsonl_path(session.session_id, session.workspace).is_file()
```

`_try_rehydrate_from_jsonl` is refactored to use `_session_jsonl_path` for path construction (eliminating duplicated path logic). It no longer assigns `provider_session_id` (field is gone).

The disk-existence signal is used in preference to `session.turn_counter > 0` because it correctly handles the edge case where turn 1 fails mid-stream (SDK has already created the `.jsonl` file but `turn_counter` was not yet incremented). On retry, the SDK appends to the existing transcript.

### `run_completion` fallback removal

The `else` branches in `routes/responses.py` that fall back to `backend.run_completion(...)` are removed:

- **Streaming (lines 438–470)**: drop `use_sdk_client` boolean, drop `else` branch. Always:

  ```python
  chunk_source = backend.run_completion_with_client(session.client, prompt, session)
  ```

- **Non-streaming (lines 603–624)**: same. Drop the `if/else`.
- **Persistent-client creation (lines 403–434)**: replace the silent-fallback `try/except` with fail-fast:

  ```python
  try:
      session.client = await backend.create_client(...)
  except Exception:
      logger.error("create_client failed", exc_info=True)
      await session_manager.delete_session_async(session_id)
      raise HTTPException(
          status_code=503,
          detail="Claude Code SDK unavailable; retry shortly",
      )
  ```

  The session is cleaned up so the next request starts fresh.

- **`hasattr(backend, "create_client")` guard (line 403)**: removed. Claude is the only registered backend; `codex/` is an empty placeholder. New backends are required to implement `create_client` and `run_completion_with_client` per the updated `BackendClient` Protocol.

`_responses_streaming_preflight` no longer returns `chunk_kwargs` (its only consumer was the removed fallback).

### Backend interface (`backends/base.py`)

`BackendClient` Protocol updated:

- **Remove**: `run_completion(...)` from required methods.
- **Add**: `create_client(...)` and `run_completion_with_client(...)` as required methods.

This formalizes the contract that every registered backend must support persistent-client interactive sessions.

### Claude backend cleanup (`backends/claude/client.py`)

- **Remove**: `run_completion` method (lines 675–739) and the `_configure_session` helper (lines 194–209). `create_client` builds session/resume options inline using the disk-existence check.

### Session guard cleanup (`session_guard.py`)

- **Remove**: `SessionPreflight.resume_id` field.
- **Remove**: lines 107–110 that compute `resume_id`. No remaining caller consumes the value.

### Admin / UI

- **Remove**: `"provider_session_id": session.provider_session_id` from `admin_service.py:604, 633`.
- **Remove**: the `provider_session_id` display span in `admin_html_sessions.py:59`.

### Tests

Bundled in the same PR:

- `tests/test_claude_cli_unit.py`: delete `run_completion` test cases (approximately lines 433–680).
- `tests/test_main_coverage_unit.py`: rewrite mocks at lines 110–115, 187, 372 to target `run_completion_with_client` / `create_client`.
- `tests/test_tool_execution.py`: delete the `run_completion` signature test (lines 143–153).
- `tests/test_session_guard_unit.py:86`: drop `provider_session_id="sdk-123"` from fixture; assert `resume_id` is no longer on `SessionPreflight`.
- `tests/test_session_manager_rehydrate.py:69`: drop the `provider_session_id` assertion.
- `tests/test_sdk_client_session.py:54`: drop `provider_session_id=` argument from session reconstruction.

New tests to add:

- `create_client` with no on-disk transcript → SDK options carry `session_id=session.session_id`, `resume=None`.
- `create_client` with an existing on-disk transcript → SDK options carry `session_id=None`, `resume=session.session_id`.
- `routes/responses.py`: `create_client` raising propagates as HTTP 503 and the session is deleted.
- End-to-end: rehydrate-on-miss → new persistent client created on the rehydrated session → `--resume` is sent (verified via SDK transport mock).

### Migration / rollout

Cold cutover. Pre-merge persistent-client sessions on disk are named with legacy random UUIDs and cannot be rehydrated by post-merge code. This matches the current de-facto behaviour: rehydrate already fails for these sessions. In-memory persistent-client sessions continue to function until the gateway is restarted (server restart already breaks them today).

No migration script. Legacy `.jsonl` files age out via the existing TTL/cleanup paths or remain harmlessly on disk.

## Verification

- `pytest tests/` passes after the test rewrite above.
- Manual: drive a session through rehydrate-on-miss → confirm `.jsonl` filename matches the gateway `session_id` and `--resume` is sent on the next turn.
- Manual: simulate `create_client` failure (e.g. revoked auth) → confirm HTTP 503 and session cleanup.

## Out of Scope / Follow-ups

- Pruning legacy `.jsonl` files named with non-gateway UUIDs (separate cleanup task).
- Removing the `codex/` placeholder backend directory.
- Renaming or splitting `run_completion_with_client` for ergonomics.
