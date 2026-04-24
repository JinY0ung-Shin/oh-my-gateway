"""Admin usage-log tab HTML."""


def get_usage_html() -> str:
    """Return the admin usage tab html."""
    return """      <!-- Usage Tab -->
      <div x-show="tab==='usage'" role="tabpanel">
        <div x-show="usage.enabled === false" class="card" style="text-align:center; padding:2rem">
          <div class="text-muted">[ USAGE LOGGING OFF ]</div>
          <div class="text-xs text-dim" style="margin-top:0.5rem">
            Set <span class="text-mono" style="color:var(--cyan)">USAGE_LOG_DB_URL</span>
            and restart the gateway to enable per-turn token / tool logging.
          </div>
        </div>

        <template x-if="usage.enabled !== false">
          <div>
            <div class="flex-between mb-md" style="flex-wrap:wrap; gap:0.5rem">
              <h3 style="margin:0">Usage</h3>
              <div class="flex-gap-sm" style="flex-wrap:wrap">
                <label class="text-xs text-dim" style="display:flex; gap:4px; align-items:center">
                  WINDOW
                  <select x-model.number="usageWindow" @change="loadUsage()"
                    style="padding:4px 8px; font-size:0.75rem">
                    <option value="1">1d</option>
                    <option value="7">7d</option>
                    <option value="30">30d</option>
                    <option value="90">90d</option>
                  </select>
                </label>
                <button class="btn btn-sm btn-ghost" @click="loadUsage()">[RELOAD]</button>
              </div>
            </div>

            <div class="card mb-lg">
              <div class="flex-between mb-md">
                <h3 style="margin:0">Trends</h3>
                <div class="flex-gap-sm">
                  <template x-for="g in ['day','week','month']" :key="g">
                    <button class="btn btn-sm"
                      :class="usageGran === g ? 'btn-primary' : 'btn-ghost'"
                      @click="usageGran = g; loadUsageSeries()"
                      x-text="g.toUpperCase()"></button>
                  </template>
                </div>
              </div>
              <div x-show="(usage.series ?? []).length === 0" class="text-muted" style="padding:1rem; text-align:center">[ NO DATA ]</div>
              <div x-show="(usage.series ?? []).length > 0"
                style="display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:1rem">
                <template x-for="chart in [
                  {key:'turns', label:'QUERIES', color:'var(--green)'},
                  {key:'users', label:'USERS', color:'var(--cyan)'},
                  {key:'tool_calls', label:'TOOL CALLS', color:'var(--amber)'},
                  {key:'tokens', label:'TOKENS (IN+OUT)', color:'var(--red)'}
                ]" :key="chart.key">
                  <div>
                    <div class="text-xs text-dim" style="margin-bottom:4px" x-text="chart.label"></div>
                    <div style="display:flex; gap:6px; align-items:flex-end; height:140px; padding:4px 4px 0; border-bottom:1px solid var(--border-dim)">
                      <template x-for="b in seriesForChart(chart.key)" :key="b.label + chart.key">
                        <div style="flex:1; display:flex; flex-direction:column; align-items:center; gap:4px; min-width:0">
                          <div class="text-xs" style="color:var(--text-bright); white-space:nowrap" x-text="formatNum(b.value)"></div>
                          <div :style="'width:80%; background:' + chart.color + '; height:' + (b.pct*100) + '%; min-height:2px; transition:height 0.3s'"></div>
                        </div>
                      </template>
                    </div>
                    <div style="display:flex; gap:6px; padding:6px 4px 0">
                      <template x-for="b in seriesForChart(chart.key)" :key="b.label + chart.key + '-lbl'">
                        <div class="text-xs text-dim" style="flex:1; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis" x-text="b.label"></div>
                      </template>
                    </div>
                  </div>
                </template>
              </div>
            </div>

            <div class="grid-3 mb-lg">
              <div class="card stat">
                <div class="value" x-text="usage.summary?.turns_today ?? '-'"></div>
                <div class="label">TURNS TODAY</div>
              </div>
              <div class="card stat">
                <div class="value" x-text="formatNum(usage.summary?.tokens_today)"></div>
                <div class="label">TOKENS TODAY</div>
              </div>
              <div class="card stat">
                <div class="value" x-text="usage.summary?.turns_window ?? '-'"></div>
                <div class="label" x-text="'TURNS ' + usageWindow + 'D'"></div>
              </div>
              <div class="card stat">
                <div class="value" x-text="formatNum((usage.summary?.input_tokens_window ?? 0) + (usage.summary?.output_tokens_window ?? 0))"></div>
                <div class="label" x-text="'TOKENS ' + usageWindow + 'D'"></div>
              </div>
              <div class="card stat">
                <div class="value" x-text="usage.summary?.users_window ?? '-'"></div>
                <div class="label" x-text="'USERS ' + usageWindow + 'D'"></div>
              </div>
              <div class="card stat">
                <div class="value" :style="(usage.summary?.errors_window ?? 0) > 0 ? 'color:var(--red)' : ''" x-text="usage.summary?.errors_window ?? '-'"></div>
                <div class="label">ERROR TURNS</div>
              </div>
            </div>

            <div class="card mb-lg">
              <div class="flex-between mb-md">
                <h3 style="margin:0">Top users</h3>
                <span class="text-xs text-dim" x-text="'last ' + usageWindow + 'd'"></span>
              </div>
              <div class="table-wrapper">
                <table>
                  <thead><tr><th>USER</th><th>CHATS</th><th>TURNS</th><th>TOKENS</th><th>CACHE READ</th><th>TOOL CALLS</th><th>ERRORS</th></tr></thead>
                  <tbody>
                    <template x-for="u in (usage.users ?? [])" :key="u.user">
                      <tr style="cursor:pointer" @click="usageTurnsFilter = u.user; loadUsageTurns()">
                        <td class="text-mono" style="color:var(--text-bright)" x-text="u.user"></td>
                        <td class="text-sm" x-text="u.chats"></td>
                        <td class="text-sm" x-text="u.turns"></td>
                        <td class="text-sm" style="color:var(--amber)" x-text="formatNum(u.tokens)"></td>
                        <td class="text-sm" style="color:var(--cyan)" x-text="formatNum(u.cache_read_tokens)"></td>
                        <td class="text-sm" x-text="u.tool_calls"></td>
                        <td class="text-sm" :style="(u.tool_errors + u.turn_errors) > 0 ? 'color:var(--red)' : 'color:var(--text-dim)'"
                          x-text="(u.tool_errors ?? 0) + '/' + (u.turn_errors ?? 0)"></td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div x-show="(usage.users ?? []).length === 0" class="text-muted" style="padding:1rem; text-align:center">[ NO USERS ]</div>
              <div class="text-xs text-dim" style="margin-top:0.5rem">// click a user to filter the turns table below</div>
            </div>

            <div class="card mb-lg">
              <div class="flex-between mb-md">
                <h3 style="margin:0">Top tools</h3>
                <span class="text-xs text-dim" x-text="'last ' + usageWindow + 'd'"></span>
              </div>
              <div class="table-wrapper">
                <table>
                  <thead><tr><th>TOOL</th><th>CALLS</th><th>ERRORS</th><th>USERS</th><th>TOTAL MS</th><th>AVG MS</th></tr></thead>
                  <tbody>
                    <template x-for="t in (usage.tools ?? [])" :key="t.tool_name">
                      <tr>
                        <td class="text-mono" style="color:var(--cyan); max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap" x-text="t.tool_name"></td>
                        <td class="text-sm" x-text="t.calls"></td>
                        <td class="text-sm" :style="(t.errors ?? 0) > 0 ? 'color:var(--red)' : 'color:var(--text-dim)'" x-text="t.errors"></td>
                        <td class="text-sm" x-text="t.users"></td>
                        <td class="text-sm" style="color:var(--amber)" x-text="t.total_ms"></td>
                        <td class="text-sm" style="color:var(--text-dim)" x-text="t.calls > 0 ? Math.round(t.total_ms / t.calls) : '-'"></td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div x-show="(usage.tools ?? []).length === 0" class="text-muted" style="padding:1rem; text-align:center">[ NO TOOL CALLS ]</div>
            </div>

            <div class="card">
              <div class="flex-between mb-md">
                <h3 style="margin:0">Recent turns</h3>
                <div class="flex-gap-sm" style="flex-wrap:wrap">
                  <input type="text" x-model="usageTurnsFilter" placeholder="filter:user"
                    style="padding:4px 8px; font-size:0.75rem; width:150px"
                    @input.debounce.300ms="usageTurnsOffset = 0; loadUsageTurns()">
                  <button class="btn btn-sm btn-ghost" @click="usageTurnsFilter=''; usageTurnsOffset=0; loadUsageTurns()">[CLEAR]</button>
                </div>
              </div>
              <div class="table-wrapper">
                <table>
                  <thead><tr><th>TIME</th><th>USER</th><th>SESSION</th><th>TURN</th><th>IN</th><th>OUT</th><th>CACHE R</th><th>MS</th><th>STATUS</th></tr></thead>
                  <tbody>
                    <template x-for="r in (usage.turns ?? [])" :key="r.id">
                      <tr>
                        <td class="text-xs" style="white-space:nowrap; color:var(--text-dim)" x-text="formatTime(r.ts)"></td>
                        <td class="text-mono" style="color:var(--text-bright)" x-text="r.user"></td>
                        <td class="text-mono text-xs" style="color:var(--cyan)" x-text="(r.session_id || '').substring(0,8)"></td>
                        <td class="text-sm" x-text="r.turn"></td>
                        <td class="text-sm" x-text="formatNum(r.input_tokens)"></td>
                        <td class="text-sm" x-text="formatNum(r.output_tokens)"></td>
                        <td class="text-sm" style="color:var(--cyan)" x-text="formatNum(r.cache_read_tokens)"></td>
                        <td class="text-sm" style="color:var(--amber)" x-text="r.duration_ms"></td>
                        <td><span :class="r.status === 'completed' ? 'badge badge-ok' : 'badge badge-err'" x-text="r.status"></span></td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div x-show="(usage.turns ?? []).length === 0" class="text-muted" style="padding:1rem; text-align:center">[ NO TURNS ]</div>
              <div style="display:flex; justify-content:center; gap:0.5rem; margin-top:0.75rem">
                <button class="btn btn-sm btn-ghost"
                  @click="usageTurnsOffset = Math.max(0, usageTurnsOffset - 50); loadUsageTurns()"
                  :disabled="usageTurnsOffset === 0">[PREV]</button>
                <span class="text-xs text-muted" style="padding:4px 8px"
                  x-text="'OFFSET ' + usageTurnsOffset"></span>
                <button class="btn btn-sm btn-ghost"
                  @click="usageTurnsOffset += 50; loadUsageTurns()"
                  :disabled="(usage.turns ?? []).length < 50">[NEXT]</button>
              </div>
            </div>
          </div>
        </template>
      </div>"""
