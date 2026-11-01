"""Disk retention: keep the output tree from growing without bound.

The app writes a timestamped directory per synthesis run, a JSON snapshot per
background job, a copy of every uploaded recording, and a dataset per media
analysis. Nothing ever removed any of it: after a few weeks the repository held
89 output directories, a 604-entry download registry and several GB of media.

This module is the single place that decides what may be deleted. Rules:

* **Never touch** user assets: ``custom_data/``, weights, ``reference_audio/``,
  ``voice_cards/``, ``lexicon.json``, ``provenance.key``, ``config.json``.
* **Only prune** synthesised output directories that are older than the
  configured age *and* are not referenced by history or the download registry.
* **Dry run by default.** ``prune()`` reports what it would delete unless
  ``apply=True``, and the API exposes it that way, because a retention policy
  that silently deletes the user's work is worse than a full disk.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.infra.logging import get_logger
from backend.services import core

logger = get_logger("xianghua.retention")

#: Output directories older than this are eligible for pruning.
DEFAULT_MAX_AGE_DAYS = 30
#: Upper bound on retained synthesised output directories, regardless of age.
DEFAULT_MAX_OUTPUT_DIRS = 500
#: Job snapshots kept on disk (``JobStore.purge``).
DEFAULT_JOB_SNAPSHOTS = 200
#: Shape of a per-run output directory written by ``core.new_output_dir()``.
#: Anything else under ``output/`` (``vc/``, ad-hoc folders) is not ours to delete.
_RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")


@dataclass
class PruneEntry:
    path: str
    kind: str
    bytes: int
    age_days: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "bytes": self.bytes,
            "age_days": round(self.age_days, 1),
            "reason": self.reason,
        }


@dataclass
class PrunePlan:
    entries: list[PruneEntry] = field(default_factory=list)
    kept_bytes: int = 0
    kept_count: int = 0

    @property
    def reclaimable_bytes(self) -> int:
        return sum(entry.bytes for entry in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "reclaimable_bytes": self.reclaimable_bytes,
            "kept_bytes": self.kept_bytes,
            "kept_count": self.kept_count,
        }


def _directory_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _referenced_output_dirs() -> set[str]:
    """Output directories still reachable from history or the download registry."""
    referenced: set[str] = set()
    for entry in core.load_history():
        for key in ("file", "dir", "output_dir"):
            value = str(entry.get(key) or "")
            if value:
                referenced.add(str(Path(value).expanduser().parent))
                referenced.add(str(Path(value).expanduser()))
    for entry in core._load_downloads().values():
        value = str(entry.get("path") or "")
        if value:
            referenced.add(str(Path(value).parent))
    return referenced


def scan(
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    max_output_dirs: int = DEFAULT_MAX_OUTPUT_DIRS,
) -> PrunePlan:
    """Describe what :func:`prune` would remove. Never deletes anything."""
    plan = PrunePlan()
    output_root = core.OUTPUT_DIR
    if not output_root.is_dir():
        return plan

    referenced = _referenced_output_dirs()
    now = time.time()
    candidates: list[tuple[float, Path]] = []

    for child in sorted(output_root.iterdir()):
        if not child.is_dir():
            continue
        # ``output/`` holds two shapes: per-run directories written by
        # ``core.new_output_dir()`` and long-lived category folders such as
        # ``vc/`` that other pipelines own. Only the former are synthesis runs.
        if not _RUN_DIR_PATTERN.fullmatch(child.name):
            plan.kept_bytes += _directory_bytes(child)
            plan.kept_count += 1
            continue
        try:
            age_days = (now - child.stat().st_mtime) / 86400.0
        except OSError:
            continue
        size = _directory_bytes(child)
        if str(child) in referenced:
            plan.kept_bytes += size
            plan.kept_count += 1
            continue
        candidates.append((age_days, child))

    # Oldest first, so the cap always removes the least recently used.
    candidates.sort(key=lambda item: item[0], reverse=True)
    overflowing = max(0, len(candidates) - max_output_dirs)

    for index, (age_days, child) in enumerate(candidates):
        too_old = age_days >= max_age_days
        over_cap = index < overflowing
        if not (too_old or over_cap):
            size = _directory_bytes(child)
            plan.kept_bytes += size
            plan.kept_count += 1
            continue
        reason = "超过保留天数" if too_old else "超过保留数量上限"
        plan.entries.append(
            PruneEntry(
                path=str(child),
                kind="synthesis_output",
                bytes=_directory_bytes(child),
                age_days=age_days,
                reason=reason,
            )
        )
    return plan


def prune(
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    max_output_dirs: int = DEFAULT_MAX_OUTPUT_DIRS,
    job_snapshots: int = DEFAULT_JOB_SNAPSHOTS,
    apply: bool = False,
) -> dict[str, Any]:
    """Prune synthesised output older than the policy allows.

    ``apply=False`` (the default) only reports. Deleting anything requires an
    explicit opt-in at the call site, and the API surfaces both modes.
    """
    plan = scan(max_age_days=max_age_days, max_output_dirs=max_output_dirs)
    removed = 0
    freed = 0
    errors: list[str] = []

    for entry in plan.entries:
        target = Path(entry.path)
        if not apply:
            continue
        try:
            shutil.rmtree(target)
            removed += 1
            freed += entry.bytes
        except OSError as exc:
            errors.append(f"{target.name}: {exc}")
            logger.warning("清理输出目录失败：%s", exc)

    snapshot_note = ""
    if apply:
        try:
            from backend.services.job_store import JobStore

            purged = JobStore(core.CONFIG_DIR).purge(keep=job_snapshots)
            snapshot_note = f"清理任务快照 {purged} 个。"
        except Exception as exc:  # pragma: no cover - disk-dependent
            errors.append(f"任务快照清理失败：{exc}")

    if apply and (removed or snapshot_note):
        logger.info(
            "保留策略执行：删除 %s 个输出目录，释放 %.1f MB。%s",
            removed,
            freed / (1024 * 1024),
            snapshot_note,
        )

    return {
        **plan.to_dict(),
        "applied": apply,
        "removed": removed,
        "freed_bytes": freed,
        "errors": errors,
        "job_store": snapshot_note,
        "policy": {
            "max_age_days": max_age_days,
            "max_output_dirs": max_output_dirs,
            "job_snapshots": job_snapshots,
        },
    }
