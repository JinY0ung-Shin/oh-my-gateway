# CLAUDE.md

Agent gateway exposing the Claude Agent SDK through the OpenAI-compatible `/v1/responses` API, plus
a stateless Claude SDK event stream at `/v1/agents/messages` (FastAPI, Python 3.10+, uv). The
OpenCode and Codex backends are **stale**: frozen since 2026-07 and unmaintained (see Testing).

## Commands

```bash
uv sync                                            # install deps (incl. dev group)
uv run uvicorn src.main:app --reload --port 8000   # dev server
uv run pytest                                      # tests (e2e excluded via addopts; full suite ~20s)
uv run pytest -m integration                       # subprocess integration tests
uv run pytest --cov=src                            # with coverage
```

- `ADMIN_API_KEY` must be set or the server fails fast at startup (`src/main.py`). `ANTHROPIC_AUTH_TOKEN` for the Claude backend; optional `API_KEY` bearer-protects public endpoints. See `.env.example` for the full list.
- `src/__init__.py` loads `.env` at package import — set `GATEWAY_SKIP_DOTENV=1` on ad-hoc
  `uv run python` snippets that import `src`, or the local `.env` leaks in (conftest already sets it).
- Backends are enabled via `BACKENDS=claude,opencode,codex` (claude is the default). `opencode` and
  `codex` are stale (frozen 2026-07): enabling them logs a startup warning and they may break without
  notice. The maintained **app-server adapter** (`src/backends/appserver`, issue #173) is **opt-in
  only** — set `CODEX_BACKEND=appserver` to route `BACKENDS=codex` to it. It is NOT the default: the
  #173 traffic cutover is a hard release gate (production two-user filesystem isolation + zero-egress
  proof) and the default flip belongs to a separate small PR once that gate passes.

## Architecture

- `src/main.py` — FastAPI app assembly, startup validation.
- `src/routes/` — `responses.py` (OpenAI-compatible streaming + non-streaming), `agent_messages.py`
  (stateless Claude SDK event stream), `admin.py`, `sessions.py`, `general.py`.
- `src/agent_message_models.py` — strict caller-owned full-history request contract for
  `/v1/agents/messages`.
- `src/backends/` — `base.py` defines the `BackendClient`/`SessionHandle` protocols and `BackendRegistry`; `claude/` implements them. `codex/` (JSON-RPC to a local `codex app-server`) and `opencode/` (managed subprocess / external HTTP modes) are frozen stale code — do not extend them.
- `src/backends/appserver/` — the direct `codex app-server` stdio transport (C0 core, #163/#170) plus the Codex compatibility adapter on top of it (#173). `transport.py`: one reader per process, id→waiter routing, generation-bound `PendingInteraction`, fail-closed unsupported server requests, EOF/parse/death fanout, process-group teardown; topology-agnostic (no session↔process placement, pooling, or resume policy — gated on #165). `events.py` (`TurnMapper`): maps native Codex thread/turn/item notifications into the canonical `/v1/responses` chunk contract so the same ChatDRAGON reducer renders Claude and Codex — conservative (translate only where semantics match; never fake absent fields). `client.py` (`AppServerCodexClient`/`AppServerSessionClient`): `BackendClient`/`SessionHandle` with 1-process-per-handle placement, `create_client`/`run_completion_with_client`/`interrupt_client`; records `session.codex_thread_id` as durable **only after a turn completes** (#165). Modules: `events.py` (`TurnMapper`, basic events + reasoning + subagent→`task_*` mapping), `interactions.py` (human-interaction bridge → AskUserQuestion UX), `subagents.py` (child-thread normalization), `isolation.py` (per-user `CODEX_HOME` + secret stripping), `policy.py` (canonical capability → sandbox/approval, fail-closed). `BACKENDS=codex` runs this adapter via `discover_backends` (rollback: `CODEX_BACKEND=frozen`); the adapter never reaches into the frozen `src/backends/codex/`. Acceptance corpus: `tests/test_appserver_{transport,events,client,interactions,subagents,isolation,cutover}.py` against `tests/fixtures/fake_app_server.py` + `env_probe_app_server.py`.
- `src/sanitizer/` — stream sanitization + OpenAI-format bridge.
- `src/agent_catalog.py` / `src/mcp_health.py` — client-facing read models behind
  `GET /v1/agent-resources` (skills/subagents across plugin + workspace + user scope, described
  from frontmatter) and `GET /v1/mcp/health` (cached, poll-safe reachability snapshot). Both are
  read-only and never raise; do not turn them into enforcement paths.
- `src/session_manager.py` / `src/workspace_manager.py` — `/v1/responses` continuation state and
  workspace isolation. Stateless agent-message runs use request-scoped temporary workspaces and never
  enter the session manager.
- Admin dashboard is server-rendered from `src/admin_*.py` modules (no frontend build).

## Code Style

- Write new code in black-88 style, even though `pyproject.toml` carries a `[tool.ruff]` section with
  line-length 100 — but existing files are not formatter-clean at either width (`black --check` flags
  `client.py`, `responses.py`, `agent_messages.py`, …), so never run black or `ruff format` over an
  existing file; match surrounding style.
- Gateway philosophy: pass SDK behavior through rather than hiding upstream breaking changes. The one
  deliberate adapter is the versioned, endpoint-local mapper in `src/routes/agent_messages.py`, because
  the Python SDK objects are not wire-compatible with Noah's JavaScript SDK handlers. Never move that
  mapper into the shared Responses conversion path.

## Gotchas

- The CLI renames tools across builds (`Task` → `Agent`, `KillShell` → `TaskStop`, …).
  Permission *rules* pass through that rename map, but **PreToolUse hook matchers fire on
  the tool's real name** — bind hooks to every spelling (`SUBAGENT_TOOL_NAMES`) or they
  become silent no-ops on a new CLI.
- A turn cut off by the agentic limit is reported as `response.incomplete` with
  `incomplete_details.reason = "max_turns"`, not `response.failed`: the partial text is
  real work. `DEFAULT_MAX_TURNS` (40) has to accommodate subagent orchestration.
- The pinned Python SDK **drops** the CLI's `tool_progress` frames (unknown message type →
  `None`), so a long MCP call looks like a wedged turn to everything downstream.
  `src/backends/claude/sdk_client.py` subclasses `ClaudeSDKClient` to surface them; the stream
  loop turns them (and its own keepalive-tick heartbeat for in-flight tools) into
  `response.tool_progress`. The gateway owns the effective MCP ceiling: unset
  `MCP_TOOL_TIMEOUT` is injected as 600000 ms into Claude children, and oversized
  per-server MCP `timeout` values are clamped to that ceiling. `TOOL_STALL_TIMEOUT`
  defaults above the larger effective MCP/Bash watchdog by 60 s. The required order is
  `watchdog/1000 < TOOL_STALL_TIMEOUT < ACTIVE_TURN_MAX_AGE`; either inversion is a
  startup `ConfigIssue(error)` and `run_startup_config_check()` refuses to start unless
  the operator explicitly sets `SKIP_CONFIG_CHECK=true`. Delete the subclass the day the
  SDK grows its own type.
- The bundled CLI opts in to MCP progress (`tools/call` carries `_meta.progressToken` and
  `_meta["claudecode/toolUseId"]`) but in SDK mode **never writes `notifications/progress` to its
  output** — its `tool_progress` frames cover Bash/REPL/heartbeat/agent-retry only. So HTTP
  (streamable-http) MCP servers are routed through `src/mcp_progress_relay.py`
  (`/internal/mcp-relay/<relay_id>/<server>`, same-host callers only, bound to the session's
  configured URLs, bytes relayed verbatim): it reads the progress off the wire and the turn loop
  emits it as `response.tool_progress` with `source: "mcp"`, `message`, `progress`, `total`.
  `MCP_PROGRESS_RELAY=false` sends servers direct again; `MCP_RELAY_BASE_URL` overrides the
  self-address the CLI child dials. **The relay must never cost the MCP call itself:** the child
  dials the served port on loopback (never `scope["server"]`'s host — behind Docker port
  publishing that is the container IP, the child's call then came from it too and the old
  loopback-only check 403'd every HTTP MCP server), loopback joins the child's `NO_PROXY`, and a
  once-per-base self-probe (`reachable()`) keeps servers direct when the relay is unreachable. `tests/test_cli_mcp_progress.py` pins both halves against the
  bundled CLI — when a CLI bump starts forwarding the text itself, prefer that and retire the relay.
- The CLI runs the MCP tools of one parallel batch **sequentially unless every tool in it declares
  `annotations.readOnlyHint: true`** (one marked is not enough), so an unannotated 2-minute research
  tool makes a 300 ms lookup in the same batch wait for it (ChatDRAGON #471). For servers we don't
  own, an MCP server entry may carry the gateway-only key `"readOnlyTools": [...tool names]` or
  `"*"`: `_configure_mcp_servers` strips it on every options build (the CLI never sees it) and the
  progress relay adds `readOnlyHint: true` to exactly those tools in the server's `tools/list`
  reply — the relay's one byte rewrite, and only for HTTP servers routed through it (stdio or an
  unreachable relay logs "not applied"). Only list tools that are truly side-effect free.
  `tests/test_cli_mcp_readonly.py` pins sequential / concurrent / one-marked against the bundled CLI.
- The SDK frames CLI stdout one JSON message at a time and **aborts the reader** — the whole
  turn fails with `sdk_error` — when one message exceeds `max_buffer_size`. A tool result is one
  message, so an MCP tool returning inline base64 images tripped the SDK's 1 MiB default (#183).
  The gateway owns that limit too: `_get_max_buffer_size` always installs
  `GATEWAY_MAX_BUFFER_SIZE_DEFAULT` (16 MiB) unless `CLAUDE_MAX_BUFFER_SIZE` overrides it, the
  slash-command preflight shares it, `describe_sdk_stream_error` rewrites the fatal
  `CLIJSONDecodeError` into actionable text (sizes + both remedies), and `_check_sdk_buffer`
  warns at startup on invalid or ≤ 1 MiB values. There is no gateway-side truncation: the frame
  is rejected inside the SDK transport before any hook or handler can see it, so a result above
  the limit still kills the turn (the pre-framing tool-result budget is #185). Unit caveat: the
  pinned SDK counts decoded text **characters** (`len(str)` on a `TextReceiveStream`), not UTF-8
  bytes, despite saying "bytes" (upstream #1165) — `tests/test_sdk_buffer_semantics.py` pins
  this against the real transport reader so an SDK upgrade that flips the unit fails loudly;
  update the docs/error text together with the pin when it does.
- The bundled CLI (2.1.224+) ships cross-session messaging: a peer-inbox socket in every child plus
  `ListAgents` / cross-session `SendMessage`. Peers are discovered through the config dir, which all
  gateway children share (one HOME), so any user's agent could inject turns into another user's
  live session. `src/constants.py` installs the undocumented gate `CLAUDE_CODE_HARBOR_KITE=0` into
  the process env (every spawn point inherits it; an operator opt-in is a startup warning), and
  `tests/test_cli_cross_session_messaging.py` pins it against the bundled CLI in both directions —
  when an SDK bump breaks that test, find the new gate before shipping. Teammate and subagent
  `SendMessage` are not gated by it.
- Agent teams default ON: `src/constants.py` installs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
  when unset or blank (`=0` opts out; the CLI reads only 1/true/yes/on as on). In SDK mode CLI
  2.1.283 never initializes a session team, so no `in_process_teammate` ever starts: the gate
  only gives the Agent tool `name` — named `local_agent`s that `SendMessage` can address and
  resume, reported as `active_tasks[].name` by pending-events. `tests/test_cli_task_identity.py`
  pins the gate parse and the spawn/resume ids.
- The CLI expands `@<path>` file mentions and inlines the file upstream — outside the workspace too,
  and the workspace sandbox hook never sees it (it is not a tool call). Covered: every non-slash
  turn goes out with `client_composed: true` (the per-message field SDK `verbatim_prompts` sets;
  CLI ≥ 2.1.248 — an older `CLAUDE_CLI_PATH` silently ignores it), and any `@` in slash-command
  arguments (`validate_prompt`, 400 `unsupported_argument`) or in the model's Skill-tool
  `skill`/`args` (`_make_skill_allow_hook` denies) is refused — no regex, because the CLI's mention
  grammar (CJK punctuation / U+FEFF prefixes, quoting, `@@`) out-ran one. **Not covered yet:**
  mentions inside a skill's or command's own body are still expanded by the CLI (#215). Keep the
  SDK-wide `verbatim_prompts` option off: its stamp would override the per-turn slash exception.
- SDK 0.2.129+ raises `ValueError` from `connect()` for skill names with parentheses, commas,
  wildcards, control characters or a leading `/`. `src/backends/claude/skill_names.py` mirrors
  those rules (parity-tested against the SDK's private validator): a request `allowed_tools` rule
  fails as 400 `invalid_skill_rule` (`Skill(*)` means allow-all), and a catalog-derived name is
  dropped with a warning, so a skill directory named `weird (v2)` cannot break every session.
- A resumed session reuses the system prompt recorded on its first request (CLI 2.1.267
  `--system-prompt-snapshot`, default on), so the resume path's `system_prompt=None` keeps the
  original `instructions`; admin base-prompt edits reach only new sessions.
- Every Claude child shares one HOME, so `~/.claude` is shared across users. The **always-on**
  `make_claude_home_guard_hook` (installed for every session, independent of the opt-in workspace
  sandbox and `WORKSPACE_SANDBOX_ALLOW_OUTSIDE`, and effective under `bypassPermissions`) allows only
  the session's own `projects/<name>` entry plus read-only shared assets (skills, plugins, agents,
  commands, output-styles, `CLAUDE.md`) — never other users' `plans/` or transcripts. `<name>` is
  the CLI's exact naming (`_project_dir_name`: 200 UTF-16 units + `-<base36 hash>` beyond that),
  pinned in `tests/test_cli_project_dir_name.py`; never grant by prefix. That guarantee holds for the
  file tools only: for **Bash the guard is static defense in depth, not a boundary** (shell-local vars,
  `cd` + relative paths, command substitution and Bash writes to shared assets get through). Bash is
  not isolated across users today: the CLI's OS bash sandbox only restricts reads via `Read` deny
  rules, which the gateway does not set (#218). Don't describe the guard as covering Bash. Plan files are
  moved into
  the workspace by `--settings {"plansDirectory": ".claude/plans"}` (`CLAUDE_PLANS_DIRECTORY` may
  pick another relative dir; empty/absolute/`..` falls back to the default, never the shared one);
  `tests/test_cli_plans_directory.py` pins it against the bundled CLI.

## API Compatibility Boundaries

- `/v1/responses` owns OpenAI response semantics, `previous_response_id`, cancellation, and stored
  continuation state. Preserve its existing conversion, streaming, and session behavior.
- `/v1/agents/messages` is Claude-only and stateless: the caller sends complete text history, every call
  creates a fresh SDK client/workspace, and the stream declares `claude-agent-sdk-message-v1` before
  normalized `sdk_message` events. Do not accept or expose continuation/session IDs.
- Noah consumes these envelopes through the same `dispatchSdkMessage` used for its local SDK runs. A
  schema or SDK-event change must be coordinated with the Noah `avatar-chat` repository; do not fork a
  second Noah-side event handler. The mapper is fail-closed on purpose: system envelopes pass only
  for subtypes in `_SYSTEM_SUBTYPES` (the set CLI 2.1.220 emitted) and new SDK fields such as
  `origin` are stripped, so a CLI bump cannot add system envelopes or that field — opening either is
  such a change. Other envelopes still pass unknown data keys through (e.g. `tool_progress.data`).
- Keep endpoint-specific partial messages, secret/path redaction, tool-result projection,
  `AskUserQuestion` denial, disconnect, and transcript/artifact cleanup isolated from `/v1/responses`.

## Testing

- `pytest-asyncio` uses `asyncio_mode = "auto"`; do not add `@pytest.mark.asyncio` unless a test specifically needs it.
- Mock SDK calls in tests and prefer the shared fixtures in `tests/conftest.py`.
- Markers: `integration` (real subprocess with mock binary), `slow`, `e2e` (needs live server + credentials; excluded by default).
- OpenCode/Codex are stale (frozen 2026-07): `tests/conftest.py` skips collection of their dedicated
  test files and deselects every test whose id mentions `opencode`/`codex` (~490 tests total; a
  default run reports only ~130 deselected — the dedicated stale files never collect at all).
  `RUN_STALE_BACKEND_TESTS=1` restores them. When a shared-code change breaks stale backend code or
  its tests, do not fix the backend — leave it frozen.
- `claude-agent-sdk` is pinned exactly (`==0.2.160`, bundled CLI 2.1.283); upgrades are deliberate,
  gap-analyzed events — do not bump casually. `tests/fixtures/fake_anthropic_api.py` drives the real
  bundled CLI at zero cost for pins like the cross-session and verbatim tests.
- Changes to the stateless mapper must pass `uv run pytest tests/test_agent_messages.py -q` and the full
  gateway suite. If the schema/event shape changes, also run Noah's `tests/external-agent.test.ts`.