"""Docker packaging expectations."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_installs_opencode_binary_for_managed_mode():
    """The gateway image should be able to start managed OpenCode itself."""
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "ARG OPENCODE_VERSION=" in dockerfile
    assert "https://opencode.ai/install" in dockerfile
    assert "--version ${OPENCODE_VERSION}" in dockerfile
    assert "/root/.opencode/bin" in dockerfile
    assert "opencode --version" in dockerfile


def test_compose_does_not_configure_external_opencode_server():
    """Compose should not point the gateway at a separate OpenCode service."""
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "OPENCODE_BASE_URL" not in compose
