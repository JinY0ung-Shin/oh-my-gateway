# Workspace skills and agents

Named Claude workspaces expose project resources at backend-neutral, user-facing paths:

```text
<workspace>/
├── skills/
├── agents/
└── .claude/
    ├── skills/
    └── agents/
```

`skills/` and `agents/` are the canonical locations that file-manager clients such as ChatDRAGON should show and edit. The `.claude/*` directories are a compatibility mirror maintained by oh-my-gateway so Claude Code can keep using its native project discovery paths.

## Compatibility mirror

The gateway creates real `.claude/skills` and `.claude/agents` directories rather than requiring clients to know Claude-specific paths. Regular files are hard-linked from the canonical tree when the filesystem supports hard links; otherwise they are copied. This keeps Claude's discovery layout conventional and avoids relying on directory-symlink behavior for subagents.

Workspace resolution only prepares the canonical and native resource roots, so file-manager polling does not recursively rescan resource trees. The mirror is refreshed immediately before Claude SDK command discovery or session creation, when the native view actually needs to be current. Symlinked entries in the canonical resource trees are not promoted into `.claude`, because a symlink could point outside the user's workspace and turn external content into trusted project configuration.

The gateway-owned `.oh-my-gateway-managed` marker files are zero-byte metadata so the compatibility layer itself does not consume user workspace quota. Hard-linked resource files are counted once by the existing inode-aware quota accounting.

## Existing workspaces

A legacy workspace that only contains `.claude/skills` or `.claude/agents` is migrated lazily on first resolve: the native directory is moved to the top-level canonical location, then the Claude compatibility mirror root is created. Its contents are materialized before the next Claude SDK discovery/query operation.

If both the canonical and legacy native directories already contain independently managed data, oh-my-gateway leaves both untouched and logs a warning rather than choosing one side and risking data loss. Merge the two trees manually and remove the unmanaged `.claude/<kind>` directory; the next workspace resolve will recreate the managed mirror.

## Other scopes

This change only affects project resources stored inside a named user workspace. Claude user-scope resources under `~/.claude/{skills,agents}` and plugin-provided resources continue to work through their existing Claude setting/plugin scopes.
