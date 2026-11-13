"""Persistence, diagnostics, and synthesis primitives for the application.

This is the compatibility facade. The big entity blocks moved out (2026-11,
third wave):

* config read/write          -> :mod:`backend.services.config_store`
* history/downloads storage  -> :mod:`backend.services.history_store`
* environment_check/stats    -> :mod:`backend.services.doctor`
* pure text layer            -> :mod:`backend.domain.text`
* engine + pipeline          -> :mod:`backend.engine.tts` / :mod:`backend.pipeline.synthesis`

Everything is re-exported under the same names, so ``core.load_config()``,
``core.load_history()`` and every ``patch.object(core, ...)`` seam keeps
behaving exactly as before. What remains here is synthesis orchestration glue:
the locks, the backend/judge singletons, the upload/patch/audio writers and the
``SynthesisHooks`` adapters that hand the pipeline the facade's *current*
collaborators.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import soundfile as sf

from backend.domain.pronunciation import AccentLevel  # noqa: F401 - facade re-export
from backend.domain.text import (  # noqa: F401 - facade re-exports
    _ACCEPTABLE_SEGMENT_DURATION_RATIO,
    _CAPITAL_INITIALISM_PATTERN,
    _CJK_PUNCTUATION_MAP,
    _DOTTED_INITIALISM_PATTERN,
    _HYPHENATED_INITIALISM_PATTERN,
    _MIXED_TERMINAL_PUNCTUATION,
    _SPACED_INITIALISM_PATTERN,
    _WORDLIKE_ENGLISH_ABBREVIATIONS,
    LANGUAGE_TAG_PATTERN,
    MIXED_LANGUAGE_BOUNDARY_PAUSE_MS,
    SUPPORTED_SYNTHESIS_LANGUAGES,
    UNKNOWN_LANGUAGE_TAG_PATTERN,
    Segment,
    SynthesisSegment,
    _merge_fragment_segments,
    _merge_pronunciation_plans,
    _normalize_fragment_punctuation,
    _spell_english_initialism,
    compose_mixed_language_text,
    configured_accent_level,
    deterministic_language_seed,
    english_pronunciation_hint,
    expected_segment_duration,
    has_pronounceable_text,
    normalize_english_pronunciations,
    normalize_mixed_pronunciation_text,
    parse_language_segments,
    parse_lines,
    render_language_segments,
    render_line_with_plan,
    render_synthesis_text,
    render_with_plan,
    to_engine_segments,
    trim_segment_edges,
    validate_synthesis_language_text,
)
from backend.engine.tts import SynthesisBackend, backend_cwd  # noqa: F401 - facade
from backend.infra.logging import get_logger
from backend.infra.paths import (
    CONFIG_DIR,  # noqa: F401 - facade re-export (app.py, retention.py, tests)
    CONFIG_FILE,  # noqa: F401 - facade re-export (tests repoint it)
    CONFIG_SCHEMA_VERSION,  # noqa: F401 - facade re-export
    DOWNLOADABLE_SUFFIXES,  # noqa: F401 - facade re-export
    DOWNLOADS_LIMIT,  # noqa: F401 - facade re-export
    FFMPEG_TIMEOUT_SECONDS,
    HISTORY_FILE,  # noqa: F401 - facade re-export
    HISTORY_LIMIT,  # noqa: F401 - facade re-export
    MAX_PATCH_BYTES,
    MAX_REFERENCE_AUDIO_BYTES,
    ORB_COLOR_DEFAULTS,  # noqa: F401 - facade re-export
    OUTPUT_DIR,
    REFERENCE_AUDIO_DIR,
    REFERENCE_AUDIO_SUFFIXES,
    REPO_ROOT,  # noqa: F401 - facade re-export (app.py, doctor.py, system.py)
    TRAINING_AUDIO_SUFFIXES,  # noqa: F401 - facade re-export
)
from backend.pipeline import synthesis as _pipeline
from backend.pipeline.synthesis import (  # noqa: F401 - facade re-exports
    _READBACK_RETRY_SECONDS,
    _TAKE_SEED_STRIDE,
    _TAKE_SELECTION_CER_THRESHOLD,
    _TAKE_SELECTION_MAX_TAKES,
    _TAKE_SELECTION_MIN_JUDGE_CHARS,
    PRONUNCIATION_MANIFEST_FILENAME,
    PRONUNCIATION_MANIFEST_SCHEMA_VERSION,
    SHORT_OUTPUT_RATIO,
    SynthesisCancelled,
    _emit_event,
    _emit_progress,
    _judgeable_char_count,
    _ReadbackJudge,
    _short_output_floor,
    build_pronunciation_manifest,
)
from backend.services.config_store import (  # noqa: F401 - facade re-exports
    _config_store,
    default_config,
    load_config,
    save_config,
)
from backend.services.doctor import (  # noqa: F401 - facade re-exports
    environment_check,
    training_stats,
)
from backend.services.history_store import (  # noqa: F401 - facade re-exports
    _downloadable_path,
    _downloads_file,
    _downloads_store,
    _history_store,
    _load_downloads,
    _prune_downloads,
    _save_downloads,
    _write_history,
    append_history,
    clear_history,
    load_history,
    load_history_with_downloads,
    register_download,
    resolve_download,
)

logger = get_logger("xianghua.core")

# 路径与上限常量已下沉到 backend/infra/paths.py，顶部 from-import 保持
# ``core.REPO_ROOT``/``core.CONFIG_DIR`` 等旧引用与 ``patch.object(core, ...)`` 不变。
# 配置/历史/下载的实体读写已迁 config_store/history_store；这两把锁是兼容面
# （test_synthesis_orb_events 断言 ``hasattr(core, "_CONFIG_LOCK")``），
# 实际的并发保护由 jsonstore 的按路径锁承担。
_DOWNLOADS_LOCK = threading.RLock()
_CONFIG_LOCK = threading.RLock()

# Below this share of the duration the text needs, the take was not really synthesised
# and the user has to be told. Deliberately far below _ACCEPTABLE_SEGMENT_DURATION_RATIO:
# that one drives the retry loop (a merely hasty take should be retried), while this one
# flags a delivery that is broken rather than quick. Measured failure case: 0.08 s
# against an expected 2.6 s, i.e. 3%.


# --- 读回自选 take（零样本路线的可靠性杠杆）--------------------------------
# 时长检查救不了"时长正常但吞字/读错"的坏 take（实测 clear_02：2.30s 对预期
# 2.16s，结尾两个字没读出来）。这里用本地 SenseVoice 读回做灾难检测，超过阈值
# 就换种子再合成一次、在真实存在的 take 里选可懂的那个。这是选 take 不是刷分：
# 阈值(15%)故意高于回归验收线(5%)，只救灾难，不追求完美；非中文与显式混语行
# 不参与，读回模型不可用时静默保留首 take。
# 读回 CER 对单字/拟声词没有区分力：实测「噗！」三次 take 都是 200%，只是白白
# 多合成两次。字数不足的行直接交首 take，读回不参与判定。

#: How long to wait before trying to load the readback model again after a
#: failure. Loading it is expensive, but "one transient failure disables take
#: selection for the whole process lifetime" silently degraded output quality.

READBACK_JUDGE = _ReadbackJudge()


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


def _patch_targets(patch_text: str) -> list[str]:
    """Collect every filesystem path a unified diff intends to touch."""
    targets: list[str] = []
    for line in patch_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("diff --git "):
            parts = stripped.split(" b/")
            if len(parts) == 2:
                targets.append(parts[1].strip().strip('"'))
        elif stripped.startswith("+++ b/"):
            targets.append(stripped[len("+++ b/"):].strip().strip('"'))
        elif stripped.startswith("--- a/"):
            targets.append(stripped[len("--- a/"):].strip().strip('"'))
        elif stripped.startswith(("rename to ", "copy to ")):
            targets.append(stripped.split(" to ", 1)[1].strip().strip('"'))
    return targets


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

    # 补丁是 `git apply` 的输入，能改写引擎仓库里的任何文件。发音补丁的
    # 合法作用域只有发音层源码，越界目标一律拒绝，防止借补丁入口改写
    # 会被 import 执行的代码。
    for target in _patch_targets(patch_text):
        normalized = target.replace("\\", "/").lstrip("/")
        if not normalized.startswith("GPT_SoVITS/text/") or ".." in normalized:
            raise ValueError(
                f"发音补丁只能修改 GPT_SoVITS/text/ 下的文件，检测到越界目标：{target}"
            )

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


SYNTHESIS_BACKEND = SynthesisBackend()


def save_audio(audio: Any, sample_rate: int, output_dir: Path, base: str) -> Path:
    """Write one take, preferring MP3 over WAV when ffmpeg can do it.

    The conversion runs while the caller holds the exclusive GPU lease, so it
    must be bounded: a wedged ffmpeg (locked file, antivirus scan, network
    share) used to block the worker thread forever and leak the lease, after
    which every later job failed with "GPU 正被占用" until a restart.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    wav = output_dir / f"{base}.wav"
    sf.write(str(wav), audio, sample_rate)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return wav
    mp3 = output_dir / f"{base}.mp3"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(wav), "-b:a", "192k", str(mp3)],
            capture_output=True,
            check=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        wav.unlink(missing_ok=True)
        return mp3
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg 转码超时（%ss），保留 WAV：%s", FFMPEG_TIMEOUT_SECONDS, wav.name)
        mp3.unlink(missing_ok=True)
        return wav
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("ffmpeg 转码失败，保留 WAV：%s（%s）", wav.name, exc)
        mp3.unlink(missing_ok=True)
        return wav


def new_output_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"{timestamp}_{uuid.uuid4().hex[:8]}"


def _write_output_provenance_quietly(path: Path, cfg: dict[str, Any], text: str) -> str:
    """Best-effort signed provenance sidecar; empty string means "not written".

    Provenance is an audit asset, never a synthesis dependency: any failure
    (missing key dir, locked file, broken card lookup) is swallowed here.
    ``XIANGHUA_PROVENANCE=0`` disables the sidecar entirely (tests do this to
    keep the real config dir untouched).
    """
    if os.environ.get("XIANGHUA_PROVENANCE", "1") == "0":
        return ""
    try:
        from backend.services import provenance

        sidecar = provenance.write_provenance(path, text, cfg)
        return str(sidecar)
    except Exception:
        return ""


def write_pronunciation_manifest(destination: Path, manifest: dict[str, Any]) -> Path:
    """Write ``pronunciation.json`` atomically into one synthesis output directory."""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / PRONUNCIATION_MANIFEST_FILENAME
    temporary = destination / f"{PRONUNCIATION_MANIFEST_FILENAME}.tmp"
    try:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


# --------------------------------------------------------------------------- #
# Synthesis facade
# --------------------------------------------------------------------------- #
# ``synthesize_lines`` and its locked core live in :mod:`backend.pipeline.synthesis`.
# These two wrappers are the only adapters: they hand the pipeline the facade's
# *current* collaborators, so ``patch.object(core, "SYNTHESIS_BACKEND")``,
# ``patch.object(core, "READBACK_JUDGE")``, ``patch.object(core, "save_audio")``
# and friends all keep taking effect.


def _synthesis_hooks() -> _pipeline.SynthesisHooks:
    return _pipeline.SynthesisHooks(
        backend=SYNTHESIS_BACKEND,
        judge=READBACK_JUDGE,
        save_audio=save_audio,
        new_output_dir=new_output_dir,
        append_history=append_history,
        register_download=register_download,
        write_manifest=write_pronunciation_manifest,
        write_provenance=_write_output_provenance_quietly,
    )


def synthesize_lines(
    cfg: dict[str, Any],
    text: str,
    output_dir: Path | None = None,
    progress_cb: Any | None = None,
    event_cb: Any | None = None,
    should_stop: Any | None = None,
) -> dict[str, Any]:
    """Synthesise every line in ``text`` (see :mod:`backend.pipeline.synthesis`)."""
    return _pipeline.synthesize_lines(
        cfg,
        text,
        output_dir,
        progress_cb,
        event_cb,
        should_stop,
        hooks=_synthesis_hooks(),
    )


def _synthesize_lines_locked(
    cfg: dict[str, Any],
    lines: list[str],
    output_dir: Path | None = None,
    progress_cb: Any | None = None,
    event_cb: Any | None = None,
    should_stop: Any | None = None,
) -> dict[str, Any]:
    """Synthesise already-validated lines; the caller must hold the GPU lease."""
    return _pipeline.run_locked(
        cfg,
        lines,
        output_dir,
        progress_cb,
        event_cb,
        should_stop,
        hooks=_synthesis_hooks(),
    )
