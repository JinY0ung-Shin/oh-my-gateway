"""Admin dashboard HTML generator.

Follows the same pattern as ``landing_page.py``: a single function
returns a self-contained HTML string with inline CSS/JS and CDN
dependencies (Alpine.js, CodeMirror).
"""


def build_admin_page() -> str:
    """Build the admin dashboard HTML."""
    return """<!DOCTYPE html>
<html lang="ko" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GATEWAY CTRL // Admin Terminal</title>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/lib/codemirror.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/theme/material-darker.min.css">
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/lib/codemirror.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/markdown/markdown.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/javascript/javascript.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/yaml/yaml.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ================================================================
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
}
</style>
</head>
<body>

<!-- Vignette overlay -->
<div class="vignette"></div>

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
      <div class="ascii-header" style="font-size:0.55rem; text-align:left; margin-bottom:1.5rem">  ██████╗ ██╗      █████╗ ██╗   ██╗██████╗ ███████╗
 ██╔════╝ ██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝
 ██║      ██║     ███████║██║   ██║██║  ██║█████╗
 ██║      ██║     ██╔══██║██║   ██║██║  ██║██╔══╝
 ╚██████╗ ███████╗██║  ██║╚██████╔╝██████╔╝███████╗
  ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝</div>
      <h2>Access Terminal</h2>
      <p class="prompt-prefix">ADMIN_API_KEY required for authentication</p>
      <p class="text-xs text-muted" style="margin-bottom:1.5rem">Enter credentials to proceed_<span class="cursor-blink"></span></p>
      <form @submit.prevent="doLogin()">
        <label class="text-xs text-dim" style="margin-bottom:4px; display:block">root@gateway:~# </label>
        <input type="password" x-model="loginKey" placeholder="••••••••••••••••"
          style="width:100%; margin-bottom:1rem" required>
        <button class="btn btn-primary" style="width:100%" type="submit">AUTHENTICATE</button>
      </form>
      <p x-show="loginError" class="text-danger text-sm" style="margin-top:0.75rem">
        <span style="color:var(--red)">ERR</span> <span x-text="loginError"></span>
      </p>
    </div>
  </template>

  <!-- Main UI -->
  <template x-if="authenticated">
    <div>
      <!-- ASCII Header -->
      <div class="ascii-header">╔═══════════════════════════════════════════════════════════════╗
║   ░██████╗░█████╗░████████╗███████╗░██╗░░░░░░░██╗░█████╗░██╗░░░██╗  ║
║   ██╔════╝██╔══██╗╚══██╔══╝██╔════╝░██║░░██╗░░██║██╔══██╗╚██╗░██╔╝  ║
║   ██║░░██╗███████║░░░██║░░░█████╗░░░╚██╗████╗██╔╝███████║░╚████╔╝░  ║
║   ██║░░╚██╗██╔══██║░░░██║░░░██╔══╝░░░░████╔═████║░██╔══██║░░╚██╔╝░░  ║
║   ╚██████╔╝██║░░██║░░░██║░░░███████╗░░╚██╔╝░╚██╔╝░██║░░██║░░░██║░░░  ║
║   ░╚═════╝░╚═╝░░╚═╝░░░╚═╝░░░╚══════╝░░░╚═╝░░░╚═╝░░╚═╝░░╚═╝░░░╚═╝░░░  ║
╚═══════════════════════════════════════════════════════════════╝</div>

      <!-- Header Bar -->
      <div class="header-bar">
        <div>
          <span class="text-xs text-muted">CLAUDE CODE GATEWAY</span>
          <span class="text-xs" style="color:var(--green-dim); margin-left:0.5rem">// ADMIN TERMINAL v2.0</span>
        </div>
        <div class="flex-gap-sm">
          <span class="status-line">
            <span class="online">&#9679;</span> CONNECTED
          </span>
          <button class="btn btn-sm btn-ghost" @click="refreshAll()" aria-label="Refresh all data">[REFRESH]</button>
          <button class="btn btn-sm btn-ghost" @click="doLogout()" aria-label="Log out">[LOGOUT]</button>
        </div>
      </div>

      <!-- Tabs -->
      <nav class="tabs" role="tablist" aria-label="Admin sections">
        <button role="tab" :aria-selected="tab === 'dashboard'" @click="tab='dashboard'">DASH</button>
        <button role="tab" :aria-selected="tab === 'sessions'" @click="tab='sessions'; loadSummary()">SESSIONS</button>
        <button role="tab" :aria-selected="tab === 'logs'" @click="tab='logs'; loadLogs()">LOGS</button>
        <button role="tab" :aria-selected="tab === 'ratelimits'" @click="tab='ratelimits'; loadRateLimits()">LIMITS</button>
        <button role="tab" :aria-selected="tab === 'files'" @click="tab='files'; loadFiles()">FILES</button>
        <button role="tab" :aria-selected="tab === 'skills'" @click="tab='skills'; loadSkills()">SKILLS</button>
        <button role="tab" :aria-selected="tab === 'config'" @click="tab='config'; loadConfig(); loadRuntimeConfig(); loadSystemPrompt(); loadTools(); loadSandbox()">CONFIG</button>
      </nav>

      <!-- Dashboard Tab -->
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
      </div>

      <!-- Logs Tab -->
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
      </div>

      <!-- Rate Limits Tab -->
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
      </div>

      <!-- Workspace Tab -->
      <div x-show="tab==='files'" role="tabpanel">
        <div class="sidebar">
          <div class="file-tree card">
            <h3>File System</h3>
            <template x-for="f in files" :key="f.path">
              <div class="file-item" :class="{ active: editor.path === f.path }" @click="openFile(f.path)">
                <span class="icon" :class="getFileIconClass(f.path)" x-text="getFileIcon(f.path)"></span>
                <span x-text="f.path.split('/').pop()"></span>
              </div>
            </template>
            <div x-show="files.length === 0" class="text-sm text-muted" style="padding:8px 12px">
              [ EMPTY ]
            </div>
          </div>
          <div class="editor-area card">
            <template x-if="!editor.path">
              <div class="text-muted" style="padding:3rem; text-align:center">
                <div style="font-size:2rem; margin-bottom:0.5rem; opacity:0.2">&lt;/&gt;</div>
                select a file to edit_<span class="cursor-blink"></span>
              </div>
            </template>
            <template x-if="editor.path">
              <div>
                <div class="editor-toolbar">
                  <div>
                    <span class="text-xs text-muted" x-text="editor.path.split('/').slice(0,-1).join('/') + '/'"></span>
                    <span class="text-sm" style="color:var(--text-bright)" x-text="editor.path.split('/').pop()"></span>
                  </div>
                  <div class="flex-gap-sm">
                    <span x-show="editor.dirty" class="dirty-dot" title="Unsaved changes"></span>
                    <span x-show="editor.dirty" class="text-xs text-muted">Ctrl+S</span>
                    <button class="btn btn-sm btn-primary" @click="saveFile()" :disabled="!editor.dirty">[SAVE]</button>
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
              <button class="btn btn-sm btn-primary" @click="showNewSkillForm()">[+ NEW]</button>
            </div>
            <template x-for="s in skills" :key="s.name">
              <div class="file-item" :class="{ active: selectedSkill === s.name }" @click="openSkill(s.name)">
                <span class="icon" style="color:var(--green)">&#9881;</span>
                <div style="flex:1; min-width:0">
                  <div style="font-size:var(--fs-sm); font-weight:600; color:var(--text-bright)" x-text="s.name"></div>
                  <div class="text-xs text-muted" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis"
                    x-text="s.description || '(no description)'"></div>
                </div>
              </div>
            </template>
            <div x-show="skills.length === 0" class="text-sm text-muted" style="padding:8px 12px">
              [ NO SKILLS ]
            </div>
          </div>
          <div class="editor-area card">
            <template x-if="skillCreating">
              <div style="padding:1rem">
                <h3 style="margin-top:0">Create Skill</h3>
                <label class="text-xs text-muted">SKILL_NAME:</label>
                <input type="text" x-model="newSkillName" placeholder="my-skill-name"
                  style="width:100%; margin-bottom:0.5rem; margin-top:4px" @input="validateNewSkillName()">
                <p x-show="newSkillNameError" class="text-sm text-danger" style="margin:0 0 0.5rem 0" x-text="'! ' + newSkillNameError"></p>
                <div class="flex-gap-sm">
                  <button class="btn btn-sm btn-primary" @click="createSkill()" :disabled="!newSkillName || newSkillNameError">[CREATE]</button>
                  <button class="btn btn-sm btn-ghost" @click="skillCreating = false">[CANCEL]</button>
                </div>
              </div>
            </template>
            <template x-if="!selectedSkill && !skillCreating">
              <div class="text-muted" style="padding:3rem; text-align:center">
                <div style="font-size:2rem; margin-bottom:0.5rem; opacity:0.2">&#9881;</div>
                select a skill or create new_<span class="cursor-blink"></span>
              </div>
            </template>
            <template x-if="selectedSkill && !skillCreating">
              <div>
                <div class="editor-toolbar">
                  <div class="flex-gap-sm">
                    <span class="path" style="color:var(--text-bright)" x-text="selectedSkill"></span>
                    <span class="text-xs" style="color:var(--text-dim)" x-text="(skillMeta.metadata?.version) ? 'v' + skillMeta.metadata.version : ''"></span>
                  </div>
                  <div class="flex-gap-sm">
                    <span x-show="skillDirty" class="dirty-dot" title="Unsaved changes"></span>
                    <span x-show="skillDirty" class="text-xs text-muted">Ctrl+S</span>
                    <button class="btn btn-sm btn-primary" @click="saveSkill()" :disabled="!skillDirty">[SAVE]</button>
                  </div>
                </div>
                <textarea x-ref="skillEditorArea" style="display:none"></textarea>
                <div style="margin-top:0.75rem; text-align:right">
                  <button class="btn btn-sm btn-danger-ghost" @click="confirmDeleteSkill()">[DELETE SKILL]</button>
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
      </div>

      <!-- Config Tab -->
      <div x-show="tab==='config'" role="tabpanel">

        <div class="card mb-lg">
          <div class="flex-between mb-md">
            <h3 style="margin:0">Runtime Settings <span class="text-xs" style="color:var(--green)">HOT-RELOAD</span></h3>
            <div class="flex-gap-sm">
              <button class="btn btn-sm btn-ghost" @click="resetAllRuntimeConfig()">[RESET ALL]</button>
              <button class="btn btn-sm btn-ghost" @click="loadRuntimeConfig()">[RELOAD]</button>
            </div>
          </div>
          <p class="text-xs text-muted mb-md">
            // changes take effect on next request. no restart needed.
          </p>
          <div class="table-wrapper">
            <table>
              <thead><tr><th>SETTING</th><th>VALUE</th><th>ORIGINAL</th><th></th></tr></thead>
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
                          style="padding:4px 8px; font-size:0.75rem">
                          <option value="true">true</option>
                          <option value="false">false</option>
                        </select>
                      </template>
                      <template x-if="meta.type === 'int'">
                        <input type="number" :value="meta.value" min="1"
                          @change="updateRuntimeConfig(key, parseInt($event.target.value))"
                          style="padding:4px 8px; font-size:0.75rem; width:100px">
                      </template>
                      <template x-if="meta.type === 'string'">
                        <input type="text" :value="meta.value"
                          @change="updateRuntimeConfig(key, $event.target.value)"
                          style="padding:4px 8px; font-size:0.75rem; width:160px">
                      </template>
                    </td>
                    <td class="text-sm text-muted" x-text="meta.original"></td>
                    <td>
                      <span x-show="meta.overridden" class="badge badge-warn" style="cursor:pointer; font-size:0.65rem"
                        @click="resetRuntimeConfig(key)">[RESET]</span>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Active prompt status bar -->
        <div style="border:1px solid var(--border); background:var(--bg-raised); padding:8px 16px; margin-bottom:2px; display:flex; justify-content:space-between; align-items:center">
          <div class="flex-gap-sm">
            <span class="text-xs text-muted">ACTIVE_PROMPT:</span>
            <span x-show="systemPrompt.active_name" style="color:var(--green); font-weight:600; font-size:var(--fs-sm); text-shadow: 0 0 6px var(--green-muted)" x-text="systemPrompt.active_name"></span>
            <span x-show="!systemPrompt.active_name && systemPrompt.mode !== 'custom'" style="color:var(--text-dim); font-size:var(--fs-sm)">claude_code (preset)</span>
            <span x-show="!systemPrompt.active_name && systemPrompt.mode === 'custom'" style="color:var(--amber); font-size:var(--fs-sm)">custom (unnamed)</span>
          </div>
          <div class="flex-gap-sm">
            <span class="text-xs text-muted" x-text="'MODE=' + systemPrompt.mode"></span>
            <span class="text-xs text-muted" x-text="systemPrompt.char_count + ' chars'"></span>
          </div>
        </div>

        <!-- System Prompt: Sidebar + Editor layout -->
        <div class="sidebar mb-lg">
          <!-- Prompt list sidebar -->
          <div class="file-tree card">
            <div class="flex-between mb-sm">
              <h3 style="margin:0">Prompts</h3>
              <button class="btn btn-sm btn-primary" @click="showNewPromptForm()">[+ NEW]</button>
            </div>

            <!-- Preset entry -->
            <div class="file-item" :class="{ active: promptView === 'preset' }" @click="selectPresetPrompt()"
              style="border-bottom:1px solid var(--border); margin-bottom:4px; padding-bottom:8px">
              <span class="icon" style="color:var(--text-dim)">&gt;_</span>
              <div style="flex:1; min-width:0">
                <div style="font-size:var(--fs-sm); font-weight:600" :style="systemPrompt.active_name == null && systemPrompt.mode !== 'custom' ? 'color:var(--green)' : 'color:var(--text)'">
                  claude_code <span x-show="systemPrompt.active_name == null && systemPrompt.mode !== 'custom'" class="text-xs" style="color:var(--green)">ACTIVE</span>
                </div>
                <div class="text-xs text-muted">built-in preset</div>
              </div>
            </div>

            <!-- Templates -->
            <template x-for="t in promptTemplates" :key="'tpl-' + t.name">
              <div class="file-item" :class="{ active: promptView === 'template' && promptViewName === t.name }"
                @click="selectTemplatePrompt(t)">
                <span class="icon" style="color:var(--amber)">&#9734;</span>
                <div style="flex:1; min-width:0">
                  <div style="font-size:var(--fs-sm); font-weight:600; color:var(--text)" x-text="t.name"></div>
                  <div class="text-xs text-muted">template</div>
                </div>
              </div>
            </template>

            <!-- Divider -->
            <div x-show="namedPrompts.length > 0" style="border-top:1px solid var(--border); margin:4px 0; padding-top:4px">
              <span class="text-xs text-muted" style="padding-left:12px">// SAVED</span>
            </div>

            <!-- Named prompts -->
            <template x-for="p in namedPrompts" :key="p.name">
              <div class="file-item" :class="{ active: promptView === 'named' && promptViewName === p.name }"
                @click="selectNamedPrompt(p.name)">
                <span class="icon" :style="systemPrompt.active_name === p.name ? 'color:var(--green)' : 'color:var(--cyan)'"
                  x-text="systemPrompt.active_name === p.name ? '&#9679;' : '&#9675;'"></span>
                <div style="flex:1; min-width:0">
                  <div style="font-size:var(--fs-sm); font-weight:600"
                    :style="systemPrompt.active_name === p.name ? 'color:var(--green)' : 'color:var(--text)'" x-text="p.name">
                  </div>
                  <div class="text-xs text-muted" x-text="p.char_count + ' chars'"></div>
                </div>
                <span x-show="systemPrompt.active_name === p.name" class="text-xs" style="color:var(--green)">ACTIVE</span>
              </div>
            </template>
          </div>

          <!-- Editor area -->
          <div class="editor-area card" style="flex:1">
            <!-- New prompt form -->
            <template x-if="promptView === 'new'">
              <div style="padding:1rem">
                <h3 style="margin-top:0">Create Prompt</h3>
                <label class="text-xs text-muted">PROMPT_NAME:</label>
                <input type="text" x-model="newPromptName" placeholder="my-prompt-name"
                  style="width:100%; margin-bottom:0.5rem; margin-top:4px" @input="validateNewPromptName()">
                <p x-show="newPromptNameError" class="text-sm text-danger" style="margin:0 0 0.5rem 0" x-text="'! ' + newPromptNameError"></p>
                <label class="text-xs text-muted">CONTENT:</label>
                <textarea x-model="newPromptContent"
                  style="width:100%; min-height:300px; max-height:60vh; font-family:var(--font); font-size:0.78rem;
                    background:var(--bg-surface); color:var(--text-bright); border:1px solid var(--border-bright);
                    padding:8px; resize:vertical; border-radius:0; margin-top:4px"
                  placeholder="// enter system prompt content..."></textarea>
                <div class="flex-gap-sm" style="margin-top:0.75rem">
                  <button class="btn btn-sm btn-primary" @click="createNamedPrompt()"
                    :disabled="!newPromptName.trim() || !newPromptContent.trim() || newPromptNameError">[CREATE]</button>
                  <button class="btn btn-sm btn-ghost" @click="promptView = null">[CANCEL]</button>
                  <span class="text-xs text-muted" x-show="newPromptContent.trim()" x-text="newPromptContent.trim().length + ' chars'"></span>
                </div>
              </div>
            </template>

            <!-- No selection -->
            <template x-if="!promptView">
              <div class="text-muted" style="padding:3rem; text-align:center">
                <div style="font-size:2rem; margin-bottom:0.5rem; opacity:0.2">&gt;_</div>
                select a prompt or create new_<span class="cursor-blink"></span>
              </div>
            </template>

            <!-- Preset view (read-only) -->
            <template x-if="promptView === 'preset'">
              <div>
                <div class="flex-between mb-md">
                  <div class="flex-gap-sm">
                    <span style="color:var(--text-bright); font-weight:600">claude_code</span>
                    <span class="badge text-xs" style="border-color:var(--border-bright); color:var(--text-dim)">PRESET</span>
                    <span x-show="systemPrompt.active_name == null && systemPrompt.mode !== 'custom'" class="badge badge-ok text-xs">ACTIVE</span>
                  </div>
                  <span class="text-xs text-muted" x-text="(systemPrompt.preset_text?.length ?? 0) + ' chars'"></span>
                </div>
                <p class="text-xs text-muted mb-md">// built-in claude_code preset. read-only.</p>
                <textarea readonly :value="systemPrompt.preset_text || ''"
                  style="width:100%; min-height:350px; max-height:60vh; font-family:var(--font); font-size:0.78rem;
                    background:var(--bg-surface); color:var(--text-dim); border:1px solid var(--border);
                    padding:8px; resize:vertical; cursor:default; border-radius:0"></textarea>
                <div class="flex-gap-sm" style="margin-top:0.75rem">
                  <button class="btn btn-sm btn-primary" @click="activatePreset()"
                    :disabled="systemPrompt.active_name == null && systemPrompt.mode !== 'custom'">[ACTIVATE]</button>
                  <button class="btn btn-sm btn-ghost" @click="forkFromPreset()">[FORK AS NEW]</button>
                </div>
              </div>
            </template>

            <!-- Template view (read-only, can fork) -->
            <template x-if="promptView === 'template'">
              <div>
                <div class="flex-between mb-md">
                  <div class="flex-gap-sm">
                    <span style="color:var(--text-bright); font-weight:600" x-text="promptViewName"></span>
                    <span class="badge text-xs" style="border-color:var(--amber); color:var(--amber)">TEMPLATE</span>
                  </div>
                  <span class="text-xs text-muted" x-text="(promptEditorContent?.length ?? 0) + ' chars'"></span>
                </div>
                <p class="text-xs text-muted mb-md">// template from docs/. save as named prompt to edit.</p>
                <textarea readonly :value="promptEditorContent || ''"
                  style="width:100%; min-height:350px; max-height:60vh; font-family:var(--font); font-size:0.78rem;
                    background:var(--bg-surface); color:var(--text-dim); border:1px solid var(--border);
                    padding:8px; resize:vertical; cursor:default; border-radius:0"></textarea>
                <div class="flex-gap-sm" style="margin-top:0.75rem">
                  <button class="btn btn-sm btn-ghost" @click="forkFromTemplate()">[SAVE AS NEW]</button>
                </div>
              </div>
            </template>

            <!-- Named prompt editor -->
            <template x-if="promptView === 'named'">
              <div>
                <div class="flex-between mb-md">
                  <div class="flex-gap-sm">
                    <span style="color:var(--text-bright); font-weight:600" x-text="promptViewName"></span>
                    <span x-show="systemPrompt.active_name === promptViewName" class="badge badge-ok text-xs">ACTIVE</span>
                    <span x-show="promptDirty" class="dirty-dot" title="Unsaved changes"></span>
                  </div>
                  <span class="text-xs text-muted" x-text="(promptEditorContent?.length ?? 0) + ' chars'"></span>
                </div>
                <p class="text-xs text-muted mb-md">// affects new sessions only after activation.</p>
                <textarea x-model="promptEditorContent"
                  style="width:100%; min-height:350px; max-height:60vh; font-family:var(--font); font-size:0.78rem;
                    background:var(--bg-surface); color:var(--text-bright); border:1px solid var(--border-bright);
                    padding:8px; resize:vertical; border-radius:0"
                  @input="promptDirty = true"></textarea>
                <div class="flex-between" style="margin-top:0.75rem">
                  <div class="flex-gap-sm">
                    <button class="btn btn-sm btn-primary" @click="saveNamedPrompt()" :disabled="!promptDirty || !promptEditorContent.trim()">[SAVE]</button>
                    <button class="btn btn-sm btn-primary" @click="activateNamedPrompt()"
                      :disabled="systemPrompt.active_name === promptViewName && !promptDirty">[ACTIVATE]</button>
                  </div>
                  <button class="btn btn-sm btn-danger-ghost" @click="deleteNamedPrompt()">[DELETE]</button>
                </div>
              </div>
            </template>
          </div>
        </div>

        <details class="config-section">
          <summary>System Information <span class="text-xs text-muted">runtime, rate_limits, env</span></summary>
          <div class="config-body">
            <div class="grid-2 mb-lg">
              <div>
                <h3>Runtime</h3>
                <table>
                  <tbody>
                    <template x-for="(v, k) in (config.runtime ?? {})" :key="k">
                      <tr><td class="config-key" x-text="k"></td><td style="color:var(--text-bright)" x-text="v"></td></tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div>
                <h3>Rate Limits <span class="text-xs text-muted">(req/min)</span></h3>
                <table>
                  <tbody>
                    <template x-for="(v, k) in (config.rate_limits ?? {})" :key="k">
                      <tr><td class="config-key" x-text="k"></td><td style="color:var(--amber)" x-text="v"></td></tr>
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
                        <td :class="{ redacted: v === '***REDACTED***' || v === '(not set)' }"
                          :style="(v !== '***REDACTED***' && v !== '(not set)') ? 'color:var(--text-bright)' : ''"
                          x-text="v"></td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <p class="text-xs text-muted" style="margin-top:0.5rem" x-text="config._note || ''"></p>
            </div>
          </div>
        </details>

        <details class="config-section">
          <summary>Security & Integrations <span class="badge badge-warn text-xs">SENSITIVE</span></summary>
          <div class="config-body">
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

            <div class="mb-lg">
              <div class="flex-between mb-sm">
                <h3 style="margin:0">Sandbox & Permissions</h3>
                <button class="btn btn-sm btn-ghost" @click="loadSandbox()">[RELOAD]</button>
              </div>
              <div class="flex-wrap-gap" style="gap:1rem; font-size:var(--fs-sm)">
                <div>
                  <span class="text-muted">permission_mode=</span>
                  <span :class="sandboxConfig.permission_mode === 'bypassPermissions' ? 'text-warning' : 'text-success'"
                    x-text="sandboxConfig.permission_mode || 'default'"></span>
                </div>
                <div>
                  <span class="text-muted">sandbox=</span>
                  <span :class="sandboxConfig.sandbox_enabled === 'true' ? 'text-success' : 'text-warning'"
                    x-text="sandboxConfig.sandbox_enabled === 'true' ? 'enabled' : 'disabled'"></span>
                </div>
              </div>
              <div x-show="(sandboxConfig.metadata_env_allowlist ?? []).length > 0" style="margin-top:0.5rem">
                <div class="text-xs text-muted mb-sm">env_allowlist:</div>
                <div class="flex-wrap-gap" style="gap:0.25rem">
                  <template x-for="v in (sandboxConfig.metadata_env_allowlist ?? [])">
                    <span class="text-xs" style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)" x-text="v"></span>
                  </template>
                </div>
              </div>
            </div>

            <div>
              <div class="flex-between mb-sm">
                <h3 style="margin:0">Tools Registry</h3>
                <button class="btn btn-sm btn-ghost" @click="loadTools()">[RELOAD]</button>
              </div>
              <template x-for="(info, backend) in (toolsRegistry.backends ?? {})" :key="backend">
                <div class="mb-md">
                  <div style="font-weight:600; margin-bottom:0.25rem; text-transform:uppercase; color:var(--cyan); font-size:var(--fs-sm)" x-text="backend + '_tools'"></div>
                  <div class="flex-wrap-gap" style="gap:0.25rem">
                    <template x-for="t in (info.all_tools ?? [])">
                      <span :class="(info.default_allowed ?? []).includes(t) ? 'badge badge-ok' : 'badge'"
                        :style="(info.default_allowed ?? []).includes(t) ? '' : 'background:var(--bg-surface); border-color:var(--border-bright); opacity:0.4; color:var(--text-dim)'"
                        style="font-size:0.65rem" x-text="t"></span>
                    </template>
                  </div>
                  <div class="text-xs text-muted" style="margin-top:0.25rem">// green = default_allowed</div>
                </div>
              </template>
              <div x-show="(toolsRegistry.mcp_tools ?? []).length > 0" style="margin-top:0.5rem">
                <div style="font-weight:600; margin-bottom:0.25rem; color:var(--cyan); font-size:var(--fs-sm); text-transform:uppercase">MCP_TOOL_PATTERNS</div>
                <div class="flex-wrap-gap" style="gap:0.25rem">
                  <template x-for="t in (toolsRegistry.mcp_tools ?? [])">
                    <span class="text-xs" style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)" x-text="t"></span>
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
    systemPrompt: { mode: 'preset', prompt: null, resolved_prompt: null, preset_text: null, char_count: 0, active_name: null },
    promptTemplates: [],
    namedPrompts: [],
    promptView: null,
    promptViewName: null,
    promptEditorContent: '',
    promptDirty: false,
    newPromptName: '',
    newPromptNameError: '',
    newPromptContent: '',
    loading: { dashboard: false, logs: false, sessions: false },

    async init() {
      document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
          e.preventDefault();
          if (this.tab === 'files' && this.editor.dirty) this.saveFile();
          if (this.tab === 'skills' && this.skillDirty) this.saveSkill();
          if (this.tab === 'config' && this.promptView === 'named' && this.promptDirty) this.saveNamedPrompt();
        }
      });
      try {
        this.loading.dashboard = true;
        const r = await this.api('/admin/api/summary');
        if (r.ok) { this.authenticated = true; this.summary = await r.json(); this.loadBackends(); this.loadMcpServers(); this.loadMetrics(); this.startPolling(); }
      } catch(e) {} finally { this.loading.dashboard = false; }
    },

    getFileIcon(path) {
      if (path.endsWith('.json')) return '{..}';
      if (path.endsWith('.md')) return '##';
      if (path.endsWith('.yaml') || path.endsWith('.yml')) return '~~';
      if (path.endsWith('.toml')) return '**';
      return '>_';
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
          this.loginError = d.detail || 'Authentication failed';
        }
      } catch(e) { this.loginError = 'Connection refused'; }
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
          this.showToast('FILE SAVED', 'ok');
        } else {
          this.showToast(d.error || 'Save failed', 'err');
          if (r.status === 409) {
            if (confirm('File modified externally. Reload?')) this.openFile(this.editor.path);
          }
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },

    async deleteSession(id) {
      if (!confirm('Delete session ' + id.substring(0,16) + '...?')) return;
      try {
        const r = await this.api('/admin/api/sessions/' + id, { method: 'DELETE' });
        if (r.ok) { this.showToast('SESSION DELETED', 'ok'); await this.loadSummary(); }
        else { const d = await r.json(); this.showToast(d.error || 'Delete failed', 'err'); }
      } catch(e) { this.showToast('Failed to delete', 'err'); }
    },

    async refreshAll() {
      await Promise.all([this.loadSummary(), this.loadFiles(), this.loadConfig(), this.loadBackends(), this.loadMcpServers(), this.loadMetrics()]);
      this.showToast('ALL SYSTEMS REFRESHED', 'ok');
    },

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

    async loadRateLimits() {
      try {
        const r = await this.api('/admin/api/rate-limits');
        if (r.ok) this.rateLimits = await r.json();
      } catch(e) {}
    },

    async loadSandbox() {
      try {
        const r = await this.api('/admin/api/sandbox');
        if (r.ok) this.sandboxConfig = await r.json();
      } catch(e) {}
    },

    async loadTools() {
      try {
        const r = await this.api('/admin/api/tools');
        if (r.ok) this.toolsRegistry = await r.json();
      } catch(e) {}
    },

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
        if (r.ok) { this.showToast('UPDATED: ' + key, 'ok'); await this.loadRuntimeConfig(); }
        else { const d = await r.json(); this.showToast(d.error || 'Update failed', 'err'); }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async resetRuntimeConfig(key) {
      try {
        const r = await this.api('/admin/api/runtime-config/reset?key=' + encodeURIComponent(key), { method: 'POST' });
        if (r.ok) { this.showToast('RESET: ' + key, 'ok'); await this.loadRuntimeConfig(); }
      } catch(e) {}
    },
    async resetAllRuntimeConfig() {
      if (!confirm('Reset all runtime settings to startup defaults?')) return;
      try {
        const r = await this.api('/admin/api/runtime-config/reset', { method: 'POST' });
        if (r.ok) { this.showToast('ALL SETTINGS RESET', 'ok'); await this.loadRuntimeConfig(); }
      } catch(e) {}
    },

    async loadSystemPrompt() {
      const [r1, r2, r3] = await Promise.all([
        this.api('/admin/api/system-prompt').catch(() => null),
        this.api('/admin/api/system-prompt/templates').catch(() => null),
        this.api('/admin/api/prompts').catch(() => null),
      ]);
      if (r1?.ok) { this.systemPrompt = await r1.json(); }
      if (r2?.ok) { this.promptTemplates = (await r2.json()).templates || []; }
      if (r3?.ok) { const d = await r3.json(); this.namedPrompts = d.prompts || []; }
    },
    // --- Prompt sidebar selection ---
    selectPresetPrompt() {
      if (this.promptDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      this.promptView = 'preset';
      this.promptViewName = null;
      this.promptEditorContent = this.systemPrompt.preset_text || '';
      this.promptDirty = false;
    },
    selectTemplatePrompt(t) {
      if (this.promptDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      this.promptView = 'template';
      this.promptViewName = t.name;
      this.promptEditorContent = t.content;
      this.promptDirty = false;
    },
    async selectNamedPrompt(name) {
      if (this.promptDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      try {
        const r = await this.api('/admin/api/prompts/' + encodeURIComponent(name));
        if (r.ok) {
          const d = await r.json();
          this.promptView = 'named';
          this.promptViewName = d.name;
          this.promptEditorContent = d.content;
          this.promptDirty = false;
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Failed to load', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    showNewPromptForm() {
      if (this.promptDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      this.promptView = 'new';
      this.promptViewName = null;
      this.newPromptName = '';
      this.newPromptNameError = '';
      this.newPromptContent = '';
      this.promptDirty = false;
    },

    validateNewPromptName() {
      const n = this.newPromptName.trim();
      if (!n) { this.newPromptNameError = ''; return; }
      if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(n)) {
        this.newPromptNameError = 'letters, digits, hyphens, underscores only (max 64 chars)';
        return;
      }
      if (this.namedPrompts.some(p => p.name === n)) {
        this.newPromptNameError = 'prompt already exists (will overwrite on create)';
      } else {
        this.newPromptNameError = '';
      }
    },

    // --- Named prompt CRUD ---
    async createNamedPrompt() {
      const name = this.newPromptName.trim();
      const content = this.newPromptContent.trim();
      if (!name || !content) return;
      try {
        const r = await this.api('/admin/api/prompts/' + encodeURIComponent(name), {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ content })
        });
        if (r.ok) {
          const d = await r.json();
          this.showToast('PROMPT CREATED: ' + name, 'ok');
          await this.loadSystemPrompt();
          this.promptView = 'named';
          this.promptViewName = d.name;
          this.promptEditorContent = d.content;
          this.promptDirty = false;
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Create failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async saveNamedPrompt() {
      if (!this.promptViewName || !this.promptEditorContent.trim()) return;
      try {
        const r = await this.api('/admin/api/prompts/' + encodeURIComponent(this.promptViewName), {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ content: this.promptEditorContent.trim() })
        });
        if (r.ok) {
          this.promptDirty = false;
          this.showToast('PROMPT SAVED', 'ok');
          const wasActive = this.systemPrompt.active_name === this.promptViewName;
          if (wasActive) {
            await this.api('/admin/api/prompts/' + encodeURIComponent(this.promptViewName) + '/activate', { method: 'POST' });
          }
          await this.loadSystemPrompt();
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Save failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async deleteNamedPrompt() {
      if (!this.promptViewName) return;
      if (!confirm('Delete prompt "' + this.promptViewName + '"?')) return;
      try {
        const r = await this.api('/admin/api/prompts/' + encodeURIComponent(this.promptViewName), { method: 'DELETE' });
        if (r.ok) {
          this.showToast('PROMPT DELETED', 'ok');
          this.promptView = null;
          this.promptViewName = null;
          this.promptDirty = false;
          await this.loadSystemPrompt();
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Delete failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async activateNamedPrompt() {
      if (!this.promptViewName) return;
      // Save first if dirty
      if (this.promptDirty) await this.saveNamedPrompt();
      try {
        const r = await this.api('/admin/api/prompts/' + encodeURIComponent(this.promptViewName) + '/activate', { method: 'POST' });
        if (r.ok) {
          this.showToast('ACTIVATED: ' + this.promptViewName, 'ok');
          await this.loadSystemPrompt();
        } else {
          const d = await r.json();
          this.showToast(d.error || 'Activate failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async activatePreset() {
      if (!confirm('Reset to claude_code preset?')) return;
      try {
        const r = await this.api('/admin/api/system-prompt', { method: 'DELETE' });
        if (r.ok) { this.showToast('PRESET ACTIVATED', 'ok'); await this.loadSystemPrompt(); }
      } catch(e) {}
    },
    forkFromPreset() {
      this.promptView = 'new';
      this.newPromptName = '';
      this.newPromptNameError = '';
      this.newPromptContent = this.systemPrompt.preset_text || '';
      this.promptDirty = false;
    },
    forkFromTemplate() {
      this.promptView = 'new';
      this.newPromptName = this.promptViewName ? this.promptViewName.replace(/-reference$/, '') : '';
      this.newPromptNameError = '';
      this.newPromptContent = this.promptEditorContent || '';
      this.promptDirty = false;
    },

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
          this.showToast('SKILL SAVED', 'ok');
          await this.loadSkills();
        } else {
          this.showToast(d.error || 'Save failed', 'err');
          if (r.status === 409) {
            if (confirm('Skill modified externally. Reload?')) this.openSkill(this.selectedSkill);
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
        this.newSkillNameError = 'lowercase, digits, hyphens only (start with letter/digit)';
        return;
      }
      if (this.skills.some(s => s.name === n)) {
        this.newSkillNameError = 'skill already exists';
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
          this.showToast('SKILL CREATED: ' + name, 'ok');
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
          this.showToast('SKILL DELETED', 'ok');
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
        this.showToast('SESSION EXPORTED', 'ok');
      } catch(e) { this.showToast('Export failed', 'err'); }
    },
    async loadFullMessage(sessionId, msgIndex) {
      try {
        const r = await this.api('/admin/api/sessions/' + encodeURIComponent(sessionId) + '/messages?truncate=0');
        if (!r.ok) return;
        const data = await r.json();
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
    }
  };
}
</script>
</body>
</html>"""
