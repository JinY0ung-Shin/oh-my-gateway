from typing import Any, Dict


def build_root_page(version: str, auth_info: Dict[str, Any], default_port: int) -> str:
    """Build the landing page HTML."""
    auth_method = auth_info.get("method", "unknown")
    auth_valid = auth_info.get("status", {}).get("valid", False)
    status_text = "ONLINE" if auth_valid else "OFFLINE"
    status_class = "online" if auth_valid else "offline"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Gateway</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ================================================================
   TERMINAL DESIGN SYSTEM — Landing Page
   Mirrors the admin panel phosphor/CRT aesthetic
   ================================================================ */

:root {{
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

  --bg-deep: #050505;
  --bg: #0a0a0a;
  --bg-raised: #111111;
  --bg-surface: #161616;
  --bg-hover: #1a1a1a;
  --border: #1e1e1e;
  --border-bright: #2a2a2a;

  --text: #b0ffb0;
  --text-bright: #00ff41;
  --text-dim: #4a7a4a;
  --text-muted: #3a5a3a;

  --font: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'SF Mono', monospace;
  --fs-xs: 0.7rem;
  --fs-sm: 0.78rem;
  --fs-base: 0.85rem;
  --fs-lg: 1rem;
  --fs-xl: 1.2rem;
}}

*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
  background: var(--bg-deep);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-base);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}}

/* CRT Scanline Overlay */
body::before {{
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
}}

/* Grid Background */
body::after {{
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
}}

.container {{ max-width: 960px; margin: 0 auto; padding: 1.5rem; }}

a {{ color: var(--green-dim); text-decoration: none; transition: color 0.15s, text-shadow 0.15s; }}
a:hover {{ color: var(--green); text-shadow: 0 0 6px var(--green-muted); }}

/* === ASCII Header === */
.ascii-header {{
  font-size: var(--fs-xs);
  color: var(--green-dim);
  text-align: center;
  line-height: 1.15;
  letter-spacing: 0.05em;
  margin-bottom: var(--fs-sm);
  text-shadow: 0 0 8px var(--green-muted);
  white-space: pre;
  overflow: hidden;
}}

.header-bar {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.5rem 0;
  border-bottom: 1px solid var(--border-bright);
  margin-bottom: 1.5rem;
  flex-wrap: wrap;
  gap: 0.5rem;
}}
.header-bar .left {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
}}
.header-bar .right {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
}}
.version-tag {{
  font-size: var(--fs-xs);
  color: var(--text-dim);
  border: 1px solid var(--border-bright);
  padding: 2px 8px;
}}
.github-link {{
  font-size: var(--fs-xs);
  color: var(--text-dim);
  border: 1px solid var(--border);
  padding: 2px 8px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: all 0.15s;
}}
.github-link:hover {{
  color: var(--green);
  border-color: var(--green-dim);
}}
.github-link svg {{ width: 14px; height: 14px; }}

/* Status */
.status-indicator {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
}}
.status-dot {{
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}}
.status-dot.online {{
  background: var(--green);
  box-shadow: 0 0 8px var(--green-muted);
  animation: pulse-glow 2s ease-in-out infinite;
}}
.status-dot.offline {{
  background: var(--red);
  box-shadow: 0 0 8px var(--red-subtle);
  animation: pulse-glow 2s ease-in-out infinite;
}}
.status-label.online {{ color: var(--green); text-shadow: 0 0 6px var(--green-muted); }}
.status-label.offline {{ color: var(--red); }}

@keyframes pulse-glow {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.5; }}
}}

/* Auth badge */
.auth-badge {{
  font-size: var(--fs-xs);
  color: var(--cyan);
  border: 1px solid var(--cyan-dim);
  padding: 1px 8px;
  background: var(--cyan-subtle);
}}

/* === Cards === */
.card {{
  background: var(--bg-raised);
  border: 1px solid var(--border);
  padding: 1rem 1.25rem;
  margin-bottom: 1rem;
  position: relative;
}}
.card::before {{
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--green-dim), transparent);
  opacity: 0.4;
}}
.card-title {{
  font-size: var(--fs-sm);
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  font-weight: 500;
  margin-bottom: 0.75rem;
}}
.card-title::before {{
  content: '// ';
  color: var(--text-muted);
}}

/* === Quick Start === */
.quickstart-wrapper {{
  position: relative;
  background: var(--bg);
  border: 1px solid var(--border);
  padding: 1rem;
  overflow-x: auto;
}}
.quickstart-wrapper pre {{
  margin: 0;
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-all;
}}
.copy-btn {{
  position: absolute;
  top: 0.5rem;
  right: 0.5rem;
  padding: 4px 10px;
  background: var(--bg-raised);
  border: 1px solid var(--border-bright);
  color: var(--text-dim);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--fs-xs);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  transition: all 0.15s;
}}
.copy-btn:hover {{
  color: var(--green);
  border-color: var(--green-dim);
  text-shadow: 0 0 4px var(--green-muted);
}}
.copy-btn.copied {{
  color: var(--green);
  border-color: var(--green-dim);
}}

/* === Endpoint List === */
.endpoint-group-label {{
  font-size: var(--fs-xs);
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--text-muted);
  padding: 0.75rem 0 0.35rem;
}}
.endpoint-group-label:first-child {{ padding-top: 0; }}

.endpoint-row {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.4rem 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-sm);
}}
.endpoint-row:last-child {{ border-bottom: none; }}

.badge {{
  display: inline-block;
  padding: 1px 8px;
  font-size: var(--fs-xs);
  font-weight: 500;
  font-family: var(--font);
  letter-spacing: 0.05em;
  border: 1px solid;
  flex-shrink: 0;
  min-width: 44px;
  text-align: center;
}}
.badge-post {{
  background: var(--green-subtle);
  color: var(--green);
  border-color: var(--green-dim);
  text-shadow: 0 0 4px var(--green-muted);
}}
.badge-get {{
  background: var(--cyan-subtle);
  color: var(--cyan);
  border-color: var(--cyan-dim);
}}
.badge-del {{
  background: var(--red-subtle);
  color: var(--red);
  border-color: var(--red-dim);
}}

.endpoint-path {{
  color: var(--text);
  font-family: var(--font);
  flex: 1;
}}
.endpoint-desc {{
  color: var(--text-dim);
  font-size: var(--fs-xs);
  flex-shrink: 0;
}}

/* === Expandable Details === */
details {{
  border: 1px solid var(--border);
  background: var(--bg-surface);
  margin-bottom: 2px;
}}
details summary {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.4rem 0.75rem;
  cursor: pointer;
  list-style: none;
  font-size: var(--fs-sm);
  transition: background 0.1s;
}}
details summary::-webkit-details-marker {{ display: none; }}
details summary::after {{
  content: '>';
  margin-left: auto;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  transition: transform 0.15s;
}}
details[open] summary::after {{
  transform: rotate(90deg);
  color: var(--green-dim);
}}
details[open] summary {{
  border-bottom: 1px solid var(--border);
}}
details summary:hover {{
  background: var(--bg-hover);
}}
details .detail-body {{
  padding: 0.75rem;
  font-size: var(--fs-sm);
}}
details .detail-body pre {{
  margin: 0;
  overflow-x: auto;
}}

/* === Config Grid === */
.config-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.75rem;
}}
.config-item {{
  padding: 0.75rem;
  background: var(--bg-surface);
  border: 1px solid var(--border);
}}
.config-item .val {{
  color: var(--green);
  font-weight: 600;
  font-size: var(--fs-sm);
  text-shadow: 0 0 4px var(--green-muted);
}}
.config-item .label {{
  font-size: var(--fs-xs);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-top: 2px;
}}

/* === Footer === */
footer {{
  border-top: 1px solid var(--border);
  padding-top: 1rem;
  margin-top: 1rem;
}}
footer nav {{
  display: flex;
  justify-content: center;
  gap: 2rem;
  flex-wrap: wrap;
}}
footer a {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 4px 0;
  border-bottom: 1px solid transparent;
  transition: all 0.15s;
}}
footer a:hover {{
  color: var(--green);
  border-bottom-color: var(--green-dim);
  text-shadow: 0 0 4px var(--green-muted);
}}
footer a::before {{
  content: '>';
  color: var(--text-muted);
  transition: color 0.15s;
}}
footer a:hover::before {{
  color: var(--green-dim);
}}
footer .copyright {{
  text-align: center;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  margin-top: 0.75rem;
}}

/* === Loading spinner === */
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

/* Shiki overrides */
.shiki {{
  padding: 0 !important;
  margin: 0 !important;
  background: transparent !important;
  overflow-x: auto;
}}
.shiki code {{
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font);
  font-size: var(--fs-sm);
}}

/* Responsive */
@media (max-width: 640px) {{
  .container {{ padding: 1rem; }}
  .ascii-header {{ font-size: 0.45rem; }}
  .endpoint-desc {{ display: none; }}
  .config-grid {{ grid-template-columns: 1fr 1fr; }}
}}
</style>
<script type="module">
    import {{ codeToHtml }} from 'https://esm.sh/shiki@3.0.0';

    const theme = 'vitesse-dark';

    async function highlightJson(json, targetId) {{
        const code = typeof json === 'string' ? json : JSON.stringify(json, null, 2);
        try {{
            const html = await codeToHtml(code, {{ lang: 'json', theme }});
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
            const html = await codeToHtml(quickstartCode, {{ lang: 'bash', theme }});
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
        try {{ document.execCommand('copy'); showCopied(btn); }} catch (e) {{}}
        document.body.removeChild(ta);
    }}

    function showCopied(btn) {{
        const orig = btn.textContent;
        btn.textContent = 'COPIED';
        btn.classList.add('copied');
        setTimeout(() => {{ btn.textContent = orig; btn.classList.remove('copied'); }}, 2000);
    }}
</script>
</head>
<body>
<main class="container">

    <!-- ASCII Art Header -->
    <div class="ascii-header" aria-hidden="true">
 ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗     ██████╗  █████╗ ████████╗███████╗██╗    ██╗ █████╗ ██╗   ██╗
██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝    ██╔════╝ ██╔══██╗╚══██╔══╝██╔════╝██║    ██║██╔══██╗╚██╗ ██╔╝
██║     ██║     ███████║██║   ██║██║  ██║█████╗      ██║  ███╗███████║   ██║   █████╗  ██║ █╗ ██║███████║ ╚████╔╝
██║     ██║     ██╔══██║██║   ██║██║  ██║██╔══╝      ██║   ██║██╔══██║   ██║   ██╔══╝  ██║███╗██║██╔══██║  ╚██╔╝
╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗    ╚██████╔╝██║  ██║   ██║   ███████╗╚███╔███╔╝██║  ██║   ██║
 ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝     ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝ ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝
    </div>

    <!-- Header Bar -->
    <div class="header-bar">
        <div class="left">
            <span class="status-indicator">
                <span class="status-dot {status_class}"></span>
                <span class="status-label {status_class}">{status_text}</span>
            </span>
            <span class="auth-badge">AUTH: {auth_method}</span>
        </div>
        <div class="right">
            <span class="version-tag">v{version}</span>
            <a href="https://github.com/JinY0ung-Shin/claude-code-gateway" target="_blank" rel="noopener noreferrer" class="github-link" title="GitHub">
                <svg fill="currentColor" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" clip-rule="evenodd"/></svg>
                GITHUB
            </a>
        </div>
    </div>

    <!-- Quick Start -->
    <div class="card">
        <div class="card-title">Quick Start</div>
        <div class="quickstart-wrapper">
            <button id="copy-btn" onclick="copyQuickstart()" class="copy-btn" title="Copy">COPY</button>
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

        <div class="endpoint-row">
            <span class="badge badge-post">POST</span>
            <span class="endpoint-path">/v1/compatibility</span>
            <span class="endpoint-desc">Request compat check</span>
        </div>
    </div>

    <!-- Configuration -->
    <div class="card">
        <div class="card-title">Configuration</div>
        <p style="color:var(--text-dim);font-size:var(--fs-sm);margin-bottom:0.75rem;">
            Set <span style="color:var(--amber);">CLAUDE_AUTH_METHOD</span> to choose authentication:
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
            <a href="/admin">Admin Terminal</a>
            <a href="/admin/chat">Chat</a>
        </nav>
        <div class="copyright">CLAUDE CODE GATEWAY // v{version}</div>
    </footer>

</main>
</body>
</html>"""
