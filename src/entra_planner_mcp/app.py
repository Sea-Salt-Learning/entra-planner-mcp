"""ASGI application: auth gate, protected-resource metadata, health endpoints and the MCP endpoint."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .auth import JwksCache, TokenError, TokenValidator
from .config import Settings
from .graph import GraphClient
from .logsafe import install_redaction
from .obo import GraphTokenProvider, MsalOboProvider
from .tools import PlannerTools, current_principal

log = logging.getLogger("entra_planner_mcp")

WELL_KNOWN = "/.well-known/oauth-protected-resource"
_SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"strict-transport-security", b"max-age=31536000"),
]


def protected_resource_metadata(s: Settings) -> dict[str, Any]:
    return {
        "resource": s.public_base_url,
        "authorization_servers": [s.issuer],
        "scopes_supported": [s.full_scope],
        "bearer_methods_supported": ["header"],
        "resource_name": "Entra Planner MCP",
    }


def _www_authenticate(s: Settings, error: str | None = None, description: str | None = None) -> str:
    parts = [f'resource_metadata="{s.resource_metadata_url}"', f'scope="{s.full_scope} offline_access"']
    if error:
        parts.append(f'error="{error}"')
    if description:
        parts.append(f'error_description="{description}"')
    return "Bearer " + ", ".join(parts)


class AuthGate:
    """Pure ASGI wrapper. Every request to the MCP path needs a valid Entra token; nothing else does."""

    def __init__(self, app: ASGIApp, settings: Settings, validator: TokenValidator, jwks: JwksCache) -> None:
        self._app = app
        self._s = settings
        self._validator = validator
        self._jwks = jwks
        self._metadata = json.dumps(protected_resource_metadata(settings)).encode()

    async def _send_json(
        self, send: Send, status: int, body: bytes, extra: list[tuple[bytes, bytes]] | None = None
    ) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            *_SECURITY_HEADERS,
            *(extra or []),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        path: str = scope["path"]
        method: str = scope["method"]

        if path in (WELL_KNOWN, WELL_KNOWN + self._s.mcp_path):
            if method not in ("GET", "HEAD"):
                await self._send_json(send, 405, b'{"error":"method not allowed"}')
                return
            await self._send_json(send, 200, self._metadata, [(b"access-control-allow-origin", b"*")])
            return
        if path == "/health/live":
            await self._send_json(send, 200, b'{"status":"ok"}')
            return
        if path == "/health/ready":
            ok = self._jwks.ready
            if not ok:
                try:
                    await self._jwks.refresh()
                    ok = self._jwks.ready
                except Exception:  # noqa: BLE001 - readiness only reports the outcome
                    ok = False
            await self._send_json(send, 200 if ok else 503, b'{"status":"ok"}' if ok else b'{"status":"not ready"}')
            return
        if path != self._s.mcp_path:
            await self._send_json(send, 404, b'{"error":"not found"}')
            return

        # ---- the MCP endpoint: authenticate first, never redirect ----
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            await self._unauthorized(send, None)
            return
        try:
            principal = await self._validator.validate(auth[7:].strip())
        except TokenError as exc:
            await self._unauthorized(send, exc)
            return
        token = current_principal.set(principal)
        try:
            await self._app(scope, receive, send)
        finally:
            current_principal.reset(token)

    async def _unauthorized(self, send: Send, exc: TokenError | None) -> None:
        err: str | None = None
        status = 401
        if exc is not None:
            status, err = (403, "insufficient_scope") if exc.insufficient_scope else (401, "invalid_token")
        desc = str(exc) if exc is not None else None
        body = json.dumps({"error": err or "unauthorized", "error_description": desc or "authentication required"})
        await self._send_json(
            send, status, body.encode(), [(b"www-authenticate", _www_authenticate(self._s, err, desc).encode())]
        )


def build_app(
    settings: Settings,
    *,
    http: httpx.AsyncClient | None = None,
    tokens: GraphTokenProvider | None = None,
) -> ASGIApp:
    install_redaction()
    logging.getLogger().setLevel(settings.log_level)
    http = http or httpx.AsyncClient(timeout=20.0, follow_redirects=False)
    jwks = JwksCache(settings.jwks_uri, http)
    validator = TokenValidator(settings, jwks)
    graph = GraphClient(http, settings.graph_base_url)
    tools = PlannerTools(settings, graph, tokens or MsalOboProvider(settings))

    server: MCPServer[Any] = MCPServer(
        name="entra-planner-mcp",
        version=__version__,
        instructions="Read and update Microsoft Planner tasks in the group plans you belong to.",
        log_level=settings.log_level,  # type: ignore[arg-type]
    )
    tools.register(server)
    mcp_app = server.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=list(settings.allowed_origins),
        ),
        host="0.0.0.0",  # noqa: S104 - host checks are done by allowed_hosts above
    )
    mcp_app.router.redirect_slashes = False
    return AuthGate(mcp_app, settings, validator, jwks)
