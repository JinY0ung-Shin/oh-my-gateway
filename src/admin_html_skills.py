"""Admin skills tab HTML (read-only plugin skills viewer)."""


def get_skills_html() -> str:
    """Return the admin skills tab html."""
    return """      <!-- Skills Tab -->
      <div x-show="tab==='skills'" role="tabpanel">
        <div class="sidebar">
          <div class="file-tree card" style="max-height:80vh; overflow-y:auto">
            <div class="flex-between mb-sm">
              <h3 style="margin:0">Plugin Skills</h3>
            </div>
            <p class="text-xs text-muted" style="padding-left:12px; margin:0 0 8px 0">Read-only. Managed by the CLI plugin system.</p>

            <!-- Plugin skills section -->
            <template x-for="p in plugins" :key="p.id + '@' + p.scope">
              <div>
                <!-- Plugin group header -->
                <div class="file-item" style="cursor:pointer; opacity:0.8" @click="p._expanded = !p._expanded">
                  <span class="text-xs" style="color:var(--amber); margin-right:4px" x-text="p._expanded ? '-' : '+'"></span>
                  <div style="flex:1; min-width:0">
                    <div class="flex-gap-sm">
                      <span style="font-size:var(--fs-sm); font-weight:600; color:var(--amber)" x-text="p.name"></span>
                      <span class="text-xs text-muted" x-text="p.scope || 'user'"></span>
                      <span class="text-xs text-muted" x-text="p.version ? 'v' + p.version : ''"></span>
                    </div>
                    <div class="text-xs text-muted" x-text="(p.skills?.length || 0) + ' skills'"></div>
                  </div>
                </div>
                <!-- Plugin skills (expandable) -->
                <template x-if="p._expanded">
                  <div>
                    <template x-for="sk in (p.skills || [])" :key="p.id + '@' + p.scope + ':' + sk.name">
                      <div class="file-item" style="padding-left:28px"
                        :class="{ active: pluginSkillView && pluginSkillView.pluginId === p.id && pluginSkillView.scope === p.scope && pluginSkillView.skillName === sk.name }"
                        @click="openPluginSkill(p, sk.name)">
                        <span class="icon" style="color:var(--cyan)">&#9670;</span>
                        <div style="flex:1; min-width:0">
                          <div style="font-size:var(--fs-sm); color:var(--text)" x-text="sk.name"></div>
                          <div x-show="sk.description" class="text-xs text-muted" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis"
                            x-text="sk.description"></div>
                        </div>
                      </div>
                    </template>
                    <div x-show="!p.skills || p.skills.length === 0" class="text-xs text-muted" style="padding:4px 12px 4px 28px">
                      (no skills)
                    </div>
                  </div>
                </template>
              </div>
            </template>
            <div x-show="plugins.length === 0" class="text-sm text-muted" style="padding:4px 12px">
              No plugins
            </div>
          </div>
          <div class="editor-area card">
            <!-- No selection -->
            <template x-if="!pluginSkillView">
              <div class="text-muted" style="padding:3rem; text-align:center">
                <div style="font-size:2rem; margin-bottom:0.5rem; opacity:0.2">&#9881;</div>
                No skill selected
              </div>
            </template>
            <!-- Plugin skill read-only view -->
            <template x-if="pluginSkillView">
              <div>
                <div class="editor-toolbar">
                  <div class="flex-gap-sm">
                    <span style="color:var(--cyan); font-weight:600" x-text="pluginSkillView.pluginName + ':' + pluginSkillView.skillName"></span>
                    <span class="badge text-xs" style="border-color:var(--amber); color:var(--amber)">PLUGIN</span>
                    <span class="text-xs" style="color:var(--text-dim)" x-text="pluginSkillView.version ? 'v' + pluginSkillView.version : ''"></span>
                  </div>
                  <span class="text-xs text-muted" x-text="(pluginSkillView.content?.length || 0) + ' chars'"></span>
                </div>
                <p class="text-xs text-muted mb-sm">Read-only. Managed by the CLI plugin system.</p>
                <textarea readonly :value="pluginSkillView.content || ''" class="readonly-editor"></textarea>
              </div>
            </template>
          </div>
        </div>
      </div>"""
