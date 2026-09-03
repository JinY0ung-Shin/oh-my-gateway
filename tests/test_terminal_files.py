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
