"""Admin dashboard HTML generator.

Follows the same pattern as ``landing_page.py``: a single function
returns a self-contained HTML string with inline CSS/JS and CDN
dependencies (Alpine.js, Pico CSS, CodeMirror).
"""


def build_admin_page() -> str:
    """Build the admin dashboard HTML."""
    return """<!DOCTYPE html>
<html lang="ko" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin - Claude Code Gateway</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2.0.6/css/pico.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/lib/codemirror.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/theme/material-darker.min.css">
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/lib/codemirror.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/markdown/markdown.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/javascript/javascript.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/yaml/yaml.min.js"></script>
<style>
:root { --accent: #16a34a; }
[data-theme="dark"] {
  --card-bg: #1e293b; --subtle-bg: #334155; --border: #475569; --page-bg: #0f172a;
  --text: #e2e8f0; --text-muted: #94a3b8;
}
[data-theme="light"] {
  --card-bg: #fff; --subtle-bg: #f1f5f9; --border: #e2e8f0; --page-bg: #f8fafc;
  --text: #1e293b; --text-muted: #64748b;
}
body { background: var(--page-bg); font-size: 14px; }
.container { max-width: 1200px; margin: 0 auto; padding: 1rem; }
.card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem; }
.card h3 { margin-top: 0; font-size: 1rem; color: var(--text-muted); }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; }
.stat { text-align: center; }
.stat .value { font-size: 2rem; font-weight: bold; color: var(--accent); }
.stat .label { font-size: 0.85rem; color: var(--text-muted); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; }
.badge-ok { background: #16a34a22; color: #16a34a; }
.badge-warn { background: #f59e0b22; color: #f59e0b; }
.badge-err { background: #ef444422; color: #ef4444; }
nav.tabs { display: flex; gap: 0; border-bottom: 2px solid var(--border); margin-bottom: 1rem; }
nav.tabs button { background: none; border: none; padding: 0.75rem 1.25rem; color: var(--text-muted);
  cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; font-size: 0.9rem; }
nav.tabs button.active { color: var(--accent); border-bottom-color: var(--accent); }
nav.tabs button:hover { color: var(--text); }
.sidebar { display: flex; gap: 1rem; }
.sidebar .file-tree { width: 260px; flex-shrink: 0; }
.sidebar .editor-area { flex: 1; min-width: 0; }
.file-item { padding: 6px 12px; cursor: pointer; border-radius: 4px; font-size: 0.85rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-item:hover { background: var(--subtle-bg); }
.file-item.active { background: var(--accent); color: #fff; }
.file-item .icon { margin-right: 6px; }
.CodeMirror { height: auto; min-height: 300px; max-height: 70vh; border: 1px solid var(--border); border-radius: 4px; font-size: 13px; }
.editor-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
.editor-toolbar .path { font-family: monospace; font-size: 0.85rem; color: var(--text-muted); }
.btn { display: inline-block; padding: 6px 16px; border-radius: 6px; border: none;
  cursor: pointer; font-size: 0.85rem; font-weight: 500; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { opacity: 0.9; }
.btn-sm { padding: 4px 10px; font-size: 0.8rem; }
.btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--text); }
.login-box { max-width: 400px; margin: 4rem auto; }
table { width: 100%; font-size: 0.85rem; }
table th { color: var(--text-muted); font-weight: 600; text-align: left; }
table td, table th { padding: 8px 12px; border-bottom: 1px solid var(--border); }
.toast { position: fixed; bottom: 1.5rem; right: 1.5rem; padding: 12px 20px; border-radius: 8px;
  font-size: 0.85rem; z-index: 100; transition: opacity 0.3s; }
.toast-ok { background: #16a34a; color: #fff; }
.toast-err { background: #ef4444; color: #fff; }
.dirty-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #f59e0b; margin-left: 6px; }
.config-key { font-family: monospace; font-size: 0.8rem; color: var(--accent); }
.redacted { color: var(--text-muted); font-style: italic; }
@media (max-width: 768px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } .sidebar { flex-direction: column; } .sidebar .file-tree { width: 100%; } }
</style>
</head>
<body>

<div x-data="adminApp()" x-init="init()" class="container">

  <!-- Toast -->
  <div x-show="toast.show" x-transition.opacity :class="'toast toast-' + toast.type" x-text="toast.msg"></div>

  <!-- Login -->
  <template x-if="!authenticated">
    <div class="login-box card">
      <h2 style="margin-top:0">Admin Login</h2>
      <p style="color:var(--text-muted)">ADMIN_API_KEY required</p>
      <form @submit.prevent="doLogin()">
        <input type="password" x-model="loginKey" placeholder="Admin API Key"
          style="width:100%; margin-bottom:1rem" required>
        <button class="btn btn-primary" style="width:100%" type="submit">Login</button>
      </form>
      <p x-show="loginError" style="color:#ef4444; margin-top:0.5rem; font-size:0.85rem" x-text="loginError"></p>
    </div>
  </template>

  <!-- Main UI -->
  <template x-if="authenticated">
    <div>
      <!-- Header -->
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem">
        <h1 style="margin:0; font-size:1.5rem">Claude Code Gateway Admin</h1>
        <div style="display:flex; gap:0.5rem; align-items:center">
          <button class="btn btn-sm btn-ghost" @click="refreshAll()">Refresh</button>
          <button class="btn btn-sm btn-ghost" @click="toggleTheme()">Theme</button>
          <button class="btn btn-sm btn-ghost" @click="doLogout()">Logout</button>
        </div>
      </div>

      <!-- Tabs -->
      <nav class="tabs">
        <button :class="{ active: tab === 'dashboard' }" @click="tab='dashboard'">Dashboard</button>
        <button :class="{ active: tab === 'logs' }" @click="tab='logs'; loadLogs()">Logs</button>
        <button :class="{ active: tab === 'ratelimits' }" @click="tab='ratelimits'; loadRateLimits()">Rate Limits</button>
        <button :class="{ active: tab === 'files' }" @click="tab='files'; loadFiles()">Workspace</button>
        <button :class="{ active: tab === 'skills' }" @click="tab='skills'; loadSkills()">Skills</button>
        <button :class="{ active: tab === 'sessions' }" @click="tab='sessions'; loadSummary()">Sessions</button>
        <button :class="{ active: tab === 'config' }" @click="tab='config'; loadConfig(); loadRuntimeConfig(); loadSystemPrompt()">Config</button>
      </nav>

      <!-- Dashboard Tab -->
      <div x-show="tab==='dashboard'">
        <div class="grid-3">
          <div class="card stat">
            <div class="value" x-text="summary.sessions?.active ?? '-'"></div>
            <div class="label">Active Sessions</div>
          </div>
          <div class="card stat">
            <div class="value" x-text="summary.models?.length ?? '-'"></div>
            <div class="label">Available Models</div>
          </div>
          <div class="card stat">
            <div class="value" x-text="backendsDetail.length || '-'"></div>
            <div class="label">Backends</div>
          </div>
        </div>

        <!-- Backend Health & Auth Detail -->
        <div class="card" style="margin-bottom:1rem">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem">
            <h3 style="margin:0">Backend Health & Auth</h3>
            <button class="btn btn-sm btn-ghost" @click="loadBackends()">Refresh</button>
          </div>
          <template x-for="b in backendsDetail" :key="b.name">
            <div style="border:1px solid var(--border); border-radius:6px; padding:0.75rem; margin-bottom:0.5rem">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem">
                <div style="display:flex; align-items:center; gap:0.5rem">
                  <strong x-text="b.name"></strong>
                  <span :class="b.healthy ? 'badge badge-ok' : 'badge badge-err'"
                    x-text="b.healthy ? 'healthy' : 'unhealthy'"></span>
                  <span :class="b.registered ? 'badge badge-ok' : 'badge badge-warn'"
                    x-text="b.registered ? 'registered' : 'not registered'"></span>
                </div>
                <span :class="b.auth?.valid ? 'badge badge-ok' : 'badge badge-err'"
                  x-text="'Auth: ' + (b.auth?.valid ? 'valid' : 'invalid')"></span>
              </div>
              <!-- Auth details -->
              <div style="font-size:0.8rem; color:var(--text-muted)">
                <span x-show="b.auth?.method" x-text="'Method: ' + b.auth?.method" style="margin-right:1rem"></span>
                <span x-show="b.auth?.env_vars?.length" x-text="'Env: ' + (b.auth?.env_vars?.join(', ') || '')" style="margin-right:1rem"></span>
              </div>
              <div x-show="b.auth?.errors?.length" style="margin-top:0.25rem">
                <template x-for="err in (b.auth?.errors ?? [])">
                  <div style="font-size:0.75rem; color:#ef4444" x-text="err"></div>
                </template>
              </div>
              <div x-show="b.health_error" style="font-size:0.75rem; color:#ef4444; margin-top:0.25rem" x-text="b.health_error"></div>
              <!-- Models -->
              <div x-show="b.models?.length" style="margin-top:0.5rem; display:flex; flex-wrap:wrap; gap:0.25rem">
                <template x-for="m in (b.models ?? [])">
                  <span class="badge" style="background:var(--subtle-bg); font-size:0.7rem" x-text="m"></span>
                </template>
              </div>
            </div>
          </template>
          <div x-show="backendsDetail.length === 0" style="color:var(--text-muted); text-align:center; padding:1rem">
            No backends detected
          </div>
        </div>

        <!-- MCP Servers -->
        <div class="card" style="margin-bottom:1rem">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem">
            <h3 style="margin:0">MCP Servers</h3>
            <button class="btn btn-sm btn-ghost" @click="loadMcpServers()">Refresh</button>
          </div>
          <template x-for="s in mcpServers" :key="s.name">
            <div style="border:1px solid var(--border); border-radius:6px; padding:0.75rem; margin-bottom:0.5rem">
              <div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.25rem">
                <strong x-text="s.name"></strong>
                <span class="badge" style="background:var(--subtle-bg); font-size:0.7rem" x-text="s.type"></span>
              </div>
              <div x-show="s.tools?.length" style="display:flex; flex-wrap:wrap; gap:0.25rem; margin-top:0.25rem">
                <template x-for="t in (s.tools ?? [])">
                  <span style="font-size:0.7rem; color:var(--text-muted); font-family:monospace" x-text="t"></span>
                </template>
              </div>
            </div>
          </template>
          <div x-show="mcpServers.length === 0" style="color:var(--text-muted); text-align:center; padding:0.5rem">
            No MCP servers configured
          </div>
        </div>

        <!-- Models table -->
        <div class="card">
          <h3>Models</h3>
          <table>
            <thead><tr><th>Model</th><th>Backend</th></tr></thead>
            <tbody>
              <template x-for="m in (summary.models ?? [])" :key="m.id">
                <tr><td x-text="m.id"></td><td><span class="badge badge-ok" x-text="m.backend"></span></td></tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Logs Tab -->
      <div x-show="tab==='logs'">
        <div class="grid-3" style="margin-bottom:1rem">
          <div class="card stat">
            <div class="value" x-text="logs.stats?.total_requests ?? '-'"></div>
            <div class="label">Total Requests</div>
          </div>
          <div class="card stat">
            <div class="value" x-text="logs.stats?.error_count ?? '-'"></div>
            <div class="label">Errors</div>
          </div>
          <div class="card stat">
            <div class="value" x-text="logs.stats?.avg_latency_ms ? (logs.stats.avg_latency_ms + 'ms') : '-'"></div>
            <div class="label">Avg Latency</div>
          </div>
        </div>
        <div class="card">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem">
            <h3 style="margin:0">Request Log</h3>
            <div style="display:flex; gap:0.5rem; align-items:center">
              <input type="text" x-model="logsFilter.endpoint" placeholder="Filter endpoint..."
                style="padding:4px 8px; font-size:0.8rem; width:160px" @input.debounce.300ms="loadLogs()">
              <select x-model="logsFilter.status" @change="loadLogs()"
                style="padding:4px 8px; font-size:0.8rem; width:100px">
                <option value="">All Status</option>
                <option value="200">200</option>
                <option value="4xx">4xx</option>
                <option value="5xx">5xx</option>
              </select>
              <label style="font-size:0.8rem; display:flex; align-items:center; gap:4px">
                <input type="checkbox" x-model="logsAutoRefresh" @change="toggleLogsPolling()"> Auto
              </label>
              <button class="btn btn-sm btn-ghost" @click="loadLogs()">Refresh</button>
            </div>
          </div>
          <table>
            <thead><tr><th>Time</th><th>Method</th><th>Path</th><th>Status</th><th>Latency</th><th>IP</th><th>Model</th></tr></thead>
            <tbody>
              <template x-for="e in (logs.items ?? [])" :key="e.timestamp + e.path">
                <tr>
                  <td style="font-size:0.75rem; white-space:nowrap" x-text="formatTime(new Date(e.timestamp * 1000).toISOString())"></td>
                  <td><span class="badge badge-ok" x-text="e.method"></span></td>
                  <td style="font-family:monospace; font-size:0.8rem; max-width:250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap" x-text="e.path"></td>
                  <td><span :class="e.status_code < 400 ? 'badge badge-ok' : e.status_code < 500 ? 'badge badge-warn' : 'badge badge-err'" x-text="e.status_code"></span></td>
                  <td style="font-size:0.8rem" x-text="e.response_time_ms + 'ms'"></td>
                  <td style="font-size:0.8rem; font-family:monospace" x-text="e.client_ip"></td>
                  <td style="font-size:0.8rem" x-text="e.model || '-'"></td>
                </tr>
              </template>
            </tbody>
          </table>
          <div x-show="(logs.items ?? []).length === 0"
            style="color:var(--text-muted); padding:1rem; text-align:center">No logs yet</div>
          <div x-show="logs.total > 50" style="display:flex; justify-content:center; gap:0.5rem; margin-top:0.75rem">
            <button class="btn btn-sm btn-ghost" @click="logsPage = Math.max(0, logsPage-1); loadLogs()" :disabled="logsPage === 0">Prev</button>
            <span style="font-size:0.8rem; padding:4px 8px; color:var(--text-muted)" x-text="'Page ' + (logsPage+1)"></span>
            <button class="btn btn-sm btn-ghost" @click="logsPage++; loadLogs()" :disabled="(logsPage+1)*50 >= logs.total">Next</button>
          </div>
          <div style="font-size:0.75rem; color:var(--text-muted); margin-top:0.5rem; text-align:right"
            x-text="'Latency = handler creation time only (streaming completion not included)'"></div>
        </div>
      </div>

      <!-- Rate Limits Tab -->
      <div x-show="tab==='ratelimits'">
        <div class="card" style="margin-bottom:0.75rem">
          <p style="font-size:0.8rem; color:var(--text-muted); margin:0">
            Approximate monitoring based on request logs. Actual enforcement is handled by slowapi.
          </p>
        </div>
        <div class="grid-2">
          <template x-for="(data, bucket) in (rateLimits.snapshot ?? {})" :key="bucket">
            <div class="card">
              <h3 x-text="bucket" style="text-transform:uppercase"></h3>
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem">
                <span style="font-size:0.85rem" x-text="data.total_usage + ' / ' + data.limit + ' req/min'"></span>
                <span :class="(data.total_usage / data.limit * 100) > 90 ? 'badge badge-err' :
                  (data.total_usage / data.limit * 100) > 70 ? 'badge badge-warn' : 'badge badge-ok'"
                  x-text="Math.round(data.total_usage / data.limit * 100) + '%'"></span>
              </div>
              <div style="background:var(--subtle-bg); border-radius:4px; height:8px; overflow:hidden; margin-bottom:0.75rem">
                <div :style="'width:' + Math.min(100, data.total_usage / data.limit * 100) + '%; height:100%; border-radius:4px; background:' +
                  ((data.total_usage / data.limit * 100) > 90 ? '#ef4444' : (data.total_usage / data.limit * 100) > 70 ? '#f59e0b' : '#16a34a')"></div>
              </div>
              <template x-if="data.clients && data.clients.length > 0">
                <table>
                  <thead><tr><th>IP</th><th>Count</th><th>Usage</th></tr></thead>
                  <tbody>
                    <template x-for="c in data.clients" :key="c.ip">
                      <tr>
                        <td style="font-family:monospace; font-size:0.8rem" x-text="c.ip"></td>
                        <td x-text="c.count"></td>
                        <td><span :class="c.pct_used > 90 ? 'badge badge-err' : c.pct_used > 70 ? 'badge badge-warn' : 'badge badge-ok'"
                          x-text="c.pct_used + '%'"></span></td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </template>
              <div x-show="!data.clients || data.clients.length === 0"
                style="font-size:0.8rem; color:var(--text-muted)">No traffic</div>
            </div>
          </template>
        </div>
      </div>

      <!-- Workspace Tab -->
      <div x-show="tab==='files'">
        <div class="sidebar">
          <div class="file-tree card">
            <h3>Files</h3>
            <template x-for="f in files" :key="f.path">
              <div class="file-item" :class="{ active: editor.path === f.path }" @click="openFile(f.path)">
                <span class="icon" x-text="f.path.endsWith('.json') ? '{...}' : f.path.endsWith('.md') ? '#' : '~'"></span>
                <span x-text="f.path.split('/').pop()"></span>
              </div>
            </template>
            <div x-show="files.length === 0" style="color:var(--text-muted); font-size:0.85rem; padding:8px 12px">
              No files found
            </div>
          </div>
          <div class="editor-area card">
            <template x-if="!editor.path">
              <div style="color:var(--text-muted); padding:2rem; text-align:center">
                Select a file to edit
              </div>
            </template>
            <template x-if="editor.path">
              <div>
                <div class="editor-toolbar">
                  <span class="path" x-text="editor.path"></span>
                  <div style="display:flex; gap:0.5rem; align-items:center">
                    <span x-show="editor.dirty" class="dirty-dot" title="Unsaved changes"></span>
                    <button class="btn btn-sm btn-primary" @click="saveFile()" :disabled="!editor.dirty">Save</button>
                  </div>
                </div>
                <textarea x-ref="editorArea" style="display:none"></textarea>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- Skills Tab -->
      <div x-show="tab==='skills'">
        <div class="sidebar">
          <div class="file-tree card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem">
              <h3 style="margin:0">Skills</h3>
              <button class="btn btn-sm btn-primary" @click="showNewSkillForm()">+ New</button>
            </div>
            <template x-for="s in skills" :key="s.name">
              <div class="file-item" :class="{ active: selectedSkill === s.name }" @click="openSkill(s.name)">
                <span class="icon">&#9881;</span>
                <div style="flex:1; min-width:0">
                  <div style="font-size:0.85rem; font-weight:600" x-text="s.name"></div>
                  <div style="font-size:0.7rem; color:var(--text-muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis"
                    x-text="s.description || '(no description)'"></div>
                </div>
              </div>
            </template>
            <div x-show="skills.length === 0" style="color:var(--text-muted); font-size:0.85rem; padding:8px 12px">
              No skills found
            </div>
          </div>
          <div class="editor-area card">
            <!-- New skill form -->
            <template x-if="skillCreating">
              <div style="padding:1rem">
                <h3 style="margin-top:0">Create New Skill</h3>
                <label style="font-size:0.8rem; color:var(--text-muted)">Skill Name</label>
                <input type="text" x-model="newSkillName" placeholder="my-skill-name"
                  style="width:100%; margin-bottom:0.5rem" @input="validateNewSkillName()">
                <p x-show="newSkillNameError" style="color:#ef4444; font-size:0.8rem; margin:0 0 0.5rem 0" x-text="newSkillNameError"></p>
                <div style="display:flex; gap:0.5rem">
                  <button class="btn btn-sm btn-primary" @click="createSkill()" :disabled="!newSkillName || newSkillNameError">Create</button>
                  <button class="btn btn-sm btn-ghost" @click="skillCreating = false">Cancel</button>
                </div>
              </div>
            </template>
            <!-- No skill selected -->
            <template x-if="!selectedSkill && !skillCreating">
              <div style="color:var(--text-muted); padding:2rem; text-align:center">
                Select a skill to edit or create a new one
              </div>
            </template>
            <!-- Skill editor -->
            <template x-if="selectedSkill && !skillCreating">
              <div>
                <div class="editor-toolbar">
                  <div style="display:flex; align-items:center; gap:0.5rem">
                    <span class="path" x-text="selectedSkill"></span>
                    <span style="font-size:0.7rem; color:var(--text-muted)" x-text="(skillMeta.metadata?.version) ? 'v' + skillMeta.metadata.version : ''"></span>
                  </div>
                  <div style="display:flex; gap:0.5rem; align-items:center">
                    <span x-show="skillDirty" class="dirty-dot" title="Unsaved changes"></span>
                    <button class="btn btn-sm btn-primary" @click="saveSkill()" :disabled="!skillDirty">Save</button>
                    <button class="btn btn-sm btn-ghost" style="color:#ef4444" @click="confirmDeleteSkill()">Delete</button>
                  </div>
                </div>
                <textarea x-ref="skillEditorArea" style="display:none"></textarea>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- Sessions Tab -->
      <div x-show="tab==='sessions'">
        <div class="card">
          <h3>Active Sessions</h3>
          <table>
            <thead><tr><th>Session ID</th><th>Backend</th><th>Msgs</th><th>Turns</th><th>Last Active</th><th></th></tr></thead>
            <tbody>
              <template x-for="s in (summary.sessions?.sessions ?? [])" :key="s.session_id">
                <tr @click="toggleSessionHistory(s.session_id)" style="cursor:pointer">
                  <td style="font-family:monospace; font-size:0.8rem">
                    <span x-text="expandedSession === s.session_id ? '▼ ' : '▶ '"></span>
                    <span x-text="s.session_id?.substring(0,16) + '...'"></span>
                  </td>
                  <td><span class="badge" style="background:var(--subtle-bg); font-size:0.7rem" x-text="s.backend || 'claude'"></span></td>
                  <td x-text="s.message_count ?? '-'"></td>
                  <td x-text="s.turn_counter ?? '-'"></td>
                  <td x-text="formatTime(s.last_accessed)"></td>
                  <td style="white-space:nowrap">
                    <button class="btn btn-sm btn-ghost" @click.stop="exportSession(s.session_id)" title="Export JSON">Export</button>
                    <button class="btn btn-sm btn-ghost" @click.stop="deleteSession(s.session_id)">Delete</button>
                  </td>
                </tr>
                <!-- Expanded message history -->
                <template x-if="expandedSession === s.session_id && sessionMessages">
                  <tr>
                    <td colspan="6" style="padding:0">
                      <div style="background:var(--subtle-bg); padding:1rem; border-radius:0 0 4px 4px">
                        <!-- Session detail row -->
                        <div x-show="sessionDetail" style="display:flex; flex-wrap:wrap; gap:0.75rem; margin-bottom:0.75rem; font-size:0.75rem; color:var(--text-muted)">
                          <span x-text="'Backend: ' + (sessionDetail?.backend || '-')"></span>
                          <span x-text="'Turns: ' + (sessionDetail?.turn_counter ?? '-')"></span>
                          <span x-text="'TTL: ' + (sessionDetail?.ttl_minutes ?? '-') + 'min'"></span>
                          <span x-show="sessionDetail?.provider_session_id" x-text="'Provider: ' + (sessionDetail?.provider_session_id?.substring(0,16) || '') + '...'"></span>
                          <span x-text="'Created: ' + formatTime(sessionDetail?.created_at)"></span>
                        </div>
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem">
                          <span style="font-size:0.8rem; color:var(--text-muted)">
                            Message History (<span x-text="sessionMessages.total"></span> messages)
                          </span>
                          <span class="badge badge-warn" style="font-size:0.7rem">Content may contain sensitive data</span>
                        </div>
                        <template x-for="m in (sessionMessages.messages ?? [])" :key="m.index">
                          <div :style="'margin-bottom:0.5rem; padding:8px 12px; border-radius:6px; max-width:85%;' +
                            (m.role === 'user' ? 'background:var(--card-bg); margin-right:auto;' :
                             m.role === 'assistant' ? 'background:#16a34a22; margin-left:auto;' :
                             'background:#f59e0b22; margin-right:auto;')">
                            <div style="font-size:0.7rem; font-weight:600; margin-bottom:4px"
                              :style="m.role === 'user' ? 'color:var(--accent)' : m.role === 'assistant' ? 'color:#16a34a' : 'color:#f59e0b'"
                              x-text="m.role.toUpperCase() + (m.name ? ' (' + m.name + ')' : '')"></div>
                            <div style="font-size:0.8rem; white-space:pre-wrap; word-break:break-word"
                              x-text="m.content || '(empty)'"></div>
                            <span x-show="m.truncated" style="font-size:0.7rem; color:var(--text-muted); cursor:pointer"
                              @click.stop="loadFullMessage(s.session_id, m.index)">[...truncated - click to expand]</span>
                          </div>
                        </template>
                        <div x-show="!sessionMessages.messages || sessionMessages.messages.length === 0"
                          style="color:var(--text-muted); font-size:0.8rem; text-align:center">No messages</div>
                      </div>
                    </td>
                  </tr>
                </template>
              </template>
            </tbody>
          </table>
          <div x-show="(summary.sessions?.sessions ?? []).length === 0"
            style="color:var(--text-muted); padding:1rem; text-align:center">No active sessions</div>
        </div>
      </div>

      <!-- Config Tab -->
      <div x-show="tab==='config'">
        <!-- Runtime Config Editor (hot-reload) -->
        <div class="card" style="margin-bottom:1rem">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem">
            <h3 style="margin:0">Runtime Settings <span style="font-size:0.7rem; color:var(--accent)">(hot-reload)</span></h3>
            <div style="display:flex; gap:0.5rem">
              <button class="btn btn-sm btn-ghost" @click="resetAllRuntimeConfig()">Reset All</button>
              <button class="btn btn-sm btn-ghost" @click="loadRuntimeConfig()">Refresh</button>
            </div>
          </div>
          <p style="font-size:0.8rem; color:var(--text-muted); margin-bottom:0.75rem">
            Changes take effect on the next request. No server restart needed.
          </p>
          <table>
            <thead><tr><th>Setting</th><th>Value</th><th>Original</th><th></th></tr></thead>
            <tbody>
              <template x-for="(meta, key) in (runtimeConfig ?? {})" :key="key">
                <tr>
                  <td>
                    <div class="config-key" x-text="meta.label"></div>
                    <div style="font-size:0.7rem; color:var(--text-muted)" x-text="meta.description"></div>
                  </td>
                  <td style="min-width:160px">
                    <template x-if="meta.type === 'bool'">
                      <select :value="meta.value ? 'true' : 'false'" @change="updateRuntimeConfig(key, $event.target.value === 'true')"
                        style="padding:4px 8px; font-size:0.8rem">
                        <option value="true">true</option>
                        <option value="false">false</option>
                      </select>
                    </template>
                    <template x-if="meta.type === 'int'">
                      <input type="number" :value="meta.value" min="1"
                        @change="updateRuntimeConfig(key, parseInt($event.target.value))"
                        style="padding:4px 8px; font-size:0.8rem; width:100px">
                    </template>
                    <template x-if="meta.type === 'string'">
                      <input type="text" :value="meta.value"
                        @change="updateRuntimeConfig(key, $event.target.value)"
                        style="padding:4px 8px; font-size:0.8rem; width:160px">
                    </template>
                  </td>
                  <td style="font-size:0.8rem; color:var(--text-muted)" x-text="meta.original"></td>
                  <td>
                    <span x-show="meta.overridden" class="badge badge-warn" style="cursor:pointer; font-size:0.7rem"
                      @click="resetRuntimeConfig(key)">reset</span>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

        <!-- System Prompt Editor -->
        <div class="card" style="margin-bottom:1rem">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem">
            <h3 style="margin:0">System Prompt
              <span x-show="systemPrompt.mode === 'preset'" class="badge" style="font-size:0.65rem">preset</span>
              <span x-show="systemPrompt.mode === 'file'" class="badge badge-warn" style="font-size:0.65rem">file default</span>
              <span x-show="systemPrompt.mode === 'custom'" class="badge badge-ok" style="font-size:0.65rem">custom override</span>
            </h3>
            <div style="display:flex; gap:0.5rem; align-items:center">
              <span style="font-size:0.75rem; color:var(--text-muted)" x-text="systemPrompt.char_count + ' chars'"></span>
              <button class="btn btn-sm btn-ghost" @click="resetSystemPrompt()"
                x-show="systemPrompt.mode === 'custom'">Reset</button>
              <button class="btn btn-sm btn-ghost" @click="loadSystemPrompt()">Refresh</button>
            </div>
          </div>
          <p style="font-size:0.8rem; color:var(--text-muted); margin-bottom:0.75rem">
            Custom system prompt replaces the <code>claude_code</code> preset. Changes only affect <strong>new sessions</strong>.
          </p>
          <!-- Preset view: read-only display with customize button -->
          <template x-if="systemPrompt.mode === 'preset' && !systemPromptEditing && systemPrompt.preset_text">
            <div>
              <textarea readonly
                :value="systemPrompt.preset_text"
                style="width:100%; min-height:200px; max-height:500px; font-family:monospace; font-size:0.8rem;
                  background:var(--bg-card); color:var(--text-muted); border:1px solid var(--border); border-radius:4px;
                  padding:8px; resize:vertical; opacity:0.7; cursor:default"></textarea>
              <div style="margin-top:0.5rem; display:flex; gap:0.5rem; align-items:center">
                <button class="btn btn-sm" @click="systemPromptText = systemPrompt.preset_text; systemPromptEditing = true">Customize</button>
                <span style="font-size:0.75rem; color:var(--text-muted)"
                  x-text="systemPrompt.preset_text.length + ' chars (read-only preset)'"></span>
              </div>
            </div>
          </template>
          <!-- Edit view: active when customizing or already in custom/file mode -->
          <template x-if="systemPrompt.mode !== 'preset' || systemPromptEditing || !systemPrompt.preset_text">
            <div>
              <textarea x-model="systemPromptText"
                style="width:100%; min-height:200px; max-height:500px; font-family:monospace; font-size:0.8rem;
                  background:var(--bg-card); color:var(--text); border:1px solid var(--border); border-radius:4px;
                  padding:8px; resize:vertical"
                placeholder="Leave empty to use claude_code preset..."></textarea>
              <div style="margin-top:0.5rem; display:flex; gap:0.5rem; align-items:center">
                <button class="btn btn-sm" @click="saveSystemPrompt()"
                  :disabled="!systemPromptText.trim()">Save</button>
                <button class="btn btn-sm btn-ghost" @click="systemPromptEditing = false; systemPromptText = ''"
                  x-show="systemPromptEditing && systemPrompt.mode === 'preset'">Cancel</button>
                <span style="font-size:0.75rem; color:var(--text-muted)"
                  x-show="systemPromptText.trim()"
                  x-text="systemPromptText.trim().length + ' chars'"></span>
              </div>
            </div>
          </template>
        </div>

        <div class="grid-2">
          <div class="card">
            <h3>Runtime</h3>
            <table>
              <tbody>
                <template x-for="(v, k) in (config.runtime ?? {})" :key="k">
                  <tr><td class="config-key" x-text="k"></td><td x-text="v"></td></tr>
                </template>
              </tbody>
            </table>
          </div>
          <div class="card">
            <h3>Rate Limits <span style="font-size:0.75rem; color:var(--text-muted)">(req/min)</span></h3>
            <table>
              <tbody>
                <template x-for="(v, k) in (config.rate_limits ?? {})" :key="k">
                  <tr><td class="config-key" x-text="k"></td><td x-text="v"></td></tr>
                </template>
              </tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <h3>Environment</h3>
          <table>
            <tbody>
              <template x-for="(v, k) in (config.environment ?? {})" :key="k">
                <tr>
                  <td class="config-key" x-text="k"></td>
                  <td :class="{ redacted: v === '***REDACTED***' || v === '(not set)' }" x-text="v"></td>
                </tr>
              </template>
            </tbody>
          </table>
          <p style="font-size:0.8rem; color:var(--text-muted); margin-top:0.5rem"
            x-text="config._note || ''"></p>
        </div>
        <template x-if="config.mcp_servers">
          <div class="card">
            <h3>MCP Servers</h3>
            <template x-for="s in config.mcp_servers" :key="s">
              <span class="badge badge-ok" style="margin-right:0.5rem" x-text="s"></span>
            </template>
          </div>
        </template>
      </div>

    </div>
  </template>
</div>

<script>
function adminApp() {
  return {
    authenticated: false,
    loginKey: '',
    loginError: '',
    tab: 'dashboard',
    summary: {},
    files: [],
    config: {},
    editor: { path: null, content: '', etag: null, dirty: false },
    cm: null,
    toast: { show: false, msg: '', type: 'ok' },
    pollTimer: null,
    logs: {},
    logsFilter: { endpoint: '', status: '' },
    logsPage: 0,
    logsAutoRefresh: false,
    logsPollTimer: null,
    rateLimits: {},
    backendsDetail: [],
    mcpServers: [],
    expandedSession: null,
    sessionMessages: null,
    sessionDetail: null,
    runtimeConfig: {},
    skills: [],
    selectedSkill: null,
    skillContent: '',
    skillEtag: null,
    skillDirty: false,
    skillMeta: {},
    skillCm: null,
    skillCreating: false,
    newSkillName: '',
    newSkillNameError: '',
    systemPrompt: { mode: 'preset', prompt: null, default_prompt: null, preset_text: null, char_count: 0 },
    systemPromptText: '',
    systemPromptEditing: false,

    async init() {
      // Check if already authenticated (cookie-based)
      try {
        const r = await this.api('/admin/api/summary');
        if (r.ok) { this.authenticated = true; this.summary = await r.json(); this.loadBackends(); this.loadMcpServers(); this.startPolling(); }
      } catch(e) {}
    },

    async doLogin() {
      this.loginError = '';
      try {
        const r = await fetch('/admin/api/login', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ api_key: this.loginKey })
        });
        if (r.ok) {
          this.authenticated = true;
          this.loginKey = '';
          await this.loadSummary();
          this.loadBackends();
          this.loadMcpServers();
          this.startPolling();
        } else {
          const d = await r.json();
          this.loginError = d.detail || 'Login failed';
        }
      } catch(e) { this.loginError = 'Connection error'; }
    },

    async doLogout() {
      await fetch('/admin/api/logout', { method: 'POST' });
      this.authenticated = false;
      this.stopPolling();
    },

    api(url, opts) { return fetch(url, { ...opts, credentials: 'same-origin' }); },

    async loadSummary() {
      try {
        const r = await this.api('/admin/api/summary');
        if (r.ok) this.summary = await r.json();
        else if (r.status === 401) { this.authenticated = false; this.stopPolling(); }
      } catch(e) {}
    },

    async loadBackends() {
      try {
        const r = await this.api('/admin/api/backends');
        if (r.ok) { const d = await r.json(); this.backendsDetail = d.backends || []; }
      } catch(e) {}
    },
    async loadMcpServers() {
      try {
        const r = await this.api('/admin/api/mcp-servers');
        if (r.ok) { const d = await r.json(); this.mcpServers = d.servers || []; }
      } catch(e) {}
    },

    async loadFiles() {
      try {
        const r = await this.api('/admin/api/files');
        if (r.ok) { const d = await r.json(); this.files = d.files || []; }
      } catch(e) {}
    },

    async loadConfig() {
      try {
        const r = await this.api('/admin/api/config');
        if (r.ok) this.config = await r.json();
      } catch(e) {}
    },

    async openFile(path) {
      if (this.editor.dirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      try {
        const r = await this.api('/admin/api/files/' + encodeURI(path));
        if (r.ok) {
          const d = await r.json();
          this.editor = { path: d.path, content: d.content, etag: d.etag, dirty: false };
          this.$nextTick(() => this.setupEditor());
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Failed to load file', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },

    setupEditor() {
      const ta = this.$refs.editorArea;
      if (!ta) return;
      if (this.cm) { this.cm.toTextArea(); this.cm = null; }
      ta.value = this.editor.content;
      const ext = this.editor.path.split('.').pop();
      const mode = ext === 'json' ? { name: 'javascript', json: true }
        : ext === 'yaml' || ext === 'yml' ? 'yaml' : 'markdown';
      this.cm = CodeMirror.fromTextArea(ta, {
        mode, theme: 'material-darker', lineNumbers: true, lineWrapping: true, tabSize: 2
      });
      this.cm.on('change', () => {
        this.editor.dirty = this.cm.getValue() !== this.editor.content;
      });
    },

    async saveFile() {
      if (!this.editor.path || !this.cm) return;
      const newContent = this.cm.getValue();
      try {
        const r = await this.api('/admin/api/files/' + encodeURI(this.editor.path), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: newContent, etag: this.editor.etag })
        });
        const d = await r.json();
        if (r.ok) {
          this.editor.content = newContent;
          this.editor.etag = d.etag;
          this.editor.dirty = false;
          this.showToast('Saved', 'ok');
        } else {
          this.showToast(d.error || 'Save failed', 'err');
          if (r.status === 409) {
            if (confirm('File was modified externally. Reload?')) this.openFile(this.editor.path);
          }
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },

    async deleteSession(id) {
      if (!confirm('Delete session ' + id.substring(0,16) + '...?')) return;
      try {
        const r = await this.api('/admin/api/sessions/' + id, { method: 'DELETE' });
        if (r.ok) { this.showToast('Session deleted', 'ok'); await this.loadSummary(); }
        else { const d = await r.json(); this.showToast(d.error || 'Delete failed', 'err'); }
      } catch(e) { this.showToast('Failed to delete', 'err'); }
    },

    async refreshAll() {
      await Promise.all([this.loadSummary(), this.loadFiles(), this.loadConfig(), this.loadBackends(), this.loadMcpServers()]);
      this.showToast('Refreshed', 'ok');
    },

    // --- Logs ---
    async loadLogs() {
      try {
        let url = '/admin/api/logs?limit=50&offset=' + (this.logsPage * 50);
        if (this.logsFilter.endpoint) url += '&endpoint=' + encodeURIComponent(this.logsFilter.endpoint);
        if (this.logsFilter.status) url += '&status=' + this.logsFilter.status;
        const r = await this.api(url);
        if (r.ok) this.logs = await r.json();
      } catch(e) {}
    },
    toggleLogsPolling() {
      if (this.logsPollTimer) { clearInterval(this.logsPollTimer); this.logsPollTimer = null; }
      if (this.logsAutoRefresh) { this.logsPollTimer = setInterval(() => this.loadLogs(), 5000); }
    },

    // --- Rate Limits ---
    async loadRateLimits() {
      try {
        const r = await this.api('/admin/api/rate-limits');
        if (r.ok) this.rateLimits = await r.json();
      } catch(e) {}
    },

    // --- Runtime Config ---
    async loadRuntimeConfig() {
      try {
        const r = await this.api('/admin/api/runtime-config');
        if (r.ok) { const d = await r.json(); this.runtimeConfig = d.settings || {}; }
      } catch(e) {}
    },
    async updateRuntimeConfig(key, value) {
      try {
        const r = await this.api('/admin/api/runtime-config', {
          method: 'PATCH', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ key, value })
        });
        if (r.ok) { this.showToast('Updated: ' + key, 'ok'); await this.loadRuntimeConfig(); }
        else { const d = await r.json(); this.showToast(d.error || 'Update failed', 'err'); }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async resetRuntimeConfig(key) {
      try {
        const r = await this.api('/admin/api/runtime-config/reset?key=' + encodeURIComponent(key), { method: 'POST' });
        if (r.ok) { this.showToast('Reset: ' + key, 'ok'); await this.loadRuntimeConfig(); }
      } catch(e) {}
    },
    async resetAllRuntimeConfig() {
      if (!confirm('Reset all runtime settings to startup defaults?')) return;
      try {
        const r = await this.api('/admin/api/runtime-config/reset', { method: 'POST' });
        if (r.ok) { this.showToast('All settings reset', 'ok'); await this.loadRuntimeConfig(); }
      } catch(e) {}
    },

    // --- System Prompt ---
    async loadSystemPrompt() {
      try {
        const r = await this.api('/admin/api/system-prompt');
        if (r.ok) {
          const d = await r.json();
          this.systemPrompt = d;
          this.systemPromptText = d.prompt || '';
          this.systemPromptEditing = false;
        }
      } catch(e) {}
    },
    async saveSystemPrompt() {
      const text = this.systemPromptText.trim();
      if (!text) { this.showToast('Prompt cannot be empty. Use Reset instead.', 'err'); return; }
      try {
        const r = await this.api('/admin/api/system-prompt', {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ prompt: text })
        });
        if (r.ok) { this.showToast('System prompt updated', 'ok'); await this.loadSystemPrompt(); }
        else { const d = await r.json(); this.showToast(d.error || 'Update failed', 'err'); }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async resetSystemPrompt() {
      if (!confirm('Reset system prompt to default?')) return;
      try {
        const r = await this.api('/admin/api/system-prompt', { method: 'DELETE' });
        if (r.ok) { this.showToast('System prompt reset', 'ok'); await this.loadSystemPrompt(); }
      } catch(e) {}
    },

    // --- Skills ---
    async loadSkills() {
      try {
        const r = await this.api('/admin/api/skills');
        if (r.ok) { const d = await r.json(); this.skills = d.skills || []; }
      } catch(e) {}
    },
    async openSkill(name) {
      if (this.skillDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      this.skillCreating = false;
      try {
        const r = await this.api('/admin/api/skills/' + encodeURIComponent(name));
        if (r.ok) {
          const d = await r.json();
          this.selectedSkill = name;
          this.skillContent = d.content;
          this.skillEtag = d.etag;
          this.skillMeta = d.metadata || {};
          this.skillDirty = false;
          this.$nextTick(() => this.setupSkillEditor());
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Failed to load skill', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    setupSkillEditor() {
      const ta = this.$refs.skillEditorArea;
      if (!ta) return;
      if (this.skillCm) { this.skillCm.toTextArea(); this.skillCm = null; }
      ta.value = this.skillContent;
      this.skillCm = CodeMirror.fromTextArea(ta, {
        mode: 'markdown', theme: 'material-darker', lineNumbers: true, lineWrapping: true, tabSize: 2
      });
      this.skillCm.on('change', () => {
        this.skillDirty = this.skillCm.getValue() !== this.skillContent;
      });
    },
    async saveSkill() {
      if (!this.selectedSkill || !this.skillCm) return;
      const newContent = this.skillCm.getValue();
      try {
        const r = await this.api('/admin/api/skills/' + encodeURIComponent(this.selectedSkill), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: newContent, etag: this.skillEtag })
        });
        const d = await r.json();
        if (r.ok || r.status === 201) {
          this.skillContent = newContent;
          this.skillEtag = d.etag;
          this.skillDirty = false;
          this.showToast('Skill saved', 'ok');
          await this.loadSkills();
        } else {
          this.showToast(d.error || 'Save failed', 'err');
          if (r.status === 409) {
            if (confirm('Skill was modified externally. Reload?')) this.openSkill(this.selectedSkill);
          }
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    showNewSkillForm() {
      if (this.skillDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      this.skillCreating = true;
      this.selectedSkill = null;
      this.skillDirty = false;
      this.newSkillName = '';
      this.newSkillNameError = '';
      if (this.skillCm) { this.skillCm.toTextArea(); this.skillCm = null; }
    },
    validateNewSkillName() {
      const n = this.newSkillName;
      if (!n) { this.newSkillNameError = ''; return; }
      if (!/^[a-z0-9][a-z0-9-]*$/.test(n)) {
        this.newSkillNameError = 'Lowercase letters, digits, and hyphens only (start with letter/digit)';
        return;
      }
      if (this.skills.some(s => s.name === n)) {
        this.newSkillNameError = 'Skill already exists';
        return;
      }
      this.newSkillNameError = '';
    },
    async createSkill() {
      if (!this.newSkillName || this.newSkillNameError) return;
      const name = this.newSkillName;
      const template = `---
name: ${name}
description: ""
metadata:
  author: ""
  version: "1.0.0"
---

# ${name}

Skill description here.
`;
      try {
        const r = await this.api('/admin/api/skills/' + encodeURIComponent(name), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: template })
        });
        if (r.ok || r.status === 201) {
          this.skillCreating = false;
          this.showToast('Skill created: ' + name, 'ok');
          await this.loadSkills();
          await this.openSkill(name);
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Create failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async confirmDeleteSkill() {
      if (!this.selectedSkill) return;
      if (!confirm('Delete skill "' + this.selectedSkill + '"? This cannot be undone.')) return;
      try {
        const r = await this.api('/admin/api/skills/' + encodeURIComponent(this.selectedSkill), { method: 'DELETE' });
        if (r.ok) {
          this.showToast('Skill deleted', 'ok');
          if (this.skillCm) { this.skillCm.toTextArea(); this.skillCm = null; }
          this.selectedSkill = null;
          this.skillDirty = false;
          await this.loadSkills();
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Delete failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },

    // --- Session Messages ---
    async toggleSessionHistory(sessionId) {
      if (this.expandedSession === sessionId) {
        this.expandedSession = null;
        this.sessionMessages = null;
        this.sessionDetail = null;
        return;
      }
      this.expandedSession = sessionId;
      this.sessionMessages = null;
      this.sessionDetail = null;
      try {
        const [msgR, detR] = await Promise.all([
          this.api('/admin/api/sessions/' + encodeURIComponent(sessionId) + '/messages?truncate=500'),
          this.api('/admin/api/sessions/' + encodeURIComponent(sessionId) + '/detail')
        ]);
        if (this.expandedSession !== sessionId) return;
        if (msgR.ok) this.sessionMessages = await msgR.json();
        else { this.showToast('Failed to load messages', 'err'); this.expandedSession = null; return; }
        if (detR.ok) this.sessionDetail = await detR.json();
      } catch(e) { if (this.expandedSession === sessionId) { this.showToast('Connection error', 'err'); this.expandedSession = null; } }
    },
    async exportSession(sessionId) {
      try {
        const r = await this.api('/admin/api/sessions/' + encodeURIComponent(sessionId) + '/export');
        if (!r.ok) { this.showToast('Export failed', 'err'); return; }
        const data = await r.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'session-' + sessionId.substring(0, 8) + '.json';
        a.click(); URL.revokeObjectURL(url);
        this.showToast('Session exported', 'ok');
      } catch(e) { this.showToast('Export failed', 'err'); }
    },
    async loadFullMessage(sessionId, msgIndex) {
      try {
        const r = await this.api('/admin/api/sessions/' + encodeURIComponent(sessionId) + '/messages?truncate=0');
        if (!r.ok) return;
        const data = await r.json();
        // Guard after all async work: only mutate state if still viewing the same session
        if (this.expandedSession !== sessionId) return;
        if (this.sessionMessages && this.sessionMessages.messages) {
          const full = data.messages.find(m => m.index === msgIndex);
          if (full) {
            const idx = this.sessionMessages.messages.findIndex(m => m.index === msgIndex);
            if (idx >= 0) { this.sessionMessages.messages[idx] = full; }
          }
        }
      } catch(e) {}
    },

    startPolling() { this.pollTimer = setInterval(() => this.loadSummary(), 15000); },
    stopPolling() {
      if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
      if (this.logsPollTimer) { clearInterval(this.logsPollTimer); this.logsPollTimer = null; }
    },

    showToast(msg, type) {
      this.toast = { show: true, msg, type };
      setTimeout(() => { this.toast.show = false; }, 2500);
    },

    formatTime(t) {
      if (!t) return '-';
      try { return new Date(t).toLocaleString('ko-KR', { hour12: false }); }
      catch(e) { return t; }
    },

    toggleTheme() {
      const el = document.documentElement;
      el.dataset.theme = el.dataset.theme === 'dark' ? 'light' : 'dark';
    }
  };
}
</script>
</body>
</html>"""
