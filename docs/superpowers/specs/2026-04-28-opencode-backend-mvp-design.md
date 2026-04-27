# OpenCode Backend MVP Design

Date: 2026-04-28

## Goal

Add an `opencode` backend to the existing FastAPI gateway so clients can call
OpenCode through the same `/v1/responses` and `/v1/models` surfaces currently
used for Claude.

The MVP prioritizes a stable backend integration over full Claude feature
parity. It must support basic and multi-turn Responses API calls, expose
configured OpenCode models, and manage or connect to an OpenCode server.

## Non-Goals

The MVP does not implement token-level OpenCode event streaming, OpenCode
`question` tool continuation, automatic conversion of the existing wrapper
`MCP_CONFIG` into OpenCode config, or full Docker production hardening. Those
are follow-up phases after the backend contract is proven.

## Architecture

Add a new backend package:

- `src/backends/opencode/__init__.py` defines the descriptor, model resolver,
  lazy exports, and `register()`.
- `src/backends/opencode/client.py` implements the `BackendClient` protocol.
- `src/backends/opencode/auth.py` implements `BackendAuthProvider`.
- `src/backends/opencode/constants.py` owns OpenCode env parsing, defaults, and
  model aliases.

`src/backends/__init__.py::discover_backends()` reads `BACKENDS`, a
comma-separated backend allowlist. The default is `BACKENDS=claude`, preserving
the current behavior. Supported values for the MVP are `claude` and `opencode`.

Examples:

```bash
BACKENDS=claude
BACKENDS=claude,opencode
BACKENDS=opencode
```

The OpenCode backend talks to OpenCode over HTTP with `httpx`. The official
JS/TS SDK is useful as API reference, but the Python gateway should not add a
Node helper process just to call the SDK.

## Server Lifecycle

The backend supports two modes:

1. External server mode: when `OPENCODE_BASE_URL` is set, the backend connects
   to that URL and never starts or stops an OpenCode process.
2. Managed server mode: when `OPENCODE_BASE_URL` is unset, the backend starts
   `opencode serve --hostname 127.0.0.1 --port <port>` as a child process.

Defaults:

- `BACKENDS=claude`
- `OPENCODE_BASE_URL` unset
- `OPENCODE_HOST=127.0.0.1`
- `OPENCODE_PORT=0` for managed mode, allowing OpenCode to choose a free port
- `OPENCODE_START_TIMEOUT_MS=5000`

In managed mode the backend parses the `opencode server listening on ...` line
from stdout, stores the discovered base URL, verifies `/global/health`, and
terminates the process during gateway shutdown.

Managed mode requires the `opencode` binary on `PATH`. External server mode does
not require the binary in the wrapper process environment.

## Model Routing

OpenCode models use `provider/model`. Public wrapper model IDs use an
`opencode/` prefix:

- `opencode/anthropic/claude-sonnet-4-5`
- `opencode/openai/gpt-5.1-codex`
- `opencode/opencode/gpt-5.1-codex`

The resolver maps `opencode/<provider>/<model>` to:

- `backend="opencode"`
- `provider_model="<provider>/<model>"`

The OpenCode client splits `provider_model` into the body expected by
`POST /session/{id}/message`:

```json
{"providerID": "<provider>", "modelID": "<model>"}
```

`/v1/models` exposes a conservative configured list from
`OPENCODE_MODELS`, a comma-separated list of full `provider/model` IDs. This
avoids needing a startup network call to enumerate every provider model.

## Request Flow

For a new `/v1/responses` request using an OpenCode model:

1. The existing route resolves the model to the OpenCode backend.
2. `create_client()` creates an OpenCode session with the gateway session ID
   as the stable mapping key and stores the OpenCode session ID on a lightweight
   client object.
3. On the first turn, the backend sends the resolved wrapper base system prompt
   plus request instructions as `system` in the OpenCode prompt body.
4. `run_completion_with_client()` sends `POST /session/{id}/message`.
5. The backend converts the returned `{ info, parts }` into gateway-compatible
   chunk dictionaries.
6. Existing route code commits `turn_counter`, user message, assistant text,
   and response ID as it does for Claude.

For a continuation request, the existing `previous_response_id` guard keeps the
same gateway session and reuses the stored OpenCode session ID.

## Streaming MVP

The MVP supports the wrapper's streaming API shape without OpenCode token-level
streaming. For `stream=true`, the backend waits for OpenCode's completed
`session.prompt` response, then yields an assistant chunk. Existing
`streaming_utils` emits the standard Responses SSE sequence from that chunk.

OpenCode event streaming via `session.prompt_async` and `/event` is a follow-up.
It needs separate handling for `message.part.updated`, tool events, errors, and
session idle completion.

## Tools, Questions, and MCP

The MVP does not map Claude tool names to OpenCode tool names. OpenCode tools
are controlled through OpenCode config and permissions.

The OpenCode `question` tool is disabled by default in managed config for the
MVP. The current wrapper `function_call_output` path is Claude hook-specific;
supporting OpenCode question continuation requires a separate design around
OpenCode events or TUI control APIs.

Wrapper `MCP_CONFIG` is not converted in the MVP. Operators can configure
OpenCode MCP servers through `opencode.json` or `OPENCODE_CONFIG_CONTENT`.

## Auth and Config

`OpenCodeAuthProvider` validates that the `opencode` binary exists for managed
mode and that the external server is reachable for external mode. Provider API
keys remain OpenCode's responsibility through its normal auth/config system.

The backend passes selected config to managed OpenCode through
`OPENCODE_CONFIG_CONTENT`. MVP managed config sets safe defaults:

- `permission.question = "deny"`
- `share = "disabled"`
- optional `model` if `OPENCODE_DEFAULT_MODEL` is set

Existing gateway API key auth remains unchanged.

## Error Handling

Startup:

- If `BACKENDS` does not include `opencode`, no OpenCode descriptor or client
  is registered.
- If `BACKENDS` includes `opencode` but startup fails, register the descriptor
  and log the failure; the live backend is unavailable and requests receive
  the existing "backend not available" response.
- If `BACKENDS` contains an unknown backend name, startup logs a clear warning
  and skips that name. Known backends continue registering.

Requests:

- HTTP errors from OpenCode become backend error chunks with sanitized messages.
- Empty OpenCode text becomes the existing `No response from backend` path.
- If the managed child process exits, verification fails and active requests
  return a backend error.
- The existing `create_client` failure response is made backend-neutral so
  OpenCode failures are not reported as Claude SDK failures.

## Testing

Add focused tests for:

- OpenCode descriptor fields and `opencode/<provider>/<model>` resolution.
- `discover_backends()` registration gated by `BACKENDS`.
- `src.auth.auth_manager.get_provider("opencode")` returns `OpenCodeAuthProvider`
  before and after backend registration.
- Managed server startup parsing with subprocess mocked.
- External server mode using `OPENCODE_BASE_URL`.
- `create_client()` and `run_completion_with_client()` using mocked `httpx`.
- `/v1/models` includes OpenCode models when the backend is registered.
- `/v1/responses` dispatches to OpenCode and preserves `previous_response_id`
  turn validation.

## Rollout

The feature is opt-in through `BACKENDS=claude,opencode` or `BACKENDS=opencode`.
Claude remains the default backend and existing model IDs keep their current
behavior when `BACKENDS` is unset.

Local development flow:

```bash
export BACKENDS=claude,opencode
export OPENCODE_MODELS=anthropic/claude-sonnet-4-5,openai/gpt-5.1-codex
uv run uvicorn src.main:app --reload --port 8000
```

External server flow:

```bash
opencode serve --hostname 127.0.0.1 --port 4096
export BACKENDS=claude,opencode
export OPENCODE_BASE_URL=http://127.0.0.1:4096
uv run uvicorn src.main:app --reload --port 8000
```
