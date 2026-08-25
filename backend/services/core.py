"""Gradio-independent persistence, diagnostics, and synthesis primitives."""

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
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import soundfile as sf

from tools.env import resolve_default_environment_root
from tools.rules import safe_name, to_lightmid


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / ".guangpu_webui"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
OUTPUT_DIR = REPO_ROOT / "output"
TRAINING_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}
DOWNLOADABLE_SUFFIXES = TRAINING_AUDIO_SUFFIXES | {".zip"}
_DOWNLOADS_LOCK = threading.RLock()


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
        "top_k": int(os.environ.get("TOP_K", "30")),
        "top_p": float(os.environ.get("TOP_P", "0.88")),
        "temperature": float(os.environ.get("TEMPERATURE", "0.95")),
        "sample_steps": int(os.environ.get("SAMPLE_STEPS", "64")),
        "speed": float(os.environ.get("SPEED", "1.0")),
        "pause_second": float(os.environ.get("PAUSE_SECOND", "0.15")),
        "auto_lightmid": os.environ.get("AUTO_LIGHTMID", "1") == "1",
        "theme": os.environ.get("GP_THEME", "auto"),
    }


def load_config() -> dict[str, Any]:
    cfg = default_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    if cfg.get("theme") not in {"auto", "light-green", "light-blue", "dark", "nymbo"}:
        cfg["theme"] = "auto"
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    merged = default_config()
    merged.update(cfg)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
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


class SynthesisBackend:
    """Owns the process-local pure GPT-SoVITS pipeline and loaded model pair."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._root: str | None = None
        self._pipeline: Any = None
        self._loaded: tuple[str | None, str | None] = (None, None)
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

    def synth_one(self, cfg: dict[str, Any], text: str, progress_cb: Any | None = None) -> tuple[Any, int]:
        with self._lock:
            if self._pipeline is None:
                raise RuntimeError("推理后端未就绪，请先初始化模型。")
            with backend_cwd(str(cfg["gpt_sovits_root"])):
                result = self._pipeline.run({
                    "text": text,
                    "text_lang": "zh",
                    "ref_audio_path": cfg["ref_audio"],
                    "prompt_text": cfg["ref_text"],
                    "prompt_lang": "zh",
                    "top_k": int(cfg["top_k"]),
                    "top_p": float(cfg["top_p"]),
                    "temperature": float(cfg["temperature"]),
                    "text_split_method": "cut0",
                    "batch_size": 1,
                    "speed_factor": float(cfg["speed"]),
                    "fragment_interval": float(cfg["pause_second"]),
                    "sample_steps": int(cfg["sample_steps"]),
                    "parallel_infer": False,
                })
                last = list(result)[-1]
                return last[1], last[0]


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


def synthesize_lines(cfg: dict[str, Any], text: str, output_dir: Path | None = None, progress_cb: Any | None = None) -> dict[str, Any]:
    lines = parse_lines(text)
    if not lines:
        raise ValueError("台词为空，请先输入内容。")
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
    for index, line in enumerate(lines, 1):
        rendered = to_lightmid(line) if cfg.get("auto_lightmid") else line
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
        })
        def report_generation_progress(local_progress: float) -> None:
            try:
                value = max(0.0, min(1.0, float(local_progress)))
            except (TypeError, ValueError):
                return
            completed = len([item for item in outputs if item["file"]])
            _emit_progress(progress_cb, {
                "status": "running",
                "phase": "generating",
                "current": index,
                "total": len(lines),
                "completed": completed,
                "progress": ((index - 1) + value) / len(lines),
                "current_text": line,
                "rendered_text": rendered,
                "output_dir": str(destination),
                "outputs": list(outputs),
                "error": "",
            })

        try:
            audio, sample_rate = SYNTHESIS_BACKEND.synth_one(cfg, rendered, progress_cb=report_generation_progress)
            path = save_audio(audio, sample_rate, destination, base)
            outputs.append({"text": line, "rendered_text": rendered, "file": str(path), "download_id": register_download(path), "error": ""})
        except Exception as exc:
            outputs.append({"text": line, "rendered_text": rendered, "file": "", "download_id": "", "error": f"{type(exc).__name__}: {exc}"})
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
        })

    completed = [item for item in outputs if item["file"]]
    append_history([
        {"time": datetime.now().strftime("%m-%d %H:%M"), "text": item["text"], "params": f"t{cfg['temperature']}/s{cfg['sample_steps']}", "name": Path(item["file"]).name, "file": item["file"], "download_id": item["download_id"]}
        for item in completed
    ])
    result = {"output_dir": str(destination), "total": len(lines), "completed": len(completed), "outputs": outputs}
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
    })
    return result
