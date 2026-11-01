"""Background synthesis jobs with polling and SSE-friendly state snapshots."""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from backend.infra.ids import new_job_id
from backend.infra.logging import get_logger
from backend.services import core

logger = get_logger("xianghua.jobs.synthesis")

TERMINAL_STATUSES = {"done", "failed", "cancelled"}

#: Finished jobs kept in memory so ``/api/jobs`` can show recent history. Before
#: this cap the dict grew for the life of the process, and every entry carried
#: its full output list (text, pronunciation plan, takes, provenance paths).
MAX_RETAINED_JOBS = 200
#: Bound on a single SSE subscriber's backlog. Each queued snapshot carries every
#: output produced so far, so an undrained browser tab could accumulate hundreds
#: of megabytes while the producer thread kept appending.
SUBSCRIBER_QUEUE_MAX = 64
#: Minimum wall time between snapshot writes to disk. Snapshots used to be
#: re-serialised on every progress tick; writes now land on phase boundaries and
#: at most this often in between.
PERSIST_MIN_INTERVAL_SECONDS = 2.0


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _copy_outputs(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in outputs]


def _snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    if isinstance(data.get("outputs"), list):
        data["outputs"] = _copy_outputs(data["outputs"])
    return data


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]


@dataclass
class _SynthesisJob:
    job_id: str
    snapshot: dict[str, Any]
    revision: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)
    subscribers: list[_Subscriber] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    last_persisted_at: float = 0.0
    # Set by ``cancel()``; polled between lines by ``core.synthesize_lines``.
    cancel_event: threading.Event = field(default_factory=threading.Event)


class SynthesisJobManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, _SynthesisJob] = {}
        self._store: Any = None  # 可选 JobStore：启用后快照落盘，重启可恢复

    def enable_persistence(self, store: Any, restore_limit: int = 30) -> None:
        """Persist snapshots to ``store`` and re-adopt recent ones at startup.

        Restored jobs that were mid-flight when the process died are marked
        failed — their worker threads are gone, so pretending they still run
        would hang the UI forever.
        """
        self._store = store
        try:
            for snapshot in store.load_recent(limit=restore_limit) or []:
                job_id = snapshot.get("job_id")
                if not isinstance(job_id, str) or not job_id or job_id in self._jobs:
                    continue
                if snapshot.get("status") not in TERMINAL_STATUSES:
                    snapshot = dict(snapshot)
                    snapshot["status"] = "failed"
                    snapshot["error"] = "服务重启，任务中断。"
                    snapshot["message"] = "服务重启，任务中断。"
                    snapshot["ended_at"] = _now()
                job = _SynthesisJob(job_id=job_id, snapshot=snapshot, revision=int(snapshot.get("revision") or 0))
                job.done.set()
                self._jobs[job_id] = job
        except Exception:
            # 恢复失败只影响历史可见性，绝不能让服务起不来。
            pass

    def _persist(self, snapshot: dict[str, Any] | None) -> None:
        if self._store is None or not snapshot:
            return
        try:
            self._store.save(snapshot)
        except Exception as exc:  # pragma: no cover - disk failure is not fatal
            logger.warning("任务快照落盘失败：%s", exc)

    def _persist_if_due(self, job: _SynthesisJob, snapshot: dict[str, Any]) -> None:
        """Throttled snapshot write: terminal states always land, progress ticks do not."""
        now = time.monotonic()
        terminal = snapshot.get("status") in TERMINAL_STATUSES
        with job.lock:
            due = terminal or (now - job.last_persisted_at) >= PERSIST_MIN_INTERVAL_SECONDS
            if due:
                job.last_persisted_at = now
        if due:
            self._persist(snapshot)

    def _trim_locked(self) -> None:
        """Drop the oldest finished jobs once the retained set is over budget.

        Called with ``self._lock`` held. Running jobs are never evicted.
        """
        if len(self._jobs) <= MAX_RETAINED_JOBS:
            return
        finished: list[tuple[float, str]] = []
        for job_id, job in self._jobs.items():
            with job.lock:
                if job.snapshot.get("status") in TERMINAL_STATUSES:
                    finished.append((job.last_persisted_at or 0.0, job_id))
        overflow = len(self._jobs) - MAX_RETAINED_JOBS
        for _, job_id in sorted(finished)[:overflow]:
            self._jobs.pop(job_id, None)

    def start(self, cfg: dict[str, Any], text: str, output_dir: Any | None = None) -> dict[str, Any]:
        lines = core.parse_lines(text)
        if not lines:
            raise ValueError("台词为空，请先输入内容。")
        job_id = new_job_id("synth")
        job = _SynthesisJob(
            job_id=job_id,
            snapshot={
                "job_id": job_id,
                "status": "running",
                "phase": "prepare",
                "current": 0,
                "total": len(lines),
                "completed": 0,
                "current_text": "",
                "rendered_text": "",
                "output_dir": str(output_dir or ""),
                "outputs": [],
                "error": "",
                "message": "合成任务已启动。",
                "started_at": _now(),
                "updated_at": _now(),
            },
        )
        with self._lock:
            self._jobs[job_id] = job
            self._trim_locked()
        self._persist(job.snapshot)

        def _progress(payload: dict[str, Any]) -> None:
            self.update(job_id, payload)

        def _event(payload: dict[str, Any]) -> None:
            self.publish(job_id, payload)

        def _run() -> None:
            try:
                result = core.synthesize_lines(
                    cfg,
                    text,
                    output_dir,
                    progress_cb=_progress,
                    event_cb=_event,
                    should_stop=job.cancel_event.is_set,
                )
            except core.SynthesisCancelled as exc:
                # Cancellation is not a failure: no error text, and whatever was
                # already produced stays downloadable.
                self.update(
                    job_id,
                    {
                        "status": "cancelled",
                        "phase": "cancelled",
                        "message": str(exc) or "任务已取消。",
                        "error": "",
                        "ended_at": _now(),
                    },
                )
                return
            except Exception as exc:
                self.update(
                    job_id,
                    {
                        "status": "failed",
                        "phase": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "message": str(exc),
                        "ended_at": _now(),
                    },
                )
                return
            self.update(
                job_id,
                {
                    "status": "done",
                    "phase": "done",
                    "progress": 1.0,
                    "current": result["total"],
                    "total": result["total"],
                    "completed": result["completed"],
                    "current_text": "",
                    "rendered_text": "",
                    "output_dir": str(result["output_dir"]),
                    "outputs": list(result["outputs"]),
                    "error": "",
                    "message": f"完成 {result['completed']}/{result['total']}，输出目录：{result['output_dir']}",
                    "ended_at": _now(),
                    **{
                        key: result[key]
                        for key in ("synthesis_mode", "segment_count", "acoustic_inference_calls")
                        if key in result
                    },
                },
            )

        thread = threading.Thread(target=_run, daemon=True, name=job_id)
        thread.start()
        return self.snapshot(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Ask a job to stop after the line it is currently synthesising.

        Returns ``{"cancelled": bool, "reason": str}`` so the job registry can map
        it onto an HTTP status. A job that already finished is not cancellable,
        and one that has not started yet stops before its first line.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"cancelled": False, "reason": "任务不存在。"}
        with job.lock:
            if job.snapshot.get("status") in TERMINAL_STATUSES:
                return {"cancelled": False, "reason": "任务已经结束，无法取消。"}
            job.cancel_event.set()
            job.snapshot["message"] = "正在取消，等待当前语句完成…"
        self.update(job_id, {"message": job.snapshot["message"]})
        return {"cancelled": True, "reason": "已请求取消，当前语句完成后停止。"}

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if not job:
            return None
        with job.lock:
            return _snapshot(job.snapshot)

    def active_count(self) -> int:
        """How many jobs are still queued or running (finished jobs don't count)."""
        with self._lock:
            jobs = list(self._jobs.values())
        count = 0
        for job in jobs:
            with job.lock:
                if job.snapshot.get("status") not in TERMINAL_STATUSES:
                    count += 1
        return count

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
                snapshots.append(copy.deepcopy(job.snapshot))
        return snapshots

    def update(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if not job:
            return None
        with job.lock:
            job.revision += 1
            job.snapshot.update(payload)
            job.snapshot["job_id"] = job_id
            job.snapshot["revision"] = job.revision
            job.snapshot["updated_at"] = _now()
            snapshot = _snapshot(job.snapshot)
            if snapshot.get("status") in TERMINAL_STATUSES:
                job.done.set()
            subscribers = list(job.subscribers)
        self._persist_if_due(job, snapshot)
        self._publish_to_subscribers(subscribers, {"type": "snapshot", "snapshot": snapshot, "revision": job.revision})
        return snapshot

    def publish(self, job_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if not job:
            return None
        payload = dict(event)
        if not isinstance(payload.get("type"), str):
            raise ValueError("合成事件缺少 type。")
        with job.lock:
            job.revision += 1
            payload["revision"] = job.revision
            subscribers = list(job.subscribers)
        self._publish_to_subscribers(subscribers, payload)
        return payload

    @staticmethod
    def _publish_to_subscribers(subscribers: list[_Subscriber], payload: dict[str, Any]) -> None:
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(SynthesisJobManager._enqueue, subscriber.queue, dict(payload))
            except RuntimeError:
                continue

    @staticmethod
    def _enqueue(queue: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]) -> None:
        """Push one event, dropping the oldest when the subscriber is behind.

        A slow client must lose intermediate frames rather than make the manager
        hold unbounded memory; the newest snapshot is the one that matters.
        """
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - racy drain
                pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - consumer faster than us
                pass

    def subscribe(self, job_id: str) -> tuple[dict[str, Any] | None, asyncio.Queue[dict[str, Any]]]:
        job = self._jobs.get(job_id)
        if not job:
            return None, asyncio.Queue()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        loop = asyncio.get_running_loop()
        subscriber = _Subscriber(loop=loop, queue=queue)
        with job.lock:
            job.subscribers.append(subscriber)
            snapshot = _snapshot(job.snapshot)
        return snapshot, queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        with job.lock:
            job.subscribers = [subscriber for subscriber in job.subscribers if subscriber.queue is not queue]

    async def stream(self, job_id: str):
        snapshot, queue = self.subscribe(job_id)
        if snapshot is None:
            raise KeyError(job_id)
        try:
            yield {"type": "snapshot", "snapshot": snapshot}
            if snapshot.get("status") in TERMINAL_STATUSES:
                return
            while True:
                item = await queue.get()
                yield item
                if item.get("type") == "snapshot" and item.get("snapshot", {}).get("status") in TERMINAL_STATUSES:
                    return
        finally:
            self.unsubscribe(job_id, queue)


SYNTHESIS_JOBS = SynthesisJobManager()
