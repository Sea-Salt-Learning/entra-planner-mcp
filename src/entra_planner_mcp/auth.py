"""Entra ID access-token validation (v2.0 tokens only).

Signature is checked against the tenant's published JWKS with PyJWT; no hand-rolled crypto.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
import jwt
from jwt import PyJWK

from .config import GUID_RE, Settings

CLOCK_SKEW_SECONDS = 120
JWKS_TTL_SECONDS = 6 * 3600
JWKS_MIN_REFRESH_INTERVAL = 300
MAX_TOKEN_LENGTH = 16_384


class TokenError(Exception):
    """The bearer token is missing or invalid. The message is safe to return to the client."""

    def __init__(self, message: str, *, insufficient_scope: bool = False) -> None:
        super().__init__(message)
        self.insufficient_scope = insufficient_scope


@dataclass(frozen=True)
class Principal:
    """The signed-in user, as proven by a validated token."""

    oid: str
    tid: str
    scopes: frozenset[str]
    client_id: str
    token: str  # the raw access token, used only as the OBO user assertion; never logged

    def __repr__(self) -> str:  # never print the token
        return f"Principal(oid={self.oid!r})"


class JwksCache:
    """Caches the tenant signing keys; refreshes on TTL expiry or an unknown key id (rate limited)."""

    def __init__(self, jwks_uri: str, http: httpx.AsyncClient) -> None:
        self._uri = jwks_uri
        self._http = http
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._last_attempt = 0.0
        self._lock = anyio.Lock()

    @property
    def ready(self) -> bool:
        return bool(self._keys)

    async def refresh(self, *, force: bool = False) -> None:
        async with self._lock:
            now = time.monotonic()
            if not force and self._keys and now - self._fetched_at < JWKS_TTL_SECONDS:
                return
            if self._keys and now - self._last_attempt < JWKS_MIN_REFRESH_INTERVAL:
                return
            self._last_attempt = now
            resp = await self._http.get(self._uri, timeout=10.0)
            resp.raise_for_status()
            keys: dict[str, PyJWK] = {}
            for raw in resp.json().get("keys", []):
                if raw.get("kty") != "RSA" or raw.get("use", "sig") != "sig" or "kid" not in raw:
                    continue
                try:
                    keys[raw["kid"]] = PyJWK.from_dict(raw, algorithm="RS256")
                except jwt.PyJWTError:
                    continue
            if not keys:
                raise RuntimeError("JWKS contained no usable signing keys")
            self._keys = keys
            self._fetched_at = now

    async def get(self, kid: str) -> PyJWK | None:
        if not self._keys or time.monotonic() - self._fetched_at >= JWKS_TTL_SECONDS:
            await self.refresh()
        key = self._keys.get(kid)
        if key is None:
            await self.refresh(force=True)
            key = self._keys.get(kid)
        return key


class TokenValidator:
    def __init__(self, settings: Settings, jwks: JwksCache) -> None:
        self._s = settings
        self._jwks = jwks

    async def validate(self, token: str) -> Principal:
        if not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
            raise TokenError("malformed token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenError("malformed token") from exc
        if header.get("alg") != "RS256":
            raise TokenError("unsupported token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenError("token has no key id")
        try:
            key = await self._jwks.get(kid)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            raise TokenError("signing keys unavailable") from exc
        if key is None:
            raise TokenError("unknown signing key")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=["RS256"],
                audience=list(self._s.audiences),
                issuer=self._s.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "nbf", "iat", "iss", "aud", "tid", "ver", "oid"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("token expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise TokenError("token not yet valid") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenError("token audience is not this API") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenError("token issuer is not the configured tenant") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise TokenError("token is missing a required claim") from exc
        except jwt.PyJWTError as exc:
            raise TokenError("invalid token") from exc

        if claims.get("ver") != "2.0":
            raise TokenError("only v2.0 access tokens are accepted")
        if claims.get("tid") != self._s.tenant_id:
            raise TokenError("token tenant is not the configured tenant")
        if claims.get("idtyp") == "app":
            raise TokenError("app-only tokens are not accepted")
        scp = claims.get("scp")
        if not isinstance(scp, str) or not scp.strip():
            # No delegated scopes means an app-only (client credentials) token.
            raise TokenError("app-only tokens are not accepted")
        scopes = frozenset(scp.split())
        if self._s.required_scope not in scopes:
            raise TokenError("token lacks the required scope", insufficient_scope=True)
        oid = claims.get("oid")
        if not isinstance(oid, str) or not GUID_RE.match(oid):
            raise TokenError("token has no valid user object id")
        client_id = str(claims.get("azp") or claims.get("appid") or "")
        if self._s.allowed_client_ids and client_id not in self._s.allowed_client_ids:
            raise TokenError("token was issued to a client that is not allowed")
        return Principal(oid=oid, tid=claims["tid"], scopes=scopes, client_id=client_id, token=token)
