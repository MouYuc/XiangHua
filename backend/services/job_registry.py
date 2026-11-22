"""Read-only aggregation facade over the five independent job stores.

The platform grew five unrelated ways to track background work:

* synthesis jobs in ``backend.services.synthesis_jobs.SYNTHESIS_JOBS``
* single-recording voice preparation in
  ``backend.services.voice_prepare_jobs.VOICE_PREPARE_JOBS``
* timbre conversion / profile jobs behind ``timbre.TIMBRE_BACKEND``
* training jobs in ``backend.services.training`` plus
  ``.guangpu_webui/train_state.json``
* media analysis state in ``<gpt_sovits_root>/custom_data/media/<id>/session.json``

Before this module the WebUI had to poll five endpoints to answer "what is
running right now", and every page implemented cancellation its own way. This
facade normalises those stores into one record shape, but it **does not own or
rewrite job execution**. Each manager remains the single writer of its own
state; this module only snapshots it.

Failure policy: one broken source must never break the whole listing. Each
source is read inside its own try/except and a failure is appended to
``errors`` while every other source is still returned. ``/api/jobs`` must not
500 just because, say, a malformed media session file exists.

The per-kind collectors live in sibling ``job_registry_*`` modules (shared
vocabulary in ``job_registry_common``); this file keeps the ``JobRegistry``
class, the cancel dispatch and the process-wide singleton.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from typing import Any

from backend.services.job_registry_common import (  # noqa: F401 - facade re-exports
    _UNSUPPORTED_CANCEL_REASON,
    KIND_DIALOGUE,
    KIND_MEDIA_ANALYSIS,
    KIND_SYNTHESIS,
    KIND_TIMBRE,
    KIND_TRAINING,
    KIND_VOICE_PREPARE,
    KINDS,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    STATUSES,
    _text,
    normalize_status,
)
from backend.services.job_registry_media import collect_media
from backend.services.job_registry_synthesis import (
    collect_dialogue,
    collect_synthesis,
    collect_voice_prepare,
)
from backend.services.job_registry_timbre import collect_timbre
from backend.services.job_registry_training import collect_training

_COLLECTORS = (
    (KIND_SYNTHESIS, collect_synthesis),
    (KIND_DIALOGUE, collect_dialogue),
    (KIND_VOICE_PREPARE, collect_voice_prepare),
    (KIND_TIMBRE, collect_timbre),
    (KIND_TRAINING, collect_training),
    (KIND_MEDIA_ANALYSIS, collect_media),
)


class JobRegistry:
    """Aggregate the five job stores into one read-only listing.

    ``media_service_factory`` is injectable so tests (and future roots) can
    avoid touching the real filesystem. It receives the GPT-SoVITS root and
    must return an object with ``list_sessions()``.
    """

    def __init__(self, *, media_service_factory: Callable[[str], Any] | None = None) -> None:
        self._media_service_factory = media_service_factory
        # ``MediaIngestService.__init__`` creates its media directory, so build
        # one instance per root and reuse it instead of doing filesystem work on
        # every poll. The lock keeps concurrent first calls from racing.
        self._media_services: dict[str, Any] = {}
        self._media_lock = threading.RLock()
        self._collectors = _COLLECTORS

    # ------------------------------------------------------------- public API ---

    def list_jobs(self, limit: int = 50) -> dict[str, Any]:
        """Return ``{"jobs", "counts", "by_kind", "errors"}`` for recent jobs.

        Ordering: running jobs first, then every remaining job by ``started_at``
        descending. ``by_kind`` holds one count per kind (all five keys are
        always present so clients can render stable badges).
        """
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 50

        records, errors = self._collect_all()
        # Stable two-pass sort: newest first, then lift running jobs to the top.
        records.sort(key=lambda job: job.get("started_at") or "", reverse=True)
        records.sort(key=lambda job: 0 if job.get("status") == STATUS_RUNNING else 1)
        records = records[:count]

        counts = {status: 0 for status in STATUSES}
        by_kind = {kind: 0 for kind in KINDS}
        for job in records:
            status = job.get("status") or STATUS_UNKNOWN
            counts[status] = counts.get(status, 0) + 1
            kind = job.get("kind") or ""
            by_kind[kind] = by_kind.get(kind, 0) + 1

        return {
            "jobs": records,
            "counts": counts,
            "by_kind": by_kind,
            "errors": errors,
        }

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return one normalised job record, or ``None`` when it is unknown."""
        wanted = _text(job_id)
        if not wanted:
            return None
        records, _errors = self._collect_all()
        for job in records:
            if job.get("job_id") == wanted:
                return copy.deepcopy(job)
        return None

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Ask the owning manager to cancel a job.

        Synthesis, dialogue and timbre jobs stop between lines/chunks (they poll a
        ``cancel_event``); training signals its subprocess. Media analysis is the
        one kind that stays unsupported, and says so instead of doing nothing.
        """
        wanted = _text(job_id)
        if not wanted:
            return {"cancelled": False, "reason": "任务不存在。"}
        job = self.get(wanted)
        if job is None:
            return {"cancelled": False, "reason": "任务不存在。"}

        kind = job.get("kind")
        try:
            if kind == KIND_SYNTHESIS:
                from backend.services import synthesis_jobs

                return synthesis_jobs.SYNTHESIS_JOBS.cancel(wanted)
            if kind == KIND_DIALOGUE:
                from backend.services import dialogue as dialogue_service

                return dialogue_service.DIALOGUE_JOBS.cancel(wanted)
            if kind == KIND_TIMBRE:
                from backend.services import timbre as timbre_service

                return timbre_service.TIMBRE_BACKEND.cancel(wanted)
            if kind == KIND_TRAINING:
                from backend.services import training as train_backend

                cancelled = bool(train_backend.stop_job(wanted))
                if cancelled:
                    return {"cancelled": True, "reason": "已发送终止信号。"}
                return {"cancelled": False, "reason": "训练任务当前没有可终止的进程。"}
            if kind == KIND_MEDIA_ANALYSIS:
                from backend.services.job_registry_media import cancel_media_analysis

                return cancel_media_analysis(self, wanted)
        except Exception as exc:
            return {"cancelled": False, "reason": f"取消失败：{type(exc).__name__}: {exc}"}
        return {"cancelled": False, "reason": _UNSUPPORTED_CANCEL_REASON}

    # ------------------------------------------------------------ aggregation ---

    def _collect_all(self) -> tuple[list[dict[str, Any]], list[str]]:
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        for kind, collector in self._collectors:
            try:
                rows = collector(self)
                if rows is None:
                    continue
                for row in list(rows):
                    if isinstance(row, dict):
                        records.append(row)
            except Exception as exc:
                errors.append(f"{kind}: {type(exc).__name__}: {exc}")
        return records, errors


# Process-wide singleton. The FastAPI app may build per-request services, but
# the registry only holds the media-service cache, so one instance is enough.
JOB_REGISTRY = JobRegistry()
