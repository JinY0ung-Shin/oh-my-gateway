# Test Coverage Lift to 95% — Design

**Status:** Approved (pending implementation)
**Date:** 2026-04-30
**Author:** brainstorming session with Jinyoung

## Goal

Raise overall test coverage from **89%** (656 of 5,792 statements uncovered) to **≥ 95%** (≤ 290 uncovered) — about **365 additional covered lines**.

`pyproject.toml` already configures `pytest-cov` with `source = ["src"]` and excludes `pragma: no cover` and `if __name__`. The work is to add tests, not infrastructure.

## Strategy

- **Domain clusters, sequential.** Three stages, each ending with a `pytest --cov` re-measurement and a progress report. Move to the next stage only after the previous stage hits its sub-target.
- **Plan per stage, not all upfront.** Stage 1 already has a detailed plan checked in (`docs/superpowers/plans/2026-04-30-coverage-improvement.md`). Stages 2 and 3 get their own plans only after the prior stage verifies, so the residual targets are re-derived from a fresh coverage report rather than guessed in advance.
- **Reuse existing patterns.** Mock SDK and DB calls (`tests/conftest.py`, fake-engine pattern in `tests/test_usage_logger.py`, `TestClient` + `monkeypatch` in `tests/test_admin_routes_coverage.py`). Do not introduce new fixtures unless required.
- **No real DB.** `usage_logger` and `usage_queries` are exercised through `_FakeEngine` / `_FakeConnection` (already established). No MySQL container, no in-memory SQLite. Trade-off documented under Open Risks.
- **`# pragma: no cover` only for genuinely unreachable lines** (optional-import fallback, best-effort dispose). Do not use it to hide reachable code.
- **CI gate at the end.** Add `fail_under = 95` to `[tool.coverage.report]` in `pyproject.toml` once 95% is achieved, so regressions break CI.

## Stage 1 — Usage Stack (target ≥ 91%, ~+2.0 pp)

**Modules**
- `src/usage_queries.py` 14% → 90%+
- `src/usage_logger.py` 41% → 90%+

**Plan reference**

Stage 1 is implemented by the existing detailed plan at `docs/superpowers/plans/2026-04-30-coverage-improvement.md` (Tasks 1–4). That plan already specifies:
- Pure-helper coverage in `tests/test_usage_logger.py` (`_bind_positional_params`, `_normalize_db_url`, `_safe_url`, `extract_sdk_usage_detail`).
- Lifecycle and write-path coverage (`UsageLogger.start` env-unset / engine-init failure / probe failure paths, `close` idempotency, `log_turn` happy path and write-failure swallow, `log_turn_from_context` metadata gating and record building).
- Read-model coverage in `tests/test_usage_queries.py` (window clause, disabled-state for every query, summary merge, top-users / top-tools, time-series bucket clamping, tool-breakdown pivot, recent-turns user filter).
- Final verification step (Task 4) confirming TOTAL ≥ 91%.

Execute that plan via `superpowers:executing-plans` rather than re-deriving it. Admin usage-route HTTP coverage moves to Stage 2.

## Stage 2 — Admin Usage Routes + OpenCode Backend (target ≥ 93%, ~+2.0 pp)

**Modules**
- `src/routes/admin.py` usage endpoints — `usage_summary_endpoint`, `usage_users_endpoint`, `usage_tools_endpoint`, `usage_series_endpoint`, `usage_tools_series_endpoint`, `usage_turns_endpoint` (~30 statements)
- `src/backends/opencode/__init__.py` 71% → 95%+
- `src/backends/opencode/client.py` 81% → 95%+ (~60 statements)
- `src/backends/opencode/events.py` 88% → 95%+
- `src/backends/opencode/auth.py`, `config.py`, `constants.py` — residual lines

**Test work**

`tests/test_admin_usage_routes.py` (new)
- Uses `TestClient` with admin auth bypassed (existing pattern in `test_admin_routes_coverage.py`).
- For each of the six usage endpoints: assert response shape when `usage_logger.enabled` is `False` (`enabled: False`) and when patched with canned `usage_queries` return values (`enabled: True`, payload echoes rows).

OpenCode tests — extend `tests/test_opencode_backend.py` and add `tests/test_opencode_client_unit.py` only if the file grows past ~500 lines.
- Client error paths: HTTP 4xx and 5xx responses, request timeout, network error, malformed SSE chunk.
- Auth: missing token, expired-token branch.
- Events: unknown event type ignored, partial chunk handling, abort signal mid-stream.
- `__init__` registration: backend descriptor returned, missing config / missing env var branches.
- `config.py`: default values, override precedence.

A detailed Stage 2 implementation plan will be written via `superpowers:writing-plans` after Stage 1 verifies. Stage delta estimates may be refined at that point based on actual Stage 1 results.

## Stage 3 — Cleanup Cluster (target ≥ 95%, ~+2.0 pp)

**Modules** (small gaps across many files plus a few larger ones)
- `src/routes/admin.py` residual handlers (`get_logs`, file ops, skill ops, `update_runtime_config` validation paths) → 95%+
- `src/streaming_utils.py` 87% → 95%+
- `src/backends/claude/client.py` 87% → 95%+
- `src/routes/responses.py` 93% → 96%+
- Smaller residual lines in: `admin_service.py` (94%), `system_prompt.py` (94%), `main.py` (95%), `session_manager.py` (93%), `routes/deps.py` (84%), `tool_stats.py` (84%)

**Test work**
- `streaming_utils`: error/abort propagation, empty chunk passthrough, unknown message-type handling.
- `claude/client`: SDK exception mapping to API errors, empty response, abort mid-stream.
- `responses` route: `previous_response_id` rehydrate failure, unsupported model, malformed payload 4xx.
- `admin` residual: file-not-found paths, invalid payload 4xx, unauthorized branches.
- Small modules: targeted unit tests for each remaining 1–8 line gap.

A detailed Stage 3 implementation plan will be written via `superpowers:writing-plans` after Stage 2 verifies. The exact module list will be re-derived from a fresh `pytest --cov` report at that point, since Stage 1/2 changes may shift the residual.

## Verification

Each stage:
1. Run `pytest --cov=src --cov-report=term-missing -q`.
2. Confirm the targeted modules reach their per-stage coverage targets.
3. Confirm overall coverage rose by approximately the stage's expected delta.
4. Report current totals to user before starting next stage.

After Stage 3:
1. Confirm `TOTAL` ≥ 95%.
2. Add `fail_under = 95` to `[tool.coverage.report]` in `pyproject.toml`.
3. Run `pytest --cov=src --cov-fail-under=95` once to verify the gate.

## Excluded / Deferred

`# pragma: no cover` is appropriate for:
- `src/usage_logger.py` lines 153–156 — `sqlalchemy[asyncio]` ImportError fallback. The dep is in `pyproject.toml`, so the branch is unreachable in any environment that runs the tests.
- `src/usage_logger.py` lines 194–195 — best-effort `engine.dispose()` exception swallow during shutdown.
- Any `if __name__ == "__main__":` block (already excluded by config).

Modules where 100% is noisy and not pursued:
- `usage_logger.py` — stop at ~90% (DB lifecycle has shutdown-only branches).
- `backends/opencode/client.py` — stop at ~95% (rare network-error variants).

The 95% target is for the **overall** number, not every module.

## Out of Scope

- Adding e2e tests (`-m e2e` is already deselected by `addopts`; that suite stays as-is).
- Refactoring source modules to be more testable. If a module genuinely cannot be tested without a refactor, flag it and stop — do not silently change source code beyond the scope of "add tests."
- New CI configuration beyond the `fail_under` gate.
- Coverage for `tests/` itself.

## Open Risks

- **Stage 1 fake-engine fidelity.** `_FakeEngine` returns whatever rows the test sets up; it does not validate that the SQL itself is valid MySQL. This is acceptable for unit-level coverage but means a malformed query could pass tests. The Stage 1 plan deliberately stays with fakes (no real DB) — SQL syntax validation is left to integration / staging environments. Accept this trade-off rather than expanding test infrastructure.
- **Stage 2 SSE chunk shape drift.** The OpenCode backend depends on a remote SSE event format; tests will pin a snapshot of expected event shapes. If the upstream format changes, tests will need updating — that is the correct failure mode.
- **`fail_under` gate at 95%.** A dependency upgrade that adds untested code paths will break CI. That is the intended behavior; the fix is to add tests, not lower the gate.
