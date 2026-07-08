"""Tests for the Open Terminal-compatible read-only workspace file server."""

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
        if user == "alice":
            return workspace
        raise ValueError("bad user")

    monkeypatch.setattr(tf.workspace_manager, "resolve", _resolve)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_AUTH = {"Authorization": "Bearer testkey"}
# Default identity header (WORKSPACE_USER_HEADER); we key off the localpart.
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
    assert names == ["sub", "blob.bin", "notes.txt"]  # dir first, then files a-z
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


def test_dotfiles_hidden_by_default(client):
    r = client.get("/files/list?directory=/", headers={**_AUTH, **_USER})
    names = [e["name"] for e in r.json()["entries"]]
    assert ".env" not in names and ".secret_dir" not in names
    assert names == ["sub", "blob.bin", "notes.txt"]


def test_hidden_path_not_accessible_by_default(client):
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


def test_dotfiles_shown_when_disabled(client, monkeypatch):
    monkeypatch.setenv("WORKSPACE_HIDE_DOTFILES", "false")
    r = client.get("/files/list?directory=/", headers={**_AUTH, **_USER})
    names = [e["name"] for e in r.json()["entries"]]
    assert ".env" in names and ".secret_dir" in names
    # ...and now readable.
    rr = client.get("/files/read?path=/.env", headers={**_AUTH, **_USER})
    assert rr.status_code == 200 and rr.json()["content"] == "TOKEN=abc"


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


def test_writes_fail_closed_without_api_key(workspace, monkeypatch):
    _patch_api_key(monkeypatch, "")
    monkeypatch.setattr(tf.workspace_manager, "resolve", lambda user, backend=None: workspace)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    r = c.post("/files/mkdir", json={"path": "/x"})
    assert r.status_code == 503
