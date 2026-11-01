"""A narrow Microsoft Graph client: only the Planner calls the tools need.

There is deliberately no generic request method, no path passthrough and no DELETE.
"""

from __future__ import annotations

import contextlib
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

MAX_PAGES = 10


class GraphError(Exception):
    """A Graph call failed. ``str()`` is safe to show to the user (no content, no token)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _error_for(resp: httpx.Response, what: str) -> GraphError:
    s = resp.status_code
    if s in (401, 403):
        return GraphError(s, f"you don't have access to this {what}")
    if s == 404:
        return GraphError(s, f"{what} not found, or you don't have access to it")
    if s in (409, 412):
        return GraphError(s, f"the {what} was changed by someone else: re-read it and retry")
    if s == 429:
        return GraphError(s, "Planner is throttling requests: try again in a minute")
    if s == 400:
        code = ""
        with contextlib.suppress(ValueError, AttributeError):
            code = str(resp.json().get("error", {}).get("code", ""))[:64]
        return GraphError(s, f"Planner rejected the request{f' ({code})' if code else ''}")
    if s >= 500:
        return GraphError(s, "Microsoft Graph is unavailable: try again shortly")
    return GraphError(s, f"Microsoft Graph returned an unexpected status {s}")


def _seg(value: str) -> str:
    """Quote a single path segment (IDs are already validated; this is defence in depth)."""
    return quote(value, safe="")


class GraphClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str = "https://graph.microsoft.com/v1.0") -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._host = urlsplit(self._base).netloc

    # ---- transport ------------------------------------------------------------------------
    def _headers(self, token: str, etag: str | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if etag is not None:
            h["If-Match"] = etag
        return h

    async def _get(self, token: str, path: str, what: str, params: dict[str, str] | None = None) -> Any:
        resp = await self._http.get(f"{self._base}{path}", headers=self._headers(token), params=params)
        if resp.status_code != 200:
            raise _error_for(resp, what)
        return resp.json()

    async def _get_all(self, token: str, path: str, what: str, limit: int) -> tuple[list[Any], bool]:
        items: list[Any] = []
        url: str | None = f"{self._base}{path}"
        pages = 0
        while url and pages < MAX_PAGES and len(items) < limit:
            # Only ever follow nextLinks back to Graph itself.
            if urlsplit(url).netloc != self._host or not url.startswith(self._base + "/"):
                raise GraphError(502, "Microsoft Graph returned an unexpected paging link")
            resp = await self._http.get(url, headers=self._headers(token))
            if resp.status_code != 200:
                raise _error_for(resp, what)
            body = resp.json()
            items.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
            pages += 1
        truncated = bool(url) or len(items) > limit
        return items[:limit], truncated

    async def _patch(self, token: str, path: str, etag: str, body: dict[str, Any], what: str) -> Any:
        headers = self._headers(token, etag)
        headers["Prefer"] = "return=representation"
        resp = await self._http.patch(f"{self._base}{path}", headers=headers, json=body)
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            raise _error_for(resp, what)
        return resp.json()

    # ---- Planner (read) -------------------------------------------------------------------
    async def my_plans(self, token: str, limit: int) -> tuple[list[Any], bool]:
        return await self._get_all(token, "/me/planner/plans", "plan", limit)

    async def plan(self, token: str, plan_id: str) -> Any:
        return await self._get(token, f"/planner/plans/{_seg(plan_id)}", "plan")

    async def plan_details(self, token: str, plan_id: str) -> Any:
        return await self._get(token, f"/planner/plans/{_seg(plan_id)}/details", "plan")

    async def plan_buckets(self, token: str, plan_id: str) -> tuple[list[Any], bool]:
        return await self._get_all(token, f"/planner/plans/{_seg(plan_id)}/buckets", "plan", 200)

    async def plan_tasks(self, token: str, plan_id: str, limit: int) -> tuple[list[Any], bool]:
        return await self._get_all(token, f"/planner/plans/{_seg(plan_id)}/tasks", "plan", limit)

    async def my_tasks(self, token: str, limit: int) -> tuple[list[Any], bool]:
        return await self._get_all(token, "/me/planner/tasks", "task", limit)

    async def task(self, token: str, task_id: str) -> Any:
        return await self._get(token, f"/planner/tasks/{_seg(task_id)}", "task")

    async def task_details(self, token: str, task_id: str) -> Any:
        return await self._get(token, f"/planner/tasks/{_seg(task_id)}/details", "task")

    async def user_id_by_upn(self, token: str, upn: str) -> str:
        body = await self._get(token, f"/users/{_seg(upn)}", "user", params={"$select": "id"})
        return str(body["id"])

    # ---- Planner (write; no delete exists) --------------------------------------------------
    async def create_task(self, token: str, body: dict[str, Any]) -> Any:
        resp = await self._http.post(f"{self._base}/planner/tasks", headers=self._headers(token), json=body)
        if resp.status_code != 201:
            raise _error_for(resp, "plan")
        return resp.json()

    async def update_task(self, token: str, task_id: str, etag: str, body: dict[str, Any]) -> Any:
        return await self._patch(token, f"/planner/tasks/{_seg(task_id)}", etag, body, "task")

    async def update_task_details(self, token: str, task_id: str, etag: str, body: dict[str, Any]) -> Any:
        return await self._patch(token, f"/planner/tasks/{_seg(task_id)}/details", etag, body, "task")
