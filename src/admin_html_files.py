"""Admin files/workspace tab HTML."""


def get_files_html() -> str:
    """Return the admin files/workspace tab html."""
    return """      <!-- Workspace Tab -->
      <div x-show="tab==='files'" role="tabpanel">
        <div class="sidebar">
          <div class="file-tree card">
            <h3>File System</h3>
            <template x-for="f in files" :key="f.path">
              <div class="file-item" :class="{ active: editor.path === f.path }" @click="openFile(f.path)">
                <span class="icon" :class="getFileIconClass(f.path)" x-text="getFileIcon(f.path)"></span>
                <span x-text="f.path.split('/').pop()"></span>
              </div>
            </template>
            <div x-show="files.length === 0" class="text-sm text-muted" style="padding:8px 12px">
              [ EMPTY ]
            </div>
          </div>
          <div class="editor-area card">
            <template x-if="!editor.path">
              <div class="text-muted" style="padding:3rem; text-align:center">
                <div style="font-size:2rem; margin-bottom:0.5rem; opacity:0.2">&lt;/&gt;</div>
                select a file to edit_<span class="cursor-blink"></span>
              </div>
            </template>
            <template x-if="editor.path">
              <div>
                <div class="editor-toolbar">
                  <div>
                    <span class="text-xs text-muted" x-text="editor.path.split('/').slice(0,-1).join('/') + '/'"></span>
                    <span class="text-sm" style="color:var(--text-bright)" x-text="editor.path.split('/').pop()"></span>
                  </div>
                  <div class="flex-gap-sm">
                    <span x-show="editor.dirty" class="dirty-dot" title="Unsaved changes"></span>
                    <span x-show="editor.dirty" class="text-xs text-muted">Ctrl+S</span>
                    <button class="btn btn-sm btn-primary" @click="saveFile()" :disabled="!editor.dirty">[SAVE]</button>
                  </div>
                </div>
                <textarea x-ref="editorArea" style="display:none"></textarea>
              </div>
            </template>
          </div>
        </div>
      </div>"""
