"""scripts/deploy.sh — the blue/green safety rules, run as the real script.

Docker is replaced by a fake binary (``DOCKER=<tmp>/bin/docker``) that records every call and
answers from small state files, and ``sleep`` only advances bash's ``SECONDS``, so an hour-long
drain finishes at once. Each test drives the REAL ``main`` and asserts the order of the side effects
it asked for (docs/zero-downtime-deploy.md §4 / §7 배포 gate / §8):

* the old color is drained (``POST /internal/drain`` pinned to the new boot id) BEFORE nginx is
  switched, and stopped only after ``/health/drain`` reads all zeros twice;
* an active color without the handoff/drain capabilities, and uid mode, are refused before
  anything is built or started;
* a drain that does not finish in time leaves the old color running unless ``--force``;
* rollback lifts the old color's drain before traffic goes back; migrate is a restart deploy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "deploy.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("flock") is None,
    reason="needs bash + flock",
)

# The fake docker. State lives in $T: state_<color> (none|running|exited|paused),
# proxy_state, legacy_state, draining_<color>, boot_<color>, and per-probe scripted answers
# <op>_<color> (one line per call, the last line repeats; FAIL = the call fails).
_FAKE_DOCKER = r"""#!/usr/bin/env bash
T="__T__"
ACTIVE="$T/repo/docker/nginx/state/active.conf"
pop() {
  local f="$T/$1" line
  [[ -f "$f" ]] || return 3
  line="$(head -n1 "$f")"
  if [[ "$(wc -l <"$f")" -gt 1 ]]; then sed -i 1d "$f"; fi
  printf '%s\n' "$line"
}
get() { if [[ -f "$T/$1" ]]; then cat "$T/$1"; else printf '%s\n' "$2"; fi; }
note() { printf '%s\n' "$*" >>"$T/calls"; }

if [[ "$1" == compose ]]; then
  shift
  rest=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profile | -f | -p) shift 2 ;;
      *) rest+=("$1"); shift ;;
    esac
  done
  set -- "${rest[@]}"
  sub=$1
  shift
  if [[ "$sub" == exec ]]; then
    [[ "$1" == -T ]] && shift
    service=$1
    shift
    if [[ "$service" == gateway-proxy ]]; then
      if [[ "$1" == nginx ]]; then
        note "compose exec gateway-proxy $*"
        [[ ! -f "$T/nginxfail" ]]
        exit
      fi
      note "compose exec gateway-proxy health"
      [[ "$(get proxy_state none)" == running ]] || exit 1
      color="$(sed -n 's/^set \$gateway_upstream gateway-\(blue\|green\);$/\1/p' "$ACTIVE" 2>/dev/null)"
      [[ -n "$color" ]] || color=blue
      boot="$(get "boot_$color" "boot-$color")"
      [[ -f "$T/proxybad_$color" ]] && boot=stale
      printf '{"status":"healthy","instance":"%s","boot_id":"%s"}\n' "$color" "$boot"
      exit 0
    fi
    color="${service#gateway-}"
    # python -I -c CODE op args...
    op=$5
    shift 5
    note "probe $color $op $*"
    [[ "$(get "state_$color" none)" == running ]] || exit 1
    if line="$(pop "${op}_$color")"; then
      [[ "$line" != FAIL ]] || exit 1
      case "$op" in
        drain-start) echo 1 >"$T/draining_$color" ;;
        drain-stop) echo 0 >"$T/draining_$color" ;;
      esac
      [[ "$op" == ready ]] || printf '%s\n' "$line"
      exit 0
    fi
    case "$op" in
      health) echo "$color $(get "boot_$color" "boot-$color") handoff,drain" ;;
      ready) : ;;
      drain-status) echo "$(get "draining_$color" 0) 0 0 0 0 0 0" ;;
      drain-start) echo 1 >"$T/draining_$color"; echo "200 {}" ;;
      drain-stop) echo 0 >"$T/draining_$color"; echo "200 {}" ;;
      isolation-mode) get "isolation_$color" "" ;;
    esac
    exit 0
  fi
  note "compose $sub $*"
  service="${*: -1}"
  case "$sub" in
    config)
      if [[ "$*" == *--services* ]]; then
        echo gateway-proxy
        [[ -f "$T/logging" ]] && echo gateway-log
      else
        get config "name: proj"
      fi
      ;;
    ps)
      if [[ "$service" == gateway-proxy ]]; then
        [[ "$(get proxy_state none)" == running ]] && echo cid-proxy
      else
        st="$(get "state_${service#gateway-}" none)"
        [[ "$st" != none ]] && echo "cid-${service#gateway-}"
      fi
      exit 0
      ;;
    up)
      if [[ "$service" == gateway-proxy ]]; then
        echo running >"$T/proxy_state"
      elif [[ "$service" == gateway-blue || "$service" == gateway-green ]]; then
        [[ -f "$T/upfail_${service#gateway-}" ]] && exit 1
        echo running >"$T/state_${service#gateway-}"
        echo 0 >"$T/draining_${service#gateway-}"
      fi
      ;;
    stop)
      if [[ "$service" == gateway-proxy ]]; then
        echo exited >"$T/proxy_state"
      else
        echo exited >"$T/state_${service#gateway-}"
      fi
      ;;
  esac
  exit 0
fi

note "docker $*"
case "$1" in
  inspect)
    cid="${*: -1}"
    if [[ "$*" == *Config.Env* ]]; then
      get legacy_env ""
    elif [[ "$cid" == cid-proxy ]]; then
      [[ "$(get proxy_state none)" == running ]] && echo true || echo false
    else
      get "state_${cid#cid-}" none
    fi
    ;;
  ps)
    if [[ "$*" == *com.docker.compose.service=gateway* ]]; then
      st="$(get legacy_state none)"
      if [[ "$*" == *" -a "* ]]; then
        [[ "$st" != none ]] && echo cid-legacy
      else
        [[ "$st" == running ]] && echo cid-legacy
      fi
    fi
    ;;
  stop) echo exited >"$T/legacy_state" ;;
  start) echo running >"$T/legacy_state" ;;
  rm) echo none >"$T/legacy_state" ;;
esac
exit 0
"""

_HARNESS = r"""
source "$T/repo/scripts/deploy.sh"
sleep() { SECONDS=$((SECONDS + ${1%.*})); }
main "$@"
"""


def _setup(tmp_path: Path, active: str | None = "blue", **files: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "docker" / "nginx" / "state").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "deploy.sh")
    (repo / ".env").write_text(files.pop("env", "ADMIN_API_KEY=x\n"))
    if active:
        (repo / "docker" / "nginx" / "state" / "active.conf").write_text(
            f"set $gateway_upstream gateway-{active};\n"
        )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text(_FAKE_DOCKER.replace("__T__", str(tmp_path)))
    fake.chmod(0o755)
    defaults = {"proxy_state": "running"}
    if active:
        defaults[f"state_{active}"] = "running"
    for name, content in {**defaults, **files}.items():
        lines = [line.strip() for line in content.strip().splitlines()]
        (tmp_path / name).write_text("\n".join(lines) + "\n")
    return repo


def _run(tmp_path: Path, *args: str, env: dict | None = None):
    proc = subprocess.run(  # noqa: PLW1510 — the tests read the exit code themselves
        ["bash", "-c", _HARNESS, "deploy.sh", *args],
        env={
            "PATH": "/usr/bin:/bin",
            "T": str(tmp_path),
            "DOCKER": str(tmp_path / "bin" / "docker"),
            "DEPLOY_LOCK_WAIT": "1",
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls_file = tmp_path / "calls"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return proc, calls


def _index(calls: list[str], needle: str) -> int:
    for i, line in enumerate(calls):
        if needle in line:
            return i
    return -1


def _active(tmp_path: Path) -> str:
    path = tmp_path / "repo" / "docker" / "nginx" / "state" / "active.conf"
    return path.read_text() if path.exists() else ""


def _out(proc) -> str:
    return proc.stdout + proc.stderr


# ── the happy path: drain before switch ──


def test_deploy_drains_the_old_color_before_switching_and_stops_it_last(tmp_path):
    _setup(tmp_path, boot_green="g-boot-1")
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, _out(proc)

    build = _index(calls, "compose build gateway-green")
    up = _index(calls, "compose up -d --no-deps --no-build --pull never --force-recreate gateway-green")
    ready = _index(calls, "probe green ready")
    drain = _index(calls, "probe blue drain-start green g-boot-1")
    reload = _index(calls, "compose exec gateway-proxy nginx -s reload")
    verify = _index(calls, "compose exec gateway-proxy health")
    stop = _index(calls, "compose stop gateway-blue")
    assert -1 not in (build, up, ready, drain, reload, verify, stop), calls
    assert build < up < ready < drain < reload < verify < stop
    # The gate looked at the active color before anything was built.
    assert _index(calls, "probe blue health") < build
    # Stopped only after two all-zero drain reads, both after the switch.
    statuses = [i for i, c in enumerate(calls) if c.startswith("probe blue drain-status")]
    assert len([i for i in statuses if reload < i < stop]) >= 2
    assert _active(tmp_path) == "set $gateway_upstream gateway-green;\n"
    assert _index(calls, "compose stop gateway-green") == -1


def test_no_build_swaps_in_the_active_build_not_the_stale_idle_tag(tmp_path):
    _setup(tmp_path, env="ADMIN_API_KEY=x\nGATEWAY_IMAGE=corp/gw\n")
    proc, calls = _run(tmp_path, "--no-build")
    assert proc.returncode == 0, _out(proc)
    assert _index(calls, "compose build") == -1
    tag = _index(calls, "docker tag corp/gw:blue corp/gw:green")
    up = _index(calls, "--force-recreate gateway-green")
    assert -1 not in (tag, up) and tag < up < _index(calls, "drain-start")


def test_fresh_install_starts_blue_behind_the_proxy_without_a_drain(tmp_path):
    _setup(tmp_path, active=None, proxy_state="none")
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, _out(proc)
    assert _index(calls, "drain-start") == -1
    assert _index(calls, "compose up -d gateway-proxy") > _index(calls, "probe blue ready")
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


def test_deploy_alternates_colors(tmp_path):
    _setup(tmp_path, active="green")
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, _out(proc)
    assert _index(calls, "probe green drain-start blue boot-blue") != -1
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


# ── the gate ──


def test_an_active_color_without_handoff_and_drain_is_refused(tmp_path):
    _setup(tmp_path, health_blue="blue - -")
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert "restart" in _out(proc) and "handoff" in _out(proc)
    assert _index(calls, "gateway-green") == -1  # nothing built, started or drained
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


def test_an_unreachable_active_color_is_refused(tmp_path):
    _setup(tmp_path, health_blue="FAIL")
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert _index(calls, "gateway-green") == -1


@pytest.mark.parametrize(
    "files",
    [
        {"env": "ADMIN_API_KEY=x\nCLAUDE_ISOLATION_MODE=uid\n"},
        {"env": "ADMIN_API_KEY=x\nexport CLAUDE_ISOLATION_MODE='uid'\n"},
        # The uid overlay sets it per service — visible in the rendered config.
        {"config": "name: proj\nservices:\n  gateway-blue:\n    environment:\n      CLAUDE_ISOLATION_MODE: uid"},
        # Overlay forgotten on this run, but the running color is in uid mode.
        {"isolation_blue": "uid"},
    ],
)
def test_uid_mode_is_refused_before_anything_changes(tmp_path, files):
    _setup(tmp_path, **files)
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert "uid" in _out(proc) and "restart" in _out(proc)
    assert _index(calls, "gateway-green") == -1
    assert _index(calls, "drain-start") == -1


def test_a_fresh_uid_install_starts_its_single_color(tmp_path):
    _setup(
        tmp_path,
        active=None,
        proxy_state="none",
        env="ADMIN_API_KEY=x\nCLAUDE_ISOLATION_MODE=uid\n",
    )
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, _out(proc)
    assert _index(calls, "--force-recreate gateway-blue") != -1
    assert _index(calls, "gateway-green") == -1


def test_uid_mode_from_the_shell_environment_is_refused(tmp_path):
    _setup(tmp_path)
    proc, calls = _run(tmp_path, env={"CLAUDE_ISOLATION_MODE": "uid"})
    assert proc.returncode != 0
    assert _index(calls, "gateway-green") == -1


def test_rollback_is_refused_in_uid_mode(tmp_path):
    _setup(tmp_path, active="green", state_blue="exited", isolation_green="uid")
    proc, calls = _run(tmp_path, "rollback")
    assert proc.returncode != 0
    assert _index(calls, "drain-start") == -1


def test_a_running_legacy_container_must_be_migrated_first(tmp_path):
    _setup(tmp_path, legacy_state="running")
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert "migrate" in _out(proc)
    assert _index(calls, "gateway-green") == -1


# ── failures before the switch leave traffic where it was ──


def test_a_new_color_that_never_gets_ready_is_stopped_and_traffic_stays(tmp_path):
    _setup(tmp_path, ready_green="FAIL")
    proc, calls = _run(tmp_path, env={"HEALTH_TIMEOUT_SECONDS": "30"})
    assert proc.returncode != 0
    assert _index(calls, "drain-start") == -1
    assert _index(calls, "nginx -s reload") == -1
    assert _index(calls, "compose stop gateway-green") != -1
    assert _index(calls, "compose stop gateway-blue") == -1


def test_a_new_color_without_the_capabilities_is_not_switched_to(tmp_path):
    _setup(tmp_path, health_green="green g-boot -")
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert _index(calls, "drain-start") == -1
    assert _index(calls, "compose stop gateway-green") != -1


def test_a_refused_drain_means_no_switch(tmp_path):
    _setup(tmp_path, **{"drain-start_blue": "FAIL"})
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert _index(calls, "nginx -s reload") == -1
    assert _index(calls, "compose stop gateway-green") != -1
    assert _index(calls, "compose stop gateway-blue") == -1
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


def test_an_unconfirmed_switch_reverts_and_retires_the_new_color(tmp_path):
    _setup(tmp_path, proxybad_green="1")
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    drain = _index(calls, "probe blue drain-start green")
    lift = _index(calls, "probe blue drain-stop")
    redrain = _index(calls, "probe green drain-start blue boot-blue")
    stop_green = _index(calls, "compose stop gateway-green")
    assert -1 not in (drain, lift, redrain, stop_green), calls
    assert drain < lift < redrain < stop_green
    assert _index(calls, "compose stop gateway-blue") == -1
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


def test_a_refused_nginx_config_restores_the_state_file(tmp_path):
    _setup(tmp_path, nginxfail="1")
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert _index(calls, "compose stop gateway-blue") == -1
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


# ── drain wait ──


def test_a_failed_drain_probe_is_unknown_never_zero(tmp_path):
    _setup(
        tmp_path,
        **{
            "drain-status_blue": """
                1 1 0 0 0 0 1
                1 0 0 0 0 0 0
                FAIL
                1 0 0 0 0 0 0
                1 0 0 0 0 0 0
            """
        },
    )
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, _out(proc)
    stop = _index(calls, "compose stop gateway-blue")
    reads = [c for c in calls[:stop] if c.startswith("probe blue drain-status")]
    # The zero before the failure did not count: two zeros AFTER it were needed.
    assert len(reads) == 5


def test_drain_timeout_leaves_the_old_color_running(tmp_path):
    _setup(tmp_path, **{"drain-status_blue": "1 1 0 1 0 0 1"})
    proc, calls = _run(tmp_path, env={"DRAIN_TIMEOUT_SECONDS": "60"})
    assert proc.returncode != 0
    out = _out(proc)
    assert "turns 1" in out and "cards 1" in out and "--force" in out
    assert _index(calls, "compose stop gateway-blue") == -1
    # Traffic already moved — the timeout is about the old color only.
    assert _active(tmp_path) == "set $gateway_upstream gateway-green;\n"


def test_drain_timeout_with_force_stops_the_old_color(tmp_path):
    _setup(tmp_path, **{"drain-status_blue": "1 1 0 0 0 0 1"})
    proc, calls = _run(tmp_path, "--force", env={"DRAIN_TIMEOUT_SECONDS": "60"})
    assert proc.returncode == 0, _out(proc)
    assert _index(calls, "compose stop gateway-blue") != -1


def test_drain_timeout_with_unknown_counts_is_not_a_stop(tmp_path):
    _setup(tmp_path, **{"drain-status_blue": "FAIL"})
    proc, calls = _run(tmp_path, env={"DRAIN_TIMEOUT_SECONDS": "60"})
    assert proc.returncode != 0
    assert _index(calls, "compose stop gateway-blue") == -1


def test_sessions_still_owned_with_nothing_in_flight_are_reported(tmp_path):
    _setup(tmp_path, **{"drain-status_blue": "1 0 0 0 0 0 2"})
    proc, calls = _run(tmp_path, env={"DRAIN_OWNED_GRACE_SECONDS": "30"})
    assert proc.returncode != 0
    assert "owns 2 session" in _out(proc)
    assert _index(calls, "compose stop gateway-blue") == -1


def test_a_lifted_drain_is_not_stopped(tmp_path):
    _setup(
        tmp_path,
        **{"drain-status_blue": "1 1 0 0 0 0 1\n0 0 0 0 0 0 0"},
    )
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert "no longer draining" in _out(proc)
    assert _index(calls, "compose stop gateway-blue") == -1


def test_a_vanished_drain_target_raises_the_alarm(tmp_path):
    _setup(
        tmp_path,
        # wait_ready reads green's /health once; the drain loop's target checks then fail.
        health_green="green boot-green handoff,drain\nFAIL",
        **{"drain-status_blue": "1 1 0 0 0 0 1"},
    )
    proc, calls = _run(tmp_path)
    assert proc.returncode != 0
    assert "drain_target_unavailable" in _out(proc) and "rollback" in _out(proc)
    assert _index(calls, "compose stop gateway-blue") == -1


def test_an_interrupted_drain_is_finished_before_the_color_is_rebuilt(tmp_path):
    # A previous blue→green deploy stopped mid-drain: green is active, blue still drains.
    _setup(
        tmp_path,
        active="green",
        state_blue="running",
        draining_blue="1",
        **{"drain-status_blue": "1 1 0 0 0 0 1\n1 0 0 0 0 0 0"},
    )
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, _out(proc)
    stop_blue = _index(calls, "compose stop gateway-blue")
    build_blue = _index(calls, "compose build gateway-blue")
    assert -1 not in (stop_blue, build_blue)
    assert stop_blue < build_blue
    # It was already draining — no second POST toward green before it stopped.
    assert _index(calls[:stop_blue], "probe blue drain-start") == -1


def test_drain_subcommand_resumes_and_stops(tmp_path):
    _setup(tmp_path, active="green", state_blue="running", draining_blue="1")
    proc, calls = _run(tmp_path, "drain", "blue")
    assert proc.returncode == 0, _out(proc)
    assert _index(calls, "compose stop gateway-blue") != -1
    assert _index(calls, "compose stop gateway-green") == -1


def test_the_active_color_is_never_drained_by_the_subcommand(tmp_path):
    _setup(tmp_path, active="green")
    proc, calls = _run(tmp_path, "drain", "green")
    assert proc.returncode != 0
    assert _index(calls, "drain-start") == -1


# ── rollback ──


def test_rollback_during_drain_lifts_it_before_traffic_goes_back(tmp_path):
    _setup(
        tmp_path,
        active="green",
        state_blue="running",
        draining_blue="1",
    )
    proc, calls = _run(tmp_path, "rollback")
    assert proc.returncode == 0, _out(proc)
    lift = _index(calls, "probe blue drain-stop")
    drain_green = _index(calls, "probe green drain-start blue boot-blue")
    reload = _index(calls, "nginx -s reload")
    stop_green = _index(calls, "compose stop gateway-green")
    assert -1 not in (lift, drain_green, reload, stop_green), calls
    assert lift < drain_green < reload < stop_green
    # The previous build is reused as is: never rebuilt, recreated or stopped.
    assert _index(calls, "build gateway-blue") == -1
    assert not any(c.startswith("compose up") and "gateway-blue" in c for c in calls)
    assert _index(calls, "compose stop gateway-blue") == -1
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


def test_rollback_after_the_old_color_stopped_restarts_its_old_image(tmp_path):
    _setup(tmp_path, active="green", state_blue="exited")
    proc, calls = _run(tmp_path, "rollback")
    assert proc.returncode == 0, _out(proc)
    up = _index(calls, "compose up -d --no-deps --no-build --pull never gateway-blue")
    assert up != -1
    assert _index(calls, "--force-recreate gateway-blue") == -1
    assert _index(calls, "compose build") == -1
    assert up < _index(calls, "probe green drain-start blue") < _index(calls, "nginx -s reload")
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


def test_rollback_without_a_previous_color_is_refused(tmp_path):
    _setup(tmp_path, active="green")
    proc, calls = _run(tmp_path, "rollback")
    assert proc.returncode != 0
    assert _index(calls, "drain-start") == -1


def test_a_reused_color_that_is_not_ready_is_put_back_to_draining(tmp_path):
    _setup(
        tmp_path,
        active="green",
        state_blue="running",
        draining_blue="1",
        ready_blue="FAIL",
    )
    proc, calls = _run(tmp_path, "rollback", env={"HEALTH_TIMEOUT_SECONDS": "20"})
    assert proc.returncode != 0
    lift = _index(calls, "probe blue drain-stop")
    redrain = _index(calls, "probe blue drain-start green")
    assert -1 not in (lift, redrain) and lift < redrain
    assert _index(calls, "compose stop gateway-blue") == -1
    assert _active(tmp_path) == "set $gateway_upstream gateway-green;\n"


# ── migrate (restart deploy, once) ──


def test_migrate_starts_blue_before_the_legacy_container_gives_up_the_port(tmp_path):
    _setup(tmp_path, active=None, legacy_state="running", proxy_state="none")
    proc, calls = _run(tmp_path, "migrate")
    assert proc.returncode == 0, _out(proc)
    up_blue = _index(calls, "compose up -d --no-deps --no-build --pull never gateway-blue")
    ready = _index(calls, "probe blue ready")
    stop_legacy = _index(calls, "docker stop -t 35 cid-legacy")
    up_proxy = _index(calls, "compose up -d gateway-proxy")
    verify = _index(calls, "compose exec gateway-proxy health")
    rm_legacy = _index(calls, "docker rm cid-legacy")
    assert -1 not in (up_blue, ready, stop_legacy, up_proxy, verify, rm_legacy), calls
    assert up_blue < ready < stop_legacy < up_proxy < verify < rm_legacy
    assert "RESTART deploy" in _out(proc)
    assert _active(tmp_path) == "set $gateway_upstream gateway-blue;\n"


def test_migrate_in_uid_mode_stops_the_legacy_container_first(tmp_path):
    _setup(
        tmp_path,
        active=None,
        legacy_state="running",
        proxy_state="none",
        env="ADMIN_API_KEY=x\nCLAUDE_ISOLATION_MODE=uid\n",
    )
    proc, calls = _run(tmp_path, "migrate")
    assert proc.returncode == 0, _out(proc)
    assert _index(calls, "docker stop -t 35 cid-legacy") < _index(
        calls, "compose up -d --no-deps --no-build --pull never gateway-blue"
    )


def test_migrate_keeps_the_legacy_container_when_blue_is_not_ready(tmp_path):
    _setup(
        tmp_path, active=None, legacy_state="running", proxy_state="none", ready_blue="FAIL"
    )
    proc, calls = _run(tmp_path, "migrate", env={"HEALTH_TIMEOUT_SECONDS": "20"})
    assert proc.returncode != 0
    assert _index(calls, "docker stop -t 35 cid-legacy") == -1
    assert _index(calls, "compose up -d gateway-proxy") == -1
    assert _active(tmp_path) == ""


def test_migrate_puts_the_legacy_container_back_when_the_proxy_fails(tmp_path):
    _setup(
        tmp_path, active=None, legacy_state="running", proxy_state="none", proxybad_blue="1"
    )
    proc, calls = _run(tmp_path, "migrate")
    assert proc.returncode != 0
    assert _index(calls, "docker start cid-legacy") > _index(calls, "docker stop -t 35 cid-legacy")
    assert _index(calls, "docker rm cid-legacy") == -1
    assert _active(tmp_path) == ""


def test_migrate_without_a_legacy_container_is_refused(tmp_path):
    _setup(tmp_path, active=None, proxy_state="none")
    proc, calls = _run(tmp_path, "migrate")
    assert proc.returncode != 0
    assert _index(calls, "gateway-blue") == -1


def test_migrate_twice_is_refused(tmp_path):
    _setup(tmp_path, legacy_state="running")
    proc, _ = _run(tmp_path, "migrate")
    assert proc.returncode != 0
    assert "already" in _out(proc)


# ── restart deploy, status, lock ──


def test_restart_recreates_the_active_color_in_place(tmp_path):
    _setup(tmp_path, active="blue", state_green="exited")
    proc, calls = _run(tmp_path, "restart")
    assert proc.returncode == 0, _out(proc)
    build = _index(calls, "compose build gateway-blue")
    up = _index(calls, "--force-recreate gateway-blue")
    assert -1 not in (build, up) and build < up
    assert _index(calls, "drain-start") == -1
    assert not any(c.startswith("compose up") and "gateway-green" in c for c in calls)
    assert _index(calls, "compose stop gateway-blue") == -1


def test_restart_refuses_to_drop_uid_mode_when_the_overlay_is_missing(tmp_path):
    _setup(tmp_path, active="blue", isolation_blue="uid")
    proc, calls = _run(tmp_path, "restart")
    assert proc.returncode != 0
    assert "docker-compose.uid.yml" in _out(proc)
    assert _index(calls, "--force-recreate") == -1 and _index(calls, "compose build") == -1


def test_restart_in_uid_mode_with_the_overlay(tmp_path):
    _setup(
        tmp_path,
        active="blue",
        isolation_blue="uid",
        config="name: proj\n      CLAUDE_ISOLATION_MODE: uid",
    )
    proc, calls = _run(tmp_path, "restart")
    assert proc.returncode == 0, _out(proc)
    assert _index(calls, "--force-recreate gateway-blue") != -1


def test_migrate_refuses_to_drop_uid_mode_of_the_legacy_container(tmp_path):
    _setup(
        tmp_path,
        active=None,
        legacy_state="running",
        proxy_state="none",
        legacy_env="PATH=/usr/bin\nCLAUDE_ISOLATION_MODE=uid",
    )
    proc, calls = _run(tmp_path, "migrate")
    assert proc.returncode != 0
    assert "docker-compose.uid.yml" in _out(proc)
    assert _index(calls, "gateway-blue") == -1
    assert _index(calls, "docker stop") == -1


def test_restart_refuses_while_the_other_color_still_runs(tmp_path):
    _setup(tmp_path, active="blue", state_green="running")
    proc, calls = _run(tmp_path, "restart")
    assert proc.returncode != 0
    assert _index(calls, "--force-recreate") == -1


def test_status_reports_both_colors(tmp_path):
    _setup(tmp_path, active="green", state_blue="running", draining_blue="1")
    proc, _ = _run(tmp_path, "status")
    assert proc.returncode == 0, _out(proc)
    assert "active color: green" in proc.stdout
    assert "DRAINING" in proc.stdout and "<- traffic" in proc.stdout


def test_active_prints_the_service_name(tmp_path):
    _setup(tmp_path, active="green")
    proc, _ = _run(tmp_path, "active")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "gateway-green"


def test_a_second_deploy_waits_for_the_lock_and_gives_up(tmp_path):
    repo = _setup(tmp_path)
    lock = repo / "docker" / "nginx" / "state" / ".deploy.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "10"])
    try:
        for _ in range(50):
            if lock.exists():
                break
            subprocess.run(["sleep", "0.05"], check=True)
        proc, calls = _run(tmp_path)
    finally:
        holder.kill()
        holder.wait()
    assert proc.returncode != 0
    assert "another deploy" in _out(proc)
    assert _index(calls, "gateway-green") == -1


def test_a_relay_base_url_in_env_is_warned_about(tmp_path):
    _setup(tmp_path, env="ADMIN_API_KEY=x\nMCP_RELAY_BASE_URL=http://gateway-proxy:8000\n")
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, _out(proc)
    assert "MCP_RELAY_BASE_URL" in _out(proc)


# ── the loopback client that runs inside the color ──


def test_probe_client_never_puts_the_admin_key_on_a_command_line(tmp_path):
    text = SCRIPT.read_text()
    # The shell never expands the key: only the in-container client reads it from its env.
    assert "$ADMIN_API_KEY" not in text and "${ADMIN_API_KEY" not in text
    assert 'os.environ.get("ADMIN_API_KEY"' in text
    # Corporate proxies in the container must never see the loopback calls.
    assert "ProxyHandler({})" in text


def test_probe_client_speaks_the_contract(tmp_path):
    """Run the embedded client against a stub gateway (the contract's JSON shapes)."""
    import http.server
    import json
    import os
    import re
    import threading

    seen: list[tuple[str, str, dict, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def _send(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            seen.append(
                (self.command, self.path, body, self.headers.get("Authorization", ""))
            )

        def do_GET(self):
            self._record()
            if self.path == "/health":
                self._send(
                    200,
                    {
                        "status": "healthy",
                        "instance": "green",
                        "boot_id": "ab12",
                        "capabilities": ["handoff", "drain"],
                    },
                )
            elif self.path == "/health/ready":
                self._send(503, {"status": "not_ready"})
            elif self.path == "/health/drain":
                self._send(
                    200,
                    {
                        "instance": "green",
                        "draining": True,
                        "drain_target": {"instance": "blue"},
                        "active_turns": 2,
                        "background_turns": 1,
                        "pending_interactions": 0,
                        "forwarding": 3,
                        "open_requests": 0,
                        "owned_sessions": 4,
                    },
                )

        def do_POST(self):
            self._record()
            self._send(200, {"draining": True, "drain_target": {}})

        def do_DELETE(self):
            self._record()
            self._send(200, {"draining": False})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    code = re.search(r"PY_PROBE='(.*?)\n'\n", SCRIPT.read_text(), re.S).group(1)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PORT": str(server.server_address[1]),
        "ADMIN_API_KEY": "sekret",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "http_proxy": "http://127.0.0.1:9",
    }

    def run(*args):
        import sys

        return subprocess.run(
            [sys.executable, "-I", "-c", code, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    try:
        assert run("health").stdout.split() == ["green", "ab12", "handoff,drain"]
        assert run("ready").returncode == 1
        assert run("drain-status").stdout.split() == ["1", "2", "1", "0", "3", "0", "4"]
        assert run("drain-start", "blue", "cd34").returncode == 0
        assert run("drain-stop").returncode == 0
    finally:
        server.shutdown()
    post = next(s for s in seen if s[0] == "POST")
    assert post[1] == "/internal/drain"
    assert post[2] == {"target": "blue", "target_boot_id": "cd34"}
    assert post[3] == "Bearer sekret"
    delete = next(s for s in seen if s[0] == "DELETE")
    assert delete[1] == "/internal/drain" and delete[3] == "Bearer sekret"
    # Read-only probes carry no credential.
    assert all(s[3] == "" for s in seen if s[0] == "GET")
