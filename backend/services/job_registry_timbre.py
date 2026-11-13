"""Job registry adapter for timbre conversion / profile jobs.

Timbre jobs live behind ``timbre.TIMBRE_BACKEND`` and are read through the most
public snapshot API the backend offers (``list_snapshots`` → ``list_jobs`` →
transitional ``_jobs`` fallback). The backend module is imported lazily so a
test patching ``patch.object(timbre, "TIMBRE_BACKEND", fake)`` is always seen.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from backend.services.job_registry_common import (
    _SOURCE_SCAN_LIMIT,
    KIND_TIMBRE,
    STATUS_DONE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    _clamp_progress,
    _record,
    _text,
    normalize_status,
)


def collect_timbre(registry: Any) -> list[dict[str, Any]]:
    from backend.services import timbre

    backend = timbre.TIMBRE_BACKEND
    snapshots = _timbre_snapshots(backend)
    records: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        job_id = _text(snapshot.get("job_id"))
        if not job_id:
            continue
        status = normalize_status(snapshot.get("status"), kind=KIND_TIMBRE)
        progress = _clamp_progress(snapshot.get("progress"))
        if status == STATUS_DONE:
            progress = 1.0
        job_kind = _text(snapshot.get("kind"))
        profile_id = _text(snapshot.get("profile_id"))
        source_name = _text(snapshot.get("source_name"))
        if job_kind == "profile":
            title = f"音色匹配池 {profile_id}".strip()
        else:
            title = f"音色转换 {source_name}".strip() if source_name else "音色转换"
        records.append(
            _record(
                job_id=job_id,
                kind=KIND_TIMBRE,
                title=title,
                status=status,
                progress=progress,
                stage=_text(snapshot.get("phase")) or _text(snapshot.get("message")),
                error=_text(snapshot.get("error")),
                started_at=_text(snapshot.get("started_at")),
                updated_at=_text(snapshot.get("updated_at")) or _text(snapshot.get("started_at")),
                # Timbre jobs honour a cancel event at the two safe boundaries
                # (before queueing for the GPU and right after the lease).
                cancelable=status in (STATUS_QUEUED, STATUS_RUNNING),
                source="backend.services.timbre.TIMBRE_BACKEND",
            )
        )
    return records


def _call_snapshot_method(method: Callable[..., Any]) -> Any:
    """Prefer ``method(limit=...)`` but accept a zero-argument peer."""
    try:
        return method(limit=_SOURCE_SCAN_LIMIT)
    except TypeError:
        return method()


def _timbre_snapshots(backend: Any) -> list[dict[str, Any]]:
    """Read timbre jobs through the most public API the backend offers.

    ``timbre.py`` now exposes ``list_jobs()``; a ``list_snapshots()`` peer
    may land later, so it is preferred when present. Reading ``_jobs`` is a
    **transitional fallback** for a backend that exposes neither method and
    should be removed once a public snapshot method is guaranteed.
    """
    method = getattr(backend, "list_snapshots", None)
    if callable(method):
        return list(_call_snapshot_method(method) or [])
    method = getattr(backend, "list_jobs", None)
    if callable(method):
        return list(_call_snapshot_method(method) or [])
    # Transitional defensive fallback: no public snapshot method exists.
    jobs = getattr(backend, "_jobs", {}) or {}
    try:
        job_ids = list(jobs.keys())[-_SOURCE_SCAN_LIMIT:]
    except (AttributeError, TypeError):
        return []
    snapshots: list[dict[str, Any]] = []
    for job_id in reversed(job_ids):
        job = jobs.get(job_id)
        snapshot = getattr(job, "snapshot", None)
        if isinstance(snapshot, dict):
            snapshots.append(copy.deepcopy(snapshot))
    return snapshots
