"""Admin dashboard tab HTML."""


def get_dashboard_html() -> str:
    """Return the admin dashboard tab html."""
    return """      <!-- Dashboard Tab -->
      <div x-show="tab==='dashboard'" role="tabpanel">
        <!-- Zone 1: Stats -->
        <template x-if="loading.dashboard">
          <div class="grid-4 mb-lg">
            <div class="card stat"><div class="skeleton skeleton-stat"></div><div class="label">LOADING...</div></div>
            <div class="card stat"><div class="skeleton skeleton-stat"></div><div class="label">LOADING...</div></div>
            <div class="card stat"><div class="skeleton skeleton-stat"></div><div class="label">LOADING...</div></div>
            <div class="card stat"><div class="skeleton skeleton-stat"></div><div class="label">LOADING...</div></div>
          </div>
        </template>
        <template x-if="!loading.dashboard">
          <div class="grid-4">
            <div class="card stat">
              <div class="value" x-text="summary.sessions?.active ?? '-'"></div>
              <div class="label">ACTIVE SESSIONS</div>
            </div>
            <div class="card stat">
              <div class="value" style="color:var(--cyan); text-shadow: 0 0 10px var(--cyan-subtle)" x-text="summary.models?.length ?? '-'"></div>
              <div class="label">MODELS LOADED</div>
            </div>
            <div class="card stat">
              <div class="value" style="color:var(--amber); text-shadow: 0 0 10px var(--amber-subtle)" x-text="backendsDetail.length || '-'"></div>
              <div class="label">BACKENDS</div>
            </div>
            <div class="card stat">
              <div class="value" :style="(metrics.stats?.error_rate ?? 0) > 0.05 ? 'color:var(--red); text-shadow: 0 0 10px var(--red-subtle)' : ''"
                x-text="((metrics.stats?.error_rate ?? 0) * 100).toFixed(1) + '%'"></div>
              <div class="label">ERROR RATE</div>
            </div>
          </div>
        </template>

        <!-- Zone 2: Performance + Backend Health -->
        <div class="grid-2">
          <div class="card">
            <div class="flex-between mb-md">
              <h3 style="margin:0">Performance</h3>
              <button class="btn btn-sm btn-ghost" @click="loadMetrics()" aria-label="Refresh metrics">[RELOAD]</button>
            </div>
            <div class="grid-3 mb-md" style="gap:0.5rem">
              <div class="stat" style="padding:0.25rem">
                <div class="value" style="font-size:1.3rem" x-text="metrics.stats?.total_requests ?? '-'"></div>
                <div class="label">REQUESTS</div>
              </div>
              <div class="stat" style="padding:0.25rem">
                <div class="value" style="font-size:1.3rem; color:var(--cyan)" x-text="metrics.total_logged ?? '-'"></div>
                <div class="label">LOGGED</div>
              </div>
              <div class="stat" style="padding:0.25rem">
                <div class="value" style="font-size:1.3rem; color:var(--amber)" x-text="(metrics.stats?.avg_latency_ms ?? '-') + 'ms'"></div>
                <div class="label">AVG LATENCY</div>
              </div>
            </div>
            <template x-for="item in [
              {label: 'p50', val: metrics.stats?.p50_latency_ms, max: 5000},
              {label: 'p95', val: metrics.stats?.p95_latency_ms, max: 10000},
              {label: 'p99', val: metrics.stats?.p99_latency_ms, max: 15000}
            ]" :key="item.label">
              <div class="flex-gap-sm mb-sm">
                <span class="text-xs" style="width:28px; color:var(--text-dim)" x-text="item.label"></span>
                <div class="latency-bar">
                  <div class="latency-fill" :style="'width:' + Math.min(100, (item.val ?? 0)/item.max*100) + '%; background:' +
                    ((item.val ?? 0) > item.max*0.8 ? 'var(--red)' : (item.val ?? 0) > item.max*0.5 ? 'var(--amber)' : 'var(--green)')"></div>
                </div>
                <span class="text-mono" style="width:60px; text-align:right; color:var(--text-dim)" x-text="(item.val ?? '-') + 'ms'"></span>
              </div>
            </template>
          </div>

          <div class="card">
            <div class="flex-between mb-md">
              <h3 style="margin:0">Backend Health</h3>
              <button class="btn btn-sm btn-ghost" @click="loadBackends()" aria-label="Refresh backends">[RELOAD]</button>
            </div>
            <template x-for="b in backendsDetail" :key="b.name">
              <div style="border:1px solid var(--border); padding:0.75rem; margin-bottom:0.5rem">
                <div class="flex-between mb-sm">
                  <div class="flex-gap-sm">
                    <strong style="color:var(--text-bright)" x-text="b.name"></strong>
                    <span :class="b.healthy ? 'badge badge-ok' : 'badge badge-err'"
                      role="status" x-text="b.healthy ? 'ONLINE' : 'OFFLINE'"></span>
                  </div>
                  <span :class="b.auth?.valid ? 'badge badge-ok' : 'badge badge-err'"
                    x-text="'AUTH: ' + (b.auth?.valid ? 'VALID' : 'INVALID')"></span>
                </div>
                <div class="text-xs text-muted">
                  <span x-show="b.auth?.method" x-text="'method=' + b.auth?.method" style="margin-right:1rem"></span>
                  <span x-show="b.auth?.env_vars?.length" x-text="'env=[' + (b.auth?.env_vars?.join(', ') || '') + ']'"></span>
                </div>
                <div x-show="b.auth?.errors?.length" style="margin-top:0.25rem">
                  <template x-for="err in (b.auth?.errors ?? [])">
                    <div class="text-xs text-danger" x-text="'! ' + err"></div>
                  </template>
                </div>
                <div x-show="b.health_error" class="text-xs text-danger" style="margin-top:0.25rem" x-text="'! ' + b.health_error"></div>
                <div x-show="b.models?.length" class="flex-wrap-gap" style="margin-top:0.5rem; gap:0.25rem">
                  <template x-for="m in (b.models ?? [])">
                    <span class="badge" style="background:var(--bg-surface); border-color:var(--border-bright); font-size:0.65rem" x-text="m"></span>
                  </template>
                </div>
              </div>
            </template>
            <div x-show="backendsDetail.length === 0" class="text-muted" style="text-align:center; padding:1rem">
              [ NO BACKENDS DETECTED ]
            </div>
          </div>
        </div>

        <!-- Zone 3: MCP + Models -->
        <div class="grid-2">
          <div class="card">
            <div class="flex-between mb-md">
              <h3 style="margin:0">MCP Servers</h3>
              <button class="btn btn-sm btn-ghost" @click="loadMcpServers()" aria-label="Refresh MCP servers">[RELOAD]</button>
            </div>
            <template x-for="s in mcpServers" :key="s.name">
              <div style="border:1px solid var(--border); padding:0.75rem; margin-bottom:0.5rem">
                <div class="flex-gap-sm mb-sm">
                  <strong style="color:var(--cyan)" x-text="s.name"></strong>
                  <span class="badge" style="background:var(--bg-surface); border-color:var(--border-bright); font-size:0.65rem" x-text="s.type"></span>
                </div>
                <div x-show="s.tools?.length" class="flex-wrap-gap" style="gap:0.25rem">
                  <template x-for="t in (s.tools ?? [])">
                    <span class="text-xs" style="color:var(--text-dim)" x-text="t"></span>
                  </template>
                </div>
              </div>
            </template>
            <div x-show="mcpServers.length === 0" class="text-muted" style="text-align:center; padding:0.5rem">
              [ NO MCP SERVERS ]
            </div>
          </div>

          <div class="card">
            <h3>Models Registry</h3>
            <div class="table-wrapper">
              <table>
                <thead><tr><th>MODEL_ID</th><th>BACKEND</th></tr></thead>
                <tbody>
                  <template x-for="m in (summary.models ?? [])" :key="m.id">
                    <tr>
                      <td style="color:var(--text-bright)" x-text="m.id"></td>
                      <td><span class="badge badge-ok" x-text="m.backend"></span></td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>"""
