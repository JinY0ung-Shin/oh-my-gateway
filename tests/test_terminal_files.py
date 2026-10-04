"""Tests for the Open Terminal-compatible read-only workspace file server."""

import errno
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


def test_claude_prefix_hide_keeps_canonical_agent_resources_editable(
    client, workspace, monkeypatch
):
    """The switch hides the managed mirror, not the resources the user edits.

    Since #206 a workspace keeps its skills/subagents at the canonical
    ``skills/`` and ``agents/`` roots and the gateway maintains a managed
    ``.claude/{skills,agents}`` mirror (hard-linked, marked with
    ``.oh-my-gateway-managed``) purely for Claude Code's own discovery. That
    split is what makes this switch the right one for the file manager: the
    duplicate mirror leaves the surface while the user's own copy stays
    listable, readable and writable.

    ``WORKSPACE_HIDE_DOTFILES`` reaches the same mirror, but only as a side
    effect of hiding every dotfile. Pinned here so a later narrowing of the
    prefix (to exactly ``.claude``) or a widening onto the canonical roots
    fails loudly instead of quietly changing which copy the operator hid.
    """
    (workspace / "skills" / "review").mkdir(parents=True)
    (workspace / "skills" / "review" / "SKILL.md").write_text("---\nname: review\n---\n")
    (workspace / "agents").mkdir()
    mirror = workspace / ".claude" / "skills" / "review"
    mirror.mkdir(parents=True)
    (mirror / "SKILL.md").write_text("---\nname: review\n---\n")
    (workspace / ".claude" / "skills" / ".oh-my-gateway-managed").write_bytes(b"")
    monkeypatch.setenv("WORKSPACE_HIDE_CLAUDE_PREFIX", "true")

    names = [
        e["name"]
        for e in client.get("/files/list?directory=/", headers={**_AUTH, **_USER}).json()[
            "entries"
        ]
    ]
    assert "skills" in names
    assert "agents" in names
    assert ".claude" not in names

    # The canonical copy stays fully usable through the file API...
    assert (
        client.get(
            "/files/read?path=/skills/review/SKILL.md", headers={**_AUTH, **_USER}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/files/mkdir", headers={**_AUTH, **_USER}, json={"path": "/skills/triage"}
        ).status_code
        == 200
    )
    assert (workspace / "skills" / "triage").is_dir()

    # ...while the mirror of that same content is gone from every surface.
    assert (
        client.get(
            "/files/read?path=/.claude/skills/review/SKILL.md",
            headers={**_AUTH, **_USER},
        ).status_code
        == 404
    )
    search = client.get("/files/search?query=SKILL", headers={**_AUTH, **_USER}).json()
    paths = [e["path"] for e in search["results"]]
    assert any("/skills/review/SKILL.md" in q for q in paths)
    assert not any(".claude" in q for q in paths)


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



def _legacy_zip_members(root: Path, rel_paths) -> dict:
    """The pre-streaming ``_build_zip`` walk: arcname -> bytes."""
    root = root.resolve()
    out = {}
    for rel in rel_paths:
        t = (root / rel.lstrip("/")).resolve()
        if t.is_dir():
            for sub in t.rglob("*"):
                if sub.is_file() and not sub.is_symlink():
                    out[str(sub.relative_to(root))] = sub.read_bytes()
        elif t.is_file() and not t.is_symlink():
            out[str(t.relative_to(root))] = t.read_bytes()
    return out


def test_archive_stream_matches_legacy_zip_for_nested_folders(client, workspace):
    import io as _io
    import zipfile as _zip

    deep = workspace / "sub" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "deep.bin").write_bytes(os.urandom(300_000))
    (workspace / "sub" / "a" / "mid.txt").write_text("mid\n" * 1000)
    (workspace / "sub" / "empty").mkdir()

    r = client.post(
        "/files/archive",
        headers={**_AUTH, **_USER},
        json={"paths": ["/notes.txt", "/sub", "/blob.bin"]},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="archive.zip"'
    zf = _zip.ZipFile(_io.BytesIO(r.content))
    assert zf.testzip() is None
    got = {n: zf.read(n) for n in zf.namelist()}
    expected = _legacy_zip_members(workspace, ["/notes.txt", "/sub", "/blob.bin"])
    assert sorted(got) == sorted(expected)
    assert got == expected
    assert "sub/a/b/deep.bin" in got


def test_archive_route_returns_streaming_response_in_multiple_chunks(
    workspace, monkeypatch
):
    import asyncio as _asyncio
    import io as _io
    import zipfile as _zip

    from fastapi.responses import StreamingResponse as _SR

    big = workspace / "big"
    big.mkdir()
    for i in range(3):
        (big / f"part{i}.bin").write_bytes(os.urandom(1024 * 1024))
    _patch_api_key(monkeypatch, "testkey")
    monkeypatch.setattr(
        tf.workspace_manager, "resolve", lambda u, backend=None: workspace
    )

    class _Req:
        headers = {**_AUTH, **_USER}

    async def _run():
        monkeypatch.setattr(tf, "verify_api_key", _noop_verify)
        resp = await tf.archive_entries(
            _Req(), tf._ArchiveBody(paths=["/big"]), credentials=None
        )
        assert isinstance(resp, _SR)
        assert resp.media_type == "application/zip"
        chunks = [c async for c in resp.body_iterator]
        return chunks

    chunks = _asyncio.run(_run())
    assert len(chunks) > 1
    assert max(len(c) for c in chunks) <= tf._ZIP_CHUNK_BYTES + 64 * 1024
    zf = _zip.ZipFile(_io.BytesIO(b"".join(chunks)))
    assert sorted(zf.namelist()) == [f"big/part{i}.bin" for i in range(3)]
    for i in range(3):
        assert zf.read(f"big/part{i}.bin") == (big / f"part{i}.bin").read_bytes()


async def _noop_verify(request, credentials):
    return True


@pytest.mark.parametrize(
    "paths,status",
    [
        (["/nope.txt"], 404),
        (["/notes.txt", "/missing/dir"], 404),
        (["/../secret.txt"], 403),
        (["/../../etc"], 403),
    ],
)
def test_archive_pre_stream_errors_keep_status(client, paths, status):
    r = client.post(
        "/files/archive", headers={**_AUTH, **_USER}, json={"paths": paths}
    )
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/json")
    assert "detail" in r.json()


def test_archive_pre_stream_auth_errors_keep_status(client):
    r = client.post("/files/archive", headers=_USER, json={"paths": ["/notes.txt"]})
    assert r.status_code == 401
    r = client.post("/files/archive", headers=_AUTH, json={"paths": ["/notes.txt"]})
    assert r.status_code in (400, 401, 403)
    assert r.headers["content-type"].startswith("application/json")


def test_archive_producer_stops_and_closes_files_on_early_close(
    workspace, monkeypatch
):
    import asyncio as _asyncio
    import threading as _threading
    import time as _time

    big = workspace / "big"
    big.mkdir()
    for i in range(8):
        (big / f"part{i}.bin").write_bytes(os.urandom(512 * 1024))
    entries = [f"big/{f.name}" for f in sorted(big.iterdir())]

    opened = []
    real_open = tf._open_archive_member

    def _tracking_open(*a, **kw):
        fh, st = real_open(*a, **kw)
        opened.append(fh)
        return fh, st

    monkeypatch.setattr(tf, "_open_archive_member", _tracking_open)
    monkeypatch.setattr(tf, "_ZIP_CHUNK_BYTES", 16 * 1024)

    def _producers():
        return [
            t
            for t in _threading.enumerate()
            if t.name == "archive-zip-producer" and t.is_alive()
        ]

    before = set(_producers())

    async def _run():
        agen = tf._stream_zip(workspace.resolve(), entries)
        first = await agen.__anext__()
        assert first
        await agen.aclose()

    _asyncio.run(_run())

    deadline = _time.monotonic() + 5
    while _time.monotonic() < deadline and set(_producers()) - before:
        _time.sleep(0.02)
    assert not (set(_producers()) - before), "producer thread still running"
    assert opened, "producer never opened a source file"
    assert all(fh.closed for fh in opened)
    # Stopped early: not every member was read.
    assert len(opened) < len(entries)



def test_archive_mid_stream_read_error_aborts_instead_of_finishing(
    workspace, monkeypatch
):
    """A read failure after headers are sent must abort the body, never end it
    as if the (incomplete) zip were complete."""
    import asyncio as _asyncio

    def _boom(*a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(tf, "_open_archive_member", _boom)
    entries = ["notes.txt"]

    async def _run():
        return [c async for c in tf._stream_zip(workspace.resolve(), entries)]

    with pytest.raises(PermissionError):
        _asyncio.run(_run())


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


# --- archive admission control (ChatDRAGON #543 review) ----------------------


@pytest.fixture
def archive_admission(monkeypatch):
    """A fresh admission pool per test so slots never leak across tests."""
    fresh = tf._ArchiveAdmission()
    monkeypatch.setattr(tf, "_ARCHIVE_ADMISSION", fresh)
    return fresh


@pytest.fixture
def big_workspace(workspace, monkeypatch):
    big = workspace / "big"
    big.mkdir()
    for i in range(6):
        (big / f"part{i}.bin").write_bytes(os.urandom(512 * 1024))
    _patch_api_key(monkeypatch, "testkey")
    monkeypatch.setattr(
        tf.workspace_manager, "resolve", lambda u, backend=None: workspace
    )
    monkeypatch.setattr(tf, "verify_api_key", _noop_verify)
    # Small chunks: a consumer that stops reading leaves the producer blocked
    # on a full queue, i.e. a live thread pinned by a slow client.
    monkeypatch.setattr(tf, "_ZIP_CHUNK_BYTES", 16 * 1024)
    return workspace


def _live_producers() -> set:
    import threading as _threading

    return {
        t
        for t in _threading.enumerate()
        if t.name == "archive-zip-producer" and t.is_alive()
    }


def _req_for(user: str):
    class _Req:
        headers = {**_AUTH, "X-User-Email": user}

    return _Req()


async def _open_archive(user: str, paths=("/big",)):
    """Call the route like Starlette would and pull the first chunk only."""
    resp = await tf.archive_entries(
        _req_for(user), tf._ArchiveBody(paths=list(paths)), credentials=None
    )
    body = resp.body_iterator
    first = await body.__anext__()
    assert first
    return body


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    import asyncio as _asyncio
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        await _asyncio.sleep(0.01)
    return predicate()


def test_archive_admission_caps_live_producer_threads(
    big_workspace, archive_admission, monkeypatch
):
    import asyncio as _asyncio

    from fastapi import HTTPException as _HTTPException

    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "2")
    monkeypatch.setenv("ARCHIVE_MAX_PER_USER", "10")
    before = _live_producers()
    limit, extra = 2, 3

    async def _run():
        held = []
        for i in range(limit):
            held.append(await _open_archive(f"u{i}@corp.com"))
        assert len(_live_producers() - before) == limit
        for i in range(extra):
            with pytest.raises(_HTTPException) as exc:
                await _open_archive(f"x{i}@corp.com")
            assert exc.value.status_code == 429
            assert exc.value.headers["Retry-After"]
            assert exc.value.detail["scope"] == "global"
            # Refused without a thread: still exactly `limit` producers.
            assert len(_live_producers() - before) == limit
        assert archive_admission.snapshot()[0] == limit

        # Disconnect one client: its slot frees as soon as its producer exits,
        # and the very next request is admitted and starts streaming.
        await held.pop(0).aclose()
        assert await _wait_for(lambda: archive_admission.snapshot()[0] == limit - 1)
        assert len(_live_producers() - before) == limit - 1
        held.append(await _open_archive("next@corp.com"))
        assert len(_live_producers() - before) == limit
        for body in held:
            await body.aclose()
        assert await _wait_for(lambda: archive_admission.snapshot() == (0, {}))

    _asyncio.run(_run())
    assert not (_live_producers() - before)


def test_archive_per_user_limit_is_independent_of_global(
    big_workspace, archive_admission, monkeypatch
):
    import asyncio as _asyncio

    from fastapi import HTTPException as _HTTPException

    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "4")
    monkeypatch.setenv("ARCHIVE_MAX_PER_USER", "1")
    before = _live_producers()

    async def _run():
        alice = await _open_archive("alice@corp.com")
        with pytest.raises(_HTTPException) as exc:
            await _open_archive("alice@corp.com")
        assert exc.value.status_code == 429
        assert exc.value.detail["scope"] == "user"
        assert len(_live_producers() - before) == 1
        # Global capacity remains for other users.
        bob = await _open_archive("bob@corp.com")
        assert archive_admission.snapshot() == (
            2,
            {"alice@corp.com": 1, "bob@corp.com": 1},
        )
        await alice.aclose()
        assert await _wait_for(
            lambda: "alice@corp.com" not in archive_admission.snapshot()[1]
        )
        again = await _open_archive("alice@corp.com")
        await again.aclose()
        await bob.aclose()
        assert await _wait_for(lambda: archive_admission.snapshot() == (0, {}))

    _asyncio.run(_run())


def test_archive_over_limit_http_response_is_429_json_without_thread(
    client, archive_admission, monkeypatch
):
    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "1")
    _, held = archive_admission.try_acquire("someone@corp.com")
    before = _live_producers()
    r = client.post(
        "/files/archive", headers={**_AUTH, **_USER}, json={"paths": ["/notes.txt"]}
    )
    assert r.status_code == 429
    assert r.headers["content-type"].startswith("application/json")
    assert int(r.headers["Retry-After"]) > 0
    assert r.json()["detail"]["code"] == "archive_concurrency_limit"
    assert _live_producers() == before
    held.release()
    r = client.post(
        "/files/archive", headers={**_AUTH, **_USER}, json={"paths": ["/notes.txt"]}
    )
    assert r.status_code == 200
    assert archive_admission.snapshot() == (0, {})


def test_archive_slot_released_on_normal_completion(client, archive_admission):
    for _ in range(5):
        r = client.post(
            "/files/archive", headers={**_AUTH, **_USER}, json={"paths": ["/sub"]}
        )
        assert r.status_code == 200
    assert archive_admission.snapshot() == (0, {})


def test_archive_slot_released_on_producer_error(
    big_workspace, archive_admission, monkeypatch
):
    import asyncio as _asyncio

    def _boom(*a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(tf, "_open_archive_member", _boom)
    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "1")

    async def _run():
        resp = await tf.archive_entries(
            _req_for("alice@corp.com"),
            tf._ArchiveBody(paths=["/notes.txt"]),
            credentials=None,
        )
        with pytest.raises(PermissionError):
            async for _ in resp.body_iterator:
                pass
        assert await _wait_for(lambda: archive_admission.snapshot() == (0, {}))

    _asyncio.run(_run())


def test_archive_slot_released_when_body_is_never_iterated(
    big_workspace, archive_admission, monkeypatch
):
    import asyncio as _asyncio
    import gc

    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "1")
    before = _live_producers()

    async def _run():
        resp = await tf.archive_entries(
            _req_for("alice@corp.com"),
            tf._ArchiveBody(paths=["/big"]),
            credentials=None,
        )
        assert archive_admission.snapshot()[0] == 1
        # The client vanished before Starlette began streaming: the body is
        # dropped without a single __anext__, so no ``finally`` ever runs.
        del resp
        gc.collect()
        assert archive_admission.snapshot() == (0, {})
        assert _live_producers() == before
        body = await _open_archive("alice@corp.com")
        await body.aclose()

    _asyncio.run(_run())


@pytest.mark.parametrize(
    "headers,paths,status",
    [
        ({**_AUTH, **_USER}, ["/nope.txt"], 404),
        ({**_AUTH, **_USER}, ["/../secret.txt"], 403),
        (_USER, ["/notes.txt"], 401),
    ],
)
def test_archive_validation_errors_do_not_consume_slots(
    client, archive_admission, monkeypatch, headers, paths, status
):
    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "1")
    monkeypatch.setenv("ARCHIVE_MAX_PER_USER", "1")
    for _ in range(3):
        r = client.post("/files/archive", headers=headers, json={"paths": paths})
        assert r.status_code == status
    assert archive_admission.snapshot() == (0, {})
    r = client.post(
        "/files/archive", headers={**_AUTH, **_USER}, json={"paths": ["/notes.txt"]}
    )
    assert r.status_code == 200


def test_archive_slot_release_is_idempotent(archive_admission):
    slot, first = archive_admission.try_acquire("a@corp.com")
    second = slot.party()
    first.release()
    first.release()
    assert archive_admission.snapshot() == (1, {"a@corp.com": 1})
    second.release()
    second.release()
    assert slot.freed
    assert archive_admission.snapshot() == (0, {})
    with pytest.raises(RuntimeError):
        slot.party()


@pytest.mark.parametrize("raw", ["0", "-3", "abc", ""])
def test_archive_limits_fall_back_to_defaults_on_invalid_env(monkeypatch, raw):
    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", raw)
    monkeypatch.setenv("ARCHIVE_MAX_PER_USER", raw)
    assert tf._archive_max_concurrent() == 4
    assert tf._archive_max_per_user() == 2


class _BlockingWalk:
    """Patch ``Path.rglob`` so every recursive walk parks at a barrier, and
    count how many walks are inside it at once."""

    def __init__(self, monkeypatch):
        import threading as _threading

        self.lock = _threading.Lock()
        self.inside = 0
        self.peak = 0
        self.started = 0
        self.release = _threading.Event()
        self.fail = False
        real = Path.rglob
        walk = self

        def rglob(path_self, pattern):
            with walk.lock:
                walk.inside += 1
                walk.started += 1
                walk.peak = max(walk.peak, walk.inside)
            try:
                walk.release.wait(5)
                if walk.fail:
                    raise OSError("walk failed")
                yield from real(path_self, pattern)
            finally:
                with walk.lock:
                    walk.inside -= 1

        monkeypatch.setattr(Path, "rglob", rglob)


def test_archive_admission_bounds_concurrent_tree_walks(
    big_workspace, archive_admission, monkeypatch
):
    """#225 review: admission covers the recursive walk too. With every walk
    parked at a barrier and limit+N requests in flight, at most `limit` walks
    ever start; the rest get 429 without walking."""
    import asyncio as _asyncio

    from fastapi import HTTPException as _HTTPException

    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "2")
    monkeypatch.setenv("ARCHIVE_MAX_PER_USER", "10")
    walk = _BlockingWalk(monkeypatch)
    limit, extra = 2, 3

    async def _run():
        tasks = [
            _asyncio.create_task(_open_archive(f"u{i}@corp.com"))
            for i in range(limit + extra)
        ]
        assert await _wait_for(lambda: walk.started == limit)
        refused = 0
        for _ in range(extra):
            done, _pending = await _asyncio.wait(
                [t for t in tasks if not t.done()] or tasks,
                timeout=2,
                return_when=_asyncio.FIRST_COMPLETED,
            )
            refused = sum(
                1
                for t in tasks
                if t.done()
                and isinstance(t.exception(), _HTTPException)
                and t.exception().status_code == 429
            )
            if refused == extra:
                break
        assert refused == extra, "over-limit requests must be refused while walks run"
        assert walk.started == limit and walk.peak == limit, "no walk beyond the limit"
        walk.release.set()
        results = await _asyncio.gather(*tasks, return_exceptions=True)
        bodies = [r for r in results if not isinstance(r, BaseException)]
        assert len(bodies) == limit
        for body in bodies:
            await body.aclose()
        assert await _wait_for(lambda: archive_admission.snapshot() == (0, {}))

    _asyncio.run(_run())


def test_archive_slot_released_when_walk_fails(
    big_workspace, archive_admission, monkeypatch
):
    import asyncio as _asyncio

    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "1")
    walk = _BlockingWalk(monkeypatch)
    walk.fail = True
    walk.release.set()

    async def _run():
        with pytest.raises(OSError):
            await _open_archive("alice@corp.com")
        assert await _wait_for(lambda: archive_admission.snapshot() == (0, {}))
        # the next request is admitted at once
        walk.fail = False
        body = await _open_archive("bob@corp.com")
        await body.aclose()
        assert await _wait_for(lambda: archive_admission.snapshot() == (0, {}))

    _asyncio.run(_run())


def test_archive_cancelled_during_walk_keeps_slot_until_walk_stops(
    big_workspace, archive_admission, monkeypatch
):
    """A request cancelled mid-walk must not free its slot while the walk
    thread is still running -- otherwise cancelled requests could stack walks
    past the limit."""
    import asyncio as _asyncio

    monkeypatch.setenv("ARCHIVE_MAX_CONCURRENT", "1")
    walk = _BlockingWalk(monkeypatch)

    async def _run():
        task = _asyncio.create_task(_open_archive("alice@corp.com"))
        assert await _wait_for(lambda: walk.started == 1)
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task
        await _asyncio.sleep(0.05)
        assert archive_admission.snapshot()[0] == 1, "slot held while the walk runs"
        walk.release.set()
        assert await _wait_for(lambda: archive_admission.snapshot() == (0, {}))

    _asyncio.run(_run())


# --- conditional write (ChatDRAGON #213) -------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_if(client, name: str, body: bytes, expected: str):
    return client.post(
        f"/files/write_if?directory=/&expected_sha256={expected}",
        headers={**_AUTH, **_USER},
        files={"file": (name, body, "text/plain")},
    )


def _leftovers(workspace: Path):
    return sorted(p.name for p in workspace.iterdir() if ".cas-" in p.name)


def test_write_if_replaces_when_unchanged(client, workspace):
    (workspace / "doc.txt").write_text("v1")
    r = _write_if(client, "doc.txt", b"v1 + mine", _sha("v1"))
    assert r.status_code == 200
    assert r.json()["preserved"] is None
    assert (workspace / "doc.txt").read_text() == "v1 + mine"
    assert _leftovers(workspace) == []


def test_write_if_refuses_changed_and_deleted_files(client, workspace):
    (workspace / "doc.txt").write_text("v2 from agent")
    r = _write_if(client, "doc.txt", b"v1 + mine", _sha("v1"))
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "save_conflict", "exists": True, "preserved": None}
    assert (workspace / "doc.txt").read_text() == "v2 from agent"
    r = _write_if(client, "gone.txt", b"mine", _sha("v1"))
    assert r.status_code == 409 and r.json()["detail"]["exists"] is False
    assert not (workspace / "gone.txt").exists()
    # the whole folder is gone: still a conflict, never a 404 (404 means "no write_if")
    r = client.post(
        f"/files/write_if?directory=/nope&expected_sha256={_sha('v1')}",
        headers={**_AUTH, **_USER},
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert r.status_code == 409 and r.json()["detail"]["exists"] is False
    assert _leftovers(workspace) == []


def test_upload_does_not_take_expected_sha256(client, workspace):
    """The conditional write is a separate endpoint: a gateway without it answers
    404 instead of an unconditional upload silently ignoring the parameter, so
    callers fail closed (#541 review)."""
    (workspace / "doc.txt").write_text("v1")
    r = client.post(
        "/files/write_if_v0?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("doc.txt", b"x", "text/plain")},
    )
    assert r.status_code == 404
    assert (workspace / "doc.txt").read_text() == "v1"


def test_write_if_external_write_between_compare_and_claim(client, workspace, monkeypatch):
    """An agent/terminal write lands right after the compare: the claimed bytes
    no longer match, the external version is put back, 409."""
    (workspace / "doc.txt").write_text("v1")
    monkeypatch.setattr(tf, "_cas_after_compare", lambda t: t.write_text("v2 from agent"))
    r = _write_if(client, "doc.txt", b"v1 + mine", _sha("v1"))
    assert r.status_code == 409
    assert (workspace / "doc.txt").read_text() == "v2 from agent"
    assert _leftovers(workspace) == []


def test_write_if_path_writer_right_before_install(client, workspace, monkeypatch):
    """The reviewer's last-syscall case: a direct writer recreates the path
    immediately before the new body would be installed. link() never replaces
    an existing path, so that writer wins and nothing is lost."""
    (workspace / "doc.txt").write_text("v1")
    monkeypatch.setattr(tf, "_cas_before_install", lambda t: t.write_text("v2 from terminal"))
    r = _write_if(client, "doc.txt", b"v1 + mine", _sha("v1"))
    assert r.status_code == 409
    assert (workspace / "doc.txt").read_text() == "v2 from terminal"
    assert _leftovers(workspace) == []


def test_write_if_descriptor_writer_is_preserved_not_lost(client, workspace, monkeypatch):
    """A writer that opened the file before the save and keeps writing through
    its descriptor writes into the claimed inode. Its version is kept beside the
    file and named in the response instead of being silently discarded."""
    target = workspace / "doc.txt"
    target.write_text("v1")
    fd = open(target, "r+")
    monkeypatch.setattr(
        tf, "_cas_before_install", lambda t: (fd.seek(0), fd.write("v2 via fd"), fd.flush())
    )
    try:
        r = _write_if(client, "doc.txt", b"v1 + mine", _sha("v1"))
    finally:
        fd.close()
    assert r.status_code == 200
    preserved = r.json()["preserved"]
    assert preserved and preserved.startswith("/doc.conflict-")
    assert (workspace / preserved.lstrip("/")).read_text() == "v2 via fd"
    assert target.read_text() == "v1 + mine"
    assert _leftovers(workspace) == []


def _cas_in_process(args):
    """Child process: run the real conditional write with a barrier right after
    its compare (held for up to a second for the other process)."""
    import time as _time

    from src.routes import terminal_files as child_tf

    lock_root, target, body, expected, flag_dir, me = args

    def barrier(_t):
        (Path(flag_dir) / me).write_text("compared")
        deadline = _time.monotonic() + 1.0
        while _time.monotonic() < deadline and len(list(Path(flag_dir).iterdir())) < 2:
            _time.sleep(0.01)

    child_tf._cas_after_compare = barrier
    try:
        child_tf._cas_write(Path(lock_root), Path(target), body, expected)
        return "ok"
    except child_tf._WriteConflict:
        return "conflict"


def test_write_if_two_gateway_processes_exactly_one_wins(tmp_path):
    """Two independent processes (not one event loop) save against the same
    expected version, each pausing at a barrier right after its compare: exactly
    one succeeds and the file holds that body."""
    import multiprocessing

    root = tmp_path / "u" / "claude"
    root.mkdir(parents=True)
    target = root / "doc.txt"
    target.write_text("v1")
    flags = tmp_path / "flags"
    flags.mkdir()
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(2) as pool:
        results = pool.map(
            _cas_in_process,
            [
                (str(tmp_path / "u"), str(target), b"from A", _sha("v1"), str(flags), "a"),
                (str(tmp_path / "u"), str(target), b"from B", _sha("v1"), str(flags), "b"),
            ],
        )
    assert sorted(results) == ["conflict", "ok"]
    assert target.read_text() == ("from A" if results[0] == "ok" else "from B")
    assert _leftovers(root) == []


def test_write_if_rejects_bad_hash(client, workspace):
    r = _write_if(client, "doc.txt", b"x", "not-a-hash")
    assert r.status_code == 400


def test_write_if_keeps_file_mode(client, workspace):
    target = workspace / "run.sh"
    target.write_text("echo 1")
    target.chmod(0o750)
    r = _write_if(client, "run.sh", b"echo 2", _sha("echo 1"))
    assert r.status_code == 200
    assert target.read_text() == "echo 2"
    assert target.stat().st_mode & 0o777 == 0o750


# --- archive members are opened beneath the root, never by path (#225 review) --

_OUTSIDE_SECRET = b"OUTSIDE-WORKSPACE-SECRET-7f3a"


def _archive_with_swap(client, monkeypatch, paths, swap):
    """POST /files/archive with ``swap()`` run after the walk has chosen the
    members and before the producer opens any of them: the exact window a
    path check cannot cover."""
    real = tf._produce_zip
    swapped = []

    def _produce(*a, **kw):
        swap()
        swapped.append(True)
        return real(*a, **kw)

    monkeypatch.setattr(tf, "_produce_zip", _produce)
    r = client.post("/files/archive", headers={**_AUTH, **_USER}, json={"paths": paths})
    assert swapped == [True], "the swap must run between walk and producer"
    return r


def _zip_members(content: bytes) -> dict:
    import io as _io
    import zipfile as _zip

    zf = _zip.ZipFile(_io.BytesIO(content))
    assert zf.testzip() is None
    return {n: zf.read(n) for n in zf.namelist()}


def _assert_no_outside_bytes(members: dict) -> None:
    for name, data in members.items():
        assert _OUTSIDE_SECRET not in data, f"{name} carries bytes from outside"


def test_archive_member_swapped_for_symlink_is_skipped(
    client, workspace, tmp_path, monkeypatch
):
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(_OUTSIDE_SECRET)
    d = workspace / "dir"
    d.mkdir()
    (d / "item.txt").write_text("item")
    (d / "keep.txt").write_text("keep")
    (d / "gone.txt").write_text("gone")

    def _swap():
        (d / "item.txt").unlink()
        (d / "item.txt").symlink_to(secret)
        (d / "gone.txt").unlink()

    r = _archive_with_swap(client, monkeypatch, ["/dir"], _swap)
    assert r.status_code == 200
    members = _zip_members(r.content)
    _assert_no_outside_bytes(members)
    assert members == {"dir/keep.txt": b"keep"}


def test_archive_directory_swapped_for_symlink_is_skipped(
    client, workspace, tmp_path, monkeypatch
):
    outside = tmp_path / "outside-dir"
    (outside / "deeper").mkdir(parents=True)
    (outside / "x.txt").write_bytes(_OUTSIDE_SECRET)
    (outside / "deeper" / "y.txt").write_bytes(_OUTSIDE_SECRET)
    nested = workspace / "dir" / "nested"
    (nested / "deeper").mkdir(parents=True)
    (nested / "x.txt").write_text("x")
    (nested / "deeper" / "y.txt").write_text("y")
    (workspace / "dir" / "keep.txt").write_text("keep")

    def _swap():
        nested.rename(tmp_path / "moved-away")
        nested.symlink_to(outside, target_is_directory=True)

    r = _archive_with_swap(client, monkeypatch, ["/dir", "/notes.txt"], _swap)
    assert r.status_code == 200
    members = _zip_members(r.content)
    _assert_no_outside_bytes(members)
    assert members == {"dir/keep.txt": b"keep", "notes.txt": b"hello\nworld\n"}


def test_archive_member_swapped_for_fifo_is_skipped_without_blocking(
    client, workspace, monkeypatch
):
    d = workspace / "dir"
    d.mkdir()
    (d / "pipe").write_text("was a file")
    (d / "keep.txt").write_text("keep")

    def _swap():
        (d / "pipe").unlink()
        os.mkfifo(d / "pipe")

    r = _archive_with_swap(client, monkeypatch, ["/dir"], _swap)
    assert r.status_code == 200
    assert _zip_members(r.content) == {"dir/keep.txt": b"keep"}


def test_archive_refuses_a_workspace_root_swapped_after_the_walk(
    client, workspace, tmp_path, monkeypatch
):
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (outside / "notes.txt").write_bytes(_OUTSIDE_SECRET)

    def _swap():
        workspace.rename(tmp_path / "real-root")
        workspace.symlink_to(outside, target_is_directory=True)

    with pytest.raises(tf._UnsafeArchiveMember):
        _archive_with_swap(client, monkeypatch, ["/notes.txt"], _swap)


# --- every route opens beneath the pinned root, never by path ----------------
# Same class as the archive TOCTOU (#225 review): the path is checked by
# ``_resolve_or_403``, then the workspace changes before the route opens it.
# ``_swap_before_open`` runs the swap at the exact point between the check and
# the first open beneath the root.


def _arm_swap(monkeypatch, swap, when):
    """Run ``swap`` once: ``before`` the root is pinned, or ``after`` the first
    directory fd beneath it is open (so only fd-relative operations can still
    reach what was checked)."""
    if when == "before":
        return _swap_before_open(monkeypatch, swap)
    ran = []
    real = tf._open_dir_beneath

    def _open(*a, **kw):
        fd = real(*a, **kw)
        if not ran:
            ran.append(True)
            swap()
        return fd

    monkeypatch.setattr(tf, "_open_dir_beneath", _open)
    return ran


def _swap_before_open(monkeypatch, swap):
    ran = []
    real_pinned, real_pin = tf._pinned_root, tf._pin_root

    def _once():
        if not ran:
            ran.append(True)
            swap()

    def _pinned(*a, **kw):
        _once()
        return real_pinned(*a, **kw)

    def _pin(*a, **kw):
        _once()
        return real_pin(*a, **kw)

    monkeypatch.setattr(tf, "_pinned_root", _pinned)
    monkeypatch.setattr(tf, "_pin_root", _pin)
    return ran


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside-area"
    (d / "deeper").mkdir(parents=True)
    (d / "inner.md").write_bytes(_OUTSIDE_SECRET)
    (d / "deeper" / "x.txt").write_bytes(_OUTSIDE_SECRET)
    (tmp_path / "outside-file.txt").write_bytes(_OUTSIDE_SECRET)
    return d


def _swap_file(workspace, rel, target):
    def _swap():
        p = workspace / rel
        p.unlink()
        p.symlink_to(target)

    return _swap


def _swap_dir(workspace, tmp_path, rel, target):
    def _swap():
        p = workspace / rel
        p.rename(tmp_path / ("moved-" + p.name))
        p.symlink_to(target, target_is_directory=True)

    return _swap


_READ_ROUTES = [
    "/files/read?path=/sub/inner.md",
    "/files/digest?path=/sub/inner.md",
    "/files/view?path=/sub/inner.md",
    "/files/serve/sub/inner.md",
]
_SECRET_SHA = hashlib.sha256(_OUTSIDE_SECRET).hexdigest()
_WHEN = ["before", "after"]


def _assert_no_outside_bytes_in(r) -> None:
    assert _OUTSIDE_SECRET not in r.content
    assert _SECRET_SHA not in r.text


@pytest.mark.parametrize("when", _WHEN)
@pytest.mark.parametrize("route", _READ_ROUTES)
def test_read_routes_refuse_a_file_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, route, when
):
    ran = _arm_swap(
        monkeypatch,
        _swap_file(workspace, "sub/inner.md", tmp_path / "outside-file.txt"),
        when,
    )
    r = client.get(route, headers={**_AUTH, **_USER})
    assert ran == [True]
    assert r.status_code == 404
    _assert_no_outside_bytes_in(r)


@pytest.mark.parametrize("when", _WHEN)
@pytest.mark.parametrize("route", _READ_ROUTES)
def test_read_routes_refuse_a_directory_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, route, when
):
    ran = _arm_swap(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside), when)
    r = client.get(route, headers={**_AUTH, **_USER})
    assert ran == [True]
    # Before the pin the swap is refused; after it, the fd still leads to the
    # directory that was checked, so the original file is served.
    assert r.status_code in (404, 200)
    _assert_no_outside_bytes_in(r)


@pytest.mark.parametrize(
    "route", _READ_ROUTES[:3]
)
def test_read_routes_refuse_a_file_swapped_for_a_fifo_without_blocking(
    client, workspace, monkeypatch, route
):
    def _swap():
        (workspace / "sub" / "inner.md").unlink()
        os.mkfifo(workspace / "sub" / "inner.md")

    _swap_before_open(monkeypatch, _swap)
    r = client.get(route, headers={**_AUTH, **_USER})
    assert r.status_code == 404


def test_view_keeps_serving_the_checked_file_after_a_later_swap(
    client, workspace, tmp_path, outside, monkeypatch
):
    """The response body comes from the file opened at check time, even if the
    path is swapped between opening and sending."""
    real = tf._read_beneath

    def _read_then_swap(root, target):
        opened = real(root, target)
        _swap_file(workspace, "sub/inner.md", tmp_path / "outside-file.txt")()
        return opened

    monkeypatch.setattr(tf, "_read_beneath", _read_then_swap)
    r = client.get("/files/view?path=/sub/inner.md", headers={**_AUTH, **_USER})
    assert r.status_code == 200
    assert r.content == b"# inner"


@pytest.mark.parametrize("when", _WHEN)
def test_list_refuses_a_directory_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, when
):
    ran = _arm_swap(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside), when)
    r = client.get("/files/list?directory=/sub", headers={**_AUTH, **_USER})
    assert ran == [True]
    assert r.status_code in (404, 200)
    assert "deeper" not in r.text


# --- write routes create/rename/delete beneath the pinned root ---------------


def _outside_tree(outside: Path) -> dict:
    return {
        p.relative_to(outside).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in outside.rglob("*")
    }


@pytest.mark.parametrize("when", _WHEN)
def test_upload_refuses_a_destination_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, when
):
    victim = tmp_path / "outside-file.txt"
    ran = _arm_swap(monkeypatch, _swap_file(workspace, "sub/inner.md", victim), when)
    r = client.post(
        "/files/upload?directory=/sub",
        headers={**_AUTH, **_USER},
        files={"file": ("inner.md", b"PAYLOAD", "text/plain")},
    )
    assert ran == [True]
    assert r.status_code == 409
    assert victim.read_bytes() == _OUTSIDE_SECRET


@pytest.mark.parametrize("when", _WHEN)
def test_upload_refuses_a_directory_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, when
):
    before = _outside_tree(outside)
    ran = _arm_swap(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside), when)
    r = client.post(
        "/files/upload?directory=/sub",
        headers={**_AUTH, **_USER},
        files={"file": ("inner.md", b"PAYLOAD", "text/plain")},
    )
    assert ran == [True]
    assert r.status_code in (409, 200)
    assert _outside_tree(outside) == before


def test_upload_onto_an_existing_fifo_is_refused_without_blocking(client, workspace):
    os.mkfifo(workspace / "pipe")
    r = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("pipe", b"PAYLOAD", "text/plain")},
    )
    assert r.status_code == 409


@pytest.mark.parametrize("when", _WHEN)
def test_write_if_refuses_a_directory_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, when
):
    """``expected`` is the OUTSIDE file's digest: a write that followed the
    symlink would match it and replace the outside file."""
    before = _outside_tree(outside)
    ran = _arm_swap(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside), when)
    r = client.post(
        f"/files/write_if?directory=/sub&expected_sha256={_SECRET_SHA}",
        headers={**_AUTH, **_USER},
        files={"file": ("inner.md", b"PAYLOAD", "text/plain")},
    )
    assert ran == [True]
    assert r.status_code == 409
    assert _outside_tree(outside) == before


def test_mkdir_refuses_a_parent_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch
):
    before = _outside_tree(outside)
    ran = _swap_before_open(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside))
    r = client.post(
        "/files/mkdir", headers={**_AUTH, **_USER}, json={"path": "/sub/made/deep"}
    )
    assert ran == [True]
    assert r.status_code == 409
    assert _outside_tree(outside) == before


@pytest.mark.parametrize("when", _WHEN)
@pytest.mark.parametrize("victim", ["deeper/x.txt", "deeper"])
def test_delete_refuses_a_parent_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, victim, when
):
    (workspace / "sub" / "deeper").mkdir()
    (workspace / "sub" / "deeper" / "x.txt").write_text("mine")
    before = _outside_tree(outside)
    ran = _arm_swap(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside), when)
    r = client.delete(f"/files/delete?path=/sub/{victim}", headers={**_AUTH, **_USER})
    assert ran == [True]
    assert r.status_code in (409, 200)
    assert _outside_tree(outside) == before


@pytest.mark.parametrize("when", _WHEN)
@pytest.mark.parametrize("no_clobber", [False, True])
@pytest.mark.parametrize("side", ["source", "destination"])
def test_move_refuses_a_directory_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, side, no_clobber, when
):
    (workspace / "sub" / "deeper").mkdir()
    (workspace / "sub" / "deeper" / "x.txt").write_text("mine")
    (workspace / "other").mkdir()
    before = _outside_tree(outside)
    body = (
        {"source": "/sub/deeper/x.txt", "destination": "/other/x.txt"}
        if side == "source"
        else {"source": "/notes.txt", "destination": "/sub/deeper/x.txt"}
    )
    ran = _arm_swap(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside), when)
    r = client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={**body, "no_clobber": no_clobber},
    )
    assert ran == [True]
    assert r.status_code in (409, 200)
    assert _outside_tree(outside) == before
    for p in workspace.rglob("*"):
        if p.is_file() and not p.is_symlink():
            assert _OUTSIDE_SECRET not in p.read_bytes(), p


@pytest.mark.parametrize("when", _WHEN)
@pytest.mark.parametrize(
    "body",
    [
        {"source": "/sub/inner.md", "destination": "/copied.md"},
        {"source": "/sub", "destination": "/copied"},
        {"source": "/notes.txt", "destination": "/sub/notes.txt"},
    ],
)
def test_copy_refuses_a_directory_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, body, when
):
    before = _outside_tree(outside)
    ran = _arm_swap(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside), when)
    r = client.post("/files/copy", headers={**_AUTH, **_USER}, json=body)
    assert ran == [True]
    assert r.status_code in (409, 200)
    assert _outside_tree(outside) == before
    for p in workspace.rglob("*"):
        if p.is_file() and not p.is_symlink():
            assert _OUTSIDE_SECRET not in p.read_bytes(), p


# --- a cross-device move never reopens the checked pathnames ------------------
# renameat() fails with EXDEV across a mount inside the workspace. The fallback
# must copy + unlink through the pinned parent fds, not ``shutil.move`` on the
# early-resolved paths: a parent swapped for a symlink after the pin would
# otherwise take the move outside the workspace.


def _exdev_rename(monkeypatch, swap=None):
    """Make ``_rename_at`` fail with EXDEV, running ``swap`` first (after both
    parents are pinned)."""
    calls = []

    def _rename(*a, **kw):
        calls.append(a)
        if swap is not None:
            swap()
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

    monkeypatch.setattr(tf, "_rename_at", _rename)
    return calls


def _no_move_leftovers(workspace: Path) -> None:
    assert not [p for p in workspace.rglob(".gw-move-*")]


def _move(client, source, destination):
    return client.post(
        "/files/move",
        headers={**_AUTH, **_USER},
        json={"source": source, "destination": destination},
    )


def test_move_across_devices_renames_a_file(client, workspace, monkeypatch):
    calls = _exdev_rename(monkeypatch)
    r = _move(client, "/notes.txt", "/renamed.txt")
    assert r.status_code == 200
    assert calls
    assert not (workspace / "notes.txt").exists()
    assert (workspace / "renamed.txt").read_text() == "hello\nworld\n"
    _no_move_leftovers(workspace)


def test_move_across_devices_replaces_an_existing_file(client, workspace, monkeypatch):
    _exdev_rename(monkeypatch)
    r = _move(client, "/notes.txt", "/blob.bin")
    assert r.status_code == 200
    assert not (workspace / "notes.txt").exists()
    assert (workspace / "blob.bin").read_text() == "hello\nworld\n"
    _no_move_leftovers(workspace)


def test_move_across_devices_into_an_existing_directory(client, workspace, monkeypatch):
    _exdev_rename(monkeypatch)
    r = _move(client, "/notes.txt", "/sub")
    assert r.status_code == 200
    assert not (workspace / "notes.txt").exists()
    assert (workspace / "sub" / "notes.txt").read_text() == "hello\nworld\n"
    assert (workspace / "sub" / "inner.md").read_text() == "# inner"
    _no_move_leftovers(workspace)


def test_move_across_devices_moves_a_directory_tree(client, workspace, monkeypatch):
    (workspace / "sub" / "deeper").mkdir()
    (workspace / "sub" / "deeper" / "x.txt").write_text("mine")
    (workspace / "sub" / "link").symlink_to("inner.md")
    _exdev_rename(monkeypatch)
    r = _move(client, "/sub", "/moved")
    assert r.status_code == 200
    assert not (workspace / "sub").exists()
    assert (workspace / "moved" / "inner.md").read_text() == "# inner"
    assert (workspace / "moved" / "deeper" / "x.txt").read_text() == "mine"
    assert os.readlink(workspace / "moved" / "link") == "inner.md"
    _no_move_leftovers(workspace)


def test_move_across_devices_failed_copy_keeps_the_source(
    client, workspace, monkeypatch
):
    _exdev_rename(monkeypatch)

    def _boom(*a, **kw):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(tf, "_write_all", _boom)
    with pytest.raises(OSError):
        _move(client, "/notes.txt", "/renamed.txt")
    assert (workspace / "notes.txt").read_text() == "hello\nworld\n"
    assert not (workspace / "renamed.txt").exists()
    _no_move_leftovers(workspace)


@pytest.mark.parametrize(
    "body",
    [
        # (source, destination, gone, landed, content) — gone/landed relative
        # to tmp_path, where the pinned (pre-swap) ``sub`` now lives at moved-sub.
        # source parent swapped: a file, and a whole directory
        (
            "/sub/deeper/x.txt",
            "/other/x.txt",
            "moved-sub/deeper/x.txt",
            "alice/claude/other/x.txt",
            "mine",
        ),
        (
            "/sub/deeper",
            "/other/deeper",
            "moved-sub/deeper",
            "alice/claude/other/deeper/x.txt",
            "mine",
        ),
        # destination parent swapped: replace, move into a directory, a tree
        (
            "/notes.txt",
            "/sub/deeper/x.txt",
            "alice/claude/notes.txt",
            "moved-sub/deeper/x.txt",
            "hello\nworld\n",
        ),
        (
            "/notes.txt",
            "/sub/deeper",
            "alice/claude/notes.txt",
            "moved-sub/deeper/notes.txt",
            "hello\nworld\n",
        ),
        (
            "/other/dir",
            "/sub/deeper/dir",
            "alice/claude/other/dir",
            "moved-sub/deeper/dir/f.txt",
            "mine too",
        ),
    ],
)
def test_move_across_devices_refuses_a_parent_swapped_for_a_symlink(
    client, workspace, tmp_path, outside, monkeypatch, body
):
    (workspace / "sub" / "deeper").mkdir()
    (workspace / "sub" / "deeper" / "x.txt").write_text("mine")
    (workspace / "other" / "dir").mkdir(parents=True)
    (workspace / "other" / "dir" / "f.txt").write_text("mine too")
    before = _outside_tree(outside)
    calls = _exdev_rename(monkeypatch, _swap_dir(workspace, tmp_path, "sub", outside))
    source, destination, gone, landed, content = body
    r = _move(client, source, destination)
    assert calls
    # Nothing outside was read into the workspace, created, replaced or deleted.
    assert _outside_tree(outside) == before
    for p in workspace.rglob("*"):
        if p.is_file() and not p.is_symlink():
            assert _OUTSIDE_SECRET not in p.read_bytes(), p
    # The move completed on the entries that were pinned, wherever they are now.
    assert r.status_code == 200
    assert not (tmp_path / gone).exists()
    assert (tmp_path / landed).read_text() == content
    _no_move_leftovers(workspace)
    _no_move_leftovers(tmp_path / "moved-sub")
