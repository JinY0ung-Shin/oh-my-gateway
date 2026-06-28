"""Admin dashboard CSS styles."""


def get_admin_css() -> str:
    """Return the admin dashboard css styles."""
    return """/* ================================================================
   Oh My Gateway Admin
   ================================================================ */

:root {
  --green: #15803d;
  --green-dim: #166534;
  --green-muted: #bbf7d0;
  --green-subtle: #ecfdf3;
  --green-glow: none;
  --amber: #b45309;
  --amber-dim: #92400e;
  --amber-subtle: #fffbeb;
  --cyan: #2563eb;
  --cyan-dim: #1d4ed8;
  --cyan-subtle: #eff6ff;
  --red: #b91c1c;
  --red-dim: #991b1b;
  --red-subtle: #fef2f2;
  --magenta: #7c3aed;

  --bg-deep: #f4f6f8;
  --bg: #ffffff;
  --bg-raised: #ffffff;
  --bg-surface: #f8fafc;
  --bg-hover: #f1f5f9;
  --border: #e2e8f0;
  --border-dim: #edf2f7;
  --border-bright: #cbd5e1;

  --text: #243244;
  --text-bright: #0f172a;
  --text-dim: #64748b;
  --text-muted: #94a3b8;

  --accent: var(--cyan);
  --accent-hover: var(--cyan-dim);
  --color-success: var(--green);
  --color-success-subtle: var(--green-subtle);
  --color-warning: var(--amber);
  --color-warning-subtle: var(--amber-subtle);
  --color-danger: var(--red);
  --color-danger-subtle: var(--red-subtle);
  --color-info: var(--cyan);
  --color-info-subtle: var(--cyan-subtle);

  --gap-xs: 0.25rem;
  --gap-sm: 0.5rem;
  --gap-md: 0.75rem;
  --gap-lg: 1rem;
  --gap-xl: 1.5rem;

  --font: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'SF Mono', monospace;
  --fs-xs: 0.72rem;
  --fs-sm: 0.8rem;
  --fs-base: 0.9rem;
  --fs-lg: 1rem;
  --fs-xl: 1.25rem;
  --fs-2xl: 1.6rem;
  --fs-display: 2rem;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html { color-scheme: light; }

body {
  min-height: 100vh;
  background: var(--bg-deep);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-base);
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}

.container {
  width: min(1440px, 100%);
  margin: 0 auto;
  padding: var(--gap-xl);
}

.flex-between {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: var(--gap-md);
}
.flex-gap-sm { display: flex; gap: var(--gap-sm); align-items: center; }
.flex-wrap-gap { display: flex; flex-wrap: wrap; gap: var(--gap-sm); }
.text-mono { font-family: var(--font); font-size: var(--fs-xs); }
.text-xs { font-size: var(--fs-xs); }
.text-sm { font-size: var(--fs-sm); }
.text-muted { color: var(--text-muted); }
.text-dim { color: var(--text-dim); }
.text-danger { color: var(--red); }
.text-success { color: var(--green); }
.text-warning { color: var(--amber); }
.text-info { color: var(--cyan); }
.mb-sm { margin-bottom: var(--gap-sm); }
.mb-md { margin-bottom: var(--gap-md); }
.mb-lg { margin-bottom: var(--gap-lg); }

.product-mark {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 700;
  line-height: 1.2;
  text-transform: uppercase;
  letter-spacing: 0;
}

.header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--gap-lg);
  margin-bottom: var(--gap-lg);
  padding: 0 0 var(--gap-lg);
  border-bottom: 1px solid var(--border);
}

.header-bar h1 {
  margin-top: 0.2rem;
  color: var(--text-bright);
  font-size: var(--fs-2xl);
  font-weight: 700;
  line-height: 1.15;
  letter-spacing: 0;
}

.header-bar .status-line {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  min-height: 32px;
  padding: 0 0.65rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  white-space: nowrap;
}

.header-bar .status-line .online {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--green);
}

.card {
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: var(--gap-lg);
  margin-bottom: var(--gap-lg);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.card h3 {
  margin-top: 0;
  color: var(--text-bright);
  font-size: var(--fs-sm);
  font-weight: 700;
  line-height: 1.35;
  text-transform: none;
  letter-spacing: 0;
}

.grid-2 {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--gap-lg);
}
.grid-3 {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--gap-lg);
}
.grid-4 {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--gap-lg);
}
.grid-2 > *,
.grid-3 > *,
.grid-4 > * {
  min-width: 0;
}
.metric-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.metric-strip .stat {
  min-height: 72px;
}
.metric-strip .stat .label {
  font-size: 0.65rem;
}

.stat {
  min-height: 112px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: var(--gap-md);
}
.stat .value {
  color: var(--text-bright);
  font-size: var(--fs-display);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  line-height: 1.05;
  overflow-wrap: anywhere;
}
.stat .label {
  margin-top: var(--gap-sm);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0;
}

.badge {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  padding: 1px 8px;
  border: 1px solid var(--border-bright);
  border-radius: 999px;
  background: var(--bg-surface);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-xs);
  font-weight: 600;
  letter-spacing: 0;
  white-space: nowrap;
}
.badge-ok { background: var(--green-subtle); color: var(--green-dim); border-color: var(--green-muted); }
.badge-warn { background: var(--amber-subtle); color: var(--amber-dim); border-color: #fde68a; }
.badge-err { background: var(--red-subtle); color: var(--red-dim); border-color: #fecaca; }
.badge-info { background: var(--cyan-subtle); color: var(--cyan-dim); border-color: #bfdbfe; }

nav.tabs {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  gap: var(--gap-xs);
  margin: 0 calc(var(--gap-xl) * -1) var(--gap-lg);
  padding: 0 var(--gap-xl);
  border-bottom: 1px solid var(--border);
  background: rgba(244, 246, 248, 0.95);
  overflow-x: auto;
  scrollbar-width: thin;
}
nav.tabs button,
.tab-link {
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  padding: 0 var(--gap-md);
  border: 0;
  border-bottom: 3px solid transparent;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--fs-sm);
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
  letter-spacing: 0;
}
nav.tabs button[aria-selected="true"] {
  color: var(--cyan-dim);
  border-bottom-color: var(--cyan);
}
nav.tabs button:hover,
.tab-link:hover {
  color: var(--text-bright);
  background: rgba(255, 255, 255, 0.65);
}
.tab-link { color: var(--cyan-dim); }

.sidebar { display: flex; gap: var(--gap-lg); }
.sidebar .file-tree { width: clamp(300px, 26vw, 360px); flex-shrink: 0; }
.sidebar .editor-area { flex: 1; min-width: 0; }
.file-item {
  display: flex;
  align-items: center;
  gap: var(--gap-xs);
  min-height: 32px;
  padding: 5px 10px;
  border-left: 3px solid transparent;
  border-radius: 6px;
  color: var(--text);
  cursor: pointer;
  font-size: var(--fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-item:hover { background: var(--bg-hover); }
.file-item.active {
  background: var(--cyan-subtle);
  border-left-color: var(--cyan);
  color: var(--cyan-dim);
}
.file-item .icon { font-size: var(--fs-sm); }
.marketplace-row {
  align-items: flex-start;
  flex-wrap: wrap;
  white-space: normal;
}
.marketplace-main {
  flex: 1 1 180px;
}
.marketplace-count {
  margin-left: auto;
  white-space: nowrap;
}

.editor-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--gap-md);
  margin-bottom: var(--gap-sm);
}
.editor-toolbar .path {
  color: var(--text-dim);
  font-family: var(--font);
  font-size: var(--fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.prompt-status-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--gap-sm) var(--gap-lg);
  margin-bottom: var(--gap-sm);
  padding: 8px 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-raised);
}
.prompt-status-main,
.prompt-status-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--gap-xs) var(--gap-sm);
  min-width: 0;
}
.prompt-status-label {
  white-space: nowrap;
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--gap-xs);
  min-height: 34px;
  padding: 6px 13px;
  border: 1px solid var(--border-bright);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text-bright);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--fs-sm);
  font-weight: 600;
  letter-spacing: 0;
  text-transform: none;
  transition: background-color 0.15s, border-color 0.15s, color 0.15s;
  white-space: nowrap;
}
.btn:hover { background: var(--bg-hover); border-color: var(--text-dim); }
.btn-primary {
  background: var(--cyan);
  color: #ffffff;
  border-color: var(--cyan);
}
.btn-primary:hover {
  background: var(--cyan-dim);
  color: #ffffff;
  border-color: var(--cyan-dim);
}
.btn-sm { min-height: 30px; padding: 4px 10px; font-size: var(--fs-xs); }
.btn-ghost {
  background: transparent;
  border-color: var(--border);
  color: var(--text);
}
.btn-ghost:hover {
  background: var(--bg-hover);
  border-color: var(--border-bright);
  color: var(--text-bright);
}
.btn-danger-ghost {
  background: transparent;
  border-color: #fecaca;
  color: var(--red-dim);
}
.btn-danger-ghost:hover { background: var(--red-subtle); }
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.login-wrap {
  min-height: calc(100vh - 3rem);
  display: grid;
  place-items: center;
}
.login-box {
  width: min(460px, 100%);
  margin: 0 auto;
}
.login-box h1 {
  margin: 0.35rem 0 0.5rem;
  color: var(--text-bright);
  font-size: var(--fs-2xl);
  line-height: 1.2;
  letter-spacing: 0;
}
.login-box .prompt-prefix {
  margin-bottom: var(--gap-lg);
  color: var(--text-dim);
  font-size: var(--fs-sm);
}

input[type="text"],
input[type="number"],
input[type="password"],
input[type="date"],
select,
textarea {
  min-height: 34px;
  background: var(--bg);
  border: 1px solid var(--border-bright);
  border-radius: 6px;
  color: var(--text-bright);
  font-family: var(--font);
  font-size: var(--fs-sm);
  padding: 6px 10px;
  outline: none;
  caret-color: var(--cyan);
}
input[type="text"]:focus,
input[type="number"]:focus,
input[type="password"]:focus,
input[type="date"]:focus,
select:focus,
textarea:focus {
  border-color: var(--cyan);
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
}
input::placeholder,
textarea::placeholder { color: var(--text-muted); }
select {
  cursor: pointer;
  -webkit-appearance: none;
  appearance: none;
  padding-right: 28px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%2364748b'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
}
input[type="checkbox"] { accent-color: var(--cyan); }

.table-wrapper {
  width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}
table th {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-bright);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 700;
  text-align: left;
  text-transform: uppercase;
  letter-spacing: 0;
  white-space: nowrap;
}
table td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
table tr:hover td { background: var(--bg-hover); }

.toast-container {
  position: fixed;
  right: var(--gap-xl);
  bottom: var(--gap-xl);
  display: flex;
  flex-direction: column-reverse;
  gap: var(--gap-sm);
  z-index: 10000;
}
.toast {
  max-width: min(420px, calc(100vw - 2rem));
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: 8px;
  background: var(--bg);
  box-shadow: 0 12px 30px rgba(15, 23, 42, 0.12);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-sm);
  animation: toast-in 0.2s ease;
}
.toast-ok { border-left-color: var(--green); }
.toast-err { border-left-color: var(--red); }
@keyframes toast-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.skeleton {
  border-radius: 6px;
  background: linear-gradient(90deg, var(--bg-surface) 25%, #e8eef6 50%, var(--bg-surface) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.4s infinite;
}
.skeleton-row { height: 20px; margin-bottom: 8px; }
.skeleton-stat { height: 3rem; width: 5rem; margin: 0 auto; }
.skeleton-card { height: 80px; }
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
.card-loading { min-height: 60px; }

.dirty-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--amber);
}

.config-key {
  color: var(--cyan-dim);
  font-family: var(--font);
  font-size: var(--fs-sm);
}
.redacted { color: var(--text-muted); font-style: normal; }
:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }

details.config-section {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-raised);
  margin-bottom: var(--gap-lg);
  overflow: hidden;
}
details.config-section > summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--gap-md);
  min-height: 46px;
  padding: var(--gap-md) var(--gap-lg);
  color: var(--text-bright);
  cursor: pointer;
  font-size: var(--fs-sm);
  font-weight: 700;
  letter-spacing: 0;
  list-style: none;
}
details.config-section > summary::-webkit-details-marker { display: none; }
details.config-section > summary::before {
  content: '+';
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 999px;
  background: var(--cyan-subtle);
  color: var(--cyan-dim);
  font-weight: 700;
}
details.config-section[open] > summary {
  border-bottom: 1px solid var(--border);
  background: var(--bg-surface);
}
details.config-section[open] > summary::before { content: '-'; }
details.config-section .config-body { padding: var(--gap-lg); }

.latency-bar {
  flex: 1;
  height: 6px;
  background: var(--bg-surface);
  border-radius: 999px;
  margin: 0 var(--gap-sm);
  overflow: hidden;
}
.latency-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.25s ease;
}

.msg-bubble {
  margin-bottom: var(--gap-sm);
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: 8px;
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.msg-user {
  background: var(--cyan-subtle);
  margin-right: auto;
  max-width: 84%;
  border-left-color: var(--cyan);
}
.msg-assistant {
  background: var(--green-subtle);
  margin-left: auto;
  max-width: 84%;
  border-left-color: var(--green);
}
.msg-system {
  background: var(--amber-subtle);
  margin: 0 auto;
  max-width: 92%;
  border-left-color: var(--amber);
}
.msg-role {
  margin-bottom: 4px;
  font-size: var(--fs-xs);
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}
.msg-thinking {
  margin: 4px 0 8px;
  padding: 6px 8px;
  border-left: 3px solid var(--amber);
  border-radius: 6px;
  background: var(--amber-subtle);
}
.msg-thinking-label {
  margin-bottom: 4px;
  color: var(--amber);
  font-size: var(--fs-xs);
  font-weight: 700;
}
.msg-thinking-text { color: var(--text-bright); }

.readonly-editor {
  width: 100%;
  min-height: 350px;
  max-height: 60vh;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-surface);
  color: var(--text);
  cursor: default;
  font-family: var(--font);
  font-size: 0.78rem;
  resize: vertical;
}

.rate-bar-fill { transition: width 0.25s ease, background-color 0.25s ease; }
.cursor-blink::after { content: ''; }
.boot-line { opacity: 1; }
.prompt::before { content: ''; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg-surface); }
::-webkit-scrollbar-thumb { background: var(--border-bright); border-radius: 999px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

@media (max-width: 1080px) {
  .grid-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-2 { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  .container { padding: var(--gap-lg); }
  .header-bar {
    align-items: flex-start;
    flex-direction: column;
  }
  .header-bar > .flex-gap-sm {
    width: 100%;
    flex-wrap: wrap;
  }
  nav.tabs {
    margin-left: calc(var(--gap-lg) * -1);
    margin-right: calc(var(--gap-lg) * -1);
    padding-left: var(--gap-lg);
    padding-right: var(--gap-lg);
  }
  .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
  .sidebar { flex-direction: column; }
  .sidebar .file-tree { width: 100%; max-height: 220px; overflow-y: auto; }
  .marketplace-row {
    gap: 2px var(--gap-sm);
  }
  .marketplace-main {
    order: 1;
    flex: 1 1 calc(100% - 28px) !important;
  }
  .marketplace-count {
    order: 2;
    margin-left: 22px;
  }
  .marketplace-remove {
    order: 3;
    margin-left: auto;
  }
  .stat { min-height: 96px; }
  .metric-strip { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .metric-strip .stat { min-height: 68px; }
  .metric-strip .stat .value { font-size: 1.15rem !important; }
  .msg-user, .msg-assistant, .msg-system { max-width: 100%; }
  .toast-container { right: var(--gap-lg); bottom: var(--gap-lg); left: var(--gap-lg); }
}
"""
