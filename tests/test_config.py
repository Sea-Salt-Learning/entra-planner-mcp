from __future__ import annotations

import pytest
from conftest import settings

from entra_planner_mcp.config import READ_TOOLS, ConfigError


def test_default_tools_are_read_only():
    s = settings(ALLOWED_TOOLS=None)
    assert s.allowed_tools == READ_TOOLS


@pytest.mark.parametrize(
    "env",
    [
        {"ENTRA_TENANT_ID": None},
        {"ENTRA_TENANT_ID": "contoso.example"},
        {"ENTRA_API_CLIENT_ID": "abc"},
        {"PUBLIC_BASE_URL": "http://planner.contoso.example/mcp"},
        {"PUBLIC_BASE_URL": "https://planner.contoso.example/mcp/"},
        {"PUBLIC_BASE_URL": "https://planner.contoso.example"},
        {"ALLOWED_TOOLS": "planner_delete_task"},
        {"OBO_CREDENTIAL": "local_dev_secret", "LOCAL_DEV_CLIENT_SECRET": "x"},
        {"OBO_CREDENTIAL": "password"},
        {"LOG_LEVEL": "DEBUG"},
        {"RATE_LIMIT_PER_MIN": "0"},
        {"ENTRA_ALLOWED_CLIENT_IDS": "not-a-guid"},
    ],
)
def test_invalid_config_refused(env):
    with pytest.raises(ConfigError):
        settings(**env)


def test_local_dev_secret_allowed_outside_production():
    s = settings(
        OBO_CREDENTIAL="local_dev_secret",
        LOCAL_DEV_CLIENT_SECRET="dev-only-value-123",
        ENV="development",
        PUBLIC_BASE_URL="http://localhost:8000/mcp",
        LOG_LEVEL="DEBUG",
    )
    assert s.obo_credential == "local_dev_secret"
    assert "dev-only-value-123" not in repr(s)


def test_required_role_validated():
    assert settings(ENTRA_REQUIRED_ROLE="Planner.User").required_role == "Planner.User"
    assert settings().required_role is None
    with pytest.raises(ConfigError):
        settings(ENTRA_REQUIRED_ROLE="bad role!")
