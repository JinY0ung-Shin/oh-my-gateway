# Codex Backend Coverage 90%+ Design

Date: 2026-05-07
Status: Draft

## Goal

Raise test coverage of the Codex backend to **90%+** for both files:

- `src/backends/codex/__init__.py`: 71% → 90%+
- `src/backends/codex/client.py`: 83% → 90%+

These two files are the largest absolute coverage gaps in the project (119 + 10 missed lines respectively). Other backends are out of scope.

## Non-Goals

- Coverage for `src/backends/claude/*`, `src/backends/opencode/*`, `src/routes/*`, or any non-codex module.
- Refactoring production code. Tests target current behavior as-is.
- End-to-end / integration coverage requiring real Codex CLI.
- Reaching 100% — defensive branches with low value remain uncovered.

## Approach

Add unit tests to the existing `tests/test_codex_backend.py` only. Reuse the
existing `FakeRpc` class and `monkeypatch` patterns already established in the
file. No new fixtures, helpers, or test files.

All tests are isolated unit tests:

- Pure helpers verified by direct input → output assertions.
- Async methods verified by injecting `FakeRpc` via
  `monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", ...)`.
- `CodexJsonRpcClient` I/O paths verified by mocking `subprocess.Popen` /
  `_proc` attributes — no actual process spawned.

## Test Groups

### Group 1: `__init__.py` lazy import and register failure (~5 tests)

Covers lines 38–46 (`__getattr__`), 52, 62–63 (register failure logger).

- `__getattr__("CodexClient")` returns the class.
- `__getattr__("CodexAuthProvider")` returns the class.
- `__getattr__("Unknown")` raises `AttributeError`.
- `register()` registers descriptor and live client when import succeeds.
- `register()` registers descriptor only and logs error when client init
  raises (use a fake registry to capture calls).

### Group 2: Pure helper input/output (~10 tests)

Covers branches in helper methods of `CodexClient`. No I/O, no async.

- `_normalize_approval_decision`: aliases (`yes`, `no`, `always`, `stop`,
  empty string), list input pulls first element, unknown value falls back to
  `"decline"`.
- `_approval_kind`: unknown method returns `"approval"`.
- `_approval_question`: empty command falls back to generic phrase;
  `file_change`, `permissions`, and `approval` kinds.
- `_approval_decision_label`: empty dict returns `""`,
  `acceptWithExecpolicyAmendment` dict, `applyNetworkPolicyAmendment` dict
  with and without `network_policy_amendment.action`/`host`.
- `_approval_decision_from_available_options`: returns matching dict
  decision; returns `None` when nothing matches or `availableDecisions`
  missing/non-list.
- `_tool_use_from_item`: non-dict input, unknown type, missing/non-string id
  all return `None`; valid item omits `id`/`type`/`aggregatedOutput` from
  input dict.
- `_tool_result_from_item`: declined / failed status sets `is_error`;
  `commandExecution` with non-zero `exitCode` is error; missing
  `aggregatedOutput` falls back to JSON dump; non-command type uses JSON dump.
- `_extract_usage`: non-dict, missing `last` returns `None`.
- `_final_response_from_items`: phase=None fallback when no `final_answer`,
  ignores non-`agentMessage` items, returns `None` on empty.
- `_turn_error_message`: dict error with message; missing → default string.
- `_combine_system_prompt`: both / one / neither.
- `_thread_params` / `_turn_params`: with and without model/cwd/system_prompt.
- `_public_error_message`: strips `stderr_tail=...` for `CodexAppServerError`;
  returns generic fallback for empty message; passes through other exceptions.
- `parse_message`: success result wins; falls back to assistant block text;
  returns `None` on empty.
- `estimate_token_usage`: length-based approximation, ignores model.
- `_metadata_env`: `None` returns `{}`; filters by `METADATA_ENV_ALLOWLIST`.

### Group 3: `CodexJsonRpcClient` I/O error branches (~6 tests)

Covers lines in `_read_message`, `_write_message`, `close`, `thread_resume`,
`turn_start`, `model_list`, `thread_start` non-dict response branches. Use a
minimal fake `_proc`/`_stdout_queue` rather than spawning subprocess.

- `_read_message` raises `CodexAppServerError` on JSON decode failure.
- `_read_message` raises `CodexAppServerError` when payload is not a dict.
- `_read_message` raises `CodexAppServerError` when stdout closed (queue
  yields `None`).
- `_read_message` raises `CodexAppServerError` when `_proc` is `None`.
- `_write_message` raises `CodexAppServerError` when `_proc`/stdin missing.
- `close()` is a no-op when `_proc` is `None` (early return at line 107–108).
- `thread_resume` / `turn_start` / `model_list` / `thread_start` raise
  `CodexAppServerError` when result is not a dict.

### Group 4: `CodexClient` async error branches (~5 tests)

Uses `FakeRpc` already defined in the test file (extend its behavior locally
where needed).

- `verify()` returns `False` when `model_list` raises.
- `verify()` returns `False` when result `data` is not a list.
- `runtime_metadata()` shape: keys present, `shared_process` False initially.
- `name == "codex"`, `supported_models()` returns list copy.
- `run_completion_with_client` yields error chunk when `turn/start` response
  has no `turn.id`.
- `resume_approval_with_client` yields error chunk when
  `pending_approval_request_id` is `None`.
- `resume_approval_with_client` yields error chunk when `turn_id` is missing.

## Architecture / Components

No production code changes. All work is additive in `tests/test_codex_backend.py`.

```
tests/test_codex_backend.py
├── existing tests (1067 lines, untouched)
└── new tests (~250–350 lines added)
    ├── __init__ lazy import / register
    ├── pure helpers
    ├── CodexJsonRpcClient I/O errors
    └── CodexClient async error paths
```

## Data Flow

All tests bypass real I/O:

- Pure helpers: instantiate `CodexClient()`, call method directly.
- `__init__` register: pass a stub `registry_cls` with `register_descriptor`
  / `register` methods; assert calls.
- `CodexJsonRpcClient` I/O: instantiate, then patch `_proc`/`_stdout_queue`
  attributes to simulate states.
- `CodexClient` async: monkeypatch `CodexJsonRpcClient` to return `FakeRpc`,
  drive `await create_client(...)` and async generators with
  `[c async for c in ...]`.

## Error Handling

- `pytest.raises(CodexAppServerError)` for sync exception cases.
- Async error chunks asserted by collecting the generator and checking
  `chunk["type"] == "error"` with expected `error_message` substring.

## Testing

- `pytest tests/test_codex_backend.py -v` must pass.
- `pytest --cov=src/backends/codex --cov-report=term-missing` must show:
  - `src/backends/codex/__init__.py` ≥ 90%
  - `src/backends/codex/client.py` ≥ 90%
- `pytest --cov=src --cov-report=term` must not regress overall coverage
  (currently 92%).
- All existing tests continue to pass.

## Risks

- Some uncovered lines (e.g., multi-thread join in `close()` after timeout)
  are hard to cover without flaky timing. If these put us below 90%, add a
  targeted test using a stubbed `_proc` whose `wait()` raises
  `subprocess.TimeoutExpired` once.
- `_metadata_env` imports `METADATA_ENV_ALLOWLIST` at call time. Test must
  use a value that is actually in the allowlist (read it from `src.constants`).

## Out-of-Scope but Worth Noting

The following codex client lines may remain uncovered after this work and
that is acceptable:

- 76 (`start()` early return when proc already running) — single line guard.
- 247–249 (timeout error path with stderr tail) — already covered by
  existing `test_codex_json_rpc_client_times_out_waiting_for_message`.
- A few defensive `assert self._rpc is not None` paths.

Acceptance bar is 90%+, not 100%.
