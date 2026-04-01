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
/* === Semantic Color Tokens === */
:root {
  --accent: #22c55e; --accent-hover: #16a34a;
  --color-success: #22c55e; --color-success-subtle: #22c55e1a;
  --color-warning: #f59e0b; --color-warning-subtle: #f59e0b1a;
  --color-danger: #ef4444; --color-danger-subtle: #ef44441a;
  --color-info: #3b82f6; --color-info-subtle: #3b82f61a;
}
[data-theme="dark"] {
  --card-bg: #1e293b; --subtle-bg: #334155; --border: #475569; --page-bg: #0f172a;
  --text: #f1f5f9; --text-muted: #a8bbd2;
}
[data-theme="light"] {
  --card-bg: #fff; --subtle-bg: #f1f5f9; --border: #e2e8f0; --page-bg: #f8fafc;
  --text: #1e293b; --text-muted: #64748b;
}

/* === Base === */
body { background: var(--page-bg); font-size: 14px; }
.container { max-width: 1200px; margin: 0 auto; padding: 1rem; }

/* === Utility Classes === */
.flex-between { display: flex; justify-content: space-between; align-items: center; }
.flex-gap-sm { display: flex; gap: 0.5rem; align-items: center; }
.flex-wrap-gap { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.text-mono { font-family: monospace; font-size: 0.8rem; }
.text-xs { font-size: 0.75rem; }
.text-sm { font-size: 0.8rem; }
.text-muted { color: var(--text-muted); }
.text-danger { color: var(--color-danger); }
.text-success { color: var(--color-success); }
.text-warning { color: var(--color-warning); }
.text-info { color: var(--color-info); }
.mb-sm { margin-bottom: 0.5rem; }
.mb-md { margin-bottom: 0.75rem; }
.mb-lg { margin-bottom: 1rem; }

/* === Cards === */
.card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem; position: relative; }
.card h3 { margin-top: 0; font-size: 1rem; color: var(--text-muted); }

/* === Grid === */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; }
.grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; }

/* === Stats === */
.stat { text-align: center; }
.stat .value { font-size: 2rem; font-weight: bold; color: var(--accent); }
.stat .label { font-size: 0.85rem; color: var(--text-muted); }

/* === Badges === */
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; }
.badge-ok { background: var(--color-success-subtle); color: var(--color-success); }
.badge-warn { background: var(--color-warning-subtle); color: var(--color-warning); }
.badge-err { background: var(--color-danger-subtle); color: var(--color-danger); }
.badge-info { background: var(--color-info-subtle); color: var(--color-info); }

/* === Tabs (ARIA tablist) === */
nav.tabs { display: flex; gap: 0; border-bottom: 2px solid var(--border); margin-bottom: 1rem; overflow-x: auto; scrollbar-width: none; }
nav.tabs::-webkit-scrollbar { display: none; }
nav.tabs button { background: none; border: none; padding: 0.75rem 1.25rem; color: var(--text-muted);
  cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; font-size: 0.9rem; white-space: nowrap; flex-shrink: 0; }
nav.tabs button[aria-selected="true"] { color: var(--accent); border-bottom-color: var(--accent); }
nav.tabs button:hover { color: var(--text); }

/* === Sidebar / Editor === */
.sidebar { display: flex; gap: 1rem; }
.sidebar .file-tree { width: 260px; flex-shrink: 0; }
.sidebar .editor-area { flex: 1; min-width: 0; }
.file-item { padding: 6px 12px; cursor: pointer; border-radius: 4px; font-size: 0.85rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: flex; align-items: center; }
.file-item:hover { background: var(--subtle-bg); }
.file-item.active { background: var(--accent); color: #fff; }
.file-item .icon { margin-right: 6px; font-size: 0.9rem; }
.file-icon-json { color: var(--color-warning); }
.file-icon-md { color: var(--color-info); }
.file-icon-yaml { color: #8b5cf6; }
.file-icon-default { color: var(--text-muted); }
.CodeMirror { height: auto; min-height: 300px; max-height: 70vh; border: 1px solid var(--border); border-radius: 4px; font-size: 13px; overflow: hidden; }
.CodeMirror-scroll { max-height: 70vh; overflow-y: auto !important; }
.editor-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
.editor-toolbar .path { font-family: monospace; font-size: 0.85rem; color: var(--text-muted); }

/* === Buttons === */
.btn { display: inline-block; padding: 6px 16px; border-radius: 6px; border: none;
  cursor: pointer; font-size: 0.85rem; font-weight: 500; transition: opacity 0.15s; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-sm { padding: 4px 10px; font-size: 0.8rem; }
.btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--text); }
.btn-danger-ghost { background: transparent; border: 1px solid var(--color-danger); color: var(--color-danger); }
.btn-danger-ghost:hover { background: var(--color-danger-subtle); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* === Login === */
.login-box { max-width: 400px; margin: 4rem auto; }

/* === Tables === */
.table-wrapper { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; font-size: 0.85rem; }
table th { color: var(--text-muted); font-weight: 600; text-align: left; }
table td, table th { padding: 8px 12px; border-bottom: 1px solid var(--border); }

/* === Toast Queue === */
.toast-container { position: fixed; bottom: 1.5rem; right: 1.5rem; display: flex; flex-direction: column-reverse; gap: 0.5rem; z-index: 100; }
.toast { padding: 12px 20px; border-radius: 8px; font-size: 0.85rem;
  animation: toast-in 0.3s ease; }
.toast-ok { background: var(--color-success); color: #fff; }
.toast-err { background: var(--color-danger); color: #fff; }
@keyframes toast-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

/* === Loading / Skeleton === */
.skeleton { border-radius: 4px; background: linear-gradient(90deg, var(--subtle-bg) 25%, var(--card-bg) 50%, var(--subtle-bg) 75%);
  background-size: 200% 100%; animation: shimmer 1.5s infinite; }
.skeleton-row { height: 20px; margin-bottom: 8px; }
.skeleton-stat { height: 3rem; width: 4rem; margin: 0 auto; }
.skeleton-card { height: 80px; }
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
.card-loading { min-height: 60px; }

/* === Misc === */
.dirty-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--color-warning); margin-left: 6px; }
.config-key { font-family: monospace; font-size: 0.8rem; color: var(--accent); }
.redacted { color: var(--text-muted); font-style: italic; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* === Accordion (Config) === */
details.config-section { border: 1px solid var(--border); border-radius: 8px; background: var(--card-bg); margin-bottom: 1rem; }
details.config-section > summary { cursor: pointer; padding: 1rem 1.25rem; list-style: none; display: flex; justify-content: space-between; align-items: center; font-weight: 600; font-size: 0.95rem; }
details.config-section > summary::-webkit-details-marker { display: none; }
details.config-section > summary::before { content: '▸'; margin-right: 0.5rem; transition: transform 0.2s; }
details.config-section[open] > summary::before { transform: rotate(90deg); }
details.config-section[open] > summary { border-bottom: 1px solid var(--border); }
details.config-section .config-body { padding: 1.25rem; }

/* === Latency Bar === */
.latency-bar { flex: 1; height: 6px; background: var(--subtle-bg); border-radius: 3px; margin: 0 0.5rem; }
.latency-fill { height: 100%; border-radius: 3px; transition: width 0.4s ease; }

/* === Session Messages Panel === */
.msg-bubble { margin-bottom: 0.5rem; padding: 8px 12px; border-radius: 6px; font-size: 0.8rem; white-space: pre-wrap; word-break: break-word; }
.msg-user { background: var(--subtle-bg); margin-right: auto; max-width: 75%; }
.msg-assistant { background: var(--color-success-subtle); margin-left: auto; max-width: 75%; }
.msg-system { background: var(--color-warning-subtle); margin: 0 auto; max-width: 90%; text-align: center; }
.msg-role { font-size: 0.7rem; font-weight: 600; margin-bottom: 4px; }

/* === Rate Limit Bar === */
.rate-bar-fill { transition: width 0.4s ease, background-color 0.4s ease; }

/* === Responsive === */
@media (max-width: 768px) {
  .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
  .sidebar { flex-direction: column; }
  .sidebar .file-tree { width: 100%; max-height: 200px; overflow-y: auto; }
  nav.tabs button { padding: 0.75rem 1rem; }
  .CodeMirror { min-height: 200px; max-height: 50vh; }
}
</style>
</head>
<body>

<div x-data="adminApp()" x-init="init()" class="container">

  <!-- Toast Queue -->
  <div class="toast-container">
    <template x-for="t in toasts" :key="t.id">
      <div x-transition.opacity :class="'toast toast-' + t.type" x-text="t.msg"></div>
    </template>
  </div>

  <!-- Login -->
  <template x-if="!authenticated">
    <div class="login-box card">
      <h2 style="margin-top:0">Admin Login</h2>
      <p class="text-muted">ADMIN_API_KEY required</p>
      <form @submit.prevent="doLogin()">
        <input type="password" x-model="loginKey" placeholder="Admin API Key"
          style="width:100%; margin-bottom:1rem" required>
        <button class="btn btn-primary" style="width:100%" type="submit">Login</button>
      </form>
      <p x-show="loginError" class="text-danger text-sm" style="margin-top:0.5rem" x-text="loginError"></p>
    </div>
  </template>

  <!-- Main UI -->
  <template x-if="authenticated">
    <div>
      <!-- Header -->
      <div class="flex-between mb-lg">
        <h1 style="margin:0; font-size:1.5rem">Claude Code Gateway Admin</h1>
        <div class="flex-gap-sm">
          <button class="btn btn-sm btn-ghost" @click="refreshAll()" aria-label="Refresh all data">Refresh</button>
          <button class="btn btn-sm btn-ghost" @click="toggleTheme()" aria-label="Toggle theme">Theme</button>
          <button class="btn btn-sm btn-ghost" @click="doLogout()" aria-label="Log out">Logout</button>
        </div>
      </div>

      <!-- Tabs (ARIA tablist) -->
      <nav class="tabs" role="tablist" aria-label="Admin sections">
        <button role="tab" :aria-selected="tab === 'dashboard'" @click="tab='dashboard'">Dashboard</button>
        <button role="tab" :aria-selected="tab === 'sessions'" @click="tab='sessions'; loadSummary()">Sessions</button>
        <button role="tab" :aria-selected="tab === 'logs'" @click="tab='logs'; loadLogs()">Logs</button>
        <button role="tab" :aria-selected="tab === 'ratelimits'" @click="tab='ratelimits'; loadRateLimits()">Rate Limits</button>
        <button role="tab" :aria-selected="tab === 'files'" @click="tab='files'; loadFiles()">Workspace</button>
        <button role="tab" :aria-selected="tab === 'skills'" @click="tab='skills'; loadSkills()">Skills</button>
        <button role="tab" :aria-selected="tab === 'config'" @click="tab='config'; loadConfig(); loadRuntimeConfig(); loadSystemPrompt(); loadTools(); loadSandbox()">Config</button>
      </nav>

      <!-- Dashboard Tab -->
      <div x-show="tab==='dashboard'" role="tabpanel">
        <!-- Zone 1: Stat Cards (4-column) -->
        <template x-if="loading.dashboard">
          <div class="grid-4 mb-lg">
            <div class="card stat"><div class="skeleton skeleton-stat"></div><div class="label">Loading...</div></div>
            <div class="card stat"><div class="skeleton skeleton-stat"></div><div class="label">Loading...</div></div>
            <div class="card stat"><div class="skeleton skeleton-stat"></div><div class="label">Loading...</div></div>
            <div class="card stat"><div class="skeleton skeleton-stat"></div><div class="label">Loading...</div></div>
          </div>
        </template>
        <template x-if="!loading.dashboard">
          <div class="grid-4">
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
            <div class="card stat">
              <div class="value" :class="(metrics.stats?.error_rate ?? 0) > 0.05 ? 'text-danger' : ''"
                x-text="((metrics.stats?.error_rate ?? 0) * 100).toFixed(1) + '%'"></div>
              <div class="label">Error Rate</div>
            </div>
          </div>
        </template>

        <!-- Zone 2: Performance + Backend Health (2-column) -->
        <div class="grid-2">
          <!-- Performance Metrics -->
          <div class="card">
            <div class="flex-between mb-md">
              <h3 style="margin:0">Performance</h3>
              <button class="btn btn-sm btn-ghost" @click="loadMetrics()" aria-label="Refresh metrics">Refresh</button>
            </div>
            <div class="grid-3 mb-md" style="gap:0.5rem">
              <div class="stat" style="padding:0.25rem">
                <div class="value" style="font-size:1.3rem" x-text="metrics.stats?.total_requests ?? '-'"></div>
                <div class="label">Requests</div>
              </div>
              <div class="stat" style="padding:0.25rem">
                <div class="value" style="font-size:1.3rem" x-text="metrics.total_logged ?? '-'"></div>
                <div class="label">Logged</div>
              </div>
              <div class="stat" style="padding:0.25rem">
                <div class="value" style="font-size:1.3rem" x-text="(metrics.stats?.avg_latency_ms ?? '-') + 'ms'"></div>
                <div class="label">Avg Latency</div>
              </div>
            </div>
            <!-- Latency Bars -->
            <template x-for="item in [
              {label: 'p50', val: metrics.stats?.p50_latency_ms, max: 5000},
              {label: 'p95', val: metrics.stats?.p95_latency_ms, max: 10000},
              {label: 'p99', val: metrics.stats?.p99_latency_ms, max: 15000}
            ]" :key="item.label">
              <div class="flex-gap-sm mb-sm">
                <span class="text-xs text-muted" style="width:24px" x-text="item.label"></span>
                <div class="latency-bar">
                  <div class="latency-fill" :style="'width:' + Math.min(100, (item.val ?? 0)/item.max*100) + '%; background:' +
                    ((item.val ?? 0) > item.max*0.8 ? 'var(--color-danger)' : (item.val ?? 0) > item.max*0.5 ? 'var(--color-warning)' : 'var(--color-success)')"></div>
                </div>
                <span class="text-mono" style="width:60px; text-align:right" x-text="(item.val ?? '-') + 'ms'"></span>
              </div>
            </template>
          </div>

          <!-- Backend Health & Auth -->
          <div class="card">
            <div class="flex-between mb-md">
              <h3 style="margin:0">Backend Health</h3>
              <button class="btn btn-sm btn-ghost" @click="loadBackends()" aria-label="Refresh backends">Refresh</button>
            </div>
            <template x-for="b in backendsDetail" :key="b.name">
              <div style="border:1px solid var(--border); border-radius:6px; padding:0.75rem; margin-bottom:0.5rem">
                <div class="flex-between mb-sm">
                  <div class="flex-gap-sm">
                    <strong x-text="b.name"></strong>
                    <span :class="b.healthy ? 'badge badge-ok' : 'badge badge-err'"
                      role="status" x-text="b.healthy ? 'healthy' : 'unhealthy'"></span>
                  </div>
                  <span :class="b.auth?.valid ? 'badge badge-ok' : 'badge badge-err'"
                    x-text="'Auth: ' + (b.auth?.valid ? 'valid' : 'invalid')"></span>
                </div>
                <div class="text-sm text-muted">
                  <span x-show="b.auth?.method" x-text="'Method: ' + b.auth?.method" style="margin-right:1rem"></span>
                  <span x-show="b.auth?.env_vars?.length" x-text="'Env: ' + (b.auth?.env_vars?.join(', ') || '')"></span>
                </div>
                <div x-show="b.auth?.errors?.length" style="margin-top:0.25rem">
                  <template x-for="err in (b.auth?.errors ?? [])">
                    <div class="text-xs text-danger" x-text="err"></div>
                  </template>
                </div>
                <div x-show="b.health_error" class="text-xs text-danger" style="margin-top:0.25rem" x-text="b.health_error"></div>
                <div x-show="b.models?.length" class="flex-wrap-gap" style="margin-top:0.5rem; gap:0.25rem">
                  <template x-for="m in (b.models ?? [])">
                    <span class="badge" style="background:var(--subtle-bg); font-size:0.7rem" x-text="m"></span>
                  </template>
                </div>
              </div>
            </template>
            <div x-show="backendsDetail.length === 0" class="text-muted" style="text-align:center; padding:1rem">
              No backends detected
            </div>
          </div>
        </div>

        <!-- Zone 3: MCP + Models (2-column) -->
        <div class="grid-2">
          <!-- MCP Servers -->
          <div class="card">
            <div class="flex-between mb-md">
              <h3 style="margin:0">MCP Servers</h3>
              <button class="btn btn-sm btn-ghost" @click="loadMcpServers()" aria-label="Refresh MCP servers">Refresh</button>
            </div>
            <template x-for="s in mcpServers" :key="s.name">
              <div style="border:1px solid var(--border); border-radius:6px; padding:0.75rem; margin-bottom:0.5rem">
                <div class="flex-gap-sm mb-sm">
                  <strong x-text="s.name"></strong>
                  <span class="badge" style="background:var(--subtle-bg); font-size:0.7rem" x-text="s.type"></span>
                </div>
                <div x-show="s.tools?.length" class="flex-wrap-gap" style="gap:0.25rem">
                  <template x-for="t in (s.tools ?? [])">
                    <span class="text-mono text-muted text-xs" x-text="t"></span>
                  </template>
                </div>
              </div>
            </template>
            <div x-show="mcpServers.length === 0" class="text-muted" style="text-align:center; padding:0.5rem">
              No MCP servers configured
            </div>
          </div>

          <!-- Models table -->
          <div class="card">
            <h3>Models</h3>
            <div class="table-wrapper">
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
        </div>
      </div>

      <!-- Logs Tab -->
      <div x-show="tab==='logs'" role="tabpanel">
        <div class="grid-3 mb-lg">
          <div class="card stat">
            <div class="value" x-text="logs.stats?.total_requests ?? '-'"></div>
            <div class="label">Total Requests</div>
          </div>
          <div class="card stat">
            <div class="value" :class="(logs.stats?.error_count ?? 0) > 0 ? 'text-danger' : ''" x-text="logs.stats?.error_count ?? '-'"></div>
            <div class="label">Errors</div>
          </div>
          <div class="card stat">
            <div class="value" x-text="logs.stats?.avg_latency_ms ? (logs.stats.avg_latency_ms + 'ms') : '-'"></div>
            <div class="label">Avg Latency</div>
          </div>
        </div>
        <div class="card">
          <div class="flex-between mb-md">
            <h3 style="margin:0">Request Log</h3>
            <div class="flex-gap-sm" style="flex-wrap:wrap">
              <input type="text" x-model="logsFilter.endpoint" placeholder="Filter endpoint..."
                style="padding:4px 8px; font-size:0.8rem; width:160px" @input.debounce.300ms="loadLogs()">
              <select x-model="logsFilter.status" @change="loadLogs()"
                style="padding:4px 8px; font-size:0.8rem; width:100px">
                <option value="">All Status</option>
                <option value="200">200</option>
                <option value="4xx">4xx</option>
                <option value="5xx">5xx</option>
              </select>
              <label class="text-sm flex-gap-sm" style="gap:4px">
                <input type="checkbox" x-model="logsAutoRefresh" @change="toggleLogsPolling()"> Auto
              </label>
              <button class="btn btn-sm btn-ghost" @click="loadLogs()" aria-label="Refresh logs">Refresh</button>
            </div>
          </div>
          <!-- Loading skeleton -->
          <template x-if="loading.logs">
            <div>
              <template x-for="i in 5" :key="i"><div class="skeleton skeleton-row"></div></template>
            </div>
          </template>
          <template x-if="!loading.logs">
            <div>
              <div class="table-wrapper">
                <table>
                  <thead><tr><th>Time</th><th>Method</th><th>Path</th><th>Status</th><th>Latency</th><th>IP</th><th>Model</th></tr></thead>
                  <tbody>
                    <template x-for="(e, idx) in (logs.items ?? [])" :key="e.timestamp + e.path + idx">
                      <tr @click="expandedLog = expandedLog === idx ? null : idx" style="cursor:pointer">
                        <td class="text-xs" style="white-space:nowrap" x-text="formatTime(new Date(e.timestamp * 1000).toISOString())"></td>
                        <td><span class="badge badge-info" x-text="e.method"></span></td>
                        <td class="text-mono" style="max-width:250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap" x-text="e.path"></td>
                        <td><span :class="e.status_code < 400 ? 'badge badge-ok' : e.status_code < 500 ? 'badge badge-warn' : 'badge badge-err'" x-text="e.status_code"></span></td>
                        <td class="text-sm" x-text="e.response_time_ms + 'ms'"></td>
                        <td class="text-mono" x-text="e.client_ip"></td>
                        <td class="text-sm" x-text="e.model || '-'"></td>
                      </tr>
                    </template>
                    <template x-for="(e, idx) in (logs.items ?? [])" :key="'detail-' + idx">
                      <tr x-show="expandedLog === idx" style="background:var(--subtle-bg)">
                        <td colspan="7" class="text-xs" style="padding:8px 12px">
                          <div class="flex-wrap-gap" style="gap:1rem">
                            <span><strong>Backend:</strong> <span x-text="e.backend || '-'"></span></span>
                            <span><strong>Session:</strong> <span class="text-mono" x-text="e.session_id ? e.session_id.substring(0,16) + '...' : '-'"></span></span>
                            <span><strong>Bucket:</strong> <span x-text="e.bucket || '-'"></span></span>
                            <span><strong>Latency:</strong> <span x-text="e.response_time_ms + 'ms'"></span></span>
                          </div>
                        </td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div x-show="(logs.items ?? []).length === 0" class="text-muted" style="padding:1rem; text-align:center">No logs yet</div>
              <div x-show="logs.total > 50" style="display:flex; justify-content:center; gap:0.5rem; margin-top:0.75rem">
                <button class="btn btn-sm btn-ghost" @click="logsPage = Math.max(0, logsPage-1); loadLogs()" :disabled="logsPage === 0">Prev</button>
                <span class="text-sm text-muted" style="padding:4px 8px" x-text="'Page ' + (logsPage+1)"></span>
                <button class="btn btn-sm btn-ghost" @click="logsPage++; loadLogs()" :disabled="(logsPage+1)*50 >= logs.total">Next</button>
              </div>
              <div class="text-xs text-muted" style="margin-top:0.5rem; text-align:right">Latency = handler creation time only (streaming completion not included)</div>
            </div>
          </template>
        </div>
      </div>

      <!-- Rate Limits Tab -->
      <div x-show="tab==='ratelimits'" role="tabpanel">
        <div class="card mb-md">
          <div class="flex-between">
            <p class="text-sm text-muted" style="margin:0">
              Approximate monitoring based on request logs. Actual enforcement by slowapi.
              Rate limits require server restart to change.
            </p>
            <button class="btn btn-sm btn-ghost" @click="loadRateLimits()" aria-label="Refresh rate limits">Refresh</button>
          </div>
          <div x-show="config.rate_limits" class="flex-wrap-gap" style="margin-top:0.5rem">
            <template x-for="(v, k) in (config.rate_limits ?? {})" :key="k">
              <span class="text-xs" style="padding:2px 8px; border-radius:4px; background:var(--subtle-bg)">
                <strong x-text="k"></strong>: <span x-text="v + '/min'"></span>
              </span>
            </template>
          </div>
        </div>
        <div class="grid-2">
          <template x-for="(data, bucket) in (rateLimits.snapshot ?? {})" :key="bucket">
            <div class="card">
              <h3 x-text="bucket" style="text-transform:uppercase"></h3>
              <div class="flex-between mb-sm">
                <span class="text-sm" x-text="data.total_usage + ' / ' + data.limit + ' req/min'"></span>
                <span :class="(data.total_usage / data.limit * 100) > 90 ? 'badge badge-err' :
                  (data.total_usage / data.limit * 100) > 70 ? 'badge badge-warn' : 'badge badge-ok'"
                  x-text="Math.round(data.total_usage / data.limit * 100) + '%'"></span>
              </div>
              <div style="background:var(--subtle-bg); border-radius:4px; height:8px; overflow:hidden; margin-bottom:0.75rem">
                <div class="rate-bar-fill" :style="'width:' + Math.min(100, data.total_usage / data.limit * 100) + '%; height:100%; border-radius:4px; background:' +
                  ((data.total_usage / data.limit * 100) > 90 ? 'var(--color-danger)' : (data.total_usage / data.limit * 100) > 70 ? 'var(--color-warning)' : 'var(--color-success)')"></div>
              </div>
              <template x-if="data.clients && data.clients.length > 0">
                <div class="table-wrapper">
                  <table>
                    <thead><tr><th>IP</th><th>Count</th><th>Usage</th></tr></thead>
                    <tbody>
                      <template x-for="c in data.clients" :key="c.ip">
                        <tr>
                          <td class="text-mono" x-text="c.ip"></td>
                          <td x-text="c.count"></td>
                          <td><span :class="c.pct_used > 90 ? 'badge badge-err' : c.pct_used > 70 ? 'badge badge-warn' : 'badge badge-ok'"
                            x-text="c.pct_used + '%'"></span></td>
                        </tr>
                      </template>
                    </tbody>
                  </table>
                </div>
              </template>
              <div x-show="!data.clients || data.clients.length === 0" class="text-sm text-muted">No traffic</div>
            </div>
          </template>
        </div>
      </div>

      <!-- Workspace Tab -->
      <div x-show="tab==='files'" role="tabpanel">
        <div class="sidebar">
          <div class="file-tree card">
            <h3>Files</h3>
            <template x-for="f in files" :key="f.path">
              <div class="file-item" :class="{ active: editor.path === f.path }" @click="openFile(f.path)">
                <span class="icon" :class="getFileIconClass(f.path)" x-text="getFileIcon(f.path)"></span>
                <span x-text="f.path.split('/').pop()"></span>
              </div>
            </template>
            <div x-show="files.length === 0" class="text-sm text-muted" style="padding:8px 12px">
              No files found
            </div>
          </div>
          <div class="editor-area card">
            <template x-if="!editor.path">
              <div class="text-muted" style="padding:2rem; text-align:center">
                Select a file to edit
              </div>
            </template>
            <template x-if="editor.path">
              <div>
                <div class="editor-toolbar">
                  <div>
                    <span class="text-xs text-muted" x-text="editor.path.split('/').slice(0,-1).join('/') + '/'"></span>
                    <span class="text-sm" style="font-family:monospace" x-text="editor.path.split('/').pop()"></span>
                  </div>
                  <div class="flex-gap-sm">
                    <span x-show="editor.dirty" class="dirty-dot" title="Unsaved changes"></span>
                    <span x-show="editor.dirty" class="text-xs text-muted">Ctrl+S</span>
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
      <div x-show="tab==='skills'" role="tabpanel">
        <div class="sidebar">
          <div class="file-tree card">
            <div class="flex-between mb-sm">
              <h3 style="margin:0">Skills</h3>
              <button class="btn btn-sm btn-primary" @click="showNewSkillForm()">+ New</button>
            </div>
            <template x-for="s in skills" :key="s.name">
              <div class="file-item" :class="{ active: selectedSkill === s.name }" @click="openSkill(s.name)">
                <span class="icon text-success">&#9881;</span>
                <div style="flex:1; min-width:0">
                  <div style="font-size:0.85rem; font-weight:600" x-text="s.name"></div>
                  <div class="text-xs text-muted" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis"
                    x-text="s.description || '(no description)'"></div>
                </div>
              </div>
            </template>
            <div x-show="skills.length === 0" class="text-sm text-muted" style="padding:8px 12px">
              No skills found
            </div>
          </div>
          <div class="editor-area card">
            <!-- New skill form -->
            <template x-if="skillCreating">
              <div style="padding:1rem">
                <h3 style="margin-top:0">Create New Skill</h3>
                <label class="text-sm text-muted">Skill Name</label>
                <input type="text" x-model="newSkillName" placeholder="my-skill-name"
                  style="width:100%; margin-bottom:0.5rem" @input="validateNewSkillName()">
                <p x-show="newSkillNameError" class="text-sm text-danger" style="margin:0 0 0.5rem 0" x-text="newSkillNameError"></p>
                <div class="flex-gap-sm">
                  <button class="btn btn-sm btn-primary" @click="createSkill()" :disabled="!newSkillName || newSkillNameError">Create</button>
                  <button class="btn btn-sm btn-ghost" @click="skillCreating = false">Cancel</button>
                </div>
              </div>
            </template>
            <!-- No skill selected -->
            <template x-if="!selectedSkill && !skillCreating">
              <div class="text-muted" style="padding:2rem; text-align:center">
                Select a skill to edit or create a new one
              </div>
            </template>
            <!-- Skill editor -->
            <template x-if="selectedSkill && !skillCreating">
              <div>
                <div class="editor-toolbar">
                  <div class="flex-gap-sm">
                    <span class="path" x-text="selectedSkill"></span>
                    <span class="text-xs text-muted" x-text="(skillMeta.metadata?.version) ? 'v' + skillMeta.metadata.version : ''"></span>
                  </div>
                  <div class="flex-gap-sm">
                    <span x-show="skillDirty" class="dirty-dot" title="Unsaved changes"></span>
                    <span x-show="skillDirty" class="text-xs text-muted">Ctrl+S</span>
                    <button class="btn btn-sm btn-primary" @click="saveSkill()" :disabled="!skillDirty">Save</button>
                  </div>
                </div>
                <textarea x-ref="skillEditorArea" style="display:none"></textarea>
                <!-- Delete separated from Save -->
                <div style="margin-top:0.75rem; text-align:right">
                  <button class="btn btn-sm btn-danger-ghost" @click="confirmDeleteSkill()">Delete skill</button>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- Sessions Tab -->
      <div x-show="tab==='sessions'" role="tabpanel">
        <div class="card">
          <h3>Active Sessions</h3>
          <!-- Loading skeleton -->
          <template x-if="loading.sessions">
            <div>
              <template x-for="i in 3" :key="i"><div class="skeleton skeleton-row"></div></template>
            </div>
          </template>
          <template x-if="!loading.sessions">
            <div>
              <div class="table-wrapper">
                <table>
                  <thead><tr><th>Session ID</th><th>Backend</th><th>Msgs</th><th>Turns</th><th>Last Active</th><th></th></tr></thead>
                  <tbody>
                    <template x-for="s in (summary.sessions?.sessions ?? [])" :key="s.session_id">
                      <tr @click="toggleSessionHistory(s.session_id)" style="cursor:pointer"
                        :style="expandedSession === s.session_id ? 'background:var(--subtle-bg)' : ''">
                        <td class="text-mono">
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
                    </template>
                  </tbody>
                </table>
              </div>
              <div x-show="(summary.sessions?.sessions ?? []).length === 0" class="text-muted" style="padding:1rem; text-align:center">No active sessions</div>
            </div>
          </template>
        </div>

        <!-- Session Detail Panel (separated from table) -->
        <template x-if="expandedSession && sessionMessages">
          <div class="card" style="margin-top:0">
            <!-- Session metadata -->
            <div class="flex-between mb-md">
              <div class="flex-gap-sm">
                <h3 style="margin:0">Session Detail</h3>
                <span class="text-mono text-xs" x-text="expandedSession?.substring(0,16) + '...'"></span>
              </div>
              <button class="btn btn-sm btn-ghost" @click="expandedSession = null; sessionMessages = null; sessionDetail = null">Close</button>
            </div>
            <div x-show="sessionDetail" class="flex-wrap-gap text-xs text-muted mb-md" style="gap:0.75rem">
              <span x-text="'Backend: ' + (sessionDetail?.backend || '-')"></span>
              <span x-text="'Turns: ' + (sessionDetail?.turn_counter ?? '-')"></span>
              <span x-text="'TTL: ' + (sessionDetail?.ttl_minutes ?? '-') + 'min'"></span>
              <span x-show="sessionDetail?.provider_session_id" x-text="'Provider: ' + (sessionDetail?.provider_session_id?.substring(0,16) || '') + '...'"></span>
              <span x-text="'Created: ' + formatTime(sessionDetail?.created_at)"></span>
            </div>
            <div class="flex-between mb-md">
              <span class="text-sm text-muted">
                Message History (<span x-text="sessionMessages.total"></span> messages)
              </span>
              <span class="badge badge-warn text-xs">Content may contain sensitive data</span>
            </div>
            <!-- Message bubbles -->
            <template x-for="m in (sessionMessages.messages ?? [])" :key="m.index">
              <div :class="'msg-bubble msg-' + m.role">
                <div class="msg-role"
                  :class="m.role === 'user' ? 'text-info' : m.role === 'assistant' ? 'text-success' : 'text-warning'"
                  x-text="m.role.toUpperCase() + (m.name ? ' (' + m.name + ')' : '')"></div>
                <div x-text="m.content || '(empty)'"></div>
                <span x-show="m.truncated" class="text-xs text-muted" style="cursor:pointer"
                  @click.stop="loadFullMessage(expandedSession, m.index)">[...truncated - click to expand]</span>
              </div>
            </template>
            <div x-show="!sessionMessages.messages || sessionMessages.messages.length === 0"
              class="text-sm text-muted" style="text-align:center">No messages</div>
          </div>
        </template>
      </div>

      <!-- Config Tab -->
      <div x-show="tab==='config'" role="tabpanel">

        <!-- Primary: Runtime Settings (hot-reload) — always visible -->
        <div class="card mb-lg">
          <div class="flex-between mb-md">
            <h3 style="margin:0">Runtime Settings <span class="text-xs text-success">(hot-reload)</span></h3>
            <div class="flex-gap-sm">
              <button class="btn btn-sm btn-ghost" @click="resetAllRuntimeConfig()">Reset All</button>
              <button class="btn btn-sm btn-ghost" @click="loadRuntimeConfig()">Refresh</button>
            </div>
          </div>
          <p class="text-sm text-muted mb-md">
            Changes take effect on the next request. No server restart needed.
          </p>
          <div class="table-wrapper">
            <table>
              <thead><tr><th>Setting</th><th>Value</th><th>Original</th><th></th></tr></thead>
              <tbody>
                <template x-for="(meta, key) in (runtimeConfig ?? {})" :key="key">
                  <tr>
                    <td>
                      <div class="config-key" x-text="meta.label"></div>
                      <div class="text-xs text-muted" x-text="meta.description"></div>
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
                    <td class="text-sm text-muted" x-text="meta.original"></td>
                    <td>
                      <span x-show="meta.overridden" class="badge badge-warn" style="cursor:pointer; font-size:0.7rem"
                        @click="resetRuntimeConfig(key)">reset</span>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Primary: System Prompt Editor — always visible -->
        <div class="card mb-lg">
          <div class="flex-between mb-md">
            <h3 style="margin:0">System Prompt
              <span x-show="systemPrompt.mode === 'preset'" class="badge text-xs">preset</span>
              <span x-show="systemPrompt.mode === 'file'" class="badge badge-warn text-xs">file default</span>
              <span x-show="systemPrompt.mode === 'custom'" class="badge badge-ok text-xs">custom override</span>
            </h3>
            <div class="flex-gap-sm">
              <span class="text-xs text-muted" x-text="systemPrompt.char_count + ' chars'"></span>
              <button class="btn btn-sm btn-ghost" @click="resetSystemPrompt()"
                x-show="systemPrompt.mode === 'custom'">Reset</button>
              <button class="btn btn-sm btn-ghost" @click="loadSystemPrompt()">Refresh</button>
            </div>
          </div>
          <p class="text-sm text-muted mb-md">
            Custom system prompt replaces the <code>claude_code</code> preset. Changes only affect <strong>new sessions</strong>.
          </p>
          <div class="flex-gap-sm mb-md">
            <label class="text-sm text-muted">Load template:</label>
            <select @change="if($event.target.value) { applyPromptTemplate($event.target.value); $event.target.value=''; }"
              style="font-size:0.8rem; padding:4px 8px; background:var(--card-bg); color:var(--text); border:1px solid var(--border); border-radius:4px">
              <option value="">Select a template...</option>
              <template x-for="t in promptTemplates" :key="t.name">
                <option :value="t.name" x-text="t.name"></option>
              </template>
            </select>
          </div>
          <!-- Preset view -->
          <template x-if="systemPrompt.mode === 'preset' && !systemPromptEditing && systemPrompt.preset_text">
            <div>
              <textarea readonly :value="systemPrompt.preset_text"
                style="width:100%; min-height:200px; max-height:500px; font-family:monospace; font-size:0.8rem;
                  background:var(--card-bg); color:var(--text-muted); border:1px solid var(--border); border-radius:4px;
                  padding:8px; resize:vertical; opacity:0.7; cursor:default"></textarea>
              <div class="flex-gap-sm" style="margin-top:0.5rem">
                <button class="btn btn-sm" @click="systemPromptText = systemPrompt.preset_text; systemPromptEditing = true">Customize</button>
                <span class="text-xs text-muted" x-text="systemPrompt.preset_text.length + ' chars (read-only preset)'"></span>
              </div>
            </div>
          </template>
          <!-- Edit view -->
          <template x-if="systemPrompt.mode !== 'preset' || systemPromptEditing || !systemPrompt.preset_text">
            <div>
              <textarea x-model="systemPromptText"
                style="width:100%; min-height:200px; max-height:500px; font-family:monospace; font-size:0.8rem;
                  background:var(--card-bg); color:var(--text); border:1px solid var(--border); border-radius:4px;
                  padding:8px; resize:vertical"
                placeholder="Leave empty to use claude_code preset..."></textarea>
              <div class="flex-gap-sm" style="margin-top:0.5rem">
                <button class="btn btn-sm" @click="saveSystemPrompt()" :disabled="!systemPromptText.trim()">Save</button>
                <button class="btn btn-sm btn-ghost" @click="systemPromptEditing = false; systemPromptText = ''"
                  x-show="systemPromptEditing && systemPrompt.mode === 'preset'">Cancel</button>
                <span class="text-xs text-muted" x-show="systemPromptText.trim()" x-text="systemPromptText.trim().length + ' chars'"></span>
              </div>
            </div>
          </template>
        </div>

        <!-- Accordion: System Information -->
        <details class="config-section">
          <summary>System Information <span class="text-xs text-muted">Runtime, Rate Limits, Environment</span></summary>
          <div class="config-body">
            <div class="grid-2 mb-lg">
              <div>
                <h3>Runtime</h3>
                <table>
                  <tbody>
                    <template x-for="(v, k) in (config.runtime ?? {})" :key="k">
                      <tr><td class="config-key" x-text="k"></td><td x-text="v"></td></tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div>
                <h3>Rate Limits <span class="text-xs text-muted">(req/min)</span></h3>
                <table>
                  <tbody>
                    <template x-for="(v, k) in (config.rate_limits ?? {})" :key="k">
                      <tr><td class="config-key" x-text="k"></td><td x-text="v"></td></tr>
                    </template>
                  </tbody>
                </table>
              </div>
            </div>
            <div>
              <h3>Environment</h3>
              <div class="table-wrapper">
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
              </div>
              <p class="text-sm text-muted" style="margin-top:0.5rem" x-text="config._note || ''"></p>
            </div>
          </div>
        </details>

        <!-- Accordion: Security & Integrations -->
        <details class="config-section">
          <summary>Security & Integrations <span class="badge badge-warn text-xs">sensitive</span></summary>
          <div class="config-body">
            <!-- MCP Servers -->
            <template x-if="config.mcp_servers">
              <div class="mb-lg">
                <h3>MCP Servers</h3>
                <div class="flex-wrap-gap">
                  <template x-for="s in config.mcp_servers" :key="s">
                    <span class="badge badge-ok" x-text="s"></span>
                  </template>
                </div>
              </div>
            </template>

            <!-- Sandbox & Permissions -->
            <div class="mb-lg">
              <div class="flex-between mb-sm">
                <h3 style="margin:0">Sandbox & Permissions</h3>
                <button class="btn btn-sm btn-ghost" @click="loadSandbox()">Refresh</button>
              </div>
              <div class="flex-wrap-gap" style="gap:1rem; font-size:0.85rem">
                <div>
                  <span class="text-muted">Permission Mode:</span>
                  <span class="badge" :class="sandboxConfig.permission_mode === 'bypassPermissions' ? 'badge-warn' : 'badge-ok'"
                    x-text="sandboxConfig.permission_mode || 'default'"></span>
                </div>
                <div>
                  <span class="text-muted">Sandbox:</span>
                  <span class="badge" :class="sandboxConfig.sandbox_enabled === 'true' ? 'badge-ok' : 'badge-warn'"
                    x-text="sandboxConfig.sandbox_enabled === 'true' ? 'enabled' : 'disabled'"></span>
                </div>
              </div>
              <div x-show="(sandboxConfig.metadata_env_allowlist ?? []).length > 0" style="margin-top:0.5rem">
                <div class="text-sm text-muted mb-sm">Metadata Env Allowlist:</div>
                <div class="flex-wrap-gap" style="gap:0.25rem">
                  <template x-for="v in (sandboxConfig.metadata_env_allowlist ?? [])">
                    <span class="badge text-mono text-xs" style="background:var(--subtle-bg)" x-text="v"></span>
                  </template>
                </div>
              </div>
            </div>

            <!-- Tools Registry -->
            <div>
              <div class="flex-between mb-sm">
                <h3 style="margin:0">Tools Registry</h3>
                <button class="btn btn-sm btn-ghost" @click="loadTools()">Refresh</button>
              </div>
              <template x-for="(info, backend) in (toolsRegistry.backends ?? {})" :key="backend">
                <div class="mb-md">
                  <div style="font-weight:600; margin-bottom:0.25rem; text-transform:capitalize" x-text="backend + ' Tools'"></div>
                  <div class="flex-wrap-gap" style="gap:0.25rem">
                    <template x-for="t in (info.all_tools ?? [])">
                      <span :class="(info.default_allowed ?? []).includes(t) ? 'badge badge-ok' : 'badge'"
                        :style="(info.default_allowed ?? []).includes(t) ? '' : 'background:var(--subtle-bg); opacity:0.6'"
                        style="font-size:0.7rem" x-text="t"></span>
                    </template>
                  </div>
                  <div class="text-xs text-muted" style="margin-top:0.25rem">Green = default allowed</div>
                </div>
              </template>
              <div x-show="(toolsRegistry.mcp_tools ?? []).length > 0" style="margin-top:0.5rem">
                <div style="font-weight:600; margin-bottom:0.25rem">MCP Tool Patterns</div>
                <div class="flex-wrap-gap" style="gap:0.25rem">
                  <template x-for="t in (toolsRegistry.mcp_tools ?? [])">
                    <span class="badge text-mono text-xs" style="background:var(--subtle-bg)" x-text="t"></span>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </details>

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
    toasts: [],
    pollTimer: null,
    logs: {},
    logsFilter: { endpoint: '', status: '' },
    logsPage: 0,
    logsAutoRefresh: false,
    expandedLog: null,
    logsPollTimer: null,
    rateLimits: {},
    metrics: {},
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
    toolsRegistry: {},
    sandboxConfig: {},
    systemPrompt: { mode: 'preset', prompt: null, resolved_prompt: null, preset_text: null, char_count: 0 },
    systemPromptText: '',
    systemPromptEditing: false,
    promptTemplates: [],
    loading: { dashboard: false, logs: false, sessions: false },

    async init() {
      // Keyboard shortcuts (Ctrl+S / Cmd+S)
      document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
          e.preventDefault();
          if (this.tab === 'files' && this.editor.dirty) this.saveFile();
          if (this.tab === 'skills' && this.skillDirty) this.saveSkill();
        }
      });
      // Check if already authenticated (cookie-based)
      try {
        this.loading.dashboard = true;
        const r = await this.api('/admin/api/summary');
        if (r.ok) { this.authenticated = true; this.summary = await r.json(); this.loadBackends(); this.loadMcpServers(); this.loadMetrics(); this.startPolling(); }
      } catch(e) {} finally { this.loading.dashboard = false; }
    },

    // File icon helpers
    getFileIcon(path) {
      if (path.endsWith('.json')) return '{...}';
      if (path.endsWith('.md')) return '#';
      if (path.endsWith('.yaml') || path.endsWith('.yml')) return '~';
      if (path.endsWith('.toml')) return '*';
      return '~';
    },
    getFileIconClass(path) {
      if (path.endsWith('.json')) return 'file-icon-json';
      if (path.endsWith('.md')) return 'file-icon-md';
      if (path.endsWith('.yaml') || path.endsWith('.yml')) return 'file-icon-yaml';
      return 'file-icon-default';
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
          this.loadMetrics();
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
      this.loading.sessions = true;
      try {
        const r = await this.api('/admin/api/summary');
        if (r.ok) this.summary = await r.json();
        else if (r.status === 401) { this.authenticated = false; this.stopPolling(); }
      } catch(e) {} finally { this.loading.sessions = false; }
    },

    async loadMetrics() {
      try {
        const r = await this.api('/admin/api/metrics');
        if (r.ok) this.metrics = await r.json();
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
      await Promise.all([this.loadSummary(), this.loadFiles(), this.loadConfig(), this.loadBackends(), this.loadMcpServers(), this.loadMetrics()]);
      this.showToast('Refreshed', 'ok');
    },

    // --- Logs ---
    async loadLogs() {
      this.loading.logs = true;
      try {
        let url = '/admin/api/logs?limit=50&offset=' + (this.logsPage * 50);
        if (this.logsFilter.endpoint) url += '&endpoint=' + encodeURIComponent(this.logsFilter.endpoint);
        if (this.logsFilter.status) url += '&status=' + this.logsFilter.status;
        const r = await this.api(url);
        if (r.ok) this.logs = await r.json();
      } catch(e) {} finally { this.loading.logs = false; }
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

    // --- Sandbox ---
    async loadSandbox() {
      try {
        const r = await this.api('/admin/api/sandbox');
        if (r.ok) this.sandboxConfig = await r.json();
      } catch(e) {}
    },

    // --- Tools ---
    async loadTools() {
      try {
        const r = await this.api('/admin/api/tools');
        if (r.ok) this.toolsRegistry = await r.json();
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
      try {
        const r = await this.api('/admin/api/system-prompt/templates');
        if (r.ok) { this.promptTemplates = (await r.json()).templates || []; }
      } catch(e) {}
    },
    applyPromptTemplate(name) {
      const t = this.promptTemplates.find(x => x.name === name);
      if (!t) return;
      this.systemPromptText = t.content;
      this.systemPromptEditing = true;
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
      const id = Date.now() + Math.random();
      this.toasts.push({ id, msg, type });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 3000);
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
