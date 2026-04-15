"""Admin dashboard HTML generator.

Assembles the full admin page from modular section files.
Each section (CSS, JS, tab HTML) lives in its own module for
maintainability while the final output is identical to the
original monolithic version.
"""

from src.admin_html_config import get_config_html
from src.admin_html_dashboard import get_dashboard_html
from src.admin_html_files import get_files_html
from src.admin_html_logs import get_logs_html
from src.admin_html_ratelimits import get_ratelimits_html
from src.admin_html_sessions import get_sessions_html
from src.admin_html_skills import get_skills_html
from src.admin_js import get_admin_js
from src.admin_styles import get_admin_css


def build_admin_page() -> str:
    """Build the admin dashboard HTML.

    Combines CSS, HTML shell, tab sections, and JS into a single
    self-contained HTML string.
    """
    return (
        """<!DOCTYPE html>
<html lang="ko" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GATEWAY CTRL // Admin Terminal</title>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js" integrity="sha384-X9kJyAubVxnP0hcA+AMMs21U445qsnqhnUF8EBlEpP3a42Kh/JwWjlv2ZcvGfphb" crossorigin="anonymous"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/lib/codemirror.min.css" integrity="sha384-EAMaqTIrSR3wOlf2EQj7Kkx5ZCHPjHmlYJsFof5IB8dzXMASyR8eIwXgp+IGz/rs" crossorigin="anonymous">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/theme/material-darker.min.css" integrity="sha384-/7IRGUZjrVsGRxRmWYycW70x8/46N5NLfEElIMgl+k0aOfmvzAMSg6Or2NG3a6tp" crossorigin="anonymous">
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/lib/codemirror.min.js" integrity="sha384-t/C99Ss9npjVCqOGZhNjaRnafXAhKyPyhSP+cSSP0CWoFTfMXG4+impqZ6WfgO0a" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/markdown/markdown.min.js" integrity="sha384-n04h9Gi6L8HyI+Xs9bOsNBdP9/Oo6HtQ6x+a7KFxgoZ267JCOoEZzP2YEAanxMTX" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/javascript/javascript.min.js" integrity="sha384-9WXBgfPaWP2zA74XYJo/qmYSaQuy6wy1FyptaVjcRrEdBZagvms/3mIBuPWPqsgg" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.18/mode/yaml/yaml.min.js" integrity="sha384-131Ong53rCgUDHHluJOESiyk8FRC3j7cL7INdfB2n4SVkOoI87gNHb0B23LKlrMp" crossorigin="anonymous"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
"""
        + get_admin_css()
        + """
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
        <a href="/admin/chat" role="tab" style="color:var(--cyan);text-decoration:none;padding:var(--gap-sm) var(--gap-lg);font-size:var(--fs-sm);letter-spacing:0.08em;border-bottom:2px solid transparent;display:flex;align-items:center;gap:4px;white-space:nowrap;">CHAT ↗</a>
      </nav>

"""
        + get_dashboard_html()
        + "\n\n"
        + get_logs_html()
        + "\n\n"
        + get_ratelimits_html()
        + "\n\n"
        + get_files_html()
        + "\n\n"
        + get_skills_html()
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
</body>
</html>"""
    )
