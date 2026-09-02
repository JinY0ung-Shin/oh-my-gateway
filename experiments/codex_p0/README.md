# Codex P0 experiments

This directory contains **evidence-gathering probes for canonical issue #163**. It is intentionally outside `src/backends/codex`: the existing Codex backend is frozen/stale and must not be extended before the P0 production gate selects a replacement architecture.

Parent architecture discussion: #162

## P0b-0: declare deployment budgets first

#163 requires resource thresholds to be fixed before measurements are interpreted. Copy and fill:

```bash
cp experiments/codex_p0/deployment_budget.example.json /tmp/codex-p0-budget.json
```

Do not leave `null` values. `process_per_session_probe.py` refuses to run with an incomplete budget. The required fields cover expected live/active/parked concurrency, memory/PID/FD limits, cold-start/resume/teardown p95 limits, idle CPU, zero tolerated orphan descendants, and the process-per-session counts to measure.

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

### What it measures

Each ownership case gets an isolated `CODEX_HOME` and durable thread:

- `healthy_conflict`: process B attempts `thread/resume` while process A remains alive.
- `sigstop`: A is frozen, B attempts resume, then A is continued. POSIX only.
- `stdin_eof`: A's stdio control connection is closed before B resumes.
- `sigterm`: A's process group is terminated before B resumes.
- `sigkill`: A's process group is killed before B resumes.

The output records `thread/resume` success/error payloads, `thread-writer-locks` snapshots, app-server startup/teardown latency, Linux `/proc` RSS and FD counts when available, descendant PIDs, surviving descendants after teardown, and stderr tails on failure.

The probe deliberately records evidence rather than encoding expectations such as “SIGKILL must make resume succeed”. Those expectations are exactly what P0b is supposed to establish for the pinned Codex version.

### Thread-density cases

One app-server process starts durable threads at densities `1,10,50,100` by default and records RSS/FD/descendant counts. Override with:

```bash
--densities 1,5,20
```

This is **not** a throughput benchmark: no model turns are executed. It answers only the low-level question “what does the idle runtime/thread-store footprint look like at this density?”

## Process-per-session budget probe

`process_per_session_probe.py` measures the B0-required topology independently from transport correctness: one app-server process per live session/thread, all using the same intended local `CODEX_HOME` store.

It requires the predeclared budget file:

```bash
uv run python experiments/codex_p0/process_per_session_probe.py \
  --codex-bin /path/to/codex \
  --budget /tmp/codex-p0-budget.json \
  --output artifacts/codex-p0-process-per-session.json
```

For each count in `process_per_session_counts` it records:

- aggregate direct + descendant PID count
- aggregate RSS and FD count where `/proc` is available
- per-process cold-start timings and p95
- idle CPU over a bounded sample window
- retained stderr line/byte volume with truncation flag
- teardown timings and p95
- surviving descendant PIDs
- mechanical `pass` / `fail` / `inconclusive` against the predeclared budget

This keeps the B0-vs-C0 transport decision separate from the later C1 multiplexing question.

## Resume latency probe

`resume_latency_probe.py` repeatedly creates a durable thread in process A, releases the writer through a declared shutdown mode, starts process B on the same `CODEX_HOME`, and measures only the `thread/resume` RPC latency. This isolates resume p95 from app-server cold-start time.

```bash
uv run python experiments/codex_p0/resume_latency_probe.py \
  --codex-bin /path/to/codex \
  --budget /tmp/codex-p0-budget.json \
  --iterations 20 \
  --output artifacts/codex-p0-resume.json
```

By default it measures `stdin_eof`, `SIGTERM`, and `SIGKILL` release paths separately. A mode fails if any iteration cannot resume successfully or if its measured p95 exceeds `acceptable_resume_p95_s`.

## Safety / interpretation

Do not infer a fencing protocol merely because a second writer is rejected. The current upstream local store uses an OS file lock as a single-writer guard. P0b must separately establish what happens during `SIGSTOP/SIGCONT`, unclean death, and takeover.

Until P0b proves stronger semantics, the v1 safety invariant from #163 stands:

> Never move a live Codex thread to another runtime merely because the old runtime looks unhealthy. Takeover requires positive proof that the old owner is dead or has successfully relinquished its writer. If that proof is unavailable, fail availability rather than risk concurrent side effects.

## Platform notes

- `SIGSTOP/SIGCONT` is skipped outside POSIX.
- `/proc` RSS/FD/CPU sampling is Linux-specific and degrades to partial output elsewhere.
- Descendant discovery uses `ps` when available.
- Runtime cleanup signals the app-server **process group** on POSIX so descendants are included in the experiment.
- Descendant PIDs are captured before parent death and checked afterward so reparenting cannot hide leaked workers.

## Not covered by these probes

These files are P0b support only. They do not decide:

- P0a-1 real internal Responses/LiteLLM compatibility
- P0a-2 isolated model-gateway fault corpus
- Python SDK B0 correctness gates B0-1 through B0-9
- direct-client C0 transport correctness
- C1 shared-shard topology
- ambiguous-turn recovery/reconciliation

Those require their own executable fixtures under #163 and must not be replaced by architectural inference from these probes.
