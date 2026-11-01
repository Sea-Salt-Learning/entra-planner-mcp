"""401 shape, protected-resource metadata and every token-rejection case."""

from __future__ import annotations

import time

import pytest
from conftest import (
    _OTHER_KEY,
    API_APP,
    CLIENT_APP,
    PUBLIC_URL,
    TENANT,
    make_token,
    rpc,
)

METADATA_URL = "https://planner.contoso.example/.well-known/oauth-protected-resource/mcp"
SCOPE = f"api://{API_APP}/Planner.Use"


def test_unauthenticated_post_gets_401_with_resource_metadata(client):
    r = rpc(client, "tools/list", token=False)
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert f'resource_metadata="{METADATA_URL}"' in www
    assert f'scope="{SCOPE} offline_access"' in www
    assert "error=" not in www  # no token at all: no error code (RFC 6750 3.1)


@pytest.mark.parametrize("path", ["/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"])
def test_protected_resource_metadata(client, path):
    r = client.get(path)
    assert r.status_code == 200
    body = r.json()
    assert body["resource"] == PUBLIC_URL
    assert body["authorization_servers"] == [f"https://login.microsoftonline.com/{TENANT}/v2.0"]
    assert body["scopes_supported"] == [SCOPE]
    assert body["bearer_methods_supported"] == ["header"]


def test_valid_token_is_accepted(client):
    r = rpc(client, "tools/list")
    assert r.status_code == 200
    assert "planner_list_plans" in r.text


now = int(time.time())

REJECTIONS = {
    "expired": make_token({"exp": now - 600}),
    "not yet valid": make_token({"nbf": now + 600}),
    "wrong audience": make_token({"aud": "00000003-0000-0000-c000-000000000000"}),
    "wrong issuer": make_token({"iss": "https://login.microsoftonline.com/00000000-0000-4000-8000-000000000000/v2.0"}),
    "v1 issuer": make_token({"iss": f"https://sts.windows.net/{TENANT}/"}),
    "wrong tenant": make_token({"tid": "00000000-0000-4000-8000-000000000000"}),
    "v1 token": make_token({"ver": "1.0"}),
    "app-only (no scp)": make_token({"roles": ["Tasks.ReadWrite.All"]}, drop=("scp",)),
    "app-only (idtyp)": make_token({"idtyp": "app"}),
    "missing oid": make_token(drop=("oid",)),
    "bad oid": make_token({"oid": "not-a-guid"}),
    "missing exp": make_token(drop=("exp",)),
    "signed by another key": make_token(key=_OTHER_KEY),
    "unknown kid": make_token(kid="nope"),
    "HS256": make_token(alg="HS256"),
    "malformed": "not.a.jwt",
    "garbage": "abc",
    "too long": "a" * 20_000 + ".b.c",
}


@pytest.mark.parametrize("case", list(REJECTIONS))
def test_rejected_tokens_get_401(client, case):
    r = rpc(client, "tools/list", token=REJECTIONS[case])
    assert r.status_code == 401, case
    www = r.headers["www-authenticate"]
    assert 'error="invalid_token"' in www
    assert f'resource_metadata="{METADATA_URL}"' in www
    assert "planner_list_plans" not in r.text


def test_alg_none_rejected(client):
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    token = b64({"alg": "none", "kid": "test-kid-1"}) + "." + b64({"aud": API_APP, "tid": TENANT}) + "."
    assert rpc(client, "tools/list", token=token).status_code == 401


def test_wrong_scope_gets_403_insufficient_scope(client):
    r = rpc(client, "tools/list", token=make_token({"scp": "User.Read"}))
    assert r.status_code == 403
    assert 'error="insufficient_scope"' in r.headers["www-authenticate"]


def test_client_allow_list(make_client):
    c = make_client(ENTRA_ALLOWED_CLIENT_IDS="00000000-0000-4000-8000-000000000001")
    assert rpc(c, "tools/list").status_code == 401
    c2 = make_client(ENTRA_ALLOWED_CLIENT_IDS=CLIENT_APP)
    assert rpc(c2, "tools/list").status_code == 200


def test_non_bearer_scheme_rejected(client):
    r = client.post(
        "/mcp",
        headers={"authorization": "Basic dXNlcjpwYXNz", "content-type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401


def test_no_redirects_on_mcp_path(client):
    for path in ("/mcp/", "/MCP", "/mcp/x"):
        r = client.post(path, follow_redirects=False, json={})
        assert r.status_code not in (301, 302, 307, 308), path


def test_wrong_host_header_rejected(client):
    r = rpc(client, "tools/list")
    assert r.status_code == 200
    headers = {
        "host": "evil.example",
        "authorization": f"Bearer {make_token()}",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 421


def test_health(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200


def test_ready_is_503_when_jwks_unreachable(make_client, graph):
    def broken(request):
        import httpx

        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(503)
        return graph.handler(request)

    graph.handler = broken  # type: ignore[method-assign]
    c = make_client()
    assert c.get("/health/ready").status_code == 503


# ---- required app role (ENTRA_REQUIRED_ROLE) -------------------------------------------------------
ROLE_CASES = {
    "missing roles claim": (make_token(), 403),
    "empty roles": (make_token({"roles": []}), 403),
    "wrong role": (make_token({"roles": ["Something.Else"]}), 403),
    "roles not a list": (make_token({"roles": "Planner.User"}), 403),
    "right role": (make_token({"roles": ["Other", "Planner.User"]}), 200),
}


@pytest.mark.parametrize("case", list(ROLE_CASES))
def test_required_role(make_client, case):
    token, expected = ROLE_CASES[case]
    c = make_client(ENTRA_REQUIRED_ROLE="Planner.User")
    r = rpc(c, "tools/list", token=token)
    assert r.status_code == expected, case
    if expected == 403:
        www = r.headers["www-authenticate"]
        assert f'resource_metadata="{METADATA_URL}"' in www
        assert "error=" not in www.replace("error_description=", "")  # no error code, so no scope step-up
        assert 'error_description="user is not assigned the required app role"' in www
        assert r.json()["error"] == "forbidden"
        assert "planner_list_plans" not in r.text


def test_role_not_required_by_default(client):
    assert rpc(client, "tools/list", token=make_token()).status_code == 200


def test_role_checked_after_token_validity(make_client):
    c = make_client(ENTRA_REQUIRED_ROLE="Planner.User")
    expired_with_role = make_token({"roles": ["Planner.User"], "exp": int(time.time()) - 600})
    assert rpc(c, "tools/list", token=expired_with_role).status_code == 401
