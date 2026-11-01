"""One structured logger for the whole process.

Before this module the backend had **zero** logging calls: 32 ``except
Exception:`` blocks silently swallowed failures, and a background job that died
at 3 a.m. left no trace anywhere. This module gives those blocks somewhere to
report to without adding a dependency.

Design notes:

* stdlib ``logging`` only — the project already drags in 240 packages and this
  must never be the reason an install breaks.
* A ``ContextVar`` carries the request/job id, so a log line can be correlated
  with the HTTP request or background job that produced it without threading an
  id through every function signature.
* Output format is human-friendly by default (``XIANGHUA_LOG_JSON=1`` switches
  to one JSON object per line for a log shipper).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s]%(context)s %(message)s"

#: Current request/job correlation id. ``-`` outside a scoped block.
_context_id: ContextVar[str] = ContextVar("xianghua_log_context", default="")

_configured = False


class _ContextFilter(logging.Filter):
    """Attach the active correlation id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        current = _context_id.get()
        record.context = f"[{current}]" if current else ""
        return True


class _JsonFormatter(logging.Formatter):
    """One compact JSON object per line, for collectors that expect it."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        current = _context_id.get()
        if current:
            payload["context"] = current
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    """Install the root handler once. Safe to call repeatedly.

    ``XIANGHUA_LOG_LEVEL`` (default ``INFO``) and ``XIANGHUA_LOG_JSON=1`` are the
    only knobs; everything else is intentional default behaviour so a fresh
    checkout logs usefully with no configuration.
    """
    global _configured
    if _configured:
        return
    _configured = True

    resolved = (level or os.environ.get("XIANGHUA_LOG_LEVEL") or "INFO").upper()
    numeric = getattr(logging, resolved, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_ContextFilter())
    if os.environ.get("XIANGHUA_LOG_JSON", "0") == "1":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))

    root = logging.getLogger()
    # Replace anything a library installed at import time so the format is ours.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric)

    # These are chatty at INFO and say nothing about our own failures.
    for noisy in ("uvicorn.access", "httpx", "urllib3", "matplotlib", "PIL"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring the process on first use."""
    if not _configured:
        configure_logging()
    return logging.getLogger(name)


def current_context() -> str:
    return _context_id.get()


@contextmanager
def log_context(identifier: str) -> Iterator[None]:
    """Tag every log record emitted inside the block with ``identifier``."""
    token = _context_id.set(str(identifier or ""))
    try:
        yield
    finally:
        _context_id.reset(token)
