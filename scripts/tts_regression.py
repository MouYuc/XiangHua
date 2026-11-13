#!/usr/bin/env python3
"""Fixed, repeatable TTS regression bench for the clarity-first baseline.

The bench deliberately runs each fixture sentence once, at the accent level already
written into that sentence. It does not expand every sentence across four accent
levels: that would turn a 20-sentence smoke gate into 80 expensive syntheses and make
the first useful signal arrive much too late.

Measurements are written as machine-readable JSON. CER is an intelligibility guardrail,
not a voice-similarity score; SECS comes from an independent speaker model. A missing
ASR or speaker checkpoint is reported as ``skipped`` and never aborts the run.

Usage:
    .venv\\Scripts\\python.exe scripts\\tts_regression.py --limit 1
    .venv\\Scripts\\python.exe scripts\\tts_regression.py --group clear --limit 2
    .venv\\Scripts\\python.exe scripts\\tts_regression.py --skip-synthesis --no-asr --no-secs

Defaults:
    fixture: backend/tests/fixtures/tts_regression_20.json
    report:  output/tts_regression/report.json
    audio:   output/tts_regression/audio/
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
EXPERIMENTS_DIR = SCRIPTS_DIR / "experiments"
DEFAULT_FIXTURE = REPO_ROOT / "backend" / "tests" / "fixtures" / "tts_regression_20.json"
DEFAULT_OUT_DIR = REPO_ROOT / "output" / "tts_regression"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
REGRESSION_SEED = 20261017
GROUPS = ("clear", "technical", "light", "medium", "heavy")
AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
REQUIRED_GROUPS = frozenset(GROUPS)
ACCENT_LEVEL_BY_GROUP = {
    "clear": "off",
    "technical": "off",
    "light": "light",
    "medium": "medium",
    "heavy": "heavy",
}
# These are lexical markers, not a general language classifier. They are used only
# to keep the fixture's clear group genuinely clear.
YUE_LEXICAL_MARKERS = frozenset("嘅乜嘢冇系唔系咁啲嗰佢哋咗喺")
# Same-text accent-swap suite (architecture §7, exchange test #3): one sentence,
# one timbre, only the runtime accent level changes. Part of the fixed bench so a
# single run answers both "is it intelligible" and "does accent move CER, not SECS".
# The sentence deliberately contains protected compounds (目的/但是) plus words the
# light/medium rules touch (没有/什么), so the swap also exercises the guards.
SWAP_BASE_TEXT = "你说这个方案的目的到底是什么？我们没有时间了，但是我们还是要验证。"
SWAP_LEVELS = ("off", "light", "medium", "heavy")
CLIPPING_THRESHOLD = 0.999
SILENCE_FRAME_SECONDS = 0.02
SILENCE_RMS_DBFS = -45.0
_EPSILON = 1e-12


def load_fixture(path: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fixture 顶层必须是 JSON 对象")
    return data


def validate_fixture(data: dict[str, Any]) -> list[str]:
    """Return fixture contract errors instead of raising on the first one."""
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version 必须为 1")
    if data.get("seed") != REGRESSION_SEED:
        errors.append(f"fixture seed 必须为 {REGRESSION_SEED}")
    declared_groups = data.get("groups")
    if declared_groups != list(GROUPS):
        errors.append(f"groups 必须严格为 {list(GROUPS)}")
    cases = data.get("cases")
    if not isinstance(cases, list):
        return [*errors, "cases 必须是数组"]
    if len(cases) != 20:
        errors.append(f"cases 必须固定为 20 条，当前 {len(cases)} 条")

    seen_ids: set[str] = set()
    seen_groups: set[str] = set()
    combined_text: list[str] = []
    for index, case in enumerate(cases):
        label = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{label} 必须是对象")
            continue
        case_id = case.get("id")
        group = case.get("group")
        text = case.get("text")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"{label}.id 缺失")
        elif case_id in seen_ids:
            errors.append(f"重复 id：{case_id}")
        else:
            seen_ids.add(case_id)
        if group not in REQUIRED_GROUPS:
            errors.append(f"{case_id or label}.group 非法：{group!r}")
        else:
            seen_groups.add(group)
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{case_id or label}.text 缺失")
            continue
        combined_text.append(text)
        expected_level = ACCENT_LEVEL_BY_GROUP.get(str(group))
        if case.get("accent_level") != expected_level:
            errors.append(
                f"{case_id or label}.accent_level 应为 {expected_level!r}，"
                f"当前为 {case.get('accent_level')!r}"
            )
        if not case.get("expected_prosody") and not case.get("notes"):
            errors.append(f"{case_id or label} 至少需要 expected_prosody 或 notes")
        if group == "clear" and any(character in YUE_LEXICAL_MARKERS for character in text):
            errors.append(f"{case_id or label} 属于 clear 组，但包含粤语/广普字词标记")

    missing_groups = REQUIRED_GROUPS - seen_groups
    if missing_groups:
        errors.append(f"缺少分组：{sorted(missing_groups)}")

    coverage = data.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("coverage 必须是对象")
    else:
        protected_terms = coverage.get("protected_terms") or []
        if not isinstance(protected_terms, list) or not protected_terms:
            errors.append("coverage.protected_terms 不能为空")
        else:
            corpus = "\n".join(combined_text)
            for term in protected_terms:
                if not isinstance(term, str) or term not in corpus:
                    errors.append(f"受保护搭配未出现在 fixture 文本中：{term!r}")
        out_of_training = coverage.get("out_of_training_ids") or []
        if not isinstance(out_of_training, list) or not out_of_training:
            errors.append("coverage.out_of_training_ids 不能为空")
        else:
            unknown = [case_id for case_id in out_of_training if case_id not in seen_ids]
            if unknown:
                errors.append(f"out_of_training_ids 引用了不存在的 id：{unknown}")
    return errors


def parse_groups(values: Sequence[str] | None) -> list[str] | None:
    if not values:
        return None
    groups: list[str] = []
    for value in values:
        for part in str(value).split(","):
            group = part.strip()
            if not group:
                continue
            if group not in REQUIRED_GROUPS:
                raise ValueError(f"未知分组 {group!r}；可用：{', '.join(GROUPS)}")
            if group not in groups:
                groups.append(group)
    if not groups:
        raise ValueError("--group 不能为空")
    return groups


def select_cases(
    data: dict[str, Any],
    groups: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if limit is not None and limit < 0:
        raise ValueError("--limit 不能为负数")
    cases = list(data.get("cases") or [])
    if groups:
        requested = set(groups)
        unknown = requested - REQUIRED_GROUPS
        if unknown:
            raise ValueError(f"未知分组：{sorted(unknown)}")
        cases = [case for case in cases if case.get("group") in requested]
    if limit is not None:
        cases = cases[:limit]
    return cases


def longest_true_run(values: Iterable[Any]) -> int:
    """Longest consecutive run of truthy values, in samples."""
    longest = 0
    current = 0
    for value in values:
        if bool(value):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def audio_metrics(
    audio: np.ndarray,
    sample_rate: int,
    expected_duration: float | None = None,
    suspiciously_short: bool | None = None,
) -> dict[str, Any]:
    """Measure clipping and silence without requiring a model or a decoder."""
    samples = np.asarray(audio, dtype=np.float64)
    if samples.ndim == 2:
        samples = samples.mean(axis=1)
    else:
        samples = samples.reshape(-1)
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须为正数")
    if samples.size == 0:
        return {
            "duration": 0.0,
            "expected_duration": expected_duration,
            "suspiciously_short": suspiciously_short,
            "clipping_ratio": None,
            "longest_clipping_run": 0,
            "silence_ratio": None,
            "peak_abs": None,
        }

    duration = float(samples.size) / float(sample_rate)
    clipping = np.abs(samples) >= CLIPPING_THRESHOLD
    clipping_ratio = float(clipping.mean())
    longest_clipping_run = longest_true_run(clipping)

    frame_size = max(1, int(round(sample_rate * SILENCE_FRAME_SECONDS)))
    silent_frames = 0
    frame_count = 0
    for start in range(0, samples.size, frame_size):
        frame = samples[start:start + frame_size]
        if frame.size == 0:
            continue
        rms = math.sqrt(float(np.mean(np.square(frame))))
        dbfs = 20.0 * math.log10(max(rms, _EPSILON))
        frame_count += 1
        if dbfs <= SILENCE_RMS_DBFS:
            silent_frames += 1

    if suspiciously_short is None and expected_duration is not None:
        suspiciously_short = duration < max(0.6, float(expected_duration) * 0.25)

    return {
        "duration": round(duration, 6),
        "expected_duration": round(float(expected_duration), 6) if expected_duration is not None else None,
        "suspiciously_short": suspiciously_short,
        "clipping_ratio": round(clipping_ratio, 8),
        "longest_clipping_run": int(longest_clipping_run),
        "silence_ratio": round(silent_frames / frame_count, 6) if frame_count else None,
        "peak_abs": round(float(np.max(np.abs(samples))), 8),
    }


def read_audio_mono(path: str | Path) -> tuple[np.ndarray, int]:
    """Decode the generated file, falling back to ffmpeg when libsndfile says no."""
    import soundfile as sf

    try:
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as first_error:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(f"无法解码音频且找不到 ffmpeg：{first_error}") from first_error
        with tempfile.TemporaryDirectory(prefix="tts_regression_decode_") as temporary:
            decoded = Path(temporary) / "decoded.wav"
            subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-i", str(path), "-ac", "1", str(decoded)],
                check=True,
                capture_output=True,
            )
            data, rate = sf.read(str(decoded), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    return np.asarray(mono, dtype=np.float32), int(rate)


def find_existing_audio(case_dir: Path) -> Path | None:
    if not case_dir.is_dir():
        return None
    candidates = [
        path
        for path in case_dir.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def load_existing_synthesis(case_dir: Path) -> dict[str, Any]:
    sidecar = case_dir / "synthesis.json"
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def synthesize_case(
    core_module: Any,
    cfg: dict[str, Any],
    case: dict[str, Any],
    case_dir: Path,
) -> dict[str, Any]:
    """Synthesize exactly one fixture item and save a sidecar for reuse."""
    case_dir.mkdir(parents=True, exist_ok=True)
    case_cfg = dict(cfg)
    # The fixture sentence already carries its intended accent level. Keeping this off
    # prevents the legacy global replacement from silently changing the text under test.
    case_cfg["auto_lightmid"] = False
    # The fixture text is the literal text under test. Do not let a user runtime
    # accent setting rewrite it a second time.
    case_cfg["accent_level"] = "off"
    case_cfg["seed"] = REGRESSION_SEED
    started = time.monotonic()
    record: dict[str, Any] = {
        "id": case["id"],
        "group": case["group"],
        "accent_level": case["accent_level"],
        "text": case["text"],
        "file": "",
        "rendered_text": "",
        "duration": None,
        "expected_duration": None,
        "suspiciously_short": None,
        "synthesis_mode": None,
        "segment_count": None,
        "acoustic_inference_calls": None,
        "error": "",
    }
    try:
        result = core_module.synthesize_lines(case_cfg, case["text"], output_dir=case_dir)
        outputs = result.get("outputs") or []
        output = outputs[0] if outputs else {}
        file_path = str(output.get("file") or "")
        record.update({
            "file": file_path,
            "rendered_text": output.get("rendered_text") or "",
            "duration": output.get("duration"),
            "expected_duration": output.get("expected_duration"),
            "suspiciously_short": output.get("suspiciously_short"),
            "synthesis_mode": output.get("synthesis_mode") or result.get("synthesis_mode"),
            "segment_count": output.get("segment_count") or result.get("segment_count"),
            "acoustic_inference_calls": output.get("acoustic_inference_calls")
            if output.get("acoustic_inference_calls") is not None
            else result.get("acoustic_inference_calls"),
            "takes": output.get("takes"),
            "selected_take": output.get("selected_take"),
            "take_cers": output.get("take_cers"),
            "error": output.get("error") or "",
        })
        if not file_path:
            record["error"] = record["error"] or "合成没有返回音频文件"
        elif not Path(file_path).is_file():
            record["error"] = record["error"] or f"合成返回的文件不存在：{file_path}"
    except Exception as exc:  # one bad sentence must not discard the whole bench
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["seconds_taken"] = round(time.monotonic() - started, 3)
    sidecar = case_dir / "synthesis.json"
    sidecar.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def reuse_case(case: dict[str, Any], case_dir: Path) -> dict[str, Any]:
    """Reuse a previous audio file and its sidecar without touching the model."""
    existing = load_existing_synthesis(case_dir)
    audio_path = find_existing_audio(case_dir)
    if audio_path is None:
        return {
            "id": case["id"],
            "group": case["group"],
            "accent_level": case["accent_level"],
            "text": case["text"],
            "file": "",
            "rendered_text": existing.get("rendered_text") or "",
            "duration": existing.get("duration"),
            "expected_duration": existing.get("expected_duration"),
            "suspiciously_short": existing.get("suspiciously_short"),
            "synthesis_mode": existing.get("synthesis_mode"),
            "segment_count": existing.get("segment_count"),
            "acoustic_inference_calls": existing.get("acoustic_inference_calls"),
            "seconds_taken": None,
            "reused": True,
            "error": "",
        }
    record = dict(existing)
    record.update({
        "id": case["id"],
        "group": case["group"],
        "accent_level": case["accent_level"],
        "text": case["text"],
        "file": str(audio_path),
        "seconds_taken": None,
        "reused": True,
        "error": "",
    })
    return record


def summarize_swap(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Descriptive roll-up of the swap suite; deliberately not a pass/fail gate."""
    levels: dict[str, dict[str, Any]] = {}
    for row in rows:
        cer = row.get("cer") or {}
        secs = row.get("secs") or {}
        levels[str(row.get("level"))] = {
            "cer": cer.get("value") if cer.get("status") == "ok" else None,
            "secs": secs.get("value") if secs.get("status") == "ok" else None,
        }
    cer_values = [v["cer"] for v in levels.values() if v["cer"] is not None]
    secs_values = [v["secs"] for v in levels.values() if v["secs"] is not None]
    cer_off = levels.get("off", {}).get("cer")
    cer_heavy = levels.get("heavy", {}).get("cer")
    return {
        "levels": levels,
        "cer_off": cer_off,
        "cer_heavy": cer_heavy,
        "cer_rises_with_accent": bool(
            cer_off is not None and cer_heavy is not None and cer_heavy > cer_off
        ),
        "secs_spread": (
            round(max(secs_values) - min(secs_values), 6) if secs_values else None
        ),
        "cer_available": len(cer_values),
        "secs_available": len(secs_values),
        "failed_levels": [str(row.get("level")) for row in rows if row.get("error")],
        "note": "同文本只改口音档：CER 应随档位上升，SECS 应基本稳定；SECS 单独不能验收。",
    }


def run_swap_suite(
    core_module: Any,
    cfg: dict[str, Any],
    out_dir: Path,
    *,
    skip_synthesis: bool = False,
    cer_evaluator: Any = None,
    secs_evaluator: Any = None,
) -> dict[str, Any]:
    """Synthesize one fixed sentence at every accent level and measure each output.

    The text and the timbre never change; only ``accent_level`` does. CER is scored
    against each level's rendered text, so lexical replacements are not errors.
    """
    from backend.services.pronunciation import build_pronunciation_plan

    rows: list[dict[str, Any]] = []
    for level in SWAP_LEVELS:
        plan = build_pronunciation_plan(SWAP_BASE_TEXT, language="zh", accent_level=level)
        level_dir = out_dir / level
        if skip_synthesis:
            audio_path = find_existing_audio(level_dir)
            synthesis = {
                "file": str(audio_path) if audio_path else "",
                "error": "" if audio_path else "没有可复用音频",
                "duration": None,
                "expected_duration": None,
            }
        else:
            try:
                result = core_module.synthesize_lines(
                    {**cfg, "accent_level": level, "auto_lightmid": False},
                    SWAP_BASE_TEXT,
                    output_dir=level_dir,
                )
                output = (result.get("outputs") or [{}])[0]
                synthesis = {
                    "file": str(output.get("file") or ""),
                    "error": output.get("error") or "",
                    "duration": output.get("duration"),
                    "expected_duration": output.get("expected_duration"),
                }
                if not synthesis["file"]:
                    synthesis["error"] = synthesis["error"] or "合成没有返回音频文件"
            except Exception as exc:  # one level must not discard the suite
                synthesis = {
                    "file": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration": None,
                    "expected_duration": None,
                }

        file_path = synthesis["file"]
        row: dict[str, Any] = {
            "level": level,
            "source_text": SWAP_BASE_TEXT,
            "rendered_text": plan.rendered_text,
            "replacements": plan.replacements,
            "file": file_path,
            "error": synthesis["error"],
            "metrics": {"duration": None, "clipping_ratio": None, "silence_ratio": None},
            "cer": {"status": "skipped", "reason": synthesis["error"] or "没有音频"},
            "secs": {"status": "skipped", "reason": synthesis["error"] or "没有音频"},
        }
        if file_path and Path(file_path).is_file():
            try:
                audio, rate = read_audio_mono(file_path)
                row["metrics"] = audio_metrics(
                    audio, rate, expected_duration=synthesis.get("expected_duration")
                )
            except Exception as exc:
                row["error"] = row["error"] or f"{type(exc).__name__}: {exc}"
            if cer_evaluator is not None:
                row["cer"] = cer_evaluator.evaluate(plan.rendered_text, file_path)
            if secs_evaluator is not None:
                row["secs"] = secs_evaluator.evaluate(file_path)
        rows.append(row)
        cer_text = format_ratio(row["cer"].get("value")) if row["cer"].get("status") == "ok" else "skip"
        secs_text = format_number(row["secs"].get("value")) if row["secs"].get("status") == "ok" else "skip"
        print(f"swap/{level:<7} CER={cer_text:>8} SECS={secs_text:>8} {plan.rendered_text}")
    return {
        "base_text": SWAP_BASE_TEXT,
        "levels": list(SWAP_LEVELS),
        "rows": rows,
        "summary": summarize_swap(rows),
    }


def run_cases(
    core_module: Any,
    cfg: dict[str, Any],
    cases: Sequence[dict[str, Any]],
    audio_root: Path,
    *,
    skip_synthesis: bool,
    asr_evaluator: Any,
    secs_evaluator: Any,
) -> list[dict[str, Any]]:
    """Run every fixture case once and return per-case reports (the bench core)."""
    case_reports: list[dict[str, Any]] = []
    for case in cases:
        case_dir = audio_root / case["id"]
        if skip_synthesis:
            synthesis = reuse_case(case, case_dir)
        else:
            synthesis = synthesize_case(core_module, cfg, case, case_dir)

        if synthesis.get("error") or not synthesis.get("file"):
            metrics = {
                "duration": synthesis.get("duration"),
                "expected_duration": synthesis.get("expected_duration"),
                "suspiciously_short": synthesis.get("suspiciously_short"),
                "clipping_ratio": None,
                "longest_clipping_run": None,
                "silence_ratio": None,
                "peak_abs": None,
            }
            cer = {"status": "skipped", "reason": "没有可评估的音频"}
            secs = {"status": "skipped", "reason": "没有可评估的音频"}
        else:
            try:
                metrics = _measure_file(
                    synthesis["file"],
                    synthesis.get("expected_duration"),
                    synthesis.get("suspiciously_short"),
                )
            except Exception as exc:
                metrics = {
                    "duration": synthesis.get("duration"),
                    "expected_duration": synthesis.get("expected_duration"),
                    "suspiciously_short": synthesis.get("suspiciously_short"),
                    "clipping_ratio": None,
                    "longest_clipping_run": None,
                    "silence_ratio": None,
                    "peak_abs": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            if asr_evaluator is None:
                cer = {"status": "skipped", "reason": "--no-asr"}
            else:
                cer = asr_evaluator.evaluate(case["text"], synthesis["file"])
            if secs_evaluator is None:
                secs = {"status": "skipped", "reason": "--no-secs"}
            else:
                secs = secs_evaluator.evaluate(synthesis["file"])

        case_report = _case_report(case, synthesis, metrics, cer, secs)
        case_reports.append(case_report)
        cer_text = format_ratio(cer.get("value")) if cer.get("status") == "ok" else "skip"
        secs_text = format_number(secs.get("value")) if secs.get("status") == "ok" else "skip"
        duration_text = format_number(metrics.get("duration"))
        print(
            f"{case['id']:<16} {case_report['status']:<7} dur={duration_text:>7}s "
            f"CER={cer_text:>8} SECS={secs_text:>8}"
        )
    return case_reports


def clean_asr_hypothesis(text: str) -> str:
    """Remove SenseVoice control tokens before CER.

    SenseVoice emits tokens such as ``<|NEUTRAL|><|Speech|><|withitn|>`` in the
    transcript. They are metadata, not spoken characters. Leaving them in makes a
    perfect readback look like a huge insertion error; measured on the first smoke
    clip, CER changed from 0.0 to 1.67 solely because of these tokens.
    """
    return re.sub(r"<\|[^|]+\|>", "", str(text or "")).strip()


class CerEvaluator:
    """Lazy SenseVoiceSmall readback; reuse existing project wrappers."""

    backend = "SenseVoiceSmall (five_language_verify.LanguageIdentifier)"

    def __init__(self, device: str | None = None) -> None:
        self.device = device
        self.reason = ""
        self._attempted = False
        self._model: Any = None
        self._cer: Any = None

    def _ensure_loaded(self) -> None:
        if self._attempted:
            return
        self._attempted = True
        try:
            if str(EXPERIMENTS_DIR) not in sys.path:
                sys.path.insert(0, str(EXPERIMENTS_DIR))
            from asr_readback import cer as cer_function
            from five_language_verify import LanguageIdentifier

            self._cer = cer_function
            self._model = LanguageIdentifier(device=self.device)
        except Exception as exc:
            self.reason = f"{type(exc).__name__}: {exc}"

    def evaluate(self, reference: str, audio_path: str | Path) -> dict[str, Any]:
        self._ensure_loaded()
        if self._model is None or self._cer is None:
            return {"status": "skipped", "reason": self.reason or "SenseVoice 不可用"}
        try:
            # Compare characters with the prompt, not ASR-normalised Arabic digits.
            identified = self._model.identify(audio_path, use_itn=False)
            hypothesis = clean_asr_hypothesis(str(identified.get("text") or ""))
            value = float(self._cer(reference, hypothesis))
            return {
                "status": "ok",
                "value": round(value, 6),
                "reference": reference,
                "hypothesis": hypothesis,
                "detected_language": identified.get("language"),
            }
        except Exception as exc:
            self.reason = f"{type(exc).__name__}: {exc}"
            return {"status": "skipped", "reason": self.reason}


class SecsEvaluator:
    """Lazy independent speaker-embedding comparison against the configured reference."""

    backend = "ERes2NetV2 (speaker_embedding.SpeakerEmbedder)"

    def __init__(self, reference_audio: str | Path | None, device: str | None = None) -> None:
        self.reference_audio = Path(reference_audio) if reference_audio else None
        self.device = device
        self.reason = ""
        self._attempted = False
        self._embedder: Any = None
        self._reference_embedding: Any = None
        self._cosine: Any = None

    def _ensure_loaded(self) -> None:
        if self._attempted:
            return
        self._attempted = True
        if self.reference_audio is None or not self.reference_audio.is_file():
            self.reason = f"参考音频不存在：{self.reference_audio}"
            return
        try:
            if str(EXPERIMENTS_DIR) not in sys.path:
                sys.path.insert(0, str(EXPERIMENTS_DIR))
            from speaker_embedding import SpeakerEmbedder, cosine_similarity

            self._embedder = SpeakerEmbedder(device=self.device)
            self._reference_embedding = self._embedder.embed_file(self.reference_audio)
            self._cosine = cosine_similarity
        except Exception as exc:
            self.reason = f"{type(exc).__name__}: {exc}"

    def evaluate(self, audio_path: str | Path) -> dict[str, Any]:
        self._ensure_loaded()
        if self._embedder is None:
            return {"status": "skipped", "reason": self.reason or "说话人模型不可用"}
        try:
            embedding = self._embedder.embed_file(audio_path)
            value = float(self._cosine(self._reference_embedding, embedding))
            return {
                "status": "ok",
                "value": round(value, 6),
                "reference_audio": str(self.reference_audio),
            }
        except Exception as exc:
            self.reason = f"{type(exc).__name__}: {exc}"
            return {"status": "skipped", "reason": self.reason}


def _metric_values(cases: Sequence[dict[str, Any]], path: Sequence[str]) -> list[float]:
    values: list[float] = []
    for case in cases:
        node: Any = case
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            values.append(float(node))
    return values


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def summarize_case_set(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    durations = _metric_values(cases, ("metrics", "duration"))
    expected_durations = _metric_values(cases, ("metrics", "expected_duration"))
    clipping_ratios = _metric_values(cases, ("metrics", "clipping_ratio"))
    silence_ratios = _metric_values(cases, ("metrics", "silence_ratio"))
    cers = _metric_values(cases, ("cer", "value"))
    secs_values = _metric_values(cases, ("secs", "value"))
    suspicious = sum(1 for case in cases if case.get("metrics", {}).get("suspiciously_short"))
    clipping_cases = sum(
        1
        for case in cases
        if int(case.get("metrics", {}).get("longest_clipping_run") or 0) > 1
    )
    failed = sum(1 for case in cases if case.get("status") == "failed")
    skipped = sum(1 for case in cases if case.get("status") == "skipped")
    return {
        "count": len(cases),
        "ok": sum(1 for case in cases if case.get("status") == "ok"),
        "failed": failed,
        "failures": failed,
        "skipped": skipped,
        "suspiciously_short": suspicious,
        "clipping_cases": clipping_cases,
        "cer": {
            "available": len(cers),
            "mean": _mean(cers),
            "min": round(min(cers), 6) if cers else None,
            "max": round(max(cers), 6) if cers else None,
        },
        "secs": {
            "available": len(secs_values),
            "mean": _mean(secs_values),
            "min": round(min(secs_values), 6) if secs_values else None,
            "max": round(max(secs_values), 6) if secs_values else None,
        },
        "duration": {
            "mean": _mean(durations),
            "min": round(min(durations), 6) if durations else None,
            "max": round(max(durations), 6) if durations else None,
            "expected_mean": _mean(expected_durations),
        },
        "clipping_ratio_mean": _mean(clipping_ratios),
        "silence_ratio_mean": _mean(silence_ratios),
    }


def summarize_groups(cases: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for group in GROUPS:
        group_cases = [case for case in cases if case.get("group") == group]
        if group_cases:
            result[group] = summarize_case_set(group_cases)
    return result


def config_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "gpt_model",
        "sovits_model",
        "ref_audio",
        "ref_text",
        "text_lang",
        "prompt_lang",
        "sample_steps",
        "temperature",
        "top_k",
        "top_p",
        "speed",
        "pause_second",
        "voice_profile_id",
        "accent_id",
        "auto_lightmid",
        "seed",
    )
    return {key: cfg.get(key) for key in keys}


def _case_report(
    case: dict[str, Any],
    synthesis: dict[str, Any],
    metrics: dict[str, Any],
    cer: dict[str, Any],
    secs: dict[str, Any],
) -> dict[str, Any]:
    if synthesis.get("error"):
        status = "failed"
    elif not synthesis.get("file"):
        status = "skipped"
    else:
        status = "ok"
    return {
        "id": case["id"],
        "group": case["group"],
        "accent_level": case["accent_level"],
        "text": case["text"],
        "expected_prosody": case.get("expected_prosody", ""),
        "notes": case.get("notes", ""),
        "status": status,
        "synthesis": synthesis,
        "metrics": metrics,
        "cer": cer,
        "secs": secs,
    }


def _measure_file(path: str | Path, expected_duration: float | None, suspiciously_short: bool | None) -> dict[str, Any]:
    audio, rate = read_audio_mono(path)
    return audio_metrics(
        audio,
        rate,
        expected_duration=expected_duration,
        suspiciously_short=suspiciously_short,
    )


def format_ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.2%}"


def format_number(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def print_summary(report: dict[str, Any]) -> None:
    print()
    print(f"{'group':<10} {'n':>3} {'ok':>3} {'fail':>5} {'short':>6} {'CER':>8} {'SECS':>8} {'dur(s)':>8}")
    for group, row in report["groups"].items():
        print(
            f"{group:<10} {row['count']:>3} {row['ok']:>3} {row['failed']:>5} "
            f"{row['suspiciously_short']:>6} {format_ratio(row['cer']['mean']):>8} "
            f"{format_number(row['secs']['mean']):>8} {format_number(row['duration']['mean']):>8}"
        )
    failures = [case for case in report["cases"] if case["status"] == "failed"]
    for case in failures:
        print(f"  FAIL {case['id']}: {case['synthesis'].get('error') or '没有音频文件'}")
    overall = report["summary"]
    print(
        f"total: {overall['count']} cases, {overall['ok']} ok, {overall['failed']} failed, "
        f"{overall['skipped']} skipped, {overall['suspiciously_short']} suspiciously short"
    )
    swap = report.get("swap")
    if swap:
        print("swap (same text, only accent level):")
        for level, row in swap["summary"]["levels"].items():
            print(
                f"  {level:<7} CER={format_ratio(row['cer']) if row['cer'] is not None else 'skip':>8} "
                f"SECS={format_number(row['secs']) if row['secs'] is not None else 'skip':>8}"
            )
        print(f"  CER rises with accent: {swap['summary']['cer_rises_with_accent']}, SECS spread: {swap['summary']['secs_spread']}")
    for run in report.get("voice_runs") or []:
        line = f"voice {run.get('display_name')} ({run.get('voice_card_id')} v{run.get('active_version')}): "
        line += " ".join(
            f"{group}={format_ratio(row['cer']['mean'])}"
            for group, row in run["groups"].items()
        )
        print(line)
        run_swap = run.get("swap")
        if run_swap:
            print(f"  swap SECS spread: {run_swap['summary']['secs_spread']}, CER off→heavy: "
                  f"{run_swap['summary']['cer_off']} → {run_swap['summary']['cer_heavy']}")
    print(f"report: {report['report_path']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the fixed 20-sentence TTS regression bench.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="fixed fixture JSON")
    parser.add_argument("--limit", type=int, default=None, help="only run the first N selected cases")
    parser.add_argument(
        "--group",
        action="append",
        default=None,
        help="only run a group; repeatable or comma-separated (clear/technical/light/medium/heavy)",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="audio and default report directory")
    parser.add_argument(
        "--skip-synthesis",
        action="store_true",
        help="reuse audio/<case-id>/ files and their synthesis.json sidecars",
    )
    parser.add_argument("--json-out", default=None, help="machine-readable report path")
    parser.add_argument("--no-asr", action="store_true", help="skip CER / SenseVoice")
    parser.add_argument("--no-secs", action="store_true", help="skip SECS / ERes2NetV2")
    parser.add_argument(
        "--no-swap",
        action="store_true",
        help="skip the same-text four-level accent swap suite (off/light/medium/heavy)",
    )
    parser.add_argument(
        "--voice-card",
        action="append",
        default=None,
        metavar="ID",
        help="run the whole bench against a VoiceCard (repeatable); running two cards "
        "in one command is the change-voice-without-changing-pronunciation exchange test",
    )
    parser.add_argument("--device", default=None, help="cpu / cuda for ASR and speaker models")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fixture_path = Path(args.fixture)
    try:
        fixture = load_fixture(fixture_path)
    except (OSError, ValueError) as exc:
        print(f"fixture 读取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    errors = validate_fixture(fixture)
    if errors:
        print("fixture 校验失败：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 2
    try:
        groups = parse_groups(args.group)
        cases = select_cases(fixture, groups=groups, limit=args.limit)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    audio_root = out_dir / "audio"
    report_path = Path(args.json_out) if args.json_out else out_dir / "report.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    from backend.services import core

    cfg = dict(core.load_config())
    cfg["seed"] = REGRESSION_SEED

    # synthesize_lines is the application path and normally appends to the WebUI history.
    # A regression run should not pollute user-facing history with bench entries.
    history_write_disabled = False
    if not args.skip_synthesis and callable(getattr(core, "append_history", None)):
        core.append_history = lambda entries: None
        history_write_disabled = True

    asr_evaluator = None if args.no_asr else CerEvaluator(args.device)

    card_ids: list[str] = list(args.voice_card or [])
    runs: list[dict[str, Any]] = []
    if card_ids:
        from backend.services import voice_assets

        store = voice_assets.VoiceCardStore()
        for card_id in card_ids:
            try:
                card = store.get(card_id)
                run_cfg = voice_assets.apply_voice_card(dict(cfg), card)
            except voice_assets.VoiceAssetError as exc:
                print(f"声线卡不可用：{card_id}：{exc}", file=sys.stderr)
                return 2
            runs.append(
                {
                    "label": card_id,
                    "voice_card_id": card_id,
                    "display_name": card.get("display_name"),
                    "active_version": card.get("active_version"),
                    "config": run_cfg,
                }
            )
    else:
        runs.append({"label": "config", "config": dict(cfg)})

    def _run_one(run: dict[str, Any]) -> dict[str, Any]:
        run_cfg = run["config"]
        base_dir = out_dir / "voices" / run["label"] if card_ids else out_dir
        secs_evaluator = (
            None if args.no_secs else SecsEvaluator(run_cfg.get("ref_audio"), args.device)
        )
        case_reports = run_cases(
            core,
            run_cfg,
            cases,
            base_dir / "audio",
            skip_synthesis=args.skip_synthesis,
            asr_evaluator=asr_evaluator,
            secs_evaluator=secs_evaluator,
        )
        swap = None
        if not args.no_swap:
            swap = run_swap_suite(
                core,
                run_cfg,
                base_dir / "swap",
                skip_synthesis=args.skip_synthesis,
                cer_evaluator=asr_evaluator,
                secs_evaluator=secs_evaluator,
            )
        result: dict[str, Any] = {
            "groups": summarize_groups(case_reports),
            "summary": summarize_case_set(case_reports),
            "cases": case_reports,
            "swap": swap,
            "_secs_reason": secs_evaluator.reason if secs_evaluator is not None else "--no-secs",
        }
        if card_ids:
            result["voice_card_id"] = run["voice_card_id"]
            result["display_name"] = run["display_name"]
            result["active_version"] = run["active_version"]
            result["out_dir"] = str(base_dir.resolve())
            result["cases"] = [
                {"voice_card_id": run["voice_card_id"], **case} for case in case_reports
            ]
        return result

    run_results = [_run_one(run) for run in runs]
    if card_ids:
        all_case_reports = [case for result in run_results for case in result["cases"]]
        single_swap = None
    else:
        all_case_reports = run_results[0]["cases"]
        single_swap = run_results[0]["swap"]

    report = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fixture": {
            "path": str(fixture_path.resolve()),
            "name": fixture.get("name"),
            "schema_version": fixture.get("schema_version"),
            "case_count": len(fixture["cases"]),
        },
        "settings": {
            "seed": {
                "value": REGRESSION_SEED,
                "configured": True,
                "effective": True,
                "note": (
                    "cfg['seed'] 进入每行 line_seed，每次重试按 base_seed+attempt*104729 派生，"
                    "引擎请求携带 seed；2026-10-25 两次全量运行输出字节级一致（sha256 抽样 11/11）。"
                ),
            },
            "skip_synthesis": bool(args.skip_synthesis),
            "groups": groups,
            "limit": args.limit,
            "device": args.device,
            "out_dir": str(out_dir.resolve()),
            "audio_dir": str(audio_root.resolve()),
            "asr_enabled": not args.no_asr,
            "secs_enabled": not args.no_secs,
            "swap_enabled": not args.no_swap,
            "voice_cards": card_ids,
            "fixture_text_policy": "literal; auto_lightmid and accent_level forced off per case",
            "history_write_disabled": history_write_disabled,
        },
        "config": config_snapshot(cfg),
        "evaluators": {
            "cer": {
                "backend": CerEvaluator.backend,
                "requested": not args.no_asr,
                "last_reason": asr_evaluator.reason if asr_evaluator is not None else "--no-asr",
            },
            "secs": {
                "backend": SecsEvaluator.backend,
                "requested": not args.no_secs,
                "last_reason": (
                    run_results[0].pop("_secs_reason", "") if run_results else "--no-secs"
                ),
            },
        },
        "groups": summarize_groups(all_case_reports),
        "summary": summarize_case_set(all_case_reports),
        "cases": all_case_reports,
        "swap": single_swap,
        "voice_runs": list(run_results) if card_ids else None,
        "report_path": str(report_path.resolve()),
    }
    for _result in run_results:
        _result.pop("_secs_reason", None)
    if not card_ids:
        report.pop("voice_runs", None)
        if single_swap is None:
            report.pop("swap", None)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(report)
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
