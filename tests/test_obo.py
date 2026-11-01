from __future__ import annotations

import time

import httpx
import pytest
from conftest import settings

from entra_planner_mcp.obo import ManagedIdentityAssertion, MsalOboProvider, OboError


def test_managed_identity_assertion_uses_token_exchange_audience():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "mi-assertion", "expires_on": str(int(time.time()) + 3600)})

    mi = ManagedIdentityAssertion(
        env={"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token", "IDENTITY_HEADER": "hdr"},
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert mi() == "mi-assertion"
    assert mi() == "mi-assertion"  # cached
    assert len(seen) == 1
    assert seen[0].url.params["resource"] == "api://AzureADTokenExchange"
    assert seen[0].headers["x-identity-header"] == "hdr"


def test_no_managed_identity():
    with pytest.raises(OboError):
        ManagedIdentityAssertion(env={})()


def test_federated_token_file(tmp_path):
    f = tmp_path / "token"
    f.write_text("projected\n")
    assert ManagedIdentityAssertion(env={"AZURE_FEDERATED_TOKEN_FILE": str(f)})() == "projected"


class FakeMsal:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def acquire_token_on_behalf_of(self, assertion, scopes):
        self.calls.append((assertion, scopes))
        return self.result


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"error": "invalid_grant", "error_codes": [65001]}, "consented"),
        ({"error": "invalid_grant", "error_codes": [50105]}, "not assigned"),
        ({"error": "interaction_required"}, "reconnect"),
        ({"error": "server_error"}, "could not get"),
    ],
)
def test_obo_errors_are_friendly(result, message):
    p = MsalOboProvider(settings(), assertion=lambda: "a")
    p._app = FakeMsal(result)  # type: ignore[assignment]
    with pytest.raises(OboError, match=message):
        p._acquire("user-token")


def test_obo_scopes():
    p = MsalOboProvider(settings(), assertion=lambda: "a")
    fake = FakeMsal({"access_token": "g"})
    p._app = fake  # type: ignore[assignment]
    assert p._acquire("user-token") == "g"
    assert fake.calls[0][1] == ["https://graph.microsoft.com/Tasks.ReadWrite", "https://graph.microsoft.com/User.Read"]
