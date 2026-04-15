"""Admin logs tab HTML."""


def get_logs_html() -> str:
    """Return the admin logs tab html."""
    return """      <!-- Logs Tab -->
      <div x-show="tab==='logs'" role="tabpanel">
        <div class="grid-3 mb-lg">
          <div class="card stat">
            <div class="value" x-text="logs.stats?.total_requests ?? '-'"></div>
            <div class="label">TOTAL REQUESTS</div>
          </div>
          <div class="card stat">
            <div class="value" :style="(logs.stats?.error_count ?? 0) > 0 ? 'color:var(--red); text-shadow: 0 0 10px var(--red-subtle)' : ''" x-text="logs.stats?.error_count ?? '-'"></div>
            <div class="label">ERRORS</div>
          </div>
          <div class="card stat">
            <div class="value" style="color:var(--amber)" x-text="logs.stats?.avg_latency_ms ? (logs.stats.avg_latency_ms + 'ms') : '-'"></div>
            <div class="label">AVG LATENCY</div>
          </div>
        </div>
        <div class="card">
          <div class="flex-between mb-md">
            <h3 style="margin:0">Request Log</h3>
            <div class="flex-gap-sm" style="flex-wrap:wrap">
              <input type="text" x-model="logsFilter.endpoint" placeholder="filter:endpoint"
                style="padding:4px 8px; font-size:0.75rem; width:150px" @input.debounce.300ms="loadLogs()">
              <select x-model="logsFilter.status" @change="loadLogs()"
                style="padding:4px 8px; font-size:0.75rem; width:90px">
                <option value="">ALL</option>
                <option value="200">200</option>
                <option value="4xx">4xx</option>
                <option value="5xx">5xx</option>
              </select>
              <label class="text-xs flex-gap-sm" style="gap:4px; color:var(--text-dim)">
                <input type="checkbox" x-model="logsAutoRefresh" @change="toggleLogsPolling()"> AUTO
              </label>
              <button class="btn btn-sm btn-ghost" @click="loadLogs()" aria-label="Refresh logs">[RELOAD]</button>
            </div>
          </div>
          <template x-if="loading.logs">
            <div>
              <template x-for="i in 5" :key="i"><div class="skeleton skeleton-row"></div></template>
            </div>
          </template>
          <template x-if="!loading.logs">
            <div>
              <div class="table-wrapper">
                <table>
                  <thead><tr><th>TIME</th><th>METHOD</th><th>PATH</th><th>STATUS</th><th>LATENCY</th><th>IP</th><th>MODEL</th></tr></thead>
                  <tbody>
                    <template x-for="(e, idx) in (logs.items ?? [])" :key="e.timestamp + e.path + idx">
                      <tr @click="expandedLog = expandedLog === idx ? null : idx" style="cursor:pointer">
                        <td class="text-xs" style="white-space:nowrap; color:var(--text-dim)" x-text="formatTime(new Date(e.timestamp * 1000).toISOString())"></td>
                        <td><span class="badge badge-info" x-text="e.method"></span></td>
                        <td class="text-mono" style="max-width:250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text-bright)" x-text="e.path"></td>
                        <td><span :class="e.status_code < 400 ? 'badge badge-ok' : e.status_code < 500 ? 'badge badge-warn' : 'badge badge-err'" x-text="e.status_code"></span></td>
                        <td class="text-sm" style="color:var(--amber)" x-text="e.response_time_ms + 'ms'"></td>
                        <td class="text-mono" style="color:var(--text-dim)" x-text="e.client_ip"></td>
                        <td class="text-sm" style="color:var(--cyan)" x-text="e.model || '-'"></td>
                      </tr>
                    </template>
                    <template x-for="(e, idx) in (logs.items ?? [])" :key="'detail-' + idx">
                      <tr x-show="expandedLog === idx" style="background:var(--bg-surface)">
                        <td colspan="7" class="text-xs" style="padding:8px 12px">
                          <div class="flex-wrap-gap" style="gap:1rem; color:var(--text-dim)">
                            <span>backend=<span style="color:var(--text-bright)" x-text="e.backend || '-'"></span></span>
                            <span>session=<span class="text-mono" style="color:var(--cyan)" x-text="e.session_id ? e.session_id.substring(0,16) + '...' : '-'"></span></span>
                            <span>bucket=<span style="color:var(--amber)" x-text="e.bucket || '-'"></span></span>
                            <span>latency=<span style="color:var(--text-bright)" x-text="e.response_time_ms + 'ms'"></span></span>
                          </div>
                        </td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div x-show="(logs.items ?? []).length === 0" class="text-muted" style="padding:1rem; text-align:center">[ NO LOGS ]</div>
              <div x-show="logs.total > 50" style="display:flex; justify-content:center; gap:0.5rem; margin-top:0.75rem">
                <button class="btn btn-sm btn-ghost" @click="logsPage = Math.max(0, logsPage-1); loadLogs()" :disabled="logsPage === 0">[PREV]</button>
                <span class="text-xs text-muted" style="padding:4px 8px" x-text="'PAGE ' + (logsPage+1)"></span>
                <button class="btn btn-sm btn-ghost" @click="logsPage++; loadLogs()" :disabled="(logsPage+1)*50 >= logs.total">[NEXT]</button>
              </div>
              <div class="text-xs text-muted" style="margin-top:0.5rem; text-align:right">// latency = handler creation time only (streaming excluded)</div>
            </div>
          </template>
        </div>
      </div>"""
