"""Fast start-up: metadata, health and the 401 must not wait for the MCP SDK to load."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_mcp_sdk_not_imported_to_serve_metadata():
    code = textwrap.dedent(
        """
        import sys
        from entra_planner_mcp.app import build_app
        from entra_planner_mcp.config import Settings
        build_app(Settings.from_env({
            "ENTRA_TENANT_ID": "11111111-2222-4333-8444-555555555555",
            "ENTRA_API_CLIENT_ID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "PUBLIC_BASE_URL": "https://planner.contoso.example/mcp",
        }))
        heavy = [m for m in ("mcp", "msal", "pydantic", "starlette") if m in sys.modules]
        print(",".join(heavy))
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)  # noqa: S603
    assert out.stdout.strip() == ""


def test_unauthenticated_401_and_metadata_before_mcp_is_ready(make_client):
    c = make_client()
    assert c.post("/mcp", json={}).status_code == 401
    assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
