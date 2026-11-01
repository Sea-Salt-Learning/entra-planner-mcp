"""Labels (Planner categories) by name, and the default bucket for new tasks."""

from __future__ import annotations

import json

from conftest import BUCKET, PLAN, TASK, call_tool

from entra_planner_mcp.tools import _applied_labels, _first_bucket, _label_keys, _label_names

DETAILS = {
    "categoryDescriptions": {
        "category1": "Urgent",
        "category2": "Client A",
        "category3": None,
        "category4": "Blocked",
        "category5": "  ",
        "category25": "Internal",
    }
}


def patches(graph, path_end=f"/planner/tasks/{TASK}"):
    return [json.loads(r.content) for r in graph.requests if r.method == "PATCH" and r.url.path.endswith(path_end)]


# ---- mapping --------------------------------------------------------------------------------------
def test_label_names_skip_unnamed():
    assert _label_names(DETAILS) == {
        "category1": "Urgent",
        "category2": "Client A",
        "category4": "Blocked",
        "category25": "Internal",
    }
    assert _label_names({}) == {}


def test_applied_labels_by_name_in_category_order():
    names = _label_names(DETAILS)
    task = {"appliedCategories": {"category25": True, "category1": True, "category3": True, "category2": False}}
    assert _applied_labels(task, names) == ["Urgent", "category3", "Internal"]  # unnamed shows its key


def test_label_keys_case_insensitive_and_unknown():
    names = _label_names(DETAILS)
    assert _label_keys(["urgent", " Client A "], names) == {"category1", "category2"}
    try:
        _label_keys(["Urgent", "Nope"], names)
    except ValueError as exc:
        assert "unknown label(s): Nope" in str(exc)
        assert "Blocked, Client A, Internal, Urgent" in str(exc)
    else:
        raise AssertionError("unknown label accepted")


def test_first_bucket_by_order_hint():
    assert _first_bucket([{"id": "b", "orderHint": "8585"}, {"id": "a", "orderHint": "8584 P!"}]) == "a"
    assert _first_bucket([]) is None


# ---- reads ----------------------------------------------------------------------------------------
def test_get_plan_returns_labels(client):
    out = call_tool(client, "planner_get_plan", {"plan_id": PLAN})["structuredContent"]
    assert out["labels"] == ["Urgent", "Client A", "Blocked", "Internal"]
    task = next(t for t in out["tasks"] if t["id"] == TASK)
    assert task["labels"] == ["Urgent", "Blocked"]
    other = next(t for t in out["tasks"] if t["id"] != TASK)
    assert other["labels"] == []


def test_get_task_returns_labels(client):
    out = call_tool(client, "planner_get_task", {"task_id": TASK})["structuredContent"]
    assert out["labels"] == ["Urgent", "Blocked"]


# ---- writes ---------------------------------------------------------------------------------------
def test_update_labels_set_exactly(client, graph):
    out = call_tool(client, "planner_update_task", {"task_id": TASK, "labels": ["client a", "Blocked"]})
    assert out["isError"] is False, out
    assert patches(graph) == [{"appliedCategories": {"category2": True, "category1": False}}]
    assert graph.task["appliedCategories"] == {"category2": True, "category4": True}
    assert out["structuredContent"]["labels"] == ["Client A", "Blocked"]
    assert graph.task["title"] != "x"  # nothing else changed
    assert all(r.headers.get("if-match") for r in graph.requests if r.method == "PATCH")


def test_update_labels_clear(client, graph):
    assert call_tool(client, "planner_update_task", {"task_id": TASK, "labels": []})["isError"] is False
    assert graph.task["appliedCategories"] == {}


def test_update_add_remove_labels(client, graph):
    out = call_tool(
        client, "planner_update_task", {"task_id": TASK, "add_labels": ["Internal"], "remove_labels": ["Urgent"]}
    )
    assert out["isError"] is False, out
    assert graph.task["appliedCategories"] == {"category4": True, "category25": True}


def test_update_unknown_label_changes_nothing(client, graph):
    before = dict(graph.task)
    out = call_tool(client, "planner_update_task", {"task_id": TASK, "title": "new", "labels": ["Nope"]})
    assert out["isError"] is True
    assert "unknown label(s): Nope" in out["content"][0]["text"]
    assert patches(graph) == []
    assert graph.task == before


def test_update_labels_with_stale_etag_conflicts(client, graph):
    out = call_tool(client, "planner_update_task", {"task_id": TASK, "labels": ["Client A"], "etag": 'W/"etag-0"'})
    assert out["isError"] is True
    assert "changed by someone else" in out["content"][0]["text"]
    assert graph.task["appliedCategories"] == {"category1": True, "category4": True}


def test_update_labels_already_as_requested_sends_nothing(client, graph):
    out = call_tool(client, "planner_update_task", {"task_id": TASK, "labels": ["Blocked", "Urgent"]})
    assert out["isError"] is False
    assert patches(graph) == []
    assert out["structuredContent"]["labels"] == ["Urgent", "Blocked"]


def test_update_without_labels_does_not_touch_them(client, graph):
    assert call_tool(client, "planner_update_task", {"task_id": TASK, "title": "renamed"})["isError"] is False
    assert patches(graph) == [{"title": "renamed"}]
    assert not [r for r in graph.requests if r.url.path.endswith("/details") and "/plans/" in r.url.path]


def test_create_with_labels_and_default_bucket(client, graph):
    out = call_tool(client, "planner_create_task", {"plan_id": PLAN, "title": "new", "labels": ["Internal", "urgent"]})
    assert out["isError"] is False, out
    post = json.loads(next(r for r in graph.requests if r.method == "POST").content)
    assert post["bucketId"] == BUCKET  # lowest orderHint, not the first in the list
    assert post["appliedCategories"] == {"category1": True, "category25": True}
    assert out["structuredContent"]["labels"] == ["Urgent", "Internal"]


def test_create_with_explicit_bucket_skips_bucket_lookup(client, graph):
    other = "BucketLaterAbcdefghijklmno1"
    assert (
        call_tool(client, "planner_create_task", {"plan_id": PLAN, "title": "x", "bucket_id": other})["isError"]
        is False
    )
    post = json.loads(next(r for r in graph.requests if r.method == "POST").content)
    assert post["bucketId"] == other
    assert "appliedCategories" not in post
    assert not [r for r in graph.requests if r.url.path.endswith("/buckets")]


def test_create_unknown_label_creates_nothing(client, graph):
    out = call_tool(client, "planner_create_task", {"plan_id": PLAN, "title": "x", "labels": ["Nope"]})
    assert out["isError"] is True
    assert not [r for r in graph.requests if r.method == "POST"]
