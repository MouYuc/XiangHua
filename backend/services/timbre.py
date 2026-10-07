"""Personal-timbre conversion service (kNN-VC) with job tracking.

This is the "use my own voice" half of the product. Synthesis produces speech in
whatever voice the engine was given; this service moves that speech - or any
other recording, including a sung vocal - into the timbre of a voice profile.

Why this is a separate service instead of another ``core`` function:

* **The model is large and shares one GPU.** WavLM-Large plus its HiFi-GAN
  vocoder is roughly 1.3 GB of weights, and GPT-SoVITS synthesis is usually
  resident on the same 8 GB card. The model is therefore loaded lazily, guarded
  by a lock, and released automatically after an idle period so that a user who
  finishes a conversion is not holding VRAM that the synthesis page needs.
* **Reference audio is expensive to encode, so the result is cached.** Turning a
  voice profile's recordings into a kNN matching pool is the slow part; the pool
  is reused by every conversion for that profile. The cache key includes the
  reference file list with sizes and mtimes, so adding or re-recording audio
  invalidates the pool instead of silently converting against stale frames.
* **Conversion is long enough to need a job.** Encoding several minutes of source
  audio through WavLM-Large takes tens of seconds, so a conversion runs on a
  worker thread and is polled exactly like a synthesis job.

The WebUI never talks to ``scripts/knnvc_runtime`` directly; that module stays
the single place where the vendored upstream code and checkpoints are handled.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from backend.services import core

REPO_ROOT = core.REPO_ROOT
TIMBRE_DIR = core.CONFIG_DIR / "timbre"
PROFILE_CACHE_DIR = TIMBRE_DIR / "matching_sets"
OUTPUT_ROOT = REPO_ROOT / "output" / "vc" / "webui"

# The paper reports diminishing returns past five minutes of reference speech, so
# a profile is capped there; beyond that the cache only grows and conversion gets
# slower without sounding better.
MAX_REFERENCE_SECONDS = 300.0

# Free the encoder after this many seconds of inactivity. Long enough that a user
# bouncing between two conversions keeps the model hot, short enough that the
# synthesis page gets its VRAM back without a restart.
IDLE_UNLOAD_SECONDS = 600.0

AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}


def _scripts_dir() -> Path:
    """Return the repository ``scripts`` directory, importable as a plain package."""
    candidate = REPO_ROOT / "scripts"
    if not candidate.is_dir():
        raise RuntimeError(f"scripts directory missing: {candidate}")
    return candidate


def _load_runtime():
    """Import the kNN-VC runtime lazily.

    Importing torch at module import time would pull several hundred megabytes of
    native libraries into every WebUI process start, including the many requests
    that never touch timbre conversion.
    """
    scripts = str(_scripts_dir())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import knnvc_runtime  # noqa: WPS433 (deliberate lazy import)

    return knnvc_runtime


def reference_files(profile_id: str, config: dict[str, Any]) -> list[Path]:
    """List the reference recordings that define a voice profile.

    A ``VoiceProfile`` carries one ``ref_audio`` clip, which is what the
    synthesis engine needs, but kNN-VC wants minutes of speech. The whole
    GPT-SoVITS slice corpus is therefore preferred when it exists, because that
    *is* the user's voice, and the single profile clip is the fallback.
    """
    slices_dir = Path(str(config.get("gpt_sovits_root") or "")) / "custom_data" / "sliced"
    found: list[Path] = []
    if slices_dir.is_dir():
        found = sorted(
            path for path in slices_dir.iterdir()
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
        )
    if found:
        return found

    fallback = Path(str(config.get("ref_audio") or ""))
    if fallback.is_file():
        return [fallback]
    return []


def corpus_digest(paths: list[Path]) -> str:
    """Hash the identity *and* the state of the reference corpus.

    Sizes and mtimes are included so that re-recording over an existing filename
    rebuilds the matching pool. A digest over names alone would keep serving
    frames of the previous take.
    """
    hasher = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        hasher.update(str(path.resolve()).encode("utf-8"))
        hasher.update(str(stat.st_size).encode("utf-8"))
        hasher.update(str(int(stat.st_mtime)).encode("utf-8"))
    return hasher.hexdigest()[:16]


@dataclass
class _Job:
    job_id: str
    snapshot: dict[str, Any]
    lock: threading.RLock = field(default_factory=threading.RLock)


class TimbreBackend:
    """Owns the kNN-VC model, the profile matching pools, and conversion jobs."""

    def __init__(self) -> None:
        self._model: Any = None
        self._model_device: str = ""
        self._model_lock = threading.RLock()
        self._last_used = 0.0

    # ---------------------------------------------------------------- model ---

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

    # -------------------------------------------------------------- profile ---

    def profile_cache_path(self, profile_id: str, paths: list[Path]) -> Path:
        """Cache file for one (profile, corpus revision) pair."""
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in profile_id)
        return PROFILE_CACHE_DIR / f"{safe_id or 'current'}-{corpus_digest(paths)}.pt"

    def profile_status(self, profile_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Describe a profile's reference corpus and whether a pool is cached."""
        paths = reference_files(profile_id, config)
        cache = self.profile_cache_path(profile_id, paths) if paths else None
        cached_frames = 0
        cached_seconds = 0.0
        if cache and cache.is_file():
            try:
                import torch

                payload = torch.load(cache, map_location="cpu", weights_only=False)
                features = payload.get("features") if isinstance(payload, dict) else payload
                cached_frames = int(features.shape[0])
                cached_seconds = round(cached_frames / 50.0, 1)
            except Exception:
                cached_frames = 0
        return {
            "profile_id": profile_id,
            "reference_files": len(paths),
            "reference_bytes": sum(p.stat().st_size for p in paths if p.is_file()),
            "cache_path": str(cache) if cache else "",
            "cache_ready": bool(cached_frames),
            "cached_frames": cached_frames,
            "cached_seconds": cached_seconds,
            "cache_digest": corpus_digest(paths) if paths else "",
            **self.vram_status(),
        }

    def build_profile(
        self,
        profile_id: str,
        config: dict[str, Any],
        progress: Callable[[str], None] | None = None,
        device: str | None = None,
    ) -> dict[str, Any]:
        """Encode a profile's reference corpus into a cached matching pool."""
        paths = reference_files(profile_id, config)
        if not paths:
            raise ValueError(
                "没有可用参考音频：请先完成录音切片，或在设置页指定参考音频。"
            )
        runtime = _load_runtime()
        model = self.model(device)
        cache = self.profile_cache_path(profile_id, paths)
        if progress:
            progress(f"正在编码 {len(paths)} 个参考文件…")
        features = runtime.build_matching_set(
            model, paths, cache=cache, progress=False
        )
        self._last_used = time.time()
        seconds = features.shape[0] / 50.0
        if seconds > MAX_REFERENCE_SECONDS:
            # Not an error: the pool is valid, just larger than the paper's
            # useful ceiling. Reported so the UI can suggest trimming.
            if progress:
                progress(
                    f"参考音 {seconds:.0f}s 超过论文建议的 {MAX_REFERENCE_SECONDS:.0f}s，"
                    "质量收益已饱和。"
                )
        return {
            "profile_id": profile_id,
            "reference_files": len(paths),
            "frames": int(features.shape[0]),
            "seconds": round(seconds, 1),
            "cache_path": str(cache),
            **self.vram_status(),
        }

    def ensure_profile(
        self,
        profile_id: str,
        config: dict[str, Any],
        progress: Callable[[str], None] | None = None,
        device: str | None = None,
    ):
        """Return the cached matching pool, building it when absent or stale."""
        paths = reference_files(profile_id, config)
        if not paths:
            raise ValueError(
                "没有可用参考音频：请先完成录音切片，或在设置页指定参考音频。"
            )
        runtime = _load_runtime()
        cache = self.profile_cache_path(profile_id, paths)
        if cache.is_file():
            model = self.model(device)
            return runtime.build_matching_set(model, paths, cache=cache, progress=False)
        self.build_profile(profile_id, config, progress=progress, device=device)
        model = self.model(device)
        return runtime.build_matching_set(model, paths, cache=cache, progress=False)

    # ------------------------------------------------------------ conversion ---

    def convert(
        self,
        source: Path,
        profile_id: str,
        config: dict[str, Any],
        topk: int = 4,
        device: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Convert one source recording into the profile's timbre."""
        if not source.is_file():
            raise ValueError(f"源音频不存在：{source}")
        if topk < 1:
            raise ValueError("topk 必须 >= 1。")

        runtime = _load_runtime()
        matching_set = self.ensure_profile(profile_id, config, progress=progress, device=device)
        if topk > matching_set.shape[0]:
            raise ValueError(
                f"topk={topk} 超过匹配池帧数 {matching_set.shape[0]}，请提供更多参考音。"
            )

        model = self.model(device)
        if progress:
            progress(f"正在转换 topk={topk}…")
        started = time.time()
        converted = runtime.convert(model, source, matching_set, topk=topk)

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

    # ------------------------------------------------------------------ jobs ---

    def start_conversion(
        self,
        source: Path,
        profile_id: str,
        config: dict[str, Any],
        topk: int = 4,
        device: str | None = None,
    ) -> dict[str, Any]:
        """Run a conversion on a worker thread and return its first snapshot."""
        job_id = f"timbre_{int(time.time() * 1000)}"
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

        def _run() -> None:
            try:
                result = self.convert(
                    source, profile_id, config, topk=topk, device=device, progress=_progress
                )
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
                        f"完成：{result['file_name']}（{result['duration']}s，"
                        f"耗时 {result['seconds_taken']}s）"
                    ),
                },
            )

        threading.Thread(target=_run, daemon=True, name=job_id).start()
        return self.snapshot(job_id) or {}

    def start_profile_build(
        self, profile_id: str, config: dict[str, Any], device: str | None = None
    ) -> dict[str, Any]:
        """Build a profile's matching pool on a worker thread."""
        job_id = f"timbreprofile_{int(time.time() * 1000)}"
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

        def _run() -> None:
            try:
                result = self.build_profile(profile_id, config, progress=_progress, device=device)
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
                        f"匹配池就绪：{result['frames']} 帧 / {result['seconds']}s，"
                        f"来自 {result['reference_files']} 个文件。"
                    ),
                },
            )

        threading.Thread(target=_run, daemon=True, name=job_id).start()
        return self.snapshot(job_id) or {}

    _jobs: dict[str, _Job] = {}
    _jobs_lock = threading.RLock()

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        with job.lock:
            return json.loads(json.dumps(job.snapshot, ensure_ascii=False))

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


def source_dir() -> Path:
    """Where uploaded conversion sources are stored, isolated from references."""
    path = OUTPUT_ROOT / "sources"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_source(filename: str, stream: Any) -> Path:
    """Persist an uploaded source recording under a generated, safe name.

    Uploads land in their own directory rather than alongside the voice-profile
    reference audio: mixing them would let a converted song drift into the kNN
    matching pool and quietly change the user's timbre.
    """
    import shutil

    name = Path(filename or "source.wav").name
    suffix = Path(name).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValueError(f"不支持的音频格式：{suffix or '（无扩展名）'}")
    destination = source_dir() / f"{int(time.time())}_{name}"
    with destination.open("wb") as output:
        shutil.copyfileobj(stream, output)
    return destination


def disambiguate_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make repeated file names tell themselves apart in the picker.

    Synthesis runs write into per-experiment directories, so the same file name
    can legitimately appear several times with different audio behind it. Two rows
    reading ``001_你好.mp3`` are indistinguishable to the user, so a colliding name
    is qualified with its parent directory, which is the experiment name.
    """
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    for row in rows:
        if counts.get(row["label"], 0) > 1:
            parent = Path(str(row["path"])).parent.name
            if parent:
                row["label"] = f"{parent}/{row['label']}"
    return rows


def list_sources(limit: int = 30) -> list[dict[str, Any]]:
    """List audio the user can convert: synthesis history plus past uploads."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        for entry in core.load_history_with_downloads():
            path = Path(str(entry.get("file") or ""))
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "label": path.name,
                    "path": str(path),
                    "download_id": str(entry.get("download_id") or ""),
                    "origin": "synthesis",
                    "created_at": str(entry.get("time") or ""),
                    "bytes": path.stat().st_size,
                }
            )
    except Exception:
        pass

    for path in sorted(source_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "label": path.name,
                "path": str(path),
                "download_id": "",
                "origin": "upload",
                "created_at": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)
                ),
                "bytes": path.stat().st_size,
            }
        )
    return disambiguate_labels(rows[:limit])


TIMBRE_BACKEND = TimbreBackend()