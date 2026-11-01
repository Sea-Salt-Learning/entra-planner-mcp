"""Test fixtures: a fake Entra (JWKS + token minting) and a fake Microsoft Graph Planner API.

All identifiers here are invented. Nothing talks to the network.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.testclient import TestClient

from entra_planner_mcp.app import build_app
from entra_planner_mcp.config import ALL_TOOLS, Settings

TENANT = "11111111-2222-4333-8444-555555555555"
API_APP = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CLIENT_APP = "99999999-8888-4777-8666-555555555555"
USER = "12345678-1234-4234-8234-123456789abc"
OTHER_USER = "87654321-4321-4321-8321-cba987654321"
PLAN = "PlanAbcdefghijklmnopqrstu1"
BUCKET = "BucketAbcdefghijklmnopqrs1"
TASK = "TaskAbcdefghijklmnopqrstuv1"
PUBLIC_URL = "https://planner.contoso.example/mcp"
SECRET_TITLE = "Quarterly widget review for Contoso"
GRAPH_TOKEN = "fake-graph-token-for-tests"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-kid-1"


def jwks() -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(_KEY.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


def make_token(
    overrides: dict[str, Any] | None = None,
    *,
    drop: tuple[str, ...] = (),
    key: Any = None,
    kid: str = KID,
    alg: str = "RS256",
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "aud": API_APP,
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "iat": now - 10,
        "nbf": now - 10,
        "exp": now + 3600,
        "tid": TENANT,
        "oid": USER,
        "sub": "subject",
        "scp": "Planner.Use",
        "azp": CLIENT_APP,
        "ver": "2.0",
    }
    claims.update(overrides or {})
    for k in drop:
        claims.pop(k, None)
    if alg == "HS256":
        return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": kid})
    return jwt.encode(claims, key or _KEY, algorithm=alg, headers={"kid": kid})


class FakeGraph:
    """Enough of Graph's Planner API for the tools, with real ETag semantics."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.task: dict[str, Any] = {
            "id": TASK,
            "planId": PLAN,
            "bucketId": BUCKET,
            "title": SECRET_TITLE,
            "percentComplete": 0,
            "priority": 5,
            "dueDateTime": "2030-01-15T12:00:00Z",
            "assignments": {USER: {"orderHint": " !"}},
            "@odata.etag": 'W/"etag-1"',
        }
        self.details: dict[str, Any] = {
            "id": TASK,
            "description": "secret notes",
            "@odata.etag": 'W/"detail-1"',
            "checklist": {"c1": {"title": "first", "isChecked": False}},
            "references": {},
        }
        self.extra_tasks: list[dict[str, Any]] = [
            {
                "id": "TaskDoneAbcdefghijklmnopqr1",
                "planId": PLAN,
                "bucketId": BUCKET,
                "title": "done",
                "percentComplete": 100,
                "assignments": {},
                "@odata.etag": 'W/"x"',
            },
            {
                "id": "TaskOtherAbcdefghijklmnopq1",
                "planId": PLAN,
                "bucketId": "BucketOtherAbcdefghijklmn1",
                "title": "other",
                "percentComplete": 50,
                "dueDateTime": "2030-03-01T12:00:00Z",
                "assignments": {OTHER_USER: {}},
                "@odata.etag": 'W/"y"',
            },
        ]

    def _bump(self, obj: dict[str, Any]) -> None:
        n = int(obj["@odata.etag"].split("-")[-1].rstrip('"')) + 1
        obj["@odata.etag"] = obj["@odata.etag"].rsplit("-", 1)[0] + f'-{n}"'

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "login.microsoftonline.com":
            return httpx.Response(200, json=jwks())
        assert url.host == "graph.microsoft.com", f"unexpected host {url.host}"
        assert request.method != "DELETE", "DELETE must never be sent"
        assert request.headers["authorization"] == f"Bearer {GRAPH_TOKEN}"
        path = url.path.removeprefix("/v1.0")
        m = request.method
        if m == "GET" and path == "/me/planner/plans":
            return httpx.Response(
                200, json={"value": [{"id": PLAN, "title": "Test plan", "container": {"containerId": "group-1"}}]}
            )
        if m == "GET" and path == f"/planner/plans/{PLAN}":
            return httpx.Response(200, json={"id": PLAN, "title": "Test plan"})
        if m == "GET" and path.startswith("/planner/plans/") and path.count("/") == 3:
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        if m == "GET" and path == f"/planner/plans/{PLAN}/buckets":
            return httpx.Response(200, json={"value": [{"id": BUCKET, "name": "To do"}]})
        if m == "GET" and path == f"/planner/plans/{PLAN}/tasks":
            return httpx.Response(200, json={"value": [self.task, *self.extra_tasks]})
        if m == "GET" and path == "/me/planner/tasks":
            return httpx.Response(200, json={"value": [self.task]})
        if path == f"/planner/tasks/{TASK}":
            if m == "GET":
                return httpx.Response(200, json=self.task)
            if m == "PATCH":
                if request.headers.get("if-match") != self.task["@odata.etag"]:
                    return httpx.Response(412, json={"error": {"code": "PreconditionFailed"}})
                body = json.loads(request.content)
                for k, v in body.items():
                    if k == "assignments":
                        for uid, a in v.items():
                            if a is None:
                                self.task["assignments"].pop(uid, None)
                            else:
                                self.task["assignments"][uid] = a
                    else:
                        self.task[k] = v
                self._bump(self.task)
                return httpx.Response(200, json=self.task)
        if path == f"/planner/tasks/{TASK}/details":
            if m == "GET":
                return httpx.Response(200, json=self.details)
            if m == "PATCH":
                if request.headers.get("if-match") != self.details["@odata.etag"]:
                    return httpx.Response(412, json={"error": {"code": "PreconditionFailed"}})
                body = json.loads(request.content)
                if "description" in body:
                    self.details["description"] = body["description"]
                for k, v in (body.get("checklist") or {}).items():
                    self.details["checklist"].setdefault(k, {}).update(
                        {kk: vv for kk, vv in v.items() if not kk.startswith("@")}
                    )
                self._bump(self.details)
                return httpx.Response(200, json=self.details)
        if m == "POST" and path == "/planner/tasks":
            body = json.loads(request.content)
            if body.get("planId") != PLAN:
                return httpx.Response(403, json={"error": {"code": "Forbidden"}})
            new = {**body, "id": TASK, "@odata.etag": 'W/"etag-1"', "percentComplete": 0}
            new.setdefault("assignments", {})
            self.task = new
            return httpx.Response(201, json=new)
        if path.startswith("/planner/tasks/"):
            return httpx.Response(403, json={"error": {"code": "Forbidden"}})
        return httpx.Response(400, json={"error": {"code": "BadRequest"}})


class FakeTokens:
    def __init__(self) -> None:
        self.assertions: list[str] = []

    async def graph_token(self, user_assertion: str) -> str:
        self.assertions.append(user_assertion)
        return GRAPH_TOKEN


def settings(**overrides: Any) -> Settings:
    env = {
        "ENTRA_TENANT_ID": TENANT,
        "ENTRA_API_CLIENT_ID": API_APP,
        "PUBLIC_BASE_URL": PUBLIC_URL,
        "ALLOWED_TOOLS": ",".join(ALL_TOOLS),
        "EXTRA_ALLOWED_HOSTS": "testserver",
        "LOG_LEVEL": "INFO",
    }
    env.update({k: v for k, v in overrides.items() if v is not None})
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
    return Settings.from_env(env)


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def tokens() -> FakeTokens:
    return FakeTokens()


@pytest.fixture
def make_client(graph: FakeGraph, tokens: FakeTokens) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(**env: Any) -> TestClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(graph.handler))
        app = build_app(settings(**env), http=http, tokens=tokens)
        c = TestClient(app, base_url="https://testserver")
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def rpc(
    client: TestClient, method: str, params: dict[str, Any] | None = None, token: str | None = None
) -> httpx.Response:
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": "2025-06-18",
    }
    if token is not False:
        headers["authorization"] = f"Bearer {token or make_token()}"
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params or {}}
    return client.post("/mcp", headers=headers, json=body)


def call_tool(
    client: TestClient, name: str, arguments: dict[str, Any] | None = None, token: str | None = None
) -> dict[str, Any]:
    resp = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, token)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]  # type: ignore[no-any-return]
