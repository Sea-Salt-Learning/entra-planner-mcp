"""Configuration, read from the environment only.

Nothing deployment-specific lives in code: tenant, app IDs and URLs all come from environment variables.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SCOPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

READ_TOOLS: tuple[str, ...] = (
    "planner_list_plans",
    "planner_get_plan",
    "planner_get_task",
    "planner_my_tasks",
)
WRITE_TOOLS: tuple[str, ...] = (
    "planner_create_task",
    "planner_update_task",
    "planner_complete_task",
    "planner_assign_task",
)
ALL_TOOLS: tuple[str, ...] = READ_TOOLS + WRITE_TOOLS

OBO_MODES = ("managed_identity", "local_dev_secret")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """Raised when the environment does not describe a valid deployment."""


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"not a boolean: {value!r}")


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    api_client_id: str
    public_base_url: str
    required_scope: str = "Planner.Use"
    allowed_audiences: tuple[str, ...] = ()
    allowed_client_ids: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = READ_TOOLS
    obo_credential: str = "managed_identity"
    managed_identity_client_id: str | None = None
    local_dev_client_secret: str | None = field(default=None, repr=False)
    rate_limit_per_min: int = 60
    log_level: str = "WARNING"
    environment: str = "production"
    enable_user_lookup: bool = False
    allowed_origins: tuple[str, ...] = ("https://claude.ai", "https://claude.com")
    extra_allowed_hosts: tuple[str, ...] = ()
    authority_host: str = "https://login.microsoftonline.com"
    graph_base_url: str = "https://graph.microsoft.com/v1.0"

    # ---- derived values -------------------------------------------------------------------
    @property
    def issuer(self) -> str:
        return f"{self.authority_host}/{self.tenant_id}/v2.0"

    @property
    def jwks_uri(self) -> str:
        return f"{self.authority_host}/{self.tenant_id}/discovery/v2.0/keys"

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_base_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_base_url).netloc

    @property
    def mcp_path(self) -> str:
        return urlsplit(self.public_base_url).path or "/mcp"

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.public_origin}/.well-known/oauth-protected-resource{self.mcp_path}"

    @property
    def full_scope(self) -> str:
        return f"api://{self.api_client_id}/{self.required_scope}"

    @property
    def audiences(self) -> tuple[str, ...]:
        return self.allowed_audiences or (self.api_client_id,)

    @property
    def graph_scopes(self) -> list[str]:
        scopes = ["https://graph.microsoft.com/Tasks.ReadWrite", "https://graph.microsoft.com/User.Read"]
        if self.enable_user_lookup:
            scopes.append("https://graph.microsoft.com/User.ReadBasic.All")
        return scopes

    @property
    def allowed_hosts(self) -> list[str]:
        host = self.public_host
        hosts = [host]
        if ":" not in host:
            hosts.append(f"{host}:443")
        hosts.extend(self.extra_allowed_hosts)
        return hosts

    # ---- construction ---------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env

        def req(name: str) -> str:
            v = (e.get(name) or "").strip()
            if not v:
                raise ConfigError(f"{name} is required")
            return v

        allowed_tools = _csv(e.get("ALLOWED_TOOLS")) or list(READ_TOOLS)
        try:
            rate = int(e.get("RATE_LIMIT_PER_MIN", "60"))
        except ValueError as exc:
            raise ConfigError("RATE_LIMIT_PER_MIN must be an integer") from exc
        origins = e.get("ALLOWED_ORIGINS")
        s = cls(
            tenant_id=req("ENTRA_TENANT_ID"),
            api_client_id=req("ENTRA_API_CLIENT_ID"),
            public_base_url=req("PUBLIC_BASE_URL"),
            required_scope=(e.get("ENTRA_REQUIRED_SCOPE") or "Planner.Use").strip(),
            allowed_audiences=tuple(_csv(e.get("ENTRA_ALLOWED_AUDIENCES"))),
            allowed_client_ids=tuple(_csv(e.get("ENTRA_ALLOWED_CLIENT_IDS"))),
            allowed_tools=tuple(allowed_tools),
            obo_credential=(e.get("OBO_CREDENTIAL") or "managed_identity").strip(),
            managed_identity_client_id=(e.get("MANAGED_IDENTITY_CLIENT_ID") or "").strip() or None,
            local_dev_client_secret=(e.get("LOCAL_DEV_CLIENT_SECRET") or "").strip() or None,
            rate_limit_per_min=rate,
            log_level=(e.get("LOG_LEVEL") or "WARNING").strip().upper(),
            environment=(e.get("ENV") or "production").strip().lower(),
            enable_user_lookup=_bool(e.get("ENABLE_USER_LOOKUP")),
            allowed_origins=tuple(_csv(origins)) if origins is not None else cls.allowed_origins,
            extra_allowed_hosts=tuple(_csv(e.get("EXTRA_ALLOWED_HOSTS"))),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if not GUID_RE.match(self.tenant_id):
            raise ConfigError("ENTRA_TENANT_ID must be a GUID")
        if not GUID_RE.match(self.api_client_id):
            raise ConfigError("ENTRA_API_CLIENT_ID must be a GUID")
        for cid in self.allowed_client_ids:
            if not GUID_RE.match(cid):
                raise ConfigError("ENTRA_ALLOWED_CLIENT_IDS must be GUIDs")
        if not SCOPE_RE.match(self.required_scope):
            raise ConfigError("ENTRA_REQUIRED_SCOPE is not a valid scope name")
        parts = urlsplit(self.public_base_url)
        if parts.scheme != "https" and self.environment == "production":
            raise ConfigError("PUBLIC_BASE_URL must be https in production")
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError("PUBLIC_BASE_URL must be an absolute URL")
        if parts.query or parts.fragment or self.public_base_url.endswith("/"):
            raise ConfigError("PUBLIC_BASE_URL must have no query, fragment or trailing slash")
        if not parts.path or parts.path == "/":
            raise ConfigError("PUBLIC_BASE_URL must include the MCP path, e.g. /mcp")
        unknown = [t for t in self.allowed_tools if t not in ALL_TOOLS]
        if unknown:
            raise ConfigError(f"ALLOWED_TOOLS has unknown tools: {', '.join(unknown)}")
        if self.obo_credential not in OBO_MODES:
            raise ConfigError(f"OBO_CREDENTIAL must be one of {', '.join(OBO_MODES)}")
        if self.obo_credential == "local_dev_secret":
            if self.environment == "production":
                raise ConfigError("OBO_CREDENTIAL=local_dev_secret is refused when ENV=production")
            if not self.local_dev_client_secret:
                raise ConfigError("LOCAL_DEV_CLIENT_SECRET is required for OBO_CREDENTIAL=local_dev_secret")
        if self.managed_identity_client_id and not GUID_RE.match(self.managed_identity_client_id):
            raise ConfigError("MANAGED_IDENTITY_CLIENT_ID must be a GUID")
        if not 1 <= self.rate_limit_per_min <= 10_000:
            raise ConfigError("RATE_LIMIT_PER_MIN must be between 1 and 10000")
        if self.log_level not in LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
        if self.log_level == "DEBUG" and self.environment == "production":
            # Library debug logs can include request bodies (task content). Never in production.
            raise ConfigError("LOG_LEVEL=DEBUG is refused when ENV=production")
