# Oh My Gateway

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://github.com/JinY0ung-Shin/oh-my-gateway)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

OpenAI-compatible gateway for coding agent backends. It exposes the Claude Agent SDK through `/v1/responses`, plus a stateless Claude SDK event stream at `/v1/agents/messages`, with MCP integration, workspace isolation, and an admin dashboard. OpenCode and Codex backends are still in the tree but **stale** (frozen since July 2026, unmaintained).

> Previously published as **Claude Code Gateway**. The repository was renamed because the gateway now fronts multiple agent backends, not just Claude. The Docker Compose service is now `gateway`; update commands such as `docker compose logs claude-wrapper` to use `gateway`.

## Quick Start

```bash
git clone https://github.com/JinY0ung-Shin/oh-my-gateway
cd oh-my-gateway
uv sync
cp .env.example .env

export ANTHROPIC_AUTH_TOKEN=your-api-key
export ADMIN_API_KEY=change-this-admin-key
uv run uvicorn src.main:app --reload --port 8000
```

`ADMIN_API_KEY` is required at startup because the admin surface can change runtime settings and read live diagnostics. Use `API_KEY` separately when public gateway endpoints need bearer-token protection.

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Hello"}'
```

## What It Provides

- **Responses API**: `/v1/responses` with non-streaming and SSE streaming responses.
- **Background mode**: `background: true` returns a `queued` response immediately and runs the turn server-side; poll `GET /v1/responses/{response_id}`, cancel with `POST /v1/responses/{response_id}/cancel`.
- **Stateless agent API**: `/v1/agents/messages` with caller-owned history and Claude SDK events.
- **Backends**: Claude (`sonnet`, `opus`, `haiku`) is the maintained backend. OpenCode (`opencode/<provider>/<model>`) and Codex (`codex/<model>`) are stale — kept frozen, unmaintained.
- **Session continuity**: `previous_response_id` and server-side session tracking.
- **Workspace isolation**: temporary sessions by default, or per-user directories with `USER_WORKSPACES_DIR`.
- **MCP support**: shared gateway `MCP_CONFIG`, with optional OpenCode managed-mode config generation.
- **Admin tools**: `/admin` dashboard, `/admin/chat`, runtime config, sessions, logs, prompts, plugin & marketplace management (install/remove + periodic auto-refresh), and diagnostics.
- **Docker support**: Dockerfile and Compose setup with optional usage-log MySQL sidecar.

## Documentation

| Topic | Doc |
|-------|-----|
| Claude backend setup, auth, workspaces, sandbox, MCP, subagents | [docs/claude-code/](docs/claude-code/) |
| OpenCode backend overview and mode selection (stale) | [docs/opencode/](docs/opencode/) |
| OpenCode managed mode (stale) | [docs/opencode/managed.md](docs/opencode/managed.md) |
| OpenCode external mode (stale) | [docs/opencode/external.md](docs/opencode/external.md) |
| OpenCode + LiteLLM recipes (stale) | [docs/opencode/litellm.md](docs/opencode/litellm.md) |
| Codex backend setup and SDK status (stale) | [docs/codex/](docs/codex/) |
| Streaming event reference | [docs/streaming-events.md](docs/streaming-events.md) |
| System prompt presets | [docs/](docs/) |

## Backend Modes

Claude is enabled by default:

```bash
BACKENDS=claude
DEFAULT_MODEL=sonnet
```

> **Stale backends.** OpenCode and Codex are frozen as of July 2026 and receive no maintenance:
> their tests are excluded from the default suite and the code is kept for reference only.
> Enabling them still works today (with a startup warning) but they may break without notice.

Enable the stale OpenCode backend alongside Claude:

```bash
BACKENDS=claude,opencode
OPENCODE_MODELS=openai/gpt-5.5
```

Enable the stale Codex backend alongside Claude:

```bash
BACKENDS=claude,codex
CODEX_MODELS=gpt-5.5
```

The Codex backend uses the local `codex app-server` harness through JSON-RPC, not the OpenAI Responses API. It was still experimental when frozen; see [docs/codex/](docs/codex/) for the freeze-time integration notes.

OpenCode has two modes:

| Mode | How it works | Use when |
|------|--------------|----------|
| Managed | Gateway starts `opencode serve` as a subprocess and generates its config | Gateway container is the trust boundary |
| External | Gateway forwards to an existing `opencode serve` via `OPENCODE_BASE_URL` | OpenCode needs broader filesystem access or a separate lifecycle |

External mode example:

```bash
OPENCODE_BASE_URL=http://opencode-host:7891
OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=...
OPENCODE_MODELS=litellm/claude-sonnet-4-5
```

Check active backend mode:

```bash
curl -s http://localhost:8000/admin/api/backends \
  | jq '.backends[] | select(.name == "opencode") | .metadata'
```

## Configuration

Most settings are environment variables. Start with `.env.example`.

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_AUTH_TOKEN` | Claude API key auth |
| `CLAUDE_AUTH_METHOD` | Force `api_key` or `cli` auth |
| `BACKENDS` | Backend allowlist; `claude` is the only maintained backend (`opencode`/`codex` are stale) |
| `DEFAULT_MODEL` | Default model for requests without `model` |
| `DEFAULT_MAX_TURNS` | Maximum agent turns per request |
| `MAX_TIMEOUT` | Backend timeout in milliseconds |
| `BACKGROUND_RESPONSE_TIMEOUT_S` | Wall-clock cap for one `background: true` turn in seconds; default `3600` |
| `MAX_REQUEST_SIZE` | Maximum request body size in bytes |
| `MAX_LIVE_SESSIONS` | Sessions held in memory before new ones get `503`; default `12` (see Capacity) |
| `SESSION_EVICTION_POLICY` | At the session cap: `reject` (default, 503) or `lru` — evict the least-recently-used idle session to admit the newcomer |
| `SESSION_EVICTION_MIN_IDLE_SECONDS` | Sessions accessed within this window are never evicted (thrash guard); default `60`, clamped to ≥1 |
| `MAX_CONCURRENT_TURNS` | Agent turns allowed to run simultaneously; default `8` |
| `MAX_CONCURRENT_TURNS_PER_USER` | Per-caller fairness hint keyed on `user` — not a security control; default `3` |
| `SSE_KEEPALIVE_INTERVAL` | SSE keepalive comment interval; `0` disables it |
| `GATEWAY_HOST` | Host bind address; falls back to legacy `CLAUDE_WRAPPER_HOST` |
| `USER_WORKSPACES_DIR` | Per-user workspace root (per-session temp dir if unset) |
| `MCP_CONFIG` | Shared MCP server config |
| `METADATA_ENV_ALLOWLIST` | Request metadata keys forwarded as env vars to Claude |
| `ASK_USER_TIMEOUT_SECONDS` | AskUserQuestion wait time before denying the tool call |
| `BLOCKED_DEFERRED_TOOLS` | Post-turn schedulers to deny; default `ScheduleWakeup,CronCreate` (see below) |
| `OPENCODE_BASE_URL` | Enables OpenCode external mode (stale backend) |
| `OPENCODE_MODELS` | Gateway allowlist for OpenCode models (stale backend) |
| `CODEX_BIN` | Codex CLI binary name/path; default `codex` (stale backend) |
| `CODEX_MODELS` | Gateway allowlist for Codex models; default `gpt-5.5` (stale backend) |
| `CODEX_APPROVAL_POLICY` | Codex approval policy; default `never` |
| `CODEX_SANDBOX` | Codex thread sandbox mode; default `danger-full-access` for local experimental use |
| `CODEX_CONFIG_OVERRIDES` | Comma-separated `codex --config key=value` overrides |
| `API_KEY` | Optional public API bearer token |
| `ADMIN_API_KEY` | Required admin dashboard key |
| `USAGE_LOG_DB_URL` | Optional SQLAlchemy URL for usage logging |

### No post-turn scheduling

`/v1/responses` is a single synchronous turn: when the stream closes, nothing the
model scheduled can still reach the caller. So the CLI's post-turn schedulers
(`ScheduleWakeup`, `CronCreate` — and skills built on them, e.g. `/loop`) are
denied by default (`BLOCKED_DEFERRED_TOOLS`). The denial carries a reason the model
reads back — "do the work inside this turn; recurring runs are not supported here" —
instead of the CLI's opaque *"CronCreate isn't available in this context"*, which
left the model ending the turn with nothing.

Supporting recurring runs is a feature, not a config flip: it needs a server-owned
run loop, per-user quotas and expiry, and a delivery channel the client can read
after the turn. Clear `BLOCKED_DEFERRED_TOOLS` only when the surface embedding the
gateway has all three.

## Capacity

Sizing the gateway is driven by one fact: a session keeps its Claude CLI
subprocess alive for its whole TTL (`SESSION_MAX_AGE_MINUTES`, default 60),
not just while a turn runs. A live subprocess measures ~400 MB RSS, so budget
**~0.5 GB per live session** including headroom for MCP child processes.

`MAX_LIVE_SESSIONS` is what prevents an OOM on the `/v1/responses` path.
`MAX_CONCURRENT_TURNS` bounds CPU and upstream API pressure there — capping
in-flight turns alone would not help, since idle sessions still hold their
subprocess.

**`/v1/agents/messages` is stateless**: every call builds a fresh SDK client,
never enters the session manager, and so counts against neither
`MAX_LIVE_SESSIONS` nor `gateway_live_sessions`. Those runs are bounded only
by `MAX_CONCURRENT_TURNS`, which makes the worst case a **sum**:

```
peak subprocesses = MAX_LIVE_SESSIONS + MAX_CONCURRENT_TURNS
```

| Host memory | Peak subprocesses it fits | Example |
|---|---|---|
| 4 GB | ~4 | `MAX_LIVE_SESSIONS=3`, `MAX_CONCURRENT_TURNS=2` |
| 8 GB | ~9 | `MAX_LIVE_SESSIONS=6`, `MAX_CONCURRENT_TURNS=4` |
| 16 GB | ~19 | `MAX_LIVE_SESSIONS=12`, `MAX_CONCURRENT_TURNS=8` (defaults) |

If you never call `/v1/agents/messages`, budget `MAX_LIVE_SESSIONS` alone and
the defaults are comfortable on 8 GB. Startup warns whenever the sum exceeds
what the detected cgroup/host memory can hold, so trust that over this table.

A `background: true` turn holds its slot for the life of the detached run,
not just until the `queued` reply is sent — otherwise background mode would
be a free bypass of `MAX_CONCURRENT_TURNS`. A wedged background run therefore
occupies a slot until `BACKGROUND_RESPONSE_TIMEOUT_S` (default 3600) expires,
so with the defaults eight stuck runs can saturate the turn cap for an hour.
Lower that timeout if your workload makes it likely.

Both limits are fail-fast: a refused caller gets `503` with `Retry-After`
rather than being queued. Continuations (`previous_response_id`) are never
refused, and non-agent endpoints (`/health`, `/admin`, `/v1/models`,
`/files/*`) stay reachable so a saturated gateway remains diagnosable. Set any
limit to `0` to disable it.

The gateway logs a startup warning when `MAX_LIVE_SESSIONS` exceeds what the
detected cgroup (or host) memory can hold. Watch `gateway_live_sessions`,
`gateway_turns_in_flight`, `gateway_turns_rejected_total`,
`gateway_sessions_rejected_total`, and (with `SESSION_EVICTION_POLICY=lru`)
`gateway_sessions_evicted_total` on `/metrics` to tune it.

> Single process only. Sessions, the rate limiter, and the Prometheus registry
> are all in-memory, so these limits are per-process and running multiple
> uvicorn workers will not behave as expected.

### Known limits of these controls

They bound resource use; they are not an authorization boundary. Deploy behind
`API_KEY` (and a trusted network) if callers are not trusted.

- **`MAX_CONCURRENT_TURNS_PER_USER` is a fairness hint, not a security
  control.** `user` is caller-supplied and tied to no credential — rotating
  it evades the limit, and claiming another caller's value burns their share.
  Opting out entirely is also trivial: omit `user`, or send the body with
  `Transfer-Encoding: chunked` so there is no content-length to peek at. Only
  `MAX_CONCURRENT_TURNS` is load-bearing against a hostile caller.
- **Session slots have no eviction or fairness by default.** `touch()`
  refreshes the TTL on every access, so a caller that keeps
  `MAX_LIVE_SESSIONS` conversations warm holds them indefinitely and everyone
  else gets `503`. With the defaults that is 12 cheap requests per hour.
  Recovering needs an admin session delete — or opt in to
  `SESSION_EVICTION_POLICY=lru`, which evicts the least-recently-used idle
  session instead of refusing. Note eviction is still not *fairness*: a
  keep-warm caller whose sessions are always fresher than the
  `SESSION_EVICTION_MIN_IDLE_SECONDS` floor continues to hold its slots.
- **`/metrics` is unauthenticated and now publishes capacity.**
  `gateway_turns_in_flight` and `gateway_live_sessions` let anyone who can
  reach the endpoint read the exact number of free slots, and the
  `gateway_turns_rejected_total` `scope` label reveals which limit binds —
  information the `503` bodies deliberately withhold. Restrict `/metrics` at
  the reverse proxy on any deployment where that matters.

## Docker

```bash
docker build -t oh-my-gateway .

docker run -d -p 8000:8000 \
  -e ANTHROPIC_AUTH_TOKEN=your-key \
  -e ADMIN_API_KEY=admin-secret \
  oh-my-gateway
```

With Compose:

```bash
cp .env.example .env
docker compose up -d
```

On startup the Compose image repairs gateway-owned bind-mount permissions for
`./data` and then runs the server as a non-root user. If your host user is not
uid/gid 1000, set `APP_UID=$(id -u)` and `APP_GID=$(id -g)` in `.env` before
starting Compose.

For corporate networks, Compose forwards build-time mirror settings from the
host environment or `.env`:

```bash
APT_MIRROR_URL=http://apt-mirror.example.com/debian \
APT_SECURITY_MIRROR_URL=http://apt-mirror.example.com/debian-security \
NPM_CONFIG_REGISTRY=https://npm.example.com/repository/npm/ \
PIP_INDEX_URL=https://pypi.example.com/simple/ \
docker compose build
```

To run a local corporate install script during the image build without checking
it into this repository, point Compose at the script path:

```bash
GATEWAY_BUILD_INSTALL_SCRIPT=/opt/company/install_gateway_addons.sh \
docker compose build
```

BuildKit does not include secret contents in the cache key. If the script
changes, rebuild with `docker compose build --no-cache` or set
`GATEWAY_BUILD_INSTALL_CACHE_BUST` to a new non-secret value.

The Docker image is pinned to Debian trixie. The default `Dockerfile` /
`docker-compose.yml` builds a Claude-only image and does not install OpenCode
or Codex; pick a backend-specific Compose file when the gateway container
should own that backend's runtime.

For an OpenCode-only deployment, use the separate Compose file. It builds
`Dockerfile.opencode`, installs `opencode-ai@${OPENCODE_VERSION:-1.14.29}`,
forces `BACKENDS=opencode`, defaults `OPENCODE_MODELS` to `openai/gpt-5.5`,
and persists OpenCode config and state in named Docker volumes:

```bash
docker compose -f docker-compose.opencode.yml up -d --build
```

When mirroring on corporate networks, host the `opencode-ai` package plus the
matching platform package such as `opencode-linux-x64` or
`opencode-linux-arm64`.

For a Codex-only deployment, use the separate Compose file. It builds a
Codex-specific image, installs `@openai/codex`, forces `BACKENDS=codex`, and
persists Codex CLI state in a named Docker volume:

```bash
docker compose -f docker-compose.codex.yml up -d --build
```

The Compose service is named `gateway`. Optional usage logging is available through the `logging` profile:

```bash
docker compose --profile logging up -d
```

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
```

Docker builds export dependency pins from `uv.lock` at build time, so `uv lock`
(or `uv sync` after editing `pyproject.toml`) is all that's needed after
dependency changes — there is no committed `requirements.txt` to refresh.

Run a live OpenCode smoke test only when a real provider is configured:

```bash
OPENCODE_SMOKE_ENABLED=1 \
OPENCODE_SMOKE_MODEL=openai/gpt-5.5 \
uv run pytest tests/integration/test_opencode_smoke.py -q
```

## API Surface

Primary endpoints:

- `POST /v1/responses`
- `GET /v1/responses/{response_id}` (retrieve a stored turn; poll background turns)
- `POST /v1/responses/{response_id}/cancel` (Claude streaming or background responses)
- `POST /v1/agents/messages` (stateless Claude SDK event stream)
- `GET /v1/models`
- `GET /v1/sessions`
- `GET /v1/sessions/{session_id}/pending-events?after=<seq>&user=<name>` (between-turn outbox: background task lifecycle + assistant messages captured by the session's idle reader; cursor-paged, polling refreshes the session TTL)
- `GET /v1/auth/status`
- `GET /v1/mcp/servers`
- `GET /v1/mcp/health` (reachability snapshot for every configured MCP server; poll-safe)
- `GET /v1/agent-resources` (skills + subagents the caller's next session would see)
- `GET /health`
- `GET /version`

Admin endpoints live under `/admin` and `/admin/api/*`.

When `USAGE_LOG_DB_URL` is configured, usage analytics are available under `/admin/api/usage/*`: `summary`, `users`, `tools`, `series`, `tools-series`, and `turns`. Usage timestamps are stored in UTC, while dashboard date filters, buckets, and displayed turn times use KST (UTC+09:00) regardless of the server, database, or browser timezone. Older gateway versions stored process-local timestamps without timezone metadata; verify the originating runtime timezone before converting historical rows because their timezone cannot be inferred from the column value alone.

### Stateless agent Messages

`POST /v1/agents/messages` accepts a complete text conversation on every call
and never creates a gateway session or continuation ID:

```json
{
  "agent": "claude",
  "model": "sonnet",
  "system": "Optional per-request instructions",
  "messages": [
    {"role": "user", "content": "First question"},
    {"role": "assistant", "content": "First answer"},
    {"role": "user", "content": "Follow-up"}
  ],
  "stream": true
}
```

Version 1 supports only `agent: "claude"`, Claude models, string or `text`
block content, and `stream: true`. It uses the same bearer authentication and
rate-limit setting as `/v1/responses`, but its execution path is independent of
Responses session storage. Each request runs in an isolated temporary
workspace; the SDK client, workspace, JSONL transcript, and tool-result
artifacts are removed when the stream ends.

The SSE sequence is `message_start`, one or more `sdk_message` events, then
`message_stop`; setup/backend failures use a terminal `error` event. The
`X-Agent-Message-Schema` response header identifies the
`claude-agent-sdk-message-v1` payload contract. SDK dataclasses are recursively
serialized into the envelope shape used by Noah's Claude SDK handlers. Session
IDs and internal transcript/plugin paths are omitted, secret-looking values are
redacted, and `tool_result` bodies are not exposed (tool ID and error status
remain available for UI lifecycle handling).

`AskUserQuestion` is disabled because a stateless request cannot resume a
parked SDK callback. Agents should ask the user in normal assistant text and
receive the answer in the next full-history request.

## Response Compatibility

Effective `/v1/responses` request fields:

- `model`: `sonnet`, `opus`, `haiku`, `opencode/<provider>/<model>`, or `codex/<model>`.
- `input`: string, message array, `input_text` or `input_image` parts, or `function_call_output`.
- `instructions`: system/developer prompt for a new session only.
- `previous_response_id`: continue the latest turn of an existing session.
- `stream`: emit Responses-style SSE events.
- `background`: return a `queued` response immediately and run the turn server-side (see below).
- `metadata`: stored on responses; allowlisted keys can be forwarded to Claude with `METADATA_ENV_ALLOWLIST`.
- `allowed_tools`: explicit tool allowlist for backends that support tool policy enforcement.
  Two entry forms select *inside* a tool: `Skill(<name>)` narrows the skill catalog to
  the named skills, and `Task(<agent>)` narrows which subagents may run (enforced by a
  gateway PreToolUse hook — an unlisted `subagent_type` is denied with a reason the model
  reads back). A bare `Skill`/`Task` keeps its allow-everything meaning and wins over the
  granular entries. Names for both forms come from `GET /v1/agent-resources`, which lists
  plugin, workspace (`<cwd>/.claude/{skills,agents}`) and user-scope definitions alike —
  the plugin admin API sees only the first of the three, so a picker built on it alone
  omits skills the session can still run.
  Subagents always run **in the foreground**: the CLI's background default promises the
  model a completion notification that would land after the HTTP turn closed, so the
  gateway rewrites subagent calls to `run_in_background: false`
  (`FORCE_FOREGROUND_SUBAGENTS=false` to opt out). Both tool spellings are governed —
  current CLI builds renamed `Task` to `Agent`, and a hook bound to the old name alone
  is a silent no-op that lets subagents drift back into the background *and* stops
  enforcing the selection allowlist.
- `disallowed_tools`: explicit tool blocklist; merged with the global `DISALLOWED_TOOLS` setting where supported.
- `reasoning.effort`: thinking effort for the session (`low|medium|high|xhigh|max`, Claude backend).
  Baked at session creation — a continuation keeps what its first turn set. The gateway also sets
  `CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1` for that subprocess, because the CLI otherwise skips the
  field entirely on a custom `ANTHROPIC_BASE_URL` with an unfamiliar model id. **Whether it changes
  anything is the upstream's business**: on the wire it is Anthropic's `output_config.effort`; the
  sanitizer translates that to OpenAI `reasoning_effort` (`xhigh`/`max` clamp to `high`) for
  LiteLLM, and a model that does not take the field simply ignores it (LiteLLM's `drop_params`
  discards it, and the CLI retries without effort if the upstream 400s on it).
- `permission_mode`: set or update the session permission mode (`default`, `acceptEdits`, `bypassPermissions`, or `plan`); omitted continuation requests keep the current session mode.
- `temperature` and `max_output_tokens`: forwarded to Codex as generation controls; accepted for compatibility elsewhere.
- `user`: per-user workspace key.

The request model also accepts `store` for client compatibility.

Notable deviations from OpenAI Responses API behavior:

- `instructions`, `system`, or `developer` input items cannot be used with `previous_response_id`; the session prompt is fixed after turn one.
- A stale `previous_response_id` returns `409` with the latest valid response ID for client recovery.
- Mixing backends inside one session is rejected.
- A streamed Claude response can be interrupted with
  `POST /v1/responses/{response_id}/cancel`. The stream terminates with
  `response.incomplete`; that response id remains valid for
  `previous_response_id`, so the client can immediately redirect the same
  conversation. Keep reading the original SSE stream until its terminal event
  instead of aborting the fetch first.

### Background mode

`background: true` detaches the turn from the HTTP connection: POST returns a
response object with `status: "queued"` immediately, the turn keeps running
server-side, and the session stays pinned (it cannot expire) until the turn
finishes.

```bash
resp_id=$(curl -s http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Long analysis...", "background": true}' | jq -r .id)

# Poll until terminal: queued → in_progress → completed | incomplete | failed
curl -s "http://localhost:8000/v1/responses/${resp_id}"

# Optional: interrupt it (commits the partial output as a continuable turn)
curl -s -X POST "http://localhost:8000/v1/responses/${resp_id}/cancel"
```

Semantics:

- Requires `store: true` (the default); `stream: true` and `function_call_output`
  continuations are not supported with `background` yet.
- Terminal statuses mirror the connected paths: `completed` commits the turn
  exactly like a non-streaming request; cancel commits a continuable
  `incomplete` turn (`incomplete_details.reason: "user_cancelled"`); a backend
  failure leaves the turn uncommitted — the response id stays retrievable with
  `status: "failed"` and retrying the same `previous_response_id` reuses it.
- A paused `AskUserQuestion` surfaces as `status: "requires_action"`; answer it
  with a normal (non-background) `function_call_output` POST.
- One turn per session at a time (the session lock applies to background turns
  too); `BACKGROUND_RESPONSE_TIMEOUT_S` (default 3600) bounds a wedged turn.
- Results live in the in-memory session store: polling refreshes the session
  TTL, but a gateway restart drops unfetched results (the conversation itself
  rehydrates from the on-disk transcript).

## Terms

You must use your own upstream credentials. This project does not pool credentials, resell access, or bypass upstream authentication.

Oh My Gateway is an independent open-source project and is not affiliated with or endorsed by Anthropic, OpenAI, or the OpenCode authors.

## License

MIT
