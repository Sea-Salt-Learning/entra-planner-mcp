"""Nothing logged may contain a token or task content."""

from __future__ import annotations

import logging

from conftest import GRAPH_TOKEN, PLAN, SECRET_TITLE, TASK, call_tool, make_token, rpc

from entra_planner_mcp.logsafe import RedactingFilter, redact


def test_no_token_or_content_in_logs(make_client, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    c = make_client(LOG_LEVEL="INFO")
    token = make_token()
    for name, args in [
        ("planner_get_task", {"task_id": TASK}),
        ("planner_get_plan", {"plan_id": PLAN}),
        ("planner_update_task", {"task_id": TASK, "title": "Secret new title", "etag": 'W/"etag-0"'}),
        ("planner_create_task", {"plan_id": PLAN, "title": "Another secret title"}),
    ]:
        call_tool(c, name, args, token=token)
    rpc(c, "tools/list", token="expired." + token.split(".", 1)[1])
    captured = capsys.readouterr()
    text = caplog.text + captured.err + captured.out
    for secret in (
        token,
        token.split(".")[1],
        GRAPH_TOKEN,
        SECRET_TITLE,
        "Secret new title",
        "Another secret title",
        "secret notes",
    ):
        assert secret not in text


def test_redaction():
    jwt_like = make_token()
    assert jwt_like not in redact(f"header Authorization: Bearer {jwt_like}")
    assert "[redacted" in redact(jwt_like)
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "token %s", (jwt_like,), None)
    assert RedactingFilter().filter(rec)
    assert jwt_like not in rec.getMessage()
