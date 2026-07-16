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
          Per-server env/headers stay on the MCP config only (not the gateway process).
          Use <code style="font-size:0.75em">{{env:VAR}}</code> to pull from the gateway environment at session create
          (Claude/Codex) or OpenCode managed startup.
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
              <thead><tr><th>NAME</th><th>TYPE</th><th>SOURCE</th><th>ENV / HEADERS</th><th>TOOLS</th><th>REACH</th><th></th></tr></thead>
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
                        <span x-show="s.env_key_count" class="text-xs text-mono"
                          style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)"
                          :title="(s.env_keys || []).join(', ')"
                          x-text="'env:' + (s.env_key_count || 0)"></span>
                        <span x-show="s.header_key_count" class="text-xs text-mono"
                          style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)"
                          :title="(s.header_keys || []).join(', ')"
                          x-text="'hdr:' + (s.header_key_count || 0)"></span>
                        <template x-for="ref in (s.env_refs || [])" :key="s.name + ':ref:' + ref">
                          <span class="text-xs text-mono" style="padding:1px 6px; border:1px solid var(--cyan); color:var(--cyan)"
                            title="Resolved from gateway env at session create / OpenCode startup"
                            x-text="mcpEnvRefBadge(ref)"></span>
                        </template>
                        <span x-show="!(s.env_key_count || s.header_key_count)" class="text-xs text-muted">-</span>
                      </div>
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
                  <td colspan="7" class="text-sm text-muted">No MCP servers</td>
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
            <select x-model="mcpForm.type" @change="onMcpTypeChange()" aria-label="MCP server type" style="flex:1">
              <option value="stdio">stdio</option>
              <option value="sse">sse</option>
              <option value="http">http</option>
              <option value="streamable-http">streamable-http</option>
            </select>
          </div>

          <!-- Per-server env (stdio) -->
          <div x-show="mcpForm.type === 'stdio'" class="mb-md" style="margin-top:0.75rem">
            <div class="flex-between mb-sm">
              <label class="text-xs text-muted" style="margin:0">ENVIRONMENT VARIABLES (stdio process only)</label>
              <button type="button" class="btn btn-sm btn-ghost" @click="addMcpEnvPair()" aria-label="Add env var">+ Add</button>
            </div>
            <p class="text-xs text-muted mb-sm" style="margin-top:0">
              Exposed only to this MCP child process. Value may be literal or
              <code style="font-size:0.75em">{{env:GATEWAY_VAR}}</code>
              (resolved at session create; not written into the gateway process).
            </p>
            <template x-for="(pair, idx) in mcpForm.envPairs" :key="'env-'+idx">
              <div class="flex-gap-sm mb-sm" style="align-items:center">
                <input type="text" x-model="pair.key" @input="onMcpPairsChange()"
                  placeholder="KEY" aria-label="Env key" style="flex:1; font-family:var(--font-mono); font-size:0.78rem">
                <input type="text" x-model="pair.value" @input="onMcpPairsChange()"
                  placeholder="value or {{env:NAME}}" aria-label="Env value" style="flex:2; font-family:var(--font-mono); font-size:0.78rem">
                <button type="button" class="btn btn-sm btn-ghost" style="color:var(--red)"
                  @click="removeMcpEnvPair(idx)" aria-label="Remove env var">×</button>
              </div>
            </template>
            <div x-show="!mcpForm.envPairs.length" class="text-xs text-muted mb-sm">No env vars</div>
          </div>

          <!-- Headers (remote) -->
          <div x-show="mcpForm.type !== 'stdio'" class="mb-md" style="margin-top:0.75rem">
            <div class="flex-between mb-sm">
              <label class="text-xs text-muted" style="margin:0">HEADERS (remote MCP only)</label>
              <button type="button" class="btn btn-sm btn-ghost" @click="addMcpHeaderPair()" aria-label="Add header">+ Add</button>
            </div>
            <p class="text-xs text-muted mb-sm" style="margin-top:0">
              Sent on HTTP/SSE requests to this server. Prefer
              <code style="font-size:0.75em">{{env:TOKEN}}</code>
              over pasting secrets. Process env is not used for remote transports.
            </p>
            <template x-for="(pair, idx) in mcpForm.headerPairs" :key="'hdr-'+idx">
              <div class="flex-gap-sm mb-sm" style="align-items:center">
                <input type="text" x-model="pair.key" @input="onMcpPairsChange()"
                  placeholder="Header-Name" aria-label="Header name" style="flex:1; font-family:var(--font-mono); font-size:0.78rem">
                <input type="text" x-model="pair.value" @input="onMcpPairsChange()"
                  placeholder="value or {{env:NAME}}" aria-label="Header value" style="flex:2; font-family:var(--font-mono); font-size:0.78rem">
                <button type="button" class="btn btn-sm btn-ghost" style="color:var(--red)"
                  @click="removeMcpHeaderPair(idx)" aria-label="Remove header">×</button>
              </div>
            </template>
            <div x-show="!mcpForm.headerPairs.length" class="text-xs text-muted mb-sm">No headers</div>
          </div>

          <div class="flex-between mb-sm" style="margin-top:0.5rem">
            <label class="text-xs text-muted" style="margin:0">CONFIG (JSON)</label>
            <button type="button" class="btn btn-sm btn-ghost" @click="mcpShowAdvanced = !mcpShowAdvanced"
              x-text="mcpShowAdvanced ? 'Hide advanced JSON' : 'Show advanced JSON'"></button>
          </div>
          <div x-show="mcpShowAdvanced">
            <textarea x-model="mcpForm.jsonConfig" @input="onMcpJsonChange()"
              style="width:100%; min-height:180px; max-height:50vh; font-family:var(--font-mono); font-size:0.78rem;
                background:var(--bg-surface); color:var(--text-bright); border:1px solid var(--border-bright);
                padding:8px; resize:vertical; border-radius:0; margin-top:4px"
              placeholder='{"command": "npx", "args": ["-y", "server"], "env": {"API_KEY": "{{env:API_KEY}}"}}'></textarea>
          </div>
          <div x-show="!mcpShowAdvanced" class="text-xs text-muted" style="margin-top:4px">
            Advanced JSON is hidden; env/headers editors stay in sync. Open it to edit command/url/args.
          </div>

          <!-- Always-visible minimal fields when advanced is hidden -->
          <div x-show="!mcpShowAdvanced" style="margin-top:0.75rem">
            <div x-show="mcpForm.type === 'stdio'" class="flex-gap-sm mb-sm">
              <input type="text" x-model="mcpForm.command" @input="onMcpSimpleFieldsChange()"
                placeholder="command (e.g. npx)" aria-label="MCP command" style="flex:1; font-family:var(--font-mono); font-size:0.78rem">
              <input type="text" x-model="mcpForm.argsText" @input="onMcpSimpleFieldsChange()"
                placeholder='args (JSON array or space-separated)' aria-label="MCP args" style="flex:2; font-family:var(--font-mono); font-size:0.78rem">
            </div>
            <div x-show="mcpForm.type !== 'stdio'">
              <input type="text" x-model="mcpForm.url" @input="onMcpSimpleFieldsChange()"
                placeholder="https://mcp.example.com/mcp" aria-label="MCP url"
                style="width:100%; font-family:var(--font-mono); font-size:0.78rem">
            </div>
          </div>

          <p x-show="mcpJsonError" class="text-sm text-danger" style="margin:0.5rem 0 0 0" x-text="'! ' + mcpJsonError"></p>
          <p x-show="mcpJsonWarning && !mcpJsonError" class="text-sm" style="margin:0.5rem 0 0 0; color:var(--amber)" x-text="'? ' + mcpJsonWarning"></p>
          <div x-show="mcpPatternPreview.length" class="flex-wrap-gap" style="gap:0.25rem; margin-top:0.5rem">
            <span class="text-xs text-muted">tool patterns:</span>
            <template x-for="p in mcpPatternPreview" :key="p">
              <span class="text-xs text-mono" style="padding:1px 6px; border:1px solid var(--border-bright); background:var(--bg-surface); color:var(--text-dim)" x-text="p"></span>
            </template>
          </div>
          <div x-show="mcpEnvRefPreview.length" class="flex-wrap-gap" style="gap:0.25rem; margin-top:0.5rem">
            <span class="text-xs text-muted">gateway env refs (resolved at session create / OpenCode startup):</span>
            <template x-for="ref in mcpEnvRefPreview" :key="'prev-'+ref">
              <span class="text-xs text-mono" style="padding:1px 6px; border:1px solid var(--cyan); color:var(--cyan)"
                x-text="mcpEnvRefBadge(ref)"></span>
            </template>
          </div>
          <p class="text-xs text-muted" style="margin:0.75rem 0 0 0">
            OpenCode: MCP (including env/headers) is baked in only when
            <code style="font-size:0.75em">OPENCODE_USE_WRAPPER_MCP_CONFIG=true</code>
            — restart required after changes.
          </p>
          <div class="flex-gap-sm" style="margin-top:0.75rem">
            <button class="btn btn-sm btn-primary" @click="mcpEditName ? saveMcpServer() : createMcpServer()"
              :disabled="!mcpForm.name.trim() || !!mcpJsonError || mcpBusy">
              <span x-text="mcpBusy ? 'Working...' : (mcpEditName ? 'Save' : 'Add server')"></span>
            </button>
            <button x-show="mcpEditName" class="btn btn-sm btn-ghost" @click="cancelMcpEdit()">Cancel</button>
          </div>
        </div>
      </div>"""
