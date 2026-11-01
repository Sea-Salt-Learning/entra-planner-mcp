"""On-Behalf-Of exchange: user token for this API -> Microsoft Graph token for the same user.

The confidential-client credential is a client assertion obtained from the platform's managed identity
(audience ``api://AzureADTokenExchange``) and trusted by the API app through a federated identity
credential. No secret is stored. ``local_dev_secret`` exists for local development only.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import anyio
import httpx

from .config import Settings

TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"  # noqa: S105 - an audience, not a secret


class OboError(Exception):
    """The Graph token could not be obtained. The message is safe to show to the user."""


class GraphTokenProvider(Protocol):
    async def graph_token(self, user_assertion: str) -> str: ...


class ManagedIdentityAssertion:
    """Fetches (and caches) a managed-identity token used as the OBO client assertion.

    Supports the App Service / Container Apps identity endpoint (``IDENTITY_ENDPOINT`` and
    ``IDENTITY_HEADER``) and a projected federated token file (``AZURE_FEDERATED_TOKEN_FILE``).
    """

    def __init__(
        self, client_id: str | None = None, env: dict[str, str] | None = None, http: httpx.Client | None = None
    ) -> None:
        self._env = dict(os.environ) if env is None else env
        self._client_id = client_id
        self._http = http or httpx.Client(timeout=10.0, follow_redirects=False)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 300:
                return self._token
            token, expires_at = self._fetch()
            self._token, self._expires_at = token, expires_at
            return token

    def _fetch(self) -> tuple[str, float]:
        token_file = self._env.get("AZURE_FEDERATED_TOKEN_FILE")
        if token_file:
            return Path(token_file).read_text().strip(), time.time() + 600
        endpoint = self._env.get("IDENTITY_ENDPOINT")
        header = self._env.get("IDENTITY_HEADER")
        if not endpoint or not header:
            raise OboError("no managed identity is available to this server")
        params = {"resource": TOKEN_EXCHANGE_AUDIENCE, "api-version": "2019-08-01"}
        if self._client_id:
            params["client_id"] = self._client_id
        resp = self._http.get(endpoint, params=params, headers={"X-IDENTITY-HEADER": header})
        if resp.status_code != 200:
            raise OboError("the managed identity token could not be obtained")
        body = resp.json()
        try:
            expires_at = float(body.get("expires_on", 0))
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at <= 0:
            expires_at = time.time() + 600
        return str(body["access_token"]), expires_at


def _friendly(result: dict[str, Any]) -> OboError:
    codes = result.get("error_codes") or []
    err = str(result.get("error", ""))
    if 65001 in codes or err == "consent_required":
        return OboError("Microsoft Graph access has not been consented for this app (ask your administrator)")
    if 50105 in codes:
        return OboError("you are not assigned to this app (ask your administrator)")
    if err in ("interaction_required", "invalid_grant"):
        return OboError("your sign-in needs refreshing: disconnect and reconnect the connector")
    return OboError("could not get a Microsoft Graph token for you")


class MsalOboProvider:
    def __init__(self, settings: Settings, assertion: Callable[[], str] | None = None) -> None:
        if settings.obo_credential == "local_dev_secret":
            credential: Any = settings.local_dev_client_secret
        else:
            credential = {
                "client_assertion": assertion or ManagedIdentityAssertion(settings.managed_identity_client_id)
            }
        self._scopes = settings.graph_scopes
        self._client_id = settings.api_client_id
        self._authority = f"{settings.authority_host}/{settings.tenant_id}"
        self._credential = credential
        self._app: Any = None  # created on first use: MSAL contacts the authority when constructed
        self._app_lock = threading.Lock()

    def _client(self) -> Any:
        with self._app_lock:
            if self._app is None:
                import msal  # imported on first use to keep start-up fast

                self._app = msal.ConfidentialClientApplication(
                    client_id=self._client_id, authority=self._authority, client_credential=self._credential
                )
            return self._app

    def _acquire(self, user_assertion: str) -> str:
        try:
            result = self._client().acquire_token_on_behalf_of(user_assertion, scopes=self._scopes)
        except OboError:
            raise
        except Exception as exc:
            raise OboError("could not get a Microsoft Graph token for you") from exc
        if not result or "access_token" not in result:
            raise _friendly(result or {})
        return str(result["access_token"])

    async def graph_token(self, user_assertion: str) -> str:
        return await anyio.to_thread.run_sync(self._acquire, user_assertion)
