"""User-editable pronunciation lexicon (多音字 / 专名 / 游戏术语读音修正).

The dictionary maps a term to a homophone replacement that is applied **only to
the engine's pronunciation text** — the source text the user sees (and the
subtitle export) keeps the original spelling. Entries are the highest-priority
layer of :mod:`backend.domain.pronunciation`: they override accent rules and
even the built-in protected-compound table, because they represent an explicit
user decision like "在这个项目里，'重创' 一律读 chóng chuàng".

Storage is one JSON file under the webui config dir::

    <CONFIG_DIR>/lexicon.json
    {"entries": {"重创": {"replacement": "虫创", "note": "读 chóng chuàng", "enabled": true,
                           "updated_at": "..."}}, "version": 1}

The file is read on every synthesis line, so edits take effect on the next
generation without a restart. A missing or corrupt file is treated as an empty
dictionary — never a synthesis failure.

Moved verbatim from ``backend.services.user_lexicon`` (2026-11, third wave) to
break the ``domain -> services -> domain`` import cycle; the old location keeps
a re-export shim for tools, scripts and tests.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from backend.infra.paths import CONFIG_DIR

LEXICON_SCHEMA_VERSION = 1
MAX_TERM_CHARS = 32
MAX_REPLACEMENT_CHARS = 128
MAX_ENTRIES = 500

_LOCK = threading.RLock()


class LexiconError(ValueError):
    """A user-actionable lexicon failure; surfaces as HTTP 400."""


def lexicon_path() -> Path:
    return CONFIG_DIR / "lexicon.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_lexicon(path: Path | None = None) -> dict:
    """Read the raw lexicon document; unreadable files degrade to empty."""
    source = path or lexicon_path()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": LEXICON_SCHEMA_VERSION, "entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"version": LEXICON_SCHEMA_VERSION, "entries": {}}
    return data


def _save_lexicon(data: dict, path: Path | None = None) -> None:
    target = path or lexicon_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def active_overrides(path: Path | None = None) -> dict[str, str]:
    """Enabled ``term -> replacement`` pairs consumed by the pronunciation layer."""
    entries = load_lexicon(path).get("entries") or {}
    return {
        str(term): str(entry.get("replacement") or "")
        for term, entry in entries.items()
        if isinstance(entry, dict) and entry.get("enabled", True) and entry.get("replacement")
    }


def list_entries(path: Path | None = None) -> list[dict]:
    data = load_lexicon(path)
    rows = []
    for term, entry in sorted((data.get("entries") or {}).items(), key=lambda item: -len(item[0])):
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "term": str(term),
                "replacement": str(entry.get("replacement") or ""),
                "note": str(entry.get("note") or ""),
                "enabled": bool(entry.get("enabled", True)),
                "updated_at": str(entry.get("updated_at") or ""),
            }
        )
    return rows


def validate_entry(term: str, replacement: str) -> tuple[str, str]:
    cleaned_term = str(term or "").strip()
    cleaned_replacement = str(replacement or "").strip()
    if not cleaned_term or not cleaned_replacement:
        raise LexiconError("词条和替换读法都不能为空。")
    if cleaned_term == cleaned_replacement:
        raise LexiconError("替换读法与原词相同，无需添加。")
    if len(cleaned_term) > MAX_TERM_CHARS:
        raise LexiconError(f"词条过长（最多 {MAX_TERM_CHARS} 字）。")
    if len(cleaned_replacement) > MAX_REPLACEMENT_CHARS:
        raise LexiconError(f"替换读法过长（最多 {MAX_REPLACEMENT_CHARS} 字）。")
    return cleaned_term, cleaned_replacement


def upsert_entry(
    term: str,
    replacement: str,
    note: str = "",
    enabled: bool = True,
    path: Path | None = None,
) -> dict:
    """Add or update one entry; returns the stored row."""
    cleaned_term, cleaned_replacement = validate_entry(term, replacement)
    with _LOCK:
        data = load_lexicon(path)
        data.setdefault("version", LEXICON_SCHEMA_VERSION)
        entries = data.setdefault("entries", {})
        if cleaned_term not in entries and len(entries) >= MAX_ENTRIES:
            raise LexiconError(f"词典最多 {MAX_ENTRIES} 条，请先删除不用的词条。")
        row = {
            "replacement": cleaned_replacement,
            "note": str(note or "").strip()[:200],
            "enabled": bool(enabled),
            "updated_at": _now(),
        }
        entries[cleaned_term] = row
        _save_lexicon(data, path)
    return {"term": cleaned_term, **row}


def delete_entry(term: str, path: Path | None = None) -> bool:
    with _LOCK:
        data = load_lexicon(path)
        entries = data.get("entries") or {}
        if term not in entries:
            return False
        del entries[term]
        _save_lexicon(data, path)
        return True
