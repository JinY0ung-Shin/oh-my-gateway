# Backend-Isolated Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Claude, Codex, and OpenCode use independent per-user working directories and backend-native skill/config locations.

**Architecture:** `WorkspaceManager` becomes backend-aware while preserving the existing anonymous temporary workspace behavior. `/v1/responses` resolves workspace paths with the already-resolved backend, stores that backend-specific cwd in `Session.workspace`, and keeps existing backend mismatch guards. Admin skill helpers gain an optional backend parameter so the current Claude-default API remains compatible while service logic can target `.agents/skills` and `.opencode/skills`.

**Tech Stack:** Python 3.12, FastAPI, pytest, unittest.mock, pathlib/shutil filesystem helpers.

---

## File Map

- Modify `src/workspace_manager.py`: add backend-aware path resolution, backend-specific template sync, skill mirroring, and generalized template source discovery.
- Modify `src/routes/responses.py`: pass `resolved.backend` into workspace resolution and rehydration lookup.
- Modify `src/admin_service.py`: map skill roots by backend and add optional backend parameters to skill helpers.
- Modify `src/routes/admin.py`: accept an optional `backend` query parameter for skill endpoints while defaulting to Claude.
- Modify `tests/test_workspace_manager.py`: cover backend-specific paths and template sync.
- Modify `tests/test_responses_user.py`: assert backend is passed to workspace resolution and cwd forwarding.
- Modify `tests/test_admin_skills.py`: cover backend-aware service helpers and route query parameters.

---

### Task 1: Backend-Aware Workspace Paths

**Files:**
- Modify: `src/workspace_manager.py`
- Test: `tests/test_workspace_manager.py`

- [ ] **Step 1: Write failing path tests**

Add these tests inside `TestResolve` in `tests/test_workspace_manager.py`:

```python
    def test_named_user_backend_creates_backend_directory(self, manager, tmp_base):
        workspace = manager.resolve("alice", backend="codex", sync_template=False)
        assert workspace == tmp_base / "alice" / "codex"
        assert workspace.is_dir()

    def test_named_user_backend_directories_are_independent(self, manager, tmp_base):
        claude = manager.resolve("alice", backend="claude", sync_template=False)
        codex = manager.resolve("alice", backend="codex", sync_template=False)
        assert claude == tmp_base / "alice" / "claude"
        assert codex == tmp_base / "alice" / "codex"
        assert claude != codex

    def test_anonymous_ignores_backend_for_tmp_layout(self, manager, tmp_base):
        workspace = manager.resolve(None, backend="opencode", sync_template=False)
        assert workspace.parent == tmp_base
        assert workspace.name.startswith("_tmp_")

    def test_rejects_invalid_backend_name(self, manager):
        with pytest.raises(ValueError, match="Invalid backend"):
            manager.resolve("alice", backend="../codex", sync_template=False)
```

- [ ] **Step 2: Run path tests and verify failure**

Run:

```bash
uv run pytest tests/test_workspace_manager.py::TestResolve -q
```

Expected: fails because `WorkspaceManager.resolve()` does not accept `backend`.

- [ ] **Step 3: Implement backend path support**

In `src/workspace_manager.py`, add a backend name pattern near `_USER_PATTERN`:

```python
_BACKEND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
```

Add this method to `WorkspaceManager`:

```python
    def _sanitize_backend(self, backend: Optional[str]) -> Optional[str]:
        """Validate and return a backend directory name."""
        if backend is None:
            return None
        if not backend or not _BACKEND_PATTERN.match(backend):
            raise ValueError(
                f"Invalid backend: {backend!r}. Must match ^[a-z][a-z0-9_-]{{0,31}}$"
            )
        return backend
```

Change the `resolve` signature and path selection:

```python
    def resolve(
        self,
        user: Optional[str] = None,
        sync_template: bool = False,
        backend: Optional[str] = None,
    ) -> Path:
        """Return the workspace path for *user*, creating it if necessary.

        Named users use ``base_path/user/backend`` when *backend* is provided.
        Anonymous workspaces remain session-scoped ``_tmp_{uuid}`` directories.
        """
        backend_name = self._sanitize_backend(backend)
        if user is not None:
            sanitized = self._sanitize(user)
            workspace = self.base_path / sanitized
            if backend_name:
                workspace = workspace / backend_name
        else:
            workspace = self.base_path / f"_tmp_{uuid.uuid4().hex}"

        workspace.mkdir(parents=True, exist_ok=True)

        if sync_template:
            self._sync_template(workspace, backend=backend_name)
            self._sync_project_files(workspace)

        return workspace
```

Temporarily update `_sync_template` to accept `backend` without changing copy behavior yet:

```python
    def _sync_template(self, workspace: Path, backend: Optional[str] = None) -> None:
        """Copy backend template files into *workspace*."""
        _ = backend
        if self.template_source is None:
            return
        src = self.template_source / ".claude"
        if not src.is_dir():
            logger.debug("Template source .claude/ not found at %s, skipping sync", src)
            return
        dst = workspace / ".claude"
        if dst.exists():
            if dst.is_symlink():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        shutil.copytree(src, dst)
        logger.debug("Synced .claude/ template to %s", dst)
```

- [ ] **Step 4: Run path tests and verify pass**

Run:

```bash
uv run pytest tests/test_workspace_manager.py::TestResolve -q
```

Expected: all `TestResolve` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/workspace_manager.py tests/test_workspace_manager.py
git commit -m "feat: resolve backend-specific workspaces"
```

---

### Task 2: Backend-Specific Template Sync And Skill Mirrors

**Files:**
- Modify: `src/workspace_manager.py`
- Test: `tests/test_workspace_manager.py`

- [ ] **Step 1: Extend template fixture**

Replace the `tmp_template` fixture body in `tests/test_workspace_manager.py` with:

```python
@pytest.fixture
def tmp_template(tmp_path):
    """Provide a temporary CLAUDE_CWD-style template directory."""
    template = tmp_path / "template"

    claude_dir = template / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text('{"key": "value"}')
    (claude_dir / "subdir").mkdir()
    (claude_dir / "subdir" / "nested.txt").write_text("nested content")
    claude_skill = claude_dir / "skills" / "shared-skill"
    claude_skill.mkdir(parents=True)
    (claude_skill / "SKILL.md").write_text(
        "---\nname: shared-skill\ndescription: Claude skill\n---\nClaude"
    )
    (template / "CLAUDE.md").write_text("# Claude instructions\n")

    agents_dir = template / ".agents"
    agents_dir.mkdir()
    (agents_dir / "config.json").write_text('{"agent": true}')
    (template / "AGENTS.md").write_text("# Agent instructions\n")

    opencode_dir = template / ".opencode"
    opencode_dir.mkdir()
    (opencode_dir / "opencode.json").write_text('{"permission": {}}')

    return template
```

- [ ] **Step 2: Write failing backend template tests**

Add these tests inside `TestResolve`:

```python
    def test_claude_sync_copies_only_claude_native_files(self, manager):
        workspace = manager.resolve("alice", backend="claude", sync_template=True)
        assert (workspace / ".claude" / "settings.json").is_file()
        assert (workspace / "CLAUDE.md").read_text() == "# Claude instructions\n"
        assert not (workspace / ".agents").exists()
        assert not (workspace / ".opencode").exists()

    def test_codex_sync_copies_agents_and_mirrors_claude_skills(self, manager):
        workspace = manager.resolve("alice", backend="codex", sync_template=True)
        assert (workspace / ".agents" / "config.json").is_file()
        assert (workspace / "AGENTS.md").read_text() == "# Agent instructions\n"
        mirrored = workspace / ".agents" / "skills" / "shared-skill" / "SKILL.md"
        assert mirrored.read_text().endswith("Claude")
        assert not (workspace / ".claude").exists()
        assert not (workspace / ".opencode").exists()

    def test_opencode_sync_copies_opencode_and_mirrors_claude_skills(self, manager):
        workspace = manager.resolve("alice", backend="opencode", sync_template=True)
        assert (workspace / ".opencode" / "opencode.json").is_file()
        mirrored = workspace / ".opencode" / "skills" / "shared-skill" / "SKILL.md"
        assert mirrored.read_text().endswith("Claude")
        assert not (workspace / ".claude").exists()
        assert not (workspace / ".agents").exists()

    def test_codex_native_skills_win_over_claude_mirror(self, tmp_base, tmp_template):
        native = tmp_template / ".agents" / "skills" / "shared-skill"
        native.mkdir(parents=True)
        (native / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Codex native\n---\nCodex"
        )
        mgr = WorkspaceManager(base_path=tmp_base, template_source=tmp_template)
        workspace = mgr.resolve("alice", backend="codex", sync_template=True)
        skill = workspace / ".agents" / "skills" / "shared-skill" / "SKILL.md"
        assert skill.read_text().endswith("Codex")

    def test_opencode_claude_compatibility_wins_over_agents_duplicate(
        self, tmp_base, tmp_template
    ):
        agent_skill = tmp_template / ".agents" / "skills" / "shared-skill"
        agent_skill.mkdir(parents=True)
        (agent_skill / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Agent copy\n---\nAgent"
        )
        mgr = WorkspaceManager(base_path=tmp_base, template_source=tmp_template)
        workspace = mgr.resolve("alice", backend="opencode", sync_template=True)
        skill = workspace / ".opencode" / "skills" / "shared-skill" / "SKILL.md"
        assert skill.read_text().endswith("Claude")

    def test_template_source_without_claude_dir_can_sync_agents(self, tmp_base, tmp_path):
        template = tmp_path / "template"
        agents = template / ".agents"
        agents.mkdir(parents=True)
        (agents / "config.json").write_text('{"agent": true}')
        mgr = WorkspaceManager(base_path=tmp_base, template_source=template)

        workspace = mgr.resolve("alice", backend="codex", sync_template=True)

        assert (workspace / ".agents" / "config.json").is_file()
```

- [ ] **Step 3: Run template tests and verify failure**

Run:

```bash
uv run pytest tests/test_workspace_manager.py::TestResolve -q
```

Expected: backend-specific template tests fail because sync still only copies `.claude/`.

- [ ] **Step 4: Implement backend template sync helpers**

In `src/workspace_manager.py`, replace `_sync_template` with these helpers:

```python
    def _replace_tree(self, src: Path, dst: Path) -> None:
        if dst.exists():
            if dst.is_symlink():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        shutil.copytree(src, dst)

    def _copy_template_file(self, name: str, workspace: Path) -> None:
        if self.template_source is None:
            return
        src = self.template_source / name
        if not src.is_file():
            return
        dst = workspace / name
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        shutil.copy2(src, dst)

    def _copy_template_dir(self, name: str, workspace: Path) -> bool:
        if self.template_source is None:
            return False
        src = self.template_source / name
        if not src.is_dir():
            return False
        self._replace_tree(src, workspace / name)
        return True

    def _mirror_skill_dirs(self, sources: tuple[Path, ...], dst: Path) -> None:
        for src in sources:
            if not src.is_dir():
                continue
            dst.mkdir(parents=True, exist_ok=True)
            for child in sorted(src.iterdir()):
                if not child.is_dir() or child.is_symlink():
                    continue
                skill_file = child / "SKILL.md"
                if not skill_file.is_file() or skill_file.is_symlink():
                    continue
                target = dst / child.name
                if target.exists():
                    continue
                shutil.copytree(child, target)
```

Then implement `_sync_template` as:

```python
    def _sync_template(self, workspace: Path, backend: Optional[str] = None) -> None:
        """Copy backend-native templates into *workspace*."""
        if self.template_source is None:
            return

        if backend == "codex":
            self._copy_template_dir(".agents", workspace)
            self._copy_template_file("AGENTS.md", workspace)
            skills_dst = workspace / ".agents" / "skills"
            if not skills_dst.exists():
                self._mirror_skill_dirs((self.template_source / ".claude" / "skills",), skills_dst)
            logger.debug("Synced Codex template to %s", workspace)
            return

        if backend == "opencode":
            self._copy_template_dir(".opencode", workspace)
            skills_dst = workspace / ".opencode" / "skills"
            if not skills_dst.exists():
                self._mirror_skill_dirs(
                    (
                        self.template_source / ".claude" / "skills",
                        self.template_source / ".agents" / "skills",
                    ),
                    skills_dst,
                )
            logger.debug("Synced OpenCode template to %s", workspace)
            return

        self._copy_template_dir(".claude", workspace)
        self._copy_template_file("CLAUDE.md", workspace)
        logger.debug("Synced Claude template to %s", workspace)
```

Change `_resolve_template_source` to return `CLAUDE_CWD` when it is a directory:

```python
def _resolve_template_source() -> Optional[Path]:
    """Determine the agent template source directory."""
    claude_cwd = os.getenv("CLAUDE_CWD", "")
    if claude_cwd:
        p = Path(claude_cwd)
        if p.is_dir():
            return p
    return None
```

- [ ] **Step 5: Run workspace tests**

Run:

```bash
uv run pytest tests/test_workspace_manager.py -q
```

Expected: all workspace manager tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/workspace_manager.py tests/test_workspace_manager.py
git commit -m "feat: sync backend native agent templates"
```

---

### Task 3: Route `/v1/responses` Through Backend-Specific Workspaces

**Files:**
- Modify: `src/routes/responses.py`
- Test: `tests/test_responses_user.py`

- [ ] **Step 1: Update failing expectations in response user tests**

In `tests/test_responses_user.py`, update existing `mock_wm.resolve` assertions:

```python
mock_wm.resolve.assert_called_once_with("alice", sync_template=True, backend="claude")
mock_wm.resolve.assert_called_once_with(None, sync_template=True, backend="claude")
mock_wm.resolve.assert_called_once_with("alice", sync_template=False, backend="claude")
```

Update `test_cwd_passed_to_run_completion` to use a backend-specific path:

```python
mock_wm.resolve.return_value = Path("/tmp/ws/alice/claude")
...
assert create_calls[0]["cwd"] == "/tmp/ws/alice/claude"
```

Add this test to `TestUserParam`:

```python
    def test_responses_resolves_codex_workspace_with_codex_backend(
        self, isolated_session_manager
    ):
        mock_wm = MagicMock()
        mock_wm.resolve.return_value = Path("/tmp/ws/alice/codex")
        create_calls = []

        async def fake_create_client(**kwargs):
            create_calls.append(kwargs)
            return object()

        async def fake_run_completion(client, prompt, session):
            yield {"subtype": "success", "result": "Hello from Codex"}

        with client_context_with_workspace(mock_wm) as (client, mock_cli):
            BackendRegistry.unregister("claude")
            BackendRegistry.register("codex", mock_cli)
            mock_cli.create_client = fake_create_client
            mock_cli.run_completion_with_client = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Hello from Codex")
            resp = client.post(
                "/v1/responses",
                json={
                    "model": "codex/openai/gpt-5.1-codex",
                    "input": "hello",
                    "user": "alice",
                },
            )

        assert resp.status_code == 200
        mock_wm.resolve.assert_called_once_with("alice", sync_template=True, backend="codex")
        assert create_calls[0]["cwd"] == "/tmp/ws/alice/codex"
```

- [ ] **Step 2: Run response tests and verify failure**

Run:

```bash
uv run pytest tests/test_responses_user.py -q
```

Expected: fails because routes do not pass `backend=...`.

- [ ] **Step 3: Thread backend through workspace resolution**

In `src/routes/responses.py`, change `_resolve_response_session` signature:

```python
def _resolve_response_session(body: ResponseCreateRequest, backend: str):
```

Change the early rehydrate cwd lookup:

```python
            _early_cwd = str(
                workspace_manager.resolve(body.user, sync_template=False, backend=backend)
            )
```

Change `_resolve_response_workspace` signature:

```python
async def _resolve_response_workspace(
    body: ResponseCreateRequest,
    session,
    session_id: str,
    is_new_session: bool,
    backend: str,
) -> Path:
```

Change new-session workspace resolution:

```python
            workspace = workspace_manager.resolve(
                body.user,
                sync_template=True,
                backend=backend,
            )
```

Change fallback resolution for sessions without stored workspace:

```python
        workspace = workspace_manager.resolve(
            body.user,
            sync_template=False,
            backend=backend,
        )
```

Change call sites in `responses()`:

```python
    session_id, session = _resolve_response_session(body, resolved.backend)
    workspace = await _resolve_response_workspace(
        body, session, session_id, is_new_session, resolved.backend
    )
```

- [ ] **Step 4: Preserve legacy Claude rehydration fallback**

Still in `_resolve_response_session`, after the first `get_session` attempt, add a legacy fallback for old Claude transcripts:

```python
    session = session_manager.get_session(session_id, user=body.user, cwd=_early_cwd)
    if session is None and backend == "claude" and body.user:
        try:
            legacy_cwd = str(workspace_manager.resolve(body.user, sync_template=False))
        except (ValueError, OSError):
            legacy_cwd = None
        if legacy_cwd and legacy_cwd != _early_cwd:
            session = session_manager.get_session(session_id, user=body.user, cwd=legacy_cwd)
```

Keep the existing 404 behavior after that block.

- [ ] **Step 5: Run response tests**

Run:

```bash
uv run pytest tests/test_responses_user.py -q
```

Expected: all `test_responses_user.py` tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/routes/responses.py tests/test_responses_user.py
git commit -m "feat: route responses to backend workspaces"
```

---

### Task 4: Backend-Aware Admin Skill Helpers

**Files:**
- Modify: `src/admin_service.py`
- Test: `tests/test_admin_skills.py`

- [ ] **Step 1: Write failing service tests**

Add this fixture to `tests/test_admin_skills.py`:

```python
@pytest.fixture
def multi_backend_workspace(tmp_path):
    for root_name, body in [
        (".claude", "Claude body"),
        (".agents", "Codex body"),
        (".opencode", "OpenCode body"),
    ]:
        skill_dir = tmp_path / root_name / "skills" / "hello-world"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: hello-world\ndescription: Backend skill\n---\n" + body
        )
    with patch("src.admin_service.get_workspace_root", return_value=tmp_path):
        yield tmp_path
```

Add these tests near the service skill tests:

```python
class TestBackendSkillRoots:
    def test_list_skills_uses_codex_agents_root(self, multi_backend_workspace):
        skills = list_skills(backend="codex")
        assert [s["name"] for s in skills] == ["hello-world"]

    def test_get_skill_uses_opencode_root(self, multi_backend_workspace):
        meta, content, etag = get_skill("hello-world", backend="opencode")
        assert meta["description"] == "Backend skill"
        assert content.endswith("OpenCode body")
        assert etag

    def test_create_skill_uses_codex_root(self, empty_workspace):
        etag, created = create_or_update_skill(
            "codex-only",
            "---\nname: codex-only\ndescription: Codex\n---\nBody",
            backend="codex",
        )
        assert created is True
        assert etag
        assert (empty_workspace / ".agents" / "skills" / "codex-only" / "SKILL.md").is_file()

    def test_delete_skill_uses_opencode_root(self, multi_backend_workspace):
        delete_skill("hello-world", backend="opencode")
        assert not (
            multi_backend_workspace / ".opencode" / "skills" / "hello-world"
        ).exists()
        assert (
            multi_backend_workspace / ".claude" / "skills" / "hello-world"
        ).exists()

    def test_rejects_unknown_skill_backend(self, workspace):
        with pytest.raises(ValueError, match="Unsupported skill backend"):
            list_skills(backend="bad-backend")
```

- [ ] **Step 2: Run admin skill service tests and verify failure**

Run:

```bash
uv run pytest tests/test_admin_skills.py::TestBackendSkillRoots -q
```

Expected: fails because skill helpers do not accept `backend`.

- [ ] **Step 3: Implement skill root mapping**

In `src/admin_service.py`, replace `_SKILL_DIR_PREFIX` with:

```python
_SKILL_DIR_BY_BACKEND = {
    "claude": ".claude/skills",
    "codex": ".agents/skills",
    "opencode": ".opencode/skills",
}
```

Add helper functions:

```python
def _skill_dir_prefix(backend: str = "claude") -> str:
    try:
        return _SKILL_DIR_BY_BACKEND[backend]
    except KeyError as exc:
        raise ValueError(f"Unsupported skill backend: {backend}") from exc


def _skill_rel_path(name: str, backend: str = "claude") -> str:
    return f"{_skill_dir_prefix(backend)}/{name}/SKILL.md"


def _skill_dir(root: Path, name: str, backend: str = "claude") -> Path:
    return root / _skill_dir_prefix(backend) / name
```

Change function signatures:

```python
def list_skills(backend: str = "claude") -> List[Dict[str, Any]]:
def get_skill(name: str, backend: str = "claude") -> Tuple[Dict[str, Any], str, str]:
def create_or_update_skill(
    name: str,
    content: str,
    expected_etag: Optional[str] = None,
    backend: str = "claude",
) -> Tuple[str, bool]:
def delete_skill(name: str, backend: str = "claude") -> None:
```

Update each function body:

```python
skills_dir = root / _skill_dir_prefix(backend)
rel_path = _skill_rel_path(name, backend)
skill_dir = _skill_dir(root, name, backend)
```

In `create_or_update_skill`, keep backward compatibility by preserving positional `expected_etag` as the third argument and making `backend` the fourth.

- [ ] **Step 4: Extend file allowlist**

In `src/admin_service.py`, add backend skill directories to `_ALLOWED_DIRS`:

```python
_ALLOWED_DIRS: Tuple[str, ...] = (
    ".claude/agents/",
    ".claude/skills/",
    ".claude/commands/",
    ".agents/skills/",
    ".opencode/skills/",
)
```

- [ ] **Step 5: Run admin skill tests**

Run:

```bash
uv run pytest tests/test_admin_skills.py -q
```

Expected: all admin skill tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/admin_service.py tests/test_admin_skills.py
git commit -m "feat: manage backend skill roots"
```

---

### Task 5: Backend Query Parameter For Admin Skill Routes

**Files:**
- Modify: `src/routes/admin.py`
- Test: `tests/test_admin_skills.py`

- [ ] **Step 1: Write failing route tests**

Add these tests inside `TestSkillsAPI` in `tests/test_admin_skills.py`:

```python
    def test_list_codex_skills_query_param(self, admin_client, workspace):
        codex_skill = workspace / ".agents" / "skills" / "codex-skill"
        codex_skill.mkdir(parents=True)
        (codex_skill / "SKILL.md").write_text(
            "---\nname: codex-skill\ndescription: Codex\n---\nBody"
        )
        r = admin_client.get("/admin/api/skills?backend=codex")
        assert r.status_code == 200
        names = {s["name"] for s in r.json()["skills"]}
        assert names == {"codex-skill"}

    def test_put_opencode_skill_query_param(self, admin_client, workspace):
        r = admin_client.put(
            "/admin/api/skills/open-skill?backend=opencode",
            json={"content": "---\nname: open-skill\ndescription: OpenCode\n---\nBody"},
        )
        assert r.status_code == 201
        assert (
            workspace / ".opencode" / "skills" / "open-skill" / "SKILL.md"
        ).is_file()

    def test_invalid_skill_backend_query_param(self, admin_client):
        r = admin_client.get("/admin/api/skills?backend=invalid")
        assert r.status_code == 400
        assert "Unsupported skill backend" in r.json()["error"]
```

- [ ] **Step 2: Run route tests and verify failure**

Run:

```bash
uv run pytest tests/test_admin_skills.py::TestSkillsAPI -q
```

Expected: query parameter is ignored or unsupported backend is not rejected.

- [ ] **Step 3: Thread backend query parameter through routes**

In `src/routes/admin.py`, import `Query` if it is not already imported:

```python
from fastapi import APIRouter, Depends, Header, Query
```

Change route signatures and calls:

```python
@router.get("/api/skills")
async def list_skills_endpoint(
    backend: str = Query("claude"),
    _=Depends(require_admin),
):
    try:
        return {"skills": list_skills(backend=backend)}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})
```

```python
@router.get("/api/skills/{name}")
async def get_skill_endpoint(
    name: str,
    backend: str = Query("claude"),
    _=Depends(require_admin),
):
    try:
        meta, content, etag = get_skill(name, backend=backend)
        return {"name": name, "metadata": meta, "content": content, "etag": etag}
```

```python
@router.put("/api/skills/{name}")
async def put_skill_endpoint(
    name: str,
    body: SkillWriteRequest,
    backend: str = Query("claude"),
    _=Depends(require_admin),
    if_match: Optional[str] = Header(None),
):
    expected_etag = body.etag or if_match
    try:
        new_etag, created = create_or_update_skill(
            name,
            body.content,
            expected_etag,
            backend=backend,
        )
```

```python
@router.delete("/api/skills/{name}")
async def delete_skill_endpoint(
    name: str,
    backend: str = Query("claude"),
    _=Depends(require_admin),
):
    try:
        delete_skill(name, backend=backend)
```

- [ ] **Step 4: Run admin skill tests**

Run:

```bash
uv run pytest tests/test_admin_skills.py -q
```

Expected: all admin skill tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/routes/admin.py tests/test_admin_skills.py
git commit -m "feat: expose backend skill admin parameter"
```

---

### Task 6: Full Regression And Documentation Check

**Files:**
- Modify: no production files unless tests reveal a defect.
- Test: full relevant suite.

- [ ] **Step 1: Run targeted backend workspace tests**

Run:

```bash
uv run pytest tests/test_workspace_manager.py tests/test_responses_user.py tests/test_admin_skills.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run formatting and lint checks**

Run:

```bash
uv run ruff format --check src/workspace_manager.py src/routes/responses.py src/admin_service.py src/routes/admin.py tests/test_workspace_manager.py tests/test_responses_user.py tests/test_admin_skills.py
uv run ruff check src/workspace_manager.py src/routes/responses.py src/admin_service.py src/routes/admin.py tests/test_workspace_manager.py tests/test_responses_user.py tests/test_admin_skills.py
```

Expected: both commands pass. If formatting fails, run:

```bash
uv run ruff format src/workspace_manager.py src/routes/responses.py src/admin_service.py src/routes/admin.py tests/test_workspace_manager.py tests/test_responses_user.py tests/test_admin_skills.py
```

Then rerun the two checks.

- [ ] **Step 3: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected: the full suite passes.

- [ ] **Step 4: Update PR body**

If this work remains on PR #100, update the PR description to mention:

```markdown
- Adds backend-isolated user workspaces: `<base>/<user>/<backend>`.
- Syncs backend-native agent templates and skill roots for Claude, Codex, and OpenCode.
- Keeps anonymous `_tmp_<uuid>` workspace behavior unchanged.
- Adds backend-aware admin skill helpers and query parameters.
```

- [ ] **Step 5: Commit final verification note if files changed**

If Step 2 formatting changed files, commit them:

```bash
git add src tests docs
git commit -m "chore: verify backend isolated workspaces"
```

If no files changed, do not create an empty commit.
