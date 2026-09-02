# Codex P0 experiments

This directory contains **evidence-gathering probes for issue #164**. It is intentionally outside `src/backends/codex`: the existing Codex backend is frozen/stale and must not be extended before the P0 production gate selects a replacement architecture.

Parent architecture discussion: #162

## Ownership/resource probe

`ownership_probe.py` talks directly to a version-pinned `codex app-server --listen stdio://`. It does **not** start a model turn, so it is suitable for measuring runtime/thread-store behavior separately from model-provider compatibility.

Run:

```bash
uv run python experiments/codex_p0/ownership_probe.py \
  --codex-bin /path/to/codex \
  --output artifacts/codex-p0-ownership.json
```

Optional:

```bash
# Avoid the 1/10/50/100-thread density sweep while debugging ownership.
uv run python experiments/codex_p0/ownership_probe.py --skip-density

# Keep CODEX_HOME/workspaces for manual inspection after the probe.
uv run python experiments/codex_p0/ownership_probe.py \
  --keep-root /tmp/codex-p0-inspect \
  --skip-density

# Pin a model field on thread/start if the tested Codex build requires one.
CODEX_P0_MODEL=<model-id> uv run python experiments/codex_p0/ownership_probe.py
```

## What it measures

### Writer ownership cases

Each case gets an isolated `CODEX_HOME` and durable thread:

- `healthy_conflict`: process B attempts `thread/resume` while process A remains alive.
- `sigstop`: A is frozen, B attempts resume, then A is continued. POSIX only.
- `stdin_eof`: A's stdio control connection is closed before B resumes.
- `sigterm`: A's process group is terminated before B resumes.
- `sigkill`: A's process group is killed before B resumes.

The output records:

- `thread/resume` success/error payloads
- `thread-writer-locks` snapshots
- app-server startup/teardown latency
- Linux `/proc` RSS and FD counts when available
- descendant PIDs discovered through `ps`
- stderr tails on failure

The probe deliberately records evidence rather than encoding expectations such as “SIGKILL must make resume succeed”. Those expectations are exactly what P0b is supposed to establish for the pinned Codex version.

### Density cases

One app-server process starts durable threads at densities `1,10,50,100` by default and records RSS/FD/descendant counts. Override with:

```bash
--densities 1,5,20
```

This is **not** a throughput benchmark: no model turns are executed. It answers only the low-level question “what does the idle runtime/thread-store footprint look like at this density?”

## Safety / interpretation

Do not infer a fencing protocol merely because a second writer is rejected. The current upstream local store uses an OS file lock as a single-writer guard. P0b must separately establish what happens during `SIGSTOP/SIGCONT`, unclean death, and takeover.

Until P0b proves stronger semantics, the v1 safety invariant from #164 stands:

> Never move a live Codex thread to another runtime merely because the old runtime looks unhealthy. Takeover requires positive proof that the old owner is dead or has successfully relinquished its writer. If that proof is unavailable, fail availability rather than risk concurrent side effects.

## Platform notes

- `SIGSTOP/SIGCONT` is skipped outside POSIX.
- `/proc` RSS/FD sampling is Linux-specific and degrades to partial output elsewhere.
- Descendant discovery uses `ps` when available.
- Runtime cleanup signals the app-server **process group** on POSIX so descendants are included in the experiment. The report still records any descendants observed after parent exit.

## Not covered by this probe

This file is only P0b support. It does not decide:

- enterprise Responses/LiteLLM compatibility (P0a)
- Python SDK B0 parked-reader death detection (P0c)
- direct-client C transport correctness (P0c)
- ambiguous-turn history reconciliation (P0e)

Those must use their own executable fixtures and must not be replaced by architectural inference from this probe.
