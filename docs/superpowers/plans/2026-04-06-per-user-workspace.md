# Per-User Workspace Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `user` parameter to `/v1/responses` and provide per-user isolated working directories with `.claude` template syncing.

**Architecture:** New `WorkspaceManager` resolves user strings to filesystem paths, copies `.claude` config from `CLAUDE_CWD` on new sessions, and passes `cwd` per-request through `BackendClient.run_completion` to the SDK's `ClaudeAgentOptions`. Temporary workspaces for anonymous users are cleaned up with session expiry.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic, shutil, pathlib, pytest

**Spec:** `docs/superpowers/specs/2026-04-06-per-user-workspace-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/workspace_manager.py` | **NEW.** `WorkspaceManager` class: sanitize user, resolve path, sync `.claude` template, cleanup temp workspaces |
| `src/response_models.py` | Add `user: Optional[str]` field to `ResponseCreateRequest` |
| `src/session_manager.py` | Add `user` and `workspace` fields to `Session` dataclass; hook temp workspace cleanup into expiry |
| `src/constants.py` | Add `USER_WORKSPACES_DIR` constant |
| `src/backends/base.py` | Add `cwd: Optional[str] = None` to `BackendClient.run_completion` Protocol |
| `src/backends/claude/client.py` | Accept `cwd` in `run_completion` and `_build_sdk_options`, override `self.cwd` when provided |
| `src/routes/responses.py` | Wire workspace resolution, user validation, per-request `ImageHandler`, pass `cwd` to `run_completion` |
| `.env.example` | Document `USER_WORKSPACES_DIR` |
| `tests/test_workspace_manager.py` | **NEW.** Unit tests for `WorkspaceManager` |
| `tests/test_responses_user.py` | **NEW.** Integration tests for user param in `/v1/responses` |

---

### Task 1: Add `USER_WORKSPACES_DIR` constant

**Files:**
- Modify: `src/constants.py:37-38`
- Modify: `.env.example:24-25`

- [ ] **Step 1: Add constant to `src/constants.py`**

After line 38 (`SESSION_MAX_AGE_MINUTES`), add:

```python
# Per-user workspace isolation
# Base directory for user workspaces. Falls back to CLAUDE_CWD if empty.
USER_WORKSPACES_DIR = os.getenv("USER_WORKSPACES_DIR", "")
```

- [ ] **Step 2: Document in `.env.example`**

After the `CLAUDE_CWD` line (line 24), add:

```env
# Per-user workspace isolation
# Base directory for user workspaces ({dir}/{user}/ per user)
# Falls back to CLAUDE_CWD if empty. If both empty, uses temp dir.
# USER_WORKSPACES_DIR=/data/workspaces
```

- [ ] **Step 3: Commit**

```bash
git add src/constants.py .env.example
git commit -m "feat(config): add USER_WORKSPACES_DIR constant"
```

---

### Task 2: Create `WorkspaceManager`

**Files:**
- Create: `src/workspace_manager.py`
- Test: `tests/test_workspace_manager.py`

- [ ] **Step 1: Write failing tests for `WorkspaceManager`**

Create `tests/test_workspace_manager.py`:

```python
"""Unit tests for WorkspaceManager."""

import re
import shutil
from pathlib import Path

import pytest

from src.workspace_manager import WorkspaceManager


@pytest.fixture
def tmp_base(tmp_path):
    """Provide a temporary base directory for workspaces."""
    return tmp_path / "workspaces"


@pytest.fixture
def tmp_template(tmp_path):
    """Provide a temporary CLAUDE_CWD with a .claude folder."""
    template = tmp_path / "template"
    claude_dir = template / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text('{"key": "value"}')
    (claude_dir / "subdir").mkdir()
    (claude_dir / "subdir" / "nested.txt").write_text("nested content")
    return template


@pytest.fixture
def manager(tmp_base, tmp_template):
    return WorkspaceManager(base_path=tmp_base, template_source=tmp_template)


@pytest.fixture
def manager_no_template(tmp_base):
    return WorkspaceManager(base_path=tmp_base, template_source=None)


class TestSanitize:
    def test_valid_usernames(self, manager):
        assert manager._sanitize("alice") == "alice"
        assert manager._sanitize("user-123") == "user-123"
        assert manager._sanitize("Bob_Smith") == "Bob_Smith"
        assert manager._sanitize("a") == "a"

    def test_rejects_empty_string(self, manager):
        with pytest.raises(ValueError, match="empty"):
            manager._sanitize("")

    def test_rejects_path_traversal(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("../etc/passwd")

    def test_rejects_dots_only(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("..")
        with pytest.raises(ValueError):
            manager._sanitize(".")

    def test_rejects_invalid_characters(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("user/name")
        with pytest.raises(ValueError):
            manager._sanitize("user name")
        with pytest.raises(ValueError):
            manager._sanitize("user@name")

    def test_rejects_too_long(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("a" * 64)

    def test_rejects_starting_with_non_alnum(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("-alice")
        with pytest.raises(ValueError):
            manager._sanitize("_alice")


class TestResolve:
    def test_creates_user_directory(self, manager, tmp_base):
        workspace = manager.resolve("alice", sync_template=False)
        assert workspace == tmp_base / "alice"
        assert workspace.is_dir()

    def test_returns_existing_directory(self, manager, tmp_base):
        first = manager.resolve("alice", sync_template=False)
        (first / "myfile.txt").write_text("data")
        second = manager.resolve("alice", sync_template=False)
        assert first == second
        assert (second / "myfile.txt").read_text() == "data"

    def test_anonymous_creates_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve(None, sync_template=False)
        assert workspace.parent == tmp_base
        assert workspace.name.startswith("_tmp_")

    def test_anonymous_returns_different_dirs(self, manager):
        w1 = manager.resolve(None, sync_template=False)
        w2 = manager.resolve(None, sync_template=False)
        assert w1 != w2

    def test_sync_template_copies_claude_dir(self, manager, tmp_base):
        workspace = manager.resolve("bob", sync_template=True)
        claude_dir = workspace / ".claude"
        assert claude_dir.is_dir()
        assert (claude_dir / "settings.json").read_text() == '{"key": "value"}'
        assert (claude_dir / "subdir" / "nested.txt").read_text() == "nested content"

    def test_sync_template_false_skips_copy(self, manager, tmp_base):
        workspace = manager.resolve("carol", sync_template=False)
        assert not (workspace / ".claude").exists()

    def test_sync_template_overwrites_existing(self, manager, tmp_base, tmp_template):
        workspace = manager.resolve("dave", sync_template=True)
        # Modify the copied file
        (workspace / ".claude" / "settings.json").write_text('{"modified": true}')
        # Re-sync should overwrite
        manager.resolve("dave", sync_template=True)
        assert (workspace / ".claude" / "settings.json").read_text() == '{"key": "value"}'

    def test_no_template_source_skips_sync(self, manager_no_template, tmp_base):
        workspace = manager_no_template.resolve("eve", sync_template=True)
        assert not (workspace / ".claude").exists()


class TestCleanupTempWorkspace:
    def test_removes_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve(None, sync_template=False)
        assert workspace.is_dir()
        manager.cleanup_temp_workspace(workspace)
        assert not workspace.exists()

    def test_ignores_non_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve("alice", sync_template=False)
        (workspace / "important.txt").write_text("keep")
        manager.cleanup_temp_workspace(workspace)
        assert workspace.exists()  # Not deleted — not a _tmp_ dir

    def test_ignores_nonexistent_directory(self, manager):
        manager.cleanup_temp_workspace(Path("/nonexistent/_tmp_abc"))
        # No error raised
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_workspace_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.workspace_manager'`

- [ ] **Step 3: Implement `WorkspaceManager`**

Create `src/workspace_manager.py`:

```python
"""Per-user workspace isolation manager.

Resolves user identifiers to filesystem paths, syncs `.claude` configuration
templates from CLAUDE_CWD, and manages temporary workspace cleanup.
"""

import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_USER_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


class WorkspaceManager:
    """Manages per-user working directories.

    Parameters:
        base_path: Root directory for all user workspaces.
        template_source: Directory containing `.claude/` to copy into new workspaces.
            Typically the ``CLAUDE_CWD`` value.
    """

    def __init__(self, base_path: Path, template_source: Optional[Path] = None):
        self.base_path = Path(base_path)
        self.template_source = Path(template_source) if template_source else None

    def resolve(self, user: Optional[str] = None, sync_template: bool = False) -> Path:
        """Return the workspace path for *user*, creating it if necessary.

        When *user* is ``None``, a temporary directory (``_tmp_{uuid}``) is
        created under *base_path*.  When *sync_template* is ``True`` and a
        template source is configured, the ``.claude/`` directory is copied
        (overwriting existing files).
        """
        if user is not None:
            sanitized = self._sanitize(user)
            workspace = self.base_path / sanitized
        else:
            workspace = self.base_path / f"_tmp_{uuid.uuid4().hex}"

        workspace.mkdir(parents=True, exist_ok=True)

        if sync_template:
            self._sync_template(workspace)

        return workspace

    def cleanup_temp_workspace(self, workspace: Path) -> None:
        """Remove a temporary workspace directory.

        Only directories whose name starts with ``_tmp_`` are removed.
        Permanent user workspaces are left untouched.
        """
        if not workspace.exists():
            return
        if not workspace.name.startswith("_tmp_"):
            logger.debug("Skipping cleanup of non-temporary workspace: %s", workspace)
            return
        shutil.rmtree(workspace, ignore_errors=True)
        logger.info("Cleaned up temporary workspace: %s", workspace)

    def _sanitize(self, user: str) -> str:
        """Validate and return *user* as a safe directory name.

        Raises ``ValueError`` for empty, too-long, or disallowed strings.
        """
        if not user:
            raise ValueError("User identifier must not be empty")
        if not _USER_PATTERN.match(user):
            raise ValueError(
                f"Invalid user identifier: {user!r}. "
                "Must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$"
            )
        return user

    def _sync_template(self, workspace: Path) -> None:
        """Copy ``.claude/`` from template source into *workspace*."""
        if self.template_source is None:
            return
        src = self.template_source / ".claude"
        if not src.is_dir():
            logger.debug("Template source .claude/ not found at %s, skipping sync", src)
            return
        dst = workspace / ".claude"
        shutil.copytree(src, dst, dirs_exist_ok=True)
        logger.debug("Synced .claude/ template to %s", dst)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_workspace_manager.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/workspace_manager.py tests/test_workspace_manager.py
git commit -m "feat: add WorkspaceManager for per-user workspace isolation"
```

---

### Task 3: Add `user` field to `ResponseCreateRequest`

**Files:**
- Modify: `src/response_models.py:16-33`

- [ ] **Step 1: Add `user` field**

In `ResponseCreateRequest`, after the `max_output_tokens` field (line 33), add:

```python
    user: Optional[str] = Field(
        default=None,
        description="Unique user identifier for workspace isolation",
    )
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `uv run pytest tests/test_main_api_unit.py -v -k "response"`
Expected: All existing response tests PASS (new optional field is backward compatible)

- [ ] **Step 3: Commit**

```bash
git add src/response_models.py
git commit -m "feat(models): add user field to ResponseCreateRequest"
```

---

### Task 4: Add `user` and `workspace` fields to `Session`

**Files:**
- Modify: `src/session_manager.py:44-63` (Session dataclass)
- Modify: `src/session_manager.py:134-140` (_purge_all_expired)

- [ ] **Step 1: Write failing test for session user/workspace fields**

Add to `tests/test_workspace_manager.py` (at the bottom):

```python
from src.session_manager import Session


class TestSessionUserField:
    def test_session_has_user_field(self):
        session = Session(session_id="test-1", user="alice")
        assert session.user == "alice"

    def test_session_user_defaults_to_none(self):
        session = Session(session_id="test-2")
        assert session.user is None

    def test_session_has_workspace_field(self):
        session = Session(session_id="test-3", workspace="/tmp/ws/alice")
        assert session.workspace == "/tmp/ws/alice"

    def test_session_workspace_defaults_to_none(self):
        session = Session(session_id="test-4")
        assert session.workspace is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workspace_manager.py::TestSessionUserField -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'user'`

- [ ] **Step 3: Add fields to Session dataclass**

In `src/session_manager.py`, in the `Session` dataclass (after line 54 `provider_session_id`), add:

```python
    user: Optional[str] = None
    workspace: Optional[str] = None
```

- [ ] **Step 4: Hook temp workspace cleanup into session expiry**

In `src/session_manager.py`, modify `_purge_all_expired` (around line 134):

```python
    def _purge_all_expired(self) -> int:
        """Remove every expired session.  Returns the count removed."""
        expired = [sid for sid, s in self.sessions.items() if s.is_expired()]
        for sid in expired:
            session = self.sessions[sid]
            # Clean up temporary workspace if present
            if session.workspace:
                self._cleanup_workspace(session.workspace)
            del self.sessions[sid]
            logger.info(f"Cleaned up expired session: {sid}")
        return len(expired)

    def _cleanup_workspace(self, workspace_path: str) -> None:
        """Remove temporary workspace directory on session expiry."""
        try:
            from src.workspace_manager import workspace_manager

            if workspace_manager is not None:
                workspace_manager.cleanup_temp_workspace(Path(workspace_path))
        except Exception:
            logger.debug("Workspace cleanup skipped for %s", workspace_path, exc_info=True)
```

Add `from pathlib import Path` to the existing imports at the top of the file.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_workspace_manager.py::TestSessionUserField -v`
Expected: PASS

Run: `uv run pytest tests/ -v -k "session"`
Expected: All existing session tests still PASS

- [ ] **Step 6: Commit**

```bash
git add src/session_manager.py tests/test_workspace_manager.py
git commit -m "feat(session): add user and workspace fields, temp workspace cleanup on expiry"
```

---

### Task 5: Add `cwd` parameter to `BackendClient` Protocol and `ClaudeCodeCLI`

**Files:**
- Modify: `src/backends/base.py:111-127`
- Modify: `src/backends/claude/client.py:286-363` (`_build_sdk_options`)
- Modify: `src/backends/claude/client.py:483-533` (`run_completion`)

- [ ] **Step 1: Write failing test**

Add to `tests/test_workspace_manager.py`:

```python
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path


class TestClaudeCLICwdOverride:
    def test_build_sdk_options_uses_override_cwd(self):
        """_build_sdk_options should use cwd param when provided."""
        with patch("src.backends.claude.client.validate_claude_code_auth", return_value=(True, {})):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = ClaudeCodeCLI(cwd="/tmp/default")
            options = cli._build_sdk_options(cwd=Path("/tmp/override"))
            assert str(options.cwd) == "/tmp/override"

    def test_build_sdk_options_falls_back_to_self_cwd(self):
        """_build_sdk_options should use self.cwd when cwd param is None."""
        with patch("src.backends.claude.client.validate_claude_code_auth", return_value=(True, {})):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = ClaudeCodeCLI(cwd="/tmp/default")
            options = cli._build_sdk_options(cwd=None)
            assert str(options.cwd) == "/tmp/default"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workspace_manager.py::TestClaudeCLICwdOverride -v`
Expected: FAIL — `TypeError: _build_sdk_options() got an unexpected keyword argument 'cwd'`

- [ ] **Step 3: Add `cwd` to `BackendClient.run_completion` Protocol**

In `src/backends/base.py`, add `cwd` parameter to `run_completion` (after `task_budget`, before `**_extra`):

```python
    def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[str] = None,
        **_extra: Any,
    ) -> AsyncIterator[Dict[str, Any]]: ...
```

- [ ] **Step 4: Add `cwd` to `ClaudeCodeCLI._build_sdk_options`**

In `src/backends/claude/client.py`, modify `_build_sdk_options` signature (around line 286) to add `cwd: Optional[Path] = None`:

```python
    def _build_sdk_options(
        self,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        _custom_base: object = _UNSET,
        extra_env: Optional[Dict[str, str]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[Path] = None,
    ) -> ClaudeAgentOptions:
```

Then change line 304 from:

```python
        options = ClaudeAgentOptions(
            max_turns=max_turns, cwd=self.cwd, setting_sources=["project", "local"]
        )
```

to:

```python
        effective_cwd = cwd or self.cwd
        options = ClaudeAgentOptions(
            max_turns=max_turns, cwd=effective_cwd, setting_sources=["project", "local"]
        )
```

- [ ] **Step 5: Add `cwd` to `ClaudeCodeCLI.run_completion`**

In `src/backends/claude/client.py`, modify `run_completion` signature (around line 483) to add `cwd`:

```python
    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[str] = None,
        **_extra,
    ) -> AsyncGenerator[Dict[str, Any], None]:
```

Then in the `_build_sdk_options` call (around line 511), add `cwd`:

```python
                options = self._build_sdk_options(
                    model=model,
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    allowed_tools=allowed_tools,
                    _custom_base=_extra.get("_custom_base", self._UNSET),
                    disallowed_tools=disallowed_tools,
                    permission_mode=permission_mode,
                    output_format=output_format,
                    mcp_servers=mcp_servers,
                    session_id=session_id,
                    resume=resume,
                    extra_env=_extra.get("_metadata"),
                    task_budget=task_budget,
                    cwd=Path(cwd) if cwd else None,
                )
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_workspace_manager.py::TestClaudeCLICwdOverride -v`
Expected: PASS

Run: `uv run pytest tests/ -v -k "claude or completion or response"`
Expected: All existing tests still PASS

- [ ] **Step 7: Commit**

```bash
git add src/backends/base.py src/backends/claude/client.py tests/test_workspace_manager.py
git commit -m "feat(backend): add cwd parameter to run_completion for per-request workspace"
```

---

### Task 6: Wire workspace into `/v1/responses` endpoint

**Files:**
- Modify: `src/routes/responses.py`
- Create: `tests/test_responses_user.py`

- [ ] **Step 1: Write failing integration tests**

Create `tests/test_responses_user.py`:

```python
"""Integration tests for user parameter in /v1/responses."""

from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.session_manager import session_manager, Session


@pytest.fixture
def isolated_session_manager():
    """Provide a clean session manager for each test."""
    with session_manager.lock:
        session_manager.sessions.clear()
    yield session_manager
    with session_manager.lock:
        session_manager.sessions.clear()


def _mock_backend():
    """Create a mock backend that returns a simple response."""
    backend = MagicMock()
    backend.name = "claude"
    backend.owned_by = "anthropic"
    backend.image_handler = MagicMock()

    async def fake_run_completion(**kwargs):
        yield {"type": "result", "subtype": "success", "result": "Hello from Claude"}

    backend.run_completion = MagicMock(side_effect=fake_run_completion)
    backend.parse_message = MagicMock(return_value="Hello from Claude")
    backend.estimate_token_usage = MagicMock(return_value={"input_tokens": 10, "output_tokens": 5})
    return backend


def _patch_all(mock_backend):
    """Return a dict of patches for responses endpoint."""
    return {
        "auth": patch("src.routes.responses.verify_api_key", new_callable=AsyncMock),
        "backend": patch(
            "src.routes.responses.resolve_and_get_backend",
            return_value=(
                MagicMock(backend="claude", provider_model="sonnet"),
                mock_backend,
            ),
        ),
        "auth_validate": patch("src.routes.responses.validate_backend_auth_or_raise"),
        "validate_image": patch("src.routes.responses.validate_image_request"),
    }


class TestUserParam:
    def test_user_field_accepted(self, isolated_session_manager):
        """POST /v1/responses accepts user field."""
        mock_backend = _mock_backend()
        patches = _patch_all(mock_backend)
        with patches["auth"], patches["backend"], patches["auth_validate"], patches["validate_image"]:
            with patch("src.routes.responses.workspace_manager") as mock_wm:
                mock_wm.resolve.return_value = Path("/tmp/ws/alice")
                with TestClient(app) as client:
                    resp = client.post(
                        "/v1/responses",
                        json={"model": "sonnet", "input": "hello", "user": "alice"},
                    )
                    assert resp.status_code == 200
                    mock_wm.resolve.assert_called_once_with("alice", sync_template=True)

    def test_user_none_creates_temp_workspace(self, isolated_session_manager):
        """POST /v1/responses without user creates temp workspace."""
        mock_backend = _mock_backend()
        patches = _patch_all(mock_backend)
        with patches["auth"], patches["backend"], patches["auth_validate"], patches["validate_image"]:
            with patch("src.routes.responses.workspace_manager") as mock_wm:
                mock_wm.resolve.return_value = Path("/tmp/ws/_tmp_abc123")
                with TestClient(app) as client:
                    resp = client.post(
                        "/v1/responses",
                        json={"model": "sonnet", "input": "hello"},
                    )
                    assert resp.status_code == 200
                    mock_wm.resolve.assert_called_once_with(None, sync_template=True)

    def test_cwd_passed_to_run_completion(self, isolated_session_manager):
        """Workspace path should be passed as cwd to backend.run_completion."""
        mock_backend = _mock_backend()
        patches = _patch_all(mock_backend)
        with patches["auth"], patches["backend"], patches["auth_validate"], patches["validate_image"]:
            with patch("src.routes.responses.workspace_manager") as mock_wm:
                mock_wm.resolve.return_value = Path("/tmp/ws/alice")
                with TestClient(app) as client:
                    resp = client.post(
                        "/v1/responses",
                        json={"model": "sonnet", "input": "hello", "user": "alice"},
                    )
                    assert resp.status_code == 200
                    call_kwargs = mock_backend.run_completion.call_args
                    assert call_kwargs.kwargs.get("cwd") == str(Path("/tmp/ws/alice"))

    def test_invalid_user_returns_400(self, isolated_session_manager):
        """Invalid user identifier should return 400."""
        mock_backend = _mock_backend()
        patches = _patch_all(mock_backend)
        with patches["auth"], patches["backend"], patches["auth_validate"], patches["validate_image"]:
            with patch("src.routes.responses.workspace_manager") as mock_wm:
                mock_wm.resolve.side_effect = ValueError("Invalid user")
                with TestClient(app) as client:
                    resp = client.post(
                        "/v1/responses",
                        json={"model": "sonnet", "input": "hello", "user": "../bad"},
                    )
                    assert resp.status_code == 400


class TestUserSessionBinding:
    def test_followup_with_same_user_succeeds(self, isolated_session_manager):
        """Continuing a session with the same user should work."""
        # Pre-create a session with user="alice"
        session = isolated_session_manager.get_or_create_session("test-sess-1")
        session.user = "alice"
        session.workspace = "/tmp/ws/alice"
        session.turn_counter = 1

        mock_backend = _mock_backend()
        patches = _patch_all(mock_backend)
        with patches["auth"], patches["backend"], patches["auth_validate"], patches["validate_image"]:
            with patch("src.routes.responses.workspace_manager") as mock_wm:
                mock_wm.resolve.return_value = Path("/tmp/ws/alice")
                with TestClient(app) as client:
                    resp = client.post(
                        "/v1/responses",
                        json={
                            "model": "sonnet",
                            "input": "follow up",
                            "user": "alice",
                            "previous_response_id": "resp_test-sess-1_1",
                        },
                    )
                    assert resp.status_code == 200
                    # sync_template should be False for follow-up
                    mock_wm.resolve.assert_called_once_with("alice", sync_template=False)

    def test_followup_with_different_user_returns_400(self, isolated_session_manager):
        """Continuing a session with a different user should fail."""
        session = isolated_session_manager.get_or_create_session("test-sess-2")
        session.user = "alice"
        session.workspace = "/tmp/ws/alice"
        session.turn_counter = 1

        mock_backend = _mock_backend()
        patches = _patch_all(mock_backend)
        with patches["auth"], patches["backend"], patches["auth_validate"], patches["validate_image"]:
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/responses",
                    json={
                        "model": "sonnet",
                        "input": "hijack",
                        "user": "eve",
                        "previous_response_id": "resp_test-sess-2_1",
                    },
                )
                assert resp.status_code == 400
                assert "user mismatch" in resp.json()["detail"].lower()

    def test_session_stores_user_and_workspace(self, isolated_session_manager):
        """New session should store user and workspace path."""
        mock_backend = _mock_backend()
        patches = _patch_all(mock_backend)
        with patches["auth"], patches["backend"], patches["auth_validate"], patches["validate_image"]:
            with patch("src.routes.responses.workspace_manager") as mock_wm:
                mock_wm.resolve.return_value = Path("/tmp/ws/alice")
                with TestClient(app) as client:
                    resp = client.post(
                        "/v1/responses",
                        json={"model": "sonnet", "input": "hello", "user": "alice"},
                    )
                    assert resp.status_code == 200

                    # Find the created session and check user/workspace
                    sessions = isolated_session_manager.list_sessions()
                    assert len(sessions) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_responses_user.py -v`
Expected: FAIL — `ImportError` or `AttributeError` (workspace_manager not wired)

- [ ] **Step 3: Create workspace_manager singleton and add to `src/workspace_manager.py`**

At the bottom of `src/workspace_manager.py`, add the global singleton:

```python
import os
from src.constants import USER_WORKSPACES_DIR


def _resolve_base_path() -> Path:
    """Determine the workspace base path from environment."""
    if USER_WORKSPACES_DIR:
        return Path(USER_WORKSPACES_DIR)
    claude_cwd = os.getenv("CLAUDE_CWD", "")
    if claude_cwd:
        return Path(claude_cwd)
    import tempfile
    return Path(tempfile.mkdtemp(prefix="claude_workspaces_"))


def _resolve_template_source() -> Optional[Path]:
    """Determine the .claude template source directory."""
    claude_cwd = os.getenv("CLAUDE_CWD", "")
    if claude_cwd:
        p = Path(claude_cwd)
        if (p / ".claude").is_dir():
            return p
    return None


workspace_manager = WorkspaceManager(
    base_path=_resolve_base_path(),
    template_source=_resolve_template_source(),
)
```

- [ ] **Step 4: Wire into `src/routes/responses.py`**

Add import at the top of `src/routes/responses.py`:

```python
from src.workspace_manager import workspace_manager
from src.image_handler import ImageHandler
```

The wiring integrates into `create_response` in three insertion points.  The key ordering is:

1. Compute `is_new_session` early (line 143, after `validate_image_request`)
2. Existing session resolution block runs (lines 153-181, unchanged)
3. **NEW block** after session resolution: user validation + workspace resolution
4. Existing prompt conversion block, with updated `image_handler`

**Insertion A** — after `validate_image_request(body, backend)` (line 143), compute `is_new_session` early:

```python
    # Moved earlier — needed for workspace sync_template decision
    is_new_session = body.previous_response_id is None
```

**Insertion B** — after the session resolution block (after line 181 `session = session_manager.get_or_create_session(session_id)`), add user validation and workspace resolution:

```python
    # --- Per-user workspace isolation ---
    if not is_new_session and session.user != body.user:
        raise HTTPException(
            status_code=400,
            detail=f"User mismatch: session belongs to {session.user!r}, "
            f"but request specifies {body.user!r}",
        )

    if is_new_session:
        try:
            workspace = workspace_manager.resolve(body.user, sync_template=True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        session.user = body.user
        session.workspace = str(workspace)
    else:
        # Follow-up: reuse stored workspace, no template sync
        if session.workspace:
            workspace = Path(session.workspace)
        else:
            try:
                workspace = workspace_manager.resolve(body.user, sync_template=False)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            session.workspace = str(workspace)
    workspace_str = str(workspace)
```

**Insertion C** — replace the existing `image_handler` line (line 202):

```python
    # Per-request ImageHandler pointing to user workspace
    image_handler = ImageHandler(workspace)
```

**Remove** the existing `is_new_session = body.previous_response_id is None` line (line 207) — it's now computed earlier in Insertion A.

**Add `cwd=workspace_str`** to both streaming and non-streaming `run_completion` calls:

In `_responses_streaming_preflight` `chunk_kwargs` dict (around line 106), add:

```python
            cwd=workspace_str,
```

In the non-streaming `backend.run_completion` call (around line 293), add:

```python
                cwd=workspace_str,
```

- [ ] **Step 5: Pass `workspace_str` to `_responses_streaming_preflight`**

Update the `_responses_streaming_preflight` function signature to accept `workspace_str: str`:

```python
async def _responses_streaming_preflight(
    body: ResponseCreateRequest,
    resolved: ResolvedModel,
    backend: "BackendClient",
    session,
    session_id: str,
    is_new_session: bool,
    prompt: str,
    system_prompt: Optional[str],
    workspace_str: str,
) -> Dict[str, Any]:
```

And add `cwd=workspace_str` to the `chunk_kwargs` dict.

Update the call site to pass `workspace_str`:

```python
        preflight = await _responses_streaming_preflight(
            body, resolved, backend, session, session_id, is_new_session, prompt, system_prompt,
            workspace_str=workspace_str,
        )
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_responses_user.py -v`
Expected: All tests PASS

Run: `uv run pytest tests/test_main_api_unit.py -v -k "response"`
Expected: All existing response tests still PASS

- [ ] **Step 7: Commit**

```bash
git add src/routes/responses.py src/workspace_manager.py tests/test_responses_user.py
git commit -m "feat(responses): wire per-user workspace isolation into /v1/responses"
```

---

### Task 7: Update documentation

**Files:**
- Modify: `.env.example` (already done in Task 1)
- Modify: `README.md`

- [ ] **Step 1: Add per-user workspace section to README**

Find the appropriate section in `README.md` (near the Configuration/Environment Variables section) and add:

```markdown
### Per-User Workspace Isolation

Each `/v1/responses` request can include a `user` field to isolate working directories:

```json
{
  "model": "sonnet",
  "input": "Create a Python script",
  "user": "alice"
}
```

**Behavior:**
- `user` specified: Permanent workspace at `{base_path}/{user}/` (survives server restarts)
- `user` omitted: Temporary workspace created per session, cleaned up on expiry
- On new sessions, `.claude/` config is copied from `CLAUDE_CWD` to the workspace

**Configuration:**
- `USER_WORKSPACES_DIR`: Base directory for workspaces (defaults to `CLAUDE_CWD`)
- User identifiers must match `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$`
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add per-user workspace isolation documentation"
```

---

### Task 8: Final integration verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run lint and format**

Run: `uv run ruff check --fix . && uv run ruff format .`
Expected: No errors

- [ ] **Step 3: Run coverage check**

Run: `uv run pytest --cov=src --cov-report=term-missing tests/test_workspace_manager.py tests/test_responses_user.py`
Expected: High coverage for `src/workspace_manager.py` and changed lines in `src/routes/responses.py`

- [ ] **Step 4: Final commit (if lint/format made changes)**

```bash
git add -A
git commit -m "style: lint and format per-user workspace changes"
```
