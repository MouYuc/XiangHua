"""Background jobs that turn one natural recording into a synthesis prompt.

``POST /api/reference-audio`` used to run slicing, the quality gate and ASR inside
the request. A 5m44s recording already took ~7 seconds, and longer recordings
simply held the HTTP connection open. This manager gives the browser a job id to
poll while the synchronous route stays available for compatibility.

Snapshots deliberately follow ``SynthesisJobManager`` semantics (status, phase,
progress, error, message, timestamps) so the WebUI can present both job kinds the
same way. Only polling is needed today, so the SSE subscription machinery is
omitted until a caller actually needs it.

Persistence is a deliberate gap, not an oversight. Job records live in
``self._jobs`` and die with the process, which is fine for the local single-user
server. When this needs to survive a restart or run behind multiple workers, only
the store behind ``start()`` / ``snapshot()`` / ``update()`` has to change (a
durable table or queue plus its own TTL sweep); the API contract and the WebUI
polling loop stay as they are. Do not grow thread-local state beyond that seam
before then.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.services import voice_ingest

TERMINAL_STATUSES = {"done", "failed"}
# Finished jobs are kept long enough for the browser to fetch the final snapshot
# after a slow poll, then pruned so a long-running server does not grow forever.
MAX_RETAINED_JOBS = 32
JOB_TTL_SECONDS = 60.0 * 60.0

PrepareCallable = Callable[..., dict[str, Any]]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clamp_progress(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(payload)


@dataclass
class _VoicePrepareJob:
    job_id: str
    snapshot: dict[str, Any]
    lock: threading.RLock = field(default_factory=threading.RLock)
    finished: threading.Event = field(default_factory=threading.Event)
    finished_at: float | None = None


class VoicePrepareJobManager:
    """Run voice preparation in daemon threads and expose pollable snapshots."""

    def __init__(
        self,
        *,
        max_retained_jobs: int = MAX_RETAINED_JOBS,
        ttl_seconds: float = JOB_TTL_SECONDS,
        prepare: PrepareCallable | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, _VoicePrepareJob] = {}
        self._max_retained_jobs = max(1, int(max_retained_jobs))
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._prepare = prepare

    @property
    def job_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def start(
        self,
        source_path: str | Path,
        output_dir: str | Path,
        *,
        language: str = "zh",
        prepare: PrepareCallable | None = None,
    ) -> dict[str, Any]:
        """Queue one preparation job and return its initial snapshot.

        ``prepare`` is injectable so tests (and future callers) can supply a fake
        without loading Whisper or the quality gate. Without it, the module
        function is resolved at run time, which keeps monkeypatching working.
        """
        job_id = f"voice_prep_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        created = _now()
        job = _VoicePrepareJob(
            job_id=job_id,
            snapshot={
                "job_id": job_id,
                "status": "queued",
                "phase": "queued",
                "progress": 0.0,
                "stage": "排队中",
                "result": None,
                "error": "",
                "message": "已收到录音，正在排队准备音色样本。",
                "source_path": str(source_path),
                "started_at": created,
                "updated_at": created,
            },
        )
        with self._lock:
            self._trim_locked()
            self._jobs[job_id] = job

        prepare_fn = prepare or self._prepare
        thread = threading.Thread(
            target=self._run,
            args=(job_id, source_path, output_dir, language, prepare_fn),
            daemon=True,
            name=job_id,
        )
        thread.start()

        snapshot = self.snapshot(job_id)
        if snapshot is None:  # pragma: no cover - the job was just inserted
            raise RuntimeError("音色准备任务启动失败。")
        return snapshot

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.lock:
            return _snapshot(job.snapshot)

    def list_snapshots(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent job snapshots, newest first, as deep copies.

        Read-only on purpose: this exists for the job registry / WebUI to list
        every running task without handing out the manager's internal objects.
        """
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 50
        if count == 0:
            return []
        with self._lock:
            jobs = list(self._jobs.values())[-count:]
        snapshots: list[dict[str, Any]] = []
        for job in reversed(jobs):
            with job.lock:
                snapshots.append(_snapshot(job.snapshot))
        return snapshots

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        """Block until a job reaches a terminal state (used by tests/tools)."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        job.finished.wait(timeout)
        return self.snapshot(job_id)

    def update(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.lock:
            snapshot = job.snapshot
            snapshot.update(payload)
            snapshot["job_id"] = job_id
            if "progress" in payload:
                snapshot["progress"] = _clamp_progress(payload.get("progress"))
            snapshot["updated_at"] = _now()
            if snapshot.get("status") in TERMINAL_STATUSES:
                job.finished.set()
                if job.finished_at is None:
                    job.finished_at = time.time()
            result = _snapshot(snapshot)
        if result.get("status") in TERMINAL_STATUSES:
            # Never evict the job whose final snapshot the caller is fetching.
            self._trim(protected=job_id)
        return result

    def _run(
        self,
        job_id: str,
        source_path: str | Path,
        output_dir: str | Path,
        language: str,
        prepare_fn: PrepareCallable | None,
    ) -> None:
        self.update(
            job_id,
            {
                "status": "running",
                "phase": "prepare",
                "progress": 0.01,
                "stage": "准备中",
                "message": "正在准备音色样本。",
            },
        )

        def _progress(value: float, stage: str) -> None:
            label = str(stage or "").strip() or "准备中"
            self.update(
                job_id,
                {
                    "status": "running",
                    "phase": "prepare",
                    "progress": _clamp_progress(value),
                    "stage": label,
                    "message": f"{label}…",
                },
            )

        try:
            function = prepare_fn or voice_ingest.prepare_reference_from_audio
            result = function(
                source_path,
                output_dir,
                language=language,
                progress=_progress,
            )
            if not isinstance(result, dict):
                raise TypeError("音色准备函数必须返回字典。")
        except voice_ingest.VoiceIngestError as exc:
            # User-actionable Chinese messages must reach the WebUI untouched.
            message = str(exc) or "自动准备音色样本失败。"
            self._fail(job_id, message, message)
            return
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            self._fail(job_id, f"{type(exc).__name__}: {detail}", detail)
            return

        self.update(
            job_id,
            {
                "status": "done",
                "phase": "done",
                "progress": 1.0,
                "stage": "完成",
                "result": result,
                "error": "",
                "message": self._done_message(result),
            },
        )

    def _fail(self, job_id: str, error: str, message: str) -> None:
        self.update(
            job_id,
            {
                "status": "failed",
                "phase": "failed",
                "stage": "失败",
                "error": error,
                "message": message,
            },
        )

    @staticmethod
    def _done_message(result: dict[str, Any]) -> str:
        message = "音色样本准备完成。"
        try:
            seconds = float(result.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds > 0:
            message += f"已选取 {seconds:.1f} 秒语音。"
        if str(result.get("ref_text") or "").strip():
            message += "原文已自动识别，可直接合成。"
        return message

    def _trim(self, protected: str | None = None) -> None:
        with self._lock:
            self._trim_locked(protected)

    def _trim_locked(self, protected: str | None = None) -> None:
        """Drop expired and excess finished jobs; running jobs are never evicted."""
        now = time.time()
        if self._ttl_seconds > 0:
            for job_id, job in list(self._jobs.items()):
                if (
                    job.finished.is_set()
                    and job.finished_at is not None
                    and now - job.finished_at > self._ttl_seconds
                    and job_id != protected
                ):
                    self._jobs.pop(job_id, None)
        while len(self._jobs) > self._max_retained_jobs:
            victim = next(
                (
                    job_id
                    for job_id, job in self._jobs.items()
                    if job.finished.is_set() and job_id != protected
                ),
                None,
            )
            if victim is None:
                return
            self._jobs.pop(victim, None)


# One process-wide manager: the app builds a service per request, so the job table
# must not live on a request-scoped object. Swap this for a durable store when the
# local-only constraint is lifted (see the module docstring).
VOICE_PREPARE_JOBS = VoicePrepareJobManager()
