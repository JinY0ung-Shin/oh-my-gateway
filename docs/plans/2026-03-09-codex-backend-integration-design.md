# Codex Backend Integration Design

## Summary

This document proposes adding Codex CLI as a second execution backend to
`claude-code-openai-wrapper`, while preserving the existing OpenAI-compatible
FastAPI API surfaces.

The gateway will continue to expose:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`

The first two endpoints become backend-aware and dispatch by `model`. The
Anthropic-compatible `/v1/messages` endpoint remains Claude-only.

The design keeps the external API stable, avoids adding a new endpoint, and
introduces a backend abstraction so `main.py` does not accumulate Claude-vs-Codex
branching logic.

## Background

This project currently wraps the Claude Agent SDK and exposes an OpenAI-compatible
gateway on top of it. The current codebase assumes a single backend in several
areas:

- model validation
- auth status reporting
- session behavior
- models listing
- health and capability reporting

Codex integration should not be implemented as ad hoc `if model == "codex"` logic
scattered across the request path. Instead, the gateway should evolve from a
single-backend wrapper into a small multi-backend dispatch layer with stable API
compatibility semantics.

## Goals

- Add Codex CLI as an alternate execution backend behind existing endpoints.
- Route requests by `model` without adding new public endpoints.
- Preserve existing Claude behavior by default.
- Reuse existing SSE response builders and response-shaping logic.
- Support Codex session continuity for `/v1/chat/completions` and `/v1/responses`.
- Keep backend authentication isolated from gateway authentication.
- Make backend capabilities visible through backend-aware status and models
  endpoints.

## Non-Goals

- Adding Codex support to `/v1/messages`
- Building a Python SDK wrapper around Codex beyond the gateway's needs
- Supporting mixed Claude/Codex multi-turn sessions
- Supporting ChatGPT OAuth login mode for server deployments
- Changing the public API shape of existing endpoints

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Routing | Backend dispatch by `model` inside existing endpoints | Avoids API surface sprawl and keeps clients unchanged |
| Model resolver | `resolve_model()` maps `codex` and `codex/<submodel>` to Codex backend | Supports backend aliasing and future Codex sub-model selection |
| Backend abstraction | `BackendClient` Protocol + `BackendRegistry` singleton | Prevents `main.py` from growing backend-specific control flow |
| Codex execution | Spawn Rust CLI directly with `asyncio.create_subprocess_exec()` | No official Python SDK exists; CLI is the primary supported surface |
| Codex transport | `codex exec --json` and `codex exec resume <thread_id> --json` | Confirmed on local `codex-cli 0.111.0`; JSON mode is no longer experimental |
| Streaming integration | Normalize Codex JSONL into the existing internal chunk dict format | Allows `streaming_utils.py` to remain unchanged |
| Sessions | Extend the unified session store with backend metadata and provider session id | Avoids split state across multiple managers |
| Previous response chaining | Strict latest-only validation with `409 Conflict` for stale ids | Matches the actual semantics of resumable backend threads |
| Auth model | Shared gateway auth, backend-specific auth providers | Clean separation of client auth and upstream provider auth |
| Default enablement | `CODEX_ENABLED=false` by default | Keeps rollout opt-in and safe for existing users |

## Routing Design

### Public API behavior

- `POST /v1/chat/completions`
  - backend-aware
  - routed by `model`
- `POST /v1/responses`
  - backend-aware
  - routed by `model`
- `POST /v1/messages`
  - Claude-only
  - rejects Codex models with a clear `400` error

### Model resolution

Introduce a `resolve_model()` function that returns a normalized backend target:

| Incoming model | Resolved backend | Provider model |
|----------------|------------------|----------------|
| `sonnet` | `claude` | `sonnet` |
| `opus` | `claude` | `opus` |
| `codex` | `codex` | `None` |
| `codex/o3` | `codex` | `o3` |
| `codex/gpt-5` | `codex` | `gpt-5` |

Proposed normalized shape:

```python
@dataclass(frozen=True)
class ResolvedModel:
    public_model: str
    backend: str
    provider_model: str | None
```

Rules:

- `codex` selects the Codex backend with its backend default model behavior.
- `codex/<submodel>` selects Codex plus an explicit provider sub-model.
- Existing Claude models continue to resolve to the Claude backend.
- Unknown models should continue to be allowed for graceful degradation, but the
  resolved backend must be explicit.

## Backend Abstraction

### BackendClient Protocol

Add a backend protocol so endpoint code depends on a stable interface instead of
concrete Claude or Codex implementations.

```python
class BackendClient(Protocol):
    async def run_completion(...) -> AsyncGenerator[dict, None]:
        ...

    def parse_message(self, chunks: list[dict]) -> str | None:
        ...

    def estimate_token_usage(
        self,
        prompt: str,
        completion: str,
        model: str | None = None,
    ) -> dict[str, int]:
        ...

    async def verify(self) -> bool:
        ...
```

The method names intentionally match current usage patterns:

- `run_completion()` for streaming and non-streaming chunk generation
- `parse_message()` for extracting the assistant text from normalized chunks
- `estimate_token_usage()` for usage fallback
- `verify()` for startup health checks

`src/claude_cli.py` will be adapted to implement this protocol. `src/codex_cli.py`
will implement the same protocol from the start.

### BackendRegistry

Add `src/backend_registry.py` with a singleton registry that owns backend
instances and model resolution.

Responsibilities:

- register backend clients by backend name
- expose `resolve_model()`
- expose `get_backend(resolved_model)`
- expose model listings for `/v1/models`
- expose backend verification for startup and health reporting

This keeps `main.py` on a single dispatch path:

```text
request -> resolve_model() -> registry.get_backend() -> backend.run_completion()
```

No endpoint-level Claude/Codex branching should be required beyond the explicit
Claude-only restriction on `/v1/messages`.

## Codex CLI Integration

### Execution model

Codex will be integrated by calling the Rust CLI binary directly.

Commands:

- new turn: `codex exec --json`
- resumed turn: `codex exec resume <thread_id> --json`

Implementation details:

- use `asyncio.create_subprocess_exec()`
- read stdout line-by-line
- parse JSONL events
- ignore non-JSON preamble lines with a `line.startswith("{")` guard
- collect stderr for diagnostics
- enforce timeout via `asyncio.wait_for()` or equivalent task supervision

The CLI path will be configurable through `CODEX_CLI_PATH`.

### Why direct CLI spawn

- no official Python SDK is available
- the CLI already exposes the needed non-interactive JSON stream
- the CLI is the authoritative integration surface
- the gateway only needs process spawning plus event normalization

### Proposed `src/codex_cli.py`

New file, roughly 300 lines, responsible for:

- command construction
- subprocess execution
- environment isolation
- JSONL parsing
- event normalization
- usage extraction
- assistant message extraction
- verification

This keeps Codex-specific logic out of `main.py` and out of `streaming_utils.py`.

## Event Normalization and Streaming

### Design choice

Codex event mapping will happen inside `src/codex_cli.py`. The output of both
backends will be the same internal chunk dict format expected by the existing
streaming layer.

`src/streaming_utils.py` changes: `0 lines`

This is a strong constraint and a useful one. It prevents the SSE shaping code
from becoming backend-aware.

### Event mapping

| Codex JSONL event | Internal chunk dict | Notes |
|-------------------|---------------------|-------|
| `item.completed` with `agent_message` | `assistant` | assistant text |
| `item.started` / `item.completed` with `command_execution` | `tool_use`-style chunk | maps command execution to tool-like events |
| `item.completed` with `file_change` | `tool_use`-style chunk | exposes file modifications as structured tool activity |
| `item.completed` with `reasoning` | thinking-like chunk | available only when present |
| `turn.completed` | `result` with `usage` | terminal success event |
| top-level `error` | error chunk | process or provider error |
| `turn.failed` | error chunk | terminal failed turn |

Normalization target principles:

- assistant-visible text should end up in the same shapes Claude currently uses
- tool/progress events should be expressible as structured task/tool events
- usage should be extracted into the same format consumed by the existing code
- failure should produce the same downstream error handling semantics

### Preamble filtering

Codex emits plain-text preamble lines such as:

```text
Reading prompt from stdin...
```

These lines are not JSON and must be filtered before parsing. The parser should
skip any stdout line that does not begin with `{`.

## Session Model

### Unified store

Do not introduce a separate `CodexThreadManager`. Instead, extend the existing
session store so one session object can represent either backend.

Proposed `Session` additions:

- `backend: str`
- `provider_session_id: str | None`

Meaning:

- `backend` is the selected gateway backend, such as `claude` or `codex`
- `provider_session_id` is the backend-native conversation identifier
  - Claude: current session id / resume id behavior
  - Codex: `thread_id`

This keeps TTL, cleanup, session stats, and admin endpoints in one place.

### Session invariants

- a session is bound to exactly one backend for its lifetime
- a session cannot switch from Claude to Codex or vice versa
- `provider_session_id` is backend-native and opaque to callers
- session history remains stored for gateway introspection and compatibility

### Per-session locking

The current code defines a per-session `asyncio.Lock`, but the request path does
not consistently use it. This should be fixed as part of Codex session support.

Required behavior:

- same-session requests are serialized under `session.lock`
- lock covers session state mutation and backend resume execution
- concurrent resume calls for the same Codex thread are prevented

This is both a Codex requirement and a correctness fix for the existing code.

### Mixed multi-turn sessions

Mixed Claude/Codex multi-turn conversations are not supported.

Behavior:

- if a session already exists and the resolved backend differs from the stored
  session backend, return a clear error
- if `previous_response_id` resolves to a session with a different backend than
  the requested `model`, reject the request

## Responses API Semantics

### Strict latest-only `previous_response_id`

Current `previous_response_id` behavior should be tightened for all backends.

New rule:

- only the latest response id for a session may be used as `previous_response_id`
- if the id is valid but stale, return `409 Conflict`

Reasons:

- resumable backend threads continue from the current head state
- accepting old response ids implies fork semantics that do not exist
- latest-only validation is simpler, explicit, and correct

### Error behavior

| Case | Status |
|------|--------|
| invalid `previous_response_id` format | `404` |
| session missing or expired | `404` |
| stale but structurally valid response id | `409` |
| backend mismatch | `409` |

## Authentication and Security

### Separation of concerns

Gateway authentication and backend authentication remain separate.

- gateway auth protects the FastAPI server from callers
- backend auth configures upstream provider access for Claude or Codex

### BackendAuthProvider

Introduce a backend auth abstraction in `src/auth.py`:

```python
class BackendAuthProvider(ABC):
    @abstractmethod
    def validate(self) -> tuple[bool, dict[str, Any]]:
        ...

    @abstractmethod
    def build_env(self) -> dict[str, str]:
        ...
```

Expected implementations:

- `ClaudeAuthProvider`
- `CodexAuthProvider`

### Codex auth

Codex backend auth will use API key mode only.

Reasons:

- server deployments require non-interactive auth
- ChatGPT OAuth mode is not a reliable server-side integration target
- API key auth is explicit and automatable

Environment input:

- `OPENAI_API_KEY`

The gateway may additionally support wrapper-specific compatibility variables
internally if needed, but the upstream CLI contract should remain the source of
truth.

### Cross-isolation

Two-way isolation is required:

- Claude env vars must not leak into Codex subprocesses
- Codex env vars and config must not leak into Claude SDK execution

This includes:

- auth env vars
- provider-specific toggles
- config files
- provider-specific command-line options

### `config.toml` isolation

Codex uses local configuration. Server operation should not implicitly depend on
the operator's personal `~/.codex/config.toml`.

Introduce `CODEX_CONFIG_ISOLATION` to control whether the gateway launches Codex
with an isolated config environment.

Recommended rollout behavior:

- default to isolated config for production-oriented deployments
- keep the flag explicit during rollout so local development remains debuggable

### Default enablement

`CODEX_ENABLED=false` by default.

If disabled:

- Codex models are omitted from `/v1/models`
- requests resolving to Codex return a clear feature-disabled error
- startup verification skips Codex or marks it as disabled

## Backend-Aware Endpoints

### `/v1/models`

Becomes backend-aware and returns both Claude and Codex models, subject to
feature flags.

Behavior:

- Claude models always listed as today
- Codex aliases listed only when `CODEX_ENABLED=true`
- ownership/provider metadata should reflect backend origin

### `/v1/auth/status`

Should report backend-specific auth and enablement state rather than only Claude.

Proposed shape:

- gateway auth status
- Claude backend auth status
- Codex backend auth status
- enabled/disabled flags

### `/health`

Should remain lightweight, but can include backend availability state if the
project wants richer diagnostics in the JSON body.

Minimum behavior:

- service healthy
- backend registry initialized
- disabled backends reported as disabled rather than failed

### `/v1/compatibility`

Should become backend-aware in its messaging. It should stop describing only
Claude SDK options once multiple backends are present.

### `/v1/debug/request`

Example payloads and diagnostics should stop assuming Claude-only model names.

## Environment Variables

New Codex-related variables:

| Variable | Purpose |
|----------|---------|
| `CODEX_ENABLED` | Enable Codex backend dispatch |
| `OPENAI_API_KEY` | Codex backend API key |
| `CODEX_CLI_PATH` | Path to Codex CLI binary |
| `CODEX_APPROVAL_MODE` | Approval mode for Codex CLI execution |
| `CODEX_TIMEOUT_MS` | Timeout for Codex backend requests |
| `CODEX_CONFIG_ISOLATION` | Whether Codex runs with isolated config |

Existing variables remain in place for Claude and gateway behavior.

When implementation begins, `.env.example` and `README.md` must be updated as
part of the same change set.

## File-Level Changes

### New files

| File | Purpose |
|------|---------|
| `src/backend_registry.py` | backend registration, model resolution, backend lookup |
| `src/codex_cli.py` | Codex CLI subprocess transport, JSONL parsing, normalization |
| `tests/test_codex_cli.py` | Codex transport, parsing, normalization, error handling |

### Modified files

| File | Change |
|------|--------|
| `src/claude_cli.py` | implement `BackendClient` protocol, minor naming alignment |
| `src/constants.py` | add Codex config defaults and backend-aware model constants |
| `src/models.py` | model validation helpers and any backend-aware model metadata |
| `src/parameter_validator.py` | backend-aware model resolution and compatibility reporting |
| `src/session_manager.py` | add `backend`, `provider_session_id`, and real per-session lock usage |
| `src/auth.py` | add `BackendAuthProvider` abstraction and Codex auth handling |
| `src/main.py` | replace direct Claude calls with registry dispatch |
| `.env.example` | document new Codex env vars |

## Implementation Sketch

### Request flow

```text
request
  -> resolve_model(request.model)
  -> registry.get_backend(resolved.backend)
  -> backend auth validation
  -> session lookup / creation
  -> backend.run_completion()
  -> normalized chunks
  -> existing streaming_utils / response builders
```

### Stateless chat flow

```text
/v1/chat/completions
  -> resolve_model()
  -> build backend-agnostic options
  -> backend.run_completion(..., stream=True/False)
  -> backend.parse_message()
  -> existing ChatCompletionResponse builders
```

### Sessioned responses flow

```text
/v1/responses
  -> resolve_model()
  -> validate previous_response_id against latest turn
  -> acquire session.lock
  -> backend.run_completion(session/provider_session_id)
  -> commit turn only on success
  -> release lock
```

## Rollout Plan

### Phase 1: Backend registry and stateless Codex support

- add `BackendClient` protocol
- add `BackendRegistry`
- implement `src/codex_cli.py`
- support stateless `POST /v1/chat/completions`
- keep `/v1/messages` Claude-only

### Phase 2: Sessioned chat completions and lock fix

- extend unified session store
- persist backend and provider session id
- enforce per-session locking
- reject mixed-backend multi-turn sessions

### Phase 3: `/v1/responses` and strict `previous_response_id`

- add Codex support to `/v1/responses`
- make `previous_response_id` latest-only
- return `409` for stale response ids
- ensure turn commit is success-only under streaming and non-streaming paths

### Phase 4: Models, auth, health, docs

- backend-aware `/v1/models`
- backend-aware `/v1/auth/status`
- backend-aware `/health` details
- update `.env.example`
- update `README.md`
- add operational documentation

## Testing Strategy

### Unit tests

- `tests/test_codex_cli.py`
  - command construction
  - preamble filtering
  - JSONL parsing
  - event normalization
  - timeout behavior
  - error mapping
- `tests/test_parameter_validator_unit.py`
  - `resolve_model()` behavior
  - Codex model alias handling
- `tests/test_session_manager_unit.py`
  - backend/provider session persistence
  - per-session lock behavior
  - mixed-backend rejection

### API tests

- `tests/test_main_api_unit.py`
  - Codex stateless chat completion
  - Codex sessioned chat completion
  - Codex `/v1/responses`
  - latest-only `previous_response_id`
  - backend-aware `/v1/models`
  - backend-aware `/v1/auth/status`
  - `/v1/messages` rejecting Codex models

### Regression tests

- all existing Claude tests continue to pass unchanged or with minimal fixture
  updates
- `streaming_utils.py` behavior remains unchanged because Codex normalizes into
  the existing chunk contract

## Risks and Mitigations

| Risk | Level | Mitigation |
|------|-------|------------|
| Codex `--json` protocol changes | Low | isolate parsing and normalization in `src/codex_cli.py`; verify at startup |
| Platform-specific CLI binary availability | Medium | configurable `CODEX_CLI_PATH`; startup verification; clear disablement path |
| No official Python SDK | Medium | keep integration boundary small and transport-only |
| OAuth-based login unsuitable for servers | High | support API key mode only |
| Session concurrency bugs | High | enforce `session.lock` around resume paths |
| Personal Codex config leakage | High | add config isolation and backend-specific env construction |

## Rejected Alternatives

### New Codex-specific endpoint

Rejected because:

- duplicates existing API surface
- complicates clients
- weakens compatibility guarantees

### Separate `CodexThreadManager`

Rejected because:

- duplicates TTL and cleanup logic
- splits session state
- complicates admin endpoints and stats

### Making `streaming_utils.py` backend-aware

Rejected because:

- pushes backend-specific complexity into the shared response layer
- increases regression risk for Claude
- makes future backends harder to add cleanly

## Open Questions

- Whether Codex sub-model aliases should be enumerated in `/v1/models` or only
  documented as free-form `codex/<submodel>`
- Whether `CODEX_CONFIG_ISOLATION` should default to `true` immediately or remain
  explicitly opt-in for the first rollout
- Whether command/file-change events should map to the existing task event shape
  or a slightly richer internal chunk variant before SSE rendering

## Recommendation

Proceed with the phased integration described above.

The critical design constraint is not the subprocess transport itself; it is
keeping backend-specific behavior behind a registry and a normalized chunk
contract. If that boundary is preserved, Codex can be added with limited impact
to the existing Claude-compatible gateway behavior.
