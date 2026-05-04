#!/bin/sh
# Idempotent installer for a Claude Code plugin from a marketplace repository.
# Activated when CLAUDE_PLUGIN_REPO is set; otherwise no-op.
#
# Env vars:
#   CLAUDE_PLUGIN_REPO         marketplace URL/path/GitHub repo (required to activate; empty = skip)
#   CLAUDE_PLUGIN_NAME         plugin name (default: repo basename)
#   CLAUDE_PLUGIN_MARKETPLACE  marketplace name from marketplace.json (optional)
#   CLAUDE_PLUGIN_SCOPE        install scope: user, project, or local (default: user)
#   CLAUDE_PLUGIN_CLAUDE_BIN   Claude CLI path override (optional)
#   CLAUDE_PLUGIN_GIT_USERNAME HTTPS git username for private repos (optional)
#   CLAUDE_PLUGIN_GIT_TOKEN    HTTPS git token/password for private repos (optional)
set -eu

REPO="${CLAUDE_PLUGIN_REPO:-}"
[ -z "$REPO" ] && exit 0

DEFAULT_NAME="$(basename "$REPO")"
DEFAULT_NAME="${DEFAULT_NAME%%#*}"
DEFAULT_NAME="${DEFAULT_NAME%%@*}"
DEFAULT_NAME="${DEFAULT_NAME%.git}"
NAME="${CLAUDE_PLUGIN_NAME:-$DEFAULT_NAME}"
MARKETPLACE="${CLAUDE_PLUGIN_MARKETPLACE:-}"
SCOPE="${CLAUDE_PLUGIN_SCOPE:-user}"
CLAUDE_BIN="${CLAUDE_PLUGIN_CLAUDE_BIN:-}"
GIT_USERNAME="${CLAUDE_PLUGIN_GIT_USERNAME:-}"
GIT_TOKEN="${CLAUDE_PLUGIN_GIT_TOKEN:-}"

[ -n "$NAME" ] || {
    echo "[install_plugins] CLAUDE_PLUGIN_NAME is required when it cannot be inferred from CLAUDE_PLUGIN_REPO" >&2
    exit 1
}

GIT_ASKPASS_FILE=""
if [ -n "$GIT_USERNAME$GIT_TOKEN" ]; then
    if [ -z "$GIT_USERNAME" ] || [ -z "$GIT_TOKEN" ]; then
        echo "[install_plugins] CLAUDE_PLUGIN_GIT_USERNAME and CLAUDE_PLUGIN_GIT_TOKEN must be set together" >&2
        exit 1
    fi

    GIT_ASKPASS_FILE="$(mktemp)"
    cat > "$GIT_ASKPASS_FILE" <<'EOF'
#!/bin/sh
case "$1" in
    *Username*) printf '%s\n' "$CLAUDE_PLUGIN_GIT_USERNAME" ;;
    *Password*) printf '%s\n' "$CLAUDE_PLUGIN_GIT_TOKEN" ;;
    *) printf '\n' ;;
esac
EOF
    chmod 700 "$GIT_ASKPASS_FILE"
    export GIT_ASKPASS="$GIT_ASKPASS_FILE"
    export GIT_TERMINAL_PROMPT=0
    trap 'if [ -n "$GIT_ASKPASS_FILE" ]; then rm -f "$GIT_ASKPASS_FILE"; fi' EXIT HUP INT TERM
fi

find_bundled_claude() {
    PYTHON_BIN=""
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    fi

    [ -n "$PYTHON_BIN" ] || return 1

    "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import platform
import claude_agent_sdk

cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
path = Path(claude_agent_sdk.__file__).parent / "_bundled" / cli_name
if path.exists() and path.is_file():
    print(path)
PY
}

if [ -z "$CLAUDE_BIN" ]; then
    if command -v claude >/dev/null 2>&1; then
        CLAUDE_BIN="$(command -v claude)"
    else
        CLAUDE_BIN="$(find_bundled_claude || true)"
    fi
fi

[ -x "$CLAUDE_BIN" ] || {
    echo "[install_plugins] claude CLI is required for marketplace plugin install" >&2
    echo "[install_plugins] set CLAUDE_PLUGIN_CLAUDE_BIN or install claude-agent-sdk with its bundled CLI" >&2
    exit 1
}

PLUGIN_SPEC="$NAME"
if [ -n "$MARKETPLACE" ]; then
    PLUGIN_SPEC="$NAME@$MARKETPLACE"
fi

if [ -e "$REPO" ]; then
    "$CLAUDE_BIN" plugin marketplace add "$REPO" --scope "$SCOPE"
else
    "$CLAUDE_BIN" plugin marketplace add "$REPO" --scope "$SCOPE" --sparse .claude-plugin plugins
fi

"$CLAUDE_BIN" plugin install "$PLUGIN_SPEC" --scope "$SCOPE"

echo "[install_plugins] installed $PLUGIN_SPEC from marketplace source $REPO (scope: $SCOPE)"
