"""Admin plugins tab HTML (manage marketplaces + plugins at runtime)."""


def get_plugins_html() -> str:
    """Return the admin plugins management tab html."""
    return """      <!-- Plugins Tab -->
      <div x-show="tab==='plugins'" role="tabpanel">
        <p class="text-xs text-muted" style="margin:0 0 12px 0">
          Admin-managed layer on top of the CLAUDE_PLUGIN_* environment bootstrap. Changes persist via the gateway manifest.
        </p>

        <!-- Marketplace auto-refresh (periodic re-clone + plugin update) -->
        <div class="card" style="margin-bottom:12px">
          <div class="flex-between mb-sm">
            <h3 style="margin:0; color:var(--cyan)">Marketplace Auto-Refresh</h3>
            <span class="badge text-xs"
              :style="autoRefresh.enabled ? 'border-color:var(--green); color:var(--green)' : 'border-color:var(--border-bright); color:var(--text-dim)'"
              x-text="autoRefresh.running ? 'RUNNING' : (autoRefresh.enabled ? 'ON' : 'OFF')"></span>
          </div>
          <div class="flex-gap-sm" style="align-items:center; flex-wrap:wrap">
            <label class="text-sm" style="display:flex; align-items:center; gap:6px; cursor:pointer">
              <input type="checkbox" x-model="autoRefresh.enabled" aria-label="Enable marketplace auto-refresh">
              Enabled
            </label>
            <label class="text-sm" style="display:flex; align-items:center; gap:6px">
              every
              <input type="number" x-model.number="autoRefresh.interval_minutes" min="5" max="10080"
                style="width:80px" aria-label="Auto-refresh interval in minutes">
              min
            </label>
            <button class="btn btn-sm btn-primary" @click="saveAutoRefresh()" :disabled="autoRefreshBusy"
              aria-label="Save auto-refresh settings">
              <span x-text="autoRefreshBusy ? 'Working...' : 'Save'"></span>
            </button>
            <button class="btn btn-sm btn-ghost" @click="runAutoRefreshNow()"
              :disabled="autoRefreshBusy || autoRefresh.running" aria-label="Run refresh cycle now">
              <span x-text="autoRefresh.running ? 'Refreshing...' : 'Run now'"></span>
            </button>
            <span class="text-xs text-muted" x-text="autoRefreshSummary()"></span>
          </div>
          <p class="text-xs text-muted" style="margin:8px 0 0 0">
            Periodically re-clones every gateway-managed marketplace and runs <code>claude plugin update</code> for its installed plugins — the same path as the per-marketplace Refresh button. New sessions pick up updated plugins.
          </p>
        </div>

        <div class="grid-2">

          <!-- Browse / Catalog section -->
          <div class="card">
            <div class="flex-between mb-sm">
              <h3 style="margin:0; color:var(--cyan)">Browse Marketplaces</h3>
              <button class="btn btn-sm btn-ghost" @click="loadCatalog()" :disabled="catalogLoading" aria-label="Reload catalog">
                <span x-text="catalogLoading ? 'Loading...' : 'Reload'"></span>
              </button>
            </div>

            <!-- Search / filter across all marketplaces -->
            <div class="flex-gap-sm mb-sm" style="align-items:center">
              <input type="text" x-model="catalogFilter" placeholder="filter plugins by name / description..."
                style="flex:1" aria-label="Filter catalog plugins">
              <button x-show="catalogFilter" class="btn btn-sm btn-ghost" @click="catalogFilter=''" aria-label="Clear filter">Clear</button>
            </div>

            <div class="file-tree" style="max-height:62vh; overflow-y:auto">
              <template x-for="m in marketplaces" :key="m.name">
                <div x-show="marketplaceVisible(m)">
                  <!-- Marketplace group header (expandable) -->
                  <div class="file-item marketplace-row" style="cursor:pointer" @click="toggleMarketplace(m)">
                    <span class="text-xs" style="color:var(--amber); margin-right:4px"
                      x-text="(m._expanded || catalogFilter) ? '-' : '+'"></span>
                    <div class="marketplace-main" style="flex:1; min-width:0">
                      <div class="flex-gap-sm" style="align-items:center">
                        <span style="font-size:var(--fs-sm); font-weight:600; color:var(--amber)" x-text="m.name"></span>
                        <span class="badge text-xs" x-text="m.source_type"></span>
                        <span class="badge text-xs" x-show="m.branch"
                          style="border-color:var(--border-bright); color:var(--text-dim)"
                          x-text="m.branch"></span>
                      </div>
                      <div class="text-mono text-xs" style="color:var(--text-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap"
                        :title="m.repo" x-text="m.repo"></div>
                    </div>
                    <span class="text-xs text-muted marketplace-count" style="margin-right:8px" x-text="m.plugin_count + ' plugins'"></span>
                    <input x-show="m._expanded || catalogFilter" type="password"
                      x-model="marketplaceTokens[m.name]" placeholder="refresh token"
                      aria-label="One-time refresh token"
                      @click.stop
                      style="width:140px; margin-right:4px">
                    <button class="btn btn-sm btn-ghost marketplace-refresh"
                      @click.stop="refreshMarketplace(m)" :disabled="!!marketplaceBusy[m.name]"
                      aria-label="Refresh marketplace">
                      <span x-text="marketplaceBusy[m.name] ? 'Refreshing...' : 'Refresh'"></span>
                    </button>
                    <button class="btn btn-sm btn-ghost marketplace-remove" style="color:var(--red)"
                      @click.stop="removeMarketplace(m.name, m.scope)" aria-label="Remove marketplace">Remove</button>
                  </div>

                  <!-- Catalog plugins (expandable; force-open while filtering) -->
                  <template x-if="m._expanded || catalogFilter">
                    <div>
                      <template x-for="plug in filteredCatalogPlugins(m)" :key="plug.id">
                        <div class="file-item" style="padding-left:28px; align-items:flex-start">
                          <span class="icon" style="color:var(--cyan); margin-top:2px">&#9670;</span>
                          <div style="flex:1; min-width:0">
                            <div class="flex-gap-sm" style="align-items:center">
                              <span style="font-size:var(--fs-sm); color:var(--text); font-weight:600" x-text="plug.name"></span>
                              <span class="text-xs text-muted" x-text="plug.version ? 'v' + plug.version : ''"></span>
                              <span class="text-xs text-muted" x-text="(plug.skill_count || 0) + ' skills'"></span>
                              <span x-show="plug.installed" class="badge text-xs"
                                style="border-color:var(--green); color:var(--green)">Installed</span>
                            </div>
                            <div x-show="plug.description" class="text-xs text-muted"
                              style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis" x-text="plug.description"></div>
                          </div>
                          <button x-show="!plug.installed" class="btn btn-sm btn-primary"
                            @click="installFromCatalog(m, plug)" :disabled="!!catalogBusy[plug.id]"
                            aria-label="Install plugin">
                            <span x-text="catalogBusy[plug.id] ? 'Working...' : 'Install'"></span>
                          </button>
                          <button x-show="plug.installed" class="btn btn-sm btn-ghost" style="color:var(--red)"
                            @click="uninstallFromCatalog(plug)" :disabled="!!catalogBusy[plug.id]"
                            aria-label="Uninstall plugin">
                            <span x-text="catalogBusy[plug.id] ? 'Working...' : 'Uninstall'"></span>
                          </button>
                        </div>
                      </template>
                      <div x-show="filteredCatalogPlugins(m).length === 0" class="text-xs text-muted" style="padding:4px 12px 4px 28px">
                        (no plugins)
                      </div>
                    </div>
                  </template>
                </div>
              </template>
              <div x-show="marketplaces.length === 0" class="text-sm text-muted" style="padding:4px 12px">
                No marketplaces
              </div>
              <div x-show="marketplaces.length > 0 && catalogVisibleCount() === 0" class="text-sm text-muted" style="padding:4px 12px">
                No plugins match the filter
              </div>
            </div>

            <!-- Add marketplace (collapsible) -->
            <details style="margin-top:12px">
              <summary class="text-xs text-dim" style="cursor:pointer">+ add marketplace</summary>
              <form @submit.prevent="addMarketplace()" style="margin-top:8px">
                <input type="text" x-model="mpForm.repo" placeholder="https://github.com/owner/repo (or local path)"
                  aria-label="Marketplace repository"
                  style="width:100%; margin-bottom:8px" required>
                <div class="flex-gap-sm" style="margin-bottom:8px">
                  <input type="text" x-model="mpForm.branch" placeholder="branch (main)" aria-label="Marketplace branch" style="flex:1">
                  <select x-model="mpForm.scope" aria-label="Marketplace install scope" style="flex:1">
                    <option value="user">user</option>
                    <option value="project">project</option>
                  </select>
                </div>
                <input type="password" x-model="mpForm.git_token" placeholder="git token (optional, private repos)"
                  aria-label="Git token"
                  style="width:100%; margin-bottom:8px">
                <button class="btn btn-sm btn-primary" type="submit" :disabled="pluginBusy" style="width:100%">
                  <span x-text="pluginBusy ? 'Working...' : 'Add marketplace'"></span>
                </button>
              </form>
            </details>
          </div>

          <!-- Installed Plugins section -->
          <div class="card">
            <div class="flex-between mb-sm">
              <h3 style="margin:0; color:var(--cyan)">Installed Plugins</h3>
              <button class="btn btn-sm btn-ghost" @click="loadPlugins()" aria-label="Refresh plugins">Reload</button>
            </div>

            <div class="table-wrapper mb-md">
              <table>
                <thead><tr><th>PLUGIN</th><th>SCOPE</th><th>ORIGIN</th><th>VERSION</th><th>CAPS</th><th></th></tr></thead>
                <template x-for="p in plugins" :key="p.id + '@' + p.scope">
                  <tbody>
                    <tr>
                      <td>
                        <button class="btn btn-sm btn-ghost" style="padding:1px 6px; margin-right:6px"
                          @click="p._expanded = !p._expanded"
                          :aria-expanded="p._expanded ? 'true' : 'false'"
                          aria-label="Toggle plugin skills">
                          <span x-text="p._expanded ? '-' : '+'"></span>
                        </button>
                        <span style="color:var(--amber); font-weight:600" x-text="p.name"></span>
                      </td>
                      <td class="text-xs text-muted" x-text="p.scope || 'user'"></td>
                      <td>
                        <span class="badge text-xs" x-show="p.origin === 'managed'"
                          style="border-color:var(--cyan); color:var(--cyan)">MANAGED</span>
                        <span class="badge text-xs" x-show="p.origin !== 'managed'"
                          style="border-color:var(--amber); color:var(--amber); opacity:0.7">ENV</span>
                      </td>
                      <td class="text-xs text-muted" x-text="p.version ? 'v' + p.version : '-'"></td>
                      <td class="text-xs text-muted"
                        x-text="'S:' + (p.skills?.length || 0) + ' A:' + (p.agents?.length || 0) + ' M:' + (p.mcp_servers?.length || 0)"></td>
                      <td>
                        <button class="btn btn-sm btn-ghost" style="color:var(--red)"
                          @click="uninstallPlugin(p.id, p.scope)" aria-label="Uninstall plugin">Delete</button>
                      </td>
                    </tr>
                    <tr x-show="p._expanded">
                      <td colspan="6" style="padding:0.75rem 1rem; background:var(--bg-surface)">
                        <div class="flex-between mb-sm">
                          <div>
                            <div class="text-xs text-muted">CAPABILITIES</div>
                            <div class="text-sm" style="color:var(--text-bright)"
                              x-text="(p.skills?.length || 0) + ' skills / ' + (p.agents?.length || 0) + ' agents / ' + (p.mcp_servers?.length || 0) + ' MCP'"></div>
                          </div>
                          <span class="text-xs text-muted" x-text="p.id"></span>
                        </div>
                        <div class="text-xs text-muted mb-sm">SKILLS</div>
                        <div class="file-tree" style="max-height:240px; overflow-y:auto">
                          <template x-for="sk in (p.skills || [])" :key="p.id + '@' + p.scope + ':' + sk.name">
                            <div class="file-item"
                              :class="{ active: pluginSkillView && pluginSkillView.pluginId === p.id && pluginSkillView.scope === p.scope && pluginSkillView.skillName === sk.name }">
                              <span class="icon" style="color:var(--cyan)">&#9670;</span>
                              <div style="flex:1; min-width:0">
                                <div class="flex-gap-sm" style="align-items:center">
                                  <span style="font-size:var(--fs-sm); color:var(--text); font-weight:600" x-text="sk.name"></span>
                                  <span class="text-xs text-muted text-mono" x-show="sk.path" x-text="sk.path"></span>
                                </div>
                                <div x-show="sk.description" class="text-xs text-muted"
                                  style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis"
                                  x-text="sk.description"></div>
                              </div>
                              <button class="btn btn-sm btn-ghost" @click="openPluginSkill(p, sk.name)"
                                aria-label="View plugin skill">View</button>
                            </div>
                          </template>
                          <div x-show="!p.skills || p.skills.length === 0" class="text-xs text-muted" style="padding:4px 12px">
                            (no skills)
                          </div>
                        </div>
                        <div class="text-xs text-muted mb-sm" style="margin-top:0.75rem">AGENTS</div>
                        <div class="file-tree">
                          <template x-for="agent in (p.agents || [])" :key="p.id + '@' + p.scope + ':agent:' + agent.name">
                            <div class="file-item">
                              <span class="icon" style="color:var(--magenta)">&#9670;</span>
                              <div style="flex:1; min-width:0">
                                <div style="font-size:var(--fs-sm); color:var(--text); font-weight:600" x-text="agent.name"></div>
                                <div class="text-xs text-muted text-mono" x-show="agent.path"
                                  style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis"
                                  x-text="agent.path"></div>
                              </div>
                            </div>
                          </template>
                          <div x-show="!p.agents || p.agents.length === 0" class="text-xs text-muted" style="padding:4px 12px">
                            (no agents)
                          </div>
                        </div>
                        <div class="text-xs text-muted mb-sm" style="margin-top:0.75rem">MCP SERVERS</div>
                        <div class="file-tree">
                          <template x-for="server in (p.mcp_servers || [])" :key="p.id + '@' + p.scope + ':mcp:' + server.name">
                            <div class="file-item">
                              <span class="icon" style="color:var(--green)">&#9670;</span>
                              <div style="flex:1; min-width:0">
                                <div class="flex-gap-sm" style="align-items:center">
                                  <span style="font-size:var(--fs-sm); color:var(--text); font-weight:600" x-text="server.name"></span>
                                  <span class="badge text-xs" x-text="server.type || 'unknown'"></span>
                                </div>
                              </div>
                            </div>
                          </template>
                          <div x-show="!p.mcp_servers || p.mcp_servers.length === 0" class="text-xs text-muted" style="padding:4px 12px">
                            (no MCP servers)
                          </div>
                        </div>
                        <template x-if="pluginSkillView && pluginSkillView.pluginId === p.id && pluginSkillView.scope === p.scope">
                          <div style="margin-top:0.75rem">
                            <div class="editor-toolbar">
                              <div class="flex-gap-sm">
                                <span style="color:var(--cyan); font-weight:600"
                                  x-text="pluginSkillView.pluginName + ':' + pluginSkillView.skillName"></span>
                                <span class="badge text-xs" style="border-color:var(--amber); color:var(--amber)">SKILL</span>
                                <span class="text-xs" style="color:var(--text-dim)"
                                  x-text="pluginSkillView.version ? 'v' + pluginSkillView.version : ''"></span>
                              </div>
                              <span class="text-xs text-muted" x-text="(pluginSkillView.content?.length || 0) + ' chars'"></span>
                            </div>
                            <textarea readonly :value="pluginSkillView.content || ''" class="readonly-editor"
                              style="min-height:220px; max-height:34vh"></textarea>
                          </div>
                        </template>
                      </td>
                    </tr>
                  </tbody>
                </template>
                <tbody>
                  <tr x-show="plugins.length === 0">
                    <td colspan="6" class="text-sm text-muted">No plugins</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- Advanced: install by name (de-emphasized; browse+click is primary) -->
            <details>
              <summary class="text-xs text-dim" style="cursor:pointer">advanced: install by name</summary>
              <form @submit.prevent="installPlugin()" style="margin-top:8px">
                <input type="text" x-model="pluginForm.name" placeholder="plugin name" aria-label="Plugin name"
                  style="width:100%; margin-bottom:8px" required>
                <div class="flex-gap-sm" style="margin-bottom:8px">
                  <select x-model="pluginForm.marketplace" aria-label="Plugin marketplace" style="flex:1">
                    <option value="">(marketplace)</option>
                    <template x-for="m in marketplaces" :key="m.name">
                      <option :value="m.name" x-text="m.name"></option>
                    </template>
                  </select>
                  <select x-model="pluginForm.scope" aria-label="Plugin install scope" style="flex:1">
                    <option value="user">user</option>
                    <option value="project">project</option>
                  </select>
                </div>
                <button class="btn btn-sm btn-primary" type="submit" :disabled="pluginBusy" style="width:100%">
                  <span x-text="pluginBusy ? 'Working...' : 'Install plugin'"></span>
                </button>
              </form>
            </details>
          </div>

        </div>
      </div>"""
