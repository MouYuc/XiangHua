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
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import soundfile as sf

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
from backend.infra import jsonstore
from backend.infra.logging import get_logger
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
from backend.services.pronunciation import AccentLevel
from tools.env import resolve_default_environment_root

logger = get_logger("xianghua.core")


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / ".guangpu_webui"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
OUTPUT_DIR = REPO_ROOT / "output"
REFERENCE_AUDIO_DIR = CONFIG_DIR / "reference_audio"
REFERENCE_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
MAX_REFERENCE_AUDIO_BYTES = 100 * 1024 * 1024
MAX_PATCH_BYTES = 1024 * 1024
ORB_COLOR_DEFAULTS = {
    "orb_synthesis_token_color": "#75BBFF",
    "orb_settled_color": "#69DDA4",
    "orb_playback_token_color": "#F8FBFF",
}
TRAINING_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
DOWNLOADABLE_SUFFIXES = TRAINING_AUDIO_SUFFIXES | {".zip", ".srt"}
_DOWNLOADS_LOCK = threading.RLock()
_CONFIG_LOCK = threading.RLock()
#: On-disk format version for ``config.json``. Bump it and add a migration entry
#: in ``_CONFIG_STORE`` whenever a persisted field changes meaning.
CONFIG_SCHEMA_VERSION = 1
#: How many synthesis history rows are kept. Older rows are dropped on write —
#: this is a bounded activity log, not an audit trail (provenance sidecars are
#: the durable record).
HISTORY_LIMIT = 200
#: The download registry is a credential map for ``/api/files/<id>``. Entries
#: whose backing file is gone are pruned on write so the map cannot grow forever.
DOWNLOADS_LIMIT = 5000
#: Hard ceiling for a single ffmpeg remux. Generous for a 10-minute take,
#: short enough that a wedged process cannot hold the GPU lease for the session.
FFMPEG_TIMEOUT_SECONDS = 120
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
        "accent_level": os.environ.get("ACCENT_LEVEL", "off"),
        "take_selection": os.environ.get("TAKE_SELECTION", "1") != "0",
        "mixed_eos_floor": None,
        "mixed_token_cap": None,
        "auto_lightmid": os.environ.get("AUTO_LIGHTMID", "0") == "1",
        "theme": os.environ.get("GP_THEME", "auto"),
        **ORB_COLOR_DEFAULTS,
    }


def _config_store() -> jsonstore.JsonStore:
    """Store bound to the *current* ``CONFIG_FILE``.

    Built per call (and not cached) on purpose: tests and the environment
    picker repoint ``core.CONFIG_FILE``/``CONFIG_DIR`` at runtime, and the
    ``JsonStore`` lock registry is keyed by path, so a fresh instance still
    shares the same lock as every other writer of that file.
    """
    return jsonstore.JsonStore(
        CONFIG_FILE,
        default=default_config,
        schema_version=CONFIG_SCHEMA_VERSION,
        validate=lambda payload: isinstance(payload, dict),
    )


def load_config() -> dict[str, Any]:
    """Effective configuration: defaults overlaid with ``config.json``.

    A corrupt file is *preserved* by the store (``config.json.corrupt-<ts>``)
    and reported on stderr instead of being silently replaced, so a user whose
    settings vanish can still recover them.
    """
    cfg = default_config()
    try:
        stored = _config_store().read()
        if isinstance(stored, dict):
            # ``schema_version`` is bookkeeping, not user configuration.
            cfg.update({key: value for key, value in stored.items() if key != "schema_version"})
    except jsonstore.StoreCorruptError as exc:
        logger.error("%s 读取失败，已按默认配置启动：%s", exc.path.name, exc)
    for field in ("text_lang", "prompt_lang"):
        if cfg.get(field) not in SUPPORTED_SYNTHESIS_LANGUAGES:
            cfg[field] = "zh"
    for field, default in ORB_COLOR_DEFAULTS.items():
        value = str(cfg.get(field) or "").upper()
        cfg[field] = value if re.fullmatch(r"#[0-9A-F]{6}", value) else default
    if cfg.get("theme") not in {"auto", "light-green", "light-blue", "dark", "nymbo"}:
        cfg["theme"] = "auto"
    if cfg.get("accent_level") not in {level.value for level in AccentLevel}:
        cfg["accent_level"] = "off"
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge ``cfg`` over the defaults and persist atomically.

    Update-in-place under the store's lock: a concurrent ``PUT /config`` from a
    second tab can no longer interleave a read and a write and lose one of them.
    """
    merged = default_config()
    merged.update(cfg)
    _config_store().write(merged)
    return merged


def _history_store() -> jsonstore.JsonStore:
    return jsonstore.JsonStore(
        HISTORY_FILE,
        default=list,
        expose_version=False,
        validate=lambda payload: isinstance(payload, list),
    )


def load_history() -> list[dict[str, Any]]:
    """Recent synthesis runs, newest first.

    A corrupt file is moved aside rather than reported as "no history": the old
    behaviour made a torn write look like an empty history, and the next append
    then persisted that emptiness.
    """
    try:
        data = _history_store().read()
    except jsonstore.StoreCorruptError as exc:
        logger.error("%s 读取失败，历史按空处理：%s", exc.path.name, exc)
        return []
    return data if isinstance(data, list) else []


def _write_history(history: list[dict[str, Any]]) -> None:
    _history_store().write(list(history)[:HISTORY_LIMIT])


def append_history(entries: list[dict[str, Any]]) -> None:
    """Prepend ``entries`` and trim, as one atomic read-modify-write.

    The previous implementation was ``_write_history(entries + load_history())``:
    a read and a write with no lock between them, so a concurrent reader landing
    in another writer's truncation window saw ``[]`` and the whole history was
    replaced by the new entries.
    """
    if not entries:
        return
    _history_store().update(lambda current: ([*(entries if isinstance(entries, list) else [entries]), *current][:HISTORY_LIMIT]))


def clear_history() -> None:
    _write_history([])


def _downloads_file() -> Path:
    return CONFIG_DIR / "downloads.json"


def _downloads_store() -> jsonstore.JsonStore:
    return jsonstore.JsonStore(
        _downloads_file(),
        default=dict,
        expose_version=False,
        validate=lambda payload: isinstance(payload, dict),
    )


def _load_downloads() -> dict[str, dict[str, str]]:
    try:
        data = _downloads_store().read()
    except jsonstore.StoreCorruptError as exc:
        logger.error("%s 读取失败，下载凭据按空处理：%s", exc.path.name, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_downloads(downloads: dict[str, dict[str, str]]) -> None:
    _downloads_store().write(downloads)


def _prune_downloads(downloads: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Drop credentials whose file is gone, then cap the map.

    Every synthesis line used to add a permanent entry; the map had no removal
    path at all, so it grew with the total number of files ever produced.
    """
    alive: dict[str, dict[str, str]] = {}
    for download_id, entry in downloads.items():
        target = entry.get("path")
        if target and Path(target).is_file():
            alive[download_id] = entry
    if len(alive) > DOWNLOADS_LIMIT:
        # dicts keep insertion order, so the tail is the newest.
        alive = dict(list(alive.items())[-DOWNLOADS_LIMIT:])
    return alive


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
    resolved = str(candidate)
    issued: dict[str, str] = {}

    def mutate(downloads: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for download_id, entry in downloads.items():
            if entry.get("path") == resolved:
                issued["id"] = download_id
                return downloads
        download_id = uuid.uuid4().hex
        issued["id"] = download_id
        downloads[download_id] = {"path": resolved, "name": candidate.name}
        return _prune_downloads(downloads)

    _downloads_store().update(mutate)
    return issued["id"]


def resolve_download(download_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{32}", download_id or ""):
        return None
    entry = _load_downloads().get(download_id)
    return _downloadable_path(entry.get("path", "")) if entry else None


def load_history_with_downloads() -> list[dict[str, Any]]:
    """Attach exact-file credentials to legacy history entries when possible.

    ``GET /api/history`` used to *write* the history file here, from a read it
    had taken before issuing any credential — so a synthesis finishing at the
    same moment could be erased. The patch now goes through ``update`` and only
    touches entries that are still missing an id.
    """
    history = load_history()
    missing = [str(entry.get("file") or "") for entry in history if not entry.get("download_id")]
    if not missing:
        return history

    issued: dict[str, str] = {}
    for path in missing:
        try:
            issued[path] = register_download(path)
        except ValueError:
            issued[path] = ""

    def mutate(current: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        changed = False
        for entry in current:
            if entry.get("download_id"):
                continue
            key = str(entry.get("file") or "")
            if key in issued:
                entry["download_id"] = issued[key]
                changed = True
        return current if changed else None

    return _history_store().update(mutate)




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
