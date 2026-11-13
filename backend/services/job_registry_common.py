"""Shared vocabulary and value shapers for the job registry adapters.

The registry is one read-only facade over several unrelated job stores, and
every store maps its own snapshot shape onto the same record. The kind
constants, the status vocabulary and the normalisation helpers here are that
shared contract; the per-kind adapters live in sibling ``job_registry_*``
modules and ``job_registry.py`` keeps the ``JobRegistry`` class.
"""

from __future__ import annotations

import math
import os
from typing import Any

KIND_SYNTHESIS = "synthesis"
KIND_DIALOGUE = "dialogue"
KIND_VOICE_PREPARE = "voice_prepare"
KIND_TIMBRE = "timbre"
KIND_TRAINING = "training"
KIND_MEDIA_ANALYSIS = "media_analysis"

KINDS = (
    KIND_SYNTHESIS,
    KIND_DIALOGUE,
    KIND_VOICE_PREPARE,
    KIND_TIMBRE,
    KIND_TRAINING,
    KIND_MEDIA_ANALYSIS,
)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_UNKNOWN,
)

# Every source is asked for a generous window before the caller's limit is
# applied, otherwise a small per-source default could hide an active job from
# ``get()`` or from a page that asks for more rows.
_SOURCE_SCAN_LIMIT = 200

# User-facing reason shared by every non-training kind.
_UNSUPPORTED_CANCEL_REASON = "该类任务暂不支持取消。"

_STATUS_ALIASES = {
    # queued / waiting
    "queued": STATUS_QUEUED,
    "pending": STATUS_QUEUED,
    "idle": STATUS_QUEUED,
    "waiting": STATUS_QUEUED,
    "created": STATUS_QUEUED,
    "starting": STATUS_QUEUED,
    # running
    "running": STATUS_RUNNING,
    "processing": STATUS_RUNNING,
    "in_progress": STATUS_RUNNING,
    "in-progress": STATUS_RUNNING,
    "started": STATUS_RUNNING,
    # done
    "done": STATUS_DONE,
    "success": STATUS_DONE,
    "succeeded": STATUS_DONE,
    "complete": STATUS_DONE,
    "completed": STATUS_DONE,
    "finish": STATUS_DONE,
    "finished": STATUS_DONE,
    "ok": STATUS_DONE,
    # failed
    "fail": STATUS_FAILED,
    "failed": STATUS_FAILED,
    "failure": STATUS_FAILED,
    "error": STATUS_FAILED,
    "errored": STATUS_FAILED,
    # cancelled
    "cancel": STATUS_CANCELLED,
    "canceled": STATUS_CANCELLED,
    "cancelled": STATUS_CANCELLED,
    "stopped": STATUS_CANCELLED,
    "aborted": STATUS_CANCELLED,
    "terminated": STATUS_CANCELLED,
    # explicit unknown
    "unknown": STATUS_UNKNOWN,
}


def normalize_status(value: Any, *, kind: str | None = None) -> str:
    """Map a source-specific status string onto the registry's vocabulary.

    ``kind`` is accepted for future source-specific rules, but the current
    stores only differ in spelling: media uses ``idle``/``fail``, training uses
    ``pending``/``fail``, while synthesis and voice preparation already use the
    canonical words. Unknown or missing values become ``"unknown"`` instead of
    leaking a raw string into the API.
    """
    if not isinstance(value, str):
        return STATUS_UNKNOWN
    key = value.strip().lower()
    if not key:
        return STATUS_UNKNOWN
    return _STATUS_ALIASES.get(key, STATUS_UNKNOWN)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, os.PathLike):
        return os.fspath(value).strip()
    return ""


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _clamp_progress(value: Any, *, percent: bool = False) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    if percent:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _first_progress(*values: Any, percent: bool = False) -> float | None:
    for value in values:
        parsed = _clamp_progress(value, percent=percent)
        if parsed is not None:
            return parsed
    return None


def _record(
    *,
    job_id: str,
    kind: str,
    title: str,
    status: str,
    progress: float | None,
    stage: str,
    error: str,
    started_at: str,
    updated_at: str,
    cancelable: bool,
    source: str,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "kind": kind,
        "title": title,
        "status": status,
        "progress": progress,
        "stage": stage,
        "error": error,
        "started_at": started_at,
        "updated_at": updated_at,
        "cancelable": cancelable,
        "source": source,
    }
