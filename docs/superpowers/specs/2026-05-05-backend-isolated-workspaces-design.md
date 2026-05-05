# Backend-Isolated Workspaces Design

## Goal

Treat each backend as an independent agent runtime with its own working
directory, configuration files, skills, and runtime state. A user can still
have a stable workspace root, but Claude, Codex, and OpenCode should no longer
share one cwd by default.

This matches the current session model: a `/v1/responses` session is already
bound to one backend, and continuation with another backend is rejected. Since
conversation memory is backend-specific, filesystem state should follow the
same boundary.

## Current Behavior

`WorkspaceManager.resolve(user, sync_template=True)` creates one workspace per
user under the configured base path. The base path is:

1. `USER_WORKSPACES_DIR` when set.
2. `CLAUDE_CWD` when set.
3. A generated temporary directory such as `/tmp/claude_workspaces_<id>`.

For a named user, the current workspace path is `<base>/<user>`. For an
anonymous request, it is `<base>/_tmp_<uuid>`.

New sessions sync only `.claude/` from `CLAUDE_CWD` into that workspace.
Responses then pass that same cwd to whichever backend is selected.

## Target Layout

For named users, the backend becomes part of the workspace path:

```text
<base>/<user>/claude/
<base>/<user>/codex/
<base>/<user>/opencode/
```

Each backend-specific directory is the actual cwd passed to that backend.

Anonymous requests keep the current shape:

```text
<base>/_tmp_<uuid>/
```

The anonymous path does not need another backend component because it is already
session-scoped and cleaned up as a temporary workspace.

## Backend Template Sync

Template sync becomes backend-aware and runs only on new sessions.
The template source should be the configured `CLAUDE_CWD` directory when it
exists, even if that directory does not contain `.claude/`. The current
`_resolve_template_source` helper is too Claude-specific for this design and
should be generalized.

Claude:

- Copy `.claude/` from the template source when present.
- Copy `CLAUDE.md` when present.
- Preserve existing `pyproject.toml` and `uv.lock` symlink behavior.

Codex:

- Copy `.agents/` from the template source when present.
- Copy `AGENTS.md` when present.
- If `.agents/skills` is missing and `.claude/skills` exists, mirror compatible
  Claude skills into `.agents/skills`.
- Preserve existing `pyproject.toml` and `uv.lock` symlink behavior.

OpenCode:

- Copy `.opencode/` from the template source when present.
- Prefer native `.opencode/skills`.
- If `.opencode/skills` is missing, mirror compatible skills from
  `.claude/skills` or `.agents/skills` into `.opencode/skills`. When both
  compatibility sources contain the same skill name, `.claude/skills` wins
  because it is the existing admin-managed skill location.
- Preserve existing `pyproject.toml` and `uv.lock` symlink behavior.

The mirror rule is fill-only: a backend-native skill path wins over a
compatibility source with the same skill name.

## API And Session Semantics

`WorkspaceManager.resolve` should accept an optional `backend` argument.

For named users:

```text
resolve(user="alice", backend="codex") -> <base>/alice/codex
```

For anonymous users:

```text
resolve(user=None, backend="codex") -> <base>/_tmp_<uuid>
```

`Session.workspace` remains a string and stores the backend-specific cwd. The
existing backend mismatch guard remains in place, so a session cannot switch
from one backend cwd to another.

Claude jsonl rehydration continues to use `Session.workspace`. Existing
sessions created before this change may still point at the old `<base>/<user>`
layout; those should continue working because stored session workspace paths
are authoritative.

## Admin Surface

The current admin skills API is Claude-specific because it reads `.claude/skills`
from the global workspace root/template source. Under backend-isolated
workspaces, admin skill management should become backend-aware at the template
source level, not by editing already-created per-user runtime directories.

Minimum viable behavior:

- Existing admin endpoints continue to target Claude by default.
- Add backend-aware service helpers that map:
  - `claude` -> `.claude/skills`
  - `codex` -> `.agents/skills`
  - `opencode` -> `.opencode/skills`
- Route changes can be incremental. Backend-aware API routes may be added after
  the workspace split if the initial implementation only needs runtime support.

## External OpenCode Caveat

In external OpenCode mode, the external server must see the same filesystem
path that the gateway passes as `directory`. Backend-isolated paths do not
change that requirement. If the external server does not mount the same base
workspace tree, OpenCode skill discovery and file edits will not match gateway
expectations.

## Migration

No bulk migration is required.

- New sessions use `<base>/<user>/<backend>`.
- Existing in-memory sessions keep their stored `Session.workspace`.
- Existing Claude jsonl rehydration uses the old or new stored path depending
  on where the transcript was created.
- Users who want old shared-workspace behavior can keep using existing
  sessions until they expire.

An explicit shared-workspace mode can be added later if a product requirement
appears, but it is out of scope for this change.

## Testing

Unit tests should cover:

- `WorkspaceManager.resolve(user, backend=...)` creates backend-specific paths.
- Anonymous workspaces keep `_tmp_<uuid>` layout.
- Backend-specific template sync copies only the relevant native config.
- Codex skill mirroring fills `.agents/skills` only when native skills are
  absent.
- OpenCode skill mirroring fills `.opencode/skills` only when native skills are
  absent.
- `/v1/responses` passes the resolved backend-specific cwd to `create_client`.
- Existing continuation still uses `Session.workspace` without recomputing a
  new backend path.

Integration tests should cover one new session per backend and assert that the
backend receives isolated cwd values under the same user.
