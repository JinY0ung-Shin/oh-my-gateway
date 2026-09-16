from __future__ import annotations

import pytest

from src.runtime_config import get_workspace_quota_bytes, runtime_config
from src.workspace_quota import (
    WorkspaceQuotaConfigError,
    workspace_quota_env_limit_bytes,
    workspace_quota_limit_bytes,
)

_MIB = 1024 * 1024
_KEY = "workspace_quota_bytes"


@pytest.fixture(autouse=True)
def clean_quota_override():
    runtime_config.reset(_KEY)
    yield
    runtime_config.reset(_KEY)


def test_env_value_seeds_runtime_quota_in_bytes(monkeypatch):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "500")

    assert workspace_quota_env_limit_bytes() == 500 * _MIB
    assert get_workspace_quota_bytes() == 500 * _MIB
    assert workspace_quota_limit_bytes() == 500 * _MIB


def test_runtime_override_takes_effect_immediately(monkeypatch):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "500")
    runtime_config.set(_KEY, 2 * 1024 * _MIB)

    assert workspace_quota_limit_bytes() == 2 * 1024 * _MIB
    meta = runtime_config.get_all()[_KEY]
    assert meta["value"] == 2 * 1024 * _MIB
    assert meta["original"] == 500 * _MIB
    assert meta["overridden"] is True


def test_reset_returns_to_environment_baseline(monkeypatch):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "750")
    runtime_config.set(_KEY, 4 * 1024 * _MIB)

    runtime_config.reset(_KEY)

    assert workspace_quota_limit_bytes() == 750 * _MIB
    assert runtime_config.is_overridden(_KEY) is False


def test_zero_runtime_quota_means_unlimited(monkeypatch):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "500")
    runtime_config.set(_KEY, 0)

    assert workspace_quota_limit_bytes() == 0


def test_negative_runtime_quota_is_rejected():
    with pytest.raises(ValueError, match="workspace_quota_bytes must be >= 0"):
        runtime_config.set(_KEY, -1)


@pytest.mark.parametrize("value", ["-1", "nope", "1.5"])
def test_invalid_environment_remains_a_configuration_error(monkeypatch, value):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", value)

    with pytest.raises(WorkspaceQuotaConfigError):
        workspace_quota_limit_bytes()
