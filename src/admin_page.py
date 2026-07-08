"""Admin dashboard HTML generator.

Assembles the full admin page from modular section files.
Each section (CSS, JS, tab HTML) lives in its own module for
maintainability while the final output is identical to the
original monolithic version.
"""

from functools import lru_cache

from src.admin_html_config import get_config_html
from src.admin_html_dashboard import get_dashboard_html
from src.admin_html_logs import get_logs_html
from src.admin_html_mcp import get_mcp_html
from src.admin_html_plugins import get_plugins_html
from src.admin_html_ratelimits import get_ratelimits_html
from src.admin_html_sessions import get_sessions_html
from src.admin_html_skills import get_skills_html
from src.admin_html_usage import get_usage_html
from src.admin_js import get_admin_js
from src.admin_styles import get_admin_css
from src.theme import (
    base_css,
    theme_head_init,
    theme_tokens_css,
    theme_toggle_html,
    theme_toggle_js,
)


@lru_cache(maxsize=1)
def build_admin_page() -> str:
    """Build the admin dashboard HTML.

    Combines CSS, HTML shell, tab sections, and JS into a single
    self-contained HTML string.
    """
    return (
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Oh My Gateway Admin</title>
"""
        + theme_head_init()
        + """
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js" integrity="sha384-X9kJyAubVxnP0hcA+AMMs21U445qsnqhnUF8EBlEpP3a42Kh/JwWjlv2ZcvGfphb" crossorigin="anonymous"></script>
<style>
"""
        + theme_tokens_css()
        + base_css()
        + get_admin_css()
        + """
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
    <div class="login-wrap">
      <div class="login-box card">
      <div class="product-mark">OH MY GATEWAY</div>
      <h1>Admin Access</h1>
      <p class="prompt-prefix">Use the configured ADMIN_API_KEY to continue.</p>
      <form @submit.prevent="doLogin()">
        <label for="admin-login-key" class="text-xs text-dim" style="margin-bottom:4px; display:block">Admin API key</label>
        <input id="admin-login-key" type="password" x-model="loginKey" placeholder="••••••••••••••••"
          style="width:100%; margin-bottom:1rem" required>
        <button class="btn btn-primary" style="width:100%; justify-content:center" type="submit">Sign in</button>
      </form>
      <p x-show="loginError" class="text-danger text-sm" style="margin-top:0.75rem">
        <span x-text="loginError"></span>
      </p>
      </div>
    </div>
  </template>

  <!-- Main UI -->
  <template x-if="authenticated">
    <div>
      <!-- Header Bar -->
      <header class="header-bar">
        <div>
          <div class="product-mark">OH MY GATEWAY</div>
          <h1>Admin</h1>
        </div>
        <div class="flex-gap-sm">
          <span class="status-line">
            <span class="online"></span> Connected
          </span>
          """
        + theme_toggle_html()
        + """
          <button class="btn btn-sm btn-ghost" @click="refreshAll()" aria-label="Refresh all data">Refresh</button>
          <button class="btn btn-sm btn-ghost" @click="doLogout()" aria-label="Log out">Log out</button>
        </div>
      </header>

      <!-- Tabs -->
      <nav class="tabs" role="tablist" aria-label="Admin sections">
        <button role="tab" :aria-selected="tab === 'dashboard'" @click="tab='dashboard'">Dashboard</button>
        <button role="tab" :aria-selected="tab === 'sessions'" @click="tab='sessions'; loadSummary()">Sessions</button>
        <button role="tab" :aria-selected="tab === 'logs'" @click="tab='logs'; loadLogs()">Logs</button>
        <button role="tab" :aria-selected="tab === 'usage'" @click="tab='usage'; loadUsage()">Usage</button>
        <button role="tab" :aria-selected="tab === 'ratelimits'" @click="tab='ratelimits'; loadRateLimits()">Limits</button>
        <button role="tab" :aria-selected="tab === 'skills'" @click="tab='skills'; loadSkills()">Skills</button>
        <button role="tab" :aria-selected="tab === 'plugins'" @click="tab='plugins'; loadPlugins(); loadCatalog(); loadAutoRefresh()">Plugins</button>
        <button role="tab" :aria-selected="tab === 'mcp'" @click="tab='mcp'; loadMcpDetail()">MCP</button>
        <button role="tab" :aria-selected="tab === 'config'" @click="tab='config'; loadConfig(); loadRuntimeConfig(); loadSystemPrompt(); loadTools(); loadSandbox()">Config</button>
        <a href="/admin/chat" role="tab" class="tab-link">Chat ↗</a>
      </nav>

"""
        + get_dashboard_html()
        + "\n\n"
        + get_logs_html()
        + "\n\n"
        + get_usage_html()
        + "\n\n"
        + get_ratelimits_html()
        + "\n\n"
        + get_skills_html()
        + "\n\n"
        + get_plugins_html()
        + "\n\n"
        + get_mcp_html()
        + "\n\n"
        + get_sessions_html()
        + "\n\n"
        + get_config_html()
        + """

    </div>
  </template>
</div>

"""
        + "<script>\n"
        + get_admin_js()
        + """
</script>
"""
        + theme_toggle_js()
        + """
</body>
</html>"""
    )
