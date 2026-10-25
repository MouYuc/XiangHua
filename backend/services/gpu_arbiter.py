"""Process-wide GPU lease arbitration.

Synthesis, media analysis, Whisper transcription, timbre conversion and training
all want the same card. Training runs in a child process but still allocates VRAM,
and nothing coordinated the five callers: two concurrent jobs could exhaust memory,
and a second job could swap model weights while the first was still generating.

This module is deliberately a lease, not a scheduler. Exactly one holder at a
time, FIFO waiters, an optional timeout, and a status snapshot the WebUI can show.
It does not try to pack several small jobs onto one card, and it is not visible
across processes; both are later problems.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

# Interactive work (synthesis, analysis, voice preparation, timbre) waits this
# long for a busy card before failing with a message that names the holder. A
# long background job such as training keeps its slot until it finishes; callers
# that should wait for it pass ``timeout=None`` explicitly.
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("GPU_LEASE_TIMEOUT_SECONDS", "60"))
HISTORY_LIMIT = 20
_UNSET = object()


class GpuBusyTimeout(RuntimeError):
    """Raised when another job held the GPU past the caller's timeout."""

    def __init__(self, kind: str, holder: str, waited_seconds: float) -> None:
        self.kind = kind
        self.holder = holder
        self.waited_seconds = waited_seconds
        super().__init__(
            f"GPU 正被「{holder}」占用，{kind} 等待了 {waited_seconds:.0f} 秒仍未轮到。"
            "请先结束该任务；如果是长任务，可调大环境变量 GPU_LEASE_TIMEOUT_SECONDS 后再试。"
        )


@dataclass(frozen=True)
class Lease:
    """One held lease. ``depth`` is 1 unless the same thread re-entered."""

    kind: str
    job_id: str
    label: str
    acquired_at: float
    waited_seconds: float
    thread_id: int
    depth: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "job_id": self.job_id,
            "label": self.label,
            "acquired_at": self.acquired_at,
            "held_seconds": round(max(0.0, time.time() - self.acquired_at), 1),
            "waited_seconds": round(self.waited_seconds, 3),
            "depth": self.depth,
        }


@dataclass
class _Waiter:
    kind: str
    job_id: str
    label: str
    since: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "job_id": self.job_id,
            "label": self.label,
            "waiting_seconds": round(max(0.0, time.monotonic() - self.since), 1),
        }


class GpuArbiter:
    """One process-wide GPU lease with FIFO waiters."""

    def __init__(self, *, default_timeout: float | None = None) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._current: Lease | None = None
        self._depth = 0
        self._queue: list[_Waiter] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
        self._default_timeout = DEFAULT_TIMEOUT_SECONDS if default_timeout is None else default_timeout

    # ------------------------------------------------------------------ public ---

    @contextmanager
    def lease(
        self,
        kind: str,
        *,
        job_id: str = "",
        label: str = "",
        timeout: float | None | object = _UNSET,
    ) -> Iterator[Lease]:
        """Hold the GPU for the duration of the block.

        ``timeout=None`` waits indefinitely (training, long batch jobs);
        the default comes from ``GPU_LEASE_TIMEOUT_SECONDS``. Re-entering from the
        same thread succeeds immediately so nested helpers cannot deadlock.
        """
        effective = self._default_timeout if timeout is _UNSET else timeout
        held = self._acquire(str(kind), str(job_id or ""), str(label or ""), effective)
        try:
            yield held
        finally:
            self._release()

    def status(self) -> dict[str, Any]:
        """Snapshot for ``/api/overview`` and the WebUI."""
        with self._condition:
            current = self._current
            queue = [waiter.as_dict() for waiter in self._queue]
            history = list(self._history)
        return {
            "busy": current is not None,
            "holder": current.as_dict() if current is not None else None,
            "queue": queue,
            "queue_length": len(queue),
            "default_timeout_seconds": self._default_timeout,
            "vram": _vram_snapshot(),
            "history": history[-5:],
        }

    def reset_for_tests(self) -> None:
        """Drop all state. Only tests should call this."""
        with self._condition:
            self._current = None
            self._depth = 0
            self._queue.clear()
            self._history.clear()
            self._condition.notify_all()

    # ---------------------------------------------------------------- internals ---

    def _acquire(self, kind: str, job_id: str, label: str, timeout: float | None) -> Lease:
        thread_id = threading.get_ident()
        with self._condition:
            current = self._current
            if current is not None and current.thread_id == thread_id:
                self._depth += 1
                return current

            waiter = _Waiter(kind=kind, job_id=job_id, label=label, since=time.monotonic())
            if current is not None or self._queue:
                self._queue.append(waiter)
                deadline = None if timeout is None else waiter.since + max(0.0, float(timeout))
                try:
                    while True:
                        if self._current is None and self._queue and self._queue[0] is waiter:
                            self._queue.pop(0)
                            break
                        remaining = None if deadline is None else deadline - time.monotonic()
                        if remaining is not None and remaining <= 0:
                            raise GpuBusyTimeout(kind, self._holder_label_locked(), time.monotonic() - waiter.since)
                        self._condition.wait(0.2 if remaining is None else min(0.2, remaining))
                finally:
                    if waiter in self._queue:
                        self._queue.remove(waiter)
                        self._condition.notify_all()

            lease = Lease(
                kind=kind,
                job_id=job_id,
                label=label or kind,
                acquired_at=time.time(),
                waited_seconds=time.monotonic() - waiter.since,
                thread_id=thread_id,
            )
            self._current = lease
            self._depth = 1
            return lease

    def _release(self) -> None:
        with self._condition:
            current = self._current
            if current is None or current.thread_id != threading.get_ident():
                return
            self._depth -= 1
            if self._depth > 0:
                return
            self._history.append({**current.as_dict(), "released_at": time.time()})
            self._current = None
            self._depth = 0
            self._condition.notify_all()

    def _holder_label_locked(self) -> str:
        current = self._current
        if current is None:
            return "未知任务"
        return current.label or current.kind


def _vram_snapshot() -> dict[str, float] | None:
    """Report VRAM only when CUDA is already initialised.

    Calling ``torch.cuda.is_available()`` would create a context on the first
    status poll; a read-only status endpoint must not allocate VRAM itself.
    """
    try:
        import torch

        if not torch.cuda.is_initialized():
            return None
        free, total = torch.cuda.mem_get_info()
    except Exception:
        return None
    return {
        "free_mb": round(free / (1024 * 1024), 1),
        "total_mb": round(total / (1024 * 1024), 1),
    }


GPU_ARBITER = GpuArbiter()
