"""Background synthesis jobs with polling and SSE-friendly state snapshots."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from backend.services import core

TERMINAL_STATUSES = {"done", "failed"}


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
    lock: threading.RLock = field(default_factory=threading.RLock)
    subscribers: list[_Subscriber] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)


class SynthesisJobManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, _SynthesisJob] = {}

    def start(self, cfg: dict[str, Any], text: str, output_dir: Any | None = None) -> dict[str, Any]:
        lines = core.parse_lines(text)
        if not lines:
            raise ValueError("台词为空，请先输入内容。")
        job_id = f"synth_{int(time.time() * 1000)}"
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

        def _progress(payload: dict[str, Any]) -> None:
            self.update(job_id, payload)

        def _run() -> None:
            try:
                result = core.synthesize_lines(cfg, text, output_dir, progress_cb=_progress)
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
                },
            )

        thread = threading.Thread(target=_run, daemon=True, name=job_id)
        thread.start()
        return self.snapshot(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if not job:
            return None
        with job.lock:
            return _snapshot(job.snapshot)

    def update(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if not job:
            return None
        with job.lock:
            job.snapshot.update(payload)
            job.snapshot["job_id"] = job_id
            job.snapshot["updated_at"] = _now()
            snapshot = _snapshot(job.snapshot)
            if snapshot.get("status") in TERMINAL_STATUSES:
                job.done.set()
            subscribers = list(job.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, snapshot)
            except RuntimeError:
                continue
        return snapshot

    def subscribe(self, job_id: str) -> tuple[dict[str, Any] | None, asyncio.Queue[dict[str, Any]]]:
        job = self._jobs.get(job_id)
        if not job:
            return None, asyncio.Queue()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
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
            yield snapshot
            if snapshot.get("status") in TERMINAL_STATUSES:
                return
            while True:
                item = await queue.get()
                yield item
                if item.get("status") in TERMINAL_STATUSES:
                    return
        finally:
            self.unsubscribe(job_id, queue)


SYNTHESIS_JOBS = SynthesisJobManager()
