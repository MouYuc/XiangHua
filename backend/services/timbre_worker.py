"""kNN-VC runtime loading, model lifecycle and timbre worker orchestration.

This module owns the parts of the timbre service that touch the GPU and run on
worker threads:

* the lazily imported ``scripts/knnvc_runtime`` module (loaded by path with
  ``importlib`` — service code must not mutate ``sys.path``),
* the encoder's lifecycle (load on demand, release after an idle period so the
  synthesis page gets its VRAM back),
* the conversion step itself, and
* the job bookkeeping around it (snapshot/cancel/list, with a cancel event
  honoured at the two safe boundaries: before waiting for the card and right
  after the lease is granted).

Reference-corpus selection and matching-pool building live in
``timbre_features``; ``timbre.py`` keeps the ``TimbreBackend`` facade.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.infra.gpu import GPU_ARBITER
from backend.infra.ids import new_job_id
from backend.infra.logging import get_logger
from backend.infra.paths import REPO_ROOT
from backend.services.timbre_refs import OUTPUT_ROOT

logger = get_logger("xianghua.timbre")

# Free the encoder after this many seconds of inactivity. Long enough that a user
# bouncing between two conversions keeps the model hot, short enough that the
# synthesis page gets its VRAM back without a restart.
IDLE_UNLOAD_SECONDS = 600.0


def _load_runtime():
    """Import the kNN-VC runtime lazily.

    Importing torch at module import time would pull several hundred megabytes of
    native libraries into every WebUI process start, including the many requests
    that never touch timbre conversion.

    The runtime lives in ``scripts/`` and is not a package member, so it is
    loaded by path with ``importlib`` (and cached in ``sys.modules``) instead of
    mutating ``sys.path`` from service code.
    """
    cached = sys.modules.get("knnvc_runtime")
    if cached is not None:
        return cached
    path = REPO_ROOT / "scripts" / "knnvc_runtime.py"
    if not path.is_file():
        raise RuntimeError(f"kNN-VC runtime script missing: {path}")
    spec = importlib.util.spec_from_file_location("knnvc_runtime", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"无法从 {path} 加载 kNN-VC 运行时。")
    module = importlib.util.module_from_spec(spec)
    sys.modules["knnvc_runtime"] = module
    spec.loader.exec_module(module)
    return module


class TimbreCancelled(RuntimeError):
    """The user cancelled a conversion; partial work is discarded, not failed."""


_CANCELLED_PAYLOAD: dict[str, Any] = {
    "status": "cancelled",
    "phase": "cancelled",
    "message": "任务已取消。",
    "error": "",
}


@dataclass
class _Job:
    job_id: str
    snapshot: dict[str, Any]
    lock: threading.RLock = field(default_factory=threading.RLock)
    # Set by ``cancel()``. Timbre work is one long model call, so the event is
    # honoured at the two safe boundaries: before waiting for the card, and again
    # right after the lease is granted (the common case is "cancel while queued").
    cancel_event: threading.Event = field(default_factory=threading.Event)


class TimbreModelMixin:
    """Lifecycle of the shared WavLM-Large + HiFi-GAN matcher."""

    def _release_locked(self) -> None:
        self._model = None
        self._model_device = ""
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # pragma: no cover - releasing VRAM must never raise
            pass

    def unload(self) -> dict[str, Any]:
        """Drop the encoder and return the freed VRAM report."""
        with self._model_lock:
            was_loaded = self._model is not None
            self._release_locked()
        return {"unloaded": was_loaded, **self.vram_status()}

    def _maybe_idle_unload_locked(self) -> None:
        if self._model is None:
            return
        if time.time() - self._last_used > IDLE_UNLOAD_SECONDS:
            self._release_locked()

    def model(self, device: str | None = None) -> Any:
        """Return the loaded matcher, loading or reloading it as needed."""
        with self._model_lock:
            self._maybe_idle_unload_locked()
            requested = device or "cuda"
            if self._model is not None and self._model_device == requested:
                self._last_used = time.time()
                return self._model
            self._release_locked()
            runtime = _load_runtime()
            self._model = runtime.load_model(device=requested)
            self._model_device = requested
            self._last_used = time.time()
            return self._model

    def vram_status(self) -> dict[str, Any]:
        """Report GPU memory so the UI can explain a load failure."""
        try:
            import torch

            if not torch.cuda.is_available():
                return {"cuda": False, "loaded": self._model is not None}
            free, total = torch.cuda.mem_get_info()
            return {
                "cuda": True,
                "loaded": self._model is not None,
                "device": self._model_device or "cuda",
                "free_gb": round(free / 1024**3, 2),
                "total_gb": round(total / 1024**3, 2),
            }
        except Exception as exc:  # pragma: no cover - diagnostics only
            return {"cuda": False, "loaded": False, "error": str(exc)}


class TimbreConvertMixin:
    """The kNN matching step: source frames against the profile's pool."""

    def convert(
        self,
        source: Path,
        profile_id: str,
        config: dict[str, Any],
        topk: int = 4,
        device: str | None = None,
        progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Convert one source recording into the profile's timbre."""
        if not source.is_file():
            raise ValueError(f"源音频不存在：{source}")
        if topk < 1:
            raise ValueError("topk 必须 >= 1。")

        runtime = _load_runtime()
        matching_set = self.ensure_profile(
            profile_id, config, progress=progress, device=device, should_stop=should_stop
        )
        if topk > matching_set.shape[0]:
            raise ValueError(
                f"topk={topk} 超过匹配池帧数 {matching_set.shape[0]}，请提供更多参考音。"
            )

        model = self.model(device)
        if progress:
            progress(f"正在转换 topk={topk}…")
        started = time.time()
        try:
            converted = runtime.convert(
                model, source, matching_set, topk=topk, should_stop=should_stop
            )
        except runtime.Cancelled as exc:
            raise TimbreCancelled(str(exc)) from exc

        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = OUTPUT_ROOT / stamp
        out_path = out_dir / f"{source.stem}_topk{topk}.wav"
        runtime.save_wav(out_path, converted)
        self._last_used = time.time()

        return {
            "file": str(out_path),
            "file_name": out_path.name,
            "duration": round(converted.shape[-1] / 16000, 2),
            "seconds_taken": round(time.time() - started, 2),
            "topk": topk,
            "profile_id": profile_id,
            "matching_frames": int(matching_set.shape[0]),
            **self.vram_status(),
        }


class TimbreJobsMixin:
    """Worker-thread orchestration and job bookkeeping for timbre tasks."""

    _jobs: dict[str, _Job] = {}
    _jobs_lock = threading.RLock()

    def start_conversion(
        self,
        source: Path,
        profile_id: str,
        config: dict[str, Any],
        topk: int = 4,
        device: str | None = None,
    ) -> dict[str, Any]:
        """Run a conversion on a worker thread and return its first snapshot."""
        job_id = new_job_id("timbre")
        job = _Job(
            job_id=job_id,
            snapshot={
                "job_id": job_id,
                "kind": "convert",
                "status": "running",
                "phase": "prepare",
                "message": "音色转换任务已启动。",
                "progress": 0.0,
                "source": str(source),
                "source_name": source.name,
                "profile_id": profile_id,
                "topk": topk,
                "result": {},
                "error": "",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        with self._jobs_lock:
            self._jobs[job_id] = job

        def _progress(message: str) -> None:
            self._update(job_id, {"message": message})

        def _work() -> None:
            try:
                if job.cancel_event.is_set():
                    self._update(job_id, _CANCELLED_PAYLOAD)
                    return
                # A conversion can take minutes, so it waits for the card instead
                # of failing after the 60 s interactive timeout. The lease is
                # released as soon as the block exits, including on error.
                with GPU_ARBITER.lease(
                    "timbre",
                    job_id=job_id,
                    label=f"音色转换 {source.name}",
                    timeout=None,
                ):
                    if job.cancel_event.is_set():
                        self._update(job_id, _CANCELLED_PAYLOAD)
                        return
                    result = self.convert(
                        source,
                        profile_id,
                        config,
                        topk=topk,
                        device=device,
                        progress=_progress,
                        should_stop=job.cancel_event.is_set,
                    )
            except TimbreCancelled:
                # Cancelled mid-inference: nothing partial is kept, and this is
                # not an error the user needs to see.
                self._update(job_id, _CANCELLED_PAYLOAD)
                return
            except Exception as exc:
                self._update(
                    job_id,
                    {
                        "status": "failed",
                        "phase": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "message": str(exc),
                    },
                )
                return
            self._update(
                job_id,
                {
                    "status": "done",
                    "phase": "done",
                    "progress": 1.0,
                    "result": result,
                    "message": (
                        f"完成：{result.get('file_name', '输出文件')}"
                        f"（{result.get('duration', '?')}s，"
                        f"耗时 {result.get('seconds_taken', '?')}s）"
                    ),
                },
            )

        def _run() -> None:
            # Last-resort guard: anything raised while writing the *terminal*
            # status used to escape the thread and leave the job "running"
            # forever, so the UI polled a state that could never change.
            try:
                _work()
            except Exception as exc:
                logger.error("音色转换收尾失败：%s", exc)
                self._update(
                    job_id,
                    {
                        "status": "failed",
                        "phase": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "message": "任务异常终止，请查看服务日志。",
                    },
                )

        threading.Thread(target=_run, daemon=True, name=job_id).start()
        return self.snapshot(job_id) or {}

    def start_profile_build(
        self, profile_id: str, config: dict[str, Any], device: str | None = None
    ) -> dict[str, Any]:
        """Build a profile's matching pool on a worker thread."""
        job_id = new_job_id("timbreprofile")
        job = _Job(
            job_id=job_id,
            snapshot={
                "job_id": job_id,
                "kind": "profile",
                "status": "running",
                "phase": "encode",
                "message": "正在建立音色匹配池。",
                "progress": 0.0,
                "profile_id": profile_id,
                "result": {},
                "error": "",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        with self._jobs_lock:
            self._jobs[job_id] = job

        def _progress(message: str) -> None:
            self._update(job_id, {"message": message})

        def _work() -> None:
            try:
                if job.cancel_event.is_set():
                    self._update(job_id, _CANCELLED_PAYLOAD)
                    return
                # Encoding the reference corpus is slow and may run for minutes;
                # wait for the card rather than failing on the default timeout.
                with GPU_ARBITER.lease(
                    "timbre",
                    job_id=job_id,
                    label=f"音色匹配池 {profile_id}",
                    timeout=None,
                ):
                    if job.cancel_event.is_set():
                        self._update(job_id, _CANCELLED_PAYLOAD)
                        return
                    result = self.build_profile(
                        profile_id,
                        config,
                        progress=_progress,
                        device=device,
                        should_stop=job.cancel_event.is_set,
                    )
            except TimbreCancelled:
                # Cancelled mid-inference: nothing partial is kept, and this is
                # not an error the user needs to see.
                self._update(job_id, _CANCELLED_PAYLOAD)
                return
            except Exception as exc:
                self._update(
                    job_id,
                    {
                        "status": "failed",
                        "phase": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "message": str(exc),
                    },
                )
                return
            self._update(
                job_id,
                {
                    "status": "done",
                    "phase": "done",
                    "progress": 1.0,
                    "result": result,
                    "message": (
                        f"匹配池就绪：{result.get('frames', '?')} 帧 / "
                        f"{result.get('seconds', '?')}s，来自 "
                        f"{result.get('reference_files', '?')} 个文件。"
                    ),
                },
            )

        def _run() -> None:
            try:
                _work()
            except Exception as exc:
                logger.error("匹配池构建收尾失败：%s", exc)
                self._update(
                    job_id,
                    {
                        "status": "failed",
                        "phase": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "message": "任务异常终止，请查看服务日志。",
                    },
                )

        threading.Thread(target=_run, daemon=True, name=job_id).start()
        return self.snapshot(job_id) or {}

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.lock:
            return json.loads(json.dumps(job.snapshot, ensure_ascii=False))

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued conversion, or signal a running one to stop."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"cancelled": False, "reason": "任务不存在。"}
        with job.lock:
            if job.snapshot.get("status") in {"done", "failed", "cancelled"}:
                return {"cancelled": False, "reason": "任务已经结束，无法取消。"}
            job.cancel_event.set()
        self._update(job_id, {"message": "正在取消…"})
        return {"cancelled": True, "reason": "已请求取消；若已进入模型推理，会在本次完成后停。"}

    def _update(self, job_id: str, payload: dict[str, Any]) -> None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return
        with job.lock:
            job.snapshot.update(payload)
            job.snapshot["job_id"] = job_id
            job.snapshot["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._jobs_lock:
            ids = list(self._jobs)[-limit:]
        return [snapshot for job_id in reversed(ids) if (snapshot := self.snapshot(job_id))]
