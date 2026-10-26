#!/bin/sh
# In-sandbox helper for the spawner (issue #248 spike).
#
#   sandbox-exec run <exec-id> <cmd> [args...]
#       exec <cmd> with OMG_EXEC_ID=<exec-id> in its environment; every process
#       the CLI starts (Bash, scripts, stdio MCP) inherits the tag.
#   sandbox-exec kill <exec-id> <signal>
#       signal every process in this container that still carries the tag.
#
# `docker exec` gives no handle on the process it started inside the
# container, and a signal sent to the `docker exec` client is not a reliable
# way to reach it, so teardown finds the tree by tag instead. Spike-grade: a
# process that scrubs its environment escapes the sweep (a per-exec cgroup or
# pid namespace is the production answer).
set -eu

case "${1:-}" in
run)
    id=$2
    shift 2
    OMG_EXEC_ID=$id
    export OMG_EXEC_ID
    exec "$@"
    ;;
kill)
    id=$2
    sig=$3
    found=0
    for p in /proc/[0-9]*; do
        pid=${p#/proc/}
        [ "$pid" = "$$" ] && continue
        if tr '\0' '\n' <"$p/environ" 2>/dev/null | grep -qx "OMG_EXEC_ID=$id"; then
            kill "-$sig" "$pid" 2>/dev/null && found=$((found + 1)) || true
        fi
    done
    echo "$found"
    ;;
*)
    echo "usage: sandbox-exec run|kill ..." >&2
    exit 64
    ;;
esac
