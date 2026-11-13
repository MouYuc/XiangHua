"""Synthesis engine adapter: the only module that knows how GPT-SoVITS works.

This is the seam the "四层分离" architecture needs: everything above it speaks
``cfg`` dicts and gets ``(audio, sample_rate)`` back, so replacing the engine
(the IndexTTS2 evaluation in ``docs/INDEX_TTS2_EVALUATION.md`` asks exactly this)
means replacing this file instead of surgery on the pipeline.

Extracted verbatim from ``backend.services.core`` in 2026-11. The facade
re-exports :class:`SynthesisBackend` and :func:`backend_cwd`, and the process-wide
instance still lives in ``core`` so ``patch.object(core, "SYNTHESIS_BACKEND")``
keeps working.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np

from backend.domain.text import (
    _ACCEPTABLE_SEGMENT_DURATION_RATIO,
    MIXED_LANGUAGE_BOUNDARY_PAUSE_MS,
    deterministic_language_seed,
    english_pronunciation_hint,
    expected_segment_duration,
    to_engine_segments,
    trim_segment_edges,
)
from backend.infra.gpu import GPU_ARBITER, GpuBusyTimeout
from backend.infra.logging import get_logger

logger = get_logger("xianghua.engine")


@contextmanager
def backend_cwd(root: str) -> Iterator[None]:
    previous = os.getcwd()
    try:
        os.chdir(root)
        yield
    finally:
        os.chdir(previous)


class SynthesisBackend:
    """Owns the process-local pure GPT-SoVITS pipeline and loaded model pair."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._root: str | None = None
        self._pipeline: Any = None
        self._loaded: tuple[str | None, str | None] = (None, None)
        self._inference_calls = threading.local()
        self._warmup = {"status": "idle", "message": "模型尚未预热。", "error": ""}

    def warmup_status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._warmup)

    def loaded_root(self) -> str | None:
        """The GPT-SoVITS tree this process actually imported, if any."""
        with self._lock:
            return self._root

    def restart_required(self, cfg: dict[str, Any]) -> bool:
        """True when the configured engine root differs from the loaded one.

        The engine imports ``GPT_SoVITS`` into ``sys.modules``, so switching
        roots in a running process would keep executing the old code while the
        new path looks configured. Detecting the mismatch lets the UI say
        "restart required" *before* the user hits generate, instead of every
        synthesis failing with a message that looks like a bug.
        """
        configured = os.path.abspath(str(cfg.get("gpt_sovits_root") or ""))
        with self._lock:
            return self._root is not None and self._root != configured

    def start_warmup(self, cfg: dict[str, Any]) -> dict[str, str]:
        with self._lock:
            if self._warmup["status"] == "warming":
                return dict(self._warmup)
            self._warmup = {"status": "warming", "message": "模型预热中。", "error": ""}

        def run() -> None:
            try:
                self.ensure(cfg)
            except GpuBusyTimeout as exc:
                # A batch or training job owns the card; that is not a broken
                # configuration, so report it as idle and let the next call retry.
                with self._lock:
                    self._warmup = {
                        "status": "idle",
                        "message": "GPU 正忙，预热稍后再试。",
                        "error": str(exc),
                    }
            except Exception as exc:
                with self._lock:
                    self._warmup = {
                        "status": "failed",
                        "message": "模型预热失败，请检查设置后重试。",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            else:
                with self._lock:
                    self._warmup = {"status": "ready", "message": "模型已就绪。", "error": ""}

        threading.Thread(target=run, daemon=True, name="synthesis-warmup").start()
        return self.warmup_status()

    def ensure(self, cfg: dict[str, Any]) -> None:
        """Load or swap the model pair while holding the GPU lease.

        ``ensure`` can reload weights, so it must never run outside a lease: a
        warm-up retry would otherwise swap models under a running batch. The lease
        is re-entrant, so callers that already hold it pay nothing.
        """
        with GPU_ARBITER.lease("model_load", label="加载 GPT-SoVITS 模型"):
            self._ensure_locked(cfg)

    def _ensure_locked(self, cfg: dict[str, Any]) -> None:
        root = os.path.abspath(str(cfg["gpt_sovits_root"]))
        with self._lock:
            pair = (str(cfg["gpt_model"]), str(cfg["sovits_model"]))
            if self._pipeline is None:
                if root not in sys.path:
                    sys.path.insert(0, root)
                engine_path = os.path.join(root, "GPT_SoVITS")
                if engine_path not in sys.path:
                    sys.path.insert(0, engine_path)
                # The application also has a top-level ``tools`` package. Add
                # GPT-SoVITS's sibling modules to that package so imports such
                # as ``tools.i18n`` resolve without replacing app utilities.
                project_tools = sys.modules.get("tools")
                project_tools_path = getattr(project_tools, "__path__", None)
                engine_tools_path = os.path.join(root, "tools")
                if project_tools_path is not None and engine_tools_path not in project_tools_path:
                    project_tools_path.append(engine_tools_path)
                with backend_cwd(root):
                    module = importlib.import_module("GPT_SoVITS.TTS_infer_pack.TTS")
                    device = "cpu"
                    try:
                        import torch

                        device = "cuda" if torch.cuda.is_available() else "cpu"
                    except Exception:
                        pass
                    pipeline_config = {
                        "device": device,
                        "is_half": device != "cpu" and os.environ.get("GPT_SOVITS_IS_HALF", "1") == "1",
                        "version": "v2Pro",
                        "t2s_weights_path": pair[0],
                        "vits_weights_path": pair[1],
                        "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
                        "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
                    }
                    self._pipeline = module.TTS(module.TTS_Config({"custom": pipeline_config}))
                self._root = root
                self._loaded = pair
            elif self._root != root:
                raise RuntimeError("已加载其他 GPT-SoVITS 环境，请重启 API 服务后再切换环境。")
            if pair != self._loaded:
                with backend_cwd(root):
                    self._pipeline.init_t2s_weights(pair[0])
                    self._pipeline.init_vits_weights(pair[1])
                self._loaded = pair

    def synth_one(
        self,
        cfg: dict[str, Any],
        text: str,
        semantic_token_cb: Any | None = None,
        text_segments: list[dict[str, Any]] | None = None,
    ) -> tuple[Any, int]:
        with self._lock:
            if self._pipeline is None:
                raise RuntimeError("推理后端未就绪，请先初始化模型。")
            with backend_cwd(str(cfg["gpt_sovits_root"])):
                request = {
                    "text": text,
                    "text_lang": cfg.get("text_lang", "zh"),
                    "ref_audio_path": cfg["ref_audio"],
                    "prompt_text": cfg["ref_text"],
                    "prompt_lang": cfg.get("prompt_lang", "zh"),
                    "top_k": int(cfg["top_k"]),
                    "top_p": float(cfg["top_p"]),
                    "temperature": float(cfg["temperature"]),
                    "text_split_method": "cut0",
                    "batch_size": 1,
                    "speed_factor": float(cfg["speed"]),
                    "fragment_interval": float(cfg["pause_second"]),
                    "sample_steps": int(cfg["sample_steps"]),
                    **({"seed": int(cfg["seed"])} if "seed" in cfg else {}),
                    "parallel_infer": False,
                    "semantic_token_callback": semantic_token_cb,
                }
                if cfg.get("mixed_eos_floor") is not None:
                    request["mixed_eos_floor"] = max(0, int(cfg["mixed_eos_floor"]))
                if cfg.get("mixed_token_cap") is not None:
                    request["mixed_token_cap"] = max(0, int(cfg["mixed_token_cap"]))
                if text_segments is not None:
                    request["text_lang"] = "segment_mixed"
                    request["text_segments"] = text_segments
                self._inference_calls.count = getattr(self._inference_calls, "count", 0) + 1
                result = self._pipeline.run(request)
                last = list(result)[-1]
                return last[1], last[0]

    def _synthesize_seeded(
        self,
        cfg: dict[str, Any],
        language: str,
        text: str,
        semantic_token_cb: Any | None = None,
        seed: int | None = None,
    ) -> tuple[Any, int, int, int]:
        base_seed = deterministic_language_seed(language, text, cfg) if seed is None else seed
        synthesis_text = english_pronunciation_hint(text) if language == "en" else text
        expected_duration = expected_segment_duration(synthesis_text, language, float(cfg.get("speed", 1)))
        attempts: list[tuple[float, Any, int, int]] = []

        for attempt in range(3):
            attempt_cfg = {
                **cfg,
                "text_lang": language,
                "seed": (base_seed + attempt * 104_729) & 0xFFFFFFFF,
            }
            attempt_token_count = 0
            attempt_frame_rate = 0

            def report_segment_token(token_count: int, frame_rate: int) -> None:
                nonlocal attempt_token_count, attempt_frame_rate
                try:
                    attempt_token_count = max(attempt_token_count, int(token_count))
                    attempt_frame_rate = max(attempt_frame_rate, int(frame_rate))
                except (TypeError, ValueError):
                    return

            audio, sample_rate = self.synth_one(
                attempt_cfg,
                synthesis_text,
                semantic_token_cb=report_segment_token,
            )
            sample_rate = int(sample_rate)
            if sample_rate <= 0:
                raise ValueError("模型返回了无效采样率。")
            audio = np.asarray(audio)
            attempts.append((len(audio) / sample_rate, audio, attempt_token_count, attempt_frame_rate))
            if attempts[-1][0] >= expected_duration * _ACCEPTABLE_SEGMENT_DURATION_RATIO:
                break

        acceptable = [item for item in attempts if item[0] >= expected_duration * _ACCEPTABLE_SEGMENT_DURATION_RATIO]
        if acceptable:
            _, audio, token_count, frame_rate = acceptable[0]
        else:
            _, audio, token_count, frame_rate = max(attempts, key=lambda item: item[0])
        if callable(semantic_token_cb):
            semantic_token_cb(token_count, frame_rate)
        return audio, sample_rate, token_count, frame_rate

    def synth_segments(
        self,
        cfg: dict[str, Any],
        segments: list[tuple[str, str]],
        semantic_token_cb: Any | None = None,
    ) -> tuple[Any, int]:
        """Synthesize marked fragments in one mixed pass when languages differ."""
        self._inference_calls.count = 0
        languages = {language for language, _ in segments}
        if len(segments) > 1 and len(languages) > 1:
            engine_segments = to_engine_segments(
                segments, boundary_pause_ms=MIXED_LANGUAGE_BOUNDARY_PAUSE_MS
            )
            try:
                return self.synth_one(
                    {**cfg, "text_lang": "segment_mixed"},
                    "",
                    semantic_token_cb=semantic_token_cb,
                    text_segments=engine_segments,
                )
            finally:
                if self._inference_calls.count == 0:
                    self._inference_calls.count = 1

        audio_parts: list[Any] = []
        sample_rate: int | None = None
        token_offset = 0
        line_seed = int(cfg["seed"]) if "seed" in cfg else deterministic_language_seed(
            "line", " ".join(text for _, text in segments), cfg
        )

        for language, text in segments:
            def report_offset_token(token_count: int, frame_rate: int) -> None:
                if callable(semantic_token_cb):
                    # B023 is a false positive here: the closure is called by the
                    # synthesis call immediately below, inside this same iteration.
                    semantic_token_cb(token_offset + token_count, frame_rate)  # noqa: B023

            audio, segment_sample_rate, segment_token_count, _segment_frame_rate = self._synthesize_seeded(
                cfg, language, text, report_offset_token, seed=line_seed
            )
            audio = trim_segment_edges(audio, segment_sample_rate)
            if sample_rate is None:
                sample_rate = segment_sample_rate
            elif segment_sample_rate != sample_rate:
                raise RuntimeError("显式语言标记分段返回了不一致的采样率。")
            audio_parts.append(audio)
            token_offset += segment_token_count

        if sample_rate is None or not audio_parts:
            raise ValueError("显式语言标记内容不能为空。")
        return np.concatenate(audio_parts), sample_rate
