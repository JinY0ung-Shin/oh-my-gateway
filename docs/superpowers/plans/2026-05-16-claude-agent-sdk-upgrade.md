# Claude Agent SDK 0.1.57 → 0.2.82 Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `claude-agent-sdk` from 0.1.57 to 0.2.82 with adjustments to tool allow-lists, MCP connection behavior, and Skill option migration, while preserving the gateway's pass-through philosophy and documenting breaking changes for downstream API consumers.

**Architecture:** Adopt **Option A (pass-through)** — no compatibility adapters added to the gateway. Update `src/backends/claude/constants.py` tool catalog for the new Task tools, preserve new server-side tool block types through chunk processing/streaming, migrate from deprecated `"Skill"` allowed_tools entry to the new `skills` option on `ClaudeAgentOptions`, and surface new SDK features incrementally. Downstream breaking changes are documented for API consumers to handle.

**Tech Stack:** Python 3.x, `uv` for dependency management, `pytest` (asyncio_mode=auto), `claude-agent-sdk`, FastAPI, SSE.

---

## Release Highlights (0.1.58 → 0.2.82)

**Downstream-visible changes (0.2.82):**
- MCP servers connect in background by default (was: blocking up to 5s). Override: `MCP_CONNECTION_NONBLOCKING=0` env or per-server `alwaysLoad: true`.
- Headless/SDK sessions still emit `TodoWrite` by default. The new `TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList` family is available when the CLI subprocess env includes `CLAUDE_CODE_ENABLE_TASKS=1`.

**API additions worth using (chronological):**
- `0.1.62`: `skills` option on `ClaudeAgentOptions` (replaces `"Skill"` in `allowed_tools`)
- `0.1.65`: `ThinkingConfig.display` field; `ServerToolUseBlock` / `AdvisorToolResultBlock` now emitted (previously dropped)
- `0.1.71`: `SandboxNetworkConfig` domain allowlist fields
- `0.1.73`: `session_store_flush` (`"batched"` / `"eager"`)
- `0.1.74`: `include_hook_events`, `"defer"` hook decision, `strict_mcp_config`, `xhigh` effort level, atexit subprocess cleanup
- `0.1.76`: `ResultMessage.api_error_status: int | None` (429/500/529)
- `0.1.77`: Actionable error messages on `Command failed exit 1`; `"Skill"` allowed_tools deprecated
- `0.2.82`: `EffortLevel` type export

**Deprecations:**
- `"Skill"` in `allowed_tools` → use `skills=` option (0.1.77)

**Bug fixes inherited (no action):**
- Trio compat restored (0.1.67), trio nursery corruption (0.1.70), ResourceWarning on disconnect (0.1.74), stderr callback isolation (0.2.82)

**Dependency floor:**
- `mcp>=1.23.0` required (0.2.82, CVE-2025-66416). **Already satisfied** — our `uv.lock` pins `mcp==1.26.0`.

---

## File Structure

**Modified files:**
- `pyproject.toml` — bump SDK pin
- `uv.lock` — regenerate via `uv lock`
- `src/backends/claude/constants.py` — update `CLAUDE_TOOLS` and `DEFAULT_ALLOWED_TOOLS`, add `Skill` option migration toggle
- `src/backends/claude/client.py` — migrate `"Skill"` allowed_tools to `skills=` option in `_build_options`
- `src/backends/claude/client.py` — (optional) wire `api_error_status` into error responses, expose `xhigh` for Opus 4.7
- `tests/test_sdk_migration.py` — add tests for new Task tool catalog entries and `skills` option migration
- `CHANGELOG.md` (create or update) — document downstream-breaking changes
- `docs/api/breaking-changes.md` (create) — downstream consumer migration guide

**Unchanged (intentional pass-through):**
- `src/chunk_processing.py` — generic `hasattr(tb, "type")` fallback already handles new block types
- `src/sse_builders.py` — pure pass-through

**Environment toggle (deployment decision, not code):**
- `MCP_CONNECTION_NONBLOCKING=0` if first-turn MCP tools are required

---

## Pre-Flight Checks

Before starting, verify:

- [ ] Working directory clean: `git status` shows no uncommitted changes
- [ ] On `main` (or branch off `main`): `git branch --show-current`
- [ ] Tests currently pass on 0.1.57: `uv run pytest -x -q tests/test_sdk_migration.py tests/test_claude_cli_unit.py`
- [ ] Downstream inventory documented: who consumes this gateway's SSE API? (sync with team; this drives Task 9 urgency)

If any check fails, resolve before proceeding.

---

## Task 1: Create feature branch and confirm baseline

**Files:**
- N/A (git only)

- [ ] **Step 1: Create branch**

Run:
```bash
git checkout -b chore/claude-agent-sdk-0.2.82
```

- [ ] **Step 2: Verify baseline tests pass on 0.1.57**

Run:
```bash
uv run pytest -x -q
```

Expected: All tests pass. Note the count for comparison after upgrade.

- [ ] **Step 3: Commit branch marker (no-op)**

No-op step — branch already created in Step 1, nothing to commit yet. Proceed to Task 2.

---

## Task 2: Bump SDK pin and refresh lockfile

**Files:**
- Modify: `pyproject.toml` (find line with `"claude-agent-sdk==0.1.57"`)
- Modify: `uv.lock` (regenerated)

- [ ] **Step 1: Update pin**

Edit `pyproject.toml`:
- Find: `"claude-agent-sdk==0.1.57",`
- Replace with: `"claude-agent-sdk==0.2.82",`

- [ ] **Step 2: Regenerate lockfile**

Run:
```bash
uv lock
```

Expected: `uv.lock` updated with `claude-agent-sdk==0.2.82` entry. No errors.

- [ ] **Step 3: Sync environment**

Run:
```bash
uv sync
```

Expected: New SDK installed. Verify:
```bash
uv run python -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)"
```
Should print `0.2.82`.

- [ ] **Step 4: Run baseline tests (expect some failures)**

Run:
```bash
uv run pytest -x -q tests/test_sdk_migration.py tests/test_claude_cli_unit.py 2>&1 | tail -30
```

Expected: Most tests pass. Any failures here indicate import or API signature changes — note them, do NOT fix yet (we'll address systematically).

- [ ] **Step 5: Commit pin bump**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): bump claude-agent-sdk to 0.2.82"
```

---

## Task 3: Add Task tools to allow-list (TDD)

**Files:**
- Test: `tests/test_sdk_migration.py` (add test class)
- Modify: `src/backends/claude/constants.py:15-31` (`CLAUDE_TOOLS`) and `:35-44` (`DEFAULT_ALLOWED_TOOLS`)

**Why:** 0.2.82 includes the opt-in `TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList` task-tracking tools. If an operator enables them with `CLAUDE_CODE_ENABLE_TASKS=1` and these tools are not in `allowed_tools`, task tracking may be filtered out. `TodoWrite` remains allowed because it is still the SDK/`claude -p` default.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_sdk_migration.py`:

```python
class TestTaskToolCatalog:
    """Task tools must be present in tool catalog after 0.2.82 upgrade."""

    def test_claude_tools_contains_task_tools(self):
        from src.backends.claude.constants import CLAUDE_TOOLS

        for tool in ("TaskCreate", "TaskUpdate", "TaskGet", "TaskList"):
            assert tool in CLAUDE_TOOLS, f"{tool} missing from CLAUDE_TOOLS"

    def test_default_allowed_tools_contains_task_tools(self):
        from src.backends.claude.constants import DEFAULT_ALLOWED_TOOLS

        for tool in ("TaskCreate", "TaskUpdate", "TaskGet", "TaskList"):
            assert tool in DEFAULT_ALLOWED_TOOLS, f"{tool} missing from DEFAULT_ALLOWED_TOOLS"

    def test_todowrite_retained_as_default(self):
        """TodoWrite remains default; Task* require CLAUDE_CODE_ENABLE_TASKS=1."""
        from src.backends.claude.constants import CLAUDE_TOOLS

        assert "TodoWrite" in CLAUDE_TOOLS
```

- [ ] **Step 2: Run tests, confirm failure**

Run:
```bash
uv run pytest tests/test_sdk_migration.py::TestTaskToolCatalog -v
```

Expected: 2 fail (`test_claude_tools_contains_task_tools`, `test_default_allowed_tools_contains_task_tools`), 1 pass (TodoWrite still present).

- [ ] **Step 3: Update CLAUDE_TOOLS**

Edit `src/backends/claude/constants.py:15-31`:

Replace the `CLAUDE_TOOLS` block with:
```python
CLAUDE_TOOLS = [
    "Task",  # Launch agents for complex tasks
    "TaskCreate",  # Task tracking (0.2.82+, opt-in via CLAUDE_CODE_ENABLE_TASKS=1)
    "TaskUpdate",  # Task tracking (0.2.82+, opt-in via CLAUDE_CODE_ENABLE_TASKS=1)
    "TaskGet",  # Task tracking (0.2.82+, opt-in via CLAUDE_CODE_ENABLE_TASKS=1)
    "TaskList",  # Task tracking (0.2.82+, opt-in via CLAUDE_CODE_ENABLE_TASKS=1)
    "Bash",  # Execute bash commands
    "Glob",  # File pattern matching
    "Grep",  # Search file contents
    "Read",  # Read files
    "Edit",  # Edit files
    "Write",  # Write files
    "NotebookEdit",  # Edit Jupyter notebooks
    "WebFetch",  # Fetch web content
    "TodoWrite",  # Default task-tracking tool when CLAUDE_CODE_ENABLE_TASKS is unset
    "WebSearch",  # Search the web
    "BashOutput",  # Get bash output
    "KillShell",  # Kill bash shells
    "Skill",  # Execute skills (deprecated 0.1.77 — translated to skills= option)
    "SlashCommand",  # Execute slash commands
]
```

- [ ] **Step 4: Update DEFAULT_ALLOWED_TOOLS**

Edit `src/backends/claude/constants.py:35-44`:

Replace with:
```python
DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "Write",
    "Edit",
    "Skill",
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "TodoWrite",
]
```

- [ ] **Step 5: Run tests, confirm pass**

Run:
```bash
uv run pytest tests/test_sdk_migration.py::TestTaskToolCatalog -v
```

Expected: All 3 pass.

- [ ] **Step 6: Commit**

```bash
git add src/backends/claude/constants.py tests/test_sdk_migration.py
git commit -m "feat(claude): add Task* tools to allow-list for SDK 0.2.82"
```

---

## Task 4: Decide MCP nonblocking strategy (deployment + docs only)

**Files:**
- Modify: `src/backends/claude/constants.py` (add doc comment block)
- (optional) Modify: deployment docs/env example

**Why:** 0.2.82 starts sessions before MCP servers connect. Pending servers report `status: "pending"`. We need an explicit team decision and runtime toggle path. Default: accept new behavior; provide override.

- [ ] **Step 1: Add documentation block to constants.py**

Append to the end of `src/backends/claude/constants.py`:

```python
# ---------------------------------------------------------------------------
# MCP Connection Behavior (claude-agent-sdk 0.2.82+)
# ---------------------------------------------------------------------------
# By default, MCP servers connect in the background; sessions start
# immediately and slow servers report ``status: "pending"`` in init.
#
# To restore pre-0.2.82 behavior (wait up to 5s before first query), set:
#     MCP_CONNECTION_NONBLOCKING=0
#
# Alternative: mark a specific server with ``alwaysLoad: true`` in the
# mcp_servers config so the SDK waits for that server in turn 1.
#
# We accept the new default; downstream consumers must handle ``pending``
# server state in init messages. See docs/api/breaking-changes.md.
```

- [ ] **Step 2: Confirm no MCP config code change needed**

Run:
```bash
grep -n "alwaysLoad\|MCP_CONNECTION_NONBLOCKING" /home/jinyoung/oh-my-gateway/src/backends/claude/client.py
```

Expected: no matches. We pass MCP server dicts through to the SDK as-is, so per-server `alwaysLoad: true` is a config-file decision by consumers, not a code change in the gateway.

- [ ] **Step 3: Commit doc block**

```bash
git add src/backends/claude/constants.py
git commit -m "docs(claude): document MCP nonblocking behavior in 0.2.82"
```

---

## Task 5: Migrate `"Skill"` allow-list to `skills=` option (TDD)

**Files:**
- Test: `tests/test_sdk_migration.py` (add test)
- Modify: `src/backends/claude/client.py` — `_build_options` (around line 356-385)

**Why:** 0.1.77 deprecated `"Skill"` in `allowed_tools`. The replacement is `skills="all" | [list of names] | []` on `ClaudeAgentOptions` (0.1.62). Migrating now eliminates a deprecation warning and gives finer-grained control.

- [ ] **Step 1: Read the existing `_build_options` to find insertion point**

Run:
```bash
sed -n '340,395p' /home/jinyoung/oh-my-gateway/src/backends/claude/client.py
```

Note the existing signature and where `options.allowed_tools` is set in `_configure_tools` (around line 195-199).

- [ ] **Step 2: Write failing test**

Append to `tests/test_sdk_migration.py`:

```python
class TestSkillsOptionMigration:
    """`Skill` allowed_tools entry should be transformed into `skills="all"`."""

    def test_skill_in_allowed_tools_sets_skills_all(self):
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeBackend

        backend = ClaudeBackend.__new__(ClaudeBackend)  # avoid __init__ side effects
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Skill", "Bash"],
            disallowed_tools=None,
        )

        assert "Skill" not in (options.allowed_tools or [])
        assert getattr(options, "skills", None) == "all"

    def test_no_skill_keeps_skills_unset(self):
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeBackend

        backend = ClaudeBackend.__new__(ClaudeBackend)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Bash"],
            disallowed_tools=None,
        )

        assert getattr(options, "skills", None) is None
```

- [ ] **Step 3: Run test, confirm failure**

Run:
```bash
uv run pytest tests/test_sdk_migration.py::TestSkillsOptionMigration -v
```

Expected: Both fail — `Skill` is still in `allowed_tools` and `options.skills` is None.

- [ ] **Step 4: Implement migration in `_configure_tools`**

Edit `src/backends/claude/client.py`, in `_configure_tools` (around line 188-201). Replace the method body with:

```python
    def _configure_tools(
        self,
        options: ClaudeAgentOptions,
        allowed_tools: Optional[List[str]],
        disallowed_tools: Optional[List[str]],
    ) -> None:
        """Apply tool allow/disallow lists to *options*.

        Translates the deprecated ``"Skill"`` entry in ``allowed_tools`` into
        the modern ``skills="all"`` option (claude-agent-sdk 0.1.62+).
        """
        if allowed_tools:
            filtered = [t for t in allowed_tools if t not in DISALLOWED_TOOLS]
            if "Skill" in filtered:
                filtered = [t for t in filtered if t != "Skill"]
                options.skills = "all"
            options.allowed_tools = filtered
        base_disallowed = list(DISALLOWED_SUBAGENT_TYPES) + list(DISALLOWED_TOOLS)
        if disallowed_tools:
            base_disallowed.extend(disallowed_tools)
        if base_disallowed:
            seen: set[str] = set()
            options.disallowed_tools = [t for t in base_disallowed if not (t in seen or seen.add(t))]
```

- [ ] **Step 5: Run test, confirm pass**

Run:
```bash
uv run pytest tests/test_sdk_migration.py::TestSkillsOptionMigration -v
```

Expected: Both pass.

- [ ] **Step 6: Run full SDK migration test suite for regressions**

Run:
```bash
uv run pytest tests/test_sdk_migration.py tests/test_claude_cli_unit.py -v 2>&1 | tail -40
```

Expected: All pass. Any failure here means `_configure_tools` interactions with other call sites are affected — investigate before continuing.

- [ ] **Step 7: Commit**

```bash
git add src/backends/claude/client.py tests/test_sdk_migration.py
git commit -m "refactor(claude): migrate Skill allow-list entry to skills= option"
```

---

## Task 6: Run full test suite and address regressions

**Files:**
- (depends on what fails)

- [ ] **Step 1: Run full suite, capture output**

Run:
```bash
uv run pytest 2>&1 | tee /tmp/sdk-upgrade-pytest.log | tail -80
```

Expected outcome categories:
- ✅ All pass → proceed to Task 7
- ⚠️ Failures in `test_ask_user_question_live.py` requiring live API — skip those (requires API key); confirm they were skipped not errored
- ❌ Real failures → diagnose individually below

- [ ] **Step 2: For each non-live failure, diagnose**

For each failing test, identify category:

**Category A — SDK API rename/removal:**
- Find the missing symbol: `uv run python -c "from claude_agent_sdk import <symbol>"`
- Cross-reference release notes 0.1.58–0.2.82 for the rename
- Update import and minimal usage in test or source

**Category B — Block type assertion changes:**
- New block types `ServerToolUseBlock`/`AdvisorToolResultBlock` may now appear where they were silently dropped before. Update assertions to either filter them out or include them as expected types.

**Category C — Behavioral change:**
- Look up release notes for the area; decide if test should be updated to reflect new behavior or if our code needs a compensating change.

- [ ] **Step 3: Fix one failure at a time with a commit per fix**

For each fix:
```bash
# make the fix
uv run pytest <specific test> -v  # verify
git add <files>
git commit -m "fix(claude): <one-line summary of fix>"
```

- [ ] **Step 4: Re-run full suite**

Run:
```bash
uv run pytest 2>&1 | tail -20
```

Expected: All non-live tests pass.

---

## Task 7: (Optional) Surface `api_error_status` and `xhigh` effort level

**Files:**
- Modify: `src/backends/claude/client.py` — wherever `ResultMessage` is handled
- Modify: `src/backends/claude/constants.py` — `CLAUDE_MODELS` or new effort config

**Skip this task** if there is no immediate consumer for the new fields. These are quality-of-life improvements, not migration blockers. If skipping, jump to Task 8.

- [ ] **Step 1: Locate ResultMessage handling**

Run:
```bash
grep -rn "ResultMessage\|is_error" /home/jinyoung/oh-my-gateway/src/backends/claude/ | head -20
```

- [ ] **Step 2: Add `api_error_status` propagation**

In the file where `ResultMessage` is converted to an error response (likely `client.py` or an error-mapping helper), add:

```python
status = getattr(result_msg, "api_error_status", None)
if status is not None:
    error_payload["api_error_status"] = status  # downstream sees 429/500/529 separately
```

- [ ] **Step 3: Test with mock**

Add to `tests/test_claude_cli_unit.py`:

```python
def test_api_error_status_propagated(self):
    from claude_agent_sdk.types import ResultMessage

    msg = ResultMessage.__new__(ResultMessage)
    msg.is_error = True
    msg.api_error_status = 429
    # ... assert your conversion logic surfaces 429 in the output
```

(Adapt to your actual error-mapping function signature.)

- [ ] **Step 4: Commit**

```bash
git add src/backends/claude/ tests/
git commit -m "feat(claude): surface api_error_status for downstream error classification"
```

- [ ] **Step 5: (Optional) Expose `xhigh` effort for Opus 4.7**

Only if you currently expose effort levels through your request schema. Locate where effort is parsed and add `"xhigh"` to the accepted values. Verify SDK accepts it:

```python
from claude_agent_sdk import ClaudeAgentOptions
options = ClaudeAgentOptions(max_turns=1)
options.effort = "xhigh"  # type: ignore  # SDK 0.1.74+
```

Commit:
```bash
git commit -am "feat(claude): accept xhigh effort level (Opus 4.7)"
```

---

## Task 8: Document breaking changes for downstream API consumers

**Files:**
- Create: `docs/api/breaking-changes.md`
- Modify: `CHANGELOG.md` (if exists) or create

**Why:** Option A propagates SDK breaking changes to our API consumers. They need a clear migration guide.

- [ ] **Step 1: Check for existing CHANGELOG**

Run:
```bash
ls /home/jinyoung/oh-my-gateway/CHANGELOG.md 2>/dev/null
ls /home/jinyoung/oh-my-gateway/docs/ 2>/dev/null
```

If `CHANGELOG.md` exists, append a new section. Otherwise create `docs/api/breaking-changes.md`.

- [ ] **Step 2: Write the breaking-changes document**

Create `docs/api/breaking-changes.md` with the following content:

```markdown
# Breaking Changes for API Consumers

## 2026-05-XX — Claude Agent SDK 0.2.82 upgrade

### Task tools are now available (opt-in)

claude-agent-sdk 0.2.82 ships a new task-tracking tool family (`TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList`) alongside the legacy `TodoWrite`. The bundled Claude CLI gates these behind the `CLAUDE_CODE_ENABLE_TASKS` env var:

- **Env unset (default)** — `TodoWrite` remains the only task-tracking tool emitted. No `response.tool_use` payload changes for existing clients.
- **`CLAUDE_CODE_ENABLE_TASKS=1`** — the SDK emits `TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList` instead. The gateway does not set this env automatically; operators choose per deployment.

Schemas observed in this SDK version:

| Tool | Input fields | id source |
|---|---|---|
| `TaskCreate` | `subject`, `description`, `activeForm?` (status is auto-`pending`) | returned in the matching `tool_result.content` as the created task record (for example, `{ "task": { "id": "...", "subject": "..." } }`), not in the `input` |
| `TaskUpdate` | `taskId`, plus any of `status`, `subject`, `description`, `activeForm`, `owner`, `addBlocks`, `addBlockedBy`, `metadata` | n/a (caller supplies `taskId`) |
| `TaskGet` | `taskId` | n/a |
| `TaskList` | (no required input) | n/a |

`Task*` events are per-id deltas (the CLI maintains task state on disk); `TodoWrite` events are full snapshots. Clients that want to render Task* should accumulate by `taskId`. Clients that only handle `TodoWrite` keep working as long as `CLAUDE_CODE_ENABLE_TASKS` stays unset.

### MCP server `init` may include pending servers

Sessions now start before MCP servers finish connecting. The `init` system message may list servers with `status: "pending"`. Clients that surface MCP server state should reflect this transitional state rather than treating non-`"ready"` as an error.

To force the previous behavior (block on MCP connect), set `MCP_CONNECTION_NONBLOCKING=0` in the gateway environment, or mark individual servers `alwaysLoad: true` in your MCP config.

### New block types may appear in assistant content

The SDK now emits `server_tool_use` and `advisor_tool_result` blocks (previously silently dropped). These pass through to clients unchanged. Clients that exhaustively switch on block `type` should add cases for these (or use a default fallback).

### `api_error_status` on error responses

The underlying `ResultMessage` exposes an `api_error_status: int | None` field surfacing the HTTP status (429, 500, 529) when the API call failed. The gateway does not yet propagate this to its downstream payload, but a follow-up may do so; clients planning to distinguish rate-limit from server errors should request it.

---

## Why the gateway propagates these changes

This gateway is intentionally a thin pass-through over `claude-agent-sdk`. We do not insert a compatibility shim because:
- Shims hide useful new fields (e.g., `pending` MCP status).
- Each shim adds maintenance cost that scales with upstream changes.
- Downstream consumers tend to want the latest SDK semantics.

If you need a compatibility layer in your own client, build it client-side.
```

- [ ] **Step 3: Commit doc**

```bash
git add docs/api/breaking-changes.md
git commit -m "docs(api): document 0.2.82 downstream breaking changes"
```

---

## Task 9: Smoke test against a running backend

**Files:** N/A (manual verification)

- [ ] **Step 1: Boot the gateway locally**

Run:
```bash
uv run uvicorn src.main:app --reload --port 8000 &
```

Wait for "Application startup complete".

- [ ] **Step 2: Hit the Claude endpoint with a simple prompt that triggers task tracking**

Run:
```bash
curl -N -X POST http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-7",
    "input": "Plan and execute these three steps: list files, count them, report the count.",
    "stream": true
  }' 2>&1 | head -100
```

- [ ] **Step 3: Optionally verify Task tool SSE events**

If validating the opt-in Task tool path, start the gateway with `CLAUDE_CODE_ENABLE_TASKS=1` in the CLI subprocess environment and look for `event: response.tool_use` lines with `"name": "TaskCreate"` (or `TaskUpdate`).

Expected with the env set: at least one `TaskCreate` event may appear when the model self-tracks the 3-step plan.

Expected with the env unset: `TodoWrite` remains the default task-tracking event. This is intentional for compatibility.

- [ ] **Step 4: Verify init event shows MCP server states**

If MCP servers are configured, look at the `init` system message. New `status: "pending"` entries are expected for slow-starting servers.

- [ ] **Step 5: Shut down gateway**

```bash
kill %1
```

- [ ] **Step 6: If anything anomalous, file an issue / fix**

If smoke test reveals problems, fix and add a regression test to `tests/test_sdk_migration.py`.

---

## Task 10: Open PR and request review

**Files:** N/A (PR creation)

- [ ] **Step 1: Push branch**

```bash
git push -u origin chore/claude-agent-sdk-0.2.82
```

- [ ] **Step 2: Create PR**

```bash
gh pr create --title "chore(deps): upgrade claude-agent-sdk 0.1.57 → 0.2.82" --body "$(cat <<'EOF'
## Summary
- Bumps `claude-agent-sdk` from `0.1.57` to `0.2.82`.
- Adds new `Task*` tools to allow-list (`TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList`); the bundled CLI emits these only when `CLAUDE_CODE_ENABLE_TASKS=1` is set. Default deployments keep `TodoWrite`.
- Migrates `"Skill"` allow-list entry to the modern `skills="all"` option on `ClaudeAgentOptions` (was deprecated in 0.1.77).
- Documents downstream-breaking changes for API consumers (see `docs/api/breaking-changes.md`).

## Pass-through philosophy
Per [[gateway-passthrough-philosophy]] (project convention), no compatibility adapters are added. SDK breaking changes propagate to our API consumers, who handle them client-side.

## Downstream impact (requires consumer changes)
- `Task*` tool events are opt-in via `CLAUDE_CODE_ENABLE_TASKS=1`; default deployments keep emitting `TodoWrite`
- MCP `init` may include `status: "pending"` servers
- New block types `server_tool_use` / `advisor_tool_result` may appear

## Test plan
- [x] Unit tests pass: `uv run pytest`
- [x] Manual smoke test against `claude-opus-4-7` endpoint shows `TaskCreate` events when Task tools are enabled
- [ ] Reviewer: confirm downstream consumers (list them) are aware of breaking changes

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Verify PR URL returned**

Confirm `gh pr create` outputs a URL. Share with team.

---

## Self-Review (run before handing off)

- [ ] **Spec coverage:** Every downstream-visible change in 0.2.82 → covered? (✅ Task 3 covers Task tool opt-in while retaining TodoWrite; Task 4 covers MCP nonblocking; ServerToolUseBlock/AdvisorToolResultBlock covered by streaming preservation + documented in Task 8.)
- [ ] **Deprecations:** `"Skill"` allowed_tools → covered in Task 5.
- [ ] **mcp version floor:** `>=1.23.0` — already satisfied (1.26.0), noted in Release Highlights.
- [ ] **Downstream contract:** documented in Task 8.
- [ ] **Tests:** TDD pattern used in Tasks 3 and 5; full-suite gate in Task 6.
- [ ] **Placeholders:** none — every code change has exact code and exact commands.

---

## Out of Scope (defer to follow-up)

Features available in 0.1.62–0.2.82 worth considering later, but **not** required for the upgrade:

- `SessionStore` adapter (0.1.64) — for cross-process resume, S3/Redis/Postgres backends
- `session_store_flush="eager"` (0.1.73) — for live-tailing UIs
- `include_hook_events` (0.1.74) — for surfacing hook events to clients
- `"defer"` hook decision + `DeferredToolUse` (0.1.74) — for two-stage permission UX
- `strict_mcp_config` (0.1.74) — for deterministic test sandboxes
- `ThinkingConfig.display="summarized"` (0.1.65) — to surface Opus 4.7 thinking text
- Distributed tracing via OTel (0.1.60) — `pip install claude-agent-sdk[otel]`

Each deserves its own brainstorm + plan.
