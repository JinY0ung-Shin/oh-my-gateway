# Per-user workspace storage quota

`USER_WORKSPACE_QUOTA_MB` optionally limits the cumulative logical file bytes owned by one named user's workspace tree.

```env
# 500 MiB across every backend directory below <USER_WORKSPACES_DIR>/<user>/.
# Unset or 0 means unlimited.
USER_WORKSPACE_QUOTA_MB=500
```

This is separate from the existing single-file upload controls:

- `WORKSPACE_UPLOAD_MAX_BYTES` seeds the runtime-configurable `workspace_upload_max_bytes` (admin `runtime-config`), which limits one `POST /files/upload` file. Its default is 10 MiB; `0` disables uploads.
- `MAX_REQUEST_SIZE` limits the whole HTTP request body for ordinary API requests. Its default is also 10 MiB. An authenticated `POST /files/upload` uses the upload-specific ceiling plus multipart envelope room instead, so the advertised `max_upload_bytes` is exactly what the route accepts.
- `USER_WORKSPACE_QUOTA_MB` limits the aggregate user workspace. It does not replace either request/upload limit.

## Scope

The quota bucket is the user root, `<USER_WORKSPACES_DIR>/<user>/`, not a backend-specific subdirectory. For example, with a 500 MiB quota, bytes in both `<user>/claude/` and `<user>/codex/` consume the same 500 MiB allowance. A Claude filesystem alias such as `CLAUDE_WORKSPACE_DIR=pro` therefore does not create a new quota bucket.

Usage counts regular-file payload bytes recursively. Symlinks are not followed, special files do not contribute payload bytes, and hard-linked files are counted once by inode for current usage. Directory/file copies account for the bytes the copy would materialize at the destination.

Quota accounting is fail-closed. An entry or directory that concurrently disappears (`ENOENT`/`ENOTDIR`) is skipped because it no longer contributes current storage, but permission failures and unexpected filesystem/I/O errors are never treated as zero bytes. If usage cannot be measured safely, quota-dependent HTTP routes return `503 Service Unavailable` with error code `workspace_quota_accounting_unavailable` rather than under-counting the workspace.

## File API behavior

`GET /files/limits` exposes both `max_upload_bytes` and `workspace_quota_bytes`. `GET /files/quota` exposes current `used_bytes`, `limit_bytes`, `remaining_bytes`, `enabled`, and `over_quota` for the caller. If the usage scan is incomplete because of an unreadable or failed subtree, `/files/quota` returns the same `503 workspace_quota_accounting_unavailable` response instead of publishing a misleading partial total.

`/files/quota` measures usage whether or not a limit is configured: with the quota off it reports `enabled: false`, `limit_bytes: 0` and the live `used_bytes`, because "how much am I using?" is a real question without an enforced ceiling. The scan is `O(files)` and occupies a worker from the shared threadpool, so a client should treat it as a deliberate, occasional read rather than a poll. The "no scan unless configured" rule belongs to `/files/upload` and `/files/copy`, where the walk would buy nothing.

The quota scope is always the named user's `<base>/<user>` directory, validated rather than assumed: a workspace path that is not exactly one user directory plus one backend directory below the managed base — or that belongs to an anonymous `_tmp_*` workspace — fails closed with `503 workspace_quota_accounting_unavailable` rather than charging usage against a root the gateway cannot identify.

The per-user mutation lock is reference-counted rather than cached: a gateway serves an unbounded set of named users over its lifetime, so an entry is dropped as soon as no caller holds or awaits it. Counting is exact rather than policy-based (LRU/TTL) because the only unsafe eviction is removing a lock someone is still using, and a refcount answers that directly.

Quota-growing `POST /files/upload` and `POST /files/copy` operations are preflighted. Within one gateway process, the file API serializes its own quota-growing operations per user so the usage check and upload/copy mutation share one accounting critical section. An operation whose projected usage exceeds the quota returns HTTP `507 Insufficient Storage` with error code `workspace_quota_exceeded`. If quota accounting itself fails, the operation returns HTTP `503` and performs no write/copy. Overwriting a file with a smaller replacement is allowed even when the workspace is already at its limit.

This serialization only covers mutations that enter through the file API. A simultaneous Claude tool, shell command, direct filesystem writer, or another gateway process can still change the same user root while a file API operation is in flight; the quota remains application-level rather than a filesystem transaction.

When the cumulative quota is disabled, the upload/copy path does not scan workspace usage or acquire quota locks; existing behavior and cost remain unchanged.

## Claude agent writes

When the quota is enabled, the existing Claude `PreToolUse` hook transport is also installed for workspace writes whose projected final size can be estimated. `Write`, `Edit`, and `MultiEdit` receive a **best-effort projected-size preflight**. Enabling the quota does **not** implicitly enable `WORKSPACE_SANDBOX_ENABLED`; path-boundary policy remains a separate setting.

The hook runs on the gateway's event loop, so its filesystem work (`read_text` of an Edit target, the recursive `scandir`/`stat` walk behind the usage scan) is executed in a worker thread — the same rule the file API follows with `run_in_threadpool` — and only the allow/deny decision is shaped on the loop. A large or slow (network-backed) workspace therefore delays that one tool call, not every other stream and websocket the process is serving. If accounting itself fails (unreadable or I/O-failed subtree), the hook denies the write with an explanatory reason rather than allowing it against an undercounted total, matching the file API's `503`.

The Claude hook does **not** reserve bytes between `PreToolUse` approval and the later tool execution. Consequently, two concurrent sessions for the same named user can both inspect the same pre-write usage, each independently fit, and then together push the workspace above the configured limit. This is an intentional limitation of the soft quota, not a serialized guarantee for deterministic agent writes. Multiple gateway processes introduce the same class of race. Once the workspace is over quota, subsequent deterministic growth is denied by later preflight checks until usage is reduced; shrinking replacements remain allowed when their projected result fits.

Arbitrary Bash commands, direct filesystem writers, and other opaque subprocess behavior are also outside a transactional accounting boundary. `USER_WORKSPACE_QUOTA_MB` is therefore a **soft application quota**, not a hard storage ceiling. Deployments that require an unbreakable byte ceiling or cross-process reservation semantics should enforce an OS/filesystem project quota in addition to this setting.
