"""Automated environment scan, diagnostics and safe repair helpers for the WebUI.

``environment_check``/``training_stats`` moved here verbatim from
``backend.services.core`` (2026-11, third wave); ``core`` re-exports both names
for the routers. This module reads configuration straight from
:mod:`backend.services.config_store`, so importing it from ``core`` creates no
facade cycle.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import soundfile as sf

from backend.infra.gptsovits_env import derive_environment_config, scan_gpt_sovits_roots
from backend.infra.paths import REPO_ROOT, TRAINING_AUDIO_SUFFIXES
from backend.services.config_store import load_config, save_config
from backend.services.training import apply_patch, missing_deps, patch_status, start_deps_job


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


def _root_is_valid(root: str) -> bool:
    path = Path(root).expanduser()
    return path.is_dir() and (path / "GPT_SoVITS").is_dir()


def run_doctor(repair: bool = False) -> dict[str, Any]:
    cfg = load_config()
    actions: list[dict[str, Any]] = []
    candidates = scan_gpt_sovits_roots(REPO_ROOT)
    selected = str(cfg.get("gpt_sovits_root") or "")
    root_valid = _root_is_valid(selected)

    if repair and (not root_valid or not str(cfg.get("gpt_model") or "").strip() or not str(cfg.get("sovits_model") or "").strip() or not str(cfg.get("ref_audio") or "").strip()):
        if candidates:
            selected = candidates[0]["path"]
            cfg.update(derive_environment_config(selected, cfg))
            cfg = save_config(cfg)
            root_valid = True
            actions.append({"kind": "config", "status": "fixed", "message": f"已自动选择环境：{selected}"})
        else:
            actions.append({"kind": "config", "status": "warn", "message": "未找到可自动选择的 GPT-SoVITS 环境。"})

    rows = environment_check(cfg)

    if repair and root_valid and not patch_status(selected):
        ok, message = apply_patch(selected)
        actions.append({"kind": "patch", "status": "fixed" if ok else "fail", "message": message})
        rows = environment_check(cfg)

    deps = missing_deps()
    if deps:
        if repair:
            job_id = start_deps_job(deps)
            actions.append({"kind": "deps", "status": "running", "job_id": job_id, "message": "已启动训练依赖安装。"})
        else:
            actions.append({"kind": "deps", "status": "warn", "message": "训练依赖缺失： " + ", ".join(deps)})

    summary = "环境已正常" if all(row["status"] == "ok" for row in rows) else "存在需要处理的环境项"
    if actions and repair:
        summary = "已执行自动修复，正在等待部分后台任务完成" if any(action.get("status") == "running" for action in actions) else "已执行自动修复"

    return {
        "repair": repair,
        "summary": summary,
        "repaired": any(action.get("status") == "fixed" for action in actions) or any(action.get("status") == "running" for action in actions),
        "selected": selected,
        "rows": rows,
        "actions": actions,
        "config": cfg,
    }
