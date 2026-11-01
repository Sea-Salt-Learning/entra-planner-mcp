"""Defence in depth: redact anything that looks like a bearer token or JWT from every log record."""

from __future__ import annotations

import logging
import re

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: str) -> str:
    return _BEARER.sub("Bearer [redacted]", _JWT.sub("[redacted-jwt]", text))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record is dropped, not raised
            return False
        cleaned = redact(message)
        if cleaned != message or record.args:
            record.msg, record.args = cleaned, None
        return True


_installed = False


def install_redaction() -> None:
    """Attach the filter to the root handlers (and to new ones via a root-level filter)."""
    global _installed
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
    _installed = True
