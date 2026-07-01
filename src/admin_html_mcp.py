"""Admin MCP tab HTML (manage MCP servers at runtime; env base + manifest overlay)."""


def get_mcp_html() -> str:
    """Return the admin MCP servers management tab html."""
    return """      <!-- MCP Tab -->
      <div x-show="tab==='mcp'" role="tabpanel">
        <div class="flex-between mb-sm">
          <h3 style="margin:0">MCP Servers <span class="text-xs" style="color:var(--green)">HOT-RELOAD</span></h3>
          <button class="btn btn-sm btn-ghost" @click="refreshMcp()" :disabled="loading.mcp" aria-label="Reload MCP servers">
            <span x-text="loading.mcp ? 'Loading...' : 'Reload'"></span>
          </button>
        </div>
        <p class="text-xs text-muted mb-md">
          Overlay on top of the MCP_CONFIG environment base (manifest wins). Applies to new sessions.
          Existing sessions keep the MCP set pinned at creation. OpenCode requires a restart.
          Plugin-provided servers are shown read-only (Claude loads them via setting_sources).
        </p>

        <!-- Dropped servers banner (diagnostic 2) -->
        <div x-show="mcpDetail.dropped.length" class="card mb-md" style="border-color:var(--amber)">
          <div class="flex-gap-sm mb-sm">
            <span class="badge badge-warn text-xs">DROPPED</span>
            <span class="text-xs text-muted">Servers present in config but not loaded.</span>
          </div>
          <template x-for="d in mcpDetail.dropped" :key="d.name">
            <div class="flex-gap-sm mb-sm" style="align-items:baseline">
              <span class="badge badge-warn text-xs" x-text="d.name"></span>
              <span class="text-xs text-muted" x-text="d.source"></span>
              <span class="text-xs text-warning" x-text="d.reason"></span>
            </div>
          </template>
        </div>

        <!-- Servers table -->
        <div class="card mb-lg">
          <div class="table-wrapper">
            <table>
              <thead><tr><th>NAME</th><th>TYPE</th><th>SOURCE</th><th>TOOLS</th><th>REACH</th><th></th></tr></thead>
              <tbody>
                <template x-for="s in mcpDetail.servers" :key="s.name">
                  <tr>
                    <td style="color:var(--cyan); font-weight:600" x-text="s.name"></td>
                    <td>
                      <span class="badge text-xs" x-text="s.type"></span>
                      <div x-show="s.valid === false" class="text-xs text-warning"
                        x-text="s.invalid_reason" :title="s.invalid_reason"></div>
                      <div x-show="s.shadowed" class="text-xs text-muted"
                        title="A same-named env/manifest server exists; non-Claude backends use that one.">shadowed</div>
                    </td>
                    <td>
                      <span class="badge text-xs" x-show="s.source === 'manifest'"
                        style="border-color:var(--green); color:var(--green)">MANIFEST</span>
                      <span class="badge text-xs" x-show="s.source === 'env'"
                        style="border-color:var(--cyan); color:var(--cyan); opacity:0.7">ENV</span>
                      <span class="badge text-xs" x-show="s.source === 'plugin'"
                        style="border-color:var(--magenta); color:var(--magenta)">PLUGIN</span>
                      <div x-show="s.source === 'plugin' && s.plugin" class="text-xs text-muted"
                        style="max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap"
                        x-text="s.plugin" :title="s.plugin"></div>
                      <div class="text-xs text-muted" x-text="s.editable ? 'editable' : 'read-only'"></div>
                    </td>
                    <td>
                      <div class="flex-wrap-gap" style="gap:0.25rem">
                        <template x-for="t in (s.tools ?? [])" :key="t">
                          <span class="text-xs text-mono" style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)" x-text="t"></span>
                        </template>
                        <span x-show="!(s.tools?.length)" class="text-xs text-muted">-</span>
                      </div>
                    </td>
                    <td>
                      <div class="flex-wrap-gap" style="gap:0.25rem">
                        <template x-for="r in (s.reach ?? [])" :key="r.backend">
                          <span class="badge text-xs" :title="r.condition"
                            :style="r.reaches ? 'border-color:var(--green); color:var(--green)' : 'border-color:var(--border-bright); color:var(--text-dim); opacity:0.5'"
                            x-text="r.backend"></span>
                        </template>
                      </div>
                    </td>
                    <td>
                      <div class="flex-gap-sm">
                        <button class="btn btn-sm btn-ghost" @click="testMcpServer(s.name)" :disabled="!!mcpTestBusy[s.name]"
                          aria-label="Test MCP server">
                          <span x-text="mcpTestBusy[s.name] ? 'Testing...' : 'Test'"></span>
                        </button>
                        <button x-show="s.editable" class="btn btn-sm btn-ghost" @click="editMcpServer(s)"
                          aria-label="Edit MCP server">Edit</button>
                        <button x-show="s.editable" class="btn btn-sm btn-ghost" style="color:var(--red)"
                          @click="deleteMcpServer(s.name)" aria-label="Delete MCP server">Delete</button>
                      </div>
                      <div x-show="mcpTestResult[s.name]" class="text-xs" style="margin-top:4px"
                        :style="mcpTestResult[s.name]?.ok ? 'color:var(--green)' : 'color:var(--red)'"
                        x-text="mcpTestResult[s.name] ? ((mcpTestResult[s.name].ok ? '✓ ' : '✗ ') + mcpTestResult[s.name].message) : ''"></div>
                    </td>
                  </tr>
                </template>
                <tr x-show="mcpDetail.servers.length === 0">
                  <td colspan="6" class="text-sm text-muted">No MCP servers</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Add / Edit form -->
        <div class="card">
          <div class="flex-between mb-md">
            <h3 style="margin:0" x-text="mcpEditName ? ('Edit ' + mcpEditName) : 'Add MCP Server'"></h3>
            <span x-show="mcpEditName" class="badge badge-ok text-xs">EDITING</span>
          </div>
          <div class="flex-gap-sm mb-sm">
            <input type="text" x-model="mcpForm.name" @input="validateMcpJson()"
              placeholder="server-name" aria-label="MCP server name"
              :disabled="!!mcpEditName" style="flex:1">
            <select x-model="mcpForm.type" @input="validateMcpJson()" aria-label="MCP server type" style="flex:1">
              <option value="stdio">stdio</option>
              <option value="sse">sse</option>
              <option value="http">http</option>
              <option value="streamable-http">streamable-http</option>
            </select>
          </div>
          <label class="text-xs text-muted">CONFIG (JSON):</label>
          <textarea x-model="mcpForm.jsonConfig" @input="validateMcpJson()"
            style="width:100%; min-height:180px; max-height:50vh; font-family:var(--font-mono); font-size:0.78rem;
              background:var(--bg-surface); color:var(--text-bright); border:1px solid var(--border-bright);
              padding:8px; resize:vertical; border-radius:0; margin-top:4px"
            placeholder='{"command": "npx", "args": ["-y", "server"], "env": {}}'></textarea>
          <p x-show="mcpJsonError" class="text-sm text-danger" style="margin:0.5rem 0 0 0" x-text="'! ' + mcpJsonError"></p>
          <p x-show="mcpJsonWarning && !mcpJsonError" class="text-sm" style="margin:0.5rem 0 0 0; color:var(--amber)" x-text="'? ' + mcpJsonWarning"></p>
          <div x-show="mcpPatternPreview.length" class="flex-wrap-gap" style="gap:0.25rem; margin-top:0.5rem">
            <span class="text-xs text-muted">tool patterns:</span>
            <template x-for="p in mcpPatternPreview" :key="p">
              <span class="text-xs text-mono" style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)" x-text="p"></span>
            </template>
          </div>
          <div class="flex-gap-sm" style="margin-top:0.75rem">
            <button class="btn btn-sm btn-primary" @click="mcpEditName ? saveMcpServer() : createMcpServer()"
              :disabled="!mcpForm.name.trim() || !!mcpJsonError || mcpBusy">
              <span x-text="mcpBusy ? 'Working...' : (mcpEditName ? 'Save' : 'Add server')"></span>
            </button>
            <button x-show="mcpEditName" class="btn btn-sm btn-ghost" @click="cancelMcpEdit()">Cancel</button>
          </div>
        </div>
      </div>"""
