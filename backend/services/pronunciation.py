"""Shim: 档位发音层已迁至 :mod:`backend.domain.pronunciation`。

domain → services 的包级环已由本次迁移消除；全部符号按原名 re-export，
backend 内部一律直接 import 新位置，本垫片只留给 tools/scripts 与旧测试。
"""

from backend.domain.pronunciation import (  # noqa: F401 - shim re-exports
    HEAVY_REPLACEMENTS,
    LEXICON_ZH_BASE,
    LIGHT_REPLACEMENTS,
    MEDIUM_REPLACEMENTS,
    PROTECTED_TERMS,
    AccentLevel,
    PronunciationPlan,
    accent_intensity_for,
    build_pronunciation_plan,
)
