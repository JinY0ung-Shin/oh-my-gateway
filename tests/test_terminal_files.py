"""Tests for the Open Terminal-compatible read-only workspace file server."""

import hashlib
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.auth as auth_module
from src.routes.terminal_files import router
from src.routes import terminal_files as tf


def _patch_api_key(monkeypatch, value: str) -> None:
    # test_auth_unit.py importlib.reload(src.auth)s mid-suite, splitting the
    # singleton: verify_api_key reads the live src.auth.auth_manager while this
    # module's import-time binding feeds _ensure_api_key — patch both objects.
    for manager in {tf.auth_manager, auth_module.auth_manager}:
        monkeypatch.setattr(manager, "get_api_key", lambda: value)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "alice" / "claude"
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("hello\nworld\n")
    (root / "sub").mkdir()
    (root / "sub" / "inner.md").write_text("# inner")
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    # Hidden entries — filtered/blocked when WORKSPACE_HIDE_DOTFILES is on.
    (root / ".secret_dir").mkdir()
    (root / ".secret_dir" / "k.txt").write_text("key")
    (root / ".env").write_text("TOKEN=abc")
    # A secret OUTSIDE the workspace root, reachable only via traversal.
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    return root


@pytest.fixture
def client(workspace, monkeypatch):
    _patch_api_key(monkeypatch, "testkey")

    def _resolve(user, backend=None):
        # The route keys the workspace on the WHOLE identity (issue #188), so the
        # fake resolver is keyed the same way.
        if user == "alice@corp.com":
            return workspace
        if user == "alice":
            return workspace.parent.parent / "legacy-localpart" / "claude"
        raise ValueError("bad user")

    monkeypatch.setattr(tf.workspace_manager, "resolve", _resolve)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_AUTH = {"Authorization": "Bearer testkey"}
# Default identity header (WORKSPACE_USER_HEADER). The value keys the workspace
# whole — no localpart truncation (issue #188).
_USER = {"X-User-Email": "alice@corp.com"}


def test_config_advertises_readonly_no_terminal(client):
    r = client.get("/api/config", headers=_AUTH)
    assert r.status_code == 200
    assert r.json() == {"features": {"terminal": False}}


def test_tool_specs_openapi_has_no_paths(client):
    # open-webui builds LLM tools from this; empty paths => zero tools => no
    # tool callables to serialize (avoids the "function not serializable" crash).
    r = client.get("/files/openapi.json", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["paths"] == {}
    assert "openapi" in body


def test_cwd_returns_real_workspace_path(client, workspace):
    r = client.get("/files/cwd", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.json()["cwd"] == str(workspace.resolve())


def test_absolute_paths_under_root_work(client, workspace):
    # The explorer echoes the real cwd, so list/read arrive as absolute paths.
    d = str(workspace.resolve())
    r = client.get(f"/files/list?directory={d}/sub", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert [e["name"] for e in r.json()["entries"]] == ["inner.md"]
    rr = client.get(f"/files/read?path={d}/notes.txt", headers={**_AUTH, **_USER})
    assert rr.status_code == 200 and rr.json()["content"] == "hello\nworld\n"


def test_absolute_path_outside_root_blocked(client, workspace):
    outside = str((workspace.parent.parent / "secret.txt"))
    r = client.get(f"/files/read?path={outside}", headers={**_AUTH, **_USER})
    assert r.status_code == 404


def test_list_root_sorts_dirs_first(client):
    r = client.get("/files/list?directory=/", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    entries = r.json()["entries"]
    names = [e["name"] for e in entries]
    # dirs first, then files, each a-z — dot-prefixed entries sort with the rest
    # now that the server no longer hides them.
    assert names == [".secret_dir", "sub", ".env", "blob.bin", "notes.txt"]
    sub = next(e for e in entries if e["name"] == "sub")
    assert sub["type"] == "directory"
    notes = next(e for e in entries if e["name"] == "notes.txt")
    assert notes["type"] == "file" and notes["size"] == 12


def test_list_subdirectory(client):
    r = client.get("/files/list?directory=/sub", headers={**_AUTH, **_USER})
    assert [e["name"] for e in r.json()["entries"]] == ["inner.md"]


def test_read_text_returns_content_and_line_count(client):
    r = client.get("/files/read?path=/notes.txt", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "hello\nworld\n"
    assert body["total_lines"] == 3  # two lines + trailing newline


def test_read_binary_returns_raw_bytes(client):
    r = client.get("/files/read?path=/blob.bin", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.content == b"\xff\xfe\x00\x01binary"
    # Non-JSON content-type so the frontend renders a binary placeholder.
    assert not r.headers["content-type"].startswith("application/json")


def test_view_streams_file(client):
    r = client.get("/files/view?path=/notes.txt", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.content == b"hello\nworld\n"


def test_path_traversal_is_blocked(client):
    for p in ("/../secret.txt", "/../../secret.txt", "/sub/../../secret.txt"):
        r = client.get(f"/files/read?path={p}", headers={**_AUTH, **_USER})
        assert r.status_code in (403, 404), p
        assert "SECRET" not in r.text


def test_above_root_navigation_is_403(client, workspace):
    # Clicking a breadcrumb segment above the workspace root -> "not allowed".
    ancestor = str(workspace.resolve().parent)  # e.g. /tmp/.../alice
    r = client.get(f"/files/list?directory={ancestor}", headers={**_AUTH, **_USER})
    assert r.status_code == 403
    assert "outside your workspace" in r.json()["detail"]


def test_missing_user_header_is_rejected(client):
    r = client.get("/files/list?directory=/", headers=_AUTH)
    assert r.status_code in (400, 403)


def test_invalid_user_is_rejected(client):
    r = client.get(
        "/files/list?directory=/",
        headers={**_AUTH, "X-User-Email": "../evil@x.com"},
    )
    assert r.status_code in (400, 403)


def test_identity_is_not_truncated_at_the_at_sign(workspace, monkeypatch):
    """Two principals sharing a localpart must not share a workspace.

    Before issue #188 the workspace key was ``identity.split("@")[0]``, so
    ``alice@a.com`` could list, read, overwrite and delete ``alice@b.com``'s
    files. The whole identity is the key now.
    """
    _patch_api_key(monkeypatch, "testkey")
    roots = {}

    def _resolve(user, backend=None):
        root = workspace.parent.parent / user / (backend or "")
        root.mkdir(parents=True, exist_ok=True)
        roots[user] = root
        return root

    monkeypatch.setattr(tf.workspace_manager, "resolve", _resolve)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)

    up = c.post(
        "/files/upload",
        headers={**_AUTH, "X-User-Email": "alice@a.com"},
        data={"path": "/"},
        files={"file": ("secret.txt", b"from a", "text/plain")},
    )
    assert up.status_code == 200

    listed = c.get(
        "/files/list?directory=/", headers={**_AUTH, "X-User-Email": "alice@b.com"}
    )
    assert listed.status_code == 200
    assert [e["name"] for e in listed.json()["entries"]] == []

    read = c.get(
        "/files/read?path=/secret.txt", headers={**_AUTH, "X-User-Email": "alice@b.com"}
    )
    assert read.status_code == 404
    assert roots["alice@a.com"] != roots["alice@b.com"]


def test_legacy_localpart_key_is_opt_in(client, monkeypatch, workspace):
    """The old truncating key stays reachable only behind an explicit switch."""
    legacy = workspace.parent.parent / "legacy-localpart" / "claude"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "only-in-legacy.txt").write_text("x")

    default = client.get("/files/list?directory=/", headers={**_AUTH, **_USER})
    assert "only-in-legacy.txt" not in [e["name"] for e in default.json()["entries"]]

    monkeypatch.setenv("WORKSPACE_LEGACY_LOCALPART_KEY", "true")
    switched = client.get("/files/list?directory=/", headers={**_AUTH, **_USER})
    assert switched.status_code == 200
    assert [e["name"] for e in switched.json()["entries"]] == ["only-in-legacy.txt"]


def test_custom_user_header_name(client, monkeypatch):
    monkeypatch.setenv("WORKSPACE_USER_HEADER", "X-Whoami")
    r = client.get(
        "/files/list?directory=/",
        headers={**_AUTH, "X-Whoami": "alice@corp.com"},
    )
    assert r.status_code == 200
    # The old default name is no longer honored.
    r2 = client.get(
        "/files/list?directory=/",
        headers={**_AUTH, "X-User-Email": "alice@corp.com"},
    )
    assert r2.status_code == 400


def test_wrong_api_key_is_unauthorized(client):
    r = client.get(
        "/files/list?directory=/",
        headers={"Authorization": "Bearer wrong", **_USER},
    )
    assert r.status_code == 401


def test_fails_closed_when_api_key_unset(workspace, monkeypatch):
    _patch_api_key(monkeypatch, "")
    monkeypatch.setattr(tf.workspace_manager, "resolve", lambda user, backend=None: workspace)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    # No API key configured -> the browser is disabled, not open to everyone.
    r = c.get("/api/config")
    assert r.status_code == 503


def test_missing_file_is_404(client):
    r = client.get("/files/read?path=/nope.txt", headers={**_AUTH, **_USER})
    assert r.status_code == 404


# --- dotfile hiding -----------------------------------------------------------
#
# Off by default (2026-08). Server-side hiding blocked WRITES to dot-prefixed
# paths too, so clients installing agent resources into the workspace got a 404
# from a flag that was only ever meant to tidy a listing. Hiding is presentation
# and now belongs to the client rendering the tree; the flag stays for
# deployments that want the old behavior.


def test_dotfiles_visible_by_default(client):
    r = client.get("/files/list?directory=/", headers={**_AUTH, **_USER})
    names = [e["name"] for e in r.json()["entries"]]
    assert ".env" in names and ".secret_dir" in names


def test_hidden_path_accessible_by_default(client):
    # Reading and listing inside a dot-prefixed path is allowed...
    rr = client.get("/files/read?path=/.env", headers={**_AUTH, **_USER})
    assert rr.status_code == 200 and rr.json()["content"] == "TOKEN=abc"
    assert (
        client.get("/files/list?directory=/.secret_dir", headers={**_AUTH, **_USER}).status_code
        == 200
    )


def test_dot_prefixed_write_path_is_allowed_by_default(client, workspace):
    """The regression this default exists for: installing into a dot-prefixed
    directory must not 404. mkdir + upload is how a client writes agent
    resources (skills, subagents) into the workspace."""
    r = client.post(
        "/files/mkdir", headers={**_AUTH, **_USER}, json={"path": "/.agent/skills/demo"}
    )
    assert r.status_code == 200
    up = client.post(
        "/files/upload?directory=/.agent/skills/demo",
        headers={**_AUTH, **_USER},
        files={"file": ("SKILL.md", b"# demo", "text/markdown")},
    )
    assert up.status_code == 200
    assert (workspace / ".agent" / "skills" / "demo" / "SKILL.md").read_text() == "# demo"


def test_dotfiles_hidden_when_enabled(client, monkeypatch):
    monkeypatch.setenv("WORKSPACE_HIDE_DOTFILES", "true")
    r = client.get("/files/list?directory=/", headers={**_AUTH, **_USER})
    names = [e["name"] for e in r.json()["entries"]]
    assert names == ["sub", "blob.bin", "notes.txt"]


def test_hidden_path_not_accessible_when_enabled(client, monkeypatch):
    monkeypatch.setenv("WORKSPACE_HIDE_DOTFILES", "true")
    # Even typing the path directly is blocked (list/read/inside-dir).
    assert client.get("/files/read?path=/.env", headers={**_AUTH, **_USER}).status_code == 404
    assert (
        client.get("/files/list?directory=/.secret_dir", headers={**_AUTH, **_USER}).status_code
        == 404
    )
    assert (
        client.get("/files/read?path=/.secret_dir/k.txt", headers={**_AUTH, **_USER}).status_code
        == 404
    )


def test_claude_prefix_hide_is_narrow_and_blocks_direct_access(client, workspace, monkeypatch):
    claude = workspace / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text('{"hooks": []}')
    claude_images = workspace / ".claude_images"
    claude_images.mkdir()
    (claude_images / "frame.png").write_bytes(b"png")
    (workspace / ".claud").write_text("visible")

    monkeypatch.setenv("WORKSPACE_HIDE_CLAUDE_PREFIX", "true")

    r = client.get("/files/list?directory=/", headers={**_AUTH, **_USER})
    names = [e["name"] for e in r.json()["entries"]]
    assert ".claude" not in names
    assert ".claude_images" not in names
    # This switch is deliberately narrower than WORKSPACE_HIDE_DOTFILES.
    assert ".env" in names
    assert ".secret_dir" in names
    assert ".claud" in names

    assert (
        client.get(
            "/files/read?path=/.claude/settings.json",
            headers={**_AUTH, **_USER},
        ).status_code
        == 404
    )
    assert (
        client.get("/files/list?directory=/.claude", headers={**_AUTH, **_USER}).status_code
        == 404
    )
    assert (
        client.get(
            "/files/read?path=/.claude_images/frame.png",
            headers={**_AUTH, **_USER},
        ).status_code
        == 404
    )
    # Other dot-prefixed paths remain accessible.
    assert client.get("/files/read?path=/.env", headers={**_AUTH, **_USER}).status_code == 200

    # Match WORKSPACE_HIDE_DOTFILES semantics: hidden paths are not writable
    # through the file API either.
    write = client.post(
        "/files/mkdir",
        headers={**_AUTH, **_USER},
        json={"path": "/.claude/new-project"},
    )
    assert write.status_code == 404
    assert not (claude / "new-project").exists()


def test_claude_prefix_hide_blocks_nested_claude_component(client, workspace, monkeypatch):
    nested = workspace / "sub" / ".claude"
    nested.mkdir()
    (nested / "project.md").write_text("private project config")
    monkeypatch.setenv("WORKSPACE_HIDE_CLAUDE_PREFIX", "true")

    assert (
        client.get(
            "/files/read?path=/sub/.claude/project.md",
            headers={**_AUTH, **_USER},
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    ("env_name", "link_name"),
    [
        ("WORKSPACE_HIDE_CLAUDE_PREFIX", ".claude_link"),
        ("WORKSPACE_HIDE_DOTFILES", ".hidden_link"),
    ],
)
def test_hidden_lexical_component_cannot_escape_via_internal_symlink(
    client, workspace, monkeypatch, env_name, link_name
):
    visible = workspace / "visible"
    visible.mkdir()
    (visible / "secret.txt").write_text("not for hidden path")
    (workspace / link_name).symlink_to(visible, target_is_directory=True)
    monkeypatch.setenv(env_name, "true")

    # Path.resolve() maps this request to visible/secret.txt.  The requested
    # hidden component must still make the file API return 404.
    r = client.get(
        f"/files/read?path=/{link_name}/secret.txt",
        headers={**_AUTH, **_USER},
    )
    assert r.status_code == 404


def test_resolved_hidden_target_is_still_blocked_through_visible_symlink(
    client, workspace, monkeypatch
):
    hidden = workspace / ".claude_images"
    hidden.mkdir()
    (hidden / "frame.png").write_bytes(b"png")
    (workspace / "visible-link").symlink_to(hidden, target_is_directory=True)
    monkeypatch.setenv("WORKSPACE_HIDE_CLAUDE_PREFIX", "true")

    r = client.get(
        "/files/read?path=/visible-link/frame.png",
        headers={**_AUTH, **_USER},
    )
    assert r.status_code == 404


# --- write operations ---------------------------------------------------------


def test_upload_creates_file(client, workspace):
    r = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("new.txt", b"content here", "text/plain")},
    )
    assert r.status_code == 200
    assert r.json() == {"path": str(workspace / "new.txt"), "size": 12}
    assert (workspace / "new.txt").read_bytes() == b"content here"


def test_upload_empty_file_is_new_file(client, workspace):
    # FileNav's "New File" uploads an empty file.
    r = client.post(
        "/files/upload?directory=/sub",
        headers={**_AUTH, **_USER},
        files={"file": ("empty.md", b"", "text/plain")},
    )
    assert r.status_code == 200
    assert (workspace / "sub" / "empty.md").exists()


def test_upload_filename_traversal_reduced_to_basename(client, workspace):
    r = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("../../escape.txt", b"x", "text/plain")},
    )
    assert r.status_code == 200
    # Written inside the workspace as a plain basename, not outside it.
    assert (workspace / "escape.txt").exists()
    assert not (workspace.parent.parent / "escape.txt").exists()


def test_mkdir(client, workspace):
    r = client.post("/files/mkdir", headers={**_AUTH, **_USER}, json={"path": "/newdir"})
    assert r.status_code == 200
    assert (workspace / "newdir").is_dir()


def test_mkdir_traversal_blocked(client, workspace):
    r = client.post(
        "/files/mkdir", headers={**_AUTH, **_USER}, json={"path": "/../evil"}
    )
    assert r.status_code in (400, 403)
    assert not (workspace.parent / "evil").exists()


def test_delete_file(client, workspace):
    r = client.delete("/files/delete?path=/notes.txt", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.json()["type"] == "file"
    assert not (workspace / "notes.txt").exists()


def test_delete_directory_recursive(client, workspace):
    r = client.delete("/files/delete?path=/sub", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.json()["type"] == "directory"
    assert not (workspace / "sub").exists()


def test_delete_traversal_blocked(client, workspace):
    r = client.delete("/files/delete?path=/../../secret.txt", headers={**_AUTH, **_USER})
    assert r.status_code in (400, 403, 404)
    assert (workspace.parent.parent / "secret.txt").exists()  # untouched


def test_move_renames(client, workspace):
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/renamed.txt"},
    )
    assert r.status_code == 200
    assert not (workspace / "notes.txt").exists()
    assert (workspace / "renamed.txt").read_text() == "hello\nworld\n"


def test_move_traversal_blocked(client, workspace):
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/../stolen.txt"},
    )
    assert r.status_code in (400, 403)
    assert (workspace / "notes.txt").exists()  # unchanged


def test_move_overwrites_by_default_preserving_filenav_contract(client, workspace):
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/blob.bin"},
    )
    assert r.status_code == 200
    assert (workspace / "blob.bin").read_text() == "hello\nworld\n"


def test_move_no_clobber_refuses_existing_destination(client, workspace):
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/blob.bin", "no_clobber": True},
    )
    assert r.status_code == 409
    assert (workspace / "notes.txt").exists()  # source untouched
    assert (workspace / "blob.bin").read_bytes() == b"\xff\xfe\x00\x01binary"


def test_move_no_clobber_still_moves_to_free_name(client, workspace):
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/renamed.txt", "no_clobber": True},
    )
    assert r.status_code == 200
    assert (workspace / "renamed.txt").read_text() == "hello\nworld\n"


def test_copy_duplicates_file(client, workspace):
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/notes copy.txt"},
    )
    assert r.status_code == 200
    assert r.json()["type"] == "file"
    assert (workspace / "notes.txt").read_text() == "hello\nworld\n"  # source untouched
    assert (workspace / "notes copy.txt").read_text() == "hello\nworld\n"


def test_copy_duplicates_directory_recursively(client, workspace):
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/sub", "destination": "/sub copy"},
    )
    assert r.status_code == 200
    assert r.json()["type"] == "directory"
    assert (workspace / "sub" / "inner.md").exists()  # source untouched
    assert (workspace / "sub copy" / "inner.md").read_text() == "# inner"


def test_copy_refuses_existing_destination(client, workspace):
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/blob.bin"},
    )
    assert r.status_code == 409
    assert (workspace / "blob.bin").read_bytes() == b"\xff\xfe\x00\x01binary"  # untouched


def test_copy_refuses_directory_into_itself(client, workspace):
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/sub", "destination": "/sub/nested"},
    )
    assert r.status_code == 400
    assert not (workspace / "sub" / "nested").exists()


def test_copy_missing_source_is_404(client):
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/nope.txt", "destination": "/copy.txt"},
    )
    assert r.status_code == 404


def test_copy_traversal_blocked(client, workspace):
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/../secret.txt", "destination": "/stolen.txt"},
    )
    assert r.status_code in (400, 403)
    assert not (workspace / "stolen.txt").exists()


def test_upload_overwrites_by_default(client, workspace):
    r = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("notes.txt", b"new", "text/plain")},
    )
    assert r.status_code == 200
    assert (workspace / "notes.txt").read_text() == "new"


def test_upload_no_clobber_refuses_existing(client, workspace):
    r = client.post(
        "/files/upload?directory=/&no_clobber=true",
        headers={**_AUTH, **_USER},
        files={"file": ("notes.txt", b"new", "text/plain")},
    )
    assert r.status_code == 409
    assert (workspace / "notes.txt").read_text() == "hello\nworld\n"  # untouched


def test_upload_no_clobber_writes_fresh_name(client, workspace):
    r = client.post(
        "/files/upload?directory=/&no_clobber=true",
        headers={**_AUTH, **_USER},
        files={"file": ("fresh.txt", b"new", "text/plain")},
    )
    assert r.status_code == 200
    assert (workspace / "fresh.txt").read_text() == "new"


def test_upload_no_clobber_refuses_destination_appearing_after_validation(
    client, workspace, monkeypatch
):
    _race_in_threadpool(monkeypatch, lambda: (workspace / "late.txt").write_text("winner"))
    r = client.post(
        "/files/upload?directory=/&no_clobber=true",
        headers={**_AUTH, **_USER},
        files={"file": ("late.txt", b"new", "text/plain")},
    )
    assert r.status_code == 409
    assert (workspace / "late.txt").read_text() == "winner"


def _race_in_threadpool(monkeypatch, create: "callable"):
    """다음 run_in_threadpool 호출 직전에 destination을 만들어, '검증 후
    실행 전' 레이스를 실제로 재현한다."""
    real = tf.run_in_threadpool

    async def raced(fn, *args, **kwargs):
        create()
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(tf, "run_in_threadpool", raced)


def test_copy_file_refuses_destination_appearing_after_validation(
    client, workspace, monkeypatch
):
    _race_in_threadpool(monkeypatch, lambda: (workspace / "raced.txt").write_text("winner"))
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/raced.txt"},
    )
    assert r.status_code == 409
    # 레이스에서 이긴 쪽의 파일이 절대 덮어써지지 않는다 — O_EXCL 계약
    assert (workspace / "raced.txt").read_text() == "winner"


def test_copy_directory_refuses_destination_appearing_after_validation(
    client, workspace, monkeypatch
):
    def create():
        (workspace / "raced-dir").mkdir()
        (workspace / "raced-dir" / "keep.txt").write_text("winner")

    _race_in_threadpool(monkeypatch, create)
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/sub", "destination": "/raced-dir"},
    )
    assert r.status_code == 409  # FileExistsError가 500으로 새지 않는다
    assert (workspace / "raced-dir" / "keep.txt").read_text() == "winner"


def test_move_no_clobber_refuses_destination_appearing_after_validation(
    client, workspace, monkeypatch
):
    _race_in_threadpool(monkeypatch, lambda: (workspace / "raced.txt").write_text("winner"))
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/raced.txt", "no_clobber": True},
    )
    assert r.status_code == 409
    assert (workspace / "notes.txt").exists()  # source 보존
    assert (workspace / "raced.txt").read_text() == "winner"


@pytest.mark.parametrize("no_clobber", [False, True])
def test_move_directory_into_itself_is_a_precise_400(client, workspace, no_clobber):
    """자기 하위로의 이동은 renameat2의 EINVAL(→501)이나 shutil.Error(→500)로
    새지 않고 명확한 400이어야 한다."""
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": "/sub", "destination": "/sub/nested", "no_clobber": no_clobber},
    )
    assert r.status_code == 400
    assert (workspace / "sub" / "inner.md").exists()  # untouched


def test_move_no_clobber_unavailable_fails_closed(client, workspace, monkeypatch):
    """renameat2를 못 쓰는 환경에서는 교체 가능한 move로 조용히 fallback하지 않고
    501로 거부한다 — no-clobber는 보장이거나 거부이지, best effort가 아니다."""

    def unavailable(src, dst):
        raise NotImplementedError("no renameat2")

    monkeypatch.setattr(tf, "_rename_noreplace", unavailable)
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": "/notes.txt", "destination": "/renamed.txt", "no_clobber": True},
    )
    assert r.status_code == 501
    assert (workspace / "notes.txt").exists()  # source untouched
    assert not (workspace / "renamed.txt").exists()


def test_copy_refuses_fifo_source(client, workspace):
    os.mkfifo(workspace / "pipe")
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/pipe", "destination": "/pipe copy"},
    )
    assert r.status_code == 400
    assert not (workspace / "pipe copy").exists()


def test_copy_fifo_refused_even_past_the_fast_path(client, workspace, monkeypatch):
    """fast-path(is_file) 검사를 우회해도 fstat 검증이 막는다 — 타입 스왑 레이스
    대비가 실제로 fd 기준인지 확인한다."""
    monkeypatch.setattr(tf.Path, "is_file", lambda self: True)
    os.mkfifo(workspace / "pipe2")
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/pipe2", "destination": "/pipe2 copy"},
    )
    assert r.status_code == 400
    assert not (workspace / "pipe2 copy").exists()


def test_copy_directory_containing_fifo_is_client_error(client, workspace):
    (workspace / "mix").mkdir()
    (workspace / "mix" / "ok.txt").write_text("x")
    os.mkfifo(workspace / "mix" / "pipe")
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/mix", "destination": "/mix copy"},
    )
    assert r.status_code == 400  # SpecialFileError가 500으로 새지 않는다


def test_copy_does_not_follow_symlinks_inside_tree(client, workspace, tmp_path):
    (workspace / "linked").mkdir()
    link = workspace / "linked" / "out"
    link.symlink_to(workspace.parent.parent / "secret.txt")
    r = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/linked", "destination": "/linked copy"},
    )
    assert r.status_code == 200
    copied = workspace / "linked copy" / "out"
    assert copied.is_symlink()  # preserved as a link, content not duplicated


def test_archive_zips_selection(client):
    r = client.post(
        "/files/archive",
        headers={**_AUTH, **_USER},
        json={"paths": ["/notes.txt", "/sub"]},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    import io as _io
    import zipfile as _zip

    names = _zip.ZipFile(_io.BytesIO(r.content)).namelist()
    assert "notes.txt" in names and "sub/inner.md" in names


def test_archive_excludes_hidden_entries_when_enabled(client, monkeypatch):
    """The zip mirrors the visible listing — when the server hides dotfiles, a
    directory download must not sweep them back in."""
    monkeypatch.setenv("WORKSPACE_HIDE_DOTFILES", "true")
    r = client.post(
        "/files/archive",
        headers={**_AUTH, **_USER},
        json={"paths": ["/"]},
    )
    assert r.status_code == 200
    import io as _io
    import zipfile as _zip

    names = _zip.ZipFile(_io.BytesIO(r.content)).namelist()
    assert "notes.txt" in names and "sub/inner.md" in names
    assert ".env" not in names
    assert not any(n.startswith(".secret_dir/") for n in names)


def test_archive_includes_hidden_by_default(client):
    r = client.post(
        "/files/archive",
        headers={**_AUTH, **_USER},
        json={"paths": ["/"]},
    )
    assert r.status_code == 200
    import io as _io
    import zipfile as _zip

    names = _zip.ZipFile(_io.BytesIO(r.content)).namelist()
    assert ".env" in names and ".secret_dir/k.txt" in names
    assert "notes.txt" in names


def test_archive_excludes_claude_prefixed_paths_when_enabled(client, workspace, monkeypatch):
    claude = workspace / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{}")
    claude_images = workspace / ".claude_images"
    claude_images.mkdir()
    (claude_images / "frame.png").write_bytes(b"png")
    monkeypatch.setenv("WORKSPACE_HIDE_CLAUDE_PREFIX", "true")

    r = client.post(
        "/files/archive",
        headers={**_AUTH, **_USER},
        json={"paths": ["/"]},
    )
    assert r.status_code == 200
    import io as _io
    import zipfile as _zip

    names = _zip.ZipFile(_io.BytesIO(r.content)).namelist()
    assert ".claude/settings.json" not in names
    assert ".claude_images/frame.png" not in names
    assert ".env" in names
    assert ".secret_dir/k.txt" in names


def test_search_finds_nested_files(client):
    r = client.get("/files/search?query=inner", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is False
    names = [e["name"] for e in body["results"]]
    assert "inner.md" in names
    hit = next(e for e in body["results"] if e["name"] == "inner.md")
    assert hit["type"] == "file"
    assert hit["path"].endswith("/sub/inner.md")


def test_search_is_case_insensitive_and_matches_dirs(client):
    r = client.get("/files/search?query=SUB", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    results = r.json()["results"]
    assert any(e["name"] == "sub" and e["type"] == "directory" for e in results)


def test_search_excludes_hidden_entries_when_enabled(client, monkeypatch):
    monkeypatch.setenv("WORKSPACE_HIDE_DOTFILES", "true")
    r = client.get("/files/search?query=secret", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_search_includes_hidden_by_default(client):
    r = client.get("/files/search?query=secret", headers={**_AUTH, **_USER})
    names = [e["name"] for e in r.json()["results"]]
    assert ".secret_dir" in names


def test_search_prunes_claude_prefixed_paths_when_enabled(client, workspace, monkeypatch):
    claude = workspace / ".claude"
    claude.mkdir()
    (claude / "claude-config.json").write_text("{}")
    claude_images = workspace / ".claude_images"
    claude_images.mkdir()
    (claude_images / "claude-frame.png").write_bytes(b"png")
    (workspace / ".claud-notes").write_text("visible")
    monkeypatch.setenv("WORKSPACE_HIDE_CLAUDE_PREFIX", "true")

    r = client.get("/files/search?query=claude", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["results"]]
    assert ".claude" not in names
    assert ".claude_images" not in names
    assert "claude-config.json" not in names
    assert "claude-frame.png" not in names

    other = client.get("/files/search?query=secret", headers={**_AUTH, **_USER})
    assert ".secret_dir" in [e["name"] for e in other.json()["results"]]


def test_search_empty_query_returns_nothing(client):
    r = client.get("/files/search?query=", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.json() == {"results": [], "truncated": False}


def test_search_respects_limit_and_flags_truncation(client, workspace):
    for i in range(5):
        (workspace / f"match_{i}.txt").write_text("x")
    r = client.get("/files/search?query=match&limit=3", headers={**_AUTH, **_USER})
    body = r.json()
    assert len(body["results"]) == 3
    assert body["truncated"] is True


def test_writes_fail_closed_without_api_key(workspace, monkeypatch):
    _patch_api_key(monkeypatch, "")
    monkeypatch.setattr(tf.workspace_manager, "resolve", lambda user, backend=None: workspace)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    r = c.post("/files/mkdir", json={"path": "/x"})
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# /files/serve — inline serving for the HTML iframe preview
# ---------------------------------------------------------------------------


def test_serve_html_inline_with_content_type(client, workspace):
    (workspace / "page.html").write_text("<h1>hi</h1>")
    d = str(workspace.resolve())
    r = client.get(f"/files/serve{d}/page.html", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.text == "<h1>hi</h1>"
    assert r.headers["content-type"].startswith("text/html")
    assert "attachment" not in r.headers.get("content-disposition", "")


def test_serve_resolves_sibling_asset(client, workspace):
    # Relative references inside a served HTML document resolve through the
    # same route — e.g. ./app.css next to the page.
    (workspace / "site").mkdir()
    (workspace / "site" / "index.html").write_text('<link href="./app.css">')
    (workspace / "site" / "app.css").write_text("body{}")
    d = str(workspace.resolve())
    r = client.get(f"/files/serve{d}/site/app.css", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert r.text == "body{}"


def test_serve_outside_root_never_leaks(client, workspace):
    outside = str(workspace.parent.parent / "secret.txt")
    r = client.get(f"/files/serve{outside}", headers={**_AUTH, **_USER})
    assert r.status_code in (403, 404)
    assert "SECRET" not in r.text


def test_serve_hidden_file_is_404_when_enabled(client, workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_HIDE_DOTFILES", "true")
    d = str(workspace.resolve())
    r = client.get(f"/files/serve{d}/.env", headers={**_AUTH, **_USER})
    assert r.status_code == 404


def test_serve_claude_prefixed_file_is_404_when_enabled(client, workspace, monkeypatch):
    claude = workspace / ".claude"
    claude.mkdir()
    (claude / "preview.html").write_text("<h1>hidden</h1>")
    monkeypatch.setenv("WORKSPACE_HIDE_CLAUDE_PREFIX", "true")
    d = str(workspace.resolve())
    r = client.get(f"/files/serve{d}/.claude/preview.html", headers={**_AUTH, **_USER})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Upload ceiling: gateway runtime-config is the single source of truth.
# ---------------------------------------------------------------------------


@pytest.fixture
def upload_limit_override():
    from src.runtime_config import runtime_config

    runtime_config.reset("workspace_upload_max_bytes")
    try:
        yield runtime_config
    finally:
        runtime_config.reset("workspace_upload_max_bytes")


def test_limits_reports_gateway_runtime_upload_ceiling(client, upload_limit_override):
    upload_limit_override.set("workspace_upload_max_bytes", 7 * 1024 * 1024)

    res = client.get("/files/limits", headers={**_AUTH, **_USER})

    assert res.status_code == 200
    assert res.json()["max_upload_bytes"] == 7 * 1024 * 1024


def test_upload_accepts_exactly_the_published_ceiling(
    client, workspace, upload_limit_override
):
    upload_limit_override.set("workspace_upload_max_bytes", 1024)
    ceiling = client.get("/files/limits", headers={**_AUTH, **_USER}).json()[
        "max_upload_bytes"
    ]
    assert ceiling == 1024

    res = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("fits.bin", b"x" * ceiling, "application/octet-stream")},
    )

    assert res.status_code == 200
    assert (workspace / "fits.bin").stat().st_size == ceiling


def test_upload_over_ceiling_is_413_and_writes_nothing(
    client, workspace, upload_limit_override
):
    upload_limit_override.set("workspace_upload_max_bytes", 1024)

    res = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("too-big.bin", b"x" * 1025, "application/octet-stream")},
    )

    assert res.status_code == 413
    assert "1024" in res.json()["detail"]
    assert not (workspace / "too-big.bin").exists()


def test_limits_requires_auth(client):
    assert client.get("/files/limits", headers=_USER).status_code in (401, 403)


# ---------------------------------------------------------------------------
# The same runtime ceiling through the REAL request-size stack.
#
# The generic API request cap remains independent. /files/upload gets exactly
# the gateway-owned file ceiling plus multipart envelope room at the raw-body
# boundary, then the route enforces the advertised file-byte ceiling itself.
# ---------------------------------------------------------------------------

_STACK_UPLOAD_SIZE = 64 * 1024


@pytest.fixture
def guarded_client(workspace, monkeypatch):
    """The router behind both real body-size guards."""
    from src import main as gateway_main
    from src.concurrency_middleware import ConcurrencyLimitMiddleware
    from src.runtime_config import runtime_config

    _patch_api_key(monkeypatch, "testkey")

    def _resolve(user, backend=None):
        if user == "alice@corp.com":
            return workspace
        raise ValueError("bad user")

    monkeypatch.setattr(tf.workspace_manager, "resolve", _resolve)
    runtime_config.reset("workspace_upload_max_bytes")
    runtime_config.set("workspace_upload_max_bytes", _STACK_UPLOAD_SIZE)

    app = FastAPI()
    app.include_router(router)
    # Same order as src.main: last-added middleware executes first, so the
    # Content-Length fast guard sits outside the actual-byte ASGI guard.
    app.add_middleware(ConcurrencyLimitMiddleware)
    app.add_middleware(gateway_main.RequestSizeLimitMiddleware)
    try:
        yield TestClient(app)
    finally:
        runtime_config.reset("workspace_upload_max_bytes")


def test_published_ceiling_survives_the_real_multipart_boundary(guarded_client, workspace):
    ceiling = guarded_client.get("/files/limits", headers={**_AUTH, **_USER}).json()[
        "max_upload_bytes"
    ]
    assert ceiling == _STACK_UPLOAD_SIZE

    res = guarded_client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("exactly.bin", b"x" * ceiling, "application/octet-stream")},
    )

    assert res.status_code == 200, res.text
    assert (workspace / "exactly.bin").stat().st_size == ceiling


def test_one_byte_over_the_published_ceiling_is_refused_by_the_stack(
    guarded_client, workspace
):
    ceiling = guarded_client.get("/files/limits", headers={**_AUTH, **_USER}).json()[
        "max_upload_bytes"
    ]

    res = guarded_client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("over.bin", b"x" * (ceiling + 1), "application/octet-stream")},
    )

    assert res.status_code == 413, res.text
    assert not (workspace / "over.bin").exists()


def test_the_reserve_covers_a_real_multipart_envelope(guarded_client, workspace):
    """A ceiling-sized file with a long filename still fits its raw body cap."""
    ceiling = guarded_client.get("/files/limits", headers={**_AUTH, **_USER}).json()[
        "max_upload_bytes"
    ]
    name = "a" * 200 + ".bin"

    res = guarded_client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": (name, b"x" * ceiling, "application/octet-stream")},
    )

    assert res.status_code == 200, res.text
    assert (workspace / name).stat().st_size == ceiling


def test_upload_limit_can_exceed_generic_request_cap(workspace, monkeypatch):
    """MAX_REQUEST_SIZE must not silently become a second file-upload setting."""
    from src import concurrency_middleware as cm
    from src import main as gateway_main
    from src.concurrency_middleware import ConcurrencyLimitMiddleware
    from src.runtime_config import runtime_config

    _patch_api_key(monkeypatch, "testkey")

    def _resolve(user, backend=None):
        if user == "alice@corp.com":
            return workspace
        raise ValueError("bad user")

    monkeypatch.setattr(tf.workspace_manager, "resolve", _resolve)
    monkeypatch.setattr(cm, "MAX_REQUEST_SIZE", 4 * 1024)
    runtime_config.reset("workspace_upload_max_bytes")
    runtime_config.set("workspace_upload_max_bytes", 16 * 1024)

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(ConcurrencyLimitMiddleware)
    app.add_middleware(gateway_main.RequestSizeLimitMiddleware)
    c = TestClient(app)
    try:
        res = c.post(
            "/files/upload?directory=/",
            headers={**_AUTH, **_USER},
            files={"file": ("larger-than-generic.bin", b"x" * (8 * 1024), "application/octet-stream")},
        )
        assert res.status_code == 200, res.text
        assert (workspace / "larger-than-generic.bin").stat().st_size == 8 * 1024
    finally:
        runtime_config.reset("workspace_upload_max_bytes")


def test_zero_ceiling_refuses_nonempty_file(
    client, workspace, upload_limit_override
):
    upload_limit_override.set("workspace_upload_max_bytes", 0)
    assert client.get("/files/limits", headers={**_AUTH, **_USER}).json()[
        "max_upload_bytes"
    ] == 0

    res = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("tiny.bin", b"x", "application/octet-stream")},
    )

    assert res.status_code == 413
    assert not (workspace / "tiny.bin").exists()


# ---------------------------------------------------------------------------
# Content identity.
#
# A timestamp is a hint about when a write happened, not a name for what the
# bytes are. `st_mtime_ns` does not fix that: filesystem granularity can
# coalesce two writes, and `os.utime` can restore an old value outright. A
# caller pinning a revision needs equality to imply the bytes are the same.
# ---------------------------------------------------------------------------


def _digest(client, path):
    res = client.get(f"/files/digest?path={path}", headers={**_AUTH, **_USER})
    assert res.status_code == 200, res.text
    return res.json()


def test_identical_metadata_with_different_bytes_still_differs(client, workspace):
    """The adversarial case: same size, deliberately identical mtime_ns.

    This is what makes the token authoritative rather than a timing artifact —
    it does not depend on the host filesystem happening to advance a clock.
    """
    target = workspace / "pinned.bin"
    target.write_bytes(b"AAA")
    before = _digest(client, "/pinned.bin")
    stamp = target.stat().st_mtime_ns

    target.write_bytes(b"BBB")  # same length, different content
    os.utime(target, ns=(stamp, stamp))  # metadata restored to the old value

    assert target.stat().st_mtime_ns == stamp, "test setup: mtime was not restored"
    assert target.stat().st_size == 3

    after = _digest(client, "/pinned.bin")
    assert after["sha256"] != before["sha256"], (
        "metadata matches but the bytes changed — equality of the published"
        " token must not imply the content is the same revision"
    )


def test_the_same_bytes_give_the_same_digest(client, workspace):
    """The other direction — a touched-but-unchanged file must stay pinned."""
    target = workspace / "same.bin"
    target.write_bytes(b"hello")
    first = _digest(client, "/same.bin")

    os.utime(target, ns=(1, 1))
    target.write_bytes(b"hello")  # rewritten, identical content

    assert _digest(client, "/same.bin")["sha256"] == first["sha256"]


def test_an_ordinary_overwrite_changes_the_digest(client, workspace):
    target = workspace / "note.txt"
    target.write_text("one")
    before = _digest(client, "/note.txt")
    target.write_text("two different")

    after = _digest(client, "/note.txt")
    assert after["sha256"] != before["sha256"]
    assert after["size"] == len("two different")


def test_digest_matches_a_known_value(client, workspace):
    """Pin the algorithm — a caller stores this string across restarts."""
    (workspace / "known.bin").write_bytes(b"abc")

    assert (
        _digest(client, "/known.bin")["sha256"]
        == hashlib.sha256(b"abc").hexdigest()
    )


def test_digest_streams_a_file_larger_than_one_chunk(client, workspace):
    payload = os.urandom(3 * 1024 * 1024 + 7)
    (workspace / "big.bin").write_bytes(payload)

    got = _digest(client, "/big.bin")

    assert got["sha256"] == hashlib.sha256(payload).hexdigest()
    assert got["size"] == len(payload)


def test_digest_refuses_traversal_and_missing_files(client):
    assert (
        client.get("/files/digest?path=/../secret.txt", headers={**_AUTH, **_USER}).status_code
        == 403
    )
    assert (
        client.get("/files/digest?path=/nope.bin", headers={**_AUTH, **_USER}).status_code == 404
    )


def test_digest_requires_auth(client):
    assert client.get("/files/digest?path=/a.txt").status_code in (401, 403)


def test_digest_refuses_a_directory(client, workspace):
    (workspace / "adir").mkdir()

    assert (
        client.get("/files/digest?path=/adir", headers={**_AUTH, **_USER}).status_code == 404
    )
