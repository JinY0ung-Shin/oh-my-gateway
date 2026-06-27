"""Admin plugins tab HTML (manage marketplaces + plugins at runtime)."""


def get_plugins_html() -> str:
    """Return the admin plugins management tab html."""
    return """      <!-- Plugins Tab -->
      <div x-show="tab==='plugins'" role="tabpanel">
        <p class="text-xs text-muted" style="margin:0 0 12px 0">
          // admin-managed layer on top of the CLAUDE_PLUGIN_* env bootstrap. changes persist via the gateway manifest.
        </p>
        <div class="grid-2">

          <!-- Browse / Catalog section -->
          <div class="card">
            <div class="flex-between mb-sm">
              <h3 style="margin:0; color:var(--cyan)">Browse Marketplaces</h3>
              <button class="btn btn-sm btn-ghost" @click="loadCatalog()" :disabled="catalogLoading" aria-label="Reload catalog">
                <span x-text="catalogLoading ? 'LOADING...' : '[RELOAD]'"></span>
              </button>
            </div>

            <!-- Search / filter across all marketplaces -->
            <div class="flex-gap-sm mb-sm" style="align-items:center">
              <input type="text" x-model="catalogFilter" placeholder="filter plugins by name / description..."
                style="flex:1" aria-label="Filter catalog plugins">
              <button x-show="catalogFilter" class="btn btn-sm btn-ghost" @click="catalogFilter=''" aria-label="Clear filter">[X]</button>
            </div>

            <div class="file-tree" style="max-height:62vh; overflow-y:auto">
              <template x-for="m in marketplaces" :key="m.name">
                <div x-show="marketplaceVisible(m)">
                  <!-- Marketplace group header (expandable) -->
                  <div class="file-item" style="cursor:pointer" @click="toggleMarketplace(m)">
                    <span class="text-xs" style="color:var(--amber); margin-right:4px"
                      x-text="(m._expanded || catalogFilter) ? '[-]' : '[+]'"></span>
                    <div style="flex:1; min-width:0">
                      <div class="flex-gap-sm" style="align-items:center">
                        <span style="font-size:var(--fs-sm); font-weight:600; color:var(--amber)" x-text="m.name"></span>
                        <span class="badge text-xs" x-text="m.source_type"></span>
                      </div>
                      <div class="text-mono text-xs" style="color:var(--text-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap"
                        :title="m.repo" x-text="m.repo"></div>
                    </div>
                    <span class="text-xs text-muted" style="margin-right:8px" x-text="m.plugin_count + ' plugins'"></span>
                    <button class="btn btn-sm btn-ghost" style="color:var(--red)"
                      @click.stop="removeMarketplace(m.name)" aria-label="Remove marketplace">[DEL]</button>
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
                                style="border-color:var(--green); color:var(--green)">INSTALLED</span>
                            </div>
                            <div x-show="plug.description" class="text-xs text-muted"
                              style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis" x-text="plug.description"></div>
                          </div>
                          <button x-show="!plug.installed" class="btn btn-sm btn-primary"
                            @click="installFromCatalog(m, plug)" :disabled="catalogBusy[plug.id]"
                            aria-label="Install plugin">
                            <span x-text="catalogBusy[plug.id] ? '...' : 'INSTALL'"></span>
                          </button>
                          <button x-show="plug.installed" class="btn btn-sm btn-ghost" style="color:var(--red)"
                            @click="uninstallFromCatalog(plug)" :disabled="catalogBusy[plug.id]"
                            aria-label="Uninstall plugin">
                            <span x-text="catalogBusy[plug.id] ? '...' : 'UNINSTALL'"></span>
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
                [ NO MARKETPLACES ]
              </div>
              <div x-show="marketplaces.length > 0 && catalogVisibleCount() === 0" class="text-sm text-muted" style="padding:4px 12px">
                [ NO PLUGINS MATCH FILTER ]
              </div>
            </div>

            <!-- Add marketplace (collapsible) -->
            <details style="margin-top:12px">
              <summary class="text-xs text-dim" style="cursor:pointer">+ add marketplace</summary>
              <form @submit.prevent="addMarketplace()" style="margin-top:8px">
                <input type="text" x-model="mpForm.repo" placeholder="https://github.com/owner/repo (or local path)"
                  style="width:100%; margin-bottom:8px" required>
                <div class="flex-gap-sm" style="margin-bottom:8px">
                  <input type="text" x-model="mpForm.branch" placeholder="branch (main)" style="flex:1">
                  <select x-model="mpForm.scope" style="flex:1">
                    <option value="user">user</option>
                    <option value="project">project</option>
                  </select>
                </div>
                <input type="password" x-model="mpForm.git_token" placeholder="git token (optional, private repos)"
                  style="width:100%; margin-bottom:8px">
                <button class="btn btn-sm btn-primary" type="submit" :disabled="pluginBusy" style="width:100%">
                  <span x-text="pluginBusy ? 'WORKING...' : 'ADD MARKETPLACE'"></span>
                </button>
              </form>
            </details>
          </div>

          <!-- Installed Plugins section -->
          <div class="card">
            <div class="flex-between mb-sm">
              <h3 style="margin:0; color:var(--cyan)">Installed Plugins</h3>
              <button class="btn btn-sm btn-ghost" @click="loadPlugins()" aria-label="Refresh plugins">[RELOAD]</button>
            </div>

            <div class="table-wrapper mb-md">
              <table>
                <thead><tr><th>PLUGIN</th><th>ORIGIN</th><th>VERSION</th><th>SKILLS</th><th></th></tr></thead>
                <tbody>
                  <template x-for="p in plugins" :key="p.id">
                    <tr>
                      <td style="color:var(--amber); font-weight:600" x-text="p.name"></td>
                      <td>
                        <span class="badge text-xs" x-show="p.origin === 'managed'"
                          style="border-color:var(--cyan); color:var(--cyan)">MANAGED</span>
                        <span class="badge text-xs" x-show="p.origin !== 'managed'"
                          style="border-color:var(--amber); color:var(--amber); opacity:0.7">ENV</span>
                      </td>
                      <td class="text-xs text-muted" x-text="p.version ? 'v' + p.version : '-'"></td>
                      <td class="text-xs text-muted" x-text="(p.skills?.length || 0)"></td>
                      <td>
                        <button class="btn btn-sm btn-ghost" style="color:var(--red)"
                          @click="uninstallPlugin(p.id, p.scope)" aria-label="Uninstall plugin">[DEL]</button>
                      </td>
                    </tr>
                  </template>
                  <tr x-show="plugins.length === 0">
                    <td colspan="5" class="text-sm text-muted">[ NO PLUGINS ]</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- Advanced: install by name (de-emphasized; browse+click is primary) -->
            <details>
              <summary class="text-xs text-dim" style="cursor:pointer">advanced: install by name</summary>
              <form @submit.prevent="installPlugin()" style="margin-top:8px">
                <input type="text" x-model="pluginForm.name" placeholder="plugin name"
                  style="width:100%; margin-bottom:8px" required>
                <div class="flex-gap-sm" style="margin-bottom:8px">
                  <select x-model="pluginForm.marketplace" style="flex:1">
                    <option value="">(marketplace)</option>
                    <template x-for="m in marketplaces" :key="m.name">
                      <option :value="m.name" x-text="m.name"></option>
                    </template>
                  </select>
                  <select x-model="pluginForm.scope" style="flex:1">
                    <option value="user">user</option>
                    <option value="project">project</option>
                  </select>
                </div>
                <button class="btn btn-sm btn-primary" type="submit" :disabled="pluginBusy" style="width:100%">
                  <span x-text="pluginBusy ? 'WORKING...' : 'INSTALL PLUGIN'"></span>
                </button>
              </form>
            </details>
          </div>

        </div>
      </div>"""
