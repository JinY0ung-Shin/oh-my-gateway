"""Chat UI page — a simple terminal-style chat interface.

Communicates with /v1/responses via SSE streaming.
Supports multi-turn conversation via previous_response_id chaining,
and AskUserQuestion (function_call / function_call_output) flow.
"""


def build_chat_page() -> str:
    """Build the chat UI HTML."""
    return r"""<!DOCTYPE html>
<html lang="ko" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GATEWAY CHAT // Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ================================================================
   TERMINAL CHAT UI — Oh My Gateway
   Matches the admin panel phosphor/CRT aesthetic
   ================================================================ */

:root {
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

  --gap-xs: 0.25rem;
  --gap-sm: 0.5rem;
  --gap-md: 0.75rem;
  --gap-lg: 1rem;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg-deep);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-base);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* CRT Scanline */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(
    0deg, transparent, transparent 2px,
    rgba(0,0,0,0.08) 2px, rgba(0,0,0,0.08) 4px
  );
  pointer-events: none;
  z-index: 9999;
}

/* Grid BG */
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

/* === Header === */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.5rem 1rem;
  border-bottom: 1px solid var(--border-bright);
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
  color: var(--green);
  font-size: var(--fs-lg);
  font-weight: 600;
  text-shadow: 0 0 8px var(--green-muted);
}
.header .session-tag {
  font-size: var(--fs-xs);
  color: var(--text-dim);
  border: 1px solid var(--border-bright);
  padding: 2px 8px;
}
.header .right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

/* Buttons */
.btn {
  font-family: var(--font);
  font-size: var(--fs-xs);
  padding: 4px 10px;
  background: var(--bg-raised);
  border: 1px solid var(--border-bright);
  color: var(--text-dim);
  cursor: pointer;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  transition: all 0.15s;
}
.btn:hover {
  color: var(--green);
  border-color: var(--green-dim);
  text-shadow: 0 0 4px var(--green-muted);
}
.btn-danger:hover {
  color: var(--red);
  border-color: var(--red-dim);
}

/* API key input */
.api-key-input {
  font-family: var(--font);
  font-size: var(--fs-xs);
  background: var(--bg-raised);
  color: var(--text);
  border: 1px solid var(--border-bright);
  padding: 3px 6px;
  width: 140px;
  outline: none;
}
.api-key-input:focus {
  border-color: var(--amber-dim);
}
.api-key-input::placeholder {
  color: var(--text-muted);
}

/* Model select */
.model-select {
  font-family: var(--font);
  font-size: var(--fs-xs);
  background: var(--bg-raised);
  color: var(--text);
  border: 1px solid var(--border-bright);
  padding: 3px 6px;
  outline: none;
  cursor: pointer;
}
.model-select:focus {
  border-color: var(--green-dim);
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

.chat-container::-webkit-scrollbar { width: 6px; }
.chat-container::-webkit-scrollbar-track { background: var(--bg-deep); }
.chat-container::-webkit-scrollbar-thumb { background: var(--border-bright); border-radius: 3px; }

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
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 2px;
}
.message.user .role { color: var(--cyan); }
.message.assistant .role { color: var(--green-dim); }
.message.system .role { color: var(--amber); }

.message .bubble {
  display: inline-block;
  text-align: left;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border);
  background: var(--bg-raised);
  font-size: var(--fs-sm);
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  max-width: 100%;
}
.message.user .bubble {
  border-color: var(--cyan-dim);
  background: var(--cyan-subtle);
}
.message.assistant .bubble {
  border-color: var(--border-bright);
}
.message.system .bubble {
  border-color: var(--amber-dim);
  background: rgba(255, 176, 0, 0.05);
  color: var(--amber);
  font-size: var(--fs-xs);
}

/* Streaming cursor */
.bubble .cursor {
  display: inline-block;
  width: 7px;
  height: 14px;
  background: var(--green);
  animation: blink 0.8s step-end infinite;
  vertical-align: text-bottom;
  margin-left: 2px;
}
@keyframes blink {
  50% { opacity: 0; }
}

/* Markdown in assistant */
.bubble code {
  background: var(--bg-surface);
  padding: 1px 4px;
  border-radius: 2px;
  font-size: 0.9em;
}
.bubble pre {
  background: var(--bg-deep);
  border: 1px solid var(--border);
  padding: 0.5rem;
  margin: 0.4rem 0;
  overflow-x: auto;
  font-size: var(--fs-xs);
}
.bubble pre code {
  background: none;
  padding: 0;
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
  letter-spacing: 0.1em;
  margin-bottom: 2px;
  color: var(--magenta);
}
.ask-prompt .ask-bubble {
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--magenta);
  background: rgba(255, 0, 255, 0.05);
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
  color: var(--text);
  cursor: pointer;
  transition: all 0.15s;
}
.ask-prompt .ask-option-btn.multi {
  display: flex;
  gap: 0.5rem;
  align-items: flex-start;
}
.ask-prompt .ask-option-btn:hover {
  border-color: var(--magenta);
  background: rgba(255, 0, 255, 0.08);
}
.ask-prompt .ask-option-btn.selected {
  border-color: var(--magenta);
  background: rgba(255, 0, 255, 0.15);
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
  color: var(--text);
  border: 1px solid var(--magenta);
  padding: 6px 10px;
  outline: none;
}
.ask-prompt .ask-input:focus {
  box-shadow: 0 0 6px rgba(255, 0, 255, 0.3);
}
.ask-prompt .ask-submit {
  font-family: var(--font);
  font-size: var(--fs-xs);
  padding: 6px 14px;
  background: rgba(255, 0, 255, 0.1);
  border: 1px solid var(--magenta);
  color: var(--magenta);
  cursor: pointer;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  transition: all 0.15s;
}
.ask-prompt .ask-submit:hover {
  background: rgba(255, 0, 255, 0.2);
  text-shadow: 0 0 4px rgba(255, 0, 255, 0.4);
}

/* Tool events */
.tool-event {
  margin-bottom: 0.5rem;
  max-width: 85%;
  animation: fadeIn 0.15s ease;
}
.tool-event details {
  border: 1px solid var(--border);
  background: var(--bg-surface);
}
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
  content: '>';
  margin-left: auto;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  transition: transform 0.15s;
}
.tool-event details[open] summary::after { transform: rotate(90deg); color: var(--green-dim); }
.tool-event details[open] summary { border-bottom: 1px solid var(--border); }
.tool-event .tool-badge {
  font-size: var(--fs-xs);
  padding: 1px 6px;
  border: 1px solid;
  font-weight: 500;
}
.tool-badge.tool-use {
  color: var(--amber);
  border-color: var(--amber-dim);
}
.tool-badge.tool-result {
  color: var(--cyan);
  border-color: var(--cyan-dim);
}
.tool-badge.tool-error {
  color: var(--red);
  border-color: var(--red-dim);
}
.tool-badge.task {
  color: var(--green-dim);
  border-color: var(--green-dim);
}
.tool-event .tool-body {
  padding: 6px 8px;
  font-size: var(--fs-xs);
  overflow-x: auto;
  max-height: 300px;
  overflow-y: auto;
}
.tool-event .tool-body pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-dim);
}

/* === Input area === */
.input-area {
  padding: 0.75rem 1rem;
  border-top: 1px solid var(--border-bright);
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
  background: var(--bg-deep);
  color: var(--text);
  border: 1px solid var(--border-bright);
  padding: 8px 12px;
  resize: none;
  outline: none;
  min-height: 38px;
  max-height: 200px;
  line-height: 1.5;
  overflow-y: auto;
}
.input-row textarea:focus {
  border-color: var(--green-dim);
  box-shadow: 0 0 6px var(--green-muted);
}
.input-row textarea::placeholder {
  color: var(--text-muted);
}

.send-btn {
  font-family: var(--font);
  font-size: var(--fs-sm);
  padding: 8px 18px;
  background: var(--bg-raised);
  color: var(--green);
  border: 1px solid var(--green-dim);
  cursor: pointer;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  transition: all 0.15s;
  white-space: nowrap;
  height: 38px;
}
.send-btn:hover:not(:disabled) {
  background: var(--green-subtle);
  text-shadow: 0 0 6px var(--green-muted);
}
.send-btn:disabled {
  opacity: 0.3;
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

/* Welcome */
.welcome {
  text-align: center;
  padding: 3rem 1rem;
  color: var(--text-dim);
}
.welcome .ascii-art {
  font-size: var(--fs-xs);
  color: var(--green-dim);
  text-shadow: 0 0 8px var(--green-muted);
  white-space: pre;
  line-height: 1.15;
  margin-bottom: 1rem;
}
.welcome p {
  font-size: var(--fs-sm);
  margin-bottom: 0.25rem;
}
.welcome .hint {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

/* Focus visible */
:focus-visible { outline: 1px solid var(--green); outline-offset: 2px; }

/* Send button active */
.send-btn:active:not(:disabled) { transform: scale(0.96); background: var(--green-muted); }

/* Tool event indent */
.tool-event { margin-left: 1.5rem; border-left: 2px solid var(--border); padding-left: var(--gap-sm); }
.tool-event .tool-body::-webkit-scrollbar { width: 4px; }
.tool-event .tool-body::-webkit-scrollbar-thumb { background: var(--border-bright); border-radius: 2px; }

/* Responsive */
@media (max-width: 640px) {
  .message { max-width: 95%; }
  .header .title { font-size: var(--fs-base); }
  .welcome .ascii-art { font-size: 0.5rem; }
  .header { flex-wrap: wrap; gap: var(--gap-sm); padding: 0.4rem var(--gap-md); }
  .header .right { width: 100%; justify-content: flex-end; overflow-x: auto; flex-wrap: nowrap; }
  .api-key-input { width: 100px; }
  .input-area { padding: var(--gap-sm); }
  .ask-prompt .ask-input-row { flex-direction: column; }
  .ask-prompt .ask-option-btn { padding: 10px 12px; }
  .tool-event { max-width: 100%; margin-left: var(--gap-sm); }
}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="left">
    <span class="title">CHAT //</span>
    <span class="session-tag" id="session-tag">NO SESSION</span>
  </div>
  <div class="right">
    <input type="password" class="api-key-input" id="api-key" placeholder="API Key (optional)" title="Bearer token for /v1/responses">
    <select class="model-select" id="model-select">
      <option value="sonnet">sonnet</option>
      <option value="opus">opus</option>
      <option value="haiku">haiku</option>
    </select>
    <button class="btn" onclick="newSession()" title="New session">NEW</button>
    <a class="btn" href="/admin">ADMIN</a>
    <a class="btn" href="/">HOME</a>
  </div>
</div>

<!-- Chat -->
<div class="chat-container" id="chat" role="log" aria-label="채팅 메시지" aria-live="polite">
  <div class="welcome" id="welcome">
    <div class="ascii-art">
 ██████╗██╗  ██╗ █████╗ ████████╗
██╔════╝██║  ██║██╔══██╗╚══██╔══╝
██║     ███████║███████║   ██║
██║     ██╔══██║██╔══██║   ██║
╚██████╗██║  ██║██║  ██║   ██║
 ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝
    </div>
    <p>Oh My Gateway Chat Terminal</p>
    <p class="hint">메시지를 입력하고 Enter로 전송 (Shift+Enter: 줄바꿈)</p>
    <p class="hint">v1/responses API를 통해 실시간 SSE 스트리밍</p>
  </div>
</div>

<!-- Input -->
<div class="input-area">
  <div class="input-row">
    <textarea id="input" rows="1" placeholder="메시지 입력..." autofocus aria-label="메시지 입력"></textarea>
    <button class="send-btn" id="send-btn" onclick="sendMessage()" aria-label="메시지 전송">SEND</button>
  </div>
</div>

<!-- Status Bar -->
<div class="status-bar">
  <span><span class="status-dot idle" id="status-dot"></span><span id="status-text">대기중</span></span>
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
  setStatus(v ? 'streaming' : 'idle', v ? '스트리밍중...' : '대기중');
}
function updateSessionTag() {
  sessionTag.textContent = sessionId ? sessionId.substring(0, 12) + '...' : 'NO SESSION';
  sessionTag.title = sessionId || '';
}
function newSession() {
  if (chatEl.querySelectorAll('.message').length > 0) {
    if (!confirm('현재 대화가 삭제됩니다. 새 세션을 시작하시겠습니까?')) return;
  }
  previousResponseId = null; sessionId = null; pendingAsk = null;
  updateSessionTag();
  chatEl.innerHTML = '';
  chatEl.appendChild(welcomeEl);
  welcomeEl.style.display = '';
  tokenInfo.textContent = '';
  setStatus('idle', '대기중');
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
function renderMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  return html;
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
  div.innerHTML = '<div class="role">assistant</div><div class="bubble"><span class="cursor"></span></div>';
  chatEl.appendChild(div);
  scrollToBottom();
  return div.querySelector('.bubble');
}

function addToolEvent(badgeClass, badgeText, title, bodyContent) {
  welcomeEl.style.display = 'none';
  const div = document.createElement('div');
  div.className = 'tool-event';
  div.innerHTML =
    '<details><summary>' +
    '<span class="tool-badge ' + badgeClass + '">' + escapeHtml(badgeText) + '</span> ' +
    '<span>' + escapeHtml(title) + '</span>' +
    '</summary>' +
    '<div class="tool-body"><pre>' + escapeHtml(bodyContent) + '</pre></div>' +
    '</details>';
  chatEl.appendChild(div);
  scrollToBottom();
  return div;
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
      '<input type="text" class="ask-input" placeholder="직접 입력...">' +
      '<button class="ask-submit">REPLY</button></div>';
    div.innerHTML = html;
  } else {
    // Simple text format
    const question = argsObj.question || argsObj.text || JSON.stringify(argsObj);
    div.innerHTML =
      '<div class="role">AskUserQuestion</div>' +
      '<div class="ask-bubble">' + escapeHtml(question) + '</div>' +
      '<div class="ask-input-row">' +
      '<input type="text" class="ask-input" placeholder="응답 입력...">' +
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
  const bubble = addStreamingMessage();
  let fullText = '';
  let responseId = null;

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
      bubble.innerHTML = renderMarkdown('Error: ' + resp.status + ' — ' + err);
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
          fullText += evt.delta;
          bubble.innerHTML = renderMarkdown(fullText) + '<span class="cursor"></span>';
          scrollToBottom();
        }

        // --- Tool use ---
        if (type === 'response.tool_use') {
          const name = evt.name || 'unknown';
          const input = evt.input || {};
          const summary = typeof input === 'object'
            ? Object.entries(input).map(([k,v]) => k + ': ' + (typeof v === 'string' ? v.substring(0, 80) : JSON.stringify(v).substring(0, 80))).join(', ')
            : String(input).substring(0, 120);
          setStatus('streaming', 'Tool: ' + name);
          addToolEvent('tool-use', 'TOOL', name + (summary ? ' — ' + summary.substring(0, 60) : ''),
            JSON.stringify(input, null, 2));
        }

        // --- Tool result ---
        if (type === 'response.tool_result') {
          const isError = evt.is_error;
          const content = typeof evt.content === 'string' ? evt.content : JSON.stringify(evt.content, null, 2);
          const preview = (content || '').substring(0, 80).replace(/\n/g, ' ');
          addToolEvent(
            isError ? 'tool-error' : 'tool-result',
            isError ? 'ERROR' : 'RESULT',
            preview || '(empty)',
            content || '(no content)'
          );
          setStatus('streaming', '스트리밍중...');
        }

        // --- Task events ---
        if (type === 'response.task') {
          const task = evt.task || evt;
          const taskType = task.type || '';
          let title = '';
          if (taskType === 'task_started') title = 'Started: ' + (task.description || '');
          else if (taskType === 'task_progress') title = 'Progress: ' + (task.description || task.last_tool_name || '');
          else if (taskType === 'task_notification') title = (task.status || '') + ': ' + (task.summary || '');
          else title = taskType;
          if (title) {
            addToolEvent('task', 'TASK', title, JSON.stringify(task, null, 2));
            setStatus('streaming', title.substring(0, 40));
          }
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
                if (!fullText) bubble.parentElement.remove();
                else { bubble.innerHTML = renderMarkdown(fullText); }
                showAskPrompt(args, item.call_id, r.id);
              }
            }
          }
        }

        // --- response.completed ---
        if (type === 'response.completed' && evt.response) {
          if (evt.response.id) previousResponseId = evt.response.id;
          if (evt.response.usage) {
            const u = evt.response.usage;
            tokenInfo.textContent = 'IN: ' + (u.input_tokens || 0) + '  OUT: ' + (u.output_tokens || 0);
          }
        }

        // --- response.failed ---
        if (type === 'response.failed' && evt.response && evt.response.error) {
          const e = evt.response.error;
          addToolEvent('tool-error', 'FAILED', e.code + ': ' + e.message, JSON.stringify(e, null, 2));
        }
      }
    }

  } catch (err) {
    if (err.name !== 'AbortError') {
      bubble.innerHTML = renderMarkdown('Error: ' + err.message);
      setStatus('error', '연결 오류');
    }
  }

  if (fullText) bubble.innerHTML = renderMarkdown(fullText);
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

loadModels();
inputEl.focus();
</script>
</body>
</html>"""
