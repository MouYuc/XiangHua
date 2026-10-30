"""Durable job snapshots so a backend restart no longer loses running jobs.

Each job writes ``root/jobs/<job_id>.json``; ``load_recent`` restores them
newest-first for the registry to re-adopt, and ``purge`` bounds the disk
footprint to the most recent snapshots. Writes are atomic (unique uuid tmp
name + ``os.replace``) with a short ``PermissionError`` backoff because
Windows file indexing intermittently locks freshly written JSON files.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


class JobStore:
    """Snapshot persistence under ``root/jobs``, one JSON file per job."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._jobs_dir = self.root / "jobs"

    def _path_for(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"

    def save(self, snapshot: dict[str, Any]) -> None:
        """Persist one snapshot; snapshots without a usable id are ignored."""
        if not isinstance(snapshot, dict):
            return
        job_id = snapshot.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            return
        job_id = job_id.strip()
        # job_id becomes a filename: separators and dot-names must never
        # escape the jobs directory.
        if "/" in job_id or "\\" in job_id or job_id in {".", ".."}:
            return
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        target = self._path_for(job_id)
        tmp = self._jobs_dir / f"{job_id}.{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for attempt in range(6):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def load_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to ``limit`` snapshots, newest ``updated_at`` first."""
        if not self._jobs_dir.is_dir():
            return []
        snapshots: list[dict[str, Any]] = []
        for entry in sorted(self._jobs_dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                payload = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                snapshots.append(payload)
        snapshots.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return snapshots[: max(0, int(limit))]

    def purge(self, keep: int = 200) -> int:
        """Delete all but the newest ``keep`` snapshots; return the count removed."""
        if not self._jobs_dir.is_dir():
            return 0
        entries: list[tuple[str, Path]] = []
        for entry in sorted(self._jobs_dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                payload = json.loads(entry.read_text(encoding="utf-8"))
                updated = (
                    str(payload.get("updated_at") or "")
                    if isinstance(payload, dict)
                    else ""
                )
            except (OSError, ValueError):
                # Unreadable files have no timestamp and sort oldest, so they
                # are the first to go once the retention bound is exceeded.
                updated = ""
            entries.append((updated, entry))
        entries.sort(key=lambda item: item[0], reverse=True)
        keep = max(0, int(keep))
        deleted = 0
        for _, path in entries[keep:]:
            try:
                path.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                continue
        return deleted
