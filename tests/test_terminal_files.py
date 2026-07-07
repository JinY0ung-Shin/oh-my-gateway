"""Tests for the Open Terminal-compatible read-only workspace file server."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes.terminal_files import router
from src.routes import terminal_files as tf


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "alice" / "claude"
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("hello\nworld\n")
    (root / "sub").mkdir()
    (root / "sub" / "inner.md").write_text("# inner")
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    # A secret OUTSIDE the workspace root, reachable only via traversal.
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    return root


@pytest.fixture
def client(workspace, monkeypatch):
    monkeypatch.setattr(tf.auth_manager, "get_api_key", lambda: "testkey")

    def _resolve(user, backend=None):
        if user == "alice":
            return workspace
        raise ValueError("bad user")

    monkeypatch.setattr(tf.workspace_manager, "resolve", _resolve)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_AUTH = {"Authorization": "Bearer testkey"}
# The proxy forwards the standard user-info headers; we key off the email localpart.
_USER = {"X-OpenWebUI-User-Email": "alice@corp.com"}


def test_config_advertises_readonly_no_terminal(client):
    r = client.get("/api/config", headers=_AUTH)
    assert r.status_code == 200
    assert r.json() == {"features": {"terminal": False}}


def test_cwd_is_virtual_root(client):
    r = client.get("/files/cwd", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.json()["cwd"] == "/"


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
        assert r.status_code == 404, p
        assert "SECRET" not in r.text


def test_missing_user_header_is_rejected(client):
    r = client.get("/files/list?directory=/", headers=_AUTH)
    assert r.status_code == 400


def test_invalid_user_is_rejected(client):
    r = client.get(
        "/files/list?directory=/",
        headers={**_AUTH, "X-OpenWebUI-User-Email": "../evil@x.com"},
    )
    assert r.status_code == 400


def test_wrong_api_key_is_unauthorized(client):
    r = client.get(
        "/files/list?directory=/",
        headers={"Authorization": "Bearer wrong", **_USER},
    )
    assert r.status_code == 401


def test_fails_closed_when_api_key_unset(workspace, monkeypatch):
    monkeypatch.setattr(tf.auth_manager, "get_api_key", lambda: "")
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


# --- write operations ---------------------------------------------------------


def test_upload_creates_file(client, workspace):
    r = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("new.txt", b"content here", "text/plain")},
    )
    assert r.status_code == 200
    assert r.json() == {"path": "/new.txt", "size": 12}
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
    assert r.status_code == 400
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
    assert r.status_code in (400, 404)
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
    assert r.status_code == 400
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
    monkeypatch.setattr(tf.auth_manager, "get_api_key", lambda: "")
    monkeypatch.setattr(tf.workspace_manager, "resolve", lambda user, backend=None: workspace)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    r = c.post("/files/mkdir", json={"path": "/x"})
    assert r.status_code == 503
