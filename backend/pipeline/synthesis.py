"""The synthesis pipeline: turn lines of text into delivered audio files.

This is the orchestration layer between the text front-end
(:mod:`backend.domain.text`), the engine adapter (:mod:`backend.engine.tts`) and
the surrounding app (take selection, short-output detection, manifests, history).

It deliberately imports **nothing from** :mod:`backend.services.core`. The few
facade-owned pieces it needs — the engine instance, the readback judge, the audio
writer, the history/download registries — arrive as a :class:`SynthesisHooks`
value that ``core.synthesize_lines`` builds at call time. That keeps the
dependency arrow pointing one way *and* keeps every existing
``patch.object(core, "<name>")`` seam effective.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from backend.domain.text import (
    configured_accent_level,
    deterministic_language_seed,
    expected_segment_duration,
    parse_lines,
    render_line_with_plan,
    validate_synthesis_language_text,
)
from backend.infra.logging import get_logger
from backend.services.gpu_arbiter import GPU_ARBITER
from backend.services.pronunciation import PronunciationPlan
from tools.rules import safe_name

logger = get_logger("xianghua.pipeline")

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SynthesisHooks:
    """Facade-owned collaborators, injected so the pipeline stays pure."""

    backend: Any
    judge: Any
    save_audio: Callable[..., Path]
    new_output_dir: Callable[[], Path]
    append_history: Callable[[list[dict[str, Any]]], None]
    register_download: Callable[..., str]
    write_manifest: Callable[[Path, dict[str, Any]], Path]
    write_provenance: Callable[[Path, dict[str, Any], str], str]


SHORT_OUTPUT_RATIO = 0.25


PRONUNCIATION_MANIFEST_SCHEMA_VERSION = 1


PRONUNCIATION_MANIFEST_FILENAME = "pronunciation.json"


def _short_output_floor(expected_seconds: float, text: str) -> float:
    """Duration below which a take counts as "not really synthesised".

    Normal lines keep the historical 0.6 s floor. A one-character line has no
    usable duration estimate (the estimator floors every line at 0.55 s), so it
    only needs to clear a collapse floor: measured 「噗！」 is a correct 0.36 s
    that used to be flagged, while a collapsed take stays around 0.05 s.
    """
    if _judgeable_char_count(text) < _TAKE_SELECTION_MIN_JUDGE_CHARS:
        return max(0.15, expected_seconds * SHORT_OUTPUT_RATIO)
    return max(0.6, expected_seconds * SHORT_OUTPUT_RATIO)


def _judgeable_char_count(text: str) -> int:
    """Characters a readback CER can actually score (letters/CJK digits, no punctuation)."""
    return sum(1 for char in text if char.isalnum())


_TAKE_SELECTION_CER_THRESHOLD = 0.15


_TAKE_SELECTION_MAX_TAKES = 3


_TAKE_SEED_STRIDE = 7 * 104_729


_TAKE_SELECTION_MIN_JUDGE_CHARS = 2


_READBACK_RETRY_SECONDS = 300.0


class _ReadbackJudge:
    """Lazy SenseVoice readback for take selection; unavailable means no opinion.

    ``status()`` exposes *why* it is unavailable. The reason used to be recorded
    in an attribute no endpoint read, so a user whose output quietly fell back to
    the first take had no way to find out.
    """

    def __init__(self) -> None:
        self._attempted = False
        self._last_attempt_at = 0.0
        self._model: Any = None
        self._cer: Any = None
        self.reason = ""

    def status(self) -> dict[str, Any]:
        """Current availability, for the job snapshot / status endpoint."""
        return {
            "available": self._model is not None and self._cer is not None,
            "attempted": self._attempted,
            "reason": self.reason,
        }

    def reset(self) -> None:
        """Forget a previous failure so the next call retries the load."""
        self._attempted = False
        self.reason = ""

    def _ensure(self) -> None:
        if self._model is not None and self._cer is not None:
            return
        now = time.monotonic()
        if self._attempted and (now - self._last_attempt_at) < _READBACK_RETRY_SECONDS:
            return
        self._attempted = True
        self._last_attempt_at = now
        try:
            scripts = REPO_ROOT / "scripts"
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            from asr_readback import cer as cer_function
            from five_language_verify import LanguageIdentifier

            self._cer = cer_function
            self._model = LanguageIdentifier()
            self.reason = ""
        except Exception as exc:  # a missing judge must never break synthesis
            self.reason = f"{type(exc).__name__}: {exc}"
            logger.warning("读回判定模型不可用，选 take 暂时停用：%s", self.reason)

    def cer(self, audio_path: Path, reference_text: str) -> float | None:
        """Readback CER against the rendered text; None means "no opinion"."""
        self._ensure()
        if self._model is None or self._cer is None:
            return None
        try:
            identified = self._model.identify(str(audio_path), use_itn=False)
            hypothesis = re.sub(r"<\|[^|]+\|>", "", str(identified.get("text") or "")).strip()
            if not hypothesis:
                return None
            return float(self._cer(reference_text, hypothesis))
        except Exception as exc:
            self.reason = f"{type(exc).__name__}: {exc}"
            logger.warning("读回判定失败，本句按首 take 交付：%s", self.reason)
            return None


def _emit_progress(progress_cb: Any | None, payload: dict[str, Any]) -> None:
    if callable(progress_cb):
        progress_cb(dict(payload))


def _emit_event(event_cb: Any | None, event_type: str, **payload: Any) -> None:
    if callable(event_cb):
        event_cb({"type": event_type, **payload})


def build_pronunciation_manifest(
    cfg: dict[str, Any],
    plans: list[PronunciationPlan],
    outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarise the per-line pronunciation plans into the output manifest.

    The manifest is intentionally redundant with each output's ``pronunciation``
    block: readers who only have the output directory must be able to audit what
    text was actually sent to the engine without parsing the job snapshots.
    """
    lexicon_id = "none"
    accent_pack_id = "none"
    accent_intensity = 0.0
    protected_terms: list[str] = []
    for plan in plans:
        accent_intensity = max(accent_intensity, plan.accent_intensity)
        if plan.lexicon_id == "none":
            continue
        if lexicon_id == "none":
            lexicon_id = plan.lexicon_id
            accent_pack_id = plan.accent_pack_id
        for term in plan.protected_terms:
            if term not in protected_terms:
                protected_terms.append(term)

    manifest_lines: list[dict[str, Any]] = []
    for position, output in enumerate(outputs):
        pronunciation = output.get("pronunciation")
        if not isinstance(pronunciation, dict):
            pronunciation = plans[position].to_dict() if position < len(plans) else {}
        manifest_lines.append({
            "index": int(output.get("output_index", position)) + 1,
            "source_text": str(pronunciation.get("source_text") or output.get("text") or ""),
            "rendered_text": str(output.get("rendered_text") or pronunciation.get("rendered_text") or ""),
            "language": str(pronunciation.get("language") or ""),
            "lexicon_id": str(pronunciation.get("lexicon_id") or "none"),
            "accent_pack_id": str(pronunciation.get("accent_pack_id") or "none"),
            "accent_intensity": float(pronunciation.get("accent_intensity") or 0.0),
            "protected_terms": list(pronunciation.get("protected_terms") or []),
            "replacements": [dict(item) for item in (pronunciation.get("replacements") or [])],
            "file": str(output.get("file") or ""),
            "error": str(output.get("error") or ""),
        })

    return {
        "schema_version": PRONUNCIATION_MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "accent_level": configured_accent_level(cfg),
        "text_lang": str(cfg.get("text_lang", "zh")),
        "lexicon_id": lexicon_id,
        "accent_pack_id": accent_pack_id,
        "accent_intensity": accent_intensity,
        "protected_terms": protected_terms,
        "line_count": len(manifest_lines),
        "lines": manifest_lines,
    }


class SynthesisCancelled(RuntimeError):
    """The user cancelled the job; partial results are kept, no error is shown."""


def synthesize_lines(
    cfg: dict[str, Any],
    text: str,
    output_dir: Path | None = None,
    progress_cb: Any | None = None,
    event_cb: Any | None = None,
    should_stop: Any | None = None,
    *,
    hooks: SynthesisHooks,
) -> dict[str, Any]:
    """Synthesise every line in ``text``.

    ``should_stop`` is polled between lines (never mid-inference, which cannot be
    interrupted safely): a cancelled job keeps the takes it already produced and
    raises :class:`SynthesisCancelled`.
    """
    lines = parse_lines(text)
    if not lines:
        raise ValueError("台词为空，请先输入内容。")
    validate_synthesis_language_text(text, cfg)
    # Hold the GPU for the whole batch, including ``ensure``: a second job that
    # swapped weights between two lines would silently change the voice of the
    # take in progress. The validation above stays outside the lease so a bad
    # request fails immediately instead of queueing behind a long job. A busy
    # card raises GpuBusyTimeout, which callers surface as a user-facing error.
    with GPU_ARBITER.lease("synthesis", job_id="", label=f"合成 {len(lines)} 句"):
        return run_locked(
            cfg, lines, output_dir, progress_cb, event_cb, should_stop=should_stop, hooks=hooks
        )


def run_locked(
    cfg: dict[str, Any],
    lines: list[str],
    output_dir: Path | None = None,
    progress_cb: Any | None = None,
    event_cb: Any | None = None,
    should_stop: Any | None = None,
    *,
    hooks: SynthesisHooks,
) -> dict[str, Any]:
    """Synthesise already-validated lines; the caller must hold the GPU lease."""
    hooks.backend.ensure(cfg)
    destination = output_dir or hooks.new_output_dir()
    outputs: list[dict[str, Any]] = []
    _emit_progress(progress_cb, {
        "status": "running",
        "phase": "prepare",
        "current": 0,
        "total": len(lines),
        "completed": 0,
        "current_text": "",
        "rendered_text": "",
        "output_dir": str(destination),
        "outputs": [],
        "error": "",
    })
    total_segment_count = 0
    total_acoustic_inference_calls = 0
    used_explicit_mixed = False
    line_plans: list[PronunciationPlan] = []
    for index, line in enumerate(lines, 1):
        if should_stop is not None and should_stop():
            raise SynthesisCancelled(f"任务已取消，已完成 {len(outputs)} 句。")
        rendered_segments, line_plan, rendered = render_line_with_plan(line, cfg)
        line_plans.append(line_plan)
        line_segment_count = len(rendered_segments)
        line_is_explicit_mixed = line_segment_count > 1 and len({language for language, _ in rendered_segments}) > 1
        line_synthesis_mode = "single_pass_explicit" if line_is_explicit_mixed else "legacy_single"
        total_segment_count += line_segment_count
        used_explicit_mixed = used_explicit_mixed or line_is_explicit_mixed
        base = f"{index:03d}_{safe_name(line)}"
        _emit_progress(progress_cb, {
            "status": "running",
            "phase": "generating",
            "current": index,
            "total": len(lines),
            "completed": len([item for item in outputs if item["file"]]),
            "current_text": line,
            "rendered_text": rendered,
            "output_dir": str(destination),
            "outputs": list(outputs),
            "error": "",
            "synthesis_mode": line_synthesis_mode,
            "segment_count": line_segment_count,
            "acoustic_inference_calls": 0,
        })
        semantic_token_count = 0
        semantic_frame_rate = 0
        last_token_event_at = 0.0
        last_emitted_token = -1

        def report_semantic_token(token_count: int, frame_rate: int) -> None:
            nonlocal semantic_token_count, semantic_frame_rate, last_token_event_at, last_emitted_token
            try:
                semantic_token_count = max(semantic_token_count, int(token_count))
                semantic_frame_rate = max(semantic_frame_rate, int(frame_rate))
            except (TypeError, ValueError):
                return
            now = time.monotonic()
            if semantic_token_count == last_emitted_token or (last_emitted_token >= 0 and now - last_token_event_at < 1 / 12):
                return
            last_emitted_token = semantic_token_count
            last_token_event_at = now
            _emit_event(
                event_cb,
                "semantic_token",
                output_index=index - 1,  # noqa: B023 — emitted synchronously
                token_index=semantic_token_count,
                semantic_frame_rate=semantic_frame_rate,
            )

        try:
            # Take selection: judge the delivered audio by readback, retry with a
            # derived seed only on catastrophes. Take 0 always uses the caller's
            # cfg verbatim, so behaviour for good takes is byte-identical to the
            # single-shot path.
            line_acoustic_inference_calls = 0
            take_eligible = (
                bool(cfg.get("take_selection", True))
                and not line_is_explicit_mixed
                and str(cfg.get("text_lang") or "zh") == "zh"
                and _judgeable_char_count(rendered or line) >= _TAKE_SELECTION_MIN_JUDGE_CHARS
            )
            base_line_seed = (
                int(cfg["seed"])
                if "seed" in cfg
                else deterministic_language_seed("line", rendered or line, cfg)
            )
            max_takes = _TAKE_SELECTION_MAX_TAKES if take_eligible else 1
            candidates: list[dict[str, Any]] = []
            for take_index in range(max_takes):
                take_cfg = (
                    cfg
                    if take_index == 0
                    else {**cfg, "seed": (base_line_seed + take_index * _TAKE_SEED_STRIDE) & 0xFFFFFFFF}
                )
                semantic_token_count = 0
                semantic_frame_rate = 0
                audio, sample_rate = hooks.backend.synth_segments(
                    take_cfg, rendered_segments, semantic_token_cb=report_semantic_token
                )
                line_acoustic_inference_calls += max(
                    1 if line_is_explicit_mixed else 0,
                    int(getattr(hooks.backend._inference_calls, "count", 0)),
                )
                produced_seconds = len(np.asarray(audio)) / float(sample_rate or 1)
                expected_seconds = expected_segment_duration(
                    rendered or line, "zh", float(cfg.get("speed", 1))
                )
                short_take = produced_seconds < _short_output_floor(expected_seconds, rendered or line)
                take_cer: float | None = None
                if take_eligible:
                    destination.mkdir(parents=True, exist_ok=True)
                    take_wav = destination / f".take_{index:03d}_{take_index}.wav"
                    try:
                        sf.write(str(take_wav), np.asarray(audio), int(sample_rate or 32000))
                        take_cer = hooks.judge.cer(take_wav, rendered or line)
                    finally:
                        take_wav.unlink(missing_ok=True)
                acceptable = (not short_take) and (
                    take_cer is None or take_cer <= _TAKE_SELECTION_CER_THRESHOLD
                )
                candidates.append(
                    {
                        "index": take_index,
                        "audio": audio,
                        "sample_rate": sample_rate,
                        "cer": take_cer,
                        "short": short_take,
                        "acceptable": acceptable,
                    }
                )
                if acceptable:
                    break

            acceptable_takes = [item for item in candidates if item["acceptable"]]
            if acceptable_takes:
                chosen = acceptable_takes[0]
            else:
                # Nothing passed: keep the least-bad take — shortest disaster first,
                # then the best readback, then the earliest take.
                chosen = min(
                    candidates,
                    key=lambda item: (
                        item["short"],
                        item["cer"] if item["cer"] is not None else 2.0,
                        item["index"],
                    ),
                )
            audio = chosen["audio"]
            sample_rate = chosen["sample_rate"]
            selected_take = chosen["index"]
            take_cers = [item["cer"] for item in candidates]
            total_acoustic_inference_calls += line_acoustic_inference_calls
            path = hooks.save_audio(audio, sample_rate, destination, base)
            # A take this far short of what the text needs was not really synthesised.
            # The engine still "succeeds": measured on this model, untagged Korean
            # produced 0.08 s of near-silence while the same sentence with an explicit
            # ``<ko>`` tag produced 2.50 s. Without this flag the WebUI reported success
            # and offered a 4 KB file for download.
            produced_seconds = len(np.asarray(audio)) / float(sample_rate or 1)
            expected_seconds = expected_segment_duration(rendered or line, "zh", float(cfg.get("speed", 1)))
            suspiciously_short = bool(
                produced_seconds < _short_output_floor(expected_seconds, rendered or line)
            )
            outputs.append({
                "text": line,
                "rendered_text": rendered,
                "accent_level": configured_accent_level(cfg),
                "pronunciation": line_plan.to_dict(),
                "file": str(path),
                "download_id": hooks.register_download(path),
                "error": "",
                "output_index": index - 1,
                "semantic_token_count": semantic_token_count,
                "semantic_frame_rate": semantic_frame_rate,
                "synthesis_mode": line_synthesis_mode,
                "segment_count": line_segment_count,
                "acoustic_inference_calls": line_acoustic_inference_calls,
                "duration": round(produced_seconds, 3),
                "expected_duration": round(expected_seconds, 3),
                "suspiciously_short": suspiciously_short,
                "takes": len(candidates),
                "selected_take": selected_take,
                "take_cers": take_cers,
                "provenance": hooks.write_provenance(path, cfg, rendered or line),
                "warning": (
                    f"生成音频只有 {produced_seconds:.2f}s，远短于这段文本应有的 "
                    f"{expected_seconds:.2f}s，很可能没有真正合成出来。"
                    "常见原因：非中文台词没有加语言标签（未标记文本按页面默认语言合成），"
                    "或该语言的训练数据不足。请在文本里用 <zh>/<yue>/<en>/<ja>/<ko> 明确标注语种。"
                    if suspiciously_short
                    else ""
                ),
            })
            _emit_event(
                event_cb,
                "output_completed",
                output_index=index - 1,
                semantic_token_count=semantic_token_count,
                semantic_frame_rate=semantic_frame_rate,
                synthesis_mode=line_synthesis_mode,
                segment_count=line_segment_count,
                acoustic_inference_calls=line_acoustic_inference_calls,
            )
        except Exception as exc:
            # Add the failed attempt's calls instead of overwriting: with take
            # selection earlier takes may already have spent inference calls.
            line_acoustic_inference_calls += max(
                1 if line_is_explicit_mixed else 0,
                int(getattr(hooks.backend._inference_calls, "count", 0)),
            )
            total_acoustic_inference_calls += line_acoustic_inference_calls
            error = f"{type(exc).__name__}: {exc}"
            outputs.append({
                "text": line,
                "rendered_text": rendered,
                "accent_level": configured_accent_level(cfg),
                "pronunciation": line_plan.to_dict(),
                "file": "",
                "download_id": "",
                "error": error,
                "output_index": index - 1,
                "semantic_token_count": semantic_token_count,
                "semantic_frame_rate": semantic_frame_rate,
                "synthesis_mode": line_synthesis_mode,
                "segment_count": line_segment_count,
                "acoustic_inference_calls": line_acoustic_inference_calls,
            })
            _emit_event(event_cb, "output_failed", output_index=index - 1, error=error)
        _emit_progress(progress_cb, {
            "status": "running",
            "phase": "writing",
            "current": index,
            "total": len(lines),
            "completed": len([item for item in outputs if item["file"]]),
            "current_text": line,
            "rendered_text": rendered,
            "output_dir": str(destination),
            "outputs": list(outputs),
            "error": outputs[-1]["error"] if outputs else "",
            "synthesis_mode": line_synthesis_mode,
            "segment_count": line_segment_count,
            "acoustic_inference_calls": line_acoustic_inference_calls,
        })

    completed = [item for item in outputs if item["file"]]
    pronunciation_manifest = ""
    pronunciation_manifest_error = ""
    try:
        manifest = build_pronunciation_manifest(cfg, line_plans, outputs)
        pronunciation_manifest = str(hooks.write_manifest(destination, manifest))
    except Exception as exc:  # a manifest failure must not discard finished audio
        pronunciation_manifest_error = f"{type(exc).__name__}: {exc}"
    hooks.append_history([
        {"time": datetime.now().strftime("%m-%d %H:%M"), "text": item["text"], "params": f"t{cfg['temperature']}/s{cfg['sample_steps']}/text={cfg.get('text_lang', 'zh')}/prompt={cfg.get('prompt_lang', 'zh')}", "name": Path(item["file"]).name, "file": item["file"], "download_id": item["download_id"]}
        for item in completed
    ])
    result = {
        "output_dir": str(destination),
        "total": len(lines),
        "completed": len(completed),
        "outputs": outputs,
        "synthesis_mode": "single_pass_explicit" if used_explicit_mixed else "legacy_single",
        "segment_count": total_segment_count,
        "acoustic_inference_calls": total_acoustic_inference_calls,
        "pronunciation_manifest": pronunciation_manifest,
        "pronunciation_manifest_error": pronunciation_manifest_error,
    }
    _emit_progress(progress_cb, {
        "status": "done",
        "phase": "done",
        "progress": 1.0,
        "current": len(lines),
        "total": len(lines),
        "completed": len(completed),
        "current_text": "",
        "rendered_text": "",
        "output_dir": str(destination),
        "outputs": list(outputs),
        "error": "",
        "synthesis_mode": result["synthesis_mode"],
        "segment_count": result["segment_count"],
        "acoustic_inference_calls": result["acoustic_inference_calls"],
        "pronunciation_manifest": pronunciation_manifest,
        "pronunciation_manifest_error": pronunciation_manifest_error,
    })
    return result
