#!/usr/bin/env bash
# oh-my-gateway zero-downtime (blue/green) deploy — docs/zero-downtime-deploy.md §4, operations §10.
#
#   scripts/deploy.sh [deploy] [--no-build] [--force]
#       build the idle color → start it → wait /health/ready → drain the active color toward it
#       (POST /internal/drain, pinned to the new boot id) → switch nginx → verify through the proxy
#       → wait until the old color's /health/drain is all zeros → stop it. --no-build swaps in the
#       ACTIVE build again (after a .env change): its image is tagged for the idle color first.
#   scripts/deploy.sh rollback [--force]
#       back to the previous color (still draining, or stopped with its old image): lift its drain
#       (DELETE /internal/drain) or restart it, drain the current color toward it, switch, wait, stop.
#   scripts/deploy.sh drain <blue|green> [--force]
#       finish an interrupted drain (e.g. after DRAIN_TIMEOUT_SECONDS) and stop that color.
#   scripts/deploy.sh restart [--no-build]
#       RESTART deploy (short outage): rebuild + recreate the active color in place. The path for
#       uid mode and for an active color too old to know handoff/drain.
#   scripts/deploy.sh migrate [--no-build]
#       once: move the legacy single `gateway` container onto blue + gateway-proxy. A restart deploy
#       (the host port changes hands; the legacy container's in-flight turns and cards are lost).
#   scripts/deploy.sh status          active color, both colors' state and drain counts
#   scripts/deploy.sh active          the active service name (gateway-blue | gateway-green)
#
# Environment:
#   DOCKER                    docker binary (default: docker) — tests inject a fake
#   GATEWAY_COMPOSE           compose command (default: "$DOCKER compose"), e.g. with the uid overlay:
#                             "docker compose -f docker-compose.yml -f docker-compose.uid.yml"
#   DRAIN_TIMEOUT_SECONDS     how long to wait for the old color to drain (default 3600). Past it the
#                             script prints what is left and stops non-zero, the old color still
#                             running; --force (or DRAIN_FORCE=true) stops it anyway.
#   DRAIN_OWNED_GRACE_SECONDS owned_sessions may lag the other counts this long (default 60); a color
#                             that still owns sessions with nothing else in flight is reported.
#   HEALTH_TIMEOUT_SECONDS    how long a new color may take to become ready (default 300).
#
# State-changing commands are serialized (docker/nginx/state/.deploy.lock). Loopback-only endpoints
# (/health/drain, /internal/drain) are reached with `compose exec` inside the color; the admin key is
# read from the container's own environment there, never put on a command line.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

DOCKER="${DOCKER:-docker}"
read -r -a COMPOSE <<<"${GATEWAY_COMPOSE:-$DOCKER compose}"
STATE_DIR=docker/nginx/state
ACTIVE_FILE="$STATE_DIR/active.conf"
LOCK_FILE="$STATE_DIR/.deploy.lock"
ENV_FILE=.env
DRAIN_TIMEOUT_SECONDS="${DRAIN_TIMEOUT_SECONDS:-3600}"
DRAIN_OWNED_GRACE_SECONDS="${DRAIN_OWNED_GRACE_SECONDS:-60}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-300}"
DRAIN_FORCE="${DRAIN_FORCE:-false}"
# /health and /health/ready share a per-IP rate limit (RATE_LIMIT_HEALTH_PER_MINUTE, 30), and every
# probe here arrives as 127.0.0.1 — poll gently.
POLL_SECONDS=5
LEGACY_SERVICE=gateway

log() { printf '[deploy %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() {
  log "ERROR: $*" >&2
  exit 1
}

compose() { "${COMPOSE[@]}" "$@"; }
svc() { printf 'gateway-%s' "$1"; }
# The per-color image tag of docker-compose.yml (`${GATEWAY_IMAGE:-oh-my-gateway}:<color>`).
image_of() {
  local name
  name="$(env_value GATEWAY_IMAGE)"
  printf '%s:%s' "${name:-oh-my-gateway}" "$1"
}
other() { if [[ "$1" == blue ]]; then echo green; else echo blue; fi; }

# ── configuration ────────────────────────────────────────────────────────────

# Value of NAME as Compose would interpolate it: the shell environment first, then ./.env.
env_value() {
  local name=$1 line value=""
  if [[ -n "${!name:-}" ]]; then
    printf '%s\n' "${!name}"
    return 0
  fi
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line#export }"
    if [[ "$line" == "$name="* ]]; then
      value="${line#"$name="}"
    fi
  done <"$ENV_FILE"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s\n' "$value"
}

# Compose project name — the legacy container is found by its compose labels.
project_name() {
  local name
  name="$(env_value COMPOSE_PROJECT_NAME)"
  if [[ -z "$name" ]]; then
    name="$(compose config 2>/dev/null | sed -n '1s/^name: *//p')" || true
  fi
  [[ -n "$name" ]] || name="$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
  printf '%s\n' "$name"
}

# ── probes (inside a color, loopback) ────────────────────────────────────────

# One small client for every loopback call. Proxies are disabled (a corporate HTTP_PROXY in the
# container must not see 127.0.0.1 calls) and ADMIN_API_KEY comes from the container env.
# shellcheck disable=SC2016
PY_PROBE='
import json, os, sys, urllib.error, urllib.request
base = "http://127.0.0.1:%s" % (os.environ.get("PORT") or "8000")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def call(method, path, body=None, auth=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", "Bearer " + os.environ.get("ADMIN_API_KEY", ""))
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
def obj(raw):
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}
op = sys.argv[1]
if op == "health":
    code, raw = call("GET", "/health")
    d = obj(raw)
    if code != 200:
        sys.exit(1)
    caps = ",".join(str(c) for c in (d.get("capabilities") or [])) or "-"
    print(d.get("instance") or "-", d.get("boot_id") or "-", caps)
elif op == "ready":
    code, _ = call("GET", "/health/ready")
    sys.exit(0 if code == 200 else 1)
elif op == "drain-status":
    code, raw = call("GET", "/health/drain")
    d = obj(raw)
    if code != 200 or "draining" not in d:
        sys.exit(1)
    keys = ("active_turns", "background_turns", "pending_interactions",
            "forwarding", "open_requests", "owned_sessions")
    print(1 if d.get("draining") else 0, *(int(d.get(k) or 0) for k in keys))
elif op == "drain-start":
    code, raw = call("POST", "/internal/drain",
                     {"target": sys.argv[2], "target_boot_id": sys.argv[3]}, auth=True)
    print(code, raw.decode("utf-8", "replace")[:500])
    sys.exit(0 if code == 200 else 1)
elif op == "drain-stop":
    code, raw = call("DELETE", "/internal/drain", auth=True)
    print(code, raw.decode("utf-8", "replace")[:500])
    sys.exit(0 if code == 200 else 1)
elif op == "isolation-mode":
    print(os.environ.get("CLAUDE_ISOLATION_MODE", ""))
else:
    sys.exit(2)
'

probe() {
  local color=$1
  shift
  compose --profile "$color" exec -T "$(svc "$color")" python -I -c "$PY_PROBE" "$@"
}

# "<instance> <boot_id> <capabilities,comma-separated>" or non-zero.
health_of() { probe "$1" health 2>/dev/null; }
boot_id_of() {
  local h
  h="$(health_of "$1")" || return 1
  read -r _ boot _ <<<"$h"
  [[ -n "$boot" && "$boot" != - ]] || return 1
  printf '%s\n' "$boot"
}
# "<draining 0|1> <active> <background> <pending> <forwarding> <open> <owned>" or non-zero.
drain_status_of() { probe "$1" drain-status 2>/dev/null; }

has_capabilities() {
  local caps=",$1,"
  [[ "$caps" == *,handoff,* && "$caps" == *,drain,* ]]
}

drain_start() {
  local color=$1 target=$2 boot=$3 out
  if out="$(probe "$color" drain-start "$target" "$boot" 2>&1)"; then
    log "$(svc "$color") is draining toward $(svc "$target") (boot ${boot})"
    return 0
  fi
  log "POST /internal/drain on $(svc "$color") refused: ${out:-no answer}"
  return 1
}

drain_stop() {
  local color=$1 out
  if out="$(probe "$color" drain-stop 2>&1)"; then
    log "$(svc "$color") drain lifted"
    return 0
  fi
  log "DELETE /internal/drain on $(svc "$color") failed: ${out:-no answer}"
  return 1
}

# ── containers ───────────────────────────────────────────────────────────────

active_color() {
  [[ -f "$ACTIVE_FILE" ]] || return 0
  # shellcheck disable=SC2016  # $gateway_upstream is an nginx variable
  sed -n 's/^set \$gateway_upstream gateway-\(blue\|green\);$/\1/p' "$ACTIVE_FILE"
}

# Container id even when it is not running (empty line when there is none). Non-zero when the
# lookup itself fails — that is "unknown", not "none".
container_of() {
  local out
  out="$(compose --profile "$1" ps -a -q "$(svc "$1")" 2>/dev/null)" || return 1
  printf '%s\n' "${out%%$'\n'*}"
}

# "none" | "<docker status>" (running, exited, paused, ...). Non-zero when unknown.
state_of() {
  local cid st
  cid="$(container_of "$1")" || return 1
  [[ -n "$cid" ]] || {
    echo none
    return 0
  }
  st="$("$DOCKER" inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)" || return 1
  [[ -n "$st" ]] || return 1
  printf '%s\n' "$st"
}

# A state the script may act on. A failed lookup is retried, and never read as "stopped".
known_state() {
  local color=$1 st tries=0
  until st="$(state_of "$color")"; do
    if ((++tries >= 5)); then
      die "could not read the state of $(svc "$color") (docker/compose lookup failed) — nothing changed"
    fi
    sleep 2
  done
  printf '%s\n' "$st"
}

# settled (no process holds work) | running. Anything else (paused, restarting, removing) may hold
# work: refuse to touch it.
classify() {
  local color=$1 st=$2
  case "$st" in
    none | exited | dead | created) echo settled ;;
    running) echo running ;;
    *) die "$(svc "$color") is ${st} — it may hold work, nothing changed. Bring it back (e.g. 'docker unpause') and rerun" ;;
  esac
}

legacy_container() {
  local project
  project="$(project_name)"
  "$DOCKER" ps -a -q --filter "label=com.docker.compose.project=${project}" \
    --filter "label=com.docker.compose.service=${LEGACY_SERVICE}" 2>/dev/null | head -n1
}

legacy_running() {
  local project
  project="$(project_name)"
  [[ -n "$("$DOCKER" ps -q --filter "label=com.docker.compose.project=${project}" \
    --filter "label=com.docker.compose.service=${LEGACY_SERVICE}" 2>/dev/null | head -n1)" ]]
}

proxy_running() {
  local cid
  cid="$(compose ps -q gateway-proxy 2>/dev/null | head -n1)" || return 1
  [[ -n "$cid" ]] && [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" == true ]]
}

# The optional usage-log sidecar (`logging` profile) — started here because the colors come up
# with --no-deps (a recreate of mysql under the active color is not part of a deploy).
ensure_sidecars() {
  if compose config --services 2>/dev/null | grep -qx gateway-log; then
    compose up -d --no-recreate gateway-log
  fi
}

# uid mode keeps the restart deploy (docs/zero-downtime-deploy.md §6): .env / shell, the rendered
# compose config (the uid overlay sets it per service), or the running active color's own env.
# What this run would start: .env / shell, or the rendered compose config (the uid overlay sets it
# per service).
configured_uid() {
  [[ "$(env_value CLAUDE_ISOLATION_MODE)" == uid ]] && return 0
  compose --profile blue --profile green config 2>/dev/null |
    grep -Eq '^[[:space:]]*-?[[:space:]]*CLAUDE_ISOLATION_MODE[=:][[:space:]]*"?uid"?[[:space:]]*$'
}

# What is running now: the active color's own env, or the legacy container's.
running_uid() {
  local active=${1:-} legacy=${2:-} mode=""
  if [[ -n "$active" ]]; then
    mode="$(probe "$active" isolation-mode 2>/dev/null)" || mode=""
  elif [[ -n "$legacy" ]]; then
    "$DOCKER" inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$legacy" 2>/dev/null |
      grep -qx 'CLAUDE_ISOLATION_MODE=uid' && mode=uid
  fi
  [[ "$mode" == uid ]]
}

uid_mode() { configured_uid || running_uid "${1:-}" "${2:-}"; }

# A restart must not silently drop uid mode because the overlay was left off this run.
refuse_uid_downgrade() {
  if running_uid "${1:-}" "${2:-}" && ! configured_uid; then
    die "the running gateway is in uid mode but this compose config is not — pass the overlay: GATEWAY_COMPOSE=\"docker compose -f docker-compose.yml -f docker-compose.uid.yml\" (nothing changed)"
  fi
}

refuse_uid() {
  die "CLAUDE_ISOLATION_MODE=uid: the blue/green swap is not available in uid mode (one gateway process per uid registry, docs/zero-downtime-deploy.md §6). Use the restart deploy (short outage): GATEWAY_COMPOSE=\"docker compose -f docker-compose.yml -f docker-compose.uid.yml\" scripts/deploy.sh restart"
}

warn_relay_base_url() {
  if [[ -n "$(env_value MCP_RELAY_BASE_URL)" ]]; then
    log "WARNING: MCP_RELAY_BASE_URL is set in .env — with two colors it must stay unset (each CLI dials its own container's loopback relay; a shared address reaches the wrong color)"
  fi
}

# ── waiting ──────────────────────────────────────────────────────────────────

# Ready = /health/ready 200 and /health names this color, with a boot id and both capabilities.
# Prints the boot id.
wait_ready() {
  local color=$1 deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS)) st h inst boot caps
  log "waiting for $(svc "$color") to be ready (up to ${HEALTH_TIMEOUT_SECONDS}s)" >&2
  while :; do
    st="$(state_of "$color")" || st=unknown
    case "$st" in
      exited | dead | none)
        log "$(svc "$color") is ${st}" >&2
        return 1
        ;;
    esac
    if [[ "$st" == running ]] && probe "$color" ready >/dev/null 2>&1 && h="$(health_of "$color")"; then
      read -r inst boot caps <<<"$h"
      if [[ "$inst" == "$color" && "$boot" != - ]] && has_capabilities "$caps"; then
        log "$(svc "$color") ready (boot ${boot})" >&2
        printf '%s\n' "$boot"
        return 0
      fi
      log "$(svc "$color") answers as instance=${inst} capabilities=${caps} — expected ${color} with handoff,drain" >&2
      return 1
    fi
    if ((SECONDS >= deadline)); then
      log "$(svc "$color") not ready after ${HEALTH_TIMEOUT_SECONDS}s (state: ${st})" >&2
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
}

# Through the proxy, a fresh connection must reach THIS color's boot — a 200 alone could be the old
# color still answering. Three hits in a row.
verify_serving() {
  local color=$1 boot=$2 tries=0 hits=0 body inst got
  while ((tries++ < 10)); do
    # shellcheck disable=SC2016  # expanded inside the proxy container
    body="$(compose exec -T gateway-proxy sh -c 'wget -q -O - "http://127.0.0.1:${GATEWAY_PORT}/health"' 2>/dev/null)" || body=""
    inst="$(printf '%s' "$body" | sed -n 's/.*"instance": *"\([a-z0-9-]*\)".*/\1/p')"
    got="$(printf '%s' "$body" | sed -n 's/.*"boot_id": *"\([A-Za-z0-9_-]*\)".*/\1/p')"
    if [[ "$inst" == "$color" && "$got" == "$boot" ]]; then
      hits=$((hits + 1))
      ((hits >= 3)) && return 0
    else
      hits=0
    fi
    sleep 1
  done
  log "the proxy does not answer as $(svc "$color") boot ${boot} (last: instance=${inst:-?} boot=${got:-?})"
  return 1
}

# Point nginx at a color: atomic file swap, then `nginx -t` + reload (or start the proxy). A refused
# config puts the previous file back.
switch_to() {
  local color=$1 previous="" tmp
  mkdir -p "$STATE_DIR"
  [[ -f "$ACTIVE_FILE" ]] && previous="$(cat "$ACTIVE_FILE")"
  tmp="$(mktemp "$STATE_DIR/.active.XXXXXX")"
  # shellcheck disable=SC2016  # nginx variable
  printf 'set $gateway_upstream %s;\n' "$(svc "$color")" >"$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$ACTIVE_FILE"
  if ! proxy_running; then
    log "starting gateway-proxy"
    compose up -d gateway-proxy && return 0
  elif compose exec -T gateway-proxy nginx -t -q && compose exec -T gateway-proxy nginx -s reload; then
    return 0
  fi
  if [[ -n "$previous" ]]; then printf '%s\n' "$previous" >"$ACTIVE_FILE"; else rm -f "$ACTIVE_FILE"; fi
  log "nginx refused the switch — $ACTIVE_FILE restored"
  return 1
}

# Wait until COLOR holds nothing (/health/drain all zeros, twice in a row), then stop it.
# TARGET/TARGET_BOOT (optional) is the drain target: if it disappears, the old color answers new
# sessions with 503 drain_target_unavailable, so the operator is told at once.
drain_and_stop() {
  local color=$1 target=${2:-} target_boot=${3:-}
  local deadline=$((SECONDS + DRAIN_TIMEOUT_SECONDS)) zero=0 known=0 last_log=-999
  local owned_since=-1 target_fail=0 next_target_check=$SECONDS st kind s left=""
  local draining active background pending forwarding open owned
  st="$(known_state "$color")" || exit 1
  kind="$(classify "$color" "$st")" || exit 1
  if [[ "$kind" == settled ]]; then
    [[ "$st" == none ]] || log "$(svc "$color") is already stopped (${st})"
    return 0
  fi
  log "waiting for $(svc "$color") to drain (up to ${DRAIN_TIMEOUT_SECONDS}s)"
  while :; do
    if s="$(drain_status_of "$color")"; then
      known=1
      read -r draining active background pending forwarding open owned <<<"$s"
      left="turns ${active} · background ${background} · cards ${pending} · forwarding ${forwarding} · requests ${open} · owned sessions ${owned}"
      if [[ "$draining" != 1 ]]; then
        die "$(svc "$color") is no longer draining (someone lifted it?) — not stopping it. Left: ${left}. Re-run 'scripts/deploy.sh drain ${color}' or 'rollback'"
      fi
      if ((active + background + pending + forwarding + open == 0)); then
        if ((owned == 0)); then
          zero=$((zero + 1))
          ((zero >= 2)) && break
        else
          zero=0
          ((owned_since >= 0)) || owned_since=$SECONDS
          if ((SECONDS - owned_since >= DRAIN_OWNED_GRACE_SECONDS)); then
            if [[ "$DRAIN_FORCE" == true ]]; then
              log "  $(svc "$color") still owns ${owned} session(s) with nothing in flight — --force: stopping it"
              break
            fi
            die "$(svc "$color") still owns ${owned} session(s) with nothing in flight for ${DRAIN_OWNED_GRACE_SECONDS}s — not stopping it (see its log for the session ids; 'scripts/deploy.sh drain ${color} --force' stops it)"
          fi
        fi
      else
        zero=0
        owned_since=-1
        if ((SECONDS - last_log >= 30)); then
          log "  left: ${left}"
          last_log=$SECONDS
        fi
      fi
    else
      if ((known || SECONDS - last_log >= 30)); then
        log "  /health/drain on $(svc "$color") did not answer — counts unknown, not stopping, asking again"
        last_log=$SECONDS
      fi
      known=0
      zero=0
    fi
    if [[ -n "$target" ]] && ((SECONDS >= next_target_check)); then
      next_target_check=$((SECONDS + 30))
      if [[ "$(boot_id_of "$target" 2>/dev/null || true)" == "$target_boot" ]]; then
        target_fail=0
      elif ((++target_fail >= 2)); then
        die "drain target $(svc "$target") (boot ${target_boot}) is gone — $(svc "$color") now answers new sessions with 503 drain_target_unavailable. Roll back ('scripts/deploy.sh rollback') or bring $(svc "$target") back and re-run 'scripts/deploy.sh drain ${color}'"
      fi
    fi
    if ((SECONDS >= deadline)); then
      if [[ "$DRAIN_FORCE" == true ]]; then
        log "  ${DRAIN_TIMEOUT_SECONDS}s passed — --force: stopping $(svc "$color") with ${left:-unknown counts} left"
        break
      fi
      if ((known)); then
        die "$(svc "$color") did not drain in ${DRAIN_TIMEOUT_SECONDS}s — left: ${left}. Traffic is on the new color; $(svc "$color") keeps running. Wait and run 'scripts/deploy.sh drain ${color}', or add --force to stop it now"
      fi
      die "could not read /health/drain on $(svc "$color") for ${DRAIN_TIMEOUT_SECONDS}s — not stopping it. Check it, then 'scripts/deploy.sh drain ${color}' (--force stops it without counts)"
    fi
    sleep "$POLL_SECONDS"
  done
  log "stopping $(svc "$color") (container and image kept for rollback)"
  compose --profile "$color" stop "$(svc "$color")"
}

# A color that is not active but still runs (an interrupted deploy, a rollback) is drained toward
# the active one before anything recreates it.
settle_idle() {
  local color=$1 active=$2 active_boot=$3 st kind s
  st="$(known_state "$color")" || exit 1
  kind="$(classify "$color" "$st")" || exit 1
  [[ "$kind" == running ]] || return 0
  log "$(svc "$color") is still running — finishing its drain first"
  if s="$(drain_status_of "$color")" && [[ "${s%% *}" == 1 ]]; then
    :
  else
    drain_start "$color" "$active" "$active_boot" ||
      die "could not drain $(svc "$color") toward $(svc "$active") — nothing changed"
  fi
  drain_and_stop "$color" "$active" "$active_boot"
}

take_lock() {
  mkdir -p "$STATE_DIR"
  exec 9>"$LOCK_FILE"
  flock -w "${DEPLOY_LOCK_WAIT:-15}" 9 || die "another deploy is running ($LOCK_FILE)"
}

# The active color must speak handoff + drain, or a new color beside it breaks §5.2.
gate_active() {
  local color=$1 h inst boot caps
  h="$(health_of "$color")" ||
    die "$(svc "$color") (active) did not answer /health — nothing changed. Check it ('scripts/deploy.sh status')"
  read -r inst boot caps <<<"$h"
  [[ "$inst" == "$color" ]] ||
    die "$ACTIVE_FILE says $(svc "$color") but it answers as instance '${inst}' — nothing changed"
  if ! has_capabilities "$caps" || [[ "$boot" == - ]]; then
    die "$(svc "$color") (active) runs a version without handoff/drain (capabilities: ${caps}) — a zero-downtime swap is refused (docs/zero-downtime-deploy.md §7). Use the restart deploy once: scripts/deploy.sh restart"
  fi
  printf '%s\n' "$boot"
}

# Undo a cutover that drained CURRENT toward TARGET but could not prove TARGET serves: lift CURRENT's
# drain, point nginx back, then retire TARGET the same careful way (it may already hold sessions
# CURRENT forwarded to it).
reverse_cutover() {
  local current=$1 current_boot=$2 target=$3
  log "reverting to $(svc "$current")"
  drain_stop "$current" ||
    die "could not lift the drain on $(svc "$current") — both colors left as they are. Check 'scripts/deploy.sh status'"
  if ! switch_to "$current" || ! verify_serving "$current" "$current_boot"; then
    die "reverting nginx to $(svc "$current") is not confirmed either — both colors left running. Check 'docker compose logs gateway-proxy' and $ACTIVE_FILE"
  fi
  if drain_start "$target" "$current" "$current_boot"; then
    drain_and_stop "$target" "$current" "$current_boot" || true
  else
    log "$(svc "$target") refused to drain — left running; stop it once it is idle"
  fi
}

# Steps 2–7 of §4 toward TARGET. CURRENT may be empty (fresh install).
cutover() {
  local target=$1 build=$2 reuse=${3:-0} reused=0 current current_boot="" st kind boot s
  current="$(active_color)"
  [[ "$target" != "$current" ]] || die "$(svc "$target") is already active"
  if [[ -n "$current" ]]; then
    current_boot="$(gate_active "$current")" || exit 1
  fi
  ensure_sidecars

  st="$(known_state "$target")" || exit 1
  if [[ "$reuse" == 1 && "$st" == running ]]; then
    log "$(svc "$target") is still running — reusing it"
    reused=1
    if s="$(drain_status_of "$target")" && [[ "${s%% *}" == 1 ]]; then
      drain_stop "$target" || die "could not lift the drain on $(svc "$target") — nothing changed"
    fi
  else
    if [[ -n "$current" ]]; then
      settle_idle "$target" "$current" "$current_boot"
    else
      kind="$(classify "$target" "$st")" || exit 1
      [[ "$kind" == settled ]] ||
        die "$(svc "$target") is running but no color is active ($ACTIVE_FILE missing) — check 'scripts/deploy.sh status'"
    fi
    if [[ "$build" == 1 ]]; then
      log "building $(svc "$target")"
      compose --profile "$target" build "$(svc "$target")"
    elif [[ "$build" == retag ]]; then
      # --no-build: run the ACTIVE build again (e.g. after a .env change), never the stale image
      # the idle color's tag still holds from two deploys ago.
      log "reusing the active build: tagging $(image_of "$current") as $(image_of "$target")"
      "$DOCKER" tag "$(image_of "$current")" "$(image_of "$target")"
    fi
    log "starting $(svc "$target") (traffic stays on ${current:-nothing})"
    if [[ "$reuse" == 1 ]]; then
      # rollback: the previous build is still tagged for this color — never rebuild or pull it.
      compose --profile "$target" up -d --no-deps --no-build --pull never "$(svc "$target")"
    else
      compose --profile "$target" up -d --no-deps --no-build --pull never --force-recreate "$(svc "$target")"
    fi
  fi

  if ! boot="$(wait_ready "$target")"; then
    compose --profile "$target" logs --tail 50 "$(svc "$target")" || true
    if ((reused)); then
      # Not ours to kill: it may still hold the previous deploy's turns. Put its drain back.
      drain_start "$target" "$current" "$current_boot" || true
      die "$(svc "$target") is not ready — not switched, traffic stays on $(svc "$current"); $(svc "$target") left running (later: 'scripts/deploy.sh drain ${target}')"
    fi
    compose --profile "$target" stop "$(svc "$target")" || true
    die "$(svc "$target") did not become ready — not switched, traffic stays on ${current:-nothing}"
  fi

  if [[ -n "$current" ]]; then
    # Drain BEFORE the switch (§4): from here the old color forwards new sessions to the pinned
    # target and lets idle sessions go, so the first turn the new color gets never collides.
    if ! drain_start "$current" "$target" "$boot"; then
      compose --profile "$target" stop "$(svc "$target")" || true
      die "$(svc "$current") refused to drain toward $(svc "$target") — not switched, $(svc "$target") stopped, traffic stays on $(svc "$current")"
    fi
  fi

  log "switching nginx: ${current:-nothing} → ${target}"
  if ! switch_to "$target" || ! verify_serving "$target" "$boot"; then
    if [[ -n "$current" ]]; then
      reverse_cutover "$current" "$current_boot" "$target"
    else
      compose --profile "$target" stop "$(svc "$target")" || true
    fi
    die "the switch to $(svc "$target") was not confirmed — traffic stays on ${current:-nothing}"
  fi
  log "new requests go to $(svc "$target") (confirmed through the proxy)"

  if [[ -n "$current" ]]; then
    drain_and_stop "$current" "$target" "$boot"
  fi
  log "done — active: $(svc "$target")"
}

# ── commands ─────────────────────────────────────────────────────────────────

cmd_deploy() {
  local build=$1 current
  take_lock
  warn_relay_base_url
  legacy_running && die "the legacy single '${LEGACY_SERVICE}' container is running — move it once with 'scripts/deploy.sh migrate'"
  current="$(active_color)"
  # A fresh install starts one color only, which uid mode allows; a swap beside it does not.
  if [[ -n "$current" ]] && uid_mode "$current"; then
    refuse_uid
  fi
  [[ "$build" == 0 && -n "$current" ]] && build=retag
  cutover "$(other "${current:-green}")" "$build"
}

cmd_rollback() {
  local current previous st
  take_lock
  current="$(active_color)"
  [[ -n "$current" ]] || die "no active color ($ACTIVE_FILE missing)"
  uid_mode "$current" && refuse_uid
  previous="$(other "$current")"
  st="$(known_state "$previous")" || exit 1
  [[ "$st" != none ]] || die "there is no $(svc "$previous") to roll back to"
  cutover "$previous" 0 1
}

cmd_drain() {
  local color=$1 active active_boot s
  take_lock
  [[ "$color" == blue || "$color" == green ]] || die "which color? scripts/deploy.sh drain blue|green"
  active="$(active_color)"
  [[ "$color" != "$active" ]] || die "$(svc "$color") is active — the color taking traffic is never drained"
  if [[ -n "$active" ]]; then
    active_boot="$(boot_id_of "$active")" || die "$(svc "$active") (active) did not answer /health"
    if [[ "$(known_state "$color")" == running ]]; then
      if ! { s="$(drain_status_of "$color")" && [[ "${s%% *}" == 1 ]]; }; then
        drain_start "$color" "$active" "$active_boot" || die "could not drain $(svc "$color")"
      fi
    fi
    drain_and_stop "$color" "$active" "$active_boot"
  else
    drain_and_stop "$color"
  fi
  log "done"
}

cmd_restart() {
  local build=$1 current st kind boot
  take_lock
  current="$(active_color)"
  [[ -n "$current" ]] || die "no active color ($ACTIVE_FILE missing) — 'scripts/deploy.sh migrate' or 'scripts/deploy.sh'"
  st="$(known_state "$(other "$current")")" || exit 1
  kind="$(classify "$(other "$current")" "$st")" || exit 1
  refuse_uid_downgrade "$current"
  [[ "$kind" == settled ]] ||
    die "$(svc "$(other "$current")") is still running — finish it first ('scripts/deploy.sh drain $(other "$current")')"
  ensure_sidecars
  if [[ "$build" == 1 ]]; then
    log "building $(svc "$current")"
    compose --profile "$current" build "$(svc "$current")"
  fi
  log "RESTART deploy: recreating $(svc "$current") in place — requests fail until it is back, in-flight turns and cards are lost"
  compose --profile "$current" up -d --no-deps --no-build --pull never --force-recreate "$(svc "$current")"
  boot="$(wait_ready "$current")" || {
    compose --profile "$current" logs --tail 50 "$(svc "$current")" || true
    die "$(svc "$current") did not become ready after the restart"
  }
  switch_to "$current" || die "nginx refused $ACTIVE_FILE"
  verify_serving "$current" "$boot" || die "the proxy does not answer as $(svc "$current")"
  log "done — active: $(svc "$current")"
}

cmd_migrate() {
  local build=$1 legacy uid=0 boot
  take_lock
  warn_relay_base_url
  [[ -z "$(active_color)" ]] || die "blue/green state already exists ($ACTIVE_FILE) — use 'scripts/deploy.sh'"
  legacy="$(legacy_container)"
  if [[ -z "$legacy" ]] || ! legacy_running; then
    die "no running legacy '${LEGACY_SERVICE}' container in project $(project_name) — for a fresh install use 'scripts/deploy.sh'"
  fi
  refuse_uid_downgrade "" "$legacy"
  uid_mode "" "$legacy" && uid=1
  ensure_sidecars
  if [[ "$build" == 1 ]]; then
    log "building gateway-blue"
    compose --profile blue build gateway-blue
  fi
  log "RESTART deploy (once): the host port moves from the legacy container to gateway-proxy"
  if ((uid)); then
    # The uid registry admits one gateway process: blue cannot start beside the legacy container.
    log "uid mode: stopping the legacy container first — the gateway is down until gateway-blue is ready"
    "$DOCKER" stop -t 35 "$legacy" >/dev/null
  fi
  compose --profile blue up -d --no-deps --no-build --pull never gateway-blue
  if ! boot="$(wait_ready blue)"; then
    compose --profile blue logs --tail 50 gateway-blue || true
    compose --profile blue stop gateway-blue || true
    ((uid)) && "$DOCKER" start "$legacy" >/dev/null
    die "gateway-blue did not become ready — the legacy container keeps serving"
  fi
  if ((!uid)); then
    log "stopping the legacy container — gateway-proxy takes the port over"
    "$DOCKER" stop -t 35 "$legacy" >/dev/null
  fi
  if ! switch_to blue || ! verify_serving blue "$boot"; then
    compose stop gateway-proxy || true
    rm -f "$ACTIVE_FILE"
    compose --profile blue stop gateway-blue || true
    "$DOCKER" start "$legacy" >/dev/null
    die "the proxy could not confirm gateway-blue — the legacy container is back. Check 'docker compose logs gateway-proxy'"
  fi
  "$DOCKER" rm "$legacy" >/dev/null
  log "done — active: gateway-blue. From now on: scripts/deploy.sh"
}

cmd_status() {
  local active color st h d line boot caps draining active_t background pending forwarding open owned
  active="$(active_color)"
  echo "active color: ${active:-none}"
  for color in blue green; do
    st="$(state_of "$color")" || st="?(lookup failed)"
    line=""
    if [[ "$st" == running ]]; then
      if h="$(health_of "$color")"; then
        read -r _ boot caps <<<"$h"
        line="boot ${boot} [${caps}]"
      else
        line="/health: no answer"
      fi
      if d="$(drain_status_of "$color")"; then
        read -r draining active_t background pending forwarding open owned <<<"$d"
        line+=" · $([[ "$draining" == 1 ]] && echo DRAINING || echo serving) · turns ${active_t} · background ${background} · cards ${pending} · forwarding ${forwarding} · requests ${open} · owned ${owned}"
      fi
    fi
    printf '  %-14s %-10s %s%s\n' "$(svc "$color")" "$st" "$line" \
      "$([[ "$color" == "$active" ]] && echo '   <- traffic')"
  done
  if proxy_running; then echo "  gateway-proxy  running"; else echo "  gateway-proxy  stopped"; fi
  legacy_running && echo "  ${LEGACY_SERVICE} (legacy single container) running — run 'scripts/deploy.sh migrate' once"
  return 0
}

main() {
  local cmd="${1:-deploy}" build=1 color=""
  [[ $# -gt 0 ]] && shift
  if [[ "$cmd" == --* ]]; then
    set -- "$cmd" "$@"
    cmd=deploy
  fi
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-build) build=0 ;;
      --force) DRAIN_FORCE=true ;;
      blue | green) color=$1 ;;
      *) die "unknown option: $1" ;;
    esac
    shift
  done
  case "$cmd" in
    deploy) cmd_deploy "$build" ;;
    rollback) cmd_rollback ;;
    drain) cmd_drain "$color" ;;
    restart) cmd_restart "$build" ;;
    migrate) cmd_migrate "$build" ;;
    status) cmd_status ;;
    active)
      color="$(active_color)"
      [[ -n "$color" ]] || die "no active color"
      svc "$color"
      echo
      ;;
    -h | --help | help) sed -n '2,35p' "$0" ;;
    *) die "unknown command: $cmd (deploy | rollback | drain | restart | migrate | status | active)" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
