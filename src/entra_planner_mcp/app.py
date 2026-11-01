"""ASGI application: auth gate, protected-resource metadata, health endpoints and the MCP endpoint.

Start-up is kept fast for scale-to-zero hosting: metadata, health and the unauthenticated 401 are served by this
module straight away, while the MCP SDK (the slow import) is loaded and started in the background. Only an
authenticated MCP request waits for it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import anyio
import httpx

from . import __version__
from .auth import JwksCache, TokenError, TokenValidator
from .config import Settings
from .context import current_principal
from .graph import GraphClient
from .logsafe import install_redaction
from .obo import GraphTokenProvider, MsalOboProvider

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

MCP_READY_TIMEOUT_SECONDS = 30

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

    def __init__(
        self, app_factory: Callable[[], ASGIApp], settings: Settings, validator: TokenValidator, jwks: JwksCache
    ) -> None:
        self._factory = app_factory
        self._app: ASGIApp | None = None
        self._ready: anyio.Event | None = None
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
            await self._lifespan(receive, send)
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
        app = await self._mcp_app()
        if app is None:
            await self._send_json(send, 503, b'{"error":"starting up, retry shortly"}', [(b"retry-after", b"2")])
            return
        token = current_principal.set(principal)
        try:
            await app(scope, receive, send)
        finally:
            current_principal.reset(token)

    async def _mcp_app(self) -> ASGIApp | None:
        if self._app is not None:
            return self._app
        if self._ready is None:  # no lifespan (server started without it): nothing will build the app
            return None
        with anyio.move_on_after(MCP_READY_TIMEOUT_SECONDS):
            await self._ready.wait()
        return self._app

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        """Report startup complete at once; build and start the MCP app in the background."""
        message = await receive()
        if message["type"] != "lifespan.startup":
            return
        self._ready = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._run_mcp)
            tg.start_soon(self._warm_jwks)
            await send({"type": "lifespan.startup.complete"})
            while (await receive())["type"] != "lifespan.shutdown":
                pass
            tg.cancel_scope.cancel()
        await send({"type": "lifespan.shutdown.complete"})

    async def _warm_jwks(self) -> None:
        try:
            await self._jwks.refresh()
        except Exception:  # noqa: BLE001 - readiness and the first request retry
            log.warning("could not fetch the tenant signing keys at start-up; will retry")

    async def _run_mcp(self) -> None:
        app: Any = await anyio.to_thread.run_sync(self._factory)
        async with app.router.lifespan_context(app):
            self._app = app
            assert self._ready is not None
            self._ready.set()
            await anyio.sleep_forever()

    async def _unauthorized(self, send: Send, exc: TokenError | None) -> None:
        err: str | None = None
        status = 401
        if exc is not None and exc.forbidden:
            # Authenticated but not permitted: 403 with no error code, so clients don't attempt a scope step-up
            # (no scope can fix a missing app role).
            status = 403
        elif exc is not None:
            status, err = (403, "insufficient_scope") if exc.insufficient_scope else (401, "invalid_token")
        desc = str(exc) if exc is not None else None
        default_err = "forbidden" if status == 403 else "unauthorized"
        body = json.dumps({"error": err or default_err, "error_description": desc or "authentication required"})
        await self._send_json(
            send, status, body.encode(), [(b"www-authenticate", _www_authenticate(self._s, err, desc).encode())]
        )


def _mcp_factory(settings: Settings, tools_args: tuple[GraphClient, GraphTokenProvider]) -> Callable[[], ASGIApp]:
    def build() -> ASGIApp:
        # Heavy imports happen here, in a worker thread, after the server is already answering.
        from mcp.server.mcpserver import MCPServer
        from mcp.server.transport_security import TransportSecuritySettings

        from .tools import PlannerTools

        graph, tokens = tools_args
        server: MCPServer[Any] = MCPServer(
            name="entra-planner-mcp",
            version=__version__,
            instructions="Read and update Microsoft Planner tasks in the group plans you belong to.",
            log_level=settings.log_level,  # type: ignore[arg-type]
        )
        PlannerTools(settings, graph, tokens).register(server)
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
        return mcp_app

    return build


def build_app(
    settings: Settings,
    *,
    http: httpx.AsyncClient | None = None,
    tokens: GraphTokenProvider | None = None,
) -> ASGIApp:
    install_redaction()
    logging.getLogger().setLevel(settings.log_level)
    # The SDK logs a failed tool call's error text at INFO; ours can echo user input (e.g. a label name), so keep
    # that logger at WARNING or above whatever LOG_LEVEL is.
    sdk_tool_log = logging.getLogger("mcp.server.mcpserver.server")
    sdk_tool_log.setLevel(max(logging.WARNING, logging.getLevelName(settings.log_level)))
    http = http or httpx.AsyncClient(timeout=20.0, follow_redirects=False)
    jwks = JwksCache(settings.jwks_uri, http)
    validator = TokenValidator(settings, jwks)
    graph = GraphClient(http, settings.graph_base_url)
    factory = _mcp_factory(settings, (graph, tokens or MsalOboProvider(settings)))
    return AuthGate(factory, settings, validator, jwks)
