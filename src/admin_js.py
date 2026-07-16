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
    config: {},
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
    plugins: [],
    pluginSkillView: null,
    marketplaces: [],
    pluginBusy: false,
    marketplaceBusy: {},
    marketplaceTokens: {},
    autoRefresh: { enabled: false, interval_minutes: 60, running: false, last_run_at: null, next_run_at: null, last_results: [] },
    autoRefreshBusy: false,
    autoRefreshPollTimer: null,
    catalogFilter: '',
    catalogBusy: {},
    catalogLoading: false,
    showAdvancedInstall: false,
    mpForm: { repo: '', branch: 'main', scope: 'user', git_token: '' },
    pluginForm: { name: '', marketplace: '', scope: 'user' },
    mcpDetail: { servers: [], dropped: [] },
    mcpForm: {
      name: '', type: 'stdio', jsonConfig: '',
      envPairs: [], headerPairs: [],
      command: '', argsText: '', url: ''
    },
    mcpBusy: false,
    mcpEditName: null,
    mcpJsonError: '',
    mcpJsonWarning: '',
    mcpPatternPreview: [],
    mcpEnvRefPreview: [],
    mcpShowAdvanced: false,
    mcpSyncLock: false,
    mcpTestBusy: {},
    mcpTestResult: {},
    mcpOverlayName: null,
    mcpOverlayPluginId: null,
    mcpOverlayHadExisting: false,
    mcpOverlayBusy: false,
    mcpOverlayForm: { envPairs: [], headerPairs: [] },
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
    loading: {
      dashboard: false,
      logs: false,
      sessions: false,
      usage: false,
      backends: false,
      mcp: false,
    },
    usage: {
      enabled: null, summary: null, users: [], tools: [], turns: [],
      series: { day: [], week: [], month: [] },
      toolsByGran: { day: [], week: [], month: [] },
      toolsSeries: {
        day: { tools: [], buckets: [] },
        week: { tools: [], buckets: [] },
        month: { tools: [], buckets: [] },
      },
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
          if (this.tab === 'config' && this.promptView === 'named' && this.promptDirty) this.saveNamedPrompt();
        }
      });
      try {
        this.loading.dashboard = true;
        const r = await this.api('/admin/api/summary');
        if (r.ok) { this.authenticated = true; this.summary = await r.json(); this.loadBackends(); this.loadMcpDetail(); this.loadMetrics(); this.startPolling(); }
      } catch(e) { console.error('Failed to load summary', e); this.loginError = 'Failed to load summary'; this.showToast('Failed to load summary', 'err'); } finally { this.loading.dashboard = false; }
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
          this.loadMcpDetail();
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
      this.loading.backends = true;
      try {
        const r = await this.api('/admin/api/backends');
        if (r.ok) { const d = await r.json(); this.backendsDetail = d.backends || []; }
      } catch(e) { console.error('Failed to load backends', e); this.showToast('Failed to load backends', 'err'); }
      finally { this.loading.backends = false; }
    },
    async loadMcpDetail() {
      this.loading.mcp = true;
      try {
        const r = await this.api('/admin/api/mcp-servers');
        if (r.ok) {
          const d = await r.json();
          this.mcpDetail = { servers: d.servers || [], dropped: d.dropped || [] };
          this.mcpServers = d.servers || [];
        }
      } catch(e) { console.error('Failed to load MCP servers', e); this.showToast('Failed to load MCP servers', 'err'); }
      finally { this.loading.mcp = false; }
    },

    async loadConfig() {
      try {
        const r = await this.api('/admin/api/config');
        if (r.ok) this.config = await r.json();
      } catch(e) { console.error('Failed to load config', e); this.showToast('Failed to load config', 'err'); }
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
      await Promise.all([this.loadSummary(), this.loadConfig(), this.loadBackends(), this.loadMcpDetail(), this.loadMetrics()]);
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
        const toolSeriesPromises = grans.map(g =>
          this.api('/admin/api/usage/tools-series?granularity=' + g + '&buckets=5&top=5'));
        const [seriesRes, toolRes, toolSeriesRes] = await Promise.all([
          Promise.all(seriesPromises),
          Promise.all(toolPromises),
          Promise.all(toolSeriesPromises),
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
          if (toolSeriesRes[i].ok) {
            const j = await toolSeriesRes[i].json();
            // backend buckets are DESC; reverse so chart reads left = older
            this.usage.toolsSeries[grans[i]] = {
              tools: j.tools || [],
              buckets: (j.buckets || []).slice().reverse(),
            };
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

    toolColor(idx) {
      const palette = [
        'var(--green)', 'var(--cyan)', 'var(--amber)',
        'var(--red)', '#a855f7', '#3b82f6'
      ];
      return palette[((idx % palette.length) + palette.length) % palette.length];
    },

    toolSeriesMax(gran) {
      const ts = (this.usage.toolsSeries || {})[gran];
      if (!ts) return 1;
      let max = 1;
      for (const b of (ts.buckets || [])) {
        for (const t of (ts.tools || [])) {
          const v = Number((b.values || {})[t] || 0);
          if (v > max) max = v;
        }
      }
      return max;
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
    selectFilePrompt() {
      if (this.promptDirty && !confirm('Unsaved changes will be lost. Continue?')) return;
      this.promptView = 'file';
      this.promptViewName = null;
      this.promptEditorContent = this.systemPrompt.prompt || '';
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
    forkFromFile() {
      this.promptView = 'new';
      this.newPromptName = '';
      this.newPromptNameError = '';
      this.newPromptNameWarning = '';
      this.newPromptContent = this.systemPrompt.prompt || '';
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
      try {
        const r = await this.api('/admin/api/plugins');
        if (r.ok) {
          const d = await r.json();
          const wasExpanded = new Set(this.plugins.filter(p => p._expanded).map(p => p.id + '@' + p.scope));
          this.plugins = (d.plugins || []).map(p => ({ ...p, _expanded: wasExpanded.has(p.id + '@' + p.scope) }));
        }
      } catch(e) { console.error('Failed to load plugins', e); this.showToast('Failed to load plugins', 'err'); }
    },
    async openPluginSkill(plugin, skillName) {
      this.pluginSkillView = { pluginId: plugin.id, scope: plugin.scope, skillName, pluginName: plugin.name, version: plugin.version, content: '' };
      try {
        const r = await this.api('/admin/api/plugins/' + encodeURIComponent(plugin.id) + '/skills/' + encodeURIComponent(skillName) + '?scope=' + encodeURIComponent(plugin.scope || 'user'));
        if (r.ok) {
          const d = await r.json();
          this.pluginSkillView.content = d.content || '';
        } else {
          this.showToast('Failed to load plugin skill', 'err');
          this.pluginSkillView = null;
        }
      } catch(e) { this.showToast('Connection error', 'err'); this.pluginSkillView = null; }
    },

    // --- Plugin management (PLUGINS tab) ---
    async loadPlugins() { await this.loadSkills(); },

    // Browse catalog: load every marketplace + its plugins in one call.
    async loadCatalog() {
      this.catalogLoading = true;
      try {
        const r = await this.api('/admin/api/marketplaces/catalog');
        if (r.ok) {
          const d = await r.json();
          const wasExpanded = new Set(this.marketplaces.filter(m => m._expanded).map(m => m.name));
          this.marketplaces = (d.marketplaces || []).map(m => ({
            ...m, _expanded: wasExpanded.has(m.name),
          }));
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Failed to load catalog', 'err');
        }
      } catch(e) {
        console.error('Failed to load catalog', e);
        this.showToast('Failed to load catalog', 'err');
      } finally { this.catalogLoading = false; }
    },

    // Refresh both the catalog and the installed list so installed flags
    // and origin badges update immediately after a mutation.
    async refreshPluginViews() {
      await Promise.all([this.loadCatalog(), this.loadPlugins()]);
    },

    // Legacy plain marketplace list (kept for the advanced install form's
    // dropdown); catalog also populates this.marketplaces with names.
    async loadMarketplaces() { await this.loadCatalog(); },

    toggleMarketplace(m) { m._expanded = !m._expanded; },

    // Client-side filter across all marketplaces by name/description.
    filteredCatalogPlugins(m) {
      const q = this.catalogFilter.trim().toLowerCase();
      const list = m.plugins || [];
      if (!q) return list;
      return list.filter(p =>
        (p.name || '').toLowerCase().includes(q) ||
        (p.description || '').toLowerCase().includes(q));
    },

    // A marketplace is hidden while filtering if it has no matching plugins.
    marketplaceVisible(m) {
      if (!this.catalogFilter.trim()) return true;
      return this.filteredCatalogPlugins(m).length > 0;
    },

    catalogVisibleCount() {
      return this.marketplaces.filter(m => this.marketplaceVisible(m)).length;
    },

    async installFromCatalog(m, plug) {
      this.catalogBusy[plug.id] = true;
      try {
        const r = await this.api('/admin/api/plugins', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ name: plug.name, marketplace: m.name, scope: m.scope || 'user' })
        });
        if (r.ok) {
          this.showToast('PLUGIN INSTALLED: ' + plug.name, 'ok');
          await this.refreshPluginViews();
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Install failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { delete this.catalogBusy[plug.id]; }
    },

    async uninstallFromCatalog(plug) {
      if (!confirm('Uninstall plugin "' + plug.name + '"?')) return;
      this.catalogBusy[plug.id] = true;
      try {
        const r = await this.api('/admin/api/plugins/' + encodeURIComponent(plug.id) +
          '?scope=' + encodeURIComponent(plug.scope || 'user'), { method: 'DELETE' });
        if (r.ok) {
          this.showToast('PLUGIN UNINSTALLED', 'ok');
          await this.refreshPluginViews();
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Uninstall failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { delete this.catalogBusy[plug.id]; }
    },

    async addMarketplace() {
      const repo = this.mpForm.repo.trim();
      if (!repo) return;
      this.pluginBusy = true;
      try {
        const r = await this.api('/admin/api/marketplaces', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            repo,
            branch: this.mpForm.branch.trim() || 'main',
            scope: this.mpForm.scope,
            git_token: this.mpForm.git_token,
          })
        });
        if (r.ok) {
          this.showToast('MARKETPLACE ADDED', 'ok');
          this.mpForm = { repo: '', branch: 'main', scope: 'user', git_token: '' };
          await this.loadMarketplaces();
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Add failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.pluginBusy = false; }
    },

    async removeMarketplace(name, scope) {
      if (!confirm('Remove marketplace "' + name + '"?')) return;
      this.pluginBusy = true;
      try {
        const r = await this.api('/admin/api/marketplaces/' + encodeURIComponent(name) +
          '?scope=' + encodeURIComponent(scope || 'user'), { method: 'DELETE' });
        if (r.ok) {
          this.showToast('MARKETPLACE REMOVED', 'ok');
          await this.loadMarketplaces();
          await this.loadPlugins();
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Remove failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.pluginBusy = false; }
    },

    async refreshMarketplace(m) {
      const name = m.name;
      if (!name) return;
      this.marketplaceBusy[name] = true;
      try {
        const token = (this.marketplaceTokens[name] || '').trim();
        const r = await this.api('/admin/api/marketplaces/' + encodeURIComponent(name) + '/refresh', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ scope: m.scope || '', git_token: token })
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) {
          const updated = (d.updated_plugins || []).length;
          const failed = (d.failed_updates || []).length;
          const suffix = failed ? ' (' + updated + ' updated, ' + failed + ' failed)' : ' (' + updated + ' updated)';
          this.showToast('MARKETPLACE REFRESHED' + suffix, failed ? 'err' : 'ok');
          await this.refreshPluginViews();
        } else {
          this.showToast(d.error || 'Refresh failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.marketplaceTokens[name] = ''; delete this.marketplaceBusy[name]; }
    },

    // --- Marketplace auto-refresh (PLUGINS tab) ---
    async loadAutoRefresh() {
      try {
        const r = await this.api('/admin/api/plugins/auto-refresh');
        if (r.ok) {
          this.autoRefresh = { ...this.autoRefresh, ...(await r.json()) };
          this._watchAutoRefreshRun();
        }
      } catch(e) { console.error('Failed to load auto-refresh', e); }
    },

    async saveAutoRefresh() {
      this.autoRefreshBusy = true;
      try {
        const r = await this.api('/admin/api/plugins/auto-refresh', {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            enabled: !!this.autoRefresh.enabled,
            interval_minutes: Number(this.autoRefresh.interval_minutes) || 60,
          })
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) {
          this.autoRefresh = { ...this.autoRefresh, ...d };
          this.showToast('AUTO-REFRESH ' + (d.enabled ? 'ENABLED' : 'DISABLED'), 'ok');
        } else {
          this.showToast(d.error || 'Save failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.autoRefreshBusy = false; }
    },

    async runAutoRefreshNow() {
      this.autoRefreshBusy = true;
      try {
        const r = await this.api('/admin/api/plugins/auto-refresh/run', { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.status === 'started') {
          this.autoRefresh.running = true;
          this.showToast('REFRESH CYCLE STARTED', 'ok');
          this._watchAutoRefreshRun();
        } else if (d.status === 'already_running') {
          this.showToast('Refresh cycle already running', 'err');
        } else {
          this.showToast(d.error || 'Trigger failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.autoRefreshBusy = false; }
    },

    _stopAutoRefreshWatch() {
      if (this.autoRefreshPollTimer) {
        clearInterval(this.autoRefreshPollTimer);
        this.autoRefreshPollTimer = null;
      }
    },

    // Poll status while a cycle is running so the card flips back to idle and
    // the plugin lists pick up new versions once it finishes. Only status
    // fields are synced — never `enabled`/`interval_minutes`, so a poll landing
    // mid-edit can't clobber the admin's unsaved form. Clears on the server
    // reporting idle (not a transition the tab-switch merge could swallow) and
    // on any non-ok response (e.g. auth expiry) so it can never poll forever.
    _watchAutoRefreshRun() {
      if (!this.autoRefresh.running || this.autoRefreshPollTimer) return;
      this.autoRefreshPollTimer = setInterval(async () => {
        let d;
        try {
          const r = await this.api('/admin/api/plugins/auto-refresh');
          if (!r.ok) { this._stopAutoRefreshWatch(); return; }
          d = await r.json();
        } catch(e) { return; /* transient network error: keep polling */ }
        this.autoRefresh.running = d.running;
        this.autoRefresh.last_run_at = d.last_run_at;
        this.autoRefresh.next_run_at = d.next_run_at;
        this.autoRefresh.last_results = d.last_results;
        if (!d.running) {
          this._stopAutoRefreshWatch();
          const errs = (d.last_results || []).filter(x => x.status !== 'refreshed').length;
          this.showToast('REFRESH CYCLE FINISHED' + (errs ? ' (' + errs + ' failed)' : ''), errs ? 'err' : 'ok');
          await this.refreshPluginViews();
        }
      }, 4000);
    },

    autoRefreshSummary() {
      const a = this.autoRefresh;
      const parts = [];
      if (a.last_run_at) {
        const errs = (a.last_results || []).filter(x => x.status !== 'refreshed').length;
        parts.push('last: ' + a.last_run_at + (errs ? ' (' + errs + ' failed)' : ''));
      } else {
        parts.push('not run yet');
      }
      if (a.enabled && a.next_run_at) parts.push('next: ' + a.next_run_at);
      return parts.join('  |  ');
    },

    async installPlugin() {
      const name = this.pluginForm.name.trim();
      if (!name) return;
      this.pluginBusy = true;
      try {
        const r = await this.api('/admin/api/plugins', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name,
            marketplace: this.pluginForm.marketplace,
            scope: this.pluginForm.scope,
          })
        });
        if (r.ok) {
          this.showToast('PLUGIN INSTALLED: ' + name, 'ok');
          this.pluginForm = { name: '', marketplace: '', scope: 'user' };
          await this.refreshPluginViews();
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Install failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.pluginBusy = false; }
    },

    async uninstallPlugin(pluginId, scope) {
      if (!confirm('Uninstall plugin "' + pluginId + '"?')) return;
      this.pluginBusy = true;
      try {
        const r = await this.api('/admin/api/plugins/' + encodeURIComponent(pluginId) +
          '?scope=' + encodeURIComponent(scope || 'user'), { method: 'DELETE' });
        if (r.ok) {
          this.showToast('PLUGIN UNINSTALLED', 'ok');
          await this.refreshPluginViews();
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Uninstall failed', 'err');
        }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.pluginBusy = false; }
    },

    // --- MCP server management (MCP tab) ---
    async refreshMcp() { await Promise.all([this.loadMcpDetail(), this.loadTools()]); },

    mcpEnvRefBadge(ref) {
      // Built via concat so static HTML never embeds mustache-like {{ }} tokens.
      return '{{' + 'env:' + ref + '}}';
    },
    _mcpEmptyForm() {
      return {
        name: '', type: 'stdio', jsonConfig: '',
        envPairs: [], headerPairs: [],
        command: '', argsText: '', url: ''
      };
    },
    _mcpPairsFromMap(map) {
      if (!map || typeof map !== 'object' || Array.isArray(map)) return [];
      return Object.keys(map).map(k => ({ key: k, value: map[k] == null ? '' : String(map[k]) }));
    },
    _mcpMapFromPairs(pairs) {
      const out = {};
      for (const p of (pairs || [])) {
        const k = (p.key || '').trim();
        if (!k) continue;
        out[k] = p.value == null ? '' : String(p.value);
      }
      return out;
    },
    _mcpParseArgs(text) {
      const raw = (text || '').trim();
      if (!raw) return [];
      try {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) return parsed.map(String);
      } catch (_) {}
      return raw.split(/\s+/).filter(Boolean);
    },
    _mcpCollectEnvRefs(cfg) {
      const refs = new Set();
      const re = /\{\{\s*env:([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;
      for (const field of ['env', 'headers']) {
        const m = cfg && cfg[field];
        if (!m || typeof m !== 'object') continue;
        for (const v of Object.values(m)) {
          if (typeof v !== 'string') continue;
          let match;
          re.lastIndex = 0;
          while ((match = re.exec(v)) !== null) refs.add(match[1]);
        }
      }
      return Array.from(refs).sort();
    },
    _mcpBuildConfigFromForm() {
      let cfg = {};
      try {
        const raw = (this.mcpForm.jsonConfig || '').trim();
        if (raw) cfg = JSON.parse(raw);
      } catch (e) {
        throw e;
      }
      if (typeof cfg !== 'object' || Array.isArray(cfg) || cfg === null) cfg = {};
      cfg.type = this.mcpForm.type || cfg.type || 'stdio';
      if (cfg.type === 'stdio') {
        if (this.mcpForm.command) cfg.command = this.mcpForm.command;
        const args = this._mcpParseArgs(this.mcpForm.argsText);
        if (args.length) cfg.args = args;
        else if ('args' in cfg && !this.mcpForm.argsText) { /* keep existing from json */ }
        const env = this._mcpMapFromPairs(this.mcpForm.envPairs);
        if (Object.keys(env).length) cfg.env = env;
        else delete cfg.env;
      } else {
        if (this.mcpForm.url) cfg.url = this.mcpForm.url;
        const headers = this._mcpMapFromPairs(this.mcpForm.headerPairs);
        if (Object.keys(headers).length) cfg.headers = headers;
        else delete cfg.headers;
      }
      return cfg;
    },
    _mcpApplyConfigToForm(cfg, name, type) {
      const c = (cfg && typeof cfg === 'object' && !Array.isArray(cfg)) ? cfg : {};
      const t = type || c.type || 'stdio';
      let argsText = '';
      if (Array.isArray(c.args)) {
        try { argsText = JSON.stringify(c.args); } catch (_) { argsText = c.args.join(' '); }
      }
      this.mcpForm = {
        name: name || '',
        type: t,
        jsonConfig: JSON.stringify(c, null, 2),
        envPairs: this._mcpPairsFromMap(c.env),
        headerPairs: this._mcpPairsFromMap(c.headers),
        command: c.command ? String(c.command) : '',
        argsText,
        url: c.url ? String(c.url) : '',
      };
    },
    _mcpSyncJsonFromEditors() {
      if (this.mcpSyncLock) return;
      this.mcpSyncLock = true;
      try {
        let cfg = {};
        try {
          const raw = (this.mcpForm.jsonConfig || '').trim();
          if (raw) cfg = JSON.parse(raw);
        } catch (_) {
          cfg = {};
        }
        if (typeof cfg !== 'object' || Array.isArray(cfg) || cfg === null) cfg = {};
        cfg.type = this.mcpForm.type || 'stdio';
        if (cfg.type === 'stdio') {
          if (this.mcpForm.command) cfg.command = this.mcpForm.command;
          else delete cfg.command;
          const args = this._mcpParseArgs(this.mcpForm.argsText);
          if (args.length) cfg.args = args;
          else delete cfg.args;
          const env = this._mcpMapFromPairs(this.mcpForm.envPairs);
          if (Object.keys(env).length) cfg.env = env;
          else delete cfg.env;
        } else {
          if (this.mcpForm.url) cfg.url = this.mcpForm.url;
          else delete cfg.url;
          const headers = this._mcpMapFromPairs(this.mcpForm.headerPairs);
          if (Object.keys(headers).length) cfg.headers = headers;
          else delete cfg.headers;
        }
        this.mcpForm.jsonConfig = JSON.stringify(cfg, null, 2);
      } finally {
        this.mcpSyncLock = false;
      }
    },
    onMcpPairsChange() { this._mcpSyncJsonFromEditors(); this.validateMcpJson(); },
    onMcpSimpleFieldsChange() { this._mcpSyncJsonFromEditors(); this.validateMcpJson(); },
    onMcpTypeChange() {
      this._mcpSyncJsonFromEditors();
      this.validateMcpJson();
    },
    onMcpJsonChange() {
      if (this.mcpSyncLock) return;
      this.mcpSyncLock = true;
      try {
        const raw = (this.mcpForm.jsonConfig || '').trim();
        if (!raw) {
          this.mcpForm.envPairs = [];
          this.mcpForm.headerPairs = [];
          return;
        }
        let cfg;
        try { cfg = JSON.parse(raw); } catch (_) { return; }
        if (typeof cfg !== 'object' || Array.isArray(cfg) || cfg === null) return;
        if (cfg.type) this.mcpForm.type = cfg.type;
        this.mcpForm.command = cfg.command ? String(cfg.command) : '';
        if (Array.isArray(cfg.args)) {
          try { this.mcpForm.argsText = JSON.stringify(cfg.args); }
          catch (_) { this.mcpForm.argsText = cfg.args.join(' '); }
        } else {
          this.mcpForm.argsText = '';
        }
        this.mcpForm.url = cfg.url ? String(cfg.url) : '';
        this.mcpForm.envPairs = this._mcpPairsFromMap(cfg.env);
        this.mcpForm.headerPairs = this._mcpPairsFromMap(cfg.headers);
      } finally {
        this.mcpSyncLock = false;
      }
      this.validateMcpJson();
    },
    addMcpEnvPair() {
      this.mcpForm.envPairs.push({ key: '', value: '' });
      this.onMcpPairsChange();
    },
    removeMcpEnvPair(idx) {
      this.mcpForm.envPairs.splice(idx, 1);
      this.onMcpPairsChange();
    },
    addMcpHeaderPair() {
      this.mcpForm.headerPairs.push({ key: '', value: '' });
      this.onMcpPairsChange();
    },
    removeMcpHeaderPair(idx) {
      this.mcpForm.headerPairs.splice(idx, 1);
      this.onMcpPairsChange();
    },

    validateMcpJson() {
      // Does not rebuild jsonConfig — pair/simple editors call _mcpSyncJsonFromEditors
      // first so free-form advanced JSON is not clobbered mid-edit.
      this.mcpJsonError = ''; this.mcpJsonWarning = ''; this.mcpPatternPreview = []; this.mcpEnvRefPreview = [];
      const nm = this.mcpForm.name.trim();
      if (nm && !/^[A-Za-z0-9._@-]+$/.test(nm)) this.mcpJsonError = 'invalid name: letters, digits, ._@- only';
      const raw = this.mcpForm.jsonConfig.trim();
      if (raw) {
        let cfg;
        try { cfg = JSON.parse(raw); }
        catch(e) { this.mcpJsonError = 'invalid JSON: ' + e.message; return; }
        if (typeof cfg !== 'object' || Array.isArray(cfg)) { this.mcpJsonError = 'config must be a JSON object'; return; }
        const t = cfg.type || this.mcpForm.type || 'stdio';
        if (t === 'stdio' && !cfg.command) this.mcpJsonWarning = "stdio requires 'command'";
        if (['sse','http','streamable-http'].includes(t) && !cfg.url) this.mcpJsonWarning = t + " requires 'url'";
        for (const field of ['env', 'headers']) {
          if (!(field in cfg)) continue;
          if (typeof cfg[field] !== 'object' || Array.isArray(cfg[field]) || cfg[field] === null) {
            this.mcpJsonError = "'" + field + "' must be an object of string values";
            return;
          }
          for (const [k, v] of Object.entries(cfg[field])) {
            if (!k || typeof k !== 'string') { this.mcpJsonError = "'" + field + "' keys must be non-empty strings"; return; }
            if (typeof v !== 'string') { this.mcpJsonError = "'" + field + "' values must be strings (key " + k + ")"; return; }
          }
        }
        this.mcpEnvRefPreview = this._mcpCollectEnvRefs(cfg);
      }
      if (nm && !this.mcpJsonError) this.mcpPatternPreview = ['mcp__' + nm.replace(/-/g,'_') + '__*'];
    },

    async createMcpServer() {
      const name = this.mcpForm.name.trim();
      if (!name || this.mcpJsonError) return;
      this._mcpSyncJsonFromEditors();
      let config;
      try { config = this._mcpBuildConfigFromForm(); }
      catch(e){ this.mcpJsonError='invalid JSON'; return; }
      if (config && typeof config === 'object' && !Array.isArray(config) && !config.type) config.type = this.mcpForm.type;
      this.mcpBusy = true;
      try {
        const r = await this.api('/admin/api/mcp-servers', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ name, config }) });
        if (r.ok) { this.showToast('MCP SERVER ADDED: ' + name, 'ok'); this.resetMcpForm(); await this.refreshMcp(); }
        else { const d = await r.json().catch(()=>({})); this.showToast(d.error || 'Add failed', 'err'); }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.mcpBusy = false; }
    },
    editMcpServer(s) {
      this.mcpEditName = s.name;
      // A type-less stored config surfaces as type "unknown" (admin_service
      // default); the backend treats it as stdio. Bind a real select option so
      // the on-save type injection can't produce an invalid "unknown" type.
      const t = (s.type && s.type !== 'unknown') ? s.type : 'stdio';
      this._mcpApplyConfigToForm(s.config ?? {}, s.name, t);
      this.mcpShowAdvanced = false;
      this.validateMcpJson();
    },
    cancelMcpEdit() { this.mcpEditName = null; this.resetMcpForm(); },
    resetMcpForm() {
      this.mcpForm = this._mcpEmptyForm();
      this.mcpJsonError=''; this.mcpJsonWarning=''; this.mcpPatternPreview=[]; this.mcpEnvRefPreview=[];
      this.mcpShowAdvanced = false;
    },
    async saveMcpServer() {
      if (!this.mcpEditName || this.mcpJsonError) return;
      this._mcpSyncJsonFromEditors();
      let config;
      try { config = this._mcpBuildConfigFromForm(); }
      catch(e){ this.mcpJsonError='invalid JSON'; return; }
      if (config && typeof config === 'object' && !Array.isArray(config) && !config.type) config.type = this.mcpForm.type;
      this.mcpBusy = true;
      try {
        const r = await this.api('/admin/api/mcp-servers/' + encodeURIComponent(this.mcpEditName), {
          method: 'PUT', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ name: this.mcpEditName, config }) });
        if (r.ok) { this.showToast('MCP SERVER SAVED', 'ok'); this.mcpEditName=null; this.resetMcpForm(); await this.refreshMcp(); }
        else { const d = await r.json().catch(()=>({})); this.showToast(d.error || 'Save failed', 'err'); }
      } catch(e) { this.showToast('Connection error', 'err'); }
      finally { this.mcpBusy = false; }
    },
    async deleteMcpServer(name) {
      if (!confirm('Delete MCP server "' + name + '"?')) return;
      try {
        const r = await this.api('/admin/api/mcp-servers/' + encodeURIComponent(name), { method: 'DELETE' });
        if (r.ok) { this.showToast('MCP SERVER DELETED', 'ok'); await this.refreshMcp(); }
        else { const d = await r.json().catch(()=>({})); this.showToast(d.error || 'Delete failed', 'err'); }
      } catch(e) { this.showToast('Connection error', 'err'); }
    },
    async testMcpServer(name) {
      this.mcpTestBusy[name] = true;
      try {
        const r = await this.api('/admin/api/mcp-servers/' + encodeURIComponent(name) + '/test', { method: 'POST' });
        const d = await r.json().catch(()=>({}));
        const agent = d.agent?.message ? ' · Agent: ' + d.agent.message : '';
        if (r.ok && d.ok) { this.mcpTestResult[name] = {ok:!!d.agent?.usable, message:(d.detail||'reachable')+' ('+(d.latency_ms||0)+'ms)' + agent}; this.showToast(d.agent?.usable ? 'MCP OK: '+name : 'MCP REACHABLE, AGENT BLOCKED: '+name, d.agent?.usable ? 'ok' : 'err'); }
        else { this.mcpTestResult[name] = {ok:false, message: (d.detail || d.error || 'unreachable') + agent}; this.showToast('MCP FAIL: '+name, 'err'); }
      } catch(e) { this.mcpTestResult[name] = {ok:false, message:'connection error'}; this.showToast('Connection error', 'err'); }
      finally { delete this.mcpTestBusy[name]; }
    },

    // --- Plugin MCP credential overlay ---
    editPluginMcpOverlay(s) {
      this.mcpOverlayName = s.name;
      this.mcpOverlayPluginId = s.plugin || null;
      this.mcpOverlayHadExisting = !!s.has_overlay;
      const ov = s.overlay || {};
      this.mcpOverlayForm = {
        envPairs: this._mcpPairsFromMap(ov.env),
        headerPairs: this._mcpPairsFromMap(ov.headers),
      };
      if (!this.mcpOverlayForm.envPairs.length && !this.mcpOverlayForm.headerPairs.length) {
        this.mcpOverlayForm.envPairs = [{ key: '', value: '' }];
      }
      // Scroll overlay card into view after Alpine paints.
      this.$nextTick && this.$nextTick(() => {
        const el = document.querySelector('[x-show="mcpOverlayName"]');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
    },
    cancelPluginMcpOverlay() {
      this.mcpOverlayName = null;
      this.mcpOverlayPluginId = null;
      this.mcpOverlayHadExisting = false;
      this.mcpOverlayForm = { envPairs: [], headerPairs: [] };
    },
    async savePluginMcpOverlay() {
      if (!this.mcpOverlayName) return;
      const env = this._mcpMapFromPairs(this.mcpOverlayForm.envPairs);
      const headers = this._mcpMapFromPairs(this.mcpOverlayForm.headerPairs);
      if (!Object.keys(env).length && !Object.keys(headers).length) {
        this.showToast('Add at least one env or header entry', 'err');
        return;
      }
      this.mcpOverlayBusy = true;
      try {
        const body = { env, headers };
        if (this.mcpOverlayPluginId) body.plugin_id = this.mcpOverlayPluginId;
        const r = await this.api(
          '/admin/api/mcp-servers/' + encodeURIComponent(this.mcpOverlayName) + '/plugin-overlay',
          { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
        );
        if (r.ok) {
          this.showToast('PLUGIN CREDENTIALS SAVED: ' + this.mcpOverlayName, 'ok');
          this.cancelPluginMcpOverlay();
          await this.refreshMcp();
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Save failed', 'err');
        }
      } catch (e) {
        this.showToast('Connection error', 'err');
      } finally {
        this.mcpOverlayBusy = false;
      }
    },
    async clearPluginMcpOverlay() {
      if (!this.mcpOverlayName) return;
      if (!confirm('Clear credential overlay for "' + this.mcpOverlayName + '"?')) return;
      this.mcpOverlayBusy = true;
      try {
        const r = await this.api(
          '/admin/api/mcp-servers/' + encodeURIComponent(this.mcpOverlayName) + '/plugin-overlay',
          { method: 'DELETE' }
        );
        if (r.ok) {
          this.showToast('PLUGIN CREDENTIALS CLEARED', 'ok');
          this.cancelPluginMcpOverlay();
          await this.refreshMcp();
        } else {
          const d = await r.json().catch(() => ({}));
          this.showToast(d.error || 'Clear failed', 'err');
        }
      } catch (e) {
        this.showToast('Connection error', 'err');
      } finally {
        this.mcpOverlayBusy = false;
      }
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

    formatKstTime(t) {
      if (!t) return '-';
      try {
        return new Date(t).toLocaleString('ko-KR', {
          hour12: false,
          timeZone: 'Asia/Seoul'
        });
      } catch(e) { return t; }
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
