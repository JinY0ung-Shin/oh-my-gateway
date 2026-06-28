"""Admin dashboard CSS styles.

Layers admin-specific components on top of the shared design system
(`src.theme.theme_tokens_css()` + `base_css()`), which are emitted before
this block in `admin_page.py`. The shared layer provides the design tokens,
reset, base elements, and shared components (`.card`, `.btn`, `.badge`,
inputs, tables, `.toast`, the theme toggle, scrollbars, focus ring). This
module only adds/overrides what is specific to the admin SPA.

UI text inherits `var(--font)` (sans) from the shared base; code, endpoint
paths, and tabular/identifier data use `var(--font-mono)`.
"""


def get_admin_css() -> str:
    """Return the admin dashboard css styles."""
    return """/* ================================================================
   Oh My Gateway Admin — surface-specific components
   (design tokens + base + shared components come from src/theme.py)
   ================================================================ */

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
.text-mono { font-family: var(--font-mono); font-size: var(--fs-xs); }
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
  letter-spacing: 0.06em;
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
}

.header-bar .status-line {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  min-height: 32px;
  padding: 0 0.65rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  background: var(--bg);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  white-space: nowrap;
}

.header-bar .status-line .online {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-pill);
  background: var(--green);
}

/* Admin cards add a bottom margin on top of the shared .card base. */
.card { margin-bottom: var(--gap-lg); }

.card h3 {
  margin-top: 0;
  color: var(--text-bright);
  font-size: var(--fs-sm);
  font-weight: 700;
  line-height: 1.35;
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
  font-family: var(--font-mono);
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
  letter-spacing: 0.04em;
}

nav.tabs {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  gap: var(--gap-xs);
  margin: 0 calc(var(--gap-xl) * -1) var(--gap-lg);
  padding: 0 var(--gap-xl);
  border-bottom: 1px solid var(--border);
  background: var(--bg-deep);
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
}
nav.tabs button[aria-selected="true"] {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
nav.tabs button:hover,
.tab-link:hover {
  color: var(--text-bright);
  background: var(--bg-hover);
}
.tab-link { color: var(--accent); }

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
  border-radius: var(--radius-sm);
  color: var(--text);
  cursor: pointer;
  font-size: var(--fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-item:hover { background: var(--bg-hover); }
.file-item.active {
  background: var(--accent-subtle);
  border-left-color: var(--accent);
  color: var(--accent);
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
  font-family: var(--font-mono);
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
  border-radius: var(--radius);
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

/* Admin-only button variant; base .btn/.btn-primary/.btn-ghost/.btn-sm
   come from the shared component layer. */
.btn-danger-ghost {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--gap-xs);
  min-height: 34px;
  padding: 6px 13px;
  border: 1px solid var(--red-muted);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--red-dim);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--fs-sm);
  font-weight: 600;
  white-space: nowrap;
  transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease;
}
.btn-danger-ghost:hover { background: var(--red-subtle); }
.btn-danger-ghost:disabled { opacity: 0.5; cursor: not-allowed; }

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
}
.login-box .prompt-prefix {
  margin-bottom: var(--gap-lg);
  color: var(--text-dim);
  font-size: var(--fs-sm);
}

.toast-container {
  position: fixed;
  right: var(--gap-xl);
  bottom: var(--gap-xl);
  display: flex;
  flex-direction: column-reverse;
  gap: var(--gap-sm);
  z-index: 10000;
}

.skeleton {
  border-radius: var(--radius-sm);
  background: linear-gradient(90deg, var(--bg-surface) 25%, var(--bg-hover) 50%, var(--bg-surface) 75%);
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
  border-radius: var(--radius-pill);
  background: var(--amber);
}

.config-key {
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
}
.redacted { color: var(--text-muted); font-style: normal; }

details.config-section {
  border: 1px solid var(--border);
  border-radius: var(--radius);
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
  border-radius: var(--radius-pill);
  background: var(--accent-subtle);
  color: var(--accent);
  font-family: var(--font-mono);
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
  border-radius: var(--radius-pill);
  margin: 0 var(--gap-sm);
  overflow: hidden;
}
.latency-fill {
  height: 100%;
  border-radius: var(--radius-pill);
  transition: width 0.25s ease;
}

.msg-bubble {
  margin-bottom: var(--gap-sm);
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: var(--radius);
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
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.msg-thinking {
  margin: 4px 0 8px;
  padding: 6px 8px;
  border-left: 3px solid var(--amber);
  border-radius: var(--radius-sm);
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
  border-radius: var(--radius);
  background: var(--bg-surface);
  color: var(--text);
  cursor: default;
  font-family: var(--font-mono);
  font-size: 0.78rem;
  resize: vertical;
}

.rate-bar-fill { transition: width 0.25s ease, background-color 0.25s ease; }

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
