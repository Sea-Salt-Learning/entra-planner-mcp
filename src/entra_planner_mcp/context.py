"""Request context shared between the auth gate and the tools (kept import-light for fast start-up)."""

from __future__ import annotations

from contextvars import ContextVar

from .auth import Principal

current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)
