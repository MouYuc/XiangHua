"""Shim: 用户发音词典已迁至 :mod:`backend.domain.user_lexicon`。

domain → services 的包级环已由本次迁移消除；全部符号按原名 re-export，
backend 内部一律直接 import 新位置，本垫片只留给 tools/scripts 与旧测试。
"""

from backend.domain.user_lexicon import (  # noqa: F401 - shim re-exports
    LEXICON_SCHEMA_VERSION,
    MAX_ENTRIES,
    MAX_REPLACEMENT_CHARS,
    MAX_TERM_CHARS,
    LexiconError,
    active_overrides,
    delete_entry,
    lexicon_path,
    list_entries,
    load_lexicon,
    upsert_entry,
    validate_entry,
)
