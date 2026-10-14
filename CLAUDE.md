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
- `src/system_prompt.py` / `src/prompt_library.py` — the live base system prompt and its versioned
  library. Prompts keep immutable, numbered versions (author + change note, append-only; a legacy
  file without `versions` reads as v1); **deploying** points the live override at one
  `name@version` and appends to `data/prompt_deployments.jsonl` (deploy/rollback/reset/direct);
  saving never changes what new sessions receive. Admin API `/admin/api/prompt-library`
  (`src/routes/admin_prompt_library.py`: optimistic `base_version` / `expected_live` 409s, a
  throwaway `try` run in a temp workspace); `X-Admin-Actor` (percent-encoded) attributes admin
  front-end actions. Each session records the version it snapshotted in `base_prompt_ref`. The legacy
  `/api/prompts` + `/api/system-prompt` endpoints stay and route through the library (an upsert
  appends a version; a direct `PUT` is logged as an untracked override the studio can import).
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
  exactly `"*"` (fail closed: `true`/numbers/objects are rejected with a warning, and a tool the server
  itself marks `readOnlyHint: false` or `destructiveHint: true` is never overridden): `_configure_mcp_servers` strips it on every options build (the CLI never sees it) and the
  progress relay adds `readOnlyHint: true` to exactly those tools in the server's `tools/list`
  reply (also when a resumable server delivers it on a `Last-Event-ID` GET resume) — the relay's one byte rewrite, and only for HTTP servers routed through it (stdio or an
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
  rules, which the gateway does not set (#218), and in the image this repo's Dockerfile builds
  with the default (no-op) build hook there is no bubblewrap/socat, so with `sandbox.failIfUnavailable`
  unset the CLI only warns on stderr and runs Bash unsandboxed — but a private
  `GATEWAY_BUILD_INSTALL_SCRIPT` may install them, and then under Docker's default seccomp (userns
  denied) every sandboxed Bash call fails instead (`bwrap: No permissions to create new namespace`),
  while a profile that allows the namespaces and mounts bwrap needs lets the sandbox run. Which state
  holds in prod is not verifiable from this dev machine, and `command -v bwrap socat` (or the probe's
  PATH lines) only shows presence, so confirm in the prod container with the probe's
  `=> CLI OS sandbox can run` line rather than asserting any. In every case Bash is not isolated
  across users today. Don't describe the guard as covering Bash. Plan files are moved into
  the workspace by `--settings {"plansDirectory": ".claude/plans"}` (`CLAUDE_PLANS_DIRECTORY` may
  pick another relative dir; empty/absolute/`..` falls back to the default, never the shared one);
  `tests/test_cli_plans_directory.py` pins it against the bundled CLI.
- Read boundary (#234, opt-in `CLAUDE_BLOCK_READS_OUTSIDE_WORKSPACE`): the CLI's native
  `permissions.blockReadsOutsideWorkingDirectories` goes out through `--settings` (flag settings
  outrank the agent-writable workspace `.claude/settings.json`). The CLI refuses file-tool reads
  outright, but a Bash read it cannot clear is an **ask**, and the gateway's `can_use_tool` approves
  asks — so with the boundary on, execution modes run as `bypassPermissions`
  (`effective_permission_mode`, also on continuation and after an approved plan; `plan` stays) and
  the callback refuses Bash / `blocked_path` asks. Under `bypassPermissions` the read block's asks
  are the only Bash asks left, and they **still reach `can_use_tool`** despite the SDK's
  `CanUseToolShadowedWarning`. A script the agent writes and runs still reads anything (needs an
  OS boundary — see Landlock confinement next). `tests/test_cli_read_boundary.py` pins all of it
  against the bundled CLI.
- Landlock confinement (#241, `CLAUDE_ISOLATION_MODE=off|landlock|uid`; `uid` is the next gotcha):
  the OS boundary that closes the written-script read path (F1) the read boundary cannot.
  One operator knob picks the mode; `confine_enabled()` is true for the Landlock modes, an unknown
  value fails closed at startup. When on, `_get_cli_path` points `cli_path`
  at a fixed launcher (`docker/landlock_launcher.py`, root-owned) instead of the real CLI; the
  launcher starts isolated from its first instruction (`#!/usr/bin/env -S python3 -I` — a later
  re-exec guard is too late, user-site `.pth`/`PYTHON*` would already have run; the in-script guard
  stays only for an explicit `python3 launcher.py`; cwd is the agent workspace — never import from
  it), applies a Landlock ruleset, and execs the real CLI (operator `CLAUDE_CLI_PATH` or SDK bundle,
  resolved by `landlock.resolve_real_cli`). Landlock is inherited across `execve`, so Bash, a script
  the agent wrote, and stdio MCP are all confined at `open()`. The per-workspace ro/rw path lists
  ride `options.env` (`landlock.options_env`) and are built from the **same** `workspace_sandbox`
  helpers as the always-on `~/.claude` guard (`_shared_asset_roots`, `plugin_resource_roots`,
  `_writable_extra_roots`), plus the CLI's own config files read-only (`<cfg>/settings.json`,
  `settings.local.json`, the legacy `<cfg>/.config.json`, and `claude_config_paths.global_config_file()`
  = `$CLAUDE_CONFIG_DIR/.claude.json` else `$HOME/.claude.json`, never `<cfg>.parent` — so the
  confined CLI logs a harmless `Failed to save config with lock`). RW is only the workspace, the
  session's own `projects/<name>`, `<cfg>/session-env/<session id>` (pre-created 0700 when the id is
  known — never `session-env` itself: its siblings are other sessions' `$CLAUDE_ENV_FILE` scripts the
  CLI sources into Bash; a confined CLI cannot sweep it, so the gateway removes it when the session
  ends — delete, expiry, eviction, shutdown, stateless cleanup) and `/dev/shm` (POSIX semaphores — multiprocessing/joblib; in landlock mode a
  surface every same-uid session shares). **No shared cache is writable**: a session-writable
  `~/.npm`, uv cache or `plugins/data` is a cross-session plant channel (other sessions' `npx -y` /
  `uv run` / plugin hooks execute from them), so the launcher/broker point `npm_config_cache`,
  `NPM_CONFIG_CACHE` and `UV_CACHE_DIR` under `<cwd>/.cli-tmp` (per-workspace downloads: they count
  against `USER_WORKSPACE_QUOTA_MB`, and a stateless run starts cold every request), and
  `plugins/data` stays read-only with the gateway pre-creating `plugins/data/<id>` for each
  `installed_plugins.json` key on every confined spawn (`_ensure_plugin_data_dirs`): the CLI mkdirs
  `$CLAUDE_PLUGIN_DATA` before every plugin hook and **skips the hook on EACCES**; with the dir
  present the hook runs and only its writes there fail. The CLI's only knob,
  `CLAUDE_CODE_PLUGIN_CACHE_DIR`, moves the whole plugins root, so a per-session data dir needs
  per-user config dirs. `CLAUDE_ADD_DIRS` rides the #240 filter (`_operator_add_dir_roots`), never
  the raw strings. A confined script reaches what a file tool may plus `/dev/shm` and the read-only
  system roots — `/app` is never listed (code + `/app/data` creds denied by rule absence, not a ro
  rule). `/etc` and `/usr` stay ro (the CLI/libc need them; a passwd entry is required), so
  acceptance is "a path **outside the ruleset**", not `/etc/passwd`. **`/proc` stays ro too (the
  bundled CLI aborts without it), so in both modes a session can read every process's command line
  (`/proc/<pid>/cmdline`)**: other sessions' `--mcp-config` JSON (with `MCP_FORWARD_CONTEXT`
  `{{header:…}}` values), their system prompt (`Current user:`) and `--json-schema`. That is the argv
  exposure of `docs/security-process-isolation.md` §6.5, not closed yet: moving those into files is
  a follow-up. `environ`, `mem` and the fds of processes outside the domain stay denied. Every list entry leaves the gateway absolute and the launcher refuses
  a relative one (exit 70) and uses `os.getcwd()`, never `$PWD` — a relative `USER_WORKSPACES_DIR`
  once granted `<ws>/<ws>`. A rule target that is a regular file (e.g. the user `CLAUDE.md`) gets
  file-only rights — the dir-only `READ_DIR` bit is EINVAL for a file and would abort the whole
  build. On ABI ≥ 6 both modes add `LANDLOCK_SCOPE_SIGNAL | LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`: a
  confined process cannot signal (or reach an abstract socket of) anything outside its domain, so
  sessions cannot signal each other even in landlock mode; below ABI 6 there is no scoping.
  `TMPDIR`/`CLAUDE_CODE_TMPDIR` point at `<cwd>/.cli-tmp`
  (off the shared `/tmp`), which is **not** a Landlock rule: it rides beneath the granted cwd and is
  symlink-stripped first, because a rule on that agent-controlled path was a `ln -s / .cli-tmp`
  escape (`_add_rule` opens targets with `O_PATH`, following the link). Failing to make it is fatal
  (launcher exit 70, broker child exit 118), not an opaque CLI exit 1 later. The slash/skill preflight
  (`slash_commands._build_preflight_options`) is confined exactly like a session in that workspace
  (uid mode: through the broker as the request user's uid — `user` is threaded from the responses
  route and `create_client`; a no-cwd preflight stays landlock-shaped in a private per-process
  `mkdtemp` dir) and in **every** mode runs with `--strict-mcp-config` (zero servers),
  `--settings {"disableAllHooks": true, "apiKeyHelper": "", …}` (every startup settings command —
  `apiKeyHelper`, `proxyAuthHelper`, `awsCredentialExport`, `awsAuthRefresh`, `gcpAuthRefresh`,
  `otelHeadersHelper` — blanked; a flag value outranks the workspace's) and the same secret blanks a
  session child gets (`client.child_secret_env`: `ADMIN_API_KEY`, `API_KEY`, the
  `SYSINFO_CHILD_ENV_MASK` names, other backends' and auth-mode credentials, the ticket secret): it
  only reads the catalog, and with plain options the CLI ran workspace/plugin hooks, started
  `.mcp.json` servers and ran a workspace `apiKeyHelper` that saw `ADMIN_API_KEY`
  (`tests/test_cli_slash_preflight.py` pins identical catalogs, no side effects, a helper that does
  run seeing no gateway secret, and the uid-mode preflight through the broker). The image ships
  launcher+broker+`_landlock_rules` at `/usr/local/bin` (root-owned, outside `/app` so
  `chown -R app /app` can't make them agent-writable) and sets `GATEWAY_LANDLOCK_LAUNCHER`. This is
  the no-privilege **interim** of `docs/security-process-isolation.md` §3: no uid switch, so no
  cross-session DAC isolation (that is `uid` mode). Fail-closed:
  `config_check._check_landlock_confinement` refuses startup if the launcher/real CLI cannot resolve,
  Landlock cannot actually be **enforced** (a forked probe builds+`restrict_self`es a real ruleset,
  not just an ABI query — a seccomp filter blocking `add_rule`/`restrict_self` is caught), the uid
  band is unusable, or the mode is a typo (an invalid `CLAUDE_ISOLATION_MODE` is refused even under
  `SKIP_CONFIG_CHECK`); the CLI's own bwrap sandbox is forced off (`_configure_sandbox`) because it
  cannot start inside a Landlock domain, and `_check_isolation_sandbox_conflict` warns when that
  overrides an explicit `CLAUDE_SANDBOX_ENABLED=true` or a managed policy enables the sandbox (the
  gateway's `--settings` cannot outrank it; with bwrap present every sandboxed Bash call would fail).
  Startup installs `GATEWAY_REAL_CLI` + `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK=1` in
  the process env (`landlock.install_process_env`, for the SDK's option-less `-v` spawn). Unit:
  `tests/test_landlock_confine.py` (launcher + wiring, no bundled CLI) and
  `tests/test_landlock_launcher_hardening.py`; the bundled-CLI closure + workspace-work proof is
  `tests/test_cli_read_boundary.py::TestLandlockConfinement` (the kept `test_known_gap_*` is its
  non-vacuous control), and `tests/test_cli_landlock_hooks.py` proves plugin/SessionStart hooks
  still run confined. Bundled-CLI experiments must use an explicit env dict (HOME and
  `CLAUDE_CONFIG_DIR` under a tmp dir, `ANTHROPIC_BASE_URL` → `tests/fixtures/fake_anthropic_api.py`):
  the Bash tool's zsh does not word-split `$VARS`, and an `env -i $ENVS claude …` once ran with the
  real HOME and wrote the developer's `~/.claude`.
- `uid` mode (#241 PR-C) adds per-user uid + DAC isolation on top of the Landlock ruleset.
  The gateway body stays unprivileged; a resident **broker** (`docker/isolation_broker.py`, holds
  only SETUID/SETGID, dedicated uid `CLAUDE_ISOLATION_BROKER_UID` default 19999 — outside the default
  band 20000–29999) forks a per-spawn **supervisor** per CLI, which applies the **same**
  `docker/_landlock_rules.py` ruleset to the CLI child *after* the switch. The launcher's uid branch
  hands the broker the SDK's fds (`SCM_RIGHTS`) + a signed single-use ticket
  (`uid_isolation.mint_ticket`, HMAC + nonce + exp; `GATEWAY_ISOLATION_TICKET_SECRET` is
  entrypoint-generated and shared with gateway+broker; the gateway moves it out of its own
  `os.environ` at startup (`capture_ticket_secret`, before any spawn — the frozen codex/opencode
  clients copy `os.environ`) and still blanks it per spawn). `create_client` re-mints the ticket
  right before `connect()` (120 s TTL; the skills preflight awaits a global lock in between). The
  resident loop is single-threaded `select.poll` (it forks): `SO_PEERCRED` pinned to
  `GATEWAY_ISOLATION_PEER_UID` before reading a byte, ≤ 32 handshakes in flight, a 10 s total
  deadline each, an 8 MiB header cap, a strict `{ticket: str, argv: [str], env: {str: str}}` schema,
  and any per-connection exception or fork failure answers REFUSED and keeps serving (one malformed
  header used to kill the only broker). It verifies the ticket **in the resident parent** (nonce→exp
  store, pruned at expiry, so single use is authoritative) and `_authorize` keeps uid **and** gid in
  the band and outside {0, APP_UID, APP_GID, the broker's own uid/euid/gid/egid}; relative
  workspace/ro/rw entries are refused. The only same-uid path is the argv-only test flag
  `--test-same-uid` (the entrypoint never passes it), which lets the whole protocol (fd passing,
  supervisor tree, Landlock, exit mirroring, ticket refusal/parity, teardown) run unprivileged in
  `tests/test_isolation_broker.py`, `tests/test_landlock_launcher_hardening.py`,
  `tests/test_entrypoint_broker.py` and `tests/test_uid_isolation.py`. **Teardown** never depends on
  a cross-uid `kill()` (a CAP_KILL-less broker gets EPERM): the supervisor runs `setgroups([])`,
  `setresgid(G,G,G)`, `setresuid(broker, U, broker)` and drops all caps, so it can signal the
  uid-U tree while uid-U processes can neither signal nor ptrace it; the CLI child completes
  `(U,U,U)` itself and asserts it (exit 115 otherwise). The launcher link is the control channel:
  relayed SIGTERM/SIGINT go to the CLI's group; on EOF the group gets SIGTERM, then SIGKILL 5 s after
  the first SIGTERM; once the CLI exits, `_sweep` (the supervisor is a subreaper) SIGKILLs every
  remaining descendant and loops until none is left — the CLI starts each Bash command detached in
  its own session, so killing only the CLI's group leaked `cmd &`/setsid jobs — never `kill(-1)` (a
  named user's other sessions share the uid), and only then reports the status. If a supervisor dies
  abnormally, the resident broker (also a subreaper) kills what it left as their own uid
  (`_sweep_orphans`). Gap: a direct launcher SIGKILL with no relayed SIGTERM (the `agent_messages`
  wedge path) leaves a CLI that ignores SIGTERM up to 5 s, so stateless cleanup can race it. A local
  container run measured under 1 s, background jobs included. **Wire format** after the
  `=I` length + JSON header (3 fds on its first byte): fixed `=Ii` `(kind, value)` records —
  launcher→broker `(1, signum)` relay; broker→launcher `(1, code)` EXITED, `(2, signum)` SIGNALED,
  `(3, 0)` REFUSED. The launcher mirrors exactly (a 128+N single int once turned exit(130) into -2 and
  exit(148) into a self-SIGTSTP); a stop/ignore-default signal exits 128+N instead of being
  self-raised; REFUSED/invalid/no status exit 70. The broker binds a `0660` socket with the gateway's
  gid (`CLAUDE_ISOLATION_BROKER_GID` defaults to `APP_GID`) in a broker-owned `0750` dir that the
  entrypoint creates fresh (or adopts only if already the broker's), behind a root-owned
  non-writable parent chain, through an `O_NOFOLLOW` fd (a socket under `/tmp` is refused, not
  chmodded); the entrypoint then waits for the broker to accept before exec'ing the gateway, and
  `install_process_env` waits again (`CLAUDE_ISOLATION_BROKER_WAIT_SECONDS`, default 10, capped at
  300, non-finite → default; one parser on both sides, pinned equal by
  `test_wait_knob_parses_like_the_gateway` — a broker that never comes up delays startup ~2× before
  the gateway fails closed). The
  broker execs the CLI with the launcher-passed env (the SDK's child env) — not the broker's —
  filtered + plumbing-stripped, `PWD` = workspace. Anonymous/stateless runs use a **recycled
  in-memory ephemeral uid band** (`_EphemeralPool`, top `CLAUDE_ISOLATION_EPHEMERAL_UIDS` of the
  band; an empty one is a startup error) so they cannot grow the persisted registry or exhaust it;
  named users get durable uids. The registry fails **closed**: a corrupt/unreadable
  `isolation_uids.json` refuses uid-mode startup (`_check_uid_registry`) instead of loading empty
  and reassigning uids; entries it cannot honor (out of band, reserved, shared — e.g. after
  narrowing the band or growing the ephemeral slice) are kept on disk, refuse startup too, and their
  uids are excluded from the ephemeral pool (else an anonymous run would get a named user's files);
  writes are fsync'd 0600, and a failing directory fsync after the replace keeps the grant.
  `effective_mode()` self-detects at startup: Landlock ABI≥2, the broker reachable, and the
  **startup probe** passed. Once the broker answers, `install_process_env` runs
  `_probe_uid_sessions`:
  - it spawns the real CLI `--version` through the launcher and broker, as an ephemeral session
    uid, in a fresh anonymous workspace from the workspace manager;
  - it checks with mode bits (no ACLs) that this uid can reach the config dir and create its
    `projects/<name>`.

  Unavailable uid is a hard `check_config` error naming the reason, **unless**
  `CLAUDE_ISOLATION_FALLBACK=landlock`. The probe is skipped when the secret, the bands or the
  registry are broken, so their own errors stand. In uid mode, `verify()` also runs in such a
  workspace and names a launcher/broker refusal (`landlock.describe_launcher_exit`) instead of
  printing npm install hints.

  **With the shipped image the probe fails and uid mode refuses to start.** A local container run
  (2026-10-14) served no turn: the session uid cannot write the app-owned 0755 workspace, so the CLI
  died on `.cli-tmp`, while startup passed and every request got a 503. `/home/app` is 0700, which
  hides the shared `~/.claude` assets, `session-env/<id>` and `plugins/data`. The image has no
  passwd entries for the broker uid or the band. uid mode becomes usable only when per-user
  workspace ownership and container provisioning land; `landlock` is the boundary to run today.
  That run did verify the privileged layout: broker caps exactly SETUID|SETGID with securebits
  locked, gateway CapEff 0, supervisor `(broker, U, broker)` with caps 0, CLI `(U,U,U)`, and teardown.
  Re-run `docs/isolation-uid-verification.md` in the deployment container before enabling uid in
  prod. Remaining
  hardening (seccomp arg filter on set*id, bounding-set drop, per-user workspace ownership + broker
  cleanup) is a container/follow-up step. `docker/{landlock_launcher,isolation_broker,_landlock_rules}.py`
  never import `/app/src` (session-writable + privileged); the broker's ticket-verify is duplicated
  from `uid_isolation` and kept in lockstep by `test_isolation_broker.py::TestTicketVerify`
  (valid/tampered/expired/replayed cross-check + identical `ValueError` refusals).
- Host/secret redaction (`src/backends/claude/sysinfo_redaction.py`, on by default,
  `SYSINFO_REDACTION` / admin key `sysinfo_redaction_enabled`): a `PostToolUse` hook
  (`updatedToolOutput`) replaces this host's name/addresses/container id/MACs, secret env values
  and private IP ranges in **successful** tool results before the model sees them, and
  `_redact_stream` filters every chunk leaving `run_completion_with_client` /
  `receive_response_from_client` (plus the idle outbox) — streamed deltas through a carry that
  never emits a value split across chunks (it keeps at least the longest literal/built-in match of a
  whitespace-free run; any custom `SYSINFO_REDACT_PATTERNS` regex makes it hold each text block
  whole, since an arbitrary regex has no bound and may span whitespace). **A failed tool call is not covered**: the CLI routes
  it through `PostToolUseFailure`, which cannot rewrite the result, so the model sees that output
  raw (only the client-facing copy is filtered). The CLI also spills oversized results to a file
  with the RAW output; reading it back is a tool call and comes back redacted. The boundary that
  holds regardless is a neutral container hostname. Gateway-only secrets (`ADMIN_API_KEY`,
  `API_KEY`, `SYSINFO_CHILD_ENV_MASK`) are blanked in the child env. `tests/conftest.py` turns it
  off by default (its table is built from the machine); `tests/test_cli_sysinfo_redaction.py`
  pins all of the above against the bundled CLI, including the failure-path limit.
- The first step of `lifespan` marks the serving process non-dumpable
  (`src/process_hardening.py`, `prctl(PR_SET_DUMPABLE, 0)`; `GATEWAY_NON_DUMPABLE=false` opts
  out, blank stays on, failure only logs): its `/proc/<pid>/` entries turn root-owned, so a
  same-uid process (a session's Bash, hook or MCP server) gets EACCES on its `environ`, `mem`
  and `fd/`. What that adds is narrow. The SDK builds the CLI child env as
  `{**os.environ, **options.env}`; for session CLI children and the slash/skill preflight the
  gateway first blanks `ADMIN_API_KEY`, `API_KEY` and the `SYSINFO_CHILD_ENV_MASK` names
  (`child_env_mask()`, only while `SYSINFO_REDACTION` is on) and always blanks `OPENAI_API_KEY`
  (plus `CLAUDE_CODE_OAUTH_TOKEN` in api-key auth) and the uid ticket secret — one list,
  `client.child_secret_env()`, used by `_configure_sdk_env` and `_build_preflight_options` — in
  `options.env` — never by popping `os.environ` around `connect()`: the SDK copies `os.environ`
  only after awaiting its `claude -v` check, so an overlapping session creation used to hand the
  popped values to another session's CLI (`tests/test_sdk_env_race.py`; a blank token reads as
  unset, `tests/test_cli_blank_oauth_token.py`). Those values then sit only in the gateway's
  environ: this closes that `/proc/<gateway>/environ` bypass and keeps the gateway's memory and
  fds out of reach. Not every spawn path goes through that scrubbing yet (#230 follow-up). It
  hides no inherited secret: `ANTHROPIC_AUTH_TOKEN` and every other unscrubbed variable sits in
  each session's own env, and every exec'd CLI child is dumpable again (`execve` resets it).
  `SYSINFO_CHILD_ENV_MASK` adds gateway-only names to the session-child mask. It starts at the
  prctl (fds opened before it are not revoked) and skips a `uvicorn --workers`/`--reload`
  supervisor. Side effects: no core dumps; py-spy/gdb attach needs CAP_SYS_PTRACE (host root or
  `cap_add: [SYS_PTRACE]`; a `docker exec` root shell has none by default); the process loses its
  own owner-only `/proc/self` files (`environ`, `io`, `auxv`, …) and writes to its own
  `oom_score_adj` (nothing uses them; `/proc/self/fd` and prometheus' process collector still
  work). `tests/conftest.py` turns it off; `tests/test_process_hardening.py` pins the EACCES on a
  child.

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