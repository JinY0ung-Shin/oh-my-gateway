"""Unit tests for src.plugin_admin_service.

The subprocess (``run``) layer and the manifest module are mocked, so no real
``git``/``claude`` process is spawned and no manifest file is written. Tests
assert (a) the exact claude CLI argv per operation, (b) correct manifest
mutations, and (c) that injection inputs raise PluginAdminError before any
subprocess runs.
"""

import subprocess

import pytest

import src.plugin_admin_service as svc

CLAUDE_BIN = "/fake/bin/claude"


@pytest.fixture
def fake_claude(monkeypatch):
    """Pretend the claude CLI is resolvable and executable."""
    monkeypatch.setattr(svc, "resolve_claude_bin", lambda: CLAUDE_BIN)
    monkeypatch.setattr(svc, "_is_executable", lambda p: p == CLAUDE_BIN)
    return CLAUDE_BIN


@pytest.fixture
def recorded_runs(monkeypatch):
    """Capture every svc.run() argv; default to a success CompletedProcess."""
    calls = []

    def fake_run(cmd, *, token=""):
        calls.append({"cmd": list(cmd), "token": token})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svc, "run", fake_run)
    return calls


@pytest.fixture
def fake_manifest(monkeypatch):
    """Replace plugin_manifest calls with recorders."""

    class M:
        def __init__(self):
            self.added_entries = []
            self.add_plugin_calls = []
            self.remove_added_calls = []
            self.mark_removed_calls = []
            self.unmark_removed_calls = []
            self.remove_marketplace_calls = []
            self.marketplaces = {}
            self.set_marketplace_calls = []

        def spec_for(self, name, marketplace):
            return f"{name}@{marketplace}" if marketplace else name

        def list_added(self):
            return self.added_entries

        def set_marketplace(self, name, *, repo, branch, scope):
            rec = {"repo": repo, "branch": branch, "scope": scope}
            self.set_marketplace_calls.append({"name": name, **rec})
            self.marketplaces[name] = rec

        def get_marketplace(self, name):
            return self.marketplaces.get(name, {})

        def list_marketplace_records(self):
            return self.marketplaces

        def add_plugin(self, *, repo, name, marketplace, scope, branch):
            self.add_plugin_calls.append(
                {
                    "repo": repo,
                    "name": name,
                    "marketplace": marketplace,
                    "scope": scope,
                    "branch": branch,
                }
            )

        def remove_added(self, spec, scope):
            self.remove_added_calls.append((spec, scope))

        def mark_removed(self, spec, scope):
            self.mark_removed_calls.append((spec, scope))

        def unmark_removed(self, spec, scope):
            self.unmark_removed_calls.append((spec, scope))

        def remove_marketplace_entries(self, marketplace):
            self.remove_marketplace_calls.append(marketplace)

    m = M()
    monkeypatch.setattr(svc, "plugin_manifest", m)
    return m


# ---------------------------------------------------------------------------
# add_marketplace
# ---------------------------------------------------------------------------


def test_add_marketplace_remote_clones_then_adds(
    monkeypatch, fake_claude, recorded_runs, fake_manifest
):
    monkeypatch.setattr(
        svc, "prepare_repo", lambda repo, **kw: svc.Path("/clones/octo-12345678")
    )
    result = svc.add_marketplace(
        "https://github.com/acme/octo", branch="main", scope="user"
    )

    assert result["status"] == "added"
    assert recorded_runs[-1]["cmd"] == [
        CLAUDE_BIN,
        "plugin",
        "marketplace",
        "add",
        "--scope",
        "user",
        "--",
        "/clones/octo-12345678",
    ]


def test_add_marketplace_local_path_added_in_place(
    monkeypatch, tmp_path, fake_claude, recorded_runs, fake_manifest
):
    local = tmp_path / "mkt"
    local.mkdir()
    result = svc.add_marketplace(str(local), scope="project")

    assert result["status"] == "added"
    # No git clone for a local path: only the marketplace add ran.
    assert all(c["cmd"][:1] != ["git"] for c in recorded_runs)
    assert recorded_runs[-1]["cmd"] == [
        CLAUDE_BIN,
        "plugin",
        "marketplace",
        "add",
        "--scope",
        "project",
        "--",
        str(local),
    ]


def test_add_marketplace_cli_failure_raises(
    monkeypatch, tmp_path, fake_claude, fake_manifest
):
    local = tmp_path / "mkt"
    local.mkdir()

    def boom(cmd, *, token=""):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(svc, "run", boom)
    with pytest.raises(svc.PluginAdminError):
        svc.add_marketplace(str(local))


# ---------------------------------------------------------------------------
# remove_marketplace
# ---------------------------------------------------------------------------


def test_remove_marketplace_argv_and_manifest(
    fake_claude, recorded_runs, fake_manifest
):
    result = svc.remove_marketplace("nyldn-plugins", scope="user")

    assert result["status"] == "removed"
    assert recorded_runs[-1]["cmd"] == [
        CLAUDE_BIN,
        "plugin",
        "marketplace",
        "remove",
        "--scope",
        "user",
        "--",
        "nyldn-plugins",
    ]
    assert fake_manifest.remove_marketplace_calls == ["nyldn-plugins"]


# ---------------------------------------------------------------------------
# install_plugin
# ---------------------------------------------------------------------------


def test_install_plugin_argv_and_manifest(fake_claude, recorded_runs, fake_manifest):
    result = svc.install_plugin("octo", marketplace="nyldn-plugins", scope="user")

    assert result["status"] == "installed"
    assert result["spec"] == "octo@nyldn-plugins"

    install_cmds = [c["cmd"] for c in recorded_runs]
    assert [
        CLAUDE_BIN,
        "plugin",
        "install",
        "--scope",
        "user",
        "--",
        "octo@nyldn-plugins",
    ] in install_cmds
    assert [
        CLAUDE_BIN,
        "plugin",
        "update",
        "--scope",
        "user",
        "--",
        "octo@nyldn-plugins",
    ] in install_cmds

    assert fake_manifest.add_plugin_calls == [
        {
            "repo": "",
            "name": "octo",
            "marketplace": "nyldn-plugins",
            "scope": "user",
            "branch": "main",
        }
    ]


def test_install_plugin_no_marketplace_spec_is_bare_name(
    fake_claude, recorded_runs, fake_manifest
):
    result = svc.install_plugin("octo")
    assert result["spec"] == "octo"
    assert [
        CLAUDE_BIN,
        "plugin",
        "install",
        "--scope",
        "user",
        "--",
        "octo",
    ] in [c["cmd"] for c in recorded_runs]


def test_install_plugin_update_failure_is_nonfatal(
    monkeypatch, fake_claude, fake_manifest
):
    def fake_run(cmd, *, token=""):
        if "update" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(svc, "run", fake_run)
    result = svc.install_plugin("octo", marketplace="m")
    assert result["status"] == "installed"
    assert fake_manifest.add_plugin_calls  # still recorded


def test_install_plugin_with_repo_adds_marketplace_first(
    monkeypatch, fake_claude, recorded_runs, fake_manifest
):
    monkeypatch.setattr(
        svc, "prepare_repo", lambda repo, **kw: svc.Path("/clones/octo-deadbeef")
    )
    svc.install_plugin("octo", marketplace="octo", repo="https://github.com/acme/octo")
    cmds = [c["cmd"] for c in recorded_runs]
    # marketplace add came before install
    add_idx = cmds.index(
        [
            CLAUDE_BIN,
            "plugin",
            "marketplace",
            "add",
            "--scope",
            "user",
            "--",
            "/clones/octo-deadbeef",
        ]
    )
    install_idx = cmds.index(
        [CLAUDE_BIN, "plugin", "install", "--scope", "user", "--", "octo@octo"]
    )
    assert add_idx < install_idx


def test_install_from_catalog_resolves_marketplace_repo(
    monkeypatch, tmp_path, fake_claude, recorded_runs, fake_manifest
):
    # A catalog install passes only a marketplace name (repo=""). The service
    # must resolve the marketplace's repo from known_marketplaces.json so the
    # manifest entry carries a replayable repo (the startup installer skips
    # entries with no repo).
    import json as _json

    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "known_marketplaces.json").write_text(
        _json.dumps(
            {
                "nyldn-plugins": {
                    "source": {"source": "github", "repo": "acme/nyldn"},
                    "installLocation": str(plugins_dir / "marketplaces" / "nyldn"),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    svc.install_plugin("octo", marketplace="nyldn-plugins", scope="user")

    assert fake_manifest.add_plugin_calls == [
        {
            "repo": "https://github.com/acme/nyldn.git",
            "name": "octo",
            "marketplace": "nyldn-plugins",
            "scope": "user",
            "branch": "main",
        }
    ]


def test_catalog_install_after_add_marketplace_replays_remote(
    monkeypatch, fake_claude, recorded_runs, fake_manifest
):
    # The REAL admin flow: add a remote marketplace (clone-then-add-local, which
    # makes claude record source: directory, losing the remote), then install a
    # plugin from its catalog. The manifest added-entry must carry the ORIGINAL
    # REMOTE + branch (from the marketplace record), not the local clone path, so
    # the startup installer can re-clone it after a `docker compose down -v`.
    monkeypatch.setattr(
        svc, "prepare_repo", lambda repo, **kw: svc.Path("/clones/octo-mkt-x")
    )
    svc.add_marketplace(
        "https://github.com/acme/octo-mkt", branch="dev", scope="project"
    )
    # name falls back to the repo basename (the fake clone has no marketplace.json)
    assert fake_manifest.marketplaces["octo-mkt"] == {
        "repo": "https://github.com/acme/octo-mkt",
        "branch": "dev",
        "scope": "project",
    }

    svc.install_plugin("octo", marketplace="octo-mkt", scope="project")
    assert fake_manifest.add_plugin_calls[-1] == {
        "repo": "https://github.com/acme/octo-mkt",
        "name": "octo",
        "marketplace": "octo-mkt",
        "scope": "project",
        "branch": "dev",
    }


def test_resolve_marketplace_repo_prefers_full_url(monkeypatch, tmp_path):
    import json as _json

    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "known_marketplaces.json").write_text(
        _json.dumps(
            {"m": {"source": {"source": "git", "url": "https://git.example/m.git"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(tmp_path / "manifest.json"))
    assert svc._resolve_marketplace_repo("m") == "https://git.example/m.git"


def test_resolve_marketplace_repo_unknown_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(tmp_path / "manifest.json"))
    assert svc._resolve_marketplace_repo("nope") == ""


def test_strip_url_credentials():
    f = svc._strip_url_credentials
    # http(s) userinfo (user:token and token-only) is removed
    assert f("https://user:tok@host/org/r.git") == "https://host/org/r.git"
    assert f("https://tok@host/org/r.git") == "https://host/org/r.git"
    assert f("http://u:p@host:8443/r") == "http://host:8443/r"
    # clean / non-http(s) values are untouched (ssh user is not a secret)
    assert f("https://host/org/r.git") == "https://host/org/r.git"
    assert f("ssh://git@host/org/r.git") == "ssh://git@host/org/r.git"
    assert f("git@host:org/r.git") == "git@host:org/r.git"
    assert f("/clones/local-mkt") == "/clones/local-mkt"
    assert f("") == ""


def test_resolve_marketplace_repo_strips_journal_credentials(monkeypatch, tmp_path):
    from src import plugin_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(tmp_path / "manifest.json"))
    monkeypatch.setattr(
        plugin_service,
        "env_bootstrap_records",
        lambda: {"m": {"repo": "https://user:tok@host/o/r.git"}},
    )
    assert svc._resolve_marketplace_repo("m") == "https://host/o/r.git"


def test_resolve_marketplace_repo_strips_known_marketplaces_credentials(
    monkeypatch, tmp_path
):
    import json as _json

    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "known_marketplaces.json").write_text(
        _json.dumps(
            {"m": {"source": {"source": "git", "url": "https://t@git.example/m.git"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(tmp_path / "manifest.json"))
    assert svc._resolve_marketplace_repo("m") == "https://git.example/m.git"


def test_resolve_marketplace_repo_falls_back_to_env_journal(monkeypatch, tmp_path):
    # An env-bootstrapped marketplace (clone-then-add records source: directory)
    # has no remote in known_marketplaces.json; the startup installer's journal
    # supplies it so the catalog-install manifest entry stays replayable.
    from src import plugin_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(tmp_path / "manifest.json"))
    monkeypatch.setattr(
        plugin_service,
        "env_bootstrap_records",
        lambda: {"envmkt": {"scope": "project", "branch": "dev", "repo": "o/r.git"}},
    )
    assert svc._resolve_marketplace_repo("envmkt") == "o/r.git"


def test_catalog_install_from_env_marketplace_replays_branch_and_repo(
    monkeypatch, fake_claude, recorded_runs, fake_manifest
):
    # Catalog install from an env-bootstrapped (non-main) marketplace must record
    # the journaled branch + remote, not the main/local defaults, so a `down -v`
    # self-heal clones the same content.
    from src import plugin_service

    monkeypatch.setattr(
        plugin_service,
        "env_bootstrap_records",
        lambda: {
            "envmkt": {
                "scope": "project",
                "branch": "develop",
                "repo": "https://host/o/r.git",
            }
        },
    )
    svc.install_plugin("octo", marketplace="envmkt", scope="project")
    assert fake_manifest.add_plugin_calls[-1] == {
        "repo": "https://host/o/r.git",
        "name": "octo",
        "marketplace": "envmkt",
        "scope": "project",
        "branch": "develop",
    }


def test_install_plugin_cli_failure_raises(monkeypatch, fake_claude, fake_manifest):
    def boom(cmd, *, token=""):
        raise subprocess.CalledProcessError(2, cmd)

    monkeypatch.setattr(svc, "run", boom)
    with pytest.raises(svc.PluginAdminError):
        svc.install_plugin("octo", marketplace="m")
    assert fake_manifest.add_plugin_calls == []


# ---------------------------------------------------------------------------
# uninstall_plugin
# ---------------------------------------------------------------------------


def test_uninstall_managed_plugin_removes_added(
    fake_claude, recorded_runs, fake_manifest
):
    fake_manifest.added_entries = [
        {"name": "octo", "marketplace": "nyldn-plugins", "scope": "user"}
    ]
    result = svc.uninstall_plugin("octo@nyldn-plugins", scope="user")

    assert result["status"] == "uninstalled"
    assert recorded_runs[-1]["cmd"] == [
        CLAUDE_BIN,
        "plugin",
        "uninstall",
        "--scope",
        "user",
        "--",
        "octo@nyldn-plugins",
    ]
    # uninstall always drops any managed entry AND records the removal, so an
    # env-bootstrap declaration of the same plugin can't resurrect it on restart.
    assert fake_manifest.remove_added_calls == [("octo@nyldn-plugins", "user")]
    assert fake_manifest.mark_removed_calls == [("octo@nyldn-plugins", "user")]


def test_uninstall_env_plugin_marks_removed(fake_claude, recorded_runs, fake_manifest):
    # Not present in manifest.added -> remove_added is a harmless no-op, but the
    # removal is still recorded so startup skips/uninstalls it.
    fake_manifest.added_entries = []
    result = svc.uninstall_plugin("telegram@other-mkt")

    assert result["status"] == "uninstalled"
    assert fake_manifest.mark_removed_calls == [("telegram@other-mkt", "user")]
    assert fake_manifest.remove_added_calls == [("telegram@other-mkt", "user")]


def test_uninstall_records_at_requested_scope(
    fake_claude, recorded_runs, fake_manifest
):
    # scope is part of the identity: uninstall targets and records the requested
    # scope, leaving any other-scope install of the same plugin untouched.
    fake_manifest.added_entries = [
        {"name": "octo", "marketplace": "m", "scope": "project"}
    ]
    svc.uninstall_plugin("octo@m", scope="user")
    assert recorded_runs[-1]["cmd"][-4:] == ["--scope", "user", "--", "octo@m"]
    assert fake_manifest.remove_added_calls == [("octo@m", "user")]
    assert fake_manifest.mark_removed_calls == [("octo@m", "user")]

    svc.uninstall_plugin("octo@m", scope="project")
    assert ("octo@m", "project") in fake_manifest.remove_added_calls
    assert ("octo@m", "project") in fake_manifest.mark_removed_calls


def test_uninstall_cli_failure_raises(monkeypatch, fake_claude, fake_manifest):
    def boom(cmd, *, token=""):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(svc, "run", boom)
    with pytest.raises(svc.PluginAdminError):
        svc.uninstall_plugin("octo@m")
    assert fake_manifest.mark_removed_calls == []
    assert fake_manifest.remove_added_calls == []


# ---------------------------------------------------------------------------
# Input validation — injection inputs must raise and run NO subprocess
# ---------------------------------------------------------------------------


INJECTION = ["x; rm -rf /", "a b", "$(whoami)", "a|b", "a`b`", "a&&b", "a>b"]


@pytest.mark.parametrize("bad", INJECTION)
def test_install_plugin_rejects_injection_name(
    monkeypatch, bad, fake_claude, fake_manifest
):
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.install_plugin(bad, marketplace="m")
    assert calls == []
    assert fake_manifest.add_plugin_calls == []


@pytest.mark.parametrize("bad", INJECTION)
def test_install_plugin_rejects_injection_marketplace(
    monkeypatch, bad, fake_claude, fake_manifest
):
    calls = []
    monkeypatch.setattr(
        svc, "run", lambda cmd, **kw: calls.append(cmd)
    )  # pragma: no cover
    with pytest.raises(svc.PluginAdminError):
        svc.install_plugin("octo", marketplace=bad)
    assert calls == []


@pytest.mark.parametrize("bad", INJECTION)
def test_add_marketplace_rejects_injection_repo(
    monkeypatch, bad, fake_claude, fake_manifest
):
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.add_marketplace(bad)
    assert calls == []


@pytest.mark.parametrize(
    "bad",
    [
        "https://user:token@host/o/r.git",
        "https://ghp_secrettoken@github.com/o/r.git",
        "http://x:y@host/o/r.git",
    ],
)
def test_add_marketplace_rejects_credential_url(
    monkeypatch, bad, fake_claude, fake_manifest
):
    # Credentials in the URL would leak into the manifest/response/logs; reject
    # before any subprocess and steer the caller to the git_token field.
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.add_marketplace(bad)
    assert calls == []
    assert fake_manifest.set_marketplace_calls == []


def test_remove_marketplace_best_effort_scope(monkeypatch, fake_claude, fake_manifest):
    # An env-added project marketplace has no manifest scope record, so the UI
    # sends user; removal falls back across scopes and succeeds at project.
    def fake_run(cmd, *, token=""):
        if "--scope" in cmd and cmd[cmd.index("--scope") + 1] == "user":
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(svc, "run", fake_run)
    result = svc.remove_marketplace("envmkt", scope="user")
    assert result["scope"] == "project"
    assert fake_manifest.remove_marketplace_calls == ["envmkt"]


@pytest.mark.parametrize("bad", INJECTION)
def test_uninstall_rejects_injection(monkeypatch, bad, fake_claude, fake_manifest):
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.uninstall_plugin(bad)
    assert calls == []
    assert fake_manifest.mark_removed_calls == []


@pytest.mark.parametrize("bad", INJECTION)
def test_remove_marketplace_rejects_injection(
    monkeypatch, bad, fake_claude, fake_manifest
):
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.remove_marketplace(bad)
    assert calls == []
    assert fake_manifest.remove_marketplace_calls == []


def test_invalid_scope_rejected(monkeypatch, fake_claude, fake_manifest):
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.install_plugin("octo", scope="root")
    assert calls == []


def test_missing_claude_bin_raises(monkeypatch, recorded_runs, fake_manifest):
    monkeypatch.setattr(svc, "resolve_claude_bin", lambda: None)
    with pytest.raises(svc.PluginAdminError):
        svc.install_plugin("octo", marketplace="m")
    # validation passed, but bin resolution failed before any subprocess
    assert recorded_runs == []


# ---------------------------------------------------------------------------
# Option-injection hardening: leading-dash names/branches and the `--`
# end-of-options separator at every user-controlled positional arg site.
# ---------------------------------------------------------------------------


LEADING_DASH = ["-h", "--help", "-rf", "--scope", "-x"]


@pytest.mark.parametrize("bad", LEADING_DASH)
def test_validate_name_rejects_leading_dash(bad):
    # A leading-dash name would otherwise be parsed as a CLI option, not a
    # positional, by the claude/git subcommand it is forwarded to.
    with pytest.raises(svc.PluginAdminError):
        svc._validate_name(bad, what="plugin name")


@pytest.mark.parametrize("bad", LEADING_DASH)
def test_validate_branch_rejects_leading_dash(bad):
    with pytest.raises(svc.PluginAdminError):
        svc._validate_branch(bad)


def test_validate_name_accepts_legitimate_values():
    for ok in ["octo", "octo@nyldn-plugins", "a.b_c", "org/repo", "x-y-z"]:
        assert svc._validate_name(ok, what="plugin name") == ok


def test_validate_branch_accepts_legitimate_values():
    for ok in ["main", "release/1.0", "feat-x", "v2.3.4"]:
        assert svc._validate_branch(ok) == ok


@pytest.mark.parametrize("bad", LEADING_DASH)
def test_install_plugin_rejects_leading_dash_name(
    monkeypatch, bad, fake_claude, fake_manifest
):
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.install_plugin(bad, marketplace="m")
    assert calls == []
    assert fake_manifest.add_plugin_calls == []


@pytest.mark.parametrize("bad", LEADING_DASH)
def test_uninstall_plugin_rejects_leading_dash_id(
    monkeypatch, bad, fake_claude, fake_manifest
):
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.uninstall_plugin(bad)
    assert calls == []
    assert fake_manifest.mark_removed_calls == []


@pytest.mark.parametrize("bad", LEADING_DASH)
def test_remove_marketplace_rejects_leading_dash_name(
    monkeypatch, bad, fake_claude, fake_manifest
):
    calls = []
    monkeypatch.setattr(svc, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(svc.PluginAdminError):
        svc.remove_marketplace(bad)
    assert calls == []
    assert fake_manifest.remove_marketplace_calls == []


def _positional_after_separator(cmd):
    """Return the positional args that follow the `--` end-of-options marker."""
    assert "--" in cmd, f"missing `--` end-of-options separator in {cmd!r}"
    return cmd[cmd.index("--") + 1 :]


def test_install_argv_passes_spec_after_options_separator(
    fake_claude, recorded_runs, fake_manifest
):
    # Layered defense: even if validation were bypassed, the user-controlled spec
    # sits after `--`, so it can never be parsed as an option.
    svc.install_plugin("octo", marketplace="nyldn-plugins")
    install = next(
        c["cmd"]
        for c in recorded_runs
        if c["cmd"][:3] == [CLAUDE_BIN, "plugin", "install"]
    )
    assert _positional_after_separator(install) == ["octo@nyldn-plugins"]


def test_uninstall_argv_passes_spec_after_options_separator(
    fake_claude, recorded_runs, fake_manifest
):
    fake_manifest.added_entries = []
    svc.uninstall_plugin("octo@m")
    assert _positional_after_separator(recorded_runs[-1]["cmd"]) == ["octo@m"]


def test_remove_marketplace_argv_passes_name_after_options_separator(
    fake_claude, recorded_runs, fake_manifest
):
    svc.remove_marketplace("nyldn-plugins")
    assert _positional_after_separator(recorded_runs[-1]["cmd"]) == ["nyldn-plugins"]
