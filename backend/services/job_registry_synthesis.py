"""Job registry adapters for the in-process snapshot stores.

Synthesis, dialogue and voice-prepare jobs are all tracked by a manager that
exposes ``list_snapshots(limit=...)`` over recent runs. Each collector here
normalises that store's snapshot dict into the registry record shape. Services
are imported lazily inside the functions so test patches applied to the service
module (``patch.object(synthesis_jobs.SYNTHESIS_JOBS, ...)``) are always seen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.services.job_registry_common import (
    _SOURCE_SCAN_LIMIT,
    KIND_DIALOGUE,
    KIND_SYNTHESIS,
    KIND_VOICE_PREPARE,
    STATUS_DONE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    _as_int,
    _clamp_progress,
    _record,
    _text,
    normalize_status,
)


def collect_synthesis(registry: Any) -> list[dict[str, Any]]:
    from backend.services import synthesis_jobs

    snapshots = synthesis_jobs.SYNTHESIS_JOBS.list_snapshots(limit=_SOURCE_SCAN_LIMIT)
    records: list[dict[str, Any]] = []
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        job_id = _text(snapshot.get("job_id"))
        if not job_id:
            continue
        status = normalize_status(snapshot.get("status"), kind=KIND_SYNTHESIS)
        total = _as_int(snapshot.get("total"))
        completed = _as_int(snapshot.get("completed"))
        if completed is None:
            completed = _as_int(snapshot.get("current"))
        progress = _clamp_progress(snapshot.get("progress"))
        if progress is None and total and completed is not None and total > 0:
            progress = max(0.0, min(1.0, completed / total))
        if status == STATUS_DONE:
            progress = 1.0
        records.append(
            _record(
                job_id=job_id,
                kind=KIND_SYNTHESIS,
                title=_synthesis_title(snapshot, total=total, completed=completed),
                status=status,
                progress=progress,
                stage=_text(snapshot.get("phase")) or _text(snapshot.get("message")),
                error=_text(snapshot.get("error")),
                started_at=_text(snapshot.get("started_at")),
                updated_at=_text(snapshot.get("updated_at")) or _text(snapshot.get("started_at")),
                cancelable=status in (STATUS_QUEUED, STATUS_RUNNING),
                source="backend.services.synthesis_jobs.SYNTHESIS_JOBS",
            )
        )
    return records


def collect_dialogue(registry: Any) -> list[dict[str, Any]]:
    """Dialogue jobs were never surfaced here, so the task centre could not
    see them and they could not be cancelled through ``/api/jobs``."""
    from backend.services import dialogue as dialogue_service

    snapshots = dialogue_service.DIALOGUE_JOBS.list_snapshots(limit=_SOURCE_SCAN_LIMIT)
    records: list[dict[str, Any]] = []
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        job_id = _text(snapshot.get("job_id"))
        if not job_id:
            continue
        status = normalize_status(snapshot.get("status"), kind=KIND_DIALOGUE)
        total = _as_int(snapshot.get("total"))
        completed = _as_int(snapshot.get("completed"))
        progress = None
        if total and completed is not None and total > 0:
            progress = max(0.0, min(1.0, completed / total))
        if status == STATUS_DONE:
            progress = 1.0
        records.append(
            _record(
                job_id=job_id,
                kind=KIND_DIALOGUE,
                title=f"对话合成 {completed or 0}/{total or 0} 句",
                status=status,
                progress=progress,
                stage=_text(snapshot.get("message")),
                error=_text(snapshot.get("error")),
                started_at=_text(snapshot.get("started_at")),
                updated_at=_text(snapshot.get("updated_at")) or _text(snapshot.get("started_at")),
                cancelable=status in (STATUS_QUEUED, STATUS_RUNNING),
                source="backend.services.dialogue.DIALOGUE_JOBS",
            )
        )
    return records


def collect_voice_prepare(registry: Any) -> list[dict[str, Any]]:
    from backend.services import voice_prepare_jobs

    snapshots = voice_prepare_jobs.VOICE_PREPARE_JOBS.list_snapshots(limit=_SOURCE_SCAN_LIMIT)
    records: list[dict[str, Any]] = []
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        job_id = _text(snapshot.get("job_id"))
        if not job_id:
            continue
        status = normalize_status(snapshot.get("status"), kind=KIND_VOICE_PREPARE)
        progress = _clamp_progress(snapshot.get("progress"))
        if status == STATUS_DONE:
            progress = 1.0
        source_name = Path(_text(snapshot.get("source_path"))).name if snapshot.get("source_path") else ""
        title = f"音色准备 {source_name}" if source_name else "音色准备"
        records.append(
            _record(
                job_id=job_id,
                kind=KIND_VOICE_PREPARE,
                title=title,
                status=status,
                progress=progress,
                # ``stage`` is already a Chinese phrase such as "逐个候选质检".
                stage=_text(snapshot.get("stage")) or _text(snapshot.get("message")),
                error=_text(snapshot.get("error")),
                started_at=_text(snapshot.get("started_at")),
                updated_at=_text(snapshot.get("updated_at")) or _text(snapshot.get("started_at")),
                cancelable=False,
                source="backend.services.voice_prepare_jobs.VOICE_PREPARE_JOBS",
            )
        )
    return records


def _synthesis_title(
    snapshot: dict[str, Any], *, total: int | None, completed: int | None
) -> str:
    if total and total > 0:
        return f"合成 {completed or 0}/{total} 句"
    output_dir = _text(snapshot.get("output_dir"))
    if output_dir:
        name = Path(output_dir).name or output_dir
        return f"合成 {name}"
    return "合成任务"
