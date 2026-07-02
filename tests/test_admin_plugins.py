"""Tests for the admin plugin / marketplace management routes.

The service layer (``src.plugin_admin_service``) is mocked — these tests cover
the route wiring: admin gating, request-model plumbing, success pass-through,
and ``PluginAdminError`` -> HTTP 400.
"""

import os
from unittest.mock import patch

import pytest

from src.plugin_admin_service import PluginAdminError


@pytest.fixture
def admin_client():
    """FastAPI TestClient with admin auth bypassed."""
    from fastapi.testclient import TestClient

    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
        from src.admin_auth import require_admin
        from src.main import app

        app.dependency_overrides[require_admin] = lambda: True
        client = TestClient(app)
        yield client
        app.dependency_overrides.pop(require_admin, None)


@pytest.fixture
def anon_client():
    """FastAPI TestClient with admin auth ENFORCED (no override)."""
    from fastapi.testclient import TestClient

    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
        from src.main import app

        yield TestClient(app)


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


class TestAuthGating:
    def test_add_marketplace_requires_admin(self, anon_client):
        r = anon_client.post(
            "/admin/api/marketplaces", json={"repo": "https://x/y.git"}
        )
        assert r.status_code in (401, 403)

    def test_remove_marketplace_requires_admin(self, anon_client):
        r = anon_client.delete("/admin/api/marketplaces/foo")
        assert r.status_code in (401, 403)

    def test_refresh_marketplace_requires_admin(self, anon_client):
        r = anon_client.post("/admin/api/marketplaces/foo/refresh")
        assert r.status_code in (401, 403)

    def test_install_plugin_requires_admin(self, anon_client):
        r = anon_client.post("/admin/api/plugins", json={"name": "octo"})
        assert r.status_code in (401, 403)

    def test_uninstall_plugin_requires_admin(self, anon_client):
        r = anon_client.delete("/admin/api/plugins/octo@mp")
        assert r.status_code in (401, 403)

    def test_manifest_requires_admin(self, anon_client):
        r = anon_client.get("/admin/api/plugins/manifest")
        assert r.status_code in (401, 403)

    def test_catalog_requires_admin(self, anon_client):
        r = anon_client.get("/admin/api/marketplaces/catalog")
        assert r.status_code in (401, 403)

    def test_marketplace_plugins_requires_admin(self, anon_client):
        r = anon_client.get("/admin/api/marketplaces/mp/plugins")
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Route ordering: manifest must not be shadowed by the catch-all detail route
# ---------------------------------------------------------------------------


class TestRouteOrdering:
    def test_manifest_not_shadowed_by_detail(self, admin_client):
        with patch("src.plugin_manifest.list_added", return_value=[{"name": "octo"}]):
            with patch("src.plugin_manifest.list_removed", return_value=["old@mp"]):
                r = admin_client.get("/admin/api/plugins/manifest")
        assert r.status_code == 200
        data = r.json()
        assert data == {"added": [{"name": "octo"}], "removed": ["old@mp"]}

    def test_catalog_not_shadowed_by_name_param(self, admin_client):
        """`/marketplaces/catalog` resolves to the literal route, not {name}."""
        with patch(
            "src.plugin_service.get_marketplaces_with_plugins",
            return_value=[{"name": "mp", "plugins": [], "plugin_count": 0}],
        ) as mock_catalog:
            with patch(
                "src.plugin_service.list_marketplace_plugins"
            ) as mock_one:
                r = admin_client.get("/admin/api/marketplaces/catalog")
        assert r.status_code == 200
        mock_catalog.assert_called_once_with()
        mock_one.assert_not_called()


# ---------------------------------------------------------------------------
# Marketplace catalog (read-only)
# ---------------------------------------------------------------------------


class TestMarketplacesCatalog:
    def test_happy_path(self, admin_client):
        catalog = [
            {
                "name": "mp",
                "source_type": "github",
                "repo": "owner/repo",
                "last_updated": "2026-01-01T00:00:00Z",
                "plugins": [
                    {
                        "name": "octo",
                        "description": "d",
                        "version": "1.0",
                        "skill_count": 2,
                        "id": "octo@mp",
                        "installed": True,
                    }
                ],
                "plugin_count": 1,
            }
        ]
        with patch(
            "src.plugin_service.get_marketplaces_with_plugins",
            return_value=catalog,
        ) as mock_catalog:
            r = admin_client.get("/admin/api/marketplaces/catalog")
        assert r.status_code == 200
        assert r.json() == {"marketplaces": catalog}
        mock_catalog.assert_called_once_with()


class TestMarketplacePlugins:
    def test_happy_path(self, admin_client):
        plugins = [
            {
                "name": "octo",
                "description": "d",
                "version": "1.0",
                "skill_count": 0,
                "id": "octo@mp",
                "installed": False,
            }
        ]
        with patch(
            "src.plugin_service.list_marketplace_plugins",
            return_value=plugins,
        ) as mock_list:
            r = admin_client.get("/admin/api/marketplaces/mp/plugins")
        assert r.status_code == 200
        assert r.json() == {"plugins": plugins}
        mock_list.assert_called_once_with("mp")


# ---------------------------------------------------------------------------
# add_marketplace
# ---------------------------------------------------------------------------


class TestAddMarketplace:
    def test_happy_path(self, admin_client):
        with patch(
            "src.plugin_admin_service.add_marketplace",
            return_value={"status": "added", "repo": "https://x/y.git"},
        ) as mock_add:
            r = admin_client.post(
                "/admin/api/marketplaces",
                json={
                    "repo": "https://x/y.git",
                    "branch": "dev",
                    "scope": "project",
                },
            )
        assert r.status_code == 200
        assert r.json()["status"] == "added"
        mock_add.assert_called_once_with(
            "https://x/y.git", branch="dev", scope="project", git_token=""
        )

    def test_plugin_admin_error_400(self, admin_client):
        with patch(
            "src.plugin_admin_service.add_marketplace",
            side_effect=PluginAdminError("bad repo"),
        ):
            r = admin_client.post(
                "/admin/api/marketplaces", json={"repo": "bogus"}
            )
        assert r.status_code == 400
        assert r.json() == {"error": "bad repo"}


# ---------------------------------------------------------------------------
# remove_marketplace
# ---------------------------------------------------------------------------


class TestRemoveMarketplace:
    def test_happy_path(self, admin_client):
        with patch(
            "src.plugin_admin_service.remove_marketplace",
            return_value={"status": "removed", "marketplace": "mp"},
        ) as mock_rm:
            r = admin_client.delete("/admin/api/marketplaces/mp?scope=local")
        assert r.status_code == 200
        assert r.json()["status"] == "removed"
        mock_rm.assert_called_once_with("mp", scope="local")

    def test_plugin_admin_error_400(self, admin_client):
        with patch(
            "src.plugin_admin_service.remove_marketplace",
            side_effect=PluginAdminError("no such marketplace"),
        ):
            r = admin_client.delete("/admin/api/marketplaces/mp")
        assert r.status_code == 400
        assert r.json() == {"error": "no such marketplace"}


# ---------------------------------------------------------------------------
# refresh_marketplace
# ---------------------------------------------------------------------------


class TestRefreshMarketplace:
    def test_happy_path(self, admin_client):
        with patch(
            "src.plugin_admin_service.refresh_marketplace",
            return_value={"status": "refreshed", "marketplace": "mp"},
        ) as mock_refresh:
            r = admin_client.post(
                "/admin/api/marketplaces/mp/refresh",
                json={"scope": "project", "git_token": "tok"},
            )
        assert r.status_code == 200
        assert r.json()["status"] == "refreshed"
        mock_refresh.assert_called_once_with(
            "mp", scope="project", git_token="tok"
        )

    def test_empty_body_uses_defaults(self, admin_client):
        with patch(
            "src.plugin_admin_service.refresh_marketplace",
            return_value={"status": "refreshed", "marketplace": "mp"},
        ) as mock_refresh:
            r = admin_client.post("/admin/api/marketplaces/mp/refresh")
        assert r.status_code == 200
        mock_refresh.assert_called_once_with("mp", scope="", git_token="")

    def test_plugin_admin_error_400(self, admin_client):
        with patch(
            "src.plugin_admin_service.refresh_marketplace",
            side_effect=PluginAdminError("no repo"),
        ):
            r = admin_client.post("/admin/api/marketplaces/mp/refresh")
        assert r.status_code == 400
        assert r.json() == {"error": "no repo"}


# ---------------------------------------------------------------------------
# install_plugin
# ---------------------------------------------------------------------------


class TestInstallPlugin:
    def test_happy_path(self, admin_client):
        with patch(
            "src.plugin_admin_service.install_plugin",
            return_value={"status": "installed", "spec": "octo@mp"},
        ) as mock_install:
            r = admin_client.post(
                "/admin/api/plugins",
                json={"name": "octo", "marketplace": "mp", "scope": "user"},
            )
        assert r.status_code == 200
        assert r.json()["spec"] == "octo@mp"
        mock_install.assert_called_once_with(
            "octo", marketplace="mp", scope="user", repo="", branch="main"
        )

    def test_plugin_admin_error_400(self, admin_client):
        with patch(
            "src.plugin_admin_service.install_plugin",
            side_effect=PluginAdminError("install failed"),
        ):
            r = admin_client.post("/admin/api/plugins", json={"name": "octo"})
        assert r.status_code == 400
        assert r.json() == {"error": "install failed"}


# ---------------------------------------------------------------------------
# uninstall_plugin (catch-all DELETE)
# ---------------------------------------------------------------------------


class TestUninstallPlugin:
    def test_happy_path(self, admin_client):
        with patch(
            "src.plugin_admin_service.uninstall_plugin",
            return_value={"status": "uninstalled", "plugin": "octo@mp"},
        ) as mock_uninstall:
            r = admin_client.delete("/admin/api/plugins/octo@mp?scope=user")
        assert r.status_code == 200
        assert r.json()["status"] == "uninstalled"
        mock_uninstall.assert_called_once_with("octo@mp", scope="user")

    def test_plugin_admin_error_400(self, admin_client):
        with patch(
            "src.plugin_admin_service.uninstall_plugin",
            side_effect=PluginAdminError("uninstall failed"),
        ):
            r = admin_client.delete("/admin/api/plugins/octo@mp")
        assert r.status_code == 400
        assert r.json() == {"error": "uninstall failed"}
