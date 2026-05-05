"""Tests for admin skills management — service logic and API endpoints."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.admin_service import (
    _parse_skill_frontmatter,
    _validate_skill_name,
    create_or_update_skill,
    delete_skill,
    get_skill,
    list_skills,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    """Workspace with sample skills."""
    skills_dir = tmp_path / ".claude" / "skills"

    # Skill: hello-world
    hw = skills_dir / "hello-world"
    hw.mkdir(parents=True)
    (hw / "SKILL.md").write_text(
        "---\nname: hello-world\ndescription: A greeting skill\n"
        "metadata:\n  author: tester\n  version: 2.0.0\n---\n\n# Hello\n\nBody here.\n"
    )

    # Skill: bare (no frontmatter)
    bare = skills_dir / "bare"
    bare.mkdir(parents=True)
    (bare / "SKILL.md").write_text("# Just markdown, no frontmatter\n")

    with patch("src.admin_service.get_workspace_root", return_value=tmp_path):
        yield tmp_path


@pytest.fixture
def empty_workspace(tmp_path):
    """Workspace with no skills directory."""
    (tmp_path / ".claude").mkdir(parents=True)
    with patch("src.admin_service.get_workspace_root", return_value=tmp_path):
        yield tmp_path


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


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\nname: test\ndescription: desc\n---\n\nBody"
        meta = _parse_skill_frontmatter(content)
        assert meta["name"] == "test"
        assert meta["description"] == "desc"

    def test_no_frontmatter(self):
        meta = _parse_skill_frontmatter("# Just markdown")
        assert meta == {}

    def test_incomplete_frontmatter(self):
        meta = _parse_skill_frontmatter("---\nname: test\n")
        assert meta == {}

    def test_invalid_yaml(self):
        meta = _parse_skill_frontmatter("---\n: [invalid yaml\n---\nBody")
        assert meta == {}

    def test_non_dict_yaml(self):
        meta = _parse_skill_frontmatter("---\n- list item\n---\nBody")
        assert meta == {}

    def test_nested_metadata(self):
        content = "---\nname: x\nmetadata:\n  author: alice\n  version: 1.0.0\n---\nBody"
        meta = _parse_skill_frontmatter(content)
        assert meta["metadata"]["author"] == "alice"
        assert meta["metadata"]["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# Skill name validation
# ---------------------------------------------------------------------------


class TestValidateSkillName:
    def test_valid_names(self):
        for name in ["hello", "hello-world", "a1", "my-skill-v2"]:
            _validate_skill_name(name)  # should not raise

    def test_reject_empty(self):
        with pytest.raises(ValueError, match="Invalid skill name"):
            _validate_skill_name("")

    def test_reject_uppercase(self):
        with pytest.raises(ValueError, match="Invalid skill name"):
            _validate_skill_name("Hello")

    def test_reject_spaces(self):
        with pytest.raises(ValueError, match="Invalid skill name"):
            _validate_skill_name("hello world")

    def test_reject_starts_with_hyphen(self):
        with pytest.raises(ValueError, match="Invalid skill name"):
            _validate_skill_name("-hello")

    def test_reject_special_chars(self):
        with pytest.raises(ValueError, match="Invalid skill name"):
            _validate_skill_name("hello_world")

    def test_reject_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid skill name"):
            _validate_skill_name("../evil")


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------


class TestListSkills:
    def test_list_skills(self, workspace):
        skills = list_skills()
        assert len(skills) == 2
        names = {s["name"] for s in skills}
        assert names == {"hello-world", "bare"}

    def test_list_skills_with_metadata(self, workspace):
        skills = list_skills()
        hw = next(s for s in skills if s["name"] == "hello-world")
        assert hw["description"] == "A greeting skill"
        assert hw["version"] == "2.0.0"
        assert hw["author"] == "tester"

    def test_list_skills_bare_metadata(self, workspace):
        skills = list_skills()
        bare = next(s for s in skills if s["name"] == "bare")
        assert bare["description"] == ""
        assert bare["version"] == ""

    def test_empty_workspace(self, empty_workspace):
        skills = list_skills()
        assert skills == []

    def test_ignores_symlinked_skill_dir(self, workspace):
        import tempfile

        outside = Path(tempfile.mkdtemp())
        (outside / "SKILL.md").write_text("---\nname: evil\n---\nEvil")
        (workspace / ".claude" / "skills" / "evil-link").symlink_to(outside)
        skills = list_skills()
        names = {s["name"] for s in skills}
        assert "evil-link" not in names

    def test_ignores_symlinked_claude_dir(self, tmp_path):
        """If .claude itself is a symlink, list_skills must return empty."""
        import tempfile

        outside = Path(tempfile.mkdtemp())
        skill = outside / "skills" / "leak"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\ndescription: leaked\n---\nbody")
        (tmp_path / ".claude").symlink_to(outside)
        with patch("src.admin_service.get_workspace_root", return_value=tmp_path):
            skills = list_skills()
        assert skills == []


# ---------------------------------------------------------------------------
# Backend skill roots
# ---------------------------------------------------------------------------


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
        assert not (multi_backend_workspace / ".opencode" / "skills" / "hello-world").exists()
        assert (multi_backend_workspace / ".claude" / "skills" / "hello-world").exists()

    def test_rejects_unknown_skill_backend(self, workspace):
        with pytest.raises(ValueError, match="Unsupported skill backend"):
            list_skills(backend="bad-backend")


# ---------------------------------------------------------------------------
# get_skill
# ---------------------------------------------------------------------------


class TestGetSkill:
    def test_get_existing(self, workspace):
        meta, content, etag = get_skill("hello-world")
        assert meta["name"] == "hello-world"
        assert "Body here." in content
        assert etag

    def test_get_nonexistent(self, workspace):
        with pytest.raises(FileNotFoundError):
            get_skill("nonexistent")

    def test_get_invalid_name(self, workspace):
        with pytest.raises(ValueError, match="Invalid skill name"):
            get_skill("INVALID")


# ---------------------------------------------------------------------------
# create_or_update_skill
# ---------------------------------------------------------------------------


class TestCreateOrUpdateSkill:
    def test_create_new(self, workspace):
        content = "---\nname: new-skill\n---\n\n# New Skill\n"
        etag, created = create_or_update_skill("new-skill", content)
        assert created is True
        assert etag

        # Verify it exists
        meta, read_content, _ = get_skill("new-skill")
        assert meta["name"] == "new-skill"
        assert read_content == content

    def test_update_existing(self, workspace):
        _, _, old_etag = get_skill("hello-world")
        new_content = "---\nname: hello-world\ndescription: updated\n---\n\nUpdated body.\n"
        new_etag, created = create_or_update_skill("hello-world", new_content, old_etag)
        assert created is False
        assert new_etag != old_etag

    def test_etag_mismatch(self, workspace):
        with pytest.raises(ValueError, match="ETag mismatch"):
            create_or_update_skill("hello-world", "content", "wrong-etag")

    def test_invalid_name(self, workspace):
        with pytest.raises(ValueError, match="Invalid skill name"):
            create_or_update_skill("BAD NAME", "content")

    def test_creates_parent_dirs(self, empty_workspace):
        content = "---\nname: brand-new\n---\nContent"
        etag, created = create_or_update_skill("brand-new", content)
        assert created is True
        skill_file = empty_workspace / ".claude" / "skills" / "brand-new" / "SKILL.md"
        assert skill_file.is_file()


# ---------------------------------------------------------------------------
# delete_skill
# ---------------------------------------------------------------------------


class TestDeleteSkill:
    def test_delete_existing(self, workspace):
        delete_skill("hello-world")
        skill_dir = workspace / ".claude" / "skills" / "hello-world"
        assert not skill_dir.exists()

    def test_delete_nonexistent(self, workspace):
        with pytest.raises(FileNotFoundError, match="Skill not found"):
            delete_skill("nonexistent")

    def test_delete_invalid_name(self, workspace):
        with pytest.raises(ValueError, match="Invalid skill name"):
            delete_skill("INVALID")

    def test_delete_symlinked_dir(self, workspace):
        import tempfile

        outside = Path(tempfile.mkdtemp())
        (outside / "SKILL.md").write_text("evil")
        (workspace / ".claude" / "skills" / "evil-link").symlink_to(outside)
        with pytest.raises(ValueError, match="(Symlinks|escapes workspace)"):
            delete_skill("evil-link")

    def test_delete_with_symlinked_claude_ancestor(self, tmp_path):
        """delete_skill must reject when .claude is a symlink."""
        import tempfile

        outside = Path(tempfile.mkdtemp())
        skill = outside / "skills" / "victim"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x")
        (tmp_path / ".claude").symlink_to(outside)
        with patch("src.admin_service.get_workspace_root", return_value=tmp_path):
            with pytest.raises(ValueError, match="(Symlinks|ancestors)"):
                delete_skill("victim")
        # Ensure the outside directory was NOT deleted
        assert (outside / "skills" / "victim").exists()


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(workspace):
    """FastAPI TestClient with admin auth bypassed."""
    import os

    from fastapi.testclient import TestClient

    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
        from src.main import app
        from src.admin_auth import require_admin

        app.dependency_overrides[require_admin] = lambda: True
        client = TestClient(app)
        yield client
        app.dependency_overrides.pop(require_admin, None)


class TestSkillsAPI:
    def test_list_skills(self, admin_client):
        r = admin_client.get("/admin/api/skills")
        assert r.status_code == 200
        data = r.json()
        assert "skills" in data
        names = {s["name"] for s in data["skills"]}
        assert "hello-world" in names

    def test_get_skill(self, admin_client):
        r = admin_client.get("/admin/api/skills/hello-world")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "hello-world"
        assert "content" in data
        assert "etag" in data
        assert "metadata" in data

    def test_get_skill_not_found(self, admin_client):
        r = admin_client.get("/admin/api/skills/nonexistent")
        assert r.status_code == 404

    def test_get_skill_invalid_name(self, admin_client):
        r = admin_client.get("/admin/api/skills/BAD%20NAME")
        assert r.status_code == 400

    def test_create_skill(self, admin_client):
        r = admin_client.put(
            "/admin/api/skills/new-skill",
            json={"content": "---\nname: new-skill\n---\nBody"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["status"] == "created"
        assert data["etag"]

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
        assert (workspace / ".opencode" / "skills" / "open-skill" / "SKILL.md").is_file()

    def test_invalid_skill_backend_query_param(self, admin_client):
        r = admin_client.get("/admin/api/skills?backend=invalid")
        assert r.status_code == 400
        assert "Unsupported skill backend" in r.json()["error"]

    def test_update_skill(self, admin_client):
        # Get current etag
        r = admin_client.get("/admin/api/skills/hello-world")
        etag = r.json()["etag"]

        r = admin_client.put(
            "/admin/api/skills/hello-world",
            json={"content": "---\nname: hello-world\n---\nUpdated", "etag": etag},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "updated"

    def test_update_skill_etag_conflict(self, admin_client):
        r = admin_client.put(
            "/admin/api/skills/hello-world",
            json={"content": "new content", "etag": "wrong-etag"},
        )
        assert r.status_code == 409

    def test_delete_skill(self, admin_client):
        r = admin_client.delete("/admin/api/skills/hello-world")
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"

        # Verify it's gone
        r = admin_client.get("/admin/api/skills/hello-world")
        assert r.status_code == 404

    def test_delete_skill_not_found(self, admin_client):
        r = admin_client.delete("/admin/api/skills/nonexistent")
        assert r.status_code == 404
