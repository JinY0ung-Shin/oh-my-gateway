"""Chat UI page.

Communicates with /v1/responses via SSE streaming.
Supports multi-turn conversation via previous_response_id chaining,
and AskUserQuestion (function_call / function_call_output) flow.
"""

from functools import lru_cache

from src.theme import (
    theme_head_init,
    theme_tokens_css,
    base_css,
    theme_toggle_html,
    theme_toggle_js,
)


@lru_cache(maxsize=1)
def build_chat_page() -> str:
    """Build the chat UI HTML."""
    return (
        _CHAT_PAGE_TEMPLATE.replace("__OMG_THEME_HEAD_INIT__", theme_head_init())
        .replace("__OMG_THEME_TOKENS_CSS__", theme_tokens_css())
        .replace("__OMG_BASE_CSS__", base_css())
        .replace("__OMG_THEME_TOGGLE_HTML__", theme_toggle_html())
        .replace("__OMG_THEME_TOGGLE_JS__", theme_toggle_js())
    )


_CHAT_PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Oh My Gateway Chat</title>
__OMG_THEME_HEAD_INIT__
<style>
__OMG_THEME_TOKENS_CSS____OMG_BASE_CSS__
/* ================================================================
   Oh My Gateway Chat — page-specific styles
   ================================================================ */

body {
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* === Header === */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.5rem 1rem;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  flex-shrink: 0;
  z-index: 10;
}
.header .left {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.header .title {
  color: var(--text-bright);
  font-size: var(--fs-lg);
  font-weight: 700;
  letter-spacing: -0.01em;
}
.header .session-tag {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-dim);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  padding: 3px 9px;
  background: var(--bg-surface);
}
.header .right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

/* Header buttons: compact variant of the shared .btn */
.header .btn {
  min-height: 30px;
  padding: 4px 11px;
  font-size: var(--fs-xs);
}
.btn-danger:hover {
  color: var(--red);
  border-color: var(--red);
}

/* API key input (mono — it holds a credential token) */
.api-key-input {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  min-height: 30px;
  width: 140px;
  padding: 4px 8px;
}

/* Model select */
.model-select {
  min-height: 30px;
  font-size: var(--fs-xs);
  padding: 4px 26px 4px 8px;
}
.model-select option {
  background: var(--bg);
  color: var(--text);
}

/* === Chat area === */
.chat-container {
  flex: 1;
  overflow-y: auto;
  padding: 1rem;
  scroll-behavior: smooth;
}

/* Messages */
.message {
  margin-bottom: 1rem;
  max-width: 85%;
  animation: fadeIn 0.2s ease;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}

.message.user {
  margin-left: auto;
  text-align: right;
}

.message .role {
  font-size: var(--fs-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 3px;
}
.message.user .role { color: var(--cyan-dim); }
.message.assistant .role { color: var(--accent); }
.message.system .role { color: var(--amber-dim); }

.message .bubble {
  display: inline-block;
  text-align: left;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-raised);
  font-size: var(--fs-sm);
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  max-width: 100%;
}
.message.user .bubble {
  border-color: var(--cyan-muted);
  background: var(--cyan-subtle);
}
.message.assistant .bubble {
  border-color: var(--border);
}
.message.system .bubble {
  border-color: var(--amber-muted);
  background: var(--amber-subtle);
  color: var(--amber-dim);
  font-size: var(--fs-xs);
}
.thinking-panel {
  margin-bottom: 0.4rem;
  border: 1px solid var(--amber-muted);
  border-radius: var(--radius);
  background: var(--amber-subtle);
  overflow: hidden;
}
.thinking-panel[hidden] { display: none; }
.thinking-panel summary {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 4px 8px;
  cursor: pointer;
  color: var(--amber);
  font-size: var(--fs-xs);
  list-style: none;
  text-transform: uppercase;
  letter-spacing: 0;
}
.thinking-panel summary::-webkit-details-marker { display: none; }
.thinking-panel summary::after {
  content: '›';
  margin-left: auto;
  color: var(--amber-dim);
  transition: transform 0.15s;
}
.thinking-panel[open] summary::after { transform: rotate(90deg); }
.thinking-panel[open] summary { border-bottom: 1px solid var(--amber-muted); }
.thinking-meta {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  text-transform: none;
  letter-spacing: 0;
}
.thinking-content {
  padding: 6px 8px;
  color: var(--text);
  font-size: var(--fs-xs);
  white-space: pre-wrap;
  word-break: break-word;
}

/* Streaming cursor */
.bubble .cursor {
  display: inline-block;
  width: 7px;
  height: 14px;
  background: var(--accent);
  animation: blink 0.8s step-end infinite;
  vertical-align: text-bottom;
  margin-left: 2px;
  border-radius: 1px;
}
@keyframes blink {
  50% { opacity: 0; }
}

/* Markdown in assistant (code = mono, body text = sans) */
.bubble code {
  font-family: var(--font-mono);
  background: var(--bg-surface);
  border: 1px solid var(--border-dim);
  padding: 1px 5px;
  border-radius: var(--radius-sm);
  font-size: 0.88em;
}
.bubble pre {
  font-family: var(--font-mono);
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.6rem 0.7rem;
  margin: 0.45rem 0;
  overflow-x: auto;
  font-size: var(--fs-xs);
}
.bubble pre code {
  background: none;
  border: 0;
  padding: 0;
}
.bubble ul, .bubble ol { margin: 0.3rem 0 0.3rem 1.25rem; padding: 0; }
.bubble li { margin: 0.12rem 0; }
.bubble a { color: var(--accent); text-decoration: underline; word-break: break-all; }
.bubble a:hover { color: var(--accent-hover); }
.bubble em { color: var(--text-bright); font-style: italic; }

/* Error bubble — visually distinct from normal assistant output */
.message.assistant .bubble.bubble-error {
  border-color: var(--red-muted);
  background: var(--red-subtle);
  color: var(--red-dim);
}

/* AskUserQuestion prompt */
.ask-prompt {
  margin-bottom: 1rem;
  max-width: 85%;
  animation: fadeIn 0.2s ease;
}
.ask-prompt .role {
  font-size: var(--fs-xs);
  text-transform: uppercase;
  letter-spacing: 0;
  margin-bottom: 2px;
  color: var(--magenta);
}
.ask-prompt .ask-bubble {
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--magenta);
  border-radius: var(--radius);
  background: var(--magenta-subtle);
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-word;
}
.ask-prompt .ask-header {
  font-size: var(--fs-sm);
  color: var(--magenta);
  font-weight: 600;
  margin-bottom: 0.4rem;
}
.ask-prompt .ask-question {
  margin-bottom: 0.65rem;
}
.ask-prompt .ask-options {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin: 0.5rem 0;
}
.ask-prompt .ask-option-btn {
  display: block;
  width: 100%;
  text-align: left;
  font-family: var(--font);
  font-size: var(--fs-sm);
  padding: 8px 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border-bright);
  border-radius: var(--radius-sm);
  color: var(--text);
  cursor: pointer;
  transition: background-color 0.15s, border-color 0.15s, color 0.15s;
}
.ask-prompt .ask-option-btn.multi {
  display: flex;
  gap: 0.5rem;
  align-items: flex-start;
}
.ask-prompt .ask-option-btn:hover {
  border-color: var(--magenta);
  background: var(--magenta-subtle);
}
.ask-prompt .ask-option-btn.selected {
  border-color: var(--magenta);
  background: var(--magenta-subtle);
  color: var(--magenta);
}
.ask-prompt .ask-option-marker {
  flex: 0 0 auto;
  color: var(--magenta);
}
.ask-prompt .ask-option-main {
  min-width: 0;
}
.ask-prompt .ask-option-desc {
  display: block;
  font-size: var(--fs-xs);
  color: var(--text-dim);
  margin-top: 2px;
}
.ask-prompt .ask-input-row {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.5rem;
}
.ask-prompt .ask-input {
  flex: 1;
  font-family: var(--font);
  font-size: var(--fs-sm);
  background: var(--bg);
  color: var(--text-bright);
  border: 1px solid var(--magenta);
  border-radius: var(--radius-sm);
  padding: 6px 10px;
  outline: none;
}
.ask-prompt .ask-input:focus {
  box-shadow: 0 0 0 3px var(--magenta-subtle);
}
.ask-prompt .ask-submit {
  font-family: var(--font);
  font-size: var(--fs-xs);
  font-weight: 600;
  padding: 6px 14px;
  background: var(--magenta-subtle);
  border: 1px solid var(--magenta);
  border-radius: var(--radius-sm);
  color: var(--magenta);
  cursor: pointer;
  transition: background-color 0.15s, color 0.15s;
}
.ask-prompt .ask-submit:hover {
  background: var(--magenta);
  color: var(--bg);
}

/* === Tool events ===
   One card per tool call. The result is merged back into the originating
   card (status pill + RESULT section) rather than a disconnected sibling.
   The left accent border encodes status; the badge color encodes tool type. */
.tool-event {
  margin-bottom: 0.5rem;
  max-width: 85%;
  animation: fadeIn 0.15s ease;
  border-left: 3px solid var(--border-bright);
  border-radius: 8px;
}
/* Only top-level cards are inset from the conversation column; nested cards
   take their indent from the parent's .tool-children padding (no stacking). */
#chat > .tool-event { margin-left: 0.75rem; }
.tool-event[data-status="running"] { border-left-color: var(--amber-dim); }
.tool-event[data-status="done"]    { border-left-color: var(--green-dim); }
.tool-event[data-status="error"]   { border-left-color: var(--red-dim); }
.tool-event.tool-agent { border-left-color: var(--magenta); }
.tool-event details {
  border: 1px solid var(--border);
  border-left: none;
  border-radius: 0 var(--radius) var(--radius) 0;
  background: var(--bg-surface);
  overflow: hidden;
}
.tool-event.tool-agent > details { background: var(--magenta-subtle); }
.tool-event summary {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 4px 8px;
  cursor: pointer;
  font-size: var(--fs-xs);
  color: var(--text-dim);
  list-style: none;
}
.tool-event summary::-webkit-details-marker { display: none; }
.tool-event summary::after {
  content: '›';
  margin-left: 0.25rem;
  color: var(--text-muted);
  font-size: var(--fs-base);
  transition: transform 0.15s;
}
.tool-event details[open] summary::after { transform: rotate(90deg); color: var(--text-dim); }
.tool-event details[open] summary { border-bottom: 1px solid var(--border); }
.tool-event .tool-badge {
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  padding: 1px 7px;
  border: 1px solid;
  border-radius: var(--radius-pill);
  font-weight: 600;
  letter-spacing: 0;
  white-space: nowrap;
}
.tool-event .tool-title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.tool-event .tool-status {
  flex: 0 0 auto;
  font-weight: 700;
  font-size: var(--fs-xs);
}
.tool-status.running { color: var(--amber); animation: pulse 1s ease-in-out infinite; }
.tool-status.done    { color: var(--green); }
.tool-status.error   { color: var(--red); }
/* Per-tool-type badge colors */
.tool-badge.cat-agent   { color: var(--magenta); border-color: var(--magenta); }
.tool-badge.cat-bash    { color: var(--green); border-color: var(--green-dim); }
.tool-badge.cat-read    { color: var(--cyan); border-color: var(--cyan-dim); }
.tool-badge.cat-write,
.tool-badge.cat-edit    { color: var(--amber); border-color: var(--amber-dim); }
.tool-badge.cat-search  { color: var(--cyan-dim); border-color: var(--cyan-dim); }
.tool-badge.cat-web     { color: var(--cyan); border-color: var(--cyan-dim); }
.tool-badge.cat-todo    { color: var(--green-dim); border-color: var(--green-dim); }
.tool-badge.cat-mcp     { color: var(--magenta); border-color: var(--magenta); }
.tool-badge.cat-ask     { color: var(--magenta); border-color: var(--magenta); }
.tool-badge.cat-result  { color: var(--cyan); border-color: var(--cyan-dim); }
.tool-badge.cat-error   { color: var(--red); border-color: var(--red-dim); }
.tool-badge.cat-default { color: var(--amber); border-color: var(--amber-dim); }
.tool-event .tool-body {
  padding: 6px 8px;
  font-size: var(--fs-xs);
  overflow-x: auto;
  max-height: 320px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--border-bright) var(--bg-surface);
}
.tool-event .tool-section { margin-bottom: 6px; }
.tool-event .tool-section:last-child { margin-bottom: 0; }
.tool-event .tool-section-label {
  font-size: 0.62rem;
  letter-spacing: 0;
  text-transform: uppercase;
  color: var(--text-muted);
  margin-bottom: 2px;
}
.tool-event .tool-section-result .tool-section-label { color: var(--cyan-dim); }
.tool-event .tool-section-error .tool-section-label { color: var(--red); }
.tool-event .tool-body pre {
  font-family: var(--font-mono);
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-dim);
  background: none;
  border: 0;
  padding: 0;
}
.tool-event .tool-children { padding-left: 0.85rem; }
.tool-event .tool-children:empty { display: none; }
.tool-event .tool-children > .tool-event:first-child,
.tool-event .tool-children > .task-status-line:first-child { margin-top: 0.4rem; }
.tool-event.tool-child {
  max-width: 100%;
  margin-bottom: 0.4rem;
}
.tool-event.tool-child > details { border-left: none; }

/* Subagent lifecycle: one in-place status line per agent (not stacked cards) */
.task-status-line {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 3px 8px;
  margin: 0.3rem 0;
  font-size: var(--fs-xs);
  color: var(--text-dim);
  border-left: 2px solid var(--border-bright);
  border-radius: 6px;
  background: var(--bg-raised);
  animation: fadeIn 0.15s ease;
}
#chat > .task-status-line { margin-left: 0.75rem; }
.task-status-line[data-state="running"] { border-left-color: var(--amber-dim); color: var(--amber); }
.task-status-line[data-state="done"]    { border-left-color: var(--green-dim); color: var(--green-dim); }
.task-status-line[data-state="error"]   { border-left-color: var(--red-dim); color: var(--red); }
.task-status-line .task-status-glyph { flex: 0 0 auto; font-weight: 700; }
.task-status-line .task-status-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* === Input area === */
.input-area {
  padding: 0.75rem 1rem;
  border-top: 1px solid var(--border);
  background: var(--bg);
  flex-shrink: 0;
  z-index: 10;
}
.input-row {
  display: flex;
  gap: 0.5rem;
  align-items: flex-end;
}
.input-row textarea {
  flex: 1;
  font-family: var(--font);
  font-size: var(--fs-sm);
  background: var(--bg-surface);
  color: var(--text-bright);
  border: 1px solid var(--border-bright);
  border-radius: var(--radius);
  padding: 8px 12px;
  resize: none;
  outline: none;
  min-height: 38px;
  max-height: 200px;
  line-height: 1.5;
  overflow-y: auto;
}
.input-row textarea:focus {
  border-color: var(--accent);
  box-shadow: var(--ring);
}

.send-btn {
  font-family: var(--font);
  font-size: var(--fs-sm);
  font-weight: 600;
  padding: 8px 18px;
  background: var(--accent);
  color: var(--accent-fg);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  cursor: pointer;
  transition: background-color 0.15s, border-color 0.15s;
  white-space: nowrap;
  height: 38px;
}
.send-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.send-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Status bar */
.status-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.25rem 1rem;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  border-top: 1px solid var(--border);
  background: var(--bg);
  flex-shrink: 0;
  z-index: 10;
}
.status-bar .status-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 4px;
  vertical-align: middle;
}
.status-dot.idle { background: var(--green); }
.status-dot.streaming { background: var(--amber); animation: pulse 1s ease-in-out infinite; }
.status-dot.error { background: var(--red); }
@keyframes pulse { 50% { opacity: 0.4; } }
.status-bar #token-info {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

/* Welcome */
.welcome {
  text-align: center;
  padding: 3rem 1rem;
  color: var(--text-dim);
}
.welcome .welcome-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 42px;
  min-height: 42px;
  margin-bottom: 1rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--accent);
  font-weight: 700;
}
.welcome p {
  font-size: var(--fs-sm);
  margin-bottom: 0.25rem;
}
.welcome .hint {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

/* === Auth Overlay === */
.auth-overlay {
  position: fixed;
  inset: 0;
  background: rgba(10, 10, 11, 0.5);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10001;
  padding: 1rem;
}
.auth-overlay.hidden { display: none; }
.auth-box {
  width: min(420px, 100%);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 1.5rem;
  box-shadow: var(--shadow-lg);
}
.auth-box h2 {
  color: var(--text-bright);
  font-size: var(--fs-lg);
  margin-bottom: 0.5rem;
}
.auth-box .auth-prompt {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  margin-bottom: 1rem;
}
.auth-box .auth-label {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  margin-bottom: 4px;
  display: block;
}
.auth-box .auth-input {
  width: 100%;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  margin-bottom: 0.75rem;
}
.auth-box .auth-submit {
  width: 100%;
  font-family: var(--font);
  font-size: var(--fs-sm);
  font-weight: 600;
  padding: 8px 14px;
  background: var(--accent);
  color: var(--accent-fg);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.auth-box .auth-submit:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
.auth-box .auth-error {
  color: var(--red);
  font-size: var(--fs-xs);
  margin-top: 0.5rem;
  min-height: 1em;
}

/* Send button active */
.send-btn:active:not(:disabled) { transform: scale(0.98); background: var(--accent-hover); }

/* Responsive */
@media (max-width: 640px) {
  .message { max-width: 95%; }
  .header .title { font-size: var(--fs-base); }
  .header { flex-wrap: wrap; gap: var(--gap-sm); padding: 0.4rem var(--gap-md); }
  .header .right { width: 100%; justify-content: flex-end; overflow-x: auto; flex-wrap: nowrap; }
  .api-key-input { width: 100px; }
  .input-area { padding: var(--gap-sm); }
  .ask-prompt .ask-input-row { flex-direction: column; }
  .ask-prompt .ask-option-btn { padding: 10px 12px; }
  .tool-event { max-width: 100%; }
  #chat > .tool-event, #chat > .task-status-line { margin-left: 0; }
  .tool-event .tool-children { padding-left: 0.6rem; }
}
</style>
</head>
<body>

<!-- Auth Overlay (admin login gate — hidden once authenticated) -->
<div class="auth-overlay" id="auth-overlay" role="dialog" aria-modal="true" aria-labelledby="auth-title">
  <div class="auth-box">
    <h2 id="auth-title">Admin Chat Access</h2>
    <p class="auth-prompt">Use the configured ADMIN_API_KEY to continue.</p>
    <form id="auth-form">
      <label class="auth-label" for="auth-key">Admin API key</label>
      <input type="password" id="auth-key" class="auth-input" placeholder="••••••••••••••••" autocomplete="current-password" required>
      <button type="submit" class="auth-submit">Sign in</button>
      <p class="auth-error" id="auth-error" aria-live="polite"></p>
    </form>
  </div>
</div>

<!-- Header -->
<div class="header">
  <div class="left">
    <span class="title">GATEWAY CHAT</span>
    <span class="session-tag" id="session-tag">No session</span>
  </div>
  <div class="right">
    <input type="password" class="api-key-input" id="api-key" placeholder="API key" title="Bearer token for /v1/responses" aria-label="Responses API key">
    <select class="model-select" id="model-select" aria-label="Model">
      <option value="sonnet">sonnet</option>
      <option value="opus">opus</option>
      <option value="haiku">haiku</option>
    </select>
    <button class="btn" onclick="newSession()" title="New session">New</button>
    <a class="btn" href="/admin">Admin</a>
    <a class="btn" href="/">Home</a>
    __OMG_THEME_TOGGLE_HTML__
  </div>
</div>

<!-- Chat -->
<div class="chat-container" id="chat" role="log" aria-label="Chat messages" aria-live="polite">
<div class="welcome" id="welcome">
<div class="welcome-mark">AI</div>
<p>Oh My Gateway Chat</p>
<p class="hint">New conversation</p>
</div>
</div>

<!-- Input -->
<div class="input-area">
  <div class="input-row">
    <textarea id="input" rows="1" placeholder="Message..." autofocus aria-label="Message"></textarea>
    <button class="send-btn" id="send-btn" onclick="sendMessage()" aria-label="Send message">Send</button>
  </div>
</div>

<!-- Status Bar -->
<div class="status-bar">
  <span><span class="status-dot idle" id="status-dot"></span><span id="status-text">Idle</span></span>
  <span id="token-info"></span>
</div>

<script>
// ================================================================
// Chat Engine — /v1/responses SSE streaming client
// ================================================================

const API_BASE = window.location.origin;
const apiKeyEl = document.getElementById('api-key');

function getHeaders() {
  const h = { 'Content-Type': 'application/json' };
  const key = apiKeyEl.value.trim();
  if (key) h['Authorization'] = 'Bearer ' + key;
  return h;
}

let previousResponseId = null;
let sessionId = null;
let isStreaming = false;
let currentAbortController = null;
let pendingAsk = null;
// tool_use_id -> tool-event card. Lets a tool_result merge back into the card
// that invoked it, and a subagent's calls nest under the agent that spawned them.
let toolEventsById = {};
// task_id -> the single live status line for that subagent (updated in place)
let taskStatusById = {};
// Resilience to out-of-order SSE: nodes whose parent agent card isn't registered
// yet are parked at top level and re-homed once the agent card appears.
let pendingChildrenByParent = {};   // parentToolUseId -> [orphaned child nodes]
let pendingResultsByToolUseId = {}; // toolUseId -> {content, isError, card} awaiting its tool_use
let orphanSeq = 0;                  // unique fallback key for task lines lacking ids
let lastIdlessTaskKey = null;       // groups id-less task lifecycle events together

const chatEl = document.getElementById('chat');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const tokenInfo = document.getElementById('token-info');
const sessionTag = document.getElementById('session-tag');
const welcomeEl = document.getElementById('welcome');
const modelSelect = document.getElementById('model-select');

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
});
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

function setStatus(state, text) {
  statusDot.className = 'status-dot ' + state;
  statusText.textContent = text;
}
function setStreaming(v) {
  isStreaming = v;
  sendBtn.disabled = v;
  inputEl.disabled = v;
  setStatus(v ? 'streaming' : 'idle', v ? 'Streaming...' : 'Idle');
}
function updateSessionTag() {
  sessionTag.textContent = sessionId ? sessionId.substring(0, 12) + '...' : 'No session';
  sessionTag.title = sessionId || '';
}
function newSession() {
  if (chatEl.querySelectorAll('.message').length > 0) {
    if (!confirm('This will clear the current conversation. Start a new session?')) return;
  }
  previousResponseId = null; sessionId = null; pendingAsk = null;
  toolEventsById = {};
  taskStatusById = {};
  pendingChildrenByParent = {};
  pendingResultsByToolUseId = {};
  orphanSeq = 0;
  lastIdlessTaskKey = null;
  updateSessionTag();
  chatEl.innerHTML = '';
  chatEl.appendChild(welcomeEl);
  welcomeEl.style.display = '';
  tokenInfo.textContent = '';
  setStatus('idle', 'Idle');
  inputEl.focus();
}
function isNearBottom() {
  return chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 100;
}
function scrollToBottom(force) {
  if (force || isNearBottom()) chatEl.scrollTop = chatEl.scrollHeight;
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}
function escapeAttr(text) {
  return String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
// U+E000 (private-use) delimits stashed code blocks. escapeHtml leaves it
// untouched and it cannot appear in normal text, so it can't collide with
// user/model content the way an ASCII sentinel could.
const CB_SENTINEL = '\uE000';
function renderMarkdown(text) {
  let html = escapeHtml(text);
  // Fenced code blocks first (tolerate a missing newline after the fence),
  // stashed as placeholders so inline rules don't touch their contents.
  const codeBlocks = [];
  html = html.replace(/```(\w*)\r?\n?([\s\S]*?)```/g, (m, lang, code) => {
    const cls = lang ? ' class="language-' + lang + '"' : '';
    codeBlocks.push('<pre><code' + cls + '>' + code.replace(/\n$/, '') + '</code></pre>');
    return CB_SENTINEL + (codeBlocks.length - 1) + CB_SENTINEL;
  });
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Links: only http(s), and the URL goes into an href attribute, so quotes
  // (which escapeHtml does NOT escape) must be neutralized to prevent
  // attribute-injection. The link text is element content and stays escaped.
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)<>"']+)\)/g, (m, txt, url) =>
    '<a href="' + url.replace(/"/g, '%22').replace(/'/g, '%27') +
    '" target="_blank" rel="noopener noreferrer">' + txt + '</a>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic: require the asterisks to hug non-space content and sit at word
  // boundaries, so globs (*.py), math (2 * 3) and a*b are left alone.
  html = html.replace(/(^|[\s(])\*(\S(?:[^*\n]*?\S)?)\*(?=[\s).,!?;:]|$)/g, '$1<em>$2</em>');
  html = renderMarkdownLists(html);
  // List markup is block-level and carries its own margins; the source
  // newlines hugging those tags would render as extra blank lines under
  // `white-space: pre-wrap`. Strip them, then clamp any run of blank lines
  // to a single one so paragraphs don't spread out.
  html = html
    .replace(/\n+(?=<\/?(?:ul|ol|li)\b)/g, '')
    .replace(/(<\/?(?:ul|ol|li)\b[^>]*>)\n+/g, '$1')
    .replace(/\n{3,}/g, '\n\n');
  // Restore code blocks; leave a stray sentinel-shaped literal untouched.
  const restore = new RegExp(CB_SENTINEL + '(\\d+)' + CB_SENTINEL, 'g');
  html = html.replace(restore, (m, i) => codeBlocks[+i] !== undefined ? codeBlocks[+i] : m);
  // <pre> is block-level too; trim the newlines touching its wrapper so the
  // code block doesn't pick up a blank line above/below from pre-wrap.
  html = html.replace(/\n+(<pre[\s>])/g, '$1').replace(/(<\/pre>)\n+/g, '$1');
  return html;
}
// Group consecutive -/*/+ or 1. lines into <ul>/<ol>; leave other lines intact.
function renderMarkdownLists(html) {
  const lines = html.split('\n');
  const out = [];
  let listType = null;
  const closeList = () => { if (listType) { out.push('</' + listType + '>'); listType = null; } };
  for (const line of lines) {
    const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (ul) {
      if (listType !== 'ul') { closeList(); out.push('<ul>'); listType = 'ul'; }
      out.push('<li>' + ul[1] + '</li>');
    } else if (ol) {
      if (listType !== 'ol') { closeList(); out.push('<ol>'); listType = 'ol'; }
      out.push('<li>' + ol[1] + '</li>');
    } else {
      closeList();
      out.push(line);
    }
  }
  closeList();
  return out.join('\n');
}

// --- UI builders ---

function addMessage(role, text) {
  welcomeEl.style.display = 'none';
  const div = document.createElement('div');
  div.className = 'message ' + role;
  div.innerHTML = '<div class="role">' + escapeHtml(role) + '</div><div class="bubble">' + escapeHtml(text) + '</div>';
  chatEl.appendChild(div);
  scrollToBottom();
  return div;
}

function addStreamingMessage() {
  welcomeEl.style.display = 'none';
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML =
    '<div class="role">assistant</div>' +
    '<details class="thinking-panel" open hidden>' +
    '<summary><span>THINKING</span><span class="thinking-meta"></span></summary>' +
    '<div class="thinking-content"></div>' +
    '</details>' +
    '<div class="bubble"><span class="cursor"></span></div>';
  chatEl.appendChild(div);
  scrollToBottom();
  return div.querySelector('.bubble');
}

function updateThinkingPanel(messageEl, text) {
  if (!messageEl) return;
  const panel = messageEl.querySelector('.thinking-panel');
  const content = messageEl.querySelector('.thinking-content');
  const meta = messageEl.querySelector('.thinking-meta');
  if (!panel || !content) return;
  panel.hidden = false;
  content.textContent = text || '(empty)';
  if (meta) {
    const n = String(text || '').length;
    meta.textContent = n ? '~' + Math.max(1, Math.round(n / 5)) + ' words' : '';
    meta.title = n + ' characters';
  }
  scrollToBottom();
}

function extractReasoningTexts(response) {
  const out = [];
  const items = response && Array.isArray(response.output) ? response.output : [];
  for (const item of items) {
    if (!item || item.type !== 'reasoning') continue;
    const parts = [];
    if (Array.isArray(item.content)) {
      for (const part of item.content) {
        if (part && typeof part.text === 'string' && part.text) parts.push(part.text);
      }
    }
    if (parts.length === 0 && Array.isArray(item.summary)) {
      for (const part of item.summary) {
        if (part && typeof part.text === 'string' && part.text) parts.push(part.text);
      }
    }
    const text = parts.join('\n');
    if (text) out.push(text);
  }
  return out;
}

function statusGlyph(status) {
  if (status === 'done') return '✓';   // ✓
  if (status === 'error') return '✗';  // ✗
  if (status === 'running') return '⋯'; // ⋯
  return '';
}

// Map a tool name to a display category {cls, glyph, label} so each kind of
// tool — and especially the Task/Agent tool — is visually distinguishable.
function toolMeta(name) {
  const raw = name || 'tool';
  const n = raw.toLowerCase();
  if (n === 'task' || n === 'agent') return { cls: 'cat-agent', glyph: '✦', label: raw };
  if (n.indexOf('mcp__') === 0) return { cls: 'cat-mcp', glyph: '⚙', label: raw };
  if (n === 'bash' || n === 'bashoutput' || n === 'killshell' || n === 'killbash') return { cls: 'cat-bash', glyph: '$', label: raw };
  if (n === 'read' || n === 'notebookread') return { cls: 'cat-read', glyph: '▤', label: raw };
  if (n === 'write') return { cls: 'cat-write', glyph: '✎', label: raw };
  if (n === 'edit' || n === 'multiedit' || n === 'notebookedit') return { cls: 'cat-edit', glyph: '✎', label: raw };
  if (n === 'grep' || n === 'glob' || n === 'ls') return { cls: 'cat-search', glyph: '⌕', label: raw };
  if (n === 'webfetch' || n === 'websearch') return { cls: 'cat-web', glyph: '◎', label: raw };
  if (n === 'todowrite' || n === 'todoread') return { cls: 'cat-todo', glyph: '☑', label: raw };
  if (n === 'askuserquestion') return { cls: 'cat-ask', glyph: '?', label: raw };
  return { cls: 'cat-default', glyph: '▸', label: raw };
}

// One-line summary of a tool's input, favoring its most telling argument.
function summarizeInput(input) {
  if (input === null || typeof input !== 'object') {
    return String(input == null ? '' : input).slice(0, 140);
  }
  const primary = input.command || input.file_path || input.path || input.pattern ||
    input.query || input.url || input.description || input.prompt || input.notebook_path;
  let s;
  if (primary) {
    s = String(primary);
  } else {
    const entries = Object.entries(input);
    if (!entries.length) return '';
    s = entries.map(([k, v]) => k + ': ' + (typeof v === 'string' ? v : JSON.stringify(v))).join(', ');
  }
  return s.replace(/\s+/g, ' ').trim().slice(0, 140);
}

// Find the .tool-children container of a registered tool card (and open it).
function childContainerFor(parentToolUseId) {
  if (!parentToolUseId || !toolEventsById[parentToolUseId]) return null;
  const details = toolEventsById[parentToolUseId].querySelector(':scope > details');
  const children = details && details.querySelector(':scope > .tool-children');
  if (children) { details.open = true; return children; }
  return null;
}

// When an agent card finally registers, pull in any child nodes (nested tool
// cards, task status lines) that arrived earlier and were parked at top level.
function adoptPendingChildren(parentToolUseId, card) {
  const orphans = pendingChildrenByParent[parentToolUseId];
  if (!orphans || !orphans.length) return;
  const details = card.querySelector(':scope > details');
  const children = details && details.querySelector(':scope > .tool-children');
  if (children) {
    details.open = true;
    for (const node of orphans) children.appendChild(node); // moves out of #chat
  }
  delete pendingChildrenByParent[parentToolUseId];
}

// Build a tool-event card. Used for tool_use, and as a fallback for an
// orphaned tool_result/failure that has no card to merge into.
function createToolCard(o) {
  welcomeEl.style.display = 'none';
  const container = childContainerFor(o.parentToolUseId) || chatEl;
  const div = document.createElement('div');
  div.className = 'tool-event ' + (o.badgeCls || 'cat-default') +
    (container !== chatEl ? ' tool-child' : '') + (o.isAgent ? ' tool-agent' : '');
  div.dataset.status = o.status || '';
  const bodyLabel = o.bodyLabel || 'INPUT';
  const sectionCls = bodyLabel === 'ERROR' ? 'tool-section-error'
    : (bodyLabel === 'RESULT' ? 'tool-section-result' : '');
  const body = o.body
    ? '<div class="tool-section ' + sectionCls + '"><div class="tool-section-label">' + bodyLabel +
      '</div><pre>' + escapeHtml(o.body) + '</pre></div>'
    : '';
  div.innerHTML =
    '<details' + (o.isAgent ? ' open' : '') + '>' +
      '<summary aria-label="' + escapeAttr((o.badgeLabel || '') + ' ' + (o.title || '')) + '">' +
        '<span class="tool-badge ' + (o.badgeCls || 'cat-default') + '">' +
          escapeHtml((o.glyph ? o.glyph + ' ' : '') + (o.badgeLabel || '')) + '</span>' +
        '<span class="tool-title">' + escapeHtml(o.title || '') + '</span>' +
        '<span class="tool-status ' + (o.status || '') + '">' + statusGlyph(o.status) + '</span>' +
      '</summary>' +
      '<div class="tool-body">' + body + '</div>' +
      '<div class="tool-children"></div>' +
    '</details>';
  container.appendChild(div);
  // If this card couldn't nest yet (parent not registered), park it so the
  // agent card can adopt it later. Result-fallback cards opt out (o.noReparent).
  if (container === chatEl && o.parentToolUseId && !o.noReparent) {
    (pendingChildrenByParent[o.parentToolUseId] = pendingChildrenByParent[o.parentToolUseId] || []).push(div);
  }
  if (o.toolUseId) {
    toolEventsById[o.toolUseId] = div;
    adoptPendingChildren(o.toolUseId, div);
    const pr = pendingResultsByToolUseId[o.toolUseId];
    if (pr) {
      if (pr.card && pr.card.parentNode) pr.card.parentNode.removeChild(pr.card);
      attachToolResult(div, pr.content, pr.isError);
      delete pendingResultsByToolUseId[o.toolUseId];
    }
  }
  scrollToBottom();
  return div;
}

// Render a tool_use SSE event as a card (status starts as "running").
function renderToolUse(evt) {
  const meta = toolMeta(evt.name);
  const input = evt.input || {};
  const isAgent = meta.cls === 'cat-agent';
  let title;
  if (isAgent) {
    const who = input.subagent_type ? '@' + input.subagent_type : '';
    title = [who, input.description || summarizeInput(input)].filter(Boolean).join('  ');
  } else {
    title = summarizeInput(input);
  }
  return createToolCard({
    badgeCls: meta.cls,
    glyph: meta.glyph,
    badgeLabel: meta.label,
    title: title,
    body: JSON.stringify(input, null, 2),
    bodyLabel: 'INPUT',
    status: 'running',
    toolUseId: evt.tool_use_id,
    parentToolUseId: evt.parent_tool_use_id,
    isAgent: isAgent,
  });
}

// Merge a tool_result into the card of the tool_use it answers, flipping the
// card's status pill and appending a RESULT/ERROR section to its body.
function attachToolResult(card, content, isError) {
  const details = card.querySelector(':scope > details');
  const body = details && details.querySelector(':scope > .tool-body');
  if (body) {
    const sec = document.createElement('div');
    sec.className = 'tool-section ' + (isError ? 'tool-section-error' : 'tool-section-result');
    sec.innerHTML = '<div class="tool-section-label">' + (isError ? 'ERROR' : 'RESULT') +
      '</div><pre>' + escapeHtml(content || '(no content)') + '</pre>';
    body.appendChild(sec);
  }
  const status = isError ? 'error' : 'done';
  card.dataset.status = status;
  const pill = details && details.querySelector(':scope > summary > .tool-status');
  if (pill) { pill.className = 'tool-status ' + status; pill.textContent = statusGlyph(status); }
  scrollToBottom();
}

// Maintain ONE live status line per subagent, nested under the spawning agent,
// updated in place across task_started / task_progress / task_notification.
function upsertTaskStatus(evt) {
  const taskType = String(evt.type || '').slice('response.'.length);
  const parentId = evt.parent_tool_use_id || evt.tool_use_id;
  let text, state;
  if (taskType === 'task_started') {
    text = 'started' + (evt.description ? ' · ' + evt.description : '');
    state = 'running';
  } else if (taskType === 'task_progress') {
    const bits = [evt.last_tool_name, evt.description].filter(Boolean);
    text = 'running' + (bits.length ? ' · ' + bits.join(' · ') : '');
    state = 'running';
  } else if (taskType === 'task_notification') {
    state = evt.status === 'failed' ? 'error' : 'done';
    text = (evt.status || 'done') + (evt.summary ? ' · ' + evt.summary : '');
  } else {
    return;
  }
  // Group a subagent's lifecycle by a STABLE id. task_id and parentId are both
  // empty only for truly id-less tasks (which the SDK does not emit in
  // practice); those fall back to a fresh key per started event so distinct
  // subagents never collide, while later progress/notification reuse the last.
  let key = evt.task_id || parentId;
  if (!key) {
    if (taskType === 'task_started' || !lastIdlessTaskKey) lastIdlessTaskKey = 'task#' + (orphanSeq++);
    key = lastIdlessTaskKey;
  }
  let line = taskStatusById[key];
  if (!line) {
    line = document.createElement('div');
    line.className = 'task-status-line';
    const children = childContainerFor(parentId);
    if (children) {
      children.insertBefore(line, children.firstChild); // pin above nested tool cards
    } else {
      welcomeEl.style.display = 'none';
      chatEl.appendChild(line);
      // Park under the agent so it can be re-homed once the agent card arrives.
      if (parentId) (pendingChildrenByParent[parentId] = pendingChildrenByParent[parentId] || []).push(line);
    }
    taskStatusById[key] = line;
  }
  line.dataset.state = state;
  line.innerHTML = '<span class="task-status-glyph">' + statusGlyph(state) + '</span>' +
    '<span class="task-status-text">' + escapeHtml(text) + '</span>';
  scrollToBottom();
}

// --- AskUserQuestion ---

function showAskPrompt(argsObj, callId, responseId) {
  welcomeEl.style.display = 'none';
  pendingAsk = { call_id: callId, response_id: responseId };

  const div = document.createElement('div');
  div.className = 'ask-prompt';
  div.id = 'ask-prompt-' + callId;

  // Parse structured questions with options
  let questions = argsObj.questions;
  if ((!Array.isArray(questions) || questions.length === 0) && argsObj.question && Array.isArray(argsObj.options)) {
    questions = [argsObj];
  }
  if (questions && Array.isArray(questions) && questions.length > 0) {
    // Structured format: { questions: [{ question, header, options }] }
    let html = '<div class="role">AskUserQuestion</div>';
    for (let i = 0; i < questions.length; i++) {
      const q = questions[i];
      const multiple = q.multiple === true;
      html += '<div class="ask-question" data-index="' + i + '" data-multiple="' + (multiple ? 'true' : 'false') + '">';
      if (q.header) html += '<div class="ask-header">' + escapeHtml(q.header) + '</div>';
      if (q.question) html += '<div class="ask-bubble">' + escapeHtml(q.question) + '</div>';
      if (q.options && Array.isArray(q.options)) {
        html += '<div class="ask-options">';
        for (const opt of q.options) {
          const isObjectOption = typeof opt === 'object' && opt !== null;
          const label = typeof opt === 'string' ? opt : (isObjectOption ? (opt.label || '') : '');
          const desc = isObjectOption ? (opt.description || '') : '';
          if (!label) continue;
          html += '<button type="button" class="ask-option-btn' + (multiple ? ' multi' : '') + '" data-label="' + escapeAttr(label) + '" aria-pressed="false">' +
            (multiple ? '<span class="ask-option-marker" aria-hidden="true">[ ]</span>' : '') +
            '<span class="ask-option-main"><span class="ask-option-label">' + escapeHtml(label) + '</span>' +
            (desc ? '<span class="ask-option-desc">' + escapeHtml(desc) + '</span>' : '') +
            '</span>' +
            '</button>';
        }
        html += '</div>';
      }
      html += '</div>';
    }
    html += '<div class="ask-input-row">' +
      '<input type="text" class="ask-input" placeholder="직접 입력..." aria-label="Answer">' +
      '<button class="ask-submit">REPLY</button></div>';
    div.innerHTML = html;
  } else {
    // Simple text format
    const question = argsObj.question || argsObj.text || JSON.stringify(argsObj);
    div.innerHTML =
      '<div class="role">AskUserQuestion</div>' +
      '<div class="ask-bubble">' + escapeHtml(question) + '</div>' +
      '<div class="ask-input-row">' +
      '<input type="text" class="ask-input" placeholder="응답 입력..." aria-label="Answer">' +
      '<button class="ask-submit">REPLY</button></div>';
  }

  chatEl.appendChild(div);
  scrollToBottom();

  // Option button click -> update selected answers
  div.querySelectorAll('.ask-option-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const question = btn.closest('.ask-question');
      const multiple = question && question.dataset.multiple === 'true';
      if (multiple) {
        btn.classList.toggle('selected');
        const selected = btn.classList.contains('selected');
        btn.setAttribute('aria-pressed', selected ? 'true' : 'false');
        const marker = btn.querySelector('.ask-option-marker');
        if (marker) marker.textContent = selected ? '[x]' : '[ ]';
      } else if (question) {
        question.querySelectorAll('.ask-option-btn').forEach(b => {
          b.classList.remove('selected');
          b.setAttribute('aria-pressed', 'false');
        });
        btn.classList.add('selected');
        btn.setAttribute('aria-pressed', 'true');
      }
      syncAskInputPreview(div);
    });
  });

  // Submit
  const submitBtn = div.querySelector('.ask-submit');
  const askInput = div.querySelector('.ask-input');
  submitBtn.addEventListener('click', () => doSubmitAsk(div));
  askInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doSubmitAsk(div); }
  });
  askInput.focus();
}

function selectedLabelsForQuestion(questionEl) {
  return Array.from(questionEl.querySelectorAll('.ask-option-btn.selected'))
    .map(btn => btn.dataset.label || '')
    .filter(Boolean);
}

function syncAskInputPreview(container) {
  const input = container.querySelector('.ask-input');
  if (!input) return;
  const questions = Array.from(container.querySelectorAll('.ask-question'));
  if (!questions.length) return;
  const labels = questions.flatMap(selectedLabelsForQuestion);
  input.value = labels.join(', ');
}

function collectAskAnswer(container) {
  const input = container.querySelector('.ask-input');
  const typed = input ? input.value.trim() : '';
  const questions = Array.from(container.querySelectorAll('.ask-question'));
  if (!questions.length) return { payload: typed, display: typed };

  const answersByQuestion = questions.map(selectedLabelsForQuestion);
  const hasSelected = answersByQuestion.some(answers => answers.length > 0);
  if (!hasSelected) return { payload: typed, display: typed };

  const display = answersByQuestion
    .map(answers => answers.join(', '))
    .filter(Boolean)
    .join(' / ');
  if (questions.length === 1) {
    const multiple = questions[0].dataset.multiple === 'true';
    const answers = answersByQuestion[0];
    return {
      payload: multiple ? JSON.stringify(answers) : answers[0],
      display,
    };
  }
  return { payload: JSON.stringify(answersByQuestion), display };
}

async function doSubmitAsk(container) {
  if (!pendingAsk) return;
  const input = container.querySelector('.ask-input');
  const answer = collectAskAnswer(container);
  if (!answer.payload) return;

  const { call_id, response_id } = pendingAsk;
  pendingAsk = null;

  // Disable UI
  input.disabled = true;
  container.querySelector('.ask-submit').disabled = true;
  container.querySelector('.ask-submit').textContent = 'SENT';
  container.querySelectorAll('.ask-option-btn').forEach(b => { b.disabled = true; });

  addMessage('user', answer.display || answer.payload);

  await streamRequest({
    model: modelSelect.value,
    input: [{ type: 'function_call_output', call_id, output: answer.payload }],
    previous_response_id: response_id,
    stream: true,
  });
}

// --- Main send/stream ---

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || isStreaming) return;
  inputEl.value = '';
  inputEl.style.height = 'auto';
  addMessage('user', text);

  const body = { model: modelSelect.value, input: text, stream: true };
  if (previousResponseId) body.previous_response_id = previousResponseId;
  await streamRequest(body);
}

async function streamRequest(body) {
  setStreaming(true);
  let activeBubble = null;
  let activeBubbleText = '';
  let thinkingMessageEl = null;
  let fullText = '';
  let reasoningSummaryText = '';
  let reasoningText = '';
  let reasoningTextDeltaSeen = false;
  let responseId = null;

  function messageHasThinking(messageEl) {
    const panel = messageEl && messageEl.querySelector('.thinking-panel');
    return !!(panel && !panel.hidden);
  }

  function ensureActiveBubble() {
    if (!activeBubble) {
      activeBubble = addStreamingMessage();
      activeBubbleText = '';
      if (!thinkingMessageEl || !thinkingMessageEl.isConnected) {
        thinkingMessageEl = activeBubble.closest('.message');
      }
    }
    return activeBubble;
  }

  function ensureThinkingMessageEl() {
    if (thinkingMessageEl && thinkingMessageEl.isConnected) return thinkingMessageEl;
    return ensureActiveBubble().closest('.message');
  }

  function finalizeActiveBubble() {
    if (!activeBubble) return;
    const messageEl = activeBubble.closest('.message');
    if (!messageEl) {
      activeBubble = null;
      activeBubbleText = '';
      return;
    }
    if (activeBubbleText) {
      activeBubble.innerHTML = renderMarkdown(activeBubbleText);
    } else if (messageHasThinking(messageEl)) {
      activeBubble.remove();
    } else {
      messageEl.remove();
    }
    activeBubble = null;
    activeBubbleText = '';
  }

  try {
    currentAbortController = new AbortController();
    const resp = await fetch(API_BASE + '/v1/responses', {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify(body),
      signal: currentAbortController.signal,
    });

    if (!resp.ok) {
      const err = await resp.text();
      const bubble = ensureActiveBubble();
      bubble.classList.add('bubble-error');
      bubble.innerHTML = renderMarkdown('Error ' + resp.status + ' — ' + err);
      activeBubble = null; activeBubbleText = '';
      setStatus('error', '오류 발생');
      setStreaming(false);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6);
        if (raw === '[DONE]') continue;

        let evt;
        try { evt = JSON.parse(raw); } catch { continue; }
        const type = evt.type;

        // --- response.created: extract IDs ---
        if (type === 'response.created' && evt.response) {
          responseId = evt.response.id;
          if (responseId) {
            const parts = responseId.split('-');
            if (parts.length >= 2) {
              const sid = parts.slice(1, -1).join('-');
              if (sid && sid !== sessionId) { sessionId = sid; updateSessionTag(); }
            }
          }
        }

        // --- Text delta ---
        if (type === 'response.output_text.delta' && evt.delta) {
          const bubble = ensureActiveBubble();
          fullText += evt.delta;
          activeBubbleText += evt.delta;
          bubble.innerHTML = renderMarkdown(activeBubbleText) + '<span class="cursor"></span>';
          scrollToBottom();
        }

        // --- Reasoning / thinking delta ---
        if (type === 'response.reasoning_summary_text.delta' && evt.delta && !reasoningTextDeltaSeen) {
          reasoningSummaryText += evt.delta;
          updateThinkingPanel(ensureThinkingMessageEl(), reasoningSummaryText);
        }
        if (type === 'response.reasoning_text.delta' && evt.delta) {
          if (!reasoningTextDeltaSeen) {
            reasoningTextDeltaSeen = true;
            reasoningText = '';
          }
          reasoningText += evt.delta;
          updateThinkingPanel(ensureThinkingMessageEl(), reasoningText);
        }

        // --- Tool use: one card per call, status starts "running" ---
        if (type === 'response.tool_use') {
          finalizeActiveBubble();
          const meta = toolMeta(evt.name);
          setStatus('streaming', meta.glyph + ' ' + (evt.name || 'tool'));
          renderToolUse(evt);
        }

        // --- Tool result: merge back into the card of the call it answers ---
        if (type === 'response.tool_result') {
          const isError = !!evt.is_error;
          const content = typeof evt.content === 'string' ? evt.content : JSON.stringify(evt.content, null, 2);
          const card = evt.tool_use_id ? toolEventsById[evt.tool_use_id] : null;
          if (card) {
            attachToolResult(card, content, isError);
          } else {
            // Orphan result (call suppressed or arrived out of order) → standalone
            // card now (never lose a result), but remember it so that if its
            // tool_use card shows up later the standalone is replaced by a merge.
            finalizeActiveBubble();
            const preview = (content || '').substring(0, 100).replace(/\s+/g, ' ');
            const orphan = createToolCard({
              badgeCls: isError ? 'cat-error' : 'cat-result',
              glyph: statusGlyph(isError ? 'error' : 'done'),
              badgeLabel: isError ? 'ERROR' : 'RESULT',
              title: preview || '(empty)',
              body: content || '(no content)',
              bodyLabel: isError ? 'ERROR' : 'RESULT',
              status: isError ? 'error' : 'done',
              parentToolUseId: evt.parent_tool_use_id,
              noReparent: true,
            });
            if (evt.tool_use_id) {
              pendingResultsByToolUseId[evt.tool_use_id] = { content: content, isError: isError, card: orphan };
            }
          }
          setStatus('streaming', '스트리밍중...');
        }

        // --- Task events: one in-place status line per subagent ---
        if (typeof type === 'string' && type.startsWith('response.task')) {
          upsertTaskStatus(evt);
          const label = type.slice('response.'.length).replace('task_', '');
          setStatus('streaming', 'agent: ' + label);
        }

        // --- Liveness: tool call starting (before its arguments finish) ---
        if (type === 'response.tool_use_started') {
          const meta = toolMeta(evt.name);
          setStatus('streaming', meta.glyph + ' 준비 중: ' + (evt.name || 'tool'));
        }

        // --- Liveness: hook lifecycle (PreToolUse/PostToolUse/…) ---
        if (type === 'response.hook_event') {
          const hookName = evt.hook_event_name || 'hook';
          const toolPart = evt.tool_name ? (' · ' + evt.tool_name) : '';
          const phase = evt.phase === 'hook_response' ? '완료' : '실행';
          setStatus('streaming', '🪝 ' + hookName + toolPart + ' ' + phase);
        }

        // --- Liveness: context compaction in progress ---
        if (type === 'response.compaction') {
          setStatus('streaming', '🗜️ 컨텍스트 압축 중…');
        }

        // --- function_call (AskUserQuestion) ---
        if (type === 'response.output_item.added' && evt.item && evt.item.type === 'function_call') {
          // Will be handled in response.completed
        }

        // --- response.completed / requires_action ---
        if ((type === 'response.completed' || type === 'response.output_item.done') && evt.response) {
          const r = evt.response;
          if (r.status === 'requires_action' && r.output) {
            for (const item of r.output) {
              if (item.type === 'function_call' && item.name === 'AskUserQuestion') {
                let args = {};
                try { args = JSON.parse(item.arguments); } catch {}
                finalizeActiveBubble();
                showAskPrompt(args, item.call_id, r.id);
              }
            }
          }
        }

        // --- response.completed ---
        if (type === 'response.completed' && evt.response) {
          if (evt.response.id) previousResponseId = evt.response.id;
          const completedReasoning = extractReasoningTexts(evt.response);
          if (completedReasoning.length && !reasoningText && !reasoningSummaryText) {
            updateThinkingPanel(ensureThinkingMessageEl(), completedReasoning.join('\n\n'));
          }
          if (evt.response.usage) {
            const u = evt.response.usage;
            const tin = u.input_tokens || 0, tout = u.output_tokens || 0;
            tokenInfo.textContent = 'tokens · in ' + tin + ' · out ' + tout;
            tokenInfo.title = tin + ' input tokens, ' + tout + ' output tokens';
          }
        }

        // --- response.failed ---
        if (type === 'response.failed' && evt.response && evt.response.error) {
          const e = evt.response.error;
          finalizeActiveBubble();
          createToolCard({
            badgeCls: 'cat-error',
            glyph: statusGlyph('error'),
            badgeLabel: 'FAILED',
            title: (e.code || 'error') + ': ' + (e.message || ''),
            body: JSON.stringify(e, null, 2),
            bodyLabel: 'ERROR',
            status: 'error',
          });
        }
      }
    }

  } catch (err) {
    if (err.name !== 'AbortError') {
      const bubble = ensureActiveBubble();
      bubble.classList.add('bubble-error');
      bubble.innerHTML = renderMarkdown('Error — ' + err.message);
      activeBubble = null; activeBubbleText = '';
      setStatus('error', '연결 오류');
    }
  }

  finalizeActiveBubble();
  setStreaming(false);
  if (!pendingAsk) inputEl.focus();
}

// --- Load models ---
async function loadModels() {
  try {
    const resp = await fetch(API_BASE + '/v1/models', { headers: getHeaders() });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.data && Array.isArray(data.data)) {
      modelSelect.innerHTML = '';
      const seen = new Set();
      for (const m of data.data) {
        const id = m.id || m;
        if (seen.has(id)) continue;
        seen.add(id);
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = id;
        modelSelect.appendChild(opt);
      }
      if (seen.has('sonnet')) modelSelect.value = 'sonnet';
    }
  } catch {}
}

// Persist API key
const savedKey = localStorage.getItem('gateway_api_key');
if (savedKey) apiKeyEl.value = savedKey;
apiKeyEl.addEventListener('change', () => {
  const v = apiKeyEl.value.trim();
  if (v) localStorage.setItem('gateway_api_key', v);
  else localStorage.removeItem('gateway_api_key');
  loadModels();
});

// ================================================================
// Admin auth gate — mirrors /admin login flow so /admin/chat works directly
// ================================================================
const authOverlay = document.getElementById('auth-overlay');
const authForm = document.getElementById('auth-form');
const authKeyEl = document.getElementById('auth-key');
const authErrorEl = document.getElementById('auth-error');

function showAuthOverlay() {
  authOverlay.classList.remove('hidden');
  authKeyEl.focus();
}
function hideAuthOverlay() {
  authOverlay.classList.add('hidden');
  authErrorEl.textContent = '';
  inputEl.focus();
}

async function checkAdminAuth() {
  try {
    const r = await fetch('/admin/api/server-info', { credentials: 'same-origin' });
    if (r.ok) { hideAuthOverlay(); return true; }
  } catch {}
  showAuthOverlay();
  return false;
}

authForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  authErrorEl.textContent = '';
  const key = authKeyEl.value;
  if (!key) return;
  try {
    const r = await fetch('/admin/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ api_key: key }),
    });
    if (r.ok) {
      authKeyEl.value = '';
      hideAuthOverlay();
    } else {
      let detail = 'Authentication failed';
      try { const d = await r.json(); detail = d.detail || detail; } catch {}
      authErrorEl.textContent = detail;
    }
  } catch {
    authErrorEl.textContent = 'Connection refused';
  }
});

checkAdminAuth();

loadModels();
inputEl.focus();
</script>
__OMG_THEME_TOGGLE_JS__
</body>
</html>"""
