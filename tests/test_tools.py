"""Tool behaviour against the fake Graph: allow-list, ETags, no delete, filters, rate limit."""

from __future__ import annotations

import inspect

from conftest import BUCKET, OTHER_USER, PLAN, SECRET_TITLE, TASK, USER, call_tool, make_token, rpc

from entra_planner_mcp import graph as graph_module
from entra_planner_mcp.config import ALL_TOOLS, READ_TOOLS, WRITE_TOOLS
from entra_planner_mcp.tools import PlannerTools


def tool_names(client):
    return [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]]


# ---- allow-list and the no-delete guarantee ----------------------------------------------------
def test_default_allow_list_is_read_only(make_client):
    c = make_client(ALLOWED_TOOLS=None)
    assert tool_names(c) == list(READ_TOOLS)
    result = call_tool(c, "planner_complete_task", {"task_id": TASK})
    assert result["isError"] is True


def test_allow_list_subset(make_client):
    c = make_client(ALLOWED_TOOLS="planner_my_tasks,planner_complete_task")
    assert tool_names(c) == ["planner_my_tasks", "planner_complete_task"]


def test_all_tools_listed_with_annotations(client):
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == list(ALL_TOOLS)
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is (t["name"] in READ_TOOLS)
        assert t["annotations"]["destructiveHint"] is False


def test_no_delete_anywhere():
    assert not [n for n in ALL_TOOLS if "delete" in n or "remove" in n]
    assert not [n for n, _ in inspect.getmembers(PlannerTools) if "delete" in n.lower()]
    assert not [n for n, _ in inspect.getmembers(graph_module.GraphClient) if "delete" in n.lower()]
    src = inspect.getsource(graph_module)
    assert ".delete(" not in src and '"DELETE"' not in src and ".request(" not in src


def test_fake_graph_never_saw_delete_or_foreign_hosts(client, graph):
    for name, args in [
        ("planner_list_plans", {}),
        ("planner_get_plan", {"plan_id": PLAN}),
        ("planner_get_task", {"task_id": TASK}),
        ("planner_my_tasks", {}),
        ("planner_complete_task", {"task_id": TASK}),
    ]:
        assert call_tool(client, name, args)["isError"] is False
    assert {r.method for r in graph.requests} <= {"GET", "PATCH", "POST"}


# ---- reads ---------------------------------------------------------------------------------------
def test_get_task_compact(client):
    out = call_tool(client, "planner_get_task", {"task_id": TASK})["structuredContent"]
    assert out["title"] == SECRET_TITLE
    assert out["etag"] == 'W/"etag-1"'
    assert out["detailsEtag"] == 'W/"detail-1"'
    assert out["checklist"] == [{"id": "c1", "title": "first", "isChecked": False}]
    assert out["assignees"] == [USER]


def test_get_plan_filters(client):
    def ids(**args):
        out = call_tool(client, "planner_get_plan", {"plan_id": PLAN, **args})["structuredContent"]
        return [t["id"] for t in out["tasks"]]

    assert ids() == [TASK, "TaskOtherAbcdefghijklmnopq1"]  # open by default
    assert ids(status="completed") == ["TaskDoneAbcdefghijklmnopqr1"]
    assert len(ids(status="all")) == 3
    assert ids(assignee="me") == [TASK]
    assert ids(assignee=OTHER_USER) == ["TaskOtherAbcdefghijklmnopq1"]
    assert ids(bucket_id=BUCKET) == [TASK]
    assert ids(due_before="2030-01-15") == [TASK]  # inclusive
    assert ids(due_after="2030-02-01") == ["TaskOtherAbcdefghijklmnopq1"]
    assert ids(limit=1) == [TASK]


def test_my_tasks(client):
    out = call_tool(client, "planner_my_tasks")["structuredContent"]
    assert [t["id"] for t in out["tasks"]] == [TASK]


def test_forbidden_plan_maps_to_clear_message(client):
    out = call_tool(client, "planner_get_plan", {"plan_id": "SomeoneElsesPlanAbcdefghij"})
    assert out["isError"] is True
    assert "not found, or you don't have access" in out["content"][0]["text"]


def test_invalid_ids_rejected_before_graph(client, graph):
    def graph_calls():
        return [r for r in graph.requests if r.url.host == "graph.microsoft.com"]

    before = len(graph_calls())
    for bad in ["../me", "a/b", "x" * 100, "short"]:
        assert call_tool(client, "planner_get_task", {"task_id": bad})["isError"] is True
    assert len(graph_calls()) == before


def test_assignee_must_be_guid_without_lookup(client):
    out = call_tool(client, "planner_assign_task", {"task_id": TASK, "add": ["someone@contoso.example"]})
    assert out["isError"] is True
    assert "object IDs" in out["content"][0]["text"]


# ---- writes and ETags ----------------------------------------------------------------------------
def test_update_with_stale_etag_conflicts(client, graph):
    out = call_tool(client, "planner_update_task", {"task_id": TASK, "title": "new", "etag": 'W/"etag-0"'})
    assert out["isError"] is True
    assert "changed by someone else" in out["content"][0]["text"]
    assert graph.task["title"] == SECRET_TITLE


def test_update_reads_then_writes_with_if_match(client, graph):
    out = call_tool(
        client,
        "planner_update_task",
        {
            "task_id": TASK,
            "title": "new title",
            "due_date": "2031-02-03",
            "priority": "urgent",
            "description": "new notes",
            "checklist_add": ["second"],
            "checklist_check": ["c1"],
        },
    )
    assert out["isError"] is False, out
    assert graph.task["title"] == "new title"
    assert graph.task["dueDateTime"] == "2031-02-03T12:00:00Z"
    assert graph.task["priority"] == 1
    assert graph.details["description"] == "new notes"
    assert graph.details["checklist"]["c1"]["isChecked"] is True
    assert len(graph.details["checklist"]) == 2
    patches = [r for r in graph.requests if r.method == "PATCH"]
    assert all(r.headers.get("if-match") for r in patches)


def test_update_with_current_etag(client, graph):
    out = call_tool(client, "planner_update_task", {"task_id": TASK, "percent_complete": 50, "etag": 'W/"etag-1"'})
    assert out["isError"] is False
    assert graph.task["percentComplete"] == 50


def test_update_requires_something(client):
    assert call_tool(client, "planner_update_task", {"task_id": TASK})["isError"] is True


def test_bad_etag_rejected(client):
    out = call_tool(client, "planner_update_task", {"task_id": TASK, "title": "x", "etag": "garbage"})
    assert out["isError"] is True


def test_complete(client, graph):
    assert call_tool(client, "planner_complete_task", {"task_id": TASK})["isError"] is False
    assert graph.task["percentComplete"] == 100


def test_assign_add_remove(client, graph):
    out = call_tool(client, "planner_assign_task", {"task_id": TASK, "add": [OTHER_USER], "remove": ["me"]})
    assert out["isError"] is False
    assert set(graph.task["assignments"]) == {OTHER_USER}


def test_create_with_details(client, graph):
    out = call_tool(
        client,
        "planner_create_task",
        {
            "plan_id": PLAN,
            "title": "brand new",
            "bucket_id": BUCKET,
            "due_date": "2031-01-01",
            "description": "notes",
            "checklist": ["a", "b"],
            "assignees": ["me"],
        },
    )
    assert out["isError"] is False, out
    post = next(r for r in graph.requests if r.method == "POST")
    assert b'"planId"' in post.content
    assert graph.details["description"] == "notes"
    assert USER in graph.task["assignments"]


def test_create_in_foreign_plan_is_forbidden(client):
    out = call_tool(client, "planner_create_task", {"plan_id": "SomeoneElsesPlanAbcdefghij", "title": "x"})
    assert out["isError"] is True
    assert "don't have access" in out["content"][0]["text"]


def test_bad_due_date(client):
    out = call_tool(client, "planner_update_task", {"task_id": TASK, "due_date": "31/01/2030xx"})
    assert out["isError"] is True


# ---- per-user rate limit ------------------------------------------------------------------------
def test_rate_limit_per_user(make_client):
    c = make_client(RATE_LIMIT_PER_MIN="3")
    results = [call_tool(c, "planner_my_tasks")["isError"] for _ in range(4)]
    assert results == [False, False, False, True]
    other = make_token({"oid": OTHER_USER})
    assert call_tool(c, "planner_my_tasks", token=other)["isError"] is False


def test_obo_uses_the_callers_token(client, tokens):
    t = make_token()
    call_tool(client, "planner_my_tasks", token=t)
    assert tokens.assertions[-1] == t


def test_write_tools_constant():
    assert set(WRITE_TOOLS) == {
        "planner_create_task",
        "planner_update_task",
        "planner_complete_task",
        "planner_assign_task",
    }
