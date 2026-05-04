#!/bin/sh
# Idempotent installer for a Claude Code plugin from GitHub.
# Activated when CLAUDE_PLUGIN_REPO is set; otherwise no-op.
#
# Env vars:
#   CLAUDE_PLUGIN_REPO         git URL (required to activate; empty = skip)
#   CLAUDE_PLUGIN_NAME         plugin name (default: repo basename)
#   CLAUDE_PLUGIN_MARKETPLACE  marketplace label (default: external)
#   CLAUDE_PLUGIN_VERSION      branch or tag (default: main)
set -eu

REPO="${CLAUDE_PLUGIN_REPO:-}"
[ -z "$REPO" ] && exit 0

DEFAULT_NAME="$(basename "$REPO" .git)"
NAME="${CLAUDE_PLUGIN_NAME:-$DEFAULT_NAME}"
MARKETPLACE="${CLAUDE_PLUGIN_MARKETPLACE:-external}"
VERSION="${CLAUDE_PLUGIN_VERSION:-main}"

CLAUDE_HOME="$HOME/.claude"
PLUGINS_ROOT="$CLAUDE_HOME/plugins"
CACHE_DIR="$PLUGINS_ROOT/cache/$MARKETPLACE/$NAME/$VERSION"
MARKETPLACE_DIR="$PLUGINS_ROOT/marketplaces/$MARKETPLACE"
MARKETPLACE_PLUGIN_DIR="$MARKETPLACE_DIR/plugins/$NAME"
REGISTRY="$PLUGINS_ROOT/installed_plugins.json"
KNOWN_MARKETPLACES="$PLUGINS_ROOT/known_marketplaces.json"
MARKETPLACE_MANIFEST="$MARKETPLACE_DIR/.claude-plugin/marketplace.json"
SETTINGS="$CLAUDE_HOME/settings.json"
KEY="${NAME}@${MARKETPLACE}"

mkdir -p "$PLUGINS_ROOT/cache/$MARKETPLACE/$NAME" "$CLAUDE_HOME"

# Clone or update. No silent fallback to the repo's default branch: a wrong
# CLAUDE_PLUGIN_VERSION must error clearly on first start, otherwise the
# initial clone succeeds against (say) `master` while the cache path encodes
# `main`, and the next restart fails on `fetch origin main`.
if [ -d "$CACHE_DIR/.git" ]; then
    git -C "$CACHE_DIR" remote set-url origin "$REPO"
    git -C "$CACHE_DIR" fetch --depth 1 origin "$VERSION"
    git -C "$CACHE_DIR" checkout -B "$VERSION" FETCH_HEAD
else
    git clone --depth 1 --branch "$VERSION" "$REPO" "$CACHE_DIR"
fi

PLUGIN_MANIFEST="$CACHE_DIR/.claude-plugin/plugin.json"
[ -f "$PLUGIN_MANIFEST" ] || {
    echo "[install_plugins] missing plugin manifest: $PLUGIN_MANIFEST" >&2
    exit 1
}

SHA="$(git -C "$CACHE_DIR" rev-parse HEAD)"
NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
PLUGIN_DESCRIPTION="$(jq -r '.description // ""' "$PLUGIN_MANIFEST" 2>/dev/null || true)"
PLUGIN_VERSION="$(jq -r '.version // ""' "$PLUGIN_MANIFEST" 2>/dev/null || true)"
[ -z "$PLUGIN_VERSION" ] && PLUGIN_VERSION="$VERSION"

# Preserve the original installedAt across restarts; only lastUpdated moves.
INSTALLED_AT=""
if [ -f "$REGISTRY" ]; then
    INSTALLED_AT="$(jq -r --arg k "$KEY" '.plugins[$k][0].installedAt // ""' "$REGISTRY" 2>/dev/null || true)"
fi
[ -z "$INSTALLED_AT" ] && INSTALLED_AT="$NOW"

ENTRY="$(jq -n --arg p "$CACHE_DIR" --arg v "$VERSION" \
    --arg ia "$INSTALLED_AT" --arg lu "$NOW" --arg sha "$SHA" \
    '{scope:"user", installPath:$p, version:$v, installedAt:$ia, lastUpdated:$lu, gitCommitSha:$sha}')"

TMP="$(mktemp)"
if [ -f "$REGISTRY" ]; then
    jq --arg k "$KEY" --argjson e "$ENTRY" \
        '.version = 2 | .plugins = ((.plugins // {}) | (.[$k] = [$e]))' \
        "$REGISTRY" > "$TMP"
else
    jq -n --arg k "$KEY" --argjson e "$ENTRY" \
        '{version: 2, plugins: {($k): [$e]}}' > "$TMP"
fi
mv "$TMP" "$REGISTRY"

# Claude also validates enabled plugins against their marketplace metadata.
# Direct GitHub installs do not have a real marketplace checkout, so create a
# small synthetic one that points at this cache entry.
mkdir -p "$MARKETPLACE_DIR/.claude-plugin" "$MARKETPLACE_PLUGIN_DIR/.claude-plugin"
cp "$PLUGIN_MANIFEST" "$MARKETPLACE_PLUGIN_DIR/.claude-plugin/plugin.json"

TMP="$(mktemp)"
if [ -f "$KNOWN_MARKETPLACES" ]; then
    jq --arg m "$MARKETPLACE" --arg r "$REPO" --arg p "$MARKETPLACE_DIR" --arg t "$NOW" \
        '.[$m] = ((.[$m] // {}) | .source = {source:"git", url:$r} | .installLocation = $p | .lastUpdated = $t)' \
        "$KNOWN_MARKETPLACES" > "$TMP"
else
    jq -n --arg m "$MARKETPLACE" --arg r "$REPO" --arg p "$MARKETPLACE_DIR" --arg t "$NOW" \
        '{($m): {source:{source:"git", url:$r}, installLocation:$p, lastUpdated:$t}}' \
        > "$TMP"
fi
mv "$TMP" "$KNOWN_MARKETPLACES"

TMP="$(mktemp)"
if [ -f "$MARKETPLACE_MANIFEST" ]; then
    jq --arg m "$MARKETPLACE" --arg n "$NAME" --arg d "$PLUGIN_DESCRIPTION" --arg v "$PLUGIN_VERSION" \
        '.name = (.name // $m)
         | .plugins = ([((.plugins // [])[]) | select(.name != $n)]
             + [{name:$n, description:$d, version:$v, source:("./plugins/" + $n)}])' \
        "$MARKETPLACE_MANIFEST" > "$TMP"
else
    jq -n --arg m "$MARKETPLACE" --arg n "$NAME" --arg d "$PLUGIN_DESCRIPTION" --arg v "$PLUGIN_VERSION" \
        '{name:$m, owner:{name:"External"}, metadata:{description:"Auto-installed external plugins", version:"1"}, plugins:[{name:$n, description:$d, version:$v, source:("./plugins/" + $n)}]}' \
        > "$TMP"
fi
mv "$TMP" "$MARKETPLACE_MANIFEST"

# Registry-only install leaves the plugin disabled — Claude reads
# enabledPlugins from settings.json before activating skills/hooks/MCP.
TMP="$(mktemp)"
if [ -f "$SETTINGS" ]; then
    jq --arg k "$KEY" \
        '.enabledPlugins = ((.enabledPlugins // {}) | (.[$k] = true))' \
        "$SETTINGS" > "$TMP"
else
    jq -n --arg k "$KEY" '{enabledPlugins: {($k): true}}' > "$TMP"
fi
mv "$TMP" "$SETTINGS"

echo "[install_plugins] $KEY -> $CACHE_DIR ($VERSION @ $(printf '%.7s' "$SHA")) enabled"
