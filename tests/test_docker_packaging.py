"""Docker packaging expectations."""

import os
from pathlib import Path
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]

APT_SOURCES = textwrap.dedent(
    """\
    Types: deb
    URIs: http://deb.debian.org/debian
    Suites: trixie trixie-updates
    Components: main

    Types: deb
    URIs: http://deb.debian.org/debian-security
    Suites: trixie-security
    Components: main
    """
)


def _final_docker_stage(dockerfile: str) -> str:
    return "FROM " + dockerfile.rsplit("\nFROM ", 1)[1]


def _rewrite_apt_sources(tmp_path: Path, **env_overrides: str) -> str:
    sources = tmp_path / "debian.sources"
    sources.write_text(APT_SOURCES)
    env = {
        **os.environ,
        "APT_SOURCES_FILE": str(sources),
        **env_overrides,
    }

    subprocess.run(
        ["sh", str(ROOT / "docker" / "apt_mirror_sources.sh")],
        check=True,
        env=env,
    )

    return sources.read_text()


def test_dockerfile_installs_opencode_binary_for_managed_mode():
    """The gateway image should install managed OpenCode through npm."""
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "FROM python:3.12-slim-trixie AS opencode-builder" in dockerfile
    assert "ARG OPENCODE_VERSION=" in dockerfile
    assert "ARG NPM_CONFIG_REGISTRY=" in dockerfile
    assert "nodejs npm" in dockerfile
    assert "npm install -g" in dockerfile
    assert '"opencode-ai@${OPENCODE_VERSION}"' in dockerfile
    assert "install -m 0755" in dockerfile
    assert (
        "COPY --from=opencode-builder /usr/local/bin/opencode /usr/local/bin/opencode" in dockerfile
    )
    assert "https://opencode.ai/install" not in dockerfile
    assert "opencode --version" in dockerfile


def test_final_docker_stage_does_not_keep_node_or_npm():
    """The runtime image should keep the OpenCode binary, not npm tooling."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    final_stage = _final_docker_stage(dockerfile)

    assert "nodejs npm" not in final_stage
    assert "npm install" not in final_stage
    assert (
        "COPY --from=opencode-builder /usr/local/bin/opencode /usr/local/bin/opencode"
        in final_stage
    )


def test_dockerfile_allows_apt_mirror_override():
    """Corporate builds should be able to replace Debian apt sources."""
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "ARG APT_MIRROR_URL=" in dockerfile
    assert "ARG APT_SECURITY_MIRROR_URL=" in dockerfile
    assert "docker/apt_mirror_sources.sh" in dockerfile


def test_apt_mirror_rewrite_uses_security_mirror_without_main_mirror(tmp_path):
    """APT_SECURITY_MIRROR_URL should work even when APT_MIRROR_URL is unset."""
    updated = _rewrite_apt_sources(
        tmp_path,
        APT_MIRROR_URL="",
        APT_SECURITY_MIRROR_URL="http://apt.example.com/debian-security",
    )

    assert "URIs: http://deb.debian.org/debian\n" in updated
    assert "URIs: http://apt.example.com/debian-security\n" in updated


def test_apt_mirror_rewrite_uses_main_mirror_for_security_by_default(tmp_path):
    """APT_MIRROR_URL alone should point both Debian sources at the same mirror."""
    updated = _rewrite_apt_sources(
        tmp_path,
        APT_MIRROR_URL="http://apt.example.com/debian",
        APT_SECURITY_MIRROR_URL="",
    )

    assert updated.count("URIs: http://apt.example.com/debian\n") == 2


def test_docker_build_docs_match_pinned_debian_suite():
    """Mirror docs should match the Debian suite used by the pinned base image."""
    env_example = (ROOT / ".env.example").read_text()
    build_options = env_example.split("# Docker Build-Time Options", 1)[1].split(
        "# ---------------------------------------------------------------------------",
        1,
    )[0]

    assert "bookworm" not in build_options
    assert "trixie" in build_options
    assert "OPENCODE_VERSION" in build_options
    assert "host environment" in build_options


def test_compose_forwards_corporate_build_mirror_args():
    """Compose should pass build-time mirrors from the host environment."""
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "- APT_MIRROR_URL" in compose
    assert "- APT_SECURITY_MIRROR_URL" in compose
    assert "- NPM_CONFIG_REGISTRY" in compose
    assert "- OPENCODE_VERSION" in compose
    assert "- PIP_INDEX_URL" in compose
    assert "- PIP_EXTRA_INDEX_URL" in compose


def test_compose_mounts_host_ca_bundle_with_debian_default_path():
    """Compose should let git/curl use the host CA bundle by default."""
    compose = (ROOT / "docker-compose.yml").read_text()
    env_example = (ROOT / ".env.example").read_text()

    expected = (
        "${HOST_CA_CERTIFICATES_PATH:-/etc/ssl/certs/ca-certificates.crt}:"
        "/etc/ssl/certs/ca-certificates.crt:ro"
    )

    assert expected in compose
    assert "HOST_CA_CERTIFICATES_PATH=/etc/ssl/certs/ca-certificates.crt" in env_example


def test_compose_defaults_claude_setting_sources_to_user_scope():
    """Docker should read user-scope Claude config so installed plugins load."""
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "CLAUDE_SETTING_SOURCES=${CLAUDE_SETTING_SOURCES:-user,project,local}" in compose


def test_compose_does_not_configure_external_opencode_server():
    """Compose should not point the gateway at a separate OpenCode service."""
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "OPENCODE_BASE_URL" not in compose
