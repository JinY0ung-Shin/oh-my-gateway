"""Admin sessions tab HTML."""


def get_sessions_html() -> str:
    """Return the admin sessions tab html."""
    return """      <!-- Sessions Tab -->
      <div x-show="tab==='sessions'" role="tabpanel">
        <div class="card">
          <h3>Active Sessions</h3>
          <template x-if="loading.sessions">
            <div>
              <template x-for="i in 3" :key="i"><div class="skeleton skeleton-row"></div></template>
            </div>
          </template>
          <template x-if="!loading.sessions">
            <div>
              <div class="table-wrapper">
                <table>
                  <thead><tr><th>SESSION_ID</th><th>BACKEND</th><th>MSGS</th><th>TURNS</th><th>LAST_ACTIVE</th><th>ACTIONS</th></tr></thead>
                  <tbody>
                    <template x-for="s in (summary.sessions?.sessions ?? [])" :key="s.session_id">
                      <tr @click="toggleSessionHistory(s.session_id)" style="cursor:pointer"
                        :style="expandedSession === s.session_id ? 'background:var(--bg-surface)' : ''">
                        <td class="text-mono">
                          <span style="color:var(--green-dim)" x-text="expandedSession === s.session_id ? '[-] ' : '[+] '"></span>
                          <span style="color:var(--cyan)" x-text="s.session_id?.substring(0,16) + '...'"></span>
                        </td>
                        <td><span class="badge" style="background:var(--bg-surface); border-color:var(--border-bright); font-size:0.65rem" x-text="s.backend || 'claude'"></span></td>
                        <td style="color:var(--text-bright)" x-text="s.message_count ?? '-'"></td>
                        <td style="color:var(--amber)" x-text="s.turn_counter ?? '-'"></td>
                        <td class="text-xs" style="color:var(--text-dim)" x-text="formatTime(s.last_accessed)"></td>
                        <td style="white-space:nowrap">
                          <button class="btn btn-sm btn-ghost" @click.stop="exportSession(s.session_id)" title="Export JSON">[EXPORT]</button>
                          <button class="btn btn-sm btn-danger-ghost" @click.stop="deleteSession(s.session_id)">[DEL]</button>
                        </td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div x-show="(summary.sessions?.sessions ?? []).length === 0" class="text-muted" style="padding:1rem; text-align:center">[ NO ACTIVE SESSIONS ]</div>
            </div>
          </template>
        </div>

        <template x-if="expandedSession && sessionMessages">
          <div class="card" style="margin-top:0">
            <div class="flex-between mb-md">
              <div class="flex-gap-sm">
                <h3 style="margin:0">Session Detail</h3>
                <span class="text-mono text-xs" style="color:var(--cyan)" x-text="expandedSession?.substring(0,16) + '...'"></span>
              </div>
              <button class="btn btn-sm btn-ghost" @click="expandedSession = null; sessionMessages = null; sessionDetail = null">[CLOSE]</button>
            </div>
            <div x-show="sessionDetail" class="flex-wrap-gap text-xs mb-md" style="gap:0.75rem; color:var(--text-dim)">
              <span>backend=<span style="color:var(--text-bright)" x-text="sessionDetail?.backend || '-'"></span></span>
              <span>turns=<span style="color:var(--amber)" x-text="sessionDetail?.turn_counter ?? '-'"></span></span>
              <span>ttl=<span style="color:var(--text-bright)" x-text="(sessionDetail?.ttl_minutes ?? '-') + 'min'"></span></span>
              <span x-show="sessionDetail?.provider_session_id">provider=<span style="color:var(--cyan)" x-text="(sessionDetail?.provider_session_id?.substring(0,16) || '') + '...'"></span></span>
              <span>created=<span style="color:var(--text-bright)" x-text="formatTime(sessionDetail?.created_at)"></span></span>
            </div>
            <div class="flex-between mb-md">
              <span class="text-sm text-muted">
                // message_history (<span style="color:var(--text-bright)" x-text="sessionMessages.total"></span> entries)
              </span>
              <span class="badge badge-warn text-xs">SENSITIVE DATA</span>
            </div>
            <template x-for="m in (sessionMessages.messages ?? [])" :key="m.index">
              <div :class="'msg-bubble msg-' + m.role">
                <div class="msg-role"
                  :class="m.role === 'user' ? 'text-info' : m.role === 'assistant' ? 'text-success' : 'text-warning'"
                  x-text="m.role.toUpperCase() + (m.name ? ' (' + m.name + ')' : '')"></div>
                <div x-text="m.content || '(empty)'"></div>
                <span x-show="m.truncated" class="text-xs" style="cursor:pointer; color:var(--cyan)"
                  @click.stop="loadFullMessage(expandedSession, m.index)">[...truncated — click to expand]</span>
              </div>
            </template>
            <div x-show="!sessionMessages.messages || sessionMessages.messages.length === 0"
              class="text-sm text-muted" style="text-align:center">[ NO MESSAGES ]</div>
          </div>
        </template>
      </div>"""
