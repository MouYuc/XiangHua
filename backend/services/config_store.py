"""Config persistence: defaults, overlay read and atomic merged write.

Extracted verbatim from ``backend.services.core`` (2026-11, third wave) so the
facade converges into pure re-exports. ``core`` re-exports ``default_config``/
``load_config``/``save_config`` unchanged, so ``core.load_config()`` call sites
and ``patch.object(core, "load_config")`` test seams keep working; this module
must never import ``core`` (that would rebuild the facade cycle).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from backend.domain.pronunciation import AccentLevel
from backend.domain.text import SUPPORTED_SYNTHESIS_LANGUAGES
from backend.infra import jsonstore
from backend.infra.gptsovits_env import resolve_default_environment_root
from backend.infra.logging import get_logger
from backend.infra.paths import (
    CONFIG_FILE,
    CONFIG_SCHEMA_VERSION,
    ORB_COLOR_DEFAULTS,
    REPO_ROOT,
)

# Same logger namespace as the facade these functions were extracted from, so
# structured-log output is byte-identical with pre-refactor runs.
logger = get_logger("xianghua.core")


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
    picker repoint ``CONFIG_FILE``/``CONFIG_DIR`` at runtime (previously on
    ``core``, now on this module), and the ``JsonStore`` lock registry is keyed
    by path, so a fresh instance still shares the same lock as every other
    writer of that file.
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
