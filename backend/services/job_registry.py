"""Read-only aggregation facade over the five independent job stores.

The platform grew five unrelated ways to track background work:

* synthesis jobs in ``backend.services.synthesis_jobs.SYNTHESIS_JOBS``
* single-recording voice preparation in
  ``backend.services.voice_prepare_jobs.VOICE_PREPARE_JOBS``
* timbre conversion / profile jobs behind ``timbre.TIMBRE_BACKEND``
* training jobs in ``tools.train`` plus ``.guangpu_webui/train_state.json``
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
"""

from __future__ import annotations

import copy
import math
import os
import threading
from pathlib import Path
from typing import Any, Callable

KIND_SYNTHESIS = "synthesis"
KIND_VOICE_PREPARE = "voice_prepare"
KIND_TIMBRE = "timbre"
KIND_TRAINING = "training"
KIND_MEDIA_ANALYSIS = "media_analysis"

KINDS = (
    KIND_SYNTHESIS,
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
        self._collectors = (
            (KIND_SYNTHESIS, self._collect_synthesis),
            (KIND_VOICE_PREPARE, self._collect_voice_prepare),
            (KIND_TIMBRE, self._collect_timbre),
            (KIND_TRAINING, self._collect_training),
            (KIND_MEDIA_ANALYSIS, self._collect_media),
        )

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
        """Cancel a job when its source supports it.

        Only training exposes a stop primitive (``tools.train.stop_job``).
        Everything else is explicitly reported as unsupported rather than
        silently doing nothing.
        """
        wanted = _text(job_id)
        if not wanted:
            return {"cancelled": False, "reason": "任务不存在。"}
        job = self.get(wanted)
        if job is None:
            return {"cancelled": False, "reason": "任务不存在。"}
        if job.get("kind") != KIND_TRAINING:
            return {"cancelled": False, "reason": _UNSUPPORTED_CANCEL_REASON}
        try:
            from tools import train as train_backend

            cancelled = bool(train_backend.stop_job(wanted))
        except Exception as exc:  # noqa: BLE001 - cancellation must not raise
            return {"cancelled": False, "reason": f"取消失败：{type(exc).__name__}: {exc}"}
        if cancelled:
            return {"cancelled": True, "reason": "已发送终止信号。"}
        return {"cancelled": False, "reason": "训练任务当前没有可终止的进程。"}

    # ------------------------------------------------------------- aggregation ---

    def _collect_all(self) -> tuple[list[dict[str, Any]], list[str]]:
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        for kind, collector in self._collectors:
            try:
                rows = collector()
                if rows is None:
                    continue
                for row in list(rows):
                    if isinstance(row, dict):
                        records.append(row)
            except Exception as exc:  # noqa: BLE001 - one broken source is isolated
                errors.append(f"{kind}: {type(exc).__name__}: {exc}")
        return records, errors

    # --------------------------------------------------------------- synthesis ---

    def _collect_synthesis(self) -> list[dict[str, Any]]:
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
                    title=self._synthesis_title(snapshot, total=total, completed=completed),
                    status=status,
                    progress=progress,
                    stage=_text(snapshot.get("phase")) or _text(snapshot.get("message")),
                    error=_text(snapshot.get("error")),
                    started_at=_text(snapshot.get("started_at")),
                    updated_at=_text(snapshot.get("updated_at")) or _text(snapshot.get("started_at")),
                    cancelable=False,
                    source="backend.services.synthesis_jobs.SYNTHESIS_JOBS",
                )
            )
        return records

    @staticmethod
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

    # ----------------------------------------------------------- voice_prepare ---

    def _collect_voice_prepare(self) -> list[dict[str, Any]]:
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

    # ----------------------------------------------------------------- timbre ---

    def _collect_timbre(self) -> list[dict[str, Any]]:
        from backend.services import timbre

        backend = timbre.TIMBRE_BACKEND
        snapshots = self._timbre_snapshots(backend)
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
                    cancelable=False,
                    source="backend.services.timbre.TIMBRE_BACKEND",
                )
            )
        return records

    @staticmethod
    def _call_snapshot_method(method: Callable[..., Any]) -> Any:
        """Prefer ``method(limit=...)`` but accept a zero-argument peer."""
        try:
            return method(limit=_SOURCE_SCAN_LIMIT)
        except TypeError:
            return method()

    @staticmethod
    def _timbre_snapshots(backend: Any) -> list[dict[str, Any]]:
        """Read timbre jobs through the most public API the backend offers.

        ``timbre.py`` now exposes ``list_jobs()``; a ``list_snapshots()`` peer
        may land later, so it is preferred when present. Reading ``_jobs`` is a
        **transitional fallback** for a backend that exposes neither method and
        should be removed once a public snapshot method is guaranteed.
        """
        method = getattr(backend, "list_snapshots", None)
        if callable(method):
            return list(JobRegistry._call_snapshot_method(method) or [])
        method = getattr(backend, "list_jobs", None)
        if callable(method):
            return list(JobRegistry._call_snapshot_method(method) or [])
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

    # --------------------------------------------------------------- training ---

    def _collect_training(self) -> list[dict[str, Any]]:
        from tools import train as train_backend

        state = train_backend.load_state() or {}
        steps = state.get("steps") if isinstance(state, dict) else {}
        if not isinstance(steps, dict):
            steps = {}
        order = {step: index for index, step in enumerate(getattr(train_backend, "STEPS", []) or [])}

        records: list[dict[str, Any]] = []
        covered_steps: set[str] = set()
        for entry in train_backend.running_jobs() or []:
            if not isinstance(entry, dict):
                continue
            job_id = _text(entry.get("job_id"))
            step = _text(entry.get("step"))
            if not job_id:
                continue
            covered_steps.add(step)
            step_state = steps.get(step) if isinstance(steps.get(step), dict) else {}
            records.append(self._training_record(job_id, step, step_state, running_job=entry))

        latest = self._latest_training_step(steps, order)
        if latest is not None:
            step, step_state = latest
            # A running job already produced a record above; do not list the
            # same task twice under a synthetic id.
            if step not in covered_steps:
                records.append(
                    self._training_record(f"trainstate_{step}", step, step_state, running_job=None)
                )
        return records

    @staticmethod
    def _latest_training_step(
        steps: dict[str, Any], order: dict[str, int]
    ) -> tuple[str, dict[str, Any]] | None:
        latest: tuple[tuple[int, str, int], str, dict[str, Any]] | None = None
        for step, step_state in steps.items():
            if not isinstance(step_state, dict):
                continue
            status = normalize_status(step_state.get("status"), kind=KIND_TRAINING)
            if status == STATUS_UNKNOWN:
                # Pending/never-started steps are history, not a recent task.
                continue
            timestamp = _text(step_state.get("started")) or _text(step_state.get("ended"))
            key = (
                2 if status == STATUS_RUNNING else 1,
                timestamp,
                order.get(step, -1),
            )
            if latest is None or key > latest[0]:
                latest = (key, step, step_state)
        if latest is None:
            return None
        return latest[1], latest[2]

    @staticmethod
    def _training_record(
        job_id: str,
        step: str,
        step_state: dict[str, Any],
        *,
        running_job: dict[str, Any] | None,
    ) -> dict[str, Any]:
        progress_meta = step_state.get("progress")
        if not isinstance(progress_meta, dict):
            progress_meta = {}
        status = normalize_status(step_state.get("status"), kind=KIND_TRAINING)
        if running_job is not None:
            status = STATUS_RUNNING
        progress = _first_progress(progress_meta.get("overall_percent"), percent=True)
        if progress is None:
            progress = _first_progress(progress_meta.get("global_percent"), percent=True)
        if progress is None:
            progress = _first_progress(progress_meta.get("stage_percent"), percent=True)
        if status == STATUS_DONE:
            progress = 1.0

        stage = _text(progress_meta.get("stage"))
        step_title = _text(step_state.get("title")) or _text(
            (running_job or {}).get("title")
        ) or step
        if step == "train" and stage in {"s1", "s2"}:
            title = f"训练 {stage}"
        else:
            title = step_title or f"训练 {step}"
        started_at = _text(step_state.get("started"))
        updated_at = _text(step_state.get("ended")) or started_at
        error = ""
        if status == STATUS_FAILED:
            error = _text(step_state.get("note")) or "训练任务失败，请查看训练日志。"
        return _record(
            job_id=job_id,
            kind=KIND_TRAINING,
            title=title,
            status=status,
            progress=progress,
            stage=stage or step_title,
            error=error,
            started_at=started_at,
            updated_at=updated_at,
            cancelable=running_job is not None and status == STATUS_RUNNING,
            source=(
                "tools.train.running_jobs()"
                if running_job is not None
                else "tools.train.load_state()"
            ),
        )

    # ---------------------------------------------------------- media analysis ---

    def _media_service(self) -> Any:
        from backend.services import core, media_ingest

        config = core.load_config() or {}
        root = _text(config.get("gpt_sovits_root"))
        if not root:
            raise RuntimeError("未配置 GPT-SoVITS 根目录，无法读取媒体分析任务。")
        key = os.path.normcase(os.path.abspath(os.path.expanduser(root)))
        with self._media_lock:
            service = self._media_services.get(key)
            if service is None:
                factory = self._media_service_factory or media_ingest.MediaIngestService
                # MediaIngestService.__init__ creates <root>/custom_data/media.
                service = factory(root)
                self._media_services[key] = service
            return service

    def _collect_media(self) -> list[dict[str, Any]]:
        sessions = self._media_service().list_sessions() or []
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


# Process-wide singleton. The FastAPI app may build per-request services, but
# the registry only holds the media-service cache, so one instance is enough.
JOB_REGISTRY = JobRegistry()
