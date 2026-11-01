"""The Planner tools. There is no delete tool, and none can be enabled by configuration."""

from __future__ import annotations

import re
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .auth import Principal
from .config import GUID_RE, Settings
from .context import current_principal
from .graph import GraphClient, GraphError
from .obo import GraphTokenProvider, OboError

PLANNER_ID = r"^[A-Za-z0-9_-]{8,64}$"
CHECKLIST_ID = r"^[A-Za-z0-9_-]{1,64}$"
_PLANNER_ID_RE = re.compile(PLANNER_ID)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
_ETAG_RE = re.compile(r'^W/"[A-Za-z0-9+/=_\-]{1,200}"$')

PRIORITIES: dict[str, int] = {"urgent": 1, "important": 3, "medium": 5, "low": 9}
MAX_TASKS = 400
MAX_CHECKLIST = 20

PlannerId = Annotated[str, Field(pattern=PLANNER_ID, description="A Planner ID")]
Title = Annotated[str, Field(min_length=1, max_length=255)]
Notes = Annotated[str, Field(max_length=10_000)]
DateStr = Annotated[str, Field(min_length=10, max_length=32, description="ISO 8601 date (YYYY-MM-DD) or date-time")]
ETag = Annotated[str, Field(max_length=210, description='Planner ETag from a previous read, e.g. W/"..."')]
Priority = Literal["urgent", "important", "medium", "low"]


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self._limit = per_minute
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self._calls[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self._limit:
            raise ToolError("rate limit reached: try again in a minute")
        q.append(now)


@asynccontextmanager
async def _guard() -> AsyncIterator[None]:
    """Turn every failure into a short, content-free tool error."""
    try:
        yield
    except ToolError:
        raise
    except (GraphError, OboError, ValueError) as exc:
        raise ToolError(str(exc)) from None
    except Exception:  # noqa: BLE001 - never leak internals or content to the client or logs
        raise ToolError("internal error") from None


# ---- helpers ---------------------------------------------------------------------------------
def _due(value: str) -> str:
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            # Midday UTC keeps the same calendar day for every time zone from UTC-11 to UTC+11.
            return f"{d.isoformat()}T12:00:00Z"
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("dates must be ISO 8601, e.g. 2026-01-31") from exc
    if dt.tzinfo is None:
        raise ValueError("date-times must include a time zone, e.g. 2026-01-31T09:00:00Z")
    return dt.isoformat().replace("+00:00", "Z")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _bound(value: str) -> datetime:
    if len(value) == 10:
        return datetime.fromisoformat(value + "T00:00:00+00:00")
    dt = _parse_dt(value)
    if dt is None or dt.tzinfo is None:
        raise ValueError("dates must be ISO 8601, e.g. 2026-01-31")
    return dt


def _etag(value: str | None) -> str | None:
    if value is None:
        return None
    if not _ETAG_RE.match(value):
        raise ValueError("etag is not a Planner ETag")
    return value


MAX_LABELS = 25
LabelList = Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]] | None, Field(max_length=MAX_LABELS)]


def _label_names(plan_details: dict[str, Any]) -> dict[str, str]:
    """category1..category25 -> label name, for the named labels only (Planner calls labels categories)."""
    desc = plan_details.get("categoryDescriptions") or {}
    names: dict[str, str] = {}
    for n in range(1, MAX_LABELS + 1):
        name = desc.get(f"category{n}")
        if isinstance(name, str) and name.strip():
            names[f"category{n}"] = name
    return names


def _applied_labels(t: dict[str, Any], names: dict[str, str]) -> list[str]:
    applied = t.get("appliedCategories") or {}
    keys = [k for k, v in applied.items() if v is True and re.fullmatch(r"category\d{1,2}", k)]
    keys.sort(key=lambda k: int(k[8:]))
    return [names.get(k, k) for k in keys]  # an unnamed label shows as its category key


def _label_keys(requested: list[str], names: dict[str, str]) -> set[str]:
    """Map label names (case-insensitive) to category keys; unknown names are an error."""
    by_name: dict[str, list[str]] = {}
    for key, name in names.items():
        by_name.setdefault(name.strip().casefold(), []).append(key)
    keys: set[str] = set()
    unknown = []
    for label in requested:
        found = by_name.get(label.strip().casefold())
        if found:
            keys.update(found)
        else:
            unknown.append(label)
    if unknown:
        available = ", ".join(sorted(set(names.values()), key=str.casefold)) or "none (the plan has no named labels)"
        raise ValueError(f"unknown label(s): {', '.join(unknown)}. This plan's labels are: {available}")
    return keys


def _first_bucket(buckets: list[Any]) -> str | None:
    """The bucket Planner shows first: lowest orderHint (ordinal comparison)."""
    ordered = sorted((b for b in buckets if b.get("id")), key=lambda b: str(b.get("orderHint") or ""))
    return str(ordered[0]["id"]) if ordered else None


def _task_summary(t: dict[str, Any], label_names: dict[str, str] | None = None) -> dict[str, Any]:
    out = _task_core(t)
    if label_names is not None:
        out["labels"] = _applied_labels(t, label_names)
    return out


def _task_core(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "planId": t.get("planId"),
        "bucketId": t.get("bucketId"),
        "percentComplete": t.get("percentComplete"),
        "priority": t.get("priority"),
        "dueDateTime": t.get("dueDateTime"),
        "assignees": sorted((t.get("assignments") or {}).keys()),
        "etag": t.get("@odata.etag"),
    }


def _details_summary(d: dict[str, Any]) -> dict[str, Any]:
    checklist = [
        {"id": k, "title": v.get("title"), "isChecked": bool(v.get("isChecked"))}
        for k, v in (d.get("checklist") or {}).items()
        if isinstance(v, dict)
    ]
    references = [
        {"url": k, "alias": v.get("alias")} for k, v in (d.get("references") or {}).items() if isinstance(v, dict)
    ]
    return {
        "description": d.get("description") or "",
        "checklist": checklist,
        "references": references,
        "detailsEtag": d.get("@odata.etag"),
    }


def _checklist_add(titles: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for title in titles:
        if not 1 <= len(title) <= 100:
            raise ValueError("checklist item titles must be 1-100 characters")
        out[str(uuid.uuid4())] = {
            "@odata.type": "microsoft.graph.plannerChecklistItem",
            "title": title,
            "isChecked": False,
        }
    return out


# ---- the tools -------------------------------------------------------------------------------
class PlannerTools:
    def __init__(self, settings: Settings, graph: GraphClient, tokens: GraphTokenProvider) -> None:
        self._s = settings
        self._graph = graph
        self._tokens = tokens
        self._rate = RateLimiter(settings.rate_limit_per_min)

    async def _token(self) -> tuple[Principal, str]:
        principal = current_principal.get()
        if principal is None:
            raise ToolError("not authenticated")
        self._rate.check(principal.oid)
        return principal, await self._tokens.graph_token(principal.token)

    async def _user_id(self, token: str, principal: Principal, who: str) -> str:
        if who == "me":
            return principal.oid
        if GUID_RE.match(who):
            return who.lower()
        if self._s.enable_user_lookup and _EMAIL_RE.match(who):
            return await self._graph.user_id_by_upn(token, who)
        if self._s.enable_user_lookup:
            raise ValueError("assignees must be user object IDs, email addresses or 'me'")
        raise ValueError("assignees must be user object IDs (GUIDs) or 'me'")

    # read --------------------------------------------------------------------------------------
    async def planner_list_plans(self) -> dict[str, Any]:
        """List the Planner plans in the Microsoft 365 groups (teams) you belong to."""
        async with _guard():
            _, token = await self._token()
            plans, truncated = await self._graph.my_plans(token, 200)
            return {
                "plans": [
                    {
                        "id": p.get("id"),
                        "title": p.get("title"),
                        "groupId": (p.get("container") or {}).get("containerId") or p.get("owner"),
                        "createdDateTime": p.get("createdDateTime"),
                    }
                    for p in plans
                ],
                "truncated": truncated,
            }

    async def planner_get_plan(
        self,
        plan_id: PlannerId,
        bucket_id: Annotated[str | None, Field(pattern=PLANNER_ID)] = None,
        assignee: Annotated[
            str | None, Field(max_length=254, description="'me', a user object ID or (if enabled) an email")
        ] = None,
        due_before: DateStr | None = None,
        due_after: DateStr | None = None,
        status: Literal["open", "completed", "all"] = "open",
        limit: Annotated[int, Field(ge=1, le=MAX_TASKS)] = 100,
    ) -> dict[str, Any]:
        """Get a plan's labels, buckets and tasks (each with its labels by name).

        Optionally filtered by bucket, assignee, due date and status.
        """
        async with _guard():
            principal, token = await self._token()
            who = await self._user_id(token, principal, assignee) if assignee else None
            before = None
            if due_before:  # inclusive: a date-only bound includes that whole day
                before = _bound(due_before) + timedelta(days=1 if len(due_before) == 10 else 0)
            after = _bound(due_after) if due_after else None
            plan = await self._graph.plan(token, plan_id)
            names = _label_names(await self._graph.plan_details(token, plan_id))
            buckets, _ = await self._graph.plan_buckets(token, plan_id)
            tasks, truncated = await self._graph.plan_tasks(token, plan_id, MAX_TASKS)
            out = []
            for t in tasks:
                done = t.get("percentComplete") == 100
                if (status == "open" and done) or (status == "completed" and not done):
                    continue
                if bucket_id and t.get("bucketId") != bucket_id:
                    continue
                if who and who not in (t.get("assignments") or {}):
                    continue
                due = _parse_dt(t.get("dueDateTime"))
                if (before or after) and due is None:
                    continue
                if before and due and due >= before:
                    continue
                if after and due and due < after:
                    continue
                out.append(_task_summary(t, names))
            return {
                "plan": {"id": plan.get("id"), "title": plan.get("title")},
                "labels": list(names.values()),
                "buckets": [{"id": b.get("id"), "name": b.get("name")} for b in buckets],
                "tasks": out[:limit],
                "truncated": truncated or len(out) > limit,
            }

    async def planner_get_task(self, task_id: PlannerId) -> dict[str, Any]:
        """Get one task with its labels (by name), description, checklist and links."""
        async with _guard():
            _, token = await self._token()
            task = await self._graph.task(token, task_id)
            details = await self._graph.task_details(token, task_id)
            names = _label_names(await self._graph.plan_details(token, str(task.get("planId"))))
            out = _task_summary(task, names)
            out.update(
                {
                    "startDateTime": task.get("startDateTime"),
                    "createdDateTime": task.get("createdDateTime"),
                    "completedDateTime": task.get("completedDateTime"),
                }
            )
            out.update(_details_summary(details))
            return out

    async def planner_my_tasks(
        self,
        status: Literal["open", "completed", "all"] = "open",
        limit: Annotated[int, Field(ge=1, le=MAX_TASKS)] = 100,
    ) -> dict[str, Any]:
        """List tasks assigned to you across all your plans."""
        async with _guard():
            _, token = await self._token()
            tasks, truncated = await self._graph.my_tasks(token, MAX_TASKS)
            out = [
                _task_summary(t)
                for t in tasks
                if status == "all" or (t.get("percentComplete") == 100) == (status == "completed")
            ]
            return {"tasks": out[:limit], "truncated": truncated or len(out) > limit}

    # write -------------------------------------------------------------------------------------
    async def planner_create_task(
        self,
        plan_id: PlannerId,
        title: Title,
        bucket_id: Annotated[str | None, Field(pattern=PLANNER_ID)] = None,
        due_date: DateStr | None = None,
        priority: Priority | None = None,
        description: Notes | None = None,
        checklist: Annotated[list[str] | None, Field(max_length=MAX_CHECKLIST)] = None,
        assignees: Annotated[list[str] | None, Field(max_length=20)] = None,
        labels: LabelList = None,
    ) -> dict[str, Any]:
        """Create a task in a plan (optionally with bucket, due date, priority, notes, checklist, assignees, labels).

        Without bucket_id the task goes in the plan's first bucket (the one Planner shows first, by orderHint).
        labels are label names as shown by planner_get_plan; an unknown name is an error.
        """
        async with _guard():
            principal, token = await self._token()
            body: dict[str, Any] = {"planId": plan_id, "title": title}
            if not bucket_id:
                buckets, _ = await self._graph.plan_buckets(token, plan_id)
                bucket_id = _first_bucket(buckets)
            if bucket_id:
                body["bucketId"] = bucket_id
            names: dict[str, str] | None = None
            if labels:
                names = _label_names(await self._graph.plan_details(token, plan_id))
                body["appliedCategories"] = dict.fromkeys(sorted(_label_keys(labels, names)), True)
            if due_date:
                body["dueDateTime"] = _due(due_date)
            if priority:
                body["priority"] = PRIORITIES[priority]
            if assignees:
                body["assignments"] = {
                    await self._user_id(token, principal, a): {
                        "@odata.type": "#microsoft.graph.plannerAssignment",
                        "orderHint": " !",
                    }
                    for a in assignees
                }
            task = await self._graph.create_task(token, body)
            out = _task_summary(task, names)
            if description or checklist:
                details_body: dict[str, Any] = {}
                if description:
                    details_body["description"] = description
                    details_body["previewType"] = "description"
                if checklist:
                    details_body["checklist"] = _checklist_add(checklist)
                details = await self._graph.task_details(token, str(task["id"]))
                updated = await self._graph.update_task_details(
                    token, str(task["id"]), str(details.get("@odata.etag")), details_body
                )
                if updated:
                    out.update(_details_summary(updated))
            return out

    async def planner_update_task(
        self,
        task_id: PlannerId,
        title: Title | None = None,
        due_date: DateStr | None = None,
        clear_due_date: bool = False,
        bucket_id: Annotated[str | None, Field(pattern=PLANNER_ID)] = None,
        percent_complete: Annotated[int | None, Field(ge=0, le=100)] = None,
        priority: Priority | None = None,
        description: Notes | None = None,
        checklist_add: Annotated[list[str] | None, Field(max_length=MAX_CHECKLIST)] = None,
        checklist_check: Annotated[
            list[Annotated[str, Field(pattern=CHECKLIST_ID)]] | None, Field(max_length=MAX_CHECKLIST)
        ] = None,
        checklist_uncheck: Annotated[
            list[Annotated[str, Field(pattern=CHECKLIST_ID)]] | None, Field(max_length=MAX_CHECKLIST)
        ] = None,
        labels: LabelList = None,
        add_labels: LabelList = None,
        remove_labels: LabelList = None,
        etag: ETag | None = None,
        details_etag: ETag | None = None,
    ) -> dict[str, Any]:
        """Update a task's title, due date, bucket, progress, priority, labels, notes or checklist.

        labels sets the task's labels exactly (an empty list clears them); add_labels / remove_labels change
        them incrementally. Use label names as shown by planner_get_plan; an unknown name is an error.
        Pass the etag/details_etag from planner_get_task to make sure nobody changed the task since you
        read it; otherwise the server reads the current version first. Checklist items can be added,
        checked or unchecked, not removed.
        """
        async with _guard():
            _, token = await self._token()
            task_body: dict[str, Any] = {}
            if title is not None:
                task_body["title"] = title
            if clear_due_date:
                task_body["dueDateTime"] = None
            elif due_date:
                task_body["dueDateTime"] = _due(due_date)
            if bucket_id:
                task_body["bucketId"] = bucket_id
            if percent_complete is not None:
                task_body["percentComplete"] = percent_complete
            if priority:
                task_body["priority"] = PRIORITIES[priority]
            details_body: dict[str, Any] = {}
            if description is not None:
                details_body["description"] = description
            checklist: dict[str, Any] = {}
            if checklist_add:
                checklist.update(_checklist_add(checklist_add))
            for item in checklist_check or []:
                checklist[item] = {"@odata.type": "microsoft.graph.plannerChecklistItem", "isChecked": True}
            for item in checklist_uncheck or []:
                checklist[item] = {"@odata.type": "microsoft.graph.plannerChecklistItem", "isChecked": False}
            if checklist:
                details_body["checklist"] = checklist
            label_change = labels is not None or bool(add_labels) or bool(remove_labels)
            if not task_body and not details_body and not label_change:
                raise ValueError("nothing to update")

            out: dict[str, Any] = {"id": task_id}
            names: dict[str, str] | None = None
            if label_change:
                current = await self._graph.task(token, task_id)
                names = _label_names(await self._graph.plan_details(token, str(current.get("planId"))))
                applied = {k for k, v in (current.get("appliedCategories") or {}).items() if v is True}
                if labels is not None:
                    target = _label_keys(labels, names)
                else:
                    target = (applied | _label_keys(add_labels or [], names)) - _label_keys(remove_labels or [], names)
                changes = dict.fromkeys(sorted(target - applied), True) | dict.fromkeys(sorted(applied - target), False)
                if changes:
                    task_body["appliedCategories"] = changes
                if not etag:  # read-then-write: use the version we just read
                    etag = str(current.get("@odata.etag"))
            if task_body:
                tag = _etag(etag) or str((await self._graph.task(token, task_id)).get("@odata.etag"))
                updated = await self._graph.update_task(token, task_id, tag, task_body)
                out.update(_task_summary(updated, names) if updated else {"updated": True})
            elif label_change:
                out["labels"] = _applied_labels(current, names or {})  # already as requested: nothing sent
            if details_body:
                dtag = _etag(details_etag) or str((await self._graph.task_details(token, task_id)).get("@odata.etag"))
                updated_d = await self._graph.update_task_details(token, task_id, dtag, details_body)
                out.update(_details_summary(updated_d) if updated_d else {"detailsUpdated": True})
            return out

    async def planner_complete_task(self, task_id: PlannerId, etag: ETag | None = None) -> dict[str, Any]:
        """Mark a task as complete (100%)."""
        async with _guard():
            _, token = await self._token()
            tag = _etag(etag) or str((await self._graph.task(token, task_id)).get("@odata.etag"))
            updated = await self._graph.update_task(token, task_id, tag, {"percentComplete": 100})
            return _task_summary(updated) if updated else {"id": task_id, "percentComplete": 100}

    async def planner_assign_task(
        self,
        task_id: PlannerId,
        add: Annotated[list[str] | None, Field(max_length=20)] = None,
        remove: Annotated[list[str] | None, Field(max_length=20)] = None,
        etag: ETag | None = None,
    ) -> dict[str, Any]:
        """Add or remove assignees on a task ('me' or user object IDs)."""
        async with _guard():
            principal, token = await self._token()
            if not add and not remove:
                raise ValueError("nothing to change: give add and/or remove")
            assignments: dict[str, Any] = {}
            for who in add or []:
                assignments[await self._user_id(token, principal, who)] = {
                    "@odata.type": "#microsoft.graph.plannerAssignment",
                    "orderHint": " !",
                }
            for who in remove or []:
                assignments[await self._user_id(token, principal, who)] = None
            tag = _etag(etag) or str((await self._graph.task(token, task_id)).get("@odata.etag"))
            updated = await self._graph.update_task(token, task_id, tag, {"assignments": assignments})
            return _task_summary(updated) if updated else {"id": task_id, "updated": True}

    # registration ------------------------------------------------------------------------------
    def register(self, server: MCPServer[Any]) -> list[str]:
        read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
        write = ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
        )
        catalogue: dict[str, tuple[Callable[..., Coroutine[Any, Any, dict[str, Any]]], ToolAnnotations]] = {
            "planner_list_plans": (self.planner_list_plans, read),
            "planner_get_plan": (self.planner_get_plan, read),
            "planner_get_task": (self.planner_get_task, read),
            "planner_my_tasks": (self.planner_my_tasks, read),
            "planner_create_task": (self.planner_create_task, write),
            "planner_update_task": (self.planner_update_task, write),
            "planner_complete_task": (self.planner_complete_task, write),
            "planner_assign_task": (self.planner_assign_task, write),
        }
        registered = []
        for name in self._s.allowed_tools:
            fn, ann = catalogue[name]
            server.add_tool(fn, name=name, annotations=ann)
            registered.append(name)
        return registered
