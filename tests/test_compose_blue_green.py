"""docker-compose.yml + docker/nginx/gateway.conf — the blue/green layout's invariants.

docs/zero-downtime-deploy.md §3: two colors of one gateway behind an nginx proxy that alone holds
the host port. Pinned here against the files themselves (PyYAML resolves the `<<` merge keys the
same way Compose does); when the `docker compose` CLI is installed, the rendered config is checked
too — no daemon is needed for `config`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
NGINX = ROOT / "docker" / "nginx" / "gateway.conf"
COLORS = ("gateway-blue", "gateway-green")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _default(value: str) -> str:
    """`${VAR:-default}` → default (what an operator gets without overriding it)."""
    m = re.fullmatch(r"\$\{[A-Z0-9_]+:-([^}]*)\}", str(value))
    assert m, value
    return m.group(1)


def test_the_single_gateway_service_is_split_into_two_colors_and_a_proxy():
    services = _compose()["services"]
    assert "gateway" not in services
    assert {"gateway-blue", "gateway-green", "gateway-proxy"} <= set(services)
    assert services["gateway-blue"]["profiles"] == ["blue"]
    assert services["gateway-green"]["profiles"] == ["green"]
    # The proxy has no profile: a bare `docker compose up -d` brings it up and never a color.
    assert "profiles" not in services["gateway-proxy"]


def test_only_the_proxy_publishes_the_gateway_port():
    services = _compose()["services"]
    assert services["gateway-proxy"]["ports"] == ["${PORT:-8000}:${PORT:-8000}"]
    for name in COLORS:
        assert "ports" not in services[name], name
    published = {
        name for name, svc in services.items() if svc.get("ports") and name != "gateway-log"
    }
    assert published == {"gateway-proxy"}


def test_colors_differ_only_in_profile_image_and_instance():
    services = _compose()["services"]
    blue, green = (dict(services[c]) for c in COLORS)
    assert blue["environment"]["GATEWAY_INSTANCE"] == "blue"
    assert green["environment"]["GATEWAY_INSTANCE"] == "green"
    assert blue["image"].endswith(":blue") and green["image"].endswith(":green")
    assert blue["image"].rsplit(":", 1)[0] == green["image"].rsplit(":", 1)[0]
    for svc in (blue, green):
        svc.pop("profiles")
        svc.pop("image")
        svc["environment"] = dict(svc["environment"])
        svc["environment"].pop("GATEWAY_INSTANCE")
    assert blue == green


def test_both_colors_share_state_volumes_and_env():
    services = _compose()["services"]
    for name in COLORS:
        svc = services[name]
        volumes = svc["volumes"]
        assert "claude_home:/home/app/.claude" in volumes
        assert "./data:/app/data" in volumes
        assert "${USER_WORKSPACES_HOST_DIR:-./workspaces}:/data/workspaces" in volumes
        assert "${PLUGIN_SOURCES_HOST_DIR:-./plugins}:/data/plugins:ro" in volumes
        assert svc["env_file"] == [".env"]
        assert svc["environment"]["ADMIN_API_KEY"].startswith("${ADMIN_API_KEY:?")
        assert svc["stop_grace_period"] == "60s"
        assert svc["hostname"] == "${GATEWAY_CONTAINER_HOSTNAME:-agent-sandbox}"
        assert svc["depends_on"]["gateway-log"]["required"] is False
        assert "gateway_build_install_script" in svc["build"]["secrets"]


def test_claude_home_has_a_fixed_name_that_defaults_to_the_old_project_scoped_one():
    volume = _compose()["volumes"]["claude_home"]
    # Before the pin Compose named it <project>_claude_home; COMPOSE_PROJECT_NAME is the
    # effective project name during interpolation, so existing deployments keep their data.
    assert volume["name"] == "${GATEWAY_CLAUDE_HOME_VOLUME:-${COMPOSE_PROJECT_NAME}_claude_home}"


def test_the_mcp_relay_base_url_stays_unset():
    assert "MCP_RELAY_BASE_URL:" not in COMPOSE.read_text().replace(
        "# MCP_RELAY_BASE_URL", ""
    )
    for name in COLORS:
        assert "MCP_RELAY_BASE_URL" not in _compose()["services"][name]["environment"]


def test_colors_trust_forwarded_headers_from_the_proxy_address_only():
    doc = _compose()
    proxy_ip = _default(doc["services"]["gateway-proxy"]["networks"]["edge"]["ipv4_address"])
    subnet = _default(doc["networks"]["edge"]["ipam"]["config"][0]["subnet"])
    import ipaddress

    net = ipaddress.ip_network(subnet)
    assert ipaddress.ip_address(proxy_ip) in net
    assert ipaddress.ip_address(proxy_ip) != next(net.hosts())  # not the bridge gateway
    for name in COLORS:
        svc = doc["services"][name]
        assert _default(svc["environment"]["FORWARDED_ALLOW_IPS"]) == proxy_ip
        assert set(svc["networks"]) == {"default", "edge"}
    assert list(doc["services"]["gateway-proxy"]["networks"]) == ["edge"]


def test_the_proxy_mounts_the_template_and_the_state_directory():
    proxy = _compose()["services"]["gateway-proxy"]
    assert (
        "./docker/nginx/gateway.conf:/etc/nginx/templates/default.conf.template:ro"
        in proxy["volumes"]
    )
    # A directory, not the file: a bind-mounted file never sees the replaced active.conf.
    assert "./docker/nginx/state:/etc/nginx/gateway-state:ro" in proxy["volumes"]
    assert proxy["environment"]["GATEWAY_PORT"] == "${PORT:-8000}"
    assert proxy["environment"]["NGINX_ENVSUBST_FILTER"] == "^GATEWAY_"


def test_the_state_directory_is_runtime_only():
    ignore = (ROOT / "docker" / "nginx" / "state" / ".gitignore").read_text().splitlines()
    assert "*" in ignore and "!.gitignore" in ignore


# ── nginx ──


def _nginx() -> str:
    return NGINX.read_text()


def _block(text: str, header: str) -> str:
    start = text.index(header)
    return text[start : text.index("\n    }", start)]


def test_nginx_blocks_internal_and_drain_paths_from_outside():
    text = _nginx()
    assert "return 404;" in _block(text, "location ^~ /internal/ {")
    assert "return 404;" in _block(text, "location = /internal {")
    assert "return 404;" in _block(text, "location = /health/drain {")
    assert "return 404;" in _block(text, "location ^~ /health/drain/ {")


def test_nginx_streams_and_never_limits_or_times_out_first():
    text = _nginx()
    proxy = _block(text, "location / {")
    for directive in (
        "proxy_buffering off;",
        "proxy_request_buffering off;",
        "proxy_read_timeout 3600s;",
        "proxy_send_timeout 3600s;",
        "proxy_http_version 1.1;",
        "proxy_set_header Host $http_host;",
        # Overwritten, never appended: the client cannot choose its own address.
        "proxy_set_header X-Forwarded-For $remote_addr;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
    ):
        assert directive in proxy, directive
    assert "$proxy_add_x_forwarded_for" not in text
    # #266 review: a client-sent X-Forwarded-Proto must never reach the colors,
    # which trust this proxy's forwarding headers.
    assert "$http_x_forwarded_proto" not in text
    assert "client_max_body_size 0;" in text


def test_nginx_resolves_the_active_color_per_request():
    text = _nginx()
    assert "resolver 127.0.0.11" in text
    assert "include /etc/nginx/gateway-state/*.conf;" in text
    assert "proxy_pass http://$gateway_upstream:${GATEWAY_PORT};" in text
    assert "listen ${GATEWAY_PORT};" in text
    # The only template names are GATEWAY_*; nginx's own variables are lowercase.
    names = set(re.findall(r"\$\{([A-Za-z0-9_]+)\}", text))
    assert names == {"GATEWAY_PORT"}


def test_deploy_script_writes_what_nginx_includes():
    script = (ROOT / "scripts" / "deploy.sh").read_text()
    assert "printf 'set $gateway_upstream %s;\\n'" in script
    assert "STATE_DIR=docker/nginx/state" in script


# ── rendered by the real Compose CLI, when it is installed ──


def _compose_cli() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, timeout=30
    )
    return proc.returncode == 0


@pytest.fixture
def rendered(tmp_path):
    if not _compose_cli():
        pytest.skip("docker compose CLI not installed")
    project = tmp_path / "some-checkout"
    (project / "docker").mkdir(parents=True)
    for name in ("docker-compose.yml", "docker-compose.uid.yml"):
        shutil.copy(ROOT / name, project / name)
    shutil.copy(ROOT / "docker" / "noop_build_install.sh", project / "docker")
    (project / ".env").write_text("ADMIN_API_KEY=x\nPORT=9123\n")

    def render(*args: str) -> dict:
        proc = subprocess.run(
            ["docker", "compose", *args, "config", "--format", "json"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        )
        assert proc.returncode == 0, proc.stderr
        import json

        return json.loads(proc.stdout)

    return render


def test_compose_renders_both_colors_on_one_volume(rendered):
    doc = rendered("--profile", "blue", "--profile", "green")
    assert doc["volumes"]["claude_home"]["name"] == "some-checkout_claude_home"
    for name in COLORS:
        env = doc["services"][name]["environment"]
        assert env["GATEWAY_INSTANCE"] == name.split("-")[1]
        assert "MCP_RELAY_BASE_URL" not in env
        assert "ports" not in doc["services"][name]
    proxy = doc["services"]["gateway-proxy"]
    assert proxy["environment"]["GATEWAY_PORT"] == "9123"
    assert [(p["published"], p["target"]) for p in proxy["ports"]] == [("9123", 9123)]


def test_compose_project_name_flows_into_the_volume_name(rendered):
    doc = rendered("-p", "renamed", "--profile", "blue")
    assert doc["volumes"]["claude_home"]["name"] == "renamed_claude_home"


def test_a_bare_up_starts_only_the_proxy(rendered):
    assert set(rendered()["services"]) == {"gateway-proxy"}


def test_uid_overlay_applies_to_both_colors(rendered):
    doc = rendered(
        "-f", "docker-compose.yml", "-f", "docker-compose.uid.yml",
        "--profile", "blue", "--profile", "green",
    )  # fmt: skip
    for name in COLORS:
        env = doc["services"][name]["environment"]
        assert env["CLAUDE_ISOLATION_MODE"] == "uid"
        assert env["GATEWAY_INSTANCE"] == name.split("-")[1]
        targets = {v["target"] for v in doc["services"][name]["volumes"]}
        assert {"/data/claude", "/home/app/.claude", "/app/data"} <= targets
