"""Crash-safe filesystem writes.

Two failure modes this module exists for, both observed in this repo:

1. **Torn writes.** ``path.write_text(...)`` truncates first and writes second.
   A crash, a power cut, or a concurrent reader can therefore observe an empty
   or half-written file. ``os.replace`` on a temp file in the *same directory*
   is atomic on Windows and POSIX, so a reader sees either the old bytes or the
   new bytes, never the middle.

2. **Shared temp names.** ``path.with_suffix(".json.tmp")`` is a fixed name; two
   threads writing the same target collide and one gets ``PermissionError`` on
   Windows because the other still holds the handle. Every temp name here
   carries a UUID.

``os.replace`` on Windows can still lose the race against an antivirus scanner
or an indexer holding a handle on the target; that surfaces as ``PermissionError``
and is transient, so writes retry with a short backoff before giving up.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

# Total wall time a single atomic write may spend retrying a locked target.
_REPLACE_RETRY_ATTEMPTS = 12
_REPLACE_RETRY_DELAY_SECONDS = 0.05


def temp_path_for(target: Path) -> Path:
    """A unique sibling temp path, so concurrent writers never collide."""
    return target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.tmp")


def replace_with_retry(temporary: Path, target: Path, attempts: int = _REPLACE_RETRY_ATTEMPTS) -> None:
    """``os.replace`` that tolerates a transiently locked destination.

    Only ``PermissionError`` is retried: it is the Windows "another process has
    this file open" signal. Every other error is a real problem and propagates
    immediately so the caller can report it instead of silently retrying.
    """
    last: PermissionError | None = None
    for attempt in range(attempts):
        try:
            os.replace(temporary, target)
            return
        except PermissionError as exc:  # pragma: no cover - timing dependent
            last = exc
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS * (attempt + 1))
    temporary.unlink(missing_ok=True)
    assert last is not None
    raise last


def atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write ``payload`` to ``target`` such that readers never see a partial file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = temp_path_for(target)
    try:
        temporary.write_bytes(payload)
        replace_with_retry(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_text(target: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(target, text.encode(encoding))


def atomic_write_json(target: Path, payload: Any, *, indent: int = 2) -> None:
    atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=indent))
