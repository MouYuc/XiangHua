"""Pure text layer: line parsing, language segmentation and pronunciation text.

Nothing here touches the filesystem, the GPU or any mutable module state, which
is what makes it directly unit-testable and safe to reuse from the batch/dialogue
pipelines without dragging in the synthesis engine.

This module used to be the first 600 lines of ``backend.services.core``; the
facade re-exports every name for compatibility.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from backend.services.pronunciation import (
    AccentLevel,
    PronunciationPlan,
    build_pronunciation_plan,
)

SUPPORTED_SYNTHESIS_LANGUAGES = {"zh", "yue", "en", "ja", "ko"}


MIXED_LANGUAGE_BOUNDARY_PAUSE_MS = 120


LANGUAGE_TAG_PATTERN = re.compile(r"<(/?)(zh|yue|en|ja|ko)>", re.IGNORECASE)


UNKNOWN_LANGUAGE_TAG_PATTERN = re.compile(r"</?([A-Za-z]{2,8})>")


SynthesisSegment = tuple[str, str]


_CJK_PUNCTUATION_MAP = str.maketrans({
    "。": ".", "，": ",", "！": "!", "？": "?", "：": ":", "；": ";", "、": ",",
})


_MIXED_TERMINAL_PUNCTUATION = "。！？!?；;：:…."


_ACCEPTABLE_SEGMENT_DURATION_RATIO = 0.80


@dataclass(frozen=True)
class Segment:
    text: str
    lang: str
    pause_after_ms: int = 0
    source_span: tuple[int, int] | None = None


def parse_lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip() and not line.strip().startswith("#")]


def configured_accent_level(cfg: dict[str, Any]) -> str:
    """Resolve the staged accent level, with the legacy toggle as a fallback.

    ``auto_lightmid`` used to mean a global 的/是 rewrite. New callers should set
    ``accent_level`` explicitly. When an old request supplies only the toggle, map it
    to ``medium``: the unambiguous phrase rules still apply, but the global rewrite
    that damaged intelligibility does not.
    """
    raw = cfg.get("accent_level")
    if raw in {level.value for level in AccentLevel}:
        return str(raw)
    return "medium" if cfg.get("auto_lightmid") else "off"


def render_with_plan(text: str, cfg: dict[str, Any]) -> PronunciationPlan:
    """Build the staged pronunciation plan used for one line or segment.

    This is the single construction point shared by text rendering and the output
    manifest. ``configured_accent_level`` keeps the legacy ``auto_lightmid`` toggle
    mapped to ``medium``, so old requests render exactly as they did before the
    plan itself was persisted. The user lexicon (``user_lexicon.active_overrides``)
    is consulted here so every Chinese line picks up dictionary corrections; a
    lookup failure must never break synthesis, so it degrades to no overrides.
    """
    try:
        from backend.services import user_lexicon

        overrides = user_lexicon.active_overrides()
    except Exception:
        overrides = {}
    return build_pronunciation_plan(
        text,
        language=str(cfg.get("text_lang", "zh")),
        accent_level=configured_accent_level(cfg),
        user_overrides=overrides,
    )


def render_synthesis_text(text: str, cfg: dict[str, Any]) -> str:
    """Apply the staged pronunciation plan only to Chinese synthesis text."""
    return render_with_plan(text, cfg).rendered_text


def parse_language_segments(text: str, default_language: str = "zh") -> list[SynthesisSegment]:
    """Parse explicit ``<en>...</en>`` markers while treating unmarked text as the default language."""
    for match in UNKNOWN_LANGUAGE_TAG_PATTERN.finditer(text):
        if match.group(1).lower() not in SUPPORTED_SYNTHESIS_LANGUAGES:
            raise ValueError(f"不支持的语言标记：{match.group(0)}。支持：zh、yue、en、ja、ko。")

    segments: list[SynthesisSegment] = []
    cursor = 0
    open_language: str | None = None

    for match in LANGUAGE_TAG_PATTERN.finditer(text):
        closing = bool(match.group(1))
        language = match.group(2).lower() #把语言标记转换为小写，确保一致性
        if open_language is None:
            if closing:
                raise ValueError(f"显式语言标记不匹配：{match.group(0)}。")
            before = text[cursor : match.start()].strip()
            if before:
                segments.append((default_language, before))
            open_language = language
            cursor = match.end()
            continue

        if not closing:
            raise ValueError("显式语言标记不能嵌套或交叉。")
        if language != open_language:
            raise ValueError(f"显式语言标记不匹配：期望 </{open_language}>，得到 {match.group(0)}。")
        content = text[cursor : match.start()].strip()
        if content:
            segments.append((open_language, content))
        cursor = match.end()
        open_language = None

    if open_language is not None:
        raise ValueError(f"显式语言标记未闭合：<{open_language}> 需要有对应的 </{open_language}>。")

    tail = text[cursor:].strip()
    if tail:
        segments.append((default_language, tail))
    if not segments:
        if LANGUAGE_TAG_PATTERN.search(text):
            raise ValueError("显式语言标记内容不能为空。")
        segments.append((default_language, text.strip()))

    return _merge_fragment_segments(segments)


def render_language_segments(segments: list[SynthesisSegment], cfg: dict[str, Any]) -> list[SynthesisSegment]:
    """Apply staged pronunciation rules to each explicit language segment.

    The pronunciation layer is language-aware and leaves non-Chinese text untouched,
    so a mixed line no longer has to disable accent handling globally. That old
    behaviour made one English fragment silently switch off pronunciation for the
    whole line.
    """
    return [
        (
            language,
            render_synthesis_text(segment, {**cfg, "text_lang": language}),
        )
        for language, segment in segments
    ]


def _merge_pronunciation_plans(
    line: str, rendered: str, plans: list[PronunciationPlan]
) -> PronunciationPlan:
    """Fold per-segment plans into the single plan persisted for a source line.

    Replacement offsets are rebased from the segment text into the full source
    line. Segments appear in source order, so the first occurrence at or after the
    previous segment is the correct anchor even when the line repeats itself. If a
    punctuation merge made a segment no longer contiguous, that segment's offsets
    stay relative to the segment text instead of being guessed.
    """
    languages = [plan.language for plan in plans]
    chinese_plans = [plan for plan in plans if plan.lexicon_id != "none"]
    replacements: list[dict[str, Any]] = []
    cursor = 0
    for plan in plans:
        base = line.find(plan.source_text, cursor)
        for item in plan.replacements:
            merged = dict(item)
            if base >= 0 and isinstance(merged.get("start"), int) and isinstance(merged.get("end"), int):
                merged["start"] += base
                merged["end"] += base
            replacements.append(merged)
        if base >= 0:
            cursor = base + len(plan.source_text)
    return PronunciationPlan(
        source_text=line,
        rendered_text=rendered,
        language=languages[0] if len(set(languages)) == 1 else "mixed",
        lexicon_id=chinese_plans[0].lexicon_id if chinese_plans else "none",
        accent_pack_id=chinese_plans[0].accent_pack_id if chinese_plans else "none",
        accent_intensity=max((plan.accent_intensity for plan in plans), default=0.0),
        replacements=replacements,
        protected_terms=list(
            dict.fromkeys(term for plan in chinese_plans for term in plan.protected_terms)
        ),
    )


def render_line_with_plan(
    line: str, cfg: dict[str, Any]
) -> tuple[list[SynthesisSegment], PronunciationPlan, str]:
    """Render one source line, building each segment's plan exactly once.

    The aggregate plan covers the whole line, so downstream manifest writers get a
    ``rendered_text`` that is byte-for-byte the text handed to the engine.
    """
    segments = parse_language_segments(line, cfg.get("text_lang", "zh"))
    plans = [
        render_with_plan(segment, {**cfg, "text_lang": language})
        for language, segment in segments
    ]
    rendered_segments = [
        (language, plan.rendered_text)
        for (language, _), plan in zip(segments, plans)
    ]
    rendered = " ".join(segment for _, segment in rendered_segments)
    return rendered_segments, _merge_pronunciation_plans(line, rendered, plans), rendered


_WORDLIKE_ENGLISH_ABBREVIATIONS = frozenset({
    "LASER",
    "NASA",
    "NATO",
    "RADAR",
    "SCUBA",
    "SONAR",
    "UNESCO",
})


_DOTTED_INITIALISM_PATTERN = re.compile(r"(?<![A-Za-z])(?:[A-Z]\.){2,}(?![A-Za-z])")


_SPACED_INITIALISM_PATTERN = re.compile(r"(?<![A-Za-z])(?:[A-Z]\s+){1,}[A-Z](?![A-Za-z])")


_HYPHENATED_INITIALISM_PATTERN = re.compile(r"(?<![A-Za-z])([A-Z]{2,})-([0-9]+)(?![A-Za-z])")


_CAPITAL_INITIALISM_PATTERN = re.compile(r"(?<![A-Za-z])([A-Z]{2,})(?![A-Za-z])")


def _spell_english_initialism(value: str) -> str:
    letters = re.findall(r"[A-Z]", value)
    return " ".join(letters)


def english_pronunciation_hint(text: str) -> str:
    """Build an internal English pronunciation hint without changing surface text."""
    hinted = re.sub(r"\bwi[\s-]*fi\b", "wifi", text, flags=re.IGNORECASE)

    def replace_initialism(match: re.Match[str]) -> str:
        value = match.group(0)
        letters = "".join(re.findall(r"[A-Z]", value))
        if letters in _WORDLIKE_ENGLISH_ABBREVIATIONS:
            # GPT-SoVITS splits pure uppercase words before G2P; lowercase
            # keeps a word-like exception intact in the internal hint.
            result = value.lower()
            tail = hinted[match.end() :].lstrip()
            return result + "," if tail and re.match(r"[A-Za-z0-9]", tail) else result
        spelled = _spell_english_initialism(value)
        tail = hinted[match.end() :].lstrip()
        return spelled + "," if tail and re.match(r"[A-Za-z0-9]", tail) else spelled

    def replace_hyphenated(match: re.Match[str]) -> str:
        spelled = _spell_english_initialism(match.group(1)) + " " + match.group(2)
        tail = hinted[match.end() :].lstrip()
        return spelled + "," if tail and re.match(r"[A-Za-z]", tail) else spelled

    hinted = _DOTTED_INITIALISM_PATTERN.sub(replace_initialism, hinted)
    hinted = _SPACED_INITIALISM_PATTERN.sub(replace_initialism, hinted)
    hinted = _HYPHENATED_INITIALISM_PATTERN.sub(replace_hyphenated, hinted)
    return _CAPITAL_INITIALISM_PATTERN.sub(replace_initialism, hinted)


def normalize_english_pronunciations(text: str) -> str:
    """Backward-compatible name for the internal English pronunciation hint."""
    return english_pronunciation_hint(text)


def normalize_mixed_pronunciation_text(text: str, *, is_final: bool) -> str:
    """Remove only non-final terminal punctuation from the engine pronunciation text."""
    if is_final:
        return text
    normalized = text.rstrip(_MIXED_TERMINAL_PUNCTUATION).rstrip()
    return normalized or text


def to_engine_segments(
    segments: list[SynthesisSegment | Segment], *, boundary_pause_ms: int = 0
) -> list[dict[str, Any]]:
    """Convert validated application segments into the engine's internal IR."""
    engine_segments: list[dict[str, Any]] = []
    non_empty_count = sum(1 for segment in segments if (segment.text if isinstance(segment, Segment) else segment[1]).strip())
    emitted_count = 0
    for segment in segments:
        if isinstance(segment, Segment):
            language, text = segment.lang, segment.text
            pause_after_ms = segment.pause_after_ms
        else:
            language, text = segment
            pause_after_ms = 0
        language = str(language).lower()
        if language not in SUPPORTED_SYNTHESIS_LANGUAGES:
            raise ValueError(f"不支持的语言片段：{language}。支持：zh、yue、en、ja、ko。")
        text = str(text).strip()
        if text:
            base_pronunciation_text = (
                english_pronunciation_hint(text) if language == "en" else text
            )
            is_final = emitted_count == non_empty_count - 1
            pronunciation_text = normalize_mixed_pronunciation_text(
                base_pronunciation_text,
                is_final=is_final,
            )
            engine_segments.append({
                "lang": language,
                "text": text,
                "pronunciation_text": pronunciation_text,
                "pause_after_ms": int(pause_after_ms)
                    if pause_after_ms
                    else int(boundary_pause_ms) if not is_final else 0,
            })
            emitted_count += 1
    if not engine_segments:
        raise ValueError("显式语言片段内容不能为空。")
    return engine_segments


def has_pronounceable_text(language: str, text: str) -> bool:
    """Check for script content that can survive that language's G2P pipeline."""
    if language == "en":
        return bool(re.search(r"[A-Za-z0-9]", text))
    if language == "ja":
        return bool(re.search(r"[A-Za-z0-9\u3040-\u30ff\u3400-\u9fff\uff66-\uff9d]", text))
    if language == "ko":
        return bool(re.search(r"[A-Za-z0-9\uac00-\ud7af]", text))
    return bool(re.search(r"[A-Za-z0-9\u3400-\u9fff]", text))


def _normalize_fragment_punctuation(text: str, language: str) -> str:
    if language in {"zh", "yue", "ja"}:
        return text
    return text.translate(_CJK_PUNCTUATION_MAP)


def _merge_fragment_segments(segments: list[SynthesisSegment]) -> list[SynthesisSegment]:
    """Attach punctuation-only fragments to a neighboring spoken segment.

    GPT-SoVITS returns an empty 16 kHz placeholder for some punctuation-only
    requests, so those fragments must never be inferred independently.
    """
    spoken: list[SynthesisSegment] = []
    pending_prefix = ""

    for language, segment in segments:
        if not has_pronounceable_text(language, segment):
            punctuation = _normalize_fragment_punctuation(segment, language)
            if spoken:
                previous_language, previous_text = spoken[-1]
                normalized = _normalize_fragment_punctuation(punctuation, previous_language)
                spoken[-1] = (previous_language, previous_text + normalized)
            else:
                pending_prefix += punctuation
            continue

        if pending_prefix:
            normalized = _normalize_fragment_punctuation(pending_prefix, language)
            segment = normalized + segment
            pending_prefix = ""
        spoken.append((language, segment))

    if pending_prefix and spoken:
        language, text = spoken[-1]
        spoken[-1] = (language, text + _normalize_fragment_punctuation(pending_prefix, language))
    if not spoken:
        return segments
    return spoken


def deterministic_language_seed(language: str, text: str, cfg: dict[str, Any]) -> int:
    """Keep repeated short cross-language fragments away from one-off bad samples."""
    payload = "\0".join((
        language,
        text,
        str(cfg.get("ref_audio", "")),
        str(cfg.get("ref_text", "")),
        str(cfg.get("gpt_model", "")),
        str(cfg.get("sovits_model", "")),
    ))
    return zlib.crc32(payload.encode("utf-8")) & 0xFFFFFFFF


def expected_segment_duration(text: str, language: str, speed: float) -> float:
    """Return a conservative floor used only to reject obviously truncated samples."""
    cjk_units = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uff66-\uff9d\uac00-\ud7af]", text))
    if language == "en":
        syllables = sum(
            max(1, len(re.findall(r"[aeiouy]+", word)))
            for word in re.findall(r"[A-Za-z]+", text)
        )
        return max(.55, .40 * syllables) / max(.1, speed)
    english_syllables = sum(
        max(1, len(re.findall(r"[aeiouy]+", word)))
        for word in re.findall(r"[A-Za-z]+", text)
    )
    if language in {"zh", "yue", "ja", "ko", "auto", "auto_yue"}:
        return max(.55, .18 * cjk_units + .40 * english_syllables) / max(.1, speed)
    return max(.35, .18 * cjk_units) / max(.1, speed)


def trim_segment_edges(audio: Any, sample_rate: int, keep_ms: int = 45, threshold_db: float = -45) -> Any:
    """Remove accumulated lead-in/tail silence before joining independent clips."""
    audio = np.asarray(audio)
    if audio.size == 0:
        return audio

    reference = float(np.iinfo(audio.dtype).max) if np.issubdtype(audio.dtype, np.integer) else 1.0
    threshold = reference * 10 ** (threshold_db / 20)
    frame_size = max(1, int(sample_rate * .01))
    keep_frame_count = max(1, int(sample_rate * keep_ms / 1000 / frame_size))
    squares = np.asarray(audio, dtype=np.float64) ** 2
    frame_count = max(1, (len(audio) + frame_size - 1) // frame_size)
    padded = np.pad(squares, (0, frame_count * frame_size - len(audio)))
    frame_energy = padded.reshape(frame_count, frame_size).mean(axis=1)
    active_frames = np.flatnonzero(frame_energy > threshold * threshold)
    if not len(active_frames):
        return audio[: min(len(audio), keep_frame_count * frame_size)]

    start = max(0, (active_frames[0] - keep_frame_count) * frame_size)
    end = min(len(audio), (active_frames[-1] + keep_frame_count + 1) * frame_size)
    trimmed = audio[start:end].copy()
    fade_size = min(len(trimmed) // 2, max(1, int(sample_rate * .008)))
    if audio.dtype.kind == "f":
        trimmed[:fade_size] *= np.linspace(0, 1, fade_size, dtype=audio.dtype)
        trimmed[-fade_size:] *= np.linspace(1, 0, fade_size, dtype=audio.dtype)
    return trimmed


def compose_mixed_language_text(segments: list[SynthesisSegment]) -> str:
    """Join marked fragments for one upstream mixed-language inference pass."""
    cleaned = [(language, segment.strip()) for language, segment in segments if segment.strip()]
    if not cleaned:
        return ""

    pause_punctuation = set("，。！？；：,.!?;:")
    parts: list[str] = [cleaned[0][1]]
    previous_language = cleaned[0][0]
    for language, segment in cleaned[1:]:
        if language != previous_language:
            if previous_language == "en" and segment[0] in {"，", ","}:
                if parts[-1][-1] not in pause_punctuation:
                    parts[-1] += ","
                segment = segment[1:].lstrip()
            elif segment[0] not in pause_punctuation and parts[-1][-1] not in pause_punctuation:
                parts[-1] += ","
        elif language == "en" and not parts[-1].endswith((" ", "\t")):
            parts[-1] += " "
        parts.append(segment)
        previous_language = language
    return "".join(parts)


def validate_synthesis_language_text(text: str, cfg: dict[str, Any]) -> None:
    """Reject text that a target-language G2P pipeline cannot read instead of emitting silence."""
    language = cfg.get("text_lang", "zh")
    lines = parse_lines(text)
    if not lines:
        return

    for line in lines:
        for segment_language, segment in parse_language_segments(line, language):
            if segment_language == "en" and not re.search(r"[A-Za-z0-9]", segment):
                raise ValueError("标记为英语的台词没有英文字母。模型不会自动翻译。")
            if segment_language == "ja" and not re.search(r"[\u3040-\u30ff\uff66-\uff9d]", segment):
                raise ValueError("标记为日语的台词没有假名。模型不会自动翻译。")
            if segment_language == "ko" and not re.search(r"[\uac00-\ud7af]", segment):
                raise ValueError("标记为韩语的台词没有韩文字母。模型不会自动翻译。")



__all__ = [
    "LANGUAGE_TAG_PATTERN",
    "MIXED_LANGUAGE_BOUNDARY_PAUSE_MS",
    "SUPPORTED_SYNTHESIS_LANGUAGES",
    "UNKNOWN_LANGUAGE_TAG_PATTERN",
    "_ACCEPTABLE_SEGMENT_DURATION_RATIO",
    "_CAPITAL_INITIALISM_PATTERN",
    "_CJK_PUNCTUATION_MAP",
    "_DOTTED_INITIALISM_PATTERN",
    "_HYPHENATED_INITIALISM_PATTERN",
    "_MIXED_TERMINAL_PUNCTUATION",
    "_SPACED_INITIALISM_PATTERN",
    "_WORDLIKE_ENGLISH_ABBREVIATIONS",
    "Segment",
    "SynthesisSegment",
    "_merge_fragment_segments",
    "_merge_pronunciation_plans",
    "_normalize_fragment_punctuation",
    "_spell_english_initialism",
    "compose_mixed_language_text",
    "configured_accent_level",
    "deterministic_language_seed",
    "english_pronunciation_hint",
    "expected_segment_duration",
    "has_pronounceable_text",
    "normalize_english_pronunciations",
    "normalize_mixed_pronunciation_text",
    "parse_language_segments",
    "parse_lines",
    "render_language_segments",
    "render_line_with_plan",
    "render_synthesis_text",
    "render_with_plan",
    "to_engine_segments",
    "trim_segment_edges",
    "validate_synthesis_language_text",
]
