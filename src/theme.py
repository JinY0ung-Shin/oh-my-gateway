"""Shared design system for all server-rendered surfaces.

One source of truth for the "modern developer console" look (Linear/Vercel
flavored): neutral zinc/slate surfaces, a single restrained indigo accent, and
the HTTP-method colors (GET/POST/PUT/DELETE) as the structural color system.

Three surfaces consume this module: the API-reference landing page (`/`), the
chat UI, and the admin SPA. Every function below is pure and returns a plain
string (single-brace CSS / HTML / JS) with no side effects, so callers can
embed the output however they template.

Theming model
-------------
- `data-theme` lives on <html> with values "light" | "dark" | "system".
- Light tokens are defined on `:root`; dark tokens override under
  `html[data-theme="dark"]`. "system" follows `prefers-color-scheme` only when
  the user has not explicitly chosen light/dark.
- `theme_head_init()` applies the saved choice before first paint (no FOUC).
- The toggle is VANILLA JS (works on plain pages AND alongside Alpine).

IMPORTANT for the landing page: `src/landing_page.py` builds its HTML with
`str.format()`, so any CSS/JS injected there must have its braces doubled
(`{` -> `{{`, `}` -> `}}`). The strings returned here are canonical
SINGLE-brace; the landing integrator is responsible for escaping them.
"""

__all__ = [
    "theme_head_init",
    "theme_tokens_css",
    "base_css",
    "theme_toggle_html",
    "theme_toggle_js",
]

# localStorage key shared by the head init script and the toggle controller.
_STORAGE_KEY = "omg-theme"


def theme_head_init() -> str:
    """Return a tiny synchronous <script> for <head> that prevents FOUC.

    Reads localStorage["omg-theme"] (light|dark|system, default "system") and
    sets <html data-theme="..."> before first paint. For "system" it sets
    data-theme="system" and lets the media query in the token CSS resolve it.
    Wrapped in try/catch so a blocked localStorage never breaks rendering.
    """
    return (
        "<script>(function(){try{"
        "var t=localStorage.getItem('" + _STORAGE_KEY + "');"
        "if(t!=='light'&&t!=='dark'&&t!=='system'){t='system';}"
        "document.documentElement.setAttribute('data-theme',t);"
        "}catch(e){document.documentElement.setAttribute('data-theme','system');}"
        "})();</script>"
    )


def theme_tokens_css() -> str:
    """Return the CSS custom-property token set (light + dark + system).

    The variable set is a SUPERSET of every var referenced across the landing,
    chat, and admin surfaces today, so all existing component rules keep
    resolving. Both light and dark values are provided for every token.

    Method/status mapping (the signature color system):
      --cyan  = GET    (blue)
      --green = POST   (emerald/green)
      --amber = PUT/PATCH (amber)
      --red   = DELETE (red)
      --accent = indigo (the single restrained UI accent)
    """
    return """/* ================================================================
   Oh My Gateway — design tokens (light + dark + system)
   data-theme on <html>: "light" | "dark" | "system"
   ================================================================ */

:root {
  color-scheme: light;

  /* --- Method / status: GET=blue, POST=green, PUT/PATCH=amber, DELETE=red --- */
  --green: #047857;          /* POST */
  --green-dim: #065f46;
  --green-muted: #6ee7b7;
  --green-subtle: #ecfdf5;
  --green-glow: none;
  --green-fg: #ffffff;

  --cyan: #2563eb;           /* GET */
  --cyan-dim: #1d4ed8;
  --cyan-muted: #93c5fd;
  --cyan-subtle: #eff6ff;
  --cyan-fg: #ffffff;

  --amber: #b45309;          /* PUT / PATCH */
  --amber-dim: #92400e;
  --amber-muted: #fcd34d;
  --amber-subtle: #fffbeb;
  --amber-fg: #ffffff;

  --red: #dc2626;            /* DELETE */
  --red-dim: #b91c1c;
  --red-muted: #fca5a5;
  --red-subtle: #fef2f2;
  --red-fg: #ffffff;

  --magenta: #7c3aed;        /* AskUserQuestion / agent accents */
  --magenta-subtle: #f5f3ff;

  /* --- Accent: indigo/violet (the one restrained UI accent) --- */
  --accent: #4f46e5;
  --accent-hover: #4338ca;
  --accent-subtle: #eef2ff;
  --accent-fg: #ffffff;

  /* --- Surfaces --- */
  --bg-deep: #f7f7f8;        /* page */
  --bg: #ffffff;             /* card / control */
  --bg-raised: #ffffff;
  --bg-surface: #f1f5f9;     /* raised / subtle */
  --bg-hover: #eef2f6;
  --border: #e4e4e7;
  --border-dim: #efeff1;
  --border-bright: #d4d4d8;

  /* --- Text --- */
  --text: #27272a;
  --text-bright: #18181b;
  --text-dim: #52525b;
  --text-muted: #a1a1aa;

  /* --- Semantic aliases --- */
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

  /* --- Radius --- */
  --radius-sm: 6px;
  --radius: 8px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --radius-pill: 999px;

  /* --- Elevation (prefer borders over heavy shadows) --- */
  --shadow-sm: 0 1px 2px rgba(24, 24, 27, 0.05);
  --shadow-md: 0 4px 12px rgba(24, 24, 27, 0.08);
  --shadow-lg: 0 12px 30px rgba(24, 24, 27, 0.12);
  --ring: 0 0 0 3px rgba(79, 70, 229, 0.28);

  /* --- Typography --- */
  --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Apple Color Emoji", sans-serif;
  --font-mono: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --fs-xs: 0.75rem;
  --fs-sm: 0.8125rem;
  --fs-base: 0.9rem;
  --fs-lg: 1rem;
  --fs-xl: 1.25rem;
  --fs-2xl: 1.6rem;
  --fs-display: 2rem;
}

/* ---------------- Dark: explicit override ---------------- */
html[data-theme="dark"] {
  color-scheme: dark;

  --green: #34d399;
  --green-dim: #6ee7b7;
  --green-muted: #065f46;
  --green-subtle: rgba(16, 185, 129, 0.14);
  --green-glow: none;
  --green-fg: #052e1b;

  --cyan: #60a5fa;
  --cyan-dim: #93c5fd;
  --cyan-muted: #1e3a8a;
  --cyan-subtle: rgba(59, 130, 246, 0.16);
  --cyan-fg: #06183a;

  --amber: #fbbf24;
  --amber-dim: #fcd34d;
  --amber-muted: #78350f;
  --amber-subtle: rgba(245, 158, 11, 0.15);
  --amber-fg: #1c1206;

  --red: #f87171;
  --red-dim: #fca5a5;
  --red-muted: #7f1d1d;
  --red-subtle: rgba(239, 68, 68, 0.16);
  --red-fg: #2a0808;

  --magenta: #a78bfa;
  --magenta-subtle: rgba(124, 58, 237, 0.18);

  --accent: #818cf8;
  --accent-hover: #a5b4fc;
  --accent-subtle: rgba(99, 102, 241, 0.18);
  --accent-fg: #0a0a0b;

  --bg-deep: #0a0a0b;
  --bg: #161618;
  --bg-raised: #161618;
  --bg-surface: #1f1f23;
  --bg-hover: #25252a;
  --border: #2a2a2e;
  --border-dim: #1f1f23;
  --border-bright: #3a3a40;

  --text: #ededf0;
  --text-bright: #fafafa;
  --text-dim: #a1a1aa;
  --text-muted: #71717a;

  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.45);
  --shadow-lg: 0 12px 30px rgba(0, 0, 0, 0.55);
  --ring: 0 0 0 3px rgba(129, 140, 248, 0.4);
}

/* ---------------- System: follow OS only when not explicit ---------------- */
@media (prefers-color-scheme: dark) {
  html[data-theme="system"],
  :root:not([data-theme]) {
    color-scheme: dark;

    --green: #34d399;
    --green-dim: #6ee7b7;
    --green-muted: #065f46;
    --green-subtle: rgba(16, 185, 129, 0.14);
    --green-glow: none;
    --green-fg: #052e1b;

    --cyan: #60a5fa;
    --cyan-dim: #93c5fd;
    --cyan-muted: #1e3a8a;
    --cyan-subtle: rgba(59, 130, 246, 0.16);
    --cyan-fg: #06183a;

    --amber: #fbbf24;
    --amber-dim: #fcd34d;
    --amber-muted: #78350f;
    --amber-subtle: rgba(245, 158, 11, 0.15);
    --amber-fg: #1c1206;

    --red: #f87171;
    --red-dim: #fca5a5;
    --red-muted: #7f1d1d;
    --red-subtle: rgba(239, 68, 68, 0.16);
    --red-fg: #2a0808;

    --magenta: #a78bfa;
    --magenta-subtle: rgba(124, 58, 237, 0.18);

    --accent: #818cf8;
    --accent-hover: #a5b4fc;
    --accent-subtle: rgba(99, 102, 241, 0.18);
    --accent-fg: #0a0a0b;

    --bg-deep: #0a0a0b;
    --bg: #161618;
    --bg-raised: #161618;
    --bg-surface: #1f1f23;
    --bg-hover: #25252a;
    --border: #2a2a2e;
    --border-dim: #1f1f23;
    --border-bright: #3a3a40;

    --text: #ededf0;
    --text-bright: #fafafa;
    --text-dim: #a1a1aa;
    --text-muted: #71717a;

    --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
    --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.45);
    --shadow-lg: 0 12px 30px rgba(0, 0, 0, 0.55);
    --ring: 0 0 0 3px rgba(129, 140, 248, 0.4);
  }
}
"""


def base_css() -> str:
    """Return the reset, base element styles, and shared component classes.

    Built entirely on the tokens from `theme_tokens_css()`. UI text uses
    var(--font) (sans); only code/endpoint/data uses var(--font-mono). Selector
    specificity is kept low (single class / element selectors) so per-page
    component CSS can still override without `!important`.
    """
    return """/* ================================================================
   Oh My Gateway — base + shared components
   Built on the design tokens. UI text = var(--font) [sans];
   code / endpoints / data = var(--font-mono).
   ================================================================ */

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html {
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}

body {
  min-height: 100vh;
  background: var(--bg-deep);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-base);
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  overflow-x: hidden;
}

a {
  color: var(--accent);
  text-decoration: none;
  transition: color 0.15s ease;
}
a:hover { color: var(--accent-hover); }

h1, h2, h3, h4 {
  color: var(--text-bright);
  line-height: 1.25;
  font-weight: 650;
}

hr { border: 0; border-top: 1px solid var(--border); margin: var(--gap-lg) 0; }

/* --- Focus ring (visible, accent) --- */
:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

/* --- Code / mono surfaces --- */
code, kbd, samp, pre {
  font-family: var(--font-mono);
  font-size: 0.92em;
}
:not(pre) > code {
  padding: 1px 5px;
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  border: 1px solid var(--border-dim);
  color: var(--text-bright);
}
pre {
  padding: var(--gap-md);
  border-radius: var(--radius);
  background: var(--bg-surface);
  border: 1px solid var(--border);
  overflow-x: auto;
  font-size: var(--fs-sm);
  line-height: 1.55;
}
pre code { padding: 0; background: none; border: 0; }

/* --- Card --- */
.card {
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--gap-lg);
  box-shadow: var(--shadow-sm);
}

/* --- Buttons --- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--gap-xs);
  min-height: 34px;
  padding: 6px 13px;
  border: 1px solid var(--border-bright);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text-bright);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--fs-sm);
  font-weight: 600;
  line-height: 1.2;
  white-space: nowrap;
  text-decoration: none;
  transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease;
}
.btn:hover { background: var(--bg-hover); border-color: var(--text-dim); }
.btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }

.btn-primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-fg);
}
.btn-primary:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
  color: var(--accent-fg);
}

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

.btn-sm { min-height: 30px; padding: 4px 10px; font-size: var(--fs-xs); }

/* --- Badges (generic + HTTP method variants) --- */
.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 22px;
  padding: 1px 8px;
  border: 1px solid var(--border-bright);
  border-radius: var(--radius-pill);
  background: var(--bg-surface);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
}
.badge-get    { background: var(--cyan-subtle);  color: var(--cyan-dim);  border-color: var(--cyan-muted); }
.badge-post   { background: var(--green-subtle); color: var(--green-dim); border-color: var(--green-muted); }
.badge-put,
.badge-patch  { background: var(--amber-subtle); color: var(--amber-dim); border-color: var(--amber-muted); }
.badge-delete,
.badge-del    { background: var(--red-subtle);   color: var(--red-dim);   border-color: var(--red-muted); }
/* status flavors */
.badge-ok   { background: var(--green-subtle); color: var(--green-dim); border-color: var(--green-muted); }
.badge-warn { background: var(--amber-subtle); color: var(--amber-dim); border-color: var(--amber-muted); }
.badge-err  { background: var(--red-subtle);   color: var(--red-dim);   border-color: var(--red-muted); }
.badge-info { background: var(--cyan-subtle);  color: var(--cyan-dim);  border-color: var(--cyan-muted); }

/* --- Tags / chips --- */
.tag, .chip {
  display: inline-flex;
  align-items: center;
  gap: var(--gap-xs);
  padding: 2px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  background: var(--bg-surface);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 500;
  white-space: nowrap;
}

/* --- Inputs --- */
input[type="text"],
input[type="number"],
input[type="password"],
input[type="email"],
input[type="search"],
input[type="date"],
select,
textarea {
  min-height: 34px;
  width: auto;
  background: var(--bg);
  border: 1px solid var(--border-bright);
  border-radius: var(--radius-sm);
  color: var(--text-bright);
  font-family: var(--font);
  font-size: var(--fs-sm);
  padding: 6px 10px;
  outline: none;
  caret-color: var(--accent);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
input[type="text"]:focus,
input[type="number"]:focus,
input[type="password"]:focus,
input[type="email"]:focus,
input[type="search"]:focus,
input[type="date"]:focus,
select:focus,
textarea:focus {
  border-color: var(--accent);
  box-shadow: var(--ring);
}
input::placeholder, textarea::placeholder { color: var(--text-muted); }
textarea { line-height: 1.5; resize: vertical; }
select {
  cursor: pointer;
  -webkit-appearance: none;
  appearance: none;
  padding-right: 28px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%2371717a'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
}
input[type="checkbox"], input[type="radio"] { accent-color: var(--accent); }

/* --- Tables (tabular data = mono numerals) --- */
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
  letter-spacing: 0.04em;
  white-space: nowrap;
}
table td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
  font-variant-numeric: tabular-nums;
}
table tr:hover td { background: var(--bg-hover); }

/* --- Toast --- */
.toast {
  max-width: min(420px, calc(100vw - 2rem));
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: var(--radius);
  background: var(--bg);
  box-shadow: var(--shadow-lg);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-sm);
  animation: omg-toast-in 0.2s ease;
}
.toast-ok  { border-left-color: var(--green); }
.toast-err { border-left-color: var(--red); }
@keyframes omg-toast-in {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* --- Theme toggle (3-way segmented control) --- */
.theme-toggle {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  background: var(--bg-surface);
}
.theme-toggle button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.theme-toggle button:hover { color: var(--text); }
.theme-toggle button svg { width: 15px; height: 15px; display: block; }
.theme-toggle button[aria-pressed="true"] {
  background: var(--bg);
  color: var(--accent);
  box-shadow: var(--shadow-sm);
}
.theme-toggle button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* --- Scrollbars (theme-aware) --- */
* { scrollbar-width: thin; scrollbar-color: var(--border-bright) transparent; }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-bright); border-radius: var(--radius-pill); }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* --- Reduced motion --- */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
"""


def theme_toggle_html() -> str:
    """Return markup for a compact 3-way segmented theme control.

    Three buttons (Light / System / Dark) with inline sun/monitor/moon SVGs and
    data-theme-set attributes. NO Alpine directives — works on any page. The
    wrapping element carries a stable class (.theme-toggle) and id
    (#omg-theme-toggle) the controller hooks onto. Include once per page.
    """
    sun = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41'
        'M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>'
    )
    monitor = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<rect x="2" y="3" width="20" height="14" rx="2"/>'
        '<path d="M8 21h8M12 17v4"/></svg>'
    )
    moon = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
    )
    return (
        '<div class="theme-toggle" id="omg-theme-toggle" role="group" aria-label="Color theme">'
        '<button type="button" data-theme-set="light" aria-pressed="false" '
        'aria-label="Light theme" title="Light">' + sun + "</button>"
        '<button type="button" data-theme-set="system" aria-pressed="false" '
        'aria-label="System theme" title="System">' + monitor + "</button>"
        '<button type="button" data-theme-set="dark" aria-pressed="false" '
        'aria-label="Dark theme" title="Dark">' + moon + "</button>"
        "</div>"
    )


def theme_toggle_js() -> str:
    """Return the vanilla-JS <script> controller for the theme toggle.

    IIFE, idempotent (guarded so re-inclusion is a no-op). Reads/writes
    localStorage["omg-theme"], sets <html data-theme>, marks the active segment
    (aria-pressed + .is-active), and live-reflects OS changes via matchMedia
    while in "system" mode. Runs on DOMContentLoaded and immediately if the DOM
    is already parsed. Safe to include once per page on plain pages and admin.
    """
    return (
        "<script>(function(){"
        "if(window.__omgTheme)return;window.__omgTheme=true;"
        "var KEY='" + _STORAGE_KEY + "';"
        "var VALID={light:1,dark:1,system:1};"
        "function read(){try{var v=localStorage.getItem(KEY);"
        "return VALID[v]?v:'system';}catch(e){return 'system';}}"
        "function apply(v){document.documentElement.setAttribute('data-theme',v);}"
        "function mark(v){"
        "var els=document.querySelectorAll('[data-theme-set]');"
        "for(var i=0;i<els.length;i++){var on=els[i].getAttribute('data-theme-set')===v;"
        "els[i].setAttribute('aria-pressed',on?'true':'false');"
        "els[i].classList.toggle('is-active',on);}}"
        "function set(v){if(!VALID[v])v='system';"
        "try{localStorage.setItem(KEY,v);}"
        "catch(e){if(window.console&&console.debug)console.debug('omg-theme: persist failed',e);}"
        "apply(v);mark(v);}"
        "function init(){var cur=read();apply(cur);mark(cur);"
        "document.addEventListener('click',function(e){"
        "var t=e.target.closest&&e.target.closest('[data-theme-set]');"
        "if(t){set(t.getAttribute('data-theme-set'));}});"
        "try{var mq=window.matchMedia('(prefers-color-scheme: dark)');"
        "var onChange=function(){if(read()==='system')mark('system');};"
        "if(mq.addEventListener)mq.addEventListener('change',onChange);"
        "else if(mq.addListener)mq.addListener(onChange);}"
        "catch(e){if(window.console&&console.debug)console.debug('omg-theme: matchMedia unavailable',e);}}"
        "if(document.readyState==='loading'){"
        "document.addEventListener('DOMContentLoaded',init);}else{init();}"
        "})();</script>"
    )
