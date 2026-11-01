"""Locked, versioned, corruption-tolerant JSON persistence.

Why this exists
---------------
The app stores nearly all of its runtime state as JSON files under
``.guangpu_webui``: config, history, the download registry, job snapshots, voice
cards, the user lexicon. Each of those used to open the file, mutate a dict and
write it back by hand. That produced three recurring bugs:

* **Lost updates.** Two writers each read, mutate and write; the second write
  erases the first. ``append_history`` could drop the entire history.
* **Torn files.** ``write_text`` truncates before writing, so a crash left an
  unparsable file that readers reported as "empty" — and the next write then
  persisted that emptiness forever.
* **Silent data loss.** A corrupt ``config.json`` was swallowed and replaced by
  defaults, so the user's settings disappeared with no message.

This module fixes all three at one place:

* every path has **one process-wide lock**, so two ``JsonStore`` objects for the
  same file serialize against each other;
* writes go through :mod:`backend.infra.atomic` (unique temp name + ``os.replace``);
* a corrupt payload is **moved aside** (``*.corrupt-<timestamp>``) and reported
  through ``StoreCorruptError`` so the caller can warn the user instead of
  overwriting their data;
* every payload carries a ``schema_version`` and the store runs registered
  migrations in order before handing data back.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.infra.atomic import atomic_write_json
from backend.infra.logging import get_logger

logger = get_logger("xianghua.store")

#: One lock per resolved path. Two stores over the same file must exclude each
#: other, so the lock cannot live on the store instance.
_PATH_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve() if path.is_absolute() else path.absolute())
    with _LOCKS_GUARD:
        existing = _PATH_LOCKS.get(key)
        if existing is None:
            existing = threading.RLock()
            _PATH_LOCKS[key] = existing
        return existing


class StoreCorruptError(RuntimeError):
    """The file on disk could not be parsed; it has been preserved for recovery."""

    def __init__(self, path: Path, backup: Path | None, detail: str) -> None:
        self.path = path
        self.backup = backup
        self.detail = detail
        message = f"配置文件损坏：{path.name}"
        if backup is not None:
            message += f"（原文件已备份为 {backup.name}）"
        super().__init__(message)


Migration = Callable[[Any], Any]


class JsonStore:
    """A single JSON file with a lock, a version and a corruption policy.

    Parameters
    ----------
    path:
        File to read/write. Parents are created on write.
    default:
        Value returned when the file does not exist. Called fresh each time so
        callers never share a mutable default.
    schema_version:
        Version this code writes. A file with a lower version is migrated; a
        file with a *higher* version is left untouched and reported as corrupt
        (downgrade would lose fields the newer code wrote).
    migrations:
        ``[(from_version, fn), ...]``. Each ``fn`` receives the raw payload and
        returns the payload at ``from_version + 1``.
    expose_version:
        Whether to inject/maintain a ``schema_version`` key. Wrappers that must
        keep the legacy on-disk shape (a bare list, say) set this to ``False``
        and track versioning themselves.
    """

    def __init__(
        self,
        path: Path,
        *,
        default: Callable[[], Any],
        schema_version: int = 1,
        migrations: Iterable[tuple[int, Migration]] = (),
        expose_version: bool = True,
        validate: Callable[[Any], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        self._default = default
        self.schema_version = int(schema_version)
        self._migrations = sorted(migrations, key=lambda item: item[0])
        self._expose_version = expose_version
        self._validate = validate
        self._lock = _lock_for(self.path)

    # ------------------------------------------------------------------ read

    def read(self) -> Any:
        """Current payload, migrated to ``schema_version``.

        Raises :class:`StoreCorruptError` when the file exists but cannot be
        understood — callers that can tolerate it should catch and use
        :meth:`default_payload`.
        """
        with self._lock:
            return self._read_locked()

    def read_or_default(self) -> Any:
        """Like :meth:`read` but treats corruption as "no data yet" *after*
        preserving the broken file. Used by non-critical caches (download
        registry, job snapshots) where a failure must not take the app down."""
        try:
            return self.read()
        except StoreCorruptError:
            return self.default_payload()

    def default_payload(self) -> Any:
        return self._default()

    def _read_locked(self) -> Any:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self.default_payload()
        except OSError as exc:
            logger.warning("读取 %s 失败：%s", self.path.name, exc)
            return self.default_payload()

        if not raw.strip():
            # Empty file = interrupted write. Treat as absent, do not "recover".
            logger.warning("%s 为空文件，按暂无数据处理。", self.path.name)
            return self.default_payload()

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            backup = self._quarantine("无法解析 JSON")
            raise StoreCorruptError(self.path, backup, str(exc)) from exc

        if self._validate is not None and not self._validate(payload):
            backup = self._quarantine("结构不符合预期")
            raise StoreCorruptError(self.path, backup, "payload shape mismatch")

        return self._migrate(payload)

    def _migrate(self, payload: Any) -> Any:
        if not self._expose_version:
            return payload
        found = 0
        if isinstance(payload, dict):
            try:
                found = int(payload.get("schema_version") or 0)
            except (TypeError, ValueError):
                found = 0
        if found > self.schema_version:
            backup = self._quarantine("版本高于当前程序")
            raise StoreCorruptError(
                self.path, backup, f"schema_version={found} > {self.schema_version}"
            )
        for from_version, migrate in self._migrations:
            if from_version < max(found, 1) or from_version >= self.schema_version:
                continue
            payload = migrate(payload)
            logger.info("%s 从 v%s 迁移到 v%s。", self.path.name, from_version, from_version + 1)
        if isinstance(payload, dict):
            payload["schema_version"] = self.schema_version
        return payload

    def _quarantine(self, reason: str) -> Path | None:
        """Move an unreadable file aside so the next write cannot destroy it."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            self.path.replace(backup)
        except OSError as exc:  # pragma: no cover - best effort
            logger.error("无法备份损坏的 %s：%s", self.path.name, exc)
            return None
        logger.error("%s 已损坏（%s），原文件保留为 %s。", self.path.name, reason, backup.name)
        return backup

    # ----------------------------------------------------------------- write

    def write(self, payload: Any) -> Any:
        """Atomically persist ``payload``; returns what was written."""
        with self._lock:
            return self._write_locked(payload)

    def _write_locked(self, payload: Any) -> Any:
        if self._expose_version and isinstance(payload, dict):
            payload = {**payload, "schema_version": self.schema_version}
        atomic_write_json(self.path, payload)
        return payload

    def update(self, mutator: Callable[[Any], Any]) -> Any:
        """Read-modify-write under one lock.

        ``mutator`` receives the current payload and returns the new one; if it
        returns ``None`` nothing is written (useful for "already up to date").
        This is the only supported way to change a file that several workers
        touch — a caller that reads and writes separately reintroduces the lost
        update this class exists to prevent.
        """
        with self._lock:
            try:
                current = self._read_locked()
            except StoreCorruptError:
                current = self.default_payload()
            updated = mutator(current)
            if updated is None:
                return current
            return self._write_locked(updated)
