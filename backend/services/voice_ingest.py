"""One-upload voice ingestion: natural speech -> reference clip + transcript.

Product rule: a user should only have to provide one normal speaking recording.
They must not record a scripted corpus or label pronunciation/accent data. This
service therefore performs the mechanical work before any user correction:

    decode -> speech detection -> choose a 2-10 s prompt -> quality gate -> ASR

Only the selected prompt is handed to the current GPT-SoVITS baseline. The source
recording remains an asset for later timbre-adapter training. The user may correct
an ASR word, but is never required to transcribe the recording by hand.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import librosa
import numpy as np
import soundfile as sf

from backend.services import media_diarization, reference_audio, whisper_cache
from backend.services.gpu_arbiter import GPU_ARBITER

MIN_SELECTED_SECONDS = 1.8
MAX_SELECTED_SECONDS = 10.0
TARGET_SELECTED_SECONDS = 6.0
PREPARED_SAMPLE_RATE = 32000
MAX_EVALUATED_CANDIDATES = 10
MERGE_GAP_SECONDS = 0.8


class VoiceIngestError(RuntimeError):
    """A user-actionable failure while preparing a voice asset."""


def _load_whisper(model_name: str):
    """Patchable seam; the model itself is cached process-wide in ``whisper_cache``.

    Keeping a second private cache here is what put two large-v3 copies on the card
    (see ``whisper_cache`` module docstring), so this delegates instead.
    """
    return whisper_cache.load_whisper(model_name)


def normalize_transcript(text: str, language: str = "zh") -> str:
    """Normalise ASR text for Chinese synthesis prompts.

    faster-whisper large-v3 sometimes emits Traditional Chinese even when the
    prompt is Simplified. GPT-SoVITS can read both, but keeping the WebUI text and
    manifest comparisons in one script is less surprising; convert with OpenCC when
    it is available and leave the original text otherwise.

    Cantonese (``yue``) deliberately keeps Traditional characters: OpenCC ``t2s``
    rewrites 係 as 系, which is a different word, so the conversion is limited to
    ``zh``. The media ingest path makes the same call for its transcripts.
    """
    value = str(text or "").strip()
    if value and language == "zh":
        try:
            from opencc import OpenCC

            value = OpenCC("t2s").convert(value)
        except Exception:
            pass
    return value


def transcribe_reference(path: str | Path, language: str = "zh", model: str = "large-v3") -> dict[str, Any]:
    """Transcribe one selected prompt with the local/faster-whisper model.

    Decoding goes through ``whisper_cache`` so this path and media ingest share one
    resident model instead of loading a second copy onto the card.
    """
    whisper_language = "zh" if language == "yue" else language
    raw_text, info = whisper_cache.transcribe(
        path,
        model=model,
        language=whisper_language,
        beam_size=5,
        loader=_load_whisper,
    )
    text = normalize_transcript(raw_text, language=language)
    return {
        "text": text,
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "model": model,
    }


def _read_mono(path: str | Path) -> tuple[np.ndarray, int]:
    try:
        audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as exc:
        raise VoiceIngestError(f"无法读取音频：{exc}") from exc
    mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
    return np.ascontiguousarray(mono, dtype=np.float32), int(rate)


def _resample(audio: np.ndarray, rate: int) -> np.ndarray:
    if rate == PREPARED_SAMPLE_RATE:
        return np.asarray(audio, dtype=np.float32)
    return np.asarray(
        librosa.resample(np.asarray(audio, dtype=np.float32), orig_sr=rate, target_sr=PREPARED_SAMPLE_RATE),
        dtype=np.float32,
    )


def candidate_windows(audio: np.ndarray, rate: int) -> list[tuple[float, float]]:
    """Return deduplicated speech windows suitable for a synthesis prompt."""
    duration = audio.size / rate if rate else 0.0
    if duration <= 0:
        return []
    regions = media_diarization.detect_speech_regions(audio, rate)
    if not regions:
        return []
    windows: list[tuple[float, float]] = []
    if MIN_SELECTED_SECONDS <= duration <= MAX_SELECTED_SECONDS:
        windows.append((0.0, duration))
    ordered = sorted(regions, key=lambda item: item.start)
    for index, region in enumerate(ordered):
        turn = media_diarization.Turn(region.start, region.end, speaker_index=0)
        for start, end in media_diarization.split_turn(
            audio,
            rate,
            turn,
            min_seconds=MIN_SELECTED_SECONDS,
            max_seconds=MAX_SELECTED_SECONDS,
        ):
            if MIN_SELECTED_SECONDS <= end - start <= MAX_SELECTED_SECONDS:
                windows.append((float(start), float(end)))
        # Join adjacent speech regions when the pause between them is short. A
        # natural recording often has 0.3-0.7 s sentence pauses; selecting each
        # region in isolation produces fragments like "我的人生目标就会". The
        # joined window keeps a complete thought for the prompt and its ASR text.
        merged_end = region.end
        for following in ordered[index + 1:]:
            if following.start - merged_end > MERGE_GAP_SECONDS:
                break
            if following.end - region.start > MAX_SELECTED_SECONDS:
                break
            merged_end = following.end
            if merged_end - region.start >= MIN_SELECTED_SECONDS:
                windows.append((float(region.start), float(merged_end)))
    if not windows and regions:
        region = max(regions, key=lambda item: item.duration)
        windows.append((float(region.start), float(min(region.end, region.start + MAX_SELECTED_SECONDS))))
    # A whole-file short prompt and a region candidate can describe the same span.
    unique: list[tuple[float, float]] = []
    for start, end in sorted(windows):
        if not unique or abs(unique[-1][0] - start) > 0.02 or abs(unique[-1][1] - end) > 0.02:
            unique.append((start, end))
    return unique


def rank_candidates(
    audio: np.ndarray, rate: int, windows: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Rank by energy and duration closeness before running the expensive gate."""
    ranked: list[tuple[float, float, float]] = []
    for start, end in windows:
        chunk = audio[int(start * rate): int(end * rate)]
        if chunk.size == 0:
            continue
        rms = float(np.sqrt(np.mean(np.asarray(chunk, dtype=np.float64) ** 2)))
        duration = end - start
        duration_score = max(0.0, 1.0 - abs(duration - TARGET_SELECTED_SECONDS) / TARGET_SELECTED_SECONDS)
        ranked.append((start, end, rms * (0.25 + 2.0 * duration_score)))
    ranked.sort(key=lambda item: item[2], reverse=True)
    return [(start, end) for start, end, _score in ranked[:MAX_EVALUATED_CANDIDATES]]


def _candidate_score(report: dict[str, Any], duration: float) -> float:
    status_rank = {"ok": 2, "warn": 1, "fail": 0}.get(str(report.get("status")), 0)
    metrics = report.get("metrics") or {}
    try:
        snr = float(metrics.get("estimated_snr_db") or 0.0)
    except (TypeError, ValueError):
        snr = 0.0
    try:
        silence = float(metrics.get("silence_ratio") or 0.0)
    except (TypeError, ValueError):
        silence = 0.0
    duration_score = max(0.0, 1.0 - abs(duration - TARGET_SELECTED_SECONDS) / TARGET_SELECTED_SECONDS)
    # Duration is weighted deliberately: a 2 s fragment can pass the DSP gate but
    # gives the ASR and the prompt an incomplete phrase. Prefer a complete 4-8 s
    # window unless its quality is materially worse.
    return status_rank * 100.0 + duration_score * 30.0 + min(max(snr, 0.0), 40.0) / 10.0 - silence * 10.0


def _report_progress(
    progress: Callable[[float, str], None] | None,
    value: float,
    stage: str,
) -> None:
    """Report a Chinese stage label without letting a bad callback break ingestion."""
    if progress is None:
        return
    try:
        progress(max(0.0, min(1.0, float(value))), stage)
    except Exception:
        # Progress is cosmetic; a broken reporter must not fail the preparation.
        pass


def prepare_reference_from_audio(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    language: str = "zh",
    model: str = "large-v3",
    transcriber: Callable[[str | Path], dict[str, Any]] | None = None,
    judge: Callable[[str | Path], dict[str, Any]] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Prepare one natural recording for synthesis without asking for a script.

    The function selects a short prompt, runs the existing deterministic quality
    gate, and auto-transcribes the selected prompt. ``transcriber`` and ``judge``
    are injectable so candidate selection and error handling can be tested without
    loading the ASR or speaker models.

    ``progress`` is an optional ``(fraction, stage)`` callback for background jobs.
    Passing ``None`` (the default) keeps the previous behaviour exactly.
    """
    source = Path(source_path)
    if not source.is_file():
        raise VoiceIngestError("找不到上传的音频文件。")
    # Whisper and the candidate quality gate both touch the GPU; hold the lease
    # for the whole preparation so it cannot race a synthesis or training job.
    # The missing-file check above stays outside: an invalid request must fail
    # immediately instead of waiting for the card.
    with GPU_ARBITER.lease("voice_prepare", job_id="", label="音色样本准备"):
        _report_progress(progress, 0.05, "读取音频")
        audio, rate = _read_mono(source)
        if rate <= 0 or audio.size == 0:
            raise VoiceIngestError("音频为空或采样率无效。")

        _report_progress(progress, 0.15, "检测语音")
        windows = rank_candidates(audio, rate, candidate_windows(audio, rate))
        if not windows:
            raise VoiceIngestError("没有检测到可用语音；请确认录音里有正常说话声。")

        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        judge_fn = judge or (lambda path: reference_audio.judge(path, "synthesis"))
        selected: tuple[float, float] | None = None
        _selected_report: dict[str, Any] | None = None
        selected_score = float("-inf")
        total_candidates = max(len(windows), 1)
        with tempfile.TemporaryDirectory(prefix=".voice_ingest_", dir=output) as temporary:
            temporary_dir = Path(temporary)
            for index, (start, end) in enumerate(windows):
                _report_progress(progress, 0.2 + 0.4 * (index / total_candidates), "逐个候选质检")
                candidate_path = temporary_dir / f"candidate_{index:02d}.wav"
                clip = _resample(audio[int(start * rate): int(end * rate)], rate)
                sf.write(str(candidate_path), clip, PREPARED_SAMPLE_RATE, subtype="PCM_16")
                report = judge_fn(candidate_path)
                score = _candidate_score(report, end - start)
                if score > selected_score:
                    selected = (start, end)
                    _selected_report = report  # kept for debugging the selection
                    selected_score = score

        if selected is None:
            raise VoiceIngestError("没有选出可用的参考片段。")

        start, end = selected
        _report_progress(progress, 0.7, "写入参考片段")
        digest = hashlib.sha256(f"{source.resolve()}|{start:.3f}|{end:.3f}".encode()).hexdigest()[:12]
        selected_path = output / f"voice_ref_{digest}.wav"
        sf.write(
            str(selected_path),
            _resample(audio[int(start * rate): int(end * rate)], rate),
            PREPARED_SAMPLE_RATE,
            subtype="PCM_16",
        )
        final_report = judge_fn(selected_path)
        _report_progress(progress, 0.85, "语音转写")
        if transcriber is None:
            transcription = transcribe_reference(selected_path, language=language, model=model)
        else:
            transcription = dict(transcriber(selected_path))
        text = normalize_transcript(str(transcription.get("text") or ""), language=language)
        if not text:
            raise VoiceIngestError("自动转写没有得到文本；请检查录音里是否有清晰语音。")

        warnings: list[str] = []
        if final_report.get("status") == "fail":
            warnings.append("选出的参考片段未通过质量门禁，建议换一段更干净的录音。")
        elif final_report.get("status") == "warn":
            warnings.append("选出的参考片段可用，但质量门禁有警告。")
        warnings.extend(final_report.get("warnings") or [])

        _report_progress(progress, 1.0, "完成")
        return {
            "source_path": str(source),
            "ref_audio": str(selected_path),
            "ref_text": text,
            "duration_seconds": round(end - start, 3),
            "source_duration_seconds": round(audio.size / rate, 3),
            "selected": {
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "candidate_count": len(windows),
                "score": round(selected_score, 4),
            },
            "quality": final_report,
            "transcription": transcription,
            "warnings": warnings,
        }
