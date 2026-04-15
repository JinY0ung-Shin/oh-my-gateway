"""Admin dashboard CSS styles."""


def get_admin_css() -> str:
    """Return the admin dashboard css styles."""
    return """/* ================================================================
   TERMINAL / HACKER DESIGN SYSTEM
   Claude Code Gateway — Admin Terminal v2.0
   ================================================================ */

:root {
  /* --- Phosphor palette --- */
  --green: #00ff41;
  --green-dim: #00cc33;
  --green-muted: #00802080;
  --green-subtle: #00ff4112;
  --green-glow: 0 0 10px #00ff4140, 0 0 40px #00ff4110;
  --amber: #ffb000;
  --amber-dim: #cc8800;
  --amber-subtle: #ffb00015;
  --cyan: #00e5ff;
  --cyan-dim: #00b8cc;
  --cyan-subtle: #00e5ff12;
  --red: #ff0033;
  --red-dim: #cc0029;
  --red-subtle: #ff003315;
  --magenta: #ff00ff;

  /* --- Surface --- */
  --bg-deep: #050505;
  --bg: #0a0a0a;
  --bg-raised: #111111;
  --bg-surface: #161616;
  --bg-hover: #1a1a1a;
  --border: #1e1e1e;
  --border-bright: #2a2a2a;

  /* --- Text --- */
  --text: #b0ffb0;
  --text-bright: #00ff41;
  --text-dim: #4a7a4a;
  --text-muted: #3a5a3a;

  /* --- Semantic (for backward compat) --- */
  --accent: var(--green);
  --accent-hover: var(--green-dim);
  --color-success: var(--green);
  --color-success-subtle: var(--green-subtle);
  --color-warning: var(--amber);
  --color-warning-subtle: var(--amber-subtle);
  --color-danger: var(--red);
  --color-danger-subtle: var(--red-subtle);
  --color-info: var(--cyan);
  --color-info-subtle: var(--cyan-subtle);

  /* --- Spacing --- */
  --gap-xs: 0.25rem;
  --gap-sm: 0.5rem;
  --gap-md: 0.75rem;
  --gap-lg: 1rem;
  --gap-xl: 1.5rem;

  /* --- Typography --- */
  --font: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'SF Mono', monospace;
  --fs-xs: 0.7rem;
  --fs-sm: 0.78rem;
  --fs-base: 0.85rem;
  --fs-lg: 1rem;
  --fs-xl: 1.2rem;
  --fs-2xl: 1.6rem;
  --fs-display: 2.4rem;
}

/* === Reset === */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

/* === Base === */
body {
  background: var(--bg-deep);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-base);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}

/* === CRT Scanline Overlay === */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent,
    transparent 2px,
    rgba(0, 0, 0, 0.08) 2px,
    rgba(0, 0, 0, 0.08) 4px
  );
  pointer-events: none;
  z-index: 9999;
}

/* === Grid Background === */
body::after {
  content: '';
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(var(--green-muted) 1px, transparent 1px),
    linear-gradient(90deg, var(--green-muted) 1px, transparent 1px);
  background-size: 60px 60px;
  opacity: 0.04;
  pointer-events: none;
  z-index: -1;
}

/* === Container === */
.container { max-width: 1280px; margin: 0 auto; padding: var(--gap-lg); }

/* === Utility === */
.flex-between { display: flex; justify-content: space-between; align-items: center; }
.flex-gap-sm { display: flex; gap: var(--gap-sm); align-items: center; }
.flex-wrap-gap { display: flex; flex-wrap: wrap; gap: var(--gap-sm); }
.text-mono { font-family: var(--font); font-size: var(--fs-xs); }
.text-xs { font-size: var(--fs-xs); }
.text-sm { font-size: var(--fs-sm); }
.text-muted { color: var(--text-dim); }
.text-danger { color: var(--red); }
.text-success { color: var(--green); }
.text-warning { color: var(--amber); }
.text-info { color: var(--cyan); }
.mb-sm { margin-bottom: var(--gap-sm); }
.mb-md { margin-bottom: var(--gap-md); }
.mb-lg { margin-bottom: var(--gap-lg); }

/* === ASCII Header === */
.ascii-header {
  font-size: var(--fs-xs);
  color: var(--green-dim);
  text-align: center;
  line-height: 1.2;
  letter-spacing: 0.05em;
  margin-bottom: var(--gap-sm);
  text-shadow: 0 0 8px var(--green-muted);
  white-space: pre;
  overflow: hidden;
}
.header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--gap-sm) 0;
  border-bottom: 1px solid var(--border-bright);
  margin-bottom: var(--gap-lg);
}
.header-bar .status-line {
  font-size: var(--fs-xs);
  color: var(--text-dim);
}
.header-bar .status-line .online {
  color: var(--green);
  text-shadow: 0 0 6px var(--green-muted);
  animation: pulse-glow 2s ease-in-out infinite;
}

@keyframes pulse-glow {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

/* === Cards (Terminal Panels) === */
.card {
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: var(--gap-lg);
  margin-bottom: var(--gap-lg);
  position: relative;
}
.card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--green-dim), transparent);
  opacity: 0.4;
}
.card h3 {
  margin-top: 0;
  font-size: var(--fs-sm);
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  font-weight: 500;
}
.card h3::before {
  content: '// ';
  color: var(--text-muted);
}

/* === Grids === */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-lg); }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: var(--gap-lg); }
.grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap-lg); }

/* === Stats (Terminal Output Style) === */
.stat { text-align: center; padding: var(--gap-sm); }
.stat .value {
  font-size: var(--fs-display);
  font-weight: 700;
  color: var(--green);
  text-shadow: var(--green-glow);
  font-variant-numeric: tabular-nums;
}
.stat .label {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.15em;
  margin-top: var(--gap-xs);
}

/* === Badges === */
.badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 0;
  font-size: var(--fs-xs);
  font-weight: 500;
  font-family: var(--font);
  letter-spacing: 0.05em;
  border: 1px solid;
}
.badge-ok {
  background: var(--green-subtle);
  color: var(--green);
  border-color: var(--green-dim);
  text-shadow: 0 0 4px var(--green-muted);
}
.badge-warn {
  background: var(--amber-subtle);
  color: var(--amber);
  border-color: var(--amber-dim);
}
.badge-err {
  background: var(--red-subtle);
  color: var(--red);
  border-color: var(--red-dim);
}
.badge-info {
  background: var(--cyan-subtle);
  color: var(--cyan);
  border-color: var(--cyan-dim);
}

/* === Tabs (Command Selector) === */
nav.tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border-bright);
  margin-bottom: var(--gap-lg);
  overflow-x: auto;
  scrollbar-width: none;
}
nav.tabs::-webkit-scrollbar { display: none; }
nav.tabs button {
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  padding: var(--gap-sm) var(--gap-lg);
  color: var(--text-dim);
  cursor: pointer;
  font-size: var(--fs-sm);
  font-family: var(--font);
  white-space: nowrap;
  flex-shrink: 0;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: -1px;
  transition: color 0.15s, border-color 0.15s;
}
nav.tabs button::before {
  content: '> ';
  opacity: 0;
  transition: opacity 0.15s;
}
nav.tabs button[aria-selected="true"] {
  color: var(--green);
  border-bottom-color: var(--green);
  text-shadow: 0 0 8px var(--green-muted);
}
nav.tabs button[aria-selected="true"]::before {
  opacity: 1;
}
nav.tabs button:hover {
  color: var(--text);
}

/* === Sidebar / Editor === */
.sidebar { display: flex; gap: var(--gap-lg); }
.sidebar .file-tree { width: 260px; flex-shrink: 0; }
.sidebar .editor-area { flex: 1; min-width: 0; }
.file-item {
  padding: 5px 12px;
  cursor: pointer;
  border-radius: 0;
  font-size: var(--fs-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  border-left: 2px solid transparent;
  transition: all 0.1s;
}
.file-item:hover {
  background: var(--bg-hover);
  border-left-color: var(--text-dim);
}
.file-item.active {
  background: var(--green-subtle);
  border-left-color: var(--green);
  color: var(--green);
}
.file-item .icon { margin-right: 6px; font-size: var(--fs-sm); }
.file-icon-json { color: var(--amber); }
.file-icon-md { color: var(--cyan); }
.file-icon-yaml { color: var(--magenta); }
.file-icon-default { color: var(--text-dim); }

.CodeMirror {
  height: auto;
  min-height: 300px;
  max-height: 70vh;
  border: 1px solid var(--border);
  border-radius: 0;
  font-size: 13px;
  font-family: var(--font) !important;
  overflow: hidden;
  background: var(--bg) !important;
}
.CodeMirror-scroll { max-height: 70vh; overflow-y: auto !important; }
.CodeMirror-gutters { background: var(--bg-raised) !important; border-right: 1px solid var(--border) !important; }
.CodeMirror-linenumber { color: var(--text-muted) !important; }
.CodeMirror-cursor { border-left-color: var(--green) !important; }
.CodeMirror-selected { background: var(--green-subtle) !important; }
.CodeMirror-focused .CodeMirror-selected { background: var(--green-subtle) !important; }

.editor-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--gap-sm);
}
.editor-toolbar .path {
  font-family: var(--font);
  font-size: var(--fs-sm);
  color: var(--text-dim);
}

/* === Buttons === */
.btn {
  display: inline-flex;
  align-items: center;
  gap: var(--gap-xs);
  padding: 5px 14px;
  border-radius: 0;
  border: 1px solid var(--border-bright);
  cursor: pointer;
  font-size: var(--fs-sm);
  font-weight: 500;
  font-family: var(--font);
  background: var(--bg-raised);
  color: var(--text);
  transition: all 0.15s;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.btn:hover {
  border-color: var(--green-dim);
  color: var(--green);
  text-shadow: 0 0 4px var(--green-muted);
}
.btn-primary {
  background: var(--green-subtle);
  color: var(--green);
  border-color: var(--green-dim);
}
.btn-primary:hover {
  background: var(--green);
  color: var(--bg);
  text-shadow: none;
}
.btn-sm { padding: 3px 10px; font-size: var(--fs-xs); }
.btn-ghost {
  background: transparent;
  border-color: var(--border);
  color: var(--text-dim);
}
.btn-ghost:hover {
  border-color: var(--green-dim);
  color: var(--green);
}
.btn-danger-ghost {
  background: transparent;
  border-color: var(--red-dim);
  color: var(--red);
}
.btn-danger-ghost:hover {
  background: var(--red-subtle);
}
.btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
  border-color: var(--border);
  color: var(--text-muted);
}
.btn:disabled:hover {
  text-shadow: none;
  color: var(--text-muted);
}

/* === Login === */
.login-box {
  max-width: 480px;
  margin: 6rem auto;
  border: 1px solid var(--green-dim);
  box-shadow: var(--green-glow);
}
.login-box h2 {
  font-size: var(--fs-xl);
  color: var(--green);
  text-shadow: 0 0 10px var(--green-muted);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  margin-top: 0;
}
.login-box .prompt-prefix {
  color: var(--green-dim);
  font-size: var(--fs-sm);
}
.login-box input[type="password"] {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border-bright);
  color: var(--green);
  font-family: var(--font);
  font-size: var(--fs-base);
  padding: 10px 14px;
  margin-bottom: var(--gap-lg);
  border-radius: 0;
  outline: none;
  caret-color: var(--green);
}
.login-box input[type="password"]:focus {
  border-color: var(--green-dim);
  box-shadow: 0 0 8px var(--green-muted);
}
.login-box input[type="password"]::placeholder {
  color: var(--text-muted);
}

/* === Tables === */
.table-wrapper { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; font-size: var(--fs-sm); border-collapse: collapse; }
table th {
  color: var(--text-dim);
  font-weight: 500;
  text-align: left;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-size: var(--fs-xs);
  border-bottom: 1px solid var(--border-bright);
}
table td, table th { padding: 8px 12px; }
table td { border-bottom: 1px solid var(--border); }
table tr:hover td { background: var(--bg-hover); }

/* === Toast === */
.toast-container {
  position: fixed;
  bottom: var(--gap-xl);
  right: var(--gap-xl);
  display: flex;
  flex-direction: column-reverse;
  gap: var(--gap-sm);
  z-index: 10000;
}
.toast {
  padding: 10px 20px;
  border-radius: 0;
  font-size: var(--fs-sm);
  font-family: var(--font);
  animation: toast-in 0.3s ease;
  border-left: 3px solid;
  background: var(--bg-surface);
}
.toast::before {
  content: '> ';
  opacity: 0.5;
}
.toast-ok {
  color: var(--green);
  border-left-color: var(--green);
  text-shadow: 0 0 4px var(--green-muted);
}
.toast-err {
  color: var(--red);
  border-left-color: var(--red);
}
@keyframes toast-in {
  from { opacity: 0; transform: translateX(20px); }
  to { opacity: 1; transform: translateX(0); }
}

/* === Loading / Skeleton === */
.skeleton {
  border-radius: 0;
  background: linear-gradient(90deg, var(--bg-surface) 25%, var(--bg-raised) 50%, var(--bg-surface) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
}
.skeleton-row { height: 20px; margin-bottom: 8px; }
.skeleton-stat { height: 3rem; width: 5rem; margin: 0 auto; }
.skeleton-card { height: 80px; }
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
.card-loading { min-height: 60px; }

/* === Misc === */
.dirty-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 0;
  background: var(--amber);
  box-shadow: 0 0 6px var(--amber);
  animation: blink-dot 1s ease-in-out infinite;
}
@keyframes blink-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.config-key {
  font-family: var(--font);
  font-size: var(--fs-sm);
  color: var(--cyan);
}
.redacted { color: var(--text-muted); font-style: normal; }
.redacted::before { content: '['; }
.redacted::after { content: ']'; }
:focus-visible { outline: 1px solid var(--green); outline-offset: 2px; }

/* === Accordion === */
details.config-section {
  border: 1px solid var(--border);
  border-radius: 0;
  background: var(--bg-raised);
  margin-bottom: var(--gap-lg);
}
details.config-section > summary {
  cursor: pointer;
  padding: var(--gap-md) var(--gap-lg);
  list-style: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 500;
  font-size: var(--fs-sm);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-dim);
}
details.config-section > summary::-webkit-details-marker { display: none; }
details.config-section > summary::before {
  content: '[+]';
  margin-right: var(--gap-sm);
  color: var(--green-dim);
  font-weight: 700;
  transition: none;
}
details.config-section[open] > summary::before {
  content: '[-]';
  color: var(--amber);
}
details.config-section[open] > summary { border-bottom: 1px solid var(--border); }
details.config-section .config-body { padding: var(--gap-lg); }

/* === Latency Bar === */
.latency-bar {
  flex: 1;
  height: 4px;
  background: var(--bg-surface);
  border-radius: 0;
  margin: 0 var(--gap-sm);
  overflow: hidden;
}
.latency-fill {
  height: 100%;
  border-radius: 0;
  transition: width 0.4s ease;
  box-shadow: 0 0 4px currentColor;
}

/* === Session Messages === */
.msg-bubble {
  margin-bottom: var(--gap-sm);
  padding: 8px 12px;
  border-radius: 0;
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-word;
  border-left: 2px solid;
}
.msg-user {
  background: var(--bg-surface);
  margin-right: auto;
  max-width: 80%;
  border-left-color: var(--cyan);
}
.msg-assistant {
  background: var(--green-subtle);
  margin-left: auto;
  max-width: 80%;
  border-left-color: var(--green);
}
.msg-system {
  background: var(--amber-subtle);
  margin: 0 auto;
  max-width: 90%;
  text-align: center;
  border-left-color: var(--amber);
}
.msg-role {
  font-size: var(--fs-xs);
  font-weight: 600;
  margin-bottom: 4px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

/* === Read-only Editor === */
.readonly-editor {
  width: 100%;
  min-height: 350px;
  max-height: 60vh;
  font-family: var(--font);
  font-size: 0.78rem;
  background: var(--bg-surface);
  color: var(--text-dim);
  border: 1px solid var(--border);
  padding: 8px;
  resize: vertical;
  cursor: default;
  border-radius: 0;
}

/* === Rate Bar === */
.rate-bar-fill { transition: width 0.4s ease, background-color 0.4s ease; }

/* === Blinking Cursor === */
.cursor-blink::after {
  content: '_';
  animation: blink 1s step-end infinite;
  color: var(--green);
}
@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

/* === Boot Sequence === */
.boot-line {
  opacity: 0;
  animation: boot-appear 0.1s forwards;
}
@keyframes boot-appear {
  to { opacity: 1; }
}

/* === Prompt prefix for inline data === */
.prompt::before {
  content: '$ ';
  color: var(--green-dim);
  opacity: 0.5;
}

/* === Form inputs === */
input[type="text"], input[type="number"], input[type="password"], select, textarea {
  background: var(--bg);
  border: 1px solid var(--border-bright);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-sm);
  padding: 6px 10px;
  border-radius: 0;
  outline: none;
  caret-color: var(--green);
}
input[type="text"]:focus, input[type="number"]:focus, select:focus, textarea:focus {
  border-color: var(--green-dim);
  box-shadow: 0 0 6px var(--green-muted);
}
input::placeholder, textarea::placeholder {
  color: var(--text-muted);
}
select {
  cursor: pointer;
  -webkit-appearance: none;
  appearance: none;
  padding-right: 24px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%234a7a4a'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 8px center;
}
input[type="checkbox"] {
  accent-color: var(--green);
}

/* === Scrollbar === */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border-bright); }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* === Responsive === */
@media (max-width: 768px) {
  .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; }
  .sidebar { flex-direction: column; }
  .sidebar .file-tree { width: 100%; max-height: 200px; overflow-y: auto; }
  nav.tabs button { padding: var(--gap-sm) var(--gap-md); }
  .CodeMirror { min-height: 200px; max-height: 50vh; }
  .ascii-header { font-size: 0.45rem; }
}

/* === Vignette effect === */
.vignette {
  position: fixed;
  inset: 0;
  background: radial-gradient(ellipse at center, transparent 60%, rgba(0,0,0,0.5) 100%);
  pointer-events: none;
  z-index: 9998;
}"""
