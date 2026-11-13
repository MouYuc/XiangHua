"""Shim: GPT-SoVITS 环境发现已迁至 :mod:`backend.infra.gptsovits_env`。

全部符号按原名 re-export；backend 内部一律直接 import 新位置，
本垫片只留给 scripts 与旧测试。
"""

from backend.infra.gptsovits_env import (  # noqa: F401 - shim re-exports
    _AUDIO_SUFFIXES,
    _GPT_MODEL_DIRS,
    _SOVITS_MODEL_DIRS,
    _first_audio,
    _first_matching_file,
    _preserved_file,
    derive_environment_config,
    resolve_default_environment_root,
    scan_gpt_sovits_roots,
)
