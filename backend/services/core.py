"""Persistence, diagnostics, and synthesis primitives for the application."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import soundfile as sf

from tools.env import resolve_default_environment_root
from tools.rules import safe_name, to_lightmid


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / ".guangpu_webui"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
OUTPUT_DIR = REPO_ROOT / "output"
REFERENCE_AUDIO_DIR = CONFIG_DIR / "reference_audio"
REFERENCE_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
MAX_REFERENCE_AUDIO_BYTES = 100 * 1024 * 1024
MAX_PATCH_BYTES = 1024 * 1024
SUPPORTED_SYNTHESIS_LANGUAGES = {"zh", "yue", "en", "ja", "ko"}
MIXED_LANGUAGE_BOUNDARY_PAUSE_MS = 120
ORB_COLOR_DEFAULTS = {
    "orb_synthesis_token_color": "#75BBFF",
    "orb_settled_color": "#69DDA4",
    "orb_playback_token_color": "#F8FBFF",
}
TRAINING_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
DOWNLOADABLE_SUFFIXES = TRAINING_AUDIO_SUFFIXES | {".zip"}
_DOWNLOADS_LOCK = threading.RLock()
_CONFIG_LOCK = threading.RLock()
LANGUAGE_TAG_PATTERN = re.compile(r"<(/?)(zh|yue|en|ja|ko)>", re.IGNORECASE)
UNKNOWN_LANGUAGE_TAG_PATTERN = re.compile(r"</?([A-Za-z]{2,8})>")
SynthesisSegment = tuple[str, str]
_CJK_PUNCTUATION_MAP = str.maketrans({
    "。": ".", "，": ",", "！": "!", "？": "?", "：": ":", "；": ";", "、": ",",
})
_MIXED_TERMINAL_PUNCTUATION = "。！？!?；;：:…."
_ACCEPTABLE_SEGMENT_DURATION_RATIO = 0.80
# Below this share of the duration the text needs, the take was not really synthesised
# and the user has to be told. Deliberately far below _ACCEPTABLE_SEGMENT_DURATION_RATIO:
# that one drives the retry loop (a merely hasty take should be retried), while this one
# flags a delivery that is broken rather than quick. Measured failure case: 0.08 s
# against an expected 2.6 s, i.e. 3%.
SHORT_OUTPUT_RATIO = 0.25


@dataclass(frozen=True)
class Segment:
    text: str
    lang: str
    pause_after_ms: int = 0
    source_span: tuple[int, int] | None = None


def default_config() -> dict[str, Any]:
    root = resolve_default_environment_root(REPO_ROOT)

    def path(*parts: str) -> str:
        return str(Path(root, *parts))

    return {
        "gpt_sovits_root": root,
        "gpt_model": os.environ.get("GPT_MODEL", path("GPT_weights_v2Pro", "guangpu07-e14.ckpt")),
        "sovits_model": os.environ.get("SOVITS_MODEL", path("SoVITS_weights_v2Pro", "guangpu07_e12_s396.pth")),
        "ref_audio": os.environ.get("REF_AUDIO", path("custom_data", "sliced", "训练.wav_0000191040_0000343680.wav")),
        "ref_text": os.environ.get("REF_TEXT", "这条路走到头，左转就到了。"),
        "text_lang": "zh",
        "prompt_lang": "zh",
        "top_k": int(os.environ.get("TOP_K", "30")),
        "top_p": float(os.environ.get("TOP_P", "0.88")),
        "temperature": float(os.environ.get("TEMPERATURE", "0.95")),
        "sample_steps": int(os.environ.get("SAMPLE_STEPS", "64")),
        "speed": float(os.environ.get("SPEED", "1.0")),
        "pause_second": float(os.environ.get("PAUSE_SECOND", "0.15")),
        "voice_profile_id": os.environ.get("VOICE_PROFILE_ID", "current"),
        "accent_id": os.environ.get("ACCENT_ID", "standard"),
        "mixed_eos_floor": None,
        "mixed_token_cap": None,
        "auto_lightmid": os.environ.get("AUTO_LIGHTMID", "1") == "1",
        "theme": os.environ.get("GP_THEME", "auto"),
        **ORB_COLOR_DEFAULTS,
    }


def load_config() -> dict[str, Any]:
    with _CONFIG_LOCK:
        cfg = default_config()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, ValueError):
            pass
    for field in ("text_lang", "prompt_lang"):
        if cfg.get(field) not in SUPPORTED_SYNTHESIS_LANGUAGES:
            cfg[field] = "zh"
    for field, default in ORB_COLOR_DEFAULTS.items():
        value = str(cfg.get(field) or "").upper()
        cfg[field] = value if re.fullmatch(r"#[0-9A-F]{6}", value) else default
    if cfg.get("theme") not in {"auto", "light-green", "light-blue", "dark", "nymbo"}:
        cfg["theme"] = "auto"
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    with _CONFIG_LOCK:
        merged = default_config()
        merged.update(cfg)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CONFIG_FILE)
        return merged


def load_history() -> list[dict[str, Any]]:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_history(history: list[dict[str, Any]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history[:200], ensure_ascii=False, indent=2), encoding="utf-8")


def append_history(entries: list[dict[str, Any]]) -> None:
    _write_history(entries + load_history())


def clear_history() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text("[]", encoding="utf-8")


def _downloads_file() -> Path:
    return CONFIG_DIR / "downloads.json"


def _load_downloads() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_downloads_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_downloads(downloads: dict[str, dict[str, str]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _downloads_file().write_text(json.dumps(downloads, ensure_ascii=False, indent=2), encoding="utf-8")


def _downloadable_path(path: str | Path) -> Path | None:
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
    except OSError:
        return None
    if not candidate.is_file() or candidate.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
        return None
    return candidate


def register_download(path: str | Path) -> str:
    """Persist a random credential for one already-validated downloadable file."""
    candidate = _downloadable_path(path)
    if candidate is None:
        raise ValueError("只能登记存在的音频或 ZIP 文件。")
    with _DOWNLOADS_LOCK:
        downloads = _load_downloads()
        for download_id, entry in downloads.items():
            if entry.get("path") == str(candidate):
                return download_id
        download_id = uuid.uuid4().hex
        downloads[download_id] = {"path": str(candidate), "name": candidate.name}
        _save_downloads(downloads)
    return download_id


def resolve_download(download_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{32}", download_id or ""):
        return None
    with _DOWNLOADS_LOCK:
        entry = _load_downloads().get(download_id)
    return _downloadable_path(entry.get("path", "")) if entry else None


def load_history_with_downloads() -> list[dict[str, Any]]:
    """Attach exact-file credentials to legacy history entries when possible."""
    history = load_history()
    changed = False
    for entry in history:
        if entry.get("download_id"):
            continue
        try:
            entry["download_id"] = register_download(str(entry.get("file") or ""))
        except ValueError:
            entry["download_id"] = ""
        changed = True
    if changed:
        _write_history(history)
    return history


def parse_lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip() and not line.strip().startswith("#")]


def environment_check(cfg: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        rows.append({"name": name, "status": status, "detail": detail, "fix": fix})

    version = sys.version_info
    add("Python 版本", "ok" if (version.major, version.minor) >= (3, 10) else "bad", f"{version.major}.{version.minor}.{version.micro}", "请安装 Python 3.10+")
    try:
        import torch

        parts = torch.__version__.split("+")[0].split(".")
        cuda = torch.cuda.is_available()
        add(
            "PyTorch + CUDA",
            "ok" if tuple(map(int, parts[:2])) >= (2, 6) and cuda else "bad",
            torch.__version__ + (" · CUDA 可用" if cuda else " · CUDA 不可用"),
            "请安装 GPT-SoVITS 要求的 CUDA 版 torch" if not cuda else "",
        )
    except Exception as exc:
        add("PyTorch + CUDA", "bad", str(exc), "请安装 GPT-SoVITS 要求的 CUDA 版 torch")

    required = ("fastapi", "uvicorn", "soundfile", "numpy")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    add("Web 服务依赖", "ok" if not missing else "bad", "已安装" if not missing else "缺少：" + ", ".join(missing), "pip install fastapi uvicorn soundfile numpy" if missing else "")
    ffmpeg = shutil.which("ffmpeg")
    add("ffmpeg", "ok" if ffmpeg else "bad", Path(ffmpeg).name if ffmpeg else "未找到（可先用 wav 输出）", "安装 ffmpeg" if not ffmpeg else "")

    root = Path(str(cfg.get("gpt_sovits_root") or "")).expanduser()
    root_ok = root.is_dir() and (root / "GPT_SoVITS").is_dir()
    add("GPT-SoVITS 根目录", "ok" if root_ok else "bad", str(root) if str(root) != "." else "未配置", "在设置页配置 GPT_SOVITS_ROOT" if not root_ok else "")
    if root_ok:
        def has_patch(name: str) -> bool:
            try:
                return "GP_PINYIN" in (root / "GPT_SoVITS" / "text" / name).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return False

        patched = all(has_patch(name) for name in ("chinese.py", "chinese2.py"))
        add("广普发音补丁", "ok" if patched else "bad", "已应用" if patched else "未应用", "在训练页应用广普补丁" if not patched else "")
    else:
        add("广普发音补丁", "warn", "跳过（根目录不可用）")

    models_ok = Path(str(cfg.get("gpt_model") or "")).is_file() and Path(str(cfg.get("sovits_model") or "")).is_file()
    add("GPT / SoVITS 模型", "ok" if models_ok else "bad", "已找到" if models_ok else "缺少模型文件", "训练完成后在设置页配置模型路径" if not models_ok else "")
    reference_ok = Path(str(cfg.get("ref_audio") or "")).is_file()
    add("参考音频", "ok" if reference_ok else "warn", "已找到" if reference_ok else "未找到，可在合成页手动指定")
    return rows


def training_stats(cfg: dict[str, Any]) -> dict[str, float | int | None]:
    data = Path(str(cfg.get("gpt_sovits_root") or "")) / "custom_data"
    stats: dict[str, float | int | None] = {"sliced": None, "lines": None, "duration": None}
    try:
        stats["sliced"] = len(list((data / "sliced").glob("*.wav")))
    except OSError:
        pass
    try:
        stats["lines"] = len([line for line in (data / "sliced_corrected.list").read_text(encoding="utf-8").splitlines() if line.strip()])
    except OSError:
        pass
    raw_dir = data / "raw"
    audio_files = []
    try:
        audio_files = [
            path for path in raw_dir.iterdir()
            if path.is_file() and path.suffix.lower() in TRAINING_AUDIO_SUFFIXES
        ]
    except OSError:
        pass
    if not audio_files:
        audio_files = [data / "训练.wav"]
    durations = []
    for audio_file in audio_files:
        try:
            durations.append(float(sf.info(str(audio_file)).duration))
        except (OSError, RuntimeError):
            continue
    if durations:
        stats["duration"] = sum(durations)
    return stats


@contextmanager
def backend_cwd(root: str) -> Iterator[None]:
    previous = os.getcwd()
    try:
        os.chdir(root)
        yield
    finally:
        os.chdir(previous)


def render_synthesis_text(text: str, cfg: dict[str, Any]) -> str:
    """Apply the Guangpu conversion only to Chinese synthesis text."""
    return to_lightmid(text) if cfg.get("auto_lightmid") and cfg.get("text_lang", "zh") == "zh" else text


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
    """Apply language-specific rendering (currently Guangpu conversion) segment by segment."""
    mixed_language = len({language for language, segment in segments if segment.strip()}) > 1
    return [
        (
            language,
            render_synthesis_text(
                segment,
                {
                    **cfg,
                    "text_lang": language,
                    "auto_lightmid": cfg.get("auto_lightmid") and not mixed_language,
                },
            ),
        )
        for language, segment in segments
    ]


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


def save_reference_audio(filename: str, stream: Any) -> Path:
    """Save an uploaded reference audio stream under a generated managed name."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in REFERENCE_AUDIO_SUFFIXES:
        raise ValueError("参考音频仅支持 wav、mp3、m4a、flac 或 ogg 文件。")
    REFERENCE_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    destination = REFERENCE_AUDIO_DIR / f"{uuid.uuid4().hex}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".upload")
    written = 0
    try:
        with temporary.open("wb") as output:
            while chunk := stream.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_REFERENCE_AUDIO_BYTES:
                    raise ValueError("参考音频不能超过 100 MB。")
                output.write(chunk)
        temporary.replace(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def apply_pronunciation_patch(root: str | Path, filename: str, stream: Any) -> str:
    """Check and apply a user-provided Guangpu pronunciation patch to an engine."""
    if Path(filename or "").suffix.lower() not in {".patch", ".diff"}:
        raise ValueError("发音补丁仅支持 .patch 或 .diff 文件。")

    chunks: list[bytes] = []
    written = 0
    while chunk := stream.read(1024 * 1024):
        written += len(chunk)
        if written > MAX_PATCH_BYTES:
            raise ValueError("发音补丁不能超过 1 MB。")
        chunks.append(chunk)
    try:
        patch_text = b"".join(chunks).decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise ValueError("发音补丁必须是 UTF-8 文本。") from exc

    targets_pronunciation = "GPT_SoVITS/text/chinese.py" in patch_text or "GPT_SoVITS/text/chinese2.py" in patch_text
    if not targets_pronunciation or "GP_PINYIN" not in patch_text:
        raise ValueError("这不是有效的发音补丁：目标文件或 GP_PINYIN 内容不匹配。")

    engine_root = Path(root).expanduser().resolve()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".patch", dir=engine_root, delete=False) as output:
            output.write(patch_text.encode("utf-8"))
            temporary = Path(output.name)

        checked = subprocess.run(
            ["git", "apply", "--ignore-whitespace", "--check", str(temporary)],
            cwd=engine_root,
            capture_output=True,
            text=True,
        )
        if checked.returncode:
            detail = checked.stderr.strip() or checked.stdout.strip()
            if "already exists" in detail.lower():
                raise ValueError("发音补丁已应用，无需重复导入。")
            raise ValueError(f"发音补丁校验失败：{detail}")

        applied = subprocess.run(
            ["git", "apply", "--ignore-whitespace", str(temporary)],
            cwd=engine_root,
            capture_output=True,
            text=True,
        )
        if applied.returncode:
            detail = applied.stderr.strip() or applied.stdout.strip()
            raise ValueError(f"发音补丁应用失败：{detail}")
        return "发音补丁已应用到当前 GPT-SoVITS 环境。"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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

    def start_warmup(self, cfg: dict[str, Any]) -> dict[str, str]:
        with self._lock:
            if self._warmup["status"] == "warming":
                return dict(self._warmup)
            self._warmup = {"status": "warming", "message": "模型预热中。", "error": ""}

        def run() -> None:
            try:
                self.ensure(cfg)
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
                    semantic_token_cb(token_offset + token_count, frame_rate)

            audio, segment_sample_rate, segment_token_count, segment_frame_rate = self._synthesize_seeded(
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


SYNTHESIS_BACKEND = SynthesisBackend()


def save_audio(audio: Any, sample_rate: int, output_dir: Path, base: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    wav = output_dir / f"{base}.wav"
    sf.write(str(wav), audio, sample_rate)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return wav
    mp3 = output_dir / f"{base}.mp3"
    try:
        subprocess.run([ffmpeg, "-y", "-i", str(wav), "-b:a", "192k", str(mp3)], capture_output=True, check=True)
        wav.unlink(missing_ok=True)
        return mp3
    except (OSError, subprocess.CalledProcessError):
        return wav


def new_output_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"{timestamp}_{uuid.uuid4().hex[:8]}"


def _emit_progress(progress_cb: Any | None, payload: dict[str, Any]) -> None:
    if callable(progress_cb):
        progress_cb(dict(payload))


def _emit_event(event_cb: Any | None, event_type: str, **payload: Any) -> None:
    if callable(event_cb):
        event_cb({"type": event_type, **payload})


def synthesize_lines(
    cfg: dict[str, Any],
    text: str,
    output_dir: Path | None = None,
    progress_cb: Any | None = None,
    event_cb: Any | None = None,
) -> dict[str, Any]:
    lines = parse_lines(text)
    if not lines:
        raise ValueError("台词为空，请先输入内容。")
    validate_synthesis_language_text(text, cfg)
    SYNTHESIS_BACKEND.ensure(cfg)
    destination = output_dir or new_output_dir()
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
    for index, line in enumerate(lines, 1):
        rendered_segments = render_language_segments(parse_language_segments(line, cfg.get("text_lang", "zh")), cfg)
        rendered = " ".join(segment for _, segment in rendered_segments)
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
                output_index=index - 1,
                token_index=semantic_token_count,
                semantic_frame_rate=semantic_frame_rate,
            )

        try:
            audio, sample_rate = SYNTHESIS_BACKEND.synth_segments(cfg, rendered_segments, semantic_token_cb=report_semantic_token)
            line_acoustic_inference_calls = max(
                1 if line_is_explicit_mixed else 0,
                int(getattr(SYNTHESIS_BACKEND._inference_calls, "count", 0)),
            )
            total_acoustic_inference_calls += line_acoustic_inference_calls
            path = save_audio(audio, sample_rate, destination, base)
            # A take this far short of what the text needs was not really synthesised.
            # The engine still "succeeds": measured on this model, untagged Korean
            # produced 0.08 s of near-silence while the same sentence with an explicit
            # ``<ko>`` tag produced 2.50 s. Without this flag the WebUI reported success
            # and offered a 4 KB file for download.
            produced_seconds = len(np.asarray(audio)) / float(sample_rate or 1)
            expected_seconds = expected_segment_duration(rendered or line, "zh", float(cfg.get("speed", 1)))
            suspiciously_short = bool(
                produced_seconds < max(0.6, expected_seconds * SHORT_OUTPUT_RATIO)
            )
            outputs.append({
                "text": line,
                "rendered_text": rendered,
                "file": str(path),
                "download_id": register_download(path),
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
            line_acoustic_inference_calls = max(
                1 if line_is_explicit_mixed else 0,
                int(getattr(SYNTHESIS_BACKEND._inference_calls, "count", 0)),
            )
            total_acoustic_inference_calls += line_acoustic_inference_calls
            error = f"{type(exc).__name__}: {exc}"
            outputs.append({
                "text": line,
                "rendered_text": rendered,
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
    append_history([
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
    })
    return result
