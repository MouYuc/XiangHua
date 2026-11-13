"""History and download-credential persistence.

Extracted verbatim from ``backend.services.core`` (2026-11, third wave) so the
facade converges into pure re-exports. ``core`` re-exports ``load_history``/
``append_history``/``register_download``/``_load_downloads``/... unchanged, so
``retention`` (which reads those names through the ``core`` namespace and is
patched that way in tests) keeps working; this module must never import
``core`` (that would rebuild the facade cycle).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from backend.infra import jsonstore
from backend.infra.logging import get_logger
from backend.infra.paths import (
    CONFIG_DIR,
    DOWNLOADABLE_SUFFIXES,
    DOWNLOADS_LIMIT,
    HISTORY_FILE,
    HISTORY_LIMIT,
)

# Same logger namespace as the facade these functions were extracted from, so
# structured-log output is byte-identical with pre-refactor runs.
logger = get_logger("xianghua.core")


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
