"""Entry point: ``python -m entra_planner_mcp``."""

from __future__ import annotations

import os
import sys

import uvicorn

from .app import build_app
from .config import ConfigError, Settings


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    app = build_app(settings)
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - container listens on all interfaces
        port=int(os.environ.get("PORT", "8000")),
        log_level=settings.log_level.lower(),
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=True,
    )


if __name__ == "__main__":
    main()
