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
  ride `options.env` (`landlock.options_env`, JSON arrays: a granted path may contain `:`) and are
  built from the guard's shared asset names (skills, plugins, agents, commands, output-styles,
  `CLAUDE.md`) plus the Landlock-only `rules`, `hooks` and `workflows` (the CLI loads user rules and
  runs user hook scripts from them; the file-tool guard still denies them), `plugin_resource_roots()`
  and the CLI's own config files read-only (`<cfg>/settings.json`, `settings.local.json`, the legacy
  `<cfg>/.config.json`, and `claude_config_paths.global_config_file()` = `$CLAUDE_CONFIG_DIR/.claude.json`
  else `$HOME/.claude.json`, never `<cfg>.parent` — so the confined CLI logs a harmless
  `Failed to save config with lock`). **Every entry goes through `_GrantFilter`**: it is resolved,
  the resolved path is what gets granted, and it is refused (warned once) when it is or contains the
  config dir, `$HOME`, the workspace base, the app root (`/app`) or the temp dir, lies inside the
  workspace base, `/app`, the temp dir, `/run`, `/var/run`, `/dev`, `/sys`, `/root` or `/var/tmp`,
  or lies inside the config dir or `$HOME` outside the shared subtrees and the global config file.
  Refusals for the workspace base, `/app` and the system roots beat every exemption, and roots that
  are the same directory apply the strictest rule (`USER_WORKSPACES_DIR=$HOME` still refuses another
  user's workspace); only an operator `CLAUDE_PLUGIN_CLONE_ROOT` exempts paths under `$HOME` (the
  default `<cfg>/plugin-marketplaces` is just a shared subtree name, so a planted
  `plugin-marketplaces -> ~/.ssh` grants nothing); a file grant must be a single-link regular file;
  `HOME=/` fails closed. The workspace itself goes through `_confined_workspace`: a
  workspace-manager workspace must resolve to exactly `realpath(base)/<rel>` and a gateway-owned
  preflight dir must be a real dir the gateway owns, else `WorkspaceRefused` refuses the spawn; the
  resolved workspace is the first rw entry and the ticket's workspace, and the launcher (exit 70) /
  broker child (exit 113) refuse a spawn whose kernel cwd is not exactly it. (A symlinked base
  passes, but the CLI names its transcript dir after the resolved cwd while the gateway's
  transcript lookup, `session_manager._encode_cwd`, uses the lexical workspace: a re-created client
  then starts fresh instead of resuming, and rehydrate 404s — pre-existing.) The launcher and broker
  open every gateway-provided entry without following any symlink (a component walk with
  `O_NOFOLLOW`) and refuse a hard-linked file target, so a swap after the filter is not granted; the
  system roots, `/dev` files, `/dev/shm`, the real-CLI dir and the shell wrapper keep a following open
  (merged-usr `/lib` links). `check_config` refuses a real-CLI dir that is or contains `/`, `$HOME`,
  the config dir, the workspace base, `/app`, the temp dir or a system root, or lies in the
  workspace base, and a workspace base, config dir, `$HOME` or app root under a root the launcher
  grants every session read-only (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/opt`, `/proc`
  — e.g. `USER_WORKSPACES_DIR=/opt/ws`) (the launcher/broker re-check
  `/` and holding the workspace). So a planted link (`agents -> .`, `settings.local.json -> /`, a
  workspace dir `-> ~/.claude`) or a broad registry/env root (`installLocation: /`,
  `CLAUDE_PLUGIN_CLONE_ROOT=/data` holding the workspace base) cannot widen every session's grant,
  while a link to a safe target (`skills -> /opt/shared-skills`) still works. It is a deny-list of
  the gateway's own state, not of every secret on the host, and a hard link an unconfined writer
  plants *inside* a granted dir (e.g. `skills/x.jsonl` → another user's transcript) is not detected.
  RW is only the workspace, the session's own `projects/<name>` (one, for the resolved cwd the CLI
  names it after; a planted link there is refused), `<cfg>/session-env/<session id>` and
  `<cfg>/tasks/<session id>` (the CLI's Task list) —
  pre-created 0700 by the gateway in landlock mode, created by the broker child as the session uid in
  uid mode, never `session-env` or `tasks` themselves: their siblings are other sessions'
  `$CLAUDE_ENV_FILE` scripts the CLI sources into Bash and other sessions' task lists. A confined CLI
  cannot sweep them, so the gateway removes both once the session is **finished** — explicit delete,
  any teardown of an anonymous session, stateless cleanup — but not when a named session expires, is
  evicted or the gateway shuts down (a continuation resumes the same id, and SessionStart then runs
  with `source=resume` only), and never while a live session holds the id
  (`SessionManager._release_session_dirs`); the dirs of named sessions never resumed stay behind,
  and in uid mode the app-uid gateway cannot remove what the session uid wrote there (the same
  broker-cleanup follow-up as its transcripts). While a session's client is starting
  (`begin_client_start` → `create_client` → `finish_client_start`), a teardown defers removing its
  workspace, lease and dirs until that client is stopped, and a session deleted meanwhile answers
  409 `session_ended`; every session-manager teardown SIGKILLs a CLI that outlived its capped
  disconnect (`_stop_client`) before anything is removed. A new session whose first turn is refused
  (e.g. a slash 400) is deleted at once.
  RW also covers `/dev/shm` (POSIX semaphores — multiprocessing/joblib; in landlock mode a
  surface every same-uid session shares). **No shared cache is writable**: a session-writable
  `~/.npm`, uv cache or `plugins/data` is a cross-session plant channel (other sessions' `npx -y` /
  `uv run` / plugin hooks execute from them), so the launcher/broker point `npm_config_cache`,
  `NPM_CONFIG_CACHE` and `UV_CACHE_DIR` under `<cwd>/.cli-tmp` (per-workspace downloads: they count
  against `USER_WORKSPACE_QUOTA_MB`, and a stateless run starts cold every request) — with
  `UV_PYTHON_INSTALL_DIR` and `UV_TOOL_DIR` too: uv treats an unreadable `~/.local/share/uv/*` as
  fatal (a missing one is fine), so as a uid-mode session uid every `uv run` / `uvx` — plugin MCP
  servers bootstrapped that way included — died before #246; no `UV_PROJECT_ENVIRONMENT` (a project
  venv stays beside its project). A plugin MCP server that dies while starting is only
  `init.mcp_servers[].status == "failed"` (the CLI keeps no stderr); the gateway logs it as
  `MCP server '<name>' failed to start` (`tests/test_cli_mcp_startup_failure.py`), and
  `plugins/data` stays read-only with the gateway pre-creating `plugins/data/<id>` for each
  `installed_plugins.json` key on every confined spawn (`_ensure_plugin_data_dirs`): the CLI mkdirs
  `$CLAUDE_PLUGIN_DATA` before every plugin hook and **skips the hook on EACCES**; with the dir
  present the hook runs and only its writes there fail. The CLI's only knob,
  `CLAUDE_CODE_PLUGIN_CACHE_DIR`, moves the whole plugins root, so a per-session data dir needs
  per-user config dirs (uid mode has them: there `P/plugins/data` is the session uid's). `CLAUDE_ADD_DIRS` rides the #240 filter (`_operator_add_dir_roots`) and then
  `_GrantFilter`, never the raw strings. A confined script reaches what a file tool may plus `/dev/shm` and the read-only
  system roots — `/app` is never listed (code + `/app/data` creds denied by rule absence, not a ro
  rule). `/etc` and `/usr` stay ro (the CLI/libc need them; a passwd entry is required), so
  acceptance is "a path **outside the ruleset**", not `/etc/passwd`. **`/proc` stays ro too (the
  bundled CLI aborts without it), so in both modes a session can read every process's command line
  (`/proc/<pid>/cmdline`)** — world-readable whatever the uid. So with confinement on,
  `create_client` and `verify()` (startup and every admin health check) move the `--mcp-config`
  JSON (with `MCP_FORWARD_CONTEXT` `{{header:…}}` values and the plugin MCP credential overlays)
  and the system prompt (`Current user:`) into per-spawn files in the CLI's cwd
  (`argv_files.externalize`: `O_EXCL|O_NOFOLLOW` under a workspace dir fd, 0600 landlock / 0644
  uid; `--system-prompt-file`, `--append-system-prompt-file` via `extra_args`), passed by **name
  relative to that cwd** (an absolute path put the workspace, named after the user, on the CLI's
  command line), and remove them once the CLI has started (`create_client` after `connect()`,
  `verify()` after its query) — the CLI reads them while parsing argv and never again
  (`tests/test_cli_argv_files.py` pins that against the bundled CLI: MCP header + prompt still
  arrive after the delete, the live cmdline holds only the relative names; when a CLI bump reads
  them lazily, keep the files for the session). Files a gateway that died mid-spawn left behind
  are removed at the next startup (`argv_files.sweep_stale`: depth ≤ 2 under the workspace base,
  only `_write`-shaped names older than 15 min, never through a symlinked dir, in a worker thread
  startup does not wait for; landlock-mode `verify()` runs in the client's private 0700 temp cwd
  instead, outside every session's ruleset, which a crash leaves behind as before), and the workspace file
  API always hides and 404s `.gateway-argv-*`. An in-process `sdk` MCP server keeps the dict (the
  SDK needs it), `--json-schema` has no file form so the request's own `text.format` schema stays
  on the command line, and **every Bash tool call's shell argv** carries the command text plus
  `<ws>/.cli-tmp/claude-<hex>-cwd` (the CLI's `pwd -P >|` tail, so the user key), readable by
  every session while it runs (pre-existing, not closed) — the rest of the argv exposure of
  `docs/security-process-isolation.md` §6.5. `environ`, `mem` and the fds of processes
  outside the domain stay denied. Every list entry leaves the gateway absolute and the launcher
  refuses anything but a JSON array of absolute paths (exit 70) and uses `os.getcwd()`, never
  `$PWD` — a relative `USER_WORKSPACES_DIR` once granted `<ws>/<ws>`, and the old `:`-joined lists
  turned `.../x:/secret` into a grant of `/secret`. A rule target that is a regular file (e.g. the user `CLAUDE.md`) gets
  file-only rights — the dir-only `READ_DIR` bit is EINVAL for a file and would abort the whole
  build. On ABI ≥ 6 both modes add `LANDLOCK_SCOPE_SIGNAL | LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`: a
  confined process cannot signal (or reach an abstract socket of) anything outside its domain, so
  sessions cannot signal each other even in landlock mode; below ABI 6 there is no scoping.
  `TMPDIR`/`CLAUDE_CODE_TMPDIR` point at `<cwd>/.cli-tmp`
  (off the shared `/tmp`), which is **not** a Landlock rule: it rides beneath the granted cwd and is
  rebuilt first when it is not a real directory we can use (symlink, file, FIFO, socket, lost owner
  bits — and the CLI's own `claude-<uid>` inside it likewise; one owned by another uid moves aside
  to `<name>.stale-<n>`), because a rule on that agent-controlled path was a `ln -s / .cli-tmp`
  escape (`_add_rule` opens targets with `O_PATH`, following the link) and a `touch .cli-tmp` locked
  the workspace out for good. The repair runs fd-relative from the workspace and never follows a
  link (an agent racing it with re-planted links only makes its own spawns fail closed after 8
  tries; nothing sweeps a landlock-mode session's detached jobs), and an operator
  `CLAUDE_CODE_TMPDIR` is honored (landlock mode only; the broker always uses `.cli-tmp`) only as a
  single entry directly in the workspace. Failing to make
  it is fatal (launcher exit 70, broker child exit 118), not an opaque CLI exit 1 later. The same
  shared helpers (`_landlock_rules.session_tmp_env` / `session_shell_env`, launcher and broker child
  alike) set two more overrides. `GIT_CONFIG_GLOBAL=<cwd>/.cli-tmp/gitconfig`, pointing
  `core.excludesFile`/`core.attributesFile` at absent tmp paths (kept only while it is a readable,
  non-empty regular file still carrying that `excludesFile` line, so the session's own
  `git config --global` edits persist; anything else moves aside to `gitconfig.broken-<n>` and is
  rewritten atomically): Landlock does not mediate `access(2)` (nor `stat`, `chmod`, `chown`,
  `utime` or `setxattr` of a file outside the ruleset — it checks opening, executing, creating,
  removing, renaming/linking, truncating and device ioctls), so git took a denied `~/.gitconfig`
  or `~/.config/git/ignore` for readable and exited 128. Operator-wide git config belongs in
  `/etc/gitconfig` — except those two keys, which the session's global file overrides; the CLI's
  internal git helpers pin `GIT_CONFIG_GLOBAL=/dev/null` and still read the default excludes file.
  And, when the CLI would
  pick bash, `CLAUDE_CODE_SHELL=claude-session-bash` (root-owned, `/usr/local/bin` in the image; the
  file itself is in the ro list): with no writable `<cfg>/shell-snapshots` — shared, so never made
  writable, since every session sources its snapshot before each command — the CLI runs every Bash
  call as `bash -c -l`, whose `/etc/profile` reset PATH and whose denied `~/.profile` put
  `Permission denied` in every result; the wrapper turns exactly that leading `-c -l` into
  `--norc -c` (Debian's bash otherwise reads `~/.bashrc` when the env looks like ssh; a zsh choice
  is left alone). Plugin `bin/` dirs on PATH and the CLI's pkill self-guard come only through the
  snapshot, so they stay off under confinement. The slash/skill preflight
  (`slash_commands._build_preflight_options`) is confined exactly like a session in that workspace
  (uid mode: through the broker as the request user's uid once that user has one — `user` is
  threaded from the responses route and `create_client`; a named user with no uid yet stays
  landlock-shaped as the gateway uid, so a request that then fails 400 allocates nothing, and so
  does a no-cwd preflight in a private per-process `mkdtemp` dir) and in **every** mode runs with
  `--strict-mcp-config` (zero servers),
  `--settings {"disableAllHooks": true, "apiKeyHelper": "", …}` (every startup settings command —
  `apiKeyHelper`, `proxyAuthHelper`, `awsCredentialExport`, `awsAuthRefresh`, `gcpAuthRefresh`,
  `otelHeadersHelper` — blanked; a flag value outranks the workspace's) and the same secret blanks a
  session child gets (`client.child_secret_env`: `ADMIN_API_KEY`, `API_KEY`, the
  `SYSINFO_CHILD_ENV_MASK` names, other backends' and auth-mode credentials, the ticket secret): it
  only reads the catalog, and with plain options the CLI ran workspace/plugin hooks, started
  `.mcp.json` servers and ran a workspace `apiKeyHelper` that saw `ADMIN_API_KEY`
  (`tests/test_cli_slash_preflight.py` pins identical catalogs, no side effects, a helper that does
  run seeing no gateway secret, and the uid-mode preflight through the broker). The image ships
  launcher+broker+`_landlock_rules`+`claude-session-bash` at `/usr/local/bin` (root-owned, outside
  `/app` so `chown -R app /app` can't make them agent-writable) and sets `GATEWAY_LANDLOCK_LAUNCHER`;
  startup **refuses** a launcher a session could replace — it, its sibling `_landlock_rules.py`, the
  sibling `claude-session-bash` when present, and every ancestor dir must be root-owned and not
  group/other-writable (sticky dirs pass;
  `landlock.launcher_write_problems`), and `CLAUDE_ISOLATION_ALLOW_UNSAFE_LAUNCHER=true` downgrades
  that to a warning for a dev checkout (`tests/conftest.py` sets it). This is
  the no-privilege **interim** of `docs/security-process-isolation.md` §3: no uid switch, so no
  cross-session DAC isolation (that is `uid` mode). Fail-closed:
  `config_check._check_landlock_confinement` refuses startup if the launcher/real CLI cannot resolve,
  Landlock cannot actually be **enforced** (a forked probe builds+`restrict_self`es a real ruleset,
  not just an ABI query — a seccomp filter blocking `add_rule`/`restrict_self` is caught, and a
  probe that cannot run counts as a failure), the ABI is below `MIN_LANDLOCK_ABI` = 3 (kernel 6.2:
  ABI 1/2 handle no `TRUNCATE`, so a session could still truncate a DAC-writable file outside the
  ruleset; mirrored by `docker/_landlock_rules.MIN_ABI`, parity-tested), the uid
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
  entrypoint-generated; the broker inherits it in its env, and `hand_off_ticket_secret` moves the
  gateway's copy out of the environment into an inherited pipe
  (`GATEWAY_ISOLATION_TICKET_SECRET_FD`) right before exec'ing the CMD, so the `sh -c` wrapper,
  `GATEWAY_STARTUP_SCRIPT`, `install_plugins.py` and uvicorn's exec-time environ never hold it;
  `capture_ticket_secret` reads and closes that pipe at startup — only a FIFO, non-blocking,
  ≤ 4096 bytes; an operator-set variable still wins and is popped from `os.environ` — before any
  spawn (the frozen codex/opencode clients copy `os.environ`), and the secret is still blanked per
  spawn. The variable is `<fd>:<dev>:<ino>`, so a `uvicorn --workers`/`--reload` worker, which
  inherits only the variable, never reads and closes a pipe of its own (multiprocessing's
  sentinel). The CMD steps inherit the descriptor: one that reads it by accident consumes the
  secret, the gateway warns the pipe was empty, and uid mode refuses to start; a deliberate
  same-uid reader could write it back unnoticed, but the app uid can already rewrite `/app`, so the
  pipe is hygiene, not a boundary). `create_client` re-mints the ticket
  right before `connect()` (120 s TTL; the skills preflight awaits a global lock in between). The
  resident loop is single-threaded `select.poll` (it forks): `SO_PEERCRED` pinned to
  `GATEWAY_ISOLATION_PEER_UID` before reading a byte, ≤ 32 handshakes in flight, a 10 s total
  deadline each, an 8 MiB header cap, a strict `{ticket: str, argv: [str], env: {str: str}}` schema,
  and any per-connection exception or fork failure answers REFUSED and keeps serving (one malformed
  header used to kill the only broker). It verifies the ticket **in the resident parent** (nonce→exp
  store, pruned at expiry, so single use is authoritative) and `_authorize` keeps uid **and** gid in
  the band, in [1, 4294967294] ((uid_t)-1 means "unchanged" to set*id), and outside {0, APP_UID,
  APP_GID, the broker's own uid/euid/gid/egid}; the gateway's allocators skip the same ids
  (`reserved_uids()`: 0, APP_UID, APP_GID, the broker uid and `CLAUDE_ISOLATION_BROKER_GID`), since
  a session's gid is its uid; relative workspace/ro/rw entries are refused. The only same-uid path
  is the argv-only test flag
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
  abnormally, the resident broker (also a subreaper) kills what it left from a short-lived killer
  with the supervisor's `(broker, U, broker)` credentials and caps 0 (`_sweep_orphans`/`_kill_as`),
  so U's orphans cannot stop or kill it even below ABI 6, and waits for it a bounded time
  (`WNOHANG|WUNTRACED`, 2 s, plus up to 2 s to reap it after a SIGKILL): a stopped or overrunning
  killer is SIGKILLed and reaped, and its pids are logged as not swept and not retried — the
  single-threaded resident never blocks on a process a session can signal. Gap: a direct launcher SIGKILL with no relayed SIGTERM (the `agent_messages`
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
  filtered + plumbing-stripped, `PWD` = workspace. Anonymous/stateless runs **lease** a uid from an
  in-memory ephemeral band (`_EphemeralPool`, top `CLAUDE_ISOLATION_EPHEMERAL_UIDS` of the band; an
  empty one is a startup error), keyed by the absolute workspace path and released by
  `WorkspaceManager.cleanup_temp_workspace` once the workspace is gone (when the pool is full, leases
  whose workspace is gone are reclaimed first), which every path runs only after the run's CLI is
  stopped, so no uid is shared by two live runs. In the known gaps (a stateless or try-run
  disconnect that times out without the cancelled path's kill, and a delete during a continuation's
  slash preflight) the uid can be handed out while the old CLI still runs, and the broker backs the
  pool up: it refuses a uid a live supervisor holds for a different workspace, and any uid an
  orphan still holds — one not swept yet, or one a sweep gave up on (`_check_uid_free`). That
  check assumes one workspace per uid: a named user whose orphan a sweep gave up on cannot spawn
  until it exits, and one live in two workspaces at once (a legacy `base/<user>` resume beside a
  `base/<user>/claude` session) is refused in the second. The session uid writes its workspace
  (next gotcha), so the gateway alone cannot delete an anonymous one: `cleanup_temp_workspace`
  hands it to `uid_cleanup.reclaim_temp_workspace` (one background `Reclaimer` thread; `sync=True`
  only for the startup probe), which has the broker wipe the workspace, the uid's per-uid config
  state (`uid_layout.ephemeral_clear_targets`) and its `/dev/shm` objects as that uid (`cleanup`
  op, parked until the run's supervisor is gone), removes the rest itself and releases the lease
  only once that is verified; a failed wipe keeps the lease and is retried. Before a leased uid's
  first spawn `uid_layout.ensure_clean_ephemeral` wipes what a dead gateway process left (an
  unleased `_tmp_` workspace's uid is read from its root's ACL). The persisted registry never grows, and with every uid leased a new anonymous run gets
  the retryable 503 (an `api_error` event on `/v1/agents/messages`). Named users get durable uids,
  never reclaimed (follow-up): a full stable
  band raises `UidBandExhausted` → HTTP 500 `uid_band_exhausted`, not "retry shortly", with a warning
  once it crosses 90%. The registry fails **closed**: a corrupt/unreadable
  `isolation_uids.json` refuses uid-mode startup (`_check_uid_registry`) instead of loading empty
  and reassigning uids; entries it cannot honor (out of band, reserved, shared — e.g. after
  narrowing the band or growing the ephemeral slice) are kept on disk, refuse startup too (the
  error counts them per reason), and their uids are excluded from the ephemeral pool (else an
  anonymous run would get a named user's files); writes are fsync'd 0600, and a failing directory
  fsync after the replace keeps the grant. It is **single-writer**: `get_registry()` holds a
  process-lifetime `flock(LOCK_EX|LOCK_NB)` on `<registry>.lock`, so a second process (`uvicorn
  --workers`, replicas sharing `/app/data`) refuses uid mode instead of handing two users one uid
  (the in-memory ephemeral pool could not be shared either); a filesystem without `flock` refuses
  too, and replicas on other hosts are detected only where the filesystem propagates `flock` (not
  NFS with `local_lock=flock`/`nolock`). The lock file is opened read-write (NFS emulates `flock`
  with a write lock) and read-only only when that is refused, so an existing one the gateway can
  only read still works locally; creating it needs a writable directory, and a non-regular lock
  path (a planted FIFO) is refused without blocking. The band is counted, never
  listed (a huge band used to OOM at startup), `CLAUDE_ISOLATION_EPHEMERAL_UIDS` is capped at
  65536 (released uids queue in memory until the never-used ones run out), and a band max above
  4294967294 is a startup error. A held entry's startup error names its uid per reason (never the
  user key) and the fix for that reason.
  `effective_mode()` self-detects at startup: Landlock ABI≥3, the broker reachable, and the
  **startup probe** passed. Once the broker answers, `install_process_env` runs
  `_probe_uid_sessions`:
  - it prepares an ephemeral session uid's layout for a fresh anonymous workspace
    (`uid_layout.prepare_spawn`) and spawns the real CLI `--version` through the launcher and
    broker with a probe session id;
  - it checks as the gateway that the uid created its `.cli-tmp`, `projects/<name>`,
    `session-env/<id>` and `tasks/<id>` (by owner: the kernel evaluated the ACLs, mode bits would
    not show them), that the shared tree is mode-readable to it (`uid_layout.shared_tree_problems`:
    it carries no ACL), and that the broker's cleanup then leaves nothing of the uid behind.

  Unavailable uid is a hard `check_config` error naming the reason, **unless**
  `CLAUDE_ISOLATION_FALLBACK=landlock`. The probe is skipped when the secret, the bands, the
  registry or a configured per-uid config base are broken, so their own errors stand (a missing
  base is reported first, as the likely cause of what the probe saw). In uid mode, `verify()` also runs in such a
  workspace and names a launcher/broker refusal (`landlock.describe_launcher_exit`) instead of
  printing npm install hints.

  uid mode needs the layout `docker-compose.uid.yml` sets (next gotcha). A local container run
  (2026-10-15, this layout) served named, anonymous and stateless turns as session uids, migrated a
  legacy transcript and memory, resumed an expired session after a restart, wiped and reused
  ephemeral uids (8 runs on a 3-uid slice), ran a plugin SessionStart hook as the session uid, and
  refused the two broken layouts (no config base; the shared config dir in the 0700 `/home/app`);
  the 2026-10-14 run had verified the privileged layout (broker caps exactly SETUID|SETGID with
  securebits locked, gateway CapEff 0, supervisor `(broker, U, broker)` with caps 0, CLI `(U,U,U)`,
  teardown). Re-run `docs/isolation-uid-verification.md` in the deployment container before
  enabling uid in prod. In uid mode the root entrypoint no longer re-owns (or walks) `~/.claude` entries owned by a
  session uid (the band and every uid the registry records, minus root, the app uid/gid and the
  broker uid/gid — the registry read as root without importing src, a symlinked, non-regular,
  oversized or malformed one ignored with a warning; other modes still hand everything back to the
  app uid). A session uid's 0600 transcript is read (rehydrate, `transcript_exists`) and removed
  through the broker as that uid (`uid_cleanup`: the gateway tries itself first); only a transcript
  neither may read is the non-retryable 500 `transcript_unreadable`
  (`session_manager.TranscriptUnreadable`). A refused workspace (`landlock.WorkspaceRefused`) is a non-retryable 500 `workspace_refused` too.
  These, `uid_band_exhausted` and the 409 `session_ended` carry `x-should-retry: false`: OpenAI
  SDKs retry 408/409/429 and every 5xx on their own otherwise (a retried `session_ended` would
  silently start a fresh conversation), CORS exposes the header to browser clients, and the
  OpenAI-style `HTTPException` handler in `src/main.py` now passes exception headers through (it
  used to drop them, `WWW-Authenticate` on the 401s included). A 429 still goes through the
  rate-limit handler instead, which keeps its own body and `Retry-After` (the archive 429's code
  and `Retry-After` are lost there — pre-existing).
  Remaining
  hardening (seccomp arg filter on set*id, bounding-set drop) is a container/follow-up step. `docker/{landlock_launcher,isolation_broker,_landlock_rules}.py`
  never import `/app/src` (session-writable + privileged); the broker's ticket-verify is duplicated
  from `uid_isolation` and kept in lockstep by `test_isolation_broker.py::TestTicketVerify`
  (valid/tampered/expired/replayed cross-check + identical `ValueError` refusals).
- uid-mode ownership + per-uid config dir (`docs/isolation-uid-ownership-design.md`, layout in
  `docker-compose.uid.yml`): `uid_layout.enabled()` = uid mode + `CLAUDE_ISOLATION_CONFIG_BASE`
  (startup requires it, an absolute dir on a persistent volume outside `$HOME`, the shared config
  dir, the workspace base, `/app`, a system root; ACL support on it and on the workspace base;
  `WORKSPACE_LEGACY_LOCALPART_KEY` refused). Before every per-uid spawn `uid_layout.prepare_spawn`
  (and, for a named user, `migrate_user` in a worker thread before `create_client` asks for the
  transcript) makes, as the gateway and never through a link:
  - the per-uid config dir `P = <base>/<uid>` — the CLI's `CLAUDE_CONFIG_DIR` — app-owned with
    ACL `u:U:r-x`: links to the shared tree `S` = the gateway's own `CLAUDE_CONFIG_DIR` (skills,
    agents, commands, output-styles, `CLAUDE.md`, rules, hooks, workflows, `settings*.json`), a real
    `plugins/` (registry and `marketplaces` linked to `S/plugins`; a linked `plugins/` silently
    skips every plugin hook), and the writable `projects`, `session-env`, `tasks`, `plugins/data`,
    `state` (`P/.claude.json -> state/.claude.json`; the CLI's locked write fails, its fallback
    writes through the link) with `u:U:rwx` plus a default ACL. `S` must lie outside `/home/app`
    (0700) and the system roots; `install_plugins.py` installs into it, and in uid mode the settings
    writer writes `settings.json` 0644 and startup widens `S`'s entry points (`ensure_shared_readable`);
  - the workspace root kept app-owned (design α) with access ACL `u:U:rwx` and default ACL
    `d:u:U:rwx, d:u:<app>:rwx` — without the app entry the gateway loses everything U creates; never
    `chmod` a granted dir (its group bits are the mask). Under a default ACL umask is ignored and a
    new object's mask is the default mask ∩ its create mode's group bits, so U's 0600/0700 files
    (transcripts, `mkstemp`, the CLI's `.cli-tmp/claude-<uid>`) close to the gateway: those go
    through the broker. An existing tree is granted once (`posix_acl.grant_tree`: no link followed,
    hard-linked files skipped, so the managed `.claude/{skills,agents}` mirror is emptied first and
    rebuilt after; marker beside the workspace, written only when complete), and a named user's
    legacy `projects/<name>` (their transcripts by recorded `cwd`, sidecars, `memory/` only without
    another user's transcript) is copied once from `CLAUDE_ISOLATION_MIGRATE_FROM` (default the
    shared dir, `off` disables; the source stays for a rollback).
  The Landlock lists come from `landlock._per_uid_ro_rw` (rw: workspace, `P/projects/<name>`,
  `P/session-env/<id>`, `P/tasks/<id>`, `P/plugins/data`, `P/state`; ro: `S`'s shared assets and
  registry roots, `P/plugins`); `_GrantFilter` knows `S` (shared names only) and denies the config
  base. The broker's two ops (`docker/isolation_broker.py`; client `broker_ops`, no fds, strict
  schema; the spawn path refuses an op ticket): `cleanup` (as `(broker, U, broker)`: clear / remove
  / expire / IPC — `/dev/shm` entries, SysV shm/sem/msg objects whose owner or creator is U, and
  `/dev/mqueue` entries —, no link followed, parked while a live supervisor of that
  `(uid, workspace)` runs, the IPC and expire steps while any of the uid runs, a spawn of the uid
  refused meanwhile) and `transcript` (returns only a turn count or a negative code:
  `transcript_turns.count_transcript_turns` over `bounded_lines`, files above
  `MAX_TRANSCRIPT_BYTES` refused, a line that may hold more than `MAX_LINE_VALUES` JSON values
  refused unparsed — `too_many_values`, one character class plus json's own C `scanstring`: a
  regex repeating a group keeps a backtracking record per repetition, gigabytes for a line of
  escapes — kept byte-identical to the broker's copy by `tests/test_transcript_turns.py`). The
  gateway's leftover check (`uid_cleanup.ipc_leftovers`, fail closed) counts only the SysV objects
  U **created** and not a segment marked `SHM_DEST`: `IPC_SET` lets any creator hand an object to
  any uid, and another session holding such a segment attached would otherwise keep U's lease
  forever (the broker still removes what U owns; accepted cost: Linux still lets a `SHM_DEST`
  segment be attached by id, so one the previous tenant shared with another uid stays readable to
  the next tenant — design §11.7). SysV IPC is not namespaced per session and Landlock
  does not mediate it: a live session can still share an object it made world-accessible with
  another live session (a per-session IPC namespace is the follow-up). Rehydrate reads the transcript
  outside `SessionManager.lock` and off the event loop (`get_session_async`); a transient broker
  failure is a retryable 503 (`TranscriptTemporarilyUnavailable`), a refusal or an unreadable one
  the 500 `transcript_unreadable`. The `Reclaimer` has 4 workers and re-parks a given-up workspace
  job every 5 min while its root and lease exist; the uid-mode startup sweep takes every `_tmp_`
  regardless of age (one process per registry). An hourly `uid_cleanup.retention_sweep` expires each
  named uid's per-uid state older than `S`'s `cleanupPeriodDays` once a day (last run = the mtime of
  `P/.oh-my-gateway-retention`, so restarts keep the schedule), only when the `user` setting source
  is on (the CLI's own sweep has the same gate and runs only when that uid's CLI starts); the broker
  refuses it while any session of the uid is live, so a refused uid is retried the next hour, one
  overdue for two days is warned about once a day (an always-live user is never swept), and an
  unreachable broker is one warning per sweep. Spawn-preparing thread offloads (`create_client`,
  `verify()`, the slash preflight, the mirror materialize) use `uid_layout.to_thread_to_completion`:
  a cancelled caller waits for the thread before its cleanup runs (it could lease again for a
  removed workspace). The CLI child gets umask 077 (new `/dev/shm` objects 0600), git `safe.directory` for
  the app-owned root via `GIT_CONFIG_COUNT`, and `USER`/`LOGNAME` from the passwd entries the root
  entrypoint adds for the broker uid and the band (`provision_session_accounts`, ≤ 65536 uids). The
  quota walk skips only a session uid's private dirs under `.cli-tmp` (else every quota-growing write
  of every uid-mode session would fail closed). The file API's compare-and-swap write keeps a session
  uid's named ACL entry and mask on the file it replaces (`_keep_owner_access`: the chmod to the old
  mode would collapse the mask and lock the agent out of its own file). The one-time tree grant
  holds `workspace_resources.workspace_lock` across mirror clear → grant → rebuild; legacy reads and
  copies are bounded (size, line length, depth; hard-linked files skipped) and a failure never fails
  the spawn; a named uid gets the ephemeral clean-start check until `P` carries its owner marker
  (`.oh-my-gateway-owner`, the key's digest, fsync'd, written before any of the user's data lands
  there; a band change can hand it a uid an anonymous run used, and a failed wipe must be redone on
  the retry and after a restart — an unreadable or malformed marker refuses the spawn rather than
  wipe; a `P` from a build before the marker reads as unclaimed and is wiped). `verify()`
  builds its options in a worker thread like `create_client`. In tests the broker refuses op paths outside the
  temp dir and touches `/dev/shm` only via `GATEWAY_ISOLATION_TEST_SHM_DIR` (the test uid is the
  developer's). Python 3.12's `Path.exists()/is_file()` raise on an unsearchable parent: code that
  looks into session-writable trees uses `lstat` and catches `OSError`. Rollback to landlock/off
  keeps U's ordinary (0666/0777) workspace files usable by the app uid through the ACL; its
  0600/0700 files and the transcripts in `P` stay closed (those sessions are not resumed).
- Host/secret redaction (`src/backends/claude/sysinfo_redaction.py`, on by default,
  `SYSINFO_REDACTION` / admin key `sysinfo_redaction_enabled`): a `PostToolUse` hook
  (`updatedToolOutput`) replaces this host's name/addresses/container id/MACs, secret env values
  (or, with opt-in `SYSINFO_REDACT_ALL_ENV`, every env value ≥ 8 chars except the process basics
  in `_ALL_ENV_EXCEPTIONS`, `SYSINFO_REDACT_ALL_ENV_EXCEPT` names, path-like values — the model
  reuses them in tool calls — and values with whitespace or over `_ALL_ENV_MAX_LEN` (256, the
  stream carry's floor, so the option never lengthens the hold); left-out names are logged)
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
- The suite never touches the developer's real Claude config dir: `tests/fixtures/hermetic_home.py`
  (imported by `conftest.py` before any `src` import) moves HOME to a temp dir and unsets
  `CLAUDE_CONFIG_DIR`, and `_guard_real_claude_home` fails the run if the real dir still gains a
  `projects/` entry or its `settings.json` / plugin registry changes
  (`GATEWAY_TEST_SKIP_HOME_GUARD=1` skips the check).
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