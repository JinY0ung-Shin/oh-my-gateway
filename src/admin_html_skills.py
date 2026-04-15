"""Admin skills tab HTML."""


def get_skills_html() -> str:
    """Return the admin skills tab html."""
    return """      <!-- Skills Tab -->
      <div x-show="tab==='skills'" role="tabpanel">
        <div class="sidebar">
          <div class="file-tree card" style="max-height:80vh; overflow-y:auto">
            <div class="flex-between mb-sm">
              <h3 style="margin:0">Skills</h3>
              <button class="btn btn-sm btn-primary" @click="showNewSkillForm()">[+ NEW]</button>
            </div>

            <!-- Local skills section -->
            <div style="margin-bottom:4px">
              <span class="text-xs text-muted" style="padding-left:12px">// LOCAL</span>
            </div>
            <template x-for="s in skills" :key="s.name">
              <div class="file-item" :class="{ active: selectedSkill === s.name }" @click="openSkill(s.name)">
                <span class="icon" style="color:var(--green)">&#9881;</span>
                <div style="flex:1; min-width:0">
                  <div style="font-size:var(--fs-sm); font-weight:600; color:var(--text-bright)" x-text="s.name"></div>
                  <div class="text-xs text-muted" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis"
                    x-text="s.description || '(no description)'"></div>
                </div>
              </div>
            </template>
            <div x-show="skills.length === 0" class="text-sm text-muted" style="padding:4px 12px">
              [ NONE ]
            </div>

            <!-- Plugin skills section -->
            <div x-show="plugins.length > 0" style="border-top:1px solid var(--border); margin-top:8px; padding-top:8px">
              <span class="text-xs text-muted" style="padding-left:12px">// PLUGINS</span>
            </div>
            <template x-for="p in plugins" :key="p.id">
              <div>
                <!-- Plugin group header -->
                <div class="file-item" style="cursor:pointer; opacity:0.8" @click="p._expanded = !p._expanded">
                  <span class="text-xs" style="color:var(--amber); margin-right:4px" x-text="p._expanded ? '[-]' : '[+]'"></span>
                  <div style="flex:1; min-width:0">
                    <div class="flex-gap-sm">
                      <span style="font-size:var(--fs-sm); font-weight:600; color:var(--amber)" x-text="p.name"></span>
                      <span class="text-xs text-muted" x-text="p.version ? 'v' + p.version : ''"></span>
                    </div>
                    <div class="text-xs text-muted" x-text="(p.skills?.length || 0) + ' skills'"></div>
                  </div>
                </div>
                <!-- Plugin skills (expandable) -->
                <template x-if="p._expanded">
                  <div>
                    <template x-for="sk in (p.skills || [])" :key="p.id + ':' + sk.name">
                      <div class="file-item" style="padding-left:28px"
                        :class="{ active: pluginSkillView && pluginSkillView.pluginId === p.id && pluginSkillView.skillName === sk.name }"
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
          </div>
          <div class="editor-area card">
            <!-- New skill form -->
            <template x-if="skillCreating">
              <div style="padding:1rem">
                <h3 style="margin-top:0">Create Skill</h3>
                <label class="text-xs text-muted">SKILL_NAME:</label>
                <input type="text" x-model="newSkillName" placeholder="my-skill-name"
                  style="width:100%; margin-bottom:0.5rem; margin-top:4px" @input="validateNewSkillName()">
                <p x-show="newSkillNameError" class="text-sm text-danger" style="margin:0 0 0.5rem 0" x-text="'! ' + newSkillNameError"></p>
                <div class="flex-gap-sm">
                  <button class="btn btn-sm btn-primary" @click="createSkill()" :disabled="!newSkillName || newSkillNameError">[CREATE]</button>
                  <button class="btn btn-sm btn-ghost" @click="skillCreating = false">[CANCEL]</button>
                </div>
              </div>
            </template>
            <!-- No selection -->
            <template x-if="!selectedSkill && !skillCreating && !pluginSkillView">
              <div class="text-muted" style="padding:3rem; text-align:center">
                <div style="font-size:2rem; margin-bottom:0.5rem; opacity:0.2">&#9881;</div>
                select a skill or create new_<span class="cursor-blink"></span>
              </div>
            </template>
            <!-- Local skill editor -->
            <template x-if="selectedSkill && !skillCreating && !pluginSkillView">
              <div>
                <div class="editor-toolbar">
                  <div class="flex-gap-sm">
                    <span class="path" style="color:var(--text-bright)" x-text="selectedSkill"></span>
                    <span class="badge text-xs" style="border-color:var(--green-dim); color:var(--green)">LOCAL</span>
                    <span class="text-xs" style="color:var(--text-dim)" x-text="(skillMeta.metadata?.version) ? 'v' + skillMeta.metadata.version : ''"></span>
                  </div>
                  <div class="flex-gap-sm">
                    <span x-show="skillDirty" class="dirty-dot" title="Unsaved changes"></span>
                    <span x-show="skillDirty" class="text-xs text-muted">Ctrl+S</span>
                    <button class="btn btn-sm btn-primary" @click="saveSkill()" :disabled="!skillDirty">[SAVE]</button>
                  </div>
                </div>
                <textarea x-ref="skillEditorArea" style="display:none"></textarea>
                <div style="margin-top:0.75rem; text-align:right">
                  <button class="btn btn-sm btn-danger-ghost" @click="confirmDeleteSkill()">[DELETE SKILL]</button>
                </div>
              </div>
            </template>
            <!-- Plugin skill read-only view -->
            <template x-if="pluginSkillView && !skillCreating">
              <div>
                <div class="editor-toolbar">
                  <div class="flex-gap-sm">
                    <span style="color:var(--cyan); font-weight:600" x-text="pluginSkillView.pluginName + ':' + pluginSkillView.skillName"></span>
                    <span class="badge text-xs" style="border-color:var(--amber); color:var(--amber)">PLUGIN</span>
                    <span class="text-xs" style="color:var(--text-dim)" x-text="pluginSkillView.version ? 'v' + pluginSkillView.version : ''"></span>
                  </div>
                  <span class="text-xs text-muted" x-text="(pluginSkillView.content?.length || 0) + ' chars'"></span>
                </div>
                <p class="text-xs text-muted mb-sm">// read-only. managed by CLI plugin system.</p>
                <textarea readonly :value="pluginSkillView.content || ''" class="readonly-editor"></textarea>
              </div>
            </template>
          </div>
        </div>
      </div>"""
