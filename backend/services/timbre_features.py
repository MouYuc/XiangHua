"""Matching-pool preparation for timbre conversion.

Turning a voice profile's reference recordings into a kNN matching pool is the
slow part of the pipeline, so the pool is cached per (profile, corpus revision)
and rebuilt only when ``reference_files`` changes on disk. This module owns the
pool lifecycle: the cache path, the status report the UI shows, building the
pool through the kNN-VC runtime, and the ``ensure_*`` path that reuses a cached
pool when it is still valid.

Runtime loading, model lifecycle and worker orchestration live in
``timbre_worker``; reference-corpus selection in ``timbre_refs``; the
``TimbreBackend`` facade in ``timbre.py``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.services.timbre_refs import (
    PROFILE_CACHE_DIR,
    corpus_digest,
    reference_files,
)
from backend.services.timbre_worker import TimbreCancelled, _load_runtime

# The paper reports diminishing returns past five minutes of reference speech, so
# a profile is capped there; beyond that the cache only grows and conversion gets
# slower without sounding better.
MAX_REFERENCE_SECONDS = 300.0


class TimbreProfileMixin:
    """Build, cache and describe a profile's kNN matching pool."""

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
        should_stop: Callable[[], bool] | None = None,
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
        try:
            features = runtime.build_matching_set(
                model, paths, cache=cache, progress=False, should_stop=should_stop
            )
        except runtime.Cancelled as exc:
            raise TimbreCancelled(str(exc)) from exc
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
        should_stop: Callable[[], bool] | None = None,
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
            return runtime.build_matching_set(
                model, paths, cache=cache, progress=False, should_stop=should_stop
            )
        self.build_profile(
            profile_id, config, progress=progress, device=device, should_stop=should_stop
        )
        model = self.model(device)
        return runtime.build_matching_set(
            model, paths, cache=cache, progress=False, should_stop=should_stop
        )
