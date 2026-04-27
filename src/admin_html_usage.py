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
              <div class="flex-gap-sm" style="flex-wrap:wrap; align-items:center">
                <label class="text-xs text-dim" style="display:flex; gap:4px; align-items:center">
                  WINDOW
                  <select x-model.number="usageWindow"
                    @change="usageStart=''; usageEnd=''; loadUsage()"
                    style="padding:4px 8px; font-size:0.75rem">
                    <option value="1">1d</option>
                    <option value="7">7d</option>
                    <option value="30">30d</option>
                    <option value="90">90d</option>
                  </select>
                </label>
                <span class="text-xs text-dim">or</span>
                <label class="text-xs text-dim" style="display:flex; gap:4px; align-items:center">
                  FROM
                  <input type="date" x-model="usageStart"
                    style="padding:4px 8px; font-size:0.75rem; color-scheme:dark">
                </label>
                <label class="text-xs text-dim" style="display:flex; gap:4px; align-items:center">
                  TO
                  <input type="date" x-model="usageEnd"
                    style="padding:4px 8px; font-size:0.75rem; color-scheme:dark">
                </label>
                <button class="btn btn-sm" :class="(usageStart && usageEnd) ? 'btn-primary' : 'btn-ghost'"
                  @click="loadUsage()" :disabled="(usageStart || usageEnd) && !(usageStart && usageEnd)">[APPLY]</button>
                <button class="btn btn-sm btn-ghost" x-show="usageStart || usageEnd"
                  @click="usageStart=''; usageEnd=''; loadUsage()">[CLEAR]</button>
                <button class="btn btn-sm btn-ghost" @click="loadUsage()">[RELOAD]</button>
              </div>
            </div>
            <div x-show="usageStart && usageEnd" class="text-xs text-dim" style="margin-top:-0.5rem; margin-bottom:0.75rem">
              showing <span style="color:var(--cyan)" x-text="usageStart"></span> → <span style="color:var(--cyan)" x-text="usageEnd"></span> (inclusive)
            </div>

            <div class="card mb-lg">
              <div class="flex-between mb-md">
                <h3 style="margin:0">Trends</h3>
                <span class="text-xs text-dim">last 5 · day / week / month</span>
              </div>
              <div x-show="usageSeriesEmpty()" class="text-muted" style="padding:1rem; text-align:center">[ NO DATA ]</div>
              <div x-show="!usageSeriesEmpty()" class="table-wrapper">
                <table style="table-layout:fixed; width:100%">
                  <colgroup>
                    <col style="width:110px">
                    <col><col><col>
                  </colgroup>
                  <thead>
                    <tr>
                      <th></th>
                      <th style="text-align:center">DAY</th>
                      <th style="text-align:center">WEEK</th>
                      <th style="text-align:center">MONTH</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template x-for="chart in [
                      {key:'turns', label:'QUERIES', color:'var(--green)'},
                      {key:'users', label:'USERS', color:'var(--cyan)'},
                      {key:'tokens', label:'TOKENS', color:'var(--red)'}
                    ]" :key="'row-' + chart.key">
                      <tr>
                        <td class="text-xs text-dim" style="vertical-align:middle; letter-spacing:0.08em" x-text="chart.label"></td>
                        <template x-for="g in ['day','week','month']" :key="'cell-' + chart.key + '-' + g">
                          <td style="vertical-align:middle; padding:8px">
                            <div style="display:flex; gap:4px; align-items:flex-end; height:90px; border-bottom:1px solid var(--border-dim)">
                              <template x-for="b in seriesForChart(g, chart.key)" :key="g + chart.key + b.label">
                                <div style="flex:1; display:flex; flex-direction:column; align-items:center; gap:2px; min-width:0">
                                  <div class="text-xs" style="color:var(--text-bright); white-space:nowrap; font-size:0.65rem" x-text="formatNum(b.value)"></div>
                                  <div :style="'width:78%; background:' + chart.color + '; height:' + (b.pct*100) + '%; min-height:2px; transition:height 0.3s'"></div>
                                </div>
                              </template>
                            </div>
                            <div style="display:flex; gap:4px; padding-top:4px">
                              <template x-for="b in seriesForChart(g, chart.key)" :key="g + chart.key + b.label + '-lbl'">
                                <div class="text-xs text-dim" style="flex:1; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:0.6rem" x-text="b.label"></div>
                              </template>
                            </div>
                          </td>
                        </template>
                      </tr>
                    </template>
                    <tr>
                      <td class="text-xs text-dim" style="vertical-align:middle; letter-spacing:0.08em">TOOL CALLS</td>
                      <template x-for="g in ['day','week','month']" :key="'tool-cell-' + g">
                        <td style="vertical-align:middle; padding:8px">
                          <template x-if="(usage.toolsSeries?.[g]?.buckets ?? []).length === 0">
                            <div class="text-muted text-xs" style="padding:1rem; text-align:center">-</div>
                          </template>
                          <template x-if="(usage.toolsSeries?.[g]?.buckets ?? []).length > 0">
                            <div>
                              <div style="display:flex; gap:6px; align-items:flex-end; height:90px; border-bottom:1px solid var(--border-dim)">
                                <template x-for="bucket in (usage.toolsSeries?.[g]?.buckets ?? [])" :key="g + 'b-' + bucket.bucket">
                                  <div style="flex:1; display:flex; flex-direction:column; align-items:center; gap:2px; min-width:0">
                                    <div style="display:flex; gap:1px; align-items:flex-end; width:100%; height:80px; justify-content:center">
                                      <template x-for="(tool, idx) in (usage.toolsSeries?.[g]?.tools ?? [])" :key="g + bucket.bucket + tool">
                                        <div :style="'flex:1; min-width:3px; max-width:14px; background:' + toolColor(idx) + '; height:' + ((Number((bucket.values || {})[tool] || 0) / toolSeriesMax(g)) * 100) + '%; min-height:2px; transition:height 0.3s'"
                                          :title="tool + ': ' + ((bucket.values || {})[tool] || 0) + '회'"></div>
                                      </template>
                                    </div>
                                  </div>
                                </template>
                              </div>
                              <div style="display:flex; gap:6px; padding-top:4px">
                                <template x-for="bucket in (usage.toolsSeries?.[g]?.buckets ?? [])" :key="g + bucket.bucket + '-lbl'">
                                  <div class="text-xs text-dim" style="flex:1; text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:0.6rem" x-text="bucket.bucket"></div>
                                </template>
                              </div>
                              <div style="display:flex; flex-wrap:wrap; gap:4px 8px; padding-top:6px">
                                <template x-for="(tool, idx) in (usage.toolsSeries?.[g]?.tools ?? [])" :key="g + 'leg-' + tool">
                                  <div style="display:flex; gap:3px; align-items:center; font-size:0.6rem; color:var(--text-dim)">
                                    <span :style="'width:8px; height:8px; background:' + toolColor(idx) + '; display:inline-block'"></span>
                                    <span class="text-mono" style="max-width:120px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap" x-text="tool"></span>
                                  </div>
                                </template>
                              </div>
                            </div>
                          </template>
                        </td>
                      </template>
                    </tr>
                    <tr>
                      <td class="text-xs text-dim" style="vertical-align:top; letter-spacing:0.08em; padding-top:12px">TOP TOOLS</td>
                      <template x-for="g in ['day','week','month']" :key="'tools-' + g">
                        <td style="vertical-align:top; padding:8px">
                          <div x-show="(usage.toolsByGran?.[g] ?? []).length === 0" class="text-muted text-xs">-</div>
                          <ul style="margin:0; padding:0; list-style:none">
                            <template x-for="t in (usage.toolsByGran?.[g] ?? []).slice(0, 6)" :key="g + t.tool_name">
                              <li class="text-xs" style="padding:2px 0; display:flex; justify-content:space-between; gap:8px">
                                <span class="text-mono" style="color:var(--cyan); overflow:hidden; text-overflow:ellipsis; white-space:nowrap" x-text="t.tool_name"></span>
                                <span style="color:var(--amber); white-space:nowrap" x-text="formatNum(t.calls) + '회'"></span>
                              </li>
                            </template>
                          </ul>
                        </td>
                      </template>
                    </tr>
                  </tbody>
                </table>
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
