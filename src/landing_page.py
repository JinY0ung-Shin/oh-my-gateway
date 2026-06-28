import html
from typing import Any, Dict

from src.theme import (
    theme_head_init,
    theme_tokens_css,
    base_css,
    theme_toggle_html,
    theme_toggle_js,
)


def build_root_page(version: str, auth_info: Dict[str, Any], default_port: int) -> str:
    """Build the landing page HTML."""
    auth_method = html.escape(str(auth_info.get("method", "unknown")))
    auth_valid = auth_info.get("status", {}).get("valid", False)
    status_text = html.escape("Online" if auth_valid else "Offline")
    status_class = "online" if auth_valid else "offline"
    version = html.escape(str(version))

    # Theme module returns single-brace CSS/JS/HTML. Because the page body below
    # is an f-string (where only `{name}` interpolates and `{{`/`}}` are literal
    # braces), interpolated values are inserted verbatim — their `{`/`}` are NOT
    # re-processed. So these can be embedded directly with no brace escaping.
    head_init = theme_head_init()
    tokens_css = theme_tokens_css()
    shared_css = base_css()
    toggle_html = theme_toggle_html()
    toggle_js = theme_toggle_js()

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="system">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Oh My Gateway</title>
{head_init}
<style>
{tokens_css}
{shared_css}

/* ================================================================
   Landing page — modern developer console (page-specific styles)
   ================================================================ */

.container {{ max-width: 960px; margin: 0 auto; padding: 2rem 1.5rem 3rem; }}

/* === Wordmark / header === */
.wordmark {{
  font-family: var(--font);
  font-weight: 700;
  font-size: var(--fs-2xl);
  letter-spacing: -0.02em;
  color: var(--text-bright);
  margin: 0;
  line-height: 1.1;
}}
.wordmark .accent {{ color: var(--accent); }}
.tagline {{
  color: var(--text-dim);
  font-size: var(--fs-sm);
  margin-top: 0.35rem;
}}

.header-bar {{
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding-bottom: 1.25rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 1.75rem;
  flex-wrap: wrap;
  gap: 1rem;
}}
.header-bar .left {{
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}}
.header-bar .meta {{
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}}
.header-bar .right {{
  display: flex;
  align-items: center;
  gap: 0.6rem;
}}

.version-tag {{
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-dim);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  padding: 2px 10px;
  background: var(--bg-surface);
}}
.github-link {{
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-dim);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 5px 10px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition: color 0.15s ease, border-color 0.15s ease, background-color 0.15s ease;
}}
.github-link:hover {{
  color: var(--text-bright);
  border-color: var(--border-bright);
  background: var(--bg-hover);
}}
.github-link svg {{ width: 15px; height: 15px; }}

/* === Status === */
.status-indicator {{
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: var(--fs-sm);
  font-weight: 500;
}}
.status-dot {{
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}}
.status-dot.online {{
  background: var(--green);
  box-shadow: 0 0 0 3px var(--green-subtle);
}}
.status-dot.offline {{
  background: var(--red);
  box-shadow: 0 0 0 3px var(--red-subtle);
}}
.status-label.online {{ color: var(--green-dim); }}
.status-label.offline {{ color: var(--red-dim); }}

/* === Auth badge === */
.auth-badge {{
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--accent);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  padding: 2px 10px;
  background: var(--accent-subtle);
}}

/* === Cards === */
.card {{ margin-bottom: 1.25rem; }}
.card-title {{
  font-size: var(--fs-xs);
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 700;
  margin-bottom: 1rem;
}}

/* === Quick Start === */
.quickstart-wrapper {{
  position: relative;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1rem 1.1rem;
  overflow-x: auto;
}}
.quickstart-wrapper pre {{
  margin: 0;
  padding: 0;
  border: 0;
  background: none;
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}}
.copy-btn {{
  position: absolute;
  top: 0.6rem;
  right: 0.6rem;
  padding: 4px 11px;
  background: var(--bg);
  border: 1px solid var(--border-bright);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--fs-xs);
  font-weight: 600;
  letter-spacing: 0.02em;
  transition: color 0.15s ease, border-color 0.15s ease, background-color 0.15s ease;
}}
.copy-btn:hover {{
  color: var(--text-bright);
  border-color: var(--text-dim);
  background: var(--bg-hover);
}}
.copy-btn.copied {{
  color: var(--green-dim);
  border-color: var(--green-muted);
  background: var(--green-subtle);
}}

/* === Endpoint List === */
.endpoint-group-label {{
  font-size: var(--fs-xs);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  padding: 1rem 0 0.4rem;
}}
.endpoint-group-label:first-child {{ padding-top: 0; }}

.endpoint-row {{
  display: flex;
  align-items: center;
  gap: 0.85rem;
  padding: 0.5rem 0;
  border-bottom: 1px solid var(--border-dim);
  font-size: var(--fs-sm);
}}
.endpoint-row:last-child {{ border-bottom: none; }}

.badge {{ min-width: 50px; }}

.endpoint-path {{
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}}
.endpoint-desc {{
  color: var(--text-dim);
  font-size: var(--fs-xs);
  flex-shrink: 0;
}}

/* === Expandable Details === */
details {{
  border: 1px solid var(--border-dim);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  margin-bottom: 4px;
}}
details summary {{
  display: flex;
  align-items: center;
  gap: 0.85rem;
  padding: 0.5rem 0.85rem;
  cursor: pointer;
  list-style: none;
  font-size: var(--fs-sm);
  border-radius: var(--radius-sm);
  transition: background-color 0.12s ease;
}}
details summary::-webkit-details-marker {{ display: none; }}
details summary::after {{
  content: '';
  width: 7px;
  height: 7px;
  margin-left: auto;
  border-right: 1.5px solid var(--text-muted);
  border-bottom: 1.5px solid var(--text-muted);
  transform: rotate(-45deg);
  transition: transform 0.15s ease, border-color 0.15s ease;
  flex-shrink: 0;
}}
details[open] summary::after {{
  transform: rotate(45deg);
  border-color: var(--accent);
}}
details[open] summary {{
  border-bottom: 1px solid var(--border);
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 0;
}}
details summary:hover {{
  background: var(--bg-hover);
}}
details .detail-body {{
  padding: 0.85rem;
  font-size: var(--fs-sm);
}}
details .detail-body pre {{
  margin: 0;
  overflow-x: auto;
  font-family: var(--font-mono);
}}

/* === Config Grid === */
.config-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 0.75rem;
}}
.config-item {{
  padding: 0.85rem;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}}
.config-item .val {{
  color: var(--text-bright);
  font-family: var(--font-mono);
  font-weight: 600;
  font-size: var(--fs-sm);
}}
.config-item .label {{
  font-size: var(--fs-xs);
  color: var(--text-muted);
  margin-top: 4px;
}}
.config-key {{
  font-family: var(--font-mono);
  color: var(--accent);
  font-weight: 600;
}}

/* === Footer === */
footer {{
  border-top: 1px solid var(--border);
  padding-top: 1.25rem;
  margin-top: 1.5rem;
}}
footer nav {{
  display: flex;
  justify-content: center;
  gap: 1.75rem;
  flex-wrap: wrap;
}}
footer a {{
  font-size: var(--fs-sm);
  font-weight: 500;
  color: var(--text-dim);
  padding: 4px 0;
  border-bottom: 1px solid transparent;
  transition: color 0.15s ease, border-color 0.15s ease;
}}
footer a:hover {{
  color: var(--text-bright);
  border-bottom-color: var(--accent);
}}
footer .copyright {{
  text-align: center;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-muted);
  margin-top: 1rem;
}}

/* === Loading indicator === */
.loader {{
  color: var(--text-dim);
  font-size: var(--fs-xs);
}}
.loader::after {{
  content: '';
  animation: dots 1.2s steps(4, end) infinite;
}}
@keyframes dots {{
  0% {{ content: ''; }}
  25% {{ content: '.'; }}
  50% {{ content: '..'; }}
  75% {{ content: '...'; }}
}}

.hidden {{ display: none !important; }}

/* === Shiki overrides === */
.shiki {{
  padding: 0 !important;
  margin: 0 !important;
  background: transparent !important;
  overflow-x: auto;
}}
.shiki code {{
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
}}

/* === Responsive === */
@media (max-width: 640px) {{
  .container {{ padding: 1.25rem 1rem 2rem; }}
  .wordmark {{ font-size: var(--fs-xl); }}
  .endpoint-desc {{ display: none; }}
  .config-grid {{ grid-template-columns: 1fr 1fr; }}
}}
</style>
<script type="module">
    import {{ codeToHtml }} from 'https://esm.sh/shiki@3.0.0';

    function shikiTheme() {{
        var t = document.documentElement.getAttribute('data-theme');
        if (t === 'dark') return 'github-dark';
        if (t === 'light') return 'github-light';
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'github-dark' : 'github-light';
    }}

    async function highlightJson(json, targetId) {{
        const code = typeof json === 'string' ? json : JSON.stringify(json, null, 2);
        try {{
            const html = await codeToHtml(code, {{ lang: 'json', theme: shikiTheme() }});
            document.getElementById(targetId).innerHTML = html;
        }} catch (e) {{
            document.getElementById(targetId).innerHTML = '<pre style="color:var(--red);">ERR: ' + e.message + '</pre>';
        }}
    }}

    document.querySelectorAll('details[data-endpoint]').forEach(details => {{
        details.addEventListener('toggle', async () => {{
            if (details.open) {{
                const id = details.id;
                const endpoint = details.dataset.endpoint;
                const dataContainer = document.getElementById('data-' + id);
                const loader = document.getElementById('loader-' + id);
                if (!dataContainer.innerHTML) {{
                    loader.classList.remove('hidden');
                    try {{
                        const response = await fetch(endpoint);
                        const json = await response.json();
                        await highlightJson(json, 'data-' + id);
                    }} catch (e) {{
                        dataContainer.innerHTML = '<span style="color:var(--red);">ERR: ' + e.message + '</span>';
                    }}
                    loader.classList.add('hidden');
                }}
            }}
        }});
    }});

    const quickstartCode = `curl -X POST http://localhost:{default_port}/v1/responses \\\\
  -H "Content-Type: application/json" \\\\
  -d '{{"model": "sonnet", "input": "Hello!"}}'`;

    async function highlightQuickstart() {{
        try {{
            const html = await codeToHtml(quickstartCode, {{ lang: 'bash', theme: shikiTheme() }});
            document.getElementById('quickstart-code').innerHTML = html;
        }} catch (e) {{
            document.getElementById('quickstart-code').textContent = quickstartCode;
        }}
    }}

    highlightQuickstart();
</script>
<script>
    const quickstartText = 'curl -X POST http://localhost:{default_port}/v1/responses -H "Content-Type: application/json" -d \\'{{"model": "sonnet", "input": "Hello!"}}\\'';

    function copyQuickstart() {{
        const btn = document.getElementById('copy-btn');
        if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(quickstartText).then(() => showCopied(btn)).catch(() => fallbackCopy(btn));
        }} else {{
            fallbackCopy(btn);
        }}
    }}

    function fallbackCopy(btn) {{
        const ta = document.createElement('textarea');
        ta.value = quickstartText;
        ta.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        try {{ document.execCommand('copy'); showCopied(btn); }}
        catch (e) {{ if (window.console && console.debug) console.debug('copy failed', e); }}
        document.body.removeChild(ta);
    }}

    function showCopied(btn) {{
        const orig = btn.textContent;
        btn.textContent = 'Copied';
        btn.classList.add('copied');
        setTimeout(() => {{ btn.textContent = orig; btn.classList.remove('copied'); }}, 2000);
    }}
</script>
</head>
<body>
<main class="container">

    <!-- Header Bar -->
    <div class="header-bar">
        <div class="left">
            <h1 class="wordmark">Oh My <span class="accent">Gateway</span></h1>
            <p class="tagline">OpenAI-compatible gateway for Claude, OpenCode &amp; Codex backends.</p>
            <div class="meta">
                <span class="status-indicator">
                    <span class="status-dot {status_class}"></span>
                    <span class="status-label {status_class}">{status_text}</span>
                </span>
                <span class="auth-badge">auth: {auth_method}</span>
            </div>
        </div>
        <div class="right">
            {toggle_html}
            <span class="version-tag">v{version}</span>
            <a href="https://github.com/JinY0ung-Shin/oh-my-gateway" target="_blank" rel="noopener noreferrer" class="github-link" title="GitHub">
                <svg fill="currentColor" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" clip-rule="evenodd"/></svg>
                GitHub
            </a>
        </div>
    </div>

    <!-- Quick Start -->
    <div class="card">
        <div class="card-title">Quick Start</div>
        <div class="quickstart-wrapper">
            <button id="copy-btn" onclick="copyQuickstart()" class="copy-btn" title="Copy">Copy</button>
            <div id="quickstart-code">
                <pre>curl -X POST http://localhost:{default_port}/v1/responses \\
  -H "Content-Type: application/json" \\
  -d '{{"model": "sonnet", "input": "Hello!"}}'</pre>
            </div>
        </div>
    </div>

    <!-- API Endpoints -->
    <div class="card">
        <div class="card-title">API Endpoints</div>

        <div class="endpoint-group-label">Completion</div>
        <div class="endpoint-row">
            <span class="badge badge-post">POST</span>
            <span class="endpoint-path">/v1/responses</span>
            <span class="endpoint-desc">Responses API</span>
        </div>

        <div class="endpoint-group-label">Sessions</div>

        <details id="sessions" data-endpoint="/v1/sessions">
            <summary>
                <span class="badge badge-get">GET</span>
                <span class="endpoint-path">/v1/sessions</span>
                <span class="endpoint-desc">List active sessions</span>
            </summary>
            <div class="detail-body">
                <span id="loader-sessions" class="loader hidden">Loading</span>
                <div id="data-sessions"></div>
            </div>
        </details>

        <details id="session-stats" data-endpoint="/v1/sessions/stats">
            <summary>
                <span class="badge badge-get">GET</span>
                <span class="endpoint-path">/v1/sessions/stats</span>
                <span class="endpoint-desc">Session statistics</span>
            </summary>
            <div class="detail-body">
                <span id="loader-session-stats" class="loader hidden">Loading</span>
                <div id="data-session-stats"></div>
            </div>
        </details>

        <div class="endpoint-row">
            <span class="badge badge-get">GET</span>
            <span class="endpoint-path">/v1/sessions/{{session_id}}</span>
            <span class="endpoint-desc">Get session</span>
        </div>
        <div class="endpoint-row">
            <span class="badge badge-del">DEL</span>
            <span class="endpoint-path">/v1/sessions/{{session_id}}</span>
            <span class="endpoint-desc">Delete session</span>
        </div>

        <div class="endpoint-group-label">Discovery &amp; Status</div>

        <details id="models" data-endpoint="/v1/models">
            <summary>
                <span class="badge badge-get">GET</span>
                <span class="endpoint-path">/v1/models</span>
                <span class="endpoint-desc">Available models</span>
            </summary>
            <div class="detail-body">
                <span id="loader-models" class="loader hidden">Loading</span>
                <div id="data-models"></div>
            </div>
        </details>

        <details id="mcp" data-endpoint="/v1/mcp/servers">
            <summary>
                <span class="badge badge-get">GET</span>
                <span class="endpoint-path">/v1/mcp/servers</span>
                <span class="endpoint-desc">MCP servers</span>
            </summary>
            <div class="detail-body">
                <span id="loader-mcp" class="loader hidden">Loading</span>
                <div id="data-mcp"></div>
            </div>
        </details>

        <details id="auth" data-endpoint="/v1/auth/status">
            <summary>
                <span class="badge badge-get">GET</span>
                <span class="endpoint-path">/v1/auth/status</span>
                <span class="endpoint-desc">Auth &amp; backend</span>
            </summary>
            <div class="detail-body">
                <span id="loader-auth" class="loader hidden">Loading</span>
                <div id="data-auth"></div>
            </div>
        </details>

        <details id="health" data-endpoint="/health">
            <summary>
                <span class="badge badge-get">GET</span>
                <span class="endpoint-path">/health</span>
                <span class="endpoint-desc">Health check</span>
            </summary>
            <div class="detail-body">
                <span id="loader-health" class="loader hidden">Loading</span>
                <div id="data-health"></div>
            </div>
        </details>

        <details id="version" data-endpoint="/version">
            <summary>
                <span class="badge badge-get">GET</span>
                <span class="endpoint-path">/version</span>
                <span class="endpoint-desc">API version</span>
            </summary>
            <div class="detail-body">
                <span id="loader-version" class="loader hidden">Loading</span>
                <div id="data-version"></div>
            </div>
        </details>

    </div>

    <!-- Configuration -->
    <div class="card">
        <div class="card-title">Configuration</div>
        <p style="color:var(--text-dim);font-size:var(--fs-sm);margin-bottom:0.75rem;">
            Set <span class="config-key">CLAUDE_AUTH_METHOD</span> to choose authentication:
        </p>
        <div class="config-grid" style="margin-bottom:1rem;">
            <div class="config-item">
                <div class="val">cli</div>
                <div class="label">Claude CLI auth</div>
            </div>
            <div class="config-item">
                <div class="val">api_key</div>
                <div class="label">ANTHROPIC_AUTH_TOKEN</div>
            </div>
        </div>
        <p style="color:var(--text-dim);font-size:var(--fs-sm);margin-bottom:0.75rem;">Backends:</p>
        <div class="config-grid">
            <div class="config-item">
                <div class="val">Claude</div>
                <div class="label">sonnet, opus, haiku</div>
            </div>
        </div>
    </div>

    <!-- Footer -->
    <footer>
        <nav>
            <a href="/docs">API Docs</a>
            <a href="/redoc">ReDoc</a>
            <a href="/admin">Admin</a>
            <a href="/admin/chat">Chat</a>
        </nav>
        <div class="copyright">Oh My Gateway // v{version}</div>
    </footer>

</main>
{toggle_js}
</body>
</html>"""
