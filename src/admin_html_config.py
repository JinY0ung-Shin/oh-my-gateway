"""Admin config tab HTML."""


def get_config_html() -> str:
    """Return the admin config tab html."""
    return """      <!-- Config Tab -->
      <div x-show="tab==='config'" role="tabpanel">

        <div class="card mb-lg">
          <div class="flex-between mb-md">
            <h3 style="margin:0">Runtime Settings <span class="text-xs" style="color:var(--green)">HOT-RELOAD</span></h3>
            <div class="flex-gap-sm">
              <button class="btn btn-sm btn-ghost" @click="resetAllRuntimeConfig()">[RESET ALL]</button>
              <button class="btn btn-sm btn-ghost" @click="loadRuntimeConfig()">[RELOAD]</button>
            </div>
          </div>
          <p class="text-xs text-muted mb-md">
            // changes take effect on next request. no restart needed.
          </p>
          <div class="table-wrapper">
            <table>
              <thead><tr><th>SETTING</th><th>VALUE</th><th>ORIGINAL</th><th></th></tr></thead>
              <tbody>
                <template x-for="(meta, key) in (runtimeConfig ?? {})" :key="key">
                  <tr>
                    <td>
                      <div class="config-key" x-text="meta.label"></div>
                      <div class="text-xs text-muted" x-text="meta.description"></div>
                    </td>
                    <td style="min-width:160px">
                      <template x-if="meta.type === 'bool'">
                        <select :value="meta.value ? 'true' : 'false'" @change="updateRuntimeConfig(key, $event.target.value === 'true')"
                          style="padding:4px 8px; font-size:0.75rem">
                          <option value="true">true</option>
                          <option value="false">false</option>
                        </select>
                      </template>
                      <template x-if="meta.type === 'int'">
                        <input type="number" :value="meta.value" min="1"
                          @change="updateRuntimeConfig(key, parseInt($event.target.value))"
                          style="padding:4px 8px; font-size:0.75rem; width:100px">
                      </template>
                      <template x-if="meta.type === 'string' && Array.isArray(meta.options)">
                        <select :value="meta.value"
                          @change="updateRuntimeConfig(key, $event.target.value)"
                          style="padding:4px 8px; font-size:0.75rem">
                          <template x-for="opt in meta.options" :key="opt">
                            <option :value="opt" x-text="opt" :selected="opt === meta.value"></option>
                          </template>
                        </select>
                      </template>
                      <template x-if="meta.type === 'string' && !Array.isArray(meta.options)">
                        <input type="text" :value="meta.value"
                          @change="updateRuntimeConfig(key, $event.target.value)"
                          style="padding:4px 8px; font-size:0.75rem; width:160px">
                      </template>
                    </td>
                    <td class="text-sm text-muted" x-text="meta.original"></td>
                    <td>
                      <span x-show="meta.overridden" class="badge badge-warn" style="cursor:pointer; font-size:0.65rem"
                        @click="resetRuntimeConfig(key)">[RESET]</span>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Active prompt status bar -->
        <div style="border:1px solid var(--border); background:var(--bg-raised); padding:8px 16px; margin-bottom:2px; display:flex; justify-content:space-between; align-items:center">
          <div class="flex-gap-sm">
            <span class="text-xs text-muted">ACTIVE_PROMPT:</span>
            <span x-show="systemPrompt.active_name" style="color:var(--green); font-weight:600; font-size:var(--fs-sm); text-shadow: 0 0 6px var(--green-muted)" x-text="systemPrompt.active_name"></span>
            <span x-show="!systemPrompt.active_name && systemPrompt.mode !== 'custom'" style="color:var(--text-dim); font-size:var(--fs-sm)">claude_code (preset)</span>
            <span x-show="!systemPrompt.active_name && systemPrompt.mode === 'custom'" style="color:var(--amber); font-size:var(--fs-sm)">custom (unnamed)</span>
          </div>
          <div class="flex-gap-sm">
            <span class="text-xs text-muted" x-text="'MODE=' + systemPrompt.mode"></span>
            <span class="text-xs text-muted" x-text="systemPrompt.char_count + ' chars'"></span>
          </div>
        </div>

        <!-- System Prompt: Sidebar + Editor layout -->
        <div class="sidebar mb-lg">
          <!-- Prompt list sidebar -->
          <div class="file-tree card">
            <div class="flex-between mb-sm">
              <h3 style="margin:0">Prompts</h3>
              <button class="btn btn-sm btn-primary" @click="showNewPromptForm()">[+ NEW]</button>
            </div>

            <!-- Preset entry -->
            <div class="file-item" :class="{ active: promptView === 'preset' }" @click="selectPresetPrompt()"
              style="border-bottom:1px solid var(--border); margin-bottom:4px; padding-bottom:8px">
              <span class="icon" style="color:var(--text-dim)">&gt;_</span>
              <div style="flex:1; min-width:0">
                <div style="font-size:var(--fs-sm); font-weight:600" :style="systemPrompt.active_name == null && systemPrompt.mode !== 'custom' ? 'color:var(--green)' : 'color:var(--text)'">
                  claude_code <span x-show="systemPrompt.active_name == null && systemPrompt.mode !== 'custom'" class="text-xs" style="color:var(--green)">ACTIVE</span>
                </div>
                <div class="text-xs text-muted">built-in preset</div>
              </div>
            </div>

            <!-- Templates -->
            <template x-for="t in promptTemplates" :key="'tpl-' + t.name">
              <div class="file-item" :class="{ active: promptView === 'template' && promptViewName === t.name }"
                @click="selectTemplatePrompt(t)">
                <span class="icon" style="color:var(--amber)">&#9734;</span>
                <div style="flex:1; min-width:0">
                  <div style="font-size:var(--fs-sm); font-weight:600; color:var(--text)" x-text="t.name"></div>
                  <div class="text-xs text-muted">template</div>
                </div>
              </div>
            </template>

            <!-- Divider -->
            <div x-show="namedPrompts.length > 0" style="border-top:1px solid var(--border); margin:4px 0; padding-top:4px">
              <span class="text-xs text-muted" style="padding-left:12px">// SAVED</span>
            </div>

            <!-- Named prompts -->
            <template x-for="p in namedPrompts" :key="p.name">
              <div class="file-item" :class="{ active: promptView === 'named' && promptViewName === p.name }"
                @click="selectNamedPrompt(p.name)">
                <span class="icon" :style="systemPrompt.active_name === p.name ? 'color:var(--green)' : 'color:var(--cyan)'"
                  x-text="systemPrompt.active_name === p.name ? '&#9679;' : '&#9675;'"></span>
                <div style="flex:1; min-width:0">
                  <div style="font-size:var(--fs-sm); font-weight:600"
                    :style="systemPrompt.active_name === p.name ? 'color:var(--green)' : 'color:var(--text)'" x-text="p.name">
                  </div>
                  <div class="text-xs text-muted" x-text="p.char_count + ' chars'"></div>
                </div>
                <span x-show="systemPrompt.active_name === p.name" class="text-xs" style="color:var(--green)">ACTIVE</span>
              </div>
            </template>
          </div>

          <!-- Editor area -->
          <div class="editor-area card" style="flex:1">
            <!-- New prompt form -->
            <template x-if="promptView === 'new'">
              <div style="padding:1rem">
                <h3 style="margin-top:0">Create Prompt</h3>
                <label class="text-xs text-muted">PROMPT_NAME:</label>
                <input type="text" x-model="newPromptName" placeholder="my-prompt-name"
                  style="width:100%; margin-bottom:0.5rem; margin-top:4px" @input="validateNewPromptName()">
                <p x-show="newPromptNameError" class="text-sm text-danger" style="margin:0 0 0.5rem 0" x-text="'! ' + newPromptNameError"></p>
                <p x-show="newPromptNameWarning && !newPromptNameError" class="text-sm" style="margin:0 0 0.5rem 0; color:var(--amber)" x-text="'? ' + newPromptNameWarning"></p>
                <label class="text-xs text-muted">CONTENT:</label>
                <textarea x-model="newPromptContent"
                  style="width:100%; min-height:300px; max-height:60vh; font-family:var(--font); font-size:0.78rem;
                    background:var(--bg-surface); color:var(--text-bright); border:1px solid var(--border-bright);
                    padding:8px; resize:vertical; border-radius:0; margin-top:4px"
                  placeholder="// enter system prompt content..."></textarea>
                <div class="flex-gap-sm" style="margin-top:0.75rem">
                  <button class="btn btn-sm btn-primary" @click="createNamedPrompt()"
                    :disabled="!newPromptName.trim() || !newPromptContent.trim() || !!newPromptNameError">[CREATE]</button>
                  <button class="btn btn-sm btn-ghost" @click="promptView = null">[CANCEL]</button>
                  <span class="text-xs text-muted" x-show="newPromptContent.trim()" x-text="newPromptContent.trim().length + ' chars'"></span>
                </div>
              </div>
            </template>

            <!-- No selection -->
            <template x-if="!promptView">
              <div class="text-muted" style="padding:3rem; text-align:center">
                <div style="font-size:2rem; margin-bottom:0.5rem; opacity:0.2">&gt;_</div>
                select a prompt or create new_<span class="cursor-blink"></span>
              </div>
            </template>

            <!-- Preset view (read-only) -->
            <template x-if="promptView === 'preset'">
              <div>
                <div class="flex-between mb-md">
                  <div class="flex-gap-sm">
                    <span style="color:var(--text-bright); font-weight:600">claude_code</span>
                    <span class="badge text-xs" style="border-color:var(--border-bright); color:var(--text-dim)">PRESET</span>
                    <span x-show="systemPrompt.active_name == null && systemPrompt.mode !== 'custom'" class="badge badge-ok text-xs">ACTIVE</span>
                  </div>
                  <span class="text-xs text-muted" x-text="(systemPrompt.preset_text?.length ?? 0) + ' chars'"></span>
                </div>
                <p class="text-xs text-muted mb-md">// built-in claude_code preset. read-only.</p>
                <textarea readonly :value="systemPrompt.preset_text || ''" class="readonly-editor"></textarea>
                <div class="flex-gap-sm" style="margin-top:0.75rem">
                  <button class="btn btn-sm btn-primary" @click="activatePreset()"
                    :disabled="systemPrompt.active_name == null && systemPrompt.mode !== 'custom'">[ACTIVATE]</button>
                  <button class="btn btn-sm btn-ghost" @click="forkFromPreset()">[FORK AS NEW]</button>
                </div>
              </div>
            </template>

            <!-- Template view (read-only, can fork) -->
            <template x-if="promptView === 'template'">
              <div>
                <div class="flex-between mb-md">
                  <div class="flex-gap-sm">
                    <span style="color:var(--text-bright); font-weight:600" x-text="promptViewName"></span>
                    <span class="badge text-xs" style="border-color:var(--amber); color:var(--amber)">TEMPLATE</span>
                  </div>
                  <span class="text-xs text-muted" x-text="(promptEditorContent?.length ?? 0) + ' chars'"></span>
                </div>
                <p class="text-xs text-muted mb-md">// template from docs/. save as named prompt to edit.</p>
                <textarea readonly :value="promptEditorContent || ''" class="readonly-editor"></textarea>
                <div class="flex-gap-sm" style="margin-top:0.75rem">
                  <button class="btn btn-sm btn-ghost" @click="forkFromTemplate()">[SAVE AS NEW]</button>
                </div>
              </div>
            </template>

            <!-- Named prompt editor -->
            <template x-if="promptView === 'named'">
              <div>
                <div class="flex-between mb-md">
                  <div class="flex-gap-sm">
                    <span style="color:var(--text-bright); font-weight:600" x-text="promptViewName"></span>
                    <span x-show="systemPrompt.active_name === promptViewName" class="badge badge-ok text-xs">ACTIVE</span>
                    <span x-show="promptDirty" class="dirty-dot" title="Unsaved changes"></span>
                  </div>
                  <span class="text-xs text-muted" x-text="(promptEditorContent?.length ?? 0) + ' chars'"></span>
                </div>
                <p class="text-xs text-muted mb-md">// affects new sessions only after activation.</p>
                <textarea x-model="promptEditorContent"
                  style="width:100%; min-height:350px; max-height:60vh; font-family:var(--font); font-size:0.78rem;
                    background:var(--bg-surface); color:var(--text-bright); border:1px solid var(--border-bright);
                    padding:8px; resize:vertical; border-radius:0"
                  @input="promptDirty = true"></textarea>
                <div class="flex-between" style="margin-top:0.75rem">
                  <div class="flex-gap-sm">
                    <button class="btn btn-sm btn-primary" @click="saveNamedPrompt()" :disabled="!promptDirty || !promptEditorContent.trim()">[SAVE]</button>
                    <button class="btn btn-sm btn-primary" @click="activateNamedPrompt()"
                      :disabled="systemPrompt.active_name === promptViewName && !promptDirty">[ACTIVATE]</button>
                  </div>
                  <button class="btn btn-sm btn-danger-ghost" @click="deleteNamedPrompt()">[DELETE]</button>
                </div>
              </div>
            </template>
          </div>
        </div>

        <details class="config-section">
          <summary>System Information <span class="text-xs text-muted">runtime, rate_limits, env</span></summary>
          <div class="config-body">
            <div class="grid-2 mb-lg">
              <div>
                <h3>Runtime</h3>
                <table>
                  <tbody>
                    <template x-for="(v, k) in (config.runtime ?? {})" :key="k">
                      <tr><td class="config-key" x-text="k"></td><td style="color:var(--text-bright)" x-text="v"></td></tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <div>
                <h3>Rate Limits <span class="text-xs text-muted">(req/min)</span></h3>
                <table>
                  <tbody>
                    <template x-for="(v, k) in (config.rate_limits ?? {})" :key="k">
                      <tr><td class="config-key" x-text="k"></td><td style="color:var(--amber)" x-text="v"></td></tr>
                    </template>
                  </tbody>
                </table>
              </div>
            </div>
            <div>
              <h3>Environment</h3>
              <div class="table-wrapper">
                <table>
                  <tbody>
                    <template x-for="(v, k) in (config.environment ?? {})" :key="k">
                      <tr>
                        <td class="config-key" x-text="k"></td>
                        <td :class="{ redacted: v === '***REDACTED***' || v === '(not set)' }"
                          :style="(v !== '***REDACTED***' && v !== '(not set)') ? 'color:var(--text-bright)' : ''"
                          x-text="v"></td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <p class="text-xs text-muted" style="margin-top:0.5rem" x-text="config._note || ''"></p>
            </div>
          </div>
        </details>

        <details class="config-section">
          <summary>Security & Integrations <span class="badge badge-warn text-xs">SENSITIVE</span></summary>
          <div class="config-body">
            <template x-if="config.mcp_servers">
              <div class="mb-lg">
                <h3>MCP Servers</h3>
                <div class="flex-wrap-gap">
                  <template x-for="s in config.mcp_servers" :key="s">
                    <span class="badge badge-ok" x-text="s"></span>
                  </template>
                </div>
              </div>
            </template>

            <div class="mb-lg">
              <div class="flex-between mb-sm">
                <h3 style="margin:0">Sandbox & Permissions</h3>
                <button class="btn btn-sm btn-ghost" @click="loadSandbox()">[RELOAD]</button>
              </div>
              <div class="flex-wrap-gap" style="gap:1rem; font-size:var(--fs-sm)">
                <div>
                  <span class="text-muted">permission_mode=</span>
                  <span :class="sandboxConfig.permission_mode === 'bypassPermissions' ? 'text-warning' : 'text-success'"
                    x-text="sandboxConfig.permission_mode || 'default'"></span>
                </div>
                <div>
                  <span class="text-muted">sandbox=</span>
                  <span :class="sandboxConfig.sandbox_enabled === 'true' ? 'text-success' : 'text-warning'"
                    x-text="sandboxConfig.sandbox_enabled === 'true' ? 'enabled' : 'disabled'"></span>
                </div>
              </div>
              <div x-show="(sandboxConfig.metadata_env_allowlist ?? []).length > 0" style="margin-top:0.5rem">
                <div class="text-xs text-muted mb-sm">env_allowlist:</div>
                <div class="flex-wrap-gap" style="gap:0.25rem">
                  <template x-for="v in (sandboxConfig.metadata_env_allowlist ?? [])">
                    <span class="text-xs" style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)" x-text="v"></span>
                  </template>
                </div>
              </div>
            </div>

            <div>
              <div class="flex-between mb-sm">
                <h3 style="margin:0">Tools Registry</h3>
                <button class="btn btn-sm btn-ghost" @click="loadTools()">[RELOAD]</button>
              </div>
              <template x-for="(info, backend) in (toolsRegistry.backends ?? {})" :key="backend">
                <div class="mb-md">
                  <div style="font-weight:600; margin-bottom:0.25rem; text-transform:uppercase; color:var(--cyan); font-size:var(--fs-sm)" x-text="backend + '_tools'"></div>
                  <div class="flex-wrap-gap" style="gap:0.25rem">
                    <template x-for="t in (info.all_tools ?? [])">
                      <span :class="(info.default_allowed ?? []).includes(t) ? 'badge badge-ok' : 'badge'"
                        :style="(info.default_allowed ?? []).includes(t) ? '' : 'background:var(--bg-surface); border-color:var(--border-bright); opacity:0.4; color:var(--text-dim)'"
                        style="font-size:0.65rem" x-text="t"></span>
                    </template>
                  </div>
                  <div class="text-xs text-muted" style="margin-top:0.25rem">// green = default_allowed</div>
                </div>
              </template>
              <div x-show="(toolsRegistry.mcp_tools ?? []).length > 0" style="margin-top:0.5rem">
                <div style="font-weight:600; margin-bottom:0.25rem; color:var(--cyan); font-size:var(--fs-sm); text-transform:uppercase">MCP_TOOL_PATTERNS</div>
                <div class="flex-wrap-gap" style="gap:0.25rem">
                  <template x-for="t in (toolsRegistry.mcp_tools ?? [])">
                    <span class="text-xs" style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)" x-text="t"></span>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </details>

      </div>"""
