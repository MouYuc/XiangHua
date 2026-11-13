"""Job registry adapter for media analysis sessions.

Media analysis state lives in ``<gpt_sovits_root>/custom_data/media/<id>/session.json``
and is read through ``media_ingest.MediaIngestService``. The per-root service
instance is cached on the ``JobRegistry`` (``MediaIngestService.__init__``
creates its media directory, so it must not be rebuilt on every poll);
``core.load_config`` is read lazily so ``patch.object(core, "load_config")``
in tests is always seen.
"""

from __future__ import annotations

import os
from typing import Any

from backend.services.job_registry_common import (
    KIND_MEDIA_ANALYSIS,
    STATUS_DONE,
    _clamp_progress,
    _record,
    _text,
    normalize_status,
)


def media_service(registry: Any) -> Any:
    from backend.services import core, media_ingest

    config = core.load_config() or {}
    root = _text(config.get("gpt_sovits_root"))
    if not root:
        raise RuntimeError("未配置 GPT-SoVITS 根目录，无法读取媒体分析任务。")
    key = os.path.normcase(os.path.abspath(os.path.expanduser(root)))
    with registry._media_lock:
        service = registry._media_services.get(key)
        if service is None:
            factory = registry._media_service_factory or media_ingest.MediaIngestService
            # MediaIngestService.__init__ creates <root>/custom_data/media.
            service = factory(root)
            registry._media_services[key] = service
        return service


def collect_media(registry: Any) -> list[dict[str, Any]]:
    sessions = media_service(registry).list_sessions() or []
    records: list[dict[str, Any]] = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        job_id = _text(session.get("session_id"))
        if not job_id:
            continue
        job = session.get("job")
        if not isinstance(job, dict):
            job = {}
        status = normalize_status(job.get("status"), kind=KIND_MEDIA_ANALYSIS)
        progress = _clamp_progress(job.get("progress"))
        if status == STATUS_DONE:
            progress = 1.0
        started_at = _text(session.get("created_at"))
        records.append(
            _record(
                job_id=job_id,
                kind=KIND_MEDIA_ANALYSIS,
                title=_text(session.get("name")) or "未命名采集",
                status=status,
                progress=progress,
                stage=_text(job.get("stage")) or _text(job.get("message")),
                error=_text(job.get("error")),
                started_at=started_at,
                updated_at=_text(session.get("updated_at")) or started_at,
                cancelable=False,
                source="backend.services.media_ingest.session.json",
            )
        )
    return records
