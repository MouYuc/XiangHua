"""GPT-SoVITS 环境发现与默认路径推导。"""

import os
from pathlib import Path

_GPT_MODEL_DIRS = (
    "GPT_weights_v2Pro",
    "GPT_weights_v2ProPlus",
    "GPT_weights_v2",
    "GPT_weights_v3",
    "GPT_weights_v4",
    "GPT_weights",
)
_SOVITS_MODEL_DIRS = (
    "SoVITS_weights_v2Pro",
    "SoVITS_weights_v2ProPlus",
    "SoVITS_weights_v2",
    "SoVITS_weights_v3",
    "SoVITS_weights_v4",
    "SoVITS_weights",
)
_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


def scan_gpt_sovits_roots(project_root):
    """扫描项目根目录的直接子目录，返回可用 GPT-SoVITS 环境。"""
    root = Path(project_root).expanduser()
    if not root.is_dir():
        return []

    candidates = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and (child / "GPT_SoVITS").is_dir():
            candidates.append({"label": child.name, "path": str(child)})
    return candidates


def resolve_default_environment_root(project_root, configured_root=None):
    """解析默认 GPT-SoVITS 根目录：先尊重显式配置，再从项目根目录自动发现。"""
    configured = configured_root if configured_root is not None else os.environ.get("GPT_SOVITS_ROOT")
    if configured:
        return str(Path(configured).expanduser())
    candidates = scan_gpt_sovits_roots(project_root)
    if candidates:
        return candidates[0]["path"]
    return str(Path(project_root).expanduser() / "GPT-SoVITS")


def _first_matching_file(root, directory_names, suffixes):
    for directory_name in directory_names:
        directory = root / directory_name
        if not directory.is_dir():
            continue
        files = sorted(
            (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in suffixes),
            key=lambda p: p.name.lower(),
        )
        if files:
            return str(files[0])
    return ""


def _first_audio(root):
    directory = root / "custom_data" / "sliced"
    if not directory.is_dir():
        return ""
    files = sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES),
        key=lambda p: p.name.lower(),
    )
    return str(files[0]) if files else ""


def _preserved_file(root, cfg, key):
    candidate = (cfg or {}).get(key, "")
    if not candidate:
        return ""
    path = Path(candidate).expanduser()
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return ""
    return str(path) if path.is_file() else ""


def derive_environment_config(selected_root, current_config=None):
    """从选中的环境目录推导模型和参考音频路径。"""
    root = Path(selected_root).expanduser()
    return {
        "gpt_sovits_root": str(root),
        "gpt_model": _preserved_file(root, current_config, "gpt_model")
        or _first_matching_file(root, _GPT_MODEL_DIRS, {".ckpt"}),
        "sovits_model": _preserved_file(root, current_config, "sovits_model")
        or _first_matching_file(root, _SOVITS_MODEL_DIRS, {".pth"}),
        "ref_audio": _preserved_file(root, current_config, "ref_audio") or _first_audio(root),
    }
