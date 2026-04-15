"""Admin rate limits tab HTML."""


def get_ratelimits_html() -> str:
    """Return the admin rate limits tab html."""
    return """      <!-- Rate Limits Tab -->
      <div x-show="tab==='ratelimits'" role="tabpanel">
        <div class="card mb-md">
          <div class="flex-between">
            <p class="text-xs text-muted" style="margin:0">
              // approximate monitoring based on request logs. actual enforcement by slowapi.
            </p>
            <button class="btn btn-sm btn-ghost" @click="loadRateLimits()" aria-label="Refresh rate limits">[RELOAD]</button>
          </div>
          <div x-show="config.rate_limits" class="flex-wrap-gap" style="margin-top:0.5rem">
            <template x-for="(v, k) in (config.rate_limits ?? {})" :key="k">
              <span class="text-xs" style="padding:2px 8px; border:1px solid var(--border-bright); background:var(--bg-surface)">
                <span style="color:var(--cyan)" x-text="k"></span>=<span style="color:var(--amber)" x-text="v + '/min'"></span>
              </span>
            </template>
          </div>
        </div>
        <div class="grid-2">
          <template x-for="(data, bucket) in (rateLimits.snapshot ?? {})" :key="bucket">
            <div class="card">
              <h3 x-text="bucket" style="text-transform:uppercase; color:var(--cyan)"></h3>
              <div class="flex-between mb-sm">
                <span class="text-sm" style="color:var(--text-dim)"><span style="color:var(--text-bright)" x-text="data.total_usage"></span> / <span x-text="data.limit"></span> req/min</span>
                <span :class="(data.total_usage / data.limit * 100) > 90 ? 'badge badge-err' :
                  (data.total_usage / data.limit * 100) > 70 ? 'badge badge-warn' : 'badge badge-ok'"
                  x-text="Math.round(data.total_usage / data.limit * 100) + '%'"></span>
              </div>
              <div style="background:var(--bg-surface); height:4px; overflow:hidden; margin-bottom:0.75rem">
                <div class="rate-bar-fill" :style="'width:' + Math.min(100, data.total_usage / data.limit * 100) + '%; height:100%; background:' +
                  ((data.total_usage / data.limit * 100) > 90 ? 'var(--red)' : (data.total_usage / data.limit * 100) > 70 ? 'var(--amber)' : 'var(--green)') +
                  '; box-shadow: 0 0 6px currentColor'"></div>
              </div>
              <template x-if="data.clients && data.clients.length > 0">
                <div class="table-wrapper">
                  <table>
                    <thead><tr><th>IP</th><th>COUNT</th><th>USAGE</th></tr></thead>
                    <tbody>
                      <template x-for="c in data.clients" :key="c.ip">
                        <tr>
                          <td class="text-mono" style="color:var(--text-dim)" x-text="c.ip"></td>
                          <td style="color:var(--text-bright)" x-text="c.count"></td>
                          <td><span :class="c.pct_used > 90 ? 'badge badge-err' : c.pct_used > 70 ? 'badge badge-warn' : 'badge badge-ok'"
                            x-text="c.pct_used + '%'"></span></td>
                        </tr>
                      </template>
                    </tbody>
                  </table>
                </div>
              </template>
              <div x-show="!data.clients || data.clients.length === 0" class="text-sm text-muted">[ NO TRAFFIC ]</div>
            </div>
          </template>
        </div>
      </div>"""
