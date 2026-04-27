"""Admin dashboard Alpine.js application code."""


def get_admin_js() -> str:
    """Return the admin dashboard alpine.js application code."""
    return """function adminApp() {
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
    plugins: [],
    pluginSkillView: null,
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
    newPromptNameWarning: '',
    newPromptContent: '',
    loading: { dashboard: false, logs: false, sessions: false, usage: false },
    usage: {
      enabled: null, summary: null, users: [], tools: [], turns: [],
      series: { day: [], week: [], month: [] },
      toolsByGran: { day: [], week: [], month: [] },
    },
    usageWindow: 7,
    usageStart: '',
    usageEnd: '',
    usageTurnsFilter: '',
    usageTurnsOffset: 0,

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
      } catch(e) { console.error('Failed to load summary', e); this.loginError = 'Failed to load summary'; this.showToast('Failed to load summary', 'err'); } finally { this.loading.dashboard = false; }
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
        if (r.ok) { this.summary = await r.json(); this._summaryFailCount = 0; }
        else if (r.status === 401) { this.authenticated = false; this.stopPolling(); }
      } catch(e) {
        console.error('Failed to load summary', e);
        this._summaryFailCount = (this._summaryFailCount || 0) + 1;
        if (this._summaryFailCount === 1) this.showToast('Failed to load summary', 'err');
      } finally { this.loading.sessions = false; }
    },

    async loadMetrics() {
      try {
        const r = await this.api('/admin/api/metrics');
        if (r.ok) this.metrics = await r.json();
      } catch(e) { console.error('Failed to load metrics', e); this.showToast('Failed to load metrics', 'err'); }
    },
    async loadBackends() {
      try {
        const r = await this.api('/admin/api/backends');
        if (r.ok) { const d = await r.json(); this.backendsDetail = d.backends || []; }
      } catch(e) { console.error('Failed to load backends', e); this.showToast('Failed to load backends', 'err'); }
    },
    async loadMcpServers() {
      try {
        const r = await this.api('/admin/api/mcp-servers');
        if (r.ok) { const d = await r.json(); this.mcpServers = d.servers || []; }
      } catch(e) { console.error('Failed to load MCP servers', e); this.showToast('Failed to load MCP servers', 'err'); }
    },

    async loadFiles() {
      try {
        const r = await this.api('/admin/api/files');
        if (r.ok) { const d = await r.json(); this.files = d.files || []; }
      } catch(e) { console.error('Failed to load files', e); this.showToast('Failed to load files', 'err'); }
    },

    async loadConfig() {
      try {
        const r = await this.api('/admin/api/config');
        if (r.ok) this.config = await r.json();
      } catch(e) { console.error('Failed to load config', e); this.showToast('Failed to load config', 'err'); }
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
        if (r.ok) { this.logs = await r.json(); this._logsFailCount = 0; }
      } catch(e) {
        console.error('Failed to load logs', e);
        this._logsFailCount = (this._logsFailCount || 0) + 1;
        if (this._logsFailCount === 1) this.showToast('Failed to load logs', 'err');
      } finally { this.loading.logs = false; }
    },
    toggleLogsPolling() {
      if (this.logsPollTimer) { clearInterval(this.logsPollTimer); this.logsPollTimer = null; }
      if (this.logsAutoRefresh) { this.logsPollTimer = setInterval(() => this.loadLogs(), 5000); }
    },

    _usageWindowQs() {
      if (this.usageStart && this.usageEnd) {
        return 'start_date=' + encodeURIComponent(this.usageStart) +
               '&end_date=' + encodeURIComponent(this.usageEnd);
      }
      return 'window_days=' + this.usageWindow;
    },

    async loadUsage() {
      this.loading.usage = true;
      try {
        const q = this._usageWindowQs();
        const [sumR, userR, toolR] = await Promise.all([
          this.api('/admin/api/usage/summary?' + q),
          this.api('/admin/api/usage/users?' + q + '&limit=20'),
          this.api('/admin/api/usage/tools?' + q + '&limit=30'),
        ]);
        if (sumR.ok) {
          const s = await sumR.json();
          this.usage.enabled = s.enabled;
          this.usage.summary = s.summary || null;
        }
        if (userR.ok) {
          const u = await userR.json();
          this.usage.users = u.items || [];
        }
        if (toolR.ok) {
          const t = await toolR.json();
          this.usage.tools = t.items || [];
        }
        await this.loadUsageTurns();
        await this.loadUsageSeries();
      } catch(e) {
        console.error('Failed to load usage', e);
        this.showToast('Failed to load usage', 'err');
      } finally { this.loading.usage = false; }
    },

    async loadUsageSeries() {
      // Always fetch all three granularities for the fixed 4x3 Trends grid.
      try {
        const grans = ['day', 'week', 'month'];
        // Approximate "last 5 of granularity" as a rolling-day window for
        // the per-cell top-tools list.
        const toolWindow = { day: 5, week: 35, month: 150 };
        const seriesPromises = grans.map(g =>
          this.api('/admin/api/usage/series?granularity=' + g + '&buckets=5'));
        const toolPromises = grans.map(g =>
          this.api('/admin/api/usage/tools?window_days=' + toolWindow[g] + '&limit=10'));
        const [seriesRes, toolRes] = await Promise.all([
          Promise.all(seriesPromises),
          Promise.all(toolPromises),
        ]);
        for (let i = 0; i < grans.length; i++) {
          if (seriesRes[i].ok) {
            const j = await seriesRes[i].json();
            this.usage.series[grans[i]] = (j.buckets || []).slice().reverse();
            if (this.usage.enabled === null) this.usage.enabled = j.enabled;
          }
          if (toolRes[i].ok) {
            const j = await toolRes[i].json();
            this.usage.toolsByGran[grans[i]] = j.items || [];
          }
        }
      } catch(e) {
        console.error('Failed to load usage series', e);
      }
    },

    usageSeriesEmpty() {
      const s = this.usage.series || {};
      return (s.day || []).length === 0 && (s.week || []).length === 0 && (s.month || []).length === 0;
    },

    seriesForChart(gran, field) {
      const rows = (this.usage.series || {})[gran] || [];
      if (rows.length === 0) return [];
      const vals = rows.map(r => {
        if (field === 'tokens') return Number(r.input_tokens || 0) + Number(r.output_tokens || 0);
        return Number(r[field] || 0);
      });
      const max = Math.max(1, ...vals);
      return rows.map((r, i) => ({
        label: String(r.bucket || ''),
        value: vals[i],
        pct: vals[i] / max,
      }));
    },

    async loadUsageTurns() {
      try {
        let url = '/admin/api/usage/turns?limit=50&offset=' + this.usageTurnsOffset;
        if (this.usageTurnsFilter) url += '&user=' + encodeURIComponent(this.usageTurnsFilter);
        const r = await this.api(url);
        if (r.ok) {
          const j = await r.json();
          this.usage.turns = j.items || [];
          if (this.usage.enabled === null) this.usage.enabled = j.enabled;
        }
      } catch(e) {
        console.error('Failed to load usage turns', e);
      }
    },

    async loadRateLimits() {
      try {
        const r = await this.api('/admin/api/rate-limits');
        if (r.ok) this.rateLimits = await r.json();
      } catch(e) { console.error('Failed to load rate limits', e); this.showToast('Failed to load rate limits', 'err'); }
    },

    async loadSandbox() {
      try {
        const r = await this.api('/admin/api/sandbox');
        if (r.ok) this.sandboxConfig = await r.json();
      } catch(e) { console.error('Failed to load sandbox config', e); this.showToast('Failed to load sandbox config', 'err'); }
    },

    async loadTools() {
      try {
        const r = await this.api('/admin/api/tools');
        if (r.ok) this.toolsRegistry = await r.json();
      } catch(e) { console.error('Failed to load tools', e); this.showToast('Failed to load tools', 'err'); }
    },

    async loadRuntimeConfig() {
      try {
        const r = await this.api('/admin/api/runtime-config');
        if (r.ok) { const d = await r.json(); this.runtimeConfig = d.settings || {}; }
      } catch(e) { console.error('Failed to load runtime config', e); this.showToast('Failed to load runtime config', 'err'); }
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
      } catch(e) { console.error('Failed to reset setting', e); this.showToast('Failed to reset setting', 'err'); }
    },
    async resetAllRuntimeConfig() {
      if (!confirm('Reset all runtime settings to startup defaults?')) return;
      try {
        const r = await this.api('/admin/api/runtime-config/reset', { method: 'POST' });
        if (r.ok) { this.showToast('ALL SETTINGS RESET', 'ok'); await this.loadRuntimeConfig(); }
      } catch(e) { console.error('Failed to reset all settings', e); this.showToast('Failed to reset all settings', 'err'); }
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
      this.newPromptNameWarning = '';
      this.newPromptContent = '';
      this.promptDirty = false;
    },

    validateNewPromptName() {
      const n = this.newPromptName.trim();
      this.newPromptNameError = '';
      this.newPromptNameWarning = '';
      if (!n) return;
      if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(n)) {
        this.newPromptNameError = 'letters, digits, hyphens, underscores only (max 64 chars)';
        return;
      }
      if (this.namedPrompts.some(p => p.name === n)) {
        this.newPromptNameWarning = 'prompt already exists (will overwrite on create)';
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
      } catch(e) { console.error('Failed to activate preset', e); this.showToast('Failed to activate preset', 'err'); }
    },
    forkFromPreset() {
      this.promptView = 'new';
      this.newPromptName = '';
      this.newPromptNameError = '';
      this.newPromptNameWarning = '';
      this.newPromptContent = this.systemPrompt.preset_text || '';
      this.promptDirty = false;
    },
    forkFromTemplate() {
      this.promptView = 'new';
      this.newPromptName = this.promptViewName ? this.promptViewName.replace(/-reference$/, '') : '';
      this.newPromptNameError = '';
      this.newPromptNameWarning = '';
      this.newPromptContent = this.promptEditorContent || '';
      this.promptDirty = false;
      this.validateNewPromptName();
    },

    async loadSkills() {
      const [r1, r2] = await Promise.all([
        this.api('/admin/api/skills').catch(() => null),
        this.api('/admin/api/plugins').catch(() => null),
      ]);
      if (r1?.ok) { const d = await r1.json(); this.skills = d.skills || []; }
      if (r2?.ok) {
        const d = await r2.json();
        const wasExpanded = new Set(this.plugins.filter(p => p._expanded).map(p => p.id));
        this.plugins = (d.plugins || []).map(p => ({ ...p, _expanded: wasExpanded.has(p.id) }));
      }
    },
    async openSkill(name) {
      if (this.skillDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      this.skillCreating = false;
      this.pluginSkillView = null;
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
    async openPluginSkill(plugin, skillName) {
      if (this.skillDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      this.selectedSkill = null;
      this.skillCreating = false;
      if (this.skillCm) { this.skillCm.toTextArea(); this.skillCm = null; }
      this.pluginSkillView = { pluginId: plugin.id, skillName, pluginName: plugin.name, version: plugin.version, content: '' };
      try {
        const r = await this.api('/admin/api/plugins/' + encodeURIComponent(plugin.id) + '/skills/' + encodeURIComponent(skillName));
        if (r.ok) {
          const d = await r.json();
          this.pluginSkillView.content = d.content || '';
        } else {
          this.showToast('Failed to load plugin skill', 'err');
          this.pluginSkillView = null;
        }
      } catch(e) { this.showToast('Connection error', 'err'); this.pluginSkillView = null; }
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
      this.pluginSkillView = null;
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
      } catch(e) { console.error('Failed to load full message', e); this.showToast('Failed to load full message', 'err'); }
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
    },

    formatNum(n) {
      if (n === null || n === undefined) return '-';
      const v = Number(n);
      if (!isFinite(v)) return String(n);
      if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
      if (v >= 1e3) return (v / 1e3).toFixed(1) + 'k';
      return String(v);
    }
  };
}"""
